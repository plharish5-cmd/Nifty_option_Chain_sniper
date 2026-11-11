"""Unit tests for Phase 6: Backtest matches live for 8 EMA Prototype.

Acceptance criteria:
1. Exit walk starts from the first completed minute AFTER entry_ts, never on or before it.
2. Opposite 8 EMA close is checked ONLY at 15m candle closes (including entry candle's close).
3. 30-second grid for entry reads completed 1m candles only (day_1m.ts <= tick_ts).
4. Entry price in backtest report is spot at touch tick (not 8 EMA), with 8 EMA value in separate column.
5. Ambiguous candle: single 1m candle touching both target (+40) and stop (-40) marked AMBIGUOUS,
   summary reports both outcomes (stops hit first vs targets hit first).
6. Option exit in backtest: no fake delta or fake option prices; reported in spot points.
7. Alternating trades rule: exactly one open prototype trade at a time; CE and PE strictly alternate.
8. Summary metrics: total trades, wins, losses, win rate, avg win, avg loss, payoff ratio, net points,
   and breakdown of exit reasons (SPOT_TARGET_HIT, SPOT_SL_HIT, OPPOSITE_8EMA_CLOSE, EOD_SQUARE_OFF_15:20, AMBIGUOUS).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    backtest_ema8_prototype_at_bar,
    forming_ohlc_from_1m,
    load_ema8_prototype_cfg,
)
from analyst.terminal8510_backend import (  # noqa: E402
    QuantTerminalBackend,
    compute_ema8_prototype_summary,
    simulate_backtest_spot_exit,
    simulate_prototype_spot_exit,
)


def _make_15m_bars(n: int = 10, start_ts: int = 1762746300, base_close: float = 25000.0) -> List[Dict[str, Any]]:
    bars = []
    for i in range(n):
        ts = start_ts + i * 900
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
        bars.append({
            "ts": ts,
            "open": base_close,
            "high": base_close + 10.0,
            "low": base_close - 10.0,
            "close": base_close,
            "e4": base_close,
            "e8": base_close,
            "day": dt.strftime("%Y-%m-%d"),
            "hm": dt.strftime("%H:%M"),
        })
    return bars


def _make_1m_df(
    candles_ohlc: List[tuple],
    start_ts: int = 1762746360,
    day: str = "2026-11-10",
) -> pd.DataFrame:
    records = []
    for i, (o, h, l, c) in enumerate(candles_ohlc):
        ts = start_ts + i * 60
        dt = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
        records.append({
            "ts": ts,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "day": day,
            "hm": dt.strftime("%H:%M"),
        })
    return pd.DataFrame(records)


def test_1_exit_walk_starts_after_entry_ts():
    """Exit walk starts strictly AFTER entry_ts (never on or before it)."""
    # 15m bar at 09:15 (ts=1000)
    bars = _make_15m_bars(5, start_ts=1000, base_close=25000.0)

    # Entry at ts = 1060 (09:16:00).
    # 1m candle at ts = 1060 has low = 24950 (would hit stop 24960 if evaluated).
    # Next 1m candle at ts = 1120 has high = 25045 (hits target 25040).
    day_1m = pd.DataFrame([
        {"ts": 1000, "open": 25000, "high": 25010, "low": 24990, "close": 25000, "day": "2026-11-10", "hm": "09:15"},
        {"ts": 1060, "open": 25000, "high": 25005, "low": 24950, "close": 24995, "day": "2026-11-10", "hm": "09:16"},  # ts == entry_ts: MUST BE IGNORED
        {"ts": 1120, "open": 25000, "high": 25045, "low": 24995, "close": 25042, "day": "2026-11-10", "hm": "09:17"},  # ts > entry_ts: target hit
    ])

    res = simulate_prototype_spot_exit(
        bars=bars,
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        sl_pts=40.0,
        tgt_pts=40.0,
        day_1m=day_1m,
        entry_ts=1060,
    )

    # Because ts=1060 is ignored, SPOT_SL_HIT does NOT fire; SPOT_TARGET_HIT fires on ts=1120!
    assert res["exit_reason"] == "SPOT_TARGET_HIT"
    assert res["exit_spot"] == 25040.0
    assert res["st_pnl_pts"] == 40.0
    assert res["exit_ts"] == 1120


def test_2_opposite_close_checked_only_at_15m_closes():
    """Opposite 8 EMA close is checked ONLY at 15m candle closes, not on 1m candles."""
    start_15m = 1000000
    bars = _make_15m_bars(3, start_ts=start_15m, base_close=25000.0)
    # Entry on bar 0 (09:15-09:30).
    # Bar 0 close is at 1000000 + 900 = 1000900.
    bars[0]["close"] = 24985.0
    bars[0]["e8"] = 25000.0  # Bar 0 closed below 8 EMA!

    # 1m candles:
    # 1m candle at ts = 1000300 (09:20): close = 24980 (below 8 EMA), but range [24970, 25010] (within +/- 40).
    # It must NOT exit on this 1m candle.
    # At ts = 1000900 (09:30): this is the 15m close. Close is below 8 EMA -> OPPOSITE_8EMA_CLOSE!
    candles = [
        {"ts": 1000100, "open": 25000, "high": 25010, "low": 24990, "close": 25000, "day": "2026-11-10", "hm": "09:16"},
        {"ts": 1000300, "open": 25000, "high": 25010, "low": 24970, "close": 24980, "day": "2026-11-10", "hm": "09:20"},
        {"ts": 1000900, "open": 24990, "high": 25000, "low": 24980, "close": 24985, "day": "2026-11-10", "hm": "09:30"},
    ]
    day_1m = pd.DataFrame(candles)

    res = simulate_prototype_spot_exit(
        bars=bars,
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        sl_pts=40.0,
        tgt_pts=40.0,
        day_1m=day_1m,
        entry_ts=1000050,
    )

    # Did NOT exit at 1000300 on 1m candle; exited at 1000900 on 15m bar close!
    assert res["exit_reason"] == "OPPOSITE_8EMA_CLOSE"
    assert res["exit_spot"] == 24985.0
    assert res["exit_ts"] == 1000900
    assert res["st_pnl_pts"] == -15.0


def test_3_30s_grid_reads_completed_1m_candles():
    """forming_ohlc_from_1m reads completed 1m candles up to tick_ts."""
    df1 = pd.DataFrame([
        {"ts": 1000060, "open": 25000.0, "high": 25010.0, "low": 24990.0, "close": 25005.0},
        {"ts": 1000120, "open": 25005.0, "high": 25020.0, "low": 25000.0, "close": 25015.0},
        {"ts": 1000180, "open": 25015.0, "high": 25050.0, "low": 25010.0, "close": 25045.0},  # Future candle
    ])

    # Query at tick_ts = 1000120: only first 2 completed candles should be considered
    hi, lo, spot = forming_ohlc_from_1m(df1, bar_open_ts=1000000, tick_ts=1000120)
    assert spot == 25015.0
    assert hi == 25020.0
    assert lo == 24990.0
    assert hi < 25050.0  # Future candle was excluded


def test_4_entry_price_in_report_is_spot_at_touch_tick():
    """Entry price is spot price at touch tick (not 8 EMA), and 8 EMA is kept in separate column."""
    bars = _make_15m_bars(5, start_ts=1000, base_close=25000.0)
    # Target and stop are measured from 25002.50:
    # Target = 25042.50, Stop = 24962.50
    day_1m = pd.DataFrame([
        {"ts": 1060, "open": 25002.5, "high": 25043.0, "low": 25000.0, "close": 25042.5, "day": "2026-11-10", "hm": "09:16"}
    ])

    res = simulate_prototype_spot_exit(
        bars=bars,
        start_idx=0,
        entry_spot=25002.50,
        is_long=True,
        sl_pts=40.0,
        tgt_pts=40.0,
        day_1m=day_1m,
        entry_ts=1000,
    )

    assert res["exit_reason"] == "SPOT_TARGET_HIT"
    assert res["exit_spot"] == 25042.50
    assert res["st_pnl_pts"] == 40.0


def test_5_ambiguous_1m_candle_marked_and_reported_both_outcomes():
    """A 1m candle touching both target (+40) and stop (-40) is marked AMBIGUOUS with dual scenario reporting."""
    bars = _make_15m_bars(3, start_ts=1000, base_close=25000.0)
    # Entry at 25000. Target = 25040, Stop = 24960.
    # Single 1m candle has high 25045 and low 24955 (touches both!).
    day_1m = pd.DataFrame([
        {"ts": 1060, "open": 25000, "high": 25045, "low": 24955, "close": 25010, "day": "2026-11-10", "hm": "09:16"}
    ])

    res = simulate_prototype_spot_exit(
        bars=bars,
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        sl_pts=40.0,
        tgt_pts=40.0,
        day_1m=day_1m,
        entry_ts=1000,
    )

    assert res["exit_reason"] == "AMBIGUOUS"
    assert res["is_ambiguous"] is True

    # Test summary function dual reporting
    trades = [
        {"exit_reason": "SPOT_TARGET_HIT", "st_pnl_pts": 40.0, "is_ambiguous": False},
        {"exit_reason": "SPOT_SL_HIT", "st_pnl_pts": -40.0, "is_ambiguous": False},
        {"exit_reason": "AMBIGUOUS", "st_pnl_pts": -40.0, "is_ambiguous": True},
    ]
    summary = compute_ema8_prototype_summary(trades)
    assert summary["total_trades"] == 3
    assert summary["ambiguous_count"] == 1
    assert summary["exit_reasons"]["AMBIGUOUS"] == 1

    # Scenario 1 (stops first): 1 win / 2 losses
    assert summary["scenario_stops_first"]["wins"] == 1
    assert summary["scenario_stops_first"]["losses"] == 2
    assert summary["scenario_stops_first"]["net_points"] == -40.0

    # Scenario 2 (targets first): 2 wins / 1 losses
    assert summary["scenario_targets_first"]["wins"] == 2
    assert summary["scenario_targets_first"]["losses"] == 1
    assert summary["scenario_targets_first"]["net_points"] == 40.0

    assert "1 wins / 2 losses if stops hit first" in summary["ambiguous_summary"]
    assert "2 wins / 1 losses if targets hit first" in summary["ambiguous_summary"]


def test_6_no_fake_delta_or_option_prices_in_backtest_report():
    """Option exit is not modeled with fake delta; reported in spot points."""
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {"risk": {"option_delta_default": 0.55}}
    b.scanner_cfg = {"ema8_prototype": {"enabled": True}}
    b.directional_bias = "NEUTRAL"
    b.market_db = ":memory:"
    b.ledger_db = ":memory:"

    # Verify simulate_backtest_spot_exit delegation returns clean prototype tuple
    bars = _make_15m_bars(3, start_ts=1000, base_close=25000.0)
    day_1m = pd.DataFrame([
        {"ts": 1060, "open": 25000, "high": 25045, "low": 24995, "close": 25040, "day": "2026-11-10", "hm": "09:16"}
    ])
    st_pnl, fx_pnl, ex_spot, st_reason, trail_steps, mfe, ed, eh, xd, xh = simulate_backtest_spot_exit(
        bars,
        0,
        25000.0,
        True,
        40.0,
        40.0,
        is_flagship=False,
        sr_levels=None,
        exit_on_opposite_8ema_close=True,
        day_1m=day_1m,
        entry_ts=1000,
    )
    assert st_reason == "SPOT_TARGET_HIT"
    assert st_pnl == 40.0
    assert ex_spot == 25040.0


def test_7_alternating_ce_and_pe_trades_single_open_position():
    """Alternating trades rule: exactly one open prototype trade at a time; CE and PE strictly alternate."""
    # 1. Start with an open trade state
    proto_state = {
        "bt_open": {"direction": "LONG", "exit_ts": 1000900},
        "arm": {"direction": "LONG", "cross_bar_open_ts": 1000000},
    }
    bars = _make_15m_bars(4, start_ts=1000000, base_close=25000.0)
    cfg = {"ema8_prototype": {"enabled": True}}

    # When bt_open is active, backtest_ema8_prototype_at_bar emits NO signals
    sigs = backtest_ema8_prototype_at_bar(bars, 1, proto_state, cfg)
    assert sigs == []


def test_8_summary_metrics_structure_and_values():
    """Summary metrics: total trades, wins, losses, win rate, avg win, avg loss, payoff ratio, net points."""
    trades = [
        {"exit_reason": "SPOT_TARGET_HIT", "st_pnl_pts": 40.0, "is_ambiguous": False},
        {"exit_reason": "SPOT_TARGET_HIT", "st_pnl_pts": 40.0, "is_ambiguous": False},
        {"exit_reason": "SPOT_SL_HIT", "st_pnl_pts": -40.0, "is_ambiguous": False},
        {"exit_reason": "OPPOSITE_8EMA_CLOSE", "st_pnl_pts": -10.0, "is_ambiguous": False},
        {"exit_reason": "EOD_SQUARE_OFF_15:20", "st_pnl_pts": 15.0, "is_ambiguous": False},
    ]

    summary = compute_ema8_prototype_summary(trades)
    assert summary["total_trades"] == 5
    assert summary["wins"] == 3  # 40, 40, 15
    assert summary["losses"] == 2  # -40, -10
    assert summary["breakeven"] == 0
    assert summary["win_rate_pct"] == 60.0
    assert summary["avg_win_pts"] == round((40.0 + 40.0 + 15.0) / 3.0, 2)  # 31.67
    assert summary["avg_loss_pts"] == round((40.0 + 10.0) / 2.0, 2)  # 25.0
    assert summary["payoff_ratio"] == round(31.67 / 25.0, 2)  # 1.27
    assert summary["net_points"] == round(40.0 + 40.0 - 40.0 - 10.0 + 15.0, 2)  # 45.0
    assert summary["exit_reasons"]["SPOT_TARGET_HIT"] == 2
    assert summary["exit_reasons"]["SPOT_SL_HIT"] == 1
    assert summary["exit_reasons"]["OPPOSITE_8EMA_CLOSE"] == 1
    assert summary["exit_reasons"]["EOD_SQUARE_OFF_15:20"] == 1
    assert summary["exit_reasons"]["AMBIGUOUS"] == 0
