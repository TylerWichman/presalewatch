"""Render site/index.html from data/presales.json.

The output is a single self-contained HTML file with the data inlined.

Usage:
    python build.py
"""

from __future__ import annotations

import json
import sys

from common import PRESALES_PATH, ROOT, assert_no_secrets

TEMPLATE_PATH = ROOT / "templates" / "index.html"
OUT_PATH = ROOT / "site" / "index.html"
PLACEHOLDER = "__PRESALE_DATA__"


def drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def compact(data: dict) -> dict:
    """Split presale rows into shared per-event fields and per-presale fields, with short keys."""
    events: list[dict] = []
    index: dict[str, int] = {}
    rows = []
    for r in data["presales"]:
        if r["event_id"] not in index:
            index[r["event_id"]] = len(events)
            inp = r["inputs"]
            events.append(drop_none({
                "a": r["artist"],
                "n": r["event_name"] if r["event_name"].strip().lower() != r["artist"].strip().lower() else None,
                "u": r["url"],
                "i": r.get("image"),
                "v": r["venue"],
                "c": r["city"],
                "s": r["state"],
                "d": r["event_date"],
                "t": r["event_time"],
                "o": r["onsale"],
                "f0": r["face_min"],
                "f1": r["face_max"],
                "fi": r["fee_included"] or None,
                "dp": r["dynamic_pricing"] or None,
                "m": r["mode"],
                "cf": r["confidence"],
                "tr": r["tier"],
                "ds": r["demand_score"],
                "p": r["profit"],
                "pl": r["profit_low"],
                "ph": r["profit_high"],
                "es": r["edge_sort"],
                "ml": r["multiple_low"],
                "mh": r["multiple_high"],
                "rm": r["resale_median"],
                "rl": r["resale_low"],
                "ra": r["resale_avg"],
                "rn": r["resale_listings"],
                "rc": r["resale_captured_at"],
                "in": drop_none({
                    "face": inp["face_used"],
                    "pop": inp["spotify_popularity"],
                    "fol": inp["spotify_followers"],
                    "cap": inp["venue_capacity"],
                    "tour": inp["tour_date_count"],
                    "mkt": inp["market_tier"],
                    "sig": inp["signals"],
                    "miss": inp["missing_signals"] or None,
                    "sgn": inp["seatgeek_listings"],
                }),
            }))
        desc = r.get("presale_description") or ""
        if desc.strip().lower() == r["presale_name"].strip().lower():
            desc = ""  # many presales just repeat their name
        rows.append(drop_none({
            "e": index[r["event_id"]],
            "n": r["presale_name"],
            "ty": r["presale_type"],
            "cs": r["code_source"],
            "ds": desc or None,
            "u": r["presale_url"],
            "lt": r["presale_link_text"],
            "st": r["presale_start"],
            "en": r["presale_end"],
        }))
    return {
        "generated_at": data["generated_at"],
        "fees": data["fees"],
        "min_listings": data["live_min_listings"],
        "calibrated_at": data.get("calibrated_at"),
        "events": events,
        "rows": rows,
    }


def main() -> None:
    if not PRESALES_PATH.exists():
        sys.exit(f"{PRESALES_PATH.relative_to(ROOT)} not found. Run: python pipeline.py")
    payload = compact(json.loads(PRESALES_PATH.read_text(encoding="utf-8")))

    # Safe to inline inside <script>: escape "<" so no "</script>" can appear.
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"Template must contain {PLACEHOLDER} exactly once.")
    html = template.replace(PLACEHOLDER, blob)
    assert_no_secrets(html, "page")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8", newline="\n")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"{len(payload['rows'])} presales, {len(payload['events'])} events -> "
          f"{OUT_PATH.relative_to(ROOT)} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
