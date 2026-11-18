"""Unit tests for proto_sweep_8510 — staged logic, FDR, block bootstrap (no engine)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.proto_sweep_8510 import (  # noqa: E402
    benjamini_hochberg,
    block_bootstrap_total,
    build_candidate_combos,
    merge_overrides,
    reversal_stage_variants,
    stage1_variants,
    trade_key,
)


def _trade(day: str, gross: float, hm: str = "10:00") -> dict:
    return {
        "day": day,
        "entry_hm": hm,
        "side": "CE",
        "entry": 25000.0,
        "gross": gross,
    }


def test_reversal_stage_variants_exposed_for_lab():
    v = reversal_stage_variants()
    assert len(v) >= 6
    ids = {x[0] for x in v}
    assert "reversal_rev_trail_on" in ids
    assert all("reversal" in ov for _, _, ov in v)


def test_stage1_has_factors_not_explosion():
    v = stage1_variants()
    assert 10 <= len(v) <= 40
    ids = {x[0] for x in v}
    assert "oi_total_oi" in ids
    assert "oi_3m_gate" in ids
    assert "candle_trail_15m" in ids


def test_benjamini_hochberg_known():
    # 5 tests, two clearly small p-values
    p = [0.001, 0.002, 0.03, 0.20, 0.50]
    flags = benjamini_hochberg(p, q=0.10)
    assert flags[0] and flags[1]
    assert not flags[4]


def test_block_bootstrap_resamples_days_not_trades():
    trades = [
        _trade("2026-09-01", 10),
        _trade("2026-09-01", -5),
        _trade("2026-09-02", -20),
        _trade("2026-09-02", 8),
    ]
    b = block_bootstrap_total(trades, ["2026-09-01", "2026-09-02"], cost=0.0, n_iter=500, seed=42)
    assert b["n_days"] == 2
    assert b["p5"] is not None and b["p50"] is not None


def test_build_combos_capped():
    winners = [
        ("a", "A", {"oi_filters": {"mode": "block"}}),
        ("b", "B", {"candle_trail": {"enabled": True}}),
        ("c", "C", {"oi_3m_feature": {"enabled": True}}),
    ]
    combos = build_candidate_combos(winners, max_combos=10)
    assert len(combos) <= 10
    assert any(c[0].startswith("combo_") for c in combos)


def test_merge_overrides_oi_switches():
    a = {"oi_filters": {"switches": {"total_oi": True}, "mode": "block"}}
    b = {"oi_filters": {"switches": {"fresh_oi": True}}}
    m = merge_overrides(a, b)
    assert m["oi_filters"]["switches"]["total_oi"] is True
    assert m["oi_filters"]["switches"]["fresh_oi"] is True


def test_trade_key_stable():
    t = _trade("2026-09-01", 5.0)
    assert trade_key(t) == trade_key(t)