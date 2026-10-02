"""PresaleWatch data pipeline: pull sources, compute edge, write tables and presales.json.

1. Pull upcoming events and presales (Ticketmaster).
2. Enrich with artist (Last.fm listeners and plays, tour size) and venue (capacity, market) data.
3. Pull resale snapshots (SeatGeek) where the event exists.
4. Pick Mode A or B, compute Profit % or range, and assign a confidence label.
5. Append to predictions; write data/presales.json.

Tables live as CSVs in db/, which the scheduled job keeps on the `data`
branch rather than main. Venue capacities are entered by hand in
config/venues.csv. Last.fm and SeatGeek are optional: without their
keys the pipeline still runs, and every row falls back to a Low-confidence
predicted range.

Usage:
    python pipeline.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import edge
import ticketmaster
from common import (PRESALES_PATH, VENUES_MANUAL_PATH, ApiError, append_table, assert_no_secrets, env, fmt_cell,
                    hours_since, integer, iso, load_config, num, read_csv, read_table, write_table)
from seatgeek import SeatGeek
from lastfm import LastFm, artist_stats

EVENT_FIELDS = [
    "event_id", "artist", "name", "artist_id", "venue", "venue_id", "city", "state", "market_ids",
    "event_date", "event_time", "presale_start", "presale_type", "onsale_date", "face_min", "face_max",
    "fee_included", "dynamic_pricing_flag", "url", "seatgeek_id", "seatgeek_checked_at", "updated_at",
]
ARTIST_FIELDS = [
    "artist_id", "name", "lastfm_lookup", "lastfm_name", "lastfm_listeners", "lastfm_playcount",
    "tour_date_count", "lastfm_checked_at", "updated_at",
]
VENUE_FIELDS = ["venue_id", "name", "city", "state", "capacity"]
SNAPSHOT_FIELDS = ["event_id", "seatgeek_id", "captured_at", "listing_count", "low", "median", "avg"]
PREDICTION_FIELDS = [
    "event_id", "predicted_at", "mode", "demand_score", "tier", "multiple_low", "multiple_high",
    "profit_low", "profit_high", "confidence", "face", "resale_median", "listing_count",
    "listeners", "engagement", "listeners_capacity", "scarcity", "market",
]
# Backfill face values for events whose presales ended up to this many days ago.
BACKFILL_DAYS = 21
# A prediction is re-logged only when one of these changes.
PREDICTION_KEY = ["mode", "tier", "demand_score", "profit_low", "profit_high", "face"]


def by_key(rows: list[dict], key: str) -> dict[str, dict]:
    return {r[key]: r for r in rows if r.get(key)}


def warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


# ---- Step 2: enrichment ----------------------------------------------------------

def upsert_artists(parsed: list[dict], now: datetime, cfg: dict) -> dict[str, dict]:
    artists = by_key(read_table("artists"), "artist_id")
    for p in parsed:
        a = p["artist"]
        row = artists.setdefault(a["artist_id"], {"artist_id": a["artist_id"]})
        row["name"] = a["name"]
        lookup = a["lastfm_lookup"] or a["name"]
        if row.get("lastfm_lookup") and row["lastfm_lookup"] != lookup:
            row["lastfm_checked_at"] = ""  # Ticketmaster now links a different Last.fm artist
        row["lastfm_lookup"] = lookup
        if a["tour_date_count"]:
            row["tour_date_count"] = a["tour_date_count"]
        row["updated_at"] = iso(now)

    lf = LastFm.from_env()
    if not lf:
        print("  Last.fm: no LASTFM_API_KEY, skipping listener counts (estimates stay Low confidence)")
        return artists
    current = {p["artist"]["artist_id"] for p in parsed}
    ttl, budget = cfg["refresh"]["lastfm_ttl_hours"], cfg["refresh"]["lastfm_max_calls"]
    stale = [artists[k] for k in current if hours_since(artists[k].get("lastfm_checked_at"), now) >= ttl]
    stale.sort(key=lambda r: r.get("lastfm_checked_at") or "")  # never-checked first
    done = found = 0
    for row in stale:
        if lf.http.calls >= budget:
            break
        try:
            lookup = row.get("lastfm_lookup") or row["name"]
            # A lookup name that differs from the Ticketmaster name came from Ticketmaster's Last.fm link.
            row.update(artist_stats(lf, lookup, trusted=lookup != row["name"]))
        except ApiError as err:
            warn(f"Last.fm lookup failed for {row['name']}: {err}")
            if err.status in (401, 403, 429, 0):
                break
            continue
        # Not-found answers are cached too, so unknown artists aren't retried every run.
        row["lastfm_checked_at"] = iso(now)
        done += 1
        found += bool(row.get("lastfm_listeners"))
    print(f"  Last.fm: refreshed {done}/{len(stale)} stale artists, {found} found ({lf.http.calls} calls)")
    return artists


def upsert_venues(parsed: list[dict], cfg: dict) -> dict[str, dict]:
    """Every venue seen so far, with capacities from the manual table.

    db/venues.csv lists venues with a blank capacity so someone can look them up;
    capacities are entered in config/venues.csv, which always wins.
    """
    venues = by_key(read_table("venues"), "venue_id")
    for p in parsed:
        v = p["venue"]
        if not v["venue_id"]:
            continue
        row = venues.setdefault(v["venue_id"], {"venue_id": v["venue_id"], "capacity": ""})
        for k in ("name", "city", "state"):
            row[k] = row.get(k) or v[k]
    for vid, manual in by_key(read_csv(VENUES_MANUAL_PATH), "venue_id").items():
        row = venues.setdefault(vid, {})
        row.update({k: val for k, val in manual.items() if val})
    return venues


def intel_capacities(venue_ids: list[str]) -> dict[str, dict]:
    """Measured and estimated capacities from the intelligence database (D1), by Ticketmaster
    venue ID. Set INTEL_DB to 'd1' (production) or 'sqlite:<path>'; unset means none, and the
    page scores exactly as before. Any failure here is a warning, never a failed refresh."""
    target = env("INTEL_DB")
    if not target or not venue_ids:
        return {}
    try:
        from ingest.db import open_db, rows_in
        rows = rows_in(open_db(target), "venues", "ticketmaster_id, capacity, capacity_source, capacity_estimate, catchment_population",
                       "ticketmaster_id", venue_ids)
    except (Exception, SystemExit) as err:  # noqa: BLE001 - the page must still build without it (incl. a missing token)
        warn(f"intelligence database unavailable, scoring without its capacities: {type(err).__name__}: {str(err)[:150]}")
        return {}
    return {r["ticketmaster_id"]: r for r in rows}


def venue_capacity(venue: dict, intel: dict | None, cfg: dict) -> tuple[float | None, str | None, bool]:
    """(capacity, source, estimated) for scoring: hand-entered first, then a measured capacity
    from the intelligence database, then its estimate (lower confidence) if estimates are on."""
    manual = num(venue.get("capacity"))
    if manual:
        return manual, "manual", False
    if intel and intel.get("capacity"):
        return float(intel["capacity"]), intel.get("capacity_source"), False
    if intel and intel.get("capacity_estimate") and (cfg.get("capacity_estimate") or {}).get("enabled"):
        return float(intel["capacity_estimate"]), "estimate", True
    return None, None, False


def backfill_prices(events: dict[str, dict], raw: dict[str, dict], current: set[str], now: datetime) -> None:
    """Ticketmaster usually publishes price ranges only around on-sale, after most presales.
    Keep face values current for recent events that left the presale list, since
    Mode A and calibration both need them."""
    today = now.strftime("%Y-%m-%d")
    cutoff = iso(now - timedelta(days=BACKFILL_DAYS))
    stale = [eid for eid, e in events.items()
             if eid not in current and not e.get("face_min") and (e.get("event_date") or "") >= today
             and (e.get("onsale_date") or e.get("presale_start") or "") >= cutoff]
    missing = [eid for eid in stale if eid not in raw]
    fetched = dict(raw)
    if missing:
        try:
            fetched.update(ticketmaster.fetch_by_ids(missing))
        except ApiError as err:
            warn(f"Ticketmaster face backfill failed: {err}")
    filled = 0
    for eid in stale:
        if eid in fetched:
            fields = ticketmaster.price_fields(fetched[eid])
            events[eid].update({k: v for k, v in fields.items() if v is not None})
            filled += fields["face_min"] is not None
    print(f"  face backfill: {filled}/{len(stale)} past-presale events now have prices")


# ---- Step 3: resale snapshots -----------------------------------------------------

def pull_resale(events: dict[str, dict], parsed: list[dict], now: datetime, cfg: dict) -> None:
    if not cfg["refresh"].get("seatgeek_enabled", True):
        # SeatGeek's free tier returns events without price stats, so matching only burned
        # ~10 minutes of rate-limited searches per run. Re-enable if partner access is granted.
        print("  SeatGeek: paused in config/model.json (free tier has no price stats)")
        return
    sg = SeatGeek.from_env()
    if not sg:
        print("  SeatGeek: no client ID, skipping live resale (every row stays predicted)")
        return
    research, budget = cfg["refresh"]["seatgeek_research_hours"], cfg["refresh"]["seatgeek_max_searches"]
    searches = matched = 0
    for p in parsed:
        e = events[p["event"]["event_id"]]
        if e.get("seatgeek_id") or not e.get("event_date") or searches >= budget:
            continue
        if hours_since(e.get("seatgeek_checked_at"), now) < research:
            continue
        try:
            sid = sg.find(e["artist"], e["event_date"], e.get("city"), e.get("venue"))
        except ApiError as err:
            warn(f"SeatGeek search failed: {err}")
            if err.status in (401, 403, 429, 0):
                break
            continue
        searches += 1
        matched += bool(sid)
        e["seatgeek_id"] = sid or ""
        e["seatgeek_checked_at"] = iso(now)
    print(f"  SeatGeek: {searches} searches, {matched} new matches")

    # Refresh every matched event until its date, including ones whose
    # presales have ended, since calibration scores them after on-sale.
    today = now.strftime("%Y-%m-%d")
    tracked = {e["seatgeek_id"]: eid for eid, e in events.items()
               if e.get("seatgeek_id") and (e.get("event_date") or "") >= today}
    try:
        stats = sg.stats(list(tracked))
    except ApiError as err:
        warn(f"SeatGeek stats failed: {err}")
        return
    rows = [{"event_id": tracked[sid], "seatgeek_id": sid, "captured_at": iso(now), **s}
            for sid, s in stats.items() if s.get("listing_count") is not None]
    append_table("resale_snapshots", rows, SNAPSHOT_FIELDS)
    print(f"  SeatGeek: {len(rows)} snapshots from {len(tracked)} tracked events")


def latest_snapshots(now: datetime, max_age_hours: float) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for r in read_table("resale_snapshots"):
        if hours_since(r["captured_at"], now) > max_age_hours:
            continue
        if r["event_id"] not in latest or r["captured_at"] > latest[r["event_id"]]["captured_at"]:
            latest[r["event_id"]] = r
    return {eid: {"listing_count": integer(r["listing_count"]), "low": num(r["low"]), "median": num(r["median"]),
                  "avg": num(r["avg"]), "captured_at": r["captured_at"]} for eid, r in latest.items()}


# ---- Steps 4-5 -----------------------------------------------------------------------

def log_predictions(results: dict[str, dict], now: datetime) -> int:
    last: dict[str, dict] = {}
    for r in read_table("predictions"):
        last[r["event_id"]] = r
    rows = []
    for eid, res in results.items():
        row = {"event_id": eid, "predicted_at": iso(now), **res, **res["signals"],
               "resale_median": (res["snapshot"] or {}).get("median"),
               "listing_count": (res["snapshot"] or {}).get("listing_count")}
        prev = last.get(eid)
        if prev and all(prev.get(k, "") == fmt_cell(row.get(k)) for k in PREDICTION_KEY):
            continue
        rows.append(row)
    append_table("predictions", rows, PREDICTION_FIELDS)
    return len(rows)


def presale_rows(parsed: list[dict], results: dict[str, dict], artists: dict, venues: dict) -> list[dict]:
    rows = []
    for p in parsed:
        e = p["event"]
        res = results[e["event_id"]]
        artist = artists.get(e["artist_id"], {})
        venue = venues.get(e["venue_id"] or "", {})
        snap = res["snapshot"] or {}
        live = res["mode"] == "live"
        for i, ps in enumerate(p["presales"]):
            rows.append({
                "id": f"{e['event_id']}-{i}",
                "event_id": e["event_id"],
                "artist": e["artist"],
                "artist_id": e["artist_id"],
                "event_name": e["name"],
                "url": e["url"],
                "image": e["image"],
                "venue": e["venue"],
                "city": e["city"],
                "state": e["state"],
                "event_date": e["event_date"],
                "event_time": e["event_time"],
                "presale_name": ps["name"],
                "presale_type": ps["type"],
                "code_source": ps["code_source"],
                "presale_description": ps["description"] or None,
                "presale_url": ps["url"],
                "presale_link_text": ps["link_text"],
                "presale_start": ps["start"],
                "presale_end": ps["end"],
                "onsale": e["onsale_date"],
                "face_min": e["face_min"],
                "face_max": e["face_max"],
                "fee_included": e["fee_included"],
                "dynamic_pricing": e["dynamic_pricing_flag"],
                "mode": res["mode"],
                "confidence": res["confidence"],
                "tier": res["tier"],
                "high_estimated": res["high_estimated"],
                "demand_score": res["demand_score"],
                "profit": res["profit"],
                "profit_low": res["profit_low"],
                "profit_high": res["profit_high"],
                "edge_sort": res["edge_sort"],
                "multiple_low": res["multiple_low"],
                "multiple_high": res["multiple_high"],
                "resale_median": snap.get("median") if live else None,
                "resale_low": snap.get("low") if live else None,
                "resale_avg": snap.get("avg") if live else None,
                "resale_listings": snap.get("listing_count") if live else None,
                "resale_captured_at": snap.get("captured_at") if live else None,
                "inputs": {
                    "face_used": res["face"],
                    "lastfm_listeners": integer(artist.get("lastfm_listeners")),
                    "lastfm_playcount": integer(artist.get("lastfm_playcount")),
                    "venue_capacity": integer(res["capacity"]),
                    "venue_capacity_source": res["capacity_source"],
                    "capacity_estimated": res["capacity_estimated"],
                    "tour_date_count": integer(artist.get("tour_date_count")),
                    "catchment_population": integer(res["catchment"]),
                    "signals": res["signals"],
                    "missing_signals": res["missing_signals"],
                    "seatgeek_listings": snap.get("listing_count"),
                },
            })
    # Edge descending (predicted rows by range midpoint); ties go to the sooner presale.
    rows.sort(key=lambda r: (-r["edge_sort"], r["presale_start"], r["artist"].lower()))
    return rows


def main() -> None:
    cfg = load_config()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    print("1/5 Pulling events and presales from Ticketmaster")
    raw, tm_calls = ticketmaster.fetch_raw(now)
    parsed = [p for p in (ticketmaster.parse_event(ev, now) for ev in raw.values()) if p]
    print(f"  {len(raw)} events scanned, {len(parsed)} with upcoming presales ({tm_calls} calls)")

    print("2/5 Enriching artists and venues")
    events = by_key(read_table("events"), "event_id")
    for p in parsed:
        e = p["event"]
        old = events.get(e["event_id"], {})
        events[e["event_id"]] = {**old, **e, "updated_at": iso(now),
                                 "seatgeek_id": old.get("seatgeek_id", ""),
                                 "seatgeek_checked_at": old.get("seatgeek_checked_at", "")}
    backfill_prices(events, raw, {p["event"]["event_id"] for p in parsed}, now)
    artists = upsert_artists(parsed, now, cfg)
    venues = upsert_venues(parsed, cfg)
    known = sum(1 for v in venues.values() if integer(v.get("capacity")))
    print(f"  {len(artists)} artists, {len(venues)} venues ({known} with capacity)")

    print("3/5 Pulling resale snapshots")
    pull_resale(events, parsed, now, cfg)
    snapshots = latest_snapshots(now, cfg["refresh"]["snapshot_max_age_hours"])

    print("4/5 Computing edge")
    intel = intel_capacities([p["event"]["venue_id"] for p in parsed if p["event"]["venue_id"]])
    check_capacity = (cfg.get("capacity_estimate") or {}).get("high_check_capacity", 3000)
    results: dict[str, dict] = {}
    for p in parsed:
        e = p["event"]
        artist = artists.get(e["artist_id"], {})
        venue = venues.get(e["venue_id"] or "", {})
        catchment = num((intel.get(e["venue_id"] or "") or {}).get("catchment_population"))
        capacity, capacity_source, estimated = venue_capacity(venue, intel.get(e["venue_id"] or ""), cfg)

        def signals_at(cap):
            return edge.demand_signals(
                listeners=num(artist.get("lastfm_listeners")),
                playcount=num(artist.get("lastfm_playcount")),
                capacity=cap,
                tour_dates=num(artist.get("tour_date_count")),
                catchment=catchment,
                cfg=cfg,
            )
        signals = signals_at(capacity)
        snap = snapshots.get(e["event_id"])
        res = edge.evaluate(face_min=e["face_min"], face_max=e["face_max"], fee_included=e["fee_included"],
                            signals=signals, snapshot=snap, cfg=cfg, capacity_estimated=estimated,
                            check_signals=signals_at(check_capacity) if estimated else None)
        res["signals"] = {k: (None if v is None else round(v, 4)) for k, v in signals.items()}
        res["snapshot"] = snap
        res["capacity"], res["capacity_source"] = capacity, capacity_source
        res["catchment"] = catchment
        results[e["event_id"]] = res
    live = sum(1 for r in results.values() if r["mode"] == "live")
    tiers = {name: sum(1 for r in results.values() if r["tier"] == name)
             for name in [*(t["name"] for t in cfg["tiers"]), edge.UNRATED]}
    with_cap = sum(1 for r in results.values() if r["capacity"] and not r["capacity_estimated"])
    est = sum(1 for r in results.values() if r["capacity_estimated"])
    print(f"  {live} live, {len(results) - live} predicted; tiers {tiers}; capacity measured for {with_cap},"
          f" estimated for {est}, High on an estimate {sum(1 for r in results.values() if r['high_estimated'])}")

    print("5/5 Writing tables and presales.json")
    logged = log_predictions(results, now)
    write_table("events", sorted(events.values(), key=lambda r: r["event_id"]), EVENT_FIELDS)
    write_table("artists", sorted(artists.values(), key=lambda r: r["artist_id"]), ARTIST_FIELDS)
    write_table("venues", sorted(venues.values(), key=lambda r: (r.get("state") or "", r.get("city") or "", r.get("name") or "")), VENUE_FIELDS)

    rows = presale_rows(parsed, results, artists, venues)
    text = json.dumps({
        "generated_at": iso(now),
        "fees": cfg["fees"],
        "live_min_listings": cfg["live_min_listings"],
        "ask_to_sale_discount": cfg.get("ask_to_sale_discount", 0.0),
        "calibrated_at": cfg.get("calibrated_at"),
        "event_count": len(parsed),
        "presale_count": len(rows),
        "presales": rows,
    }, indent=1, ensure_ascii=False)
    assert_no_secrets(text, "presales.json")
    PRESALES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRESALES_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"  {logged} predictions logged, {len(rows)} presale rows -> data/presales.json")


if __name__ == "__main__":
    main()
