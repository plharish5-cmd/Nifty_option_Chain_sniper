"""
next_candle_audit.py — Scorer, Baselines, and Edge Tracker.

WHAT THIS MODULE DOES
─────────────────────
Scores next-candle predictions against actual price outcomes.
Computes the three mandatory baselines that every prediction model must beat:
  1. Persistence baseline: "Next candle is the same colour as this one"
  2. Trend baseline: "Next candle follows the prevailing 8 EMA side"
  3. Majority baseline: "Always predict the session's more common colour"

Computes EDGE = App Accuracy - Best Baseline.
Enforces quantitative discipline:
  • Edge is the primary metric; accuracy is NEVER displayed without its baseline.
  • Below 30 calls: Reports INSUFFICIENT SAMPLE (counts only, no percentages).
  • NO_CALL is scored separately — never penalised as a loss.
  • Stored persistently across sessions in data/next_candle/YYYY-MM-DD.csv.
  • Detects one-way trending days (>70% one colour) and issues baseline warnings.

WHAT THIS MODULE CANNOT DO
──────────────────────────
  1. It cannot alter historical outcomes to inflate accuracy.
  2. It does not score the in-progress (forming) candle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import next_candle as nc
import strategy1 as s1

logger = logging.getLogger(__name__)

AUDIT_DIR = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "next_candle"


@dataclass
class ScoredCandle:
    candle_index: int
    time_str: str
    open: float
    high: float
    low: float
    close: float
    candle_color: str           # "RED" | "GREEN" | "DOJI"
    predicted_dir: str          # "UP" | "DOWN" | "NO_CALL"
    confidence: str             # "HIGH" | "MEDIUM" | "LOW"
    confidence_detail: str
    actual_dir: str             # "UP" | "DOWN" | "DOJI" | "FORMING"
    actual_pts: float
    verdict: str                # "RIGHT" | "WRONG" | "NO_CALL" | "FORMING"
    reason: str
    baseline_persistence: str
    baseline_trend: str
    baseline_majority: str
    is_persistence_right: Optional[bool] = None
    is_trend_right: Optional[bool] = None
    is_majority_right: Optional[bool] = None
    regime: str = "TREND"       # "TREND" | "RANGE"
    time_bucket: str = "MIDDAY" # "MORNING" | "MIDDAY" | "AFTERNOON"
    is_stretched: bool = False
    stretch_atr: float = 0.0


@dataclass
class AuditMetrics:
    n_total_candles: int
    n_scored_calls: int          # Directional calls that have an outcome
    n_no_call: int
    n_right: int
    n_wrong: int
    app_accuracy: Optional[float]           # Float percentage or None if insufficient sample
    baseline_persistence_acc: Optional[float]
    baseline_trend_acc: Optional[float]
    baseline_majority_acc: Optional[float]
    best_baseline_acc: Optional[float]
    best_baseline_name: str
    edge: Optional[float]                   # App accuracy - best baseline
    is_sufficient_sample: bool              # True if n_scored >= min_sample_size
    min_sample_size: int
    is_one_way_session: bool
    one_way_pct: float
    majority_color: str
    no_call_avg_pts: float
    regime_breakdown: dict = field(default_factory=dict)
    time_breakdown: dict = field(default_factory=dict)
    confidence_breakdown: dict = field(default_factory=dict)


def _ensure_audit_dir() -> None:
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("Failed to create audit directory %s: %s", AUDIT_DIR, e)


def audit_session(
    df_15m: pd.DataFrame,
    chain_snapshots: Optional[dict] = None,
    prev_close: Optional[float] = None,
    rules: Optional[dict] = None
) -> Tuple[List[ScoredCandle], AuditMetrics]:
    """
    Score all closed candles in the session and evaluate baselines and edge.
    """
    rules = rules or nc.load_rules()
    min_sample = rules.get("min_sample_size", 30)
    doji_pts = rules.get("doji_pts_max", 5.0)
    doji_pct = rules.get("doji_body_pct_max", 20.0)
    
    if df_15m is None or df_15m.empty:
        empty_metrics = AuditMetrics(
            n_total_candles=0, n_scored_calls=0, n_no_call=0, n_right=0, n_wrong=0,
            app_accuracy=None, baseline_persistence_acc=None, baseline_trend_acc=None,
            baseline_majority_acc=None, best_baseline_acc=None, best_baseline_name="None",
            edge=None, is_sufficient_sample=False, min_sample_size=min_sample,
            is_one_way_session=False, one_way_pct=0.0, majority_color="NONE", no_call_avg_pts=0.0
        )
        return [], empty_metrics

    d = df_15m.copy()
    if "timestamp" not in d.columns:
        if isinstance(d.index, pd.DatetimeIndex):
            d["timestamp"] = d.index
        else:
            d["timestamp"] = pd.to_datetime(d.index, errors="coerce")
    else:
        d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    
    d = d.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)

    # Separate today's candles from warmup history if multi-day dataframe provided
    ts_series = pd.to_datetime(d["timestamp"])
    unique_dates = ts_series.dt.date.unique()
    target_date = unique_dates[-1] if len(unique_dates) else None
    
    # If only one day is provided, try loading the previous session from data/candles_15m/ for warmup
    if len(unique_dates) <= 1 and target_date is not None:
        try:
            cand_dir = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "candles_15m"
            hist_files = sorted(cand_dir.glob("*.csv"))
            prev_file = None
            target_str = str(target_date)
            for f in hist_files:
                if f.stem < target_str:
                    prev_file = f
            if prev_file is not None:
                prev_df = pd.read_csv(prev_file)
                if "timestamp" in prev_df.columns:
                    prev_df["timestamp"] = pd.to_datetime(prev_df["timestamp"])
                    prev_df = prev_df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
                    d = pd.concat([prev_df.tail(25), d], ignore_index=True)
                    d = d.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
                    ts_series = pd.to_datetime(d["timestamp"])
        except Exception as e:
            logger.error("Warmup loading error: %s", e)

    # Flag in-progress candle vs closed candles
    if "closed" in d.columns:
        is_closed_mask = d["closed"].fillna(True).astype(bool)
    else:
        is_closed_mask = pd.Series([True] * len(d), index=d.index)

    # Indices belonging to the target date
    if target_date is not None:
        target_indices = [i for i in range(len(d)) if ts_series.iloc[i].date() == target_date]
    else:
        target_indices = list(range(len(d)))

    # 1. Determine candle colours across closed set
    candle_colors = []
    for i in range(len(d)):
        r = d.iloc[i]
        o, c, h, l = float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"])
        rng = h - l
        body = abs(c - o)
        bpct = (body / rng * 100.0) if rng > 0 else 0.0
        if bpct <= doji_pct or body <= doji_pts:
            candle_colors.append("DOJI")
        elif c > o:
            candle_colors.append("GREEN")
        else:
            candle_colors.append("RED")

    # Majority color of closed candles
    closed_colors = [c for i, c in enumerate(candle_colors) if is_closed_mask.iloc[i] and c in ("RED", "GREEN")]
    if closed_colors:
        red_cnt = closed_colors.count("RED")
        grn_cnt = closed_colors.count("GREEN")
        majority_color = "RED" if red_cnt >= grn_cnt else "GREEN"
        majority_dir = "DOWN" if majority_color == "RED" else "UP"
        one_way_pct = max(red_cnt, grn_cnt) / len(closed_colors) * 100.0
    else:
        majority_color = "NONE"
        majority_dir = "NO_CALL"
        one_way_pct = 0.0

    is_one_way = one_way_pct >= (rules.get("trend_one_way_threshold", 0.70) * 100.0)

    # 2. Sequential candle-by-candle predictions and scoring
    scored: list[ScoredCandle] = []
    
    for idx_in_day, i in enumerate(target_indices):
        curr_row = d.iloc[i]
        curr_ts = curr_row["timestamp"]
        curr_ts_str = curr_ts.strftime("%H:%M") if hasattr(curr_ts, "strftime") else str(curr_ts)[11:16]
        c_col = candle_colors[i]
        
        # In-progress candle guard
        is_curr_closed = is_closed_mask.iloc[i]
        
        # Match chain snapshot if available
        snap_df = None
        if chain_snapshots and curr_ts is not None:
            ts_str = pd.to_datetime(curr_ts).strftime("%H%M")
            snap_df = chain_snapshots.get(ts_str)

        # Generate prediction based strictly on data UP TO candle i
        pred = nc.predict_next_candle(
            df_15m=d.iloc[:i + 1],
            chain_df=snap_df,
            prev_close=prev_close,
            rules=rules
        )
        
        # Baselines for the NEXT candle:
        # Persistence: predicts next candle will match this candle's colour
        base_pers = "UP" if c_col == "GREEN" else ("DOWN" if c_col == "RED" else "NO_CALL")
        # Trend: 8 EMA side
        base_trend = pred.baseline
        # Majority: prevailing majority of day
        base_maj = majority_dir

        # Determine outcome from NEXT candle (i + 1)
        if i < len(d) - 1 and is_closed_mask.iloc[i + 1]:
            nxt_row = d.iloc[i + 1]
            nxt_col = candle_colors[i + 1]
            nxt_c = float(nxt_row["close"])
            nxt_o = float(nxt_row["open"])
            curr_c = float(curr_row["close"])
            pts = nxt_c - curr_c
            
            actual_dir = "UP" if nxt_col == "GREEN" else ("DOWN" if nxt_col == "RED" else "DOJI")
            
            # Score application call
            if pred.direction == "NO_CALL":
                verdict = "NO_CALL"
                reason = f"No call made ({pred.confidence_detail}). Price moved {pts:+.1f} pts ({nxt_col})."
            elif actual_dir == "DOJI":
                verdict = "NO_CALL"
                reason = f"Next candle formed a Doji/indecision bar ({pts:+.1f} pts)."
            elif pred.direction == actual_dir:
                verdict = "RIGHT"
                reason = f"Correctly predicted {pred.direction}. Next candle moved {pts:+.1f} pts ({actual_dir})."
            else:
                verdict = "WRONG"
                reason = f"Predicted {pred.direction}, but next candle formed {actual_dir} ({pts:+.1f} pts)."

            # Score baselines
            is_pers_right = (base_pers == actual_dir) if base_pers != "NO_CALL" else None
            is_trend_right = (base_trend == actual_dir) if base_trend != "NO_CALL" else None
            is_maj_right = (base_maj == actual_dir) if base_maj != "NO_CALL" else None
        elif not is_curr_closed:
            actual_dir = "FORMING"
            pts = 0.0
            verdict = "FORMING"
            reason = "Current candle is forming and excluded from scoring."
            is_pers_right = is_trend_right = is_maj_right = None
        else:
            actual_dir = "FORMING"
            pts = 0.0
            verdict = "FORMING"
            reason = "Awaiting close of the next candle."
            is_pers_right = is_trend_right = is_maj_right = None

        # Time bucket
        t_hour = curr_ts.hour if hasattr(curr_ts, "hour") else 12
        if t_hour < 11 or (t_hour == 11 and curr_ts.minute <= 30):
            time_b = "MORNING"
        elif t_hour < 13 or (t_hour == 13 and curr_ts.minute <= 30):
            time_b = "MIDDAY"
        else:
            time_b = "AFTERNOON"

        # Regime tag
        stage_str, _, _ = s1._stage(d.iloc[:i + 1], s1.load_params())
        reg_tag = "RANGE" if "STAGE 1" in stage_str else "TREND"

        scored.append(ScoredCandle(
            candle_index=idx_in_day + 1,
            time_str=curr_ts_str,
            open=float(curr_row["open"]),
            high=float(curr_row["high"]),
            low=float(curr_row["low"]),
            close=float(curr_row["close"]),
            candle_color=c_col,
            predicted_dir=pred.direction,
            confidence=pred.confidence,
            confidence_detail=pred.confidence_detail,
            actual_dir=actual_dir,
            actual_pts=pts,
            verdict=verdict,
            reason=reason,
            baseline_persistence=base_pers,
            baseline_trend=base_trend,
            baseline_majority=base_maj,
            is_persistence_right=is_pers_right,
            is_trend_right=is_trend_right,
            is_majority_right=is_maj_right,
            regime=reg_tag,
            time_bucket=time_b,
            is_stretched=pred.is_stretched,
            stretch_atr=pred.stretch_atr
        ))

    # 3. Compute Aggregate Metrics
    completed = [sc for sc in scored if sc.verdict in ("RIGHT", "WRONG")]
    no_calls = [sc for sc in scored if sc.verdict == "NO_CALL" and sc.actual_dir != "FORMING"]
    
    n_scored = len(completed)
    n_right = sum(1 for sc in completed if sc.verdict == "RIGHT")
    n_wrong = sum(1 for sc in completed if sc.verdict == "WRONG")
    n_nc = len(no_calls)
    
    no_call_pts = [abs(sc.actual_pts) for sc in no_calls]
    no_call_avg = float(np.mean(no_call_pts)) if no_call_pts else 0.0

    # Baselines across the EXACT SAME candles where app made calls
    pers_rights = sum(1 for sc in completed if sc.is_persistence_right is True)
    trend_rights = sum(1 for sc in completed if sc.is_trend_right is True)
    maj_rights = sum(1 for sc in completed if sc.is_majority_right is True)

    is_sufficient = (n_scored >= min_sample)

    if n_scored > 0:
        raw_app_acc = (n_right / n_scored) * 100.0
        raw_pers_acc = (pers_rights / n_scored) * 100.0
        raw_trend_acc = (trend_rights / n_scored) * 100.0
        raw_maj_acc = (maj_rights / n_scored) * 100.0

        baselines = [
            ("Trend Follow", raw_trend_acc),
            ("Persistence", raw_pers_acc),
            ("Majority", raw_maj_acc)
        ]
        best_base_name, best_base_acc = max(baselines, key=lambda x: x[1])
        raw_edge = raw_app_acc - best_base_acc
    else:
        raw_app_acc = raw_pers_acc = raw_trend_acc = raw_maj_acc = None
        best_base_acc = None
        best_base_name = "None"
        raw_edge = None

    # Breakdowns
    regime_break = {}
    for r in ("TREND", "RANGE"):
        sub = [sc for sc in completed if sc.regime == r]
        if sub:
            r_right = sum(1 for sc in sub if sc.verdict == "RIGHT")
            regime_break[r] = {"n": len(sub), "acc": (r_right / len(sub)) * 100.0}
        else:
            regime_break[r] = {"n": 0, "acc": None}

    time_break = {}
    for tb in ("MORNING", "MIDDAY", "AFTERNOON"):
        sub = [sc for sc in completed if sc.time_bucket == tb]
        if sub:
            t_right = sum(1 for sc in sub if sc.verdict == "RIGHT")
            time_break[tb] = {"n": len(sub), "acc": (t_right / len(sub)) * 100.0}
        else:
            time_break[tb] = {"n": 0, "acc": None}

    conf_break = {}
    for cl in ("HIGH", "MEDIUM", "LOW"):
        sub = [sc for sc in completed if sc.confidence == cl]
        if sub:
            c_right = sum(1 for sc in sub if sc.verdict == "RIGHT")
            conf_break[cl] = {"n": len(sub), "acc": (c_right / len(sub)) * 100.0}
        else:
            conf_break[cl] = {"n": 0, "acc": None}

    metrics = AuditMetrics(
        n_total_candles=len(d),
        n_scored_calls=n_scored,
        n_no_call=n_nc,
        n_right=n_right,
        n_wrong=n_wrong,
        app_accuracy=raw_app_acc if is_sufficient else None,
        baseline_persistence_acc=raw_pers_acc if is_sufficient else None,
        baseline_trend_acc=raw_trend_acc if is_sufficient else None,
        baseline_majority_acc=raw_maj_acc if is_sufficient else None,
        best_baseline_acc=best_base_acc if is_sufficient else None,
        best_baseline_name=best_base_name,
        edge=raw_edge if is_sufficient else None,
        is_sufficient_sample=is_sufficient,
        min_sample_size=min_sample,
        is_one_way_session=is_one_way,
        one_way_pct=one_way_pct,
        majority_color=majority_color,
        no_call_avg_pts=no_call_avg,
        regime_breakdown=regime_break,
        time_breakdown=time_break,
        confidence_breakdown=conf_break
    )

    return scored, metrics


def save_session_audit(day_str: str, scored: list[ScoredCandle]) -> Path:
    """
    Persist session audit rows to data/next_candle/YYYY-MM-DD.csv.
    """
    _ensure_audit_dir()
    filepath = AUDIT_DIR / f"{day_str}.csv"
    
    rows = []
    for sc in scored:
        rows.append({
            "candle_index": sc.candle_index,
            "time": sc.time_str,
            "open": sc.open,
            "high": sc.high,
            "low": sc.low,
            "close": sc.close,
            "candle_color": sc.candle_color,
            "predicted_dir": sc.predicted_dir,
            "confidence": sc.confidence,
            "confidence_detail": sc.confidence_detail,
            "actual_dir": sc.actual_dir,
            "actual_pts": sc.actual_pts,
            "verdict": sc.verdict,
            "reason": sc.reason,
            "baseline_persistence": sc.baseline_persistence,
            "baseline_trend": sc.baseline_trend,
            "baseline_majority": sc.baseline_majority,
            "is_persistence_right": sc.is_persistence_right,
            "is_trend_right": sc.is_trend_right,
            "is_majority_right": sc.is_majority_right,
            "regime": sc.regime,
            "time_bucket": sc.time_bucket,
            "is_stretched": sc.is_stretched,
            "stretch_atr": sc.stretch_atr
        })
        
    df = pd.DataFrame(rows)
    try:
        df.to_csv(filepath, index=False)
    except Exception as e:
        logger.error("Failed to save session audit to %s: %s", filepath, e)
    return filepath


def load_historical_audits() -> pd.DataFrame:
    """
    Load all stored audit CSVs across history to evaluate aggregated multi-session metrics.
    """
    _ensure_audit_dir()
    files = sorted(AUDIT_DIR.glob("*.csv"))
    if not files:
        return pd.DataFrame()
        
    dfs = []
    for f in files:
        try:
            sub = pd.read_csv(f)
            sub["session_date"] = f.stem
            dfs.append(sub)
        except Exception as e:
            logger.error("Error reading historical audit %s: %s", f, e)
            
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def compute_historical_edge(rules: Optional[dict] = None) -> AuditMetrics:
    """
    Aggregate all historical calls across saved sessions and compute grand edge.
    """
    rules = rules or nc.load_rules()
    min_sample = rules.get("min_sample_size", 30)
    
    df = load_historical_audits()
    if df.empty:
        return AuditMetrics(
            n_total_candles=0, n_scored_calls=0, n_no_call=0, n_right=0, n_wrong=0,
            app_accuracy=None, baseline_persistence_acc=None, baseline_trend_acc=None,
            baseline_majority_acc=None, best_baseline_acc=None, best_baseline_name="None",
            edge=None, is_sufficient_sample=False, min_sample_size=min_sample,
            is_one_way_session=False, one_way_pct=0.0, majority_color="NONE", no_call_avg_pts=0.0
        )
        
    completed = df[df["verdict"].isin(["RIGHT", "WRONG"])].copy()
    no_calls = df[df["verdict"] == "NO_CALL"]
    
    n_scored = len(completed)
    n_right = (completed["verdict"] == "RIGHT").sum()
    n_wrong = (completed["verdict"] == "WRONG").sum()
    n_nc = len(no_calls)
    
    no_call_pts = no_calls["actual_pts"].abs().tolist()
    no_call_avg = float(np.mean(no_call_pts)) if no_call_pts else 0.0
    
    pers_rights = (completed["is_persistence_right"] == True).sum()
    trend_rights = (completed["is_trend_right"] == True).sum()
    maj_rights = (completed["is_majority_right"] == True).sum()
    
    is_sufficient = (n_scored >= min_sample)
    
    if n_scored > 0:
        raw_app_acc = (n_right / n_scored) * 100.0
        raw_pers_acc = (pers_rights / n_scored) * 100.0
        raw_trend_acc = (trend_rights / n_scored) * 100.0
        raw_maj_acc = (maj_rights / n_scored) * 100.0
        
        baselines = [
            ("Trend Follow", raw_trend_acc),
            ("Persistence", raw_pers_acc),
            ("Majority", raw_maj_acc)
        ]
        best_base_name, best_base_acc = max(baselines, key=lambda x: x[1])
        raw_edge = raw_app_acc - best_base_acc
    else:
        raw_app_acc = raw_pers_acc = raw_trend_acc = raw_maj_acc = None
        best_base_acc = None
        best_base_name = "None"
        raw_edge = None
        
    # Breakdowns
    regime_break = {}
    for r in ("TREND", "RANGE"):
        sub = completed[completed["regime"] == r]
        if len(sub):
            r_right = (sub["verdict"] == "RIGHT").sum()
            regime_break[r] = {"n": len(sub), "acc": (r_right / len(sub)) * 100.0}
        else:
            regime_break[r] = {"n": 0, "acc": None}

    time_break = {}
    for tb in ("MORNING", "MIDDAY", "AFTERNOON"):
        sub = completed[completed["time_bucket"] == tb]
        if len(sub):
            t_right = (sub["verdict"] == "RIGHT").sum()
            time_break[tb] = {"n": len(sub), "acc": (t_right / len(sub)) * 100.0}
        else:
            time_break[tb] = {"n": 0, "acc": None}

    conf_break = {}
    for cl in ("HIGH", "MEDIUM", "LOW"):
        sub = completed[completed["confidence"] == cl]
        if len(sub):
            c_right = (sub["verdict"] == "RIGHT").sum()
            conf_break[cl] = {"n": len(sub), "acc": (c_right / len(sub)) * 100.0}
        else:
            conf_break[cl] = {"n": 0, "acc": None}
            
    return AuditMetrics(
        n_total_candles=len(df),
        n_scored_calls=n_scored,
        n_no_call=n_nc,
        n_right=n_right,
        n_wrong=n_wrong,
        app_accuracy=raw_app_acc if is_sufficient else None,
        baseline_persistence_acc=raw_pers_acc if is_sufficient else None,
        baseline_trend_acc=raw_trend_acc if is_sufficient else None,
        baseline_majority_acc=raw_maj_acc if is_sufficient else None,
        best_baseline_acc=best_base_acc if is_sufficient else None,
        best_baseline_name=best_base_name,
        edge=raw_edge if is_sufficient else None,
        is_sufficient_sample=is_sufficient,
        min_sample_size=min_sample,
        is_one_way_session=False,
        one_way_pct=0.0,
        majority_color="MULTI",
        no_call_avg_pts=no_call_avg,
        regime_breakdown=regime_break,
        time_breakdown=time_break,
        confidence_breakdown=conf_break
    )


if __name__ == "__main__":
    import glob
    print("=" * 65)
    print("  NEXT 15M CANDLE AUDIT ENGINE — CUMULATIVE BENCHMARK")
    print("=" * 65)
    cumulative = compute_historical_edge()
    print(f"Total Scored Calls  : {cumulative.n_scored_calls}")
    print(f"Total NO_CALLs      : {cumulative.n_no_call}")
    if cumulative.app_accuracy is not None:
        print(f"App Accuracy        : {cumulative.app_accuracy:.1f}%")
        print(f"Persistence Baseline: {cumulative.baseline_persistence_acc:.1f}%")
        print(f"Trend Baseline      : {cumulative.baseline_trend_acc:.1f}%")
        print(f"Majority Baseline   : {cumulative.baseline_majority_acc:.1f}%")
        print(f"Best Baseline       : {cumulative.best_baseline_name} ({cumulative.best_baseline_acc:.1f}%)")
        print(f"Net Statistical Edge: {cumulative.edge:+.1f}%")
    else:
        print("Insufficient sample size (< min_sample) for aggregate reporting.")
    print("=" * 65)
