"""Render site/index.html from data/presales.json.

The output is a single self-contained HTML file with the data inlined.

Usage:
    python build.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "presales.json"
TEMPLATE_PATH = ROOT / "templates" / "index.html"
OUT_PATH = ROOT / "site" / "index.html"
PLACEHOLDER = "__PRESALE_DATA__"


def compact_event(ev: dict) -> dict:
    """Keep only what the page renders, with short keys to trim page size."""
    presales = []
    for p in ev["presales"]:
        desc = p.get("description") or ""
        if desc.strip().lower() == p["name"].strip().lower():
            desc = ""  # many presales just repeat their name
        presales.append({
            "name": p["name"],
            "desc": desc or None,
            "start": p["start"],
            "end": p["end"],
            "url": p.get("url"),
            "link": p.get("link_text"),
        })
    out = {
        "artist": ev["artist"],
        "name": ev["name"],
        "url": ev.get("url"),
        "image": ev.get("image"),
        "venue": ev.get("venue"),
        "city": ev.get("city"),
        "state": ev.get("state"),
        "date": None if ev.get("event_tba") else ev.get("event_date"),
        "time": ev.get("event_time"),
        "onsale": ev.get("public_onsale"),
        "presales": presales,
    }
    return {k: v for k, v in out.items() if v is not None}


def main() -> None:
    if not DATA_PATH.exists():
        sys.exit(f"{DATA_PATH.relative_to(ROOT)} not found. Run: python fetch_presales.py")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    payload = {
        "generated_at": data["generated_at"],
        "events": [compact_event(ev) for ev in data["events"]],
    }
    for ev in payload["events"]:
        for p in ev["presales"]:
            for k in [k for k, v in p.items() if v is None]:
                del p[k]

    # Safe to inline inside <script>: escape "<" so no "</script>" can appear.
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"Template must contain {PLACEHOLDER} exactly once.")
    html = template.replace(PLACEHOLDER, blob)

    key = os.environ.get("TM_API_KEY", "").strip()
    if (key and key in html) or "apikey=" in html.lower():
        sys.exit("Refusing to write page: it appears to contain the API key.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8", newline="\n")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"{len(payload['events'])} events -> {OUT_PATH.relative_to(ROOT)} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
