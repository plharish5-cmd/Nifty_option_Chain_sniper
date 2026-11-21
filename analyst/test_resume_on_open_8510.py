"""Tests for Resume-on-Open semantics, historical candle assessment, and SL-blocking."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from analyst.ema8_prototype_8510 import (
    _arm_new,
    bar_open_timestamp,
    evaluate_ema8_prototype_intrabar,
    load_ema8_prototype_cfg,
)
from analyst.ema_arm_persistence_8510 import bootstrap_prototype_arm
from analyst.prototype_arm_recovery_8510 import recover_missing_arm
from analyst.terminal8510_backend import QuantTerminalBackend


def _mk_b15(
    closes: list[float],
    day: str = "2026-11-21",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs if highs is not None else [c + 5 for c in closes],
            "low": lows if lows is not None else [c - 5 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _backend(tmp_path: Path) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.cfg = {"scanner": {"ema8_prototype": {"enabled": True, "reentry_same_side": True, "away_min_pts": 2.0}}}
    b.scanner_cfg = b.cfg["scanner"]
    b._ema8_prototype_state = {}
    b._mj_sir_state = {}
    b._ema_lane_lock = {}
    b._proto_arm_loaded_from_db = False
    b._mj_sir_loaded_from_db = False
    b.log_ema8_prototype_event = Mock()
    b._init_sqlite()
    return b


def test_save_arm_restart_assert_resumed_not_recovered(tmp_path: Path):
    """C1: save arm → restart → assert ARM RESUMED (not RECOVERED)."""
    day = "2026-11-21"
    b1 = _backend(tmp_path)
    b15 = _mk_b15([25000.0] * 8 + [25100.0] * 4, day=day)
    cross_ts = int(b15.index[8].timestamp())
    _arm_new(
        b1._ema8_prototype_state,
        direction="LONG",
        cross_bar_open_ts=cross_ts,
        e8=25080.0,
        away_seen=True,
    )
    b1._ema8_prototype_state["day"] = day
    b1._save_ema8_prototype_state()

    # Clean restart
    b2 = _backend(tmp_path)
    assert b2._proto_arm_loaded_from_db is True
    b2.log_ema8_prototype_event = Mock()
    proto_cfg = load_ema8_prototype_cfg(b2.scanner_cfg)

    mode = bootstrap_prototype_arm(b2, b15, proto_cfg, day)
    assert mode == "resumed"

    # Verify event logged was ARM RESUMED, never RECOVERED
    events = [call.kwargs.get("event") for call in b2.log_ema8_prototype_event.call_args_list]
    assert "ARM RESUMED" in events
    assert "ARM RECOVERED" not in events
    assert "ARM REBUILT" not in events


def test_absent_state_recovered_and_away_seen_primed_first_live_touch_trades(tmp_path: Path):
    """C2: absent state → RECOVERED + away_seen primed → first live touch trades."""
    day = "2026-11-21"
    b = _backend(tmp_path)
    # No saved state in database: _proto_arm_loaded_from_db is False
    assert b._proto_arm_loaded_from_db is False

    # Completed bars with clear LONG cross + demonstrated move away (high clears e8 + 2)
    closes = [25000.0] * 8 + [25100.0, 25100.0, 25100.0, 25100.0]
    b15 = _mk_b15(closes, day=day)
    proto_cfg = load_ema8_prototype_cfg(b.scanner_cfg)

    mode = bootstrap_prototype_arm(b, b15, proto_cfg, day)
    assert mode == "recovered"
    events = [call.kwargs.get("event") for call in b.log_ema8_prototype_event.call_args_list]
    assert "ARM RECOVERED" in events

    arm = b._ema8_prototype_state.get("arm")
    assert arm is not None
    assert arm["direction"] == "LONG"
    assert arm["away_seen"] is True  # Primed from completed candles!

    # Very first live touch arrives at 8EMA: evaluate intrabar
    spot = arm["cross_e8"]
    sigs, _ = evaluate_ema8_prototype_intrabar(
        b15,
        spot,
        cfg=proto_cfg,
        proto_state=b._ema8_prototype_state,
        session_day=day,
        gate_meta=lambda _k: {},
        tick_high=spot + 0.5,
        tick_low=spot - 0.5,
        now_hm="10:30",
    )
    # Trades immediately on first touch (not wasted on priming away_seen)
    assert len(sigs) >= 1
    assert sigs[0]["strategy_id"].startswith("EMA8_PROTOTYPE")
    assert sigs[0]["direction"] == "LONG"


def test_historical_downtime_touch_is_not_back_traded(tmp_path: Path):
    """Historical touches that occurred during down-time must never generate back-dated orders."""
    day = "2026-11-21"
    b = _backend(tmp_path)
    closes = [25000.0] * 8 + [25100.0, 25100.0, 25050.0, 25100.0]
    b15 = _mk_b15(closes, day=day)
    proto_cfg = load_ema8_prototype_cfg(b.scanner_cfg)

    # Recovery rebuilds arm without placing any orders
    mode = bootstrap_prototype_arm(b, b15, proto_cfg, day)
    assert mode == "recovered"
    # Verify no orders/signals were emitted during arm recovery
    assert not hasattr(b, "recent_signals") or len(b.recent_signals) == 0


def test_sl_blocked_side_stays_blocked_until_opposite_close(tmp_path: Path):
    """C2: If a side was SL-blocked earlier today, it stays blocked until an opposite 15m close."""
    day = "2026-11-21"
    cfg = {"enabled": True, "reentry_same_side": True, "away_min_pts": 2.0}

    # Simulate candles: LONG cross at bar 8, touch at bar 9, drops to hit 40pt SL at bar 10
    closes = [25000.0] * 8 + [25100.0, 25080.0, 25045.0, 25045.0]
    highs = [25005.0] * 8 + [25110.0, 25120.0, 25050.0, 25050.0]
    lows = [24995.0] * 8 + [25000.0, 25025.0, 24980.0, 25040.0]
    b15 = _mk_b15(closes, day=day, highs=highs, lows=lows)

    state: dict = {}
    recovered = recover_missing_arm(b15, state=state, cfg=cfg, session_day=day)
    # The trade hit SL on the completed bars, so LONG is SL-blocked and cannot arm
    assert recovered is False
    assert state.get("sl_blocked_side") == "LONG"
    assert "arm" not in state

    # Same side touch arrives: intrabar evaluation must block
    proto_cfg = load_ema8_prototype_cfg({"ema8_prototype": cfg})
    sigs, st = evaluate_ema8_prototype_intrabar(
        b15,
        25080.0,
        cfg=proto_cfg,
        proto_state=state,
        session_day=day,
        gate_meta=lambda _k: {},
        now_hm="11:00",
    )
    assert len(sigs) == 0
    assert state.get("sl_blocked_side") == "LONG"

    # Now, an opposite 15m candle closes below 8EMA (Bearish cross to 24900)
    bear_closes = closes + [24900.0, 24880.0]
    bear_highs = highs + [24950.0, 24890.0]
    bear_lows = lows + [24880.0, 24870.0]
    b15_bear = _mk_b15(bear_closes, day=day, highs=bear_highs, lows=bear_lows)
    state2 = {"sl_blocked_side": "LONG"}
    recovered_opp = recover_missing_arm(b15_bear, state=state2, cfg=cfg, session_day=day)
    # Opposite close clears the LONG block and arms SHORT!
    assert recovered_opp is True
    assert "sl_blocked_side" not in state2
    assert state2["arm"]["direction"] == "SHORT"


def test_eod_square_off_time_blocks_entry(tmp_path: Path):
    """C2: EOD 15:15 blocks entries."""
    day = "2026-11-21"
    b15 = _mk_b15([25000.0] * 8 + [25100.0] * 4, day=day)
    proto_cfg = load_ema8_prototype_cfg(
        {"ema8_prototype": {"enabled": True, "reentry_same_side": True, "eod_square_off_time": "15:15"}}
    )
    state = {
        "arm": {
            "direction": "LONG",
            "cross_bar_open_ts": int(b15.index[8].timestamp()),
            "cross_e8": 25080.0,
            "away_seen": True,
        },
        "day": day,
    }

    # At 15:16 IST, after EOD cutoff:
    sigs, st = evaluate_ema8_prototype_intrabar(
        b15,
        25080.0,
        cfg=proto_cfg,
        proto_state=state,
        session_day=day,
        gate_meta=lambda _k: {},
        tick_high=25081.0,
        tick_low=25079.0,
        now_hm="15:16",
    )
    assert len(sigs) == 0
    reason = str(st.get("block_reason") or "").upper()
    assert "EOD" in reason or "NO TRADE" in reason or "OFF-HOURS" in reason
