"""Venue matching and capacity parsing rules. Pure functions, so every rule is unit-tested.

Matching (coordinates first):
  confident  within 300 m of Ticketmaster's coordinates AND a matching name AND a venue-like type
  review     within 300 m with a name at least 40% alike (ignoring filler words), or 300-1000 m
             with a matching name. Differently named neighbors are dropped
  no         anything else
Only confident matches write to venues; review goes to match_review; nothing is guessed.

Capacity: prefer a value labeled for concerts; otherwise take the largest. Confidence:
  high    one clear value, or exactly one concert value
  medium  several values and we took the largest (or the largest of several concert values),
          or a single value marked approximate
  low     a range ("1,500-2,000"), or an implausible number. Low never writes; it goes to review.
"""

from __future__ import annotations

import re

from ingest.resolve import haversine_km, name_key, same_name, token_overlap

CONFIDENT_M = 300
REVIEW_M = 1000
MIN_CAPACITY, MAX_CAPACITY = 20, 150_000

# ---- Venue type -----------------------------------------------------------------------

# Checked in order: the first match decides. Lowercased substrings of type labels, infobox
# template names, or OpenStreetMap tag values.
TYPE_RULES = [
    ("stadium", ("stadium", "ballpark", "football ground")),
    ("arena", ("arena", "indoor arena", "sports venue", "sports centre", "sports center", "coliseum", "colosseum", "fieldhouse", "events center", "event center")),
    ("amphitheater", ("amphitheat", "amphitheater", "amphitheatre", "pavilion", "bandstand", "outdoor venue", "bowl")),
    ("theater", ("theatre", "theater", "opera house", "concert hall", "performing arts", "auditorium", "music hall", "playhouse", "cinema", "arts centre", "arts center", "arts_centre")),
    ("club", ("nightclub", "music venue", "music_venue", "concert venue", "night club", "club", "bar", "ballroom", "pub", "jazz", "lounge")),
    ("festival", ("festival",)),
    ("other", ("event venue", "events_venue", "events venue", "convention", "conference", "exhibition", "casino", "fairground", "hotel", "venue",
               "community_centre", "community center", "church", "hall")),
]


def venue_type(texts: list[str]) -> str | None:
    """Our venue_type for a set of descriptive texts, or None if none look like a venue."""
    low = [t.lower() for t in texts if t]
    for kind, words in TYPE_RULES:
        if any(w in t for t in low for w in words):
            return kind
    return None


def is_venue(texts: list[str]) -> bool:
    return venue_type(texts) is not None


# ---- Matching ---------------------------------------------------------------------------

def strip_disambiguation(title: str) -> str:
    """'Neptune Theatre (Seattle)' -> 'Neptune Theatre'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title or "")


NAME_MATCH = 0.6       # word overlap for a matching name (confident needs this)
NAME_REVIEW = 0.4      # weaker overlap that's still worth a person's look; below this it's a neighbor
FILLER = {"the", "at", "of", "and", "a", "an", "in", "on", "by", "presented", "for"}


def name_similarity(name: str, candidate_names: list[str]) -> float:
    """Best match against any candidate name: 1.0 for the same name, otherwise the share of
    words in common, ignoring filler words ("The Funhouse at Mr. Smalls" vs "Mr. Smalls" = 0.67)."""
    names = [n for n in candidate_names if n]
    names += [strip_disambiguation(n) for n in names]
    best = 0.0
    for n in names:
        if same_name(name, n):
            return 1.0
        a = set(name_key(name).split()) - FILLER
        b = set(name_key(n).split()) - FILLER
        if a and b:
            best = max(best, len(a & b) / len(a | b))
    return best


def name_matches(name: str, candidate_names: list[str]) -> bool:
    """Strict: the same name, or 60%+ of all words (filler included) in common. Kept stricter than
    name_similarity on purpose, so "The Funhouse at Mr. Smalls" (a side room) never confidently
    takes Mr. Smalls' capacity."""
    names = [n for n in candidate_names if n]
    names += [strip_disambiguation(n) for n in names]
    return any(same_name(name, n) or token_overlap(name, n) >= NAME_MATCH for n in names)


LARGE_VENUE_NAME = re.compile(r"\b(stadium|arena|field|park|center|centre|dome|coliseum|colosseum|ballpark|garden|bowl)\b", re.I)


# What a large-sounding name says the venue is. Coliseum is used for both.
NAME_KINDS = {
    "stadium": re.compile(r"\b(stadium|field|park|ballpark|dome|bowl|coliseum|colosseum)\b", re.I),
    "arena": re.compile(r"\b(arena|center|centre|garden|fieldhouse|coliseum|colosseum)\b", re.I),
}


def looks_large(name: str | None) -> bool:
    """Ticketmaster's venue name suggests a stadium or arena ("Daikin Park", "Xfinity Mobile Arena")."""
    return bool(LARGE_VENUE_NAME.search(name or ""))


def name_kinds(name: str | None) -> set[str]:
    """Which of stadium/arena the name allows: "Oakland Arena" -> {"arena"}, "Daikin Park" -> {"stadium"}."""
    return {kind for kind, pattern in NAME_KINDS.items() if pattern.search(name or "")}


def classify(*, name: str, lat: float | None, lon: float | None, cand_names: list[str],
             cand_lat: float | None, cand_lon: float | None, type_ok: bool, meters: float | None = None,
             cand_kind: str | None = None) -> tuple[str, float | None]:
    """'confident', 'review', or 'no', plus the distance in meters.

    `meters` can be passed when the source already measured it (Wikipedia geosearch does).

    Renamed stadiums and arenas (naming rights change often, and Wikidata lags): a Wikidata entry
    typed as a stadium or arena (cand_kind) within 300 m is confident WITHOUT a name match, but
    only when Ticketmaster's name says the same kind of venue. Arenas often sit next to stadiums
    (Smoothie King Center by the Superdome, Oakland Arena by the Coliseum), so an "Arena" or
    "Center" never takes a stadium's capacity, and a room inside an arena ("The Theater at MSG")
    never takes the arena's.
    """
    if meters is None and None not in (lat, lon, cand_lat, cand_lon):
        meters = haversine_km(lat, lon, cand_lat, cand_lon) * 1000
    if meters is None:
        return "no", None
    name_ok = name_matches(name, cand_names)
    if meters <= CONFIDENT_M and name_ok and type_ok:
        return "confident", meters
    if meters <= CONFIDENT_M and cand_kind and cand_kind in name_kinds(name):
        return "confident", meters
    # Review only when the names are at least somewhat alike. A differently named neighbor
    # (another theater down the block, a hotel) is just a neighbor, not a possible match.
    if meters <= CONFIDENT_M and name_similarity(name, cand_names) >= NAME_REVIEW:
        return "review", meters
    if meters <= REVIEW_M and name_ok:
        return "review", meters
    return "no", meters


def pick_confident(classified: list[dict]) -> dict | None:
    """The one confident candidate, using tie-breakers when a building has two entries:
    the only one with a capacity, then the only one with a Wikipedia article, then the closest
    if it's under half the distance of the next. Otherwise None (review)."""
    confident = [c for c in classified if c["verdict"] == "confident"]
    for prefer in (lambda c: bool(c.get("capacity_values") or c.get("capacity_raw")), lambda c: bool(c.get("wikipedia_title"))):
        if len(confident) > 1 and sum(map(prefer, confident)) == 1:
            confident = [c for c in confident if prefer(c)]
    if len(confident) > 1:
        confident.sort(key=lambda c: c["meters"])
        if confident[0]["meters"] * 2 < confident[1]["meters"]:
            confident = confident[:1]
    return confident[0] if len(confident) == 1 else None


# ---- Capacity parsing ---------------------------------------------------------------------

CONCERT = re.compile(r"concert|music|end[- ]?stage|centre[- ]?stage|center[- ]?stage|performance|show", re.I)
APPROX = re.compile(r"approx|about|around|circa|~|over|more than|up to|nearly|almost|est\.?", re.I)
YEAR_CONTEXT = re.compile(r"(since|in|from|until|as of|opened|built|renovat|expanded|\()\s*$", re.I)
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+")
RANGE_JOIN = re.compile(r"^\s*(?:–|—|-|to)\s*$", re.I)
LIST_TEMPLATES = ("plainlist", "ubl", "unbulleted list", "flatlist", "hlist", "plain list", "bulleted list")


def clean_wikitext(raw: str) -> str:
    """Strip references, comments, links, and formatting from an infobox value, keeping line breaks."""
    s = raw or ""
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/>", " ", s, flags=re.I)
    s = re.sub(r"<ref[^>]*>.*?</ref>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>|</?p>|</?li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\{\{\s*(?:efn|refn|sfn|citation needed|cn|r)\b[^{}]*\}\}", " ", s, flags=re.I)
    for _ in range(5):  # unwrap inner templates first
        def unwrap(m: re.Match) -> str:
            name, _, body = m.group(1).partition("|")
            n = name.strip().lower()
            if n in LIST_TEMPLATES:
                return "\n" + body.replace("|", "\n") + "\n"
            if n.startswith("formatnum") or n in ("nowrap", "small", "nobr", "nbsp", "big"):
                return body.split("|")[0] if body else name.partition(":")[2]
            return " "
        s2 = re.sub(r"\{\{([^{}]*)\}\}", unwrap, s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)
    s = s.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    s = re.sub(r"[*•]", "\n", s)
    return "\n".join(" ".join(line.split()) for line in s.splitlines() if line.strip())


def extract_values(text: str) -> list[dict]:
    """Capacity numbers in text, each with the label that applies to it ("Concerts: 20,000",
    "20,000 (concerts)", "17,600 seated"), and flags for ranges and approximations."""
    out: list[dict] = []
    for line in re.split(r"\n|;", text):
        matches = list(NUMBER.finditer(line))
        for i, m in enumerate(matches):
            raw = m.group(0)
            if "," in raw and not re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw):
                continue
            n = int(raw.replace(",", ""))
            before = line[matches[i - 1].end() if i else 0:m.start()]
            after = line[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(line)]
            if "," not in raw and 1850 <= n <= 2100 and (YEAR_CONTEXT.search(before) or after.strip().startswith(")")):
                continue  # a year, e.g. "(2019)" or "since 1998"
            label = None
            colon = re.search(r"([A-Za-z][A-Za-z /&-]*):\s*$", before)
            paren = re.match(r"^\s*\(([^)]*)\)", after)
            words = re.match(r"^\s*([A-Za-z][A-Za-z /&-]*)", after)
            if colon:
                label = colon.group(1).strip()
            elif paren:
                label = paren.group(1).strip()
            elif words:
                label = words.group(1).strip()
            out.append({
                "n": n, "label": label,
                "approx": bool(APPROX.search(before[-20:])),
                "range": bool(i + 1 < len(matches) and RANGE_JOIN.match(after)) or bool(i and RANGE_JOIN.match(before)),
            })
    return [v for v in out if v["n"] >= MIN_CAPACITY or v["range"]]


def choose_capacity(values: list[dict]) -> dict | None:
    """Pick one capacity from labeled values. Returns {capacity, label, confidence, reason} or None."""
    if not values:
        return None
    concert = [v for v in values if v["label"] and CONCERT.search(v["label"])]
    if concert:
        pick = max(concert, key=lambda v: v["n"])
        confidence = "high" if len(concert) == 1 else "medium"
        reason = "the value labeled for concerts" if len(concert) == 1 else "the largest of several concert values"
    elif len({v["n"] for v in values}) == 1:
        pick, confidence, reason = values[0], "high", "the only value given"
    else:
        pick = max(values, key=lambda v: v["n"])
        confidence, reason = "medium", "the largest of several values (none labeled for concerts)"
    if pick["approx"] and confidence == "high":
        confidence, reason = "medium", reason + ", marked approximate"
    if pick["range"]:
        confidence, reason = "low", "a range, not one number"
    if not MIN_CAPACITY <= pick["n"] <= MAX_CAPACITY:
        confidence, reason = "low", f"{pick['n']:,} is outside the plausible range"
    return {"capacity": pick["n"], "label": pick["label"], "confidence": confidence, "reason": reason}


def parse_capacity(raw: str | None) -> dict | None:
    """Raw infobox or tag text -> {capacity, label, confidence, reason}, or None if no number."""
    if not raw or not raw.strip():
        return None
    return choose_capacity(extract_values(clean_wikitext(raw)))
