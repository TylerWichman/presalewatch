"""Estimated capacity: the scoring rule, the order capacities are chosen in, and the estimates job."""

import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import edge
import pipeline
from common import load_config
from ingest import venue_estimates
from ingest.db import SqliteDatabase

NOW = "2026-10-02T12:00:00Z"


class EstimatedHigh(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def score(self, listeners, capacity, estimated, check=3000):
        sig = lambda cap: edge.demand_signals(listeners, listeners * 30, cap, 10, 20_000_000, self.cfg)  # noqa: E731
        return edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=sig(capacity), snapshot=None,
                             cfg=self.cfg, capacity_estimated=estimated, check_signals=sig(check) if estimated else None)

    def test_measured_capacity_is_unaffected(self):
        r = self.score(3_000_000, 835, estimated=False)
        self.assertEqual((r["tier"], r["high_estimated"]), ("High", False))

    def test_estimate_unlocks_high_only_if_high_at_3000(self):
        big = self.score(3_000_000, 835, estimated=True)      # still High at 3,000 seats
        self.assertEqual((big["tier"], big["high_estimated"]), ("High", True))
        mid = self.score(700_000, 835, estimated=True)        # High only because the room is assumed tiny
        self.assertEqual(self.score(700_000, 835, estimated=False)["tier"], "High")
        self.assertEqual((mid["tier"], mid["high_estimated"]), ("Med", False))

    def test_estimate_without_a_check_never_unlocks_high(self):
        sig = edge.demand_signals(3_000_000, 90_000_000, 835, 10, 20_000_000, self.cfg)
        r = edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=sig, snapshot=None, cfg=self.cfg,
                          capacity_estimated=True, check_signals=None)
        self.assertEqual(r["tier"], "Med")

    def test_estimates_are_off_by_default(self):
        self.assertFalse(load_config()["capacity_estimate"]["enabled"])


class CapacityPrecedence(unittest.TestCase):
    def cfg(self, enabled):
        c = load_config()
        c["capacity_estimate"]["enabled"] = enabled
        return c

    def test_order(self):
        intel = {"capacity": 2800, "capacity_source": "wikipedia", "capacity_estimate": None}
        self.assertEqual(pipeline.venue_capacity({"capacity": "850"}, intel, self.cfg(True)), (850.0, "manual", False))
        self.assertEqual(pipeline.venue_capacity({"capacity": ""}, intel, self.cfg(True)), (2800.0, "wikipedia", False))
        est = {"capacity": None, "capacity_source": None, "capacity_estimate": 835}
        self.assertEqual(pipeline.venue_capacity({}, est, self.cfg(True)), (835.0, "estimate", True))
        self.assertEqual(pipeline.venue_capacity({}, est, self.cfg(False)), (None, None, False))   # off: ignored
        self.assertEqual(pipeline.venue_capacity({}, None, self.cfg(True)), (None, None, False))

    def test_unreachable_database_never_breaks_the_refresh(self):
        with mock.patch.object(pipeline, "env", return_value="d1"), \
                mock.patch("ingest.db.D1Database.from_env", side_effect=SystemExit("no token")):
            self.assertEqual(pipeline.intel_capacities(["V1"]), {})   # a missing token must not stop the refresh
        with mock.patch.object(pipeline, "env", return_value="d1"), \
                mock.patch("ingest.db.D1Database.from_env", side_effect=RuntimeError("network down")):
            self.assertEqual(pipeline.intel_capacities(["V1"]), {})
        with mock.patch.object(pipeline, "env", return_value=""):
            self.assertEqual(pipeline.intel_capacities(["V1"]), {})


class EstimatesJob(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(Path(tempfile.mkdtemp()) / "t.db")
        rows = [("M1", "Club One", 500), ("M2", "Club Two", 800), ("M3", "Hall", 1200), ("M4", "Theatre", 2000),
                ("M5", "Big Arena", 18000),                                      # measured, not small: excluded from p25
                ("U1", "Tiny Room", None), ("U2", "Some Stadium", None), ("U3", "Unchecked Bar", None), ("U4", "Reviewed Club", None),
                ("U5", "Awaiting OSM Bar", None)]
        for tm, name, cap in rows:
            checked = None if tm == "U3" else NOW
            osm = None if tm == "U5" else checked
            self.db.run("INSERT INTO venues (ticketmaster_id, name, name_key, capacity, capacity_checked_at, osm_checked_at, source, last_updated)"
                        " VALUES (?, ?, ?, ?, ?, ?, 't', ?)", (tm, name, name.lower(), cap, checked, osm, NOW))
        vid = self.db.scalar("SELECT id FROM venues WHERE ticketmaster_id = 'U4'")
        self.db.run("INSERT INTO match_review (kind, source, external_id, external_name, status, created_at, last_updated)"
                    " VALUES ('venue_match', 'enrichment', ?, 'Reviewed Club', 'open', ?, ?)", (str(vid), NOW, NOW))

    def run_job(self, enabled):
        cfg = load_config()
        cfg["capacity_estimate"]["enabled"] = enabled
        with mock.patch.object(venue_estimates, "load_config", return_value=cfg):
            venue_estimates.run(self.db, {"api_calls": 0, "rows_written": 0})

    def estimates(self):
        return {r["ticketmaster_id"]: r["capacity_estimate"] for r in self.db.query("SELECT ticketmaster_id, capacity_estimate FROM venues")}

    def test_only_eligible_venues_get_the_25th_percentile(self):
        self.run_job(True)
        est = self.estimates()
        p25 = venue_estimates.p25([500, 800, 1200, 2000])
        self.assertEqual(est["U1"], p25)
        for tm in ("U2", "U3", "U4", "U5", "M1", "M5"):   # large name, not tried, under review, OSM pending, measured
            self.assertIsNone(est[tm], tm)
        self.assertIn("25th percentile of 4 measured venues", self.db.scalar("SELECT capacity_estimate_basis FROM venues WHERE ticketmaster_id = 'U1'"))

    def test_turning_estimates_off_clears_them(self):
        self.run_job(True)
        self.run_job(False)
        self.assertTrue(all(v is None for v in self.estimates().values()))

    def test_measured_capacity_later_clears_the_estimate(self):
        self.run_job(True)
        self.db.run("UPDATE venues SET capacity = 650 WHERE ticketmaster_id = 'U1'")
        self.run_job(True)
        self.assertIsNone(self.estimates()["U1"])


class PageFlag(unittest.TestCase):
    @staticmethod
    def row_template():
        return {"event_id": "E1", "artist": "A", "event_name": "A", "url": None, "image": None, "venue": "V", "city": "C", "state": "NY",
               "event_date": "2026-12-01", "event_time": None, "onsale": None, "face_min": None, "face_max": None, "fee_included": False,
               "dynamic_pricing": False, "mode": "predicted", "confidence": "Med", "tier": "High", "high_estimated": True,
               "demand_score": 0.8, "profit": None, "profit_low": 0.2, "profit_high": 1.0, "edge_sort": 0.6, "multiple_low": 1.8,
               "multiple_high": 3.0, "resale_median": None, "resale_low": None, "resale_avg": None, "resale_listings": None,
               "resale_captured_at": None, "presale_name": "P", "presale_type": "Artist", "code_source": "x", "presale_description": None,
               "presale_url": None, "presale_link_text": None, "presale_start": "2026-10-03T14:00:00Z", "presale_end": "2026-10-04T14:00:00Z",
               "inputs": {"face_used": None, "lastfm_listeners": 1, "lastfm_playcount": 1, "venue_capacity": 835, "capacity_estimated": True,
                          "tour_date_count": 1, "catchment_population": 20000000, "signals": {}, "missing_signals": [], "seatgeek_listings": None}}

    def test_build_passes_the_estimated_flags(self):
        import build
        out = build.compact({"generated_at": NOW, "fees": {}, "live_min_listings": 10, "presales": [self.row_template()]})
        ev = out["events"][0]
        self.assertEqual((ev["he"], ev["in"]["cest"], ev["in"]["pop"]), (True, True, 20000000))

    def test_build_keeps_unrated_rows_without_numbers(self):
        import build
        row = dict(self.row_template(), tier="Unrated", confidence="Low", high_estimated=False, demand_score=None,
                   profit_low=None, profit_high=None, edge_sort=None, multiple_low=None, multiple_high=None)
        ev = build.compact({"generated_at": NOW, "fees": {}, "live_min_listings": 10, "presales": [row]})["events"][0]
        self.assertEqual(ev["tr"], "Unrated")
        for k in ("ds", "pl", "ph", "es", "he"):
            self.assertNotIn(k, ev)


if __name__ == "__main__":
    unittest.main()
