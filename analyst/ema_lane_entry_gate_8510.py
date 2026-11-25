"""Per-lane entry gates for Prototype vs Reversal — isolated from other desk strategies."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from analyst.ema_lane_lock_8510 import (
    LANE_PROTOTYPE,
    LANE_REVERSAL,
    _lane_holders,
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


def position_matches_ema_lane(position: Dict[str, Any], lane: str) -> bool:
    sid = str(position.get("strategy_id") or "").upper()
    lane_u = str(lane or "").upper()
    if lane_u == LANE_PROTOTYPE:
        return sid.startswith("EMA8_PROTOTYPE")
    if lane_u == LANE_REVERSAL:
        return sid.startswith("CROSSOVER")
    return False


def count_open_positions_for_lane(positions: List[Dict[str, Any]], lane: str) -> int:
    return sum(1 for p in positions if position_matches_ema_lane(p, lane))


def open_position_count_for_strategy(
    positions: List[Dict[str, Any]],
    strategy_id: str,
) -> int:
    lane = ema_lane_for_strategy(strategy_id)
    if not lane:
        return 0
    return count_open_positions_for_lane(positions, lane)


def try_reserve_lane_for_signal(
    lock: Dict[str, Any],
    strategy_id: str,
    *,
    open_position_count: int,
    note: str = "",
) -> Tuple[bool, str]:
    """Reserve lock before order placement. False = stand down (no second entry in this lane)."""
    lane = ema_lane_for_strategy(strategy_id)
    if not lane:
        return True, ""
    lane_u = lane.upper()
    holders = _lane_holders(lock)
    if holders.get(lane_u) and open_position_count == 0:
        return False, "Lane reservation in progress — no second entry"
    blocked, why = lane_lock_blocks_entry(lock, lane, open_position_count=open_position_count)
    if blocked:
        return False, why
    if not try_acquire_lane_lock(lock, lane, note=note):
        return False, f"Lane reservation in progress — no second entry"
    return True, ""


def release_lane_reservation(lock: Dict[str, Any], strategy_id: str) -> None:
    lane = ema_lane_for_strategy(strategy_id)
    if lane:
        release_lane_lock(lock, lane)


def filter_signals_one_ema_trade(
    signals: List[Dict[str, Any]],
    lock: Dict[str, Any],
    *,
    open_position_count: int = 0,
    active_positions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """At most one CROSSOVER and one EMA8_PROTOTYPE signal per batch (per lane)."""
    positions = list(active_positions or [])
    proto_open = count_open_positions_for_lane(positions, LANE_PROTOTYPE)
    rev_open = count_open_positions_for_lane(positions, LANE_REVERSAL)
    _ = open_position_count  # legacy kwarg; use active_positions for lane-scoped counts

    holders = _lane_holders(lock or {})
    proto_taken = proto_open > 0 or bool(holders.get(LANE_PROTOTYPE))
    rev_taken = rev_open > 0 or bool(holders.get(LANE_REVERSAL))
    out: List[Dict[str, Any]] = []
    for sig in signals:
        sid = str(sig.get("strategy_id") or "").upper()
        lane = ema_lane_for_strategy(sid)
        if not lane:
            out.append(sig)
            continue
        if lane == LANE_PROTOTYPE:
            if proto_taken:
                continue
            out.append(sig)
            proto_taken = True
            continue
        if lane == LANE_REVERSAL:
            if rev_taken:
                continue
            out.append(sig)
            rev_taken = True
    return out