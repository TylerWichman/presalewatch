"""MusicBrainz -> D1: resolve missing MBIDs by strict search, and pull details for known ones
(artist type, country, years active, aliases, and the Wikidata item from URL relationships).

Terms: core MusicBrainz data (artists, aliases, relationships) is CC0. The API allows about
1 request per second per IP and requires a User-Agent with contact details; too many
requests get HTTP 503, which the client backs off from.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import ApiError, Http
from ingest.db import Database, now_iso, reject_mbid, review, set_mbid, upsert
from ingest.resolve import implausible_era, is_mbid, name_key, pick_search_result

SOURCE = "musicbrainz"
API = "https://musicbrainz.org/ws/2"
USER_AGENT = "PouchIt/1.0 ( https://pouchit.net )"
DETAIL_TTL_DAYS = 90
SEARCH_RETRY_DAYS = 30
MAX_CALLS = 400            # about 7 minutes at 1 request/second
MAX_ALIASES = 20
WIKIDATA_RE = re.compile(r"wikidata\.org/wiki/(Q\d+)$")


def client() -> Http:
    return Http("MusicBrainz", 1.1)


def get(http: Http, path: str, params: dict) -> dict:
    return http.get_json(f"{API}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'})}",
                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def year(value: str | None) -> int | None:
    return int(value[:4]) if value and value[:4].isdigit() else None


def parse_details(data: dict) -> dict:
    """Pure: the fields we keep from an artist lookup with inc=aliases+url-rels."""
    life = data.get("life-span") or {}
    wikidata = None
    for rel in data.get("relations") or []:
        if rel.get("type") == "wikidata":
            m = WIKIDATA_RE.search((rel.get("url") or {}).get("resource") or "")
            if m:
                wikidata = m.group(1)
                break
    aliases = []
    for a in data.get("aliases") or []:
        if a.get("name") and a["name"] not in [x["alias"] for x in aliases]:
            aliases.append({"alias": a["name"], "locale": a.get("locale")})
    return {
        "artist_type": data.get("type"),
        "country": data.get("country"),
        "active_from": year(life.get("begin")),
        "active_to": year(life.get("end")) if life.get("ended") else None,
        "wikidata_id": wikidata,
        "aliases": aliases[:MAX_ALIASES],
    }


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso()
    http = client()
    retry_before = (now - timedelta(days=SEARCH_RETRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail_before = (now - timedelta(days=DETAIL_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    held = {r["mbid"]: r["id"] for r in db.query("SELECT id, mbid FROM artists WHERE mbid IS NOT NULL")}
    taken_qids = {r["wikidata_id"] for r in db.query("SELECT wikidata_id FROM artists WHERE wikidata_id IS NOT NULL")}
    resolved = reviewed = detailed = rejected = 0
    try:
        # 1. Artists with no MBID: strict name search.
        for row in db.query(
                "SELECT id, name FROM artists WHERE mbid IS NULL AND (musicbrainz_checked_at IS NULL OR musicbrainz_checked_at < ?)"
                " ORDER BY musicbrainz_checked_at IS NOT NULL, musicbrainz_checked_at", (retry_before,)):
            if http.calls >= MAX_CALLS // 2:
                break
            data = get(http, "artist", {"query": f'artist:"{row["name"]}"', "limit": 10})
            pick, why = pick_search_result(row["name"], data.get("artists") or [])
            # A resolved artist is left unstamped so step 2 fetches its details in this same run.
            stmts = []
            if pick and pick["id"] not in held:
                stmts.append(set_mbid(row["id"], pick["id"], "musicbrainz_search", stamp))
                held[pick["id"]] = row["id"]
                resolved += 1
            else:
                cands = [{"mbid": r.get("id"), "name": r.get("name"), "disambiguation": r.get("disambiguation"),
                          "country": r.get("country"), "score": r.get("score")} for r in (data.get("artists") or [])[:5]]
                reason = why if not pick else "MusicBrainz ID already belongs to another artist row"
                stmts.append(review("artist_match", SOURCE, str(row["id"]), row["name"], {"reason": reason, "candidates": cands}))
                stmts.append(("UPDATE artists SET musicbrainz_checked_at = ? WHERE id = ?", (stamp, row["id"])))
                reviewed += 1
            db.batch(stmts)
            stats["rows_written"] += len(stmts)

        # 2. Artists with an MBID: details, aliases, and the Wikidata item.
        for row in db.query(
                "SELECT id, name, mbid, wikidata_id FROM artists WHERE mbid IS NOT NULL"
                " AND (musicbrainz_checked_at IS NULL OR musicbrainz_checked_at < ? OR artist_type IS NULL AND musicbrainz_checked_at < ?)"
                " ORDER BY musicbrainz_checked_at IS NOT NULL, musicbrainz_checked_at", (detail_before, retry_before)):
            if http.calls >= MAX_CALLS or not is_mbid(row["mbid"]):
                continue
            try:
                d = parse_details(get(http, f"artist/{row['mbid']}", {"inc": "aliases+url-rels"}))
            except ApiError as err:
                if err.status == 404:  # MBID merged or deleted upstream
                    db.batch([review("artist_match", SOURCE, str(row["id"]), row["mbid"], {"reason": "MBID not found in MusicBrainz"}),
                              ("UPDATE artists SET musicbrainz_checked_at = ? WHERE id = ?", (stamp, row["id"]))])
                    continue
                raise
            why = implausible_era(d["artist_type"], d["active_from"], d["active_to"])
            if why:
                db.batch(reject_mbid(row["id"], row["mbid"], row["name"], why, stamp))
                del held[row["mbid"]]
                rejected += 1
                continue
            qid = d["wikidata_id"] if d["wikidata_id"] and not row["wikidata_id"] and d["wikidata_id"] not in taken_qids else None
            if qid:
                taken_qids.add(qid)
            stmts = [("UPDATE artists SET artist_type = ?, country = ?, active_from = ?, active_to = ?,"
                      " wikidata_id = COALESCE(wikidata_id, ?), musicbrainz_checked_at = ?, last_updated = ? WHERE id = ?",
                      (d["artist_type"], d["country"], d["active_from"], d["active_to"], qid, stamp, stamp, row["id"])),
                     ("DELETE FROM artist_aliases WHERE artist_id = ? AND source = ?", (row["id"], SOURCE))]
            stmts += [upsert("artist_aliases", ("artist_id", "alias", "source"), {
                "artist_id": row["id"], "alias": a["alias"], "alias_key": name_key(a["alias"]), "locale": a["locale"],
                "source": SOURCE, "last_updated": stamp}) for a in d["aliases"]]
            db.batch(stmts)
            stats["rows_written"] += len(stmts)
            detailed += 1
    finally:
        stats["api_calls"] += http.calls
    print(f"  MusicBrainz: {resolved} MBIDs found by search, {reviewed} sent to review, {detailed} artists detailed,"
          f" {rejected} MBIDs rejected as a namesake from another era ({http.calls} calls)")
