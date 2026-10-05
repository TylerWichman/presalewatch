"""Last.fm -> D1: listeners, plays, tags, similar artists, and a MusicBrainz ID candidate.

One artist.getInfo call per artist returns all of it. Artists are refreshed every
`refresh.lastfm_ttl_hours` (7 days), and each refresh adds a dated snapshot, so momentum
builds up. Not-found answers are cached too.

Terms: Last.fm data is for non-commercial use only (API ToS 3.1), must be credited, and total
stored Last.fm data must stay under 100 MB (4.3.4). Our use is far below that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import lastfm
from common import ApiError, load_config
from ingest.db import Database, now_iso, prune, review, set_mbid, upsert
from ingest.resolve import choose_mbid, is_mbid, name_key, same_name

SOURCE = "lastfm"
MAX_TAGS = 10
MAX_SIMILAR = 10


def parse(artist: dict | None, asked: str, trusted: bool) -> dict:
    """Pure: the fields we keep from an artist.getInfo response, or not-found."""
    if not artist or not (trusted or lastfm.same_artist(asked, artist.get("name") or "")):
        return {"found": False}
    stats = artist.get("stats") or {}
    listeners = lastfm.to_int(stats.get("listeners")) or None
    tags = [t.get("name", "").strip().lower() for t in ((artist.get("tags") or {}).get("tag") or []) if t.get("name")]
    similar = [s.get("name", "").strip() for s in ((artist.get("similar") or {}).get("artist") or []) if s.get("name")]
    return {
        "found": True,
        "name": artist.get("name"),
        "mbid": (artist.get("mbid") or "").lower() or None,
        "listeners": listeners,
        "playcount": lastfm.to_int(stats.get("playcount")) if listeners else None,
        "tags": list(dict.fromkeys(tags))[:MAX_TAGS],
        "similar": list(dict.fromkeys(similar))[:MAX_SIMILAR],
    }


def due(db: Database, now: datetime, ttl_hours: float, limit: int) -> list[dict]:
    cutoff = (now - timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.query(
        "SELECT a.id, a.name, a.lastfm_name, a.mbid FROM artists a"
        " WHERE a.lastfm_checked_at IS NULL OR a.lastfm_checked_at < ?"
        " ORDER BY a.lastfm_checked_at IS NOT NULL, a.lastfm_checked_at LIMIT ?", (cutoff, limit))


def statements(row: dict, info: dict, now: str, today: str, mbid_owner: int | None) -> list:
    """Pure: the writes for one artist's result."""
    aid = row["id"]
    if not info["found"]:
        return [("UPDATE artists SET lastfm_checked_at = ? WHERE id = ?", (now, aid))]
    out = [
        ("UPDATE artists SET lastfm_name = ?, lastfm_listeners = ?, lastfm_playcount = ?, lastfm_checked_at = ?, last_updated = ?"
         " WHERE id = ?", (info["name"], info["listeners"], info["playcount"], now, now, aid)),
        upsert("artist_metrics_snapshots", ("artist_id", "captured_on", "source"), {
            "artist_id": aid, "captured_on": today, "lastfm_listeners": info["listeners"],
            "lastfm_playcount": info["playcount"], "source": SOURCE, "last_updated": now}),
        prune("artist_tags", "tag", aid, SOURCE, info["tags"]),
        prune("artist_similar", "similar_name", aid, SOURCE, info["similar"]),
    ]
    out += [upsert("artist_tags", ("artist_id", "tag", "source"),
                   {"artist_id": aid, "tag": t, "rank": i + 1, "source": SOURCE, "last_updated": now})
            for i, t in enumerate(info["tags"])]
    out += [upsert("artist_similar", ("artist_id", "similar_name", "source"),
                   {"artist_id": aid, "similar_name": s, "source": SOURCE, "last_updated": now})
            for s in info["similar"]]
    if not row["mbid"]:
        mbid, how = choose_mbid(ticketmaster_mbid=None, lastfm_mbid=info["mbid"],
                                lastfm_name_ok=same_name(row["name"], info["name"]) or same_name(row["lastfm_name"], info["name"]))
        if mbid and mbid_owner not in (None, aid):
            out.append(review("artist_match", SOURCE, str(aid), row["name"],
                              {"reason": "Last.fm's MusicBrainz ID already belongs to another artist row", "mbid": mbid}, mbid_owner))
        elif mbid:
            out.append(set_mbid(aid, mbid, how, now))
    return out


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    lf = lastfm.LastFm.from_env()
    if not lf:
        print("  Last.fm: no LASTFM_API_KEY, skipping")
        return
    cfg = load_config()["refresh"]
    rows = due(db, now, cfg["lastfm_ttl_hours"], cfg["lastfm_max_calls"])
    stamp, today = now_iso(), now.strftime("%Y-%m-%d")
    pending, found = [], 0
    # Every MBID already held, plus ones assigned earlier in this run (not yet written).
    claimed: dict[str, int] = {r["mbid"]: r["id"] for r in db.query("SELECT id, mbid FROM artists WHERE mbid IS NOT NULL")}
    for row in rows:
        # A name that came from Ticketmaster's Last.fm link, or from an earlier verified lookup, is trusted.
        asked = row["lastfm_name"] or row["name"]
        try:
            info = parse(lf.artist_info(asked), asked, trusted=bool(row["lastfm_name"]))
        except ApiError as err:
            print(f"  Last.fm: stopping after error ({err.status})")
            break
        mbid = info.get("mbid")
        owner = None
        if is_mbid(mbid) and not row["mbid"]:
            owner = claimed.get(mbid)
            if owner is None:
                claimed[mbid] = row["id"]
        pending += statements(row, info, stamp, today, owner)
        found += info["found"]
        if len(pending) >= 200:
            db.batch(pending)
            stats["rows_written"] += len(pending)
            pending = []
    db.batch(pending)
    stats["rows_written"] += len(pending)
    stats["api_calls"] += lf.http.calls
    print(f"  Last.fm: {len(rows)} artists checked, {found} found ({lf.http.calls} calls)")
