"""Candle trail after +40 — 8 EMA Prototype unit tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import (  # noqa: E402
    flagship_candle_trail_update,
    simulate_prototype_spot_exit,
)


def _bars(entry_ts: int, n: int = 8):
    bars = []
    for i in range(n):
        ts = entry_ts + i * 900
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
        bars.append(
            {
                "ts": ts,
                "open": 25000.0,
                "high": 25010.0,
                "low": 24990.0,
                "close": 25000.0,
                "e8": 25000.0,
                "day": dt.strftime("%Y-%m-%d"),
                "hm": dt.strftime("%H:%M"),
            }
        )
    return bars


def _1m(rows, day="2026-11-17"):
    out = []
    for ts, o, h, l, c in rows:
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
        out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "day": day, "hm": dt.strftime("%H:%M")})
    return pd.DataFrame(out)


def test_disabled_exits_at_target_40():
    entry_ts = 1_731_907_500
    day_1m = _1m([(entry_ts + 60, 25000, 25045, 24995, 25040)])
    res = simulate_prototype_spot_exit(
        bars=_bars(entry_ts),
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        day_1m=day_1m,
        entry_ts=entry_ts,
        candle_trail={"enabled": False},
    )
    assert res["exit_reason"] == "SPOT_TARGET_HIT"
    assert res["st_pnl_pts"] == 40.0


def test_long_rides_greens_then_trail_hit():
    """Arm at +40, ratchet on green 15m lows, exit when spot breaks last green low."""
    from analyst.proto_candle_trail_8510 import session_anchor_ts

    day = "2026-11-17"
    anchor = session_anchor_ts(day)
    entry_ts = anchor + 180
    entry_spot = 25000.0
    rows = [
        (entry_ts + 60, 25000, 25045, 24995, 25040),
        (anchor + 840, 25040, 25058, 25035, 25055),
        (anchor + 1740, 25055, 25072, 25048, 25068),
        (anchor + 2640, 25068, 25070, 25020, 25022),
    ]
    day_1m = _1m(rows, day=day)
    res = simulate_prototype_spot_exit(
        bars=_bars(anchor),
        start_idx=0,
        entry_spot=entry_spot,
        is_long=True,
        day_1m=day_1m,
        entry_ts=entry_ts,
        candle_trail={"enabled": True, "activate_pts": 40.0, "candle_tf_min": 15},
    )
    assert res["exit_reason"] == "CANDLE_TRAIL_HIT"
    assert res["st_pnl_pts"] > 15.0
    assert any("TRAIL armed" in s for s in res["trail_steps"])


def test_red_candle_does_not_move_long_stop():
    stop = 24960.0
    assert flagship_candle_trail_update(True, stop, 25050, 25055, 25040, 25045) == stop


def test_monotonic_long_trail():
    stop = 24960.0
    s1 = flagship_candle_trail_update(True, stop, 25000, 25050, 24990, 25040)
    s2 = flagship_candle_trail_update(True, s1, 25040, 25055, 25020, 25050)
    assert s2 >= s1 >= stop


def test_short_mirror_convergence_trail():
    from analyst.proto_candle_trail_8510 import session_anchor_ts

    day = "2026-11-17"
    anchor = session_anchor_ts(day)
    entry_ts = anchor + 180
    rows = [
        (entry_ts + 60, 25000, 25005, 24955, 24960),
        (anchor + 840, 24960, 24980, 24930, 24935),
        (anchor + 1740, 24935, 24940, 24890, 24895),
        (anchor + 2640, 24895, 24985, 24890, 24892),
    ]
    day_1m = _1m(rows, day=day)
    res = simulate_prototype_spot_exit(
        bars=_bars(anchor),
        start_idx=0,
        entry_spot=25000.0,
        is_long=False,
        day_1m=day_1m,
        entry_ts=entry_ts,
        candle_trail={"enabled": True, "activate_pts": 40.0, "candle_tf_min": 15},
    )
    assert res["exit_reason"] == "CANDLE_TRAIL_HIT"


def test_no_trail_before_activate_pts():
    entry_ts = 1_731_907_500
    day_1m = _1m([(entry_ts + 60, 25000, 25030, 24990, 25025)])
    res = simulate_prototype_spot_exit(
        bars=_bars(entry_ts),
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        day_1m=day_1m,
        entry_ts=entry_ts,
        candle_trail={"enabled": True, "activate_pts": 40.0, "candle_tf_min": 15},
    )
    assert res["exit_reason"] != "CANDLE_TRAIL_HIT"
    assert "TRAIL armed" not in " ".join(res.get("trail_steps") or [])