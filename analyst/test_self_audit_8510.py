"""v2.0.42 desk self-audit: lane reconcile, exit sweep, missed-trade watchdog."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from analyst.ema_lane_lock_8510 import LANE_PROTOTYPE, try_acquire_lane_lock
from analyst.ema_lane_reconcile_8510 import reconcile_lane_lock_on_startup
from analyst.missed_trade_watchdog_8510 import (
    _legitimate_block_reason,
    load_watchdog_cfg,
    note_signals_submitted,
    run_watchdog_timeouts,
)
from analyst.open_position_exit_sweep_8510 import spot_exit_should_close, sweep_open_positions
from analyst.test_arm_persistence_8510 import _backend_with_db


def test_crash_after_reserve_auto_releases_lane(tmp_path: Path):
    b1 = _backend_with_db(tmp_path)
    try_acquire_lane_lock(b1._ema_lane_lock, LANE_PROTOTYPE, note="mid-entry crash")
    b1._save_ema_lane_lock_state()
    b2 = _backend_with_db(tmp_path)
    b2.get_active_positions = lambda: []  # type: ignore
    assert reconcile_lane_lock_on_startup(b2)
    assert not b2._ema_lane_lock.get("holder")
    with sqlite3.connect(b2.ledger_db) as conn:
        assert conn.execute("SELECT count(*) FROM ema_lane_lock_state").fetchone()[0] == 0


def test_stuck_open_at_target_sweep_closes_and_frees_lane(tmp_path: Path):
    b = _backend_with_db(tmp_path)
    b.cached_telemetry = {"spot": {"price": 25180.0}}
    b.cfg = {"eod_square_off_time": "15:20"}
    b.scanner_cfg = {}
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute(
            """
            INSERT INTO positions (
                position_id, symbol, security_id, side, lots, qty,
                entry_price, current_ltp, target_price, sl_price,
                status, mode, strategy_id, spot_entry, spot_target_level, spot_stop_level
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "P1",
                "NIFTY 25200 CE",
                "99",
                "BUY",
                1,
                65,
                120.0,
                120.0,
                160.0,
                80.0,
                "OPEN",
                "PAPER_ONLY",
                "EMA8_PROTOTYPE_LONG",
                25100.0,
                25140.0,
                25060.0,
            ),
        )
        conn.commit()
    try_acquire_lane_lock(b._ema_lane_lock, LANE_PROTOTYPE, note="open")
    b._ema8_prototype_state = {"open": {"direction": "LONG"}}
    p = {
        "position_id": "P1",
        "strategy_id": "EMA8_PROTOTYPE_LONG",
        "symbol": "NIFTY 25200 CE",
        "spot_entry": 25100.0,
        "spot_target_level": 25140.0,
        "spot_stop_level": 25060.0,
    }
    hit, reason = spot_exit_should_close(b, p, spot_idx=25180.0)
    assert hit and reason == "SPOT_TARGET_HIT"
    def _open_only():
        with sqlite3.connect(b.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE status='OPEN'")]

    b.get_active_positions = _open_only  # type: ignore
    b.off_hours_testing_active = lambda: True  # type: ignore
    b.nse_holidays = []  # type: ignore
    closed = sweep_open_positions(b, enforce_market_hours=False)
    assert len(closed) == 1
    assert not b.get_active_positions()
    assert not b._ema_lane_lock.get("holder")
    assert "open" not in (b._ema8_prototype_state or {})


def test_eod_square_off_on_stuck_open(tmp_path: Path):
    b = _backend_with_db(tmp_path)
    b.cached_telemetry = {"spot": {"price": 25100.0}}
    b.cfg = {"eod_square_off_time": "15:20"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 11, 19, 15, 25, tzinfo=ZoneInfo("Asia/Kolkata"))
    p = {
        "strategy_id": "EMA8_PROTOTYPE_LONG",
        "spot_entry": 25100.0,
        "spot_target_level": 25200.0,
        "spot_stop_level": 25060.0,
    }
    hit, reason = spot_exit_should_close(b, p, spot_idx=25100.0, now=now)
    assert hit and "EOD_SQUARE_OFF" in reason


def test_watchdog_alarms_qualified_no_order(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class _FixedNow:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 19, 11, 4, tzinfo=tz or ZoneInfo("Asia/Kolkata"))

        def strftime(self, *a, **k):
            return "11:04"

    b = SimpleNamespace(
        scanner_cfg={"miss_watchdog": {"enabled": True, "qualification_timeout_sec": 0.1}},
        get_active_positions=lambda: [],
        _ema_lane_lock={},
        _ema8_prototype_state={"arm": {"direction": "LONG", "away_seen": True}},
        _ema_entry_decisions={"ema8_prototype": {"stage": "BLOCKED", "reason": "Contract selection: no quote"}},
        _proto_ema_audit={},
        strategy_cooldowns={},
        algo_strategy_execution_allowed=lambda _s: True,
        __dict__={},
    )
    with patch("analyst.missed_trade_watchdog_8510.datetime") as dtmod, patch(
        "analyst.missed_trade_watchdog_8510.append_miss_event"
    ) as am, patch("analyst.missed_trade_watchdog_8510.try_telegram_alert"):
        dtmod.now = _FixedNow.now
        note_signals_submitted(
            b,
            [{"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG", "setup_name": "proto"}],
        )
        b._miss_watchdog["pending"][list(b._miss_watchdog["pending"].keys())[0]]["t0"] = time.time() - 60
        run_watchdog_timeouts(b)
        assert am.called
        assert am.call_args.kwargs.get("event") == "WOULD_TAKE_UNFILLED"


def test_watchdog_silent_when_legitimately_blocked():
    b = SimpleNamespace(
        get_active_positions=lambda: [{"position_id": "x"}],
        _ema_lane_lock={},
        _ema8_prototype_state={},
        strategy_cooldowns={},
        algo_strategy_execution_allowed=lambda _s: True,
    )
    assert _legitimate_block_reason(b, setup="prototype", direction="LONG") == "lane held by open position"


def test_watchdog_dedupe_one_alert_per_bar(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class _FixedNow:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 19, 11, 4, tzinfo=tz or ZoneInfo("Asia/Kolkata"))

    b = SimpleNamespace(
        scanner_cfg={"miss_watchdog": {"enabled": True, "qualification_timeout_sec": 0.0}},
        get_active_positions=lambda: [],
        _ema_lane_lock={},
        _ema8_prototype_state={},
        _ema_entry_decisions={},
        _proto_ema_audit={},
        strategy_cooldowns={},
        algo_strategy_execution_allowed=lambda _s: True,
        __dict__={},
    )
    sig = [{"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}]
    with patch("analyst.missed_trade_watchdog_8510.datetime") as dtmod, patch(
        "analyst.missed_trade_watchdog_8510.append_miss_event"
    ) as am:
        dtmod.now = _FixedNow.now
        note_signals_submitted(b, sig)
        key = list(b._miss_watchdog["pending"].keys())[0]
        b._miss_watchdog["pending"][key]["t0"] = time.time() - 60
        run_watchdog_timeouts(b)
        run_watchdog_timeouts(b)
        assert am.call_count == 1


def test_no_quote_retry_preserves_arm_away_seen(tmp_path: Path):
    from analyst.terminal8510_backend import QuantTerminalBackend

    b = object.__new__(QuantTerminalBackend)
    b._ema8_prototype_state = {
        "arm": {"direction": "LONG", "away_seen": True, "cross_bar_open_ts": 1},
    }
    b._mj_sir_state = {}
    b._release_unfilled_setup(
        {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG", "setup_name": "x"}
    )
    assert b._ema8_prototype_state["arm"]["away_seen"] is True


def test_c2_resample_anchor_module_present():
    from analyst import nse_spot_candles_8510 as nse

    assert "09:15" in nse.NSE_SESSION_OPEN
    assert callable(nse.resample_nse_15m)
    assert callable(nse.current_nse_15m_open_ts)


def test_load_watchdog_cfg_defaults():
    cfg = load_watchdog_cfg({})
    assert cfg["enabled"] is True
    assert cfg["armed_idle_heartbeat_min"] == 10.0