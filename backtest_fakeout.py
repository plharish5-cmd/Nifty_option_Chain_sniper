"""
backtest_fakeout.py — Backtesting Priyank Sharma's Fakeout Strategy on NIFTY intraday candles.

Usage:
    python backtest_fakeout.py                  # runs both 5m and 15m
    python backtest_fakeout.py --timeframe 15m   # 15m only
    python backtest_fakeout.py --timeframe 5m    # 5m only
    python backtest_fakeout.py --detailed        # print trade log
    python backtest_fakeout.py --export          # save data/fakeout_backtest_trades.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import config
from fakeout_strategy import (
    BUFFER_PTS,
    DEFAULT_MAX_ENTRY_TIME,
    FakeoutSignal,
    detect_immediate_fakeout,
    detect_rounded_fakeout,
)


@dataclass
class BacktestTrade:
    date: str
    timeframe: str
    setup: str
    direction: str
    action: str
    level_name: str
    level_price: float
    entry_time: str
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_pts: float
    reward_t1: float
    reward_t2: float
    exit_time: str = ""
    exit_price: float = 0.0
    outcome: str = ""
    pnl_pts: float = 0.0
    pnl_rupees: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0


def simulate_trade(df: pd.DataFrame, entry_idx: int, trade: BacktestTrade) -> BacktestTrade:
    entry = trade.entry_price
    sl = trade.stop_loss
    t1 = trade.target_1
    t2 = trade.target_2
    is_bull = trade.direction == "BULLISH"

    t1_hit = False
    max_fav = 0.0
    max_adv = 0.0

    for i in range(entry_idx + 1, len(df)):
        row = df.iloc[i]
        h = float(row["high"])
        l = float(row["low"])
        t = str(row["timestamp"])

        if is_bull:
            fav = h - entry
            adv = entry - l
        else:
            fav = entry - l
            adv = h - entry
        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        if is_bull:
            if not t1_hit:
                if l <= sl:
                    trade.outcome = "SL_HIT"
                    trade.exit_time = t
                    trade.exit_price = sl
                    trade.pnl_pts = -trade.risk_pts
                    break
                if h >= t1:
                    t1_hit = True
                    if h >= t2 and t2 > t1:
                        trade.outcome = "T1_AND_T2_HIT"
                        trade.exit_time = t
                        trade.exit_price = t2
                        trade.pnl_pts = 0.5 * (t1 - entry) + 0.5 * (t2 - entry)
                        break
            else:
                if h >= t2 and t2 > t1:
                    trade.outcome = "T1_AND_T2_HIT"
                    trade.exit_time = t
                    trade.exit_price = t2
                    trade.pnl_pts = 0.5 * (t1 - entry) + 0.5 * (t2 - entry)
                    break
                if l <= entry:
                    trade.outcome = "T1_HIT_BE"
                    trade.exit_time = t
                    trade.exit_price = entry
                    trade.pnl_pts = 0.5 * (t1 - entry)
                    break
        else:  # BEARISH
            if not t1_hit:
                if h >= sl:
                    trade.outcome = "SL_HIT"
                    trade.exit_time = t
                    trade.exit_price = sl
                    trade.pnl_pts = -trade.risk_pts
                    break
                if l <= t1:
                    t1_hit = True
                    if l <= t2 and t2 < t1:
                        trade.outcome = "T1_AND_T2_HIT"
                        trade.exit_time = t
                        trade.exit_price = t2
                        trade.pnl_pts = 0.5 * (entry - t1) + 0.5 * (entry - t2)
                        break
            else:
                if l <= t2 and t2 < t1:
                    trade.outcome = "T1_AND_T2_HIT"
                    trade.exit_time = t
                    trade.exit_price = t2
                    trade.pnl_pts = 0.5 * (entry - t1) + 0.5 * (entry - t2)
                    break
                if h >= entry:
                    trade.outcome = "T1_HIT_BE"
                    trade.exit_time = t
                    trade.exit_price = entry
                    trade.pnl_pts = 0.5 * (entry - t1)
                    break
    else:
        last = df.iloc[-1]
        trade.exit_time = str(last["timestamp"])
        trade.exit_price = float(last["close"])
        pnl = (last["close"] - entry) if is_bull else (entry - last["close"])
        if t1_hit:
            trade.outcome = "T1_HIT_EOD"
            trade.pnl_pts = 0.5 * trade.risk_pts + 0.5 * pnl
        else:
            trade.outcome = "EOD_EXIT"
            trade.pnl_pts = pnl

    trade.mfe = max_fav
    trade.mae = max_adv
    trade.pnl_rupees = trade.pnl_pts * config.LOT_SIZE
    return trade


def scan_session(
    df: pd.DataFrame,
    date_str: str,
    levels: Dict[str, Tuple[float, str]],
    tf: str,
    max_entry_time: time = DEFAULT_MAX_ENTRY_TIME,
    max_risk_pts: float = 55.0,
) -> List[BacktestTrade]:
    trades: List[BacktestTrade] = []
    used_levels = set()
    start_idx = 2 if tf == "15m" else 3

    # Scan candle by candle
    for idx in range(start_idx, len(df) - 2):
        candle_ts = pd.to_datetime(df.iloc[idx]["timestamp"])
        if candle_ts.time() > max_entry_time:
            break

        for lvl_name, (lvl_val, lvl_type) in levels.items():
            if lvl_name in used_levels:
                continue

            # Check Immediate Fakeout
            sig = detect_immediate_fakeout(df, lvl_val, lvl_type, lvl_name, check_index=idx)
            if not sig:
                # Check Rounded Fakeout
                sig = detect_rounded_fakeout(df, lvl_val, lvl_type, lvl_name, check_index=idx)

            if sig and sig.risk_pts <= max_risk_pts:
                bt_trade = BacktestTrade(
                    date=date_str,
                    timeframe=tf,
                    setup=sig.setup_type,
                    direction=sig.direction,
                    action=sig.action,
                    level_name=sig.level_name,
                    level_price=sig.level_price,
                    entry_time=f"{sig.timestamp:%Y-%m-%d %H:%M:%S}",
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    target_1=sig.target_1,
                    target_2=sig.target_2,
                    risk_pts=sig.risk_pts,
                    reward_t1=sig.reward_t1_pts,
                    reward_t2=sig.reward_t2_pts,
                )
                simulated = simulate_trade(df, idx, bt_trade)
                trades.append(simulated)
                used_levels.add(lvl_name)

    return trades


def run_backtest(timeframes: List[str], max_risk: float = 55.0, export: bool = False, detailed: bool = False):
    all_trades: List[BacktestTrade] = []

    for tf in timeframes:
        folder = config.DATA_DIR / f"candles_{tf}"
        if not folder.exists():
            print(f"Warning: {folder} does not exist. Skipping {tf}.")
            continue

        files = sorted(folder.glob("*.csv"))
        days: Dict[str, pd.DataFrame] = {}
        for f in files:
            d = pd.read_csv(f)
            d["timestamp"] = pd.to_datetime(d["timestamp"])
            d = d.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            if len(d) >= 5:
                days[f.stem] = d

        dates = sorted(days.keys())
        tf_trades: List[BacktestTrade] = []

        for i in range(1, len(dates)):
            prev_df = days[dates[i - 1]]
            cur_df = days[dates[i]]
            cur_date = dates[i]

            prev_ohlc = {
                "open": float(prev_df.iloc[0]["open"]),
                "high": float(prev_df["high"].max()),
                "low": float(prev_df["low"].min()),
                "close": float(prev_df.iloc[-1]["close"]),
            }

            m_bars = 1 if tf == "15m" else 3
            levels = {
                "PDH": (prev_ohlc["high"], "RESISTANCE"),
                "PDL": (prev_ohlc["low"], "SUPPORT"),
                "PDC": (
                    prev_ohlc["close"],
                    "RESISTANCE" if cur_df.iloc[0]["open"] < prev_ohlc["close"] else "SUPPORT",
                ),
                "MORNING_HIGH": (float(cur_df.iloc[:m_bars]["high"].max()), "RESISTANCE"),
                "MORNING_LOW": (float(cur_df.iloc[:m_bars]["low"].min()), "SUPPORT"),
            }

            session_trades = scan_session(cur_df, cur_date, levels, tf, max_risk_pts=max_risk)
            tf_trades.extend(session_trades)

        all_trades.extend(tf_trades)

        # Print report for this timeframe
        print_report(tf_trades, tf, len(dates) - 1, detailed=detailed)

    if export and all_trades:
        export_path = config.DATA_DIR / "fakeout_backtest_trades.csv"
        df_exp = pd.DataFrame([t.__dict__ for t in all_trades])
        df_exp.to_csv(export_path, index=False)
        print(f"\n[OK] Exported {len(all_trades)} trades to {export_path}")


def print_report(trades: List[BacktestTrade], tf: str, total_sessions: int, detailed: bool = False):
    print("\n" + "=" * 75)
    print(f"       FAKEOUT STRATEGY BACKTEST — {tf.upper()} TIMEFRAME ({total_sessions} Sessions)")
    print("=" * 75)

    if not trades:
        print("No trades triggered.")
        return

    tdf = pd.DataFrame([t.__dict__ for t in trades])

    wins = tdf[tdf["outcome"].isin(["T1_AND_T2_HIT", "T1_HIT_BE", "T1_HIT_EOD"])]
    t2_wins = tdf[tdf["outcome"] == "T1_AND_T2_HIT"]
    losses = tdf[tdf["outcome"] == "SL_HIT"]
    be_exits = tdf[tdf["outcome"] == "T1_HIT_BE"]

    win_rate = len(wins) / len(tdf) * 100
    t2_rate = len(t2_wins) / len(tdf) * 100
    loss_rate = len(losses) / len(tdf) * 100
    total_pts = tdf["pnl_pts"].sum()
    avg_pts = tdf["pnl_pts"].mean()
    total_inr = tdf["pnl_rupees"].sum()

    gross_profit = tdf[tdf["pnl_pts"] > 0]["pnl_pts"].sum()
    gross_loss = abs(tdf[tdf["pnl_pts"] < 0]["pnl_pts"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

    print(f"Total Trades:               {len(tdf)}")
    print(f"Win Rate (Target 1 Reached): {win_rate:.1f}% ({len(wins)} / {len(tdf)})")
    print(f"Target 2 Reached:            {t2_rate:.1f}% ({len(t2_wins)} / {len(tdf)})")
    print(f"Stopped Out (Full SL):       {loss_rate:.1f}% ({len(losses)} / {len(tdf)})")
    print(f"Breakeven Exits (After T1):  {len(be_exits)} trades")
    print(f"Total Net Points:            {total_pts:+.1f} pts")
    print(f"Avg Return per Trade:        {avg_pts:+.1f} pts")
    print(f"Net PnL (1 Lot @ {config.LOT_SIZE} qty):  ₹{total_inr:+,.0f}")
    print(f"Profit Factor:               {profit_factor:.2f}")

    print("\n--- Setup Breakdown ---")
    print(f"{'Setup':<20} | {'Trades':>6} | {'Win %':>6} | {'Net Pts':>9} | {'Avg Pts':>8} | {'PF':>5}")
    print("-" * 65)
    for setup, grp in tdf.groupby("setup"):
        s_wins = grp[grp["outcome"].isin(["T1_AND_T2_HIT", "T1_HIT_BE", "T1_HIT_EOD"])]
        s_pts = grp["pnl_pts"].sum()
        s_gp = grp[grp["pnl_pts"] > 0]["pnl_pts"].sum()
        s_gl = abs(grp[grp["pnl_pts"] < 0]["pnl_pts"].sum())
        s_pf = (s_gp / s_gl) if s_gl > 0 else 999.0
        print(
            f"{setup:<20} | {len(grp):>6} | {len(s_wins)/len(grp)*100:>5.1f}% | {s_pts:>+8.1f}p | {grp['pnl_pts'].mean():>+7.1f}p | {s_pf:>5.2f}"
        )

    print("\n--- Level Breakdown ---")
    print(f"{'Level':<16} | {'Trades':>6} | {'Win %':>6} | {'Net Pts':>9} | {'Avg Pts':>8}")
    print("-" * 55)
    for lvl, grp in tdf.groupby("level_name"):
        l_wins = grp[grp["outcome"].isin(["T1_AND_T2_HIT", "T1_HIT_BE", "T1_HIT_EOD"])]
        l_pts = grp["pnl_pts"].sum()
        print(
            f"{lvl:<16} | {len(grp):>6} | {len(l_wins)/len(grp)*100:>5.1f}% | {l_pts:>+8.1f}p | {grp['pnl_pts'].mean():>+7.1f}p"
        )

    if detailed:
        print("\n--- Individual Trades (Most Recent 10) ---")
        cols = ["date", "setup", "direction", "level_name", "entry_time", "entry_price", "stop_loss", "risk_pts", "outcome", "pnl_pts"]
        print(tdf[cols].tail(10).to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Backtest Priyank Sharma's Fakeout Strategy.")
    parser.add_argument(
        "--timeframe",
        "-tf",
        choices=["5m", "15m", "both"],
        default="both",
        help="Candle timeframe to test (default: both)",
    )
    parser.add_argument("--max-risk", type=float, default=55.0, help="Max SL points allowed (default: 55)")
    parser.add_argument("--export", action="store_true", help="Export trades to CSV")
    parser.add_argument("--detailed", action="store_true", help="Print recent individual trades")
    args = parser.parse_args()

    tfs = ["5m", "15m"] if args.timeframe == "both" else [args.timeframe]
    run_backtest(tfs, max_risk=args.max_risk, export=args.export, detailed=args.detailed)


if __name__ == "__main__":
    main()
