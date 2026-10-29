"""
Unit and integration tests for Institutional ICT Reference Levels & True Breakout Engine.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", "/tmp/ict-institutional-tests")

import pytest
from app.engine import institutional_levels
from fastapi.testclient import TestClient
from app.api.main import app

client = TestClient(app)


def test_pdh_pdl_extraction_from_daily_candles():
    daily_candles = [
        {"d": "2026-10-27", "o": 22600.0, "h": 22700.0, "l": 22550.0, "c": 22650.0},
        {"d": "2026-10-28", "o": 22660.0, "h": 22800.0, "l": 22620.0, "c": 22780.0},
        {"d": "2026-10-29", "o": 22790.0, "h": 22920.0, "l": 22750.0, "c": 22900.0},
    ]
    # Intraday bars for 2026-10-29
    intraday_bars = [
        {"ts": 1793245500, "o": 22790.0, "h": 22820.0, "l": 22780.0, "c": 22810.0, "rvol": 1.1},
        {"ts": 1793245680, "o": 22810.0, "h": 22850.0, "l": 22800.0, "c": 22840.0, "rvol": 1.2},
    ]

    res = institutional_levels.compute_institutional_levels(
        sec_id="13",
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=1.2,
        override_pe_ce_delta_l=10.0,
    )

    assert res["ok"] is True
    # Yesterday is 2026-10-28
    assert res["levels"]["pdh"] == 22800.0
    assert res["levels"]["pdl"] == 22620.0
    assert res["levels"]["pdc"] == 22780.0
    assert res["levels"]["pdo"] == 22660.0
    assert res["levels"]["session_open"] == 22790.0
    assert res["current_price"] == 22840.0


def test_case_a_pdh_sweep_rejection_judas_swing():
    daily_candles = [
        {"d": "2026-10-28", "o": 22700.0, "h": 22800.0, "l": 22650.0, "c": 22750.0},
    ]
    # Today spiked above PDH (22800) to 22825, but closed below it at 22780 on low volume
    intraday_bars = [
        {"ts": 1793245500, "o": 22760.0, "h": 22825.0, "l": 22755.0, "c": 22780.0, "rvol": 0.8},
    ]

    res = institutional_levels.compute_institutional_levels(
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=0.8,
        override_pe_ce_delta_l=5.0,
    )

    assert res["status"] == "PDH_SWEEP_REJECTION"
    assert res["is_sweep"] is True
    assert res["is_breakout"] is False
    assert res["alert_kind"] == "SIG_PDH_SWEEP"
    assert "PDH LIQUIDITY SWEEP (FALSE BREAKOUT)" in res["alert_text"]


def test_case_b_true_pdh_breakout_expansion():
    daily_candles = [
        {"d": "2026-10-28", "o": 22700.0, "h": 22800.0, "l": 22650.0, "c": 22750.0},
    ]
    # Today cleanly closed above PDH (22800) at 22845 on high RVOL (1.8x) and strong PE-CE writing (+65L)
    intraday_bars = [
        {"ts": 1793245500, "o": 22780.0, "h": 22850.0, "l": 22775.0, "c": 22845.0, "rvol": 1.8},
    ]

    res = institutional_levels.compute_institutional_levels(
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=1.8,
        override_pe_ce_delta_l=65.0,
    )

    assert res["status"] == "PDH_TRUE_BREAKOUT"
    assert res["is_breakout"] is True
    assert res["is_sweep"] is False
    assert res["alert_kind"] == "SIG_PDH_BREAKOUT"
    assert "PDH TRUE BREAKOUT EXPANSION" in res["alert_text"]
    assert "DO NOT FADE" in res["alert_text"]


def test_case_c_pdl_sweep_rejection_judas_swing():
    daily_candles = [
        {"d": "2026-10-28", "o": 22700.0, "h": 22800.0, "l": 22650.0, "c": 22750.0},
    ]
    # Today flushed below PDL (22650) to 22620, but closed back above at 22670 on low volume
    intraday_bars = [
        {"ts": 1793245500, "o": 22660.0, "h": 22680.0, "l": 22620.0, "c": 22670.0, "rvol": 0.7},
    ]

    res = institutional_levels.compute_institutional_levels(
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=0.7,
        override_pe_ce_delta_l=-5.0,
    )

    assert res["status"] == "PDL_SWEEP_REJECTION"
    assert res["is_sweep"] is True
    assert res["is_breakout"] is False
    assert res["alert_kind"] == "SIG_PDL_SWEEP"
    assert "PDL LIQUIDITY SWEEP (FALSE BREAKDOWN)" in res["alert_text"]


def test_case_d_true_pdl_breakdown_expansion():
    daily_candles = [
        {"d": "2026-10-28", "o": 22700.0, "h": 22800.0, "l": 22650.0, "c": 22750.0},
    ]
    # Today cleanly closed below PDL (22650) at 22610 on high RVOL (1.9x) and aggressive Call writing (-70L)
    intraday_bars = [
        {"ts": 1793245500, "o": 22645.0, "h": 22648.0, "l": 22605.0, "c": 22610.0, "rvol": 1.9},
    ]

    res = institutional_levels.compute_institutional_levels(
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=1.9,
        override_pe_ce_delta_l=-70.0,
    )

    assert res["status"] == "PDL_TRUE_BREAKDOWN"
    assert res["is_breakout"] is True
    assert res["is_sweep"] is False
    assert res["alert_kind"] == "SIG_PDL_BREAKDOWN"
    assert "PDL TRUE BREAKDOWN EXPANSION" in res["alert_text"]
    assert "DO NOT FADE" in res["alert_text"]


def test_inside_range_status():
    daily_candles = [
        {"d": "2026-10-28", "o": 22700.0, "h": 22800.0, "l": 22650.0, "c": 22750.0},
    ]
    intraday_bars = [
        {"ts": 1793245500, "o": 22720.0, "h": 22760.0, "l": 22710.0, "c": 22740.0, "rvol": 1.0},
    ]

    res = institutional_levels.compute_institutional_levels(
        intraday_candles=intraday_bars,
        daily_candles=daily_candles,
        override_rvol=1.0,
        override_pe_ce_delta_l=0.0,
    )

    assert res["status"] == "INSIDE_RANGE"
    assert res["is_sweep"] is False
    assert res["is_breakout"] is False


def test_api_endpoint_institutional_levels():
    r = client.get("/api/desk/institutional-levels?sec_id=13&rvol=1.6&pe_ce_delta_l=55.0")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "levels" in data
    assert "pdh" in data["levels"]
    assert "pdl" in data["levels"]
    assert "session_open" in data["levels"]
    assert "status" in data


# ── provenance: a placeholder level must never masquerade as yesterday's ──────────────────────────
# rules.json carries default_pdh/pdl/pdc/pdo, which are one specific past session's OHLC. Falling
# back to them silently does not degrade gracefully: it asserts a stale level as fact, in the one
# feature built to stop the trader fading breakouts.

def _no_daily(**kw):
    """Intraday bars but NO previous session, which is the condition that triggers the fallback."""
    bars = kw.pop("bars", None) or [
        {"ts": 1793245500, "o": 22900.0, "h": 22940.0, "l": 22890.0, "c": 22936.8, "rvol": 2.4},
    ]
    return institutional_levels.compute_institutional_levels(
        sec_id="13", intraday_candles=bars, daily_candles=[], **kw)


def test_missing_previous_session_is_reported_not_guessed():
    res = _no_daily(override_rvol=2.4, override_pe_ce_delta_l=120.0)
    assert res["levels_available"] is False
    assert res["levels_source"] == "fallback"
    assert res["prev_day_date"] is None
    assert res["status"] == "LEVELS_UNAVAILABLE"
    assert "NOT yesterday's levels" in res["details"]


def test_no_breakout_is_classified_on_placeholder_levels():
    """The specific scenario this guard exists for.

    29 Oct closed at 22,936.8, already ABOVE the default PDH of 22,778.05. With the fallback engaged
    and otherwise perfect confluence -- 2.4x RVOL and +120L writer support, comfortably past both
    thresholds -- the old code would have fired "PDH TRUE BREAKOUT EXPANSION — DO NOT FADE" against a
    level from days earlier, on a morning when the data layer had already failed.
    """
    res = _no_daily(override_rvol=2.4, override_pe_ce_delta_l=120.0)
    assert res["current_price"] > res["levels"]["pdh"], "setup check: price is above the placeholder PDH"
    assert res["is_breakout"] is False
    assert res["status"] != "PDH_TRUE_BREAKOUT"
    assert not res["alert_kind"], "a stale level must not raise any alert at all"
    assert not res["alert_text"]


def test_no_sweep_is_classified_on_placeholder_levels():
    bars = [{"ts": 1793245500, "o": 22800.0, "h": 22900.0, "l": 22400.0, "c": 22600.0, "rvol": 0.6}]
    res = _no_daily(bars=bars, override_rvol=0.6, override_pe_ce_delta_l=-5.0)
    assert res["is_sweep"] is False
    assert res["status"] == "LEVELS_UNAVAILABLE"


def test_real_daily_data_still_classifies_normally():
    """The guard must not suppress anything when the data IS there."""
    daily = [
        {"d": "2026-10-28", "o": 22660.0, "h": 22778.05, "l": 22497.70, "c": 22698.85},
        {"d": "2026-10-29", "o": 22720.0, "h": 22953.3, "l": 22688.0, "c": 22936.8},
    ]
    bars = [{"ts": 1793245500, "o": 22900.0, "h": 22940.0, "l": 22890.0, "c": 22936.8, "rvol": 2.4}]
    res = institutional_levels.compute_institutional_levels(
        sec_id="13", intraday_candles=bars, daily_candles=daily,
        override_rvol=2.4, override_pe_ce_delta_l=120.0)
    assert res["levels_available"] is True
    assert res["levels_source"] == "daily"
    assert res["prev_day_date"] == "2026-10-28"
    assert res["status"] != "LEVELS_UNAVAILABLE"
