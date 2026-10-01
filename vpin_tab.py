"""
vpin_tab.py — Institutional VPIN & Order Flow Toxicity Terminal Tab
====================================================================

Streamlit UI Tab implementing the Volume-Synchronized Probability of
Informed Trading (VPIN) market microstructure suite:
  1. Executive Toxicity Action Banner: Real-time order flow toxicity status,
     directional aggression, and options regime recommendation.
  2. Institutional Microstructure HUD: VPIN score gauge, directional imbalance,
     historical empirical CDF percentile, and flash-crash stress alarm.
  3. Interactive Dual Volume-Clock Chart:
     - Price candlestick with underlying equal-volume buckets (Buy Vol vs Sell Vol)
     - Rolling VPIN toxicity oscillator with theoretical threshold zones
  4. Options Playbook & Execution Bias:
     - Momentum Option Buying (Call/Put) on high-toxicity expansion
     - Rangebound Option Selling (Theta Harvest) on low-toxicity balance
     - Market Maker Stress / Liquidity Shock Warning (Alert-only, Rule 8)
  5. Volume Clock & Bucket Inspector: Detailed inspectable breakdown of
     strictly synchronized volume buckets.

REFERENCES:
  Easley, Lopez de Prado, & O'Hara (2010, 2011, 2012)
  All thresholds loaded from vpin_rules.json (Rule 4).
"""

from __future__ import annotations

print(f"[vpin_tab root] loaded from {__file__}")

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import config
from ui_utils import render_html, _render_html
from vpin_engine import VPINEngine, VPINResult, load_vpin_rules

# =============================================================================
# Color Palette & Styling (Institutional Dark Theme)
# =============================================================================
BG_DARK = "#0e1117"
CARD_BG = "#071626"
CARD_BG2 = "#0b2035"
BORDER_DARK = "#1e293b"
TEXT_LIGHT = "#f8fafc"
TEXT_MUTED = "#94a3b8"
CYAN = "#00e5ff"          # Volume Clock / Neutral Accent
EMERALD = "#10b981"       # Bullish Informed Flow / Low Toxicity Buy
CRIMSON = "#ef4444"       # Bearish Informed Flow / High Toxicity Alert
GOLD = "#ffb703"          # VPIN Metric / Warning Level
AMBER = "#f59e0b"         # Moderate Toxicity
PURPLE = "#a855f7"        # Option Selling / Theta Regime


def render_vpin_hud_card(result: VPINResult) -> None:
    """Renders top-level 4-metric executive HUD for VPIN."""
    summary = result.summary
    vpin = summary.get("latest_vpin")
    direction = summary.get("latest_direction")
    cdf = summary.get("latest_cdf")
    regime = summary.get("latest_regime", "UNKNOWN")
    alert = summary.get("alert_active", False)

    vpin_str = "SENTINEL-0000"
    vpin_color = (
        CRIMSON if (vpin and vpin >= 0.35)
        else AMBER if (vpin and vpin >= 0.25)
        else EMERALD if (vpin and vpin <= 0.18)
        else CYAN
    )

    dir_str = f"{direction:+.4f}" if direction is not None else "---"
    dir_color = EMERALD if (direction and direction > 0.05) else CRIMSON if (direction and direction < -0.05) else TEXT_MUTED
    dir_label = "BULLISH BUYERS" if (direction and direction > 0.05) else "BEARISH SELLERS" if (direction and direction < -0.05) else "BALANCED FLOW"

    cdf_str = f"{cdf:.1%}" if cdf is not None else "---"
    cdf_color = CRIMSON if (cdf and cdf >= 0.90) else AMBER if (cdf and cdf >= 0.75) else EMERALD

    regime_display = {
        "EXPANSION_OPTION_BUYING_CE": ("OPTION BUYING — CALL (CE)", EMERALD, "High Toxicity Bullish Momentum"),
        "EXPANSION_OPTION_BUYING_PE": ("OPTION BUYING — PUT (PE)", CRIMSON, "High Toxicity Bearish Cascade"),
        "EXPANSION_VOLATILITY_BREAKOUT": ("OPTION BUYING — STRADDLE", GOLD, "Toxic Flow — Large Move Impending"),
        "RANGEBOUND_OPTION_SELLING": ("OPTION SELLING — THETA HARVEST", PURPLE, "Balanced Flow — Sell Straddle / Condor"),
        "BALANCED_FLOW": ("WAIT / MONITOR", TEXT_MUTED, "Normal Two-Way Order Flow"),
        "WARMUP": ("WARMING UP", TEXT_MUTED, "Accumulating Initial Volume Buckets"),
    }.get(regime, (regime, TEXT_MUTED, "Processing Flow"))

    action_title, action_color, action_sub = regime_display

    html = f"""
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px;">
        <!-- Card 1: VPIN Score -->
        <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-top: 3px solid {vpin_color}; border-radius: 6px; padding: 12px 16px;">
            <div style="font-size: 11px; color: {TEXT_MUTED}; text-transform: uppercase; font-family: monospace; font-weight: 700;">VPIN Score (Toxicity)</div>
            <div style="font-size: 26px; font-weight: 800; color: {vpin_color}; font-family: monospace; margin: 4px 0;">{vpin_str}</div>
            <div style="font-size: 11px; color: {TEXT_MUTED};">Range: 0.0 (Calm) — 1.0 (Toxic)</div>
        </div>

        <!-- Card 2: Directional Aggression -->
        <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-top: 3px solid {dir_color}; border-radius: 6px; padding: 12px 16px;">
            <div style="font-size: 11px; color: {TEXT_MUTED}; text-transform: uppercase; font-family: monospace; font-weight: 700;">Directional Bias</div>
            <div style="font-size: 26px; font-weight: 800; color: {dir_color}; font-family: monospace; margin: 4px 0;">{dir_str}</div>
            <div style="font-size: 11px; color: {dir_color}; font-weight: 600;">{dir_label}</div>
        </div>

        <!-- Card 3: Toxicity CDF Percentile -->
        <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-top: 3px solid {cdf_color}; border-radius: 6px; padding: 12px 16px;">
            <div style="font-size: 11px; color: {TEXT_MUTED}; text-transform: uppercase; font-family: monospace; font-weight: 700;">Toxicity CDF Percentile</div>
            <div style="font-size: 26px; font-weight: 800; color: {cdf_color}; font-family: monospace; margin: 4px 0;">{cdf_str}</div>
            <div style="font-size: 11px; color: {TEXT_MUTED};">{'🚨 TOP 10% TOXICITY' if (cdf and cdf >= 0.90) else 'Normal Historical Range'}</div>
        </div>

        <!-- Card 4: Recommended Options Strategy -->
        <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-top: 3px solid {action_color}; border-radius: 6px; padding: 12px 16px;">
            <div style="font-size: 11px; color: {TEXT_MUTED}; text-transform: uppercase; font-family: monospace; font-weight: 700;">Derivatives Regime (Alert Only)</div>
            <div style="font-size: 15px; font-weight: 800; color: {action_color}; margin: 6px 0; font-family: monospace;">{action_title}</div>
            <div style="font-size: 11px; color: {TEXT_MUTED};">{action_sub}</div>
        </div>
    </div>
    """
    _render_html(html)

    if alert:
        _render_html(
            f"""
            <div style="background: rgba(239, 68, 68, 0.18); border: 2px solid {CRIMSON}; border-radius: 6px; padding: 12px 16px; margin-bottom: 14px; display: flex; align-items: center; gap: 12px;">
                <span style="font-size: 24px;">🚨</span>
                <div>
                    <div style="color: {CRIMSON}; font-weight: 800; font-size: 13px; font-family: monospace; text-transform: uppercase;">
                        CRITICAL LIQUIDITY SHOCK & FLASH CRASH STRESS ALARM
                    </div>
                    <div style="color: {TEXT_LIGHT}; font-size: 12px; margin-top: 2px;">
                        CDF[VPIN] exceeded the 99th percentile. Market makers are suffering severe adverse selection and may abruptly pull bids/asks. Expect violent slippage and liquidity evaporation.
                    </div>
                </div>
            </div>
            """
        )


def render_vpin_charts(result: VPINResult, bucket_vol: float) -> None:
    """Renders the interactive dual volume-clock chart."""
    b_df = result.buckets
    if b_df.empty or len(b_df) < 2:
        st.info("Accumulating volume buckets for charting...")
        return

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.55, 0.45],
        subplot_titles=(
            "Volume Clock: Buy vs Sell Volume Buckets & Price",
            "Rolling VPIN Toxicity Oscillator & Threshold Bands"
        ),
    )

    # ── Top Subplot: Buy Volume, Sell Volume, and Price ──
    # Format bucket timestamps
    x_axis = [f"B#{b.bucket_idx} ({str(b.timestamp)[-8:-3]})" for b in b_df.itertuples()]

    fig.add_trace(
        go.Bar(
            x=x_axis,
            y=b_df["buy_volume"],
            name="Buy Volume (BVC)",
            marker_color=EMERALD,
            opacity=0.75,
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Bar(
            x=x_axis,
            y=b_df["sell_volume"],
            name="Sell Volume (BVC)",
            marker_color=CRIMSON,
            opacity=0.75,
        ),
        row=1, col=1,
    )

    # Price Line on Secondary Y axis in Row 1
    fig.add_trace(
        go.Scatter(
            x=x_axis,
            y=b_df["price"],
            name="Bucket Close Price",
            mode="lines+markers",
            line=dict(color=GOLD, width=2),
            marker=dict(size=4),
            yaxis="y3",
        ),
        row=1, col=1,
    )

    # ── Bottom Subplot: VPIN Line & Thresholds ──
    fig.add_trace(
        go.Scatter(
            x=x_axis,
            y=b_df["vpin"],
            name="VPIN",
            mode="lines",
            line=dict(color=CYAN, width=2.5),
        ),
        row=2, col=1,
    )

    # Add Directional Imbalance as subtle filled line
    fig.add_trace(
        go.Scatter(
            x=x_axis,
            y=b_df["vpin_direction"],
            name="Directional Imbalance",
            mode="lines",
            line=dict(color=PURPLE, width=1.5, dash="dot"),
        ),
        row=2, col=1,
    )

    # Threshold horizontal reference lines
    fig.add_hline(y=0.35, line_dash="dash", line_color=CRIMSON, annotation_text="High Toxicity (0.35)", row=2, col=1)
    fig.add_hline(y=0.25, line_dash="dash", line_color=AMBER, annotation_text="Moderate (0.25)", row=2, col=1)
    fig.add_hline(y=0.18, line_dash="dash", line_color=EMERALD, annotation_text="Low / Theta (0.18)", row=2, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=BG_DARK,
        plot_bgcolor=BG_DARK,
        barmode="stack",
        height=580,
        margin=dict(l=40, r=40, t=40, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
        yaxis3=dict(
            overlaying="y",
            side="right",
            showgrid=False,
            title=dict(text="Price", font=dict(color=GOLD)),
            tickfont=dict(color=GOLD),
        ),
    )
    fig.update_yaxes(title_text="Volume / Bucket", row=1, col=1, gridcolor=BORDER_DARK)
    fig.update_yaxes(title_text="VPIN (0 to 1)", row=2, col=1, range=[0, 0.6], gridcolor=BORDER_DARK)
    fig.update_xaxes(gridcolor=BORDER_DARK)

    st.plotly_chart(fig, use_container_width=True)


def render_strategy_playbook(result: VPINResult) -> None:
    """Renders actionable playbooks for Option Buying vs Option Selling."""
    rules = load_vpin_rules()
    t_high = rules.get("toxicity_threshold_high", 0.35)
    t_low = rules.get("toxicity_threshold_low", 0.18)

    col1, col2 = st.columns(2)
    with col1:
        _render_html(
            f"""
            <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-radius: 6px; padding: 14px; height: 100%;">
                <div style="font-size: 13px; font-weight: 700; color: {EMERALD}; font-family: monospace; text-transform: uppercase;">
                    🎯 Option Buying Playbook (Momentum Expansion)
                </div>
                <div style="font-size: 11px; color: {TEXT_MUTED}; margin: 6px 0 10px 0;">
                    Trigger: VPIN ≥ {t_high} with directional confirmation.
                </div>
                <ul style="font-size: 12px; color: {TEXT_LIGHT}; margin: 0; padding-left: 18px; line-height: 1.6;">
                    <li><b>Call Buying (CE):</b> Directional Imbalance > +0.15. High informed buying pressure indicates an imminent explosive upside run.</li>
                    <li><b>Put Buying (PE):</b> Directional Imbalance < -0.15. Informed dumping signals cascading sell stops and market maker withdrawal.</li>
                    <li><b>Long Straddle / Strangle:</b> High VPIN with unresolved direction (|Dir| &le; 0.15) flags high probability of non-directional volatility expansion.</li>
                </ul>
            </div>
            """
        )

    with col2:
        _render_html(
            f"""
            <div style="background: {CARD_BG}; border: 1px solid {BORDER_DARK}; border-radius: 6px; padding: 14px; height: 100%;">
                <div style="font-size: 13px; font-weight: 700; color: {PURPLE}; font-family: monospace; text-transform: uppercase;">
                    🛡️ Option Selling Playbook (Theta Decay)
                </div>
                <div style="font-size: 11px; color: {TEXT_MUTED}; margin: 6px 0 10px 0;">
                    Trigger: VPIN &le; {t_low} with balanced two-way flow.
                </div>
                <ul style="font-size: 12px; color: {TEXT_LIGHT}; margin: 0; padding-left: 18px; line-height: 1.6;">
                    <li><b>Short Straddle / Strangle:</b> When VPIN is low, adverse selection is minimal. Market makers keep spreads tight and price oscillates in balance.</li>
                    <li><b>Iron Condors:</b> Best deployed when both VPIN &le; {t_low} and Bollinger Band width is compressed, capturing rapid premium decay.</li>
                    <li><b>Discipline Rule:</b> If VPIN starts spiking above {rules.get('toxicity_threshold_moderate', 0.25)}, proactively square off naked short strikes.</li>
                </ul>
            </div>
            """
        )


def render_volume_buckets_table(result: VPINResult) -> None:
    """Renders the detailed volume buckets table."""
    b_df = result.buckets
    if b_df.empty:
        return

    st.markdown(
        f"<div style='font-size:13px;font-weight:700;color:{TEXT_LIGHT};font-family:monospace;margin:16px 0 8px 0;'>"
        f"📋 VOLUME CLOCK BUCKET LOG (Easley-Lopez de Prado Equal-Volume Partition)</div>",
        unsafe_allow_html=True
    )

    disp = b_df.copy()
    disp["timestamp"] = disp["timestamp"].astype(str)
    disp["buy_vol_k"] = (disp["buy_volume"] / 1000).map("{:,.1f}k".format)
    disp["sell_vol_k"] = (disp["sell_volume"] / 1000).map("{:,.1f}k".format)
    disp["imbalance_k"] = (disp["abs_imbalance"] / 1000).map("{:,.1f}k".format)
    disp["price"] = disp["price"].map("{:,.2f}".format)
    disp["vpin"] = disp["vpin"].map(lambda x: f"{x:.4f}" if pd.notna(x) else "---")
    disp["vpin_dir"] = disp["vpin_direction"].map(lambda x: f"{x:+.4f}" if pd.notna(x) else "---")
    disp["cdf"] = disp["vpin_cdf"].map(lambda x: f"{x:.1%}" if pd.notna(x) else "---")

    show_cols = [
        "bucket_idx", "timestamp", "price", "buy_vol_k", "sell_vol_k",
        "imbalance_k", "vpin", "vpin_dir", "cdf", "toxicity_level", "strategy_regime"
    ]
    st.dataframe(disp[show_cols].iloc[::-1], use_container_width=True, hide_index=True)


# =============================================================================
# Main Tab Entry Point
# =============================================================================
def render(
    df: Optional[pd.DataFrame] = None,
    spot: Optional[float] = None,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    fut_c: Optional[pd.DataFrame] = None,
    **kwargs,
) -> None:
    """
    Renders the complete VPIN & Order Flow Toxicity Tab.

    Can be invoked directly by dashboard.py as a registered tab, or
    run standalone inside Streamlit.
    """
    rules = load_vpin_rules()

    # Header
    col_hdr1, col_hdr2 = st.columns([2.5, 1])
    with col_hdr1:
        st.markdown(
            f"<h3 style='margin:0;padding:0;color:{TEXT_LIGHT};font-family:monospace;'>"
            f"⚡ VPIN & ORDER FLOW TOXICITY ENGINE</h3>"
            f"<span style='font-size:11px;color:{TEXT_MUTED};font-family:monospace;'>"
            f"Volume-Synchronized Probability of Informed Trading · Easley, Lopez de Prado, & O'Hara (2010, 2012)</span>",
            unsafe_allow_html=True,
        )

    # Controls row
    with st.expander("⚙️ VPIN Parameters & Data Source Controls", expanded=False):
        c_src, c_vol, c_win, c_vwin = st.columns(4)
        with c_src:
            source_mode = st.selectbox(
                "Data Source",
                ["LIVE_OR_PASSED", "OFFLINE_CSV", "DHAN_API"],
                format_func=lambda x: {
                    "LIVE_OR_PASSED": "🟢 Live Passed Feed / Current Session",
                    "OFFLINE_CSV": "📁 Stored Candle Session (CSV)",
                    "DHAN_API": "🌐 Direct Dhan HQ Futures API",
                }.get(x, x),
                key="vpin_source_mode_sel",
            )
        with c_vol:
            custom_bucket_vol = st.number_input(
                "Bucket Volume (V)",
                min_value=1000.0,
                max_value=50000000.0,
                value=float(rules.get("bucket_volume", 50000.0)),
                step=10000.0,
                key="vpin_bvol_input",
                help="Fixed volume per bucket. For NIFTY futures ~50k; for cash index ~2.5M",
            )
        with c_win:
            custom_window = st.number_input(
                "Window Length (n buckets)",
                min_value=5,
                max_value=100,
                value=int(rules.get("window_length", 50)),
                step=5,
                key="vpin_win_input",
                help="Rolling number of completed buckets used in the VPIN formula.",
            )
        with c_vwin:
            custom_vwin = st.number_input(
                "Volatility Window (bars)",
                min_value=5,
                max_value=100,
                value=int(rules.get("volatility_window", 50)),
                step=5,
                key="vpin_vwin_input",
                help="Bars used for rolling standard deviation of price changes sigma_{Delta S}.",
            )

    # Data resolution
    input_df: Optional[pd.DataFrame] = None

    if source_mode == "DHAN_API":
        with st.spinner("Fetching live futures candles from Dhan HQ..."):
            res_dhan, report = VPINEngine.from_dhan_frames(
                interval="15",
                days_back=5,
                mode="futures",
                bucket_volume=custom_bucket_vol,
                window_length=custom_window,
            )
            if not res_dhan.buckets.empty:
                render_vpin_hud_card(res_dhan)
                render_vpin_charts(res_dhan, custom_bucket_vol)
                render_strategy_playbook(res_dhan)
                render_volume_buckets_table(res_dhan)
                return
            else:
                st.warning("Dhan API returned no candles. Falling back to local candle session.")
                source_mode = "OFFLINE_CSV"

    if source_mode == "OFFLINE_CSV":
        candle_dir = Path(__file__).parent / "data" / "candles"
        available_files = sorted(candle_dir.glob("2026-*.csv"), reverse=True)
        if available_files:
            sel_file = st.selectbox(
                "Select Historical Session",
                available_files,
                format_func=lambda p: p.name,
                key="vpin_offline_csv_sel",
            )
            input_df = pd.read_csv(sel_file)
            # Auto-scale bucket volume if cash volume is loaded and bucket volume was small
            tot_v = input_df["volume"].sum()
            if tot_v > 50_000_000 and custom_bucket_vol < 500_000:
                custom_bucket_vol = 2_500_000.0

    if input_df is None:
        # Check passed parameters: fut_c (futures) -> pa_frames -> df
        if fut_c is not None and not fut_c.empty and "volume" in fut_c.columns and fut_c["volume"].sum() > 0:
            input_df = fut_c
        elif pa_frames is not None and "5m" in pa_frames and not pa_frames["5m"].empty and "volume" in pa_frames["5m"].columns and pa_frames["5m"]["volume"].sum() > 0:
            input_df = pa_frames["5m"]
        elif df is not None and not df.empty and "volume" in df.columns and df["volume"].sum() > 0:
            input_df = df
        else:
            # Fallback to latest candle file
            candle_dir = Path(__file__).parent / "data" / "candles"
            csv_files = sorted(candle_dir.glob("2026-*.csv"), reverse=True)
            if csv_files:
                input_df = pd.read_csv(csv_files[0])
                tot_v = input_df["volume"].sum()
                if tot_v > 50_000_000 and custom_bucket_vol < 500_000:
                    custom_bucket_vol = 2_500_000.0

    if input_df is None or input_df.empty:
        st.warning("No candle data available to compute VPIN. Please select a CSV or enable Dhan.")
        return

    # Execute Engine
    engine = VPINEngine(
        bucket_volume=custom_bucket_vol,
        window_length=custom_window,
        volatility_window=custom_vwin,
    )

    result = engine.calculate(input_df)

    # Render complete UI components
    render_vpin_hud_card(result)
    render_vpin_charts(result, custom_bucket_vol)
    render_strategy_playbook(result)
    render_volume_buckets_table(result)
