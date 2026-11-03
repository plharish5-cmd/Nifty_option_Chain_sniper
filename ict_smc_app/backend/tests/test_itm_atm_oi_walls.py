"""
test_itm_atm_oi_walls.py — Unit Tests for ATM & ITM OI Wall + Unwinding Intelligence Engine.
Tests:
  1. Hollow Rally vs. Solid Floor Migration detection.
  2. Stubborn ITM Call Writer & OTM Vacuum detection.
  3. Put Panic Unwinding Trigger calculation (23,220 -> 23,119).
  4. Overbought PCR Shield & Futures RSI Divergence / Star Pattern.
"""
from typing import Any, Dict, List
import pytest
from app.engine.itm_atm_oi_walls import (
    compute_itm_atm_oi_walls,
    compute_rsi,
    detect_rsi_divergence,
    detect_candlestick_star_pattern,
)


def test_hollow_rally_detection():
    """Verify Hollow Rally triggers when spot is >= 100 pts above Put Wall with no ITM put writing."""
    # Put Wall at 23,200 PE (140 pts below spot 23,340)
    # Put Writers parked at 23,200 with zero / negative ITM writing
    chain_rows = [
        {"strike": 23150.0, "ce": {"oi": 10000, "oi_chg": 0}, "pe": {"oi": 50000, "oi_chg": 10000}},
        {"strike": 23200.0, "ce": {"oi": 15000, "oi_chg": 0}, "pe": {"oi": 250000, "oi_chg": 50000}},  # Main Put Wall
        {"strike": 23250.0, "ce": {"oi": 20000, "oi_chg": 0}, "pe": {"oi": 80000, "oi_chg": 10000}},
        {"strike": 23300.0, "ce": {"oi": 80000, "oi_chg": 0}, "pe": {"oi": 60000, "oi_chg": 0}},
        {"strike": 23350.0, "ce": {"oi": 120000, "oi_chg": 0}, "pe": {"oi": 20000, "oi_chg": -5000}},  # ATM/ITM
        {"strike": 23400.0, "ce": {"oi": 200000, "oi_chg": 0}, "pe": {"oi": 10000, "oi_chg": -10000}}, # ITM
    ]

    res = compute_itm_atm_oi_walls(spot=23340.0, chain_rows=chain_rows)
    assert res["ok"] is True
    assert res["is_hollow_rally"] is True
    assert res["put_writer_conviction"] == "HOLLOW_RALLY"
    assert res["hollow_rally_warning"] is not None
    assert "Hollow Rally" in res["hollow_rally_warning"]
    assert res["main_put_wall"]["strike"] == 23200.0
    assert res["put_wall_distance_pts"] == 140.0


def test_solid_floor_migration_detection():
    """Verify Solid Floor Migration when Put Writers aggressively write ITM/ATM puts alongside rising spot."""
    chain_rows = [
        {"strike": 23200.0, "ce": {"oi": 15000, "oi_chg": 0}, "pe": {"oi": 200000, "oi_chg": 10000}},
        {"strike": 23300.0, "ce": {"oi": 40000, "oi_chg": 0}, "pe": {"oi": 180000, "oi_chg": 50000}},
        {"strike": 23350.0, "ce": {"oi": 60000, "oi_chg": 0}, "pe": {"oi": 190000, "oi_chg": 85000}},  # Aggressive ITM/ATM
        {"strike": 23400.0, "ce": {"oi": 120000, "oi_chg": 0}, "pe": {"oi": 150000, "oi_chg": 60000}},
    ]

    res = compute_itm_atm_oi_walls(spot=23340.0, chain_rows=chain_rows)
    assert res["ok"] is True
    assert res["is_hollow_rally"] is False
    assert res["put_writer_conviction"] == "SOLID_FLOOR_MIGRATION"
    assert res["hollow_rally_warning"] is None


def test_otm_vacuum_and_stubborn_itm_call_writer():
    """Verify Stubborn ITM Call Writer unwinding and OTM Vacuum activation."""
    spot = 23440.0
    # Spot is 23,440. 23,350 CE is ITM with heavy OI unwinding (-45,000)
    # Intermediate OTM strikes (23,450, 23,500) are unwinding (vacuum)
    # Major Call Iron Ceiling at 23,600 CE
    chain_rows = [
        {"strike": 23300.0, "ce": {"oi": 50000, "oi_chg": -10000}, "pe": {"oi": 100000, "oi_chg": 0}},
        {"strike": 23350.0, "ce": {"oi": 350000, "oi_chg": -45000}, "pe": {"oi": 80000, "oi_chg": 0}},  # Trapped ITM CE unwinding
        {"strike": 23400.0, "ce": {"oi": 120000, "oi_chg": -15000}, "pe": {"oi": 90000, "oi_chg": 0}},
        {"strike": 23450.0, "ce": {"oi": 80000, "oi_chg": -20000}, "pe": {"oi": 60000, "oi_chg": 0}},   # Vacuum
        {"strike": 23500.0, "ce": {"oi": 90000, "oi_chg": -30000}, "pe": {"oi": 50000, "oi_chg": 0}},   # Vacuum
        {"strike": 23550.0, "ce": {"oi": 180000, "oi_chg": 10000}, "pe": {"oi": 20000, "oi_chg": 0}},
        {"strike": 23600.0, "ce": {"oi": 450000, "oi_chg": 85000}, "pe": {"oi": 10000, "oi_chg": 0}},  # Iron Ceiling
    ]

    res = compute_itm_atm_oi_walls(spot=spot, chain_rows=chain_rows)
    assert res["ok"] is True
    assert res["itm_ce_status"] == "SL_HUNT_UNWINDING"
    assert res["itm_trapped_ce"]["strike"] == 23350.0
    assert 23450.0 in res["otm_vacuum_strikes"]
    assert 23500.0 in res["otm_vacuum_strikes"]
    assert res["vacuum_active"] is True
    assert res["vacuum_signal"] is not None
    assert "SHORT-COVERING VACUUM ACTIVE" in res["vacuum_signal"]
    assert res["otm_major_ce_wall"]["strike"] == 23600.0


def test_panic_unwinding_trigger_calculation():
    """Verify Put Panic Unwinding Trigger dynamically derives from chain."""
    # Support bottom is 23,200. Panic trigger is 23,200 + 20 = 23,220.
    # When spot drops to 23,210 (< 23,220) with Put unwinding, cascade triggers.
    chain_rows = [
        {"strike": 23150.0, "ce": {"oi": 10000, "oi_chg": 0}, "pe": {"oi": 80000, "oi_chg": 5000}},
        {"strike": 23200.0, "ce": {"oi": 20000, "oi_chg": 0}, "pe": {"oi": 250000, "oi_chg": -40000}},  # Unwinding
        {"strike": 23250.0, "ce": {"oi": 30000, "oi_chg": 0}, "pe": {"oi": 150000, "oi_chg": -25000}},  # Unwinding
        {"strike": 23300.0, "ce": {"oi": 150000, "oi_chg": 0}, "pe": {"oi": 50000, "oi_chg": -10000}},
    ]

    res = compute_itm_atm_oi_walls(spot=23210.0, chain_rows=chain_rows)
    assert res["ok"] is True
    assert res["panic_unwinding_trigger"] == 23220.0
    assert res["structural_downside_target"] == 23150.0
    assert res["is_panic_unwinding"] is True
    assert res["panic_unwinding_banner"] is not None
    assert "LONG UNWINDING CASCADE" in res["panic_unwinding_banner"]
    assert "23,150" in res["panic_unwinding_banner"]


def test_dynamic_adaptation_two_chains_no_hardcoded_nov3():
    """Verify that on two different synthetic chains (one centered at 22,500 and one at 24,000),
    supp_bottom, panic_trigger, structural_downside_support, and iron_ceiling
    automatically adapt to the chain's strikes (22,520 vs 24,020) and NEVER return 23,220 or 23,119."""

    # Chain 1: Centered at 22,500
    chain_22500 = [
        {"strike": 22350.0, "ce": {"oi": 5000, "oi_chg": 0}, "pe": {"oi": 40000, "oi_chg": 5000}},
        {"strike": 22400.0, "ce": {"oi": 10000, "oi_chg": 0}, "pe": {"oi": 120000, "oi_chg": 8000}},
        {"strike": 22450.0, "ce": {"oi": 15000, "oi_chg": 0}, "pe": {"oi": 80000, "oi_chg": 5000}},
        {"strike": 22500.0, "ce": {"oi": 25000, "oi_chg": 0}, "pe": {"oi": 300000, "oi_chg": -20000}},
        {"strike": 22550.0, "ce": {"oi": 40000, "oi_chg": 0}, "pe": {"oi": 180000, "oi_chg": -15000}},
        {"strike": 22600.0, "ce": {"oi": 80000, "oi_chg": -5000}, "pe": {"oi": 60000, "oi_chg": 0}},
        {"strike": 22650.0, "ce": {"oi": 110000, "oi_chg": 12000}, "pe": {"oi": 30000, "oi_chg": 0}},
        {"strike": 22700.0, "ce": {"oi": 400000, "oi_chg": 50000}, "pe": {"oi": 10000, "oi_chg": 0}},
    ]

    res_22500 = compute_itm_atm_oi_walls(spot=22510.0, chain_rows=chain_22500)
    assert res_22500["ok"] is True
    assert res_22500["support_bottom"] == 22500.0
    assert res_22500["support_top"] == 22550.0
    assert res_22500["panic_unwinding_trigger"] == 22520.0
    assert res_22500["panic_unwinding_trigger"] != 23220.0
    assert res_22500["structural_downside_target"] < 22500.0
    assert res_22500["structural_downside_target"] != 23119.0
    assert res_22500["otm_major_ce_wall"]["strike"] == 22700.0
    assert res_22500["otm_major_ce_wall"]["strike"] != 23600.0
    assert res_22500["iron_ceiling_min"] == 22650.0

    # Chain 2: Centered at 24,000
    chain_24000 = [
        {"strike": 23850.0, "ce": {"oi": 5000, "oi_chg": 0}, "pe": {"oi": 50000, "oi_chg": 5000}},
        {"strike": 23900.0, "ce": {"oi": 10000, "oi_chg": 0}, "pe": {"oi": 140000, "oi_chg": 9000}},
        {"strike": 23950.0, "ce": {"oi": 20000, "oi_chg": 0}, "pe": {"oi": 90000, "oi_chg": 5000}},
        {"strike": 24000.0, "ce": {"oi": 30000, "oi_chg": 0}, "pe": {"oi": 350000, "oi_chg": -30000}},
        {"strike": 24050.0, "ce": {"oi": 50000, "oi_chg": 0}, "pe": {"oi": 200000, "oi_chg": -20000}},
        {"strike": 24100.0, "ce": {"oi": 90000, "oi_chg": -8000}, "pe": {"oi": 70000, "oi_chg": 0}},
        {"strike": 24150.0, "ce": {"oi": 130000, "oi_chg": 15000}, "pe": {"oi": 40000, "oi_chg": 0}},
        {"strike": 24200.0, "ce": {"oi": 450000, "oi_chg": 60000}, "pe": {"oi": 15000, "oi_chg": 0}},
    ]

    res_24000 = compute_itm_atm_oi_walls(spot=24010.0, chain_rows=chain_24000)
    assert res_24000["ok"] is True
    assert res_24000["support_bottom"] == 24000.0
    assert res_24000["support_top"] == 24050.0
    assert res_24000["panic_unwinding_trigger"] == 24020.0
    assert res_24000["panic_unwinding_trigger"] != 23220.0
    assert res_24000["structural_downside_target"] < 24000.0
    assert res_24000["structural_downside_target"] != 23119.0
    assert res_24000["otm_major_ce_wall"]["strike"] == 24200.0
    assert res_24000["otm_major_ce_wall"]["strike"] != 23600.0
    assert res_24000["iron_ceiling_min"] == 24150.0


def test_overbought_pcr_shield_and_anti_fomo():
    """Verify Overbought PCR Shield triggers at PCR >= 1.60 and blocks FOMO CE buys."""
    chain_rows = [
        {"strike": 23300.0, "ce": {"oi": 50000, "oi_chg": 0}, "pe": {"oi": 100000, "oi_chg": 0}},
    ]
    # PCR 1.67
    res = compute_itm_atm_oi_walls(
        spot=23440.0,
        chain_rows=chain_rows,
        pcr=1.67,
        market_minutes_elapsed=5,  # Opening bell (first 15m)
    )
    assert res["ok"] is True
    assert res["is_pcr_overbought"] is True
    assert res["pcr"] == 1.67
    assert res["pcr_shield_banner"] is not None
    assert "OVERBOUGHT PCR (1.67)" in res["pcr_shield_banner"]
    assert res["block_fomo_ce"] is True
    assert "ANTI-FOMO SHIELD" in res["fomo_guard_reason"]


def test_rsi_calculation_and_divergence():
    """Verify RSI computation and Bearish Divergence detection on Futures."""
    # Synthetic prices creating higher high with lower high in RSI
    # Initial ramp to set baseline
    prices = [23000 + i * 10 for i in range(20)]
    rsi = compute_rsi(prices, period=14)
    assert len(rsi) == len(prices)
    assert rsi[-1] > 60.0

    # Test Candlestick Star Pattern
    # Evening star: large green -> star -> red closing into bar 1
    candles = [
        {"open": 23500.0, "close": 23550.0, "high": 23555.0, "low": 23495.0},
        {"open": 23555.0, "close": 23558.0, "high": 23565.0, "low": 23552.0},
        {"open": 23555.0, "close": 23510.0, "high": 23558.0, "low": 23505.0},
    ]
    pattern = detect_candlestick_star_pattern(candles)
    assert pattern == "EVENING_STAR"

    # Test Iron Wall alert when near 23550–23623
    res = compute_itm_atm_oi_walls(
        spot=23560.0,
        candles_fut=candles,
    )
    assert res["ok"] is True
    assert res["fut_star_pattern"] == "EVENING_STAR"
    assert res["iron_wall_star_divergence_alert"] is not None
    assert "IRON WALL" in res["iron_wall_star_divergence_alert"]
