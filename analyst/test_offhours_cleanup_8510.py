"""Off-hours entry block + stray position cleanup (8510)."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class FakeBrokerCleanup:
    def is_configured(self):
        return True

    async def get_marketfeed_ltp(self, payload: Dict[str, Any] = None):
        return {"54321": 8.5}

    _ltp_cache = {"13": 25010.0}


def make_backend(tmp_path: Path) -> QuantTerminalBackend:
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
    b.scanner_cfg = {}
    b.strategy_status = {}
    b._lock = None
    b.algo_allowlist_cfg = {"allowed_strategy_ids": ["H1_FLAGSHIP_LONG"]}
    b.chasing_cfg = b.cfg["algo_order_chasing"]
    b.super_trail_cfg = {}
    b.nse_holidays = set()
    b._real_money_enabled = False
    b.paper_execution_enabled = True
    b.execution_mode = "PAPER_ONLY"
    b.kill_switch_active = False
    b.directional_bias = "NEUTRAL"
    b.cached_telemetry = {
        "spot": {
            "price": 25010.0,
            "is_stale": False,
            "status": "LIVE",
            "updated_epoch": time.time(),
        }
    }
    b.broker = FakeBrokerCleanup()
    b.cached_quotes = {}
    b._position_close_locks = {}
    b._auto_cleanup_count_today = 0
    b._auto_cleanup_count_date = ""
    b._last_auto_cleanup = None
    b.allow_off_hours_paper_testing = True
    b._off_hours_expires_at = time.time() + 3600
    b._init_sqlite()
    b.algo_strike_selection = {"LONG_CE_ATM": True}
    b.dhan_oi = type("OI", (), {"evaluate_synergy_status": lambda self: {"synergy_status": "MIXED"}})()
    return b


def _insert_open(
    db_path: Path,
    pos_id: str,
    symbol: str = "23 Nov 2026 — NIFTY 25400 CE",
    mode: str = "PAPER",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO positions (
                position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                current_ltp, target_price, sl_price, status, mode, strategy_id
            ) VALUES (
                ?, ?, '54321', 'BUY', 1, 65, 120.0,
                '2026-11-17 19:31:00 IST', 110.0, 160.0, 80.0, 'OPEN', ?, 'H1_FLAGSHIP_LONG'
            )
            """,
            (pos_id, symbol, mode),
        )
        conn.commit()


def _contract() -> Dict[str, Any]:
    return {
        "id": "LONG_CE_ATM",
        "symbol": "25 Nov 2026 — NIFTY 25400 CE",
        "opt_type": "CE",
        "strike": 25400,
        "ltp": 180.0,
        "security_id": 54321,
        "lot_size": 65,
        "delta": 0.6,
    }


def test_paper_entry_blocked_off_hours(tmp_path: Path):
    b = make_backend(tmp_path)
    b.is_nse_market_open = lambda dt=None: (False, "AFTER_HOURS")
    res = asyncio.run(b.execute_order_with_chaser(
        contract=_contract(),
        side="BUY",
        strategy_name="Homework",
        strategy_id="H1_FLAGSHIP_LONG",
        is_algo_trade=True,
    ))
    assert res["ok"] is False
    assert res.get("entry_guard") or "Market closed" in str(res.get("rejection_reason", ""))


def test_paper_entry_blocked_stale_spot(tmp_path: Path):
    b = make_backend(tmp_path)
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b.cached_telemetry["spot"] = {
        "price": 25010.0,
        "is_stale": True,
        "updated_epoch": time.time(),
    }
    res = asyncio.run(b.execute_order_with_chaser(
        contract=_contract(),
        side="BUY",
        strategy_name="Homework",
        strategy_id="H1_FLAGSHIP_LONG",
        is_algo_trade=True,
    ))
    assert res["ok"] is False
    assert "STALE" in str(res.get("rejection_reason", "")).upper()


def test_paper_entry_allowed_in_session(tmp_path: Path):
    b = make_backend(tmp_path)
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b._lock = None
    b.run_aggressive_limit_chaser = AsyncMock(
        return_value={"status": "FILLED", "fill_price": 179.0, "order_id": "O1"}
    )
    b._place_companion_sl = AsyncMock(return_value="SL1")
    b.live_execution_decision = lambda: {"route": "PAPER_ONLY", "reason": "", "disarm": False}
    b.algo_strategy_execution_allowed = lambda _sid: True
    res = asyncio.run(b.execute_order_with_chaser(
        contract=_contract(),
        side="BUY",
        strategy_name="Homework",
        strategy_id="H1_FLAGSHIP_LONG",
        is_algo_trade=True,
    ))
    assert res.get("ok") is True


def test_cleanup_eod_after_1520(tmp_path: Path):
    b = make_backend(tmp_path)
    _insert_open(b.ledger_db, "POS_EOD")
    dt = datetime(2026, 11, 10, 15, 21, 0, tzinfo=IST)
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    closed = b.check_open_position_cleanup(dt)
    assert len(closed) == 1
    assert "EOD_SQUARE_OFF_15:20" in str(closed[0].get("message", closed[0].get("exit_reason", "")))


def test_cleanup_off_hours(tmp_path: Path):
    b = make_backend(tmp_path)
    _insert_open(b.ledger_db, "POS_OFF")
    dt = datetime(2026, 11, 17, 0, 31, 2, tzinfo=IST)
    b.is_nse_market_open = lambda dt=None: (False, "AFTER_HOURS")
    closed = b.check_open_position_cleanup(dt)
    assert len(closed) == 1
    assert b._last_auto_cleanup
    assert "OFF_HOURS_CLEANUP" in b._last_auto_cleanup["reason"]


def test_cleanup_expired_contract(tmp_path: Path):
    b = make_backend(tmp_path)
    _insert_open(b.ledger_db, "POS_EXP", symbol="17 Nov 2026 — NIFTY 25400 CE")
    dt = datetime(2026, 11, 18, 10, 0, 0, tzinfo=IST)
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    closed = b.check_open_position_cleanup(dt)
    assert len(closed) == 1
    assert b._last_auto_cleanup["reason"] == "EXPIRED_CONTRACT_CLEANUP"


def test_exit_allowed_off_hours_enforce_false(tmp_path: Path):
    b = make_backend(tmp_path)
    _insert_open(b.ledger_db, "POS_EXIT")
    b.is_nse_market_open = lambda dt=None: (False, "AFTER_HOURS")
    res = b.modify_open_position("POS_EXIT", action="CLOSE", enforce_market_hours=False, reason="MANUAL")
    assert res["ok"] is True


def test_cleanup_idempotent(tmp_path: Path):
    b = make_backend(tmp_path)
    _insert_open(b.ledger_db, "POS_IDEM")
    dt = datetime(2026, 11, 17, 19, 0, 0, tzinfo=IST)
    b.is_nse_market_open = lambda dt=None: (False, "AFTER_HOURS")
    first = b.check_open_position_cleanup(dt)
    second = b.check_open_position_cleanup(dt)
    assert len(first) == 1
    assert len(second) == 0