"""
backtest_war_setup.py — Backtest the WAR SETUP (30-Min First-Candle FII Strategy).

Usage:
    python backtest_war_setup.py                # Runs full test on all historical sessions
    python backtest_war_setup.py --detailed     # Prints individual trade breakdown
    python backtest_war_setup.py --export       # Exports trades to data/war_setup_trades.csv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from war_setup import WARSignal, scan_war_signals


@dataclass
class WARBacktestTrade:
    date: str
    scenario: str
    direction: str
    entry_time: str
    entry_price: float
    gap_pts: float
    h30: float
    l30: float
    target_60: float
    target_120: float
    target_160: float
    max_favourable: float = 0.0
    max_adverse: float = 0.0
    hit_60: bool = False
    hit_120: bool = False
    hit_160: bool = False
    pts_captured_60: float = 0.0      # Exit at 60 or EOD
    pts_captured_120: float = 0.0     # Exit at 120 or EOD
    pts_captured_trail: float = 0.0   # 50% booked at 60, 50% trailed
    opt_pnl_est: float = 0.0          # Option premium PnL capped at -80 pts
    pnl_rupees_60: float = 0.0


def simulate_trade(df: pd.DataFrame, sig: WARSignal) -> WARBacktestTrade:
    entry = sig.entry_price
    is_ce = sig.direction == "BUY_CE"

    # Find the starting index for simulation
    ts_series = pd.to_datetime(df["timestamp"])
    matches = df.index[ts_series == sig.timestamp].tolist()
    start_i = matches[0] if matches else 6

    max_fav = 0.0
    max_adv = 0.0
    hit_60 = False
    hit_120 = False
    hit_160 = False

    for i in range(start_i + 1, len(df)):
        row = df.iloc[i]
        h = float(row["high"])
        l = float(row["low"])

        fav = (h - entry) if is_ce else (entry - l)
        adv = (entry - l) if is_ce else (h - entry)
        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        if fav >= 60.0:
            hit_60 = True
        if fav >= 120.0:
            hit_120 = True
        if fav >= 160.0:
            hit_160 = True

    eod_c = float(df.iloc[-1]["close"])
    eod_pnl = (eod_c - entry) if is_ce else (entry - eod_c)

    pts_60 = 60.0 if hit_60 else eod_pnl
    pts_120 = 120.0 if hit_120 else eod_pnl

    if hit_120:
        pts_trail = 0.5 * 60.0 + 0.5 * 120.0  # 90 pts
    elif hit_60:
        pts_trail = 0.5 * 60.0  # 30 pts
    else:
        pts_trail = eod_pnl

    # Option model: Bought at ~₹80. Max loss is -80 pts. Target 60 yields ~+35 pts.
    if hit_60:
        opt_pnl = 35.0
    elif max_adv >= 120.0:
        opt_pnl = -80.0
    else:
        opt_pnl = max(-80.0, eod_pnl * 0.45)

    return WARBacktestTrade(
        date=sig.date_str,
        scenario=sig.scenario,
        direction=sig.direction,
        entry_time=f"{sig.timestamp:%Y-%m-%d %H:%M:%S}",
        entry_price=sig.entry_price,
        gap_pts=sig.gap_pts,
        h30=sig.h30,
        l30=sig.l30,
        target_60=sig.target_60,
        target_120=sig.target_120,
        target_160=sig.target_160,
        max_favourable=max_fav,
        max_adverse=max_adv,
        hit_60=hit_60,
        hit_120=hit_120,
        hit_160=hit_160,
        pts_captured_60=pts_60,
        pts_captured_120=pts_120,
        pts_captured_trail=pts_trail,
        opt_pnl_est=opt_pnl,
        pnl_rupees_60=pts_60 * config.LOT_SIZE,
    )


def run_war_backtest(export: bool = False, detailed: bool = False):
    folder = config.DATA_DIR / "candles_5m"
    files = sorted(folder.glob("*.csv"))
    days: Dict[str, pd.DataFrame] = {}

    for f in files:
        d = pd.read_csv(f)
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d = d.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        if len(d) >= 10:
            days[f.stem] = d

    dates = sorted(days.keys())
    all_trades: List[WARBacktestTrade] = []

    for i in range(1, len(dates)):
        prev_df = days[dates[i - 1]]
        cur_df = days[dates[i]]
        cur_date = dates[i]
        pdc = float(prev_df.iloc[-1]["close"])

        signals = scan_war_signals(cur_df, pdc, cur_date)
        for sig in signals:
            trade = simulate_trade(cur_df, sig)
            all_trades.append(trade)

    print("=" * 80)
    print(f"       WAR SETUP — 30-MIN FII STRATEGY BACKTEST ({len(dates)-1} SESSIONS)")
    print("=" * 80)
    print(f"Total Trades Generated: {len(all_trades)}")

    if not all_trades:
        print("No trades triggered.")
        return

    tdf = pd.DataFrame([t.__dict__ for t in all_trades])

    w60 = tdf[tdf["hit_60"]]
    w120 = tdf[tdf["hit_120"]]
    w160 = tdf[tdf["hit_160"]]

    print(f"\nTarget Hit Rates:")
    print(f"  Target +60 pts Hit:  {len(w60)/len(tdf)*100:>5.1f}% ({len(w60)} / {len(tdf)})")
    print(f"  Target +120 pts Hit: {len(w120)/len(tdf)*100:>5.1f}% ({len(w120)} / {len(tdf)})")
    print(f"  Target +160 pts Hit: {len(w160)/len(tdf)*100:>5.1f}% ({len(w160)} / {len(tdf)})")

    print("\n" + "-" * 80)
    print("PERFORMANCE BY SCENARIO (+60 Points Target Model)")
    print("-" * 80)
    print(f"{'Scenario':<32} | {'Trades':>6} | {'Win %':>6} | {'Net Pts':>9} | {'Avg Pts':>8} | {'Avg MFE':>8}")
    print("-" * 80)
    for sc, grp in tdf.groupby("scenario"):
        sc_wins = grp[grp["hit_60"]]
        net_pts = grp["pts_captured_60"].sum()
        avg_pts = grp["pts_captured_60"].mean()
        avg_mfe = grp["max_favourable"].mean()
        print(
            f"{sc:<32} | {len(grp):>6} | {len(sc_wins)/len(grp)*100:>5.1f}% | {net_pts:>+8.1f}p | {avg_pts:>+7.1f}p | {avg_mfe:>7.1f}p"
        )

    # Verification table for the exact days in student notes
    student_dates = ["2026-06-11", "2026-06-12", "2026-06-19", "2026-06-23", "2026-06-24", "2026-06-25"]
    student_sample = tdf[tdf["date"].isin(student_dates)]
    if not student_sample.empty:
        print("\n" + "=" * 80)
        print("VERIFICATION OF STUDENT LOG DATES (From Class Notes)")
        print("=" * 80)
        cols = ["date", "scenario", "direction", "entry_time", "entry_price", "max_favourable", "hit_60", "hit_120", "pts_captured_60"]
        print(student_sample[cols].to_string(index=False))

    if detailed:
        print("\n--- All Trades Detail (First 15) ---")
        cols = ["date", "scenario", "direction", "entry_time", "entry_price", "max_favourable", "max_adverse", "hit_60", "pts_captured_60"]
        print(tdf[cols].head(15).to_string(index=False))

    if export:
        export_path = config.DATA_DIR / "war_setup_trades.csv"
        tdf.to_csv(export_path, index=False)
        print(f"\n[OK] Exported {len(tdf)} trades to {export_path}")


def main():
    parser = argparse.ArgumentParser(description="Backtest WAR Setup (30-Min First-Candle FII Strategy).")
    parser.add_argument("--export", action="store_true", help="Export trades to CSV")
    parser.add_argument("--detailed", action="store_true", help="Print detailed trade logs")
    args = parser.parse_args()

    run_war_backtest(export=args.export, detailed=args.detailed)


if __name__ == "__main__":
    main()
