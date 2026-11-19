#!/usr/bin/env python3
"""Narrow owner patch: enable Prototype + chart-friendly + 8600 board record-only."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def _atomic_write(path: Path, data: dict) -> None:
    original = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(mode="w", dir=str(path.parent), prefix=".tomorrow-", delete=False) as stream:
        tmp = Path(stream.name)
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    try:
        os.chmod(tmp, mode)
        if path.read_bytes() != original:
            raise RuntimeError("Config changed concurrently; aborting")
        os.replace(tmp, path)
    finally:
        if tmp.exists() and tmp != path:
            tmp.unlink(missing_ok=True)


def apply(data: dict) -> list[str]:
    changes: list[str] = []
    sc = data.setdefault("scanner", {})

    proto = sc.setdefault("ema8_prototype", {})
    if not proto.get("enabled"):
        proto["enabled"] = True
        changes.append("ema8_prototype.enabled=true")
    proto["retouch_requires_away"] = False
    proto["reentry_same_side"] = True
    proto["reentry_after_stop_loss"] = True
    proto["execution_mode"] = "LIVE_ALLOWED"
    proto["e8_refresh_seconds"] = 3.0
    proto["min_option_premium_inr"] = 200.0
    proto["min_option_premium_floor_inr"] = 150.0
    proto.setdefault("oi_filters", {})["mode"] = "log_only"
    changes.append("prototype: chart-friendly retouch/re-entry, LIVE_ALLOWED route, 3s touch")

    rev = sc.setdefault("reversal", {})
    if not rev.get("enabled", True):
        rev["enabled"] = True
    rev["execution_mode"] = "LIVE_ALLOWED"
    rev["live_trade_enabled"] = True
    rev["candle_trail_enabled"] = True
    rev["candle_trail_activate_pts"] = 20.0
    rev.setdefault("oi_filters", {})["mode"] = "log_only"
    if bool(rev.get("oi_filters", {}).get("log_only", True)):
        rev["oi_filters"]["log_only"] = True
    changes.append("reversal: LIVE_ALLOWED route, OI log_only")

    board = sc.setdefault("desk8600_board", {})
    sources = dict(board.get("sources") or {})
    cleared = [k for k, v in list(sources.items()) if str(v).upper() == "FILTER"]
    for k in cleared:
        sources.pop(k, None)
    board["sources"] = sources
    board.setdefault("apply_to", ["EMA8_PROTOTYPE", "CROSSOVER"])
    if cleared:
        changes.append(f"desk8600_board: removed FILTER on {', '.join(cleared)} (record-only)")

    sc["signal_cooldown_5m_min"] = 0
    sc["skipped_signal_retry_seconds"] = 0
    sc["bollinger_enabled"] = False
    changes.append("scanner: cooldown/retry=0, bollinger off")

    ff = data.setdefault("five_factor_confluence", {})
    ff["enabled"] = False
    ff["scanner_gate_mode"] = "off"
    ff["scanner_min_score"] = 0

    radar = data.setdefault("ema_proximity_radar", {})
    radar["enabled"] = False

    syn = data.setdefault("dhan_oi_synergy", {})
    syn["filter_stage2_8ema_synergy"] = False
    syn["filter_strategy2_reversal_synergy"] = False
    syn["filter_strategy3_1h_synergy"] = False
    syn["block_pe_when_bullish"] = False
    syn["block_ce_when_bearish"] = False

    return changes


def main() -> int:
    if not CFG.is_file():
        print("config missing", file=sys.stderr)
        return 1
    backup_dir = ROOT / "data" / "terminal8510" / "config_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    bak = backup_dir / f"pre_tomorrow_desk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    shutil.copy2(CFG, bak)
    data = json.loads(CFG.read_text(encoding="utf-8"))
    changes = apply(data)
    _atomic_write(CFG, data)
    print("backup:", bak)
    for line in changes:
        print(" -", line)
    print("Desk execution_mode unchanged — arm LIVE in cockpit for broker orders.")
    print("Restart: python3 scripts/restart_8510_engine.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())