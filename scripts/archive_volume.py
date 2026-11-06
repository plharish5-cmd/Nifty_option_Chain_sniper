#!/usr/bin/env python3
"""
archive_volume.py — permanent per-date archive of futures volume and spot candles.

WHY
---
candles_1m already keeps 400 days, but it is ONE SQLite file on ONE disk with no
copy. Futures volume cannot be rebuilt if it is lost: Dhan purges expired
contracts from its scrip master, so there is no way to re-fetch a past month's
futures series. Every session recorded is therefore irreplaceable.

This writes one file per trading date. The app NEVER reads these back — they are
backup only, so they cannot drift from the database and become a second source
of truth.

USAGE
  python3 scripts/archive_volume.py                 # archive everything not yet archived
  python3 scripts/archive_volume.py --date 2026-11-05
  python3 scripts/archive_volume.py --force         # rewrite existing files
  python3 scripts/archive_volume.py --report        # coverage only, write nothing

Writes to data/archive/volume/YYYY-MM-DD.csv.gz (parquet when pyarrow is present).
Idempotent: an existing file is skipped unless --force.
"""
from __future__ import annotations

import argparse
import gzip
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ict_smc_app" / "backend"))

DB = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
OUT = ROOT / "data" / "archive" / "volume"
IST = "'+5 hours','+30 minutes'"
SEC_IDS = ("FUT", "FUT2", "13")
COLS = ["sec_id", "ts", "ist", "o", "h", "l", "c", "v"]


def sessions(conn) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT date(ts,'unixepoch',{tz}) d FROM candles_1m "
        "WHERE sec_id IN ({q}) ORDER BY d".format(
            tz=IST, q=",".join("?" * len(SEC_IDS))), SEC_IDS).fetchall()
    return [r[0] for r in rows if r[0]]


def rows_for(conn, day: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT sec_id, ts, datetime(ts,'unixepoch',{tz}) ist, o,h,l,c,v "
        "FROM candles_1m WHERE sec_id IN ({q}) "
        "AND date(ts,'unixepoch',{tz}) = ? ORDER BY sec_id, ts".format(
            tz=IST, q=",".join("?" * len(SEC_IDS))), list(SEC_IDS) + [day]).fetchall()
    return [dict(zip(COLS, r)) for r in cur]


def write_day(day: str, rows: List[Dict[str, Any]]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd  # noqa
        import pyarrow  # noqa
        path = OUT / "{}.parquet".format(day)
        pd.DataFrame(rows, columns=COLS).to_parquet(path, index=False)
        return path
    except ImportError:
        path = OUT / "{}.csv.gz".format(day)
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        return path


def existing(day: str) -> bool:
    return (OUT / "{}.parquet".format(day)).exists() or \
           (OUT / "{}.csv.gz".format(day)).exists()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="archive only this YYYY-MM-DD")
    ap.add_argument("--force", action="store_true", help="rewrite existing files")
    ap.add_argument("--report", action="store_true", help="coverage only, write nothing")
    a = ap.parse_args()

    if not DB.exists():
        print("database not found: {}".format(DB))
        return 2

    conn = sqlite3.connect("file:{}?mode=ro".format(DB), uri=True)
    try:
        from app.engine import volume_store as vs
        days = [a.date] if a.date else sessions(conn)
        if not days:
            print("no sessions in candles_1m")
            return 1

        fut = vs.load_rows(conn, "FUT")
        cov = vs.coverage_from_rows(fut)
        print("FUTURES VOLUME COVERAGE")
        print("  sessions      : {}".format(cov["n_sessions"]))
        print("  range         : {} .. {}".format(cov["first_session"], cov["last_session"]))
        print("  mean coverage : {}% of a 375-minute session".format(cov["mean_coverage_pct"]))
        if cov["partial_sessions"]:
            p = cov["partial_sessions"]
            print("  below 80%     : {} session(s){}".format(
                len(p), "  e.g. " + ", ".join(p[-5:]) if p else ""))
        print()

        if a.report:
            return 0

        written = skipped = 0
        total_rows = 0
        for day in days:
            if existing(day) and not a.force:
                skipped += 1
                continue
            rows = rows_for(conn, day)
            if not rows:
                continue
            path = write_day(day, rows)
            written += 1
            total_rows += len(rows)
            print("  wrote {}  ({} rows)".format(path.relative_to(ROOT), len(rows)))

        print()
        print("archived {} session(s), {} rows; {} already present".format(
            written, total_rows, skipped))
        print("archive dir: {}".format(OUT))
        print()
        print("These files are BACKUP ONLY. Nothing in the app reads them, so they")
        print("cannot drift from the database. Futures volume is irreplaceable:")
        print("Dhan purges expired contracts, so a lost month cannot be re-fetched.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
