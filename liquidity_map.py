"""
liquidity_map.py — Stop-Loss Liquidity Pools & Level Strength Engine.

WHAT THIS MODULE CANNOT DO:
───────────────────────────
- Stop-loss pools are INFERRED from trading convention, NOT OBSERVED order book data.
  NSE publishes no resting-order depth at these price levels. The model identifies where
  stops are conventionally clustered; if traders place stops elsewhere, the model is wrong.
- A swept pool is an OBSERVATION, NOT A DIRECTIONAL SIGNAL. Price trading through a pool
  and returning is common and does not guarantee a reversal or continuation.
- It CANNOT eliminate the fractal pivot confirmation lag (N bars into the future).
- It CANNOT predict future price action or replace risk management.
- It DOES NOT feed the decision cockpit, alerts, or Strategy 1. It is an isolated map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
import pandas as pd

import smc

LevelStrengthClass = Literal["WEAK", "LEVEL", "ZONE"]
LevelType = Literal["SUPPORT", "RESISTANCE"]
LevelTimeframe = Literal["INTRADAY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"]
PoolSide = Literal["BUY_STOPS", "SELL_STOPS"]
ReadingType = Literal["REJECTION", "ACCEPTANCE", "INDECISIVE", "CLEAR_OF_LEVELS"]


# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TrackedLevel:
    name: str
    price: float
    level_type: LevelType
    timeframe: LevelTimeframe
    touch_count: int
    touch_times: list[datetime]
    strength_class: LevelStrengthClass
    originating_move_atr: float
    psychological_context: str
    is_in_play: bool
    distance_pts: float

    @property
    def touch_history_str(self) -> str:
        if not self.touch_times:
            return "None"
        return ", ".join(t.strftime("%H:%M") if hasattr(t, "strftime") else str(t) for t in self.touch_times[-4:])


@dataclass
class StopLossPool:
    pool_id: str
    level_ref_name: str
    level_price: float
    pool_price: float
    pool_side: PoolSide
    offset_pts: float
    weight_score: float
    is_swept_today: bool
    swept_time: Optional[datetime]
    distance_from_spot: float
    is_approaching: bool
    mechanical_explanation: str


@dataclass
class HTFLevel:
    timeframe: str
    period_label: str
    level_name: str
    price: float
    distance_pts: float
    in_striking_distance: bool
    significance_note: str


@dataclass
class CandleAtLevelReading:
    reading: ReadingType
    nearest_level_name: str
    level_price: float
    level_type: LevelType
    wick_to_body_ratio: float
    body_to_range_ratio: float
    dominant_side: str
    psychological_reading: str
    is_active: bool


@dataclass
class LiquiditySnapshot:
    timeframe: str
    spot_price: float
    atr_points: float
    nearest_buy_stops: Optional[StopLossPool]
    nearest_sell_stops: Optional[StopLossPool]
    strongest_level: Optional[TrackedLevel]
    candle_reading: CandleAtLevelReading
    levels: list[TrackedLevel]
    pools: list[StopLossPool]
    htf_levels: list[HTFLevel]
    structure_state: str
    sideways_message: Optional[str]
    rules: dict
    candle_count: int
    data_guard_warning: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Rule Loader
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_RULES_PATH = Path(__file__).parent / "liquidity_rules.json"


def load_rules(rules_path: Optional[Path] = None) -> dict:
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "pivot_window_n": 2,
        "touch_tolerance_pts": 8.0,
        "zone_half_width_pts": 6.0,
        "pool_offset_atr_mult": 0.5,
        "min_pool_offset_pts": 10.0,
        "pool_approach_buffer_pts": 15.0,
        "striking_distance_pts": 150.0,
        "rejection_wick_ratio": 1.5,
        "acceptance_body_ratio": 0.55,
        "level_proximity_band_pts": 12.0,
        "weekly_history_weeks": 8,
        "monthly_history_months": 6,
        "lot_size": 65,
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
# 1 · Higher-Timeframe Level Extraction from Daily OHLC
# ═══════════════════════════════════════════════════════════════════════════

def extract_htf_levels(ohlc: Optional[pd.DataFrame], spot: float, rules: dict) -> list[HTFLevel]:
    """
    Extracts Weekly (8 weeks), Monthly (6 months), and Previous Year levels from daily OHLC.
    Tags each level with its timeframe and notes if within striking distance today.
    """
    htf_out: list[HTFLevel] = []
    if ohlc is None or ohlc.empty or len(ohlc) < 15:
        return htf_out

    df = ohlc.copy()
    # Normalize column names
    col_map = {c: c.capitalize() for c in df.columns}
    df.rename(columns=col_map, inplace=True)
    if "Date" not in df.columns or "High" not in df.columns or "Low" not in df.columns or "Close" not in df.columns:
        return htf_out

    try:
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
    except Exception:
        return htf_out

    striking_dist = float(rules.get("striking_distance_pts", 150.0))
    n_weeks = int(rules.get("weekly_history_weeks", 8))
    n_months = int(rules.get("monthly_history_months", 6))

    # A. Weekly Resampling
    try:
        df_w = df.set_index("Date").resample("W-FRI").agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last"
        }).dropna()
        # Drop the currently forming week to get completed weeks
        completed_weeks = df_w.iloc[:-1].tail(n_weeks)
        for i, (dt, row) in enumerate(completed_weeks.iterrows()):
            weeks_ago = len(completed_weeks) - i
            label = f"W-{weeks_ago} ({dt.strftime('%d-%b')})"
            for l_type, val in [("HIGH", row["High"]), ("LOW", row["Low"]), ("CLOSE", row["Close"])]:
                d_pts = abs(spot - float(val))
                htf_out.append(HTFLevel(
                    timeframe="WEEKLY",
                    period_label=label,
                    level_name=f"Weekly {l_type}",
                    price=round(float(val), 1),
                    distance_pts=round(d_pts, 1),
                    in_striking_distance=d_pts <= striking_dist,
                    significance_note="Weekly levels break far less often than intraday levels; institutional anchor."
                ))
    except Exception:
        pass

    # B. Monthly Resampling
    try:
        try:
            df_m = df.set_index("Date").resample("ME").agg({
                "Open": "first", "High": "max", "Low": "min", "Close": "last"
            }).dropna()
        except Exception:
            df_m = df.set_index("Date").resample("M").agg({
                "Open": "first", "High": "max", "Low": "min", "Close": "last"
            }).dropna()
        completed_months = df_m.iloc[:-1].tail(n_months)
        for i, (dt, row) in enumerate(completed_months.iterrows()):
            label = dt.strftime("%b-%Y")
            for l_type, val in [("HIGH", row["High"]), ("LOW", row["Low"]), ("CLOSE", row["Close"])]:
                d_pts = abs(spot - float(val))
                htf_out.append(HTFLevel(
                    timeframe="MONTHLY",
                    period_label=label,
                    level_name=f"Monthly {l_type}",
                    price=round(float(val), 1),
                    distance_pts=round(d_pts, 1),
                    in_striking_distance=d_pts <= striking_dist,
                    significance_note="Monthly macro boundaries require major institutional volume to breach."
                ))
    except Exception:
        pass

    # C. Prior Year High & Low
    try:
        df["Year"] = df["Date"].dt.year
        current_year = df["Year"].iloc[-1]
        prior_year_df = df[df["Year"] == (current_year - 1)]
        if not prior_year_df.empty:
            py_high = float(prior_year_df["High"].max())
            py_low = float(prior_year_df["Low"].min())
            for l_type, val in [("HIGH", py_high), ("LOW", py_low)]:
                d_pts = abs(spot - val)
                htf_out.append(HTFLevel(
                    timeframe="YEARLY",
                    period_label=str(current_year - 1),
                    level_name=f"Prev Year {l_type}",
                    price=round(val, 1),
                    distance_pts=round(d_pts, 1),
                    in_striking_distance=d_pts <= (striking_dist * 2.0),
                    significance_note="Annual pivot extreme. Massive structural benchmark."
                ))
    except Exception:
        pass

    return htf_out


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Level Strength by Touch Count & Extreme Swing Measurement
# ═══════════════════════════════════════════════════════════════════════════

def build_tracked_levels(
    df: pd.DataFrame,
    pivots_list: list[smc.Pivot],
    spot: float,
    atr_val: float,
    read_levels: Any = None,
    htf_levels: Optional[list[HTFLevel]] = None,
    rules: Optional[dict] = None
) -> list[TrackedLevel]:
    """
    Counts touches against confirmed pivots and candle wicks with configurable tolerance.
    Classifies WEAK (1), LEVEL (2), ZONE (3+).
    Calculates originating move size in ATR units.
    """
    rules = rules or {}
    touch_tol = float(rules.get("touch_tolerance_pts", 8.0))
    prox_band = float(rules.get("level_proximity_band_pts", 12.0))

    tracked: list[TrackedLevel] = []
    if df is None or df.empty:
        return tracked

    curr_idx = len(df) - 1
    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index).tolist()

    knowable_pivots = [p for p in pivots_list if p.known_at(curr_idx)]

    # Seed initial candidate levels
    candidates: list[dict[str, Any]] = []

    # 1. Swings from smc.pivots()
    for p in knowable_pivots:
        lvl_type: LevelType = "RESISTANCE" if p.kind == "HIGH" else "SUPPORT"
        candidates.append({
            "name": f"Pivot {p.kind} ({p.subkind or 'SWING'})",
            "price": p.price,
            "level_type": lvl_type,
            "timeframe": "INTRADAY",
            "anchor_bar": p.i,
        })

    # 2. Daily / System Levels from read_levels if available
    if read_levels is not None:
        for attr, name, ltype in [
            ("r1", "R1 Pivot", "RESISTANCE"),
            ("r2", "R2 Pivot", "RESISTANCE"),
            ("s1", "S1 Pivot", "SUPPORT"),
            ("s2", "S2 Pivot", "SUPPORT"),
        ]:
            val = getattr(read_levels, attr, None)
            if val is not None and isinstance(val, (int, float)) and val > 0:
                candidates.append({
                    "name": name,
                    "price": float(val),
                    "level_type": ltype,
                    "timeframe": "DAILY",
                    "anchor_bar": None,
                })

    # 3. HTF Levels in striking distance
    if htf_levels:
        for htf in htf_levels:
            if htf.in_striking_distance:
                ltype: LevelType = "RESISTANCE" if "HIGH" in htf.level_name else "SUPPORT"
                tf_label: LevelTimeframe = "WEEKLY" if htf.timeframe == "WEEKLY" else ("MONTHLY" if htf.timeframe == "MONTHLY" else "YEARLY")
                candidates.append({
                    "name": f"{htf.period_label} {htf.level_name}",
                    "price": htf.price,
                    "level_type": ltype,
                    "timeframe": tf_label,
                    "anchor_bar": None,
                })

    # Cluster nearby candidate levels within touch_tol to avoid redundant overlapping entries
    merged_levels: list[dict[str, Any]] = []
    candidates.sort(key=lambda x: x["price"])
    for cand in candidates:
        if not merged_levels:
            merged_levels.append(cand)
        else:
            prev = merged_levels[-1]
            if abs(cand["price"] - prev["price"]) <= (touch_tol * 0.75):
                # Merge into higher-timeframe or more descriptive name
                if cand["timeframe"] in ("WEEKLY", "MONTHLY", "YEARLY"):
                    prev["name"] = cand["name"]
                    prev["timeframe"] = cand["timeframe"]
                prev["price"] = (prev["price"] + cand["price"]) / 2.0
            else:
                merged_levels.append(cand)

    # For each level, calculate touches across the intraday session
    for lvl in merged_levels:
        p_val = lvl["price"]
        touches = 0
        touch_times: list[datetime] = []

        # Walk bars to find touches
        for i in range(len(df)):
            hit = False
            if lvl["level_type"] == "RESISTANCE":
                if abs(h[i] - p_val) <= touch_tol or abs(c[i] - p_val) <= touch_tol:
                    hit = True
            else:
                if abs(l[i] - p_val) <= touch_tol or abs(c[i] - p_val) <= touch_tol:
                    hit = True

            if hit:
                touches += 1
                touch_times.append(ts[i])

        touches = max(touches, 1)

        # Classify Level Strength
        if touches == 1:
            strength: LevelStrengthClass = "WEAK"
            psy_txt = "Single touch. Untested support/resistance; high probability of slippage."
        elif touches == 2:
            strength = "LEVEL"
            psy_txt = "Standard level tested twice. Market participants have recognized this boundary."
        else:
            strength = "ZONE"
            psy_txt = (
                f"Defended {touches} times. Established supply/demand zone. "
                f"Source: the 4th visit is where buyers/sellers are most reliably waiting."
            )

        # Originating move size in ATR units
        move_atr = 0.0
        anchor_i = lvl.get("anchor_bar")
        if anchor_i is not None and anchor_i < curr_idx:
            # Measure subsequent swing expansion
            subsequent_high = float(h[anchor_i:].max())
            subsequent_low = float(l[anchor_i:].min())
            if lvl["level_type"] == "RESISTANCE":
                move_pts = abs(p_val - subsequent_low)
            else:
                move_pts = abs(subsequent_high - p_val)
            move_atr = move_pts / max(atr_val, 1.0)
        else:
            # For HTF or daily levels, proxy using distance moved today
            today_range = float(h.max() - l.min())
            move_atr = today_range / max(atr_val, 1.0)

        dist_pts = abs(spot - p_val)
        in_play = dist_pts <= prox_band

        tracked.append(TrackedLevel(
            name=lvl["name"],
            price=round(p_val, 1),
            level_type=lvl["level_type"],
            timeframe=lvl["timeframe"],
            touch_count=touches,
            touch_times=touch_times,
            strength_class=strength,
            originating_move_atr=round(move_atr, 1),
            psychological_context=psy_txt,
            is_in_play=in_play,
            distance_pts=round(dist_pts, 1),
        ))

    # Sort by distance from spot
    tracked.sort(key=lambda x: x.distance_pts)
    return tracked


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Stop-Loss Liquidity Pools Model
# ═══════════════════════════════════════════════════════════════════════════

def build_stop_loss_pools(
    tracked_levels: list[TrackedLevel],
    df: pd.DataFrame,
    spot: float,
    atr_val: float,
    rules: dict
) -> tuple[list[StopLossPool], Optional[StopLossPool], Optional[StopLossPool]]:
    """
    Projects inferred stop-loss liquidity pools:
      - BUY_STOPS above resistances (shorts' stop-loss cluster)
      - SELL_STOPS below supports (longs' stop-loss cluster)
    Tracks whether pools have been swept today.
    """
    pools: list[StopLossPool] = []
    if not tracked_levels or df is None or df.empty:
        return pools, None, None

    offset_mult = float(rules.get("pool_offset_atr_mult", 0.5))
    min_offset = float(rules.get("min_pool_offset_pts", 10.0))
    approach_buf = float(rules.get("pool_approach_buffer_pts", 15.0))

    offset = max(offset_mult * atr_val, min_offset)

    high_today = float(df["high"].max())
    low_today = float(df["low"].min())
    curr_close = float(df["close"].iloc[-1])
    ts = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index).tolist()
    h_arr = df["high"].astype(float).to_numpy()
    l_arr = df["low"].astype(float).to_numpy()

    for lvl in tracked_levels:
        if lvl.level_type == "RESISTANCE":
            side: PoolSide = "BUY_STOPS"
            pool_price = lvl.price + offset
            dist = pool_price - spot
            mech_note = (
                f"Traders who shorted at {lvl.name} ({lvl.price:,.1f}) have resting buy-stops "
                f"placed above. A breakout triggers these stops, adding buying demand to the rally."
            )
            # Check if swept today: High exceeded pool_price, but current close is back below
            is_swept = (high_today >= pool_price) and (curr_close < pool_price)
            swept_time = None
            if is_swept:
                for b_i in range(len(df)):
                    if h_arr[b_i] >= pool_price:
                        swept_time = ts[b_i]
                        break

        else:  # SUPPORT
            side = "SELL_STOPS"
            pool_price = lvl.price - offset
            dist = spot - pool_price
            mech_note = (
                f"Traders who bought at {lvl.name} ({lvl.price:,.1f}) have resting sell-stops "
                f"placed below. A breakdown triggers these stops, adding selling supply to the decline."
            )
            is_swept = (low_today <= pool_price) and (curr_close > pool_price)
            swept_time = None
            if is_swept:
                for b_i in range(len(df)):
                    if l_arr[b_i] <= pool_price:
                        swept_time = ts[b_i]
                        break

        weight = (lvl.touch_count * 2.0) + (lvl.originating_move_atr * 1.5)
        if lvl.timeframe in ("WEEKLY", "MONTHLY", "YEARLY"):
            weight += 4.0

        is_appr = (0.0 <= dist <= approach_buf)

        pools.append(StopLossPool(
            pool_id=f"POOL_{side}_{int(pool_price)}",
            level_ref_name=lvl.name,
            level_price=lvl.price,
            pool_price=round(pool_price, 1),
            pool_side=side,
            offset_pts=round(offset, 1),
            weight_score=round(weight, 1),
            is_swept_today=is_swept,
            swept_time=swept_time,
            distance_from_spot=round(dist, 1),
            is_approaching=is_appr,
            mechanical_explanation=mech_note,
        ))

    # Identify nearest buy-stops above and sell-stops below
    buy_pools_above = [p for p in pools if p.pool_side == "BUY_STOPS" and p.pool_price > spot]
    sell_pools_below = [p for p in pools if p.pool_side == "SELL_STOPS" and p.pool_price < spot]

    nearest_buy = min(buy_pools_above, key=lambda p: p.pool_price) if buy_pools_above else None
    nearest_sell = max(sell_pools_below, key=lambda p: p.pool_price) if sell_pools_below else None

    # Sort pools by distance from spot
    pools.sort(key=lambda p: abs(p.pool_price - spot))
    return pools, nearest_buy, nearest_sell


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Candle Behaviour at a Level
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_candle_at_level(
    df: pd.DataFrame,
    tracked_levels: list[TrackedLevel],
    rules: dict
) -> CandleAtLevelReading:
    """
    Classifies the last closed candle relative to a nearby level:
      - REJECTION (long opposing wick: buyers or sellers failed)
      - ACCEPTANCE (strong full body: buyers or sellers took over)
      - INDECISIVE (balanced wick/body in chop)
      - CLEAR_OF_LEVELS (price in open space)
    """
    if df is None or df.empty or not tracked_levels:
        return CandleAtLevelReading(
            reading="CLEAR_OF_LEVELS",
            nearest_level_name="None",
            level_price=0.0,
            level_type="SUPPORT",
            wick_to_body_ratio=0.0,
            body_to_range_ratio=0.0,
            dominant_side="None",
            psychological_reading="No active candle data.",
            is_active=False,
        )

    prox_band = float(rules.get("level_proximity_band_pts", 12.0))
    rej_wick_ratio = float(rules.get("rejection_wick_ratio", 1.5))
    acc_body_ratio = float(rules.get("acceptance_body_ratio", 0.55))

    bar = df.iloc[-1]
    b_open = float(bar["open"])
    b_high = float(bar["high"])
    b_low = float(bar["low"])
    b_close = float(bar["close"])

    total_range = max(b_high - b_low, 1.0)
    body = abs(b_close - b_open)
    upper_wick = b_high - max(b_open, b_close)
    lower_wick = min(b_open, b_close) - b_low
    is_green = b_close >= b_open

    # Find nearest level in play
    nearby = [lvl for lvl in tracked_levels if lvl.distance_pts <= prox_band]
    if not nearby:
        # Clear of levels
        above = [lvl for lvl in tracked_levels if lvl.price > b_close]
        below = [lvl for lvl in tracked_levels if lvl.price < b_close]
        d_above = (min(above, key=lambda x: x.price).price - b_close) if above else 0.0
        d_below = (b_close - max(below, key=lambda x: x.price).price) if below else 0.0
        return CandleAtLevelReading(
            reading="CLEAR_OF_LEVELS",
            nearest_level_name="Open Space",
            level_price=0.0,
            level_type="SUPPORT",
            wick_to_body_ratio=round(max(upper_wick, lower_wick) / max(body, 1.0), 2),
            body_to_range_ratio=round(body / total_range, 2),
            dominant_side="Balanced / Open Air",
            psychological_reading=f"Price trading in open space ({d_above:.1f} pts to resistance, {d_below:.1f} pts to support).",
            is_active=False,
        )

    target_lvl = min(nearby, key=lambda x: x.distance_pts)

    if target_lvl.level_type == "RESISTANCE":
        w_b_ratio = upper_wick / max(body, 1.0)
        b_r_ratio = body / total_range

        if w_b_ratio >= rej_wick_ratio or upper_wick >= (0.45 * total_range):
            return CandleAtLevelReading(
                reading="REJECTION",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="RESISTANCE",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Sellers still dominant",
                psychological_reading=f"Buyers tried and failed. Sellers still dominant. Rejection at {target_lvl.name}.",
                is_active=True,
            )
        elif is_green and b_r_ratio >= acc_body_ratio:
            return CandleAtLevelReading(
                reading="ACCEPTANCE",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="RESISTANCE",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Buyers have taken over",
                psychological_reading=f"Buyers have taken over. Breakout more likely than rejection at {target_lvl.name}.",
                is_active=True,
            )
        else:
            return CandleAtLevelReading(
                reading="INDECISIVE",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="RESISTANCE",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Buyers and sellers balanced",
                psychological_reading=f"Indecisive struggle at {target_lvl.name}. Awaiting decisive acceptance or rejection.",
                is_active=True,
            )

    else:  # SUPPORT
        w_b_ratio = lower_wick / max(body, 1.0)
        b_r_ratio = body / total_range

        if w_b_ratio >= rej_wick_ratio or lower_wick >= (0.45 * total_range):
            return CandleAtLevelReading(
                reading="REJECTION",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="SUPPORT",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Buyers dominant",
                psychological_reading=f"Sellers tried and failed. Buyers dominant. Rejection at {target_lvl.name}.",
                is_active=True,
            )
        elif (not is_green) and b_r_ratio >= acc_body_ratio:
            return CandleAtLevelReading(
                reading="ACCEPTANCE",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="SUPPORT",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Sellers have taken over",
                psychological_reading=f"Sellers have taken over. Breakdown more likely at {target_lvl.name}.",
                is_active=True,
            )
        else:
            return CandleAtLevelReading(
                reading="INDECISIVE",
                nearest_level_name=target_lvl.name,
                level_price=target_lvl.price,
                level_type="SUPPORT",
                wick_to_body_ratio=round(w_b_ratio, 2),
                body_to_range_ratio=round(b_r_ratio, 2),
                dominant_side="Buyers and sellers balanced",
                psychological_reading=f"Indecisive struggle at {target_lvl.name}. Awaiting decisive acceptance or rejection.",
                is_active=True,
            )


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Master Evaluator: evaluate_liquidity_map()
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_liquidity_map(
    df: pd.DataFrame,
    spot: float,
    timeframe: str = "15m",
    read_levels: Any = None,
    ohlc: Optional[pd.DataFrame] = None,
    rules_path: Optional[Path] = None,
    custom_rules: Optional[dict] = None
) -> LiquiditySnapshot:
    """
    Master pipeline evaluating Stop-Loss Liquidity Pools, Level Strength by Touch Count,
    Higher-Timeframe Level Overlays, and Candle-at-Level psychological readings.
    """
    rules = load_rules(rules_path)
    if custom_rules:
        rules.update(custom_rules)

    n = int(rules.get("pivot_window_n", 2))
    min_bars = 2 * n + 3

    # Guard 1: Empty or Pre-Open
    if df is None or df.empty:
        return LiquiditySnapshot(
            timeframe=timeframe,
            spot_price=spot,
            atr_points=20.0,
            nearest_buy_stops=None,
            nearest_sell_stops=None,
            strongest_level=None,
            candle_reading=CandleAtLevelReading("CLEAR_OF_LEVELS", "None", 0.0, "SUPPORT", 0.0, 0.0, "None", "Pre-open session.", False),
            levels=[],
            pools=[],
            htf_levels=[],
            structure_state="SIDEWAYS",
            sideways_message="Pre-open session: No candle data available.",
            rules=rules,
            candle_count=0,
            data_guard_warning="Pre-open: Awaiting intraday candle feed.",
        )

    # Guard 2: Missing Essential Columns
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        return LiquiditySnapshot(
            timeframe=timeframe,
            spot_price=spot,
            atr_points=20.0,
            nearest_buy_stops=None,
            nearest_sell_stops=None,
            strongest_level=None,
            candle_reading=CandleAtLevelReading("CLEAR_OF_LEVELS", "None", 0.0, "SUPPORT", 0.0, 0.0, "None", "Schema error.", False),
            levels=[],
            pools=[],
            htf_levels=[],
            structure_state="SIDEWAYS",
            sideways_message=f"Missing columns: {missing}",
            rules=rules,
            candle_count=len(df),
            data_guard_warning=f"Data schema error: Missing columns {missing}.",
        )

    # Guard 3: Fewer than 2N+3 bars
    if len(df) < min_bars:
        return LiquiditySnapshot(
            timeframe=timeframe,
            spot_price=spot,
            atr_points=20.0,
            nearest_buy_stops=None,
            nearest_sell_stops=None,
            strongest_level=None,
            candle_reading=CandleAtLevelReading("CLEAR_OF_LEVELS", "None", 0.0, "SUPPORT", 0.0, 0.0, "None", "Insufficient bars.", False),
            levels=[],
            pools=[],
            htf_levels=[],
            structure_state="SIDEWAYS",
            sideways_message=f"Only {len(df)} bars. Requires at least {min_bars}.",
            rules=rules,
            candle_count=len(df),
            data_guard_warning=f"Insufficient bar history: Received {len(df)} bars (requires {min_bars}).",
        )

    # Resolve ATR
    atr_val = 20.0
    if "atr" in df.columns and not df["atr"].dropna().empty:
        atr_val = float(df["atr"].dropna().iloc[-1])
    elif len(df) >= 15:
        h = df["high"].astype(float).to_numpy()
        l = df["low"].astype(float).to_numpy()
        c = df["close"].astype(float).to_numpy()
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr_val = float(pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1])
    atr_val = max(atr_val, 5.0)

    # 1. Structural Pivots
    pivots_list = smc.pivots(df, n=n)

    # 2. Derive Structure (Psychology of Sideways)
    curr_idx = len(df) - 1
    knowable = [p for p in pivots_list if p.known_at(curr_idx)]
    highs = [p for p in knowable if p.kind == "HIGH"]
    lows = [p for p in knowable if p.kind == "LOW"]

    struct_state = "SIDEWAYS"
    sideways_msg = None
    if len(highs) >= 2 and len(lows) >= 2:
        is_hh = highs[-1].price > highs[-2].price
        is_hl = lows[-1].price > lows[-2].price
        is_lh = highs[-1].price < highs[-2].price
        is_ll = lows[-1].price < lows[-2].price

        if is_hh and is_hl:
            struct_state = "UPTREND"
        elif is_lh and is_ll:
            struct_state = "DOWNTREND"
        else:
            struct_state = "SIDEWAYS"
            sideways_msg = (
                "Sideways structure: Buyers and sellers are balanced. "
                "Breakouts and breakdowns are highly vulnerable to whipsaws. Avoid directional bias."
            )
    else:
        sideways_msg = "Early session: Structural pivots unformed. Market is in transition."

    # 3. Higher-Timeframe Level Overlays
    htf_levels = extract_htf_levels(ohlc, spot, rules)

    # 4. Level Strength by Touch Count & Originating Moves
    tracked_levels = build_tracked_levels(
        df=df,
        pivots_list=pivots_list,
        spot=spot,
        atr_val=atr_val,
        read_levels=read_levels,
        htf_levels=htf_levels,
        rules=rules,
    )

    # 5. Stop-Loss Liquidity Pools
    pools, nearest_buy, nearest_sell = build_stop_loss_pools(
        tracked_levels=tracked_levels,
        df=df,
        spot=spot,
        atr_val=atr_val,
        rules=rules,
    )

    # 6. Candle Behaviour at Level
    candle_read = evaluate_candle_at_level(df, tracked_levels, rules)

    # Strongest level in play
    strongest = None
    if tracked_levels:
        strongest = max(tracked_levels, key=lambda x: (x.touch_count * 2.0 + x.originating_move_atr))

    return LiquiditySnapshot(
        timeframe=timeframe,
        spot_price=round(float(spot), 2),
        atr_points=round(atr_val, 2),
        nearest_buy_stops=nearest_buy,
        nearest_sell_stops=nearest_sell,
        strongest_level=strongest,
        candle_reading=candle_read,
        levels=tracked_levels,
        pools=pools,
        htf_levels=htf_levels,
        structure_state=struct_state,
        sideways_message=sideways_msg,
        rules=rules,
        candle_count=len(df),
        data_guard_warning=None,
    )
