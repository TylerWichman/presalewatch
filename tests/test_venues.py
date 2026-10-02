"""Venue enrichment tests: matching, capacity parsing, infobox extraction, and the database rules
(hand-entered values win, disagreements go to review, processing order). No network calls."""

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ingest import venue_enrichment as ve
from ingest import venue_handfill
from ingest.db import SqliteDatabase
from ingest.resolve import haversine_km
from ingest.venue_rules import classify, is_venue, parse_capacity, pick_confident, venue_type

NOW = "2026-10-02T12:00:00Z"
FOX = {"id": 1, "name": "Fox Theater - Oakland", "latitude": 37.80815, "longitude": -122.27077,
       "capacity": None, "capacity_source": None}


def temp_db() -> SqliteDatabase:
    return SqliteDatabase(Path(tempfile.mkdtemp()) / "t.db")


class Matching(unittest.TestCase):
    def test_distance(self):
        self.assertAlmostEqual(haversine_km(40.7505, -73.9934, 40.6826, -73.9754), 7.7, delta=0.2)

    def test_confident_needs_300m_name_and_type(self):
        ok = dict(name="Fox Theater - Oakland", lat=37.80815, lon=-122.27077, cand_names=["Fox Oakland Theatre"],
                  cand_lat=37.8079, cand_lon=-122.2701)
        self.assertEqual(classify(**ok, type_ok=True)[0], "confident")
        self.assertEqual(classify(**ok, type_ok=False)[0], "review")                    # name but not a venue
        far = {**ok, "cand_lat": 37.8110, "cand_lon": -122.2701}                        # ~400 m
        self.assertEqual(classify(**far, type_ok=True)[0], "review")
        self.assertEqual(classify(**{**ok, "cand_lat": 37.83}, type_ok=True)[0], "no")  # ~2.4 km

    def test_close_but_different_name(self):
        close = dict(name="The Theater at MSG", lat=40.7505, lon=-73.9934, cand_lat=40.7506, cand_lon=-73.9935)
        self.assertEqual(classify(**close, cand_names=["Pennsylvania Station"], type_ok=False)[0], "no")
        self.assertEqual(classify(**close, cand_names=["Madison Square Garden"], type_ok=True)[0], "review")

    def test_disambiguated_titles_and_spelling(self):
        self.assertEqual(classify(name="Neptune Theatre", lat=None, lon=None, cand_names=["Neptune Theatre (Seattle)"],
                                  cand_lat=None, cand_lon=None, type_ok=True, meters=40)[0], "confident")

    def test_no_coordinates_never_matches(self):
        self.assertEqual(classify(name="X", lat=None, lon=None, cand_names=["X"], cand_lat=1, cand_lon=1, type_ok=True)[0], "no")

    def test_venue_type(self):
        self.assertEqual(venue_type(["indoor arena"]), "arena")
        self.assertEqual(venue_type(["Infobox stadium"]), "stadium")
        self.assertEqual(venue_type(["theatre"]), "theater")
        self.assertEqual(venue_type(["nightclub"]), "club")
        self.assertEqual(venue_type(["amphitheatre"]), "amphitheater")
        self.assertFalse(is_venue(["railway station", "street"]))

    def test_tie_breakers(self):
        def c(i, m, cap=None, title=None):
            return {"verdict": "confident", "meters": m, "source_id": i, "capacity_raw": cap, "wikipedia_title": title}
        self.assertEqual(pick_confident([c("A", 100), c("B", 120, cap="2,800")])["source_id"], "B")
        self.assertEqual(pick_confident([c("A", 100, title="Fox Oakland Theatre"), c("B", 120)])["source_id"], "A")
        self.assertEqual(pick_confident([c("A", 40), c("B", 200)])["source_id"], "A")        # much closer
        self.assertIsNone(pick_confident([c("A", 100), c("B", 120)]))                         # still tied


class CapacityParsing(unittest.TestCase):
    def check(self, raw, capacity, confidence):
        got = parse_capacity(raw)
        self.assertIsNotNone(got, raw)
        self.assertEqual((got["capacity"], got["confidence"]), (capacity, confidence), raw)

    def test_plain(self):
        self.check("2,800", 2800, "high")
        self.check("{{formatnum:2195}}", 2195, "high")
        self.check("1,200<ref name=x>{{cite web|title=Capacity 9,999}}</ref>", 1200, "high")

    def test_concert_value_preferred(self):
        self.check("20,000 (concerts)<br>18,500 (basketball)", 20000, "high")
        self.check("6,500 (basketball)<br />10,000 (concerts)", 10000, "high")
        self.check("{{plainlist|\n* Basketball: 19,722\n* Ice hockey: 18,700\n* Concerts: 20,000}}", 20000, "high")

    def test_several_concert_values_take_largest(self):
        self.check("Concerts: 21,000 (end stage)<br>23,000 (center stage concerts)", 23000, "medium")

    def test_unlabeled_several_take_largest(self):
        self.check("Seated: 2,195<br />Standing: 3,000", 3000, "medium")
        self.check("17,600 (seated); 20,000 (standing)", 20000, "medium")

    def test_low_confidence(self):
        self.check("1,500–2,000", 2000, "low")       # a range
        self.check("250,000", 250000, "low")         # implausible

    def test_approximate_and_years(self):
        self.check("approx. 5,000", 5000, "medium")
        self.check("2,800 (1928)", 2800, "high")
        self.check("Opened in 1928, 2,800 seats", 2800, "high")

    def test_nothing(self):
        for raw in ("", "TBD", "5", None):
            self.assertIsNone(parse_capacity(raw), raw)


class Infobox(unittest.TestCase):
    WIKITEXT = """{{Short description|Theater in Oakland}}
{{Infobox venue
| name = Fox Oakland Theatre
| image = [[File:Fox.jpg|thumb|The marquee]]
| address = 1807 Telegraph Ave
| type = [[Theatre]]
| capacity = 2,800<ref>{{cite web|url=https://x|title=About}}</ref>
| opened = {{Start date|1928|10|27}}
| operator = [[Another Planet Entertainment]]
}}
'''The Fox Oakland Theatre''' is a ..."""

    def test_params(self):
        template, params = ve.infobox_params(self.WIKITEXT)
        self.assertEqual(template, "venue")
        self.assertEqual(params["name"], "Fox Oakland Theatre")
        self.assertTrue(params["capacity"].startswith("2,800"))
        self.assertIn("thumb", params["image"])  # nested link kept inside its parameter

    def test_candidate(self):
        c = ve.wikipedia_candidate("Fox Oakland Theatre", 150.0, self.WIKITEXT)
        self.assertEqual(ve.candidate_capacity(c)["capacity"], 2800)
        self.assertEqual(c["operator"], "Another Planet Entertainment")
        self.assertEqual(c["url"], "https://en.wikipedia.org/wiki/Fox_Oakland_Theatre")
        self.assertIsNone(ve.wikipedia_candidate("No box", 10, "Just text, no infobox."))


class WikidataParsing(unittest.TestCase):
    ENTITY = {
        "sitelinks": {"enwiki": {"title": "Mississippi Coliseum"}},
        "claims": {
            "P31": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q641226"}}}}],
            "P1083": [
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"amount": "+6500"}}},
                 "qualifiers": {"P518": [{"datavalue": {"value": {"id": "Q5372"}}}]}},
                {"rank": "normal", "mainsnak": {"datavalue": {"value": {"amount": "+10000"}}},
                 "qualifiers": {"P518": [{"datavalue": {"value": {"id": "Q182832"}}}]}},
                {"rank": "deprecated", "mainsnak": {"datavalue": {"value": {"amount": "+99999"}}}},
            ],
            "P1619": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"time": "+1962-11-27T00:00:00Z", "precision": 11}}}}],
        },
    }
    LABELS = {"Q641226": "arena", "Q5372": "basketball", "Q182832": "concert"}

    def test_candidate(self):
        item = {"qid": "Q6878271", "label": "Mississippi Coliseum", "aliases": [], "lat": 32.32, "lon": -90.17}
        c = ve.wikidata_candidate(item, self.ENTITY, self.LABELS)
        self.assertEqual(ve.candidate_capacity(c), {"capacity": 10000, "label": "concert", "confidence": "high",
                                                    "reason": "the value labeled for concerts"})
        self.assertEqual((c["opened"], c["wikipedia_title"], venue_type(c["type_texts"])), ("1962-11-27", "Mississippi Coliseum", "arena"))
        self.assertNotIn(99999, [v["n"] for v in c["capacity_values"]])  # deprecated value ignored

    def test_parse_point(self):
        self.assertEqual(ve.parse_point("Point(-73.99 40.75)"), (40.75, -73.99))
        self.assertEqual(ve.parse_point(None), (None, None))


class Evaluate(unittest.TestCase):
    def cand(self, source_id, names, lat, lon, types, raw=None, title=None):
        return {"source": "openstreetmap", "source_id": source_id, "names": names, "lat": lat, "lon": lon,
                "type_texts": types, "capacity_raw": raw, "wikipedia_title": title}

    def test_match_and_capacity(self):
        r = ve.evaluate(FOX, [self.cand("way/1", ["Fox Oakland Theatre"], 37.8079, -122.2701, ["theatre"], "2800"),
                              self.cand("way/2", ["Starbucks"], 37.8081, -122.2708, ["cafe"])])
        self.assertEqual((r["match"]["source_id"], r["capacity"]["capacity"], r["review"]), ("way/1", 2800, []))

    def test_uncertain_goes_to_review(self):
        r = ve.evaluate(FOX, [self.cand("way/1", ["Fox Oakland Theatre"], 37.8079, -122.2701, ["office"])])
        self.assertIsNone(r["match"])
        self.assertEqual([c["source_id"] for c in r["review"]], ["way/1"])


class FakeFetch:
    """Stands in for the network: one OSM venue for the Fox, nothing anywhere else."""
    calls = 0

    def wikidata_candidates(self, lat, lon):
        return []

    def wikidata_entities(self, qids, props="", languages=None):
        return {}

    def labels(self, qids):
        return {}

    def wikipedia_nearby(self, lat, lon):
        return []

    def wikipedia_lead(self, title):
        return ""

    def osm_nearby(self, lat, lon):
        if abs(lat - 37.80815) < 0.01:
            return [{"osm_id": "way/1", "tags": {"name": "Fox Oakland Theatre", "amenity": "theatre", "capacity": "2800"},
                     "lat": 37.8079, "lon": -122.2701}]
        return []

    def ticketmaster_venue(self, venue_id):
        return {"timezone": "America/Los_Angeles", "url": "https://www.ticketmaster.com/fox"}


class Database(unittest.TestCase):
    def setUp(self):
        self.db = temp_db()
        venues = [("KV_FOX", "Fox Theater - Oakland", 37.80815, -122.27077, 3), ("KV_X", "Mystery Room", 40.0, -100.0, 5),
                  ("KovZpZAdt7AA", "The Stone Pony", 40.22, -74.0, 1)]
        for tm, name, lat, lon, n in venues:
            self.db.run("INSERT INTO venues (ticketmaster_id, name, name_key, latitude, longitude, source, last_updated)"
                        " VALUES (?, ?, ?, ?, ?, 't', ?)", (tm, name, name.lower(), lat, lon, NOW))
            vid = self.db.scalar("SELECT id FROM venues WHERE ticketmaster_id = ?", (tm,))
            for i in range(n):
                self.db.run("INSERT INTO events (ticketmaster_id, name, venue_id, event_date, first_seen_at, last_seen_at, source, last_updated)"
                            " VALUES (?, 'S', ?, '2099-01-0' || ?, ?, ?, 't', ?)", (f"{tm}-{i}", vid, i + 1, NOW, NOW, NOW))

    def run_job(self, when="2026-10-02"):
        with mock.patch.object(ve, "Fetch", FakeFetch):
            ve.run(self.db, {"api_calls": 0, "rows_written": 0}, now=datetime.fromisoformat(when).replace(tzinfo=timezone.utc))

    def venue(self, tm):
        return self.db.query("SELECT * FROM venues WHERE ticketmaster_id = ?", (tm,))[0]

    def test_manual_capacity_applied_and_never_overwritten(self):
        self.run_job()
        pony = self.venue("KovZpZAdt7AA")  # in config/venues.csv
        self.assertEqual((pony["capacity"], pony["capacity_source"], pony["capacity_verified"]), (850, "manual", 1))
        self.db.run("UPDATE venues SET capacity_checked_at = '2000-01-01T00:00:00Z' WHERE ticketmaster_id = 'KovZpZAdt7AA'")
        due = [v["ticketmaster_id"] for v in ve.due(self.db, datetime(2026, 10, 2, tzinfo=timezone.utc), 10)]
        self.assertNotIn("KovZpZAdt7AA", due)  # verified venues are never re-processed

    def test_resolves_writes_details_and_records_observation(self):
        self.run_job()
        fox = self.venue("KV_FOX")
        self.assertEqual((fox["capacity"], fox["capacity_source"], fox["capacity_confidence"], fox["capacity_raw"]),
                         (2800, "openstreetmap", "high", "2800"))
        self.assertEqual((fox["venue_type"], fox["osm_id"], fox["timezone"]), ("theater", "way/1", "America/Los_Angeles"))
        self.assertEqual(self.db.scalar("SELECT parsed_capacity FROM venue_capacity_observations WHERE venue_id = ?", (fox["id"],)), 2800)
        mystery = self.venue("KV_X")
        self.assertIsNone(mystery["capacity"])
        self.assertIsNotNone(mystery["capacity_checked_at"])

    def test_order_new_first_then_busiest(self):
        order = [v["ticketmaster_id"] for v in ve.due(self.db, datetime(2026, 10, 2, tzinfo=timezone.utc), 10)]
        self.assertEqual(order[:2], ["KV_X", "KV_FOX"])  # 5 upcoming events before 3

    def test_retry_and_reverify_schedule(self):
        self.run_job("2026-10-02")
        due = lambda d: {v["ticketmaster_id"] for v in ve.due(self.db, datetime.fromisoformat(d).replace(tzinfo=timezone.utc), 10)}  # noqa: E731
        self.assertEqual(due("2026-10-20"), set())                 # nothing due yet
        self.assertEqual(due("2026-11-05"), {"KV_X"})              # unresolved: retried after 30 days
        self.assertEqual(due("2027-04-05"), {"KV_X", "KV_FOX"})    # filled: re-verified after 180 days

    def test_reverify_disagreement_goes_to_review(self):
        self.run_job("2026-10-02")
        self.db.run("UPDATE venues SET capacity = 1500 WHERE ticketmaster_id = 'KV_FOX'")
        self.run_job("2027-04-05")
        self.assertEqual(self.venue("KV_FOX")["capacity"], 1500)    # not overwritten
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM match_review WHERE kind = 'venue_capacity'"), 1)

    def test_source_down_is_retried_next_run(self):
        class Down(FakeFetch):
            def osm_nearby(self, lat, lon):
                raise ve.ApiError("OpenStreetMap Overpass", 504, "timeout")
        with mock.patch.object(ve, "Fetch", Down):
            ve.run(self.db, {"api_calls": 0, "rows_written": 0}, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertIsNone(self.venue("KV_X")["capacity_checked_at"])      # not stamped: retried next run
        self.assertIsNone(self.venue("KV_FOX")["capacity"])
        self.run_job("2026-10-03")                                          # source back up
        self.assertEqual(self.venue("KV_FOX")["capacity"], 2800)

    def test_handfill_export_and_import(self):
        self.run_job()
        out = Path(tempfile.mkdtemp()) / "fill.csv"
        self.assertEqual(venue_handfill.export(self.db, out), 1)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["ticketmaster_id"], "KV_X")
        rows[0]["capacity"] = "1,250"
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        self.assertEqual(venue_handfill.import_file(self.db, out), {"filled": 1, "errors": []})
        v = self.venue("KV_X")
        self.assertEqual((v["capacity"], v["capacity_source"], v["capacity_verified"]), (1250, "manual", 1))


if __name__ == "__main__":
    unittest.main()
