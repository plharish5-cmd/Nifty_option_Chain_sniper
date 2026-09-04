"""
red_bar_tab.py — Streamlit UI Tab for Dr. Devendra Pratap Singh's Advanced Red Bar Strategy.

Provides a 100% "Spoon-Fed" execution terminal:
  • EXACT BUY LEVEL
  • EXACT STOP LOSS LEVEL (with max ₹ risk per lot)
  • EXACT TARGET 1 (1:1), TARGET 2 (1:1.5), TARGET 3 (1:2)
  • RECOMMENDED ATM OPTION CONTRACT (Symbol, LTP, Option SL & Target)
  • STEP-BY-STEP EXECUTION SOP (No guessing, rule-based)
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
from red_bar_strategy import (
    POBZone,
    calculate_pob,
    get_spoon_fed_signal,
    scan_all_red_bar_setups,
)

GREEN = "#2FBF71"
RED = "#E8503A"
SLATE = "#8FA6B8"
GOLD = "#C9A227"
BLUE = "#4A9BD1"
AMBER = "#E0A030"
PURPLE = "#9B51E0"


def _render_spoon_fed_chart(
    df_5m: pd.DataFrame,
    signal_info: Dict[str, Any],
) -> go.Figure:
    """Plots 5m candlesticks with POB band, Asian H/L liquidity lines, 30 EMA, and signal markers."""
    fig = go.Figure()
    ts = pd.to_datetime(df_5m["timestamp"])

    # Candlestick
    fig.add_trace(
        go.Candlestick(
            x=ts,
            open=df_5m["open"],
            high=df_5m["high"],
            low=df_5m["low"],
            close=df_5m["close"],
            name="Nifty 5m",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            increasing_fillcolor=GREEN,
            decreasing_fillcolor=RED,
            line_width=1.2,
        )
    )

    # 30 EMA on 5m (equivalent to 5 EMA on 30m)
    if "ema30" not in df_5m.columns:
        df_5m["ema30"] = df_5m["close"].ewm(span=30, adjust=False).mean()

    fig.add_trace(
        go.Scatter(
            x=ts,
            y=df_5m["ema30"],
            name="30 EMA (150m Mean)",
            line=dict(color="#FFD166", width=1.5, dash="dot"),
        )
    )

    pob_mean = signal_info.get("pob_mean", 0.0)
    pob_lower = signal_info.get("pob_lower", 0.0)
    pob_upper = signal_info.get("pob_upper", 0.0)
    h_asia = signal_info.get("asian_high", 0.0)
    l_asia = signal_info.get("asian_low", 0.0)

    # Shaded Candle 1 POB Zone
    if pob_lower > 0 and pob_upper > 0:
        fig.add_hrect(
            y0=pob_lower,
            y1=pob_upper,
            fillcolor="rgba(74, 155, 209, 0.12)",
            line=dict(color="rgba(74, 155, 209, 0.4)", width=1, dash="dot"),
            annotation_text=f"POB Zone (0.44-0.56): {pob_mean:,.1f}",
            annotation_position="top left",
            annotation_font=dict(color=BLUE, size=10),
        )

    # Asian Session High and Low Liquidity Lines
    if h_asia > 0 and l_asia > 0:
        fig.add_hline(
            y=h_asia,
            line=dict(color=RED, width=1.5, dash="dash"),
            annotation_text=f"Asian High (H_asia): {h_asia:,.1f}",
            annotation_position="bottom right",
            annotation_font=dict(color=RED, size=11),
        )
        fig.add_hline(
            y=l_asia,
            line=dict(color=GREEN, width=1.5, dash="dash"),
            annotation_text=f"Asian Low (L_asia): {l_asia:,.1f}",
            annotation_position="top right",
            annotation_font=dict(color=GREEN, size=11),
        )

    # Active Trade Overlay Lines
    if signal_info.get("has_signal"):
        entry = signal_info["entry_level"]
        sl = signal_info["sl_level"]
        t1 = signal_info["target_1"]
        t2 = signal_info["target_2"]
        is_ce = signal_info["direction"] == "BULLISH"

        fig.add_hline(
            y=entry,
            line=dict(color=GOLD, width=2, dash="solid"),
            annotation_text=f"BUY ENTRY: {entry:,.1f}",
            annotation_position="top left",
            annotation_font=dict(color=GOLD, size=11, family="monospace"),
        )
        fig.add_hline(
            y=sl,
            line=dict(color=RED, width=1.8, dash="solid"),
            annotation_text=f"STOP LOSS: {sl:,.1f} (-{signal_info['risk_pts']:.1f}p)",
            annotation_position="bottom left",
            annotation_font=dict(color=RED, size=11, family="monospace"),
        )
        fig.add_hline(
            y=t1,
            line=dict(color=GREEN, width=1.8, dash="solid"),
            annotation_text=f"TARGET 1: {t1:,.1f} (+{signal_info['reward_t1_pts']:.1f}p)",
            annotation_position="top left",
            annotation_font=dict(color=GREEN, size=11, family="monospace"),
        )
        fig.add_hline(
            y=t2,
            line=dict(color="#00E676", width=2, dash="dash"),
            annotation_text=f"TARGET 2: {t2:,.1f} (+{signal_info['reward_t2_pts']:.1f}p)",
            annotation_position="top left",
            annotation_font=dict(color="#00E676", size=11, family="monospace"),
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0A192F",
        plot_bgcolor="#0E2238",
        margin=dict(l=10, r=20, t=30, b=20),
        xaxis_rangeslider_visible=False,
        height=480,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=10, color=SLATE),
        ),
    )
    return fig


def render_red_bar_tab(
    pa_frames: Optional[Dict[str, pd.DataFrame]],
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
) -> None:
    """Renders Dr. Devendra Pratap Singh's Advanced Red Bar & Asian Sweep Tab."""
    df_5m = pa_frames.get("5m") if pa_frames else None

    # Header
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #112240 0%, #0A192F 100%);
                    border: 1px solid #E8503A; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h2 style="margin: 0; color: #FFFFFF; font-size: 22px; font-weight: 700;">
                        🔴 Dr. Devendra Advanced Red Bar & Asian Sweep Sniper
                    </h2>
                    <p style="margin: 4px 0 0 0; color: #8FA6B8; font-size: 13px;">
                        Point of Balance (POB 0.44-0.56) · European Open Asian Liquidity Sweep (12:45-14:15) · 150-Min Mean Reversion
                    </p>
                </div>
                <div style="background: #1B3A5B; border: 1px solid #4A9BD1; padding: 6px 14px; border-radius: 6px; text-align: right;">
                    <div style="font-size: 11px; color: #8FA6B8;">SPOT NIFTY</div>
                    <div style="font-size: 18px; font-weight: 700; color: #00D2D3; font-family: monospace;">
                        """ + f"{spot:,.2f}" + """
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if df_5m is None or df_5m.empty:
        st.warning("⚠️ 5-minute candle data feed is required to evaluate Red Bar & Asian Sweep setups.")
        return

    # Evaluate live signal
    signal_info = get_spoon_fed_signal(df_5m, spot=spot, chain_df=chain_df, current_time=now)

    # ── SECTION 1: SPOON-FED ACTION HERO CARD ────────────────────────────────
    if signal_info.get("has_signal"):
        is_ce = signal_info["direction"] == "BULLISH"
        badge_bg = "rgba(47, 191, 113, 0.15)" if is_ce else "rgba(232, 80, 58, 0.15)"
        badge_border = GREEN if is_ce else RED
        badge_text = GREEN if is_ce else RED

        st.markdown(
            f"""
            <div style="background: {badge_bg}; border: 2px solid {badge_border};
                        border-radius: 10px; padding: 18px 22px; margin-bottom: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                    <div style="font-size: 22px; font-weight: 800; color: {badge_text};">
                        🎯 {signal_info['action_text']} — {signal_info['setup_name']}
                    </div>
                    <div style="background: {badge_border}; color: #FFFFFF; font-size: 12px; font-weight: 700;
                                padding: 4px 12px; border-radius: 20px;">
                        ACTIVE SIGNAL
                    </div>
                </div>
                <div style="font-size: 14px; color: #FFFFFF; margin-bottom: 14px;">
                    <b>Trigger Reason:</b> {signal_info['notes']}
                </div>
                <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px;">
                    <div style="background: #0A192F; border: 1px solid #20436B; border-radius: 8px; padding: 12px; text-align: center;">
                        <div style="font-size: 11px; color: #8FA6B8;">BUY AT LEVEL</div>
                        <div style="font-size: 20px; font-weight: 700; color: #FFD166; font-family: monospace;">
                            {signal_info['entry_level']:,.1f}
                        </div>
                        <div style="font-size: 11px; color: #8FA6B8;">Spot Reference</div>
                    </div>
                    <div style="background: #0A192F; border: 1px solid {RED}; border-radius: 8px; padding: 12px; text-align: center;">
                        <div style="font-size: 11px; color: {RED};">STOP LOSS</div>
                        <div style="font-size: 20px; font-weight: 700; color: {RED}; font-family: monospace;">
                            {signal_info['sl_level']:,.1f}
                        </div>
                        <div style="font-size: 11px; color: #8FA6B8;">Risk: -{signal_info['risk_pts']:.1f} pts</div>
                    </div>
                    <div style="background: #0A192F; border: 1px solid {GREEN}; border-radius: 8px; padding: 12px; text-align: center;">
                        <div style="font-size: 11px; color: {GREEN};">TARGET 1 (1:1 Scalp)</div>
                        <div style="font-size: 20px; font-weight: 700; color: {GREEN}; font-family: monospace;">
                            {signal_info['target_1']:,.1f}
                        </div>
                        <div style="font-size: 11px; color: #8FA6B8;">Reward: +{signal_info['reward_t1_pts']:.1f} pts</div>
                    </div>
                    <div style="background: #0A192F; border: 1px solid #00E676; border-radius: 8px; padding: 12px; text-align: center;">
                        <div style="font-size: 11px; color: #00E676;">TARGET 2 (1:1.5 Standard)</div>
                        <div style="font-size: 20px; font-weight: 700; color: #00E676; font-family: monospace;">
                            {signal_info['target_2']:,.1f}
                        </div>
                        <div style="font-size: 11px; color: #8FA6B8;">Reward: +{signal_info['reward_t2_pts']:.1f} pts</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Spoon-Fed Option Contract Details Card
        col_opt, col_sop = st.columns([1.1, 1.4])

        with col_opt:
            st.markdown(
                f"""
                <div style="background: #0E2238; border: 1px solid #4A9BD1; border-radius: 8px; padding: 14px 16px;">
                    <div style="font-size: 13px; font-weight: 700; color: #00D2D3; margin-bottom: 10px;">
                        📌 RECOMMENDED OPTION CONTRACT (ATM Delta ~0.50)
                    </div>
                    <div style="font-size: 20px; font-weight: 800; color: #FFFFFF; font-family: monospace; margin-bottom: 8px;">
                        {signal_info['opt_symbol']}
                    </div>
                    <table style="width: 100%; font-size: 13px; color: #CCD6F6; line-height: 1.8;">
                        <tr>
                            <td>Contract Current LTP:</td>
                            <td style="text-align: right; font-weight: 700; color: #FFD166;">₹{signal_info['opt_ltp']:.1f}</td>
                        </tr>
                        <tr>
                            <td>Option Hard Stop Loss:</td>
                            <td style="text-align: right; font-weight: 700; color: {RED};">₹{signal_info['opt_sl_price']:.1f} (-{signal_info['opt_risk_pts']:.1f}p)</td>
                        </tr>
                        <tr>
                            <td>Option Target 1 (50% exit):</td>
                            <td style="text-align: right; font-weight: 700; color: {GREEN};">₹{signal_info['opt_t1_price']:.1f} (+{signal_info['opt_t1_pts']:.1f}p)</td>
                        </tr>
                        <tr>
                            <td>Option Target 2 (Runner):</td>
                            <td style="text-align: right; font-weight: 700; color: #00E676;">₹{signal_info['opt_t2_price']:.1f} (+{signal_info['opt_t2_pts']:.1f}p)</td>
                        </tr>
                        <tr style="border-top: 1px solid #20436B;">
                            <td><b>Max Rupee Risk (per lot):</b></td>
                            <td style="text-align: right; font-weight: 800; color: {RED};">-₹{signal_info['risk_rupees']:,.0f}</td>
                        </tr>
                        <tr>
                            <td><b>Expected Profit (per lot):</b></td>
                            <td style="text-align: right; font-weight: 800; color: {GREEN};">+₹{signal_info['gain_t2_rupees']:,.0f}</td>
                        </tr>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with col_sop:
            st.markdown(
                """
                <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 14px 16px;">
                    <div style="font-size: 13px; font-weight: 700; color: #FFD166; margin-bottom: 8px;">
                        📋 EXACT SPOON-FED EXECUTION SOP
                    </div>
                """,
                unsafe_allow_html=True,
            )
            for step in signal_info.get("sop_steps", []):
                st.markdown(f"<div style='font-size: 13px; color: #FFFFFF; margin-bottom: 6px;'>{step}</div>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)

    else:
        # Waiting for Level State
        st.markdown(
            f"""
            <div style="background: #0E2238; border: 1px solid #E0A030; border-radius: 10px; padding: 16px 20px; margin-bottom: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div style="font-size: 18px; font-weight: 700; color: #E0A030;">
                        ⏳ {signal_info.get('headline', 'Watching Key Levels')}
                    </div>
                    <div style="background: rgba(224, 160, 48, 0.2); color: #E0A030; font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 12px;">
                        NO IMPULSIVE TRADES
                    </div>
                </div>
                <div style="font-size: 13px; color: #CCD6F6; margin-bottom: 12px;">
                    {signal_info.get('advice', '')}
                </div>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;">
                    <div style="background: #0A192F; border: 1px solid #20436B; border-radius: 6px; padding: 10px; text-align: center;">
                        <div style="font-size: 11px; color: #8FA6B8;">CANDLE 1 POB MEAN</div>
                        <div style="font-size: 16px; font-weight: 700; color: #00D2D3; font-family: monospace;">
                            {signal_info.get('pob_mean', 0.0):,.1f}
                        </div>
                        <div style="font-size: 10px; color: #8FA6B8;">Zone: {signal_info.get('pob_lower', 0.0):,.1f} - {signal_info.get('pob_upper', 0.0):,.1f}</div>
                    </div>
                    <div style="background: #0A192F; border: 1px solid #20436B; border-radius: 6px; padding: 10px; text-align: center;">
                        <div style="font-size: 11px; color: #8FA6B8;">ASIAN SESSION HIGH (H_asia)</div>
                        <div style="font-size: 16px; font-weight: 700; color: {RED}; font-family: monospace;">
                            {signal_info.get('asian_high', 0.0):,.1f}
                        </div>
                        <div style="font-size: 10px; color: #8FA6B8;">European Resistance Liquidity</div>
                    </div>
                    <div style="background: #0A192F; border: 1px solid #20436B; border-radius: 6px; padding: 10px; text-align: center;">
                        <div style="font-size: 11px; color: #8FA6B8;">ASIAN SESSION LOW (L_asia)</div>
                        <div style="font-size: 16px; font-weight: 700; color: {GREEN}; font-family: monospace;">
                            {signal_info.get('asian_low', 0.0):,.1f}
                        </div>
                        <div style="font-size: 10px; color: #8FA6B8;">European Support Liquidity</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── SECTION 2: INTERACTIVE CHART ─────────────────────────────────────────
    st.subheader("📊 Price Action & Setup Overlay")
    fig = _render_spoon_fed_chart(df_5m, signal_info)
    st.plotly_chart(fig, use_container_width=True)

    # ── SECTION 3: QUANTITATIVE BACKTEST PROOF CARD ───────────────────────────
    st.subheader("🏆 Institutional Quantitative Verification (64 Historical Sessions)")

    c_s5, c_s1, c_all = st.columns(3)

    with c_s5:
        st.markdown(
            f"""
            <div style="background: #0E2238; border: 1px solid {GREEN}; border-radius: 8px; padding: 14px;">
                <div style="font-size: 12px; color: {GREEN}; font-weight: 700;">★ TOP PERFORMER: SETUP 5 (ASIAN SWEEP)</div>
                <div style="font-size: 26px; font-weight: 800; color: #FFFFFF; margin: 4px 0;">
                    3.02 <span style="font-size: 14px; color: #8FA6B8; font-weight: normal;">Profit Factor</span>
                </div>
                <div style="font-size: 13px; color: #CCD6F6; line-height: 1.6;">
                    • <b>Win Rate:</b> 57.1% (1:1.5 R:R) / 67.9% (1:1 R:R)<br>
                    • <b>Net Spot Gain:</b> +235.6 pts (+₹8,836/lot)<br>
                    • <b>Average Risk:</b> Just 10.6 pts (~₹400/lot)<br>
                    • <b>Timing:</b> 12:45 to 14:15 European Open
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_s1:
        st.markdown(
            f"""
            <div style="background: #0E2238; border: 1px solid #4A9BD1; border-radius: 8px; padding: 14px;">
                <div style="font-size: 12px; color: #4A9BD1; font-weight: 700;">MORNING SNIPER: SETUP 1 (POB RETEST)</div>
                <div style="font-size: 26px; font-weight: 800; color: #FFFFFF; margin: 4px 0;">
                    59.0% <span style="font-size: 14px; color: #8FA6B8; font-weight: normal;">Win Rate (1:1)</span>
                </div>
                <div style="font-size: 13px; color: #CCD6F6; line-height: 1.6;">
                    • <b>Trades:</b> 39 setups across 64 days<br>
                    • <b>Net Spot Gain:</b> +87.5 pts (+₹3,281/lot)<br>
                    • <b>Profit Factor:</b> 1.24<br>
                    • <b>Core Rule:</b> Never trade Candle 1 directly. Wait for POB retest rejection.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_all:
        st.markdown(
            """
            <div style="background: #0E2238; border: 1px solid #FFD166; border-radius: 8px; padding: 14px;">
                <div style="font-size: 12px; color: #FFD166; font-weight: 700;">THE 'POWER PAIR' (SETUP 1 + SETUP 5)</div>
                <div style="font-size: 26px; font-weight: 800; color: #00E676; margin: 4px 0;">
                    +₹12,117 <span style="font-size: 14px; color: #8FA6B8; font-weight: normal;">Net / lot</span>
                </div>
                <div style="font-size: 13px; color: #CCD6F6; line-height: 1.6;">
                    • <b>Total Captured:</b> +323.1 Net Spot Points<br>
                    • <b>Combined Win Rate:</b> ~63.4%<br>
                    • <b>Max Drawdown:</b> < 3 consecutive losses<br>
                    • <b>Filtered Out:</b> Choppy middle-of-day counter-trend knife catching.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
