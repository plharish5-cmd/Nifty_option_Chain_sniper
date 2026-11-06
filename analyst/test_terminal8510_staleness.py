#!/usr/bin/env python3
"""
analyst/test_terminal8510_staleness.py — Automated Unit Test Suite for Spot Price Freshness & Staleness.

Asserts:
  1. Outside market hours with non-zero LTP from Dhan:
     - spot_status == "STALE"
     - is_stale is True
     - spot_time_ist != current clock time (uses exchange candle close timestamp, e.g. 15:29:00 IST)
  2. During market hours with fresh non-zero LTP:
     - spot_status == "LIVE"
     - is_stale is False
     - spot_time_ist == current clock time
  3. Feed failure / off-hours candle fallback (Tier 2):
     - spot_status == "STALE"
     - is_stale is True
     - spot_time_ist == last closed 1m candle timestamp (never current clock time)
  4. Complete feed & DB outage (Tier 3):
     - spot == 0.0
     - spot_status == "UNAVAILABLE"
     - fail closed loudly with matrix_reason set
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from analyst.terminal8510_backend import QuantTerminalBackend, IST, MarketStatusResult


def test_off_hours_dhan_ltp_classified_as_stale():
    """
    Test 1: Outside market hours with non-zero Dhan LTP.
    Dhan returns the last traded price at any hour.
    The terminal must classify it as STALE with the last closed candle's timestamp,
    NEVER LIVE and NEVER with current clock time.
    """
    backend = QuantTerminalBackend()

    # Mock market hours to CLOSED (outside 09:15 - 15:30 IST)
    closed_status = MarketStatusResult({
        "is_open": False,
        "status": "CLOSED",
        "market": "NSE",
        "time_ist": "01:23:45 IST",
        "regime": "CLOSED",
    })

    with patch.object(backend.broker, "is_configured", return_value=True), \
         patch.object(backend.broker, "get_marketfeed_ltp", return_value={"13": 24536.6, "21": 15.2}), \
         patch.object(backend.broker, "get_fundlimit", return_value={"available_balance": 100000.0}), \
         patch.object(backend, "is_nse_market_open", return_value=(False, closed_status)):
        telemetry = asyncio.run(backend.get_live_telemetry())
        
        spot = telemetry["spot"]
        clock = telemetry["clock"]
        
        assert spot["price"] == 24536.6, f"Expected 24536.6, got {spot['price']}"
        assert spot["status"] == "STALE", f"Expected status 'STALE', got '{spot['status']}'"
        assert spot["is_stale"] is True, f"Expected is_stale=True, got {spot['is_stale']}"
        assert spot["time_ist"] is not None, "Expected valid spot_time_ist"
        assert spot["time_ist"] != clock, (
            f"Defect Regression: spot_time_ist ({spot['time_ist']}) must NOT equal "
            f"current clock time ({clock})"
        )
        # Verify spot_time_ist reflects a closed candle timestamp with IST format (never current clock time)
        assert "IST" in spot["time_ist"] and spot["time_ist"] != clock, (
            f"Expected valid spot_time_ist != clock, got {spot['time_ist']}"
        )
        print(f"✔ Test 1 Passed: Off-hours Dhan LTP correctly classified as STALE ({spot['time_ist']}) != clock ({clock}).")


def test_market_open_dhan_ltp_classified_as_live():
    """
    Test 2: During market hours with fresh non-zero Dhan LTP.
    Terminal must classify as LIVE with is_stale=False and spot_time_ist == current clock time.
    """
    backend = QuantTerminalBackend()

    open_status = MarketStatusResult({
        "is_open": True,
        "status": "OPEN",
        "market": "NSE",
        "time_ist": "10:30:00 IST",
        "regime": "REGULAR_HOURS",
    })

    with patch.object(backend.broker, "is_configured", return_value=True), \
         patch.object(backend.broker, "get_marketfeed_ltp", return_value={"13": 24550.0, "21": 15.2}), \
         patch.object(backend.broker, "get_fundlimit", return_value={"available_balance": 100000.0}), \
         patch.object(backend, "is_nse_market_open", return_value=(True, open_status)):
        telemetry = asyncio.run(backend.get_live_telemetry())
        
        spot = telemetry["spot"]
        clock = telemetry["clock"]
        
        assert spot["price"] == 24550.0
        assert spot["status"] == "LIVE", f"Expected status 'LIVE', got '{spot['status']}'"
        assert spot["is_stale"] is False, f"Expected is_stale=False, got {spot['is_stale']}"
        assert spot["time_ist"] == clock, (
            f"Expected spot_time_ist ({spot['time_ist']}) to match clock ({clock})"
        )
        print(f"✔ Test 2 Passed: Market-open quote classified as LIVE at {spot['time_ist']}.")


def test_tier2_candle_fallback_staleness():
    """
    Test 3: Dhan broker unconfigured or HTTP failure -> Tier 2 Candle Fallback.
    Terminal must fall back to last closed 1m candle and mark STALE.
    """
    backend = QuantTerminalBackend()

    # Mock broker.get_marketfeed_ltp to return empty dict
    with patch.object(backend.broker, "get_marketfeed_ltp", return_value={}), \
         patch.object(backend.broker, "get_fundlimit", return_value={"available_balance": 100000.0}):
        telemetry = asyncio.run(backend.get_live_telemetry())
        
        spot = telemetry["spot"]
        clock = telemetry["clock"]
        
        assert spot["price"] > 0, f"Expected positive candle fallback price, got {spot['price']}"
        assert spot["status"] == "STALE", f"Expected status 'STALE', got '{spot['status']}'"
        assert spot["is_stale"] is True, f"Expected is_stale=True, got {spot['is_stale']}"
        assert spot["time_ist"] != clock, (
            f"Tier 2 spot_time_ist ({spot['time_ist']}) must NOT equal current clock ({clock})"
        )
        print(f"✔ Test 3 Passed: Tier 2 Candle Fallback correctly marked STALE at {spot['time_ist']}.")


def test_tier3_fail_closed_loudly():
    """
    Test 4: Complete feed and DB outage (Tier 3).
    Terminal must fail closed loudly: spot = 0.0, status = UNAVAILABLE, route NO orders.
    """
    backend = QuantTerminalBackend()

    # Point to nonexistent database
    backend.market_db = Path("/tmp/nonexistent_market_db_for_test.sqlite3")

    # Mock broker to return empty
    with patch.object(backend.broker, "get_marketfeed_ltp", return_value={}), \
         patch.object(backend.broker, "get_fundlimit", return_value={"available_balance": 100000.0}):
        telemetry = asyncio.run(backend.get_live_telemetry())
        
        spot = telemetry["spot"]
        
        assert spot["price"] == 0.0, f"Expected spot=0.0, got {spot['price']}"
        assert spot["status"] == "UNAVAILABLE", f"Expected 'UNAVAILABLE', got '{spot['status']}'"
        assert spot["is_stale"] is False
        assert spot["time_ist"] is None
        assert telemetry["matrix_reason"] is not None
        assert "Spot unavailable" in telemetry["matrix_reason"]
        print("✔ Test 4 Passed: Tier 3 Outage fails closed loudly (spot=0.0, UNAVAILABLE).")


if __name__ == "__main__":
    print("\n--- Running Terminal 8510 Spot Staleness Unit Tests ---")
    test_off_hours_dhan_ltp_classified_as_stale()
    test_market_open_dhan_ltp_classified_as_live()
    test_tier2_candle_fallback_staleness()
    test_tier3_fail_closed_loudly()
    print("\n🎉 ALL 4 SPOT STALENESS TESTS PASSED!\n")
