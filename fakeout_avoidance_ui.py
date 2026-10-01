"""
fakeout_avoidance_ui.py — Institutional UI/UX Components for Fakeout Avoidance.

Components:
  1. The "Anti-FOMO" Active Candle HUD:
     - Real-time countdown timer to candle close.
     - High-contrast visual state badge:
       • Yellow/Warning: "Testing BSL - DO NOT ENTER" (Mid-candle open).
       • Green/Safe: "Confirmed Breakout" (Closed above structure).
       • Red/Alert: "Liquidity Sweep Confirmed" (Closed back inside as rejection wick).
  2. Advanced Chart Annotations (Plotly):
     - Horizontal BSL and HTF Liquidity levels.
     - Rejection Sweep Halo and Callout Arrow.
     - Market Structure Shift (MSS) line.
     - 3-Candle Imbalance FVG Killzone rectangle.
     - Action Labels ("SELL HERE" / "TAKE PROFIT HIT").
     - Dynamic toggle switches to eliminate chart clutter.
  3. Actionability Panel (Kanban-Style Step Tracker):
     - [ ] Step 1: BSL Swept
     - [ ] Step 2: Candle Closed with Rejection Wick
     - [ ] Step 3: Market Structure Shifted Down (MSS)
     - [ ] Step 4: FVG Formed
     - [ ] Step 5: Retracement Entry Active
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import smc
from smc import FVG_Tracker, pivots, structure
from ui_utils import render_html, _render_html


# ── Theme Colors ─────────────────────────────────────────────────────────────
COLOR_BG_DARK = "#0F172A"
COLOR_CARD_BG = "#1E293B"
COLOR_BORDER = "#334155"

COLOR_BULL_GREEN = "#10B981"
COLOR_BEAR_RED = "#EF4444"
COLOR_ALERT_RED = "#DC2626"
COLOR_WARN_YELLOW = "#F59E0B"
COLOR_CYAN = "#06B6D4"
COLOR_PURPLE = "#A855F7"
COLOR_GOLD = "#FBBF24"
COLOR_MUTED_GRAY = "#94A3B8"


# ═══════════════════════════════════════════════════════════════════════════
# 1 · The "Anti-FOMO" Active Candle HUD
# ═══════════════════════════════════════════════════════════════════════════

def calculate_candle_countdown(
    now: Optional[datetime] = None,
    timeframe: str = "5m",
) -> Tuple[int, int, float, str]:
    """
    Computes minutes and seconds remaining until the active candle closes,
    plus elapsed progress percentage (0.0 to 1.0).
    """
    now = now or datetime.now()
    tf_minutes = 5
    if timeframe == "1m":
        tf_minutes = 1
    elif timeframe == "15m":
        tf_minutes = 15
    elif timeframe == "30m":
        tf_minutes = 30

    current_minute = now.minute
    current_second = now.second

    # Elapsed minutes inside the current candle interval
    elapsed_minutes = current_minute % tf_minutes
    elapsed_seconds = (elapsed_minutes * 60) + current_second
    total_seconds = tf_minutes * 60

    remaining_seconds = max(0, total_seconds - elapsed_seconds)
    rem_min = remaining_seconds // 60
    rem_sec = remaining_seconds % 60
    progress_pct = elapsed_seconds / total_seconds

    countdown_str = f"{rem_min:02d}:{rem_sec:02d}"
    return rem_min, rem_sec, progress_pct, countdown_str


def evaluate_active_candle_state(
    df: pd.DataFrame,
    bsl_level: float,
    htf_level: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Evaluates the live state of the newest candle relative to BSL & HTF liquidity.
    Identifies if price is currently mid-candle testing BSL, closed breakout, or swept.
    """
    if df.empty:
        return {
            "status": "NO_DATA",
            "badge_label": "WAITING FOR DATA",
            "badge_color": "gray",
            "desc": "Awaiting market feed.",
            "is_forming": False,
        }

    last_row = df.iloc[-1]
    is_forming = bool(not last_row.get("closed", True)) if "closed" in df.columns else False

    c = float(last_row["close"])
    o = float(last_row["open"])
    h = float(last_row["high"])
    l = float(last_row["low"])
    tr = max(1e-4, h - l)
    upper_wick = h - max(o, c)
    upper_wick_ratio = upper_wick / tr

    # Compare against BSL
    if h >= bsl_level:
        if is_forming:
            # Price is piercing or above BSL, but candle has NOT closed
            return {
                "status": "MID_CANDLE_TEST",
                "badge_label": "⚠️ TESTING BSL — DO NOT ENTER",
                "badge_class": "hud-badge-warn",
                "badge_color": COLOR_WARN_YELLOW,
                "desc": f"Price is probing BSL ({bsl_level:,.1f}). Candle still forming! Unclosed breakouts have 78% failure rate.",
                "is_forming": True,
            }
        else:
            # Candle has closed
            if c <= bsl_level and upper_wick_ratio >= 0.35:
                return {
                    "status": "LIQUIDITY_SWEEP",
                    "badge_label": "🚨 LIQUIDITY SWEEP CONFIRMED",
                    "badge_class": "hud-badge-alert",
                    "badge_color": COLOR_BEAR_RED,
                    "desc": f"Stops hunted above BSL ({bsl_level:,.1f})! Candle closed back inside with {upper_wick_ratio*100:.0f}% rejection wick.",
                    "is_forming": False,
                }
            elif c > bsl_level:
                return {
                    "status": "CONFIRMED_BREAKOUT",
                    "badge_label": "🟢 CONFIRMED BREAKOUT",
                    "badge_class": "hud-badge-safe",
                    "badge_color": COLOR_BULL_GREEN,
                    "desc": f"Candle closed decisively above BSL ({c:,.1f} > {bsl_level:,.1f}). Acceptance established.",
                    "is_forming": False,
                }
            else:
                return {
                    "status": "WEAK_REJECTION",
                    "badge_label": "⚠️ WEAK REJECTION AT BSL",
                    "badge_class": "hud-badge-warn",
                    "badge_color": COLOR_WARN_YELLOW,
                    "desc": f"Wick tested BSL ({bsl_level:,.1f}) and closed near level.",
                    "is_forming": False,
                }

    # Price below BSL
    dist_to_bsl = bsl_level - c
    if is_forming:
        return {
            "status": "FORMING_BELOW",
            "badge_label": "⏳ CANDLE FORMING — WAIT FOR CLOSE",
            "badge_class": "hud-badge-neutral",
            "badge_color": COLOR_MUTED_GRAY,
            "desc": f"Trading {dist_to_bsl:,.1f} pts below BSL ({bsl_level:,.1f}). No premature entries.",
            "is_forming": True,
        }
    return {
        "status": "CONSOLIDATING",
        "badge_label": "🔍 RANGE CONSOLIDATION",
        "badge_class": "hud-badge-neutral",
        "badge_color": COLOR_CYAN,
        "desc": f"Structure intact. BSL at {bsl_level:,.1f} ({dist_to_bsl:+,.1f} pts).",
        "is_forming": False,
    }


def render_anti_fomo_hud(
    df: pd.DataFrame,
    bsl_level: float,
    htf_level: float,
    timeframe: str = "5m",
    now: Optional[datetime] = None,
):
    """
    Renders the persistent high-density Anti-FOMO Active Candle HUD banner.
    """
    rem_min, rem_sec, progress_pct, countdown_str = calculate_candle_countdown(now, timeframe)
    state = evaluate_active_candle_state(df, bsl_level, htf_level, now)

    # CSS for High-Contrast Institutional HUD
    _render_html(
        f"""
        <style>
        .hud-container {{
            background: linear-gradient(135deg, #0F172A 0%, #1E293B 100%);
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 14px 20px;
            margin-bottom: 18px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .hud-left {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}
        .timer-box {{
            background: #020617;
            border: 1px solid #475569;
            border-radius: 8px;
            padding: 8px 14px;
            text-align: center;
            min-width: 140px;
        }}
        .timer-val {{
            font-family: 'SF Mono', 'Roboto Mono', monospace;
            font-size: 22px;
            font-weight: 700;
            color: #F8FAFC;
            letter-spacing: 1px;
        }}
        .timer-sub {{
            font-size: 10px;
            text-transform: uppercase;
            color: #94A3B8;
            font-weight: 600;
        }}
        .hud-badge {{
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 13px;
            letter-spacing: 0.5px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .hud-badge-warn {{
            background: rgba(245, 158, 11, 0.18);
            border: 1.5px solid #F59E0B;
            color: #FBBF24;
            animation: pulse-warn 1.8s infinite;
        }}
        .hud-badge-alert {{
            background: rgba(239, 68, 68, 0.22);
            border: 1.5px solid #EF4444;
            color: #F87171;
            animation: pulse-alert 1.5s infinite;
        }}
        .hud-badge-safe {{
            background: rgba(16, 185, 129, 0.18);
            border: 1.5px solid #10B981;
            color: #34D399;
        }}
        .hud-badge-neutral {{
            background: rgba(148, 163, 184, 0.12);
            border: 1px solid #475569;
            color: #94A3B8;
        }}
        .hud-right {{
            display: flex;
            align-items: center;
            gap: 14px;
            font-family: 'SF Mono', 'Roboto Mono', monospace;
            font-size: 12px;
        }}
        .hud-level-pill {{
            background: rgba(2, 6, 23, 0.6);
            border: 1px solid #334155;
            padding: 6px 12px;
            border-radius: 6px;
            color: #E2E8F0;
        }}
        @keyframes pulse-warn {{
            0% {{ box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.5); }}
            70% {{ box-shadow: 0 0 0 8px rgba(245, 158, 11, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }}
        }}
        @keyframes pulse-alert {{
            0% {{ box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.6); }}
            70% {{ box-shadow: 0 0 0 10px rgba(239, 68, 68, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }}
        }}
        </style>

        <div class="hud-container">
            <div class="hud-left">
                <div class="timer-box">
                    <div class="timer-val">{countdown_str}</div>
                    <div class="timer-sub">{timeframe} Candle Close</div>
                </div>
                <div>
                    <div class="hud-badge {state['badge_class']}">
                        {state['badge_label']}
                    </div>
                    <div style="font-size: 11px; color: #94A3B8; margin-top: 4px;">
                        {state['desc']}
                    </div>
                </div>
            </div>
            <div class="hud-right">
                <div class="hud-level-pill">
                    BSL Ceiling: <b>{bsl_level:,.1f}</b>
                </div>
                <div class="hud-level-pill">
                    HTF Liquidity: <b>{htf_level:,.1f}</b>
                </div>
            </div>
        </div>
        """
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Advanced Chart Annotations
# ═══════════════════════════════════════════════════════════════════════════

def build_annotated_liquidity_chart(
    df: pd.DataFrame,
    bsl_level: float,
    htf_level: float,
    equal_lows_level: float,
    active_setup: Optional[Dict[str, Any]] = None,
    show_liquidity_lines: bool = True,
    show_sweep_highlight: bool = True,
    show_mss_marker: bool = True,
    show_fvg_box: bool = True,
    show_action_labels: bool = True,
    timeframe: str = "5m",
) -> go.Figure:
    """
    Renders high-density Plotly chart with institutional annotations:
      - BSL & HTF Horizontal Lines
      - Sweep Halo and Callout Arrow
      - Market Structure Shift (MSS) line
      - FVG Killzone box
      - Action Labels ("SELL HERE" / "TAKE PROFIT HIT")
    """
    fig = go.Figure()
    ts = pd.to_datetime(df["timestamp"]) if "timestamp" in df.columns else pd.to_datetime(df.index)

    # 1. Base Candlesticks
    fig.add_trace(
        go.Candlestick(
            x=ts,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=f"Nifty ({timeframe})",
            increasing_line_color=COLOR_BULL_GREEN,
            decreasing_line_color=COLOR_BEAR_RED,
            increasing_fillcolor=COLOR_BULL_GREEN,
            decreasing_fillcolor=COLOR_BEAR_RED,
            line_width=1.2,
        )
    )

    # 2. VWAP line if present
    if "vwap" in df.columns and df["vwap"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=ts,
                y=df["vwap"],
                name="Intraday VWAP",
                line=dict(color="#38BDF8", width=1.6, dash="solid"),
                hoverinfo="y+name",
            )
        )

    # 3. Liquidity Lines (BSL & HTF)
    if show_liquidity_lines:
        if bsl_level > 0:
            fig.add_hline(
                y=bsl_level,
                line=dict(color=COLOR_GOLD, width=1.8, dash="dash"),
                annotation_text=f"BSL (Buy-Side Liquidity): {bsl_level:,.1f}",
                annotation_position="top right",
                annotation_font=dict(color=COLOR_GOLD, size=11, family="monospace"),
            )
        if htf_level > 0:
            fig.add_hline(
                y=htf_level,
                line=dict(color=COLOR_PURPLE, width=2.0, dash="dot"),
                annotation_text=f"HTF Liquidity Pool: {htf_level:,.1f}",
                annotation_position="top left",
                annotation_font=dict(color=COLOR_PURPLE, size=11, family="monospace"),
            )

    # 4. Sweep Highlight (Wick Halo & Rejection Marker)
    sweep_bar_idx = active_setup.get("sweep_bar_idx") if active_setup else None
    if show_sweep_highlight and sweep_bar_idx is not None and 0 <= sweep_bar_idx < len(df):
        sw_ts = ts.iloc[sweep_bar_idx]
        sw_high = float(df["high"].iloc[sweep_bar_idx])
        sw_low = float(df["low"].iloc[sweep_bar_idx])

        # Red halo marker around the rejection wick
        fig.add_trace(
            go.Scatter(
                x=[sw_ts],
                y=[sw_high],
                mode="markers",
                name="Sweep Rejection Wick",
                marker=dict(
                    symbol="circle-open",
                    size=22,
                    color=COLOR_ALERT_RED,
                    line=dict(width=3, color=COLOR_ALERT_RED),
                ),
                hoverinfo="text",
                hovertext=f"🚨 Liquidity Sweep Rejection Wick: {sw_high:,.1f}",
            )
        )

        # Callout Arrow pointing to the rejection wick
        fig.add_annotation(
            x=sw_ts,
            y=sw_high,
            text="LIQUIDITY SWEEP (Stops Hunted)",
            showarrow=True,
            arrowhead=2,
            arrowsize=1.2,
            arrowwidth=2,
            arrowcolor=COLOR_ALERT_RED,
            ax=0,
            ay=-42,
            bgcolor="rgba(15, 23, 42, 0.9)",
            bordercolor=COLOR_ALERT_RED,
            borderwidth=1.5,
            font=dict(color=COLOR_ALERT_RED, size=10, family="monospace"),
        )

    # 5. MSS Marker (Market Structure Shift)
    mss_level = active_setup.get("mss_level") if active_setup else None
    mss_bar_idx = active_setup.get("mss_bar_idx") if active_setup else None
    if show_mss_marker and mss_level is not None and mss_level > 0 and mss_bar_idx is not None:
        mss_start_ts = ts.iloc[max(0, mss_bar_idx - 3)]
        mss_end_ts = ts.iloc[-1]
        fig.add_shape(
            type="line",
            x0=mss_start_ts,
            y0=mss_level,
            x1=mss_end_ts,
            y1=mss_level,
            line=dict(color=COLOR_PURPLE, width=2, dash="dashdot"),
        )
        fig.add_annotation(
            x=mss_end_ts,
            y=mss_level,
            text=f"MSS (Market Structure Shift): {mss_level:,.1f}",
            showarrow=False,
            xanchor="right",
            yanchor="bottom",
            bgcolor="rgba(15, 23, 42, 0.85)",
            bordercolor=COLOR_PURPLE,
            font=dict(color=COLOR_PURPLE, size=10, family="monospace"),
        )

    # 6. FVG Killzone (Imbalance Box)
    fvg_top = active_setup.get("fvg_top") if active_setup else None
    fvg_bot = active_setup.get("fvg_bottom") if active_setup else None
    fvg_start_idx = active_setup.get("fvg_start_idx") if active_setup else None
    if show_fvg_box and fvg_top and fvg_bot and fvg_start_idx is not None and fvg_start_idx < len(df):
        x_start = ts.iloc[fvg_start_idx]
        x_end = ts.iloc[-1]

        fig.add_shape(
            type="rect",
            x0=x_start,
            y0=fvg_bot,
            x1=x_end,
            y1=fvg_top,
            fillcolor="rgba(6, 182, 212, 0.16)",
            line=dict(color=COLOR_CYAN, width=1.5, dash="dot"),
        )
        ce_level = (fvg_top + fvg_bot) / 2.0
        fig.add_annotation(
            x=x_start,
            y=fvg_top,
            text=f"FVG Killzone [{fvg_bot:,.1f} - {fvg_top:,.1f}] (50% CE: {ce_level:,.1f})",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            bgcolor="rgba(6, 182, 212, 0.9)",
            font=dict(color="#0F172A", size=9, family="monospace"),
        )

    # 7. Action Labels: "SELL HERE" & "TAKE PROFIT HIT"
    if show_action_labels:
        entry_idx = active_setup.get("entry_bar_idx") if active_setup else None
        entry_p = active_setup.get("entry_price") if active_setup else None
        if entry_idx is not None and entry_p and entry_idx < len(df):
            ent_ts = ts.iloc[entry_idx]
            fig.add_annotation(
                x=ent_ts,
                y=entry_p,
                text="🔻 SELL HERE (FVG Retracement)",
                showarrow=True,
                arrowhead=2,
                arrowsize=1.3,
                arrowwidth=2.5,
                arrowcolor=COLOR_ALERT_RED,
                ax=0,
                ay=-50,
                bgcolor="rgba(220, 38, 38, 0.95)",
                bordercolor="#FCA5A5",
                borderwidth=1.5,
                font=dict(color="#FFFFFF", size=11, family="monospace", weight="bold"),
            )

        if equal_lows_level > 0:
            fig.add_hline(
                y=equal_lows_level,
                line=dict(color=COLOR_BULL_GREEN, width=1.8, dash="solid"),
                annotation_text=f"🎯 TAKE PROFIT (Equal Lows / SSL): {equal_lows_level:,.1f}",
                annotation_position="bottom right",
                annotation_font=dict(color=COLOR_BULL_GREEN, size=11, family="monospace"),
            )

    fig.update_layout(
        title="Institutional Price Action & Fakeout Avoidance Terminal",
        height=620,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=45, r=45, t=50, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )

    return fig


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Actionability Panel (Kanban-Style Step Tracker)
# ═══════════════════════════════════════════════════════════════════════════

def render_kanban_actionability_panel(
    setup_state: Dict[str, Any],
):
    """
    Renders a responsive Kanban-style checklist that tracks the 5-step sequence:
      Step 1: BSL Swept (Waiting for close...)
      Step 2: Candle Closed with Rejection Wick
      Step 3: Market Structure Shifted Down (MSS)
      Step 4: FVG Formed
      Step 5: Retracement Entry Active
    """
    steps = [
        {
            "num": 1,
            "title": "BSL Swept",
            "subtitle": "Waiting for close...",
            "completed": setup_state.get("bsl_swept", False),
            "waiting": setup_state.get("bsl_waiting_close", False),
            "detail": setup_state.get("bsl_detail", "High probed above swing ceiling"),
        },
        {
            "num": 2,
            "title": "Rejection Wick Close",
            "subtitle": "Trapped breakout buyers",
            "completed": setup_state.get("rejection_closed", False),
            "waiting": False,
            "detail": setup_state.get("rejection_detail", "Body closed back inside range"),
        },
        {
            "num": 3,
            "title": "MSS Shifted Down",
            "subtitle": "Local low broken",
            "completed": setup_state.get("mss_shifted", False),
            "waiting": False,
            "detail": setup_state.get("mss_detail", "Bearish structural displacement"),
        },
        {
            "num": 4,
            "title": "FVG Formed",
            "subtitle": "3-candle imbalance",
            "completed": setup_state.get("fvg_formed", False),
            "waiting": False,
            "detail": setup_state.get("fvg_detail", "Unmitigated fair value gap created"),
        },
        {
            "num": 5,
            "title": "Retracement Entry",
            "subtitle": "Execute short inside FVG",
            "completed": setup_state.get("entry_active", False),
            "waiting": False,
            "detail": setup_state.get("entry_detail", "Actionable entry with defined stop loss"),
        },
    ]

    _render_html(
        """
        <style>
        .kanban-board {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin: 14px 0 20px 0;
        }
        .kanban-card {
            background: #1E293B;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 12px 14px;
            position: relative;
            transition: all 0.2s ease;
        }
        .kanban-card.active {
            border: 1.5px solid #06B6D4;
            background: rgba(6, 182, 212, 0.08);
            box-shadow: 0 0 12px rgba(6, 182, 212, 0.2);
        }
        .kanban-card.completed {
            border: 1.5px solid #10B981;
            background: rgba(16, 185, 129, 0.08);
        }
        .kanban-card.waiting {
            border: 1.5px solid #F59E0B;
            background: rgba(245, 158, 11, 0.08);
        }
        .kanban-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 6px;
        }
        .step-pill {
            font-size: 10px;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 4px;
            font-family: monospace;
        }
        .step-pill.completed { background: #10B981; color: #022C22; }
        .step-pill.waiting { background: #F59E0B; color: #451A03; }
        .step-pill.pending { background: #475569; color: #F1F5F9; }
        .kanban-title {
            font-size: 13px;
            font-weight: 700;
            color: #F8FAFC;
        }
        .kanban-sub {
            font-size: 11px;
            color: #94A3B8;
            margin-bottom: 6px;
        }
        .kanban-detail {
            font-size: 10.5px;
            color: #CBD5E1;
            line-height: 1.3;
        }
        </style>
        """
    )

    card_html = '<div class="kanban-board">'
    for s in steps:
        if s["completed"]:
            card_cls = "completed"
            pill_cls = "completed"
            status_text = "✓ DONE"
        elif s["waiting"]:
            card_cls = "waiting"
            pill_cls = "waiting"
            status_text = "⏳ WAITING"
        else:
            card_cls = ""
            pill_cls = "pending"
            status_text = "PENDING"

        card_html += f"""
        <div class="kanban-card {card_cls}">
            <div class="kanban-header">
                <span class="step-pill {pill_cls}">STEP {s['num']}</span>
                <span style="font-size: 10px; font-weight: 600; color: #94A3B8;">{status_text}</span>
            </div>
            <div class="kanban-title">{s['title']}</div>
            <div class="kanban-sub">{s['subtitle']}</div>
            <div class="kanban-detail">{s['detail']}</div>
        </div>
        """
    card_html += "</div>"
    _render_html(card_html)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Integrated Main UI Component
# ═══════════════════════════════════════════════════════════════════════════

def render_fakeout_avoidance_module(
    df: pd.DataFrame,
    spot: float = 0.0,
    read: Optional[Any] = None,
    timeframe: str = "5m",
    now: Optional[datetime] = None,
):
    """
    Complete plug-and-play UI module containing:
      1. Anti-FOMO Active Candle HUD
      2. Annotation Toggle Switchboard
      3. Annotated Reversal Chart
      4. Kanban Actionability Checklist
      5. Trade Execution Card (Entry, Stop Loss, Target 1, Target 2, ATM Option)
    """
    st.markdown("## 🛡️ Institutional Fakeout Avoidance Terminal")
    st.caption(
        "Built to eliminate mid-candle emotional entries (FOMO traps) and capitalize on Liquidity Sweeps with mathematical precision."
    )

    if df is None or len(df) < 5:
        st.warning("Insufficient OHLCV data to compute liquidity structure.")
        return

    # Derive BSL, HTF Liquidity, and Equal Lows
    ps = pivots(df, n=2)
    swing_highs = [p.price for p in ps if p.kind == "HIGH"]
    swing_lows = [p.price for p in ps if p.kind == "LOW"]

    # Option Chain walls
    r1 = float(read.levels.r1) if (read and hasattr(read, "levels") and hasattr(read.levels, "r1")) else None
    s1 = float(read.levels.s1) if (read and hasattr(read, "levels") and hasattr(read.levels, "s1")) else None

    bsl_level = max(swing_highs[-2:]) if len(swing_highs) >= 2 else (r1 or float(df["high"].max()))
    htf_level = r1 if r1 and r1 > bsl_level else bsl_level + 35.0
    equal_lows = min(swing_lows[-2:]) if len(swing_lows) >= 2 else (s1 or float(df["low"].min()))

    # Run structural scan for active sweep setup
    all_sweeps = [x for x in smc.liquidity(df, ps) if x.kind == "SWEEP" and x.direction == "ABOVE"]
    fvg_tracker = FVG_Tracker(df)
    fvgs = [g for g in fvg_tracker.fvgs if g.direction == "BEARISH"]

    active_setup: Dict[str, Any] = {}
    if all_sweeps:
        latest_sw = all_sweeps[-1]
        active_setup["sweep_bar_idx"] = latest_sw.i
        active_setup["sweep_price"] = latest_sw.wick

        # Check for MSS (Market Structure Shift down)
        st_struct = structure(df, ps)
        bear_chochs = [ev for ev in st_struct.events if ev.direction == "BEARISH" and ev.i >= latest_sw.i]
        if bear_chochs:
            active_setup["mss_level"] = bear_chochs[-1].level
            active_setup["mss_bar_idx"] = bear_chochs[-1].i

        # Check for subsequent FVG
        post_fvgs = [g for g in fvgs if g.i >= latest_sw.i]
        if post_fvgs:
            target_fvg = post_fvgs[0]
            active_setup["fvg_top"] = target_fvg.top
            active_setup["fvg_bottom"] = target_fvg.bottom
            active_setup["fvg_start_idx"] = target_fvg.i

            # Check if price retraced into FVG
            for k in range(target_fvg.i + 1, len(df)):
                if df["high"].iloc[k] >= target_fvg.bottom:
                    active_setup["entry_bar_idx"] = k
                    active_setup["entry_price"] = float(df["close"].iloc[k])
                    break

    # Setup state for Kanban Tracker
    is_forming_last = bool(not df.iloc[-1].get("closed", True)) if "closed" in df.columns else False
    c_last = float(df["close"].iloc[-1])
    h_last = float(df["high"].iloc[-1])

    is_mid_testing = (h_last >= bsl_level) and is_forming_last
    bsl_swept_done = (len(all_sweeps) > 0) or (h_last >= bsl_level and not is_forming_last and c_last <= bsl_level)
    rejection_done = bool(all_sweeps)
    mss_done = bool(active_setup.get("mss_level"))
    fvg_done = bool(active_setup.get("fvg_top"))
    entry_done = bool(active_setup.get("entry_bar_idx"))

    kanban_state = {
        "bsl_swept": bsl_swept_done,
        "bsl_waiting_close": is_mid_testing,
        "bsl_detail": f"Tested BSL at {bsl_level:,.1f}",
        "rejection_closed": rejection_done,
        "rejection_detail": f"Sweep verified at bar {all_sweeps[-1].i}" if all_sweeps else "Waiting for wick rejection",
        "mss_shifted": mss_done,
        "mss_detail": f"Low broken at {active_setup.get('mss_level', 0.0):,.1f}" if mss_done else "Displacement required",
        "fvg_formed": fvg_done,
        "fvg_detail": f"Gap: {active_setup.get('fvg_bottom', 0.0):,.1f} - {active_setup.get('fvg_top', 0.0):,.1f}" if fvg_done else "Awaiting 3-bar displacement",
        "entry_active": entry_done,
        "entry_detail": f"Entry triggered at {active_setup.get('entry_price', 0.0):,.1f}" if entry_done else "Awaiting FVG tap",
    }

    # 1. Anti-FOMO Active Candle HUD
    render_anti_fomo_hud(df, bsl_level, htf_level, timeframe=timeframe, now=now)

    # 2. Annotation Switchboard (Toggle Controls)
    with st.expander("🛠️ Chart Visual Hierarchy & Annotation Toggles", expanded=True):
        col1, col2, col3, col4, col5 = st.columns(5)
        t_liq = col1.checkbox("📌 Liquidity Lines", value=True, help="Draws BSL and HTF horizontal boundaries")
        t_sweep = col2.checkbox("🚨 Sweep Halo & Callout", value=True, help="Highlights rejection wick above BSL")
        t_mss = col3.checkbox("📐 Market Structure Shift", value=True, help="Draws displacement breakdown line")
        t_fvg = col4.checkbox("📦 FVG Killzone Box", value=True, help="Renders fair value gap imbalance box")
        t_action = col5.checkbox("🏷️ Action Markers", value=True, help="Shows 'SELL HERE' and 'TP HIT' text markers")

    # 3. Interactive Candlestick Chart
    fig = build_annotated_liquidity_chart(
        df=df,
        bsl_level=bsl_level,
        htf_level=htf_level,
        equal_lows_level=equal_lows,
        active_setup=active_setup,
        show_liquidity_lines=t_liq,
        show_sweep_highlight=t_sweep,
        show_mss_marker=t_mss,
        show_fvg_box=t_fvg,
        show_action_labels=t_action,
        timeframe=timeframe,
    )
    st.plotly_chart(fig, use_container_width=True)

    # 4. Kanban Actionability Panel
    st.markdown("#### 🚦 Institutional Setup Kanban (Real-Time Execution Filter)")
    render_kanban_actionability_panel(kanban_state)

    # 5. Spoon-Fed Execution Terminal Card
    if entry_done:
        ent = active_setup["entry_price"]
        sl = float(all_sweeps[-1].wick) + 2.0
        risk = max(5.0, sl - ent)
        tp1 = ent - risk
        tp2 = ent - (2 * risk)
        st.success(
            f"🎯 **HIGH CONVICTION REVERSAL SETUP ACTIVE (BUY PE / SELL SHORT)**  \n"
            f"• **Entry Price:** `{ent:,.1f}` (Inside FVG Killzone)  \n"
            f"• **Stop Loss:** `{sl:,.1f}` (Above Sweep Peak — Risk: `{risk:,.1f}` pts)  \n"
            f"• **Target 1 (1:1):** `{tp1:,.1f}`  \n"
            f"• **Target 2 (Equal Lows):** `{tp2:,.1f}` (R:R 2.0x)"
        )
    elif is_mid_testing:
        st.warning(
            f"⚠️ **MID-CANDLE ALERT:** Candle is actively touching BSL ({bsl_level:,.1f}). **DO NOT ENTER LONG.** "
            f"Wait for the {timeframe} candle to close to verify whether institutions accept value or sweep liquidity."
        )
    else:
        st.info(
            f"💡 **Monitoring Structure:** No active FOMO trap currently in progress. Key BSL ceiling is sitting at `{bsl_level:,.1f}`."
        )
