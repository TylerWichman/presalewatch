"""Profit % (edge) model: fee-adjusted formula, demand score, tiers, and mode choice.

Pure functions only, so the math is easy to test and to reuse in calibration.

    Profit% = (P_resale * (1 - f_seller) - (P_face + F_primary)) / (P_face + F_primary)

Mode A (live): P_resale is the median SeatGeek listing when at least
`live_min_listings` listings exist and face value is known, less
`ask_to_sale_discount`: listings are asking prices, and resale usually clears
below the ask.
Mode B (predicted): P_resale = P_face * multiple, where the multiple range
comes from the demand tier. The result is a margin range, not a point.
"""

from __future__ import annotations

import math

SIGNALS = ("listeners", "engagement", "listeners_capacity", "scarcity", "market")
# Without Last.fm data confidence is Low and the tier is capped at Med, since market
# and tour size alone can't show that people want the tickets.


def all_in_cost(face: float, cfg: dict, fee_included: bool = False) -> float:
    fee = 0.0 if fee_included else face * cfg["fees"]["primary_fee_pct"]
    return face + fee


def profit_pct(resale: float, face: float, cfg: dict, fee_included: bool = False) -> float:
    """Expected return per dollar spent, net of seller and primary fees."""
    cost = all_in_cost(face, cfg, fee_included)
    return (resale * (1 - cfg["fees"]["seller_fee"]) - cost) / cost


def sale_price(ask: float, cfg: dict) -> float:
    """Expected sale price from a resale asking price."""
    return ask * (1 - cfg.get("ask_to_sale_discount", 0.0))


def face_price(face_min: float | None, face_max: float | None, cfg: dict) -> float | None:
    """The standard ticket price the formula uses (midpoint of the standard range by default)."""
    values = [v for v in (face_min, face_max) if v is not None and v > 0]
    if not values:
        return None
    basis = cfg.get("face_basis", "mid")
    if basis == "min":
        return min(values)
    if basis == "max":
        return max(values)
    return sum(values) / len(values)


def market_tier(market_ids: list[str], cfg: dict) -> float:
    top = set(cfg["market"]["top_market_ids"])
    return cfg["market"]["top"] if top.intersection(market_ids) else cfg["market"]["other"]


def log_scale(value: float, lo: float, hi: float) -> float:
    """log10(value) mapped from [lo, hi] onto [0, 1]."""
    return clip((math.log10(max(value, 1.0)) - lo) / (hi - lo))


def demand_signals(listeners: float | None, playcount: float | None, capacity: float | None,
                   tour_dates: float | None, market: float | None, cfg: dict) -> dict:
    """Scale raw inputs to 0-1 signals. Missing inputs come back as None.

    listeners: Last.fm listeners, log-scaled (reach).
    engagement: Last.fm plays per listener, log-scaled (how hard fans listen).
    listeners_capacity: listeners per venue seat, log-scaled (demand vs supply).
    """
    s = cfg["scaling"]
    out: dict[str, float | None] = dict.fromkeys(SIGNALS)
    if listeners:
        out["listeners"] = log_scale(listeners, *s["listeners_log10"])
        if playcount:
            out["engagement"] = log_scale(playcount / listeners, *s["plays_per_listener_log10"])
        if capacity:
            out["listeners_capacity"] = log_scale(listeners / capacity, *s["listeners_per_seat_log10"])
    if tour_dates is not None and tour_dates >= 1:
        out["scarcity"] = clip(1 / tour_dates)
    if market is not None:
        out["market"] = clip(market)
    return out


def demand_score(signals: dict, cfg: dict) -> tuple[float, list[str]]:
    """Weighted demand score D over the signals that are available.

    Missing signals are left out and the remaining weights are rescaled to sum to 1,
    so a missing input neither drags the score down nor gets a made-up value.
    """
    weights = cfg["weights"]
    missing = [k for k in SIGNALS if signals.get(k) is None]
    present = [k for k in SIGNALS if signals.get(k) is not None and weights.get(k, 0) > 0]
    total = sum(weights[k] for k in present)
    if total <= 0:
        return 0.0, missing
    return clip(sum(weights[k] * signals[k] for k in present) / total), missing


def has_artist_data(missing: list[str]) -> bool:
    """Last.fm found the artist (listeners known). Plays per listener comes with it."""
    return "listeners" not in missing


def tier_for(score: float, cfg: dict) -> dict:
    tiers = sorted(cfg["tiers"], key=lambda t: t["min_score"], reverse=True)
    for tier in tiers:
        if score >= tier["min_score"]:
            return tier
    return tiers[-1]


def evaluate(*, face_min: float | None, face_max: float | None, fee_included: bool,
             signals: dict, snapshot: dict | None, cfg: dict) -> dict:
    """Pick Mode A or B and compute the edge for one event."""
    score, missing = demand_score(signals, cfg)
    tier = tier_for(score, cfg)
    if not has_artist_data(missing) and tier["name"] == "High":
        tier = next(t for t in cfg["tiers"] if t["name"] == "Med")
    face = face_price(face_min, face_max, cfg)
    result = {
        "demand_score": round(score, 4),
        "tier": tier["name"],
        "missing_signals": missing,
        "face": face,
    }

    listings = (snapshot or {}).get("listing_count")
    median = (snapshot or {}).get("median")
    if face and median and listings is not None and listings >= cfg["live_min_listings"]:
        resale = sale_price(median, cfg)
        profit = profit_pct(resale, face, cfg, fee_included)
        result.update({
            "mode": "live",
            "confidence": "High",
            "resale": round(resale, 2),
            "multiple_low": round(resale / face, 4),
            "multiple_high": round(resale / face, 4),
            "profit": round(profit, 4),
            "profit_low": round(profit, 4),
            "profit_high": round(profit, 4),
            "edge_sort": round(profit, 4),
        })
        return result

    # Mode B. Profit as a multiple of face does not depend on the face value
    # itself when fees scale with face, so a range exists even without prices.
    base = face or 1.0
    lo = profit_pct(base * tier["multiple_low"], base, cfg, fee_included)
    hi = profit_pct(base * tier["multiple_high"], base, cfg, fee_included)
    result.update({
        "mode": "predicted",
        "confidence": "Med" if has_artist_data(missing) else "Low",
        "multiple_low": tier["multiple_low"],
        "multiple_high": tier["multiple_high"],
        "profit": None,
        "profit_low": round(lo, 4),
        "profit_high": round(hi, 4),
        "edge_sort": round((lo + hi) / 2, 4),
    })
    return result


def clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
