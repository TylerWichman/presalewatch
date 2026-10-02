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
UNRATED = "Unrated"  # no Last.fm data: no demand tier, no estimated range
# Without Last.fm data confidence is Low. An event is capped at Med unless every signal
# in config high_requires is known (Last.fm listeners and listeners per venue seat).


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


def market_value(catchment: float | None, cfg: dict) -> float | None:
    """Market size from the population within radius_km of the venue, log-scaled: 250K -> 0,
    20M -> 1 (config market_population). None when unknown."""
    if not catchment:
        return None
    m = cfg["market_population"]
    return log_scale(catchment, math.log10(m["low"]), math.log10(m["high"]))


def log_scale(value: float, lo: float, hi: float) -> float:
    """log10(value) mapped from [lo, hi] onto [0, 1]."""
    return clip((math.log10(max(value, 1.0)) - lo) / (hi - lo))


def demand_signals(listeners: float | None, playcount: float | None, capacity: float | None,
                   tour_dates: float | None, catchment: float | None, cfg: dict) -> dict:
    """Scale raw inputs to 0-1 signals. Missing inputs come back as None.

    listeners: Last.fm listeners, log-scaled (reach).
    engagement: Last.fm plays per listener, log-scaled (how hard fans listen).
    listeners_capacity: listeners per venue seat, log-scaled (demand vs supply).
    catchment: people living within 80 km of the venue; becomes the market signal.
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
    market = market_value(catchment, cfg)
    if market is not None:
        out["market"] = market
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


def tier_for(score: float, cfg: dict, cutoffs: dict | None = None) -> dict:
    """The tier for a demand score. cutoffs ({"High": x, "Med": y}, from percentile_cutoffs)
    replace the fixed min_score values in config when given."""
    tiers = sorted(cfg["tiers"], key=lambda t: t["min_score"], reverse=True)
    for tier in tiers:
        if score >= (cutoffs or {}).get(tier["name"], tier["min_score"]):
            return tier
    return tiers[-1]


def pool_score(signals: dict, check_signals: dict | None, capacity_estimated: bool, cfg: dict) -> float | None:
    """An event's score for percentile ranking, or None when it isn't ranked.

    Ranked: events with Last.fm data and a venue capacity (the signals High requires). An
    estimated capacity ranks at the 3,000-seat check, so a guessed small room can't push it up.
    """
    sig = check_signals if capacity_estimated else signals
    if sig is None or any(sig.get(k) is None for k in cfg.get("high_requires", ["listeners"])):
        return None
    return demand_score(sig, cfg)[0]


def percentile_cutoffs(scores: list[float], cfg: dict) -> dict | None:
    """Score cutoffs that put the top high_share of ranked events in High and the next med_share
    in Med (config tiering). None means use the fixed cutoffs: method isn't percentile, or too few
    events to rank."""
    t = cfg.get("tiering") or {}
    if t.get("method") != "percentile" or len(scores) < t.get("min_pool", 50):
        return None
    ranked = sorted(scores, reverse=True)
    at = lambda share: ranked[max(math.ceil(len(ranked) * share), 1) - 1]  # noqa: E731
    return {"High": at(t["high_share"]), "Med": at(t["high_share"] + t["med_share"])}


def evaluate(*, face_min: float | None, face_max: float | None, fee_included: bool,
             signals: dict, snapshot: dict | None, cfg: dict,
             capacity_estimated: bool = False, check_signals: dict | None = None,
             cutoffs: dict | None = None) -> dict:
    """Pick Mode A or B and compute the edge for one event.

    capacity_estimated: the venue capacity behind `signals` is an estimate, not a measurement.
    check_signals: the same signals recomputed at the top of the small-venue range (3,000
    seats). An estimate can only make an event High if it's still High at that capacity.
    cutoffs: percentile tier cutoffs for this run (percentile_cutoffs); None uses the fixed ones.
    """
    listings = (snapshot or {}).get("listing_count")
    median = (snapshot or {}).get("median")
    face = face_price(face_min, face_max, cfg)
    is_live = bool(face and median and listings is not None and listings >= cfg["live_min_listings"])
    if not is_live and signals.get("listeners") is None:
        # No Last.fm data: the demand score would rest on market and tour size alone, which
        # can't show that people want the tickets. Don't rate the event at all.
        return {"mode": "predicted", "tier": UNRATED, "confidence": "Low", "demand_score": None,
                "missing_signals": [k for k in SIGNALS if signals.get(k) is None], "face": face,
                "capacity_estimated": capacity_estimated, "high_estimated": False,
                "multiple_low": None, "multiple_high": None, "profit": None, "profit_low": None,
                "profit_high": None, "edge_sort": None}
    score, missing = demand_score(signals, cfg)
    tier = tier_for(score, cfg, cutoffs)
    med = next(t for t in cfg["tiers"] if t["name"] == "Med")
    # High needs the signals that show demand outrunning supply (config: high_requires).
    # Without them the rescaled weights fall on market and raw popularity, which overrate
    # big-market shows and big artists in big rooms.
    if tier["name"] == "High" and any(k in missing for k in cfg.get("high_requires", ["listeners"])):
        tier = med
    if tier["name"] == "High" and capacity_estimated:
        check_score, _ = demand_score(check_signals or {}, cfg)
        if check_signals is None or tier_for(check_score, cfg, cutoffs)["name"] != "High":
            tier = med
    result = {
        "demand_score": round(score, 4),
        "tier": tier["name"],
        "missing_signals": missing,
        "face": face,
        "capacity_estimated": capacity_estimated,
        # Shown on the page as "estimated": this High depends on an estimated venue capacity.
        "high_estimated": tier["name"] == "High" and capacity_estimated,
    }

    if is_live:
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
