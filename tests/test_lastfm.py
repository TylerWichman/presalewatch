import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import lastfm
import ticketmaster
from common import ApiError


def client(response):
    lf = lastfm.LastFm("test-key")
    lf.http.get_json = mock.Mock(side_effect=[response] if not isinstance(response, list) else response)
    return lf


class ArtistStats(unittest.TestCase):
    def test_found(self):
        lf = client({"artist": {"name": "Hilary Duff", "stats": {"listeners": "1523456", "playcount": "61234567"}}})
        self.assertEqual(lastfm.artist_stats(lf, "Hilary Duff"),
                         {"lastfm_name": "Hilary Duff", "lastfm_listeners": 1523456, "lastfm_playcount": 61234567})
        url = lf.http.get_json.call_args[0][0]
        self.assertIn("method=artist.getinfo", url)
        self.assertIn("autocorrect=1", url)

    def test_autocorrect_to_a_different_act_is_rejected(self):
        lf = client({"artist": {"name": "Face to Face", "stats": {"listeners": "337066", "playcount": "9000000"}}})
        self.assertIsNone(lastfm.artist_stats(lf, "Face 2 Face")["lastfm_listeners"])
        lf = client({"artist": {"name": "Face to Face", "stats": {"listeners": "337066", "playcount": "9000000"}}})
        self.assertEqual(lastfm.artist_stats(lf, "Face 2 Face", trusted=True)["lastfm_listeners"], 337066)

    def test_autocorrect_spelling_variants_are_accepted(self):
        for asked, got in (("Four Tops", "The Four Tops"), ("Emerson Lake and Palmer", "Emerson, Lake & Palmer"),
                           ("Tommy James and the Shondells", "Tommy James & The Shondells"),
                           ("Queensryche", "Queensrÿche")):
            lf = client({"artist": {"name": got, "stats": {"listeners": "500000", "playcount": "9000000"}}})
            self.assertEqual(lastfm.artist_stats(lf, asked)["lastfm_listeners"], 500000, asked)

    def test_not_found_is_none_not_an_error(self):
        lf = client({"error": 6, "message": "The artist you supplied could not be found"})
        self.assertEqual(lastfm.artist_stats(lf, "Roger Waters Presents LEGACY"),
                         {"lastfm_name": None, "lastfm_listeners": None, "lastfm_playcount": None})

    def test_not_found_as_http_error(self):
        lf = lastfm.LastFm("test-key")
        lf.http.get_json = mock.Mock(side_effect=ApiError("Last.fm", 404, '{"error": 6, "message": "not found"}'))
        self.assertIsNone(lf.artist_info("Nobody"))

    def test_zero_or_junk_counts_count_as_missing(self):
        lf = client({"artist": {"name": "X", "stats": {"listeners": "0", "playcount": "10"}}})
        self.assertEqual(lastfm.artist_stats(lf, "X")["lastfm_listeners"], None)
        lf = client({"artist": {"name": "X", "stats": {"listeners": "abc"}}})
        self.assertEqual(lastfm.artist_stats(lf, "X")["lastfm_listeners"], None)

    def test_key_and_rate_errors_raise_so_the_run_stops(self):
        for code, status in ((10, 403), (26, 403), (29, 429)):
            with self.assertRaises(ApiError) as ctx:
                client({"error": code, "message": "nope"}).artist_info("X")
            self.assertEqual(ctx.exception.status, status)

    def test_missing_key_disables_client(self):
        with mock.patch.object(lastfm, "env", return_value=""):
            self.assertIsNone(lastfm.LastFm.from_env())


class TicketmasterLastfmLink(unittest.TestCase):
    def test_name_from_external_link(self):
        a = {"externalLinks": {"lastfm": [{"url": "https://www.last.fm/music/Guns+N%27+Roses"}]}}
        self.assertEqual(ticketmaster.lastfm_name(a), "Guns N' Roses")
        self.assertIsNone(ticketmaster.lastfm_name({"externalLinks": {"spotify": [{"url": "x"}]}}))
        self.assertIsNone(ticketmaster.lastfm_name({}))


class AppendTableMigration(unittest.TestCase):
    def test_renamed_columns_rewrite_the_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, "DB_DIR", Path(tmp)):
            common.append_table("predictions", [{"event_id": "E1", "popularity": "0.5", "market": "1"}], ["event_id", "popularity", "market"])
            common.append_table("predictions", [{"event_id": "E2", "listeners": "0.7", "market": "0.5"}], ["event_id", "listeners", "market"])
            with open(Path(tmp) / "predictions.csv", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0].keys()), ["event_id", "listeners", "market"])
            self.assertEqual([(r["event_id"], r["listeners"], r["market"]) for r in rows], [("E1", "", "1"), ("E2", "0.7", "0.5")])


if __name__ == "__main__":
    unittest.main()
