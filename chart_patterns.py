"""
chart_patterns.py — V, A, W, M Chart Pattern Recognition Engine.

Supports:
  - 15-minute timeframe (strict institutional discipline)
  - 30-minute timeframe (macro structural clarity)

Patterns:
  1. V-Pattern (V-Bottom Reversal): Sharp selloff -> V-vertex -> sharp bounce.
  2. A-Pattern (A-Top / Inverted-V Reversal): Sharp rally -> A-vertex -> sharp drop.
  3. W-Pattern (Double Bottom): T1 -> Neckline peak -> T2 (Hammer/Doji) -> Neckline breakout.
  4. M-Pattern (Double Top): P1 -> Neckline trough -> P2 (Doji/Inv Hammer) -> Neckline breakdown.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import config

GREEN = "#2FBF71"
RED = "#E8503A"
SLATE = "#8FA6B8"
GOLD = "#C9A227"
CYAN = "#00D2D3"
BLUE = "#4A9BD1"
PURPLE = "#9B59B6"

# Load thresholds from rules.json if present
_RULES_PATH = Path(__file__).parent / "rules.json"

DEFAULT_PATTERN_PARAMS = {
    "swing_lookback": 2,
    "double_extremum_tol_pct": 0.0035,
    "min_pattern_depth_pts": 25.0,
    "v_drop_min_pts": 35.0,
    "v_rebound_min_ratio": 0.50,
    "a_rise_min_pts": 35.0,
    "a_drop_min_ratio": 0.50,
    "doji_body_pct": 0.12,
    "hammer_wick_ratio": 2.0,
    "hammer_opp_ratio": 0.6,
    "inverted_hammer_wick_ratio": 2.0,
    "inverted_hammer_opp_ratio": 0.6,
}


def load_pattern_params() -> dict:
    params = dict(DEFAULT_PATTERN_PARAMS)
    try:
        if _RULES_PATH.exists():
            data = json.loads(_RULES_PATH.read_text())
            block = data.get("chart_patterns", {})
            for k, v in block.items():
                if not k.startswith("_"):
                    params[k] = v
    except Exception:
        pass
    return params


@dataclass
class PatternPoint:
    index: int
    time: pd.Timestamp
    price: float
    label: str


@dataclass
class ChartPattern:
    pattern_type: str                   # "V", "A", "W", "M"
    name: str                           # e.g. "M-Pattern Top (Double Top)"
    timeframe: str                      # "15m" or "30m"
    direction: str                      # "BULLISH" or "BEARISH"
    status: str                         # "FORMING", "ARMED", "CONFIRMED_BREAKOUT", "TARGET_HIT", "INVALIDATED"
    badge_color: str
    points: list[PatternPoint]
    neckline: float
    stop_loss: float
    entry_trigger: float                # Conservative neckline trigger
    early_trigger: float | None         # Early reversal candle trigger (Mentor rule)
    target_1: float
    target_2: float
    risk_pts: float
    reward_pts: float
    rr_ratio: float
    confirmation_candle: str | None     # e.g. "Doji at Peak 2"
    description: str
    mentor_rule: str
    created_at: pd.Timestamp


def _is_doji_row(row: pd.Series, p: dict) -> bool:
    rng = max(float(row["high"]) - float(row["low"]), 0.001)
    body = abs(float(row["close"]) - float(row["open"]))
    return (body / rng) <= p["doji_body_pct"]


def _is_hammer_row(row: pd.Series, p: dict) -> bool:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = max(abs(c - o), 0.001)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return (lower >= p["hammer_wick_ratio"] * body) and (upper <= p["hammer_opp_ratio"] * body)


def _is_inverted_hammer_row(row: pd.Series, p: dict) -> bool:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = max(abs(c - o), 0.001)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return (upper >= p["inverted_hammer_wick_ratio"] * body) and (lower <= p["inverted_hammer_opp_ratio"] * body)


# ═══════════════════════════════════════════════════════════════════════════
# Swing Extrema Finder
# ═══════════════════════════════════════════════════════════════════════════

def find_swings(df: pd.DataFrame, lookback: int = 2) -> tuple[list[tuple[int, pd.Timestamp, float]], list[tuple[int, pd.Timestamp, float]]]:
    """
    Finds swing highs (peaks) and swing lows (troughs) using a symmetric window.
    Returns: (peaks, troughs) as lists of (index, timestamp, price).
    """
    if len(df) < lookback * 2 + 1:
        return [], []

    highs = df["high"].values
    lows = df["low"].values
    times = df["timestamp"].values if "timestamp" in df.columns else df.index.values

    peaks = []
    troughs = []

    for i in range(lookback, len(df) - lookback):
        # Check peak
        h_val = highs[i]
        if all(h_val >= highs[i - k] for k in range(1, lookback + 1)) and \
           all(h_val >= highs[i + k] for k in range(1, lookback + 1)):
            peaks.append((i, pd.to_datetime(times[i]), float(h_val)))

        # Check trough
        l_val = lows[i]
        if all(l_val <= lows[i - k] for k in range(1, lookback + 1)) and \
           all(l_val <= lows[i + k] for k in range(1, lookback + 1)):
            troughs.append((i, pd.to_datetime(times[i]), float(l_val)))

    return peaks, troughs


# ═══════════════════════════════════════════════════════════════════════════
# Pattern Detectors
# ═══════════════════════════════════════════════════════════════════════════

def detect_m_patterns(df: pd.DataFrame, peaks: list, troughs: list, timeframe: str, params: dict) -> list[ChartPattern]:
    """Detects M-Pattern / Double Top formations."""
    patterns = []
    current_close = float(df["close"].iloc[-1]) if len(df) else 0.0

    for i in range(len(peaks) - 1):
        p1_idx, p1_time, p1_high = peaks[i]
        for j in range(i + 1, len(peaks)):
            p2_idx, p2_time, p2_high = peaks[j]
            # Must have reasonable bar separation (2 to 24 bars)
            if not (2 <= (p2_idx - p1_idx) <= 24):
                continue

            # Check peak price tolerance (within 0.35%)
            if abs(p1_high - p2_high) / max(p1_high, 0.01) > params["double_extremum_tol_pct"]:
                continue

            # Find intermediate trough between p1 and p2 (the neckline)
            intermediate_troughs = [t for t in troughs if p1_idx < t[0] < p2_idx]
            if not intermediate_troughs:
                # Fallback to absolute lowest in slice
                mid_slice = df.iloc[p1_idx + 1:p2_idx]
                if mid_slice.empty:
                    continue
                min_row_idx = mid_slice["low"].idxmin()
                neck_idx = df.index.get_loc(min_row_idx)
                neck_time = pd.to_datetime(df.iloc[neck_idx]["timestamp"] if "timestamp" in df.columns else df.index[neck_idx])
                neck_val = float(df.iloc[neck_idx]["low"])
            else:
                neck_idx, neck_time, neck_val = min(intermediate_troughs, key=lambda x: x[2])

            pattern_height = min(p1_high, p2_high) - neck_val
            if pattern_height < params["min_pattern_depth_pts"]:
                continue

            # Second peak candle check for Doji / Inverted Hammer (Mentor rule)
            p2_row = df.iloc[p2_idx]
            is_dj = _is_doji_row(p2_row, params)
            is_ih = _is_inverted_hammer_row(p2_row, params)
            conf_str = "Doji at Peak 2" if is_dj else ("Inverted Hammer at Peak 2" if is_ih else "Rejection at Peak 2")

            neckline = round(neck_val, 2)
            stop_loss = round(max(p1_high, p2_high) + 1.0, 2)
            early_trigger = round(float(p2_row["low"]) - 0.5, 2)
            entry_trigger = round(neckline - 0.5, 2)
            target_1 = round(neckline, 2)
            target_2 = round(neckline - pattern_height, 2)
            risk_pts = round(stop_loss - early_trigger, 2)
            reward_pts = round(early_trigger - target_2, 2)
            rr_ratio = round(reward_pts / risk_pts, 2) if risk_pts > 0 else 1.0

            # State determination
            # Check price action after P2
            post_p2 = df.iloc[p2_idx + 1:]
            lowest_after_p2 = float(post_p2["low"].min()) if not post_p2.empty else current_close
            highest_after_p2 = float(post_p2["high"].max()) if not post_p2.empty else current_close

            if highest_after_p2 > stop_loss:
                status = "INVALIDATED"
                color = SLATE
            elif lowest_after_p2 <= target_2:
                status = "TARGET_HIT"
                color = GREEN
            elif lowest_after_p2 < entry_trigger:
                status = "CONFIRMED_BREAKOUT"
                color = RED
            elif lowest_after_p2 < early_trigger:
                status = "ARMED"
                color = GOLD
            else:
                status = "FORMING"
                color = BLUE

            pts = [
                PatternPoint(p1_idx, p1_time, p1_high, "Peak 1"),
                PatternPoint(neck_idx, neck_time, neck_val, "Neckline"),
                PatternPoint(p2_idx, p2_time, p2_high, f"Peak 2 ({conf_str})"),
            ]
            if not post_p2.empty:
                last_idx = len(df) - 1
                last_time = pd.to_datetime(df.iloc[-1]["timestamp"] if "timestamp" in df.columns else df.index[-1])
                pts.append(PatternPoint(last_idx, last_time, current_close, "Current"))

            patterns.append(ChartPattern(
                pattern_type="M",
                name="M-Pattern Top (Double Top)",
                timeframe=timeframe,
                direction="BEARISH",
                status=status,
                badge_color=color,
                points=pts,
                neckline=neckline,
                stop_loss=stop_loss,
                entry_trigger=entry_trigger,
                early_trigger=early_trigger,
                target_1=target_1,
                target_2=target_2,
                risk_pts=risk_pts,
                reward_pts=reward_pts,
                rr_ratio=rr_ratio,
                confirmation_candle=conf_str,
                description=f"Double Top formed at {p1_high:,.1f} & {p2_high:,.1f} with neckline at {neckline:,.1f}.",
                mentor_rule="If a Doji forms at top of second peak, signals end of uptrend. Early high-R:R entry on 2nd peak Doji low breakdown.",
                created_at=p2_time,
            ))
    return patterns


def detect_w_patterns(df: pd.DataFrame, peaks: list, troughs: list, timeframe: str, params: dict) -> list[ChartPattern]:
    """Detects W-Pattern / Double Bottom formations."""
    patterns = []
    current_close = float(df["close"].iloc[-1]) if len(df) else 0.0

    for i in range(len(troughs) - 1):
        t1_idx, t1_time, t1_low = troughs[i]
        for j in range(i + 1, len(troughs)):
            t2_idx, t2_time, t2_low = troughs[j]
            if not (2 <= (t2_idx - t1_idx) <= 24):
                continue

            if abs(t1_low - t2_low) / max(t1_low, 0.01) > params["double_extremum_tol_pct"]:
                continue

            intermediate_peaks = [p for p in peaks if t1_idx < p[0] < t2_idx]
            if not intermediate_peaks:
                mid_slice = df.iloc[t1_idx + 1:t2_idx]
                if mid_slice.empty:
                    continue
                max_row_idx = mid_slice["high"].idxmax()
                neck_idx = df.index.get_loc(max_row_idx)
                neck_time = pd.to_datetime(df.iloc[neck_idx]["timestamp"] if "timestamp" in df.columns else df.index[neck_idx])
                neck_val = float(df.iloc[neck_idx]["high"])
            else:
                neck_idx, neck_time, neck_val = max(intermediate_peaks, key=lambda x: x[2])

            pattern_height = neck_val - max(t1_low, t2_low)
            if pattern_height < params["min_pattern_depth_pts"]:
                continue

            # Second trough candle check for Hammer / Doji (Mentor rule)
            t2_row = df.iloc[t2_idx]
            is_hm = _is_hammer_row(t2_row, params)
            is_dj = _is_doji_row(t2_row, params)
            conf_str = "Hammer at Trough 2" if is_hm else ("Doji at Trough 2" if is_dj else "Reversal at Trough 2")

            neckline = round(neck_val, 2)
            stop_loss = round(min(t1_low, t2_low) - 1.0, 2)
            early_trigger = round(float(t2_row["high"]) + 0.5, 2)
            entry_trigger = round(neckline + 0.5, 2)
            target_1 = round(neckline, 2)
            target_2 = round(neckline + pattern_height, 2)
            risk_pts = round(early_trigger - stop_loss, 2)
            reward_pts = round(target_2 - early_trigger, 2)
            rr_ratio = round(reward_pts / risk_pts, 2) if risk_pts > 0 else 1.0

            post_t2 = df.iloc[t2_idx + 1:]
            lowest_after_t2 = float(post_t2["low"].min()) if not post_t2.empty else current_close
            highest_after_t2 = float(post_t2["high"].max()) if not post_t2.empty else current_close

            if lowest_after_t2 < stop_loss:
                status = "INVALIDATED"
                color = SLATE
            elif highest_after_t2 >= target_2:
                status = "TARGET_HIT"
                color = GREEN
            elif highest_after_t2 > entry_trigger:
                status = "CONFIRMED_BREAKOUT"
                color = GREEN
            elif highest_after_t2 > early_trigger:
                status = "ARMED"
                color = GOLD
            else:
                status = "FORMING"
                color = BLUE

            pts = [
                PatternPoint(t1_idx, t1_time, t1_low, "Trough 1"),
                PatternPoint(neck_idx, neck_time, neck_val, "Neckline"),
                PatternPoint(t2_idx, t2_time, t2_low, f"Trough 2 ({conf_str})"),
            ]
            if not post_t2.empty:
                last_idx = len(df) - 1
                last_time = pd.to_datetime(df.iloc[-1]["timestamp"] if "timestamp" in df.columns else df.index[-1])
                pts.append(PatternPoint(last_idx, last_time, current_close, "Current"))

            patterns.append(ChartPattern(
                pattern_type="W",
                name="W-Pattern Bottom (Double Bottom)",
                timeframe=timeframe,
                direction="BULLISH",
                status=status,
                badge_color=color,
                points=pts,
                neckline=neckline,
                stop_loss=stop_loss,
                entry_trigger=entry_trigger,
                early_trigger=early_trigger,
                target_1=target_1,
                target_2=target_2,
                risk_pts=risk_pts,
                reward_pts=reward_pts,
                rr_ratio=rr_ratio,
                confirmation_candle=conf_str,
                description=f"Double Bottom formed at {t1_low:,.1f} & {t2_low:,.1f} with neckline at {neckline:,.1f}.",
                mentor_rule="Double Bottom at the lows. Second swing low forms reversal candle (Hammer/Doji) signaling accumulation. Early entry above 2nd trough high.",
                created_at=t2_time,
            ))
    return patterns


def detect_v_patterns(df: pd.DataFrame, peaks: list, troughs: list, timeframe: str, params: dict) -> list[ChartPattern]:
    """Detects sharp V-Bottom Reversals."""
    patterns = []
    current_close = float(df["close"].iloc[-1]) if len(df) else 0.0

    for t_idx, t_time, t_low in troughs:
        # Search prior 2-6 bars for the drop origin
        look_start = max(0, t_idx - 6)
        if look_start >= t_idx - 1:
            continue

        drop_slice = df.iloc[look_start:t_idx]
        orig_high = float(drop_slice["high"].max())
        orig_idx = df.index.get_loc(drop_slice["high"].idxmax())
        orig_time = pd.to_datetime(df.iloc[orig_idx]["timestamp"] if "timestamp" in df.columns else df.index[orig_idx])

        drop_pts = orig_high - t_low
        if drop_pts < params["v_drop_min_pts"]:
            continue

        # Look after trough for sharp bounce
        bounce_slice = df.iloc[t_idx + 1:min(len(df), t_idx + 6)]
        if bounce_slice.empty:
            continue

        bounce_high = float(bounce_slice["high"].max())
        rebound_pts = bounce_high - t_low
        rebound_ratio = rebound_pts / drop_pts if drop_pts > 0 else 0

        if rebound_ratio < params["v_rebound_min_ratio"]:
            continue

        t_row = df.iloc[t_idx]
        is_hm = _is_hammer_row(t_row, params)
        is_dj = _is_doji_row(t_row, params)
        conf_str = "Hammer at V-Low" if is_hm else ("Doji at V-Low" if is_dj else "Sharp V Pivot")

        neckline = round(orig_high, 2)
        stop_loss = round(t_low - 1.0, 2)
        early_trigger = round(t_low + 0.5 * drop_pts, 2) # 50% retracement
        entry_trigger = round(neckline + 0.5, 2)
        target_1 = round(neckline, 2)
        target_2 = round(neckline + 0.382 * drop_pts, 2)
        risk_pts = round(early_trigger - stop_loss, 2)
        reward_pts = round(target_1 - early_trigger, 2)
        rr_ratio = round(reward_pts / risk_pts, 2) if risk_pts > 0 else 1.0

        post_t = df.iloc[t_idx + 1:]
        lowest_post = float(post_t["low"].min()) if not post_t.empty else current_close
        highest_post = float(post_t["high"].max()) if not post_t.empty else current_close

        if lowest_post < stop_loss:
            status = "INVALIDATED"
            color = SLATE
        elif highest_post >= target_1:
            status = "TARGET_HIT"
            color = GREEN
        elif highest_post >= early_trigger:
            status = "CONFIRMED_BREAKOUT"
            color = GREEN
        else:
            status = "ARMED"
            color = GOLD

        pts = [
            PatternPoint(orig_idx, orig_time, orig_high, "Drop Origin"),
            PatternPoint(t_idx, t_time, t_low, f"V-Bottom ({conf_str})"),
        ]
        b_max_idx = df.index.get_loc(bounce_slice["high"].idxmax())
        b_max_time = pd.to_datetime(df.iloc[b_max_idx]["timestamp"] if "timestamp" in df.columns else df.index[b_max_idx])
        pts.append(PatternPoint(b_max_idx, b_max_time, bounce_high, "V-Bounce"))

        patterns.append(ChartPattern(
            pattern_type="V",
            name="V-Pattern Bottom (V-Reversal)",
            timeframe=timeframe,
            direction="BULLISH",
            status=status,
            badge_color=color,
            points=pts,
            neckline=neckline,
            stop_loss=stop_loss,
            entry_trigger=entry_trigger,
            early_trigger=early_trigger,
            target_1=target_1,
            target_2=target_2,
            risk_pts=risk_pts,
            reward_pts=reward_pts,
            rr_ratio=rr_ratio,
            confirmation_candle=conf_str,
            description=f"Sharp V-Bottom reversal from {t_low:,.1f} bouncing +{rebound_pts:.1f} pts ({rebound_ratio:.0%}).",
            mentor_rule="Sharp impulsive drop reversed abruptly by institutional accumulation. Trigger above 50% retracement; stop below vertex.",
            created_at=t_time,
        ))
    return patterns


def detect_a_patterns(df: pd.DataFrame, peaks: list, troughs: list, timeframe: str, params: dict) -> list[ChartPattern]:
    """Detects sharp A-Top / Inverted-V Reversals."""
    patterns = []
    current_close = float(df["close"].iloc[-1]) if len(df) else 0.0

    for p_idx, p_time, p_high in peaks:
        look_start = max(0, p_idx - 6)
        if look_start >= p_idx - 1:
            continue

        rally_slice = df.iloc[look_start:p_idx]
        orig_low = float(rally_slice["low"].min())
        orig_idx = df.index.get_loc(rally_slice["low"].idxmin())
        orig_time = pd.to_datetime(df.iloc[orig_idx]["timestamp"] if "timestamp" in df.columns else df.index[orig_idx])

        rally_pts = p_high - orig_low
        if rally_pts < params["a_rise_min_pts"]:
            continue

        drop_slice = df.iloc[p_idx + 1:min(len(df), p_idx + 6)]
        if drop_slice.empty:
            continue

        drop_low = float(drop_slice["low"].min())
        drop_pts = p_high - drop_low
        drop_ratio = drop_pts / rally_pts if rally_pts > 0 else 0

        if drop_ratio < params["a_drop_min_ratio"]:
            continue

        p_row = df.iloc[p_idx]
        is_ih = _is_inverted_hammer_row(p_row, params)
        is_dj = _is_doji_row(p_row, params)
        conf_str = "Inverted Hammer at A-Peak" if is_ih else ("Doji at A-Peak" if is_dj else "Sharp A Pivot")

        neckline = round(orig_low, 2)
        stop_loss = round(p_high + 1.0, 2)
        early_trigger = round(p_high - 0.5 * rally_pts, 2) # 50% retracement
        entry_trigger = round(neckline - 0.5, 2)
        target_1 = round(neckline, 2)
        target_2 = round(neckline - 0.382 * rally_pts, 2)
        risk_pts = round(stop_loss - early_trigger, 2)
        reward_pts = round(early_trigger - target_1, 2)
        rr_ratio = round(reward_pts / risk_pts, 2) if risk_pts > 0 else 1.0

        post_p = df.iloc[p_idx + 1:]
        lowest_post = float(post_p["low"].min()) if not post_p.empty else current_close
        highest_post = float(post_p["high"].max()) if not post_p.empty else current_close

        if highest_post > stop_loss:
            status = "INVALIDATED"
            color = SLATE
        elif lowest_post <= target_1:
            status = "TARGET_HIT"
            color = GREEN
        elif lowest_post <= early_trigger:
            status = "CONFIRMED_BREAKOUT"
            color = RED
        else:
            status = "ARMED"
            color = GOLD

        pts = [
            PatternPoint(orig_idx, orig_time, orig_low, "Rally Origin"),
            PatternPoint(p_idx, p_time, p_high, f"A-Top ({conf_str})"),
        ]
        d_min_idx = df.index.get_loc(drop_slice["low"].idxmin())
        d_min_time = pd.to_datetime(df.iloc[d_min_idx]["timestamp"] if "timestamp" in df.columns else df.index[d_min_idx])
        pts.append(PatternPoint(d_min_idx, d_min_time, drop_low, "A-Drop"))

        patterns.append(ChartPattern(
            pattern_type="A",
            name="A-Pattern Top (Inverted-V Reversal)",
            timeframe=timeframe,
            direction="BEARISH",
            status=status,
            badge_color=color,
            points=pts,
            neckline=neckline,
            stop_loss=stop_loss,
            entry_trigger=entry_trigger,
            early_trigger=early_trigger,
            target_1=target_1,
            target_2=target_2,
            risk_pts=risk_pts,
            reward_pts=reward_pts,
            rr_ratio=rr_ratio,
            confirmation_candle=conf_str,
            description=f"Sharp A-Top exhaustion from {p_high:,.1f} selling off -{drop_pts:.1f} pts ({drop_ratio:.0%}).",
            mentor_rule="Sharp impulsive rally rejected abruptly by institutional supply. Trigger below 50% retracement; stop above vertex.",
            created_at=p_time,
        ))
    return patterns


def detect_chart_patterns(df: pd.DataFrame, timeframe: str = "15m") -> list[ChartPattern]:
    """
    Main detection entrypoint. Scans dataframe for V, A, W, and M patterns.
    """
    if df is None or df.empty or len(df) < 6:
        return []

    params = load_pattern_params()
    peaks, troughs = find_swings(df, lookback=params["swing_lookback"])

    all_patterns: list[ChartPattern] = []
    all_patterns.extend(detect_m_patterns(df, peaks, troughs, timeframe, params))
    all_patterns.extend(detect_w_patterns(df, peaks, troughs, timeframe, params))
    all_patterns.extend(detect_v_patterns(df, peaks, troughs, timeframe, params))
    all_patterns.extend(detect_a_patterns(df, peaks, troughs, timeframe, params))

    # Deduplicate and sort by recency (created_at desc)
    all_patterns.sort(key=lambda p: (p.created_at, p.status in ("CONFIRMED_BREAKOUT", "ARMED")), reverse=True)
    return all_patterns


# ═══════════════════════════════════════════════════════════════════════════
# Plotly Chart Visualizer with Pattern Overlays
# ═══════════════════════════════════════════════════════════════════════════

def build_pattern_chart(
    df: pd.DataFrame,
    patterns: list[ChartPattern],
    timeframe: str = "15m",
    spot: float | None = None,
    active_pattern_idx: int = 0,
) -> go.Figure:
    """
    Generates a dedicated Plotly candlestick chart with V, A, W, M overlays.
    """
    fig = go.Figure()
    if df.empty:
        return fig

    times = df["timestamp"] if "timestamp" in df.columns else df.index

    # 1. Candlestick Base
    fig.add_trace(go.Candlestick(
        x=times,
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=f"NIFTY {timeframe}",
        increasing_line_color=GREEN,
        decreasing_line_color=RED,
        showlegend=True,
    ))

    # 2. Indicators (4 EMA, 8 EMA, VWAP)
    for col, col_name, color, width in [
        ("ema4", "4 EMA", GREEN, 1.2),
        ("ema8", "8 EMA", RED, 1.2),
        ("vwap", "VWAP", GOLD, 1.8),
    ]:
        if col in df.columns and df[col].notna().any():
            fig.add_trace(go.Scatter(
                x=times,
                y=df[col],
                mode="lines",
                name=col_name,
                line=dict(color=color, width=width, dash="dot" if col == "vwap" else "solid"),
                opacity=0.85,
            ))

    # 3. Draw Pattern Overlays
    if patterns:
        pat = patterns[active_pattern_idx] if 0 <= active_pattern_idx < len(patterns) else patterns[0]
        pat_color = GREEN if pat.direction == "BULLISH" else RED

        # Draw connecting line through pattern vertices
        pts_x = [p.time for p in pat.points]
        pts_y = [p.price for p in pat.points]
        fig.add_trace(go.Scatter(
            x=pts_x,
            y=pts_y,
            mode="lines+markers+text",
            name=f"{pat.pattern_type}-Pattern Polyline",
            line=dict(color=pat_color, width=3.5, shape="linear"),
            marker=dict(size=9, color=pat_color, symbol="circle"),
            text=[f"  {p.label}" for p in pat.points],
            textposition="top right" if pat.direction == "BEARISH" else "bottom right",
            textfont=dict(color="#F2F5F8", size=10),
            showlegend=True,
        ))

        # Horizontal level lines: Neckline, Stop Loss, Target 1, Target 2
        t_start = pat.points[0].time
        t_end = times.iloc[-1]

        # Neckline
        fig.add_trace(go.Scatter(
            x=[t_start, t_end],
            y=[pat.neckline, pat.neckline],
            mode="lines",
            name=f"Neckline ({pat.neckline:,.1f})",
            line=dict(color=GOLD, width=1.8, dash="dash"),
        ))

        # Stop Loss
        fig.add_trace(go.Scatter(
            x=[t_start, t_end],
            y=[pat.stop_loss, pat.stop_loss],
            mode="lines",
            name=f"Stop Loss ({pat.stop_loss:,.1f})",
            line=dict(color="#FF4757", width=1.5, dash="dot"),
        ))

        # Target 2 (Measured move)
        fig.add_trace(go.Scatter(
            x=[t_start, t_end],
            y=[pat.target_2, pat.target_2],
            mode="lines",
            name=f"Target ({pat.target_2:,.1f})",
            line=dict(color=CYAN, width=1.8, dash="dash"),
        ))

    # Spot line
    if spot:
        fig.add_hline(
            y=spot,
            line=dict(color=GOLD, width=1.0, dash="dot"),
            annotation_text=f"Spot {spot:,.1f}",
            annotation_position="right",
            annotation_font=dict(color=GOLD, size=10),
        )

    # Layout styling
    fig.update_layout(
        height=540,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#F2F5F8"),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.03, yanchor="bottom", x=0.0),
    )

    # Rangebreaks for Indian Market Hours
    fig.update_xaxes(
        gridcolor="#12314d",
        rangebreaks=[
            dict(bounds=["sat", "mon"]),
            dict(bounds=[15.6, 9.25], pattern="hour"),
        ],
    )
    fig.update_yaxes(gridcolor="#12314d")
    return fig
