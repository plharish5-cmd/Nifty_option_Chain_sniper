"""Optional candle trail after profit for 8 EMA Prototype (ride the trend).

Reuses terminal8510_backend.flagship_candle_trail_update for the ratchet rule.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

DEFAULT_CANDLE_TRAIL: Dict[str, Any] = {
    "enabled": False,
    "activate_pts": 40.0,
    "candle_tf_min": 15,
    "use_completed_candles": True,
}

ALLOWED_TF_MIN = (3, 5, 15)


def load_candle_trail_cfg(proto_cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_CANDLE_TRAIL)
    raw = (proto_cfg or {}).get("candle_trail")
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        if "activate_pts" in raw:
            out["activate_pts"] = float(raw["activate_pts"])
        if "candle_tf_min" in raw:
            tf = int(raw["candle_tf_min"])
            out["candle_tf_min"] = tf if tf in ALLOWED_TF_MIN else int(out["candle_tf_min"])
        if "use_completed_candles" in raw:
            out["use_completed_candles"] = bool(raw["use_completed_candles"])
    return out


def candle_trail_active(proto_cfg: Dict[str, Any]) -> bool:
    return bool(load_candle_trail_cfg(proto_cfg).get("enabled"))


def session_anchor_ts(day: str) -> int:
    from datetime import datetime, timedelta, timezone

    IST = timezone(timedelta(hours=5, minutes=30))
    return int(datetime.strptime(f"{day} 09:15", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def aggregate_tf_bar(day_1m: pd.DataFrame, bar_open: int, bar_close: int) -> Optional[Tuple[float, float, float, float]]:
    """OHLC for one completed tf bar [bar_open, bar_close)."""
    if day_1m is None or day_1m.empty:
        return None
    sl = day_1m[(day_1m["ts"] >= bar_open) & (day_1m["ts"] < bar_close)]
    if sl.empty:
        return None
    o = float(sl.iloc[0]["open"])
    h = float(sl["high"].max())
    l = float(sl["low"].min())
    c = float(sl.iloc[-1]["close"])
    return o, h, l, c


def floor_trail_stop(is_long: bool, trailed: float, floor: float) -> float:
    """Never worse than the original disaster stop (entry ± sl_pts)."""
    if is_long:
        return max(trailed, floor)
    return min(trailed, floor)


def fav_points(is_long: bool, entry: float, high: float, low: float) -> float:
    return (high - entry) if is_long else (entry - low)


def maybe_arm_trail(
    *,
    enabled: bool,
    armed: bool,
    is_long: bool,
    entry: float,
    running_mfe: float,
    activate_pts: float,
    floor_stop: float,
    trail_steps: List[str],
) -> Tuple[bool, float, float]:
    """Return (armed, trailed_stop, running_mfe) after checking activation."""
    if not enabled or armed:
        return armed, floor_stop, running_mfe
    if running_mfe >= float(activate_pts):
        trail_steps.append(f"TRAIL armed +{int(activate_pts)}")
        return True, float(floor_stop), running_mfe
    return False, floor_stop, running_mfe