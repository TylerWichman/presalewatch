"""Schema tests: apply every migration to an in-memory SQLite (the engine D1 runs) and check
constraints and the derived-feature views against hand-computed answers."""

import sqlite3
import unittest
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
T = "2026-10-02T00:00:00Z"


def fresh_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    for f in sorted(MIGRATIONS.glob("*.sql")):
        db.executescript(f.read_text(encoding="utf-8"))
    return db


class Fixture:
    """Small helpers that insert rows with the required bookkeeping columns."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def artist(self, name, listeners=None, playcount=None, **kw):
        cur = self.db.execute(
            "INSERT INTO artists (name, name_key, lastfm_listeners, lastfm_playcount, source, last_updated, ticketmaster_id)"
            " VALUES (?, ?, ?, ?, 'test', ?, ?)",
            (name, name.lower(), listeners, playcount, T, kw.get("tm")))
        return cur.lastrowid

    def venue(self, name, city="Chicago", state="IL", capacity=None, venue_type=None, metro_id=None):
        return self.db.execute(
            "INSERT INTO venues (name, name_key, city, state, capacity, venue_type, metro_id, source, last_updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'test', ?)",
            (name, name.lower(), city, state, capacity, venue_type, metro_id, T)).lastrowid

    def event(self, artist_id, venue_id, date, face_min=None, face_max=None, status="onsale", sold_out=None, tm=None):
        eid = self.db.execute(
            "INSERT INTO events (ticketmaster_id, name, venue_id, event_date, status, sold_out, face_min, face_max,"
            " first_seen_at, last_seen_at, source, last_updated) VALUES (?, 'Show', ?, ?, ?, ?, ?, ?, ?, ?, 'ticketmaster', ?)",
            (tm, venue_id, date, status, sold_out, face_min, face_max, T, T, T)).lastrowid
        self.db.execute("INSERT INTO event_artists (event_id, artist_id, position, source, last_updated) VALUES (?, ?, 0, 'test', ?)",
                        (eid, artist_id, T))
        return eid

    def snapshot(self, artist_id, day, listeners):
        self.db.execute(
            "INSERT INTO artist_metrics_snapshots (artist_id, captured_on, lastfm_listeners, source, last_updated)"
            " VALUES (?, ?, ?, 'lastfm', ?)", (artist_id, day, listeners, T))

    def resale(self, event_id, at, median, basis="ask"):
        self.db.execute(
            "INSERT INTO resale_snapshots (event_id, captured_at, median, price_basis, source, last_updated)"
            " VALUES (?, ?, ?, ?, 'seatgeek', ?)", (event_id, at, median, basis, T))

    def observed(self, event_id, kind, price, basis=None, standard=1, day="2026-10-01", point="single"):
        self.db.execute(
            "INSERT INTO observed_prices (event_id, event_ref, kind, price_basis, price, standard_ticket, price_point,"
            " observed_on, source, import_batch, last_updated) VALUES (?, 'ref', ?, ?, ?, ?, ?, ?, 'manual', 'batch1', ?)",
            (event_id, kind, basis, price, standard, point, day, T))

    def get_in(self, event_id, day, price):
        self.observed(event_id, "resale", price, basis="ask", day=day, point="get_in")

    def snapshot_lowest(self, event_id, at, lowest):
        self.db.execute(
            "INSERT INTO resale_snapshots (event_id, captured_at, lowest, source, last_updated)"
            " VALUES (?, ?, ?, 'seatgeek', ?)", (event_id, at, lowest, T))

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()


class Constraints(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.f = Fixture(self.db)

    def test_accounts_tables_untouched(self):
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "sessions", "follows", "preferences", "sent_alerts"} <= names)

    def test_external_ids_are_unique_for_upserts(self):
        self.f.artist("A", tm="K1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.artist("A again", tm="K1")

    def test_resale_snapshots_are_append_only(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        self.f.resale(e, "2026-10-01T00:00:00Z", 120)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE resale_snapshots SET median = 1")
        self.db.execute("DELETE FROM resale_snapshots")  # allowed, for retention limits
        self.assertEqual(self.f.one("SELECT COUNT(*) FROM resale_snapshots")[0], 0)

    def test_observed_resale_needs_a_basis(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.observed(e, "resale", 150, basis=None)
        self.f.observed(e, "face", 80)  # face needs none

    def test_get_in_must_be_a_resale_listing_price(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.observed(e, "resale", 90, basis="sold", point="get_in")
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.observed(e, "face", 90, point="get_in")
        self.f.get_in(e, "2026-10-01", 90)

    def test_bad_values_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.venue("V", capacity=0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO venues (name, name_key, venue_type, source, last_updated) VALUES ('V', 'v', 'garage', 't', ?)", (T,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO state_resale_rules (state, source, last_updated) VALUES ('NYC', 't', ?)", (T,))


class Views(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.f = Fixture(self.db)

    def test_demand_and_listeners_per_seat(self):
        a = self.f.artist("A", listeners=1_000_000, playcount=30_000_000)
        e = self.f.event(a, self.f.venue("Small", capacity=500), "2026-11-01")
        r = self.f.one("SELECT * FROM v_event_demand WHERE event_id = ?", e)
        self.assertEqual(r["listeners_per_seat"], 2000)
        self.assertEqual(r["plays_per_listener"], 30)
        e2 = self.f.event(a, self.f.venue("Unknown size"), "2026-11-02")
        self.assertIsNone(self.f.one("SELECT listeners_per_seat FROM v_event_demand WHERE event_id = ?", e2)[0])

    def test_momentum(self):
        a = self.f.artist("A", listeners=1_200_000)
        self.f.event(a, self.f.venue("V"), "2026-11-01")
        self.f.snapshot(a, "2026-06-15", 900_000)   # 109 days back: in the 90-day window (90-120)
        self.f.snapshot(a, "2026-08-25", 1_000_000)  # 38 days back: in the 30-day window
        self.f.snapshot(a, "2026-09-25", 1_100_000)  # 7 days back: too recent for either
        self.f.snapshot(a, "2026-10-02", 1_200_000)
        m = self.f.one("SELECT * FROM v_artist_momentum WHERE artist_id = ?", a)
        self.assertEqual((m["listeners"], m["listeners_30d_ago"], m["listeners_90d_ago"]), (1_200_000, 1_000_000, 900_000))
        d = self.f.one("SELECT growth_30d, growth_90d FROM v_event_demand")
        self.assertAlmostEqual(d["growth_30d"], 0.2)
        self.assertAlmostEqual(d["growth_90d"], 1_200_000 / 900_000 - 1)

    def test_momentum_is_null_without_old_enough_history(self):
        a = self.f.artist("A")
        self.f.event(a, self.f.venue("V"), "2026-11-01")
        self.f.snapshot(a, "2026-09-30", 100)
        self.f.snapshot(a, "2026-10-02", 110)
        d = self.f.one("SELECT growth_30d, growth_90d FROM v_event_demand")
        self.assertEqual((d["growth_30d"], d["growth_90d"]), (None, None))

    def test_scarcity(self):
        a = self.f.artist("A")
        chi1, chi2 = self.f.venue("Chi 1", "Chicago"), self.f.venue("Chi 2", "Chicago")
        nyc = self.f.venue("NYC", "New York", "NY")
        old = self.f.event(a, chi1, "2025-11-15")               # last visit to Chicago, a year earlier
        e1 = self.f.event(a, chi1, "2026-11-01")
        self.f.event(a, chi2, "2026-11-03")                     # second Chicago date on this tour
        self.f.event(a, nyc, "2026-11-10")
        self.f.event(a, nyc, "2026-11-11", status="cancelled")  # cancelled dates don't count
        s = self.f.one("SELECT * FROM v_event_scarcity WHERE event_id = ?", e1)
        self.assertEqual(s["tour_dates"], 3)
        self.assertEqual(s["dates_in_market_on_tour"], 2)
        self.assertEqual(s["days_since_last_in_market"], 351)
        self.assertEqual(s["dates_last_365d"], 1)
        first = self.f.one("SELECT * FROM v_event_scarcity WHERE event_id = ?", old)
        self.assertIsNone(first["days_since_last_in_market"])

    def test_metro_groups_cities(self):
        self.db.execute("INSERT INTO metros (name, source, last_updated) VALUES ('NYC metro', 'manual', ?)", (T,))
        a = self.f.artist("A")
        e1 = self.f.event(a, self.f.venue("MSG", "New York", "NY", metro_id=1), "2026-11-01")
        self.f.event(a, self.f.venue("UBS", "Elmont", "NY", metro_id=1), "2026-11-02")
        self.assertEqual(self.f.one("SELECT dates_in_market_on_tour FROM v_event_scarcity WHERE event_id = ?", e1)[0], 2)

    def test_prices_api_first_then_observed(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        api = self.f.event(a, v, "2026-11-01", face_min=50, face_max=150)
        self.f.resale(api, "2026-10-01T00:00:00Z", 200)
        self.f.resale(api, "2026-10-02T00:00:00Z", 260)          # latest snapshot wins
        self.f.observed(api, "resale", 999, basis="sold")          # ignored: API data exists
        p = self.f.one("SELECT * FROM v_event_prices WHERE event_id = ?", api)
        self.assertEqual((p["face"], p["face_source"], p["resale"], p["resale_basis"], p["resale_source"]),
                         (100, "ticketmaster", 260, "ask", "seatgeek"))

        manual = self.f.event(a, v, "2026-11-02")
        for price in (80, 90, 100, 400):
            self.f.observed(manual, "face", price)
        self.f.observed(manual, "face", 1000, standard=0)            # VIP: excluded
        self.f.observed(manual, "resale", 300, basis="ask")
        self.f.observed(manual, "resale", 210, basis="sold")
        self.f.observed(manual, "resale", 230, basis="sold")
        p = self.f.one("SELECT * FROM v_event_prices WHERE event_id = ?", manual)
        self.assertEqual((p["face"], p["face_source"], p["resale"], p["resale_basis"], p["resale_source"]),
                         (95, "observed", 220, "sold", "observed"))

        none = self.f.event(a, v, "2026-11-03")
        p = self.f.one("SELECT face, resale FROM v_event_prices WHERE event_id = ?", none)
        self.assertEqual((p["face"], p["resale"]), (None, None))   # stays NULL, never guessed

    def test_get_in_and_7_day_trend_from_logged_prices(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        self.f.get_in(e, "2026-09-20", 60)   # 12 days back: outside the 5-9 day window
        self.f.get_in(e, "2026-09-24", 80)   # 8 days back: the comparison point
        self.f.get_in(e, "2026-09-29", 90)   # 3 days back: too recent
        self.f.get_in(e, "2026-10-02", 100)
        g = self.f.one("SELECT * FROM v_event_get_in WHERE event_id = ?", e)
        self.assertEqual((g["get_in"], g["get_in_as_of"], g["get_in_7d_ago"], g["get_in_source"]), (100, "2026-10-02", 80, "observed"))
        self.assertAlmostEqual(g["get_in_trend_7d"], 0.25)
        f = self.f.one("SELECT get_in, get_in_trend_7d FROM v_event_features WHERE event_id = ?", e)
        self.assertAlmostEqual(f["get_in_trend_7d"], 0.25)

    def test_get_in_trend_is_null_without_a_week_of_history(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        self.f.get_in(e, "2026-10-01", 70)
        self.f.get_in(e, "2026-10-02", 75)
        g = self.f.one("SELECT get_in, get_in_7d_ago, get_in_trend_7d FROM v_event_get_in WHERE event_id = ?", e)
        self.assertEqual((g["get_in"], g["get_in_7d_ago"], g["get_in_trend_7d"]), (75, None, None))

    def test_get_in_prefers_api_and_stays_out_of_medians(self):
        a, v = self.f.artist("A"), self.f.venue("V")
        e = self.f.event(a, v, "2026-11-01")
        self.f.get_in(e, "2026-10-02", 50)                               # ignored: the API has readings
        self.f.snapshot_lowest(e, "2026-09-25T12:00:00Z", 100)
        self.f.snapshot_lowest(e, "2026-10-01T12:00:00Z", 110)
        g = self.f.one("SELECT get_in, get_in_source, get_in_7d_ago FROM v_event_get_in WHERE event_id = ?", e)
        self.assertEqual((g["get_in"], g["get_in_source"], g["get_in_7d_ago"]), (110, "seatgeek", 100))
        manual = self.f.event(a, v, "2026-11-02")
        self.f.get_in(manual, "2026-10-01", 40)                          # cheapest listing...
        self.f.observed(manual, "resale", 200, basis="ask")
        self.f.observed(manual, "resale", 220, basis="ask")
        resale = self.f.one("SELECT resale FROM v_event_prices WHERE event_id = ?", manual)[0]
        self.assertEqual(resale, 210)                                    # ...doesn't drag the median down

    def test_premiums_use_medians(self):
        a, b = self.f.artist("A"), self.f.artist("B")
        v = self.f.venue("Club", capacity=500, venue_type="club")
        for i, (artist, resale) in enumerate([(a, 150), (a, 200), (a, 400), (b, 120)]):
            e = self.f.event(artist, v, f"2026-11-0{i + 1}", face_min=100, face_max=100, sold_out=1 if i == 0 else None)
            self.f.resale(e, "2026-10-01T00:00:00Z", resale)
        vp = self.f.one("SELECT * FROM v_venue_premium WHERE venue_id = ?", v)
        self.assertEqual((vp["events"], vp["median_markup"]), (4, 1.75))   # median of 1.2, 1.5, 2.0, 4.0
        self.assertEqual(self.f.one("SELECT median_markup FROM v_venue_type_premium WHERE venue_type = 'club'")[0], 1.75)
        ap = self.f.one("SELECT * FROM v_artist_premium WHERE artist_id = ?", a)
        self.assertEqual((ap["events_with_markup"], ap["median_markup"]), (3, 2.0))
        self.assertEqual((ap["events_with_sellout_known"], ap["sellout_rate"]), (1, 1.0))
        bp = self.f.one("SELECT * FROM v_artist_premium WHERE artist_id = ?", b)
        self.assertEqual((bp["events_with_sellout_known"], bp["sellout_rate"]), (0, None))

    def test_market_reference(self):
        self.db.execute("INSERT INTO metros (name, population, source, last_updated) VALUES ('Chicago metro', 9400000, 'census', ?)", (T,))
        self.db.execute("INSERT INTO state_resale_rules (state, has_price_cap, source, last_updated) VALUES ('IL', 0, 'manual', ?)", (T,))
        v = self.f.venue("V", metro_id=1)
        m = self.f.one("SELECT * FROM venue_market WHERE venue_id = ?", v)
        self.assertEqual((m["metro_population"], m["has_price_cap"]), (9_400_000, 0))

    def test_features_row_per_event(self):
        a = self.f.artist("A", listeners=500_000)
        for i in range(3):
            self.f.event(a, self.f.venue(f"V{i}", capacity=1000), f"2026-11-0{i + 1}")
        self.f.event(self.f.artist("No data"), self.f.venue("X"), None)   # TBA date, no data
        rows = self.db.execute("SELECT * FROM v_event_features").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["listeners_per_seat"], 500)
        self.assertIsNone(rows[3]["listeners"])


if __name__ == "__main__":
    unittest.main()
