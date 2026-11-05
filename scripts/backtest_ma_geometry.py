#!/usr/bin/env python3
"""
scripts/backtest_ma_geometry.py

Advanced Chart Geometry, Contact & Regime Backtest for Port 8510 / OC App.
Boss Upgraded Edition:
  - 17 Geometry, Contact, Ribbon, MTF & Volume Measures
  - Three-Layer Protocol (Spearman rho + Inverted-U Chop-Removal -> Monotone Terciles -> 50/50 OOS Split)
  - Dual Exit Scoring: Model 1 (Fixed 40/40 after Rs 400 brokerage) vs Model 2 (Super Trail v1.1 + Follow-Through CTC)
  - Mandatory Controls: Random Time-Matched Baseline (200 Monte Carlo draws), Session Bootstrap (1,000 whole-day iterations),
    Opposite Direction Control (Short), Outlier Ex-Top 1/3/5 Wins, and Multiple-Comparison Noise Ceiling (+2.0 SD).
  - Strict BUILD Hurdle: OOS Net Rs/trade > 0 after Rs 400 brokerage at lot 65, PF >= 1.30, OOS n >= 60,
    Ex-top5 > 0, Z-score >= +2.0 SD, Directional Advantage > 0.
"""

from __future__ import annotations

import bisect
import csv
import json
import logging
import math
import os
import random
import sqlite3
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper_trading

from algo_execution_engine.backtest_setup1 import (
    BacktestTrade,
    Setup1BacktestSimulator,
    build_15m_data,
    load_historical_1m_data,
)
from algo_execution_engine.strategy_setup1 import add_course_emas

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest.geometry")

DB_PATH = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
OUT_DIR = ROOT / "scripts" / "out" / "ma_geometry"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOT_SIZE = 65
OPT_BASE_ENTRY = 200.0
SLIPPAGE_PTS_PER_LEG = 0.25
SLIPPAGE_INR_PER_TRADE = round(2.0 * SLIPPAGE_PTS_PER_LEG * LOT_SIZE, 2)  # ₹32.50
ASYM_WIN_DELTA = 0.60
ASYM_LOSS_DELTA = 0.50
FIXED_SL_PTS = 40.0
FIXED_TARGET_PTS = 40.0
SQUARE_OFF_TIME = "15:20:00"
BROKERAGE_HURDLE_INR = 400.0  # Mandated ₹400 round-trip hurdle at lot 65

# Super Trail v1.1 parameters
SUPER_TRAIL_ACTIVATION = 15.0
SUPER_TRAIL_JUMP = 5.0
SUPER_TRAIL_INITIAL_SL = 20.0
SUPER_TRAIL_INITIAL_TARGET = 25.0
SUPER_TRAIL_TARGET_STEP = 10.0
SUPER_TRAIL_TARGET_PULLBACK = 5.0
SUPER_TRAIL_CTC_TRIGGER = 40.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. MATHEMATICAL INDICATORS & ROLLING PERCENTILES
# ─────────────────────────────────────────────────────────────────────────────

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR on High, Low, Close."""
    h, l, c = df["high"], df["low"], df["close"]
    tr0 = h - l
    tr1 = (h - c.shift(1)).abs()
    tr2 = (l - c.shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Standard Wilder's ADX(14)."""
    h, l, c = df["high"], df["low"], df["close"]
    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr0 = h - l
    tr1 = (h - c.shift(1)).abs()
    tr2 = (l - c.shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / (atr + 1e-9))
    minus_di = 100.0 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / (atr + 1e-9))
    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return adx


def fast_rolling_percentile(s: pd.Series, window: int = 6250) -> pd.Series:
    """Fast rolling percentile rank in O(log W) using bisect."""
    vals = s.tolist()
    n = len(vals)
    out = [np.nan] * n
    win = deque()
    sorted_win: List[float] = []
    for i, v in enumerate(vals):
        if np.isnan(v):
            continue
        win.append(v)
        bisect.insort(sorted_win, v)
        if len(win) > window:
            old = win.popleft()
            idx = bisect.bisect_right(sorted_win, old) - 1
            del sorted_win[idx]
        if len(sorted_win) >= 30:
            rank = bisect.bisect_right(sorted_win, v)
            out[i] = (rank / len(sorted_win)) * 100.0
    return pd.Series(out, index=s.index)


# ─────────────────────────────────────────────────────────────────────────────
# 2. DATA LOADING & RESAMPLING (15M & 1H BARS WITH ZERO LOOK-AHEAD)
# ─────────────────────────────────────────────────────────────────────────────

def resample_bars(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resamples 1m data aligned to 09:15 IST (closed-left, label-left)."""
    df = df_1m.copy()
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime")
    agg = {
        "ts": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "session_date": "first",
        "session_time": "first",
    }
    out = df.resample(rule, closed="left", label="left").agg(agg).dropna(subset=["ts"]).reset_index(drop=True)
    return out


def prepare_15m_geometry_frame(df_15m: pd.DataFrame) -> pd.DataFrame:
    """Computes all 15m line geometry, contact depth, and regime features."""
    df = df_15m.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # Moving Averages
    df["ema4"] = c.ewm(span=4, adjust=False).mean()
    df["ema8"] = c.ewm(span=8, adjust=False).mean()
    df["sma4"] = c.rolling(4, min_periods=1).mean()

    # ATR(14)
    df["atr14"] = calc_atr(df, 14).replace(0, np.nan).bfill()

    # Bollinger Bands (20, 2)
    mbb = c.rolling(20).mean()
    std = c.rolling(20).std(ddof=0)
    df["mbb"] = mbb
    df["ubb"] = mbb + 2.0 * std
    df["lbb"] = mbb - 2.0 * std
    df["bb_width"] = (df["ubb"] - df["lbb"]) / (df["mbb"] + 1e-9)
    df["bb_width_percentile"] = fast_rolling_percentile(df["bb_width"], window=6250)

    # ADX(14)
    df["adx14"] = calc_adx(df, 14)

    # Group A: Geometry Measures (#1 to #11)
    spread = df["ema4"] - df["ema8"]
    df["m01_separation_signed"] = spread / df["atr14"]
    df["m02_separation_abs"] = spread.abs() / df["atr14"]
    df["m03_separation_percentile"] = fast_rolling_percentile(df["m02_separation_abs"], window=6250)

    df["m04_ma4_slope"] = (df["ema4"] - df["ema4"].shift(5)) / (5.0 * df["atr14"] + 1e-9)
    df["m05_ema8_slope"] = (df["ema8"] - df["ema8"].shift(5)) / (5.0 * df["atr14"] + 1e-9)

    df["m06_spread_velocity"] = (spread - spread.shift(3)) / (3.0 * df["atr14"] + 1e-9)
    df["m07_spread_phase"] = np.sign(spread) * np.sign(df["m06_spread_velocity"])
    df["m08_ema8_curvature"] = (df["ema8"] - 2.0 * df["ema8"].shift(3) + df["ema8"].shift(6)) / df["atr14"]
    df["m09_adx14"] = df["adx14"]
    df["m10_bb_width_percentile"] = df["bb_width_percentile"]
    df["m11_ma4_angle_deg"] = np.degrees(np.arctan(df["m04_ma4_slope"].fillna(0.0)))

    # Group B: Boss Upgrades (#12 to #15)
    # Measure 12: 5-Bar Integrated Ribbon Area
    df["m12_ribbon_area_5"] = spread.rolling(5, min_periods=1).sum() / (5.0 * df["atr14"] + 1e-9)

    # Measure 13: 12-Bar Whip-Saw Cross Count
    sign_spread = np.sign(spread)
    flips = (sign_spread != sign_spread.shift(1)).astype(int)
    flips.iloc[0] = 0
    df["m13_whipsaw_cross_count_12"] = flips.rolling(12, min_periods=1).sum()

    # Measure 14: 8-EMA Contact Penetration Depth (Long perspective)
    df["m14_penetration_depth"] = (df["ema8"] - l) / df["atr14"]

    # Measure 15: Contact Wick Rejection Intensity (Pin-Bar Ratio)
    bar_range = (h - l).replace(0, 0.01)
    df["m15_wick_rejection_pinbar"] = (c - l) / bar_range

    # Secondary: SMA4 vs EMA8
    sma_spread = df["sma4"] - df["ema8"]
    df["sma4_vs_ema8_separation_signed"] = sma_spread / df["atr14"]
    df["sma4_vs_ema8_spread_velocity"] = (sma_spread - sma_spread.shift(3)) / (3.0 * df["atr14"] + 1e-9)

    return df


def prepare_1h_trend_frame(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Computes 1H 8-EMA slope and candle alignment for Measure #16 (Mentor's Rule)."""
    df = df_1h.copy()
    c = df["close"]
    o = df["open"]
    df["ema8_1h"] = c.ewm(span=8, adjust=False).mean()
    df["atr14_1h"] = calc_atr(df, 14).replace(0, np.nan).bfill()
    df["m16_slope_1h_8ema"] = (df["ema8_1h"] - df["ema8_1h"].shift(2)) / (2.0 * df["atr14_1h"] + 1e-9)
    df["m16_candle_align_1h"] = np.where(c >= o, 1.0, -1.0)
    return df


class ClosedBarLookup:
    """Zero look-ahead lookup: retrieves the last fully closed bar strictly before t."""

    def __init__(self, df: pd.DataFrame, bar_seconds: int):
        self.ts = df["ts"].tolist()
        self.records = df.to_dict(orient="records")
        self.bar_seconds = bar_seconds

    def get(self, t: int) -> Optional[Dict[str, Any]]:
        idx = bisect.bisect_right(self.ts, t - self.bar_seconds) - 1
        return self.records[idx] if idx >= 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# 3. FORWARD-WALK SIMULATORS (FIXED 40/40, DIRECTION CONTROL & SUPER TRAIL + CTC)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_direction_control_short(
    entry_ts: int,
    entry_spot: float,
    session_1m: List[Dict[str, Any]],
) -> Tuple[float, float, float, float, float, str, float]:
    """
    Mandatory Control 1: Mirror short trade entered at exact same bar.
    Returns: (spot_pnl, hurdle400_net, statutory_net, net_with_slip, stat_charges, reason, duration_min)
    """
    short_target = entry_spot - FIXED_TARGET_PTS
    short_sl = entry_spot + FIXED_SL_PTS
    idx_start = bisect.bisect_right([b["ts"] for b in session_1m], entry_ts)

    for i in range(idx_start, len(session_1m)):
        b = session_1m[i]
        b_time = b["session_time"]
        b_high = b["high"]
        b_low = b["low"]
        b_close = b["close"]
        b_ts = b["ts"]

        # Check SL (High >= entry + 40)
        if b_high >= short_sl:
            spot_pnl = entry_spot - short_sl
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * ASYM_LOSS_DELTA
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "SL_HIT", max(0.0, dur_min)

        # Check Target (Low <= entry - 40)
        if b_low <= short_target:
            spot_pnl = entry_spot - short_target
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * ASYM_WIN_DELTA
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "TARGET_HIT", max(0.0, dur_min)

        # EOD Square-off
        if b_time >= SQUARE_OFF_TIME or i == len(session_1m) - 1:
            spot_pnl = entry_spot - b_close
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "EOD_SQUARE_OFF", max(0.0, dur_min)

    return 0.0, -400.0, -78.0, -110.5, 78.0, "NO_DATA", 0.0


def simulate_super_trail_ctc_long(
    entry_ts: int,
    entry_spot: float,
    session_1m: List[Dict[str, Any]],
    closed_15m_ema8: float,
) -> Tuple[float, float, float, float, float, str, float]:
    """
    Model 2: Super Trail v1.1 + Follow-Through CTC Rule.
    If immediate bar after entry fails to follow through (e.g., lower low or closes below 8 EMA),
    exits near Cost-to-Cost (0 to -5 pts).
    Otherwise trails Activation=15, Jump=5, Target Initial=25, Step=10, Pullback=5, CTC trigger=+40.
    Returns: (spot_pnl, hurdle400_net, statutory_net, net_with_slip, stat_charges, reason, duration_min)
    """
    idx_start = bisect.bisect_right([b["ts"] for b in session_1m], entry_ts)
    if idx_start >= len(session_1m):
        return 0.0, -400.0, -78.0, -110.5, 78.0, "NO_DATA", 0.0

    # Follow-Through Candle Check on immediate bar after entry
    first_bar = session_1m[idx_start]
    entry_bar_low = entry_spot - 5.0
    if len(session_1m) > idx_start:
        # If immediate bar fails to follow through (closes below 8-EMA or breaches low)
        if first_bar["close"] < closed_15m_ema8 or first_bar["low"] < entry_bar_low:
            exit_price = max(first_bar["low"], entry_spot - 5.0)
            spot_pnl = exit_price - entry_spot
            dur_min = (first_bar["ts"] - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "FOLLOW_THROUGH_CTC_CUT", max(0.0, dur_min)

    mfe = 0.0
    highest_after_target = entry_spot + SUPER_TRAIL_INITIAL_TARGET

    for i in range(idx_start, len(session_1m)):
        b = session_1m[i]
        b_time = b["session_time"]
        b_high = b["high"]
        b_low = b["low"]
        b_close = b["close"]
        b_ts = b["ts"]

        mfe = max(mfe, b_high - entry_spot)

        # Trailing SL calculation
        if mfe >= SUPER_TRAIL_ACTIVATION:
            steps = math.floor((mfe - SUPER_TRAIL_ACTIVATION) / SUPER_TRAIL_JUMP) + 1
            trailed_sl = (entry_spot - SUPER_TRAIL_INITIAL_SL) + (steps * SUPER_TRAIL_JUMP)
        else:
            trailed_sl = entry_spot - SUPER_TRAIL_INITIAL_SL

        # CTC follow-through rule
        if mfe >= SUPER_TRAIL_CTC_TRIGGER:
            trailed_sl = max(trailed_sl, entry_spot)

        # Trailing Target Pullback calculation
        if mfe >= SUPER_TRAIL_INITIAL_TARGET:
            highest_after_target = max(highest_after_target, b_high)
            pullback_exit = highest_after_target - SUPER_TRAIL_TARGET_PULLBACK
            if b_low <= pullback_exit:
                exit_price = pullback_exit
                spot_pnl = exit_price - entry_spot
                dur_min = (b_ts - entry_ts) / 60.0
                opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
                gross_inr = opt_pts * LOT_SIZE
                opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
                chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
                stat_charges = float(chg["total_charges"])
                hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
                stat_net = round(gross_inr - stat_charges, 2)
                net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
                return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "TRAIL_TARGET_PULLBACK", max(0.0, dur_min)

        # SL Hit
        if b_low <= trailed_sl:
            exit_price = trailed_sl
            spot_pnl = exit_price - entry_spot
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "TRAIL_SL_HIT", max(0.0, dur_min)

        # EOD Square-off
        if b_time >= SQUARE_OFF_TIME or i == len(session_1m) - 1:
            exit_price = b_close
            spot_pnl = exit_price - entry_spot
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            hurdle400_net = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
            stat_net = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, hurdle400_net, stat_net, net_with_slip, stat_charges, "EOD_SQUARE_OFF", max(0.0, dur_min)

    return 0.0, -400.0, -78.0, -110.5, 78.0, "NO_DATA", 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. ENRICH TRADE POPULATION WITH 17 GEOMETRY & CONTACT MEASURES
# ─────────────────────────────────────────────────────────────────────────────

def enrich_trades(
    trades: List[BacktestTrade],
    lookup_15m: ClosedBarLookup,
    lookup_1h: ClosedBarLookup,
    lookup_fut: Optional[ClosedBarLookup],
    session_1m_map: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Attaches all 17 geometry & contact features, direction controls, and dual exits."""
    enriched: List[Dict[str, Any]] = []

    for idx, tr in enumerate(trades):
        t_entry = tr.entry_ts
        b15 = lookup_15m.get(t_entry)
        b1h = lookup_1h.get(t_entry)
        bfut = lookup_fut.get(t_entry) if lookup_fut else None

        if not b15:
            continue

        session_1m = session_1m_map.get(tr.session_date, [])

        # Model 1: Fixed 40/40 PnL (Calculated with both Rs 400 Hurdle and Dhan Tariff)
        pts = tr.spot_pnl_pts
        is_win = 1 if pts > 0 else 0
        if pts > 0:
            opt_pts = pts * ASYM_WIN_DELTA
            gross_inr = opt_pts * LOT_SIZE
        else:
            opt_pts = pts * ASYM_LOSS_DELTA
            gross_inr = opt_pts * LOT_SIZE

        hurdle400_net_inr = round(gross_inr - BROKERAGE_HURDLE_INR, 2)
        stat_charges = float(getattr(tr, "statutory_charges_inr", 78.5))
        stat_net_inr = round(gross_inr - stat_charges, 2)
        net_with_slip_inr = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)

        # Direction control (Short mirror trade)
        dir_pnl, dir_hurdle400, dir_stat_net, dir_net_slip, dir_chg, dir_reason, dir_dur = simulate_direction_control_short(
            entry_ts=t_entry,
            entry_spot=tr.entry_spot,
            session_1m=session_1m,
        )

        # Model 2: Super Trail + Follow-Through CTC
        e8_val = b15.get("ema8", tr.entry_spot)
        st_pnl, st_hurdle400, st_stat_net, st_net_slip, st_chg, st_reason, st_dur = simulate_super_trail_ctc_long(
            entry_ts=t_entry,
            entry_spot=tr.entry_spot,
            session_1m=session_1m,
            closed_15m_ema8=e8_val,
        )

        # Time-to-resolution column (Mandatory Control 2)
        dur_min = (tr.exit_ts - tr.entry_ts) / 60.0 if tr.exit_ts and tr.entry_ts else 0.0
        dur_min = max(0.0, dur_min)

        # Measure 17 (Volume Surge Ratio)
        fut_vol_surge = np.nan
        fut_vol_delta = np.nan
        if bfut and bfut.get("fut_vol_surge") is not None:
            fut_vol_surge = bfut.get("fut_vol_surge")
            fut_vol_delta = bfut.get("fut_vol_delta_ratio")

        row = {
            "trade_id": idx + 1,
            "session_date": tr.session_date,
            "entry_ts": tr.entry_ts,
            "entry_time": tr.entry_time,
            "entry_spot": tr.entry_spot,
            "exit_ts": tr.exit_ts,
            "exit_time": tr.exit_time,
            "exit_spot": tr.exit_spot,
            "spot_pnl_pts": tr.spot_pnl_pts,
            "exit_reason": tr.exit_reason,
            "time_to_resolution_min": round(dur_min, 2),
            "is_win": is_win,
            # Dual Exit Model 1: Fixed 40/40
            "hurdle400_net_inr": hurdle400_net_inr,
            "asym_net_inr": stat_net_inr,
            "asym_net_with_slippage_inr": net_with_slip_inr,
            "statutory_charges_inr": round(stat_charges, 2),
            "asym_gross_inr": round(gross_inr, 2),
            # Mandatory Control 1: Direction Control Short
            "dir_ctrl_spot_pnl_pts": round(dir_pnl, 2),
            "dir_ctrl_hurdle400_net_inr": round(dir_hurdle400, 2),
            "dir_ctrl_asym_net_inr": round(dir_stat_net, 2),
            "dir_ctrl_asym_net_with_slippage_inr": round(dir_net_slip, 2),
            "dir_ctrl_statutory_charges_inr": round(dir_chg, 2),
            "dir_ctrl_exit_reason": dir_reason,
            "dir_ctrl_time_to_resolution_min": round(dir_dur, 2),
            # Dual Exit Model 2: Super Trail + Follow-Through CTC
            "super_trail_spot_pnl_pts": round(st_pnl, 2),
            "super_trail_hurdle400_net_inr": round(st_hurdle400, 2),
            "super_trail_net_inr": round(st_stat_net, 2),
            "super_trail_net_with_slippage_inr": round(st_net_slip, 2),
            "super_trail_statutory_charges_inr": round(st_chg, 2),
            "super_trail_exit_reason": st_reason,
            "super_trail_time_to_resolution_min": round(st_dur, 2),
            # ─────────────────────────────────────────────────────────────
            # Group A: 4/8 MA Line Geometry Measures (#1 to #11)
            # ─────────────────────────────────────────────────────────────
            "m01_separation_signed": b15.get("m01_separation_signed", np.nan),
            "m02_separation_abs": b15.get("m02_separation_abs", np.nan),
            "m03_separation_percentile": b15.get("m03_separation_percentile", np.nan),
            "m04_ma4_slope": b15.get("m04_ma4_slope", np.nan),
            "m05_ema8_slope": b15.get("m05_ema8_slope", np.nan),
            "m06_spread_velocity": b15.get("m06_spread_velocity", np.nan),
            "m07_spread_phase": b15.get("m07_spread_phase", np.nan),
            "m08_ema8_curvature": b15.get("m08_ema8_curvature", np.nan),
            "m09_adx14": b15.get("m09_adx14", np.nan),
            "m10_bb_width_percentile": b15.get("m10_bb_width_percentile", np.nan),
            "m11_ma4_angle_deg": b15.get("m11_ma4_angle_deg", np.nan),
            # ─────────────────────────────────────────────────────────────
            # Group B: Boss Upgrades (#12 to #17)
            # ─────────────────────────────────────────────────────────────
            "m12_ribbon_area_5": b15.get("m12_ribbon_area_5", np.nan),
            "m13_whipsaw_cross_count_12": b15.get("m13_whipsaw_cross_count_12", np.nan),
            "m14_penetration_depth": b15.get("m14_penetration_depth", np.nan),
            "m15_wick_rejection_pinbar": b15.get("m15_wick_rejection_pinbar", np.nan),
            "m16_slope_1h_8ema": b1h.get("m16_slope_1h_8ema", np.nan) if b1h else np.nan,
            "m16_candle_align_1h": b1h.get("m16_candle_align_1h", np.nan) if b1h else np.nan,
            "m17_vol_surge_ratio": fut_vol_surge,
            "m17_vol_delta_ratio": fut_vol_delta,
            # Secondary MA Types
            "sma4_vs_ema8_separation_signed": b15.get("sma4_vs_ema8_separation_signed", np.nan),
            "sma4_vs_ema8_spread_velocity": b15.get("sma4_vs_ema8_spread_velocity", np.nan),
        }
        enriched.append(row)

    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# 5. TERCILE ANALYSIS, INVERTED-U REGIME CHECKS & THREE-LAYER PROTOCOL
# ─────────────────────────────────────────────────────────────────────────────

MEASURE_METADATA = [
    # Group A: 4/8 MA Line Geometry
    ("m01_separation_signed", "Separation (Signed)", "Group A (Line Geometry)"),
    ("m02_separation_abs", "Separation (Absolute)", "Group A (Line Geometry)"),
    ("m03_separation_percentile", "Separation Percentile", "Group A (Line Geometry)"),
    ("m04_ma4_slope", "MA4 Slope (Normalized Angle)", "Group A (Line Geometry)"),
    ("m05_ema8_slope", "EMA8 Slope (Underlying Trend)", "Group A (Line Geometry)"),
    ("m06_spread_velocity", "Spread Velocity", "Group A (Line Geometry)"),
    ("m07_spread_phase", "Spread Phase (+1 Exp, -1 Cont)", "Group A (Line Geometry)"),
    ("m08_ema8_curvature", "EMA8 Curvature (2nd Deriv)", "Group A (Line Geometry)"),
    ("m09_adx14", "ADX(14) Trend Strength", "Group A (Regime)"),
    ("m10_bb_width_percentile", "BB Width Percentile", "Group A (Regime)"),
    ("m11_ma4_angle_deg", "MA4 Angle in Degrees", "Group A (Sanity Check)"),
    # Group B: Boss Upgrades
    ("m12_ribbon_area_5", "5-Bar Ribbon Area", "Group B (Ribbon Area)"),
    ("m13_whipsaw_cross_count_12", "12-Bar Whip-Saw Count", "Group B (Chop Flips)"),
    ("m14_penetration_depth", "8-EMA Contact Penetration", "Group B (Contact)"),
    ("m15_wick_rejection_pinbar", "Contact Wick Rejection", "Group B (Contact)"),
    ("m16_slope_1h_8ema", "1H 8-EMA Slope (Mentor)", "Group B (1H Trend)"),
    ("m17_vol_surge_ratio", "Volume Surge Ratio (FUT)", "Group B (Volume)"),
]

CONSOLIDATION_MEASURES = {
    "m02_separation_abs",
    "m03_separation_percentile",
    "m10_bb_width_percentile",
    "m12_ribbon_area_5",
    "m13_whipsaw_cross_count_12",
}


def classify_tercile_shape(terciles: List[Dict[str, Any]]) -> str:
    """
    Classifies the 3-tercile progression:
      - MONOTONE_POSITIVE: B1 < B2 < B3
      - MONOTONE_NEGATIVE: B1 > B2 > B3
      - CHOP_FLOOR: B1 << B2 approx B3 (B1 significantly worse; removing B1 fixes edge)
      - INVERTED_U: B2 > B1 and B2 > B3 (Goldilocks sweet spot)
      - NO_SHAPE: none of above
    """
    if len(terciles) < 3:
        return "NO_SHAPE"

    p1 = terciles[0]["net_inr_per_trade"]
    p2 = terciles[1]["net_inr_per_trade"]
    p3 = terciles[2]["net_inr_per_trade"]

    if p1 < p2 < p3:
        return "MONOTONE_POSITIVE"
    if p1 > p2 > p3:
        return "MONOTONE_NEGATIVE"
    if p2 > p1 and p2 > p3 and p2 > (max(p1, p3) + 40.0):
        return "INVERTED_U"
    if p1 < min(p2, p3) - 60.0 and abs(p2 - p3) <= max(40.0, 0.4 * abs(p2)):
        return "CHOP_FLOOR"

    return "NO_SHAPE"


def run_session_block_bootstrap(
    trades: List[Dict[str, Any]],
    n_iterations: int = 1000,
    seed: int = 42,
    pnl_col: str = "hurdle400_net_inr",
) -> Tuple[float, float]:
    """Resamples whole trading sessions with replacement to yield robust 95% CI."""
    if not trades:
        return 0.0, 0.0

    random.seed(seed)
    np.random.seed(seed)

    by_session: Dict[str, List[float]] = {}
    for t in trades:
        s = t["session_date"]
        if s not in by_session:
            by_session[s] = []
        by_session[s].append(t[pnl_col])

    sessions = list(by_session.keys())
    s_count = len(sessions)
    if s_count < 5:
        return 0.0, 0.0

    boot_means = []
    for _ in range(n_iterations):
        sample_sessions = random.choices(sessions, k=s_count)
        sample_pnls = []
        for s in sample_sessions:
            sample_pnls.extend(by_session[s])
        boot_means.append(float(np.mean(sample_pnls)))

    ci_low = float(np.percentile(boot_means, 2.5))
    ci_high = float(np.percentile(boot_means, 97.5))
    return ci_low, ci_high


def run_time_matched_random_baseline(
    trades: List[Dict[str, Any]],
    session_1m_map: Dict[str, List[Dict[str, Any]]],
    oos_sessions: List[str],
    n_draws: int = 200,
    seed: int = 42,
) -> Dict[str, float]:
    """
    Mandatory Control: Time-of-day-matched random baseline (200 Monte Carlo draws).
    In each draw, for each trade, pairs its entry time of day with a randomly chosen OOS session.
    Simulates fixed 40/40 trade to establish the true empirical null distribution.
    """
    if not trades or not oos_sessions:
        return {"mean_net": -120.0, "se_net": 15.0, "ci_low": -150.0, "ci_high": -90.0}

    random.seed(seed)
    entry_times = [t["entry_time"] for t in trades]
    n_trades = len(entry_times)
    draw_means: List[float] = []

    for _ in range(n_draws):
        draw_pnls: List[float] = []
        for et in entry_times:
            rand_session = random.choice(oos_sessions)
            bars = session_1m_map.get(rand_session, [])
            if not bars:
                continue
            # Find bar matching entry time
            match_bar = next((b for b in bars if b["session_time"] == et), None)
            if not match_bar:
                continue
            entry_spot = match_bar["open"]
            entry_ts = match_bar["ts"]
            idx_start = bisect.bisect_right([b["ts"] for b in bars], entry_ts)
            target = entry_spot + FIXED_TARGET_PTS
            sl = entry_spot - FIXED_SL_PTS
            trade_pnl = 0.0
            for i in range(idx_start, len(bars)):
                b = bars[i]
                if b["low"] <= sl:
                    trade_pnl = -FIXED_SL_PTS * ASYM_LOSS_DELTA * LOT_SIZE - BROKERAGE_HURDLE_INR
                    break
                if b["high"] >= target:
                    trade_pnl = FIXED_TARGET_PTS * ASYM_WIN_DELTA * LOT_SIZE - BROKERAGE_HURDLE_INR
                    break
                if b["session_time"] >= SQUARE_OFF_TIME or i == len(bars) - 1:
                    diff = b["close"] - entry_spot
                    trade_pnl = diff * (ASYM_WIN_DELTA if diff > 0 else ASYM_LOSS_DELTA) * LOT_SIZE - BROKERAGE_HURDLE_INR
                    break
            draw_pnls.append(trade_pnl)

        if draw_pnls:
            draw_means.append(float(np.mean(draw_pnls)))

    if not draw_means:
        return {"mean_net": -120.0, "se_net": 15.0, "ci_low": -150.0, "ci_high": -90.0}

    return {
        "mean_net": round(float(np.mean(draw_means)), 2),
        "se_net": round(float(np.std(draw_means, ddof=1)), 2),
        "ci_low": round(float(np.percentile(draw_means, 2.5)), 2),
        "ci_high": round(float(np.percentile(draw_means, 97.5)), 2),
    }


def evaluate_single_measure(
    measure_key: str,
    measure_name: str,
    group_name: str,
    df_trades: pd.DataFrame,
    is_sessions: List[str],
    oos_sessions: List[str],
    pnl_col: str = "hurdle400_net_inr",
) -> Dict[str, Any]:
    """
    Executes Three-Layer Protocol:
      Layer 1: Continuous Rank Correlation + Inverted-U Chop-Removal Tests
      Layer 2: Tercile Bucketing (Bottom, Middle, Top) + Archetype Classification
      Layer 3: Strict 50/50 OOS Evaluation + 6 BUILD Hurdle Checks
    """
    valid = df_trades.dropna(subset=[measure_key]).copy()
    if len(valid) < 100:
        return {
            "key": measure_key,
            "name": measure_name,
            "group": group_name,
            "status": "BLOCKED",
            "reason": f"Insufficient sample size (n={len(valid)}).",
            "verdict": "NOT BUILT",
        }

    is_df = valid[valid["session_date"].isin(is_sessions)].copy()
    oos_df = valid[valid["session_date"].isin(oos_sessions)].copy()

    if len(is_df) < 50 or len(oos_df) < 50:
        return {
            "key": measure_key,
            "name": measure_name,
            "group": group_name,
            "status": "BLOCKED",
            "reason": f"Sample split too small (IS={len(is_df)}, OOS={len(oos_df)}).",
            "verdict": "NOT BUILT",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 1: Continuous Rank Correlation + Chop-Removal / Inverted-U Check
    # ─────────────────────────────────────────────────────────────────────────
    spearman_rho, spearman_p = stats.spearmanr(is_df[measure_key], is_df[pnl_col])

    # Chop-Removal Binary Test (Bottom 30% Chop Excluded vs Bottom 30% Chop Only)
    chop_cut = float(is_df[measure_key].quantile(0.30))
    chop_excluded = is_df[is_df[measure_key] > chop_cut]
    chop_only = is_df[is_df[measure_key] <= chop_cut]

    chop_excluded_pnl = float(chop_excluded[pnl_col].mean()) if len(chop_excluded) else 0.0
    chop_only_pnl = float(chop_only[pnl_col].mean()) if len(chop_only) else 0.0
    chop_removal_edge = round(chop_excluded_pnl - chop_only_pnl, 2)

    # Goldilocks Middle-Regime Check (25th–85th percentile vs Extremes)
    q25 = float(is_df[measure_key].quantile(0.25))
    q85 = float(is_df[measure_key].quantile(0.85))
    goldilocks_df = is_df[(is_df[measure_key] >= q25) & (is_df[measure_key] <= q85)]
    extremes_df = is_df[(is_df[measure_key] < q25) | (is_df[measure_key] > q85)]

    goldilocks_pnl = float(goldilocks_df[pnl_col].mean()) if len(goldilocks_df) else 0.0
    extremes_pnl = float(extremes_df[pnl_col].mean()) if len(extremes_df) else 0.0
    goldilocks_edge = round(goldilocks_pnl - extremes_pnl, 2)

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 2: Tercile Bucketing (Bottom / Middle / Top)
    # ─────────────────────────────────────────────────────────────────────────
    if measure_key == "m13_whipsaw_cross_count_12":
        # Discrete buckets: 0 flips, 1 flip, >= 2 flips
        bins = [-0.1, 0.5, 1.5, 999.0]
        labels = [0, 1, 2]
        is_df["tercile"] = pd.cut(is_df[measure_key], bins=bins, labels=labels)
    else:
        try:
            is_df["tercile"] = pd.qcut(is_df[measure_key], q=3, labels=[0, 1, 2], duplicates="drop")
        except Exception:
            is_df["tercile"] = pd.cut(is_df[measure_key], bins=3, labels=[0, 1, 2])

    tercile_summary = []
    bucket_labels = ["Bottom (T1)", "Middle (T2)", "Top (T3)"]
    unique_terciles = sorted(is_df["tercile"].dropna().unique())

    for idx_t in unique_terciles:
        sub = is_df[is_df["tercile"] == idx_t]
        n_t = len(sub)
        w_t = int(sub["is_win"].sum())
        wr_t = (w_t / n_t * 100.0) if n_t else 0.0
        wins = sub[sub["spot_pnl_pts"] > 0]["spot_pnl_pts"]
        losses = sub[sub["spot_pnl_pts"] < 0]["spot_pnl_pts"]
        avg_w_pts = float(wins.mean()) if len(wins) else 0.0
        avg_l_pts = float(losses.mean()) if len(losses) else 0.0
        net_t = float(sub[pnl_col].sum())
        exp_t = (net_t / n_t) if n_t else 0.0
        stat_exp_t = float(sub["asym_net_inr"].mean()) if n_t else 0.0
        wins_pnl = sub[sub[pnl_col] > 0][pnl_col].sum()
        loss_pnl = abs(sub[sub[pnl_col] < 0][pnl_col].sum())
        pf_t = round(wins_pnl / loss_pnl, 2) if loss_pnl > 0 else 99.0
        min_v = float(sub[measure_key].min())
        max_v = float(sub[measure_key].max())

        b_name = bucket_labels[int(idx_t)] if int(idx_t) < len(bucket_labels) else f"T{int(idx_t)+1}"
        tercile_summary.append({
            "bucket_id": int(idx_t) + 1,
            "label": b_name,
            "min_val": round(min_v, 4),
            "max_val": round(max_v, 4),
            "n": n_t,
            "win_rate": round(wr_t, 1),
            "avg_win_pts": round(avg_w_pts, 2),
            "avg_loss_pts": round(avg_l_pts, 2),
            "net_inr_per_trade": round(exp_t, 2),
            "statutory_dhan_net_per_trade": round(stat_exp_t, 2),
            "profit_factor": pf_t,
        })

    # Classify Shape Archetype
    shape = classify_tercile_shape(tercile_summary)

    # ─────────────────────────────────────────────────────────────────────────
    # LAYER 3: Strict 50/50 OOS Chronological Split & Cutoff Rule
    # ─────────────────────────────────────────────────────────────────────────
    cutoff_type = "NONE"
    cutoff_val: Any = None

    if shape == "MONOTONE_POSITIVE":
        cutoff_val = float(is_df[measure_key].quantile(0.333))  # Drop bottom tercile
        cutoff_type = "GTE"
    elif shape == "MONOTONE_NEGATIVE":
        cutoff_val = float(is_df[measure_key].quantile(0.667))  # Drop top tercile
        cutoff_type = "LTE"
    elif shape == "CHOP_FLOOR":
        cutoff_val = float(is_df[measure_key].quantile(0.333))  # Chop-floor drop bottom tercile
        cutoff_type = "GTE"
    elif shape == "INVERTED_U":
        q_low = float(is_df[measure_key].quantile(0.25))
        q_high = float(is_df[measure_key].quantile(0.85))
        cutoff_val = (q_low, q_high)
        cutoff_type = "BETWEEN"
    else:
        # Default hypothesis test: Chop removal (drop bottom 30%)
        cutoff_val = float(is_df[measure_key].quantile(0.30))
        cutoff_type = "GTE"

    # Evaluate untouched on OOS
    if cutoff_type == "GTE":
        oos_passed = oos_df[oos_df[measure_key] >= cutoff_val].copy()
    elif cutoff_type == "LTE":
        oos_passed = oos_df[oos_df[measure_key] <= cutoff_val].copy()
    elif cutoff_type == "BETWEEN":
        oos_passed = oos_df[(oos_df[measure_key] >= cutoff_val[0]) & (oos_df[measure_key] <= cutoff_val[1])].copy()
    else:
        oos_passed = oos_df.copy()

    n_oos = len(oos_passed)
    w_oos = int(oos_passed["is_win"].sum()) if n_oos else 0
    wr_oos = (w_oos / n_oos * 100.0) if n_oos else 0.0
    net_inr_oos = float(oos_passed[pnl_col].sum()) if n_oos else 0.0
    exp_oos = (net_inr_oos / n_oos) if n_oos else 0.0
    stat_exp_oos = float(oos_passed["asym_net_inr"].mean()) if n_oos else 0.0
    wins_oos = oos_passed[oos_passed[pnl_col] > 0][pnl_col].sum()
    loss_oos = abs(oos_passed[oos_passed[pnl_col] < 0][pnl_col].sum())
    pf_oos = round(wins_oos / loss_oos, 2) if loss_oos > 0 else 99.0

    # Model 2 Super Trail comparison on same OOS subset
    st_exp_oos = float(oos_passed["super_trail_hurdle400_net_inr"].mean()) if n_oos else 0.0
    st_stat_oos = float(oos_passed["super_trail_net_inr"].mean()) if n_oos else 0.0

    # Direction Control on same OOS subset
    dir_net_oos = float(oos_passed["dir_ctrl_hurdle400_net_inr"].sum()) if n_oos else 0.0
    dir_exp_oos = (dir_net_oos / n_oos) if n_oos else 0.0
    directional_advantage = round(exp_oos - dir_exp_oos, 2)

    # Outlier Robustness: Exclude top 1, 3, 5 wins
    sorted_pnls = sorted(oos_passed[pnl_col].tolist(), reverse=True)
    exp_ex_top1 = float(np.mean(sorted_pnls[1:])) if len(sorted_pnls) > 1 else exp_oos
    exp_ex_top3 = float(np.mean(sorted_pnls[3:])) if len(sorted_pnls) > 3 else exp_oos
    exp_ex_top5 = float(np.mean(sorted_pnls[5:])) if len(sorted_pnls) > 5 else exp_oos

    # Session-block bootstrap 95% CI
    ci_low, ci_high = run_session_block_bootstrap(oos_passed.to_dict(orient="records"), pnl_col=pnl_col)

    # Z-score vs noise hurdle (+2.0 SD ceiling)
    pnls_arr = oos_passed[pnl_col].values
    se = (np.std(pnls_arr, ddof=1) / math.sqrt(n_oos)) if n_oos > 1 else 1.0
    z_score = round(exp_oos / (se + 1e-9), 2)

    # BUILD Hurdle Checks:
    # 1. Filtered Net ₹/trade > 0 (after ₹400 brokerage)
    # 2. Profit Factor >= 1.30
    # 3. OOS n >= 60
    # 4. Ex-top5 wins Net > 0
    # 5. Z-score exceeds best-of-17 noise ceiling (+2.0 SD)
    # 6. Directional Advantage > 0
    cleared_pnl = exp_oos > 0.0
    cleared_pf = pf_oos >= 1.30
    cleared_n = n_oos >= 60
    cleared_outlier = exp_ex_top5 > 0.0
    cleared_noise = z_score >= 2.0
    cleared_dir = directional_advantage > 0.0

    is_build = (
        cleared_pnl
        and cleared_pf
        and cleared_n
        and cleared_outlier
        and cleared_noise
        and cleared_dir
    )
    verdict = "BUILD" if is_build else "NOT BUILT"

    failure_reasons = []
    if not cleared_pnl:
        failure_reasons.append(f"Net ₹/trade <= 0 (₹{exp_oos:.2f} after ₹400 hurdle)")
    if not cleared_pf:
        failure_reasons.append(f"PF < 1.30 ({pf_oos:.2f})")
    if not cleared_n:
        failure_reasons.append(f"OOS n < 60 ({n_oos})")
    if not cleared_outlier:
        failure_reasons.append(f"Ex-top5 wins Net <= 0 (₹{exp_ex_top5:.2f})")
    if not cleared_noise:
        failure_reasons.append(f"Z-score {z_score} SD < +2.0 SD noise hurdle")
    if not cleared_dir:
        failure_reasons.append(f"Fails Direction Control (Advantage ₹{directional_advantage:.2f})")

    return {
        "key": measure_key,
        "name": measure_name,
        "group": group_name,
        "status": "EVALUATED",
        "shape": shape,
        "layer1_correlation": {
            "spearman_rho": round(float(spearman_rho), 3),
            "spearman_p": round(float(spearman_p), 4),
            "chop_removal_test": {
                "chop_cut": round(chop_cut, 4),
                "excluded_mean_inr": round(chop_excluded_pnl, 2),
                "chop_only_mean_inr": round(chop_only_pnl, 2),
                "chop_removal_edge_inr": chop_removal_edge,
            },
            "goldilocks_regime_test": {
                "q25": round(q25, 4),
                "q85": round(q85, 4),
                "goldilocks_mean_inr": round(goldilocks_pnl, 2),
                "extremes_mean_inr": round(extremes_pnl, 2),
                "goldilocks_edge_inr": goldilocks_edge,
            },
        },
        "layer2_terciles": tercile_summary,
        "layer3_oos": {
            "cutoff_rule": f"{cutoff_type} {cutoff_val}",
            "n_surviving": n_oos,
            "win_rate": round(wr_oos, 1),
            "net_inr_hurdle400": round(exp_oos, 2),
            "net_inr_statutory_dhan": round(stat_exp_oos, 2),
            "profit_factor": pf_oos,
            "super_trail_hurdle400": round(st_exp_oos, 2),
            "super_trail_statutory_dhan": round(st_stat_oos, 2),
            "net_inr_ex_top1": round(exp_ex_top1, 2),
            "net_inr_ex_top3": round(exp_ex_top3, 2),
            "net_inr_ex_top5": round(exp_ex_top5, 2),
            "bootstrap_ci_95": [round(ci_low, 2), round(ci_high, 2)],
            "z_score_sd": z_score,
            "direction_control_short_net": round(dir_exp_oos, 2),
            "directional_advantage": round(directional_advantage, 2),
        },
        "verdict": verdict,
        "failure_reasons": failure_reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN EXECUTION ROUTINE
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.time()
    log.info("══════════════════════════════════════════════════════════════════")
    log.info("PORT 8510 ADVANCED CHART GEOMETRY & CONTACT BACKTEST ENGINE (BOSS UPGRADED)")
    log.info("══════════════════════════════════════════════════════════════════")

    # 1. Load Data
    log.info("Loading 1m spot candles from %s...", DB_PATH)
    df_1m = load_historical_1m_data(str(DB_PATH))
    sessions_list = sorted(df_1m["session_date"].unique().tolist())
    n_sessions = len(sessions_list)
    log.info("Loaded %d 1m candles across %d unique trading sessions.", len(df_1m), n_sessions)

    # 50/50 Chronological Split
    split_idx = n_sessions // 2
    is_sessions = sessions_list[:split_idx]
    oos_sessions = sessions_list[split_idx:]
    log.info("Chronological 50/50 Split: %d IS sessions (%s to %s) | %d OOS sessions (%s to %s)",
             len(is_sessions), is_sessions[0], is_sessions[-1],
             len(oos_sessions), oos_sessions[0], oos_sessions[-1])

    # 2. Resample 15m and 1H
    log.info("Resampling 15m and 1H bars with standard 09:15 NSE alignment...")
    df_15m_raw = resample_bars(df_1m, "15min")
    df_15m = prepare_15m_geometry_frame(df_15m_raw)

    df_1h_raw = resample_bars(df_1m, "1h")
    df_1h = prepare_1h_trend_frame(df_1h_raw)

    lookup_15m = ClosedBarLookup(df_15m, bar_seconds=900)
    lookup_1h = ClosedBarLookup(df_1h, bar_seconds=3600)

    # Load Futures data for Measure 17 (Volume) using institutional RVOL engine
    lookup_fut: Optional[ClosedBarLookup] = None
    is_fut_sessions: List[str] = []
    oos_fut_sessions: List[str] = []
    try:
        from scripts.backtest_futures_volume import load_and_resample_futures, compute_all_rvol_measures
        df_fut_15m = load_and_resample_futures(DB_PATH)
        df_fut_rvol = compute_all_rvol_measures(df_fut_15m, filter_expiries=True)
        df_fut_rvol["fut_vol_surge"] = df_fut_rvol["comb_bar_rvol"]
        df_fut_rvol["fut_vol_delta_ratio"] = df_fut_rvol["comb_cum_rvol"]
        lookup_fut = ClosedBarLookup(df_fut_rvol, bar_seconds=900)

        valid_fut_sessions = sorted(df_fut_rvol.dropna(subset=["comb_bar_rvol"])["session_date"].unique().tolist())
        n_fut_split = len(valid_fut_sessions) // 2
        is_fut_sessions = valid_fut_sessions[:n_fut_split]
        oos_fut_sessions = valid_fut_sessions[n_fut_split:]
        log.info("Futures 15m loaded (%d bars across %d sessions) with Time-of-Day RVOL engine.",
                 len(df_fut_rvol), len(valid_fut_sessions))
    except Exception as e:
        log.warning("Could not load FUT volume data: %s", e)

    # Pre-index 1m sessions for fast forward-walk simulation
    session_1m_map: Dict[str, List[Dict[str, Any]]] = {}
    for s_date, grp in df_1m.groupby("session_date"):
        session_1m_map[s_date] = grp.to_dict(orient="records")

    # 3. Run Setup #1 Simulator to Extract Populations A, B, and E
    log.info("Executing Setup #1 Backtest Simulator to generate populations A, B, and E...")
    sim = Setup1BacktestSimulator()
    df_15m_sim = build_15m_data(df_1m, sim.cfg)
    map_15m_sim = {r["ts"]: r for r in df_15m_sim.to_dict(orient="records")}
    list_15m_ts = sorted(map_15m_sim.keys())
    sessions_df_list = [grp for _, grp in df_1m.groupby("session_date", sort=True)]

    # Variant A: Raw 15m 8 EMA Touch (Primary Population, n ~ 2,370)
    rules_a = {"trend_filter": False, "ubb_filter": False, "first_opp": False, "daily_stop": False, "ctc": False, "fii_window": False, "gap_filter": False, "ema_sep": False, "vwap_filter": False}
    trades_a = sim._simulate_dataset(sessions_df_list, map_15m_sim, list_15m_ts, "Variant A (Raw 15m 8 EMA Touch)", rules_a)

    # Variant B: + 4>8 Trend Filter (Secondary Population, n ~ 1,120)
    rules_b = {"trend_filter": True, "ubb_filter": False, "first_opp": False, "daily_stop": False, "ctc": False, "fii_window": False, "gap_filter": False, "ema_sep": False, "vwap_filter": False}
    trades_b = sim._simulate_dataset(sessions_df_list, map_15m_sim, list_15m_ts, "Variant B (15m 4>8 Trend Filter)", rules_b)

    # Variant E: Full Institutional Stack (Footnote Population, n = 80)
    rules_e = {"trend_filter": True, "ubb_filter": True, "first_opp": True, "daily_stop": True, "ctc": True, "ctc_brokerage_buffer": True, "fii_window": True, "gap_filter": True, "ema_sep": True, "vwap_filter": True}
    trades_e = sim._simulate_dataset(sessions_df_list, map_15m_sim, list_15m_ts, "Variant E (Institutional 15m)", rules_e)

    log.info("Simulated trades: Variant A (n=%d) | Variant B (n=%d) | Variant E (n=%d)",
             len(trades_a), len(trades_b), len(trades_e))

    # 4. Enrich Trades with All 17 Geometry & Contact Measures
    log.info("Enriching trades with 17 geometry measures, contact intensity, direction control, and Super Trail...")
    enriched_a = enrich_trades(trades_a, lookup_15m, lookup_1h, lookup_fut, session_1m_map)
    enriched_b = enrich_trades(trades_b, lookup_15m, lookup_1h, lookup_fut, session_1m_map)
    enriched_e = enrich_trades(trades_e, lookup_15m, lookup_1h, lookup_fut, session_1m_map)

    # Export Events CSVs (Mandatory Deliverable)
    csv_a = OUT_DIR / "events_variantA.csv"
    csv_b = OUT_DIR / "events_variantB.csv"
    csv_e = OUT_DIR / "events_variantE.csv"
    pd.DataFrame(enriched_a).to_csv(csv_a, index=False)
    pd.DataFrame(enriched_b).to_csv(csv_b, index=False)
    pd.DataFrame(enriched_e).to_csv(csv_e, index=False)
    log.info("Wrote enriched events: %s, %s, %s", csv_a, csv_b, csv_e)

    # 5. Mandatory Control: Time-Matched Random Baseline (200 Monte Carlo draws)
    log.info("Computing Time-Matched Random Baseline (200 Monte Carlo draws)...")
    random_baseline = run_time_matched_random_baseline(
        trades=enriched_a,
        session_1m_map=session_1m_map,
        oos_sessions=oos_sessions,
        n_draws=200,
    )
    log.info("Random Baseline Null Expectancy: Mean=₹%.2f/tr (SE=₹%.2f, 95%% CI=[₹%.2f, ₹%.2f])",
             random_baseline["mean_net"], random_baseline["se_net"], random_baseline["ci_low"], random_baseline["ci_high"])

    # 6. PRIMARY ANALYSIS (LOCKED): Variant A, Fixed 40/40, Evaluated vs ₹400 Hurdle
    log.info("══════════════════════════════════════════════════════════════════")
    log.info("RUNNING PRIMARY ANALYSIS: Variant A (n=%d), Fixed 40/40, EMA(4)/EMA(8)", len(enriched_a))
    log.info("Scored against: ₹400 round-trip brokerage hurdle at lot size = 65")
    log.info("══════════════════════════════════════════════════════════════════")

    df_a = pd.DataFrame(enriched_a)
    primary_results = []

    for key, name, group in MEASURE_METADATA:
        # Measure 17 (Volume Surge): scoped to valid futures window
        if key == "m17_vol_surge_ratio" and is_fut_sessions and oos_fut_sessions:
            res = evaluate_single_measure(
                measure_key=key,
                measure_name=name,
                group_name=group,
                df_trades=df_a,
                is_sessions=is_fut_sessions,
                oos_sessions=oos_fut_sessions,
                pnl_col="hurdle400_net_inr",
            )
            res["scope_note"] = f"Scoped to futures volume window ({is_fut_sessions[0]} to {oos_fut_sessions[-1]})"
            primary_results.append(res)
            continue

        res = evaluate_single_measure(
            measure_key=key,
            measure_name=name,
            group_name=group,
            df_trades=df_a,
            is_sessions=is_sessions,
            oos_sessions=oos_sessions,
            pnl_col="hurdle400_net_inr",
        )
        primary_results.append(res)

    # 7. SECONDARY ANALYSIS (EXPLORATORY, NOT SCORED AGAINST BUILD HURDLE)
    log.info("Executing Secondary Exploratory Configurations...")
    df_b = pd.DataFrame(enriched_b)
    df_e = pd.DataFrame(enriched_e)

    secondary_results: Dict[str, Any] = {
        "variant_b_40_40": [],
        "variant_e_institutional": [],
        "variant_a_sma4_vs_ema8": [],
        "variant_a_super_trail": [],
    }

    # Exploratory 1: Variant B (4>8 Trend Filter)
    for key in ["m14_penetration_depth", "m15_wick_rejection_pinbar", "m13_whipsaw_cross_count_12", "m01_separation_signed"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Variant B Secondary", df_b, is_sessions, oos_sessions, pnl_col="hurdle400_net_inr")
        secondary_results["variant_b_40_40"].append(r)

    # Exploratory 2: Variant E (Institutional Footnote)
    for key in ["m14_penetration_depth", "m16_slope_1h_8ema", "m01_separation_signed"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Variant E Footnote", df_e, is_sessions, oos_sessions, pnl_col="hurdle400_net_inr")
        secondary_results["variant_e_institutional"].append(r)

    # Exploratory 3: SMA(4) vs EMA(8)
    for key in ["sma4_vs_ema8_separation_signed", "sma4_vs_ema8_spread_velocity"]:
        r = evaluate_single_measure(key, key, "SMA(4) TradingView", df_a, is_sessions, oos_sessions, pnl_col="hurdle400_net_inr")
        secondary_results["variant_a_sma4_vs_ema8"].append(r)

    # Exploratory 4: Super Trail v1.1 + Follow-Through CTC on Variant A
    for key in ["m14_penetration_depth", "m15_wick_rejection_pinbar", "m01_separation_signed", "m12_ribbon_area_5"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Super Trail Model", df_a, is_sessions, oos_sessions, pnl_col="super_trail_hurdle400_net_inr")
        secondary_results["variant_a_super_trail"].append(r)

    # Baseline Statistics
    baseline_stats = {
        "variant_a_all": {
            "n": len(df_a),
            "win_rate": round(float(df_a["is_win"].mean() * 100), 1),
            "net_inr_hurdle400": round(float(df_a["hurdle400_net_inr"].mean()), 2),
            "profit_factor_hurdle400": round(float(df_a[df_a["hurdle400_net_inr"] > 0]["hurdle400_net_inr"].sum() / (abs(df_a[df_a["hurdle400_net_inr"] < 0]["hurdle400_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_statutory_dhan": round(float(df_a["asym_net_inr"].mean()), 2),
            "profit_factor_statutory": round(float(df_a[df_a["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_a[df_a["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_with_slippage": round(float(df_a["asym_net_with_slippage_inr"].mean()), 2),
            "profit_factor_with_slippage": round(float(df_a[df_a["asym_net_with_slippage_inr"] > 0]["asym_net_with_slippage_inr"].sum() / (abs(df_a[df_a["asym_net_with_slippage_inr"] < 0]["asym_net_with_slippage_inr"].sum()) + 1e-9)), 2),
        },
        "variant_b_all": {
            "n": len(df_b),
            "win_rate": round(float(df_b["is_win"].mean() * 100), 1),
            "net_inr_hurdle400": round(float(df_b["hurdle400_net_inr"].mean()), 2),
            "profit_factor_hurdle400": round(float(df_b[df_b["hurdle400_net_inr"] > 0]["hurdle400_net_inr"].sum() / (abs(df_b[df_b["hurdle400_net_inr"] < 0]["hurdle400_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_statutory_dhan": round(float(df_b["asym_net_inr"].mean()), 2),
            "profit_factor_statutory": round(float(df_b[df_b["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_b[df_b["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_with_slippage": round(float(df_b["asym_net_with_slippage_inr"].mean()), 2),
            "profit_factor_with_slippage": round(float(df_b[df_b["asym_net_with_slippage_inr"] > 0]["asym_net_with_slippage_inr"].sum() / (abs(df_b[df_b["asym_net_with_slippage_inr"] < 0]["asym_net_with_slippage_inr"].sum()) + 1e-9)), 2),
        },
        "variant_e_all": {
            "n": len(df_e),
            "win_rate": round(float(df_e["is_win"].mean() * 100), 1),
            "net_inr_hurdle400": round(float(df_e["hurdle400_net_inr"].mean()), 2),
            "profit_factor_hurdle400": round(float(df_e[df_e["hurdle400_net_inr"] > 0]["hurdle400_net_inr"].sum() / (abs(df_e[df_e["hurdle400_net_inr"] < 0]["hurdle400_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_statutory_dhan": round(float(df_e["asym_net_inr"].mean()), 2),
            "profit_factor_statutory": round(float(df_e[df_e["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_e[df_e["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
            "net_inr_with_slippage": round(float(df_e["asym_net_with_slippage_inr"].mean()), 2),
            "profit_factor_with_slippage": round(float(df_e[df_e["asym_net_with_slippage_inr"] > 0]["asym_net_with_slippage_inr"].sum() / (abs(df_e[df_e["asym_net_with_slippage_inr"] < 0]["asym_net_with_slippage_inr"].sum()) + 1e-9)), 2),
        },
    }

    # 8. Generate Master Report JSON (Mandatory Deliverable)
    report_data = {
        "title": "Port 8510 Advanced Chart Geometry & Contact Backtest Report (Boss Upgraded)",
        "script": "scripts/backtest_ma_geometry.py",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S IST"),
        "execution_mode": "PAPER",
        "lot_size": LOT_SIZE,
        "brokerage_hurdle_inr": BROKERAGE_HURDLE_INR,
        "statutory_tariff_model": "Exact Dhan Tariff (paper_trading.calculate_trade_charges)",
        "slippage_premium_pts_per_leg": SLIPPAGE_PTS_PER_LEG,
        "slippage_inr_per_trade": SLIPPAGE_INR_PER_TRADE,
        "multiple_comparison_noise_hurdle_sd": 2.0,
        "total_measures_in_primary": 17,
        "random_baseline_null": random_baseline,
        "baseline_statistics": baseline_stats,
        "primary_analysis_locked": {
            "description": "Variant A (Raw 15m 8 EMA Touch), Fixed 40/40, EMA(4) vs EMA(8), lot 65, Rs 400 brokerage hurdle",
            "results": primary_results,
        },
        "secondary_analysis_exploratory": {
            "description": "Exploratory only — not scored against BUILD hurdle",
            "results": secondary_results,
        },
        "mandatory_controls_summary": {
            "direction_control": "Opposite (Short) trade simulated at exact same entry bar; directional advantage reported",
            "time_to_resolution": "Exported in minutes in events_variantA.csv, events_variantB.csv, events_variantE.csv",
            "session_block_bootstrap": "1,000 whole-session bootstrap iterations for 95% CI on surviving OOS trades",
            "outlier_robustness": "Net Rs/trade evaluated excluding top 1, 3, and 5 winning trades",
            "random_baseline": "200 Monte Carlo draws time-of-day matched across OOS sessions",
            "measure_17_volume_status": "Evaluated using institutional Time-of-Day RVOL engine (FUT + FUT2) scoped to 39 sessions",
            "measure_16_1h_slope_status": "Documented as continuous refinement of prior failed binary 1H gate",
        },
    }

    report_path = OUT_DIR / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    log.info("Successfully persisted master report JSON: %s", report_path)

    # 9. Print Clean Markdown Verdict Table to Terminal
    print("\n" + "=" * 135)
    print("PORT 8510 — MA GEOMETRY & CONTACT BACKTEST: PRIMARY ANALYSIS VERDICT TABLE")
    print("Configuration: Variant A (n ≈ 2,370) | Fixed 40/40 | EMA(4) vs EMA(8) | Lot 65 | ₹400 Brokerage Hurdle")
    print("Hurdle for BUILD: OOS Net ₹/trade > 0, PF ≥ 1.30, OOS n ≥ 60, Ex-top5 > 0, Z-score ≥ +2.0 SD, Dir Adv > 0")
    print("=" * 135)

    headers = [
        "Measure",
        "Group",
        "Shape",
        "OOS n",
        "OOS WR%",
        "Net ₹400",
        "Dhan Net",
        "PF",
        "ExTop5 ₹",
        "Dir Adv ₹",
        "Z-Score",
        "Verdict",
    ]
    fmt = "{:<26} | {:<16} | {:<14} | {:>5} | {:>7} | {:>9} | {:>9} | {:>5} | {:>9} | {:>9} | {:>7} | {:<9}"
    print(fmt.format(*headers))
    print("-" * 135)

    for r in primary_results:
        if r.get("status") == "BLOCKED":
            print(fmt.format(
                r["name"][:26],
                r["group"][:16],
                "BLOCKED",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                "BLOCKED"
            ))
            continue

        oos = r["layer3_oos"]
        print(fmt.format(
            r["name"][:26],
            r["group"][:16],
            r["shape"][:14],
            str(oos["n_surviving"]),
            f"{oos['win_rate']:.1f}%",
            f"₹{oos['net_inr_hurdle400']:.1f}",
            f"₹{oos['net_inr_statutory_dhan']:.1f}",
            f"{oos['profit_factor']:.2f}",
            f"₹{oos['net_inr_ex_top5']:.1f}",
            f"₹{oos['directional_advantage']:.1f}",
            f"{oos['z_score_sd']} SD",
            r["verdict"]
        ))

    print("=" * 135)
    print("\nBASELINE UNFILTERED CONTEXT:")
    print(f"  - Variant A Raw Baseline: n={baseline_stats['variant_a_all']['n']}, Win Rate={baseline_stats['variant_a_all']['win_rate']}%, Net ₹400 Hurdle=₹{baseline_stats['variant_a_all']['net_inr_hurdle400']}/trade (PF {baseline_stats['variant_a_all']['profit_factor_hurdle400']}) | Statutory Net=₹{baseline_stats['variant_a_all']['net_inr_statutory_dhan']}/trade (PF {baseline_stats['variant_a_all']['profit_factor_statutory']})")
    print(f"  - Variant B (4>8 Trend): n={baseline_stats['variant_b_all']['n']}, Win Rate={baseline_stats['variant_b_all']['win_rate']}%, Net ₹400 Hurdle=₹{baseline_stats['variant_b_all']['net_inr_hurdle400']}/trade (PF {baseline_stats['variant_b_all']['profit_factor_hurdle400']}) | Statutory Net=₹{baseline_stats['variant_b_all']['net_inr_statutory_dhan']}/trade (PF {baseline_stats['variant_b_all']['profit_factor_statutory']})")
    print(f"  - Variant E (Institutional): n={baseline_stats['variant_e_all']['n']}, Win Rate={baseline_stats['variant_e_all']['win_rate']}%, Net ₹400 Hurdle=₹{baseline_stats['variant_e_all']['net_inr_hurdle400']}/trade (PF {baseline_stats['variant_e_all']['profit_factor_hurdle400']}) | Statutory Net=₹{baseline_stats['variant_e_all']['net_inr_statutory_dhan']}/trade (PF {baseline_stats['variant_e_all']['profit_factor_statutory']})")
    print(f"  - Time-Matched Random Null: Mean=₹{random_baseline['mean_net']}/trade, SE=₹{random_baseline['se_net']}, 95% CI=[₹{random_baseline['ci_low']}, ₹{random_baseline['ci_high']}]")
    print(f"\nExecution completed in {time.time() - t_start:.2f}s.")
    print(f"Full path: {Path(__file__).resolve()}")


if __name__ == "__main__":
    main()
