"""
session_reversal_matrix.py — Session Reversal Matrix (15m) Engine.

Transcribed faithfully from Mentor Rules:
"Stick strictly to the 15-minute timeframe. Ignore the 5-minute chart.
In 5 minutes, you will see random Dojis and jump into bad trades prematurely.
Observe and execute on 15 minutes only."

Contains the 10 core setups:
PART 1: Upside / Short Setups (Resistance & Reversal)
  1. Point 1 — Gap-Up Days (Doji / Inverted Hammer at Day High)
  2. Point 2 — M-Pattern Top (Double Top / Exhaustion with 2nd Peak Doji)
  3. Point 3 — Option Chain Resistance Confluence (Call OI writing wall)
  4. Point 4 — Major Round Number Levels (Multiples of 500 / 1,000)
  5. Point 5 — Double Reversal Candles near Day High (Double Doji / Inv Hammer)

PART 2: Downside / Long Setups (Support & Reversal)
  1. Point 1 — Gap-Down Days (Hammer at Day Low)
  2. Point 2 — W-Pattern / Double Bottom (2nd Swing Low Hammer / Doji)
  3. Point 3 — Option Chain Support Confluence (Put OI writing wall)
  4. Point 4 — Round Numbers (Multiples of 500 / 1,000 Support)
  5. Point 5 — Double Reversal Candles near Day Low (Double Hammer / Double Doji)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config

# ── Load thresholds from rules.json ──────────────────────────────────────
_RULES_PATH = Path(__file__).parent / "rules.json"

DEFAULT_PARAMS = {
    "gap_pts": 25.0,
    "round_number_step": 500,
    "round_proximity_pts": 25.0,
    "extremum_proximity_pts": 25.0,
    "chain_wall_proximity_pts": 25.0,
    "doji_body_pct": 0.12,
    "hammer_wick_ratio": 2.0,
    "hammer_opp_ratio": 0.6,
    "inverted_hammer_wick_ratio": 2.0,
    "inverted_hammer_opp_ratio": 0.6,
    "engulf_ratio": 1.1,
    "default_sl_buffer_pts": 1.0,
}


def load_matrix_params() -> dict:
    params = dict(DEFAULT_PARAMS)
    try:
        if _RULES_PATH.exists():
            data = json.loads(_RULES_PATH.read_text())
            matrix_block = data.get("session_reversal_matrix", {})
            for k, v in matrix_block.items():
                if not k.startswith("_"):
                    params[k] = v
    except Exception:
        pass
    return params


# ═══════════════════════════════════════════════════════════════════════════
# Candle geometry & Pattern helpers (15m strict)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Candle15m:
    i: int
    ts: pd.Timestamp
    o: float
    h: float
    l: float
    c: float
    closed: bool = True

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def rng(self) -> float:
        return max(self.h - self.l, 0.001)

    @property
    def upper(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def bull(self) -> bool:
        return self.c > self.o

    @property
    def bear(self) -> bool:
        return self.c < self.o


def _is_doji(k: Candle15m, p: dict) -> bool:
    return k.rng > 0 and (k.body <= p["doji_body_pct"] * k.rng)


def _is_hammer(k: Candle15m, p: dict) -> bool:
    if k.rng <= 0 or k.body <= 0:
        return False
    return (k.lower >= p["hammer_wick_ratio"] * k.body and
            k.upper <= p["hammer_opp_ratio"] * k.body)


def _is_inverted_hammer(k: Candle15m, p: dict) -> bool:
    if k.rng <= 0 or k.body <= 0:
        return False
    return (k.upper >= p["inverted_hammer_wick_ratio"] * k.body and
            k.lower <= p["inverted_hammer_opp_ratio"] * k.body)


def _is_bullish_engulfing(cur: Candle15m, prev: Candle15m, p: dict) -> bool:
    if prev.body <= 0 or cur.body < p["engulf_ratio"] * prev.body:
        return False
    return cur.bull and prev.bear and (cur.c >= prev.o) and (cur.o <= prev.c)


def _is_bearish_engulfing(cur: Candle15m, prev: Candle15m, p: dict) -> bool:
    if prev.body <= 0 or cur.body < p["engulf_ratio"] * prev.body:
        return False
    return cur.bear and prev.bull and (cur.o >= prev.c) and (cur.c <= prev.o)


# ═══════════════════════════════════════════════════════════════════════════
# Reversal Setup Representation
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ReversalSetup:
    id: str
    name: str
    side: str                          # "SHORT" or "LONG"
    category: str                      # "Resistance & Reversal" or "Support & Reversal"
    status: str                        # "TRIGGERED", "ARMED", "WATCH", "INACTIVE"
    badge_color: str                   # "#2FBF71", "#E8503A", "#C9A227", "#8FA6B8"
    candle_idx: int | None = None
    candle_ts: pd.Timestamp | None = None
    candle_pattern: str = ""
    trigger_price: float | None = None
    stop_loss: float | None = None
    predefined_risk: float | None = None
    target_price: float | None = None
    rr_ratio: float | None = None
    confluence_detail: str = ""
    mentor_rule: str = ""
    mentor_note: str = ""
    execution_guidance: str = ""
    triggered_candle_ts: pd.Timestamp | None = None
    outcome: str = "INACTIVE"          # "TRIGGERED", "ARMED", "INVALIDATED", "TARGET_HIT", "INACTIVE"


@dataclass
class SessionMatrixResult:
    timeframe: str
    eval_time: datetime
    today_date: date | None
    prev_close: float | None
    today_open: float | None
    day_high: float | None
    day_low: float | None
    gap_points: float | None
    gap_type: str                     # "GAP_UP", "GAP_DOWN", "FLAT"
    setups: list[ReversalSetup]
    active_count: int
    short_triggered_count: int
    long_triggered_count: int
    discipline_rule: str = (
        "Stick strictly to the 15-minute timeframe. Ignore the 5-minute chart. "
        "In 5 minutes, you will see random Dojis and jump into bad trades prematurely. "
        "Observe and execute on 15 minutes only."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation Engine
# ═══════════════════════════════════════════════════════════════════════════

def _extract_candles(df: pd.DataFrame) -> list[Candle15m]:
    if df is None or df.empty:
        return []
    candles = []
    has_closed_col = "closed" in df.columns
    for i in range(len(df)):
        r = df.iloc[i]
        ts = r["timestamp"] if "timestamp" in df.columns else df.index[i]
        is_closed = bool(r["closed"]) if has_closed_col else True
        candles.append(Candle15m(
            i=i,
            ts=pd.to_datetime(ts),
            o=float(r["open"]),
            h=float(r["high"]),
            l=float(r["low"]),
            c=float(r["close"]),
            closed=is_closed
        ))
    return candles


def evaluate_session_reversals(
    df_15m: pd.DataFrame | None,
    prev_close: float | None = None,
    spot: float | None = None,
    levels: Any = None,
    chain_df: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> SessionMatrixResult:
    """
    Evaluates the 10 Mentor reversal conditions strictly on closed 15m candles.
    """
    params = load_matrix_params()
    eval_time = now or datetime.now()

    if df_15m is None or df_15m.empty:
        return SessionMatrixResult(
            timeframe="15m",
            eval_time=eval_time,
            today_date=None,
            prev_close=prev_close,
            today_open=None,
            day_high=None,
            day_low=None,
            gap_points=None,
            gap_type="UNKNOWN",
            setups=[],
            active_count=0,
            short_triggered_count=0,
            long_triggered_count=0,
        )

    all_candles = _extract_candles(df_15m)
    closed_candles = [c for c in all_candles if c.closed]

    if not closed_candles:
        closed_candles = all_candles[:-1] if len(all_candles) > 1 else all_candles

    if not closed_candles:
        return SessionMatrixResult(
            timeframe="15m",
            eval_time=eval_time,
            today_date=None,
            prev_close=prev_close,
            today_open=None,
            day_high=None,
            day_low=None,
            gap_points=None,
            gap_type="UNKNOWN",
            setups=[],
            active_count=0,
            short_triggered_count=0,
            long_triggered_count=0,
        )

    today_dt = closed_candles[-1].ts.date()
    today_candles = [c for c in closed_candles if c.ts.date() == today_dt]
    if not today_candles:
        today_candles = closed_candles

    today_open = today_candles[0].o if today_candles else None
    day_high = max(c.h for c in today_candles) if today_candles else None
    day_low = min(c.l for c in today_candles) if today_candles else None

    # Derive previous close if not directly provided
    if prev_close is None:
        prior_candles = [c for c in closed_candles if c.ts.date() < today_dt]
        if prior_candles:
            prev_close = prior_candles[-1].c

    # Gap determination
    gap_points = (today_open - prev_close) if (today_open is not None and prev_close is not None) else None
    min_gap = params["gap_pts"]
    if gap_points is not None:
        if gap_points >= min_gap:
            gap_type = "GAP_UP"
        elif gap_points <= -min_gap:
            gap_type = "GAP_DOWN"
        else:
            gap_type = "FLAT"
    else:
        gap_type = "UNKNOWN"

    # Option chain levels extract
    r1_val = getattr(levels, "r1", None)
    r2_val = getattr(levels, "r2", None)
    s1_val = getattr(levels, "s1", None)
    s2_val = getattr(levels, "s2", None)

    max_call_wall = None
    max_put_wall = None
    if chain_df is not None and not chain_df.empty:
        try:
            if "ce_oi" in chain_df.columns:
                max_call_wall = float(chain_df.loc[chain_df["ce_oi"].idxmax()]["strike"])
            if "pe_oi" in chain_df.columns:
                max_put_wall = float(chain_df.loc[chain_df["pe_oi"].idxmax()]["strike"])
        except Exception:
            pass

    key_resistances = [float(x) for x in [r1_val, r2_val, max_call_wall] if x is not None]
    key_supports = [float(x) for x in [s1_val, s2_val, max_put_wall] if x is not None]

    setups: list[ReversalSetup] = []

    # ─────────────────────────────────────────────────────────────────────────
    # PART 1: UPSIDE / SHORT SETUPS (RESISTANCE & REVERSAL)
    # ─────────────────────────────────────────────────────────────────────────

    # --- Short Setup 1: Gap-Up Days (Doji / Inverted Hammer at Day High) ---
    s1_status = "INACTIVE"
    s1_candle = None
    s1_trigger = None
    s1_sl = None
    s1_risk = None
    s1_target = None
    s1_pat = ""
    s1_detail = ""
    s1_trig_ts = None

    if gap_type == "GAP_UP":
        for idx, k in enumerate(today_candles):
            is_dj = _is_doji(k, params)
            is_ih = _is_inverted_hammer(k, params)
            if (is_dj or is_ih) and (day_high is not None) and (abs(k.h - day_high) <= params["extremum_proximity_pts"]):
                s1_candle = k
                s1_pat = "Doji" if is_dj else "Inverted Hammer"
                s1_trigger = round(k.l - 0.5, 2)
                s1_sl = round(k.h + params["default_sl_buffer_pts"], 2)
                s1_risk = round(s1_sl - s1_trigger, 2)
                s1_target = round(today_open if today_open else (s1_trigger - 2.0 * s1_risk), 2)
                s1_status = "ARMED"
                s1_detail = f"Gap-Up +{gap_points:.1f} pts. {s1_pat} printed near Day High {day_high:,.1f} at {k.ts.strftime('%H:%M')}."

                for later in today_candles[idx + 1:]:
                    if later.l < s1_trigger:
                        if later.h > s1_sl:
                            s1_status = "STOPPED"
                        else:
                            s1_status = "TRIGGERED"
                            s1_trig_ts = later.ts
                        break
                break
    else:
        s1_detail = f"No gap-up today (Gap: {gap_points:+.1f} pts vs threshold +{min_gap:.0f} pts)." if gap_points is not None else "Previous close unknown for gap calculation."

    setups.append(ReversalSetup(
        id="SHORT_1",
        name="Point 1 — Gap-Up Day: Doji / Inverted Hammer at Day High",
        side="SHORT",
        category="Resistance & Reversal",
        status=s1_status,
        badge_color="#E8503A" if s1_status == "TRIGGERED" else ("#C9A227" if s1_status == "ARMED" else "#8FA6B8"),
        candle_idx=s1_candle.i if s1_candle else None,
        candle_ts=s1_candle.ts if s1_candle else None,
        candle_pattern=s1_pat,
        trigger_price=s1_trigger,
        stop_loss=s1_sl,
        predefined_risk=s1_risk,
        target_price=s1_target,
        rr_ratio=round((s1_trigger - s1_target) / s1_risk, 2) if (s1_trigger and s1_target and s1_risk and s1_risk > 0) else None,
        confluence_detail=s1_detail,
        mentor_rule="On gap-up days, identify a Doji or an Inverted Hammer near Day High.",
        mentor_note="Entry Trigger: Low breakdown of that Doji / Inverted Hammer. Risk/Stop Loss: High of the candle. (Predefined tight risk, e.g. ~13 points).",
        execution_guidance="Wait for 15m candle close. Enter only when low breaks. Stop is high of candle.",
        triggered_candle_ts=s1_trig_ts,
        outcome=s1_status,
    ))

    # --- Short Setup 2: M-Pattern Top (Double Top / Exhaustion) ---
    s2_status = "INACTIVE"
    s2_candle = None
    s2_trigger = None
    s2_sl = None
    s2_risk = None
    s2_target = None
    s2_pat = ""
    s2_detail = ""
    s2_trig_ts = None

    if len(today_candles) >= 5:
        highs = [c.h for c in today_candles]
        for i in range(1, len(today_candles) - 1):
            if highs[i] > highs[i - 1] and highs[i] >= highs[i + 1]:
                p1 = today_candles[i]
                for j in range(i + 2, len(today_candles)):
                    p2 = today_candles[j]
                    if abs(p1.h - p2.h) / max(p1.h, 0.01) <= 0.0035:
                        trough_candidates = [c.l for c in today_candles[i + 1:j]]
                        if trough_candidates:
                            neckline_val = min(trough_candidates)
                            if (p1.h - neckline_val) >= 20.0:
                                is_dj = _is_doji(p2, params)
                                is_ih = _is_inverted_hammer(p2, params)
                                if is_dj or is_ih or p2.bear:
                                    s2_candle = p2
                                    s2_pat = "Doji at Peak 2" if is_dj else ("Inverted Hammer at Peak 2" if is_ih else "Rejection at Peak 2")
                                    s2_trigger = round(p2.l - 0.5, 2)
                                    s2_sl = round(max(p1.h, p2.h) + params["default_sl_buffer_pts"], 2)
                                    s2_risk = round(s2_sl - s2_trigger, 2)
                                    neck_proj = neckline_val - (s2_sl - neckline_val)
                                    s2_target = round(neckline_val if neckline_val < s2_trigger else neck_proj, 2)
                                    if s2_target >= s2_trigger:
                                        s2_target = round(s2_trigger - 2.0 * s2_risk, 2)
                                    s2_status = "ARMED"
                                    s2_detail = f"M-Pattern Double Top: P1 {p1.h:,.1f} ({p1.ts.strftime('%H:%M')}) & P2 {p2.h:,.1f} ({p2.ts.strftime('%H:%M')}). Neckline at {neckline_val:,.1f}."

                                    for later in today_candles[j + 1:]:
                                        if later.l < s2_trigger:
                                            s2_status = "TRIGGERED"
                                            s2_trig_ts = later.ts
                                            break
                                    break
                if s2_candle:
                    break

    if not s2_candle:
        s2_detail = "No M-Pattern Double Top with 2nd peak Doji/reversal confirmed in today's 15m session."

    setups.append(ReversalSetup(
        id="SHORT_2",
        name="Point 2 — M-Pattern Top (Double Top / Exhaustion)",
        side="SHORT",
        category="Resistance & Reversal",
        status=s2_status,
        badge_color="#E8503A" if s2_status == "TRIGGERED" else ("#C9A227" if s2_status == "ARMED" else "#8FA6B8"),
        candle_idx=s2_candle.i if s2_candle else None,
        candle_ts=s2_candle.ts if s2_candle else None,
        candle_pattern=s2_pat,
        trigger_price=s2_trigger,
        stop_loss=s2_sl,
        predefined_risk=s2_risk,
        target_price=s2_target,
        rr_ratio=round((s2_trigger - s2_target) / s2_risk, 2) if (s2_trigger and s2_target and s2_risk and s2_risk > 0) else None,
        confluence_detail=s2_detail,
        mentor_rule="Price rises from normal levels, faces resistance near ATM strike, and pushes up again to test the zone.",
        mentor_note="Rule: If a Doji forms at the top of the second peak of an M-pattern, it signals the end of the uptrend. Target: Minimum retracement back to the neckline/origin. Note: Early high-R:R entry at 2nd peak rather than waiting for neckline break.",
        execution_guidance="Standard textbook waits for neckline breakdown, but Mentor rule enters early on second peak Doji low breakdown for maximum R:R.",
        triggered_candle_ts=s2_trig_ts,
        outcome=s2_status,
    ))

    # --- Short Setup 3: Option Chain Resistance Confluence ---
    s3_status = "INACTIVE"
    s3_candle = None
    s3_trigger = None
    s3_sl = None
    s3_risk = None
    s3_target = None
    s3_pat = ""
    s3_detail = ""
    s3_trig_ts = None

    if key_resistances:
        for idx, k in enumerate(today_candles):
            is_ih = _is_inverted_hammer(k, params)
            is_dj = _is_doji(k, params)
            is_eng = (idx > 0 and _is_bearish_engulfing(k, today_candles[idx - 1], params)) if idx > 0 else False
            if is_ih or is_dj or is_eng:
                for r_lvl in key_resistances:
                    if abs(k.h - r_lvl) <= params["chain_wall_proximity_pts"]:
                        s3_candle = k
                        s3_pat = "Inverted Hammer" if is_ih else ("Bearish Engulfing" if is_eng else "Doji")
                        s3_trigger = round(k.l - 0.5, 2)
                        s3_sl = round(max(k.h, float(r_lvl)) + params["default_sl_buffer_pts"], 2)
                        s3_risk = round(s3_sl - s3_trigger, 2)
                        s3_target = round(s3_trigger - 2.0 * s3_risk, 2)
                        s3_status = "ARMED"
                        s3_detail = f"Rejection {s3_pat} at Call Wall / Resistance {r_lvl:,.0f} (tested {k.h:,.1f}) at {k.ts.strftime('%H:%M')}."

                        for later in today_candles[idx + 1:]:
                            if later.l < s3_trigger:
                                s3_status = "TRIGGERED"
                                s3_trig_ts = later.ts
                                break
                        break
            if s3_candle:
                break

    if not s3_candle:
        walls_str = ", ".join(f"{x:,.0f}" for x in key_resistances) if key_resistances else "None available"
        s3_detail = f"No candlestick rejection at Call writing walls ({walls_str}) today."

    setups.append(ReversalSetup(
        id="SHORT_3",
        name="Point 3 — Option Chain Resistance Confluence",
        side="SHORT",
        category="Resistance & Reversal",
        status=s3_status,
        badge_color="#E8503A" if s3_status == "TRIGGERED" else ("#C9A227" if s3_status == "ARMED" else "#8FA6B8"),
        candle_idx=s3_candle.i if s3_candle else None,
        candle_ts=s3_candle.ts if s3_candle else None,
        candle_pattern=s3_pat,
        trigger_price=s3_trigger,
        stop_loss=s3_sl,
        predefined_risk=s3_risk,
        target_price=s3_target,
        rr_ratio=round((s3_trigger - s3_target) / s3_risk, 2) if (s3_trigger and s3_target and s3_risk and s3_risk > 0) else None,
        confluence_detail=s3_detail,
        mentor_rule="Look for candlestick rejection at key Option Chain Resistance (heavy Call OI / Call writing wall).",
        mentor_note="Heavy Call OI acts as an institutional ceiling. Candlestick rejection confirms writers are actively defending.",
        execution_guidance="Enter on breakdown of the 15m rejection candle low. Stop above the wall/candle high.",
        triggered_candle_ts=s3_trig_ts,
        outcome=s3_status,
    ))

    # --- Short Setup 4: Major Round Number Levels (Multiples of 500 / 1000) ---
    s4_status = "INACTIVE"
    s4_candle = None
    s4_trigger = None
    s4_sl = None
    s4_risk = None
    s4_target = None
    s4_pat = ""
    s4_detail = ""
    s4_trig_ts = None

    step = params["round_number_step"]
    for idx, k in enumerate(today_candles):
        near_500 = round(k.h / step) * step
        dist = abs(k.h - near_500)
        if dist <= params["round_proximity_pts"]:
            is_ih = _is_inverted_hammer(k, params)
            is_dj = _is_doji(k, params)
            if is_ih or is_dj:
                s4_candle = k
                s4_pat = "Inverted Hammer" if is_ih else "Doji"
                s4_trigger = round(k.l - 0.5, 2)
                s4_sl = round(max(k.h, float(near_500)) + params["default_sl_buffer_pts"], 2)
                s4_risk = round(s4_sl - s4_trigger, 2)
                s4_target = round(s4_trigger - 50.0, 2)
                s4_status = "ARMED"
                s4_detail = f"Round number {near_500:,.0f} rejected by {s4_pat} (high {k.h:,.1f}) at {k.ts.strftime('%H:%M')}."

                for later in today_candles[idx + 1:]:
                    if later.l < s4_trigger:
                        s4_status = "TRIGGERED"
                        s4_trig_ts = later.ts
                        break
                break

    if not s4_candle:
        s4_detail = "No Inverted Hammer / Doji formed at major 500/1000 round number psychological barriers."

    setups.append(ReversalSetup(
        id="SHORT_4",
        name="Point 4 — Major Round Number Psychological Levels (500 / 1,000)",
        side="SHORT",
        category="Resistance & Reversal",
        status=s4_status,
        badge_color="#E8503A" if s4_status == "TRIGGERED" else ("#C9A227" if s4_status == "ARMED" else "#8FA6B8"),
        candle_idx=s4_candle.i if s4_candle else None,
        candle_ts=s4_candle.ts if s4_candle else None,
        candle_pattern=s4_pat,
        trigger_price=s4_trigger,
        stop_loss=s4_sl,
        predefined_risk=s4_risk,
        target_price=s4_target,
        rr_ratio=round((s4_trigger - s4_target) / s4_risk, 2) if (s4_trigger and s4_target and s4_risk and s4_risk > 0) else None,
        confluence_detail=s4_detail,
        mentor_rule="Watch multiples of 1,000 and 500 (e.g., 24,000 / 23,500).",
        mentor_note="Rule: Even if it's not the exact Day High or top OI strike, an Inverted Hammer forming at a major round number acts as an institutional barrier and triggers a short move. Even if Upper Bollinger Band (UBB) is pointing up, you can trade this reversal.",
        execution_guidance="Do not fear upward-sloping bands here — round numbers trigger institutional profit booking. Enter on low breakdown.",
        triggered_candle_ts=s4_trig_ts,
        outcome=s4_status,
    ))

    # --- Short Setup 5: Double Reversal Candles near Day High ---
    s5_status = "INACTIVE"
    s5_candle = None
    s5_trigger = None
    s5_sl = None
    s5_risk = None
    s5_target = None
    s5_pat = ""
    s5_detail = ""
    s5_trig_ts = None

    if len(today_candles) >= 2 and day_high is not None:
        for i in range(1, len(today_candles)):
            c1 = today_candles[i - 1]
            c2 = today_candles[i]
            near_dh1 = abs(c1.h - day_high) <= params["extremum_proximity_pts"]
            near_dh2 = abs(c2.h - day_high) <= params["extremum_proximity_pts"]
            if near_dh1 and near_dh2:
                c1_rev = _is_doji(c1, params) or _is_inverted_hammer(c1, params)
                c2_rev = _is_doji(c2, params) or _is_inverted_hammer(c2, params)
                if c1_rev and c2_rev:
                    s5_candle = c2
                    p1_name = "Doji" if _is_doji(c1, params) else "Inv Hammer"
                    p2_name = "Doji" if _is_doji(c2, params) else "Inv Hammer"
                    s5_pat = f"Double {p2_name}" if p1_name == p2_name else f"{p1_name} + {p2_name}"
                    s5_trigger = round(c2.l - 0.5, 2)
                    s5_sl = round(max(c1.h, c2.h) + params["default_sl_buffer_pts"], 2)
                    s5_risk = round(s5_sl - s5_trigger, 2)
                    s5_target = round(s5_trigger - 2.0 * s5_risk, 2)
                    s5_status = "ARMED"
                    s5_detail = f"Two consecutive rejection candles ({s5_pat}) near Day High {day_high:,.1f} ({c1.ts.strftime('%H:%M')} & {c2.ts.strftime('%H:%M')})."

                    for later in today_candles[i + 1:]:
                        if later.l < s5_trigger:
                            s5_status = "TRIGGERED"
                            s5_trig_ts = later.ts
                            break
                    break

    if not s5_candle:
        s5_detail = "No consecutive Double Reversal candles printed at Day High in today's session."

    setups.append(ReversalSetup(
        id="SHORT_5",
        name="Point 5 — Double Reversal Candles near Day High",
        side="SHORT",
        category="Resistance & Reversal",
        status=s5_status,
        badge_color="#E8503A" if s5_status == "TRIGGERED" else ("#C9A227" if s5_status == "ARMED" else "#8FA6B8"),
        candle_idx=s5_candle.i if s5_candle else None,
        candle_ts=s5_candle.ts if s5_candle else None,
        candle_pattern=s5_pat,
        trigger_price=s5_trigger,
        stop_loss=s5_sl,
        predefined_risk=s5_risk,
        target_price=s5_target,
        rr_ratio=round((s5_trigger - s5_target) / s5_risk, 2) if (s5_trigger and s5_target and s5_risk and s5_risk > 0) else None,
        confluence_detail=s5_detail,
        mentor_rule="A Double Doji or Double Inverted Hammer forming near Day High.",
        mentor_note="Two consecutive rejection candles confirm sellers defending the ceiling with strong institutional conviction.",
        execution_guidance="Wait for the second candle to close. Trigger on the second candle's low break; stop above the highest high of both.",
        triggered_candle_ts=s5_trig_ts,
        outcome=s5_status,
    ))

    # ─────────────────────────────────────────────────────────────────────────
    # PART 2: DOWNSIDE / LONG SETUPS (SUPPORT & REVERSAL)
    # ─────────────────────────────────────────────────────────────────────────

    # --- Long Setup 1: Gap-Down Days (Hammer at Day Low) ---
    l1_status = "INACTIVE"
    l1_candle = None
    l1_trigger = None
    l1_sl = None
    l1_risk = None
    l1_target = None
    l1_pat = ""
    l1_detail = ""
    l1_trig_ts = None

    if gap_type == "GAP_DOWN":
        for idx, k in enumerate(today_candles):
            is_hm = _is_hammer(k, params)
            if is_hm and (day_low is not None) and (abs(k.l - day_low) <= params["extremum_proximity_pts"]):
                l1_candle = k
                l1_pat = "Hammer"
                l1_trigger = round(k.h + 0.5, 2)
                l1_sl = round(k.l - params["default_sl_buffer_pts"], 2)
                l1_risk = round(l1_trigger - l1_sl, 2)
                l1_target = round(prev_close if prev_close else (l1_trigger + 2.0 * l1_risk), 2)
                l1_status = "ARMED"
                l1_detail = f"Gap-Down {gap_points:.1f} pts. Hammer printed near Day Low {day_low:,.1f} at {k.ts.strftime('%H:%M')}."

                for later in today_candles[idx + 1:]:
                    if later.h > l1_trigger:
                        if later.l < l1_sl:
                            l1_status = "STOPPED"
                        else:
                            l1_status = "TRIGGERED"
                            l1_trig_ts = later.ts
                        break
                break
    else:
        l1_detail = f"No gap-down today (Gap: {gap_points:+.1f} pts vs threshold -{min_gap:.0f} pts)." if gap_points is not None else "Previous close unknown for gap calculation."

    setups.append(ReversalSetup(
        id="LONG_1",
        name="Point 1 — Gap-Down Day: Hammer at Day Low",
        side="LONG",
        category="Support & Reversal",
        status=l1_status,
        badge_color="#2FBF71" if l1_status == "TRIGGERED" else ("#C9A227" if l1_status == "ARMED" else "#8FA6B8"),
        candle_idx=l1_candle.i if l1_candle else None,
        candle_ts=l1_candle.ts if l1_candle else None,
        candle_pattern=l1_pat,
        trigger_price=l1_trigger,
        stop_loss=l1_sl,
        predefined_risk=l1_risk,
        target_price=l1_target,
        rr_ratio=round((l1_target - l1_trigger) / l1_risk, 2) if (l1_trigger and l1_target and l1_risk and l1_risk > 0) else None,
        confluence_detail=l1_detail,
        mentor_rule="On gap-down days, look for a Hammer forming near Day Low.",
        mentor_note="Trigger: Entry above the high of the Hammer candle once it closes. Stop: Low of the candle.",
        execution_guidance="Must wait for 15-minute close. Enter only when high is broken. Predefined risk is the candle range.",
        triggered_candle_ts=l1_trig_ts,
        outcome=l1_status,
    ))

    # --- Long Setup 2: W-Pattern / Double Bottom ---
    l2_status = "INACTIVE"
    l2_candle = None
    l2_trigger = None
    l2_sl = None
    l2_risk = None
    l2_target = None
    l2_pat = ""
    l2_detail = ""
    l2_trig_ts = None

    if len(today_candles) >= 5:
        lows = [c.l for c in today_candles]
        for i in range(1, len(today_candles) - 1):
            if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
                t1 = today_candles[i]
                for j in range(i + 2, len(today_candles)):
                    t2 = today_candles[j]
                    if abs(t1.l - t2.l) / max(t1.l, 0.01) <= 0.0035:
                        neckline_candidates = [c.h for c in today_candles[i + 1:j]]
                        if neckline_candidates:
                            neckline_val = max(neckline_candidates)
                            if (neckline_val - t1.l) >= 20.0:
                                is_hm = _is_hammer(t2, params)
                                is_dj = _is_doji(t2, params)
                                if is_hm or is_dj or t2.bull:
                                    l2_candle = t2
                                    l2_pat = "Hammer at Trough 2" if is_hm else ("Doji at Trough 2" if is_dj else "Reversal at Trough 2")
                                    l2_trigger = round(t2.h + 0.5, 2)
                                    l2_sl = round(min(t1.l, t2.l) - params["default_sl_buffer_pts"], 2)
                                    l2_risk = round(l2_trigger - l2_sl, 2)
                                    neck_proj = neckline_val + (neckline_val - l2_sl)
                                    l2_target = round(neckline_val if neckline_val > l2_trigger else neck_proj, 2)
                                    if l2_target <= l2_trigger:
                                        l2_target = round(l2_trigger + 2.0 * l2_risk, 2)
                                    l2_status = "ARMED"
                                    l2_detail = f"W-Pattern Double Bottom: T1 {t1.l:,.1f} ({t1.ts.strftime('%H:%M')}) & T2 {t2.l:,.1f} ({t2.ts.strftime('%H:%M')}). Neckline at {neckline_val:,.1f}."

                                    for later in today_candles[j + 1:]:
                                        if later.h > l2_trigger:
                                            l2_status = "TRIGGERED"
                                            l2_trig_ts = later.ts
                                            break
                                    break
                if l2_candle:
                    break

    if not l2_candle:
        l2_detail = "No W-Pattern Double Bottom with 2nd trough Hammer/Doji confirmed in today's 15m session."

    setups.append(ReversalSetup(
        id="LONG_2",
        name="Point 2 — W-Pattern / Double Bottom",
        side="LONG",
        category="Support & Reversal",
        status=l2_status,
        badge_color="#2FBF71" if l2_status == "TRIGGERED" else ("#C9A227" if l2_status == "ARMED" else "#8FA6B8"),
        candle_idx=l2_candle.i if l2_candle else None,
        candle_ts=l2_candle.ts if l2_candle else None,
        candle_pattern=l2_pat,
        trigger_price=l2_trigger,
        stop_loss=l2_sl,
        predefined_risk=l2_risk,
        target_price=l2_target,
        rr_ratio=round((l2_target - l2_trigger) / l2_risk, 2) if (l2_trigger and l2_target and l2_risk and l2_risk > 0) else None,
        confluence_detail=l2_detail,
        mentor_rule="Double Bottom or W-pattern formation at the lows.",
        mentor_note="Rule: Second swing low forms a reversal candle (Hammer/Doji) signaling strong accumulation.",
        execution_guidance="Early high-R:R entry above 2nd swing low reversal candle high rather than waiting for neckline breakout.",
        triggered_candle_ts=l2_trig_ts,
        outcome=l2_status,
    ))

    # --- Long Setup 3: Option Chain Support Confluence ---
    l3_status = "INACTIVE"
    l3_candle = None
    l3_trigger = None
    l3_sl = None
    l3_risk = None
    l3_target = None
    l3_pat = ""
    l3_detail = ""
    l3_trig_ts = None

    if key_supports:
        for idx, k in enumerate(today_candles):
            is_hm = _is_hammer(k, params)
            is_dj = _is_doji(k, params)
            is_eng = (idx > 0 and _is_bullish_engulfing(k, today_candles[idx - 1], params)) if idx > 0 else False
            if is_hm or is_dj or is_eng:
                for s_lvl in key_supports:
                    if abs(k.l - s_lvl) <= params["chain_wall_proximity_pts"]:
                        l3_candle = k
                        l3_pat = "Hammer" if is_hm else ("Bullish Engulfing" if is_eng else "Doji")
                        l3_trigger = round(k.h + 0.5, 2)
                        l3_sl = round(min(k.l, float(s_lvl)) - params["default_sl_buffer_pts"], 2)
                        l3_risk = round(l3_trigger - l3_sl, 2)
                        l3_target = round(l3_trigger + 2.0 * l3_risk, 2)
                        l3_status = "ARMED"
                        l3_detail = f"Confirmation {l3_pat} at Put Wall / Support {s_lvl:,.0f} (tested {k.l:,.1f}) at {k.ts.strftime('%H:%M')}."

                        for later in today_candles[idx + 1:]:
                            if later.h > l3_trigger:
                                l3_status = "TRIGGERED"
                                l3_trig_ts = later.ts
                                break
                        break
            if l3_candle:
                break

    if not l3_candle:
        supp_str = ", ".join(f"{x:,.0f}" for x in key_supports) if key_supports else "None available"
        l3_detail = f"No candlestick confirmation at Put writing walls ({supp_str}) today."

    setups.append(ReversalSetup(
        id="LONG_3",
        name="Point 3 — Option Chain Support Confluence",
        side="LONG",
        category="Support & Reversal",
        status=l3_status,
        badge_color="#2FBF71" if l3_status == "TRIGGERED" else ("#C9A227" if l3_status == "ARMED" else "#8FA6B8"),
        candle_idx=l3_candle.i if l3_candle else None,
        candle_ts=l3_candle.ts if l3_candle else None,
        candle_pattern=l3_pat,
        trigger_price=l3_trigger,
        stop_loss=l3_sl,
        predefined_risk=l3_risk,
        target_price=l3_target,
        rr_ratio=round((l3_target - l3_trigger) / l3_risk, 2) if (l3_trigger and l3_target and l3_risk and l3_risk > 0) else None,
        confluence_detail=l3_detail,
        mentor_rule="Candlestick confirmation right at key Option Chain Support (heavy Put OI / Put writing wall).",
        mentor_note="Heavy Put writing floor. Bullish candlestick confirms institutional support is defending.",
        execution_guidance="Enter on high breakout of the 15m confirmation candle. Stop below the support floor.",
        triggered_candle_ts=l3_trig_ts,
        outcome=l3_status,
    ))

    # --- Long Setup 4: Round Numbers (Multiples of 500 / 1000 Support) ---
    l4_status = "INACTIVE"
    l4_candle = None
    l4_trigger = None
    l4_sl = None
    l4_risk = None
    l4_target = None
    l4_pat = ""
    l4_detail = ""
    l4_trig_ts = None

    for idx, k in enumerate(today_candles):
        near_500 = round(k.l / step) * step
        dist = abs(k.l - near_500)
        if dist <= params["round_proximity_pts"]:
            is_hm = _is_hammer(k, params)
            is_eng = (idx > 0 and _is_bullish_engulfing(k, today_candles[idx - 1], params)) if idx > 0 else False
            if is_hm or is_eng:
                l4_candle = k
                l4_pat = "Hammer" if is_hm else "Bullish Engulfing"
                l4_trigger = round(k.h + 0.5, 2)
                l4_sl = round(min(k.l, float(near_500)) - params["default_sl_buffer_pts"], 2)
                l4_risk = round(l4_trigger - l4_sl, 2)
                l4_target = round(l4_trigger + 50.0, 2)
                l4_status = "ARMED"
                l4_detail = f"Round number {near_500:,.0f} defended by {l4_pat} (low {k.l:,.1f}) at {k.ts.strftime('%H:%M')}."

                for later in today_candles[idx + 1:]:
                    if later.h > l4_trigger:
                        l4_status = "TRIGGERED"
                        l4_trig_ts = later.ts
                        break
                break

    if not l4_candle:
        l4_detail = "No Hammer / Bullish Engulfing closed at a major 500/1000 round number support."

    setups.append(ReversalSetup(
        id="LONG_4",
        name="Point 4 — Round Numbers (Multiples of 500 / 1,000 Support)",
        side="LONG",
        category="Support & Reversal",
        status=l4_status,
        badge_color="#2FBF71" if l4_status == "TRIGGERED" else ("#C9A227" if l4_status == "ARMED" else "#8FA6B8"),
        candle_idx=l4_candle.i if l4_candle else None,
        candle_ts=l4_candle.ts if l4_candle else None,
        candle_pattern=l4_pat,
        trigger_price=l4_trigger,
        stop_loss=l4_sl,
        predefined_risk=l4_risk,
        target_price=l4_target,
        rr_ratio=round((l4_target - l4_trigger) / l4_risk, 2) if (l4_trigger and l4_target and l4_risk and l4_risk > 0) else None,
        confluence_detail=l4_detail,
        mentor_rule="Reversal candle (Hammer / Bullish Engulfing) appearing right on a 500 or 1,000 round number strike.",
        mentor_note="Rule: Must wait for the 15-minute candle close to confirm the level held. Institutional barrier triggers bounce.",
        execution_guidance="Strict 15m close discipline. Enter when next candle trades above high; stop below round number / candle low.",
        triggered_candle_ts=l4_trig_ts,
        outcome=l4_status,
    ))

    # --- Long Setup 5: Double Reversal Candles near Day Low ---
    l5_status = "INACTIVE"
    l5_candle = None
    l5_trigger = None
    l5_sl = None
    l5_risk = None
    l5_target = None
    l5_pat = ""
    l5_detail = ""
    l5_trig_ts = None

    if len(today_candles) >= 2 and day_low is not None:
        for i in range(1, len(today_candles)):
            c1 = today_candles[i - 1]
            c2 = today_candles[i]
            near_dl1 = abs(c1.l - day_low) <= params["extremum_proximity_pts"]
            near_dl2 = abs(c2.l - day_low) <= params["extremum_proximity_pts"]
            if near_dl1 and near_dl2:
                c1_rev = _is_hammer(c1, params) or _is_doji(c1, params)
                c2_rev = _is_hammer(c2, params) or _is_doji(c2, params)
                if c1_rev and c2_rev:
                    l5_candle = c2
                    p1_name = "Hammer" if _is_hammer(c1, params) else "Doji"
                    p2_name = "Hammer" if _is_hammer(c2, params) else "Doji"
                    l5_pat = f"Double {p2_name}" if p1_name == p2_name else f"{p1_name} + {p2_name}"
                    l5_trigger = round(c2.h + 0.5, 2)
                    l5_sl = round(min(c1.l, c2.l) - params["default_sl_buffer_pts"], 2)
                    l5_risk = round(l5_trigger - l5_sl, 2)
                    l5_target = round(l5_trigger + 2.0 * l5_risk, 2)
                    l5_status = "ARMED"
                    l5_detail = f"Two consecutive reversal candles ({l5_pat}) near Day Low {day_low:,.1f} ({c1.ts.strftime('%H:%M')} & {c2.ts.strftime('%H:%M')})."

                    for later in today_candles[i + 1:]:
                        if later.h > l5_trigger:
                            l5_status = "TRIGGERED"
                            l5_trig_ts = later.ts
                            break
                    break

    if not l5_candle:
        l5_detail = "No consecutive Double Reversal candles printed at Day Low in today's session."

    setups.append(ReversalSetup(
        id="LONG_5",
        name="Point 5 — Double Reversal Candles near Day Low",
        side="LONG",
        category="Support & Reversal",
        status=l5_status,
        badge_color="#2FBF71" if l5_status == "TRIGGERED" else ("#C9A227" if l5_status == "ARMED" else "#8FA6B8"),
        candle_idx=l5_candle.i if l5_candle else None,
        candle_ts=l5_candle.ts if l5_candle else None,
        candle_pattern=l5_pat,
        trigger_price=l5_trigger,
        stop_loss=l5_sl,
        predefined_risk=l5_risk,
        target_price=l5_target,
        rr_ratio=round((l5_target - l5_trigger) / l5_risk, 2) if (l5_trigger and l5_target and l5_risk and l5_risk > 0) else None,
        confluence_detail=l5_detail,
        mentor_rule="A Double Hammer or Double Doji right at Day Low.",
        mentor_note="Confirms a hard floor has formed, offering low-risk entries before moving up.",
        execution_guidance="Wait for the second candle to close. Trigger on the second candle's high break; stop below the lowest low of both.",
        triggered_candle_ts=l5_trig_ts,
        outcome=l5_status,
    ))

    # Counts
    active_count = sum(1 for s in setups if s.status in ("TRIGGERED", "ARMED"))
    short_trig = sum(1 for s in setups if s.side == "SHORT" and s.status == "TRIGGERED")
    long_trig = sum(1 for s in setups if s.side == "LONG" and s.status == "TRIGGERED")

    return SessionMatrixResult(
        timeframe="15m",
        eval_time=eval_time,
        today_date=today_dt,
        prev_close=prev_close,
        today_open=today_open,
        day_high=day_high,
        day_low=day_low,
        gap_points=gap_points,
        gap_type=gap_type,
        setups=setups,
        active_count=active_count,
        short_triggered_count=short_trig,
        long_triggered_count=long_trig,
    )
