"""Hand-fill CSV for venues no source could resolve.

Export: the 50 venues without a capacity whose upcoming artists draw the biggest audiences
(highest Last.fm listener count among each venue's upcoming billed artists), with links to check
and any open review item. Those are the venues where a capacity changes scores the most. Fill in the `capacity` column (and ideally `source_url`), then import. Imported
capacities are marked verified and are never overwritten by an automated source.

Usage:
    python -m ingest.venue_handfill export --db d1 --out venues_to_fill.csv
    python -m ingest.venue_handfill import venues_to_fill.csv --db d1
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from ingest.db import Database, id_map, now_iso, open_db
from ingest.venue_enrichment import write_manual

COLUMNS = ["ticketmaster_id", "name", "city", "state", "top_artist", "top_artist_listeners", "upcoming_events",
           "map", "search", "review_note", "capacity", "source_url", "notes"]
TOP_N = 50


def unresolved(db: Database, limit: int | None = TOP_N) -> list[dict]:
    """Venues without a capacity, biggest upcoming audience first (venues with no Last.fm data
    for any upcoming artist come last, then by event count)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.query(
        "WITH up AS (SELECT e.id, e.venue_id FROM events e WHERE e.event_date >= ?),"
        " reach AS (SELECT up.venue_id, a.name, a.lastfm_listeners,"
        "   ROW_NUMBER() OVER (PARTITION BY up.venue_id ORDER BY a.lastfm_listeners DESC) AS rn"
        "   FROM up JOIN event_artists ea ON ea.event_id = up.id JOIN artists a ON a.id = ea.artist_id"
        "   WHERE a.lastfm_listeners IS NOT NULL)"
        " SELECT v.id, v.ticketmaster_id, v.name, v.city, v.state, v.latitude, v.longitude,"
        " (SELECT COUNT(*) FROM up WHERE up.venue_id = v.id) AS upcoming,"
        " r.name AS top_artist, r.lastfm_listeners AS top_listeners,"
        " (SELECT details FROM match_review mr WHERE mr.external_id = CAST(v.id AS TEXT) AND mr.status = 'open'"
        "   AND mr.kind IN ('venue_match', 'venue_capacity') ORDER BY mr.last_updated DESC LIMIT 1) AS review"
        " FROM venues v LEFT JOIN reach r ON r.venue_id = v.id AND r.rn = 1"
        " WHERE v.capacity IS NULL AND EXISTS (SELECT 1 FROM up WHERE up.venue_id = v.id)"
        " ORDER BY r.lastfm_listeners IS NULL, r.lastfm_listeners DESC, upcoming DESC, v.name" + (" LIMIT ?" if limit else ""),
        (today, limit) if limit else (today,))
    for r in rows:
        r["review_note"] = json.loads(r["review"]).get("reason", "") if r["review"] else ""
    return rows


def export(db: Database, out: Path) -> int:
    rows = unresolved(db)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            q = urllib.parse.quote_plus(f"{r['name']} {r['city'] or ''} capacity")
            w.writerow({
                "ticketmaster_id": r["ticketmaster_id"], "name": r["name"], "city": r["city"], "state": r["state"],
                "top_artist": r["top_artist"] or "", "top_artist_listeners": r["top_listeners"] or "",
                "upcoming_events": r["upcoming"],
                "map": f"https://www.openstreetmap.org/?mlat={r['latitude']}&mlon={r['longitude']}#map=18/{r['latitude']}/{r['longitude']}"
                       if r["latitude"] is not None else "",
                "search": f"https://en.wikipedia.org/w/index.php?search={q}", "review_note": r["review_note"],
                "capacity": "", "source_url": "", "notes": "",
            })
    return len(rows)


def import_file(db: Database, path: Path) -> dict:
    now = now_iso()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("capacity") or "").strip()]
    ids = id_map(db, "venues", "ticketmaster_id", [r.get("ticketmaster_id") for r in rows])
    stmts, errors = [], []
    for n, r in enumerate(rows, start=2):
        raw = (r.get("capacity") or "").replace(",", "").strip()
        if not raw.isdigit() or not 20 <= int(raw) <= 150_000:
            errors.append(f"line {n}: capacity must be a whole number from 20 to 150,000")
            continue
        vid = ids.get(r.get("ticketmaster_id"))
        if vid is None:
            errors.append(f"line {n}: unknown ticketmaster_id {r.get('ticketmaster_id')!r}")
            continue
        stmts += write_manual(vid, int(raw), "csv", now)
        url = (r.get("source_url") or "").strip()
        if url.startswith("https://") or url.startswith("http://"):
            stmts.append(("UPDATE venues SET capacity_source_url = ? WHERE id = ?", (url, vid)))
    db.batch(stmts)
    return {"filled": len(rows) - len(errors), "errors": errors}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--db", required=True)
    ex.add_argument("--out", type=Path, default=Path("venues_to_fill.csv"))
    im = sub.add_parser("import")
    im.add_argument("csv", type=Path)
    im.add_argument("--db", required=True)
    args = ap.parse_args()
    db = open_db(args.db)
    if args.cmd == "export":
        print(f"top {export(db, args.out)} venues without a capacity, by audience size -> {args.out}")
        return
    result = import_file(db, args.csv)
    print(f"{result['filled']} capacities imported")
    for e in result["errors"]:
        print(f"  skipped {e}")
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
