"""Database access for ingestion: a local SQLite file (development, tests, dry runs) or
Cloudflare D1 over its HTTP API (production, from GitHub Actions).

Both speak the same small interface, and both run the same SQL, since D1 is SQLite.
Every write is an idempotent upsert keyed on an external ID, and an upsert whose values haven't
changed writes nothing.

Writes are counted (D1 reports rows written per statement, index entries included) and can be
capped with a budget: D1's free tier allows 100,000 rows written a day across the whole database,
and sign-ins and alert preferences share it. A batch that would start past the budget raises
WriteBudgetSpent; the job stops there and resumes from where it left off on the next run.
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


def now_iso(at: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp for `at` (default: the current time). Jobs pass their run time, so a
    run dated in a test stamps its rows with that date, not the real clock."""
    return (at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class WriteBudgetSpent(Exception):
    """The run's write budget is used up. Not a failure: the job resumes on the next run."""


class Database:
    rows_written = 0          # rows written so far through this connection (D1's own count on D1)
    budget: int | None = None  # stop starting new batches once rows_written reaches this

    def check_budget(self) -> None:
        if self.budget is not None and self.rows_written >= self.budget:
            raise WriteBudgetSpent(f"write budget of {self.budget:,} rows spent ({self.rows_written:,} written)")

    @contextmanager
    def unbudgeted(self) -> Iterator[None]:
        """For bookkeeping writes (the ingest_runs log) that must happen even when the budget is spent."""
        saved, self.budget = self.budget, None
        try:
            yield
        finally:
            self.budget = saved

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
        if not statements:
            return
        self.check_budget()
        before = self.conn.total_changes
        with self.conn:  # one transaction
            for sql, params in statements:
                self.conn.execute(sql, params)
        # SQLite counts changed rows, not index entries, so locally this undercounts D1's figure.
        self.rows_written += self.conn.total_changes - before


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
            self.check_budget()
            chunk = statements[i:i + D1_BATCH]
            results = self._post({"batch": [{"sql": s, "params": list(p)} for s, p in chunk]})
            self.rows_written += sum(int((r.get("meta") or {}).get("rows_written") or 0) for r in results)


def open_db(target: str) -> Database:
    """'d1' for production, or 'sqlite:<path>' for a local file."""
    if target == "d1":
        return D1Database.from_env()
    if target.startswith("sqlite:"):
        return SqliteDatabase(target.removeprefix("sqlite:"))
    raise ValueError("database must be 'd1' or 'sqlite:<path>'")


VOLATILE = ("last_updated",)


def upsert(table: str, key: tuple[str, ...], row: dict, update: tuple[str, ...] | None = None,
           volatile: tuple[str, ...] = VOLATILE) -> Statement:
    """INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE something changed. Table and column names
    come from code, never input.

    update: columns to overwrite on conflict (default: every non-key column). NULLs in `row`
    never overwrite existing values, so a source that lacks a field can't erase another's.
    volatile: columns that are refreshed when the row is written but don't by themselves make
    it worth writing (last_updated). If nothing else differs, the existing row is left alone and
    D1 counts no write. last_updated therefore means "last changed".
    """
    cols = list(row)
    update = tuple(c for c in (update or cols) if c not in key)
    head = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT ({', '.join(key)})"
    params = tuple(row[c] for c in cols)
    changed = [c for c in update if c not in volatile]
    if not changed:
        return f"{head} DO NOTHING", params
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})" for c in update)
    where = " OR ".join(f"(excluded.{c} IS NOT NULL AND excluded.{c} IS NOT {table}.{c})" for c in changed)
    return f"{head} DO UPDATE SET {sets} WHERE {where}", params


def prune(table: str, column: str, artist_id: int, source: str, keep: list[str]) -> Statement:
    """Delete an artist's rows from `source` whose `column` is no longer in `keep`. Used with
    upserts instead of delete-everything-then-reinsert, so an unchanged list writes nothing."""
    keep = list(dict.fromkeys(keep))[:D1_MAX_PARAMS - 2]
    sql = f"DELETE FROM {table} WHERE artist_id = ? AND source = ?"
    if keep:
        sql += f" AND {column} NOT IN ({', '.join('?' for _ in keep)})"
    return sql, (artist_id, source, *keep)


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
    start_written = db.rows_written
    try:
        yield stats
    except WriteBudgetSpent:
        stats["status"] = "partial"
        stats["note"] = "daily write budget spent; continues on the next run"
        raise
    except Exception as err:
        stats["status"] = "failed"
        stats["note"] = f"{type(err).__name__}: {str(err)[:200]}"
        raise
    finally:
        # rows_written is what the database counted (on D1, index entries included), not what the
        # job tried to write: unchanged upserts count nothing.
        stats["rows_written"] = db.rows_written - start_written
        with db.unbudgeted():
            db.run("INSERT INTO ingest_runs (source, started_at, finished_at, status, api_calls, rows_written, note)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (source, started, now_iso(), stats["status"], stats["api_calls"], stats["rows_written"], stats["note"]))


def set_mbid(artist_id: int, mbid: str, how: str, now: str) -> Statement:
    """Give an artist an MBID unless it already has one or another row holds that MBID.
    The guard is in the SQL itself, so a conflict skips this row instead of failing the batch."""
    return ("UPDATE artists SET mbid = ?, mbid_source = ?, last_updated = ? WHERE id = ? AND mbid IS NULL"
            " AND mbid_rejected IS NOT ?"
            " AND NOT EXISTS (SELECT 1 FROM artists other WHERE other.mbid = ?)", (mbid, how, now, artist_id, mbid, mbid))


def reject_mbid(artist_id: int, mbid: str, name: str, reason: str, now: str) -> list[Statement]:
    """Unlink a MusicBrainz ID that belongs to someone else, and everything found through it.

    The ID is kept in mbid_rejected so no job (Ticketmaster supplies some of these) attaches it
    again; the artist is searched by name again on the next MusicBrainz run.
    """
    return [
        ("UPDATE artists SET mbid = NULL, mbid_source = NULL, mbid_rejected = ?, wikidata_id = NULL, wikipedia_title = NULL,"
         " youtube_channel_id = NULL, artist_type = NULL, country = NULL, active_from = NULL, active_to = NULL,"
         " musicbrainz_checked_at = NULL, wikidata_checked_at = NULL, listenbrainz_checked_at = NULL,"
         " pageviews_checked_at = NULL, last_updated = ? WHERE id = ?", (mbid, now, artist_id)),
        ("DELETE FROM artist_aliases WHERE artist_id = ? AND source = 'musicbrainz'", (artist_id,)),
        ("DELETE FROM artist_metrics_snapshots WHERE artist_id = ? AND source = 'listenbrainz'", (artist_id,)),
        ("DELETE FROM artist_pageviews_weekly WHERE artist_id = ?", (artist_id,)),
        ("DELETE FROM artist_youtube_current WHERE artist_id = ?", (artist_id,)),
        review("artist_match", "musicbrainz", str(artist_id), name,
               {"reason": f"MusicBrainz ID rejected: that artist {reason}", "mbid": mbid}),
    ]


def review(kind: str, source: str, external_id: str, name: str, details: dict,
           candidate_id: int | None = None, score: float | None = None) -> Statement:
    """Queue something for a person to check. Re-queuing updates the open item, never duplicates it."""
    now = now_iso()
    return (
        "INSERT INTO match_review (kind, source, external_id, external_name, details, candidate_id, candidate_score,"
        " status, created_at, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)"
        " ON CONFLICT (kind, source, external_id) DO UPDATE SET details = excluded.details,"
        " candidate_id = excluded.candidate_id, candidate_score = excluded.candidate_score,"
        " last_updated = excluded.last_updated WHERE match_review.status = 'open'"
        " AND (match_review.details IS NOT excluded.details OR match_review.candidate_id IS NOT excluded.candidate_id"
        " OR match_review.candidate_score IS NOT excluded.candidate_score)",
        (kind, source, external_id, name, json.dumps(details, ensure_ascii=False), candidate_id, score, now, now),
    )
