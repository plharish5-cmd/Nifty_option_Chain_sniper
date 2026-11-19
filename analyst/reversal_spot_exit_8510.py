"""Reversal spot exit helpers — candle trail after +N pts (reuse flagship maths)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

def _flagship_candle_trail_update(
    is_long: bool, stop: float, candle_o: float, candle_h: float, candle_l: float, candle_c: float
) -> float:
    if is_long and candle_c > candle_o:
        return max(stop, candle_l)
    if not is_long and candle_c < candle_o:
        return min(stop, candle_h)
    return stop


def reversal_trail_activate_profit(
    *,
    is_long: bool,
    spot_entry: float,
    spot_idx: float,
    activate_pts: float,
) -> bool:
    if spot_entry <= 0 or spot_idx <= 0:
        return False
    pts = (spot_idx - spot_entry) if is_long else (spot_entry - spot_idx)
    return pts >= float(activate_pts)


def apply_reversal_candle_trail_to_stop(
    *,
    is_long: bool,
    spot_stop: float,
    candle_o: float,
    candle_h: float,
    candle_l: float,
    candle_c: float,
) -> float:
    """Trail spot stop on completed bar extremes — no candle-color filter."""
    if is_long:
        return max(spot_stop, candle_l)
    return min(spot_stop, candle_h)


def disabled_spot_target_for_trail(is_long: bool, spot_entry: float) -> float:
    """After trail arms, fixed ±target no longer applies — use unreachable level."""
    bump = 50000.0
    return spot_entry + bump if is_long else max(1.0, spot_entry - bump)


def parse_reversal_trail_fields(p: Dict[str, Any]) -> Dict[str, Any]:
    """From position row + optional spot_context keys."""
    return {
        "enabled": bool(int(p.get("reversal_candle_trail") or 0)),
        "tf": str(p.get("reversal_trail_tf") or "15m"),
        "activate_pts": float(p.get("reversal_trail_activate_pts") or 20.0),
        "armed": bool(int(p.get("spot_trail_armed") or 0)),
    }


def reversal_spot_exit_tick(
    *,
    is_long: bool,
    spot_entry: float,
    spot_idx: float,
    spot_tgt: float,
    spot_sl: float,
    trail: Dict[str, Any],
    bar: Optional[Dict[str, Any]],
) -> Tuple[float, float, bool, str]:
    """
    Returns (spot_tgt, spot_sl, should_close, reason) for one monitor tick.
    `bar` is a completed candle dict with open/high/low/close.
    """
    if spot_entry <= 0 or spot_idx <= 0:
        return spot_tgt, spot_sl, False, ""
    tgt = spot_tgt
    sl = spot_sl
    if trail.get("enabled"):
        if not trail.get("armed") and reversal_trail_activate_profit(
            is_long=is_long,
            spot_entry=spot_entry,
            spot_idx=spot_idx,
            activate_pts=trail.get("activate_pts", 20.0),
        ):
            trail["armed"] = True
            tgt = disabled_spot_target_for_trail(is_long, spot_entry)
        if trail.get("armed") and bar:
            sl = apply_reversal_candle_trail_to_stop(
                is_long=is_long,
                spot_stop=sl,
                candle_o=float(bar["open"]),
                candle_h=float(bar["high"]),
                candle_l=float(bar["low"]),
                candle_c=float(bar["close"]),
            )
    if is_long:
        if tgt > 0 and spot_idx >= tgt:
            return tgt, sl, True, "SPOT_TARGET_HIT"
        if sl > 0 and spot_idx <= sl:
            return tgt, sl, True, "SPOT_SL_HIT"
    else:
        if tgt > 0 and spot_idx <= tgt:
            return tgt, sl, True, "SPOT_TARGET_HIT"
        if sl > 0 and spot_idx >= sl:
            return tgt, sl, True, "SPOT_SL_HIT"
    return tgt, sl, False, ""

def trail_bar_belongs_to_position(bar: Dict[str, Any], entry_time: Any, timeframe: str) -> bool:
    """A pre-entry closed candle cannot tighten a newly opened trade's stop."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        entry = datetime.fromisoformat(str(entry_time).replace(' IST', '').replace('Z', '+00:00'))
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=ZoneInfo('Asia/Kolkata'))
        seconds = 300 if timeframe == '5m' else 900
        return float(bar['ts']) + seconds > entry.timestamp()
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
