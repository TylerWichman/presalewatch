"""Last.fm API: artist listener and play counts, the demand signal for estimated edges.

Needs LASTFM_API_KEY. Uses artist.getInfo, looking up the Ticketmaster artist
name. Last.fm's autocorrect fixes small spelling differences. Artists Last.fm
doesn't know (tribute acts, "X Presents Y" billings) come back as not found,
and that answer is cached like any other so they aren't retried every run.

Last.fm asks API clients to stay under about 5 requests per second, and its
terms require crediting Last.fm where the data is shown (the page footer does).
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

from common import ApiError, Http, env

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "PresaleWatch/1.0 (+https://pouchit.net)"
# Last.fm error codes that mean "no such artist", as opposed to a key or rate problem.
NOT_FOUND_CODES = {6}
FATAL_CODES = {4, 9, 10, 26, 29}  # auth failed, bad session, invalid key, suspended key, rate limit


class LastFm:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.http = Http("Last.fm", 0.25)

    @classmethod
    def from_env(cls) -> "LastFm | None":
        key = env("LASTFM_API_KEY")
        return cls(key) if key else None

    def artist_info(self, name: str) -> dict | None:
        """The artist object, or None if Last.fm doesn't know the artist."""
        query = urllib.parse.urlencode({
            "method": "artist.getinfo", "artist": name, "autocorrect": 1, "api_key": self.api_key, "format": "json",
        })
        try:
            data = self.http.get_json(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT})
        except ApiError as err:
            # Last.fm answers some errors with HTTP 4xx and a JSON error code in the body.
            if err.status in (400, 404) and '"error":6' in str(err).replace(" ", ""):
                return None
            raise
        code = data.get("error")
        if code in NOT_FOUND_CODES:
            return None
        if code is not None:
            raise ApiError("Last.fm", 429 if code == 29 else 403 if code in FATAL_CODES else 400, f"error {code}")
        return data.get("artist") or None


def to_int(value) -> int | None:
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def same_artist(a: str, b: str) -> bool:
    """Same name apart from case, accents, punctuation, "&" vs "and", and a leading "The"."""
    def key(s: str) -> str:
        s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
        s = re.sub(r"&", " and ", s.lower())
        s = re.sub(r"^\s*the\s+", "", s)
        return re.sub(r"[\W_]+", "", s)
    return key(a) == key(b)


def artist_stats(lf: LastFm, name: str, trusted: bool = False) -> dict:
    """Returns lastfm_name, lastfm_listeners, lastfm_playcount (all None if not found).

    Autocorrect can jump to a different act ("Face 2 Face" the tribute show became the band
    "Face to Face"), so a corrected name is only accepted when it's the same name written
    differently. trusted=True skips that check for names from Ticketmaster's own Last.fm link.
    """
    artist = lf.artist_info(name)
    if artist and not trusted and not same_artist(name, artist.get("name") or ""):
        artist = None
    if not artist:
        return {"lastfm_name": None, "lastfm_listeners": None, "lastfm_playcount": None}
    stats = artist.get("stats") or {}
    listeners = to_int(stats.get("listeners"))
    return {
        "lastfm_name": artist.get("name") or None,
        # An artist page with zero listeners carries no demand information.
        "lastfm_listeners": listeners or None,
        "lastfm_playcount": to_int(stats.get("playcount")) if listeners else None,
    }
