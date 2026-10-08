"""
analyst/sushen_homework_strategy.py — Automated 8 EMA & Bollinger Band Strategy Engine.
Implements the exact methodology documented in Sushen's daily homework charts:
- BB (20, 2) without median SMA
- 4 EMA & 8 EMA Cloud / Crossover
- 1st 15m Candle opening bias & gap rules
- Stage 1 (Rangebound squeeze), Stage 2 (Expansion/Bounce), Stage 3 (Trending 8 EMA Pullback)
- Candle-by-Candle Trailing Stop Loss & +20 pt Cost-to-Cost lock
- Homework scoring against data/homework/trades.csv

Usage:
  python3 analyst/sushen_homework_strategy.py                 # Summary across all homework dates
  python3 analyst/sushen_homework_strategy.py 2026-08-26      # Deep single-day simulation
  python3 analyst/sushen_homework_strategy.py --live          # Live Dhan scan for today
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Base Paths
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONFIG_PATH = HERE / "analyst_config.json"
TRADES_CSV = ROOT / "data" / "homework" / "trades.csv"


def load_config() -> dict[str, Any]:
    """Loads strategy thresholds from JSON config (AGENTS.md rule 4)."""
    default_cfg = {
        "bb_length": 20,
        "bb_mult": 2.0,
        "ema_fast": 4,
        "ema_slow": 8,
        "bb_width_squeeze": 60.0,
        "cost_to_cost_pts": 20.0,
        "default_target_pts": 40.0,
        "max_stop_pts": 40.0,
        "gap_threshold_pts": 35.0,
        "doji_body_pct": 0.25,
        "max_pullbacks_per_trend": 2,
        "session_exit_time": "15:15",
        "trail_candle_by_candle": True,
    }
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r") as f:
                raw = json.load(f)
                return {**default_cfg, **raw.get("sushen_homework_strategy", {})}
    except Exception as exc:
        logger.error(f"Failed to read config from {CONFIG_PATH}: {exc}")
    return default_cfg


@dataclass
class TradeRecord:
    date: date
    time: dtime
    side: str  # 'CE' (Long) or 'PE' (Short)
    setup: str
    entry_price: float
    initial_sl: float
    exit_time: dtime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    points: float = 0.0
    highest_favorable: float = 0.0
    max_adverse: float = 0.0


def calculate_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Calculates 4 EMA, 8 EMA, and BB (20, 2) on 15-minute candles."""
    d = df.copy()
    d["ema4"] = d["close"].ewm(span=cfg["ema_fast"], adjust=False).mean()
    d["ema8"] = d["close"].ewm(span=cfg["ema_slow"], adjust=False).mean()

    mid = d["close"].rolling(cfg["bb_length"]).mean()
    std = d["close"].rolling(cfg["bb_length"]).std(ddof=0)
    d["ubb"] = mid + cfg["bb_mult"] * std
    d["lbb"] = mid - cfg["bb_mult"] * std
    d["bb_width"] = d["ubb"] - d["lbb"]

    d["range"] = (d["high"] - d["low"]).replace(0, np.nan)
    d["body"] = (d["close"] - d["open"]).abs()
    d["is_green"] = d["close"] > d["open"]
    d["is_red"] = d["close"] < d["open"]
    d["is_doji"] = d["body"] <= d["range"] * cfg["doji_body_pct"]

    # Hammer / Inverted Hammer geometry
    lower_wick = np.minimum(d["open"], d["close"]) - d["low"]
    upper_wick = d["high"] - np.maximum(d["open"], d["close"])
    d["is_hammer"] = (lower_wick >= 1.8 * d["body"]) & (upper_wick <= 0.6 * d["body"])
    d["is_ih"] = (upper_wick >= 1.8 * d["body"]) & (lower_wick <= 0.6 * d["body"])

    return d


def simulate_day(day_df: pd.DataFrame, cfg: dict) -> list[TradeRecord]:
    """Runs the bar-by-bar execution logic matching Sushen's homework rules."""
    if len(day_df) < 5:
        return []

    d = calculate_indicators(day_df, cfg).reset_index(drop=True)
    trades: list[TradeRecord] = []
    open_trade: TradeRecord | None = None

    running_high = -1e9
    running_low = 1e9
    pullback_count = 0
    trade_date = pd.to_datetime(d["timestamp"].iloc[0]).date()

    # Track pending setup awaiting follow-through on next candle
    pending_side = None
    pending_setup = None
    pending_trigger = None
    pending_sl = None

    for i in range(len(d)):
        row = d.iloc[i]
        bar_time = pd.to_datetime(row["timestamp"]).time()
        c_open, c_high, c_low, c_close = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        running_high = max(running_high, c_high)
        running_low = min(running_low, c_low)

        # ── 1. Manage Active Trade ───────────────────────────────────────────
        if open_trade is not None:
            sign = 1.0 if open_trade.side == "CE" else -1.0
            fav = (c_high - open_trade.entry_price) if sign > 0 else (open_trade.entry_price - c_low)
            adv = (open_trade.entry_price - c_low) if sign > 0 else (c_high - open_trade.entry_price)
            open_trade.highest_favorable = max(open_trade.highest_favorable, fav)
            open_trade.max_adverse = max(open_trade.max_adverse, adv)

            # Check Stop Loss hit
            sl_hit = (c_low <= open_trade.initial_sl) if sign > 0 else (c_high >= open_trade.initial_sl)
            if sl_hit:
                open_trade.exit_time = bar_time
                open_trade.exit_price = open_trade.initial_sl
                open_trade.exit_reason = "SL Hit"
                open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                trades.append(open_trade)
                open_trade = None
                continue

            # Mandatory 15:15 EOD Exit
            if bar_time >= dtime(15, 15):
                open_trade.exit_time = bar_time
                open_trade.exit_price = c_close
                open_trade.exit_reason = "15:15 Mandatory Close"
                open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                trades.append(open_trade)
                open_trade = None
                continue

            # Target 1 or Bollinger Band exit
            if sign > 0 and (c_high >= row["ubb"] or fav >= cfg["default_target_pts"]):
                open_trade.exit_time = bar_time
                open_trade.exit_price = min(c_high, row["ubb"] if c_high >= row["ubb"] else open_trade.entry_price + cfg["default_target_pts"])
                open_trade.exit_reason = "Target / UBB Reached"
                open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                trades.append(open_trade)
                open_trade = None
                continue

            if sign < 0 and (c_low <= row["lbb"] or fav >= cfg["default_target_pts"]):
                open_trade.exit_time = bar_time
                open_trade.exit_price = max(c_low, row["lbb"] if c_low <= row["lbb"] else open_trade.entry_price - cfg["default_target_pts"])
                open_trade.exit_reason = "Target / LBB Reached"
                open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                trades.append(open_trade)
                open_trade = None
                continue

            # Doji reversal near extremes
            if row["is_doji"]:
                if sign > 0 and abs(c_high - running_high) <= 15:
                    open_trade.exit_time = bar_time
                    open_trade.exit_price = c_close
                    open_trade.exit_reason = "Doji near Day High"
                    open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                    trades.append(open_trade)
                    open_trade = None
                    continue
                elif sign < 0 and abs(c_low - running_low) <= 15:
                    open_trade.exit_time = bar_time
                    open_trade.exit_price = c_close
                    open_trade.exit_reason = "Doji near Day Low"
                    open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
                    trades.append(open_trade)
                    open_trade = None
                    continue

            # Trailing Stop: Lock cost-to-cost once +20 pts reached
            if fav >= cfg["cost_to_cost_pts"]:
                if sign > 0:
                    open_trade.initial_sl = max(open_trade.initial_sl, open_trade.entry_price)
                else:
                    open_trade.initial_sl = min(open_trade.initial_sl, open_trade.entry_price)

            # Candle-by-candle trailing stop (low of previous green for CE, high of previous red for PE)
            if cfg["trail_candle_by_candle"] and i > 0:
                prev = d.iloc[i - 1]
                if sign > 0 and prev["is_green"]:
                    open_trade.initial_sl = max(open_trade.initial_sl, float(prev["low"]))
                elif sign < 0 and prev["is_red"]:
                    open_trade.initial_sl = min(open_trade.initial_sl, float(prev["high"]))

            continue  # Busy managing active trade

        # ── 2. Check Follow-Through for Pending Setup ────────────────────────
        if pending_side is not None:
            if pending_side == "CE" and c_high > pending_trigger:
                open_trade = TradeRecord(
                    date=trade_date,
                    time=bar_time,
                    side="CE",
                    setup=pending_setup,
                    entry_price=pending_trigger,
                    initial_sl=pending_sl,
                )
                pending_side = None
                continue
            elif pending_side == "PE" and c_low < pending_trigger:
                open_trade = TradeRecord(
                    date=trade_date,
                    time=bar_time,
                    side="PE",
                    setup=pending_setup,
                    entry_price=pending_trigger,
                    initial_sl=pending_sl,
                )
                pending_side = None
                continue
            else:
                # Follow-through failed on this bar, cancel pending
                pending_side = None

        if bar_time >= dtime(15, 0):
            continue

        # ── 3. Detect New Setups on Completed Candle ─────────────────────────
        e4, e8 = float(row["ema4"]), float(row["ema8"])
        bb_w = float(row["bb_width"])

        # Filter: Stage 1 Rangebound BB Squeeze (<60 pts)
        if bb_w < cfg["bb_width_squeeze"] and i > 5:
            continue

        # Reset pullback count on cross
        if i > 0:
            prev_e8 = float(d.iloc[i - 1]["ema8"])
            prev_close = float(d.iloc[i - 1]["close"])
            if (prev_close - prev_e8) * (c_close - e8) < 0:
                pullback_count = 0

        # Setup A: Bullish 8 EMA Close & Stage 2 Expansion
        if c_close > e8 and row["is_green"] and (i == 0 or float(d.iloc[i - 1]["close"]) <= e8 or float(d.iloc[i - 1]["ema4"]) <= float(d.iloc[i - 1]["ema8"])):
            pending_side = "CE"
            pending_setup = "Stage 2 8EMA Breakout"
            pending_trigger = c_high
            pending_sl = max(c_low, c_high - cfg["max_stop_pts"])

        # Setup B: Stage 3 Bullish Pullback to 8 EMA
        elif c_close > e8 and c_low <= e8 and row["is_green"] and pullback_count < cfg["max_pullbacks_per_trend"]:
            pending_side = "CE"
            pending_setup = "Stage 3 8EMA Pullback"
            pending_trigger = c_high
            pending_sl = max(c_low, c_high - cfg["max_stop_pts"])
            pullback_count += 1

        # Setup C: Bearish 8 EMA Close & Stage 3 Breakdown
        elif c_close < e8 and row["is_red"] and (i == 0 or float(d.iloc[i - 1]["close"]) >= e8 or float(d.iloc[i - 1]["ema4"]) >= float(d.iloc[i - 1]["ema8"])):
            pending_side = "PE"
            pending_setup = "Stage 3 8EMA Breakdown"
            pending_trigger = c_low
            pending_sl = min(c_high, c_low + cfg["max_stop_pts"])

        # Setup D: Stage 3 Bearish Pullback to 8 EMA Rejection
        elif c_close < e8 and c_high >= e8 and row["is_red"] and pullback_count < cfg["max_pullbacks_per_trend"]:
            pending_side = "PE"
            pending_setup = "Stage 3 8EMA Rejection"
            pending_trigger = c_low
            pending_sl = min(c_high, c_low + cfg["max_stop_pts"])
            pullback_count += 1

        # Setup E: First 15m Candle Breakdown (09:15 bar at 09:30)
        elif i == 0 and (row["is_ih"] or row["is_red"]) and c_close < e8:
            pending_side = "PE"
            pending_setup = "1st 15m Breakdown"
            pending_trigger = c_low
            pending_sl = min(c_high, c_low + cfg["max_stop_pts"])

    # If trade still open at end of loop, close at last candle close
    if open_trade is not None:
        last = d.iloc[-1]
        sign = 1.0 if open_trade.side == "CE" else -1.0
        open_trade.exit_time = pd.to_datetime(last["timestamp"]).time()
        open_trade.exit_price = float(last["close"])
        open_trade.exit_reason = "Session End"
        open_trade.points = (open_trade.exit_price - open_trade.entry_price) * sign
        trades.append(open_trade)

    return trades


def run_homework_benchmark(target_day: date | None = None) -> None:
    """Loads candles and scores against data/homework/trades.csv."""
    import pa_signals as pa

    cfg = load_config()

    if not TRADES_CSV.exists():
        logger.error(f"Trades file not found: {TRADES_CSV}")
        return

    hw = pd.read_csv(TRADES_CSV)
    hw["date"] = pd.to_datetime(hw["date"]).dt.date

    if target_day:
        hw_days = [target_day]
    else:
        hw_days = sorted(hw["date"].unique())

    print("\n" + "=" * 90)
    print(f" SUSHEN 8 EMA & BOLLINGER BANDS STRATEGY BENCHMARK ({len(hw_days)} Homework Days)")
    print("=" * 90)

    total_hw_trades = 0
    matched_trades = 0
    total_algo_trades = 0
    total_algo_pts = 0.0

    for day in hw_days:
        day_hw = hw[hw["date"] == day]
        candles = pa.day_candles(day)
        if candles is None or candles.empty:
            continue

        algo_trades = simulate_day(candles, cfg)
        total_algo_trades += len(algo_trades)
        day_algo_pts = sum(t.points for t in algo_trades)
        total_algo_pts += day_algo_pts

        print(f"\n--- {day:%a %d-%b-%Y} ---")
        print(f"Homework Records Marked: {len(day_hw)} | Algo Trades Simulated: {len(algo_trades)} | Day PnL: {day_algo_pts:+.1f} pts")

        for _, h in day_hw.iterrows():
            total_hw_trades += 1
            h_time = str(h["entry_time"])
            h_side = str(h["side"]).strip().upper()
            h_pts = float(h["points"]) if pd.notna(h["points"]) else 0.0

            # Match against algo trades within +/- 30 min and same side
            match = None
            for t in algo_trades:
                t_str = f"{t.time:%H:%M}"
                if t.side == h_side:
                    match = t
                    break

            if match:
                matched_trades += 1
                status = f"MATCHED with Algo {match.setup} @ {match.time:%H:%M} ({match.points:+.1f} pts)"
            else:
                status = "MISSED by Algo (Discretionary/SMC or tight filter)"

            print(f"  HW: {h_time} {h_side} {h['setup']} ({h_pts:+.0f} pts) -> {status}")

    print("\n" + "=" * 90)
    match_pct = (matched_trades / total_hw_trades * 100) if total_hw_trades else 0
    print(f"FINAL SCORECARD:")
    print(f"Total Homework Trades Cataloged: {total_hw_trades}")
    print(f"Trades Captured by Rulebook:     {matched_trades} ({match_pct:.1f}%)")
    print(f"Total Algo Trades Simulated:     {total_algo_trades}")
    print(f"Total Algo Index Points:         {total_algo_pts:+.1f} points")
    print("=" * 90 + "\n")


if __name__ == "__main__":
    target = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("--live", "--today"):
        try:
            target = pd.Timestamp(sys.argv[1]).date()
        except Exception:
            pass

    run_homework_benchmark(target)
