"""Coverage report: how complete the intelligence database is.

Usage:
    python -m ingest.coverage --db sqlite:local.db
"""

from __future__ import annotations

import argparse

from ingest.db import Database, open_db

TABLES = ["artists", "artist_aliases", "artist_tags", "artist_similar", "artist_metrics_snapshots", "artist_pageviews",
          "artist_youtube_current", "venues", "events", "event_artists", "presales", "event_status_history",
          "resale_snapshots", "observed_prices", "match_review", "ingest_runs"]


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def numbers(db: Database) -> dict:
    one = lambda sql: db.scalar(sql) or 0  # noqa: E731
    return {
        "artists": one("SELECT COUNT(*) FROM artists"),
        "headliners": one("SELECT COUNT(DISTINCT artist_id) FROM event_artists WHERE position = 0"),
        "mbid": one("SELECT COUNT(*) FROM artists WHERE mbid IS NOT NULL"),
        "wikidata": one("SELECT COUNT(*) FROM artists WHERE wikidata_id IS NOT NULL"),
        "wikipedia": one("SELECT COUNT(*) FROM artists WHERE wikipedia_title IS NOT NULL"),
        "youtube": one("SELECT COUNT(*) FROM artists WHERE youtube_channel_id IS NOT NULL"),
        "youtube_stats": one("SELECT COUNT(*) FROM artist_youtube_current"),
        "lastfm": one("SELECT COUNT(*) FROM artists WHERE lastfm_listeners IS NOT NULL"),
        "listenbrainz": one("SELECT COUNT(DISTINCT artist_id) FROM artist_metrics_snapshots WHERE listenbrainz_listeners IS NOT NULL"),
        "pageviews": one("SELECT COUNT(DISTINCT artist_id) FROM artist_pageviews"),
        "venues": one("SELECT COUNT(*) FROM venues"),
        "venue_capacity": one("SELECT COUNT(*) FROM venues WHERE capacity IS NOT NULL"),
        "venue_wikidata": one("SELECT COUNT(*) FROM venues WHERE wikidata_id IS NOT NULL"),
        "venue_coords": one("SELECT COUNT(*) FROM venues WHERE latitude IS NOT NULL"),
        "events": one("SELECT COUNT(*) FROM events"),
        "upcoming": one("SELECT COUNT(*) FROM events WHERE event_date >= date('now')"),
        "event_face": one("SELECT COUNT(*) FROM events WHERE face_min IS NOT NULL OR face_max IS NOT NULL"),
        "event_capacity": one("SELECT COUNT(*) FROM events e JOIN venues v ON v.id = e.venue_id WHERE v.capacity IS NOT NULL"),
        "event_resale": one("SELECT COUNT(DISTINCT event_id) FROM resale_snapshots"),
        "event_observed": one("SELECT COUNT(DISTINCT event_id) FROM observed_prices WHERE event_id IS NOT NULL"),
        "review": {r["kind"]: r["n"] for r in db.query("SELECT kind, COUNT(*) AS n FROM match_review WHERE status = 'open' GROUP BY kind")},
        "rows": {t: one(f"SELECT COUNT(*) FROM {t}") for t in TABLES},
    }


def report(db: Database) -> str:
    n = numbers(db)
    a, v, e = n["artists"], n["venues"], n["events"]
    lines = [
        "Coverage report",
        f"Artists: {a}",
        f"  MusicBrainz ID       {n['mbid']:>5}  {pct(n['mbid'], a)}",
        f"  Wikidata item        {n['wikidata']:>5}  {pct(n['wikidata'], a)}",
        f"  Wikipedia title      {n['wikipedia']:>5}  {pct(n['wikipedia'], a)}",
        f"  YouTube channel ID   {n['youtube']:>5}  {pct(n['youtube'], a)}   (stats stored for {n['youtube_stats']})",
        f"  Last.fm listeners    {n['lastfm']:>5}  {pct(n['lastfm'], a)}",
        f"  ListenBrainz data    {n['listenbrainz']:>5}  {pct(n['listenbrainz'], a)}",
        f"  Pageview history     {n['pageviews']:>5}  {pct(n['pageviews'], a)}",
        f"Venues: {v}",
        f"  Capacity             {n['venue_capacity']:>5}  {pct(n['venue_capacity'], v)}",
        f"  Wikidata item        {n['venue_wikidata']:>5}  {pct(n['venue_wikidata'], v)}",
        f"  Coordinates          {n['venue_coords']:>5}  {pct(n['venue_coords'], v)}",
        f"Events: {e} ({n['upcoming']} upcoming)",
        f"  Face value           {n['event_face']:>5}  {pct(n['event_face'], e)}",
        f"  Venue capacity       {n['event_capacity']:>5}  {pct(n['event_capacity'], e)}",
        f"  Resale snapshot      {n['event_resale']:>5}  {pct(n['event_resale'], e)}",
        f"  Logged prices        {n['event_observed']:>5}  {pct(n['event_observed'], e)}",
        "Open review items: " + (", ".join(f"{k} {c}" for k, c in sorted(n["review"].items())) or "none"),
        "Rows: " + ", ".join(f"{t} {c}" for t, c in n["rows"].items()),
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the coverage report.")
    ap.add_argument("--db", required=True, help="'d1' or 'sqlite:<path>'")
    print(report(open_db(ap.parse_args().db)))


if __name__ == "__main__":
    main()
