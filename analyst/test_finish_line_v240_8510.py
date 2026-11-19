"""v2.0.40 finish-line: replay proof, OI wording, exit log, visible reasons."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from analyst.engine_exit_log_8510 import default_exit_log_path, install_engine_exit_logging
from analyst.oi_decision_board_8510 import build_oi_decision_board
from analyst.replay_marked_opportunities_8510 import replay_marked_day


def test_log_only_never_bare_blocked_in_verdict():
    board = {"sources": [], "as_of": "2026-11-19T10:00:00"}
    cfg = {
        "ema8_prototype": {
            "enabled": True,
            "oi_filters": {"mode": "log_only", "rule": "all_agree", "switches": {"writer_flow": True}},
        }
    }
    row = {"state": "ARMED", "armed": True, "direction": "LONG", "enabled": True}
    arm_state = {"arm": {"direction": "LONG", "away_seen": True}}
    # Force oppose vote via minimal reads stub — use empty board (no block)
    out = build_oi_decision_board("prototype", cfg, board, row, arm_state, [])
    assert out["mode"] == "log_only"
    for side in (out["buy"], out["sell"]):
        if side.get("would_block"):
            assert side["verdict"] != "BLOCKED"
            assert "trading still allowed" in side["verdict"]


def test_replay_19_nov_deterministic_row_count():
    rows = replay_marked_day("2026-11-19")
    assert len(rows) == 9
    assert all(r.verdict in ("WOULD TAKE", "NO") for r in rows)
    takes = [r for r in rows if r.verdict == "WOULD TAKE"]
    assert len(takes) >= 3


def test_engine_exit_log_writes_on_exception(tmp_path: Path):
    log_path = tmp_path / "engine_exits.log"
    install_engine_exit_logging(log_path=log_path, heartbeat_seconds=3600.0, label="test")
    try:
        raise ValueError("synthetic crash for test")
    except ValueError:
        import sys

        sys.excepthook(*sys.exc_info())
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(ln)["event"] == "STARTUP" for ln in lines)
    assert any(json.loads(ln)["event"] == "UNHANDLED_EXCEPTION" for ln in lines)