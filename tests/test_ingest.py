"""Ingestion tests: ID resolution rules, each source's parser, and idempotent writes to a real
SQLite database with every migration applied. No network calls."""

import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from ingest import (import_prices, lastfm_job, listenbrainz_job, musicbrainz_job, pageviews_job, ticketmaster_job,
                    wikidata_job, youtube_job)
from ingest.coverage import report
from ingest.db import D1_MAX_PARAMS, SqliteDatabase, upsert
from ingest.resolve import choose_mbid, event_status, name_key, parse_ticket_limit, pick_search_result, same_name

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_events.json"
MB1 = "63094905-b963-46ca-8429-090e1afd6752"
MB2 = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
NOW = "2026-10-02T12:00:00Z"


def temp_db() -> SqliteDatabase:
    d = tempfile.mkdtemp()
    return SqliteDatabase(Path(d) / "t.db")


class NameMatching(unittest.TestCase):
    def test_name_key(self):
        self.assertEqual(name_key("The Fillmore"), "fillmore")
        self.assertEqual(name_key("Fox Theatre - Oakland"), "fox theater oakland")
        self.assertEqual(name_key("Beyoncé & Jay-Z"), "beyonce and jay z")
        self.assertEqual(name_key(None), "")

    def test_same_name(self):
        self.assertTrue(same_name("Queensryche", "Queensrÿche"))
        self.assertTrue(same_name("Four Tops", "The Four Tops"))
        self.assertTrue(same_name("Emerson Lake and Palmer", "Emerson, Lake & Palmer"))
        self.assertFalse(same_name("Face 2 Face", "Face to Face"))
        self.assertFalse(same_name("", ""))


class ArtistResolution(unittest.TestCase):
    def test_ticketmaster_link_wins(self):
        self.assertEqual(choose_mbid(ticketmaster_mbid=MB1.upper(), lastfm_mbid=MB2, lastfm_name_ok=True), (MB1, "ticketmaster"))

    def test_lastfm_mbid_only_when_names_match(self):
        self.assertEqual(choose_mbid(ticketmaster_mbid=None, lastfm_mbid=MB2, lastfm_name_ok=True), (MB2, "lastfm"))
        self.assertEqual(choose_mbid(ticketmaster_mbid=None, lastfm_mbid=MB2, lastfm_name_ok=False), (None, None))
        self.assertEqual(choose_mbid(ticketmaster_mbid="not-an-mbid", lastfm_mbid="", lastfm_name_ok=True), (None, None))

    def test_search_accepts_only_a_single_exact_full_score_match(self):
        r = lambda name, score=100, aliases=(): {"id": MB1, "name": name, "score": score, "aliases": [{"name": a} for a in aliases]}  # noqa: E731
        self.assertEqual(pick_search_result("Hilary Duff", [r("Hilary Duff"), r("Hilary Duff Tribute", 80)])[1], "ok")
        self.assertEqual(pick_search_result("Low", [r("Low"), r("Low")])[1], "2 artists share this name")
        self.assertEqual(pick_search_result("Bush", [r("Bush", 90)])[1], "only a partial match")
        self.assertEqual(pick_search_result("Nobody", [r("Somebody")])[1], "no result with this name")
        self.assertEqual(pick_search_result("Prince", [r("Prince Rogers Nelson", aliases=["Prince"])])[1], "ok")


class TicketmasterDetails(unittest.TestCase):
    def test_ticket_limit(self):
        self.assertEqual(parse_ticket_limit("There is an overall 8 ticket limit for this event."), 8)
        self.assertEqual(parse_ticket_limit("Tickets are limited to 4 per household."), 4)
        self.assertIsNone(parse_ticket_limit("Limit 4 for GA, 6-ticket limit for seats."))  # ambiguous
        self.assertIsNone(parse_ticket_limit(None))

    def test_status(self):
        self.assertEqual(event_status("canceled"), "cancelled")
        self.assertEqual(event_status("offsale"), "offsale")
        self.assertEqual(event_status("weird"), "unknown")

    def test_parse_fixture(self):
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["_embedded"]["events"]
        parsed = [ticketmaster_job.parse(ev) for ev in raw]
        self.assertTrue(all(parsed))
        with_mbid = [a for p in parsed for a in p["artists"] if a["mbid"]]
        self.assertGreater(len(with_mbid), 5)
        self.assertTrue(all(len(a["mbid"]) == 36 for a in with_mbid))
        self.assertTrue(all(p["venue"]["latitude"] is not None for p in parsed))
        self.assertEqual({p["event"]["status"] for p in parsed} - {"onsale", "offsale", "cancelled"}, set())
        self.assertIn(8, [p["event"]["ticket_limit"] for p in parsed])
        multi = next(p for p in parsed if len(p["artists"]) > 1)
        self.assertEqual([a["position"] for a in multi["artists"]], list(range(len(multi["artists"]))))


class TicketmasterWrites(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["_embedded"]["events"]
        self.parsed = [ticketmaster_job.parse(ev) for ev in raw]

    def counts(self):
        return {t: self.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in
                ("events", "venues", "artists", "event_artists", "presales", "event_status_history")}

    def test_idempotent_and_status_changes_logged_once(self):
        ticketmaster_job.write(self.db, self.parsed, NOW)
        first = self.counts()
        self.assertEqual(first["event_status_history"], first["events"])  # first sighting of each
        ticketmaster_job.write(self.db, self.parsed, "2026-10-02T18:00:00Z")
        self.assertEqual(self.counts(), first)  # nothing new, nothing duplicated
        changed = json.loads(json.dumps(self.parsed))
        changed[0]["event"]["status"] = "offsale" if changed[0]["event"]["status"] != "offsale" else "onsale"
        ticketmaster_job.write(self.db, changed, "2026-10-03T00:00:00Z")
        h = self.db.query("SELECT * FROM event_status_history ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((h["status"], h["previous_status"], h["previous_seen_at"], h["seen_at"]),
                         (changed[0]["event"]["status"], self.parsed[0]["event"]["status"], "2026-10-02T18:00:00Z", "2026-10-03T00:00:00Z"))

    def test_first_seen_kept_and_mbids_set(self):
        ticketmaster_job.write(self.db, self.parsed, NOW)
        ticketmaster_job.write(self.db, self.parsed, "2026-10-09T00:00:00Z")
        self.assertEqual(self.db.scalar("SELECT MIN(first_seen_at) FROM events"), NOW)
        self.assertEqual(self.db.scalar("SELECT MAX(last_seen_at) FROM events"), "2026-10-09T00:00:00Z")
        self.assertGreater(self.db.scalar("SELECT COUNT(*) FROM artists WHERE mbid_source = 'ticketmaster'"), 5)

    def test_duplicate_mbid_goes_to_review(self):
        dup = json.loads(json.dumps(self.parsed[:1]))
        a = next(a for p in self.parsed for a in p["artists"] if a["mbid"])
        dup[0]["artists"] = [{**a, "ticketmaster_id": "K_DUPLICATE", "name": a["name"] + " (2)"}]
        ticketmaster_job.write(self.db, self.parsed, NOW)
        ticketmaster_job.write(self.db, dup, NOW)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM artists WHERE mbid = ?", (a["mbid"],)), 1)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM match_review WHERE external_id = 'K_DUPLICATE'"), 1)


class LastFm(unittest.TestCase):
    INFO = {"name": "Hilary Duff", "mbid": MB1.upper(), "stats": {"listeners": "1500000", "playcount": "60000000"},
            "tags": {"tag": [{"name": "Pop"}, {"name": "pop"}, {"name": "Disney"}]},
            "similar": {"artist": [{"name": "Ashlee Simpson"}]}}

    def test_parse(self):
        d = lastfm_job.parse(self.INFO, "Hilary Duff", trusted=False)
        self.assertEqual((d["listeners"], d["mbid"], d["tags"], d["similar"]), (1500000, MB1, ["pop", "disney"], ["Ashlee Simpson"]))
        self.assertFalse(lastfm_job.parse({**self.INFO, "name": "Face to Face"}, "Face 2 Face", trusted=False)["found"])
        self.assertTrue(lastfm_job.parse({**self.INFO, "name": "Face to Face"}, "Face to Face", trusted=True)["found"])

    def test_statements_write_and_guard_mbid(self):
        db = temp_db()
        db.run("INSERT INTO artists (name, name_key, source, last_updated) VALUES ('Hilary Duff', 'hilary duff', 't', ?)", (NOW,))
        db.run("INSERT INTO artists (name, name_key, mbid, source, last_updated) VALUES ('Other', 'other', ?, 't', ?)", (MB1, NOW))
        row = {"id": 1, "name": "Hilary Duff", "lastfm_name": None, "mbid": None}
        info = lastfm_job.parse(self.INFO, "Hilary Duff", trusted=False)
        db.batch(lastfm_job.statements(row, info, NOW, "2026-10-02", mbid_owner=2))
        self.assertIsNone(db.scalar("SELECT mbid FROM artists WHERE id = 1"))           # taken: not assigned
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM match_review"), 1)
        self.assertEqual(db.scalar("SELECT lastfm_listeners FROM artist_metrics_snapshots WHERE artist_id = 1"), 1500000)
        db.batch(lastfm_job.statements(row, info, NOW, "2026-10-02", mbid_owner=None))  # same day again: no duplicate
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM artist_metrics_snapshots"), 1)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM artist_tags WHERE artist_id = 1"), 2)


class MusicBrainzAndWikidata(unittest.TestCase):
    def test_mb_details(self):
        d = musicbrainz_job.parse_details({
            "type": "Group", "country": "US", "life-span": {"begin": "1998-05", "end": "2010", "ended": True},
            "aliases": [{"name": "HD", "locale": "en"}, {"name": "HD"}],
            "relations": [{"type": "official homepage", "url": {"resource": "https://x.com"}},
                          {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q44190"}}]})
        self.assertEqual((d["artist_type"], d["country"], d["active_from"], d["active_to"], d["wikidata_id"]), ("Group", "US", 1998, 2010, "Q44190"))
        self.assertEqual(len(d["aliases"]), 1)
        self.assertIsNone(musicbrainz_job.parse_details({"life-span": {"begin": "1998", "ended": False, "end": "2001"}})["active_to"])

    def test_wikidata_item_must_carry_our_mbid(self):
        ent = {"sitelinks": {"enwiki": {"title": "Radiohead"}},
               "claims": {"P434": [{"rank": "normal", "mainsnak": {"datavalue": {"value": MB2}}}],
                          "P2397": [{"rank": "deprecated", "mainsnak": {"datavalue": {"value": "UCold"}}},
                                    {"rank": "preferred", "mainsnak": {"datavalue": {"value": "UCq19-LqvG35A-30oyAiPiqA"}}}]}}
        self.assertEqual(wikidata_job.parse_artist_entity(ent, MB2),
                         {"ok": True, "wikipedia_title": "Radiohead", "youtube_channel_id": "UCq19-LqvG35A-30oyAiPiqA"})
        self.assertFalse(wikidata_job.parse_artist_entity(ent, MB1)["ok"])


class Pageviews(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(pageviews_job.parse({"items": [{"timestamp": "2026093000", "views": 120}, {"timestamp": "x", "views": 1}]}),
                         [{"day": "2026-09-30", "views": 120}])

    def test_plan(self):
        y = date(2026, 10, 1)
        self.assertEqual(pageviews_job.plan("Radiohead", None, None, y), (date(2025, 10, 2), False))       # 12-month backfill
        self.assertEqual(pageviews_job.plan("Radiohead", "Radiohead", "2026-09-28", y), (date(2026, 9, 29), False))
        self.assertEqual(pageviews_job.plan("Radiohead", "Radiohead", "2026-10-01", y), (None, False))     # up to date
        self.assertEqual(pageviews_job.plan("Radiohead (band)", "Radiohead", "2026-09-30", y), (date(2025, 10, 2), True))

    def test_inserts_stay_under_d1_param_cap_and_upsert(self):
        db = temp_db()
        db.run("INSERT INTO artists (name, name_key, source, last_updated) VALUES ('R', 'r', 't', ?)", (NOW,))
        rows = [{"day": f"2026-{m:02d}-{d:02d}", "views": d} for m in range(1, 3) for d in range(1, 29)]
        stmts = pageviews_job.insert_statements(1, "R", rows, NOW)
        self.assertTrue(all(len(p) <= D1_MAX_PARAMS for _, p in stmts))
        db.batch(stmts)
        db.batch(pageviews_job.insert_statements(1, "R", rows, NOW))
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM artist_pageviews"), len(rows))


class ListenBrainzAndYouTube(unittest.TestCase):
    def test_listenbrainz_keeps_totals_and_drops_user_names(self):
        d = listenbrainz_job.parse({"payload": {"total_user_count": 33163, "total_listen_count": 8055763,
                                                "listeners": [{"user_name": "someone", "listen_count": 10}]}})
        self.assertEqual(d, {"listeners": 33163, "listens": 8055763})
        self.assertNotIn("someone", json.dumps(d))
        self.assertIsNone(listenbrainz_job.parse(None))

    def test_youtube_parse(self):
        self.assertIsNone(youtube_job.parse({"id": "UC1", "statistics": {"hiddenSubscriberCount": True, "subscriberCount": "0"}})["subscriber_count"])
        self.assertEqual(youtube_job.parse({"id": "UC1", "statistics": {"subscriberCount": "1200", "viewCount": "5"}})["subscriber_count"], 1200)

    def test_youtube_retention_runs_even_without_a_key(self):
        db = temp_db()
        db.run("INSERT INTO artists (name, name_key, youtube_channel_id, source, last_updated) VALUES ('A', 'a', 'UC1', 't', ?)", (NOW,))
        db.run("INSERT INTO artists (name, name_key, youtube_channel_id, source, last_updated) VALUES ('B', 'b', 'UC2', 't', ?)", (NOW,))
        for aid, ch, at in ((1, "UC1", "2026-08-01T00:00:00Z"), (2, "UC2", "2026-10-01T00:00:00Z")):
            db.run("INSERT INTO artist_youtube_current (artist_id, channel_id, subscriber_count, fetched_at, source, last_updated)"
                   " VALUES (?, ?, 1, ?, 'youtube', ?)", (aid, ch, at, at))
        with mock.patch.object(youtube_job, "env", return_value=""):
            youtube_job.run(db, {"api_calls": 0, "rows_written": 0}, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual([r["artist_id"] for r in db.query("SELECT artist_id FROM artist_youtube_current")], [2])  # 62 days old: deleted


class PriceImport(unittest.TestCase):
    def row(self, **over):
        base = {"event": "1B00648D9DB09AA5", "kind": "resale", "basis": "ask", "price_point": "get_in", "section_tier": "",
                "standard": "", "price": "$145", "fees_included": "yes", "observed_on": "2026-10-02", "source": "StubHub", "notes": ""}
        return {**base, **over}

    def test_validation(self):
        self.assertEqual(import_prices.parse_row(self.row())["price"], 145.0)
        for bad in ({"kind": "rent"}, {"price": "abc"}, {"price": "0"}, {"observed_on": "10/2/2026"}, {"source": ""},
                    {"price_point": "get_in", "basis": "sold"}, {"kind": "face", "price_point": "get_in"},
                    {"price_point": "single", "basis": ""}, {"standard": "maybe"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                import_prices.parse_row(self.row(**bad))
        face = import_prices.parse_row(self.row(kind="face", basis="ask", price_point="single"))
        self.assertIsNone(face["price_basis"])

    def test_event_reference(self):
        self.assertEqual(import_prices.event_ref_id("https://www.ticketmaster.com/foo/event/1B00648D9DB09AA5?x=1"), "1B00648D9DB09AA5")
        self.assertEqual(import_prices.event_ref_id("1B00648D9DB09AA5"), "1B00648D9DB09AA5")

    def test_import_matches_dedupes_and_queues_unknown_events(self):
        db = temp_db()
        db.run("INSERT INTO events (ticketmaster_id, name, first_seen_at, last_seen_at, source, last_updated)"
               " VALUES ('1B00648D9DB09AA5', 'Show', ?, ?, 't', ?)", (NOW, NOW, NOW))
        path = Path(tempfile.mkdtemp()) / "prices.csv"
        rows = [self.row(), self.row(event="UNKNOWN123", price="99"), self.row(price="")]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        result = import_prices.import_file(db, path)
        self.assertEqual((result["imported"], result["unmatched"], len(result["errors"])), (2, 1, 1))
        import_prices.import_file(db, path)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM observed_prices"), 2)
        self.assertEqual(db.scalar("SELECT get_in FROM v_event_get_in"), 145.0)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM match_review WHERE kind = 'observed_price'"), 1)


class DbHelpers(unittest.TestCase):
    def test_upsert_never_overwrites_with_null(self):
        db = temp_db()
        db.run(*upsert("venues", ("ticketmaster_id",), {"ticketmaster_id": "V1", "name": "X", "name_key": "x", "capacity": 900, "source": "t", "last_updated": NOW}))
        db.run(*upsert("venues", ("ticketmaster_id",), {"ticketmaster_id": "V1", "name": "X2", "name_key": "x2", "capacity": None, "source": "t", "last_updated": NOW}))
        self.assertEqual(db.query("SELECT name, capacity FROM venues")[0], {"name": "X2", "capacity": 900})

    def test_coverage_report_on_empty_db(self):
        out = report(temp_db())
        self.assertIn("Coverage report", out)
        self.assertIn("Wikipedia title", out)


if __name__ == "__main__":
    unittest.main()
