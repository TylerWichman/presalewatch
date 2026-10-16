"""Score predicted (Mode B) edges against actual resale, and refit the heuristic.

Each event's last prediction made before its public on-sale is scored against
the SeatGeek median 7 and 14 days after on-sale:

    actual multiple = median resale at day N * (1 - ask_to_sale_discount) / face

SeatGeek medians are asking prices, so they get the same discount edge.py
applies in Live mode, and predicted multiples are scored on that sale basis.

Hand-logged prices (observed_prices in the intelligence database, read when
INTEL_DB is set) are scored separately, because they measure something else:
the cheapest standard listing per ticket, all-in with fees, for 2 tickets,
logged 7 and 14 days after general on-sale (see docs/logging_prices.md). Their
multiple is that price over the all-in face cost, with no ask-to-sale discount.
They never feed the median-based metrics or the refit; their report is tier
order plus the rank correlation between demand score and actual multiple.

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
from common import CONFIG_PATH, DB_DIR, env, iso, load_config, num, parse_utc, read_table, write_table

SCORE_FIELDS = [
    "event_id", "artist", "onsale_date", "predicted_at", "tier", "demand_score", "multiple_low",
    "multiple_high", "listeners", "engagement", "listeners_capacity", "scarcity", "market", "face",
    "median_d7", "multiple_d7", "median_d14", "multiple_d14",
    "getin_d7", "getin_multiple_d7", "getin_d14", "getin_multiple_d14", "getin_source", "scored_at",
]
REPORT_PATH = DB_DIR / "calibration.json"
# Tier boundaries in multiple space: at or above 1.8x is High, 1.2x-1.8x Med, below Low.
MED_MULTIPLE, HIGH_MULTIPLE = 1.2, 1.8
# A hand-logged price counts for day N if it was logged within this many days of on-sale + N.
LOG_WINDOW_DAYS = 1
# Only rows logged the way docs/logging_prices.md says.
HAND_LOGGED_SQL = (
    "SELECT e.ticketmaster_id AS event_id, o.price, o.observed_on, o.source FROM observed_prices o"
    " JOIN events e ON e.id = o.event_id WHERE o.kind = 'resale' AND o.price_basis = 'ask'"
    " AND o.price_point = 'get_in' AND o.standard_ticket = 1 AND o.fees_included = 1"
)


def hand_logged() -> dict[str, list[dict]]:
    """Hand-logged cheapest-listing prices by Ticketmaster event ID, from the intelligence
    database named by INTEL_DB ('d1' or 'sqlite:<path>'). Unset or unreachable means none."""
    target = env("INTEL_DB")
    if not target:
        return {}
    try:
        from ingest.db import open_db
        rows = open_db(target).query(HAND_LOGGED_SQL)
    except (Exception, SystemExit) as err:  # noqa: BLE001 - calibration must still run without it
        print(f"  warning: intelligence database unavailable, scoring without hand-logged prices: "
              f"{type(err).__name__}: {str(err)[:150]}", file=sys.stderr)
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["event_id"], []).append(r)
    return out


def closest_log(logs: list[dict], target: datetime) -> dict | None:
    best, best_gap = None, None
    for r in logs:
        try:
            gap = abs((datetime.fromisoformat(r["observed_on"]).date() - target.date()).days)
        except (TypeError, ValueError):
            continue
        if gap <= LOG_WINDOW_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = r, gap
    return best


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


def score(now: datetime, cfg: dict, logged: dict[str, list[dict]] | None = None) -> list[dict]:
    logged = logged or {}
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
        if not onsale or now < onsale + timedelta(days=7) or (eid not in snaps and eid not in logged):
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
            if now < onsale + timedelta(days=day):
                continue
            snap = closest_snapshot(snaps.get(eid, []), onsale + timedelta(days=day), window)
            if snap and not row.get(f"multiple_d{day}"):
                row[f"median_d{day}"] = num(snap["median"])
                row[f"multiple_d{day}"] = round(edge.sale_price(num(snap["median"]), cfg) / face, 4)
                changed = True
            log = closest_log(logged.get(eid, []), onsale + timedelta(days=day))
            if log and not row.get(f"getin_multiple_d{day}"):
                cost = edge.all_in_cost(face, cfg, (ev or {}).get("fee_included") == "1")
                row[f"getin_d{day}"] = num(log["price"])
                row[f"getin_multiple_d{day}"] = round(num(log["price"]) / cost, 4)
                row["getin_source"] = log["source"]
                changed = True
        if changed:
            row["scored_at"] = iso(now)
            scores[eid] = row
    rows = [r for r in scores.values() if r.get("multiple_d7") or r.get("getin_multiple_d7")]
    write_table("scores", sorted(rows, key=lambda r: r["event_id"]), SCORE_FIELDS)
    return rows


def ranks(values: list[float]) -> list[float]:
    """Rank positions, with ties sharing their average position."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2
        i = j + 1
    return out


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(cov / var, 3) if var else None


def hand_logged_metrics(rows: list[dict], cfg: dict) -> dict:
    """Ranking checks only: a cheapest listing isn't on the scale of the median-based ranges."""
    rows = [r for r in rows if num(r.get("getin_multiple_d7")) and num(r.get("demand_score")) is not None]
    if not rows:
        return {"scored": 0}
    by_tier: dict[str, list[float]] = {}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(num(r["getin_multiple_d7"]))
    order = [t["name"] for t in sorted(cfg["tiers"], key=lambda t: t["min_score"], reverse=True)]
    means = {t: sum(v) / len(v) for t, v in by_tier.items()}
    present = [means[t] for t in order if t in means]
    return {
        "scored": len(rows),
        "tiers": {t: {"events": len(by_tier[t]), "mean_multiple": round(means[t], 3)} for t in order if t in by_tier},
        "tier_order_holds": all(a > b for a, b in zip(present, present[1:])),
        "rank_correlation": spearman([num(r["demand_score"]) for r in rows], [num(r["getin_multiple_d7"]) for r in rows]),
    }


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
    scored = score(now, cfg, hand_logged())
    rows = [r for r in scored if r.get("multiple_d7")]
    hand = hand_logged_metrics(scored, cfg)
    report = {"generated_at": iso(now), "calibrated_at": cfg.get("calibrated_at"),
              **metrics(rows, cfg), "review_due": review_due(len(rows), now, cfg), "hand_logged": hand}
    need = cfg["calibration"]["min_scored_for_refit"]
    print(f"{len(rows)} predictions scored ({need} needed for a refit)")
    if rows:
        print(f"  in range: {report['share_in_range']:.0%} (target 70%), MAE {report['mae_multiple']}x, "
              f"tier order holds: {report['tier_order_holds']}")
    if hand["scored"]:
        print(f"{hand['scored']} hand-logged events scored: rank correlation {hand['rank_correlation']}, "
              f"tier order holds: {hand['tier_order_holds']}")
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
