"""Ticketmaster Discovery API: upcoming US concert presales, face values, venues, and tour sizes.

Used by pipeline.py (step 1 of the pipeline).
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from datetime import datetime, timedelta

from common import ApiError, Http, env, iso, norm, parse_utc, safe_url

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 200          # API max
MAX_DEEP = 1000          # API rejects size * page >= 1000
MIN_INTERVAL = 0.25      # seconds between requests (limit is 5/sec)

# Presales usually run before the public on-sale, but some (card/venue
# presales) overlap it, so also scan events that went on sale recently.
LOOKBACK_DAYS = 5
MAX_CURSOR_STEPS = 6     # safety cap on forward paging (~6000 events)

LASTFM_ARTIST_RE = re.compile(r"last\.fm/music/([^/?#]+)")


def lastfm_name(attraction: dict) -> str | None:
    """The artist's Last.fm name from Ticketmaster's external links, when it has one."""
    for link in (attraction.get("externalLinks") or {}).get("lastfm") or []:
        m = LASTFM_ARTIST_RE.search(link.get("url") or "")
        if m:
            name = urllib.parse.unquote_plus(m.group(1)).strip()
            if name:
                return name
    return None

# (type, pattern on the normalized presale name, where the code comes from).
# Order matters: the first match wins.
PRESALE_TYPES = [
    ("Platinum", r"platinum", "Dynamic-priced tickets, no code"),
    ("VIP", r"\bvip\b|package", "VIP package purchase, no code"),
    ("Amex", r"\bamex\b|american express", "American Express card, no code"),
    ("Citi", r"\bciti\b", "Citi card, no code"),
    ("Spotify", r"spotify", "Spotify email to top listeners"),
    ("Card / Rewards", r"capital one|chase|mastercard|visa|verizon|t mobile|bank|credit union|cardmember|rewards|perks",
     "Card or loyalty program membership"),
    ("Live Nation", r"live nation|\blnp\b", "Live Nation code (app or email)"),
    ("Ticketmaster", r"ticketmaster|verified fan|\btm\b|mobile app", "Ticketmaster account or Verified Fan invite"),
    ("Artist", r"artist|fan club|discord|official|\bfan\b", "Artist newsletter or fan club sign-up"),
    ("Radio / Local", r"radio|local|k love|iheart|station", "Local radio or partner promotion"),
    ("Venue", r"venue|arena|theat|center|centre|coliseum|dome|amphithea|hall|club|casino|stadium|ballroom",
     "Venue newsletter or email list"),
    ("Promoter", r"promoter|events|presents|productions|previous buyer", "Promoter email list"),
]
OTHER_TYPE = ("Other", "See presale details")


def presale_type(name: str) -> tuple[str, str]:
    """Classify a presale name into (type, code source)."""
    text = norm(name)
    for kind, pattern, source in PRESALE_TYPES:
        if re.search(pattern, text):
            return kind, source
    return OTHER_TYPE


def fetch_pages(client: Http, api_key: str, filters: dict) -> tuple[list[dict], int]:
    """Fetch up to the deep-paging limit for one query."""
    events: list[dict] = []
    total = 0
    page = 0
    while True:
        query = urllib.parse.urlencode({
            "countryCode": "US",
            "classificationName": "music",
            "sort": "onSaleStartDate,asc",
            "size": PAGE_SIZE,
            "page": page,
            **filters,
            "apikey": api_key,
        })
        data = client.get_json(f"{API_URL}?{query}")
        info = data.get("page", {})
        total = info.get("totalElements", 0)
        events.extend(data.get("_embedded", {}).get("events", []))
        page += 1
        if page >= info.get("totalPages", 0) or (page + 1) * PAGE_SIZE > MAX_DEEP:
            break
    return events, total


def public_onsale_date(ev: dict) -> str | None:
    value = ev.get("sales", {}).get("public", {}).get("startDateTime")
    return value[:10] if value else None


def fetch_raw(now: datetime) -> tuple[dict[str, dict], int]:
    """All music events with an on-sale from LOOKBACK_DAYS ago onward, keyed by id."""
    api_key = env("TM_API_KEY")
    if not api_key:
        sys.exit("TM_API_KEY is not set (env var or .env file).")
    client = Http("Ticketmaster", MIN_INTERVAL)
    raw: dict[str, dict] = {}

    def add(events: list[dict]) -> None:
        for ev in events:
            raw.setdefault(ev["id"], ev)

    try:
        # Events that went on sale in the last few days (overlapping presales).
        for back in range(LOOKBACK_DAYS, 0, -1):
            day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
            events, total = fetch_pages(client, api_key, {"onsaleOnStartDate": day})
            print(f"  on-sale {day}: {len(events)}/{total} events")
            add(events)

        # Events going on sale today or later. Results are sorted by on-sale
        # date, so when a query hits the paging cap, restart from the last
        # on-sale date seen.
        after = now.strftime("%Y-%m-%d")
        for _ in range(MAX_CURSOR_STEPS):
            events, total = fetch_pages(client, api_key, {"onsaleOnAfterStartDate": after})
            print(f"  on-sale {after} onward: {len(events)}/{total} events")
            add(events)
            if len(events) >= total or not events:
                break
            last = public_onsale_date(events[-1]) or after
            if last <= after:  # a single day with >1000 events; skip ahead
                last = (datetime.fromisoformat(after) + timedelta(days=1)).strftime("%Y-%m-%d")
            after = last
    except ApiError as err:
        sys.exit(str(err))
    return raw, client.calls


def fetch_by_ids(ids: list[str]) -> dict[str, dict]:
    """Raw events by ID, in batches (used to backfill face values after presales end)."""
    api_key = env("TM_API_KEY")
    client = Http("Ticketmaster", MIN_INTERVAL)
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        query = urllib.parse.urlencode({"id": ",".join(ids[i:i + 50]), "size": 50, "apikey": api_key})
        data = client.get_json(f"{API_URL}?{query}")
        for ev in data.get("_embedded", {}).get("events", []):
            out[ev["id"]] = ev
    return out


def price_fields(ev: dict) -> dict:
    """The events-table fields that can change after the presale window."""
    face_min, face_max, fee_included = face_range(ev)
    public = ev.get("sales", {}).get("public", {})
    return {
        "face_min": face_min,
        "face_max": face_max,
        "fee_included": fee_included,
        "dynamic_pricing_flag": dynamic_pricing(ev),
        "onsale_date": None if public.get("startTBD") or public.get("startTBA") else public.get("startDateTime"),
    }


def pick_image(images: list[dict]) -> str | None:
    best = None
    for img in images or []:
        if img.get("ratio") != "16_9" or not img.get("url", "").startswith("https://"):
            continue
        # Prefer something around 640px wide: sharp on phones, not huge.
        score = abs((img.get("width") or 0) - 640)
        if best is None or score < best[0]:
            best = (score, img["url"])
    return best[1] if best else None


def face_range(ev: dict) -> tuple[float | None, float | None, bool]:
    """Standard (non-platinum) USD price range, and whether it already includes fees."""
    lows, highs, fee_included = [], [], False
    for pr in ev.get("priceRanges") or []:
        kind = (pr.get("type") or "").lower()
        if not kind.startswith("standard") or pr.get("currency", "USD") != "USD":
            continue
        if isinstance(pr.get("min"), (int, float)) and pr["min"] > 0:
            lows.append(float(pr["min"]))
        if isinstance(pr.get("max"), (int, float)) and pr["max"] > 0:
            highs.append(float(pr["max"]))
        fee_included = fee_included or "including fees" in kind
    lo = min(lows) if lows else (min(highs) if highs else None)
    hi = max(highs) if highs else lo
    return lo, hi, fee_included


def dynamic_pricing(ev: dict) -> bool:
    """Ticketmaster doesn't flag dynamic pricing directly; infer it from Platinum sales and notes."""
    names = " ".join(p.get("name") or "" for p in ev.get("sales", {}).get("presales") or [])
    notes = " ".join(ev.get(k) or "" for k in ("info", "pleaseNote"))
    text = norm(names + " " + notes)
    return "platinum" in text or "dynamic pric" in text or any(
        "platinum" in (pr.get("type") or "").lower() for pr in ev.get("priceRanges") or [])


def parse_event(ev: dict, now: datetime) -> dict | None:
    """Clean one raw event. Returns None unless it has a presale that hasn't ended."""
    if ev.get("test"):
        return None
    sales = ev.get("sales", {})
    presales = []
    for p in sales.get("presales") or []:
        start, end = parse_utc(p.get("startDateTime")), parse_utc(p.get("endDateTime"))
        if not start or not end or end <= now:
            continue
        name = (p.get("name") or "Presale").strip()
        kind, code_source = presale_type(name)
        presales.append({
            "name": name,
            "type": kind,
            "code_source": code_source,
            "description": (p.get("shortDescription") or p.get("description") or "").strip(),
            "start": iso(start),
            "end": iso(end),
            "url": safe_url(p.get("url")),
            "link_text": (p.get("linkDescription") or "").strip() or None,
        })
    if not presales:
        return None
    presales.sort(key=lambda p: (p["start"], p["end"], p["name"]))

    emb = ev.get("_embedded", {})
    venue = (emb.get("venues") or [{}])[0]
    attraction = (emb.get("attractions") or [{}])[0]
    start = ev.get("dates", {}).get("start", {})
    artist = attraction.get("name") or ev.get("name", "").strip()

    tour_dates = (attraction.get("upcomingEvents") or {}).get("_total")

    return {
        "event": {
            "event_id": ev["id"],
            "name": ev.get("name", "").strip(),
            "artist": artist,
            "artist_id": attraction.get("id") or f"name:{norm(artist)}",
            "url": safe_url(ev.get("url")),
            "image": pick_image(ev.get("images")),
            "venue": venue.get("name"),
            "venue_id": venue.get("id"),
            "city": venue.get("city", {}).get("name"),
            "state": venue.get("state", {}).get("stateCode"),
            "market_ids": ";".join(str(m.get("id")) for m in venue.get("markets") or [] if m.get("id")),
            "event_date": None if start.get("dateTBA") or start.get("dateTBD") else start.get("localDate"),
            "event_time": None if start.get("noSpecificTime") or start.get("timeTBA") else start.get("localTime"),
            "presale_start": presales[0]["start"],
            "presale_type": presales[0]["type"],
            **price_fields(ev),
        },
        "presales": presales,
        "artist": {
            "artist_id": attraction.get("id") or f"name:{norm(artist)}",
            "name": artist,
            "lastfm_lookup": lastfm_name(attraction),
            "tour_date_count": tour_dates if isinstance(tour_dates, int) and tour_dates > 0 else None,
        },
        "venue": {
            "venue_id": venue.get("id"),
            "name": venue.get("name"),
            "city": venue.get("city", {}).get("name"),
            "state": venue.get("state", {}).get("stateCode"),
            "market_ids": [str(m.get("id")) for m in venue.get("markets") or [] if m.get("id")],
        },
    }
