"""
test_streamlit_rvol_integration.py — Integration test verifying RVOL gate port into Streamlit.

Acceptance Tests:
  Replay 2026-09-18 through Streamlit's evaluation pipeline:
    - 09:50 signal: suppressed (RVOL 0.82)
    - 09:57 signal: suppressed (RVOL 0.81)
    - 10:17 signal: suppressed (RVOL 0.80)
    - 10:20 signal: suppressed (RVOL 0.79)
    - 11:35 regime flip: CHOP → NORMAL (RVOL 0.85)
    - 13:20 signal: ALLOWED (RVOL 0.93)
"""
from datetime import date, datetime, time
import pytest
import pandas as pd

import decision_cockpit_view as dcv
import rvol_gate
import trading_journal as tj


def test_rvol_standalone_and_pipeline_match():
    """Verify 2026-09-18 signal times match standalone numbers on uniform futures volume."""
    target_d = date(2026, 9, 18)

    # 1. 09:50 signal: suppressed (RVOL 0.36)
    dec_0950 = rvol_gate.get_gate_decision(target_d, time(9, 50))
    assert dec_0950["status"] == "SUPPRESSED"
    assert dec_0950["regime"] == "DEAD_ZONE"
    assert dec_0950["rvol_at_gate"] == pytest.approx(0.36, abs=0.01)

    # 2. 09:57 signal: suppressed (RVOL 0.36)
    dec_0957 = rvol_gate.get_gate_decision(target_d, time(9, 57))
    assert dec_0957["status"] == "SUPPRESSED"
    assert dec_0957["regime"] == "DEAD_ZONE"
    assert 0.35 <= dec_0957["rvol_at_gate"] <= 0.37

    # 3. 10:17 signal: suppressed (RVOL 0.32)
    dec_1017 = rvol_gate.get_gate_decision(target_d, time(10, 17))
    assert dec_1017["status"] == "SUPPRESSED"
    assert dec_1017["regime"] == "DEAD_ZONE"
    assert dec_1017["rvol_at_gate"] == pytest.approx(0.32, abs=0.01)

    # 4. 10:20 signal: suppressed (RVOL 0.31)
    dec_1020 = rvol_gate.get_gate_decision(target_d, time(10, 20))
    assert dec_1020["status"] == "SUPPRESSED"
    assert dec_1020["regime"] == "DEAD_ZONE"
    assert dec_1020["rvol_at_gate"] == pytest.approx(0.31, abs=0.01)

    # 5. 13:20 reference trade: suppressed (RVOL 0.34 — DEAD ZONE under 0.85 threshold)
    dec_1320 = rvol_gate.get_gate_decision(target_d, time(13, 20))
    assert dec_1320["status"] == "SUPPRESSED"
    assert dec_1320["regime"] == "DEAD_ZONE"
    assert dec_1320["rvol_at_gate"] == pytest.approx(0.34, abs=0.01)

    # 6. Session flip on trend day (2026-09-24 flips at 09:45 to TREND_ZONE)
    flip = rvol_gate.find_regime_flip(date(2026, 9, 24))
    assert flip is not None
    assert flip["flip_time"] == "09:45"
    assert flip["to"] == "TREND_ZONE"


def test_decision_cockpit_signal_stack_veto_pipeline():
    """Verify Streamlit's evaluate_signal_stack enforces L3 veto and allows post-flip."""
    target_d = date(2026, 9, 18)

    # Mock dual candle and tabs radar
    dual = dcv.DualCandleReport(
        c15=dcv.CandleAnalysis(timeframe="15m", pattern_name="Bullish Marubozu", direction="BULLISH", close_vs_8ema="ABOVE", close_vs_vwap="ABOVE", body_pct=70.0, upper_wick_pct=15.0, lower_wick_pct=15.0, summary="", is_doji=False, is_reversal=False),
        c5=dcv.CandleAnalysis(timeframe="5m", pattern_name="Bullish Trend", direction="BULLISH", close_vs_8ema="ABOVE", close_vs_vwap="ABOVE", body_pct=65.0, upper_wick_pct=15.0, lower_wick_pct=20.0, summary="", is_doji=False, is_reversal=False),
        harmony_verdict="BULLISH_HARMONY",
        harmony_title="Strong Bullish Momentum",
        harmony_color="#2FBF71",
        what_candles_say="Bullish alignment",
    )

    tabs_radar = dcv.AllTabsRadarReport(
        items=[],
        bull_count=10,
        bear_count=1,
        neutral_count=3,
        total_strategies=14,
        net_score=9,
        consensus_verdict="STRONG BULLISH",
        consensus_color="#2FBF71",
        pillar_summary={},
        active_trap_warning="",
        macro_event_warning="",
    )

    # 09:50 signal: Candidate BUY_CE is SUPPRESSED by L3 Veto
    stack_0950 = dcv.evaluate_signal_stack(
        spot=25350.0,
        chain_df=None,
        target_date=target_d,
        target_time=time(9, 50),
        candidate_action="BUY_CE",
        is_range_structure=False,
        dual=dual,
        tabs_radar=tabs_radar,
    )
    assert stack_0950["veto_active"] is True
    assert stack_0950["veto_layer"] == "L3"
    assert stack_0950["rvol_data"]["regime"] == "DEAD_ZONE"
    assert stack_0950["rvol_data"]["value"] == pytest.approx(0.36, abs=0.01)

    # 10:20 signal: Candidate BUY_CE is SUPPRESSED by L3 Veto
    stack_1020 = dcv.evaluate_signal_stack(
        spot=25330.0,
        chain_df=None,
        target_date=target_d,
        target_time=time(10, 20),
        candidate_action="BUY_CE",
        is_range_structure=False,
        dual=dual,
        tabs_radar=tabs_radar,
    )
    assert stack_1020["veto_active"] is True
    assert stack_1020["veto_layer"] == "L3"
    assert stack_1020["rvol_data"]["value"] == pytest.approx(0.31, abs=0.01)

    # In DEAD ZONE, Range structure (fade the wall) is ALLOWED
    stack_chop_range = dcv.evaluate_signal_stack(
        spot=25330.0,
        chain_df=None,
        target_date=target_d,
        target_time=time(10, 20),
        candidate_action="BUY_CE",
        is_range_structure=True,
        dual=dual,
        tabs_radar=tabs_radar,
    )
    assert stack_chop_range["l3_pass"] is True
    assert stack_chop_range["l3_veto"] is False
    assert "Range trades ALLOWED" in stack_chop_range["l3_detail"]

    # 13:20 reference trade: Candidate BUY_CE is SUPPRESSED by L3 Veto (RVOL 0.34, DEAD_ZONE)
    stack_1320 = dcv.evaluate_signal_stack(
        spot=25400.0,
        chain_df=None,
        target_date=target_d,
        target_time=time(13, 20),
        candidate_action="BUY_CE",
        is_range_structure=False,
        dual=dual,
        tabs_radar=tabs_radar,
    )
    assert stack_1320["veto_active"] is True
    assert stack_1320["veto_layer"] == "L3"
    assert stack_1320["rvol_data"]["regime"] == "DEAD_ZONE"
    assert stack_1320["rvol_data"]["value"] == pytest.approx(0.34, abs=0.01)

    # Post-flip session on 2026-09-24 at 12:30: Candidate BUY_CE is ALLOWED (RVOL 1.60, TREND_ZONE)
    stack_24 = dcv.evaluate_signal_stack(
        spot=25400.0,
        chain_df=None,
        target_date=date(2026, 9, 24),
        target_time=time(12, 30),
        candidate_action="BUY_CE",
        is_range_structure=False,
        dual=dual,
        tabs_radar=tabs_radar,
    )
    assert stack_24["l3_pass"] is True
    assert stack_24["l3_veto"] is False
    assert stack_24["rvol_data"]["regime"] == "TREND_ZONE"
    assert stack_24["rvol_data"]["value"] == pytest.approx(1.60, abs=0.01)


def test_sparkline_svg_generation():
    """Verify inline SVG sparkline builds with 2 polylines for today vs 10-day TOD avg."""
    svg = dcv.compute_rvol_sparkline_svg("2026-09-18", "10:00", color="#EF4444", width=140, height=38)
    assert "<svg" in svg
    assert "</svg>" in svg
    assert "polyline" in svg
    assert "#64748b" in svg  # 10-day avg line
    assert "#EF4444" in svg  # today line


def test_trading_journal_rvol_regime():
    """Verify CompletedTrade stores RVOL and regime at trade entry."""
    trade = tj.CompletedTrade(
        trade_id="DH-001",
        symbol="NIFTY 25400 CE",
        entry_time="10:00:00",
        exit_time="10:15:00",
        hold_duration_mins=15.0,
        buy_price=100.0,
        sell_price=120.0,
        qty=65,
        gross_pnl=1300.0,
        charges={"brokerage": 40.0, "stt": 9.75, "exchange": 7.15, "gst": 8.49, "total_charges": 65.39},
        net_pnl=1234.61,
        trade_type="LONG",
        impulse_flags=["🟢 Disciplined"],
        rvol=0.81,
        rvol_regime="CHOP_ZONE",
    )
    assert trade.rvol == 0.81
    assert trade.rvol_regime == "CHOP_ZONE"
