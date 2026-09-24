"""Fetch upcoming US concert presales from the Ticketmaster Discovery API.

Writes a cleaned list to data/presales.json. Reads the API key from the
TM_API_KEY environment variable, falling back to a local .env file.

Usage:
    python fetch_presales.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "presales.json"

API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 200          # API max
MAX_DEEP = 1000          # API rejects size * page >= 1000
MIN_INTERVAL = 0.25      # seconds between requests (limit is 5/sec)
MAX_RETRIES = 5

# Presales usually run before the public on-sale, but some (card/venue
# presales) overlap it, so also scan events that went on sale recently.
LOOKBACK_DAYS = 5
MAX_CURSOR_STEPS = 6     # safety cap on forward paging (~6000 events)


def load_api_key() -> str:
    key = os.environ.get("TM_API_KEY", "").strip()
    if not key:
        env_path = ROOT / ".env"
        if env_path.exists():
            # utf-8-sig: tolerate the BOM that Windows editors add
            for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == "TM_API_KEY":
                    key = value.strip().strip('"').strip("'")
    if not key:
        sys.exit("TM_API_KEY is not set (env var or .env file).")
    return key


class Client:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.last_call = 0.0
        self.calls = 0

    def get(self, params: dict) -> dict:
        query = urllib.parse.urlencode({**params, "apikey": self.api_key})
        url = f"{API_URL}?{query}"
        for attempt in range(MAX_RETRIES):
            wait = MIN_INTERVAL - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                if err.code == 429 or err.code >= 500:
                    delay = float(err.headers.get("Retry-After") or 2 ** (attempt + 1))
                    print(f"  HTTP {err.code}, retrying in {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                # Never echo the URL: it contains the key.
                body = err.read().decode("utf-8", "replace")[:300]
                raise SystemExit(f"HTTP {err.code} from Ticketmaster: {body}") from None
            except urllib.error.URLError as err:
                delay = 2 ** (attempt + 1)
                print(f"  network error ({err.reason}), retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
        raise SystemExit("Ticketmaster API kept failing; giving up.")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_pages(client: Client, filters: dict) -> tuple[list[dict], int]:
    """Fetch up to the deep-paging limit for one query."""
    events: list[dict] = []
    total = 0
    page = 0
    while True:
        data = client.get({
            "countryCode": "US",
            "classificationName": "music",
            "sort": "onSaleStartDate,asc",
            "size": PAGE_SIZE,
            "page": page,
            **filters,
        })
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


def pick_image(images: list[dict]) -> str | None:
    best = None
    for img in images or []:
        if img.get("ratio") != "16_9" or not img.get("url", "").startswith("https://"):
            continue
        width = img.get("width") or 0
        # Prefer something around 640px wide: sharp on phones, not huge.
        score = abs(width - 640)
        if best is None or score < best[0]:
            best = (score, img["url"])
    return best[1] if best else None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_url(value: str | None) -> str | None:
    return value if value and value.startswith(("https://", "http://")) else None


def clean_event(ev: dict, now: datetime) -> dict | None:
    if ev.get("test"):
        return None
    sales = ev.get("sales", {})
    presales = []
    for p in sales.get("presales") or []:
        start, end = parse_utc(p.get("startDateTime")), parse_utc(p.get("endDateTime"))
        if not start or not end or end <= now:
            continue
        presales.append({
            "name": (p.get("name") or "Presale").strip(),
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
    attractions = emb.get("attractions") or []
    start = ev.get("dates", {}).get("start", {})
    public = sales.get("public", {})

    return {
        "id": ev["id"],
        "name": ev.get("name", "").strip(),
        "artist": (attractions[0].get("name") if attractions else None) or ev.get("name", "").strip(),
        "url": safe_url(ev.get("url")),
        "image": pick_image(ev.get("images")),
        "venue": venue.get("name"),
        "city": venue.get("city", {}).get("name"),
        "state": venue.get("state", {}).get("stateCode"),
        "event_date": start.get("localDate"),
        "event_time": None if start.get("noSpecificTime") or start.get("timeTBA") else start.get("localTime"),
        "event_tba": bool(start.get("dateTBA") or start.get("dateTBD")),
        "public_onsale": None if public.get("startTBD") or public.get("startTBA") else public.get("startDateTime"),
        "presales": presales,
    }


def main() -> None:
    client = Client(load_api_key())
    now = datetime.now(timezone.utc).replace(microsecond=0)

    raw: dict[str, dict] = {}

    def add(events: list[dict]) -> None:
        for ev in events:
            raw.setdefault(ev["id"], ev)

    # Events that went on sale in the last few days (overlapping presales).
    for back in range(LOOKBACK_DAYS, 0, -1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        events, total = fetch_pages(client, {"onsaleOnStartDate": day})
        print(f"on-sale {day}: {len(events)}/{total} events")
        add(events)

    # Events going on sale today or later. Results are sorted by on-sale
    # date, so when a query hits the paging cap, restart from the last
    # on-sale date seen.
    after = now.strftime("%Y-%m-%d")
    for _ in range(MAX_CURSOR_STEPS):
        events, total = fetch_pages(client, {"onsaleOnAfterStartDate": after})
        print(f"on-sale {after} onward: {len(events)}/{total} events")
        add(events)
        if len(events) >= total or not events:
            break
        last = public_onsale_date(events[-1]) or after
        if last <= after:  # a single day with >1000 events; skip ahead
            last = (datetime.fromisoformat(after) + timedelta(days=1)).strftime("%Y-%m-%d")
        after = last

    cleaned = [c for c in (clean_event(ev, now) for ev in raw.values()) if c]
    cleaned.sort(key=lambda e: (e["presales"][0]["start"], e["artist"].lower()))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at": iso(now),
        "event_count": len(cleaned),
        "presale_count": sum(len(e["presales"]) for e in cleaned),
        "events": cleaned,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(raw)} events scanned, {len(cleaned)} with upcoming presales "
          f"({client.calls} API calls) -> {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
