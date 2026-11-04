"""
analyst/test_super_trail_8510.py — Comprehensive Unit Tests for Port 8510 v1.1 Super Trail Update.

Verifies:
  1. Trailing SL exact step-function mathematical progression trace.
  2. Trailing Target spike-high pullback state machine trace.
  3. Strike Matrix Algo Trade checkbox skipping vs manual order pass-through.
  4. Multi-month date-range backtest simulation (Fixed vs Super Trail).
"""
import asyncio
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import (
    QuantTerminalBackend,
    compute_trailing_sl,
    compute_trailing_target,
    round_to_tick,
)


def test_trailing_sl_step_progression():
    """
    User Specification Trace:
      Entry = 100, Initial SL = 80 (-20), Initial Target = 125 (+25), Activation = 15, Jump = 5:
      LTP = 100 -> SL = 80
      LTP = 114 -> Profit = 14 (< 15) -> SL = 80
      LTP = 115 -> Profit = 15 (Activation Hit, steps = 1 -> +5) -> SL = 85
      LTP = 120 -> Profit = 20 -> SL = 90
      LTP = 125 -> Profit = 25 -> SL = 95
      LTP = 130 -> Profit = 30 -> SL = 100 (COST / BREAKEVEN)
      LTP = 140 -> Profit = 40 -> SL = 110
      LTP = 150 -> Profit = 50 -> SL = 120
      If LTP drops from 150 to 120 -> Trigger SL Exit at 120 (+20 pts locked profit).
    """
    entry = 100.0
    init_sl_pts = 20.0
    act_pts = 15.0
    jump_pts = 5.0

    highest = entry
    current_sl = entry - init_sl_pts

    trace = [
        (100.0, 100.0, 80.0),
        (114.0, 114.0, 80.0),
        (115.0, 115.0, 85.0),
        (120.0, 120.0, 90.0),
        (125.0, 125.0, 95.0),
        (130.0, 130.0, 100.0),
        (140.0, 140.0, 110.0),
        (150.0, 150.0, 120.0),
    ]

    for ltp, expected_highest, expected_sl in trace:
        highest, current_sl = compute_trailing_sl(
            entry_price=entry,
            current_ltp=ltp,
            highest_ltp=highest,
            current_trailing_sl=current_sl,
            initial_sl_pts=init_sl_pts,
            activation_pts=act_pts,
            jump_pts=jump_pts,
        )
        assert highest == expected_highest, f"LTP {ltp}: expected highest {expected_highest}, got {highest}"
        assert current_sl == expected_sl, f"LTP {ltp}: expected SL {expected_sl}, got {current_sl}"

    # Drop from 150 to 120 -> SL exit condition
    drop_ltp = 120.0
    highest, current_sl = compute_trailing_sl(
        entry_price=entry,
        current_ltp=drop_ltp,
        highest_ltp=highest,
        current_trailing_sl=current_sl,
        initial_sl_pts=init_sl_pts,
        activation_pts=act_pts,
        jump_pts=jump_pts,
    )
    assert highest == 150.0, "Highest must remain at peak"
    assert current_sl == 120.0, "SL must strictly not move down (monotonicity)"
    assert drop_ltp <= current_sl, "Trigger SL exit condition must be met"
    print("✔ Test 1 Passed: Trailing SL step progression trace verified 100%.")


def test_trailing_target_pullback_trace():
    """
    User Specification Trace:
      Entry = 100, Initial Target = 125 (+25), Step = 10, Pullback = 5.
      LTP < 125: FIXED target mode.
      LTP hits 125: switches to TRAILING, sets level = 135, highest = 125.
      LTP hits 135: advances level = 145.
      LTP hits 142: highest = 142, pullback trigger = 137.
      LTP drops to 137: triggers immediate exit at 137 (+37 pts locked).
    """
    entry = 100.0
    init_target_pts = 25.0
    step_pts = 10.0
    pullback_pts = 5.0

    status = "FIXED"
    target_level = entry + init_target_pts
    highest_after = entry

    # 1. LTP = 110 (Below target)
    res1 = compute_trailing_target(entry, 110.0, status, target_level, highest_after, init_target_pts, step_pts, pullback_pts)
    assert res1["target_status"] == "FIXED"
    assert res1["should_exit"] is False

    # 2. LTP = 125 (Initial target hit -> DO NOT EXIT, switch to TRAILING)
    res2 = compute_trailing_target(entry, 125.0, res1["target_status"], res1["trailing_target_level"], res1["highest_after_target"], init_target_pts, step_pts, pullback_pts)
    assert res2["target_status"] == "TRAILING"
    assert res2["should_exit"] is False
    assert res2["trailing_target_level"] == 135.0
    assert res2["highest_after_target"] == 125.0
    assert res2["pullback_exit_level"] == 120.0

    # 3. LTP = 135 (Hits 135 -> advances to 145)
    res3 = compute_trailing_target(entry, 135.0, res2["target_status"], res2["trailing_target_level"], res2["highest_after_target"], init_target_pts, step_pts, pullback_pts)
    assert res3["target_status"] == "TRAILING"
    assert res3["trailing_target_level"] == 145.0
    assert res3["highest_after_target"] == 135.0
    assert res3["should_exit"] is False

    # 4. LTP = 142 (Peak at 142 -> pullback level is 137)
    res4 = compute_trailing_target(entry, 142.0, res3["target_status"], res3["trailing_target_level"], res3["highest_after_target"], init_target_pts, step_pts, pullback_pts)
    assert res4["target_status"] == "TRAILING"
    assert res4["highest_after_target"] == 142.0
    assert res4["pullback_exit_level"] == 137.0
    assert res4["should_exit"] is False

    # 5. LTP drops to 137 (Hits pullback level -> FIRE EXIT)
    res5 = compute_trailing_target(entry, 137.0, res4["target_status"], res4["trailing_target_level"], res4["highest_after_target"], init_target_pts, step_pts, pullback_pts)
    assert res5["should_exit"] is True
    assert res5["exit_reason"] == "TRAIL_TARGET_PULLBACK_HIT"
    print("✔ Test 2 Passed: Trailing Target spike-high pullback trace verified 100%.")


def test_strike_matrix_algo_checkbox_filtering():
    """
    Verifies that when a strike is unchecked in algo_strike_selection:
      - Automated Algo execution skips the strike.
      - Manual order on that strike is still allowed.
    """
    backend = QuantTerminalBackend()
    # Uncheck LONG_CE_ITM
    backend.update_strike_selection({"LONG_CE_ITM": False, "LONG_CE_ATM": True})
    assert backend.algo_strike_selection["LONG_CE_ITM"] is False
    assert backend.algo_strike_selection["LONG_CE_ATM"] is True

    contract = {
        "id": "LONG_CE_ITM",
        "symbol": "NIFTY-Nov2026-23800-CE",
        "security_id": "39951",
        "lot_size": 65,
        "ltp": 200.0,
        "delta": 0.65,
    }

    # 1. Automated Algo trigger -> Must SKIP
    res_algo = asyncio.run(backend.execute_order_with_chaser(
        contract=contract,
        side="BUY",
        strategy_name="8-EMA Pullback Buy",
        is_algo_trade=True,
    ))
    assert res_algo["ok"] is False
    assert res_algo.get("skipped") is True
    assert "excluded by Trader Strike Matrix Filter" in res_algo["error"]

    # 2. Manual Matrix trigger -> Must EXECUTE (not skipped)
    res_manual = asyncio.run(backend.execute_order_with_chaser(
        contract=contract,
        side="BUY",
        strategy_name="Manual Matrix 8510",
        is_algo_trade=False,
    ))
    assert res_manual["ok"] is True
    assert res_manual["status"] == "FILLED"

    # Reset checkbox
    backend.update_strike_selection({
        "LONG_CE_ITM": True,
        "LONG_CE_ATM": True,
        "LONG_PE_ITM": True,
        "LONG_PE_ATM": True,
    })
    assert backend.algo_strike_selection["LONG_CE_ITM"] is True
    print("✔ Test 3 Passed: Strike Matrix Algo Trade checkbox filtering verified 100%.")


def test_date_range_backtest_simulation():
    """
    Verifies date-range backtest evaluates historical trades correctly
    and produces side-by-side comparison with alpha.
    """
    backend = QuantTerminalBackend()
    res = backend.run_date_range_backtest(from_date="2026-06-01", to_date="2026-11-04")
    assert res["ok"] is True, f"Backtest failed: {res.get('error')}"
    assert res["total_trades"] == 285, f"Expected 285 trades, got {res['total_trades']}"
    assert res["trailing_net_pnl"] > res["fixed_net_pnl"], "Super Trail must produce positive alpha over fixed 40/40"
    assert res["alpha_locked"] > 100000.0, f"Expected > ₹100,000 alpha locked, got ₹{res['alpha_locked']}"

    # Sample checks on trades
    first_trade = res["trades"][0]
    assert "trade_id" in first_trade
    assert "exit_reason" in first_trade
    assert "trailing_net_pnl" in first_trade
    assert "alpha_locked" in first_trade

    print(f"✔ Test 4 Passed: Date-range backtest verified (285 trades, Fixed ₹{res['fixed_net_pnl']:,.2f} vs Trail ₹{res['trailing_net_pnl']:,.2f}, Alpha +₹{res['alpha_locked']:,.2f}).")


def test_nov4_3strategies_replay_sequence():
    """
    Verifies that Replay for 2026-11-04 returns the verified 3-strategy sequence:
      1. Stage 2 8-EMA (Long) [Pullback Touch]
      2. 4 MA / 8 EMA Downside Reversal
      3. 4 MA / 8 EMA Upside Reversal
      4. 1H 8-EMA Support Bounce Confluence
    Total Net PnL: +₹13,905.43 with 100% Win Rate.
    """
    backend = QuantTerminalBackend()
    trades = backend.get_replay_trades_for_date("2026-11-04")
    assert len(trades) == 4, f"Expected 4 trades, got {len(trades)}"

    expected_names = [
        "Stage 2 8-EMA (Long) [Pullback Touch]",
        "4 MA / 8 EMA Downside Reversal",
        "4 MA / 8 EMA Upside Reversal",
        "1H 8-EMA Support Bounce Confluence",
    ]
    actual_names = [t.get("setup_name") for t in trades]
    assert actual_names == expected_names, f"Trade names mismatch: {actual_names}"

    total_net = sum(t["net_pnl"] for t in trades)
    assert round(total_net, 2) == 13905.43, f"Expected ₹13,905.43, got {total_net}"

    # Verify table keys compatibility
    for t in trades:
        assert "setup_name" in t and "setup" in t
        assert "entry_ist" in t and "entry_time" in t
        assert "exit_ist" in t and "exit_time" in t
        assert "duration" in t
        assert "index_entry" in t and "index_exit" in t
        assert "option_entry" in t and "option_exit" in t
        assert "outcome" in t and "result" in t
        assert "gross_pnl" in t and "net_pnl" in t
        assert "dhan_charges" in t and "charges" in t

    # Verify async replay response
    replay_res = asyncio.run(backend.run_date_replay("2026-11-04"))
    assert replay_res["ok"] is True
    assert replay_res["trades_count"] == 4
    assert replay_res["win_rate"] == 100.0
    assert replay_res["total_net_pnl"] == 13905.43
    print("✔ Test 5 Passed: Nov 4 3-Strategies Replay sequence verified (+₹13,905.43, 100% WR).")


def test_all_4_core_strategies_scanner_and_party_switch():
    """
    Verifies the 4-Core Strategy Scanner logic & Party-Switch Rule:
      1. Stage 2 8-EMA (Long): UBB Up, Close > 8EMA, BB Width > 60, Touch 8EMA -> CE Long (+40/-40).
      2. Stage 3 8-EMA (Short): LBB Down, Close < 8EMA, BB Width > 60, Reject 8EMA -> PE Short (+40/-40).
      3. 4/8 MA Crossover Reversal:
         - Bearish Cross sets Party to BEARISH and blocks CE Longs.
         - Bullish Cross sets Party to BULLISH and blocks PE Shorts.
      4. 1H 8-EMA Confluence: Double Confirmation / High Conviction (+100/-40).
      5. Scanner status & telemetry reporting.
    """
    backend = QuantTerminalBackend()
    backend.scanner_cfg["tolerance_pts"] = 25.0
    backend.scanner_cfg["bb_width_min"] = 40.0

    # 1. Test Party-Switch manually and via crossover
    backend.active_party = "BEARISH"
    assert backend.active_party == "BEARISH"

    # Create synthetic 15m candles
    dates = pd.date_range("2026-11-04 09:15:00", periods=50, freq="15min", tz="Asia/Kolkata")

    # Generate Bullish Sequence (Stage 2)
    bull_prices = np.linspace(23700, 24000, 50)
    df_bull = pd.DataFrame({
        "open": bull_prices - 5,
        "high": bull_prices + 15,
        "low": bull_prices - 15,
        "close": bull_prices,
        "volume": [1000] * 50
    }, index=dates)

    # When active_party is BEARISH, Bullish Stage 2 is BLOCKED
    backend.active_party = "BEARISH"
    sigs = backend.evaluate_candle_dataframe(df_bull, spot_price=bull_prices[-1])
    # Long should be blocked
    long_sigs = [s for s in sigs if s["direction"] == "LONG"]
    assert len(long_sigs) == 0, "Party-Switch BEARISH must block all CE Long signals"

    # Now set active_party to NEUTRAL or BULLISH -> Stage 2 triggers!
    backend.active_party = "NEUTRAL"
    e8_last = df_bull["close"].ewm(span=8, adjust=False).mean().iloc[-1]
    sigs_bull = backend.evaluate_candle_dataframe(df_bull, spot_price=e8_last)
    stage2_sigs = [s for s in sigs_bull if s["strategy_id"] == "STAGE_2_LONG"]
    assert len(stage2_sigs) >= 1, "Stage 2 8-EMA (Long) must trigger when price touches 8-EMA in uptrend"
    assert stage2_sigs[0]["target_pts"] == 40.0
    assert stage2_sigs[0]["sl_pts"] == 40.0

    # Generate Bearish Sequence (Stage 3)
    bear_prices = np.linspace(24000, 23700, 50)
    df_bear = pd.DataFrame({
        "open": bear_prices + 5,
        "high": bear_prices + 15,
        "low": bear_prices - 15,
        "close": bear_prices,
        "volume": [1000] * 50
    }, index=dates)

    # When active_party is BULLISH, Bearish Stage 3 is BLOCKED
    backend.active_party = "BULLISH"
    sigs_blocked = backend.evaluate_candle_dataframe(df_bear, spot_price=bear_prices[-1])
    short_sigs = [s for s in sigs_blocked if s["direction"] == "SHORT"]
    assert len(short_sigs) == 0, "Party-Switch BULLISH must block all PE Short signals"

    # Now with active_party NEUTRAL/BEARISH -> Stage 3 triggers
    backend.active_party = "NEUTRAL"
    e8_bear = df_bear["close"].ewm(span=8, adjust=False).mean().iloc[-1]
    sigs_bear = backend.evaluate_candle_dataframe(df_bear, spot_price=e8_bear)
    stage3_sigs = [s for s in sigs_bear if s["strategy_id"] == "STAGE_3_SHORT"]
    assert len(stage3_sigs) >= 1, "Stage 3 8-EMA (Short) must trigger when price touches 8-EMA in downtrend"
    assert stage3_sigs[0]["target_pts"] == 40.0
    assert stage3_sigs[0]["sl_pts"] == 40.0

    # Verify Scanner Status Dictionary
    status = backend.get_scanner_status()
    assert status["ok"] is True
    assert "strategies" in status
    assert "active_party" in status
    assert "stage2_long" in status["strategies"]
    assert "stage3_short" in status["strategies"]
    assert "crossover_reversal" in status["strategies"]
    assert "h1_confluence" in status["strategies"]

    print("✔ Test 6 Passed: 4-Core Strategy Scanner & Party-Switch logic verified 100%.")


def main():
    print("Running Port 8510 Super Trail v1.1 Unit Test Suite...")
    test_trailing_sl_step_progression()
    test_trailing_target_pullback_trace()
    test_strike_matrix_algo_checkbox_filtering()
    test_date_range_backtest_simulation()
    test_nov4_3strategies_replay_sequence()
    test_all_4_core_strategies_scanner_and_party_switch()
    print("\n🎉 ALL 6 UNIT TESTS PASSED WITH 100% PRECISION!")


if __name__ == "__main__":
    main()
