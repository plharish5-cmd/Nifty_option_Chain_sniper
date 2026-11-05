#!/usr/bin/env python3
"""
scripts/backtest_ma_geometry.py

Advanced Chart Geometry, Contact & Regime Backtest for Port 8510 / OC App.
Offline, read-only analytical script evaluating whether chart geometry, contact
penetration, whip-saws, 1-hour trend, and volume surge can close the 1.9% break-even
gap in Setup #1 (Stage 2 8-EMA Long).

PRIMARY ANALYSIS (LOCKED):
  - Population: Variant A (n ~ 2,370)
  - Exit Model: Fixed 40/40 bracket after ₹400 round-trip brokerage at lot size 65
  - MA Type: EMA(4) vs EMA(8)
  - 16 Active Measures, Decile Buckets (D1..D10)
  - Best-of-16 Noise Benchmark (~ +2.0 SD)

SECONDARY ANALYSIS (EXPLORATORY, NOT SCORED AGAINST BUILD HURDLE):
  - Variant B (4>8 Trend Filter, n ~ 1,120)
  - Variant E (Institutional 15m, n = 80)
  - SMA(4) vs EMA(8)
  - Super Trail v1.1 + Follow-Through CTC

MANDATORY CONTROLS:
  1. Direction Control: Opposite (Short) trade on identical entry bar vs matching baseline
  2. Time-to-Resolution: (exit_ts - entry_ts)/60 in every events_*.csv
  3. Session-Block Bootstrap: 1,000 resamples of whole trading days for 95% CI
  4. Outlier Robustness: Expectancy excluding top 1, 3, and 5 winning trades
  5. Measure 17 (Volume): BLOCKED per futures history limitation (09-09 to 11-05 only)
  6. Measure 16 (1H Slope): Explicitly documented as continuous refinement of prior failed binary test
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
BROKERAGE = 400.0
ASYM_WIN_DELTA = 0.60
ASYM_LOSS_DELTA = 0.50
FIXED_SL_PTS = 40.0
FIXED_TARGET_PTS = 40.0
SQUARE_OFF_TIME = "15:20:00"

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

    # Group A: Geometry Measures
    spread = df["ema4"] - df["ema8"]
    df["separation_signed"] = spread / df["atr14"]
    df["separation_abs"] = spread.abs() / df["atr14"]
    df["separation_percentile"] = fast_rolling_percentile(df["separation_abs"], window=6250)

    df["ma4_slope"] = (df["ema4"] - df["ema4"].shift(5)) / (5.0 * df["atr14"] + 1e-9)
    df["ema8_slope"] = (df["ema8"] - df["ema8"].shift(5)) / (5.0 * df["atr14"] + 1e-9)
    df["ma4_angle_deg"] = np.degrees(np.arctan(df["ma4_slope"].fillna(0.0)))

    df["spread_velocity"] = (spread - spread.shift(3)) / (3.0 * df["atr14"] + 1e-9)
    df["spread_phase"] = np.sign(spread) * np.sign(df["spread_velocity"])
    df["ema8_curvature"] = (df["ema8"] - 2.0 * df["ema8"].shift(3) + df["ema8"].shift(6)) / df["atr14"]

    # Group B: Contact Measures (Long/CE perspective)
    df["penetration_depth"] = (df["ema8"] - l) / df["atr14"]
    bar_range = (h - l).replace(0, 0.01)
    df["wick_rejection_pinbar"] = (c - l) / bar_range

    # 12-bar whip-saw count (sign changes of spread)
    sign_spread = np.sign(spread)
    flips = (sign_spread != sign_spread.shift(1)).astype(int)
    flips.iloc[0] = 0
    df["whipsaw_cross_count_12"] = flips.rolling(12, min_periods=1).sum()

    # Secondary: SMA4 vs EMA8
    sma_spread = df["sma4"] - df["ema8"]
    df["sma4_vs_ema8_separation_signed"] = sma_spread / df["atr14"]
    df["sma4_vs_ema8_spread_velocity"] = (sma_spread - sma_spread.shift(3)) / (3.0 * df["atr14"] + 1e-9)

    return df


def prepare_1h_trend_frame(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Computes 1H 8-EMA slope and candle alignment for Mentor's Rule."""
    df = df_1h.copy()
    c = df["close"]
    o = df["open"]
    df["ema8_1h"] = c.ewm(span=8, adjust=False).mean()
    df["atr14_1h"] = calc_atr(df, 14).replace(0, np.nan).bfill()
    df["slope_1h_8ema"] = (df["ema8_1h"] - df["ema8_1h"].shift(2)) / (2.0 * df["atr14_1h"] + 1e-9)
    df["candle_align_1h"] = np.where(c >= o, 1.0, -1.0)
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
# 3. FORWARD-WALK SIMULATORS (FIXED 40/40, DIRECTION CONTROL & SUPER TRAIL)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_direction_control_short(
    entry_ts: int,
    entry_spot: float,
    session_1m: List[Dict[str, Any]],
) -> Tuple[float, float, str, float]:
    """
    Mandatory Control 1: Mirror short trade entered at exact same bar.
    Evaluates short fixed 40/40 bracket to test whether the edge is directional.
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
            net_inr = opt_pts * LOT_SIZE - BROKERAGE
            return spot_pnl, net_inr, "SL_HIT", max(0.0, dur_min)

        # Check Target (Low <= entry - 40)
        if b_low <= short_target:
            spot_pnl = entry_spot - short_target
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * ASYM_WIN_DELTA
            net_inr = opt_pts * LOT_SIZE - BROKERAGE
            return spot_pnl, net_inr, "TARGET_HIT", max(0.0, dur_min)

        # EOD Square-off
        if b_time >= SQUARE_OFF_TIME or i == len(session_1m) - 1:
            spot_pnl = entry_spot - b_close
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            net_inr = opt_pts * LOT_SIZE - BROKERAGE
            return spot_pnl, net_inr, "EOD_SQUARE_OFF", max(0.0, dur_min)

    return 0.0, -BROKERAGE, "NO_DATA", 0.0


def simulate_super_trail_long(
    entry_ts: int,
    entry_spot: float,
    session_1m: List[Dict[str, Any]],
) -> Tuple[float, float, str, float]:
    """
    Secondary Model: Super Trail v1.1 + Follow-Through CTC rule.
    Activation=15, Jump=5, Initial SL=20; Target Initial=25, Step=10, Pullback=5.
    """
    idx_start = bisect.bisect_right([b["ts"] for b in session_1m], entry_ts)
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
                net_inr = opt_pts * LOT_SIZE - BROKERAGE
                return spot_pnl, net_inr, "TRAIL_TARGET_PULLBACK", max(0.0, dur_min)

        # SL Hit
        if b_low <= trailed_sl:
            exit_price = trailed_sl
            spot_pnl = exit_price - entry_spot
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            net_inr = opt_pts * LOT_SIZE - BROKERAGE
            return spot_pnl, net_inr, "TRAIL_SL_HIT", max(0.0, dur_min)

        # EOD Square-off
        if b_time >= SQUARE_OFF_TIME or i == len(session_1m) - 1:
            exit_price = b_close
            spot_pnl = exit_price - entry_spot
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            net_inr = opt_pts * LOT_SIZE - BROKERAGE
            return spot_pnl, net_inr, "EOD_SQUARE_OFF", max(0.0, dur_min)

    return 0.0, -BROKERAGE, "NO_DATA", 0.0


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

        # Direction control (Short mirror trade)
        dir_pnl, dir_net_inr, dir_reason, dir_dur = simulate_direction_control_short(
            entry_ts=t_entry,
            entry_spot=tr.entry_spot,
            session_1m=session_1m,
        )

        # Super Trail exit model (Secondary)
        st_pnl, st_net_inr, st_reason, st_dur = simulate_super_trail_long(
            entry_ts=t_entry,
            entry_spot=tr.entry_spot,
            session_1m=session_1m,
        )

        # Time-to-resolution column (Mandatory Control 2)
        dur_min = (tr.exit_ts - tr.entry_ts) / 60.0 if tr.exit_ts and tr.entry_ts else 0.0
        dur_min = max(0.0, dur_min)

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
            "asym_net_inr": round(tr.asym_net_inr, 2),
            "asym_gross_inr": round(tr.asym_gross_inr, 2),
            "is_win": 1 if tr.spot_pnl_pts > 0 else 0,
            # Direction Control
            "dir_ctrl_spot_pnl_pts": round(dir_pnl, 2),
            "dir_ctrl_asym_net_inr": round(dir_net_inr, 2),
            "dir_ctrl_exit_reason": dir_reason,
            "dir_ctrl_time_to_resolution_min": round(dir_dur, 2),
            # Super Trail Model
            "super_trail_spot_pnl_pts": round(st_pnl, 2),
            "super_trail_net_inr": round(st_net_inr, 2),
            "super_trail_exit_reason": st_reason,
            "super_trail_time_to_resolution_min": round(st_dur, 2),
            # Group B Measures (Priority Contact & Boss Priorities)
            "m01_penetration_depth": b15.get("penetration_depth", np.nan),
            "m02_wick_rejection_pinbar": b15.get("wick_rejection_pinbar", np.nan),
            "m03_whipsaw_cross_count_12": b15.get("whipsaw_cross_count_12", np.nan),
            "m04_slope_1h_8ema": b1h.get("slope_1h_8ema", np.nan) if b1h else np.nan,
            "m04_candle_align_1h": b1h.get("candle_align_1h", np.nan) if b1h else np.nan,
            "m05_fut_volume_surge": fut_vol_surge,
            "m05_fut_volume_delta": fut_vol_delta,
            # Group A Measures (4/8 MA Line Geometry)
            "m06_separation_signed": b15.get("separation_signed", np.nan),
            "m07_separation_abs": b15.get("separation_abs", np.nan),
            "m08_separation_percentile": b15.get("separation_percentile", np.nan),
            "m09_ma4_slope": b15.get("ma4_slope", np.nan),
            "m10_ema8_slope": b15.get("ema8_slope", np.nan),
            "m11_spread_velocity": b15.get("spread_velocity", np.nan),
            "m12_spread_phase": b15.get("spread_phase", np.nan),
            "m13_ema8_curvature": b15.get("ema8_curvature", np.nan),
            "m14_adx14": b15.get("adx14", np.nan),
            "m15_bb_width_percentile": b15.get("bb_width_percentile", np.nan),
            "m16_ma4_angle_deg": b15.get("ma4_angle_deg", np.nan),
            # Secondary MA Types
            "sma4_vs_ema8_separation_signed": b15.get("sma4_vs_ema8_separation_signed", np.nan),
            "sma4_vs_ema8_spread_velocity": b15.get("sma4_vs_ema8_spread_velocity", np.nan),
        }
        enriched.append(row)

    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# 5. DECILE-FIRST SHAPE ANALYSIS & BUILD HURDLE EVALUATION
# ─────────────────────────────────────────────────────────────────────────────

MEASURE_METADATA = [
    # Group B: Contact Geometry & Boss Priorities (Run First)
    ("m01_penetration_depth", "8-EMA Contact Penetration Depth", "Group B (Contact)"),
    ("m02_wick_rejection_pinbar", "Contact Wick Rejection (Pin-Bar)", "Group B (Contact)"),
    ("m03_whipsaw_cross_count_12", "12-Bar Whip-Saw Count (Chop)", "Group B (Contact)"),
    ("m04_slope_1h_8ema", "1H 8-EMA Continuous Slope", "Group B (1H Trend)"),
    ("m05_fut_volume_surge", "NIFTY Futures Volume Surge Ratio", "Group B (Volume — BLOCKED)"),
    # Group A: 4/8 MA Line Geometry
    ("m06_separation_signed", "Separation (Signed)", "Group A (Line Geometry)"),
    ("m07_separation_abs", "Separation (Absolute)", "Group A (Line Geometry)"),
    ("m08_separation_percentile", "Separation Percentile", "Group A (Line Geometry)"),
    ("m09_ma4_slope", "MA4 Slope (Normalized Angle)", "Group A (Line Geometry)"),
    ("m10_ema8_slope", "EMA8 Slope (Underlying Trend)", "Group A (Line Geometry)"),
    ("m11_spread_velocity", "Spread Velocity", "Group A (Line Geometry)"),
    ("m12_spread_phase", "Spread Phase (+1 Exp, -1 Cont)", "Group A (Line Geometry)"),
    ("m13_ema8_curvature", "EMA8 Curvature (2nd Derivative)", "Group A (Line Geometry)"),
    ("m14_adx14", "ADX(14) Trend Strength", "Group A (Regime)"),
    ("m15_bb_width_percentile", "BB Width Percentile", "Group A (Regime)"),
    ("m16_ma4_angle_deg", "MA4 Angle in Degrees", "Group A (Sanity Check)"),
]


def classify_decile_shape(deciles: List[Dict[str, Any]]) -> str:
    """Classifies the decile progression into one of 5 archetypes."""
    if len(deciles) < 10:
        return "NO_SHAPE"

    net_pnls = [d["net_inr_per_trade"] for d in deciles]
    win_rates = [d["win_rate"] for d in deciles]

    # Spearman rank correlation across decile index vs Net PnL
    rho, _ = stats.spearmanr(range(10), net_pnls)

    if rho > 0.60:
        return "MONOTONE_POSITIVE"
    if rho < -0.60:
        return "MONOTONE_NEGATIVE"

    # Chop-Floor / Bottom Removal: D1 or D1+D2 substantially worse than median
    med_pnl = statistics_median(net_pnls)
    if net_pnls[0] < med_pnl - 150.0 and net_pnls[1] > net_pnls[0] + 80.0:
        return "CHOP_FLOOR"

    # Inverted-U / Sweet Spot: D4..D7 outperform extremes D1..D2 and D9..D10
    mid_avg = statistics_mean(net_pnls[3:7])
    ext_avg = statistics_mean(net_pnls[0:2] + net_pnls[8:10])
    if mid_avg > ext_avg + 100.0:
        return "INVERTED_U"

    return "NO_SHAPE"


def statistics_mean(vals: List[float]) -> float:
    return float(np.mean(vals)) if vals else 0.0


def statistics_median(vals: List[float]) -> float:
    return float(np.median(vals)) if vals else 0.0


def run_session_block_bootstrap(
    trades: List[Dict[str, Any]],
    n_iterations: int = 1000,
    seed: int = 42,
) -> Tuple[float, float]:
    """Resamples whole trading sessions with replacement to yield robust 95% CI."""
    if not trades:
        return 0.0, 0.0

    random.seed(seed)
    np.random.seed(seed)

    by_session = {}
    for t in trades:
        s = t["session_date"]
        if s not in by_session:
            by_session[s] = []
        by_session[s].append(t["asym_net_inr"])

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


def evaluate_single_measure(
    measure_key: str,
    measure_name: str,
    group_name: str,
    df_trades: pd.DataFrame,
    is_sessions: List[str],
    oos_sessions: List[str],
    pnl_col: str = "asym_net_inr",
) -> Dict[str, Any]:
    """Executes decile analysis, OOS validation, and BUILD hurdle tests for a single measure."""
    # Filter valid rows
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

    # Split into In-Sample and Out-of-Sample
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

    # Decile Bucketing on In-Sample
    try:
        is_df["decile"] = pd.qcut(is_df[measure_key], q=10, labels=False, duplicates="drop")
    except Exception:
        is_df["decile"] = pd.cut(is_df[measure_key], bins=10, labels=False)

    decile_summary = []
    unique_deciles = sorted(is_df["decile"].dropna().unique())
    for d in unique_deciles:
        sub = is_df[is_df["decile"] == d]
        n_d = len(sub)
        w_d = int(sub["is_win"].sum())
        wr_d = (w_d / n_d * 100.0) if n_d else 0.0
        net_d = float(sub[pnl_col].sum())
        exp_d = (net_d / n_d) if n_d else 0.0
        wins_pnl = sub[sub[pnl_col] > 0][pnl_col].sum()
        loss_pnl = abs(sub[sub[pnl_col] < 0][pnl_col].sum())
        pf_d = round(wins_pnl / loss_pnl, 2) if loss_pnl > 0 else 99.0
        min_v = float(sub[measure_key].min())
        max_v = float(sub[measure_key].max())
        decile_summary.append({
            "decile": int(d) + 1,
            "min_val": round(min_v, 4),
            "max_val": round(max_v, 4),
            "n": n_d,
            "win_rate": round(wr_d, 1),
            "net_inr_per_trade": round(exp_d, 2),
            "profit_factor": pf_d,
        })

    # Classify Shape Archetype
    shape = classify_decile_shape(decile_summary)
    spearman_rho, spearman_p = stats.spearmanr(is_df[measure_key], is_df[pnl_col])

    # Select Optimal Filter Rule on In-Sample
    cutoff_type = "NONE"
    cutoff_val: Any = None

    if shape == "MONOTONE_POSITIVE":
        # Keep upper deciles
        cutoff_val = float(is_df[measure_key].quantile(0.30))  # Drop bottom 30%
        cutoff_type = "GTE"
    elif shape == "MONOTONE_NEGATIVE":
        # Keep lower deciles
        cutoff_val = float(is_df[measure_key].quantile(0.70))  # Drop top 30%
        cutoff_type = "LTE"
    elif shape == "CHOP_FLOOR":
        # Drop bottom 15%
        cutoff_val = float(is_df[measure_key].quantile(0.15))
        cutoff_type = "GTE"
    elif shape == "INVERTED_U":
        # Keep middle 60%
        q_low = float(is_df[measure_key].quantile(0.20))
        q_high = float(is_df[measure_key].quantile(0.80))
        cutoff_val = (q_low, q_high)
        cutoff_type = "BETWEEN"
    else:
        # Default test: drop bottom 20%
        cutoff_val = float(is_df[measure_key].quantile(0.20))
        cutoff_type = "GTE"

    # Evaluate on Untouched Out-of-Sample (OOS)
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
    wins_oos = oos_passed[oos_passed[pnl_col] > 0][pnl_col].sum()
    loss_oos = abs(oos_passed[oos_passed[pnl_col] < 0][pnl_col].sum())
    pf_oos = round(wins_oos / loss_oos, 2) if loss_oos > 0 else 99.0

    # Direction Control on same OOS subset
    dir_net_oos = float(oos_passed["dir_ctrl_asym_net_inr"].sum()) if n_oos else 0.0
    dir_exp_oos = (dir_net_oos / n_oos) if n_oos else 0.0
    directional_advantage = exp_oos - dir_exp_oos

    # Outlier Robustness: Exclude top 1, 3, 5 wins
    sorted_pnls = sorted(oos_passed[pnl_col].tolist(), reverse=True)
    exp_ex_top1 = float(np.mean(sorted_pnls[1:])) if len(sorted_pnls) > 1 else exp_oos
    exp_ex_top3 = float(np.mean(sorted_pnls[3:])) if len(sorted_pnls) > 3 else exp_oos
    exp_ex_top5 = float(np.mean(sorted_pnls[5:])) if len(sorted_pnls) > 5 else exp_oos

    # Session-block bootstrap 95% CI
    ci_low, ci_high = run_session_block_bootstrap(oos_passed.to_dict(orient="records"))

    # Noise Benchmark (Z-score vs +2.0 SD ceiling)
    pnls_arr = oos_passed[pnl_col].values
    se = (np.std(pnls_arr, ddof=1) / math.sqrt(n_oos)) if n_oos > 1 else 1.0
    z_score = round(exp_oos / (se + 1e-9), 2)

    # BUILD Hurdle Verification:
    # 1. Filtered Net ₹/trade > 0
    # 2. Profit Factor >= 1.30
    # 3. n >= 60 in OOS
    # 4. Remains net-positive excluding top 5 wins
    # 5. Z-score exceeds best-of-16 noise ceiling (+2.0 SD)
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

    # Explanatory failure reason if not built
    failure_reasons = []
    if not cleared_pnl:
        failure_reasons.append(f"Net ₹/trade $\\le$ 0 (₹{exp_oos:.2f})")
    if not cleared_pf:
        failure_reasons.append(f"PF < 1.30 ({pf_oos:.2f})")
    if not cleared_n:
        failure_reasons.append(f"OOS n < 60 ({n_oos})")
    if not cleared_outlier:
        failure_reasons.append(f"Ex-top5 wins Net $\\le$ 0 (₹{exp_ex_top5:.2f})")
    if not cleared_noise:
        failure_reasons.append(f"Z-score {z_score} SD < +2.0 SD noise hurdle")
    if not cleared_dir:
        failure_reasons.append(f"Fails Direction Control (Diff ₹{directional_advantage:.2f})")

    return {
        "key": measure_key,
        "name": measure_name,
        "group": group_name,
        "status": "EVALUATED",
        "shape": shape,
        "spearman_rho": round(float(spearman_rho), 3),
        "spearman_p": round(float(spearman_p), 4),
        "cutoff_rule": f"{cutoff_type} {cutoff_val}",
        "in_sample": {
            "n_total": len(is_df),
            "deciles": decile_summary,
        },
        "out_of_sample": {
            "n_surviving": n_oos,
            "win_rate": round(wr_oos, 1),
            "net_inr_per_trade": round(exp_oos, 2),
            "profit_factor": pf_oos,
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
    log.info("PORT 8510 ADVANCED CHART GEOMETRY & CONTACT BACKTEST ENGINE")
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

    # Load Futures data for Measure 17 (Volume)
    lookup_fut: Optional[ClosedBarLookup] = None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        fut_1m = pd.read_sql_query("""
            SELECT ts, o as open, h as high, l as low, c as close, v as volume,
                   date(ts, 'unixepoch', '+5 hours', '+30 minutes') as session_date,
                   time(ts, 'unixepoch', '+5 hours', '+30 minutes') as session_time
            FROM candles_1m WHERE sec_id = 'FUT' ORDER BY ts ASC
        """, conn)
        conn.close()
        if not fut_1m.empty:
            df_fut_15m = resample_bars(fut_1m, "15min")
            vol_roll = df_fut_15m["volume"].rolling(10).mean().replace(0, np.nan)
            df_fut_15m["fut_vol_surge"] = df_fut_15m["volume"] / vol_roll
            df_fut_15m["fut_vol_delta_ratio"] = (df_fut_15m["volume"] - df_fut_15m["volume"].shift(1)) / vol_roll
            lookup_fut = ClosedBarLookup(df_fut_15m, bar_seconds=900)
            log.info("Futures 15m loaded (%d bars from %s to %s) for Measure 17 audit.",
                     len(df_fut_15m), df_fut_15m.iloc[0]["session_date"], df_fut_15m.iloc[-1]["session_date"])
    except Exception as e:
        log.warning("Could not load FUT volume data: %s", e)

    # Pre-index 1m sessions for fast direction & super trail simulation
    session_1m_map = {}
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

    # 4. Enrich Trades with 17 Geometry & Contact Measures
    log.info("Enriching trades with geometry, contact depth, direction control, and Super Trail...")
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

    # 5. PRIMARY ANALYSIS (LOCKED): Variant A, Fixed 40/40 Bracket, EMA(4)/EMA(8)
    log.info("══════════════════════════════════════════════════════════════════")
    log.info("RUNNING PRIMARY ANALYSIS: Variant A (n=%d), Fixed 40/40, EMA(4)/EMA(8)", len(enriched_a))
    log.info("══════════════════════════════════════════════════════════════════")

    df_a = pd.DataFrame(enriched_a)
    primary_results = []

    for key, name, group in MEASURE_METADATA:
        # Measure 17 (Volume Surge) Handling per Addendum C
        if key == "m05_fut_volume_surge":
            fut_valid_count = df_a["m05_fut_volume_surge"].notna().sum()
            primary_results.append({
                "key": key,
                "name": name,
                "group": group,
                "status": "BLOCKED",
                "reason": (
                    f"Blocked per Addendum C: NIFTY Futures volume covers only 09-09 to 11-05 "
                    f"(valid n={fut_valid_count}/{len(df_a)}, covering ~16% of sample). "
                    f"NIFTY spot has no meaningful volume; result cannot reach BUILD hurdle."
                ),
                "verdict": "NOT BUILT",
                "failure_reasons": ["Measure 17 blocked due to truncated futures volume coverage."],
            })
            continue

        res = evaluate_single_measure(
            measure_key=key,
            measure_name=name,
            group_name=group,
            df_trades=df_a,
            is_sessions=is_sessions,
            oos_sessions=oos_sessions,
            pnl_col="asym_net_inr",
        )
        primary_results.append(res)

    # 6. SECONDARY ANALYSIS (EXPLORATORY, NOT SCORED AGAINST BUILD HURDLE)
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
    for key in ["m01_penetration_depth", "m02_wick_rejection_pinbar", "m03_whipsaw_cross_count_12", "m06_separation_signed"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Variant B Secondary", df_b, is_sessions, oos_sessions)
        secondary_results["variant_b_40_40"].append(r)

    # Exploratory 2: Variant E (Institutional Footnote)
    for key in ["m01_penetration_depth", "m04_slope_1h_8ema", "m06_separation_signed"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Variant E Footnote", df_e, is_sessions, oos_sessions)
        secondary_results["variant_e_institutional"].append(r)

    # Exploratory 3: SMA(4) vs EMA(8)
    for key in ["sma4_vs_ema8_separation_signed", "sma4_vs_ema8_spread_velocity"]:
        r = evaluate_single_measure(key, key, "SMA(4) TradingView", df_a, is_sessions, oos_sessions)
        secondary_results["variant_a_sma4_vs_ema8"].append(r)

    # Exploratory 4: Super Trail v1.1 + Follow-Through CTC on Variant A
    for key in ["m01_penetration_depth", "m02_wick_rejection_pinbar", "m06_separation_signed"]:
        meta = next((m for m in MEASURE_METADATA if m[0] == key), (key, key, "Secondary"))
        r = evaluate_single_measure(key, meta[1], "Super Trail Model", df_a, is_sessions, oos_sessions, pnl_col="super_trail_net_inr")
        secondary_results["variant_a_super_trail"].append(r)

    # Baseline Statistics
    baseline_stats = {
        "variant_a_all": {
            "n": len(df_a),
            "win_rate": round(float(df_a["is_win"].mean() * 100), 1),
            "net_inr_per_trade": round(float(df_a["asym_net_inr"].mean()), 2),
            "profit_factor": round(float(df_a[df_a["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_a[df_a["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
        },
        "variant_b_all": {
            "n": len(df_b),
            "win_rate": round(float(df_b["is_win"].mean() * 100), 1),
            "net_inr_per_trade": round(float(df_b["asym_net_inr"].mean()), 2),
            "profit_factor": round(float(df_b[df_b["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_b[df_b["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
        },
        "variant_e_all": {
            "n": len(df_e),
            "win_rate": round(float(df_e["is_win"].mean() * 100), 1),
            "net_inr_per_trade": round(float(df_e["asym_net_inr"].mean()), 2),
            "profit_factor": round(float(df_e[df_e["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_e[df_e["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9)), 2),
        },
    }

    # 7. Generate Master Report JSON (Mandatory Deliverable)
    report_data = {
        "title": "Port 8510 Advanced Chart Geometry & Contact Backtest Report",
        "script": "scripts/backtest_ma_geometry.py",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S IST"),
        "execution_mode": "PAPER",
        "lot_size": LOT_SIZE,
        "round_trip_brokerage_inr": BROKERAGE,
        "multiple_comparison_noise_hurdle_sd": 2.0,
        "total_measures_in_primary": 16,
        "baseline_statistics": baseline_stats,
        "primary_analysis_locked": {
            "description": "Variant A (Raw 15m 8 EMA Touch), Fixed 40/40, EMA(4) vs EMA(8), lot 65, Rs400 brokerage",
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
            "measure_17_volume_blocked": "Reported BLOCKED due to limited futures history (09-09 to 11-05, ~16% sample)",
            "measure_16_1h_slope_status": "Documented as continuous refinement of prior failed binary 1H gate",
        },
    }

    report_path = OUT_DIR / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    log.info("Successfully persisted master report JSON: %s", report_path)

    # 8. Print Clean Markdown Verdict Table to Terminal
    print("\n" + "=" * 115)
    print("PORT 8510 — MA GEOMETRY & CONTACT BACKTEST: PRIMARY ANALYSIS VERDICT TABLE")
    print("Configuration: Variant A (n ≈ 2,370) | Fixed 40/40 | EMA(4) vs EMA(8) | Lot 65 | Brokerage ₹400")
    print("Hurdle for BUILD: OOS Net ₹/trade > 0, PF ≥ 1.30, OOS n ≥ 60, Ex-top5 > 0, Z-score ≥ +2.0 SD, Dir Adv > 0")
    print("=" * 115)

    headers = [
        "Measure",
        "Group",
        "Shape",
        "OOS n",
        "OOS WR%",
        "Net ₹/Tr",
        "PF",
        "ExTop5 ₹",
        "Dir Adv ₹",
        "Z-Score",
        "Verdict",
    ]
    fmt = "{:<26} | {:<16} | {:<14} | {:>5} | {:>7} | {:>8} | {:>5} | {:>8} | {:>9} | {:>7} | {:<9}"
    print(fmt.format(*headers))
    print("-" * 115)

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
                "BLOCKED"
            ))
            continue

        oos = r["out_of_sample"]
        print(fmt.format(
            r["name"][:26],
            r["group"][:16],
            r["shape"][:14],
            str(oos["n_surviving"]),
            f"{oos['win_rate']:.1f}%",
            f"₹{oos['net_inr_per_trade']:.1f}",
            f"{oos['profit_factor']:.2f}",
            f"₹{oos['net_inr_ex_top5']:.1f}",
            f"₹{oos['directional_advantage']:.1f}",
            f"{oos['z_score_sd']} SD",
            r["verdict"]
        ))

    print("=" * 115)
    print("\nBASELINE UNFILTERED CONTEXT:")
    print(f"  - Variant A Raw Baseline: n={baseline_stats['variant_a_all']['n']}, Win Rate={baseline_stats['variant_a_all']['win_rate']}%, Expectancy=₹{baseline_stats['variant_a_all']['net_inr_per_trade']}/trade, PF={baseline_stats['variant_a_all']['profit_factor']}")
    print(f"  - Variant B (4>8 Trend): n={baseline_stats['variant_b_all']['n']}, Win Rate={baseline_stats['variant_b_all']['win_rate']}%, Expectancy=₹{baseline_stats['variant_b_all']['net_inr_per_trade']}/trade, PF={baseline_stats['variant_b_all']['profit_factor']}")
    print(f"  - Variant E (Institutional): n={baseline_stats['variant_e_all']['n']}, Win Rate={baseline_stats['variant_e_all']['win_rate']}%, Expectancy=₹{baseline_stats['variant_e_all']['net_inr_per_trade']}/trade, PF={baseline_stats['variant_e_all']['profit_factor']}")
    print(f"\nExecution completed in {time.time() - t_start:.2f}s.")


if __name__ == "__main__":
    main()
