"""Self-filling venue enrichment, run daily: capacity and details for every venue with upcoming events.

Each run:
  1. Hand-entered capacities (config/venues.csv, plus any imported hand-fill CSV) are applied
     first. They're marked verified and nothing automated ever overwrites them.
  2. Venues with upcoming events are processed busiest first: never-checked venues (new ones
     get done on the run they first appear), then ones still without a capacity after 30 days,
     then filled ones due for re-verification after 180 days.
  3. Sources are tried in order until one gives a confident match with a usable capacity:
     Wikidata -> Wikipedia (MediaWiki API) -> OpenStreetMap (Overpass). Ticketmaster venue details
     (time zone, venue page) are fetched once per venue.
  4. Confident matches write to venues; uncertain matches and low-confidence capacities go to
     match_review. Every capacity seen is kept in venue_capacity_observations.

Terms (see docs/database.md):
  Wikidata: CC0. Wikipedia: text CC BY-SA; a capacity number is a fact, the article is linked as
  its source. OpenStreetMap: ODbL, so OSM values need "(c) OpenStreetMap contributors" credit and
  the OSM-derived data must be offered under ODbL if the database is used publicly; OSM is the
  last resort and every value it supplies is marked capacity_source = 'openstreetmap'.
  Ticketmaster: venue details stored only to provide the service.
"""

from __future__ import annotations

import csv
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import ApiError, Http, ROOT, env
from ingest import mediawiki
from ingest.db import Database, now_iso, review, rows_in
from ingest.venue_rules import choose_capacity, classify, is_venue, parse_capacity, pick_confident, venue_type

USER_AGENT = "PouchIt/1.0 (https://pouchit.net) python-urllib"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
OVERPASS = "https://overpass-api.de/api/interpreter"
TM_VENUE = "https://app.ticketmaster.com/discovery/v2/venues/{id}.json"
MANUAL_CSV = ROOT / "config" / "venues.csv"

RETRY_DAYS = 30
REVERIFY_DAYS = 180
MAX_VENUES_PER_RUN = 200
DISAGREE = 0.10          # a re-check differing by more than 10% goes to review instead of overwriting
SOURCES = ("wikidata", "wikipedia", "openstreetmap")
OSM_RADIUS_M = 500     # lighter on the public Overpass server; confident matches need 300 m anyway


# ---- Fetchers ------------------------------------------------------------------------------

class Fetch:
    def __init__(self):
        self.wikidata = Http("Wikidata", 1.0)
        self.wikipedia = Http("Wikipedia", 0.5)
        self.osm = Http("OpenStreetMap Overpass", 2.0)
        self.tm = Http("Ticketmaster", 0.25)

    @property
    def calls(self) -> int:
        return self.wikidata.calls + self.wikipedia.calls + self.osm.calls + self.tm.calls

    # Wikidata ----------------------------------------------------------------------------
    def wikidata_candidates(self, lat: float, lon: float) -> list[dict]:
        query = f"""
SELECT ?item ?coord (SAMPLE(?l) AS ?label) (GROUP_CONCAT(DISTINCT ?a; separator="|") AS ?aliases) WHERE {{
  SERVICE wikibase:around {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral . bd:serviceParam wikibase:radius "1" . }}
  ?item rdfs:label ?l . FILTER(LANG(?l) = "en")
  OPTIONAL {{ ?item skos:altLabel ?a . FILTER(LANG(?a) = "en") }}
}} GROUP BY ?item ?coord LIMIT 2000"""
        body = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
        data = self.wikidata.get_json(SPARQL, data=body, headers={
            "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json", "Content-Type": "application/x-www-form-urlencoded"})
        out = []
        for b in data.get("results", {}).get("bindings", []):
            g = {k: v.get("value") for k, v in b.items()}
            clat, clon = parse_point(g.get("coord"))
            out.append({"qid": g["item"].rsplit("/", 1)[-1], "label": g.get("label") or "",
                        "aliases": [a for a in (g.get("aliases") or "").split("|") if a], "lat": clat, "lon": clon})
        return out

    def wikidata_entities(self, qids: list[str], props: str = "claims|sitelinks", languages: str | None = None) -> dict:
        out: dict = {}
        for i in range(0, len(qids), 50):
            params = {"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": props, "format": "json", "maxlag": 5}
            if "sitelinks" in props:
                params["sitefilter"] = "enwiki"
            if languages:
                params["languages"] = languages
            out.update(mediawiki.get(self.wikidata, f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}", {"User-Agent": USER_AGENT}).get("entities", {}))
        return out

    def labels(self, qids: set[str]) -> dict[str, str]:
        if not qids:
            return {}
        ents = self.wikidata_entities(sorted(qids), props="labels", languages="en")
        return {q: ((e.get("labels") or {}).get("en") or {}).get("value", "") for q, e in ents.items()}

    # Wikipedia ---------------------------------------------------------------------------
    def wikipedia_nearby(self, lat: float, lon: float) -> list[dict]:
        q = urllib.parse.urlencode({"action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 1000,
                                    "gslimit": 50, "format": "json", "maxlag": 5})
        data = mediawiki.get(self.wikipedia, f"{WIKIPEDIA_API}?{q}", {"User-Agent": USER_AGENT})
        return [{"title": g["title"], "meters": g.get("dist")} for g in data.get("query", {}).get("geosearch", [])]

    def wikipedia_lead(self, title: str) -> str:
        """Wikitext of the article's lead section (where the infobox is)."""
        q = urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext", "section": 0, "redirects": 1,
                                    "format": "json", "maxlag": 5})
        try:
            data = mediawiki.get(self.wikipedia, f"{WIKIPEDIA_API}?{q}", {"User-Agent": USER_AGENT})
        except ApiError as err:
            if err.status == 404 or "missingtitle" in str(err):
                return ""
            raise
        return ((data.get("parse") or {}).get("wikitext") or {}).get("*", "")

    # OpenStreetMap -----------------------------------------------------------------------
    def osm_nearby(self, lat: float, lon: float) -> list[dict]:
        kinds = "theatre|nightclub|arts_centre|events_venue|concert_hall|music_venue|stadium|sports_centre|arena|casino|bar|pub|amphitheatre|community_centre|conference_centre|exhibition_centre"
        query = f"""[out:json][timeout:25];
(
  nwr(around:{OSM_RADIUS_M},{lat},{lon})["name"][~"^(amenity|leisure|building)$"~"^({kinds})$"];
  nwr(around:{OSM_RADIUS_M},{lat},{lon})["name"]["capacity"];
);
out tags center 100;"""
        data = self.osm.get_json(OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
        out = []
        for el in data.get("elements", []):
            tags = el.get("tags") or {}
            c = el.get("center") or el
            out.append({"osm_id": f"{el.get('type')}/{el.get('id')}", "tags": tags, "lat": c.get("lat"), "lon": c.get("lon")})
        return out

    # Ticketmaster ------------------------------------------------------------------------
    def ticketmaster_venue(self, venue_id: str) -> dict | None:
        key = env("TM_API_KEY")
        if not key:
            return None
        try:
            return self.tm.get_json(f"{TM_VENUE.format(id=urllib.parse.quote(venue_id))}?{urllib.parse.urlencode({'apikey': key})}")
        except ApiError as err:
            if err.status == 404:
                return {}
            raise


def parse_point(wkt: str | None) -> tuple[float | None, float | None]:
    """'Point(-73.99 40.75)' -> (40.75, -73.99)."""
    try:
        lon, lat = wkt.split("(", 1)[1].rstrip(")").split()
        return float(lat), float(lon)
    except (AttributeError, IndexError, ValueError):
        return None, None


# ---- Turning source data into candidates (pure) ----------------------------------------------

def claims(entity: dict, prop: str) -> list[dict]:
    return [c for c in (entity.get("claims") or {}).get(prop, []) if c.get("rank") != "deprecated"]


def claim_qids(entity: dict, prop: str) -> list[str]:
    out = []
    for c in claims(entity, prop):
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(v["id"])
    return out


def wikidata_capacities(entity: dict) -> list[dict]:
    """P1083 values with the configuration they apply to (qualifier P518, as a QID for now)."""
    out = []
    for c in claims(entity, "P1083"):
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if not isinstance(v, dict):
            continue
        try:
            n = int(float(v.get("amount", "0")))
        except ValueError:
            continue
        parts = [q.get("datavalue", {}).get("value", {}).get("id") for q in (c.get("qualifiers") or {}).get("P518", [])]
        out.append({"n": n, "part": next((p for p in parts if p), None), "approx": False, "range": False})
    return out


def opened_date(entity: dict) -> str | None:
    for c in claims(entity, "P1619"):
        v = (c.get("mainsnak") or {}).get("datavalue", {}).get("value") or {}
        t, precision = v.get("time"), v.get("precision")
        if t and t.startswith("+"):
            return t[1:5] if (precision or 0) < 11 else t[1:11]
    return None


def wikidata_candidate(item: dict, entity: dict, labels: dict[str, str]) -> dict:
    caps = [{**c, "label": labels.get(c.pop("part"), None) if c.get("part") else None} for c in wikidata_capacities(entity)]
    types = [labels.get(q, "") for q in claim_qids(entity, "P31")]
    operators = [labels.get(q, "") for q in claim_qids(entity, "P137")]
    title = ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    return {
        "source": "wikidata", "source_id": item["qid"], "url": f"https://www.wikidata.org/wiki/{item['qid']}",
        "names": [item["label"], *item["aliases"]], "lat": item["lat"], "lon": item["lon"], "type_texts": types,
        "capacity_values": caps, "capacity_raw": "; ".join(f"{c['n']:,}" + (f" ({c['label']})" if c["label"] else "") for c in caps) or None,
        "opened": opened_date(entity), "operator": next((o for o in operators if o), None), "wikipedia_title": title,
    }


INFOBOX = re.compile(r"\{\{\s*Infobox\s+([^|\n}]+)", re.I)


def infobox_params(wikitext: str) -> tuple[str | None, dict[str, str]]:
    """The first infobox's template name and its top-level parameters."""
    m = INFOBOX.search(wikitext or "")
    if not m:
        return None, {}
    i, depth, start = m.start(), 0, m.start()
    end = len(wikitext)
    while i < len(wikitext) - 1:
        two = wikitext[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
            continue
        i += 1
    body = wikitext[start + 2:end - 2]
    params, depth, cur = {}, 0, ""
    parts = []
    for ch_i, ch in enumerate(body):  # split on top-level "|"
        pair = body[ch_i:ch_i + 2]
        if pair in ("{{", "[["):
            depth += 1
        elif pair in ("}}", "]]"):
            depth -= 1
        if ch == "|" and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts[1:]:
        k, sep, v = p.partition("=")
        if sep:
            params[k.strip().lower()] = v.strip()
    return m.group(1).strip(), params


def wikipedia_candidate(title: str, meters: float | None, wikitext: str) -> dict | None:
    template, params = infobox_params(wikitext)
    if not template:
        return None
    raw = params.get("capacity") or params.get("seating_capacity") or params.get("seating capacity")
    return {
        "source": "wikipedia", "source_id": title, "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
        "names": [title, params.get("name", "")], "meters": meters, "lat": None, "lon": None,
        "type_texts": [template, params.get("type", ""), params.get("building_type", "")],
        "capacity_raw": raw or None, "opened": re.sub(r"[^\d-]", "", params.get("opened", ""))[:10] or None,
        "operator": re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", params.get("operator", "")).strip() or None,
        "wikipedia_title": title,
    }


def osm_candidate(el: dict) -> dict:
    t = el["tags"]
    return {
        "source": "openstreetmap", "source_id": el["osm_id"], "url": f"https://www.openstreetmap.org/{el['osm_id']}",
        "names": [t.get("name", ""), t.get("alt_name", ""), t.get("official_name", ""), t.get("old_name", "")],
        "lat": el["lat"], "lon": el["lon"],
        "type_texts": [t.get(k, "") for k in ("amenity", "leisure", "building", "theatre:type")],
        "capacity_raw": t.get("capacity") or None, "osm_id": el["osm_id"],
        "operator": t.get("operator") or None, "opened": (t.get("start_date") or "")[:10] or None,
    }


def candidate_capacity(c: dict) -> dict | None:
    if c.get("capacity_values"):
        return choose_capacity(c["capacity_values"])
    return parse_capacity(c.get("capacity_raw"))


def evaluate(venue: dict, candidates: list[dict]) -> dict:
    """Pure: classify a source's candidates for a venue and decide.

    Returns {"match": candidate or None, "capacity": parsed or None, "review": [candidates]}.
    """
    classified = []
    for c in candidates:
        verdict, meters = classify(name=venue["name"], lat=venue["latitude"], lon=venue["longitude"], cand_names=c["names"],
                                   cand_lat=c.get("lat"), cand_lon=c.get("lon"), type_ok=is_venue(c["type_texts"]), meters=c.get("meters"))
        if verdict != "no":
            classified.append({**c, "verdict": verdict, "meters": meters})
    match = pick_confident(classified)
    reviewable = [] if match else sorted(classified, key=lambda c: (c["verdict"] != "confident", c["meters"]))[:6]
    return {"match": match, "capacity": candidate_capacity(match) if match else None, "review": reviewable}


# ---- Database work -----------------------------------------------------------------------------

def apply_manual(db: Database, now: str) -> int:
    """Hand-entered capacities from config/venues.csv: verified, and never overwritten by a source."""
    if not MANUAL_CSV.exists():
        return 0
    with MANUAL_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = {r["venue_id"]: r for r in csv.DictReader(fh) if (r.get("venue_id") and (r.get("capacity") or "").strip().isdigit())}
    venues = {r["ticketmaster_id"]: r for r in rows_in(db, "venues", "id, ticketmaster_id, capacity, capacity_source", "ticketmaster_id", list(rows))}
    stmts = []
    for tm_id, v in venues.items():
        cap = int(rows[tm_id]["capacity"])
        if v["capacity_source"] == "manual" and v["capacity"] == cap:
            continue
        stmts += write_manual(v["id"], cap, "config", now)
    db.batch(stmts)
    return len(stmts)


def write_manual(venue_id: int, capacity: int, source_id: str, now: str) -> list:
    return [
        ("UPDATE venues SET capacity = ?, capacity_source = 'manual', capacity_source_url = NULL, capacity_raw = ?,"
         " capacity_confidence = 'high', capacity_verified = 1, capacity_checked_at = ?, last_updated = ? WHERE id = ?",
         (capacity, str(capacity), now, now, venue_id)),
        ("INSERT INTO venue_capacity_observations (venue_id, source, source_id, raw_text, parsed_capacity, confidence, observed_at, last_updated)"
         " VALUES (?, 'manual', ?, ?, ?, 'high', ?, ?) ON CONFLICT (venue_id, source, source_id) DO UPDATE SET raw_text = excluded.raw_text,"
         " parsed_capacity = excluded.parsed_capacity, observed_at = excluded.observed_at, last_updated = excluded.last_updated",
         (venue_id, source_id, str(capacity), capacity, now, now)),
    ]


def due(db: Database, now: datetime, limit: int) -> list[dict]:
    """Venues with upcoming events to process: never checked first, then 30-day retries of unresolved,
    then 180-day re-verification of filled ones; busiest first within each group."""
    today = now.strftime("%Y-%m-%d")
    retry = (now - timedelta(days=RETRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reverify = (now - timedelta(days=REVERIFY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.query(
        "SELECT v.id, v.ticketmaster_id, v.name, v.city, v.state, v.latitude, v.longitude, v.capacity, v.capacity_source,"
        " v.capacity_verified, v.capacity_checked_at, v.ticketmaster_checked_at, v.wikidata_id, COUNT(e.id) AS upcoming"
        " FROM venues v JOIN events e ON e.venue_id = v.id AND e.event_date >= ?"
        " WHERE v.latitude IS NOT NULL AND v.capacity_verified = 0 AND ("
        "   v.capacity_checked_at IS NULL"
        "   OR (v.capacity IS NULL AND v.capacity_checked_at < ?)"
        "   OR (v.capacity IS NOT NULL AND v.capacity_checked_at < ?))"
        " GROUP BY v.id"
        " ORDER BY v.capacity_checked_at IS NOT NULL, v.capacity IS NOT NULL, upcoming DESC LIMIT ?",
        (today, retry, reverify, limit))


def observation(venue_id: int, c: dict, cap: dict | None, now: str) -> tuple:
    return ("INSERT INTO venue_capacity_observations (venue_id, source, source_id, source_url, raw_text, parsed_capacity, label,"
            " confidence, match_distance_m, observed_at, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (venue_id, source, source_id) DO UPDATE SET source_url = excluded.source_url, raw_text = excluded.raw_text,"
            " parsed_capacity = excluded.parsed_capacity, label = excluded.label, confidence = excluded.confidence,"
            " match_distance_m = excluded.match_distance_m, observed_at = excluded.observed_at, last_updated = excluded.last_updated",
            (venue_id, c["source"], c["source_id"], c.get("url"), c.get("capacity_raw"), cap["capacity"] if cap else None,
             cap["label"] if cap else None, cap["confidence"] if cap else "low", round(c["meters"], 1) if c.get("meters") is not None else None, now, now))


def details_update(venue: dict, c: dict, now: str) -> tuple:
    """Fill venue details from a confident match without overwriting anything already set."""
    kind = venue_type(c["type_texts"])
    return ("UPDATE venues SET venue_type = COALESCE(venue_type, ?), opened = COALESCE(opened, ?), operator = COALESCE(operator, ?),"
            " wikipedia_title = COALESCE(wikipedia_title, ?), osm_id = COALESCE(osm_id, ?),"
            " wikidata_id = COALESCE(wikidata_id, CASE WHEN ? IS NOT NULL AND NOT EXISTS (SELECT 1 FROM venues o WHERE o.wikidata_id = ?) THEN ? END),"
            " last_updated = ? WHERE id = ?",
            (kind, c.get("opened"), c.get("operator"), c.get("wikipedia_title"), c.get("osm_id"),
             c["source_id"] if c["source"] == "wikidata" else None, c["source_id"] if c["source"] == "wikidata" else None,
             c["source_id"] if c["source"] == "wikidata" else None, now, venue["id"]))


def capacity_statements(venue: dict, c: dict, cap: dict, now: str) -> list:
    """Write a resolved capacity, or send a disagreement with the stored value to review."""
    current = venue["capacity"]
    if current is not None and abs(cap["capacity"] - current) > DISAGREE * current:
        return [review("venue_capacity", c["source"], str(venue["id"]), venue["name"], {
            "reason": f"re-check found {cap['capacity']:,} but {current:,} is stored", "stored_source": venue["capacity_source"],
            "found": cap["capacity"], "found_raw": c.get("capacity_raw"), "url": c.get("url")})]
    return [("UPDATE venues SET capacity = ?, capacity_raw = ?, capacity_source = ?, capacity_source_url = ?, capacity_confidence = ?,"
             " capacity_verified = 0, last_updated = ? WHERE id = ? AND capacity_verified = 0",
             (cap["capacity"], c.get("capacity_raw"), c["source"], c.get("url"), cap["confidence"], now, venue["id"]))]


def enabled_sources() -> tuple[str, ...]:
    """VENUE_SOURCES (e.g. 'wikidata,wikipedia') limits a one-off run; default is every source."""
    wanted = [x.strip() for x in (env("VENUE_SOURCES") or "").split(",") if x.strip()]
    return tuple(x for x in SOURCES if x in wanted) if wanted else SOURCES


def enrich_one(fetch: Fetch, venue: dict, now: str, sources: tuple[str, ...] = SOURCES) -> tuple[list, str]:
    """All writes for one venue, and what happened ('resolved', 'review', 'unresolved', 'same', 'source_error', 'deferred')."""
    stmts: list = []
    reviews: list[dict] = []
    resolved = None
    known_title = None
    low_caps: list[tuple[dict, dict]] = []
    failed: list[str] = []
    for source in sources:
        try:
            if source == "wikidata":
                items = fetch.wikidata_candidates(venue["latitude"], venue["longitude"])
                near = [i for i in items if i["lat"] is not None and classify(
                    name=venue["name"], lat=venue["latitude"], lon=venue["longitude"], cand_names=[i["label"], *i["aliases"]],
                    cand_lat=i["lat"], cand_lon=i["lon"], type_ok=True)[0] != "no"][:50]
                ents = fetch.wikidata_entities([i["qid"] for i in near]) if near else {}
                label_ids = {q for e in ents.values() for p in ("P31", "P137") for q in claim_qids(e, p)}
                label_ids |= {c["part"] for e in ents.values() for c in wikidata_capacities(e) if c["part"]}
                labels = fetch.labels(label_ids)
                cands = [wikidata_candidate(i, ents[i["qid"]], labels) for i in near if i["qid"] in ents]
            elif source == "wikipedia":
                if known_title:
                    titles = [{"title": known_title, "meters": 0.0}]
                else:
                    titles = [t for t in fetch.wikipedia_nearby(venue["latitude"], venue["longitude"])
                              if classify(name=venue["name"], lat=None, lon=None, cand_names=[t["title"]], cand_lat=None, cand_lon=None,
                                          type_ok=True, meters=t["meters"])[0] != "no"][:3]
                cands = [c for c in (wikipedia_candidate(t["title"], t["meters"], fetch.wikipedia_lead(t["title"])) for t in titles) if c]
                if known_title:  # the title came from a confident Wikidata match: trust it
                    cands = [{**c, "names": [venue["name"], *c["names"]], "type_texts": [*c["type_texts"], "venue"]} for c in cands]
            else:
                cands = [osm_candidate(el) for el in fetch.osm_nearby(venue["latitude"], venue["longitude"])]
        except ApiError as err:
            # A source that's down (e.g. Overpass timing out) is skipped for this venue, and the
            # venue isn't marked checked unless resolved, so it's retried on the next run.
            failed.append(f"{source} (HTTP {err.status})")
            continue
        result = evaluate(venue, cands)
        if result["match"]:
            m = result["match"]
            stmts.append(details_update(venue, m, now))
            known_title = known_title or m.get("wikipedia_title")
            if m.get("capacity_raw") or m.get("capacity_values"):
                stmts.append(observation(venue["id"], m, result["capacity"], now))
            cap = result["capacity"]
            if cap and cap["confidence"] in ("high", "medium"):
                resolved = (m, cap)
                break
            if cap:
                low_caps.append((m, cap))
        else:
            reviews += [{**c, "source": source} for c in result["review"]]
    if resolved:
        m, cap = resolved
        if venue["capacity"] is not None and abs(cap["capacity"] - venue["capacity"]) <= DISAGREE * venue["capacity"]:
            outcome = "same"
        else:
            outcome = "resolved" if venue["capacity"] is None else "review"
        stmts += capacity_statements(venue, m, cap, now)
        if outcome == "resolved":
            # An earlier run may have queued this venue for review; it's settled now.
            stmts.append(("UPDATE match_review SET status = 'accepted', resolved_at = ?, last_updated = ?,"
                          " resolution_note = 'resolved automatically by a later enrichment run'"
                          " WHERE kind = 'venue_match' AND source = 'enrichment' AND external_id = ? AND status = 'open'",
                          (now, now, str(venue["id"]))))
    elif low_caps:
        m, cap = low_caps[0]
        stmts.append(review("venue_capacity", m["source"], str(venue["id"]), venue["name"], {
            "reason": f"low-confidence capacity: {cap['reason']}", "raw": m.get("capacity_raw"), "parsed": cap["capacity"], "url": m.get("url")}))
        outcome = "review"
    elif reviews:
        stmts.append(review("venue_match", "enrichment", str(venue["id"]), venue["name"], {
            "reason": "nearby entries but no confident match (needs 300 m + same name + venue type)",
            "candidates": [{"source": c["source"], "id": c["source_id"], "names": [n for n in c["names"] if n][:2],
                            "meters": round(c["meters"]) if c["meters"] is not None else None, "verdict": c["verdict"],
                            "capacity": c.get("capacity_raw"), "url": c.get("url")} for c in reviews[:8]]}))
        outcome = "review"
    else:
        outcome = "unresolved"
    if failed and not resolved:
        return stmts, "source_error"  # not stamped: retried next run once the source is back
    if len(sources) < len(SOURCES) and not resolved:
        return stmts, "deferred"  # some sources were skipped: not stamped, so the next full run tries them
    stmts.append(("UPDATE venues SET capacity_checked_at = ? WHERE id = ?", (now, venue["id"])))
    return stmts, outcome


def ticketmaster_details(fetch: Fetch, venue: dict, now: str) -> list:
    data = fetch.ticketmaster_venue(venue["ticketmaster_id"]) if venue["ticketmaster_id"] else None
    if data is None:
        return []
    return [("UPDATE venues SET timezone = COALESCE(?, timezone), url = COALESCE(?, url), ticketmaster_checked_at = ? WHERE id = ?",
             (data.get("timezone"), data.get("url"), now, venue["id"]))]


def capacity_coverage(db: Database) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    v = db.query("SELECT COUNT(DISTINCT v.id) AS total, COUNT(DISTINCT CASE WHEN v.capacity IS NOT NULL THEN v.id END) AS filled"
                 " FROM venues v JOIN events e ON e.venue_id = v.id AND e.event_date >= ?", (today,))[0]
    e = db.query("SELECT COUNT(*) AS total, SUM(CASE WHEN v.capacity IS NOT NULL THEN 1 ELSE 0 END) AS filled"
                 " FROM events e LEFT JOIN venues v ON v.id = e.venue_id WHERE e.event_date >= ?", (today,))[0]
    return {"venues": v["total"] or 0, "venues_filled": v["filled"] or 0, "events": e["total"] or 0, "events_filled": e["filled"] or 0}


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    stamp = now_iso()
    stats["rows_written"] += apply_manual(db, stamp)
    fetch = Fetch()
    outcomes: dict[str, int] = {}
    limit = int(env("VENUE_LIMIT") or MAX_VENUES_PER_RUN)  # override for a one-off backfill
    sources = enabled_sources()
    try:
        for venue in due(db, now, limit):
            stmts, outcome = enrich_one(fetch, venue, stamp, sources)
            if venue["ticketmaster_checked_at"] is None:
                stmts += ticketmaster_details(fetch, venue, stamp)
            db.batch(stmts)
            stats["rows_written"] += len(stmts)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            done = sum(outcomes.values())
            if done % 50 == 0:
                print(f"  venues: {done} processed so far {json.dumps(outcomes)}", flush=True)
    finally:
        stats["api_calls"] += fetch.calls
    cov = capacity_coverage(db)
    print(f"  Venues: {sum(outcomes.values())} processed {json.dumps(outcomes)} ({fetch.calls} calls);"
          f" capacity known for {cov['venues_filled']}/{cov['venues']} venues with upcoming events,"
          f" {cov['events_filled']}/{cov['events']} upcoming events")
