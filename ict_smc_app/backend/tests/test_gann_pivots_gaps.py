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

    # Verify time_cycles alert_text is never "None"
    tc = data["time_cycles"]
    assert tc["alert_text"] != "None"
    assert len(tc["alert_text"]) > 0
    assert "horizons" in tc
    assert "intermediate_90d" in tc["horizons"]
    assert "major_1yr" in tc["horizons"]
    assert "global_5yr" in tc["horizons"]


def test_gann_time_cycles_countdown_never_none():
    """
    Verifies that when today is between cycles (e.g. 35 days from high, 142 days from low),
    alert_text NEVER shows 'None' and formats the live position and next-cycle countdown:
    Top+35D (Next: 45D in 10d) · Btm+142D (🔥 144D Cycle in 2d!)
    """
    today_dt = date(2026, 10, 30)
    high_dt = today_dt - timedelta(days=35)   # 35 days ago (between 30D and 45D)
    low_dt = today_dt - timedelta(days=142)   # 142 days ago (2 days before 144D cycle!)

    bars = [
        {"date": low_dt.isoformat(), "open": 21000.0, "high": 21100.0, "low": 20800.0, "close": 20900.0},  # Low
        {"date": high_dt.isoformat(), "open": 24000.0, "high": 24500.0, "low": 23900.0, "close": 24400.0},  # High
        {"date": (today_dt - timedelta(days=1)).isoformat(), "open": 22900.0, "high": 22950.0, "low": 22800.0, "close": 22920.0},
        {"date": today_dt.isoformat(), "open": 22950.0, "high": 23120.0, "low": 22900.0, "close": 23050.0},
    ]

    res = compute_master_levels_from_daily(bars, target_date_str=today_dt.isoformat())
    assert res["ok"] is True
    tc = res["time_cycles"]
    assert tc["alert_text"] != "None"
    assert "Top+35D" in tc["alert_text"]
    assert "Next: 45D" in tc["alert_text"]
    # 142 is 2 days before 144 -> approaching active cycle!
    assert "144D Cycle" in tc["alert_text"]


def test_sync_and_append_master_csv_and_excel():
    """
    Verifies sync_and_append_daily_master_csv creates the master CSV and Excel view.
    """
    from app.engine.gann_pivots_gaps import (
        sync_and_append_daily_master_csv,
        CANONICAL_MASTER_CSV,
        EXCEL_MASTER_VIEW,
    )
    res = sync_and_append_daily_master_csv()
    assert res["ok"] is True
    assert res["total_bars"] >= 1900
    assert CANONICAL_MASTER_CSV.exists()
    assert EXCEL_MASTER_VIEW.exists()


def test_full_history_gap_fill_and_walkback_upon_appending_new_day():
    """
    Test 1 (Full-History Gap Fill & Walkback Upon Appending New Day):
    When a new daily OHLC bar is appended at 15:40 IST whose High/Low fills an old gap from 200 days ago
    and sets a new R1 / S1, the engine re-scans the entire dataset from index 0, removes the 200-day-old
    filled gap, walks backward across all historical bars to recompute R1-R4 and S1-S4, and updates
    Gann Sq9 and Time Cycles.
    """
    base_date = date(2025, 1, 10)
    bars: List[Dict[str, Any]] = []

    # Historical anchor bars with macro swing highs and lower lows
    bars.append({"date": "2025-01-01", "open": 24000.0, "high": 24800.0, "low": 19600.0, "close": 24000.0})
    bars.append({"date": "2025-01-02", "open": 24000.0, "high": 24700.0, "low": 19700.0, "close": 24000.0})
    bars.append({"date": "2025-01-03", "open": 24000.0, "high": 24600.0, "low": 19800.0, "close": 24000.0})

    # Bar 3: Day 0 base
    bars.append({
        "date": base_date.isoformat(),
        "open": 20000.0,
        "high": 20100.0,
        "low": 19900.0,
        "close": 20050.0,
    })

    # Bar 4: Day 1 creates a Gap Up at 20050.0 (+50 pts)
    bars.append({
        "date": (base_date + timedelta(days=1)).isoformat(),
        "open": 20100.0,
        "high": 20250.0,
        "low": 20080.0,
        "close": 20200.0,
    })

    # Bars 5..223: Prices trade between 21000 and 23000 (gap at 20050 remains unfilled for >200 days)
    for day_i in range(2, 221):
        cur_d = base_date + timedelta(days=day_i)
        bars.append({
            "date": cur_d.isoformat(),
            "open": 21500.0 + (day_i * 2),
            "high": 21600.0 + (day_i * 2),
            "low": 21400.0 + (day_i * 2),
            "close": 21550.0 + (day_i * 2),
        })

    # Verify that before day 221 is appended, the 200-day-old gap at 20050 is UNFILLED in lower_gaps
    res_before = compute_master_levels_from_daily(bars, mode="next_day")
    assert res_before["ok"] is True
    assert any(g["price"] == 20050.0 for g in res_before["lower_gaps"])

    # Bar 221 (appended today at 15:40 IST):
    # Its Low drops to 20020.0 (<= 20050.0, FILLING the 200-day-old gap!)
    # Its High sets a new peak at 24500.0 (setting a new R1!)
    # Its Low sets a new trough at 20020.0 (setting a new S1!)
    day_221_date = (base_date + timedelta(days=221)).isoformat()
    new_bar = {
        "date": day_221_date,
        "open": 22000.0,
        "high": 24500.0,
        "low": 20020.0,
        "close": 24025.0,
    }
    bars.append(new_bar)

    # Full recalculation across the entire dataset (0 .. N-1) with mode="next_day" (post-15:40 settled)
    res_after = compute_master_levels_from_daily(bars, target_date_str=day_221_date, mode="next_day")
    assert res_after["ok"] is True
    assert res_after["ref_date"] == day_221_date
    assert res_after["ref_close"] == 24025.0

    # 1. Old gap from 200 days ago was filled and removed by the full re-scan
    assert not any(g["price"] == 20050.0 for g in res_after["lower_gaps"])

    # 2. R1 is Day 221's High (24500.0), S1 is Day 221's Low (20020.0)
    assert res_after["resistances"][0]["level"] == "R1"
    assert res_after["resistances"][0]["price"] == 24500.0
    assert res_after["supports"][0]["level"] == "S1"
    assert res_after["supports"][0]["price"] == 20020.0

    # 3. Monotonic walkback recomputed across all preceding bars
    assert len(res_after["supports"]) == 4
    for k in range(1, len(res_after["supports"])):
        assert res_after["supports"][k]["price"] < res_after["supports"][k - 1]["price"]

    # 4. Gann Sq9 recalculated from latest settled Close (24025.0)
    expected_sq9_r1 = round((math.sqrt(24025.0) + 45.0 / 180.0) ** 2, 2)
    assert res_after["gann_resistances"][0]["price"] == expected_sq9_r1

    # 5. Recalc audit confirms all historical rows were scanned
    audit = res_after["recalc_audit"]
    assert audit["total_historical_days_scanned"] == len(bars)
    assert audit["first_historical_date"] == "2025-01-01"
    assert audit["latest_settled_date"] == day_221_date
    assert audit["post_1540_settled"] is True


def test_1540_ist_cutoff_behavior():
    """
    Test 2 (15:40 IST Cutoff Behavior):
    Before 15:40 IST on trading day T, ref_date is T-1 (intraday levels active);
    at/after 15:40 IST on trading day T, ref_date advances to T and recalculates all levels
    across the full dataset including day T.
    """
    bars = [
        {"date": "2026-10-21", "open": 22400.0, "high": 23500.0, "low": 22100.0, "close": 22500.0},
        {"date": "2026-10-22", "open": 22500.0, "high": 23300.0, "low": 22250.0, "close": 22600.0},
        {"date": "2026-10-23", "open": 22600.0, "high": 23150.0, "low": 22400.0, "close": 22700.0},
        {"date": "2026-10-24", "open": 22700.0, "high": 23000.0, "low": 22500.0, "close": 22800.0},
        {"date": "2026-10-25", "open": 22900.0, "high": 22950.0, "low": 22600.0, "close": 22920.0},  # Day T-1
        {"date": "2026-10-26", "open": 22930.0, "high": 23200.0, "low": 22550.0, "close": 23100.0},  # Day T
    ]

    # A) Before 15:40 IST (e.g. 14:30 IST): ref_date is T-1 (2026-10-25)
    dt_before_1540 = datetime(2026, 10, 26, 14, 30)
    res_before = compute_master_levels_from_daily(
        bars,
        target_date_str="2026-10-26",
        current_ist=dt_before_1540,
    )
    assert res_before["ok"] is True
    assert res_before["ref_date"] == "2026-10-25"
    assert res_before["ref_close"] == 22920.0
    assert res_before["recalc_audit"]["post_1540_settled"] is False
    assert res_before["recalc_audit"]["total_historical_days_scanned"] == 6

    # B) At/After 15:40 IST (e.g. 15:40 IST): ref_date advances to T (2026-10-26)
    dt_after_1540 = datetime(2026, 10, 26, 15, 40)
    res_after = compute_master_levels_from_daily(
        bars,
        target_date_str="2026-10-26",
        current_ist=dt_after_1540,
    )
    assert res_after["ok"] is True
    assert res_after["ref_date"] == "2026-10-26"
    assert res_after["ref_close"] == 23100.0
    assert res_after["recalc_audit"]["post_1540_settled"] is True
    assert res_after["recalc_audit"]["total_historical_days_scanned"] == 6

    # C) Mode query param override test: mode='intraday' vs mode='next_day'
    res_intraday = compute_master_levels_from_daily(bars, target_date_str="2026-10-26", mode="intraday")
    assert res_intraday["ref_date"] == "2026-10-25"
    assert res_intraday["recalc_audit"]["post_1540_settled"] is False

    res_next_day = compute_master_levels_from_daily(bars, target_date_str="2026-10-26", mode="next_day")
    assert res_next_day["ref_date"] == "2026-10-26"
    assert res_next_day["recalc_audit"]["post_1540_settled"] is True


def test_fastapi_recalculate_post_endpoint():
    """
    Verifies POST /api/desk/gann-pivots-gaps/recalculate endpoint triggers full recalculation
    and returns 200 with recalc_audit metadata.
    """
    client = TestClient(app)
    resp = client.post("/api/desk/gann-pivots-gaps/recalculate?sec_id=13")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "recalc_audit" in data
    audit = data["recalc_audit"]
    assert audit["total_historical_days_scanned"] >= 1900
    assert len(audit["first_historical_date"]) >= 10
    assert len(audit["latest_settled_date"]) >= 10
    assert "last_full_recalc_ist" in audit

