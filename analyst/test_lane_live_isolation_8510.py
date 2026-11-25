"""Regression: paper-only lanes must never POST live Dhan entry orders."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from analyst.terminal8510_backend import DhanBroker, QuantTerminalBackend


def _live_armed_backend(tmp_path: Path) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.cfg = {
        "execution_safety_v194": {
            "live_validation_status": "APPROVED",
            "live_dry_run": False,
        },
        "chasing": {
            "aggressive_limit_buffer_pts": 2.0,
            "auto_modify_interval_ms": 10,
            "max_chase_retries": 0,
            "sl_limit_buffer_pts": 2.0,
        },
    }
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.market_db = tmp_path / "market.sqlite3"
    b.execution_mode = "BOTH"
    b._real_money_enabled = True
    b.kill_switch_active = False
    b.paper_execution_enabled = True
    b.algo_strike_selection = {}
    b.directional_bias = "BOTH"
    b.cached_telemetry = {"spot": {"price": 25800.0, "time": time.time()}, "quotes": {}}
    b.scanner_cfg = {
        "ema8_prototype": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True},
        "reversal": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True},
        "flagship_1h_pa": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True},
    }
    b.chasing_cfg = b.cfg["chasing"]
    b.super_trail_cfg = {
        "initial_sl_pts": 20.0,
        "initial_target_pts": 25.0,
        "trailing_sl_enabled": False,
        "trailing_target_enabled": False,
    }
    b.dhan_oi = MagicMock(evaluate_synergy_status=lambda: {"synergy_status": "MIXED"})
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b.algo_strategy_execution_allowed = lambda _sid: True
    b.new_position_entry_allowed = lambda: (True, "")
    b.live_execution_decision = lambda: {"route": "LIVE", "disarm": False, "reason": "test"}
    b.dry_run_gate_state = lambda: {"dry_run": False, "reason": ""}

    broker = MagicMock(spec=DhanBroker)
    broker.is_configured = MagicMock(return_value=True)
    broker.place_order = AsyncMock(return_value={"ok": True, "order_id": "DHAN_SHOULD_NOT"})
    broker.get_marketfeed_quote = AsyncMock(return_value={})
    broker.cancel_order = AsyncMock(return_value={"ok": True})
    broker.modify_order = AsyncMock(return_value={"ok": True})
    broker._live_gate_backend = b
    b.broker = broker

    b.ledger_db.parent.mkdir(parents=True, exist_ok=True)
    b._record_dry_run_order = MagicMock(return_value={})
    b._init_sqlite()
    return b


@pytest.mark.parametrize(
    "strategy_id,source",
    [
        ("ribbon_scalp_long", "REAL_ALGO_PAPER"),
        ("RIBBON_SCALP_LONG", "REAL_ALGO_PAPER"),
        ("matrix_scalp_ce", "REAL_ALGO_PAPER"),
    ],
)
def test_ribbon_scalp_execute_order_never_calls_dhan(tmp_path, strategy_id, source):
    b = _live_armed_backend(tmp_path)
    contract = {
        "symbol": "NIFTY 01 DEC 25800 CALL",
        "security_id": "12345",
        "lot_size": 65,
        "ltp": 238.3,
        "opt_type": "CE",
        "expiry": "2025-12-01",
        "strike": 25800,
        "delta": 0.5,
    }

    async def run():
        return await b.execute_order_with_chaser(
            contract=contract,
            side="BUY",
            strategy_name="Ribbon Touch",
            strategy_id=strategy_id,
            target_pts=15.0,
            sl_pts=15.0,
            is_algo_trade=True,
            source=source,
        )

    res = asyncio.run(run())
    b.broker.place_order.assert_not_called()
    assert res.get("ok") is True or res.get("status") != "REJECTED"


def test_broker_gateway_blocks_ribbon_buy_even_if_bypassed():
    b = object.__new__(QuantTerminalBackend)
    b.execution_mode = "BOTH"
    b._real_money_enabled = True
    b.kill_switch_active = False
    b.scanner_cfg = {}
    real_broker = DhanBroker(cfg={})
    real_broker._live_gate_backend = b

    real_broker.is_configured = lambda: True  # type: ignore[method-assign]

    payload = {
        "transactionType": "BUY",
        "exchangeSegment": "NSE_FNO",
        "securityId": "12345",
        "quantity": 65,
        "orderType": "LIMIT",
        "price": 238.3,
    }

    async def run():
        return await real_broker.place_order(payload, strategy_id="ribbon_scalp_long")

    res = asyncio.run(run())
    assert res.get("ok") is False
    assert res.get("lane_live_blocked") is True


def _open_row(position_id: str, strategy_id: str, *, mode: str = "PAPER_ONLY") -> dict:
    return {
        "position_id": position_id,
        "symbol": "NIFTY 25800 CE",
        "strategy_id": strategy_id,
        "status": "OPEN",
        "mode": mode,
    }


@pytest.mark.parametrize(
    "blocking_strategy_id",
    [
        "STAGE_2_LONG",
        "ribbon_scalp_long",
        "H1_FLAGSHIP_LONG",
    ],
)
@pytest.mark.parametrize(
    "candidate_id",
    ["EMA8_PROTOTYPE_LONG", "CROSSOVER_BULLISH"],
)
@pytest.mark.parametrize("mode", ["PAPER_ONLY", "LIVE_ALLOWED"])
def test_foreign_open_position_never_blocks_ema8_lanes(blocking_strategy_id, candidate_id, mode):
    from analyst.ema_lane_entry_gate_8510 import (
        open_position_count_for_strategy,
        try_reserve_lane_for_signal,
    )

    lock: dict = {}
    foreign = [_open_row("OTHER", blocking_strategy_id, mode=mode)]
    n_lane = open_position_count_for_strategy(foreign, candidate_id)
    assert n_lane == 0
    ok, why = try_reserve_lane_for_signal(lock, candidate_id, open_position_count=n_lane)
    assert ok is True, why
    assert "no second entry" not in why.lower()


def test_prototype_and_reversal_do_not_block_each_other():
    from analyst.ema_lane_entry_gate_8510 import (
        open_position_count_for_strategy,
        try_reserve_lane_for_signal,
    )

    lock: dict = {}
    proto_open = [_open_row("P1", "EMA8_PROTOTYPE_LONG")]
    assert open_position_count_for_strategy(proto_open, "CROSSOVER_BULLISH") == 0
    ok, why = try_reserve_lane_for_signal(
        lock, "CROSSOVER_BULLISH", open_position_count=0
    )
    assert ok is True, why

    lock = {}
    rev_open = [_open_row("R1", "CROSSOVER_BEARISH")]
    assert open_position_count_for_strategy(rev_open, "EMA8_PROTOTYPE_LONG") == 0
    ok2, why2 = try_reserve_lane_for_signal(
        lock, "EMA8_PROTOTYPE_LONG", open_position_count=0
    )
    assert ok2 is True, why2


@pytest.mark.parametrize(
    "lane_sid,other_sid",
    [
        ("EMA8_PROTOTYPE_LONG", "EMA8_PROTOTYPE_SHORT"),
        ("CROSSOVER_BULLISH", "CROSSOVER_BEARISH"),
    ],
)
def test_same_lane_blocks_second_open_position(lane_sid, other_sid):
    from analyst.ema_lane_entry_gate_8510 import (
        open_position_count_for_strategy,
        try_reserve_lane_for_signal,
    )

    lock: dict = {}
    open_pos = [_open_row("X", lane_sid)]
    n = open_position_count_for_strategy(open_pos, other_sid)
    assert n == 1
    ok, why = try_reserve_lane_for_signal(lock, other_sid, open_position_count=n)
    assert ok is False
    assert "no second entry" in why.lower()


def test_ledger_seed_foreign_open_allows_prototype_reserve(tmp_path: Path):
    from analyst.ema_lane_entry_gate_8510 import open_position_count_for_strategy, try_reserve_lane_for_signal

    b = _live_armed_backend(tmp_path)
    b.strategy_status = {"daily_sr": {"levels": {}}}
    b._ema_lane_lock = {}
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute(
            """
            INSERT INTO positions (
                position_id, symbol, status, mode, entry_time, strategy_id,
                qty, entry_price, side, lots, security_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "MJ_OPEN",
                "NIFTY 25800 CE",
                "OPEN",
                "PAPER_ONLY",
                "2026-11-25 09:30:00 IST",
                "STAGE_2_LONG",
                65,
                200.0,
                "BUY",
                1,
                "88946520",
            ),
        )
        conn.commit()
    active = b.get_active_positions()
    assert len(active) == 1
    n = open_position_count_for_strategy(active, "EMA8_PROTOTYPE_LONG")
    assert n == 0
    ok, why = try_reserve_lane_for_signal(b._ema_lane_lock, "EMA8_PROTOTYPE_LONG", open_position_count=n)
    assert ok is True, why