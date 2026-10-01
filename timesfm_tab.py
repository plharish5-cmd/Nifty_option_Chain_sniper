"""
timesfm_tab.py — AI Forecast & Trade Signals (5m & 15m)

Converts Google TimesFM / Statistical Volatility Cone quantile projections
into clear, actionable Buy/Sell tables with exact Entry, Stop Loss (SL),
Target 1 (Median), Target 2 (Cone Bound), and Risk:Reward ratios.
"""
from __future__ import annotations

import datetime
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import timesfm_nifty as tfn
from ui_utils import render_html, _render_html

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
CYAN, NAVY2 = "#00E5FF", "#0B2035"


@st.cache_resource(show_spinner="Loading Google TimesFM 3.0 Neural Weights...")
def _get_timesfm3_model():
    """Load and cache Google TimesFM 3.0 model weights in memory."""
    try:
        from timesfm3 import TimesFM3Evaluator, ModelConfig
        config = ModelConfig(
            checkpoint_path="google/timesfm-3.0-pytorch",
            per_core_batch_size=1,
            device="cpu"
        )
        return TimesFM3Evaluator(config)
    except Exception:
        return None


def _compute_forecast(
    close_series: pd.Series, 
    horizon: int = 25, 
    timeframe: str = "15m"
) -> dict:
    """Compute quantile forecast (median, q10 support, q90 resistance)."""
    last_price = float(close_series.iloc[-1])
    last_ts = close_series.index[-1]
    context = close_series.values.astype(np.float32)[-1000:]
    tf = timeframe.lower()

    if tf in ("1d", "d", "daily"):
        future_dates = tfn.generate_future_nse_daily_timestamps(pd.Timestamp(last_ts), horizon)
        is_daily = True
    elif tf in ("5m", "5min"):
        future_dates = tfn.generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 5, horizon)
        is_daily = False
    elif tf in ("15m", "15min"):
        future_dates = tfn.generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 15, horizon)
        is_daily = False
    else:
        future_dates = tfn.generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 1, horizon)
        is_daily = False

    # Check if neural TimesFM 3.0 is available
    has_timesfm = False
    model = _get_timesfm3_model()
    if model is not None:
        try:
            out = list(model.predict_batch(
                contexts=[context[-512:]], 
                horizon=horizon, 
                return_quantiles=True, 
                use_symmetric_averaging=False
            ))[0]
            median = out.quantiles[:, 4]
            q10 = out.quantiles[:, 0]
            q90 = out.quantiles[:, 8]
            engine_name = "TimesFM 3.0 (Google Research Foundation Model)"
            has_timesfm = True
        except Exception:
            has_timesfm = False

    if not has_timesfm:
        engine_name = "Statistical Volatility Cone (Log-Diffusion Fallback)"
        returns = np.diff(np.log(context[-100:]))
        mu, sigma = float(np.mean(returns)), float(np.std(returns))
        steps = np.arange(1, horizon + 1)
        drift = steps * mu
        diffusion = sigma * np.sqrt(steps)
        median = last_price * np.exp(drift)
        q10 = last_price * np.exp(drift - 1.28 * diffusion)
        q90 = last_price * np.exp(drift + 1.28 * diffusion)

    return {
        "engine": engine_name,
        "has_neural": has_timesfm,
        "last_price": last_price,
        "last_ts": last_ts,
        "future_dates": future_dates,
        "median": median,
        "q10": q10,
        "q90": q90,
        "is_daily": is_daily,
    }


def _derive_trade_plan(
    fc: dict, 
    timeframe: str
) -> dict:
    """
    Translate forecast cone into concrete BUY / SELL / WAIT trade plan.
    """
    last_p = fc["last_price"]
    med_end = float(fc["median"][-1])
    q10_end = float(fc["q10"][-1])
    q90_end = float(fc["q90"][-1])
    
    # Early horizon (T+3 bars)
    med_t3 = float(fc["median"][min(2, len(fc["median"])-1)])
    q10_t3 = float(fc["q10"][min(2, len(fc["q10"])-1)])
    q90_t3 = float(fc["q90"][min(2, len(fc["q90"])-1)])

    pts_drift = med_end - last_p
    pct_drift = (pts_drift / last_p) * 100.0
    cone_width = q90_end - q10_end

    # Timeframe-dependent drift thresholds
    tf = timeframe.lower()
    thresh_pts = 6.0 if "5m" in tf else (12.0 if "15m" in tf else 35.0)

    if pts_drift > thresh_pts:
        action = "BUY (CE)"
        color = GREEN
        tag = "BULLISH EXPANSION"
        entry = last_p
        # Stop loss: conservative floor based on q10 cone
        max_sl_pts = 20.0 if "5m" in tf else (35.0 if "15m" in tf else 90.0)
        sl = max(last_p - max_sl_pts, q10_t3 - 4.0)
        target1 = med_end
        target2 = q90_end
        risk = max(1.0, entry - sl)
        reward1 = max(0.0, target1 - entry)
        reward2 = max(0.0, target2 - entry)
        rr1 = reward1 / risk
        rr2 = reward2 / risk
        advice = f"Strong upward drift ({pct_drift:+.2f}%). Enter on 1-min pullback near {entry:,.1f}. Lock Cost-to-Cost (CTC) when +20 pts favorable excursion is reached."
    elif pts_drift < -thresh_pts:
        action = "SELL (PE)"
        color = RED
        tag = "BEARISH EXPANSION"
        entry = last_p
        max_sl_pts = 20.0 if "5m" in tf else (35.0 if "15m" in tf else 90.0)
        sl = min(last_p + max_sl_pts, q90_t3 + 4.0)
        target1 = med_end
        target2 = q10_end
        risk = max(1.0, sl - entry)
        reward1 = max(0.0, entry - target1)
        reward2 = max(0.0, entry - target2)
        rr1 = reward1 / risk
        rr2 = reward2 / risk
        advice = f"Downside momentum forecast ({pct_drift:+.2f}%). Enter on bounce rejection near {entry:,.1f}. Trail stop to breakeven once Target 1 is touched."
    else:
        action = "WAIT / RANGE CHOP"
        color = GOLD
        tag = "NEUTRAL COMPRESSION"
        entry = last_p
        sl = q10_end - 5.0
        target1 = q90_end
        target2 = q90_end + 15.0
        risk = max(1.0, abs(last_p - sl))
        reward1 = max(0.0, abs(target1 - last_p))
        reward2 = max(0.0, abs(target2 - last_p))
        rr1 = reward1 / risk
        rr2 = reward2 / risk
        advice = f"Neutral drift ({pct_drift:+.2f}%). Volatility cone is compressing ({cone_width:.1f} pts wide). Do not buy breakout in the middle; only scalp extreme bounds ({q10_end:,.0f} support or {q90_end:,.0f} resistance)."

    return {
        "action": action,
        "color": color,
        "tag": tag,
        "entry": entry,
        "sl": sl,
        "target1": target1,
        "target2": target2,
        "risk": risk,
        "reward1": reward1,
        "reward2": reward2,
        "rr1": rr1,
        "rr2": rr2,
        "advice": advice,
        "pts_drift": pts_drift,
        "pct_drift": pct_drift,
        "cone_width": cone_width,
        "med_end": med_end,
        "q10_end": q10_end,
        "q90_end": q90_end,
        "med_t3": med_t3,
        "q10_t3": q10_t3,
        "q90_t3": q90_t3,
    }


def _render_forecast_chart(
    close_series: pd.Series, 
    fc: dict, 
    plan: dict, 
    timeframe: str
) -> go.Figure:
    """Build high-fidelity interactive Plotly chart with probability cone."""
    lookback = min(len(close_series), 65)
    hist_x = [str(x) for x in close_series.index[-lookback:]]
    hist_y = close_series.values[-lookback:]

    fut_x = [str(x) for x in fc["future_dates"]]
    
    # Anchor point
    full_x = [hist_x[-1]] + fut_x
    full_med = [float(hist_y[-1])] + list(fc["median"])
    full_q10 = [float(hist_y[-1])] + list(fc["q10"])
    full_q90 = [float(hist_y[-1])] + list(fc["q90"])

    fig = go.Figure()

    # 1. Historical Line
    fig.add_trace(go.Scatter(
        x=hist_x, y=hist_y,
        mode="lines",
        line=dict(color="#E0E0E0", width=1.8),
        name="Historical Close"
    ))

    # 2. Upper Bound q90 (Resistance Cap)
    fig.add_trace(go.Scatter(
        x=full_x, y=full_q90,
        mode="lines",
        line=dict(color=RED, width=1.5, dash="dash"),
        name="q90 Resistance Cap"
    ))

    # 3. Lower Bound q10 (Support Floor) with fill to q90
    fig.add_trace(go.Scatter(
        x=full_x, y=full_q10,
        mode="lines",
        line=dict(color=GREEN, width=1.5, dash="dash"),
        fill="tonexty",
        fillcolor="rgba(0, 229, 255, 0.15)",
        name="q10 Support Floor (Cone)"
    ))

    # 4. Forecast Median Path
    fig.add_trace(go.Scatter(
        x=full_x, y=full_med,
        mode="lines+markers",
        marker=dict(size=4, color=CYAN),
        line=dict(color=CYAN, width=2.5),
        name="Forecast Median Target"
    ))

    # 5. Visual Levels for Trade Plan (Entry, SL, Target 1, Target 2)
    fig.add_hline(y=plan["entry"], line_dash="dot", line_color="#8FA6B8", 
                  annotation_text=f"Entry: {plan['entry']:,.1f}", annotation_position="top right")
    fig.add_hline(y=plan["sl"], line_dash="dot", line_color=RED, 
                  annotation_text=f"Stop Loss: {plan['sl']:,.1f}", annotation_position="bottom right")
    fig.add_hline(y=plan["target1"], line_dash="dot", line_color=GOLD, 
                  annotation_text=f"Target 1: {plan['target1']:,.1f}", annotation_position="top right")
    fig.add_hline(y=plan["target2"], line_dash="dot", line_color=GREEN, 
                  annotation_text=f"Target 2: {plan['target2']:,.1f}", annotation_position="top right")

    fig.update_layout(
        title=f"NIFTY 50 — {timeframe.upper()} AI Forecast Cone ({fc['engine']})",
        height=520,
        margin=dict(l=10, r=10, t=40, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#0A0B10",
        font=dict(color="#F2F5F8"),
        legend=dict(orientation="h", y=1.05, x=0, xanchor="left", bgcolor="rgba(10,11,16,0.7)"),
        xaxis=dict(gridcolor="#162233", showgrid=True),
        yaxis=dict(gridcolor="#162233", showgrid=True, tickformat=",.1f")
    )
    return fig


def render(pa_frames: dict | None = None, spot: float | None = None) -> None:
    st.subheader("🔮 AI Quantile Forecast & Trade Signal Terminal")
    st.caption(
        "Powered by **TimesFM 3.0 / Statistical Volatility Cones**. "
        "Projects probability cones (q10 floor to q90 cap) and generates precise "
        "**Buy/Sell/SL/Target** trade execution plans directly without requiring TradingView."
    )

    # ── Timeframe Selector ──────────────────────────────────────────────────
    c_tf1, c_tf2, c_tf3 = st.columns([1.5, 1.5, 3])
    with c_tf1:
        sel_tf = st.radio(
            "Timeframe",
            ["15m", "5m", "1d"],
            index=0,
            horizontal=True,
            help="15m: Best for intraday trend & swing. 5m: Best for scalping momentum. 1d: Daily outlook.",
            key="timesfm_tf_selector"
        )
    with c_tf2:
        horizon_bars = st.slider(
            "Forecast Horizon (Bars)",
            min_value=10,
            max_value=35,
            value=25,
            step=5,
            key="timesfm_horizon_slider"
        )
    with c_tf3:
        st.write("")
        st.write("")
        st.caption(f"Selected: **{sel_tf.upper()}** · Planning **{horizon_bars} bars** ahead.")

    # ── Data Loading ────────────────────────────────────────────────────────
    close_series = None
    if pa_frames and sel_tf in pa_frames and not pa_frames[sel_tf].empty and "close" in pa_frames[sel_tf]:
        df_src = pa_frames[sel_tf].copy()
        if "timestamp" in df_src.columns:
            df_src["timestamp"] = pd.to_datetime(df_src["timestamp"])
            close_series = df_src.set_index("timestamp")["close"].dropna()
        else:
            close_series = df_src["close"].dropna()

    if close_series is None or len(close_series) < 20:
        try:
            close_series = tfn.load_nifty_data(sel_tf)
        except Exception as e:
            st.error(f"Could not load candle data for {sel_tf}: {e}")
            return

    # Compute Forecast & Trade Plan
    fc = _compute_forecast(close_series, horizon=horizon_bars, timeframe=sel_tf)
    plan = _derive_trade_plan(fc, timeframe=sel_tf)

    # ── Executive Trade Signal Banner ───────────────────────────────────────
    _render_html(
        f"<div style='background:{NAVY2}; border-left:6px solid {plan['color']}; "
        f"padding:16px 20px; border-radius:0 10px 10px 0; margin-bottom:14px;'>"
        f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
        f"  <div>"
        f"    <span style='font-size:26px; font-weight:800; color:{plan['color']};'>{plan['action']}</span> "
        f"    <span style='font-size:14px; background:#162E4A; color:#8FA6B8; padding:3px 10px; border-radius:12px; margin-left:10px;'>{plan['tag']}</span>"
        f"    <div style='color:#F2F5F8; font-size:14px; margin-top:6px;'>{plan['advice']}</div>"
        f"  </div>"
        f"  <div style='text-align:right;'>"
        f"    <div style='color:{SLATE}; font-size:12px;'>SPOT PRICE</div>"
        f"    <div style='color:#FFFFFF; font-size:24px; font-weight:700;'>{plan['entry']:,.2f}</div>"
        f"    <div style='color:{plan['color']}; font-size:12px;'>Exp Move: {plan['pts_drift']:+.1f} pts ({plan['pct_drift']:+.2f}%)</div>"
        f"  </div>"
        f"</div>"
        f"</div>"
    )

    # ── Key Metrics Cards ───────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Action", plan["action"].split(" ")[0], plan["tag"])
    m2.metric("Trigger / Entry", f"{plan['entry']:,.1f}", "Current Spot")
    m3.metric("Stop Loss (SL)", f"{plan['sl']:,.1f}", f"-{plan['risk']:.1f} pts risk", delta_color="inverse")
    m4.metric("Target 1 (Median)", f"{plan['target1']:,.1f}", f"+{plan['reward1']:.1f} pts (1:{plan['rr1']:.1f} R:R)", delta_color="normal")
    m5.metric("Target 2 (Runner)", f"{plan['target2']:,.1f}", f"+{plan['reward2']:.1f} pts (1:{plan['rr2']:.1f} R:R)", delta_color="normal")

    st.markdown("---")

    # ── 15-Min & 5-Min Trade Plan Table ─────────────────────────────────────
    st.markdown(f"#### 📋 Concrete Trade Execution Matrix ({sel_tf.upper()})")

    # Multi-Horizon Breakdown
    t1_time = str(fc["future_dates"][0])[-8:-3]
    t3_time = str(fc["future_dates"][min(2, len(fc["future_dates"])-1)])[-8:-3]
    tend_time = str(fc["future_dates"][-1])[-8:-3]

    table_data = [
        {
            "Horizon / Stage": f"Immediate Next Candle ({t1_time})",
            "Action": plan["action"],
            "Entry Trigger": f"{plan['entry']:,.1f}",
            "Stop Loss (SL)": f"{plan['sl']:,.1f}",
            "Target (pts)": f"{float(fc['median'][0]):,.1f} ({float(fc['median'][0]) - plan['entry']:+.1f})",
            "q10 Floor (Support)": f"{float(fc['q10'][0]):,.1f}",
            "q90 Cap (Resistance)": f"{float(fc['q90'][0]):,.1f}",
            "R : R": f"1 : {abs(float(fc['median'][0]) - plan['entry']) / max(1, plan['risk']):.1f}",
            "Execution Rule": "Break of previous high/low. Do not anticipate.",
        },
        {
            "Horizon / Stage": f"Early Session Swing ({t3_time})",
            "Action": plan["action"],
            "Entry Trigger": f"{plan['entry']:,.1f}",
            "Stop Loss (SL)": "Move to CTC (Cost-to-Cost)" if plan["action"] != "WAIT / RANGE CHOP" else f"{plan['sl']:,.1f}",
            "Target (pts)": f"{plan['target1']:,.1f} ({plan['reward1']:+.1f})",
            "q10 Floor (Support)": f"{plan['q10_t3']:,.1f}",
            "q90 Cap (Resistance)": f"{plan['q90_t3']:,.1f}",
            "R : R": f"1 : {plan['rr1']:.1f}",
            "Execution Rule": "Book 50-70% quantity at Target 1. Trail remainder.",
        },
        {
            "Horizon / Stage": f"Full Horizon Terminal ({tend_time})",
            "Action": "RUNNER / CAP",
            "Entry Trigger": f"{plan['entry']:,.1f}",
            "Stop Loss (SL)": f"Trail via 8 EMA",
            "Target (pts)": f"{plan['target2']:,.1f} ({plan['reward2']:+.1f})",
            "q10 Floor (Support)": f"{plan['q10_end']:,.1f}",
            "q90 Cap (Resistance)": f"{plan['q90_end']:,.1f}",
            "R : R": f"1 : {plan['rr2']:.1f}",
            "Execution Rule": "Exit full lot at cone boundary or opposite reversal candle.",
        },
    ]

    df_table = pd.DataFrame(table_data)
    st.dataframe(df_table, use_container_width=True, hide_index=True)

    # ── Interactive Plotly Chart ────────────────────────────────────────────
    st.markdown("#### 📈 Interactive Quantile Projection & Trade Levels")
    chart_fig = _render_forecast_chart(close_series, fc, plan, timeframe=sel_tf)
    st.plotly_chart(chart_fig, use_container_width=True)

    # ── Comparison Quick-View: 5m vs 15m Side-by-Side ───────────────────────
    with st.expander("⚡ Compare 5-Min Scalp vs 15-Min Trend Cones Side-by-Side"):
        try:
            fc_5m = _compute_forecast(tfn.load_nifty_data("5m"), horizon=25, timeframe="5m")
            plan_5m = _derive_trade_plan(fc_5m, timeframe="5m")

            fc_15m = _compute_forecast(tfn.load_nifty_data("15m"), horizon=25, timeframe="15m")
            plan_15m = _derive_trade_plan(fc_15m, timeframe="15m")

            comp_rows = [
                {
                    "Timeframe": "⚡ 5-Minute (Scalp)",
                    "Bias": plan_5m["action"],
                    "Entry": f"{plan_5m['entry']:,.1f}",
                    "Stop Loss": f"{plan_5m['sl']:,.1f} (-{plan_5m['risk']:.1f}p)",
                    "Target 1 (Median)": f"{plan_5m['target1']:,.1f} (+{plan_5m['reward1']:.1f}p)",
                    "Target 2 (Extreme)": f"{plan_5m['target2']:,.1f} (+{plan_5m['reward2']:.1f}p)",
                    "R : R": f"1 : {plan_5m['rr1']:.1f}",
                    "Volatility Width": f"{plan_5m['cone_width']:.1f} pts",
                },
                {
                    "Timeframe": "📊 15-Minute (Trend)",
                    "Bias": plan_15m["action"],
                    "Entry": f"{plan_15m['entry']:,.1f}",
                    "Stop Loss": f"{plan_15m['sl']:,.1f} (-{plan_15m['risk']:.1f}p)",
                    "Target 1 (Median)": f"{plan_15m['target1']:,.1f} (+{plan_15m['reward1']:.1f}p)",
                    "Target 2 (Extreme)": f"{plan_15m['target2']:,.1f} (+{plan_15m['reward2']:.1f}p)",
                    "R : R": f"1 : {plan_15m['rr1']:.1f}",
                    "Volatility Width": f"{plan_15m['cone_width']:.1f} pts",
                }
            ]
            st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)
        except Exception as err:
            st.caption(f"Side-by-side comparison unavailable: {err}")

    # ── TradingView Pine Script Arrays (1-Click Copy Expander) ──────────────
    with st.expander("📋 Need Pine Script arrays for TradingView anyway? (Click to view)"):
        all_dates = [fc["last_ts"]] + list(fc["future_dates"])
        all_millis = [str(tfn.to_epoch_millis(d, is_daily=fc["is_daily"])) for d in all_dates]
        all_q50 = [fc["last_price"]] + list(fc["median"])
        all_q10 = [fc["last_price"]] + list(fc["q10"])
        all_q90 = [fc["last_price"]] + list(fc["q90"])

        j = lambda v: ", ".join(f"{x:.2f}" for x in v)
        pinescript_arrays = (
            f"var int[]   f_t   = array.from({', '.join(all_millis)})\n"
            f"var float[] f_q50 = array.from({j(all_q50)})\n"
            f"var float[] f_q10 = array.from({j(all_q10)})\n"
            f"var float[] f_q90 = array.from({j(all_q90)})"
        )
        st.code(pinescript_arrays, language="pinescript")
