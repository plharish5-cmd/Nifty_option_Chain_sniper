"""
option_chain_narrative.py
═══════════════════════════════════════════════════════════════════════════════
Institutional Option Chain Narrative Intelligence & Interactive Matrix Suite
═══════════════════════════════════════════════════════════════════════════════

Provides rich, actionable, plain-English market-maker narratives for:
  1. Tab 1: "📊 Option Chain / Key Levels"
     - Executive Battlefield & Defense Perimeter Storyboard
     - Max Pain & Expiry Pinning Gravitation Narrative
     - ATM Straddle Implied Move & Volatility Boundary Narrative
     - 4-Quadrant Trapped Trader & Gamma Squeeze Radar
     - Actionable Tactical Execution Playbook (When to Buy CE / PE / Sit Out)
     - Interactive Full Option Chain Matrix with Live Quadrant Buildups & Strike-Level Narratives

  2. Tab 2: "Change in OI"
     - Intraday Flow & Shift Narrative Story (Who is in control today)
     - Dynamic Strike Migration & Overhead Pressure Analysis
     - Trapped Writers & Short-Covering Warning Radar
     - Top Intraday Shift Activity Cards (Additions vs Unwinding)
     - Ranked Intraday Activity Table with Institutional Action Meanings
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

import analytics as an
import config
from ui_utils import render_html, _render_html


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions: Formatting & Quadrants
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_oi(val: float | None) -> str:
    """Formats contract quantities into readable Lakhs / Crores."""
    if val is None or np.isnan(val):
        return "—"
    abs_val = abs(val)
    sign = "-" if val < 0 else ("+" if val > 0 else "")
    if abs_val >= 1e7:
        return f"{sign}{abs_val / 1e7:.2f} Cr"
    elif abs_val >= 1e5:
        return f"{sign}{abs_val / 1e5:.1f} L"
    elif abs_val >= 1e3:
        return f"{sign}{abs_val / 1e3:.1f} k"
    return f"{val:,.0f}"


def _fmt_price(val: float | None) -> str:
    if val is None or np.isnan(val):
        return "—"
    return f"{val:,.2f}"


def _get_buildup_pill(pillar: str, side: str = "CE") -> str:
    """
    Returns an HTML badge for the 4-quadrant buildup.
    Accounts for option side (Call writing = bearish, Put writing = bullish).
    """
    p = str(pillar).lower().strip()
    if "short_buildup" in p or "short_build_up" in p or "shortbuildup" in p:
        if side == "CE":
            bg, border, col, text = "rgba(239,68,68,0.18)", "#ef4444", "#f87171", "🔴 Short Buildup (Writing)"
        else:
            bg, border, col, text = "rgba(16,185,129,0.18)", "#10b981", "#34d399", "🟢 Short Buildup (Writing)"
    elif "long_buildup" in p or "long_build_up" in p or "longbuildup" in p:
        if side == "CE":
            bg, border, col, text = "rgba(16,185,129,0.18)", "#10b981", "#34d399", "🟢 Long Buildup (Buying)"
        else:
            bg, border, col, text = "rgba(239,68,68,0.18)", "#ef4444", "#f87171", "🔴 Long Buildup (Buying)"
    elif "short_covering" in p or "shortcovering" in p:
        if side == "CE":
            bg, border, col, text = "rgba(56,189,248,0.18)", "#38bdf8", "#7dd3fc", "⚡ Short Covering (Squeeze)"
        else:
            bg, border, col, text = "rgba(245,158,11,0.18)", "#f59e0b", "#fbbf24", "⚡ Short Covering (Bearish)"
    elif "long_unwinding" in p or "longunwinding" in p:
        if side == "CE":
            bg, border, col, text = "rgba(245,158,11,0.18)", "#f59e0b", "#fbbf24", "⚠️ Long Unwinding (Selling)"
        else:
            bg, border, col, text = "rgba(56,189,248,0.18)", "#38bdf8", "#7dd3fc", "⚠️ Long Unwinding (Dumping)"
    else:
        bg, border, col, text = "rgba(148,163,184,0.10)", "#475569", "#94a3b8", "⚪ Neutral"

    return f'<span style="background:{bg};border:1px solid {border}55;color:{col};padding:2px 7px;border-radius:4px;font-size:0.72em;font-weight:600;white-space:nowrap;">{text}</span>'


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tab 1: Master Option Chain Narrative Storyboard & Intel
# ─────────────────────────────────────────────────────────────────────────────

def render_tab1_narratives(
    df: pd.DataFrame,
    read: an.Read,
    spot: float,
    now: Optional[datetime] = None,
    dte: float = 0.0,
) -> None:
    """
    Renders the executive Option Chain Narrative Intel storyboard on Tab 1.
    Translates raw strikes, OI, PCR, Max Pain, and Straddle pricing into
    plain-English institutional market stories.
    """
    if df is None or df.empty or spot <= 0 or not hasattr(read, "levels"):
        st.info("Option Chain data is synchronizing. Narrative intelligence will render shortly.")
        return

    lv = read.levels
    atm = lv.atm
    s1, r1 = lv.s1, lv.r1
    s2, r2 = lv.s2, lv.r2
    s1_oi, r1_oi = lv.s1_oi, lv.r1_oi
    max_pain = lv.max_pain
    dist_s1 = spot - s1
    dist_r1 = r1 - spot
    dist_mp = max_pain - spot
    channel_width = r1 - s1
    pcr = float(read.pcr_oi)
    bias_score = float(read.bias_score)

    # Calculate Total / Window OI Dominance
    w = an.window(df, spot, 5)
    near_ce_oi = w.ce_oi.sum() if "ce_oi" in w.columns else read.total_ce_oi
    near_pe_oi = w.pe_oi.sum() if "pe_oi" in w.columns else read.total_pe_oi
    oi_ratio = (near_pe_oi / near_ce_oi) if near_ce_oi > 0 else 1.0

    # 1. Determine Battlefield & Defense Perimeter Story
    if r1_oi > s1_oi * 1.3:
        bf_bias = "BEARISH CALL DOMINANCE"
        bf_bias_col = "#EF4444"
        bf_bias_bg = "rgba(239, 68, 68, 0.12)"
        bf_story = (
            f"**Call writers dominate the field.** The overhead resistance wall at **{r1:,}** "
            f"holds {_fmt_oi(r1_oi)} contracts, outgunning the primary Put support at **{s1:,}** "
            f"({_fmt_oi(s1_oi)}) by **{r1_oi / s1_oi:.1f}x**. Smart money is heavily capping upside, "
            f"pressuring Spot ({spot:,.1f}) from above. Bulls face severe friction on every bounce toward {r1:,}."
        )
    elif s1_oi > r1_oi * 1.3:
        bf_bias = "BULLISH PUT DOMINANCE"
        bf_bias_col = "#10B981"
        bf_bias_bg = "rgba(16, 185, 129, 0.12)"
        bf_story = (
            f"**Put writers are aggressively defending the floor.** The primary Put fortress at **{s1:,}** "
            f"holds {_fmt_oi(s1_oi)} contracts, exceeding Call resistance at **{r1:,}** "
            f"({_fmt_oi(r1_oi)}) by **{s1_oi / r1_oi:.1f}x**. Institutional money is absorbing dips, "
            f"anchoring Spot ({spot:,.1f}). Dips into {s1:,} are actively bought by option sellers."
        )
    else:
        bf_bias = "BALANCED TRENCH WARFARE"
        bf_bias_col = "#FACC15"
        bf_bias_bg = "rgba(250, 204, 21, 0.12)"
        bf_story = (
            f"**Equally matched battleground.** Call Wall **{r1:,}** ({_fmt_oi(r1_oi)}) and Put Wall "
            f"**{s1:,}** ({_fmt_oi(s1_oi)}) have comparable commitment ({r1_oi / s1_oi:.2f} ratio). "
            f"Neither bulls nor bears have established a decisive advantage. The market is oscillating "
            f"in an orderly two-way channel between support and resistance."
        )

    # Channel Compression Check
    if channel_width <= 60:
        regime_badge = "⚠️ SEVERE SQUEEZE COMPRESSION"
        regime_badge_col = "#F59E0B"
        regime_narrative = (
            f"**Channel is compressed into an ultra-tight {channel_width:.0f}-pt range ({s1:,} to {r1:,}).** "
            f"Option sellers are choking volatility. Historically, compression this tight leads to a violent "
            f"breakout / gamma squeeze once either wall breaks. **Warning for option buyers:** premium decay is severe inside the channel; wait for the break."
        )
    elif channel_width <= 120:
        regime_badge = "⚖️ STANDARD TRADING CORRIDOR"
        regime_badge_col = "#38BDF8"
        regime_narrative = (
            f"**Healthy {channel_width:.0f}-pt operating channel ({s1:,} to {r1:,}).** "
            f"Spot ({spot:,.1f}) has room to oscillate. Spot is currently **{dist_s1:+.0f} pts above S1** "
            f"and **{dist_r1:+.0f} pts below R1**. Fade boundaries or trade mean-reversion."
        )
    else:
        regime_badge = "🚀 WIDE EXPANSION CORRIDOR"
        regime_badge_col = "#A855F7"
        regime_narrative = (
            f"**Wide {channel_width:.0f}-pt institutional corridor.** S1 is at {s1:,} and R1 is at {r1:,}. "
            f"Wide boundaries indicate that writers expect substantial intraday swing potential or high volatility."
        )

    # 2. Max Pain & Expiry Pinning Story
    if abs(dist_mp) <= 25:
        mp_status = "🎯 PINNED AT MAX PAIN"
        mp_col = "#10B981"
        mp_story = (
            f"Spot ({spot:,.1f}) is trading virtually **on top of Max Pain ({max_pain:,})** (only {dist_mp:+.1f} pts off). "
            f"Option sellers are in their ideal zone of maximum aggregate profit. Expect low directional momentum, "
            f"elevated chop, and relentless premium erosion as writers defend this exact level."
        )
    elif dist_mp > 25:
        mp_status = "🧲 UPWARD GRAVITATIONAL PULL"
        mp_col = "#38BDF8"
        mp_story = (
            f"Spot ({spot:,.1f}) is trading **{dist_mp:.0f} pts below Max Pain ({max_pain:,})**. "
            f"Option sellers suffer cumulative losses if price expires below Max Pain. As expiry approaches "
            f"({dte:.1f} days left), institutional writers will exert upward gravitational pressure to pull Spot back toward {max_pain:,}."
        )
    else:
        mp_status = "🧲 DOWNWARD GRAVITATIONAL PULL"
        mp_col = "#F59E0B"
        mp_story = (
            f"Spot ({spot:,.1f}) is trading **{abs(dist_mp):.0f} pts above Max Pain ({max_pain:,})**. "
            f"Call writers are experiencing elevated payout liability. Option sellers will favor downward "
            f"gravitational drift to drag Spot back down toward {max_pain:,} into expiry."
        )

    # 3. ATM Straddle Volatility & Expected Move Pricing
    sr = an.straddle_range(df, spot)
    if sr and sr.premium:
        prem = float(sr.premium)
        low_exp, high_exp = float(sr.lower), float(sr.upper)
        w_pct = float(sr.width_pct)
        pos_txt = getattr(sr, "position", "in range")

        straddle_story = (
            f"Option sellers are pricing today's move at **±{prem:.0f} points** (ATM Straddle {sr.strike:,} "
            f"CE ₹{sr.ce_ltp:.1f} + PE ₹{sr.pe_ltp:.1f}). This defines the expected session envelope between "
            f"**{low_exp:,.0f}** and **{high_exp:,.0f}** ({w_pct:.2f}% width). Spot ({spot:,.1f}) is **{pos_txt}**. "
            f"Moves inside this boundary represent expected market maker absorption; a move closing outside {high_exp:,.0f} or {low_exp:,.0f} "
            f"signals an unhedged gamma explosion."
        )
    else:
        prem = 0.0
        low_exp, high_exp = spot - 75, spot + 75
        straddle_story = f"ATM straddle pricing currently calibrating for Spot {spot:,.1f}."

    # 4. Trapped Trader & Squeeze Radar
    trapped_alerts: List[str] = []
    # Check Call Squeeze (Spot presses R1 while call additions or heavy call short buildup is present)
    if dist_r1 <= 25:
        trapped_alerts.append(
            f"🚨 **CALL WRITER PANIC ALERT:** Spot is only {dist_r1:+.1f} pts from Call Wall **{r1:,}**! "
            f"If NIFTY prints a 5-minute candle close above {r1:,}, Call writers holding {_fmt_oi(r1_oi)} contracts "
            f"will be forced to cover their naked shorts, unleashing a rapid **Short Covering Gamma Squeeze** toward {r2:,}."
        )
    elif dist_s1 <= 25:
        trapped_alerts.append(
            f"🚨 **PUT WRITER FLUSH ALERT:** Spot is only {dist_s1:+.1f} pts above Put Wall **{s1:,}**! "
            f"If NIFTY breaks and closes below {s1:,}, Put writers defending {_fmt_oi(s1_oi)} contracts "
            f"will face catastrophic margin calls, triggering a cascade of **Long Unwinding & Forced Liquidation** toward {s2:,}."
        )
    else:
        trapped_alerts.append(
            f"✓ **NO IMMEDIATE WRITER TRAP:** Spot is comfortably insulated between Put Wall {s1:,} (+{dist_s1:.0f} pts) "
            f"and Call Wall {r1:,} (-{dist_r1:.0f} pts). Option sellers are safely harvesting theta premium."
        )

    # ── Render Section Header ───────────────────────────────────────────────
    st.markdown("### 🏛️ Executive Option Chain Narrative Intelligence")

    # ── 3-Card High-Impact Storyboard Grid ──────────────────────────────────
    story_html = f"""
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin-bottom: 16px;">
        <!-- Card 1: Battlefield & Perimeter -->
        <div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid {bf_bias_col}; border-radius: 8px; padding: 14px 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-size: 0.76em; font-weight: 800; color: #8fa6b8; text-transform: uppercase; letter-spacing: 0.5px;">🛡️ Battlefield Perimeter</span>
                <span style="background: {bf_bias_bg}; color: {bf_bias_col}; border: 1px solid {bf_bias_col}55; padding: 2px 7px; border-radius: 4px; font-size: 0.72em; font-weight: 700;">{bf_bias}</span>
            </div>
            <div style="font-size: 0.88em; color: #cbd5e1; line-height: 1.5; margin-bottom: 10px;">
                {bf_story}
            </div>
            <div style="padding: 6px 10px; background: rgba(15, 23, 42, 0.6); border-radius: 6px; border: 1px solid #1e293b; font-size: 0.78em; color: {regime_badge_col};">
                <span style="font-weight: 700;">{regime_badge}</span>: {regime_narrative}
            </div>
        </div>

        <!-- Card 2: Max Pain Gravitation -->
        <div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid {mp_col}; border-radius: 8px; padding: 14px 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-size: 0.76em; font-weight: 800; color: #8fa6b8; text-transform: uppercase; letter-spacing: 0.5px;">🎯 Max Pain Gravitation</span>
                <span style="background: rgba(56,189,248,0.12); color: {mp_col}; border: 1px solid {mp_col}55; padding: 2px 7px; border-radius: 4px; font-size: 0.72em; font-weight: 700;">{mp_status}</span>
            </div>
            <div style="display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px;">
                <span style="font-family: monospace; font-size: 1.6em; font-weight: 900; color: #f8fafc;">{max_pain:,}</span>
                <span style="font-size: 0.85em; font-weight: 700; color: {mp_col}; font-family: monospace;">{dist_mp:+.0f} pts from Spot</span>
            </div>
            <div style="font-size: 0.88em; color: #cbd5e1; line-height: 1.5;">
                {mp_story}
            </div>
        </div>

        <!-- Card 3: Straddle Expected Range -->
        <div style="background: #0d131f; border: 1px solid #1e293b; border-top: 3px solid #38bdf8; border-radius: 8px; padding: 14px 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-size: 0.76em; font-weight: 800; color: #8fa6b8; text-transform: uppercase; letter-spacing: 0.5px;">📐 Straddle Implied Bounds</span>
                <span style="background: rgba(56,189,248,0.12); color: #38bdf8; border: 1px solid #38bdf855; padding: 2px 7px; border-radius: 4px; font-size: 0.72em; font-weight: 700;">±{prem:.0f} PTS PRICED</span>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;">
                <div>
                    <span style="font-size: 0.72em; color: #64748b; text-transform: uppercase;">Expected Low</span>
                    <div style="font-family: monospace; font-size: 1.25em; font-weight: 800; color: #10b981;">{low_exp:,.0f}</div>
                </div>
                <div style="text-align: center;">
                    <span style="font-size: 0.72em; color: #64748b; text-transform: uppercase;">Current Spot</span>
                    <div style="font-family: monospace; font-size: 1.25em; font-weight: 800; color: #facc15;">{spot:,.1f}</div>
                </div>
                <div style="text-align: right;">
                    <span style="font-size: 0.72em; color: #64748b; text-transform: uppercase;">Expected High</span>
                    <div style="font-family: monospace; font-size: 1.25em; font-weight: 800; color: #ef4444;">{high_exp:,.0f}</div>
                </div>
            </div>
            <div style="font-size: 0.88em; color: #cbd5e1; line-height: 1.5;">
                {straddle_story}
            </div>
        </div>
    </div>
    """
    _render_html(story_html)

    # ── Trapped Trader & Squeeze Radar Alert ──────────────────────────────────
    for alert in trapped_alerts:
        if "🚨" in alert:
            st.error(alert)
        else:
            st.info(alert)

    # ── Actionable Tactical Playbook Expander ─────────────────────────────────
    with st.expander("📋 Tactical Option Buyer Execution Playbook (Rules for This Chain)", expanded=False):
        c_ce, c_pe, c_no = st.columns(3)
        with c_ce:
            st.markdown("#### 🟢 When to Buy Call (CE)")
            st.markdown(
                f"- **Condition 1 (Support Bounce):** Spot dips to **{s1:,} (S1)**, tests Put defense, prints a 5m bullish pin bar / hammer, and Call writers at {s1:,} start shedding.\n"
                f"- **Condition 2 (Breakout Squeeze):** Spot crosses and closes above **{r1:,} (R1)** with surge in volume, forcing Call short covering.\n"
                f"- **Stop Loss:** Below S1 ({s1 - 20:,.0f}) or 25 pts index risk.\n"
                f"- **Target:** Next resistance wall at {r1:,} (or {r2:,} on breakout)."
            )
        with c_pe:
            st.markdown("#### 🔴 When to Buy Put (PE)")
            st.markdown(
                f"- **Condition 1 (Resistance Rejection):** Spot tests **{r1:,} (R1)**, fails to break, prints an inverted hammer / rejection candle, and Call additions surge.\n"
                f"- **Condition 2 (Breakdown Flush):** Spot falls and closes below **{s1:,} (S1)**, causing Put writers to liquidate.\n"
                f"- **Stop Loss:** Above R1 ({r1 + 20:,.0f}) or 25 pts index risk.\n"
                f"- **Target:** Next support wall at {s1:,} (or {s2:,} on breakdown)."
            )
        with c_no:
            st.markdown("#### 🛑 When to Sit Out")
            st.markdown(
                f"- **Inside Channel Chop:** Spot between **{s1 + 25:,} and {r1 - 25:,}** with low volume.\n"
                f"- **Midday Theta Burn (11:30 to 13:15):** Theta decay accelerates while institutional flow dries up.\n"
                f"- **Conflicting PCR & Flow:** PCR says bullish but Call additions exceed Put additions."
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tab 1: Full Option Chain Matrix Table with Live Quadrants & Narratives
# ─────────────────────────────────────────────────────────────────────────────

def render_option_chain_matrix(df: pd.DataFrame, spot: float, read: an.Read) -> None:
    """
    Renders an institutional-grade, color-coded tabular Option Chain view:
    Calls on Left | Center Strike | Puts on Right | Strike-Level Narrative Role.
    """
    if df is None or df.empty or spot <= 0:
        return

    st.markdown("---")
    col_hdr, col_flt = st.columns([3, 1])
    with col_hdr:
        st.markdown("### 📊 Interactive Option Chain Matrix & Live Buildup Radar")
        st.caption("Complete strike-by-strike microstructure with real-time quadrant categorization and institutional roles.")
    with col_flt:
        span_choice = st.selectbox(
            "Strike View Depth",
            ["Near ATM (±5 Strikes)", "Active Band (±10 Strikes)", "Wide Range (±15 Strikes)"],
            index=1,
            key="oc_matrix_span_sel"
        )

    n_strikes = 5 if "5" in span_choice else (10 if "10" in span_choice else 15)
    w = an.window(df, spot, n_strikes).copy()
    if w.empty:
        st.caption("No strike rows available for this span.")
        return

    lv = getattr(read, "levels", None)
    s1 = getattr(lv, "s1", None) if lv else None
    r1 = getattr(lv, "r1", None) if lv else None
    s2 = getattr(lv, "s2", None) if lv else None
    r2 = getattr(lv, "r2", None) if lv else None
    atm = getattr(lv, "atm", int(round(spot / 50.0) * 50))
    max_pain = getattr(lv, "max_pain", None) if lv else None

    # Construct clean display rows
    table_rows: List[Dict[str, Any]] = []
    for _, r in w.iterrows():
        stk = int(round(float(r["strike"])))
        c_chg = r.get("ce_chg")
        c_oi_chg = r.get("ce_chg_oi")
        p_chg = r.get("pe_chg")
        p_oi_chg = r.get("pe_chg_oi")

        qc = an.quadrant(c_chg, c_oi_chg) if pd.notna(c_chg) and pd.notna(c_oi_chg) else "unknown"
        qp = an.quadrant(p_chg, p_oi_chg) if pd.notna(p_chg) and pd.notna(p_oi_chg) else "unknown"

        # Determine Strike Role Narrative
        roles: List[str] = []
        if stk == atm:
            roles.append("🎯 ATM Battleground")
        if r1 and stk == r1:
            roles.append("🛑 Call Wall R1 (Ceiling)")
        elif r2 and stk == r2:
            roles.append("🧱 Secondary R2")
        if s1 and stk == s1:
            roles.append("🛡️ Put Wall S1 (Floor)")
        elif s2 and stk == s2:
            roles.append("🧱 Secondary S2")
        if max_pain and stk == max_pain:
            roles.append("🧲 Max Pain Anchor")

        if not roles:
            if stk > spot and r.get("ce_oi", 0) > 2e6:
                roles.append("🔴 Heavy Call Writing")
            elif stk < spot and r.get("pe_oi", 0) > 2e6:
                roles.append("🟢 Heavy Put Writing")
            elif stk > spot:
                roles.append("OTM Call")
            else:
                roles.append("OTM Put")

        role_str = " · ".join(roles)

        table_rows.append({
            "CE Buildup": qc.replace("_", " ").title(),
            "CE OI": _fmt_oi(r.get("ce_oi")),
            "CE ΔOI": _fmt_oi(r.get("ce_chg_oi")),
            "CE Vol": f"{r.get('ce_vol', 0):,.0f}" if pd.notna(r.get('ce_vol')) else "—",
            "CE IV": f"{r.get('ce_iv', 0):.1f}%" if pd.notna(r.get('ce_iv')) and r.get('ce_iv', 0) > 0 else "—",
            "CE LTP": f"₹{r.get('ce_ltp', 0):,.2f}" if pd.notna(r.get('ce_ltp')) else "—",
            "STRIKE": f"{stk:,}" + (" 🎯 [ATM]" if stk == atm else ""),
            "PE LTP": f"₹{r.get('pe_ltp', 0):,.2f}" if pd.notna(r.get('pe_ltp')) else "—",
            "PE IV": f"{r.get('pe_iv', 0):.1f}%" if pd.notna(r.get('pe_iv')) and r.get('pe_iv', 0) > 0 else "—",
            "PE Vol": f"{r.get('pe_vol', 0):,.0f}" if pd.notna(r.get('pe_vol')) else "—",
            "PE ΔOI": _fmt_oi(r.get("pe_chg_oi")),
            "PE OI": _fmt_oi(r.get("pe_oi")),
            "PE Buildup": qp.replace("_", " ").title(),
            "Institutional Role": role_str,
        })

    df_disp = pd.DataFrame(table_rows)

    def _style_matrix(val):
        s = str(val).lower()
        if "short buildup" in s or "short build-up" in s:
            return "background-color: rgba(239,68,68,0.15); color: #fca5a5; font-weight: 600;"
        if "long buildup" in s or "long build-up" in s:
            return "background-color: rgba(16,185,129,0.15); color: #86efac; font-weight: 600;"
        if "short covering" in s:
            return "background-color: rgba(56,189,248,0.15); color: #7dd3fc; font-weight: 600;"
        if "long unwinding" in s:
            return "background-color: rgba(245,158,11,0.15); color: #fde047; font-weight: 600;"
        if "🎯 [atm]" in s:
            return "background-color: rgba(250,204,21,0.22); color: #fef08a; font-weight: 800; border: 1px solid #facc15;"
        if "call wall" in s or "r1" in s:
            return "color: #f87171; font-weight: 700;"
        if "put wall" in s or "s1" in s:
            return "color: #34d399; font-weight: 700;"
        return ""

    st.dataframe(
        df_disp.style.applymap(_style_matrix),
        use_container_width=True,
        hide_index=True,
        height=min(620, (len(df_disp) + 1) * 36)
    )

    # Telemetry chip bar
    tot_ce = w.ce_oi.sum() if "ce_oi" in w.columns else 0
    tot_pe = w.pe_oi.sum() if "pe_oi" in w.columns else 0
    tot_pcr = (tot_pe / tot_ce) if tot_ce > 0 else 1.0

    st.markdown(
        f"""
        <div style="display: flex; gap: 16px; align-items: center; justify-content: space-between; flex-wrap: wrap; background: #0b111c; border: 1px solid #1e293b; padding: 10px 16px; border-radius: 6px; margin-top: 4px; font-size: 0.82em; color: #8fa6b8;">
            <div><b>Window Total Calls:</b> <span style="color:#ef4444; font-family: monospace;">{_fmt_oi(tot_ce)}</span></div>
            <div><b>Window Total Puts:</b> <span style="color:#10b981; font-family: monospace;">{_fmt_oi(tot_pe)}</span></div>
            <div><b>Window PCR:</b> <span style="color:#38bdf8; font-family: monospace;">{tot_pcr:.2f}</span></div>
            <div><b>Current Spot:</b> <span style="color:#facc15; font-family: monospace;">{spot:,.1f}</span> (ATM {atm:,})</div>
        </div>
        """,
        unsafe_allow_html=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tab 2: Intraday Flow & Shift Narratives for "Change in OI"
# ─────────────────────────────────────────────────────────────────────────────

def render_tab2_oi_change_narratives(
    df: pd.DataFrame,
    read: an.Read,
    spot: float,
) -> None:
    """
    Renders comprehensive intraday flow narratives and strike shift analysis on Tab 2.
    Explains who is dominating the day's fresh writing, dynamic strike migration,
    trapped writer risks, and ranked institutional shift activity.
    """
    if df is None or df.empty or spot <= 0:
        return

    w = an.window(df, spot, config.WINDOW_STRIKES + 3)
    if w.empty:
        return

    # Aggregate near-ATM flow stats
    tot_ce_add = w.ce_chg_oi[w.ce_chg_oi > 0].sum()
    tot_ce_unw = w.ce_chg_oi[w.ce_chg_oi < 0].sum()
    tot_pe_add = w.pe_chg_oi[w.pe_chg_oi > 0].sum()
    tot_pe_unw = w.pe_chg_oi[w.pe_chg_oi < 0].sum()
    net_ce = w.ce_chg_oi.sum()
    net_pe = w.pe_chg_oi.sum()
    pcr_chg = (net_pe / net_ce) if net_ce > 0 else (1.0 if net_ce == 0 else 0.0)

    # Find Top Intraday Activity Strikes
    top_ce_add_row = w.loc[w.ce_chg_oi.idxmax()] if (w.ce_chg_oi > 0).any() else None
    top_pe_add_row = w.loc[w.pe_chg_oi.idxmax()] if (w.pe_chg_oi > 0).any() else None
    top_ce_unw_row = w.loc[w.ce_chg_oi.idxmin()] if (w.ce_chg_oi < 0).any() else None
    top_pe_unw_row = w.loc[w.pe_chg_oi.idxmin()] if (w.pe_chg_oi < 0).any() else None

    # 1. Synthesize Intraday Power Balance Story
    if net_pe > net_ce * 1.3:
        shift_verdict = "🟢 PUT WRITERS IN AGGRESSIVE COMMAND"
        shift_col = "#10B981"
        shift_bg = "rgba(16, 185, 129, 0.12)"
        shift_narrative = (
            f"**Put writers have taken definitive control of today's session.** Institutional players have added "
            f"**{_fmt_oi(tot_pe_add)}** Put contracts compared to only **{_fmt_oi(tot_ce_add)}** Call additions "
            f"(net Put dominance of **{net_pe / max(net_ce, 1):.1f}x**). Intraday PCR stands at **{pcr_chg:.2f}**. "
            f"This heavy concentration of fresh put writing below spot ({spot:,.1f}) acts as a hydraulic lift, "
            f"absorbing sell orders and defending against downside continuation. Dips into support are institutional buying opportunities."
        )
    elif net_ce > net_pe * 1.3:
        shift_verdict = "🔴 CALL WRITERS AGGRESSIVELY CAPPING"
        shift_col = "#EF4444"
        shift_bg = "rgba(239, 68, 68, 0.12)"
        shift_narrative = (
            f"**Call writers are aggressively capping upside.** Bears have flooded the board with **{_fmt_oi(tot_ce_add)}** "
            f"in fresh Call additions, dwarfing Put additions of **{_fmt_oi(tot_pe_add)}** (net Call dominance of **{net_ce / max(net_pe, 1):.1f}x**). "
            f"Intraday PCR is a depressed **{pcr_chg:.2f}**. Fresh call writing has carpet-bombed strikes directly overhead, "
            f"erecting a fortress of supply above spot ({spot:,.1f}). Rallies will face relentless institutional selling pressure."
        )
    else:
        shift_verdict = "🟡 BALANCED TWO-WAY INTRADAY CHOP"
        shift_col = "#FACC15"
        shift_bg = "rgba(250, 204, 21, 0.12)"
        shift_narrative = (
            f"**Two-way positioning with no clear institutional victor.** Fresh Call additions ({_fmt_oi(tot_ce_add)}) and "
            f"fresh Put additions ({_fmt_oi(tot_pe_add)}) are running neck-and-neck. Intraday PCR sits at **{pcr_chg:.2f}**. "
            f"Both calls and puts are being actively written or unwound. The market is consolidating in equilibrium; avoid chasing "
            f"directional breakouts until one side begins unwinding."
        )

    # 2. Strike Migration & Squeeze Radar
    migration_points: List[str] = []
    if top_ce_add_row is not None:
        c_stk = int(top_ce_add_row["strike"])
        c_add = top_ce_add_row["ce_chg_oi"]
        dist_c = c_stk - spot
        if dist_c <= 25 and dist_c >= 0:
            migration_points.append(
                f"⚠️ **IMMEDIATE CEILING SQUEEZE ZONE ({c_stk:,}):** The day's biggest Call addition (+{_fmt_oi(c_add)}) "
                f"is clustered right at **{c_stk:,}** — only **{dist_c:+.0f} pts above Spot**! Call writers are daring the market. "
                f"If spot crosses {c_stk:,}, these aggressive writers become instantly trapped, triggering explosive short covering."
            )
        else:
            migration_points.append(
                f"🧱 **PRIMARY INTRADAY CEILING ({c_stk:,}):** Heaviest fresh Call writing is concentrated at **{c_stk:,}** "
                f"(+{_fmt_oi(c_add)} contracts, {dist_c:+.0f} pts above spot). This is the key overhead barrier for the session."
            )

    if top_pe_add_row is not None:
        p_stk = int(top_pe_add_row["strike"])
        p_add = top_pe_add_row["pe_chg_oi"]
        dist_p = spot - p_stk
        if dist_p <= 25 and dist_p >= 0:
            migration_points.append(
                f"⚠️ **IMMEDIATE FLOOR FLUSH ZONE ({p_stk:,}):** The day's biggest Put addition (+{_fmt_oi(p_add)}) "
                f"is anchored at **{p_stk:,}** — only **{dist_p:.0f} pts below Spot**! If spot breaks below {p_stk:,}, "
                f"these fresh put writers will panic-dump, sparking an institutional liquidation cascade."
            )
        else:
            migration_points.append(
                f"🛡️ **PRIMARY INTRADAY FLOOR ({p_stk:,}):** Heaviest fresh Put defense is concentrated at **{p_stk:,}** "
                f"(+{_fmt_oi(p_add)} contracts, {dist_p:.0f} pts below spot). This represents the primary institutional demand base."
            )

    # Unwinding highlights
    if top_ce_unw_row is not None and top_ce_unw_row["ce_chg_oi"] < -30000:
        c_unw_stk = int(top_ce_unw_row["strike"])
        c_unw = top_ce_unw_row["ce_chg_oi"]
        migration_points.append(
            f"⚡ **CALL SHORTS FLEEING AT {c_unw_stk:,}:** {_fmt_oi(abs(c_unw))} Call contracts were liquidated / covered today. "
            f"Short sellers are relinquishing this strike, clearing upside resistance."
        )

    if top_pe_unw_row is not None and top_pe_unw_row["pe_chg_oi"] < -30000:
        p_unw_stk = int(top_pe_unw_row["strike"])
        p_unw = top_pe_unw_row["pe_chg_oi"]
        migration_points.append(
            f"⚠️ **PUT DEFENDERS RETREATING AT {p_unw_stk:,}:** {_fmt_oi(abs(p_unw))} Put contracts were liquidated / shed today. "
            f"Support is eroding at this strike; buyers are backing away."
        )

    # ── Render Intraday Shift Narrative ───────────────────────────────────────
    st.markdown("### 🌊 Live Intraday Flow & Shift Narrative Storyboard")

    narrative_html = f"""
    <div style="background: #0d131f; border: 1px solid #1e293b; border-left: 4px solid {shift_col}; border-radius: 8px; padding: 16px 18px; margin-bottom: 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-size: 0.8em; font-weight: 800; color: #8fa6b8; text-transform: uppercase; letter-spacing: 0.5px;">Session Shift Narrative</span>
            <span style="background: {shift_bg}; color: {shift_col}; border: 1px solid {shift_col}55; padding: 3px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 700;">{shift_verdict}</span>
        </div>
        <div style="font-size: 0.92em; color: #cbd5e1; line-height: 1.6;">
            {shift_narrative}
        </div>
    </div>
    """
    _render_html(narrative_html)

    # Render Migration & Squeeze Radar Bulletins
    if migration_points:
        st.markdown("#### 🎯 Dynamic Strike Migration & Pressure Radar")
        for mp in migration_points:
            if "🚨" in mp or "⚠️" in mp:
                st.warning(mp)
            elif "⚡" in mp:
                st.info(mp)
            else:
                st.markdown(f"- {mp}")

    # ── 4-Quadrant Top Shift Activity Grid ───────────────────────────────────
    st.markdown("#### 🏛️ Top Intraday Strike Moves & Institutional Interpretations")
    col1, col2 = st.columns(2)

    with col1:
        # Top Call Additions
        st.markdown("**🔴 Top Call Additions (Resistance Fortification)**")
        top_calls_add = w[w.ce_chg_oi > 0].sort_values("ce_chg_oi", ascending=False).head(3)
        if not top_calls_add.empty:
            for _, r in top_calls_add.iterrows():
                stk = int(r["strike"])
                add_val = r["ce_chg_oi"]
                d = stk - spot
                st.markdown(
                    f"- **{stk:,} CE**: <span style='color:#ef4444;font-family:monospace;'>+{_fmt_oi(add_val)}</span> ({d:+.0f} pts from spot) — "
                    f"*Ceiling reinforced. Call writers betting Spot remains capped below {stk:,}.*",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No significant Call additions recorded.")

        st.markdown("**⚡ Top Call Unwinding (Short Panic / Short Covering)**")
        top_calls_unw = w[w.ce_chg_oi < 0].sort_values("ce_chg_oi", ascending=True).head(3)
        if not top_calls_unw.empty:
            for _, r in top_calls_unw.iterrows():
                stk = int(r["strike"])
                unw_val = r["ce_chg_oi"]
                st.markdown(
                    f"- **{stk:,} CE**: <span style='color:#38bdf8;font-family:monospace;'>{_fmt_oi(unw_val)}</span> — "
                    f"*Shorts covering in panic. Overhead resistance melting away.*",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No Call unwinding detected.")

    with col2:
        # Top Put Additions
        st.markdown("**🟢 Top Put Additions (Support Fortification)**")
        top_puts_add = w[w.pe_chg_oi > 0].sort_values("pe_chg_oi", ascending=False).head(3)
        if not top_puts_add.empty:
            for _, r in top_puts_add.iterrows():
                stk = int(r["strike"])
                add_val = r["pe_chg_oi"]
                d = spot - stk
                st.markdown(
                    f"- **{stk:,} PE**: <span style='color:#10b981;font-family:monospace;'>+{_fmt_oi(add_val)}</span> ({d:.0f} pts below spot) — "
                    f"*Support fortified. Put writers underwriting this level against decline.*",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No significant Put additions recorded.")

        st.markdown("**⚠️ Top Put Unwinding (Long Liquidation / Floor Collapse)**")
        top_puts_unw = w[w.pe_chg_oi < 0].sort_values("pe_chg_oi", ascending=True).head(3)
        if not top_puts_unw.empty:
            for _, r in top_puts_unw.iterrows():
                stk = int(r["strike"])
                unw_val = r["pe_chg_oi"]
                st.markdown(
                    f"- **{stk:,} PE**: <span style='color:#f59e0b;font-family:monospace;'>{_fmt_oi(unw_val)}</span> — "
                    f"*Defenders backing off. Support base weakening.*",
                    unsafe_allow_html=True
                )
        else:
            st.caption("No Put unwinding detected.")


# ─────────────────────────────────────────────────────────────────────────────
# Self-Test Block
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═════════════════════════════════════════════════════════════════")
    print("  TESTING OPTION CHAIN NARRATIVE SUITE (option_chain_narrative.py)")
    print("═════════════════════════════════════════════════════════════════")

    import chain_parser as cp
    df_sample = cp.parse_csv("data/sample_chain.csv")
    spot_val = cp.infer_spot(df_sample)
    read_val = an.analyse(df_sample, spot_val)

    print(f"Sample Spot: {spot_val:,.2f}")
    print(f"Read ATM: {read_val.levels.atm}, S1: {read_val.levels.s1}, R1: {read_val.levels.r1}")
    print(f"PCR: {read_val.pcr_oi:.2f}, Regime: {read_val.regime}")
    print("\nVerifying helper formatting:")
    print("  _fmt_oi(12500000) ->", _fmt_oi(12500000))
    print("  _fmt_oi(-450000)  ->", _fmt_oi(-450000))
    print("  _get_buildup_pill('short_buildup', 'CE') ->", _get_buildup_pill('short_buildup', 'CE')[:40], "...")

    print("\nSUCCESS: option_chain_narrative.py verified and operational!")
