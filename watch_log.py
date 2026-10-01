"""
watch_log.py — follow data/agt_briefing_watch.log in the terminal WITH COLOURS (like `tail -f`, but coloured).

  .venv/bin/python watch_log.py            # last 200 lines, then follow live (Ctrl+C to stop)
  .venv/bin/python watch_log.py -n 1000    # more history first
  .venv/bin/python watch_log.py --only-pa  # only your price-action + 8 EMA signals (15m / 5m / 3m)
  .venv/bin/python watch_log.py --file data/briefing_logs/2026-09-30.log --no-follow   # an old day

Colours
  [PA 15m SUPER]   bold white on MAGENTA header, whole block bright magenta   <- the one to act on
  [PA 5m]          cyan block
  [PA 3m]          yellow block (noisy)
  [8EMA 15m SUPER] / [8EMA 5m] / [8EMA 3m]   same colours as PA (your 8 EMA method)
  ★ MAIN SIGNAL    bold green
  ⚠️ EXIT HINT      bold red
  🟪 15-MIN / 🟦 5-MIN periodic headers   magenta / blue
  errors / tracebacks                    red
The log file itself stays plain text; colours are added only on screen.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import config

RESET = "\033[0m"
STYLE = {
    "15m": "\033[1;95m", "15m_head": "\033[1;97;45m",
    "5m": "\033[96m", "5m_head": "\033[1;30;46m",
    "3m": "\033[93m", "3m_head": "\033[1;30;43m",
    "main": "\033[1;92m", "exit": "\033[1;91m", "err": "\033[91m",
    "h15": "\033[35m", "h5": "\033[34m", "mentor": "\033[33m",
}
TAGS = {"[PA 15m SUPER]": "15m", "[PA 5m]": "5m", "[PA 3m]": "3m",
        "[8EMA 15m SUPER]": "15m", "[8EMA 5m]": "5m", "[8EMA 3m]": "3m"}


class Painter:
    def __init__(self, only_pa: bool = False):
        self.block = None          # current PA timeframe block until a blank line
        self.only_pa = only_pa

    def paint(self, line: str) -> str | None:
        s = line.rstrip("\n")
        if not s.strip():
            self.block = None
            return None if self.only_pa else ""
        tf = next((v for k, v in TAGS.items() if k in s), None)
        if tf:
            self.block = tf
            return f"{STYLE[tf + '_head']} {s} {RESET}"
        if "PA 15-MIN SUPER SIGNAL" in s:
            self.block = "15m"
            return f"{STYLE['15m_head']} {s} {RESET}"
        if s.startswith("PA 5-min / 3-min"):
            self.block = "5m"
            return f"{STYLE['5m_head']} {s} {RESET}"
        if "EXIT HINT" in s:
            return f"{STYLE['exit']}{s}{RESET}"
        if self.block:
            return f"{STYLE[self.block]}{s}{RESET}"
        if self.only_pa:
            return None
        if "★ MAIN" in s:
            return f"{STYLE['main']}{s}{RESET}"
        if "Traceback" in s or "Error" in s or "ERROR" in s:
            return f"{STYLE['err']}{s}{RESET}"
        if s.startswith("──── 🟪 15-MIN"):
            return f"{STYLE['h15']}{s}{RESET}"
        if s.startswith("──── 🟦 5-MIN"):
            return f"{STYLE['h5']}{s}{RESET}"
        if s.startswith("──── 🟧 MENTOR"):
            return f"{STYLE['mentor']}{s}{RESET}"
        return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--file")
    ap.add_argument("--only-pa", action="store_true")
    ap.add_argument("--no-follow", action="store_true")
    a = ap.parse_args()
    path = Path(a.file) if a.file else Path(getattr(config, "DATA_DIR", Path("data"))) / "agt_briefing_watch.log"
    if not path.exists():
        print(f"{path} not found")
        return 1
    if os.name == "nt":
        os.system("")                                  # enable ANSI on Windows terminals
    p = Painter(a.only_pa)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for ln in lines[-a.n:]:
            out = p.paint(ln)
            if out is not None:
                print(out)
        if a.no_follow:
            return 0
        try:
            while True:
                ln = f.readline()
                if not ln:
                    time.sleep(1.0)
                    if path.stat().st_size < f.tell():     # file rotated / truncated
                        f.seek(0)
                    continue
                out = p.paint(ln)
                if out is not None:
                    print(out, flush=True)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
