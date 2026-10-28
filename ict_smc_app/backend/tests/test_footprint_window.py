"""
tests/test_footprint_window.py — the heatmap window must be sized in SESSIONS, not in bars.

A flat bar count truncates differently on every timeframe. On 28 Oct 2026 the chart asked for n=30 and the
5m heatmap began at 13:00 while 15m showed the whole day — from the same recorded ticks, because 30 bars
is 2h30 on 5m and 7h30 on 15m. It looked like missing data and was arithmetic.

These tests pin the arithmetic the endpoint relies on, so the window cannot be quietly shrunk back below a
full session.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.timeutil import TF_SECONDS, now_ist, session_bounds  # noqa: E402

RULES = json.loads((Path(__file__).resolve().parent.parent / "app" / "rules.json").read_text())
FP = RULES["footprints"]


def _session_bars(tf: str) -> int:
    t0, t1 = session_bounds(now_ist().date())
    return max(1, -(-(int(t1) - int(t0)) // TF_SECONDS[tf]))      # ceil, as the endpoint does


def _window(tf: str) -> int:
    """What the endpoint will actually request for cover=session."""
    return max(4, min(_session_bars(tf) * int(FP["heat_sessions"]), int(FP["max_bars"])))


def test_the_old_flat_count_really_did_truncate_5m_at_1pm():
    """Documents the bug rather than trusting the explanation: 30 bars of 5m is 150 minutes, so from a
    15:30 close the heatmap could not reach past 13:00."""
    assert 30 * TF_SECONDS["5m"] // 60 == 150
    t0, t1 = session_bounds(now_ist().date())
    session_min = (int(t1) - int(t0)) // 60
    assert session_min > 150, "if a session were under 2h30 the flat count would not have truncated"
    # 15m was unaffected, which is why it looked like a data problem and was not
    assert 30 * TF_SECONDS["15m"] // 60 >= session_min


def test_cover_session_spans_at_least_one_whole_session_on_every_chart_tf():
    for tf in ("1m", "5m", "15m", "30m", "1h"):
        covered_min = _window(tf) * TF_SECONDS[tf] // 60
        session_min = _session_bars(tf) * TF_SECONDS[tf] // 60
        if tf == "1m":
            # 1m cannot reach a full session inside max_bars; that is a payload decision, not an accident.
            assert _window(tf) == int(FP["max_bars"])
            continue
        assert covered_min >= session_min, f"{tf}: {covered_min}m does not cover a {session_min}m session"


def test_heat_sessions_pulls_in_the_previous_session():
    """The point of heat_sessions > 1: at 09:20 today has almost no bars, so the window has to reach back
    into yesterday or the heatmap is nearly empty every morning. footprint_levels is never pruned, so that
    history is really there."""
    assert int(FP["heat_sessions"]) >= 2
    assert _window("5m") > _session_bars("5m")


def test_max_bars_caps_the_payload():
    assert int(FP["max_bars"]) >= _session_bars("5m") * int(FP["heat_sessions"])
    assert int(FP["max_bars"]) <= 400, "every bar carries a price ladder; this is refetched every 30s"
    for tf in ("1m", "5m", "15m", "30m", "1h"):
        assert _window(tf) <= int(FP["max_bars"])


def test_history_days_survives_a_weekend():
    """history_days is CALENDAR days of 1m candles. Asking for 2 sessions with only 3 calendar days of
    history silently returns one session across a weekend."""
    assert int(FP["history_days"]) >= int(FP["heat_sessions"]) + 4
