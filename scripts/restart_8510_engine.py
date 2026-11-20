#!/usr/bin/env python3
"""Relaunch engine_8510_live.py after the current process exits (detach)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine_8510_live.py"
DELAY_SEC = float(os.environ.get("ENGINE_RESTART_DELAY_SEC", "2.5"))


def main() -> int:
    if not ENGINE.is_file():
        print(f"engine not found: {ENGINE}", file=sys.stderr)
        return 1
    time.sleep(DELAY_SEC)
    subprocess.Popen(
        [sys.executable, str(ENGINE)],
        cwd=str(ROOT),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
