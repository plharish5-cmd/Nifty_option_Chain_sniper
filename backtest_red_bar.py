"""
backtest_red_bar.py — Backtest Dr. Devendra Pratap Singh's Advanced Red Bar Strategy.

Usage:
    python backtest_red_bar.py                       # Runs full test on all historical sessions
    python backtest_red_bar.py --setup 1             # Runs only Setup 1 (POB Retest)
    python backtest_red_bar.py --setup 5             # Runs only Setup 5 (Asian Sweep)
    python backtest_red_bar.py --rr 1:1              # Uses 1:1 Risk-to-Reward
    python backtest_red_bar.py --detailed            # Prints detailed trade log
    python backtest_red_bar.py --export              # Exports trades to data/red_bar_backtest_trades.csv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

import config
from red_bar_strategy import RedBarSignal, scan_all_red_bar_setups

LOT_SIZE = getattr(config, "NIFTY_LOT_SIZE", 75)
ATM_DELTA = 0.50  # Dr. Devendra: ATM option delta is ~0.50


@dataclass
class RedBarBacktestTrade:
    date: str
    entry_time: str
    setup_name: str
    direction: str
    action: str
    entry_price: float
    stop_loss: float
    target_price: float
    risk_pts: float
    reward_target_pts: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""      # TARGET | STOP_LOSS | EOD
    spot_pnl: float = 0.0
    opt_pnl_est: float = 0.0
    pnl_rupees: float = 0.0
    max_fav_pts: float = 0.0
    max_adv_pts: float = 0.0
    hit_t1: bool = False
    hit_t15: bool = False
    hit_t2: bool = False
    notes: str = ""


def simulate_trade(
    day_df: pd.DataFrame,
    sig: RedBarSignal,
    rr_mode: str = "1:1.5",
) -> RedBarBacktestTrade:
    """Simulates forward price action for a RedBarSignal."""
    is_ce = sig.direction == "BULLISH"
    entry = sig.entry_price
    sl = sig.stop_loss
    risk = sig.risk_pts

    # Target selection based on rr_mode
    if rr_mode == "1:1":
        target = sig.target_1
    elif rr_mode == "1:2":
        target = sig.target_3
    else:  # default 1:1.5
        target = sig.target_2

    # Find starting index in day_df
    ts_series = pd.to_datetime(day_df["timestamp"])
    matches = day_df.index[ts_series == sig.timestamp].tolist()
    start_i = day_df.index.get_loc(matches[0]) if matches else 0

    max_fav = 0.0
    max_adv = 0.0
    hit_t1 = False
    hit_t15 = False
    hit_t2 = False
    hit_target = False
    hit_sl = False

    exit_time_str = "15:30:00"
    exit_price = entry
    exit_reason = "EOD"

    for i in range(start_i + 1, len(day_df)):
        row = day_df.iloc[i]
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        t = pd.to_datetime(row["timestamp"])

        fav = (h - entry) if is_ce else (entry - l)
        adv = (entry - l) if is_ce else (h - entry)
        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        # Track targets
        if is_ce:
            if h >= sig.target_1:
                hit_t1 = True
            if h >= sig.target_2:
                hit_t15 = True
            if h >= sig.target_3:
                hit_t2 = True

            # Check SL
            if l <= sl and exit_reason == "EOD":
                hit_sl = True
                exit_price = sl
                exit_time_str = f"{t:%H:%M:%S}"
                exit_reason = "STOP_LOSS"
                break
            # Check Target
            elif h >= target and exit_reason == "EOD":
                hit_target = True
                exit_price = target
                exit_time_str = f"{t:%H:%M:%S}"
                exit_reason = "TARGET"
                break
        else:
            if l <= sig.target_1:
                hit_t1 = True
            if l <= sig.target_2:
                hit_t15 = True
            if l <= sig.target_3:
                hit_t2 = True

            # Check SL
            if h >= sl and exit_reason == "EOD":
                hit_sl = True
                exit_price = sl
                exit_time_str = f"{t:%H:%M:%S}"
                exit_reason = "STOP_LOSS"
                break
            # Check Target
            elif l <= target and exit_reason == "EOD":
                hit_target = True
                exit_price = target
                exit_time_str = f"{t:%H:%M:%S}"
                exit_reason = "TARGET"
                break

    if exit_reason == "EOD" and len(day_df) > start_i + 1:
        last_row = day_df.iloc[-1]
        exit_price = float(last_row["close"])
        exit_time_str = f"{pd.to_datetime(last_row['timestamp']):%H:%M:%S}"

    spot_pnl = (exit_price - entry) if is_ce else (entry - exit_price)
    opt_pnl = spot_pnl * ATM_DELTA
    pnl_rupees = opt_pnl * LOT_SIZE

    return RedBarBacktestTrade(
        date=f"{pd.to_datetime(sig.timestamp):%Y-%m-%d}",
        entry_time=f"{sig.timestamp:%H:%M:%S}",
        setup_name=sig.setup_name,
        direction=sig.direction,
        action=sig.action,
        entry_price=round(entry, 2),
        stop_loss=round(sl, 2),
        target_price=round(target, 2),
        risk_pts=round(risk, 2),
        reward_target_pts=round(abs(target - entry), 2),
        exit_time=exit_time_str,
        exit_price=round(exit_price, 2),
        exit_reason=exit_reason,
        spot_pnl=round(spot_pnl, 2),
        opt_pnl_est=round(opt_pnl, 2),
        pnl_rupees=round(pnl_rupees, 2),
        max_fav_pts=round(max_fav, 2),
        max_adv_pts=round(max_adv, 2),
        hit_t1=hit_t1,
        hit_t15=hit_t15,
        hit_t2=hit_t2,
        notes=sig.notes,
    )


def run_backtest(
    setup_filter: str = "all",
    rr_mode: str = "1:1.5",
    detailed: bool = False,
    export_path: Optional[str] = None,
) -> pd.DataFrame:
    candles_dir = Path("data/candles_5m")
    files = sorted(candles_dir.glob("*.csv"))

    if not files:
        print(f"Error: No candle files found in {candles_dir.resolve()}")
        return pd.DataFrame()

    # Load continuous candle series
    dfs = []
    for f in files:
        d = pd.read_csv(f)
        d["date"] = f.stem
        dfs.append(d)
    full_df = pd.concat(dfs, ignore_index=True)
    full_df["ts"] = pd.to_datetime(full_df["timestamp"])
    full_df["time"] = full_df["ts"].dt.time
    full_df["ema30"] = full_df["close"].ewm(span=30, adjust=False).mean()

    all_trades: List[RedBarBacktestTrade] = []

    # Process session by session
    for d_str, day_df in full_df.groupby("date", sort=False):
        if len(day_df) < 20:
            continue

        signals = scan_all_red_bar_setups(day_df)

        for sig in signals:
            if setup_filter == "1" and sig.setup_name != "SETUP_1_POB_RETEST":
                continue
            elif setup_filter == "2" and sig.setup_name != "SETUP_2_RED_BAR":
                continue
            elif setup_filter == "3" and sig.setup_name != "SETUP_3_EMA_DEFLECTION":
                continue
            elif setup_filter == "5" and sig.setup_name != "SETUP_5_ASIAN_SWEEP":
                continue

            trade = simulate_trade(day_df, sig, rr_mode=rr_mode)
            all_trades.append(trade)

    df_trades = pd.DataFrame([tr.__dict__ for tr in all_trades])

    print("=" * 96)
    print("       DR. DEVENDRA PRATAP SINGH'S ADVANCED RED BAR STRATEGY BACKTEST")
    print("=" * 96)
    print(f"Sessions Scanned:    {len(files)} trading days (2026-06 to 2026-09)")
    print(f"Active Filter:       Setup: {setup_filter.upper()} | Risk:Reward Target: {rr_mode}")
    print(f"Total Setups Fired:  {len(df_trades)}")

    if df_trades.empty:
        print("No trades triggered under the selected filter.")
        return df_trades

    wins = df_trades[df_trades["exit_reason"] == "TARGET"]
    losses = df_trades[df_trades["exit_reason"] == "STOP_LOSS"]
    eods = df_trades[df_trades["exit_reason"] == "EOD"]

    win_rate = len(wins) / len(df_trades) * 100
    gross_profit = df_trades[df_trades["spot_pnl"] > 0]["spot_pnl"].sum()
    gross_loss = abs(df_trades[df_trades["spot_pnl"] < 0]["spot_pnl"].sum())
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0
    tot_spot_pnl = df_trades["spot_pnl"].sum()
    tot_opt_pnl = df_trades["opt_pnl_est"].sum()
    tot_rupees = df_trades["pnl_rupees"].sum()

    print("-" * 96)
    print(f"Overall Win Rate:    {win_rate:5.1f}% ({len(wins)} Wins / {len(losses)} Losses / {len(eods)} EOD Exits)")
    print(f"Profit Factor:       {profit_factor:5.2f}")
    print(f"Net Spot Points:     {tot_spot_pnl:+7.1f} pts")
    print(f"Net Option Points:   {tot_opt_pnl:+7.1f} pts (ATM Delta ~0.50)")
    print(f"Total PnL (₹/lot):   ₹{tot_rupees:+11,.2f}")
    print(f"Avg Favourable Run:  +{df_trades['max_fav_pts'].mean():5.1f} pts")
    print(f"Avg Adverse Pullback:-{df_trades['max_adv_pts'].mean():5.1f} pts")
    print("-" * 96)

    # Breakdown by Setup
    print("\n=== SETUP-BY-SETUP PERFORMANCE BREAKDOWN ===")
    setup_summary = []
    for s_name, group in df_trades.groupby("setup_name"):
        s_wins = group[group["exit_reason"] == "TARGET"]
        s_losses = group[group["exit_reason"] == "STOP_LOSS"]
        s_wr = len(s_wins) / len(group) * 100
        gp = group[group["spot_pnl"] > 0]["spot_pnl"].sum()
        gl = abs(group[group["spot_pnl"] < 0]["spot_pnl"].sum())
        pf = round(gp / gl, 2) if gl > 0 else 99.0
        setup_summary.append({
            "Setup Name": s_name,
            "Trades": len(group),
            "Win Rate": f"{s_wr:.1f}%",
            "Wins": len(s_wins),
            "Losses": len(s_losses),
            "Profit Factor": pf,
            "Net Spot (pts)": f"{group['spot_pnl'].sum():+.1f}",
            "PnL (₹/lot)": f"₹{group['pnl_rupees'].sum():+,.0f}",
            "Avg Win (pts)": f"+{s_wins['spot_pnl'].mean():.1f}" if not s_wins.empty else "0.0",
            "Avg Loss (pts)": f"{s_losses['spot_pnl'].mean():.1f}" if not s_losses.empty else "0.0",
        })

    print(pd.DataFrame(setup_summary).to_string(index=False))

    if detailed:
        print("\n--- Detailed Trade Log ---")
        show_cols = [
            "date", "entry_time", "setup_name", "direction", "entry_price",
            "stop_loss", "target_price", "exit_reason", "spot_pnl", "pnl_rupees"
        ]
        print(df_trades[show_cols].to_string(index=False))

    if export_path:
        out_p = Path(export_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        df_trades.to_csv(out_p, index=False)
        print(f"\n[OK] Trades successfully exported to: {out_p.resolve()}")

    return df_trades


def main():
    parser = argparse.ArgumentParser(description="Backtest Dr. Devendra Pratap Singh Advanced Red Bar Strategy")
    parser.add_argument("--setup", type=str, default="all", choices=["all", "1", "2", "3", "5"], help="Filter specific setup")
    parser.add_argument("--rr", type=str, default="1:1.5", choices=["1:1", "1:1.5", "1:2"], help="Risk to Reward target")
    parser.add_argument("--detailed", action="store_true", help="Print trade-by-trade log")
    parser.add_argument("--export", action="store_true", help="Export trades to data/red_bar_backtest_trades.csv")

    args = parser.parse_args()
    export_file = "data/red_bar_backtest_trades.csv" if args.export else None

    run_backtest(
        setup_filter=args.setup,
        rr_mode=args.rr,
        detailed=args.detailed,
        export_path=export_file,
    )


if __name__ == "__main__":
    main()
