"""OI momentum v2 — normalized Significance S = dD/T (17 Nov 2026 spec)."""
from __future__ import annotations

from analyst.chain_reads_8510 import (
    _hm_ts,
    _to_lakhs,
    compute_momentum_oi_reads,
    momentum_direction_from_significance,
    volume_backing_between_snapshots,
    DEFAULT_CFG,
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


def _strike_row(ce_oi: float, pe_oi: float, ce_vol: float, pe_vol: float) -> dict:
    return {"ce_oi": ce_oi, "pe_oi": pe_oi, "ce_vol": ce_vol, "pe_vol": pe_vol}


def _synthetic_volume_history(day: str, n_steps: int = 12, step_min: int = 10) -> tuple:
    """Cumulative vol/OI series with stable high OI-per-vol on prior windows."""
    t_open = _hm_ts(day, "09:15")
    ts_sorted = [t_open + i * step_min * 60 for i in range(n_steps)]
    k = 25000.0
    by_ts = {}
    ce_oi = pe_oi = 10 * 1e5
    ce_vol = pe_vol = 0.0
    for i, t in enumerate(ts_sorted):
        if i > 0:
            ce_oi += 5 * 1e4
            pe_oi += 5 * 1e4
            ce_vol += 1000.0
            pe_vol += 1000.0
        by_ts[t] = {k: _strike_row(ce_oi, pe_oi, ce_vol, pe_vol)}
    return by_ts, ts_sorted


def test_volume_backing_churn_and_backed_labels():
    day = "2026-11-17"
    by_ts, ts_sorted = _synthetic_volume_history(day, n_steps=14)
    k = 25000.0
    t_prev = ts_sorted[-3]
    t_now = ts_sorted[-2]
    # Churn: huge volume tick, tiny OI change on the last window.
    prev = by_ts[t_prev][k]
    by_ts[t_now][k] = _strike_row(
        prev["ce_oi"] + 2e3,
        prev["pe_oi"] + 2e3,
        prev["ce_vol"] + 5e6,
        prev["pe_vol"] + 5e6,
    )
    backed, lbl = volume_backing_between_snapshots(
        by_ts, ts_sorted, t_prev, t_now, spot=25000.0, band=1000.0, cfg=DEFAULT_CFG
    )
    assert backed is False and lbl == "churn"

    by_ts2, ts_sorted2 = _synthetic_volume_history(day, n_steps=14)
    tp = ts_sorted2[-3]
    tn = ts_sorted2[-2]
    p2 = by_ts2[tp][k]
    by_ts2[tn][k] = _strike_row(
        p2["ce_oi"] + 5e4,
        p2["pe_oi"] + 5e4,
        p2["ce_vol"] + 1000.0,
        p2["pe_vol"] + 1000.0,
    )
    backed2, lbl2 = volume_backing_between_snapshots(
        by_ts2, ts_sorted2, tp, tn, spot=25000.0, band=1000.0, cfg=DEFAULT_CFG
    )
    assert backed2 is True and lbl2 == "backed"


def test_volume_confirm_on_churn_downgrades_to_neutral():
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_prev = _hm_ts(day, "10:25")
    t_now = _hm_ts(day, "10:35")
    k = 25000.0
    by_ts, ts_sorted = _synthetic_volume_history(day, n_steps=12)
    by_ts[t_open] = {k: _strike_row(10 * 1e5, 10 * 1e5, 0, 0)}
    by_ts[t_prev] = {k: _strike_row(10 * 1e5, 16 * 1e5, 11000, 11000)}
    # PE momentum vs t_prev: calls up, puts down (with churn volume on last window).
    by_ts[t_now] = {k: _strike_row(20 * 1e5, 5 * 1e5, 5e6 + 11000, 5e6 + 11000)}
    for t in (t_open, t_prev, t_now):
        if t not in ts_sorted:
            ts_sorted.append(t)
    ts_sorted = sorted(ts_sorted)
    out_off = compute_momentum_oi_reads(
        by_ts,
        ts_sorted,
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={"require_volume_confirm": False},
    )
    assert out_off["total_oi"]["direction"] == "PE"
    out_on = compute_momentum_oi_reads(
        by_ts,
        ts_sorted,
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={"require_volume_confirm": True},
    )
    assert out_on["total_oi"]["direction"] == "NEUTRAL"
    assert out_on["total_oi"].get("volume_backing") == "churn"


def test_volume_confirm_on_backed_keeps_direction():
    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_prev = _hm_ts(day, "10:25")
    t_now = _hm_ts(day, "10:35")
    k = 25000.0
    by_ts, ts_sorted = _synthetic_volume_history(day, n_steps=12)
    by_ts[t_open] = {k: _strike_row(10 * 1e5, 10 * 1e5, 0, 0)}
    by_ts[t_prev] = {k: _strike_row(10 * 1e5, 15 * 1e5, 11000, 11000)}
    by_ts[t_now] = {k: _strike_row(5 * 1e5, 5 * 1e5, 12000, 12000)}
    for t in (t_open, t_prev, t_now):
        if t not in ts_sorted:
            ts_sorted.append(t)
    ts_sorted = sorted(ts_sorted)
    out = compute_momentum_oi_reads(
        by_ts,
        ts_sorted,
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={"require_volume_confirm": True},
    )
    assert out["total_oi"]["direction"] == "PE"
    assert out["total_oi"].get("volume_backing") == "backed"


def test_volume_confirm_off_unchanged_direction():
    from analyst.chain_reads_8510 import _hm_ts

    day = "2026-11-17"
    t_open = _hm_ts(day, "09:15")
    t_now = _hm_ts(day, "09:25")
    by_ts = {
        t_open: {25000.0: {"ce_oi": 10 * 1e5, "pe_oi": 10 * 1e5}},
        t_now: {25000.0: {"ce_oi": 15 * 1e5, "pe_oi": 5 * 1e5}},
    }
    base = {
        "momentum_cfg": {"require_volume_confirm": False},
    }
    out_a = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg=base["momentum_cfg"],
    )
    out_b = compute_momentum_oi_reads(
        by_ts,
        sorted(by_ts),
        spot=25000.0,
        band=1000.0,
        day=day,
        now_ts=t_now,
        last_t=t_now,
        age_sec=60,
        momentum_cfg={},
    )
    assert out_a["total_oi"]["direction"] == out_b["total_oi"]["direction"]


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
    for rid in ("total_oi", "fresh_oi"):
        assert out[rid]["direction"] is None
        assert out[rid]["S"] is None