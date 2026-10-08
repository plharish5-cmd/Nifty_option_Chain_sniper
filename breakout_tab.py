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

    # 1b. VWAP fix — the incoming 'vwap' column was not reset per session (showed 23,170 with
    #     price at 22,420). Recompute a session-anchored VWAP; drop it when there is no real volume.
    try:
        import smc_trade_desk as _desk
        df_raw = df_raw.copy()
        if "timestamp" not in df_raw.columns and isinstance(df_raw.index, pd.DatetimeIndex):
            df_raw = df_raw.reset_index().rename(columns={df_raw.index.name or "index": "timestamp"})
        _vw = _desk.session_vwap(df_raw)
        if _vw is not None:
            df_raw["vwap"] = _vw.values
        elif "vwap" in df_raw.columns:
            df_raw = df_raw.drop(columns=["vwap"])
    except Exception as _vw_err:                           # noqa: BLE001
        st.caption(f"VWAP recompute skipped: {_vw_err}")

    # 1c. SMC TRADE DESK — one verdict (BUY / SELL / WAIT / AVOID) with entry, SL, targets
    try:
        import smc_trade_desk as _desk
        _desk.render(df_raw, (pa_frames or {}).get("15m"), tf, frames=pa_frames)
    except Exception as _desk_err:                         # noqa: BLE001
        st.caption(f"SMC Trade Desk unavailable: {_desk_err}")

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
        # FAKEOUTFIX — rules live in fakeout_engine.py: closed candles only, FVG ≥ 3 pts / 0.1×ATR, sweep cancelled
        # by a close above the swept high, entry only after the MSS, stop above the true high, every entry logged.
        import fakeout_engine as _fx
        _closed = _fx.closed_only(df_raw)
        _fxs = _fx.find_setup(_closed)
        active_setup = {k: v for k, v in _fxs.items()
                        if k in ("sweep_bar_idx", "sweep_price", "mss_level", "mss_bar_idx", "fvg_top",
                                 "fvg_bottom", "fvg_start_idx", "entry_bar_idx", "entry_price")}
        all_sweeps = [_fxs] if "sweep_bar_idx" in _fxs else []
        try:
            _fx.log_signal(_fxs, tf, now=now)
        except Exception as _log_err:                      # noqa: BLE001
            _fx.LAST_ERROR = f"signal log not saved: {_log_err}"
        _status = _fxs.get("status", "")
        # TP line only when there is a trade, and only the swing low KNOWN at the entry candle
        equal_lows = float(_fxs.get("ssl_target") or 0.0) if _status == "ENTRY" else 0.0

        is_forming_last = bool(not df_raw.iloc[-1].get("closed", True)) if "closed" in df_raw.columns else False
        h_last = float(df_raw["high"].iloc[-1])
        is_mid_testing = (h_last >= bsl_level) and is_forming_last
        entry_done = _status == "ENTRY"
        _sw_ok = "sweep_bar_idx" in _fxs
        _mss_ok = "mss_level" in _fxs
        _fvg_ok = "fvg_top" in _fxs
        kanban_state = {
            "bsl_swept": _sw_ok,
            "bsl_waiting_close": is_mid_testing,
            "bsl_detail": (f"{_fxs['sweep_time']} wick {_fxs['sweep_price']:,.1f} over {_fxs['sweep_level']:,.1f}" if _sw_ok
                           else ("Last sweep cancelled — closed back above" if _fxs.get("cancelled") else f"BSL {bsl_level:,.1f}")),
            "rejection_closed": _sw_ok,
            "rejection_detail": "Closed back below, no close above since" if _sw_ok else "Waiting for a CLOSED rejection",
            "mss_shifted": _mss_ok,
            "mss_detail": f"{_fxs['mss_time']} closed below {_fxs['mss_level']:,.1f}" if _mss_ok else "Needs a CLOSE below a swing low",
            "fvg_formed": _fvg_ok,
            "fvg_detail": (f"{_fxs['fvg_bottom']:,.1f}–{_fxs['fvg_top']:,.1f} ({_fxs['fvg_top'] - _fxs['fvg_bottom']:.1f} pts)" if _fvg_ok
                           else f"Needs a gap ≥ {_fx.MIN_FVG_PTS:g} pts / {_fx.MIN_FVG_ATR:g}×ATR"),
            "entry_active": entry_done,
            "entry_detail": (f"{_fxs['entry_time']} close {_fxs['entry_price']:,.1f}" if entry_done
                             else "After the MSS: a CLOSED candle inside the FVG"),
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

        # Execution card — FAKEOUTFIX
        if entry_done:
            _res = _fxs.get("result", "OPEN")
            _rr = float(_fxs.get("r") or 0.0)
            _res_txt = {"OPEN": "running", "T1": f"T1 hit at {_fxs.get('result_time', '')} (+1R)",
                        "SL": f"stopped at {_fxs.get('result_time', '')} (−1R)",
                        "EOD": f"no target or stop by the close ({_rr:+.2f}R)"}.get(_res, _res)
            (st.success if _res in ("OPEN", "T1") else st.warning)(
                f"🎯 **SHORT / BUY PE — {_fxs['entry_time']} candle CLOSED inside the FVG**  \n"
                f"• **Entry:** `{_fxs['entry_price']:,.1f}` | **Stop:** `{_fxs['sl']:,.1f}` (highest high since the sweep "
                f"+ {_fx.SL_BUFFER:g} · risk `{_fxs['risk']:,.1f}` pts)  \n"
                f"• **T1 (1R):** `{_fxs['t1']:,.1f}` | **T2 (2R):** `{_fxs['t2']:,.1f}`"
                + (f" | SSL known at entry: `{_fxs['ssl_target']:,.1f}`" if _fxs.get("ssl_target") else "")
                + f"  \n• **Status:** {_res_txt}" + (" · T2 reached" if _fxs.get("t2_hit") else "")
            )
        elif is_mid_testing:
            st.warning(
                f"⚠️ **MID-CANDLE ALERT:** Price is actively probing BSL ({bsl_level:,.1f}). **DO NOT ENTER.** "
                f"Wait for the {tf} candle to close — nothing on this tab is painted from a forming candle."
            )
        else:
            _why = {"NO_SWEEP": "no sweep above a swing high yet", "CANCELLED": "the last sweep was cancelled",
                    "WAIT_MSS": "sweep standing, waiting for the MSS close", "WAIT_FVG": "MSS done, no qualifying FVG",
                    "WAIT_FVG_TAP": "waiting for a closed candle back inside the FVG"}.get(_status, _status)
            st.info(f"💡 **No short setup** — {_why}.")

        with st.expander("🧾 Rule check — why the tab says what it says", expanded=not entry_done):
            for _n in _fxs.get("notes", []):
                st.markdown(f"- {_n}")
            st.caption(f"Rules: closed candles only · FVG ≥ {_fx.MIN_FVG_PTS:g} pts or {_fx.MIN_FVG_ATR:g}×ATR · "
                       f"sweep cancelled by a close above the swept high · entry only after the MSS candle · "
                       f"stop = highest high since the sweep + {_fx.SL_BUFFER:g} · T1 1R, T2 2R.")

        with st.expander("📒 Track record — every entry, logged when the app first showed it", expanded=False):
            _tr = _fx.track_record(live_only=True)
            if _tr["n"] == 0:
                st.info("No live signals logged yet. Entries first seen after the market closes are kept as "
                        "'backfill' and not counted — only what you could actually have traded counts.")
            else:
                _m = st.columns(5)
                _m[0].metric("Live signals", _tr["n"])
                _m[1].metric("T1 hit", f"{_tr['t1']}/{_tr['done']}" if _tr["done"] else "—")
                _m[2].metric("Stopped", f"{_tr['sl']}/{_tr['done']}" if _tr["done"] else "—")
                _m[3].metric("T2 reached", f"{_tr['t2']}/{_tr['done']}" if _tr["done"] else "—")
                _m[4].metric("Avg R", f"{_tr['avg_r']:+.2f}" if _tr["done"] else "—")
                _cols = ["date", "tf", "entry_time", "entry", "sl", "t1", "t2", "first_seen", "lag_min", "result", "result_time", "r"]
                st.dataframe(_tr["rows"][_cols].tail(20), use_container_width=True, hide_index=True)
                if _tr["done"] < 20:
                    st.caption(f"Only {_tr['done']} finished live signal(s) — fewer than 20 is too few to call it an edge.")
            if _tr.get("backfill"):
                st.caption(f"{_tr['backfill']} backfill signal(s) (first seen after the candle was long closed) not counted.")
            if getattr(_fx, "LAST_ERROR", ""):
                st.caption(_fx.LAST_ERROR)

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
        # FAKEOUTFIX — the forming candle never carries a signal; it is judged once it closes
        if "closed" in df_raw.columns and len(df_signals) and not bool(df_raw["closed"].iloc[-1]):
            _last = df_signals.index[-1]
            for _col in ("breakout_signal", "trap_signal", "actionable_retest_entry", "primary_signal"):
                if _col in df_signals.columns:
                    df_signals.loc[_last, _col] = ""
            for _col in ("entry_price", "stop_loss", "target_1", "target_2", "risk_pts", "rr_ratio"):
                if _col in df_signals.columns:
                    df_signals.loc[_last, _col] = np.nan
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
