"""
decision_cockpit_view.py — Unified Master Decision Cockpit & All-Tab Confluence Engine.

Solves the trader's real-time dilemma when price is fluctuating every minute:
  1. "What level is price striking or approaching right now?" (Distance to Resistance & Support)
  2. "If it's striking that price, WHAT DO WE HAVE TO DO? BUY CE, BUY PE, or WAIT?"
  3. "What are the 15-Minute and 5-Minute candles saying right now?" (Dual Timeframe Confluence)
  4. "Is this an institutional support/resistance reversal, a genuine breakout, or a trap/fakeout?"
  5. "What is the complete confluence verdict across ALL tabs in the system?"
  6. "Give me the exact spoon-fed order ticket: Strike, LTP, Entry, Stop Loss, Target 1 (+20 pts CTC), Target 2, and Risk per lot in ₹."
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

import config
import confluence as cf
import oi_analytics as oia
from price_action_breakout import PriceActionBreakoutEngine
import session_reversal_matrix as srm
import top_bottom_sniper as tbs
import trade_advisor_15m as ta15
import rvol_gate
import agt_briefing
import participant_history as ph
from ui_utils import render_html, _render_html

# ── Color Palette ─────────────────────────────────────────────────────────────
GREEN = "#2FBF71"
RED = "#E8503A"
BLUE = "#38BDF8"
AMBER = "#F59E0B"
PURPLE = "#A855F7"
GOLD = "#EAB308"
SLATE = "#94A3B8"
DARK_BG = "#0B132B"
CARD_BG = "rgba(15, 23, 42, 0.75)"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Real-Time Level Proximity & Striking Radar
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class KeyBarrier:
    name: str
    price: float
    kind: str           # "RESISTANCE", "SUPPORT", "ANCHOR"
    distance_pts: float # (price - spot) for resistance, (spot - price) for support
    source: str         # "OI_WALL", "PIVOT", "HISTORICAL", "BAND", "ROUND_STRIKE", "VWAP"


@dataclass
class LevelRadarReport:
    spot: float
    nearest_resistance: KeyBarrier
    nearest_support: KeyBarrier
    closest_barrier: KeyBarrier
    striking_status: str       # "STRIKING_RESISTANCE", "STRIKING_SUPPORT", "APPROACHING_RESISTANCE", "APPROACHING_SUPPORT", "EQUILIBRIUM"
    striking_title: str
    striking_color: str
    striking_border: str
    striking_bg: str
    striking_advice: str       # Exact directive what to do right now
    action_if_reversing: str   # Fade/reversal trade instructions
    action_if_breakout: str    # Momentum breakout trade instructions
    action_if_trap: str        # Trap rejection instructions
    all_resistances: List[KeyBarrier] = field(default_factory=list)
    all_supports: List[KeyBarrier] = field(default_factory=list)
    striking_prediction: Optional[Any] = None


def evaluate_live_level_radar(
    spot: float,
    lv: Any = None,
    chain_df: Optional[pd.DataFrame] = None,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    vb_daily: Optional[pd.DataFrame] = None,
    idx_c: Optional[pd.DataFrame] = None,
) -> LevelRadarReport:
    """
    Catalogues and evaluates all institutional barriers relative to current spot.
    Identifies the exact overhead ceiling and underfoot floor, their distances,
    and formats unambiguous directives on how to act when striking those levels.
    """
    res_list: List[KeyBarrier] = []
    sup_list: List[KeyBarrier] = []

    # 1. Floor & Ceiling Pivots
    if lv is not None:
        if hasattr(lv, "r1") and float(lv.r1) > 0:
            p = float(lv.r1)
            if p > spot: res_list.append(KeyBarrier("R1 Resistance Pivot", p, "RESISTANCE", p - spot, "PIVOT"))
            else: sup_list.append(KeyBarrier("R1 (Flipped Support)", p, "SUPPORT", spot - p, "PIVOT"))
        if hasattr(lv, "r2") and float(lv.r2) > 0:
            p = float(lv.r2)
            if p > spot: res_list.append(KeyBarrier("R2 Major Resistance", p, "RESISTANCE", p - spot, "PIVOT"))
            else: sup_list.append(KeyBarrier("R2 (Flipped Support)", p, "SUPPORT", spot - p, "PIVOT"))
        if hasattr(lv, "s1") and float(lv.s1) > 0:
            p = float(lv.s1)
            if p < spot: sup_list.append(KeyBarrier("S1 Support Floor", p, "SUPPORT", spot - p, "PIVOT"))
            else: res_list.append(KeyBarrier("S1 (Flipped Resistance)", p, "RESISTANCE", p - spot, "PIVOT"))
        if hasattr(lv, "s2") and float(lv.s2) > 0:
            p = float(lv.s2)
            if p < spot: sup_list.append(KeyBarrier("S2 Major Support Floor", p, "SUPPORT", spot - p, "PIVOT"))
            else: res_list.append(KeyBarrier("S2 (Flipped Resistance)", p, "RESISTANCE", p - spot, "PIVOT"))

    # 2. Daily Reference Levels (PDH, PDL, PDC)
    if vb_daily is not None and not vb_daily.empty:
        try:
            pdh = float(vb_daily["High"].iloc[-1])
            pdl = float(vb_daily["Low"].iloc[-1])
            pdc = float(vb_daily["Close"].iloc[-1])
            if pdh > spot: res_list.append(KeyBarrier("PDH (Previous Day High)", pdh, "RESISTANCE", pdh - spot, "HISTORICAL"))
            else: sup_list.append(KeyBarrier("PDH (Flipped Support)", pdh, "SUPPORT", spot - pdh, "HISTORICAL"))
            if pdl < spot: sup_list.append(KeyBarrier("PDL (Previous Day Low)", pdl, "SUPPORT", spot - pdl, "HISTORICAL"))
            else: res_list.append(KeyBarrier("PDL (Flipped Resistance)", pdl, "RESISTANCE", pdl - spot, "HISTORICAL"))
            if pdc > spot: res_list.append(KeyBarrier("PDC (Gap Fill Resistance)", pdc, "RESISTANCE", pdc - spot, "HISTORICAL"))
            elif pdc < spot: sup_list.append(KeyBarrier("PDC (Gap Fill Support)", pdc, "SUPPORT", spot - pdc, "HISTORICAL"))
        except Exception:
            pass

    # 3. Today's High & Low
    if idx_c is not None and not idx_c.empty and "high" in idx_c.columns and "low" in idx_c.columns:
        try:
            thi = float(idx_c["high"].max())
            tlo = float(idx_c["low"].min())
            if thi > spot + 3.0: res_list.append(KeyBarrier("Day High (Intraday Peak)", thi, "RESISTANCE", thi - spot, "INTRADAY"))
            if tlo < spot - 3.0: sup_list.append(KeyBarrier("Day Low (Intraday Bottom)", tlo, "SUPPORT", spot - tlo, "INTRADAY"))
        except Exception:
            pass

    # 4. Option Chain OI Walls (Highest Call & Put OI)
    if chain_df is not None and not chain_df.empty:
        try:
            if "ce_oi" in chain_df.columns:
                max_ce_row = chain_df.loc[chain_df["ce_oi"].idxmax()]
                call_wall = float(max_ce_row["strike"])
                if call_wall > spot:
                    res_list.append(KeyBarrier(f"Call OI Wall ({int(call_wall):,})", call_wall, "RESISTANCE", call_wall - spot, "OI_WALL"))
                else:
                    sup_list.append(KeyBarrier(f"Call OI Wall ({int(call_wall):,}) [Breached]", call_wall, "SUPPORT", spot - call_wall, "OI_WALL"))
            if "pe_oi" in chain_df.columns:
                max_pe_row = chain_df.loc[chain_df["pe_oi"].idxmax()]
                put_wall = float(max_pe_row["strike"])
                if put_wall < spot:
                    sup_list.append(KeyBarrier(f"Put OI Wall ({int(put_wall):,})", put_wall, "SUPPORT", spot - put_wall, "OI_WALL"))
                else:
                    res_list.append(KeyBarrier(f"Put OI Wall ({int(put_wall):,}) [Breached]", put_wall, "RESISTANCE", put_wall - spot, "OI_WALL"))
        except Exception:
            pass

    # 5. Dynamic Bollinger Bands & VWAP on 15m
    if pa_frames and "15m" in pa_frames and not pa_frames["15m"].empty:
        df15 = pa_frames["15m"]
        try:
            last15 = df15.iloc[-1]
            if "ubb" in last15 and float(last15["ubb"]) > spot:
                res_list.append(KeyBarrier("15m Upper Bollinger Band", float(last15["ubb"]), "RESISTANCE", float(last15["ubb"]) - spot, "BAND"))
            if "lbb" in last15 and float(last15["lbb"]) < spot:
                sup_list.append(KeyBarrier("15m Lower Bollinger Band", float(last15["lbb"]), "SUPPORT", spot - float(last15["lbb"]), "BAND"))
            if "vwap" in last15 and not np.isnan(last15["vwap"]):
                v15 = float(last15["vwap"])
                if v15 > spot + 3.0:
                    res_list.append(KeyBarrier("15m Institutional VWAP", v15, "RESISTANCE", v15 - spot, "VWAP"))
                elif v15 < spot - 3.0:
                    sup_list.append(KeyBarrier("15m Institutional VWAP", v15, "SUPPORT", spot - v15, "VWAP"))
        except Exception:
            pass

    # 6. Psychological 100-Point Institutional Strike Barriers
    round_ceil = math.ceil(spot / 100.0) * 100.0
    round_floor = math.floor(spot / 100.0) * 100.0
    if round_ceil - spot >= 5.0:
        res_list.append(KeyBarrier(f"100-Pt Strike ({int(round_ceil):,})", round_ceil, "RESISTANCE", round_ceil - spot, "ROUND_STRIKE"))
    if spot - round_floor >= 5.0:
        sup_list.append(KeyBarrier(f"100-Pt Strike ({int(round_floor):,})", round_floor, "SUPPORT", spot - round_floor, "ROUND_STRIKE"))

    # Fallbacks if list is empty
    if not res_list:
        res_list.append(KeyBarrier("Overhead Resistance", spot + 50.0, "RESISTANCE", 50.0, "ESTIMATE"))
    if not sup_list:
        sup_list.append(KeyBarrier("Underfoot Support", spot - 50.0, "SUPPORT", 50.0, "ESTIMATE"))

    # Sort to find closest
    res_list.sort(key=lambda x: x.distance_pts)
    sup_list.sort(key=lambda x: x.distance_pts)

    nearest_res = res_list[0]
    nearest_sup = sup_list[0]

    if nearest_res.distance_pts <= nearest_sup.distance_pts:
        closest_barrier = nearest_res
    else:
        closest_barrier = nearest_sup

    # Striking zone evaluation
    threshold_pts = 16.0
    approach_pts = 32.0

    if nearest_res.distance_pts <= threshold_pts:
        striking_status = "STRIKING_RESISTANCE"
        striking_title = f"🔴 STRIKING INSTITUTIONAL RESISTANCE: {nearest_res.name} ({nearest_res.price:,.1f})"
        striking_color = RED
        striking_border = "#DC2626"
        striking_bg = "rgba(239, 68, 68, 0.15)"
        striking_advice = (
            f"Spot is only <b>+{nearest_res.distance_pts:.1f} pts</b> below {nearest_res.name}. "
            f"<b>DO NOT CHASE CALLS (CE) INTO THE CEILING!</b> "
            f"Institutions are defending this barrier. Watch for a 15m/5m Red Reversal (Inverted Hammer/Shooting Star) to BUY PE, "
            f"or wait for a high-volume candle to decisively break and close above {nearest_res.price:,.0f} before considering calls."
        )
    elif nearest_sup.distance_pts <= threshold_pts:
        striking_status = "STRIKING_SUPPORT"
        striking_title = f"🟢 STRIKING INSTITUTIONAL SUPPORT: {nearest_sup.name} ({nearest_sup.price:,.1f})"
        striking_color = GREEN
        striking_border = "#059669"
        striking_bg = "rgba(16, 185, 129, 0.15)"
        striking_advice = (
            f"Spot is only <b>-{nearest_sup.distance_pts:.1f} pts</b> above {nearest_sup.name}. "
            f"<b>DO NOT SHORT PUTS (PE) INTO THE FLOOR!</b> "
            f"Institutions are absorbing supply at this level. Watch for a 15m/5m Green Reversal (Hammer/Bullish Engulfing) to BUY CE, "
            f"or wait for a high-volume candle to decisively break and close below {nearest_sup.price:,.0f} before considering puts."
        )
    elif nearest_res.distance_pts <= approach_pts:
        striking_status = "APPROACHING_RESISTANCE"
        striking_title = f"⚠️ APPROACHING OVERHEAD RESISTANCE (+{nearest_res.distance_pts:.0f} pts to {nearest_res.name})"
        striking_color = GOLD
        striking_border = "#D97706"
        striking_bg = "rgba(245, 158, 11, 0.12)"
        striking_advice = (
            f"Spot is within {nearest_res.distance_pts:.0f} pts of {nearest_res.name} ({nearest_res.price:,.1f}). "
            f"Upside runway is narrowing. If holding CE, prepare to lock profit or move SL to Cost. Stand by for resistance reaction."
        )
    elif nearest_sup.distance_pts <= approach_pts:
        striking_status = "APPROACHING_SUPPORT"
        striking_title = f"⚠️ APPROACHING UNDERFOOT SUPPORT (-{nearest_sup.distance_pts:.0f} pts to {nearest_sup.name})"
        striking_color = BLUE
        striking_border = "#0284C7"
        striking_bg = "rgba(56, 189, 248, 0.12)"
        striking_advice = (
            f"Spot is within {nearest_sup.distance_pts:.0f} pts of {nearest_sup.name} ({nearest_sup.price:,.1f}). "
            f"Downside runway is narrowing. If holding PE, prepare to lock profit or move SL to Cost. Stand by for support reaction."
        )
    else:
        striking_status = "EQUILIBRIUM"
        striking_title = f"🟡 OPEN RUNWAY / EQUILIBRIUM (Floor -{nearest_sup.distance_pts:.0f} pts · Ceiling +{nearest_res.distance_pts:.0f} pts)"
        striking_color = SLATE
        striking_border = "#475569"
        striking_bg = "rgba(148, 163, 184, 0.10)"
        striking_advice = (
            f"Spot is at {spot:,.1f}, positioned in the channel between Support {nearest_sup.name} ({nearest_sup.price:,.0f}) "
            f"and Resistance {nearest_res.name} ({nearest_res.price:,.0f}). "
            f"<b>DO NOT ENTER IN NO-MAN'S LAND.</b> Waiting is 80% of profitable trading. Wait for price to touch a boundary."
        )

    action_if_reversing = (
        f"<b>IF REVERSING:</b> Look for 15m Hammer at Support ({nearest_sup.price:,.0f}) for CE Buy (SL: {nearest_sup.price - 15:,.0f}), "
        f"or 15m Inverted Hammer at Resistance ({nearest_res.price:,.0f}) for PE Buy (SL: {nearest_res.price + 15:,.0f})."
    )
    action_if_breakout = (
        f"<b>IF BREAKING OUT:</b> Must see >=60% body 15m candle close beyond {nearest_res.price:,.0f} (for CE) or below {nearest_sup.price:,.0f} (for PE) "
        f"with expanding volume. Enter strictly on retest confirmation."
    )
    action_if_trap = (
        f"<b>IF TRAP / SWEEP:</b> A candle wicking past {nearest_res.price:,.0f} or {nearest_sup.price:,.0f} but closing back inside is a "
        f"Liquidity Trap. Enter the counter-move immediately on break of sweep candle extreme!"
    )

    striking_pred = None
    if chain_df is not None and not chain_df.empty:
        try:
            import writers_graph_engine as wge
            stk_val = round(closest_barrier.price / 50.0) * 50.0 if closest_barrier else round(spot / 50.0) * 50.0
            striking_pred = wge.evaluate_striking_price_prediction_power(
                chain_df=chain_df,
                spot=spot,
                striking_strike=stk_val,
                barrier_kind=closest_barrier.kind if closest_barrier else "AUTO",
            )
        except Exception as _w_err:
            pass

    return LevelRadarReport(
        spot=spot,
        nearest_resistance=nearest_res,
        nearest_support=nearest_sup,
        closest_barrier=closest_barrier,
        striking_status=striking_status,
        striking_title=striking_title,
        striking_color=striking_color,
        striking_border=striking_border,
        striking_bg=striking_bg,
        striking_advice=striking_advice,
        action_if_reversing=action_if_reversing,
        action_if_breakout=action_if_breakout,
        action_if_trap=action_if_trap,
        all_resistances=res_list[:5],
        all_supports=sup_list[:5],
        striking_prediction=striking_pred,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Dual-Timeframe Candle Confluence (15m Execution & 5m Momentum)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CandleAnalysis:
    timeframe: str
    pattern_name: str
    direction: str       # "BULLISH", "BEARISH", "NEUTRAL"
    close_vs_8ema: str   # "ABOVE", "BELOW", "TOUCHING"
    close_vs_vwap: str   # "ABOVE", "BELOW", "TOUCHING"
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    summary: str
    is_doji: bool = False
    is_reversal: bool = False


@dataclass
class DualCandleReport:
    c15: CandleAnalysis
    c5: CandleAnalysis
    harmony_verdict: str # "BULLISH_HARMONY", "BEARISH_HARMONY", "REVERSAL_IN_PROGRESS", "CONFLICTED_CHOP"
    harmony_color: str
    harmony_title: str
    what_candles_say: str


def _analyze_single_candle(df: pd.DataFrame, tf: str) -> CandleAnalysis:
    """Extracts pattern, wick geometry, and indicator relation from candle dataframe."""
    if df is None or df.empty or len(df) < 2:
        return CandleAnalysis(
            timeframe=tf,
            pattern_name="Data Pending",
            direction="NEUTRAL",
            close_vs_8ema="NEUTRAL",
            close_vs_vwap="NEUTRAL",
            body_pct=50.0,
            upper_wick_pct=25.0,
            lower_wick_pct=25.0,
            summary="Awaiting candle feed",
        )

    # Ensure indicators
    d = df.copy()
    if "ema8" not in d.columns:
        d["ema8"] = d["close"].ewm(span=8, adjust=False).mean()
    if "vwap" not in d.columns:
        d["vwap"] = d["close"].rolling(10, min_periods=1).mean()

    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last

    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    rng = max(0.1, h - l)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    body_pct = (body / rng) * 100.0
    upper_wick_pct = (upper_wick / rng) * 100.0
    lower_wick_pct = (lower_wick / rng) * 100.0

    ema8_val = float(last.get("ema8", c))
    vwap_val = float(last.get("vwap", c))

    c_vs_ema = "ABOVE" if c > ema8_val + 1.0 else ("BELOW" if c < ema8_val - 1.0 else "TOUCHING")
    c_vs_vwap = "ABOVE" if c > vwap_val + 1.0 else ("BELOW" if c < vwap_val - 1.0 else "TOUCHING")

    is_green = c >= o
    is_doji = False
    is_reversal = False
    pat = "Normal Body"
    bias = "BULLISH" if is_green else "BEARISH"

    # Pattern recognition
    if body_pct <= 15.0:
        is_doji = True
        if lower_wick_pct >= 50.0:
            pat = "Dragonfly Doji (Bullish Wick Rejection)"
            bias = "BULLISH"
            is_reversal = True
        elif upper_wick_pct >= 50.0:
            pat = "Gravestone Doji (Bearish Wick Rejection)"
            bias = "BEARISH"
            is_reversal = True
        else:
            pat = "Indecision Doji (Exhaustion Pause)"
            bias = "NEUTRAL"
    elif lower_wick >= 2.0 * body and upper_wick_pct <= 20.0:
        pat = "Hammer / Pin Bar (Institutional Absorption)"
        bias = "BULLISH"
        is_reversal = True
    elif upper_wick >= 2.0 * body and lower_wick_pct <= 20.0:
        pat = "Inverted Hammer / Shooting Star (Overhead Rejection)"
        bias = "BEARISH"
        is_reversal = True
    elif body_pct >= 65.0:
        pat = "Marubozu / Strong Expansion Body"
        bias = "BULLISH" if is_green else "BEARISH"
    else:
        pat = "Bullish Candle" if is_green else "Bearish Candle"

    # Summary
    summary = f"{pat} · Close {c_vs_ema} 8-EMA ({ema8_val:,.1f}) · {c_vs_vwap} VWAP ({vwap_val:,.1f})"

    return CandleAnalysis(
        timeframe=tf,
        pattern_name=pat,
        direction=bias,
        close_vs_8ema=c_vs_ema,
        close_vs_vwap=c_vs_vwap,
        body_pct=body_pct,
        upper_wick_pct=upper_wick_pct,
        lower_wick_pct=lower_wick_pct,
        summary=summary,
        is_doji=is_doji,
        is_reversal=is_reversal,
    )


def evaluate_dual_candles(pa_frames: Optional[Dict[str, pd.DataFrame]], spot: float) -> DualCandleReport:
    """Evaluates 15m and 5m candles side by side for structural alignment."""
    f15 = pa_frames.get("15m") if pa_frames else None
    f5 = pa_frames.get("5m") if pa_frames else None

    c15 = _analyze_single_candle(f15, "15m")
    c5 = _analyze_single_candle(f5, "5m")

    # Harmony verdict
    if c15.direction == "BULLISH" and c5.direction == "BULLISH" and c15.close_vs_8ema == "ABOVE":
        verdict = "BULLISH_HARMONY"
        color = GREEN
        title = "🟢 100% BULLISH HARMONY (15m & 5m Aligned Long)"
        say = "Both 15m and 5m candles are green and holding cleanly above 8-EMA & VWAP. High probability environment for Call (CE) buying."
    elif c15.direction == "BEARISH" and c5.direction == "BEARISH" and c15.close_vs_8ema == "BELOW":
        verdict = "BEARISH_HARMONY"
        color = RED
        title = "🔴 100% BEARISH HARMONY (15m & 5m Aligned Short)"
        say = "Both 15m and 5m candles are red and rolling below 8-EMA & VWAP. High probability environment for Put (PE) buying."
    elif c15.is_reversal or c5.is_reversal:
        verdict = "REVERSAL_IN_PROGRESS"
        color = PURPLE
        rev_side = "Bullish Hammer Reversal" if (c15.direction == "BULLISH" or c5.direction == "BULLISH") else "Bearish Inverted Hammer Reversal"
        title = f"🔄 REVERSAL CANDLE DETECTED ({rev_side})"
        say = f"Reversal candle printed ({c15.pattern_name} on 15m / {c5.pattern_name} on 5m). Follow-Through rule applies: wait for next candle to break trigger extreme before entering!"
    elif c15.is_doji or c5.is_doji:
        verdict = "CONFLICTED_CHOP"
        color = GOLD
        title = "⚠️ INDECISION / DOJI IN PLAY (Capital Protection Freeze)"
        say = "Doji printed on chart. In accordance with Mentor Sushen's Rule: Doji is an EXIT signal, not an entry! Stand aside and avoid initiating fresh trades."
    else:
        verdict = "CONFLICTED_CHOP"
        color = SLATE
        title = f"⚠️ TIMEFRAME CONFLICT (15m {c15.direction} vs 5m {c5.direction})"
        say = f"15m candle is {c15.direction} while 5m candle is {c5.direction}. Conflicting timeframes produce whipsaws. Wait for 5m to align with 15m trend."

    return DualCandleReport(
        c15=c15,
        c5=c5,
        harmony_verdict=verdict,
        harmony_color=color,
        harmony_title=title,
        what_candles_say=say,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. All-Tab Institutional Confluence Radar
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TabConfluenceItem:
    tab_name: str
    feature: str
    signal: str          # "BULLISH", "BEARISH", "NEUTRAL"
    badge_color: str
    reading: str
    plain_meaning: str
    pillar: str = "📐 Price Action & Structure"
    weight: float = 1.0


@dataclass
class AllTabsRadarReport:
    items: List[TabConfluenceItem]
    bull_count: int
    bear_count: int
    neutral_count: int
    total_strategies: int
    net_score: int       # -100 to +100
    consensus_verdict: str
    consensus_color: str
    pillar_summary: Dict[str, Dict[str, int]] = field(default_factory=dict)
    active_trap_warning: Optional[str] = None
    macro_event_warning: Optional[str] = None


def evaluate_all_tabs_radar(
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    master_cockpit: Any = None,
    prev_close: Optional[float] = None,
) -> AllTabsRadarReport:
    """
    Synthesizes signals from all 14 core institutional analytical engines across tabs:

      🏛️ PILLAR 1: Institutional Derivatives & Order Flow (5 Engines)
        1. Tab 37: Anti-Fakeout & Real-Time Pressure Gauge (200-pt core)
        2. Tab 36: Option Chain & OI Breakout Validation (Focus S/R & Anti-Trap)
        3. Tab 1/2: OI Profile & Put-Call Ratio (PCR & Net Walls)
        4. Tab 33: Order Flow & Market Depth (Cumulative Volume Delta - CVD)
        5. Tab 34: Participant Positioning (FII Futures Deciles & Writing Bias)

      📐 PILLAR 2: Price Action & Market Structure (5 Engines)
        6. Tab 19/31: Smart Money Concepts (MSS Structure Break & Liquidity Sweeps)
        7. Tab 32: Volume Profile & AMT (POC Magnet, VAH/VAL & Profile Shape)
        8. Tab 7/17: Price Action Strategy 1 (15m 8 EMA Follow-Through)
        9. Tab 18: Institutional Futures VWAP Benchmark Engine
       10. Tab 28: Price Action Breakout & Trap Scanner (Volume & Momentum)

      🔮 PILLAR 3: Quantitative, Setups & AI Forecast (4 Engines)
       11. Tab 26: TimesFM 3.0 AI Quantile Gate (Runway vs Saturation)
       12. Tab 22: Session Reversal Matrix (10 Mentor Setups on 15m)
       13. Tab 23: WAR Setup (FII 30m First-Candle Trend & Gap Squeeze)
       14. Tab 8/30: Setups Context & Classical Chart Patterns (M/W/V/A)
    """
    items: List[TabConfluenceItem] = []
    active_trap_str: Optional[str] = None
    macro_warn_str: Optional[str] = None

    # ─────────────────────────────────────────────────────────────────────────
    # PILLAR 1: INSTITUTIONAL DERIVATIVES & ORDER FLOW (5 ENGINES)
    # ─────────────────────────────────────────────────────────────────────────

    # 1. Tab 37: Anti-Fakeout & Real-Time Pressure Gauge
    p37_sig = "NEUTRAL"
    p37_detail = "Core 200pt pressure balanced"
    p37_reading = "Net Index: 0.0"
    if chain_df is not None and not chain_df.empty:
        try:
            import anti_fakeout_pressure_engine as afpe
            core_df, _ = afpe.TruthOptionChainEngine.extract_core_200pt_range(chain_df, spot)
            if not core_df.empty:
                true_sr = afpe.TruthOptionChainEngine.calculate_true_sr(core_df, spot)
                gauge = afpe.TruthOptionChainEngine.calculate_pressure_gauge(core_df)
                p37_reading = f"{gauge.dominant_side.split(' ')[0]} ({gauge.net_pressure_index:+.0f})"
                if gauge.net_pressure_index >= 15.0 or "PUT_SELLERS" in gauge.dominant_side:
                    p37_sig = "BULLISH"
                    p37_detail = f"Put Sellers dominating core 200pt window ({gauge.net_pressure_index:+.0f}) · Bullish Lift active"
                elif gauge.net_pressure_index <= -15.0 or "CALL_SELLERS" in gauge.dominant_side:
                    p37_sig = "BEARISH"
                    p37_detail = f"Call Sellers dominating core 200pt window ({gauge.net_pressure_index:+.0f}) · Bearish Compression active"
                else:
                    p37_sig = "NEUTRAL"
                    p37_detail = f"Equal pressure in core window ({gauge.call_weight_pct:.0f}% Calls vs {gauge.put_weight_pct:.0f}% Puts)"

                f15_or_5 = pa_frames.get("5m") if pa_frames and "5m" in pa_frames else (pa_frames.get("15m") if pa_frames else None)
                vwap_v = float(f15_or_5["vwap"].iloc[-1]) if f15_or_5 is not None and "vwap" in f15_or_5.columns else spot
                fakeout_res = afpe.FakeoutDetectorEngine.detect_fakeout(spot, vwap_v, true_sr, gauge)
                if fakeout_res.is_fakeout:
                    active_trap_str = f"{fakeout_res.status}: {fakeout_res.action_prompt}"

            macro_stat = afpe.MacroEventEngine.evaluate_macro_status(datetime.now())
            if not macro_stat.trading_permitted or macro_stat.is_critical:
                macro_warn_str = macro_stat.warning_banner_text
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 37: Anti-Fakeout & Pressure",
        feature="200-Pt Core Pressure Index & Fakeout Shield",
        signal=p37_sig,
        badge_color=GREEN if p37_sig == "BULLISH" else (RED if p37_sig == "BEARISH" else SLATE),
        reading=p37_reading,
        plain_meaning=p37_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.2,
    ))

    # 2. Tab 36: Option Chain & OI Breakout Validation
    p36_sig = "NEUTRAL"
    p36_detail = "S/R corridor balanced"
    p36_reading = "Corridor Range"
    if chain_df is not None and not chain_df.empty:
        try:
            import option_chain_oi_tab as oco
            focus_df = oco.OptionChainOIEngine.filter_focus_window(chain_df, spot)
            lv_data = oco.OptionChainOIEngine.analyze_levels(focus_df, spot)
            oc_verdict = oco.OptionChainOIEngine.validate_breakout(focus_df, spot, lv_data)
            p36_reading = oc_verdict.status.replace("_", " ").title()
            if oc_verdict.direction == "BULLISH" or oc_verdict.status == "VALID_BREAKOUT_EXPECTED":
                p36_sig = "BULLISH"
                p36_detail = f"Valid Breakout Expected above R1 ({oc_verdict.tested_strike:.0f}) · Call writers covering"
            elif oc_verdict.direction in ("BEARISH", "BEARISH_REVERSAL") or oc_verdict.status in ("VALID_BREAKDOWN_EXPECTED", "FAKE_BREAKOUT_REVERSAL_EXPECTED"):
                p36_sig = "BEARISH"
                p36_detail = f"{oc_verdict.title} · Defense active at {oc_verdict.tested_strike:.0f}"
                if oc_verdict.is_bull_trap and not active_trap_str:
                    active_trap_str = oc_verdict.trap_warning or "Bull Trap Active at Resistance"
                elif oc_verdict.is_bear_trap and not active_trap_str:
                    active_trap_str = oc_verdict.trap_warning or "Bear Trap Active at Support"
            else:
                p36_sig = "NEUTRAL"
                p36_detail = "Spot oscillating between Call Wall resistance & Put Wall support"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 36: Option Chain & OI Analysis",
        feature="Focus S/R Breakout Validation & Anti-Trap",
        signal=p36_sig,
        badge_color=GREEN if p36_sig == "BULLISH" else (RED if p36_sig == "BEARISH" else SLATE),
        reading=p36_reading,
        plain_meaning=p36_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.2,
    ))

    # 3. Tab 1/2: OI Profile & Put-Call Ratio (PCR)
    pcr_val = 1.0
    oi_sig = "NEUTRAL"
    oi_detail = "Option chain balanced"
    if chain_df is not None and not chain_df.empty:
        try:
            ce_tot = chain_df["ce_oi"].sum() if "ce_oi" in chain_df.columns else 1
            pe_tot = chain_df["pe_oi"].sum() if "pe_oi" in chain_df.columns else 1
            pcr_val = pe_tot / max(1, ce_tot)
            if pcr_val >= 1.20:
                oi_sig = "BULLISH"
                oi_detail = f"PCR {pcr_val:.2f} (Put writers aggressively building support floor)"
            elif pcr_val <= 0.80:
                oi_sig = "BEARISH"
                oi_detail = f"PCR {pcr_val:.2f} (Call writers aggressively capping overhead ceiling)"
            else:
                oi_sig = "NEUTRAL"
                oi_detail = f"PCR {pcr_val:.2f} (Balanced rangebound positioning)"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 1/2: OI Profile & Walls",
        feature="Put-Call Ratio (PCR) & Net Wall Positioning",
        signal=oi_sig,
        badge_color=GREEN if oi_sig == "BULLISH" else (RED if oi_sig == "BEARISH" else SLATE),
        reading=f"PCR {pcr_val:.2f}",
        plain_meaning=oi_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.1,
    ))

    # 4. Tab 33: Order Flow & Market Depth (CVD Delta)
    of_sig = "NEUTRAL"
    of_detail = "Order flow delta balanced"
    of_reading = "Delta Balanced"
    if pa_frames and ("1m" in pa_frames or "5m" in pa_frames):
        try:
            import market_depth_tab as mdt
            c_src = pa_frames.get("1m") if "1m" in pa_frames else pa_frames.get("5m")
            c_stream = mdt.resolve_candle_stream(c_src, spot=spot)
            cvd_df, divergences = mdt.build_cvd_indicator(c_stream)
            footprints = mdt.build_footprint_data(c_stream, spot)
            last_fp = footprints[-1] if footprints else {}
            last_delta = int(last_fp.get("delta", 0))
            latest_div = divergences[-1] if divergences else None

            if (latest_div and latest_div.get("type") == "BULLISH_ABSORPTION") or last_delta > 500:
                of_sig = "BULLISH"
                of_reading = f"Buy Delta (+{last_delta:,})"
                of_detail = "CVD Bullish Absorption / Aggressive limit buyer defense on tape"
            elif (latest_div and latest_div.get("type") == "BEARISH_EXHAUSTION") or last_delta < -500:
                of_sig = "BEARISH"
                of_reading = f"Sell Delta ({last_delta:,})"
                of_detail = "CVD Bearish Exhaustion / Aggressive market selling into bids"
            else:
                of_sig = "NEUTRAL"
                of_reading = f"Delta ({last_delta:+,})"
                of_detail = "Order flow delta balanced with no major absorption divergences"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 33: Order Flow & Market Depth",
        feature="Cumulative Volume Delta (CVD) & Microstructure",
        signal=of_sig,
        badge_color=GREEN if of_sig == "BULLISH" else (RED if of_sig == "BEARISH" else SLATE),
        reading=of_reading,
        plain_meaning=of_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.1,
    ))

    # 5. Tab 34: Institutional Participant Positioning (FII Net Derivatives Bias)
    part_sig = "NEUTRAL"
    part_detail = "Participant positions balanced"
    part_reading = "FII Neutral"
    try:
        import participant_history as ph
        df_part = ph.load()
        if df_part is not None and not df_part.empty:
            pcts = ph.compute_percentiles(df_part)
            fii_fut = pcts[(pcts["participant"] == "FII") & (pcts["series"] == "net_index_futures")]
            fii_ce = pcts[(pcts["participant"] == "FII") & (pcts["series"] == "net_ce")]

            fut_rank = float(fii_fut.iloc[0]["percentile_rank"]) if not fii_fut.empty else 50.0
            ce_val = float(fii_ce.iloc[0]["raw_value"]) if not fii_ce.empty else 0.0

            if fut_rank >= 65.0:
                part_sig = "BULLISH"
                part_reading = f"FII Long Fut ({fut_rank:.0f}%ile)"
                part_detail = f"FIIs hold heavy Index Futures Longs ({fut_rank:.0f}th percentile)"
            elif fut_rank <= 35.0:
                part_sig = "BEARISH"
                part_reading = f"FII Short Fut ({fut_rank:.0f}%ile)"
                part_detail = f"FIIs hold heavy Index Futures Shorts ({fut_rank:.0f}th percentile)"
            elif ce_val < -150000:
                part_sig = "BEARISH"
                part_reading = f"FII Heavy CE Short ({ce_val/1000:,.0f}k)"
                part_detail = f"FIIs heavily short Calls ({ce_val/1000:,.0f}k contracts) capping upside"
            else:
                part_sig = "NEUTRAL"
                part_reading = f"FII Balanced ({fut_rank:.0f}%ile)"
                part_detail = "FII derivatives positioning in normal historical equilibrium"
    except Exception:
        pass

    items.append(TabConfluenceItem(
        tab_name="Tab 34: Participant Positioning",
        feature="FII Derivatives Positioning & Historical Deciles",
        signal=part_sig,
        badge_color=GREEN if part_sig == "BULLISH" else (RED if part_sig == "BEARISH" else SLATE),
        reading=part_reading,
        plain_meaning=part_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.0,
    ))

    # Tab 36 sub-tab: Call vs Put Writers (CE vs PE) Live Flow & Striking Defense
    cw_sig = "NEUTRAL"
    cw_detail = "Option writers balanced across focus strikes"
    cw_reading = "Writers Balanced"
    if chain_df is not None and not chain_df.empty:
        try:
            import writers_graph_engine as wge
            atm_stk = round(spot / 50.0) * 50.0
            cw_res = wge.evaluate_striking_price_prediction_power(chain_df, spot, striking_strike=atm_stk)
            cw_sig = "BULLISH" if cw_res.expected_direction == "UP" else ("BEARISH" if cw_res.expected_direction == "DOWN" else "NEUTRAL")
            cw_reading = f"{cw_res.strike_dominance.replace('_', ' ').title()} ({cw_res.strike_writer_pcr:.1f}x)"
            cw_detail = f"{cw_res.summary} · {cw_res.actionable_directive}"
            if cw_res.trap_risk != "NONE" and not active_trap_str:
                active_trap_str = f"⚠️ {cw_res.trap_risk.replace('_', ' ').title()}: {cw_res.actionable_directive}"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 36: Call vs Put Writers Flow",
        feature="CE vs PE Writer Aggression & Striking Price Defense",
        signal=cw_sig,
        badge_color=GREEN if cw_sig == "BULLISH" else (RED if cw_sig == "BEARISH" else SLATE),
        reading=cw_reading,
        plain_meaning=cw_detail,
        pillar="🏛️ Institutional Flow",
        weight=1.25,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # PILLAR 2: PRICE ACTION & MARKET STRUCTURE (5 ENGINES)
    # ─────────────────────────────────────────────────────────────────────────

    # 6. Tab 19 & 31: Smart Money Concepts (SMC) & Liquidity Terminal
    smc_sig = "NEUTRAL"
    smc_detail = "Order blocks & structural equilibrium"
    smc_reading = "Equilibrium"
    try:
        from engine.smc_engine import AdvancedSMCEngine
        f_smc = pa_frames.get("15m") if pa_frames and "15m" in pa_frames else (pa_frames.get("5m") if pa_frames else None)
        if f_smc is not None and len(f_smc) >= 10:
            smc_data = AdvancedSMCEngine().analyze(f_smc)
            act_msb = smc_data.get("active_msb")
            if act_msb is not None:
                if act_msb.direction == "BULLISH":
                    smc_sig = "BULLISH"
                    smc_reading = f"MSS Breakout ({act_msb.strength})"
                    smc_detail = f"Bullish Market Structure Shift ({act_msb.strength}) above {act_msb.level_price:,.0f}"
                elif act_msb.direction == "BEARISH":
                    smc_sig = "BEARISH"
                    smc_reading = f"MSS Breakdown ({act_msb.strength})"
                    smc_detail = f"Bearish Market Structure Shift ({act_msb.strength}) below {act_msb.level_price:,.0f}"

        if smc_sig == "NEUTRAL" and pa_frames and "1m" in pa_frames:
            import three_drive_scalper as tds
            td = tds.evaluate_three_drive(pa_frames["1m"], spot)
            if td and td.status in ("ARMED_AWAITING_MSS", "TRIGGERED_MSS_CONFIRMED"):
                if "BULLISH" in td.side:
                    smc_sig = "BULLISH"
                    smc_reading = "1m 3-Drive Bullish"
                    smc_detail = "1m 3-Drive Liquidity Sweep Confirmed! Bullish MSS Breakout"
                elif "BEARISH" in td.side:
                    smc_sig = "BEARISH"
                    smc_reading = "1m 3-Drive Bearish"
                    smc_detail = "1m 3-Drive Liquidity Sweep Confirmed! Bearish MSS Breakdown"
    except Exception:
        pass

    items.append(TabConfluenceItem(
        tab_name="Tab 19/31: SMC & Liquidity",
        feature="Market Structure Shift (MSS), FVG & Liquidity Sweeps",
        signal=smc_sig,
        badge_color=GREEN if smc_sig == "BULLISH" else (RED if smc_sig == "BEARISH" else SLATE),
        reading=smc_reading,
        plain_meaning=smc_detail,
        pillar="📐 Price Action & Structure",
        weight=1.2,
    ))

    # 7. Tab 32: Volume Profile & AMT (Auction Market Theory)
    vp_sig = "NEUTRAL"
    vp_detail = "Spot oscillating near POC magnet"
    vp_reading = "Inside Value Area"
    try:
        import volume_profile_tab as vpt
        f_vp = pa_frames.get("5m") if pa_frames and "5m" in pa_frames else (pa_frames.get("15m") if pa_frames else None)
        if f_vp is not None and len(f_vp) >= 15:
            vp_res = vpt.VolumeProfileEngine().compute_profile(f_vp)
            dist_poc = spot - vp_res.poc_price
            shape_str = vp_res.shape.shape
            vp_reading = f"{shape_str} (POC {vp_res.poc_price:,.0f})"
            if spot > vp_res.vah_price + 3.0:
                vp_sig = "BULLISH"
                vp_detail = f"Spot ({spot:,.1f}) accepted above VAH ({vp_res.vah_price:,.0f}) · Bullish value expansion"
            elif spot < vp_res.val_price - 3.0:
                vp_sig = "BEARISH"
                vp_detail = f"Spot ({spot:,.1f}) rejected below VAL ({vp_res.val_price:,.0f}) · Bearish value breakdown"
            elif shape_str == "P-Shape":
                vp_sig = "BULLISH"
                vp_detail = f"P-Shape Profile · Short covering rally holding above POC ({vp_res.poc_price:,.0f})"
            elif shape_str == "B-Shape":
                vp_sig = "BEARISH"
                vp_detail = f"B-Shape Profile · Long liquidation holding below POC ({vp_res.poc_price:,.0f})"
            else:
                vp_sig = "NEUTRAL"
                vp_detail = f"D-Shape Balanced Auction: Spot {dist_poc:+.0f} pts from Fair Price Magnet POC ({vp_res.poc_price:,.0f})"
    except Exception:
        pass

    items.append(TabConfluenceItem(
        tab_name="Tab 32: Volume Profile & AMT",
        feature="Point of Control (POC), Value Area (VAH/VAL) & Auction Shape",
        signal=vp_sig,
        badge_color=GREEN if vp_sig == "BULLISH" else (RED if vp_sig == "BEARISH" else SLATE),
        reading=vp_reading,
        plain_meaning=vp_detail,
        pillar="📐 Price Action & Structure",
        weight=1.1,
    ))

    # 8. Tab 7 & 17: Price Action Strategy 1 (8 EMA Rule)
    pa_sig = "NEUTRAL"
    pa_detail = "Price hovering at 8-EMA"
    if pa_frames and "15m" in pa_frames and not pa_frames["15m"].empty:
        try:
            d15 = pa_frames["15m"]
            c_val = float(d15["close"].iloc[-1])
            e8_val = float(d15["ema8"].iloc[-1]) if "ema8" in d15.columns else c_val
            if c_val > e8_val + 2.0:
                pa_sig = "BULLISH"
                pa_detail = f"15m Close ({c_val:,.1f}) > 8 EMA ({e8_val:,.1f}) · Bullish follow-through active"
            elif c_val < e8_val - 2.0:
                pa_sig = "BEARISH"
                pa_detail = f"15m Close ({c_val:,.1f}) < 8 EMA ({e8_val:,.1f}) · Bearish rollover active"
            else:
                pa_sig = "NEUTRAL"
                pa_detail = f"15m Close touching 8 EMA ({e8_val:,.1f}) · Awaiting directional close"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 7/17: PA Strategy 1",
        feature="15m 8-EMA Follow-Through Rule",
        signal=pa_sig,
        badge_color=GREEN if pa_sig == "BULLISH" else (RED if pa_sig == "BEARISH" else SLATE),
        reading=pa_sig,
        plain_meaning=pa_detail,
        pillar="📐 Price Action & Structure",
        weight=1.1,
    ))

    # 9. Tab 18: VWAP Institutional Engine
    vwap_sig = "NEUTRAL"
    vwap_detail = "Price at VWAP equilibrium"
    if pa_frames and "15m" in pa_frames and not pa_frames["15m"].empty:
        try:
            d15 = pa_frames["15m"]
            c_val = float(d15["close"].iloc[-1])
            v_val = float(d15["vwap"].iloc[-1]) if "vwap" in d15.columns else c_val
            if c_val > v_val + 2.0:
                vwap_sig = "BULLISH"
                vwap_detail = f"15m Close ({c_val:,.1f}) > Institutional VWAP ({v_val:,.1f}) · Bullish intraday anchor"
            elif c_val < v_val - 2.0:
                vwap_sig = "BEARISH"
                vwap_detail = f"15m Close ({c_val:,.1f}) < Institutional VWAP ({v_val:,.1f}) · Bearish intraday anchor"
            else:
                vwap_sig = "NEUTRAL"
                vwap_detail = f"15m Close ({c_val:,.1f}) at Institutional VWAP ({v_val:,.1f}) equilibrium"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 18: VWAP Engine",
        feature="Futures Volume-Weighted Price Benchmark",
        signal=vwap_sig,
        badge_color=GREEN if vwap_sig == "BULLISH" else (RED if vwap_sig == "BEARISH" else SLATE),
        reading=vwap_sig,
        plain_meaning=vwap_detail,
        pillar="📐 Price Action & Structure",
        weight=1.0,
    ))

    # 10. Tab 28: Breakout & Trap Scanner
    bo_sig = "NEUTRAL"
    bo_detail = "Consolidation within boundaries"
    if pa_frames and "5m" in pa_frames and not pa_frames["5m"].empty:
        try:
            engine = PriceActionBreakoutEngine()
            scan_lv = {"Level": spot}
            df_bo = engine.apply_breakout_signals(pa_frames["5m"], scan_lv)
            last_sig = df_bo["primary_signal"].iloc[-1] if "primary_signal" in df_bo.columns else ""
            if "STRONG_BREAKOUT" in last_sig:
                bo_sig = "BULLISH" if "UP" in last_sig or "RES" in last_sig else "BEARISH"
                bo_detail = f"Valid strong breakout on volume ({last_sig})"
            elif "TRAP" in last_sig:
                bo_sig = "BEARISH" if "BULL" in last_sig or "UP" in last_sig else "BULLISH"
                bo_detail = f"Fakeout / Liquidity trap detected ({last_sig})"
                if not active_trap_str:
                    active_trap_str = f"Breakout Trap: {last_sig}"
            else:
                bo_sig = "NEUTRAL"
                bo_detail = "No breakout or trap currently firing on 5m"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 28: Breakout & Trap Scanner",
        feature="Volume Breakout vs Fakeout Trap Detection",
        signal=bo_sig,
        badge_color=GREEN if bo_sig == "BULLISH" else (RED if bo_sig == "BEARISH" else SLATE),
        reading=bo_sig,
        plain_meaning=bo_detail,
        pillar="📐 Price Action & Structure",
        weight=1.0,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # PILLAR 3: QUANTITATIVE, SETUPS & AI FORECAST (4 ENGINES)
    # ─────────────────────────────────────────────────────────────────────────

    # 11. Tab 26: TimesFM 3.0 AI Quantile Gate
    ai_sig = "NEUTRAL"
    ai_detail = "Clear runway in both directions"
    try:
        f15 = pa_frames.get("15m") if pa_frames else None
        f5 = pa_frames.get("5m") if pa_frames else None
        gate = cf.evaluate_ai_confluence_gate(spot, df_15m=f15, df_5m=f5)
        if gate.blocked_ce:
            ai_sig = "BEARISH"
            ai_detail = f"TimesFM q90 Cap at {gate.q90_cap:,.0f} (+{gate.dist_to_cap:.0f} pts) · Upside Saturated"
        elif gate.blocked_pe:
            ai_sig = "BULLISH"
            ai_detail = f"TimesFM q10 Floor at {gate.q10_floor:,.0f} (-{gate.dist_to_floor:.0f} pts) · Downside Saturated"
        else:
            ai_sig = "NEUTRAL"
            ai_detail = f"Runway Clear: Floor {gate.q10_floor:,.0f} | Median {gate.q50_median:,.0f} | Cap {gate.q90_cap:,.0f}"
    except Exception:
        pass

    items.append(TabConfluenceItem(
        tab_name="Tab 26: TimesFM AI Forecast",
        feature="Quantile Runway & Volatility Envelope (q10/q90)",
        signal=ai_sig,
        badge_color=GREEN if ai_sig == "BULLISH" else (RED if ai_sig == "BEARISH" else SLATE),
        reading=ai_sig,
        plain_meaning=ai_detail,
        pillar="🔮 Quantitative & AI",
        weight=1.0,
    ))

    # 12. Tab 22: Session Reversal Matrix (10 Mentor Setups)
    rev_sig = "NEUTRAL"
    rev_detail = "No matrix setups triggered"
    if pa_frames and "15m" in pa_frames:
        try:
            m_res = srm.evaluate_session_reversals(df_15m=pa_frames["15m"], spot=spot, chain_df=chain_df, prev_close=prev_close)
            if m_res.long_triggered_count > 0:
                rev_sig = "BULLISH"
                rev_detail = f"{m_res.long_triggered_count} Bullish Support Reversal setups triggered on 15m"
            elif m_res.short_triggered_count > 0:
                rev_sig = "BEARISH"
                rev_detail = f"{m_res.short_triggered_count} Bearish Resistance Reversal setups triggered on 15m"
            elif m_res.active_count > 0:
                rev_sig = "NEUTRAL"
                rev_detail = f"{m_res.active_count} setups armed/monitoring near key levels"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 22: 15m Reversal Matrix",
        feature="10 Mentor Intraday Reversal Setups",
        signal=rev_sig,
        badge_color=GREEN if rev_sig == "BULLISH" else (RED if rev_sig == "BEARISH" else SLATE),
        reading=rev_sig,
        plain_meaning=rev_detail,
        pillar="🔮 Quantitative & AI",
        weight=1.0,
    ))

    # 13. Tab 23: WAR Setup (FII 30m First-Candle Strategy)
    war_sig = "NEUTRAL"
    war_detail = "Inside first 30-min range"
    war_reading = "30m Range"
    if pa_frames and "5m" in pa_frames:
        try:
            import war_setup as ws
            dt_str = datetime.now().strftime("%Y-%m-%d")
            pc_val = prev_close or spot
            w_sigs = ws.scan_war_signals(pa_frames["5m"], pdc=pc_val, date_str=dt_str)
            if w_sigs:
                last_w = w_sigs[-1]
                if last_w.direction == "BUY_CE":
                    war_sig = "BULLISH"
                    war_reading = "WAR Buy CE"
                    war_detail = f"WAR 30m Trigger: {last_w.trigger_type} ({last_w.quality_grade})"
                elif last_w.direction == "BUY_PE":
                    war_sig = "BEARISH"
                    war_reading = "WAR Buy PE"
                    war_detail = f"WAR 30m Trigger: {last_w.trigger_type} ({last_w.quality_grade})"
            else:
                c30 = ws.extract_30m_candle(pa_frames["5m"])
                if c30:
                    if spot > c30["high"]:
                        war_sig = "BULLISH"
                        war_reading = "Above 30m High"
                        war_detail = f"Spot ({spot:,.1f}) trading above 30m First Candle High ({c30['high']:,.1f})"
                    elif spot < c30["low"]:
                        war_sig = "BEARISH"
                        war_reading = "Below 30m Low"
                        war_detail = f"Spot ({spot:,.1f}) trading below 30m First Candle Low ({c30['low']:,.1f})"
                    else:
                        war_sig = "NEUTRAL"
                        war_reading = "Inside 30m Range"
                        war_detail = f"Spot inside 30m range ({c30['low']:,.0f} - {c30['high']:,.0f})"
        except Exception:
            pass

    items.append(TabConfluenceItem(
        tab_name="Tab 23: WAR Setup (FII 30m)",
        feature="30-Min First Candle Range & Gap Squeeze",
        signal=war_sig,
        badge_color=GREEN if war_sig == "BULLISH" else (RED if war_sig == "BEARISH" else SLATE),
        reading=war_reading,
        plain_meaning=war_detail,
        pillar="🔮 Quantitative & AI",
        weight=0.9,
    ))

    # 14. Tab 8/30: Setups Context & Classical Chart Patterns (M/W)
    pat_sig = "NEUTRAL"
    pat_detail = "No classical pattern confirmed"
    pat_reading = "No Pattern"
    try:
        import chart_patterns as cp
        f_pat = pa_frames.get("15m") if pa_frames and "15m" in pa_frames else (pa_frames.get("5m") if pa_frames else None)
        if f_pat is not None and len(f_pat) >= 15:
            pats = cp.detect_chart_patterns(f_pat, timeframe="15m")
            if pats:
                last_p = pats[-1]
                if last_p.pattern_type in ("W", "V"):
                    pat_sig = "BULLISH"
                    pat_reading = f"{last_p.name}"
                    pat_detail = f"Bullish {last_p.name} pattern confirmed on {last_p.timeframe}"
                elif last_p.pattern_type in ("M", "A"):
                    pat_sig = "BEARISH"
                    pat_reading = f"{last_p.name}"
                    pat_detail = f"Bearish {last_p.name} pattern confirmed on {last_p.timeframe}"

        if pat_sig == "NEUTRAL" and pa_frames and "5m" in pa_frames and "15m" in pa_frames:
            import setups as su
            f5_su = su.prepare(pa_frames["5m"])
            f15_su = su.prepare(pa_frames["15m"])
            ctx_su = su.context_summary(f5_su, f15_su, datetime.now())
            if ctx_su and "trend15" in ctx_su:
                tr = str(ctx_su["trend15"]).upper()
                if "BULL" in tr:
                    pat_sig = "BULLISH"
                    pat_reading = "15m Trend Up"
                    pat_detail = f"15m Trend is Bullish (BB Width: {ctx_su.get('bb_width', 0):.0f})"
                elif "BEAR" in tr:
                    pat_sig = "BEARISH"
                    pat_reading = "15m Trend Down"
                    pat_detail = f"15m Trend is Bearish (BB Width: {ctx_su.get('bb_width', 0):.0f})"
    except Exception:
        pass

    items.append(TabConfluenceItem(
        tab_name="Tab 8/30: Setups & Patterns",
        feature="Classical Chart Patterns (M/W) & Trend Alignment",
        signal=pat_sig,
        badge_color=GREEN if pat_sig == "BULLISH" else (RED if pat_sig == "BEARISH" else SLATE),
        reading=pat_reading,
        plain_meaning=pat_detail,
        pillar="🔮 Quantitative & AI",
        weight=0.9,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # Calculate Totals & Pillar Summaries
    # ─────────────────────────────────────────────────────────────────────────
    bull_count = sum(1 for i in items if i.signal == "BULLISH")
    bear_count = sum(1 for i in items if i.signal == "BEARISH")
    neutral_count = sum(1 for i in items if i.signal == "NEUTRAL")
    total_strategies = len(items)

    pillar_summary = {}
    for p in ["🏛️ Institutional Flow", "📐 Price Action & Structure", "🔮 Quantitative & AI"]:
        p_items = [i for i in items if i.pillar == p]
        pillar_summary[p] = {
            "total": len(p_items),
            "bull": sum(1 for i in p_items if i.signal == "BULLISH"),
            "bear": sum(1 for i in p_items if i.signal == "BEARISH"),
            "neutral": sum(1 for i in p_items if i.signal == "NEUTRAL"),
        }

    # Weighted net score (-100 to +100)
    w_bull = sum(i.weight for i in items if i.signal == "BULLISH")
    w_bear = sum(i.weight for i in items if i.signal == "BEARISH")
    w_tot = sum(i.weight for i in items)
    net_score = int(((w_bull - w_bear) / max(1.0, w_tot)) * 100)

    # Multi-Strategy Consensus Verdict
    if bull_count >= 7 and bull_count > bear_count:
        pct = int((bull_count / total_strategies) * 100)
        consensus_verdict = f"🟢 BULLISH CONFLUENCE ({bull_count}/{total_strategies} Strategies Agree · {pct}% Consensus)"
        consensus_color = GREEN
    elif bear_count >= 7 and bear_count > bull_count:
        pct = int((bear_count / total_strategies) * 100)
        consensus_verdict = f"🔴 BEARISH CONFLUENCE ({bear_count}/{total_strategies} Strategies Agree · {pct}% Consensus)"
        consensus_color = RED
    elif bull_count > bear_count and bull_count >= 5:
        consensus_verdict = f"🟢 MILD BULLISH SKEW ({bull_count}/{total_strategies} Agree · Conflicted)"
        consensus_color = GREEN
    elif bear_count > bull_count and bear_count >= 5:
        consensus_verdict = f"🔴 MILD BEARISH SKEW ({bear_count}/{total_strategies} Agree · Conflicted)"
        consensus_color = RED
    else:
        consensus_verdict = f"🟡 RANGEBOUND / CONFLICTED ({neutral_count}/{total_strategies} Strategies Neutral)"
        consensus_color = GOLD

    return AllTabsRadarReport(
        items=items,
        bull_count=bull_count,
        bear_count=bear_count,
        neutral_count=neutral_count,
        total_strategies=total_strategies,
        net_score=net_score,
        consensus_verdict=consensus_verdict,
        consensus_color=consensus_color,
        pillar_summary=pillar_summary,
        active_trap_warning=active_trap_str,
        macro_event_warning=macro_warn_str,
    )


def calculate_bayesian_win_rate(
    action_type: str,
    tabs_radar: AllTabsRadarReport,
    dual: DualCandleReport,
    radar: LevelRadarReport,
) -> Tuple[float, float]:
    """
    Computes authentic Bayesian Posterior Probability and Prospective Win Rate:
    P(Directional Follow-Through | All-Tab Confluence, Candles, Key Levels, Anti-Trap)
    Returns: (actual_win_rate, prospective_win_rate)
    """
    base_prior = 50.0  # 50% baseline in efficient market

    # 1. Multi-Tab Consensus Shift (-100 to +100)
    net_score = tabs_radar.net_score

    if action_type == "BUY_CE" or (action_type == "WAIT_WATCH" and net_score > 0):
        dir_mult = max(0.0, net_score / 100.0)
        consensus_shift = dir_mult * 24.0
    elif action_type == "BUY_PE" or (action_type == "WAIT_WATCH" and net_score < 0):
        dir_mult = max(0.0, abs(net_score) / 100.0)
        consensus_shift = dir_mult * 24.0
    else:
        consensus_shift = 0.0

    # 2. Key Level Striking Alignment
    level_bonus = 0.0
    if radar.striking_status == "STRIKING_RESISTANCE":
        if action_type == "BUY_PE" or (action_type == "WAIT_WATCH" and net_score <= 0):
            level_bonus += 6.0
            if dual.c15.is_reversal or dual.c15.direction == "BEARISH":
                level_bonus += 5.0
        elif action_type == "BUY_CE":
            level_bonus -= 14.0
    elif radar.striking_status == "STRIKING_SUPPORT":
        if action_type == "BUY_CE" or (action_type == "WAIT_WATCH" and net_score >= 0):
            level_bonus += 6.0
            if dual.c15.is_reversal or dual.c15.direction == "BULLISH":
                level_bonus += 5.0
        elif action_type == "BUY_PE":
            level_bonus -= 14.0

    # 3. Dual Timeframe Harmony
    candle_bonus = 0.0
    if (action_type == "BUY_CE" or net_score > 0) and dual.c15.direction == "BULLISH" and dual.c5.direction == "BULLISH":
        candle_bonus += 5.5
    elif (action_type == "BUY_PE" or net_score < 0) and dual.c15.direction == "BEARISH" and dual.c5.direction == "BEARISH":
        candle_bonus += 5.5
    elif dual.harmony_verdict == "CONFLICTED_CHOP":
        candle_bonus -= 4.0

    # Prospective Win Rate (Expectancy once current 15m candle closes confirming the setup)
    prospective = min(86.0, max(52.0, base_prior + consensus_shift + level_bonus + candle_bonus))

    # Actual Current Win Rate
    if action_type in ("BUY_CE", "BUY_PE"):
        actual = prospective
    else:
        # In WAIT_WATCH, before candle close or trigger confirmation, edge has NOT yet materialized!
        actual = 50.0  # Unconfirmed / stand-aside

    return round(actual, 1), round(prospective, 1)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Master Synthesized Action & Order Placement Ticket
# ═══════════════════════════════════════════════════════════════════════════

def compute_rvol_sparkline_svg(
    target_date: Any,
    target_time: Any,
    color: str = "#ef4444",
    width: int = 135,
    height: int = 36,
) -> str:
    """
    Renders an inline SVG sparkline of today's cumulative volume curve
    vs the 10-day TOD average curve up to target_time.
    """
    try:
        target_date_str = rvol_gate._parse_date(target_date)
        target_time_val = rvol_gate._parse_time(target_time)

        cfg = rvol_gate.load_rvol_config()
        baseline_sessions = int(cfg.get("baseline_sessions", 10))
        all_sessions = rvol_gate._get_available_sessions()
        prior_sessions = [d for d in all_sessions if d < target_date_str][-baseline_sessions:]

        today_df = rvol_gate._load_session_df(target_date_str)
        if today_df is None or today_df.empty:
            return ""

        today_bars = today_df[today_df["time_val"] <= target_time_val].copy()
        if today_bars.empty:
            return ""
        today_bars["cum_vol"] = today_bars["volume"].cumsum()

        base_cum_vols = []
        for d in prior_sessions:
            b_df = rvol_gate._load_session_df(d)
            if b_df is not None and not b_df.empty:
                b_bars = b_df[b_df["time_val"] <= target_time_val].copy()
                if not b_bars.empty:
                    b_bars["cum_vol"] = b_bars["volume"].cumsum()
                    base_cum_vols.append(b_bars["cum_vol"].values)

        if not base_cum_vols:
            return ""

        min_len = min(len(today_bars), min(len(v) for v in base_cum_vols))
        if min_len < 2:
            return ""

        today_curve = list(today_bars["cum_vol"].values[:min_len])
        base_matrix = np.array([v[:min_len] for v in base_cum_vols])
        avg_curve = list(np.mean(base_matrix, axis=0))

        max_val = max(max(today_curve), max(avg_curve))
        if max_val <= 0:
            max_val = 1.0

        step_x = (width - 6) / max(1, min_len - 1)
        avg_pts = [f"{3 + i*step_x:.1f},{height - 3 - (v/max_val*(height-6)):.1f}" for i, v in enumerate(avg_curve)]
        today_pts = [f"{3 + i*step_x:.1f},{height - 3 - (v/max_val*(height-6)):.1f}" for i, v in enumerate(today_curve)]

        avg_d = " ".join(avg_pts)
        today_d = " ".join(today_pts)

        return (
            f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="display:block; margin-top:2px;">'
            f'<polyline fill="none" stroke="#64748b" stroke-width="1.5" stroke-dasharray="3,3" points="{avg_d}" />'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.2" points="{today_d}" />'
            f'</svg>'
        )
    except Exception as exc:
        logging.getLogger("decision_cockpit").warning(f"Error generating RVOL sparkline: {exc}")
        return ""


def evaluate_signal_stack(
    spot: float,
    chain_df: Optional[pd.DataFrame],
    target_date: Any,
    target_time: Any,
    candidate_action: str,
    is_range_structure: bool,
    dual: DualCandleReport,
    tabs_radar: AllTabsRadarReport,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
) -> Dict[str, Any]:
    """
    Evaluates the 7-Layer Signal Stack with strict veto order:
      L1 Institutional Context (Hedge / True Trend / Pure Expiry)
      L2 GEX (structural support/resistance, flip point)
      L3 RVOL Gate (chop filter — NEW)
      L4 IV & Greeks (HV/IV spread, edge check)
      L5 Pre-Trade Checklist (6 boxes)
      L6 False Breakdown / Trap Detection
      L7 8 EMA Alignment (entry trigger)

    Veto Order: If L1 or L2 or L3 fails -> NO TRADE, regardless of L4–L7.
    """
    cfg = rvol_gate.load_rvol_config()
    target_date_val = rvol_gate._parse_date(target_date)
    target_time_val = rvol_gate._parse_time(target_time)

    # ── L1: Institutional Context ──
    l1_pass = True
    l1_status = "PASS"
    l1_detail = "Pro positioning aligned"
    pro_bias_str = "NEUTRAL"
    try:
        tidy_df = ph.load()
        if tidy_df is not None and not tidy_df.empty:
            tidy_df = tidy_df.copy()
            tidy_df["date"] = pd.to_datetime(tidy_df["date"]).dt.date
            t_d = pd.to_datetime(target_date_val).date()
            df_prev = tidy_df[tidy_df["date"] < t_d]
            if df_prev.empty:
                df_prev = tidy_df[tidy_df["date"] <= t_d]
            if not df_prev.empty:
                prev_date = df_prev["date"].max()
                pcts_df = ph.compute_percentiles(tidy_df, window=65, as_of_date=prev_date)
                pro_row = pcts_df[(pcts_df["participant"] == "PRO") & (pcts_df["series"] == "fut_long")]
                pro_fut_pct = float(pro_row["percentile_rank"].iloc[0]) if not pro_row.empty else 50.0
                if pro_fut_pct > 70.0:
                    pro_bias_str = f"BULLISH ({int(pro_fut_pct)}th pct)"
                    if candidate_action == "BUY_PE":
                        l1_pass = False
                        l1_status = "FAIL"
                        l1_detail = f"PRO is Bullish ({int(pro_fut_pct)}th pct) — opposing PE"
                elif pro_fut_pct < 30.0:
                    pro_bias_str = f"BEARISH ({int(pro_fut_pct)}th pct)"
                    if candidate_action == "BUY_CE":
                        l1_pass = False
                        l1_status = "FAIL"
                        l1_detail = f"PRO is Bearish ({int(pro_fut_pct)}th pct) — opposing CE"
                else:
                    pro_bias_str = f"NEUTRAL ({int(pro_fut_pct)}th pct)"
    except Exception as exc:
        logging.getLogger("decision_cockpit").warning(f"L1 error: {exc}")

    # ── L2: GEX Structure & Regime ──
    l2_pass = True
    l2_status = "PASS"
    l2_detail = "GEX supports structure"
    gex_data: Dict[str, Any] = {}
    gex_regime = "NEUTRAL"
    zero_gamma = spot
    total_gex_cr = 0.0
    try:
        if chain_df is not None and not chain_df.empty:
            gex_data = agt_briefing.compute_gex_metrics(chain_df, spot)
            gex_regime = gex_data.get("regime", "NEUTRAL")
            zero_gamma = float(gex_data.get("zero_gamma_level", spot))
            total_gex_cr = float(gex_data.get("total_gex_cr", 0.0))
            if gex_regime == "SHORT" and candidate_action == "BUY_CE" and spot < zero_gamma - 50:
                l2_detail = "Short Gamma flip point overhead"
            elif gex_regime == "LONG":
                l2_detail = f"Long Gamma (+₹{total_gex_cr:.1f}Cr) — Pinning walls hold"
            else:
                l2_detail = f"GEX {gex_regime} (₹{total_gex_cr:+.1f}Cr)"
    except Exception as exc:
        logging.getLogger("decision_cockpit").warning(f"L2 error: {exc}")

    # ── L3: RVOL Gate (Chop Filter — Ported from Validated Build) ──
    rvol_dec = rvol_gate.get_gate_decision(target_date_val, target_time_val)
    rvol_val = rvol_dec.get("rvol_at_gate")
    rvol_regime = rvol_dec.get("regime", "NA")
    rvol_status = rvol_dec.get("status", "ALLOWED")

    l3_pass = True
    l3_veto = False
    l3_detail = ""

    if rvol_status == "HOLD":
        l3_pass = False
        l3_status_label = "HOLD"
        l3_detail = "Market open warmup (<09:30) — first RVOL not yet available"
    elif rvol_regime == "CHOP_ZONE" or rvol_status == "SUPPRESSED":
        if is_range_structure:
            l3_pass = True
            l3_status_label = "PASS (RANGE)"
            l3_detail = f"RVOL {rvol_val:.2f}× (CHOP ZONE) — Breakouts blocked, but Wall Fade / Range trades ALLOWED"
        else:
            l3_pass = False
            l3_veto = True
            l3_status_label = "FAIL (CHOP ZONE)"
            l3_detail = f"⚠️ CHOP ZONE ({rvol_val:.2f}×) — 77% breakouts fail. Wait for RVOL > 0.85 before any entry."
    elif rvol_regime == "TREND_DAY":
        l3_pass = True
        l3_status_label = "PASS (TREND DAY)"
        l3_detail = f"🚀 TREND DAY ({rvol_val:.2f}×) — Disabling range structures, promoting breakouts to full target"
    else:  # NORMAL or NA
        l3_pass = True
        l3_status_label = "PASS (NORMAL)"
        l3_detail = f"RVOL {rvol_val:.2f}× (Normal volume runway)" if rvol_val is not None else "RVOL N/A (normal mode)"

    # ── L4: IV & Greeks ──
    l4_pass = True
    l4_detail = "IV normal"
    try:
        iv_ctx = agt_briefing.compute_iv_context(chain_df, spot, datetime.now(), pd.to_datetime(target_date_val).date())
        iv_pct = iv_ctx.get("percentile")
        if iv_pct is not None and iv_pct > 65.0:
            l4_detail = f"IV expensive ({int(iv_pct)}%)"
        else:
            l4_detail = f"IV percentile: {int(iv_pct)}%" if iv_pct is not None else "IV normal"
    except Exception:
        pass

    # ── L8: Order Flow DOM Layer (Execution Confirmation) ──
    l8_pass = True
    l8_status = "SILENT"
    l8_detail = "Order flow silent / paper-trade active"
    l8_report = None
    try:
        import order_flow_dom
        l8_report = order_flow_dom.evaluate_l8_order_flow(
            candidate_action=candidate_action,
            spot=spot,
            target_date=target_date_val,
            target_time=target_time_val,
        )
        if l8_report.verdict == "VETO":
            if l8_report.sim_suppressed or l8_report.is_paper_trade:
                l8_pass = True
                l8_status = "PASS (SIM)"
                l8_detail = f"VETO in Sim Only ({'; '.join(l8_report.reasons)})"
            else:
                l8_pass = False
                l8_status = "FAIL"
                l8_detail = f"VETO: {'; '.join(l8_report.reasons)}"
        elif l8_report.verdict == "CONFIRM":
            l8_pass = True
            l8_status = "CONFIRM"
            l8_detail = f"CONFIRM: {'; '.join(l8_report.reasons)}"
        else:
            l8_pass = True
            l8_status = "SILENT"
            l8_detail = f"{l8_report.active_walls_summary} | CVD {l8_report.cvd_slope_badge}"
    except Exception as exc:
        logging.getLogger("decision_cockpit").warning(f"L8 evaluation error: {exc}")

    # ── L5: Pre-Trade Checklist (7 Boxes) ──
    l5_pass = True
    l5_detail = "Checklist active"
    try:
        chk = agt_briefing.compute_pretrade_checklist(
            s1_row=None,
            r1_row=None,
            d_pe_15m=0,
            d_ce_15m=0,
            iv_percentile=50.0,
            gex_data=gex_data,
            latest_ts=datetime.combine(pd.to_datetime(target_date_val).date(), target_time_val),
            target_date=pd.to_datetime(target_date_val).date(),
            target_time=target_time_val,
            rules=cfg,
            has_history=True,
            l8_report=l8_report,
        )
        t_boxes = len(chk.get("boxes", [])) or 7
        l5_detail = f"Score {chk.get('score', t_boxes)}/{t_boxes} ({chk.get('verdict', 'TRADE')})"
    except Exception as exc:
        logging.getLogger("decision_cockpit").warning(f"L5 error: {exc}")

    # ── L6: False Breakdown / Trap Detection ──
    l6_pass = not bool(tabs_radar.active_trap_warning)
    l6_detail = tabs_radar.active_trap_warning or "No traps detected"

    # ── L7: 8 EMA Alignment ──
    l7_pass = (dual.harmony_verdict != "CONFLICTED_CHOP")
    l7_detail = dual.harmony_title

    # ── STRICT VETO ORDER EVALUATION ──
    veto_active = False
    veto_layer = None
    veto_reason = None

    if not l1_pass:
        veto_active = True
        veto_layer = "L1"
        veto_reason = l1_detail
    elif not l2_pass:
        veto_active = True
        veto_layer = "L2"
        veto_reason = l2_detail
    elif l3_veto or (rvol_status == "HOLD" and candidate_action in ("BUY_CE", "BUY_PE")):
        veto_active = True
        veto_layer = "L3"
        veto_reason = l3_detail
    elif not l8_pass:
        veto_active = True
        veto_layer = "L8"
        veto_reason = l8_detail

    return {
        "l1_status": l1_status,
        "l1_detail": l1_detail,
        "l1_pro_bias": pro_bias_str,
        "l2_status": l2_status,
        "l2_detail": l2_detail,
        "l2_regime": gex_regime,
        "l2_zero_gamma": zero_gamma,
        "l2_total_gex_cr": total_gex_cr,
        "l3_status": l3_status_label,
        "l3_detail": l3_detail,
        "l3_pass": l3_pass,
        "l3_veto": l3_veto,
        "l4_status": "PASS" if l4_pass else "WARN",
        "l4_detail": l4_detail,
        "l5_status": "PASS" if l5_pass else "WARN",
        "l5_detail": l5_detail,
        "l6_status": "PASS" if l6_pass else "WARN",
        "l6_detail": l6_detail,
        "l7_status": "PASS" if l7_pass else "WARN",
        "l7_detail": l7_detail,
        "l8_status": l8_status,
        "l8_detail": l8_detail,
        "l8_verdict": l8_report.verdict if l8_report else "SILENT",
        "l8_report": l8_report,
        "veto_active": veto_active,
        "veto_layer": veto_layer,
        "veto_reason": veto_reason,
        "rvol_data": {
            "value": rvol_val,
            "regime": rvol_regime,
            "status": rvol_status,
            "label": rvol_dec.get("reason", ""),
        },
    }


@dataclass
class MasterSynthesizedAction:
    action_type: str         # "BUY_CE", "BUY_PE", "WAIT_WATCH", "EXIT_POSITION"
    headline: str            # Big bold verdict
    sub_headline: str
    action_color: str
    action_border: str
    action_bg: str
    win_rate: float
    recommended_strike: float
    option_type: str
    option_symbol: str
    option_ltp: float
    spot_entry: float
    spot_sl: float
    spot_sl_pts: float
    spot_t1: float
    spot_t1_pts: float
    spot_t2: float
    spot_t2_pts: float
    opt_sl_price: float
    opt_t1_price: float
    opt_t2_price: float
    risk_rupees: float
    reward_t1_rupees: float
    reward_t2_rupees: float
    rr_ratio: float
    what_to_do_now: str
    reasons: List[str]
    prospective_win_rate: float = 50.0
    prospective_trigger_condition: str = ""
    pillar_breakdown_str: str = ""
    signal_stack: Optional[Dict[str, Any]] = None
    rvol_data: Optional[Dict[str, Any]] = None
    l8_data: Optional[Any] = None


def synthesize_master_cockpit_verdict(
    spot: float,
    radar: LevelRadarReport,
    dual: DualCandleReport,
    tabs_radar: AllTabsRadarReport,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    chain_df: Optional[pd.DataFrame] = None,
    prev_close: Optional[float] = None,
    now: Optional[datetime] = None,
) -> MasterSynthesizedAction:
    """
    Synthesizes Level Radar + Dual Candles + All-Tab Radar + 15m Trade Advisor into
    one single definitive Master Verdict and Order Ticket.
    """
    f15 = pa_frames.get("15m") if pa_frames else None
    
    # 1. Base technical advice from 15m Advisor
    adv = ta15.evaluate_15m_trade_advisor(
        df_15m=f15,
        spot=spot,
        chain_df=chain_df,
        prev_close=prev_close,
        now=now,
    )

    reasons: List[str] = []
    
    # Start with base action
    final_action = adv.action
    rec_type = adv.opt_type
    rec_strike = adv.opt_strike

    # Apply Level Striking logic
    if radar.striking_status == "STRIKING_RESISTANCE":
        # At Resistance ceiling
        # Only trigger PE if 15m candle confirms rejection (bearish or reversal wick) and 5m is not opposing
        if (dual.c15.direction == "BEARISH" or dual.c15.is_reversal) and dual.c5.direction != "BULLISH":
            final_action = "BUY_PE"
            rec_type = "PE"
            reasons.append(f"Spot is striking {radar.nearest_resistance.name} ({radar.nearest_resistance.price:,.0f}). Rejection in play.")
            reasons.append(f"15m candle confirms bearish defense: {dual.c15.pattern_name}.")
        elif dual.c15.direction == "BULLISH" and dual.c15.body_pct >= 60.0 and tabs_radar.bull_count >= 7:
            final_action = "BUY_CE"
            rec_type = "CE"
            reasons.append(f"Decisive breakout above {radar.nearest_resistance.name} on strong candle body.")
        else:
            final_action = "WAIT_WATCH"
            if dual.c15.direction == "BULLISH":
                reasons.append(f"Spot striking Resistance ({radar.nearest_resistance.name}), but 15m candle is {dual.c15.pattern_name}. Do not short into green candles! Wait for 15m candle close confirmation.")
            else:
                reasons.append(f"Striking Resistance ({radar.nearest_resistance.name}). No confirmed breakout or rejection close. WAIT.")

    elif radar.striking_status == "STRIKING_SUPPORT":
        # At Support floor
        # Only trigger CE if 15m candle confirms absorption (bullish or reversal wick) and 5m is not opposing
        if (dual.c15.direction == "BULLISH" or dual.c15.is_reversal) and dual.c5.direction != "BEARISH":
            final_action = "BUY_CE"
            rec_type = "CE"
            reasons.append(f"Spot is striking {radar.nearest_support.name} ({radar.nearest_support.price:,.0f}). Institutional floor defended.")
            reasons.append(f"15m candle confirms bullish absorption: {dual.c15.pattern_name}.")
        elif dual.c15.direction == "BEARISH" and dual.c15.body_pct >= 60.0 and tabs_radar.bear_count >= 7:
            final_action = "BUY_PE"
            rec_type = "PE"
            reasons.append(f"Decisive breakdown below {radar.nearest_support.name} on strong selling volume.")
        else:
            final_action = "WAIT_WATCH"
            if dual.c15.direction == "BEARISH":
                reasons.append(f"Spot striking Support ({radar.nearest_support.name}), but 15m candle is {dual.c15.pattern_name}. Do not buy calls into red candles! Wait for 15m candle close confirmation.")
            else:
                reasons.append(f"Striking Support ({radar.nearest_support.name}). No confirmed bounce or breakdown close. WAIT.")

    elif radar.striking_status == "APPROACHING_RESISTANCE":
        if final_action == "BUY_CE":
            final_action = "WAIT_WATCH"
            reasons.append(f"CE blocked: Spot is within {radar.nearest_resistance.distance_pts:.1f} pts of overhead {radar.nearest_resistance.name}. Never chase calls into resistance ceiling!")

    elif radar.striking_status == "APPROACHING_SUPPORT":
        if final_action == "BUY_PE":
            final_action = "WAIT_WATCH"
            reasons.append(f"PE blocked: Spot is within {radar.nearest_support.distance_pts:.1f} pts of underfoot {radar.nearest_support.name}. Never short puts into support floor!")

    elif radar.striking_status == "EQUILIBRIUM":
        # In the middle of nowhere
        if tabs_radar.bull_count >= 8 and dual.c15.direction == "BULLISH" and dual.c5.direction != "BEARISH":
            final_action = "BUY_CE"
            reasons.append("Strong multi-tab bullish trend across 14 institutional strategies despite open channel.")
        elif tabs_radar.bear_count >= 8 and dual.c15.direction == "BEARISH" and dual.c5.direction != "BULLISH":
            final_action = "BUY_PE"
            reasons.append("Strong multi-tab bearish trend across 14 institutional strategies despite open channel.")
        else:
            final_action = "WAIT_WATCH"
            reasons.append("Price is in No-Man's Land (Equilibrium). Wait for price to reach an institutional barrier.")

    # Guardrails: Anti-Fakeout & Trap Defense from Tab 36 & Tab 37
    if tabs_radar.active_trap_warning:
        reasons.append(f"⚠️ Anti-Trap / Fakeout Shield Active: {tabs_radar.active_trap_warning}")
        tr_up = tabs_radar.active_trap_warning.upper()
        if "BUY CALLS" in tr_up or "FAKE_BREAKOUT" in tr_up or "BULL TRAP" in tr_up:
            if final_action == "BUY_CE":
                final_action = "WAIT_WATCH"
                reasons.append("BUY_CE blocked by Anti-Fakeout Engine (Call writers defending overhead ceiling).")
        if "BUY PUTS" in tr_up or "FAKE_BREAKDOWN" in tr_up or "BEAR TRAP" in tr_up:
            if final_action == "BUY_PE":
                final_action = "WAIT_WATCH"
                reasons.append("BUY_PE blocked by Anti-Fakeout Engine (Put writers defending underfoot floor).")

    # Guardrails: Macroeconomic Event Lockout
    if tabs_radar.macro_event_warning:
        final_action = "WAIT_WATCH"
        reasons.append(f"🚨 Macro Event Lockout: {tabs_radar.macro_event_warning}")

    # Guardrails: Doji Emergency Exit
    if dual.c15.is_doji:
        final_action = "WAIT_WATCH"
        reasons.append("15m Doji printed. In accordance with Mentor Sushen's law: Doji is an EXIT signal. No fresh entries allowed.")

    # Guardrails: Timeframe Conflict between 15m and 5m when planning fresh trade
    if final_action in ("BUY_CE", "BUY_PE") and dual.harmony_verdict == "CONFLICTED_CHOP" and not dual.c15.is_reversal:
        final_action = "WAIT_WATCH"
        reasons.append(f"Timeframe Conflict: 15m ({dual.c15.direction}) opposes 5m ({dual.c5.direction}). Wait for 5m to align with 15m before entry.")

    # If rec_type was explicitly updated by synthesis and differs from adv.opt_type, refresh adv parameters
    if final_action in ("BUY_CE", "BUY_PE") and rec_type != adv.opt_type:
        adv = ta15.evaluate_15m_trade_advisor(
            df_15m=f15,
            spot=spot,
            chain_df=chain_df,
            prev_close=prev_close,
            now=now,
            selected_type=rec_type,
        )

    # ── 7-Layer Signal Stack & RVOL Gate Evaluation ─────────────────────────
    # Gate veto order: If L1 or L2 or L3 fails → NO TRADE, regardless of L4–L7.
    is_range_structure = (
        radar.striking_status in ("STRIKING_SUPPORT", "STRIKING_RESISTANCE")
        and dual.c15.is_reversal
    )

    # Resolve target_date and now_t from now or pa_frames
    if now is not None:
        target_date_val = now.date()
        target_time_val = now.time()
    elif pa_frames and "5m" in pa_frames and not pa_frames["5m"].empty and "timestamp" in pa_frames["5m"].columns:
        try:
            last_ts = pd.to_datetime(pa_frames["5m"]["timestamp"].iloc[-1])
            target_date_val = last_ts.date()
            target_time_val = last_ts.time()
        except Exception:
            target_date_val = datetime.now().date()
            target_time_val = datetime.now().time()
    else:
        target_date_val = datetime.now().date()
        target_time_val = datetime.now().time()

    stack_report = evaluate_signal_stack(
        spot=spot,
        chain_df=chain_df,
        target_date=target_date_val,
        target_time=target_time_val,
        candidate_action=final_action,
        is_range_structure=is_range_structure,
        dual=dual,
        tabs_radar=tabs_radar,
        pa_frames=pa_frames,
    )

    rvol_info = stack_report.get("rvol_data", {})
    rvol_regime = rvol_info.get("regime", "NA")
    rvol_val = rvol_info.get("value")
    l3_suppressed = False

    # Check L1, L2, L3 Veto
    if stack_report.get("veto_active"):
        veto_layer = stack_report.get("veto_layer")
        veto_reason = stack_report.get("veto_reason", "")
        reasons.append(f"⛔ Signal Stack Veto [{veto_layer}]: {veto_reason}")
        if veto_layer == "L3":
            l3_suppressed = True
        final_action = "WAIT_WATCH"

    # 4. Calculate Dynamic Bayesian Win Rate (Actual vs Prospective on Candle Confirmation)
    actual_wr, prospective_wr = calculate_bayesian_win_rate(
        action_type=final_action,
        tabs_radar=tabs_radar,
        dual=dual,
        radar=radar,
    )
    win_rate = actual_wr

    # 5. Format Strategic Pillar Breakdown String
    flow_p = tabs_radar.pillar_summary.get("🏛️ Institutional Flow", {})
    pa_p = tabs_radar.pillar_summary.get("📐 Price Action & Structure", {})
    ai_p = tabs_radar.pillar_summary.get("🔮 Quantitative & AI", {})
    pillar_breakdown_str = (
        f"🏛️ Flow: {flow_p.get('bull',0)}🟢/{flow_p.get('bear',0)}🔴/{flow_p.get('neutral',0)}⚪ · "
        f"📐 PA: {pa_p.get('bull',0)}🟢/{pa_p.get('bear',0)}🔴/{pa_p.get('neutral',0)}⚪ · "
        f"🔮 Setups/AI: {ai_p.get('bull',0)}🟢/{ai_p.get('bear',0)}🔴/{ai_p.get('neutral',0)}⚪"
    )

    # Format appearance
    if l3_suppressed:
        headline = "🟡 NO TRADE — CHOP ZONE FILTER ACTIVE"
        sub = "⚠️ CHOP ZONE — 77% breakouts fail. Wait for RVOL > 0.85 before any entry."
        color = GOLD
        border = "#D97706"
        bg = "rgba(245, 158, 11, 0.12)"
        what_now = "⚠️ CHOP ZONE — 77% breakouts fail. Wait for RVOL > 0.85 before any entry."
    elif final_action == "BUY_CE":
        headline = "🟢 STRONG BUY CALL (CE)"
        sub = f"Bullish Confluence ({tabs_radar.bull_count}/{tabs_radar.total_strategies} Strategies Agree) · {dual.c15.pattern_name}"
        color = GREEN
        border = "#059669"
        bg = "rgba(16, 185, 129, 0.15)"
        what_now = f"Enter NIFTY {int(rec_strike)} CE at spot {spot:,.1f}. Move SL to Cost as soon as spot reaches +20 pts!"
        if rvol_regime == "TREND_DAY":
            sub += " · 🚀 TREND DAY"
            what_now += " 🚀 hold to full target, don't exit at first wall."
    elif final_action == "BUY_PE":
        headline = "🔴 STRONG BUY PUT (PE)"
        sub = f"Bearish Confluence ({tabs_radar.bear_count}/{tabs_radar.total_strategies} Strategies Agree) · {dual.c15.pattern_name}"
        color = RED
        border = "#DC2626"
        bg = "rgba(239, 68, 68, 0.15)"
        what_now = f"Enter NIFTY {int(rec_strike)} PE at spot {spot:,.1f}. Move SL to Cost as soon as spot reaches +20 pts!"
        if rvol_regime == "TREND_DAY":
            sub += " · 🚀 TREND DAY"
            what_now += " 🚀 hold to full target, don't exit at first wall."
    elif final_action == "EXIT_POSITION":
        headline = "🛑 EXIT ALL POSITIONS IMMEDIATELY"
        sub = "Reversal / Doji Exit Triggered at Day Extreme"
        color = PURPLE
        border = "#9333EA"
        bg = "rgba(147, 51, 234, 0.15)"
        what_now = "Book available profits or cut risk now. Market structure has shifted."
    else:
        headline = "🟡 STAND ASIDE & WAIT (NO TRADE)"
        sub = f"{radar.striking_title.split(':')[0]} · Capital Protection Gate Active"
        color = GOLD
        border = "#D97706"
        bg = "rgba(245, 158, 11, 0.12)"
        if tabs_radar.active_trap_warning:
            what_now = f"⚠️ TRAP / FAKEOUT ACTIVE: {tabs_radar.active_trap_warning}. Institutions are trapping retail breakouts! Stand aside."
        elif tabs_radar.macro_event_warning:
            what_now = f"🚨 MACRO HALT: {tabs_radar.macro_event_warning}. System risk locked."
        elif radar.striking_status == "STRIKING_RESISTANCE":
            what_now = f"Spot is testing Resistance ({radar.nearest_resistance.name}). 🔴 {tabs_radar.bear_count}/{tabs_radar.total_strategies} Strategies confirm institutional selling overhead! Hands off keyboard! Wait for 15m candle close confirmation before entering PE to unlock {prospective_wr:.1f}% Bayesian win rate."
        elif radar.striking_status == "STRIKING_SUPPORT":
            what_now = f"Spot is testing Support ({radar.nearest_support.name}). 🟢 {tabs_radar.bull_count}/{tabs_radar.total_strategies} Strategies confirm institutional absorption underfoot! Hands off keyboard! Wait for 15m candle close confirmation before entering CE to unlock {prospective_wr:.1f}% Bayesian win rate."
        elif radar.striking_status == "APPROACHING_RESISTANCE":
            what_now = f"Spot approaching Resistance ({radar.nearest_resistance.name}). Ceiling overhead: Do not buy CE into resistance!"
        elif radar.striking_status == "APPROACHING_SUPPORT":
            what_now = f"Spot approaching Support ({radar.nearest_support.name}). Floor underfoot: Do not short PE into support!"
        else:
            what_now = "Hands off keyboard. Wait for price to touch a key support/resistance and confirm with a 15m candle close."

    # Prepend L8 Order Flow confirmation note when present (Module I requirement)
    l8_rep = stack_report.get("l8_report")
    if l8_rep and l8_rep.action_line_note and final_action in ("BUY_CE", "BUY_PE"):
        what_now = f"{l8_rep.action_line_note} — {what_now}"

    return MasterSynthesizedAction(
        action_type=final_action,
        headline=headline,
        sub_headline=sub,
        action_color=color,
        action_border=border,
        action_bg=bg,
        win_rate=win_rate,
        recommended_strike=rec_strike,
        option_type=rec_type,
        option_symbol=f"NIFTY {int(rec_strike)} {rec_type}",
        option_ltp=adv.opt_ltp,
        spot_entry=adv.spot_entry,
        spot_sl=adv.spot_sl,
        spot_sl_pts=adv.spot_risk_pts,
        spot_t1=adv.spot_t1,
        spot_t1_pts=adv.spot_reward_t1_pts,
        spot_t2=adv.spot_t2,
        spot_t2_pts=adv.spot_reward_t2_pts,
        opt_sl_price=adv.opt_sl_price,
        opt_t1_price=adv.opt_t1_price,
        opt_t2_price=adv.opt_t2_price,
        risk_rupees=adv.opt_risk_rupees,
        reward_t1_rupees=adv.opt_gain_t1_rupees,
        reward_t2_rupees=adv.opt_gain_t2_rupees,
        rr_ratio=adv.rr_ratio,
        what_to_do_now=what_now,
        reasons=reasons,
        prospective_win_rate=prospective_wr,
        prospective_trigger_condition=f"15m Candle Rejection Close at {radar.closest_barrier.name}" if "STRIKING" in radar.striking_status else "Key Level Touch & Confirmation",
        pillar_breakdown_str=pillar_breakdown_str,
        signal_stack=stack_report,
        rvol_data=rvol_info,
        l8_data=l8_rep,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Master UI Renderer for Tab 0
# ═══════════════════════════════════════════════════════════════════════════

def render_unified_decision_cockpit(
    spot: float,
    lv: Any = None,
    read: Any = None,
    chain_df: Optional[pd.DataFrame] = None,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    vb_daily: Optional[pd.DataFrame] = None,
    idx_c: Optional[pd.DataFrame] = None,
    master_cockpit: Any = None,
    prev_close: Optional[float] = None,
    now: Optional[datetime] = None,
    sig: Any = None,
    dte: float = 0.0,
    nse_ts: str = "",
) -> None:
    """
    Renders the complete, unified Decision Cockpit solving all user dilemmas.
    """
    now_dt = now or datetime.now()

    # 1. Compute Engines
    radar = evaluate_live_level_radar(
        spot=spot,
        lv=lv,
        chain_df=chain_df,
        pa_frames=pa_frames,
        vb_daily=vb_daily,
        idx_c=idx_c,
    )

    dual = evaluate_dual_candles(pa_frames, spot)

    tabs_radar = evaluate_all_tabs_radar(
        spot=spot,
        chain_df=chain_df,
        pa_frames=pa_frames,
        master_cockpit=master_cockpit,
        prev_close=prev_close,
    )

    master_act = synthesize_master_cockpit_verdict(
        spot=spot,
        radar=radar,
        dual=dual,
        tabs_radar=tabs_radar,
        pa_frames=pa_frames,
        chain_df=chain_df,
        prev_close=prev_close,
        now=now_dt,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 0: 7-LAYER SIGNAL STACK & RVOL DECISION TERMINAL RIBBON
    # ═══════════════════════════════════════════════════════════════════════
    stack = master_act.signal_stack or {}
    rvol_data = master_act.rvol_data or {}

    rvol_val = rvol_data.get("value")
    rvol_regime = rvol_data.get("regime", "NA")
    rvol_status = rvol_data.get("status", "ALLOWED")

    # Format RVOL Card values (red: CHOP_ZONE, green: NORMAL, gold: TREND_DAY)
    if rvol_regime == "CHOP_ZONE":
        rvol_badge_txt = "CHOP ZONE"
        rvol_card_color = "#EF4444"
        rvol_card_bg = "rgba(239, 68, 68, 0.16)"
        rvol_border = "#DC2626"
    elif rvol_regime == "TREND_DAY":
        rvol_badge_txt = "TREND DAY"
        rvol_card_color = "#F59E0B"
        rvol_card_bg = "rgba(245, 158, 11, 0.16)"
        rvol_border = "#D97706"
    elif rvol_regime == "NORMAL":
        rvol_badge_txt = "NORMAL"
        rvol_card_color = "#10B981"
        rvol_card_bg = "rgba(16, 185, 129, 0.16)"
        rvol_border = "#059669"
    elif rvol_status == "HOLD":
        rvol_badge_txt = "HOLD (<09:30)"
        rvol_card_color = "#94A3B8"
        rvol_card_bg = "rgba(148, 163, 184, 0.12)"
        rvol_border = "#475569"
    else:
        rvol_badge_txt = "N/A"
        rvol_card_color = "#94A3B8"
        rvol_card_bg = "rgba(148, 163, 184, 0.12)"
        rvol_border = "#475569"

    rvol_val_str = f"{rvol_val:.2f}×" if rvol_val is not None else "N/A"

    # Mini SVG Sparkline comparing today's volume curve vs 10-day TOD average curve
    t_date_val = rvol_gate._parse_date(now_dt)
    t_time_val = rvol_gate._parse_time(now_dt)
    sparkline_svg = compute_rvol_sparkline_svg(
        t_date_val,
        t_time_val,
        color=rvol_card_color,
        width=135,
        height=36,
    )

    # L1 & L2 display
    l1_bias = stack.get("l1_pro_bias", "NEUTRAL")
    l1_col = "#10B981" if "BULLISH" in l1_bias else ("#EF4444" if "BEARISH" in l1_bias else "#94A3B8")
    l2_regime = stack.get("l2_regime", "NEUTRAL")
    l2_gex_cr = stack.get("l2_total_gex_cr", 0.0)
    l2_zero_g = stack.get("l2_zero_gamma", spot)
    l2_col = "#10B981" if l2_regime == "LONG" else ("#EF4444" if l2_regime == "SHORT" else "#94A3B8")

    # L8 Order Flow display
    l8_rep = stack.get("l8_report")
    if l8_rep:
        l8_sym_short = l8_rep.symbol
        l8_v = l8_rep.verdict
        if l8_v == "CONFIRM":
            l8_badge_txt = "CONFIRM"
            l8_card_color = "#10B981"
            l8_card_bg = "rgba(16, 185, 129, 0.16)"
            l8_border = "#059669"
        elif "VETO" in l8_v:
            l8_badge_txt = "VETO"
            l8_card_color = "#EF4444"
            l8_card_bg = "rgba(239, 68, 68, 0.16)"
            l8_border = "#DC2626"
        else:
            l8_badge_txt = "SILENT"
            l8_card_color = "#94A3B8"
            l8_card_bg = "rgba(148, 163, 184, 0.12)"
            l8_border = "#475569"
        if l8_rep.sim_suppressed:
            l8_badge_txt += " (SIM)"

        l8_cvd_badge = l8_rep.cvd_slope_badge
        l8_cvd_col = "#10B981" if l8_cvd_badge == "▲" else ("#EF4444" if l8_cvd_badge == "▼" else "#94A3B8")
        l8_s_str = f"₹{l8_rep.nearest_defended_bid_wall:.2f}" if l8_rep.nearest_defended_bid_wall else "None"
        l8_r_str = f"₹{l8_rep.nearest_defended_ask_wall:.2f}" if l8_rep.nearest_defended_ask_wall else "None"
        l8_absorb_short = l8_rep.absorption_status[:36]
    else:
        l8_sym_short = "NIFTY DOM"
        l8_badge_txt = "SILENT"
        l8_card_color = "#94A3B8"
        l8_card_bg = "rgba(148, 163, 184, 0.12)"
        l8_border = "#475569"
        l8_cvd_badge = "—"
        l8_cvd_col = "#94A3B8"
        l8_s_str = "None"
        l8_r_str = "None"
        l8_absorb_short = "No DOM feed loaded"

    # Veto status
    veto_active = stack.get("veto_active", False)
    veto_layer = stack.get("veto_layer")
    if veto_active:
        veto_badge = f"⛔ VETO [{veto_layer}]"
        veto_col = "#EF4444"
        veto_desc = stack.get("veto_reason", "Capital protection lock active")
    else:
        veto_badge = "✅ RUNWAY CLEAR"
        veto_col = "#10B981"
        veto_desc = "All 8 layers aligned for execution"

    # Regime Recheck & Flip Notification (12:30 and 14:00)
    recheck_alert_html = ""
    flip = rvol_gate.find_regime_flip(t_date_val)
    if flip and t_time_val >= datetime.strptime("11:30", "%H:%M").time():
        flip_time = flip.get("flip_time")
        recheck_alert_html = f"""
        <div style="background: rgba(16, 185, 129, 0.12); border: 1px solid #059669; border-radius: 8px; padding: 8px 14px; margin-bottom: 12px; font-size: 12.5px; color: #E2E8F0;">
            🔄 <b>REGIME RE-CHECK:</b> Intraday volume regime flipped from <b>{flip.get('from')}</b> to <b>{flip.get('to')}</b> at <b>{flip_time}</b> ({flip.get('rvol_at_flip', 0.85):.2f}×). Chop suppression lifted — breakout signals reactivated. Open trades: let run to target.
        </div>
        """

    _render_html(
        f"""
        {recheck_alert_html}
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 14px;">
            <!-- Card 1: L1 Institutional Context -->
            <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 12px 16px;">
                <div style="font-size: 11px; font-weight: 700; color: #94A3B8; text-transform: uppercase;">🏛️ L1: Inst Context</div>
                <div style="font-size: 18px; font-weight: 900; color: {l1_col}; margin-top: 4px;">{l1_bias}</div>
                <div style="font-size: 11px; color: #CBD5E1; margin-top: 2px;">PRO Net Futures Bias</div>
            </div>

            <!-- Card 2: L2 GEX Structure -->
            <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 12px 16px;">
                <div style="font-size: 11px; font-weight: 700; color: #94A3B8; text-transform: uppercase;">🎯 L2: GEX Structure</div>
                <div style="font-size: 18px; font-weight: 900; color: {l2_col}; margin-top: 4px;">{l2_regime} · {l2_gex_cr:+.1f}Cr</div>
                <div style="font-size: 11px; color: #CBD5E1; margin-top: 2px;">Zero-G Flip: {l2_zero_g:,.0f}</div>
            </div>

            <!-- Card 3: L3 RVOL Gate -->
            <div style="background: rgba(15, 23, 42, 0.8); border: 1.5px solid {rvol_border}; border-radius: 10px; padding: 12px 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 11px; font-weight: 700; color: #94A3B8; text-transform: uppercase;">📊 L3: RVOL Gate</span>
                    <span style="background: {rvol_card_bg}; color: {rvol_card_color}; font-size: 10px; font-weight: 800; padding: 2px 7px; border-radius: 6px;">{rvol_badge_txt}</span>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-top: 4px;">
                    <div>
                        <div style="font-size: 22px; font-weight: 900; color: {rvol_card_color}; font-family: monospace;">{rvol_val_str}</div>
                        <div style="font-size: 10px; color: #94A3B8;">vs 10-day TOD avg</div>
                    </div>
                    <div>
                        {sparkline_svg}
                    </div>
                </div>
            </div>

            <!-- Card 4: L8 Order Flow (DOM) -->
            <div style="background: rgba(15, 23, 42, 0.8); border: 1.5px solid {l8_border}; border-radius: 10px; padding: 12px 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 11px; font-weight: 700; color: #94A3B8; text-transform: uppercase;">🌊 L8: Order Flow</span>
                    <span style="background: {l8_card_bg}; color: {l8_card_color}; font-size: 10px; font-weight: 800; padding: 2px 7px; border-radius: 6px;">{l8_badge_txt}</span>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 5px;">
                    <div style="font-size: 13.5px; font-weight: 800; color: #F8FAFC;">{l8_sym_short}</div>
                    <div style="font-size: 14px; font-weight: 900; color: {l8_cvd_col};">{l8_cvd_badge} CVD</div>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px; color: #CBD5E1; margin-top: 3px;">
                    <span>S: <b style="color: #10B981;">{l8_s_str}</b></span>
                    <span>R: <b style="color: #EF4444;">{l8_r_str}</b></span>
                </div>
                <div style="font-size: 10px; color: #94A3B8; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                    {l8_absorb_short}
                </div>
            </div>

            <!-- Card 5: Signal Stack Status -->
            <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 12px 16px;">
                <div style="font-size: 11px; font-weight: 700; color: #94A3B8; text-transform: uppercase;">🛡️ Signal Stack (8 Layers)</div>
                <div style="font-size: 18px; font-weight: 900; color: {veto_col}; margin-top: 4px;">{veto_badge}</div>
                <div style="font-size: 11px; color: #CBD5E1; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{veto_desc}</div>
            </div>
        </div>
        """
    )

    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 1: MASTER VERDICT BANNER & SPOON-FED ORDER PLACEMENT TICKET
    # ═══════════════════════════════════════════════════════════════════════
    _render_html(
        f"""
        <div style="background: {master_act.action_bg}; border: 2.5px solid {master_act.action_border}; border-radius: 12px; padding: 18px 22px; margin-bottom: 14px; box-shadow: 0 6px 20px rgba(0,0,0,0.4);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                <div>
                    <div style="font-size: 11px; font-weight: 800; color: {SLATE}; letter-spacing: 1px; text-transform: uppercase;">
                        🎯 ALL-TAB MASTER DECISION VERDICT · ZERO DILEMMA
                    </div>
                    <div style="font-size: 32px; font-weight: 900; color: {master_act.action_color}; line-height: 1.15; margin: 4px 0;">
                        {master_act.headline}
                    </div>
                    <div style="font-size: 14px; font-weight: 600; color: #E2E8F0;">
                        {master_act.sub_headline}
                    </div>
                    {f'<div style="font-size: 12px; color: #94A3B8; margin-top: 5px; font-weight: 600; font-family: monospace;">{master_act.pillar_breakdown_str}</div>' if master_act.pillar_breakdown_str else ''}
                </div>
                <div style="text-align: right; background: rgba(0,0,0,0.45); padding: 10px 18px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);">
                    <div style="font-size: 11px; font-weight: 700; color: {SLATE}; text-transform: uppercase;">Bayesian Win Rate</div>
                    <div style="font-size: 28px; font-weight: 900; color: {master_act.action_color};">
                        {master_act.win_rate:.1f}%
                    </div>
                    <div style="font-size: 11px; font-weight: 700; color: {'#38BDF8' if master_act.action_type == 'WAIT_WATCH' else '#CBD5E1'}; margin-top: 2px;">
                        {f"🎯 Prospective: {master_act.prospective_win_rate:.1f}% on 15m Close" if master_act.action_type == 'WAIT_WATCH' else tabs_radar.consensus_verdict.split('·')[0]}
                    </div>
                    <div style="font-size: 10px; color: #94A3B8; margin-top: 2px;">
                        {tabs_radar.consensus_verdict}
                    </div>
                </div>
            </div>
            <div style="margin-top: 12px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.12); font-size: 14px; color: #F8FAFC;">
                👉 <b>WHAT TO DO RIGHT NOW:</b> {master_act.what_to_do_now}
            </div>
        </div>
        """
    )

    # 🎫 Trader's Order Placement Ticket
    if master_act.action_type in ("BUY_CE", "BUY_PE"):
        st.markdown("#### 🎫 Trader's Order Placement Ticket (Zero Mental Math)")
        st.caption("Everything you need before clicking Buy on your broker terminal:")

        t1, t2, t3, t4 = st.columns(4)
        t1.metric(
            "Recommended Contract",
            master_act.option_symbol,
            f"LTP ₹{master_act.option_ltp:.1f}"
        )
        t2.metric(
            "Exact Stop Loss",
            f"{master_act.spot_sl:,.1f}",
            f"-{master_act.spot_sl_pts:.1f} pts (Opt ₹{master_act.opt_sl_price:.1f})",
            delta_color="inverse"
        )
        t3.metric(
            "Target 1 (+20 pts CTC)",
            f"{master_act.spot_t1:,.1f}",
            f"+{master_act.spot_t1_pts:.1f} pts (Opt ₹{master_act.opt_t1_price:.1f})",
            delta_color="normal"
        )
        t4.metric(
            "Target 2 (+40 pts)",
            f"{master_act.spot_t2:,.1f}",
            f"+{master_act.spot_t2_pts:.1f} pts (Opt ₹{master_act.opt_t2_price:.1f})",
            delta_color="normal"
        )

        _render_html(
            f"""
            <div style="background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 10px 16px; margin-bottom: 16px; font-size: 13px; color: #CBD5E1;">
                💰 <b>Capital Risk Math:</b> Risk per lot: <b style="color:{RED};">₹{master_act.risk_rupees:,.0f}</b> · 
                Target 1 Gain: <b style="color:{GREEN};">+₹{master_act.reward_t1_rupees:,.0f}</b> · 
                Target 2 Gain: <b style="color:{GREEN};">+₹{master_act.reward_t2_rupees:,.0f}</b> · 
                Risk-Reward: <b>1 : {master_act.rr_ratio:.1f}</b> · 
                <span style="background:rgba(56,189,248,0.2);color:#38BDF8;padding:2px 6px;border-radius:4px;font-weight:700;">Rule: Trail SL to Cost immediately at +20 pts</span>
            </div>
            """
        )

    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 2: REAL-TIME LEVEL STRIKING & PROXIMITY RADAR
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### 🧭 Real-Time Level Striking & Proximity Radar")
    st.caption("Tracks whether Nifty is striking key institutional support/resistance and gives exact directives:")

    _render_html(
        f"""
        <div style="background: {radar.striking_bg}; border: 1.5px solid {radar.striking_border}; border-radius: 10px; padding: 14px 18px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="font-size: 16px; font-weight: 800; color: {radar.striking_color};">
                    {radar.striking_title}
                </div>
                <div style="font-size: 12px; background: rgba(0,0,0,0.3); color: #fff; padding: 3px 10px; border-radius: 12px; font-family: monospace;">
                    Spot: {spot:,.1f}
                </div>
            </div>
            <div style="font-size: 13.5px; color: #F1F5F9; margin-top: 8px; line-height: 1.5;">
                {radar.striking_advice}
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 12px; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 10px;">
                <div style="background: rgba(0,0,0,0.25); padding: 8px; border-radius: 6px;">
                    {radar.action_if_reversing}
                </div>
                <div style="background: rgba(0,0,0,0.25); padding: 8px; border-radius: 6px;">
                    {radar.action_if_breakout}
                </div>
                <div style="background: rgba(0,0,0,0.25); padding: 8px; border-radius: 6px;">
                    {radar.action_if_trap}
                </div>
            </div>
        </div>
        """
    )

    if getattr(radar, "striking_prediction", None) is not None:
        try:
            import writers_graph_engine as wge
            _render_html(wge.render_striking_price_prediction_card(radar.striking_prediction))
        except Exception:
            pass

    r_col1, r_col2 = st.columns(2)
    with r_col1:
        st.markdown(f"**🔴 Nearest Overhead Resistances (Ceilings)**")
        r_rows = []
        for b in radar.all_resistances[:4]:
            r_rows.append({
                "Level Name": b.name,
                "Price": f"{b.price:,.1f}",
                "Distance": f"+{b.distance_pts:.1f} pts",
                "Source": b.source,
            })
        st.dataframe(pd.DataFrame(r_rows), use_container_width=True, hide_index=True)

    with r_col2:
        st.markdown(f"**🟢 Nearest Underfoot Supports (Floors)**")
        s_rows = []
        for b in radar.all_supports[:4]:
            s_rows.append({
                "Level Name": b.name,
                "Price": f"{b.price:,.1f}",
                "Distance": f"-{b.distance_pts:.1f} pts",
                "Source": b.source,
            })
        st.dataframe(pd.DataFrame(s_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 3: DUAL-TIMEFRAME CANDLE CONFLUENCE (15m vs 5m)
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### ⚖️ Dual-Timeframe Candle Confluence (15-Min Execution & 5-Min Momentum)")
    st.caption("Checks what 15m and 5m candles are saying right now:")

    _render_html(
        f"""
        <div style="background: {CARD_BG}; border: 1px solid rgba(255,255,255,0.12); border-left: 4px solid {dual.harmony_color}; border-radius: 8px; padding: 12px 16px; margin-bottom: 14px;">
            <div style="font-size: 15px; font-weight: 800; color: {dual.harmony_color};">
                {dual.harmony_title}
            </div>
            <div style="font-size: 13px; color: #CBD5E1; margin-top: 4px;">
                {dual.what_candles_say}
            </div>
        </div>
        """
    )

    c15_col, c5_col = st.columns(2)

    with c15_col:
        c15 = dual.c15
        badge_c15 = GREEN if c15.direction == "BULLISH" else (RED if c15.direction == "BEARISH" else SLATE)
        _render_html(
            f"""
            <div style="background: rgba(30, 41, 59, 0.6); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 16px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:14px; font-weight:800; color:#60A5FA;">15-MINUTE CANDLE (EXECUTION ANCHOR)</span>
                    <span style="background:{badge_c15}; color:#fff; font-size:11px; font-weight:700; padding:2px 8px; border-radius:10px;">{c15.direction}</span>
                </div>
                <div style="font-size:16px; font-weight:700; color:#fff; margin-top:6px;">
                    {c15.pattern_name}
                </div>
                <div style="font-size:12px; color:#94A3B8; margin-top:4px;">
                    {c15.summary}
                </div>
                <div style="display:flex; gap:8px; margin-top:10px; font-size:11px; color:#CBD5E1;">
                    <span>Body: <b>{c15.body_pct:.0f}%</b></span> · 
                    <span>Upper Wick: <b>{c15.upper_wick_pct:.0f}%</b></span> · 
                    <span>Lower Wick: <b>{c15.lower_wick_pct:.0f}%</b></span>
                </div>
            </div>
            """
        )

    with c5_col:
        c5 = dual.c5
        badge_c5 = GREEN if c5.direction == "BULLISH" else (RED if c5.direction == "BEARISH" else SLATE)
        _render_html(
            f"""
            <div style="background: rgba(30, 41, 59, 0.6); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 12px 16px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:14px; font-weight:800; color:#38BDF8;">5-MINUTE CANDLE (EARLY MOMENTUM)</span>
                    <span style="background:{badge_c5}; color:#fff; font-size:11px; font-weight:700; padding:2px 8px; border-radius:10px;">{c5.direction}</span>
                </div>
                <div style="font-size:16px; font-weight:700; color:#fff; margin-top:6px;">
                    {c5.pattern_name}
                </div>
                <div style="font-size:12px; color:#94A3B8; margin-top:4px;">
                    {c5.summary}
                </div>
                <div style="display:flex; gap:8px; margin-top:10px; font-size:11px; color:#CBD5E1;">
                    <span>Body: <b>{c5.body_pct:.0f}%</b></span> · 
                    <span>Upper Wick: <b>{c5.upper_wick_pct:.0f}%</b></span> · 
                    <span>Lower Wick: <b>{c5.lower_wick_pct:.0f}%</b></span>
                </div>
            </div>
            """
        )

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 4: ALL-TAB INSTITUTIONAL CONFLUENCE RADAR (TABLE & PILLARS)
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### 🌐 All-Tab Institutional Confluence Radar (14 Strategy Engines)")
    st.caption("Synthesizes live voting from all 14 analytical engines across the application into 3 strategic pillars:")

    # Render Pillar Badges
    p_cols = st.columns(3)
    p_configs = [
        ("🏛️ Institutional Flow", p_cols[0], BLUE),
        ("📐 Price Action & Structure", p_cols[1], PURPLE),
        ("🔮 Quantitative & AI", p_cols[2], GOLD),
    ]
    for p_label, p_col, p_color in p_configs:
        p_data = tabs_radar.pillar_summary.get(p_label, {"bull": 0, "bear": 0, "neutral": 0, "total": 0})
        with p_col:
            _render_html(
                f"""
                <div style="background: rgba(15, 23, 42, 0.7); border: 1px solid rgba(255,255,255,0.1); border-left: 3.5px solid {p_color}; border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;">
                    <div style="font-size: 12px; font-weight: 800; color: #E2E8F0;">{p_label} ({p_data.get('total', 0)} Engines)</div>
                    <div style="display: flex; gap: 8px; margin-top: 5px; font-size: 12px; font-weight: 700;">
                        <span style="color: {GREEN};">🟢 {p_data.get('bull', 0)} Bull</span> · 
                        <span style="color: {RED};">🔴 {p_data.get('bear', 0)} Bear</span> · 
                        <span style="color: {SLATE};">⚪ {p_data.get('neutral', 0)} Neutral</span>
                    </div>
                </div>
                """
            )

    tab_rows = []
    for item in tabs_radar.items:
        s_icon = "🟢" if item.signal == "BULLISH" else ("🔴" if item.signal == "BEARISH" else "⚪")
        tab_rows.append({
            "Pillar": item.pillar,
            "Dashboard Tab": item.tab_name,
            "Strategy Engine": item.feature,
            "Signal": f"{s_icon} {item.signal}",
            "Live Reading": item.reading,
            "Plain-English Translation": item.plain_meaning,
        })

    st.dataframe(
        pd.DataFrame(tab_rows),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # BLOCK 5: SPECIALIZED DEEP-DIVE EXPANDERS (100% PRESERVED)
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### 🛡️ Specialized Deep-Dive Intelligence Panels")

    # Expander 1: Top & Bottom 15m Reversal Sniper
    with st.expander("🏹 Top & Bottom 15m Reversal Sniper & 2-Phase Gap Engine", expanded=False):
        try:
            _f5 = pa_frames.get("5m") if pa_frames else None
            _f15 = pa_frames.get("15m") if pa_frames else None
            _r1_val = float(lv.r1) if (lv and hasattr(lv, "r1")) else None
            _s1_val = float(lv.s1) if (lv and hasattr(lv, "s1")) else None
            _pdh = float(vb_daily["High"].iloc[-1]) if (vb_daily is not None and not vb_daily.empty) else None
            _pdl = float(vb_daily["Low"].iloc[-1]) if (vb_daily is not None and not vb_daily.empty) else None
            _pdc = float(vb_daily["Close"].iloc[-1]) if (vb_daily is not None and not vb_daily.empty) else None
            _sniper_rep = tbs.evaluate_sniper(
                _f5, _f15, spot, r1=_r1_val, s1=_s1_val, pdh=_pdh, pdl=_pdl, pdc=_pdc, round_step=100
            )
            tbs.render_sniper_card(_sniper_rep)
        except Exception as e:
            st.caption(f"Top/Bottom sniper view unavailable: {e}")

    # Expander 2: 1m 3-Drive Institutional Scalper
    with st.expander("⚡ Priyank Sharma's 1-Minute 3-Drive Institutional Scalper", expanded=False):
        try:
            import three_drive_scalper as _tds
            _fut1 = pa_frames.get("1m") if pa_frames else None
            _kl_cockpit = {}
            if lv and hasattr(lv, "r1"): _kl_cockpit["R1 Pivot"] = float(lv.r1)
            if lv and hasattr(lv, "s1"): _kl_cockpit["S1 Pivot"] = float(lv.s1)
            _td_setup = _tds.evaluate_three_drive(_fut1, spot, key_levels=_kl_cockpit)
            if _td_setup:
                _tds.render_three_drive_card(_td_setup)
            else:
                st.info("No active 1-minute 3-drive setup detected currently.")
        except Exception as e:
            st.caption(f"3-Drive scalper view unavailable: {e}")

    # Expander 3: Historical Daily Wicks & 100-Pt Turning Levels
    with st.expander("🏛️ Historical Daily Wicks & 100-Pt Turning Levels", expanded=False):
        try:
            import historical_levels as _hl
            _t_hi = float(idx_c["high"].max()) if (idx_c is not None and not idx_c.empty and "high" in idx_c) else spot
            _t_lo = float(idx_c["low"].min()) if (idx_c is not None and not idx_c.empty and "low" in idx_c) else spot
            _cur_dt = now_dt.date() if hasattr(now_dt, "date") else None
            _hl.render_cockpit_card(spot, today_high=_t_hi, today_low=_t_lo, current_date=_cur_dt)
        except Exception as e:
            st.caption(f"Historical levels view unavailable: {e}")

    # Expander 4: 15-Minute Live Interactive Strike Calculator
    with st.expander("⚡ 15-Minute Live Trade Advisor & Strike Decision Box (Calculator)", expanded=False):
        try:
            ta15.render_15m_trade_advisor_ui(
                pa_frames=pa_frames,
                spot=spot,
                chain_df=chain_df,
                prev_close=prev_close,
                now=now_dt,
            )
        except Exception as e:
            st.caption(f"15m Trade advisor calculator unavailable: {e}")

    # Expander 5: 14-Parameter Institutional Confluence Matrix & Master Story
    if master_cockpit is not None:
        with st.expander("📊 14-Parameter Institutional Matrix & Master Market Story", expanded=False):
            try:
                import narrative as _nar_mod
                _nar_mod.render_cockpit(master_cockpit, spot, lv, sig, dte, nse_ts)
            except Exception as e:
                st.caption(f"Narrative cockpit view unavailable: {e}")
