"""
test_breakout_engine.py — Comprehensive Unit & Integration Tests for PriceActionBreakoutEngine.
"""

import numpy as np
import pandas as pd
from price_action_breakout import PriceActionBreakoutEngine, compute_candle_anatomy


def test_candle_anatomy():
    print("\n--- Test 1: Candle Anatomy Calculations ---")
    data = {
        "open": [100.0, 105.0],
        "high": [110.0, 108.0],
        "low": [95.0, 98.0],
        "close": [108.0, 100.0],
        "volume": [1000, 2000],
    }
    df = pd.DataFrame(data)
    out = compute_candle_anatomy(df)

    # Candle 0: Bullish (100 -> 108), High=110, Low=95.
    # Total Range = 110 - 95 = 15.0
    # Body = 8.0
    # Upper Wick = 110 - 108 = 2.0
    # Lower Wick = 100 - 95 = 5.0
    assert np.isclose(out["total_range"].iloc[0], 15.0)
    assert np.isclose(out["body_size"].iloc[0], 8.0)
    assert np.isclose(out["upper_wick"].iloc[0], 2.0)
    assert np.isclose(out["lower_wick"].iloc[0], 5.0)
    assert np.isclose(out["body_ratio"].iloc[0], 8.0 / 15.0)
    assert np.isclose(out["upper_wick_ratio"].iloc[0], 2.0 / 15.0)
    assert np.isclose(out["lower_wick_ratio"].iloc[0], 5.0 / 15.0)
    assert out["candle_type"].iloc[0] == "BULLISH"
    assert out["candle_type"].iloc[1] == "BEARISH"
    print("✓ Candle anatomy calculations verified.")


def test_strong_vs_weak_breakout():
    print("\n--- Test 2: Strong vs Weak Breakout Validation ---")
    engine = PriceActionBreakoutEngine(min_body_ratio=0.60, max_breakout_wick_ratio=0.20)

    # Level L = 24000.0 (Resistance)
    # Candle 0: Below resistance, base volume = 1000
    # Candle 1: Strong Breakout (large body, vol 5000 > sma, tiny upper wick, decisive close)
    # Candle 2: Another test, but weak breakout (small body / doji, long upper wick)
    data = {
        "timestamp": pd.date_range("2026-09-04 09:15", periods=5, freq="5min"),
        "open":  [23980.0, 23990.0, 24020.0, 24010.0, 24015.0],
        "high":  [23995.0, 24050.0, 24040.0, 24060.0, 24050.0],
        "low":   [23970.0, 23985.0, 24010.0, 23990.0, 24010.0],
        "close": [23990.0, 24045.0, 24025.0, 24015.0, 24048.0],
        "volume": [1000.0, 5000.0, 3000.0, 100.0, 4000.0],
    }
    df = pd.DataFrame(data)
    key_levels = {"R1_Call_Wall": 24000.0}

    res = engine.apply_breakout_signals(df, key_levels)
    assert res["breakout_signal"].iloc[1] == "VALID_STRONG_BREAKOUT", f"Expected VALID_STRONG_BREAKOUT, got {res['breakout_signal'].iloc[1]}"
    assert res["signal_direction"].iloc[1] == "BULLISH"
    print("✓ VALID_STRONG_BREAKOUT successfully identified.")


def test_trap_detections():
    print("\n--- Test 3: False Breakout (Trap) Detections ---")
    engine = PriceActionBreakoutEngine()

    # 1. Wick Breakout / Sweep Trap
    # Candle 0 below 24000. Candle 1 sweeps high to 24030, but closes at 23985 (below 24000) with long upper wick
    data_sweep = {
        "open":  [23970.0, 23980.0],
        "high":  [23990.0, 24035.0],
        "low":   [23960.0, 23975.0],
        "close": [23980.0, 23985.0],
        "volume": [1000.0, 1500.0],
    }
    df_sweep = pd.DataFrame(data_sweep)
    res_sweep = engine.apply_breakout_signals(df_sweep, {"R1": 24000.0})
    assert res_sweep["trap_signal"].iloc[1] == "TRAP_WICK_REJECTION", f"Got: {res_sweep['trap_signal'].iloc[1]}"
    print("✓ TRAP_WICK_REJECTION detected.")

    # 2. Inside Bar Trap / Immediate Reversal
    # Candle 1 closes above 24000 (24020). Candle 2 immediately dumps red and closes at 23970 (below 24000)
    data_rev = {
        "open":  [23970.0, 23990.0, 24020.0],
        "high":  [23985.0, 24025.0, 24025.0],
        "low":   [23960.0, 23980.0, 23965.0],
        "close": [23980.0, 24020.0, 23970.0],
        "volume": [1000.0, 2000.0, 3000.0],
    }
    df_rev = pd.DataFrame(data_rev)
    res_rev = engine.apply_breakout_signals(df_rev, {"R1": 24000.0})
    assert res_rev["trap_signal"].iloc[2] == "TRAP_IMMEDIATE_REVERSAL", f"Got: {res_rev['trap_signal'].iloc[2]}"
    print("✓ TRAP_IMMEDIATE_REVERSAL detected.")

    # 3. Double Rejection
    # Candle 1 tests 24000 with upper wick and rejects. Candle 3 tests 24000 with upper wick and rejects again.
    data_double = {
        "open":  [23960.0, 23980.0, 23970.0, 23975.0],
        "high":  [23975.0, 24008.0, 23985.0, 24006.0],
        "low":   [23955.0, 23970.0, 23965.0, 23970.0],
        "close": [23970.0, 23975.0, 23972.0, 23975.0],
        "volume": [1000.0, 1200.0, 900.0, 1100.0],
    }
    df_double = pd.DataFrame(data_double)
    res_double = engine.apply_breakout_signals(df_double, {"R1": 24000.0})
    assert res_double["trap_signal"].iloc[3] == "TRAP_DOUBLE_REJECTION", f"Got: {res_double['trap_signal'].iloc[3]}"
    print("✓ TRAP_DOUBLE_REJECTION detected.")


def test_3_phase_retest_continuation():
    print("\n--- Test 4: 3-Phase Retest & Continuation Engine ---")
    engine = PriceActionBreakoutEngine(retest_buffer_pts=3.0)

    # R1 = 24000.0
    # Candle 0: Base candle below 24000
    # Candle 1: Strong Breakout (Phase 1) above 24000 (Close=24040, Vol=10000)
    # Candle 2: Retracement to 24002 (Phase 2), Low vol = 3000 < 10000
    # Candle 3: Hammer reversal directly at flipped support 24000 (Phase 3 confirmation)
    data = {
        "timestamp": pd.date_range("2026-09-04 09:15", periods=4, freq="5min"),
        "open":  [23970.0, 23990.0, 24035.0, 24005.0],
        "high":  [23985.0, 24045.0, 24038.0, 24025.0],
        "low":   [23965.0, 23985.0, 24002.0, 23999.0], # Low touches 23999 (flipped support)
        "close": [23980.0, 24040.0, 24008.0, 24022.0], # Closes strong green with hammer wick
        "volume": [1000.0, 10000.0, 3000.0, 4000.0],    # Pullback vols < 10000
    }
    df = pd.DataFrame(data)
    res = engine.apply_breakout_signals(df, {"R1_Call_Wall": 24000.0})

    assert res["breakout_signal"].iloc[1] == "VALID_STRONG_BREAKOUT"
    assert res["actionable_retest_entry"].iloc[3] == "BULLISH_RETEST_ENTRY", f"Got: {res['actionable_retest_entry'].iloc[3]}"
    assert res["entry_price"].iloc[3] == 24022.0
    assert res["stop_loss"].iloc[3] < 24000.0
    assert res["target_1"].iloc[3] > res["entry_price"].iloc[3]
    assert res["target_2"].iloc[3] > res["target_1"].iloc[3]
    print(f"✓ ACTIONABLE_RETEST_ENTRY fired! Entry: {res['entry_price'].iloc[3]}, SL: {res['stop_loss'].iloc[3]}, T1: {res['target_1'].iloc[3]}, T2: {res['target_2'].iloc[3]}")


def test_real_nifty_data():
    print("\n--- Test 5: Real Nifty 5m Historical Data ---")
    df_nifty = pd.read_csv("data/candles_5m/2026-09-04.csv")
    engine = PriceActionBreakoutEngine()

    levels = {
        "VWAP": df_nifty["vwap"],
        "R1_Call_Wall": 23950.0,
        "S1_Put_Wall": 23900.0,
    }
    res = engine.apply_breakout_signals(df_nifty, levels)
    stats = engine.get_summary_stats(res)
    print("Nifty 2026-09-04 Session Results:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    actionable = engine.extract_actionable_signals(res)
    print(f"\nTotal Actionable Rows Detected: {len(actionable)}")
    for idx, row in actionable.iterrows():
        print(f"  [{row['timestamp']}] {row['primary_signal']} ({row['signal_direction']}) at {row['key_level_hit']}")


if __name__ == "__main__":
    test_candle_anatomy()
    test_strong_vs_weak_breakout()
    test_trap_detections()
    test_3_phase_retest_continuation()
    test_real_nifty_data()
    print("\n🎉 ALL UNIT & INTEGRATION TESTS PASSED SUCCESSFULLY!")
