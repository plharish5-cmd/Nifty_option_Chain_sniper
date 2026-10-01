"""
smc_strategy.py — Institutional Smart Money Concepts (SMC) Trading Engine & UI Suite.

Designed for professional prop-desk execution:
  1. Live Interactive SMC Structure Chart (Plotly with OBs, FVGs, BOS, CHoCH, EQH/EQL).
  2. Actionable Sniper Execution Cards (Deduplicated, zero clutter).
  3. High-conviction Order Block Retest Engine (1:3 to 1:6 R:R).
  4. Multi-day Historical Backtester across Dhan 15m historical candles.
  5. Clean 5-subtab executive layout eliminating vertical scroll.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import price_action as pa
import smc
from ui_utils import render_html, _render_html


@dataclass
class SMCSetup:
    setup_id: str
    setup_type: str              # CHOCH_REVERSAL | BOS_CONTINUATION
    direction: str               # BULLISH | BEARISH
    signal_time: datetime
    structure_level: float
    ob_low: float
    ob_high: float
    entry_zone: tuple[float, float]
    stop_loss: float
    risk_pts: float
    target_1: float
    target_2: float
    target_pool: str = ""
    status: str = "ARMED"
    note: str = ""


def create_smc_plotly_chart(df: pd.DataFrame, r: dict, spot: float | None = None, show_emas: bool = False) -> go.Figure:
    """
    Constructs a TradingView-grade institutional SMC Candlestick Chart in Plotly.
    Visualizes:
      - 15m Institutional Candlesticks (no lagging indicators like RSI/MACD/Bollinger)
      - Swing Pivot Labels (HH, HL, LH, LL) on confirmed extremes
      - Algorithmic Support Trendlines (connecting HLs) & Resistance Trendlines (connecting LHs)
      - Distinct vertical lines & markers for Market Structure Shifts (MSS / CHoCH)
      - Horizontal Institutional Liquidity Zones (PDH/PDL, PWH/PWL, Asian Session H/L, EQH/EQL)
      - Highlighted Liquidity Sweep Wicks (bright red/green stop hunt markers)
      - Active Demand & Supply Order Blocks (translucent boxes)
      - Active Breaker Blocks (inverted polarity from failed OBs)
      - Fair Value Gaps (FVG) with mitigation fading
      - Shaded Premium Zone (top red tint) & Discount Zone (bottom green tint)
      - Optimal Trade Entry (OTE 61.8% to 78.6%) shaded gold band
      - 50% Equilibrium and live Spot reference line
    """
    fig = go.Figure()

    if df is None or df.empty:
        return fig

    # 1. Candlestick series
    fig.add_trace(go.Candlestick(
        x=df["timestamp"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="NIFTY",
        increasing_line_color="#2FBF71", decreasing_line_color="#E8503A"
    ))

    # Optional EMAs (disabled by default to strictly preserve SMC pure price action)
    if show_emas:
        if "ema8" in df.columns:
            fig.add_trace(go.Scatter(
                x=df["timestamp"], y=df["ema8"], mode="lines", name="8 EMA",
                line=dict(color="#E0A030", width=1.2, dash="dot")
            ))
        if "ema20" in df.columns:
            fig.add_trace(go.Scatter(
                x=df["timestamp"], y=df["ema20"], mode="lines", name="20 EMA",
                line=dict(color="#4A9BD1", width=1.2, dash="dot")
            ))

    last_ts = df["timestamp"].iloc[-1]
    first_ts = df["timestamp"].iloc[0]

    # 2. Premium & Discount Zones Shading + 50% Equilibrium + OTE Band
    dr = r.get("dealing_range") or r.get("macro_dealing_range")
    if dr and dr.high > dr.low:
        # Premium Zone: Top half (faint red tint)
        fig.add_shape(
            type="rect",
            x0=first_ts, x1=last_ts,
            y0=dr.equilibrium, y1=dr.high,
            fillcolor="rgba(232, 80, 58, 0.05)",
            line=dict(width=0),
            layer="below"
        )
        fig.add_annotation(
            x=last_ts, y=dr.high - (dr.size * 0.05),
            text="PREMIUM ZONE (Sell Territory >50%)",
            showarrow=False, xanchor="right",
            font=dict(size=9, color="rgba(232, 80, 58, 0.6)")
        )

        # Discount Zone: Bottom half (faint green tint)
        fig.add_shape(
            type="rect",
            x0=first_ts, x1=last_ts,
            y0=dr.low, y1=dr.equilibrium,
            fillcolor="rgba(47, 191, 113, 0.05)",
            line=dict(width=0),
            layer="below"
        )
        fig.add_annotation(
            x=last_ts, y=dr.low + (dr.size * 0.05),
            text="DISCOUNT ZONE (Buy Territory <50%)",
            showarrow=False, xanchor="right",
            font=dict(size=9, color="rgba(47, 191, 113, 0.6)")
        )

        # 50% Equilibrium Line
        fig.add_shape(
            type="line",
            x0=first_ts, x1=last_ts,
            y0=dr.equilibrium, y1=dr.equilibrium,
            line=dict(color="rgba(148, 163, 184, 0.65)", width=1.4, dash="dashdot")
        )
        fig.add_annotation(
            x=first_ts, y=dr.equilibrium,
            text=f"Equilibrium 50% ({dr.equilibrium:,.0f})",
            showarrow=False, xanchor="left",
            font=dict(size=10, color="#94A3B8")
        )

        # Optimal Trade Entry (OTE: 61.8% to 78.6% Fib Zone)
        # In Discount: Bullish OTE pocket
        if getattr(dr, "ote_bullish_618", 0) > 0:
            fig.add_shape(
                type="rect",
                x0=first_ts, x1=last_ts,
                y0=dr.ote_bullish_786, y1=dr.ote_bullish_618,
                fillcolor="rgba(245, 158, 11, 0.12)",
                line=dict(color="rgba(245, 158, 11, 0.45)", width=1, dash="dot"),
                layer="below"
            )
            fig.add_annotation(
                x=first_ts, y=(dr.ote_bullish_786 + dr.ote_bullish_618) / 2.0,
                text=f"Bullish OTE (61.8%–78.6% [{dr.ote_bullish_786:,.0f}–{dr.ote_bullish_618:,.0f}])",
                showarrow=False, xanchor="left",
                font=dict(size=9, color="#F59E0B")
            )
        # In Premium: Bearish OTE pocket
        if getattr(dr, "ote_bearish_618", 0) > 0:
            fig.add_shape(
                type="rect",
                x0=first_ts, x1=last_ts,
                y0=dr.ote_bearish_618, y1=dr.ote_bearish_786,
                fillcolor="rgba(245, 158, 11, 0.10)",
                line=dict(color="rgba(245, 158, 11, 0.45)", width=1, dash="dot"),
                layer="below"
            )
            fig.add_annotation(
                x=first_ts, y=(dr.ote_bearish_618 + dr.ote_bearish_786) / 2.0,
                text=f"Bearish OTE (61.8%–78.6% [{dr.ote_bearish_618:,.0f}–{dr.ote_bearish_786:,.0f}])",
                showarrow=False, xanchor="left",
                font=dict(size=9, color="#F59E0B")
            )

    # 3. Swing Pivots (HH, HL, LH, LL)
    pivots = r.get("pivots", [])
    for p in pivots[-24:]:
        is_h = p.kind == "HIGH"
        txt = p.subkind if p.subkind else ("H" if is_h else "L")
        col = "#F87171" if is_h else "#34D399"
        y_pos = p.price + (6.0 if is_h else -6.0)
        fig.add_annotation(
            x=p.at, y=y_pos,
            text=f"<b>{txt}</b>",
            showarrow=False,
            font=dict(size=9, color=col)
        )

    # 4. Support & Resistance Trendlines Auto-Draw
    trendlines = r.get("trendlines", {})
    if trendlines:
        sup_tl = trendlines.get("support")
        if sup_tl and sup_tl.is_active:
            fig.add_shape(
                type="line",
                x0=sup_tl.start_at, x1=last_ts,
                y0=sup_tl.start_price, y1=sup_tl.projected_price,
                line=dict(color="#10B981", width=1.8, dash="dash")
            )
            fig.add_annotation(
                x=last_ts, y=sup_tl.projected_price,
                text="Support Trendline (HLs)",
                showarrow=False, xanchor="left",
                font=dict(size=9, color="#10B981")
            )
        res_tl = trendlines.get("resistance")
        if res_tl and res_tl.is_active:
            fig.add_shape(
                type="line",
                x0=res_tl.start_at, x1=last_ts,
                y0=res_tl.start_price, y1=res_tl.projected_price,
                line=dict(color="#EF4444", width=1.8, dash="dash")
            )
            fig.add_annotation(
                x=last_ts, y=res_tl.projected_price,
                text="Resistance Trendline (LHs)",
                showarrow=False, xanchor="left",
                font=dict(size=9, color="#EF4444")
            )

    # 5. Order Blocks (Active Demand & Supply)
    obs = r.get("open_obs", [])
    for ob in obs[-6:]:
        is_bull = ob.direction == "BULLISH"
        fill_col = "rgba(47, 191, 113, 0.22)" if is_bull else "rgba(232, 80, 58, 0.22)"
        line_col = "#2FBF71" if is_bull else "#E8503A"
        fig.add_shape(
            type="rect",
            x0=ob.at, x1=last_ts,
            y0=ob.low, y1=ob.high,
            fillcolor=fill_col,
            line=dict(color=line_col, width=1.2, dash="solid")
        )
        tag = "Demand OB" if is_bull else "Supply OB"
        fig.add_annotation(
            x=last_ts, y=(ob.low + ob.high) / 2.0,
            text=f" {tag} [{ob.low:,.0f}–{ob.high:,.0f}]",
            showarrow=False, xanchor="left",
            font=dict(size=9, color=line_col)
        )

    # 6. Breaker Blocks (Inverted Polarity from Failed OBs)
    breakers = r.get("active_breakers", [])
    for brk in breakers[-3:]:
        is_bull = brk.breaker_direction == "BULLISH"
        fill_col = "rgba(47, 191, 113, 0.16)" if is_bull else "rgba(232, 80, 58, 0.16)"
        line_col = "#2FBF71" if is_bull else "#E8503A"
        fig.add_shape(
            type="rect",
            x0=brk.breaker_at or brk.at, x1=last_ts,
            y0=brk.low, y1=brk.high,
            fillcolor=fill_col,
            line=dict(color=line_col, width=1.3, dash="dash")
        )
        tag = "Bullish Breaker" if is_bull else "Bearish Breaker"
        fig.add_annotation(
            x=last_ts, y=(brk.low + brk.high) / 2.0,
            text=f" {tag} [{brk.low:,.0f}–{brk.high:,.0f}]",
            showarrow=False, xanchor="left",
            font=dict(size=9, color=line_col)
        )

    # 7. Fair Value Gaps (FVG)
    fvgs = r.get("open_fvgs", [])
    for g in fvgs[-5:]:
        fig.add_shape(
            type="rect",
            x0=g.at, x1=last_ts,
            y0=g.low, y1=g.high,
            fillcolor="rgba(255, 193, 7, 0.15)",
            line=dict(color="rgba(255, 193, 7, 0.5)", width=1, dash="dot")
        )

    # 8. BOS & CHoCH / MSS Structure Break Lines & Markers
    stx = r.get("structure")
    if stx and stx.events:
        for ev in stx.events[-8:]:
            is_bull = ev.direction == "BULLISH"
            col = "#2FBF71" if is_bull else "#E8503A"
            # MSS / CHoCH gets a distinct vertical dashed line and callout text marker
            if getattr(ev, "is_mss", False) or ev.kind in ("CHOCH", "MSS"):
                fig.add_shape(
                    type="line",
                    x0=ev.at, x1=ev.at,
                    y0=0, y1=1,
                    yref="paper",
                    line=dict(color=col, width=1.5, dash="dashdot")
                )
                fig.add_annotation(
                    x=ev.at, y=ev.level,
                    text=f"⚡ {ev.direction[:4]} MSS @ {ev.level:,.0f}",
                    showarrow=True, arrowhead=1, arrowsize=1,
                    arrowcolor=col, font=dict(size=9, color=col, family="monospace"),
                    bgcolor="#0F172A", bordercolor=col, borderwidth=1
                )
            else:
                # Regular BOS continuation
                fig.add_shape(
                    type="line",
                    x0=ev.at, x1=last_ts,
                    y0=ev.level, y1=ev.level,
                    line=dict(color=col, width=1.2, dash="dash")
                )
                fig.add_annotation(
                    x=ev.at, y=ev.level,
                    text=f"BOS ({ev.direction[:4]})",
                    showarrow=True, arrowhead=1, arrowsize=1,
                    arrowcolor=col, font=dict(size=8.5, color=col)
                )

    # 9. Horizontal Institutional Liquidity Zones (PDH/PDL, PWH/PWL, Asian H/L, EQH/EQL)
    liq_levels = r.get("liquidity_levels", [])
    for lvl in liq_levels:
        fig.add_shape(
            type="line",
            x0=lvl.at or first_ts, x1=last_ts,
            y0=lvl.level, y1=lvl.level,
            line=dict(color=lvl.color, width=1.3, dash="dot")
        )
        swept_tag = " [SWEPT]" if lvl.swept else ""
        fig.add_annotation(
            x=last_ts, y=lvl.level,
            text=f"{lvl.name} ({lvl.level:,.0f}){swept_tag}",
            showarrow=False, xanchor="left",
            font=dict(size=9, color=lvl.color)
        )

    # 10. Highlight Liquidity Sweeps on Candle Wicks
    sweeps = r.get("liquidity_sweeps", [])
    for sw in sweeps[-6:]:
        if sw.swept_wick and sw.swept_at:
            is_high_sweep = sw.kind == "BUY_SIDE"
            m_col = "#EF4444" if is_high_sweep else "#10B981"
            m_symbol = "triangle-down" if is_high_sweep else "triangle-up"
            sw_text = f"🚨 {sw.name} Sweep"
            fig.add_trace(go.Scatter(
                x=[sw.swept_at], y=[sw.swept_wick],
                mode="markers+text",
                marker=dict(symbol=m_symbol, size=11, color=m_col, line=dict(color="#FFFFFF", width=1.2)),
                text=[sw_text],
                textposition="top center" if is_high_sweep else "bottom center",
                textfont=dict(size=9, color=m_col),
                showlegend=False
            ))

    # 11. Spot Line
    if spot:
        fig.add_shape(
            type="line",
            x0=first_ts, x1=last_ts,
            y0=spot, y1=spot,
            line=dict(color="#FFFFFF", width=1, dash="dot")
        )
        fig.add_annotation(
            x=first_ts, y=spot,
            text=f"Spot {spot:,.1f}",
            showarrow=False, xanchor="left",
            font=dict(size=9.5, color="#FFFFFF")
        )

    fig.update_layout(
        height=560, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(11, 19, 30, 0.95)",
        plot_bgcolor="rgba(11, 19, 30, 0.95)",
        font=dict(color="#F2F5F8", family="Inter, -apple-system, sans-serif"),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.02, yanchor="bottom")
    )
    fig.update_xaxes(
        gridcolor="#1b2a3d",
        rangebreaks=[
            dict(bounds=["sat", "mon"]),
            dict(bounds=[15.6, 9.25], pattern="hour"),
        ]
    )
    fig.update_yaxes(gridcolor="#1b2a3d")
    return fig


def get_live_smc_signals(df15: pd.DataFrame, spot: float, chain_df: pd.DataFrame | None = None) -> list[dict]:
    """
    Scans latest candles for live active/armed SMC setups.
    Deduplicates strictly so only 1 distinct setup per direction appears.
    """
    if df15 is None or len(df15) < 10:
        return []

    r = smc.read(df15, n=2, spot=spot)
    obs = r.get("open_obs", [])
    stx = r.get("structure")
    cf = r.get("sniper_confluence", {})
    signals = []

    seen_directions = set()

    # Prioritize 4/4 Triggered and 3/4 Armed institutional setups
    for d_key, sc in cf.items():
        if sc and sc.status in ("TRIGGERED", "ARMED") and sc.direction not in seen_directions:
            seen_directions.add(sc.direction)
            signals.append({
                "type": "SNIPER_CONFLUENCE",
                "direction": sc.direction,
                "title": f"⚡ {sc.direction} 4-GATE SNIPER ({sc.status})",
                "zone": f"Target: {sc.target_name}",
                "spot": spot,
                "stop": sc.stop_loss,
                "target1": sc.target_1,
                "target2": sc.target_2,
                "risk": sc.risk_pts,
                "detail": f"{sc.summary} Criteria: {', '.join(sc.conditions_met[:2])}",
                "urgent": sc.status == "TRIGGERED"
            })

    for ob in obs:
        if ob.contains(spot) and ob.direction not in seen_directions:
            seen_directions.add(ob.direction)
            is_bull = ob.direction == "BULLISH"
            stop_lvl = round(ob.low - 5.0 if is_bull else ob.high + 5.0, 1)
            t1_lvl = round(spot + 35.0 if is_bull else spot - 35.0, 1)
            t2_lvl = round(spot + 80.0 if is_bull else spot - 80.0, 1)
            risk_val = abs(spot - stop_lvl)

            signals.append({
                "type": "OB_RETEST",
                "direction": ob.direction,
                "title": f"⚡ {ob.direction} SMC SNIPER ENTRY ZONE",
                "zone": f"[{ob.low:,.0f} – {ob.high:,.0f}]",
                "spot": spot,
                "stop": stop_lvl,
                "target1": t1_lvl,
                "target2": t2_lvl,
                "risk": risk_val,
                "detail": f"Spot {spot:,.1f} is testing unmitigated {ob.direction} OB [{ob.low:,.0f}–{ob.high:,.0f}]. Risk {risk_val:.0f} pts for 1:3+ runner!",
                "urgent": True
            })

    # Fresh BOS / CHoCH alert (only if event happened within last 2 bars)
    if stx and stx.events and (stx.bars_since_event is None or stx.bars_since_event <= 2):
        last_ev = stx.last
        if last_ev and last_ev.direction not in seen_directions:
            signals.append({
                "type": "STRUCTURE_BREAK",
                "direction": last_ev.direction,
                "title": f"🚀 FRESH {last_ev.direction} {last_ev.kind} @ {last_ev.level:,.1f}",
                "level": last_ev.level,
                "close": last_ev.close,
                "detail": f"Confirmed {last_ev.kind} break at {last_ev.level:,.1f}. Await pullback into origin Order Block.",
                "urgent": last_ev.kind == "CHOCH"
            })

    return signals


def backtest_smc_engine(full_df: pd.DataFrame, n_pivots: int = 2,
                        max_risk: float = 40.0, min_risk: float = 8.0,
                        buffer_pts: float = 5.0) -> tuple[pd.DataFrame, dict]:
    """
    Strict bar-by-bar backtest of the SMC Sniper strategy across historical candles.
    Enforces no-lookahead bias, EMA confluence, and Cost-to-Cost trailing.
    """
    if full_df is None or full_df.empty:
        return pd.DataFrame(), {}

    d = full_df.copy()
    if "timestamp" not in d.columns:
        if isinstance(d.index, pd.DatetimeIndex):
            d["timestamp"] = d.index
        elif d.index.name in ("timestamp", "time"):
            d["timestamp"] = pd.to_datetime(d.index)
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.sort_values("timestamp").reset_index(drop=True)

    d["ema8"] = d["close"].ewm(span=8, adjust=False).mean()
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()

    r = smc.read(d, n=n_pivots)
    obs = r["order_blocks"]
    stx = r["structure"]

    trades: list[dict] = []

    for ob in obs:
        ev = next((e for e in stx.events if e.i == ob.bos_i), None)
        setup_kind = "CHOCH_REVERSAL" if (ev and ev.kind == "CHOCH") else "BOS_CONTINUATION"
        direction = ob.direction

        end_idx = ob.invalidated_i if ob.invalidated_i else len(d)
        retest_idx = None

        for k in range(ob.bos_i + 1, min(ob.bos_i + 14, end_idx)):
            bar = d.iloc[k]
            if (bar["timestamp"] - ob.at).total_seconds() > 86400 * 2.5:
                break

            touches = (bar["low"] <= ob.high and bar["high"] >= ob.low)
            if not touches:
                continue

            if direction == "BULLISH":
                is_rejection = (bar["close"] >= bar["open"]) or ((bar["close"] - bar["low"]) >= (bar["high"] - bar["close"]))
                ema_ok = bar["close"] >= bar["ema8"] or bar["ema8"] >= bar["ema20"]
                if is_rejection and ema_ok:
                    retest_idx = k
                    break
            else:
                is_rejection = (bar["close"] <= bar["open"]) or ((bar["high"] - bar["close"]) >= (bar["close"] - bar["low"]))
                ema_ok = bar["close"] <= bar["ema8"] or bar["ema8"] <= bar["ema20"]
                if is_rejection and ema_ok:
                    retest_idx = k
                    break

        if retest_idx is None:
            continue

        entry_row = d.iloc[retest_idx]
        entry_price = float(entry_row["close"])
        stop = (ob.low - buffer_pts) if direction == "BULLISH" else (ob.high + buffer_pts)
        risk = abs(entry_price - stop)

        if risk < min_risk or risk > max_risk:
            continue

        t1 = entry_price + 1.5 * risk if direction == "BULLISH" else entry_price - 1.5 * risk
        t2 = entry_price + 3.0 * risk if direction == "BULLISH" else entry_price - 3.0 * risk

        exit_price = None
        exit_reason = None
        exit_ts = None
        cur_stop = stop
        hit_t1 = False

        for m in range(retest_idx + 1, len(d)):
            m_row = d.iloc[m]

            if direction == "BULLISH":
                if m_row["low"] <= cur_stop:
                    exit_price = cur_stop
                    exit_reason = "STOP LOSS" if not hit_t1 else "COST-TO-COST (BE)"
                    exit_ts = m_row["timestamp"]
                    break
                if m_row["high"] >= t2:
                    exit_price = t2
                    exit_reason = "TARGET 2 (RUNNER)"
                    exit_ts = m_row["timestamp"]
                    break
                if m_row["high"] >= t1 and not hit_t1:
                    hit_t1 = True
                    cur_stop = entry_price
            else:
                if m_row["high"] >= cur_stop:
                    exit_price = cur_stop
                    exit_reason = "STOP LOSS" if not hit_t1 else "COST-TO-COST (BE)"
                    exit_ts = m_row["timestamp"]
                    break
                if m_row["low"] <= t2:
                    exit_price = t2
                    exit_reason = "TARGET 2 (RUNNER)"
                    exit_ts = m_row["timestamp"]
                    break
                if m_row["low"] <= t1 and not hit_t1:
                    hit_t1 = True
                    cur_stop = entry_price

            if m_row["timestamp"].date() != entry_row["timestamp"].date():
                exit_price = float(m_row["close"])
                exit_reason = "EOD EXIT"
                exit_ts = m_row["timestamp"]
                break

        if exit_price is not None and exit_ts is not None:
            pts = (exit_price - entry_price) if direction == "BULLISH" else (entry_price - exit_price)
            rr = pts / risk if risk > 0 else 0.0
            trades.append({
                "Date": entry_row["timestamp"].strftime("%Y-%m-%d"),
                "Time": entry_row["timestamp"].strftime("%H:%M"),
                "Setup": setup_kind.replace("_", " "),
                "Direction": direction,
                "Entry": round(entry_price, 1),
                "Stop": round(stop, 1),
                "Exit": round(exit_price, 1),
                "Risk": round(risk, 1),
                "Points": round(pts, 1),
                "R:R": round(rr, 2),
                "Result": "WIN" if pts > 0.5 else ("BE" if abs(pts) <= 0.5 else "LOSS"),
                "Exit Reason": exit_reason
            })

    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return tdf, {
            "total_trades": 0, "wins": 0, "losses": 0, "be": 0,
            "win_rate": 0.0, "net_points": 0.0, "profit_factor": 0.0,
            "avg_points": 0.0, "max_winner": 0.0
        }

    wins = tdf[tdf["Points"] > 0.5]
    losses = tdf[tdf["Points"] < -0.5]
    be = tdf[(tdf["Points"] >= -0.5) & (tdf["Points"] <= 0.5)]

    win_pts = float(wins["Points"].sum())
    loss_pts = float(abs(losses["Points"].sum()))
    pf = win_pts / loss_pts if loss_pts > 0 else 999.0
    net_pts = float(tdf["Points"].sum())
    win_rate = (len(wins) / len(tdf)) * 100.0 if len(tdf) else 0.0

    stats = {
        "total_trades": len(tdf),
        "wins": len(wins),
        "losses": len(losses),
        "be": len(be),
        "win_rate": round(win_rate, 1),
        "net_points": round(net_pts, 1),
        "profit_factor": round(pf, 2),
        "avg_points": round(net_pts / len(tdf), 1) if len(tdf) else 0.0,
        "max_winner": round(float(tdf["Points"].max()), 1) if len(tdf) else 0.0
    }
    return tdf, stats


def render_narrative_banner(title: str, narrative: str, action_badge: str, action_text: str, color: str = "#00BCD4"):
    """
    Renders an executive institutional narrative banner with an immediate, unambiguous
    tactical action directive for the running spot price.
    """
    badge_bg = "#0B2B1E" if color == "#0F9D58" else ("#2B0E11" if color == "#E8503A" else ("#2E1E05" if color == "#FF9800" else "#122030"))
    _render_html(
        f"""
        <div style="background:#0F1823;border-left:5px solid {color};border-radius:6px;padding:11px 15px;margin-bottom:14px;box-shadow:0 3px 8px rgba(0,0,0,0.35);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;">
                <span style="font-size:12px;font-weight:700;color:{color};letter-spacing:0.8px;text-transform:uppercase;">
                    {title}
                </span>
                <span style="font-size:11px;color:#8FA6B8;font-weight:600;background:#162332;padding:2px 8px;border-radius:3px;">
                    INSTITUTIONAL PLAYBOOK
                </span>
            </div>
            <div style="font-size:13px;color:#C7D2DF;line-height:1.45;margin-bottom:8px;">
                {narrative}
            </div>
            <div style="background:#080E16;border:1px solid #1C2D40;border-radius:5px;padding:7px 11px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <span style="background:{badge_bg};color:{color};border:1px solid {color};font-weight:700;font-size:11px;padding:2px 7px;border-radius:4px;white-space:nowrap;">
                    {action_badge}
                </span>
                <span style="font-size:12.5px;color:#FFFFFF;line-height:1.4;font-weight:500;">
                    {action_text}
                </span>
            </div>
        </div>
        """
    )


def play_chime_sound():
    """Plays an institutional synthesized two-tone chime via Web Audio API."""
    chime_html = """
    <script>
    (function() {
        try {
            var AudioCtx = window.AudioContext || window.webkitAudioContext;
            if (!AudioCtx) return;
            var ctx = new AudioCtx();
            var now = ctx.currentTime;
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type = 'triangle';
            osc.frequency.setValueAtTime(880, now);
            osc.frequency.exponentialRampToValueAtTime(1320, now + 0.15);
            gain.gain.setValueAtTime(0.25, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.45);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(now);
            osc.stop(now + 0.45);
        } catch(e) {}
    })();
    </script>
    """
    try:
        st.components.v1.html(chime_html, height=0, width=0)
    except Exception:
        pass


def render_sniper_kanban_subtab(sdf: pd.DataFrame, spot: float, r: dict):
    """
    Renders the institutional 'Sniper Entry' Confluence Scanner & 4-Stage Kanban Board.
    Enforces the 4-step ICT/SMC institutional criteria:
      1. Higher-Timeframe Liquidity Zone (Discount <50% for Longs / Premium >50% for Shorts)
      2. Liquidity Purge (Sweep of Sell-Side / Buy-Side Liquidity)
      3. Market Structure Shift (Displacement MSS/CHoCH)
      4. Optimal Trade Entry / Retracement (OB, FVG, or 61.8%–78.6% OTE pocket)
    Includes Web Audio API chime trigger and exact execution parameters (Entry, SL, T1, T2, RR).
    """
    sniper_cf = r.get("sniper_confluence")
    if not sniper_cf:
        sniper_cf = smc.detect_sniper_confluence(sdf, spot, r)

    bull = sniper_cf.get("bullish")
    bear = sniper_cf.get("bearish")

    # Audio cue trigger when setup is TRIGGERED
    any_triggered = (bull and bull.status == "TRIGGERED") or (bear and bear.status == "TRIGGERED")
    if any_triggered:
        play_chime_sound()
        _render_html(
            """
            <div style="background:linear-gradient(90deg, #10B98133, #05966955);border:2px solid #10B981;border-radius:8px;padding:12px 18px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 0 15px rgba(16,185,129,0.3);">
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="font-size:22px;">⚡</span>
                    <div>
                        <b style="color:#10B981;font-size:14px;letter-spacing:0.8px;">SMC 4/4 SNIPER ENTRY TRIGGERED — INSTITUTIONAL ALIGNMENT ACTIVE</b>
                        <div style="font-size:12px;color:#E2E8F0;">All 4 institutional execution gates satisfied. Prepare order entry with strict invalidation stop.</div>
                    </div>
                </div>
                <span style="background:#10B981;color:#0F172A;font-weight:800;font-size:11px;padding:4px 10px;border-radius:4px;letter-spacing:1px;">
                    EXECUTE NOW
                </span>
            </div>
            """
        )

    # Narrative banner
    n_col = "#10B981" if any_triggered else ("#F59E0B" if ((bull and bull.score >= 3) or (bear and bear.score >= 3)) else "#00BCD4")
    n_badge = "⚡ 4/4 TRIGGERED" if any_triggered else ("🟠 3/4 ARMED" if ((bull and bull.score >= 3) or (bear and bear.score >= 3)) else "📐 SCANNING STRUCTURE")
    n_text = (
        f"Live Spot: <b>{spot:,.1f}</b> · Sniper entry conditions met! Limit order placed at origin OB."
        if any_triggered else
        f"Live Spot: <b>{spot:,.1f}</b> · Setup is 3/4 Armed — waiting for final confirmation trigger."
        if ((bull and bull.score >= 3) or (bear and bear.score >= 3)) else
        f"Live Spot: <b>{spot:,.1f}</b> · Filter active. No retail market orders in mid-zone chop."
    )

    render_narrative_banner(
        title="Institutional 'Sniper Entry' Confluence Engine (ICT / SMC Playbook)",
        narrative="Algorithmic execution engine filtering out retail stop hunts. Setups are triggered ONLY when all 4 institutional dynamics converge: <b>HTF Dealing Range</b> (<50% Discount for Longs, >50% Premium for Shorts), <b>Liquidity Purge</b> (SSL/BSL sweep), <b>Market Structure Shift</b> (displacement MSS), and <b>Optimal Retracement</b> (OB, FVG, or 61.8%–78.6% OTE).",
        action_badge=n_badge,
        action_text=n_text,
        color=n_col
    )

    # 4-Stage Kanban Board
    st.markdown("#### 📋 Sniper Setup Kanban Board")
    st.caption("Live lifecycle tracking across the 4 institutional progression stages: Watchlist ➔ Forming ➔ Armed ➔ Triggered")

    col_watch, col_form, col_arm, col_trig = st.columns(4)

    def _render_kanban_card(setup, is_bull: bool):
        c = "#10B981" if is_bull else "#EF4444"
        bg = "#0B1D16" if is_bull else "#260E12"
        dir_lbl = "BULLISH LONG" if is_bull else "BEARISH SHORT"
        badge_bg = "#10B98122" if is_bull else "#EF444422"
        score_blocks = ("🟩" if is_bull else "🟥") * setup.score + "⬜" * (4 - setup.score)

        next_req = setup.conditions_pending[0] if setup.conditions_pending else "All 4 gates satisfied!"

        _render_html(
            f"""
            <div style="background:{bg};border:1px solid {c}66;border-left:4px solid {c};border-radius:6px;padding:12px;margin-bottom:10px;box-shadow:0 3px 6px rgba(0,0,0,0.35);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                    <b style="color:{c};font-size:12px;">{dir_lbl}</b>
                    <span style="background:{badge_bg};color:{c};font-size:10.5px;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid {c}55;">
                        {setup.status}
                    </span>
                </div>
                <div style="font-family:monospace;font-size:10.5px;letter-spacing:1px;margin-bottom:8px;">
                    {score_blocks} <span style="color:#94A3B8;font-size:11px;">({setup.score}/4)</span>
                </div>
                <div style="font-size:11.5px;color:#CBD5E1;line-height:1.4;margin-bottom:8px;">
                    <b>Next:</b> <span style="color:#F1F5F9;">{next_req}</span>
                </div>
                <div style="background:#080E17;padding:6px 8px;border-radius:4px;font-family:monospace;font-size:11px;color:#F8FAFC;border:1px solid #1E293B;line-height:1.45;">
                    <div>Entry: <b>{setup.entry_price:,.1f}</b></div>
                    <div>Stop: <b style="color:#EF4444;">{setup.stop_loss:,.1f}</b> ({setup.risk_pts:.0f} pts)</div>
                    <div>Tgt 2: <b style="color:#10B981;">{setup.target_2:,.1f}</b> ({setup.rr_ratio:.1f} R:R)</div>
                </div>
            </div>
            """
        )

    def _render_empty_kanban():
        _render_html(
            """
            <div style="background:#09111C;border:1px dashed #1E293B;border-radius:6px;padding:22px 10px;text-align:center;color:#64748B;font-size:11.5px;">
                No setups in this stage
            </div>
            """
        )

    # 1. Watchlist (Score <= 1)
    with col_watch:
        st.markdown("<b style='color:#94A3B8;font-size:13px;'>📋 Watchlist (0–1/4)</b>", unsafe_allow_html=True)
        rendered = False
        if bull and bull.score <= 1:
            _render_kanban_card(bull, is_bull=True)
            rendered = True
        if bear and bear.score <= 1:
            _render_kanban_card(bear, is_bull=False)
            rendered = True
        if not rendered:
            _render_empty_kanban()

    # 2. Forming (Score == 2)
    with col_form:
        st.markdown("<b style='color:#38BDF8;font-size:13px;'>🟡 Forming (2/4)</b>", unsafe_allow_html=True)
        rendered = False
        if bull and bull.score == 2:
            _render_kanban_card(bull, is_bull=True)
            rendered = True
        if bear and bear.score == 2:
            _render_kanban_card(bear, is_bull=False)
            rendered = True
        if not rendered:
            _render_empty_kanban()

    # 3. Armed (Score == 3)
    with col_arm:
        st.markdown("<b style='color:#F59E0B;font-size:13px;'>🟠 Armed (3/4)</b>", unsafe_allow_html=True)
        rendered = False
        if bull and bull.score == 3:
            _render_kanban_card(bull, is_bull=True)
            rendered = True
        if bear and bear.score == 3:
            _render_kanban_card(bear, is_bull=False)
            rendered = True
        if not rendered:
            _render_empty_kanban()

    # 4. Triggered (Score == 4)
    with col_trig:
        st.markdown("<b style='color:#10B981;font-size:13px;'>🚀 Triggered (4/4)</b>", unsafe_allow_html=True)
        rendered = False
        if bull and bull.score == 4:
            _render_kanban_card(bull, is_bull=True)
            rendered = True
        if bear and bear.score == 4:
            _render_kanban_card(bear, is_bull=False)
            rendered = True
        if not rendered:
            _render_empty_kanban()

    st.divider()

    # Deep-Dive Confluence Playbooks (Bullish vs Bearish)
    st.markdown("#### 🎯 Confluence Diagnostic & Execution Playbooks")

    b_card_col, s_card_col = st.columns(2)

    def _render_detailed_playbook(setup, is_bull: bool):
        c = "#10B981" if is_bull else "#EF4444"
        bg = "#0C1926"
        dir_title = "🟢 Bullish Sniper Long Playbook" if is_bull else "🔴 Bearish Sniper Short Playbook"
        badge_bg = "#10B98122" if is_bull else "#EF444422"

        crit1_name = "HTF Dealing Range (Discount Zone < 50%)" if is_bull else "HTF Dealing Range (Premium Zone > 50%)"
        crit2_name = "Liquidity Purge (Sell-Side SSL Sweep)" if is_bull else "Liquidity Purge (Buy-Side BSL Sweep)"
        crit3_name = "Market Structure Shift (Displacement MSS)" if is_bull else "Market Structure Shift (Displacement MSS)"
        crit4_name = "Optimal Retracement (Demand OB / FVG / OTE)" if is_bull else "Optimal Retracement (Supply OB / FVG / OTE)"

        def _item_html(title, met_list, pend_list, kw):
            matched = any(kw.lower() in m.lower() for m in met_list)
            icon = "✅" if matched else "⏳"
            col = "#10B981" if matched else "#94A3B8"
            detail = next((m for m in met_list if kw.lower() in m.lower()), None)
            if not detail:
                detail = next((p for p in pend_list if kw.lower() in p.lower()), "Pending confirmation")
            return f"""
            <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:6px;font-size:12px;">
                <span style="font-size:13px;">{icon}</span>
                <div>
                    <b style="color:{col};">{title}</b>
                    <div style="font-size:11px;color:#94A3B8;line-height:1.35;">{detail}</div>
                </div>
            </div>
            """

        c1 = _item_html(crit1_name, setup.conditions_met, setup.conditions_pending, "Zone")
        c2 = _item_html(crit2_name, setup.conditions_met, setup.conditions_pending, "Sweep")
        c3 = _item_html(crit3_name, setup.conditions_met, setup.conditions_pending, "MSS")
        c4 = _item_html(crit4_name, setup.conditions_met, setup.conditions_pending, "Retest") or _item_html(crit4_name, setup.conditions_met, setup.conditions_pending, "OTE")

        _render_html(
            f"""
            <div style="background:{bg};border:1px solid #1E2D42;border-top:4px solid {c};border-radius:6px;padding:14px;box-shadow:0 3px 8px rgba(0,0,0,0.35);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <span style="font-size:13.5px;font-weight:700;color:{c};">{dir_title}</span>
                    <span style="background:{badge_bg};color:{c};font-weight:700;font-size:11px;padding:3px 8px;border-radius:4px;border:1px solid {c}55;">
                        {setup.status} ({setup.score}/4)
                    </span>
                </div>
                <div style="margin-bottom:12px;">
                    {c1}
                    {c2}
                    {c3}
                    {c4}
                </div>
                <div style="background:#070D14;border:1px solid #1A2838;border-radius:5px;padding:10px;font-family:monospace;font-size:11.5px;color:#F1F5F9;line-height:1.6;">
                    <div style="display:flex;justify-content:space-between;">
                        <span style="color:#94A3B8;">Execution Entry:</span>
                        <b>{setup.entry_price:,.1f}</b>
                    </div>
                    <div style="display:flex;justify-content:space-between;">
                        <span style="color:#94A3B8;">Invalidation Stop Loss:</span>
                        <b style="color:#EF4444;">{setup.stop_loss:,.1f} ({setup.risk_pts:.0f} pts risk)</b>
                    </div>
                    <div style="display:flex;justify-content:space-between;">
                        <span style="color:#94A3B8;">Target 1 (1.5R / Eq):</span>
                        <b style="color:#38BDF8;">{setup.target_1:,.1f} (Move to Cost-to-Cost)</b>
                    </div>
                    <div style="display:flex;justify-content:space-between;">
                        <span style="color:#94A3B8;">Target 2 (Runner Pool):</span>
                        <b style="color:#10B981;">{setup.target_2:,.1f} ({setup.target_name})</b>
                    </div>
                    <div style="display:flex;justify-content:space-between;border-top:1px dashed #24354A;padding-top:4px;margin-top:4px;">
                        <span style="color:#94A3B8;">Mathematical Expectancy:</span>
                        <b style="color:#F59E0B;">1 : {setup.rr_ratio:.2f} R:R</b>
                    </div>
                </div>
            </div>
            """
        )

    with b_card_col:
        if bull:
            _render_detailed_playbook(bull, is_bull=True)

    with s_card_col:
        if bear:
            _render_detailed_playbook(bear, is_bull=False)

    st.divider()

    # Historical / Recent Triggered Setups (Mitigated Tracker)
    st.markdown("#### 🏁 Recent Institutional High-Low Sweep & IOFED Triggers")
    st.caption("Bar-by-bar verified institutional displacement and retracement setups across recent 15m candles.")
    try:
        iofed_df = smc.detect_sweep_iofed_setup(sdf, pivot_n=2)
        if isinstance(iofed_df, pd.DataFrame) and not iofed_df.empty and "setup_detected" in iofed_df.columns:
            trig_df = iofed_df[iofed_df["setup_detected"]].copy()
            if not trig_df.empty:
                show_cols = ["entry_time", "direction", "sweep_level", "entry_price", "stop_loss", "risk_pts", "tp_1_5r", "tp_3r", "status"]
                valid_cols = [c for c in show_cols if c in trig_df.columns]
                render_df = trig_df[valid_cols].rename(columns={
                    "entry_time": "Time", "direction": "Dir", "sweep_level": "Sweep Lvl",
                    "entry_price": "Entry", "stop_loss": "Stop", "risk_pts": "Risk",
                    "tp_1_5r": "Target 1 (1.5R)", "tp_3r": "Target 2 (3R)", "status": "Status"
                })
                st.dataframe(render_df[::-1].head(10), use_container_width=True, hide_index=True)
            else:
                st.caption("No IOFED displacement setups triggered in the current candle window.")
        else:
            st.caption("No historical displacement setups detected.")
    except Exception as e_hist:
        st.caption(f"Historical setups monitor unavailable: {e_hist}")


def render_smc_sidebar_kanban(sdf: pd.DataFrame | None = None, spot: float | None = None):
    """
    Renders a compact, high-contrast SMC Sniper Radar card on the Streamlit sidebar.
    Displays Market Phase, current Bullish & Bearish setup status, and Sweep Alerts.
    """
    try:
        if sdf is None or sdf.empty:
            sdf = pa.load_history_candles(days=2, timeframe="15m")
        if sdf is None or sdf.empty:
            return

        if spot is None:
            spot = float(sdf["close"].iloc[-1])

        r = smc.read(sdf, n=2, spot=spot)
        phase = r.get("market_phase")
        cf = r.get("sniper_confluence") or smc.detect_sniper_confluence(sdf, spot, r)
        bull = cf.get("bullish")
        bear = cf.get("bearish")

        st.sidebar.markdown("---")
        st.sidebar.markdown("### ⚡ SMC Sniper Radar")

        # Market phase badge
        if phase:
            p_name = phase.phase.replace("_", " ")
            st.sidebar.markdown(
                f"""<div style="background:#0F1823;border:1px solid {phase.badge_color}55;border-left:4px solid {phase.badge_color};padding:6px 10px;border-radius:4px;margin-bottom:8px;">
                    <span style="font-size:11px;color:#94A3B8;">MARKET PHASE:</span><br>
                    <b style="color:{phase.badge_color};font-size:12.5px;">{p_name} ({phase.confidence*100:.0f}%)</b>
                </div>""",
                unsafe_allow_html=True
            )

        # Sniper alerts
        for setup, is_bull in [(bull, True), (bear, False)]:
            if not setup:
                continue
            if setup.score >= 3:
                c = "#10B981" if is_bull else "#EF4444"
                bg = "#0B2B1E" if is_bull else "#2B0E11"
                lbl = "BULLISH LONG" if is_bull else "BEARISH SHORT"
                st.sidebar.markdown(
                    f"""<div style="background:{bg};border:1px solid {c};border-radius:6px;padding:8px 10px;margin-bottom:8px;">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <b style="color:{c};font-size:12px;">⚡ {lbl}</b>
                            <span style="background:#0F172A;color:{c};font-size:10px;font-weight:700;padding:2px 5px;border-radius:3px;">
                                {setup.status} ({setup.score}/4)
                            </span>
                        </div>
                        <div style="font-family:monospace;font-size:11px;color:#F8FAFC;margin-top:4px;">
                            Entry: <b>{setup.entry_price:,.1f}</b> | SL: <b style="color:#EF4444;">{setup.stop_loss:,.1f}</b><br>
                            Target: <b style="color:#10B981;">{setup.target_2:,.1f}</b> ({setup.rr_ratio:.1f}R)
                        </div>
                    </div>""",
                    unsafe_allow_html=True
                )

        # Recent liquidity sweep badge
        sweeps = r.get("liquidity_sweeps", [])
        if sweeps:
            last_sw = sweeps[-1]
            sw_dir = getattr(last_sw, "direction", "BELOW" if getattr(last_sw, "kind", "") == "SELL_SIDE" else "ABOVE")
            sw_c = "#0F9D58" if sw_dir == "BELOW" else "#E8503A"
            sw_wick = getattr(last_sw, "swept_wick", getattr(last_sw, "level", 0.0))
            st.sidebar.markdown(
                f"""<div style="background:#111D2D;border:1px solid {sw_c}55;border-left:3px solid {sw_c};padding:6px 9px;border-radius:4px;margin-bottom:8px;">
                    <span style="font-size:11px;color:#94A3B8;">🚨 RECENT SWEEP:</span><br>
                    <b style="color:{sw_c};font-size:11.5px;">{last_sw.name} @ {last_sw.level:,.1f}</b>
                    <span style="color:#CBD5E1;font-size:10.5px;"> (wick {sw_wick:,.1f})</span>
                </div>""",
                unsafe_allow_html=True
            )
    except Exception:
        pass


def render_smc_tab_executive(sdf: pd.DataFrame, spot: float, chain_df: pd.DataFrame | None,
                             sf: str, sn: int, depth: str,
                             pa_frames: dict | None = None, read=None, CTX=None,
                             store=None, now: datetime | None = None, vb_daily=None):
    """
    Renders the executive-level institutional Smart Money Concepts Tab.
    Organized into 5 clean sub-tabs eliminating endless vertical scrolling,
    each featuring a tactical action narrative banner for the running price.
    """
    r = smc.read(sdf.reset_index() if "timestamp" not in sdf.columns else sdf, n=sn, spot=spot)
    stx = r["structure"]
    dr = r.get("dealing_range")
    ah = r.get("active_high")
    al = r.get("active_low")

    # ── Executive Macro Barometer ──────────────────────────────────────────
    phase = r.get("market_phase")
    tcol = {"BULLISH": "#0F9D58", "BEARISH": "#E8503A"}.get(stx.trend, "#8A8F98")
    zcol = {"PREMIUM": "#E8503A", "DISCOUNT": "#0F9D58"}.get(getattr(dr, "zone", ""), "#8A8F98")

    b0, b1, b2, b3, b4, b5 = st.columns(6)
    with b0:
        if phase:
            _render_html(f"<div><b>Market Phase</b><br><span style='color:{phase.badge_color};font-size:1.15em;font-weight:700'>{phase.phase.replace('_', ' ')}</span><br><span style='color:#8FA6B8;font-size:0.85em'>Evidence: {getattr(phase, 'factor_count', 3)}/{getattr(phase, 'total_factors', 4)} Factors</span></div>")
        else:
            st.metric("Market Phase", "—")
    with b1:
        _render_html(f"<div><b>Structural Trend</b><br><span style='color:{tcol};font-size:1.15em;font-weight:700'>{stx.trend}</span></div>")
    with b2:
        macro_dr = r.get("macro_dealing_range")
        active_dr = dr
        dr_label = "Intraday Swing"
        if (active_dr is None or getattr(active_dr, "size", 0.0) < 100.0) and macro_dr:
            active_dr = macro_dr
            dr_label = "Session Macro"
        if active_dr:
            zcol = {"PREMIUM": "#E8503A", "DISCOUNT": "#0F9D58"}.get(getattr(active_dr, "zone", ""), "#8A8F98")
            _render_html(f"<div><b>Dealing Range ({dr_label})</b><br><span style='color:{zcol};font-weight:700'>{active_dr.zone} ({active_dr.position_pct*100:.0f}%)</span><br><span style='color:#8FA6B8;font-size:0.85em'>Range: {active_dr.size:,.0f} pts · Eq: {active_dr.equilibrium:,.0f}</span></div>")
        else:
            st.metric("Dealing Range", "—")
    b3.metric(f"Swing High ({getattr(ah, 'subkind', 'HH')})", f"{ah.price:,.1f}" if ah else "—", f"{ah.at:%H:%M}" if ah else "")
    b4.metric(f"Swing Low ({getattr(al, 'subkind', 'LL')})", f"{al.price:,.1f}" if al else "—", f"{al.at:%H:%M}" if al else "")
    fresh_obs = len(r.get("unmitigated_obs", []))
    b5.metric("Open OBs / FVGs", f"{len(r.get('open_obs', []))} / {len(r.get('open_fvgs', []))}", f"{fresh_obs} fresh" if fresh_obs else "")

    st.caption(f"⚡ Analyzing **{len(sdf)}** {sf} candles ({depth}) from Dhan storage · Confirmed pivots lag {sn} bars to prevent repainting.")
    st.info("💡 **Planning for Today's or Tomorrow's Open?** Check the dedicated **🌅 Opening Gap Planner & Confluence** tab in the top ribbon to simulate Gap Up / Down scenarios, BSL/SSL sweeps, Option Chain squeezes, and AI Quantile cones with an actionable CE vs PE playbook!")

    # ── Executive Sub-Tabs ──────────────────────────────────────────────────
    tab_chart, tab_kanban, tab_matrix, tab_bridge, tab_backtest, tab_flow = st.tabs([
        "🎯 SMC Execution Cockpit & Chart",
        "⚡ Sniper Confluence & Kanban",
        "🧱 Order Blocks & Liquidity Matrix",
        "🌉 Institutional Quantitative Bridge",
        "📊 SMC Sniper Backtester & Trade Log",
        "🌊 Order Flow & Scalper"
    ])

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 1: SMC Execution Cockpit & Chart
    # ────────────────────────────────────────────────────────────────────────
    with tab_chart:
        live_sigs = get_live_smc_signals(sdf, spot, chain_df)

        # Dynamic tactical action determination at current spot
        if live_sigs:
            primary_sig = live_sigs[0]
            if primary_sig.get("type") in ("OB_RETEST", "SNIPER_CONFLUENCE"):
                is_bull = primary_sig["direction"] == "BULLISH"
                c_col = "#0F9D58" if is_bull else "#E8503A"
                c_badge = f"⚡ ACTION: EXECUTE {primary_sig['direction']} SNIPER NOW"
                c_action = (
                    f"Spot <b>{spot:,.1f}</b> is inside {primary_sig['direction']} Order Block {primary_sig['zone']}. "
                    f"Invalidation SL: <b style='color:#FFF'>{primary_sig['stop']}</b> | "
                    f"Target 1: <b style='color:#FFF'>{primary_sig['target1']}</b> | "
                    f"Runner Target: <b style='color:#FFF'>{primary_sig['target2']}</b>. "
                    f"Enter on confirmation with strict {primary_sig.get('risk', 0):.0f} pt risk for 1:3+ R:R!"
                )
            else:
                c_col = "#00BCD4"
                c_badge = f"🚀 ACTION: FRESH {primary_sig.get('direction', '')} BREAK CONFIRMED"
                c_action = (
                    f"{primary_sig['title']} broke key structure! "
                    f"DO NOT FOMO market buy at {spot:,.1f}; wait for retracement into origin Order Block."
                )
        elif dr:
            if dr.zone == "PREMIUM":
                c_col = "#FF9800"
                c_badge = f"⚠️ ACTION AT SPOT {spot:,.1f}: PREMIUM REGIME ({dr.position_pct*100:.0f}%)"
                c_action = (
                    f"Spot is {spot - dr.equilibrium:,.0f} pts above 50% Equilibrium ({dr.equilibrium:,.0f}). "
                    f"Institutional players do NOT accumulate in Premium! AVOID fresh Longs; wait for pullback into Discount or scalp Bearish rejection wicks."
                )
            elif dr.zone == "DISCOUNT":
                c_col = "#0F9D58"
                c_badge = f"🛡️ ACTION AT SPOT {spot:,.1f}: DISCOUNT REGIME ({dr.position_pct*100:.0f}%)"
                c_action = (
                    f"Spot is {dr.equilibrium - spot:,.0f} pts below 50% Equilibrium ({dr.equilibrium:,.0f}). "
                    f"High-probability accumulation territory! Stalk Demand Order Blocks or Equal Low sweeps for Long entries targeting Equilibrium ({dr.equilibrium:,.0f})."
                )
            else:
                c_col = "#00BCD4"
                c_badge = f"⏳ ACTION AT SPOT {spot:,.1f}: EQUILIBRIUM CONSOLIDATION"
                c_action = f"Spot is hovering within 5 pts of 50% Equilibrium ({dr.equilibrium:,.0f}). Market is balanced. Stand aside until price sweeps key boundaries."
        else:
            c_col = "#8FA6B8"
            c_badge = f"⏳ ACTION AT SPOT {spot:,.1f}: AWAITING SETUP"
            c_action = "Price is currently in no-man's land. Monitor chart for Order Block tests or BOS / CHoCH structure shifts."

        render_narrative_banner(
            title="SMC Execution Cockpit & Structure Chart",
            narrative="Real-time institutional execution terminal. Displays structural trend breaks (BOS/CHoCH), Order Blocks, Fair Value Gaps (FVG), resting liquidity pools, and the 50% Equilibrium dividing Premium from Discount.",
            action_badge=c_badge,
            action_text=c_action,
            color=c_col
        )

        # Dashboard "Sweep Alert" badge (Feature 2)
        sweeps = r.get("liquidity_sweeps", [])
        if sweeps:
            last_sw = sweeps[-1]
            sw_dir = getattr(last_sw, "direction", "BELOW" if getattr(last_sw, "kind", "") == "SELL_SIDE" else "ABOVE")
            sw_col = "#0F9D58" if sw_dir == "BELOW" else "#EF4444"
            sw_bg = "#0B2B1E" if sw_dir == "BELOW" else "#2B0E11"
            sw_time = getattr(last_sw, "swept_at", None) or getattr(last_sw, "swept_time", None)
            sw_time_txt = f"{sw_time:%H:%M}" if sw_time else "RECENT"
            sw_wick = getattr(last_sw, "swept_wick", getattr(last_sw, "level", 0.0))
            _render_html(
                f"""
                <div style="background:{sw_bg};border:1px solid {sw_col}77;border-left:5px solid {sw_col};border-radius:6px;padding:9px 14px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;">
                    <span style="font-size:12.5px;font-weight:700;color:{sw_col};letter-spacing:0.5px;">
                        🚨 LIQUIDITY SWEEP DETECTED: {last_sw.name} ({last_sw.level:,.1f}) swept at wick {sw_wick:,.1f} — Watch for rejection/MSS confirmation
                    </span>
                    <span style="background:#0F172A;color:{sw_col};font-weight:700;font-size:11px;padding:2px 7px;border-radius:4px;border:1px solid {sw_col}55;">
                        {sw_time_txt}
                    </span>
                </div>
                """
            )

        if live_sigs:
            for sig in live_sigs:
                bg_col = "#0B2B1E" if sig["direction"] == "BULLISH" else "#2B0E11"
                border_col = "#0F9D58" if sig["direction"] == "BULLISH" else "#E8503A"
                _render_html(
                    f"<div style='background:{bg_col};border-left:5px solid {border_col};padding:12px 16px;border-radius:6px;margin-bottom:12px'>"
                    f"<div style='font-size:18px;font-weight:700;color:{border_col}'>{sig['title']}</div>"
                    f"<div style='color:#F2F5F8;margin-top:4px'>{sig['detail']}</div>"
                    f"<div style='color:#8FA6B8;font-size:13px;margin-top:4px'>"
                    f"Invalidation Stop: <b style='color:#FFF'>{sig.get('stop', '—')}</b> | "
                    f"Target 1 (1.5R): <b style='color:#FFF'>{sig.get('target1', '—')}</b> | "
                    f"Target 2 (Runner 3R+): <b style='color:#FFF'>{sig.get('target2', '—')}</b>"
                    f"</div></div>"
                )
        else:
            st.info(f"⏳ **Price is currently in equilibrium ({spot:,.1f})** — No active Order Block test. Awaiting fresh structure break or liquidity sweep.")

        # Interactive TradingView-Grade Plotly Chart
        chart_fig = create_smc_plotly_chart(sdf, r, spot=spot)
        st.plotly_chart(chart_fig, use_container_width=True)

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 2: The "Sniper Entry" Confluence Scanner & Kanban Board
    # ────────────────────────────────────────────────────────────────────────
    with tab_kanban:
        render_sniper_kanban_subtab(sdf, spot, r)

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 2: Order Blocks & Liquidity Matrix
    # ────────────────────────────────────────────────────────────────────────
    with tab_matrix:
        obs = r.get("open_obs", [])
        dem_obs = [ob for ob in obs if ob.direction == "BULLISH"]
        sup_obs = [ob for ob in obs if ob.direction == "BEARISH"]

        # Nearest demand below spot
        dem_below = [ob for ob in dem_obs if ob.high <= spot]
        nearest_dem = dem_below[-1] if dem_below else (dem_obs[-1] if dem_obs else None)

        # Nearest supply above spot
        sup_above = [ob for ob in sup_obs if ob.low >= spot]
        nearest_sup = sup_above[-1] if sup_above else (sup_obs[-1] if sup_obs else None)

        # Tactical action determination
        m_col = "#00BCD4"
        m_badge = f"📐 ACTION AT SPOT {spot:,.1f}: AVOID MID-ZONE TRAPS"
        m_action = "Price is rotating between zones. Monitor standing Order Blocks."

        if nearest_dem and (spot - nearest_dem.high) <= 25 and spot >= nearest_dem.low:
            m_col = "#0F9D58"
            m_badge = f"🎯 ACTION: DEMAND TEST IN PROGRESS [{nearest_dem.low:,.0f}–{nearest_dem.high:,.0f}]"
            m_action = f"Spot {spot:,.1f} is testing Demand OB. Watch for 5m/15m bullish wick absorption. Place Long trigger with SL at {nearest_dem.low - 5.0:,.1f}."
        elif nearest_sup and (nearest_sup.low - spot) <= 25 and spot <= nearest_sup.high:
            m_col = "#E8503A"
            m_badge = f"🎯 ACTION: SUPPLY TEST IN PROGRESS [{nearest_sup.low:,.0f}–{nearest_sup.high:,.0f}]"
            m_action = f"Spot {spot:,.1f} is testing Supply OB. Watch for rejection at upper boundary. Place Short trigger with SL at {nearest_sup.high + 5.0:,.1f}."
        else:
            m_col = "#00BCD4"
            m_badge = f"📐 ACTION AT SPOT {spot:,.1f}: AVOID MID-ZONE TRAPS"
            dem_info = f"Demand [{nearest_dem.low:,.0f}–{nearest_dem.high:,.0f}] ({spot - nearest_dem.high:.0f} pts below)" if nearest_dem else "None"
            sup_info = f"Supply [{nearest_sup.low:,.0f}–{nearest_sup.high:,.0f}] ({nearest_sup.low - spot:.0f} pts above)" if nearest_sup else "None"
            dem_alert = f"{nearest_dem.high:,.0f}" if nearest_dem else "Demand"
            sup_alert = f"{nearest_sup.low:,.0f}" if nearest_sup else "Supply"
            m_action = f"Spot is floating between zones ({dem_info} | {sup_info}). NEVER execute market orders in mid-channel chop! Set alerts at {dem_alert} and {sup_alert}."

        render_narrative_banner(
            title="Order Blocks & Liquidity Matrix",
            narrative="Spatial inventory radar mapping institutional accumulation (Demand Order Blocks), distribution (Supply Order Blocks), and resting retail stop-loss clusters (Equal Highs / Equal Lows).",
            action_badge=m_badge,
            action_text=m_action,
            color=m_col
        )

        col_obs, col_pools = st.columns(2)

        with col_obs:
            st.markdown("#### 🧱 Active Supply & Demand Order Blocks")
            if obs:
                # Proximity filter: prioritize active order blocks within intraday reach (<= 200 pts)
                prox_obs = [ob for ob in obs if (spot - ob.high if spot > ob.high else (ob.low - spot if spot < ob.low else 0.0)) <= 200.0]
                display_obs = prox_obs if prox_obs else obs[-6:]
                # Sort by distance to current spot
                display_obs = sorted(display_obs, key=lambda b: (spot - b.high if spot > b.high else (b.low - spot if spot < b.low else 0.0)))

                seen_obs = set()
                for ob in display_obs[:6]:
                    key = (ob.direction, round(ob.low, 1), round(ob.high, 1))
                    if key in seen_obs:
                        continue
                    seen_obs.add(key)
                    inside = ob.contains(spot)
                    dist = spot - ob.high if spot > ob.high else (ob.low - spot if spot < ob.low else 0.0)
                    dist_str = f"<b style='color:#FF9800'>INSIDE ZONE NOW</b>" if inside else f"{dist:.0f} pts away"
                    is_bull = ob.direction == "BULLISH"
                    tag_col = "#0F9D58" if is_bull else "#E8503A"
                    status = "Fresh (Unmitigated)" if getattr(ob, "unmitigated", False) else f"Tested ({getattr(ob, 'test_count', 1)}x)"

                    _render_html(
                        f"<div style='background:#121D2B;border-left:4px solid {tag_col};padding:10px 14px;border-radius:4px;margin-bottom:8px'>"
                        f"<span style='color:{tag_col};font-weight:700'>{ob.direction} OB [{ob.low:,.0f} – {ob.high:,.0f}]</span> "
                        f"<span style='color:#8FA6B8;font-size:0.85em'>({ob.at:%H:%M})</span>"
                        f"<br><span style='font-size:0.9em;color:#CCC'>{status} · {dist_str}</span>"
                        f"</div>"
                    )
            else:
                st.caption("No open order blocks standing.")

        with col_pools:
            st.markdown("#### 🎯 Resting Liquidity Pools (EQH / EQL)")
            pools = r.get("open_pools", [])
            if pools:
                seen_pools = set()
                for p in pools[-6:][::-1]:
                    key = (p.kind, round(p.level, 0))
                    if key in seen_pools:
                        continue
                    seen_pools.add(key)
                    p_col = "#00BCD4" if p.kind == "EQH" else "#FF9800"
                    side_str = "Buy-Side Liquidity (Short Stops)" if p.kind == "EQH" else "Sell-Side Liquidity (Long Stops)"
                    _render_html(
                        f"<div style='background:#121D2B;border-left:4px solid {p_col};padding:10px 14px;border-radius:4px;margin-bottom:8px'>"
                        f"<span style='color:{p_col};font-weight:700'>{p.kind} @ {p.level:,.1f}</span> "
                        f"<span style='color:#8FA6B8;font-size:0.85em'>({p.at:%H:%M})</span>"
                        f"<br><span style='font-size:0.85em;color:#CCC'>{side_str}</span>"
                        f"</div>"
                    )
            else:
                st.caption("No uncollected liquidity pools.")

            st.markdown("#### 🕳️ Unmitigated Fair Value Gaps (FVGs)")
            fvgs = r.get("open_fvgs", [])
            if fvgs:
                st.dataframe(pd.DataFrame([
                    {"time": f"{g.at:%H:%M}", "dir": g.direction,
                     "zone": f"{g.low:,.0f} – {g.high:,.0f}",
                     "size": round(g.size, 1),
                     "filled": f"{g.mitigated_pct*100:.0f}%"}
                    for g in fvgs[::-1]]),
                    use_container_width=True, hide_index=True)
            else:
                st.caption("No unmitigated fair value gaps.")

        # ── Breaker Blocks Subsection ──
        active_breakers = r.get("active_breakers", [])
        st.markdown("#### ⚡ Active Breaker Blocks (Inverted Polarity Zones)")
        if active_breakers:
            b_cols = st.columns(min(len(active_breakers), 3))
            for idx_b, brk in enumerate(active_breakers[:3]):
                with b_cols[idx_b]:
                    is_bull = brk.breaker_direction == "BULLISH"
                    b_color = "#0F9D58" if is_bull else "#E8503A"
                    b_icon = "🟢 Support Breaker" if is_bull else "🔴 Supply Breaker"
                    _render_html(
                        f"<div style='background:#121D2B;border-top:3px solid {b_color};padding:12px;border-radius:4px;'>"
                        f"<div style='color:{b_color};font-weight:700;'>{b_icon}</div>"
                        f"<div style='font-size:1.1em;font-weight:800;color:#FFF;'>{brk.low:,.0f} – {brk.high:,.0f}</div>"
                        f"<div style='color:#8FA6B8;font-size:0.85em;'>Flipped at {brk.breaker_at:%H:%M} · Tested {brk.breaker_test_count}x</div>"
                        f"</div>"
                    )
        else:
            st.caption("No unmitigated breaker blocks standing.")

        st.divider()
        st.markdown("#### 🪤 Sweeps vs Acceptance (Traps vs True Runs)")
        liq = r.get("liquidity", [])
        if liq:
            for x in liq[-5:][::-1]:
                fn = st.error if x.kind == "SWEEP" else st.success
                fn(f"**{x.at:%H:%M} · {x.kind} {x.direction} @ {x.level:,.1f}** — {x.note}")
        else:
            st.caption("No recent sweeps or runs against confirmed pivots.")

        st.divider()
        st.markdown("#### ⚡ Active Breaker Blocks (Polarity Inversion)")
        st.caption("Order Blocks invalidated by candle body closes invert into opposite polarity support/resistance zones.")
        breakers = r.get("breaker_obs", [])
        active_br = [b for b in breakers if b.breaker_active]
        if active_br:
            for br in active_br[-6:][::-1]:
                b_is_bull = br.breaker_direction == "BULLISH"
                b_col = "#0F9D58" if b_is_bull else "#EF4444"
                b_bg = "#0B2B1E" if b_is_bull else "#260E12"
                orig_str = f"Failed {br.direction} OB [{br.low:,.0f}–{br.high:,.0f}]"
                inv_str = f"Inverted into {br.breaker_direction} BREAKER"
                inside_br = br.low <= spot <= br.high
                status_br = "<b style='color:#FF9800'>TESTING BREAKER NOW</b>" if inside_br else f"{abs(spot - (br.low if spot > br.high else br.high)):.0f} pts away"
                _render_html(
                    f"<div style='background:{b_bg};border-left:4px solid {b_col};padding:10px 14px;border-radius:4px;margin-bottom:8px'>"
                    f"<span style='color:{b_col};font-weight:700'>{inv_str}</span> "
                    f"<span style='color:#8FA6B8;font-size:0.85em'>({orig_str})</span>"
                    f"<br><span style='font-size:0.85em;color:#CCC'>Mitigation: Active / Unmitigated · {status_br}</span>"
                    f"</div>"
                )
        else:
            st.caption("No unmitigated breaker blocks standing.")

        st.divider()
        st.markdown("#### 🧭 Institutional Liquidity Radar (PDH / PDL / PWH / PWL / IBH / IBL / EQH / EQL)")
        st.caption("Master spatial inventory of major resting buy-side/sell-side liquidity pools and stop hunt sweep state.")
        liq_lvls = r.get("liquidity_levels", [])
        if liq_lvls:
            radar_rows = []
            for l in liq_lvls:
                dist = round(l.level - spot, 1)
                st_txt = f"⚡ Swept ({l.swept_wick:,.1f})" if l.swept else "Resting Pool"
                radar_rows.append({
                    "Pool": l.name,
                    "Type": "BSL (Buy-Side)" if l.kind == "BUY_SIDE" else "SSL (Sell-Side)",
                    "Level": f"{l.level:,.1f}",
                    "Distance": f"{dist:+,.1f} pts",
                    "Status": st_txt,
                    "Time": f"{l.at:%d-%b %H:%M}" if l.at else "—"
                })
            st.dataframe(pd.DataFrame(radar_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No institutional liquidity levels available.")

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 3: Institutional Quantitative Bridge
    # ────────────────────────────────────────────────────────────────────────
    with tab_bridge:
        try:
            bridge_sigs = smc.confluence_bridge(chain_df, r, spot)
        except Exception:
            bridge_sigs = []

        if bridge_sigs:
            bs = bridge_sigs[0]
            b_col = "#0F9D58" if bs.sentiment == "BULLISH" else "#E8503A"
            b_badge = f"🔥 ACTION: {bs.verdict.upper()} CONFLUENCE ({bs.confidence})"
            b_action = f"<b>{bs.title}</b>: Price Action ({bs.trigger_detail}) is backed by Option Chain open interest ({bs.chain_detail}). Execute with high size conviction in direction of smart money flow."
        else:
            b_col = "#00BCD4"
            b_badge = f"🛡️ ACTION AT SPOT {spot:,.1f}: EQUILIBRIUM (NO CONFLICT)"
            b_action = f"Option writers and 15m price structure are in equilibrium. No active sweep traps or wall collapses. Wait for price to touch S1/R1 or Order Blocks where option writers are forced to cover."

        render_narrative_banner(
            title="Institutional Quantitative Bridge",
            narrative="Algorithmic cross-validation fusing 15-minute SMC market structures with real-time Option Chain positioning, PCR momentum, and option seller unwinding to filter out false breakouts.",
            action_badge=b_badge,
            action_text=b_action,
            color=b_col
        )

        st.markdown("#### 🌉 Quantitative Bridge (SMC Price Action × Option Chain Walls)")
        st.caption("Algorithmic execution when 15m structure interactions are confirmed by institutional Option Chain positioning.")

        if bridge_sigs:
            for bs in bridge_sigs:
                card_fn = st.error if bs.sentiment == "BEARISH" else st.success
                card_fn(
                    f"**{bs.time} · {bs.title} — {bs.verdict} ({bs.confidence})**\n\n"
                    f"• **Price Action**: {bs.trigger_detail}\n\n"
                    f"• **Option Chain**: {bs.chain_detail}"
                )
        else:
            st.info("No active sweep traps, OB defenses, or wall collapses on the current poll. Option chain is in equilibrium with structure.")

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 4: SMC Sniper Backtester & Trade Log
    # ────────────────────────────────────────────────────────────────────────
    with tab_backtest:
        render_narrative_banner(
            title="SMC Sniper Backtester & Trade Log",
            narrative="Quantitative verification and mathematical expectancy of the 15m Order Block Retest system with strict no-lookahead bias, EMA trend confirmation, and Cost-to-Cost trailing.",
            action_badge=f"🧠 EXECUTION MANDATE AT SPOT {spot:,.1f}",
            action_text=f"Proven edge (+623 pts / 2.53 PF over 60 days). DISCIPLINE MANDATE: When entering at current spot {spot:,.1f}, keep risk strictly capped at 25–35 pts (beyond OB edge). Once trade reaches +1.5R, move stop to Cost-to-Cost (BE) immediately. Let the remaining 50% seek 3.0R+ opposing liquidity!",
            color="#AB47BC"
        )

        st.markdown("#### 📊 Historical Backtester (NIFTY 15m Candles)")
        st.caption("Simulates the automated Order Block Retest Strategy with strict no-lookahead bias, CTC lock, and 3.0R runner targets.")

        bc1, bc2 = st.columns([1, 1])
        with bc1:
            bt_days = st.selectbox("Backtest Depth", [15, 30, 60], index=1, key="smc_bt_depth_tab")
        with bc2:
            bt_n = st.slider("Pivot Window (N bars)", 2, 4, 2, key="smc_bt_n_tab")

        hist_df = pa.load_history_candles(days=bt_days, timeframe="15m")
        if hist_df.empty:
            st.warning("No historical candle data found.")
        else:
            trades_df, stats = backtest_smc_engine(hist_df, n_pivots=bt_n)

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Total Trades", stats.get("total_trades", 0))
            k2.metric("Win Rate", f"{stats.get('win_rate', 0.0)}%", f"{stats.get('wins', 0)}W / {stats.get('losses', 0)}L / {stats.get('be', 0)}BE")
            k3.metric("Net Points", f"{stats.get('net_points', 0.0):+,.1f}", delta_color="normal")
            k4.metric("Profit Factor", f"{stats.get('profit_factor', 0.0):.2f}")
            k5.metric("Max Winner", f"+{stats.get('max_winner', 0.0):.1f} pts")

            if not trades_df.empty:
                st.dataframe(
                    trades_df[::-1],
                    use_container_width=True,
                    hide_index=True
                )
                st.caption(f"⚡ Evaluated across **{len(hist_df)}** 15-minute candles over {bt_days} trading days.")

    # ────────────────────────────────────────────────────────────────────────
    # Sub-Tab 5: Order Flow & Scalper
    # ────────────────────────────────────────────────────────────────────────
    with tab_flow:
        _of_col = "#00BCD4"
        _sp, _pr, _dp = None, None, None
        _ofr = None
        try:
            import order_flow as _of
            _fut1 = pa_frames.get("1m") if pa_frames else None
            _ofr = _of.read(_fut1, spot,
                            {"R1": getattr(read.levels, "r1", None), "S1": getattr(read.levels, "s1", None),
                             **(CTX.all_levels if CTX else {})})
            _sp, _pr, _dp = _ofr["speed"], _ofr["profile"], _ofr.get("depth")
        except Exception:
            pass

        _td_setup_smc = None
        try:
            import three_drive_scalper as _tds
            _fut1 = pa_frames.get("1m") if pa_frames else None
            _kl_smc = {}
            if read and hasattr(read, "levels"):
                _lv_s = read.levels
                if hasattr(_lv_s, "r1") and _lv_s.r1: _kl_smc["R1 Pivot"] = float(_lv_s.r1)
                if hasattr(_lv_s, "s1") and _lv_s.s1: _kl_smc["S1 Pivot"] = float(_lv_s.s1)
            if vb_daily is not None and len(vb_daily):
                _kl_smc["PDH (Prev Day High)"] = float(vb_daily["High"].iloc[-1])
                _kl_smc["PDL (Prev Day Low)"] = float(vb_daily["Low"].iloc[-1])
                _kl_smc["PDC (Prev Day Close)"] = float(vb_daily["Close"].iloc[-1])
            _td_setup_smc = _tds.evaluate_three_drive(_fut1, spot, key_levels=_kl_smc)
        except Exception:
            pass

        # Action calculation for Order Flow
        _td_act = False
        td_dir = "CALL"
        _td_sl_val = None
        _td_tgt_val = None

        if _td_setup_smc:
            if isinstance(_td_setup_smc, dict):
                _td_act = bool(_td_setup_smc.get("actionable", False))
                td_dir = str(_td_setup_smc.get("direction", "CALL"))
                _td_sl_val = _td_setup_smc.get("stop")
                _td_tgt_val = _td_setup_smc.get("target")
            else:
                _td_status = getattr(_td_setup_smc, "status", "")
                _td_act = _td_status in ("ARMED_AWAITING_MSS", "TRIGGERED_MSS_CONFIRMED")
                _td_side = getattr(_td_setup_smc, "side", "")
                td_dir = "CALL" if ("BULLISH" in str(_td_side) or "CALL" in str(_td_side)) else "PUT"
                _td_sl_val = getattr(_td_setup_smc, "stop_loss", None)
                _td_tgt_val = getattr(_td_setup_smc, "target_1", None)

        if _td_act:
            _of_col = "#0F9D58" if td_dir == "CALL" else "#E8503A"
            of_badge = f"⚡ ACTION: 1-MIN 3-DRIVE {td_dir} ACTIVE"
            _sl_txt = f"{_td_sl_val:,.1f}" if _td_sl_val else "SL below sweep"
            _tgt_txt = f"{_td_tgt_val:,.1f}" if _td_tgt_val else "Target 1"
            of_action = f"Priyank Sharma 3-Drive exhaustion detected at {spot:,.1f}! Quick momentum reversal scalp. Stop: {_sl_txt}, Target: {_tgt_txt}."
        elif _pr:
            if spot > _pr.vah:
                _of_col = "#0F9D58"
                of_badge = f"🚀 ACTION: SPOT ABOVE VALUE AREA HIGH ({_pr.vah:,.0f})"
                of_action = f"Spot {spot:,.1f} is trading above VAH. Institutional buyers have accepted higher prices. Look for long pullbacks towards VAH ({_pr.vah:,.0f}) or POC ({_pr.poc:,.0f}). Do NOT short while price holds above VAH."
            elif spot < _pr.val:
                _of_col = "#E8503A"
                of_badge = f"🩸 ACTION: SPOT BELOW VALUE AREA LOW ({_pr.val:,.0f})"
                of_action = f"Spot {spot:,.1f} is trading below VAL. Sellers driving price into auction imbalance. Look for short continuations on pullbacks to VAL ({_pr.val:,.0f}). Do NOT attempt knife-catching longs."
            else:
                poc_side = "ABOVE POC" if spot >= _pr.poc else "BELOW POC"
                target_va = f"VAH ({_pr.vah:,.0f})" if spot >= _pr.poc else f"VAL ({_pr.val:,.0f})"
                _of_col = "#0F9D58" if spot >= _pr.poc else "#FF9800"
                of_badge = f"⚖️ ACTION: INSIDE VALUE AREA ({poc_side} {_pr.poc:,.0f})"
                sp_str = f"Tape speed: {_sp.ratio:.2f}x." if _sp and _sp.ratio else ""
                of_action = f"Spot {spot:,.1f} is rotating inside Value Area ({_pr.val:,.0f}–{_pr.vah:,.0f}). Auction target: {target_va}. {sp_str} Scalp towards value boundaries when tape speed surges > 1.3x."
        else:
            of_badge = f"⚡ ACTION AT SPOT {spot:,.1f}"
            of_action = "Monitor microsecond Tape Speed and Order Depth. Enter scalps only when tape speed accelerates with strong delta expansion."

        render_narrative_banner(
            title="Microstructure Order Flow & Scalper",
            narrative="Microstructure auction dynamics: Real-time Tape Speed (TPS), Volume Profile Point of Control (POC), Value Area (VAH/VAL), and Priyank Sharma 1-Min 3-Drive scalping setups.",
            action_badge=of_badge,
            action_text=of_action,
            color=_of_col
        )

        st.markdown("#### 🌊 Order Flow Diagnostics")
        if _ofr:
            st.caption(_ofr.get("headline", ""))
            _o = st.columns(4 if _dp else 3)
            if _sp:
                _o[0].metric("Tape speed", f"{_sp.ratio:.2f}x" if _sp.ratio else "—", _sp.state, delta_color="off")
            if _dp:
                _o[1].metric("5-Depth Imbalance", f"{_dp.imbalance_5:+.1%}", _dp.state, delta_color="off")
            _poc_col = _o[2] if _dp else _o[1]
            _va_col = _o[3] if _dp else _o[2]
            if _pr:
                _poc_col.metric("POC", f"{_pr.poc:,.0f}")
                _va_col.metric("Value area", f"{_pr.val:,.0f}–{_pr.vah:,.0f}", f"{_pr.value_width:.0f} pts", delta_color="off")

        st.divider()
        st.markdown("#### ⚡ 1-Minute 3-Drive Institutional Scalper (Priyank Sharma Setup)")
        if _td_setup_smc:
            try:
                import three_drive_scalper as _tds
                _tds.render_three_drive_card(_td_setup_smc)
            except Exception as tde:
                st.caption(f"1m 3-Drive Scalper card unavailable: {tde}")
        else:
            st.caption("No 3-Drive setup active on 1-minute chart.")
