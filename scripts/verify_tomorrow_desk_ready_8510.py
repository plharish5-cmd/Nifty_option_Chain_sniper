#!/usr/bin/env python3
"""Read-only checklist before market open."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> int:
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.get("scanner") or {}
    proto = sc.get("ema8_prototype") or {}
    rev = sc.get("reversal") or {}
    board = sc.get("desk8600_board") or {}
    filters = [k for k, v in (board.get("sources") or {}).items() if str(v).upper() == "FILTER"]

    ok = True
    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(("PASS" if cond else "FAIL"), msg)
        if not cond:
            ok = False

    check(bool(proto.get("enabled")), "Prototype enabled")
    check(bool(rev.get("enabled", True)), "Reversal enabled")
    check(float(proto.get("e8_refresh_seconds") or 0) <= 3.0, "Prototype touch cadence <= 3s")
    check(float(proto.get("min_option_premium_floor_inr") or 0) == 150.0, "Premium floor ₹150")
    check(bool(rev.get("candle_trail_enabled")), "Reversal candle trail on")
    check(proto.get("execution_mode") == "LIVE_ALLOWED", "Prototype route LIVE_ALLOWED")
    check(rev.get("execution_mode") == "LIVE_ALLOWED", "Reversal route LIVE_ALLOWED")
    check(bool(rev.get("live_trade_enabled")), "Reversal live_trade_enabled")
    check(not filters, f"8600 board has no FILTER sources (found: {filters})")
    check(str((proto.get("oi_filters") or {}).get("mode")).lower() == "log_only", "Prototype OI log_only")
    check(float(sc.get("signal_cooldown_5m_min") or 0) == 0, "Inter-signal cooldown 0")
    desk = str(data.get("execution_mode") or "PAPER_ONLY").upper()
    print("INFO", f"Desk execution_mode={desk} (arm LIVE in UI for real broker)")
    print("INFO", f"Prototype live_trade_enabled={proto.get('live_trade_enabled')}")
    print("INFO", f"Reversal live_trade_enabled={rev.get('live_trade_enabled')}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())