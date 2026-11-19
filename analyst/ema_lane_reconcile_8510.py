"""Startup lane-lock reconcile — release holder when no OPEN position exists."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from analyst.ema_lane_lock_8510 import release_lane_lock

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("ema_lane_reconcile_8510")


def _reservation_age_minutes(lock: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    raw = str(lock.get("since_ist") or "").strip()
    if not raw:
        return None
    now = now or datetime.now(IST)
    try:
        since = datetime.fromisoformat(raw.replace(" IST", ""))
        if since.tzinfo is None:
            since = since.replace(tzinfo=IST)
        return (now - since).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def reconcile_lane_lock_on_startup(
    backend: Any,
    *,
    max_reservation_min: float = 15.0,
) -> bool:
    """Release orphaned lane lock after reconstruct, before scanner trading.

    Returns True if a release was performed.
    """
    lock = getattr(backend, "_ema_lane_lock", None) or {}
    holder = str(lock.get("holder") or "").strip()
    if not holder:
        return False
    try:
        n_open = len(backend.get_active_positions())
    except Exception:
        n_open = 0
    age = _reservation_age_minutes(lock)
    stale_reservation = age is not None and age > float(max_reservation_min)
    if n_open > 0 and not stale_reservation:
        return False
    if n_open == 0 or stale_reservation:
        reason = (
            "LANE LOCK AUTO-RELEASED (no open position)"
            if n_open == 0
            else f"LANE LOCK AUTO-RELEASED (reservation age {age:.0f}m, no fill)"
        )
        release_lane_lock(lock)
        backend._ema_lane_lock = lock
        if hasattr(backend, "_save_ema_lane_lock_state"):
            backend._save_ema_lane_lock_state()
        log.warning("%s — was %s", reason, holder)
        setattr(backend, "_last_lane_reconcile_log", reason)
        return True
    return False