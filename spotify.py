"""Spotify Web API: artist popularity (0-100) and follower counts.

Uses the client-credentials flow (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).
The artist ID comes from Ticketmaster's attraction links when present;
otherwise we search by name and accept only an exact name match.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.parse

from common import ApiError, Http, env, norm

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"


class Spotify:
    def __init__(self, client_id: str, client_secret: str):
        self.http = Http("Spotify", 0.1)
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        token = self.http.get_json(
            TOKEN_URL,
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        self.headers = {"Authorization": f"Bearer {token['access_token']}"}

    @classmethod
    def from_env(cls) -> "Spotify | None":
        cid, secret = env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET")
        if not (cid and secret):
            return None
        try:
            return cls(cid, secret)
        except (ApiError, KeyError, json.JSONDecodeError) as err:
            print(f"  Spotify auth failed, skipping popularity signals: {err}", file=sys.stderr)
            return None

    def artist(self, spotify_id: str) -> dict:
        return self.http.get_json(f"{API_URL}/artists/{spotify_id}", headers=self.headers)

    def search(self, name: str) -> dict | None:
        query = urllib.parse.urlencode({"q": name, "type": "artist", "limit": 10})
        data = self.http.get_json(f"{API_URL}/search?{query}", headers=self.headers)
        target = norm(name)
        matches = [a for a in data.get("artists", {}).get("items", []) if norm(a.get("name")) == target]
        if not matches:
            return None
        # Several artists can share a name; the most-followed one is the likely headliner.
        return max(matches, key=lambda a: (a.get("followers") or {}).get("total") or 0)


def artist_stats(sp: Spotify, name: str, spotify_id: str | None) -> dict:
    """Returns spotify_id, spotify_popularity, spotify_followers (values may be None)."""
    data = sp.artist(spotify_id) if spotify_id else sp.search(name)
    if not data:
        return {"spotify_id": spotify_id, "spotify_popularity": None, "spotify_followers": None}
    popularity = data.get("popularity")
    followers = (data.get("followers") or {}).get("total")
    return {
        "spotify_id": data.get("id") or spotify_id,
        "spotify_popularity": popularity if isinstance(popularity, int) else None,
        "spotify_followers": followers if isinstance(followers, int) else None,
    }
