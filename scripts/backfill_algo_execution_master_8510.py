#!/usr/bin/env python3
"""One-time / repeat-safe backfill: closed positions → algo_execution_master."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyst.algo_execution_master_8510 import backfill_algo_execution_master_from_positions
from analyst.terminal8510_backend import QuantTerminalBackend


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill immutable algo execution master from positions")
    parser.add_argument("--dry-run", action="store_true", help="Count rows only; do not insert")
    parser.add_argument("--ledger", type=str, default="", help="Override ledger sqlite path")
    args = parser.parse_args()
    if args.ledger:
        ledger = Path(args.ledger)
    else:
        backend = QuantTerminalBackend()
        ledger = backend.ledger_db
    stats = backfill_algo_execution_master_from_positions(ledger, dry_run=args.dry_run)
    print(json.dumps({**stats, "ledger": str(ledger)}, indent=2))
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
