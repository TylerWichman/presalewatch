"""Wikipedia Pageviews -> D1: weekly human views of each artist's English Wikipedia article.

Stored as one row per artist per Monday-to-Sunday week (artist_pageviews_weekly), and only for
weeks that are over, so a daily run writes nothing until a new week completes. New artists get
a 52-week backfill (one request covers the whole range); after that each run adds the weeks
completed since the last stored one. If an artist's article title changes, their history is
replaced, since views of a different article aren't comparable.

Writes: D1's free tier allows 100,000 rows written a day for the whole database. A backfill is
52 rows per artist, and the table has no separate key index (WITHOUT ROWID), so one write each;
at most BACKFILL_ARTISTS_PER_RUN artists are backfilled per run (~3,100 writes) and the rest
follow on later runs. A week's top-up is one row per artist.

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
BACKFILL_WEEKS = 52
BACKFILL_ARTISTS_PER_RUN = 60    # 60 x 52 = ~3,100 rows written
ROWS_PER_INSERT = 14             # 7 parameters per row, under D1's 100 per statement


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


def last_full_week(yesterday: date) -> date:
    """Pure: the Monday of the latest Monday-to-Sunday week that ended on or before `yesterday`."""
    sunday = yesterday - timedelta(days=(yesterday.weekday() + 1) % 7)
    return sunday - timedelta(days=6)


def weekly(days: list[dict], through: date) -> list[dict]:
    """Pure: daily rows -> [{'week_start', 'views', 'days'}] for weeks ending on or before `through`."""
    weeks: dict[str, dict] = {}
    for d in days:
        day = date.fromisoformat(d["day"])
        monday = day - timedelta(days=day.weekday())
        if monday + timedelta(days=6) > through:
            continue
        w = weeks.setdefault(monday.isoformat(), {"week_start": monday.isoformat(), "views": 0, "days": 0})
        w["views"] += d["views"]
        w["days"] += 1
    return [weeks[k] for k in sorted(weeks)]


def insert_statements(artist_id: int, article: str, weeks: list[dict], now: str) -> list:
    stmts = []
    for i in range(0, len(weeks), ROWS_PER_INSERT):
        chunk = weeks[i:i + ROWS_PER_INSERT]
        values = ", ".join("(?, ?, ?, ?, ?, ?, ?)" for _ in chunk)
        params = tuple(x for w in chunk for x in (artist_id, w["week_start"], w["views"], w["days"], article, SOURCE, now))
        stmts.append((f"INSERT INTO artist_pageviews_weekly (artist_id, week_start, views, days, article, source, last_updated)"
                      f" VALUES {values} ON CONFLICT (artist_id, week_start) DO UPDATE SET views = excluded.views,"
                      " days = excluded.days, article = excluded.article, last_updated = excluded.last_updated"
                      " WHERE artist_pageviews_weekly.views IS NOT excluded.views OR artist_pageviews_weekly.days IS NOT excluded.days"
                      " OR artist_pageviews_weekly.article IS NOT excluded.article", params))
    return stmts


def plan(title: str, stored_article: str | None, last_week: str | None, full_week: date) -> tuple[date | None, bool]:
    """Pure: (Monday to fetch from, whether to wipe existing rows first). None = up to date.
    full_week is the Monday of the latest completed week (last_full_week)."""
    backfill = full_week - timedelta(weeks=BACKFILL_WEEKS - 1)
    if stored_article and stored_article != title:
        return backfill, True
    if not last_week:
        return backfill, False
    start = date.fromisoformat(last_week) + timedelta(weeks=1)
    return (start if start <= full_week else None), False


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso(now)
    full_week = last_full_week((now - timedelta(days=1)).date())
    through = full_week + timedelta(days=6)
    http = Http("Wikipedia pageviews", 0.2)
    rows = db.query(
        "SELECT a.id, a.wikipedia_title, MAX(p.week_start) AS last_week, MAX(p.article) AS article"
        " FROM artists a LEFT JOIN artist_pageviews_weekly p ON p.artist_id = a.id"
        " WHERE a.wikipedia_title IS NOT NULL GROUP BY a.id, a.wikipedia_title")
    backfills = topups = 0
    try:
        for r in rows:
            start, wipe = plan(r["wikipedia_title"], r["article"], r["last_week"], full_week)
            if start is None:
                continue
            full = r["last_week"] is None or wipe
            if full and backfills >= BACKFILL_ARTISTS_PER_RUN:
                continue
            weeks = weekly(fetch(http, r["wikipedia_title"], start, through), through)
            stmts = [("DELETE FROM artist_pageviews_weekly WHERE artist_id = ?", (r["id"],))] if wipe else []
            stmts += insert_statements(r["id"], r["wikipedia_title"], weeks, stamp)
            stmts.append(("UPDATE artists SET pageviews_checked_at = ? WHERE id = ?", (stamp, r["id"])))
            db.batch(stmts)
            backfills += full
            topups += not full
    finally:
        stats["api_calls"] += http.calls
    print(f"  Pageviews: {backfills} artists backfilled, {topups} topped up with new weeks ({http.calls} calls)")
