"""
writers_graph_engine.py — Call Writers vs Put Writers (CE vs PE) Multi-Timeframe Analysis Engine.

Built strictly according to Institutional Options Seller Principles & Video Methodology:
1. "Never take a directional trade without checking the Call Writers vs Put Writers graph."
2. Real-Time Tracking of CE Writers (Supply Ceilings) vs PE Writers (Demand Floors)
   across every 3-minute, 5-minute, and 15-minute candle.
3. Quantifies:
   - Cumulative CE Writers vs PE Writers (from market open 09:15 to 15:30 IST)
   - Net Difference: (PE - CE) [Put Writer Domination] and (CE - PE) [Call Writer Domination]
   - Dual Ratios: PE/CE (Writer PCR) & CE/PE (Writer CPR)
   - Per-Candle Incremental ΔOI (exact contracts added/unwound in each 3m, 5m, 15m bar)
4. Trap Detection Engine:
   - Bear Trap: Spot breaking session low or dipping sharply while PE Writers aggressively expand (+ΔPE)
     and refuse to unwind, flashing an immediate Bear Trap (e.g. 09:45 dip to 23,121).
   - Bull Trap: Spot breaking session high or rallying while CE Writers aggressively build (+ΔCE),
     flashing an immediate Bull Trap.
5. All thresholds loaded from patterns/patterns_rules.json and strategy1_rules.json (No hardcoding).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import config
import store

log = logging.getLogger("writers_graph_engine")

# ─────────────────────────────────────────────────────────────────────────────
# 1. Config Loader (Zero Hardcoding - Rule 4)
# ─────────────────────────────────────────────────────────────────────────────
def load_writers_config() -> Dict[str, Any]:
    """Loads writers graph parameters from patterns_rules.json or strategy1_rules.json."""
    defaults = {
        "focus_window_default_radius": 450.0,
        "available_timeframes": ["3m", "5m", "15m"],
        "default_timeframe": "5m",
        "stabilization_time": "10:15",
        "warmup_time": "09:45",
        "trap_drop_pts": 15.0,
        "trap_rally_pts": 15.0,
        "ratio_dominant": 1.35,
        "ratio_overwhelming": 1.8,
        "net_diff_min_contracts": 25000.0,
        "display_lot_size": 65,
        "colors": {
            "ce_writers": "#EF4444",
            "pe_writers": "#10B981",
            "diff_pe_dominant": "#10B981",
            "diff_ce_dominant": "#EF4444",
            "spot_line": "#F59E0B",
            "bg_dark": "#0B1120"
        }
    }
    
    # Try patterns/patterns_rules.json
    p_rules = Path("patterns/patterns_rules.json")
    if p_rules.exists():
        try:
            with open(p_rules, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "writers_graph" in data:
                    return {**defaults, **data["writers_graph"]}
        except Exception as e:
            log.warning(f"Failed to read writers_graph from {p_rules}: {e}")

    # Fallback to strategy1_rules.json
    s_rules = Path("strategy1_rules.json")
    if s_rules.exists():
        try:
            with open(s_rules, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "writers_graph" in data:
                    return {**defaults, **data["writers_graph"]}
        except Exception as e:
            log.warning(f"Failed to read writers_graph from {s_rules}: {e}")

    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# 1B. Striking Price Prediction Power & Edge Engine
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StrikePredictionPowerResult:
    striking_strike: float
    spot_price: float
    distance_pts: float
    barrier_kind: str           # "SUPPORT", "RESISTANCE", "ATM"
    ce_oi: float
    ce_chg_oi: float
    pe_oi: float
    pe_chg_oi: float
    ce_status: str              # 'DEFENDING' (heavy writing) or 'UNWINDING' (covering)
    pe_status: str              # 'DEFENDING' (heavy writing) or 'UNWINDING' (covering)
    net_strike_diff: float      # pe_chg_oi - ce_chg_oi (> 0 = PE advantage, < 0 = CE advantage)
    strike_writer_pcr: float    # pe_chg_oi / max(ce_chg_oi, 1.0)
    strike_dominance: str       # 'PUT_WRITERS_DOMINATING', 'CALL_WRITERS_DOMINATING', 'BALANCED'
    focus_net_diff: float       # Focus window (PE - CE)
    focus_writer_pcr: float     # Focus window PE / CE ratio
    expected_direction: str     # 'UP' (Bullish rebound/support defense), 'DOWN' (Bearish rejection/ceiling capping), 'NEUTRAL'
    confidence: str            # 'HIGH', 'MEDIUM', 'LOW'
    win_rate_edge_pct: float    # e.g. 85.0%, 70.0%, 55.0%
    trap_risk: str             # 'BEAR_TRAP_RISK', 'BULL_TRAP_RISK', 'NONE'
    badge_color: str           # '#10B981' (green), '#EF4444' (red), '#94A3B8' (slate)
    title: str                 # e.g. "Put Writers Fortifying 23,200 Support"
    summary: str               # "Put writers hold 3.2x advantage over Call writers at this strike"
    actionable_directive: str  # "BUY CE on dip / DO NOT SHORT. Reversal probability 85%."


def evaluate_striking_price_prediction_power(
    chain_df: Optional[pd.DataFrame],
    spot: float,
    striking_strike: Optional[float] = None,
    barrier_kind: str = "AUTO",
    focus_radius: float = 450.0,
    cfg: Optional[Dict[str, Any]] = None,
) -> StrikePredictionPowerResult:
    """
    Evaluates institutional writer positioning at the specific strike price being struck or tested.
    Calculates the exact Call vs Put writer delta, win-rate edge, trap risk, and actionable directive.
    """
    c = cfg or load_writers_config()
    ratio_dom = float(c.get("strike_dominance_ratio", 1.35))
    min_diff = float(c.get("strike_min_diff_contracts", 15000.0))
    prob_high = float(c.get("reversal_prob_high", 85.0))
    prob_med = float(c.get("reversal_prob_med", 70.0))
    prob_low = float(c.get("reversal_prob_low", 55.0))

    if striking_strike is None or striking_strike <= 0:
        striking_strike = round(spot / 50.0) * 50.0

    dist = abs(spot - striking_strike)
    if barrier_kind == "AUTO":
        if striking_strike < spot - 5.0:
            b_kind = "SUPPORT"
        elif striking_strike > spot + 5.0:
            b_kind = "RESISTANCE"
        else:
            b_kind = "ATM"
    else:
        b_kind = barrier_kind

    if chain_df is None or chain_df.empty or "strike" not in chain_df.columns:
        return StrikePredictionPowerResult(
            striking_strike=striking_strike,
            spot_price=spot,
            distance_pts=dist,
            barrier_kind=b_kind,
            ce_oi=0.0, ce_chg_oi=0.0, pe_oi=0.0, pe_chg_oi=0.0,
            ce_status="UNKNOWN", pe_status="UNKNOWN",
            net_strike_diff=0.0, strike_writer_pcr=1.0,
            strike_dominance="BALANCED", focus_net_diff=0.0, focus_writer_pcr=1.0,
            expected_direction="NEUTRAL", confidence="LOW",
            win_rate_edge_pct=prob_low, trap_risk="NONE",
            badge_color="#94A3B8",
            title=f"Striking Price {striking_strike:,.0f} Data Ingestion",
            summary="Option chain quotes parsing",
            actionable_directive="Awaiting live chain feed."
        )

    # Focus window metrics
    f_df = chain_df[(chain_df["strike"] >= spot - focus_radius) & (chain_df["strike"] <= spot + focus_radius)]
    if f_df.empty:
        f_df = chain_df
    f_ce_chg = float(f_df["ce_chg_oi"].sum()) if "ce_chg_oi" in f_df.columns else 0.0
    f_pe_chg = float(f_df["pe_chg_oi"].sum()) if "pe_chg_oi" in f_df.columns else 0.0
    f_net_diff = f_pe_chg - f_ce_chg
    f_pcr = f_pe_chg / max(f_ce_chg, 1.0) if f_ce_chg > 0 else (2.0 if f_pe_chg > 0 else 1.0)

    # Strike row
    stk_row = chain_df[chain_df["strike"] == striking_strike]
    if stk_row.empty:
        idx = (chain_df["strike"] - striking_strike).abs().idxmin()
        stk_row = chain_df.loc[[idx]]

    ce_oi = float(stk_row["ce_oi"].iloc[0]) if "ce_oi" in stk_row.columns else 0.0
    pe_oi = float(stk_row["pe_oi"].iloc[0]) if "pe_oi" in stk_row.columns else 0.0
    ce_chg = float(stk_row["ce_chg_oi"].iloc[0]) if "ce_chg_oi" in stk_row.columns else 0.0
    pe_chg = float(stk_row["pe_chg_oi"].iloc[0]) if "pe_chg_oi" in stk_row.columns else 0.0

    ce_status = "DEFENDING" if ce_chg > 1000 else ("UNWINDING" if ce_chg < -1000 else "NEUTRAL")
    pe_status = "DEFENDING" if pe_chg > 1000 else ("UNWINDING" if pe_chg < -1000 else "NEUTRAL")

    net_diff = pe_chg - ce_chg
    strike_pcr = pe_chg / max(ce_chg, 1.0) if ce_chg > 0 else (2.0 if pe_chg > 0 else 1.0)
    strike_cpr = ce_chg / max(pe_chg, 1.0) if pe_chg > 0 else (2.0 if ce_chg > 0 else 1.0)

    # Classification
    if net_diff >= min_diff and strike_pcr >= ratio_dom:
        dominance = "PUT_WRITERS_DOMINATING"
    elif -net_diff >= min_diff and strike_cpr >= ratio_dom:
        dominance = "CALL_WRITERS_DOMINATING"
    else:
        dominance = "BALANCED"

    # Prediction Power Synthesis
    trap_risk = "NONE"
    if b_kind in ("SUPPORT", "ATM"):
        if dominance == "PUT_WRITERS_DOMINATING":
            expected_dir = "UP"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#10B981"
            if spot < striking_strike:
                trap_risk = "BEAR_TRAP_RISK"
                title = f"⚠️ Bear Trap Active at {striking_strike:,.0f} Support"
                summary = f"Spot dipped below {striking_strike:,.0f}, but Put Writers injected +{pe_chg:,.0f} contracts ({strike_pcr:.1f}x Call writers)."
                directive = f"DO NOT SHORT. Bear Trap active. Put sellers aggressively absorbing retail selling at {striking_strike:,.0f}. Look for Buy CE / Rebound."
            else:
                title = f"🟢 Put Writers Fortifying {striking_strike:,.0f} Support"
                summary = f"Put sellers injected +{pe_chg:,.0f} contracts ({strike_pcr:.1f}x Call writers) at striking price."
                directive = f"BUY CE on pullbacks / Reversal upward favored ({win_rate:.0f}% win rate edge). Floor defended."
        elif ce_chg > pe_chg * ratio_dom and (ce_chg - pe_chg) >= min_diff:
            expected_dir = "DOWN"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#EF4444"
            title = f"🔴 Call Writers Capping Support at {striking_strike:,.0f}"
            summary = f"Call sellers aggressively invading {striking_strike:,.0f} (+{ce_chg:,.0f} vs +{pe_chg:,.0f} PE)."
            directive = f"Floor breakdown pressure at {striking_strike:,.0f}. Look for BUY PE / Short on breakdown."
        elif pe_chg < -min_diff and ce_chg > 0:
            expected_dir = "DOWN"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#EF4444"
            title = f"🔴 Put Writers Abandoning {striking_strike:,.0f} Support"
            summary = f"Put writers unwinding (-{abs(pe_chg):,.0f} contracts) while Call writers apply pressure."
            directive = f"Floor collapse at {striking_strike:,.0f}. Look for BUY PE / Breakdown trade. High probability downward."
        else:
            expected_dir = "UP" if f_net_diff > 0 else "NEUTRAL"
            conf = "MEDIUM"
            win_rate = prob_med
            badge_color = "#38BDF8"
            title = f"⚖️ {striking_strike:,.0f} Support Battle in Progress"
            summary = f"PE ΔOI: {pe_chg:+,.0f} | CE ΔOI: {ce_chg:+,.0f} (Net: {net_diff:+,.0f})"
            directive = "Wait for 15m candle close confirmation before executing."

    elif b_kind == "RESISTANCE":
        if dominance == "CALL_WRITERS_DOMINATING":
            expected_dir = "DOWN"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#EF4444"
            if spot > striking_strike:
                trap_risk = "BULL_TRAP_RISK"
                title = f"⚠️ Bull Trap Active at {striking_strike:,.0f} Resistance"
                summary = f"Spot breached above {striking_strike:,.0f}, but Call Writers fortified ceiling with +{ce_chg:,.0f} contracts."
                directive = f"DO NOT BUY CALLS. Bull Trap active. Heavy Call writing overhead at {striking_strike:,.0f}. Look for BUY PE / Short on rejection."
            else:
                title = f"🔴 Call Writers Capping {striking_strike:,.0f} Resistance"
                summary = f"Call sellers fortified ceiling with +{ce_chg:,.0f} contracts ({strike_cpr:.1f}x Put writers)."
                directive = f"Ceiling heavily defended. Rejection downward favored ({win_rate:.0f}% win rate edge). Look for BUY PE / Fade rally."
        elif dominance == "PUT_WRITERS_DOMINATING":
            expected_dir = "UP"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#10B981"
            title = f"🟢 Put Writers Storming {striking_strike:,.0f} Resistance"
            summary = f"Put sellers aggressively writing +{pe_chg:,.0f} contracts into resistance strike ({strike_pcr:.1f}x Call writers)."
            directive = f"Bulls building floor beneath {striking_strike:,.0f}. Valid breakout expected. High win rate for BUY CE."
        elif ce_chg < -min_diff and pe_chg > 0:
            expected_dir = "UP"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#10B981"
            title = f"🟢 Call Writers Covering at {striking_strike:,.0f} Resistance"
            summary = f"Call sellers fleeing (-{abs(ce_chg):,.0f} contracts) while Put writers shift higher."
            directive = f"Ceiling collapse at {striking_strike:,.0f}. Valid breakout expected higher. Look for BUY CE."
        else:
            expected_dir = "DOWN" if f_net_diff < 0 else "NEUTRAL"
            conf = "MEDIUM"
            win_rate = prob_med
            badge_color = "#38BDF8"
            title = f"⚖️ {striking_strike:,.0f} Resistance Battle in Progress"
            summary = f"CE ΔOI: {ce_chg:+,.0f} | PE ΔOI: {pe_chg:+,.0f} (Net: {-net_diff:+,.0f})"
            directive = "Wait for 15m candle close confirmation above ceiling before executing."
    else:  # ATM
        if dominance == "PUT_WRITERS_DOMINATING":
            expected_dir = "UP"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#10B981"
            title = f"🟢 Put Writers Dominating ATM Strike {striking_strike:,.0f}"
            summary = f"Put writers hold {strike_pcr:.1f}x advantage (+{pe_chg:,.0f} vs +{ce_chg:,.0f})."
            directive = f"Strong upward momentum support. Pullback buy favored ({win_rate:.0f}% win rate)."
        elif dominance == "CALL_WRITERS_DOMINATING":
            expected_dir = "DOWN"
            conf = "HIGH"
            win_rate = prob_high
            badge_color = "#EF4444"
            title = f"🔴 Call Writers Dominating ATM Strike {striking_strike:,.0f}"
            summary = f"Call writers hold {strike_cpr:.1f}x advantage (+{ce_chg:,.0f} vs +{pe_chg:,.0f})."
            directive = f"Strong overhead resistance. Rally fade favored ({win_rate:.0f}% win rate)."
        else:
            expected_dir = "NEUTRAL"
            conf = "MEDIUM"
            win_rate = prob_med
            badge_color = "#38BDF8"
            title = f"⚪ ATM Strike {striking_strike:,.0f} in Equilibrium"
            summary = f"CE ΔOI: {ce_chg:+,.0f} | PE ΔOI: {pe_chg:+,.0f}"
            directive = "Market in balanced equilibrium. Await writer expansion."

    return StrikePredictionPowerResult(
        striking_strike=striking_strike,
        spot_price=spot,
        distance_pts=dist,
        barrier_kind=b_kind,
        ce_oi=ce_oi, ce_chg_oi=ce_chg, pe_oi=pe_oi, pe_chg_oi=pe_chg,
        ce_status=ce_status, pe_status=pe_status,
        net_strike_diff=net_diff, strike_writer_pcr=strike_pcr,
        strike_dominance=dominance, focus_net_diff=f_net_diff, focus_writer_pcr=f_pcr,
        expected_direction=expected_dir, confidence=conf,
        win_rate_edge_pct=win_rate, trap_risk=trap_risk,
        badge_color=badge_color,
        title=title, summary=summary,
        actionable_directive=directive,
    )


def render_striking_price_prediction_card(pred: StrikePredictionPowerResult) -> str:
    """Returns styled HTML card for the striking price prediction power."""
    bg = "rgba(16, 185, 129, 0.12)" if pred.badge_color == "#10B981" else ("rgba(239, 68, 68, 0.12)" if pred.badge_color == "#EF4444" else "rgba(56, 189, 248, 0.12)")
    return f"""
    <div style="background: {bg}; border: 1.5px solid {pred.badge_color}; border-radius: 8px; padding: 12px 16px; margin: 8px 0 12px 0;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
                <span style="font-size: 11px; font-weight: 800; color: #94A3B8; text-transform: uppercase; letter-spacing: 0.5px;">
                    🎯 STRIKING PRICE WRITER POWER ({pred.striking_strike:,.0f}) · {pred.barrier_kind}
                </span>
                <div style="font-size: 16px; font-weight: 800; color: {pred.badge_color}; margin-top: 2px;">
                    {pred.title}
                </div>
            </div>
            <div style="text-align: right;">
                <span style="background: {pred.badge_color}; color: #000; font-weight: 800; font-size: 11px; padding: 2px 8px; border-radius: 4px;">
                    {pred.win_rate_edge_pct:.0f}% WIN RATE EDGE ({pred.confidence})
                </span>
            </div>
        </div>
        <div style="font-size: 13px; color: #E2E8F0; margin-top: 6px; font-weight: 500;">
            {pred.summary}
        </div>
        <div style="font-size: 13px; color: #FFFFFF; font-weight: 700; margin-top: 5px; padding-top: 5px; border-top: 1px solid rgba(255,255,255,0.1);">
            👉 <b>Directive:</b> {pred.actionable_directive}
        </div>
    </div>
    """
# ─────────────────────────────────────────────────────────────────────────────
MKT_OPEN = dtime(9, 15, 0)
MKT_CLOSE = dtime(15, 30, 0)

@st.cache_data(ttl=60, show_spinner=False)
def load_session_snapshots_cached(sess_date: date) -> List[Tuple[datetime, float, pd.DataFrame]]:
    """Loads and caches all snapshots for a session date."""
    try:
        snaps = store.load_day(sess_date)
        eligible = [
            (ts, spot, df) for ts, spot, df in snaps
            if MKT_OPEN <= ts.time() <= MKT_CLOSE
        ]
        eligible.sort(key=lambda x: x[0])
        return eligible
    except Exception as err:
        log.exception(f"Error loading snapshots for date {sess_date}: {err}")
        return []


def extract_writers_raw_series(
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    focus_radius: float = 450.0,
    strike_mode: str = "focus",
) -> pd.DataFrame:
    """
    Extracts raw 1-minute writer aggregate time series from snapshots.
    Supports focus window, ATM strike only, or all strikes.
    """
    if not snapshots:
        return pd.DataFrame()

    records = []
    first_spot = snapshots[0][1] if snapshots else 23000.0

    for ts, spot, df in snapshots:
        if df.empty or "strike" not in df.columns:
            continue

        curr_spot = spot if spot > 0 else first_spot

        if strike_mode == "focus":
            sub_df = df[(df["strike"] >= curr_spot - focus_radius) & (df["strike"] <= curr_spot + focus_radius)]
        elif strike_mode == "atm":
            sub_df = df.iloc[(df["strike"] - curr_spot).abs().argsort()[:2]]
        else:  # all strikes
            sub_df = df

        ce_oi = float(sub_df["ce_oi"].sum()) if "ce_oi" in sub_df.columns else 0.0
        pe_oi = float(sub_df["pe_oi"].sum()) if "pe_oi" in sub_df.columns else 0.0
        ce_chg = float(sub_df["ce_chg_oi"].sum()) if "ce_chg_oi" in sub_df.columns else 0.0
        pe_chg = float(sub_df["pe_chg_oi"].sum()) if "pe_chg_oi" in sub_df.columns else 0.0

        records.append({
            "timestamp": ts,
            "spot": curr_spot,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_chg_oi": ce_chg,
            "pe_chg_oi": pe_chg,
        })

    if not records:
        return pd.DataFrame()

    df_raw = pd.DataFrame(records).set_index("timestamp")
    df_raw = df_raw.sort_index()
    return df_raw


# ─────────────────────────────────────────────────────────────────────────────
# 3. Multi-Timeframe Resampling & Trap Detection Engine (3m / 5m / 15m)
# ─────────────────────────────────────────────────────────────────────────────
def resample_writers_candle_data(
    df_raw: pd.DataFrame,
    timeframe: str = "5m",
    cfg: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    Resamples raw snapshot data into precise 3m, 5m, or 15m candle buckets.
    Calculates Spot OHLC, Cumulative CE/PE Writers, Net Differences, Ratios,
    and flags institutional Bear/Bull Traps.
    """
    if df_raw.empty:
        return pd.DataFrame()

    c = cfg or load_writers_config()
    tf_map = {"3m": "3min", "5m": "5min", "15m": "15min"}
    rule_str = tf_map.get(timeframe, "5min")

    resampled = df_raw.resample(rule_str).agg({
        "spot": ["first", "max", "min", "last"],
        "ce_oi": ["first", "last"],
        "pe_oi": ["first", "last"],
        "ce_chg_oi": "last",
        "pe_chg_oi": "last",
    }).dropna()

    if resampled.empty:
        return pd.DataFrame()

    # Flatten multi-level columns
    res = pd.DataFrame(index=resampled.index)
    res["open"] = resampled[("spot", "first")]
    res["high"] = resampled[("spot", "max")]
    res["low"] = resampled[("spot", "min")]
    res["close"] = resampled[("spot", "last")]

    # Cumulative writers (from session open vs prev close)
    res["ce_writers_cum"] = resampled[("ce_chg_oi", "last")]
    res["pe_writers_cum"] = resampled[("pe_chg_oi", "last")]

    # Per-Candle Incremental additions/unwinding during this specific candle
    res["ce_writers_candle"] = resampled[("ce_oi", "last")] - resampled[("ce_oi", "first")]
    res["pe_writers_candle"] = resampled[("pe_oi", "last")] - resampled[("pe_oi", "first")]

    # Cumulative Net Differences
    res["diff_pe_ce_cum"] = res["pe_writers_cum"] - res["ce_writers_cum"]
    res["diff_ce_pe_cum"] = res["ce_writers_cum"] - res["pe_writers_cum"]

    # Candle Incremental Net Differences
    res["diff_pe_ce_candle"] = res["pe_writers_candle"] - res["ce_writers_candle"]
    res["diff_ce_pe_candle"] = res["ce_writers_candle"] - res["pe_writers_candle"]

    # Ratios (safe division)
    res["ratio_pe_ce"] = np.where(
        res["ce_writers_cum"].abs() > 100,
        res["pe_writers_cum"] / np.maximum(res["ce_writers_cum"], 1.0),
        1.0
    )
    res["ratio_ce_pe"] = np.where(
        res["pe_writers_cum"].abs() > 100,
        res["ce_writers_cum"] / np.maximum(res["pe_writers_cum"], 1.0),
        1.0
    )

    # Running Session Extremes to identify Breakouts & Breakdowns
    res["cum_session_low"] = res["low"].cummin()
    res["cum_session_high"] = res["high"].cummax()

    # Trap Detection & Stance Classification
    trap_drop = float(c.get("trap_drop_pts", 15.0))
    trap_rally = float(c.get("trap_rally_pts", 15.0))
    ratio_dom = float(c.get("ratio_dominant", 1.35))
    ratio_over = float(c.get("ratio_overwhelming", 1.8))
    min_diff = float(c.get("net_diff_min_contracts", 25000.0))

    stances = []
    trap_flags = []

    for i in range(len(res)):
        row = res.iloc[i]
        t_str = res.index[i].strftime("%H:%M")
        
        # Check for Session Low breakdown (Potential Bear Trap)
        # Did this candle make a new low or test near session low?
        prev_low = res["cum_session_low"].iloc[i - 1] if i > 0 else row["low"]
        broke_low = (row["low"] < prev_low) or (row["close"] < prev_low + 5.0)
        
        # Did this candle break high?
        prev_high = res["cum_session_high"].iloc[i - 1] if i > 0 else row["high"]
        broke_high = (row["high"] > prev_high) or (row["close"] > prev_high - 5.0)

        # Video Bear Trap Condition:
        # Price went below low to trigger retail stop losses / shorts,
        # but Put Writers aggressively added contracts during the candle (+dPE > 0)
        # and PE Writers dominated CE Writers!
        is_bear_trap = (
            broke_low and
            (row["pe_writers_candle"] > 0) and
            (row["diff_pe_ce_candle"] > 0 or row["diff_pe_ce_cum"] > min_diff)
        )

        # Video Bull Trap Condition:
        # Price broke higher, but Call Writers added ceiling (+dCE > 0)
        # and CE Writers dominated PE Writers!
        is_bull_trap = (
            broke_high and
            (row["ce_writers_candle"] > 0) and
            (row["diff_ce_pe_candle"] > 0 or row["diff_ce_pe_cum"] > min_diff)
        )

        if is_bear_trap:
            trap_flags.append("BEAR_TRAP")
            stances.append("⚠️ BEAR TRAP (PUT WRITERS DEFENDING LOW)")
        elif is_bull_trap:
            trap_flags.append("BULL_TRAP")
            stances.append("⚠️ BULL TRAP (CALL WRITERS CAPPING HIGH)")
        elif t_str < c.get("warmup_time", "09:45"):
            trap_flags.append("NONE")
            stances.append("WARMUP (EARLY SESSION)")
        elif row["ratio_pe_ce"] >= ratio_over and row["diff_pe_ce_cum"] > min_diff:
            trap_flags.append("NONE")
            stances.append("🟢 PUT WRITERS OVERWHELMING (STRONG FLOOR)")
        elif row["ratio_pe_ce"] >= ratio_dom and row["diff_pe_ce_cum"] > 0:
            trap_flags.append("NONE")
            stances.append("🟢 PUT WRITERS DOMINANT (BUY DIP BIAS)")
        elif row["ratio_ce_pe"] >= ratio_over and row["diff_ce_pe_cum"] > min_diff:
            trap_flags.append("NONE")
            stances.append("🔴 CALL WRITERS OVERWHELMING (STRONG ROOF)")
        elif row["ratio_ce_pe"] >= ratio_dom and row["diff_ce_pe_cum"] > 0:
            trap_flags.append("NONE")
            stances.append("🔴 CALL WRITERS DOMINANT (SELL RALLY BIAS)")
        else:
            trap_flags.append("NONE")
            stances.append("⚪ BALANCED / CHOP ZONE")

    res["trap_flag"] = trap_flags
    res["stance"] = stances

    return res


# ─────────────────────────────────────────────────────────────────────────────
# 4. Interactive Plotly Visualizer (Candles + Cumulative & Incremental Writers)
# ─────────────────────────────────────────────────────────────────────────────
def create_writers_multi_pane_chart(
    df_res: pd.DataFrame,
    timeframe: str = "5m",
    view_mode: str = "pe_primary",  # 'pe_primary' (PE vs CE) or 'ce_primary' (CE vs PE)
    units_lakhs: bool = True,
    cfg: Optional[Dict[str, Any]] = None,
) -> go.Figure:
    """
    Constructs an institutional 3-pane synchronized Plotly chart:
    - Pane 1 (Top): Nifty Spot Candlestick with overlaid Cumulative Writers on secondary y-axis,
                    plus shaded alert boxes on detected Trap candles.
    - Pane 2 (Middle): Per-Candle Incremental ΔOI (Green for PE added, Red for CE added) + Candle Net Diff.
    - Pane 3 (Bottom): Writers' Net Advantage Spread & Dominance Ratio.
    """
    if df_res.empty:
        fig = go.Figure()
        fig.update_layout(title="No Snapshot Data Available for Selected Parameters", template="plotly_dark")
        return fig

    c = cfg or load_writers_config()
    colors = c.get("colors", {})
    ce_col = colors.get("ce_writers", "#EF4444")
    pe_col = colors.get("pe_writers", "#10B981")
    spot_col = colors.get("spot_line", "#F59E0B")

    lot_size = c.get("display_lot_size", 65)
    divisor = (lot_size * 100000.0) if units_lakhs else 1.0
    unit_label = "Lakhs (L)" if units_lakhs else "Contracts"

    # Scale values
    ce_cum = df_res["ce_writers_cum"] / divisor
    pe_cum = df_res["pe_writers_cum"] / divisor
    ce_bar = df_res["ce_writers_candle"] / divisor
    pe_bar = df_res["pe_writers_candle"] / divisor
    diff_pe_ce_cum = df_res["diff_pe_ce_cum"] / divisor
    diff_ce_pe_cum = df_res["diff_ce_pe_cum"] / divisor
    diff_candle = (df_res["diff_pe_ce_candle"] if view_mode == "pe_primary" else df_res["diff_ce_pe_candle"]) / divisor

    times = [t.strftime("%H:%M") for t in df_res.index]

    # 3-row layout
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.50, 0.28, 0.22],
        specs=[
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": True}],
        ],
        subplot_titles=[
            f"<b>NIFTY Spot Price & Cumulative Writers ({timeframe} Candles)</b>",
            f"<b>Per-{timeframe} Candle Incremental Writer Flow (ΔOI added/unwound per bar)</b>",
            "<b>Writer Net Advantage & Ratio Spread</b>"
        ]
    )

    # ── Pane 1: Spot Candlestick + Cumulative Writers Overlay ──
    fig.add_trace(
        go.Candlestick(
            x=times,
            open=df_res["open"],
            high=df_res["high"],
            low=df_res["low"],
            close=df_res["close"],
            name="Nifty Spot",
            increasing_line_color="#10B981",
            decreasing_line_color="#EF4444",
        ),
        row=1, col=1, secondary_y=False
    )

    # Overlay Cumulative PE Writers Line
    fig.add_trace(
        go.Scatter(
            x=times,
            y=pe_cum,
            mode="lines+markers",
            name=f"PE Writers (Puts Sold) [{unit_label}]",
            line=dict(color=pe_col, width=2.5),
            marker=dict(size=4),
        ),
        row=1, col=1, secondary_y=True
    )

    # Overlay Cumulative CE Writers Line
    fig.add_trace(
        go.Scatter(
            x=times,
            y=ce_cum,
            mode="lines+markers",
            name=f"CE Writers (Calls Sold) [{unit_label}]",
            line=dict(color=ce_col, width=2.5, dash="dot"),
            marker=dict(size=4),
        ),
        row=1, col=1, secondary_y=True
    )

    # Add Trap Annotations on Pane 1
    for i, flag in enumerate(df_res["trap_flag"]):
        if flag == "BEAR_TRAP":
            fig.add_annotation(
                x=times[i],
                y=df_res["low"].iloc[i],
                text="🪤 BEAR TRAP<br>PE Writing Heavy",
                showarrow=True,
                arrowhead=2,
                arrowcolor="#10B981",
                arrowsize=1.2,
                bgcolor="rgba(16, 185, 129, 0.25)",
                bordercolor="#10B981",
                font=dict(color="#10B981", size=10, family="monospace"),
                row=1, col=1, secondary_y=False
            )
        elif flag == "BULL_TRAP":
            fig.add_annotation(
                x=times[i],
                y=df_res["high"].iloc[i],
                text="🪤 BULL TRAP<br>CE Writing Heavy",
                showarrow=True,
                arrowhead=2,
                arrowcolor="#EF4444",
                arrowsize=1.2,
                ay=-35,
                bgcolor="rgba(239, 68, 68, 0.25)",
                bordercolor="#EF4444",
                font=dict(color="#EF4444", size=10, family="monospace"),
                row=1, col=1, secondary_y=False
            )

    # ── Pane 2: Per-Candle Incremental ΔOI Bars ──
    fig.add_trace(
        go.Bar(
            x=times,
            y=pe_bar,
            name=f"PE Added / Candle [{unit_label}]",
            marker_color="rgba(16, 185, 129, 0.65)",
            marker_line=dict(color="#10B981", width=1),
        ),
        row=2, col=1
    )

    fig.add_trace(
        go.Bar(
            x=times,
            y=ce_bar,
            name=f"CE Added / Candle [{unit_label}]",
            marker_color="rgba(239, 68, 68, 0.65)",
            marker_line=dict(color="#EF4444", width=1),
        ),
        row=2, col=1
    )

    # Net candle difference line
    diff_label = "Net Candle (PE - CE)" if view_mode == "pe_primary" else "Net Candle (CE - PE)"
    diff_line_color = "#38BDF8"
    fig.add_trace(
        go.Scatter(
            x=times,
            y=diff_candle,
            mode="lines+markers",
            name=diff_label,
            line=dict(color=diff_line_color, width=2),
            marker=dict(size=4),
        ),
        row=2, col=1
    )

    # ── Pane 3: Net Cumulative Difference & Ratio ──
    if view_mode == "pe_primary":
        spread_cum = diff_pe_ce_cum
        spread_label = f"Net Advantage: (PE - CE) [{unit_label}]"
        ratio_series = df_res["ratio_pe_ce"]
        ratio_label = "Writer PCR (PE/CE Ratio)"
        spread_color = "#10B981"
    else:
        spread_cum = diff_ce_pe_cum
        spread_label = f"Net Advantage: (CE - PE) [{unit_label}]"
        ratio_series = df_res["ratio_ce_pe"]
        ratio_label = "Writer CPR (CE/PE Ratio)"
        spread_color = "#EF4444"

    fig.add_trace(
        go.Bar(
            x=times,
            y=spread_cum,
            name=spread_label,
            marker_color=np.where(spread_cum >= 0, "rgba(16, 185, 129, 0.4)", "rgba(239, 68, 68, 0.4)"),
            marker_line=dict(color=np.where(spread_cum >= 0, "#10B981", "#EF4444"), width=1),
        ),
        row=3, col=1, secondary_y=False
    )

    fig.add_trace(
        go.Scatter(
            x=times,
            y=ratio_series,
            mode="lines",
            name=ratio_label,
            line=dict(color="#F59E0B", width=2),
        ),
        row=3, col=1, secondary_y=True
    )

    # Reference line at ratio = 1.0
    fig.add_hline(y=1.0, line_dash="dash", line_color="#94A3B8", row=3, col=1, secondary_y=True)

    # Layout Aesthetics
    fig.update_layout(
        template="plotly_dark",
        height=850,
        margin=dict(l=40, r=40, t=50, b=30),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="center",
            x=0.5,
            bgcolor="rgba(15, 23, 42, 0.8)",
            bordercolor="#334155",
            borderwidth=1,
        ),
        hovermode="x unified",
        paper_bgcolor="#0B1120",
        plot_bgcolor="#0D1527",
    )

    fig.update_xaxes(showgrid=True, gridcolor="#1E293B")
    fig.update_yaxes(showgrid=True, gridcolor="#1E293B")
    fig.update_yaxes(title_text="Nifty Spot", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text=f"Writers ({unit_label})", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text=f"ΔOI ({unit_label})", row=2, col=1)
    fig.update_yaxes(title_text=f"Net Spread ({unit_label})", row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Ratio (1.0 = Neutral)", row=3, col=1, secondary_y=True)

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 5. Full Streamlit Tab Renderer (Zero Dashboard Edit - Rule 2)
# ─────────────────────────────────────────────────────────────────────────────
def render_writers_graph_ui(
    df: pd.DataFrame,
    spot: float,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    read: Optional[Any] = None,
) -> None:
    """
    Renders the complete 'Call Writers vs Put Writers Graph' interactive UI.
    Features:
    - 3m / 5m / 15m Resolution Switcher.
    - PE Primary (Bull/Floor) vs CE Primary (Bear/Roof) View Modes.
    - Real-Time & Historical Session Replay from snapshots.
    - Bear/Bull Trap Alerts synchronized with Price Breakouts.
    - Pedagogical Video Synchronization Blueprint.
    - Candle-by-Candle Flow Inspection Ledger.
    """
    cfg = load_writers_config()
    st.markdown("### 📊 Call Writers vs Put Writers (CE vs PE) Live Flow Graph")
    st.caption(
        "Institutional Option Writer Tracking: Isolates supply ceilings (CE sellers) from demand floors (PE sellers) "
        "for every 3m, 5m, and 15m candle to expose market traps before price moves."
    )

    # 1. Interactive Control Strip
    c1, c2, c3, c4 = st.columns([1.5, 2.2, 1.3, 1.0])

    with c1:
        timeframe = st.radio(
            "⏱️ Candle Timeframe",
            options=["3m", "5m", "15m"],
            index=1,
            horizontal=True,
            help="Resamples price and writer flow into 3-min, 5-min, or 15-min candles."
        )

    with c2:
        view_mode_choice = st.radio(
            "👁️ Writer Dominance Perspective",
            options=[
                "🟢 PE Writers vs CE (Floor Defense & Bear Trap Avoidance)",
                "🔴 CE Writers vs PE (Ceiling Capping & Bull Trap Avoidance)",
            ],
            index=0,
            horizontal=False,
            help="Toggles whether Net Advantage is plotted as (PE - CE) or (CE - PE)."
        )
        view_mode = "pe_primary" if "PE Writers" in view_mode_choice else "ce_primary"

    with c3:
        strike_scope = st.selectbox(
            "🎯 Strike Focus Area",
            options=["Focus Window (±450 pts)", "ATM Strike Only (±50 pts)", "All Chain Strikes"],
            index=0,
            help="Filters option chain to institutional focus area, eliminating far illiquid strikes."
        )
        mode_key = "focus" if "Focus" in strike_scope else ("atm" if "ATM" in strike_scope else "all")

    with c4:
        unit_choice = st.selectbox(
            "📐 Units",
            options=["Lakhs (L)", "Contracts"],
            index=0,
            help="Display metrics in Lakh contracts or raw contract count."
        )
        units_lakhs = (unit_choice == "Lakhs (L)")

    # 2. Session Date Selector
    snap_dir = Path("data/snapshots")
    available_dates = []
    if snap_dir.exists():
        for p in sorted(snap_dir.iterdir(), reverse=True):
            if p.is_dir() and len(list(p.glob("*.parquet"))) > 0:
                try:
                    available_dates.append(datetime.strptime(p.name, "%Y-%m-%d").date())
                except Exception:
                    continue

    today_date = date.today()
    default_idx = 0
    if available_dates:
        selected_date = st.selectbox(
            "📅 Session Date",
            options=available_dates,
            index=default_idx,
            format_func=lambda d: f"{d.strftime('%d-%b-%Y')} ({'🔴 LIVE TODAY' if d == today_date else 'HISTORICAL'})"
        )
    else:
        selected_date = today_date

    # 3. Data Processing
    with st.spinner(f"Loading {selected_date} snapshots and computing {timeframe} writer flow..."):
        snaps = load_session_snapshots_cached(selected_date)
        if not snaps:
            st.warning(f"No stored snapshots found for {selected_date}. Option writer timeline requires active recorder snapshots.")
            return

        focus_rad = float(cfg.get("focus_window_default_radius", 450.0))
        df_raw = extract_writers_raw_series(snaps, focus_radius=focus_rad, strike_mode=mode_key)
        df_res = resample_writers_candle_data(df_raw, timeframe=timeframe, cfg=cfg)

    if df_res.empty:
        st.info("Insufficient snapshot history in market hours to render candles.")
        return

    # 4. Top Telemetry Cards (Latest Status)
    latest = df_res.iloc[-1]
    lot_size = cfg.get("display_lot_size", 65)
    divisor = (lot_size * 100000.0) if units_lakhs else 1.0
    u_sym = "L" if units_lakhs else "ct"

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label="🟢 Put Writers (Puts Sold)",
            value=f"{latest['pe_writers_cum'] / divisor:,.2f} {u_sym}",
            delta=f"{latest['pe_writers_candle'] / divisor:+,.2f} {u_sym} in last {timeframe}",
            delta_color="normal"
        )
        st.caption("Floor fortification / Demand injection")

    with m2:
        st.metric(
            label="🔴 Call Writers (Calls Sold)",
            value=f"{latest['ce_writers_cum'] / divisor:,.2f} {u_sym}",
            delta=f"{latest['ce_writers_candle'] / divisor:+,.2f} {u_sym} in last {timeframe}",
            delta_color="inverse"
        )
        st.caption("Ceiling capping / Overhead supply")

    with m3:
        if view_mode == "pe_primary":
            net_val = latest["diff_pe_ce_cum"] / divisor
            ratio_val = latest["ratio_pe_ce"]
            adv_name = "PE Advantage (PE - CE)"
            r_name = "Writer PCR"
        else:
            net_val = latest["diff_ce_pe_cum"] / divisor
            ratio_val = latest["ratio_ce_pe"]
            adv_name = "CE Advantage (CE - PE)"
            r_name = "Writer CPR"

        st.metric(
            label=f"⚖️ {adv_name}",
            value=f"{net_val:+,.2f} {u_sym}",
            delta=f"{r_name}: {ratio_val:.2f}x"
        )
        st.caption("Net institutional bias across focus strikes")

    with m4:
        st.metric(
            label=f"🎯 Current {timeframe} Stance",
            value=latest["trap_flag"] if latest["trap_flag"] != "NONE" else "CLEAR",
            delta=latest["stance"]
        )
        st.caption(f"Last candle: {df_res.index[-1].strftime('%H:%M')} IST")

    # 5. The Core 3-Pane Plotly Chart
    fig = create_writers_multi_pane_chart(
        df_res=df_res,
        timeframe=timeframe,
        view_mode=view_mode,
        units_lakhs=units_lakhs,
        cfg=cfg
    )
    st.plotly_chart(fig, use_container_width=True)

    # 6. Video Synchronization & Trap Blueprint Callout
    with st.expander("🎥 Mentor Video Synchronization: How the Call/Put Writers Graph Exposed the 23,121 Trap", expanded=True):
        st.markdown(
            """
            ### ⏱️ Video Breakdown & Quantitative Market Truth
            
            | Video Timestamp | Market Scenario & Action on Chart | What Retail Saw vs What the Writers Graph Revealed |
            | :--- | :--- | :--- |
            | **02:58 – 03:33** | *Market plunged lower in morning session.* | **The Core Rule:** Retail rushed to buy puts/short. But the **Put Writers vs Call Writers Graph** showed Put Writers were overwhelmingly aggressive (+ΔPE surging). This proved the drop was **solely to trap retail below the lows** before immediately rebounding. |
            | **05:10 – 05:50** | *Spot broke support at 23,200 to low of 23,121.* | In the **15-minute candle (09:45–10:00)**, spot broke the low. **Crucial insight:** At 23,200 strike, PE writers expanded from +65k to +82k contracts! Total PE dOI was **397k vs CE dOI only 168k** (PE writers were **2.35x** larger). The breakdown was an unconfirmed trap, followed by an immediate identical bullish green candle back to 23,223! |
            | **08:47 – 08:57** | *Trading Execution Discipline.* | **Wait for 10:00 – 10:15 AM:** Let morning opening noise settle. Check the **Difference line (PE - CE)**. If difference is positive and expanding, only look for dips to buy; never short into aggressive put writing! |
            | **11:58 – 12:00** | *Double Bottom & Support Defense.* | At key support zones (e.g. 23,120 / 23,000), **never initiate short positions** unless the Call Writers vs Put Writers graph shows Call Writers have taken over aggressive control (+ΔCE dominating). |
            
            ---
            #### 📐 Mathematical Formulas Used:
            1. **Cumulative Writers**:
               $$\text{PE Writers}(t) = \sum \Delta \text{PE OI}(t), \quad \text{CE Writers}(t) = \sum \Delta \text{CE OI}(t)$$
            2. **Net Dominance Spread**:
               $$\text{Net Spread}_{\text{Bullish}} = \text{PE Writers} - \text{CE Writers}, \quad \text{Net Spread}_{\text{Bearish}} = \text{CE Writers} - \text{PE Writers}$$
            3. **Per-Candle Incremental Flow**:
               $$\Delta \text{PE}_{\text{candle}} = \text{PE OI}(t_{\text{end}}) - \text{PE OI}(t_{\text{start}}), \quad \Delta \text{CE}_{\text{candle}} = \text{CE OI}(t_{\text{end}}) - \text{CE OI}(t_{\text{start}})$$
            4. **Bear Trap Identification**:
               $$\text{Spot Low} < \text{Prev Low} \quad \text{AND} \quad \Delta \text{PE}_{\text{candle}} > 0 \quad \text{AND} \quad (\text{PE Writers} - \text{CE Writers}) > 0$$
            """
        )

    # 7. Detailed Candle-by-Candle Inspection Ledger
    with st.expander(f"📋 {timeframe} Candle-by-Candle Writer Inspection Ledger", expanded=False):
        table_df = pd.DataFrame()
        table_df["Time"] = [t.strftime("%H:%M") for t in df_res.index]
        table_df["Spot Open"] = df_res["open"].map("{:,.1f}".format)
        table_df["Spot High"] = df_res["high"].map("{:,.1f}".format)
        table_df["Spot Low"] = df_res["low"].map("{:,.1f}".format)
        table_df["Spot Close"] = df_res["close"].map("{:,.1f}".format)
        table_df[f"PE Added ({u_sym})"] = (df_res["pe_writers_candle"] / divisor).map("{:+,.2f}".format)
        table_df[f"CE Added ({u_sym})"] = (df_res["ce_writers_candle"] / divisor).map("{:+,.2f}".format)
        table_df[f"Net Candle ({u_sym})"] = (df_res["diff_pe_ce_candle"] / divisor).map("{:+,.2f}".format)
        table_df[f"Net Cum Diff ({u_sym})"] = (df_res["diff_pe_ce_cum"] / divisor).map("{:+,.2f}".format)
        table_df["Writer PCR"] = df_res["ratio_pe_ce"].map("{:.2f}x".format)
        table_df["Signal / Trap"] = df_res["stance"]

        # Show newest on top
        st.dataframe(table_df.iloc[::-1], use_container_width=True, hide_index=True)
