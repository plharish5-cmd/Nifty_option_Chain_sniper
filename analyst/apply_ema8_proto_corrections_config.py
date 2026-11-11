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

    # Apply named keys
    ema8_proto["retouch_requires_away"] = True
    ema8_proto["arm_until_opposite_close"] = True

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Successfully patched {CONFIG_PATH}")
    print(f"scanner.ema8_prototype.retouch_requires_away = {ema8_proto['retouch_requires_away']}")
    print(f"scanner.ema8_prototype.arm_until_opposite_close = {ema8_proto['arm_until_opposite_close']}")


if __name__ == "__main__":
    patch_config()
