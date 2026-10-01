"""
pa_basics_tab.py — Price Action Basics Tab for Streamlit.

WHAT THIS TAB CANNOT DO:
────────────────────────
- It CANNOT generate automatic trades or buy/sell execution signals.
- It CANNOT feed the decision cockpit, system alerts, or Strategy 1.
- It CANNOT eliminate fractal pivot confirmation lag (N bars into the future).
- It CANNOT bypass the two-close rule: single-bar closes are strictly UNCONFIRMED.
- It CANNOT display combined probability scores or win-rate projections.
- It CANNOT enable trading in sideways regimes: sideways is a strict NO-TRADE state.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from ui_utils import render_html, _render_html

import config
from pa_basics import (
    PABasicsSnapshot,
    evaluate_pa_basics,
    load_rules,
)

GREEN = "#10b981"
RED = "#ef4444"
AMBER = "#f59e0b"
SLATE = "#94a3b8"
DARK_CARD = "#1e293b"
DARK_BG = "#0f172a"
BORDER = "#334155"


def _build_plotly_chart(
    df: pd.DataFrame,
    snap: PABasicsSnapshot,
    show_trendlines: bool = True,
    show_patterns: bool = True,
    show_rsi: bool = True,
    show_obs_fvgs: bool = False,
    show_candle_pats: bool = False,
    show_ema: bool = False,
    zone_width: float = 6.0
) -> go.Figure:
    """Builds interactive candlestick chart with trendlines, pivots, pattern necklines, and RSI."""
    rows = 2 if show_rsi else 1
    row_heights = [0.72, 0.28] if show_rsi else [1.0]

    fig = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=row_heights
    )

    times = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index)
    x_indices = list(range(len(df)))

    # 1. Candlestick
    fig.add_trace(
        go.Candlestick(
            x=x_indices,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="NIFTY",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            increasing_fillcolor="rgba(16, 185, 129, 0.7)",
            decreasing_fillcolor="rgba(239, 68, 68, 0.7)",
            showlegend=False
        ),
        row=1, col=1
    )

    # 1B. Optional Imported Indicators: 4 EMA, 8 EMA, VWAP
    if show_ema:
        if "ema4" in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x_indices, y=df["ema4"], mode="lines",
                    line=dict(color="#34d399", width=1.5),
                    name="4 EMA", showlegend=True
                ),
                row=1, col=1
            )
        if "ema8" in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x_indices, y=df["ema8"], mode="lines",
                    line=dict(color="#f87171", width=1.5),
                    name="8 EMA", showlegend=True
                ),
                row=1, col=1
            )
        if "vwap" in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x_indices, y=df["vwap"], mode="lines",
                    line=dict(color="#38bdf8", width=1.5, dash="dash"),
                    name="VWAP", showlegend=True
                ),
                row=1, col=1
            )

    # 2. Confirmed Pivots Markers
    if snap.pivots:
        knowable_pivots = [p for p in snap.pivots if p.known_at(len(df) - 1)]
        high_x = [p.i for p in knowable_pivots if p.kind == "HIGH"]
        high_y = [p.price for p in knowable_pivots if p.kind == "HIGH"]
        high_txt = [p.subkind or "PH" for p in knowable_pivots if p.kind == "HIGH"]

        low_x = [p.i for p in knowable_pivots if p.kind == "LOW"]
        low_y = [p.price for p in knowable_pivots if p.kind == "LOW"]
        low_txt = [p.subkind or "PL" for p in knowable_pivots if p.kind == "LOW"]

        if high_x:
            fig.add_trace(
                go.Scatter(
                    x=high_x, y=high_y,
                    mode="markers+text",
                    marker=dict(symbol="triangle-down", size=9, color="#f87171"),
                    text=high_txt,
                    textposition="top center",
                    textfont=dict(size=9, color="#f87171", family="monospace"),
                    name="Pivot High",
                    showlegend=False
                ),
                row=1, col=1
            )
        if low_x:
            fig.add_trace(
                go.Scatter(
                    x=low_x, y=low_y,
                    mode="markers+text",
                    marker=dict(symbol="triangle-up", size=9, color="#34d399"),
                    text=low_txt,
                    textposition="bottom center",
                    textfont=dict(size=9, color="#34d399", family="monospace"),
                    name="Pivot Low",
                    showlegend=False
                ),
                row=1, col=1
            )

    # 3. Diagonal Trendlines as Bands/Shapes
    curr_i = len(df) - 1
    if show_trendlines:
        supp = snap.trendlines.get("support")
        if supp:
            # Line from anchor1 to current bar
            y_start = supp.anchor1_price
            y_curr = supp.projected_current_level
            fig.add_trace(
                go.Scatter(
                    x=[supp.anchor1_idx, curr_i],
                    y=[y_start, y_curr],
                    mode="lines+text",
                    line=dict(color="#38bdf8", width=2, dash="solid"),
                    name="Support Trendline",
                    text=["", f" Supp Line: {y_curr:,.1f}"],
                    textposition="middle right",
                    textfont=dict(size=10, color="#38bdf8"),
                    showlegend=True
                ),
                row=1, col=1
            )

        res = snap.trendlines.get("resistance")
        if res:
            y_start = res.anchor1_price
            y_curr = res.projected_current_level
            fig.add_trace(
                go.Scatter(
                    x=[res.anchor1_idx, curr_i],
                    y=[y_start, y_curr],
                    mode="lines+text",
                    line=dict(color="#fb923c", width=2, dash="solid"),
                    name="Resistance Trendline",
                    text=["", f" Res Line: {y_curr:,.1f}"],
                    textposition="middle right",
                    textfont=dict(size=10, color="#fb923c"),
                    showlegend=True
                ),
                row=1, col=1
            )

    # 4. Pattern Necklines & S/R Zones (Rendered as Bands)
    if show_patterns and snap.patterns:
        for pat in snap.patterns[:3]:  # Top 3 most recent
            color = GREEN if pat.bias == "BULLISH" else RED
            # Support/Resistance Band
            fig.add_shape(
                type="rect",
                x0=pat.bar_index,
                x1=curr_i,
                y0=pat.level - zone_width,
                y1=pat.level + zone_width,
                fillcolor=f"rgba({'16, 185, 129' if pat.bias=='BULLISH' else '239, 68, 68'}, 0.15)",
                line=dict(width=1, color=color, dash="dot"),
                row=1, col=1
            )
            # Neckline
            fig.add_shape(
                type="line",
                x0=pat.bar_index,
                x1=curr_i,
                y0=pat.neckline,
                y1=pat.neckline,
                line=dict(color="#e2e8f0", width=1.5, dash="dash"),
                row=1, col=1
            )
            fig.add_annotation(
                x=curr_i,
                y=pat.neckline,
                text=f"{pat.name} Neckline ({pat.neckline:,.1f})",
                showarrow=False,
                font=dict(size=9, color="#e2e8f0"),
                xanchor="left",
                row=1, col=1
            )

    # 4B. Optional Imported SMC Layers: Order Blocks & FVGs
    if show_obs_fvgs:
        if snap.obs:
            for ob in snap.obs[-4:]:  # Most recent 4 OBs
                if getattr(ob, "open", True):
                    is_bull = ob.direction == "BULLISH"
                    fig.add_shape(
                        type="rect",
                        x0=ob.i, x1=curr_i,
                        y0=ob.low, y1=ob.high,
                        fillcolor="rgba(16, 185, 129, 0.18)" if is_bull else "rgba(239, 68, 68, 0.18)",
                        line=dict(width=1, color=GREEN if is_bull else RED, dash="solid"),
                        row=1, col=1
                    )
        if snap.fvgs:
            for fg in snap.fvgs[-4:]:  # Most recent 4 FVGs
                if getattr(fg, "open", True):
                    is_bull = fg.direction == "BULLISH"
                    fig.add_shape(
                        type="rect",
                        x0=fg.i, x1=curr_i,
                        y0=fg.bottom, y1=fg.top,
                        fillcolor="rgba(56, 189, 248, 0.12)" if is_bull else "rgba(244, 63, 94, 0.12)",
                        line=dict(width=1, color="#38bdf8" if is_bull else "#f43f5e", dash="dot"),
                        row=1, col=1
                    )

    # 4C. Optional Imported Candlestick Reversal Markers (Strategy 1)
    if show_candle_pats and snap.candle_patterns:
        cand_x, cand_y, cand_labels = [], [], []
        for idx, tags in enumerate(snap.candle_patterns):
            if tags and idx < len(df):
                meaningful = [t for t in tags if t != "BIG CANDLE"]
                if meaningful:
                    cand_x.append(idx)
                    is_bear = any(b in meaningful[0] for b in ("INVERTED", "BEARISH", "EVENING", "DOJI"))
                    cand_y.append(df["high"].iloc[idx] + 10 if is_bear else df["low"].iloc[idx] - 10)
                    cand_labels.append(meaningful[0])
        if cand_x:
            fig.add_trace(
                go.Scatter(
                    x=cand_x, y=cand_y,
                    mode="markers+text",
                    text=cand_labels,
                    textposition="top center",
                    textfont=dict(size=8, color="#fde047"),
                    marker=dict(size=7, color="#fde047", symbol="diamond"),
                    name="Candle Reversals (Strategy 1)",
                    showlegend=True
                ),
                row=1, col=1
            )

    # 5. Wilder's RSI Subplot
    if show_rsi and snap.rsi.series:
        rsi_x = x_indices[-len(snap.rsi.series):]
        fig.add_trace(
            go.Scatter(
                x=rsi_x,
                y=snap.rsi.series,
                mode="lines",
                line=dict(color="#a855f7", width=1.8),
                name="Wilder's RSI (14)",
                showlegend=False
            ),
            row=2, col=1
        )
        # Horizontal Guide Levels (70, 50, 30)
        fig.add_hline(y=70, line=dict(color="#f87171", width=1, dash="dash"), row=2, col=1)
        fig.add_hline(y=50, line=dict(color="#64748b", width=1, dash="dot"), row=2, col=1)
        fig.add_hline(y=30, line=dict(color="#34d399", width=1, dash="dash"), row=2, col=1)

    # Layout Customization
    # Tick formatting
    step = max(len(df) // 8, 1)
    tick_vals = x_indices[::step]
    tick_texts = [times.iloc[i].strftime("%H:%M") for i in tick_vals]

    fig.update_xaxes(
        tickmode="array",
        tickvals=tick_vals,
        ticktext=tick_texts,
        gridcolor="#1e293b",
        zerolinecolor="#1e293b",
        showgrid=True,
    )
    fig.update_yaxes(
        gridcolor="#1e293b",
        zerolinecolor="#1e293b",
        showgrid=True,
    )

    fig.update_layout(
        height=580,
        margin=dict(l=10, r=10, t=20, b=20),
        paper_bgcolor=DARK_BG,
        plot_bgcolor=DARK_BG,
        font=dict(family="monospace", color="#cbd5e1", size=11),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=10, color="#94a3b8"),
            bgcolor="rgba(15, 23, 42, 0.8)",
        ),
        hovermode="x unified",
    )
    return fig


def render(
    pa_frames: Optional[dict[str, pd.DataFrame]] = None,
    df: Optional[pd.DataFrame] = None,
    spot: Optional[float] = None,
    levels: Any = None,
    rules: Optional[dict] = None
):
    """
    Main entrypoint for the Price Action Basics Tab.
    Can receive pa_frames dict (1m/5m/15m) or a single DataFrame df.
    """
    _render_html(
        """
        <style>
        .pa-metric-box {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 10px 14px;
        }
        .pa-title {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #94a3b8;
        }
        .pa-val {
            font-size: 20px;
            font-weight: 800;
            font-family: monospace;
            margin-top: 2px;
        }
        .pa-sub {
            font-size: 11px;
            color: #64748b;
            font-family: monospace;
        }
        .pa-card {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 10px;
        }
        .pa-card-muted {
            opacity: 0.45;
            filter: grayscale(80%);
        }
        .pa-pill {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 10.5px;
            font-weight: 700;
            font-family: monospace;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    # 1. Controls Toolbar
    ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns([2, 2, 2, 4])

    available_tfs = ["15m", "5m", "1m"]
    with ctrl_col1:
        sel_tf = st.selectbox(
            "Timeframe",
            available_tfs,
            index=0,
            key="pa_basics_tf_sel",
            help="Higher timeframes produce authoritative structural swings."
        )

    # Resolve dataframe
    curr_df = None
    if pa_frames and isinstance(pa_frames, dict):
        curr_df = pa_frames.get(sel_tf)
    if curr_df is None and df is not None:
        curr_df = df

    with ctrl_col2:
        pivot_n = st.slider(
            "Pivot Window (N)",
            min_value=2,
            max_value=5,
            value=2,
            key="pa_basics_pivot_n",
            help="Fractal lookback/lookahead window. N=2 requires 2 bars lower/higher on both sides."
        )

    with ctrl_col3:
        equal_tol = st.slider(
            "Equal S/R Tol (%)",
            min_value=0.08,
            max_value=0.35,
            value=0.18,
            step=0.02,
            key="pa_basics_equal_tol",
            help="Tolerance for recognizing equal highs/lows in double tops/bottoms."
        )

    with ctrl_col4:
        _render_html("<div style='height: 4px;'></div>")
        t_col1, t_col2, t_col3, t_col4 = st.columns(4)
        with t_col1:
            show_tl = st.checkbox("Trendlines", value=True, key="pa_chk_tl")
        with t_col2:
            show_rsi = st.checkbox("RSI (14)", value=True, key="pa_chk_rsi")
        with t_col3:
            show_atr = st.checkbox("ATR Stops", value=True, key="pa_chk_atr")
        with t_col4:
            show_pat = st.checkbox("Patterns", value=True, key="pa_chk_pat")

    # Imported Confluence Techniques Toolbar
    with st.expander("🛠️ Overlay Imported Confluence Layers (smc.py & strategy1.py)", expanded=False):
        imp_col1, imp_col2, imp_col3 = st.columns(3)
        with imp_col1:
            show_obs_fvgs = st.checkbox("Order Blocks & FVGs (smc.py)", value=False, key="pa_chk_obs_fvgs")
        with imp_col2:
            show_candle_pats = st.checkbox("Candlestick Reversals (strategy1.py)", value=False, key="pa_chk_cands")
        with imp_col3:
            show_ema = st.checkbox("4 & 8 EMA / VWAP (strategy1.py)", value=False, key="pa_chk_ema")

    # Resolve spot price
    if spot is None or spot <= 0:
        if curr_df is not None and not curr_df.empty:
            spot = float(curr_df["close"].iloc[-1])
        else:
            spot = 24150.0

    # 2. Run Engine
    custom_rules = {
        "pivot_window_n": pivot_n,
        "pattern_equal_tolerance_pct": equal_tol,
    }
    snap = evaluate_pa_basics(
        df=curr_df,
        spot=spot,
        timeframe=sel_tf,
        custom_rules=custom_rules
    )

    # 3. Defensive Guard Warnings
    if snap.data_guard_warning:
        st.warning(f"⚠️ {snap.data_guard_warning}")
        if curr_df is None or len(curr_df) < 5:
            st.info("Pre-open / waiting for market session candles. Chart-reading engine standing by.")
            return

    # 4. Top Strip — 4 Primary Metrics
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)

    # Metric 1: Structural Trend
    struct_col = GREEN if snap.structure.trend == "UPTREND" else (RED if snap.structure.trend == "DOWNTREND" else AMBER)
    with m_col1:
        _render_html(
            f"""
            <div class="pa-metric-box">
              <div class="pa-title">Structure Trend ({sel_tf})</div>
              <div class="pa-val" style="color: {struct_col};">{snap.structure.trend}</div>
              <div class="pa-sub">Highs: {snap.structure.last_high_subkind} | Lows: {snap.structure.last_low_subkind}</div>
            </div>
            """
        )

    # Metric 2: Wilder's RSI with State Label
    rsi_val = snap.rsi.current_rsi
    rsi_state = snap.rsi.state
    rsi_color = GREEN if rsi_state == "CROSSED_UP_FROM_30" else (RED if rsi_state == "CROSSED_DOWN_FROM_70" else ("#cbd5e1" if snap.rsi.bias=="BULLISH" else "#94a3b8"))
    with m_col2:
        _render_html(
            f"""
            <div class="pa-metric-box">
              <div class="pa-title">Wilder's RSI (14)</div>
              <div class="pa-val" style="color: {rsi_color};">{rsi_val:.1f}</div>
              <div class="pa-sub">{rsi_state.replace('_', ' ')} ({snap.rsi.bias})</div>
            </div>
            """
        )

    # Metric 3: ATR & 1.5x Stop Distance
    atr_rep = snap.atr_stop
    atr_color = RED if atr_rep.is_fixed_stop_too_tight else GREEN
    with m_col3:
        _render_html(
            f"""
            <div class="pa-metric-box">
              <div class="pa-title">ATR Volatility Stop</div>
              <div class="pa-val" style="color: {atr_color};">{atr_rep.stop_1_5x_pts:.1f} <span style="font-size:13px; color:#94a3b8;">pts</span></div>
              <div class="pa-sub">1.5×: ₹{atr_rep.stop_1_5x_inr_lot:,.0f} | Fixed 40pt: ₹{atr_rep.fixed_stop_inr_lot:,.0f}</div>
            </div>
            """
        )

    # Metric 4: Live Patterns Count
    live_pats_cnt = len(snap.patterns)
    conf_pats_cnt = sum(1 for p in snap.patterns if p.status == "CONFIRMED")
    with m_col4:
        _render_html(
            f"""
            <div class="pa-metric-box">
              <div class="pa-title">Multi-Bar Patterns</div>
              <div class="pa-val" style="color: #38bdf8;">{live_pats_cnt} <span style="font-size:13px; color:#94a3b8;">detected</span></div>
              <div class="pa-sub">{conf_pats_cnt} Confirmed | {live_pats_cnt - conf_pats_cnt} Forming</div>
            </div>
            """
        )

    _render_html("<div style='height: 8px;'></div>")

    # 4b. Multi-Timeframe Market Structure (1M · 1W · 1D · 30M · 15M)
    try:
        import market_structure
        market_structure.render_market_structure_component(
            spot=spot,
            pa_frames=pa_frames,
            compact=False,
            title="🏛️ Multi-Timeframe Market Structure (1M · 1W · 1D · 30M · 15M)"
        )
    except Exception as _ms_err:
        st.caption(f"Market structure module error: {_ms_err}")

    # 5. Sideways Banner (Rule: Beginners avoid sideways markets entirely)
    is_sideways = snap.structure.is_sideways_no_trade
    if is_sideways:
        _render_html(
            """
            <div style="background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.45); border-radius: 6px; padding: 12px 16px; margin-bottom: 12px;">
              <div style="font-weight: 800; color: #f59e0b; font-size: 13.5px; letter-spacing: 0.03em;">
                ⚠️ SIDEWAYS REGIME DETECTED — STRICT NO-TRADE CONDITION
              </div>
              <div style="color: #cbd5e1; font-size: 12px; margin-top: 4px; line-height: 1.5;">
                <b>The source rule is explicit:</b> <i>Beginners should avoid sideways markets entirely.</i> Structure has failed to resolve into clean Higher Highs + Higher Lows or Lower Highs + Lower Lows.
                Trendline breaks and chart patterns within rangebound chop are overwhelmingly traps. Setups below are greyed out to prevent impulsive trades.
              </div>
            </div>
            """
        )

    # 6. Interactive Candlestick Chart
    fig = _build_plotly_chart(
        df=curr_df,
        snap=snap,
        show_trendlines=show_tl,
        show_patterns=show_pat,
        show_rsi=show_rsi,
        show_obs_fvgs=show_obs_fvgs,
        show_candle_pats=show_candle_pats,
        show_ema=show_ema,
        zone_width=float(snap.rules.get("zone_half_width_pts", 6.0))
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    # 7. Two-Column Detailed Breakdown Cards (Trendlines + Patterns + ATR)
    grid_left, grid_right = st.columns([5, 5], gap="medium")
    muted_class = "pa-card-muted" if is_sideways else ""

    with grid_left:
        # ── CONCEPT 1: DIAGONAL TRENDLINES ────────────────────────────────
        supp_tl = snap.trendlines.get("support")
        res_tl = snap.trendlines.get("resistance")

        supp_html = ""
        if supp_tl:
            supp_badge_bg = (
                "rgba(16, 185, 129, 0.15)" if supp_tl.break_state == "INTACT"
                else ("rgba(245, 158, 11, 0.15)" if "UNCONFIRMED" in supp_tl.break_state
                else ("rgba(56, 189, 248, 0.15)" if supp_tl.break_state == "FAKE BREAK"
                else "rgba(239, 68, 68, 0.15)"))
            )
            supp_badge_col = (
                GREEN if supp_tl.break_state == "INTACT"
                else (AMBER if "UNCONFIRMED" in supp_tl.break_state
                else ("#38bdf8" if supp_tl.break_state == "FAKE BREAK" else RED))
            )
            supp_html = f"""
            <div style="padding: 8px; background: #0f172a; border-radius: 4px; margin-bottom: 8px; border-left: 3px solid #38bdf8;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:700; color:#38bdf8; font-size:12px;">UPTREND SUPPORT LINE</span>
                <span class="pa-pill" style="background:{supp_badge_bg}; color:{supp_badge_col}; border:1px solid {supp_badge_col};">
                  {supp_tl.break_state}
                </span>
              </div>
              <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px; font-family:monospace;">
                Projected Level: <b>{supp_tl.projected_current_level:,.1f}</b> | Distance from Spot: <b>{supp_tl.distance_from_spot:+.1f} pts</b> | Touches: <b>{supp_tl.touch_count}</b>
              </div>
              <div style="font-size:11px; color:#94a3b8; margin-top:3px;">
                <i>{supp_tl.break_detail}</i>
              </div>
              <div style="font-size:10.5px; color:#f59e0b; margin-top:2px;">
                ⚠️ <b>Trap Guard:</b> {supp_tl.warning_note}
              </div>
            </div>
            """

        res_html = ""
        if res_tl:
            res_badge_bg = (
                "rgba(16, 185, 129, 0.15)" if res_tl.break_state == "INTACT"
                else ("rgba(245, 158, 11, 0.15)" if "UNCONFIRMED" in res_tl.break_state
                else ("rgba(56, 189, 248, 0.15)" if res_tl.break_state == "FAKE BREAK"
                else "rgba(239, 68, 68, 0.15)"))
            )
            res_badge_col = (
                GREEN if res_tl.break_state == "INTACT"
                else (AMBER if "UNCONFIRMED" in res_tl.break_state
                else ("#38bdf8" if res_tl.break_state == "FAKE BREAK" else RED))
            )
            res_html = f"""
            <div style="padding: 8px; background: #0f172a; border-radius: 4px; margin-bottom: 6px; border-left: 3px solid #fb923c;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:700; color:#fb923c; font-size:12px;">DOWNTREND RESISTANCE LINE</span>
                <span class="pa-pill" style="background:{res_badge_bg}; color:{res_badge_col}; border:1px solid {res_badge_col};">
                  {res_tl.break_state}
                </span>
              </div>
              <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px; font-family:monospace;">
                Projected Level: <b>{res_tl.projected_current_level:,.1f}</b> | Distance from Spot: <b>{res_tl.distance_from_spot:+.1f} pts</b> | Touches: <b>{res_tl.touch_count}</b>
              </div>
              <div style="font-size:11px; color:#94a3b8; margin-top:3px;">
                <i>{res_tl.break_detail}</i>
              </div>
              <div style="font-size:10.5px; color:#f59e0b; margin-top:2px;">
                ⚠️ <b>Trap Guard:</b> {res_tl.warning_note}
              </div>
            </div>
            """

        empty_tl_html = "<div style='font-size:12px; color:#64748b;'>Fewer than 2 confirmed swing pivots available to anchor trendlines.</div>" if (not supp_tl and not res_tl) else ""

        trendline_card_html = f"""
        <div class="pa-card {muted_class}">
          <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:6px; margin-bottom:8px;">
            <span style="font-size:12px; font-weight:700; color:#e2e8f0; text-transform:uppercase;">1 · Diagonal Trendlines (Close-Line View)</span>
            <span class="pa-pill" style="background:#0f172a; color:#38bdf8; border:1px solid #334155;">FRACTAL ANCHORS</span>
          </div>
          {empty_tl_html}
          {supp_html}
          {res_html}
        </div>
        """
        _render_html(trendline_card_html)

        # ── CONCEPT 3: ATR-BASED STOP DISTANCES ───────────────────────────
        atr_card_html = f"""
        <div class="pa-card {muted_class}">
          <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:6px; margin-bottom:8px;">
            <span style="font-size:12px; font-weight:700; color:#e2e8f0; text-transform:uppercase;">3 · ATR Stop Distance & Risk Comparison</span>
            <span class="pa-pill" style="background:#0f172a; color:#a855f7; border:1px solid #334155;">1 LOT = {atr_rep.stop_1_5x_inr_lot / atr_rep.stop_1_5x_pts:.0f} QTY</span>
          </div>
          <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 8px; font-family: monospace; font-size: 11.5px;">
            <div style="background:#0f172a; padding:8px; border-radius:4px;">
              <div style="color:#94a3b8; font-size:10.5px;">1.5× ATR Stop (Source Standard)</div>
              <div style="font-size:14px; font-weight:700; color:#f8fafc; margin-top:2px;">{atr_rep.stop_1_5x_pts:.1f} pts ({atr_rep.stop_1_5x_pct:.2f}%)</div>
              <div style="color:#38bdf8;">₹{atr_rep.stop_1_5x_inr_lot:,.0f} per lot</div>
            </div>
            <div style="background:#0f172a; padding:8px; border-radius:4px;">
              <div style="color:#94a3b8; font-size:10.5px;">2.0× ATR Stop (Wide Regime)</div>
              <div style="font-size:14px; font-weight:700; color:#f8fafc; margin-top:2px;">{atr_rep.stop_2_0x_pts:.1f} pts ({atr_rep.stop_2_0x_pct:.2f}%)</div>
              <div style="color:#a855f7;">₹{atr_rep.stop_2_0x_inr_lot:,.0f} per lot</div>
            </div>
          </div>
          <div style="margin-top:8px; padding:8px; background:#0f172a; border-radius:4px; font-size:11px; color:#cbd5e1;">
            <b>Fixed 15m Baseline Stop:</b> 40.0 pts (₹{atr_rep.fixed_stop_inr_lot:,.0f} per lot).<br>
            <span style="color: {'#f87171' if atr_rep.is_fixed_stop_too_tight else '#34d399'}; font-weight:600;">
              {atr_rep.volatility_vs_fixed_warning}
            </span>
          </div>
        </div>
        """
        _render_html(atr_card_html)

    with grid_right:
        # ── CONCEPT 4: MULTI-CANDLE CHART PATTERNS ────────────────────────
        if not snap.patterns:
            patterns_inner_html = """
            <div style="padding: 14px; text-align: center; color: #64748b; font-size: 12px;">
              No multi-candle patterns (Double Top/Bottom, H&S) detected on confirmed structural pivots.
            </div>
            """
        else:
            pats_list_html = []
            for pat in snap.patterns:
                pat_color = GREEN if pat.bias == "BULLISH" else RED
                time_str = pat.time.strftime("%H:%M") if hasattr(pat.time, "strftime") else str(pat.time)

                confluent_cnt = len(pat.confluent_concepts)
                confluence_label = (
                    f"<span class='pa-pill' style='background:rgba(16, 185, 129, 0.15); color:{GREEN}; border:1px solid {GREEN};'>{confluent_cnt} CONFLUENCES</span>"
                    if confluent_cnt >= 2 else
                    "<span class='pa-pill' style='background:rgba(239, 68, 68, 0.15); color:#f87171; border:1px solid #f87171;'>SINGLE SIGNAL — NOT CONFIRMED</span>"
                )

                pats_list_html.append(f"""
                <div style="padding: 8px 10px; background: #0f172a; border-radius: 4px; margin-bottom: 8px; border-left: 3px solid {pat_color};">
                  <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:700; color:{pat_color}; font-size:12px;">
                      {time_str} · {pat.name} · {pat.status}
                    </span>
                    <span>{confluence_label}</span>
                  </div>
                  <div style="font-size:11.5px; color:#cbd5e1; margin-top:4px; font-family:monospace;">
                    Key S/R Level: <b>{pat.level:,.1f}</b> | Neckline: <b>{pat.neckline:,.1f}</b>
                  </div>
                  <div style="font-size:11px; color:#94a3b8; margin-top:3px; line-height:1.4;">
                    <b>S/R Context:</b> {pat.underlying_sr_meaning}
                  </div>
                  <div style="font-size:10.5px; color:#64748b; margin-top:2px;">
                    <i>{pat.confirmation_detail}</i>
                  </div>
                  <div style="font-size:10.5px; margin-top:4px; color:#cbd5e1;">
                    <span style="color:#34d399;">✓ Agrees:</span> {', '.join(pat.confluent_concepts) or 'None'}<br>
                    <span style="color:#f87171;">✗ Conflicts:</span> {', '.join(pat.conflicting_concepts) or 'None'}
                  </div>
                </div>
                """)
            patterns_inner_html = "".join(pats_list_html)

        patterns_card_html = f"""
        <div class="pa-card {muted_class}">
          <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:6px; margin-bottom:8px;">
            <span style="font-size:12px; font-weight:700; color:#e2e8f0; text-transform:uppercase;">4 · Multi-Candle Patterns (S/R Over Multiple Bars)</span>
            <span class="pa-pill" style="background:#0f172a; color:#f59e0b; border:1px solid #334155;">2-CLOSE RULE</span>
          </div>
          {patterns_inner_html}
        </div>
        """
        _render_html(patterns_card_html)

        # ── CONCEPT 2: RSI CROSSING RULE CONTEXT CARD ─────────────────────
        rsi_card_html = f"""
        <div class="pa-card {muted_class}">
          <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:6px; margin-bottom:6px;">
            <span style="font-size:12px; font-weight:700; color:#e2e8f0; text-transform:uppercase;">2 · RSI Crossing Rule Architecture</span>
            <span class="pa-pill" style="background:#0f172a; color:#e2e8f0; border:1px solid #334155;">WILDER'S 14</span>
          </div>
          <div style="font-size:11.5px; color:#cbd5e1; line-height:1.5;">
            <div style="margin-bottom:4px;">
              <b>Current Read:</b> <span style="color:{rsi_color}; font-family:monospace; font-weight:700;">{snap.rsi.detail}</span>
            </div>
            <div style="font-size:11px; color:#94a3b8;">
              <b>Source Rule:</b> {snap.rsi.rule_reminder}
            </div>
          </div>
        </div>
        """
        _render_html(rsi_card_html)
