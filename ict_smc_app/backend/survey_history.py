#!/usr/bin/env python3
"""survey_history.py — READ-ONLY survey of what history the desk actually holds.

Before any backtest or threshold tuning can mean anything, we need to know how much data exists and how
complete it is. This answers that from the database itself rather than from a rendered table — the OcLab
tape view filters rows by default (filterMode "actionable"), so an exported table says nothing about
whether a snapshot was recorded.

Opens the sqlite read-only and writes nothing. Safe to run while the desk is live.

    python3 ict_smc_app/backend/survey_history.py
    python3 ict_smc_app/backend/survey_history.py --db /path/to/ict_smc.sqlite3 --gap-min 5

Reports, per session date:
  * how many chain snapshots were recorded, across which expiries
  * first/last snapshot and the median spacing between them
  * every gap longer than --gap-min, because those are the holes that make an interval delta cover far
    more wall-clock than the column implies
  * candle and alert coverage, so you can see which sessions are replayable at all
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DB = Path(__file__).resolve().parent / "data" / "ict_smc.sqlite3"


def ist(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), IST)


def hhmm(ts: int) -> str:
    return ist(ts).strftime("%H:%M")


def connect_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}\n(pass --db, or run this on the machine that holds the desk's data)")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_exists(cx: sqlite3.Connection, name: str) -> bool:
    row = cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def survey_snapshots(cx: sqlite3.Connection, gap_min: int) -> None:
    print("\n=== CHAIN SNAPSHOT COVERAGE ===")
    if not table_exists(cx, "chain_snap"):
        print("  chain_snap table not present — nothing recorded")
        return

    rows = cx.execute("SELECT DISTINCT expiry, ts FROM chain_snap ORDER BY ts").fetchall()
    if not rows:
        print("  chain_snap is empty — no session can be replayed")
        return

    by_day: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for expiry, ts in rows:
        by_day[ist(ts).strftime("%Y-%m-%d")].append((expiry, int(ts)))

    print(f"  {len(by_day)} session(s), {len(rows)} snapshot rows (expiry x timestamp)\n")
    print(f"  {'date':<12}{'exp':>4}{'snaps':>8}{'first':>8}{'last':>8}{'median':>9}{'gaps':>6}{'worst':>8}")
    print(f"  {'-' * 12}{'-' * 4:>4} {'-' * 7:>7} {'-' * 7:>7} {'-' * 7:>7} {'-' * 8:>8} {'-' * 5:>5} {'-' * 7:>7}")

    grand_gaps: list[tuple[str, int, int, int]] = []
    for day in sorted(by_day):
        entries = by_day[day]
        expiries = sorted({e for e, _ in entries})
        # Collapse to the distinct instants a snapshot exists at, across all expiries: that is what the
        # matrix buckets on, and what a hole in the series actually means.
        stamps = sorted({t for _, t in entries})
        deltas = [b - a for a, b in zip(stamps, stamps[1:])]
        med = int(statistics.median(deltas)) if deltas else 0
        gaps = [(a, b) for a, b in zip(stamps, stamps[1:]) if (b - a) >= gap_min * 60]
        worst = max((b - a for a, b in gaps), default=0)
        for a, b in gaps:
            grand_gaps.append((day, a, b, b - a))
        print(f"  {day:<12}{len(expiries):>4}{len(stamps):>8}{hhmm(stamps[0]):>8}{hhmm(stamps[-1]):>8}"
              f"{med:>8}s{len(gaps):>6}{str(worst // 60) + 'm':>8}")

    if grand_gaps:
        print(f"\n  --- every gap >= {gap_min}m (these are the rows whose interval delta is not an interval) ---")
        for day, a, b, span in sorted(grand_gaps, key=lambda g: -g[3])[:40]:
            print(f"  {day}  {hhmm(a)} -> {hhmm(b)}   {span // 60:>3}m {span % 60:02d}s")
        if len(grand_gaps) > 40:
            print(f"  ... and {len(grand_gaps) - 40} more")
    else:
        print(f"\n  no gaps >= {gap_min}m — the snapshot series is continuous")


def survey_candles(cx: sqlite3.Connection) -> None:
    print("\n=== CANDLE COVERAGE (needed to score any setup's SL/TP) ===")
    if not table_exists(cx, "candles_1m"):
        print("  candles_1m table not present")
        return
    rows = cx.execute(
        "SELECT sec_id, COUNT(*), MIN(ts), MAX(ts) FROM candles_1m GROUP BY sec_id ORDER BY COUNT(*) DESC"
    ).fetchall()
    if not rows:
        print("  candles_1m is empty — outcomes cannot be scored")
        return
    print(f"  {'sec_id':<14}{'bars':>9}  {'from':<18}{'to':<18}")
    print(f"  {'-' * 13:<14}{'-' * 8:>9}  {'-' * 17:<18}{'-' * 17:<18}")
    for sec_id, n, lo, hi in rows[:20]:
        print(f"  {str(sec_id):<14}{n:>9}  {ist(lo).strftime('%Y-%m-%d %H:%M'):<17}"
              f"{ist(hi).strftime('%Y-%m-%d %H:%M'):<17}")


def survey_alerts(cx: sqlite3.Connection) -> None:
    print("\n=== RECORDED ALERTS (the only stored record of what actually fired) ===")
    if not table_exists(cx, "alerts"):
        print("  alerts table not present")
        return
    total = cx.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    if not total:
        print("  alerts is empty — note that OcLab setups are computed in the browser and are NOT stored,")
        print("  so SNIPER BREAKDOWN / PULLBACK SELL / DIP BUY leave no trace here to score against.")
        return
    print(f"  {total} alert(s)")
    by_day = cx.execute(
        "SELECT DATE(ts + 19800, 'unixepoch') AS d, COUNT(*) FROM alerts GROUP BY d ORDER BY d"
    ).fetchall()
    for d, n in by_day[-15:]:
        print(f"    {d}  {n:>5}")
    print("\n  kinds:")
    for kind, n in cx.execute("SELECT kind, COUNT(*) FROM alerts GROUP BY kind ORDER BY COUNT(*) DESC").fetchall():
        print(f"    {str(kind):<28}{n:>6}")


def survey_usage(cx: sqlite3.Connection) -> None:
    print("\n=== DHAN CALL USAGE (kv: dhan_calls) ===")
    if not table_exists(cx, "kv"):
        print("  kv table not present")
        return
    row = cx.execute("SELECT v FROM kv WHERE k='dhan_calls'").fetchone()
    if not row:
        print("  no dhan_calls entry")
        return
    print(f"  {row[0]}")
    print("  (rate_limited counts ATTEMPTS, not polls — before the retry-ceiling fix each throttled poll")
    print("   burned up to four of them)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only survey of the desk's stored history.")
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("ICT_DB", DEFAULT_DB)),
                    help=f"path to ict_smc.sqlite3 (default: {DEFAULT_DB})")
    ap.add_argument("--gap-min", type=int, default=5, help="report snapshot gaps at least this many minutes")
    args = ap.parse_args()

    print(f"database: {args.db}")
    cx = connect_ro(args.db)
    try:
        survey_snapshots(cx, args.gap_min)
        survey_candles(cx)
        survey_alerts(cx)
        survey_usage(cx)
    finally:
        cx.close()

    print("\n=== WHAT THIS DOES NOT TELL YOU ===")
    print("  The OcLab setups (conviction %, Scissor, Synergy, SNIPER BREAKDOWN, PULLBACK SELL, DIP BUY)")
    print("  are computed in the browser in OcLab.tsx and never persisted. Nothing in this database records")
    print("  which setups fired or at what price, so their hit rate cannot be measured from stored data")
    print("  alone — that logic has to be made replayable first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
