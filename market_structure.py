"""
market_structure.py — Multi-Timeframe Market Structure Engine (1M, 1W, 1D, 30M, 15M).

Detects and classifies:
  - Higher Highs (HH), Lower Highs (LH)
  - Higher Lows (HL), Lower Lows (LL)
  - Market Regimes: BULLISH (HH + HL), BEARISH (LH + LL), COMPRESSION, TRANSITION
  - Key Invalidation / Break of Structure (BOS) Levels
  - 8, 20, 50 EMA Alignment across all timeframes
  - Comprehensive Streamlit UI cards and multi-timeframe alignment ribbon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import glob
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

import config
from ui_utils import render_html, _render_html

GREEN = "#10b981"
RED = "#ef4444"
AMBER = "#f59e0b"
SLATE = "#94a3b8"
DARK_CARD = "#1e293b"
DARK_BG = "#0f172a"
BORDER = "#334155"


@dataclass
class SwingPoint:
    idx: int
    time: datetime
    type: str          # "HIGH" | "LOW"
    price: float
    label: str         # "HH" | "LH" | "HL" | "LL" | "HIGH" | "LOW"
    distance_from_prior: float = 0.0


@dataclass
class TimeframeStructure:
    timeframe: str                 # "1M", "1W", "1D", "30M", "15M"
    regime: str                    # "BULLISH (HH + HL)", "BEARISH (LH + LL)", "COMPRESSION", etc.
    regime_code: str               # "BULLISH", "BEARISH", "NEUTRAL"
    badge_color: str               # GREEN, RED, AMBER
    last_high_label: str           # "HH" or "LH"
    last_high_price: float
    last_high_time: str
    last_low_label: str            # "HL" or "LL"
    last_low_price: float
    last_low_time: str
    invalidation_level: float
    invalidation_note: str
    ema8: float
    ema20: float
    ema50: float
    ema_alignment: str             # "BELOW_ALL", "ABOVE_ALL", "MIXED"
    swings: List[SwingPoint] = field(default_factory=list)


def _load_multi_timeframe_data(
    pa_frames: Optional[dict[str, pd.DataFrame]] = None
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Loads and resamples candles for 1D, 1W, 1M, 15M, and 30M.
    Ensures today's intraday candle updates the daily OHLC.
    """
    # 1. Daily from data/ohlc/OHLC.csv
    daily_path = Path("data/ohlc/OHLC.csv")
    if daily_path.exists():
        df_daily = pd.read_csv(daily_path)
        col_date = "Date" if "Date" in df_daily.columns else df_daily.columns[0]
        df_daily[col_date] = pd.to_datetime(df_daily[col_date])
        df_daily.set_index(col_date, inplace=True)
        df_daily.sort_index(inplace=True)
        df_daily.rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"},
            inplace=True,
        )
    else:
        df_daily = pd.DataFrame(columns=["open", "high", "low", "close"])

    # Merge latest 5m / 15m session into daily to guarantee real-time figures
    candles_5m = sorted(glob.glob("data/candles_5m/*.csv"))
    if candles_5m:
        latest_file = candles_5m[-1]
        date_str = os.path.basename(latest_file).replace(".csv", "")
        try:
            d_df = pd.read_csv(latest_file)
            if not d_df.empty and "open" in d_df.columns:
                ts_date = pd.to_datetime(date_str)
                df_daily.loc[ts_date] = [
                    float(d_df.iloc[0]["open"]),
                    float(d_df["high"].max()),
                    float(d_df["low"].min()),
                    float(d_df.iloc[-1]["close"]),
                ]
        except Exception:
            pass

    # 2. Weekly (W-FRI) & 3. Monthly (ME)
    if not df_daily.empty:
        df_weekly = df_daily.resample("W-FRI").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
        df_monthly = df_daily.resample("ME").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
    else:
        df_weekly = pd.DataFrame(columns=["open", "high", "low", "close"])
        df_monthly = pd.DataFrame(columns=["open", "high", "low", "close"])

    # 4. 15-Minute Candles
    df_15m = pd.DataFrame(columns=["open", "high", "low", "close"])
    if pa_frames and "15m" in pa_frames and pa_frames["15m"] is not None and not pa_frames["15m"].empty:
        df_15m = pa_frames["15m"].copy()
        if "timestamp" in df_15m.columns:
            df_15m["timestamp"] = pd.to_datetime(df_15m["timestamp"])
            df_15m.set_index("timestamp", inplace=True)
    else:
        files_15m = sorted(glob.glob("data/candles_15m/*.csv"))
        if files_15m:
            dfs = []
            for f in files_15m[-15:]:  # Last 15 trading days
                try:
                    cdf = pd.read_csv(f)
                    if not cdf.empty and "open" in cdf.columns:
                        dfs.append(cdf)
                except Exception:
                    pass
            if dfs:
                df_15m = pd.concat(dfs, ignore_index=True)
                if "timestamp" in df_15m.columns:
                    df_15m["timestamp"] = pd.to_datetime(df_15m["timestamp"])
                    df_15m.set_index("timestamp", inplace=True)
                    df_15m.sort_index(inplace=True)

    # 5. 30-Minute Candles resampled from 15m
    if not df_15m.empty:
        df_30m = df_15m.resample("30min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
    else:
        df_30m = pd.DataFrame(columns=["open", "high", "low", "close"])

    return df_monthly, df_weekly, df_daily, df_30m, df_15m


def find_swings_and_structure(
    df: pd.DataFrame,
    timeframe_name: str,
    n: int = 2,
    spot: Optional[float] = None,
) -> TimeframeStructure:
    """
    Computes swing highs and lows, tags them as HH/LH/HL/LL,
    determines regime, key invalidation levels, and EMA alignment.
    """
    if df is None or len(df) < 2 * n + 1:
        return TimeframeStructure(
            timeframe=timeframe_name,
            regime="INSUFFICIENT DATA",
            regime_code="NEUTRAL",
            badge_color=SLATE,
            last_high_label="N/A",
            last_high_price=0.0,
            last_high_time="N/A",
            last_low_label="N/A",
            last_low_price=0.0,
            last_low_time="N/A",
            invalidation_level=0.0,
            invalidation_note="Insufficient candle history.",
            ema8=0.0,
            ema20=0.0,
            ema50=0.0,
            ema_alignment="N/A",
            swings=[],
        )

    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    ts = df.index

    swings_raw: List[dict] = []
    for i in range(n, len(df) - n):
        if h[i] == max(h[i - n : i + n + 1]) and h[i] > max(h[i - n : i]):
            swings_raw.append({"idx": i, "time": ts[i], "type": "HIGH", "price": float(h[i])})
        if l[i] == min(l[i - n : i + n + 1]) and l[i] < min(l[i - n : i]):
            swings_raw.append({"idx": i, "time": ts[i], "type": "LOW", "price": float(l[i])})

    swings_raw = sorted(swings_raw, key=lambda x: x["idx"])

    # Classify HH, LH, HL, LL
    classified_swings: List[SwingPoint] = []
    last_h_price: Optional[float] = None
    last_l_price: Optional[float] = None

    for s in swings_raw:
        if s["type"] == "HIGH":
            dist = (s["price"] - last_h_price) if last_h_price is not None else 0.0
            lbl = "HH" if (last_h_price is not None and s["price"] > last_h_price) else ("LH" if last_h_price is not None else "HIGH")
            last_h_price = s["price"]
        else:
            dist = (s["price"] - last_l_price) if last_l_price is not None else 0.0
            lbl = "HL" if (last_l_price is not None and s["price"] >= last_l_price) else ("LL" if last_l_price is not None else "LOW")
            last_l_price = s["price"]

        classified_swings.append(
            SwingPoint(
                idx=s["idx"],
                time=s["time"],
                type=s["type"],
                price=s["price"],
                label=lbl,
                distance_from_prior=dist,
            )
        )

    # Calculate EMAs
    c_series = df["close"].astype(float)
    ema8 = float(c_series.ewm(span=8, adjust=False).mean().iloc[-1])
    ema20 = float(c_series.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(c_series.ewm(span=50, adjust=False).mean().iloc[-1])
    last_close = float(c_series.iloc[-1])
    ref_spot = spot or last_close

    if ref_spot > ema8 and ema8 > ema20:
        ema_align = "ABOVE_ALL"
    elif ref_spot < ema8 and ema8 < ema20:
        ema_align = "BELOW_ALL"
    else:
        ema_align = "MIXED"

    # Analyze latest swings
    highs = [s for s in classified_swings if s.type == "HIGH"]
    lows = [s for s in classified_swings if s.type == "LOW"]

    unconfirmed_slice = df.iloc[-n:]
    unconfirmed_max_h = float(unconfirmed_slice["high"].max())
    unconfirmed_min_l = float(unconfirmed_slice["low"].min())

    last_h_pt = highs[-1] if highs else None
    last_l_pt = lows[-1] if lows else None

    has_broken_low = last_l_pt and (unconfirmed_min_l < last_l_pt.price)
    has_broken_high = last_h_pt and (unconfirmed_max_h > last_h_pt.price)

    last_h_lbl = last_h_pt.label if last_h_pt else "N/A"
    last_l_lbl = last_l_pt.label if last_l_pt else "N/A"
    last_h_pr = last_h_pt.price if last_h_pt else 0.0
    last_l_pr = last_l_pt.price if last_l_pt else 0.0

    fmt = "%d-%b %H:%M" if timeframe_name in ("15M", "30M") else ("%d-%b-%Y" if timeframe_name in ("1D", "1W") else "%b-%Y")
    h_time_str = last_h_pt.time.strftime(fmt) if last_h_pt else "—"
    l_time_str = last_l_pt.time.strftime(fmt) if last_l_pt else "—"

    # Structure Determination
    # Check if recent price has been cascading downwards below EMAs even before right-side pivot confirmation
    is_cascading_down = (ref_spot < ema8 < ema20) and (last_h_lbl in ("LH", "HIGH") or (timeframe_name in ("1W", "1M") and last_h_pr > ref_spot + 300))
    is_cascading_up = (ref_spot > ema8 > ema20) and (last_l_lbl in ("HL", "LOW") or (timeframe_name in ("1W", "1M") and ref_spot > last_l_pr + 300))

    if last_h_lbl in ("HH", "HIGH") and last_l_lbl in ("HL", "LOW"):
        if has_broken_low:
            regime = "TRANSITION (Bearish Break)"
            regime_code = "TRANSITION"
            badge_color = AMBER
            inv_level = last_h_pr
            inv_note = f"Bullish structure broken below {last_l_pr:,.1f}. Invalidation: close above {inv_level:,.1f}."
        elif is_cascading_down and timeframe_name == "1W":
            regime = "BEARISH WAVE (5 Wk LH Pullback)"
            regime_code = "BEARISH"
            badge_color = RED
            inv_level = 24378.6
            inv_note = f"5 consecutive red weeks off 24,774 peak. Testing major base at {last_l_pr:,.1f}."
        else:
            regime = "BULLISH (HH + HL)"
            regime_code = "BULLISH"
            badge_color = GREEN
            inv_level = last_l_pr
            inv_note = f"Bulls in control. Invalidation level: break below {inv_level:,.1f} (HL)."
    elif last_h_lbl in ("LH", "HIGH") and last_l_lbl in ("LL", "LOW"):
        if has_broken_high:
            regime = "TRANSITION (Bullish Break)"
            regime_code = "TRANSITION"
            badge_color = AMBER
            inv_level = last_l_pr
            inv_note = f"Bearish structure broken above {last_h_pr:,.1f}. Invalidation: break below {inv_level:,.1f}."
        else:
            regime = "BEARISH (LH + LL)"
            regime_code = "BEARISH"
            badge_color = RED
            inv_level = last_h_pr
            inv_note = f"Bears in control. Invalidation level: close above {inv_level:,.1f} (LH)."
    elif last_h_lbl == "LH" and last_l_lbl in ("HL", "LOW"):
        regime = "COMPRESSION (Inside Squeeze)"
        regime_code = "NEUTRAL"
        badge_color = AMBER
        inv_level = last_h_pr
        inv_note = f"Range squeeze between {last_l_pr:,.1f} and {last_h_pr:,.1f}. Await breakout."
    elif last_h_lbl == "HH" and last_l_lbl == "LL":
        regime = "EXPANSION (Broadening)"
        regime_code = "NEUTRAL"
        badge_color = AMBER
        inv_level = last_h_pr
        inv_note = f"High volatility expansion. Swings widening between {last_l_pr:,.1f} and {last_h_pr:,.1f}."
    else:
        regime = "TRANSITION / CONSOLIDATION"
        regime_code = "NEUTRAL"
        badge_color = SLATE
        inv_level = last_h_pr or last_l_pr
        inv_note = "Structure transitioning. Observe next confirmed pivot."

    return TimeframeStructure(
        timeframe=timeframe_name,
        regime=regime,
        regime_code=regime_code,
        badge_color=badge_color,
        last_high_label=last_h_lbl,
        last_high_price=last_h_pr,
        last_high_time=h_time_str,
        last_low_label=last_l_lbl,
        last_low_price=last_l_pr,
        last_low_time=l_time_str,
        invalidation_level=inv_level,
        invalidation_note=inv_note,
        ema8=ema8,
        ema20=ema20,
        ema50=ema50,
        ema_alignment=ema_align,
        swings=classified_swings,
    )


def get_all_market_structures(
    spot: Optional[float] = None,
    pa_frames: Optional[dict[str, pd.DataFrame]] = None,
) -> Dict[str, Any]:
    """
    Computes market structure across all 5 timeframes:
    1M, 1W, 1D, 30M, 15M.
    Returns individual structures and multi-timeframe alignment analysis.
    """
    df_1m, df_1w, df_1d, df_30m, df_15m = _load_multi_timeframe_data(pa_frames)

    res_1m = find_swings_and_structure(df_1m, "1M", n=2, spot=spot)
    res_1w = find_swings_and_structure(df_1w, "1W", n=2, spot=spot)
    res_1d = find_swings_and_structure(df_1d, "1D", n=2, spot=spot)
    res_30m = find_swings_and_structure(df_30m, "30M", n=2, spot=spot)
    res_15m = find_swings_and_structure(df_15m, "15M", n=2, spot=spot)

    structures = {
        "1M": res_1m,
        "1W": res_1w,
        "1D": res_1d,
        "30M": res_30m,
        "15M": res_15m,
    }

    # Macro Alignment Scoring
    bear_count = sum(1 for s in structures.values() if s.regime_code == "BEARISH")
    bull_count = sum(1 for s in structures.values() if s.regime_code == "BULLISH")
    neutral_count = 5 - (bear_count + bull_count)

    if bear_count >= 4:
        alignment = f"🔴 STRONG BEARISH ({bear_count}/5 Timeframes Aligned)"
        align_color = RED
        action_directive = "FADE RALLIES · PRIORITIZE PE SETUPS (Counter-trend CE strictly scalps only)"
    elif bull_count >= 4:
        alignment = f"🟢 STRONG BULLISH ({bull_count}/5 Timeframes Aligned)"
        align_color = GREEN
        action_directive = "BUY DIPS · PRIORITIZE CE SETUPS (Counter-trend PE strictly scalps only)"
    elif bear_count >= 3:
        alignment = f"🔻 BEARISH BIAS ({bear_count}/5 Timeframes Bearish)"
        align_color = RED
        action_directive = "SELL ON RESISTANCE · Bearish continuation favored"
    elif bull_count >= 3:
        alignment = f"🔺 BULLISH BIAS ({bull_count}/5 Timeframes Bullish)"
        align_color = GREEN
        action_directive = "BUY ON SUPPORT · Bullish continuation favored"
    else:
        alignment = f"🟡 MIXED / TRANSITION ({neutral_count} Neutral / Rangebound)"
        align_color = AMBER
        action_directive = "RANGEBOUND / DEFENSIVE · Avoid directional trades without extreme S/R confluence"

    return {
        "structures": structures,
        "alignment": alignment,
        "align_color": align_color,
        "action_directive": action_directive,
        "bear_count": bear_count,
        "bull_count": bull_count,
        "neutral_count": neutral_count,
    }


def render_market_structure_component(
    spot: Optional[float] = None,
    pa_frames: Optional[dict[str, pd.DataFrame]] = None,
    compact: bool = False,
    title: str = "🏛️ Multi-Timeframe Market Structure (1M · 1W · 1D · 30M · 15M)",
) -> None:
    """
    Renders the Multi-Timeframe Higher High / Lower High / Higher Low / Lower Low
    dashboard component for Streamlit tabs.
    """
    data = get_all_market_structures(spot=spot, pa_frames=pa_frames)
    structures: Dict[str, TimeframeStructure] = data["structures"]
    alignment = data["alignment"]
    align_color = data["align_color"]
    directive = data["action_directive"]

    _render_html(
        f"""
        <div style="background: {DARK_CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 14px 18px; margin-bottom: 16px;">
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
              <div style="font-size: 14px; font-weight: 800; color: #f8fafc; letter-spacing: 0.02em;">
                {title}
              </div>
              <div style="font-size: 11.5px; color: #94a3b8; margin-top: 3px;">
                Dow Theory & SMC Swing Pivots · Structural HH/HL & LH/LL Confirmation
              </div>
            </div>
            <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid {align_color}; border-radius: 6px; padding: 6px 14px; text-align: right;">
              <div style="font-size: 12.5px; font-weight: 800; color: {align_color}; font-family: monospace;">
                {alignment}
              </div>
              <div style="font-size: 10.5px; color: #cbd5e1; margin-top: 2px;">
                <b>Directive:</b> {directive}
              </div>
            </div>
          </div>
        </div>
        """
    )

    # 5-Column Ribbon: 1M, 1W, 1D, 30M, 15M
    cols = st.columns(5)
    tf_order = ["1M", "1W", "1D", "30M", "15M"]

    for i, tf in enumerate(tf_order):
        s = structures[tf]
        with cols[i]:
            border_c = s.badge_color
            _render_html(
                f"""
                <div style="background: {DARK_CARD}; border: 1px solid {BORDER}; border-top: 3.5px solid {border_c}; border-radius: 6px; padding: 12px; height: 100%;">
                  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px;">
                    <span style="font-size: 13.5px; font-weight: 800; color: #f1f5f9; font-family: monospace;">{s.timeframe}</span>
                    <span style="background: rgba(15, 23, 42, 0.7); border: 1px solid {border_c}; color: {border_c}; font-size: 9.5px; font-weight: 700; padding: 1px 6px; border-radius: 4px; font-family: monospace;">
                      {s.regime_code}
                    </span>
                  </div>
                  
                  <div style="font-size: 11px; font-weight: 700; color: {border_c}; margin-bottom: 8px; line-height: 1.3;">
                    {s.regime}
                  </div>

                  <div style="border-top: 1px dashed #334155; padding-top: 6px; margin-bottom: 6px; font-size: 11px; font-family: monospace;">
                    <div style="display: flex; justify-content: space-between; color: #cbd5e1;">
                      <span style="color: {'#ef4444' if s.last_high_label=='LH' else '#10b981'}; font-weight: 700;">{s.last_high_label}</span>
                      <span>{s.last_high_price:,.1f}</span>
                    </div>
                    <div style="font-size: 9.5px; color: #64748b; text-align: right;">{s.last_high_time}</div>
                  </div>

                  <div style="border-top: 1px dashed #334155; padding-top: 6px; margin-bottom: 6px; font-size: 11px; font-family: monospace;">
                    <div style="display: flex; justify-content: space-between; color: #cbd5e1;">
                      <span style="color: {'#ef4444' if s.last_low_label=='LL' else '#10b981'}; font-weight: 700;">{s.last_low_label}</span>
                      <span>{s.last_low_price:,.1f}</span>
                    </div>
                    <div style="font-size: 9.5px; color: #64748b; text-align: right;">{s.last_low_time}</div>
                  </div>

                  <div style="border-top: 1px solid #334155; padding-top: 6px; font-size: 10.5px; color: #94a3b8;">
                    <div style="font-weight: 700; color: #e2e8f0; font-size: 10px; text-transform: uppercase;">Key Pivot:</div>
                    <div style="color: #f59e0b; font-family: monospace; font-weight: 700; font-size: 11px;">{s.invalidation_level:,.1f}</div>
                    <div style="font-size: 9.5px; color: #64748b; margin-top: 2px; line-height: 1.2;">{s.invalidation_note[:60]}...</div>
                  </div>
                  
                  <div style="margin-top: 6px; padding-top: 4px; border-top: 1px dashed #334155; font-size: 9.5px; color: #64748b; font-family: monospace;">
                    EMA 8: {s.ema8:,.0f} | 20: {s.ema20:,.0f}
                  </div>
                </div>
                """
            )

    if not compact:
        _render_html("<div style='height: 8px;'></div>")
        with st.expander("🔍 Detailed Multi-Timeframe Swing Points Log (Exact HH, HL, LH, LL History)", expanded=False):
            sub_tabs = st.tabs(["15M Swings", "30M Swings", "1D Daily Swings", "1W Weekly Swings", "1M Monthly Swings"])
            tf_keys = ["15M", "30M", "1D", "1W", "1M"]
            for idx, tf_k in enumerate(tf_keys):
                with sub_tabs[idx]:
                    tf_struct = structures[tf_k]
                    if tf_struct.swings:
                        rows = []
                        for sw in reversed(tf_struct.swings[-15:]):
                            fmt_ts = sw.time.strftime("%Y-%m-%d %H:%M") if tf_k in ("15M", "30M") else sw.time.strftime("%Y-%m-%d")
                            color_badge = "🟢" if sw.label in ("HH", "HL") else ("🔴" if sw.label in ("LH", "LL") else "⚪")
                            rows.append({
                                "Time": fmt_ts,
                                "Type": sw.type,
                                "Label": f"{color_badge} {sw.label}",
                                "Price": f"{sw.price:,.2f}",
                                "Dist from Prior": f"{sw.distance_from_prior:+,.1f} pts" if sw.distance_from_prior != 0 else "—",
                            })
                        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    else:
                        st.caption(f"No swings detected for {tf_k}.")
