"""Tests for live exit order execution and broker close path (Phase 1).

Acceptance criteria:
1. Spot target hit live -> companion SL cancelled, exactly one broker SELL placed, ledger closed at fake fill price.
2. Opposite 15m close live -> companion SL cancelled, exactly one broker SELL placed, ledger closed at fake fill price.
3. Companion SL already filled -> zero broker SELL placed, ledger closed with BROKER_SL_FILLED.
4. Two exit triggers in same pass -> exactly one broker SELL placed, no double exit.
5. SELL rejected -> position marked EXIT_FAILED, protective stop re-placed, banner flag set, no silent ledger close.
6. Paper position -> zero broker calls, wallet balance updated as before.
7. Missing quote -> position stays open, no zero-P&L close.
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
    """Mock broker simulating Dhan HQ API v2 without real network or account calls."""

    def __init__(self, configured: bool = True):
        self._configured = configured
        self.orders: List[Dict[str, Any]] = []
        self.placed_orders: List[Dict[str, Any]] = []
        self.cancelled_orders: List[str] = []
        self.modified_orders: List[Dict[str, Any]] = []
        self.reject_sell: bool = False
        self.reject_sell_reason: str = "RMS: Margin shortfall / Order rejected"
        self.quotes: Dict[str, float] = {}
        self.order_counter: int = 100

    def is_configured(self) -> bool:
        return self._configured

    async def get_orders(self) -> List[Dict[str, Any]]:
        return list(self.orders)

    async def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        for o in self.orders:
            if str(o.get("orderId")) == str(order_id):
                return {"data": dict(o)}
        return {"data": {}}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        self.cancelled_orders.append(str(order_id))
        for o in self.orders:
            if str(o.get("orderId")) == str(order_id):
                o["orderStatus"] = "CANCELLED"
        return {"ok": True, "order_id": order_id}

    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ttype = str(payload.get("transactionType", "")).upper()
        otype = str(payload.get("orderType", "")).upper()
        if ttype == "SELL" and otype == "LIMIT" and self.reject_sell:
            return {"ok": False, "error": self.reject_sell_reason, "rejection_reason": self.reject_sell_reason}

        self.order_counter += 1
        oid = f"DHAN_{self.order_counter}"
        record = dict(payload)
        record["orderId"] = oid
        if otype == "LIMIT":
            record["orderStatus"] = "TRADED"
            record["averageTradedPrice"] = float(payload.get("price") or 0.0)
            record["filledQty"] = int(payload.get("quantity") or 0)
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
        for o in self.orders:
            if str(o.get("orderId")) == str(order_id):
                o.update(payload)
        return {"ok": True, "order_id": order_id}

    async def get_marketfeed_ltp(self, payload: Dict[str, Any]) -> Dict[str, float]:
        return dict(self.quotes)


def make_test_backend(tmp_path: Path, fake_broker: FakeBroker) -> QuantTerminalBackend:
    """Instantiate an isolated QuantTerminalBackend in tmp_path with a fake broker."""
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {
        "execution_safety_v194": {
            "live_validation_status": "APPROVED",
            "typed_confirmation": "LIVE",
            "live_dry_run": False,
            "dry_run_exit_min_orders": 0,
            "live_entry_window_start": "09:15",
            "live_entry_window_end": "15:20",
            "max_live_trades_per_day": 0,
            "max_live_loss_rupees_per_day": 0.0,
        },
        "chasing": {
            "aggressive_limit_buffer_pts": 2.0,
            "auto_modify_interval_ms": 10,
        },
        "starting_paper_capital": 100000.0,
    }
    b.config_path = tmp_path / "terminal8510_config.json"
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.broker = fake_broker
    b._position_close_locks = {}
    b.exit_failure_banner = None
    b.cached_telemetry = {"quotes": {}, "spot": {"price": 25000.0}}
    b.strategy_status = {}
    b.scanner_cfg = {}
    b.super_trail_cfg = {}
    b._init_sqlite()
    return b


def insert_test_position(
    b: QuantTerminalBackend,
    pos_id: str,
    mode: str = "LIVE",
    symbol: str = "NIFTY 25000 CE",
    sec_id: str = "54321",
    entry_price: float = 200.0,
    qty: int = 65,
    sl_order_id: str = "SL_INIT_101",
    strategy_id: str = "EMA8_PROTOTYPE_LONG",
    spot_entry: float = 25000.0,
    spot_target: float = 25040.0,
    spot_stop: float = 24960.0,
    status: str = "OPEN",
) -> Dict[str, Any]:
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO positions (
                position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                current_ltp, target_price, sl_price, exit_price, exit_time, duration,
                gross_pnl, net_pnl, status, mode, strategy_id, spot_entry, spot_target_level,
                spot_stop_level, initial_target, initial_sl, current_trailing_sl, sl_order_id
            ) VALUES (?, ?, ?, 'BUY', 1, ?, ?, '2026-11-11 10:00:00 IST',
                     ?, ?, ?, NULL, NULL, '', 0.0, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pos_id, symbol, sec_id, qty, entry_price, entry_price,
                entry_price + 40.0, entry_price - 40.0, status, mode,
                strategy_id, spot_entry, spot_target, spot_stop,
                entry_price + 40.0, entry_price - 40.0, entry_price - 40.0, sl_order_id,
            ),
        )
        conn.commit()
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone()
        return dict(row)


def test_1_spot_target_hit_live(tmp_path: Path):
    """1. Spot target hit live -> companion SL cancelled, exactly one broker SELL placed, ledger closed at fake fill price."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.orders.append({
            "orderId": "SL_INIT_101",
            "orderStatus": "TRANSIT",
            "transactionType": "SELL",
            "orderType": "STOP_LOSS_LIMIT",
            "quantity": 65,
        })
        broker.quotes["54321"] = 230.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_LIVE_1", mode="LIVE", sl_order_id="SL_INIT_101")

        # Spot price moves to 25045.0 (hits spot target of 25040.0)
        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25045.0)
        assert res.get("ok") is True

        # 1. Companion SL cancelled
        assert "SL_INIT_101" in broker.cancelled_orders

        # 2. Exactly one broker SELL placed (LIMIT)
        limit_sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]
        assert len(limit_sells) == 1
        sell_order = limit_sells[0]
        assert sell_order["quantity"] == 65
        assert sell_order["price"] == pytest.approx(228.0, abs=0.01)  # 230.0 - 2.0 buffer

        # 3. Ledger closed at fake fill price
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_LIVE_1'").fetchone()
            assert p["status"] == "CLOSED"
            assert p["exit_reason"] == "SPOT_TARGET_HIT"
            assert p["exit_price"] == pytest.approx(228.0, abs=0.01)
            assert p["spot_exit"] == 25045.0

    asyncio.run(_run())


def test_2_opposite_15m_close_live(tmp_path: Path):
    """2. Opposite 15m close live -> companion SL cancelled, exactly one broker SELL placed, ledger closed at fake fill price."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.orders.append({
            "orderId": "SL_INIT_102",
            "orderStatus": "TRANSIT",
            "transactionType": "SELL",
            "orderType": "STOP_LOSS_LIMIT",
            "quantity": 65,
        })
        broker.quotes["54321"] = 195.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_LIVE_2", mode="LIVE", sl_order_id="SL_INIT_102")

        # Square-off triggered by opposite close
        res = await b._close_position_with_broker(pos, "OPPOSITE_8EMA_CLOSE", spot_exit=24950.0)
        assert res.get("ok") is True

        assert "SL_INIT_102" in broker.cancelled_orders
        limit_sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]
        assert len(limit_sells) == 1
        assert limit_sells[0]["price"] == pytest.approx(193.0, abs=0.01)

        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_LIVE_2'").fetchone()
            assert p["status"] == "CLOSED"
            assert p["exit_reason"] == "OPPOSITE_8EMA_CLOSE"
            assert p["exit_price"] == pytest.approx(193.0, abs=0.01)

    asyncio.run(_run())


def test_3_companion_sl_already_filled(tmp_path: Path):
    """3. Companion SL already filled -> zero broker SELL placed, ledger closed with BROKER_SL_FILLED."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.orders.append({
            "orderId": "SL_INIT_103",
            "orderStatus": "TRADED",
            "transactionType": "SELL",
            "orderType": "STOP_LOSS_LIMIT",
            "quantity": 65,
            "averageTradedPrice": 160.0,
            "filledQty": 65,
        })
        broker.quotes["54321"] = 158.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_LIVE_3", mode="LIVE", sl_order_id="SL_INIT_103")

        # Trigger exit check
        res = await b._close_position_with_broker(pos, "SPOT_SL_HIT", spot_exit=24955.0)
        assert res.get("ok") is True

        # Zero broker SELL limit orders placed
        limit_sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]
        assert len(limit_sells) == 0

        # Ledger closed with BROKER_SL_FILLED at fill price 160.0
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_LIVE_3'").fetchone()
            assert p["status"] == "CLOSED"
            assert p["exit_reason"] == "BROKER_SL_FILLED"
            assert p["exit_price"] == 160.0

    asyncio.run(_run())


def test_4_two_exit_triggers_in_same_pass_no_double_exit(tmp_path: Path):
    """4. Two exit triggers in same pass -> exactly one broker SELL placed, no double exit."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.orders.append({
            "orderId": "SL_INIT_104",
            "orderStatus": "TRANSIT",
            "transactionType": "SELL",
            "orderType": "STOP_LOSS_LIMIT",
            "quantity": 65,
        })
        broker.quotes["54321"] = 240.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_LIVE_4", mode="LIVE", sl_order_id="SL_INIT_104")

        # Dispatch two exit tasks concurrently
        r1, r2 = await asyncio.gather(
            b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25045.0),
            b._close_position_with_broker(pos, "OPPOSITE_8EMA_CLOSE", spot_exit=24950.0),
        )

        # Exactly one SELL placed
        limit_sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]
        assert len(limit_sells) == 1

        # Position is closed
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_LIVE_4'").fetchone()
            assert p["status"] == "CLOSED"

    asyncio.run(_run())


def test_5_sell_rejected_marks_exit_failed_and_replaces_protective_sl(tmp_path: Path):
    """5. SELL rejected -> position marked EXIT_FAILED, protective stop re-placed, banner flag set, no silent ledger close."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.reject_sell = True
        broker.orders.append({
            "orderId": "SL_INIT_105",
            "orderStatus": "TRANSIT",
            "transactionType": "SELL",
            "orderType": "STOP_LOSS_LIMIT",
            "quantity": 65,
        })
        broker.quotes["54321"] = 210.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_LIVE_5", mode="LIVE", sl_order_id="SL_INIT_105")

        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25045.0)
        assert res.get("ok") is False
        assert res.get("status") == "EXIT_FAILED"

        # Banner flag set
        assert b.exit_failure_banner is not None
        assert b.exit_failure_banner["position_id"] == "POS_LIVE_5"
        assert "Margin shortfall" in b.exit_failure_banner["error"]

        # Protective SL-L re-placed
        protective_sl = [o for o in broker.placed_orders if o.get("orderType") == "STOP_LOSS_LIMIT"]
        assert len(protective_sl) >= 1
        new_sl = protective_sl[-1]
        assert new_sl["quantity"] == 65

        # Position status is EXIT_FAILED, NOT closed
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_LIVE_5'").fetchone()
            assert p["status"] == "EXIT_FAILED"
            assert "EXIT_FAILED" in p["exit_reason"]

    asyncio.run(_run())


def test_6_paper_position_zero_broker_calls_wallet_updated(tmp_path: Path):
    """6. Paper position -> zero broker calls, wallet balance updated as before."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.quotes["54321"] = 220.0

        b = make_test_backend(tmp_path, broker)
        pos = insert_test_position(b, "POS_PAPER_6", mode="PAPER_ONLY", entry_price=200.0, qty=65)

        with sqlite3.connect(b.ledger_db) as conn:
            w_before = conn.execute("SELECT balance, realized_pnl FROM paper_wallet WHERE id=1").fetchone()
            bal_before, pnl_before = float(w_before[0]), float(w_before[1])

        b.cached_telemetry["quotes"]["54321"] = 220.0
        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25040.0)
        assert res.get("ok") is True

        # Zero broker calls
        assert len(broker.placed_orders) == 0
        assert len(broker.cancelled_orders) == 0

        # Wallet balance updated
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_PAPER_6'").fetchone()
            assert p["status"] == "CLOSED"
            net_pnl = float(p["net_pnl"])
            w_after = conn.execute("SELECT balance, realized_pnl FROM paper_wallet WHERE id=1").fetchone()
            bal_after, pnl_after = float(w_after[0]), float(w_after[1])
            assert bal_after == pytest.approx(bal_before + net_pnl, abs=0.01)
            assert pnl_after == pytest.approx(pnl_before + net_pnl, abs=0.01)

    asyncio.run(_run())


def test_7_missing_quote_position_stays_open(tmp_path: Path):
    """7. Missing quote -> position stays open, no zero-P&L close."""
    async def _run():
        broker = FakeBroker(configured=True)
        broker.quotes = {}  # Empty quotes

        b = make_test_backend(tmp_path, broker)
        b.cached_telemetry["quotes"] = {}  # No quote in telemetry
        pos = insert_test_position(b, "POS_NOQUOTE_7", mode="LIVE", sl_order_id="SL_INIT_107")

        res = await b._close_position_with_broker(pos, "SPOT_TARGET_HIT", spot_exit=25040.0)
        assert res.get("ok") is False
        assert res.get("error") == "MISSING_QUOTE_RETRY"

        # Zero broker SELL placed
        limit_sells = [o for o in broker.placed_orders if o.get("transactionType") == "SELL" and o.get("orderType") == "LIMIT"]
        assert len(limit_sells) == 0

        # Position remains OPEN in ledger
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            p = conn.execute("SELECT * FROM positions WHERE position_id='POS_NOQUOTE_7'").fetchone()
            assert p["status"] == "OPEN"
            assert p["exit_price"] is None

    asyncio.run(_run())
