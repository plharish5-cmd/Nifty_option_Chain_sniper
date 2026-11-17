"""Off-hours entry guard and stray-position cleanup helpers (8510 desk)."""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Any, Dict, Optional, Tuple

from analyst.ema8_display_audit_8510 import spot_quote_stale


def parse_contract_expiry_date(symbol: str) -> Optional[date]:
    """Expiry calendar date from a desk contract symbol, or None if unparseable."""
    if not symbol:
        return None
    try:
        head = str(symbol).split(" — ")[0].strip()
        if not head:
            return None
        return datetime.strptime(head, "%d %b %Y").date()
    except Exception:
        return None


def new_position_entry_allowed(
    *,
    market_open: bool,
    market_status: str,
    spot_blob: Optional[Dict[str, Any]],
    max_spot_age_sec: float = 5.0,
) -> Tuple[bool, str]:
    """Both market open and fresh spot are required before ANY new position."""
    if not market_open:
        return False, f"Market closed ({market_status}) — entries blocked (paper and live)"
    blob = spot_blob if isinstance(spot_blob, dict) else {}
    if blob.get("is_stale") is True:
        return False, "Spot feed STALE — entries blocked (fail closed)"
    stale, age = spot_quote_stale(blob, max_age_sec=max_spot_age_sec)
    if stale:
        age_s = f"{age:.1f}s" if age is not None else "unknown"
        return False, f"Spot feed STALE ({age_s}) — entries blocked (fail closed)"
    return True, ""


def resolve_position_cleanup_reason(
    *,
    symbol: str,
    now: datetime,
    market_open: bool,
    eod_square_off_time: dtime,
) -> Optional[str]:
    """Return close reason tag, or None if position should stay open."""
    today = now.date()
    exp = parse_contract_expiry_date(symbol)
    if exp is not None and exp < today:
        return "EXPIRED_CONTRACT_CLEANUP"
    t = now.time()
    if t >= eod_square_off_time:
        hh = eod_square_off_time.strftime("%H:%M")
        return f"EOD_SQUARE_OFF_{hh}"
    if not market_open:
        return "OFF_HOURS_CLEANUP"
    return None


def format_auto_close_log_line(
    *,
    symbol: str,
    mode: str,
    reason: str,
    at: datetime,
    ltp: float,
) -> str:
    mode_u = "PAPER" if "PAPER" in str(mode).upper() else "REAL"
    ts = at.strftime("%H:%M:%S")
    return f"AUTO-CLOSE {symbol} ({mode_u}) reason={reason} at {ts} IST, LTP {ltp:.2f}"