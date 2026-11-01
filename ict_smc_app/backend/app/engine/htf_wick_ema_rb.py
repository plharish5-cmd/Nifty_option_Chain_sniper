"""
engine/htf_wick_ema_rb.py — Institutional 1H / 4H Wick & Shaved Extremes + 1H 8-EMA
First-Touch Bounce/Rejection Engine & Quantitative Backtester.

Features:
1. 1H & 4H Upper Extremes:
   - Standard Upper Wick: [max(O, C), H] when wick >= wick_min_pts (3.0 pts).
   - Shaved-Top (Zero-Wick / shaved candle): High with ±4.0 pt institutional body-extreme band.
2. 1H & 4H Lower Extremes:
   - Standard Lower Wick: [L, min(O, C)] when wick >= wick_min_pts (3.0 pts).
   - Shaved-Bottom (Zero-Wick / shaved candle): Low with ±4.0 pt institutional body-extreme band.
3. 1H 8-EMA Dynamic Line:
   - Value, slope (RISING / FALLING / FLAT), price position, and first-touch test detection.
4. First-Touch Virgin Level Tracking:
   - Measures ONLY the first untouched test of each level.
   - Evaluates 20-40 pt MFE within 30-60 minutes before adverse stop (15.0 pts).
5. Voice Alert Synthesis:
   - High-fidelity plain-English voice alert texts for web speech synthesis.
6. Quantitative Backtest Summary:
   - Win rates (>=20 pts, >=40 pts), sample sizes, and average reaction points.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .. import config, db
from ..market.candles import resample
from ..timeutil import chart_time, now_epoch, to_ist

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


def evaluate_level_touches_and_backtest(
    levels: List[HtfLevel],
    intraday_bars: List[Dict[str, float]],
    thresholds: Dict[str, float],
    candle_duration_s: int,
) -> Dict[str, Any]:
    """
    Evaluates historical touches on levels using subsequent intraday bars.
    Measures first-touch 20pt / 40pt MFE reaction within 60 minutes.
    """
    prox = thresholds["proximity_pts"]
    target_20 = thresholds["target_20_pts"]
    target_40 = thresholds["target_40_pts"]
    adverse_stop = thresholds["adverse_stop_pts"]
    window_sec = thresholds["mfe_window_minutes"] * 60

    stats = {
        "total_levels": len(levels),
        "first_touches": 0,
        "target_20_wins": 0,
        "target_40_wins": 0,
        "stopped_out": 0,
        "mfe_list": [],
    }

    # Sort intraday bars chronologically
    intraday_bars = sorted(intraday_bars, key=lambda x: x["ts"])
    bar_map = {int(b["ts"]): b for b in intraday_bars}
    all_ts = [int(b["ts"]) for b in intraday_bars]

    for lvl in levels:
        level_close_ts = lvl.created_ts + candle_duration_s
        # Subsequent bars strictly after candle closed
        subsequent = [b for b in intraday_bars if int(b["ts"]) >= level_close_ts]
        if not subsequent:
            continue

        touches = 0
        prev_bar_price: Optional[float] = None

        for idx, bar in enumerate(subsequent):
            b_high = float(bar["h"])
            b_low = float(bar["l"])
            b_close = float(bar["c"])
            b_ts = int(bar["ts"])

            is_touch = False
            touch_price = 0.0
            direction = ""

            if lvl.side == "RESISTANCE":
                # Resistance rejection test: price approaching from below
                # Prior bar close was below level bottom
                if prev_bar_price is not None and prev_bar_price <= lvl.bottom + prox:
                    if b_high >= lvl.bottom - prox:
                        is_touch = True
                        touch_price = max(b_low, lvl.bottom)
                        direction = "FROM_BELOW_RESISTANCE"
            elif lvl.side == "SUPPORT":
                # Support bounce test: price approaching from above
                # Prior bar close was above level top
                if prev_bar_price is not None and prev_bar_price >= lvl.top - prox:
                    if b_low <= lvl.top + prox:
                        is_touch = True
                        touch_price = min(b_high, lvl.top)
                        direction = "FROM_ABOVE_SUPPORT"

            prev_bar_price = b_close

            if is_touch:
                touches += 1
                if touches == 1:
                    # Record FIRST TOUCH details
                    lvl.touch_count = 1
                    lvl.is_virgin = False
                    lvl.first_touch_ts = b_ts
                    lvl.first_touch_price = touch_price
                    lvl.direction_of_approach = direction

                    # Forward window evaluation (next 30-60 mins)
                    fwd_bars = [b for b in subsequent[idx + 1:] if int(b["ts"]) <= b_ts + window_sec]
                    if fwd_bars:
                        if lvl.side == "SUPPORT":
                            # Measure upward bounce
                            mfe = max(float(x["h"]) for x in fwd_bars) - touch_price
                            mae = touch_price - min(float(x["l"]) for x in fwd_bars)
                        else:
                            # Measure downward rejection
                            mfe = touch_price - min(float(x["l"]) for x in fwd_bars)
                            mae = max(float(x["h"]) for x in fwd_bars) - touch_price

                        lvl.mfe_bounce_pts = round(mfe, 2)
                        stats["first_touches"] += 1
                        stats["mfe_list"].append(mfe)

                        if mfe >= target_20:
                            lvl.hit_20 = True
                            stats["target_20_wins"] += 1
                        if mfe >= target_40:
                            lvl.hit_40 = True
                            stats["target_40_wins"] += 1
                        if mae >= adverse_stop and mfe < target_20:
                            stats["stopped_out"] += 1

                elif touches >= 2:
                    lvl.touch_count = touches
                    # Once exhausted, break out of search
                    break

        if touches == 0:
            lvl.touch_count = 0
            lvl.is_virgin = True

    first_t = stats["first_touches"]
    stats["win_rate_20_pct"] = round((stats["target_20_wins"] / max(1, first_t)) * 100.0, 2) if first_t > 0 else 0.0
    stats["win_rate_40_pct"] = round((stats["target_40_wins"] / max(1, first_t)) * 100.0, 2) if first_t > 0 else 0.0
    stats["avg_mfe_pts"] = round(sum(stats["mfe_list"]) / max(1, len(stats["mfe_list"])), 2) if stats["mfe_list"] else 0.0

    return stats


def evaluate_1h_8ema_first_touches(
    h1_candles: List[Dict[str, float]],
    intraday_bars: List[Dict[str, float]],
    thresholds: Dict[str, float],
) -> Dict[str, Any]:
    """
    Evaluates 1H 8-EMA first-touch bounce / rejection performance.
    """
    prox = thresholds["proximity_pts"]
    target_20 = thresholds["target_20_pts"]
    target_40 = thresholds["target_40_pts"]
    adverse_stop = thresholds["adverse_stop_pts"]
    window_sec = thresholds["mfe_window_minutes"] * 60

    stats = {
        "first_touches": 0,
        "target_20_wins": 0,
        "target_40_wins": 0,
        "stopped_out": 0,
        "mfe_list": [],
    }

    if len(h1_candles) < 9:
        stats["win_rate_20_pct"] = 0.0
        stats["win_rate_40_pct"] = 0.0
        stats["avg_mfe_pts"] = 0.0
        return stats

    k = 2.0 / (8 + 1)
    ema_series: List[Tuple[int, float]] = []
    ema: Optional[float] = None

    for c in h1_candles:
        cl = float(c["c"])
        if ema is None:
            ema = cl
        else:
            ema = ema + k * (cl - ema)
        ema_series.append((int(c["ts"]), ema))

    # For each 1H candle after seed period
    for i in range(8, len(ema_series) - 1):
        c_ts, c_ema = ema_series[i]
        next_c_ts = ema_series[i + 1][0]

        # Intraday bars during this 1H window
        window_bars = [b for b in intraday_bars if c_ts <= int(b["ts"]) < next_c_ts]
        if not window_bars:
            continue

        # Look for first touch of c_ema
        first_bar = window_bars[0]
        state = "ABOVE" if float(first_bar["c"]) > c_ema + prox else "BELOW" if float(first_bar["c"]) < c_ema - prox else "AT"

        for idx, bar in enumerate(window_bars):
            bh = float(bar["h"])
            bl = float(bar["l"])
            b_ts = int(bar["ts"])

            if state == "ABOVE" and bl <= c_ema + prox:
                # First touch from above (Support Test)
                stats["first_touches"] += 1
                entry = c_ema
                fwd = [b for b in intraday_bars if b_ts < int(b["ts"]) <= b_ts + window_sec]
                if fwd:
                    mfe = max(float(x["h"]) for x in fwd) - entry
                    mae = entry - min(float(x["l"]) for x in fwd)
                    stats["mfe_list"].append(mfe)
                    if mfe >= target_20:
                        stats["target_20_wins"] += 1
                    if mfe >= target_40:
                        stats["target_40_wins"] += 1
                    if mae >= adverse_stop and mfe < target_20:
                        stats["stopped_out"] += 1
                break  # Evaluate first touch only per 1H bar
            elif state == "BELOW" and bh >= c_ema - prox:
                # First touch from below (Resistance Test)
                stats["first_touches"] += 1
                entry = c_ema
                fwd = [b for b in intraday_bars if b_ts < int(b["ts"]) <= b_ts + window_sec]
                if fwd:
                    mfe = entry - min(float(x["l"]) for x in fwd)
                    mae = max(float(x["h"]) for x in fwd) - entry
                    stats["mfe_list"].append(mfe)
                    if mfe >= target_20:
                        stats["target_20_wins"] += 1
                    if mfe >= target_40:
                        stats["target_40_wins"] += 1
                    if mae >= adverse_stop and mfe < target_20:
                        stats["stopped_out"] += 1
                break  # Evaluate first touch only per 1H bar

    first_t = stats["first_touches"]
    stats["win_rate_20_pct"] = round((stats["target_20_wins"] / max(1, first_t)) * 100.0, 2) if first_t > 0 else 0.0
    stats["win_rate_40_pct"] = round((stats["target_40_wins"] / max(1, first_t)) * 100.0, 2) if first_t > 0 else 0.0
    stats["avg_mfe_pts"] = round(sum(stats["mfe_list"]) / max(1, len(stats["mfe_list"])), 2) if stats["mfe_list"] else 0.0

    return stats


def generate_live_alerts_and_voice(
    active_levels: List[HtfLevel],
    ema8_val: Optional[float],
    current_spot: float,
    thresholds: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Generates live proximity alerts with exact spoken English voice synthesis texts.
    """
    prox = thresholds["proximity_pts"]
    alerts: List[Dict[str, Any]] = []

    # 1. Check virgin & first-touch HTF levels
    for lvl in active_levels:
        if lvl.touch_count > 1:
            continue  # Exhausted

        dist = abs(current_spot - lvl.ref_price)
        if dist <= prox:
            price_fmt = f"{lvl.ref_price:,.1f}"
            tf_spoken = "one hour" if lvl.tf == "1H" else "four hour"

            if lvl.side == "RESISTANCE":
                if lvl.is_shaved:
                    voice_text = (
                        f"Alert: NIFTY is approaching the {tf_spoken} highest open-close level at {price_fmt} "
                        "from below for the first time. Watch for resistance rejection."
                    )
                else:
                    voice_text = (
                        f"Alert: NIFTY is approaching the {tf_spoken} upper wick resistance at {price_fmt} "
                        "from below for the first time. Watch for a 20 to 40 point rejection downward."
                    )
                alerts.append({
                    "id": f"alert-{lvl.id}",
                    "level_id": lvl.id,
                    "kind": lvl.kind,
                    "side": "SELL",
                    "tf": lvl.tf,
                    "price": lvl.ref_price,
                    "distance_pts": round(dist, 2),
                    "title": f"🚨 {lvl.tf} {('Shaved High' if lvl.is_shaved else 'Upper Wick')} Rejection Zone ({price_fmt})",
                    "body": f"First touch test of {lvl.label}. Watch for 20-40 pt rejection downward.",
                    "voice_text": voice_text,
                })

            elif lvl.side == "SUPPORT":
                if lvl.is_shaved:
                    voice_text = (
                        f"Alert: NIFTY is coming near the {tf_spoken} lowest open-close price at {price_fmt} "
                        "from above for the first time. Watch for support bounce."
                    )
                else:
                    voice_text = (
                        f"Alert: NIFTY is coming near the {tf_spoken} lower wick support at {price_fmt} "
                        "from above for the first time. Watch for a 20 to 40 point bounce upward."
                    )
                alerts.append({
                    "id": f"alert-{lvl.id}",
                    "level_id": lvl.id,
                    "kind": lvl.kind,
                    "side": "BUY",
                    "tf": lvl.tf,
                    "price": lvl.ref_price,
                    "distance_pts": round(dist, 2),
                    "title": f"🟢 {lvl.tf} {('Shaved Low' if lvl.is_shaved else 'Lower Wick')} Support Zone ({price_fmt})",
                    "body": f"First touch test of {lvl.label}. Watch for 20-40 pt bounce upward.",
                    "voice_text": voice_text,
                })

    # 2. Check 1H 8-EMA
    if ema8_val is not None:
        ema_dist = abs(current_spot - ema8_val)
        if ema_dist <= prox:
            price_fmt = f"{ema8_val:,.1f}"
            if current_spot >= ema8_val:
                voice_text = (
                    f"Alert: NIFTY is above the one hour 8 EMA and pulling back to test 8 EMA support at {price_fmt}. "
                    "Watch for bullish bounce continuation."
                )
                alerts.append({
                    "id": f"alert-ema8-support-{int(ema8_val)}",
                    "level_id": "1H-EMA8",
                    "kind": "1H_EMA8_SUPPORT",
                    "side": "BUY",
                    "tf": "1H",
                    "price": round(ema8_val, 2),
                    "distance_pts": round(ema_dist, 2),
                    "title": f"⚡ 1H 8-EMA Support Test ({price_fmt})",
                    "body": f"NIFTY pulling back to test 1H 8 EMA ({price_fmt}) from above. Watch for bullish bounce continuation.",
                    "voice_text": voice_text,
                })
            else:
                voice_text = (
                    f"Alert: NIFTY is coming from below the one hour 8 EMA and touching 8 EMA resistance at {price_fmt}. "
                    "Watch for bearish rejection."
                )
                alerts.append({
                    "id": f"alert-ema8-resistance-{int(ema8_val)}",
                    "level_id": "1H-EMA8",
                    "kind": "1H_EMA8_RESISTANCE",
                    "side": "SELL",
                    "tf": "1H",
                    "price": round(ema8_val, 2),
                    "distance_pts": round(ema_dist, 2),
                    "title": f"⚡ 1H 8-EMA Resistance Test ({price_fmt})",
                    "body": f"NIFTY rallying to test 1H 8 EMA ({price_fmt}) from below. Watch for bearish rejection.",
                    "voice_text": voice_text,
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
        # Update live alerts if spot was passed
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
    m5 = resample(raw_1m, "5m")

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

    # Run backtest / touch evaluations
    stats_1h = evaluate_level_touches_and_backtest(levels_1h, m5, thresh, candle_duration_s=3600)
    stats_4h = evaluate_level_touches_and_backtest(levels_4h, m5, thresh, candle_duration_s=14400)
    stats_ema8 = evaluate_1h_8ema_first_touches(h1, m5, thresh)

    # Filter active levels nearby spot (within 400 pts, limit max_active_levels)
    for lvl in all_levels:
        lvl.distance_pts = round(abs(spot - lvl.ref_price), 2)
        if lvl.touch_count == 0:
            lvl.label = f"{lvl.label} (Virgin)"
        elif lvl.touch_count == 1:
            lvl.label = f"{lvl.label} (1st Touch Active)"
        else:
            lvl.label = f"{lvl.label} (Exhausted)"

    # Prioritize virgin and first-touch levels closest to spot
    active_candidates = [l for l in all_levels if l.touch_count <= 1 and (l.distance_pts is not None and l.distance_pts <= 450.0)]
    active_candidates.sort(key=lambda x: (x.touch_count, x.distance_pts if x.distance_pts is not None else 9999))
    max_active = int(thresh["max_active_levels"])
    active_levels = active_candidates[:max_active]

    # Generate live alerts
    live_alerts = generate_live_alerts_and_voice(active_levels, ema8_val, spot, thresh)

    # Dates
    first_date = to_ist(raw_1m[0]["ts"]).strftime("%Y-%m-%d")
    latest_date = to_ist(raw_1m[-1]["ts"]).strftime("%Y-%m-%d")
    latest_ist = to_ist(raw_1m[-1]["ts"]).strftime("%Y-%m-%d %H:%M:%S IST")

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
        "backtest_summary": {
            "dataset_range": f"{first_date} to {latest_date}",
            "total_intraday_candles": len(raw_1m),
            "total_1h_candles": len(h1),
            "total_4h_candles": len(h4),
            "wick_1h": stats_1h,
            "wick_4h": stats_4h,
            "ema8_1h": stats_ema8,
        },
    }

    _cached_payload = res
    _cached_time = now_t
    _cached_last_ts = latest_bar_ts

    return res


def print_backtest_table() -> None:
    """
    CLI command to print the formatted Backtest Summary Table in the terminal.
    """
    res = get_htf_wick_ema_rb(sec_id="13")
    if not res.get("ok"):
        print(f"Error: {res.get('error')}")
        return

    bt = res["backtest_summary"]
    w1 = bt["wick_1h"]
    w4 = bt["wick_4h"]
    we = bt["ema8_1h"]

    print("=" * 88)
    print(" 🎯 REJECTION BLOCKS (RB) HTF WICK & 1H 8-EMA QUANTITATIVE BACKTEST SUMMARY")
    print(f" Dataset: {bt['dataset_range']} | Intraday: {bt['total_intraday_candles']} bars | 1H: {bt['total_1h_candles']} | 4H: {bt['total_4h_candles']}")
    print("=" * 88)
    header = f"{'SETUP / LEVEL TYPE':<26} | {'TOUCHES':<8} | {'WIN >=20pt':<11} | {'WIN >=40pt':<11} | {'AVG MFE PTS':<12} | {'STOPPED':<8}"
    print(header)
    print("-" * 88)
    print(f"{'1H Wick & Shaved Extr':<26} | {w1['first_touches']:<8} | {w1['win_rate_20_pct']:>5.1f}% ({w1['target_20_wins']}) | {w1['win_rate_40_pct']:>5.1f}% ({w1['target_40_wins']}) | {w1['avg_mfe_pts']:>8.1f} pts | {w1['stopped_out']:<8}")
    print(f"{'4H Wick & Shaved Extr':<26} | {w4['first_touches']:<8} | {w4['win_rate_20_pct']:>5.1f}% ({w4['target_20_wins']}) | {w4['win_rate_40_pct']:>5.1f}% ({w4['target_40_wins']}) | {w4['avg_mfe_pts']:>8.1f} pts | {w4['stopped_out']:<8}")
    print(f"{'1H 8-EMA Dynamic Line':<26} | {we['first_touches']:<8} | {we['win_rate_20_pct']:>5.1f}% ({we['target_20_wins']}) | {we['win_rate_40_pct']:>5.1f}% ({we['target_40_wins']}) | {we['avg_mfe_pts']:>8.1f} pts | {we['stopped_out']:<8}")
    print("=" * 88)
    print(f" Current Spot: {res['spot']} | 1H 8-EMA: {res['ema8_1h']['value']} ({res['ema8_1h']['slope']}, {res['ema8_1h']['position']})")
    print(f" Active HTF Levels: {len(res['active_levels'])} | Live Voice Alerts: {len(res['live_alerts'])}")
    if res['live_alerts']:
        for al in res['live_alerts']:
            print(f" 🔔 {al['title']}: \"{al['voice_text']}\"")
    print("=" * 88)


if __name__ == "__main__":
    print_backtest_table()
