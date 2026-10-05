"""Run the ingestion jobs against D1 (production) or a local SQLite file.

Order matters: Ticketmaster creates the artists and venues, Last.fm and MusicBrainz resolve
MusicBrainz IDs, Wikidata turns those into Wikipedia titles and YouTube channel IDs, venue
enrichment fills capacities, catchment adds the population around each venue, and the rest
read those IDs. A job that fails is logged in
ingest_runs and the others still run.

Write budget: D1's free tier allows 100,000 rows written a day for the whole database, shared
with sign-ins and alert preferences. Ingestion gets config ingest.daily_write_budget of it
(60,000), minus whatever earlier runs the same UTC day already wrote. When it's spent, the job
in progress stops after its current batch (logged as 'partial'), the rest are skipped, and the
next day's run carries on: every job works from what's still due, not from a fixed list.

Usage:
    python -m ingest.run --db sqlite:local.db                 # every source
    python -m ingest.run --db d1 --sources lastfm,listenbrainz
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone

from common import load_config

from ingest import (catchment, lastfm_job, listenbrainz_job, musicbrainz_job, pageviews_job, ticketmaster_job,
                    venue_enrichment, venue_estimates, wikidata_job, youtube_job)
from ingest.coverage import report
from ingest.db import Database, WriteBudgetSpent, open_db, run_log

JOBS = {
    "ticketmaster": ticketmaster_job,
    "lastfm": lastfm_job,
    "musicbrainz": musicbrainz_job,
    "wikidata": wikidata_job,
    "venues": venue_enrichment,
    "estimates": venue_estimates,
    "catchment": catchment,
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
    set_budget(db, load_config()["ingest"]["daily_write_budget"])
    print(f"Write budget for this run: {db.budget:,} rows")
    failed = []
    for i, name in enumerate(names):
        print(f"{name}:")
        try:
            with run_log(db, name) as stats:
                db.check_budget()
                JOBS[name].run(db, stats)
        except WriteBudgetSpent as err:
            print(f"  Stopped: {err}. Skipped for today: {', '.join(names[i + 1:]) or 'nothing'}; they continue on the next run.")
            break
        except Exception:  # keep going; the failure is recorded in ingest_runs
            failed.append(name)
            traceback.print_exc(limit=3)
    print(f"Rows written this run: {db.rows_written:,}")
    print()
    print(report(db))
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


def set_budget(db: Database, daily: int, now: datetime | None = None) -> None:
    """This run's budget: the daily allowance minus what ingestion already wrote today (UTC)."""
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    spent = db.scalar("SELECT COALESCE(SUM(rows_written), 0) FROM ingest_runs WHERE started_at >= ?", (today,)) or 0
    db.budget = max(daily - int(spent), 0)


if __name__ == "__main__":
    main()
