"""Entity resolution: decide when records from different sources are the same artist or venue.

Pure functions, so every rule is unit-tested. The rule throughout: accept a match only when
the evidence is strong; anything weaker goes to match_review for a person. Never guess.
"""

from __future__ import annotations

import math
import re
import unicodedata

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SPELLING = {"theatre": "theater", "centre": "center", "amphitheatre": "amphitheater", "st": "saint", "mt": "mount"}


def name_key(s: str | None) -> str:
    """Lowercase, no accents or punctuation, "&" as "and", no leading "The", common spellings unified."""
    if not s:
        return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    words = re.sub(r"[^\w\s]|_", " ", s).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(SPELLING.get(w, w) for w in words)


def same_name(a: str | None, b: str | None) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka) and ka.replace(" ", "") == kb.replace(" ", "")


def token_overlap(a: str | None, b: str | None) -> float:
    """Jaccard similarity of the two names' words (0-1)."""
    ta, tb = set(name_key(a).split()), set(name_key(b).split())
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def is_mbid(value: str | None) -> bool:
    return bool(value) and bool(MBID_RE.match(value.lower()))


# ---- Artists ---------------------------------------------------------------------------

def artist_name_matches(name: str, candidate_name: str, aliases: list[str] = ()) -> bool:
    return same_name(name, candidate_name) or any(same_name(name, a) for a in aliases)


def choose_mbid(*, ticketmaster_mbid: str | None, lastfm_mbid: str | None, lastfm_name_ok: bool) -> tuple[str | None, str | None]:
    """The MBID to use and where it came from, before any MusicBrainz search.

    1. Ticketmaster's own MusicBrainz link for the attraction: the promoter's data, trusted.
    2. The MBID Last.fm returned, but only when Last.fm's artist name matched ours (Last.fm
       autocorrect can land on a different act, and its MBIDs are occasionally wrong).
    Otherwise None, and the MusicBrainz job searches by name.
    """
    if is_mbid(ticketmaster_mbid):
        return ticketmaster_mbid.lower(), "ticketmaster"
    if is_mbid(lastfm_mbid) and lastfm_name_ok:
        return lastfm_mbid.lower(), "lastfm"
    return None, None


ERA_START = 1900   # a person born before this can't be a touring act
ERA_END = 1970     # an act that ended before this isn't the one on tour now


def implausible_era(artist_type: str | None, begin: int | None, end: int | None) -> str | None:
    """Why a MusicBrainz artist can't be the act on a current tour, or None if it can.

    Catches IDs that point at a namesake: the composer Engelbert Humperdinck (1854-1921) for the
    singer, a 1960s band for the Southern rock Outlaws. Groups may be older than 1900 (orchestras).
    """
    if end is not None and end < ERA_END:
        return f"ended in {end}"
    if artist_type == "Person" and begin is not None and begin < ERA_START:
        return f"born in {begin}"
    return None


def pick_search_result(name: str, results: list[dict]) -> tuple[dict | None, str]:
    """Pick a MusicBrainz artist search result for `name`, or explain why not.

    Accept only when exactly one result has the same name (or alias) and MusicBrainz scores it
    100. Several same-name artists (common: "Low", "Bush") always go to review.
    """
    matches = [r for r in results if artist_name_matches(name, r.get("name", ""), [a.get("name", "") for a in r.get("aliases") or []])]
    if not matches:
        return None, "no result with this name"
    if len(matches) > 1:
        return None, f"{len(matches)} artists share this name"
    if (matches[0].get("score") or 0) < 100:
        return None, "only a partial match"
    return matches[0], "ok"


# ---- Ticketmaster details ------------------------------------------------------------

LIMIT_PATTERNS = [
    re.compile(r"(\d{1,2})\s*(?:-\s*)?ticket\s+limit", re.I),
    re.compile(r"limit(?:ed)?\s+(?:of|to)\s+(\d{1,2})\b", re.I),
    re.compile(r"\blimit\s+(\d{1,2})\b", re.I),
    re.compile(r"maximum\s+of\s+(\d{1,2})\s+tickets", re.I),
]


def parse_ticket_limit(text: str | None) -> int | None:
    """'There is an overall 8 ticket limit for this event.' -> 8. None if no clear number."""
    if not text:
        return None
    found = {int(m.group(1)) for p in LIMIT_PATTERNS for m in p.finditer(text)}
    return found.pop() if len(found) == 1 and 0 < next(iter(found)) <= 50 else None


STATUS_MAP = {"onsale": "onsale", "offsale": "offsale", "cancelled": "cancelled", "canceled": "cancelled",
              "postponed": "postponed", "rescheduled": "rescheduled"}


def event_status(code: str | None) -> str:
    return STATUS_MAP.get((code or "").lower(), "unknown")
