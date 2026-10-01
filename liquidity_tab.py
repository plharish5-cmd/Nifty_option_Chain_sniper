"""
liquidity_tab.py — Institutional Liquidity & Smart Money Terminal UI Tab.

WHAT THIS TAB CANNOT DO:
────────────────────────
1. Stop-loss pools are INFERRED from conventional order placement, NOT OBSERVED
   order book data. NSE publishes no resting-order depth. Stop placement is modelled
   by convention; if traders place stops elsewhere, the model is wrong.
2. A swept pool is an OBSERVATION, NOT A DIRECTIONAL TRIGGER. Price sweeping a pool
   and returning is routine. It becomes a setup only with displacement in the
   opposite direction AND a confirmed structure break.
3. It CANNOT eliminate the fractal pivot confirmation lag (N bars into the future).
   Pivots are knowable only after confirmed_at (bar t+N).
4. Claims, reclaims, and structure breaks require two consecutive closes. The newest
   candle can never confirm anything — it reports WAITING.
5. OTE is a confirmation, NEVER a standalone entry.
6. SMT Divergence requires a real continuous BANKNIFTY feed. If unavailable, it renders
   as UNAVAILABLE — needs BANKNIFTY feed. Proxy substitutions are strictly forbidden.
7. Judas Swing cannot be called during formation — it is reported only after reversal
   and displacement are fully confirmed.
8. Power of Three (AMD) is valid ONLY at a known pool, OI wall, or prior session extreme.
9. No win rates, profit factors, or invented statistics.
10. This tab does NOT feed the decision bar, alerts, or Strategy 1.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import liquidity_engine as le
from ui_utils import render_html, _render_html

# ═══════════════════════════════════════════════════════════════════════════
# Institutional Terminal Palette
# ═══════════════════════════════════════════════════════════════════════════

GREEN = "#0F9D58"     # Bullish
RED = "#E8503A"       # Bearish (Action required now)
AMBER = "#E65100"     # Unconfirmed / Waiting
GREY = "#8A8F98"      # Neutral / Inferred
DARK_CARD = "#1e293b"
DARK_BG = "#0f172a"
BORDER = "#334155"


# ═══════════════════════════════════════════════════════════════════════════
# Chart Builder
# ═══════════════════════════════════════════════════════════════════════════

def build_terminal_chart(
    df: pd.DataFrame,
    snap: le.LiquidityTerminalSnapshot,
    show_pools: bool = True,
    show_dr: bool = True,
    show_fvg_ob: bool = True,
    show_breakers: bool = True,
    show_windows: bool = True,
) -> go.Figure:
    """
    Builds the 500px full-width institutional candlestick chart.
    - Pools as horizontal bands with opacity scaled to weight
    - Swept pools marked with X and sweep time
    - Claimed pools drawn in dotted linestyle
    - FVGs and order blocks as filled rectangles (mitigated at 30% opacity)
    - Dealing range with equilibrium line and shaded OTE band
    - Displacement candles outlined
    - Session windows as faint vertical bands
    """
    fig = go.Figure()
    if df is None or df.empty:
        return fig

    times = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index)
    x_indices = list(range(len(df)))
    curr_i = len(df) - 1

    # 1. Candlesticks
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
            increasing_fillcolor="rgba(15, 157, 88, 0.6)",
            decreasing_fillcolor="rgba(232, 80, 58, 0.6)",
            showlegend=False,
        )
    )

    # 2. Session Windows as faint vertical bands
    if show_windows and snap.session_model.windows:
        for win in snap.session_model.windows:
            st_parts = [int(p) for p in win.start_time.split(":")]
            et_parts = [int(p) for p in win.end_time.split(":")]
            # Find matching bar indices
            idx_matches = [
                i for i in range(len(df))
                if times.iloc[i].time().hour == st_parts[0] and times.iloc[i].time().minute == st_parts[1]
            ]
            if idx_matches:
                w_x0 = idx_matches[0]
                fig.add_vline(
                    x=w_x0,
                    line_width=1,
                    line_dash="dot",
                    line_color="rgba(138, 143, 152, 0.35)",
                    annotation_text=win.start_time,
                    annotation_position="top left",
                    annotation_font=dict(size=8, color=GREY),
                )

    # 3. Dealing Range & OTE Band
    if show_dr and snap.dealing_range.size > 1.0:
        dr = snap.dealing_range
        # Equilibrium line (0.50)
        fig.add_shape(
            type="line",
            x0=0,
            x1=curr_i,
            y0=dr.equilibrium,
            y1=dr.equilibrium,
            line=dict(width=1.2, color=GREY, dash="dash"),
        )
        fig.add_annotation(
            x=curr_i,
            y=dr.equilibrium,
            text=f"EQ 50% ({dr.equilibrium:,.1f})",
            showarrow=False,
            font=dict(size=9, color=GREY),
            xanchor="left",
        )
        # OTE Band (0.618 to 0.790) Shaded
        fig.add_shape(
            type="rect",
            x0=0,
            x1=curr_i,
            y0=dr.ote_low,
            y1=dr.ote_high,
            fillcolor="rgba(230, 81, 0, 0.12)",
            line=dict(width=1, color=AMBER, dash="dot"),
        )
        fig.add_annotation(
            x=curr_i,
            y=(dr.ote_low + dr.ote_high) / 2.0,
            text=f"OTE (62–79%)",
            showarrow=False,
            font=dict(size=8.5, color=AMBER),
            xanchor="left",
        )

    # 4. FVGs and Order Blocks
    if show_fvg_ob:
        # Fair Value Gaps
        for g in snap.gaps[-8:]:
            is_bull = g.direction == "BULLISH"
            fill_col = "rgba(15, 157, 88, 0.25)" if is_bull else "rgba(232, 80, 58, 0.25)"
            line_col = GREEN if is_bull else RED
            # Mitigated ones drawn at 30% opacity
            if not g.open:
                fill_col = "rgba(15, 157, 88, 0.08)" if is_bull else "rgba(232, 80, 58, 0.08)"
                line_col = "rgba(138, 143, 152, 0.4)"

            fig.add_shape(
                type="rect",
                x0=g.i,
                x1=curr_i,
                y0=g.low,
                y1=g.high,
                fillcolor=fill_col,
                line=dict(width=1, color=line_col),
            )

        # Order Blocks
        for ob in snap.order_blocks[-6:]:
            is_bull = ob.direction == "BULLISH"
            fill_col = "rgba(15, 157, 88, 0.30)" if is_bull else "rgba(232, 80, 58, 0.30)"
            line_col = GREEN if is_bull else RED
            if not ob.open:
                fill_col = "rgba(138, 143, 152, 0.08)"
                line_col = "rgba(138, 143, 152, 0.3)"

            fig.add_shape(
                type="rect",
                x0=ob.i,
                x1=curr_i,
                y0=ob.low,
                y1=ob.high,
                fillcolor=fill_col,
                line=dict(width=1.2, color=line_col),
            )

    # 5. Breakers and Mitigation Blocks
    if show_breakers and snap.breaker_mitigation.items:
        for bm in snap.breaker_mitigation.items[-4:]:
            b_col = GREEN if bm.direction == "BULLISH" else RED
            dash_st = "solid" if bm.state == "ACTIVE" else "dot"
            fig.add_shape(
                type="rect",
                x0=bm.broken_bar_i or 0,
                x1=curr_i,
                y0=bm.bottom,
                y1=bm.top,
                fillcolor="rgba(168, 85, 247, 0.15)",
                line=dict(width=1.2, color="#a855f7", dash=dash_st),
            )
            fig.add_annotation(
                x=curr_i,
                y=bm.mid,
                text=f"{bm.block_type} ({bm.mid:,.0f})",
                showarrow=False,
                font=dict(size=8.5, color="#c084fc"),
                xanchor="left",
            )

    # 6. Stop Pools as Horizontal Bands (Opacity scaled to weight)
    if show_pools and snap.pool_map.pools:
        for p in snap.pool_map.pools[:8]:
            norm_weight = min(max(p.weight / 8.0, 0.15), 0.70)
            is_buy = p.pool_side == "BUY_STOPS"
            base_col = RED if is_buy else GREEN
            band_half = 3.0

            # Line style based on state
            if p.state == "CLAIMED":
                line_dash = "dot"
                line_col = GREY
                fill_col = "rgba(138, 143, 152, 0.10)"
            elif p.state == "SWEPT":
                line_dash = "dash"
                line_col = AMBER
                fill_col = "rgba(230, 81, 0, 0.15)"
            elif p.state == "WAITING":
                line_dash = "dashdot"
                line_col = AMBER
                fill_col = "rgba(230, 81, 0, 0.20)"
            else:  # RESTING
                line_dash = "solid"
                line_col = base_col
                fill_col = f"rgba({232 if is_buy else 15}, {80 if is_buy else 157}, {58 if is_buy else 88}, {norm_weight*0.4:.2f})"

            fig.add_shape(
                type="rect",
                x0=0,
                x1=curr_i,
                y0=p.pool_price - band_half,
                y1=p.pool_price + band_half,
                fillcolor=fill_col,
                line=dict(width=1.2, color=line_col, dash=line_dash),
            )

            # Swept marker (X with sweep time)
            if p.state == "SWEPT":
                t_str = p.swept_time.strftime("%H:%M") if p.swept_time else "Swept"
                fig.add_annotation(
                    x=curr_i - 2,
                    y=p.pool_price,
                    text=f"✗ SWEPT ({t_str})",
                    showarrow=False,
                    font=dict(size=9, color=AMBER, family="monospace"),
                    bgcolor="rgba(15, 23, 42, 0.85)",
                    bordercolor=AMBER,
                    borderwidth=1,
                    xanchor="right",
                )
            else:
                fig.add_annotation(
                    x=curr_i,
                    y=p.pool_price,
                    text=f"{p.pool_type} [{p.state}] ({p.pool_price:,.0f})",
                    showarrow=False,
                    font=dict(size=9, color=line_col, family="monospace"),
                    xanchor="left",
                )

    # 7. Outline Displacement Candles
    if snap.displacement.score >= 3 and snap.displacement.candle_idx >= 0:
        disp_i = snap.displacement.candle_idx
        if disp_i < len(df):
            disp_col = GREEN if snap.displacement.direction == "BULLISH" else RED
            fig.add_annotation(
                x=disp_i,
                y=df["high"].iloc[disp_i] if snap.displacement.direction == "BULLISH" else df["low"].iloc[disp_i],
                text=f"{'▲' if snap.displacement.direction == 'BULLISH' else '▼'} DISP ({snap.displacement.score}/4)",
                showarrow=True,
                arrowhead=2,
                arrowsize=1,
                arrowcolor=disp_col,
                font=dict(size=9, color=disp_col, family="monospace"),
                bgcolor="rgba(15, 23, 42, 0.9)",
                bordercolor=disp_col,
                borderwidth=1,
                ay=-25 if snap.displacement.direction == "BULLISH" else 25,
            )

    # Formatting axes and layout (strictly 500px, full width)
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
        height=500,
        margin=dict(l=10, r=90, t=15, b=20),
        paper_bgcolor=DARK_BG,
        plot_bgcolor=DARK_BG,
        font=dict(family="monospace", color="#cbd5e1", size=11),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════
# Main Tab Render Function
# ═══════════════════════════════════════════════════════════════════════════

def render(ctx: Any = None, **kwargs) -> None:
    """
    Main entrypoint for the Liquidity & Smart Money Terminal.
    Accepts a single `ctx` context object or fallback kwargs.
    Layout: HUD strip (top) -> 500px Liquidity map (centre) -> Detail rail expanders (below).
    """
    # 1. Resolve Data Contracts
    pa_frames = getattr(ctx, "pa_frames", None) or kwargs.get("pa_frames")
    spot = getattr(ctx, "spot", None) or kwargs.get("spot") or 24150.0
    df = getattr(ctx, "df", None) or kwargs.get("df")
    ohlc = getattr(ctx, "ohlc", None) or kwargs.get("ohlc")
    read_obj = getattr(ctx, "read", None)
    levels = getattr(read_obj, "levels", None) if read_obj else getattr(ctx, "chain_levels", None) or kwargs.get("levels")
    banknifty_candles = getattr(ctx, "extra", {}).get("banknifty_candles", None) if hasattr(ctx, "extra") and isinstance(ctx.extra, dict) else kwargs.get("banknifty_candles")
    if banknifty_candles is None:
        try:
            import candle_source as _csrc
            banknifty_candles = _csrc.fetch_banknifty(days_back=2)
        except Exception:
            banknifty_candles = None

    # 2. Institutional CSS Injection
    _render_html(
        """
        <style>
        .liq-hud-box {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 4px;
            padding: 8px 10px;
            min-height: 70px;
        }
        .liq-hud-title {
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #8A8F98;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .liq-hud-val {
            font-size: 17px;
            font-weight: 800;
            font-family: monospace;
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .liq-hud-sub {
            font-size: 10px;
            color: #8A8F98;
            font-family: monospace;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .liq-prov-line {
            font-size: 10px;
            color: #8A8F98;
            font-family: monospace;
            padding: 4px 6px;
            background: rgba(15, 23, 42, 0.6);
            border-left: 2px solid #8A8F98;
            margin-bottom: 8px;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    # 3. Controls in the Sidebar (Not Inline)
    st.sidebar.markdown("### Liquidity & SMC Controls")
    sel_tf = st.sidebar.selectbox(
        "Timeframe",
        ["15m", "5m", "1m"],
        index=0,
        key="liq_tf_sel",
        help="Higher timeframes anchor structural pools; lower timeframes give entry timing."
    )
    pivot_n = st.sidebar.slider(
        "Pivot Window (N)",
        min_value=1,
        max_value=5,
        value=2,
        key="liq_pivot_n",
        help="Fractal lag: pivot at bar t is confirmed at bar t+N."
    )
    touch_tol = st.sidebar.slider(
        "Touch Tolerance (pts)",
        min_value=4.0,
        max_value=16.0,
        value=8.0,
        step=1.0,
        key="liq_touch_tol",
        help="Distance in points from level to count as a touch."
    )
    offset_mult = st.sidebar.slider(
        "Pool Offset Multiplier",
        min_value=0.2,
        max_value=1.0,
        value=0.5,
        step=0.1,
        key="liq_offset_mult",
        help="Stop pool distance beyond level = max(mult * ATR, 10 pts)."
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown("**Chart Overlays**")
    chk_pools = st.sidebar.checkbox("Resting Stop Pools", value=True, key="liq_chk_pools")
    chk_dr = st.sidebar.checkbox("Dealing Range & OTE", value=True, key="liq_chk_dr")
    chk_fvg_ob = st.sidebar.checkbox("FVGs & Order Blocks", value=True, key="liq_chk_fvg_ob")
    chk_breakers = st.sidebar.checkbox("Breakers & BPRs", value=True, key="liq_chk_breakers")
    chk_windows = st.sidebar.checkbox("Session Windows", value=True, key="liq_chk_windows")

    # 4. Resolve Active DataFrame
    curr_df = None
    if pa_frames and isinstance(pa_frames, dict):
        curr_df = pa_frames.get(sel_tf)
    if curr_df is None and df is not None:
        curr_df = df

    # 5. Run Engine Evaluation
    custom_rules = {
        "pivot_window_n": pivot_n,
        "touch_tolerance_pts": touch_tol,
        "pool_offset_atr_mult": offset_mult,
    }

    snap = le.evaluate_liquidity_terminal(
        candles_df=curr_df,
        pa_frames=pa_frames,
        spot=spot,
        ohlc_daily=ohlc,
        levels=levels,
        banknifty_candles=banknifty_candles,
        rules=custom_rules,
        timeframe=sel_tf,
    )

    # Display any defensive warnings if present
    if snap.warnings:
        for w in snap.warnings:
            st.warning(f"⚠️ {w}")
        if curr_df is None or len(curr_df) < 5:
            st.info("Awaiting live continuous candle feed. Terminal standing by.")
            return

    # ═══════════════════════════════════════════════════════════════════════
    # ZONE 1: HUD STRIP (Top, Always Visible — 6 Compact Metrics in 1 Row)
    # ═══════════════════════════════════════════════════════════════════════
    h_col1, h_col2, h_col3, h_col4, h_col5, h_col6 = st.columns(6)

    # 1. Session Window
    with h_col1:
        win_label = snap.session_model.active_window.name if snap.session_model.active_window else "After-Hours"
        win_char = snap.session_model.active_window.label if snap.session_model.active_window else "Closed"
        win_col = GREEN if ("Primary" in win_char or "Second" in win_char) else GREY
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Session Window</div>
          <div class="liq-hud-val" style="color: {win_col};">{win_label}</div>
          <div class="liq-hud-sub">{win_char}</div>
        </div>
        """)

    # 2. Structure State
    with h_col2:
        st_val = snap.structure.trend
        st_col = GREEN if st_val == "BULLISH" else (RED if st_val == "BEARISH" else GREY)
        disp_sub = f"Disp {snap.displacement.score}/4"
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Structure State</div>
          <div class="liq-hud-val" style="color: {st_col};">{st_val}</div>
          <div class="liq-hud-sub">{disp_sub}</div>
        </div>
        """)

    # 3. Dealing Range
    with h_col3:
        dr = snap.dealing_range
        dr_col = GREEN if dr.zone == "DISCOUNT" else (RED if dr.zone == "PREMIUM" else GREY)
        dr_sub = "IN OTE" if dr.in_ote else f"EQ {dr.equilibrium:,.0f}"
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Dealing Range</div>
          <div class="liq-hud-val" style="color: {dr_col};">{dr.zone} ({dr.position_pct*100:.0f}%)</div>
          <div class="liq-hud-sub">{dr_sub}</div>
        </div>
        """)

    # 4. Nearest Above
    with h_col4:
        na = snap.pool_map.nearest_above
        if na:
            na_val = f"{na.pool_price:,.0f}"
            na_col = RED if na.distance_pts <= 25.0 else GREY
            na_sub = f"+{na.distance_pts:,.0f} pts · {na.pool_type}"
        else:
            na_val = "None"
            na_col = GREY
            na_sub = "No pool above"
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Nearest Above</div>
          <div class="liq-hud-val" style="color: {na_col};">{na_val}</div>
          <div class="liq-hud-sub">{na_sub}</div>
        </div>
        """)

    # 5. Nearest Below
    with h_col5:
        nb = snap.pool_map.nearest_below
        if nb:
            nb_val = f"{nb.pool_price:,.0f}"
            nb_col = GREEN if abs(nb.distance_pts) <= 25.0 else GREY
            nb_sub = f"{nb.distance_pts:,.0f} pts · {nb.pool_type}"
        else:
            nb_val = "None"
            nb_col = GREY
            nb_sub = "No pool below"
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Nearest Below</div>
          <div class="liq-hud-val" style="color: {nb_col};">{nb_val}</div>
          <div class="liq-hud-sub">{nb_sub}</div>
        </div>
        """)

    # 6. Confluence Count
    with h_col6:
        confl = snap.confluence
        c_col = GREEN if (confl.total_factors >= 4 and confl.sentiment == "BULLISH") else \
                (RED if (confl.total_factors >= 4 and confl.sentiment == "BEARISH") else GREY)
        _render_html(f"""
        <div class="liq-hud-box">
          <div class="liq-hud-title">Zone Confluence</div>
          <div class="liq-hud-val" style="color: {c_col};">{confl.total_factors} Factors</div>
          <div class="liq-hud-sub">{confl.strong_factors_count} Strong @ {confl.zone_price:,.0f}</div>
        </div>
        """)

    # ═══════════════════════════════════════════════════════════════════════
    # ZONE 2: LIQUIDITY MAP (Centre, Full Width, 500px)
    # ═══════════════════════════════════════════════════════════════════════
    chart_df = curr_df if (curr_df is not None and not curr_df.empty) else df
    fig = build_terminal_chart(
        df=chart_df,
        snap=snap,
        show_pools=chk_pools,
        show_dr=chk_dr,
        show_fvg_ob=chk_fvg_ob,
        show_breakers=chk_breakers,
        show_windows=chk_windows,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    # Mandatory provenance note directly below chart
    _render_html(f"""
    <div class="liq-prov-line">
      PROVENANCE: {snap.pool_map.provenance}
    </div>
    """)

    # ═══════════════════════════════════════════════════════════════════════
    # ZONE 3: DETAIL RAIL (Below — Collapsed Expanders with Verdict in Header)
    # ═══════════════════════════════════════════════════════════════════════

    # 1. Pools Expander
    pm = snap.pool_map
    with st.expander(f"Pools ({pm.resting_count} resting · {pm.swept_count} swept · {pm.claimed_count} claimed)"):
        _render_html(f"<div class='liq-prov-line'>{pm.provenance}</div>")
        if pm.pools:
            pool_data = []
            for p in pm.pools:
                pool_data.append({
                    "Pool ID": p.pool_id,
                    "Type": p.pool_type,
                    "Level": f"{p.level:,.1f}",
                    "Stops Price": f"{p.pool_price:,.1f}",
                    "Side": p.pool_side,
                    "Dist (pts)": f"{p.distance_pts:+.1f}",
                    "Weight": f"{p.weight:.1f}",
                    "State": p.state,
                    "Touches": p.touch_count,
                    "Timeframe": p.timeframe,
                    "Agreements": ", ".join(p.family_agreements),
                })
            st.dataframe(pd.DataFrame(pool_data), use_container_width=True, hide_index=True)
        else:
            st.info("No structural liquidity pools detected in proximity.")

    # 2. Structure & Displacement Expander
    disp = snap.displacement
    with st.expander(f"Structure: {snap.structure.trend} · Displacement {disp.score}/4 ({disp.verdict})"):
        _render_html(f"<div class='liq-prov-line'>{disp.provenance}</div>")
        st.markdown(f"**Displacement Analysis:** {disp.note}")
        d_col1, d_col2, d_col3, d_col4 = st.columns(4)
        with d_col1:
            st.metric("Large Body", "YES" if disp.large_body else "NO")
        with d_col2:
            st.metric("Small Wicks", "YES" if disp.small_wicks else "NO")
        with d_col3:
            st.metric("FVG Created", "YES" if disp.fvg_created else "NO")
        with d_col4:
            st.metric("2 Closes in Dir", "YES" if disp.two_closes_in_dir else "NO")
        st.caption("Rule: A structure break without conviction displacement (>=3/4) is classified as drift.")

    # 3. Dealing Range & OTE Expander
    dr = snap.dealing_range
    with st.expander(f"Dealing Range: {dr.zone} ({dr.position_pct*100:.0f}%) · OTE {dr.ote_low:,.0f}–{dr.ote_high:,.0f}"):
        _render_html(f"<div class='liq-prov-line'>{dr.provenance}</div>")
        st.markdown(f"**Status:** {dr.note}")
        b_cols = st.columns(len(dr.bands))
        for col, (fib, price) in zip(b_cols, dr.bands.items()):
            with col:
                is_eq = fib == "0.500"
                is_ote = fib in ("0.618", "0.705", "0.790")
                lbl = f"{fib} (EQ)" if is_eq else (f"{fib} (OTE)" if is_ote else fib)
                st.metric(lbl, f"{price:,.1f}")

    # 4. Confluence Scoring Expander
    confl = snap.confluence
    with st.expander(f"Confluence at {confl.zone_name}: {confl.total_factors} factors ({confl.strong_factors_count} Strong)"):
        _render_html(f"<div class='liq-prov-line'>{confl.provenance}</div>")
        st.markdown(f"**Verdict:** {confl.verdict} · **Dominant Bias:** {confl.sentiment}")
        if confl.factors:
            c_data = []
            for f in confl.factors:
                c_data.append({
                    "Factor": f.name,
                    "Strength": f.strength,
                    "Sentiment": f.sentiment,
                    "Evidence / Details": f.detail,
                })
            st.dataframe(pd.DataFrame(c_data), use_container_width=True, hide_index=True)
        st.caption("Rule: Factors are reported individually without collapsing into a single percentage.")

    # 5. Breakers & Mitigation Blocks Expander
    bm = snap.breaker_mitigation
    with st.expander(f"Breakers & Mitigation: {bm.breaker_count} Breakers · {bm.mitigation_count} Mitigation"):
        _render_html(f"<div class='liq-prov-line'>{bm.provenance}</div>")
        if bm.items:
            bm_data = []
            for it in bm.items:
                bm_data.append({
                    "Type": it.block_type,
                    "New Direction": it.direction,
                    "Zone": f"{it.bottom:,.1f}–{it.top:,.1f}",
                    "Midpoint": f"{it.mid:,.1f}",
                    "State": it.state,
                    "Swept Prior Extreme?": "YES (Breaker)" if it.origin_took_liquidity else "NO (Mitigation)",
                    "Retested": "YES" if it.retested else "NO",
                })
            st.dataframe(pd.DataFrame(bm_data), use_container_width=True, hide_index=True)
        else:
            st.info("No active Breaker or Mitigation blocks detected from failed order blocks.")

    # 6. Balanced Price Range & Consequent Encroachment
    bpr = snap.bpr_ce
    with st.expander(f"BPR & Consequent Encroachment: {bpr.bpr_count} BPRs · {bpr.ce_count} CEs"):
        _render_html(f"<div class='liq-prov-line'>{bpr.provenance}</div>")
        st.markdown(f"**Status:** {bpr.note}")
        if bpr.bprs:
            st.markdown("**Balanced Price Ranges (Opposing FVG Overlaps):**")
            bpr_df = pd.DataFrame(bpr.bprs)
            st.dataframe(bpr_df, use_container_width=True, hide_index=True)
        if bpr.ces:
            st.markdown("**Consequent Encroachments (50% Midpoints):**")
            ce_df = pd.DataFrame(bpr.ces)
            st.dataframe(ce_df, use_container_width=True, hide_index=True)

    # 7. Turtle Soup / False Break
    tsoup = snap.turtle_soup
    tsoup_header = f"Turtle Soup: {tsoup.sentiment} on {tsoup.level_name} ({tsoup.reclaim_state})" if tsoup.detected else "Turtle Soup: None detected"
    with st.expander(tsoup_header):
        _render_html(f"<div class='liq-prov-line'>{tsoup.provenance}</div>")
        if tsoup.detected:
            st.markdown(f"**Signal:** {tsoup.note}")
            t_col1, t_col2, t_col3 = st.columns(3)
            with t_col1:
                st.metric("Level Raided", f"{tsoup.level_name} ({tsoup.level_price:,.1f})")
            with t_col2:
                st.metric("Bars Outside", str(tsoup.bars_outside))
            with t_col3:
                st.metric("Reclaim State", tsoup.reclaim_state)
        else:
            st.info(tsoup.note)

    # 8. Session Model (NSE Specific)
    sm = snap.session_model
    with st.expander(f"Session Model: {sm.active_window.name if sm.active_window else 'Closed'} · Range: {sm.day_range:,.1f} pts · {sm.pending_extreme} Pending"):
        _render_html(f"<div class='liq-prov-line'>{sm.provenance}</div>")
        st.markdown(f"**Session Read:** {sm.note}")
        if sm.windows:
            w_data = []
            for w in sm.windows:
                w_data.append({
                    "Window Time": w.name,
                    "Session Phase": w.label,
                    "High": f"{w.high:,.1f}" if w.high else "-",
                    "Low": f"{w.low:,.1f}" if w.low else "-",
                    "Range (pts)": f"{w.range_pts:,.1f}" if w.range_pts else "-",
                    "Status": "ACTIVE" if w.is_active else ("COMPLETED" if w.is_completed else "PENDING"),
                    "Character": w.character,
                })
            st.dataframe(pd.DataFrame(w_data), use_container_width=True, hide_index=True)

    # 9. Judas Swing
    judas = snap.judas_swing
    judas_header = f"Judas Swing: {judas.direction} ({judas.status})" if judas.detected else "Judas Swing: None detected"
    with st.expander(judas_header):
        _render_html(f"<div class='liq-prov-line'>{judas.provenance}</div>")
        if judas.detected:
            st.markdown(f"**Confirmed Setup:** {judas.note}")
            j_col1, j_col2 = st.columns(2)
            with j_col1:
                st.metric("Initial False Move", f"{judas.initial_move_pts:.1f} pts ({judas.initial_move_atr:.1f} ATR)")
            with j_col2:
                st.metric("Reversal Status", judas.status)
        else:
            st.info(judas.note)
            st.caption("Rule: Reported strictly after confirmed reversal; calling during initial move is indistinguishable from trend.")

    # 10. SMT Divergence (NIFTY vs BANKNIFTY)
    smt = snap.smt_divergence
    with st.expander(f"SMT Divergence: {smt.status_label}"):
        _render_html(f"<div class='liq-prov-line'>{smt.provenance}</div>")
        if smt.available:
            st.markdown(f"**SMT Read:** {smt.note}")
            s_col1, s_col2 = st.columns(2)
            with s_col1:
                st.metric("NIFTY Swing", smt.nifty_swing)
            with s_col2:
                st.metric("BANKNIFTY Swing", smt.banknifty_swing)
        else:
            st.warning(f"⚠️ {smt.status_label}")
            st.caption("Requires continuous BANKNIFTY candle feed. Proxy substitutions are strictly forbidden.")

    # 11. Power of Three (AMD)
    amd = snap.power_of_three
    amd_header = f"Power of Three (AMD): {amd.phase} ({amd.direction})" if amd.detected else "Power of Three (AMD): None active"
    with st.expander(amd_header):
        _render_html(f"<div class='liq-prov-line'>{amd.provenance}</div>")
        if amd.detected:
            st.markdown(f"**AMD Setup:** {amd.note}")
            a_col1, a_col2 = st.columns(2)
            with a_col1:
                st.metric("Anchor Level", f"{amd.anchor_level_name} ({amd.anchor_price:,.1f})")
            with a_col2:
                st.metric("Consolidation Range", f"{amd.consolidation_range[0]:,.0f}–{amd.consolidation_range[1]:,.0f}")
        else:
            st.info(amd.note)
            st.caption("Rule: AMD is valid ONLY at a known pool, OI wall, or prior session extreme.")

    # 12. Inducement
    ind = snap.inducement
    with st.expander(f"Inducement: {ind.quality_verdict}"):
        _render_html(f"<div class='liq-prov-line'>{ind.provenance}</div>")
        st.markdown(f"**Inducement Status:** {ind.note}")
        if ind.detected:
            i_col1, i_col2 = st.columns(2)
            with i_col1:
                st.metric("Minor Pivot Price", f"{ind.minor_pivot_price:,.1f}" if ind.minor_pivot_price else "-")
            with i_col2:
                st.metric("Inducement Swept?", "YES" if ind.is_taken else "NO")
        st.caption("Rule: Zones reached without inducement taken first are lower quality.")
