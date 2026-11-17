"""OI momentum (Cr/min) divergence direction — unit tests (17 Nov 2026 spec)."""
from __future__ import annotations

from analyst.chain_reads_8510 import (
    compute_momentum_oi_reads,
    momentum_direction_from_crpm,
)


def test_divergence_ce_puts_up_calls_down():
    assert momentum_direction_from_crpm(-0.10, 0.10, deadzone=0.05) == "CE"
    assert momentum_direction_from_crpm(-0.06, 0.08, deadzone=0.05) == "CE"


def test_convergence_pe_puts_down_calls_up():
    d = momentum_direction_from_crpm(0.12, -0.11, deadzone=0.05)
    assert d == "PE"


def test_both_rising_net_ce():
    assert momentum_direction_from_crpm(0.05, 0.20, deadzone=0.05) == "CE"


def test_deadzone_neutral():
    assert momentum_direction_from_crpm(0.02, 0.02, deadzone=0.05) == "NEUTRAL"
    assert momentum_direction_from_crpm(0.02, -0.02, deadzone=0.05) == "NEUTRAL"


def _chain_at(t: int, ce: float, pe: float):
    return {
        t: {
            25000.0: {"ce_oi": ce, "pe_oi": pe, "ce_vol": 1, "pe_vol": 1, "ce_ltp": 1, "pe_ltp": 1},
        }
    }


def test_insufficient_history_direction_none():
    day = "2026-11-17"
    base_ts = 1731907500
    by_ts = {base_ts: {25000.0: {"ce_oi": 1e8, "pe_oi": 2e8}}}
    ts_sorted = [base_ts]
    out = compute_momentum_oi_reads(
        by_ts,
        ts_sorted,
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=base_ts,
        last_t=base_ts,
        age_sec=60,
        momentum_cfg={"window_min": 10, "deadzone_cr_per_min": 0.05},
    )
    for rid in ("total_oi", "fresh_oi", "writer_flow"):
        assert out[rid]["direction"] is None
        assert out[rid]["calls_crpm"] is None


def test_total_oi_crpm_convergence_pe():
    """Falling puts + rising calls → PE (owner 17 Nov afternoon window pattern)."""
    from analyst.chain_reads_8510 import _hm_ts

    day = "2026-11-17"
    base = _hm_ts(day, "09:15")
    t0 = base + 5 * 60
    t1 = t0 + 32 * 60
    by_ts = {}
    by_ts.update(_chain_at(base, 39e7, 42e7))
    by_ts.update(_chain_at(t0, 40e7, 41.45e7))
    by_ts.update(_chain_at(t1, 41.0e7, 36.0e7))
    ts_sorted = sorted(by_ts)
    out = compute_momentum_oi_reads(
        by_ts,
        ts_sorted,
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t1,
        last_t=t1,
        age_sec=120,
        momentum_cfg={"window_min": 10, "deadzone_cr_per_min": 0.05},
    )
    tot = out["total_oi"]
    assert tot["direction"] == "PE"
    assert tot["puts_crpm"] < 0
    assert tot["calls_crpm"] > 0