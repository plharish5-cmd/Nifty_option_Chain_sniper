"""
price_action_breakout.py — Advanced Breakout & Trap Detection Engine.

Production-grade, vectorized price action engine for options trading (Nifty/BankNifty).
Consumes live/historical OHLCV data, intraday VWAP, and Option Chain S/R levels
(Call/Put OI walls) to detect high-probability breakouts, filter out traps, and
confirm actionable retest continuation entries without lookahead bias.

FEATURES IMPLEMENTED:
─────────────────────
1. Candle Anatomy Validator:
   - Evaluates Total Range, Body Size, Upper Wick, Lower Wick, Body Ratio, Wick Ratios.
   - 20-period Volume SMA validation.
   - VALID_STRONG_BREAKOUT: Decisive expansion through Key Level with >=60% body,
     above-average volume, short rejection wicks, decisive close beyond level.
   - WEAK_BREAKOUT_WARNING: Flags crossing candles with small body (<60%), below-average
     volume, long rejection wicks, or indecisive closes sitting right on the level.

2. False Breakout (Trap) Detector:
   - TRAP_WICK_REJECTION (Wick Breakout / Sweep): Candle wicks beyond Key Level to trigger
     breakout stops, but body closes back inside the original range with long rejection wick.
   - TRAP_IMMEDIATE_REVERSAL (Inside Bar / Immediate Trap): Candle 1 closes beyond Key Level;
     Candle 2 immediately reverses and closes back inside the original range, trapping breakout traders.
   - TRAP_DOUBLE_REJECTION: Second consecutive failed breakout attempt leaving pronounced
     rejection wicks at the level within 2 to 8 bars.

3. 3-Phase Retest & Continuation Engine (Rolling State Machine):
   - Phase 1 (Breakout): Confirms VALID_STRONG_BREAKOUT across Key Level.
   - Phase 2 (Retest): Over next 1 to 5 candles, price retraces to touch the flipped level
     on strictly lower volume than the breakout candle.
   - Phase 3 (Continuation / Entry): Reversal candlestick confirmation (Hammer/Pin Bar,
     Engulfing, Morning/Evening Star, Piercing/Dark Cloud) triggers ACTIONABLE_RETEST_ENTRY
     with precise Entry, Stop Loss, Target 1 (1:1), and Target 2 (1:2).

Zero lookahead bias: every signal at bar t is computed strictly using data available up to bar t.
Handles both Bullish (Resistance breaking into Support) and Bearish (Support breaking into Resistance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════════════════
# Level Parsing & Normalization
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class NormalizedLevel:
    name: str
    price: Union[float, pd.Series]
    level_type: str  # "RESISTANCE", "SUPPORT", "DYNAMIC_VWAP", "KEY_LEVEL"

    def get_price_at(self, idx: int) -> float:
        if isinstance(self.price, pd.Series):
            val = self.price.iloc[idx]
            return float(val) if pd.notna(val) else np.nan
        return float(self.price)


def _normalize_key_levels(
    key_levels_dict: Optional[Dict[str, Any]],
    df_len: int,
    df_vwap: Optional[pd.Series] = None,
) -> List[NormalizedLevel]:
    """
    Normalizes diverse key level dictionary formats into a canonical list of NormalizedLevel.
    Handles:
      - {"R1": 24500, "S1": 24000, "VWAP": 24250}
      - {"resistances": [24500, 24600], "supports": [24000, 23900]}
      - {"R1_Call_Wall": {"price": 24500, "type": "resistance"}}
      - Dynamic VWAP series from ohlcv_df['vwap']
    """
    levels: List[NormalizedLevel] = []
    if not key_levels_dict:
        key_levels_dict = {}

    for k, v in key_levels_dict.items():
        k_str = str(k).strip()
        k_lower = k_str.lower()

        # Determine level type
        if "vwap" in k_lower:
            lvl_type = "DYNAMIC_VWAP"
        elif any(r in k_lower for r in ["res", "call", "r1", "r2", "r3", "pdh", "high", "ceiling"]):
            lvl_type = "RESISTANCE"
        elif any(s in k_lower for s in ["sup", "put", "s1", "s2", "s3", "pdl", "low", "floor"]):
            lvl_type = "SUPPORT"
        else:
            lvl_type = "KEY_LEVEL"

        # Handle list of prices (e.g. {"resistances": [24500, 24600]})
        if isinstance(v, (list, tuple)):
            for i, p in enumerate(v, 1):
                try:
                    price_val = float(p)
                    sub_name = f"{k_str}_{i}"
                    sub_type = "RESISTANCE" if "res" in k_lower else ("SUPPORT" if "sup" in k_lower else lvl_type)
                    levels.append(NormalizedLevel(name=sub_name, price=price_val, level_type=sub_type))
                except (ValueError, TypeError):
                    continue
        # Handle nested dict: {"R1": {"price": 24500, "type": "resistance"}}
        elif isinstance(v, dict):
            p = v.get("price") or v.get("level") or v.get("strike")
            custom_type = v.get("type", lvl_type).upper()
            if p is not None:
                try:
                    price_val = float(p)
                    levels.append(NormalizedLevel(name=k_str, price=price_val, level_type=custom_type))
                except (ValueError, TypeError):
                    pass
        # Handle pandas Series (e.g. dynamic intraday VWAP)
        elif isinstance(v, pd.Series):
            levels.append(NormalizedLevel(name=k_str, price=v, level_type=lvl_type))
        # Handle scalar float / int
        else:
            try:
                price_val = float(v)
                levels.append(NormalizedLevel(name=k_str, price=price_val, level_type=lvl_type))
            except (ValueError, TypeError):
                continue

    # Auto-inject VWAP from dataframe if not explicitly passed in key_levels_dict
    has_vwap = any("vwap" in l.name.lower() for l in levels)
    if not has_vwap and df_vwap is not None and len(df_vwap) == df_len and df_vwap.notna().any():
        levels.append(NormalizedLevel(name="VWAP", price=df_vwap, level_type="DYNAMIC_VWAP"))

    return levels


# ═══════════════════════════════════════════════════════════════════════════
# Candle Anatomy Engine
# ═══════════════════════════════════════════════════════════════════════════

def compute_candle_anatomy(df: pd.DataFrame, vol_window: int = 20) -> pd.DataFrame:
    """
    Vectorized computation of Candle Anatomy metrics:
      - Total Range = High - Low
      - Body Size = |Close - Open|
      - Upper Wick = High - max(Open, Close)
      - Lower Wick = min(Open, Close) - Low
      - Body Ratio = Body Size / Total Range
      - Upper/Lower Wick Ratios
      - 20-period Volume SMA & High Volume Flag
      - Candle Direction: BULLISH, BEARISH, NEUTRAL
    """
    d = df.copy()
    o = d["open"].to_numpy(dtype=np.float64)
    h = d["high"].to_numpy(dtype=np.float64)
    l = d["low"].to_numpy(dtype=np.float64)
    c = d["close"].to_numpy(dtype=np.float64)

    total_range = h - l
    # Epsilon to prevent divide by zero on zero-range dojis
    denom = np.where(total_range > 1e-6, total_range, 1e-6)

    body_size = np.abs(c - o)
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l

    # Zero-out tiny negative floating point artifacts
    upper_wick = np.maximum(0.0, upper_wick)
    lower_wick = np.maximum(0.0, lower_wick)

    body_ratio = body_size / denom
    upper_wick_ratio = upper_wick / denom
    lower_wick_ratio = lower_wick / denom

    d["total_range"] = total_range
    d["body_size"] = body_size
    d["upper_wick"] = upper_wick
    d["lower_wick"] = lower_wick
    d["body_ratio"] = body_ratio
    d["upper_wick_ratio"] = upper_wick_ratio
    d["lower_wick_ratio"] = lower_wick_ratio

    # Volume SMA (rolling without lookahead bias)
    if "volume" in d.columns and d["volume"].notna().any() and d["volume"].sum() > 0:
        vol = d["volume"].astype(float)
        vol_sma = vol.rolling(window=vol_window, min_periods=1).mean()
        d["vol_sma20"] = vol_sma
        d["is_high_volume"] = vol > vol_sma
        d["vol_ratio"] = vol / np.where(vol_sma > 0, vol_sma, 1.0)
    else:
        # Fallback if volume is absent (e.g. pure synthetic price feed)
        d["vol_sma20"] = 1.0
        d["is_high_volume"] = True
        d["vol_ratio"] = 1.0

    # Candle Direction
    is_bull = c > o
    is_bear = c < o
    candle_type = np.where(is_bull, "BULLISH", np.where(is_bear, "BEARISH", "NEUTRAL"))
    d["candle_type"] = candle_type

    return d


# ═══════════════════════════════════════════════════════════════════════════
# Reversal Candle Pattern Recognizers (Vectorized / Fast)
# ═══════════════════════════════════════════════════════════════════════════

def identify_reversal_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Identifies high-probability reversal candle patterns strictly without lookahead bias:
      - Bullish: HAMMER / PIN_BAR, BULLISH_ENGULFING, MORNING_STAR, PIERCING_BOUNCE
      - Bearish: SHOOTING_STAR / INV_PIN, BEARISH_ENGULFING, EVENING_STAR, DARK_CLOUD
    """
    d = df.copy()
    n = len(d)
    o = d["open"].to_numpy(dtype=np.float64)
    h = d["high"].to_numpy(dtype=np.float64)
    l = d["low"].to_numpy(dtype=np.float64)
    c = d["close"].to_numpy(dtype=np.float64)

    body = d["body_size"].to_numpy(dtype=np.float64)
    tr = d["total_range"].to_numpy(dtype=np.float64)
    uw = d["upper_wick"].to_numpy(dtype=np.float64)
    lw = d["lower_wick"].to_numpy(dtype=np.float64)
    br = d["body_ratio"].to_numpy(dtype=np.float64)
    uwr = d["upper_wick_ratio"].to_numpy(dtype=np.float64)
    lwr = d["lower_wick_ratio"].to_numpy(dtype=np.float64)

    bull_pattern = np.array([""] * n, dtype=object)
    bear_pattern = np.array([""] * n, dtype=object)

    for i in range(n):
        if tr[i] < 1e-4:
            continue

        # ── 1. Bullish Reversals ───────────────────────────────────────────
        # Hammer / Bullish Pin Bar:
        # Long lower wick (>= 50% of range, or >= 1.8x body), small upper wick (<= 20% of range)
        is_hammer = (lwr[i] >= 0.50 or (lw[i] >= 1.8 * body[i] and lwr[i] >= 0.40)) and (uwr[i] <= 0.20)
        if is_hammer:
            bull_pattern[i] = "HAMMER"

        # Bullish Engulfing:
        if i >= 1:
            prev_bear = c[i - 1] < o[i - 1]
            curr_bull = c[i] > o[i]
            # Current body engulfs previous body
            if prev_bear and curr_bull and (o[i] <= c[i - 1] + 1.5) and (c[i] >= o[i - 1] - 0.5) and br[i] >= 0.50:
                bull_pattern[i] = "BULLISH_ENGULFING"

        # Morning Star (3-candle pattern):
        if i >= 2 and bull_pattern[i] == "":
            first_bear = (c[i - 2] < o[i - 2]) and (br[i - 2] >= 0.50)
            star_indecision = br[i - 1] <= 0.35
            third_bull = (c[i] > o[i]) and (c[i] >= (o[i - 2] + c[i - 2]) / 2.0)
            if first_bear and star_indecision and third_bull:
                bull_pattern[i] = "MORNING_STAR"

        # Piercing / Rejection Bounce:
        if i >= 1 and bull_pattern[i] == "":
            prev_bear = c[i - 1] < o[i - 1]
            curr_bull = c[i] > o[i]
            mid_prev = (o[i - 1] + c[i - 1]) / 2.0
            if prev_bear and curr_bull and (c[i] > mid_prev) and (lwr[i] >= 0.30 or br[i] >= 0.55):
                bull_pattern[i] = "PIERCING_BOUNCE"

        # ── 2. Bearish Reversals ───────────────────────────────────────────
        # Shooting Star / Bearish Pin Bar:
        # Long upper wick (>= 50% of range, or >= 1.8x body), small lower wick (<= 20% of range)
        is_shooting_star = (uwr[i] >= 0.50 or (uw[i] >= 1.8 * body[i] and uwr[i] >= 0.40)) and (lwr[i] <= 0.20)
        if is_shooting_star:
            bear_pattern[i] = "SHOOTING_STAR"

        # Bearish Engulfing:
        if i >= 1:
            prev_bull = c[i - 1] > o[i - 1]
            curr_bear = c[i] < o[i]
            # Current body engulfs previous body
            if prev_bull and curr_bear and (o[i] >= c[i - 1] - 1.5) and (c[i] <= o[i - 1] + 0.5) and br[i] >= 0.50:
                bear_pattern[i] = "BEARISH_ENGULFING"

        # Evening Star (3-candle pattern):
        if i >= 2 and bear_pattern[i] == "":
            first_bull = (c[i - 2] > o[i - 2]) and (br[i - 2] >= 0.50)
            star_indecision = br[i - 1] <= 0.35
            third_bear = (c[i] < o[i]) and (c[i] <= (o[i - 2] + c[i - 2]) / 2.0)
            if first_bull and star_indecision and third_bear:
                bear_pattern[i] = "EVENING_STAR"

        # Dark Cloud Cover / Rejection:
        if i >= 1 and bear_pattern[i] == "":
            prev_bull = c[i - 1] > o[i - 1]
            curr_bear = c[i] < o[i]
            mid_prev = (o[i - 1] + c[i - 1]) / 2.0
            if prev_bull and curr_bear and (c[i] < mid_prev) and (uwr[i] >= 0.30 or br[i] >= 0.55):
                bear_pattern[i] = "DARK_CLOUD_REJECTION"

    d["bull_reversal_pattern"] = bull_pattern
    d["bear_reversal_pattern"] = bear_pattern
    return d


# ═══════════════════════════════════════════════════════════════════════════
# Breakout & Retest State Tracking
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BreakoutState:
    bar_index: int
    timestamp: Any
    level_name: str
    level_price: float
    direction: str       # "BULLISH" | "BEARISH"
    breakout_volume: float
    breakout_high: float
    breakout_low: float
    breakout_close: float
    status: str = "PHASE_1_BREAKOUT"  # PHASE_1_BREAKOUT -> PHASE_2_RETESTED -> PHASE_3_CONFIRMED | EXPIRED
    retest_bar_index: Optional[int] = None
    retest_extreme: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# PriceActionBreakoutEngine (Primary Engine Class)
# ═══════════════════════════════════════════════════════════════════════════

class PriceActionBreakoutEngine:
    """
    Advanced Breakout & Trap Detection Engine.

    Vectorized and state-machine driven engine for:
      - Feature 1: Candle Anatomy Validator (Strong vs. Weak Breakout)
      - Feature 2: False Breakout / Trap Scanner (Wick Rejection, Immediate Reversal, Double Rejection)
      - Feature 3: 3-Phase Retest & Continuation Engine (Breakout -> Low Vol Retest -> Continuation)
    """

    def __init__(
        self,
        min_body_ratio: float = 0.60,
        max_breakout_wick_ratio: float = 0.20,
        vol_sma_window: int = 20,
        min_close_penetration_ratio: float = 0.15,
        min_close_penetration_pts: float = 1.5,
        trap_wick_ratio: float = 0.35,
        double_rejection_window: int = 8,
        retest_max_bars: int = 5,
        retest_buffer_pts: float = 4.0,
        min_risk_pts: float = 5.0,
        max_risk_pts: float = 65.0,
        rr_target_1: float = 1.0,
        rr_target_2: float = 2.0,
    ):
        self.min_body_ratio = min_body_ratio
        self.max_breakout_wick_ratio = max_breakout_wick_ratio
        self.vol_sma_window = vol_sma_window
        self.min_close_penetration_ratio = min_close_penetration_ratio
        self.min_close_penetration_pts = min_close_penetration_pts
        self.trap_wick_ratio = trap_wick_ratio
        self.double_rejection_window = double_rejection_window
        self.retest_max_bars = retest_max_bars
        self.retest_buffer_pts = retest_buffer_pts
        self.min_risk_pts = min_risk_pts
        self.max_risk_pts = max_risk_pts
        self.rr_target_1 = rr_target_1
        self.rr_target_2 = rr_target_2

    def apply_breakout_signals(
        self,
        ohlcv_df: pd.DataFrame,
        key_levels_dict: Optional[Dict[str, Any]] = None,
    ) -> pd.DataFrame:
        """
        Primary engine method: Analyzes ohlcv_df against key_levels_dict and returns
        the dataframe augmented with candle anatomy, breakout classifications, trap
        detections, and 3-phase retest continuation signals.

        Parameters:
          - ohlcv_df: DataFrame containing OHLCV data. Tolerates case-insensitive column names
                      (open, high, low, close, volume, timestamp, vwap).
          - key_levels_dict: Dict containing S/R levels, e.g.:
              {"VWAP": 23925.0, "R1_Call_Wall": 23950.0, "S1_Put_Wall": 23900.0}
              or {"resistances": [23950, 24000], "supports": [23900, 23850]}

        Returns:
          pd.DataFrame with original columns plus:
            - Candle Anatomy: total_range, body_size, upper_wick, lower_wick,
                              body_ratio, upper_wick_ratio, lower_wick_ratio,
                              vol_sma20, is_high_volume, vol_ratio, candle_type
            - Feature 1: breakout_signal (VALID_STRONG_BREAKOUT, WEAK_BREAKOUT_WARNING)
            - Feature 2: trap_signal (TRAP_WICK_REJECTION, TRAP_IMMEDIATE_REVERSAL, TRAP_DOUBLE_REJECTION)
            - Feature 3: actionable_retest_entry (BULLISH_RETEST_ENTRY, BEARISH_RETEST_ENTRY)
            - Execution Context: signal_direction, key_level_hit, key_level_price,
                                 retest_phase, retest_pattern, entry_price, stop_loss,
                                 target_1, target_2, risk_pts, rr_ratio, signal_notes
        """
        if ohlcv_df is None or len(ohlcv_df) < 2:
            return ohlcv_df.copy() if ohlcv_df is not None else pd.DataFrame()

        # Step 1: Standardize column names
        col_map = {c: str(c).lower().strip() for c in ohlcv_df.columns}
        df = ohlcv_df.rename(columns=col_map).copy()
        for required in ["open", "high", "low", "close"]:
            if required not in df.columns:
                raise ValueError(f"Required column '{required}' not found in OHLCV DataFrame.")

        # Step 2: Compute Candle Anatomy & Reversal Patterns
        df = compute_candle_anatomy(df, vol_window=self.vol_sma_window)
        df = identify_reversal_patterns(df)

        n = len(df)
        ts = df["timestamp"] if "timestamp" in df.columns else pd.Series(df.index)

        # Step 3: Parse and normalize key levels
        df_vwap = df["vwap"] if "vwap" in df.columns else None
        normalized_levels = _normalize_key_levels(key_levels_dict, n, df_vwap=df_vwap)

        # Output series placeholders
        breakout_signal = np.array([""] * n, dtype=object)
        trap_signal = np.array([""] * n, dtype=object)
        actionable_retest = np.array([""] * n, dtype=object)
        sig_direction = np.array([""] * n, dtype=object)
        key_lvl_hit = np.array([""] * n, dtype=object)
        key_lvl_price = np.full(n, np.nan, dtype=np.float64)
        retest_phase = np.zeros(n, dtype=np.int32)
        retest_pattern = np.array([""] * n, dtype=object)
        entry_price = np.full(n, np.nan, dtype=np.float64)
        stop_loss = np.full(n, np.nan, dtype=np.float64)
        target_1 = np.full(n, np.nan, dtype=np.float64)
        target_2 = np.full(n, np.nan, dtype=np.float64)
        risk_pts = np.full(n, np.nan, dtype=np.float64)
        rr_ratio = np.full(n, np.nan, dtype=np.float64)
        signal_notes = np.array([""] * n, dtype=object)

        o = df["open"].to_numpy(dtype=np.float64)
        h = df["high"].to_numpy(dtype=np.float64)
        l = df["low"].to_numpy(dtype=np.float64)
        c = df["close"].to_numpy(dtype=np.float64)
        v = df["volume"].to_numpy(dtype=np.float64) if "volume" in df.columns else np.ones(n, dtype=np.float64)
        tr = df["total_range"].to_numpy(dtype=np.float64)
        body = df["body_size"].to_numpy(dtype=np.float64)
        uw = df["upper_wick"].to_numpy(dtype=np.float64)
        lw = df["lower_wick"].to_numpy(dtype=np.float64)
        br = df["body_ratio"].to_numpy(dtype=np.float64)
        uwr = df["upper_wick_ratio"].to_numpy(dtype=np.float64)
        lwr = df["lower_wick_ratio"].to_numpy(dtype=np.float64)
        is_hi_vol = df["is_high_volume"].to_numpy(dtype=bool)

        bull_rev = df["bull_reversal_pattern"].to_numpy(dtype=object)
        bear_rev = df["bear_reversal_pattern"].to_numpy(dtype=object)

        # Active breakout tracking per level: {level_name: List[BreakoutState]}
        active_breakouts: Dict[str, List[BreakoutState]] = {lvl.name: [] for lvl in normalized_levels}

        # ── Step 4: Chronological Simulation (Strictly Forward, Zero Lookahead) ────
        for i in range(1, n):
            # Prior candle attributes
            c_prev = c[i - 1]

            # Iterate over all levels to inspect interaction at candle i
            for lvl in normalized_levels:
                L = lvl.get_price_at(i)
                if np.isnan(L) or L <= 0:
                    continue

                L_prev = lvl.get_price_at(i - 1)
                if np.isnan(L_prev):
                    L_prev = L

                # Minimum penetration required for a decisive close beyond the level
                decisive_margin = max(self.min_close_penetration_pts, self.min_close_penetration_ratio * body[i])

                # ─────────────────────────────────────────────────────────────
                # FEATURE 1: CANDLE ANATOMY VALIDATOR (STRONG VS. WEAK BREAKOUT)
                # ─────────────────────────────────────────────────────────────
                # Bullish Breakout Check (Crossing above L)
                bullish_cross = (c_prev <= L_prev + 0.5 or o[i] <= L + 0.5) and (c[i] > L) and (h[i] > L)
                # Bearish Breakout Check (Crossing below L)
                bearish_cross = (c_prev >= L_prev - 0.5 or o[i] >= L - 0.5) and (c[i] < L) and (l[i] < L)

                # 1. Bullish Breakout Anatomy
                if bullish_cross and c[i] > o[i]:
                    is_large_body = br[i] >= self.min_body_ratio
                    is_volume_backed = is_hi_vol[i]
                    is_short_upper_wick = uwr[i] <= self.max_breakout_wick_ratio
                    is_decisive_close = (c[i] - L) >= decisive_margin

                    if is_large_body and is_volume_backed and is_short_upper_wick and is_decisive_close:
                        if breakout_signal[i] == "":
                            breakout_signal[i] = "VALID_STRONG_BREAKOUT"
                            sig_direction[i] = "BULLISH"
                            key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                            key_lvl_price[i] = L
                            retest_phase[i] = 1
                            signal_notes[i] = (
                                f"Decisive Bullish Breakout above {lvl.name} ({L:,.1f}). "
                                f"Body: {br[i]*100:.0f}%, Vol: Above 20-SMA, Upper Wick: {uwr[i]*100:.0f}%."
                            )

                        # Register in active breakouts for Feature 3
                        active_breakouts[lvl.name].append(
                            BreakoutState(
                                bar_index=i,
                                timestamp=ts.iloc[i],
                                level_name=lvl.name,
                                level_price=L,
                                direction="BULLISH",
                                breakout_volume=v[i],
                                breakout_high=h[i],
                                breakout_low=l[i],
                                breakout_close=c[i],
                            )
                        )
                    else:
                        # Failed one or more strong criteria -> WEAK_BREAKOUT_WARNING
                        reasons = []
                        if not is_large_body:
                            reasons.append(f"Small Body ({br[i]*100:.0f}% < 60%)")
                        if not is_volume_backed:
                            reasons.append("Low Volume (<= 20-SMA)")
                        if not is_short_upper_wick:
                            reasons.append(f"Upper Rejection Wick ({uwr[i]*100:.0f}% > 20%)")
                        if not is_decisive_close:
                            reasons.append("Close resting on level without expansion")

                        if breakout_signal[i] == "":
                            breakout_signal[i] = "WEAK_BREAKOUT_WARNING"
                            sig_direction[i] = "BULLISH"
                            key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                            key_lvl_price[i] = L
                            signal_notes[i] = f"Weak Bullish Cross of {lvl.name} ({L:,.1f}): {', '.join(reasons)}."

                # 2. Bearish Breakout Anatomy
                elif bearish_cross and c[i] < o[i]:
                    is_large_body = br[i] >= self.min_body_ratio
                    is_volume_backed = is_hi_vol[i]
                    is_short_lower_wick = lwr[i] <= self.max_breakout_wick_ratio
                    is_decisive_close = (L - c[i]) >= decisive_margin

                    if is_large_body and is_volume_backed and is_short_lower_wick and is_decisive_close:
                        if breakout_signal[i] == "":
                            breakout_signal[i] = "VALID_STRONG_BREAKOUT"
                            sig_direction[i] = "BEARISH"
                            key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                            key_lvl_price[i] = L
                            retest_phase[i] = 1
                            signal_notes[i] = (
                                f"Decisive Bearish Breakdown below {lvl.name} ({L:,.1f}). "
                                f"Body: {br[i]*100:.0f}%, Vol: Above 20-SMA, Lower Wick: {lwr[i]*100:.0f}%."
                            )

                        # Register in active breakouts for Feature 3
                        active_breakouts[lvl.name].append(
                            BreakoutState(
                                bar_index=i,
                                timestamp=ts.iloc[i],
                                level_name=lvl.name,
                                level_price=L,
                                direction="BEARISH",
                                breakout_volume=v[i],
                                breakout_high=h[i],
                                breakout_low=l[i],
                                breakout_close=c[i],
                            )
                        )
                    else:
                        reasons = []
                        if not is_large_body:
                            reasons.append(f"Small Body ({br[i]*100:.0f}% < 60%)")
                        if not is_volume_backed:
                            reasons.append("Low Volume (<= 20-SMA)")
                        if not is_short_lower_wick:
                            reasons.append(f"Lower Rejection Wick ({lwr[i]*100:.0f}% > 20%)")
                        if not is_decisive_close:
                            reasons.append("Close resting on level without expansion")

                        if breakout_signal[i] == "":
                            breakout_signal[i] = "WEAK_BREAKOUT_WARNING"
                            sig_direction[i] = "BEARISH"
                            key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                            key_lvl_price[i] = L
                            signal_notes[i] = f"Weak Bearish Breakdown of {lvl.name} ({L:,.1f}): {', '.join(reasons)}."

                # ─────────────────────────────────────────────────────────────
                # FEATURE 2: FALSE BREAKOUT (TRAP) DETECTOR
                # ─────────────────────────────────────────────────────────────

                # Pattern A: Double Rejection -> TRAP_DOUBLE_REJECTION (higher-order pattern)
                # Price attempts to pierce the level twice within double_rejection_window,
                # leaving two pronounced wicks and failing to hold beyond the level.
                if trap_signal[i] == "":
                    lookback_start = max(0, i - self.double_rejection_window)
                    # Resistance Double Rejection:
                    if (h[i] >= L - 1.0) and (c[i] <= L + 1.0) and (uwr[i] >= 0.30):
                        for k in range(i - 1, lookback_start - 1, -1):
                            Lk = lvl.get_price_at(k)
                            if np.isnan(Lk):
                                continue
                            # Did bar k test Lk with a pronounced upper wick?
                            if (h[k] >= Lk - 1.0) and (c[k] <= Lk + 1.0) and (uwr[k] >= 0.30):
                                # Verify price did not accept/close significantly above L in between
                                in_between_closes = c[k + 1 : i]
                                if len(in_between_closes) == 0 or np.all(in_between_closes <= L + decisive_margin):
                                    trap_signal[i] = "TRAP_DOUBLE_REJECTION"
                                    sig_direction[i] = "BEARISH"
                                    key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                                    key_lvl_price[i] = L
                                    signal_notes[i] = (
                                        f"Double Rejection Trap at {lvl.name} ({L:,.1f}). "
                                        f"Price failed breakout attempt twice (bars {k} & {i}) leaving "
                                        f"pronounced rejection wicks ({uwr[k]*100:.0f}% & {uwr[i]*100:.0f}%)."
                                    )
                                    break

                    # Support Double Rejection:
                    elif (l[i] <= L + 1.0) and (c[i] >= L - 1.0) and (lwr[i] >= 0.30):
                        for k in range(i - 1, lookback_start - 1, -1):
                            Lk = lvl.get_price_at(k)
                            if np.isnan(Lk):
                                continue
                            if (l[k] <= Lk + 1.0) and (c[k] >= Lk - 1.0) and (lwr[k] >= 0.30):
                                in_between_closes = c[k + 1 : i]
                                if len(in_between_closes) == 0 or np.all(in_between_closes >= L - decisive_margin):
                                    trap_signal[i] = "TRAP_DOUBLE_REJECTION"
                                    sig_direction[i] = "BULLISH"
                                    key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                                    key_lvl_price[i] = L
                                    signal_notes[i] = (
                                        f"Double Rejection Trap at {lvl.name} ({L:,.1f}). "
                                        f"Price failed breakdown attempt twice (bars {k} & {i}) leaving "
                                        f"pronounced rejection wicks ({lwr[k]*100:.0f}% & {lwr[i]*100:.0f}%)."
                                    )
                                    break

                # Pattern B: Inside Bar Trap / Immediate Reversal -> TRAP_IMMEDIATE_REVERSAL
                # Candle 1 closed beyond L. Candle 2 immediately reverses and closes back inside.
                if i >= 1 and trap_signal[i] == "":
                    # FAKEOUTFIX: a trap needs a real break in the last 1–2 candles — a close on the other side of
                    # the level at i-2 or i-3. Before, a plain support break after hours above it was called a "bull trap".
                    def _inside(j, up, _lvl=lvl, _Lp=L_prev):
                        if j < 0:
                            return False
                        Lj = _lvl.get_price_at(j)
                        if np.isnan(Lj):
                            Lj = _Lp
                        return bool(c[j] <= Lj) if up else bool(c[j] >= Lj)
                    # Bullish Trap (Longs trapped above resistance):
                    c1_above = (c_prev > L_prev) and (_inside(i - 2, True) or _inside(i - 3, True))
                    c2_back_below = (c[i] < L) and (c[i] < o[i])
                    if c1_above and c2_back_below:
                        trap_signal[i] = "TRAP_IMMEDIATE_REVERSAL"
                        sig_direction[i] = "BEARISH"
                        key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                        key_lvl_price[i] = L
                        signal_notes[i] = (
                            f"Immediate Reversal Trap at {lvl.name} ({L:,.1f}). "
                            f"Prior candle broke out ({c_prev:,.1f} > {L_prev:,.1f}), but current candle "
                            f"reversed immediately back inside ({c[i]:,.1f} < {L:,.1f}). Breakout buyers trapped!"
                        )

                    # Bearish Trap (Shorts trapped below support):
                    c1_below = (c_prev < L_prev) and (_inside(i - 2, False) or _inside(i - 3, False))   # FAKEOUTFIX
                    c2_back_above = (c[i] > L) and (c[i] > o[i])
                    if c1_below and c2_back_above:
                        trap_signal[i] = "TRAP_IMMEDIATE_REVERSAL"
                        sig_direction[i] = "BULLISH"
                        key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                        key_lvl_price[i] = L
                        signal_notes[i] = (
                            f"Immediate Reversal Trap at {lvl.name} ({L:,.1f}). "
                            f"Prior candle broke down ({c_prev:,.1f} < {L_prev:,.1f}), but current candle "
                            f"reversed immediately back inside ({c[i]:,.1f} > {L:,.1f}). Breakout short sellers trapped!"
                        )

                # Pattern C: Wick Breakout (Sweep) -> TRAP_WICK_REJECTION
                # Bullish Sweep: High penetrates resistance L, but body closes completely back inside/below L
                bull_sweep = (
                    (h[i] > L)
                    and (max(o[i], c[i]) <= L + 1.0)
                    and (c[i] <= L)
                    and (uwr[i] >= self.trap_wick_ratio or (uw[i] >= 1.5 * body[i] and uwr[i] >= 0.30))
                )
                # Bearish Sweep: Low penetrates support L, but body closes completely back inside/above L
                bear_sweep = (
                    (l[i] < L)
                    and (min(o[i], c[i]) >= L - 1.0)
                    and (c[i] >= L)
                    and (lwr[i] >= self.trap_wick_ratio or (lw[i] >= 1.5 * body[i] and lwr[i] >= 0.30))
                )

                if bull_sweep and trap_signal[i] == "":
                    trap_signal[i] = "TRAP_WICK_REJECTION"
                    sig_direction[i] = "BEARISH"  # Sellers trapped buyers at the top
                    key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                    key_lvl_price[i] = L
                    signal_notes[i] = (
                        f"Liquidity Sweep Trap at {lvl.name} ({L:,.1f}). "
                        f"High ({h[i]:,.1f}) swept above level, but closed back inside ({c[i]:,.1f}). "
                        f"Upper Rejection Wick: {uwr[i]*100:.0f}%."
                    )
                elif bear_sweep and trap_signal[i] == "":
                    trap_signal[i] = "TRAP_WICK_REJECTION"
                    sig_direction[i] = "BULLISH"  # Buyers trapped sellers at the bottom
                    key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                    key_lvl_price[i] = L
                    signal_notes[i] = (
                        f"Liquidity Sweep Trap at {lvl.name} ({L:,.1f}). "
                        f"Low ({l[i]:,.1f}) swept below level, but closed back inside ({c[i]:,.1f}). "
                        f"Lower Rejection Wick: {lwr[i]*100:.0f}%."
                    )

                # ─────────────────────────────────────────────────────────────
                # FEATURE 3: THE 3-PHASE RETEST & CONTINUATION ENGINE
                # ─────────────────────────────────────────────────────────────
                # Iterate through active breakouts for this level
                surviving_breakouts: List[BreakoutState] = []
                for b_state in active_breakouts[lvl.name]:
                    bars_since = i - b_state.bar_index

                    # If beyond 5 candles, breakout expires without retest
                    if bars_since > self.retest_max_bars:
                        b_state.status = "EXPIRED"
                        continue

                    # Current candle cannot be the breakout candle itself
                    if bars_since < 1:
                        surviving_breakouts.append(b_state)
                        continue

                    # Volume condition: Retracement candles MUST display lower volume than breakout candle
                    has_lower_volume = v[i] < b_state.breakout_volume

                    # ── Bullish Setup: Resistance broke, now flipped to Support ──
                    if b_state.direction == "BULLISH":
                        # Retest condition: Low touches the flipped support level within buffer
                        touches_flipped_support = (l[i] <= L + self.retest_buffer_pts) and (c[i] >= L - self.retest_buffer_pts)
                        # Invalidated if price breaks significantly back below level
                        broken_support_invalidated = c[i] < L - 2.0 * self.retest_buffer_pts

                        if broken_support_invalidated:
                            b_state.status = "INVALIDATED"
                            continue

                        if touches_flipped_support and has_lower_volume:
                            b_state.status = "PHASE_2_RETESTED"
                            b_state.retest_bar_index = i
                            b_state.retest_extreme = min(b_state.retest_extreme or l[i], l[i])

                        # Phase 3 Confirmation Check:
                        # If candle i confirmed retest or follows a confirmed retest within window,
                        # check for bullish reversal candlestick pattern
                        if b_state.status == "PHASE_2_RETESTED":
                            rev_cand = bull_rev[i]
                            # Or a strong green bounce off the level
                            is_bounce = (c[i] > o[i]) and (c[i] > L) and (lwr[i] >= 0.30 or br[i] >= 0.50)

                            if (rev_cand != "" or is_bounce) and actionable_retest[i] == "":
                                cand_label = rev_cand if rev_cand != "" else "BULLISH_BOUNCE"
                                actionable_retest[i] = "BULLISH_RETEST_ENTRY"
                                sig_direction[i] = "BULLISH"
                                key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                                key_lvl_price[i] = L
                                retest_phase[i] = 3
                                retest_pattern[i] = cand_label

                                entry_p = c[i]
                                # Stop loss below lowest low of breakout and retest structure
                                sl_p = min(b_state.breakout_low, b_state.retest_extreme, l[i]) - 2.0
                                r_pts = max(self.min_risk_pts, min(self.max_risk_pts, entry_p - sl_p))
                                # Recalculate SL to respect risk bounds if needed
                                sl_p = entry_p - r_pts

                                entry_price[i] = entry_p
                                stop_loss[i] = sl_p
                                risk_pts[i] = r_pts
                                target_1[i] = entry_p + (r_pts * self.rr_target_1)
                                target_2[i] = entry_p + (r_pts * self.rr_target_2)
                                rr_ratio[i] = self.rr_target_2

                                signal_notes[i] = (
                                    f"Phase 3 Retest Entry (BUY CE/LONG) confirmed at flipped support {lvl.name} ({L:,.1f}). "
                                    f"Pattern: {cand_label}. Entry: {entry_p:,.1f}, SL: {sl_p:,.1f}, "
                                    f"T1 (1:1): {target_1[i]:,.1f}, T2 (1:2): {target_2[i]:,.1f}."
                                )
                                b_state.status = "PHASE_3_CONFIRMED"
                                continue

                    # ── Bearish Setup: Support broke, now flipped to Resistance ──
                    elif b_state.direction == "BEARISH":
                        # Retest condition: High touches flipped resistance level within buffer
                        touches_flipped_resistance = (h[i] >= L - self.retest_buffer_pts) and (c[i] <= L + self.retest_buffer_pts)
                        broken_resistance_invalidated = c[i] > L + 2.0 * self.retest_buffer_pts

                        if broken_resistance_invalidated:
                            b_state.status = "INVALIDATED"
                            continue

                        if touches_flipped_resistance and has_lower_volume:
                            b_state.status = "PHASE_2_RETESTED"
                            b_state.retest_bar_index = i
                            b_state.retest_extreme = max(b_state.retest_extreme or h[i], h[i])

                        if b_state.status == "PHASE_2_RETESTED":
                            rev_cand = bear_rev[i]
                            is_bounce = (c[i] < o[i]) and (c[i] < L) and (uwr[i] >= 0.30 or br[i] >= 0.50)

                            if (rev_cand != "" or is_bounce) and actionable_retest[i] == "":
                                cand_label = rev_cand if rev_cand != "" else "BEARISH_BOUNCE"
                                actionable_retest[i] = "BEARISH_RETEST_ENTRY"
                                sig_direction[i] = "BEARISH"
                                key_lvl_hit[i] = f"{lvl.name} ({L:,.1f})"
                                key_lvl_price[i] = L
                                retest_phase[i] = 3
                                retest_pattern[i] = cand_label

                                entry_p = c[i]
                                # Stop loss above highest high of breakout and retest structure
                                sl_p = max(b_state.breakout_high, b_state.retest_extreme, h[i]) + 2.0
                                r_pts = max(self.min_risk_pts, min(self.max_risk_pts, sl_p - entry_p))
                                sl_p = entry_p + r_pts

                                entry_price[i] = entry_p
                                stop_loss[i] = sl_p
                                risk_pts[i] = r_pts
                                target_1[i] = entry_p - (r_pts * self.rr_target_1)
                                target_2[i] = entry_p - (r_pts * self.rr_target_2)
                                rr_ratio[i] = self.rr_target_2

                                signal_notes[i] = (
                                    f"Phase 3 Retest Entry (BUY PE/SHORT) confirmed at flipped resistance {lvl.name} ({L:,.1f}). "
                                    f"Pattern: {cand_label}. Entry: {entry_p:,.1f}, SL: {sl_p:,.1f}, "
                                    f"T1 (1:1): {target_1[i]:,.1f}, T2 (1:2): {target_2[i]:,.1f}."
                                )
                                b_state.status = "PHASE_3_CONFIRMED"
                                continue

                    surviving_breakouts.append(b_state)

                active_breakouts[lvl.name] = surviving_breakouts

        # Step 5: Merge calculated signals back into dataframe
        df["breakout_signal"] = breakout_signal
        df["trap_signal"] = trap_signal
        df["actionable_retest_entry"] = actionable_retest
        df["signal_direction"] = sig_direction
        df["key_level_hit"] = key_lvl_hit
        df["key_level_price"] = key_lvl_price
        df["retest_phase"] = retest_phase
        df["retest_pattern"] = retest_pattern
        df["entry_price"] = entry_price
        df["stop_loss"] = stop_loss
        df["risk_pts"] = risk_pts
        df["target_1"] = target_1
        df["target_2"] = target_2
        df["rr_ratio"] = rr_ratio
        df["signal_notes"] = signal_notes

        # Unified primary signal column for easy filtering in Streamlit
        primary_signals = np.where(
            actionable_retest != "",
            actionable_retest,
            np.where(
                trap_signal != "",
                trap_signal,
                np.where(breakout_signal != "", breakout_signal, ""),
            ),
        )
        df["primary_signal"] = primary_signals

        return df

    def extract_actionable_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Filters the processed dataframe to rows where any breakout, trap, or retest entry occurred.
        """
        if df.empty or "primary_signal" not in df.columns:
            return pd.DataFrame()
        mask = df["primary_signal"] != ""
        return df[mask].copy()

    def get_summary_stats(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Computes executive statistics on detected breakouts, traps, and retest setups.
        """
        if df.empty or "primary_signal" not in df.columns:
            return {
                "total_candles": len(df),
                "strong_breakouts": 0,
                "weak_breakouts": 0,
                "wick_traps": 0,
                "immediate_reversal_traps": 0,
                "double_rejection_traps": 0,
                "retest_entries": 0,
                "bullish_retest_entries": 0,
                "bearish_retest_entries": 0,
            }

        return {
            "total_candles": len(df),
            "strong_breakouts": int((df["breakout_signal"] == "VALID_STRONG_BREAKOUT").sum()),
            "weak_breakouts": int((df["breakout_signal"] == "WEAK_BREAKOUT_WARNING").sum()),
            "wick_traps": int((df["trap_signal"] == "TRAP_WICK_REJECTION").sum()),
            "immediate_reversal_traps": int((df["trap_signal"] == "TRAP_IMMEDIATE_REVERSAL").sum()),
            "double_rejection_traps": int((df["trap_signal"] == "TRAP_DOUBLE_REJECTION").sum()),
            "retest_entries": int((df["actionable_retest_entry"] != "").sum()),
            "bullish_retest_entries": int((df["actionable_retest_entry"] == "BULLISH_RETEST_ENTRY").sum()),
            "bearish_retest_entries": int((df["actionable_retest_entry"] == "BEARISH_RETEST_ENTRY").sum()),
        }
