"""Catchment population: everyone living within 80 km of each venue (runs daily, cheap).

Uses the 2020 Census "centers of population" by census tract: one row per tract (~85,000) with
its population and the population-weighted center. A venue's catchment is the sum over tracts
whose center is within the radius. This replaces the old hand-set top-market list: a casino two
hours from New York no longer counts as New York, and Atlanta no longer counts the same as a
small town.

Terms: Census Bureau data is a US government work, public domain.
The file is static (decennial), so a venue is computed once and recomputed only if the radius
or source changes (catchment_basis records both).
"""

from __future__ import annotations

import csv
import io
import math
import urllib.request
from collections import defaultdict

from common import load_config
from ingest.db import Database, now_iso
from ingest.resolve import haversine_km

TRACTS_URL = "https://www2.census.gov/geo/docs/reference/cenpop2020/tract/CenPop2020_Mean_TR.txt"
SOURCE = "2020 Census tract centers of population"
USER_AGENT = "PouchIt/1.0 (https://pouchit.net)"


def load_tracts(text: str) -> list[tuple[float, float, int]]:
    """(lat, lon, population) per tract from the Census file."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        try:
            out.append((float(r["LATITUDE"]), float(r["LONGITUDE"]), int(r["POPULATION"])))
        except (KeyError, ValueError):
            continue
    return out


class TractIndex:
    """Tracts bucketed by whole degree, so a venue only checks the cells near it."""

    def __init__(self, tracts: list[tuple[float, float, int]]):
        self.cells: dict[tuple[int, int], list[tuple[float, float, int]]] = defaultdict(list)
        for t in tracts:
            self.cells[(math.floor(t[0]), math.floor(t[1]))].append(t)

    def population_within(self, lat: float, lon: float, radius_km: float) -> int:
        dlat = radius_km / 111.0
        dlon = radius_km / max(111.0 * math.cos(math.radians(lat)), 1.0)
        total = 0
        for ci in range(math.floor(lat - dlat), math.floor(lat + dlat) + 1):
            for cj in range(math.floor(lon - dlon), math.floor(lon + dlon) + 1):
                for tlat, tlon, pop in self.cells.get((ci, cj), ()):
                    if abs(tlat - lat) <= dlat and abs(tlon - lon) <= dlon and haversine_km(lat, lon, tlat, tlon) <= radius_km:
                        total += pop
        return total


def fetch_tracts() -> str:
    req = urllib.request.Request(TRACTS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8-sig")


def run(db: Database, stats: dict, now=None) -> None:
    radius = load_config()["market_population"]["radius_km"]
    basis = f"{SOURCE}, within {radius:g} km"
    venues = db.query("SELECT id, latitude, longitude FROM venues WHERE latitude IS NOT NULL"
                      " AND (catchment_population IS NULL OR catchment_basis IS NOT ?)", (basis,))
    if not venues:
        print("  Catchment: every venue is up to date")
        return
    index = TractIndex(load_tracts(fetch_tracts()))
    stats["api_calls"] += 1
    stamp = now_iso(now)
    stmts = [("UPDATE venues SET catchment_population = ?, catchment_basis = ?, catchment_at = ? WHERE id = ?",
              (index.population_within(v["latitude"], v["longitude"], radius), basis, stamp, v["id"])) for v in venues]
    db.batch(stmts)
    stats["rows_written"] += len(stmts)
    print(f"  Catchment: population within {radius:g} km computed for {len(stmts)} venues")
