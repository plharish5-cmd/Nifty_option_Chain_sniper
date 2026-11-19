"""Timestamped engine exit / crash lines for port 8510 (diagnostics only)."""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_log = logging.getLogger("engine_exit_8510")
_INSTALLED = False
_HEARTBEAT_STOP: Optional[threading.Event] = None


def default_exit_log_path(root: Optional[Path] = None) -> Path:
    base = root or Path(__file__).resolve().parents[1]
    return base / "data" / "terminal8510" / "engine_exits.log"


def _append(path: Path, event: str, detail: str = "", extra: Optional[dict] = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "pid": os.getpid(),
            "event": event,
            "detail": detail[:2000],
        }
        if extra:
            row["extra"] = extra
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("Could not write engine exit log: %s", exc)


def install_engine_exit_logging(
    *,
    log_path: Optional[Path] = None,
    heartbeat_seconds: float = 60.0,
    label: str = "8510",
) -> Path:
    """Hook unhandled exceptions, signals, and clean shutdown."""
    global _INSTALLED, _HEARTBEAT_STOP
    path = log_path or default_exit_log_path()
    if _INSTALLED:
        return path
    _INSTALLED = True

    prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        lines = "".join(traceback.format_exception(exc_type, exc, tb))
        _append(path, "UNHANDLED_EXCEPTION", lines)
        if prev_hook:
            prev_hook(exc_type, exc, tb)

    sys.excepthook = _hook

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        _append(path, "SIGNAL", f"Received {name}; process exiting")

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (OSError, ValueError):
                pass

    @atexit.register
    def _clean_shutdown():
        _append(path, "CLEAN_SHUTDOWN", f"{label} main loop ended (atexit)")
        if _HEARTBEAT_STOP is not None:
            _HEARTBEAT_STOP.set()

    _append(path, "STARTUP", f"{label} process started")

    _HEARTBEAT_STOP = threading.Event()

    def _beat():
        while not _HEARTBEAT_STOP.wait(heartbeat_seconds):
            _append(path, "HEARTBEAT", f"{label} alive")

    threading.Thread(target=_beat, name="engine-exit-heartbeat", daemon=True).start()
    return path


def note_abnormal_exit(path: Path, reason: str, **extra: Any) -> None:
    _append(path, "ABNORMAL_EXIT", reason, extra=extra or None)