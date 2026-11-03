"""Schema tests: apply every migration to an in-memory SQLite (the engine D1 runs) and check the
account tables."""

import sqlite3
import unittest
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def fresh_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    for f in sorted(MIGRATIONS.glob("*.sql")):
        db.executescript(f.read_text(encoding="utf-8"))
    return db


class Accounts(unittest.TestCase):
    def test_account_tables_exist(self):
        names = {r[0] for r in fresh_db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "sessions", "follows", "preferences", "sent_alerts"} <= names)

    def test_preferences_keep_only_the_follow_alert_setting(self):
        cols = {r[1] for r in fresh_db().execute("PRAGMA table_info(preferences)")}
        self.assertEqual(cols, {"user_id", "follow_alerts", "updated_at"})


if __name__ == "__main__":
    unittest.main()
