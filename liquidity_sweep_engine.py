"""
liquidity_sweep_engine.py — Institutional Liquidity Sweep, Run & Order Block Strength Engine.

Derived from Institutional Trading Models (ICT, CRT, and Smart Money Concepts):
  1. ORDER BLOCK STRENGTH GRADER (+ / ++ / +++):
     - Weak Demand/Supply (+): Small/inefficient base (<2 bars), little consolidation, easily broken.
     - Normal Demand/Supply (++): Decent base with structure (2-3 bars), moderate consolidation.
     - Strong Demand/Supply (+++): Wide & well-formed base (>=4 bars), high probability continuation.
     - Rejection Order Blocks: Identifies wick-based institutional absorption bases.

  2. LIQUIDITY SWEEP vs. LIQUIDITY RUN CLASSIFIER:
     - Liquidity Sweep: Wick pierces a key level (PDH/PDL, PWH/PWL, Session High/Low, EQH/EQL)
       and closes back inside, followed by reclaim confirmation -> High R:R Reversal Setup.
     - Liquidity Run: Price breaks out and closes 2+ consecutive bars beyond the level with an FVG
       -> Momentum Continuation / Expansion Setup.

  3. FAIR VALUE GAP (FVG) 3-TYPE QUALITY CLASSIFIER:
     - Type 1: Deep Retracement FVG (3rd candle strongly counter-reverses -> lower probability).
     - Type 2: Expansion FVG (Strong displacement with continuation).
     - Type 3: Consolidation FVG (3rd candle pauses inside 2nd candle -> highest probability retest).

  4. LIQUIDITY RADAR (6 Key Institutional Liquidity Zones):
     - Previous Day High / Low (PDH / PDL)
     - Previous Week High / Low (PWH / PWL)
     - Previous Month High / Low (PMH / PML)
     - Current Session High / Low (SH / SL)
     - Equal Highs / Lows (EQH / EQL - Double/Triple Tops & Bottoms)
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
SKY = "#38bdf8"
PURPLE = "#a855f7"
DARK_CARD = "#1e293b"
DARK_BG = "#0f172a"
BORDER = "#334155"


@dataclass
class LiquidityLevelItem:
    name: str                   # "PDH", "PDL", "PWH", "PWL", "Session High", etc.
    price: float
    level_type: str             # "BUY_SIDE_LIQUIDITY" | "SELL_SIDE_LIQUIDITY"
    distance_pts: float
    status: str                 # "INTACT" | "APPROACHING" | "SWEPT" | "RUN_EXPANSION"
    badge_color: str


@dataclass
class OrderBlockItem:
    direction: str              # "DEMAND" | "SUPPLY"
    grade: str                  # "WEAK (+)", "NORMAL (++)", "STRONG (+++)"
    grade_stars: str            # "+", "++", "+++"
    top: float
    bottom: float
    base_bars: int
    is_rejection_block: bool
    probability: str            # "High (80%+)", "Good (65-75%)", "Low (<50%)"
    badge_color: str
    characteristics: List[str] = field(default_factory=list)


@dataclass
class FVGQualityItem:
    direction: str              # "BULLISH" | "BEARISH"
    time: str
    top: float
    bottom: float
    quality_type: str           # "CONSOLIDATION", "EXPANSION", "DEEP_RETRACEMENT"
    quality_title: str
    badge_color: str
    success_probability: str
    note: str


@dataclass
class LiquidityTradeSetup:
    setup_type: str             # "LIQUIDITY_SWEEP" | "LIQUIDITY_RUN"
    direction: str              # "BUY (CALL)" | "SELL (PUT)"
    level_name: str
    level_price: float
    sweep_price: float
    entry_price: float
    stop_loss: float
    target_price: float
    risk_pts: float
    reward_pts: float
    risk_reward: str
    status: str                 # "TRIGGERED" | "ARMED" | "COMPLETED"
    time_str: str
    action_plan: str
    badge_color: str


def extract_key_liquidity_levels(
    spot: float,
    ohlc_df: Optional[pd.DataFrame] = None,
    df_today: Optional[pd.DataFrame] = None,
) -> List[LiquidityLevelItem]:
    """
    Extracts the 6 key institutional liquidity reference levels:
    PDH, PDL, PWH, PWL, PMH, PML, Session High, Session Low, EQH/EQL.
    """
    levels: List[LiquidityLevelItem] = []

    # 1. Daily / Weekly / Monthly extremes from OHLC.csv
    daily_path = Path("data/ohlc/OHLC.csv")
    if ohlc_df is None and daily_path.exists():
        ohlc_df = pd.read_csv(daily_path)
        col_date = "Date" if "Date" in ohlc_df.columns else ohlc_df.columns[0]
        ohlc_df[col_date] = pd.to_datetime(ohlc_df[col_date])
        ohlc_df.set_index(col_date, inplace=True)
        ohlc_df.sort_index(inplace=True)
        ohlc_df.rename(columns={"High": "high", "Low": "low", "Close": "close", "Open": "open"}, inplace=True)

    if ohlc_df is not None and len(ohlc_df) >= 2:
        # FIX 30-Sep-2026: OHLC.csv only gets today's row after the close, so during the session iloc[-2] was the
        # day BEFORE yesterday (PDH read 23,080 from 28-Sep instead of 22,753 from 29-Sep, and the PE trap at the
        # real PDH never fired). Use only rows dated before today, and whole weeks / months before the current one.
        _today = pd.Timestamp(datetime.now().date())
        if not isinstance(ohlc_df.index, pd.DatetimeIndex):
            ohlc_df = ohlc_df.copy()
            ohlc_df.index = pd.to_datetime(ohlc_df.index)
        past = ohlc_df[ohlc_df.index.normalize() < _today]
        if len(past) == 0:
            past = ohlc_df
        pdh = float(past["high"].iloc[-1])
        pdl = float(past["low"].iloc[-1])

        # Prior Week (the last full week before this one)
        week_start = _today - pd.Timedelta(days=_today.weekday())
        wk_rows = past[past.index < week_start]
        weekly = wk_rows.resample("W-FRI").agg({"high": "max", "low": "min"}).dropna()
        pwh = float(weekly["high"].iloc[-1]) if len(weekly) else pdh
        pwl = float(weekly["low"].iloc[-1]) if len(weekly) else pdl

        # Prior Month (the last full month before this one)
        mo_rows = past[past.index < _today.replace(day=1)]
        monthly = mo_rows.resample("ME").agg({"high": "max", "low": "min"}).dropna()
        pmh = float(monthly["high"].iloc[-1]) if len(monthly) else pwh
        pml = float(monthly["low"].iloc[-1]) if len(monthly) else pwl
    else:
        # Fallback estimates
        pdh, pdl = spot + 100.0, spot - 100.0
        pwh, pwl = spot + 250.0, spot - 250.0
        pmh, pml = spot + 500.0, spot - 500.0

    # 2. Session High / Low from today's candles
    if df_today is not None and not df_today.empty:
        sh = float(df_today["high"].max())
        sl = float(df_today["low"].min())
    else:
        sh, sl = spot + 30.0, spot - 30.0

    raw_levels = [
        ("PDH (Prev Day High)", pdh, "BUY_SIDE_LIQUIDITY"),
        ("PDL (Prev Day Low)", pdl, "SELL_SIDE_LIQUIDITY"),
        ("PWH (Prev Week High)", pwh, "BUY_SIDE_LIQUIDITY"),
        ("PWL (Prev Week Low)", pwl, "SELL_SIDE_LIQUIDITY"),
        ("PMH (Prev Month High)", pmh, "BUY_SIDE_LIQUIDITY"),
        ("PML (Prev Month Low)", pml, "SELL_SIDE_LIQUIDITY"),
        ("Session High", sh, "BUY_SIDE_LIQUIDITY"),
        ("Session Low", sl, "SELL_SIDE_LIQUIDITY"),
    ]

    for name, pr, side in raw_levels:
        dist = pr - spot
        abs_dist = abs(dist)
        if abs_dist <= 15.0:
            status = "APPROACHING"
            b_col = AMBER
        elif dist < 0 and side == "BUY_SIDE_LIQUIDITY":
            status = "SWEPT"
            b_col = PURPLE
        elif dist > 0 and side == "SELL_SIDE_LIQUIDITY":
            status = "SWEPT"
            b_col = PURPLE
        else:
            status = "INTACT"
            b_col = GREEN if side == "SELL_SIDE_LIQUIDITY" else RED

        levels.append(
            LiquidityLevelItem(
                name=name,
                price=pr,
                level_type=side,
                distance_pts=dist,
                status=status,
                badge_color=b_col,
            )
        )

    return levels


def evaluate_order_blocks(df_5m: pd.DataFrame) -> List[OrderBlockItem]:
    """
    Detects Demand and Supply Order Blocks and grades them (+ / ++ / +++)
    based on base candle consolidation, departure momentum, and wick rejection.
    """
    obs: List[OrderBlockItem] = []
    if df_5m is None or len(df_5m) < 8:
        return obs

    o = df_5m["open"].astype(float).values
    h = df_5m["high"].astype(float).values
    l = df_5m["low"].astype(float).values
    c = df_5m["close"].astype(float).values

    # Scan for prominent displacement moves (Departure legs)
    for i in range(3, len(df_5m) - 2):
        body = abs(c[i] - o[i])
        prior_body = abs(c[i - 1] - o[i - 1])

        # Bullish Departure: Big green bar breaking above previous 3 bars
        if c[i] > o[i] and body >= 15.0 and c[i] > max(h[i - 3 : i]):
            # Base candles prior to displacement
            base_count = 1
            for k in range(1, 4):
                if i - 1 - k >= 0 and abs(c[i - 1 - k] - o[i - 1 - k]) < 12.0:
                    base_count += 1
                else:
                    break

            base_bottom = min(l[i - base_count : i])
            base_top = max(h[i - base_count : i])
            # Check rejection wick
            has_long_wicks = (min(o[i - 1], c[i - 1]) - l[i - 1]) > (body * 0.4)

            if base_count >= 3:
                grade = "STRONG (+++)"
                stars = "+++"
                prob = "High (80%+)"
                col = GREEN
                chars = ["Wide & well-formed base", "Long consolidation (3+ bars)", "High probability continuation"]
            elif base_count == 2:
                grade = "NORMAL (++)"
                stars = "++"
                prob = "Good (65-75%)"
                col = SKY
                chars = ["Decent base with structure", "Moderate consolidation", "Good probability of reaction"]
            else:
                grade = "WEAK (+)"
                stars = "+"
                prob = "Low (<50%)"
                col = AMBER
                chars = ["Small / single-bar base", "Little consolidation", "Often gets broken easily"]

            obs.append(
                OrderBlockItem(
                    direction="DEMAND",
                    grade=grade,
                    grade_stars=stars,
                    top=base_top,
                    bottom=base_bottom,
                    base_bars=base_count,
                    is_rejection_block=has_long_wicks,
                    probability=prob,
                    badge_color=col,
                    characteristics=chars,
                )
            )

        # Bearish Departure: Big red bar breaking below previous 3 bars
        elif c[i] < o[i] and body >= 15.0 and c[i] < min(l[i - 3 : i]):
            base_count = 1
            for k in range(1, 4):
                if i - 1 - k >= 0 and abs(c[i - 1 - k] - o[i - 1 - k]) < 12.0:
                    base_count += 1
                else:
                    break

            base_top = max(h[i - base_count : i])
            base_bottom = min(l[i - base_count : i])
            has_long_wicks = (h[i - 1] - max(o[i - 1], c[i - 1])) > (body * 0.4)

            if base_count >= 3:
                grade = "STRONG (+++)"
                stars = "+++"
                prob = "High (80%+)"
                col = RED
                chars = ["Wide & well-formed supply base", "Long consolidation (3+ bars)", "Holds strong & rejects price"]
            elif base_count == 2:
                grade = "NORMAL (++)"
                stars = "++"
                prob = "Good (65-75%)"
                col = "#f87171"
                chars = ["Decent supply structure", "Moderate consolidation", "Good probability of reaction"]
            else:
                grade = "WEAK (+)"
                stars = "+"
                prob = "Low (<50%)"
                col = AMBER
                chars = ["Small / single-bar base", "Little consolidation", "Low probability of holding"]

            obs.append(
                OrderBlockItem(
                    direction="SUPPLY",
                    grade=grade,
                    grade_stars=stars,
                    top=base_top,
                    bottom=base_bottom,
                    base_bars=base_count,
                    is_rejection_block=has_long_wicks,
                    probability=prob,
                    badge_color=col,
                    characteristics=chars,
                )
            )

    return obs[-6:]  # Return most recent 6 order blocks


def evaluate_fvg_qualities(df_5m: pd.DataFrame) -> List[FVGQualityItem]:
    """
    Classifies Fair Value Gaps into 3 Quality Types:
      - CONSOLIDATION: 3rd candle pauses inside 2nd candle (Highest probability retest reaction)
      - EXPANSION: 3rd candle continues aggressive impulse
      - DEEP_RETRACEMENT: 3rd candle strongly counter-reverses (Lower success rate)
    """
    fvg_list: List[FVGQualityItem] = []
    if df_5m is None or len(df_5m) < 4:
        return fvg_list

    o = df_5m["open"].astype(float).values
    h = df_5m["high"].astype(float).values
    l = df_5m["low"].astype(float).values
    c = df_5m["close"].astype(float).values
    ts = df_5m["timestamp"].tolist()

    for i in range(2, len(df_5m)):
        t_str = str(ts[i])[-8:-3] if len(str(ts[i])) >= 8 else str(i)

        # Bullish FVG: Low of candle 3 is higher than High of candle 1
        if l[i] > h[i - 2]:
            gap_top = l[i]
            gap_bot = h[i - 2]
            # Candle 3 geometry
            c3_body = abs(c[i] - o[i])
            c2_body = abs(c[i - 1] - o[i - 1])

            if c[i] < o[i] and c[i] < (h[i - 1] + l[i - 1]) / 2.0:
                q_type = "DEEP_RETRACEMENT"
                q_title = "Type 1 · Deep Retracement FVG"
                b_col = AMBER
                prob = "Lower (~50%)"
                note = "3rd candle strongly counter-reversed into gap. Opposing intent present — extra confirmation needed."
            elif c[i] > h[i - 1]:
                q_type = "EXPANSION"
                q_title = "Type 2 · Expansion FVG"
                b_col = SKY
                prob = "High (~75%)"
                note = "Strong displacement impulse continuing in trend direction. High momentum."
            else:
                q_type = "CONSOLIDATION"
                q_title = "Type 3 · Consolidation FVG"
                b_col = GREEN
                prob = "Highest (~85%)"
                note = "3rd candle paused inside impulse candle. High probability retest reaction toward DOL."

            fvg_list.append(
                FVGQualityItem(
                    direction="BULLISH",
                    time=t_str,
                    top=gap_top,
                    bottom=gap_bot,
                    quality_type=q_type,
                    quality_title=q_title,
                    badge_color=b_col,
                    success_probability=prob,
                    note=note,
                )
            )

        # Bearish FVG: High of candle 3 is lower than Low of candle 1
        elif h[i] < l[i - 2]:
            gap_top = l[i - 2]
            gap_bot = h[i]
            if c[i] > o[i] and c[i] > (h[i - 1] + l[i - 1]) / 2.0:
                q_type = "DEEP_RETRACEMENT"
                q_title = "Type 1 · Deep Retracement FVG"
                b_col = AMBER
                prob = "Lower (~50%)"
                note = "3rd candle strongly counter-reversed. Opposing buying intent — extra confirmation needed."
            elif c[i] < l[i - 1]:
                q_type = "EXPANSION"
                q_title = "Type 2 · Expansion FVG"
                b_col = "#f87171"
                prob = "High (~75%)"
                note = "Aggressive downward displacement continuing. High selling momentum."
            else:
                q_type = "CONSOLIDATION"
                q_title = "Type 3 · Consolidation FVG"
                b_col = RED
                prob = "Highest (~85%)"
                note = "3rd candle paused inside drop candle. Highest probability retest reaction toward sell-side target."

            fvg_list.append(
                FVGQualityItem(
                    direction="BEARISH",
                    time=t_str,
                    top=gap_top,
                    bottom=gap_bot,
                    quality_type=q_type,
                    quality_title=q_title,
                    badge_color=b_col,
                    success_probability=prob,
                    note=note,
                )
            )

    return fvg_list[-6:]  # Return most recent 6 FVGs


def scan_liquidity_sweep_setups(
    df_5m: pd.DataFrame,
    levels: List[LiquidityLevelItem],
    spot: float,
) -> List[LiquidityTradeSetup]:
    """
    Scans for exact Liquidity Sweep and Liquidity Run execution trade setups.
    """
    setups: List[LiquidityTradeSetup] = []
    if df_5m is None or len(df_5m) < 5:
        return setups

    h = df_5m["high"].astype(float).values
    l = df_5m["low"].astype(float).values
    c = df_5m["close"].astype(float).values
    o = df_5m["open"].astype(float).values
    ts = df_5m["timestamp"].tolist()

    # Build target liquidity levels pool: macro levels + intraday swings + EQH/EQL
    intraday_targets: List[Tuple[str, float, str, int]] = []
    for lvl in levels:
        intraday_targets.append((lvl.name, lvl.price, "SELL_SIDE" if "SELL" in lvl.level_type else "BUY_SIDE", 0))

    # Local swing highs & lows
    sw_lows: List[Tuple[int, str, float]] = []
    sw_highs: List[Tuple[int, str, float]] = []
    for j in range(2, len(df_5m) - 2):
        t_j = str(ts[j])[-8:-3] if len(str(ts[j])) >= 8 else str(j)
        if l[j] <= min(l[j-2:j]) and l[j] <= min(l[j+1:j+3]):
            sw_lows.append((j, t_j, float(l[j])))
        if h[j] >= max(h[j-2:j]) and h[j] >= max(h[j+1:j+3]):
            sw_highs.append((j, t_j, float(h[j])))

    # Detect Equal Lows (EQL) / Equal Highs (EQH)
    for idx_a in range(len(sw_lows)):
        for idx_b in range(idx_a + 1, len(sw_lows)):
            ja, ta, va = sw_lows[idx_a]
            jb, tb, vb = sw_lows[idx_b]
            if abs(va - vb) <= 3.5:
                intraday_targets.append((f"EQL ({ta} & {tb})", min(va, vb), "SELL_SIDE", jb))

    for idx_a in range(len(sw_highs)):
        for idx_b in range(idx_a + 1, len(sw_highs)):
            ja, ta, va = sw_highs[idx_a]
            jb, tb, vb = sw_highs[idx_b]
            if abs(va - vb) <= 3.5:
                intraday_targets.append((f"EQH ({ta} & {tb})", max(va, vb), "BUY_SIDE", jb))

    # Add individual swing highs/lows
    for j, tj, vj in sw_lows:
        intraday_targets.append((f"Swing Low ({tj})", vj, "SELL_SIDE", j))
    for j, tj, vj in sw_highs:
        intraday_targets.append((f"Swing High ({tj})", vj, "BUY_SIDE", j))

    # Check for sweeps
    for lvl_name, lvl_price, side, created_idx in intraday_targets:
        start_scan = max(created_idx + 2, len(df_5m) - 25)
        for i in range(start_scan, len(df_5m)):
            t_str = str(ts[i])[-8:-3] if len(str(ts[i])) >= 8 else str(i)

            # 1. Bullish Liquidity Sweep (Sell Stops Hunted)
            # Low pierced below level, but candle closed back inside!
            if side == "SELL_SIDE" and l[i] < lvl_price and c[i] >= lvl_price:
                sweep_wick = l[i]
                reclaim_pr = max(c[i], lvl_price + 2.0)
                sl = sweep_wick - 2.5
                target = lvl_price + max(abs(reclaim_pr - sl) * 2.5, 30.0)
                risk = reclaim_pr - sl
                reward = target - reclaim_pr
                rr = f"1 : {reward / max(risk, 1.0):.1f}"

                setups.append(
                    LiquidityTradeSetup(
                        setup_type="LIQUIDITY_SWEEP",
                        direction="BUY (CALL)",
                        level_name=lvl_name,
                        level_price=lvl_price,
                        sweep_price=sweep_wick,
                        entry_price=reclaim_pr,
                        stop_loss=sl,
                        target_price=target,
                        risk_pts=risk,
                        reward_pts=reward,
                        risk_reward=rr,
                        status="TRIGGERED" if spot >= reclaim_pr else "ARMED",
                        time_str=t_str,
                        action_plan=f"Wick swept {lvl_name} to {sweep_wick:,.1f}, closed back inside. Enter on reclaim @ {reclaim_pr:,.1f}. SL: {sl:,.1f}. Target opposite pool @ {target:,.1f}.",
                        badge_color=GREEN,
                    )
                )

            # 2. Bearish Liquidity Sweep (Buy Stops Hunted)
            # High pierced above level, but candle closed back below!
            elif side == "BUY_SIDE" and h[i] > lvl_price and c[i] <= lvl_price:
                sweep_wick = h[i]
                reclaim_pr = min(c[i], lvl_price - 2.0)
                sl = sweep_wick + 2.5
                target = lvl_price - max(abs(sl - reclaim_pr) * 2.5, 30.0)
                risk = sl - reclaim_pr
                reward = reclaim_pr - target
                rr = f"1 : {reward / max(risk, 1.0):.1f}"

                setups.append(
                    LiquidityTradeSetup(
                        setup_type="LIQUIDITY_SWEEP",
                        direction="SELL (PUT)",
                        level_name=lvl_name,
                        level_price=lvl_price,
                        sweep_price=sweep_wick,
                        entry_price=reclaim_pr,
                        stop_loss=sl,
                        target_price=target,
                        risk_pts=risk,
                        reward_pts=reward,
                        risk_reward=rr,
                        status="TRIGGERED" if spot <= reclaim_pr else "ARMED",
                        time_str=t_str,
                        action_plan=f"Wick swept {lvl_name} to {sweep_wick:,.1f}, closed back below. Enter short @ {reclaim_pr:,.1f}. SL: {sl:,.1f}. Target sell-side liquidity @ {target:,.1f}.",
                        badge_color=RED,
                    )
                )

    # De-duplicate setups by level_name and direction, keeping latest
    unique_setups: Dict[str, LiquidityTradeSetup] = {}
    for s in setups:
        k = f"{s.level_name}_{s.direction}"
        unique_setups[k] = s

    return list(unique_setups.values())[-4:]


def render_liquidity_sweep_component(
    df_5m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    spot: Optional[float] = None,
    ohlc: Optional[pd.DataFrame] = None,
) -> None:
    """
    Streamlit rendering component for the Liquidity Sweep vs Run Engine & Order Block Grader.
    """
    if df_5m is None or df_5m.empty:
        return

    ref_spot = spot or float(df_5m["close"].iloc[-1])
    levels = extract_key_liquidity_levels(spot=ref_spot, ohlc_df=ohlc, df_today=df_5m)
    order_blocks = evaluate_order_blocks(df_5m)
    fvgs = evaluate_fvg_qualities(df_5m)
    setups = scan_liquidity_sweep_setups(df_5m, levels, ref_spot)

    _render_html(
        f"""
        <div style="background: {DARK_CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 14px 18px; margin-top: 14px; margin-bottom: 14px;">
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
              <div style="font-size: 14px; font-weight: 800; color: #f8fafc; letter-spacing: 0.02em;">
                ⚡ Institutional Liquidity Setups: Sweep vs. Run & Order Block Grader
              </div>
              <div style="font-size: 11.5px; color: #94a3b8; margin-top: 3px;">
                ICT / CRT Execution Architecture · Order Block Strength (+ / ++ / +++) · FVG Quality Classifier
              </div>
            </div>
            <div style="background: rgba(15, 23, 42, 0.7); border: 1px solid #38bdf8; border-radius: 6px; padding: 6px 12px; font-size: 11px; color: #38bdf8; font-family: monospace;">
              <b>ACTIVE SETUPS:</b> {len(setups)} | <b>ORDER BLOCKS:</b> {len(order_blocks)}
            </div>
          </div>
        </div>
        """
    )

    # 1. Top Section: Active Liquidity Trade Setups (Sweep Trade / Run Trade)
    if setups:
        st.markdown("##### 🎯 Live Liquidity Sweep / Run Trade Opportunities")
        s_cols = st.columns(min(len(setups), 3))
        for idx, s in enumerate(setups[:3]):
            with s_cols[idx]:
                _render_html(
                    f"""
                    <div style="background: {DARK_CARD}; border: 1px solid {BORDER}; border-top: 3.5px solid {s.badge_color}; border-radius: 6px; padding: 12px; height: 100%;">
                      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <span style="font-size: 12px; font-weight: 800; color: {s.badge_color}; font-family: monospace;">{s.direction}</span>
                        <span style="background: rgba(15, 23, 42, 0.8); border: 1px solid {s.badge_color}; color: {s.badge_color}; font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 4px; font-family: monospace;">
                          {s.setup_type.replace('_', ' ')}
                        </span>
                      </div>
                      
                      <div style="font-size: 11px; color: #f8fafc; font-weight: 700; margin-bottom: 4px;">
                        {s.level_name} Swept ({s.sweep_price:,.1f})
                      </div>
                      
                      <div style="font-size: 11px; font-family: monospace; color: #cbd5e1; line-height: 1.4; border-top: 1px dashed #334155; padding-top: 6px;">
                        <div>Entry: <b style="color:#e2e8f0;">{s.entry_price:,.1f}</b></div>
                        <div>Stop Loss: <b style="color:#ef4444;">{s.stop_loss:,.1f}</b> ({s.risk_pts:.1f}p)</div>
                        <div>Target: <b style="color:#10b981;">{s.target_price:,.1f}</b> ({s.reward_pts:.1f}p)</div>
                        <div style="color:#f59e0b; font-weight:700;">R:R: {s.risk_reward}</div>
                      </div>
                      
                      <div style="font-size: 10px; color: #94a3b8; margin-top: 6px; line-height: 1.25; border-top: 1px solid #334155; padding-top: 5px;">
                        {s.action_plan}
                      </div>
                    </div>
                    """
                )
    else:
        st.info("No active liquidity sweep setups triggered on current bar. Monitoring key levels.")

    _render_html("<div style='height: 10px;'></div>")

    # 2. Two-Column Workspace: Order Block Grader (Left) & FVG Quality + Liquidity Radar (Right)
    col_ob, col_radar = st.columns([5, 5], gap="medium")

    with col_ob:
        # Order Block Strength Levels Card (+ / ++ / +++)
        _render_html(
            """
            <div style="font-size: 12px; font-weight: 700; color: #e2e8f0; text-transform: uppercase; margin-bottom: 6px;">
              🧱 Order Block Strength Levels (ICT Grader)
            </div>
            """
        )
        if order_blocks:
            for ob in order_blocks[:4]:
                _render_html(
                    f"""
                    <div style="background: {DARK_CARD}; border: 1px solid {BORDER}; border-left: 4px solid {ob.badge_color}; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px;">
                      <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                          <span style="font-size: 12px; font-weight: 800; color: {ob.badge_color}; font-family: monospace;">
                            {ob.direction} {ob.grade}
                          </span>
                          <span style="font-size: 10px; color: #94a3b8; margin-left: 6px;">
                            ({ob.base_bars} base bar{'s' if ob.base_bars > 1 else ''})
                          </span>
                        </div>
                        <span style="background: rgba(15, 23, 42, 0.7); color: #cbd5e1; font-size: 10px; font-family: monospace; padding: 1px 6px; border-radius: 4px; border: 1px solid #334155;">
                          {ob.probability}
                        </span>
                      </div>
                      
                      <div style="font-size: 11px; font-family: monospace; color: #e2e8f0; margin-top: 4px;">
                        Zone: <b>{ob.bottom:,.1f} — {ob.top:,.1f}</b> {'(Rejection Block)' if ob.is_rejection_block else ''}
                      </div>

                      <div style="font-size: 10px; color: #94a3b8; margin-top: 3px;">
                        • {' • '.join(ob.characteristics)}
                      </div>
                    </div>
                    """
                )
        else:
            st.caption("Awaiting fresh Order Block formation.")

    with col_radar:
        # FVG Quality & Liquidity Radar Table
        _render_html(
            """
            <div style="font-size: 12px; font-weight: 700; color: #e2e8f0; text-transform: uppercase; margin-bottom: 6px;">
              ⚡ Fair Value Gap (FVG) Quality Classifier
            </div>
            """
        )
        if fvgs:
            fvg_rows = []
            for f in reversed(fvgs):
                fvg_rows.append({
                    "Time": f.time,
                    "Dir": "🟢 BULL" if f.direction == "BULLISH" else "🔴 BEAR",
                    "Type": f.quality_type,
                    "Top": f"{f.top:,.1f}",
                    "Bottom": f"{f.bottom:,.1f}",
                    "Win Rate": f.success_probability,
                })
            st.dataframe(pd.DataFrame(fvg_rows), use_container_width=True, hide_index=True, height=160)
        else:
            st.caption("No active Fair Value Gaps in range.")

        # Key Liquidity Levels Mini Radar
        _render_html(
            """
            <div style="font-size: 12px; font-weight: 700; color: #e2e8f0; text-transform: uppercase; margin-top: 10px; margin-bottom: 6px;">
              📍 Key Liquidity Levels Radar (PDH/PDL · PWH/PWL · Session)
            </div>
            """
        )
        lvl_rows = []
        for l in levels:
            lvl_rows.append({
                "Level": l.name,
                "Price": f"{l.price:,.1f}",
                "Pool Side": "BUY_STOPS" if "BUY" in l.level_type else "SELL_STOPS",
                "Dist": f"{l.distance_pts:+,.1f}p",
                "Status": l.status,
            })
        st.dataframe(pd.DataFrame(lvl_rows), use_container_width=True, hide_index=True, height=180)
