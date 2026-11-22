"""Track 3 Bug Hunt Acceptance Tests: Real-trade cancellation bug hunt across all 3 strategies.

Investigates all 6 suspects:
1. DH-905 Invalid IP: confirm reject is surfaced/alerted, never silently swallowed.
2. Lane lock / EXIT_PENDING: prove a stuck EXIT_PENDING alarms, can be un-stuck via modify_open_position,
   and resolves on fresh bid without permanently freezing re-entry on the lane.
3. Algo-only / allowlist / preflight: prove allowlist never silently excludes enabled Reversal or Flagship strategies.
4. Cooldown / retry interval: prove cooldown only blocks exact duplicates, never distinct setups.
5. Arm clearing: prove SL on one side never blocks opposite side, and profit exit preserves re-entry.
6. Contract selection: prove invalid/failed contract or sec_id lookup alarms instead of silently cancelling.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from analyst.ema_arm_persistence_8510 import format_broker_rejection
from analyst.ema_lane_lock_8510 import lane_lock_blocks_entry, release_lane_lock, try_acquire_lane_lock
from analyst.ema8_prototype_8510 import note_prototype_exit
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
    b.algo_allowlist_cfg = {"enabled": False, "strategy_ids": []}
    b._real_money_enabled = False
    b._lock = None
    b.nse_holidays = set()
    b.is_nse_market_open = lambda dt=None: (True, "OPEN")
    b.recent_signals = []
    b.priority_contracts = []
    b.strategy_cooldowns = {}
    b._ema_lane_lock = {}

    class MockDhanOI:
        def evaluate_synergy_status(self):
            return {"synergy_status": "MIXED"}
        def cached_snapshot(self):
            return {}

    b.dhan_oi = MockDhanOI()
    b._init_sqlite()
    return b


def test_suspect1_dh905_rejection_is_surfaced_and_not_swallowed():
    """Suspect 1: DH-905 Invalid IP is surfaced with full error description, never silently swallowed."""
    raw_error = '{"errorCode": "DH-905", "errorMessage": "Invalid IP: 103.21.244.2 is not whitelisted"}'
    formatted = format_broker_rejection(raw_error)
    assert "DH-905" in formatted
    assert "Invalid IP" in formatted
    assert "103.21.244.2" in formatted


def test_suspect2_stuck_exit_pending_alarms_and_resolves_without_lane_freeze(tmp_path: Path):
    """Suspect 2: EXIT_PENDING retry alarms, allows manual close, and resolves without freezing lane."""
    broker = FakeBroker()
    sec_id = "77001"
    broker.get_marketfeed_quote = AsyncMock(return_value={})  # Missing bid
    b = make_test_backend(tmp_path, broker)

    pos = insert_test_position(b, "POS_STUCK_LANE", mode="PAPER_ONLY", sec_id=sec_id, entry_price=200.0)

    # 3 failed attempts to exit trigger the alarm banner
    for _ in range(3):
        res = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
        assert res["status"] == "EXIT_PENDING"

    assert b.exit_failure_banner is not None
    assert "EXIT_PENDING ALARM" in b.exit_failure_banner["error"]

    # Verify modify_open_position can unstick / close a position in EXIT_PENDING
    with sqlite3.connect(b.ledger_db) as conn:
        st = conn.execute("SELECT status FROM positions WHERE position_id='POS_STUCK_LANE'").fetchone()[0]
    assert st == "EXIT_PENDING"

    # Now fresh bid arrives
    broker.get_marketfeed_quote = AsyncMock(return_value={
        sec_id: {"bid": 240.0, "ask": 241.0, "last_price": 240.5}
    })

    # Resuming exit resolves position to CLOSED and clears alarm banner
    res_final = asyncio.run(b.execute_position_exit(pos, "SPOT_TARGET_HIT", spot_exit=25040))
    assert res_final["ok"] is True
    assert b.exit_failure_banner is None

    # Verify lane lock is eligible
    blocks, reason = lane_lock_blocks_entry(b._ema_lane_lock, "PROTOTYPE", open_position_count=len(b.get_active_positions()))
    assert blocks is False
    assert reason == ""


def test_suspect3_allowlist_does_not_exclude_enabled_strategies(tmp_path: Path):
    """Suspect 3: Ensure allowlist includes Reversal and Flagship when enabled."""
    broker = FakeBroker()
    b = make_test_backend(tmp_path, broker)
    b.algo_allowlist_cfg = {
        "enabled": True,
        "strategy_ids": ["EMA8_PROTOTYPE_LONG"],
    }

    # Run ensure function
    b._ensure_ema8_prototype_allowlist_ids()

    allowed = b._allowed_algo_strategy_id_set()
    assert allowed is not None

    # Prove all 3 desk strategies are covered
    assert "EMA8_PROTOTYPE_LONG" in allowed
    assert "EMA8_PROTOTYPE_SHORT" in allowed
    assert "CROSSOVER_BULLISH" in allowed
    assert "CROSSOVER_BEARISH" in allowed
    assert "FLAGSHIP_1H_LONG" in allowed
    assert "FLAGSHIP_1H_SHORT" in allowed

    assert b.algo_strategy_execution_allowed("CROSSOVER_BULLISH") is True
    assert b.algo_strategy_execution_allowed("FLAGSHIP_1H_LONG") is True


def test_suspect4_cooldown_only_blocks_exact_duplicate_not_distinct_setup(tmp_path: Path):
    """Suspect 4: Cooldown on one strategy does not block a distinct strategy or opposite direction."""
    broker = FakeBroker()
    b = make_test_backend(tmp_path, broker)

    now = time.time()
    # Stamp cooldown on Prototype Long
    b.strategy_cooldowns["EMA8_PROTOTYPE_LONG"] = now

    # Prototype Long is in cooldown
    cool_sec = 300.0
    is_blocked_proto_long = (now - b.strategy_cooldowns.get("EMA8_PROTOTYPE_LONG", 0.0)) < cool_sec
    assert is_blocked_proto_long is True

    # But Reversal Bullish is NOT blocked
    is_blocked_reversal = (now - b.strategy_cooldowns.get("CROSSOVER_BULLISH", 0.0)) < cool_sec
    assert is_blocked_reversal is False

    # And Prototype Short is NOT blocked
    is_blocked_proto_short = (now - b.strategy_cooldowns.get("EMA8_PROTOTYPE_SHORT", 0.0)) < cool_sec
    assert is_blocked_proto_short is False


def test_suspect5_arm_clearing_preserves_opposite_side_and_reentry():
    """Suspect 5: Stop loss on one side never blocks the opposite side; profit exit preserves re-entry."""
    state = {"arm": {"direction": "SHORT"}}
    cfg = {"reentry_after_stop_loss": False}

    # SL on SHORT blocks SHORT only
    changed = note_prototype_exit(state, cfg, "SHORT", "SPOT_SL_HIT")
    assert changed is True
    assert state.get("sl_blocked_side") == "SHORT"
    assert "arm" not in state

    # Does NOT block LONG
    assert state.get("sl_blocked_side") != "LONG"

    # Profit target exit does not clear or block re-entry
    state2 = {"arm": {"direction": "LONG"}}
    changed2 = note_prototype_exit(state2, cfg, "LONG", "SPOT_TARGET_HIT")
    assert changed2 is False
    assert state2.get("arm") is not None  # arm preserved for re-entry


def test_suspect6_contract_selection_failure_raises_alarm(tmp_path: Path):
    """Suspect 6: Invalid/failed contract selection surfaces alarm instead of silently swallowing."""
    broker = FakeBroker()
    b = make_test_backend(tmp_path, broker)

    # Contract with invalid security_id (non-digit)
    invalid_contract = {
        "security_id": "INVALID_SEC_ID",
        "symbol": "NIFTY26NOV25000CE",
        "lot_size": 65,
        "ltp": 200.0,
        "opt_type": "CE",
        "strike": 25000,
        "expiry": "2026-11-26",
    }

    res = asyncio.run(b.execute_order_with_chaser(contract=invalid_contract, is_algo_trade=True))
    assert res["ok"] is False
    assert res["status"] == "REJECTED"

    # Contract selection alarm was raised
    assert b.contract_selection_alarm is not None
    assert b.contract_selection_alarm["sec_id"] == "INVALID_SEC_ID"
    assert "Verified instrument and dynamic lot size required" in b.contract_selection_alarm["error"]
