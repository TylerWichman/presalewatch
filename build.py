"""Render site/ from data/presales.json.

- site/index.html: the presale page, one self-contained file with the data inlined.
- site/alerts.json: one row per event, read by the alert Worker and by the My alerts page
  for artist suggestions. Public data only.
- The account pages from templates/static/ (sign in, My alerts, unsubscribe, privacy).
- site/_headers: security headers for every static file, with a strict Content-Security-Policy
  that allows the page's inline script and style by hash.

Usage:
    python build.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sys

from common import PRESALES_PATH, ROOT, assert_no_secrets

TEMPLATE_PATH = ROOT / "templates" / "index.html"
STATIC_DIR = ROOT / "templates" / "static"
SITE_DIR = ROOT / "site"
OUT_PATH = SITE_DIR / "index.html"
ALERTS_PATH = SITE_DIR / "alerts.json"
HEADERS_PATH = SITE_DIR / "_headers"
SECURITY_HEADERS_PATH = ROOT / "config" / "security_headers.json"
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
                "ai": r.get("artist_id"),  # groups a tour's dates into one card; older data falls back to the name
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
                "he": r.get("high_estimated") or None,  # High that relies on an estimated venue capacity
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
                    "lis": inp.get("lastfm_listeners"),
                    "plc": inp.get("lastfm_playcount"),
                    "cap": inp["venue_capacity"],
                    "cest": inp.get("capacity_estimated") or None,
                    "tour": inp["tour_date_count"],
                    "pop": inp.get("catchment_population"),
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
        "ask_discount": data.get("ask_to_sale_discount"),
        "calibrated_at": data.get("calibrated_at"),
        "events": events,
        "rows": rows,
    }


def alerts_feed(data: dict) -> dict:
    """One row per event for the alert Worker. presale_end is the latest close across its presales."""
    events: dict[str, dict] = {}
    for r in data["presales"]:
        ev = events.get(r["event_id"])
        if ev is None:
            events[r["event_id"]] = ev = drop_none({
                "id": r["event_id"],
                "artist": r["artist"],
                "name": r["event_name"] if r["event_name"].strip().lower() != r["artist"].strip().lower() else None,
                "url": r["url"],
                "venue": r["venue"],
                "city": r["city"],
                "state": r["state"],
                "date": r["event_date"],
                "mode": r["mode"],
                "profit": r["profit"],
                "profit_low": r["profit_low"],
                "profit_high": r["profit_high"],
                "edge": r["edge_sort"],
                "tier": r["tier"],
                "confidence": r["confidence"],
            })
        end = r.get("presale_end")
        if end and end > ev.get("presale_end", ""):
            ev["presale_end"] = end
    return {"v": 1, "generated_at": data["generated_at"], "events": list(events.values())}


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


INLINE_SCRIPT = re.compile(r"<script(\s[^>]*)?>(.*?)</script>", re.S)
INLINE_STYLE = re.compile(r"<style>(.*?)</style>", re.S)


def inline_hashes(html: str) -> tuple[list[str], list[str]]:
    """CSP hashes of the executable inline <script> blocks and the <style> blocks."""
    scripts = [csp_hash(m.group(2)) for m in INLINE_SCRIPT.finditer(html)
               if m.group(2).strip() and "application/json" not in (m.group(1) or "")]
    styles = [csp_hash(m.group(1)) for m in INLINE_STYLE.finditer(html)]
    return scripts, styles


def headers_file(html: str) -> str:
    cfg = json.loads(SECURITY_HEADERS_PATH.read_text(encoding="utf-8"))
    scripts, styles = inline_hashes(html)
    csp = {k: list(v) for k, v in cfg["page_csp"].items()}
    csp["script-src"] += scripts
    csp["style-src"] += styles
    policy = "; ".join(" ".join([k, *v]) for k, v in csp.items())
    lines = ["/*", f"  Content-Security-Policy: {policy}"]
    lines += [f"  {name}: {value}" for name, value in cfg["common"].items()]
    lines += ["", "/alerts.json", "  Cache-Control: no-cache",
              "", "/auth/*", "  Cache-Control: no-store",
              "", "/unsubscribe", "  Cache-Control: no-store"]
    return "\n".join(lines) + "\n"


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
    shutil.copytree(STATIC_DIR, SITE_DIR, dirs_exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8", newline="\n")

    feed = json.dumps(alerts_feed(json.loads(PRESALES_PATH.read_text(encoding="utf-8"))),
                      ensure_ascii=False, separators=(",", ":"))
    assert_no_secrets(feed, "alerts.json")
    ALERTS_PATH.write_text(feed, encoding="utf-8", newline="\n")
    HEADERS_PATH.write_text(headers_file(html), encoding="utf-8", newline="\n")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"{len(payload['rows'])} presales, {len(payload['events'])} events -> "
          f"{OUT_PATH.relative_to(ROOT)} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
