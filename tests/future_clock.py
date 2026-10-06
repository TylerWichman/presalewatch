"""Run the Python test suite as if today were a later date, to catch tests that break with time.

A test that mixes a fixed date with the real clock passes today and fails weeks later; on
Oct 6, 2026 one such test blocked the page refresh. This replaces `datetime` (and `date`) in
the project's modules with versions whose now()/today() return a chosen date, then runs every
test. Dates are relative to the real today, so the check keeps looking ahead.

Usage:
    python tests/future_clock.py                 # +60 days, +1 year, +2 years
    python tests/future_clock.py 2027-06-01      # specific dates (UTC noon)

Not a test module itself (unittest's discovery only picks up test*.py).
"""

from __future__ import annotations

import datetime as real_dt
import importlib
import io
import pkgutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = ["common", "edge", "pipeline", "build", "calibrate", "lastfm"]
OFFSETS_DAYS = [60, 365, 730]


def patch_clock(when: real_dt.datetime) -> None:
    class FakeDatetime(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return when.replace(tzinfo=None)

    class FakeDate(real_dt.date):
        @classmethod
        def today(cls):
            return when.date()

    import ingest

    names = MODULES + [f"ingest.{m.name}" for m in pkgutil.iter_modules(ingest.__path__)]
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        # The real classes, or the fakes from an earlier date in this run.
        current = getattr(module, "datetime", None)
        if isinstance(current, type) and issubclass(current, real_dt.datetime):
            module.datetime = FakeDatetime
        current = getattr(module, "date", None)
        if isinstance(current, type) and issubclass(current, real_dt.date) and not issubclass(current, real_dt.datetime):
            module.date = FakeDate


def run_at(when: real_dt.datetime) -> unittest.TestResult:
    patch_clock(when)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)


def main() -> int:
    if len(sys.argv) > 1:
        dates = [real_dt.datetime.fromisoformat(d).replace(hour=12, tzinfo=real_dt.timezone.utc) for d in sys.argv[1:]]
    else:
        today = real_dt.datetime.now(real_dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        dates = [today + real_dt.timedelta(days=d) for d in OFFSETS_DAYS]
    failed = False
    for when in dates:
        result = run_at(when)
        bad = result.failures + result.errors
        print(f"{when:%Y-%m-%d}: {result.testsRun} tests, {len(bad)} failing")
        for test, tb in bad:
            failed = True
            print(f"  {test.id()}: {tb.strip().splitlines()[-1][:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
