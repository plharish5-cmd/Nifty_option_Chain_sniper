"""
pa_basics.py — Price Action Basics Engine (Pure Python, Zero Streamlit Imports).

CONCEPTS IMPLEMENTED:
─────────────────────
1. Diagonal Trendlines (fitted on close line-view at confirmed swing pivots from smc.py).
   Encodes the critical warning: 1st break is BROKEN — UNCONFIRMED. Two closes required.
   Reversals within 3 bars tagged FAKE BREAK.
2. Wilder's 14-period RSI with the Crossing Rule.
   Overbought = crossed BACK DOWN from 70. Oversold = crossed BACK UP from 30.
   Never reported as a naked buy or sell.
3. ATR-based Stop Distance at 1.5x and 2.0x ATR.
   Shown alongside the existing 40-pt fixed stop, highlighting volatility mismatch.
4. Multi-candle Chart Patterns on confirmed pivots:
   Double Bottom (W), Double Top (M), Head & Shoulders, Inverse H&S, Triple Top/Bottom.
   Two consecutive closes required for confirmation. Framed as multi-bar S/R.

WHAT THIS MODULE CANNOT DO:
───────────────────────────
- It CANNOT predict the future or generate buy/sell signals.
- It CANNOT eliminate the fractal pivot confirmation lag (N bars). Attempting to do so
  introduces lookahead bias that ruins live trading results.
- It CANNOT confirm a break or pattern on a single candle. Two closes are strictly required.
- It CANNOT produce trade setups when structure is SIDEWAYS (sideways is a strict no-trade state).
- It CANNOT feed the decision bar, alerts, or Strategy 1. It is an isolated chart-reading aid.
- It CANNOT output win rates, probabilities, or 'high probability' claims.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
import pandas as pd

import smc
try:
    import strategy1
except ImportError:
    strategy1 = None


# ═══════════════════════════════════════════════════════════════════════════
# Configuration & Rule Loader
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_RULES_PATH = Path(__file__).parent / "pa_basics_rules.json"


def load_rules(rules_path: Optional[Path] = None) -> dict:
    """Loads rules from JSON with strict fallback defaults."""
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "pivot_window_n": 2,
        "use_close_for_trendline": True,
        "trendline_touch_tolerance_pts": 8.0,
        "trendline_fake_break_max_bars": 3,
        "rsi_period": 14,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "rsi_neutral_mid": 50.0,
        "rsi_cross_lookback_bars": 3,
        "atr_period": 14,
        "atr_stop_multiplier_1": 1.5,
        "atr_stop_multiplier_2": 2.0,
        "fixed_15m_stop_pts": 40.0,
        "lot_size": 65,
        "pattern_equal_tolerance_pct": 0.18,
        "zone_half_width_pts": 6.0,
    }
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update({k: v for k, v in data.items() if not k.startswith("_")})
        except Exception:
            pass
    return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Diagonal Trendlines Dataclass & Logic
# ═══════════════════════════════════════════════════════════════════════════

TrendlineBreakState = Literal[
    "INTACT",
    "BROKEN — UNCONFIRMED",
    "CONFIRMED BREAK",
    "FAKE BREAK",
    "INSUFFICIENT_PIVOTS"
]

@dataclass
class Trendline:
    kind: Literal["SUPPORT", "RESISTANCE"]
    anchor1_idx: int
    anchor1_price: float
    anchor1_time: datetime
    anchor2_idx: int
    anchor2_price: float
    anchor2_time: datetime
    slope_pts_per_bar: float
    projected_current_level: float
    touch_count: int
    distance_from_spot: float
    break_state: TrendlineBreakState
    break_detail: str
    warning_note: str

    @property
    def direction_label(self) -> str:
        if self.slope_pts_per_bar > 0.05:
            return "ASCENDING"
        elif self.slope_pts_per_bar < -0.05:
            return "DESCENDING"
        return "FLAT"


def detect_trendlines(
    df: pd.DataFrame,
    pivots_list: list[smc.Pivot],
    spot: float,
    rules: dict
) -> dict[str, Optional[Trendline]]:
    """
    Fits diagonal trendlines on the line chart view of closes at confirmed pivots.
    Evaluates Uptrend Support (from swing lows) and Downtrend Resistance (from swing highs).
    """
    out: dict[str, Optional[Trendline]] = {"support": None, "resistance": None}
    if df is None or len(df) < 5 or not pivots_list:
        return out

    curr_idx = len(df) - 1
    use_close = rules.get("use_close_for_trendline", True)
    touch_tol = float(rules.get("trendline_touch_tolerance_pts", 8.0))
    fake_break_max = int(rules.get("trendline_fake_break_max_bars", 3))

    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index).tolist()

    # Confirmed lows for support line
    low_pivots = [p for p in pivots_list if p.kind == "LOW" and p.known_at(curr_idx)]
    # Confirmed highs for resistance line
    high_pivots = [p for p in pivots_list if p.kind == "HIGH" and p.known_at(curr_idx)]

    # 1. Support Line (Uptrend Support Line from swing lows)
    if len(low_pivots) >= 2:
        p1, p2 = low_pivots[-2], low_pivots[-1]
        y1 = float(c[p1.i]) if use_close else p1.price
        y2 = float(c[p2.i]) if use_close else p2.price
        dx = p2.i - p1.i
        if dx > 0:
            slope = (y2 - y1) / dx
            proj_level = y2 + slope * (curr_idx - p2.i)

            # Count touches between p1 and curr_idx
            touches = 0
            for bar_i in range(p1.i, curr_idx + 1):
                line_val = y1 + slope * (bar_i - p1.i)
                # Touch if low or close is within touch_tol of the line
                if abs(l[bar_i] - line_val) <= touch_tol or abs(c[bar_i] - line_val) <= touch_tol:
                    touches += 1
            touches = max(touches, 2)  # Anchor points are at least 2 touches

            # Evaluate Break State with two-close rule & fake break trap detection
            # Check closes starting after p2.i
            closes_below = []
            for bar_i in range(p2.i + 1, curr_idx + 1):
                line_val = y1 + slope * (bar_i - p1.i)
                closes_below.append(c[bar_i] < line_val)

            break_state: TrendlineBreakState = "INTACT"
            break_detail = "Price holding above support line."
            warning = "Standard uptrend support."

            if closes_below:
                # Was there a break recently?
                # Check for fake break: broke below, then reversed back above within fake_break_max bars
                is_currently_below = closes_below[-1]
                was_prev_below = closes_below[-2] if len(closes_below) >= 2 else False

                if is_currently_below:
                    if was_prev_below:
                        break_state = "CONFIRMED BREAK"
                        break_detail = "Two consecutive closes below support line."
                        warning = "Break confirmed by 2 closes. Watch for breakdown continuation or retest."
                    else:
                        break_state = "BROKEN — UNCONFIRMED"
                        break_detail = "1st close below support line. Awaiting 2nd close confirmation."
                        warning = "TRAP WARNING: 1st break is frequently a liquidity trap. Do NOT trade on 1 close."
                else:
                    # Currently back above. Did it break below in the last fake_break_max bars?
                    recent_window = closes_below[-fake_break_max:]
                    if any(recent_window):
                        break_state = "FAKE BREAK"
                        break_detail = f"Price broke below but reversed back above within {fake_break_max} bars."
                        warning = "BEAR TRAP CONFIRMED: Trapped shorts being squeezed back through line."
                    else:
                        break_state = "INTACT"

            out["support"] = Trendline(
                kind="SUPPORT",
                anchor1_idx=p1.i,
                anchor1_price=y1,
                anchor1_time=ts[p1.i],
                anchor2_idx=p2.i,
                anchor2_price=y2,
                anchor2_time=ts[p2.i],
                slope_pts_per_bar=round(slope, 3),
                projected_current_level=round(proj_level, 2),
                touch_count=touches,
                distance_from_spot=round(spot - proj_level, 2),
                break_state=break_state,
                break_detail=break_detail,
                warning_note=warning,
            )

    # 2. Resistance Line (Downtrend Resistance Line from swing highs)
    if len(high_pivots) >= 2:
        p1, p2 = high_pivots[-2], high_pivots[-1]
        y1 = float(c[p1.i]) if use_close else p1.price
        y2 = float(c[p2.i]) if use_close else p2.price
        dx = p2.i - p1.i
        if dx > 0:
            slope = (y2 - y1) / dx
            proj_level = y2 + slope * (curr_idx - p2.i)

            touches = 0
            for bar_i in range(p1.i, curr_idx + 1):
                line_val = y1 + slope * (bar_i - p1.i)
                if abs(h[bar_i] - line_val) <= touch_tol or abs(c[bar_i] - line_val) <= touch_tol:
                    touches += 1
            touches = max(touches, 2)

            closes_above = []
            for bar_i in range(p2.i + 1, curr_idx + 1):
                line_val = y1 + slope * (bar_i - p1.i)
                closes_above.append(c[bar_i] > line_val)

            break_state = "INTACT"
            break_detail = "Price holding below resistance line."
            warning = "Standard downtrend resistance."

            if closes_above:
                is_currently_above = closes_above[-1]
                was_prev_above = closes_above[-2] if len(closes_above) >= 2 else False

                if is_currently_above:
                    if was_prev_above:
                        break_state = "CONFIRMED BREAK"
                        break_detail = "Two consecutive closes above resistance line."
                        warning = "Break confirmed by 2 closes. Watch for breakout continuation or retest."
                    else:
                        break_state = "BROKEN — UNCONFIRMED"
                        break_detail = "1st close above resistance line. Awaiting 2nd close confirmation."
                        warning = "TRAP WARNING: 1st break is frequently a liquidity trap. Do NOT trade on 1 close."
                else:
                    recent_window = closes_above[-fake_break_max:]
                    if any(recent_window):
                        break_state = "FAKE BREAK"
                        break_detail = f"Price broke above but reversed back below within {fake_break_max} bars."
                        warning = "BULL TRAP CONFIRMED: Trapped longs being dumped back through line."
                    else:
                        break_state = "INTACT"

            out["resistance"] = Trendline(
                kind="RESISTANCE",
                anchor1_idx=p1.i,
                anchor1_price=y1,
                anchor1_time=ts[p1.i],
                anchor2_idx=p2.i,
                anchor2_price=y2,
                anchor2_time=ts[p2.i],
                slope_pts_per_bar=round(slope, 3),
                projected_current_level=round(proj_level, 2),
                touch_count=touches,
                distance_from_spot=round(proj_level - spot, 2),
                break_state=break_state,
                break_detail=break_detail,
                warning_note=warning,
            )

    return out


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Wilder's 14-period RSI with the Crossing Rule
# ═══════════════════════════════════════════════════════════════════════════

RSIState = Literal[
    "ABOVE_70",
    "CROSSED_DOWN_FROM_70",
    "NEUTRAL",
    "BELOW_30",
    "CROSSED_UP_FROM_30",
    "INSUFFICIENT_DATA"
]

@dataclass
class RSIAnalysis:
    current_rsi: float
    state: RSIState
    bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    series: list[float]
    detail: str
    rule_reminder: str


def compute_wilder_rsi(
    df: pd.DataFrame,
    rules: dict
) -> RSIAnalysis:
    """
    Calculates Wilder's 14-period RSI from closes and applies the exact Crossing Rule.
    Overbought = crossed BACK DOWN from 70.
    Oversold = crossed BACK UP from 30.
    """
    period = int(rules.get("rsi_period", 14))
    lookback = int(rules.get("rsi_cross_lookback_bars", 3))
    ob_level = float(rules.get("rsi_overbought", 70.0))
    os_level = float(rules.get("rsi_oversold", 30.0))

    if df is None or len(df) < period + 2:
        return RSIAnalysis(
            current_rsi=50.0,
            state="INSUFFICIENT_DATA",
            bias="NEUTRAL",
            series=[],
            detail=f"Requires at least {period + 2} bars to compute Wilder's RSI.",
            rule_reminder="Wait for minimum bar history.",
        )

    closes = df["close"].astype(float).to_numpy()
    diff = np.diff(closes)

    gains = np.maximum(diff, 0.0)
    losses = np.maximum(-diff, 0.0)

    rsi_values = [50.0] * (period + 1)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    if avg_loss == 0:
        rsi_values.append(100.0)
    elif avg_gain == 0:
        rsi_values.append(0.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    for i in range(period, len(diff)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        elif avg_gain == 0:
            rsi_values.append(0.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    # Truncate to match length of df
    rsi_series = rsi_values[-len(df):]
    curr_rsi = float(rsi_series[-1])

    # Crossing rule evaluation
    # Look back over recent bars
    recent_rsi = rsi_series[-lookback - 1:] if len(rsi_series) >= lookback + 1 else rsi_series

    # Check crossing down from 70
    crossed_down_70 = False
    if curr_rsi < ob_level:
        for prev in recent_rsi[:-1]:
            if prev >= ob_level:
                crossed_down_70 = True
                break

    # Check crossing up from 30
    crossed_up_30 = False
    if curr_rsi > os_level:
        for prev in recent_rsi[:-1]:
            if prev <= os_level:
                crossed_up_30 = True
                break

    if crossed_down_70:
        state: RSIState = "CROSSED_DOWN_FROM_70"
        detail = f"RSI touched above {ob_level} and has now crossed back below to {curr_rsi:.1f}. Exhaustion confirmed."
        rule_reminder = "Overbought validated only on cross back down. Bearish exhaustion signal."
    elif crossed_up_30:
        state: RSIState = "CROSSED_UP_FROM_30"
        detail = f"RSI dropped below {os_level} and has now crossed back above to {curr_rsi:.1f}. Recovery confirmed."
        rule_reminder = "Oversold validated only on cross back up. Bullish recovery signal."
    elif curr_rsi > ob_level:
        state: RSIState = "ABOVE_70"
        detail = f"RSI currently {curr_rsi:.1f} (> {ob_level}). IN OVERBOUGHT TERRITORY."
        rule_reminder = "WARNING: RSI > 70 is NOT a sell signal! Strong momentum can stay overbought. Await cross down."
    elif curr_rsi < os_level:
        state: RSIState = "BELOW_30"
        detail = f"RSI currently {curr_rsi:.1f} (< {os_level}). IN OVERSOLD TERRITORY."
        rule_reminder = "WARNING: RSI < 30 is NOT a buy signal! Price can keep falling for a long time. Await cross up."
    else:
        state: RSIState = "NEUTRAL"
        detail = f"RSI {curr_rsi:.1f} inside normal 30–70 range."
        rule_reminder = "No crossing event. Follow structural trend."

    bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    if curr_rsi > 50.0:
        bias = "BULLISH"
    elif curr_rsi < 50.0:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return RSIAnalysis(
        current_rsi=round(curr_rsi, 1),
        state=state,
        bias=bias,
        series=[round(x, 1) for x in rsi_series],
        detail=detail,
        rule_reminder=rule_reminder,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3 · ATR-Based Stop Distance Dataclass & Logic
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ATRStopAnalysis:
    atr_points: float
    timeframe: str
    stop_1_5x_pts: float
    stop_2_0x_pts: float
    stop_1_5x_pct: float
    stop_2_0x_pct: float
    stop_1_5x_inr_lot: float
    stop_2_0x_inr_lot: float
    fixed_stop_pts: float
    fixed_stop_inr_lot: float
    volatility_vs_fixed_warning: str
    is_fixed_stop_too_tight: bool


def compute_atr_stops(
    df: pd.DataFrame,
    spot: float,
    timeframe: str,
    rules: dict
) -> ATRStopAnalysis:
    """
    Computes ATR stop distances at 1.5x and 2.0x, compares against fixed 40-pt stop.
    Never silently widens or replaces the fixed stop.
    """
    lot_size = int(rules.get("lot_size", 65))
    fixed_stop = float(rules.get("fixed_15m_stop_pts", 40.0))
    mult1 = float(rules.get("atr_stop_multiplier_1", 1.5))
    mult2 = float(rules.get("atr_stop_multiplier_2", 2.0))

    atr_val = 20.0
    if df is not None and not df.empty:
        if "atr" in df.columns and not df["atr"].dropna().empty:
            atr_val = float(df["atr"].dropna().iloc[-1])
        elif len(df) >= 15:
            # Wilder's 14 ATR
            h = df["high"].astype(float).to_numpy()
            l = df["low"].astype(float).to_numpy()
            c = df["close"].astype(float).to_numpy()
            tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
            atr_val = float(pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1])

    stop_1_5 = atr_val * mult1
    stop_2_0 = atr_val * mult2

    pct_1_5 = (stop_1_5 / spot * 100.0) if spot > 0 else 0.0
    pct_2_0 = (stop_2_0 / spot * 100.0) if spot > 0 else 0.0

    inr_1_5 = stop_1_5 * lot_size
    inr_2_0 = stop_2_0 * lot_size
    inr_fixed = fixed_stop * lot_size

    is_tight = stop_1_5 > fixed_stop
    if is_tight:
        warning = (
            f"VOLATILITY MISMATCH: 1.5× ATR stop ({stop_1_5:.1f} pts / ₹{inr_1_5:,.0f}) "
            f"exceeds the fixed 40-pt stop (₹{inr_fixed:,.0f}). The fixed stop is tighter "
            f"than current volatility suggests. Normal breathing room will cause premature stop-outs."
        )
    else:
        warning = (
            f"Fixed 40-pt stop (₹{inr_fixed:,.0f}) is wider than 1.5× ATR stop "
            f"({stop_1_5:.1f} pts / ₹{inr_1_5:,.0f}). Volatility is safely contained."
        )

    return ATRStopAnalysis(
        atr_points=round(atr_val, 2),
        timeframe=timeframe,
        stop_1_5x_pts=round(stop_1_5, 1),
        stop_2_0x_pts=round(stop_2_0, 1),
        stop_1_5x_pct=round(pct_1_5, 2),
        stop_2_0x_pct=round(pct_2_0, 2),
        stop_1_5x_inr_lot=round(inr_1_5, 0),
        stop_2_0x_inr_lot=round(inr_2_0, 0),
        fixed_stop_pts=round(fixed_stop, 1),
        fixed_stop_inr_lot=round(inr_fixed, 0),
        volatility_vs_fixed_warning=warning,
        is_fixed_stop_too_tight=is_tight,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Multi-Candle Chart Patterns Dataclass & Logic
# ═══════════════════════════════════════════════════════════════════════════

PatternStatus = Literal["FORMING", "CONFIRMED"]

@dataclass
class ChartPattern:
    name: str                           # DOUBLE BOTTOM (W) | DOUBLE TOP (M) | HEAD & SHOULDERS | etc.
    status: PatternStatus
    bias: Literal["BULLISH", "BEARISH"]
    level: float                        # Key structural reference level (support/resistance)
    neckline: float
    time: datetime
    bar_index: int
    underlying_sr_meaning: str          # e.g. "Support tested twice over 14 bars"
    confirmation_detail: str            # Notes two-close status
    confluent_concepts: list[str] = field(default_factory=list)
    conflicting_concepts: list[str] = field(default_factory=list)


def detect_chart_patterns(
    df: pd.DataFrame,
    pivots_list: list[smc.Pivot],
    rules: dict
) -> list[ChartPattern]:
    """
    Detects multi-bar chart patterns strictly on confirmed pivots:
    - Double Bottom (W), Double Top (M)
    - Head & Shoulders, Inverse Head & Shoulders
    - Triple Top / Triple Bottom
    Requires two consecutive closes beyond neckline for confirmation.
    Newest candle can never confirm (reported as FORMING).
    """
    patterns: list[ChartPattern] = []
    if df is None or len(df) < 8 or not pivots_list:
        return patterns

    curr_idx = len(df) - 1
    c = df["close"].astype(float).to_numpy()
    tol_pct = float(rules.get("pattern_equal_tolerance_pct", 0.18)) / 100.0

    # Only consider knowable pivots as of curr_idx
    knowable = [p for p in pivots_list if p.known_at(curr_idx)]
    if len(knowable) < 3:
        return patterns

    lows = [p for p in knowable if p.kind == "LOW"]
    highs = [p for p in knowable if p.kind == "HIGH"]

    # 1. Double Bottom (W) & Triple Bottom
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        # Check tolerance between two lows
        avg_low = (l1.price + l2.price) / 2.0
        if abs(l1.price - l2.price) / avg_low <= tol_pct:
            # Find intervening high between l1 and l2
            intervening = [p for p in highs if l1.i < p.i < l2.i]
            if intervening:
                peak = max(intervening, key=lambda p: p.price)
                neckline = peak.price

                # Confirmation requires two consecutive closes above neckline
                # Newest bar cannot confirm alone
                closes_above = [c[j] > neckline for j in range(l2.i + 1, curr_idx + 1)]
                confirmed = len(closes_above) >= 2 and closes_above[-1] and closes_above[-2]

                # Check if Triple Bottom (3 lows)
                is_triple = False
                if len(lows) >= 3:
                    l0 = lows[-3]
                    if abs(l0.price - avg_low) / avg_low <= tol_pct and l0.i < l1.i:
                        is_triple = True

                pat_name = "TRIPLE BOTTOM" if is_triple else "DOUBLE BOTTOM (W)"
                meaning = (
                    f"Support at {avg_low:,.1f} tested {3 if is_triple else 2} times. "
                    f"Institutional buyers absorbing supply at this horizontal zone."
                )

                status: PatternStatus = "CONFIRMED" if confirmed else "FORMING"
                conf_detail = (
                    "Two consecutive closes held above neckline."
                    if confirmed else
                    "Awaiting two consecutive closes above neckline (newest bar cannot confirm)."
                )

                patterns.append(ChartPattern(
                    name=pat_name,
                    status=status,
                    bias="BULLISH",
                    level=round(avg_low, 1),
                    neckline=round(neckline, 1),
                    time=l2.at,
                    bar_index=l2.i,
                    underlying_sr_meaning=meaning,
                    confirmation_detail=conf_detail,
                ))

    # 2. Double Top (M) & Triple Top
    if len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        avg_high = (h1.price + h2.price) / 2.0
        if abs(h1.price - h2.price) / avg_high <= tol_pct:
            intervening = [p for p in lows if h1.i < p.i < h2.i]
            if intervening:
                trough = min(intervening, key=lambda p: p.price)
                neckline = trough.price

                closes_below = [c[j] < neckline for j in range(h2.i + 1, curr_idx + 1)]
                confirmed = len(closes_below) >= 2 and closes_below[-1] and closes_below[-2]

                is_triple = False
                if len(highs) >= 3:
                    h0 = highs[-3]
                    if abs(h0.price - avg_high) / avg_high <= tol_pct and h0.i < h1.i:
                        is_triple = True

                pat_name = "TRIPLE TOP" if is_triple else "DOUBLE TOP (M)"
                meaning = (
                    f"Resistance at {avg_high:,.1f} tested {3 if is_triple else 2} times. "
                    f"Institutional sellers defending this horizontal supply zone."
                )

                status = "CONFIRMED" if confirmed else "FORMING"
                conf_detail = (
                    "Two consecutive closes held below neckline."
                    if confirmed else
                    "Awaiting two consecutive closes below neckline (newest bar cannot confirm)."
                )

                patterns.append(ChartPattern(
                    name=pat_name,
                    status=status,
                    bias="BEARISH",
                    level=round(avg_high, 1),
                    neckline=round(neckline, 1),
                    time=h2.at,
                    bar_index=h2.i,
                    underlying_sr_meaning=meaning,
                    confirmation_detail=conf_detail,
                ))

    # 3. Head and Shoulders (H&S)
    if len(highs) >= 3:
        h1, h2, h3 = highs[-3], highs[-2], highs[-1]
        # Middle must be highest: h2 > h1 and h2 > h3
        if h2.price > h1.price and h2.price > h3.price:
            avg_shoulder = (h1.price + h3.price) / 2.0
            # Outer two roughly equal
            if abs(h1.price - h3.price) / avg_shoulder <= (tol_pct * 1.5):
                # Intervening troughs
                troughs_1 = [p for p in lows if h1.i < p.i < h2.i]
                troughs_2 = [p for p in lows if h2.i < p.i < h3.i]
                if troughs_1 and troughs_2:
                    neckline = (troughs_1[0].price + troughs_2[0].price) / 2.0
                    closes_below = [c[j] < neckline for j in range(h3.i + 1, curr_idx + 1)]
                    confirmed = len(closes_below) >= 2 and closes_below[-1] and closes_below[-2]

                    patterns.append(ChartPattern(
                        name="HEAD & SHOULDERS",
                        status="CONFIRMED" if confirmed else "FORMING",
                        bias="BEARISH",
                        level=round(h2.price, 1),
                        neckline=round(neckline, 1),
                        time=h3.at,
                        bar_index=h3.i,
                        underlying_sr_meaning=(
                            f"Multi-bar distribution. Peak at {h2.price:,.1f}, shoulders at {avg_shoulder:,.1f}. "
                            f"Resistance zone defended three times with failing momentum on 3rd attempt."
                        ),
                        confirmation_detail=(
                            "Two consecutive closes held below neckline."
                            if confirmed else
                            "Awaiting two consecutive closes below neckline (newest bar cannot confirm)."
                        ),
                    ))

    # 4. Inverse Head and Shoulders
    if len(lows) >= 3:
        l1, l2, l3 = lows[-3], lows[-2], lows[-1]
        # Middle must be lowest: l2 < l1 and l2 < l3
        if l2.price < l1.price and l2.price < l3.price:
            avg_shoulder = (l1.price + l3.price) / 2.0
            if abs(l1.price - l3.price) / avg_shoulder <= (tol_pct * 1.5):
                peaks_1 = [p for p in highs if l1.i < p.i < l2.i]
                peaks_2 = [p for p in highs if l2.i < p.i < l3.i]
                if peaks_1 and peaks_2:
                    neckline = (peaks_1[0].price + peaks_2[0].price) / 2.0
                    closes_above = [c[j] > neckline for j in range(l3.i + 1, curr_idx + 1)]
                    confirmed = len(closes_above) >= 2 and closes_above[-1] and closes_above[-2]

                    patterns.append(ChartPattern(
                        name="INVERSE HEAD & SHOULDERS",
                        status="CONFIRMED" if confirmed else "FORMING",
                        bias="BULLISH",
                        level=round(l2.price, 1),
                        neckline=round(neckline, 1),
                        time=l3.at,
                        bar_index=l3.i,
                        underlying_sr_meaning=(
                            f"Multi-bar accumulation. Trough at {l2.price:,.1f}, shoulders at {avg_shoulder:,.1f}. "
                            f"Support zone tested three times with sellers exhausted on 3rd attempt."
                        ),
                        confirmation_detail=(
                            "Two consecutive closes held above neckline."
                            if confirmed else
                            "Awaiting two consecutive closes above neckline (newest bar cannot confirm)."
                        ),
                    ))

    # Sort newest first
    patterns.sort(key=lambda p: p.bar_index, reverse=True)
    return patterns


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Trend & Structure Derivation (Strict SMC Pivots, No EMA Angle)
# ═══════════════════════════════════════════════════════════════════════════

StructuralTrend = Literal["UPTREND", "DOWNTREND", "SIDEWAYS"]

@dataclass
class StructureState:
    trend: StructuralTrend
    last_high_subkind: str
    last_low_subkind: str
    is_sideways_no_trade: bool
    explanation: str


def derive_structure_trend(pivots_list: list[smc.Pivot], curr_idx: int) -> StructureState:
    """
    Derives trend strictly from confirmed structural pivots:
    - Uptrend: Higher Highs (HH) and Higher Lows (HL).
    - Downtrend: Lower Highs (LH) and Lower Lows (LL).
    - Equal/Mixed: Sideways (Strict NO-TRADE state for beginners).
    Never uses EMA angle or visual slope.
    """
    knowable = [p for p in pivots_list if p.known_at(curr_idx)]
    highs = [p for p in knowable if p.kind == "HIGH"]
    lows = [p for p in knowable if p.kind == "LOW"]

    if len(highs) < 2 or len(lows) < 2:
        return StructureState(
            trend="SIDEWAYS",
            last_high_subkind="UNKNOWN",
            last_low_subkind="UNKNOWN",
            is_sideways_no_trade=True,
            explanation="Fewer than two confirmed swing highs and lows. Structure is unformed/sideways.",
        )

    last_h, prev_h = highs[-1], highs[-2]
    last_l, prev_l = lows[-1], lows[-2]

    is_hh = last_h.price > prev_h.price
    is_hl = last_l.price > prev_l.price
    is_lh = last_h.price < prev_h.price
    is_ll = last_l.price < prev_l.price

    h_tag = "HH" if is_hh else ("LH" if is_lh else "EH")
    l_tag = "HL" if is_hl else ("LL" if is_ll else "EL")

    if is_hh and is_hl:
        return StructureState(
            trend="UPTREND",
            last_high_subkind=h_tag,
            last_low_subkind=l_tag,
            is_sideways_no_trade=False,
            explanation="Confirmed structure: Higher Highs (HH) and Higher Lows (HL). Bullish structural trend.",
        )
    elif is_lh and is_ll:
        return StructureState(
            trend="DOWNTREND",
            last_high_subkind=h_tag,
            last_low_subkind=l_tag,
            is_sideways_no_trade=False,
            explanation="Confirmed structure: Lower Highs (LH) and Lower Lows (LL). Bearish structural trend.",
        )
    else:
        return StructureState(
            trend="SIDEWAYS",
            last_high_subkind=h_tag,
            last_low_subkind=l_tag,
            is_sideways_no_trade=True,
            explanation=(
                f"Conflicting structure: {h_tag} with {l_tag}. Market is rangebound/sideways. "
                f"Strict NO-TRADE state per source rule."
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6 · Full Price Action Basics Snapshot
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PABasicsSnapshot:
    timeframe: str
    spot_price: float
    structure: StructureState
    rsi: RSIAnalysis
    atr_stop: ATRStopAnalysis
    trendlines: dict[str, Optional[Trendline]]
    patterns: list[ChartPattern]
    pivots: list[smc.Pivot]
    rules: dict
    candle_count: int
    data_guard_warning: Optional[str] = None
    # Imported existing techniques:
    obs: list[Any] = field(default_factory=list)
    fvgs: list[Any] = field(default_factory=list)
    candle_patterns: list[list[str]] = field(default_factory=list)
    structure_events: list[Any] = field(default_factory=list)


def evaluate_pa_basics(
    df: pd.DataFrame,
    spot: float,
    timeframe: str = "15m",
    rules_path: Optional[Path] = None,
    custom_rules: Optional[dict] = None
) -> PABasicsSnapshot:
    """
    Master evaluator running all four price action basics concepts.
    Guards defensively against:
      - df is None or fewer than 2N+3 bars
      - missing required columns
      - all-NaN volume
      - division by zero
      - pre-open with no candles
    """
    rules = load_rules(rules_path)
    if custom_rules:
        rules.update(custom_rules)

    n = int(rules.get("pivot_window_n", 2))
    min_required_bars = 2 * n + 3

    # Guard 1: Empty or Pre-Open
    if df is None or df.empty:
        return PABasicsSnapshot(
            timeframe=timeframe,
            spot_price=spot,
            structure=StructureState("SIDEWAYS", "NONE", "NONE", True, "Pre-open or no candles available."),
            rsi=RSIAnalysis(50.0, "INSUFFICIENT_DATA", "NEUTRAL", [], "No candle data.", ""),
            atr_stop=compute_atr_stops(None, spot, timeframe, rules),
            trendlines={"support": None, "resistance": None},
            patterns=[],
            pivots=[],
            rules=rules,
            candle_count=0,
            data_guard_warning="Pre-open session: No candle data available to compute price action basics.",
        )

    # Guard 2: Missing Essential Columns
    required_cols = {"open", "high", "low", "close"}
    missing = required_cols - set(df.columns)
    if missing:
        return PABasicsSnapshot(
            timeframe=timeframe,
            spot_price=spot,
            structure=StructureState("SIDEWAYS", "ERROR", "ERROR", True, f"Missing columns: {missing}"),
            rsi=RSIAnalysis(50.0, "INSUFFICIENT_DATA", "NEUTRAL", [], f"Missing columns {missing}", ""),
            atr_stop=compute_atr_stops(None, spot, timeframe, rules),
            trendlines={"support": None, "resistance": None},
            patterns=[],
            pivots=[],
            rules=rules,
            candle_count=len(df),
            data_guard_warning=f"Data schema error: Missing required candle columns {missing}.",
        )

    # Guard 3: Fewer than 2N+3 bars
    if len(df) < min_required_bars:
        return PABasicsSnapshot(
            timeframe=timeframe,
            spot_price=spot,
            structure=StructureState("SIDEWAYS", "INSUFFICIENT", "INSUFFICIENT", True, f"Only {len(df)} bars."),
            rsi=RSIAnalysis(50.0, "INSUFFICIENT_DATA", "NEUTRAL", [], "Insufficient bars.", ""),
            atr_stop=compute_atr_stops(df, spot, timeframe, rules),
            trendlines={"support": None, "resistance": None},
            patterns=[],
            pivots=[],
            rules=rules,
            candle_count=len(df),
            data_guard_warning=(
                f"Insufficient bar history: Received {len(df)} candles, but at least {min_required_bars} "
                f"bars are required for N={n} fractal pivots."
            ),
        )

    curr_idx = len(df) - 1

    # 1. Compute Confirmed Fractal Pivots using smc.pivots()
    pivots_list = smc.pivots(df, n=n)

    # 2. Derive Structural Trend
    struct = derive_structure_trend(pivots_list, curr_idx)

    # 3. Trendlines (Close-line view, Fake-break aware)
    tls = detect_trendlines(df, pivots_list, spot, rules)

    # 4. Wilder's 14 RSI with Crossing Rule
    rsi_res = compute_wilder_rsi(df, rules)

    # 5. ATR Stop Distance
    atr_res = compute_atr_stops(df, spot, timeframe, rules)

    # 6. Multi-Candle Chart Patterns
    pats = detect_chart_patterns(df, pivots_list, rules)

    # 7. Imported Existing Techniques (SMC Structure, Order Blocks, FVGs, Candlestick Patterns)
    smc_st = smc.structure(df, pivots_list)
    fvgs_list = smc.fvgs(df)
    obs_list = []
    try:
        obs_list = smc.order_blocks(df, smc_st, gaps=fvgs_list)
    except Exception:
        pass

    candle_pats = []
    if strategy1 is not None:
        try:
            candle_pats = strategy1.detect(df)
        except Exception:
            pass

    # 8. Multi-concept confluence enrichment on pattern cards
    for p in pats:
        agreed: list[str] = []
        conflicted: list[str] = []

        # A. Compare with structure
        if p.bias == "BULLISH":
            if struct.trend == "UPTREND":
                agreed.append("Structure (Higher Highs & Higher Lows)")
            elif struct.trend == "DOWNTREND":
                conflicted.append("Structure (Downtrend with Lower Lows)")
            else:
                conflicted.append("Structure (Sideways / No-Trade)")

            # B. Compare with RSI
            if rsi_res.state == "CROSSED_UP_FROM_30":
                agreed.append("RSI (Crossed Up from Oversold 30)")
            elif rsi_res.bias == "BULLISH":
                agreed.append("RSI (> 50 Bullish Bias)")
            else:
                conflicted.append("RSI (< 50 Bearish Bias)")

            # C. Compare with Support Trendline
            supp_tl = tls.get("support")
            if supp_tl and supp_tl.break_state == "INTACT":
                agreed.append("Support Trendline (Intact)")
            elif supp_tl and supp_tl.break_state == "FAKE BREAK":
                agreed.append("Support Trendline (Bear Trap / Fake Break Reversal)")

            # D. Compare with Candlestick Reversals (strategy1.py)
            if candle_pats and p.bar_index < len(candle_pats):
                # Check recent bars around pivot
                cand_slice = []
                for b_i in range(max(0, p.bar_index - 1), min(len(candle_pats), p.bar_index + 2)):
                    cand_slice.extend(candle_pats[b_i])
                bull_cands = [c for c in cand_slice if any(k in c for k in ("HAMMER", "BULLISH ENGULF", "MORNING STAR"))]
                if bull_cands:
                    agreed.append(f"Candlestick ({', '.join(set(bull_cands))})")

            # E. Compare with Order Blocks (smc.py)
            for ob in obs_list:
                if ob.open and (ob.low - 8.0 <= p.level <= ob.high + 8.0):
                    if ob.direction == "BULLISH":
                        agreed.append(f"SMC Bullish OB ({ob.low:,.0f}–{ob.high:,.0f})")
                    else:
                        conflicted.append(f"Opposing Bearish OB ({ob.low:,.0f}–{ob.high:,.0f})")

            # F. Compare with FVGs (smc.py)
            for fg in fvgs_list:
                if fg.open and (fg.bottom - 8.0 <= p.level <= fg.top + 8.0):
                    if fg.direction == "BULLISH":
                        agreed.append(f"SMC Bullish FVG ({fg.bottom:,.0f}–{fg.top:,.0f})")

        elif p.bias == "BEARISH":
            if struct.trend == "DOWNTREND":
                agreed.append("Structure (Lower Highs & Lower Lows)")
            elif struct.trend == "UPTREND":
                conflicted.append("Structure (Uptrend with Higher Highs)")
            else:
                conflicted.append("Structure (Sideways / No-Trade)")

            # B. Compare with RSI
            if rsi_res.state == "CROSSED_DOWN_FROM_70":
                agreed.append("RSI (Crossed Down from Overbought 70)")
            elif rsi_res.bias == "BEARISH":
                agreed.append("RSI (< 50 Bearish Bias)")
            else:
                conflicted.append("RSI (> 50 Bullish Bias)")

            # C. Compare with Resistance Trendline
            res_tl = tls.get("resistance")
            if res_tl and res_tl.break_state == "INTACT":
                agreed.append("Resistance Trendline (Intact)")
            elif res_tl and res_tl.break_state == "FAKE BREAK":
                agreed.append("Resistance Trendline (Bull Trap / Fake Break Reversal)")

            # D. Compare with Candlestick Reversals (strategy1.py)
            if candle_pats and p.bar_index < len(candle_pats):
                cand_slice = []
                for b_i in range(max(0, p.bar_index - 1), min(len(candle_pats), p.bar_index + 2)):
                    cand_slice.extend(candle_pats[b_i])
                bear_cands = [c for c in cand_slice if any(k in c for k in ("INVERTED HAMMER", "BEARISH ENGULF", "EVENING STAR"))]
                if bear_cands:
                    agreed.append(f"Candlestick ({', '.join(set(bear_cands))})")

            # E. Compare with Order Blocks (smc.py)
            for ob in obs_list:
                if ob.open and (ob.low - 8.0 <= p.level <= ob.high + 8.0):
                    if ob.direction == "BEARISH":
                        agreed.append(f"SMC Bearish OB ({ob.low:,.0f}–{ob.high:,.0f})")
                    else:
                        conflicted.append(f"Opposing Bullish OB ({ob.low:,.0f}–{ob.high:,.0f})")

            # F. Compare with FVGs (smc.py)
            for fg in fvgs_list:
                if fg.open and (fg.bottom - 8.0 <= p.level <= fg.top + 8.0):
                    if fg.direction == "BEARISH":
                        agreed.append(f"SMC Bearish FVG ({fg.bottom:,.0f}–{fg.top:,.0f})")

        p.confluent_concepts = agreed
        p.conflicting_concepts = conflicted

    return PABasicsSnapshot(
        timeframe=timeframe,
        spot_price=round(float(spot), 2),
        structure=struct,
        rsi=rsi_res,
        atr_stop=atr_res,
        trendlines=tls,
        patterns=pats,
        pivots=pivots_list,
        rules=rules,
        candle_count=len(df),
        data_guard_warning=None,
        obs=obs_list,
        fvgs=fvgs_list,
        candle_patterns=candle_pats,
        structure_events=smc_st.events,
    )
