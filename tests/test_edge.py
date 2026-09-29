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
