"""8 EMA Prototype — arm_mode cross_only vs any_close."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    DEFAULT_CFG,
    _maybe_flip_arm,
    bar_close_arm_triggers,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
    normalize_arm_mode,
)

DAY = "2026-11-23"


def _b15(closes: List[float], start: str = "09:15") -> pd.DataFrame:
    idx = pd.date_range(f"{DAY} {start}", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"open": closes, "high": [c + 2 for c in closes], "low": [c - 2 for c in closes], "close": closes},
        index=idx,
    )


def _cfg(**over: Any) -> Dict[str, Any]:
    return load_ema8_prototype_cfg({"ema8_prototype": over})


def _close_tick(b15: pd.DataFrame, state: Dict[str, Any], cfg: Dict[str, Any], bar_i: int):
    return evaluate_ema8_prototype(
        b15,
        bar_ci=bar_i,
        cfg=cfg,
        proto_state=state,
        session_day=DAY,
        intrabar=False,
    )


def test_default_arm_mode_is_cross_only():
    assert DEFAULT_CFG["arm_mode"] == "cross_only"
    assert normalize_arm_mode({}) == "cross_only"


def test_cross_only_no_arm_when_already_below_without_cross():
    cfg = _cfg()
    scenarios = [
        (95.0, 97.0, 94.0, 96.0),
        (94.0, 96.0, 93.0, 95.0),
        (93.0, 95.0, 92.0, 94.0),
    ]
    for prev_c, prev_e8, c, e8 in scenarios:
        bull, bear = bar_close_arm_triggers(cfg, prev_c=prev_c, prev_e8=prev_e8, c=c, e8=e8)
        assert bull is False and bear is False
    state: Dict[str, Any] = {}
    for i, (c, e8) in enumerate([(94.0, 96.0), (93.0, 95.0), (92.0, 94.0)]):
        bull, bear = bar_close_arm_triggers(cfg, prev_c=c + 1, prev_e8=e8 + 1, c=c, e8=e8)
        _maybe_flip_arm(state, bull, bear, 1_000 + i * 900, e8, DAY)
    assert state.get("arm") is None


def test_any_close_arms_pe_on_close_below_ema():
    cfg = _cfg(arm_mode="any_close")
    state: Dict[str, Any] = {}
    closes = [100.0, 100.0, 100.0, 100.0, 96.0, 95.0]
    b15 = _b15(closes)
    for i in range(1, len(b15)):
        _close_tick(b15, state, cfg, i)
    arm = state.get("arm") or {}
    assert str(arm.get("direction")).upper() == "SHORT"


def test_toggle_mid_day_cross_only_to_any_close_arms_without_restart():
    state: Dict[str, Any] = {}
    cfg_cross = _cfg(arm_mode="cross_only")
    for i, (c, e8) in enumerate([(94.0, 96.0), (93.0, 95.0), (92.0, 94.0)]):
        bull, bear = bar_close_arm_triggers(cfg_cross, prev_c=c + 1, prev_e8=e8 + 1, c=c, e8=e8)
        _maybe_flip_arm(state, bull, bear, 2_000 + i * 900, e8, DAY)
    assert state.get("arm") is None
    cfg_any = _cfg(arm_mode="any_close")
    bull, bear = bar_close_arm_triggers(cfg_any, prev_c=92.0, prev_e8=94.0, c=91.0, e8=93.0)
    assert bear and not bull
    _maybe_flip_arm(state, bull, bear, 5_000, 93.0, DAY)
    assert str((state.get("arm") or {}).get("direction")).upper() == "SHORT"


def test_toggle_any_close_to_cross_only_no_duplicate_arm():
    cfg_any = _cfg(arm_mode="any_close")
    state: Dict[str, Any] = {}
    cross_ts = 1_700_000_000
    _maybe_flip_arm(state, False, True, cross_ts, 25000.0, DAY)
    first_ts = int((state.get("arm") or {}).get("cross_bar_open_ts") or 0)
    assert first_ts == cross_ts
    cfg_cross = _cfg(arm_mode="cross_only")
    bull, bear = bar_close_arm_triggers(
        cfg_cross, prev_c=95.0, prev_e8=97.0, c=94.0, e8=96.5, open_p=94.0
    )
    assert bull is False and bear is False
    _maybe_flip_arm(state, bull, bear, cross_ts + 900, 96.5, DAY)
    assert int((state.get("arm") or {}).get("cross_bar_open_ts") or 0) == first_ts
    assert str((state.get("arm") or {}).get("direction")).upper() == "SHORT"