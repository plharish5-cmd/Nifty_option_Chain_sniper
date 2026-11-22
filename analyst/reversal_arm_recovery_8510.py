"""Rebuild Reversal lane arm from completed session candles (no back-traded touches)."""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from analyst.mj_sir_stage_reversal_8510 import _find_cross_index, load_mj_cfg, touch_fixed_level
from analyst.nse_spot_candles_8510 import ema4_ema8


def _session_day_ok(ts, session_day: str) -> bool:
    try:
        return ts.strftime("%Y-%m-%d") == session_day
    except Exception:
        return False


def recover_reversal_arm(
    b15: pd.DataFrame,
    *,
    mj_state: Dict[str, Any],
    scanner_cfg: Optional[Dict[str, Any]],
    session_day: str,
) -> bool:
    """Prime reversal_arm or mark historical touch as fired; never emit orders."""
    if isinstance(mj_state.get("reversal_arm"), dict) and mj_state["reversal_arm"].get("side"):
        return False
    if str(mj_state.get("reversal_sl_blocked_side") or "").upper():
        return False

    cfg = load_mj_cfg(scanner_cfg or {})
    wait_bars = int(cfg["reversal_wait_bars_after_cross"])
    touch_win = int(cfg["reversal_touch_window_bars"])
    lookback = int(cfg["reversal_lookback_bars"])
    tol = float(cfg.get("dynamic_touch_tol_pts", 3.0))
    sl_pts = float(cfg.get("reversal_sl_pts", 20.0))

    if len(b15) < 5:
        return False

    e4, e8 = ema4_ema8(b15["close"])
    t_i = len(b15) - 2
    if t_i < 1:
        return False

    cross_up = _find_cross_index(e4, e8, b15["close"], True, t_i)
    cross_dn = _find_cross_index(e4, e8, b15["close"], False, t_i)
    if cross_up is not None and cross_dn is not None:
        if cross_up > cross_dn:
            cross_dn = None
        else:
            cross_up = None

    def _eval_side(bullish: bool, cross_i: Optional[int]) -> Optional[str]:
        if cross_i is None:
            return None
        if t_i - cross_i > lookback:
            return None
        fixed_e8 = float(e8.iloc[cross_i])
        fixed_e4 = float(e4.iloc[cross_i])
        if bullish and fixed_e4 <= fixed_e8:
            return None
        if not bullish and fixed_e4 >= fixed_e8:
            return None
        age = t_i - cross_i
        if age < wait_bars:
            return None
        win_start = cross_i + wait_bars
        win_end = cross_i + wait_bars + touch_win - 1
        if t_i < win_start or t_i > win_end:
            return None

        side = "UP" if bullish else "DN"
        fired_key = f"{session_day}:REV_15m_{side}:{b15.index[cross_i]}"
        if mj_state.get("reversal_fired_key") == fired_key:
            return None

        touched_on_closed = False
        in_pos = False
        pos_sl = 0.0
        active_side = side

        for bi in range(win_start, min(t_i, win_end) + 1):
            if not _session_day_ok(b15.index[bi], session_day):
                continue
            lo = float(b15["low"].iloc[bi])
            hi = float(b15["high"].iloc[bi])
            if not in_pos:
                if touch_fixed_level(lo, hi, fixed_e8, tol, bullish):
                    touched_on_closed = True
                    in_pos = True
                    entry = fixed_e8
                    pos_sl = entry - sl_pts if bullish else entry + sl_pts
            else:
                if bullish and lo <= pos_sl:
                    mj_state["reversal_sl_blocked_side"] = "LONG"
                    mj_state["day"] = session_day
                    mj_state.pop("reversal_arm", None)
                    return False
                if not bullish and hi >= pos_sl:
                    mj_state["reversal_sl_blocked_side"] = "SHORT"
                    mj_state["day"] = session_day
                    mj_state.pop("reversal_arm", None)
                    return False

        if touched_on_closed:
            mj_state["reversal_fired_key"] = fired_key
            mj_state["day"] = session_day
            mj_state.pop("reversal_arm", None)
            return False

        mj_state["reversal_arm"] = {
            "side": active_side,
            "cross_index": str(b15.index[cross_i]),
            "cross_bar_open_ts": int(b15.index[cross_i].timestamp()),
            "fixed_e8": round(fixed_e8, 2),
            "touch_ready": True,
            "fired_key_template": fired_key,
        }
        mj_state["day"] = session_day
        return True

    if _eval_side(True, cross_up):
        return True
    if _eval_side(False, cross_dn):
        return True
    return False