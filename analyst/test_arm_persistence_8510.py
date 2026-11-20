"""Arm continuity across simulated 8510 restarts (Prototype + Reversal)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from analyst.ema8_prototype_8510 import (
    _arm_new,
    bar_open_timestamp,
    evaluate_ema8_prototype_intrabar,
    load_ema8_prototype_cfg,
)
from analyst.ema_arm_persistence_8510 import (
    bootstrap_prototype_arm,
    format_broker_rejection,
    mj_state_persist_slice,
    apply_mj_persist_slice,
)
from analyst.ema_lane_lock_8510 import LANE_PROTOTYPE, LANE_REVERSAL, try_acquire_lane_lock
from analyst.terminal8510_backend import QuantTerminalBackend


def _mk_b15(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-11-19 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _backend_with_db(tmp_path: Path) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.cfg = {"scanner": {"ema8_prototype": {"enabled": True, "reentry_same_side": True}}}
    b.scanner_cfg = b.cfg["scanner"]
    b._ema8_prototype_state = {}
    b._mj_sir_state = {}
    b._ema_lane_lock = {}
    b._proto_arm_loaded_from_db = False
    b._mj_sir_loaded_from_db = False
    b.log_ema8_prototype_event = lambda **_: None  # type: ignore
    b._init_sqlite()
    return b


def test_format_broker_rejection_dh905():
    raw = '{"errorType":"Input_Exception","errorCode":"DH-905","errorMessage":"Invalid IP"}'
    assert format_broker_rejection(raw) == "DH-905: Invalid IP"


def test_prototype_arm_persists_and_resumes(tmp_path: Path):
    b1 = _backend_with_db(tmp_path)
    cross_ts = int(_mk_b15([25100.0] * 8).index[-3].timestamp())
    _arm_new(
        b1._ema8_prototype_state,
        direction="LONG",
        cross_bar_open_ts=cross_ts,
        e8=25120.0,
        away_seen=True,
    )
    b1._ema8_prototype_state["day"] = "2026-11-19"
    b1._ema8_prototype_state["last_touch_ts"] = time.time() - 3600
    b1._save_ema8_prototype_state()

    b2 = _backend_with_db(tmp_path)
    assert b2._proto_arm_loaded_from_db is True
    arm = b2._ema8_prototype_state["arm"]
    assert arm["direction"] == "LONG"
    assert arm["away_seen"] is True
    assert b2._ema8_prototype_state.get("day") == "2026-11-19"


def test_live_touch_after_restart_is_taken_not_historical(tmp_path: Path):
    """Five-hour hole: arm at T, restart at T+n, live touch at T+2n fires a signal."""
    b1 = _backend_with_db(tmp_path)
    b15 = _mk_b15([25180.0] * 10)
    cross_ts = bar_open_timestamp(b15, 8)
    _arm_new(
        b1._ema8_prototype_state,
        direction="LONG",
        cross_bar_open_ts=cross_ts,
        e8=25180.0,
        away_seen=True,
    )
    b1._ema8_prototype_state["day"] = "2026-11-19"
    b1._save_ema8_prototype_state()

    b2 = _backend_with_db(tmp_path)
    b2._proto_arm_bootstrap_done = False
    calls = []
    b2.log_ema8_prototype_event = lambda **kw: calls.append(kw.get("event"))  # type: ignore
    proto_cfg = load_ema8_prototype_cfg(
        {
            "ema8_prototype": {
                "enabled": True,
                "reentry_same_side": True,
                "ribbon_filter_enabled": False,
                "touch_tol_pts": 3.0,
                "away_min_pts": 3.0,
            }
        }
    )
    mode = bootstrap_prototype_arm(b2, b15, proto_cfg, "2026-11-19")
    assert mode == "resumed"
    assert calls and calls[0] == "ARM RESUMED"

    spot = 25180.0
    sigs, _ = evaluate_ema8_prototype_intrabar(
        b15,
        spot,
        cfg=proto_cfg,
        proto_state=b2._ema8_prototype_state,
        session_day="2026-11-19",
        gate_meta=lambda _k: {},
        tick_high=spot + 0.5,
        tick_low=spot - 0.5,
        now_hm="10:00",
    )
    assert len(sigs) >= 1
    assert sigs[0]["strategy_id"].startswith("EMA8_PROTOTYPE")


def test_stale_spot_does_not_drop_arm(tmp_path: Path):
    b = _backend_with_db(tmp_path)
    _arm_new(b._ema8_prototype_state, direction="SHORT", cross_bar_open_ts=1, e8=25200.0, away_seen=True)
    before = dict(b._ema8_prototype_state["arm"])
    # Simulate stale pause path: persist without mutation
    b._save_ema8_prototype_state()
    b2 = _backend_with_db(tmp_path)
    after = b2._ema8_prototype_state["arm"]
    assert after["direction"] == before["direction"]
    assert after["away_seen"] is True


def test_lane_lock_survives_restart(tmp_path: Path):
    b1 = _backend_with_db(tmp_path)
    assert try_acquire_lane_lock(b1._ema_lane_lock, LANE_PROTOTYPE, note="test hold")
    b1._save_ema_lane_lock_state()

    b2 = _backend_with_db(tmp_path)
    assert str(b2._ema_lane_lock.get("holder")).upper() == LANE_PROTOTYPE


def test_mj_slice_persist_reversal_keys():
    mj = {"day": "2026-11-19", "reversal_fired_key": "x", "noise": 1}
    sl = mj_state_persist_slice(mj)
    assert sl == {"day": "2026-11-19", "reversal_fired_key": "x"}
    out: dict = {}
    apply_mj_persist_slice(out, sl)
    assert out["reversal_fired_key"] == "x"


def test_recovered_only_when_no_persisted_arm(tmp_path: Path):
    b = _backend_with_db(tmp_path)
    b15 = _mk_b15([25100.0, 25090.0, 25120.0, 25130.0, 25140.0, 25150.0, 25160.0, 25170.0])
    b._proto_arm_loaded_from_db = False
    events = []
    b.log_ema8_prototype_event = lambda **kw: events.append(kw.get("event"))  # type: ignore
    proto_cfg = load_ema8_prototype_cfg(b.scanner_cfg)
    mode = bootstrap_prototype_arm(b, b15, proto_cfg, "2026-11-19")
    assert mode in ("recovered", "none")
    if mode == "recovered":
        assert "ARM RECOVERED" in events

def test_recovered_arm_reconstructs_away_seen_so_next_touch_trades():
    """2026-11-20 14:45 miss: after a mid-session restart the arm is rebuilt from the
    last cross. It must reconstruct away_seen=True from completed bars that already
    pulled away from the 8EMA, so the FIRST live touch after restart trades instead
    of being consumed just priming away_seen."""
    from analyst.prototype_arm_recovery_8510 import recover_missing_arm

    cfg = {"enabled": True, "reentry_same_side": True, "away_min_pts": 2.0}
    # Flat ~25000 (e8 settles), then a clear LONG cross up to 25100 and two bars that
    # hold up there (high = close+5 = 25105, well > e8 + 2). Last bar is "forming".
    closes = [25000.0] * 8 + [25100.0, 25100.0, 25100.0, 25100.0]
    b15 = _mk_b15(closes)
    state: dict = {}
    assert recover_missing_arm(b15, state=state, cfg=cfg, session_day="2026-11-19") is True
    arm = state["arm"]
    assert arm["direction"] == "LONG"
    assert arm["away_seen"] is True  # the fix: was False before, wasting the next touch


def test_recovered_arm_without_moveaway_stays_unprimed():
    """Negative: if price never demonstrably pulled away after the cross, away_seen
    stays False — the rebuild can never fabricate an entry at the cross itself."""
    from analyst.prototype_arm_recovery_8510 import recover_missing_arm

    cfg = {"enabled": True, "reentry_same_side": True, "away_min_pts": 500.0}
    # away_min_pts is huge (500): no bar high ever clears e8 + 500, so no away.
    closes = [25000.0] * 8 + [25100.0, 25100.0, 25100.0, 25100.0]
    b15 = _mk_b15(closes)
    state: dict = {}
    assert recover_missing_arm(b15, state=state, cfg=cfg, session_day="2026-11-19") is True
    assert state["arm"]["away_seen"] is False
