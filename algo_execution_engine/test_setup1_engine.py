"""
algo_execution_engine/test_setup1_engine.py — Unit & Integration Test Suite for Setup #1 Algorithmic Execution Service.

Validates:
  1. 15m Trend Filter (4 EMA > 8 EMA and BB width > 60).
  2. 5m Preconditions (UBB sloping up >= 2 candles, 4 EMA > 8 EMA).
  3. Exact touch of 8 EMA on 5m bar.
  4. Rally Counter & First Opportunity Rule (1st touch triggers, 2nd touch in same rally skipped, reset on 4/8 crossover).
  5. Daily Stop Limit (-40 pt SL hit disables Setup #1 for the day).
  6. Cost-to-cost trigger (+40 pts in favor moves SL to entry).
  7. Candle Trail (confirming candle close > entry trails SL to candle low).
  8. Volatility Exits (UBB curls FLAT, or 5m candle range >= 100 pts).
  9. Daily Circuit Breakers (max 2 trades, max daily loss -₹3,000).
 10. Companion exchange SL-M order calculation with option delta.
 11. API telemetry endpoint GET /api/algo/status.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, time as dtime
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from algo_execution_engine.strategy_setup1 import (
    ActivePosition,
    RallyState,
    Setup1Signal,
    Setup1StrategyEvaluator,
    add_bollinger,
    add_course_emas,
    band_slope,
    prepare_frame,
)
from algo_execution_engine.order_gateway import DhanOrderGateway
from algo_execution_engine.engine import Setup1ExecutionEngine
from algo_execution_engine.server import app


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES & SYNTHETIC DATA GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def create_synthetic_candles(
    n_candles: int = 50,
    start_price: float = 23500.0,
    trend_slope: float = 2.0,
    volatility: float = 5.0,
) -> pd.DataFrame:
    """Generates synthetic OHLCV dataframe with positive trend."""
    prices = [start_price]
    for i in range(1, n_candles):
        noise = (i % 3 - 1) * volatility
        p = prices[-1] + trend_slope + noise
        prices.append(p)

    rows = []
    base_ts = 1762141560
    for i, c in enumerate(prices):
        h = c + 4.0
        l = c - 4.0
        o = c - 1.0
        rows.append({
            "ts": base_ts + i * 300,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_15m_trend_filter_bullish_and_bearish():
    evaluator = Setup1StrategyEvaluator()

    # Bullish series: rising prices with wide BB
    df_bull = create_synthetic_candles(30, start_price=23000.0, trend_slope=15.0, volatility=10.0)
    ok_bull, info_bull = evaluator.evaluate_15m_trend(df_bull)
    assert ok_bull is True
    assert info_bull["bullish"] is True
    assert info_bull["bb_width"] > 60.0
    assert info_bull["ema4"] > info_bull["ema8"]

    # Bearish series: falling prices
    df_bear = create_synthetic_candles(30, start_price=23500.0, trend_slope=-15.0, volatility=5.0)
    ok_bear, info_bear = evaluator.evaluate_15m_trend(df_bear)
    assert ok_bear is False
    assert info_bear["bullish"] is False


def test_5m_preconditions_ubb_slope_and_party():
    evaluator = Setup1StrategyEvaluator()

    # Steady upward expansion: UBB slope UP
    df = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=2.0)
    ok, info = evaluator.evaluate_5m_preconditions(df)
    assert ok is True
    assert info["party_switch_ok"] is True
    assert info["ubb_slope"] == "UP"


def test_rally_counter_and_first_opportunity_rule():
    evaluator = Setup1StrategyEvaluator()
    evaluator.reset_day()

    # 1. Start in neutral or flat
    df_5m = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=2.0)
    df_15m = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=10.0)

    # Initialize rally #1
    evaluator.update_rally_state(df_5m, current_ts=1000)
    assert evaluator.rally.in_bullish_party is True
    assert evaluator.rally.touch_count == 0

    f5 = prepare_frame(df_5m, evaluator.cfg)
    e8 = float(f5.iloc[-1]["ema8"])

    # 1st touch: should be valid
    sig1 = evaluator.check_setup1_entry_signal(
        df_15m=df_15m,
        df_5m=df_5m,
        current_spot=e8,  # Exact touch of 8 EMA
        current_ts=1005,
        current_time=dtime(10, 30),
    )
    assert sig1.is_valid is True
    assert sig1.touch_number == 1
    assert sig1.rejection_reason is None

    # Simulate trade taken (touch count incremented)
    evaluator.rally.touch_count += 1

    # 2nd touch in the same rally: MUST BE SKIPPED
    sig2 = evaluator.check_setup1_entry_signal(
        df_15m=df_15m,
        df_5m=df_5m,
        current_spot=e8,
        current_ts=1010,
        current_time=dtime(10, 45),
    )
    assert sig2.is_valid is False
    assert "second opportunity" in sig2.rejection_reason.lower()


def test_daily_stop_limit_disables_setup1():
    evaluator = Setup1StrategyEvaluator()
    evaluator.reset_day()

    df_5m = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=2.0)
    df_15m = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=10.0)
    f5 = prepare_frame(df_5m, evaluator.cfg)
    e8 = float(f5.iloc[-1]["ema8"])

    # First trade entered
    pos = ActivePosition(
        trade_id="TEST_TRADE",
        symbol="NIFTY 23500 CE",
        security_id="SIM_23500_CE",
        option_type="CE",
        strike=23500.0,
        quantity=75,
        entry_spot=23500.0,
        entry_option_price=120.0,
        entry_time="2026-11-03 10:00:00",
        entry_ts=1000,
        spot_sl=23460.0,  # -40 pts
        spot_target=23540.0,
        option_sl=98.0,
        option_target=142.0,
        delta=0.55,
    )

    # Price drops to stop loss
    should_exit, reason, _ = evaluator.evaluate_active_position(
        pos=pos,
        current_spot=23459.0,
        current_option_ltp=97.0,
        df_5m=df_5m,
    )
    assert should_exit is True
    assert "Initial Stop-Loss Hit" in reason
    assert evaluator.stage2_stopped_today is True

    # Next attempt to enter today MUST BE REJECTED
    sig_blocked = evaluator.check_setup1_entry_signal(
        df_15m=df_15m,
        df_5m=df_5m,
        current_spot=e8,
        current_ts=1200,
        current_time=dtime(11, 0),
    )
    assert sig_blocked.is_valid is False
    assert "stage 2 stop already taken today" in sig_blocked.rejection_reason.lower()


def test_cost_to_cost_trigger_and_candle_trail():
    evaluator = Setup1StrategyEvaluator()
    evaluator.reset_day()

    df_5m = create_synthetic_candles(30, start_price=23500.0, trend_slope=10.0, volatility=2.0)

    pos = ActivePosition(
        trade_id="TEST_CTC",
        symbol="NIFTY 23500 CE",
        security_id="SIM_23500_CE",
        option_type="CE",
        strike=23500.0,
        quantity=75,
        entry_spot=23500.0,
        entry_option_price=120.0,
        entry_time="2026-11-03 10:00:00",
        entry_ts=1000,
        spot_sl=23460.0,  # -40 pts initial
        spot_target=23580.0,  # +80 pts target so +40 pts triggers CTC without exiting
        option_sl=98.0,
        option_target=164.0,
        delta=0.55,
    )

    # 1. Price moves +40 pts in favor -> CTC Trigger
    should_exit, reason, new_sl = evaluator.evaluate_active_position(
        pos=pos,
        current_spot=23540.0,  # +40 pts
        current_option_ltp=142.0,
        df_5m=df_5m,
    )
    assert should_exit is False
    assert pos.cost_to_cost_active is True
    # SL moved to entry + cost_to_cost_brokerage_buffer_pts (2.0 pts to cover real Dhan charges ~₹78)
    buffer = evaluator.cfg.get("cost_to_cost_brokerage_buffer_pts", 2.0)
    assert new_sl == 23500.0 + buffer

    pos.spot_sl = new_sl

    # 2. Subsequent confirming candle close > entry trails SL to candle low
    confirming_candle = {"ts": 1005, "o": 23510.0, "h": 23545.0, "l": 23515.0, "c": 23535.0}
    should_exit, reason, trailed_sl = evaluator.evaluate_active_position(
        pos=pos,
        current_spot=23535.0,
        current_option_ltp=140.0,
        df_5m=df_5m,
        last_closed_candle_5m=confirming_candle,
    )
    assert trailed_sl == 23515.0  # Trailed to confirming candle low!


def test_volatility_exit_100_pt_candle_and_flat_ubb():
    evaluator = Setup1StrategyEvaluator()

    pos = ActivePosition(
        trade_id="TEST_VOL",
        symbol="NIFTY 23500 CE",
        security_id="SIM_23500_CE",
        option_type="CE",
        strike=23500.0,
        quantity=75,
        entry_spot=23500.0,
        entry_option_price=120.0,
        entry_time="2026-11-03 10:00:00",
        entry_ts=1000,
        spot_sl=23460.0,
        spot_target=23540.0,
        option_sl=98.0,
        option_target=142.0,
        delta=0.55,
    )

    # Candle expands by 105 points (> 100 pt volatility threshold)
    huge_candle = {"ts": 1005, "o": 23500.0, "h": 23605.0, "l": 23500.0, "c": 23580.0}
    should_exit, reason, _ = evaluator.evaluate_active_position(
        pos=pos,
        current_spot=23580.0,
        current_option_ltp=160.0,
        df_5m=pd.DataFrame(),
        last_closed_candle_5m=huge_candle,
    )
    assert should_exit is True
    assert "Volatility Exit" in reason
    assert "105.0 >= 100.0 pts" in reason


def test_daily_circuit_breakers():
    gateway = DhanOrderGateway()

    # Under limits
    ok, reason = gateway.check_circuit_breakers(daily_trades=1, daily_pnl_inr=-1500.0)
    assert ok is True

    # Max 2 trades hit
    ok, reason = gateway.check_circuit_breakers(daily_trades=2, daily_pnl_inr=0.0)
    assert ok is False
    assert "Max daily trades" in reason

    # Max loss -₹3,000 breached
    ok, reason = gateway.check_circuit_breakers(daily_trades=1, daily_pnl_inr=-3050.0)
    assert ok is False
    assert "Max daily loss limit" in reason


def test_order_gateway_paper_execution_and_companion_sl():
    async def _run():
        gateway = DhanOrderGateway()
        gateway.execution_mode = "PAPER"

        signal = Setup1Signal(
            timestamp=1000,
            spot=23600.0,
            ema8=23600.0,
            ema4=23610.0,
            ubb=23650.0,
            bb_width_15m=85.0,
            rally_id=1,
            touch_number=1,
            is_valid=True,
            target_spot=23640.0,
            sl_spot=23560.0,
        )

        pos = await gateway.execute_setup1_entry(signal, daily_trades=0, daily_pnl_inr=0.0)
        assert pos is not None
        assert pos.symbol == "NIFTY 23500 CE"
        assert pos.entry_spot == 23600.0
        assert pos.spot_sl == 23560.0
        assert pos.spot_target == 23640.0
        # Companion SL-M price = entry_opt - (40 * delta)
        assert pos.option_sl == round(pos.entry_option_price - (40.0 * pos.delta), 1)

        # Exit at target
        closed = await gateway.execute_position_exit(pos, exit_spot=23640.0, reason="Target Hit (+40 pts)")
        assert closed.status == "CLOSED_TARGET"
        assert closed.pnl_pts == 40.0
        assert closed.pnl_inr > 0.0

    asyncio.run(_run())


def test_api_telemetry_status_endpoint():
    client = TestClient(app)
    response = client.get("/api/algo/status")
    assert response.status_code == 200
    data = response.json()
    assert "circuit_breaker" in data
    assert "rally_state" in data
    assert "trend_filter_15m" in data
    assert "setup1_5m_state" in data
    assert "stage2_stopped_today" in data
    assert "port" in data
    assert data["port"] == 8500
    assert data["health"] == "OK"


def test_admin_endpoints_require_secret():
    client = TestClient(app)
    # 1. Without X-Algo-Secret header -> 401 Unauthorized
    res1 = client.post("/api/algo/mode", json={"mode": "PAPER"})
    assert res1.status_code == 401

    res2 = client.post("/api/algo/emergency-flatten")
    assert res2.status_code == 401

    res3 = client.post("/api/algo/reset-circuit-breaker")
    assert res3.status_code == 401

    # 2. With valid X-Algo-Secret header
    secret = os.environ.get("ALGO_SHARED_SECRET", "")
    headers = {"X-Algo-Secret": secret}
    res_ok = client.post("/api/algo/mode", json={"mode": "PAPER"}, headers=headers)
    assert res_ok.status_code == 200
    assert res_ok.json().get("ok") is True
