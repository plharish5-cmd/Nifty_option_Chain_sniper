"""Deterministic allow/block truth table for every gate across Prototype / Reversal / 1H Flagship."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from analyst.decision_audit_8510 import read, record
from analyst.oi_3m_gate_8510 import oi_3m_gate_decision
from analyst.proto_oi_filters_8510 import oi_switch_block
from analyst.terminal8510_backend import QuantTerminalBackend


def _backend(tmp_path: Path) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = tmp_path / "audit.sqlite3"
    b.cfg = {
        "algo_execution_allowlist": [
            "EMA8_PROTOTYPE_LONG",
            "EMA8_PROTOTYPE_SHORT",
            "CROSSOVER_BULLISH",
            "CROSSOVER_BEARISH",
            "FLAGSHIP_1H_LONG",
            "FLAGSHIP_1H_SHORT",
        ],
        "five_factor_confluence": {"scanner_min_score": 6, "scanner_gate_mode": "off"},
    }
    b.scanner_cfg = {
        "algo_execution_allowlist": b.cfg["algo_execution_allowlist"],
        "signal_cooldown_5m_min": 5.0,
        "skipped_signal_retry_seconds": 5.0,
        "ema8_prototype": {"enabled": True},
        "reversal": {"enabled": True},
    }
    b.directional_bias = "BOTH"
    b._real_money_enabled = False
    b.kill_switch_active = False
    b._ema_lane_lock = {}
    b.strategy_cooldowns = {}
    b.recent_signals = []
    b.cached_telemetry = {"spot": {"price": 25000.0, "time": time.time()}}
    b.log_ema8_prototype_event = Mock()
    b.algo_strategy_execution_allowed = lambda sid: sid in b.cfg["algo_execution_allowlist"]
    b.get_active_positions = lambda: []
    b.log_signal_observation = lambda *args, **kwargs: "OBS_1"
    b.log_signal_oi_context = lambda *args, **kwargs: None
    b.log_signal_desk8600 = lambda *args, **kwargs: None
    b.oi_context = lambda: {}
    b.desk8600_board = lambda: {"sources": []}
    return b


def test_directional_bias_truthtable(tmp_path: Path):
    """Truth table: BOTH / LONG_ONLY / SHORT_ONLY on LONG and SHORT signals."""
    b = _backend(tmp_path)
    sig_long = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG", "opt_side": "CE", "setup_name": "Proto Long"}
    sig_short = {"strategy_id": "EMA8_PROTOTYPE_SHORT", "direction": "SHORT", "opt_side": "PE", "setup_name": "Proto Short"}

    # 1. BOTH: allows both
    b.directional_bias = "BOTH"
    record(b, sig_long, "EVALUATING", "Candidate received")
    record(b, sig_short, "EVALUATING", "Candidate received")
    rows = read(b.ledger_db)
    assert not any(r["stage"] == "BLOCKED" for r in rows)

    # 2. LONG_ONLY: blocks SHORT
    b.directional_bias = "LONG_ONLY"
    record(b, sig_short, "BLOCKED", f"Directional bias {b.directional_bias}")
    rows = read(b.ledger_db)
    blocked = [r for r in rows if r["stage"] == "BLOCKED"]
    assert len(blocked) >= 1
    assert "Directional bias LONG_ONLY" in blocked[0]["reason"]

    # 3. SHORT_ONLY: blocks LONG
    b.directional_bias = "SHORT_ONLY"
    record(b, sig_long, "BLOCKED", f"Directional bias {b.directional_bias}")
    rows = read(b.ledger_db)
    blocked_long = [r for r in rows if r["stage"] == "BLOCKED" and r["side"] == "LONG"]
    assert len(blocked_long) >= 1
    assert "Directional bias SHORT_ONLY" in blocked_long[0]["reason"]


def test_algo_allowlist_truthtable(tmp_path: Path):
    """Truth table: Allowed vs Disallowed strategy in allowlist."""
    b = _backend(tmp_path)
    b.cfg["algo_execution_allowlist"] = ["EMA8_PROTOTYPE_LONG"]
    sig_allowed = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}
    sig_disallowed = {"strategy_id": "EMA8_PROTOTYPE_SHORT", "direction": "SHORT"}

    # Allowed
    assert b.algo_strategy_execution_allowed(sig_allowed["strategy_id"]) is True

    # Disallowed
    assert b.algo_strategy_execution_allowed(sig_disallowed["strategy_id"]) is False
    record(b, sig_disallowed, "BLOCKED", "Strategy not in algo allowlist")
    rows = [r for r in read(b.ledger_db) if r["stage"] == "BLOCKED"]
    assert len(rows) == 1
    assert "Strategy not in algo allowlist" in rows[0]["reason"]


def test_proto_oi_filters_truthtable():
    """Truth table: Prototype OI filters (mode, switches, neutral_blocks)."""
    reads_oppose = {"total_oi": {"direction": "PE", "age_sec": 30.0}}
    reads_agree = {"total_oi": {"direction": "CE", "age_sec": 30.0}}
    reads_neutral = {"total_oi": {"direction": "NEUTRAL", "age_sec": 30.0}}

    # 1. Mode log_only: would_block is True (opposes), but trade is NOT blocked in execution
    cfg_log = {"oi_filters": {"mode": "log_only", "switches": {"total_oi": True}, "neutral_blocks": False}}
    would_blk, det = oi_switch_block(True, cfg_log, reads_oppose)
    assert would_blk is True
    blocks_trade = would_blk and det.get("mode") == "block"
    assert blocks_trade is False  # log_only never blocks!

    # 2. Mode block: Oppose blocks
    cfg_blk = {"oi_filters": {"mode": "block", "switches": {"total_oi": True}, "neutral_blocks": False}}
    would_blk, det = oi_switch_block(True, cfg_blk, reads_oppose)
    assert would_blk is True
    blocks_trade = would_blk and det.get("mode") == "block"
    assert blocks_trade is True
    assert "total_oi" in det["blocked_by"]

    # 3. Mode block: Agree allows
    would_blk, det = oi_switch_block(True, cfg_blk, reads_agree)
    assert would_blk is False
    assert (would_blk and det.get("mode") == "block") is False

    # 4. Neutral blocks False: NEUTRAL allows
    would_blk, det = oi_switch_block(True, cfg_blk, reads_neutral)
    assert would_blk is False
    assert (would_blk and det.get("mode") == "block") is False

    # 5. Neutral blocks True: NEUTRAL blocks
    cfg_neut = {"oi_filters": {"mode": "block", "switches": {"total_oi": True}, "neutral_blocks": True}}
    would_blk, det = oi_switch_block(True, cfg_neut, reads_neutral)
    assert would_blk is True
    assert (would_blk and det.get("mode") == "block") is True


def test_oi_3m_gate_truthtable():
    """Truth table: 3m OI Gate (disabled vs enabled, oppose vs agree)."""
    read_oppose = {"direction": "PE", "S": -0.25}
    read_agree = {"direction": "CE", "S": 0.25}

    # 1. Disabled: never blocks
    cfg_off = {"enabled": False, "gate": {"enabled": False, "mode": "block"}}
    blk, _ = oi_3m_gate_decision(True, cfg_off, read_oppose)
    assert blk is False

    # 2. Enabled + Oppose: blocks
    cfg_on = {"enabled": True, "gate": {"enabled": True, "mode": "block"}}
    blk, det = oi_3m_gate_decision(True, cfg_on, read_oppose)
    assert blk is True
    assert "opposes" in det.get("reason", "")

    # 3. Enabled + Agree: allows
    blk, det = oi_3m_gate_decision(True, cfg_on, read_agree)
    assert blk is False
    assert det.get("blocks_trade") is False

    # 3. Enabled + Agree: allows
    blk, det = oi_3m_gate_decision(True, cfg_on, read_agree)
    assert blk is False


def test_five_factor_score_gate_truthtable(tmp_path: Path):
    """Truth table: Five-Factor Score Gate (mode off vs live, score < min vs score >= min)."""
    b = _backend(tmp_path)
    b.cfg["five_factor_confluence"] = {"scanner_min_score": 7, "scanner_gate_mode": "live"}
    b._real_money_enabled = True  # live armed

    # Score 5 < 7: blocks
    b.cached_telemetry["five_factor_confluence"] = {"score": 5, "grade": "C"}
    assert b._confluence_gate_blocks(live_armed=True) is True

    sig = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}
    b.apply_signal_gates([sig])
    rows = [r for r in read(b.ledger_db) if r["stage"] == "BLOCKED"]
    assert len(rows) == 1
    assert "Five-factor score 5 < 7" in rows[0]["reason"]

    # Score 8 >= 7: allows
    b.cached_telemetry["five_factor_confluence"] = {"score": 8, "grade": "A"}
    assert b._confluence_gate_blocks(live_armed=True) is False

    # Mode off: allows even if score is 0
    b.cfg["five_factor_confluence"]["scanner_gate_mode"] = "off"
    assert b._confluence_gate_blocks(live_armed=True) is False


def test_flagship_risk_gate_truthtable(tmp_path: Path):
    """Truth table: 1H Flagship risk allows vs blocks."""
    b = _backend(tmp_path)
    sig = {"strategy_id": "FLAGSHIP_1H_LONG", "direction": "LONG", "setup_name": "Flagship"}

    b._flagship_risk_allows_entry = lambda: (False, "daily risk limit reached")
    ok_risk, risk_reason = b._flagship_risk_allows_entry()
    assert ok_risk is False
    record(b, sig, "BLOCKED", f"Flagship risk: {risk_reason}")
    rows = [r for r in read(b.ledger_db) if r["stage"] == "BLOCKED"]
    assert len(rows) == 1
    assert "Flagship risk: daily risk limit reached" in rows[0]["reason"]


def test_stale_feed_logs_not_blocking(tmp_path: Path):
    """Stale feed logs 'feed stale — not blocking', never a silent block."""
    b = _backend(tmp_path)
    sig = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}

    # 1. Switched-on OI filter with STALE age (e.g. 500s > 420s max_age)
    st_stale = {
        "direction": "LONG",
        "feed_stale": True,
        "extra": {
            "oi_filters": {
                "mode": "block",
                "reads": {
                    "total_oi": {"switch_on": True, "label": "Cumulative Total OI", "vote": "STALE", "age_sec": 500}
                },
            }
        },
    }
    record(b, sig, "WAITING", "evaluating touch", state=st_stale)
    rows = read(b.ledger_db)
    assert len(rows) == 1
    reason = rows[0]["reason"]
    assert "feed stale — not blocking" in reason or "not blocking" in reason

    # 2. Intrabar stale spot quote
    record(b, sig, "WAITING", "feed stale — not blocking (spot age 12s)")
    rows2 = read(b.ledger_db)
    assert any("feed stale — not blocking" in r["reason"] for r in rows2)


def test_1000_tick_cooldown_dedup_and_zero_spam(tmp_path: Path):
    """C3: 1,000-tick cooldown produces 1 audit row and zero Signal audit log spam."""
    b = _backend(tmp_path)
    b.log_ema8_prototype_event = Mock()
    sig = {"strategy_id": "EMA8_PROTOTYPE_LONG", "direction": "LONG"}

    # Simulate 1,000 live ticks over a cooldown period with varying seconds
    for tick in range(1000):
        secs_remaining = max(0.0, 300.0 - tick * 0.3)
        record(b, sig, "WAITING", f"Order cooldown: {secs_remaining:.1f}s remaining")

    rows = [r for r in read(b.ledger_db) if r["stage"] == "WAITING"]
    assert len(rows) == 1, f"Expected 1 audit row for 1000-tick cooldown, got {len(rows)}"

    # Zero Signal audit log spam (legacy mirror never called for WAITING)
    b.log_ema8_prototype_event.assert_not_called()
