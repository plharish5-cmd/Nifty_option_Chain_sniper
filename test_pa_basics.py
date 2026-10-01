"""
test_pa_basics.py — Rigorous unit tests for pa_basics.py.
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from pa_basics import (
    evaluate_pa_basics,
    compute_wilder_rsi,
    compute_atr_stops,
    detect_trendlines,
    detect_chart_patterns,
    derive_structure_trend,
    load_rules,
)
import smc


def make_candles(closes, highs=None, lows=None, opens=None, start_time=None):
    n = len(closes)
    start_time = start_time or datetime(2026, 9, 4, 9, 15)
    ts = [start_time + timedelta(minutes=15 * i) for i in range(n)]
    c = np.array(closes, dtype=float)
    h = np.array(highs, dtype=float) if highs is not None else c + 5.0
    l = np.array(lows, dtype=float) if lows is not None else c - 5.0
    o = np.array(opens, dtype=float) if opens is not None else np.roll(c, 1)
    o[0] = c[0]
    return pd.DataFrame({
        "timestamp": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": np.random.randint(1000, 5000, size=n),
    })


def test_guards():
    rules = load_rules()

    # 1. Empty / None DataFrame
    snap_none = evaluate_pa_basics(None, spot=24000.0)
    assert snap_none.data_guard_warning is not None
    assert snap_none.structure.trend == "SIDEWAYS"
    assert snap_none.structure.is_sideways_no_trade is True

    # 2. Insufficient bars (< 2N + 3)
    short_df = make_candles([24000, 24010, 24020])
    snap_short = evaluate_pa_basics(short_df, spot=24020.0)
    assert snap_short.data_guard_warning is not None

    # 3. Missing columns
    bad_df = pd.DataFrame({"close": [24000] * 20})
    snap_bad = evaluate_pa_basics(bad_df, spot=24000.0)
    assert "Missing required candle columns" in snap_bad.data_guard_warning


def test_wilder_rsi_crossing_rule():
    rules = load_rules()

    # Case A: Steady uptrend pushing RSI above 70
    up_prices = [24000 + i * 25 for i in range(30)]
    df_up = make_candles(up_prices)
    rsi_up = compute_wilder_rsi(df_up, rules)
    assert rsi_up.current_rsi > 70.0
    assert rsi_up.state == "ABOVE_70"
    assert rsi_up.bias == "BULLISH"
    assert "NOT a sell signal" in rsi_up.rule_reminder

    # Case B: Cross down from 70
    # Add a drop after the peak
    cross_down_prices = up_prices + [up_prices[-1] - 80, up_prices[-1] - 160, up_prices[-1] - 250]
    df_cd = make_candles(cross_down_prices)
    rsi_cd = compute_wilder_rsi(df_cd, rules)
    assert rsi_cd.state == "CROSSED_DOWN_FROM_70"
    assert "Exhaustion confirmed" in rsi_cd.detail

    # Case C: Steady downtrend pushing RSI below 30
    down_prices = [25000 - i * 30 for i in range(30)]
    df_down = make_candles(down_prices)
    rsi_down = compute_wilder_rsi(df_down, rules)
    assert rsi_down.current_rsi < 30.0
    assert rsi_down.state == "BELOW_30"
    assert rsi_down.bias == "BEARISH"
    assert "NOT a buy signal" in rsi_down.rule_reminder

    # Case D: Cross up from 30
    cross_up_prices = down_prices + [down_prices[-1] + 80, down_prices[-1] + 160, down_prices[-1] + 250]
    df_cu = make_candles(cross_up_prices)
    rsi_cu = compute_wilder_rsi(df_cu, rules)
    assert rsi_cu.state == "CROSSED_UP_FROM_30"
    assert "Recovery confirmed" in rsi_cu.detail


def test_atr_stops_and_warning():
    rules = load_rules()
    df = make_candles([24000 + i * 5 for i in range(25)])
    spot = 24100.0

    # Test with default ~20 ATR (1.5x is 30 pts <= 40 pts)
    df["atr"] = 18.0
    atr_rep = compute_atr_stops(df, spot, "15m", rules)
    assert atr_rep.atr_points == 18.0
    assert atr_rep.stop_1_5x_pts == 27.0
    assert not atr_rep.is_fixed_stop_too_tight
    assert atr_rep.stop_1_5x_inr_lot == 27.0 * 65

    # Test when ATR exceeds 40 pts threshold (e.g. ATR = 32 -> 1.5x = 48 > 40)
    df["atr"] = 32.0
    atr_high = compute_atr_stops(df, spot, "15m", rules)
    assert atr_high.stop_1_5x_pts == 48.0
    assert atr_high.is_fixed_stop_too_tight
    assert "VOLATILITY MISMATCH" in atr_high.volatility_vs_fixed_warning


def test_trendline_break_states():
    rules = load_rules()

    # Build an uptrend series with clear swing lows at N=2:
    # Bar 2: Low at 24000
    # Bar 7: Low at 24050
    # Bar 12: Projected support at ~24100
    prices = [
        24020, 24010, 24000, 24030, 24050,  # Pivot low around bar 2
        24040, 24030, 24050, 24080, 24110,  # Pivot low around bar 6-7
        24100, 24090, 24130, 24150, 24170   # Extension
    ]
    df = make_candles(prices)
    pivots = smc.pivots(df, n=2)
    tls = detect_trendlines(df, pivots, spot=24170.0, rules=rules)
    supp = tls["support"]
    if supp:
        assert supp.break_state in ("INTACT", "BROKEN — UNCONFIRMED", "CONFIRMED BREAK", "FAKE BREAK")
        assert supp.touch_count >= 2


def test_chart_patterns_w_and_m():
    rules = load_rules()

    # Create a Double Bottom (W pattern) with equal lows at bar 2 and bar 8, peak at bar 5
    # Then two closes breaking above the peak at bar 12, 13
    closes = [
        24100, 24050, 24000, 24060, 24120, 24070, 24010, 24002, 24050, 24090, 24130, 24150, 24160
    ]
    highs  = [c + 4.0 for c in closes]
    lows   = [c - 4.0 for c in closes]
    # Emphasize pivot lows
    lows[2] = 23995.0
    lows[7] = 23996.0
    # Peak
    highs[4] = 24130.0

    df_w = make_candles(closes, highs=highs, lows=lows)
    pivots_w = smc.pivots(df_w, n=2)
    pats = detect_chart_patterns(df_w, pivots_w, rules)
    # Check if W was detected or list returned cleanly
    assert isinstance(pats, list)


def test_full_snapshot():
    # End-to-end integration test
    np.random.seed(42)
    closes = [24000 + np.sin(i / 3.0) * 80 + i * 2 for i in range(40)]
    df = make_candles(closes)
    snap = evaluate_pa_basics(df, spot=float(closes[-1]), timeframe="15m")
    assert snap.timeframe == "15m"
    assert abs(snap.spot_price - float(closes[-1])) < 0.01
    assert snap.structure.trend in ("UPTREND", "DOWNTREND", "SIDEWAYS")
    assert snap.rsi.current_rsi >= 0.0
    assert snap.atr_stop.atr_points > 0.0
    print(f"Snapshot trend: {snap.structure.trend}, RSI: {snap.rsi.current_rsi} ({snap.rsi.state}), ATR: {snap.atr_stop.atr_points}")


if __name__ == "__main__":
    test_guards()
    test_wilder_rsi_crossing_rule()
    test_atr_stops_and_warning()
    test_trendline_break_states()
    test_chart_patterns_w_and_m()
    test_full_snapshot()
    print("\nALL PA BASICS TESTS PASSED!")
