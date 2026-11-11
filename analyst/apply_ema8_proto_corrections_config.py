#!/usr/bin/env python3
"""
Narrow patch script for 8510 config — touches only scanner.ema8_prototype keys
named in the 8 EMA Prototype corrections prompt.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "terminal8510_config.json"


def patch_config() -> None:
    if not CONFIG_PATH.exists():
        print(f"Config file not found: {CONFIG_PATH}")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    scanner = data.setdefault("scanner", {})
    ema8_proto = scanner.setdefault("ema8_prototype", {})

    # Apply named keys per Harish's specifications
    ema8_proto["touch_tol_pts"] = 3.0
    ema8_proto["touch_tol_slow_pts"] = 0.0
    ema8_proto["slow_market_exact_line"] = False
    ema8_proto["retouch_requires_away"] = True
    ema8_proto["arm_until_opposite_close"] = True

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Successfully patched {CONFIG_PATH}")
    for k in [
        "touch_tol_pts",
        "touch_tol_slow_pts",
        "slow_market_exact_line",
        "retouch_requires_away",
        "arm_until_opposite_close",
    ]:
        print(f"scanner.ema8_prototype.{k} = {ema8_proto[k]}")


if __name__ == "__main__":
    patch_config()
