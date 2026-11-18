#!/usr/bin/env python3
"""Scope live desk to Prototype + Reversal + 1H Flagship (reversible config)."""
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
    bak = CFG.with_suffix(f".json.bak-desk-scope-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CFG, bak)
    print("backup:", bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", {})
    sc["desk_scope_8510"] = {
        "enabled_strategy_lanes": ["EMA8_PROTOTYPE", "CROSSOVER", "FLAGSHIP"],
        "stage2_enabled": False,
        "stage3_enabled": False,
        "prototype_enabled": True,
        "reversal_enabled": True,
        "flagship_1h_enabled": True,
        "ticket_lane_enabled": False,
        "matrix_scalp_enabled": False,
        "pa_sr_live_enabled": False,
    }
    sc.setdefault("desk8600_ticket_lane", {})["enabled"] = False
    data.setdefault("matrix_scalp", {})["enabled"] = False
    data.setdefault("pa_sr_homework", {})["enabled"] = False
    data.setdefault("pa_sr_homework", {})["live_paper_enabled"] = False
    data.setdefault("trend_capture_v192", {})["enabled"] = False
    CFG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched desk_scope_8510 — only Prototype, Reversal (CROSSOVER), 1H Flagship active lanes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())