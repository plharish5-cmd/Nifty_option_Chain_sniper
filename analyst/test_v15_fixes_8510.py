#!/usr/bin/env python3
"""
analyst/test_v15_fixes_8510.py
Verification Test Suite for Master v1.5 Fixes:
  - Test 1: Dhan Persistence & Masking (Disk save to terminal8510_config.json, auto-load on startup, ****1234 masking, 10s timeout)
  - Test 2: Contract Symbol Expiry Formatting (Full expiry date format globally: 09 Nov 2026 — NIFTY 24300 CE)
  - Test 3: Market Hours Square-Off Gate (Blocks live and paper orders/square-off outside market hours, testing override)
  - Test 4: Position Source & Strategy Tracking (Persistence and API exposure of source and strategy_setup)
"""
import asyncio
import os
import sys
import json
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Setup paths
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from analyst.terminal8510_backend import (
    load_env_safe,
    save_env_safe,
    format_contract_symbol,
    get_contract_structured_fields,
    is_nse_market_open,
    decode_token_expiry,
    DhanBroker,
    QuantTerminalBackend,
)
from engine_8510_live import app

CONFIG_PATH = ROOT / "analyst" / "terminal8510_config.json"


class TestV15Fixes8510(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        load_env_safe()
        cls.orig_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        cls.orig_cid = os.environ.get("DHAN_CLIENT_ID", "")
        cls.orig_env = os.environ.get("DHAN_ENVIRONMENT", "LIVE")
        cls.orig_ip = os.environ.get("DHAN_WHITELISTED_IP", "")
        cls.orig_expiry = os.environ.get("TOKEN_EXPIRY_UTC", "")

    @classmethod
    def tearDownClass(cls):
        restore_dict = {
            "DHAN_ACCESS_TOKEN": cls.orig_token,
            "DHAN_CLIENT_ID": cls.orig_cid,
            "DHAN_ENVIRONMENT": cls.orig_env,
            "DHAN_WHITELISTED_IP": cls.orig_ip,
            "TOKEN_EXPIRY_UTC": cls.orig_expiry,
        }
        save_env_safe(restore_dict)

    # ── TEST 1: DHAN PERSISTENCE & MASKING ──────────────────────────────────
    async def test_01_dhan_persistence_and_masking(self):
        """
        Verify:
        - Saving Dhan config writes permanently to terminal8510_config.json and .env
        - Token is masked with ****last4 (e.g. ****1234)
        - Non-expired token auto-loads on startup without re-prompt
        - Enforces strict 10s connection timeout and returns retry hint on failure
        """
        backend = QuantTerminalBackend()
        test_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummyPayload.secret1234"
        test_cid = os.environ.get("DHAN_CLIENT_ID", "")  # Fixed secure backend ID; UI cannot replace it

        res = backend.update_dhan_config({
            "client_id": test_cid,
            "access_token": test_token,
            "api_key": "KEY8510",
            "api_secret": "SECRET8510",
            "environment": "LIVE"
        })
        self.assertTrue(res["ok"])

        # 1. Verify written to disk config file
        self.assertTrue(CONFIG_PATH.exists())
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            disk_cfg = json.load(f)
        self.assertEqual(disk_cfg.get("DHAN_CLIENT_ID"), test_cid)
        self.assertEqual(disk_cfg.get("DHAN_ACCESS_TOKEN"), test_token)
        self.assertIn("TOKEN_EXPIRY_UTC", disk_cfg)

        # 2. Verify status endpoint auto-load metadata & masking
        status = backend.get_dhan_status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["configured"])
        self.assertNotIn("masked_token", status)
        self.assertNotIn(test_token, json.dumps(status))
        self.assertIn("Auto-loaded saved Dhan API", status["auto_load_log"])
        self.assertNotIn("1234", status["status_text"])
        self.assertIn("CONNECTED", status["status_text"])

        # 3. Verify a fresh broker instance automatically loads saved credentials from disk
        fresh_broker = DhanBroker()
        self.assertTrue(fresh_broker.is_configured())
        self.assertEqual(fresh_broker.get_masked_config()["masked_token"], "****1234")

        # 4. Verify 10-second timeout on live connection test (does not hang indefinitely)
        start_t = asyncio.get_event_loop().time()
        conn_test = await backend.test_dhan_connection()
        elapsed = asyncio.get_event_loop().time() - start_t
        self.assertLess(elapsed, 11.0, "Connection test took longer than 10-second timeout")
        self.assertIn("message", conn_test)

    # ── TEST 2: CONTRACT SYMBOL EXPIRY FORMATTING ────────────────────────────
    def test_02_contract_symbol_formatting(self):
        """
        Verify:
        - Contract symbol formatter outputs exact standard: 09 Nov 2026 — NIFTY 24300 CE
        - Exposes 3 structured fields: Expiry Date, Symbol, Strike
        - Legacy raw month symbols (NIFTY-Nov2026-24300-CE) are parsed and formatted cleanly
        """
        sym1 = format_contract_symbol(expiry="2026-11-09", strike=24300, opt_type="CE", underlying="NIFTY")
        self.assertEqual(sym1, "09 Nov 2026 — NIFTY 24300 CE")

        sym2 = format_contract_symbol(expiry="2026-11-09", strike=24500, opt_type="PE", underlying="NIFTY")
        self.assertEqual(sym2, "09 Nov 2026 — NIFTY 24500 PE")

        # Parsing from raw symbol
        sym3 = format_contract_symbol(raw_symbol="NIFTY-Nov2026-23800-CE", default_expiry="2026-11-09")
        self.assertEqual(sym3, "09 Nov 2026 — NIFTY 23800 CE")

        # Structured fields
        struct = get_contract_structured_fields("09 Nov 2026 — NIFTY 24300 CE")
        self.assertIn("09 Nov 2026", struct["expiry_display"])
        self.assertIn("Monday", struct["expiry_display"])
        self.assertEqual(struct["symbol"], "NIFTY")
        self.assertEqual(struct["strike_display"], "24300 CE")

        # Priority matrix telemetry format check
        backend = QuantTerminalBackend()
        for c in backend.priority_contracts:
            formatted = format_contract_symbol(
                expiry=c.get("expiry", "2026-11-09"),
                strike=c.get("strike", 24300),
                opt_type=c.get("opt_type", "CE"),
                underlying="NIFTY"
            )
            self.assertTrue(formatted.startswith("09 Nov 2026 — NIFTY "))

    # ── TEST 3: MARKET HOURS SQUARE-OFF GATE ────────────────────────────────
    def test_03_market_hours_validator(self):
        """
        Verify:
        - NSE Market Hours: Mon-Fri 09:15:00 to 15:30:00 IST
        - Rejects weekends (weekday 5 or 6), before 09:15, after 15:30, or holidays
        """
        # Thursday 11:30 IST -> OPEN
        dt_open = datetime(2026, 11, 5, 11, 30, 0)
        res_open = is_nse_market_open(dt_open)
        self.assertTrue(res_open["is_open"])
        self.assertEqual(res_open["status"], "OPEN")

        # Thursday 20:00 IST (8:00 PM) -> CLOSED (POST_CLOSE)
        dt_post = datetime(2026, 11, 5, 20, 0, 0)
        res_post = is_nse_market_open(dt_post)
        self.assertFalse(res_post["is_open"])
        self.assertIn(res_post["status"], ("POST_CLOSE", "CLOSED"))

        # Saturday 10:00 IST -> WEEKEND CLOSED
        dt_sat = datetime(2026, 11, 7, 10, 0, 0)
        res_sat = is_nse_market_open(dt_sat)
        self.assertFalse(res_sat["is_open"])
        self.assertEqual(res_sat["status"], "WEEKEND_CLOSED")

    async def test_03b_square_off_gate_enforcement(self):
        """
        Verify:
        - Square-off & order endpoints block execution outside market hours
          with the exact message '❌ Market Closed — Cannot Square-Off Now'
        - Setting allow_off_hours_paper_testing=True permits Paper Square-Off
        """
        backend = QuantTerminalBackend()
        backend.paper_execution_enabled = True
        backend.algo_only_execution = False  # Explicit fixture for manual paper-position / exit tests.
        backend.execution_mode = 'PAPER_ONLY'

        # Temporarily enable testing override to create test position in paper ledger
        backend.set_off_hours_paper_testing(True)

        contract = {
            "id": "LONG_CE_ATM",
            "symbol": "09 Nov 2026 — NIFTY 24300 CE",
            "opt_type": "CE",
            "strike": 24300,
            "ltp": 80.0,
            "security_id": 39951,
            "lot_size": backend.get_nifty_lot_size()
        }
        order_res = await backend.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="Test Setup",
            target_pts=40.0,
            sl_pts=20.0,
            source="AUTO ALGO 🤖",
            strategy_setup="Stage 2 Test Fixture", is_algo_trade=True
        )
        self.assertTrue(order_res["ok"])

        pos_list = backend.get_active_positions()
        self.assertTrue(len(pos_list) > 0, "Failed to create test position")
        pos_id = pos_list[0]["position_id"]

        # Ensure market is treated as closed (post-market or weekend)
        current_m = backend.is_nse_market_open()

        if not current_m["is_open"]:
            # With override OFF: Square-off must be BLOCKED
            backend.set_off_hours_paper_testing(False)
            sq_res = backend.modify_open_position(pos_id, action="CLOSE")
            self.assertFalse(sq_res["ok"])
            self.assertIn("❌ Market Closed — Cannot Square-Off Now", sq_res["error"])
            self.assertIn("Your position will remain OPEN till next market open.", sq_res["error"])

            # Also verify via alias "SQUARE_OFF"
            sq_res_alias = backend.modify_open_position(pos_id, action="SQUARE_OFF")
            self.assertFalse(sq_res_alias["ok"])
            self.assertIn("❌ Market Closed — Cannot Square-Off Now", sq_res_alias["error"])

            # With override ON: Paper square-off is permitted
            backend.set_off_hours_paper_testing(True)
            sq_res_override = backend.modify_open_position(pos_id, action="CLOSE")
            self.assertTrue(sq_res_override["ok"])
            self.assertEqual(sq_res_override["status"], "CLOSED")
            self.assertIn("warning", sq_res_override)

            # Reset override back to OFF
            backend.set_off_hours_paper_testing(False)

    # ── TEST 4: POSITION SOURCE & STRATEGY TRACKING ──────────────────────────
    async def test_04_position_source_and_strategy_tracking(self):
        """
        Verify:
        - Source (placed_by) and strategy_setup columns are stored in SQLite
        - Retrieved in get_active_positions() and get_order_book()
        - Supported sources: AUTO ALGO 🤖, MANUAL MATRIX ✋, MANUAL ROUTER ✋, MANUAL TAB 6 ✋
        """
        backend = QuantTerminalBackend()
        backend.paper_execution_enabled = True
        backend.algo_only_execution = False  # Explicit fixture for manual paper-position / exit tests.
        backend.execution_mode = 'PAPER_ONLY'
        backend.set_off_hours_paper_testing(True)

        contract = {
            "id": "LONG_CE_TEST_SRC",
            "symbol": "09 Nov 2026 — NIFTY 24400 CE",
            "opt_type": "CE",
            "strike": 24400,
            "ltp": 95.0,
            "security_id": 39955,
            "lot_size": backend.get_nifty_lot_size()
        }

        # 1. Place Auto-Algo order
        order_res1 = await backend.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="Stage 2 Scanner",
            source="AUTO ALGO 🤖",
            strategy_setup="Stage 2 8 EMA Touch", is_algo_trade=True
        )
        self.assertTrue(order_res1["ok"])
        self.assertEqual(order_res1["source"], "AUTO ALGO 🤖")
        self.assertEqual(order_res1["strategy_setup"], "Stage 2 8 EMA Touch")

        prior_order_count = len(backend.get_order_book())
        # 2. A manual router order is refused permanently
        order_res2 = await backend.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="Manual Router Trade",
            source="MANUAL ROUTER ✋",
            strategy_setup="Manual — No Strategy"
        )
        self.assertFalse(order_res2["ok"])
        self.assertTrue(order_res2["algo_only_blocked"])

        # 3. Check positions return exact source & strategy
        positions = backend.get_active_positions()
        sources_found = [p.get("source") or p.get("placed_by") for p in positions]
        strats_found = [p.get("strategy_setup") for p in positions]

        self.assertIn("AUTO ALGO 🤖", sources_found)
        # Historical manual rows are retained, not erased by the safety upgrade.
        self.assertIn("Stage 2 8 EMA Touch", strats_found)

        # 4. Check order book returns exact source & strategy
        orders = backend.get_order_book()
        order_sources = [o.get("source") or o.get("placed_by") for o in orders]
        order_strats = [o.get("strategy_setup") for o in orders]

        self.assertIn("AUTO ALGO 🤖", order_sources)
        self.assertEqual(len(orders), prior_order_count)
        self.assertIn("Stage 2 8 EMA Touch", order_strats)

        # Clean up
        backend.set_off_hours_paper_testing(False)

    # ── TEST 5: REST API ENDPOINTS VIA ASYNC HTTP CLIENT ─────────────────────
    async def test_05_rest_api_endpoints_v15(self):
        """
        Verify new REST endpoints:
        - GET  /api/dhan/status
        - POST /api/dhan/logout
        - GET  /api/market/status
        - POST /api/settings/testing_override
        - POST /api/positions/squareoff (and /api/paper/squareoff)
        """
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Dhan status endpoint
            r_dhan = await client.get("/api/dhan/status")
            self.assertEqual(r_dhan.status_code, 200)
            data_dhan = r_dhan.json()
            self.assertTrue(data_dhan.get("ok"))
            self.assertNotIn("masked_token", data_dhan)

            # 2. Market status endpoint
            r_m = await client.get("/api/market/status")
            self.assertEqual(r_m.status_code, 200)
            data_m = r_m.json()
            self.assertIn("is_open", data_m)
            self.assertIn("status", data_m)
            self.assertIn("time_ist", data_m)

            # 3. Testing override endpoint
            r_ov = await client.post("/api/settings/testing_override", json={"allow_off_hours_paper_testing": True})
            self.assertEqual(r_ov.status_code, 200)
            self.assertTrue(r_ov.json().get("allow_off_hours_paper_testing"))

            # Toggle back to False
            r_ov2 = await client.post("/api/settings/testing_override", json={"allow_off_hours_paper_testing": False})
            self.assertEqual(r_ov2.status_code, 200)
            self.assertFalse(r_ov2.json().get("allow_off_hours_paper_testing"))

    # ── TEST 6: DYNAMIC LOT SIZE & 1-LOT HARD CAP ─────────────────────────────
    def test_06_dynamic_lot_size_and_1lot_cap(self):
        """
        Verify Claude Correction 1:
        - NIFTY lot size is dynamically parsed from Dhan master CSV (65, not 75)
        - validate_1lot_cap permits exactly 1 lot and rejects > 1 lot
        """
        from algo_execution_engine.order_gateway import validate_1lot_cap, get_dynamic_nifty_lot_size

        backend = QuantTerminalBackend()
        lot_size = backend.get_nifty_lot_size()
        self.assertEqual(lot_size, 65, f"Expected dynamic lot size 65, got {lot_size}")

        dynamic_lot_engine = get_dynamic_nifty_lot_size()
        self.assertEqual(dynamic_lot_engine, 65)

        # 1 Lot passes
        self.assertEqual(validate_1lot_cap(65, 65), 65)

        # > 1 Lot strictly clamped to 1 Lot (65)
        self.assertEqual(validate_1lot_cap(130, 65), 65)

        # <= 0 qty strictly raises ValueError
        with self.assertRaises(ValueError):
            validate_1lot_cap(0, 65)

    # ── TEST 7: EOD 15:20:00 IST SQUARE-OFF ──────────────────────────────────
    async def test_07_eod_square_off_1520(self):
        """
        Verify Claude Correction 2:
        - eod_square_off_time reads dynamically from config (15:20)
        - Square-off is NOT triggered before 15:20 (e.g. 14:30)
        - All open intraday positions are squared off at or after 15:20:00 IST
        """
        backend = QuantTerminalBackend()
        self.assertEqual(backend.cfg.get("eod_square_off_time"), "15:20")

        # Create an open paper position with testing override
        backend.paper_execution_enabled = True
        backend.algo_only_execution = False  # Explicit fixture for manual paper-position / exit tests.
        backend.execution_mode = 'PAPER_ONLY'
        backend.set_off_hours_paper_testing(True)

        contract = {
            "id": "LONG_CE_EOD",
            "symbol": "09 Nov 2026 — NIFTY 24300 CE",
            "opt_type": "CE",
            "strike": 24300,
            "ltp": 110.0,
            "security_id": 39951,
            "lot_size": backend.get_nifty_lot_size()
        }
        res = await backend.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="EOD Test",
            source="AUTO ALGO 🤖",
            strategy_setup="Stage 2 8 EMA Touch", is_algo_trade=True
        )
        self.assertTrue(res["ok"])
        self.assertGreater(len(backend.get_active_positions()), 0)

        # 1. Simulate check at 14:30:00 IST -> should NOT square off
        dt_1430 = datetime(2026, 11, 5, 14, 30, 0)
        closed_early = backend.check_eod_square_off(dt=dt_1430)
        self.assertEqual(len(closed_early), 0)
        self.assertGreater(len(backend.get_active_positions()), 0)

        # 2. Simulate check at 15:20:01 IST -> MUST square off open positions
        dt_1520 = datetime(2026, 11, 5, 15, 20, 1)
        closed_eod = backend.check_eod_square_off(dt=dt_1520)
        self.assertGreater(len(closed_eod), 0)
        self.assertEqual(len(backend.get_active_positions()), 0)

        # Reset override
        backend.set_off_hours_paper_testing(False)


if __name__ == "__main__":
    unittest.main()
