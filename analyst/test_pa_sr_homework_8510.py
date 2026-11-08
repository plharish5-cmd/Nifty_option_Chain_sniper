"""Unit tests for 15m PA @ S/R homework backtest helpers."""
from __future__ import annotations

from analyst.pa_sr_homework_8510 import detect_pa_patterns_on_bar, scan_pa_sr_breakout
from analyst.terminal8510_backend import simulate_backtest_spot_exit


def _bar(o, h, l, c, **kw):
    return {"open": o, "high": h, "low": l, "close": c, "hm": "10:00", "day": "2026-11-03", "ts": 1, **kw}


def test_hammer_detected():
    bars = [_bar(100, 101, 99.5, 100.5), _bar(100, 100.2, 98.0, 100.1)]
    pat_l, pat_s, _, _ = detect_pa_patterns_on_bar(bars, 1)
    assert pat_l == "HAMMER"


def test_long_break_at_support():
    sup = 99.0
    bars = [
        _bar(101, 102, 100, 101),
        _bar(100, 100.5, 98.5, 99.8),  # hammer zone at S1
        _bar(99.9, 101.2, 99.5, 101.0),  # break high
    ]
    sr = {"S1": sup, "S2": None, "S3": None, "R1": 110, "S4": None, "R4": None}
    cfg = {"level_tolerance_pts": 25, "fixed_sl_pts": 40, "fixed_tgt_pts": 40, "swing_lookback_bars": 2}
    sigs = scan_pa_sr_breakout(bars, 1, sr, cfg)
    assert len(sigs) >= 1
    assert sigs[0][2] == "LONG"


def test_trail_every_bar_tightens_stop():
    bars = [
        _bar(100, 101, 99, 100.5, hm="10:00", e8=102),
        _bar(100, 105, 99.5, 104, hm="10:15", e8=102),
        _bar(104, 104.5, 103, 103.5, hm="10:30", e8=102),
    ]
    pnl, _, _, reason, steps, _ = simulate_backtest_spot_exit(
        bars, 1, 100.0, True, 40, 40, False, {}, trail_every_bar=True, trail_green_only=False
    )
    assert any("Bar Low" in s for s in steps)
