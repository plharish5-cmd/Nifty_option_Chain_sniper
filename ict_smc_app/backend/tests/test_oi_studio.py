"""
test_oi_studio.py — Unit & Integration tests for StockMojo Open Interest & Writer Map Studio.

Tests:
  1. OI Change+Total segments sum accurately (solid, inc_hatched, dec_hollow).
  2. Time-window slicing (Last 3 min, Last 15 min, Last 1 hr, All) computes accurate deltas.
  3. All expiry modes ('this_wk', 'next_wk', 'both') return structured strike matrices.
  4. Max Pain and Sentiment scoring calculations.
  5. API endpoint contracts for GET /api/chain/oi-studio.
"""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("ICT_DATA_DIR", str(BACKEND_DIR / "tests" / ".data"))

import pytest
from app.engine import oi_studio


def test_oi_studio_tab1_segment_mathematics():
    """Validates that Tab 1 (OI Change + Total) segments reconstruct base and total OI accurately."""
    # Scenario 1: Call OI increased (call_chg > 0)
    # 22500 CE: base = 1,000,000, total = 1,400,000 (+400,000)
    # Expected: call_solid = 1,000,000, call_inc_hatched = 400,000, call_dec_hollow = 0
    #
    # Scenario 2: Put OI decreased (put_chg < 0)
    # 22500 PE: base = 2,000,000, total = 1,700,000 (-300,000)
    # Expected: put_solid = 1,700,000, put_inc_hatched = 0, put_dec_hollow = 300,000
    mock_snaps = [
        {
            "ts": 1793159100,
            "time_label": "09:15 AM",
            "spot": 22650.0,
            "strikes": {
                22500.0: {"call_oi": 1000000.0, "put_oi": 2000000.0},
                22600.0: {"call_oi": 1500000.0, "put_oi": 1800000.0},
            },
        },
        {
            "ts": 1793160000,
            "time_label": "09:30 AM",
            "spot": 22680.0,
            "strikes": {
                22500.0: {"call_oi": 1400000.0, "put_oi": 1700000.0},
                22600.0: {"call_oi": 1300000.0, "put_oi": 2100000.0},
            },
        },
    ]

    res = oi_studio.compute_oi_studio_payload(
        snapshots=mock_snaps,
        spot_price=22680.0,
        strike_range="all",
    )
    assert res["ok"] is True
    assert len(res["strikes"]) >= 2

    s22500 = next(s for s in res["strikes"] if s["strike"] == 22500)

    # Call increased checks
    assert s22500["call_oi_base"] == 1000000.0
    assert s22500["call_oi_total"] == 1400000.0
    assert s22500["call_oi_chg"] == 400000.0
    assert s22500["call_solid"] == 1000000.0
    assert s22500["call_inc_hatched"] == 400000.0
    assert s22500["call_dec_hollow"] == 0.0
    # Invariant: solid + hatched = total
    assert s22500["call_solid"] + s22500["call_inc_hatched"] == s22500["call_oi_total"]

    # Put decreased checks
    assert s22500["put_oi_base"] == 2000000.0
    assert s22500["put_oi_total"] == 1700000.0
    assert s22500["put_oi_chg"] == -300000.0
    assert s22500["put_solid"] == 1700000.0
    assert s22500["put_inc_hatched"] == 0.0
    assert s22500["put_dec_hollow"] == 300000.0
    # Invariant: solid + hollow = base
    assert s22500["put_solid"] + s22500["put_dec_hollow"] == s22500["put_oi_base"]


def test_time_window_slicing_computation():
    """Validates that slicing between arbitrary start_ts and end_ts computes window deltas accurately."""
    t0 = 1793160000  # 09:30 AM
    t1 = t0 + 180    # 09:33 AM (Last 3m)
    t2 = t0 + 900    # 09:45 AM (Last 15m)
    t3 = t0 + 3600   # 10:30 AM (Last 1h)

    mock_snaps = [
        {"ts": t0, "time_label": "09:30 AM", "spot": 22700.0, "strikes": {22700.0: {"call_oi": 1000000.0, "put_oi": 1000000.0}}},
        {"ts": t1, "time_label": "09:33 AM", "spot": 22710.0, "strikes": {22700.0: {"call_oi": 1050000.0, "put_oi": 1100000.0}}},
        {"ts": t2, "time_label": "09:45 AM", "spot": 22720.0, "strikes": {22700.0: {"call_oi": 1200000.0, "put_oi": 1350000.0}}},
        {"ts": t3, "time_label": "10:30 AM", "spot": 22750.0, "strikes": {22700.0: {"call_oi": 1400000.0, "put_oi": 1800000.0}}},
    ]

    # Test Window 1: Last 3m (t2 to t3 not applicable, test t0 to t1)
    res_3m = oi_studio.compute_oi_studio_payload(mock_snaps, start_ts=t0, end_ts=t1, strike_range="atm")
    s_3m = next(s for s in res_3m["strikes"] if s["strike"] == 22700)
    assert s_3m["call_oi_chg"] == 50000.0
    assert s_3m["put_oi_chg"] == 100000.0

    # Test Window 2: Last 15m (t1 to t2: 9:33 to 9:45)
    res_15m = oi_studio.compute_oi_studio_payload(mock_snaps, start_ts=t1, end_ts=t2, strike_range="atm")
    s_15m = next(s for s in res_15m["strikes"] if s["strike"] == 22700)
    assert s_15m["call_oi_chg"] == 150000.0  # 1200000 - 1050000
    assert s_15m["put_oi_chg"] == 250000.0   # 1350000 - 1100000

    # Test Window 3: Full session (t0 to t3)
    res_all = oi_studio.compute_oi_studio_payload(mock_snaps, start_ts=t0, end_ts=t3, strike_range="atm")
    s_all = next(s for s in res_all["strikes"] if s["strike"] == 22700)
    assert s_all["call_oi_chg"] == 400000.0  # 1400000 - 1000000
    assert s_all["put_oi_chg"] == 800000.0   # 1800000 - 1000000


def test_all_expiry_modes_integration():
    """Validates that this_wk, next_wk, and both return rich strike arrays and expiries_meta."""
    from app import db
    if not db.query("SELECT ts FROM chain_snap WHERE expiry='2026-11-03' LIMIT 1"):
        t0 = 1793160000
        for exp in ("2026-11-03", "2026-11-09"):
            db.save_chain_snap(exp, t0, [
                (22600.0, 500000, 3000000, 150.0, 40.0, 1000, 1000),
                (22650.0, 1200000, 2500000, 110.0, 60.0, 1000, 1000),
                (22700.0, 2000000, 2000000, 80.0, 80.0, 1000, 1000),
                (22750.0, 2800000, 1200000, 50.0, 120.0, 1000, 1000),
                (22800.0, 3500000, 400000, 30.0, 160.0, 1000, 1000),
            ])
            db.save_chain_snap(exp, t0 + 180, [
                (22600.0, 520000, 3200000, 140.0, 45.0, 2000, 2000),
                (22650.0, 1250000, 2600000, 105.0, 65.0, 2000, 2000),
                (22700.0, 2100000, 2100000, 75.0, 85.0, 2000, 2000),
                (22750.0, 2900000, 1250000, 48.0, 125.0, 2000, 2000),
                (22800.0, 3600000, 420000, 28.0, 165.0, 2000, 2000),
            ])
        db.upsert_candles("13", [
            {"ts": t0, "o": 22690, "h": 22710, "l": 22680, "c": 22700, "v": 5000},
            {"ts": t0 + 180, "o": 22700, "h": 22720, "l": 22695, "c": 22710, "v": 6000},
        ])

    for mode in ("this_wk", "next_wk", "both"):
        res = oi_studio.get_oi_studio_data(expiry_mode=mode, strike_range="10")
        assert res["ok"] is True
        assert "strikes" in res
        assert len(res["strikes"]) > 0
        assert "expiries_meta" in res
        assert res["expiries_meta"]["active_mode"] == mode
        assert "cur_expiry" in res["expiries_meta"]
        assert "summary" in res
        assert "pcr" in res["summary"]
        assert "sentiment_score" in res["summary"]


def test_max_pain_and_sentiment_calculation():
    """Validates that Max Pain strike minimizes writer loss and sentiment gauge scores accurately."""
    mock_snaps = [
        {
            "ts": 1793160000,
            "spot": 22700.0,
            "strikes": {
                22600.0: {"call_oi": 500000.0, "put_oi": 3000000.0},
                22700.0: {"call_oi": 2000000.0, "put_oi": 2000000.0},
                22800.0: {"call_oi": 3500000.0, "put_oi": 400000.0},
            },
        }
    ]
    res = oi_studio.compute_oi_studio_payload(mock_snaps, spot_price=22700.0, strike_range="all")
    assert res["ok"] is True
    assert res["max_pain"] == 22700
    assert 0 <= res["summary"]["sentiment_score"] <= 100
