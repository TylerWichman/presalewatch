"""GET requests to Wikidata and Wikipedia APIs that handle their in-band errors.

These APIs answer errors with HTTP 200 and an {"error": ...} body. The important one is
"maxlag": we ask the servers to refuse work while their replicas lag (good API etiquette), and
they do, so the request must wait and retry. Treating that body as "no data" silently drops
real data, so any error here either retries or raises; it's never returned as an empty result.

Wikidata also counts its Query Service (SPARQL) lag toward maxlag, to slow down editors. That
service can run minutes behind for hours, and our reads never touch it, so lag of that type
(error type "wikibase-queryservice") resends the request without maxlag instead of waiting.
"""

from __future__ import annotations

import time
import urllib.parse

from common import ApiError, Http

MAX_TRIES = 6


def get(http: Http, url: str, headers: dict) -> dict:
    for attempt in range(MAX_TRIES):
        data = http.get_json(url, headers=headers)
        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            return data
        if err.get("code") == "maxlag" and err.get("type") == "wikibase-queryservice":
            url = without_maxlag(url)  # the replicas we read from are fine
            continue
        if err.get("code") == "maxlag":
            # Wait at least as long as the reported lag, backing off on repeats.
            lag = float(err.get("lag") or 5)
            time.sleep(min(max(lag, 5) * (attempt + 1), 60))
            continue
        raise ApiError(http.source, 400, f"API error {err.get('code')}: {str(err.get('info'))[:150]}")
    raise ApiError(http.source, 503, "servers stayed lagged (maxlag); try again later")


def without_maxlag(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "maxlag"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
