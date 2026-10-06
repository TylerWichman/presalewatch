"""Ticketmaster -> D1: events, venues (with coordinates), artists (with MusicBrainz links),
billing order, presales, and a status-change history.

Terms: Ticketmaster allows storing event content "for reasonable periods" to provide the
service, so this stores the facts scoring and tour history need, not images or long text.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ticketmaster
from common import parse_utc, safe_url
from ingest.db import D1_MAX_PARAMS, Database, id_map, now_iso, review, rows_in, set_mbid, upsert
from ingest.resolve import event_status, is_mbid, name_key, parse_ticket_limit

SOURCE = "ticketmaster"
RECHECK_DAYS = 30   # keep re-checking events this long after on-sale, when sellouts happen


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tour_name(event_name: str, headliner: str | None) -> str | None:
    """Ticketmaster has no tour field. Use the event title only when it names a tour."""
    if "tour" not in event_name.lower():
        return None
    return event_name.strip() or None


def parse(ev: dict) -> dict | None:
    """One raw Discovery event -> rows for each table. Pure, so it's tested on the fixture."""
    if ev.get("test") or not ev.get("id"):
        return None
    emb = ev.get("_embedded", {})
    v = (emb.get("venues") or [{}])[0]
    attractions = emb.get("attractions") or []
    start = ev.get("dates", {}).get("start", {})
    public = ev.get("sales", {}).get("public", {})
    face_min, face_max, fee_included = ticketmaster.face_range(ev)
    loc = v.get("location") or {}

    venue = None
    if v.get("id"):
        venue = {
            "ticketmaster_id": v["id"], "name": (v.get("name") or "").strip() or "Venue TBA",
            "address": (v.get("address") or {}).get("line1"), "city": (v.get("city") or {}).get("name"),
            "state": (v.get("state") or {}).get("stateCode"), "postal_code": v.get("postalCode"),
            "country": (v.get("country") or {}).get("countryCode"),
            "latitude": _float(loc.get("latitude")), "longitude": _float(loc.get("longitude")),
            "primary_platform": "ticketmaster",
        }

    artists = []
    for pos, a in enumerate(attractions):
        if not a.get("id") or not a.get("name"):
            continue
        links = a.get("externalLinks") or {}
        mb = next((l.get("id") for l in links.get("musicbrainz") or [] if is_mbid(l.get("id"))), None)
        artists.append({"ticketmaster_id": a["id"], "name": a["name"].strip(), "position": pos,
                        "mbid": mb.lower() if mb else None, "lastfm_name": ticketmaster.lastfm_name(a)})

    presales = []
    for p in ev.get("sales", {}).get("presales") or []:
        s, e = parse_utc(p.get("startDateTime")), parse_utc(p.get("endDateTime"))
        if not s or not e:
            continue
        name = (p.get("name") or "Presale").strip()
        kind, access = ticketmaster.presale_type(name)
        presales.append({
            "name": name, "presale_type": kind, "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"), "access_requirements": access,
            "description": (p.get("shortDescription") or p.get("description") or "").strip() or None,
            "url": safe_url(p.get("url")), "link_text": (p.get("linkDescription") or "").strip() or None,
        })

    name = (ev.get("name") or "").strip()
    event = {
        "ticketmaster_id": ev["id"], "name": name or "Event",
        "tour_name": tour_name(name, artists[0]["name"] if artists else None),
        "event_date": None if start.get("dateTBA") or start.get("dateTBD") else start.get("localDate"),
        "event_time": None if start.get("noSpecificTime") or start.get("timeTBA") else start.get("localTime"),
        "starts_at": start.get("dateTime"),
        "status": event_status(ev.get("dates", {}).get("status", {}).get("code")),
        "face_min": face_min, "face_max": face_max, "face_currency": "USD" if face_min is not None else None,
        "face_fee_included": int(fee_included) if face_min is not None else None,
        "dynamic_pricing": int(ticketmaster.dynamic_pricing(ev)),
        "onsale_at": None if public.get("startTBD") or public.get("startTBA") else public.get("startDateTime"),
        "ticket_limit": parse_ticket_limit((ev.get("ticketLimit") or {}).get("info")),
        "url": safe_url(ev.get("url")),
    }
    return {"event": event, "venue": venue, "artists": artists, "presales": presales}


def ids_to_recheck(db: Database, now: datetime) -> list[str]:
    """Known events that went on sale in the last RECHECK_DAYS and haven't happened yet."""
    since = (now - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.query(
        "SELECT ticketmaster_id FROM events WHERE ticketmaster_id IS NOT NULL AND onsale_at >= ? AND onsale_at <= ?"
        " AND (event_date IS NULL OR event_date >= ?) AND COALESCE(status, 'unknown') <> 'cancelled'",
        (since, now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")))
    return [r["ticketmaster_id"] for r in rows]


def write(db: Database, parsed: list[dict], now: str) -> int:
    """Upsert everything and log status changes. Returns statements written."""
    stmts = []
    venues = {p["venue"]["ticketmaster_id"]: p["venue"] for p in parsed if p["venue"]}
    for v in venues.values():
        stmts.append(upsert("venues", ("ticketmaster_id",), {**v, "name_key": name_key(v["name"]), "source": SOURCE, "last_updated": now}))
    artists = {a["ticketmaster_id"]: a for p in parsed for a in p["artists"]}
    for a in artists.values():
        stmts.append(upsert("artists", ("ticketmaster_id",), {
            "ticketmaster_id": a["ticketmaster_id"], "name": a["name"], "name_key": name_key(a["name"]),
            "lastfm_name": a["lastfm_name"], "source": SOURCE, "last_updated": now}, update=("name", "name_key", "lastfm_name", "last_updated")))
    db.batch(stmts)
    written = len(stmts)

    venue_ids = id_map(db, "venues", "ticketmaster_id", list(venues))
    artist_ids = id_map(db, "artists", "ticketmaster_id", list(artists))
    tm_ids = [p["event"]["ticketmaster_id"] for p in parsed]

    # The status each event had before this run, to log changes.
    before = {r["ticketmaster_id"]: r for r in rows_in(db, "events", "id, ticketmaster_id, status, last_seen_at", "ticketmaster_id", tm_ids)}

    stmts = []
    for p in parsed:
        e = p["event"]
        row = {**e, "venue_id": venue_ids.get(p["venue"]["ticketmaster_id"]) if p["venue"] else None,
               "first_seen_at": now, "last_seen_at": now, "source": SOURCE, "last_updated": now}
        update = tuple(c for c in row if c not in ("first_seen_at", "ticketmaster_id"))
        # last_seen_at changes every run, so it doesn't count as a change: an event whose details
        # are the same isn't rewritten (5 rows with its indexes). It's refreshed below instead,
        # with one write per event, since last_seen_at has no index.
        stmts.append(upsert("events", ("ticketmaster_id",), row, update=update, volatile=("last_updated", "last_seen_at")))
    for i in range(0, len(tm_ids), D1_MAX_PARAMS - 10):
        chunk = tm_ids[i:i + D1_MAX_PARAMS - 10]
        stmts.append((f"UPDATE events SET last_seen_at = ? WHERE ticketmaster_id IN ({', '.join('?' for _ in chunk)})"
                      " AND last_seen_at IS NOT ?", (now, *chunk, now)))
    db.batch(stmts)
    written += len(stmts)
    event_ids = id_map(db, "events", "ticketmaster_id", tm_ids)

    stmts = []
    for p in parsed:
        eid = event_ids.get(p["event"]["ticketmaster_id"])
        if eid is None:
            continue
        prev = before.get(p["event"]["ticketmaster_id"])
        status = p["event"]["status"]
        if prev is None or prev["status"] != status:
            stmts.append(("INSERT INTO event_status_history (event_id, status, previous_status, previous_seen_at, seen_at, source, last_updated)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (eid, status, prev["status"] if prev else None, prev["last_seen_at"] if prev else None, now, SOURCE, now)))
        for a in p["artists"]:
            aid = artist_ids.get(a["ticketmaster_id"])
            if aid is not None:
                stmts.append(upsert("event_artists", ("event_id", "artist_id"),
                                    {"event_id": eid, "artist_id": aid, "position": a["position"], "source": SOURCE, "last_updated": now}))
        for ps in p["presales"]:
            stmts.append(upsert("presales", ("event_id", "name", "starts_at"), {**ps, "event_id": eid, "source": SOURCE, "last_updated": now}))
    stmts += mbid_statements(db, artists, artist_ids, now)
    db.batch(stmts)
    return written + len(stmts)


def mbid_statements(db: Database, artists: dict, artist_ids: dict, now: str) -> list:
    """Set MBIDs from Ticketmaster's MusicBrainz links. If another artist row already holds that
    MBID (Ticketmaster sometimes has two attractions for one act), send it to review instead."""
    wanted = {a["mbid"]: tm for tm, a in artists.items() if a["mbid"]}
    if not wanted:
        return []
    taken = id_map(db, "artists", "mbid", list(wanted))
    current = {r["id"]: r["mbid"] for r in rows_in(db, "artists", "id, mbid", "id", list(artist_ids.values()))}
    out = []
    for mbid, tm in wanted.items():
        aid = artist_ids.get(tm)
        if aid is None or current.get(aid):
            continue
        if mbid in taken and taken[mbid] != aid:
            out.append(review("artist_match", SOURCE, tm, artists[tm]["name"],
                              {"reason": "MusicBrainz ID already belongs to another artist row", "mbid": mbid}, taken[mbid]))
            continue
        out.append(set_mbid(aid, mbid, "ticketmaster", now))
    return out


def run(db: Database, stats: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    raw, calls = ticketmaster.fetch_raw(now)
    recheck = [i for i in ids_to_recheck(db, now) if i not in raw]
    if recheck:
        raw.update(ticketmaster.fetch_by_ids(recheck))
        calls += -(-len(recheck) // 50)
    parsed = [p for p in (parse(ev) for ev in raw.values()) if p]
    stats["api_calls"] += calls
    stats["rows_written"] += write(db, parsed, now_iso(now))
    print(f"  Ticketmaster: {len(parsed)} events ({len(recheck)} re-checked for status), {calls} API calls")
