#!/usr/bin/env python3
"""
report_slippage.py — what execution ACTUALLY costs, from recorded fills.

WHY THIS EXISTS
---------------
Every slippage number in this programme so far has been an assumption:

  - backtest_ma_geometry.py and backtest_futures_volume.py both hardcode
    SLIPPAGE_INR_PER_TRADE = 32.50, i.e. 0.5 points on a 65 lot.
  - The "pocket limit-fill" proposal is justified by "turning -Rs32.50 slippage
    into +Rs32.50 price improvement", a Rs65/trade swing — which rests entirely
    on that 32.50 being real.

Meanwhile the live config places an AGGRESSIVE limit 4.0 points through the
market (aggressive_limit_buffer_pts) with up to 3 chase retries, so the
theoretical worst case is 4 points = Rs260/leg, eight times the assumed figure.

The orders table has recorded signal_ltp, limit_price, fill_price, slippage and
chase_retries for every fill. So the number does not need assuming. This reads
it.

This is the same correction already applied twice on this desk: the Rs400
brokerage turned out to be Rs78, and the 0.60/0.50 deltas turned out to be
0.59/0.67. Measure before optimising.

USAGE
  python3 scripts/report_slippage.py
  python3 scripts/report_slippage.py --mode PAPER_ONLY
  python3 scripts/report_slippage.py --since 2026-10-01
  python3 scripts/report_slippage.py --json

Reads the ledger READ-ONLY. Writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "terminal8510" / "ledger.sqlite3"


def load_fills(conn, mode: Optional[str] = None,
               since: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT order_id, created_at, strategy_name, symbol, side, qty, "
           "signal_ltp, limit_price, fill_price, slippage, chase_retries, "
           "brokerage_taxes, mode, order_status, source "
           "FROM orders WHERE order_status = 'FILLED'")
    args: List[Any] = []
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    sql += " ORDER BY created_at ASC"
    try:
        cur = conn.execute(sql, args)
    except sqlite3.OperationalError:
        # `source` was added by a later migration; retry without it.
        cur = conn.execute(sql.replace(", source", ""), args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def realised_slippage_pts(row: Dict[str, Any]) -> Optional[float]:
    """Points paid away from the signal price, positive = worse than signal.

    Prefers the recorded slippage column; falls back to fill - signal, signed by
    side, so a SELL filling below the signal also reads as a cost.
    """
    rec = num(row.get("slippage"))
    if rec is not None:
        return rec
    fill, sig = num(row.get("fill_price")), num(row.get("signal_ltp"))
    if fill is None or sig is None:
        return None
    return (fill - sig) if str(row.get("side", "")).upper() == "BUY" else (sig - fill)


def describe(vals: List[float], label: str, lot: Optional[int]) -> Dict[str, Any]:
    if not vals:
        return {"label": label, "n": 0}
    out = {
        "label": label, "n": len(vals),
        "mean_pts": round(st.mean(vals), 4),
        "median_pts": round(st.median(vals), 4),
        "p90_pts": round(sorted(vals)[int(0.9 * (len(vals) - 1))], 4),
        "worst_pts": round(max(vals), 4),
        "best_pts": round(min(vals), 4),
        "pct_improved": round(100.0 * sum(1 for v in vals if v < 0) / len(vals), 1),
    }
    if lot:
        out["mean_inr"] = round(out["mean_pts"] * lot, 2)
        out["median_inr"] = round(out["median_pts"] * lot, 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", help="PAPER_ONLY | ALGO_LIVE_ONLY | BOTH")
    ap.add_argument("--since", help="only fills at/after this YYYY-MM-DD")
    ap.add_argument("--lot", type=int, default=65, help="lot size for rupee conversion")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    if not LEDGER.exists():
        print("ledger not found: {}".format(LEDGER))
        return 2

    conn = sqlite3.connect("file:{}?mode=ro".format(LEDGER), uri=True)
    try:
        rows = load_fills(conn, a.mode, a.since)
    finally:
        conn.close()

    if not rows:
        print("No FILLED orders recorded{}{}.".format(
            " for mode " + a.mode if a.mode else "",
            " since " + a.since if a.since else ""))
        print("\nNothing to measure yet. Execution cost stays an ASSUMPTION until")
        print("there are real fills, so the pocket-limit case cannot be settled.")
        return 1

    pts = [(r, realised_slippage_pts(r)) for r in rows]
    pts = [(r, p) for r, p in pts if p is not None]
    if not pts:
        print("{} filled orders, but none carry a usable slippage or "
              "signal/fill pair.".format(len(rows)))
        return 1

    allv = [p for _, p in pts]
    buys = [p for r, p in pts if str(r.get("side", "")).upper() == "BUY"]
    sells = [p for r, p in pts if str(r.get("side", "")).upper() == "SELL"]
    by_retry: Dict[int, List[float]] = {}
    for r, p in pts:
        by_retry.setdefault(int(num(r.get("chase_retries"), 0) or 0), []).append(p)

    report = {
        "ledger": str(LEDGER),
        "mode_filter": a.mode, "since": a.since, "lot": a.lot,
        "n_filled_orders": len(rows), "n_measurable": len(pts),
        "first_fill": rows[0].get("created_at"), "last_fill": rows[-1].get("created_at"),
        "overall": describe(allv, "all fills", a.lot),
        "buy_legs": describe(buys, "BUY legs", a.lot),
        "sell_legs": describe(sells, "SELL legs", a.lot),
        "by_chase_retries": {str(k): describe(v, "{} retries".format(k), a.lot)
                             for k, v in sorted(by_retry.items())},
        "assumed_in_backtests_inr": 32.50,
    }
    o = report["overall"]
    modes = {str(r.get("mode") or "") for r in rows}
    report["modes_present"] = sorted(modes)

    # Whether these are real fills is decided by EVIDENCE, not by the mode
    # label. A row tagged BOTH proves nothing: if the broker was not configured
    # the live leg never filled, only the paper one did, and the PAPER_ONLY
    # fill_price override does not apply to BOTH — so the row still carries
    # fill_price == signal_ltp and a slippage of exactly 0.
    #
    # A genuine market fill essentially never lands exactly on the signal price.
    # So rows with slippage exactly 0.0 are treated as non-evidence, and the
    # measurement is only trustworthy if a real share of fills moved at all.
    nonzero = [p for p in allv if abs(p) > 1e-9]
    report["n_nonzero_slippage"] = len(nonzero)
    report["pct_nonzero"] = round(100.0 * len(nonzero) / len(allv), 1) if allv else 0.0
    is_simulated = report["pct_nonzero"] < 50.0
    report["is_simulated"] = is_simulated
    if nonzero:
        report["nonzero_only"] = describe(nonzero, "fills that actually moved", a.lot)
    report["assumption_vs_reality"] = {
        "assumed_inr_per_trade": 32.50,
        "measured_inr_per_leg": o.get("mean_inr"),
        "verdict": (
            "NOT A MEASUREMENT. Only {}% of fills differ from the signal price "
            "at all, so these are simulated: fill_price was taken from "
            "signal_ltp and no order reached a broker. Cannot confirm or refute "
            "the 32.50 assumption, and cannot justify an execution "
            "optimisation.".format(report["pct_nonzero"])
            if is_simulated else
            ("MEASURED COST IS HIGHER than the 32.50 assumed in the backtests"
             if (o.get("mean_inr") or 0) > 32.50 else
             "MEASURED COST IS LOWER than the 32.50 assumed in the backtests")),
    }

    if a.json:
        print(json.dumps(report, indent=2))
        return 0

    print("REALISED EXECUTION SLIPPAGE  (from recorded fills, not assumed)")
    print("=" * 68)
    print("ledger        : {}".format(LEDGER))
    print("filled orders : {}  (measurable: {})".format(len(rows), len(pts)))
    print("range         : {} .. {}".format(report["first_fill"], report["last_fill"]))
    if a.mode:
        print("mode filter   : {}".format(a.mode))
    print()
    hdr = "{:<16}{:>5}{:>11}{:>11}{:>10}{:>11}{:>12}"
    print(hdr.format("group", "n", "mean pts", "med pts", "p90 pts", "mean INR", "% improved"))
    print("-" * 76)
    for key in ("overall", "buy_legs", "sell_legs"):
        d = report[key]
        if d["n"]:
            print(hdr.format(d["label"][:15], d["n"], d["mean_pts"], d["median_pts"],
                             d["p90_pts"], d.get("mean_inr", "-"), d["pct_improved"]))
    print()
    print("BY CHASE RETRIES (how much the chaser costs when it has to re-price)")
    print("-" * 76)
    for k, d in report["by_chase_retries"].items():
        if d["n"]:
            print(hdr.format("retries=" + k, d["n"], d["mean_pts"], d["median_pts"],
                             d["p90_pts"], d.get("mean_inr", "-"), d["pct_improved"]))
    print()
    print("ASSUMPTION VS REALITY")
    print("-" * 76)
    print("  modes present    : {}".format(", ".join(report["modes_present"]) or "-"))
    print("  fills that moved : {} of {}  ({}%)".format(
        report["n_nonzero_slippage"], len(allv), report["pct_nonzero"]))
    print("  backtests assume : Rs32.50 per trade (0.5 pts on lot 65)")
    print("  from this ledger : Rs{} per leg (mean)".format(o.get("mean_inr")))
    if is_simulated:
        print()
        print("  *** NOT A MEASUREMENT ***")
        print("  Only {}% of fills differ from the signal price at all. A real".format(
            report["pct_nonzero"]))
        print("  market fill essentially never lands exactly on the signal, so")
        print("  these are simulated: fill_price was taken from signal_ltp and")
        print("  no order reached a broker. A 'BOTH' mode tag does not change")
        print("  that -- if the broker was unconfigured only the paper leg")
        print("  filled. So this neither confirms nor refutes the Rs32.50")
        print("  assumption, and cannot justify an execution optimisation.")
        if report.get("nonzero_only", {}).get("n"):
            d = report["nonzero_only"]
            print()
            print("  Of the {} that DID move: mean {} pts (Rs{}).".format(
                d["n"], d["mean_pts"], d.get("mean_inr")))
            print("  Too few to conclude anything.")
    else:
        print("  {}".format(report["assumption_vs_reality"]["verdict"]))
    print()
    print("READING THIS FOR THE POCKET-LIMIT DECISION")
    print("-" * 76)
    print("  'mean pts' is the cost of the CURRENT aggressive-limit execution.")
    print("  A passive limit inside the 4/8 EMA pocket can only save that much,")
    print("  and it does NOT convert the cost into profit: it converts it into")
    print("  NON-FILL RISK. The trades that fail to fill are disproportionately")
    print("  the ones that moved away immediately, i.e. the winners, so the")
    print("  saving must be weighed against adverse selection.")
    print("  '% improved' already shows how often fills beat the signal price.")
    print("  If mean pts is near zero, the pocket-limit idea is worth little and")
    print("  the effort belongs elsewhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
