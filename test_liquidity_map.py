"""
test_liquidity_map.py — Rigorous unit test suite for liquidity_map.py.
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from liquidity_map import (
    evaluate_liquidity_map,
    extract_htf_levels,
    build_tracked_levels,
    build_stop_loss_pools,
    evaluate_candle_at_level,
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


def make_daily_ohlc(n_days=700, base=23000.0):
    start = datetime(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    c = base + np.cumsum(np.random.normal(5, 50, n_days))
    h = c + np.random.uniform(20, 80, n_days)
    l = c - np.random.uniform(20, 80, n_days)
    o = l + (h - l) * 0.5
    return pd.DataFrame({
        "Date": dates,
        "Open": o,
        "High": h,
        "Low": l,
        "Close": c,
    })


def test_guards():
    rules = load_rules()

    # 1. Empty / None
    snap_none = evaluate_liquidity_map(None, spot=24000.0)
    assert snap_none.data_guard_warning is not None
    assert snap_none.structure_state == "SIDEWAYS"

    # 2. Missing columns
    bad_df = pd.DataFrame({"close": [24000] * 20})
    snap_bad = evaluate_liquidity_map(bad_df, spot=24000.0)
    assert "Missing columns" in snap_bad.data_guard_warning

    # 3. Insufficient bars (< 2N + 3)
    short_df = make_candles([24000, 24010, 24020])
    snap_short = evaluate_liquidity_map(short_df, spot=24020.0)
    assert "Insufficient bar history" in snap_short.data_guard_warning


def test_stop_loss_pools():
    rules = load_rules()
    spot = 24100.0
    atr_val = 24.0
    expected_offset = max(0.5 * atr_val, 10.0)  # 12.0 pts

    closes = [24050, 24100, 24150, 24120, 24090, 24060, 24080, 24110, 24140, 24160, 24100]
    df = make_candles(closes)
    pivots = smc.pivots(df, n=2)

    tracked = build_tracked_levels(df, pivots, spot, atr_val, rules=rules)
    pools, nearest_buy, nearest_sell = build_stop_loss_pools(tracked, df, spot, atr_val, rules)

    assert len(pools) > 0
    # Every resistance pool must be BUY_STOPS and above resistance level
    # Every support pool must be SELL_STOPS and below support level
    for p in pools:
        if p.pool_side == "BUY_STOPS":
            assert p.pool_price > p.level_price
            assert "resting buy-stops" in p.mechanical_explanation
        else:
            assert p.pool_price < p.level_price
            assert "resting sell-stops" in p.mechanical_explanation


def test_level_strength_by_touch_count():
    rules = load_rules()
    spot = 24050.0
    atr_val = 20.0

    # Build a series that touches ~24000 three times (support zone)
    closes = [
        24020, 24000, 24050, 24080, 24010, 24001, 24070, 24090, 24020, 24002, 24060, 24080, 24050
    ]
    lows = [c - 4.0 for c in closes]
    lows[1] = 23999.0
    lows[5] = 24000.0
    lows[9] = 24001.0
    df = make_candles(closes, lows=lows)

    pivots = smc.pivots(df, n=2)
    tracked = build_tracked_levels(df, pivots, spot, atr_val, rules=rules)

    # Find the level near 24000
    lvl_24000 = [l for l in tracked if abs(l.price - 24000.0) <= 8.0]
    assert len(lvl_24000) > 0
    the_lvl = lvl_24000[0]
    assert the_lvl.touch_count >= 3
    assert the_lvl.strength_class == "ZONE"
    assert "Defended" in the_lvl.psychological_context


def test_htf_level_extraction():
    rules = load_rules()
    spot = 24200.0
    ohlc = make_daily_ohlc(n_days=700, base=23500.0)
    htfs = extract_htf_levels(ohlc, spot, rules)

    assert len(htfs) > 0
    weeklies = [h for h in htfs if h.timeframe == "WEEKLY"]
    monthlies = [h for h in htfs if h.timeframe == "MONTHLY"]
    yearlies = [h for h in htfs if h.timeframe == "YEARLY"]

    assert len(weeklies) > 0
    assert len(monthlies) > 0
    assert len(yearlies) > 0


def test_candle_behaviour_at_level():
    rules = load_rules()

    # Case A: Rejection at Resistance (long upper wick)
    df_rej = make_candles([24100, 24120, 24140, 24150])
    df_rej.loc[3, "open"] = 24145.0
    df_rej.loc[3, "close"] = 24146.0  # tiny body
    df_rej.loc[3, "high"] = 24165.0   # 19-pt upper wick
    df_rej.loc[3, "low"] = 24144.0

    pivots_rej = smc.pivots(df_rej, n=2)
    tracked = build_tracked_levels(df_rej, pivots_rej, spot=24146.0, atr_val=20.0, rules=rules)
    # Add explicit resistance at 24155
    from liquidity_map import TrackedLevel
    res_lvl = TrackedLevel(
        name="Key Resistance",
        price=24155.0,
        level_type="RESISTANCE",
        timeframe="INTRADAY",
        touch_count=2,
        touch_times=[],
        strength_class="LEVEL",
        originating_move_atr=2.0,
        psychological_context="Resistance",
        is_in_play=True,
        distance_pts=9.0,
    )
    read_res = evaluate_candle_at_level(df_rej, [res_lvl], rules)
    assert read_res.reading == "REJECTION"
    assert "Sellers still dominant" in read_res.dominant_side

    # Case B: Acceptance at Resistance (strong green body)
    df_acc = make_candles([24100, 24120, 24140, 24160])
    df_acc.loc[3, "open"] = 24145.0
    df_acc.loc[3, "close"] = 24168.0  # strong green close above 24155
    df_acc.loc[3, "high"] = 24170.0
    df_acc.loc[3, "low"] = 24144.0
    read_acc = evaluate_candle_at_level(df_acc, [res_lvl], rules)
    assert read_acc.reading == "ACCEPTANCE"
    assert "Buyers have taken over" in read_acc.dominant_side


def test_full_liquidity_snapshot():
    spot = 24125.0
    closes = [24000 + np.sin(i / 3.0) * 80 + i * 3 for i in range(45)]
    df = make_candles(closes)
    ohlc = make_daily_ohlc(n_days=250, base=23500.0)

    snap = evaluate_liquidity_map(df, spot, "15m", ohlc=ohlc)
    assert snap.spot_price == 24125.0
    assert snap.atr_points > 0.0
    assert len(snap.levels) > 0
    assert len(snap.pools) > 0
    assert len(snap.htf_levels) > 0
    print(f"Snapshot verified: {len(snap.levels)} levels, {len(snap.pools)} pools, {len(snap.htf_levels)} HTF levels.")


if __name__ == "__main__":
    test_guards()
    test_stop_loss_pools()
    test_level_strength_by_touch_count()
    test_htf_level_extraction()
    test_candle_behaviour_at_level()
    test_full_liquidity_snapshot()
    print("\nALL LIQUIDITY MAP TESTS PASSED!")
