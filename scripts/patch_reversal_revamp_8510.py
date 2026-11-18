#!/usr/bin/env python3
"""Patch scanner.reversal defaults into terminal8510_config.json (backup first)."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst" / "terminal8510_config.json"
DEFAULTS = {
    "timeframe_mode": "15m",
    "pre_arm_15m_from_5m": False,
    "ema_separation_enabled": False,
    "ema_separation_min_pts": 5.0,
    "candle_trail_enabled": False,
    "candle_trail_activate_pts": 20.0,
    "execution_mode": "PAPER_ONLY",
    "oi_filters": {
        "enabled": False,
        "log_only": True,
        "all_agree": False,
        "momentum_window": 10,
        "momentum_sig_bull": 0.15,
        "momentum_sig_bear": 0.30,
    },
}


def main() -> int:
    if not CFG.exists():
        print("Config not found:", CFG, file=sys.stderr)
        return 1
    bak = CFG.with_suffix(f".json.bak-reversal-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", {})
    rev = sc.setdefault("reversal", {})
    for k, v in DEFAULTS.items():
        rev.setdefault(k, v)
    CFG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("Patched", CFG, "backup", bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())