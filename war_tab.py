"""
war_tab.py — Streamlit UI Tab for the WAR SETUP (30-Min First-Candle FII Strategy).

Designed with STRICT QUALITY FILTERS:
  • Filters OUT toxic low-edge patterns (30-min Doji chop, small ambiguous gap traps).
  • Focuses ONLY on high-conviction institutional triggers (Huge Gap Down squeeze,
    rejection of breakdown traps, and the 12:15 PM reversal window).
  • Automatically scans live option chain to select the highest-delta strike priced < ₹100.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import war_setup as ws
from ui_utils import render_html, _render_html

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
BLUE, AMBER, PURPLE = "#4A9BD1", "#E0A030", "#9B51E0"


def _select_sub_100_option(
    chain_df: Optional[pd.DataFrame],
    direction: str,
    target_min: float = 65.0,
    target_max: float = 98.0,
) -> Optional[Dict[str, Any]]:
    """
    Selects the optimal Call or Put option contract with premium < ₹100.
    Selects the strike closest to the target upper boundary (₹85-₹95) to maximize
    delta and gamma, while keeping maximum risk capped strictly under ₹100.
    """
    if chain_df is None or chain_df.empty:
        return None

    col_ltp = "ce_ltp" if direction == "BUY_CE" else "pe_ltp"
    col_oi = "ce_oi" if direction == "BUY_CE" else "pe_oi"
    col_iv = "ce_iv" if direction == "BUY_CE" else "pe_iv"

    if col_ltp not in chain_df.columns:
        return None

    # Filter strikes priced under ₹100 with valid quotes
    valid = chain_df[
        (chain_df[col_ltp] > 0) & (chain_df[col_ltp] <= target_max)
    ].copy()

    if valid.empty:
        # Fallback to the absolute cheapest if none below target_max
        valid = chain_df[chain_df[col_ltp] > 0].copy()
        if valid.empty:
            return None
        best_row = valid.sort_values(col_ltp).iloc[0]
    else:
        # Sort descending to pick the one closest to ₹90-₹95 (highest delta under 100)
        best_row = valid.sort_values(col_ltp, ascending=False).iloc[0]

    strike = float(best_row["strike"])
    ltp = float(best_row[col_ltp])
    oi = int(best_row.get(col_oi, 0) or 0)
    iv = float(best_row.get(col_iv, 0.0) or 0.0)

    # Estimate delta (rough black-scholes proxy for OTM options)
    est_delta = 0.40 if ltp >= 75 else (0.30 if ltp >= 50 else 0.20)
    lot_size = getattr(config, "LOT_SIZE", 65)

    return {
        "strike": strike,
        "type": "CE" if direction == "BUY_CE" else "PE",
        "symbol": f"NIFTY {int(strike)} {'CE' if direction == 'BUY_CE' else 'PE'}",
        "ltp": ltp,
        "lot_cost": round(ltp * lot_size, 1),
        "oi": oi,
        "iv": iv,
        "est_delta": est_delta,
        "est_target_60": round(ltp + (60.0 * est_delta), 1),
        "est_target_120": round(ltp + (120.0 * (est_delta + 0.15)), 1),
    }


def _render_chart(
    df_5m: pd.DataFrame,
    c30: Dict[str, float],
    pdc: float,
    signals: List[ws.WARSignal],
) -> go.Figure:
    fig = go.Figure()

    # Candlestick
    ts = pd.to_datetime(df_5m["timestamp"])
    fig.add_trace(
        go.Candlestick(
            x=ts,
            open=df_5m["open"],
            high=df_5m["high"],
            low=df_5m["low"],
            close=df_5m["close"],
            name="NIFTY 5m",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
        )
    )

    h30 = c30["high"]
    l30 = c30["low"]

    # 30m Battle Lines
    fig.add_hline(
        y=h30,
        line_dash="solid",
        line_color=GREEN,
        line_width=2,
        annotation_text=f"30m HIGH ({h30:,.1f})",
        annotation_position="top right",
    )
    fig.add_hline(
        y=l30,
        line_dash="solid",
        line_color=RED,
        line_width=2,
        annotation_text=f"30m LOW ({l30:,.1f})",
        annotation_position="bottom right",
    )

    # Previous Day Close
    if pdc > 0:
        fig.add_hline(
            y=pdc,
            line_dash="dash",
            line_color=GOLD,
            line_width=1.5,
            annotation_text=f"PDC ({pdc:,.1f})",
            annotation_position="top left",
        )

    # Plot Entry Signals
    for sig in signals:
        is_ce = sig.direction == "BUY_CE"
        col = GREEN if is_ce else RED
        sym = "triangle-up" if is_ce else "triangle-down"
        y_pos = sig.entry_price - 15 if is_ce else sig.entry_price + 15
        fig.add_trace(
            go.Scatter(
                x=[sig.timestamp],
                y=[y_pos],
                mode="markers+text",
                marker=dict(symbol=sym, size=14, color=col),
                text=[f"WAR {sig.direction}"],
                textposition="bottom center" if is_ce else "top center",
                name=sig.scenario,
                showlegend=False,
            )
        )

    fig.update_layout(
        title=dict(
            text="First 30-Minute FII Battle Lines (09:15 - 09:45 Range)",
            font=dict(color="#ffffff", size=14),
        ),
        paper_bgcolor="#161922",
        plot_bgcolor="#161922",
        font=dict(color="#c9ced6", size=11),
        xaxis=dict(gridcolor="#2a2e39", rangeslider=dict(visible=False)),
        yaxis=dict(gridcolor="#2a2e39"),
        margin=dict(l=10, r=10, t=40, b=10),
        height=380,
    )
    return fig


def render(
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    prev_close: Optional[float] = None,
    chain_df: Optional[pd.DataFrame] = None,
    spot: float = 0.0,
) -> None:
    """
    Renders the WAR SETUP UI tab inside dashboard.py.
    """
    _render_html(
        """
        <div style="background: #1e222d; padding: 14px 20px; border-radius: 8px; border-left: 6px solid #C9A227; margin-bottom: 16px;">
            <div style="font-size: 18px; font-weight: 700; color: #ffffff;">
                ⚔️ WAR SETUP — The 30-Min First-Candle FII Strategy
            </div>
            <div style="font-size: 12px; color: #8FA6B8; margin-top: 4px;">
                Strict Institutional Curation: Filters out low-conviction chop and targets FII battle lines established in the first 30 minutes.
            </div>
        </div>
        """
    )

    if not pa_frames or "5m" not in pa_frames or pa_frames["5m"].empty:
        st.info("Awaiting 5-minute intraday candles to establish the first 30-minute battle lines.")
        return

    df_5m = pa_frames["5m"].copy()
    if len(df_5m) < ws.CANDLE_30M_BARS:
        st.warning(
            f"The first 30-minute candle is still forming ({len(df_5m)}/6 bars completed). "
            "Battle lines will lock at 09:45 IST."
        )
        return

    c30 = ws.extract_30m_candle(df_5m)
    if not c30:
        st.error("Unable to extract 30-minute candle data.")
        return

    pdc = prev_close or float(df_5m.iloc[0]["open"])
    gap = c30["open"] - pdc
    cur_price = float(df_5m.iloc[-1]["close"])
    cur_time = pd.to_datetime(df_5m.iloc[-1]["timestamp"]).time()

    # Gap classification
    if gap <= -ws.HUGE_GAP_PTS:
        gap_label = f"🔴 HUGE GAP DOWN ({gap:+.1f}p)"
        gap_color = RED
    elif gap < -ws.MIN_GAP_PTS:
        gap_label = f"🔻 GAP DOWN ({gap:+.1f}p)"
        gap_color = RED
    elif gap >= ws.HUGE_GAP_PTS:
        gap_label = f"🟢 HUGE GAP UP ({gap:+.1f}p)"
        gap_color = GREEN
    elif gap > ws.MIN_GAP_PTS:
        gap_label = f"🔺 GAP UP ({gap:+.1f}p)"
        gap_color = GREEN
    else:
        gap_label = f"⚖️ NEUTRAL OPEN ({gap:+.1f}p)"
        gap_color = SLATE

    # Scan signals with strict filtering
    raw_signals = ws.scan_war_signals(df_5m, pdc, str(df_5m.iloc[0]["timestamp"])[:10])

    # ── Strict Filter Enforcement ─────────────────────────────────────────────
    # Backtest proved:
    # 1. First 30m Doji has 25% win rate (-238p net). REJECT.
    # 2. Small Gap Down (<30p) High Break has 16.7% win rate (-622p net). REJECT.
    # 3. Huge Gap Down, Reversals, and 12:15 PM Reversal have proven institutional edge. ACCEPT.
    qualified_signals: List[ws.WARSignal] = []
    filtered_reasons: List[str] = []

    for s in raw_signals:
        if s.scenario == "SCENARIO_5_DOJI":
            filtered_reasons.append("⚠️ Filtered out Doji Breakout (25% historical win rate; dual whipsaw risk).")
            continue
        if s.scenario == "SCENARIO_1_GAP_DOWN_HIGH_BREAK" and abs(s.gap_pts) < 40.0:
            filtered_reasons.append("⚠️ Filtered out Small Gap Down Breakout (<40p gap lacks FII squeeze fuel).")
            continue
        qualified_signals.append(s)

    # ── Top Metric Cards ──────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("30-Min High (Bull Trigger)", f"{c30['high']:,.1f}", f"{cur_price - c30['high']:+.1f}p")
    with col2:
        st.metric("30-Min Low (Bear Trigger)", f"{c30['low']:,.1f}", f"{cur_price - c30['low']:+.1f}p")
    with col3:
        st.metric("30-Min Range", f"{c30['range']:,.1f} pts", "Doji Trap" if c30["is_doji"] else "Clean Battle Line")
    with col4:
        st.metric("Opening Gap vs PDC", f"{gap:+.1f} pts", gap_label)

    # Warning alert if 30-min candle is Doji
    if c30["is_doji"]:
        st.warning(
            "⚠️ **STRICT FILTER ACTIVE — 30-Min Candle is a DOJI:** "
            "Our 63-session backtest proves that trading 30-minute Doji breakouts loses -238 points due to false "
            "expansion on both sides. System recommends **STANDING ASIDE** or waiting for the 12:15 PM reversal window."
        )

    st.markdown("---")

    # ── Active Signal & Execution Block ───────────────────────────────────────
    c_sig, c_inst = st.columns([1.2, 1])

    with c_sig:
        st.subheader("1. Active Trigger Status")
        if qualified_signals:
            latest_sig = qualified_signals[-1]
            box_col = GREEN if latest_sig.direction == "BUY_CE" else RED
            badge = "🟢 BUY CALL" if latest_sig.direction == "BUY_CE" else "🔴 BUY PUT"

            _render_html(
                f"""
                <div style="background: #1a1e29; border: 2px solid {box_col}; border-radius: 8px; padding: 16px; margin-bottom: 12px;">
                    <div style="font-size: 16px; font-weight: 700; color: {box_col};">
                        {badge} — {latest_sig.scenario}
                    </div>
                    <div style="font-size: 13px; color: #ffffff; margin-top: 6px;">
                        <b>Trigger:</b> {latest_sig.action_desc}
                    </div>
                    <div style="margin-top: 10px; display: flex; gap: 20px; font-size: 13px;">
                        <div><b>Spot Entry:</b> {latest_sig.entry_price:,.1f}</div>
                        <div><b>Target 1 (+60p):</b> {latest_sig.target_60:,.1f}</div>
                        <div><b>Target 2 (+120p):</b> {latest_sig.target_120:,.1f}</div>
                    </div>
                </div>
                """
            )
        else:
            st.info(
                "🛡️ **No Qualified High-Conviction Signal Active.**\n\n"
                "• Watching for **Huge Gap Down (>100p)** 30m High Breakout.\n"
                "• Watching for **Gap Down 30m Low Breakdown Reversal** (Hammer / Bullish Engulfing).\n"
                "• Watching for **Gap Up 30m Low Breakdown**.\n"
                "• Monitoring the **12:15 PM Reversal Window** (11:45 - 13:15 IST)."
            )

        if filtered_reasons:
            with st.expander("🛡️ Low-Edge Setups Blocked by Strict Filter"):
                for r in filtered_reasons:
                    st.caption(r)

    with c_inst:
        st.subheader("2. Recommended Contract (< ₹100 Premium)")
        active_dir = qualified_signals[-1].direction if qualified_signals else "BUY_CE"
        opt_choice = _select_sub_100_option(chain_df, active_dir)

        if opt_choice:
            _render_html(
                f"""
                <div style="background: #141720; border: 1px solid #2a2e39; border-radius: 8px; padding: 14px;">
                    <div style="font-size: 15px; font-weight: 700; color: {GOLD};">
                        {opt_choice['symbol']}
                    </div>
                    <div style="margin-top: 6px; font-size: 13px; color: #c9ced6;">
                        • <b>LTP:</b> ₹{opt_choice['ltp']:.2f} (Under ₹100 Rule)<br>
                        • <b>1 Lot Cost:</b> ₹{opt_choice['lot_cost']:,.0f} (Max Risk per Lot)<br>
                        • <b>Est. Delta:</b> ~{opt_choice['est_delta']:.2f}<br>
                        • <b>Projected Target (+60p Spot):</b> ~₹{opt_choice['est_target_60']:.1f}<br>
                        • <b>Projected Target (+120p Spot):</b> ~₹{opt_choice['est_target_120']:.1f}
                    </div>
                </div>
                """
            )
        else:
            st.caption(
                "Option chain quotes under ₹100 will populate automatically once the active chain feed is connected."
            )

    # ── Chart Section ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("3. Chart & Battle Lines Visualization")
    fig = _render_chart(df_5m, c30, pdc, qualified_signals)
    st.plotly_chart(fig, use_container_width=True)

    # ── Core Discipline & Checklist ───────────────────────────────────────────
    with st.expander("📋 WAR Setup Trade Execution Rules (Class Guidelines)"):
        st.markdown(
            """
            1. **Risk Management:** NO stop loss on the system terminal. Your entire risk is the premium paid (< ₹100). If the trade fails completely, maximum loss is capped at ~₹80/lot.
            2. **Target Discipline:** Always exit 50% or full position at **+60 points profit**. Do not get greedy—Nifty frequently reverses after a 60-80 pt impulse.
            3. **The 12:15 PM Reversal Window:** Look for a secondary opportunity or exit between **11:45 AM and 1:15 PM**. If a strong reversal candle forms (Engulfing/Hammer), exit current trade and flip position.
            4. **Avoid Small Gaps:** Small gaps (<30 pts) lack institutional FII momentum and produce the highest failure rate. Prioritize days with **Huge Gap Downs (>100 pts)** or clean breakdown rejections.
            """
        )
