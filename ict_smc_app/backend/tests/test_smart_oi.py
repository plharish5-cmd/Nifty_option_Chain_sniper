"""
test_smart_oi.py — Unit tests for Smart OI Filtering & Price-Action Confluence Engine.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import smart_oi


def test_smart_oi_noise_filtering():
    """Verify deep OTM noise and stale position filters correctly isolate signal strikes."""
    mock_rows = [
        # Strike 1: Retail noise (Low OI, Low volume) -> Must be filtered out
        {
            "strike": 21500.0,
            "ce": {"oi": 40000, "oi_chg": 500, "ltp": 1200.0, "chg": 0.0, "vol": 50},
            "pe": {"oi": 60000, "oi_chg": 200, "ltp": 2.0, "chg": -0.5, "vol": 100},
        },
        # Strike 2: Stale position (High OI, but near-zero change) -> Must be filtered out
        {
            "strike": 22000.0,
            "ce": {"oi": 1500000, "oi_chg": 200, "ltp": 700.0, "chg": -10.0, "vol": 15000},
            "pe": {"oi": 2000000, "oi_chg": 300, "ltp": 8.0, "chg": -1.0, "vol": 20000},
        },
        # Strike 3: Active Institutional Put Support Floor -> Must be retained as Signal Strike
        {
            "strike": 22600.0,
            "ce": {"oi": 800000, "oi_chg": 15000, "ltp": 140.0, "chg": -15.0, "vol": 80000},
            "pe": {"oi": 4500000, "oi_chg": 450000, "ltp": 55.0, "chg": 5.0, "vol": 500000},
        },
        # Strike 4: Active Institutional Call Resistance Ceiling -> Must be retained as Signal Strike
        {
            "strike": 22800.0,
            "ce": {"oi": 5200000, "oi_chg": 620000, "ltp": 45.0, "chg": -8.0, "vol": 600000},
            "pe": {"oi": 900000, "oi_chg": 10000, "ltp": 160.0, "chg": 12.0, "vol": 70000},
        },
    ]

    chain_dict = {
        "day": "2026-10-22",
        "expiry": "2026-10-27",
        "spot": 22700.0,
        "step": 50.0,
        "rows": mock_rows,
    }

    res = smart_oi.filter_institutional_smart_oi(chain_dict, range_mode="all")
    assert res["ok"] is True
    assert res["filtered_out_count"] >= 2
    assert res["signal_strike_count"] >= 2

    # Verify key support & resistance identification
    assert res["key_support_level"] == 22600.0
    assert res["key_resistance_level"] == 22800.0

    sig_strikes = {s["strike"]: s for s in res["signal_strikes"]}
    assert 22600.0 in sig_strikes
    assert sig_strikes[22600.0]["signal_type"] == "PUT_SUPPORT_FLOOR"
    assert 22800.0 in sig_strikes
    assert sig_strikes[22800.0]["signal_type"] == "CALL_RESISTANCE_CEILING"


def test_smart_oi_confluence_and_divergence():
    """Verify price action confluence scoring and divergence trap warnings."""
    signal_data = {
        "key_support_level": 22600.0,
        "key_resistance_level": 22800.0,
    }

    # Simulate candles interacting with levels
    candles = [
        # Candle 1: Testing support floor at 22600 and bouncing green
        {"ts": 1700000060, "o": 22615.0, "h": 22640.0, "l": 22595.0, "c": 22635.0, "v": 15000.0},
        # Candle 2: Neutral mid-range
        {"ts": 1700000120, "o": 22635.0, "h": 22670.0, "l": 22630.0, "c": 22660.0, "v": 12000.0},
        # Candle 3: Testing resistance ceiling at 22800 and rejecting red
        {"ts": 1700000180, "o": 22795.0, "h": 22810.0, "l": 22760.0, "c": 22765.0, "v": 18000.0},
        # Candle 4: Divergence warning (Price pushing up +20 pts, but Smart OI delta strongly negative)
        {"ts": 1700000240, "o": 22710.0, "h": 22735.0, "l": 22705.0, "c": 22730.0, "v": 14000.0},
    ]

    pcr_series = [
        {"time": 1700000060, "net_oi_chg": 350000, "pcr_oi": 1.25, "pcr_vol": 1.30},
        {"time": 1700000120, "net_oi_chg": 50000, "pcr_oi": 1.22, "pcr_vol": 1.20},
        {"time": 1700000180, "net_oi_chg": -400000, "pcr_oi": 0.82, "pcr_vol": 0.80},
        {"time": 1700000240, "net_oi_chg": -500000, "pcr_oi": 0.75, "pcr_vol": 0.70},  # Bearish trap
    ]

    res = smart_oi.compute_smart_oi_confluence(signal_data, candles, pcr_series=pcr_series)
    assert len(res["candles"]) == 4
    assert res["stats"]["bullish_confluences"] >= 1
    assert res["stats"]["bearish_confluences"] >= 1
    assert res["stats"]["divergence_warnings"] >= 1

    kinds = [m["kind"] for m in res["markers"]]
    assert "BULLISH_CONFLUENCE" in kinds
    assert "BEARISH_CONFLUENCE" in kinds
    assert "BEARISH_TRAP_DIVERGENCE" in kinds


def test_build_smart_oi_payload():
    """Verify master payload assembly with stats and time_series."""
    mock_rows = [
        {"strike": 22650.0, "ce": {"oi": 600000, "oi_chg": 20000, "ltp": 90.0, "chg": -5.0, "vol": 25000},
         "pe": {"oi": 2500000, "oi_chg": 180000, "ltp": 40.0, "chg": 2.0, "vol": 120000}},
        {"strike": 22750.0, "ce": {"oi": 3000000, "oi_chg": 250000, "ltp": 35.0, "chg": -3.0, "vol": 180000},
         "pe": {"oi": 700000, "oi_chg": -15000, "ltp": 110.0, "chg": 8.0, "vol": 30000}},
    ]
    chain_dict = {
        "day": "2026-10-22",
        "expiry": "2026-10-27",
        "spot": 22700.0,
        "step": 50.0,
        "rows": mock_rows,
    }
    candles = [
        {"ts": 1700000060, "o": 22690.0, "h": 22710.0, "l": 22685.0, "c": 22705.0, "v": 10000.0},
    ]

    payload = smart_oi.build_smart_oi_payload(chain_dict, candles=candles, tf="3m")
    assert payload["ok"] is True
    assert "time_series" in payload
    assert "signal_strikes" in payload
    assert payload["tf"] == "3m"
    assert payload["stats"]["signal_strikes"] >= 1
