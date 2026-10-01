"""
views/oi_timeline_tab.py — Per-Strike Intraday OI Change Timeline View.
========================================================================

Visualizes per-strike cumulative intraday OI change (dCE, dPE) from market open to close
with small multiples per strike, orthogonal dominance and flow state indicators,
dual quadrant disambiguation badges (cumulative vs local 15m), and institutional veto callouts.

Follows project conventions:
- Pure Streamlit + Plotly.
- Color scheme: Red (#EF4444) for CE, Green (#10B981) for PE.
- All thresholds read from strategy1_rules.json under "oi_timeline".
- Never modifies dashboard.py.
"""

from __future__ import annotations

print(f"[views.oi_timeline_tab] loaded from {__file__}")

from datetime import date, datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import oi_timeline
import store
from ui_utils import render_html

log = logging.getLogger("views.oi_timeline_tab")

# ═══════════════════════════════════════════════════════════════════════════
# Institutional Dark Theme Palette
# ═══════════════════════════════════════════════════════════════════════════
BG_CONTAINER = "#0b1120"
BG_CARD = "#0f172a"
BG_CARD_INNER = "#1e293b"
BORDER_DARK = "#334155"
TEXT_PRIMARY = "#f8fafc"
TEXT_MUTED = "#94a3b8"
CRIMSON_CE = "#ef4444"
EMERALD_PE = "#10b981"
AMBER_WARNING = "#f59e0b"
CYAN_ACCENT = "#00e5ff"
PURPLE_ACCENT = "#a855f7"
SLATE_NEUTRAL = "#64748b"


@st.cache_data(ttl=60)
def _list_snapshot_sessions() -> List[Tuple[date, str]]:
    """Lists available snapshot dates quickly from data/snapshots/ without loading all parquet files."""
    snap_dir = Path("data/snapshots")
    if not snap_dir.exists():
        return []

    date_folders = sorted([d for d in snap_dir.iterdir() if d.is_dir()], reverse=True)
    sessions = []
    today = date.today()

    for d in date_folders:
        try:
            sess_d = datetime.strptime(d.name, "%Y-%m-%d").date()
            if sess_d == today:
                tag = "🔴 LIVE / TODAY"
            else:
                tag = "HISTORICAL"
            file_count = len(list(d.glob("*.parquet")))
            label = f"{d.name} ({tag} | {file_count} snapshots)"
            sessions.append((sess_d, label))
        except Exception as err:
            log.warning("Could not parse session %s: %s", d.name, err)
            continue
    return sessions


def _render_dominance_badge(dom: str) -> str:
    """Returns styled HTML badge for dominance state."""
    colors = {
        "PE_DOMINANT": (EMERALD_PE, "PUT WRITERS DOMINANT (SUPPORT BUILDING)"),
        "CE_DOMINANT": (CRIMSON_CE, "CALL WRITERS DOMINANT (RESISTANCE CAPPING)"),
        "BALANCED": (SLATE_NEUTRAL, "BALANCED BOOK (NO DIRECTIONAL EDGE)"),
        "CROSSOVER": (PURPLE_ACCENT, "CROSSOVER IN PROGRESS"),
        "WARMUP": (AMBER_WARNING, "EARLY SESSION WARMUP (< 09:50)"),
        "EOD_UNWIND": (AMBER_WARNING, "EOD POSITION SQUARING (≥ 15:00)"),
        "EXPIRY_DAY": (PURPLE_ACCENT, "EXPIRY DAY SESSION"),
        "STALE": (CRIMSON_CE, "STALE FEED"),
    }
    col, title = colors.get(dom, (SLATE_NEUTRAL, dom))
    return f"""
    <span style="background: rgba({int(col[1:3], 16)}, {int(col[3:5], 16)}, {int(col[5:7], 16)}, 0.15);
                 color: {col}; border: 1px solid {col}; border-radius: 4px; padding: 2px 8px;
                 font-weight: 700; font-size: 11px; font-family: monospace; letter-spacing: 0.5px;">
        {dom}
    </span>
    """


def _render_flow_badge(flow: str) -> str:
    """Returns styled HTML badge for flow direction."""
    colors = {
        "BUILDING": (EMERALD_PE, "▲ BUILDING"),
        "UNWINDING": (AMBER_WARNING, "▼ UNWINDING"),
        "FLAT": (SLATE_NEUTRAL, "■ FLAT"),
    }
    col, text = colors.get(flow, (SLATE_NEUTRAL, flow))
    return f"""
    <span style="background: rgba({int(col[1:3], 16)}, {int(col[3:5], 16)}, {int(col[5:7], 16)}, 0.15);
                 color: {col}; border: 1px solid {col}; border-radius: 4px; padding: 2px 8px;
                 font-weight: 600; font-size: 11px; font-family: monospace;">
        {text}
    </span>
    """


def _render_agreement_badge(agreement: str, disambig_read: str = "") -> str:
    """Returns styled HTML badge for quadrant agreement."""
    if agreement == "AGREE":
        return f"""
        <span style="background: rgba(16, 185, 129, 0.15); color: {EMERALD_PE}; border: 1px solid {EMERALD_PE};
                     border-radius: 4px; padding: 2px 6px; font-weight: 600; font-size: 10px; font-family: monospace;">
            AGREE
        </span>
        """
    return f"""
    <span title="{disambig_read}" style="background: rgba(239, 68, 68, 0.2); color: {CRIMSON_CE}; border: 1px solid {CRIMSON_CE};
                 border-radius: 4px; padding: 2px 6px; font-weight: 700; font-size: 10px; font-family: monospace;">
        ⚠️ DISAGREE
    </span>
    """


def _build_strike_chart(
    tl: oi_timeline.StrikeTimelineResult,
    rules: dict,
    display_units: str = "lakhs",
    display_lot: int = 65,
) -> go.Figure:
    """Builds a polished Plotly chart for a single strike's OI timeline matching OptionTick style."""
    ce_col = rules.get("colors", {}).get("ce", CRIMSON_CE)
    pe_col = rules.get("colors", {}).get("pe", EMERALD_PE)

    fig = go.Figure()

    if not tl.series:
        fig.add_annotation(
            text="No market hours data available for this strike",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(color=TEXT_MUTED, size=14),
        )
        fig.update_layout(
            paper_bgcolor=BG_CARD,
            plot_bgcolor=BG_CARD,
            height=280,
            margin=dict(l=40, r=20, t=30, b=30),
        )
        return fig

    times = [p.clock_time for p in tl.series]
    use_lakhs = (display_units.lower() == "lakhs")
    lot = display_lot if display_lot > 0 else 65
    mult = (lot / 100000.0) if use_lakhs else 1.0
    units_suffix = "L" if use_lakhs else " Contracts"
    y_title = "ΔOI (Lakhs)" if use_lakhs else "ΔOI (Contracts)"

    dce_vals = [p.ce_chg_oi * mult for p in tl.series]
    dpe_vals = [p.pe_chg_oi * mult for p in tl.series]

    # Primary CE line (Red)
    fig.add_trace(go.Scatter(
        x=times,
        y=dce_vals,
        mode="lines",
        name="CE ΔOI (Calls)",
        line=dict(color=ce_col, width=2.2),
        hovertemplate=f"CE ΔOI: %{{y:+,.2f}}{units_suffix}<br>Time: %{{x}}<extra></extra>" if use_lakhs else f"CE ΔOI: %{{y:+,.0f}}{units_suffix}<br>Time: %{{x}}<extra></extra>",
    ))

    # Primary PE line (Green)
    fig.add_trace(go.Scatter(
        x=times,
        y=dpe_vals,
        mode="lines",
        name="PE ΔOI (Puts)",
        line=dict(color=pe_col, width=2.2),
        hovertemplate=f"PE ΔOI: %{{y:+,.2f}}{units_suffix}<br>Time: %{{x}}<extra></extra>" if use_lakhs else f"PE ΔOI: %{{y:+,.0f}}{units_suffix}<br>Time: %{{x}}<extra></extra>",
    ))

    # Zero baseline
    fig.add_hline(y=0, line=dict(color="#475569", width=1, dash="dot"))

    # Warmup boundary (09:50)
    warmup_str = rules.get("warmup_until", "09:50")
    if warmup_str in times:
        fig.add_shape(
            type="line", x0=warmup_str, x1=warmup_str, y0=0, y1=1, yref="paper",
            line=dict(color=TEXT_MUTED, width=1, dash="dash"),
        )
        fig.add_annotation(
            x=warmup_str, y=1, yref="paper", text="Warmup (09:50)",
            showarrow=False, xanchor="right", yanchor="top",
            font=dict(color=TEXT_MUTED, size=9),
        )

    # EOD Unwind boundary (15:00)
    eod_str = rules.get("eod_from", "15:00")
    if eod_str in times:
        fig.add_shape(
            type="line", x0=eod_str, x1=eod_str, y0=0, y1=1, yref="paper",
            line=dict(color=AMBER_WARNING, width=1, dash="dash"),
        )
        fig.add_annotation(
            x=eod_str, y=1, yref="paper", text="EOD (15:00)",
            showarrow=False, xanchor="right", yanchor="top",
            font=dict(color=AMBER_WARNING, size=9),
        )

    # Crossover markers
    if tl.crossover_points:
        cross_times = [cp[0] for cp in tl.crossover_points if cp[0] in times]
        cross_spreads = [cp[2] * mult for cp in tl.crossover_points if cp[0] in times]
        cross_labels = [cp[1] for cp in tl.crossover_points if cp[0] in times]
        if cross_times:
            fig.add_trace(go.Scatter(
                x=cross_times,
                y=cross_spreads,
                mode="markers",
                name="Crossover",
                marker=dict(symbol="diamond", size=9, color=PURPLE_ACCENT, line=dict(width=1, color="#ffffff")),
                text=cross_labels,
                hovertemplate="<b>CROSSOVER</b><br>%{text}<br>Spread: %{y:,.2f}<extra></extra>" if use_lakhs else "<b>CROSSOVER</b><br>%{text}<br>Spread: %{y:,.0f}<extra></extra>",
            ))

    fig.update_layout(
        paper_bgcolor=BG_CARD,
        plot_bgcolor=BG_CARD,
        font=dict(color=TEXT_PRIMARY, size=10),
        margin=dict(l=45, r=20, t=30, b=30),
        height=280,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1.0,
            font=dict(size=10),
        ),
        xaxis=dict(
            showgrid=True,
            gridcolor=BORDER_DARK,
            nticks=12,
            tickfont=dict(size=9, color=TEXT_MUTED),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor=BORDER_DARK,
            tickfont=dict(size=9, color=TEXT_MUTED),
            title=dict(text=y_title, font=dict(size=10, color=TEXT_MUTED)),
            ticksuffix="L" if use_lakhs else "",
        ),
        hovermode="x unified",
    )
    return fig


def _build_net_writer_gap_chart(wg_res: oi_timeline.WriterGapSessionResult) -> go.Figure:
    """
    Builds the Net Writer Gap + Wall Migration chart (Section 7):
    - Subplot 1: Net CE (green) and Net PE (red) lines
    - Shaded band between Net CE and Net PE (red when CE above, green when PE above)
    - Confirmed crossover markers (▲ green for bullish, ▼ red for bearish) labelled with 'how'
    - Spot on secondary y-axis (cyan dotted line)
    - Thin vertical dashed line for basket-change points
    - Subplot 2: Small strip showing max_ce_add / max_pe_add strike over time
    """
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.75, 0.25],
        specs=[[{"secondary_y": True}], [{"secondary_y": False}]]
    )

    if not wg_res.poll_results:
        return fig

    times = [p.clock_time for p in wg_res.poll_results]
    net_ce = [p.net_ce_d for p in wg_res.poll_results]
    net_pe = [p.net_pe_d for p in wg_res.poll_results]
    spots = [p.spot for p in wg_res.poll_results]
    ce_add_walls = [p.wall_migration.max_ce_add_strike for p in wg_res.poll_results]
    pe_add_walls = [p.wall_migration.max_pe_add_strike for p in wg_res.poll_results]

    # Shaded band between NET CE and NET PE (red fill when CE above, green when PE above)
    for i in range(len(times) - 1):
        x_sub = [times[i], times[i+1]]
        ce_sub = [net_ce[i], net_ce[i+1]]
        pe_sub = [net_pe[i], net_pe[i+1]]
        mid_ce = (net_ce[i] + net_ce[i+1]) / 2.0
        mid_pe = (net_pe[i] + net_pe[i+1]) / 2.0
        fill_col = "rgba(239, 68, 68, 0.25)" if mid_ce >= mid_pe else "rgba(16, 185, 129, 0.25)"
        fig.add_trace(
            go.Scatter(
                x=x_sub + x_sub[::-1],
                y=ce_sub + pe_sub[::-1],
                fill="toself",
                fillcolor=fill_col,
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip"
            ),
            row=1, col=1, secondary_y=False
        )

    # Net CE ΔOI (Green)
    fig.add_trace(
        go.Scatter(
            x=times, y=net_ce,
            name="Net CE ΔOI",
            line=dict(color="#10b981", width=2.5),
            hovertemplate="Time: %{x}<br>Net CE ΔOI: %{y:+.1f}L<extra></extra>",
        ),
        row=1, col=1, secondary_y=False
    )

    # Net PE ΔOI (Red)
    fig.add_trace(
        go.Scatter(
            x=times, y=net_pe,
            name="Net PE ΔOI",
            line=dict(color="#ef4444", width=2.5),
            hovertemplate="Time: %{x}<br>Net PE ΔOI: %{y:+.1f}L<extra></extra>",
        ),
        row=1, col=1, secondary_y=False
    )

    # Spot on secondary y-axis
    fig.add_trace(
        go.Scatter(
            x=times, y=spots,
            name="NIFTY Spot",
            line=dict(color="#00e5ff", width=1.5, dash="dot"),
            hovertemplate="Time: %{x}<br>Spot: %{y:,.1f}<extra></extra>",
        ),
        row=1, col=1, secondary_y=True
    )

    # Confirmed crossovers markers
    for c in wg_res.crossovers:
        sym = "triangle-up" if c.direction == "BULLISH" else "triangle-down"
        col = "#10b981" if c.direction == "BULLISH" else "#ef4444"
        gap_val = c.net_gap
        fig.add_trace(
            go.Scatter(
                x=[c.clock_time], y=[gap_val],
                mode="markers+text",
                marker=dict(symbol=sym, size=15, color=col, line=dict(color="#ffffff", width=1)),
                text=[f" {c.direction}: {c.how}"],
                textposition="top center" if c.direction == "BULLISH" else "bottom center",
                textfont=dict(size=10, color=col),
                name=f"Cross: {c.clock_time} {c.direction}",
                hoverinfo="text",
                hovertext=f"🔀 WRITER CROSS {c.clock_time}<br>Direction: {c.direction}<br>How: {c.how}<br>Net Gap: {c.net_gap:+.1f}L<br>Spot: {c.spot:,.1f}",
                showlegend=False
            ),
            row=1, col=1, secondary_y=False
        )

    # Basket changes: thin vertical line
    for ts_bc, _ in wg_res.basket_changes:
        bc_t = ts_bc.strftime("%H:%M")
        if bc_t in times:
            fig.add_vline(
                x=bc_t,
                line_width=1,
                line_dash="dash",
                line_color="#a855f7",
                annotation_text="Basket change",
                annotation_position="top left",
                annotation_font_size=9,
                annotation_font_color="#a855f7"
            )

    # Subplot 2: Wall Migration Strip
    fig.add_trace(
        go.Scatter(
            x=times, y=ce_add_walls,
            name="Max CE Add Strike",
            mode="lines+markers",
            marker=dict(size=4),
            line=dict(color="#10b981", width=1.5),
            hovertemplate="Time: %{x}<br>Max CE Add: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1
    )
    fig.add_trace(
        go.Scatter(
            x=times, y=pe_add_walls,
            name="Max PE Add Strike",
            mode="lines+markers",
            marker=dict(size=4),
            line=dict(color="#ef4444", width=1.5),
            hovertemplate="Time: %{x}<br>Max PE Add: %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1
    )

    fig.update_layout(
        paper_bgcolor=BG_CARD,
        plot_bgcolor=BG_CONTAINER,
        margin=dict(l=50, r=50, t=30, b=30),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0.0,
            font=dict(size=10, color=TEXT_PRIMARY),
        ),
        xaxis=dict(showgrid=True, gridcolor=BORDER_DARK, nticks=12, tickfont=dict(size=9, color=TEXT_MUTED)),
        xaxis2=dict(showgrid=True, gridcolor=BORDER_DARK, nticks=12, tickfont=dict(size=9, color=TEXT_MUTED), title=dict(text="Time", font=dict(size=10, color=TEXT_MUTED))),
        yaxis=dict(showgrid=True, gridcolor=BORDER_DARK, tickfont=dict(size=9, color=TEXT_MUTED), title=dict(text="Net ΔOI (Lakhs)", font=dict(size=10, color=TEXT_MUTED)), ticksuffix="L"),
        yaxis2=dict(showgrid=False, tickfont=dict(size=9, color=CYAN_ACCENT), title=dict(text="NIFTY Spot", font=dict(size=10, color=CYAN_ACCENT)), overlaying="y", side="right"),
        yaxis3=dict(showgrid=True, gridcolor=BORDER_DARK, tickfont=dict(size=9, color=TEXT_MUTED), title=dict(text="Wall Strike", font=dict(size=9, color=TEXT_MUTED))),
        hovermode="x unified",
        height=520,
    )
    return fig


def render_oi_timeline_tab(client=None) -> None:
    """Main entrypoint for Per-Strike OI Change Timeline Streamlit tab."""
    rules = oi_timeline.load_rules()
    display_lot = int(rules.get("display_lot_size") or rules.get("lot_size") or 65)

    render_html(f"""
    <div style="background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 8px; padding: 14px 18px; margin-bottom: 15px;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
                <span style="font-size: 18px; font-weight: 700; color: {TEXT_PRIMARY}; font-family: monospace;">
                    ⚡ INTRADAY OI CHANGE TIMELINE
                </span>
                <span style="margin-left: 10px; font-size: 11px; background: rgba(0, 229, 255, 0.15); color: {CYAN_ACCENT};
                             border: 1px solid {CYAN_ACCENT}; border-radius: 4px; padding: 2px 8px; font-family: monospace;">
                    OPTIONTICK MULTIPLES & QUADRANTS
                </span>
            </div>
            <div style="font-size: 12px; color: {TEXT_MUTED}; font-family: monospace;">
                Mode: <b style="color: {EMERALD_PE};">{rules.get('interpretation_mode', 'combined').upper()}</b> &nbsp;|&nbsp;
                Lot Size: <b style="color: {CYAN_ACCENT};">{display_lot}</b> &nbsp;|&nbsp;
                Default Step: <b style="color: {CYAN_ACCENT};">{int(rules.get('default_strike_step', 100))} pts</b>
            </div>
        </div>
        <div style="font-size: 12px; color: {TEXT_MUTED}; margin-top: 6px;">
            Per-strike intraday cumulative open interest flow (dCE vs dPE). Anchored to current Nifty spot price with 100-point institutional round strikes and premium quadrant disambiguation.
        </div>
    </div>
    """)

    # 1. Date selector
    sessions = _list_snapshot_sessions()
    if not sessions:
        st.warning("No option chain snapshot directories found in data/snapshots/.")
        return

    col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2.2, 1.6, 1.0])
    with col_ctrl1:
        session_idx = st.selectbox(
            "Select Session Date",
            options=range(len(sessions)),
            format_func=lambda i: sessions[i][1],
            index=0,
            key="oi_timeline_sess_sel",
        )
        selected_date = sessions[session_idx][0]

    with col_ctrl2:
        view_layout = st.radio(
            "View Layout",
            options=["Grid (Small Multiples)", "Focused Tabbed View"],
            horizontal=True,
            key="oi_timeline_layout_sel",
        )

    with col_ctrl3:
        auto_csv = st.checkbox("Export CSV", value=True, help="Auto-logs evaluated points to data/oi_timeline/")

    # 2. Load snapshots for the selected date
    snaps = store.load_day(selected_date)
    if not snaps:
        st.error(f"No snapshot data available for {selected_date}.")
        return

    first_ts, first_spot, first_df = snaps[0]
    last_ts, last_spot, last_df = snaps[-1]
    all_strikes = sorted(last_df["strike"].unique())
    hundred_strikes = [s for s in all_strikes if s % 100 == 0]

    # 3. Strike Selection Controls (OptionTick Style)
    col_s1, col_s2, col_s3, col_s4 = st.columns([2.0, 1.2, 1.0, 1.0])
    with col_s1:
        strike_preset = st.selectbox(
            "Strike Spacing / Range Preset",
            options=[
                "100 Levels (Institutional Round Strikes)",
                "Active Range 23300 - 23600 (Current Spot)",
                "50 Levels (ATM Focus)",
                "Custom Multi-Strike Selection",
            ],
            index=0,
            key="oi_timeline_preset_sel",
        )
    with col_s2:
        atm_anchor_choice = st.selectbox(
            "ATM Spot Reference",
            options=[
                f"Current Spot ({last_spot:,.1f})",
                f"Market Open ({first_spot:,.1f})",
            ],
            index=0,
            key="oi_timeline_anchor_sel",
        )
    with col_s3:
        disp_units_choice = st.selectbox(
            "Display Units",
            options=["Lakhs (L)", "Contracts"],
            index=0,
            key="oi_timeline_units_sel",
        )
        units_mode = "lakhs" if "Lakhs" in disp_units_choice else "contracts"
    with col_s4:
        max_s_count = st.number_input(
            "Strike Count",
            min_value=3,
            max_value=12,
            value=5,
            step=1,
            key="oi_timeline_count_sel",
        )

    # 4. Resolve selected strikes based on mode
    ref_spot = last_spot if "Current" in atm_anchor_choice else first_spot

    if strike_preset.startswith("100 Levels"):
        atm_100 = round(ref_spot / 100.0) * 100.0
        half = int(max_s_count) // 2
        cand_strikes = [atm_100 + (i - half) * 100.0 for i in range(int(max_s_count))]
        selected_strikes = [s for s in cand_strikes if s in hundred_strikes]
        if not selected_strikes:
            selected_strikes = [s for s in cand_strikes if s in all_strikes]
        effective_step = 100.0

    elif "23300 - 23600" in strike_preset:
        cand_strikes = [23300.0, 23400.0, 23500.0, 23600.0]
        selected_strikes = [s for s in cand_strikes if s in all_strikes]
        if not selected_strikes:
            # Fallback if selected session is outside 23300-23600
            atm_100 = round(ref_spot / 100.0) * 100.0
            selected_strikes = [s for s in [atm_100 - 100, atm_100, atm_100 + 100, atm_100 + 200] if s in all_strikes]
        effective_step = 100.0

    elif strike_preset.startswith("50 Levels"):
        atm_50 = round(ref_spot / 50.0) * 50.0
        half = int(max_s_count) // 2
        cand_strikes = [atm_50 + (i - half) * 50.0 for i in range(int(max_s_count))]
        selected_strikes = [s for s in cand_strikes if s in all_strikes]
        effective_step = 50.0

    else:  # Custom selection
        default_strikes = [s for s in [23300.0, 23350.0, 23400.0, 23450.0, 23500.0] if s in all_strikes]
        if not default_strikes:
            atm_s = round(ref_spot / 50.0) * 50.0
            default_strikes = [s for s in [atm_s - 50, atm_s, atm_s + 50] if s in all_strikes]
        selected_strikes = st.multiselect(
            "Select Custom Strikes to Watch",
            options=all_strikes,
            default=default_strikes,
            format_func=lambda s: f"Strike {int(s)}",
            key="oi_timeline_custom_strikes",
        )
        effective_step = 100.0 if any(s % 100 == 0 for s in selected_strikes) else 50.0

    if not selected_strikes:
        selected_strikes = [round(ref_spot / 100.0) * 100.0]

    # 5. Active Strike Chips Banner (OptionTick Visual Match)
    chips_html = "".join([
        f"""<span style="background: rgba(0, 229, 255, 0.12); color: {CYAN_ACCENT}; border: 1px solid {CYAN_ACCENT};
                         border-radius: 14px; padding: 3px 10px; font-weight: 700; font-size: 11px;
                         font-family: monospace; display: inline-flex; align-items: center; margin: 2px 4px;">
            Strike : {int(s)}
        </span>"""
        for s in selected_strikes
    ])
    active_atm = selected_strikes[len(selected_strikes) // 2] if selected_strikes else ref_spot
    render_html(f"""
    <div style="background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 6px; padding: 8px 12px; margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
        <div style="display: flex; align-items: center; flex-wrap: wrap;">
            <span style="font-size: 11px; font-weight: 700; color: {TEXT_MUTED}; font-family: monospace; margin-right: 8px;">
                ACTIVE STRIKES ({len(selected_strikes)}):
            </span>
            {chips_html}
        </div>
        <div style="font-size: 11px; color: {TEXT_MUTED}; font-family: monospace;">
            Current Spot: <b style="color: {CYAN_ACCENT};">{last_spot:,.1f}</b> &nbsp;|&nbsp;
            Ref ATM: <b style="color: {EMERALD_PE};">{int(active_atm)}</b> &nbsp;|&nbsp;
            Step: <b style="color: {TEXT_PRIMARY};">{int(effective_step)} pts</b> &nbsp;|&nbsp;
            Units: <b style="color: {CYAN_ACCENT};">{units_mode.upper()}</b>
        </div>
    </div>
    """)

    # 6. Evaluate session timelines
    session_res = oi_timeline.evaluate_session_timelines(
        all_snapshots=snaps,
        sess_date=selected_date,
        strikes=selected_strikes,
        strike_step=effective_step,
        current_spot=ref_spot,
        atm_anchor="current" if "Current" in atm_anchor_choice else "open",
        rules=rules,
    )
    cov = session_res.coverage

    # Auto-log to CSV
    if auto_csv:
        try:
            oi_timeline.export_timeline_to_csv(session_res)
        except Exception as exc:
            log.warning("Could not auto-log timeline to CSV: %s", exc)

    # 7. Status Banners
    if not cov.passes_gate:
        render_html(f"""
        <div style="background: rgba(239, 68, 68, 0.12); border: 1px solid {CRIMSON_CE}; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
            <span style="font-weight: 700; color: {CRIMSON_CE}; font-family: monospace;">⚠️ GATE AUDIT:</span>
            <span style="color: {TEXT_PRIMARY}; font-size: 12px; margin-left: 6px;">
                Session {selected_date} ({cov.status_note}).
                Coverage: {cov.occupied_bins}/125 bins ({cov.coverage_pct:.1f}%). Max gap: {cov.max_contiguous_missing_bins} bins ({cov.max_gap_minutes:.1f}m).
            </span>
        </div>
        """)
    elif cov.divergence_flag:
        render_html(f"""
        <div style="background: rgba(245, 158, 11, 0.12); border: 1px solid {AMBER_WARNING}; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
            <span style="font-weight: 700; color: {AMBER_WARNING}; font-family: monospace;">⚠️ BASELINE DIVERGENCE:</span>
            <span style="color: {TEXT_PRIMARY}; font-size: 12px; margin-left: 6px;">
                Primary ce_chg_oi diverged from secondary subtraction by max {cov.max_divergence_contracts:,.0f} contracts.
                Primary series trusted.
            </span>
        </div>
        """)

    # Clock Drift Banner
    try:
        hb_path = Path("data/scheduler_heartbeat.json")
        if hb_path.exists():
            hb_data = json.loads(hb_path.read_text())
            drift_val = hb_data.get("drift_minutes")
            if drift_val is not None and drift_val > 15.0:
                render_html(f"""
                <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid {AMBER_WARNING}; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                    <span style="font-weight: 700; color: {AMBER_WARNING}; font-family: monospace;">⚠️ SYSTEM CLOCK DRIFT:</span>
                    <span style="color: {TEXT_PRIMARY}; font-size: 12px; margin-left: 6px;">
                        Local system clock diverges from NSE feed by {drift_val:.1f} minutes (>15m).
                    </span>
                </div>
                """)
    except Exception as _e:
        log.debug("Heartbeat drift check skipped: %s", _e)

    # 8. Metric Header Bar
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("NIFTY Spot", f"{last_spot:,.1f}", f"{last_spot - first_spot:+,.1f} pts")
    with m2:
        st.metric("ATM Strike", f"{session_res.atm_strike:,.0f}", f"{int(effective_step)} pt step")
    with m3:
        st.metric("Coverage (125 Bins)", f"{cov.occupied_bins} / 125", f"{cov.coverage_pct:.1f}%")
    with m4:
        st.metric("Max In-Session Gap", f"{cov.max_gap_minutes:.1f} min", f"{cov.max_contiguous_missing_bins} bins")
    with m5:
        st.metric("Gate Audit", "PASSED" if cov.passes_gate else "IN-PROGRESS / AUDIT", cov.status_note)

    st.markdown("---")

    # 8.5 Net Writer Gap Evaluation & Rendering (Section 7)
    try:
        wg_rules = oi_timeline.load_writer_gap_rules()
        wg_res = oi_timeline.evaluate_writer_gap_session(snaps, selected_date, wg_rules)
        if wg_res and wg_res.poll_results:
            latest_poll = wg_res.poll_results[-1]
            side_color = EMERALD_PE if latest_poll.net_side == "PE_ABOVE" else (CRIMSON_CE if latest_poll.net_side == "CE_ABOVE" else SLATE_NEUTRAL)
            render_html(f"""
            <div style="background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 8px; padding: 12px 16px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div>
                        <span style="font-size: 16px; font-weight: 800; color: {TEXT_PRIMARY}; font-family: monospace;">
                            ⚡ NET WRITER GAP & WRITER LINES
                        </span>
                        <span style="margin-left: 8px; font-size: 11px; background: rgba(168, 85, 247, 0.15); color: {PURPLE_ACCENT}; border: 1px solid {PURPLE_ACCENT}; border-radius: 4px; padding: 2px 6px; font-family: monospace;">
                            BASKET ATM±200
                        </span>
                    </div>
                    <div style="font-size: 12px; font-family: monospace; color: {TEXT_MUTED};">
                        Side: <b style="color: {side_color};">{latest_poll.net_side}</b> &nbsp;|&nbsp;
                        Net Gap: <b style="color: {TEXT_PRIMARY};">{latest_poll.net_gap:+.1f}L</b> &nbsp;|&nbsp;
                        Trend: <b style="color: {CYAN_ACCENT};">{latest_poll.gap_trend_15m}</b> &nbsp;|&nbsp;
                        Walls: <b style="color: {TEXT_PRIMARY};">{latest_poll.wall_migration.summary_str}</b>
                    </div>
                </div>
            </div>
            """)

            fig_wg = _build_net_writer_gap_chart(wg_res)
            st.plotly_chart(fig_wg, use_container_width=True, key="net_writer_gap_chart")

            # 5-min WRITER LINES table expander
            if wg_res.table_results:
                latest_tbl = wg_res.table_results[-1]
                with st.expander(f"📊 Latest WRITER LINES 5-Min Table ({latest_tbl.clock_time}) — READ: {latest_tbl.read.read_text}", expanded=False):
                    st.text(latest_tbl.table_text)
            st.markdown("---")
    except Exception as exc:
        log.warning("Could not evaluate Net Writer Gap chart: %s", exc)

    # 9. Strike Multiples Rendering (OptionTick Style Cards)
    watched_strikes = session_res.watched_strikes
    if not watched_strikes:
        st.info("No strike timelines evaluated.")
        return

    if view_layout == "Grid (Small Multiples)":
        for i in range(0, len(watched_strikes), 2):
            cols = st.columns(2)
            for j, strike in enumerate(watched_strikes[i:i+2]):
                tl = session_res.strike_timelines[strike]
                st_stat = tl.latest_stats
                with cols[j]:
                    _render_strike_card(strike, tl, st_stat, rules, display_units=units_mode, display_lot=display_lot)
    else:
        tabs = st.tabs([f"Strike {int(s)}" for s in watched_strikes])
        for idx, strike in enumerate(watched_strikes):
            tl = session_res.strike_timelines[strike]
            st_stat = tl.latest_stats
            with tabs[idx]:
                _render_strike_card(strike, tl, st_stat, rules, display_units=units_mode, display_lot=display_lot)

    # 10. Tabular Raw Resampled Data
    with st.expander("📋 View Resampled 3-Minute Session Timeline Data", expanded=False):
        selected_table_strike = st.selectbox(
            "Select Strike for Detailed Inspection",
            options=watched_strikes,
            format_func=lambda s: f"Strike {int(s)}",
            key="oi_timeline_tbl_strike",
        )
        if selected_table_strike in session_res.strike_timelines:
            tl_tbl = session_res.strike_timelines[selected_table_strike]
            rows = []
            for p in tl_tbl.series:
                rows.append({
                    "Time": p.clock_time,
                    "Spot": p.spot,
                    "CE ΔOI": p.ce_chg_oi,
                    "PE ΔOI": p.pe_chg_oi,
                    "Spread (PE-CE)": p.spread,
                    "Dominance": p.dominance,
                    "Flow": p.flow,
                    "Cum PE Quad": p.pe_quadrant_cum,
                    "Cum CE Quad": p.ce_quadrant_cum,
                    "Cum Agree": p.agreement_cum,
                    "15m PE Quad": p.pe_quadrant_local,
                    "15m CE Quad": p.ce_quadrant_local,
                    "15m Agree": p.agreement_local,
                    "Flow vs Cum": p.cum_vs_local_agreement,
                    "PCR": p.pcr,
                })
            df_display = pd.DataFrame(rows)
            st.dataframe(df_display, use_container_width=True, hide_index=True)


def _render_strike_card(
    strike: float,
    tl: oi_timeline.StrikeTimelineResult,
    st_stat: Optional[oi_timeline.StrikeSummaryStats],
    rules: dict,
    display_units: str = "lakhs",
    display_lot: int = 65,
) -> None:
    """Renders a single strike card matching OptionTick UI screenshot layout."""
    dom_badge = _render_dominance_badge(st_stat.dominance) if st_stat else ""
    flow_badge = _render_flow_badge(st_stat.flow) if st_stat else ""
    comb_interp = getattr(st_stat, "combined_interpretation", "") if st_stat else ""
    agree_badge = _render_agreement_badge(st_stat.agreement_cum, comb_interp) if st_stat else ""

    use_lakhs = (display_units.lower() == "lakhs")
    lot = display_lot if display_lot > 0 else 65
    mult = (lot / 100000.0) if use_lakhs else 1.0

    render_html(f"""
    <div style="background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <div style="font-size: 10px; font-weight: 700; color: {TEXT_MUTED}; text-transform: uppercase; letter-spacing: 0.8px;">
                    COMBINED OI CHANGE
                </div>
                <div style="font-size: 18px; font-weight: 800; color: {TEXT_PRIMARY}; font-family: monospace; margin-top: 2px;">
                    Strike <span style="color: {CYAN_ACCENT};">{int(strike)}</span>
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 6px;">
                {dom_badge}
                {flow_badge}
                {agree_badge}
            </div>
        </div>
    </div>
    """)

    # Render Plotly Chart
    fig = _build_strike_chart(tl, rules, display_units=display_units, display_lot=lot)
    st.plotly_chart(fig, use_container_width=True, key=f"oi_chart_{int(strike)}")

    if st_stat:
        # OptionTick 6-metric summary card (3x2 grid matching screenshot)
        if use_lakhs:
            ce_chg_str = f"{(st_stat.dce * mult):+,.2f}L"
            pe_chg_str = f"{(st_stat.dpe * mult):+,.2f}L"
            call_oi_str = f"{(st_stat.ce_oi * mult):,.2f}L"
            put_oi_str = f"{(st_stat.pe_oi * mult):,.2f}L"
        else:
            ce_chg_str = f"{st_stat.dce:+,.0f}"
            pe_chg_str = f"{st_stat.dpe:+,.0f}"
            call_oi_str = f"{st_stat.ce_oi:,.0f}"
            put_oi_str = f"{st_stat.pe_oi:,.0f}"

        render_html(f"""
        <div style="background: {BG_CARD_INNER}; border: 1px solid {BORDER_DARK}; border-radius: 6px; padding: 10px 14px; margin-top: 6px; margin-bottom: 8px;">
            <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; font-family: monospace;">
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">CE ΔOI</div>
                    <div style="font-size: 14px; font-weight: 700; color: {CRIMSON_CE};">{ce_chg_str}</div>
                </div>
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">PE ΔOI</div>
                    <div style="font-size: 14px; font-weight: 700; color: {EMERALD_PE};">{pe_chg_str}</div>
                </div>
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">SPOT</div>
                    <div style="font-size: 14px; font-weight: 700; color: {CYAN_ACCENT};">{st_stat.current_spot:,.2f}</div>
                </div>
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">PCR</div>
                    <div style="font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY};">{st_stat.pcr:.2f}</div>
                </div>
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">CALL OI</div>
                    <div style="font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY};">{call_oi_str}</div>
                </div>
                <div>
                    <div style="font-size: 10px; color: {TEXT_MUTED}; font-weight: 600;">PUT OI</div>
                    <div style="font-size: 14px; font-weight: 700; color: {TEXT_PRIMARY};">{put_oi_str}</div>
                </div>
            </div>
        </div>
        """)

        # Veto Discipline Box
        veto_bg = "rgba(16, 185, 129, 0.1)" if "Short-side entries discouraged" in st_stat.veto_note else (
            "rgba(239, 68, 68, 0.1)" if "Long-side entries discouraged" in st_stat.veto_note else "rgba(148, 163, 184, 0.1)"
        )
        veto_border = EMERALD_PE if "Short-side" in st_stat.veto_note else (
            CRIMSON_CE if "Long-side" in st_stat.veto_note else BORDER_DARK
        )

        render_html(f"""
        <div style="background: {veto_bg}; border-left: 3px solid {veto_border}; padding: 6px 12px; border-radius: 4px; margin-bottom: 6px;">
            <div style="font-size: 9px; font-weight: 700; color: {TEXT_MUTED}; text-transform: uppercase;">
                VETO DISCIPLINE
            </div>
            <div style="font-size: 11px; color: {TEXT_PRIMARY}; font-weight: 600; margin-top: 2px;">
                {st_stat.veto_note}
            </div>
        </div>
        """)

        # Quadrant details row
        render_html(f"""
        <div style="background: {BG_CARD}; border: 1px solid {BORDER_DARK}; border-radius: 4px; padding: 6px 10px; font-size: 11px; font-family: monospace; color: {TEXT_MUTED}; margin-bottom: 14px;">
            <span style="color: {TEXT_PRIMARY}; font-weight: 600;">Day Net:</span> CE: <b style="color: {TEXT_PRIMARY};">{st_stat.ce_quadrant_cum}</b> | PE: <b style="color: {TEXT_PRIMARY};">{st_stat.pe_quadrant_cum}</b>
            &nbsp;|&nbsp;
            <span style="color: {TEXT_PRIMARY}; font-weight: 600;">15m Flow:</span> CE: <b style="color: {TEXT_PRIMARY};">{st_stat.ce_quadrant_local}</b> | PE: <b style="color: {TEXT_PRIMARY};">{st_stat.pe_quadrant_local}</b>
            &nbsp;|&nbsp;
            Flow Alignment: <b style="color: {'#10b981' if st_stat.cum_vs_local_agreement == 'AGREE' else '#f59e0b'};">{st_stat.cum_vs_local_agreement}</b>
        </div>
        """)


def render(ctx=None, client=None) -> None:
    """Standard view contract callable from orchestrators."""
    render_oi_timeline_tab(client=client)

