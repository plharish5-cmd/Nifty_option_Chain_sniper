"""
engine/htf_wick_ema_rb.py — Institutional 1H / 4H Wick & Shaved Extremes + 1H 8-EMA
First-Touch Bounce/Rejection Engine & Quantitative Backtester Scored Against Null Baselines.

PHASE 1B REQUIREMENTS:
1. Random-entry baseline matched on time of day (400 runs per category).
2. Direction control: opposite side on identical bars, scored against random direction gap in sd.
3. Concentration: expectancy excluding best 1, 3, and 5 events (drop5 <= 0 fails).
4. Out of sample: chronological split-half (h1 vs h2).
5. Forward walk begins strictly after the signal bar closes (no lookahead, no 0 min hits).
6. Per-event table & time-to-resolution for every level type.
7. Explicit disclosure of 10 level categories and 20 cells tested.
8. Gated voice alerts: edge >= +2.0 sd, dir >= +2.0 sd, drop5 > 0, OOS positive required.
   Otherwise shipped as silent chart context only.
"""
from __future__ import annotations

import logging
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..market.candles import resample
from ..timeutil import chart_time, now_epoch, to_ist

# Re-use baseline machinery from scripts.backtest_ma_touch
try:
    from scripts.backtest_ma_touch import (  # type: ignore
        IST,
        STOPS,
        TARGETS,
        contribution,
        describe,
        expectancy,
        moving_average,
        session_bars,
        walk_forward,
        zscore,
    )
except ImportError:
    root_dir = Path(__file__).resolve().parent.parent.parent.parent.parent
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    from scripts.backtest_ma_touch import (  # type: ignore
        IST,
        STOPS,
        TARGETS,
        contribution,
        describe,
        expectancy,
        moving_average,
        session_bars,
        walk_forward,
        zscore,
    )

log = logging.getLogger("htf_wick_ema_rb")


def _get_rules() -> Dict[str, Any]:
    return config.rules().get("htf_wick_ema_rb", {})


def get_thresholds() -> Dict[str, float]:
    r = _get_rules()
    return {
        "wick_min_pts": float(r.get("wick_min_pts", 3.0)),
        "shaved_band_pts": float(r.get("shaved_band_pts", 4.0)),
        "ema_period": int(r.get("ema_period", 8)),
        "proximity_pts": float(r.get("proximity_pts", 5.0)),
        "target_20_pts": float(r.get("target_20_pts", 20.0)),
        "target_40_pts": float(r.get("target_40_pts", 40.0)),
        "adverse_stop_pts": float(r.get("adverse_stop_pts", 15.0)),
        "mfe_window_minutes": int(r.get("mfe_window_minutes", 60)),
        "max_active_levels": int(r.get("max_active_levels", 16)),
        "min_edge_sd": float(r.get("min_edge_sd", 2.0)),
        "min_dir_sd": float(r.get("min_dir_sd", 2.0)),
        "baseline_runs": int(r.get("baseline_runs", 400)),
        "horizon_hours": int(r.get("horizon_hours", 6)),
    }


@dataclass
class HtfLevel:
    id: str
    tf: str  # "1H" or "4H"
    kind: str  # e.g. "1H_UPPER_WICK", "1H_SHAVED_HIGH", "1H_LOWER_WICK", "1H_SHAVED_LOW"
    side: str  # "RESISTANCE" or "SUPPORT"
    top: float
    bottom: float
    ref_price: float
    wick_pts: float
    is_shaved: bool
    created_ts: int
    created_ist: str
    touch_count: int = 0
    is_virgin: bool = True
    direction_of_approach: Optional[str] = None  # "FROM_ABOVE_SUPPORT" or "FROM_BELOW_RESISTANCE"
    first_touch_ts: Optional[int] = None
    first_touch_price: Optional[float] = None
    mfe_bounce_pts: Optional[float] = None
    hit_20: bool = False
    hit_40: bool = False
    label: str = ""
    distance_pts: Optional[float] = None
    voice_armed: bool = False
    edge_z: float = 0.0
    edge_status: str = "UNPROVEN_EDGE (SILENT CONTEXT ONLY)"


def compute_1h_8ema(h1_candles: List[Dict[str, float]], period: int = 8) -> Tuple[Optional[float], str, List[Dict[str, Any]]]:
    """
    Computes 8-period EMA on closed 1H candles.
    Returns: (latest_ema, slope_str, history_series)
    """
    if not h1_candles:
        return None, "FLAT", []

    k = 2.0 / (period + 1)
    ema: Optional[float] = None
    history: List[Dict[str, Any]] = []

    for c in h1_candles:
        close = float(c["c"])
        if ema is None:
            ema = close
        else:
            ema = ema + k * (close - ema)
        history.append({
            "ts": chart_time(c["ts"]),
            "time": to_ist(c["ts"]).strftime("%Y-%m-%d %H:%M"),
            "val": round(ema, 2),
        })

    if len(history) >= 2:
        prev_ema = history[-2]["val"]
        curr_ema = history[-1]["val"]
        if curr_ema > prev_ema + 0.5:
            slope = "RISING"
        elif curr_ema < prev_ema - 0.5:
            slope = "FALLING"
        else:
            slope = "FLAT"
    else:
        slope = "FLAT"

    return ema, slope, history


def extract_htf_levels(candles: List[Dict[str, float]], tf: str, thresholds: Dict[str, float]) -> List[HtfLevel]:
    """
    Extracts upper and lower extreme levels from completed 1H or 4H candles.
    Handles standard wicks (>= 3.0 pts) and shaved candles (< 3.0 pts / zero-wick).
    """
    wick_min = thresholds["wick_min_pts"]
    shaved_band = thresholds["shaved_band_pts"]
    levels: List[HtfLevel] = []

    for c in candles:
        if c.get("closed") is False:
            continue

        ts = int(c["ts"])
        ist_str = to_ist(ts).strftime("%Y-%m-%d %H:%M")
        o, h, l, cl = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
        body_top = max(o, cl)
        body_bot = min(o, cl)

        # Upper Extreme (Resistance)
        upper_wick = h - body_top
        if upper_wick >= wick_min:
            kind = f"{tf}_UPPER_WICK"
            top = round(h, 2)
            bottom = round(body_top, 2)
            ref_price = top
            is_shaved = False
            label = f"{tf} Wick Res [{bottom:.1f} - {top:.1f}]"
        else:
            kind = f"{tf}_SHAVED_HIGH"
            top = round(h + shaved_band, 2)
            bottom = round(h - shaved_band, 2)
            ref_price = round(h, 2)
            is_shaved = True
            label = f"{tf} Shaved High {ref_price:.1f}"

        levels.append(HtfLevel(
            id=f"{tf}-{ts}-upper",
            tf=tf,
            kind=kind,
            side="RESISTANCE",
            top=top,
            bottom=bottom,
            ref_price=ref_price,
            wick_pts=round(upper_wick, 2),
            is_shaved=is_shaved,
            created_ts=ts,
            created_ist=ist_str,
            label=label,
        ))

        # Lower Extreme (Support)
        lower_wick = body_bot - l
        if lower_wick >= wick_min:
            kind = f"{tf}_LOWER_WICK"
            top = round(body_bot, 2)
            bottom = round(l, 2)
            ref_price = bottom
            is_shaved = False
            label = f"{tf} Wick Supp [{bottom:.1f} - {top:.1f}]"
        else:
            kind = f"{tf}_SHAVED_LOW"
            top = round(l + shaved_band, 2)
            bottom = round(l - shaved_band, 2)
            ref_price = round(l, 2)
            is_shaved = True
            label = f"{tf} Shaved Low {ref_price:.1f}"

        levels.append(HtfLevel(
            id=f"{tf}-{ts}-lower",
            tf=tf,
            kind=kind,
            side="SUPPORT",
            top=top,
            bottom=bottom,
            ref_price=ref_price,
            wick_pts=round(lower_wick, 2),
            is_shaved=is_shaved,
            created_ts=ts,
            created_ist=ist_str,
            label=label,
        ))

    return levels


def run_null_scored_backtest(
    raw_1m: List[Dict[str, Any]],
    thresholds: Dict[str, float],
    seed: int = 20261101,
) -> Dict[str, Any]:
    """
    Executes full Phase 1B quantitative backtest scored against null baselines.
    Tests 10 level categories across 20 cells (Stop 15 / Target 20 & 40).
    Reuses scripts.backtest_ma_touch machinery directly.
    """
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in raw_1m:
        ts = int(r["ts"])
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({
            "ts": ts,
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
        })

    days = sorted(by_day)
    if not days:
        return {"ok": False, "error": "No trading days available."}

    # Build 1H and 4H session bars aligned to 09:15
    hourly: List[Dict[str, Any]] = []
    for d in days:
        for b in session_bars(by_day[d], 60):
            b["day"] = d
            hourly.append(b)

    h4_bars: List[Dict[str, Any]] = []
    for d in days:
        for b in session_bars(by_day[d], 240):
            b["day"] = d
            h4_bars.append(b)

    horizon_hours = int(thresholds["horizon_hours"])
    runs = int(thresholds["baseline_runs"])
    prox = thresholds["proximity_pts"]
    min_edge_sd = thresholds["min_edge_sd"]
    min_dir_sd = thresholds["min_dir_sd"]
    rng = random.Random(seed)

    # Pre-build base pool for 1H
    base_pool_1h: List[Dict[str, Any]] = []
    for i in range(len(hourly)):
        b = hourly[i]
        end_ts = b["ts_end"] + horizon_hours * 3600
        after = [r for r in by_day[b["day"]] if b["ts_end"] < r["ts"] <= end_ts]
        if len(after) >= 10:
            base_pool_1h.append({
                "idx": i,
                "slot": b["slot"],
                "day": b["day"],
                "c": b["c"],
                "long_res": {**walk_forward(after, b["c"], True), "entry": b["c"], "long": True},
                "short_res": {**walk_forward(after, b["c"], False), "entry": b["c"], "long": False},
            })

    by_slot_1h: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in base_pool_1h:
        by_slot_1h[e["slot"]].append(e)

    # Pre-build base pool for 4H
    base_pool_4h: List[Dict[str, Any]] = []
    for i in range(len(h4_bars)):
        b = h4_bars[i]
        end_ts = b["ts_end"] + horizon_hours * 3600
        after = [r for r in by_day[b["day"]] if b["ts_end"] < r["ts"] <= end_ts]
        if len(after) >= 10:
            base_pool_4h.append({
                "idx": i,
                "slot": b["slot"],
                "day": b["day"],
                "c": b["c"],
                "long_res": {**walk_forward(after, b["c"], True), "entry": b["c"], "long": True},
                "short_res": {**walk_forward(after, b["c"], False), "entry": b["c"], "long": False},
            })

    by_slot_4h: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in base_pool_4h:
        by_slot_4h[e["slot"]].append(e)

    evs_by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    # 1. Collect 1H level events
    active_u_wick_1h: List[Tuple[int, float, float]] = []
    active_sh_hi_1h: List[Tuple[int, float, float]] = []
    active_l_wick_1h: List[Tuple[int, float, float]] = []
    active_sh_lo_1h: List[Tuple[int, float, float]] = []

    for j in range(len(hourly)):
        hj = hourly[j]
        end_ts = hj["ts_end"] + horizon_hours * 3600
        after = [r for r in by_day[hj["day"]] if hj["ts_end"] < r["ts"] <= end_ts]
        if len(after) < 10:
            continue

        prev_close = hourly[j - 1]["c"] if j > 0 else hj["o"]

        # Upper Wicks (Resistance, Short)
        hit_u_wick = False
        rem_uw = []
        for k_idx, bot, top in active_u_wick_1h:
            if not hit_u_wick and prev_close <= bot + prox and hj["h"] >= bot - prox:
                hit_u_wick = True
            elif hj["h"] >= bot - prox:
                pass  # exhausted
            else:
                rem_uw.append((k_idx, bot, top))
        active_u_wick_1h = rem_uw
        if hit_u_wick:
            w = walk_forward(after, hj["c"], False)
            flip = walk_forward(after, hj["c"], True)
            evs_by_type["1H_UPPER_WICK"].append({
                **w, "long": False, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": True, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Shaved High (Resistance, Short)
        hit_sh_hi = False
        rem_sh = []
        for k_idx, bot, top in active_sh_hi_1h:
            if not hit_sh_hi and prev_close <= bot + prox and hj["h"] >= bot - prox:
                hit_sh_hi = True
            elif hj["h"] >= bot - prox:
                pass
            else:
                rem_sh.append((k_idx, bot, top))
        active_sh_hi_1h = rem_sh
        if hit_sh_hi:
            w = walk_forward(after, hj["c"], False)
            flip = walk_forward(after, hj["c"], True)
            evs_by_type["1H_SHAVED_HIGH"].append({
                **w, "long": False, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": True, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Lower Wicks (Support, Long)
        hit_l_wick = False
        rem_lw = []
        for k_idx, bot, top in active_l_wick_1h:
            if not hit_l_wick and prev_close >= top - prox and hj["l"] <= top + prox:
                hit_l_wick = True
            elif hj["l"] <= top + prox:
                pass
            else:
                rem_lw.append((k_idx, bot, top))
        active_l_wick_1h = rem_lw
        if hit_l_wick:
            w = walk_forward(after, hj["c"], True)
            flip = walk_forward(after, hj["c"], False)
            evs_by_type["1H_LOWER_WICK"].append({
                **w, "long": True, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": False, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Shaved Low (Support, Long)
        hit_sh_lo = False
        rem_sl = []
        for k_idx, bot, top in active_sh_lo_1h:
            if not hit_sh_lo and prev_close >= top - prox and hj["l"] <= top + prox:
                hit_sh_lo = True
            elif hj["l"] <= top + prox:
                pass
            else:
                rem_sl.append((k_idx, bot, top))
        active_sh_lo_1h = rem_sl
        if hit_sh_lo:
            w = walk_forward(after, hj["c"], True)
            flip = walk_forward(after, hj["c"], False)
            evs_by_type["1H_SHAVED_LOW"].append({
                **w, "long": True, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": False, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Register hj's own levels for subsequent bars
        u_w = hj["h"] - max(hj["o"], hj["c"])
        l_w = min(hj["o"], hj["c"]) - hj["l"]
        if u_w >= thresholds["wick_min_pts"]:
            active_u_wick_1h.append((j, max(hj["o"], hj["c"]), hj["h"]))
        else:
            active_sh_hi_1h.append((j, hj["h"] - thresholds["shaved_band_pts"], hj["h"] + thresholds["shaved_band_pts"]))

        if l_w >= thresholds["wick_min_pts"]:
            active_l_wick_1h.append((j, hj["l"], min(hj["o"], hj["c"])))
        else:
            active_sh_lo_1h.append((j, hj["l"] - thresholds["shaved_band_pts"], hj["l"] + thresholds["shaved_band_pts"]))

    # 2. Collect 4H level events
    active_u_wick_4h: List[Tuple[int, float, float]] = []
    active_sh_hi_4h: List[Tuple[int, float, float]] = []
    active_l_wick_4h: List[Tuple[int, float, float]] = []
    active_sh_lo_4h: List[Tuple[int, float, float]] = []

    for j in range(len(h4_bars)):
        hj = h4_bars[j]
        end_ts = hj["ts_end"] + horizon_hours * 3600
        after = [r for r in by_day[hj["day"]] if hj["ts_end"] < r["ts"] <= end_ts]
        if len(after) < 10:
            continue

        prev_close = h4_bars[j - 1]["c"] if j > 0 else hj["o"]

        # Upper Wicks (Resistance, Short)
        hit_u_wick = False
        rem_uw = []
        for k_idx, bot, top in active_u_wick_4h:
            if not hit_u_wick and prev_close <= bot + prox and hj["h"] >= bot - prox:
                hit_u_wick = True
            elif hj["h"] >= bot - prox:
                pass
            else:
                rem_uw.append((k_idx, bot, top))
        active_u_wick_4h = rem_uw
        if hit_u_wick:
            w = walk_forward(after, hj["c"], False)
            flip = walk_forward(after, hj["c"], True)
            evs_by_type["4H_UPPER_WICK"].append({
                **w, "long": False, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": True, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Shaved High (Resistance, Short)
        hit_sh_hi = False
        rem_sh = []
        for k_idx, bot, top in active_sh_hi_4h:
            if not hit_sh_hi and prev_close <= bot + prox and hj["h"] >= bot - prox:
                hit_sh_hi = True
            elif hj["h"] >= bot - prox:
                pass
            else:
                rem_sh.append((k_idx, bot, top))
        active_sh_hi_4h = rem_sh
        if hit_sh_hi:
            w = walk_forward(after, hj["c"], False)
            flip = walk_forward(after, hj["c"], True)
            evs_by_type["4H_SHAVED_HIGH"].append({
                **w, "long": False, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": True, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Lower Wicks (Support, Long)
        hit_l_wick = False
        rem_lw = []
        for k_idx, bot, top in active_l_wick_4h:
            if not hit_l_wick and prev_close >= top - prox and hj["l"] <= top + prox:
                hit_l_wick = True
            elif hj["l"] <= top + prox:
                pass
            else:
                rem_lw.append((k_idx, bot, top))
        active_l_wick_4h = rem_lw
        if hit_l_wick:
            w = walk_forward(after, hj["c"], True)
            flip = walk_forward(after, hj["c"], False)
            evs_by_type["4H_LOWER_WICK"].append({
                **w, "long": True, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": False, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Shaved Low (Support, Long)
        hit_sh_lo = False
        rem_sl = []
        for k_idx, bot, top in active_sh_lo_4h:
            if not hit_sh_lo and prev_close >= top - prox and hj["l"] <= top + prox:
                hit_sh_lo = True
            elif hj["l"] <= top + prox:
                pass
            else:
                rem_sl.append((k_idx, bot, top))
        active_sh_lo_4h = rem_sl
        if hit_sh_lo:
            w = walk_forward(after, hj["c"], True)
            flip = walk_forward(after, hj["c"], False)
            evs_by_type["4H_SHAVED_LOW"].append({
                **w, "long": True, "entry": hj["c"], "day": hj["day"], "slot": hj["slot"],
                "flip": {**flip, "long": False, "entry": hj["c"]},
                "time": datetime.fromtimestamp(hj["ts"], IST).strftime("%H:%M"),
                "ts": hj["ts"], "ts_end": hj["ts_end"],
            })

        # Register hj's own levels
        u_w = hj["h"] - max(hj["o"], hj["c"])
        l_w = min(hj["o"], hj["c"]) - hj["l"]
        if u_w >= thresholds["wick_min_pts"]:
            active_u_wick_4h.append((j, max(hj["o"], hj["c"]), hj["h"]))
        else:
            active_sh_hi_4h.append((j, hj["h"] - thresholds["shaved_band_pts"], hj["h"] + thresholds["shaved_band_pts"]))

        if l_w >= thresholds["wick_min_pts"]:
            active_l_wick_4h.append((j, hj["l"], min(hj["o"], hj["c"])))
        else:
            active_sh_lo_4h.append((j, hj["l"] - thresholds["shaved_band_pts"], hj["l"] + thresholds["shaved_band_pts"]))

    # 3. Collect 1H 8-EMA events
    ema_vals = moving_average([b["c"] for b in hourly], int(thresholds["ema_period"]), "ema")
    last_touch = -99
    gap = 3

    for i in range(1, len(hourly)):
        if ema_vals[i] is None or ema_vals[i - 1] is None:
            continue
        b = hourly[i]
        end_ts = b["ts_end"] + horizon_hours * 3600
        after = [r for r in by_day[b["day"]] if b["ts_end"] < r["ts"] <= end_ts]
        if len(after) < 10:
            continue
        m = ema_vals[i]
        if b["l"] <= m <= b["h"]:
            if i - last_touch > gap:
                long = hourly[i - 1]["c"] > ema_vals[i - 1]
                w = walk_forward(after, b["c"], long)
                flip = walk_forward(after, b["c"], not long)
                ev = {
                    **w,
                    "long": long,
                    "entry": b["c"],
                    "ma": m,
                    "day": b["day"],
                    "slot": b["slot"],
                    "ts": b["ts"],
                    "ts_end": b["ts_end"],
                    "flip": {**flip, "long": not long, "entry": b["c"]},
                    "time": datetime.fromtimestamp(b["ts"], IST).strftime("%H:%M"),
                }
                if long:
                    evs_by_type["1H_8EMA_SUPPORT"].append(ev)
                else:
                    evs_by_type["1H_8EMA_RESISTANCE"].append(ev)
            last_touch = i

    # Categories to evaluate
    all_categories = [
        ("1H_UPPER_WICK", "RESISTANCE", False, "1H"),
        ("1H_SHAVED_HIGH", "RESISTANCE", False, "1H"),
        ("1H_LOWER_WICK", "SUPPORT", True, "1H"),
        ("1H_SHAVED_LOW", "SUPPORT", True, "1H"),
        ("4H_UPPER_WICK", "RESISTANCE", False, "4H"),
        ("4H_SHAVED_HIGH", "RESISTANCE", False, "4H"),
        ("4H_LOWER_WICK", "SUPPORT", True, "4H"),
        ("4H_SHAVED_LOW", "SUPPORT", True, "4H"),
        ("1H_8EMA_SUPPORT", "SUPPORT", True, "1H"),
        ("1H_8EMA_RESISTANCE", "RESISTANCE", False, "1H"),
    ]

    scored_categories: Dict[str, Any] = {}
    per_event_tables: Dict[str, List[Dict[str, Any]]] = {}

    for cat_name, side, is_long, cat_tf in all_categories:
        evs = evs_by_type.get(cat_name, [])
        n = len(evs)
        base_pool = base_pool_1h if cat_tf == "1H" else base_pool_4h
        by_slot = by_slot_1h if cat_tf == "1H" else by_slot_4h

        cat_result: Dict[str, Any] = {
            "name": cat_name,
            "side": side,
            "direction": "LONG" if is_long else "SHORT",
            "tf": cat_tf,
            "sample_n": n,
            "voice_armed": False,
            "edge_status": "UNPROVEN_EDGE (SILENT CONTEXT ONLY)",
            "best_edge_z": 0.0,
            "best_cell": "s15_t20",
        }

        # Build Per-Event Table
        per_event_rows: List[Dict[str, Any]] = []
        for e in sorted(evs, key=lambda x: (x["day"], x["time"])):
            si = e["stop_at"][15.0]
            ti_20 = e["tgt_at"][20.0]
            ti_40 = e["tgt_at"][40.0]

            # Outcome 15/20
            if ti_20 is not None and (si is None or ti_20 < si):
                out_20 = f"WIN +20 in {ti_20 + 1} min"
            elif si is not None:
                out_20 = f"LOSS -15 in {si + 1} min"
            else:
                c20 = contribution(e, 15.0, 20.0)
                out_20 = f"open ({c20:+.0f})"

            # Outcome 15/40
            if ti_40 is not None and (si is None or ti_40 < si):
                out_40 = f"WIN +40 in {ti_40 + 1} min"
            elif si is not None:
                out_40 = f"LOSS -15 in {si + 1} min"
            else:
                c40 = contribution(e, 15.0, 40.0)
                out_40 = f"open ({c40:+.0f})"

            per_event_rows.append({
                "date": e["day"],
                "time": e["time"],
                "dir": "LONG" if e["long"] else "SHORT",
                "entry": round(e["entry"], 1),
                "mfe": round(e["mfe"], 1),
                "mae": round(e["mae"], 1),
                "outcome_20": out_20,
                "outcome_40": out_40,
                "time_to_target_20": (ti_20 + 1) if ti_20 is not None else None,
                "time_to_target_40": (ti_40 + 1) if ti_40 is not None else None,
                "time_to_stop_15": (si + 1) if si is not None else None,
            })
        per_event_tables[cat_name] = per_event_rows

        if n < 5:
            cat_result["status_note"] = f"Sample N={n} too small for statistical significance."
            scored_categories[cat_name] = cat_result
            continue

        best_z = -999.0
        best_cell_name = "s15_t20"
        cat_voice_armed = False

        # Evaluate Stop 15 / Target 20 & 40
        for s, t, cell_key in [(15.0, 20.0, "s15_t20"), (15.0, 40.0, "s15_t40")]:
            w_cnt, l_cnt, o_cnt, real_exp = expectancy(evs, s, t)
            win_pct = round((w_cnt / n) * 100.0, 1)

            # Random-entry baseline matched on slot
            samples = [[rng.choice(by_slot[e["slot"]]) if by_slot[e["slot"]] else rng.choice(base_pool) for e in evs] for _ in range(runs)]
            rand_exps: List[float] = []
            rand_gaps: List[float] = []

            for sm in samples:
                sm_evs = [x["long_res"] if is_long else x["short_res"] for x in sm]
                sm_flips = [x["short_res"] if is_long else x["long_res"] for x in sm]
                re = expectancy(sm_evs, s, t)[3]
                rf = expectancy(sm_flips, s, t)[3]
                rand_exps.append(re)
                rand_gaps.append(re - rf)

            rm, rsd, edge_z = zscore(real_exp, rand_exps)

            # Direction control
            fl = expectancy([e["flip"] for e in evs], s, t)[3]
            gap = real_exp - fl
            gm, gsd, dir_z = zscore(gap, rand_gaps)

            # Concentration
            contribs = sorted((contribution(e, s, t) for e in evs), reverse=True)
            tot_pts = sum(contribs)
            drop1 = round((tot_pts - sum(contribs[:1])) / max(1, n - 1), 1) if n > 1 else 0.0
            drop3 = round((tot_pts - sum(contribs[:3])) / max(1, n - 3), 1) if n > 3 else 0.0
            drop5 = round((tot_pts - sum(contribs[:5])) / max(1, n - 5), 1) if n > 5 else 0.0

            # Out of Sample chronological split-half
            udays = sorted({e["day"] for e in evs})
            cut = udays[len(udays) // 2] if udays else ""
            h1 = [e for e in evs if e["day"] < cut]
            h2 = [e for e in evs if e["day"] >= cut]
            eh1 = round(expectancy(h1, s, t)[3], 1) if len(h1) >= 2 else 0.0
            eh2 = round(expectancy(h2, s, t)[3], 1) if len(h2) >= 2 else 0.0

            # Time to resolution
            tt = [e["tgt_at"][t] + 1 for e in evs if e["tgt_at"][t] is not None]
            ts = [e["stop_at"][s] + 1 for e in evs if e["stop_at"][s] is not None]
            med_tgt = round(float(statistics.median(tt)), 1) if tt else None
            med_stop = round(float(statistics.median(ts)), 1) if ts else None

            # Voice Gating Check (Items 1-4)
            cell_armed = (edge_z >= min_edge_sd and dir_z >= min_dir_sd and drop5 > 0.0 and eh1 > 0.0 and eh2 > 0.0)
            if cell_armed:
                cat_voice_armed = True

            cell_dict = {
                "sample_n": n,
                "stop": s,
                "target": t,
                "wins": w_cnt,
                "losses": l_cnt,
                "open": o_cnt,
                "win_pct": win_pct,
                "real_exp": round(real_exp, 1),
                "rand_mean": round(rm, 1),
                "rand_sd": round(rsd, 1),
                "edge_z": round(edge_z, 1),
                "dir_flip_exp": round(fl, 1),
                "dir_gap": round(gap, 1),
                "dir_z": round(dir_z, 1),
                "drop1": drop1,
                "drop3": drop3,
                "drop5": drop5,
                "oos_h1_exp": eh1,
                "oos_h2_exp": eh2,
                "median_time_target_min": med_tgt,
                "median_time_stop_min": med_stop,
                "voice_armed": cell_armed,
            }
            cat_result[cell_key] = cell_dict

            if edge_z > best_z:
                best_z = edge_z
                best_cell_name = cell_key

        cat_result["voice_armed"] = cat_voice_armed
        cat_result["best_edge_z"] = round(best_z, 1)
        cat_result["best_cell"] = best_cell_name
        cat_result["edge_status"] = "ACTIONABLE_EDGE (VOICE ARMED)" if cat_voice_armed else "UNPROVEN_EDGE (SILENT CONTEXT ONLY)"
        scored_categories[cat_name] = cat_result

    summary = {
        "ok": True,
        "metadata": {
            "categories_tested": 10,
            "cells_tested": 20,
            "statement": "Tested 10 level categories across 20 cells. Multiple-comparison discount applies. Gated: Edge >= +2.0 sd, Direction >= +2.0 sd, drop5 > 0, and OOS positive required for voice alerts.",
            "dataset_range": f"{days[0]} to {days[-1]}",
            "total_sessions": len(days),
            "total_intraday_candles": len(raw_1m),
            "total_1h_candles": len(hourly),
            "total_4h_candles": len(h4_bars),
        },
        "categories": scored_categories,
        "per_event_tables": per_event_tables,
    }

    return summary


def generate_live_alerts_and_voice(
    active_levels: List[HtfLevel],
    ema8_val: Optional[float],
    current_spot: float,
    thresholds: Dict[str, float],
    null_summary: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Generates live proximity alerts with strict voice alert gating.
    If level category edge is under +2.0 sd or unproven, shipped as SILENT chart context only.
    """
    prox = thresholds["proximity_pts"]
    alerts: List[Dict[str, Any]] = []
    cat_stats_map = null_summary.get("categories", {}) if null_summary else {}

    # 1. Check virgin & first-touch HTF levels
    for lvl in active_levels:
        if lvl.touch_count > 1:
            continue  # Exhausted

        dist = abs(current_spot - lvl.ref_price)
        if dist <= prox:
            price_fmt = f"{lvl.ref_price:,.1f}"
            tf_spoken = "one hour" if lvl.tf == "1H" else "four hour"

            cat_info = cat_stats_map.get(lvl.kind, {})
            is_armed = bool(cat_info.get("voice_armed", False))
            edge_z = cat_info.get("best_edge_z", 0.0)

            if lvl.side == "RESISTANCE":
                if is_armed:
                    voice_text = (
                        f"Alert: NIFTY is approaching the {tf_spoken} resistance at {price_fmt} "
                        "from below for the first time. Watch for rejection."
                    )
                    title = f"🚨 {lvl.tf} Resistance Rejection ({price_fmt}) [VOICE ARMED]"
                else:
                    voice_text = ""
                    title = f"ℹ️ {lvl.tf} Resistance Zone ({price_fmt}) [Silent: Z {edge_z:+.1f} sd]"

                body = (
                    f"First touch test of {lvl.label}. "
                    + (f"Proven edge (+{edge_z:.1f} sd)." if is_armed else f"Silent context only (Edge Z: {edge_z:+.1f} sd < +2.0 sd threshold. No chime or voice).")
                )

                alerts.append({
                    "id": f"alert-{lvl.id}",
                    "level_id": lvl.id,
                    "kind": lvl.kind,
                    "side": "SELL",
                    "tf": lvl.tf,
                    "price": lvl.ref_price,
                    "distance_pts": round(dist, 2),
                    "title": title,
                    "body": body,
                    "voice_text": voice_text,
                    "voice_armed": is_armed,
                    "silent": not is_armed,
                    "edge_z": edge_z,
                })

            elif lvl.side == "SUPPORT":
                if is_armed:
                    voice_text = (
                        f"Alert: NIFTY is coming near the {tf_spoken} support at {price_fmt} "
                        "from above for the first time. Watch for bounce."
                    )
                    title = f"🟢 {lvl.tf} Support Zone ({price_fmt}) [VOICE ARMED]"
                else:
                    voice_text = ""
                    title = f"ℹ️ {lvl.tf} Support Zone ({price_fmt}) [Silent: Z {edge_z:+.1f} sd]"

                body = (
                    f"First touch test of {lvl.label}. "
                    + (f"Proven edge (+{edge_z:.1f} sd)." if is_armed else f"Silent context only (Edge Z: {edge_z:+.1f} sd < +2.0 sd threshold. No chime or voice).")
                )

                alerts.append({
                    "id": f"alert-{lvl.id}",
                    "level_id": lvl.id,
                    "kind": lvl.kind,
                    "side": "BUY",
                    "tf": lvl.tf,
                    "price": lvl.ref_price,
                    "distance_pts": round(dist, 2),
                    "title": title,
                    "body": body,
                    "voice_text": voice_text,
                    "voice_armed": is_armed,
                    "silent": not is_armed,
                    "edge_z": edge_z,
                })

    # 2. Check 1H 8-EMA
    if ema8_val is not None:
        ema_dist = abs(current_spot - ema8_val)
        if ema_dist <= prox:
            price_fmt = f"{ema8_val:,.1f}"
            if current_spot >= ema8_val:
                cat_info = cat_stats_map.get("1H_8EMA_SUPPORT", {})
                is_armed = bool(cat_info.get("voice_armed", False))
                edge_z = cat_info.get("best_edge_z", 0.0)

                if is_armed:
                    voice_text = (
                        f"Alert: NIFTY is above the one hour 8 EMA and pulling back to test 8 EMA support at {price_fmt}."
                    )
                    title = f"⚡ 1H 8-EMA Support Test ({price_fmt}) [VOICE ARMED]"
                else:
                    voice_text = ""
                    title = f"ℹ️ 1H 8-EMA Support Test ({price_fmt}) [Silent: Z {edge_z:+.1f} sd]"

                alerts.append({
                    "id": f"alert-ema8-support-{int(ema8_val)}",
                    "level_id": "1H-EMA8",
                    "kind": "1H_EMA8_SUPPORT",
                    "side": "BUY",
                    "tf": "1H",
                    "price": round(ema8_val, 2),
                    "distance_pts": round(ema_dist, 2),
                    "title": title,
                    "body": f"NIFTY pulling back to test 1H 8 EMA ({price_fmt}). "
                            + (f"Proven edge (+{edge_z:.1f} sd)." if is_armed else f"Silent context only (Edge Z: {edge_z:+.1f} sd < +2.0 sd threshold. No chime or voice)."),
                    "voice_text": voice_text,
                    "voice_armed": is_armed,
                    "silent": not is_armed,
                    "edge_z": edge_z,
                })
            else:
                cat_info = cat_stats_map.get("1H_8EMA_RESISTANCE", {})
                is_armed = bool(cat_info.get("voice_armed", False))
                edge_z = cat_info.get("best_edge_z", 0.0)

                if is_armed:
                    voice_text = (
                        f"Alert: NIFTY is coming from below the one hour 8 EMA and touching 8 EMA resistance at {price_fmt}."
                    )
                    title = f"⚡ 1H 8-EMA Resistance Test ({price_fmt}) [VOICE ARMED]"
                else:
                    voice_text = ""
                    title = f"ℹ️ 1H 8-EMA Resistance Test ({price_fmt}) [Silent: Z {edge_z:+.1f} sd]"

                alerts.append({
                    "id": f"alert-ema8-resistance-{int(ema8_val)}",
                    "level_id": "1H-EMA8",
                    "kind": "1H_EMA8_RESISTANCE",
                    "side": "SELL",
                    "tf": "1H",
                    "price": round(ema8_val, 2),
                    "distance_pts": round(ema_dist, 2),
                    "title": title,
                    "body": f"NIFTY rallying to test 1H 8 EMA ({price_fmt}). "
                            + (f"Proven edge (+{edge_z:.1f} sd)." if is_armed else f"Silent context only (Edge Z: {edge_z:+.1f} sd < +2.0 sd threshold. No chime or voice)."),
                    "voice_text": voice_text,
                    "voice_armed": is_armed,
                    "silent": not is_armed,
                    "edge_z": edge_z,
                })

    return alerts


_cached_payload: Optional[Dict[str, Any]] = None
_cached_time: float = 0.0
_cached_last_ts: int = 0


def get_htf_wick_ema_rb(sec_id: str = "13", spot: Optional[float] = None) -> Dict[str, Any]:
    """
    Main entry point for GET /api/desk/htf-wick-ema-rb.
    """
    global _cached_payload, _cached_time, _cached_last_ts
    thresh = get_thresholds()

    # Load 1m candles
    raw_1m = db.candles_1m(sec_id, since=0)
    if not raw_1m:
        return {
            "ok": False,
            "error": "No candle data found for sec_id " + sec_id,
        }

    latest_bar_ts = int(raw_1m[-1]["ts"])
    now_t = now_epoch()

    # Fast cache check (30 second TTL if no new candle arrived)
    if _cached_payload is not None and latest_bar_ts == _cached_last_ts and (now_t - _cached_time < 30.0):
        effective_spot = spot if spot is not None and spot > 0 else float(raw_1m[-1]["c"])
        out = dict(_cached_payload)
        out["spot"] = effective_spot
        if out.get("ema8_1h") and out["ema8_1h"].get("value") is not None:
            ema_v = out["ema8_1h"]["value"]
            dist = round(effective_spot - ema_v, 2)
            out["ema8_1h"]["distance_pts"] = dist
            out["ema8_1h"]["position"] = "ABOVE_EMA8" if dist >= 0 else "BELOW_EMA8"
            if abs(dist) <= thresh["proximity_pts"]:
                out["ema8_1h"]["approach_state"] = "TESTING_FROM_ABOVE_SUPPORT" if dist >= 0 else "TESTING_FROM_BELOW_RESISTANCE"
            else:
                out["ema8_1h"]["approach_state"] = "NONE"
        return out

    # Resample
    h1 = resample(raw_1m, "1h")
    h4 = resample(raw_1m, "4h")

    # Latest spot price
    if spot is None or spot <= 0:
        spot = float(raw_1m[-1]["c"])

    # 1H 8-EMA calculation
    ema8_val, ema8_slope, ema8_hist = compute_1h_8ema(h1, period=int(thresh["ema_period"]))

    ema8_dist = round(spot - ema8_val, 2) if ema8_val is not None else 0.0
    ema8_pos = "ABOVE_EMA8" if ema8_dist >= 0 else "BELOW_EMA8"
    if ema8_val is not None and abs(ema8_dist) <= thresh["proximity_pts"]:
        approach_state = "TESTING_FROM_ABOVE_SUPPORT" if ema8_pos == "ABOVE_EMA8" else "TESTING_FROM_BELOW_RESISTANCE"
    else:
        approach_state = "NONE"

    # Extract levels
    levels_1h = extract_htf_levels(h1, "1H", thresh)
    levels_4h = extract_htf_levels(h4, "4H", thresh)
    all_levels = levels_1h + levels_4h

    # Run Phase 1B Null-Scored Quantitative Backtest
    null_summary = run_null_scored_backtest(raw_1m, thresh)
    cat_stats_map = null_summary.get("categories", {})

    # Tag levels with gating status & edge Z-score
    for lvl in all_levels:
        lvl.distance_pts = round(abs(spot - lvl.ref_price), 2)
        cat_info = cat_stats_map.get(lvl.kind, {})
        lvl.voice_armed = bool(cat_info.get("voice_armed", False))
        lvl.edge_z = float(cat_info.get("best_edge_z", 0.0))
        lvl.edge_status = str(cat_info.get("edge_status", "UNPROVEN_EDGE"))

        label_base = lvl.label
        if lvl.touch_count == 0:
            status_tag = "Virgin"
        elif lvl.touch_count == 1:
            status_tag = "1st Touch"
        else:
            status_tag = "Exhausted"

        edge_tag = f"Z: {lvl.edge_z:+.1f} sd"
        voice_tag = "Voice Armed" if lvl.voice_armed else "Silent"
        lvl.label = f"{label_base} ({status_tag}, {edge_tag}, {voice_tag})"

    # Prioritize virgin and first-touch levels closest to spot
    active_candidates = [l for l in all_levels if l.touch_count <= 1 and (l.distance_pts is not None and l.distance_pts <= 450.0)]
    active_candidates.sort(key=lambda x: (x.touch_count, x.distance_pts if x.distance_pts is not None else 9999))
    max_active = int(thresh["max_active_levels"])
    active_levels = active_candidates[:max_active]

    # Generate live alerts (gated strictly)
    live_alerts = generate_live_alerts_and_voice(active_levels, ema8_val, spot, thresh, null_summary)

    # Dates
    first_date = to_ist(raw_1m[0]["ts"]).strftime("%Y-%m-%d")
    latest_date = to_ist(raw_1m[-1]["ts"]).strftime("%Y-%m-%d")
    latest_ist = to_ist(raw_1m[-1]["ts"]).strftime("%Y-%m-%d %H:%M:%S IST")

    # Compatibility shim for existing dashboard consumers
    w1_s = cat_stats_map.get("1H_UPPER_WICK", {}).get("s15_t20", {})
    w4_s = cat_stats_map.get("4H_UPPER_WICK", {}).get("s15_t20", {})
    ema_s = cat_stats_map.get("1H_8EMA_SUPPORT", {}).get("s15_t20", {})

    legacy_summary = {
        "dataset_range": f"{first_date} to {latest_date}",
        "total_intraday_candles": len(raw_1m),
        "total_1h_candles": len(h1),
        "total_4h_candles": len(h4),
        "wick_1h": {
            "first_touches": w1_s.get("sample_n", 0),
            "target_20_wins": w1_s.get("wins", 0),
            "win_rate_20_pct": w1_s.get("win_pct", 0.0),
            "target_40_wins": cat_stats_map.get("1H_UPPER_WICK", {}).get("s15_t40", {}).get("wins", 0),
            "win_rate_40_pct": cat_stats_map.get("1H_UPPER_WICK", {}).get("s15_t40", {}).get("win_pct", 0.0),
            "avg_mfe_pts": w1_s.get("real_exp", 0.0),
            "stopped_out": w1_s.get("losses", 0),
        },
        "wick_4h": {
            "first_touches": w4_s.get("sample_n", 0),
            "target_20_wins": w4_s.get("wins", 0),
            "win_rate_20_pct": w4_s.get("win_pct", 0.0),
            "target_40_wins": cat_stats_map.get("4H_UPPER_WICK", {}).get("s15_t40", {}).get("wins", 0),
            "win_rate_40_pct": cat_stats_map.get("4H_UPPER_WICK", {}).get("s15_t40", {}).get("win_pct", 0.0),
            "avg_mfe_pts": w4_s.get("real_exp", 0.0),
            "stopped_out": w4_s.get("losses", 0),
        },
        "ema8_1h": {
            "first_touches": ema_s.get("sample_n", 0),
            "target_20_wins": ema_s.get("wins", 0),
            "win_rate_20_pct": ema_s.get("win_pct", 0.0),
            "target_40_wins": cat_stats_map.get("1H_8EMA_SUPPORT", {}).get("s15_t40", {}).get("wins", 0),
            "win_rate_40_pct": cat_stats_map.get("1H_8EMA_SUPPORT", {}).get("s15_t40", {}).get("win_pct", 0.0),
            "avg_mfe_pts": ema_s.get("real_exp", 0.0),
            "stopped_out": ema_s.get("losses", 0),
        },
    }

    res = {
        "ok": True,
        "as_of_ist": latest_ist,
        "spot": spot,
        "ema8_1h": {
            "value": round(ema8_val, 2) if ema8_val is not None else None,
            "slope": ema8_slope,
            "position": ema8_pos,
            "distance_pts": ema8_dist,
            "approach_state": approach_state,
            "history": ema8_hist[-120:],  # last 120 hourly bars for charting
        },
        "active_levels": [asdict(l) for l in active_levels],
        "live_alerts": live_alerts,
        "backtest_summary": legacy_summary,
        "null_baseline_summary": null_summary,
        "per_event_tables": null_summary.get("per_event_tables", {}),
    }

    _cached_payload = res
    _cached_time = now_t
    _cached_last_ts = latest_bar_ts

    return res


def print_backtest_table() -> None:
    """
    CLI command to print the full Phase 1B Null-Baseline Backtest Summary Table
    and Per-Event Tables in the terminal.
    """
    res = get_htf_wick_ema_rb(sec_id="13")
    if not res.get("ok"):
        print(f"Error: {res.get('error')}")
        return

    null_res = res.get("null_baseline_summary", {})
    meta = null_res.get("metadata", {})
    cats = null_res.get("categories", {})
    ev_tables = res.get("per_event_tables", {})

    print("=" * 115)
    print(" 🎯 REJECTION BLOCKS (RB): 1H/4H WICK & 1H 8-EMA NULL-BASELINE QUANTITATIVE BACKTEST")
    print(f" Dataset: {meta.get('dataset_range')} ({meta.get('total_sessions')} sessions) | 1m: {meta.get('total_intraday_candles'):,} | 1H: {meta.get('total_1h_candles')} | 4H: {meta.get('total_4h_candles')}")
    print(f" Disclosure: {meta.get('statement')}")
    print("=" * 115)

    hdr = f"{'CATEGORY':<19} | {'CELL':<7} | {'N':>3} | {'WIN%':>5} | {'REAL EXP':>8} | {'RAND NULL':>12} | {'EDGE Z':>7} | {'DIR Z':>7} | {'DROP5':>6} | {'OOS H1/H2':>11} | {'MED TGT':>7} | {'STATUS':<15}"
    print(hdr)
    print("-" * 115)

    for cat_name, cat in cats.items():
        n = cat.get("sample_n", 0)
        if n < 5:
            print(f"{cat_name:<19} | {'—':<7} | {n:>3} | {'—':>5} | {'—':>8} | {'—':>12} | {'—':>7} | {'—':>7} | {'—':>6} | {'—':>11} | {'—':>7} | {cat.get('edge_status', 'UNPROVEN')[:15]:<15}")
            continue

        for cell_key in ["s15_t20", "s15_t40"]:
            cell = cat.get(cell_key, {})
            c_label = f"s{cell.get('stop', 15):.0f}/t{cell.get('target', 20):.0f}"
            w_pct = f"{cell.get('win_pct', 0.0):.1f}%"
            r_exp = f"{cell.get('real_exp', 0.0):>+6.1f} pt"
            r_null = f"{cell.get('rand_mean', 0.0):>+5.1f}±{cell.get('rand_sd', 0.0):<3.1f}"
            e_z = f"{cell.get('edge_z', 0.0):>+5.1f} sd"
            d_z = f"{cell.get('dir_z', 0.0):>+5.1f} sd"
            d5 = f"{cell.get('drop5', 0.0):>+5.1f}"
            oos = f"{cell.get('oos_h1_exp', 0.0):>+4.1f}/{cell.get('oos_h2_exp', 0.0):>+4.1f}"
            med_t = f"{cell.get('median_time_target_min', 0):>4.0f}m" if cell.get('median_time_target_min') is not None else "   — "
            status = "VOICE ARMED" if cell.get("voice_armed") else "SILENT (Z<2sd)"

            print(f"{cat_name:<19} | {c_label:<7} | {n:>3} | {w_pct:>5} | {r_exp:>8} | {r_null:>12} | {e_z:>7} | {d_z:>7} | {d5:>6} | {oos:>11} | {med_t:>7} | {status:<15}")

    print("=" * 115)
    print("\n" + "=" * 90)
    print(" 📋 PER-EVENT TABLES & TIME-TO-RESOLUTION (Forward walk begins strictly after candle close)")
    print("=" * 90)

    for cat_name, rows in ev_tables.items():
        if not rows:
            continue
        print(f"\n--- {cat_name} ({len(rows)} events) ---")
        ev_hdr = f"{'DATE':<11} | {'TIME':<5} | {'DIR':<5} | {'ENTRY':>8} | {'MFE':>6} | {'MAE':>6} | {'OUTCOME AT 15/20':<18} | {'OUTCOME AT 15/40':<18}"
        print(ev_hdr)
        print("-" * 90)
        for r in rows[:15]:  # show up to 15 per category in terminal
            print(f"{r['date']:<11} | {r['time']:<5} | {r['dir']:<5} | {r['entry']:>8.1f} | {r['mfe']:>6.1f} | {r['mae']:>6.1f} | {r['outcome_20']:<18} | {r['outcome_40']:<18}")
        if len(rows) > 15:
            print(f" ... and {len(rows) - 15} more events (all tracked in API payload per_event_tables).")

    print("\n" + "=" * 90)
    print(f" Current Spot: {res['spot']} | 1H 8-EMA: {res['ema8_1h']['value']} ({res['ema8_1h']['slope']}, {res['ema8_1h']['position']})")
    print(f" Active HTF Levels: {len(res['active_levels'])} | Live Alerts: {len(res['live_alerts'])}")
    for al in res["live_alerts"]:
        print(f" 🔔 {al['title']}: {al['body']}")
    print("=" * 90)


if __name__ == "__main__":
    print_backtest_table()
