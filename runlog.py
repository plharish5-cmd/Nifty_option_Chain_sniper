"""
runlog.py — save everything a script prints to data/logs/<name>_YYYY-MM-DD_HHMMSS.log (and still show it).

    import runlog; runlog.start("backtest_five_rule")

The log path is printed at the end, so you can open it or paste it into the chat.
"""
from __future__ import annotations

import atexit
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "data" / "logs"
_started = None


class _Tee:
    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        try:
            self.fh.write(s)
            self.fh.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        for x in (self.stream, self.fh):
            try:
                x.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self.stream, "isatty", lambda: False)()

    def __getattr__(self, k):
        return getattr(self.stream, k)


def start(name: str) -> Path | None:
    """Tee stdout + stderr into a new log file. Safe to call once per process; never raises."""
    global _started
    if _started:
        return _started
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{name}_{datetime.now():%Y-%m-%d_%H%M%S}.log"
        fh = path.open("w", encoding="utf-8")
        fh.write(f"$ {' '.join(sys.argv)}\n")
        sys.stdout, sys.stderr = _Tee(sys.stdout, fh), _Tee(sys.stderr, fh)
        _started = path

        def _bye():
            try:
                sys.stdout.stream.write(f"\n[log saved → {path}]\n")
                fh.close()
            except Exception:
                pass
        atexit.register(_bye)
        return path
    except Exception:
        return None
