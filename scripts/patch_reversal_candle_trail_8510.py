#!/usr/bin/env python3
"""Enable Reversal spot candle trail (activate at 20 pts) — config only."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    bak = ROOT / "data/terminal8510/config_backups" / f"pre_reversal_trail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    rev = data.setdefault("scanner", {}).setdefault("reversal", {})
    rev["candle_trail_enabled"] = True
    rev["candle_trail_activate_pts"] = 20.0
    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("backup:", bak)
    print("reversal candle_trail_enabled=true, activate_pts=20")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())