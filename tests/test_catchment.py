"""Catchment population: parsing the Census tract file, the radius sum, and the daily job."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import load_config
from ingest import catchment
from ingest.db import SqliteDatabase

NOW = "2026-10-02T12:00:00Z"
# Midtown Manhattan, a tract 40 km away (Long Island), one 120 km away (Philadelphia), and a bad row.
TRACTS = ("﻿STATEFP,COUNTYFP,TRACTCE,POPULATION,LATITUDE,LONGITUDE\n"
          "36,061,010200,5000,+40.758000,-73.985000\n"
          "36,059,300000,3000,+40.720000,-73.510000\n"
          "42,101,000100,4000,+39.952000,-75.165000\n"
          "36,061,999999,x,+40.7,-73.9\n")


class Tracts(unittest.TestCase):
    def test_load_skips_bad_rows(self):
        self.assertEqual(len(catchment.load_tracts(TRACTS.lstrip("﻿"))), 3)

    def test_population_within_radius(self):
        index = catchment.TractIndex(catchment.load_tracts(TRACTS))
        self.assertEqual(index.population_within(40.7505, -73.9934, 80), 8000)    # MSG: not Philadelphia
        self.assertEqual(index.population_within(40.7505, -73.9934, 150), 12000)
        self.assertEqual(index.population_within(40.7505, -73.9934, 5), 5000)
        self.assertEqual(index.population_within(34.05, -118.25, 80), 0)


class Job(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(Path(tempfile.mkdtemp()) / "t.db")
        for tm, lat, lon in (("MSG", 40.7505, -73.9934), ("LA", 34.05, -118.25), ("NOGEO", None, None)):
            self.db.run("INSERT INTO venues (ticketmaster_id, name, name_key, latitude, longitude, source, last_updated)"
                        " VALUES (?, ?, ?, ?, ?, 't', ?)", (tm, tm, tm.lower(), lat, lon, NOW))

    def run_job(self, radius=80):
        cfg = load_config()
        cfg["market_population"]["radius_km"] = radius
        stats = {"api_calls": 0, "rows_written": 0}
        with mock.patch.object(catchment, "load_config", return_value=cfg), \
                mock.patch.object(catchment, "fetch_tracts", return_value=TRACTS) as fetch:
            catchment.run(self.db, stats)
        return fetch.call_count, stats

    def values(self):
        return {r["ticketmaster_id"]: (r["catchment_population"], r["catchment_basis"])
                for r in self.db.query("SELECT ticketmaster_id, catchment_population, catchment_basis FROM venues")}

    def test_fills_venues_with_coordinates_once(self):
        calls, stats = self.run_job()
        v = self.values()
        self.assertEqual(v["MSG"], (8000, "2020 Census tract centers of population, within 80 km"))
        self.assertEqual(v["LA"][0], 0)
        self.assertEqual(v["NOGEO"], (None, None))
        self.assertEqual((calls, stats["rows_written"]), (1, 2))
        # Up to date: the file isn't downloaded again.
        self.assertEqual(self.run_job()[0], 0)

    def test_new_radius_recomputes(self):
        self.run_job()
        self.assertEqual(self.run_job(radius=150)[0], 1)
        self.assertEqual(self.values()["MSG"], (12000, "2020 Census tract centers of population, within 150 km"))


if __name__ == "__main__":
    unittest.main()
