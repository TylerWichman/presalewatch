"""ListenBrainz -> D1: all-time listener and listen totals per artist MBID, as dated snapshots.

Only the two totals are kept. The API response also lists individual user names with their
listen counts; those are personal data we don't need, so they're dropped and never stored.

Terms: ListenBrainz data is CC0 and commercial use is allowed (MetaBrainz asks commercial
users to support them financially). The API allows about 30 requests per 10 seconds.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from ingest.db import Database, now_iso, upsert
from ingest.resolve import is_mbid

SOURCE = "listenbrainz"
API = "https://api.listenbrainz.org/1/stats/artist/{mbid}/listeners?range=all_time"
USER_AGENT = "PouchIt/1.0 ( https://pouchit.net )"
TTL_DAYS = 7
MIN_INTERVAL = 0.4   # 30 requests per 10 seconds, with headroom


def parse(data: dict | None) -> dict | None:
    """Pure: the two totals, or None. Drops the per-user listener list."""
    p = (data or {}).get("payload") or {}
    users, listens = p.get("total_user_count"), p.get("total_listen_count")
    if not isinstance(users, int) or not isinstance(listens, int):
        return None
    return {"listeners": users, "listens": listens}


class Client:
    def __init__(self):
        self.calls = 0
        self.last = 0.0

    def get(self, mbid: str) -> dict | None:
        """The JSON body, or None when ListenBrainz has no stats for the artist (204/404)."""
        for attempt in range(5):
            wait = MIN_INTERVAL - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(API.format(mbid=mbid), headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return json.loads(body) if resp.status == 200 and body else None
            except urllib.error.HTTPError as err:
                if err.code in (204, 404):
                    return None
                if err.code == 429 or err.code >= 500:
                    reset = err.headers.get("X-RateLimit-Reset-In")
                    time.sleep(min(float(reset) + 1 if reset else 2 ** (attempt + 1), 60))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** (attempt + 1))
        return None


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp, today = now_iso(), now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.query("SELECT id, mbid FROM artists WHERE mbid IS NOT NULL"
                    " AND (listenbrainz_checked_at IS NULL OR listenbrainz_checked_at < ?)", (cutoff,))
    client, found, pending = Client(), 0, []
    try:
        for r in rows:
            if not is_mbid(r["mbid"]):
                continue
            totals = parse(client.get(r["mbid"]))
            pending.append(("UPDATE artists SET listenbrainz_checked_at = ? WHERE id = ?", (stamp, r["id"])))
            if totals:
                found += 1
                pending.append(upsert("artist_metrics_snapshots", ("artist_id", "captured_on", "source"), {
                    "artist_id": r["id"], "captured_on": today, "listenbrainz_listeners": totals["listeners"],
                    "listenbrainz_listens": totals["listens"], "source": SOURCE, "last_updated": stamp}))
            if len(pending) >= 100:
                db.batch(pending)
                stats["rows_written"] += len(pending)
                pending = []
        db.batch(pending)
        stats["rows_written"] += len(pending)
    finally:
        stats["api_calls"] += client.calls
    print(f"  ListenBrainz: {len(rows)} artists checked, {found} with stats ({client.calls} calls)")
