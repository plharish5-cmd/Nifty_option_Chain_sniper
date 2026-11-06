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

# The two figures in conflict across this desk. Neither is measured; the
# dry-run ledger exists to decide between them.
CONFIG_ASSUMED_INR = 8.13      # paper_execution.simulated_slippage_pts mean 0.125 pts x 65
BACKTEST_ASSUMED_INR = 32.50   # SLIPPAGE_INR_PER_TRADE in the backtest scripts (0.5 pts)


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


def load_dry_run(conn, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """v1.9.4b dry-run rows: the real touch at signal time, no order sent.

    These exist because the question this script was written to answer is
    otherwise unanswerable. Under PAPER_ONLY, fill_price is derived from
    signal_ltp, so the recorded slippage is an artefact of the simulation, not a
    measurement -- which is why the main report prints NOT A MEASUREMENT. A
    dry-run row carries the actual bid and ask, so the half-spread in it is a
    real cost that a real order would have paid.
    """
    sql = ("SELECT ts, strategy, symbol, side, qty, signal_ltp, bid, ask, "
           "spread_pts, intended_limit_price FROM dry_run_orders")
    args: List[Any] = []
    if since:
        sql += " WHERE ts >= ?"
        args.append(since)
    sql += " ORDER BY ts ASC"
    try:
        cur = conn.execute(sql, args)
    except sqlite3.OperationalError:
        return []          # table absent on a ledger that never armed dry-run
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def half_spreads(rows: List[Dict[str, Any]]) -> List[float]:
    """Half the bid-ask spread: what crossing the touch costs one way.

    A round trip crosses twice, so the per-leg cost is half the spread and the
    per-trade cost is the full spread. Reported separately below so neither is
    confused for the other.
    """
    out = []
    for r in rows:
        bid, ask = num(r.get("bid")), num(r.get("ask"))
        if bid is None or ask is None or ask <= 0 or bid <= 0 or ask < bid:
            continue
        out.append((ask - bid) / 2.0)
    return out


def dry_run_report(rows: List[Dict[str, Any]], lot: int) -> Dict[str, Any]:
    hs = half_spreads(rows)
    rep: Dict[str, Any] = {
        "n_rows": len(rows),
        "n_with_depth": len(hs),
        "n_missing_depth": len(rows) - len(hs),
    }
    if not hs:
        rep["verdict"] = (
            "NO MEASUREMENT YET. {} dry-run rows, none carrying a usable "
            "bid/ask. Both the Rs8.13 config assumption and the Rs32.50 "
            "backtest assumption remain unverified.".format(len(rows)))
        return rep
    srt = sorted(hs)
    med = st.median(srt)
    p90 = srt[int(0.9 * (len(srt) - 1))]
    rep.update({
        "median_half_spread_pts": round(med, 4),
        "p90_half_spread_pts": round(p90, 4),
        "median_half_spread_inr_per_leg": round(med * lot, 2),
        "p90_half_spread_inr_per_leg": round(p90 * lot, 2),
        "median_full_spread_inr_round_trip": round(2 * med * lot, 2),
        "worst_half_spread_pts": round(max(srt), 4),
        "best_half_spread_pts": round(min(srt), 4),
    })
    # UNIT DISCIPLINE. The two assumptions are quoted in DIFFERENT units and
    # comparing them directly is the same apples-to-oranges error this script
    # was written to stop. simulated_slippage_pts is applied per LEG; the
    # backtests' SLIPPAGE_INR_PER_TRADE is per ROUND TRIP. Everything below is
    # normalised to a round trip, which is what a trade actually costs.
    measured_leg = med * lot
    measured_trip = 2.0 * measured_leg
    cfg_trip = 2.0 * CONFIG_ASSUMED_INR
    d_cfg = abs(measured_trip - cfg_trip)
    d_bt = abs(measured_trip - BACKTEST_ASSUMED_INR)
    closer = ("config ({:.2f}/leg = Rs{:.2f}/trip)".format(CONFIG_ASSUMED_INR, cfg_trip)
              if d_cfg < d_bt else
              "backtest (Rs{:.2f}/trip)".format(BACKTEST_ASSUMED_INR))
    rep.update({
        "config_assumed_inr_per_leg": CONFIG_ASSUMED_INR,
        "config_assumed_inr_per_round_trip": round(cfg_trip, 2),
        "backtest_assumed_inr_per_round_trip": BACKTEST_ASSUMED_INR,
        "measured_inr_per_leg": round(measured_leg, 2),
        "measured_inr_per_round_trip": round(measured_trip, 2),
        "closer_to_reality": closer,
        "distance_from_config_inr": round(d_cfg, 2),
        "distance_from_backtest_inr": round(d_bt, 2),
        "ratio_measured_over_config": round(measured_trip / cfg_trip, 2) if cfg_trip else None,
        "ratio_measured_over_backtest": (round(measured_trip / BACKTEST_ASSUMED_INR, 2)
                                         if BACKTEST_ASSUMED_INR else None),
        "verdict": (
            "MEASURED median half-spread Rs{:.2f} per leg = Rs{:.2f} per round "
            "trip. Normalised to a round trip, config assumes Rs{:.2f} and the "
            "backtests assume Rs{:.2f}; the {} figure is closer, off by Rs{:.2f} "
            "against Rs{:.2f}. NOTE the units: the two assumptions were never 4x "
            "apart, they were quoted per-leg vs per-trade, which is a 2x "
            "difference. This is a SPREAD, not a realised fill -- the cost of "
            "crossing the touch, which an aggressive limit pays and a passive "
            "limit converts into non-fill risk. No threshold is changed here."
            .format(measured_leg, measured_trip, cfg_trip, BACKTEST_ASSUMED_INR,
                    "config" if d_cfg < d_bt else "backtest",
                    min(d_cfg, d_bt), max(d_cfg, d_bt))),
    })
    if len(hs) < 30:
        rep["caution"] = ("Only {} rows with depth. The dry-run gate asks for 30 "
                          "before this is treated as settled.".format(len(hs)))
    return rep


def print_dry_run(rep: Dict[str, Any]) -> None:
    print()
    print("DRY-RUN REAL SPREAD  (v1.9.4b — measured, no order sent)")
    print("-" * 76)
    print("  rows                 : {} ({} with depth, {} missing)".format(
        rep["n_rows"], rep["n_with_depth"], rep["n_missing_depth"]))
    if not rep.get("median_half_spread_pts"):
        print("  {}".format(rep["verdict"]))
        return
    print("  median half-spread   : {} pts  = Rs{} per leg".format(
        rep["median_half_spread_pts"], rep["median_half_spread_inr_per_leg"]))
    print("  p90 half-spread      : {} pts  = Rs{} per leg".format(
        rep["p90_half_spread_pts"], rep["p90_half_spread_inr_per_leg"]))
    print("  full spread (2 legs) : Rs{} per round trip".format(
        rep["median_full_spread_inr_round_trip"]))
    print()
    print("  NORMALISED TO A ROUND TRIP (the two assumptions use different units)")
    print("  measured             : Rs{} per leg = Rs{} per round trip".format(
        rep["measured_inr_per_leg"], rep["measured_inr_per_round_trip"]))
    print("  config assumes       : Rs{} per leg = Rs{} per trip  (off by Rs{}, {}x)".format(
        rep["config_assumed_inr_per_leg"], rep["config_assumed_inr_per_round_trip"],
        rep["distance_from_config_inr"], rep["ratio_measured_over_config"]))
    print("  backtests assume     : Rs{} per trip               (off by Rs{}, {}x)".format(
        rep["backtest_assumed_inr_per_round_trip"],
        rep["distance_from_backtest_inr"], rep["ratio_measured_over_backtest"]))
    print("  CLOSER TO REALITY    : {}".format(rep["closer_to_reality"]))
    print()
    print("  {}".format(rep["verdict"]))
    if rep.get("caution"):
        print("  CAUTION: {}".format(rep["caution"]))


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
        dry_rows = load_dry_run(conn, a.since)
    finally:
        conn.close()

    dry = dry_run_report(dry_rows, a.lot)

    if not rows:
        print("No FILLED orders recorded{}{}.".format(
            " for mode " + a.mode if a.mode else "",
            " since " + a.since if a.since else ""))
        print("\nNo fills means no realised slippage to read. That is expected while")
        print("the desk is PAPER_ONLY, and it is exactly why the dry-run ledger")
        print("exists: it measures the spread a real order would have crossed.")
        if a.json:
            print(json.dumps({"n_filled_orders": 0, "dry_run": dry}, indent=2))
        else:
            print_dry_run(dry)
        # Dry-run rows with depth ARE a measurement, so this is a success.
        return 0 if dry.get("median_half_spread_pts") else 1

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

    report["dry_run"] = dry

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
    print_dry_run(dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
