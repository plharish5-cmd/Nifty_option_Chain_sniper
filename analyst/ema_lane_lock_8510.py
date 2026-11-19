"""One open setup at a time across 8 EMA Prototype and Reversal lanes."""
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
    """True when this lane must not open a new position."""
    if open_position_count > 0:
        return True, "Open position exists — no second entry"
    if not isinstance(lock, dict):
        return False, ""
    holder = str(lock.get("holder") or "").upper()
    if not holder:
        return False, ""
    if holder == lane.upper():
        return False, ""
    other = LANE_PROTOTYPE if lane.upper() == LANE_REVERSAL else LANE_REVERSAL
    return True, f"Standing down — {other} holds the trade lock"


def try_acquire_lane_lock(lock: Dict[str, Any], lane: str, *, note: str = "") -> bool:
    lane_u = lane.upper()
    holder = str(lock.get("holder") or "").upper()
    if holder and holder != lane_u:
        return False
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
    if lane is not None and str(lock.get("holder") or "").upper() != str(lane).upper():
        return
    lock.pop("holder", None)
    lock.pop("since_mono", None)
    lock["note"] = "flat — both lanes eligible"