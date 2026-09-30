import json
import unittest

import build

PAGE = """<!doctype html><html><head><style>body{color:red}</style></head><body>
<script id="presale-data" type="application/json">{"a":1}</script>
<script>console.log("hi")</script></body></html>"""


class SecurityHeaders(unittest.TestCase):
    def setUp(self):
        self.headers = build.headers_file(PAGE)
        self.csp = next(line for line in self.headers.splitlines() if "Content-Security-Policy" in line)

    def test_csp_is_strict(self):
        self.assertNotIn("unsafe-inline", self.csp)
        self.assertNotIn("unsafe-eval", self.csp)
        for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"):
            self.assertIn(directive, self.csp)

    def test_inline_blocks_allowed_by_hash_only(self):
        self.assertIn(build.csp_hash('console.log("hi")'), self.csp)
        self.assertIn(build.csp_hash("body{color:red}"), self.csp)
        self.assertNotIn(build.csp_hash('{"a":1}'), self.csp)  # JSON data isn't executable

    def test_common_headers(self):
        for header in ("Strict-Transport-Security", "X-Content-Type-Options: nosniff", "Referrer-Policy",
                       "X-Frame-Options: DENY", "Permissions-Policy"):
            self.assertIn(header, self.headers)


class AlertsFeed(unittest.TestCase):
    def row(self, **over):
        base = {"event_id": "E1", "artist": "A", "event_name": "A", "url": "https://www.ticketmaster.com/e", "venue": "V",
                "city": "C", "state": "NY", "event_date": "2026-12-01", "mode": "predicted", "profit": None,
                "profit_low": -0.1, "profit_high": 0.3, "edge_sort": 0.1, "tier": "Med", "confidence": "Low",
                "presale_end": "2026-10-01T00:00:00Z"}
        return {**base, **over}

    def test_one_row_per_event_with_latest_presale_end(self):
        feed = build.alerts_feed({"generated_at": "g", "presales": [
            self.row(), self.row(presale_end="2026-10-09T00:00:00Z"), self.row(event_id="E2", presale_end=None)]})
        self.assertEqual(feed["v"], 1)
        self.assertEqual([e["id"] for e in feed["events"]], ["E1", "E2"])
        self.assertEqual(feed["events"][0]["presale_end"], "2026-10-09T00:00:00Z")
        self.assertNotIn("presale_end", feed["events"][1])
        self.assertNotIn("name", feed["events"][0])  # same as the artist
        json.dumps(feed)


if __name__ == "__main__":
    unittest.main()
