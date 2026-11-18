#!/usr/bin/env python3
"""Add require_price_confirm (default OFF) to Prototype OI momentum + optional Reversal mirror.

Does not commit terminal8510_config.json to git — run on owner machine only.

  python3 scripts/patch_price_confirm_matrix_recorder_8510.py
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"
BACKUP = CFG.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")


def main() -> None:
    if not CFG.is_file():
        raise SystemExit(f"missing {CFG}")
    shutil.copy2(CFG, BACKUP)
    print("backup:", BACKUP)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    scanner = cfg.setdefault("scanner", {})
    proto = scanner.setdefault("ema8_prototype", {})
    oi = proto.setdefault("oi_filters", {})
    mom = oi.setdefault("momentum", {})
    if "require_price_confirm" not in mom:
        mom["require_price_confirm"] = False
        oi["momentum"] = mom
        proto["oi_filters"] = oi
    rev = scanner.get("reversal")
    if isinstance(rev, dict):
        roi = rev.get("oi_filters")
        if isinstance(roi, dict):
            rm = roi.setdefault("momentum", {}) if isinstance(roi.get("momentum"), dict) else {}
            if isinstance(roi.get("momentum"), dict):
                rm = roi["momentum"]
            if "require_price_confirm" not in rm:
                rm["require_price_confirm"] = False
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched require_price_confirm on scanner.ema8_prototype.oi_filters.momentum (default false)")


if __name__ == "__main__":
    main()