"""Tests for Phase 2: 8 EMA Prototype option exit is 40 premium points (no delta scaling).

Acceptance criteria:
- Prototype buy filled at 250.00 -> target is 290.00, stop is 210.00, companion SL placed at 210.00 trigger.
- Non-prototype buy filled at 250.00 with delta 0.55 -> target is 250 + (40 * 0.55) = 272.00, unchanged.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend, round_to_tick  # noqa: E402


class FakeBroker:
    def __init__(self, configured: bool = True):
        self._configured = configured
        self.orders: List[Dict[str, Any]] = []
        self.placed_orders: List[Dict[str, Any]] = []
        self.cancelled_orders: List[str] = []
        self.modified_orders: List[Dict[str, Any]] = []
        self.quotes: Dict[str, float] = {}
        self.order_counter: int = 200

    def is_configured(self) -> bool:
        return self._configured

    def get_status(self) -> Dict[str, Any]:
        return {
            "connected": True,
            "dhan_connected": True,
            "token_expired": False,
            "token_valid_till": "2099-01-01T00:00:00",
            "status": "CONNECTED",
        }

    async def get_orders(self) -> List[Dict[str, Any]]:
        return list(self.orders)

    async def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        for o in self.orders:
            if str(o.get("orderId")) == str(order_id):
                return {"data": dict(o)}
        return {"data": {}}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        self.cancelled_orders.append(str(order_id))
        return {"ok": True, "order_id": order_id}

    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.order_counter += 1
        oid = f"DHAN_{self.order_counter}"
        record = dict(payload)
        record["orderId"] = oid
        otype = str(payload.get("orderType", "")).upper()
        if otype == "LIMIT":
            record["orderStatus"] = "TRADED"
            # Fill at exact limit price
            record["averageTradedPrice"] = float(payload.get("price") or 250.0)
            record["filledQty"] = int(payload.get("quantity") or 65)
        elif otype == "STOP_LOSS_LIMIT":
            record["orderStatus"] = "TRANSIT"
            record["triggerPrice"] = float(payload.get("triggerPrice") or 0.0)
            record["price"] = float(payload.get("price") or 0.0)
        else:
            record["orderStatus"] = "TRANSIT"

        self.orders.append(record)
        self.placed_orders.append(record)
        return {"ok": True, "order_id": oid, "data": {"orderId": oid}}

    async def modify_order(self, order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.modified_orders.append({"order_id": order_id, "payload": payload})
        return {"ok": True, "order_id": order_id}

    async def get_marketfeed_ltp(self, payload: Dict[str, Any]) -> Dict[str, float]:
        return dict(self.quotes)


def make_test_backend(tmp_path: Path, fake_broker: FakeBroker) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b._lock = None
    b.kill_switch_active = False
    b._real_money_enabled = True
    b.paper_execution_enabled = True
    b.directional_bias = "BOTH"
    b.cfg = {
        "execution_safety_v194": {
            "live_validation_status": "APPROVED",
            "typed_confirmation": "LIVE",
            "live_dry_run": False,
            "dry_run_exit_min_orders": 0,
            "live_entry_window_start": "00:00",
            "live_entry_window_end": "23:59",
            "max_live_trades_per_day": 0,
            "max_live_loss_rupees_per_day": 0.0,
        },
        "chasing": {
            "aggressive_limit_buffer_pts": 0.0,
            "auto_modify_interval_ms": 10,
            "sl_limit_buffer_pts": 1.0,
        },
        "starting_paper_capital": 100000.0,
    }
    b.algo_allowlist_cfg = {"enabled": False}
    b.algo_strategy_execution_allowed = lambda s: True
    b.algo_strike_selection = {}
    b.chasing_cfg = b.cfg.get("chasing", {})
    b.live_trades_today = lambda: 0
    b.live_pnl_today = lambda: 0.0
    b._dhan_credentials_live_ok = lambda: {"ok": True, "reason": "OK"}
    b.live_entry_window = lambda: {"open": True, "start": "09:15", "end": "15:20", "now": "10:00"}
    b.dry_run_gate_state = lambda: {"dry_run": False, "reason": "No dry run"}
    b.nse_holidays = []
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b._simulated_slippage_pts = lambda oid: 0.0
    b.config_path = tmp_path / "terminal8510_config.json"
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.broker = fake_broker
    b._position_close_locks = {}
    b.exit_failure_banner = None
    b.cached_telemetry = {"quotes": {}, "spot": {"price": 25000.0}}
    b.strategy_status = {}
    b.scanner_cfg = {}
    b.super_trail_cfg = {
        "initial_sl_pts": 20.0,
        "initial_target_pts": 25.0,
        "trailing_sl_enabled": True,
        "trailing_target_enabled": True,
    }
    b.off_hours_testing_active = lambda: True
    b._init_sqlite()
    return b


def test_prototype_buy_option_points_40(tmp_path: Path):
    """Prototype buy filled at 250.00 -> target is 290.00, stop is 210.00, companion SL placed at 210.00 trigger."""
    async def _run():
        broker = FakeBroker(configured=True)
        b = make_test_backend(tmp_path, broker)

        contract = {
            "symbol": "NIFTY 25000 CE",
            "security_id": "54321",
            "lot_size": 65,
            "ltp": 250.00,
            "delta": 0.55,
        }
        spot_ctx = {
            "strategy_id": "EMA8_PROTOTYPE_LONG",
            "spot_entry": 25000.0,
            "spot_target_level": 25040.0,
            "spot_stop_level": 24960.0,
            "spot_target_pts": 40.0,
            "spot_stop_pts": 40.0,
        }

        res = await b.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="8 EMA Prototype (Long)",
            strategy_id="EMA8_PROTOTYPE_LONG",
            target_pts=40.0,
            sl_pts=40.0,
            is_algo_trade=True,
            spot_context=spot_ctx,
            enforce_market_hours=False,
        )

        print("EXECUTION RESULT:", res)
        assert res["ok"] is True
        assert res["fill_price"] == 250.00
        assert res["opt_target_pts"] == 40.0
        assert res["opt_sl_pts"] == 40.0
        assert res["target_price"] == 290.00
        assert res["sl_price"] == 210.00

        # Verify companion SL-L placed with broker
        sl_orders = [o for o in broker.placed_orders if o.get("orderType") == "STOP_LOSS_LIMIT"]
        assert len(sl_orders) == 1
        assert sl_orders[0]["triggerPrice"] == 210.00
        assert sl_orders[0]["price"] == 209.00  # 210.00 - 1.0 buffer

        # Verify position in ledger
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            pos = conn.execute("SELECT * FROM positions WHERE strategy_id='EMA8_PROTOTYPE_LONG'").fetchone()
            assert pos is not None
            assert pos["entry_price"] == 250.00
            assert pos["target_price"] == 290.00
            assert pos["sl_price"] == 210.00

    asyncio.run(_run())


def test_non_prototype_buy_delta_scaled(tmp_path: Path):
    """Non-prototype buy filled at 250.00 with delta 0.55 -> target is 250 + (40 * 0.55) = 272.00, unchanged."""
    async def _run():
        broker = FakeBroker(configured=True)
        b = make_test_backend(tmp_path, broker)

        contract = {
            "symbol": "NIFTY 25000 CE",
            "security_id": "54322",
            "lot_size": 65,
            "ltp": 250.00,
            "delta": 0.55,
        }

        res = await b.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="Stage 2 8 EMA Support",
            strategy_id="STAGE_2_LONG",
            target_pts=40.0,
            sl_pts=40.0,
            is_algo_trade=True,
            enforce_market_hours=False,
        )

        print("EXECUTION RESULT:", res)
        assert res["ok"] is True
        assert res["fill_price"] == 250.00
        # 40 * 0.55 = 22.0
        assert res["opt_target_pts"] == 22.0
        assert res["opt_sl_pts"] == 22.0
        assert res["target_price"] == 272.00
        assert res["sl_price"] == 228.00

        # Companion SL-L trigger at 228.00
        sl_orders = [o for o in broker.placed_orders if o.get("orderType") == "STOP_LOSS_LIMIT"]
        assert len(sl_orders) == 1
        assert sl_orders[0]["triggerPrice"] == 228.00

    asyncio.run(_run())
