#!/usr/bin/env python3
"""Prefer ₹200 option premium; fallback walk from ₹150 if needed."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    bak = ROOT / "data/terminal8510/config_backups" / f"pre_premium_floor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    proto = data.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["min_option_premium_inr"] = 200.0
    proto["min_option_premium_floor_inr"] = 150.0
    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("backup:", bak)
    print("min_option_premium_inr=200, min_option_premium_floor_inr=150 (try 200 first, else 150–199)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())