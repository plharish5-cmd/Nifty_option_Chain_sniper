"""
algo_execution_engine/backtest_setup1.py — Event-Driven Historical Backtester for Setup #1 (Stage 2 8 EMA Long).

Event-Driven Architecture:
  1. Data Ingestion: Connects read-only to ict_smc.sqlite3, pulling all available historical sessions from candles_1m (sec_id='13').
  2. Zero Look-Ahead Resampling: Resamples 1m bars into 5m and 15m candles strictly as-of timestamp t.
  3. Replay of Strategy Logic (strategy_setup1.py):
     - 15m Major Trend Filter: 4 EMA > 8 EMA and BB width > 60.
     - 5m Setup #1 Preconditions: 4 EMA > 8 EMA (Party-Switch), UBB sloping UP >= 2 consecutive 5m candles.
     - Intrabar Entry Trigger: Exact touch of 5m 8 EMA before 15:00 IST.
     - First-Opportunity Rule: Only 1st touch per 4>8 rally (skips 2nd/3rd touches until 4<8 reset).
     - Daily Stop Limit: -40 pt Spot SL disables Setup #1 for the rest of that session.
     - Gap Day Filter: Compares performance on regular vs major gap days (|open - prev_close| >= 60 pts).
  4. Part K Money Math & Option Modeling:
     - 1 Lot = 75 qty (configurable).
     - Asymmetric ITM Delta Model (Trading Bible Part G): +0.60 delta on wins, cushioned 0.50 delta on stops.
     - Linear 0.60 Delta Model: Flat 0.60 delta on all moves.
     - Fixed Round-Trip Brokerage & Taxes: ₹400.00 per trade.
  5. Trailing Stop & Risk Automation:
     - Initial SL: -40 pts.
     - Cost-to-Cost: Once price moves +40 pts in favor, move SL to entry price.
     - Candle Trail: On each confirming 5m candle close > entry, trail SL to candle low.
     - Volatility Exit: 5m candle range >= 100 pts or UBB curls FLAT.
     - Time Exit: 15:20 IST intraday square-off.
  6. Ablation Comparison Across 4 Variants + Null Baseline with Statistical Significance & GO / NO-GO Verdict.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

from .strategy_setup1 import (
    add_bollinger,
    add_course_emas,
    band_slope,
    load_config,
    prepare_frame,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("algo.backtest")

CONFIG_PATH = Path(__file__).parent / "config.json"


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    session_date: str
    entry_ts: int
    entry_time: str
    entry_spot: float
    exit_ts: int
    exit_time: str
    exit_spot: float
    spot_pnl_pts: float
    exit_reason: str
    cost_to_cost_activated: bool
    highest_spot: float
    trailed_sl: float
    # Option Money Math
    asym_opt_pts: float
    asym_gross_inr: float
    asym_net_inr: float
    linear_opt_pts: float
    linear_gross_inr: float
    linear_net_inr: float
    is_gap_day: bool
    gap_pts: float
    variant: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# HISTORICAL DATA LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_historical_1m_data(sqlite_path: str) -> pd.DataFrame:
    """Loads all historical 1m candles for NIFTY 50 (sec_id='13') read-only."""
    full_path = Path(sqlite_path)
    if not full_path.is_absolute():
        full_path = Path(__file__).resolve().parent.parent / sqlite_path

    if not full_path.exists():
        raise FileNotFoundError(f"Database not found at {full_path}")

    log.info("Loading historical 1m data from %s (read-only)...", full_path)
    uri = f"file:{full_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)

    query = """
        SELECT ts, o as open, h as high, l as low, c as close, v as volume,
               date(ts, 'unixepoch', '+05:30') as session_date,
               time(ts, 'unixepoch', '+05:30') as session_time
        FROM candles_1m
        WHERE sec_id = '13'
        ORDER BY ts ASC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    # Filter to intraday session 09:15 to 15:30 IST
    df = df[(df["session_time"] >= "09:15:00") & (df["session_time"] <= "15:30:00")].copy()
    df = df.reset_index(drop=True)

    log.info("Loaded %d clean 1m intraday candles across %d unique sessions", len(df), df["session_date"].nunique())
    return df


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-TIMEFRAME RESAMPLING & INDICATOR PREPARATION
# ─────────────────────────────────────────────────────────────────────────────

def build_multitimeframe_data(df_1m: pd.DataFrame, cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Constructs continuous 5m and 15m OHLCV dataframes with EMAs, Bollinger Bands, and UBB slopes.
    Aligned strictly to session boundaries with zero lookahead bias.
    """
    strat = cfg.get("strategy", {})
    bb_len = int(strat.get("bb_length", 20))
    bb_mult = float(strat.get("bb_mult", 2.0))
    fast = int(strat.get("ema_fast", 4))
    slow = int(strat.get("ema_slow", 8))
    slope_candles = int(strat.get("ubb_slope_min_candles", 2))
    slope_tol = float(strat.get("ubb_slope_tol", 1.5))

    df = df_1m.copy()
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime")

    # Resample 5m
    df_5m = df.resample("5min", closed="left", label="left").agg({
        "ts": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "session_date": "first",
        "session_time": "first",
    }).dropna().reset_index()

    df_5m = add_bollinger(df_5m, bb_len, bb_mult)
    df_5m = add_course_emas(df_5m, fast, slow)

    # Compute rolling UBB slope on 5m
    slopes = ["UNKNOWN"] * len(df_5m)
    for i in range(slope_candles, len(df_5m)):
        sub = df_5m["ubb"].iloc[i - slope_candles:i + 1]
        slopes[i] = band_slope(sub, candles=slope_candles, tol=slope_tol)
    df_5m["ubb_slope"] = slopes

    # Resample 15m
    df_15m = df.resample("15min", closed="left", label="left").agg({
        "ts": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "session_date": "first",
        "session_time": "first",
    }).dropna().reset_index()

    df_15m = add_bollinger(df_15m, bb_len, bb_mult)
    df_15m = add_course_emas(df_15m, fast, slow)

    return df_5m, df_15m


# ─────────────────────────────────────────────────────────────────────────────
# EVENT-DRIVEN SIMULATION CORE
# ─────────────────────────────────────────────────────────────────────────────

class Setup1BacktestSimulator:
    """Simulates Setup #1 execution minute-by-minute with exact state machine."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = cfg or load_config()
        self.strat_cfg = self.cfg.get("strategy", {})
        self.backtest_cfg = self.cfg.get("backtest", {})
        self.risk_cfg = self.cfg.get("risk", {})

        self.fixed_sl = float(self.strat_cfg.get("fixed_sl_pts", 40.0))
        self.target_pts = float(self.strat_cfg.get("target_pts", 40.0))
        self.ctc_trigger_pts = float(self.strat_cfg.get("cost_to_cost_trigger_pts", 40.0))
        self.vol_candle_pts = float(self.strat_cfg.get("volatility_candle_pts", 100.0))
        self.bb_width_min_15m = float(self.strat_cfg.get("bb_width_min_15m", 60.0))
        self.touch_prox = float(self.strat_cfg.get("touch_proximity_pts", 2.5))

        self.lot_size = int(self.backtest_cfg.get("lot_size", 75))
        self.brokerage = float(self.backtest_cfg.get("brokerage_inr_per_trade", 400.0))
        self.asym_win_delta = float(self.backtest_cfg.get("itm_delta_win", 0.60))
        self.asym_loss_delta = float(self.backtest_cfg.get("itm_delta_loss", 0.50))
        self.linear_delta = float(self.backtest_cfg.get("linear_delta", 0.60))
        self.gap_threshold = float(self.backtest_cfg.get("gap_threshold_pts", 60.0))
        self.square_off_time = str(self.backtest_cfg.get("square_off_time", "15:20"))

    def run_all(self, df_1m: pd.DataFrame) -> Dict[str, Any]:
        """Runs the backtest across all historical sessions for all 4 ablation variants + null baseline."""
        df_5m, df_15m = build_multitimeframe_data(df_1m, self.cfg)

        # Precompute maps for O(1) lookups: ts -> row
        map_5m = {r["ts"]: r for r in df_5m.to_dict(orient="records")}
        map_15m = {r["ts"]: r for r in df_15m.to_dict(orient="records")}
        list_5m_ts = sorted(map_5m.keys())
        list_15m_ts = sorted(map_15m.keys())

        # Group 1m by session date
        sessions = [group for _, group in df_1m.groupby("session_date", sort=True)]
        log.info("Prepared %d daily sessions for simulation", len(sessions))

        # Run 4 variants
        variants = [
            ("Variant A (Raw 8 EMA Touch)", {"trend_filter": False, "ubb_filter": False, "first_opp": False, "daily_stop": False, "ctc": False, "trail": False}),
            ("Variant B (Trend Filter Only)", {"trend_filter": True, "ubb_filter": False, "first_opp": False, "daily_stop": False, "ctc": False, "trail": False}),
            ("Variant C (Trend + Preconditions)", {"trend_filter": True, "ubb_filter": True, "first_opp": False, "daily_stop": False, "ctc": False, "trail": False}),
            ("Variant D (Full Setup #1 Trading Bible)", {"trend_filter": True, "ubb_filter": True, "first_opp": True, "daily_stop": True, "ctc": True, "trail": True}),
        ]

        variant_results = {}
        for var_name, var_rules in variants:
            log.info("Running simulation for %s...", var_name)
            trades = self._simulate_dataset(sessions, map_5m, list_5m_ts, map_15m, list_15m_ts, var_name, var_rules)
            variant_results[var_name] = trades

        # Run Null / Random Baseline
        log.info("Running Null Baseline simulation...")
        null_trades = self._simulate_null_baseline(sessions, map_15m, list_15m_ts)
        variant_results["Null Baseline (Random Longs)"] = null_trades

        # Build comprehensive report
        report = self._build_report(sessions, variant_results)
        return report

    def _simulate_dataset(
        self,
        sessions: List[pd.DataFrame],
        map_5m: Dict[int, Any],
        list_5m_ts: List[int],
        map_15m: Dict[int, Any],
        list_15m_ts: List[int],
        variant_name: str,
        rules: Dict[str, bool],
    ) -> List[BacktestTrade]:
        trades: List[BacktestTrade] = []
        prev_close: Optional[float] = None

        for session_df in sessions:
            if session_df.empty:
                continue

            session_date = session_df.iloc[0]["session_date"]
            open_price = session_df.iloc[0]["open"]
            gap_pts = (open_price - prev_close) if prev_close is not None else 0.0
            is_gap_day = bool(abs(gap_pts) >= self.gap_threshold)
            prev_close = session_df.iloc[-1]["close"]

            session_trades = self._simulate_single_session(
                session_df=session_df,
                map_5m=map_5m,
                list_5m_ts=list_5m_ts,
                map_15m=map_15m,
                list_15m_ts=list_15m_ts,
                is_gap_day=is_gap_day,
                gap_pts=gap_pts,
                variant_name=variant_name,
                rules=rules,
            )
            trades.extend(session_trades)

        return trades

    def _simulate_single_session(
        self,
        session_df: pd.DataFrame,
        map_5m: Dict[int, Any],
        list_5m_ts: List[int],
        map_15m: Dict[int, Any],
        list_15m_ts: List[int],
        is_gap_day: bool,
        gap_pts: float,
        variant_name: str,
        rules: Dict[str, bool],
    ) -> List[BacktestTrade]:
        trades: List[BacktestTrade] = []
        session_date = session_df.iloc[0]["session_date"]

        # Intraday State
        active_trade: Optional[Dict[str, Any]] = None
        rally_id = 0
        in_bullish_party = False
        touch_count = 0
        stage2_stopped_today = False
        last_closed_5m_ts = 0

        # Iterate 1m candles chronologically
        rows_1m = session_df.to_dict(orient="records")
        for bar in rows_1m:
            t = bar["ts"]
            bar_time = bar["session_time"]
            b_open = bar["open"]
            b_high = bar["high"]
            b_low = bar["low"]
            b_close = bar["close"]

            # Find latest CLOSED 5m and 15m bars strictly before or at t
            # A 5m bar starting at ts=X closes at X+300. So if t >= X+300 (X <= t-300), it is closed!
            closed_5m = None
            idx_5m = bisect.bisect_right(list_5m_ts, t - 300) - 1
            if idx_5m >= 0:
                closed_5m = map_5m.get(list_5m_ts[idx_5m])

            closed_15m = None
            idx_15m = bisect.bisect_right(list_15m_ts, t - 900) - 1
            if idx_15m >= 0:
                closed_15m = map_15m.get(list_15m_ts[idx_15m])

            # Update rally state based on closed 5m 4/8 EMA regime
            if closed_5m is not None:
                e4 = closed_5m.get("ema4", 0.0)
                e8 = closed_5m.get("ema8", 0.0)
                is_party_bullish = (e4 > e8)

                if is_party_bullish and not in_bullish_party:
                    rally_id += 1
                    in_bullish_party = True
                    touch_count = 0
                elif not is_party_bullish and in_bullish_party:
                    in_bullish_party = False

            # ─────────────────────────────────────────────────────────────
            # A. If in an Active Trade -> Manage Exits & Trailing Stop
            # ─────────────────────────────────────────────────────────────
            if active_trade is not None:
                entry_spot = active_trade["entry_spot"]
                cur_sl = active_trade["trailed_sl"]
                target = active_trade["target"]
                highest = max(active_trade["highest_spot"], b_high)
                active_trade["highest_spot"] = highest

                # Check Cost-to-Cost (+40 pts move in favor)
                if rules.get("ctc", True) and not active_trade["cost_to_cost_active"]:
                    if b_high - entry_spot >= self.ctc_trigger_pts:
                        active_trade["cost_to_cost_active"] = True
                        cur_sl = max(cur_sl, entry_spot)
                        active_trade["trailed_sl"] = cur_sl

                # Check Candle Trail on newly closed 5m bar
                if rules.get("trail", True) and closed_5m is not None and closed_5m["ts"] > last_closed_5m_ts:
                    last_closed_5m_ts = closed_5m["ts"]
                    c5_close = closed_5m["close"]
                    c5_low = closed_5m["low"]
                    c5_high = closed_5m["high"]

                    # Volatility Exit 1: Single 5m candle >= 100 pts
                    if (c5_high - c5_low) >= self.vol_candle_pts:
                        trade = self._close_trade(active_trade, t, bar_time, b_close, "Volatility Exit: 5m range >= 100 pts", is_gap_day, gap_pts, variant_name)
                        trades.append(trade)
                        active_trade = None
                        continue

                    # Volatility Exit 2: UBB curls FLAT
                    if closed_5m.get("ubb_slope") in ("FLAT", "DOWN"):
                        trade = self._close_trade(active_trade, t, bar_time, b_close, "Volatility Exit: UBB curled FLAT", is_gap_day, gap_pts, variant_name)
                        trades.append(trade)
                        active_trade = None
                        continue

                    # Candle Low Trail: if candle close > entry, trail SL to candle low
                    if c5_close > entry_spot and c5_low > cur_sl:
                        cur_sl = max(cur_sl, c5_low)
                        active_trade["trailed_sl"] = cur_sl

                # Check Intrabar SL or Target breach
                hit_sl = (b_low <= cur_sl)
                hit_tp = (b_high >= target)

                if hit_sl and hit_tp:
                    # Conservative tie-breaker: assume Stop-Loss hit first
                    exit_price = cur_sl
                    reason = "Stop-Loss Hit (-40 pts)" if cur_sl == entry_spot - self.fixed_sl else "Trailing/CTC Stop Hit"
                    if reason == "Stop-Loss Hit (-40 pts)":
                        stage2_stopped_today = True
                    trade = self._close_trade(active_trade, t, bar_time, exit_price, reason, is_gap_day, gap_pts, variant_name)
                    trades.append(trade)
                    active_trade = None
                    continue
                elif hit_sl:
                    exit_price = min(b_open, cur_sl) if b_open < cur_sl else cur_sl
                    reason = "Stop-Loss Hit (-40 pts)" if cur_sl == entry_spot - self.fixed_sl else "Trailing/CTC Stop Hit"
                    if reason == "Stop-Loss Hit (-40 pts)":
                        stage2_stopped_today = True
                    trade = self._close_trade(active_trade, t, bar_time, exit_price, reason, is_gap_day, gap_pts, variant_name)
                    trades.append(trade)
                    active_trade = None
                    continue
                elif hit_tp:
                    exit_price = max(b_open, target) if b_open > target else target
                    trade = self._close_trade(active_trade, t, bar_time, exit_price, "Target Hit (+40 pts)", is_gap_day, gap_pts, variant_name)
                    trades.append(trade)
                    active_trade = None
                    continue

                # Check Intraday Square-off (15:20 IST)
                if bar_time >= self.square_off_time:
                    trade = self._close_trade(active_trade, t, bar_time, b_close, "Intraday EOD Square-off (15:20)", is_gap_day, gap_pts, variant_name)
                    trades.append(trade)
                    active_trade = None
                    continue

                continue

            # ─────────────────────────────────────────────────────────────
            # B. If NO Active Trade -> Check Setup #1 Entry Signal
            # ─────────────────────────────────────────────────────────────
            if bar_time >= "15:00:00":
                continue  # No entries permitted after 15:00 IST

            if rules.get("daily_stop", True) and stage2_stopped_today:
                continue  # Stage 2 stop taken today

            if closed_5m is None:
                continue

            ema8_val = closed_5m.get("ema8", 0.0)
            ema4_val = closed_5m.get("ema4", 0.0)

            # Gate 1: 15-Minute Trend Filter (4>8 and BB width > 60)
            if rules.get("trend_filter", True):
                if closed_15m is None:
                    continue
                t15_e4 = closed_15m.get("ema4", 0.0)
                t15_e8 = closed_15m.get("ema8", 0.0)
                t15_width = closed_15m.get("bb_width", 0.0)
                if t15_e4 <= t15_e8 or t15_width <= self.bb_width_min_15m:
                    continue

            # Gate 2: 5-Minute Preconditions (4>8 and UBB slope UP)
            if rules.get("ubb_filter", True):
                if ema4_val <= ema8_val:
                    continue
                if closed_5m.get("ubb_slope") != "UP":
                    continue

            # Gate 3: 8 EMA Touch Trigger in current 1m bar
            # bar touches 8 EMA if low <= ema8 <= high or within proximity
            touch = (b_low <= ema8_val <= b_high) or (abs(b_low - ema8_val) <= self.touch_prox) or (abs(b_close - ema8_val) <= self.touch_prox)
            if not touch:
                continue

            # Gate 4: First Opportunity Rule in current rally
            if rules.get("first_opp", True):
                if not in_bullish_party or touch_count >= 1:
                    continue

            # All gates passed -> ENTER LONG
            entry_price = min(b_high, max(b_low, ema8_val))
            touch_count += 1

            active_trade = {
                "session_date": session_date,
                "entry_ts": t,
                "entry_time": bar_time,
                "entry_spot": entry_price,
                "target": entry_price + self.target_pts,
                "trailed_sl": entry_price - self.fixed_sl,
                "highest_spot": entry_price,
                "cost_to_cost_active": False,
            }

        return trades

    def _close_trade(
        self,
        active_trade: Dict[str, Any],
        exit_ts: int,
        exit_time: str,
        exit_spot: float,
        reason: str,
        is_gap_day: bool,
        gap_pts: float,
        variant_name: str,
    ) -> BacktestTrade:
        entry_spot = active_trade["entry_spot"]
        spot_pnl = exit_spot - entry_spot

        # Asymmetric ITM Delta Model (Trading Bible Part G)
        if spot_pnl > 0:
            asym_opt_pts = spot_pnl * self.asym_win_delta
        else:
            asym_opt_pts = spot_pnl * self.asym_loss_delta
        asym_gross = asym_opt_pts * self.lot_size
        asym_net = asym_gross - self.brokerage

        # Linear Conservative 0.60 Delta Model
        linear_opt_pts = spot_pnl * self.linear_delta
        linear_gross = linear_opt_pts * self.lot_size
        linear_net = linear_gross - self.brokerage

        return BacktestTrade(
            session_date=active_trade["session_date"],
            entry_ts=active_trade["entry_ts"],
            entry_time=active_trade["entry_time"],
            entry_spot=round(entry_spot, 2),
            exit_ts=exit_ts,
            exit_time=exit_time,
            exit_spot=round(exit_spot, 2),
            spot_pnl_pts=round(spot_pnl, 2),
            exit_reason=reason,
            cost_to_cost_activated=active_trade["cost_to_cost_active"],
            highest_spot=round(active_trade["highest_spot"], 2),
            trailed_sl=round(active_trade["trailed_sl"], 2),
            asym_opt_pts=round(asym_opt_pts, 2),
            asym_gross_inr=round(asym_gross, 2),
            asym_net_inr=round(asym_net, 2),
            linear_opt_pts=round(linear_opt_pts, 2),
            linear_gross_inr=round(linear_gross, 2),
            linear_net_inr=round(linear_net, 2),
            is_gap_day=is_gap_day,
            gap_pts=round(gap_pts, 2),
            variant=variant_name,
        )

    def _simulate_null_baseline(
        self,
        sessions: List[pd.DataFrame],
        map_15m: Dict[int, Any],
        list_15m_ts: List[int],
    ) -> List[BacktestTrade]:
        """
        Simulates random long entries placed during sessions to establish the empirical null baseline.
        Uses pseudo-random deterministic seed for reproducibility.
        """
        np.random.seed(42)
        trades: List[BacktestTrade] = []
        prev_close = None

        for session_df in sessions:
            if len(session_df) < 50:
                continue

            session_date = session_df.iloc[0]["session_date"]
            open_price = session_df.iloc[0]["open"]
            gap_pts = (open_price - prev_close) if prev_close is not None else 0.0
            is_gap_day = bool(abs(gap_pts) >= self.gap_threshold)
            prev_close = session_df.iloc[-1]["close"]

            # Pick a random minute between 09:30 and 14:30
            eligible_rows = session_df[(session_df["session_time"] >= "09:30:00") & (session_df["session_time"] <= "14:30:00")]
            if eligible_rows.empty:
                continue

            pick_idx = np.random.randint(0, len(eligible_rows))
            entry_bar = eligible_rows.iloc[pick_idx]
            entry_price = float(entry_bar["close"])
            target = entry_price + self.target_pts
            sl = entry_price - self.fixed_sl

            # Simulate path forward until EOD
            subsequent = session_df[session_df["ts"] > entry_bar["ts"]].to_dict(orient="records")
            exit_spot = entry_price
            exit_time = "15:20:00"
            exit_ts = entry_bar["ts"] + 3600
            exit_reason = "Intraday EOD Square-off"

            for b in subsequent:
                if b["session_time"] >= self.square_off_time:
                    exit_spot = b["close"]
                    exit_time = b["session_time"]
                    exit_ts = b["ts"]
                    break
                if b["low"] <= sl:
                    exit_spot = sl
                    exit_time = b["session_time"]
                    exit_ts = b["ts"]
                    exit_reason = "Stop-Loss Hit (-40 pts)"
                    break
                if b["high"] >= target:
                    exit_spot = target
                    exit_time = b["session_time"]
                    exit_ts = b["ts"]
                    exit_reason = "Target Hit (+40 pts)"
                    break

            trade = self._close_trade(
                active_trade={
                    "session_date": session_date,
                    "entry_ts": int(entry_bar["ts"]),
                    "entry_time": entry_bar["session_time"],
                    "entry_spot": entry_price,
                    "target": target,
                    "trailed_sl": sl,
                    "highest_spot": entry_price,
                    "cost_to_cost_active": False,
                },
                exit_ts=exit_ts,
                exit_time=exit_time,
                exit_spot=exit_spot,
                reason=exit_reason,
                is_gap_day=is_gap_day,
                gap_pts=gap_pts,
                variant_name="Null Baseline (Random Longs)",
            )
            trades.append(trade)

        return trades

    # ─────────────────────────────────────────────────────────────────────────
    # METRICS & STATISTICAL ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def _build_report(self, sessions: List[pd.DataFrame], variant_results: Dict[str, List[BacktestTrade]]) -> Dict[str, Any]:
        sessions_count = len(sessions)
        report: Dict[str, Any] = {
            "meta": {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_sessions_analyzed": sessions_count,
                "lot_size": self.lot_size,
                "brokerage_inr": self.brokerage,
                "asym_delta_win": self.asym_win_delta,
                "asym_delta_loss": self.asym_loss_delta,
                "linear_delta": self.linear_delta,
            },
            "variants": {},
            "statistical_comparison": {},
            "verdict": {},
        }

        for var_name, trades in variant_results.items():
            metrics = self._calculate_variant_metrics(trades, sessions_count)
            report["variants"][var_name] = metrics

        # Statistical t-test: Variant D vs Null Baseline
        full_setup_trades = variant_results.get("Variant D (Full Setup #1 Trading Bible)", [])
        null_trades = variant_results.get("Null Baseline (Random Longs)", [])
        if full_setup_trades and null_trades:
            setup_pnls = [t.linear_net_inr for t in full_setup_trades]
            null_pnls = [t.linear_net_inr for t in null_trades]
            t_stat, p_val = stats.ttest_ind(setup_pnls, null_pnls, equal_var=False)
            report["statistical_comparison"] = {
                "variant_d_mean_net_inr": round(float(np.mean(setup_pnls)), 2),
                "null_baseline_mean_net_inr": round(float(np.mean(null_pnls)), 2),
                "t_statistic": round(float(t_stat), 4),
                "p_value": round(float(p_val), 5),
                "statistically_significant": bool(p_val < 0.05 and t_stat > 0),
            }

        # Go / No-Go Verdict on Full Setup #1 (Variant D)
        var_d_metrics = report["variants"].get("Variant D (Full Setup #1 Trading Bible)", {})
        net_exp_inr = var_d_metrics.get("linear_net_expectancy_inr", 0.0)
        profit_factor = var_d_metrics.get("linear_net_profit_factor", 0.0)
        win_rate = var_d_metrics.get("win_rate_pct", 0.0)

        min_pf = float(self.backtest_cfg.get("profit_factor_min_verdict", 1.30))
        is_go = bool(net_exp_inr > 0 and profit_factor >= min_pf)

        verdict_text = "🟢 GO (APPROVED FOR REAL EXECUTION)" if is_go else "🔴 NO-GO (EDGE INSUFFICIENT AFTER BROKERAGE)"
        reason_text = (
            f"Net Expectancy ₹{net_exp_inr:+,.1f}/trade, Profit Factor {profit_factor:.2f} (>= {min_pf}), Win Rate {win_rate:.1f}%"
            if is_go else
            f"Expectancy ₹{net_exp_inr:+,.1f} or Profit Factor {profit_factor:.2f} did not clear minimum institutional hurdle ({min_pf})"
        )

        report["verdict"] = {
            "status": "GO" if is_go else "NO-GO",
            "verdict_badge": verdict_text,
            "reason": reason_text,
            "min_profit_factor_required": min_pf,
            "achieved_profit_factor": profit_factor,
            "achieved_net_expectancy_inr": net_exp_inr,
        }

        return report

    def _calculate_variant_metrics(self, trades: List[BacktestTrade], sessions_count: int) -> Dict[str, Any]:
        if not trades:
            return {
                "total_trades": 0,
                "trades_per_session": 0.0,
                "win_rate_pct": 0.0,
                "scratch_rate_pct": 0.0,
                "loss_rate_pct": 0.0,
                "net_profit_factor": 0.0,
                "expectancy_spot_pts": 0.0,
                "asym_net_expectancy_inr": 0.0,
                "linear_net_expectancy_inr": 0.0,
            }

        n = len(trades)
        wins = [t for t in trades if t.spot_pnl_pts > 0]
        losses = [t for t in trades if t.spot_pnl_pts < 0]
        scratches = [t for t in trades if t.spot_pnl_pts == 0]

        win_rate = len(wins) / n * 100.0
        loss_rate = len(losses) / n * 100.0
        scratch_rate = len(scratches) / n * 100.0

        # Spot Pts
        avg_win_pts = float(np.mean([t.spot_pnl_pts for t in wins])) if wins else 0.0
        avg_loss_pts = float(np.mean([abs(t.spot_pnl_pts) for t in losses])) if losses else 0.0
        exp_spot_pts = (win_rate / 100.0 * avg_win_pts) - (loss_rate / 100.0 * avg_loss_pts)

        # Asymmetric ITM Delta Money Math
        asym_net_wins = [t.asym_net_inr for t in trades if t.asym_net_inr > 0]
        asym_net_losses = [abs(t.asym_net_inr) for t in trades if t.asym_net_inr < 0]
        avg_asym_win_inr = float(np.mean(asym_net_wins)) if asym_net_wins else 0.0
        avg_asym_loss_inr = float(np.mean(asym_net_losses)) if asym_net_losses else 0.0
        asym_total_net_inr = float(sum(t.asym_net_inr for t in trades))
        asym_net_exp = asym_total_net_inr / n
        asym_pf = (sum(asym_net_wins) / sum(asym_net_losses)) if asym_net_losses and sum(asym_net_losses) > 0 else 999.0

        # Linear 0.60 Delta Money Math
        lin_net_wins = [t.linear_net_inr for t in trades if t.linear_net_inr > 0]
        lin_net_losses = [abs(t.linear_net_inr) for t in trades if t.linear_net_inr < 0]
        avg_lin_win_inr = float(np.mean(lin_net_wins)) if lin_net_wins else 0.0
        avg_lin_loss_inr = float(np.mean(lin_net_losses)) if lin_net_losses else 0.0
        lin_total_net_inr = float(sum(t.linear_net_inr for t in trades))
        lin_net_exp = lin_total_net_inr / n
        lin_pf = (sum(lin_net_wins) / sum(lin_net_losses)) if lin_net_losses and sum(lin_net_losses) > 0 else 999.0

        # Drawdown calculation
        cumulative_pnl = np.cumsum([t.linear_net_inr for t in trades])
        running_max = np.maximum.accumulate(cumulative_pnl)
        drawdowns = running_max - cumulative_pnl
        max_dd_inr = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        cum_pts = np.cumsum([t.spot_pnl_pts for t in trades])
        running_max_pts = np.maximum.accumulate(cum_pts)
        max_dd_pts = float(np.max(running_max_pts - cum_pts)) if len(cum_pts) > 0 else 0.0

        # Max consecutive losses
        max_consec_losses = 0
        cur_consec = 0
        for t in trades:
            if t.spot_pnl_pts < 0:
                cur_consec += 1
                if cur_consec > max_consec_losses:
                    max_consec_losses = cur_consec
            else:
                cur_consec = 0

        # Sharpe ratio
        daily_returns: Dict[str, float] = {}
        for t in trades:
            daily_returns[t.session_date] = daily_returns.get(t.session_date, 0.0) + t.linear_net_inr
        daily_vals = list(daily_returns.values())
        sharpe = (float(np.mean(daily_vals)) / float(np.std(daily_vals)) * math.sqrt(252)) if len(daily_vals) > 1 and float(np.std(daily_vals)) > 0 else 0.0

        # Gap day breakdown
        gap_trades = [t for t in trades if t.is_gap_day]
        nongap_trades = [t for t in trades if not t.is_gap_day]
        gap_win_rate = (len([t for t in gap_trades if t.spot_pnl_pts > 0]) / len(gap_trades) * 100.0) if gap_trades else 0.0
        nongap_win_rate = (len([t for t in nongap_trades if t.spot_pnl_pts > 0]) / len(nongap_trades) * 100.0) if nongap_trades else 0.0

        return {
            "total_trades": n,
            "trades_per_session": round(n / sessions_count, 2),
            "wins_count": len(wins),
            "losses_count": len(losses),
            "scratches_count": len(scratches),
            "win_rate_pct": round(win_rate, 1),
            "loss_rate_pct": round(loss_rate, 1),
            "scratch_rate_pct": round(scratch_rate, 1),
            "avg_win_spot_pts": round(avg_win_pts, 1),
            "avg_loss_spot_pts": round(avg_loss_pts, 1),
            "expectancy_spot_pts": round(exp_spot_pts, 2),
            # Asymmetric ITM Delta
            "asym_avg_win_net_inr": round(avg_asym_win_inr, 1),
            "asym_avg_loss_net_inr": round(avg_asym_loss_inr, 1),
            "asym_total_net_pnl_inr": round(asym_total_net_inr, 2),
            "asym_net_expectancy_inr": round(asym_net_exp, 2),
            "asym_net_profit_factor": round(asym_pf, 2),
            # Linear Conservative 0.60 Delta
            "linear_avg_win_net_inr": round(avg_lin_win_inr, 1),
            "linear_avg_loss_net_inr": round(avg_lin_loss_inr, 1),
            "linear_total_net_pnl_inr": round(lin_total_net_inr, 2),
            "linear_net_expectancy_inr": round(lin_net_exp, 2),
            "linear_net_profit_factor": round(lin_pf, 2),
            # Risk & Drawdown
            "max_drawdown_spot_pts": round(max_dd_pts, 1),
            "max_drawdown_net_inr": round(max_dd_inr, 2),
            "max_consecutive_losses": max_consec_losses,
            "annualized_sharpe_ratio": round(sharpe, 2),
            # Gap Breakdown
            "gap_day_trades": len(gap_trades),
            "gap_day_win_rate_pct": round(gap_win_rate, 1),
            "nongap_day_trades": len(nongap_trades),
            "nongap_day_win_rate_pct": round(nongap_win_rate, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# TERMINAL REPORT FORMATTER & MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def print_terminal_report(report: Dict[str, Any]) -> None:
    meta = report.get("meta", {})
    variants = report.get("variants", {})
    stat = report.get("statistical_comparison", {})
    verdict = report.get("verdict", {})

    print("\n" + "=" * 90)
    print("🎯 STAGE 2 EVENT-DRIVEN HISTORICAL BACKTEST REPORT: SETUP #1 (STAGE 2 8 EMA LONG)")
    print(f"   Database: ict_smc.sqlite3 | Total Sessions: {meta.get('total_sessions_analyzed')} | Timestamp: {meta.get('generated_at')}")
    print(f"   Money Math: 1 Lot ({meta.get('lot_size')} qty) | Round-Trip Brokerage: ₹{meta.get('brokerage_inr'):.0f} | Linear Delta: {meta.get('linear_delta'):.2f}")
    print("=" * 90)

    # Ablation Comparison Table
    print("\n📊 ABLATION & FEATURE VALUE COMPARISON TABLE:")
    print(f"{'Variant':<42} | {'Trades':<7} | {'Win%':<6} | {'Net PnL (INR)':<14} | {'Exp/Tr (INR)':<13} | {'PF':<5}")
    print("-" * 96)

    for vname, m in variants.items():
        n_tr = m.get("total_trades", 0)
        win_pct = m.get("win_rate_pct", 0.0)
        net_pnl = m.get("linear_total_net_pnl_inr", 0.0)
        exp_tr = m.get("linear_net_expectancy_inr", 0.0)
        pf = m.get("linear_net_profit_factor", 0.0)
        print(f"{vname:<42} | {n_tr:<7} | {win_pct:>5.1f}% | ₹{net_pnl:>12,.1f} | ₹{exp_tr:>11,.1f} | {pf:>4.2f}")

    print("-" * 96)

    # Detailed Metrics for Full Setup #1 (Variant D)
    d_metrics = variants.get("Variant D (Full Setup #1 Trading Bible)", {})
    print("\n🏆 DETAILED METRICS FOR VARIANT D (FULL SETUP #1 TRADING BIBLE):")
    print(f"  • Total Sessions Tested:        {meta.get('total_sessions_analyzed')}")
    print(f"  • Total Trades Fired:           {d_metrics.get('total_trades')} ({d_metrics.get('trades_per_session', 0):.2f} trades/session)")
    print(f"  • Win / Scratch / Loss:         {d_metrics.get('wins_count')} wins ({d_metrics.get('win_rate_pct')}%) | {d_metrics.get('scratches_count')} scratches ({d_metrics.get('scratch_rate_pct')}%) | {d_metrics.get('losses_count')} losses ({d_metrics.get('loss_rate_pct')}%)")
    print(f"  • Spot P&L Expectancy:          {d_metrics.get('expectancy_spot_pts'):+.2f} pts/trade (Avg Win: +{d_metrics.get('avg_win_spot_pts'):.1f} pts, Avg Loss: -{d_metrics.get('avg_loss_spot_pts'):.1f} pts)")
    print(f"  • Linear 0.60 Delta Net PnL:    ₹{d_metrics.get('linear_total_net_pnl_inr'):+,.2f} (Net Expectancy: ₹{d_metrics.get('linear_net_expectancy_inr'):+,.2f} / trade after ₹400 brokerage)")
    print(f"  • Asymmetric ITM Delta Net PnL: ₹{d_metrics.get('asym_total_net_pnl_inr'):+,.2f} (Net Expectancy: ₹{d_metrics.get('asym_net_expectancy_inr'):+,.2f} / trade)")
    print(f"  • Profit Factor:                {d_metrics.get('linear_net_profit_factor'):.2f} (Asymmetric ITM: {d_metrics.get('asym_net_profit_factor'):.2f})")
    print(f"  • Max Drawdown:                 ₹{d_metrics.get('max_drawdown_net_inr'):,.2f} ({d_metrics.get('max_drawdown_spot_pts'):.1f} spot pts)")
    print(f"  • Max Consecutive Losses:       {d_metrics.get('max_consecutive_losses')}")
    print(f"  • Annualized Sharpe Ratio:      {d_metrics.get('annualized_sharpe_ratio'):.2f}")
    print(f"  • Gap Day Nuance:               {d_metrics.get('gap_day_trades')} trades on gap days ({d_metrics.get('gap_day_win_rate_pct')}% Win) vs {d_metrics.get('nongap_day_trades')} on regular days ({d_metrics.get('nongap_day_win_rate_pct')}% Win)")

    # Statistical Comparison vs Null
    if stat:
        print("\n🔬 STATISTICAL HYPOTHESIS TEST VS NULL BASELINE:")
        print(f"  • Setup #1 Mean Net:            ₹{stat.get('variant_d_mean_net_inr'):+,.2f} / trade")
        print(f"  • Null Baseline Mean Net:       ₹{stat.get('null_baseline_mean_net_inr'):+,.2f} / trade")
        print(f"  • Welch's t-statistic:          {stat.get('t_statistic')}")
        print(f"  • p-value:                      {stat.get('p_value')}")
        sig_str = "YES (p < 0.05)" if stat.get("statistically_significant") else "NO (p >= 0.05)"
        print(f"  • Statistically Significant:    {sig_str}")

    # Verdict
    print("\n" + "=" * 90)
    print(f"⚖️ GO / NO-GO VERDICT: {verdict.get('verdict_badge')}")
    print(f"   Reason: {verdict.get('reason')}")
    print("=" * 90 + "\n")


def run_backtest_and_save(sqlite_path: str = "ict_smc_app/backend/data/ict_smc.sqlite3", output_json: str = "algo_execution_engine/backtest_report_setup1.json") -> Dict[str, Any]:
    cfg = load_config()
    db_p = cfg.get("execution", {}).get("sqlite_db_path", sqlite_path)
    df_1m = load_historical_1m_data(db_p)

    simulator = Setup1BacktestSimulator(cfg)
    report = simulator.run_all(df_1m)

    print_terminal_report(report)

    # Save to JSON
    out_path = Path(output_json)
    if not out_path.is_absolute():
        out_path = Path(__file__).resolve().parent.parent / output_json
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    log.info("Full backtest report saved to %s", out_path)
    return report


if __name__ == "__main__":
    run_backtest_and_save()
