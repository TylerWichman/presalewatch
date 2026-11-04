import unittest
from datetime import datetime, timezone

import build
import pipeline
import ticketmaster

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def raw_event(**over):
    ev = {
        "id": "E1", "name": "Big Star Tour", "url": "https://www.ticketmaster.com/e/E1",
        "dates": {"start": {"localDate": "2026-12-01", "localTime": "19:30:00"}},
        "sales": {
            "public": {"startDateTime": "2026-10-10T14:00:00Z"},
            "presales": [
                {"name": "Artist Presale", "startDateTime": "2026-10-03T14:00:00Z", "endDateTime": "2026-10-04T14:00:00Z"},
                {"name": "Old Presale", "startDateTime": "2026-09-01T14:00:00Z", "endDateTime": "2026-09-02T14:00:00Z"},
            ],
        },
        "_embedded": {
            "venues": [{"name": "V", "city": {"name": "New York"}, "state": {"stateCode": "NY"}}],
            "attractions": [{"id": "A1", "name": "Big Star"}],
        },
    }
    ev.update(over)
    return ev


class PresaleTypes(unittest.TestCase):
    def test_classification(self):
        cases = {
            "Artist Presale": "Artist", "LIVE NATION PRESALE": "Live Nation", "Citi® Cardmember Presale": "Citi",
            "Reserved by Spotify": "Spotify", "Amex Presale Tickets": "Amex", "Artist Presale VIP Package": "VIP",
            "Venue Presale": "Venue", "Platinum Onsale": "Platinum", "K-LOVE Presale": "Radio / Local",
        }
        for name, kind in cases.items():
            self.assertEqual(ticketmaster.presale_type(name)[0], kind, name)


class ParseAndList(unittest.TestCase):
    def test_keeps_only_presales_that_have_not_ended(self):
        p = ticketmaster.parse_event(raw_event(), NOW)
        self.assertEqual([ps["name"] for ps in p["presales"]], ["Artist Presale"])
        self.assertEqual((p["event"]["artist"], p["event"]["city"], p["event"]["event_date"]), ("Big Star", "New York", "2026-12-01"))

    def test_events_without_open_presales_are_skipped(self):
        self.assertIsNone(ticketmaster.parse_event(raw_event(sales={"presales": []}), NOW))
        self.assertIsNone(ticketmaster.parse_event(raw_event(test=True), NOW))

    def test_rows_sort_by_soonest_presale(self):
        later = raw_event(id="E2", sales={"presales": [
            {"name": "Venue Presale", "startDateTime": "2026-10-08T14:00:00Z", "endDateTime": "2026-10-09T14:00:00Z"}]})
        parsed = [ticketmaster.parse_event(ev, NOW) for ev in (later, raw_event())]
        rows = pipeline.presale_rows(parsed)
        self.assertEqual([r["event_id"] for r in rows], ["E1", "E2"])


class PageData(unittest.TestCase):
    def test_page_data_is_listing_facts_only(self):
        rows = pipeline.presale_rows([ticketmaster.parse_event(raw_event(), NOW)])
        out = build.compact({"generated_at": "g", "presales": rows})
        self.assertEqual(set(out), {"generated_at", "events", "rows"})
        self.assertEqual(set(out["events"][0]), {"a", "ai", "n", "u", "v", "c", "s", "d", "t", "o"})
        self.assertEqual((out["events"][0]["a"], out["events"][0]["v"], len(out["rows"])), ("Big Star", "V", 1))
        feed = build.alerts_feed({"generated_at": "g", "presales": rows})
        self.assertEqual(set(feed["events"][0]), {"id", "artist", "name", "url", "venue", "city", "state", "date", "presale_end"})


if __name__ == "__main__":
    unittest.main()
