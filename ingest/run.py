"""Run the ingestion jobs against D1 (production) or a local SQLite file.

Order matters: Ticketmaster creates the artists and venues, Last.fm and MusicBrainz resolve
MusicBrainz IDs, Wikidata turns those into Wikipedia titles and YouTube channel IDs, venue
enrichment fills capacities, and the rest read those IDs. A job that fails is logged in
ingest_runs and the others still run.

Usage:
    python -m ingest.run --db sqlite:local.db                 # every source
    python -m ingest.run --db d1 --sources lastfm,listenbrainz
"""

from __future__ import annotations

import argparse
import sys
import traceback

from ingest import (lastfm_job, listenbrainz_job, musicbrainz_job, pageviews_job, ticketmaster_job,
                    venue_enrichment, venue_estimates, wikidata_job, youtube_job)
from ingest.coverage import report
from ingest.db import open_db, run_log

JOBS = {
    "ticketmaster": ticketmaster_job,
    "lastfm": lastfm_job,
    "musicbrainz": musicbrainz_job,
    "wikidata": wikidata_job,
    "venues": venue_enrichment,
    "estimates": venue_estimates,
    "pageviews": pageviews_job,
    "listenbrainz": listenbrainz_job,
    "youtube": youtube_job,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill the intelligence database.")
    ap.add_argument("--db", required=True, help="'d1' or 'sqlite:<path>'")
    ap.add_argument("--sources", default="all", help=f"comma-separated: {', '.join(JOBS)} (default all)")
    args = ap.parse_args()
    names = list(JOBS) if args.sources == "all" else [s.strip() for s in args.sources.split(",")]
    unknown = [n for n in names if n not in JOBS]
    if unknown:
        sys.exit(f"unknown source(s): {', '.join(unknown)}")
    db = open_db(args.db)
    failed = []
    for name in names:
        print(f"{name}:")
        try:
            with run_log(db, name) as stats:
                JOBS[name].run(db, stats)
        except Exception:  # keep going; the failure is recorded in ingest_runs
            failed.append(name)
            traceback.print_exc(limit=3)
    print()
    print(report(db))
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
