"""PouchIt data pipeline: pull upcoming presales from Ticketmaster and write data/presales.json.

Usage:
    python pipeline.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import ticketmaster
from common import PRESALES_PATH, assert_no_secrets, iso


def presale_rows(parsed: list[dict]) -> list[dict]:
    rows = []
    for p in parsed:
        e = p["event"]
        for i, ps in enumerate(p["presales"]):
            rows.append({
                "id": f"{e['event_id']}-{i}",
                "event_id": e["event_id"],
                "artist": e["artist"],
                "artist_id": e["artist_id"],
                "event_name": e["name"],
                "url": e["url"],
                "image": e["image"],
                "venue": e["venue"],
                "city": e["city"],
                "state": e["state"],
                "event_date": e["event_date"],
                "event_time": e["event_time"],
                "presale_name": ps["name"],
                "presale_type": ps["type"],
                "code_source": ps["code_source"],
                "presale_description": ps["description"] or None,
                "presale_url": ps["url"],
                "presale_link_text": ps["link_text"],
                "presale_start": ps["start"],
                "presale_end": ps["end"],
                "onsale": e["onsale_date"],
                "face_min": e["face_min"],
                "face_max": e["face_max"],
                "dynamic_pricing": e["dynamic_pricing_flag"],
            })
    rows.sort(key=presale_sort_key)
    return rows


def presale_sort_key(row: dict) -> tuple:
    """Soonest presale first; ties by artist."""
    return (row["presale_start"] or "", row["artist"].lower())


def main() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)

    print("1/2 Pulling events and presales from Ticketmaster")
    raw, tm_calls = ticketmaster.fetch_raw(now)
    parsed = [p for p in (ticketmaster.parse_event(ev, now) for ev in raw.values()) if p]
    print(f"  {len(raw)} events scanned, {len(parsed)} with upcoming presales ({tm_calls} calls)")

    print("2/2 Writing presales.json")
    rows = presale_rows(parsed)
    text = json.dumps({
        "generated_at": iso(now),
        "event_count": len(parsed),
        "presale_count": len(rows),
        "presales": rows,
    }, indent=1, ensure_ascii=False)
    assert_no_secrets(text, "presales.json")
    PRESALES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRESALES_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"  {len(rows)} presale rows -> data/presales.json")


if __name__ == "__main__":
    main()
