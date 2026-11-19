#!/usr/bin/env python3
"""One-shot: all tomorrow config fixes (idempotent)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    bak = ROOT / "data/terminal8510/config_backups" / f"pre_final_desk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", {})
    proto = sc.setdefault("ema8_prototype", {})
    rev = sc.setdefault("reversal", {})

    proto["enabled"] = True
    proto["e8_refresh_seconds"] = 3.0
    proto["retouch_requires_away"] = False
    proto["min_option_premium_inr"] = 200.0
    proto["min_option_premium_floor_inr"] = 150.0
    proto["execution_mode"] = "LIVE_ALLOWED"
    proto.setdefault("oi_filters", {})["mode"] = "log_only"

    rev["enabled"] = True
    rev["execution_mode"] = "LIVE_ALLOWED"
    rev["live_trade_enabled"] = True
    rev["candle_trail_enabled"] = True
    rev["candle_trail_activate_pts"] = 20.0
    rev.setdefault("oi_filters", {})["mode"] = "log_only"
    rev.setdefault("oi_filters", {})["log_only"] = True

    board = sc.setdefault("desk8600_board", {})
    board["sources"] = {
        k: v for k, v in (board.get("sources") or {}).items() if str(v).upper() != "FILTER"
    }
    board.setdefault("apply_to", ["EMA8_PROTOTYPE", "CROSSOVER"])

    sc["signal_cooldown_5m_min"] = 0
    sc["skipped_signal_retry_seconds"] = 0
    sc["bollinger_enabled"] = False

    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("backup:", bak)
    print("consolidated desk config written; restart 8510 engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())