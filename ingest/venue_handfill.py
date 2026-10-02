"""Hand-fill CSV for venues no source could resolve.

Export: venues with upcoming events and no capacity, busiest first, with links to check and any
open review item. Fill in the `capacity` column (and ideally `source_url`), then import. Imported
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

COLUMNS = ["ticketmaster_id", "name", "city", "state", "upcoming_events", "map", "search", "review_note",
           "capacity", "source_url", "notes"]


def unresolved(db: Database, limit: int | None = None) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT v.id, v.ticketmaster_id, v.name, v.city, v.state, v.latitude, v.longitude, COUNT(e.id) AS upcoming,"
        " (SELECT details FROM match_review r WHERE r.external_id = CAST(v.id AS TEXT) AND r.status = 'open'"
        "   AND r.kind IN ('venue_match', 'venue_capacity') ORDER BY r.last_updated DESC LIMIT 1) AS review"
        " FROM venues v JOIN events e ON e.venue_id = v.id AND e.event_date >= ?"
        " WHERE v.capacity IS NULL GROUP BY v.id ORDER BY upcoming DESC, v.name" + (" LIMIT ?" if limit else ""),
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
        print(f"{export(db, args.out)} venues without a capacity -> {args.out}")
        return
    result = import_file(db, args.csv)
    print(f"{result['filled']} capacities imported")
    for e in result["errors"]:
        print(f"  skipped {e}")
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
