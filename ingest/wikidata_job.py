"""Wikidata -> D1.

Artists: MBID -> Wikidata item -> English Wikipedia title (sitelink) and official YouTube
channel ID (P2397). An item is accepted only if it carries our MBID (P434), so a wrong link
can't slip in. Artists with an MBID but no item go to match_review.

Venues are handled by ingest/venue_enrichment.py.

Terms: Wikidata's structured data is CC0. Requests send a descriptive User-Agent, run one at
a time, and the SPARQL queries are small.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import Http
from ingest import mediawiki
from ingest.db import Database, now_iso, review
from ingest.resolve import is_mbid

SOURCE = "wikidata"
API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "PouchIt/1.0 (https://pouchit.net) python-urllib"
ARTIST_TTL_DAYS = 30
ENTITY_BATCH = 50
SPARQL_BATCH = 150


def client() -> Http:
    return Http("Wikidata", 1.0)


def sparql(http: Http, query: str) -> list[dict]:
    # POST, so a query listing 150 MBIDs doesn't hit URL length limits.
    body = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    data = http.get_json(SPARQL, data=body, headers={
        "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded"})
    return [{k: v.get("value") for k, v in b.items()} for b in data.get("results", {}).get("bindings", [])]


def entities(http: Http, qids: list[str]) -> dict:
    q = urllib.parse.urlencode({"action": "wbgetentities", "ids": "|".join(qids), "props": "sitelinks|claims",
                                "sitefilter": "enwiki", "format": "json", "maxlag": 5})
    # mediawiki.get waits out "maxlag" refusals instead of reading them as "no data", which
    # would otherwise make every artist look unlinked during server lag.
    return mediawiki.get(http, f"{API}?{q}", {"User-Agent": USER_AGENT}).get("entities", {})


def claim_values(entity: dict, prop: str) -> list[str]:
    """Values of a string or ID claim, skipping deprecated statements, preferred first."""
    claims = [c for c in (entity.get("claims") or {}).get(prop, []) if c.get("rank") != "deprecated"]
    claims.sort(key=lambda c: c.get("rank") != "preferred")
    out = []
    for c in claims:
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(v, str):
            out.append(v)
    return out


def parse_artist_entity(entity: dict, mbid: str) -> dict:
    """Pure. ok=False when the item doesn't carry our MBID (a wrong link)."""
    if mbid.lower() not in [v.lower() for v in claim_values(entity, "P434")]:
        return {"ok": False}
    channels = claim_values(entity, "P2397")
    return {
        "ok": True,
        "wikipedia_title": ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title"),
        "youtube_channel_id": channels[0] if channels else None,
    }


def resolve_artist_items(http: Http, mbids: list[str]) -> dict[str, list[str]]:
    """MBID -> Wikidata item IDs carrying that MBID (P434)."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(mbids), SPARQL_BATCH):
        values = " ".join(json.dumps(m) for m in mbids[i:i + SPARQL_BATCH] if is_mbid(m))
        if not values:
            continue
        for r in sparql(http, f"SELECT ?item ?mbid WHERE {{ VALUES ?mbid {{ {values} }} ?item wdt:P434 ?mbid . }}"):
            out.setdefault(r["mbid"], []).append(r["item"].rsplit("/", 1)[-1])
    return out


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso(now)
    http = client()
    try:
        # Artists: find items for MBIDs without one.
        need = db.query("SELECT id, name, mbid FROM artists WHERE mbid IS NOT NULL AND wikidata_id IS NULL")
        found = resolve_artist_items(http, [r["mbid"] for r in need]) if need else {}
        taken = {r["wikidata_id"] for r in db.query("SELECT wikidata_id FROM artists WHERE wikidata_id IS NOT NULL")}
        stmts = []
        for r in need:
            items = found.get(r["mbid"], [])
            if len(items) == 1 and items[0] not in taken:
                stmts.append(("UPDATE artists SET wikidata_id = ? WHERE id = ? AND wikidata_id IS NULL", (items[0], r["id"])))
                taken.add(items[0])
            else:
                reason = "no Wikidata item has this MusicBrainz ID" if not items else (
                    "several Wikidata items share this MusicBrainz ID" if len(items) > 1 else "Wikidata item already linked to another artist row")
                stmts.append(review("artist_match", SOURCE, str(r["id"]), r["name"], {"reason": reason, "mbid": r["mbid"], "items": items}))
        db.batch(stmts)
        stats["rows_written"] += len(stmts)

        # Artists: Wikipedia title and YouTube channel from each item.
        cutoff = (now - timedelta(days=ARTIST_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.query("SELECT id, name, mbid, wikidata_id FROM artists WHERE wikidata_id IS NOT NULL AND mbid IS NOT NULL"
                        " AND (wikidata_checked_at IS NULL OR wikidata_checked_at < ?)", (cutoff,))
        for i in range(0, len(rows), ENTITY_BATCH):
            chunk = rows[i:i + ENTITY_BATCH]
            ents = entities(http, [r["wikidata_id"] for r in chunk])
            stmts = []
            for r in chunk:
                ent = ents.get(r["wikidata_id"])
                if ent is None:
                    continue  # not in the response: try again next run, never read as a mismatch
                d = parse_artist_entity(ent, r["mbid"])
                if not d["ok"]:
                    stmts.append(review("artist_match", SOURCE, str(r["id"]), r["name"],
                                        {"reason": "Wikidata item doesn't carry this MusicBrainz ID", "item": r["wikidata_id"], "mbid": r["mbid"]}))
                    stmts.append(("UPDATE artists SET wikidata_id = NULL, wikidata_checked_at = ? WHERE id = ?", (stamp, r["id"])))
                    continue
                stmts.append(("UPDATE artists SET wikipedia_title = ?, youtube_channel_id = ?, wikidata_checked_at = ?, last_updated = ? WHERE id = ?",
                              (d["wikipedia_title"], d["youtube_channel_id"], stamp, stamp, r["id"])))
            db.batch(stmts)
            stats["rows_written"] += len(stmts)

        print(f"  Wikidata: {len(need)} artists looked up, {len(rows)} detailed ({http.calls} calls)")
    finally:
        stats["api_calls"] += http.calls
