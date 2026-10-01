"""
breakout_tab.py — Streamlit UI Tab for Advanced Breakout, Trap Detection & Fakeout Avoidance.

Combines:
  1. The "Anti-FOMO" Active Candle HUD:
     - Prominent real-time countdown timer to candle close.
     - Dynamic Color-coded Live State Badges (Yellow Warning, Green Safe, Red Alert).
  2. Advanced Chart Annotations (Plotly):
     - BSL / HTF Liquidity horizontal lines.
     - Sweep Rejection Halo & Callout Arrow.
     - Market Structure Shift (MSS) line.
     - 3-Candle Imbalance FVG Killzone box.
     - Action Labels ("SELL HERE" / "TAKE PROFIT HIT").
     - Live toggles to eliminate visual clutter.
  3. Actionability Panel (Kanban 5-Step Pipeline):
     - [ ] BSL Swept -> [ ] Rejection Wick -> [ ] MSS -> [ ] FVG -> [ ] Retracement Entry
  4. Breakout & Trap Detection Engine:
     - Vectorized Candle Anatomy Validation (>=60% body, 20-SMA vol, short wicks).
     - False Breakout Trap Detector (Wick Sweep, Inside Bar, Double Rejection).
     - 3-Phase Retest & Continuation Scanner (Phase 1 -> Low-Vol Phase 2 -> Reversal Phase 3).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import fakeout_avoidance_ui as faui
from price_action_breakout import PriceActionBreakoutEngine
import smc
from smc import FVG_Tracker, pivots, structure


# ── Color Palette ─────────────────────────────────────────────────────────────
GREEN = "#2FBF71"
RED = "#E8503A"
BLUE = "#4A9BD1"
AMBER = "#E0A030"
PURPLE = "#9B51E0"
GOLD = "#F39C12"


def render_breakout_chart(
    df: pd.DataFrame,
    key_levels: Dict[str, float],
    timeframe: str = "5m",
) -> go.Figure:
    """
    Creates an interactive Plotly Candlestick chart overlaid with Key S/R levels,
    VWAP, Breakout markers, Trap markers, and Actionable Retest Entry points.
    """
    fig = go.Figure()
    ts = pd.to_datetime(df["timestamp"]) if "timestamp" in df.columns else pd.to_datetime(df.index)

    # 1. Candlestick trace
    fig.add_trace(
        go.Candlestick(
            x=ts,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=f"Nifty ({timeframe})",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            increasing_fillcolor=GREEN,
            decreasing_fillcolor=RED,
            line_width=1.1,
        )
    )

    # 2. VWAP Trace if available
    if "vwap" in df.columns and df["vwap"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=ts,
                y=df["vwap"],
                name="Intraday VWAP",
                line=dict(color="#38BDF8", width=1.8, dash="solid"),
            )
        )

    # 3. Horizontal lines for Key Levels (Call/Put Walls, S/R)
    for name, lvl in key_levels.items():
        if isinstance(lvl, (int, float)) and not np.isnan(lvl) and lvl > 0:
            color = RED if any(r in name.lower() for r in ["r1", "r2", "res", "call"]) else (
                GREEN if any(s in name.lower() for s in ["s1", "s2", "sup", "put"]) else GOLD
            )
            fig.add_hline(
                y=lvl,
                line=dict(color=color, width=1.4, dash="dash"),
                annotation_text=f"{name}: {lvl:,.1f}",
                annotation_position="top right",
                annotation_font=dict(color=color, size=10),
            )

    # 4. Markers for Strong Breakouts
    strong_mask = df["breakout_signal"] == "VALID_STRONG_BREAKOUT"
    if strong_mask.any():
        bull_mask = strong_mask & (df["signal_direction"] == "BULLISH")
        bear_mask = strong_mask & (df["signal_direction"] == "BEARISH")
        if bull_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ts[bull_mask],
                    y=df.loc[bull_mask, "high"] + 4.0,
                    mode="markers",
                    name="Strong Bull Breakout",
                    marker=dict(symbol="triangle-up", size=12, color="#10B981"),
                    hovertext=df.loc[bull_mask, "signal_notes"],
                )
            )
        if bear_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ts[bear_mask],
                    y=df.loc[bear_mask, "low"] - 4.0,
                    mode="markers",
                    name="Strong Bear Breakdown",
                    marker=dict(symbol="triangle-down", size=12, color="#EF4444"),
                    hovertext=df.loc[bear_mask, "signal_notes"],
                )
            )

    # 5. Markers for Traps
    trap_mask = df["trap_signal"] != ""
    if trap_mask.any():
        fig.add_trace(
            go.Scatter(
                x=ts[trap_mask],
                y=np.where(
                    df.loc[trap_mask, "signal_direction"] == "BEARISH",
                    df.loc[trap_mask, "high"] + 6.0,
                    df.loc[trap_mask, "low"] - 6.0,
                ),
                mode="markers+text",
                name="Trap Warning",
                marker=dict(symbol="diamond", size=11, color="#F59E0B", line=dict(width=1, color="#B45309")),
                text=df.loc[trap_mask, "trap_signal"].str.replace("TRAP_", ""),
                textposition="top center",
                textfont=dict(size=9, color="#F59E0B"),
                hovertext=df.loc[trap_mask, "signal_notes"],
            )
        )

    # 6. Markers for Actionable Retest Continuation Entries
    entry_mask = df["actionable_retest_entry"] != ""
    if entry_mask.any():
        fig.add_trace(
            go.Scatter(
                x=ts[entry_mask],
                y=np.where(
                    df.loc[entry_mask, "signal_direction"] == "BULLISH",
                    df.loc[entry_mask, "low"] - 10.0,
                    df.loc[entry_mask, "high"] + 10.0,
                ),
                mode="markers+text",
                name="⭐ Actionable Retest Entry",
                marker=dict(symbol="star", size=16, color="#06B6D4", line=dict(width=1.5, color="#0891B2")),
                text="ENTRY",
                textposition="bottom center",
                textfont=dict(size=10, color="#06B6D4"),
                hovertext=df.loc[entry_mask, "signal_notes"],
            )
        )

    fig.update_layout(
        title=f"Breakout & Trap Scanner — Key Levels & Retest Continuations ({timeframe})",
        height=580,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=40, r=40, t=50, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )

    return fig


def render_breakout_tab(
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    spot: float = 0.0,
    read: Optional[Any] = None,
    now: Optional[Any] = None,
):
    """
    Main entrypoint for Breakout, Trap Detection & Fakeout Avoidance tab in Streamlit.
    """
    st.markdown("### 🏹 Breakout, Trap Detection & Fakeout Avoidance Terminal")
    st.caption(
        "Institutional price action intelligence: Anti-FOMO active candle HUD, automated liquidity sweep annotations, "
        "and 3-Phase Retest Continuations across Key S/R & Option Chain OI Walls."
    )

    # 1. Select Timeframe
    available_tfs = list(pa_frames.keys()) if pa_frames else ["5m", "15m"]
    tf = st.radio(
        "Select Analysis Timeframe",
        available_tfs,
        index=available_tfs.index("5m") if "5m" in available_tfs else 0,
        horizontal=True,
        key="breakout_tf_selector",
    )

    df_raw = pa_frames.get(tf) if pa_frames else None

    if df_raw is None or len(df_raw) < 5:
        try:
            import price_action as pa
            df_raw = pa.load_history_candles(days=1, timeframe=tf)
        except Exception:
            pass

    if df_raw is None or len(df_raw) < 5:
        st.warning(f"Insufficient candle data for timeframe {tf}. Need at least 5 candles.")
        return

    # 2. Derive Key Levels (BSL, HTF Liquidity, Option Chain Walls, Equal Lows)
    ps = pivots(df_raw, n=2)
    swing_highs = [p.price for p in ps if p.kind == "HIGH"]
    swing_lows = [p.price for p in ps if p.kind == "LOW"]

    r1 = float(read.levels.r1) if (read and hasattr(read, "levels") and hasattr(read.levels, "r1")) else None
    s1 = float(read.levels.s1) if (read and hasattr(read, "levels") and hasattr(read.levels, "s1")) else None
    r2 = float(read.levels.r2) if (read and hasattr(read, "levels") and hasattr(read.levels, "r2")) else None
    s2 = float(read.levels.s2) if (read and hasattr(read, "levels") and hasattr(read.levels, "s2")) else None

    bsl_level = max(swing_highs[-2:]) if len(swing_highs) >= 2 else (r1 or float(df_raw["high"].max()))
    htf_level = r1 if r1 and r1 > bsl_level else (bsl_level + 35.0)
    equal_lows = min(swing_lows[-2:]) if len(swing_lows) >= 2 else (s1 or float(df_raw["low"].min()))

    # Key levels dictionary for Breakout Engine
    key_levels: Dict[str, Any] = {}
    if "vwap" in df_raw.columns:
        key_levels["Intraday_VWAP"] = df_raw["vwap"]
    if r1:
        key_levels["R1_Call_Wall"] = r1
    if r2:
        key_levels["R2_Call_Wall"] = r2
    if s1:
        key_levels["S1_Put_Wall"] = s1
    if s2:
        key_levels["S2_Put_Wall"] = s2

    if len(key_levels) <= 1 and spot > 0:
        base = round(spot / 50.0) * 50
        key_levels["R1_Call_Wall"] = base + 50.0
        key_levels["S1_Put_Wall"] = base - 50.0
        key_levels["R2_Call_Wall"] = base + 100.0
        key_levels["S2_Put_Wall"] = base - 100.0

    # ═══════════════════════════════════════════════════════════════════════
    # COMPONENT 1: THE "ANTI-FOMO" ACTIVE CANDLE HUD (ALWAYS ON TOP)
    # ═══════════════════════════════════════════════════════════════════════
    faui.render_anti_fomo_hud(df_raw, bsl_level, htf_level, timeframe=tf, now=now)

    # 3. View Switcher Ribbon
    view_mode = st.pills(
        "Module Focus",
        [
            "🛡️ Fakeout Avoidance (Liquidity Sweeps & FVG)",
            "🏹 Breakout & Retest Engine (Anatomy & S/R Retest)",
        ],
        default="🛡️ Fakeout Avoidance (Liquidity Sweeps & FVG)",
        key="breakout_subview_selector",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # VIEW A: FAKEOUT AVOIDANCE TERMINAL (COMPONENTS 2 & 3)
    # ═══════════════════════════════════════════════════════════════════════
    if view_mode == "🛡️ Fakeout Avoidance (Liquidity Sweeps & FVG)":
        # Run structural scan for active sweep setup
        all_sweeps = [x for x in smc.liquidity(df_raw, ps) if x.kind == "SWEEP" and x.direction == "ABOVE"]
        fvg_tracker = FVG_Tracker(df_raw)
        fvgs = [g for g in fvg_tracker.fvgs if g.direction == "BEARISH"]

        active_setup: Dict[str, Any] = {}
        if all_sweeps:
            latest_sw = all_sweeps[-1]
            active_setup["sweep_bar_idx"] = latest_sw.i
            active_setup["sweep_price"] = latest_sw.wick

            st_struct = structure(df_raw, ps)
            bear_chochs = [ev for ev in st_struct.events if ev.direction == "BEARISH" and ev.i >= latest_sw.i]
            if bear_chochs:
                active_setup["mss_level"] = bear_chochs[-1].level
                active_setup["mss_bar_idx"] = bear_chochs[-1].i

            post_fvgs = [g for g in fvgs if g.i >= latest_sw.i]
            if post_fvgs:
                target_fvg = post_fvgs[0]
                active_setup["fvg_top"] = target_fvg.top
                active_setup["fvg_bottom"] = target_fvg.bottom
                active_setup["fvg_start_idx"] = target_fvg.i

                for k in range(target_fvg.i + 1, len(df_raw)):
                    if df_raw["high"].iloc[k] >= target_fvg.bottom:
                        active_setup["entry_bar_idx"] = k
                        active_setup["entry_price"] = float(df_raw["close"].iloc[k])
                        break

        # Setup state for Kanban Tracker
        is_forming_last = bool(not df_raw.iloc[-1].get("closed", True)) if "closed" in df_raw.columns else False
        c_last = float(df_raw["close"].iloc[-1])
        h_last = float(df_raw["high"].iloc[-1])

        is_mid_testing = (h_last >= bsl_level) and is_forming_last
        bsl_swept_done = (len(all_sweeps) > 0) or (h_last >= bsl_level and not is_forming_last and c_last <= bsl_level)
        rejection_done = bool(all_sweeps)
        mss_done = bool(active_setup.get("mss_level"))
        fvg_done = bool(active_setup.get("fvg_top"))
        entry_done = bool(active_setup.get("entry_bar_idx"))

        kanban_state = {
            "bsl_swept": bsl_swept_done,
            "bsl_waiting_close": is_mid_testing,
            "bsl_detail": f"Probed BSL ({bsl_level:,.1f})",
            "rejection_closed": rejection_done,
            "rejection_detail": f"Sweep confirmed bar {all_sweeps[-1].i}" if all_sweeps else "Waiting for rejection wick",
            "mss_shifted": mss_done,
            "mss_detail": f"Low broken at {active_setup.get('mss_level', 0.0):,.1f}" if mss_done else "Displacement required",
            "fvg_formed": fvg_done,
            "fvg_detail": f"Gap: {active_setup.get('fvg_bottom', 0.0):,.1f}-{active_setup.get('fvg_top', 0.0):,.1f}" if fvg_done else "Awaiting 3-bar imbalance",
            "entry_active": entry_done,
            "entry_detail": f"Entry at {active_setup.get('entry_price', 0.0):,.1f}" if entry_done else "Awaiting FVG tap",
        }

        # Component 2: Annotation Switchboard
        with st.expander("🛠️ Visual Hierarchy & Annotation Toggles (Eliminate Clutter)", expanded=True):
            col1, col2, col3, col4, col5 = st.columns(5)
            t_liq = col1.checkbox("📌 Liquidity Lines", value=True, help="Draws BSL and HTF horizontal boundaries")
            t_sweep = col2.checkbox("🚨 Sweep Halo & Callout", value=True, help="Highlights rejection wick above BSL")
            t_mss = col3.checkbox("📐 Market Structure Shift", value=True, help="Draws displacement breakdown line")
            t_fvg = col4.checkbox("📦 FVG Killzone Box", value=True, help="Renders fair value gap imbalance box")
            t_action = col5.checkbox("🏷️ Action Markers", value=True, help="Shows 'SELL HERE' and 'TP HIT' text markers")

        # Component 2: Annotated Chart
        fig_faui = faui.build_annotated_liquidity_chart(
            df=df_raw,
            bsl_level=bsl_level,
            htf_level=htf_level,
            equal_lows_level=equal_lows,
            active_setup=active_setup,
            show_liquidity_lines=t_liq,
            show_sweep_highlight=t_sweep,
            show_mss_marker=t_mss,
            show_fvg_box=t_fvg,
            show_action_labels=t_action,
            timeframe=tf,
        )
        st.plotly_chart(fig_faui, use_container_width=True)

        # Component 3: Kanban Actionability Panel
        st.markdown("#### 🚦 Institutional Setup Kanban (Real-Time Execution Pipeline)")
        faui.render_kanban_actionability_panel(kanban_state)

        # Execution Advice Card
        if entry_done:
            ent = active_setup["entry_price"]
            sl = float(all_sweeps[-1].wick) + 2.0
            risk = max(5.0, sl - ent)
            tp1 = ent - risk
            tp2 = ent - (2 * risk)
            st.success(
                f"🎯 **ACTIONABLE REVERSAL EXECUTION (BUY PE / SHORT)**  \n"
                f"• **Entry:** `{ent:,.1f}` (Inside FVG Killzone) | **Stop Loss:** `{sl:,.1f}` (Risk: `{risk:,.1f}` pts)  \n"
                f"• **Target 1 (1:1):** `{tp1:,.1f}` | **Target 2 (Equal Lows):** `{tp2:,.1f}` (R:R 2.0x)"
            )
        elif is_mid_testing:
            st.warning(
                f"⚠️ **MID-CANDLE ALERT:** Price is actively probing BSL ({bsl_level:,.1f}). **DO NOT ENTER LONG.** "
                f"Wait for the {tf} candle to close to verify whether institutions accept value or sweep liquidity."
            )

    # ═══════════════════════════════════════════════════════════════════════
    # VIEW B: BREAKOUT & RETEST ENGINE (ANATOMY & S/R RETEST)
    # ═══════════════════════════════════════════════════════════════════════
    else:
        with st.expander("⚙️ Engine Parameters & Strictness Controls", expanded=False):
            c1, c2, c3, c4 = st.columns(4)
            min_body = c1.slider("Min Body % for Strong Breakout", 40, 80, 60, 5) / 100.0
            max_wick = c2.slider("Max Rejection Wick %", 10, 35, 20, 5) / 100.0
            trap_wick = c3.slider("Min Wick % for Sweep Trap", 25, 50, 35, 5) / 100.0
            retest_bars = c4.slider("Max Retest Window (Bars)", 1, 8, 5, 1)

        engine = PriceActionBreakoutEngine(
            min_body_ratio=min_body,
            max_breakout_wick_ratio=max_wick,
            trap_wick_ratio=trap_wick,
            retest_max_bars=retest_bars,
        )
        df_signals = engine.apply_breakout_signals(df_raw, key_levels)
        stats = engine.get_summary_stats(df_signals)

        # Executive KPI Tiles
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Strong Breakouts", f"{stats['strong_breakouts']}", help="Large body >= 60%, Volume > 20-SMA, decisive close")
        k2.metric("Weak Warnings", f"{stats['weak_breakouts']}", help="Small body, low volume, or rejection wick")
        total_traps = stats['wick_traps'] + stats['immediate_reversal_traps'] + stats['double_rejection_traps']
        k3.metric("Traps Avoided", f"{total_traps}", delta=f"{stats['wick_traps']} Sweeps, {stats['immediate_reversal_traps']} In-Bar", delta_color="inverse")
        k4.metric("Actionable Retest Entries", f"{stats['retest_entries']}", delta=f"{stats['bullish_retest_entries']} CE / {stats['bearish_retest_entries']} PE", delta_color="normal")
        latest_sig = df_signals["primary_signal"].iloc[-1] if len(df_signals) else ""
        k5.metric("Current Bar Status", latest_sig if latest_sig else "Normal Consolidation")

        chart_levels = {k: v for k, v in key_levels.items() if not isinstance(v, pd.Series)}
        fig_bo = render_breakout_chart(df_signals, chart_levels, timeframe=tf)
        st.plotly_chart(fig_bo, use_container_width=True)

        st.markdown("#### 📋 Signals & Execution Terminal")
        actionable_df = engine.extract_actionable_signals(df_signals)

        if actionable_df.empty:
            st.info("No active breakout, trap, or retest continuation signals triggered yet in this session.")
        else:
            filter_opt = st.pills(
                "Filter Signals",
                ["All Triggers", "Retest Entries Only", "Traps Only", "Strong Breakouts Only"],
                default="All Triggers",
                key="filter_signals_pill",
            )

            if filter_opt == "Retest Entries Only":
                view_df = actionable_df[actionable_df["actionable_retest_entry"] != ""]
            elif filter_opt == "Traps Only":
                view_df = actionable_df[actionable_df["trap_signal"] != ""]
            elif filter_opt == "Strong Breakouts Only":
                view_df = actionable_df[actionable_df["breakout_signal"] == "VALID_STRONG_BREAKOUT"]
            else:
                view_df = actionable_df

            display_cols = [
                "timestamp",
                "primary_signal",
                "signal_direction",
                "key_level_hit",
                "entry_price",
                "stop_loss",
                "target_1",
                "target_2",
                "risk_pts",
                "rr_ratio",
                "signal_notes",
            ]
            available_cols = [c for c in display_cols if c in view_df.columns]

            st.dataframe(
                view_df[available_cols].style.format(
                    {
                        "entry_price": "{:,.1f}",
                        "stop_loss": "{:,.1f}",
                        "target_1": "{:,.1f}",
                        "target_2": "{:,.1f}",
                        "risk_pts": "{:,.1f}",
                        "rr_ratio": "{:.1f}R",
                    },
                    na_rep="—",
                ),
                use_container_width=True,
                hide_index=True,
            )

        with st.expander("🔬 Candle Anatomy Inspector (Body %, Wicks, Volume 20-SMA)"):
            inspect_cols = [
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "body_ratio",
                "upper_wick_ratio",
                "lower_wick_ratio",
                "vol_ratio",
                "is_high_volume",
                "candle_type",
                "primary_signal",
            ]
            avail_inspect = [c for c in inspect_cols if c in df_signals.columns]
            st.dataframe(
                df_signals[avail_inspect].tail(15).style.format(
                    {
                        "open": "{:,.1f}",
                        "high": "{:,.1f}",
                        "low": "{:,.1f}",
                        "close": "{:,.1f}",
                        "body_ratio": "{:.1%}",
                        "upper_wick_ratio": "{:.1%}",
                        "lower_wick_ratio": "{:.1%}",
                        "vol_ratio": "{:.2f}x",
                    },
                    na_rep="—",
                ),
                use_container_width=True,
                hide_index=True,
            )
