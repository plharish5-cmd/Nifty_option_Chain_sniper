"""Unit tests for Phase 4: Retouch must come from the correct side (updated per Harish).

Acceptance criteria:
1. Candle after cross opens inside band (3.0 pts) and never leaves -> no entry.
2. Price moves > 3.0 points above then returns to within 3.0 points -> CE entry fires.
3. Mirror for PE: price moves > 3.0 points below then returns to within 3.0 points -> PE entry fires.
4. Price falls straight through the line for a CE arm without having been away -> no entry.
5. Config toggle: retouch_requires_away=False restores legacy overlap behavior.
6. Persistence: away_seen flag is saved to SQLite table ema8_prototype_state and reloaded across restarts.
7. Slow market exact line toggle: slow_market_exact_line=True (tolerance 0.0) requires exact line touch/cross.
8. Alternating CE and PE trades: one entry per cross; no repeated entry in same direction until opposite close.
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
    get_effective_touch_tolerance,
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
    """Candle after the cross opens inside the band (3.0 pts) and never leaves -> no entry."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 3.0}})
    assert get_effective_touch_tolerance(cfg) == 3.0

    # Bar 9: dynamic 8 EMA = 25000. Spot stays inside 25000 ± 3 (25001.0, 25002.5, 24998.0, 25000.5)
    for spot in [25001.0, 25002.5, 24998.0, 25000.5]:
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
    """Price moves > 3.0 points above dynamic 8 EMA then returns to within 3.0 points -> CE entry fires."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 3.0}})

    # Tick 1 on bar 9: price moves > 3.0 points above dynamic 8 EMA (dynamic e8 = 25000, spot = 25008)
    sigs1, st1 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25008.0,
        intrabar=True,
    )
    assert len(sigs1) == 0
    assert proto_state["arm"]["away_seen"] is True
    assert "Waiting for re-touch" in st1.get("block_reason", "")

    # Tick 2 on bar 9: price returns to the line (spot = 25002 <= 25000 + 3.0)
    sigs2, st2 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25002.0,
        intrabar=True,
    )
    assert len(sigs2) == 1
    assert sigs2[0]["direction"] == "LONG"
    assert sigs2[0]["opt_side"] == "CE"
    assert sigs2[0]["spot_entry"] == 25002.0
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("last_traded_direction") == "LONG"
    assert proto_state.get("arm") is None


def test_3_pe_away_seen_then_returns_to_line_entry_fires():
    """PE mirror: price moves > 3.0 points below dynamic 8 EMA then returns to within 3.0 points -> PE entry fires."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="SHORT", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 3.0}})

    # Tick 1 on bar 9: price drops > 3.0 points below dynamic 8 EMA (dynamic e8 = 25000, spot = 24992)
    sigs1, st1 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24992.0,
        intrabar=True,
    )
    assert len(sigs1) == 0
    assert proto_state["arm"]["away_seen"] is True
    assert "Waiting for re-touch" in st1.get("block_reason", "")

    # Tick 2 on bar 9: price rises back to the line (spot = 24998 >= 25000 - 3.0)
    sigs2, st2 = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=24998.0,
        intrabar=True,
    )
    assert len(sigs2) == 1
    assert sigs2[0]["direction"] == "SHORT"
    assert sigs2[0]["opt_side"] == "PE"
    assert sigs2[0]["spot_entry"] == 24998.0
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("last_traded_direction") == "SHORT"
    assert proto_state.get("arm") is None


def test_4_ce_falls_straight_through_without_away_no_entry():
    """Price falls straight through the line for a CE arm without having been away -> no entry."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 3.0}})

    # Bar 9: price opens at 25001 and falls straight down to 24985 without ever exceeding 25003
    for spot in [25001.0, 24998.0, 24990.0, 24985.0]:
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
    # Dynamic 8 EMA = 25000, spot = 25001 (inside band 25000 ± 3), away_seen is False
    res_default = check_retouch(arm, bullish=True, e8_val=25000.0, tol=3.0, spot=25001.0, retouch_requires_away=True)
    assert res_default is False

    # With toggle False, old overlap applies -> returns True immediately
    res_legacy = check_retouch(arm, bullish=True, e8_val=25000.0, tol=3.0, spot=25001.0, retouch_requires_away=False)
    assert res_legacy is True


def test_6_persistence_saves_and_reloads_away_seen(tmp_path: Path):
    """away_seen flag and last_traded_direction are persisted in SQLite and reloaded across restarts."""
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
        "last_traded_direction": "SHORT",
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
    assert b2._ema8_prototype_state.get("last_traded_direction") == "SHORT"


def test_7_slow_market_exact_line_requires_zero_tolerance():
    """Slow market mode: slow_market_exact_line=True uses touch_tol_slow_pts=0.0."""
    closes = [25000.0] * 10
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg(
        {
            "ema8_prototype": {
                "ribbon_filter_enabled": False,
                "slow_market_exact_line": True,
                "touch_tol_slow_pts": 0.0,
            }
        }
    )
    assert get_effective_touch_tolerance(cfg) == 0.0

    # 1. Price moves above line (25005.0) -> away_seen becomes True
    sigs1, _ = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25005.0,
        intrabar=True,
    )
    assert len(sigs1) == 0
    assert proto_state["arm"]["away_seen"] is True

    # 2. Price at 25001.0 (above exact line 25000.0) -> does NOT touch yet
    sigs2, _ = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25001.0,
        intrabar=True,
    )
    assert len(sigs2) == 0

    # 3. Price reaches exact 8 EMA line (25000.0) -> entry fires!
    sigs3, _ = evaluate_ema8_prototype(
        b15,
        bar_ci=9,
        cfg=cfg,
        proto_state=proto_state,
        session_day="2026-11-10",
        live_spot=25000.0,
        intrabar=True,
    )
    assert len(sigs3) == 1
    assert sigs3[0]["direction"] == "LONG"
    assert sigs3[0]["spot_entry"] == 25000.0


def test_8_alternating_ce_and_pe_trades_per_cross():
    """CE and PE alternate: after CE trade closes, no same direction entry until opposite 15m close."""
    closes = [25000.0] * 12
    b15 = _make_b15(closes)
    cross_ts = bar_open_timestamp(b15, 8)

    proto_state: Dict[str, Any] = {}
    _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25000.0)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False, "touch_tol_pts": 3.0}})

    # 1. CE entry fires on bar 9
    evaluate_ema8_prototype(b15, bar_ci=9, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", live_spot=25008.0, intrabar=True)
    sigs1, _ = evaluate_ema8_prototype(b15, bar_ci=9, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", live_spot=25002.0, intrabar=True)
    assert len(sigs1) == 1
    assert proto_state.get("last_traded_direction") == "LONG"
    assert proto_state.get("consumed_cross_ts") == cross_ts
    assert proto_state.get("arm") is None

    # 2. Trade closes (e.g. target or stop hit). Later touch on same bar/arm -> NO ENTRY
    sigs_same_arm, _ = evaluate_ema8_prototype(b15, bar_ci=9, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", live_spot=25002.0, intrabar=True)
    assert len(sigs_same_arm) == 0

    # 3. Bar 9 closes above 8 EMA. A bull cross does NOT re-arm LONG because last_traded_direction is LONG
    b15.iloc[9, b15.columns.get_loc("close")] = 25010.0
    evaluate_ema8_prototype(b15, bar_ci=9, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", intrabar=False)
    assert proto_state.get("arm") is None

    # 4. Bar 10 closes below 8 EMA (24980.0) -> OPPOSITE CLOSE arms SHORT (PE)!
    b15.iloc[10, b15.columns.get_loc("close")] = 24980.0
    b15.iloc[10, b15.columns.get_loc("open")] = 25005.0
    _, st_bear = evaluate_ema8_prototype(b15, bar_ci=10, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", intrabar=False)
    assert proto_state.get("arm") is not None
    assert proto_state["arm"]["direction"] == "SHORT"
    assert proto_state.get("last_traded_direction") is None

    # 5. On bar 11: PE entry fires on touch!
    cross_ts_pe = bar_open_timestamp(b15, 10)
    evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", live_spot=24970.0, intrabar=True)
    sigs_pe, _ = evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", live_spot=24995.0, intrabar=True)
    assert len(sigs_pe) == 1
    assert sigs_pe[0]["direction"] == "SHORT"
    assert sigs_pe[0]["opt_side"] == "PE"
    assert proto_state.get("last_traded_direction") == "SHORT"
    assert proto_state.get("consumed_cross_ts") == cross_ts_pe

    # 6. Bar 11 closes above 8 EMA (25020.0) -> OPPOSITE CLOSE arms LONG (CE) again!
    b15.iloc[11, b15.columns.get_loc("close")] = 25020.0
    b15.iloc[11, b15.columns.get_loc("open")] = 24975.0
    evaluate_ema8_prototype(b15, bar_ci=11, cfg=cfg, proto_state=proto_state, session_day="2026-11-10", intrabar=False)
    assert proto_state.get("arm") is not None
    assert proto_state["arm"]["direction"] == "LONG"
