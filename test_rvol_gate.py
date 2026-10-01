"""
test_rvol_gate.py — Unit tests for standalone NIFTY RVOL gate module.
"""
from datetime import date, datetime, time
import pytest

import agt_briefing
import rvol_gate


def test_rvol_compute_valid():
    val = rvol_gate.compute_rvol(date(2026, 9, 18), time(10, 0))
    assert val is not None
    assert isinstance(val, float)
    assert 0.1 <= val <= 2.5


def test_rvol_compute_early_hold():
    # Before 09:30 gate decision is HOLD
    dec = rvol_gate.get_gate_decision(date(2026, 9, 18), time(9, 20))
    assert dec["status"] == "HOLD"
    assert dec["regime"] == "HOLD"


def test_rvol_regime_thresholds():
    assert rvol_gate.get_rvol_regime(0.50) == "DEAD_ZONE"
    assert rvol_gate.get_rvol_regime(0.84) == "DEAD_ZONE"
    assert rvol_gate.get_rvol_regime(0.85) == "TRAP_ZONE"
    assert rvol_gate.get_rvol_regime(1.10) == "TRAP_ZONE"
    assert rvol_gate.get_rvol_regime(1.30) == "TREND_ZONE"
    assert rvol_gate.get_rvol_regime(1.80) == "TREND_ZONE"
    assert rvol_gate.get_rvol_regime(2.51) == "EXTREME_ZONE"
    assert rvol_gate.get_rvol_regime(None) == "NA"


def test_rvol_display():
    disp = rvol_gate.get_rvol_display(date(2026, 9, 18), time(10, 0))
    assert disp["regime"] == "DEAD_ZONE"
    assert "DEAD ZONE" in disp["label"]
    assert "📊 RVOL:" in disp["telegram_line"]
    assert disp["suppress_signals"] is True
    assert disp["trend_day"] is False


def test_gate_decision_suppression_and_allow():
    # On 2026-09-18 morning at 10:00 (RVOL 0.35) -> SUPPRESSED
    g_1000 = rvol_gate.get_gate_decision(date(2026, 9, 18), time(10, 0))
    assert g_1000["status"] == "SUPPRESSED"
    assert "DEAD ZONE" in g_1000["reason"]

    # Afternoon on 2026-09-24 at 12:30 (RVOL 1.60) -> ALLOWED (TREND_ZONE)
    g_1230 = rvol_gate.get_gate_decision(date(2026, 9, 24), time(12, 30))
    assert g_1230["status"] == "ALLOWED"
    assert g_1230["regime"] == "TREND_ZONE"


def test_regime_flip_detection():
    flip = rvol_gate.find_regime_flip(date(2026, 9, 24))
    assert flip is not None
    assert flip["date"] == "2026-09-24"
    assert flip["from"] == "CHOP/TRAP"
    assert flip["to"] == "TREND_ZONE"
    assert flip["flip_time"] == "09:45"


def test_checklist_box6_integration():
    rules = rvol_gate.load_rvol_config()
    chk = agt_briefing.compute_pretrade_checklist(
        s1_row={"pe_oi": 100000},
        r1_row={"ce_oi": 100000},
        d_pe_15m=10000,
        d_ce_15m=0,
        iv_percentile=50.0,
        gex_data={"status": "ok", "total_gex_cr": 5.0},
        latest_ts=datetime(2026, 9, 18, 10, 0),
        target_date=date(2026, 9, 18),
        target_time=time(10, 0),
        rules=rules,
        has_history=True,
    )
    # Total boxes must now be 6
    assert len(chk["boxes"]) == 6
    labels = [b[0] for b in chk["boxes"]]
    assert any("RVOL" in l for l in labels)
    
    # At 10:00 on 2026-09-18, RVOL is 0.81 (< 1.30) -> Box 6 fails
    b6 = [b for b in chk["boxes"] if "RVOL" in b[0]][0]
    assert b6[2] is False # b6_pass
    assert "FAIL" in b6[1]
