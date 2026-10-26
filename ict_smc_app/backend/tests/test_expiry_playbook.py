"""
tests/test_expiry_playbook.py — Unit tests for Institutional Expiry-Day (Zero-DTE) Playbook Engine.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from app.engine import expiry_playbook


def test_is_expiry_day():
    today = date(2026, 10, 27)
    # Matching date
    assert expiry_playbook.is_expiry_day("2026-10-27", session_date=today) is True
    # Mismatched date without force
    assert expiry_playbook.is_expiry_day("2026-11-03", session_date=today) is False
    # Force expiry preview mode
    assert expiry_playbook.is_expiry_day("2026-11-03", session_date=today, force_expiry_mode=True) is True


def test_compute_straddle_telemetry():
    spot = 22753.2
    chain_rows = [
        {"strike": 22700, "ce_ltp": 120.0, "pe_ltp": 45.0, "ce_oi": 5000000, "pe_oi": 6000000},
        {"strike": 22750, "ce_ltp": 76.9, "pe_ltp": 63.0, "ce_oi": 4000000, "pe_oi": 4500000},
        {"strike": 22800, "ce_ltp": 42.0, "pe_ltp": 105.0, "ce_oi": 7000000, "pe_oi": 3000000},
    ]
    open_chain_rows = [
        {"strike": 22750, "ce_ltp": 110.0, "pe_ltp": 90.0},
    ]
    cfg = {
        "straddle_tight_box_mult": 0.35,
    }
    telemetry = expiry_playbook.compute_straddle_telemetry(spot, chain_rows, open_chain_rows, cfg=cfg)

    assert telemetry["atm_strike"] == 22750.0
    assert telemetry["atm_ce_ltp"] == 76.9
    assert telemetry["atm_pe_ltp"] == 63.0
    assert telemetry["atm_straddle_ltp"] == 139.9
    assert telemetry["upper_breakeven"] == 22889.9  # 22750 + 139.9
    assert telemetry["lower_breakeven"] == 22610.1  # 22750 - 139.9
    assert telemetry["open_straddle_ltp"] == 200.0  # 110 + 90
    # Decay: (200 - 139.9) / 200 * 100 = 30.05 -> 30.0%
    assert telemetry["straddle_decay_pct"] == 30.0
    # Pin bounds: 22750 ± (139.9 * 0.35 = 48.965 -> 49.0)
    assert telemetry["pin_zone_high"] == 22799.0
    assert telemetry["pin_zone_low"] == 22701.0


def test_detect_gamma_pin_or_squeeze():
    spot = 22748.0
    atm_strike = 22750.0
    atm_straddle_ltp = 100.0
    decay_pct = 25.0
    chain_rows = [
        {"strike": 22700, "ce_oi": 2000000, "pe_oi": 4000000},
        {"strike": 22750, "ce_oi": 5000000, "pe_oi": 5000000},
        {"strike": 22800, "ce_oi": 6000000, "pe_oi": 1500000},
    ]
    cfg = {
        "pin_range_pts": 35.0,
        "pin_step_diff_max": 1500000,
        "pin_decay_min_pct": 6.0,
        "straddle_tight_box_mult": 0.35,
        "min_writer_fortify_oi": 1500000,
    }

    # Pin Locked Case
    res = expiry_playbook.detect_gamma_pin_or_squeeze(
        spot, atm_strike, atm_straddle_ltp, decay_pct, chain_rows, pe_ce_step_diff=50000, cfg=cfg
    )
    assert res["is_pinned"] is True
    assert res["pin_state"] == "GAMMA_PIN_ACTIVE"
    assert "GAMMA PIN LOCKED @ 22,750" in res["pin_label"]

    # Upper Expansion Case
    breakout_spot = 22790.0  # Above pin_zone_high (22750 + 35 = 22785)
    res_upper = expiry_playbook.detect_gamma_pin_or_squeeze(
        breakout_spot, atm_strike, atm_straddle_ltp, decay_pct, chain_rows, pe_ce_step_diff=800000, cfg=cfg
    )
    assert res_upper["is_pinned"] is False
    assert res_upper["pin_state"] == "UPPER_BREAKEVEN_EXPANSION"

    # Lower Expansion Case
    breakdown_spot = 22710.0  # Below pin_zone_low (22750 - 35 = 22715)
    res_lower = expiry_playbook.detect_gamma_pin_or_squeeze(
        breakdown_spot, atm_strike, atm_straddle_ltp, decay_pct, chain_rows, pe_ce_step_diff=-800000, cfg=cfg
    )
    assert res_lower["is_pinned"] is False
    assert res_lower["pin_state"] == "LOWER_BREAKEVEN_EXPANSION"


def test_detect_trapped_writers():
    # Spot is 22,740, Call writers at 22,700 are underwater
    spot = 22740.0
    chain_rows = [
        {"strike": 22650, "ce_oi": 500000, "pe_oi": 4000000},
        {"strike": 22700, "ce_oi": 5500000, "pe_oi": 2000000},
        {"strike": 22750, "ce_oi": 4000000, "pe_oi": 2500000},
        {"strike": 22800, "ce_oi": 6000000, "pe_oi": 1000000},
    ]
    cfg = {
        "min_writer_unwind_oi": 1000000,
        "min_writer_fortify_oi": 1500000,
    }
    trapped = expiry_playbook.detect_trapped_writers(spot, chain_rows, cfg=cfg)
    assert trapped["trapped_side"] == "CALLS"
    assert trapped["trapped_strike"] == 22700.0
    assert "CE Writers Trapped" in trapped["unwinding_str"]


def test_evaluate_hero_zero_setup():
    chain_rows = [
        {"strike": 22700, "ce_ltp": 72.0, "pe_ltp": 12.0},
        {"strike": 22750, "ce_ltp": 38.0, "pe_ltp": 35.0},
        {"strike": 22800, "ce_ltp": 15.0, "pe_ltp": 75.0},
    ]
    cfg = {
        "hero_zero_start_hhmm": "12:45",
        "hero_zero_end_hhmm": "14:50",
        "settlement_lock_hhmm": "15:05",
        "min_slot_rvol": 1.4,
        "min_c_rvol": 1.15,
        "min_pe_ce_day_diff": 15000000,
        "sweet_spot_min_premium": 10.0,
        "sweet_spot_max_premium": 75.0,
        "premium_sl_pct": 0.35,
        "tp1_mult": 2.0,
        "tp2_mult": 3.5,
        "index_sl_pts": 22.0,
    }

    # Active Hero-Zero Buy Call in prime window
    hz = expiry_playbook.evaluate_hero_zero_setup(
        spot=22755.0,
        fut_ltp=22765.0,
        vwap=22740.0,
        swing_high_30m=22750.0,
        swing_low_30m=22710.0,
        slot_rvol=1.65,
        c_rvol=1.30,
        chain_rows=chain_rows,
        pe_ce_step_diff=350000,
        pe_ce_day_diff=25000000,
        time_label="13:15",
        cfg=cfg,
    )

    assert hz["state"] == "ACTIVE_HERO_ZERO_BUY_CE"
    assert hz["score"] == 5
    assert hz["contract"] == "NIFTY 22,750 CE"
    assert hz["entry_ltp"] == 38.0
    # SL: 38 * (1 - 0.35) = 24.7
    assert hz["sl_ltp"] == 24.7
    # TP1: 38 * 2.0 = 76.0, TP2: 38 * 3.5 = 133.0
    assert hz["tp1_ltp"] == 76.0
    assert hz["tp2_ltp"] == 133.0
    assert hz["index_sl"] == 22733.0  # 22755 - 22

    # Post-15:05 Settlement Lock
    hz_lock = expiry_playbook.evaluate_hero_zero_setup(
        spot=22755.0,
        fut_ltp=22765.0,
        vwap=22740.0,
        swing_high_30m=22750.0,
        swing_low_30m=22710.0,
        slot_rvol=1.65,
        c_rvol=1.30,
        chain_rows=chain_rows,
        pe_ce_step_diff=350000,
        pe_ce_day_diff=25000000,
        time_label="15:12",
        cfg=cfg,
    )
    assert hz_lock["state"] == "EXPIRY_SETTLEMENT_LOCK"
    assert hz_lock["score"] == 0
    assert "SETTLEMENT LOCK" in hz_lock["badge"]


def test_compute_settlement_vwap():
    candles = [
        {"ts": 1730021400, "c": 22750.0},
        {"ts": 1730021460, "c": 22760.0},
        {"ts": 1730021520, "c": 22770.0},
    ]
    settle = expiry_playbook.compute_settlement_vwap(candles, atm_strike=22750.0)
    assert settle["active"] is True
    assert settle["samples"] == 3
    # Average of 22750, 22760, 22770 = 22760.0
    assert settle["estimated_settlement_price"] == 22760.0
    # ITM CE strikes (< 22760): 22650, 22700, 22750
    assert 22750.0 in settle["itm_ce_strikes"]
    # ITM PE strikes (> 22760): 22800, 22850
    assert 22800.0 in settle["itm_pe_strikes"]
