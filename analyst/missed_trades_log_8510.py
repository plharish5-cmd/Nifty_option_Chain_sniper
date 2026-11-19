"""Append-only miss watchdog log (observe only)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("missed_trades_log_8510")


def default_log_path(root: Optional[Path] = None) -> Path:
    base = root or Path(__file__).resolve().parents[1]
    return base / "data" / "terminal8510" / "missed_trades.log"


def append_miss_event(
    *,
    event: str,
    setup: str,
    side: str,
    detail: str,
    bar_key: str = "",
    log_path: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    path = log_path or default_log_path()
    row = {
        "ts_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "setup": setup,
        "side": side,
        "bar_key": bar_key,
        "detail": detail[:500],
    }
    if extra:
        row["extra"] = extra
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("Could not write missed_trades.log: %s", exc)


def try_telegram_alert(text: str) -> None:
    try:
        from scheduler import send_telegram_alert

        send_telegram_alert(text)
    except Exception as exc:
        log.debug("Telegram alert skipped: %s", exc)