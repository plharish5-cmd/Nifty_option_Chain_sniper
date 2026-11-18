#!/usr/bin/env python3
"""Backup and purge junk PAPER rows from the 8510 ledger (never LIVE/BOTH)."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "analyst/terminal8510_config.json"
IST = ZoneInfo("Asia/Kolkata")

from analyst.desk_scope_8510 import is_disabled_lane_setup, is_test_fixture_text  # noqa: E402
from analyst.offhours_cleanup_8510 import parse_contract_expiry_date  # noqa: E402


def _ledger_path() -> Path:
    if not CFG.is_file():
        return ROOT / "data/terminal8510/ledger.sqlite3"
    data = json.loads(CFG.read_text(encoding="utf-8"))
    raw = data.get("ledger_db_path") or "data/terminal8510/ledger.sqlite3"
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _is_live_mode(mode: Any) -> bool:
    u = str(mode or "").upper()
    if not u:
        return False
    if u in ("LIVE", "BOTH", "REAL", "ALGO_LIVE_ONLY"):
        return True
    if "LIVE" in u and "PAPER" not in u:
        return True
    return False


def _scanner_cfg() -> Dict[str, Any]:
    if not CFG.is_file():
        return {}
    return json.loads(CFG.read_text(encoding="utf-8")).get("scanner") or {}


def _row_blob(row: Dict[str, Any]) -> str:
    return " ".join(
        str(row.get(k) or "")
        for k in ("order_id", "strategy_setup", "strategy_name", "strategy_id", "symbol", "position_id")
    )


def classify_row(row: Dict[str, Any], *, drop_disabled: bool, today: date) -> List[str]:
    reasons: List[str] = []
    if _is_live_mode(row.get("mode")):
        return reasons
    if is_test_fixture_text(row.get("strategy_setup"), row.get("strategy_name"), row.get("order_id"), row.get("position_id")):
        reasons.append("test_fixture")
    exp = parse_contract_expiry_date(str(row.get("symbol") or ""))
    if exp is not None and exp < today:
        reasons.append("expired_contract")
    if drop_disabled and is_disabled_lane_setup(
        _scanner_cfg(),
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_setup") or row.get("strategy_name") or ""),
    ):
        reasons.append("disabled_lane")
    return reasons


def _fetch(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    except sqlite3.Error:
        return []


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Clean junk PAPER rows from 8510 ledger")
    ap.add_argument("--preview", action="store_true", help="List rows only (default)")
    ap.add_argument("--drop", action="store_true", help="Delete after backup")
    ap.add_argument(
        "--drop-disabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove rows from disabled lanes (default on)",
    )
    ap.add_argument("--ledger", default="", help="Override ledger sqlite path")
    args = ap.parse_args(argv)
    if args.drop and args.preview:
        print("use either --preview or --drop", file=sys.stderr)
        return 2
    do_drop = bool(args.drop)
    ledger = Path(args.ledger) if args.ledger else _ledger_path()
    if not ledger.is_file():
        print("ledger not found:", ledger, file=sys.stderr)
        return 1
    today = datetime.now(IST).date()
    to_delete: Dict[str, Set[str]] = {"orders": set(), "positions": set()}
    summary: Dict[str, int] = {"test_fixture": 0, "expired_contract": 0, "disabled_lane": 0}

    with sqlite3.connect(ledger) as conn:
        for table, id_col in (("orders", "order_id"), ("positions", "position_id")):
            for row in _fetch(conn, table):
                if _is_live_mode(row.get("mode")):
                    continue
                reasons = classify_row(row, drop_disabled=bool(args.drop_disabled), today=today)
                if not reasons:
                    continue
                rid = str(row.get(id_col) or "")
                if not rid:
                    continue
                to_delete[table].add(rid)
                for r in reasons:
                    summary[r] = summary.get(r, 0) + 1

    print("ledger:", ledger)
    print("today:", today.isoformat())
    print("would remove — orders:", len(to_delete["orders"]), "positions:", len(to_delete["positions"]))
    print("by reason (row hits, may overlap):", summary)
    for table in ("orders", "positions"):
        for rid in sorted(to_delete[table])[:40]:
            print(f"  [{table}] {rid}")
        if len(to_delete[table]) > 40:
            print(f"  ... +{len(to_delete[table]) - 40} more in {table}")

    if not do_drop:
        print("(preview only — pass --drop to delete after backup)")
        return 0

    if not any(to_delete.values()):
        print("nothing to delete")
        return 0

    bak = ledger.with_suffix(f".sqlite3.bak-clean-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(ledger, bak)
    print("backup:", bak)

    with sqlite3.connect(ledger) as conn:
        for table, id_col in (("orders", "order_id"), ("positions", "position_id")):
            ids = list(to_delete[table])
            if not ids:
                continue
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT {id_col}, mode FROM {table} WHERE {id_col} IN ({placeholders})",
                ids,
            ).fetchall()
            for rid, mode in rows:
                if _is_live_mode(mode):
                    raise RuntimeError(f"Refusing delete on live row {table}:{rid} mode={mode}")
            conn.execute(f"DELETE FROM {table} WHERE {id_col} IN ({placeholders})", ids)
        conn.commit()
    print("deleted ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())