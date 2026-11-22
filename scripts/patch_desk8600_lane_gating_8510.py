#!/usr/bin/env python3
"""Merge desk8600_lane_gating defaults (LOG_ONLY all lanes/signals). Does not commit JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst" / "terminal8510_config.json"


def main() -> int:
    from analyst.desk8600_lane_gating_8510 import DEFAULT_DESK8600_LANE_GATING

    if not CFG.exists():
        print(f"Missing {CFG}", file=sys.stderr)
        return 1
    data = json.loads(CFG.read_text(encoding="utf-8"))
    sc = data.setdefault("scanner", data)
    if "scanner" in data:
        sc = data["scanner"]
    cur = dict(sc.get("desk8600_lane_gating") or {})
    for lane, defaults in DEFAULT_DESK8600_LANE_GATING.items():
        block = dict(defaults)
        block.update(cur.get(lane) or {})
        cur[lane] = block
    sc["desk8600_lane_gating"] = cur
    data["scanner"] = sc
    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Patched desk8600_lane_gating in {CFG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())