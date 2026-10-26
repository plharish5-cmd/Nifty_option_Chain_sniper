"""
tests/test_pe_ce_matrix.py — Unit tests for Institutional PE-CE Difference & Skew Matrix 2.0.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from app.engine import pe_ce_matrix


def test_magnitude_regime_classification():
    cfg = {
        "balanced_range_threshold": 500000,
        "moderate_conviction_threshold": 2000000,
        "large_conviction_threshold": 3000000,
    }

    # < 5L: BALANCED_RANGE (0.5x size)
    k, txt, size = pe_ce_matrix.classify_magnitude_regime(250000, cfg)
    assert k == "BALANCED_RANGE"
    assert size == 0.5
    assert "<5L" in txt

    # 5L - 20L: MODERATE_CONVICTION (1.0x size)
    k, txt, size = pe_ce_matrix.classify_magnitude_regime(1200000, cfg)
    assert k == "MODERATE_CONVICTION"
    assert size == 1.0
    assert "5L–20L" in txt

    # 20L - 30L: LARGE_HIGH_CONVICTION (1.5x size)
    k, txt, size = pe_ce_matrix.classify_magnitude_regime(2400000, cfg)
    assert k == "LARGE_HIGH_CONVICTION"
    assert size == 1.5
    assert "20L+" in txt

    # >= 30L: EXTREME_CROWDED_ALERT (1.0x size with contrarian caution)
    k, txt, size = pe_ce_matrix.classify_magnitude_regime(3500000, cfg)
    assert k == "EXTREME_CROWDED_ALERT"
    assert size == 1.0
    assert "30L+" in txt


def test_slope_flip_detection():
    cfg = {
        "slope_flip_bars": 3,
        "slope_flip_min_impulse": 100000,
    }

    # Bullish Slope Flip: Day diff is still negative (-20.8L), but recent 3 bars are strongly positive
    recent_positive_impulses = [3060000, 1010000, 1070000]
    flip, txt = pe_ce_matrix.detect_slope_flip(recent_positive_impulses, -2080000, cfg)
    assert flip == "BULLISH_SLOPE_FLIP"
    assert "BULLISH SLOPE FLIP" in txt

    # Bearish Slope Flip: Day diff is still positive (+15.4L), but recent 3 bars are strongly negative
    recent_negative_impulses = [-1500000, -800000, -1200000]
    flip, txt = pe_ce_matrix.detect_slope_flip(recent_negative_impulses, 1540000, cfg)
    assert flip == "BEARISH_SLOPE_FLIP"
    assert "BEARISH SLOPE FLIP" in txt

    # Normal ongoing trend (no flip)
    normal_impulses = [500000, 400000, 600000]
    flip, txt = pe_ce_matrix.detect_slope_flip(normal_impulses, 2500000, cfg)
    assert flip == "NONE"


def test_premium_skew_calculation():
    cfg = {
        "premium_skew_steep_pct": 20.0,
        "premium_skew_normal_pct": 5.0,
    }

    # Steep Put Skew (PE >> CE by > 20%)
    s = pe_ce_matrix.calculate_premium_skew(130.0, 100.0, cfg)
    assert s["skew_state"] == "STEEP_PUT_SKEW"
    assert s["skew_pct"] == 30.0
    assert "Downside Hedging" in s["interpretation"]

    # Normal Put Skew (PE > CE by 5% to 20%)
    s = pe_ce_matrix.calculate_premium_skew(110.0, 100.0, cfg)
    assert s["skew_state"] == "NORMAL_PUT_SKEW"
    assert s["skew_pct"] == 10.0
    assert "Protective Bias" in s["interpretation"]

    # Flat Skew (PE ≈ CE within ±5%)
    s = pe_ce_matrix.calculate_premium_skew(102.0, 100.0, cfg)
    assert s["skew_state"] == "FLAT_SKEW"
    assert s["skew_pct"] == 2.0
    assert "Balanced Expectations" in s["interpretation"]

    # Call Skew Rising / Narrowing (CE >= PE by 5% to 20%)
    s = pe_ce_matrix.calculate_premium_skew(90.0, 100.0, cfg)
    assert s["skew_state"] == "CALL_SKEW_NARROWING"
    assert "Upside Momentum" in s["interpretation"]

    # Extreme Call Skew (CE >> PE by > 20%)
    s = pe_ce_matrix.calculate_premium_skew(70.0, 100.0, cfg)
    assert s["skew_state"] == "EXTREME_CALL_SKEW"
    assert "Euphoric Upside Chase" in s["interpretation"]


def test_fut_buildup():
    assert pe_ce_matrix.determine_fut_buildup(15.0, 50000) == "LONG_BUILDUP"
    assert pe_ce_matrix.determine_fut_buildup(-15.0, 50000) == "SHORT_BUILDUP"
    assert pe_ce_matrix.determine_fut_buildup(15.0, -50000) == "SHORT_COVERING"
    assert pe_ce_matrix.determine_fut_buildup(-15.0, -50000) == "LONG_UNWINDING"


def test_strike_scope_filtering_matrix():
    from app import db
    from app.timeutil import now_ist, session_bounds

    t0, _ = session_bounds(now_ist().date())
    exp = "2026-10-27"

    # Seed mock snapshot rows into test db
    db.save_chain_snap(exp, t0, [
        (22500.0, 500000, 600000, 150.0, 140.0, 1000, 1000),
        (22550.0, 400000, 700000, 120.0, 160.0, 1000, 1000),
        (22600.0, 300000, 800000, 90.0, 190.0, 1000, 1000),
    ])
    db.save_chain_snap(exp, t0 + 180, [
        (22500.0, 520000, 650000, 155.0, 135.0, 2000, 2000),
        (22550.0, 410000, 780000, 125.0, 155.0, 2000, 2000),
        (22600.0, 310000, 900000, 95.0, 185.0, 2000, 2000),
    ])
    db.upsert_candles("FUT", [
        {"ts": t0, "o": 22550, "h": 22580, "l": 22540, "c": 22570, "v": 1000},
        {"ts": t0 + 180, "o": 22570, "h": 22600, "l": 22560, "c": 22590, "v": 1200},
    ])

    matrix_10 = pe_ce_matrix.compute_pe_ce_matrix(expiry=exp, scope="atm_10", tf="3m")
    assert matrix_10["ok"] is True
    assert "±10 ATM" in matrix_10["strike_range_label"]

    matrix_5 = pe_ce_matrix.compute_pe_ce_matrix(expiry=exp, scope="atm_5", tf="3m")
    assert matrix_5["ok"] is True
    assert "±5 ATM" in matrix_5["strike_range_label"]

    matrix_full = pe_ce_matrix.compute_pe_ce_matrix(expiry=exp, scope="full", tf="3m")
    assert matrix_full["ok"] is True
    assert "Full Chain" in matrix_full["strike_range_label"]

    # Check rows format
    rows = matrix_10["rows"]
    assert len(rows) > 0
    assert rows[-1]["time_label"] == "Prev Day"
    for r in rows[:-1]:
        assert "put_oi_day_chg" in r
        assert "call_oi_day_chg" in r
        assert "pe_ce_day_diff" in r
        assert "pe_ce_step_diff" in r
        assert "fut_ltp" in r
        assert "fut_oi_day_chg" in r
        assert "fut_buildup" in r
        assert "stage_label" in r


def test_classify_action_stage():
    cfg = {
        "stage_regime_threshold": 500000,
        "stage_step_accel_threshold": 250000,
        "stage_step_digestion_threshold": -250000,
    }

    # Post-15:10 EOD Closing Window
    s = pe_ce_matrix.classify_action_stage(2000000, 300000, "15:15", cfg)
    assert "EOD Closing" in s

    # Bullish Day (+2.0Cr)
    # Step >= 250k -> Stage 3 Bullish Acceleration (Add CE)
    s = pe_ce_matrix.classify_action_stage(20000000, 350000, "10:15", cfg)
    assert "Stage 3 Bullish Acceleration" in s
    assert "Add CE" in s

    # Step > 0 and < 250k -> Stage 2 Dip Buy (CE)
    s = pe_ce_matrix.classify_action_stage(20000000, 50000, "10:18", cfg)
    assert "Stage 2 Dip Buy" in s

    # Step <= -250k -> Stage 5 Bullish Digestion / Trail CE
    s = pe_ce_matrix.classify_action_stage(20000000, -300000, "10:48", cfg)
    assert "Stage 5 Bullish Digestion" in s

    # Step between -250k and 0 -> Stage 4 Hold CE
    s = pe_ce_matrix.classify_action_stage(20000000, -50000, "11:00", cfg)
    assert "Stage 4 Hold CE" in s

    # Bearish Day (-2.0Cr)
    # Step <= -250k -> Stage 3 Bearish Acceleration (Add PE)
    s = pe_ce_matrix.classify_action_stage(-20000000, -350000, "09:25", cfg)
    assert "Stage 3 Bearish Acceleration" in s
    assert "Add PE" in s

    # Step < 0 and > -250k -> Stage 2 Pullback Sell (PE)
    s = pe_ce_matrix.classify_action_stage(-20000000, -50000, "09:30", cfg)
    assert "Stage 2 Pullback Sell" in s

    # Step >= 250k -> Stage 5 Short Covering / Book PE
    s = pe_ce_matrix.classify_action_stage(-20000000, 300000, "09:40", cfg)
    assert "Stage 5 Short Covering" in s

    # Step between 0 and 250k -> Stage 4 Hold PE
    s = pe_ce_matrix.classify_action_stage(-20000000, 50000, "09:45", cfg)
    assert "Stage 4 Hold PE" in s

    # Balanced Day (diff < 500k)
    s = pe_ce_matrix.classify_action_stage(100000, 300000, "09:20", cfg)
    assert "Stage 1 Bullish Breakout" in s

    s = pe_ce_matrix.classify_action_stage(100000, -300000, "09:20", cfg)
    assert "Stage 1 Bearish Breakdown" in s

    s = pe_ce_matrix.classify_action_stage(100000, 50000, "09:20", cfg)
    assert "S0 WAIT" in s


def test_active_expiry_and_both_combination():
    # Calling with cur should resolve to active expiry (not expired 2026-10-13)
    res_cur = pe_ce_matrix.compute_pe_ce_matrix(expiry="cur", tf="3m")
    assert res_cur["ok"] is True
    assert res_cur["expiry"] >= "2026-10-27"

    # Calling with both should succeed and combine cur + next
    res_both = pe_ce_matrix.compute_pe_ce_matrix(expiry="both", tf="3m")
    assert res_both["ok"] is True
    assert len(res_both["rows"]) > 0
    assert "2026-10-27" in res_both["expiry"]


def test_eod_mutual_unwinding_slope_flip():
    # Mutual unwinding into EOD bell: step diff is negative, but both calls and puts are negative
    recent_diffs = [-3500000, -2500000, -2800000]
    call_steps = [-4700000, -10200000, -6300000]
    put_steps = [-8200000, -12700000, -9200000]

    flip, txt = pe_ce_matrix.detect_slope_flip(
        recent_diffs,
        44000000,
        call_steps=call_steps,
        put_steps=put_steps,
        current_time_label="15:24",
    )
    assert flip == "EOD_MUTUAL_UNWINDING"
    assert "EOD Mutual Profit-Booking" in txt
    assert "BEARISH" not in flip


def test_fut_buildup_quadrant_matrix():
    # Price Up & OI Down -> Short Covering (SC)
    assert pe_ce_matrix.determine_fut_buildup(230.0, -205000) == "SHORT_COVERING"
    # Price Down & OI Down -> Long Unwinding (LU)
    assert pe_ce_matrix.determine_fut_buildup(-50.0, -150000) == "LONG_UNWINDING"
    # Price Up & OI Up -> Long Buildup (LB)
    assert pe_ce_matrix.determine_fut_buildup(50.0, 150000) == "LONG_BUILDUP"
    # Price Down & OI Up -> Short Buildup (SB)
    assert pe_ce_matrix.determine_fut_buildup(-50.0, 150000) == "SHORT_BUILDUP"


def test_extrinsic_skew_calculation():
    # At Spot 22770, Strike 22750: CE 76.9 has 20.0 intrinsic value (extrinsic = 56.9)
    # PE 65.0 has 0 intrinsic value (extrinsic = 65.0)
    skew = pe_ce_matrix.calculate_premium_skew(
        atm_pe_ltp=65.0,
        atm_ce_ltp=76.9,
        spot=22770.0,
        atm_strike=22750.0,
    )
    assert skew["pe_extrinsic"] == 65.0
    assert skew["ce_extrinsic"] == 56.9
    assert skew["skew_pct"] > 0  # PE extrinsic > CE extrinsic -> positive put skew
    assert "Put Skew" in skew["skew_label"]


