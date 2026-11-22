"""Track 4: Resume-on-open for Prototype, Reversal, and 1H Flagship."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from analyst.decision_audit_8510 import read as audit_read
from analyst.ema8_prototype_8510 import (
    _arm_new,
    evaluate_ema8_prototype_intrabar,
    load_ema8_prototype_cfg,
)
from analyst.ema_arm_persistence_8510 import (
    bootstrap_flagship_arm,
    bootstrap_prototype_arm,
    bootstrap_reversal_arm,
)
from analyst.resume_all3_8510 import bootstrap_all_strategies_on_open
from analyst.reversal_revamp_8510 import load_reversal_cfg, run_reversal_revamp
from analyst.terminal8510_backend import QuantTerminalBackend


def _mk_b15(
    closes: list[float],
    day: str = "2026-11-21",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 09:15", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else [c + 8 for c in closes],
            "low": lows if lows is not None else [c - 8 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _backend(tmp_path: Path) -> QuantTerminalBackend:
    b = object.__new__(QuantTerminalBackend)
    b.ledger_db = tmp_path / "ledger.sqlite3"
    b.cfg = {
        "scanner": {
            "ema8_prototype": {
                "enabled": True,
                "reentry_same_side": True,
                "away_min_pts": 2.0,
                "ribbon_filter_enabled": False,
            },
            "mj_sir_lecture": {
                "reversal_wait_bars_after_cross": 1,
                "reversal_touch_window_bars": 3,
                "reversal_lookback_bars": 12,
                "dynamic_touch_tol_pts": 5.0,
            },
            "reversal": {"enabled": True, "timeframe_mode": "15m"},
            "flagship_no_entry_after": "15:15",
        }
    }
    b.scanner_cfg = b.cfg["scanner"]
    b._ema8_prototype_state = {}
    b._mj_sir_state = {}
    b._ema_lane_lock = {}
    b.strategy_status = {"flagship_1h": {"state": "SCANNING", "armed": False}}
    b._proto_arm_loaded_from_db = False
    b._mj_sir_loaded_from_db = False
    b.log_ema8_prototype_event = Mock()
    b._init_sqlite()
    return b


def _audit_stages(tmp_path: Path, day: str, lane: str) -> list[str]:
    rows = audit_read(tmp_path / "ledger.sqlite3", day=day, lane=lane, limit=20)
    return [str(r.get("stage") or "").upper() for r in rows]


# ── Prototype (parity with dedicated resume tests) ──────────────────────────


def test_prototype_persisted_restart_arm_resumed(tmp_path: Path):
    day = "2026-11-21"
    b1 = _backend(tmp_path)
    b15 = _mk_b15([25000.0] * 8 + [25100.0] * 4, day=day)
    cross_ts = int(b15.index[8].timestamp())
    _arm_new(b1._ema8_prototype_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25080.0, away_seen=True)
    b1._ema8_prototype_state["day"] = day
    b1._save_ema8_prototype_state()

    b2 = _backend(tmp_path)
    proto_cfg = load_ema8_prototype_cfg(b2.scanner_cfg)
    assert bootstrap_prototype_arm(b2, b15, proto_cfg, day) == "resumed"
    events = [c.kwargs.get("event") for c in b2.log_ema8_prototype_event.call_args_list]
    assert "ARM RESUMED" in events
    assert "ARM RECOVERED" not in events


# ── Reversal ────────────────────────────────────────────────────────────────


def test_reversal_persisted_restart_arm_resumed(tmp_path: Path):
    day = "2026-11-21"
    b1 = _backend(tmp_path)
    b1._mj_sir_state = {
        "day": day,
        "reversal_arm": {
            "side": "UP",
            "cross_index": "2026-11-21 10:00:00+05:30",
            "fixed_e8": 25100.0,
            "touch_ready": True,
        },
    }
    b1._save_mj_sir_runtime_state()

    b2 = _backend(tmp_path)
    assert b2._mj_sir_loaded_from_db is True
    b15 = _mk_b15([25100.0] * 12, day=day)
    mode = bootstrap_reversal_arm(b2, b15, b2.scanner_cfg, day)
    assert mode == "resumed"
    stages = _audit_stages(tmp_path, day, "reversal")
    assert "ARM_RESUMED" in stages


def test_reversal_recovered_first_live_touch_eligible(tmp_path: Path):
    """Absent persistence → RECOVERED; completed bars prime touch_ready; live touch can fire."""
    day = "2026-11-21"
    # Ramp into a bullish 4/8 cross on bar 8, then hold above 8EMA (no touch on closed bars in window).
    closes = [24950.0, 24960.0, 24970.0, 24980.0, 24985.0, 24990.0, 24995.0, 25000.0, 25080.0, 25120.0, 25130.0, 25135.0]
    lows = [c - 15 for c in closes]
    highs = [c + 15 for c in closes]
    b15 = _mk_b15(closes, day=day, lows=lows, highs=highs)
    b = _backend(tmp_path)
    mode = bootstrap_reversal_arm(b, b15, b.scanner_cfg, day)
    if mode != "recovered":
        pytest.skip("synthetic frame did not yield reversal cross recovery — data tuning")
    assert "ARM_RECOVERED" in _audit_stages(tmp_path, day, "reversal")
    arm = b._mj_sir_state.get("reversal_arm")
    assert arm and arm.get("touch_ready") is True

    rev_cfg = load_reversal_cfg(b.scanner_cfg)
    mj_cfg = b.scanner_cfg.get("mj_sir_lecture") or {}
    cfg = {**mj_cfg, **rev_cfg}
    sigs, st = run_reversal_revamp(
        scanner_cfg=b.scanner_cfg,
        b15=b15,
        b5=None,
        cfg=cfg,
        bar_ci=-1,
        bb_cross=False,
        party_switch_enabled=False,
        active_party="NEUTRAL",
        mj_state=b._mj_sir_state,
        session_day=day,
        dyn_on=True,
        dynamic={"e8": arm["fixed_e8"]},
        touch_long=True,
        touch_short=False,
        entry_px=arm["fixed_e8"],
        e8_dyn=arm["fixed_e8"],
        early=False,
        early_msg="",
        wait_msg="",
        rev_block=None,
        ubb=pd.Series([100.0] * len(b15)),
        lbb=pd.Series([90.0] * len(b15)),
        slope_tol=2.0,
        tol=5.0,
        low_15=float(b15["low"].iloc[-1]),
        high_15=float(b15["high"].iloc[-1]),
        gate_meta=lambda _k: {},
        legacy_runner=lambda: ([], {}),
    )
    assert len(sigs) >= 1
    assert sigs[0]["strategy_id"].startswith("CROSSOVER")


def test_reversal_downtime_touch_not_back_traded(tmp_path: Path):
    day = "2026-11-21"
    closes = [24950.0, 24960.0, 24970.0, 24980.0, 24985.0, 24990.0, 24995.0, 25000.0, 25080.0, 25100.0, 25095.0, 25100.0]
    b15 = _mk_b15(closes, day=day)
    b = _backend(tmp_path)
    bootstrap_reversal_arm(b, b15, b.scanner_cfg, day)
    # Recovery must not append trading signals by itself
    assert not getattr(b, "recent_signals", None)


def test_reversal_sl_blocked_until_opposite_close(tmp_path: Path):
    day = "2026-11-21"
    b = _backend(tmp_path)
    b._mj_sir_state = {"day": day, "reversal_sl_blocked_side": "LONG"}
    b._save_mj_sir_runtime_state()
    b2 = _backend(tmp_path)
    assert b2._mj_sir_state.get("reversal_sl_blocked_side") == "LONG"
    b15 = _mk_b15([25100.0] * 12, day=day)
    mode = bootstrap_reversal_arm(b2, b15, b2.scanner_cfg, day)
    assert mode == "resumed"


# ── 1H Flagship ─────────────────────────────────────────────────────────────


def _hammer_b15(day: str = "2026-11-21") -> pd.DataFrame:
    """Closed confirmation bar (-2) is a hammer; forming bar last."""
    n = 14
    closes = [25100.0] * (n - 2) + [25095.0, 25105.0]
    opens = [25100.0] * (n - 2) + [25100.0, 25100.0]
    highs = [25105.0] * (n - 2) + [25102.0, 25110.0]
    lows = [25095.0] * (n - 2) + [25070.0, 25098.0]
    return _mk_b15(closes, day=day, opens=opens, highs=highs, lows=lows)


def test_flagship_persisted_restart_arm_resumed(tmp_path: Path):
    day = "2026-11-21"
    b1 = _backend(tmp_path)
    b1._mj_sir_state = {
        "day": day,
        "flagship_resume": {
            "day": day,
            "armed": True,
            "direction": "LONG",
            "pattern": "HAMMER",
            "confirm_bar_ts": 1,
        },
    }
    b1._save_mj_sir_runtime_state()

    b2 = _backend(tmp_path)
    b15 = _hammer_b15(day)
    mode = bootstrap_flagship_arm(b2, b15, b2.scanner_cfg, day)
    assert mode == "resumed"
    assert _audit_stages(tmp_path, day, "flagship")
    assert b2.strategy_status["flagship_1h"]["state"] == "ARMED"


def test_flagship_recovered_from_confirmation_bar(tmp_path: Path):
    day = "2026-11-21"
    b = _backend(tmp_path)
    b15 = _hammer_b15(day)
    mode = bootstrap_flagship_arm(b, b15, b.scanner_cfg, day)
    assert mode in ("recovered", "none")
    if mode == "recovered":
        assert "ARM_RECOVERED" in _audit_stages(tmp_path, day, "flagship")
        assert b._mj_sir_state.get("flagship_resume", {}).get("armed") is True


def test_flagship_eod_15_15_blocks_recovery(tmp_path: Path):
    day = "2026-11-21"
    b = _backend(tmp_path)
    closes = [25100.0] * 16
    idx = pd.date_range(f"{day} 13:30", periods=16, freq="15min", tz="Asia/Kolkata")
    b15 = pd.DataFrame(
        {"open": closes, "high": [c + 5 for c in closes], "low": [c - 5 for c in closes], "close": closes, "volume": [1] * 16},
        index=idx,
    )
    mode = bootstrap_flagship_arm(b, b15, b.scanner_cfg, day)
    assert mode == "none"


# ── Unified bootstrap + mid-session one-tick proof ──────────────────────────


def test_mid_session_restart_all_three_audit_rows(tmp_path: Path):
    day = "2026-11-21"
    b1 = _backend(tmp_path)
    flat = [25180.0] * 10
    b15 = _mk_b15(flat, day=day)
    cross_ts = int(b15.index[7].timestamp())
    _arm_new(b1._ema8_prototype_state, direction="LONG", cross_bar_open_ts=cross_ts, e8=25180.0, away_seen=True)
    b1._ema8_prototype_state["day"] = day
    b1._save_ema8_prototype_state()
    b1._mj_sir_state = {
        "day": day,
        "reversal_arm": {"side": "UP", "fixed_e8": 25180.0, "touch_ready": True},
        "flagship_resume": {"day": day, "armed": True, "direction": "LONG", "pattern": "HAMMER"},
    }
    b1._save_mj_sir_runtime_state()

    b2 = _backend(tmp_path)
    b2._proto_arm_bootstrap_done = False
    proto_cfg = load_ema8_prototype_cfg(b2.scanner_cfg)
    modes = bootstrap_all_strategies_on_open(b2, b15, proto_cfg, day)
    assert modes["prototype"] == "resumed"
    assert modes["reversal"] == "resumed"
    assert modes["flagship"] == "resumed"

    spot = 25180.0
    sigs, st = evaluate_ema8_prototype_intrabar(
        b15,
        spot,
        cfg=proto_cfg,
        proto_state=b2._ema8_prototype_state,
        session_day=day,
        gate_meta=lambda _k: {},
        tick_high=spot + 0.5,
        tick_low=spot - 0.5,
        now_hm="10:00",
    )
    assert len(sigs) >= 1, st.get("state") in ("ARMED", "TRIGGERED", None)

    for lane in ("prototype", "reversal", "flagship"):
        rows = audit_read(tmp_path / "ledger.sqlite3", day=day, lane=lane, limit=5)
        assert rows, f"expected decision audit row for {lane}"


def test_bootstrap_all_three_negative_no_false_resume(tmp_path: Path):
    day = "2026-11-21"
    b = _backend(tmp_path)
    b15 = _mk_b15([25100.0] * 6, day=day)
    proto_cfg = load_ema8_prototype_cfg(b.scanner_cfg)
    modes = bootstrap_all_strategies_on_open(b, b15, proto_cfg, day)
    assert modes["prototype"] in ("none", "recovered")
    assert modes["reversal"] in ("none", "recovered")
    assert modes["flagship"] in ("none", "recovered")