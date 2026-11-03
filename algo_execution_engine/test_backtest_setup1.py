"""
algo_execution_engine/test_backtest_setup1.py — Unit Tests for Setup #1 Historical Backtester.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from algo_execution_engine.backtest_setup1 import (
    BacktestTrade,
    Setup1BacktestSimulator,
    build_multitimeframe_data,
    load_config,
    load_historical_1m_data,
)


def create_mock_1m_data(n_bars: int = 375, start_price: float = 23500.0) -> pd.DataFrame:
    base_ts = 1762141560
    prices = [start_price]
    for i in range(1, n_bars):
        p = prices[-1] + 0.5 + ((i % 5) - 2) * 1.5
        prices.append(p)

    rows = []
    for i, c in enumerate(prices):
        t = base_ts + i * 60
        mins = 15 + (i % 60)
        hours = 9 + (i // 60)
        time_str = f"{hours:02d}:{mins:02d}:00"
        rows.append({
            "ts": t,
            "open": c - 0.5,
            "high": c + 2.0,
            "low": c - 2.0,
            "close": c,
            "volume": 500.0,
            "session_date": "2026-11-03",
            "session_time": time_str,
        })
    return pd.DataFrame(rows)


def test_build_multitimeframe_data():
    df_1m = create_mock_1m_data(375)
    cfg = load_config()
    df_5m, df_15m = build_multitimeframe_data(df_1m, cfg)

    assert len(df_5m) in (75, 76)
    assert len(df_15m) in (25, 26)
    assert "ema4" in df_5m.columns
    assert "ema8" in df_5m.columns
    assert "ubb_slope" in df_5m.columns
    assert "bb_width" in df_15m.columns


def test_part_k_money_math_and_brokerage():
    simulator = Setup1BacktestSimulator()

    # Winning trade (+40 pts)
    active_trade = {
        "session_date": "2026-11-03",
        "entry_ts": 1000,
        "entry_time": "10:00:00",
        "entry_spot": 23500.0,
        "target": 23540.0,
        "trailed_sl": 23460.0,
        "highest_spot": 23540.0,
        "cost_to_cost_active": True,
    }

    win_trade = simulator._close_trade(
        active_trade=active_trade,
        exit_ts=1300,
        exit_time="10:05:00",
        exit_spot=23540.0,
        reason="Target Hit (+40 pts)",
        is_gap_day=False,
        gap_pts=10.0,
        variant_name="Variant D",
    )

    assert win_trade.spot_pnl_pts == 40.0
    # Linear: 40 * 0.60 * 75 = 1800 gross - 400 = 1400 net
    assert win_trade.linear_gross_inr == 1800.0
    assert win_trade.linear_net_inr == 1400.0
    # Asymmetric: 40 * 0.60 * 75 = 1800 gross - 400 = 1400 net
    assert win_trade.asym_net_inr == 1400.0

    # Losing trade (-40 pts)
    loss_trade = simulator._close_trade(
        active_trade=active_trade,
        exit_ts=1400,
        exit_time="10:10:00",
        exit_spot=23460.0,
        reason="Stop-Loss Hit (-40 pts)",
        is_gap_day=False,
        gap_pts=10.0,
        variant_name="Variant D",
    )

    assert loss_trade.spot_pnl_pts == -40.0
    # Linear: -40 * 0.60 * 75 = -1800 gross - 400 = -2200 net
    assert loss_trade.linear_gross_inr == -1800.0
    assert loss_trade.linear_net_inr == -2200.0
    # Asymmetric cushioned: -40 * 0.50 * 75 = -1500 gross - 400 = -1900 net
    assert loss_trade.asym_net_inr == -1900.0


def test_metrics_calculation():
    simulator = Setup1BacktestSimulator()

    trades = [
        BacktestTrade(
            session_date="2026-11-01",
            entry_ts=1, entry_time="10:00", entry_spot=23500.0,
            exit_ts=2, exit_time="10:30", exit_spot=23540.0, spot_pnl_pts=40.0,
            exit_reason="Target Hit", cost_to_cost_activated=True, highest_spot=23540.0, trailed_sl=23500.0,
            asym_opt_pts=24.0, asym_gross_inr=1800.0, asym_net_inr=1400.0,
            linear_opt_pts=24.0, linear_gross_inr=1800.0, linear_net_inr=1400.0,
            is_gap_day=False, gap_pts=10.0, variant="Variant D",
        ),
        BacktestTrade(
            session_date="2026-11-02",
            entry_ts=3, entry_time="11:00", entry_spot=23600.0,
            exit_ts=4, exit_time="11:20", exit_spot=23560.0, spot_pnl_pts=-40.0,
            exit_reason="SL Hit", cost_to_cost_activated=False, highest_spot=23605.0, trailed_sl=23560.0,
            asym_opt_pts=-20.0, asym_gross_inr=-1500.0, asym_net_inr=-1900.0,
            linear_opt_pts=-24.0, linear_gross_inr=-1800.0, linear_net_inr=-2200.0,
            is_gap_day=False, gap_pts=5.0, variant="Variant D",
        ),
    ]

    metrics = simulator._calculate_variant_metrics(trades, sessions_count=2)
    assert metrics["total_trades"] == 2
    assert metrics["win_rate_pct"] == 50.0
    assert metrics["loss_rate_pct"] == 50.0
    assert metrics["linear_total_net_pnl_inr"] == -800.0
    assert metrics["linear_net_expectancy_inr"] == -400.0


def test_read_real_sqlite_data():
    db_path = "ict_smc_app/backend/data/ict_smc.sqlite3"
    df = load_historical_1m_data(db_path)
    assert not df.empty
    assert "session_date" in df.columns
    assert df["session_date"].nunique() >= 200
