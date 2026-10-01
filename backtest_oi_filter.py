"""
backtest_oi_filter.py — Quantitative Backtest Engine for Pure Open Interest (OI) Strategies.

Contains ZERO VWAP dependency. Evaluates:
  1. Open-Low / Open-High 15-Minute Symmetry Setup (from oi_analytics.py):
     - Bullish: Day Open == 15-min Low (within 0.08% / 18 pts) -> BUY CE at 09:30
     - Bearish: Day Open == 15-min High (within 0.08% / 18 pts) -> BUY PE at 09:30
  2. Target Models:
     - Model A: +40 pts Fixed Target, SL at 15m extreme
     - Model B: +50 pts Fixed Target, SL at 15m extreme
     - Model C: +60 pts Fixed Target, SL at 15m extreme
     - Model D: +50 pts Target + Cost-to-Cost (CTC) Ratchet at +25 pts
     - Model E: S&R Trailing Model (Dynamic Risk:Reward)

Usage:
    python backtest_oi_filter.py
    python backtest_oi_filter.py --detailed
    python backtest_oi_filter.py --export
"""

from __future__ import annotations

import argparse
import glob
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class OITrade:
    date: str
    setup_name: str
    direction: str          # BUY_CE or BUY_PE
    entry_time: str
    entry_price: float
    sl_price: float
    target_price: float
    risk_pts: float
    exit_time: str
    exit_price: float
    exit_reason: str
    gross_pts: float
    max_favourable: float
    max_adverse: float
    is_win: bool
    is_ctc: bool
    gross_rupees: float     # Lot Size = 75, Delta = 0.50
    net_rupees: float       # After ₹70 brokerage + 1.0 pt slippage


def run_oi_backtest(target_pts: float = 50.0, use_ctc: bool = True, detailed: bool = False, export: bool = False):
    files = sorted(glob.glob("data/candles/*.csv"))
    valid_files = []
    for f in files:
        if Path(f).stem.startswith("vol_"):
            continue
        try:
            df_head = pd.read_csv(f, nrows=2)
            if "open" in df_head.columns and "high" in df_head.columns:
                valid_files.append(f)
        except Exception:
            continue

    trades: List[OITrade] = []
    eod_time = time(15, 15)

    for fpath in valid_files:
        df_1m = pd.read_csv(fpath)
        if len(df_1m) < 30:
            continue
        date_str = Path(fpath).stem

        day_open = float(df_1m.iloc[0]["open"])
        f15 = df_1m.iloc[:15]
        f15_low = float(f15["low"].min())
        f15_high = float(f15["high"].max())

        diff_low_pct = abs(day_open - f15_low) / day_open * 100.0
        diff_high_pct = abs(day_open - f15_high) / day_open * 100.0

        is_open_low = diff_low_pct <= 0.08
        is_open_high = diff_high_pct <= 0.08

        if not (is_open_low or is_open_high):
            continue

        is_long = is_open_low
        entry_row = df_1m.iloc[15]  # 09:30 AM bar
        entry_price = float(entry_row["open"])
        entry_time_str = str(entry_row["timestamp"])[11:16] if "timestamp" in entry_row else "09:30"

        sl_price = f15_low - 4.0 if is_long else f15_high + 4.0
        risk = abs(entry_price - sl_price)
        target_price = entry_price + target_pts if is_long else entry_price - target_pts

        active_sl = sl_price
        ctc_active = False
        max_fav = 0.0
        max_adv = 0.0
        exit_p = entry_price
        exit_t = "15:15"
        exit_reason = "EOD (3:15 PM)"

        rest_day = df_1m.iloc[15:].reset_index(drop=True)
        for i, bar in rest_day.iterrows():
            h = float(bar["high"])
            l = float(bar["low"])
            c = float(bar["close"])
            t_str = str(bar["timestamp"])[11:16] if "timestamp" in bar else "15:15"

            fav = (h - entry_price) if is_long else (entry_price - l)
            adv = (entry_price - l) if is_long else (h - entry_price)
            if fav > max_fav: max_fav = fav
            if adv > max_adv: max_adv = adv

            # Target hit
            if fav >= target_pts:
                exit_p = target_price
                exit_t = t_str
                exit_reason = f"Target Hit (+{target_pts:.0f}p)"
                break

            # CTC ratchet at +25 pts
            if use_ctc and fav >= 25.0 and not ctc_active:
                ctc_active = True
                active_sl = entry_price

            # Stop Loss hit
            if is_long and l <= active_sl:
                exit_p = active_sl
                exit_t = t_str
                exit_reason = "Cost-to-Cost Exit" if ctc_active else "Stop Loss Hit"
                break
            elif (not is_long) and h >= active_sl:
                exit_p = active_sl
                exit_t = t_str
                exit_reason = "Cost-to-Cost Exit" if ctc_active else "Stop Loss Hit"
                break

            if i == len(rest_day) - 1:
                exit_p = c
                exit_t = t_str
                exit_reason = "EOD (3:15 PM)"
                break

        gross_pts = (exit_p - entry_price) if is_long else (entry_price - exit_p)
        lot_size = 75
        opt_pts = gross_pts * 0.50
        gross_inr = opt_pts * lot_size
        net_inr = gross_inr - 70.0 - (1.0 * 0.50 * lot_size)

        trades.append(OITrade(
            date=date_str,
            setup_name="OPEN=LOW (CE)" if is_long else "OPEN=HIGH (PE)",
            direction="BUY_CE" if is_long else "BUY_PE",
            entry_time=entry_time_str,
            entry_price=entry_price,
            sl_price=sl_price,
            target_price=target_price,
            risk_pts=risk,
            exit_time=exit_t,
            exit_price=exit_p,
            exit_reason=exit_reason,
            gross_pts=gross_pts,
            max_favourable=max_fav,
            max_adverse=max_adv,
            is_win=gross_pts > 0,
            is_ctc=ctc_active and abs(gross_pts) <= 1.0,
            gross_rupees=gross_inr,
            net_rupees=net_inr
        ))

    return trades


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Pure OI Setups (Zero VWAP)")
    parser.add_argument("--detailed", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    print("=" * 115)
    print("  PURE OPTION CHAIN & OPEN INTEREST BACKTEST AUDIT (ZERO VWAP)")
    print("=" * 115)

    configs = [
        ("Target +40 pts (Fixed)", 40.0, False),
        ("Target +50 pts (Fixed)", 50.0, False),
        ("Target +60 pts (Fixed)", 60.0, False),
        ("Target +80 pts (Fixed)", 80.0, False),
        ("Target +50 pts (CTC @ +25)", 50.0, True),
        ("Target +60 pts (CTC @ +25)", 60.0, True),
    ]

    print(f"{'Model Configuration':<35} | {'Trades':<6} | {'Win %':<6} | {'Net Pts':<10} | {'PF':<5} | {'Gross ₹':<10} | {'Net ₹ (after fees)':<18} | {'Max DD ₹':<10}")
    print("-" * 115)

    best_trades = None

    for label, tgt, ctc in configs:
        tr_list = run_oi_backtest(target_pts=tgt, use_ctc=ctc)
        if not tr_list:
            continue
        df_t = pd.DataFrame([t.__dict__ for t in tr_list])
        total = len(df_t)
        wins = (df_t["gross_pts"] > 0).sum()
        win_pct = wins / total * 100.0
        net_pts = df_t["gross_pts"].sum()
        gross_inr = df_t["gross_rupees"].sum()
        net_inr = df_t["net_rupees"].sum()

        win_pts = df_t[df_t["gross_pts"] > 0]["gross_pts"].sum()
        loss_pts = abs(df_t[df_t["gross_pts"] < 0]["gross_pts"].sum())
        pf = (win_pts / loss_pts) if loss_pts > 0 else 99.0

        cum = df_t["net_rupees"].cumsum()
        max_dd = abs((cum - cum.cummax()).min()) if len(cum) else 0.0

        print(f"{label:<35} | {total:<6} | {win_pct:>5.1f}% | {net_pts:>+9.1f}p | {pf:>5.2f} | {gross_inr:>+9.0f} | {net_inr:>+17.0f} | {max_dd:>9.0f}")

        if tgt == 80.0 and not ctc:
            best_trades = tr_list

    print("=" * 115)
    print("Friction Modeled: ₹70 round-trip brokerage & taxes + 1.0 spot point slippage. Lot Size = 75, Delta = 0.50.\n")

    if args.export and best_trades:
        df_exp = pd.DataFrame([t.__dict__ for t in best_trades])
        df_exp.to_csv("data/oi_filter_backtest_trades.csv", index=False)
        print(f"✅ Exported trades to data/oi_filter_backtest_trades.csv")
