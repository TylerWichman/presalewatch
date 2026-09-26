import math
import random
import unittest
from datetime import datetime, timezone

import calibrate
import edge
import ticketmaster
from common import load_config


class ProfitFormula(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["fees"] = {"seller_fee": 0.15, "primary_fee_pct": 0.25}

    def test_spec_formula(self):
        # $100 face + $25 fees = $125 cost; $200 resale nets $170 -> +36%.
        self.assertAlmostEqual(edge.profit_pct(200, 100, self.cfg), 0.36)

    def test_fees_already_included(self):
        self.assertAlmostEqual(edge.profit_pct(200, 100, self.cfg, fee_included=True), 0.70)

    def test_high_tier_range_matches_spec_example(self):
        signals = {"popularity": 1.0, "followers_capacity": 1.0, "scarcity": 1.0, "market": 1.0}
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

    def test_tier_cutoffs(self):
        self.assertEqual(edge.tier_for(0.70, self.cfg)["name"], "High")
        self.assertEqual(edge.tier_for(0.6999, self.cfg)["name"], "Med")
        self.assertEqual(edge.tier_for(0.45, self.cfg)["name"], "Med")
        self.assertEqual(edge.tier_for(0.44, self.cfg)["name"], "Low")

    def test_missing_signals_lower_confidence(self):
        signals = edge.demand_signals(None, None, None, 4, 1.0, self.cfg)
        score, missing = edge.demand_score(signals, self.cfg)
        self.assertEqual(missing, ["popularity", "followers_capacity"])
        self.assertAlmostEqual(score, 0.35 * 0.5 + 0.30 * 0.5 + 0.20 * 0.25 + 0.15 * 1.0)

    def test_followers_capacity_log_scale(self):
        s = edge.demand_signals(80, 1_000_000, 1000, 1, 0.5, self.cfg)
        self.assertAlmostEqual(s["followers_capacity"], math.log10(1000) / 4)
        self.assertAlmostEqual(s["popularity"], 0.8)
        self.assertEqual(s["scarcity"], 1.0)


class PresaleTypes(unittest.TestCase):
    def test_classification(self):
        cases = {
            "Artist Presale": "Artist", "LIVE NATION PRESALE": "Live Nation", "Citi� Cardmember Presale": "Citi",
            "Reserved by Spotify": "Spotify", "Amex Presale Tickets": "Amex", "Artist Presale VIP Package": "VIP",
            "Venue Presale": "Venue", "Platinum Onsale": "Platinum", "K-LOVE Presale": "Radio / Local",
        }
        for name, kind in cases.items():
            self.assertEqual(ticketmaster.presale_type(name)[0], kind, name)


class Refit(unittest.TestCase):
    def test_recovers_signal_and_covers_range(self):
        cfg = load_config()
        rng = random.Random(1)
        rows = []
        for _ in range(200):
            pop, fc, sc, mk = rng.random(), rng.random(), rng.random(), rng.choice([0.5, 1.0])
            mult = math.exp(-0.5 + 1.5 * pop + 0.5 * fc + rng.gauss(0, 0.1))
            rows.append({"popularity": pop, "followers_capacity": fc, "scarcity": sc, "market": mk, "multiple_d7": mult})
        fit = calibrate.refit(rows, cfg)
        self.assertGreater(fit["weights"]["popularity"], fit["weights"]["followers_capacity"])
        self.assertLess(fit["weights"]["scarcity"], 0.1)
        names = [t["name"] for t in fit["tiers"]]
        self.assertEqual(names, ["High", "Med", "Low"])


if __name__ == "__main__":
    unittest.main()
