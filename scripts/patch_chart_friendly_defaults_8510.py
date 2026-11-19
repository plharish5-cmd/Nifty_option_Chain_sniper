#!/usr/bin/env python3
"""Chart-friendly Prototype defaults (owner machine). Backs up config first."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    if not CFG.is_file():
        print("config missing", file=sys.stderr)
        return 1
    bak = CFG.with_suffix(f".json.bak-chart-defaults-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    proto = data.setdefault("scanner", {}).setdefault("ema8_prototype", {})
    proto["retouch_requires_away"] = False
    proto["reentry_same_side"] = True
    proto["reentry_after_stop_loss"] = True
    proto.setdefault("execution_mode", "LIVE_ALLOWED")
    proto.setdefault("live_trade_enabled", False)
    oi = proto.setdefault("oi_filters", {})
    oi["mode"] = "log_only"
    rev = data["scanner"].setdefault("reversal", {})
    rev_oi = rev.setdefault("oi_filters", {})
    rev_oi["mode"] = "log_only"
    rev.setdefault("execution_mode", "LIVE_ALLOWED")
    rev.setdefault("live_trade_enabled", False)
    fp = data["scanner"].setdefault("flagship_1h_pa", {})
    fp.setdefault("execution_mode", "LIVE_ALLOWED")
    fp.setdefault("live_trade_enabled", False)
    radar = data.setdefault("ema_proximity_radar", {})
    radar["enabled"] = False
    radar["auto_highlight_strike_matrix"] = False
    syn = data.setdefault("dhan_oi_synergy", {})
    syn["show_pe_ce_table_in_tab1"] = False
    syn.setdefault("filter_stage2_8ema_synergy", False)
    syn.setdefault("filter_strategy2_reversal_synergy", False)
    syn.setdefault("filter_strategy3_1h_synergy", False)
    syn.setdefault("block_pe_when_bullish", False)
    syn.setdefault("block_ce_when_bearish", False)
    ui = data.setdefault("desk_ui", {})
    ui["show_trend_watchlist"] = False
    ui["show_five_factor_confluence"] = False
    ui["show_eod_summary_card"] = False
    ui["show_desk_link_strip"] = False
    ui["show_dhan_api_panel"] = False
    ff = data.setdefault("five_factor_confluence", {})
    ff["enabled"] = False
    ff["scanner_gate_mode"] = "off"
    ff["scanner_min_score"] = 0
    CFG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("backup:", bak)
    print("applied chart-friendly defaults: retouch_requires_away=OFF, OI log_only (proto+reversal), ema matrix OFF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())