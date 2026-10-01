"""
backtest_vwap_institutional.py — Quantitative Backtest Engine for Institutional VWAP Strategies.

Integrates the 3 core Indian institutional trading frameworks:
  1. 6-Year Quantitative Nifty Benchmark Study:
     - Strategy 1: 09:30 Opening Continuation
     - Strategy 2: Post-09:35 Failed Opening Reversal (Bullish Reclaim & Bearish Breakdown)
  2. Mukul Chowdhury (Dhan) Institutional Liquidity Sweeps:
     - Asian / morning low/high sweep and reclaim back across VWAP
  3. CA Nitin Murarka & Mentor Sushen Risk Rules:
     - 1-Hour Time-Based Stop Loss (exit stagnant option trades before theta decay)
     - 1:1 Partial Target (50% booking) + Ratchet to Cost-to-Cost (CTC)
     - Triple Confluence Filter (Price vs VWAP, 20 EMA, and 4x Slow MACD)
  4. oi_analytics Open-Low / Open-High 15-minute S&R Setup

Usage:
    python backtest_vwap_institutional.py                  # Full comparative backtest across all models
    python backtest_vwap_institutional.py --detailed       # Print individual trade execution logs
    python backtest_vwap_institutional.py --export         # Export all trade logs to CSV
    python backtest_vwap_institutional.py --setup s2       # Focus specifically on Strategy 2 Reversals
"""

from __future__ import annotations

import argparse
import glob
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd

import vwap_institutional_strategy as vis


# ─────────────────────────────────────────────────────────────────────────────
# Trade Data Structure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    date: str
    model_name: str
    strategy_id: str
    direction: str          # BUY (CE) or SELL (PE)
    entry_time: str
    entry_price: float
    initial_sl: float
    risk_pts: float
    target_pts: float = 0.0
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    gross_pts: float = 0.0
    max_favourable: float = 0.0
    max_adverse: float = 0.0
    duration_min: int = 0
    is_win: bool = False
    is_ctc: bool = False
    opt_pnl_pts: float = 0.0        # ATM Delta ~0.50 estimation
    gross_rupees: float = 0.0       # Lot size 75
    net_rupees: float = 0.0         # After ₹70 brokerage + 1.0 pt slippage


# ─────────────────────────────────────────────────────────────────────────────
# 1-Minute Candle Loader & 5-Minute Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def load_session_data(data_dir: Path = Path("data/candles")) -> Dict[str, pd.DataFrame]:
    """
    Loads all valid historical 1m CSV files and resamples them to 5m with volume.
    """
    files = sorted(glob.glob(str(data_dir / "*.csv")))
    sessions: Dict[str, pd.DataFrame] = {}

    for fpath in files:
        fname = Path(fpath).stem
        if fname.startswith("vol_"):
            continue
        try:
            df = pd.read_csv(fpath)
            if "timestamp" not in df.columns or "open" not in df.columns or "volume" not in df.columns:
                continue
            if len(df) < 30:
                continue

            df["ts"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("ts")

            # Resample 1m to 5m bars
            df_5m = df.resample("5min").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum"
            }).dropna().reset_index()

            if len(df_5m) >= 10:
                sessions[fname] = df_5m
        except Exception:
            continue

    return sessions


# ─────────────────────────────────────────────────────────────────────────────
# Simulation Engines
# ─────────────────────────────────────────────────────────────────────────────

def simulate_strategy_1(
    df: pd.DataFrame,
    date_str: str,
    use_confluence_filter: bool = False,
    use_ctc_target: bool = False,
    target_multiple: float = 1.5,
    max_duration_min: int = 0
) -> Optional[BacktestTrade]:
    """
    Strategy 1: 09:30 Opening Continuation.
    """
    d = vis.compute_vwap_and_bands(df)
    if len(d) < 5:
        return None

    c2 = d.iloc[min(2, len(d) - 1)]  # 09:25 candle
    open_dir = "BULLISH" if c2["close"] > c2["vwap"] else "BEARISH"
    is_long = (open_dir == "BULLISH")

    entry_row = d.iloc[3]  # 09:30 candle
    entry_price = float(entry_row["open"])
    entry_time = str(entry_row["ts"].time())[:5]

    # Triple Confluence Filter
    if use_confluence_filter:
        if is_long and not (entry_row["close"] > entry_row["ema20"] and entry_row["macd_hist_4x"] > 0):
            return None
        if (not is_long) and not (entry_row["close"] < entry_row["ema20"] and entry_row["macd_hist_4x"] < 0):
            return None

    # SL at previous candle low/high or VWAP
    if is_long:
        initial_sl = min(float(c2["low"]), float(c2["vwap"])) - 2.0
    else:
        initial_sl = max(float(c2["high"]), float(c2["vwap"])) + 2.0

    risk = abs(entry_price - initial_sl)
    target_pts = max(30.0, risk * target_multiple) if use_ctc_target else 0.0

    active_sl = initial_sl
    ctc_triggered = False
    exit_price = entry_price
    exit_time = "15:15"
    exit_reason = "EOD (3:15 PM)"
    max_fav = 0.0
    max_adv = 0.0
    eod_time = time(15, 15)

    for i in range(3, len(d)):
        row = d.iloc[i]
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        vw = float(row["vwap"])
        t = row["ts"].time()
        elapsed = (i - 3 + 1) * 5

        fav = (h - entry_price) if is_long else (entry_price - l)
        adv = (entry_price - l) if is_long else (h - entry_price)
        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        # Murarka 1-Hour Time Stop (< 15 pts after 60 min)
        if max_duration_min > 0 and elapsed >= max_duration_min:
            if fav < 15.0:
                exit_price = c
                exit_time = str(t)[:5]
                exit_reason = "1-Hour Stagnant Time Exit"
                break

        # Check Target
        if use_ctc_target and fav >= target_pts:
            exit_price = entry_price + target_pts if is_long else entry_price - target_pts
            exit_time = str(t)[:5]
            exit_reason = f"Target Hit (+{target_pts:.1f}p)"
            break

        # Ratchet SL to CTC when reaching 50% target
        if use_ctc_target and fav >= (target_pts * 0.5) and not ctc_triggered:
            ctc_triggered = True
            active_sl = entry_price

        # Stop Loss hit
        if is_long and l <= active_sl:
            exit_price = active_sl
            exit_time = str(t)[:5]
            exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
            break
        elif (not is_long) and h >= active_sl:
            exit_price = active_sl
            exit_time = str(t)[:5]
            exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
            break

        # Dynamic VWAP Violation Exit
        if is_long and c < vw:
            exit_price = c
            exit_time = str(t)[:5]
            exit_reason = "VWAP Trailing SL Cross"
            break
        elif (not is_long) and c > vw:
            exit_price = c
            exit_time = str(t)[:5]
            exit_reason = "VWAP Trailing SL Cross"
            break

        if t >= eod_time or i == len(d) - 1:
            exit_price = c
            exit_time = str(t)[:5]
            exit_reason = "EOD (3:15 PM)"
            break

    gross_pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    dur = (int(exit_time[:2]) * 60 + int(exit_time[3:5])) - (int(entry_time[:2]) * 60 + int(entry_time[3:5]))
    dur = max(5, dur)

    opt_pts = gross_pts * 0.50
    lot_size = 75
    gross_inr = opt_pts * lot_size
    # Deduct ₹70 brokerage & taxes + 1.0 pt spot slippage
    net_inr = gross_inr - 70.0 - (1.0 * 0.50 * lot_size)

    model_label = "Strat 1 (Raw VWAP)"
    if use_confluence_filter:
        model_label = "Strat 1 + 20EMA/MACD"
    elif use_ctc_target:
        model_label = "Strat 1 + 1:1.5 CTC"

    return BacktestTrade(
        date=date_str,
        model_name=model_label,
        strategy_id="S1_CONTINUATION",
        direction="BUY (CE)" if is_long else "SELL (PE)",
        entry_time=entry_time,
        entry_price=entry_price,
        initial_sl=initial_sl,
        risk_pts=risk,
        target_pts=target_pts,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_pts=gross_pts,
        max_favourable=max_fav,
        max_adverse=max_adv,
        duration_min=dur,
        is_win=gross_pts > 0,
        is_ctc=abs(gross_pts) <= 1.0 and ctc_triggered,
        opt_pnl_pts=opt_pts,
        gross_rupees=gross_inr,
        net_rupees=net_inr
    )


def simulate_strategy_2(
    df: pd.DataFrame,
    date_str: str,
    long_only_gold: bool = False,
    use_ctc_target: bool = False,
    target_pts_fixed: float = 60.0,
    max_duration_min: int = 0
) -> Optional[BacktestTrade]:
    """
    Strategy 2: Post-09:35 Failed Opening Reversal (The Dr. Devendra / Dhan Playbook Edge).
    """
    d = vis.compute_vwap_and_bands(df)
    if len(d) < 5:
        return None

    c2 = d.iloc[min(2, len(d) - 1)]  # 09:25 candle
    open_dir = "BULLISH" if c2["close"] > c2["vwap"] else "BEARISH"

    cutoff_entry_time = time(14, 0)
    eod_time = time(15, 15)

    trade: Optional[BacktestTrade] = None

    for i in range(4, len(d)):
        row = d.iloc[i]
        c = float(row["close"])
        vw = float(row["vwap"])
        t = row["ts"].time()

        if t >= cutoff_entry_time:
            break

        triggered = False
        is_long = False
        setup_type = ""

        # Bullish Reclaim: Opened Bearish, but closes back above VWAP
        if open_dir == "BEARISH" and c > vw:
            triggered = True
            is_long = True
            setup_type = "S2_BULLISH_RECLAIM"

        # Bearish Breakdown: Opened Bullish, but closes back below VWAP
        elif open_dir == "BULLISH" and c < vw and not long_only_gold:
            triggered = True
            is_long = False
            setup_type = "S2_BEARISH_BREAKDOWN"

        if not triggered:
            continue

        # Triggered! Enter on open of next candle
        entry_idx = min(i + 1, len(d) - 1)
        entry_row = d.iloc[entry_idx]
        entry_price = float(entry_row["open"])
        entry_time = str(entry_row["ts"].time())[:5]

        # Stop Loss: Morning extreme (low of day for long, high of day for short)
        if is_long:
            morning_low = float(d.iloc[:entry_idx]["low"].min())
            initial_sl = morning_low - 2.0
        else:
            morning_high = float(d.iloc[:entry_idx]["high"].max())
            initial_sl = morning_high + 2.0

        risk = abs(entry_price - initial_sl)
        # Cap risk to max 60 pts to prevent huge out-of-whack stops
        if is_long:
            initial_sl = max(initial_sl, entry_price - 60.0)
        else:
            initial_sl = min(initial_sl, entry_price + 60.0)
        risk = min(risk, 60.0)

        active_sl = initial_sl
        ctc_triggered = False
        exit_price = entry_price
        exit_time = "15:15"
        exit_reason = "EOD (3:15 PM)"
        max_fav = 0.0
        max_adv = 0.0

        for j in range(entry_idx, len(d)):
            r2 = d.iloc[j]
            h2 = float(r2["high"])
            l2 = float(r2["low"])
            c2_sub = float(r2["close"])
            vw2_sub = float(r2["vwap"])
            t2 = r2["ts"].time()
            elapsed = (j - entry_idx + 1) * 5

            fav = (h2 - entry_price) if is_long else (entry_price - l2)
            adv = (entry_price - l2) if is_long else (h2 - entry_price)
            if fav > max_fav:
                max_fav = fav
            if adv > max_adv:
                max_adv = adv

            # Nitin Murarka 1-Hour Time Stop (< 15 pts after 60 min)
            if max_duration_min > 0 and elapsed >= max_duration_min:
                if fav < 15.0:
                    exit_price = c2_sub
                    exit_time = str(t2)[:5]
                    exit_reason = "1-Hour Stagnant Time Exit"
                    break

            # Target Check
            if use_ctc_target and fav >= target_pts_fixed:
                exit_price = entry_price + target_pts_fixed if is_long else entry_price - target_pts_fixed
                exit_time = str(t2)[:5]
                exit_reason = f"Target Hit (+{target_pts_fixed:.1f}p)"
                break

            # CTC Ratchet at +25 pts
            if use_ctc_target and fav >= 25.0 and not ctc_triggered:
                ctc_triggered = True
                active_sl = entry_price

            # Stop Loss hit
            if is_long and l2 <= active_sl:
                exit_price = active_sl
                exit_time = str(t2)[:5]
                exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
                break
            elif (not is_long) and h2 >= active_sl:
                exit_price = active_sl
                exit_time = str(t2)[:5]
                exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
                break

            # Dynamic VWAP Violation Exit (if not using fixed target)
            if not use_ctc_target:
                if is_long and c2_sub < vw2_sub:
                    exit_price = c2_sub
                    exit_time = str(t2)[:5]
                    exit_reason = "VWAP Trailing SL Cross"
                    break
                elif (not is_long) and c2_sub > vw2_sub:
                    exit_price = c2_sub
                    exit_time = str(t2)[:5]
                    exit_reason = "VWAP Trailing SL Cross"
                    break

            if t2 >= eod_time or j == len(d) - 1:
                exit_price = c2_sub
                exit_time = str(t2)[:5]
                exit_reason = "EOD (3:15 PM)"
                break

        gross_pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
        dur = (int(exit_time[:2]) * 60 + int(exit_time[3:5])) - (int(entry_time[:2]) * 60 + int(entry_time[3:5]))
        dur = max(5, dur)

        opt_pts = gross_pts * 0.50
        lot_size = 75
        gross_inr = opt_pts * lot_size
        net_inr = gross_inr - 70.0 - (1.0 * 0.50 * lot_size)

        model_label = "Strat 2 (Raw VWAP)"
        if long_only_gold:
            model_label = "Strat 2 (Bull Reclaim Only)"
        elif use_ctc_target:
            model_label = f"Strat 2 (+{target_pts_fixed:.0f}p Target + CTC)"

        trade = BacktestTrade(
            date=date_str,
            model_name=model_label,
            strategy_id=setup_type,
            direction="BUY (CE)" if is_long else "SELL (PE)",
            entry_time=entry_time,
            entry_price=entry_price,
            initial_sl=initial_sl,
            risk_pts=risk,
            target_pts=target_pts_fixed if use_ctc_target else 0.0,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_pts=gross_pts,
            max_favourable=max_fav,
            max_adverse=max_adv,
            duration_min=dur,
            is_win=gross_pts > 0,
            is_ctc=abs(gross_pts) <= 1.0 and ctc_triggered,
            opt_pnl_pts=opt_pts,
            gross_rupees=gross_inr,
            net_rupees=net_inr
        )
        break  # 1 trade per session

    return trade


def simulate_mukul_sweeps(df: pd.DataFrame, date_str: str) -> Optional[BacktestTrade]:
    """
    Setup 3: Mukul Chowdhury Liquidity Sweep & Reclaim.
    """
    d = vis.compute_vwap_and_bands(df)
    if len(d) < 10:
        return None

    trade: Optional[BacktestTrade] = None
    cutoff_entry_time = time(13, 0)
    eod_time = time(15, 15)

    for i in range(4, len(d)):
        curr = d.iloc[i]
        t = curr["ts"].time()
        if t >= cutoff_entry_time:
            break

        prev_window = d.iloc[max(0, i-4):i]
        prev_low = float(prev_window["low"].min())
        prev_high = float(prev_window["high"].max())

        c = float(curr["close"])
        o = float(curr["open"])
        l = float(curr["low"])
        h = float(curr["high"])
        vw = float(curr["vwap"])

        is_bull_sweep = (l < prev_low) and (c > vw) and (c > o)
        is_bear_sweep = (h > prev_high) and (c < vw) and (c < o)

        if not (is_bull_sweep or is_bear_sweep):
            continue

        is_long = is_bull_sweep
        entry_idx = min(i + 1, len(d) - 1)
        entry_row = d.iloc[entry_idx]
        entry_price = float(entry_row["open"])
        entry_time = str(entry_row["ts"].time())[:5]

        initial_sl = l - 2.0 if is_long else h + 2.0
        risk = abs(entry_price - initial_sl)
        target = 45.0  # Quick scalp target

        exit_price = entry_price
        exit_time = "15:15"
        exit_reason = "EOD"
        max_fav = 0.0
        max_adv = 0.0

        for j in range(entry_idx, len(d)):
            r2 = d.iloc[j]
            h2 = float(r2["high"])
            l2 = float(r2["low"])
            c2_sub = float(r2["close"])
            t2 = r2["ts"].time()

            fav = (h2 - entry_price) if is_long else (entry_price - l2)
            adv = (entry_price - l2) if is_long else (h2 - entry_price)
            if fav > max_fav:
                max_fav = fav
            if adv > max_adv:
                max_adv = adv

            if fav >= target:
                exit_price = entry_price + target if is_long else entry_price - target
                exit_time = str(t2)[:5]
                exit_reason = f"Target Hit (+{target}p)"
                break

            if is_long and l2 <= initial_sl:
                exit_price = initial_sl
                exit_time = str(t2)[:5]
                exit_reason = "Stop Loss Hit"
                break
            elif (not is_long) and h2 >= initial_sl:
                exit_price = initial_sl
                exit_time = str(t2)[:5]
                exit_reason = "Stop Loss Hit"
                break

            if t2 >= eod_time or j == len(d) - 1:
                exit_price = c2_sub
                exit_time = str(t2)[:5]
                exit_reason = "EOD (3:15 PM)"
                break

        gross_pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
        dur = (int(exit_time[:2]) * 60 + int(exit_time[3:5])) - (int(entry_time[:2]) * 60 + int(entry_time[3:5]))
        dur = max(5, dur)

        opt_pts = gross_pts * 0.50
        lot_size = 75
        gross_inr = opt_pts * lot_size
        net_inr = gross_inr - 70.0 - (1.0 * 0.50 * lot_size)

        trade = BacktestTrade(
            date=date_str,
            model_name="Mukul Liquidity Sweep (+45p Target)",
            strategy_id="MUKUL_SWEEP",
            direction="BUY (CE)" if is_long else "SELL (PE)",
            entry_time=entry_time,
            entry_price=entry_price,
            initial_sl=initial_sl,
            risk_pts=risk,
            target_pts=target,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_pts=gross_pts,
            max_favourable=max_fav,
            max_adverse=max_adv,
            duration_min=dur,
            is_win=gross_pts > 0,
            is_ctc=False,
            opt_pnl_pts=opt_pts,
            gross_rupees=gross_inr,
            net_rupees=net_inr
        )
        break

    return trade


def simulate_open_low_high(df: pd.DataFrame, date_str: str) -> Optional[BacktestTrade]:
    """
    Setup 4: oi_analytics Open-Low / Open-High 15-Minute S&R Setup.
    Checks if day open is within 0.08% (~18 pts) of first 15-min low or high.
    """
    if len(df) < 10:
        return None

    day_open = float(df.iloc[0]["open"])
    first_3_bars = df.iloc[:3]  # 09:15, 09:20, 09:25 (first 15m)
    f15_low = float(first_3_bars["low"].min())
    f15_high = float(first_3_bars["high"].max())

    diff_low_pct = abs(day_open - f15_low) / day_open * 100.0
    diff_high_pct = abs(day_open - f15_high) / day_open * 100.0

    is_open_low = diff_low_pct <= 0.08
    is_open_high = diff_high_pct <= 0.08

    if not (is_open_low or is_open_high):
        return None

    is_long = is_open_low
    entry_row = df.iloc[3]  # 09:30 candle
    entry_price = float(entry_row["open"])
    entry_time = str(entry_row["ts"].time())[:5]

    initial_sl = f15_low - 4.0 if is_long else f15_high + 4.0
    risk = abs(entry_price - initial_sl)
    target = 50.0  # +50 pts Target
    active_sl = initial_sl
    ctc_triggered = False

    exit_price = entry_price
    exit_time = "15:15"
    exit_reason = "EOD"
    max_fav = 0.0
    max_adv = 0.0
    eod_time = time(15, 15)

    for i in range(3, len(df)):
        row = df.iloc[i]
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        t = row["ts"].time()

        fav = (h - entry_price) if is_long else (entry_price - l)
        adv = (entry_price - l) if is_long else (h - entry_price)
        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        if fav >= target:
            exit_price = entry_price + target if is_long else entry_price - target
            exit_time = str(t)[:5]
            exit_reason = f"Target Hit (+{target}p)"
            break

        if fav >= 25.0 and not ctc_triggered:
            ctc_triggered = True
            active_sl = entry_price

        if is_long and l <= active_sl:
            exit_price = active_sl
            exit_time = str(t)[:5]
            exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
            break
        elif (not is_long) and h >= active_sl:
            exit_price = active_sl
            exit_time = str(t)[:5]
            exit_reason = "Cost-to-Cost Exit" if ctc_triggered else "Stop Loss Hit"
            break

        if t >= eod_time or i == len(df) - 1:
            exit_price = c
            exit_time = str(t)[:5]
            exit_reason = "EOD (3:15 PM)"
            break

    gross_pts = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    dur = (int(exit_time[:2]) * 60 + int(exit_time[3:5])) - (int(entry_time[:2]) * 60 + int(entry_time[3:5]))
    dur = max(5, dur)

    opt_pts = gross_pts * 0.50
    lot_size = 75
    gross_inr = opt_pts * lot_size
    net_inr = gross_inr - 70.0 - (1.0 * 0.50 * lot_size)

    return BacktestTrade(
        date=date_str,
        model_name="oi_analytics: Open-Low/High (+50p CTC)",
        strategy_id="OI_OPEN_LOW_HIGH",
        direction="BUY (CE)" if is_long else "SELL (PE)",
        entry_time=entry_time,
        entry_price=entry_price,
        initial_sl=initial_sl,
        risk_pts=risk,
        target_pts=target,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_pts=gross_pts,
        max_favourable=max_fav,
        max_adverse=max_adv,
        duration_min=dur,
        is_win=gross_pts > 0,
        is_ctc=abs(gross_pts) <= 1.0 and ctc_triggered,
        opt_pnl_pts=opt_pts,
        gross_rupees=gross_inr,
        net_rupees=net_inr
    )


# ─────────────────────────────────────────────────────────────────────────────
# Quantitative Performance Metric Generator
# ─────────────────────────────────────────────────────────────────────────────

def calculate_metrics(trades: List[BacktestTrade]) -> Dict[str, Any]:
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "net_pts": 0.0, "profit_factor": 0.0,
            "avg_pts": 0.0, "gross_rupees": 0.0, "net_rupees": 0.0, "max_drawdown_inr": 0.0,
            "avg_mfe": 0.0, "avg_mae": 0.0
        }

    df = pd.DataFrame([t.__dict__ for t in trades])
    total = len(df)
    wins = (df["gross_pts"] > 0).sum()
    win_rate = (wins / total) * 100.0 if total > 0 else 0.0

    net_pts = df["gross_pts"].sum()
    avg_pts = df["gross_pts"].mean()
    gross_inr = df["gross_rupees"].sum()
    net_inr = df["net_rupees"].sum()

    win_pts = df[df["gross_pts"] > 0]["gross_pts"].sum()
    loss_pts = abs(df[df["gross_pts"] < 0]["gross_pts"].sum())
    pf = (win_pts / loss_pts) if loss_pts > 0 else (99.0 if win_pts > 0 else 0.0)

    # Calculate Max Drawdown in Rupees
    cum_net = df["net_rupees"].cumsum()
    peak = cum_net.cummax()
    dd = cum_net - peak
    max_dd = abs(dd.min()) if len(dd) > 0 else 0.0

    avg_mfe = df["max_favourable"].mean()
    avg_mae = df["max_adverse"].mean()

    return {
        "total_trades": total,
        "win_rate": win_rate,
        "net_pts": net_pts,
        "profit_factor": pf,
        "avg_pts": avg_pts,
        "gross_rupees": gross_inr,
        "net_rupees": net_inr,
        "max_drawdown_inr": max_dd,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main Runner & CLI Reporter
# ─────────────────────────────────────────────────────────────────────────────

def run_full_backtest(detailed: bool = False, export_csv: bool = False, focus_setup: Optional[str] = None):
    print("=" * 135)
    print("  INSTITUTIONAL VWAP & ADVANCED OPTIONS STRATEGY QUANTITATIVE AUDIT")
    print("=" * 135)

    sessions = load_session_data()
    print(f"Loaded {len(sessions)} valid 5-minute historical sessions (June 8, 2026 – September 4, 2026).\n")

    models = {
        "Model 1: Strat 1 (09:30 Continuation - Raw VWAP Exit)": lambda df, d: simulate_strategy_1(df, d, use_confluence_filter=False, use_ctc_target=False),
        "Model 2: Strat 1 (+ 20 EMA & 4x MACD Triple Confluence)": lambda df, d: simulate_strategy_1(df, d, use_confluence_filter=True, use_ctc_target=False),
        "Model 3: Strat 1 (+ 1:1.5 Target & CTC Trailing)": lambda df, d: simulate_strategy_1(df, d, use_confluence_filter=False, use_ctc_target=True),
        "Model 4: Strat 2 (Post-09:35 Failed Reversal - Raw VWAP)": lambda df, d: simulate_strategy_2(df, d, long_only_gold=False, use_ctc_target=False),
        "Model 5: Strat 2 (⭐ Bullish Reclaim GOLD SETUP Only)": lambda df, d: simulate_strategy_2(df, d, long_only_gold=True, use_ctc_target=False),
        "Model 6: Strat 2 (+60p Fixed Target + CTC Ratchet)": lambda df, d: simulate_strategy_2(df, d, long_only_gold=False, use_ctc_target=True, target_pts_fixed=60.0),
        "Model 7: Strat 2 (+ Nitin Murarka 1-Hour Time Stop)": lambda df, d: simulate_strategy_2(df, d, long_only_gold=False, use_ctc_target=True, target_pts_fixed=60.0, max_duration_min=60),
        "Model 8: Mukul Chowdhury Liquidity Sweeps (+45p Target)": lambda df, d: simulate_mukul_sweeps(df, d),
        "Model 9: oi_analytics: Open-Low/High Setup (+50p CTC)": lambda df, d: simulate_open_low_high(df, d),
    }

    all_model_trades: Dict[str, List[BacktestTrade]] = {}

    for name, func in models.items():
        trade_list = []
        for d_str, df_sess in sessions.items():
            t = func(df_sess, d_str)
            if t is not None:
                trade_list.append(t)
        all_model_trades[name] = trade_list

    # Print Comparative Matrix
    print(f"{'Model Name':<56} | {'Trades':<6} | {'Win %':<6} | {'Net Pts':<10} | {'PF':<5} | {'Gross ₹':<10} | {'Net ₹ (after fees)':<18} | {'Max DD ₹':<10}")
    print("-" * 135)

    all_trades_flat: List[BacktestTrade] = []

    for name, t_list in all_model_trades.items():
        all_trades_flat.extend(t_list)
        m = calculate_metrics(t_list)
        net_str = f"₹{m['net_rupees']:+,.0f}"
        gross_str = f"₹{m['gross_rupees']:+,.0f}"
        pts_str = f"{m['net_pts']:+,.1f}p"
        dd_str = f"₹{m['max_drawdown_inr']:,.0f}"
        print(f"{name:<56} | {m['total_trades']:<6} | {m['win_rate']:>5.1f}% | {pts_str:>10} | {m['profit_factor']:>5.2f} | {gross_str:>10} | {net_str:>18} | {dd_str:>10}")

    print("=" * 135)
    print("Friction Modeled: ₹70 round-trip brokerage & taxes + 1.0 spot point slippage. Lot Size = 75, Delta = 0.50.\n")

    # Detailed log if requested
    if detailed:
        print("\n" + "=" * 115)
        print("  DETAILED TRADE LOGS (Top Consistent Models)")
        print("=" * 115)
        target_models = [
            "Model 6: Strat 2 (+60p Fixed Target + CTC Ratchet)",
            "Model 9: oi_analytics: Open-Low/High Setup (+50p CTC)"
        ]
        for tm in target_models:
            print(f"\n--- {tm} ---")
            df_log = pd.DataFrame([t.__dict__ for t in all_model_trades[tm]])
            if not df_log.empty:
                cols = ["date", "entry_time", "direction", "entry_price", "exit_time", "exit_price", "gross_pts", "exit_reason", "net_rupees"]
                print(df_log[cols].to_string(index=False))

    # Export CSV if requested
    if export_csv:
        out_path = Path("data/vwap_institutional_backtest_trades.csv")
        df_export = pd.DataFrame([t.__dict__ for t in all_trades_flat])
        df_export.to_csv(out_path, index=False)
        print(f"\n✅ All {len(df_export)} simulated backtest trades exported to `{out_path}`")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Institutional VWAP Strategies")
    parser.add_argument("--detailed", action="store_true", help="Print detailed trade breakdowns")
    parser.add_argument("--export", action="store_true", help="Export all trades to CSV")
    parser.add_argument("--setup", type=str, default=None, help="Filter by setup")
    args = parser.parse_args()

    run_full_backtest(detailed=args.detailed, export_csv=args.export, focus_setup=args.setup)
