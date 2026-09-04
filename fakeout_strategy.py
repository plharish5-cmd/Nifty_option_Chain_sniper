"""
fakeout_strategy.py — Automated detection for Priyank Sharma's Fakeout Trading System.

CORE CONCEPTS:
──────────────
1. IMMEDIATE FAKEOUT:
   - Candle T-1 closes cleanly beyond a key S/R level (Breakout candle).
   - Candle T immediately reverses and closes cleanly back on the opposite side of the level.
   - Confirmation trigger: Subsequent candle closes beyond the structure extreme
     (below the structure low for resistance fakeout / above structure high for support fakeout).
   - Stop-Loss: Placed at the highest high (resistance) or lowest low (support) of the fakeout structure.
   - Target 1: Minimum 1:1 Risk-to-Reward (partial 50% booked, SL trailed to breakeven).
   - Target 2: Origin of the impulse move (prior swing pivot before the test).

2. ROUNDED FAKEOUT (With Market Structure Shift — MSS):
   - Price penetrates a key S/R level and prints multiple candles (2 to 10) forming a curved/rounded arc.
   - Traps breakout traders and retest traders.
   - Price fails to expand, curls back, and closes inside the S/R level.
   - Confirmation trigger (MSS): A candle breaks and closes beyond the internal swing pivot
     formed during the rounded consolidation.
   - Stop-Loss: The absolute peak (resistance) or trough (support) of the rounded arc.
   - Target 1: Minimum 1:1 Risk-to-Reward.
   - Target 2: Origin of the move prior to breakout initiation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

import config

# ── Tunable Thresholds ────────────────────────────────────────────────────────
MIN_RISK_PTS = 5.0        # Minimum SL distance (avoid tick noise)
MAX_RISK_PTS = 60.0       # Maximum SL distance on Nifty (avoid giant candle trap)
DEFAULT_MAX_ENTRY_TIME = time(14, 30)  # No new entries after 14:30 IST
BUFFER_PTS = 1.0          # Buffer added to SL extreme


@dataclass
class FakeoutSignal:
    timestamp: datetime
    setup_type: str        # IMMEDIATE_FAKEOUT | ROUNDED_FAKEOUT
    direction: str         # BULLISH | BEARISH
    action: str            # BUY_CE | BUY_PE / SHORT
    level_name: str        # PDH | PDL | PDC | MORNING_HIGH | MORNING_LOW | CUSTOM
    level_price: float
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_pts: float
    reward_t1_pts: float
    reward_t2_pts: float
    mss_level: Optional[float] = None
    structure_extreme: float = 0.0
    notes: str = ""

    @property
    def rr_target_1(self) -> float:
        return 1.0 if self.risk_pts <= 0 else self.reward_t1_pts / self.risk_pts

    @property
    def rr_target_2(self) -> float:
        return 0.0 if self.risk_pts <= 0 else self.reward_t2_pts / self.risk_pts

    def summary(self) -> str:
        arrow = "🟢 CALL (BUY CE)" if self.direction == "BULLISH" else "🔴 PUT (BUY PE)"
        return (
            f"[{self.timestamp:%H:%M}] {arrow} | {self.setup_type} at {self.level_name} ({self.level_price:,.1f})\n"
            f"  Entry: {self.entry_price:,.1f} | SL: {self.stop_loss:,.1f} (Risk: {self.risk_pts:,.1f} pts)\n"
            f"  T1 (1:1): {self.target_1:,.1f} | T2 (Origin): {self.target_2:,.1f} (R:R {self.rr_target_2:.2f})\n"
            f"  {self.notes}"
        )


def _times(df: pd.DataFrame) -> pd.Series:
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    return pd.Series(pd.to_datetime(df.index), index=df.index)


def detect_immediate_fakeout(
    df: pd.DataFrame,
    level: float,
    level_type: str,
    level_name: str = "KEY_LEVEL",
    check_index: int = -1,
    max_confirmation_bars: int = 3,
) -> Optional[FakeoutSignal]:
    """
    Evaluates whether an Immediate Fakeout occurred around candle `check_index`.
    """
    if len(df) < 5:
        return None

    idx = len(df) - 1 if check_index == -1 else check_index
    if idx < 2:
        return None

    c_prev = df.iloc[idx - 1]
    c_curr = df.iloc[idx]
    ts = _times(df)

    if level_type == "RESISTANCE":
        # Candle T-1 closes above resistance, Candle T closes back below
        if c_prev["close"] > level and c_curr["close"] < level:
            struct_high = max(float(c_prev["high"]), float(c_curr["high"]))
            struct_low = min(float(c_prev["low"]), float(c_curr["low"]))

            # Check if Candle T itself closed below struct_low
            conf_idx = None
            if c_curr["close"] < c_prev["low"]:
                conf_idx = idx
            else:
                for k in range(idx + 1, min(idx + 1 + max_confirmation_bars, len(df))):
                    if df.iloc[k]["high"] > struct_high:
                        return None  # Invalidation: broke above fakeout high
                    if df.iloc[k]["close"] < struct_low:
                        conf_idx = k
                        break

            if conf_idx is not None and conf_idx == idx:
                entry = float(df.iloc[conf_idx]["close"])
                sl = struct_high + BUFFER_PTS
                risk = sl - entry
                if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                    t1 = entry - risk
                    origin_low = float(df.iloc[max(0, idx - 15) : idx - 1]["low"].min())
                    t2 = origin_low if origin_low < entry - risk else entry - 2 * risk

                    return FakeoutSignal(
                        timestamp=ts.iloc[conf_idx].to_pydatetime(),
                        setup_type="IMMEDIATE_FAKEOUT",
                        direction="BEARISH",
                        action="BUY_PE",
                        level_name=level_name,
                        level_price=level,
                        entry_price=entry,
                        stop_loss=sl,
                        target_1=t1,
                        target_2=t2,
                        risk_pts=risk,
                        reward_t1_pts=risk,
                        reward_t2_pts=entry - t2,
                        structure_extreme=struct_high,
                        notes=f"Failed breakout above {level_name} ({level:,.1f}), reversal confirmed below {struct_low:,.1f}",
                    )

    elif level_type == "SUPPORT":
        # Candle T-1 closes below support, Candle T closes back above
        if c_prev["close"] < level and c_curr["close"] > level:
            struct_low = min(float(c_prev["low"]), float(c_curr["low"]))
            struct_high = max(float(c_prev["high"]), float(c_curr["high"]))

            conf_idx = None
            if c_curr["close"] > c_prev["high"]:
                conf_idx = idx
            else:
                for k in range(idx + 1, min(idx + 1 + max_confirmation_bars, len(df))):
                    if df.iloc[k]["low"] < struct_low:
                        return None  # Invalidation: broke below fakeout low
                    if df.iloc[k]["close"] > struct_high:
                        conf_idx = k
                        break

            if conf_idx is not None and conf_idx == idx:
                entry = float(df.iloc[conf_idx]["close"])
                sl = struct_low - BUFFER_PTS
                risk = entry - sl
                if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                    t1 = entry + risk
                    origin_high = float(df.iloc[max(0, idx - 15) : idx - 1]["high"].max())
                    t2 = origin_high if origin_high > entry + risk else entry + 2 * risk

                    return FakeoutSignal(
                        timestamp=ts.iloc[conf_idx].to_pydatetime(),
                        setup_type="IMMEDIATE_FAKEOUT",
                        direction="BULLISH",
                        action="BUY_CE",
                        level_name=level_name,
                        level_price=level,
                        entry_price=entry,
                        stop_loss=sl,
                        target_1=t1,
                        target_2=t2,
                        risk_pts=risk,
                        reward_t1_pts=risk,
                        reward_t2_pts=t2 - entry,
                        structure_extreme=struct_low,
                        notes=f"Failed breakdown below {level_name} ({level:,.1f}), reversal confirmed above {struct_high:,.1f}",
                    )

    return None


def detect_rounded_fakeout(
    df: pd.DataFrame,
    level: float,
    level_type: str,
    level_name: str = "KEY_LEVEL",
    check_index: int = -1,
    min_candles: int = 2,
    max_candles: int = 10,
) -> Optional[FakeoutSignal]:
    """
    Evaluates whether a Rounded Fakeout with Market Structure Shift (MSS) confirmed at candle `check_index`.
    """
    if len(df) < 6:
        return None

    idx = len(df) - 1 if check_index == -1 else check_index
    ts = _times(df)

    if level_type == "RESISTANCE":
        for start_i in range(max(0, idx - max_candles), idx - min_candles):
            if df.iloc[start_i]["close"] > level:
                peak_high = float(df.iloc[start_i]["high"])
                internal_lows = []
                curled_idx = -1

                for j in range(start_i + 1, idx + 1):
                    h = float(df.iloc[j]["high"])
                    c = float(df.iloc[j]["close"])
                    if h > peak_high:
                        peak_high = h
                    if j > start_i + 1:
                        internal_lows.append(float(df.iloc[j - 1]["low"]))
                    if c < level and (j - start_i >= min_candles):
                        curled_idx = j
                        break

                if curled_idx != -1 and internal_lows:
                    mss_level = min(internal_lows)
                    if (
                        df.iloc[idx]["close"] < mss_level
                        and df.iloc[idx]["high"] <= peak_high
                        and (idx - curled_idx <= 4)
                    ):
                        entry = float(df.iloc[idx]["close"])
                        sl = peak_high + BUFFER_PTS
                        risk = sl - entry
                        if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                            t1 = entry - risk
                            origin_low = float(df.iloc[max(0, start_i - 15) : start_i]["low"].min())
                            t2 = origin_low if origin_low < entry - risk else entry - 2 * risk

                            return FakeoutSignal(
                                timestamp=ts.iloc[idx].to_pydatetime(),
                                setup_type="ROUNDED_FAKEOUT",
                                direction="BEARISH",
                                action="BUY_PE",
                                level_name=level_name,
                                level_price=level,
                                entry_price=entry,
                                stop_loss=sl,
                                target_1=t1,
                                target_2=t2,
                                risk_pts=risk,
                                reward_t1_pts=risk,
                                reward_t2_pts=entry - t2,
                                mss_level=mss_level,
                                structure_extreme=peak_high,
                                notes=f"Rounded top over {level_name} ({level:,.1f}). MSS confirmed below swing low {mss_level:,.1f}",
                            )

    elif level_type == "SUPPORT":
        for start_i in range(max(0, idx - max_candles), idx - min_candles):
            if df.iloc[start_i]["close"] < level:
                trough_low = float(df.iloc[start_i]["low"])
                internal_highs = []
                curled_idx = -1

                for j in range(start_i + 1, idx + 1):
                    l = float(df.iloc[j]["low"])
                    c = float(df.iloc[j]["close"])
                    if l < trough_low:
                        trough_low = l
                    if j > start_i + 1:
                        internal_highs.append(float(df.iloc[j - 1]["high"]))
                    if c > level and (j - start_i >= min_candles):
                        curled_idx = j
                        break

                if curled_idx != -1 and internal_highs:
                    mss_level = max(internal_highs)
                    if (
                        df.iloc[idx]["close"] > mss_level
                        and df.iloc[idx]["low"] >= trough_low
                        and (idx - curled_idx <= 4)
                    ):
                        entry = float(df.iloc[idx]["close"])
                        sl = trough_low - BUFFER_PTS
                        risk = entry - sl
                        if MIN_RISK_PTS <= risk <= MAX_RISK_PTS:
                            t1 = entry + risk
                            origin_high = float(df.iloc[max(0, start_i - 15) : start_i]["high"].max())
                            t2 = origin_high if origin_high > entry + risk else entry + 2 * risk

                            return FakeoutSignal(
                                timestamp=ts.iloc[idx].to_pydatetime(),
                                setup_type="ROUNDED_FAKEOUT",
                                direction="BULLISH",
                                action="BUY_CE",
                                level_name=level_name,
                                level_price=level,
                                entry_price=entry,
                                stop_loss=sl,
                                target_1=t1,
                                target_2=t2,
                                risk_pts=risk,
                                reward_t1_pts=risk,
                                reward_t2_pts=t2 - entry,
                                mss_level=mss_level,
                                structure_extreme=trough_low,
                                notes=f"Rounded base under {level_name} ({level:,.1f}). MSS confirmed above swing high {mss_level:,.1f}",
                            )

    return None


def scan_candle_for_fakeouts(
    df: pd.DataFrame,
    levels: Dict[str, Tuple[float, str]],
    check_index: int = -1,
    max_entry_time: time = DEFAULT_MAX_ENTRY_TIME,
) -> List[FakeoutSignal]:
    """
    Scans a completed candle across all active Support/Resistance levels.
    """
    signals: List[FakeoutSignal] = []
    if df is None or len(df) < 5:
        return signals

    idx = len(df) - 1 if check_index == -1 else check_index
    candle_ts = _times(df).iloc[idx]
    if candle_ts.time() > max_entry_time:
        return signals

    for lvl_name, (lvl_val, lvl_type) in levels.items():
        imm = detect_immediate_fakeout(df, lvl_val, lvl_type, lvl_name, check_index=idx)
        if imm:
            signals.append(imm)
            continue

        rnd = detect_rounded_fakeout(df, lvl_val, lvl_type, lvl_name, check_index=idx)
        if rnd:
            signals.append(rnd)

    return signals
