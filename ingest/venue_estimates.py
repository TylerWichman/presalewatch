"""Estimated capacity for venues no source could resolve (runs daily, after venue enrichment).

The estimate is the 25th percentile of measured small venues (capacity <= 3,000). The 25th
rather than the median because measured small venues skew large: a club with a Wikipedia page
is usually a notable, bigger one.

A venue gets an estimate only if all of these hold:
  * it has no measured capacity, and every source has been tried: Wikidata and Wikipedia
    (capacity_checked_at) and OpenStreetMap (osm_checked_at). Venues deferred, skipped for a
    source error, or still waiting for the capped OpenStreetMap step are left for later runs;
  * nothing about it is open in match_review;
  * its name doesn't suggest a large venue (stadium, arena, field, park, amphitheater, center,
    casino, ...) and its known type isn't stadium, arena, amphitheater, or festival. Unprocessed
    or renamed stadiums would otherwise get a small-room estimate and look like sellouts.

Estimates live in venues.capacity_estimate, never in venues.capacity. Scoring uses one only with
lower confidence: it can make an event High only if the event is still High at 3,000 seats, and
the page labels that High as estimated. With `capacity_estimate.enabled` off, every estimate is
cleared.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone

from common import load_config
from ingest.db import Database, now_iso

LARGE_TYPES = ("stadium", "arena", "amphitheater", "festival")


def p25(values: list[int]) -> int | None:
    if len(values) < 4:
        return None
    return round(statistics.quantiles(sorted(values), n=4, method="inclusive")[0])


def eligible(venue: dict, large_name: re.Pattern, hand_fill: frozenset = frozenset()) -> bool:
    """Pure: may this venue get an estimate? Not if it's waiting on a hand-entered capacity."""
    return (venue["capacity"] is None and venue.get("ticketmaster_id") not in hand_fill and venue["capacity_checked_at"] is not None and venue["osm_checked_at"] is not None
            and not venue["open_review"]
            and not large_name.search(venue["name"] or "") and (venue["venue_type"] or "") not in LARGE_TYPES)


def clear_all(db: Database) -> None:
    db.run("UPDATE venues SET capacity_estimate = NULL, capacity_estimate_basis = NULL, capacity_estimate_at = NULL"
           " WHERE capacity_estimate IS NOT NULL")


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    cfg = load_config().get("capacity_estimate") or {}
    stamp = now_iso(now)
    if not cfg.get("enabled"):
        clear_all(db)
        print("  Estimates: off (config capacity_estimate.enabled); any existing estimates cleared")
        return
    small_max = cfg.get("small_max", 3000)
    measured = [r["capacity"] for r in db.query("SELECT capacity FROM venues WHERE capacity IS NOT NULL AND capacity <= ?", (small_max,))]
    # A percentile from a thin sample swings a lot (51 venues gave 600 seats, 125 gave 850), and a
    # too-small estimate inflates listeners per seat. Below the minimum, estimates are off this run.
    min_measured = cfg.get("min_measured", 100)
    value = p25(measured) if len(measured) >= min_measured else None
    if value is None:
        clear_all(db)
        print(f"  Estimates: off this run; only {len(measured)} measured venues <= {small_max:,}"
              f" (need {min_measured}); any existing estimates cleared")
        return
    basis = f"25th percentile of {len(measured)} measured venues <= {small_max:,}"
    large_name = re.compile(cfg.get("large_name_pattern", "stadium|arena"), re.I)
    venues = db.query(
        "SELECT v.id, v.ticketmaster_id, v.name, v.capacity, v.capacity_checked_at, v.osm_checked_at, v.venue_type, v.capacity_estimate,"
        " EXISTS (SELECT 1 FROM match_review r WHERE r.external_id = CAST(v.id AS TEXT) AND r.status = 'open'"
        "   AND r.kind IN ('venue_match', 'venue_capacity')) AS open_review FROM venues v")
    hand_fill = frozenset(cfg.get("hand_fill_venues") or ())
    stmts, given, cleared = [], 0, 0
    for v in venues:
        if eligible(v, large_name, hand_fill):
            given += 1
            if v["capacity_estimate"] != value:
                stmts.append(("UPDATE venues SET capacity_estimate = ?, capacity_estimate_basis = ?, capacity_estimate_at = ? WHERE id = ?",
                              (value, basis, stamp, v["id"])))
        elif v["capacity_estimate"] is not None:
            cleared += 1
            stmts.append(("UPDATE venues SET capacity_estimate = NULL, capacity_estimate_basis = NULL, capacity_estimate_at = NULL WHERE id = ?",
                          (v["id"],)))
    db.batch(stmts)
    stats["rows_written"] += len(stmts)
    print(f"  Estimates: {value:,} seats ({basis}) for {given} venues; {cleared} cleared")
