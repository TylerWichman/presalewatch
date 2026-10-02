import csv
import math
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import calibrate
import common
import edge
import ticketmaster
from common import load_config


class ProfitFormula(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["fees"] = {"seller_fee": 0.15, "primary_fee_pct": 0.25}
        self.cfg["ask_to_sale_discount"] = 0.0

    def test_spec_formula(self):
        # $100 face + $25 fees = $125 cost; $200 resale nets $170 -> +36%.
        self.assertAlmostEqual(edge.profit_pct(200, 100, self.cfg), 0.36)

    def test_fees_already_included(self):
        self.assertAlmostEqual(edge.profit_pct(200, 100, self.cfg, fee_included=True), 0.70)

    def test_high_tier_range_matches_spec_example(self):
        signals = dict.fromkeys(edge.SIGNALS, 1.0)
        res = edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=signals, snapshot=None, cfg=self.cfg)
        self.assertEqual((res["mode"], res["tier"], res["confidence"]), ("predicted", "High", "Med"))
        self.assertAlmostEqual(res["profit_low"], 1.8 * 0.85 / 1.25 - 1, places=4)   # about +22%
        self.assertAlmostEqual(res["profit_high"], 3.0 * 0.85 / 1.25 - 1, places=4)  # about +104%
        self.assertAlmostEqual(res["edge_sort"], (res["profit_low"] + res["profit_high"]) / 2, places=4)

    def test_live_needs_ten_listings_and_face(self):
        signals = dict.fromkeys(edge.SIGNALS, 0.5)
        snap = {"listing_count": 9, "median": 300.0}
        res = edge.evaluate(face_min=100, face_max=100, fee_included=False, signals=signals, snapshot=snap, cfg=self.cfg)
        self.assertEqual(res["mode"], "predicted")
        snap["listing_count"] = 10
        res = edge.evaluate(face_min=100, face_max=100, fee_included=False, signals=signals, snapshot=snap, cfg=self.cfg)
        self.assertEqual((res["mode"], res["confidence"]), ("live", "High"))
        self.assertAlmostEqual(res["profit"], (300 * 0.85 - 125) / 125, places=4)
        res = edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=signals, snapshot=snap, cfg=self.cfg)
        self.assertEqual(res["mode"], "predicted")

    def test_live_discounts_asking_price(self):
        self.cfg["ask_to_sale_discount"] = 0.15
        signals = dict.fromkeys(edge.SIGNALS, 0.5)
        snap = {"listing_count": 10, "median": 300.0}
        res = edge.evaluate(face_min=100, face_max=100, fee_included=False, signals=signals, snapshot=snap, cfg=self.cfg)
        # $300 ask sells for about $255; $255 * 0.85 = $216.75 net on $125 cost -> +73.4%.
        self.assertAlmostEqual(res["resale"], 255.0)
        self.assertAlmostEqual(res["multiple_low"], 2.55)
        self.assertAlmostEqual(res["profit"], (300 * 0.85 * 0.85 - 125) / 125, places=4)

    def test_default_discount_is_configured(self):
        self.assertEqual(load_config()["ask_to_sale_discount"], 0.15)

    def test_tier_cutoffs(self):
        self.assertEqual(edge.tier_for(0.70, self.cfg)["name"], "High")
        self.assertEqual(edge.tier_for(0.6999, self.cfg)["name"], "Med")
        self.assertEqual(edge.tier_for(0.45, self.cfg)["name"], "Med")
        self.assertEqual(edge.tier_for(0.44, self.cfg)["name"], "Low")

    def test_missing_signals_are_left_out_and_weights_rescaled(self):
        # No capacity: listeners, engagement, scarcity (1/4) and market (1.0) count.
        signals = edge.demand_signals(10**9, 10**11, None, 4, 20_000_000, self.cfg)
        score, missing = edge.demand_score(signals, self.cfg)
        self.assertEqual(missing, ["listeners_capacity"])
        w = self.cfg["weights"]
        kept = w["listeners"] + w["engagement"] + w["scarcity"] + w["market"]
        self.assertAlmostEqual(score, (w["listeners"] + w["engagement"] + w["scarcity"] * 0.25 + w["market"]) / kept)

    def test_missing_signal_does_not_drag_score_down(self):
        full = dict.fromkeys(edge.SIGNALS, 0.9)
        partial = {**full, "listeners_capacity": None}
        self.assertAlmostEqual(edge.demand_score(full, self.cfg)[0], 0.9)
        self.assertAlmostEqual(edge.demand_score(partial, self.cfg)[0], 0.9)

    def test_no_lastfm_data_means_unrated(self):
        # A one-night show in the biggest market would score 1.0 on what's left; it isn't rated at all.
        signals = edge.demand_signals(None, None, 2000, 1, 20_000_000, self.cfg)
        res = edge.evaluate(face_min=50, face_max=50, fee_included=False, signals=signals, snapshot=None, cfg=self.cfg)
        self.assertEqual((res["tier"], res["mode"], res["confidence"]), (edge.UNRATED, "predicted", "Low"))
        for k in ("demand_score", "profit_low", "profit_high", "edge_sort", "multiple_low"):
            self.assertIsNone(res[k], k)

    def test_unrated_still_goes_live_with_enough_listings(self):
        signals = edge.demand_signals(None, None, None, 1, 20_000_000, self.cfg)
        snap = {"listing_count": 40, "median": 200}
        res = edge.evaluate(face_min=100, face_max=100, fee_included=False, signals=signals, snapshot=snap, cfg=self.cfg)
        self.assertEqual(res["mode"], "live")
        self.assertIsNotNone(res["edge_sort"])

    def test_market_from_catchment_population(self):
        self.assertEqual(edge.market_value(20_000_000, self.cfg), 1.0)
        self.assertEqual(edge.market_value(250_000, self.cfg), 0.0)
        self.assertEqual(edge.market_value(50_000, self.cfg), 0.0)
        self.assertAlmostEqual(edge.market_value(math.sqrt(250_000 * 20_000_000), self.cfg), 0.5)
        self.assertIsNone(edge.market_value(None, self.cfg))
        self.assertIsNone(edge.demand_signals(10**6, None, None, None, None, self.cfg)["market"])
        # Bigger catchments score higher, never past 1.
        self.assertLess(edge.market_value(5_000_000, self.cfg), edge.market_value(10_000_000, self.cfg))
        self.assertEqual(edge.market_value(40_000_000, self.cfg), 1.0)

    def test_high_is_reachable_when_demand_outruns_seats(self):
        # 3M listeners (40 plays each) for a 2,000-seat room on a 20-date tour in a top market.
        signals = edge.demand_signals(3_000_000, 120_000_000, 2000, 20, 20_000_000, self.cfg)
        res = edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=signals, snapshot=None, cfg=self.cfg)
        self.assertEqual(res["missing_signals"], [])
        self.assertEqual((res["tier"], res["confidence"]), ("High", "Med"))

    def test_high_needs_listeners_per_seat(self):
        # Same act, venue capacity unknown: Last.fm data gives Med confidence, but the tier stops at Med.
        signals = edge.demand_signals(3_000_000, 120_000_000, None, 20, 20_000_000, self.cfg)
        res = edge.evaluate(face_min=None, face_max=None, fee_included=False, signals=signals, snapshot=None, cfg=self.cfg)
        self.assertGreaterEqual(res["demand_score"], 0.70)
        self.assertEqual((res["tier"], res["confidence"]), ("Med", "Med"))

    def test_listeners_per_seat_dominates(self):
        # Mid-size artist in a small room beats a bigger artist in an arena.
        small_room = edge.demand_score(edge.demand_signals(1_000_000, 30_000_000, 600, 10, 20_000_000, self.cfg), self.cfg)[0]
        arena = edge.demand_score(edge.demand_signals(4_000_000, 120_000_000, 40_000, 10, 20_000_000, self.cfg), self.cfg)[0]
        self.assertGreater(small_room, arena)

    def test_signal_scaling(self):
        sc = self.cfg["scaling"]
        s = edge.demand_signals(1_000_000, 30_000_000, 2000, 1, 2_000_000, self.cfg)
        lo, hi = sc["listeners_log10"]
        self.assertAlmostEqual(s["listeners"], (6 - lo) / (hi - lo))
        lo, hi = sc["plays_per_listener_log10"]
        self.assertAlmostEqual(s["engagement"], (math.log10(30) - lo) / (hi - lo))
        lo, hi = sc["listeners_per_seat_log10"]
        self.assertAlmostEqual(s["listeners_capacity"], (math.log10(500) - lo) / (hi - lo))
        self.assertEqual(s["scarcity"], 1.0)
        self.assertEqual(edge.demand_signals(50, 100, 1000, None, None, self.cfg)["listeners"], 0.0)
        self.assertEqual(edge.demand_signals(10**9, None, None, None, None, self.cfg)["listeners"], 1.0)


class PresaleTypes(unittest.TestCase):
    def test_classification(self):
        cases = {
            "Artist Presale": "Artist", "LIVE NATION PRESALE": "Live Nation", "Citi� Cardmember Presale": "Citi",
            "Reserved by Spotify": "Spotify", "Amex Presale Tickets": "Amex", "Artist Presale VIP Package": "VIP",
            "Venue Presale": "Venue", "Platinum Onsale": "Platinum", "K-LOVE Presale": "Radio / Local",
        }
        for name, kind in cases.items():
            self.assertEqual(ticketmaster.presale_type(name)[0], kind, name)


class Scoring(unittest.TestCase):
    def test_actual_multiple_uses_sale_basis(self):
        cfg = load_config()
        cfg["ask_to_sale_discount"] = 0.15
        tables = {
            "events": [{"event_id": "e1", "artist": "A", "onsale_date": "2026-09-01T14:00:00Z", "face_min": "100", "face_max": "100"}],
            "predictions": [{"event_id": "e1", "mode": "predicted", "predicted_at": "2026-08-30T00:00:00Z", "tier": "Med",
                             "demand_score": "0.5", "multiple_low": "1.2", "multiple_high": "1.8", "face": "100"}],
            "resale_snapshots": [{"event_id": "e1", "captured_at": "2026-09-08T14:00:00Z", "median": "200", "listing_count": "40"}],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, "DB_DIR", Path(tmp)):
            for name, rows in tables.items():
                with open(Path(tmp) / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            rows = calibrate.score(datetime(2026, 9, 9, tzinfo=timezone.utc), cfg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["median_d7"], 200)
        self.assertAlmostEqual(rows[0]["multiple_d7"], 1.7)  # $200 ask * 0.85 / $100 face


class Refit(unittest.TestCase):
    def test_recovers_signal_and_covers_range(self):
        cfg = load_config()
        rng = random.Random(1)
        rows = []
        for _ in range(200):
            lis, eng, lc, sc, mk = rng.random(), rng.random(), rng.random(), rng.random(), rng.choice([0.5, 1.0])
            mult = math.exp(-0.5 + 1.5 * lis + 0.5 * lc + rng.gauss(0, 0.1))
            rows.append({"listeners": lis, "engagement": eng, "listeners_capacity": lc, "scarcity": sc, "market": mk, "multiple_d7": mult})
        fit = calibrate.refit(rows, cfg)
        self.assertGreater(fit["weights"]["listeners"], fit["weights"]["listeners_capacity"])
        self.assertLess(fit["weights"]["scarcity"], 0.1)
        names = [t["name"] for t in fit["tiers"]]
        self.assertEqual(names, ["High", "Med", "Low"])


if __name__ == "__main__":
    unittest.main()
