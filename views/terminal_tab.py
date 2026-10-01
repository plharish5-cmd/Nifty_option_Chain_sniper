"""
views/terminal_tab.py — Modern High-Performance Institutional Trading Terminal View.

Strict Institutional UI/UX Specifications:
  1. Dark Mode Palette: slate-900 (#0f172a) background, slate-800 (#1e293b) cards,
     slate-700 (#334155) borders, slate-400 (#94a3b8) subdued text,
     emerald-400 (#34d399) bullish, rose-400 (#fb7185) bearish, amber-400 (#fbbf24) warning.
  2. Tabular Numerals: Monospace (JetBrains Mono / Fira Code) for all pricing, OI data, and metrics.
  3. High Contrast & Zero Unnecessary Padding to maximize institutional screen real estate.
  4. Decision Cockpit (Top Header):
     - Horizontal Four Pillars scorecard (VIX, FII/DII, Market Breadth, Global Cues).
     - Composite Bias Score badge (e.g. -0.2 / 4.0, +3.2 / 4.0).
     - Dynamic Algo Playbook Directive text block.
  5. Real-Time Option Flow (Left Panel):
     - Divergent OI Bar Chart (ATM centered, Left red Call OI Chg, Right green Put OI Chg).
     - Strike Evidence Table (Tabular numerals, soft color-coded state backgrounds).
  6. SMC & Price Action Canvas (Center/Right Panel):
     - Active Setups Feed (Setup, Logic, Action, Conviction, R:R).
     - Zone Matrix (Liquidity pools, Option Chain S/R, Confirmed/Contested/Weakening status).
     - Visual Alignment of SMC structures with heavy Option Walls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from engine.terminal_engine import UnifiedTerminalEngine, TerminalSnapshot
from engine.smc_engine import DealingRange
from engine.options_flow_engine import (
    StrikeOIClassification,
    ZoneMatrixRow,
    ActiveSetupCard,
    FourPillarsReport,
    GammaProfile,
)
from ui_utils import render_html, _render_html


# ═══════════════════════════════════════════════════════════════════════════
# Institutional CSS Injection (Zero-Padding, Monospace Tabular Numerals)
# ═══════════════════════════════════════════════════════════════════════════

TERMINAL_STYLES = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

  /* Root variables & reset */
  :root {
    --bg-main: #0f172a;
    --card-bg: #1e293b;
    --card-border: #334155;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --emerald-signal: #34d399;
    --emerald-bg: rgba(52, 211, 153, 0.12);
    --rose-signal: #fb7185;
    --rose-bg: rgba(251, 113, 133, 0.12);
    --amber-signal: #fbbf24;
    --amber-bg: rgba(251, 191, 36, 0.12);
    --blue-signal: #38bdf8;
  }

  /* Maximize viewport real estate */
  .block-container {
    padding-top: 0.5rem !important;
    padding-bottom: 1rem !important;
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    max-width: 100% !important;
  }

  /* Universal typography */
  .prop-font-ui { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }
  .mono-nums {
    font-family: 'JetBrains Mono', 'Fira Code', monospace !important;
    font-variant-numeric: tabular-nums !important;
  }

  /* Decision Cockpit Container */
  .cockpit-header {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 8px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
  }

  /* Four Pillars Ribbon */
  .pillars-ribbon {
    display: grid;
    grid-template-columns: repeat(4, 1fr) 220px;
    gap: 10px;
    align-items: center;
  }

  .pillar-card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .pillar-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
    font-weight: 600;
  }

  .pillar-metric {
    font-size: 15px;
    font-weight: 700;
    color: #f1f5f9;
  }

  .pillar-regime {
    font-size: 10px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 4px;
    display: inline-block;
    width: fit-content;
  }

  /* Composite Badge */
  .composite-badge-card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 8px 12px;
    text-align: center;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .composite-val {
    font-size: 22px;
    font-weight: 800;
    line-height: 1.1;
  }

  /* Algo Directive Banner */
  .directive-box {
    background: rgba(15, 23, 42, 0.85);
    border-left: 4px solid #38bdf8;
    border-top: 1px solid #1e293b;
    border-right: 1px solid #1e293b;
    border-bottom: 1px solid #1e293b;
    border-radius: 0 6px 6px 0;
    padding: 6px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .directive-tag {
    font-size: 11px;
    font-weight: 700;
    background: #0284c7;
    color: #ffffff;
    padding: 2px 8px;
    border-radius: 3px;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }

  .directive-text {
    font-size: 12.5px;
    color: #e2e8f0;
    font-weight: 500;
  }

  /* Panel Cards */
  .terminal-card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }

  .terminal-card-title {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94a3b8;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /* Strike Evidence Table */
  .compact-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }

  .compact-table th {
    background: #0f172a;
    color: #94a3b8;
    padding: 4px 6px;
    text-align: right;
    font-weight: 600;
    border-bottom: 1px solid #334155;
    font-size: 10px;
    text-transform: uppercase;
  }

  .compact-table td {
    padding: 4px 6px;
    border-bottom: 1px solid #1e293b;
    text-align: right;
  }

  .compact-table tr:hover {
    background: rgba(51, 65, 85, 0.4);
  }

  .compact-table .atm-row {
    background: rgba(56, 189, 248, 0.12) !important;
    font-weight: 700;
  }

  /* State badges */
  .state-badge {
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.02em;
    display: inline-block;
    text-align: center;
  }

  .state-long-buildup { background: rgba(52, 211, 153, 0.16); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.35); }
  .state-short-buildup { background: rgba(251, 113, 133, 0.16); color: #fb7185; border: 1px solid rgba(251, 113, 133, 0.35); }
  .state-short-covering { background: rgba(56, 189, 248, 0.16); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); }
  .state-long-unwinding { background: rgba(251, 191, 36, 0.16); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.35); }
  .state-flat { color: #64748b; }

  /* Active Setups Cards */
  .setup-card {
    background: #111827;
    border-left: 3px solid #34d399;
    border-top: 1px solid #1f2937;
    border-right: 1px solid #1f2937;
    border-bottom: 1px solid #1f2937;
    border-radius: 0 6px 6px 0;
    padding: 8px 12px;
    margin-bottom: 8px;
  }

  .setup-card.bearish {
    border-left-color: #fb7185;
  }

  .setup-header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
  }

  .setup-name {
    font-size: 13px;
    font-weight: 700;
    color: #f8fafc;
  }

  .setup-conviction {
    font-size: 10px;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 3px;
    background: #334155;
    color: #f1f5f9;
  }

  .setup-section-label {
    font-size: 11px;
    font-weight: 700;
    color: #94a3b8;
  }

  .setup-section-content {
    font-size: 11.5px;
    color: #e2e8f0;
    margin-bottom: 3px;
  }

  /* Zone Matrix Rows */
  .matrix-item {
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 7px 10px;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .matrix-level {
    font-size: 14px;
    font-weight: 700;
    color: #f8fafc;
  }

  .status-tag-confirmed {
    background: rgba(52, 211, 153, 0.18);
    color: #34d399;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid rgba(52, 211, 153, 0.4);
  }

  .status-tag-contested {
    background: rgba(251, 191, 36, 0.18);
    color: #fbbf24;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid rgba(251, 191, 36, 0.4);
  }

  .status-tag-weakening {
    background: rgba(251, 113, 133, 0.18);
    color: #fb7185;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid rgba(251, 113, 133, 0.4);
  }
</style>
"""


def render_decision_cockpit(report: FourPillarsReport, spot: float, dealing_range: Optional[DealingRange]):
    """Renders The Decision Cockpit (Top Header)."""
    score = report.composite_score
    score_color = "#34d399" if score >= 1.0 else ("#fb7185" if score <= -1.0 else "#94a3b8")
    score_bg = "rgba(52, 211, 153, 0.12)" if score >= 1.0 else ("rgba(251, 113, 133, 0.12)" if score <= -1.0 else "rgba(148, 163, 184, 0.12)")

    # 4 Pillar cards HTML
    pillar_cards_html = []
    for p in report.pillars:
        regime_style = (
            "background: rgba(52, 211, 153, 0.16); color: #34d399;" if p.status_color == "EMERALD" else
            ("background: rgba(251, 113, 133, 0.16); color: #fb7185;" if p.status_color == "ROSE" else
             "background: rgba(251, 191, 36, 0.16); color: #fbbf24;")
        )
        pillar_cards_html.append(
            f'<div class="pillar-card">'
            f'<div class="pillar-title">{p.title}</div>'
            f'<div class="pillar-metric mono-nums">{p.raw_metric}</div>'
            f'<div class="pillar-regime" style="{regime_style}">{p.regime}</div>'
            f'</div>'
        )

    # L3 RVOL Gate Card (Chop Filter — Ported from Validated Build)
    rvol_directive_prefix = ""
    try:
        import rvol_gate
        from decision_cockpit_view import compute_rvol_sparkline_svg
        now_dt = datetime.now()
        rvol_dec = rvol_gate.get_gate_decision(now_dt.date(), now_dt.time())
        r_val = rvol_dec.get("rvol_at_gate")
        r_reg = rvol_dec.get("regime", "NA")

        if r_reg == "CHOP_ZONE":
            r_col = "#fb7185"
            r_bg = "rgba(251, 113, 133, 0.16)"
            r_title = "CHOP ZONE"
            rvol_directive_prefix = "⚠️ CHOP ZONE — 77% breakouts fail. Wait for RVOL > 0.85 before any entry. "
        elif r_reg == "TREND_DAY":
            r_col = "#fbbf24"
            r_bg = "rgba(251, 191, 36, 0.16)"
            r_title = "TREND DAY"
            rvol_directive_prefix = "🚀 TREND DAY — hold trades to full target, don't exit early at first wall. "
        elif r_reg == "NORMAL":
            r_col = "#34d399"
            r_bg = "rgba(52, 211, 153, 0.16)"
            r_title = "NORMAL"
        else:
            r_col = "#94a3b8"
            r_bg = "rgba(148, 163, 184, 0.16)"
            r_title = r_reg

        r_val_txt = f"{r_val:.2f}×" if r_val is not None else "N/A"
        r_spark = compute_rvol_sparkline_svg(now_dt.date(), now_dt.time(), color=r_col, width=95, height=26)

        pillar_cards_html.append(
            f'<div class="pillar-card" style="min-width: 140px;">'
            f'<div class="pillar-title" style="display:flex; justify-content:space-between;">'
            f'<span>RVOL GATE</span>'
            f'<span style="color:{r_col}; font-weight:800; font-size:9.5px;">{r_title}</span>'
            f'</div>'
            f'<div style="display:flex; justify-content:space-between; align-items:flex-end;">'
            f'<div class="pillar-metric mono-nums" style="color:{r_col};">{r_val_txt}</div>'
            f'<div>{r_spark}</div>'
            f'</div>'
            f'</div>'
        )
    except Exception as _r_err:
        pass

    cards_joined = "".join(pillar_cards_html)

    range_txt = ""
    if dealing_range:
        range_txt = (
            f'<div style="font-size: 11px; color: #94a3b8; margin-top: 2px;" class="mono-nums">'
            f'Range EQ: <span style="color:#f8fafc; font-weight:600;">{dealing_range.equilibrium:,.0f}</span> | '
            f'Regime: <span style="color:{"#fb7185" if dealing_range.current_regime=="PREMIUM" else "#34d399"}; font-weight:700;">{dealing_range.current_regime}</span> ({dealing_range.discount_depth_pct:.0f}%)'
            f'</div>'
        )

    directive_text = rvol_directive_prefix + report.algo_directive

    cockpit_html = (
        f'<div class="cockpit-header prop-font-ui">'
        f'<div class="pillars-ribbon">'
        f'{cards_joined}'
        f'<div class="composite-badge-card" style="background:{score_bg}; border-color:{score_color};">'
        f'<div style="font-size: 10.5px; text-transform:uppercase; color:#94a3b8; font-weight:700; letter-spacing:0.06em;">COMPOSITE BIAS</div>'
        f'<div class="composite-val mono-nums" style="color:{score_color};">{score:+0.1f} / 4.0</div>'
        f'<div style="font-size: 10.5px; font-weight:700; color:{score_color};">{report.bias_label}</div>'
        f'{range_txt}'
        f'</div>'
        f'</div>'
        f'<div class="directive-box">'
        f'<div class="directive-tag">ALGO DIRECTIVE</div>'
        f'<div class="directive-text">{directive_text}</div>'
        f'</div>'
        f'</div>'
    )
    _render_html(cockpit_html)


def create_divergent_oi_chart(chart_data: list[dict], atm_strike: int) -> go.Figure:
    """
    Renders Divergent OI Bar Chart centered on the ATM strike.
    Left-pointing Red/Rose bars: Call OI Change
    Right-pointing Green/Emerald bars: Put OI Change
    """
    fig = go.Figure()

    if not chart_data:
        return fig

    strikes = [d["strike"] for d in chart_data]
    # For horizontal bars: x = change in OI, y = strike
    call_chg = [d["call_oi_chg_left"] for d in chart_data]  # negative values point left
    put_chg = [d["put_oi_chg_right"] for d in chart_data]    # positive values point right
    call_raw = [d["call_oi_chg_raw"] for d in chart_data]
    put_raw = [d["put_oi_chg_raw"] for d in chart_data]

    # Left: Call ΔOI (Rose)
    fig.add_trace(go.Bar(
        y=strikes,
        x=call_chg,
        orientation='h',
        name='Call ΔOI (Left)',
        marker=dict(
            color='#fb7185',
            line=dict(color='#e11d48', width=1)
        ),
        customdata=call_raw,
        hovertemplate="<b>%{y} CE</b><br>ΔOI: %{customdata:+,d}<extra></extra>"
    ))

    # Right: Put ΔOI (Emerald)
    fig.add_trace(go.Bar(
        y=strikes,
        x=put_chg,
        orientation='h',
        name='Put ΔOI (Right)',
        marker=dict(
            color='#34d399',
            line=dict(color='#059669', width=1)
        ),
        customdata=put_raw,
        hovertemplate="<b>%{y} PE</b><br>ΔOI: %{customdata:+,d}<extra></extra>"
    ))

    # Add ATM strike horizontal reference line
    fig.add_hline(
        y=atm_strike,
        line=dict(color="#38bdf8", width=1.5, dash="dot"),
        annotation_text=f"ATM {atm_strike}",
        annotation_position="top right",
        annotation_font=dict(color="#38bdf8", size=10)
    )

    # Vertical zero line
    fig.add_vline(x=0, line=dict(color="#475569", width=1.5))

    fig.update_layout(
        title=dict(
            text="<b>Divergent Open Interest Flow (ATM Centered)</b>",
            font=dict(size=12, color="#94a3b8"),
            x=0.01, y=0.98
        ),
        barmode='relative',
        height=380,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#1e293b",
        plot_bgcolor="#0f172a",
        xaxis=dict(
            zeroline=True,
            zerolinecolor="#475569",
            gridcolor="#1e293b",
            tickfont=dict(family="JetBrains Mono", size=9, color="#94a3b8"),
            title=dict(text="◀ CE Writing / Unwinding (Left) | PE Writing / Unwinding (Right) ▶", font=dict(size=9, color="#64748b"))
        ),
        yaxis=dict(
            gridcolor="#1e293b",
            tickmode="linear",
            dtick=50,
            tickfont=dict(family="JetBrains Mono", size=9, color="#cbd5e1")
        ),
        showlegend=False
    )
    return fig


def render_strike_evidence_table(strikes: list[StrikeOIClassification], atm_strike: int):
    """
    Renders Strike Evidence Table with tabular numerals & soft-tint background state pills.
    """
    # Filter 7 strikes around ATM
    subset = [s for s in strikes if abs(s.strike - atm_strike) <= 350]

    rows_html = []
    for s in subset:
        is_atm = s.is_atm
        row_cls = "atm-row" if is_atm else ""

        # CE State Pill
        ce_cls = "state-flat"
        if s.ce_state == "LONG BUILD-UP": ce_cls = "state-long-buildup"
        elif s.ce_state == "SHORT BUILD-UP": ce_cls = "state-short-buildup"
        elif s.ce_state == "SHORT COVERING": ce_cls = "state-short-covering"
        elif s.ce_state == "LONG UNWINDING": ce_cls = "state-long-unwinding"

        # PE State Pill
        pe_cls = "state-flat"
        if s.pe_state == "LONG BUILD-UP": pe_cls = "state-long-buildup"
        elif s.pe_state == "SHORT BUILD-UP": pe_cls = "state-short-buildup"
        elif s.pe_state == "SHORT COVERING": pe_cls = "state-short-covering"
        elif s.pe_state == "LONG UNWINDING": pe_cls = "state-long-unwinding"

        ce_pct_col = "#34d399" if s.ce_oi_pct > 0 else ("#fb7185" if s.ce_oi_pct < 0 else "#94a3b8")
        pe_pct_col = "#34d399" if s.pe_oi_pct > 0 else ("#fb7185" if s.pe_oi_pct < 0 else "#94a3b8")

        strike_label = f"★ {s.strike}" if is_atm else f"{s.strike}"

        rows_html.append(
            f'<tr class="{row_cls}">'
            f'<td class="mono-nums" style="font-weight:600; color:#f1f5f9;">{s.ce_ltp:,.1f}</td>'
            f'<td class="mono-nums" style="color:{ce_pct_col};">{s.ce_oi_pct:+0.1f}%</td>'
            f'<td><span class="state-badge {ce_cls}">{s.ce_state}</span></td>'
            f'<td class="mono-nums" style="font-weight:700; color:{"#38bdf8" if is_atm else "#e2e8f0"}; text-align:center;">{strike_label}</td>'
            f'<td><span class="state-badge {pe_cls}">{s.pe_state}</span></td>'
            f'<td class="mono-nums" style="color:{pe_pct_col};">{s.pe_oi_pct:+0.1f}%</td>'
            f'<td class="mono-nums" style="font-weight:600; color:#f1f5f9;">{s.pe_ltp:,.1f}</td>'
            f'</tr>'
        )

    table_html = (
        f'<div style="overflow-x: auto; max-height: 380px;">'
        f'<table class="compact-table prop-font-ui">'
        f'<thead>'
        f'<tr>'
        f'<th style="text-align:right;">CE LTP</th>'
        f'<th style="text-align:right;">CE ΔOI%</th>'
        f'<th style="text-align:center;">CE State</th>'
        f'<th style="text-align:center; color:#38bdf8;">Strike</th>'
        f'<th style="text-align:center;">PE State</th>'
        f'<th style="text-align:right;">PE ΔOI%</th>'
        f'<th style="text-align:right;">PE LTP</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        f'</table>'
        f'</div>'
    )
    _render_html(table_html)


def create_smc_candlestick_chart(
    df: pd.DataFrame,
    smc_res: dict,
    gamma: GammaProfile,
    spot: float
) -> go.Figure:
    """
    Renders high-definition Price Action Canvas with:
      - 15m Candlesticks
      - Strong vs. Weak MSB break lines
      - Bullish/Bearish Fair Value Gaps (FVG)
      - Order Blocks & Breaker Blocks (Flipped state)
      - 50% Dealing Range Equilibrium & OTE Golden Zone
      - Option Walls (Call Wall & Put Wall) for direct visual confluence!
    """
    fig = go.Figure()
    if df is None or (hasattr(df, "empty") and df.empty) or len(df) < 5:
        from engine.test_terminal_engine import generate_synthetic_candles
        df = generate_synthetic_candles(n=60, base_price=spot or 24150.0)

    # Subset last 45 bars for high-definition clarity
    sub_df = df.tail(45).reset_index(drop=True)
    if "timestamp" not in sub_df.columns:
        sub_df["timestamp"] = pd.to_datetime(sub_df.index)
    last_ts = sub_df["timestamp"].iloc[-1]
    first_ts = sub_df["timestamp"].iloc[0]

    # 1. Candlestick series
    fig.add_trace(go.Candlestick(
        x=sub_df["timestamp"],
        open=sub_df["open"],
        high=sub_df["high"],
        low=sub_df["low"],
        close=sub_df["close"],
        name="NIFTY",
        increasing_line_color="#34d399",
        decreasing_line_color="#fb7185",
        increasing_fillcolor="#34d399",
        decreasing_fillcolor="#fb7185",
    ))

    # 2. Fair Value Gaps (FVGs)
    for fvg in smc_res.get("active_fvgs", [])[-5:]:
        is_bull = fvg.direction == "BULLISH"
        fill_col = "rgba(52, 211, 153, 0.15)" if is_bull else "rgba(251, 113, 133, 0.15)"
        line_col = "#34d399" if is_bull else "#fb7185"
        fig.add_shape(
            type="rect",
            x0=fvg.timestamp, x1=last_ts,
            y0=fvg.bottom, y1=fvg.top,
            fillcolor=fill_col,
            line=dict(color=line_col, width=1, dash="dot")
        )

    # 3. Order Blocks & Breaker Blocks
    for ob in smc_res.get("order_blocks", [])[-4:]:
        is_bull = ob.direction == "BULLISH"
        if ob.state == "BREAKER":
            # Breaker block has flipped!
            fill_col = "rgba(251, 191, 36, 0.20)"
            line_col = "#fbbf24"
            tag = "Breaker Block"
        else:
            fill_col = "rgba(52, 211, 153, 0.22)" if is_bull else "rgba(251, 113, 133, 0.22)"
            line_col = "#34d399" if is_bull else "#fb7185"
            tag = "Demand OB" if is_bull else "Supply OB"

        fig.add_shape(
            type="rect",
            x0=ob.timestamp, x1=last_ts,
            y0=ob.bottom, y1=ob.top,
            fillcolor=fill_col,
            line=dict(color=line_col, width=1.2)
        )
        fig.add_annotation(
            x=last_ts, y=ob.mean_threshold,
            text=f"<b>{tag}</b> ({ob.mean_threshold:,.0f})",
            showarrow=False,
            font=dict(size=9, color=line_col),
            xanchor="left", yanchor="middle"
        )

    # 4. Dealing Range 50% Equilibrium & OTE Golden Zone
    dr = smc_res.get("dealing_range")
    if dr:
        fig.add_hline(
            y=dr.equilibrium,
            line=dict(color="#38bdf8", width=1.5, dash="dash"),
            annotation_text=f"50% EQ ({dr.equilibrium:,.0f})",
            annotation_position="bottom right",
            annotation_font=dict(color="#38bdf8", size=9)
        )
        # OTE Golden zone
        fig.add_shape(
            type="rect",
            x0=first_ts, x1=last_ts,
            y0=min(dr.ote_705, dr.ote_790), y1=max(dr.ote_705, dr.ote_790),
            fillcolor="rgba(251, 191, 36, 0.08)",
            line=dict(color="rgba(251, 191, 36, 0.4)", width=1, dash="dot")
        )

    # 5. Options Flow Alignment: Heavy Call Wall & Put Wall lines
    fig.add_hline(
        y=gamma.call_wall_strike,
        line=dict(color="#e11d48", width=1.8, dash="longdash"),
        annotation_text=f"CE Wall {gamma.call_wall_strike}",
        annotation_position="top left",
        annotation_font=dict(color="#fb7185", size=10)
    )
    fig.add_hline(
        y=gamma.put_wall_strike,
        line=dict(color="#059669", width=1.8, dash="longdash"),
        annotation_text=f"PE Wall {gamma.put_wall_strike}",
        annotation_position="bottom left",
        annotation_font=dict(color="#34d399", size=10)
    )

    # Layout
    fig.update_layout(
        title=dict(
            text="<b>SMC Structure Canvas & Options Wall Confluence</b>",
            font=dict(size=12, color="#94a3b8"),
            x=0.01, y=0.98
        ),
        xaxis_rangeslider_visible=False,
        height=380,
        margin=dict(l=10, r=90, t=30, b=10),
        paper_bgcolor="#1e293b",
        plot_bgcolor="#0f172a",
        xaxis=dict(
            gridcolor="#1e293b",
            tickfont=dict(family="JetBrains Mono", size=9, color="#94a3b8")
        ),
        yaxis=dict(
            gridcolor="#1e293b",
            tickfont=dict(family="JetBrains Mono", size=9, color="#cbd5e1"),
            side="right"
        ),
        showlegend=False
    )
    return fig


def render_active_setups_feed(setups: list[ActiveSetupCard]):
    """
    Renders the live Active Setups Feed in the exact institutional format requested:
      - Setup: e.g. "Dragon Fruit Combo Detected"
      - Logic: e.g. "15m Imbalance overlaps with 23900 Call Unwinding. Premium Zone."
      - Action: e.g. "Look for SFP (Swing Failure) on 1m chart for short entry."
    """
    if not setups:
        _render_html("<div style='color:#94a3b8; font-size:12px;'>No active institutional setups flagged.</div>")
        return

    cards_html = []
    is_chop = False
    try:
        import rvol_gate
        now_dt = datetime.now()
        r_dec = rvol_gate.get_gate_decision(now_dt.date(), now_dt.time())
        is_chop = (r_dec.get("status") == "SUPPRESSED")
    except Exception:
        pass

    for s in setups:
        is_bearish = (s.direction == "BEARISH")
        card_cls = "setup-card bearish" if is_bearish else "setup-card"
        dir_color = "#fb7185" if is_bearish else ("#34d399" if s.direction == "BULLISH" else "#38bdf8")

        # Range setups permitted in chop, breakouts suppressed
        is_range = any(w in s.setup_name.lower() or w in s.logic.lower() for w in ["sfp", "reversal", "order block", "fade", "absorp", "wall"])
        suppress_badge = ""
        if is_chop and not is_range:
            suppress_badge = '<span style="background:rgba(251,113,133,0.25); color:#fb7185; font-size:10px; font-weight:700; padding:1px 6px; border-radius:4px; margin-left:6px;">⚠️ CHOP SUPPRESSED</span>'

        cards_html.append(
            f'<div class="{card_cls} prop-font-ui">'
            f'<div class="setup-header-row">'
            f'<span class="setup-name" style="color:{dir_color};">● {s.setup_name}{suppress_badge}</span>'
            f'<div>'
            f'<span class="setup-conviction">{s.timeframe}</span>'
            f'<span class="setup-conviction" style="background:{"rgba(52,211,153,0.2)" if not is_bearish else "rgba(251,113,133,0.2)"}; color:{dir_color}; font-weight:700;">'
            f'{s.conviction}'
            f'</span>'
            f'</div>'
            f'</div>'
            f'<div class="setup-section-content"><span class="setup-section-label">Logic:</span> {s.logic}</div>'
            f'<div class="setup-section-content"><span class="setup-section-label">Action:</span> <span style="color:#38bdf8; font-weight:600;">{s.action}</span></div>'
            f'<div style="display:flex; justify-content:space-between; margin-top:6px; font-size:11px; color:#94a3b8;" class="mono-nums">'
            f'<span>Entry: <b style="color:#f8fafc;">{s.trigger_level:,.1f}</b></span>'
            f'<span>Stop: <b style="color:#fb7185;">{s.stop_loss:,.1f}</b></span>'
            f'<span>Target: <b style="color:#34d399;">{s.target_level:,.1f}</b></span>'
            f'<span>R:R: <b style="color:#fbbf24;">{s.risk_reward_ratio}</b></span>'
            f'</div>'
            f'</div>'
        )

    _render_html("".join(cards_html))


def render_zone_matrix(matrix_rows: list[ZoneMatrixRow]):
    """
    Renders Zone Matrix displaying upcoming liquidity pools combined with Option Chain S/R.
    Classifies zones as "Confirmed", "Contested", or "Weakening".
    """
    if not matrix_rows:
        _render_html("<div style='color:#94a3b8; font-size:12px;'>No active zone matrix rows available.</div>")
        return

    rows_html = []
    for r in matrix_rows:
        tag_cls = "status-tag-confirmed"
        if r.alignment == "CONTESTED": tag_cls = "status-tag-contested"
        elif r.alignment == "WEAKENING": tag_cls = "status-tag-weakening"

        type_color = "#34d399" if r.zone_type == "SUPPORT" else ("#fb7185" if r.zone_type == "RESISTANCE" else "#fbbf24")

        rows_html.append(
            f'<div class="matrix-item prop-font-ui">'
            f'<div style="display:flex; flex-direction:column; gap:2px;">'
            f'<div style="display:flex; align-items:center; gap:8px;">'
            f'<span class="matrix-level mono-nums" style="color:{type_color};">{r.level:,.0f}</span>'
            f'<span style="font-size:11.5px; font-weight:700; color:#f1f5f9;">{r.label}</span>'
            f'<span class="{tag_cls}">{r.alignment}</span>'
            f'</div>'
            f'<div style="font-size:10.5px; color:#94a3b8;">'
            f'<span style="color:#cbd5e1; font-weight:600;">OI:</span> {r.oi_evidence} | '
            f'<span style="color:#cbd5e1; font-weight:600;">SMC:</span> {r.smc_evidence}'
            f'</div>'
            f'</div>'
            f'<div style="text-align:right; font-size:11px;" class="mono-nums">'
            f'<div style="color:{"#38bdf8" if r.is_immediate else "#64748b"}; font-weight:600;">{r.distance_pts:+.0f} pts</div>'
            f'<div style="font-size:9.5px; color:#94a3b8;">{r.zone_type}</div>'
            f'</div>'
            f'</div>'
        )

    _render_html("".join(rows_html))


# ═══════════════════════════════════════════════════════════════════════════
# Primary Terminal View Entrypoint
# ═══════════════════════════════════════════════════════════════════════════

def render_institutional_terminal(
    candle_df: Optional[pd.DataFrame] = None,
    chain_df: Optional[pd.DataFrame] = None,
    spot: Optional[float] = None
):
    """
    Renders the complete institutional terminal layout:
      - The Decision Cockpit (Top Header)
      - Left Panel (Divergent OI Bar Chart + Strike Evidence Table)
      - Center/Right Panel (SMC Price Action Canvas + Active Setups Feed + Zone Matrix)
    """
    # 1. Inject Styles
    _render_html(TERMINAL_STYLES)

    if candle_df is None or (hasattr(candle_df, "empty") and candle_df.empty) or len(candle_df) < 5:
        from engine.test_terminal_engine import generate_synthetic_candles
        candle_df = generate_synthetic_candles(n=60, base_price=spot or 24150.0)

    # 2. Run Unified Engine
    engine = UnifiedTerminalEngine(pivot_window=2)
    snapshot = engine.process(candle_df=candle_df, chain_df=chain_df, spot=spot)

    # 3. Decision Cockpit (Top Header)
    render_decision_cockpit(
        report=snapshot.four_pillars,
        spot=snapshot.spot_price,
        dealing_range=snapshot.dealing_range
    )

    # 4. Strict Two-Panel Institutional Grid
    col_left, col_right = st.columns([5, 6], gap="small")

    with col_left:
        # Divergent OI Bar Chart
        _render_html(
            f'<div class="terminal-card prop-font-ui">'
            f'<div class="terminal-card-title">'
            f'<span>REAL-TIME OPTION FLOW · DIVERGENT OI</span>'
            f'<span class="mono-nums" style="color:#38bdf8;">SPOT: {snapshot.spot_price:,.1f}</span>'
            f'</div>'
            f'</div>'
        )

        atm_strike = int(round(snapshot.spot_price / 50.0) * 50)
        fig_oi = create_divergent_oi_chart(snapshot.divergent_oi_chart, atm_strike)
        st.plotly_chart(fig_oi, use_container_width=True, config={"displayModeBar": False})

        # Strike Evidence Table
        _render_html(
            f'<div class="terminal-card prop-font-ui" style="margin-top:6px;">'
            f'<div class="terminal-card-title">'
            f'<span>STRIKE EVIDENCE TABLE · TABULAR OI MIGRATION</span>'
            f'<span style="font-size:10px; color:#94a3b8;">MONOSPACE TABULAR NUMS</span>'
            f'</div>'
            f'</div>'
        )
        render_strike_evidence_table(snapshot.strike_evidence_table, atm_strike)

    with col_right:
        # SMC & Price Action Canvas
        _render_html(
            f'<div class="terminal-card prop-font-ui">'
            f'<div class="terminal-card-title">'
            f'<span>SMC & PRICE ACTION CANVAS · CONFLUENCE MATRIX</span>'
            f'<span style="color:#34d399; font-size:10px;">MSB · FVG · OB · BREAKERS · WALLS</span>'
            f'</div>'
            f'</div>'
        )

        fig_smc = create_smc_candlestick_chart(
            df=snapshot.smc_data.get("df", candle_df),
            smc_res=snapshot.smc_data,
            gamma=snapshot.gamma_profile,
            spot=snapshot.spot_price
        )
        st.plotly_chart(fig_smc, use_container_width=True, config={"displayModeBar": False})

        # Sub-split for Active Setups & Zone Matrix
        sub_col1, sub_col2 = st.columns([1, 1], gap="small")

        with sub_col1:
            _render_html(
                f'<div class="terminal-card prop-font-ui">'
                f'<div class="terminal-card-title">'
                f'<span>ACTIVE SETUPS FEED</span>'
                f'<span style="color:#38bdf8; font-size:10px;">{len(snapshot.active_setups)} ARMED</span>'
                f'</div>'
                f'</div>'
            )
            render_active_setups_feed(snapshot.active_setups)

        with sub_col2:
            _render_html(
                f'<div class="terminal-card prop-font-ui">'
                f'<div class="terminal-card-title">'
                f'<span>ZONE MATRIX · LIQUIDITY & WALLS</span>'
                f'<span style="color:#fbbf24; font-size:10px;">DEALER CONFLUENCE</span>'
                f'</div>'
                f'</div>'
            )
            render_zone_matrix(snapshot.zone_matrix)
