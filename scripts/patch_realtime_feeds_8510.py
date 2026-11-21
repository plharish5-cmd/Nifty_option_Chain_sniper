#!/usr/bin/env python3
"""Narrow config patch: 10s fast lane + 30s stale contract (does not commit JSON)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "analyst" / "terminal8510_config.json"


def main() -> int:
    if not CFG.exists():
        print(f"Missing {CFG}", file=sys.stderr)
        return 1
    data = json.loads(CFG.read_text(encoding="utf-8"))
    data.setdefault("feed_freshness", {})
    data["feed_freshness"].update({"fast_feed_seconds": 10, "stale_after_seconds": 30, "enabled": True})
    ics = data.setdefault("ict_smc_desk_signals", {})
    ics["fast_feed_seconds"] = 10
    CFG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Patched {CFG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())