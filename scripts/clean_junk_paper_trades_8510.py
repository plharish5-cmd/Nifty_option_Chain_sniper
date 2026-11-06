#!/usr/bin/env python3
"""scripts/clean_junk_paper_trades_8510.py — remove trades the desk never took.

WHY THIS IS NEEDED
------------------
The 06 Nov replay reported 180 trades and Rs85,321.94 net P&L. Those are not
real results. Three things went wrong, and the first one caused the rest:

1. SEVERAL TESTS WRITE TO THE LIVE CONFIG. They call update_dhan_config and the
   testing-override endpoints against the real analyst/terminal8510_config.json
   instead of a tmp_path copy. A clean suite run flips
   allow_off_hours_paper_testing from false to true AND rewrites
   whitelisted_ip to 127.0.0.1, and leaves both that way.

2. THAT FLAG IS THE OFF-HOURS GATE. terminal8510_backend.py line ~5580 only
   permits execution outside market hours when it is true. Left true, the
   scanner placed paper trades at 02:49, 03:50, 04:10 and 16:56 IST -- times
   when NIFTY is not trading and no quote can be real.

3. THE SAME FILL WAS RECORDED MANY TIMES. '23700 CE 195.xx -> 234.60' at
   +Rs2,482 appears dozens of times within the same second, which is most of
   the Rs85k. One fabricated trade counted over and over.

'Manual - No Strategy' is not evidence that anyone traded manually: it is the
UI's fallback label (Algorithmic_Trading_Workspace_8510.html) shown when a row
carries neither strategy_setup nor strategy_name.

WHAT THIS DELETES, and only this
--------------------------------
  A. OFF_HOURS   -- entry_time outside 09:15-15:30 IST. NIFTY was not trading.
  B. DUPLICATE   -- same symbol, same entry second, same entry premium, keeping
                    the earliest row of each group.
  C. ZERO_DURATION -- opened and closed in the same second with zero gross P&L,
                    which is not a trade.

It does NOT touch anything inside market hours that is not a duplicate, and it
never deletes a position still OPEN.

SAFETY
------
Dry run by default: it prints what it would remove and changes nothing. It only
writes with --apply, and --apply takes a timestamped backup of the ledger first.

USAGE
  python3 scripts/clean_junk_paper_trades_8510.py                 # dry run
  python3 scripts/clean_junk_paper_trades_8510.py --date 2026-11-06
  python3 scripts/clean_junk_paper_trades_8510.py --apply
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "terminal8510" / "ledger.sqlite3"

SESSION_OPEN = "09:15"
SESSION_CLOSE = "15:30"


TIME_RE = re.compile(r"(?:^|[T ])([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?")


def hhmm(stamp: str) -> str:
    """Extract HH:MM from any timestamp shape in this ledger.

    THE BUG THIS REPLACES, which made the first run worthless: the old version
    did `if "T" in s: s.split("T", 1)[1]` to detect ISO format. But this desk
    writes positions.entry_time as "%Y-%m-%d %H:%M:%S IST" -- and "IST"
    CONTAINS A "T". So every timestamp was split on that T, yielding an empty
    string, so every row looked unreadable, so the off-hours check was skipped
    for all of them and the 02:49 trades were reported as survivors.

    A regex that matches the time itself handles every shape here:
        2026-11-06 02:49:17 IST
        2026-11-06T02:49:17+05:30
        2026-11-06 02:49
    """
    m = TIME_RE.search(str(stamp or ""))
    return "{}:{}".format(m.group(1), m.group(2)) if m else ""


def daypart(stamp: str) -> str:
    s = str(stamp or "")
    return s.split("T", 1)[0] if "T" in s else s.split(" ", 1)[0]


def best_time(r: Dict[str, Any], orders_by_pos: Dict[str, str]) -> str:
    """HH:MM for a position, from whichever field actually carries it.

    BUG THIS FIXES. The first version read only positions.entry_time and, when
    that was empty, `if t and ...` silently skipped the off-hours check. On the
    06 Nov ledger that meant ZERO off-hours rows were detected and the 02:49
    trades were reported as survivors -- false comfort, which is worse than no
    tool. positions.entry_time is absent on those rows (the UI reads a separate
    `entry_ist` field, and the HTML even carries the string "entry_time is
    absent."), so the order's created_at is used as the fallback and an
    unreadable timestamp is now REPORTED rather than ignored.
    """
    for key in ("entry_time", "entry_ist"):
        t = hhmm(r.get(key))
        if t:
            return t
    oid = str(r.get("position_id") or "")
    return hhmm(orders_by_pos.get(oid, "")) or hhmm(r.get("exit_time"))


def classify(rows: List[Dict[str, Any]],
             orders_by_pos: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen: Dict[tuple, Dict[str, Any]] = {}

    for r in rows:
        if str(r.get("status") or "").upper() == "OPEN":
            continue                      # never delete a live position
        t = best_time(r, orders_by_pos)
        r["_resolved_time"] = t
        if not t:
            # Fail LOUD, not closed. Reported, never auto-deleted.
            out["UNREADABLE_TIME"].append(r)
            continue
        if not (SESSION_OPEN <= t <= SESSION_CLOSE):
            out["OFF_HOURS"].append(r)
            continue

        # Duplicate: identical contract, same entry second, same premium.
        key = (str(r.get("symbol")), str(r.get("entry_time")),
               round(float(r.get("entry_price") or 0.0), 2))
        if key in seen:
            out["DUPLICATE"].append(r)
            continue
        seen[key] = r

        try:
            same_second = str(r.get("entry_time")) == str(r.get("exit_time"))
            zero_pnl = abs(float(r.get("gross_pnl") or 0.0)) < 1e-9
        except (TypeError, ValueError):
            same_second = zero_pnl = False
        if same_second and zero_pnl:
            out["ZERO_DURATION"].append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--date", help="limit to one YYYY-MM-DD entry date")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (backs the ledger up first)")
    a = ap.parse_args()

    led = Path(a.ledger)
    if not led.exists():
        print("ledger not found: {}".format(led))
        print("Run this on the desk, or pass --ledger <path>.")
        return 2

    conn = sqlite3.connect(led)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM positions"
    args: List[Any] = []
    if a.date:
        sql += " WHERE substr(replace(entry_time,'T',' '),1,10)=?"
        args.append(a.date)
    sql += " ORDER BY entry_time ASC"
    rows = [dict(r) for r in conn.execute(sql, args)]

    # positions.entry_time is empty on the affected rows, so fall back to the
    # order that opened the position.
    orders_by_pos: Dict[str, str] = {}
    try:
        for o in conn.execute("SELECT order_id, created_at FROM orders"):
            oid = str(o["order_id"] or "")
            if oid.startswith("X_"):
                orders_by_pos[oid[2:]] = str(o["created_at"] or "")
            else:
                orders_by_pos.setdefault(oid, str(o["created_at"] or ""))
    except sqlite3.Error:
        pass

    groups = classify(rows, orders_by_pos)
    # UNREADABLE_TIME is reported, never deleted: without a timestamp there
    # is no evidence the trade was out of session.
    doomed = {r["position_id"]: k for k, v in groups.items()
              for r in v if k != "UNREADABLE_TIME"}

    print("JUNK PAPER TRADE CLEANUP{}".format(
        " — " + a.date if a.date else ""))
    print("=" * 76)
    print("ledger        : {}".format(led))
    print("positions seen : {}".format(len(rows)))
    print("open (kept)    : {}".format(
        sum(1 for r in rows if str(r.get("status") or "").upper() == "OPEN")))
    print()

    net_removed = 0.0
    for kind in ("OFF_HOURS", "DUPLICATE", "ZERO_DURATION", "UNREADABLE_TIME"):
        v = groups.get(kind) or []
        if not v:
            continue
        s = sum(float(x.get("net_pnl") or 0.0) for x in v)
        net_removed += s
        print("{:<15} {:>5} rows   net Rs{:>12,.2f}".format(kind, len(v), s))
        times = sorted({x.get("_resolved_time") or "??:??" for x in v})
        print("                entry times: {}{}".format(
            ", ".join(times[:10]), " …" if len(times) > 10 else ""))
        worst = sorted(v, key=lambda x: -abs(float(x.get("net_pnl") or 0.0)))[:3]
        for w in worst:
            print("                e.g. {} {} @ {} -> {}  net Rs{}".format(
                w.get("_resolved_time") or "??:??", w.get("symbol"),
                w.get("entry_price"), w.get("exit_price"), w.get("net_pnl")))
    print()

    kept = [r for r in rows if r["position_id"] not in doomed
            and str(r.get("status") or "").upper() != "OPEN"]
    kept_net = sum(float(r.get("net_pnl") or 0.0) for r in kept)
    total_net = sum(float(r.get("net_pnl") or 0.0) for r in rows
                    if str(r.get("status") or "").upper() != "OPEN")
    print("BEFORE : {} closed rows, net Rs{:,.2f}".format(
        len(rows) - sum(1 for r in rows if str(r.get("status") or "").upper() == "OPEN"),
        total_net))
    print("REMOVE : {} rows, net Rs{:,.2f}".format(len(doomed), net_removed))
    print("AFTER  : {} closed rows, net Rs{:,.2f}".format(len(kept), kept_net))
    print()
    if kept:
        print("What survives (real market-hours trades):")
        for r in kept[:15]:
            print("   {} {:<32} {:<26} net Rs{}".format(
                r.get("_resolved_time") or "??:??", str(r.get("symbol"))[:32],
                str(r.get("exit_reason") or r.get("status"))[:26], r.get("net_pnl")))
        if len(kept) > 15:
            print("   … and {} more".format(len(kept) - 15))

    if groups.get("UNREADABLE_TIME"):
        print()
        print("*** {} rows carry NO READABLE TIMESTAMP. They are NOT deleted, ***"
              .format(len(groups["UNREADABLE_TIME"])))
        print("*** because without a time there is no evidence they were out of  ***")
        print("*** session. Inspect them before trusting any net P&L figure.     ***")

    if not a.apply:
        print()
        print("DRY RUN — nothing was changed. Re-run with --apply to delete.")
        conn.close()
        return 0

    if not doomed:
        print("Nothing to delete.")
        conn.close()
        return 0

    backup = led.with_name("{}.backup-{}".format(
        led.name, datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(led, backup)
    print()
    print("backup written: {}".format(backup))

    ids = list(doomed)
    cur = conn.cursor()
    # Orders are keyed to the position by its id in the exit leg (X_<pos_id>).
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        qs = ",".join("?" * len(chunk))
        cur.execute("DELETE FROM positions WHERE position_id IN ({})".format(qs), chunk)
        cur.execute("DELETE FROM orders WHERE order_id IN ({})".format(
            ",".join("?" * len(chunk))), ["X_" + c for c in chunk])
    conn.commit()

    left = conn.execute("SELECT COUNT(*), COALESCE(SUM(net_pnl),0.0) FROM positions "
                        "WHERE status NOT LIKE 'OPEN%'").fetchone()
    print("deleted {} positions".format(len(ids)))
    print("ledger now: {} closed positions, net Rs{:,.2f}".format(left[0], left[1]))
    print()
    print("The paper wallet is NOT recomputed here. Reset it from the UI, or")
    print("call reset_paper_ledger(), which recomputes margin from genuinely")
    print("OPEN positions rather than zeroing it.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
