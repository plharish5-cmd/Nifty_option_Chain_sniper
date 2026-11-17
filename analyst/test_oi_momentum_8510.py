"""OI momentum v2 — normalized Significance S = dD/T (17 Nov 2026 spec)."""
from __future__ import annotations

from analyst.chain_reads_8510 import (
    _hm_ts,
    _to_lakhs,
    compute_momentum_oi_reads,
    momentum_direction_from_significance,
)


def test_owner_figures_0915_neutral():
    """09:15 C=10 P=10 lakh → S=0 → NEUTRAL (no 10m window yet)."""
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    ce = 10 * 1e5
    pe = 10 * 1e5
    by_ts = {t_open: {25000.0: {"ce_oi": ce, "pe_oi": pe}}}
    out = compute_momentum_oi_reads(
        by_ts,
        [t_open],
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_open,
        last_t=t_open,
        age_sec=0,
    )
    assert out["total_oi"]["direction"] is None


def test_owner_figures_0925_pe_extreme():
    """09:25 C=15 P=5 lakh vs 09:15 → S=-0.50 PE EXTREME."""
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_now = _hm_ts(day, "09:25")
    by_ts = {
        t_open: {25000.0: {"ce_oi": 10 * 1e5, "pe_oi": 10 * 1e5}},
        t_now: {25000.0: {"ce_oi": 15 * 1e5, "pe_oi": 5 * 1e5}},
    }
    out = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
    )
    tot = out["total_oi"]
    assert tot["direction"] == "PE"
    assert tot["strength"] == "EXTREME"
    assert abs(float(tot["S"]) - (-0.5)) < 1e-6
    assert abs(float(tot["B"]) - (-0.5)) < 1e-6  # D=-10, T=20


def test_significance_thresholds():
    d, s = momentum_direction_from_significance(0.31, sig_min=0.15, sig_extreme=0.30)
    assert d == "CE" and s == "EXTREME"
    d, s = momentum_direction_from_significance(0.20, sig_min=0.15, sig_extreme=0.30)
    assert d == "CE" and s == "STRONG"
    d, s = momentum_direction_from_significance(0.10, sig_min=0.15, sig_extreme=0.30)
    assert d == "NEUTRAL"
    d, s = momentum_direction_from_significance(-0.25, sig_min=0.15, sig_extreme=0.30)
    assert d == "PE" and s == "STRONG"


def test_b_context_never_flips_direction():
    """Strong positive B (level) with |S| in noise band stays NEUTRAL — no level gate."""
    d, s = momentum_direction_from_significance(0.05, sig_min=0.15, sig_extreme=0.30)
    assert d == "NEUTRAL"


def test_cumd_confirm_blocks_ce_when_cumd_negative():
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_prev = _hm_ts(day, "09:25")
    t_now = _hm_ts(day, "09:35")
    by_ts = {
        t_open: {25000.0: {"ce_oi": 5 * 1e5, "pe_oi": 20 * 1e5}},
        t_prev: {25000.0: {"ce_oi": 14 * 1e5, "pe_oi": 16 * 1e5}},
        t_now: {25000.0: {"ce_oi": 12 * 1e5, "pe_oi": 20 * 1e5}},
    }
    out = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={"window_min": 10, "sig_min": 0.15, "sig_extreme": 0.30, "require_cumd_confirm": True},
    )
    assert out["total_oi"]["direction"] == "NEUTRAL"


def test_cumd_confirm_off_allows_ce():
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_prev = _hm_ts(day, "09:25")
    t_now = _hm_ts(day, "09:35")
    by_ts = {
        t_open: {25000.0: {"ce_oi": 5 * 1e5, "pe_oi": 20 * 1e5}},
        t_prev: {25000.0: {"ce_oi": 14 * 1e5, "pe_oi": 16 * 1e5}},
        t_now: {25000.0: {"ce_oi": 12 * 1e5, "pe_oi": 20 * 1e5}},
    }
    out = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={"require_cumd_confirm": False},
    )
    assert out["total_oi"]["direction"] == "CE"


def test_window_tolerance_rejects_stale_prev():
    """32-min-old snapshot must not stand in for a 10-min window."""
    day = "2026-11-17"
    base = _hm_ts(day, "09:15")
    t_prev = base + 5 * 60
    t_now = t_prev + 32 * 60
    by_ts = {
        base: {25000.0: {"ce_oi": 39e7, "pe_oi": 42e7}},
        t_prev: {25000.0: {"ce_oi": 40e7, "pe_oi": 41.45e7}},
        t_now: {25000.0: {"ce_oi": 41e7, "pe_oi": 36e7}},
    }
    out = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=120,
    )
    assert out["total_oi"]["direction"] is None


def test_convergence_pe_within_window():
    """Falling puts + rising calls over 10m → negative S → PE."""
    day = "2026-11-17"
    base = _hm_ts(day, "09:15")
    t_prev = base + 75 * 60  # 10:30
    t_now = t_prev + 10 * 60  # 10:40
    by_ts = {
        base: {25000.0: {"ce_oi": 39e7, "pe_oi": 42e7}},
        t_prev: {25000.0: {"ce_oi": 38e7, "pe_oi": 44e7}},
        t_now: {25000.0: {"ce_oi": 43e7, "pe_oi": 34e7}},
    }
    out = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=120,
    )
    tot = out["total_oi"]
    assert tot["direction"] == "PE"
    assert float(tot["S"]) < -0.15


def test_to_lakhs():
    assert _to_lakhs(20e7) == 2000.0
    assert _to_lakhs(2e7) == 200.0


def test_insufficient_history_direction_none():
    day = "2026-11-17"
    base_ts = 1731907500
    by_ts = {base_ts: {25000.0: {"ce_oi": 1e8, "pe_oi": 2e8}}}
    out = compute_momentum_oi_reads(
        by_ts,
        [base_ts],
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=base_ts,
        last_t=base_ts,
        age_sec=60,
    )
    for rid in ("total_oi", "fresh_oi", "writer_flow"):
        assert out[rid]["direction"] is None
        assert out[rid]["S"] is None