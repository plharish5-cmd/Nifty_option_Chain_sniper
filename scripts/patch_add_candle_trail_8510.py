#!/usr/bin/env python3
"""Add scanner.ema8_prototype.candle_trail defaults (disabled). Backs up config first."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"
BACKUP = CFG.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")

DEFAULT_CT = {
    "enabled": False,
    "activate_pts": 40.0,
    "candle_tf_min": 15,
    "use_completed_candles": True,
}


def main() -> None:
    if not CFG.is_file():
        raise SystemExit(f"missing {CFG}")
    shutil.copy2(CFG, BACKUP)
    print("backup:", BACKUP)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    block = cfg.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    before = block.get("candle_trail")
    merged = dict(DEFAULT_CT)
    if isinstance(before, dict):
        merged.update(before)
    block["candle_trail"] = merged
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched scanner.ema8_prototype.candle_trail")
    print("before:", before)
    print("after:", block["candle_trail"])


if __name__ == "__main__":
    main()