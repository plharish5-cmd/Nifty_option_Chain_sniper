"""oi_3m separate feature — gate + context panel (17 Nov 2026)."""
from __future__ import annotations

from analyst.chain_reads_8510 import momentum_direction_from_significance
from analyst.oi_3m_gate_8510 import (
    compute_oi_3m_read,
    load_oi_3m_feature_cfg,
    oi_3m_gate_decision,
)
from analyst.oi_context_panel_8510 import build_context_panel


def _feat(**kw):
    base = load_oi_3m_feature_cfg({})
    gate = kw.pop("gate", None)
    if isinstance(gate, dict):
        g = dict(base["gate"])
        g.update(gate)
        base["gate"] = g
    base.update(kw)
    return base


def test_thresholds_015_030():
    d, s = momentum_direction_from_significance(0.16, sig_min=0.15, sig_extreme=0.30)
    assert d == "CE" and s == "STRONG"
    d, s = momentum_direction_from_significance(-0.31, sig_min=0.15, sig_extreme=0.30)
    assert d == "PE" and s == "EXTREME"
    d, _ = momentum_direction_from_significance(0.10, sig_min=0.15, sig_extreme=0.30)
    assert d == "NEUTRAL"


def test_feature_off_never_blocks():
    blocked, det = oi_3m_gate_decision(True, _feat(enabled=False), {"direction": "PE"})
    assert blocked is False and det["blocks_trade"] is False


def test_gate_off_never_blocks():
    cfg = _feat(enabled=True, gate={"enabled": False, "mode": "block"})
    blocked, _ = oi_3m_gate_decision(True, cfg, {"direction": "PE"})
    assert blocked is False


def test_log_only_never_blocks_trade():
    cfg = _feat(
        enabled=True,
        gate={"enabled": True, "mode": "log_only", "window_min": 3, "sig_min": 0.15, "sig_extreme": 0.30},
    )
    blocked, det = oi_3m_gate_decision(True, cfg, {"direction": "PE", "S": -0.2, "B": 0.42})
    assert blocked is False and det["would_block"] is True


def test_block_mode_opposing():
    cfg = _feat(
        enabled=True,
        gate={"enabled": True, "mode": "block", "window_min": 3, "sig_min": 0.15, "sig_extreme": 0.30},
    )
    blocked, _ = oi_3m_gate_decision(True, cfg, {"direction": "PE"})
    assert blocked is True
    blocked2, _ = oi_3m_gate_decision(False, cfg, {"direction": "CE"})
    assert blocked2 is True


def test_agreeing_never_blocks():
    cfg = _feat(enabled=True, gate={"enabled": True, "mode": "block"})
    assert oi_3m_gate_decision(True, cfg, {"direction": "CE"})[0] is False
    assert oi_3m_gate_decision(False, cfg, {"direction": "PE"})[0] is False


def test_b_display_only_not_gating():
    cfg = _feat(enabled=True, gate={"enabled": True, "mode": "block"})
    blocked, _ = oi_3m_gate_decision(
        True, cfg, {"direction": "PE", "S": -0.20, "B": 0.42}
    )
    assert blocked is True


def test_none_direction_fail_open():
    cfg = _feat(enabled=True, gate={"enabled": True, "mode": "block"})
    assert oi_3m_gate_decision(True, cfg, {"direction": None})[0] is False


def test_insufficient_history_none():
    from analyst.chain_reads_8510 import _hm_ts

    day = "2026-11-17"
    t0 = _hm_ts(day, "09:20")
    by_ts_list = [
        {
            "ts": t0,
            "strike": 25000.0,
            "ce_oi": 10e5,
            "pe_oi": 10e5,
            "ce_vol": 1,
            "pe_vol": 1,
            "ce_ltp": 1,
            "pe_ltp": 1,
        }
    ]
    out = compute_oi_3m_read(
        by_ts_list,
        25000.0,
        day=day,
        now_ts=t0,
        gate_cfg={"window_min": 3},
    )
    assert out["direction"] is None


def test_context_panel_no_gate_fields():
    board = {"sources": [{"id": "pcr_zone", "direction": "CE", "detail": "PCR 1.1"}]}
    rows = build_context_panel(board=board, show=["pcr_zone"])
    assert rows[0]["direction"] == "CE"
    _, det = oi_3m_gate_decision(True, _feat(enabled=True), None)
    assert "context" not in det