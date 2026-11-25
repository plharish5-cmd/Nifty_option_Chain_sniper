"""Per-lane entry lock for 8 EMA Prototype and Reversal (isolated from other strategies)."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

LANE_PROTOTYPE = "PROTOTYPE"
LANE_REVERSAL = "REVERSAL"


def lane_lock_snapshot(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    st = state if isinstance(state, dict) else {}
    holder = st.get("holder")
    return {
        "holder": holder,
        "since_ist": st.get("since_ist"),
        "note": st.get("note") or (f"{holder} holds the trade lock" if holder else "both lanes eligible"),
    }


def lane_lock_blocks_entry(
    lock: Optional[Dict[str, Any]],
    lane: str,
    *,
    open_position_count: int = 0,
) -> Tuple[bool, str]:
    """True when this lane must not open a new position (lane-scoped open count only)."""
    if open_position_count > 0:
        return True, "Open position exists — no second entry"
    return False, ""


def _lane_holders(lock: Dict[str, Any]) -> Dict[str, bool]:
    holders: Dict[str, bool] = dict(lock.get("holders") or {})
    legacy = str(lock.get("holder") or "").upper()
    if legacy and legacy not in holders:
        holders[legacy] = True
    return holders


def try_acquire_lane_lock(lock: Dict[str, Any], lane: str, *, note: str = "") -> bool:
    lane_u = lane.upper()
    holders = _lane_holders(lock)
    holders[lane_u] = True
    lock["holders"] = holders
    lock["holder"] = lane_u
    lock["since_mono"] = time.monotonic()
    lock["note"] = note or f"{lane_u} armed touch"
    try:
        from zoneinfo import ZoneInfo

        lock["since_ist"] = __import__("datetime").datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )
    except Exception:
        pass
    return True


def release_lane_lock(lock: Dict[str, Any], lane: Optional[str] = None) -> None:
    if not isinstance(lock, dict):
        return
    holders = _lane_holders(lock)
    if lane is not None:
        lane_u = str(lane).upper()
        holders.pop(lane_u, None)
    else:
        holders.clear()
    if holders:
        lock["holders"] = holders
        lock["holder"] = next(iter(holders.keys()))
    else:
        lock.pop("holders", None)
        lock.pop("holder", None)
        lock.pop("since_mono", None)
        lock["note"] = "flat — both lanes eligible"