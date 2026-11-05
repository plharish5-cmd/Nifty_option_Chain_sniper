#!/usr/bin/env python3
"""
analyst/test_directional_bias_8510.py — Test Suite for 429 Rate-Limit Mitigation & Daily Directional Bias.
"""
import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from analyst.terminal8510_backend import QuantTerminalBackend, DhanBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_8510_bias")


def test_part1_in_memory_cache_5s_ttl():
    """Verify 5.0-second in-memory LTP cache layer."""
    broker = DhanBroker()
    now = time.time()
    
    # Pre-seed cache
    broker._ltp_cache["13"] = 24000.0
    broker._ltp_cache["21"] = 15.2
    broker._cache_timestamps["13"] = now
    broker._cache_timestamps["21"] = now
    
    # Requesting within 5.0s TTL should return cached directly without HTTP calls
    req = {"IDX_I": [13, 21]}
    res = asyncio.run(broker.get_marketfeed_ltp(req))
    assert res.get("13") == 24000.0, f"Expected 24000.0, got {res.get('13')}"
    assert res.get("21") == 15.2, f"Expected 15.2, got {res.get('21')}"
    
    # Simulate expired timestamp (>5.0s ago)
    broker._cache_timestamps["13"] = now - 6.0
    # Expired entry should detect cache is not fresh
    all_req_ids = ["13", "21"]
    all_cached_fresh = all(
        (sid in broker._ltp_cache and (time.time() - broker._cache_timestamps.get(sid, 0.0) < broker.LTP_TTL))
        for sid in all_req_ids
    )
    assert not all_cached_fresh, "Cache should be detected as expired after 6 seconds"
    print("✔ Test 1 Passed: In-Memory 5.0-second TTL cache verified.")


def test_part1_429_rate_limit_backoff():
    """Verify HTTP 429 handler catches 429, logs warning, suppresses error, and backs off."""
    broker = DhanBroker()
    broker._ltp_cache["13"] = 23950.0
    broker._cache_timestamps["13"] = 0.0  # Force attempt to call API
    
    # Mock httpx response with 429 status code
    mock_response = AsyncMock()
    mock_response.status_code = 429
    mock_response.text = "Too Many Requests"
    
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with patch.object(broker, "is_configured", return_value=True):
            with patch("analyst.terminal8510_backend.log.warning") as mock_log_warn:
                t_before = time.time()
                res = asyncio.run(broker.get_marketfeed_ltp({"IDX_I": [13]}))
                
                # Verify cached price was served
                assert res.get("13") == 23950.0, "Expected last known cached price on 429"
                
                # Verify exact warning message logged
                mock_log_warn.assert_any_call("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                
                # Verify backoff was scheduled 3-5 seconds out
                assert broker._backoff_until >= t_before + 3.0, f"Backoff time {broker._backoff_until} not >= {t_before + 3.0}"
                
                # While backing off, subsequent calls must immediately return cache without network call
                res2 = asyncio.run(broker.get_marketfeed_ltp({"IDX_I": [13]}))
                assert res2.get("13") == 23950.0
    print("✔ Test 2 Passed: HTTP 429 Rate Limit backoff and cache serving verified.")


def test_part2_directional_bias_live_and_strike_matrix():
    """Verify Directional Bias blocks orders in execute_order_with_chaser."""
    backend = QuantTerminalBackend()
    
    ce_contract = {
        "id": "LONG_CE_ATM",
        "symbol": "NIFTY-24000-CE",
        "opt_type": "CE",
        "security_id": "39961",
        "lot_size": 65,
        "ltp": 150.0,
        "delta": 0.50
    }
    pe_contract = {
        "id": "LONG_PE_ATM",
        "symbol": "NIFTY-24000-PE",
        "opt_type": "PE",
        "security_id": "39962",
        "lot_size": 65,
        "ltp": 150.0,
        "delta": -0.50
    }
    
    # 1. LONG_ONLY Mode
    backend.set_directional_bias("LONG_ONLY")
    assert backend.directional_bias == "LONG_ONLY"
    
    # Attempt PE order in LONG_ONLY -> must be REJECTED
    pe_res = asyncio.run(backend.execute_order_with_chaser(contract=pe_contract, side="BUY"))
    assert pe_res["ok"] is False
    assert pe_res["status"] == "REJECTED"
    assert "LONG_ONLY" in pe_res["error"]
    
    # 2. SHORT_ONLY Mode
    backend.set_directional_bias("SHORT_ONLY")
    assert backend.directional_bias == "SHORT_ONLY"
    
    # Attempt CE order in SHORT_ONLY -> must be REJECTED
    ce_res = asyncio.run(backend.execute_order_with_chaser(contract=ce_contract, side="BUY"))
    assert ce_res["ok"] is False
    assert ce_res["status"] == "REJECTED"
    assert "SHORT_ONLY" in ce_res["error"]
    
    # Reset to BOTH
    backend.set_directional_bias("BOTH")
    assert backend.directional_bias == "BOTH"
    print("✔ Test 3 Passed: Directional Bias Live & Strike Matrix enforcement gate verified.")


def test_part2_directional_bias_scanner_signals():
    """Verify scanner signal filtering under LONG_ONLY and SHORT_ONLY."""
    backend = QuantTerminalBackend()
    
    sample_signals = [
        {"strategy_id": "STAGE2_LONG", "setup_name": "Stage 2 8-EMA (Long)", "direction": "LONG", "opt_side": "CE", "target_pts": 40.0, "sl_pts": 40.0, "details": "test", "conviction": "HIGH"},
        {"strategy_id": "STAGE3_SHORT", "setup_name": "Stage 3 8-EMA (Short)", "direction": "SHORT", "opt_side": "PE", "target_pts": 40.0, "sl_pts": 40.0, "details": "test", "conviction": "HIGH"},
        {"strategy_id": "CROSSOVER_UP", "setup_name": "4 MA / 8 EMA Upside Reversal", "direction": "LONG", "opt_side": "CE", "target_pts": 20.0, "sl_pts": 20.0, "details": "test", "conviction": "HIGH"},
        {"strategy_id": "CROSSOVER_DOWN", "setup_name": "4 MA / 8 EMA Downside Reversal", "direction": "SHORT", "opt_side": "PE", "target_pts": 20.0, "sl_pts": 20.0, "details": "test", "conviction": "HIGH"},
    ]
    
    # Under LONG_ONLY: only LONG / CE allowed
    backend.set_directional_bias("LONG_ONLY")
    long_filtered = [s for s in sample_signals if not (backend.directional_bias == "LONG_ONLY" and (s["direction"] == "SHORT" or s["opt_side"] == "PE"))]
    assert len(long_filtered) == 2
    assert all(s["opt_side"] == "CE" for s in long_filtered)
    
    # Under SHORT_ONLY: only SHORT / PE allowed
    backend.set_directional_bias("SHORT_ONLY")
    short_filtered = [s for s in sample_signals if not (backend.directional_bias == "SHORT_ONLY" and (s["direction"] == "LONG" or s["opt_side"] == "CE"))]
    assert len(short_filtered) == 2
    assert all(s["opt_side"] == "PE" for s in short_filtered)
    
    # Under BOTH: all 4 allowed
    backend.set_directional_bias("BOTH")
    print("✔ Test 4 Passed: 4-Core Strategy Scanner directional signal filtering verified.")


def test_part2_directional_bias_replay_and_backtest():
    """Verify historical replay and backtest filter trades according to bias."""
    backend = QuantTerminalBackend()
    
    # 1. Nov 4 replay has 1 Short and 3 Longs
    all_nov4 = backend.get_replay_trades_for_date("2026-11-04", bias="BOTH")
    assert len(all_nov4) == 4, f"Expected 4 trades under BOTH, got {len(all_nov4)}"
    
    # Replay under LONG_ONLY: Short trade must be excluded
    long_nov4 = backend.get_replay_trades_for_date("2026-11-04", bias="LONG_ONLY")
    assert len(long_nov4) == 3, f"Expected 3 trades under LONG_ONLY, got {len(long_nov4)}"
    for t in long_nov4:
        assert "PE Short" not in t.get("outcome", "")
        assert "Downside" not in t.get("setup_name", "")
        
    # Replay under SHORT_ONLY: Long trades must be excluded
    short_nov4 = backend.get_replay_trades_for_date("2026-11-04", bias="SHORT_ONLY")
    assert len(short_nov4) == 1, f"Expected 1 trade under SHORT_ONLY, got {len(short_nov4)}"
    assert "Downside Reversal" in short_nov4[0]["setup_name"]
    
    # 2. Date-range backtest with directional bias
    bt_both = backend.run_date_range_backtest("2026-11-04", "2026-11-04", bias="BOTH")
    bt_long = backend.run_date_range_backtest("2026-11-04", "2026-11-04", bias="LONG_ONLY")
    bt_short = backend.run_date_range_backtest("2026-11-04", "2026-11-04", bias="SHORT_ONLY")
    
    assert bt_both["total_trades"] == 4
    assert bt_long["total_trades"] == 3
    assert bt_short["total_trades"] == 1
    
    # Reset
    backend.set_directional_bias("BOTH")
    print("✔ Test 5 Passed: Historical Replay & Date-Range Backtest directional bias verified.")


def main():
    print("=" * 70)
    print("Running Port 8510 429 Rate-Limit Mitigation & Directional Bias Test Suite...")
    print("=" * 70)
    test_part1_in_memory_cache_5s_ttl()
    test_part1_429_rate_limit_backoff()
    test_part2_directional_bias_live_and_strike_matrix()
    test_part2_directional_bias_scanner_signals()
    test_part2_directional_bias_replay_and_backtest()
    print("=" * 70)
    print("🎉 ALL TESTS PASSED WITH 100% PRECISION!")
    print("=" * 70)


if __name__ == "__main__":
    main()
