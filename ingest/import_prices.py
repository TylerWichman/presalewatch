"""Import hand-logged prices from a CSV into observed_prices.

Columns (a header row is required; see docs/observed_prices_template.csv):
  event         Ticketmaster event ID or event URL
  kind          face | resale
  basis         ask | sold          (resale only; get_in is always ask)
  price_point   single | get_in | median   (default single)
  section_tier  e.g. "GA floor", "Sec 104" (optional)
  standard      yes | no            (no for VIP/Platinum; default yes)
  price         per ticket, e.g. 129.50
  fees_included yes | no            (optional)
  observed_on   YYYY-MM-DD
  source        where you saw it, e.g. "StubHub listing"
  notes         optional

Rows that can't be matched to a known event are stored with no event and queued in
match_review. Importing the same file twice doesn't create duplicates.

Usage:
    python -m ingest.import_prices prices.csv --db sqlite:local.db
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

from ingest.db import Database, id_map, now_iso, open_db, review

TM_ID_RE = re.compile(r"(?:/event/|^)([A-Za-z0-9]{6,32})(?:[/?#]|$)")
YES, NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0"}


def event_ref_id(ref: str) -> str | None:
    """'https://www.ticketmaster.com/x/event/1B00648D9DB09AA5' or '1B00648D9DB09AA5' -> the ID."""
    m = TM_ID_RE.search(ref.strip())
    return m.group(1) if m else None


def yes_no(value: str, default: int | None) -> int | None:
    v = (value or "").strip().lower()
    if not v:
        return default
    if v in YES:
        return 1
    if v in NO:
        return 0
    raise ValueError(f"expected yes or no, got {value!r}")


def parse_row(row: dict) -> dict:
    """Pure: validate one CSV row. Raises ValueError with a plain-English message."""
    ref = (row.get("event") or "").strip()
    if not ref:
        raise ValueError("event is empty")
    kind = (row.get("kind") or "").strip().lower()
    if kind not in ("face", "resale"):
        raise ValueError("kind must be face or resale")
    point = (row.get("price_point") or "single").strip().lower()
    if point not in ("single", "get_in", "median"):
        raise ValueError("price_point must be single, get_in, or median")
    basis = (row.get("basis") or "").strip().lower() or None
    if point == "get_in":
        if kind != "resale":
            raise ValueError("get_in is a resale price")
        basis = basis or "ask"
        if basis != "ask":
            raise ValueError("get_in is always an asking price (basis ask)")
    if kind == "resale" and basis not in ("ask", "sold"):
        raise ValueError("resale rows need basis ask or sold")
    if kind == "face":
        basis = None
    try:
        price = float((row.get("price") or "").replace("$", "").replace(",", ""))
    except ValueError:
        raise ValueError("price must be a number") from None
    if not 0 < price < 100_000:
        raise ValueError("price must be between 0 and 100,000")
    try:
        observed = date.fromisoformat((row.get("observed_on") or "").strip()).isoformat()
    except ValueError:
        raise ValueError("observed_on must be YYYY-MM-DD") from None
    source = (row.get("source") or "").strip()
    if not source:
        raise ValueError("source is empty")
    return {
        "event_ref": ref, "kind": kind, "price_basis": basis, "price_point": point,
        "section_tier": (row.get("section_tier") or "").strip() or None,
        "standard_ticket": yes_no(row.get("standard"), 1), "price": round(price, 2),
        "fees_included": yes_no(row.get("fees_included"), None), "observed_on": observed,
        "source": source[:200], "notes": (row.get("notes") or "").strip()[:500] or None,
    }


def import_file(db: Database, path: Path) -> dict:
    now = now_iso()
    batch = f"{path.name} {now}"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    good, errors = [], []
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        try:
            good.append(parse_row(row))
        except ValueError as err:
            errors.append(f"line {n}: {err}")
    events = id_map(db, "events", "ticketmaster_id", [event_ref_id(r["event_ref"]) for r in good])
    stmts, unmatched = [], 0
    cols = ("event_id", "event_ref", "kind", "price_basis", "price_point", "section_tier", "standard_ticket", "price",
            "fees_included", "currency", "observed_on", "source", "notes", "import_batch", "last_updated")
    for r in good:
        eid = events.get(event_ref_id(r["event_ref"]))
        values = (eid, r["event_ref"], r["kind"], r["price_basis"], r["price_point"], r["section_tier"], r["standard_ticket"],
                  r["price"], r["fees_included"], "USD", r["observed_on"], r["source"], r["notes"], batch, now)
        # Skip a row identical to one already imported (same event, kind, basis, point, section, price, date, source).
        stmts.append((
            f"INSERT INTO observed_prices ({', '.join(cols)}) SELECT {', '.join('?' for _ in cols)} WHERE NOT EXISTS ("
            "SELECT 1 FROM observed_prices WHERE event_ref = ? AND kind = ? AND price_basis IS ? AND price_point = ?"
            " AND section_tier IS ? AND price = ? AND observed_on = ? AND source = ?)",
            values + (r["event_ref"], r["kind"], r["price_basis"], r["price_point"], r["section_tier"], r["price"], r["observed_on"], r["source"])))
        if eid is None:
            unmatched += 1
            stmts.append(review("observed_price", "csv", r["event_ref"], r["event_ref"],
                                {"reason": "no known event with this Ticketmaster ID", "file": path.name}))
    db.batch(stmts)
    return {"rows": len(rows), "imported": len(good), "unmatched": unmatched, "errors": errors}


def main() -> None:
    ap = argparse.ArgumentParser(description="Import hand-logged prices into observed_prices.")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--db", required=True, help="'d1' or 'sqlite:<path>'")
    args = ap.parse_args()
    result = import_file(open_db(args.db), args.csv)
    print(f"{result['imported']} of {result['rows']} rows imported, {result['unmatched']} not matched to an event (queued for review)")
    for e in result["errors"]:
        print(f"  skipped {e}")
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
