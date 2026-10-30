"""
tests/test_gann_pivots_gaps.py — Comprehensive Unit Tests for Gann Square of 9,
Monotonic R1-R4 / S1-S4 Pivots, Unfilled Opening Gaps, and Gann Time Cycles.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
import pytest
from starlette.testclient import TestClient

from app.api.main import app
from app.engine.gann_pivots_gaps import (
    gann_sq9,
    compute_master_levels_from_daily,
    get_gann_pivots_gaps,
    GANN_TIME_CYCLES,
)


def test_gann_sq9_formula():
    """
    Verifies gann_sq9(price, degrees) exactly matches:
    round(( math.sqrt(price) + degrees / 180.0 ) ** 2, 2)
    """
    test_cases = [
        (22936.75, 45.0),
        (22936.75, 90.0),
        (22936.75, 135.0),
        (22936.75, 180.0),
        (22936.75, -45.0),
        (22936.75, -90.0),
        (22936.75, -135.0),
        (22936.75, -180.0),
        (10000.0, 45.0),
        (25000.0, 90.0),
    ]

    for price, deg in test_cases:
        expected = round((math.sqrt(price) + deg / 180.0) ** 2, 2)
        actual = gann_sq9(price, deg)
        assert actual == pytest.approx(expected, abs=0.01)

    # Edge cases
    assert gann_sq9(None, 45) is None
    assert gann_sq9(0, 45) is None
    assert gann_sq9(-500, 45) is None


def test_monotonic_r1_to_r4_and_s1_to_s4_walk_backward():
    """
    Verifies Monotonic R1..R4 and S1..S4 walk backward across daily bars accurately:
    R1 = prev_day['high'], then walks backward appending strictly higher highs until 4 found.
    S1 = prev_day['low'], then walks backward appending strictly lower lows until 4 found.
    """
    # Create synthetic daily bars (sorted chronologically) where 4 strictly higher highs and 4 strictly lower lows exist
    bars = [
        {"date": "2026-10-21", "open": 22400.0, "high": 23500.0, "low": 22100.0, "close": 22500.0},
        {"date": "2026-10-22", "open": 22500.0, "high": 23300.0, "low": 22250.0, "close": 22600.0},
        {"date": "2026-10-23", "open": 22600.0, "high": 23150.0, "low": 22400.0, "close": 22700.0},
        {"date": "2026-10-24", "open": 22700.0, "high": 23000.0, "low": 22500.0, "close": 22800.0},
        {"date": "2026-10-25", "open": 22900.0, "high": 22950.0, "low": 22600.0, "close": 22920.0},  # ref_row
        {"date": "2026-10-26", "open": 22930.0, "high": 23150.0, "low": 22550.0, "close": 23000.0},  # today
    ]

    res = compute_master_levels_from_daily(bars, target_date_str="2026-10-26")
    assert res["ok"] is True
    assert res["ref_date"] == "2026-10-25"
    assert res["ref_close"] == 22920.0

    r_levels = res["resistances"]
    assert len(r_levels) == 4
    # R1 = 22950.0 (from 2026-10-25)
    assert r_levels[0]["level"] == "R1"
    assert r_levels[0]["price"] == 22950.0
    # R2 = 23000.0 (from 2026-10-24, since 23000 > 22950)
    assert r_levels[1]["level"] == "R2"
    assert r_levels[1]["price"] == 23000.0
    # R3 = 23150.0 (from 2026-10-23, since 23150 > 23000)
    assert r_levels[2]["level"] == "R3"
    assert r_levels[2]["price"] == 23150.0
    # R4 = 23300.0 (from 2026-10-22, since 23300 > 23150)
    assert r_levels[3]["level"] == "R4"
    assert r_levels[3]["price"] == 23300.0

    # Verify monotonic increasing
    for k in range(1, len(r_levels)):
        assert r_levels[k]["price"] > r_levels[k-1]["price"]

    s_levels = res["supports"]
    assert len(s_levels) == 4
    # S1 = 22600.0 (from 2026-10-25)
    assert s_levels[0]["level"] == "S1"
    assert s_levels[0]["price"] == 22600.0
    # S2 = 22500.0 (from 2026-10-24, since 22500 < 22600)
    assert s_levels[1]["level"] == "S2"
    assert s_levels[1]["price"] == 22500.0
    # S3 = 22400.0 (from 2026-10-23, since 22400 < 22500)
    assert s_levels[2]["level"] == "S3"
    assert s_levels[2]["price"] == 22400.0
    # S4 = 22250.0 (from 2026-10-22, since 22250 < 22400)
    assert s_levels[3]["level"] == "S4"
    assert s_levels[3]["price"] == 22250.0

    # Verify monotonic decreasing
    for k in range(1, len(s_levels)):
        assert s_levels[k]["price"] < s_levels[k-1]["price"]


def test_unfilled_gaps_tracking_and_fill_removal():
    """
    Verifies Upper_Gap_1..3 and Lower_Gap_1..3 track unfilled > 5pt opening gaps
    and remove them once filled by subsequent daily highs/lows.
    """
    bars = [
        # Day 0: Base
        {"date": "2026-10-01", "open": 22000.0, "high": 22100.0, "low": 21900.0, "close": 22050.0},
        # Day 1: Gap Up (+50 pts > 5 pts) -> Open 22100 vs prev close 22050. Anchored at 22050.
        {"date": "2026-10-02", "open": 22100.0, "high": 22250.0, "low": 22080.0, "close": 22200.0},
        # Day 2: Gap Down (-100 pts < -5 pts) -> Open 22100 vs prev close 22200. Anchored at 22200.
        {"date": "2026-10-03", "open": 22100.0, "high": 22150.0, "low": 22090.0, "close": 22120.0},
        # Day 3: Price drops to 22040, filling Day 1's Gap Up (L <= 22050)!
        {"date": "2026-10-04", "open": 22120.0, "high": 22160.0, "low": 22040.0, "close": 22150.0},
        # Day 4: Today
        {"date": "2026-10-05", "open": 22150.0, "high": 22180.0, "low": 22130.0, "close": 22160.0},
    ]

    res = compute_master_levels_from_daily(bars, target_date_str="2026-10-05")
    assert res["ok"] is True

    # Day 1's gap up at 22050 was filled on Day 3 (low=22040 <= 22050).
    # Day 2's gap down at 22200 has high=22160 on Day 3 and high=22180 on Day 4, so it is STILL OPEN!
    upper_gaps = res["upper_gaps"]
    lower_gaps = res["lower_gaps"]

    # Day 2 gap at 22200 is above ref_close (22150), so it must be in upper_gaps!
    found_22200 = any(g["price"] == 22200.0 for g in upper_gaps)
    assert found_22200 is True

    # The 22050 gap must NOT be present (it was filled)
    found_22050 = any(g["price"] == 22050.0 for g in lower_gaps)
    assert found_22050 is False


def test_gann_time_cycles_alert_within_plus_minus_one_day():
    """
    Verifies GANN_TIME_CYCLES triggers Top+{c}D and Btm+{c}D alerts within +/- 1 day.
    """
    today_dt = date(2026, 10, 30)
    high_dt = today_dt - timedelta(days=90)  # Exactly 90 days ago (Top+90D)
    low_dt = today_dt - timedelta(days=144)  # Exactly 144 days ago (Btm+144D)

    bars = [
        {"date": low_dt.isoformat(), "open": 21000.0, "high": 21100.0, "low": 20800.0, "close": 20900.0},  # Major Low
        {"date": high_dt.isoformat(), "open": 24000.0, "high": 24500.0, "low": 23900.0, "close": 24400.0},  # Major High
        {"date": (today_dt - timedelta(days=1)).isoformat(), "open": 22900.0, "high": 22950.0, "low": 22800.0, "close": 22920.0},
        {"date": today_dt.isoformat(), "open": 22950.0, "high": 23120.0, "low": 22900.0, "close": 23050.0},
    ]

    res = compute_master_levels_from_daily(bars, target_date_str=today_dt.isoformat())
    assert res["ok"] is True
    tc = res["time_cycles"]
    assert tc["days_from_major_high"] == 90
    assert tc["days_from_major_low"] == 144

    alerts = tc["active_alerts"]
    assert any("Top+90D" in a for a in alerts)
    assert any("Btm+144D" in a for a in alerts)
    assert "Top+90D" in tc["alert_text"]
    assert "Btm+144D" in tc["alert_text"]


def test_fastapi_gann_pivots_gaps_endpoint():
    """
    Verifies GET /api/desk/gann-pivots-gaps endpoint returns 200 with valid structure.
    """
    client = TestClient(app)
    resp = client.get("/api/desk/gann-pivots-gaps?target_date=2026-10-30&spot=23050.0")
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert "ref_date" in data
    assert "ref_close" in data
    assert "resistances" in data
    assert "supports" in data
    assert "upper_gaps" in data
    assert "lower_gaps" in data
    assert "gann_resistances" in data
    assert "gann_supports" in data
    assert "time_cycles" in data

    # Verify R1 and S1 structure
    assert len(data["resistances"]) >= 1
    assert data["resistances"][0]["level"] == "R1"
    assert len(data["supports"]) >= 1
    assert data["supports"][0]["level"] == "S1"

    # Verify Gann levels (45, 90, 135, 180 deg)
    assert len(data["gann_resistances"]) == 4
    assert len(data["gann_supports"]) == 4
    assert data["gann_resistances"][0]["degrees"] == 45
    assert data["gann_resistances"][1]["degrees"] == 90
