"""First-to-reserve wins for Prototype vs Reversal entries — pure, testable."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from analyst.ema_lane_lock_8510 import (
    LANE_PROTOTYPE,
    LANE_REVERSAL,
    lane_lock_blocks_entry,
    release_lane_lock,
    try_acquire_lane_lock,
)


def ema_lane_for_strategy(strategy_id: str) -> Optional[str]:
    sid = str(strategy_id or "").upper()
    if sid.startswith("CROSSOVER"):
        return LANE_REVERSAL
    if sid.startswith("EMA8_PROTOTYPE"):
        return LANE_PROTOTYPE
    return None


def try_reserve_lane_for_signal(
    lock: Dict[str, Any],
    strategy_id: str,
    *,
    open_position_count: int,
    note: str = "",
) -> Tuple[bool, str]:
    """Reserve lock before order placement. False = stand down (no second live entry)."""
    lane = ema_lane_for_strategy(strategy_id)
    if not lane:
        return True, ""
    if lock.get("holder"):
        return False, f"Standing down — {lock.get('holder')} holds the trade lock"
    blocked, why = lane_lock_blocks_entry(lock, lane, open_position_count=open_position_count)
    if blocked:
        return False, why
    if not try_acquire_lane_lock(lock, lane, note=note):
        return False, f"Standing down — {lock.get('holder')} holds the trade lock"
    return True, ""


def release_lane_reservation(lock: Dict[str, Any], strategy_id: str) -> None:
    lane = ema_lane_for_strategy(strategy_id)
    if lane:
        release_lane_lock(lock, lane)


def filter_signals_one_ema_trade(
    signals: List[Dict[str, Any]],
    lock: Dict[str, Any],
    *,
    open_position_count: int,
) -> List[Dict[str, Any]]:
    """At most one CROSSOVER/EMA8_PROTOTYPE signal per batch when flat."""
    out: List[Dict[str, Any]] = []
    ema_taken = open_position_count > 0 or bool(lock.get("holder"))
    for sig in signals:
        sid = str(sig.get("strategy_id") or "").upper()
        lane = ema_lane_for_strategy(sid)
        if not lane:
            out.append(sig)
            continue
        if ema_taken:
            continue
        out.append(sig)
        ema_taken = True
    return out