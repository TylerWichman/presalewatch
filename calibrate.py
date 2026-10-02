"""Score predicted (Mode B) edges against actual resale, and refit the heuristic.

Each event's last prediction made before its public on-sale is scored against
the SeatGeek median 7 and 14 days after on-sale:

    actual multiple = median resale at day N * (1 - ask_to_sale_discount) / face

SeatGeek medians are asking prices, so they get the same discount edge.py
applies in Live mode, and predicted multiples are scored on that sale basis.

Metrics (day 7): tier accuracy (do High events resell higher than Med and
Low?), share of actual multiples inside the predicted range (target >= 70%),
and mean absolute error on the multiple.

With --refit and 50+ scored events, a log-linear regression of the actual
multiple on the demand signals replaces the weights, and the tier cutoffs and
multiple ranges are re-derived from its residuals. Review monthly, or every 25
newly scored events; the report says when a review is due.

Usage:
    python calibrate.py            # score and report (runs after every pipeline run)
    python calibrate.py --refit    # also rewrite weights and tiers in config/model.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

import edge
from common import CONFIG_PATH, DB_DIR, iso, load_config, num, parse_utc, read_table, write_table

SCORE_FIELDS = [
    "event_id", "artist", "onsale_date", "predicted_at", "tier", "demand_score", "multiple_low",
    "multiple_high", "listeners", "engagement", "listeners_capacity", "scarcity", "market", "face",
    "median_d7", "multiple_d7", "median_d14", "multiple_d14", "scored_at",
]
REPORT_PATH = DB_DIR / "calibration.json"
# Tier boundaries in multiple space: at or above 1.8x is High, 1.2x-1.8x Med, below Low.
MED_MULTIPLE, HIGH_MULTIPLE = 1.2, 1.8


def closest_snapshot(snaps: list[dict], target: datetime, window_h: float) -> dict | None:
    best, best_gap = None, None
    for s in snaps:
        at = parse_utc(s["captured_at"])
        median = num(s["median"])
        if not at or not median:
            continue
        gap = abs((at - target).total_seconds()) / 3600
        if gap <= window_h and (best_gap is None or gap < best_gap):
            best, best_gap = s, gap
    return best


def score(now: datetime, cfg: dict) -> list[dict]:
    events = {r["event_id"]: r for r in read_table("events")}
    preds: dict[str, list[dict]] = {}
    for r in read_table("predictions"):
        if r["mode"] == "predicted":
            preds.setdefault(r["event_id"], []).append(r)
    snaps: dict[str, list[dict]] = {}
    for r in read_table("resale_snapshots"):
        snaps.setdefault(r["event_id"], []).append(r)
    scores = {r["event_id"]: r for r in read_table("scores")}
    window = cfg["calibration"]["snapshot_window_hours"]

    for eid, rows in preds.items():
        ev = events.get(eid)
        onsale = parse_utc((ev or {}).get("onsale_date"))
        if not onsale or now < onsale + timedelta(days=7) or eid not in snaps:
            continue
        before = [r for r in rows if (parse_utc(r["predicted_at"]) or now) <= onsale]
        if not before:
            continue  # first seen after on-sale: nothing was actually predicted
        pred = max(before, key=lambda r: r["predicted_at"])
        if pred.get("tier") == edge.UNRATED:
            continue  # no Last.fm data: nothing was predicted
        face = num(pred["face"]) or edge.face_price(num(ev.get("face_min")), num(ev.get("face_max")), cfg)
        if not face:
            continue
        row = scores.get(eid) or {
            "event_id": eid, "artist": ev.get("artist"), "onsale_date": ev.get("onsale_date"),
            **{k: pred.get(k) for k in ("predicted_at", "tier", "demand_score", "multiple_low", "multiple_high",
                                        *edge.SIGNALS)},
            "face": face,
        }
        changed = False
        for day in (7, 14):
            if row.get(f"multiple_d{day}") or now < onsale + timedelta(days=day):
                continue
            snap = closest_snapshot(snaps[eid], onsale + timedelta(days=day), window)
            if snap:
                row[f"median_d{day}"] = num(snap["median"])
                row[f"multiple_d{day}"] = round(edge.sale_price(num(snap["median"]), cfg) / face, 4)
                changed = True
        if changed:
            row["scored_at"] = iso(now)
            scores[eid] = row
    rows = [r for r in scores.values() if r.get("multiple_d7")]
    write_table("scores", sorted(rows, key=lambda r: r["event_id"]), SCORE_FIELDS)
    return rows


def metrics(rows: list[dict], cfg: dict) -> dict:
    if not rows:
        return {"scored": 0}
    by_tier: dict[str, list[float]] = {}
    inside, errors = 0, []
    for r in rows:
        actual, lo, hi = num(r["multiple_d7"]), num(r["multiple_low"]), num(r["multiple_high"])
        by_tier.setdefault(r["tier"], []).append(actual)
        inside += lo <= actual <= hi
        errors.append(abs(actual - (lo + hi) / 2))
    order = [t["name"] for t in sorted(cfg["tiers"], key=lambda t: t["min_score"], reverse=True)]
    means = {t: sum(v) / len(v) for t, v in by_tier.items()}
    present = [means[t] for t in order if t in means]
    return {
        "scored": len(rows),
        "tiers": {t: {"events": len(by_tier[t]), "mean_multiple": round(means[t], 3)} for t in order if t in by_tier},
        "tier_order_holds": all(a > b for a, b in zip(present, present[1:])),
        "share_in_range": round(inside / len(rows), 3),
        "share_in_range_target": 0.70,
        "mae_multiple": round(sum(errors) / len(errors), 3),
    }


def review_due(n: int, now: datetime, cfg: dict) -> bool:
    c = cfg["calibration"]
    if n < c["min_scored_for_refit"]:
        return False
    last = parse_utc(cfg.get("calibrated_at"))
    if last is None:
        return True
    return (n - (cfg.get("calibrated_on_events") or 0) >= c["review_every_scored"]
            or now - last >= timedelta(days=c["review_every_days"]))


# ---- Refit ----------------------------------------------------------------------

def signal_matrix(rows: list[dict], cfg: dict) -> list[list[float]]:
    fill = cfg["refit_missing_value"]  # least squares needs a value for every signal
    return [[1.0] + [num(r.get(k)) if num(r.get(k)) is not None else fill for k in edge.SIGNALS] for r in rows]


def least_squares(x: list[list[float]], y: list[float], ridge: float = 1e-3) -> list[float]:
    """Solve (X'X + ridge*I) b = X'y by Gaussian elimination (intercept unpenalized)."""
    k = len(x[0])
    a = [[sum(row[i] * row[j] for row in x) + (ridge if i == j and i > 0 else 0.0) for j in range(k)] for i in range(k)]
    b = [sum(row[i] * yi for row, yi in zip(x, y)) for i in range(k)]
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("signals are collinear; need more varied events")
        a[col], a[pivot], b[col], b[pivot] = a[pivot], a[col], b[pivot], b[col]
        for r in range(k):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [ar - f * ac for ar, ac in zip(a[r], a[col])]
                b[r] -= f * b[col]
    return [b[i] / a[i][i] for i in range(k)]


def quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (pos - lo)


def refit(rows: list[dict], cfg: dict) -> dict:
    """New weights, cutoffs, and ranges. D keeps its 0-1 scale: log(multiple) ~ b0 + S * D."""
    x = signal_matrix(rows, cfg)
    y = [math.log(num(r["multiple_d7"])) for r in rows]
    coef = least_squares(x, y)
    b0, slopes = coef[0], [max(c, 0.0) for c in coef[1:]]  # negative effects are dropped
    total = sum(slopes)
    if total <= 0:
        raise ValueError("no signal predicts a higher resale multiple; keeping current weights")
    weights = {k: round(s / total, 4) for k, s in zip(edge.SIGNALS, slopes)}

    fitted = [b0 + sum(s * xi for s, xi in zip(slopes, row[1:])) for row in x]
    resid = [yi - fi for yi, fi in zip(y, fitted)]
    q_lo, q_hi = (quantile(resid, q) for q in cfg["calibration"]["range_quantiles"])

    def cutoff(multiple: float) -> float:
        return edge.clip((math.log(multiple) - b0) / total)

    d_med, d_high = cutoff(MED_MULTIPLE), cutoff(HIGH_MULTIPLE)

    def band(d_lo: float, d_hi: float) -> tuple[float, float]:
        return (round(math.exp(b0 + total * d_lo + q_lo), 2), round(math.exp(b0 + total * d_hi + q_hi), 2))

    tiers = []
    for name, lo, hi in (("High", d_high, 1.0), ("Med", d_med, d_high), ("Low", 0.0, d_med)):
        m_lo, m_hi = band(lo, hi)
        tiers.append({"name": name, "min_score": round(lo, 4), "multiple_low": m_lo, "multiple_high": m_hi})
    return {"weights": weights, "tiers": tiers, "intercept": round(b0, 4), "slope": round(total, 4),
            "residual_quantiles": [round(q_lo, 4), round(q_hi, 4)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--refit", action="store_true", help="rewrite weights and tiers in config/model.json")
    args = parser.parse_args()

    cfg = load_config()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = score(now, cfg)
    report = {"generated_at": iso(now), "calibrated_at": cfg.get("calibrated_at"),
              **metrics(rows, cfg), "review_due": review_due(len(rows), now, cfg)}
    need = cfg["calibration"]["min_scored_for_refit"]
    print(f"{len(rows)} predictions scored ({need} needed for a refit)")
    if rows:
        print(f"  in range: {report['share_in_range']:.0%} (target 70%), MAE {report['mae_multiple']}x, "
              f"tier order holds: {report['tier_order_holds']}")
    if report["review_due"] and not args.refit:
        print("  Review due: run `python calibrate.py --refit` and check the new tiers.")

    if args.refit:
        if len(rows) < need:
            sys.exit(f"Refit needs {need}+ scored events; have {len(rows)}.")
        try:
            fit = refit(rows, cfg)
        except ValueError as err:
            sys.exit(f"Refit skipped: {err}")
        cfg.update(weights=fit["weights"], tiers=fit["tiers"], calibrated_at=iso(now), calibrated_on_events=len(rows))
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        report.update(refit=fit, calibrated_at=cfg["calibrated_at"], review_due=False)
        print(f"  Refit on {len(rows)} events: weights {fit['weights']}")
        for t in fit["tiers"]:
            print(f"    {t['name']}: D >= {t['min_score']}, {t['multiple_low']}x-{t['multiple_high']}x")

    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
