"""
tests/test_desk_bias.py — Unit tests for Live Prop-Desk Bias Synthesis Engine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from app.engine import desk_bias


def test_desk_bias_call_capitulation():
    cfg = {
        "call_capitulation_net_oi": 5000000,
        "put_capitulation_net_oi": -5000000,
        "theta_chop_net_oi": 2000000,
        "theta_chop_min_decay": 8.0,
        "spike_min_step_oi": 500000,
    }

    # Mock hub returning Call Capitulation
    class MockHub:
        class MockChain:
            def get_pcr_trinity(self, exp=None, fut_src="combined", hub=None):
                return {
                    "fut_close": 22770.0,
                    "pcr_oi": 1.15,
                    "call_oi_chg": -13500000,  # -1.35Cr (Call capitulation)
                    "put_oi_chg": 95000000,   # +9.50Cr (Put floor)
                    "net_oi_chg": 108500000,
                    "selected_expiry": "both",
                    "pcr_oi_series": [{"time": 1793028540, "fut_close": 22770.0, "pcr_oi": 1.15, "call_oi_chg": -13500000, "put_oi_chg": 95000000}],
                }
        chain = MockChain()

    res = desk_bias.compute_live_desk_bias(opt_expiry="both", hub=MockHub(), cfg=cfg)
    assert res["ok"] is True
    assert res["bias_title"] == "🟢 AGGRESSIVE BUY DIPS / CE"
    assert res["bias_theme"] == "bullish"
    assert res["bias_color"] == "#10b981"
    assert "Call writers are capitulating" in res["narrative"]
    assert "CE Wall" in res["target_wall"]
    assert "Tighten SL to VWAP" in res["risk_note"]


def test_desk_bias_put_capitulation():
    cfg = {
        "call_capitulation_net_oi": 5000000,
        "put_capitulation_net_oi": -5000000,
        "theta_chop_net_oi": 2000000,
        "theta_chop_min_decay": 8.0,
        "spike_min_step_oi": 500000,
    }

    class MockHub:
        class MockChain:
            def get_pcr_trinity(self, exp=None, fut_src="combined", hub=None):
                return {
                    "fut_close": 22400.0,
                    "pcr_oi": 0.65,
                    "call_oi_chg": 85000000,   # +8.50Cr (Call resistance)
                    "put_oi_chg": -15000000,  # -1.50Cr (Put capitulation)
                    "net_oi_chg": -100000000,
                    "selected_expiry": "cur",
                    "pcr_oi_series": [{"time": 1793028540, "fut_close": 22400.0, "pcr_oi": 0.65, "call_oi_chg": 85000000, "put_oi_chg": -15000000}],
                }
        chain = MockChain()

    res = desk_bias.compute_live_desk_bias(opt_expiry="cur", hub=MockHub(), cfg=cfg)
    assert res["ok"] is True
    assert res["bias_title"] == "🔴 AGGRESSIVE SELL RALLIES / PE"
    assert res["bias_theme"] == "bearish"
    assert res["bias_color"] == "#ef4444"
    assert "Put writers are capitulating" in res["narrative"]
    assert "PE Wall" in res["target_wall"]


def test_desk_bias_theta_chop(monkeypatch):
    cfg = {
        "call_capitulation_net_oi": 5000000,
        "put_capitulation_net_oi": -5000000,
        "theta_chop_net_oi": 2000000,
        "theta_chop_min_decay": 8.0,
        "spike_min_step_oi": 500000,
    }

    # Mock expiry cockpit telemetry to simulate decay / pin
    from app.engine import expiry_playbook
    monkeypatch.setattr(
        expiry_playbook,
        "compute_expiry_cockpit_telemetry",
        lambda expiry="cur": {
            "straddle": {
                "straddle_decay_pct": 14.2,
                "lower_breakeven": 22610.0,
                "upper_breakeven": 22890.0,
            },
            "pin": {"is_pinned": True, "pin_strike": 22750},
        },
    )

    class MockHub:
        class MockChain:
            def get_pcr_trinity(self, exp=None, fut_src="combined", hub=None):
                return {
                    "fut_close": 22750.0,
                    "pcr_oi": 1.01,
                    "call_oi_chg": 2500000,
                    "put_oi_chg": 3000000,
                    "net_oi_chg": 500000,     # Small net change (< 2M)
                    "selected_expiry": "cur",
                    "pcr_oi_series": [{"time": 1793028540, "fut_close": 22750.0, "pcr_oi": 1.01, "call_oi_chg": 2500000, "put_oi_chg": 3000000}],
                }
        chain = MockChain()

    res = desk_bias.compute_live_desk_bias(opt_expiry="cur", hub=MockHub(), cfg=cfg)
    assert res["ok"] is True
    # If straddle decay >= 8.0% or pinned, fires Theta Chop
    assert res["bias_title"] == "⚖️ THETA CHOP / GAMMA PIN"
    assert res["bias_theme"] == "neutral"
    assert "Breakeven Box" in res["narrative"]


def test_supporting_spike_extractor():
    rows = [
        {"time_label": "14:12", "stage_label": "🔥 Stage 3 Bullish Acceleration (Add CE)", "pe_ce_step_diff": 2270000},
        {"time_label": "13:45", "stage_label": "🟢 Stage 2 Dip Buy (CE)", "pe_ce_step_diff": 450000},
    ]
    spike = desk_bias.extract_supporting_spike(rows)
    assert "Stage 3 Bullish Acceleration" in spike
    assert "14:12" in spike
    assert "+2.3Cr" in spike or "+22.7L" in spike


def test_desk_bias_live_query():
    # End-to-end integration test against live database state
    res = desk_bias.compute_live_desk_bias()
    assert res["ok"] is True
    assert "bias_title" in res
    assert "bias_color" in res
    assert "narrative" in res
    assert "target_wall" in res
    assert "risk_note" in res
    assert res["spot"] > 20000.0
