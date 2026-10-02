"""Wikipedia Pageviews -> D1: daily human views of each artist's English Wikipedia article.

New artists get a 12-month backfill (one request covers the whole range); after that each
run adds the days since the last stored one. If an artist's article title changes, their
history is replaced, since views of a different article aren't comparable.

D1's free tier allows 100,000 row writes a day and a full backfill is ~365 rows per artist,
so at most BACKFILL_ARTISTS_PER_RUN artists are backfilled per run; the rest follow on later
runs. Daily top-ups are tiny.

Terms: pageview data is CC0. The API requires a descriptive User-Agent with contact details
and asks clients to send one request at a time.
"""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime, timedelta, timezone

from common import ApiError, Http
from ingest.db import Database, now_iso

SOURCE = "wikimedia_pageviews"
API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user"
USER_AGENT = "PouchIt/1.0 (https://pouchit.net) python-urllib"
BACKFILL_DAYS = 365
BACKFILL_ARTISTS_PER_RUN = 120   # ~44,000 rows, well under D1's 100,000 daily writes
ROWS_PER_INSERT = 15             # 6 parameters per row, under D1's 100 per statement


def article_path(title: str) -> str:
    return urllib.parse.quote(title.replace(" ", "_"), safe="")


def fetch(http: Http, title: str, start: date, end: date) -> list[dict]:
    """Daily views between start and end inclusive. [] when the article has no data (404)."""
    url = f"{API}/{article_path(title)}/daily/{start:%Y%m%d}/{end:%Y%m%d}"
    try:
        data = http.get_json(url, headers={"User-Agent": USER_AGENT})
    except ApiError as err:
        if err.status == 404:
            return []
        raise
    return parse(data)


def parse(data: dict) -> list[dict]:
    """Pure: [{'day': 'YYYY-MM-DD', 'views': n}, ...] from an API response."""
    out = []
    for item in data.get("items") or []:
        ts, views = item.get("timestamp") or "", item.get("views")
        if len(ts) >= 8 and isinstance(views, int) and views >= 0:
            out.append({"day": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "views": views})
    return out


def insert_statements(artist_id: int, article: str, rows: list[dict], now: str) -> list:
    stmts = []
    for i in range(0, len(rows), ROWS_PER_INSERT):
        chunk = rows[i:i + ROWS_PER_INSERT]
        values = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in chunk)
        params = tuple(x for r in chunk for x in (artist_id, r["day"], r["views"], article, SOURCE, now))
        stmts.append((f"INSERT INTO artist_pageviews (artist_id, day, views, article, source, last_updated) VALUES {values}"
                      " ON CONFLICT (artist_id, day) DO UPDATE SET views = excluded.views, article = excluded.article,"
                      " last_updated = excluded.last_updated", params))
    return stmts


def plan(title: str, stored_article: str | None, last_day: str | None, yesterday: date) -> tuple[date | None, bool]:
    """Pure: (start date to fetch from, whether to wipe existing rows first). None = up to date."""
    if stored_article and stored_article != title:
        return yesterday - timedelta(days=BACKFILL_DAYS - 1), True
    if not last_day:
        return yesterday - timedelta(days=BACKFILL_DAYS - 1), False
    start = date.fromisoformat(last_day) + timedelta(days=1)
    return (start if start <= yesterday else None), False


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso()
    yesterday = (now - timedelta(days=1)).date()
    http = Http("Wikipedia pageviews", 0.2)
    rows = db.query(
        "SELECT a.id, a.wikipedia_title, MAX(p.day) AS last_day, MAX(p.article) AS article"
        " FROM artists a LEFT JOIN artist_pageviews p ON p.artist_id = a.id"
        " WHERE a.wikipedia_title IS NOT NULL GROUP BY a.id, a.wikipedia_title")
    backfills = topups = 0
    try:
        for r in rows:
            start, wipe = plan(r["wikipedia_title"], r["article"], r["last_day"], yesterday)
            if start is None:
                continue
            full = (yesterday - start).days >= BACKFILL_DAYS - 1
            if full and backfills >= BACKFILL_ARTISTS_PER_RUN:
                continue
            days = fetch(http, r["wikipedia_title"], start, yesterday)
            stmts = [("DELETE FROM artist_pageviews WHERE artist_id = ?", (r["id"],))] if wipe else []
            stmts += insert_statements(r["id"], r["wikipedia_title"], days, stamp)
            stmts.append(("UPDATE artists SET pageviews_checked_at = ? WHERE id = ?", (stamp, r["id"])))
            db.batch(stmts)
            stats["rows_written"] += len(days)
            backfills += full
            topups += not full
    finally:
        stats["api_calls"] += http.calls
    print(f"  Pageviews: {backfills} artists backfilled, {topups} topped up ({http.calls} calls)")
