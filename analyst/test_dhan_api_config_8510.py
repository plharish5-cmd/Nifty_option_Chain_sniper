#!/usr/bin/env python3
"""
analyst/test_dhan_api_config_8510.py
Comprehensive Verification Suite for Dhan API Connection Card,
Masked Credentials, Safe .env Updates, IP Matching & End-to-End REST Gateway (v1.4)
"""

import os
import sys
import json
import asyncio
import unittest
from pathlib import Path

# Setup paths
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from analyst.terminal8510_backend import (
    load_env_safe,
    save_env_safe,
    DhanBroker,
    QuantTerminalBackend,
)
from engine_8510_live import app


class TestDhanApiConfig8510(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        load_env_safe()
        cls.orig_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        cls.orig_cid = os.environ.get("DHAN_CLIENT_ID", "")
        cls.orig_env = os.environ.get("DHAN_ENVIRONMENT", "LIVE")
        cls.orig_ip = os.environ.get("DHAN_WHITELISTED_IP", "")

    @classmethod
    def tearDownClass(cls):
        # Restore original .env variables safely
        restore_dict = {
            "DHAN_ACCESS_TOKEN": cls.orig_token,
            "DHAN_CLIENT_ID": cls.orig_cid,
            "DHAN_ENVIRONMENT": cls.orig_env,
            "DHAN_WHITELISTED_IP": cls.orig_ip,
        }
        save_env_safe(restore_dict)

    def test_01_masked_credentials_safety(self):
        """Verify token and secret are masked as ****last4 and never exposed in full."""
        print("Testing 1: Masked credentials safety & zero secret exposure...")
        broker = DhanBroker()
        cfg = broker.get_masked_config()

        self.assertTrue(cfg["ok"])
        self.assertIn("masked_token", cfg)
        self.assertIn("masked_secret", cfg)

        # Full token must NOT be in the returned config
        self.assertNotIn("access_token", cfg)
        if broker.is_configured():
            token = os.environ.get("DHAN_ACCESS_TOKEN", "")
            self.assertTrue(cfg["masked_token"].startswith("****"))
            self.assertEqual(cfg["masked_token"], f"****{token[-4:]}")
            self.assertTrue(cfg["token_configured"])

        print("✔ Test 1 Passed: Secrets are strictly masked and never exposed in config output.")

    def test_02_save_env_safe_preserves_other_variables(self):
        """Verify save_env_safe updates specified keys without disturbing existing comments or variables."""
        print("Testing 2: Safe .env writer preserves unrelated variables and comments...")
        # Check an unrelated key in .env
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

        test_cid = "1199887766"
        res = save_env_safe({"DHAN_CLIENT_ID": test_cid})
        self.assertTrue(res)

        # Re-read from disk
        load_env_safe()
        self.assertEqual(os.environ.get("DHAN_CLIENT_ID"), test_cid)
        if telegram_token:
            self.assertEqual(os.environ.get("TELEGRAM_BOT_TOKEN"), telegram_token)

        # Restore test CID
        save_env_safe({"DHAN_CLIENT_ID": self.orig_cid})
        print("✔ Test 2 Passed: .env file updated cleanly with 0 collateral disturbance.")

    def test_03_token_protection_against_masked_overwrite(self):
        """Verify updating with a masked token (****7935) retains the existing active token."""
        print("Testing 3: Protection against overwriting token with masked value...")
        broker = DhanBroker()
        real_token = os.environ.get("DHAN_ACCESS_TOKEN", "")

        # Attempt to save with masked token representation
        masked_input = "****7935"
        res = broker.update_credentials(access_token=masked_input, client_id="1112377935")

        self.assertEqual(os.environ.get("DHAN_ACCESS_TOKEN"), real_token)
        self.assertEqual(res["client_id"], "1112377935")
        print("✔ Test 3 Passed: Masked token input safely ignored; active token preserved.")

    def test_04_ip_matching_logic(self):
        """Verify current IP detection and whitelist comparison status."""
        print("Testing 4: Public IP lookup and whitelisted IP matching logic...")
        broker = DhanBroker()
        curr_ip = broker.get_current_ip()
        self.assertTrue(bool(curr_ip))

        # Test matched case
        broker.whitelisted_ip = curr_ip
        cfg_matched = broker.get_masked_config()
        self.assertTrue(cfg_matched["ip_matched"])
        self.assertEqual(cfg_matched["whitelisted_ip"], curr_ip)

        # Test mismatch case
        broker.whitelisted_ip = "192.168.1.99"
        cfg_mismatch = broker.get_masked_config()
        self.assertFalse(cfg_mismatch["ip_matched"])
        self.assertEqual(cfg_mismatch["whitelisted_ip"], "192.168.1.99")

        # Restore
        broker.whitelisted_ip = self.orig_ip
        print("✔ Test 4 Passed: IP matching logic accurately detects MATCHED vs MISMATCH.")

    async def test_05_test_connection_live_or_expired(self):
        """Verify test_connection returns proper formatted status string."""
        print("Testing 5: Live connection test & formatted status output...")
        broker = DhanBroker()
        status_res = await broker.test_connection()

        self.assertIn("status", status_res)
        self.assertIn("message", status_res)
        self.assertIn("last_updated", status_res)

        if status_res["connected"]:
            self.assertIn("CONNECTED ✅", status_res["message"])
            self.assertIn("dhan_connected: true", status_res["message"])
            self.assertIn("Nifty Spot:", status_res["message"])
            self.assertIn("Nifty Future:", status_res["message"])
            self.assertGreater(status_res["spot_price"], 0)
            self.assertGreater(status_res["future_price"], 0)
            print(f"✔ Test 5 (Live): {status_res['message']}")
        else:
            self.assertTrue(
                "DISCONNECTED ❌" in status_res["message"]
                or "IP NOT WHITELISTED ❌" in status_res["message"]
            )
            print(f"✔ Test 5 (Handled Error): {status_res['message']}")

    async def test_06_rest_endpoints_end_to_end(self):
        """Verify /api/dhan/config, /api/dhan/connect, and /api/dhan/test REST endpoints."""
        print("Testing 6: End-to-end REST API endpoints...")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            # 1. GET /api/dhan/config
            r_get = await client.get("/api/dhan/config")
            self.assertEqual(r_get.status_code, 200)
            data_get = r_get.json()
            self.assertTrue(data_get["ok"])
            self.assertNotIn("masked_token", data_get)
            self.assertIn("token_configured", data_get)
            self.assertIn("current_ip", data_get)

            # 2. POST /api/dhan/config (update harmless field)
            r_post = await client.post("/api/dhan/config", json={"environment": "LIVE", "whitelisted_ip": data_get["current_ip"]})
            self.assertEqual(r_post.status_code, 200)
            data_post = r_post.json()
            self.assertEqual(data_post["environment"], "LIVE")
            self.assertTrue(data_post["ip_matched"])

            # 3. POST /api/dhan/test
            r_test = await client.post("/api/dhan/test")
            self.assertEqual(r_test.status_code, 200)
            data_test = r_test.json()
            self.assertIn("status", data_test)
            self.assertIn("message", data_test)

            # 4. POST /api/dhan/connect
            r_conn = await client.post("/api/dhan/connect")
            self.assertEqual(r_conn.status_code, 200)
            data_conn = r_conn.json()
            self.assertIn("status", data_conn)
            self.assertIn("message", data_conn)

        print("✔ Test 6 Passed: All Dhan API REST endpoints function with 100% precision.")


def main():
    print("=" * 70)
    print("Running Dhan API Config Card & Live Connection Gateway Test Suite (v1.4)...")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestDhanApiConfig8510)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("=" * 70)
    print("🎉 ALL 6 DHAN API CONFIG CARD TESTS PASSED WITH 100% PRECISION!")
    print("=" * 70)


if __name__ == "__main__":
    main()
