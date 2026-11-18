#!/usr/bin/env python3
"""Add scanner.ema8_prototype.oi_filters defaults (all switches OFF, mode log_only).

Backs up analyst/terminal8510_config.json first. Touches only the oi_filters block.

Run from repo root:  python3 scripts/patch_add_oi_filters_8510.py
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"
BACKUP = CFG.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")

DEFAULT_OI = {
    "mode": "log_only",
    "rule": "all_agree",
    "neutral_blocks": False,
    "max_age_sec": 420,
    "switches": {
        "writer_flow": False,
        "price_oi": False,
        "total_oi": False,
        "fresh_oi": False,
        "volume_oi": False,
    },
    "momentum": {
        "window_min": 10,
        "sig_min": 0.15,
        "sig_extreme": 0.30,
        "require_cumd_confirm": False,
        "require_volume_confirm": False,
        "require_price_confirm": False,
    },
}


def main() -> None:
    if not CFG.is_file():
        raise SystemExit(f"missing {CFG}")
    shutil.copy2(CFG, BACKUP)
    print("backup:", BACKUP)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    block = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    before = block.get("oi_filters")
    if isinstance(before, dict):
        merged = dict(DEFAULT_OI)
        merged.update(before)
        sw = dict(DEFAULT_OI["switches"])
        sw.update(before.get("switches") or {})
        merged["switches"] = sw
        mom = dict(DEFAULT_OI["momentum"])
        mom.update(before.get("momentum") or {})
        merged["momentum"] = mom
        block["oi_filters"] = merged
    else:
        block["oi_filters"] = dict(DEFAULT_OI)
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched scanner.ema8_prototype.oi_filters")
    print("before:", before)
    print("after:", block["oi_filters"])


if __name__ == "__main__":
    main()