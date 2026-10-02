"""Database access for ingestion: a local SQLite file (development, tests, dry runs) or
Cloudflare D1 over its HTTP API (production, from GitHub Actions).

Both speak the same small interface, and both run the same SQL, since D1 is SQLite.
Every write is an idempotent upsert keyed on an external ID.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from common import ROOT, env

MIGRATIONS_DIR = ROOT / "migrations"
# The production database (see wrangler.toml). Not a secret.
D1_DATABASE_ID = "b3dc330c-e82f-48b6-a117-b0d6400d1b73"
D1_BATCH = 50      # statements per HTTP request
D1_MAX_PARAMS = 100

Statement = tuple[str, tuple]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def batch(self, statements: list[Statement]) -> None:
        raise NotImplementedError

    def run(self, sql: str, params: tuple = ()) -> None:
        self.batch([(sql, params)])

    def scalar(self, sql: str, params: tuple = ()):
        rows = self.query(sql, params)
        return next(iter(rows[0].values())) if rows else None


class SqliteDatabase(Database):
    """A local SQLite file with every migration applied, tracked in _migrations."""

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY)")
        done = {r[0] for r in self.conn.execute("SELECT name FROM _migrations")}
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name not in done:
                self.conn.executescript(f.read_text(encoding="utf-8"))
                self.conn.execute("INSERT INTO _migrations (name) VALUES (?)", (f.name,))
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def batch(self, statements: list[Statement]) -> None:
        with self.conn:  # one transaction
            for sql, params in statements:
                self.conn.execute(sql, params)


class D1Error(Exception):
    pass


class D1Database(Database):
    """Cloudflare D1 through the REST API. Errors never include the token."""

    def __init__(self, account_id: str, token: str, database_id: str = D1_DATABASE_ID):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.token = token
        self.requests = 0

    @classmethod
    def from_env(cls) -> "D1Database":
        account, token = env("CLOUDFLARE_ACCOUNT_ID"), env("CLOUDFLARE_D1_TOKEN")
        if not (account and token):
            sys.exit("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_D1_TOKEN must be set to write to D1.")
        return cls(account, token)

    def _post(self, body: dict) -> list[dict]:
        data = json.dumps(body).encode()
        for attempt in range(5):
            self.requests += 1
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as err:
                if err.code == 429 or err.code >= 500:
                    time.sleep(min(2 ** (attempt + 1), 30))
                    continue
                detail = err.read().decode("utf-8", "replace")[:300]
                raise D1Error(f"D1 HTTP {err.code}: {detail}") from None
        else:
            raise D1Error("D1 kept failing; giving up")
        if not out.get("success"):
            raise D1Error(f"D1 error: {json.dumps(out.get('errors'))[:300]}")
        return out["result"]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return self._post({"sql": sql, "params": list(params)})[0].get("results", [])

    def batch(self, statements: list[Statement]) -> None:
        for sql, params in statements:
            if len(params) > D1_MAX_PARAMS:
                raise ValueError(f"D1 allows at most {D1_MAX_PARAMS} parameters per statement")
        for i in range(0, len(statements), D1_BATCH):
            chunk = statements[i:i + D1_BATCH]
            self._post({"batch": [{"sql": s, "params": list(p)} for s, p in chunk]})


def open_db(target: str) -> Database:
    """'d1' for production, or 'sqlite:<path>' for a local file."""
    if target == "d1":
        return D1Database.from_env()
    if target.startswith("sqlite:"):
        return SqliteDatabase(target.removeprefix("sqlite:"))
    raise ValueError("database must be 'd1' or 'sqlite:<path>'")


def upsert(table: str, key: tuple[str, ...], row: dict, update: tuple[str, ...] | None = None) -> Statement:
    """INSERT ... ON CONFLICT (key) DO UPDATE. Table and column names come from code, never input.

    update: columns to overwrite on conflict (default: every non-key column). NULLs in `row`
    never overwrite existing values, so a source that lacks a field can't erase another's.
    """
    cols = list(row)
    update = tuple(c for c in (update or cols) if c not in key)
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})" for c in update) or f"{key[0]} = {key[0]}"
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
           f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {sets}")
    return sql, tuple(row[c] for c in cols)


def rows_in(db: Database, table: str, columns: str, column: str, values: list) -> list[dict]:
    """SELECT columns FROM table WHERE column IN (values), chunked under D1's parameter cap."""
    out: list[dict] = []
    values = [v for v in dict.fromkeys(values) if v is not None]
    for i in range(0, len(values), D1_MAX_PARAMS - 1):
        chunk = values[i:i + D1_MAX_PARAMS - 1]
        marks = ", ".join("?" for _ in chunk)
        out += db.query(f"SELECT {columns} FROM {table} WHERE {column} IN ({marks})", tuple(chunk))
    return out


def id_map(db: Database, table: str, column: str, values: list) -> dict:
    """Internal IDs for rows keyed by an external ID column."""
    return {r["k"]: r["id"] for r in rows_in(db, table, f"id, {column} AS k", column, values)}


@contextmanager
def run_log(db: Database, source: str) -> Iterator[dict]:
    """Record an ingestion run in ingest_runs. The yielded dict collects api_calls, rows_written, note."""
    started = now_iso()
    stats = {"api_calls": 0, "rows_written": 0, "note": None, "status": "ok"}
    try:
        yield stats
    except Exception as err:
        stats["status"] = "failed"
        stats["note"] = f"{type(err).__name__}: {str(err)[:200]}"
        raise
    finally:
        db.run("INSERT INTO ingest_runs (source, started_at, finished_at, status, api_calls, rows_written, note)"
               " VALUES (?, ?, ?, ?, ?, ?, ?)",
               (source, started, now_iso(), stats["status"], stats["api_calls"], stats["rows_written"], stats["note"]))


def set_mbid(artist_id: int, mbid: str, how: str, now: str) -> Statement:
    """Give an artist an MBID unless it already has one or another row holds that MBID.
    The guard is in the SQL itself, so a conflict skips this row instead of failing the batch."""
    return ("UPDATE artists SET mbid = ?, mbid_source = ?, last_updated = ? WHERE id = ? AND mbid IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM artists other WHERE other.mbid = ?)", (mbid, how, now, artist_id, mbid))


def review(kind: str, source: str, external_id: str, name: str, details: dict,
           candidate_id: int | None = None, score: float | None = None) -> Statement:
    """Queue something for a person to check. Re-queuing updates the open item, never duplicates it."""
    now = now_iso()
    return (
        "INSERT INTO match_review (kind, source, external_id, external_name, details, candidate_id, candidate_score,"
        " status, created_at, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
        " ON CONFLICT (kind, source, external_id) DO UPDATE SET details = excluded.details,"
        " candidate_id = excluded.candidate_id, candidate_score = excluded.candidate_score,"
        " last_updated = excluded.last_updated WHERE match_review.status = 'open'",
        (kind, source, external_id, name, json.dumps(details, ensure_ascii=False), candidate_id, score, now, now),
    )
