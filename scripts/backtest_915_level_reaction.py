#!/usr/bin/env python3
"""
scripts/backtest_915_level_reaction.py

NIFTY 50 Opening 15-Minute (09:15–09:30 IST) Pre-Market Level Reaction
& Confluence Backtester — Phase 1 & Phase 2 Enhanced Edition.

Architectural Guarantees:
  1. Zero Look-Ahead:
     - PDH, PDL, PDC, PDO computed strictly from Day D-1 (09:15 to 15:30 IST).
     - PREV_1H_8EMA computed across continuous 1-hour sessions and frozen at Day D-1 15:30 IST.
       (Never uses Day D's 09:15-10:15 bar!).
     - DOUBLE_BOTTOM_3D / DOUBLE_TOP_3D identified on 15m bars across completed Days D-3, D-2, D-1 only.
  2. Execution Window:
     - 09:15:00 to 09:30:00 IST ONLY (15 x 1m bars: 09:15 through 09:29; hard exit at 09:30:00 close).
     - 8 EMA only (span=8, adjust=False).
  3. Touch Detection, Confirmation Modes & Path Excursion:
     - Mode 1: Blind Limit Touch (Phase 1 Baseline).
     - Mode 2: Confirmed 1m Rejection Candle Close on PDH & DOUBLE_TOP_3D Shorts:
       Triggers Short at close of 1m bar t in [09:15, 09:27] iff high[t] >= Level - 5.0,
       close[t] <= Level, and upper rejection wick (high - close) >= 30% of total bar range.
       SL = max(high[t] + 5.0, Level + 20.0).
     - Mode 3: PDL Breakdown Continuation Short:
       Exploits the 63.8% PDL support failure. When price opens above PDL and a 1m bar closes
       below PDL - 3.0 inside 09:15-09:27, enters SHORT with SL = PDL + 15.0 pts.
  4. Deliverables:
     - Table 1: Level-by-Level Performance Matrix (Phase 1 Baseline)
     - Table 2: Multi-Factor Confluence Combinations (Single vs. Double vs. Triple Confluence)
     - Table 3: Session-by-Session Audit Log (CSV: analyst/reports/nifty_915_930_level_reactions.csv)
     - Table 4: Phase 2 Head-to-Head Comparison (Blind Touch vs. Wick Confirmation vs. PDL Breakdown)
     - Statistical Edge Verdict: Ranked Setups, t-test vs 36.5% break-even, N >= 30 hurdle.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "analyst" / "analyst_config.json"
REPORT_DIR = ROOT / "analyst" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest_915")


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_backtest_config() -> Dict[str, Any]:
    """Loads configuration thresholds from analyst/analyst_config.json."""
    defaults = {
        "execution_start_time": "09:15:00",
        "execution_last_entry_time": "09:28:00",
        "execution_exit_time": "09:30:00",
        "touch_tolerance_pts": 5.0,
        "touch_tolerance_choices": [3.0, 5.0, 8.0],
        "breakout_stop_pts": 20.0,
        "confluence_proximity_pts": 15.0,
        "target_25_pts": 25.0,
        "target_40_pts": 40.0,
        "target_50_pts": 50.0,
        "lot_size": 65,
        "option_delta": 0.55,
        "statutory_charges_inr": 78.0,
        "slippage_inr": 32.50,
        "total_trade_cost_inr": 110.50,
        "breakeven_win_rate": 0.365,
        "min_sample_size": 30,
        "double_bottom_lookback_days": 3,
        "double_bottom_min_bars_separation": 4,
        "double_bottom_interim_move_pts": 40.0,
        "double_bottom_price_tol_pct": 0.0015,
        "double_bottom_level_proximity_pts": 30.0,
        "h1_ema_span": 8,
        "rejection_wick_ratio": 0.30,
        "rejection_sl_buffer_pts": 5.0,
        "pdl_breakdown_trigger_pts": 3.0,
        "pdl_breakdown_sl_pts": 15.0,
        "default_source": "auto",
        "csv_path": "data/ohlc/nifty_1m_dhan.csv",
        "sqlite_path": "ict_smc_app/backend/data/ict_smc.sqlite3",
        "output_csv_path": "analyst/reports/nifty_915_930_level_reactions.csv",
        "output_report_path": "analyst/reports/nifty_915_930_level_reactions_summary.md",
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                root_cfg = json.load(f)
                section = root_cfg.get("opening_15m_level_reaction", {})
                defaults.update(section)
        except Exception as e:
            log.warning("Could not read config from %s: %s", CONFIG_PATH, e)
    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# 2. DATA LOADER & RESAMPLER
# ─────────────────────────────────────────────────────────────────────────────

def load_clean_1m_candles(
    source: str = "auto",
    csv_rel: str = "data/ohlc/nifty_1m_dhan.csv",
    db_rel: str = "ict_smc_app/backend/data/ict_smc.sqlite3",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    Loads 1-minute NIFTY Spot candles, filtered to intraday session 09:15 to 15:29.
    Supports auto (merge CSV+DB), db, or csv.
    """
    csv_full = ROOT / csv_rel
    db_full = ROOT / db_rel

    frames: List[pd.DataFrame] = []

    if source in ("auto", "csv") and csv_full.exists():
        log.info("Reading 1m candles from CSV: %s", csv_full)
        df_c = pd.read_csv(csv_full)
        df_c["dt"] = df_c["timestamp"].str.slice(0, 10)
        df_c["tm"] = df_c["timestamp"].str.slice(11, 19)
        df_c = df_c[(df_c["tm"] >= "09:15:00") & (df_c["tm"] <= "15:29:00")]
        frames.append(df_c[["dt", "tm", "open", "high", "low", "close"]])

    if source in ("auto", "db") and db_full.exists():
        max_csv_date = frames[0]["dt"].max() if (source == "auto" and frames) else "1970-01-01"
        log.info("Reading 1m candles from SQLite: %s (dates > %s)", db_full, max_csv_date)
        conn = sqlite3.connect(f"file:{db_full}?mode=ro", uri=True)
        query = f"""
            SELECT ts, o as open, h as high, l as low, c as close,
                   date(ts, 'unixepoch', '+05:30') as dt,
                   time(ts, 'unixepoch', '+05:30') as tm
            FROM candles_1m
            WHERE sec_id = '13' AND date(ts, 'unixepoch', '+05:30') > '{max_csv_date}'
            ORDER BY ts ASC
        """
        df_db = pd.read_sql_query(query, conn)
        conn.close()
        df_db = df_db[(df_db["tm"] >= "09:15:00") & (df_db["tm"] <= "15:29:00")]
        if not df_db.empty:
            frames.append(df_db[["dt", "tm", "open", "high", "low", "close"]])

    if not frames:
        raise FileNotFoundError(f"No valid 1m data found at {csv_full} or {db_full}")

    df_all = pd.concat(frames, ignore_index=True)
    df_all = df_all.sort_values(["dt", "tm"]).drop_duplicates(subset=["dt", "tm"]).reset_index(drop=True)

    if start_date:
        df_all = df_all[df_all["dt"] >= start_date].reset_index(drop=True)
    if end_date:
        df_all = df_all[df_all["dt"] <= end_date].reset_index(drop=True)

    log.info(
        "Successfully assembled %d clean 1m candles across %d trading sessions (%s to %s)",
        len(df_all),
        df_all["dt"].nunique(),
        df_all["dt"].min(),
        df_all["dt"].max(),
    )
    return df_all


def compute_standard_1h_8ema(df_1m: pd.DataFrame, span: int = 8) -> pd.DataFrame:
    """
    Resamples 1-minute candles into standard NSE 1-hour candles:
      Bin 0: 09:15 - 10:14:59 (closes at 10:15)
      Bin 1: 10:15 - 11:14:59 (closes at 11:15)
      Bin 2: 11:15 - 12:14:59 (closes at 12:15)
      Bin 3: 12:15 - 13:14:59 (closes at 13:15)
      Bin 4: 13:15 - 14:14:59 (closes at 14:15)
      Bin 5: 14:15 - 15:14:59 (closes at 15:15)
      Bin 6: 15:15 - 15:29:59 (closes at 15:30 EOD)
    Computes continuous EMA(close, span=8, adjust=False) chronologically.
    """
    def _bin_1h(tm: str) -> int:
        if tm < "10:15:00":
            return 0
        elif tm < "11:15:00":
            return 1
        elif tm < "12:15:00":
            return 2
        elif tm < "13:15:00":
            return 3
        elif tm < "14:15:00":
            return 4
        elif tm < "15:15:00":
            return 5
        return 6

    df_copy = df_1m.copy()
    df_copy["h1_bin"] = df_copy["tm"].apply(_bin_1h)

    df_1h = df_copy.groupby(["dt", "h1_bin"], as_index=False).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "tm": "first",
    })

    df_1h = df_1h.sort_values(["dt", "h1_bin"]).reset_index(drop=True)
    df_1h["ema_8"] = df_1h["close"].ewm(span=span, adjust=False).mean()
    return df_1h


def compute_15m_candles(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    Resamples 1m candles into 25 standard 15-minute intraday candles per session.
    09:15, 09:30, 09:45, ..., 15:15.
    """
    def _bin_15m(tm: str) -> int:
        parts = [int(p) for p in tm.split(":")]
        mins = (parts[0] - 9) * 60 + (parts[1] - 15)
        return max(0, min(24, mins // 15))

    df_copy = df_1m.copy()
    df_copy["b15"] = df_copy["tm"].apply(_bin_15m)

    df_15m = df_copy.groupby(["dt", "b15"], as_index=False).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    })
    return df_15m.sort_values(["dt", "b15"]).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. ZERO-LOOKAHEAD PRE-MARKET LEVEL EXTRACTION & PATTERN DETECTION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PreMarketLevels:
    date: str
    pdh: float
    pdl: float
    pdc: float
    pdo: float
    prev_1h_8ema: float
    double_bottom_3d: bool
    db_support_level: Optional[float]
    double_top_3d: bool
    dt_resistance_level: Optional[float]


def identify_3d_patterns(
    bars_3d: pd.DataFrame,
    pdh: float,
    pdl: float,
    prev_1h_8ema: float,
    cfg: Dict[str, Any],
) -> Tuple[bool, Optional[float], bool, Optional[float]]:
    """
    Zero-lookahead 3-day swing structure inspection on 15m bars:
      - DOUBLE_BOTTOM_3D: swing pivot lows Li <= min(Li-2..Li-1, Li+1..Li+2).
        Two lows separated by >= 4 bars with interim high >= 40 pts, within 0.15% of each other,
        and within 30 pts of PDL or PREV_1H_8EMA.
      - DOUBLE_TOP_3D: symmetric check for swing pivot highs near PDH or PREV_1H_8EMA.
    """
    min_separation = cfg.get("double_bottom_min_bars_separation", 4)
    interim_move = cfg.get("double_bottom_interim_move_pts", 40.0)
    tol_pct = cfg.get("double_bottom_price_tol_pct", 0.0015)
    prox_pts = cfg.get("double_bottom_level_proximity_pts", 30.0)

    n = len(bars_3d)
    if n < 10:
        return False, None, False, None

    lows = bars_3d["low"].values
    highs = bars_3d["high"].values

    swing_lows: List[Tuple[int, float]] = []
    for k in range(2, n - 2):
        if lows[k] <= min(lows[k - 2], lows[k - 1], lows[k + 1], lows[k + 2]):
            swing_lows.append((k, float(lows[k])))

    swing_highs: List[Tuple[int, float]] = []
    for k in range(2, n - 2):
        if highs[k] >= max(highs[k - 2], highs[k - 1], highs[k + 1], highs[k + 2]):
            swing_highs.append((k, float(highs[k])))

    has_db = False
    best_db_level: Optional[float] = None
    min_db_dist = float("inf")

    for i in range(len(swing_lows)):
        for j in range(i + 1, len(swing_lows)):
            k1, l1 = swing_lows[i]
            k2, l2 = swing_lows[j]
            if (k2 - k1) >= min_separation:
                interim_high = float(highs[k1 : k2 + 1].max())
                if interim_high >= max(l1, l2) + interim_move:
                    denom = min(l1, l2)
                    if denom > 0 and (abs(l1 - l2) / denom) <= tol_pct:
                        support_candidate = min(l1, l2)
                        dist_pdl = abs(support_candidate - pdl)
                        dist_ema = abs(support_candidate - prev_1h_8ema)
                        best_dist = min(dist_pdl, dist_ema)
                        if best_dist <= prox_pts:
                            has_db = True
                            if best_dist < min_db_dist:
                                min_db_dist = best_dist
                                best_db_level = support_candidate

    has_dt = False
    best_dt_level: Optional[float] = None
    min_dt_dist = float("inf")

    for i in range(len(swing_highs)):
        for j in range(i + 1, len(swing_highs)):
            k1, h1 = swing_highs[i]
            k2, h2 = swing_highs[j]
            if (k2 - k1) >= min_separation:
                interim_low = float(lows[k1 : k2 + 1].min())
                if interim_low <= min(h1, h2) - interim_move:
                    denom = max(h1, h2)
                    if denom > 0 and (abs(h1 - h2) / denom) <= tol_pct:
                        res_candidate = max(h1, h2)
                        dist_pdh = abs(res_candidate - pdh)
                        dist_ema = abs(res_candidate - prev_1h_8ema)
                        best_dist = min(dist_pdh, dist_ema)
                        if best_dist <= prox_pts:
                            has_dt = True
                            if best_dist < min_dt_dist:
                                min_dt_dist = best_dist
                                best_dt_level = res_candidate

    return has_db, best_db_level, has_dt, best_dt_level


# ─────────────────────────────────────────────────────────────────────────────
# 4. PATH EXCURSION & TRADE SIMULATOR (09:15:00 TO 09:30:00 IST)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReactionResult:
    level_name: str
    level_price: float
    direction: str  # "LONG" or "SHORT"
    approach: str   # "Support" or "Resistance"
    touch_bar_idx: int
    touch_time_ist: str
    entry_price: float
    stop_price: float
    risk_pts: float
    mfe_before_sl_pts: float
    mae_pts: float
    hit_25_plus: bool
    hit_40_plus: bool
    hit_50_plus: bool
    outcome_bucket: str
    close_930_pts: float
    resolution_quality: str  # "CLEAN" or "AMBIGUOUS_SAME_BAR"
    opt_net_pnl_inr_40m: float
    opt_net_pnl_inr_25m: float
    is_optimistic_win_40: bool
    confluence_combo: str
    confluence_count: int
    entry_mode: str  # "BLIND_TOUCH", "CONFIRMED_1M_REJECTION_CLOSE", "BREAKDOWN_CONTINUATION"


def evaluate_level_reaction_path(
    bars_15m_window: pd.DataFrame,
    level_name: str,
    level_price: float,
    direction: str,
    approach: str,
    entry_bar_idx: int,
    entry_price: float,
    stop_price: float,
    cfg: Dict[str, Any],
    confluence_combo: str = "",
    confluence_count: int = 1,
    entry_mode: str = "BLIND_TOUCH",
    is_confirmed_close: bool = False,
) -> ReactionResult:
    """
    Evaluates step-by-step path excursion from entry_bar through 09:29 (close at 09:30:00).
    If is_confirmed_close is True, the trade triggered at the close of entry_bar_idx,
    so forward price excursion strictly begins at entry_bar_idx + 1.
    """
    lot_size = cfg.get("lot_size", 65)
    opt_delta = cfg.get("option_delta", 0.55)
    total_cost = cfg.get("total_trade_cost_inr", 110.50)

    n_bars = len(bars_15m_window)
    end_bar_idx = min(14, n_bars - 1)

    touch_bar = bars_15m_window.iloc[entry_bar_idx]
    touch_time_ist = str(touch_bar["tm"])
    risk_pts = round(abs(stop_price - entry_price), 2)

    mfe_before_sl = 0.0
    mae = 0.0
    stopped_out = False
    ambiguous_same_bar = False
    target_40_hit = False

    start_eval_idx = (entry_bar_idx + 1) if is_confirmed_close else entry_bar_idx

    for b_idx in range(start_eval_idx, end_bar_idx + 1):
        bar = bars_15m_window.iloc[b_idx]
        b_high = float(bar["high"])
        b_low = float(bar["low"])

        if direction == "SHORT":
            bar_fav = entry_price - b_low
            bar_adv = b_high - entry_price
            hit_sl = (b_high >= stop_price)
            hit_tp25 = (bar_fav >= 25.0)
            hit_tp40 = (bar_fav >= 40.0)
        else:
            bar_fav = b_high - entry_price
            bar_adv = entry_price - b_low
            hit_sl = (b_low <= stop_price)
            hit_tp25 = (bar_fav >= 25.0)
            hit_tp40 = (bar_fav >= 40.0)

        mae = max(mae, max(0.0, bar_adv))

        if hit_sl and hit_tp25:
            ambiguous_same_bar = True
            mfe_before_sl = max(mfe_before_sl, bar_fav)
            stopped_out = True
            if hit_tp40:
                target_40_hit = True
            break
        elif hit_sl:
            stopped_out = True
            mfe_before_sl = max(mfe_before_sl, max(0.0, bar_fav))
            break
        else:
            mfe_before_sl = max(mfe_before_sl, max(0.0, bar_fav))
            if hit_tp40:
                target_40_hit = True

    # Close price at 09:30:00 (close of bar 09:29)
    bar_929 = bars_15m_window.iloc[end_bar_idx]
    close_930_price = float(bar_929["close"])
    if direction == "SHORT":
        close_930_pts = entry_price - close_930_price
    else:
        close_930_pts = close_930_price - entry_price

    resolution_quality = "AMBIGUOUS_SAME_BAR" if ambiguous_same_bar else "CLEAN"

    if stopped_out:
        if ambiguous_same_bar:
            outcome_bucket = "BREAKOUT_FAIL"
            is_optimistic_win_40 = (mfe_before_sl >= 40.0)
        else:
            outcome_bucket = "BREAKOUT_FAIL"
            is_optimistic_win_40 = False
    else:
        is_optimistic_win_40 = (mfe_before_sl >= 40.0)
        if mfe_before_sl >= 50.0:
            outcome_bucket = ">50_PTS"
        elif mfe_before_sl >= 40.0:
            outcome_bucket = "40_TO_50_PTS"
        elif mfe_before_sl >= 25.0:
            outcome_bucket = "25_TO_40_PTS"
        else:
            outcome_bucket = "<25_PTS"

    hit_25_plus = outcome_bucket in ("25_TO_40_PTS", "40_TO_50_PTS", ">50_PTS")
    hit_40_plus = outcome_bucket in ("40_TO_50_PTS", ">50_PTS")
    hit_50_plus = outcome_bucket == ">50_PTS"

    # Financial Model A: 40pt Target vs SL Model
    if hit_40_plus:
        pnl_40m = 40.0 * opt_delta * lot_size - total_cost
    elif outcome_bucket == "BREAKOUT_FAIL":
        pnl_40m = -risk_pts * opt_delta * lot_size - total_cost
    else:
        pnl_40m = close_930_pts * opt_delta * lot_size - total_cost

    # Financial Model B: 25pt Target vs SL Model
    if hit_25_plus:
        pnl_25m = 25.0 * opt_delta * lot_size - total_cost
    elif outcome_bucket == "BREAKOUT_FAIL":
        pnl_25m = -risk_pts * opt_delta * lot_size - total_cost
    else:
        pnl_25m = close_930_pts * opt_delta * lot_size - total_cost

    return ReactionResult(
        level_name=level_name,
        level_price=round(level_price, 2),
        direction=direction,
        approach=approach,
        touch_bar_idx=entry_bar_idx,
        touch_time_ist=touch_time_ist,
        entry_price=round(entry_price, 2),
        stop_price=round(stop_price, 2),
        risk_pts=risk_pts,
        mfe_before_sl_pts=round(mfe_before_sl, 2),
        mae_pts=round(mae, 2),
        hit_25_plus=hit_25_plus,
        hit_40_plus=hit_40_plus,
        hit_50_plus=hit_50_plus,
        outcome_bucket=outcome_bucket,
        close_930_pts=round(close_930_pts, 2),
        resolution_quality=resolution_quality,
        opt_net_pnl_inr_40m=round(pnl_40m, 2),
        opt_net_pnl_inr_25m=round(pnl_25m, 2),
        is_optimistic_win_40=is_optimistic_win_40,
        confluence_combo=confluence_combo,
        confluence_count=confluence_count,
        entry_mode=entry_mode,
    )


@dataclass
class LevelReactionRecord:
    date: str
    open_915: float
    pdh: float
    pdl: float
    pdc: float
    pdo: float
    prev_1h_8ema: float
    open_vs_1h_8ema: str
    double_bottom_3d: bool
    first_level_touched: str
    touch_time_ist: str
    direction: str
    confluence_combo: str
    confluence_count: int
    entry_price: float
    level_price: float
    stop_price: float
    mfe_before_sl_pts: float
    mae_pts: float
    hit_25_plus: bool
    hit_40_plus: bool
    hit_50_plus: bool
    outcome_bucket: str
    close_930_pts: float
    resolution_quality: str
    opt_net_pnl_inr: float
    opt_net_pnl_inr_40m: float
    opt_net_pnl_inr_25m: float
    is_optimistic_win_40: bool


# ─────────────────────────────────────────────────────────────────────────────
# 5. BACKTEST ENGINE (PHASE 1 + PHASE 2 HEAD-TO-HEAD)
# ─────────────────────────────────────────────────────────────────────────────

def run_915_level_backtest(
    df_1m: pd.DataFrame,
    cfg: Dict[str, Any],
) -> Tuple[List[LevelReactionRecord], List[ReactionResult], List[ReactionResult], List[ReactionResult]]:
    """
    Runs zero-lookahead backtest across all sessions:
      Returns:
        1. session_records: 1 record per session (Table 3 audit log)
        2. level_results: all qualifying level touches in 09:15-09:28 (Table 1 matrix)
        3. confluence_results: all qualifying multi-factor setup reactions (Table 2 matrix)
        4. phase2_results: all Phase 2 comparison trades (Table 4 comparison)
    """
    touch_tol = cfg.get("touch_tolerance_pts", 5.0)
    stop_width = cfg.get("breakout_stop_pts", 20.0)
    confluence_dist = cfg.get("confluence_proximity_pts", 15.0)
    wick_ratio = cfg.get("rejection_wick_ratio", 0.30)
    rejection_sl_buf = cfg.get("rejection_sl_buffer_pts", 5.0)
    pdl_bd_trigger = cfg.get("pdl_breakdown_trigger_pts", 3.0)
    pdl_bd_sl = cfg.get("pdl_breakdown_sl_pts", 15.0)

    log.info("Computing continuous 1H 8-EMA across all sessions...")
    df_1h = compute_standard_1h_8ema(df_1m, span=cfg.get("h1_ema_span", 8))

    log.info("Computing 15m candles for swing structure analysis...")
    df_15m = compute_15m_candles(df_1m)

    sessions = sorted(df_1m["dt"].unique())
    log.info("Running zero-lookahead backtest across %d sessions...", len(sessions))

    m1_by_date = {dt: g.sort_values("tm").reset_index(drop=True) for dt, g in df_1m.groupby("dt")}
    m15_by_date = {dt: g.sort_values("b15").reset_index(drop=True) for dt, g in df_15m.groupby("dt")}
    h1_last_ema_by_date = {dt: g.iloc[-1]["ema_8"] for dt, g in df_1h.groupby("dt")}

    session_records: List[LevelReactionRecord] = []
    level_results: List[ReactionResult] = []
    confluence_results: List[ReactionResult] = []
    phase2_results: List[ReactionResult] = []

    for idx in range(3, len(sessions)):
        date_d = sessions[idx]
        date_prev = sessions[idx - 1]
        prev_3_dates = sessions[idx - 3 : idx]

        prev_m1 = m1_by_date.get(date_prev)
        if prev_m1 is None or prev_m1.empty:
            continue

        pdh = float(prev_m1["high"].max())
        pdl = float(prev_m1["low"].min())
        pdc = float(prev_m1.iloc[-1]["close"])
        pdo = float(prev_m1.iloc[0]["open"])

        prev_1h_8ema = float(h1_last_ema_by_date.get(date_prev, 0.0))
        if prev_1h_8ema == 0.0:
            continue

        bars_3d_list = [m15_by_date[d] for d in prev_3_dates if d in m15_by_date]
        if bars_3d_list:
            bars_3d = pd.concat(bars_3d_list, ignore_index=True)
            has_db, db_lvl, has_dt, dt_lvl = identify_3d_patterns(bars_3d, pdh, pdl, prev_1h_8ema, cfg)
        else:
            has_db, db_lvl, has_dt, dt_lvl = False, None, False, None

        levels = PreMarketLevels(
            date=date_d,
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
            pdo=pdo,
            prev_1h_8ema=prev_1h_8ema,
            double_bottom_3d=has_db,
            db_support_level=db_lvl,
            double_top_3d=has_dt,
            dt_resistance_level=dt_lvl,
        )

        day_d_m1 = m1_by_date.get(date_d)
        if day_d_m1 is None or day_d_m1.empty:
            continue

        bars_15m_window = day_d_m1[
            (day_d_m1["tm"] >= "09:15:00") & (day_d_m1["tm"] <= "09:29:00")
        ].sort_values("tm").reset_index(drop=True)

        if len(bars_15m_window) < 15:
            continue

        open_915 = float(bars_15m_window.iloc[0]["open"])
        open_vs_1h = "ABOVE_1H_8EMA" if open_915 > prev_1h_8ema else "BELOW_1H_8EMA"

        candidate_levels: List[Dict[str, Any]] = [
            {"name": "PDH", "price": levels.pdh, "can_support": True, "can_resist": True},
            {"name": "PDL", "price": levels.pdl, "can_support": True, "can_resist": True},
            {"name": "PDC", "price": levels.pdc, "can_support": True, "can_resist": True},
            {"name": "PDO", "price": levels.pdo, "can_support": True, "can_resist": True},
            {"name": "PREV_1H_8EMA", "price": levels.prev_1h_8ema, "can_support": (open_vs_1h == "ABOVE_1H_8EMA"), "can_resist": (open_vs_1h == "BELOW_1H_8EMA")},
        ]
        if levels.double_bottom_3d and levels.db_support_level is not None:
            candidate_levels.append({
                "name": "DOUBLE_BOTTOM_3D",
                "price": levels.db_support_level,
                "can_support": True,
                "can_resist": False,
            })
        if levels.double_top_3d and levels.dt_resistance_level is not None:
            candidate_levels.append({
                "name": "DOUBLE_TOP_3D",
                "price": levels.dt_resistance_level,
                "can_support": False,
                "can_resist": True,
            })

        # ─────────────────────────────────────────────────────────────────────
        # Part A: Table 1 Level Touches (Blind Limit Touch)
        # ─────────────────────────────────────────────────────────────────────
        session_level_touches: List[Tuple[int, float, Dict[str, Any], str, str, float]] = []

        for cand in candidate_levels:
            lvl_name = cand["name"]
            lvl_p = cand["price"]

            # Test from below (Resistance / Short)
            if cand["can_resist"] and open_915 < (lvl_p - touch_tol):
                for b_i in range(14):
                    if float(bars_15m_window.iloc[b_i]["high"]) >= (lvl_p - touch_tol):
                        entry = min(float(bars_15m_window.iloc[b_i]["high"]), lvl_p)
                        sl = lvl_p + stop_width
                        res = evaluate_level_reaction_path(
                            bars_15m_window, lvl_name, lvl_p, "SHORT", "Resistance", b_i, entry, sl, cfg
                        )
                        level_results.append(res)
                        session_level_touches.append((b_i, lvl_p - open_915, cand, "SHORT", "Resistance", entry))
                        break

            # Test from above (Support / Long)
            if cand["can_support"] and open_915 > (lvl_p + touch_tol):
                for b_i in range(14):
                    if float(bars_15m_window.iloc[b_i]["low"]) <= (lvl_p + touch_tol):
                        entry = max(float(bars_15m_window.iloc[b_i]["low"]), lvl_p)
                        sl = lvl_p - stop_width
                        res = evaluate_level_reaction_path(
                            bars_15m_window, lvl_name, lvl_p, "LONG", "Support", b_i, entry, sl, cfg
                        )
                        level_results.append(res)
                        session_level_touches.append((b_i, open_915 - lvl_p, cand, "LONG", "Support", entry))
                        break

        # ─────────────────────────────────────────────────────────────────────
        # Part B: Table 3 Session Primary First-Touch Trade
        # ─────────────────────────────────────────────────────────────────────
        if session_level_touches:
            session_level_touches.sort(key=lambda x: (x[0], x[1]))
            first_b_idx, _, first_cand, first_dir, first_approach, first_entry = session_level_touches[0]
            first_lvl_name = first_cand["name"]
            first_lvl_price = first_cand["price"]

            stacked_levels = [
                c["name"] for c in candidate_levels
                if c["name"] != first_lvl_name and abs(c["price"] - first_lvl_price) <= confluence_dist
            ]

            is_regime_aligned = (
                (first_dir == "SHORT" and open_vs_1h == "BELOW_1H_8EMA")
                or (first_dir == "LONG" and open_vs_1h == "ABOVE_1H_8EMA")
            )
            confluence_count = 1 + (1 if is_regime_aligned else 0) + len(stacked_levels)

            if first_dir == "SHORT" and open_vs_1h == "BELOW_1H_8EMA" and first_lvl_name == "PDH":
                combo_name = "BELOW_1H_8EMA + Touch PDH"
            elif first_dir == "SHORT" and open_vs_1h == "BELOW_1H_8EMA" and first_lvl_name == "PDC":
                combo_name = "BELOW_1H_8EMA + Touch PDC"
            elif first_dir == "LONG" and open_vs_1h == "ABOVE_1H_8EMA" and first_lvl_name == "PDL" and levels.double_bottom_3d:
                combo_name = "ABOVE_1H_8EMA + Touch PDL + DOUBLE_BOTTOM_3D"
            elif first_dir == "LONG" and open_vs_1h == "ABOVE_1H_8EMA" and first_lvl_name == "PREV_1H_8EMA" and abs(levels.pdl - levels.prev_1h_8ema) <= confluence_dist:
                combo_name = "ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support + PDL within 15 pts"
            elif first_lvl_name in ("PDO", "PDC") and len(stacked_levels) == 0 and not is_regime_aligned:
                combo_name = "Single Level Touch (PDO / PDC alone with 0 confluence)"
            else:
                elements = [f"Touch_{first_lvl_name}"]
                if is_regime_aligned:
                    elements.insert(0, open_vs_1h)
                for s in stacked_levels:
                    elements.append(f"+{s}")
                combo_name = " ".join(elements)

            sl_first = first_lvl_price + stop_width if first_dir == "SHORT" else first_lvl_price - stop_width
            res_first = evaluate_level_reaction_path(
                bars_15m_window, first_lvl_name, first_lvl_price, first_dir, first_approach,
                first_b_idx, first_entry, sl_first, cfg, combo_name, confluence_count
            )

            rec = LevelReactionRecord(
                date=date_d,
                open_915=round(open_915, 2),
                pdh=round(levels.pdh, 2),
                pdl=round(levels.pdl, 2),
                pdc=round(levels.pdc, 2),
                pdo=round(levels.pdo, 2),
                prev_1h_8ema=round(levels.prev_1h_8ema, 2),
                open_vs_1h_8ema=open_vs_1h,
                double_bottom_3d=levels.double_bottom_3d,
                first_level_touched=first_lvl_name,
                touch_time_ist=res_first.touch_time_ist,
                direction=first_dir,
                confluence_combo=combo_name,
                confluence_count=confluence_count,
                entry_price=res_first.entry_price,
                level_price=res_first.level_price,
                stop_price=res_first.stop_price,
                mfe_before_sl_pts=res_first.mfe_before_sl_pts,
                mae_pts=res_first.mae_pts,
                hit_25_plus=res_first.hit_25_plus,
                hit_40_plus=res_first.hit_40_plus,
                hit_50_plus=res_first.hit_50_plus,
                outcome_bucket=res_first.outcome_bucket,
                close_930_pts=res_first.close_930_pts,
                resolution_quality=res_first.resolution_quality,
                opt_net_pnl_inr=res_first.opt_net_pnl_inr_40m,
                opt_net_pnl_inr_40m=res_first.opt_net_pnl_inr_40m,
                opt_net_pnl_inr_25m=res_first.opt_net_pnl_inr_25m,
                is_optimistic_win_40=res_first.is_optimistic_win_40,
            )
            session_records.append(rec)
        else:
            rec = LevelReactionRecord(
                date=date_d,
                open_915=round(open_915, 2),
                pdh=round(levels.pdh, 2),
                pdl=round(levels.pdl, 2),
                pdc=round(levels.pdc, 2),
                pdo=round(levels.pdo, 2),
                prev_1h_8ema=round(levels.prev_1h_8ema, 2),
                open_vs_1h_8ema=open_vs_1h,
                double_bottom_3d=levels.double_bottom_3d,
                first_level_touched="NO_TOUCH",
                touch_time_ist="",
                direction="",
                confluence_combo="NO_TOUCH",
                confluence_count=0,
                entry_price=0.0,
                level_price=0.0,
                stop_price=0.0,
                mfe_before_sl_pts=0.0,
                mae_pts=0.0,
                hit_25_plus=False,
                hit_40_plus=False,
                hit_50_plus=False,
                outcome_bucket="NO_TOUCH",
                close_930_pts=0.0,
                resolution_quality="CLEAN",
                opt_net_pnl_inr=0.0,
                opt_net_pnl_inr_40m=0.0,
                opt_net_pnl_inr_25m=0.0,
                is_optimistic_win_40=False,
            )
            session_records.append(rec)

        # ─────────────────────────────────────────────────────────────────────
        # Part C: Table 2 Confluence Combinations (Blind Touch)
        # ─────────────────────────────────────────────────────────────────────
        # 1. BELOW_1H_8EMA + Touch PDH
        if open_vs_1h == "BELOW_1H_8EMA" and open_915 < (pdh - touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["high"]) >= (pdh - touch_tol):
                    entry = min(float(bars_15m_window.iloc[b_i]["high"]), pdh)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDH", pdh, "SHORT", "Resistance", b_i, entry, pdh + stop_width, cfg,
                        "BELOW_1H_8EMA + Touch PDH", 2
                    )
                    confluence_results.append(res)
                    break

        # 2. BELOW_1H_8EMA + Touch PDC
        if open_vs_1h == "BELOW_1H_8EMA" and open_915 < (pdc - touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["high"]) >= (pdc - touch_tol):
                    entry = min(float(bars_15m_window.iloc[b_i]["high"]), pdc)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDC", pdc, "SHORT", "Resistance", b_i, entry, pdc + stop_width, cfg,
                        "BELOW_1H_8EMA + Touch PDC", 2
                    )
                    confluence_results.append(res)
                    break

        # 3. ABOVE_1H_8EMA + Touch PDL + DOUBLE_BOTTOM_3D
        if open_vs_1h == "ABOVE_1H_8EMA" and levels.double_bottom_3d and open_915 > (pdl + touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["low"]) <= (pdl + touch_tol):
                    entry = max(float(bars_15m_window.iloc[b_i]["low"]), pdl)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDL", pdl, "LONG", "Support", b_i, entry, pdl - stop_width, cfg,
                        "ABOVE_1H_8EMA + Touch PDL + DOUBLE_BOTTOM_3D", 3
                    )
                    confluence_results.append(res)
                    break

        # 4. ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support + PDL within 15 pts
        if open_vs_1h == "ABOVE_1H_8EMA" and abs(pdl - prev_1h_8ema) <= confluence_dist and open_915 > (prev_1h_8ema + touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["low"]) <= (prev_1h_8ema + touch_tol):
                    entry = max(float(bars_15m_window.iloc[b_i]["low"]), prev_1h_8ema)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PREV_1H_8EMA", prev_1h_8ema, "LONG", "Support", b_i, entry, prev_1h_8ema - stop_width, cfg,
                        "ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support + PDL within 15 pts", 3
                    )
                    confluence_results.append(res)
                    break

        # 5. Single Level Touch (PDO / PDC alone with 0 confluence)
        for check_name, check_p in [("PDC", pdc), ("PDO", pdo)]:
            other_near = any(c["name"] != check_name and abs(c["price"] - check_p) <= confluence_dist for c in candidate_levels)
            if not other_near:
                if open_vs_1h != "BELOW_1H_8EMA" and open_915 < (check_p - touch_tol):
                    for b_i in range(14):
                        if float(bars_15m_window.iloc[b_i]["high"]) >= (check_p - touch_tol):
                            entry = min(float(bars_15m_window.iloc[b_i]["high"]), check_p)
                            res = evaluate_level_reaction_path(
                                bars_15m_window, check_name, check_p, "SHORT", "Resistance", b_i, entry, check_p + stop_width, cfg,
                                "Single Level Touch (PDO / PDC alone with 0 confluence)", 1
                            )
                            confluence_results.append(res)
                            break
                elif open_vs_1h != "ABOVE_1H_8EMA" and open_915 > (check_p + touch_tol):
                    for b_i in range(14):
                        if float(bars_15m_window.iloc[b_i]["low"]) <= (check_p + touch_tol):
                            entry = max(float(bars_15m_window.iloc[b_i]["low"]), check_p)
                            res = evaluate_level_reaction_path(
                                bars_15m_window, check_name, check_p, "LONG", "Support", b_i, entry, check_p - stop_width, cfg,
                                "Single Level Touch (PDO / PDC alone with 0 confluence)", 1
                            )
                            confluence_results.append(res)
                            break

        # 6. ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support
        if open_vs_1h == "ABOVE_1H_8EMA" and open_915 > (prev_1h_8ema + touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["low"]) <= (prev_1h_8ema + touch_tol):
                    entry = max(float(bars_15m_window.iloc[b_i]["low"]), prev_1h_8ema)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PREV_1H_8EMA", prev_1h_8ema, "LONG", "Support", b_i, entry, prev_1h_8ema - stop_width, cfg,
                        "ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support", 2
                    )
                    confluence_results.append(res)
                    break

        # 7. BELOW_1H_8EMA + Touch PREV_1H_8EMA Resistance
        if open_vs_1h == "BELOW_1H_8EMA" and open_915 < (prev_1h_8ema - touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["high"]) >= (prev_1h_8ema - touch_tol):
                    entry = min(float(bars_15m_window.iloc[b_i]["high"]), prev_1h_8ema)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PREV_1H_8EMA", prev_1h_8ema, "SHORT", "Resistance", b_i, entry, prev_1h_8ema + stop_width, cfg,
                        "BELOW_1H_8EMA + Touch PREV_1H_8EMA Resistance", 2
                    )
                    confluence_results.append(res)
                    break

        # 8. Touch PDH + DOUBLE_TOP_3D
        if levels.double_top_3d and open_915 < (pdh - touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["high"]) >= (pdh - touch_tol):
                    entry = min(float(bars_15m_window.iloc[b_i]["high"]), pdh)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDH", pdh, "SHORT", "Resistance", b_i, entry, pdh + stop_width, cfg,
                        "Touch PDH + DOUBLE_TOP_3D", 2
                    )
                    confluence_results.append(res)
                    break

        # 9. Touch PDL + DOUBLE_BOTTOM_3D
        if levels.double_bottom_3d and open_915 > (pdl + touch_tol):
            for b_i in range(14):
                if float(bars_15m_window.iloc[b_i]["low"]) <= (pdl + touch_tol):
                    entry = max(float(bars_15m_window.iloc[b_i]["low"]), pdl)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDL", pdl, "LONG", "Support", b_i, entry, pdl - stop_width, cfg,
                        "Touch PDL + DOUBLE_BOTTOM_3D", 2
                    )
                    confluence_results.append(res)
                    break

        # ─────────────────────────────────────────────────────────────────────
        # Part D: Phase 2 Head-to-Head Refinements
        # ─────────────────────────────────────────────────────────────────────
        # D1. Refinement A — Confirmed 1m Rejection Candle Close on PDH
        if open_915 < (pdh - touch_tol):
            for t in range(13):  # bar t in [09:15, 09:27]
                b = bars_15m_window.iloc[t]
                bh = float(b["high"])
                bl = float(b["low"])
                bc = float(b["close"])
                br = bh - bl
                # Rule: high >= PDH - 5.0, close <= PDH, upper rejection wick >= 30% of bar range
                if bh >= (pdh - touch_tol) and bc <= pdh and br > 0 and (bh - bc) >= (wick_ratio * br):
                    entry = bc
                    sl = max(bh + rejection_sl_buf, pdh + stop_width)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDH", pdh, "SHORT", "Resistance", t, entry, sl, cfg,
                        "PDH Confirmed 1M Rejection Close", 1, "CONFIRMED_1M_REJECTION_CLOSE", is_confirmed_close=True
                    )
                    phase2_results.append(res)
                    break

        # D2. Refinement A — Confirmed 1m Rejection Candle Close on PDH + DOUBLE_TOP_3D
        if levels.double_top_3d and open_915 < (pdh - touch_tol):
            for t in range(13):
                b = bars_15m_window.iloc[t]
                bh = float(b["high"])
                bl = float(b["low"])
                bc = float(b["close"])
                br = bh - bl
                if bh >= (pdh - touch_tol) and bc <= pdh and br > 0 and (bh - bc) >= (wick_ratio * br):
                    entry = bc
                    sl = max(bh + rejection_sl_buf, pdh + stop_width)
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDH", pdh, "SHORT", "Resistance", t, entry, sl, cfg,
                        "Touch PDH + DOUBLE_TOP_3D (Wick Confirmed)", 2, "CONFIRMED_1M_REJECTION_CLOSE", is_confirmed_close=True
                    )
                    phase2_results.append(res)
                    break

        # D3. Refinement B — PDL Breakdown Continuation Short (All Regimes)
        if open_915 > pdl:
            for t in range(13):  # bar t in [09:15, 09:27]
                b = bars_15m_window.iloc[t]
                bc = float(b["close"])
                if bc <= (pdl - pdl_bd_trigger):
                    entry = bc
                    sl = pdl + pdl_bd_sl
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDL", pdl, "SHORT", "Breakdown", t, entry, sl, cfg,
                        "PDL Breakdown Short (All Regimes)", 1, "BREAKDOWN_CONTINUATION", is_confirmed_close=True
                    )
                    phase2_results.append(res)
                    break

        # D4. Refinement B — BELOW_1H_8EMA + PDL Breakdown Continuation Short (Regime Aligned)
        if open_915 > pdl and open_vs_1h == "BELOW_1H_8EMA":
            for t in range(13):
                b = bars_15m_window.iloc[t]
                bc = float(b["close"])
                if bc <= (pdl - pdl_bd_trigger):
                    entry = bc
                    sl = pdl + pdl_bd_sl
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDL", pdl, "SHORT", "Breakdown", t, entry, sl, cfg,
                        "BELOW_1H_8EMA + PDL Breakdown Short", 2, "BREAKDOWN_CONTINUATION", is_confirmed_close=True
                    )
                    phase2_results.append(res)
                    break

        # D5. Refinement B — PDL + DOUBLE_BOTTOM_3D Breakdown Continuation Short
        if open_915 > pdl and levels.double_bottom_3d:
            for t in range(13):
                b = bars_15m_window.iloc[t]
                bc = float(b["close"])
                if bc <= (pdl - pdl_bd_trigger):
                    entry = bc
                    sl = pdl + pdl_bd_sl
                    res = evaluate_level_reaction_path(
                        bars_15m_window, "PDL", pdl, "SHORT", "Breakdown", t, entry, sl, cfg,
                        "PDL + DOUBLE_BOTTOM_3D Breakdown Short", 2, "BREAKDOWN_CONTINUATION", is_confirmed_close=True
                    )
                    phase2_results.append(res)
                    break

    log.info(
        "Backtest completed: %d sessions evaluated, %d total level tests, %d confluence triggers, %d phase2 refinement trades.",
        len(session_records),
        len(level_results),
        len(confluence_results),
        len(phase2_results),
    )
    return session_records, level_results, confluence_results, phase2_results


# ─────────────────────────────────────────────────────────────────────────────
# 6. TABLE BUILDERS & STATISTICAL EDGE VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def build_table_1_matrix(level_results: List[ReactionResult]) -> pd.DataFrame:
    """Table 1 — Level-by-Level Performance Matrix (Phase 1 Baseline)."""
    if not level_results:
        return pd.DataFrame()

    df = pd.DataFrame([asdict(r) for r in level_results])
    rows = []

    for (lvl, approach), g in df.groupby(["level_name", "approach"]):
        n = len(g)
        ambig = int((g["resolution_quality"] == "AMBIGUOUS_SAME_BAR").sum())
        win_25 = (g["hit_25_plus"].sum() / n) * 100.0
        win_40 = (g["hit_40_plus"].sum() / n) * 100.0
        win_50 = (g["hit_50_plus"].sum() / n) * 100.0
        stall = ((g["outcome_bucket"] == "<25_PTS").sum() / n) * 100.0
        fail = ((g["outcome_bucket"] == "BREAKOUT_FAIL").sum() / n) * 100.0
        avg_mfe = float(g["mfe_before_sl_pts"].mean())
        avg_mae = float(g["mae_pts"].mean())
        avg_pnl = float(g["opt_net_pnl_inr_40m"].mean())

        rows.append({
            "Level Name": lvl,
            "Approach": approach,
            "Total Touches": n,
            "Ambiguous Bars": ambig,
            "Win >=25 Pts %": round(win_25, 1),
            "Win >=40 Pts %": round(win_40, 1),
            "Win >=50 Pts %": round(win_50, 1),
            "<25 Pts Stall %": round(stall, 1),
            "Breakout Fail %": round(fail, 1),
            "Avg MFE (Pts)": round(avg_mfe, 1),
            "Avg MAE (Pts)": round(avg_mae, 1),
            "Est Net ₹/Trade": round(avg_pnl, 2),
        })

    order_map = {"PDH": 1, "PDL": 2, "PDC": 3, "PDO": 4, "PREV_1H_8EMA": 5, "DOUBLE_BOTTOM_3D": 6, "DOUBLE_TOP_3D": 7}
    rows.sort(key=lambda x: (order_map.get(x["Level Name"], 99), x["Approach"]))

    tot_n = len(df)
    tot_ambig = int((df["resolution_quality"] == "AMBIGUOUS_SAME_BAR").sum())
    rows.append({
        "Level Name": "ALL_LEVELS_AGGREGATE",
        "Approach": "Combined",
        "Total Touches": tot_n,
        "Ambiguous Bars": tot_ambig,
        "Win >=25 Pts %": round((df["hit_25_plus"].sum() / tot_n) * 100.0, 1),
        "Win >=40 Pts %": round((df["hit_40_plus"].sum() / tot_n) * 100.0, 1),
        "Win >=50 Pts %": round((df["hit_50_plus"].sum() / tot_n) * 100.0, 1),
        "<25 Pts Stall %": round(((df["outcome_bucket"] == "<25_PTS").sum() / tot_n) * 100.0, 1),
        "Breakout Fail %": round(((df["outcome_bucket"] == "BREAKOUT_FAIL").sum() / tot_n) * 100.0, 1),
        "Avg MFE (Pts)": round(float(df["mfe_before_sl_pts"].mean()), 1),
        "Avg MAE (Pts)": round(float(df["mae_pts"].mean()), 1),
        "Est Net ₹/Trade": round(float(df["opt_net_pnl_inr_40m"].mean()), 2),
    })

    return pd.DataFrame(rows)


def build_table_2_matrix(
    confluence_results: List[ReactionResult],
    session_records: List[LevelReactionRecord],
) -> pd.DataFrame:
    """Table 2 — Multi-Factor Confluence Combinations (Single vs. Double vs. Triple Confluence)."""
    if not confluence_results:
        return pd.DataFrame()

    df_conf = pd.DataFrame([asdict(r) for r in confluence_results])
    df_sess = pd.DataFrame([asdict(r) for r in session_records if r.first_level_touched != "NO_TOUCH"])

    focus_setups = [
        ("BELOW_1H_8EMA + Touch PDH", "SHORT"),
        ("BELOW_1H_8EMA + Touch PDC", "SHORT"),
        ("ABOVE_1H_8EMA + Touch PDL + DOUBLE_BOTTOM_3D", "LONG"),
        ("ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support + PDL within 15 pts", "LONG"),
        ("Single Level Touch (PDO / PDC alone with 0 confluence)", "BASELINE"),
        ("BELOW_1H_8EMA + Touch PREV_1H_8EMA Resistance", "SHORT"),
        ("ABOVE_1H_8EMA + Touch PREV_1H_8EMA Support", "LONG"),
        ("Touch PDH + DOUBLE_TOP_3D", "SHORT"),
        ("Touch PDL + DOUBLE_BOTTOM_3D", "LONG"),
    ]

    rows = []
    for setup_name, dir_label in focus_setups:
        sub = df_conf[df_conf["confluence_combo"] == setup_name]
        n = len(sub)
        if n == 0:
            rows.append({
                "Confluence Setup / Tier": setup_name,
                "Direction": dir_label,
                "Touches": 0,
                "Win >=25 Pts %": 0.0,
                "Win >=40 Pts %": 0.0,
                "Win >=50 Pts %": 0.0,
                "Stall <25 %": 0.0,
                "Breakout Fail %": 0.0,
                "Avg MFE (Pts)": 0.0,
                "Avg MAE (Pts)": 0.0,
                "Est Net ₹/Trade": 0.0,
            })
            continue

        rows.append({
            "Confluence Setup / Tier": setup_name,
            "Direction": dir_label,
            "Touches": n,
            "Win >=25 Pts %": round((sub["hit_25_plus"].sum() / n) * 100.0, 1),
            "Win >=40 Pts %": round((sub["hit_40_plus"].sum() / n) * 100.0, 1),
            "Win >=50 Pts %": round((sub["hit_50_plus"].sum() / n) * 100.0, 1),
            "Stall <25 %": round(((sub["outcome_bucket"] == "<25_PTS").sum() / n) * 100.0, 1),
            "Breakout Fail %": round(((sub["outcome_bucket"] == "BREAKOUT_FAIL").sum() / n) * 100.0, 1),
            "Avg MFE (Pts)": round(float(sub["mfe_before_sl_pts"].mean()), 1),
            "Avg MAE (Pts)": round(float(sub["mae_pts"].mean()), 1),
            "Est Net ₹/Trade": round(float(sub["opt_net_pnl_inr_40m"].mean()), 2),
        })

    for tier_label, cond in [
        ("Tier 1: Single Level (1 Factor)", df_sess["confluence_count"] <= 1),
        ("Tier 2: Double Confluence (2 Factors)", df_sess["confluence_count"] == 2),
        ("Tier 3: Triple+ Confluence (3+ Factors)", df_sess["confluence_count"] >= 3),
    ]:
        sub = df_sess[cond]
        n = len(sub)
        if n > 0:
            rows.append({
                "Confluence Setup / Tier": f"== {tier_label} ==",
                "Direction": "ALL",
                "Touches": n,
                "Win >=25 Pts %": round((sub["hit_25_plus"].sum() / n) * 100.0, 1),
                "Win >=40 Pts %": round((sub["hit_40_plus"].sum() / n) * 100.0, 1),
                "Win >=50 Pts %": round((sub["hit_50_plus"].sum() / n) * 100.0, 1),
                "Stall <25 %": round(((sub["outcome_bucket"] == "<25_PTS").sum() / n) * 100.0, 1),
                "Breakout Fail %": round(((sub["outcome_bucket"] == "BREAKOUT_FAIL").sum() / n) * 100.0, 1),
                "Avg MFE (Pts)": round(float(sub["mfe_before_sl_pts"].mean()), 1),
                "Avg MAE (Pts)": round(float(sub["mae_pts"].mean()), 1),
                "Est Net ₹/Trade": round(float(sub["opt_net_pnl_inr_40m"].mean()), 2),
            })

    return pd.DataFrame(rows)


def build_table_4_head_to_head(
    level_results: List[ReactionResult],
    confluence_results: List[ReactionResult],
    phase2_results: List[ReactionResult],
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    """
    Table 4 — Phase 2 Head-to-Head Comparison:
    Compares:
      1. PDH Short: Blind Touch vs. Confirmed 1M Rejection Close
      2. PDH + DOUBLE_TOP_3D: Blind Touch vs. Confirmed 1M Rejection Close
      3. PDL Support Bounce (Blind Touch) vs. PDL Breakdown Continuation Short (All Regimes)
      4. PDL Support Bounce (Blind Touch) vs. BELOW_1H_8EMA + PDL Breakdown Continuation Short
      5. PDL + DOUBLE_BOTTOM_3D Long vs. PDL + DOUBLE_BOTTOM_3D Breakdown Short
    """
    breakeven_p = cfg.get("breakeven_win_rate", 0.365)
    all_trades = level_results + confluence_results + phase2_results
    df = pd.DataFrame([asdict(r) for r in all_trades])

    comparison_specs = [
        # (Label, Filter condition, Target model)
        ("1a. PDH Short (Phase 1 Blind Touch)", (df["level_name"] == "PDH") & (df["approach"] == "Resistance") & (df["entry_mode"] == "BLIND_TOUCH")),
        ("1b. PDH Short (Phase 2 Confirmed 1M Rejection Close)", df["confluence_combo"] == "PDH Confirmed 1M Rejection Close"),
        ("2a. Touch PDH + DOUBLE_TOP_3D (Phase 1 Blind Touch)", df["confluence_combo"] == "Touch PDH + DOUBLE_TOP_3D"),
        ("2b. Touch PDH + DOUBLE_TOP_3D (Phase 2 Wick Confirmed)", df["confluence_combo"] == "Touch PDH + DOUBLE_TOP_3D (Wick Confirmed)"),
        ("3a. PDL Support Bounce (Phase 1 Blind Touch - Failed 63.8%)", (df["level_name"] == "PDL") & (df["approach"] == "Support") & (df["entry_mode"] == "BLIND_TOUCH")),
        ("3b. PDL Breakdown Short (Phase 2 All Regimes)", df["confluence_combo"] == "PDL Breakdown Short (All Regimes)"),
        ("4a. BELOW_1H_8EMA + PDL Support Bounce (Phase 1 Blind Touch)", (df["level_name"] == "PDL") & (df["approach"] == "Support") & (df["entry_mode"] == "BLIND_TOUCH")),
        ("4b. BELOW_1H_8EMA + PDL Breakdown Short (Phase 2 Regime-Aligned)", df["confluence_combo"] == "BELOW_1H_8EMA + PDL Breakdown Short"),
        ("5a. Touch PDL + DOUBLE_BOTTOM_3D Long (Phase 1 Blind Touch)", df["confluence_combo"] == "Touch PDL + DOUBLE_BOTTOM_3D"),
        ("5b. Touch PDL + DOUBLE_BOTTOM_3D Breakdown Short (Phase 2)", df["confluence_combo"] == "PDL + DOUBLE_BOTTOM_3D Breakdown Short"),
    ]

    rows = []
    for label, cond in comparison_specs:
        sub = df[cond].drop_duplicates(subset=["touch_bar_idx", "entry_price", "stop_price", "mfe_before_sl_pts", "confluence_combo"])
        n = len(sub)
        if n == 0:
            continue
        win_25 = (sub["hit_25_plus"].sum() / n) * 100.0
        win_40 = (sub["hit_40_plus"].sum() / n) * 100.0
        fail_rate = ((sub["outcome_bucket"] == "BREAKOUT_FAIL").sum() / n) * 100.0
        avg_risk = float(sub["risk_pts"].mean())
        avg_mfe = float(sub["mfe_before_sl_pts"].mean())
        pnl_25 = float(sub["opt_net_pnl_inr_25m"].mean())
        pnl_40 = float(sub["opt_net_pnl_inr_40m"].mean())

        # t-stat vs 36.5% break-even
        se_null = math.sqrt(breakeven_p * (1.0 - breakeven_p) / n) if n > 0 else 1.0
        t_stat_40 = ((win_40 / 100.0) - breakeven_p) / se_null if se_null > 0 else 0.0

        rows.append({
            "Strategy Variant": label,
            "N": n,
            "Win >=25 Pts %": round(win_25, 1),
            "Win >=40 Pts %": round(win_40, 1),
            "Breakout Fail %": round(fail_rate, 1),
            "Avg Risk (Pts)": round(avg_risk, 1),
            "Avg MFE (Pts)": round(avg_mfe, 1),
            "Net ₹ (25pt Tgt)": round(pnl_25, 2),
            "Net ₹ (40pt Tgt)": round(pnl_40, 2),
            "t-stat (40pt BE)": round(t_stat_40, 2),
        })

    return pd.DataFrame(rows)


def export_table_3_audit_log(records: List[LevelReactionRecord], csv_path: Path) -> None:
    """Exports Session-by-Session Audit Log CSV matching Table 3 specification."""
    fieldnames = [
        "Date", "Open_915", "PDH", "PDL", "PDC", "PDO", "Prev_1H_8EMA",
        "Open_vs_1H_8EMA", "Double_Bottom_3D", "First_Level_Touched",
        "Touch_Time_IST", "Direction (LONG/SHORT)", "Confluence_Combo",
        "MFE_Before_SL_Pts", "MAE_Pts", "Hit_25_Plus", "Hit_40_Plus",
        "Hit_50_Plus", "Outcome_Bucket", "Close_930_Pts",
    ]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "Date": r.date,
                "Open_915": f"{r.open_915:.2f}",
                "PDH": f"{r.pdh:.2f}",
                "PDL": f"{r.pdl:.2f}",
                "PDC": f"{r.pdc:.2f}",
                "PDO": f"{r.pdo:.2f}",
                "Prev_1H_8EMA": f"{r.prev_1h_8ema:.2f}",
                "Open_vs_1H_8EMA": r.open_vs_1h_8ema,
                "Double_Bottom_3D": "TRUE" if r.double_bottom_3d else "FALSE",
                "First_Level_Touched": r.first_level_touched,
                "Touch_Time_IST": r.touch_time_ist,
                "Direction (LONG/SHORT)": r.direction,
                "Confluence_Combo": r.confluence_combo,
                "MFE_Before_SL_Pts": f"{r.mfe_before_sl_pts:.2f}",
                "MAE_Pts": f"{r.mae_pts:.2f}",
                "Hit_25_Plus": "TRUE" if r.hit_25_plus else "FALSE",
                "Hit_40_Plus": "TRUE" if r.hit_40_plus else "FALSE",
                "Hit_50_Plus": "TRUE" if r.hit_50_plus else "FALSE",
                "Outcome_Bucket": r.outcome_bucket,
                "Close_930_Pts": f"{r.close_930_pts:.2f}",
            })
    log.info("Saved Table 3 Audit Log to %s (%d rows)", csv_path, len(records))


def compute_statistical_edge_verdict(
    confluence_results: List[ReactionResult],
    phase2_results: List[ReactionResult],
    session_records: List[LevelReactionRecord],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Ranks Top Confluence Setups across Phase 1 and Phase 2."""
    breakeven_p = cfg.get("breakeven_win_rate", 0.365)
    min_n = cfg.get("min_sample_size", 30)

    combined_results = confluence_results + phase2_results
    df_conf = pd.DataFrame([asdict(r) for r in combined_results])
    setup_stats = []

    for combo, sub in df_conf.groupby("confluence_combo"):
        n = len(sub)
        if n == 0:
            continue
        wins_40 = int(sub["hit_40_plus"].sum())
        wins_25 = int(sub["hit_25_plus"].sum())
        fails = int((sub["outcome_bucket"] == "BREAKOUT_FAIL").sum())
        win_rate_40 = wins_40 / n
        win_rate_25 = wins_25 / n
        fail_rate = fails / n

        se_null = math.sqrt(breakeven_p * (1.0 - breakeven_p) / n) if n > 0 else 1.0
        t_stat = (win_rate_40 - breakeven_p) / se_null if se_null > 0 else 0.0
        p_val = float(stats.norm.sf(t_stat))

        pnl_mean_40 = float(sub["opt_net_pnl_inr_40m"].mean())
        pnl_mean_25 = float(sub["opt_net_pnl_inr_25m"].mean())
        pnl_std = float(sub["opt_net_pnl_inr_40m"].std()) if n > 1 else 0.0
        pnl_se = pnl_std / math.sqrt(n) if n > 0 else 1.0
        t_stat_pnl = pnl_mean_40 / pnl_se if pnl_se > 0 else 0.0

        setup_stats.append({
            "setup": combo,
            "direction": sub["direction"].iloc[0] if sub["direction"].nunique() == 1 else "MIXED",
            "N": n,
            "wins_40": wins_40,
            "win_rate_40_pct": round(win_rate_40 * 100.0, 2),
            "wins_25": wins_25,
            "win_rate_25_pct": round(win_rate_25 * 100.0, 2),
            "fails": fails,
            "fail_rate_pct": round(fail_rate * 100.0, 2),
            "t_stat_prop": round(t_stat, 3),
            "p_val_prop": round(p_val, 4),
            "avg_net_inr_40m": round(pnl_mean_40, 2),
            "avg_net_inr_25m": round(pnl_mean_25, 2),
            "t_stat_pnl": round(t_stat_pnl, 3),
            "has_enough_sample": (n >= min_n),
        })

    filtered = [s for s in setup_stats if "Single Level Touch" not in s["setup"] and not s["setup"].startswith("==")]

    qualifying = [s for s in filtered if s["has_enough_sample"]]
    qualifying.sort(key=lambda x: (x["win_rate_40_pct"], -x["fail_rate_pct"]), reverse=True)

    all_sorted = sorted(filtered, key=lambda x: (x["has_enough_sample"], x["win_rate_40_pct"], -x["fail_rate_pct"]), reverse=True)

    top2 = qualifying[:2] if len(qualifying) >= 2 else all_sorted[:2]
    baseline = next((s for s in setup_stats if "Single Level Touch" in s["setup"]), None)

    return {
        "breakeven_p": breakeven_p,
        "min_n": min_n,
        "top2": top2,
        "baseline": baseline,
        "all_sorted": all_sorted,
    }


def format_cli_table(df: pd.DataFrame, title: str) -> str:
    """Formats DataFrame as a clean ASCII tabular grid."""
    if df.empty:
        return f"\n--- {title} ---\n[Empty Table]\n"

    headers = list(df.columns)
    col_widths = [max(len(str(h)), max(len(str(v)) for v in df[h].values)) for h in headers]

    header_line = " | ".join(f"{h:<{w}}" for h, w in zip(headers, col_widths))
    divider_line = "-+-".join("-" * w for w in col_widths)

    rows_str = []
    for _, row in df.iterrows():
        line = " | ".join(f"{str(row[h]):<{w}}" for h, w in zip(headers, col_widths))
        rows_str.append(line)

    box_divider = "=" * len(header_line)
    return f"\n{box_divider}\n{title.center(len(header_line))}\n{box_divider}\n{header_line}\n{divider_line}\n" + "\n".join(rows_str) + f"\n{box_divider}\n"


def print_and_save_full_report(
    session_records: List[LevelReactionRecord],
    table1: pd.DataFrame,
    table2: pd.DataFrame,
    table4: pd.DataFrame,
    verdict: Dict[str, Any],
    cfg: Dict[str, Any],
    output_md_path: Path,
) -> None:
    """Prints comprehensive CLI report and writes markdown summary artifact."""
    total_sessions = len(session_records)
    active_records = [r for r in session_records if r.first_level_touched != "NO_TOUCH"]
    active_trades = len(active_records)
    ambiguous_total = sum(1 for r in active_records if r.resolution_quality == "AMBIGUOUS_SAME_BAR")

    pessimistic_wins = sum(1 for r in active_records if r.hit_40_plus)
    optimistic_wins = sum(1 for r in active_records if r.is_optimistic_win_40)

    report_lines: List[str] = []
    def _out(msg: str = ""):
        print(msg)
        report_lines.append(msg)

    _out("=" * 125)
    _out("   NIFTY 50 OPENING 15-MINUTE (09:15-09:30 IST) PRE-MARKET LEVEL REACTION & CONFLUENCE BACKTESTER")
    _out("=" * 125)
    _out(f"Session Coverage        : {total_sessions} completed trading sessions ({session_records[0].date} to {session_records[-1].date})")
    _out(f"Opening Trade Activity  : {active_trades} sessions touched a pre-market level ({active_trades/total_sessions*100:.1f}%)")
    _out(f"Touch Tolerance Zone    : {cfg.get('touch_tolerance_pts', 5.0)} Spot points")
    _out(f"Breakout Invalidation SL: {cfg.get('breakout_stop_pts', 20.0)} Spot points beyond level")
    _out(f"Confluence Proximity    : {cfg.get('confluence_proximity_pts', 15.0)} Spot points")
    _out(f"Lot Size & Cost Model   : Lot Size = 65 | ATM Delta = 0.55 | Total Charges + Slippage = ₹110.50 / trade")
    _out(f"Break-Even Hurdle       : {cfg.get('breakeven_win_rate', 0.365)*100:.1f}% Win Rate for 40pt Target vs 20pt SL")
    _out(f"Ambiguity Guard         : {ambiguous_total} bars hit both 20pt SL and +25pt Target in the same minute")
    _out(f"  - Pessimistic Win >=40: {pessimistic_wins} ({pessimistic_wins/active_trades*100:.1f}%) [Breakout Fail counted first]")
    _out(f"  - Optimistic Win >=40 : {optimistic_wins} ({optimistic_wins/active_trades*100:.1f}%) [Favorable move counted first]")

    # Table 1
    t1_str = format_cli_table(table1, "TABLE 1 — LEVEL-BY-LEVEL PERFORMANCE MATRIX (PHASE 1 BASELINE)")
    _out(t1_str)

    # Table 2
    t2_str = format_cli_table(table2, "TABLE 2 — MULTI-FACTOR CONFLUENCE COMBINATIONS (PHASE 1 BASELINE)")
    _out(t2_str)

    # Table 4 (Phase 2 Head-to-Head Comparison)
    t4_str = format_cli_table(table4, "TABLE 4 — PHASE 2 HEAD-TO-HEAD COMPARISON (BLIND TOUCH vs. WICK CONFIRMATION vs. PDL BREAKDOWN)")
    _out(t4_str)

    # Statistical Edge Verdict
    _out("=" * 125)
    _out("                          STATISTICAL EDGE VERDICT & SETUP RANKINGS")
    _out("=" * 125)

    top2 = verdict.get("top2", [])
    if len(top2) >= 1:
        s1 = top2[0]
        n_flag1 = "ENOUGH_SAMPLE (N >= 30)" if s1["has_enough_sample"] else "NOT_ENOUGH_SAMPLE (N < 30)"
        _out(f"\n[RANK #1 TOP CONFLUENCE SETUP]: {s1['setup']} ({s1['direction']})")
        _out(f"  - Sample Size (N)   : {s1['N']} [{n_flag1}]")
        _out(f"  - Win >= 40 Pts Rate: {s1['win_rate_40_pct']}% ({s1['wins_40']}/{s1['N']})")
        _out(f"  - Win >= 25 Pts Rate: {s1['win_rate_25_pct']}% ({s1['wins_25']}/{s1['N']})")
        _out(f"  - Breakout Fail Rate: {s1['fail_rate_pct']}% ({s1['fails']}/{s1['N']})")
        _out(f"  - t-stat vs 36.5% BE: {s1['t_stat_prop']:+.3f} (p-value = {s1['p_val_prop']:.4f})")
        _out(f"  - Est Net PnL (40pt): ₹{s1['avg_net_inr_40m']:+.2f} / lot (t_pnl = {s1['t_stat_pnl']:+.2f})")
        _out(f"  - Est Net PnL (25pt): ₹{s1['avg_net_inr_25m']:+.2f} / lot")

    if len(top2) >= 2:
        s2 = top2[1]
        n_flag2 = "ENOUGH_SAMPLE (N >= 30)" if s2["has_enough_sample"] else "NOT_ENOUGH_SAMPLE (N < 30)"
        _out(f"\n[RANK #2 TOP CONFLUENCE SETUP]: {s2['setup']} ({s2['direction']})")
        _out(f"  - Sample Size (N)   : {s2['N']} [{n_flag2}]")
        _out(f"  - Win >= 40 Pts Rate: {s2['win_rate_40_pct']}% ({s2['wins_40']}/{s2['N']})")
        _out(f"  - Win >= 25 Pts Rate: {s2['win_rate_25_pct']}% ({s2['wins_25']}/{s2['N']})")
        _out(f"  - Breakout Fail Rate: {s2['fail_rate_pct']}% ({s2['fails']}/{s2['N']})")
        _out(f"  - t-stat vs 36.5% BE: {s2['t_stat_prop']:+.3f} (p-value = {s2['p_val_prop']:.4f})")
        _out(f"  - Est Net PnL (40pt): ₹{s2['avg_net_inr_40m']:+.2f} / lot (t_pnl = {s2['t_stat_pnl']:+.2f})")
        _out(f"  - Est Net PnL (25pt): ₹{s2['avg_net_inr_25m']:+.2f} / lot")

    baseline = verdict.get("baseline")
    if baseline:
        _out(f"\n[CONTROL BASELINE]: {baseline['setup']}")
        _out(f"  - Sample Size (N)   : {baseline['N']}")
        _out(f"  - Win >= 40 Pts Rate: {baseline['win_rate_40_pct']}% | Fail Rate: {baseline['fail_rate_pct']}%")
        _out(f"  - Est Net PnL/Trade : ₹{baseline['avg_net_inr_40m']:+.2f} / lot")

    _out("\n" + "=" * 125 + "\n")

    try:
        output_md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines) + "\n")
        log.info("Saved summary report to %s", output_md_path)
    except Exception as e:
        log.error("Could not write markdown summary: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# 7. MAIN CLI RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="NIFTY 50 Opening 15-Minute (09:15-09:30 IST) Level Reaction & Confluence Backtester."
    )
    parser.add_argument("--source", choices=["auto", "db", "csv"], default="auto", help="Data source: auto (merge CSV+DB), db, or csv.")
    parser.add_argument("--start-date", type=str, default=None, help="Filter start date YYYY-MM-DD.")
    parser.add_argument("--end-date", type=str, default=None, help="Filter end date YYYY-MM-DD.")
    parser.add_argument("--touch-tol", type=float, default=None, help="Touch tolerance zone in points (e.g. 3.0, 5.0, 8.0).")
    parser.add_argument("--stop-pts", type=float, default=None, help="Breakout invalidation stop in points beyond level (default 20.0).")
    parser.add_argument("--confluence-dist", type=float, default=None, help="Confluence clustering distance in points (default 15.0).")
    parser.add_argument("--csv-out", type=str, default=None, help="Path to write session audit CSV.")
    parser.add_argument("--report-out", type=str, default=None, help="Path to write markdown report.")

    args = parser.parse_args()

    cfg = load_backtest_config()

    if args.touch_tol is not None:
        cfg["touch_tolerance_pts"] = args.touch_tol
    if args.stop_pts is not None:
        cfg["breakout_stop_pts"] = args.stop_pts
    if args.confluence_dist is not None:
        cfg["confluence_proximity_pts"] = args.confluence_dist

    source = args.source or cfg.get("default_source", "auto")
    csv_path = Path(args.csv_out or cfg.get("output_csv_path", "analyst/reports/nifty_915_930_level_reactions.csv"))
    report_path = Path(args.report_out or cfg.get("output_report_path", "analyst/reports/nifty_915_930_level_reactions_summary.md"))

    if not csv_path.is_absolute():
        csv_path = ROOT / csv_path
    if not report_path.is_absolute():
        report_path = ROOT / report_path

    t0 = time.time()

    # Step 1: Load clean 1m data
    df_1m = load_clean_1m_candles(
        source=source,
        csv_rel=cfg.get("csv_path", "data/ohlc/nifty_1m_dhan.csv"),
        db_rel=cfg.get("sqlite_path", "ict_smc_app/backend/data/ict_smc.sqlite3"),
        start_date=args.start_date,
        end_date=args.end_date,
    )

    # Step 2: Run zero-lookahead backtest (Phase 1 + Phase 2)
    session_records, level_results, confluence_results, phase2_results = run_915_level_backtest(df_1m, cfg)

    if not session_records:
        log.error("No records produced. Exiting.")
        return 1

    # Step 3: Build deliverables
    table1 = build_table_1_matrix(level_results)
    table2 = build_table_2_matrix(confluence_results, session_records)
    table4 = build_table_4_head_to_head(level_results, confluence_results, phase2_results, cfg)
    verdict = compute_statistical_edge_verdict(confluence_results, phase2_results, session_records, cfg)

    # Step 4: Export Table 3 CSV Audit Log
    export_table_3_audit_log(session_records, csv_path)

    # Step 5: Print CLI report and save markdown artifact
    print_and_save_full_report(session_records, table1, table2, table4, verdict, cfg, report_path)

    elapsed = time.time() - t0
    log.info("Full execution completed in %.2f seconds.", elapsed)
    print(f"\n[DELIVERABLE FILE PATHS]:")
    print(f"1. Engine Script : {Path(__file__).resolve()}")
    print(f"2. Audit Log CSV : {csv_path.resolve()}")
    print(f"3. Summary Report: {report_path.resolve()}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
