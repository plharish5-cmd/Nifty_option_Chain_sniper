"""Unit tests for Phase 3: State timestamps, persistence, and one arm per cross.

Acceptance criteria:
1. cross_bar_open_ts: arm stores cross_bar_open_ts. Same candle refuses entry; subsequent candle permits entry.
2. consumed_cross_ts: one entry per cross. Subsequent retouch on same cross is blocked.
3. State persistence: SQLite table ema8_prototype_state stores and reloads arm across app restart.
4. Derive open: derived from positions table, never out of sync.
5. arm_until_opposite_close: arm persists across session day changes when True, clears when False.
6. spot_entry: real live spot at entry time, not 8 EMA.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    _arm_new,
    _maybe_flip_arm,
    bar_open_timestamp,
    evaluate_ema8_prototype,
    evaluate_ema8_prototype_intrabar,
    load_ema8_prototype_cfg,
    prototype_spot_is_long,
)
from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402


def _make_b15(closes: List[float], start_time: str = "2026-11-10 09:15") -> pd.DataFrame:
    idx = pd.date_range(start_time, periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 2.0 for c in closes],
            "low": [c - 2.0 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def test_1_cross_bar_open_ts_blocks_same_bar_permits_next():
    """Forming bar with open timestamp <= cross_bar_open_ts refuses entry; subsequent bar permits entry."""
    # 8 bars: bar 5 crosses above 8 EMA
    closes = [25000.0, 24990.0, 24980.0, 24970.0, 24960.0, 25010.0, 25010.0]
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 5)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=24980.0)

    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False}})

    # Intrabar test while still on the crossing candle (bar_ci = 5)
    sigs, st = evaluate_ema8_prototype(
        b15,
        bar_ci=5,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24980.0,  # Touches 8 EMA
        intrabar=True,
    )
    assert len(sigs) == 0
    assert "No entry on crossing candle" in st.get("block_reason", "")

    # On subsequent candle (bar_ci = 6)
    sigs_next, st_next = evaluate_ema8_prototype(
        b15,
        bar_ci=6,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24985.0,  # Touches 8 EMA
        intrabar=True,
    )
    assert len(sigs_next) == 1
    assert sigs_next[0]["direction"] == "LONG"


def test_2_consumed_cross_ts_blocks_second_entry_on_same_cross():
    """When an entry fires, consumed_cross_ts is set. A second touch on same cross does not fire."""
    closes = [25000.0, 24990.0, 24980.0, 24970.0, 24960.0, 25010.0, 25010.0, 25010.0]
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 5)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=24980.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False}})

    # First entry on bar 6
    sigs1, st1 = evaluate_ema8_prototype(
        b15,
        bar_ci=6,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24985.0,
        intrabar=True,
    )
    assert len(sigs1) == 1
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("arm") is None

    # Suppose user manually re-armed or state kept the cross without a fresh cross
    proto_state["arm"] = {"direction": "LONG", "cross_bar_open_ts": cross_ts, "cross_e8": 24980.0}
    sigs2, st2 = evaluate_ema8_prototype(
        b15,
        bar_ci=7,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24985.0,
        intrabar=True,
    )
    assert len(sigs2) == 0
    assert "Cross already consumed" in st2.get("block_reason", "")


def test_3_state_persistence_across_app_restart(tmp_path: Path):
    """SQLite table ema8_prototype_state persists arm, cross_bar_open_ts, and consumed_cross_ts across restart."""
    db_path = tmp_path / "ledger.sqlite3"

    b1 = object.__new__(QuantTerminalBackend)
    b1.ledger_db = db_path
    b1.cfg = {}
    b1._init_sqlite()

    b1._ema8_prototype_state = {
        "arm": {
            "direction": "LONG",
            "cross_bar_open_ts": 1794350000,
            "cross_e8": 25010.5,
        },
        "consumed_cross_ts": 1794340000,
        "last_flip": "FLIPPED_TO_LONG_ARM",
    }
    b1._save_ema8_prototype_state()

    # Simulate restart by creating a new backend instance pointing to same db
    b2 = object.__new__(QuantTerminalBackend)
    b2.ledger_db = db_path
    b2.cfg = {}
    b2._init_sqlite()

    assert b2._ema8_prototype_state.get("arm") is not None
    assert b2._ema8_prototype_state["arm"]["direction"] == "LONG"
    assert b2._ema8_prototype_state["arm"]["cross_bar_open_ts"] == 1794350000
    assert b2._ema8_prototype_state["arm"]["cross_e8"] == pytest.approx(25010.5, abs=0.01)
    assert b2._ema8_prototype_state.get("consumed_cross_ts") == 1794340000
    assert b2._ema8_prototype_state.get("last_flip") == "FLIPPED_TO_LONG_ARM"


def test_4_derive_open_position_from_ledger(tmp_path: Path):
    """open position state is derived from positions table, never out of sync."""
    db_path = tmp_path / "ledger.sqlite3"

    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = db_path
    b.cfg = {}
    b._init_sqlite()

    # Initially no open position
    b._sync_open_ema8_prototype_position()
    assert b._ema8_prototype_state.get("open") is None

    # Insert open prototype position
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO positions (
                position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                current_ltp, target_price, sl_price, status, mode, strategy_id, spot_entry,
                spot_target_level, spot_stop_level
            ) VALUES (
                'POS_PROTO_1', 'NIFTY 25000 CE', '54321', 'BUY', 1, 65, 200.0,
                '2026-11-10 10:00:00 IST', 200.0, 240.0, 160.0, 'OPEN', 'LIVE',
                'EMA8_PROTOTYPE_LONG', 25000.0, 25040.0, 24960.0
            )
            """
        )
        conn.commit()

    b._sync_open_ema8_prototype_position()
    assert b._ema8_prototype_state.get("open") is not None
    assert b._ema8_prototype_state["open"]["direction"] == "LONG"
    assert b._ema8_prototype_state["open"]["position_id"] == "POS_PROTO_1"

    # Close position in ledger
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE positions SET status='CLOSED' WHERE position_id='POS_PROTO_1'")
        conn.commit()

    b._sync_open_ema8_prototype_position()
    assert b._ema8_prototype_state.get("open") is None


def test_5_arm_until_opposite_close_persists_across_days():
    """arm persists across session_day change when arm_until_opposite_close=True, clears when False."""
    closes = [25000.0, 24990.0, 24980.0, 24970.0, 24960.0, 25010.0, 25010.0]
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 5)

    # 1. With arm_until_opposite_close = True (default)
    state1: Dict[str, Any] = {
        "day": "2026-11-09",
        "arm": {"direction": "LONG", "cross_bar_open_ts": cross_ts, "cross_e8": 24980.0},
    }
    cfg1 = load_ema8_prototype_cfg({"ema8_prototype": {"arm_until_opposite_close": True}})
    evaluate_ema8_prototype(b15, bar_ci=6, cfg=cfg1, proto_state=state1, session_day="2026-11-10")
    assert state1.get("arm") is not None
    assert state1["day"] == "2026-11-10"

    # 2. With arm_until_opposite_close = False
    state2: Dict[str, Any] = {
        "day": "2026-11-09",
        "arm": {"direction": "LONG", "cross_bar_open_ts": cross_ts, "cross_e8": 24980.0},
    }
    cfg2 = load_ema8_prototype_cfg({"ema8_prototype": {"arm_until_opposite_close": False}})
    evaluate_ema8_prototype(b15, bar_ci=6, cfg=cfg2, proto_state=state2, session_day="2026-11-10")
    assert state2.get("arm") is None


def test_6_spot_entry_records_real_spot_not_8ema():
    """spot_entry stores real live spot at entry time, NOT dynamic 8 EMA."""
    closes = [25000.0, 24990.0, 24980.0, 24970.0, 24960.0, 25010.0, 25010.0]
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 5)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=24980.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 5.0}})

    live_spot_tick = 24982.50
    sigs, st = evaluate_ema8_prototype(
        b15,
        bar_ci=6,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=live_spot_tick,
        intrabar=True,
    )
    assert len(sigs) == 1
    sig = sigs[0]
    # Real spot entry
    assert sig["index_price"] == 24982.50
    assert sig["spot_entry"] == 24982.50
    # Dynamic 8 EMA stored separately
    assert sig["dynamic_e8_at_entry"] != sig["spot_entry"]
