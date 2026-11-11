"""Unit tests for Phase 5: End-of-Day (EOD) square-off for 8 EMA Prototype lane.

Acceptance criteria:
1. Live monitor: when now_dt >= eod_square_off_time (15:20), prototype positions are closed with reason EOD_SQUARE_OFF_15:20.
2. Routed through Phase 1 broker close path: companion SL is cancelled at Dhan, ledger closed with reason EOD_SQUARE_OFF_15:20.
3. Paper mode: paper wallet credited, position marked closed with reason EOD_SQUARE_OFF_15:20.
4. check_eod_square_off(dt): when dt.time() >= 15:20, prototype position squares off with reason EOD_SQUARE_OFF_15:20.
5. Backtest simulation (simulate_backtest_spot_exit): squares off at 15:20 with reason EOD_SQUARE_OFF_15:20.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend, simulate_backtest_spot_exit  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class FakeBrokerEOD:
    def __init__(self):
        self.orders = []
        self.cancelled_order_ids = []
        self.placed_orders = []

    def is_configured(self):
        return True

    async def get_orders(self):
        return list(self.orders)

    async def get_marketfeed_ltp(self, payload: Dict[str, Any] = None):
        return {"54321": 208.5}

    async def get_order_by_id(self, order_id: str):
        return {
            "orderId": order_id,
            "orderStatus": "PENDING",
            "price": 200.0,
            "quantity": 65,
        }

    async def cancel_order(self, order_id: str):
        self.cancelled_order_ids.append(order_id)
        return {"ok": True, "orderId": order_id, "status": "CANCELLED"}

    async def place_order(self, payload: Dict[str, Any] = None, **kwargs):
        oid = f"ORD_SELL_{len(self.placed_orders) + 1}"
        record = dict(payload or kwargs)
        record["orderId"] = oid
        otype = str(record.get("orderType", "")).upper()
        if otype == "LIMIT":
            record["orderStatus"] = "TRADED"
            record["averageTradedPrice"] = float(record.get("price") or 0.0)
            record["filledQty"] = int(record.get("quantity") or 0)
        self.orders.append(record)
        self.placed_orders.append(record)
        return {"ok": True, "order_id": oid, "data": {"orderId": oid}}


def make_test_backend(tmp_path: Path) -> QuantTerminalBackend:
    db_path = tmp_path / "ledger.sqlite3"
    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = db_path
    b.cfg = {
        "eod_square_off_time": "15:20",
        "chasing": {"auto_modify_interval_ms": 10},
        "algo_order_chasing": {
            "max_attempts": 3,
            "step_interval_sec": 0.05,
            "sl_limit_buffer_pts": 1.0,
            "chase_limit_buffer_pts": 2.0,
        },
    }
    b.scanner_cfg = {
        "flagship_eod_square_off": "15:20",
        "ema8_prototype": {
            "enabled": True,
            "touch_tol_pts": 3.0,
            "target_pts": 40.0,
            "sl_pts": 40.0,
        },
    }
    b.strategy_status = {}
    b._lock = None
    b.algo_allowlist_cfg = {"allowed_strategy_ids": ["EMA8_PROTOTYPE_LONG", "EMA8_PROTOTYPE_SHORT"]}
    b.chasing_cfg = b.cfg["algo_order_chasing"]
    b.super_trail_cfg = {}
    b.nse_holidays = set()
    b._real_money_enabled = True
    b.paper_wallet = {"cash_balance": 100000.0, "realized_pnl": 0.0}
    b.cached_telemetry = {"quotes": {}, "spot": {"price": 25000.0}}
    b.broker = FakeBrokerEOD()
    b.cached_quotes = {}
    b._position_close_locks = {}
    b._latest_closed_15m_bar = lambda: None
    b._ledger_nifty_spot = lambda: 25000.0
    b.is_nse_market_open = lambda: (True, "OPEN")
    b.off_hours_testing_active = lambda: False
    b._init_sqlite()
    return b


def _insert_open_proto_pos(db_path: Path, pos_id: str, mode: str = "LIVE", sl_order_id: str = "SL_ORD_1") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO positions (
                position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                current_ltp, target_price, sl_price, status, mode, strategy_id, spot_entry,
                spot_target_level, spot_stop_level, sl_order_id, initial_target, initial_sl
            ) VALUES (
                ?, 'NIFTY 25000 CE', '54321', 'BUY', 1, 65, 200.0,
                '2026-11-10 10:00:00 IST', 210.0, 240.0, 160.0, 'OPEN', ?,
                'EMA8_PROTOTYPE_LONG', 25000.0, 25040.0, 24960.0, ?, 240.0, 160.0
            )
            """,
            (pos_id, mode, sl_order_id),
        )
        conn.commit()


def test_1_live_monitor_squares_off_at_1520(tmp_path: Path):
    """When now_dt >= 15:20, live monitor squares off open prototype position via broker with EOD_SQUARE_OFF_15:20."""
    b = make_test_backend(tmp_path)
    pos_id = "POS_EOD_1"
    _insert_open_proto_pos(b.ledger_db, pos_id, mode="LIVE", sl_order_id="SL_ORD_101")

    dt_1520 = datetime(2026, 11, 10, 15, 20, 5, tzinfo=IST)
    close_mock = AsyncMock(return_value={"ok": True})
    b._close_position_with_broker = close_mock

    with pytest.MonkeyPatch.context() as mp:
        import analyst.terminal8510_backend as tmod
        class MockDatetime:
            @classmethod
            def now(cls, tz=None):
                return dt_1520
        mp.setattr(tmod, "datetime", MockDatetime)

        b.get_active_positions(live_quotes={"54321": 210.0}, spot_price=25010.0)

    assert close_mock.called
    call_args = close_mock.call_args[0]
    assert call_args[0]["position_id"] == pos_id
    assert call_args[1] == "EOD_SQUARE_OFF_15:20"


def test_2_eod_square_off_cancels_companion_sl_and_closes_ledger(tmp_path: Path):
    """Full broker close path on EOD_SQUARE_OFF_15:20 cancels companion SL at Dhan and closes position in ledger."""
    b = make_test_backend(tmp_path)
    pos_id = "POS_EOD_LIVE"
    _insert_open_proto_pos(b.ledger_db, pos_id, mode="LIVE", sl_order_id="SL_ORD_202")

    pos = b.get_active_positions()[0]
    b.run_aggressive_limit_chaser = AsyncMock(return_value={"status": "FILLED", "fill_price": 208.5})

    res = asyncio.run(b._close_position_with_broker(pos, "EOD_SQUARE_OFF_15:20"))
    assert res["ok"] is True
    assert res["exit_reason"] == "EOD_SQUARE_OFF_15:20"

    assert "SL_ORD_202" in b.broker.cancelled_order_ids

    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone()
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "EOD_SQUARE_OFF_15:20"
        assert row["exit_price"] == 206.5


def test_3_eod_square_off_paper_mode(tmp_path: Path):
    """Paper prototype position EOD square-off credits wallet and closes ledger with EOD_SQUARE_OFF_15:20."""
    b = make_test_backend(tmp_path)
    pos_id = "POS_EOD_PAPER"
    _insert_open_proto_pos(b.ledger_db, pos_id, mode="PAPER", sl_order_id="")

    pos = b.get_active_positions()[0]
    res = asyncio.run(b._close_position_with_broker(pos, "EOD_SQUARE_OFF_15:20"))
    assert res["ok"] is True
    assert res["exit_reason"] == "EOD_SQUARE_OFF_15:20"

    with sqlite3.connect(b.ledger_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone()
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "EOD_SQUARE_OFF_15:20"


def test_4_check_eod_square_off_function(tmp_path: Path):
    """check_eod_square_off(dt) closes open positions with EOD_SQUARE_OFF_15:20 when dt.time() >= 15:20."""
    b = make_test_backend(tmp_path)
    pos_id = "POS_EOD_FUNC"
    _insert_open_proto_pos(b.ledger_db, pos_id, mode="PAPER", sl_order_id="")

    dt_1510 = datetime(2026, 11, 10, 15, 10, 0, tzinfo=IST)
    res_1510 = b.check_eod_square_off(dt_1510)
    assert len(res_1510) == 0

    dt_1520 = datetime(2026, 11, 10, 15, 20, 0, tzinfo=IST)
    res_1520 = b.check_eod_square_off(dt_1520)
    assert len(res_1520) == 1
    assert "EOD_SQUARE_OFF_15:20" in res_1520[0]["message"]


def test_5_backtest_squares_off_at_1520():
    """simulate_backtest_spot_exit squares off prototype positions at 15:20 with reason EOD_SQUARE_OFF_15:20."""
    times = ["14:00", "14:15", "14:30", "14:45", "15:00", "15:15", "15:30"]
    bars = []
    for i, t in enumerate(times):
        bars.append(
            {
                "open": 25000.0,
                "high": 25010.0,  # Never hits target (25040)
                "low": 24990.0,   # Never hits stop (24960)
                "close": 25005.0,
                "hm": t,
                "day": "2026-11-10",
                "ts": 1794290000 + i * 900,
                "e8": 24995.0,    # Always below close -> no opposite close
            }
        )

    st_pnl, fx_pnl, ex_spot, st_reason, trail_steps, mfe, ent_day, ent_hm, ex_day, ex_hm = simulate_backtest_spot_exit(
        bars,
        start_idx=0,
        entry_spot=25000.0,
        is_long=True,
        sl_pts=40.0,
        tgt_pts=40.0,
        is_flagship=False,
        sr_levels={},
        eod_hm="15:20",
        exit_on_opposite_8ema_close=True,
    )

    assert st_reason == "EOD_SQUARE_OFF_15:20"
    assert ex_spot == 25005.0
    assert ex_hm == "15:30"
