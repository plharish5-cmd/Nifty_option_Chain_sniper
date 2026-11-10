"""Human-readable IST timestamps for Port 8510 desk tables (not API wire format)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_FMT = "%d %b %Y, %H:%M:%S IST"


def parse_desk_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("—", "-", "unknown"):
        return None
    if ", " in s and " IST" in s and not s.startswith("20"):
        # Already formatted, e.g. 09 Nov 2026, 14:00:00 IST
        try:
            return datetime.strptime(s.replace(" IST", ""), "%d %b %Y, %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            try:
                return datetime.strptime(s.replace(" IST", ""), "%d %b %Y, %H:%M").replace(tzinfo=IST)
            except ValueError:
                return None
    try:
        norm = s.replace(" IST", "").replace("Z", "+00:00")
        if " " in norm and "T" not in norm:
            norm = norm.replace(" ", "T", 1)
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except (ValueError, TypeError):
        return None


def format_desk_timestamp_ist(value: Any) -> str:
    dt = parse_desk_timestamp(value)
    if dt is None:
        raw = str(value or "").strip()
        return raw if raw else "—"
    return dt.strftime(_FMT)


def format_trade_time_fields(trade: dict) -> dict:
    """Normalize common trade dict timestamp keys for UI display."""
    out = dict(trade)
    for key in ("entry_ist", "exit_ist", "entry_time", "exit_time", "Entry_Time", "Exit_Time"):
        if key in out and out[key]:
            out[key] = format_desk_timestamp_ist(out[key])
    return out
