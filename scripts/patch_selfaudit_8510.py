#!/usr/bin/env python3
"""Add miss_watchdog defaults to scanner config — owner machine only; backs up first."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    if not CFG.is_file():
        print("missing", CFG, file=sys.stderr)
        return 1
    bak = CFG.with_suffix(f".json.bak-selfaudit-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CFG, bak)
    print("backup:", bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", {})
    sc["miss_watchdog_enabled"] = True
    sc["armed_idle_heartbeat_min"] = 10
    sc["lane_reservation_max_min"] = 15
    sc["quote_retry_max"] = 5
    sc.setdefault("miss_watchdog", {}).update(
        {
            "enabled": True,
            "qualification_timeout_sec": 12,
            "armed_idle_heartbeat_min": 10,
        }
    )
    CFG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched miss_watchdog + lane_reservation_max_min + quote_retry_max")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())