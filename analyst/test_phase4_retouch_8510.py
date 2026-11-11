"""Unit tests for Phase 4: Retouch must come from the correct side.

Acceptance criteria:
1. Candle after the cross opens inside the band and never leaves -> no entry.
2. Price moves 12 points above then returns to the line -> CE entry fires.
3. Mirror for PE: price moves 12 points below then returns to the line -> PE entry fires.
4. Price falls straight through the line for a CE arm without having been away -> no entry.
5. Config toggle: retouch_requires_away=False restores legacy overlap behavior.
6. Persistence: away_seen flag is saved to SQLite table ema8_prototype_state and reloaded across restarts.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    _arm_new,
    bar_open_timestamp,
    check_retouch,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
)
from analyst.terminal8510_backend import QuantTerminalBackend  # noqa: E402


def _make_b15(closes: List[float], start_time: str = "2026-11-10 09:15") -> pd.DataFrame:
    idx = pd.date_range(start_time, periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def test_1_candle_inside_band_never_leaves_no_entry():
    """Candle after the cross opens inside the band and never leaves -> no entry."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 5.0}})

    # Bar 9: dynamic 8 EMA = 25000. Spot stays inside 25000 ± 5 (25002, 25004, 24998, 25001)
    for spot in [25002.0, 25004.0, 24998.0, 25001.0]:
        sigs, st = evaluate_ema8_prototype(
            b15,
            bar_ci=9,
            cfg=cfg,
            proto_state=proto_state,
            session_day="2026-11-10",
            live_spot=spot,
            intrabar=True,
        )
        assert len(sigs) == 0
        assert proto_state["arm"]["away_seen"] is False
        assert "Waiting for price to move away" in st.get("block_reason", "")


def test_2_ce_away_seen_then_returns_to_line_entry_fires():
    """Price moves 12 points above dynamic 8 EMA then returns to the line -> CE entry fires."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 5.0}})

    # Tick 1 on bar 9: price moves 12 points above dynamic 8 EMA (dynamic e8 = 25000, spot = 25012)
    sigs1, st1 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25012.0,
        intrabar=True,
    )
    assert len(sigs1) == 0
    assert proto_state["arm"]["away_seen"] is True
    assert "Waiting for re-touch" in st1.get("block_reason", "")

    # Tick 2 on bar 9: price returns to the line (spot = 25003 <= 25000 + 5)
    sigs2, st2 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25003.0,
        intrabar=True,
    )
    assert len(sigs2) == 1
    assert sigs2[0]["direction"] == "LONG"
    assert sigs2[0]["opt_side"] == "CE"
    assert sigs2[0]["spot_entry"] == 25003.0
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("arm") is None


def test_3_pe_away_seen_then_returns_to_line_entry_fires():
    """PE mirror: price moves 12 points below dynamic 8 EMA then returns to the line -> PE entry fires."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="SHORT", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 5.0}})

    # Tick 1 on bar 9: price drops 12 points below dynamic 8 EMA (dynamic e8 = 25000, spot = 24988)
    sigs1, st1 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24988.0,
        intrabar=True,
    )
    assert len(sigs1) == 0
    assert proto_state["arm"]["away_seen"] is True
    assert "Waiting for re-touch" in st1.get("block_reason", "")

    # Tick 2 on bar 9: price rises back to the line (spot = 24997 >= 25000 - 5)
    sigs2, st2 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24997.0,
        intrabar=True,
    )
    assert len(sigs2) == 1
    assert sigs2[0]["direction"] == "SHORT"
    assert sigs2[0]["opt_side"] == "PE"
    assert sigs2[0]["spot_entry"] == 24997.0
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("arm") is None


def test_4_ce_falls_straight_through_without_away_no_entry():
    """Price falls straight through the line for a CE arm without having been away -> no entry."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 5.0}})

    # Bar 9: price opens at 25002 and falls straight down to 24985 without ever exceeding 25005
    for spot in [25002.0, 24998.0, 24990.0, 24985.0]:
        sigs, st = evaluate_ema8_prototype(
            b15,
            bar_ci=9,
            cfg=cfg,
            proto_state=proto_state,
            session_day="2026-11-10",
            live_spot=spot,
            intrabar=True,
        )
        assert len(sigs) == 0
        assert proto_state["arm"]["away_seen"] is False


def test_5_retouch_requires_away_toggle_false_restores_legacy_behavior():
    """When retouch_requires_away=False, old overlap behavior applies (enters inside band without moving away)."""
    arm = {"direction": "LONG", "away_seen": False}
    # Dynamic 8 EMA = 25000, spot = 25002 (inside band 25000 ± 5), away_seen is False
    res_default = check_retouch(arm, bullish=True, e8_val=25000.0, tol=5.0, spot=25002.0, retouch_requires_away=True)
    assert res_default is False

    # With toggle False, old overlap applies -> returns True immediately
    res_legacy = check_retouch(arm, bullish=True, e8_val=25000.0, tol=5.0, spot=25002.0, retouch_requires_away=False)
    assert res_legacy is True


def test_6_persistence_saves_and_reloads_away_seen(tmp_path: Path):
    """away_seen flag is persisted in SQLite table ema8_prototype_state and reloaded across restarts."""
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
            "away_seen": True,
        },
        "consumed_cross_ts": 0,
        "last_flip": "FLIPPED_TO_LONG_ARM",
    }
    b1._save_ema8_prototype_state()

    # Simulate restart by creating a second backend instance
    b2 = object.__new__(QuantTerminalBackend)
    b2.ledger_db = db_path
    b2.cfg = {}
    b2._init_sqlite()

    assert b2._ema8_prototype_state.get("arm") is not None
    assert b2._ema8_prototype_state["arm"]["direction"] == "LONG"
    assert b2._ema8_prototype_state["arm"]["away_seen"] is True
