"""Unit tests for Phase 7: Cockpit and log.

Validates:
1. SQLite log table ema8_prototype_log persistence and retrieval across simulated restart.
2. Strategy card plain English lifecycle states (6 phases):
   - WAITING FOR CLOSE
   - ARMED CE / ARMED PE
   - WAITING FOR RE-TOUCH (away_seen: YES/NO)
   - ENTRY
   - EXITED — <reason>
   - FLIPPED TO EMA SHORT / LONG
3. Filter block reasons appear ONLY when filter is ON:
   - 4-8 Attached (Ribbon ON)
   - EMA Separation
   - SuperTrend
   - Opposite Close — Exited
   - EXIT FAILED — <Dhan error>
4. Cockpit filter config updates for retouch_requires_away and slow_market_exact_line.
5. Active positions row reflects EXIT_PENDING and EXIT_FAILED.
6. HTML workspace contains new controls, status indicators, and DOM bindings.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import pandas as pd
import pytest

from analyst.ema8_prototype_8510 import (
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
    _filter_blocks,
)
from analyst.terminal8510_backend import QuantTerminalBackend


class FakeBroker:
    def __init__(self):
        self._ltp_cache = {}
        self._cache_timestamps = {}
        self.client_id = "FAKE_CLIENT"

    def is_configured(self):
        return False


def _make_bars(closes, start_time="2026-11-05 09:15"):
    idx = pd.date_range(start_time, periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "open": [c - 1 for c in closes],
            "high": [c + 3 for c in closes],
            "low": [c - 3 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _make_temp_backend(tmpdir):
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    db_path = os.path.join(tmpdir, "test_ledger.sqlite3")
    cfg_base = json.loads(Path("analyst/terminal8510_config.json").read_text())
    cfg_base["ledger_db_path"] = db_path
    cfg_path = os.path.join(tmpdir, "test_config.json")
    Path(cfg_path).write_text(json.dumps(cfg_base))
    backend = QuantTerminalBackend(cfg_path)
    backend.broker = FakeBroker()
    return backend, db_path, cfg_path


def test_phase7_sqlite_log_persistence_across_restart():
    """Unit test: SQLite log persists and retrieves transition events across a restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. First session: log transition events
        backend1, db_path, cfg_path = _make_temp_backend(tmpdir)
        backend1.log_ema8_prototype_event(
            event="ARMED",
            direction="LONG",
            dynamic_8ema=24200.5,
            spot=24205.0,
            reason="ARMED LONG",
            time_str="09:30:00",
        )
        backend1.log_ema8_prototype_event(
            event="RETOUCH SEEN",
            direction="LONG",
            dynamic_8ema=24202.0,
            spot=24201.5,
            reason="DYNAMIC 8 EMA TOUCHED",
            time_str="09:32:00",
        )
        backend1.log_ema8_prototype_event(
            event="ENTRY PLACED",
            direction="LONG",
            dynamic_8ema=24202.0,
            spot=24201.5,
            position_id="POS_701",
            reason="ENTRY @ ₹205.50",
            time_str="09:32:05",
        )
        backend1.log_ema8_prototype_event(
            event="TARGET HIT",
            direction="LONG",
            dynamic_8ema=24205.0,
            spot=24245.0,
            position_id="POS_701",
            reason="SPOT_TARGET_HIT",
            time_str="09:45:00",
        )
        backend1.log_ema8_prototype_event(
            event="OPPOSITE CLOSE",
            direction="SHORT",
            dynamic_8ema=24210.0,
            spot=24190.0,
            position_id="POS_702",
            reason="OPPOSITE_8EMA_CLOSE",
            time_str="10:15:00",
        )
        backend1.log_ema8_prototype_event(
            event="EOD SQUARE OFF",
            direction="SHORT",
            dynamic_8ema=24180.0,
            spot=24185.0,
            position_id="POS_703",
            reason="EOD_SQUARE_OFF_15:20",
            time_str="15:20:00",
        )
        backend1.log_ema8_prototype_event(
            event="CANCELLED",
            direction="LONG",
            reason="EOD_SQUARE_OFF_15:20",
            time_str="15:20:01",
        )
        backend1.log_ema8_prototype_event(
            event="EXIT FAILED",
            direction="LONG",
            reason="EXIT FAILED — Exchange link down",
            position_id="POS_704",
            time_str="15:20:05",
        )

        # 2. Simulate desk restart pointing to the same SQLite database
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        backend2 = QuantTerminalBackend(cfg_path)
        backend2.broker = FakeBroker()
        log_rows = backend2.get_ema8_prototype_log(limit=50)

        assert len(log_rows) == 8
        events = [r["event"] for r in log_rows]
        assert events == [
            "EXIT FAILED",
            "CANCELLED",
            "EOD SQUARE OFF",
            "OPPOSITE CLOSE",
            "TARGET HIT",
            "ENTRY PLACED",
            "RETOUCH SEEN",
            "ARMED",
        ]

        # Verify attributes on retrieved rows
        ef = log_rows[0]
        assert ef["event"] == "EXIT FAILED"
        assert ef["position_id"] == "POS_704"
        assert "Exchange link down" in ef["reason"]

        tgt = [r for r in log_rows if r["event"] == "TARGET HIT"][0]
        assert tgt["dynamic_8ema"] == 24205.0
        assert tgt["spot"] == 24245.0
        assert tgt["position_id"] == "POS_701"
        assert tgt["direction"] == "LONG"


def test_phase7_card_state_6_phases():
    """Unit test: Card state string matches the 6 phases specified by Harish:
    1. WAITING FOR CLOSE
    2. ARMED CE / ARMED PE
    3. WAITING FOR RE-TOUCH (away_seen: YES/NO)
    4. ENTRY
    5. EXITED — <reason>
    6. FLIPPED TO EMA SHORT / LONG
    """
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {}})

    # Phase 1: WAITING FOR CLOSE (no arm)
    closes_flat = [100.0] * 12
    b15 = _make_bars(closes_flat)
    state = {}
    _, st = evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=state, session_day="2026-11-05")
    assert st["phase"] == "WAITING FOR CLOSE"

    # Phase 2: ARMED CE on crossing candle
    closes_cross_long = [100.0] * 11 + [108.0]
    b15_cross = _make_bars(closes_cross_long)
    state_long = {}
    _, st_cross = evaluate_ema8_prototype(b15_cross, bar_ci=11, cfg=cfg, proto_state=state_long, session_day="2026-11-05")
    assert st_cross["phase"] == "ARMED CE"

    # Phase 2 (PE): ARMED PE on crossing candle
    closes_cross_short = [100.0] * 11 + [92.0]
    b15_short = _make_bars(closes_cross_short)
    state_short = {}
    _, st_short = evaluate_ema8_prototype(b15_short, bar_ci=11, cfg=cfg, proto_state=state_short, session_day="2026-11-05")
    assert st_short["phase"] == "ARMED PE"

    # Phase 3: WAITING FOR RE-TOUCH (away_seen: NO)
    # Next bar close is at 104.0, within band, so away is not seen yet
    b15_next = _make_bars(closes_cross_long + [104.0])
    _, st_wait_no = evaluate_ema8_prototype(b15_next, bar_ci=12, cfg=cfg, proto_state=state_long, session_day="2026-11-05")
    assert st_wait_no["phase"] == "WAITING FOR RE-TOUCH (away_seen: NO)"

    # Phase 3 (away seen): WAITING FOR RE-TOUCH (away_seen: YES)
    # Price moved away (> 3 pts above 8 EMA ~103) e.g. 115.0
    b15_away = _make_bars(closes_cross_long + [115.0])
    state_long["arm"]["away_seen"] = True
    _, st_wait_yes = evaluate_ema8_prototype(b15_away, bar_ci=12, cfg=cfg, proto_state=state_long, session_day="2026-11-05")
    assert st_wait_yes["phase"] == "WAITING FOR RE-TOUCH (away_seen: YES)"

    # Phase 4: ENTRY (in position or triggered)
    state_long["open"] = {"direction": "LONG", "symbol": "NIFTY26NOV24200CE"}
    _, st_open = evaluate_ema8_prototype(b15_next, bar_ci=12, cfg=cfg, proto_state=state_long, session_day="2026-11-05")
    assert st_open["phase"] == "ENTRY"

    # Phase 5: EXITED — <reason>
    state_closed = {"last_exit_reason": "SPOT_TARGET_HIT"}
    _, st_exited = evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=state_closed, session_day="2026-11-05")
    assert st_exited["phase"] == "EXITED — SPOT_TARGET_HIT"

    state_eod = {"last_exit_reason": "EOD_SQUARE_OFF_15:20"}
    _, st_eod = evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=state_eod, session_day="2026-11-05")
    assert st_eod["phase"] == "EXITED — EOD_SQUARE_OFF_15:20"

    # Phase 6: FLIPPED TO EMA SHORT / LONG
    state_flip = {
        "arm": {"direction": "LONG", "cross_bar_open_ts": 1000},
    }
    # Bar closes below -> flips arm to SHORT
    closes_flip = [100.0] * 11 + [90.0]
    b15_flip = _make_bars(closes_flip)
    _, st_flip = evaluate_ema8_prototype(b15_flip, bar_ci=11, cfg=cfg, proto_state=state_flip, session_day="2026-11-05")
    assert st_flip["phase"] == "FLIPPED TO EMA SHORT"


def test_phase7_filter_block_reasons_only_when_on():
    """Unit test: Filter block reasons appear only when the filter is ON."""
    closes = [100.0] * 12
    b15 = _make_bars(closes)

    # 1. Ribbon filter
    cfg_ribbon_off = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "ribbon_max_pts": 5.0}})
    block_off = _filter_blocks(bullish=True, e4=100.0, e8=100.0, b15=b15, cfg=cfg_ribbon_off, bar_ci=11)
    assert block_off == ""

    cfg_ribbon_on = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": True, "ribbon_max_pts": 5.0}})
    block_on = _filter_blocks(bullish=True, e4=100.0, e8=100.0, b15=b15, cfg=cfg_ribbon_on, bar_ci=11)
    assert block_on == "4-8 Attached (Ribbon ON)"

    # 2. EMA Separation filter
    cfg_sep_off = load_ema8_prototype_cfg({"ema8_prototype": {"ema_separation_enabled": False}})
    block_sep_off = _filter_blocks(bullish=True, e4=95.0, e8=100.0, b15=b15, cfg=cfg_sep_off, bar_ci=11)
    assert block_sep_off == ""

    cfg_sep_on = load_ema8_prototype_cfg({"ema8_prototype": {"ema_separation_enabled": True}})
    block_sep_on = _filter_blocks(bullish=True, e4=95.0, e8=100.0, b15=b15, cfg=cfg_sep_on, bar_ci=11)
    assert block_sep_on == "EMA Separation"

    # 3. SuperTrend filter
    cfg_st_off = load_ema8_prototype_cfg({"ema8_prototype": {"supertrend_enabled": False}})
    block_st_off = _filter_blocks(bullish=False, e4=95.0, e8=100.0, b15=b15, cfg=cfg_st_off, bar_ci=11)
    assert block_st_off == ""

    cfg_st_on = load_ema8_prototype_cfg({"ema8_prototype": {"supertrend_enabled": True}})
    # On flat bars, SuperTrend is UP -> blocks bearish direction (bullish=False)
    block_st_on = _filter_blocks(bullish=False, e4=95.0, e8=100.0, b15=b15, cfg=cfg_st_on, bar_ci=11)
    assert block_st_on == "SuperTrend"

    # 4. Special block reasons: Opposite Close
    state_opp = {"last_exit_reason": "OPPOSITE_8EMA_CLOSE"}
    _, st_opp = evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg_ribbon_off, proto_state=state_opp, session_day="2026-11-05")
    assert st_opp["block_reason"] == "Opposite Close — Exited"


def test_phase7_cockpit_filter_config_updates():
    """Unit test: Cockpit filter config updates for retouch_requires_away and slow_market_exact_line."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend, db_path, cfg_path = _make_temp_backend(tmpdir)

        # Initial defaults
        cfg = backend.get_ema8_prototype_filter_config()
        assert cfg["retouch_requires_away"] is True
        assert cfg["slow_market_exact_line"] is False
        assert cfg["touch_tol_pts"] == 3.0
        assert cfg["touch_tol_slow_pts"] == 0.0

        # Update via cockpit endpoint handler
        updated = backend.update_ema8_prototype_filter_config({
            "retouch_requires_away": False,
            "slow_market_exact_line": True,
            "ribbon_filter_enabled": True,
        })
        assert updated["retouch_requires_away"] is False
        assert updated["slow_market_exact_line"] is True
        assert updated["ribbon_filter_enabled"] is True

        # Verify persisted to file
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        proto_cfg = disk["scanner"]["ema8_prototype"]
        assert proto_cfg["retouch_requires_away"] is False
        assert proto_cfg["slow_market_exact_line"] is True


def test_phase7_active_positions_row_exit_pending_and_failed():
    """Unit test: Active positions table reflects EXIT_PENDING and EXIT_FAILED states."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend, db_path, cfg_path = _make_temp_backend(tmpdir)

        with sqlite3.connect(db_path) as conn:
            conn.execute("""
            INSERT INTO positions (
                position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                status, mode, strategy_id, exit_reason
            ) VALUES
            ('POS_OPEN_1', 'NIFTY26NOV24200CE', '12345', 'BUY', 1, 65, 200.0, '2026-11-05 09:30:00 IST', 'OPEN', 'PAPER', 'EMA8_PROTOTYPE_LONG', NULL),
            ('POS_PEND_2', 'NIFTY26NOV24250PE', '12346', 'BUY', 1, 65, 210.0, '2026-11-05 10:00:00 IST', 'EXIT_PENDING', 'LIVE', 'EMA8_PROTOTYPE_SHORT', 'SPOT_TARGET_HIT'),
            ('POS_FAIL_3', 'NIFTY26NOV24300CE', '12347', 'BUY', 1, 65, 205.0, '2026-11-05 11:00:00 IST', 'EXIT_FAILED', 'LIVE', 'EMA8_PROTOTYPE_LONG', 'EXIT_FAILED: Broker connection dropped')
            """)
            conn.commit()

        active = backend.get_active_positions()
        statuses = {p["position_id"]: p["status"] for p in active}
        assert statuses.get("POS_OPEN_1") == "OPEN"
        assert statuses.get("POS_PEND_2") == "EXIT_PENDING"
        assert statuses.get("POS_FAIL_3") == "EXIT_FAILED"

        fail_pos = [p for p in active if p["position_id"] == "POS_FAIL_3"][0]
        assert "Broker connection dropped" in fail_pos["exit_reason"]


def test_phase7_cockpit_html_dom_elements():
    """Manual/DOM verification: Cockpit HTML includes controls and status indicators."""
    html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Algorithmic_Trading_Workspace_8510.html")
    assert os.path.exists(html_path), f"Workspace HTML not found at {html_path}"

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. New cockpit radio controls
    assert 'id="protoAwayOn"' in html
    assert 'id="protoAwayOff"' in html
    assert 'id="protoSlowOff"' in html
    assert 'id="protoSlowOn"' in html

    # 2. Table header Status / Source
    assert "<th>Status / Source</th>" in html

    # 3. JS bindings for EXIT_PENDING and EXIT_FAILED in renderPositionsTable
    assert 'EXIT_PENDING' in html
    assert 'EXIT_FAILED' in html
    assert 'p.status === "EXIT_PENDING"' in html
    assert 'p.status === "EXIT_FAILED"' in html

    # 4. JS bindings for radio controls save & apply
    assert 'protoAwayOn' in html
    assert 'protoSlowOn' in html
    assert 'retouch_requires_away' in html
    assert 'slow_market_exact_line' in html

    # 5. Plain English filter block strings
    assert '4-8 Attached (Ribbon ON)' in html
    assert 'EMA Separation' in html
    assert 'Opposite Close — Exited' in html
