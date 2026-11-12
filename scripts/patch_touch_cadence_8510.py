#!/usr/bin/env python3
"""Owner request, 12 Nov 2026: check the 8 EMA touch every 3 seconds, not every 30.

Narrow config patch. Changes ONLY these two keys in analyst/terminal8510_config.json:
    scanner.ema8_prototype.e8_refresh_seconds       -> 3   (8 EMA Prototype)
    scanner.mj_sir_lecture.dynamic_refresh_seconds  -> 3   (Stage 2 / Stage 3 / 8 EMA Reversal)
Everything else in the file is left byte-for-byte as it is (keys, order, your local edits).
Backtest/replay keep their own 30-second grid (backtest_tick_seconds).

Run from the repo root:   python3 scripts/patch_touch_cadence_8510.py [--seconds 3]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst/terminal8510_config.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0, help="touch-check cadence, 2–60 s (default 3)")
    args = ap.parse_args()
    secs = max(2.0, min(float(args.seconds), 60.0))
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    sc = cfg.setdefault("scanner", {})
    changed = []
    for block, key in (("ema8_prototype", "e8_refresh_seconds"), ("mj_sir_lecture", "dynamic_refresh_seconds")):
        b = sc.setdefault(block, {})
        old = b.get(key)
        if old != secs:
            b[key] = secs
            changed.append(f"scanner.{block}.{key}: {old} -> {secs}")
    if not changed:
        print(f"Already at {secs:g} s. Nothing changed.")
        return
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for c in changed:
        print("patched", c)
    print("Restart the 8510 app (or wait: the workers re-read the cadence every loop).")


if __name__ == "__main__":
    main()
