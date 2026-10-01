"""
writers_ratio.py — Call vs Put Writers Ratio Engine for 15-Minute Support / Resistance Labels.

WHAT THIS MODULE DOES:
Evaluates whether a 15-minute swing pivot is being actively defended or contested by institutional
option sellers.
- At a confirmed Swing Low: checks if Put writers are accumulating open interest faster than Call writers
  across the strikes near the pivot level (±2 strike steps = ±100 pts). If PE/CE >= 1.35, marks SUPPORT.
- At a confirmed Swing High: checks if Call writers are accumulating open interest faster than Put writers.
  If CE/PE >= 1.35, marks RESISTANCE.
- When the ratio opposes the pivot (e.g. Call writers dominating at a swing low), marks CONTESTED.
- Integrates flow.ChurnFlag to detect contract churn: if high volume occurs without new OI commitment,
  downgrades strength to TURNOVER (a soft wall).

WHAT THIS MODULE CANNOT DO:
1. It CANNOT identify a pivot at the moment it forms. By definition, a fractal swing pivot (N=2)
   requires 2 subsequent candles to confirm. This module strictly reads the writer flow at
   `pivot.confirmed_i` (30 minutes after formation). It NEVER reads or enters at `pivot.i`.
2. It CANNOT prevent drawdowns or guarantee bounces. Option walls can and do break, especially
   on trend days or when churn is present.
3. It CANNOT interpolate missing data. If two valid snapshots spanning the pivot window do not exist,
   it yields NO_READ as a first-class output.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import flow
import smc

log = logging.getLogger("writers_ratio")

RULES_FILE = Path(__file__).parent / "writers_ratio_rules.json"


def load_rules() -> Dict[str, Any]:
    """Loads operational thresholds from writers_ratio_rules.json (Rule 4: Zero Hardcoding)."""
    defaults = {
        "ratio_threshold": 1.35,
        "ratio_ladder": [1.0, 1.2, 1.35, 1.5, 1.8, 2.0],
        "strike_window_steps": 2,
        "strike_step": 50,
        "churn_cutoff": 3.0,
        "min_sample_size": 30,
        "max_snapshot_gap_minutes": 20.0,
        "swing_n": 2,
        "mfe_target_pts": 25.0,
        "mae_stop_pts": 25.0,
        "fwd_horizon_mins": 30,
        "one_way_day_threshold": 0.70,
    }
    if RULES_FILE.exists():
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**defaults, **data}
        except Exception as err:
            log.warning(f"Could not read {RULES_FILE}, falling back to defaults: {err}")
    return defaults


@dataclass
class WriterFlow:
    at: datetime                       # Timestamp of the CONFIRMED candle
    pivot_kind: str                    # "SWING_HIGH" | "SWING_LOW" | "NONE"
    pivot_price: float                 # The price level of the pivot (or NaN if NONE)
    pivot_formed_at: Optional[datetime] # When the pivot candle formed
    confirm_lag_bars: int              # Bars elapsed between forming and confirming
    ce_oi_delta: float                 # Change in CE OI across the window at near strikes
    pe_oi_delta: float                 # Change in PE OI across the window at near strikes
    ratio: float                       # PE/CE at a low, CE/PE at a high (or NaN)
    label: str                         # "SUPPORT" | "RESISTANCE" | "CONTESTED" | "NO_READ"
    strength: str                      # "DOMINANT" | "MILD" | "BALANCED" | "TURNOVER" | "OPPOSED" | "NO_READ"
    churn_flag: bool                   # From flow.ChurnFlag — is the wall soft?
    note: str = ""                     # Qualitative context or warning note


def get_near_strikes(price: float, step: int = 50, window_steps: int = 2) -> List[float]:
    """Returns the strikes within ±window_steps of the given price level."""
    if math.isnan(price) or price <= 0:
        return []
    atm = round(price / step) * step
    return [float(atm + i * step) for i in range(-window_steps, window_steps + 1)]


def find_snapshot_near_time(
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    target_time: datetime,
    max_tol_sec: float = 1200.0,  # 20 minutes tolerance
) -> Optional[Tuple[datetime, float, pd.DataFrame]]:
    """Finds the closest snapshot to target_time within tolerance."""
    if not snapshots:
        return None
    best = None
    min_diff = float("inf")
    for s in snapshots:
        diff = abs((s[0] - target_time).total_seconds())
        if diff < min_diff and diff <= max_tol_sec:
            min_diff = diff
            best = s
    return best


def evaluate_pivot_writer_flow(
    pivot: smc.Pivot,
    candles_15m: pd.DataFrame,
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    rules: Optional[Dict[str, Any]] = None,
) -> WriterFlow:
    """
    Evaluates writer flow for a confirmed fractal swing pivot.
    Strictly evaluated at pivot.confirmed_i (the confirmation bar), NEVER at pivot.i.
    """
    cfg = rules or load_rules()
    c_i = pivot.confirmed_i
    n_bars = len(candles_15m)

    # Defensive Guard: bounds check
    if c_i < 0 or c_i >= n_bars:
        return WriterFlow(
            at=pivot.at,
            pivot_kind=f"SWING_{pivot.kind}",
            pivot_price=pivot.price,
            pivot_formed_at=pivot.at,
            confirm_lag_bars=0,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Confirmation bar index out of candle bounds",
        )

    t_confirm = pd.to_datetime(candles_15m.loc[c_i, "timestamp"])
    t_formed = pivot.at
    lag_bars = c_i - pivot.i

    # Defensive Guard: Need at least two snapshots to compute delta across the window
    if not snapshots or len(snapshots) < 2:
        return WriterFlow(
            at=t_confirm,
            pivot_kind=f"SWING_{pivot.kind}",
            pivot_price=pivot.price,
            pivot_formed_at=t_formed,
            confirm_lag_bars=lag_bars,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Insufficient snapshot history (< 2 snapshots)",
        )

    # Find snapshot spanning the window
    max_gap_tol = (cfg.get("max_snapshot_gap_minutes", 20.0) + (lag_bars * 15.0)) * 60.0
    s_start = find_snapshot_near_time(snapshots, t_formed, max_tol_sec=max_gap_tol)
    s_end = find_snapshot_near_time(snapshots, t_confirm, max_tol_sec=max_gap_tol)

    if s_start is None or s_end is None:
        return WriterFlow(
            at=t_confirm,
            pivot_kind=f"SWING_{pivot.kind}",
            pivot_price=pivot.price,
            pivot_formed_at=t_formed,
            confirm_lag_bars=lag_bars,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Missing snapshots spanning formation-to-confirmation window",
        )

    snap_span_sec = abs((s_end[0] - s_start[0]).total_seconds())
    if snap_span_sec < 30.0:
        # Snapshots identical or too close together
        return WriterFlow(
            at=t_confirm,
            pivot_kind=f"SWING_{pivot.kind}",
            pivot_price=pivot.price,
            pivot_formed_at=t_formed,
            confirm_lag_bars=lag_bars,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Snapshot poll gap too narrow to resolve OI delta",
        )

    # Strike window: within ±2 steps of pivot price
    step = int(cfg.get("strike_step", 50))
    w_steps = int(cfg.get("strike_window_steps", 2))
    near_strikes = get_near_strikes(pivot.price, step=step, window_steps=w_steps)

    df_start = s_start[2]
    df_end = s_end[2]

    if df_start.empty or df_end.empty or "strike" not in df_start.columns or "strike" not in df_end.columns:
        return WriterFlow(
            at=t_confirm,
            pivot_kind=f"SWING_{pivot.kind}",
            pivot_price=pivot.price,
            pivot_formed_at=t_formed,
            confirm_lag_bars=lag_bars,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Malformed snapshot data table missing strike column",
        )

    sub_start = df_start[df_start["strike"].isin(near_strikes)]
    sub_end = df_end[df_end["strike"].isin(near_strikes)]

    ce_start = float(sub_start["ce_oi"].sum()) if "ce_oi" in sub_start.columns else 0.0
    pe_start = float(sub_start["pe_oi"].sum()) if "pe_oi" in sub_start.columns else 0.0
    ce_end = float(sub_end["ce_oi"].sum()) if "ce_oi" in sub_end.columns else 0.0
    pe_end = float(sub_end["pe_oi"].sum()) if "pe_oi" in sub_end.columns else 0.0

    d_ce = ce_end - ce_start
    d_pe = pe_end - pe_start

    # Churn Check at near strikes via flow.ChurnFlag
    churn_flag = False
    churn_note = ""
    try:
        closest_strike = round(pivot.price / step) * step
        # Check volume vs new OI on df_end
        c_row = df_end[df_end["strike"] == closest_strike]
        if not c_row.empty:
            side_col = "pe" if pivot.kind == "LOW" else "ce"
            vol = float(c_row.iloc[0].get(f"{side_col}_vol", 0.0) or 0.0)
            chg = float(c_row.iloc[0].get(f"{side_col}_chg_oi", 0.0) or 0.0)
            if chg != 0 and vol > 0:
                cf = flow.ChurnFlag(int(closest_strike), side_col.upper(), vol, chg)
                if cf.is_churn:
                    churn_flag = True
                    churn_note = f"Wall soft: {cf.ratio:.1f}x volume/OI churn at {closest_strike:,.0f}"
    except Exception as e:
        log.debug(f"Churn check failed: {e}")

    # Ratio & Stance Determination
    ratio_th = float(cfg.get("ratio_threshold", 1.35))
    p_kind = f"SWING_{pivot.kind}"

    if pivot.kind == "LOW":
        # At Swing Low: We evaluate Put writing defense (PE/CE)
        if d_pe > 0 and d_ce <= 0:
            ratio = 3.0
            strength = "DOMINANT"
            label = "SUPPORT"
            note = f"Put writers fortifying (+{d_pe:,.0f}) while Call writers covering ({d_ce:,.0f})"
        elif d_pe <= 0 and d_ce > 0:
            ratio = 0.33
            strength = "OPPOSED"
            label = "CONTESTED"
            note = f"Call writers outbuilding Put writers (+{d_ce:,.0f} CE vs {d_pe:,.0f} PE)"
        elif d_pe <= 0 and d_ce <= 0:
            ratio = 1.0
            strength = "BALANCED"
            label = "NO_READ"
            note = "Both sides unwinding near pivot strikes — no positioning"
        else:
            # Both positive
            ratio = d_pe / max(d_ce, 1.0)
            if ratio >= ratio_th:
                strength = "DOMINANT"
                label = "SUPPORT"
                note = f"Put writers dominant ({ratio:.2f}x advantage over Call writers)"
            elif ratio < 1.0:
                strength = "OPPOSED"
                label = "CONTESTED"
                note = f"Call writers outnumber Put writers ({1.0/max(ratio, 0.01):.2f}x CE advantage)"
            else:
                strength = "MILD"
                label = "SUPPORT"
                note = f"Put writers slightly lead ({ratio:.2f}x), but below dominant threshold"

        # Churn gating: downgrade if wall is churning
        if churn_flag and strength == "DOMINANT":
            strength = "TURNOVER"
            note = f"{churn_note}. Downgraded to turnover rather than commitment."

    elif pivot.kind == "HIGH":
        # At Swing High: We evaluate Call writing resistance (CE/PE)
        if d_ce > 0 and d_pe <= 0:
            ratio = 3.0
            strength = "DOMINANT"
            label = "RESISTANCE"
            note = f"Call writers capping (+{d_ce:,.0f}) while Put writers unwinding ({d_pe:,.0f})"
        elif d_ce <= 0 and d_pe > 0:
            ratio = 0.33
            strength = "OPPOSED"
            label = "CONTESTED"
            note = f"Put writers outbuilding Call writers (+{d_pe:,.0f} PE vs {d_ce:,.0f} CE)"
        elif d_ce <= 0 and d_pe <= 0:
            ratio = 1.0
            strength = "BALANCED"
            label = "NO_READ"
            note = "Both sides unwinding near pivot strikes — no positioning"
        else:
            ratio = d_ce / max(d_pe, 1.0)
            if ratio >= ratio_th:
                strength = "DOMINANT"
                label = "RESISTANCE"
                note = f"Call writers dominant ({ratio:.2f}x advantage over Put writers)"
            elif ratio < 1.0:
                strength = "OPPOSED"
                label = "CONTESTED"
                note = f"Put writers outnumber Call writers ({1.0/max(ratio, 0.01):.2f}x PE advantage)"
            else:
                strength = "MILD"
                label = "RESISTANCE"
                note = f"Call writers slightly lead ({ratio:.2f}x), but below dominant threshold"

        if churn_flag and strength == "DOMINANT":
            strength = "TURNOVER"
            note = f"{churn_note}. Downgraded to turnover rather than commitment."

    else:
        return WriterFlow(
            at=t_confirm,
            pivot_kind="NONE",
            pivot_price=float("nan"),
            pivot_formed_at=None,
            confirm_lag_bars=0,
            ce_oi_delta=0.0,
            pe_oi_delta=0.0,
            ratio=1.0,
            label="NO_READ",
            strength="NO_READ",
            churn_flag=False,
            note="Non-swing candle",
        )

    return WriterFlow(
        at=t_confirm,
        pivot_kind=p_kind,
        pivot_price=pivot.price,
        pivot_formed_at=t_formed,
        confirm_lag_bars=lag_bars,
        ce_oi_delta=d_ce,
        pe_oi_delta=d_pe,
        ratio=ratio,
        label=label,
        strength=strength,
        churn_flag=churn_flag,
        note=note,
    )


def compute_session_writers_flow(
    candles_15m: pd.DataFrame,
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    rules: Optional[Dict[str, Any]] = None,
) -> List[WriterFlow]:
    """
    Produces one WriterFlow record per 15-minute candle of the session.
    Every candle is represented; non-pivot candles are explicitly stamped NO_READ.
    """
    if candles_15m is None or candles_15m.empty:
        return []

    cfg = rules or load_rules()
    c_df = candles_15m.sort_values("timestamp").reset_index(drop=True)
    n_bars = len(c_df)

    # Compute pivots via smc.pivots (confirmed_i stamped strictly)
    pivots_list = smc.pivots(c_df, n=int(cfg.get("swing_n", 2)))

    # Map confirmed_i to pivot
    confirmed_map: Dict[int, smc.Pivot] = {}
    for p in pivots_list:
        confirmed_map[p.confirmed_i] = p

    results: List[WriterFlow] = []
    for bar_idx in range(n_bars):
        t_bar = pd.to_datetime(c_df.loc[bar_idx, "timestamp"])

        if bar_idx in confirmed_map:
            p = confirmed_map[bar_idx]
            flow_record = evaluate_pivot_writer_flow(p, c_df, snapshots, rules=cfg)
            results.append(flow_record)
        else:
            # First-class NO_READ row
            results.append(
                WriterFlow(
                    at=t_bar,
                    pivot_kind="NONE",
                    pivot_price=float("nan"),
                    pivot_formed_at=None,
                    confirm_lag_bars=0,
                    ce_oi_delta=0.0,
                    pe_oi_delta=0.0,
                    ratio=float("nan"),
                    label="NO_READ",
                    strength="NO_READ",
                    churn_flag=False,
                    note="No confirmed pivot at this bar close",
                )
            )

    return results
