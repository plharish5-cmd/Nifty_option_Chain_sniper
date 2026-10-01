"""
test_writer_dominance.py — Tests for Writer Dominance Ratio, Structure Classifier, Asymmetry & Risk Staging.
Validates:
1. Loading configuration & bands from strategy1_rules.json (Rule 4: Zero Hardcoding).
2. Exact calculation matching the user prompt's worked example (spot 23,171).
3. Scale bands: 1.0-1.3 Balanced, 1.3-2.0 Mild, 2.0-3.0 Clear, 3.0+ Heavy.
4. Position detection: Strikes above spot (CE ceiling), below spot (PE floor), ATM (both).
5. Four Structure states: RANGE, COMPRESSION, ABANDONMENT, TRENDING.
6. Asymmetry calculation & min_gap gating (lid firm vs floor firm vs symmetric).
7. Divide-by-zero guards and sign handling.
8. Pre-market suppression in observation logs.
9. Live briefing integration under INSTITUTIONAL READ and Telegram briefing lines.
"""

import pandas as pd
import pytest
from datetime import datetime, date, time as dtime

import agt_briefing


def test_writer_dominance_rules_loaded():
    rules = agt_briefing.load_briefing_rules()
    dom_cfg = rules.get("writer_dominance", {})
    assert dom_cfg.get("enabled") is True
    assert dom_cfg.get("atm_steps") == 2
    assert dom_cfg.get("dominance_strike_span") == 2

    bands = dom_cfg.get("bands", {})
    assert bands.get("balanced_max") == 1.3
    assert bands.get("mild_max") == 2.0
    assert bands.get("clear_max") == 3.0

    labels = dom_cfg.get("labels", {})
    assert labels.get("balanced") == "BALANCED"
    assert labels.get("mild") == "MILD"
    assert labels.get("clear") == "CLEAR"
    assert labels.get("heavy") == "HEAVY"

    struct_cfg = dom_cfg.get("structure", {})
    assert struct_cfg.get("range_min_width_pts") == 100
    assert struct_cfg.get("compression_max_width_pts") == 50
    assert struct_cfg.get("trending_threshold") == 2.0

    asym_cfg = dom_cfg.get("asymmetry", {})
    assert asym_cfg.get("min_gap") == 2.0
    assert asym_cfg.get("conviction_bands", {}).get("weak_max") == 1.5
    assert asym_cfg.get("conviction_bands", {}).get("firm_max") == 3.0

    risk_cfg = dom_cfg.get("risk_staging", {})
    assert risk_cfg.get("cascade_delta_threshold") == -25000
    assert risk_cfg.get("proximity_pts") == 25.0


def test_writer_dominance_exact_user_example():
    """
    Validates against the exact numbers from the user prompt:
    Spot: 23,171
    23,250: +79 above, CE +159.2k, PE +20.3k -> 7.8x, Calls heavy
    23,200: +29 above, CE +176.8k, PE +60.8k -> 2.9x, Calls clear
    23,150: ATM,       CE +33.6k,  PE +28.4k -> 1.2x, Balanced
    23,100: -71 below, CE +29.4k,  PE +17.0k -> 1.7x, Calls mild
    23,050: -121 below,CE +5.5k,   PE +7.1k  -> 1.3x, Balanced
    Aggregate: CE +399k vs PE +126k = 3.2x call-writer dominance (HEAVY).
    Structure: RANGE 23,050–23,250 (edges defended both sides).
    Asymmetry: Floor conviction 1.3x WEAK | Ceiling 7.8x STRONG -> lid firm, floor thin.
    """
    rules = agt_briefing.load_briefing_rules()
    spot = 23171.0

    df = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 159200.0, "pe_chg_oi": 20300.0, "ce_oi": 159200.0, "pe_oi": 20300.0},
        {"strike": 23200.0, "ce_chg_oi": 176800.0, "pe_chg_oi": 60800.0, "ce_oi": 176800.0, "pe_oi": 60800.0},
        {"strike": 23150.0, "ce_chg_oi": 33600.0, "pe_chg_oi": 28400.0, "ce_oi": 33600.0, "pe_oi": 28400.0},
        {"strike": 23100.0, "ce_chg_oi": 29400.0, "pe_chg_oi": 17000.0, "ce_oi": 29400.0, "pe_oi": 17000.0},
        {"strike": 23050.0, "ce_chg_oi": 5500.0, "pe_chg_oi": 7100.0, "ce_oi": 5500.0, "pe_oi": 7100.0},
    ])

    res = agt_briefing.compute_writer_dominance(df, spot, rules, atm_override=23150.0)

    assert res["enabled"] is True
    assert res["agg_side"] == "CALLS"
    assert round(res["agg_ratio"], 1) in (3.0, 3.2)
    assert res["intensity_label"] == "HEAVY"

    # Check individual strikes
    strikes_by_s = {r["strike"]: r for r in res["analyzed_strikes"]}

    # 23,250
    s23250 = strikes_by_s[23250.0]
    assert round(s23250["ratio"], 1) == 7.8
    assert s23250["verdict"] == "Calls heavy"
    assert "+79 above" in s23250["pos_label"]

    # 23,200
    s23200 = strikes_by_s[23200.0]
    assert round(s23200["ratio"], 1) == 2.9
    assert s23200["verdict"] == "Calls clear"
    assert "+29 above" in s23200["pos_label"]

    # 23,150
    s23150 = strikes_by_s[23150.0]
    assert round(s23150["ratio"], 1) == 1.2
    assert s23150["verdict"] == "Balanced"
    assert s23150["pos_label"] == "ATM"

    # 23,100
    s23100 = strikes_by_s[23100.0]
    assert round(s23100["ratio"], 1) == 1.7
    assert s23100["verdict"] == "Calls mild"
    assert "71 below" in s23100["pos_label"]

    # 23,050
    s23050 = strikes_by_s[23050.0]
    assert round(s23050["ratio"], 1) == 1.3
    assert s23050["verdict"] == "Balanced"

    # Structure & Asymmetry checks
    assert res["structure_state"] == "RANGE"
    assert res["floor_strike"] == 23050.0
    assert res["ceil_strike"] == 23250.0
    asym = res["asymmetry"]
    assert asym["is_asymmetric"] is True
    assert round(asym["floor_conviction"], 1) == 1.3
    assert asym["floor_band"] == "WEAK"
    assert round(asym["ceil_conviction"], 1) == 7.8
    assert asym["ceil_band"] == "STRONG"

    # Terminal lines verification
    lines = res["terminal_lines"]
    assert any("DOMINANCE: CALLS " in ln and "HEAVY" in ln for ln in lines)
    assert any("Heaviest: 23,250 CE 7.8× | 23,200 CE 2.9×" in ln for ln in lines)
    assert any("STRUCTURE: RANGE 23,050–23,250 (edges defended both sides)" in ln for ln in lines)
    assert any("Floor conviction 1.3× WEAK | Ceiling 7.8× STRONG" in ln for ln in lines)
    assert any("Asymmetric: lid firm, floor thin" in ln for ln in lines)
    assert any("Risk to call writers: close above 23,250 = squeeze" in ln for ln in lines)


def test_four_structure_states():
    """Validates the synthetic chains for all 4 structure states: RANGE, COMPRESSION, ABANDONMENT, TRENDING."""
    rules = agt_briefing.load_briefing_rules()
    spot = 23150.0

    # 1. RANGE FORMING: CE additions above spot (23,250), PE additions below spot (23,050), gap >= 100
    df_range = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 80000.0, "pe_chg_oi": 10000.0, "ce_oi": 80000.0, "pe_oi": 10000.0},
        {"strike": 23200.0, "ce_chg_oi": 40000.0, "pe_chg_oi": 10000.0, "ce_oi": 40000.0, "pe_oi": 10000.0},
        {"strike": 23150.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 20000.0, "ce_oi": 20000.0, "pe_oi": 20000.0},
        {"strike": 23100.0, "ce_chg_oi": 10000.0, "pe_chg_oi": 40000.0, "ce_oi": 10000.0, "pe_oi": 40000.0},
        {"strike": 23050.0, "ce_chg_oi": 10000.0, "pe_chg_oi": 70000.0, "ce_oi": 10000.0, "pe_oi": 70000.0},
    ])
    res_range = agt_briefing.compute_writer_dominance(df_range, spot, rules, atm_override=23150.0)
    assert res_range["structure_state"] == "RANGE"
    assert "RANGE" in res_range["structure_line"]

    # 2. COMPRESSION: Both sides positive at same strike / gap <= 50 pts
    df_comp = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": -5000.0, "pe_chg_oi": -5000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23200.0, "ce_chg_oi": -2000.0, "pe_chg_oi": -2000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23150.0, "ce_chg_oi": 60000.0, "pe_chg_oi": 55000.0, "ce_oi": 90000.0, "pe_oi": 85000.0},
        {"strike": 23100.0, "ce_chg_oi": -2000.0, "pe_chg_oi": -2000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23050.0, "ce_chg_oi": -5000.0, "pe_chg_oi": -5000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
    ])
    res_comp = agt_briefing.compute_writer_dominance(df_comp, spot, rules, atm_override=23150.0)
    assert res_comp["structure_state"] == "COMPRESSION"
    assert "COMPRESSION" in res_comp["structure_line"]

    # 3. ABANDONMENT: All additions negative across span
    df_aband = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": -25000.0, "pe_chg_oi": -10000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23200.0, "ce_chg_oi": -30000.0, "pe_chg_oi": -20000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23150.0, "ce_chg_oi": -15000.0, "pe_chg_oi": -15000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23100.0, "ce_chg_oi": -10000.0, "pe_chg_oi": -35000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
        {"strike": 23050.0, "ce_chg_oi": -5000.0, "pe_chg_oi": -25000.0, "ce_oi": 50000.0, "pe_oi": 50000.0},
    ])
    res_aband = agt_briefing.compute_writer_dominance(df_aband, spot, rules, atm_override=23150.0)
    assert res_aband["structure_state"] == "ABANDONMENT"
    assert "ABANDONMENT" in res_aband["structure_line"]

    # 4. TRENDING: One side dominant >= 2.0x, opposite edge not defended (negative/0 ΔOI)
    df_trend = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 150000.0, "pe_chg_oi": -10000.0, "ce_oi": 150000.0, "pe_oi": 50000.0},
        {"strike": 23200.0, "ce_chg_oi": 120000.0, "pe_chg_oi": -20000.0, "ce_oi": 120000.0, "pe_oi": 50000.0},
        {"strike": 23150.0, "ce_chg_oi": 30000.0, "pe_chg_oi": -5000.0, "ce_oi": 30000.0, "pe_oi": 50000.0},
        {"strike": 23100.0, "ce_chg_oi": 10000.0, "pe_chg_oi": -15000.0, "ce_oi": 10000.0, "pe_oi": 50000.0},
        {"strike": 23050.0, "ce_chg_oi": 0.0, "pe_chg_oi": -20000.0, "ce_oi": 0.0, "pe_oi": 50000.0},
    ])
    res_trend = agt_briefing.compute_writer_dominance(df_trend, spot, rules, atm_override=23150.0)
    assert res_trend["structure_state"] == "TRENDING"
    assert "TRENDING" in res_trend["structure_line"]


def test_asymmetry_gating():
    """Validates that asymmetry narrative only fires when edges differ by >= min_gap (2.0x)."""
    rules = agt_briefing.load_briefing_rules()
    spot = 23171.0

    # Symmetric range: Floor ratio 2.0x FIRM vs Ceiling ratio 2.2x FIRM (gap 1.1x < 2.0x)
    df_sym = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 44000.0, "pe_chg_oi": 20000.0, "ce_oi": 44000.0, "pe_oi": 20000.0},  # 2.2x
        {"strike": 23200.0, "ce_chg_oi": 30000.0, "pe_chg_oi": 20000.0, "ce_oi": 30000.0, "pe_oi": 20000.0},
        {"strike": 23150.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 20000.0, "ce_oi": 20000.0, "pe_oi": 20000.0},
        {"strike": 23100.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 30000.0, "ce_oi": 20000.0, "pe_oi": 30000.0},
        {"strike": 23050.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 40000.0, "ce_oi": 20000.0, "pe_oi": 40000.0},  # 2.0x
    ])
    res_sym = agt_briefing.compute_writer_dominance(df_sym, spot, rules, atm_override=23150.0)
    assert res_sym["structure_state"] == "RANGE"
    assert res_sym["asymmetry"]["is_asymmetric"] is False
    assert any("Symmetric range: balanced defense both sides" in ln for ln in res_sym["terminal_lines"])

    # Asymmetric: Floor ratio 5.0x STRONG vs Ceiling ratio 1.2x WEAK (gap 4.16x >= 2.0x)
    df_asym = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 24000.0, "pe_chg_oi": 20000.0, "ce_oi": 24000.0, "pe_oi": 20000.0},  # 1.2x
        {"strike": 23200.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 20000.0, "ce_oi": 20000.0, "pe_oi": 20000.0},
        {"strike": 23150.0, "ce_chg_oi": 20000.0, "pe_chg_oi": 20000.0, "ce_oi": 20000.0, "pe_oi": 20000.0},
        {"strike": 23100.0, "ce_chg_oi": 10000.0, "pe_chg_oi": 30000.0, "ce_oi": 10000.0, "pe_oi": 30000.0},
        {"strike": 23050.0, "ce_chg_oi": 10000.0, "pe_chg_oi": 50000.0, "ce_oi": 10000.0, "pe_oi": 50000.0},  # 5.0x
    ])
    res_asym = agt_briefing.compute_writer_dominance(df_asym, spot, rules, atm_override=23150.0)
    assert res_asym["structure_state"] == "RANGE"
    assert res_asym["asymmetry"]["is_asymmetric"] is True
    assert any("Asymmetric: floor firm, lid thin" in ln for ln in res_asym["terminal_lines"])


def test_divide_by_zero_guards():
    """Ensures divide by zero guards work gracefully without errors."""
    rules = agt_briefing.load_briefing_rules()
    spot = 23171.0

    # 1. Zero additions everywhere
    df_zeros = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23200.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23150.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23100.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23050.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
    ])
    res0 = agt_briefing.compute_writer_dominance(df_zeros, spot, rules, atm_override=23150.0)
    assert res0["agg_ratio"] == 1.0

    # 2. Only calls adding, PE additions strictly 0
    df_calls_only = pd.DataFrame([
        {"strike": 23250.0, "ce_chg_oi": 100000.0, "pe_chg_oi": 0.0},
        {"strike": 23200.0, "ce_chg_oi": 50000.0, "pe_chg_oi": 0.0},
        {"strike": 23150.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23100.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
        {"strike": 23050.0, "ce_chg_oi": 0.0, "pe_chg_oi": 0.0},
    ])
    res_c = agt_briefing.compute_writer_dominance(df_calls_only, spot, rules, atm_override=23150.0)
    assert res_c["agg_side"] == "CALLS"
    assert res_c["agg_ratio"] == 99.9


def test_pre_market_suppression():
    """Validates that format_observation_entry suppresses Dominance and Structure pre-market (< 09:15)."""
    rules = agt_briefing.load_briefing_rules()
    dom_data = {
        "enabled": True,
        "obs_dominance": "CALLS 3.2× HEAVY (CE +399k vs PE +126k)",
        "obs_structure": "RANGE 23,050–23,250 (edges defended both sides)",
    }

    # Pre-market timestamp (09:10)
    pre_ts = datetime(2026, 9, 24, 9, 10)
    entry_pre = agt_briefing.format_observation_entry(
        target_date=pre_ts.date(),
        latest_ts=pre_ts,
        latest_spot=23171.0,
        bias_word="NEUTRAL →",
        gex_data=None,
        iv_data=None,
        s1_row=None,
        r1_row=None,
        mult=65.0 / 100000.0,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data=None,
        action_line="NO TRADE",
        rules=rules,
        dom_data=dom_data,
    )
    assert "- Dominance:" not in entry_pre
    assert "- Structure:" not in entry_pre

    # Market-hours timestamp (12:54)
    mkt_ts = datetime(2026, 9, 24, 12, 54)
    entry_mkt = agt_briefing.format_observation_entry(
        target_date=mkt_ts.date(),
        latest_ts=mkt_ts,
        latest_spot=23171.0,
        bias_word="NEUTRAL →",
        gex_data=None,
        iv_data=None,
        s1_row=None,
        r1_row=None,
        mult=65.0 / 100000.0,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data=None,
        action_line="NO TRADE",
        rules=rules,
        dom_data=dom_data,
    )
    assert "- Dominance: CALLS 3.2× HEAVY (CE +399k vs PE +126k)" in entry_mkt
    assert "- Structure: RANGE 23,050–23,250 (edges defended both sides)" in entry_mkt


def test_writer_dominance_briefing_integration():
    """Validates that build_briefing outputs writer dominance and structure lines and populates attributes."""
    today = date(2026, 9, 24)
    briefing = agt_briefing.build_briefing(today)
    assert briefing != "Market closed or no data yet"

    # Check terminal lines in briefing
    assert "⚖️ DOMINANCE:" in briefing
    assert "Heaviest:" in briefing
    assert "🔒 STRUCTURE:" in briefing

    # Check attribute
    dom = getattr(agt_briefing.build_briefing, "writer_dominance", None)
    assert dom is not None
    assert dom.get("enabled") is True
    assert dom.get("agg_side") in ("CALLS", "PUTS", "BALANCED")
    assert dom.get("terminal_lines") is not None
    assert len(dom.get("terminal_lines")) >= 3

    # Check telegram briefing
    tg = getattr(agt_briefing.build_briefing, "telegram_briefing", None)
    assert tg is not None
    assert "⚖️ Dominance:" in tg
    assert "🔒 Structure:" in tg
