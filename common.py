"""Shared helpers: paths, secrets, HTTP, CSV tables, and time parsing."""

from __future__ import annotations

import csv
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
DB_DIR = ROOT / "db"
CONFIG_PATH = ROOT / "config" / "model.json"
VENUES_MANUAL_PATH = ROOT / "config" / "venues.csv"
PRESALES_PATH = ROOT / "data" / "presales.json"

SECRET_NAMES = (
    "TM_API_KEY",
    "SEATGEEK_CLIENT_ID",
    "SEATGEEK_CLIENT_SECRET",
    "LASTFM_API_KEY",
    "CLOUDFLARE_D1_TOKEN",
)

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


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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
                    # Never retry faster than exponential backoff, even if Retry-After says 0
                    # (MusicBrainz sends that while it's rate limiting us).
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


# ---- CSV tables ---------------------------------------------------------------

def read_table(name: str) -> list[dict]:
    return read_csv(DB_DIR / f"{name}.csv")


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_table(name: str, rows: list[dict], fields: list[str]) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    path = DB_DIR / f"{name}.csv"
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: fmt_cell(row.get(k)) for k in fields})
    tmp.replace(path)


def append_table(name: str, rows: list[dict], fields: list[str]) -> None:
    """Append rows, writing the header if the file is new.

    If the saved file has different columns (a field was renamed), it's rewritten with
    the new header first, so old rows keep their shared columns instead of misaligning.
    """
    DB_DIR.mkdir(parents=True, exist_ok=True)
    path = DB_DIR / f"{name}.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh), [])
        if header and header != fields:
            write_table(name, read_table(name) + rows, fields)
            return
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: fmt_cell(row.get(k)) for k in fields})


def fmt_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value == value else ""
    return str(value)


def num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value) -> int | None:
    n = num(value)
    return None if n is None else int(n)


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


def hours_since(value: str | None, now: datetime) -> float:
    dt = parse_utc(value)
    return float("inf") if dt is None else (now - dt).total_seconds() / 3600


def safe_url(value: str | None) -> str | None:
    return value if value and value.startswith(("https://", "http://")) else None


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
