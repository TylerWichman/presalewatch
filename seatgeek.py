"""SeatGeek API: live resale stats (listing count, low / median / average price).

Needs SEATGEEK_CLIENT_ID (SEATGEEK_CLIENT_SECRET is optional). Each Ticketmaster
event is matched to a SeatGeek event once, by performer search on the event
date plus a city check; after that its stats are pulled in batches by ID.
"""

from __future__ import annotations

import urllib.parse
from datetime import date, timedelta

from common import Http, env, norm

API_URL = "https://api.seatgeek.com/2/events"
BATCH = 100


class SeatGeek:
    def __init__(self, client_id: str, client_secret: str = ""):
        self.http = Http("SeatGeek", 0.3)
        self.auth = {"client_id": client_id}
        if client_secret:
            self.auth["client_secret"] = client_secret

    @classmethod
    def from_env(cls) -> "SeatGeek | None":
        cid = env("SEATGEEK_CLIENT_ID")
        return cls(cid, env("SEATGEEK_CLIENT_SECRET")) if cid else None

    def _get(self, params: dict) -> dict:
        return self.http.get_json(f"{API_URL}?{urllib.parse.urlencode({**params, **self.auth})}")

    def find(self, artist: str, event_date: str, city: str | None, venue: str | None) -> str | None:
        """SeatGeek event ID for an artist's show on a local date in a city, or None."""
        day = date.fromisoformat(event_date)
        data = self._get({
            "q": artist,
            "datetime_local.gte": day.isoformat(),
            "datetime_local.lt": (day + timedelta(days=1)).isoformat(),
            "per_page": 25,
        })
        want_city, want_venue = norm(city), norm(venue)
        for ev in data.get("events", []):
            v = ev.get("venue") or {}
            performers = " ".join(norm(p.get("name")) for p in ev.get("performers") or [])
            if norm(artist) not in performers and norm(artist) not in norm(ev.get("title")):
                continue
            if want_city and norm(v.get("city")) == want_city:
                return str(ev["id"])
            if want_venue and norm(v.get("name")) == want_venue:
                return str(ev["id"])
        return None

    def stats(self, ids: list[str]) -> dict[str, dict]:
        """Resale stats keyed by SeatGeek event ID."""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            data = self._get({"id": ",".join(chunk), "per_page": len(chunk)})
            for ev in data.get("events", []):
                s = ev.get("stats") or {}
                out[str(ev["id"])] = {
                    "listing_count": s.get("listing_count"),
                    "low": s.get("lowest_price"),
                    "median": s.get("median_price"),
                    "avg": s.get("average_price"),
                }
        return out
