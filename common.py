"""Shared helpers: paths, secrets, HTTP, and time parsing."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRESALES_PATH = ROOT / "data" / "presales.json"

SECRET_NAMES = ("TM_API_KEY",)

_dotenv: dict[str, str] | None = None


def env(name: str) -> str:
    """Read a setting from the environment, falling back to a local .env file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    global _dotenv
    if _dotenv is None:
        _dotenv = {}
        path = ROOT / ".env"
        if path.exists():
            # utf-8-sig: tolerate the BOM that Windows editors add
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, val = line.partition("=")
                if sep:
                    _dotenv[key.strip()] = val.strip().strip('"').strip("'")
    return _dotenv.get(name, "")


def assert_no_secrets(text: str, what: str) -> None:
    for secret in (env(n) for n in SECRET_NAMES):
        if secret and secret in text:
            sys.exit(f"Refusing to write {what}: it appears to contain an API secret.")
    if "apikey=" in text.lower() or "client_secret=" in text.lower():
        sys.exit(f"Refusing to write {what}: it appears to contain an API key parameter.")


# ---- HTTP -------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, source: str, status: int, body: str):
        super().__init__(f"HTTP {status} from {source}: {body}")
        self.status = status


class Http:
    """Rate-limited JSON client. Errors never include the URL, since URLs can carry keys."""

    def __init__(self, source: str, min_interval: float, max_retries: int = 5):
        self.source = source
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.last_call = 0.0
        self.calls = 0

    def get_json(self, url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(url, data=data, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                if err.code == 429 or err.code >= 500:
                    # Never retry faster than exponential backoff, even if Retry-After says 0.
                    try:
                        after = float(err.headers.get("Retry-After") or 0)
                    except ValueError:
                        after = 0.0
                    delay = min(max(after, 2 ** (attempt + 1)), 60)
                    print(f"  {self.source}: HTTP {err.code}, retrying in {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                body = err.read().decode("utf-8", "replace")[:300]
                raise ApiError(self.source, err.code, body) from None
            except (urllib.error.URLError, TimeoutError) as err:
                delay = 2 ** (attempt + 1)
                print(f"  {self.source}: network error ({getattr(err, 'reason', err)}), retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
        raise ApiError(self.source, 0, "kept failing; giving up")


# ---- Time and text --------------------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def safe_url(value: str | None) -> str | None:
    return value if value and value.startswith(("https://", "http://")) else None


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
