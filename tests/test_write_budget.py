"""Write budget: unchanged rows aren't rewritten, the daily budget stops jobs cleanly, and the
pageview history moves to weekly totals."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ingest import lastfm_job, run as ingest_run, ticketmaster_job
from ingest.db import MIGRATIONS_DIR, SqliteDatabase, WriteBudgetSpent, review, run_log, upsert

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_events.json"
NOW = "2026-10-03T12:00:00Z"


def temp_db() -> SqliteDatabase:
    return SqliteDatabase(Path(tempfile.mkdtemp()) / "t.db")


class UnchangedRowsAreNotRewritten(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()

    def written(self, statements) -> int:
        before = self.db.rows_written
        self.db.batch(statements)
        return self.db.rows_written - before

    def test_upsert_writes_only_on_change(self):
        row = {"ticketmaster_id": "V1", "name": "Hall", "name_key": "hall", "city": "Reno", "source": "t", "last_updated": NOW}
        self.assertEqual(self.written([upsert("venues", ("ticketmaster_id",), row)]), 1)
        later = {**row, "last_updated": "2026-10-04T12:00:00Z"}
        self.assertEqual(self.written([upsert("venues", ("ticketmaster_id",), later)]), 0)     # only last_updated differs
        self.assertEqual(self.db.scalar("SELECT last_updated FROM venues"), NOW)                # last changed, not last seen
        self.assertEqual(self.written([upsert("venues", ("ticketmaster_id",), {**later, "city": None})]), 0)  # NULL never erases
        self.assertEqual(self.written([upsert("venues", ("ticketmaster_id",), {**later, "city": "Sparks"})]), 1)
        self.assertEqual(self.db.scalar("SELECT city FROM venues"), "Sparks")

    def test_ticketmaster_rerun_only_refreshes_last_seen(self):
        parsed = [ticketmaster_job.parse(ev) for ev in json.loads(FIXTURE.read_text(encoding="utf-8"))["_embedded"]["events"]]
        parsed = [p for p in parsed if p]
        first = self.db.rows_written
        ticketmaster_job.write(self.db, parsed, NOW)
        full = self.db.rows_written - first
        second = self.db.rows_written
        ticketmaster_job.write(self.db, parsed, "2026-10-04T12:00:00Z")
        events = self.db.scalar("SELECT COUNT(*) FROM events")
        self.assertEqual(self.db.rows_written - second, events)          # one last_seen_at refresh per event, nothing else
        self.assertLess(events, full)
        self.assertEqual(self.db.scalar("SELECT MIN(last_seen_at) FROM events"), "2026-10-04T12:00:00Z")
        self.assertEqual(self.db.scalar("SELECT MIN(first_seen_at) FROM events"), NOW)

    def test_tags_are_pruned_not_rewritten(self):
        self.db.run("INSERT INTO artists (name, name_key, source, last_updated) VALUES ('A', 'a', 't', ?)", (NOW,))
        info = {"found": True, "name": "A", "listeners": 10, "playcount": 20, "mbid": None, "tags": ["rock", "indie"], "similar": ["B"]}
        row = {"id": 1, "name": "A", "lastfm_name": None, "mbid": "x"}
        self.db.batch(lastfm_job.statements(row, info, NOW, "2026-10-03", None))
        again = self.written(lastfm_job.statements(row, info, "2026-10-10T12:00:00Z", "2026-10-10", None))
        self.assertEqual(again, 2)   # the artist's counts and the new dated snapshot; tags and similar untouched
        changed = self.written(lastfm_job.statements(row, {**info, "tags": ["rock"]}, "2026-10-17T12:00:00Z", "2026-10-17", None))
        self.assertEqual(changed, 3)  # counts, snapshot, and the one tag that's gone
        self.assertEqual([r["tag"] for r in self.db.query("SELECT tag FROM artist_tags")], ["rock"])

    def test_unchanged_review_item_is_not_rewritten(self):
        item = review("venue_match", "enrichment", "7", "Hall", {"reason": "x"})
        self.assertEqual(self.written([item]), 1)
        self.assertEqual(self.written([review("venue_match", "enrichment", "7", "Hall", {"reason": "x"})]), 0)
        self.assertEqual(self.written([review("venue_match", "enrichment", "7", "Hall", {"reason": "y"})]), 1)


class DailyBudget(unittest.TestCase):
    def test_budget_stops_batches_and_logs_partial(self):
        db = temp_db()
        db.budget = 2
        rows = [upsert("venues", ("ticketmaster_id",), {"ticketmaster_id": f"V{i}", "name": "x", "name_key": "x", "source": "t",
                                                         "last_updated": NOW}) for i in range(3)]
        with self.assertRaises(WriteBudgetSpent):
            with run_log(db, "venues"):
                for r in rows:
                    db.batch([r])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM venues"), 2)        # stopped before the third batch
        log = db.query("SELECT status, rows_written, note FROM ingest_runs")[0]
        self.assertEqual((log["status"], log["rows_written"]), ("partial", 2))   # the log itself is written past the budget
        self.assertIn("budget", log["note"])

    def test_budget_counts_what_ran_earlier_the_same_day(self):
        db = temp_db()
        db.run("INSERT INTO ingest_runs (source, started_at, finished_at, status, api_calls, rows_written) VALUES"
               " ('ticketmaster', '2026-10-03T09:40:00Z', '2026-10-03T09:41:00Z', 'ok', 1, 45000),"
               " ('ticketmaster', '2026-10-02T09:40:00Z', '2026-10-02T09:41:00Z', 'ok', 1, 99000)")
        ingest_run.set_budget(db, 60000, datetime(2026, 10, 3, 15, tzinfo=timezone.utc))
        self.assertEqual(db.budget, 15000)
        ingest_run.set_budget(db, 60000, datetime(2026, 10, 4, 1, tzinfo=timezone.utc))
        self.assertEqual(db.budget, 60000)                                  # a new UTC day

    def test_run_skips_remaining_jobs_cleanly(self):
        path = Path(tempfile.mkdtemp()) / "t.db"
        calls = []

        def job(name, spend):
            def run(db, stats, now=None):
                calls.append(name)
                if spend:
                    db.budget = 0
                    db.batch([("INSERT INTO metros (name, source, last_updated) VALUES ('m', 't', 'x')", ())])
            return type("Job", (), {"run": staticmethod(run)})

        jobs = {"a": job("a", True), "b": job("b", False)}
        cfg = {"ingest": {"daily_write_budget": 60000}}
        with mock.patch.object(ingest_run, "JOBS", jobs), mock.patch.object(ingest_run, "load_config", return_value=cfg), \
                mock.patch.object(ingest_run, "report", return_value=""), \
                mock.patch("sys.argv", ["run", "--db", f"sqlite:{path}"]):
            ingest_run.main()                                               # no SystemExit: a spent budget isn't a failure
        self.assertEqual(calls, ["a"])                                      # b is skipped until the next run
        db = SqliteDatabase(path)
        self.assertEqual([r["status"] for r in db.query("SELECT status FROM ingest_runs")], ["partial"])


class D1Counting(unittest.TestCase):
    def test_d1_counts_its_reported_rows_written(self):
        from ingest.db import D1Database
        db = D1Database("acct", "token")
        results = [{"meta": {"rows_written": 5}}, {"meta": {"rows_written": 0}}, {"meta": {}}]
        with mock.patch.object(db, "_post", return_value=results) as post:
            db.batch([("UPDATE x SET y = 1", ())] * 3)
            self.assertEqual(db.rows_written, 5)
            db.budget = 5
            with self.assertRaises(WriteBudgetSpent):
                db.batch([("UPDATE x SET y = 1", ())])
            self.assertEqual(post.call_count, 1)                           # nothing sent once the budget is spent


class PageviewMigration(unittest.TestCase):
    def test_daily_rows_become_complete_weeks(self):
        conn = sqlite3.connect(":memory:")
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        upto = [f for f in files if f.name < "0007"]
        for f in upto:
            conn.executescript(f.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO artists (name, name_key, source, last_updated) VALUES ('R', 'r', 't', 'x')")
        # Wed Sep 16 .. Thu Oct 1: a partial first week, one full week, and an unfinished last week.
        for d in range(16, 31):
            conn.execute("INSERT INTO artist_pageviews VALUES (1, ?, 10, 'R', 'wikimedia_pageviews', 'x')", (f"2026-09-{d:02d}",))
        conn.execute("INSERT INTO artist_pageviews VALUES (1, '2026-10-01', 10, 'R', 'wikimedia_pageviews', 'x')")
        conn.executescript((MIGRATIONS_DIR / "0007_write_budget.sql").read_text(encoding="utf-8"))
        rows = conn.execute("SELECT week_start, views, days FROM artist_pageviews_weekly ORDER BY week_start").fetchall()
        self.assertEqual(rows, [("2026-09-14", 50, 5), ("2026-09-21", 70, 7)])   # Sep 28 week isn't over: left out
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'artist_pageviews'").fetchone())
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'artists_name_key'").fetchone())
        conn.execute("SELECT * FROM v_event_demand_sources").fetchall()       # dependent views still resolve


if __name__ == "__main__":
    unittest.main()
