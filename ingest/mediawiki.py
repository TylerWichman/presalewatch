"""GET requests to Wikidata and Wikipedia APIs that handle their in-band errors.

These APIs answer errors with HTTP 200 and an {"error": ...} body. The important one is
"maxlag": we ask the servers to refuse work while their replicas lag (good API etiquette), and
they do, so the request must wait and retry. Treating that body as "no data" silently drops
real data, so any error here either retries or raises; it's never returned as an empty result.
"""

from __future__ import annotations

import time

from common import ApiError, Http

MAX_TRIES = 6


def get(http: Http, url: str, headers: dict) -> dict:
    for attempt in range(MAX_TRIES):
        data = http.get_json(url, headers=headers)
        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            return data
        if err.get("code") == "maxlag":
            # Wait at least as long as the reported lag, backing off on repeats.
            lag = float(err.get("lag") or 5)
            time.sleep(min(max(lag, 5) * (attempt + 1), 60))
            continue
        raise ApiError(http.source, 400, f"API error {err.get('code')}: {str(err.get('info'))[:150]}")
    raise ApiError(http.source, 503, "servers stayed lagged (maxlag); try again later")
