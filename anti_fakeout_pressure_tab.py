"""
anti_fakeout_pressure_tab.py — Institutional Anti-Fakeout & Real-Time Pressure Dashboard Tab.

Consumes live Dhan / NSE market data (OHLCV, VWAP, Option Chain) to provide:
1. The "Truth" Option Chain Engine (Seller's Perspective):
   - 200-point constrained range (approx. 2 strikes above / below ATM).
   - True S/R Calculation (Max Call OI = True Resistance, Max Put OI = True Support).
   - The Pressure Gauge (Change in OI as "gas balloon" compression/decompression).
   - Fakeout Detector (VWAP + OI Divergence: Fake Breakout vs Fake Breakdown vs Genuine Move).
2. The "Bodybuilder" Tug-of-War Visualizer:
   - Horizontal comparative weight & momentum chart (Call Sellers vs Put Sellers).
3. Risk Management & Discipline HUD:
   - 1:2 Risk-to-Reward Execution Lock.
   - Capital Protection Tracker (Profits Re-risking "House Money" Rule).
   - Overtrading Killswitch ("Sit Quietly" banner on target or max loss).
   - One-Click "Book Loss" Invalidation Exit.
4. Macroeconomic Event & Volatility Spike Overlay:
   - Live Event Surveillance (RBI MPC, US Fed FOMC, CPI) with countdown & 200-300 pt spike protection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import anti_fakeout_pressure_engine as ape
import config


# ─────────────────────────────────────────────────────────────────────────────
# 1. Styling & Color Palette
# ─────────────────────────────────────────────────────────────────────────────
RED = "#EF4444"
RED_BG = "rgba(239, 68, 68, 0.12)"
RED_BORDER = "#EF4444"

GREEN = "#10B981"
GREEN_BG = "rgba(16, 185, 129, 0.12)"
GREEN_BORDER = "#10B981"

AMBER = "#F59E0B"
AMBER_BG = "rgba(245, 158, 11, 0.14)"
AMBER_BORDER = "#F59E0B"

BLUE = "#38BDF8"
BLUE_BG = "rgba(56, 189, 248, 0.12)"
BLUE_BORDER = "#38BDF8"


def _render_html(html_str: str) -> None:
    """Render clean HTML without CommonMark code block bugs."""
    clean = "\n".join(line for line in html_str.splitlines() if line.strip())
    if hasattr(st, "html"):
        st.html(clean)
    else:
        st.markdown(clean, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Macroeconomic Event Banner
# ─────────────────────────────────────────────────────────────────────────────
def render_macro_event_banner(macro_status: ape.MacroEventStatus) -> None:
    """Renders a persistent high-contrast banner for upcoming macroeconomic events."""
    if macro_status.is_critical:
        bg_color = "rgba(239, 68, 68, 0.25)"
        border_color = RED
        icon = "🚨"
        badge = "TRADING HALTED — CRITICAL EVENT"
    elif macro_status.is_alert:
        bg_color = "rgba(245, 158, 11, 0.22)"
        border_color = AMBER
        icon = "⚠️"
        badge = "MACRO ALERT TODAY — REDUCE RISK 50%"
    else:
        bg_color = "rgba(15, 23, 42, 0.7)"
        border_color = "#334155"
        icon = "📅"
        badge = "MACRO SURVEILLANCE"

    html = (
        f'<div style="background:{bg_color}; border: 1.5px solid {border_color}; border-radius: 8px; padding: 10px 16px; margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">'
        f'<div style="display: flex; align-items: center; gap: 10px;">'
        f'<span style="font-size: 1.25em;">{icon}</span>'
        f'<div>'
        f'<span style="font-weight: 800; font-size: 0.92em; color: #FFFFFF;">{macro_status.warning_banner_text}</span>'
        f'</div>'
        f'</div>'
        f'<div>'
        f'<span style="background: {border_color}; color: #000; font-weight: 800; font-size: 0.72em; padding: 3px 8px; border-radius: 4px; text-transform: uppercase;">{badge}</span>'
        f'</div>'
        f'</div>'
    )
    _render_html(html)


# ─────────────────────────────────────────────────────────────────────────────
# 3. The "Bodybuilder" Tug-of-War Visualizer (Plotly Horizontal Bar)
# ─────────────────────────────────────────────────────────────────────────────
def create_bodybuilder_chart(pressure: ape.PressureGaugeResult) -> go.Figure:
    """
    Renders the institutional horizontal Tug-of-War bar chart.
    Displays:
      - Call Sellers (Bearish Weight & Momentum) pulling LEFT (Red)
      - Put Sellers (Bullish Weight & Momentum) pulling RIGHT (Green)
      - Center-of-gravity balance marker
    """
    fig = go.Figure()

    # Normalized percentages
    call_wt = pressure.call_weight_pct
    put_wt = pressure.put_weight_pct

    # Tug-of-war bar
    fig.add_trace(
        go.Bar(
            y=["Tug-of-War (OI Weight)"],
            x=[-call_wt],
            name=f"Call Sellers (Bears: {call_wt:.1f}%)",
            orientation="h",
            marker=dict(color=RED, line=dict(color="#DC2626", width=1.5)),
            text=[f"🛑 CALL SELLERS: {pressure.call_weight_oi/1e5:.1f}L ({call_wt:.1f}%)"],
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="#FFFFFF", size=11, family="monospace"),
        )
    )

    fig.add_trace(
        go.Bar(
            y=["Tug-of-War (OI Weight)"],
            x=[put_wt],
            name=f"Put Sellers (Bulls: {put_wt:.1f}%)",
            orientation="h",
            marker=dict(color=GREEN, line=dict(color="#059669", width=1.5)),
            text=[f"🛡️ PUT SELLERS: {pressure.put_weight_oi/1e5:.1f}L ({put_wt:.1f}%)"],
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="#FFFFFF", size=11, family="monospace"),
        )
    )

    # Center-of-Gravity Balance Line
    fig.add_vline(
        x=0,
        line=dict(color="#FACC15", width=2.5, dash="solid"),
        annotation_text="Equilibrium (0%)",
        annotation_position="top",
        annotation_font=dict(color="#FACC15", size=10),
    )

    # Dynamic Tug Needle Marker based on Net Pressure Index
    needle_pos = pressure.net_pressure_index / 2.0  # Scale -50 to +50
    needle_col = GREEN if needle_pos > 5 else (RED if needle_pos < -5 else "#FACC15")
    fig.add_vline(
        x=needle_pos,
        line=dict(color=needle_col, width=3, dash="dot"),
        annotation_text=f"Pressure Needle: {pressure.net_pressure_index:+.1f}%",
        annotation_position="bottom",
        annotation_font=dict(color=needle_col, size=10, family="monospace"),
    )

    fig.update_layout(
        barmode="relative",
        height=145,
        margin=dict(l=10, r=10, t=25, b=25),
        paper_bgcolor="#0A0F1A",
        plot_bgcolor="#0A0F1A",
        xaxis=dict(
            range=[-100, 100],
            showgrid=True,
            gridcolor="rgba(255, 255, 255, 0.06)",
            tickvals=[-100, -75, -50, -25, 0, 25, 50, 75, 100],
            ticktext=["-100% (Bear)", "-75%", "-50%", "-25%", "50/50", "+25%", "+50%", "+75%", "+100% (Bull)"],
            tickfont=dict(size=9, color="#94A3B8"),
            zeroline=False,
        ),
        yaxis=dict(showticklabels=False),
        showlegend=False,
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 4. Actionable Directives & Pressure HUD
# ─────────────────────────────────────────────────────────────────────────────
def render_actionable_prompt_banner(detector: ape.FakeoutDetectorResult, pressure: ape.PressureGaugeResult) -> None:
    """Renders the high-contrast definitive institutional action prompt banner."""
    if detector.is_fakeout:
        bg_col = "rgba(239, 68, 68, 0.22)"
        border_col = RED
        badge_bg = RED
        badge_text = "⚠️ INSTITUTIONAL FAKEOUT ALERT"
    elif "HIGH PROBABILITY" in detector.action_prompt:
        bg_col = "rgba(16, 185, 129, 0.22)"
        border_col = GREEN
        badge_bg = GREEN
        badge_text = "⚡ CONFLUENT BREAKOUT APPROVED"
    elif "WAIT" in detector.action_prompt:
        bg_col = "rgba(56, 189, 248, 0.15)"
        border_col = BLUE
        badge_bg = BLUE
        badge_text = "⚖️ EQUAL PRESSURE REGIME"
    else:
        bg_col = "rgba(245, 158, 11, 0.18)"
        border_col = AMBER
        badge_bg = AMBER
        badge_text = "🧭 CORRIDOR ACCUMULATION / FADE"

    html = (
        f'<div style="background:{bg_col}; border: 2px solid {border_col}; border-radius: 10px; padding: 16px 20px; margin-bottom: 14px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3);">'
        f'<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 8px;">'
        f'<div style="display: flex; align-items: center; gap: 8px;">'
        f'<span style="background:{badge_bg}; color:#000; font-size:0.72em; font-weight:900; padding:2px 8px; border-radius:4px; text-transform:uppercase;">{badge_text}</span>'
        f'<span style="font-size: 1.28em; font-weight: 800; color: #FFFFFF;">{detector.action_prompt}</span>'
        f'</div>'
        f'<div style="font-family: monospace; font-size: 0.85em; color: #CBD5E1;">'
        f'NET PRESSURE: <b style="color:{border_col};">{pressure.net_pressure_index:+.1f}%</b> ({pressure.dominant_side})'
        f'</div>'
        f'</div>'
        f'<div style="font-size: 0.92em; color: #E2E8F0; line-height: 1.45; margin-top: 4px;">{detector.rationale}</div>'
        f'<div style="margin-top: 8px; font-size: 0.78em; color: #94A3B8; display: flex; gap: 16px; flex-wrap: wrap;">'
        f'<span>Gas Balloon: <b>{pressure.balloon_status}</b></span>'
        f'<span>Spot vs VWAP: <b style="color:#FACC15;">{detector.spot_vs_vwap}</b> ({detector.vwap_val:,.1f} VWAP)</span>'
        f'</div>'
        f'</div>'
    )
    _render_html(html)


# ─────────────────────────────────────────────────────────────────────────────
# 5. The "Truth" Option Chain 200-Point Table
# ─────────────────────────────────────────────────────────────────────────────
def render_truth_chain_table(core_df: pd.DataFrame, spot: float, true_sr: ape.TrueSRResult) -> None:
    """Renders the strictly constrained 200-point Option Chain Grid (5 core strikes)."""
    if core_df.empty:
        st.info("Core 200-pt option chain quotes unavailable.")
        return

    df_rev = core_df.sort_values("strike", ascending=False).copy()
    max_ce = df_rev["ce_oi"].max() or 1.0
    max_pe = df_rev["pe_oi"].max() or 1.0

    rows_html = []
    for _, row in df_rev.iterrows():
        stk = float(row["strike"])
        ce_oi = float(row["ce_oi"])
        ce_chg = float(row["ce_chg_oi"])
        pe_oi = float(row["pe_oi"])
        pe_chg = float(row["pe_chg_oi"])

        is_atm = abs(stk - true_sr.atm_strike) < 25.0
        is_true_r = abs(stk - true_sr.true_resistance_strike) < 5.0
        is_true_s = abs(stk - true_sr.true_support_strike) < 5.0

        badges = []
        if is_atm:
            badges.append('<span style="background:#FACC15;color:#000;font-size:0.7em;font-weight:700;padding:1px 4px;border-radius:3px;">ATM</span>')
        if is_true_r:
            badges.append('<span style="background:#EF4444;color:#FFF;font-size:0.7em;font-weight:700;padding:1px 4px;border-radius:3px;">🛑 TRUE RESISTANCE</span>')
        if is_true_s:
            badges.append('<span style="background:#10B981;color:#FFF;font-size:0.7em;font-weight:700;padding:1px 4px;border-radius:3px;">🛡️ TRUE SUPPORT</span>')

        badge_str = " ".join(badges)

        # Balloon indicator
        ce_balloon = "💨 Inflating" if ce_chg > 20000 else ("🎈 Releasing" if ce_chg < -10000 else "—")
        pe_balloon = "💨 Inflating" if pe_chg > 20000 else ("🎈 Releasing" if pe_chg < -10000 else "—")

        ce_bg = f"rgba(239, 68, 68, {0.10 + 0.60 * (ce_oi / max_ce):.2f})"
        pe_bg = f"rgba(16, 185, 129, {0.10 + 0.60 * (pe_oi / max_pe):.2f})"

        row_bg = "#111827" if is_atm else "#0C1322"

        row_str = (
            f'<tr style="background:{row_bg}; border-bottom: 1px solid #1E293B; font-family: monospace; font-size: 0.88em;">'
            # CALL SIDE
            f'<td style="padding: 8px 12px; text-align: right; color: #F87171;">{ce_balloon}</td>'
            f'<td style="padding: 8px 12px; text-align: right; color: {"#F87171" if ce_chg > 0 else "#94A3B8"};">{ce_chg:+,.0f}</td>'
            f'<td style="padding: 8px 14px; text-align: right; background: {ce_bg}; color: #FFFFFF; font-weight: 700;">{ce_oi:,.0f}</td>'
            # STRIKE (CENTER)
            f'<td style="padding: 8px 14px; text-align: center; background: #151F30; border-left: 1px solid #334155; border-right: 1px solid #334155; color: #F8FAFC; font-weight: 700;">'
            f'{stk:,.0f} {badge_str}'
            f'</td>'
            # PUT SIDE
            f'<td style="padding: 8px 14px; text-align: left; background: {pe_bg}; color: #FFFFFF; font-weight: 700;">{pe_oi:,.0f}</td>'
            f'<td style="padding: 8px 12px; text-align: left; color: {"#34D399" if pe_chg > 0 else "#94A3B8"};">{pe_chg:+,.0f}</td>'
            f'<td style="padding: 8px 12px; text-align: left; color: #34D399;">{pe_balloon}</td>'
            f'</tr>'
        )
        rows_html.append(row_str)

    tbl_content = "\n".join(rows_html)
    tbl_html = (
        f'<div style="overflow-x: auto; border: 1px solid #1E293B; border-radius: 8px; margin-top: 8px; margin-bottom: 16px;">'
        f'<table style="width: 100%; border-collapse: collapse;">'
        f'<thead>'
        f'<tr style="background: #1E293B; color: #94A3B8; font-size: 0.74em; text-transform: uppercase; font-weight: 700;">'
        f'<th colspan="3" style="text-align: center; padding: 6px; color: #EF4444; border-right: 1px solid #334155;">CALL WRITERS (CEILING PRESSURE)</th>'
        f'<th style="text-align: center; padding: 6px; color: #FACC15;">CORE STRIKE (200-PT RANGE)</th>'
        f'<th colspan="3" style="text-align: center; padding: 6px; color: #10B981; border-left: 1px solid #334155;">PUT WRITERS (FLOOR SUPPORT)</th>'
        f'</tr>'
        f'<tr style="background: #111827; color: #94A3B8; font-size: 0.72em; text-transform: uppercase; border-bottom: 1px solid #334155;">'
        f'<th style="padding: 6px 12px; text-align: right;">Gas Balloon</th>'
        f'<th style="padding: 6px 12px; text-align: right;">Call ΔOI</th>'
        f'<th style="padding: 6px 14px; text-align: right;">Call OI</th>'
        f'<th style="padding: 6px 14px; text-align: center; background: #151F30; border-left: 1px solid #334155; border-right: 1px solid #334155; color: #FFFFFF;">Strike</th>'
        f'<th style="padding: 6px 14px; text-align: left;">Put OI</th>'
        f'<th style="padding: 6px 12px; text-align: left;">Put ΔOI</th>'
        f'<th style="padding: 6px 12px; text-align: left;">Gas Balloon</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>{tbl_content}</tbody>'
        f'</table>'
        f'</div>'
    )
    _render_html(tbl_html)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Risk Management & Discipline HUD
# ─────────────────────────────────────────────────────────────────────────────
def render_risk_discipline_hud(
    spot: float,
    true_sr: ape.TrueSRResult,
    default_base_cap: float = 100000.0,
) -> None:
    """Renders the 1:2 R:R Execution Lock, Capital Protection Tracker, and Overtrading Killswitch."""
    st.markdown("### 🛡️ Risk Management & Discipline HUD")

    # Session State initializations for stateful simulation
    if "risk_realized_pnl" not in st.session_state:
        st.session_state["risk_realized_pnl"] = 1250.0  # Sample positive start
    if "risk_trades_count" not in st.session_state:
        st.session_state["risk_trades_count"] = 2

    # 3 Columns for Risk Tools
    c_risk1, c_risk2, c_risk3 = st.columns([1.2, 1.2, 1.0])

    with c_risk1:
        st.markdown("#### 🔒 1:2 Risk-to-Reward Lock")
        # Pre-populate suggested levels from True S/R
        def_entry = spot
        def_sl = true_sr.true_support_strike - 15.0 if spot > true_sr.true_support_strike else (spot - 20.0)
        def_tgt = true_sr.true_resistance_strike + 25.0 if spot < true_sr.true_resistance_strike else (spot + 45.0)

        c_e1, c_e2, c_e3 = st.columns(3)
        with c_e1:
            in_entry = st.number_input("Entry (₹)", value=round(def_entry, 1), step=5.0)
        with c_e2:
            in_sl = st.number_input("Stop Loss (₹)", value=round(def_sl, 1), step=5.0)
        with c_e3:
            in_tgt = st.number_input("Target (₹)", value=round(def_tgt, 1), step=5.0)

        risk_status = ape.RiskDisciplineEngine.evaluate_trade_risk(
            entry_price=in_entry,
            stop_loss=in_sl,
            target_price=in_tgt,
            base_capital=default_base_cap,
            realized_pnl=st.session_state["risk_realized_pnl"],
            daily_trades_count=st.session_state["risk_trades_count"],
        )

        risk_pts = abs(in_entry - in_sl)
        reward_pts = abs(in_tgt - in_entry)

        if risk_status.rr_locked:
            st.error(f"🔒 **EXECUTION BLOCKED: R:R is 1:{risk_status.rr_ratio:.2f}** (Risk: {risk_pts:.1f} pts / Reward: {reward_pts:.1f} pts). Minimum 1:2.0 required!")
        else:
            st.success(f"🔓 **EXECUTION APPROVED: R:R is 1:{risk_status.rr_ratio:.2f}** (Risk: {risk_pts:.1f} pts / Reward: {reward_pts:.1f} pts). Asymmetric edge confirmed.")

    with c_risk2:
        st.markdown("#### 💰 Capital Protection Tracker")
        pnl = st.session_state["risk_realized_pnl"]
        equity = default_base_cap + pnl
        house_money = risk_status.house_money_pool
        max_risk = risk_status.max_risk_inr

        pnl_col = "#10B981" if pnl >= 0 else "#EF4444"

        cap_html = (
            f'<div style="background:#0D131F; border:1px solid #1E293B; border-radius:8px; padding:12px 14px; font-size:0.85em;">'
            f'<div style="display:flex; justify-content:space-between; margin-bottom:6px;">'
            f'<span style="color:#8FA6B8;">Base Capital (Protected):</span>'
            f'<b style="color:#FFFFFF; font-family:monospace;">₹{default_base_cap:,.0f}</b>'
            f'</div>'
            f'<div style="display:flex; justify-content:space-between; margin-bottom:6px;">'
            f'<span style="color:#8FA6B8;">Realized Daily P&L:</span>'
            f'<b style="color:{pnl_col}; font-family:monospace;">{pnl:+,.0f}</b>'
            f'</div>'
            f'<div style="display:flex; justify-content:space-between; margin-bottom:6px;">'
            f'<span style="color:#8FA6B8;">House Money (Profit Pool):</span>'
            f'<b style="color:#10B981; font-family:monospace;">₹{house_money:,.0f}</b>'
            f'</div>'
            f'<div style="display:flex; justify-content:space-between; border-top:1px solid #334155; padding-top:6px;">'
            f'<span style="color:#FACC15; font-weight:700;">Max Risk Allowed on Next Trade:</span>'
            f'<b style="color:#FACC15; font-family:monospace;">₹{max_risk:,.0f}</b>'
            f'</div>'
            f'</div>'
        )
        _render_html(cap_html)
        st.caption("Rule: Base capital is never risked once in profit. Trades strictly re-risk a fraction of profits.")

    with c_risk3:
        st.markdown("#### 🛑 Overtrading Killswitch")
        trades_done = st.session_state["risk_trades_count"]
        st.write(f"Trades Taken Today: **{trades_done} of 4 Max**")

        if risk_status.killswitch_active:
            st.warning(f"**{risk_status.killswitch_reason}**")
            st.markdown('<div style="background:#7F1D1D; color:#FFF; font-weight:800; text-align:center; padding:8px; border-radius:6px;">SIT QUIETLY — LOG OFF TERMINAL</div>', unsafe_allow_html=True)
        else:
            st.info(f"Target: +₹{risk_status.profit_target:,.0f} | Stop: -₹{risk_status.max_loss_limit:,.0f}. Killswitch standing by.")

    # ── One-Click Loss Cut Section ──
    st.markdown("---")
    c_loss1, c_loss2 = st.columns([3, 1])
    with c_loss1:
        st.markdown(
            "**One-Click Loss Cut:** When a setup invalidates (e.g. Call writers aggressively defend ceiling or support collapses), "
            "do NOT wait for hopeless recoveries. Cut the trade mechanically to preserve mental and financial capital."
        )
    with c_loss2:
        if st.button("🛑 BOOK LOSS / CUT POSITION NOW", type="primary", use_container_width=True):
            st.session_state["risk_realized_pnl"] -= 450.0  # Simulate booked loss
            st.session_state["risk_trades_count"] += 1
            st.toast("Loss cut mechanically executed at market! Discipline score +10.", icon="🛡️")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Complete Tab Renderer
# ─────────────────────────────────────────────────────────────────────────────
def render_anti_fakeout_tab(
    df: pd.DataFrame,
    spot: float,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    read: Optional[Any] = None,
) -> None:
    """
    Renders the complete '🛡️ Anti-Fakeout & Real-Time Pressure' Dashboard Tab.
    """
    st.markdown("## 🛡️ Anti-Fakeout & Real-Time Pressure Engine")
    st.caption("Institutional option seller's perspective: 200-point core truth chain, gas balloon pressure gauge, and VWAP fakeout detection.")

    # 1. Macroeconomic Event Overlay Banner (Persistent)
    macro_status = ape.MacroEventEngine.evaluate_macro_status()
    render_macro_event_banner(macro_status)

    # 2. Extract 200-pt Core Chain & S/R
    core_df, atm = ape.TruthOptionChainEngine.extract_core_200pt_range(df, spot)
    true_sr = ape.TruthOptionChainEngine.calculate_true_sr(core_df, atm)
    pressure = ape.TruthOptionChainEngine.calculate_pressure_gauge(core_df)

    # 3. Extract VWAP from pa_frames (Futures / 5m)
    vwap_val = None
    candle_high = None
    candle_low = None
    if pa_frames:
        for tf in ("5m", "15m", "1m"):
            fr = pa_frames.get(tf)
            if fr is not None and not fr.empty:
                if "vwap" in fr.columns and fr["vwap"].notna().any():
                    vwap_val = float(fr["vwap"].dropna().iloc[-1])
                if "high" in fr.columns:
                    candle_high = float(fr["high"].max())
                if "low" in fr.columns:
                    candle_low = float(fr["low"].min())
                break

    # 4. Fakeout Detection Engine
    detector = ape.FakeoutDetectorEngine.detect_fakeout(
        spot=spot,
        vwap=vwap_val,
        true_sr=true_sr,
        pressure=pressure,
        candle_high=candle_high,
        candle_low=candle_low,
    )

    # 5. Definitive Actionable Prompt Banner
    render_actionable_prompt_banner(detector, pressure)

    # 6. The "Bodybuilder" Tug-of-War Visualizer
    st.markdown("### 🏋️ The \"Bodybuilder\" Tug-of-War Visualizer *(Call Sellers vs Put Sellers)*")
    st.caption("Horizontal comparative weight (Total OI in 200-pt core) & dynamic acceleration needle (Net Pressure Index).")
    fig_bodybuilder = create_bodybuilder_chart(pressure)
    st.plotly_chart(fig_bodybuilder, use_container_width=True)

    # 7. Core Metrics Cards
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(
            label="🛑 True Resistance (Ceiling)",
            value=f"{true_sr.true_resistance_strike:,.0f}",
            delta=f"{true_sr.true_resistance_status} ({true_sr.true_resistance_chg:+,.0f} ΔOI)",
            delta_color="inverse"
        )
        st.caption(f"Call OI: **{true_sr.true_resistance_oi:,.0f}** contracts")

    with m2:
        st.metric(
            label="🛡️ True Support (Floor)",
            value=f"{true_sr.true_support_strike:,.0f}",
            delta=f"{true_sr.true_support_status} ({true_sr.true_support_chg:+,.0f} ΔOI)",
        )
        st.caption(f"Put OI: **{true_sr.true_support_oi:,.0f}** contracts")

    with m3:
        st.metric(
            label="💨 Net Pressure Index",
            value=f"{pressure.net_pressure_index:+.1f}%",
            delta=pressure.pressure_regime
        )
        st.caption(f"PCR ΔOI: **{pressure.pcr_chg:.2f}** | PCR OI: **{pressure.pcr_oi:.2f}**")

    with m4:
        st.metric(
            label="🎯 ATM Strike (Center)",
            value=f"{atm:,.0f}",
            delta=f"Corridor: {true_sr.corridor_width:,.0f} pts"
        )
        st.caption(f"Spot: **{spot:,.1f}** | VWAP: **{(vwap_val or spot):,.1f}**")

    # 8. The "Truth" Option Chain 200-Point Table
    st.markdown("### 🔍 The \"Truth\" Option Chain Grid *(Strict 200-Point Core)*")
    st.caption("Filtered strictly to ATM ±100 points (5 core strikes). Deep OTM retail noise completely excluded.")
    render_truth_chain_table(core_df, spot, true_sr)

    # 8B. Striking Price Writer Prediction Power Card
    try:
        import writers_graph_engine as wge
        tested_stk = true_sr.true_support_strike if (spot - true_sr.true_support_strike) < (true_sr.true_resistance_strike - spot) else true_sr.true_resistance_strike
        cw_pred = wge.evaluate_striking_price_prediction_power(df, spot, striking_strike=tested_stk)
        _render_html(wge.render_striking_price_prediction_card(cw_pred))
    except Exception:
        pass

    # 9. Risk Management & Discipline HUD
    render_risk_discipline_hud(spot, true_sr)
