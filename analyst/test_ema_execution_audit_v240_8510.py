"""Regressions for missed EMA Prototype / Reversal candidates (mocked broker)."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from analyst.oi_decision_board_8510 import build_oi_decision_board
from analyst.terminal8510_backend import QuantTerminalBackend
from analyst.test_reversal_crosslane_8510 import _cross_sig, _proto_sig


def stub(tmp_path):
    b = QuantTerminalBackend.__new__(QuantTerminalBackend)
    b._ema_lane_lock = {}
    b._real_money_enabled = True
    b.kill_switch_active = False
    b.cfg = {"execution_safety_v194": {"live_validation_status": "USER_CONTROLLED"}}
    b.scanner_cfg = {
        "signal_cooldown_5m_min": 0,
        "skipped_signal_retry_seconds": 0,
        "reversal": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True, "oi_filters": {"enabled": False}},
        "ema8_prototype": {"execution_mode": "LIVE_ALLOWED", "live_trade_enabled": True},
        "desk8600_board": {"enabled": False},
    }
    b.directional_bias = "BOTH"
    b.strategy_cooldowns = {}
    b.recent_signals = []
    b.cached_telemetry = {"spot": {"price": 25100.0}}
    b._skip_retry_at = {}
    b.get_active_positions = lambda: []
    b.algo_strategy_execution_allowed = lambda sid: True
    b.log_signal_observation = lambda *a, **k: "unit"
    b.mark_signal_outcome = lambda *a, **k: None
    b.log_signal_oi_context = lambda *a, **k: None
    b.log_signal_desk8600 = lambda *a, **k: None
    b.oi_context = lambda: {}
    b.desk8600_board = lambda: {"sources": []}
    b._select_ema8_prototype_contract_any_strike = AsyncMock(
        return_value=({"symbol": "FIXTURE", "security_id": "1"}, "")
    )
    b.execute_order_with_chaser = AsyncMock(return_value={"ok": True, "position_id": "fixture"})
    b._sync_open_ema8_prototype_position = lambda: None
    b._save_ema8_prototype_state = lambda: None
    b.ledger_db = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(b.ledger_db) as c:
        c.execute("create table positions (symbol text, status text)")
    return b


def test_batch_preserves_candidates_for_retry(tmp_path):
    b = stub(tmp_path)
    signals = [_proto_sig(), _cross_sig()]
    assert b.apply_signal_gates(signals) == signals


def test_zero_cooldown_after_previous_order(tmp_path):
    b = stub(tmp_path)
    b.strategy_cooldowns["CROSSOVER_BULLISH"] = time.time() - 1
    asyncio.run(b._process_strategy_signals([_cross_sig()]))
    assert b.execute_order_with_chaser.await_count == 1


@pytest.mark.parametrize("mode,log_only,expected", [("block", False, 0), ("log_only", True, 1)])
def test_reversal_oi_nested_shape(tmp_path, mode, log_only, expected):
    b = stub(tmp_path)
    b.scanner_cfg["reversal"]["oi_filters"] = {
        "enabled": True,
        "mode": mode,
        "log_only": log_only,
        "rule": "all_agree",
        "switches": {"writer_flow": True},
    }
    b.desk8600_board = lambda: {"sources": [{"id": "writer_flow", "direction": "PE", "age_sec": 1}]}
    asyncio.run(b._process_strategy_signals([_cross_sig()]))
    assert b.execute_order_with_chaser.await_count == expected
    view = build_oi_decision_board("reversal", b.scanner_cfg, b.desk8600_board())
    assert view["buy"]["would_block"] is True
    assert view["buy"]["actual_block"] is (expected == 0)


def test_rejected_first_allows_second(tmp_path):
    b = stub(tmp_path)
    b.execute_order_with_chaser.side_effect = [
        {"ok": False, "error": "fixture rejection"},
        {"ok": True},
    ]
    asyncio.run(b._process_strategy_signals(b.apply_signal_gates([_proto_sig(), _cross_sig()])))
    assert b.execute_order_with_chaser.await_count == 2


def test_same_lane_pending_order_cannot_double_fill(tmp_path):
    b = stub(tmp_path)
    asyncio.run(b._process_strategy_signals([_cross_sig(), _cross_sig()]))
    assert b.execute_order_with_chaser.await_count == 1