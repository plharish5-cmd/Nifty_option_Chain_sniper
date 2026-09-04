"""
chart_patterns_tab.py — Dedicated tab for V-A-W-M Chart Patterns and the 15m Session Reversal Matrix.

Features:
  1. Interactive Candlestick Chart (15m and 30m) with V, A, W, M overlays, necklines, stops & targets.
  2. Pattern Scanner: Filters for V-Bottom, A-Top, W-Double Bottom, M-Double Top.
  3. Session Reversal Matrix (15m): All 10 Mentor setups evaluated strictly on 15m closed candles.
"""
from __future__ import annotations

from datetime import datetime
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import chart_patterns as cp
import session_reversal_matrix as srm

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
CYAN, BLUE, AMBER = "#00D2D3", "#4A9BD1", "#E0A030"


def render(
    pa_frames: dict | None,
    spot: float | None = None,
    read: Any = None,
    prev_close: float | None = None,
    df: pd.DataFrame | None = None,
) -> None:
    """
    Renders the dedicated Chart Patterns (V-A-W-M) & Session Reversal Matrix tab.
    """
    st.markdown("### 📐 V-A-W-M Chart Patterns & Session Reversal Matrix (15m)")

    # Mentor Timeframe Discipline Callout
    st.info(
        "🧠 **MENTOR TIMEFRAME DISCIPLINE:** *\"Stick strictly to the 15-minute timeframe. "
        "Ignore the 5-minute chart. In 5 minutes, you will see random Dojis and jump into bad trades prematurely. "
        "Observe and execute on 15 minutes only.\"*"
    )

    # Top & Bottom Reversal Sniper
    try:
        import top_bottom_sniper as _tbs
        _df_5 = pa_frames.get("5m") if pa_frames else None
        _df_15 = pa_frames.get("15m") if pa_frames else None
        _r1_v = float(read.levels.r1) if read and hasattr(read, "levels") and hasattr(read.levels, "r1") else None
        _s1_v = float(read.levels.s1) if read and hasattr(read, "levels") and hasattr(read.levels, "s1") else None
        _sniper_rep = _tbs.evaluate_sniper(_df_5, _df_15, spot or 0.0, r1=_r1_v, s1=_s1_v)
        _tbs.render_sniper_card(_sniper_rep)
        st.divider()
    except Exception:
        pass

    if not pa_frames or "15m" not in pa_frames:
        st.warning("15-minute candle data is currently unavailable. Check data sources or connection.")
        return

    df_15m = pa_frames.get("15m")
    df_30m = pa_frames.get("30m")

    # If 30m frame was not yet built in pa_frames, resample from 15m or 1m if available
    if df_30m is None or df_30m.empty:
        try:
            import price_action as pa
            if "1m" in pa_frames and not pa_frames["1m"].empty:
                df_30m = pa.resample(pa_frames["1m"], 30)
                df_30m = pa.add_emas(df_30m)
                df_30m = pa.add_atr(df_30m)
                df_30m = pa.add_vwap_self(df_30m)
            elif df_15m is not None and not df_15m.empty:
                df_30m = pa.resample(df_15m, 30)
                df_30m = pa.add_emas(df_30m)
                df_30m = pa.add_atr(df_30m)
                df_30m = pa.add_vwap_self(df_30m)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 1: V-A-W-M CHART PATTERN DETECTOR
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### 🔍 Institutional Chart Patterns (V · A · W · M)")

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1, 1.2, 1.2])

    with ctrl_col1:
        selected_tf = st.radio(
            "Chart Timeframe",
            options=["15m", "30m"],
            index=0,
            horizontal=True,
            help="15m for intraday execution; 30m for higher-timeframe structural swings."
        )

    target_df = df_15m if selected_tf == "15m" else (df_30m if df_30m is not None and not df_30m.empty else df_15m)

    # Detect patterns
    all_patterns = cp.detect_chart_patterns(target_df, timeframe=selected_tf)

    with ctrl_col2:
        pattern_filter = st.selectbox(
            "Pattern Filter",
            options=["All Patterns", "W — Double Bottom", "M — Double Top", "V — Sharp Reversal", "A — Sharp Reversal"],
            index=0,
        )

    with ctrl_col3:
        status_filter = st.selectbox(
            "Status Filter",
            options=["Active / Armed / Breakout", "All (including Hit & Invalidated)"],
            index=0,
        )

    # Filter patterns
    filtered_patterns = all_patterns
    if pattern_filter == "W — Double Bottom":
        filtered_patterns = [p for p in filtered_patterns if p.pattern_type == "W"]
    elif pattern_filter == "M — Double Top":
        filtered_patterns = [p for p in filtered_patterns if p.pattern_type == "M"]
    elif pattern_filter == "V — Sharp Reversal":
        filtered_patterns = [p for p in filtered_patterns if p.pattern_type == "V"]
    elif pattern_filter == "A — Sharp Reversal":
        filtered_patterns = [p for p in filtered_patterns if p.pattern_type == "A"]

    if status_filter == "Active / Armed / Breakout":
        active_pats = [p for p in filtered_patterns if p.status in ("CONFIRMED_BREAKOUT", "ARMED", "FORMING")]
        # If no active, keep all so the chart isn't empty
        if active_pats:
            filtered_patterns = active_pats

    # Pattern selector for chart overlay
    active_pat_idx = 0
    if filtered_patterns:
        pat_labels = [
            f"[{p.status}] {p.name} ({p.points[0].time.strftime('%d-%b %H:%M')}) — R:R {p.rr_ratio}:1"
            for p in filtered_patterns
        ]
        selected_label = st.selectbox("Highlight Pattern Overlay on Chart", pat_labels, index=0)
        active_pat_idx = pat_labels.index(selected_label)

    # Interactive Chart
    fig = cp.build_pattern_chart(
        df=target_df,
        patterns=filtered_patterns,
        timeframe=selected_tf,
        spot=spot,
        active_pattern_idx=active_pat_idx,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Display highlighted pattern metrics card
    if filtered_patterns:
        sel_p = filtered_patterns[active_pat_idx]
        with st.container():
            st.markdown(
                f"""
                <div style="background-color: #0e2238; padding: 14px 18px; border-radius: 8px; border-left: 4px solid {sel_p.badge_color}; margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-size: 16px; font-weight: bold; color: #FFFFFF;">
                            {sel_p.name} · <span style="color: {sel_p.badge_color};">{sel_p.status}</span>
                        </span>
                        <span style="font-size: 13px; color: {SLATE};">
                            Timeframe: <b>{sel_p.timeframe}</b> · Formed at {sel_p.created_at.strftime('%H:%M')}
                        </span>
                    </div>
                    <div style="margin-top: 6px; font-size: 13px; color: #d0dbe5;">
                        <b>Candle Confirmation:</b> {sel_p.confirmation_candle or 'Standard Pivot'} &nbsp;|&nbsp;
                        <b>Description:</b> {sel_p.description}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

            mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
            mcol1.metric("Neckline Trigger", f"{sel_p.entry_trigger:,.1f}")
            mcol2.metric("Mentor Early Entry", f"{sel_p.early_trigger:,.1f}" if sel_p.early_trigger else "—")
            mcol3.metric("Stop Loss", f"{sel_p.stop_loss:,.1f}", delta=f"-{sel_p.risk_pts:.1f} pts", delta_color="inverse")
            mcol4.metric("Target (100% Proj)", f"{sel_p.target_2:,.1f}", delta=f"+{sel_p.reward_pts:.1f} pts")
            mcol5.metric("Risk-to-Reward", f"{sel_p.rr_ratio}:1")

            st.caption(f"💡 **Mentor Execution Rule:** {sel_p.mentor_rule}")
    else:
        st.info("No patterns found matching current filters.")

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 2: SESSION REVERSAL MATRIX (15m) — THE 10 MENTOR CONDITIONS
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("#### 🛡️ Session Reversal Matrix (15m) — 10 Institutional Setups")
    st.caption("Systematically evaluates today's 15-minute closed candles against all 10 Mentor resistance and support reversal points.")

    levels_obj = getattr(read, "levels", None)
    matrix_res = srm.evaluate_session_reversals(
        df_15m=df_15m,
        prev_close=prev_close,
        spot=spot,
        levels=levels_obj,
        chain_df=df,
    )

    # Session Status Metric Header
    sc1, sc2, sc3, sc4 = st.columns(4)
    gap_str = f"{matrix_res.gap_points:+.1f} pts ({matrix_res.gap_type})" if matrix_res.gap_points is not None else "Unknown"
    sc1.metric("Session Open Gap", gap_str)
    sc2.metric("Day High / Low", f"{matrix_res.day_high:,.1f} / {matrix_res.day_low:,.1f}" if matrix_res.day_high else "—")
    sc3.metric("Active Matrix Setups", f"{matrix_res.active_count} / 10")
    sc4.metric("Triggered Signals", f"{matrix_res.short_triggered_count} Short · {matrix_res.long_triggered_count} Long")

    # Split into Part 1 (Shorts at Resistance) and Part 2 (Longs at Support)
    short_setups = [s for s in matrix_res.setups if s.side == "SHORT"]
    long_setups = [s for s in matrix_res.setups if s.side == "LONG"]

    col_short, col_long = st.columns(2)

    # ── PART 1: UPSIDE / SHORT SETUPS ─────────────────────────────────────
    with col_short:
        st.markdown(
            f"""
            <div style="background-color: rgba(232, 80, 58, 0.12); padding: 8px 12px; border-radius: 6px; border-left: 4px solid {RED}; margin-bottom: 12px;">
                <b style="color: {RED}; font-size: 15px;">PART 1: Upside / Short Setups (Resistance & Reversal)</b>
            </div>
            """,
            unsafe_allow_html=True
        )

        for s in short_setups:
            _render_setup_card(s)

    # ── PART 2: DOWNSIDE / LONG SETUPS ────────────────────────────────────
    with col_long:
        st.markdown(
            f"""
            <div style="background-color: rgba(47, 191, 113, 0.12); padding: 8px 12px; border-radius: 6px; border-left: 4px solid {GREEN}; margin-bottom: 12px;">
                <b style="color: {GREEN}; font-size: 15px;">PART 2: Downside / Long Setups (Support & Reversal)</b>
            </div>
            """,
            unsafe_allow_html=True
        )

        for s in long_setups:
            _render_setup_card(s)


def _render_setup_card(s: srm.ReversalSetup) -> None:
    """Helper to render an individual setup card."""
    status_icon = "🟢" if s.status == "TRIGGERED" else ("🟡" if s.status == "ARMED" else "⚪")
    border_col = GREEN if s.status == "TRIGGERED" else (GOLD if s.status == "ARMED" else "#1b3858")
    bg_col = "#0e2238" if s.status != "INACTIVE" else "#0a1726"

    with st.container():
        st.markdown(
            f"""
            <div style="background-color: {bg_col}; padding: 10px 14px; border-radius: 6px; border-left: 3px solid {border_col}; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-weight: 600; font-size: 13.5px; color: #F2F5F8;">
                        {s.name}
                    </span>
                    <span style="font-size: 11.5px; font-weight: bold; color: {s.badge_color}; background-color: rgba(255,255,255,0.06); padding: 2px 8px; border-radius: 4px;">
                        {status_icon} {s.status}
                    </span>
                </div>
                <div style="margin-top: 5px; font-size: 12px; color: {SLATE};">
                    {s.confluence_detail}
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

        # If Active or Triggered, show execution strip
        if s.status in ("TRIGGERED", "ARMED", "STOPPED"):
            c1, c2, c3, c4 = st.columns(4)
            c1.caption(f"Trigger: **{s.trigger_price}**")
            c2.caption(f"Stop: **{s.stop_loss}**")
            c3.caption(f"Risk: **{s.predefined_risk} pts**")
            c4.caption(f"Target: **{s.target_price}**")

        with st.expander(f"Mentor Rule & Details — {s.id}"):
            st.markdown(f"**Mentor Instruction:** *\"{s.mentor_rule}\"*")
            st.markdown(f"**Execution & Risk:** {s.mentor_note}")
            st.markdown(f"**App Trigger Rule:** {s.execution_guidance}")
            if s.triggered_candle_ts:
                st.markdown(f"⏱️ **Trigger Candle:** `{s.triggered_candle_ts.strftime('%H:%M')}`")
