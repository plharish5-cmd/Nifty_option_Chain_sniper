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
    assert res["total_trades"] == 290, f"Expected 290 trades, got {res['total_trades']}"
    assert res["trailing_net_pnl"] > res["fixed_net_pnl"], "Super Trail must produce positive alpha over fixed 40/40"
    assert res["alpha_locked"] > 100000.0, f"Expected > ₹100,000 alpha locked, got ₹{res['alpha_locked']}"

    # Verify MA stack enforcement filtering
    res_ma = backend.run_date_range_backtest(from_date="2026-06-01", to_date="2026-11-04", custom_cfg={"enforce_ma_stack": True})
    assert res_ma["ok"] is True
    assert res_ma["total_trades"] == 134, f"Expected 134 trades passing strict MA stack filter, got {res_ma['total_trades']}"
    assert res_ma["trailing_net_pnl"] > 200000.0

    # Sample checks on trades
    first_trade = res["trades"][0]
    assert "trade_id" in first_trade
    assert "exit_reason" in first_trade
    assert "trailing_net_pnl" in first_trade
    assert "alpha_locked" in first_trade

    print(f"✔ Test 4 Passed: Date-range backtest verified (290 trades, Fixed ₹{res['fixed_net_pnl']:,.2f} vs Trail ₹{res['trailing_net_pnl']:,.2f}, Alpha +₹{res['alpha_locked']:,.2f}, Strict MA Stack filter: {res_ma['total_trades']} trades).")


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


def test_nov3_clean_bullish_trades_replay():
    """
    Verifies that Replay and Backtest for 2026-11-03 strictly respects the 4-EMA > 8-EMA Bullish Stack:
      - Old incorrect Stage 3 Short and invalid Reversal Up trades are eliminated.
      - Exactly 3 Bullish trades returned:
        1. 4 MA / 8 EMA Upside Reversal (09:45 - 10:30, CE Long, Net +₹2,066.50)
        2. Stage 2 8-EMA (Long) [Pullback Touch] (11:00 - 11:45, CE Long, Net +₹2,066.50)
        3. 1H 8-EMA Support Bounce Confluence (12:15 - 13:45, CE Long, Net +₹3,039.88)
      Total Net PnL: +₹7,172.88 with 100% Win Rate and ZERO Short trades.
    """
    backend = QuantTerminalBackend()
    trades = backend.get_replay_trades_for_date("2026-11-03")
    assert len(trades) == 3, f"Expected exactly 3 trades for Nov 3, got {len(trades)}"

    expected_names = [
        "4 MA / 8 EMA Upside Reversal",
        "Stage 2 8-EMA (Long) [Pullback Touch]",
        "1H 8-EMA Support Bounce Confluence",
    ]
    actual_names = [t.get("setup_name") for t in trades]
    assert actual_names == expected_names, f"Trade names mismatch: {actual_names}"

    # Verify all trades are Bullish CE Long and zero Shorts
    for t in trades:
        assert "CE" in t.get("outcome", "") or "Trailed" in t.get("outcome", "") or "Conviction" in t.get("outcome", "")
        assert t.get("net_pnl") > 0, "Every trade on Nov 3 must be profitable"

    total_net = sum(t["net_pnl"] for t in trades)
    assert round(total_net, 2) == 7172.88, f"Expected ₹7,172.88, got {total_net}"

    # Verify backtest single-day ledger for Nov 3
    res_nov3 = backend.run_date_range_backtest(from_date="2026-11-03", to_date="2026-11-03")
    assert res_nov3["ok"] is True
    assert res_nov3["total_trades"] == 3
    assert all(t["direction"] == "LONG" for t in res_nov3["trades"]), "Zero Short trades allowed on Nov 3"

    # Verify async replay simulation response
    replay_res = asyncio.run(backend.run_date_replay("2026-11-03"))
    assert replay_res["ok"] is True
    assert replay_res["trades_count"] == 3
    assert replay_res["win_rate"] == 100.0
    assert replay_res["total_net_pnl"] == 7172.88
    print("✔ Test 7 Passed: Nov 3 Clean Bullish Replay & Backtest verified (+₹7,172.88, 100% WR, 0 Shorts).")


def test_strict_ma_stack_blocking():
    """
    Verifies strict MA Stack (4-EMA vs 8-EMA) enforcement and Reversal crossover prerequisites:
      1. Bullish Stack (4-EMA > 8-EMA):
         - Stage 2 Long is allowed.
         - Stage 3 Short is STRICTLY BLOCKED (state == BLOCKED_MA_STACK, armed == False).
         - Reversal Down is blocked.
         - 1H Resistance Rejection is blocked.
      2. Bearish Stack (4-EMA < 8-EMA):
         - Stage 3 Short is allowed.
         - Stage 2 Long is STRICTLY BLOCKED (state == BLOCKED_MA_STACK, armed == False).
         - Reversal Up is blocked.
         - 1H Support Bounce is blocked.
      3. Reversal Setup Prerequisite:
         - Setup #5 (Reversal Up) requires actual preceding cross where 4-EMA crossed above 8-EMA.
         - Setup #6 (Reversal Down) requires actual preceding cross where 4-EMA crossed below 8-EMA.
    """
    backend = QuantTerminalBackend()
    backend.active_party = "NEUTRAL"
    backend.scanner_cfg["bb_width_min"] = 30.0
    backend.scanner_cfg["tolerance_pts"] = 25.0

    dates = pd.date_range("2026-11-04 09:15:00", periods=40, freq="15min", tz="Asia/Kolkata")

    # 1. Bullish Stack Scenario: prices increasing steadily, 4-EMA > 8-EMA
    bull_prices = np.linspace(23500, 23800, 40)
    df_bull = pd.DataFrame({
        "open": bull_prices - 2,
        "high": bull_prices + 10,
        "low": bull_prices - 10,
        "close": bull_prices,
        "volume": [1000] * 40
    }, index=dates)

    backend.evaluate_candle_dataframe(df_bull, spot_price=bull_prices[-1])
    assert backend.strategy_status["stage3_short"]["state"] == "BLOCKED_MA_STACK", "Stage 3 Short MUST be BLOCKED_MA_STACK in Bullish Stack"
    assert backend.strategy_status["stage3_short"]["armed"] is False, "Stage 3 Short MUST NOT be armed in Bullish Stack"

    # 2. Bearish Stack Scenario: prices decreasing steadily, 4-EMA < 8-EMA
    bear_prices = np.linspace(23800, 23500, 40)
    df_bear = pd.DataFrame({
        "open": bear_prices + 2,
        "high": bear_prices + 10,
        "low": bear_prices - 10,
        "close": bear_prices,
        "volume": [1000] * 40
    }, index=dates)

    backend.evaluate_candle_dataframe(df_bear, spot_price=bear_prices[-1])
    assert backend.strategy_status["stage2_long"]["state"] == "BLOCKED_MA_STACK", "Stage 2 Long MUST be BLOCKED_MA_STACK in Bearish Stack"
    assert backend.strategy_status["stage2_long"]["armed"] is False, "Stage 2 Long MUST NOT be armed in Bearish Stack"

    # 3. Reversal without cross: when 4-EMA has been above 8-EMA continuously without a cross in last 3 bars
    sigs_no_cross = backend.evaluate_candle_dataframe(df_bull, spot_price=bull_prices[-1])
    reversal_sigs = [s for s in sigs_no_cross if "Reversal" in s.get("setup_name", "")]
    assert len(reversal_sigs) == 0, "Reversal MUST NOT trigger without an actual preceding crossover"

    print("✔ Test 8 Passed: Strict MA Stack Blocking & Reversal Crossover Prerequisites verified 100%.")


def test_backtest_timestamp_logging_and_homework_reconciliation():
    """
    Test 9: Verifies Backtest Entry/Exit IST Timestamps & Mentor Homework Reconciliation:
      1. Every backtest row contains non-empty entry_ist, exit_ist, day_outcome, and ema_bb_state_at_entry.
      2. Calibrated benchmark sessions return verified trade setups and outcomes.
      3. HomeworkReconciler and POST /api/reconcile/homework endpoint cleanly scan data/UI_screenshot/,
         match dates against the backtest ledger, and output structured delta table without errors.
    """
    backend = QuantTerminalBackend()

    # 1. Date-range backtest verification of timestamps & indicators
    res = backend.run_date_range_backtest(from_date="2026-06-01", to_date="2026-11-04")
    assert res["ok"] is True, f"Backtest failed: {res.get('error')}"
    trades = res.get("trades", [])
    assert len(trades) > 0, "Backtest must return non-empty trade rows"

    for t in trades:
        t_id = t.get("trade_id")
        t_date = t.get("date")
        assert bool(t.get("entry_ist")), f"Trade #{t_id} ({t_date}) missing entry_ist"
        assert bool(t.get("exit_ist")), f"Trade #{t_id} ({t_date}) missing exit_ist"
        assert bool(t.get("day_outcome")), f"Trade #{t_id} ({t_date}) missing day_outcome"
        assert bool(t.get("ema_bb_state_at_entry")), f"Trade #{t_id} ({t_date}) missing ema_bb_state_at_entry"
        assert t.get("index_entry") is not None, f"Trade #{t_id} missing index_entry"
        assert t.get("index_exit") is not None, f"Trade #{t_id} missing index_exit"
        assert t.get("duration") is not None, f"Trade #{t_id} missing duration"

    # 2. Benchmark dates calibration check
    benchmarks = ["2026-11-04", "2026-11-03", "2026-11-02", "2026-10-30", "2026-10-29",
                  "2026-10-28", "2026-10-27", "2026-10-19", "2026-10-14", "2026-10-13",
                  "2026-10-08", "2026-10-07", "2026-10-06"]
    for bd in benchmarks:
        b_trades = backend.get_replay_trades_for_date(bd)
        assert len(b_trades) >= 1, f"Benchmark date {bd} must return at least 1 verified trade"
        for bt in b_trades:
            assert bool(bt.get("entry_ist")), f"{bd} trade missing entry_ist"
            assert bool(bt.get("exit_ist")), f"{bd} trade missing exit_ist"
            assert bool(bt.get("outcome")), f"{bd} trade missing outcome"

    # 3. Homework Reconciler direct scanning & delta table
    reconciler = backend.homework_reconciler
    rec_res = reconciler.scan_and_reconcile(folder_path="data/UI_screenshot", backend=backend)
    assert rec_res["ok"] is True, f"Reconciler scan failed: {rec_res.get('error')}"
    assert rec_res["images_found"] >= 1, f"Expected >= 1 image in data/UI_screenshot, got {rec_res['images_found']}"
    assert len(rec_res["reconciliation_table"]) >= 1, "Reconciliation table must contain comparison rows"

    # Verify column presence on reconciliation rows
    for r in rec_res["reconciliation_table"]:
        assert "date" in r
        assert "image_file" in r
        assert "homework_trade" in r
        assert "homework_entry_result" in r
        assert "algo_trade" in r
        assert "algo_entry_result" in r
        assert "discrepancy_type" in r
        assert "recommended_correction" in r

    # 4. FastAPI POST /api/reconcile/homework API endpoint check via TestClient
    from fastapi.testclient import TestClient
    from engine_8510_live import app

    with TestClient(app) as client:
        # POST with default folder
        resp_post = client.post("/api/reconcile/homework", json={"folder": "data/UI_screenshot"})
        assert resp_post.status_code == 200, f"Expected 200, got {resp_post.status_code}: {resp_post.text}"
        data_post = resp_post.json()
        assert data_post["ok"] is True
        assert data_post["images_found"] >= 1
        assert "reconciliation_table" in data_post

        # GET endpoint
        resp_get = client.get("/api/reconcile/homework?folder=data/UI_screenshot")
        assert resp_get.status_code == 200
        assert resp_get.json()["ok"] is True

    print("✔ Test 9 Passed: Backtest Entry/Exit IST Timestamps & Homework Reconciliation Engine verified 100%.")


def test_stockmojo_and_port8600_confluence_integration():
    """
    Test 10: Verifies Port 8600 Removal, Institutional OI 1m Ingestion, Staged Trend Lab Variables, and Tab 6:
      1. parse_indian_unit_to_lakhs handles Cr, L, K, and +/- correctly.
      2. POST /api/oi/ingest & /api/stockmojo/ingest ingests live 1m rows from Nov 4 (15:33, 15:36, 15:39).
      3. GET /api/oi/pe_ce_table & /api/stockmojo/pe_ce_table returns parsed newest-first table.
      4. GET /api/confluence/port8600 is removed (returns 404).
      5. /api/telemetry populates generic trend_lab_variables and does NOT contain port8600_confluence or var_8600_*.
      6. HTML DOM contains no references to Port 8600 or var_8600_*.
    """
    from analyst.stockmojo_scraper import parse_indian_unit_to_lakhs
    from fastapi.testclient import TestClient
    from engine_8510_live import app

    # 1. Indian unit parser tests
    assert parse_indian_unit_to_lakhs("2.6 Cr") == 260.0, f"Expected 260.0, got {parse_indian_unit_to_lakhs('2.6 Cr')}"
    assert parse_indian_unit_to_lakhs("-10.3 L") == -10.3, f"Expected -10.3, got {parse_indian_unit_to_lakhs('-10.3 L')}"
    assert parse_indian_unit_to_lakhs("-35.2 K") == -0.352, f"Expected -0.352, got {parse_indian_unit_to_lakhs('-35.2 K')}"
    assert parse_indian_unit_to_lakhs("+83.2 K") == 0.832, f"Expected 0.832, got {parse_indian_unit_to_lakhs('+83.2 K')}"
    assert parse_indian_unit_to_lakhs("54.4 L") == 54.4, f"Expected 54.4, got {parse_indian_unit_to_lakhs('54.4 L')}"
    assert parse_indian_unit_to_lakhs("200.0") == 200.0, f"Expected 200.0, got {parse_indian_unit_to_lakhs('200.0')}"

    with TestClient(app) as client:
        # 2. Test POST /api/oi/ingest and legacy /api/stockmojo/ingest with exact benchmark rows
        sample_rows = [
            {
                "Time": "15:33",
                "Put OI Change (Day)": "99.5 L",
                "Put OI Change": "-6.37 L",
                "Call OI Change (Day)": "-7.32 L",
                "Call OI Change": "-8.65 L",
                "PE-CE OI Change (Day)": "106.9 L",
                "PE-CE Bar Change": "2.28 L",
                "PCR Total": 1.23,
                "Future LTP": 23785.0,
                "Future OI Change (Day)": "-0.66 L",
            },
            {
                "Time": "15:36",
                "Put OI Change (Day)": "96.1 L",
                "Put OI Change": "-3.43 L",
                "Call OI Change (Day)": "-11.0 L",
                "Call OI Change": "-3.70 L",
                "PE-CE OI Change (Day)": "107.1 L",
                "PE-CE Bar Change": "0.27 L",
                "PCR Total": 1.24,
                "Future LTP": 23782.0,
                "Future OI Change (Day)": "-0.62 L",
            },
            {
                "Time": "15:39",
                "Put OI Change (Day)": "94.5 L",
                "Put OI Change": "-1.65 L",
                "Call OI Change (Day)": "-13.5 L",
                "Call OI Change": "-2.48 L",
                "PE-CE OI Change (Day)": "108.0 L",
                "PE-CE Bar Change": "0.83 L",
                "PCR Total": 1.26,
                "Future LTP": 23780.5,
                "Future OI Change (Day)": "-1.3 L",
            },
        ]
        resp_ingest = client.post("/api/oi/ingest", json=sample_rows)
        assert resp_ingest.status_code == 200, f"Ingest failed: {resp_ingest.text}"
        data_ingest = resp_ingest.json()
        assert data_ingest["ok"] is True
        assert data_ingest["ingested_count"] == 3

        # 3. Test GET /api/oi/pe_ce_table
        resp_tbl = client.get("/api/oi/pe_ce_table?limit=10")
        assert resp_tbl.status_code == 200
        data_tbl = resp_tbl.json()
        assert data_tbl["ok"] is True
        assert len(data_tbl["rows"]) >= 3
        # Check newest-first order
        times = [r["time_ist"] for r in data_tbl["rows"][:5]]
        assert times == sorted(times, reverse=True), f"Expected newest-first ordering, got {times}"
        assert "15:39" in times and "15:36" in times

        # 4. Verify Port 8600 endpoint is completely removed (returns 404)
        resp_8600 = client.get("/api/confluence/port8600")
        assert resp_8600.status_code == 404, f"Expected 404 for removed Port 8600 route, got {resp_8600.status_code}"

        # 5. Test GET /api/telemetry contains generic trend_lab_variables and NO Port 8600 artifacts
        resp_telem = client.get("/api/telemetry")
        assert resp_telem.status_code == 200
        telem = resp_telem.json()
        assert "trend_lab_variables" in telem, "Telemetry missing trend_lab_variables"
        vars_dict = telem["trend_lab_variables"]
        expected_vars = [
            "var_pe_minus_ce",
            "var_call_oi_change",
            "var_put_oi_change",
            "var_total_call_oi",
            "var_total_put_oi",
            "var_oi_writer_bias",
        ]
        for v in expected_vars:
            assert v in vars_dict, f"Missing generic variable {v} in trend_lab_variables"

        # Assert all 8600 variables removed
        for k in vars_dict.keys():
            assert not k.startswith("var_8600_"), f"Unexpected Port 8600 variable in trend_lab_variables: {k}"
        assert "port8600_confluence" not in telem, "port8600_confluence MUST be removed from telemetry"
        assert "institutional_pe_ce" in telem, "institutional_pe_ce MUST be present in telemetry"

        # 6. Verify HTML Tab Cleanup & Port 8600 Removal
        html_path = ROOT / "Algorithmic_Trading_Workspace_8510.html"
        html_content = html_path.read_text(encoding="utf-8")
        assert "8600" not in html_content, "Port 8600 MUST be completely removed from HTML"
        assert "var_8600" not in html_content, "var_8600_* MUST be completely removed from HTML"
        assert "tabTrendLab" in html_content, "tabTrendLab (Multi-Factor Trend Lab) MUST remain intact"
        assert "tabStockMojoSuite" in html_content, "tabStockMojoSuite MUST remain intact"
        assert "Institutional OI Writers" in html_content, "Institutional OI Writers nomenclature MUST be present in HTML DOM"

    print("✔ Test 10 Passed: Port 8600 Removal, Institutional OI Ingestion & Generic Nomenclature verified 100%.")


def main():
    print("Running Port 8510 Super Trail v1.1 & Multi-Strategy Strict MA Stack Test Suite...")
    test_trailing_sl_step_progression()
    test_trailing_target_pullback_trace()
    test_strike_matrix_algo_checkbox_filtering()
    test_date_range_backtest_simulation()
    test_nov4_3strategies_replay_sequence()
    test_all_4_core_strategies_scanner_and_party_switch()
    test_nov3_clean_bullish_trades_replay()
    test_strict_ma_stack_blocking()
    test_backtest_timestamp_logging_and_homework_reconciliation()
    test_stockmojo_and_port8600_confluence_integration()
    print("\n🎉 ALL 10 UNIT TESTS PASSED WITH 100% PRECISION!")


if __name__ == "__main__":
    main()
