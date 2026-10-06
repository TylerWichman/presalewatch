"""YouTube Data API -> D1: CURRENT channel statistics only.

YouTube's developer policies shape this module:
  * III.E.4.d: public channel statistics may be stored for at most 30 days. Each run
    overwrites the single current row per artist, and deletes any row older than 30 days.
  * III.E.4.h: no new or derived metrics from YouTube data. So nothing here is combined
    with other data (no subscribers per seat), and scoring doesn't use it. The values are
    kept only for display.
Channel IDs come from Wikidata (P2397), never from YouTube search (search.list costs 100
quota units; channels.list costs 1 unit per 50 channels).
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone

from common import ApiError, Http, env
from ingest.db import Database, now_iso

SOURCE = "youtube"
API = "https://www.googleapis.com/youtube/v3/channels"
BATCH = 50
MAX_AGE_DAYS = 30


def to_int(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse(item: dict) -> dict:
    """Pure: statistics for one channel. Hidden subscriber counts stay NULL."""
    s = item.get("statistics") or {}
    return {
        "channel_id": item.get("id"),
        "subscriber_count": None if s.get("hiddenSubscriberCount") else to_int(s.get("subscriberCount")),
        "view_count": to_int(s.get("viewCount")),
        "video_count": to_int(s.get("videoCount")),
    }


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso(now)
    expired = (now - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Retention first, so a missing key or a failed run can't leave stale YouTube data behind.
    db.batch([
        ("DELETE FROM artist_youtube_current WHERE fetched_at < ?", (expired,)),
        ("DELETE FROM artist_youtube_current WHERE artist_id NOT IN"
         " (SELECT id FROM artists WHERE youtube_channel_id IS NOT NULL AND youtube_channel_id = artist_youtube_current.channel_id)", ()),
    ])
    key = env("YOUTUBE_API_KEY")
    if not key:
        print("  YouTube: no YOUTUBE_API_KEY, skipping (expired rows were still cleaned up)")
        return
    rows = db.query("SELECT id, youtube_channel_id FROM artists WHERE youtube_channel_id IS NOT NULL")
    by_channel = {r["youtube_channel_id"]: r["id"] for r in rows}
    http = Http("YouTube", 0.2)
    updated = 0
    try:
        ids = list(by_channel)
        for i in range(0, len(ids), BATCH):
            q = urllib.parse.urlencode({"part": "statistics", "id": ",".join(ids[i:i + BATCH]), "maxResults": BATCH, "key": key})
            try:
                data = http.get_json(f"{API}?{q}")
            except ApiError as err:
                print(f"  YouTube: stopping after HTTP {err.status}")
                break
            stmts = []
            for item in data.get("items") or []:
                d = parse(item)
                aid = by_channel.get(d["channel_id"])
                if aid is None:
                    continue
                # Every value is overwritten (a channel that hides its count now must not keep an
                # old one), and fetched_at always moves: retention is counted from the last fetch.
                # An update in place is one row written; INSERT OR REPLACE would be a delete plus
                # an insert.
                stmts.append(("INSERT INTO artist_youtube_current (artist_id, channel_id, subscriber_count,"
                              " view_count, video_count, fetched_at, source, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                              " ON CONFLICT (artist_id) DO UPDATE SET channel_id = excluded.channel_id,"
                              " subscriber_count = excluded.subscriber_count, view_count = excluded.view_count,"
                              " video_count = excluded.video_count, fetched_at = excluded.fetched_at, source = excluded.source,"
                              " last_updated = excluded.last_updated",
                              (aid, d["channel_id"], d["subscriber_count"], d["view_count"], d["video_count"], stamp, SOURCE, stamp)))
            db.batch(stmts)
            updated += len(stmts)
            stats["rows_written"] += len(stmts)
    finally:
        stats["api_calls"] += http.calls
    print(f"  YouTube: {updated} channels updated ({http.calls} calls, {http.calls} quota units)")
