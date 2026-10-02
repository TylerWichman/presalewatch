"""The Wikidata/Wikipedia helper must never turn an in-band API error into an empty result."""

import unittest
from unittest import mock

from common import ApiError, Http
from ingest import mediawiki, wikidata_job

LAGGED = {"error": {"code": "maxlag", "info": "Waiting for wdqs1013: 5.2 seconds lagged.", "lag": 5.2}}
OK = {"entities": {"Q1": {"id": "Q1"}}}


class MaxLag(unittest.TestCase):
    def test_waits_and_retries_then_returns_data(self):
        http = Http("Wikidata", 0)
        with mock.patch.object(http, "get_json", side_effect=[LAGGED, LAGGED, OK]) as g, \
                mock.patch.object(mediawiki.time, "sleep") as sleep:
            self.assertEqual(mediawiki.get(http, "u", {}), OK)
        self.assertEqual(g.call_count, 3)
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 5)

    def test_gives_up_with_an_error_not_an_empty_result(self):
        http = Http("Wikidata", 0)
        with mock.patch.object(http, "get_json", return_value=LAGGED), mock.patch.object(mediawiki.time, "sleep"):
            with self.assertRaises(ApiError) as ctx:
                mediawiki.get(http, "u", {})
        self.assertEqual(ctx.exception.status, 503)

    def test_other_api_errors_raise(self):
        http = Http("Wikipedia", 0)
        with mock.patch.object(http, "get_json", return_value={"error": {"code": "badvalue", "info": "nope"}}):
            with self.assertRaises(ApiError):
                mediawiki.get(http, "u", {})


class ArtistsNotUnlinkedDuringLag(unittest.TestCase):
    def test_entity_missing_from_response_is_skipped(self):
        from ingest.db import SqliteDatabase
        import tempfile
        from pathlib import Path
        db = SqliteDatabase(Path(tempfile.mkdtemp()) / "t.db")
        mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
        db.run("INSERT INTO artists (name, name_key, mbid, wikidata_id, source, last_updated) VALUES ('Radiohead', 'radiohead', ?, 'Q44190', 't', 'x')", (mbid,))
        with mock.patch.object(wikidata_job, "resolve_artist_items", return_value={}), \
                mock.patch.object(wikidata_job, "entities", return_value={}):
            wikidata_job.run(db, {"api_calls": 0, "rows_written": 0})
        self.assertEqual(db.scalar("SELECT wikidata_id FROM artists"), "Q44190")   # still linked
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM match_review"), 0)


if __name__ == "__main__":
    unittest.main()
