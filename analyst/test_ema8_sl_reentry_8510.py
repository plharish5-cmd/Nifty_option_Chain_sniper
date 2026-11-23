"""SL flip-only arm logging + prototype header EMA refresh (v2.6.3)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema8_prototype_8510 import (  # noqa: E402
    DEFAULT_CFG,
    _arm_new,
    _maybe_flip_arm,
    append_cross_arm_actions,
    check_retouch,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
    note_prototype_exit,
)
from analyst.nse_spot_candles_8510 import last_closed_bar_emas  # noqa: E402
from analyst.test_live_exit_orders_8510 import FakeBroker, make_test_backend  # noqa: E402

DAY = "2026-11-23"


def _b15(
    closes: List[float],
    *,
    highs: List[float] | None = None,
    lows: List[float] | None = None,
    start: str = "09:15",
) -> pd.DataFrame:
    idx = pd.date_range(f"{DAY} {start}", periods=len(closes), freq="15min", tz="Asia/Kolkata")
    hi = highs if highs is not None else [c + 5 for c in closes]
    lo = lows if lows is not None else [c - 5 for c in closes]
    return pd.DataFrame({"open": closes, "high": hi, "low": lo, "close": closes}, index=idx)


def _cfg(**over: Any) -> Dict[str, Any]:
    return load_ema8_prototype_cfg({"ema8_prototype": over})


def _bear_cross_series(n: int = 14) -> pd.DataFrame:
    """Last closed bar (-2) is a bearish close cross below 8 EMA."""
    closes = [25650.0 - (i * 8 if i >= n - 2 else 2) for i in range(n)]
    return _b15(closes)


def test_sl_blocked_same_side_cross_logs_arm_blocked_not_arm():
    state: Dict[str, Any] = {"sl_blocked_side": "SHORT"}
    b15 = _bear_cross_series()
    _, st = evaluate_ema8_prototype(
        b15,
        bar_ci=-2,
        cfg=_cfg(),
        proto_state=state,
        session_day=DAY,
        intrabar=False,
    )
    action_types = [str(a.get("type")) for a in st.get("actions") or []]
    assert "ARM_BLOCKED" in action_types
    assert "ARM" not in action_types
    assert "arm" not in state
    assert st.get("armed") is False
    assert "SHORT blocked after SL (Flip only mode)" in str(st.get("block_reason") or "")


def test_reentry_after_sl_same_side_stays_armed_and_takes_retouch():
    cfg = _cfg(reentry_after_stop_loss=True, retouch_requires_away=False, slow_market_exact_line=True)
    b15 = _b15([25600.0] * 12)
    state: Dict[str, Any] = {}
    cross_ts = int(b15.index[-3].timestamp())
    _arm_new(state, direction="SHORT", cross_bar_open_ts=cross_ts, e8=25600.0)
    assert note_prototype_exit(state, cfg, "SHORT", "OPTION_SL_HIT") is False
    assert state.get("arm", {}).get("direction") == "SHORT"
    assert "sl_blocked_side" not in state

    # New bearish 15m close still keeps / refreshes the SHORT arm.
    b15_bear = _bear_cross_series()
    sigs, st = evaluate_ema8_prototype(
        b15_bear,
        bar_ci=-2,
        cfg=cfg,
        proto_state=state,
        session_day=DAY,
        intrabar=False,
    )
    assert state.get("arm", {}).get("direction") == "SHORT"
    arm_actions = [a for a in (st.get("actions") or []) if a.get("type") == "ARM"]
    assert len(arm_actions) <= 1

    touch_b15 = _b15([25580.0] * 14)
    touch_state = dict(state)
    sigs2, st2 = evaluate_ema8_prototype(
        touch_b15,
        bar_ci=-1,
        cfg=cfg,
        proto_state=touch_state,
        session_day=DAY,
        live_spot=25610.0,
        intrabar=True,
        tick_high=25615.0,
        tick_low=25595.0,
        now_hm="11:35",
    )
    assert st2.get("armed") is True
    assert touch_state.get("arm", {}).get("direction") == "SHORT"


def test_append_cross_arm_actions_respects_actual_arm_state():
    actions: List[Dict[str, Any]] = []
    state: Dict[str, Any] = {"sl_blocked_side": "SHORT"}
    append_cross_arm_actions(
        actions,
        cfg=_cfg(),
        proto_state=state,
        bull_cross=False,
        bear_cross=True,
        cross_open_ts=111,
        e8_val=25600.0,
        spot=25590.0,
    )
    assert actions and actions[0]["type"] == "ARM_BLOCKED"
    state2: Dict[str, Any] = {}
    _maybe_flip_arm(state2, False, True, 222, 25600.0)
    actions2: List[Dict[str, Any]] = []
    append_cross_arm_actions(
        actions2,
        cfg=_cfg(),
        proto_state=state2,
        bull_cross=False,
        bear_cross=True,
        cross_open_ts=222,
        e8_val=25600.0,
        spot=25590.0,
    )
    assert actions2 and actions2[0]["type"] == "ARM"


def test_nov23_chase_blocked_when_spot_far_from_e8_despite_bar_wick():
    """2026-11-23 09:36:52 IST — PE entry must not chase 78.8 pts below dynamic 8 EMA."""
    arm: Dict[str, Any] = {"direction": "SHORT", "away_seen": True}
    e8 = 25717.6
    spot = 25638.8
    tol = 0.0
    # Forming-bar high wick touched the line earlier; live spot was still ~79 pts below.
    assert check_retouch(
        arm,
        bullish=False,
        e8_val=e8,
        tol=tol,
        spot=spot,
        retouch_requires_away=False,
        candle_high=25720.0,
        candle_low=25635.0,
    ) is False

    state: Dict[str, Any] = {}
    cross_ts = 1795405500
    _arm_new(state, direction="SHORT", cross_bar_open_ts=cross_ts, e8=e8)
    cfg = _cfg(retouch_requires_away=False, slow_market_exact_line=True, touch_tol_slow_pts=0.0)
    b15 = _b15([25650.0] * 12)
    sigs, st = evaluate_ema8_prototype(
        b15,
        bar_ci=-1,
        cfg=cfg,
        proto_state=state,
        session_day=DAY,
        live_spot=spot,
        intrabar=True,
        form_high=25720.0,
        form_low=25635.0,
        tick_high=25720.0,
        tick_low=spot,
        now_hm="09:36",
    )
    assert sigs == []
    assert "RETOUCH" not in str(st.get("actions") or st.get("phase") or "")


def test_proto_ema_audit_refreshes_without_arm(tmp_path: Path):
    b = make_test_backend(tmp_path, FakeBroker(configured=False))
    closes = [25600.0 - i * 10 for i in range(20)]
    b15 = _b15(closes)
    spot = float(closes[-1])
    b._ema8_prototype_state = {"sl_blocked_side": "SHORT"}
    b._refresh_proto_ema_audit(b15, spot)
    audit = b._proto_ema_audit or {}
    assert audit.get("e8_live") is not None
    assert audit.get("e8_closed") is not None
    _, _, expected_closed, _ = last_closed_bar_emas(b15, offset=-2)
    assert audit["e8_closed"] == round(float(expected_closed), 2)