#!/usr/bin/env python3
"""
scripts/backtest_futures_volume.py — NIFTY Futures Volume Surge Ratio & RVOL Backtest.

Implements the time-of-day normalized Relative Volume (RVOL) engine for NIFTY Futures,
scoping the analysis strictly to the available futures volume window (2026-09-09 to 2026-11-05).

Investigates:
  1. Combined Front + Next Month Volume (sec_id FUT + FUT2) vs Front-Only (sec_id FUT)
  2. Per-Bar Slot RVOL (compute_bar_rvol) vs Cumulative Daily RVOL (compute_rvol)
  3. Naive Flat 10-bar SMA vs Institutional Time-of-Day Corrected RVOL
  4. Full Decile-First Out-of-Sample Build Hurdle (PF >= 1.30, Ex-top5 > 0, Z >= 2.0 SD, Dir Adv > 0)
  5. Exact Dhan Statutory Charges (~Rs 78.50) & Configurable Slippage (0.25 pts/leg)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest.volume")

DB_PATH = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
OUT_DIR = ROOT / "scripts" / "out" / "futures_volume"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOT_SIZE = 65
OPT_BASE_ENTRY = 200.0
SLIPPAGE_PTS_PER_LEG = 0.25
SLIPPAGE_INR_PER_TRADE = round(2.0 * SLIPPAGE_PTS_PER_LEG * LOT_SIZE, 2)  # Rs 32.50
ASYM_WIN_DELTA = 0.60
ASYM_LOSS_DELTA = 0.50
FIXED_SL_PTS = 40.0
FIXED_TARGET_PTS = 40.0
SQUARE_OFF_TIME = "15:20:00"


# ─────────────────────────────────────────────────────────────────────────────
# 1. FUTURES DATA RESAMPLING & TIME-OF-DAY RVOL COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def load_and_resample_futures(db_path: Path) -> pd.DataFrame:
    """
    Loads 1m FUT and FUT2 candles from sqlite3 and resamples to 15m bars
    aligned to 09:15 NSE session boundaries.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    df_1m = pd.read_sql_query("""
        SELECT ts, sec_id, v FROM candles_1m
        WHERE sec_id IN ('FUT', 'FUT2')
        ORDER BY ts ASC
    """, conn)
    conn.close()

    p = df_1m.pivot(index="ts", columns="sec_id", values="v").fillna(0.0)
    if "FUT2" not in p.columns:
        p["FUT2"] = 0.0
    p["COMBINED"] = p["FUT"] + p["FUT2"]

    df = p.reset_index()
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df["session_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["session_time"] = df["datetime"].dt.strftime("%H:%M")
    df = df.set_index("datetime")

    df_15m = df.resample("15min", closed="left", label="left").agg({
        "ts": "first",
        "session_date": "first",
        "session_time": "first",
        "FUT": "sum",
        "FUT2": "sum",
        "COMBINED": "sum",
    }).dropna().reset_index()

    return df_15m


def compute_all_rvol_measures(df_15m: pd.DataFrame, filter_expiries: bool = True) -> pd.DataFrame:
    """
    Computes time-of-day normalized bar RVOL, cumulative RVOL, and flat SMA for both
    front-only (FUT) and combined (FUT + FUT2) contracts.
    """
    sessions = sorted(df_15m["session_date"].unique().tolist())
    df = df_15m.copy()

    # Cumulative volume within each day
    df["cum_vol_fut"] = df.groupby("session_date")["FUT"].cumsum()
    df["cum_vol_comb"] = df.groupby("session_date")["COMBINED"].cumsum()

    # Pre-index by (session_date, session_time)
    vol_map_fut = {(r["session_date"], r["session_time"]): r["FUT"] for _, r in df.iterrows()}
    cum_map_fut = {(r["session_date"], r["session_time"]): r["cum_vol_fut"] for _, r in df.iterrows()}
    vol_map_comb = {(r["session_date"], r["session_time"]): r["COMBINED"] for _, r in df.iterrows()}
    cum_map_comb = {(r["session_date"], r["session_time"]): r["cum_vol_comb"] for _, r in df.iterrows()}

    # Compute rolling flat 10-bar SMA for comparison
    df["flat_sma10_comb"] = df["COMBINED"].rolling(10).mean().replace(0, np.nan)
    df["flat_sma10_ratio"] = df["COMBINED"] / df["flat_sma10_comb"]

    comb_bar_rvol: List[float] = []
    comb_cum_rvol: List[float] = []
    fut_bar_rvol: List[float] = []
    fut_cum_rvol: List[float] = []

    for _, r in df.iterrows():
        s_date, s_time = r["session_date"], r["session_time"]

        # Prior non-expiry sessions for institutional baseline
        prior_dates = [d for d in sessions if d < s_date]
        if filter_expiries:
            # Tuesdays (1) & Thursdays (3) filtered out from normal baseline
            prior_dates = [d for d in prior_dates if datetime.strptime(d, "%Y-%m-%d").weekday() not in [1, 3]]

        base_dates = prior_dates[-10:]  # last 10 sessions

        if len(base_dates) < 3:
            comb_bar_rvol.append(np.nan)
            comb_cum_rvol.append(np.nan)
            fut_bar_rvol.append(np.nan)
            fut_cum_rvol.append(np.nan)
            continue

        # Combined RVOL
        b_comb = [vol_map_comb.get((d, s_time), 0.0) for d in base_dates]
        c_comb = [cum_map_comb.get((d, s_time), 0.0) for d in base_dates]
        mean_b_comb = float(np.mean(b_comb)) if b_comb else 0.0
        mean_c_comb = float(np.mean(c_comb)) if c_comb else 0.0
        comb_bar_rvol.append(round(r["COMBINED"] / mean_b_comb, 3) if mean_b_comb > 0 else np.nan)
        comb_cum_rvol.append(round(r["cum_vol_comb"] / mean_c_comb, 3) if mean_c_comb > 0 else np.nan)

        # Front-Only RVOL
        b_fut = [vol_map_fut.get((d, s_time), 0.0) for d in base_dates]
        c_fut = [cum_map_fut.get((d, s_time), 0.0) for d in base_dates]
        mean_b_fut = float(np.mean(b_fut)) if b_fut else 0.0
        mean_c_fut = float(np.mean(c_fut)) if c_fut else 0.0
        fut_bar_rvol.append(round(r["FUT"] / mean_b_fut, 3) if mean_b_fut > 0 else np.nan)
        fut_cum_rvol.append(round(r["cum_vol_fut"] / mean_c_fut, 3) if mean_c_fut > 0 else np.nan)

    df["comb_bar_rvol"] = comb_bar_rvol
    df["comb_cum_rvol"] = comb_cum_rvol
    df["fut_bar_rvol"] = fut_bar_rvol
    df["fut_cum_rvol"] = fut_cum_rvol

    return df


# ─────────────────────────────────────────────────────────────────────────────
# 2. DIRECTION CONTROL SHORT SIMULATION (MANDATORY CONTROL)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_direction_control_short(
    entry_ts: int,
    entry_spot: float,
    session_1m: List[Dict[str, Any]],
) -> Tuple[float, float, float, float, str, float]:
    """
    Mirror short trade entered at exact same bar.
    Evaluates short fixed 40/40 bracket to test whether the edge is directional.
    Returns: (spot_pnl, net_statutory_inr, net_with_slip_inr, stat_charges, reason, duration_min)
    """
    short_target = entry_spot - FIXED_TARGET_PTS
    short_sl = entry_spot + FIXED_SL_PTS

    for i, bar in enumerate(session_1m):
        b_ts = bar["ts"]
        if b_ts < entry_ts:
            continue
        b_high = bar["high"]
        b_low = bar["low"]
        b_close = bar["close"]
        b_time = bar.get("session_time", "15:20:00")

        # Check SL (High >= entry + 40)
        if b_high >= short_sl:
            spot_pnl = entry_spot - short_sl
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * ASYM_LOSS_DELTA
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            net_inr = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, net_inr, net_with_slip, stat_charges, "SL_HIT", max(0.0, dur_min)

        # Check Target (Low <= entry - 40)
        if b_low <= short_target:
            spot_pnl = entry_spot - short_target
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * ASYM_WIN_DELTA
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            net_inr = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, net_inr, net_with_slip, stat_charges, "TARGET_HIT", max(0.0, dur_min)

        # EOD Square-off
        if b_time >= SQUARE_OFF_TIME or i == len(session_1m) - 1:
            spot_pnl = entry_spot - b_close
            dur_min = (b_ts - entry_ts) / 60.0
            opt_pts = spot_pnl * (ASYM_WIN_DELTA if spot_pnl > 0 else ASYM_LOSS_DELTA)
            gross_inr = opt_pts * LOT_SIZE
            opt_exit = max(0.05, round(OPT_BASE_ENTRY + opt_pts, 2))
            chg = paper_trading.calculate_trade_charges(OPT_BASE_ENTRY, opt_exit, LOT_SIZE, side="BUY")
            stat_charges = float(chg["total_charges"])
            net_inr = round(gross_inr - stat_charges, 2)
            net_with_slip = round(gross_inr - stat_charges - SLIPPAGE_INR_PER_TRADE, 2)
            return spot_pnl, net_inr, net_with_slip, stat_charges, "EOD_SQUARE_OFF", max(0.0, dur_min)

    return 0.0, -78.0, -110.5, 78.0, "NO_DATA", 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. STATISTICAL EVALUATION & DECILE BUILD HURDLE
# ─────────────────────────────────────────────────────────────────────────────

def session_block_bootstrap_ci(
    df: pd.DataFrame,
    pnl_col: str = "asym_net_inr",
    n_bootstraps: int = 1000,
    ci_level: float = 0.95,
) -> Tuple[float, float]:
    """Computes session-block bootstrap confidence intervals."""
    if df.empty or len(df) < 5:
        return 0.0, 0.0
    sessions = df["session_date"].unique()
    session_groups = [grp[pnl_col].values for _, grp in df.groupby("session_date")]
    n_sessions = len(session_groups)

    rng = np.random.default_rng(seed=42)
    boot_means = np.empty(n_bootstraps)

    for i in range(n_bootstraps):
        sampled_indices = rng.integers(0, n_sessions, size=n_sessions)
        sampled_pnl = np.concatenate([session_groups[idx] for idx in sampled_indices])
        boot_means[i] = sampled_pnl.mean() if len(sampled_pnl) > 0 else 0.0

    alpha = (1.0 - ci_level) / 2.0
    lower = float(np.percentile(boot_means, alpha * 100))
    upper = float(np.percentile(boot_means, (1.0 - alpha) * 100))
    return lower, upper


def evaluate_volume_measure(
    measure_key: str,
    measure_name: str,
    df_trades: pd.DataFrame,
    is_sessions: List[str],
    oos_sessions: List[str],
    pnl_col: str = "asym_net_inr",
) -> Dict[str, Any]:
    """
    Evaluates a volume measure using the standardized 8-step build hurdle:
      1. Drop NaNs
      2. In-Sample Decile Bucketing (D1..D10)
      3. Monotonicity Shape Classification
      4. Cutoff Selection on In-Sample
      5. Out-of-Sample Verification
      6. Direction Control Advantage
      7. Outlier Sensitivity (Ex-Top 5)
      8. Session-Block Bootstrap CI & Z-Score
    """
    valid = df_trades.dropna(subset=[measure_key]).copy()
    if len(valid) < 40:
        return {
            "key": measure_key,
            "name": measure_name,
            "status": "FAIL",
            "reason": f"Sample too small (n={len(valid)})",
            "verdict": "NOT BUILT",
        }

    is_df = valid[valid["session_date"].isin(is_sessions)].copy()
    oos_df = valid[valid["session_date"].isin(oos_sessions)].copy()

    if len(is_df) < 20 or len(oos_df) < 20:
        return {
            "key": measure_key,
            "name": measure_name,
            "status": "FAIL",
            "reason": f"IS/OOS split too small (IS={len(is_df)}, OOS={len(oos_df)})",
            "verdict": "NOT BUILT",
        }

    # In-Sample Decile Bucketing
    try:
        is_df["decile"] = pd.qcut(is_df[measure_key], q=10, labels=False, duplicates="drop")
    except Exception:
        is_df["decile"] = pd.cut(is_df[measure_key], bins=10, labels=False)

    decile_summary = []
    for d, grp in is_df.groupby("decile"):
        decile_summary.append({
            "decile": int(d) + 1,
            "n": len(grp),
            "min_val": round(float(grp[measure_key].min()), 3),
            "max_val": round(float(grp[measure_key].max()), 3),
            "win_rate": round(float(grp["is_win"].mean() * 100), 1),
            "expectancy_inr": round(float(grp[pnl_col].mean()), 2),
            "profit_factor": round(float(grp[grp[pnl_col] > 0][pnl_col].sum() / (abs(grp[grp[pnl_col] < 0][pnl_col].sum()) + 1e-9)), 2),
        })

    # Shape Detection
    deciles_ordered = sorted(decile_summary, key=lambda x: x["decile"])
    exp_series = [d["expectancy_inr"] for d in deciles_ordered]
    spearman_rho, spearman_p = stats.spearmanr(range(len(exp_series)), exp_series)

    if spearman_rho >= 0.70 and spearman_p < 0.05:
        shape = "MONOTONE_POSITIVE"
        cutoff_type = ">="
        cutoff_val = deciles_ordered[len(deciles_ordered) // 2]["min_val"]
    elif spearman_rho <= -0.70 and spearman_p < 0.05:
        shape = "MONOTONE_NEGATIVE"
        cutoff_type = "<="
        cutoff_val = deciles_ordered[len(deciles_ordered) // 2]["max_val"]
    else:
        shape = "NO_SHAPE"
        cutoff_type = ">="
        cutoff_val = float(is_df[measure_key].median())

    # Out-of-Sample Test with In-Sample Cutoff
    if cutoff_type == ">=":
        oos_surviving = oos_df[oos_df[measure_key] >= cutoff_val].copy()
    else:
        oos_surviving = oos_df[oos_df[measure_key] <= cutoff_val].copy()

    n_oos = len(oos_surviving)
    wr_oos = float(oos_surviving["is_win"].mean() * 100) if n_oos > 0 else 0.0
    exp_oos = float(oos_surviving[pnl_col].mean()) if n_oos > 0 else 0.0

    gross_win = float(oos_surviving[oos_surviving[pnl_col] > 0][pnl_col].sum())
    gross_loss = abs(float(oos_surviving[oos_surviving[pnl_col] < 0][pnl_col].sum()))
    pf_oos = round(gross_win / (gross_loss + 1e-9), 2) if gross_loss > 0 else 99.0

    # Direction Control on OOS Surviving
    dir_exp_oos = float(oos_surviving["dir_ctrl_asym_net_inr"].mean()) if n_oos > 0 else 0.0
    directional_advantage = exp_oos - dir_exp_oos

    # Outlier Sensitivity
    sorted_pnl = oos_surviving[pnl_col].sort_values(ascending=False).values
    exp_ex_top1 = float(sorted_pnl[1:].mean()) if len(sorted_pnl) > 1 else exp_oos
    exp_ex_top3 = float(sorted_pnl[3:].mean()) if len(sorted_pnl) > 3 else exp_oos
    exp_ex_top5 = float(sorted_pnl[5:].mean()) if len(sorted_pnl) > 5 else exp_oos

    # Bootstrap & Z-score
    ci_low, ci_high = session_block_bootstrap_ci(oos_surviving, pnl_col=pnl_col)
    std_err = float(oos_surviving[pnl_col].std() / np.sqrt(n_oos)) if n_oos > 1 else 1.0
    baseline_exp = float(df_trades[pnl_col].mean())
    z_score = round(float((exp_oos - baseline_exp) / (std_err + 1e-9)), 2)

    # Score against hurdle
    passed_shape = (shape != "NO_SHAPE")
    passed_oos_net = (exp_oos > 0.0)
    passed_pf = (pf_oos >= 1.30)
    passed_n = (n_oos >= 25)  # Scoped window has ~150 OOS total trades
    passed_extop5 = (exp_ex_top5 > 0.0)
    passed_dir_adv = (directional_advantage > 0.0)
    passed_z = (z_score >= 2.0)

    verdict = "BUILD" if (
        passed_shape and passed_oos_net and passed_pf and passed_n and
        passed_extop5 and passed_dir_adv and passed_z
    ) else "NOT BUILT"

    failure_reasons = []
    if not passed_shape:
        failure_reasons.append("No monotonic decile shape")
    if not passed_oos_net:
        failure_reasons.append(f"OOS Net <= 0 (₹{exp_oos:.1f})")
    if not passed_pf:
        failure_reasons.append(f"OOS PF < 1.30 ({pf_oos:.2f})")
    if not passed_extop5:
        failure_reasons.append(f"Ex-top5 Net <= 0 (₹{exp_ex_top5:.1f})")
    if not passed_dir_adv:
        failure_reasons.append(f"Directional advantage <= 0 (₹{directional_advantage:.1f})")
    if not passed_z:
        failure_reasons.append(f"Z-Score < +2.0 SD ({z_score:.2f} SD)")

    return {
        "key": measure_key,
        "name": measure_name,
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
# 4. MAIN WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.time()
    log.info("══════════════════════════════════════════════════════════════════")
    log.info("NIFTY FUTURES VOLUME SURGE RATIO & RVOL DEEP-DIVE BACKTEST ENGINE")
    log.info("══════════════════════════════════════════════════════════════════")

    # 1. Load 1m spot and simulate Variant A
    log.info("Loading 1m spot candles from %s...", DB_PATH)
    df_1m = load_historical_1m_data(str(DB_PATH))
    sim = Setup1BacktestSimulator()
    df_15m_spot = build_15m_data(df_1m, sim.cfg)
    map_15m = {r["ts"]: r for r in df_15m_spot.to_dict(orient="records")}
    list_15m_ts = sorted(map_15m.keys())
    sessions_df = [grp for _, grp in df_1m.groupby("session_date", sort=True)]

    log.info("Simulating Variant A (Raw 8-EMA Touch)...")
    rules_a = {"trend_filter": False, "ubb_filter": False, "first_opp": False, "daily_stop": False, "ctc": False, "fii_window": False, "gap_filter": False, "ema_sep": False, "vwap_filter": False}
    trades_a = sim._simulate_dataset(sessions_df, map_15m, list_15m_ts, "Variant A (Raw 8 EMA Touch)", rules_a)

    # 2. Load FUT & FUT2 and compute RVOL
    log.info("Loading and resampling Futures candles (FUT and FUT2)...")
    df_fut_15m = load_and_resample_futures(DB_PATH)
    df_fut_rvol = compute_all_rvol_measures(df_fut_15m, filter_expiries=True)
    lookup_rvol = {int(r["ts"]): r.to_dict() for _, r in df_fut_rvol.iterrows()}
    sorted_fut_ts = sorted(lookup_rvol.keys())

    # Available volume window
    vol_start = df_fut_15m["session_date"].min()
    vol_end = df_fut_15m["session_date"].max()
    all_vol_sessions = sorted(df_fut_15m["session_date"].unique().tolist())
    n_vol_sessions = len(all_vol_sessions)
    log.info("Futures volume exists across %d sessions: %s to %s", n_vol_sessions, vol_start, vol_end)

    # Filter Variant A strictly to volume window
    trades_scoped = [t for t in trades_a if vol_start <= t.session_date <= vol_end]
    scoped_sessions = sorted(list(set(t.session_date for t in trades_scoped)))
    log.info("Scoped Variant A population: n=%d trades across %d sessions", len(trades_scoped), len(scoped_sessions))

    # Pre-index 1m sessions for mirror short direction control
    session_1m_map = {s_date: grp.to_dict(orient="records") for s_date, grp in df_1m.groupby("session_date")}

    # Helper to find prior closed 15m futures bar
    def get_prior_closed_fut(entry_ts: int) -> Optional[Dict[str, Any]]:
        cand = [t for t in sorted_fut_ts if t + 900 <= entry_ts]
        return lookup_rvol[cand[-1]] if cand else None

    # Enrich scoped trades
    enriched_trades = []
    for tr in trades_scoped:
        bfut = get_prior_closed_fut(tr.entry_ts)
        s_1m = session_1m_map.get(tr.session_date, [])
        dir_pnl, dir_net_inr, dir_net_slip, dir_chg, dir_reason, dir_dur = simulate_direction_control_short(
            tr.entry_ts, tr.entry_spot, s_1m
        )

        row = {
            "session_date": tr.session_date,
            "entry_ts": tr.entry_ts,
            "entry_time": tr.entry_time,
            "entry_spot": tr.entry_spot,
            "spot_pnl_pts": tr.spot_pnl_pts,
            "asym_net_inr": round(tr.asym_net_inr, 2),
            "asym_net_with_slippage_inr": round(getattr(tr, "asym_net_with_slippage_inr", tr.asym_net_inr - SLIPPAGE_INR_PER_TRADE), 2),
            "statutory_charges_inr": round(getattr(tr, "statutory_charges_inr", 78.5), 2),
            "is_win": 1 if tr.spot_pnl_pts > 0 else 0,
            # Direction Control
            "dir_ctrl_asym_net_inr": round(dir_net_inr, 2),
            "dir_ctrl_spot_pnl_pts": round(dir_pnl, 2),
            # Volume Measures
            "comb_bar_rvol": bfut.get("comb_bar_rvol", np.nan) if bfut else np.nan,
            "comb_cum_rvol": bfut.get("comb_cum_rvol", np.nan) if bfut else np.nan,
            "fut_bar_rvol": bfut.get("fut_bar_rvol", np.nan) if bfut else np.nan,
            "fut_cum_rvol": bfut.get("fut_cum_rvol", np.nan) if bfut else np.nan,
            "flat_sma10_ratio": bfut.get("flat_sma10_ratio", np.nan) if bfut else np.nan,
        }
        enriched_trades.append(row)

    df_enriched = pd.DataFrame(enriched_trades)
    csv_path = OUT_DIR / "events_variantA_volume.csv"
    df_enriched.to_csv(csv_path, index=False)
    log.info("Wrote enriched volume events: %s", csv_path)

    # 50/50 Chronological Session Split
    valid_sessions = sorted(df_enriched.dropna(subset=["comb_bar_rvol"])["session_date"].unique().tolist())
    n_split = len(valid_sessions) // 2
    is_sessions = valid_sessions[:n_split]
    oos_sessions = valid_sessions[n_split:]
    log.info("50/50 Split on valid RVOL sessions (%d total): %d IS (%s to %s) | %d OOS (%s to %s)",
             len(valid_sessions), len(is_sessions), is_sessions[0], is_sessions[-1],
             len(oos_sessions), oos_sessions[0], oos_sessions[-1])

    # 3. Evaluate the 4 Volume Measures
    MEASURES = [
        ("comb_bar_rvol", "Combined Bar Slot RVOL (FUT + FUT2)", "Time-of-day Slot RVOL (Rollover Handled)"),
        ("comb_cum_rvol", "Combined Cumulative RVOL (FUT + FUT2)", "Day Cumulative Volume vs Time-Matched Baseline"),
        ("fut_bar_rvol", "Front-Only Bar Slot RVOL (FUT Alone)", "Unadjusted Front Contract (Tests Rollover Distortion)"),
        ("flat_sma10_ratio", "Naive Flat 10-Bar SMA Surge Ratio", "Non-Time-Normalized Flat Rolling SMA"),
    ]

    results = []
    for key, name, desc in MEASURES:
        r = evaluate_volume_measure(key, name, df_enriched, is_sessions, oos_sessions)
        r["description"] = desc
        results.append(r)

    # 4. Save JSON Report
    report_data = {
        "engine": "scripts/backtest_futures_volume.py",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S IST"),
        "volume_window": {
            "start_date": vol_start,
            "end_date": vol_end,
            "total_futures_sessions": n_vol_sessions,
            "scoped_variant_a_trades_n": len(trades_scoped),
            "trades_with_valid_rvol_n": len(df_enriched.dropna(subset=["comb_bar_rvol"])),
            "is_sessions_count": len(is_sessions),
            "oos_sessions_count": len(oos_sessions),
        },
        "charges_model": "Exact Dhan Tariff (paper_trading.calculate_trade_charges)",
        "statutory_avg_inr": round(float(df_enriched["statutory_charges_inr"].mean()), 2),
        "slippage_inr_per_trade": SLIPPAGE_INR_PER_TRADE,
        "measures_evaluated": results,
    }

    report_json_path = OUT_DIR / "report.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    log.info("Successfully persisted report JSON: %s", report_json_path)

    # 5. Print Markdown Verdict Table to Terminal
    print("\n" + "=" * 125)
    print("PORT 8510 — NIFTY FUTURES VOLUME SURGE & RVOL BACKTEST VERDICT TABLE")
    print(f"Population: Variant A Scoped to Volume Window ({vol_start} to {vol_end}) | Lot 65 | Real Dhan Tariff (~₹78/tr)")
    print("Hurdle for BUILD: Monotonic Shape, OOS Net > 0, PF >= 1.30, Ex-top5 > 0, Dir Adv > 0, Z >= +2.0 SD")
    print("=" * 125)
    print(f"{'Measure Name':<42} | {'Shape':<17} | {'OOS n':<6} | {'OOS WR%':<8} | {'Net ₹/Tr':<9} | {'PF':<5} | {'ExTop5 ₹':<9} | {'Dir Adv ₹':<10} | {'Z-Score':<8} | {'Verdict'}")
    print("-" * 125)

    for r in results:
        oos = r.get("out_of_sample", {})
        print(f"{r['name']:<42} | {r.get('shape', 'NO_SHAPE'):<17} | {oos.get('n_surviving', '-'):<6} | {str(oos.get('win_rate', '-')) + '%':<8} | ₹{oos.get('net_inr_per_trade', 0.0):<8.1f} | {oos.get('profit_factor', 0.0):<5.2f} | ₹{oos.get('net_inr_ex_top5', 0.0):<8.1f} | ₹{oos.get('directional_advantage', 0.0):<9.1f} | {oos.get('z_score_sd', 0.0):<4.2f} SD | {r.get('verdict')}")

    print("=" * 125)

    # Unfiltered Context
    base_net = float(df_enriched["asym_net_inr"].mean())
    base_pf = float(df_enriched[df_enriched["asym_net_inr"] > 0]["asym_net_inr"].sum() / (abs(df_enriched[df_enriched["asym_net_inr"] < 0]["asym_net_inr"].sum()) + 1e-9))
    print(f"\nSCOPED BASELINE CONTEXT ({vol_start} to {vol_end}):")
    print(f"  - Total Scoped Trades: n={len(trades_scoped)}, Win Rate={df_enriched['is_win'].mean()*100:.1f}%, Expectancy=₹{base_net:.2f}/trade, PF={base_pf:.2f}")
    print(f"Execution completed in {time.time() - t_start:.2f}s.\n")


if __name__ == "__main__":
    main()
