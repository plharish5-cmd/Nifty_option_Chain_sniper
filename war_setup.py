"""
war_setup.py — WAR SETUP: The 30-Min First-Candle FII Strategy for NIFTY 50.

Core Logic:
───────────
Follow the FIIs who are active in the first 30 minutes of the day (09:15 - 09:45 IST).
The first 30-min candle sets the battle lines — its HIGH and LOW are the primary triggers.

The 5 Core Scenarios:
─────────────────────
1. HUGE GAP DOWN (> 100 pts) / GAP DOWN:
   - First 30-min candle HIGH broken at any time in the day -> BUY CALL.
   - Market typically closes near day high (short squeeze / gap fill).

2. GAP DOWN DAY:
   - First 30-min candle LOW broken -> DO NOT sell / buy PE on breakdown.
   - Wait for Bullish Reversal pattern (Hammer OR Bullish Engulfing) -> BUY CALL.

3. GAP UP DAY:
   - First 30-min candle LOW broken -> BUY PUT.
   - Market typically closes near day low (long liquidation / gap fill).

4. GAP UP DAY:
   - First 30-min candle HIGH broken -> DO NOT buy CE on breakout.
   - Wait for Bearish Reversal pattern (Inverted Hammer OR Bearish Engulfing) -> BUY PUT.

5. DOJI FIRST 30-MIN CANDLE (either gap):
   - Trade Doji breakout: High break -> BUY CALL, Low break -> BUY PUT.

The 12:15 PM Reversal Window:
─────────────────────────────
Between 11:45 and 13:15, a reversal opportunity often forms (Bearish/Bullish Engulfing,
or the opposite 30-min boundary breaking), offering a second trade or exit/flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

import config


# ── Constants ─────────────────────────────────────────────────────────────────
CANDLE_30M_BARS = 6             # Six 5-min bars = 30 minutes (09:15 - 09:45)
DOJI_BODY_RATIO_MAX = 0.28      # Body <= 28% of total range = Doji
HUGE_GAP_PTS = 100.0            # >= 100 pts gap
MIN_GAP_PTS = 20.0              # < 20 pts is considered neutral open
TARGET_SCALP_PTS = 60.0         # Primary target from student notes
TARGET_RUNNER_PTS = 120.0       # Secondary target
REVERSAL_WINDOW_START = time(11, 45)
REVERSAL_WINDOW_END = time(13, 15)


@dataclass
class WARSignal:
    timestamp: datetime
    date_str: str
    scenario: str
    direction: str             # BUY_CE | BUY_PE
    action_desc: str
    entry_price: float
    target_60: float
    target_120: float
    target_160: float
    gap_pts: float
    h30: float
    l30: float
    is_doji: bool
    is_midday_reversal: bool = False

    def summary(self) -> str:
        arrow = "🟢 CALL (BUY CE)" if self.direction == "BUY_CE" else "🔴 PUT (BUY PE)"
        rev = " [12:15 REVERSAL]" if self.is_midday_reversal else ""
        return (
            f"[{self.timestamp:%H:%M}] {arrow}{rev} | {self.scenario}\n"
            f"  Entry: {self.entry_price:,.1f} | T1 (+60p): {self.target_60:,.1f} | T2 (+120p): {self.target_120:,.1f}\n"
            f"  Gap: {self.gap_pts:+.1f}p | 30m Range: [{self.l30:,.1f} - {self.h30:,.1f}] (Doji: {self.is_doji})\n"
            f"  Trigger: {self.action_desc}"
        )


def is_hammer(row: pd.Series, prev_row: Optional[pd.Series] = None) -> bool:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= 1.8 * body and upper_wick <= 0.25 * rng and c > l + 0.5 * rng


def is_bullish_engulfing(row: pd.Series, prev_row: Optional[pd.Series]) -> bool:
    if prev_row is None:
        return False
    cur_green = float(row["close"]) > float(row["open"])
    prev_red = float(prev_row["close"]) < float(prev_row["open"])
    if not (cur_green and prev_red):
        return False
    return float(row["close"]) >= float(prev_row["open"]) and float(row["open"]) <= float(prev_row["close"])


def is_inverted_hammer(row: pd.Series, prev_row: Optional[pd.Series] = None) -> bool:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return upper_wick >= 1.8 * body and lower_wick <= 0.25 * rng and c < h - 0.5 * rng


def is_bearish_engulfing(row: pd.Series, prev_row: Optional[pd.Series]) -> bool:
    if prev_row is None:
        return False
    cur_red = float(row["close"]) < float(row["open"])
    prev_green = float(prev_row["close"]) > float(prev_row["open"])
    if not (cur_red and prev_green):
        return False
    return float(row["open"]) >= float(prev_row["close"]) and float(row["close"]) <= float(prev_row["open"])


def extract_30m_candle(df_5m: pd.DataFrame) -> Optional[Dict[str, float]]:
    """
    Extracts the first 30-minute candle (09:15 - 09:45) from 5m data.
    """
    if len(df_5m) < CANDLE_30M_BARS:
        return None
    sub = df_5m.iloc[0:CANDLE_30M_BARS]
    o = float(sub.iloc[0]["open"])
    h = float(sub["high"].max())
    l = float(sub["low"].min())
    c = float(sub.iloc[-1]["close"])
    body = abs(c - o)
    rng = h - l
    is_doji = (body <= DOJI_BODY_RATIO_MAX * rng) if rng > 0 else True
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "range": rng,
        "body": body,
        "is_doji": is_doji,
    }


def scan_war_signals(df_5m: pd.DataFrame, pdc: float, date_str: str) -> List[WARSignal]:
    """
    Evaluates an entire session of 5-minute candles and identifies all WAR signals.
    """
    signals: List[WARSignal] = []
    c30 = extract_30m_candle(df_5m)
    if not c30:
        return signals

    o30, h30, l30 = c30["open"], c30["high"], c30["low"]
    is_doji = c30["is_doji"]
    gap = o30 - pdc

    is_huge_gap_down = gap <= -HUGE_GAP_PTS
    is_gap_down = gap < -MIN_GAP_PTS
    is_huge_gap_up = gap >= HUGE_GAP_PTS
    is_gap_up = gap > MIN_GAP_PTS
    is_neutral = abs(gap) <= MIN_GAP_PTS

    primary_triggered = False
    reversal_triggered = False

    for i in range(CANDLE_30M_BARS, len(df_5m)):
        row = df_5m.iloc[i]
        prev_row = df_5m.iloc[i - 1] if i > 0 else None
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        t = pd.to_datetime(row["timestamp"])
        t_time = t.time()

        # SCENARIO 5: DOJI First Candle
        if is_doji and not primary_triggered:
            if h > h30:
                entry = max(c, h30)
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario="SCENARIO_5_DOJI",
                    direction="BUY_CE",
                    action_desc=f"Doji High {h30:,.1f} taken out",
                    entry_price=entry,
                    target_60=entry + TARGET_SCALP_PTS,
                    target_120=entry + TARGET_RUNNER_PTS,
                    target_160=entry + 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=True,
                )
                signals.append(sig)
                primary_triggered = True
                continue
            elif l < l30:
                entry = min(c, l30)
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario="SCENARIO_5_DOJI",
                    direction="BUY_PE",
                    action_desc=f"Doji Low {l30:,.1f} taken out",
                    entry_price=entry,
                    target_60=entry - TARGET_SCALP_PTS,
                    target_120=entry - TARGET_RUNNER_PTS,
                    target_160=entry - 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=True,
                )
                signals.append(sig)
                primary_triggered = True
                continue

        # SCENARIO 1: Huge Gap Down / Gap Down -> 30m High broken -> BUY CALL
        if is_gap_down and not primary_triggered:
            if h > h30:
                entry = max(c, h30)
                sc_name = "SCENARIO_1_HUGE_GAP_DOWN" if is_huge_gap_down else "SCENARIO_1_GAP_DOWN_HIGH_BREAK"
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario=sc_name,
                    direction="BUY_CE",
                    action_desc=f"Gap Down ({gap:+.1f}p), 30m High {h30:,.1f} broken",
                    entry_price=entry,
                    target_60=entry + TARGET_SCALP_PTS,
                    target_120=entry + TARGET_RUNNER_PTS,
                    target_160=entry + 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=False,
                )
                signals.append(sig)
                primary_triggered = True
                continue

        # SCENARIO 2: Gap Down -> 30m Low broken -> Wait for Hammer / Bullish Engulfing -> BUY CALL
        if is_gap_down and not primary_triggered:
            if l < l30:
                if is_hammer(row, prev_row) or is_bullish_engulfing(row, prev_row):
                    entry = c
                    sig = WARSignal(
                        timestamp=t.to_pydatetime(),
                        date_str=date_str,
                        scenario="SCENARIO_2_GAP_DOWN_REVERSAL",
                        direction="BUY_CE",
                        action_desc=f"Gap Down ({gap:+.1f}p), 30m Low broken, Bullish Reversal candle",
                        entry_price=entry,
                        target_60=entry + TARGET_SCALP_PTS,
                        target_120=entry + TARGET_RUNNER_PTS,
                        target_160=entry + 160.0,
                        gap_pts=gap,
                        h30=h30,
                        l30=l30,
                        is_doji=False,
                    )
                    signals.append(sig)
                    primary_triggered = True
                    continue

        # SCENARIO 3: Gap Up -> 30m Low broken -> BUY PUT
        if is_gap_up and not primary_triggered:
            if l < l30:
                entry = min(c, l30)
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario="SCENARIO_3_GAP_UP_LOW_BREAK",
                    direction="BUY_PE",
                    action_desc=f"Gap Up ({gap:+.1f}p), 30m Low {l30:,.1f} broken",
                    entry_price=entry,
                    target_60=entry - TARGET_SCALP_PTS,
                    target_120=entry - TARGET_RUNNER_PTS,
                    target_160=entry - 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=False,
                )
                signals.append(sig)
                primary_triggered = True
                continue

        # SCENARIO 4: Gap Up -> 30m High broken -> Wait for Inverted Hammer / Bearish Engulfing -> BUY PUT
        if is_gap_up and not primary_triggered:
            if h > h30:
                if is_inverted_hammer(row, prev_row) or is_bearish_engulfing(row, prev_row):
                    entry = c
                    sig = WARSignal(
                        timestamp=t.to_pydatetime(),
                        date_str=date_str,
                        scenario="SCENARIO_4_GAP_UP_REVERSAL",
                        direction="BUY_PE",
                        action_desc=f"Gap Up ({gap:+.1f}p), 30m High broken, Bearish Reversal candle",
                        entry_price=entry,
                        target_60=entry - TARGET_SCALP_PTS,
                        target_120=entry - TARGET_RUNNER_PTS,
                        target_160=entry - 160.0,
                        gap_pts=gap,
                        h30=h30,
                        l30=l30,
                        is_doji=False,
                    )
                    signals.append(sig)
                    primary_triggered = True
                    continue

        # NEUTRAL OPEN: Break of 30m High -> BUY CALL, Break of 30m Low -> BUY PUT
        if is_neutral and not primary_triggered:
            if h > h30:
                entry = max(c, h30)
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario="NEUTRAL_OPEN_BREAK",
                    direction="BUY_CE",
                    action_desc=f"Neutral Open ({gap:+.1f}p), 30m High {h30:,.1f} broken",
                    entry_price=entry,
                    target_60=entry + TARGET_SCALP_PTS,
                    target_120=entry + TARGET_RUNNER_PTS,
                    target_160=entry + 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=False,
                )
                signals.append(sig)
                primary_triggered = True
                continue
            elif l < l30:
                entry = min(c, l30)
                sig = WARSignal(
                    timestamp=t.to_pydatetime(),
                    date_str=date_str,
                    scenario="NEUTRAL_OPEN_BREAK",
                    direction="BUY_PE",
                    action_desc=f"Neutral Open ({gap:+.1f}p), 30m Low {l30:,.1f} broken",
                    entry_price=entry,
                    target_60=entry - TARGET_SCALP_PTS,
                    target_120=entry - TARGET_RUNNER_PTS,
                    target_160=entry - 160.0,
                    gap_pts=gap,
                    h30=h30,
                    l30=l30,
                    is_doji=False,
                )
                signals.append(sig)
                primary_triggered = True
                continue

        # THE 12:15 PM REVERSAL WINDOW (Rule 8)
        if primary_triggered and not reversal_triggered and (REVERSAL_WINDOW_START <= t_time <= REVERSAL_WINDOW_END):
            first_sig = signals[-1]
            if first_sig.direction == "BUY_CE":
                if is_bearish_engulfing(row, prev_row) or l < l30:
                    entry = c
                    sig = WARSignal(
                        timestamp=t.to_pydatetime(),
                        date_str=date_str,
                        scenario="MIDDAY_1215_REVERSAL",
                        direction="BUY_PE",
                        action_desc="12:15 Mid-day Bearish Reversal / 30m Low breakdown",
                        entry_price=entry,
                        target_60=entry - TARGET_SCALP_PTS,
                        target_120=entry - TARGET_RUNNER_PTS,
                        target_160=entry - 160.0,
                        gap_pts=gap,
                        h30=h30,
                        l30=l30,
                        is_doji=is_doji,
                        is_midday_reversal=True,
                    )
                    signals.append(sig)
                    reversal_triggered = True
            elif first_sig.direction == "BUY_PE":
                if is_bullish_engulfing(row, prev_row) or h > h30:
                    entry = c
                    sig = WARSignal(
                        timestamp=t.to_pydatetime(),
                        date_str=date_str,
                        scenario="MIDDAY_1215_REVERSAL",
                        direction="BUY_CE",
                        action_desc="12:15 Mid-day Bullish Reversal / 30m High breakout",
                        entry_price=entry,
                        target_60=entry + TARGET_SCALP_PTS,
                        target_120=entry + TARGET_RUNNER_PTS,
                        target_160=entry + 160.0,
                        gap_pts=gap,
                        h30=h30,
                        l30=l30,
                        is_doji=is_doji,
                        is_midday_reversal=True,
                    )
                    signals.append(sig)
                    reversal_triggered = True

    return signals
