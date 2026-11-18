#!/usr/bin/env python3
"""Enable full-stack options (log-only OI, Reversal LIVE) — owner machine only; backs up config."""
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
    bak = CFG.with_suffix(f".json.bak-finish-enable-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CFG, bak)
    print("backup:", bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", {})
    rev = sc.setdefault("reversal", {})
    rev.update(
        {
            "enabled": True,
            "timeframe_mode": "15m",
            "pre_arm_15m_from_5m": False,
            "ema_separation_enabled": True,
            "candle_trail_enabled": True,
            "candle_trail_activate_pts": 20.0,
            "execution_mode": "LIVE_ALLOWED",
            "oi_filters": {
                "enabled": True,
                "log_only": True,
                "mode": "log_only",
                "rule": "all_agree",
                "neutral_blocks": False,
                "momentum_window": 10,
                "momentum_sig_bull": 0.15,
                "momentum_sig_bear": 0.30,
                "require_price_confirm": False,
                "switches": {
                    "writer_flow": True,
                    "total_oi": True,
                    "fresh_oi": True,
                    "price_oi": False,
                    "volume_oi": True,
                },
            },
        }
    )
    proto = sc.setdefault("ema8_prototype", {})
    oi = proto.setdefault("oi_filters", {})
    oi.setdefault("mode", "log_only")
    oi["mode"] = "log_only"
    mom = oi.setdefault("momentum", {})
    mom.setdefault("require_price_confirm", False)
    for k in ("writer_flow", "total_oi", "fresh_oi", "volume_oi"):
        oi.setdefault("switches", {})[k] = True
    oi3 = sc.setdefault("oi_3m_feature", {})
    oi3["enabled"] = True
    g = oi3.setdefault("gate", {})
    g["enabled"] = True
    g["mode"] = "log_only"
    CFG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("patched scanner.reversal LIVE_ALLOWED + filters log_only; prototype/oi_3m log_only ON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())