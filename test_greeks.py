"""
test_greeks.py — Comprehensive Unit Tests for Greeks, Inversion, and Repricing Consistency.
"""

from __future__ import annotations

from datetime import datetime, date
import math
import numpy as np
import pandas as pd
import pytest

import greeks
from greeks import (
    black_scholes,
    black_scholes_price,
    implied_volatility_bisection,
    time_to_expiry,
    add_greeks,
    _norm_cdf,
    CALENDAR_DAYS,
)


def test_calendar_365_day_count():
    """Verifies continuous calendar/365 day count convention."""
    now = datetime(2026, 9, 11, 15, 0, 0)
    exp = datetime(2026, 9, 15, 15, 30, 0)
    years = time_to_expiry(exp, now)
    
    # 4 days + 30 min = 4.020833 days / 365
    expected_years = (4.0 + 30.0 / 1440.0) / 365.0
    assert abs(years - expected_years) < 1e-6
    assert abs(years * CALENDAR_DAYS - 4.020833) < 1e-4


def test_iv_inversion_and_repricing():
    """Verifies bisection solves IV from LTP and reprices within 0.05 tolerance."""
    spot = 23398.1
    strike = 23300.0
    ltp = 198.25
    years = 4.020833 / 365.0

    # Solve with r=0.0
    iv_solved, note = implied_volatility_bisection(spot, strike, ltp, years, is_call=True, rate=0.0)
    assert iv_solved is not None
    assert "converged" in note

    # Reprice check
    reprice = black_scholes_price(spot, strike, iv_solved, years, is_call=True, rate=0.0)
    reprice_err = abs(reprice - ltp)
    assert reprice_err < 0.01, f"Repricing error {reprice_err} exceeds 0.01"


def test_delta_equals_norm_cdf_d1():
    """Asserts that delta strictly equals N(d1) for call and N(d1)-1 for put."""
    spot = 23398.1
    strike = 23300.0
    years = 4.020833 / 365.0

    g_call = black_scholes(spot, strike, 14.72, years, is_call=True, rate=0.0)
    assert g_call.delta is not None

    sigma = 14.72 / 100.0
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (0.5 * sigma**2) * years) / (sigma * sqrt_t)
    expected_call_delta = _norm_cdf(d1)
    assert abs(g_call.delta - expected_call_delta) < 1e-9

    g_put = black_scholes(spot, strike, 14.72, years, is_call=False, rate=0.0)
    expected_put_delta = _norm_cdf(d1) - 1.0
    assert abs(g_put.delta - expected_put_delta) < 1e-9


def test_futures_underlying_source():
    """Verifies that add_greeks prefers near-month futures with r=0.0 when supplied."""
    df = pd.DataFrame([{
        "strike": 23300.0,
        "ce_oi": 1000,
        "pe_oi": 1000,
        "ce_ltp": 198.25,
        "pe_ltp": 50.0,
        "ce_iv": 0.0,
        "pe_iv": 14.0,
    }])
    exp = datetime(2026, 9, 15, 15, 30)
    now = datetime(2026, 9, 11, 15, 0)

    # 1. With futures (strike 23300 < spot 23398.1 -> PE is preferred OTM leg)
    df_fut = add_greeks(df, spot=23398.1, expiry=exp, now=now, futures=23466.1)
    assert df_fut["underlying_source"].iloc[0] == "futures"
    assert df_fut["pe_iv_source"].iloc[0] == "nse"
    assert df_fut["pe_iv_leg_source"].iloc[0] == "own"
    assert df_fut["ce_iv_source"].iloc[0] == "parity"
    assert df_fut["ce_iv_leg_source"].iloc[0] == "parity"

    # 2. Without futures (fallback to spot)
    df_spot = add_greeks(df, spot=23398.1, expiry=exp, now=now, futures=None)
    assert df_spot["underlying_source"].iloc[0] == "spot"


def test_single_iv_put_call_parity_and_convexity():
    """Verifies that single-IV eliminates convexity mismatch and satisfies Delta_CE - Delta_PE = e^(-qT)."""
    df = pd.DataFrame([
        {"strike": 23300.0, "ce_oi": 1000, "pe_oi": 2000, "ce_ltp": 198.25, "pe_ltp": 45.0, "ce_iv": 14.33, "pe_iv": 9.62},
        {"strike": 23500.0, "ce_oi": 2500, "pe_oi": 800, "ce_ltp": 62.0, "pe_ltp": 150.0, "ce_iv": 11.19, "pe_iv": 6.95},
    ])
    exp = datetime(2026, 9, 15, 15, 30)
    now = datetime(2026, 9, 11, 15, 0)
    spot = 23398.1

    res = add_greeks(df, spot=spot, expiry=exp, now=now)
    years = time_to_expiry(exp, now)
    df_q = math.exp(-greeks.DIVIDEND_YIELD * years)

    # 23300 (K < Spot): PE is OTM preferred leg
    r_23300 = res[res.strike == 23300.0].iloc[0]
    assert r_23300["pe_iv_leg_source"] == "own"
    assert r_23300["ce_iv_leg_source"] == "parity"
    assert r_23300["iv_used"] == 9.62
    assert abs(r_23300["ce_gamma"] - r_23300["pe_gamma"]) < 1e-12
    assert abs(r_23300["ce_delta"] - r_23300["pe_delta"] - df_q) < 1e-12
    assert r_23300["parity_residual"] < 1e-12
    assert r_23300["gamma_coverage"] == 1.0

    # 23500 (K >= Spot): CE is OTM preferred leg
    r_23500 = res[res.strike == 23500.0].iloc[0]
    assert r_23500["ce_iv_leg_source"] == "own"
    assert r_23500["pe_iv_leg_source"] == "parity"
    assert r_23500["iv_used"] == 11.19
    assert abs(r_23500["ce_gamma"] - r_23500["pe_gamma"]) < 1e-12
    assert abs(r_23500["ce_delta"] - r_23500["pe_delta"] - df_q) < 1e-12
    assert r_23500["parity_residual"] < 1e-12
    assert r_23500["gamma_coverage"] == 1.0


def test_expiry_unknown_returns_nan():
    """Verifies that when expiry is None, add_greeks returns NaN with audit note."""
    df = pd.DataFrame([{
        "strike": 23300.0,
        "ce_oi": 1000,
        "pe_oi": 1000,
        "ce_ltp": 198.25,
        "pe_ltp": 50.0,
        "ce_iv": 15.0,
        "pe_iv": 15.0,
    }])
    res = add_greeks(df, spot=23398.1, expiry=None)
    assert np.isnan(res["ce_delta"].iloc[0])
    assert np.isnan(res["pe_delta"].iloc[0])
    assert res["ce_iv_source"].iloc[0] == "unavailable"
    assert res.attrs.get("greeks_note") == "expiry unknown"


def test_total_gamma_requires_both_legs():
    """Verifies that total_gamma requires both legs and tags gamma_coverage."""
    # Both legs valid
    df_both = pd.DataFrame([{
        "strike": 23300.0, "ce_oi": 100, "pe_oi": 100,
        "ce_ltp": 198.25, "pe_ltp": 50.0, "ce_iv": 15.0, "pe_iv": 15.0,
    }])
    res_both = add_greeks(df_both, spot=23398.1, expiry=datetime(2026, 9, 15, 15, 30), now=datetime(2026, 9, 11, 15, 0))
    assert res_both["gamma_coverage"].iloc[0] == 1.0
    assert not np.isnan(res_both["total_gamma"].iloc[0])


def test_duplicate_strike_live_graceful_handling():
    """Verifies that live consumers drop duplicate strikes, tag duplicate_strikes_dropped, and never raise."""
    import analytics
    import chain_metrics
    import institutional
    import strike_select

    df_dup = pd.DataFrame([
        {"strike": 23300.0, "expiry": "15-Sep-2026", "ce_oi": 100, "pe_oi": 100, "ce_chg_oi": 10, "pe_chg_oi": 10, "ce_ltp": 100.0, "pe_ltp": 50.0, "ce_iv": 15.0, "pe_iv": 15.0, "ce_gamma": 0.001, "pe_gamma": 0.001},
        {"strike": 23300.0, "expiry": "22-Sep-2026", "ce_oi": 200, "pe_oi": 200, "ce_chg_oi": 20, "pe_chg_oi": 20, "ce_ltp": 120.0, "pe_ltp": 70.0, "ce_iv": 16.0, "pe_iv": 16.0, "ce_gamma": 0.001, "pe_gamma": 0.001},
    ])
    spot = 23300.0

    # 1. analytics.compute_levels drops duplicates and continues
    lvl = analytics.compute_levels(df_dup, spot)
    assert lvl is not None

    # 2. analytics.analyse drops duplicates and continues
    read = analytics.analyse(df_dup, spot)
    assert read is not None

    # 3. chain_metrics.weight_balance drops duplicates and continues
    wt = chain_metrics.weight_balance(df_dup, int(spot))

    # 4. institutional.net_gex drops duplicates and continues
    gex = institutional.net_gex(df_dup, spot)
    assert isinstance(gex, dict)

    # 5. strike_select.evaluate_strike_candidates drops duplicates and continues
    cands = strike_select.evaluate_strike_candidates(df_dup, spot)
    assert isinstance(cands, list)

    # 6. strike_select.analyze_wall_intent drops duplicates and continues
    intent = strike_select.analyze_wall_intent(df_dup, spot)
    assert intent is not None

    # 7. Helper tags duplicate_strikes_dropped = True and keeps nearest expiry
    cleaned = analytics.deduplicate_strikes_live(df_dup, "test")
    assert len(cleaned) == 1
    assert cleaned.attrs.get("duplicate_strikes_dropped") is True
    assert cleaned["expiry"].iloc[0] == "15-Sep-2026"


def test_load_day_multi_duplicate_strikes_hard_raise(tmp_path, monkeypatch):
    """Verifies that load_day_multi hard raises ValueError if a snapshot has duplicate strikes for same expiry."""
    import store
    import json
    monkeypatch.setattr(store.config, "SNAPSHOT_MULTI_DIR", tmp_path)

    day_dir = tmp_path / "2026-09-15"
    day_dir.mkdir()

    # Write invalid parquet with duplicate strike for same expiry
    corrupt_df = pd.DataFrame([
        {"strike": 23300.0, "expiry": "15-Sep-2026", "ce_oi": 100},
        {"strike": 23300.0, "expiry": "15-Sep-2026", "ce_oi": 200},
    ])
    corrupt_df.to_parquet(day_dir / "091500.parquet", index=False)
    (day_dir / "091500.meta.json").write_text(json.dumps({"spot": 23300.0, "timestamp": "2026-09-15T09:15:00"}))

    with pytest.raises(ValueError, match="duplicate strikes found within the same expiry"):
        store.load_day_multi(date(2026, 9, 15))


def test_multi_expiry_legitimate_overlapping_strikes_persist_and_load(tmp_path, monkeypatch):
    """Legitimate 3-expiry frame with identical strikes across different expiries must persist and load without error."""
    import store
    multi_dir = tmp_path / "snapshots_multi"
    multi_dir.mkdir()
    monkeypatch.setattr(store.config, "SNAPSHOT_MULTI_DIR", multi_dir)
    monkeypatch.setattr(store.config, "ALLOW_WEEKEND_STORAGE", True)

    df_multi = pd.DataFrame([
        {"strike": 23300.0, "expiry": "15-Sep-2026", "ce_oi": 100, "pe_oi": 500},
        {"strike": 23350.0, "expiry": "15-Sep-2026", "ce_oi": 150, "pe_oi": 400},
        {"strike": 23300.0, "expiry": "22-Sep-2026", "ce_oi": 80, "pe_oi": 300},
        {"strike": 23350.0, "expiry": "22-Sep-2026", "ce_oi": 90, "pe_oi": 250},
        {"strike": 23300.0, "expiry": "29-Sep-2026", "ce_oi": 50, "pe_oi": 150},
        {"strike": 23350.0, "expiry": "29-Sep-2026", "ce_oi": 60, "pe_oi": 120},
    ])

    test_ts = datetime(2026, 9, 15, 9, 20, 0)
    saved_path = store.save_snapshot(df_multi, 23325.0, test_ts, expiry="15-Sep-2026", base_dir=multi_dir)
    assert saved_path is not None
    assert saved_path.exists()

    loaded = store.load_day_multi(date(2026, 9, 15))
    assert len(loaded) == 1
    ts, spot, df_loaded = loaded[0]
    assert spot == 23325.0
    assert len(df_loaded) == 6
    # 23300 appears 3 times under different expiries
    assert (df_loaded["strike"] == 23300.0).sum() == 3
    assert set(df_loaded["expiry"].unique()) == {"15-Sep-2026", "22-Sep-2026", "29-Sep-2026"}


def test_prune_multi_snapshots_refuses_primary_snapshots(tmp_path):
    """Safety test: pruner must REFUSE with ValueError if directed at a folder other than 'snapshots_multi'."""
    import store
    primary_dir = tmp_path / "snapshots"
    primary_dir.mkdir()
    # Create fake old snapshot day in primary
    old_primary = primary_dir / "2026-05-01"
    old_primary.mkdir()

    with pytest.raises(ValueError, match="SAFETY VIOLATION"):
        store.prune_multi_snapshots(retention_days=90, base_dir=primary_dir)
    
    # Assert primary directory was not touched
    assert old_primary.exists()


def test_prune_multi_snapshots_dry_run_and_execution(tmp_path):
    """Verifies dry-run mode identifies old folders without deleting them, and execution deletes them."""
    import store
    multi_dir = tmp_path / "snapshots_multi"
    multi_dir.mkdir()

    old_dir = multi_dir / "2026-05-01"
    old_dir.mkdir()
    # Create 15 recent session folders to satisfy min_keep_sessions safety margin
    for day in range(1, 16):
        (multi_dir / f"2026-09-{day:02d}").mkdir()

    # Dry-run
    candidates = store.prune_multi_snapshots(retention_days=90, dry_run=True, base_dir=multi_dir)
    assert candidates == ["2026-05-01"]
    assert old_dir.exists()
    assert (multi_dir / "2026-09-15").exists()

    # Real execution
    deleted = store.prune_multi_snapshots(retention_days=90, dry_run=False, base_dir=multi_dir)
    assert deleted == ["2026-05-01"]
    assert not old_dir.exists()
    assert (multi_dir / "2026-09-15").exists()


def test_prune_multi_snapshots_never_deletes_recent_n_sessions(tmp_path):
    """Safety test: retention never selects the most recent N (>=15) sessions for deletion even if retention_days=0."""
    import store
    multi_dir = tmp_path / "snapshots_multi"
    multi_dir.mkdir()

    # Create 20 session folders from 2026-01-01 to 2026-01-20
    for day in range(1, 21):
        (multi_dir / f"2026-01-{day:02d}").mkdir()

    # Even with retention_days=0 (which would delete everything without safety guard),
    # the 15 most recent sessions (days 06-20) MUST NOT be selected
    candidates = store.prune_multi_snapshots(retention_days=0, dry_run=True, base_dir=multi_dir, min_keep_sessions=15)
    assert len(candidates) == 5
    assert candidates == [f"2026-01-{day:02d}" for day in range(1, 6)]
    for day in range(6, 21):
        assert f"2026-01-{day:02d}" not in candidates


