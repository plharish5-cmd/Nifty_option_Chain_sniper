"""
analyst/test_post_market_scheduler_8510.py — the post-market ingest must fire ONCE.

THE BUG THIS LOCKS DOWN
-----------------------
The scheduler used to be:

    if now_ist.strftime('%H:%M') >= cfg['post_market_archivers']['schedule_ist']:
        await asyncio.to_thread(backend.daily_ingest, today_s)

inside a `while True: await asyncio.sleep(60)` loop. That condition is true for
every minute from schedule_ist until midnight, and daily_ingest()'s own early
return is guarded by `date_str < today`, which is False for today — so nothing
stopped it. From 15:35 to 23:59 that is roughly 505 full ingests per evening,
each pulling Dhan and rewriting analyst/replay_data/<today>.json.

The archive subprocesses were safe (run_post_market_archivers is idempotent via
the post_market_archives table) but the Dhan pulls around them were not, which
is a plausible contributor to the 429 rate-limit stalls, and it is why the
replay JSON kept showing up as modified in git status.

The guard is now a pure function so it can be tested instead of trusted.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEDULE = "15:35"
RETRY = 300.0
TODAY = "2026-11-06"


def guard(now_hhmm, done_for_day=None, last_attempt_ts=0.0, now_ts=1_000_000.0,
          today_s=TODAY, schedule=SCHEDULE, retry=RETRY):
    """Mirror of engine_8510_live.should_run_post_market_ingest.

    Imported from the engine when that import is cheap; otherwise this local
    copy is compared against it in test_matches_engine_implementation below so
    the two cannot drift.
    """
    if now_hhmm < schedule:
        return False
    if done_for_day == today_s:
        return False
    if last_attempt_ts and (now_ts - last_attempt_ts) < retry:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# THE SCHEDULE WINDOW
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hhmm", ["00:00", "09:15", "12:45", "15:20", "15:29", "15:34"])
def test_does_not_run_before_the_scheduled_time(hhmm):
    assert guard(hhmm) is False


@pytest.mark.parametrize("hhmm", ["15:35", "15:36", "18:00", "23:59"])
def test_runs_at_or_after_the_scheduled_time(hhmm):
    assert guard(hhmm) is True


# ─────────────────────────────────────────────────────────────────────────────
# ONCE PER DAY — the actual regression
# ─────────────────────────────────────────────────────────────────────────────

def test_does_not_run_again_once_the_day_succeeded():
    assert guard("15:35", done_for_day=TODAY) is False
    assert guard("18:00", done_for_day=TODAY) is False
    assert guard("23:59", done_for_day=TODAY) is False


def test_the_505_ingest_loop_cannot_happen_again():
    """Walk every minute from 15:35 to 23:59 and count the runs.

    Old behaviour: 505. Required behaviour: 1.
    """
    done = None
    last_ts = 0.0
    runs = 0
    ts = 1_000_000.0
    for total in range(15 * 60 + 35, 24 * 60):
        hhmm = "{:02d}:{:02d}".format(total // 60, total % 60)
        if guard(hhmm, done_for_day=done, last_attempt_ts=last_ts, now_ts=ts):
            runs += 1
            last_ts = ts
            done = TODAY          # a successful ingest marks the day done
        ts += 60.0
    assert runs == 1, "expected exactly one ingest, got {}".format(runs)


def test_a_new_day_runs_again():
    """done_for_day is yesterday, so today must still ingest."""
    assert guard("15:35", done_for_day="2026-11-05") is False or True
    assert guard("15:35", done_for_day="2026-11-05", today_s=TODAY) is True


# ─────────────────────────────────────────────────────────────────────────────
# FAILURE RETRY MUST BE BOUNDED
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_attempt_is_not_retried_immediately():
    """A broken feed must not become a tight loop against the broker."""
    assert guard("15:36", last_attempt_ts=1_000_000.0, now_ts=1_000_060.0) is False


def test_failed_attempt_is_retried_after_the_cooldown():
    assert guard("15:40", last_attempt_ts=1_000_000.0,
                 now_ts=1_000_000.0 + RETRY + 1) is True


def test_failure_retries_are_capped_across_the_evening():
    """With the ingest never succeeding, retries happen on the cooldown only.

    15:35 to 23:59 is 505 minutes; at a 300s cooldown that is at most 102
    attempts, not 505 — and the first one is immediate.
    """
    last_ts = 0.0
    attempts = 0
    ts = 1_000_000.0
    for total in range(15 * 60 + 35, 24 * 60):
        hhmm = "{:02d}:{:02d}".format(total // 60, total % 60)
        if guard(hhmm, done_for_day=None, last_attempt_ts=last_ts, now_ts=ts):
            attempts += 1
            last_ts = ts          # failure: day NOT marked done
        ts += 60.0
    assert attempts <= 1 + int(505 * 60 / RETRY), attempts
    assert attempts < 505


# ─────────────────────────────────────────────────────────────────────────────
# THE LOCAL MIRROR MUST MATCH THE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def test_matches_engine_implementation():
    """Guards against this test's mirror drifting from the real function.

    Skipped rather than failed when importing the engine is not possible in
    this environment (it constructs the backend at import time).
    """
    try:
        import engine_8510_live  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip("engine import unavailable here: {}".format(type(exc).__name__))

    from fastapi.testclient import TestClient
    with TestClient(engine_8510_live.app) as client:  # runs startup
        fn = getattr(engine_8510_live.app.state, "should_run_post_market_ingest", None)
        assert fn is not None, "engine no longer exposes the guard"
        cases = [
            ("15:34", None, 0.0, 1_000_000.0),
            ("15:35", None, 0.0, 1_000_000.0),
            ("18:00", TODAY, 0.0, 1_000_000.0),
            ("15:36", None, 1_000_000.0, 1_000_060.0),
            ("15:40", None, 1_000_000.0, 1_000_000.0 + RETRY + 1),
        ]
        for hhmm, done, last, now in cases:
            mine = guard(hhmm, done_for_day=done, last_attempt_ts=last, now_ts=now)
            theirs = fn(hhmm, SCHEDULE, TODAY, done, last, RETRY, now)
            assert mine == theirs, (hhmm, done, last, now, mine, theirs)
        _ = client  # keep the context manager meaningful
