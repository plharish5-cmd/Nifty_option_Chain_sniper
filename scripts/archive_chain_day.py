#!/usr/bin/env python3
"""
scripts/archive_chain_day.py — Nightly Parquet Archival Utility for NIFTY Chain & Candles.

Purpose:
  Exports closed session snapshots from `chain_snap` and `candles_1m` in `ict_smc.sqlite3`
  into cold-storage Parquet files:
    - data/archive/chain/YYYY-MM-DD.parquet
    - data/archive/candles/YYYY-MM-DD.parquet

Safety Invariants:
  1. chain_snap and candles_1m in SQLite remain the sole live working store.
  2. The Port 8510 app and live terminal NEVER read these archive files (backup/insurance only).
  3. Idempotent: skips if file already exists unless --force is specified.
  4. Fully backfills all historical dates present in chain_snap.

Usage:
  python3 scripts/archive_chain_day.py --all           # Archive/backfill all dates in DB
  python3 scripts/archive_chain_day.py --date 2026-11-05 # Archive specific session
  python3 scripts/archive_chain_day.py --date 2026-11-05 --force # Overwrite
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_DEFAULT = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
CHAIN_ARCHIVE_DIR = ROOT / "data" / "archive" / "chain"
CANDLES_ARCHIVE_DIR = ROOT / "data" / "archive" / "candles"

IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("archive_chain_day")

CHAIN_COLS = [
    "expiry", "ts", "strike", "ce_oi", "pe_oi",
    "ce_ltp", "pe_ltp", "ce_vol", "pe_vol",
    "ce_bid", "ce_ask", "pe_bid", "pe_ask"
]

CANDLES_COLS = ["sec_id", "ts", "o", "h", "l", "c", "v"]


def get_available_dates(conn: sqlite3.Connection, table: str) -> List[str]:
    """Returns all unique IST dates present in table."""
    cur = conn.cursor()
    query = f"""
        SELECT DISTINCT date(ts, 'unixepoch', '+330 minutes') as dt
        FROM {table}
        WHERE dt IS NOT NULL
        ORDER BY dt ASC
    """
    rows = cur.execute(query).fetchall()
    return [r[0] for r in rows if r[0]]


def archive_session_chain(
    conn: sqlite3.Connection,
    date_str: str,
    output_dir: Path = CHAIN_ARCHIVE_DIR,
    force: bool = False
) -> Optional[Path]:
    """Archives chain_snap rows for date_str to Parquet using indexed timestamp ranges."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{date_str}.parquet"

    if out_file.exists() and not force:
        log.info("Chain archive already exists: %s (skipping, use --force to overwrite)", out_file.name)
        return out_file

    dt_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    start_ts = int(dt_obj.timestamp())
    end_ts = int((dt_obj + timedelta(days=1)).timestamp())

    cols_str = ", ".join(CHAIN_COLS)
    query = f"""
        SELECT {cols_str}
        FROM chain_snap
        WHERE ts >= ? AND ts < ?
        ORDER BY ts ASC, strike ASC
    """
    df = pd.read_sql_query(query, conn, params=(start_ts, end_ts))
    if df.empty:
        log.warning("No chain_snap records found for date %s", date_str)
        return None

    df.to_parquet(out_file, engine="pyarrow", index=False)
    file_size_kb = out_file.stat().st_size / 1024.0
    log.info("✔ Archived Chain [%s]: %d rows -> %s (%.1f KB)", date_str, len(df), out_file.name, file_size_kb)
    return out_file


def archive_session_candles(
    conn: sqlite3.Connection,
    date_str: str,
    output_dir: Path = CANDLES_ARCHIVE_DIR,
    force: bool = False
) -> Optional[Path]:
    """Archives candles_1m rows for date_str to Parquet using indexed timestamp ranges."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{date_str}.parquet"

    if out_file.exists() and not force:
        log.info("Candles archive already exists: %s (skipping, use --force to overwrite)", out_file.name)
        return out_file

    dt_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    start_ts = int(dt_obj.timestamp())
    end_ts = int((dt_obj + timedelta(days=1)).timestamp())

    cols_str = ", ".join(CANDLES_COLS)
    query = f"""
        SELECT {cols_str}
        FROM candles_1m
        WHERE ts >= ? AND ts < ?
        ORDER BY ts ASC, sec_id ASC
    """
    df = pd.read_sql_query(query, conn, params=(start_ts, end_ts))
    if df.empty:
        log.warning("No candles_1m records found for date %s", date_str)
        return None

    df.to_parquet(out_file, engine="pyarrow", index=False)
    file_size_kb = out_file.stat().st_size / 1024.0
    log.info("✔ Archived Candles [%s]: %d rows -> %s (%.1f KB)", date_str, len(df), out_file.name, file_size_kb)
    return out_file


def run_archival(
    db_path: Path = DB_DEFAULT,
    target_date: Optional[str] = None,
    archive_all: bool = False,
    force: bool = False
) -> None:
    """Executes archival workflow."""
    if not db_path.exists():
        log.error("Database file not found: %s", db_path)
        sys.exit(1)

    log.info("Opening database read-only: %s", db_path)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        chain_dates = get_available_dates(conn, "chain_snap")
        candles_dates = get_available_dates(conn, "candles_1m")

        log.info("Found %d chain_snap dates (range: %s to %s)", len(chain_dates), chain_dates[0], chain_dates[-1])
        log.info("Found %d candles_1m dates (range: %s to %s)", len(candles_dates), candles_dates[0], candles_dates[-1])

        chain_count = 0
        candles_count = 0

        if archive_all:
            log.info("Backfilling all %d chain sessions and %d candles sessions...", len(chain_dates), len(candles_dates))
            for dt in chain_dates:
                if archive_session_chain(conn, dt, force=force):
                    chain_count += 1
            for dt in candles_dates:
                if archive_session_candles(conn, dt, force=force):
                    candles_count += 1
        elif target_date:
            if archive_session_chain(conn, target_date, force=force):
                chain_count += 1
            if archive_session_candles(conn, target_date, force=force):
                candles_count += 1
        else:
            # Runs after 16:00 IST and writes the previous/closed session
            now_ist = datetime.now(IST)
            today_str = now_ist.strftime("%Y-%m-%d")
            if now_ist.hour >= 16 and today_str in chain_dates:
                session_dt = today_str
            else:
                prev_dates = [d for d in chain_dates if d < today_str]
                session_dt = prev_dates[-1] if prev_dates else chain_dates[-1]
            log.info("Selected session date: %s (Current IST: %s)", session_dt, now_ist.strftime("%H:%M:%S"))
            if archive_session_chain(conn, session_dt, force=force):
                chain_count += 1
            if archive_session_candles(conn, session_dt, force=force):
                candles_count += 1

        log.info("Archival run finished: %d chain files, %d candle files processed.", chain_count, candles_count)

    print("\n" + "="*80)
    print("⚠️  SECURITY & RESILIENCE NOTE FOR HARISH:")
    print("These parquet archives and ict_smc.sqlite3 currently exist on ONE local disk with no second copy.")
    print("Recommended next step: Add automated rsync / s3cmd / cloud-bucket mirror for data/archive/")
    print("="*80 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightly Parquet Archival Utility for NIFTY Chain & Candles")
    parser.add_argument("--db-path", type=str, default=str(DB_DEFAULT), help="Path to ict_smc.sqlite3")
    parser.add_argument("--date", type=str, default=None, help="Specific session date YYYY-MM-DD")
    parser.add_argument("--all", action="store_true", help="Backfill/archive all sessions in DB")
    parser.add_argument("--force", action="store_true", help="Force overwrite existing archive parquet files")

    args = parser.parse_args()
    run_archival(
        db_path=Path(args.db_path),
        target_date=args.date,
        archive_all=args.all,
        force=args.force
    )
