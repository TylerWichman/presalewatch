"""Wikidata -> D1.

Artists: MBID -> Wikidata item -> English Wikipedia title (sitelink) and official YouTube
channel ID (P2397). An item is accepted only if it carries our MBID (P434), so a wrong link
can't slip in. Artists with an MBID but no item go to match_review.

Venues: items within 1 km of Ticketmaster's coordinates. A confident match (close AND same
name) writes the Wikidata ID, and the capacity (P1083) when there's exactly one value. Two or
more capacity values (e.g. basketball vs concerts) and weaker matches go to match_review.

Terms: Wikidata's structured data is CC0. Requests send a descriptive User-Agent, run one at
a time, and the SPARQL queries are small.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import Http
from ingest.db import Database, now_iso, review
from ingest.resolve import is_mbid, venue_match

SOURCE = "wikidata"
API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "PouchIt/1.0 (https://pouchit.net) python-urllib"
ARTIST_TTL_DAYS = 30
VENUE_RETRY_DAYS = 90
MAX_VENUES = 150
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
    return http.get_json(f"{API}?{q}", headers={"User-Agent": USER_AGENT}).get("entities", {})


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


def venue_candidates(http: Http, lat: float, lon: float) -> list[dict]:
    rows = sparql(http, f"""
SELECT ?item ?label ?alias ?coord ?cap WHERE {{
  SERVICE wikibase:around {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral . bd:serviceParam wikibase:radius "1" . }}
  ?item rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  OPTIONAL {{ ?item wdt:P1083 ?cap }}
}} LIMIT 5000""")
    items: dict[str, dict] = {}
    for r in rows:
        qid = r["item"].rsplit("/", 1)[-1]
        it = items.setdefault(qid, {"qid": qid, "label": r.get("label"), "aliases": set(), "caps": set(), "coord": r.get("coord"),
                                    "enwiki": False})
        it["enwiki"] = it["enwiki"] or bool(r.get("article"))
        if r.get("alias"):
            it["aliases"].add(r["alias"])
        if r.get("cap"):
            try:
                it["caps"].add(int(float(r["cap"])))
            except ValueError:
                pass
    # Which candidates have an English Wikipedia article (a tie-breaker). A separate query,
    # because the query service doesn't reliably join this inside the location search.
    if items:
        values = " ".join(f"wd:{q}" for q in items)
        for r in sparql(http, f"SELECT ?item WHERE {{ VALUES ?item {{ {values} }}"
                              " ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }"):
            q = r["item"].rsplit("/", 1)[-1]
            if q in items:
                items[q]["enwiki"] = True
    return list(items.values())


def parse_point(wkt: str | None) -> tuple[float | None, float | None]:
    """'Point(-73.99 40.75)' -> (40.75, -73.99)."""
    try:
        lon, lat = wkt.split("(", 1)[1].rstrip(")").split()
        return float(lat), float(lon)
    except (AttributeError, IndexError, ValueError):
        return None, None


def decide_venue(venue: dict, candidates: list[dict]) -> dict:
    """Pure: what to do with a venue given its Wikidata candidates."""
    scored = []
    for c in candidates:
        clat, clon = parse_point(c["coord"])
        verdict, km = venue_match(name=venue["name"], lat=venue["latitude"], lon=venue["longitude"],
                                  cand_name=c["label"] or "", cand_aliases=sorted(c["aliases"]), cand_lat=clat, cand_lon=clon)
        if verdict != "no":
            scored.append({"verdict": verdict, "km": km, **c, "caps": sorted(c["caps"]), "aliases": sorted(c["aliases"])})
    confident = [s for s in scored if s["verdict"] == "confident"]
    # Wikidata often has two items for one building (e.g. the venue and a historic-theater record).
    # Tie-breakers, in order: the only one carrying a capacity, then the only one with an English
    # Wikipedia article (the main record). Still tied: review.
    for prefer in (lambda s: bool(s["caps"]), lambda s: s.get("enwiki", False)):
        if len(confident) > 1 and sum(map(prefer, confident)) == 1:
            confident = [s for s in confident if prefer(s)]
    if len(confident) == 1:
        c = confident[0]
        return {"action": "match", "qid": c["qid"], "capacity": c["caps"][0] if len(c["caps"]) == 1 else None,
                "capacity_review": c["caps"] if len(c["caps"]) > 1 else None, "candidate": c}
    if scored:
        scored.sort(key=lambda s: (s["verdict"] != "confident", s["km"] if s["km"] is not None else 99))
        return {"action": "review", "candidates": scored[:8],
                "reason": "several confident matches" if len(confident) > 1 else "close but not a clear match"}
    return {"action": "none"}


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso()
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
                d = parse_artist_entity(ents.get(r["wikidata_id"]) or {}, r["mbid"])
                if not d["ok"]:
                    stmts.append(review("artist_match", SOURCE, str(r["id"]), r["name"],
                                        {"reason": "Wikidata item doesn't carry this MusicBrainz ID", "item": r["wikidata_id"], "mbid": r["mbid"]}))
                    stmts.append(("UPDATE artists SET wikidata_id = NULL, wikidata_checked_at = ? WHERE id = ?", (stamp, r["id"])))
                    continue
                stmts.append(("UPDATE artists SET wikipedia_title = ?, youtube_channel_id = ?, wikidata_checked_at = ?, last_updated = ? WHERE id = ?",
                              (d["wikipedia_title"], d["youtube_channel_id"], stamp, stamp, r["id"])))
            db.batch(stmts)
            stats["rows_written"] += len(stmts)

        # Venues: confident coordinate + name matches only.
        retry = (now - timedelta(days=VENUE_RETRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        venues = db.query(
            "SELECT v.id, v.name, v.latitude, v.longitude, v.capacity, COUNT(e.id) AS n FROM venues v LEFT JOIN events e ON e.venue_id = v.id"
            " WHERE v.wikidata_id IS NULL AND v.latitude IS NOT NULL AND (v.wikidata_checked_at IS NULL OR v.wikidata_checked_at < ?)"
            " GROUP BY v.id ORDER BY n DESC LIMIT ?", (retry, MAX_VENUES))
        taken_v = {r["wikidata_id"] for r in db.query("SELECT wikidata_id FROM venues WHERE wikidata_id IS NOT NULL")}
        matched = reviewed = 0
        for v in venues:
            d = decide_venue(v, venue_candidates(http, v["latitude"], v["longitude"]))
            stmts = [("UPDATE venues SET wikidata_checked_at = ? WHERE id = ?", (stamp, v["id"]))]
            if d["action"] == "match" and d["qid"] not in taken_v:
                taken_v.add(d["qid"])
                matched += 1
                url = f"https://www.wikidata.org/wiki/{d['qid']}"
                stmts.append(("UPDATE venues SET wikidata_id = ?, last_updated = ? WHERE id = ?", (d["qid"], stamp, v["id"])))
                if d["capacity"] and v["capacity"] is None:
                    stmts.append(("UPDATE venues SET capacity = ?, capacity_source = 'wikidata', capacity_source_url = ?,"
                                  " capacity_verified = 0 WHERE id = ? AND capacity IS NULL", (d["capacity"], url, v["id"])))
                elif d["capacity_review"] and v["capacity"] is None:
                    stmts.append(review("venue_capacity", SOURCE, str(v["id"]), v["name"],
                                        {"reason": "several capacities listed (e.g. by event type)", "item": d["qid"], "values": d["capacity_review"]}))
            elif d["action"] in ("match", "review"):
                reviewed += 1
                cands = d.get("candidates") or [d["candidate"]]
                stmts.append(review("venue_match", SOURCE, str(v["id"]), v["name"], {
                    "reason": d.get("reason", "Wikidata item already linked to another venue"),
                    "candidates": [{"item": c["qid"], "label": c["label"], "km": round(c["km"], 2) if c["km"] is not None else None,
                                    "capacities": c["caps"]} for c in cands]}))
            db.batch(stmts)
            stats["rows_written"] += len(stmts)
        print(f"  Wikidata: {len(need)} artists looked up, {len(rows)} detailed; {len(venues)} venues checked,"
              f" {matched} matched, {reviewed} to review ({http.calls} calls)")
    finally:
        stats["api_calls"] += http.calls
