"""Track 1 Acceptance Tests: Paper must fill on real option prices (Entry & Exit).

Coverage requirements:
1. Fresh entry -> REAL_QUOTE (paper fill equals real ask of exact sec_id, price_source="REAL_QUOTE", quote_age recorded).
2. Missing quote -> on-demand fetch then ESTIMATED_SIGNAL_LTP (negative test: signal_ltp never labelled REAL_QUOTE).
3. Exit symmetry: exit uses real bid of the same sec_id; prove EXIT_PENDING resolves on next fresh bid or alarms (never silently holds forever).
4. EOD diagnostic: every paper fill row records price_source in {REAL_QUOTE, ESTIMATED_SIGNAL_LTP} and quote age.
5. Paper sec_id == real-money sec_id (the exact same option security_id is traded).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from analyst.paper_quote_8510 import executable_paper_quote
from analyst.terminal8510_backend import QuantTerminalBackend
from analyst.test_live_exit_orders_8510 import FakeBroker, insert_test_position


def make_test_backend(tmp_path: Path, fake_broker: FakeBroker) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {
        "execution_safety_v194": {
            "live_validation_status": "APPROVED",
            "typed_confirmation": "LIVE",
            "live_dry_run": False,
            "dry_run_exit_min_orders": 0,
        },
        "starting_paper_capital": 100000.0,
    }
    b.config_path = tmp_path / "terminal8510_config.json"
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.market_db = tmp_path / "market.sqlite3"
    b.broker = fake_broker
    b._position_close_locks = {}
    b.exit_failure_banner = None
    b.contract_selection_alarm = None
    b.cached_telemetry = {"quotes": {}, "spot": {"price": 25000.0}}
    b.strategy_status = {}
    b.scanner_cfg = {}
    b.super_trail_cfg = {
        "initial_sl_pts": 20.0,
        "initial_target_pts": 25.0,
        "trailing_sl_enabled": True,
        "trailing_target_enabled": True,
    }
    b.chasing_cfg = {
        "aggressive_limit_buffer_pts": 2.0,
        "auto_modify_interval_ms": 10,
        "max_chase_retries": 1,
    }
    b.directional_bias = "NEUTRAL"
    b.algo_only_execution = True
    b.execution_mode = "PAPER_ONLY"
    b.paper_execution_enabled = True
    b.kill_switch_active = False
    b.algo_strike_selection = {}
    b.algo_allowlist_cfg = {"enabled": False}
    b._real_money_enabled = False
    b._lock = None
    b.nse_holidays = set()
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")

    class MockDhanOI:
        def evaluate_synergy_status(self):
            return {"synergy_status": "MIXED"}
        def cached_snapshot(self):
            return {}

    b.dhan_oi = MockDhanOI()
    b._init_sqlite()
    return b


def test_executable_paper_quote_detail_and_bid_ask():
    broker = FakeBroker()
    broker.get_marketfeed_quote = AsyncMock(return_value={
        '12345': {'bid': 240.5, 'ask': 242.0, 'last_price': 241.0}
    })
    # BUY uses ask
    ask, src, age = asyncio.run(executable_paper_quote(broker, '12345', 'BUY', detail=True))
    assert ask == 242.0
    assert src == "REAL_QUOTE"
    assert age >= 0.0

    # SELL uses bid
    bid, src, age = asyncio.run(executable_paper_quote(broker, '12345', 'SELL', detail=True))
    assert bid == 240.5
    assert src == "REAL_QUOTE"
    assert age >= 0.0


def test_missing_quote_returns_none_in_detail_mode():
    broker = FakeBroker()
    broker.get_marketfeed_quote = AsyncMock(return_value={})
    price, src, age = asyncio.run(executable_paper_quote(broker, '99999', 'BUY', detail=True))
    assert price is None
    assert src is None
    assert age >= 0.0


def test_paper_entry_fills_on_real_ask_and_records_real_quote(tmp_path: Path):
    """1. Fresh entry -> REAL_QUOTE (paper fill equals real ask of exact sec_id)."""
    broker = FakeBroker()
    sec_id = "61471"
    broker.get_marketfeed_quote = AsyncMock(return_value={
        sec_id: {"bid": 210.0, "ask": 212.5, "last_price": 211.0}
    })
    b = make_test_backend(tmp_path, broker)
    b.execution_mode = "PAPER_ONLY"
    b.paper_execution_enabled = True

    contract = {
        "security_id": sec_id,
        "symbol": "NIFTY26NOV25000CE",
        "lot_size": 65,
        "ltp": 205.0,  # signal LTP differs from live ask
        "opt_type": "CE",
        "strike": 25000,
        "expiry": "2026-11-26",
    }

    res = asyncio.run(b.execute_order_with_chaser(
        contract=contract,
        side="BUY",
        strategy_name="8 EMA Prototype",
        strategy_id="EMA8_PROTOTYPE_LONG",
        is_algo_trade=True,
    ))

    assert res["ok"] is True
    assert res["status"] == "FILLED"
    # Paper fill price must equal the real option ask (212.5), NOT signal_ltp (205.0)
    assert res["fill_price"] == 212.5
    assert res["price_source"] == "REAL_QUOTE"

    # Verify positions and orders table records
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE position_id=?", (res["position_id"],)).fetchone()
        assert pos["entry_price"] == 212.5
        assert pos["price_source"] == "REAL_QUOTE"
        assert pos["quote_age"] is not None

        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (res["order_id"],)).fetchone()
        assert order["fill_price"] == 212.5
        assert order["price_source"] == "REAL_QUOTE"
        assert order["quote_age"] is not None


def test_missing_quote_falls_back_to_estimated_signal_ltp_never_real_quote(tmp_path: Path):
    """2. Negative test: missing quote -> ESTIMATED_SIGNAL_LTP. Signal_ltp is NEVER labelled REAL_QUOTE."""
    broker = FakeBroker()
    sec_id = "61472"
    broker.get_marketfeed_quote = AsyncMock(return_value={})  # Empty depth
    b = make_test_backend(tmp_path, broker)
    b.execution_mode = "PAPER_ONLY"
    b.paper_execution_enabled = True

    contract = {
        "security_id": sec_id,
        "symbol": "NIFTY26NOV25000PE",
        "lot_size": 65,
        "ltp": 195.0,  # Signal LTP
        "opt_type": "PE",
        "strike": 25000,
        "expiry": "2026-11-26",
    }

    res = asyncio.run(b.execute_order_with_chaser(
        contract=contract,
        side="BUY",
        strategy_name="8 EMA Prototype",
        strategy_id="EMA8_PROTOTYPE_SHORT",
        is_algo_trade=True,
    ))

    assert res["ok"] is True
    assert res["fill_price"] == 195.0
    # Must be marked ESTIMATED_SIGNAL_LTP — NEVER REAL_QUOTE
    assert res["price_source"] == "ESTIMATED_SIGNAL_LTP"
    assert res["price_source"] != "REAL_QUOTE"

    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE position_id=?", (res["position_id"],)).fetchone()
        assert pos["entry_price"] == 195.0
        assert pos["price_source"] == "ESTIMATED_SIGNAL_LTP"
        assert pos["price_source"] != "REAL_QUOTE"

        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (res["order_id"],)).fetchone()
        assert order["fill_price"] == 195.0
        assert order["price_source"] == "ESTIMATED_SIGNAL_LTP"
        assert order["price_source"] != "REAL_QUOTE"


def test_exit_uses_real_bid_and_exit_pending_resolution(tmp_path: Path):
    """3. Exit symmetry: exit uses real bid; prove EXIT_PENDING alarms then resolves on next fresh bid."""
    broker = FakeBroker()
    sec_id = "61473"
    # Initially depth is missing
    broker.get_marketfeed_quote = AsyncMock(return_value={})
    b = make_test_backend(tmp_path, broker)

    pos = insert_test_position(b, "POS_EXIT_TEST", mode="PAPER_ONLY", sec_id=sec_id, entry_price=200.0)

    # 1st attempt: missing quote -> status remains EXIT_PENDING
    res1 = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
    assert res1["status"] == "EXIT_PENDING"
    assert res1["error"] == "MISSING_FRESH_PAPER_BID"

    # Check status in DB
    with sqlite3.connect(b.ledger_db) as conn:
        st = conn.execute("SELECT status FROM positions WHERE position_id=?", (pos["position_id"],)).fetchone()[0]
    assert st == "EXIT_PENDING"

    # 2nd attempt: still missing quote
    res2 = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
    assert res2["status"] == "EXIT_PENDING"

    # 3rd attempt: still missing quote -> alarms via exit_failure_banner
    res3 = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
    assert res3["status"] == "EXIT_PENDING"
    assert b.exit_failure_banner is not None
    assert "EXIT_PENDING ALARM" in b.exit_failure_banner["error"]

    # Now fresh quote arrives with real bid of 235.0
    broker.get_marketfeed_quote = AsyncMock(return_value={
        sec_id: {"bid": 235.0, "ask": 236.0, "last_price": 235.5}
    })

    # Next attempt: resolves EXIT_PENDING at the real bid 235.0
    res4 = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
    assert res4["ok"] is True
    assert res4["fill_price"] == 235.0
    # Alarm banner cleared
    assert b.exit_failure_banner is None

    # Check position is CLOSED and price_source is REAL_QUOTE
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        closed_pos = conn.execute("SELECT * FROM positions WHERE position_id=?", (pos["position_id"],)).fetchone()
        assert closed_pos["status"] == "CLOSED"
        assert closed_pos["exit_price"] == 235.0
        assert closed_pos["exit_price_source"] == "REAL_QUOTE"

        exit_order = conn.execute("SELECT * FROM orders WHERE position_id=? AND side='SELL'", (pos["position_id"],)).fetchone()
        assert exit_order is not None
        assert exit_order["fill_price"] == 235.0
        assert exit_order["price_source"] == "REAL_QUOTE"


def test_paper_sec_id_equals_real_money_sec_id(tmp_path: Path):
    """5. Prove paper sec_id is identical to the exact option contract real money trades."""
    broker = FakeBroker()
    target_sec_id = "59876"
    broker.get_marketfeed_quote = AsyncMock(return_value={
        target_sec_id: {"bid": 250.0, "ask": 252.0, "last_price": 251.0}
    })
    b = make_test_backend(tmp_path, broker)
    b.execution_mode = "PAPER_ONLY"
    b.paper_execution_enabled = True

    contract = {
        "security_id": target_sec_id,
        "symbol": "NIFTY26NOV25100CE",
        "lot_size": 65,
        "ltp": 240.0,
        "opt_type": "CE",
        "strike": 25100,
        "expiry": "2026-11-26",
    }

    res = asyncio.run(b.execute_order_with_chaser(
        contract=contract,
        side="BUY",
        strategy_name="8 EMA Prototype",
        strategy_id="EMA8_PROTOTYPE_LONG",
        is_algo_trade=True,
    ))

    assert res["ok"] is True
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        pos = conn.execute("SELECT * FROM positions WHERE position_id=?", (res["position_id"],)).fetchone()
        assert pos["security_id"] == target_sec_id
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (res["order_id"],)).fetchone()
        assert order["security_id"] == target_sec_id

    # Exit check
    exit_res = asyncio.run(b.execute_position_exit(dict(pos), "SPOT_TARGET_HIT", spot_exit=25150))
    assert exit_res["ok"] is True
    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        exit_order = conn.execute("SELECT * FROM orders WHERE position_id=? AND side='SELL'", (pos["position_id"],)).fetchone()
        assert exit_order["security_id"] == target_sec_id


def test_eod_diagnostic_records_price_source_and_quote_age(tmp_path: Path):
    """4. EOD diagnostic: every paper fill row records price_source in {REAL_QUOTE, ESTIMATED_SIGNAL_LTP} and quote age."""
    broker = FakeBroker()
    broker.get_marketfeed_quote = AsyncMock(side_effect=[
        # First call has real depth
        {"11111": {"bid": 200.0, "ask": 202.0, "last_price": 201.0}},
        # Second call has no depth
        {},
    ])
    b = make_test_backend(tmp_path, broker)
    b.execution_mode = "PAPER_ONLY"
    b.paper_execution_enabled = True

    # Trade 1: Fresh real quote
    c1 = {
        "security_id": "11111",
        "symbol": "NIFTY26NOV25000CE",
        "lot_size": 65,
        "ltp": 190.0,
        "opt_type": "CE",
        "strike": 25000,
        "expiry": "2026-11-26",
    }
    r1 = asyncio.run(b.execute_order_with_chaser(contract=c1, is_algo_trade=True))
    assert r1["ok"] is True

    # Trade 2: Missing quote -> estimated signal LTP
    c2 = {
        "security_id": "22222",
        "symbol": "NIFTY26NOV25000PE",
        "lot_size": 65,
        "ltp": 180.0,
        "opt_type": "PE",
        "strike": 25000,
        "expiry": "2026-11-26",
    }
    r2 = asyncio.run(b.execute_order_with_chaser(contract=c2, is_algo_trade=True))
    assert r2["ok"] is True

    # Check order book API / table
    orders = b.get_order_book()
    assert len(orders) >= 2
    sources = {o["price_source"] for o in orders if o.get("price_source")}
    assert "REAL_QUOTE" in sources
    assert "ESTIMATED_SIGNAL_LTP" in sources
    for o in orders:
        if o.get("price_source"):
            assert o["price_source"] in ("REAL_QUOTE", "ESTIMATED_SIGNAL_LTP")
            assert o.get("quote_age") is not None
