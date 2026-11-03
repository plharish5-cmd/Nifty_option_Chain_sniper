"""
engine/gann_pivots_gaps.py — True Historical Monotonic Swing Pivots (R1-R4, S1-S4),
Unfilled Daily Opening Gaps (Upper & Lower), Gann Square of 9 + Multi-Horizon Time Cycles,
and Auto-Daily Master CSV & Excel Store.

Features:
1. Canonical 5-Year Master CSV Store (nifty50_5yr_master_daily.csv) auto-appends every new trading day.
2. Auto-regeneration of NIFTY50_All_In_One_Master_View.xlsx whenever new daily bars arrive.
3. Monotonic Resistances (R1..R4) and Supports (S1..S4) walking backwards across daily bars.
4. Unfilled Opening Gaps (> 5.0 pt threshold) tracked row by row and removed when filled.
5. Gann Square of 9 levels: (sqrt(C) + (degrees / 180.0)) ** 2 for +/- 45, 90, 135, 180 degrees.
6. Multi-Horizon Gann Time Cycles (90-Day Intermediate, 1-Year Swing, 5-Year Global)
   with Active Hit vibrations (within +/- 2 calendar days) and Next-Cycle Countdown.
   alert_text NEVER shows "None".
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .. import config, db
from ..timeutil import now_ist, now_epoch, IST

log = logging.getLogger("gann_pivots_gaps")

# Canonical 5-Year Data Paths inside 8600 app
CANONICAL_MASTER_CSV = config.DATA_DIR / "nifty50_5yr_master_daily.csv"
EXCEL_MASTER_VIEW = config.DATA_DIR / "NIFTY50_All_In_One_Master_View.xlsx"

# Institutional Gann Time Cycles across lookback horizons
GANN_TIME_CYCLES_1YR = [30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360]
GANN_TIME_CYCLES_90D = [7, 14, 21, 30, 45, 60, 72, 90]
GANN_TIME_CYCLES_5YR = [
    30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360,
    540, 720, 900, 1080, 1260, 1440, 1620, 1800, 1980, 2160, 2340, 2520, 2700, 2880, 3240, 3600
]
HARMONIC_CYCLES = [360, 540, 720, 900, 1080, 1260, 1440, 1620, 1800, 1980, 2160, 2340, 2520, 2700, 2880]

# For backward compatibility
GANN_TIME_CYCLES = GANN_TIME_CYCLES_1YR
DEFAULT_TIME_CYCLES = GANN_TIME_CYCLES_1YR
DEFAULT_DEGREES = [45, 90, 135, 180]
DEFAULT_GAP_PTS = 5.0

# In-memory store of the last full recalculation audit metadata
_LAST_RECALC_AUDIT: Dict[str, Any] = {
    "total_historical_days_scanned": 0,
    "first_historical_date": "",
    "latest_settled_date": "",
    "post_1540_settled": False,
    "last_full_recalc_ist": "",
}
_SCHEDULER_RUNNING = False
_LAST_EOD_SETTLED_DATE: Optional[str] = None


def is_post_1540_settled(
    target_date_str: Optional[str] = None,
    mode: str = "auto",
    current_ist: Optional[datetime] = None,
    target_time_str: Optional[str] = None,
) -> bool:
    """
    Determines whether trading day T is completed and settled (>= 15:40 IST).
    - If mode == 'next_day': True (advance ref_idx to day T for next session levels)
    - If mode == 'intraday': False (keep ref_idx at T-1 for intraday monitoring)
    - If mode == 'auto':
      - If target_time_str is provided: return target_time_str >= '15:40'
      - If current_ist is provided: return current_ist.strftime("%H:%M") >= '15:40'
      - If target_date_str is None or matches today's IST date:
          - If weekend (Saturday/Sunday): return True (prior trading day is settled)
          - Otherwise: return now_ist().strftime("%H:%M") >= '15:40'
      - Else (historical target_date_str without time/datetime): return False (default to intraday).
    """
    if mode == "next_day":
        return True
    if mode == "intraday":
        return False

    if target_time_str:
        return target_time_str >= "15:40"

    if current_ist is not None:
        return current_ist.strftime("%H:%M") >= "15:40"

    now = now_ist()
    today_iso = now.strftime("%Y-%m-%d")
    if target_date_str is None or target_date_str == today_iso:
        if now.weekday() >= 5:
            return True
        return now.strftime("%H:%M") >= "15:40"

    return False


def parse_date_to_iso(d_raw: Any) -> Optional[str]:
    """Converts diverse date formats (YYYY-MM-DD, DD-MMM-YYYY, DD MMM YYYY, etc.) to ISO YYYY-MM-DD."""
    d_clean = str(d_raw or "").strip().replace('"', '')
    if not d_clean:
        return None
    if len(d_clean) >= 10 and d_clean[4] == "-" and d_clean[7] == "-":
        return d_clean[:10]
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(d_clean, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("gann_pivots_gaps", {})


def gann_sq9(price: Optional[float], degrees: float) -> Optional[float]:
    """
    Calculates the Gann Square of 9 value based on a starting price and degrees of rotation.
    Formula: (sqrt(price) + (degrees / 180.0)) ** 2
    """
    if price is None or price <= 0 or math.isnan(price):
        return None
    val = math.sqrt(price) + (degrees / 180.0)
    return round(val ** 2, 2) if val > 0 else 0.0


def evaluate_cycle(
    days: int,
    cycle_list: List[int],
    pivot_type: str,
    pivot_date: str,
    pivot_price: float,
) -> Dict[str, Any]:
    """
    Evaluates Gann cycle vibration for a swing pivot (Top or Btm):
    1. Active Hit: if days is within +/- 2 calendar days of any cycle in cycle_list.
    2. Next-Cycle Countdown: if between cycles or approaching next cycle.
    NEVER returns empty or 'None' text.
    """
    fmt_date = ""
    try:
        fmt_date = datetime.strptime(pivot_date, "%Y-%m-%d").strftime("%d-%b")
    except Exception:
        fmt_date = str(pivot_date)

    active_hit = None
    approaching = None
    matched_cycle = None

    for c in cycle_list:
        diff = days - c
        if abs(diff) <= 2:
            matched_cycle = c
            if diff == 0:
                active_hit = f"🔥 ACTIVE: {pivot_type}+{c}D"
                detail = f"🔥 ACTIVE: {pivot_type}+{c}D (from {fmt_date} @ {pivot_price:,.2f})"
            elif diff < 0:
                rem = -diff
                approaching = f"🔥 {c}D Cycle in {rem}d!"
                active_hit = f"🔥 ACTIVE: {pivot_type}+{c}D in {rem}d"
                detail = f"{pivot_type}+{days}D (🔥 {c}D Cycle in {rem}d! from {fmt_date} @ {pivot_price:,.2f})"
            else:
                active_hit = f"🔥 ACTIVE: {pivot_type}+{c}D (+{diff}d)"
                detail = f"🔥 ACTIVE: {pivot_type}+{c}D (+{diff}d from {fmt_date} @ {pivot_price:,.2f})"
            break

    # Determine next cycle and countdown
    next_c = None
    for c in cycle_list:
        if c > days:
            next_c = c
            break

    if next_c is None:
        next_c = ((days // 180) + 1) * 180
    days_to_next = next_c - days

    if approaching:
        summary_text = f"{pivot_type}+{days}D ({approaching})"
        badge_text = approaching
    elif active_hit:
        summary_text = detail
        badge_text = active_hit
    else:
        summary_text = f"{pivot_type}+{days}D (Next: {next_c}D in {days_to_next}d)"
        badge_text = f"{pivot_type}+{days}D (Next: {next_c}D in {days_to_next}d)"

    return {
        "days": days,
        "pivot_date": pivot_date,
        "pivot_price": pivot_price,
        "pivot_type": pivot_type,
        "is_active": bool(active_hit or approaching),
        "matched_cycle": matched_cycle,
        "active_text": active_hit or approaching,
        "next_cycle": next_c,
        "days_to_next": days_to_next,
        "badge_text": badge_text,
        "summary_text": summary_text,
    }


def compute_master_levels_from_daily(
    daily_bars: List[Dict[str, Any]],
    current_spot: Optional[float] = None,
    target_date_str: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
    mode: str = "auto",
    current_ist: Optional[datetime] = None,
    target_time_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Expects daily_bars sorted chronologically:
    [{'date': 'YYYY-MM-DD', 'open': float, 'high': float, 'low': float, 'close': float}, ...]

    Calculates:
    - R1..R4 (strictly higher highs) & S1..S4 (strictly lower lows) walking backward.
    - Upper_Gap_1..3 & Lower_Gap_1..3 (> 5.0 pt threshold, tracking unfilled gaps).
    - Gann Square of 9 (+/- 45, 90, 135, 180 degrees).
    - Multi-Horizon Gann Time Cycles (90-Day, 1-Year, 5-Year) with countdowns (NEVER 'None').
    """
    c = cfg or get_cfg()
    gap_thresh = float(c.get("gap_threshold_pts", DEFAULT_GAP_PTS))
    degrees_list = c.get("gann_degrees", DEFAULT_DEGREES)

    if not daily_bars:
        return {"ok": False, "error": "No daily bars available"}

    # Sort chronologically & deduplicate by date
    seen: Dict[str, Dict[str, Any]] = {}
    for b in daily_bars:
        d_raw = b.get("date") or b.get("d") or b.get("Date") or ""
        d_key = parse_date_to_iso(d_raw)
        if d_key:
            o_val = b.get("open") if b.get("open") is not None else b.get("o") if b.get("o") is not None else b.get("Open")
            h_val = b.get("high") if b.get("high") is not None else b.get("h") if b.get("h") is not None else b.get("High")
            l_val = b.get("low") if b.get("low") is not None else b.get("l") if b.get("l") is not None else b.get("Low")
            c_val = b.get("close") if b.get("close") is not None else b.get("c") if b.get("c") is not None else b.get("Close")
            if None not in (o_val, h_val, l_val, c_val):
                seen[d_key] = {
                    "date": d_key,
                    "open": float(o_val),
                    "high": float(h_val),
                    "low": float(l_val),
                    "close": float(c_val),
                }

    df = [seen[k] for k in sorted(seen.keys())]
    if not df:
        return {"ok": False, "error": "Empty daily bars"}

    # Determine reference completed daily bar index `ref_idx`:
    today_str = target_date_str or (current_ist or now_ist()).strftime("%Y-%m-%d")
    is_settled = is_post_1540_settled(
        target_date_str=target_date_str,
        mode=mode,
        current_ist=current_ist,
        target_time_str=target_time_str,
    )

    if len(df) >= 2 and df[-1]["date"] == today_str:
        if is_settled:
            ref_idx = len(df) - 1
            today_bar = df[-1]
        else:
            ref_idx = len(df) - 2
            today_bar = df[-1]
    else:
        ref_idx = len(df) - 1
        today_bar = df[-1]

    # Walk row by row up to ref_idx to track unfilled historical gaps accurately
    open_gaps: List[Dict[str, Any]] = []
    for i in range(ref_idx + 1):
        row = df[i]
        O, H, L, C = row["open"], row["high"], row["low"], row["close"]

        # 1. Check which previous gaps were filled on day `i`
        still_open = []
        for gap in open_gaps:
            filled = False
            if gap["type"] == "GAP_UP" and L <= gap["price"]:
                filled = True
            elif gap["type"] == "GAP_DOWN" and H >= gap["price"]:
                filled = True
            if not filled:
                still_open.append(gap)
        open_gaps = still_open

        # 2. Create new gap based on day `i` open vs day `i-1` close
        if i > 0:
            prev_C = df[i - 1]["close"]
            if O - prev_C > gap_thresh:
                open_gaps.append({"type": "GAP_UP", "price": round(prev_C, 2), "date": row["date"]})
            elif O - prev_C < -gap_thresh:
                open_gaps.append({"type": "GAP_DOWN", "price": round(prev_C, 2), "date": row["date"]})

    # Also include today's opening gap if today is a new day after ref_idx
    today_gap_points = 0.0
    if len(df) >= 2 and ref_idx == len(df) - 2:
        prev_C = df[ref_idx]["close"]
        today_O = today_bar["open"]
        today_gap_points = round(today_O - prev_C, 2)
        if today_O - prev_C > gap_thresh:
            open_gaps.append({"type": "GAP_UP", "price": round(prev_C, 2), "date": today_bar["date"]})
        elif today_O - prev_C < -gap_thresh:
            open_gaps.append({"type": "GAP_DOWN", "price": round(prev_C, 2), "date": today_bar["date"]})
    elif len(df) >= 2 and ref_idx == len(df) - 1:
        today_gap_points = round(df[-1]["open"] - df[-2]["close"], 2)

    ref_row = df[ref_idx]
    ref_O, ref_H, ref_L, ref_C = ref_row["open"], ref_row["high"], ref_row["low"], ref_row["close"]

    # Monotonic Historical Resistances (R1, R2, R3, R4) from previous days
    r_lvls = [{"level": "R1", "price": round(ref_H, 2), "date": ref_row["date"]}]
    for j in range(ref_idx - 1, -1, -1):
        if df[j]["high"] > r_lvls[-1]["price"]:
            r_lvls.append({
                "level": f"R{len(r_lvls) + 1}",
                "price": round(df[j]["high"], 2),
                "date": df[j]["date"],
            })
        if len(r_lvls) == 4:
            break

    # Monotonic Historical Supports (S1, S2, S3, S4) from previous days
    s_lvls = [{"level": "S1", "price": round(ref_L, 2), "date": ref_row["date"]}]
    for j in range(ref_idx - 1, -1, -1):
        if df[j]["low"] < s_lvls[-1]["price"]:
            s_lvls.append({
                "level": f"S{len(s_lvls) + 1}",
                "price": round(df[j]["low"], 2),
                "date": df[j]["date"],
            })
        if len(s_lvls) == 4:
            break

    # Sort active unfilled gaps relative to reference close (or current_spot)
    anchor_price = float(current_spot or ref_C)
    upper_gaps_raw = sorted([g for g in open_gaps if g["price"] > anchor_price], key=lambda x: x["price"])[:3]
    lower_gaps_raw = sorted([g for g in open_gaps if g["price"] < anchor_price], key=lambda x: x["price"], reverse=True)[:3]

    upper_gaps = [
        {"level": f"Upper_Gap_{k+1}", "price": g["price"], "date": g["date"], "type": g["type"]}
        for k, g in enumerate(upper_gaps_raw)
    ]
    lower_gaps = [
        {"level": f"Lower_Gap_{k+1}", "price": g["price"], "date": g["date"], "type": g["type"]}
        for k, g in enumerate(lower_gaps_raw)
    ]

    # Gann Square of 9 Resistances (+45, +90, +135, +180) & Supports (-45, -90, -135, -180)
    gann_r = [
        {"level": f"Gann_R{idx+1}({deg}°)", "degrees": deg, "price": gann_sq9(ref_C, deg)}
        for idx, deg in enumerate(degrees_list)
    ]
    gann_s = [
        {"level": f"Gann_S{idx+1}({deg}°)", "degrees": -deg, "price": gann_sq9(ref_C, -deg)}
        for idx, deg in enumerate(degrees_list)
    ]

    # ── PDH-anchored ladder (the one that was actually measured) ──────────────────────────────────
    # The ladder above anchors on the previous CLOSE. scripts/backtest_gann.py scored all three
    # anchors over 33 NIFTY sessions and 471 turning points, each against a random baseline of equal
    # density, and they are not equivalent:
    #
    #   anchor   edge over random (±10 pts)   survives excluding best 3 sessions
    #   PDC      +2.6 pp  (1.4 sd)            not tested at that strength
    #   PDH      +5.5 pp  (2.6 sd)            yes, +3.3 pp
    #   PDL      +1.9 pp  (1.0 sd)            no
    #
    # PDH also held in both chronological halves (+4.3 then +5.4). There is a plain mechanical
    # reading for that: the previous day's HIGH is a level many traders mark by hand, so levels
    # derived from it may inherit that attention. Which would make it real without the spiral
    # having anything to do with it.
    #
    # It is published alongside the close-anchored set rather than replacing it: the existing
    # overlay keeps working untouched, and the frontend draws this one behind its own toggle.
    # BANKNIFTY passed the split but FAILED the concentration test, so this is CONTEXT, not a
    # signal, and it is off by default.
    gann_pdh_r = [
        {"level": f"PDH_R{idx+1}({deg}°)", "degrees": deg, "price": gann_sq9(ref_H, deg)}
        for idx, deg in enumerate(degrees_list)
    ]
    gann_pdh_s = [
        {"level": f"PDH_S{idx+1}({deg}°)", "degrees": -deg, "price": gann_sq9(ref_H, -deg)}
        for idx, deg in enumerate(degrees_list)
    ]

    # --- UPGRADED MULTI-HORIZON GANN TIME CYCLES ---
    curr_dt = datetime.strptime(today_bar["date"], "%Y-%m-%d").date()

    # 1. 5-Year Global Major High & Low
    global_high_bar = max(df, key=lambda x: x["high"])
    global_low_bar = min(df, key=lambda x: x["low"])
    global_high_date = datetime.strptime(global_high_bar["date"], "%Y-%m-%d").date()
    global_low_date = datetime.strptime(global_low_bar["date"], "%Y-%m-%d").date()
    days_from_high_global = (curr_dt - global_high_date).days
    days_from_low_global = (curr_dt - global_low_date).days

    # 2. 1-Year (365 Days) Lookback
    bars_1yr = [b for b in df if (curr_dt - datetime.strptime(b["date"], "%Y-%m-%d").date()).days <= 365]
    if not bars_1yr:
        bars_1yr = df
    high_1yr_bar = max(bars_1yr, key=lambda x: x["high"])
    low_1yr_bar = min(bars_1yr, key=lambda x: x["low"])
    days_from_high_1yr = (curr_dt - datetime.strptime(high_1yr_bar["date"], "%Y-%m-%d").date()).days
    days_from_low_1yr = (curr_dt - datetime.strptime(low_1yr_bar["date"], "%Y-%m-%d").date()).days

    # 3. 90-Day Intermediate Lookback
    bars_90d = [b for b in df if (curr_dt - datetime.strptime(b["date"], "%Y-%m-%d").date()).days <= 90]
    if not bars_90d:
        bars_90d = df
    high_90d_bar = max(bars_90d, key=lambda x: x["high"])
    low_90d_bar = min(bars_90d, key=lambda x: x["low"])
    days_from_high_90d = (curr_dt - datetime.strptime(high_90d_bar["date"], "%Y-%m-%d").date()).days
    days_from_low_90d = (curr_dt - datetime.strptime(low_90d_bar["date"], "%Y-%m-%d").date()).days

    # Cycle vibration evaluations across all 3 horizons
    eval_global_h = evaluate_cycle(days_from_high_global, GANN_TIME_CYCLES_5YR, "Top", global_high_bar["date"], global_high_bar["high"])
    eval_global_l = evaluate_cycle(days_from_low_global, GANN_TIME_CYCLES_5YR, "Btm", global_low_bar["date"], global_low_bar["low"])

    eval_1yr_h = evaluate_cycle(days_from_high_1yr, GANN_TIME_CYCLES_1YR, "Top", high_1yr_bar["date"], high_1yr_bar["high"])
    eval_1yr_l = evaluate_cycle(days_from_low_1yr, GANN_TIME_CYCLES_1YR, "Btm", low_1yr_bar["date"], low_1yr_bar["low"])

    eval_90d_h = evaluate_cycle(days_from_high_90d, GANN_TIME_CYCLES_90D, "Top", high_90d_bar["date"], high_90d_bar["high"])
    eval_90d_l = evaluate_cycle(days_from_low_90d, GANN_TIME_CYCLES_90D, "Btm", low_90d_bar["date"], low_90d_bar["low"])

    # Collect active alerts (+/- 2 calendar days)
    cycle_alerts: List[str] = []
    seen_details: set = set()
    for ev in [eval_1yr_h, eval_1yr_l, eval_90d_h, eval_90d_l, eval_global_h, eval_global_l]:
        if ev["is_active"] and ev["summary_text"] not in seen_details:
            cycle_alerts.append(ev["summary_text"])
            seen_details.add(ev["summary_text"])

    # Determine primary major high/low bar for backward compatibility
    # When 1-year high matches global high, or dataset is small, align with 1-Year/Global
    if len(df) <= 10 or days_from_high_global <= 365:
        primary_high_bar = global_high_bar
        days_from_major_high = days_from_high_global
    else:
        primary_high_bar = high_1yr_bar
        days_from_major_high = days_from_high_1yr

    if len(df) <= 10 or days_from_low_global <= 365:
        primary_low_bar = global_low_bar
        days_from_major_low = days_from_low_global
    else:
        primary_low_bar = low_1yr_bar
        days_from_major_low = days_from_low_1yr

    # Build concise, non-empty alert_text:
    # If active hits exist, display active hits prominently.
    # Otherwise, display live cycle position and next cycle countdown.
    if cycle_alerts:
        active_badges = [ev["badge_text"] for ev in [eval_1yr_h, eval_1yr_l, eval_90d_h, eval_90d_l] if ev["is_active"]]
        if not active_badges:
            active_badges = [ev["badge_text"] for ev in [eval_global_h, eval_global_l] if ev["is_active"]]
        alert_text = " · ".join(active_badges)
        # Append 1-year countdown for context if not already active
        if not eval_1yr_h["is_active"]:
            alert_text += f" · Top+{days_from_high_1yr}D (Next: {eval_1yr_h['next_cycle']}D in {eval_1yr_h['days_to_next']}d)"
        if not eval_1yr_l["is_active"] and not any("Btm" in b for b in active_badges):
            alert_text += f" · Btm+{days_from_low_1yr}D (Next: {eval_1yr_l['next_cycle']}D in {eval_1yr_l['days_to_next']}d)"
    else:
        # Clean next cycle countdown — NEVER 'None'
        alert_text = (
            f"Top+{days_from_high_1yr}D (Next: {eval_1yr_h['next_cycle']}D in {eval_1yr_h['days_to_next']}d) · "
            f"Btm+{days_from_low_1yr}D (Next: {eval_1yr_l['next_cycle']}D in {eval_1yr_l['days_to_next']}d)"
        )

    # Evaluate whether today's intraday price hit any of the projected levels
    if ref_idx < len(df) - 1:
        # Intraday active candle
        eval_high = today_bar["high"]
        eval_low = today_bar["low"]
        if current_spot is not None:
            eval_high = max(eval_high, current_spot)
            eval_low = min(eval_low, current_spot)
    else:
        # Settled reference bar; awaiting next trading day. Only spot touches trigger hits.
        if current_spot is not None:
            eval_high = current_spot
            eval_low = current_spot
        else:
            eval_high = None
            eval_low = None

    for r in r_lvls:
        r["hit"] = bool(eval_high is not None and eval_high >= r["price"])

    for s in s_lvls:
        s["hit"] = bool(eval_low is not None and eval_low <= s["price"])

    for ug in upper_gaps:
        ug["hit"] = bool(eval_high is not None and eval_high >= ug["price"])

    for lg in lower_gaps:
        lg["hit"] = bool(eval_low is not None and eval_low <= lg["price"])

    for gr in gann_r:
        gr["hit"] = bool(gr["price"] is not None and eval_high is not None and eval_high >= gr["price"])

    for gs in gann_s:
        gs["hit"] = bool(gs["price"] is not None and eval_low is not None and eval_low <= gs["price"])

    now_ist_str = (current_ist or now_ist()).strftime("%Y-%m-%d %H:%M:%S IST")
    audit = {
        "total_historical_days_scanned": len(df),
        "first_historical_date": df[0]["date"],
        "latest_settled_date": ref_row["date"],
        "post_1540_settled": bool(ref_idx == len(df) - 1),
        "last_full_recalc_ist": now_ist_str,
    }
    global _LAST_RECALC_AUDIT
    _LAST_RECALC_AUDIT = audit

    return {
        "ok": True,
        "ref_date": ref_row["date"],
        "ref_close": round(ref_C, 2),
        "today_date": today_bar["date"],
        "gap_points": today_gap_points,
        "today_high": round(eval_high, 2) if eval_high is not None else round(today_bar["high"], 2),
        "today_low": round(eval_low, 2) if eval_low is not None else round(today_bar["low"], 2),
        "current_spot": round(current_spot, 2) if current_spot is not None else None,
        "recalc_audit": audit,
        "resistances": r_lvls,       # R1, R2, R3, R4 (Red)
        "supports": s_lvls,          # S1, S2, S3, S4 (Green)
        "upper_gaps": upper_gaps,    # Upper_Gap_1..3 (Amber/Purple)
        "lower_gaps": lower_gaps,    # Lower_Gap_1..3 (Amber/Purple)
        "gann_resistances": gann_r,  # Gann_R1..R4 (45°, 90°, 135°, 180°) — anchored on prev CLOSE
        "gann_supports": gann_s,     # Gann_S1..S4 (-45°, -90°, -135°, -180°)
        # PDH-anchored ladder, plus the anchor itself so the UI can state what it was built from.
        "gann_pdh_resistances": gann_pdh_r,
        "gann_pdh_supports": gann_pdh_s,
        "gann_pdh_anchor": round(float(ref_H), 2) if ref_H else None,
        "time_cycles": {
            "major_high_date": primary_high_bar["date"],
            "major_high_price": round(primary_high_bar["high"], 2),
            "days_from_major_high": days_from_major_high,
            "major_low_date": primary_low_bar["date"],
            "major_low_price": round(primary_low_bar["low"], 2),
            "days_from_major_low": days_from_major_low,
            "active_alerts": cycle_alerts,
            "alert_text": alert_text,
            "has_active_hit": bool(len(cycle_alerts) > 0),
            "horizons": {
                "intermediate_90d": {
                    "high": eval_90d_h,
                    "low": eval_90d_l,
                },
                "major_1yr": {
                    "high": eval_1yr_h,
                    "low": eval_1yr_l,
                },
                "global_5yr": {
                    "high": eval_global_h,
                    "low": eval_global_l,
                    "days_from_global_high": days_from_high_global,
                    "days_from_global_low": days_from_low_global,
                    "global_high_date": global_high_bar["date"],
                    "global_high_price": round(global_high_bar["high"], 2),
                    "global_low_date": global_low_bar["date"],
                    "global_low_price": round(global_low_bar["low"], 2),
                },
            },
        },
    }


def generate_excel_master_view(input_csv: Path | str, output_excel: Path | str) -> bool:
    """
    Generates the complete NIFTY50_All_In_One_Master_View.xlsx workbook
    matching the user's master Excel view specification:
    - 31 standardized columns
    - Row-by-row Monotonic R1-R4, S1-S4, Upper/Lower Gaps, Gann Sq9, and Time Cycles
    - Color conditional fills (Green hit resistances, Red hit supports, Yellow vibration alerts)
    - Freezes header row at B2 and formats styling
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError:
        log.warning("openpyxl not installed; skipping Excel generation")
        return False

    p_in = Path(input_csv)
    if not p_in.exists():
        log.warning("Input CSV %s does not exist for Excel generation", input_csv)
        return False

    try:
        import pandas as pd
        df = pd.read_csv(p_in)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").drop_duplicates(subset=["Date"]).reset_index(drop=True)
        dates = df["Date"].dt.strftime("%d-%b-%Y").to_numpy()
        dates_raw = df["Date"].to_numpy()
        opens = df["Open"].to_numpy(dtype=float)
        highs = df["High"].to_numpy(dtype=float)
        lows = df["Low"].to_numpy(dtype=float)
        closes = df["Close"].to_numpy(dtype=float)
        N = len(df)

        major_high_date = df.loc[df["High"].idxmax(), "Date"]
        major_low_date = df.loc[df["Low"].idxmin(), "Date"]

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "NIFTY Ultimate Master View"
        headers = [
            "Date", "Open", "High", "Low", "Close", "Gap_Points",
            "R1", "R2", "R3", "R4",
            "S1", "S2", "S3", "S4",
            "Upper_Gap_1", "Upper_Gap_2", "Upper_Gap_3",
            "Lower_Gap_1", "Lower_Gap_2", "Lower_Gap_3",
            "Gann_R1(45°)", "Gann_R2(90°)", "Gann_R3(135°)", "Gann_R4(180°)",
            "Gann_S1(45°)", "Gann_S2(90°)", "Gann_S3(135°)", "Gann_S4(180°)",
            "Days_from_Major_High", "Days_from_Major_Low", "Time_Cycle_Alert"
        ]
        ws.append(headers)

        hit_green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        hit_green_font = Font(color="006100", bold=True)
        hit_red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        hit_red_font = Font(color="9C0006", bold=True)
        alert_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        alert_font = Font(color="9C5700", bold=True)

        open_gaps_excel: List[Dict[str, Any]] = []
        for i in range(N):
            O, H, L, C = opens[i], highs[i], lows[i], closes[i]
            curr_date = pd.Timestamp(dates_raw[i])
            gap_points = round(O - closes[i - 1], 2) if i > 0 else None

            # Resistances
            r_lvls = [H]
            for j in range(i - 1, -1, -1):
                if highs[j] > r_lvls[-1]:
                    r_lvls.append(highs[j])
                if len(r_lvls) == 4:
                    break
            R = [round(r_lvls[k], 2) if k < len(r_lvls) else None for k in range(4)]

            # Supports
            s_lvls = [L]
            for j in range(i - 1, -1, -1):
                if lows[j] < s_lvls[-1]:
                    s_lvls.append(lows[j])
                if len(s_lvls) == 4:
                    break
            S = [round(s_lvls[k], 2) if k < len(s_lvls) else None for k in range(4)]

            # Gaps
            still_open = []
            for gap in open_gaps_excel:
                if gap["type"] == "GAP_UP" and L <= gap["price"]:
                    continue
                if gap["type"] == "GAP_DOWN" and H >= gap["price"]:
                    continue
                still_open.append(gap)
            open_gaps_excel = still_open

            if i > 0:
                prev_C = closes[i - 1]
                if O - prev_C > 5:
                    open_gaps_excel.append({"type": "GAP_UP", "price": prev_C})
                elif O - prev_C < -5:
                    open_gaps_excel.append({"type": "GAP_DOWN", "price": prev_C})

            upper_m = sorted([g["price"] for g in open_gaps_excel if g["price"] > C])
            lower_m = sorted([g["price"] for g in open_gaps_excel if g["price"] < C], reverse=True)
            U = [round(upper_m[k], 2) if k < len(upper_m) else None for k in range(3)]
            Low_G = [round(lower_m[k], 2) if k < len(lower_m) else None for k in range(3)]

            g_r = [gann_sq9(C, 45), gann_sq9(C, 90), gann_sq9(C, 135), gann_sq9(C, 180)]
            g_s = [gann_sq9(C, -45), gann_sq9(C, -90), gann_sq9(C, -135), gann_sq9(C, -180)]

            days_high = (curr_date - major_high_date).days
            days_low = (curr_date - major_low_date).days

            alert_msg = ""
            if days_high > 0:
                for c_day in GANN_TIME_CYCLES_1YR:
                    if abs(days_high - c_day) <= 2:
                        alert_msg += f"Top+{c_day}D "
            if days_low > 0:
                for c_day in GANN_TIME_CYCLES_1YR:
                    if abs(days_low - c_day) <= 2:
                        alert_msg += f"Btm+{c_day}D "
            if not alert_msg:
                next_h = next((c for c in GANN_TIME_CYCLES_1YR if c > days_high), None) if days_high >= 0 else None
                next_l = next((c for c in GANN_TIME_CYCLES_1YR if c > days_low), None) if days_low >= 0 else None
                h_str = f"Top+{days_high}D" + (f" (Next:{next_h}D)" if next_h else "") if days_high >= 0 else ""
                l_str = f"Btm+{days_low}D" + (f" (Next:{next_l}D)" if next_l else "") if days_low >= 0 else ""
                alert_msg = f"{h_str} · {l_str}".strip(" · ") or "Top/Btm Initializing"

            row_data = [
                dates[i], O, H, L, C, gap_points,
                R[0], R[1], R[2], R[3],
                S[0], S[1], S[2], S[3],
                U[0], U[1], U[2],
                Low_G[0], Low_G[1], Low_G[2],
                g_r[0], g_r[1], g_r[2], g_r[3],
                g_s[0], g_s[1], g_s[2], g_s[3],
                days_high if days_high >= 0 else None,
                days_low if days_low >= 0 else None,
                alert_msg.strip()
            ]
            ws.append(row_data)

            # Apply hit formatting based on next day's price action
            curr_row_idx = ws.max_row
            if i < N - 1:
                next_H = highs[i + 1]
                next_L = lows[i + 1]
                # Resistances (Cols 7..10)
                for col_idx in range(7, 11):
                    val = row_data[col_idx - 1]
                    if val is not None and next_H >= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_green_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_green_font
                # Supports (Cols 11..14)
                for col_idx in range(11, 15):
                    val = row_data[col_idx - 1]
                    if val is not None and next_L <= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_red_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_red_font
                # Upper Gaps (Cols 15..17)
                for col_idx in range(15, 18):
                    val = row_data[col_idx - 1]
                    if val is not None and next_H >= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_green_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_green_font
                # Lower Gaps (Cols 18..20)
                for col_idx in range(18, 21):
                    val = row_data[col_idx - 1]
                    if val is not None and next_L <= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_red_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_red_font
                # Gann R (Cols 21..24)
                for col_idx in range(21, 25):
                    val = row_data[col_idx - 1]
                    if val is not None and next_H >= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_green_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_green_font
                # Gann S (Cols 25..28)
                for col_idx in range(25, 29):
                    val = row_data[col_idx - 1]
                    if val is not None and next_L <= val:
                        ws.cell(row=curr_row_idx, column=col_idx).fill = hit_red_fill
                        ws.cell(row=curr_row_idx, column=col_idx).font = hit_red_font

            if "Top+" in alert_msg or "Btm+" in alert_msg:
                ws.cell(row=curr_row_idx, column=31).fill = alert_fill
                ws.cell(row=curr_row_idx, column=31).font = alert_font

        # Top Header Formatting
        header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)
        for col_idx, col in enumerate(ws.columns, 1):
            letter = col[0].column_letter
            ws.column_dimensions[letter].width = 24 if letter == "AE" else 14
            ws.cell(row=1, column=col_idx).fill = header_fill
            ws.cell(row=1, column=col_idx).font = header_font

        ws.freeze_panes = "B2"
        p_out = Path(output_excel)
        p_out.parent.mkdir(parents=True, exist_ok=True)
        wb.save(p_out)
        log.info("Regenerated Excel Master View at %s (%d rows)", p_out, N)
        return True
    except Exception as e:
        log.error("Failed to generate Excel Master View: %s", e)
        return False


def sync_and_append_daily_master_csv(
    sec_id: str = "13",
    today_bar: Optional[Dict[str, Any]] = None,
    force_rewrite: bool = False,
    generate_excel: bool = False,
) -> Dict[str, Any]:
    """
    Checks the canonical 5-year master CSV on disk:
    ict_smc_app/backend/data/nifty50_5yr_master_daily.csv.
    Aggregates any newer completed or active trading sessions from:
    1. SQLite daily table (ict_smc.sqlite3)
    2. SQLite candles_1m table (aggregated into daily OHLC)
    3. Other CSV stores / live market data
    Appends missing completed trading days directly to nifty50_5yr_master_daily.csv.
    Auto-regenerates NIFTY50_All_In_One_Master_View.xlsx whenever new rows are appended.
    """
    repo_root = config.BACKEND_DIR.parent.parent
    csv_path = CANONICAL_MASTER_CSV
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: Dict[str, Dict[str, Any]] = {}

    # Read existing master CSV if available
    if csv_path.exists():
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    r_clean = {k.strip().replace('"', ''): v for k, v in r.items() if k}
                    d_iso = parse_date_to_iso(r_clean.get("Date") or r_clean.get("date"))
                    if d_iso:
                        try:
                            existing_rows[d_iso] = {
                                "Date": d_iso,
                                "Open": round(float(str(r_clean["Open"]).replace(",", "")), 2),
                                "High": round(float(str(r_clean["High"]).replace(",", "")), 2),
                                "Low": round(float(str(r_clean["Low"]).replace(",", "")), 2),
                                "Close": round(float(str(r_clean["Close"]).replace(",", "")), 2),
                            }
                        except (ValueError, KeyError):
                            pass
        except Exception as e:
            log.warning("Could not read existing master CSV %s: %s", csv_path, e)

    # If CSV doesn't exist or is empty, seed from external historical CSVs
    if not existing_rows:
        possible_seed_csvs = [
            repo_root / "data" / "ohlc" / "OHLC.csv",
            Path("data/ohlc/OHLC.csv"),
            Path("/home/harish/Documents/Swathi K N/1 Trading/AI/historical data ohlc 5 years/OHLC.csv"),
            Path("/home/harish/Documents/Swathi K N/1 Trading/option chain/NIFTY50/NIFTY 50_Historical_PR_07102025to06102026.csv"),
            Path("/home/harish/Documents/Swathi K N/1 Trading/PRICE_ACTION/NIFTY 50_Historical_PR_10092025to09092026.csv"),
            repo_root / "data" / "ohlc" / "NIFTY 50_Historical_PR_19082025to18082026.csv",
        ]
        for y in range(2019, 2027):
            possible_seed_csvs.append(repo_root / "data" / "ohlc" / f"{y}.csv")
            possible_seed_csvs.append(Path(f"/home/harish/Documents/Swathi K N/1 Trading/AI/historical data ohlc 5 years/{y}.csv"))

        for p in possible_seed_csvs:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        for r in csv.DictReader(f):
                            r_clean = {k.strip().replace('"', ''): v for k, v in r.items() if k}
                            d_iso = parse_date_to_iso(r_clean.get("Date") or r_clean.get("date"))
                            if d_iso and d_iso not in existing_rows:
                                try:
                                    existing_rows[d_iso] = {
                                        "Date": d_iso,
                                        "Open": round(float(str(r_clean["Open"]).replace(",", "")), 2),
                                        "High": round(float(str(r_clean["High"]).replace(",", "")), 2),
                                        "Low": round(float(str(r_clean["Low"]).replace(",", "")), 2),
                                        "Close": round(float(str(r_clean["Close"]).replace(",", "")), 2),
                                    }
                                except (ValueError, KeyError):
                                    pass
                except Exception as e:
                    log.warning("Seed read failed on %s: %s", p, e)

    max_date = max(existing_rows.keys()) if existing_rows else "2019-01-01"
    new_rows_added = 0

    # 1. Check SQLite daily table for newer or updated days
    try:
        daily_rows = db.daily(sec_id, limit=500)
        for r in daily_rows:
            d_iso = parse_date_to_iso(r.get("d"))
            if d_iso:
                rec = {
                    "Date": d_iso,
                    "Open": round(float(r["o"]), 2),
                    "High": round(float(r["h"]), 2),
                    "Low": round(float(r["l"]), 2),
                    "Close": round(float(r["c"]), 2),
                }
                if d_iso not in existing_rows:
                    existing_rows[d_iso] = rec
                    new_rows_added += 1
                elif d_iso >= max_date:
                    curr = existing_rows[d_iso]
                    if any(curr[k] != rec[k] for k in ("Open", "High", "Low", "Close")):
                        existing_rows[d_iso] = rec
                        new_rows_added += 1
    except Exception as e:
        log.warning("Could not sync daily table: %s", e)

    # 2. Check SQLite candles_1m for completed/active daily sessions
    try:
        # Only query recent candles (from max_date minus 1 day onwards) instead of full 92,000+ candle history
        since_ts = 0
        if max_date:
            try:
                dt_max = datetime.strptime(max_date, "%Y-%m-%d")
                since_ts = int(dt_max.timestamp()) - 19800 - 86400
            except Exception:
                since_ts = 0

        candles_rows = db.query(
            "SELECT ts, o, h, l, c FROM candles_1m WHERE sec_id = ? AND ts >= ? ORDER BY ts ASC",
            (sec_id, since_ts)
        )
        candles_by_date: Dict[str, Dict[str, float]] = {}
        for c_row in candles_rows:
            ts = c_row["ts"]
            # IST offset is +19800 seconds (+5:30)
            dt_ist = datetime.utcfromtimestamp(ts + 19800)
            d_str = dt_ist.strftime("%Y-%m-%d")
            if d_str not in candles_by_date:
                candles_by_date[d_str] = {
                    "Date": d_str,
                    "Open": round(float(c_row["o"]), 2),
                    "High": round(float(c_row["h"]), 2),
                    "Low": round(float(c_row["l"]), 2),
                    "Close": round(float(c_row["c"]), 2),
                }
            else:
                rec = candles_by_date[d_str]
                rec["High"] = max(rec["High"], round(float(c_row["h"]), 2))
                rec["Low"] = min(rec["Low"], round(float(c_row["l"]), 2))
                rec["Close"] = round(float(c_row["c"]), 2)

        for d_str, rec in candles_by_date.items():
            if d_str not in existing_rows:
                existing_rows[d_str] = rec
                new_rows_added += 1
            elif d_str >= max_date:
                curr = existing_rows[d_str]
                if any(curr[k] != rec[k] for k in ("Open", "High", "Low", "Close")):
                    existing_rows[d_str] = rec
                    new_rows_added += 1
    except Exception as e:
        log.warning("Could not aggregate candles_1m: %s", e)

    # 3. Check explicit today_bar if supplied
    if today_bar:
        d_iso = parse_date_to_iso(today_bar.get("date") or today_bar.get("Date"))
        if d_iso:
            rec = {
                "Date": d_iso,
                "Open": round(float(today_bar.get("open", today_bar.get("Open", 0.0))), 2),
                "High": round(float(today_bar.get("high", today_bar.get("High", 0.0))), 2),
                "Low": round(float(today_bar.get("low", today_bar.get("Low", 0.0))), 2),
                "Close": round(float(today_bar.get("close", today_bar.get("Close", 0.0))), 2),
            }
            if d_iso not in existing_rows:
                existing_rows[d_iso] = rec
                new_rows_added += 1
            else:
                curr = existing_rows[d_iso]
                if any(curr[k] != rec[k] for k in ("Open", "High", "Low", "Close")):
                    existing_rows[d_iso] = rec
                    new_rows_added += 1

    # Write back sorted, deduplicated rows if new rows arrived or if CSV missing or forced
    if new_rows_added > 0 or force_rewrite or not csv_path.exists():
        sorted_dates = sorted(existing_rows.keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Date", "Open", "High", "Low", "Close"])
            writer.writeheader()
            for d_k in sorted_dates:
                writer.writerow(existing_rows[d_k])
        log.info("Saved %d total daily bars to %s (+%d new/updated)", len(sorted_dates), csv_path, new_rows_added)
        if generate_excel or not EXCEL_MASTER_VIEW.exists():
            generate_excel_master_view(csv_path, EXCEL_MASTER_VIEW)
    elif not EXCEL_MASTER_VIEW.exists():
        generate_excel_master_view(csv_path, EXCEL_MASTER_VIEW)

    return {
        "ok": True,
        "csv_path": str(csv_path),
        "excel_path": str(EXCEL_MASTER_VIEW),
        "total_bars": len(existing_rows),
        "new_appended": new_rows_added,
        "latest_date": max(existing_rows.keys()) if existing_rows else None,
    }


def load_historical_daily_bars(sec_id: str = "13", sync: bool = True) -> List[Dict[str, Any]]:
    """
    Returns a unified, chronologically sorted list of daily bars.
    Prioritizes canonical master CSV (auto-synced and appended).
    """
    # Trigger auto-sync and append
    if sync:
        sync_and_append_daily_master_csv(sec_id=sec_id)

    bars: List[Dict[str, Any]] = []

    # Read from canonical master CSV
    if CANONICAL_MASTER_CSV.exists():
        try:
            with open(CANONICAL_MASTER_CSV, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    r_clean = {k.strip().replace('"', ''): v for k, v in r.items() if k}
                    d_iso = parse_date_to_iso(r_clean.get("Date") or r_clean.get("date"))
                    if d_iso:
                        try:
                            bars.append({
                                "date": d_iso,
                                "open": float(str(r_clean["Open"]).replace(",", "")),
                                "high": float(str(r_clean["High"]).replace(",", "")),
                                "low": float(str(r_clean["Low"]).replace(",", "")),
                                "close": float(str(r_clean["Close"]).replace(",", "")),
                            })
                        except (ValueError, KeyError):
                            pass
        except Exception as e:
            log.warning("Error reading canonical CSV %s: %s", CANONICAL_MASTER_CSV, e)

    # Fallback to SQLite daily if canonical CSV is empty
    if not bars:
        try:
            daily_db = db.daily(sec_id, limit=5000)
            for r in daily_db:
                bars.append({
                    "date": str(r.get("d")),
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                })
        except Exception as e:
            log.warning("Error loading daily rows from db: %s", e)

    return bars


_GANN_CACHE: Dict[Tuple[str, float, Optional[str], str], Tuple[float, Dict[str, Any]]] = {}

def get_gann_pivots_gaps(
    sec_id: str = "13",
    current_spot: Optional[float] = None,
    target_date_str: Optional[str] = None,
    mode: str = "auto",
    current_ist: Optional[datetime] = None,
    target_time_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for GET /api/desk/gann-pivots-gaps.
    Loads deep 5-year daily history, auto-syncs new daily bars, and computes master levels.
    Cached for 10 seconds to protect against tight polling loops.
    """
    cache_key = (sec_id, round(current_spot or 0.0, 0), target_date_str, mode)
    now_t = time.time()
    cached = _GANN_CACHE.get(cache_key)
    if cached and (now_t - cached[0] < 10.0):
        return cached[1]

    daily_bars = load_historical_daily_bars(sec_id=sec_id)
    res = compute_master_levels_from_daily(
        daily_bars=daily_bars,
        current_spot=current_spot,
        target_date_str=target_date_str,
        mode=mode,
        current_ist=current_ist,
        target_time_str=target_time_str,
    )
    _GANN_CACHE[cache_key] = (now_t, res)
    return res


def force_recalculate(
    sec_id: str = "13",
    current_spot: Optional[float] = None,
    today_bar: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Forces a 100% full-history sync and recalculation:
    1. Pulls settled daily OHLC for today from Dhan/SQLite (or intraday candles).
    2. Appends/updates nifty50_5yr_master_daily.csv.
    3. Regenerates NIFTY50_All_In_One_Master_View.xlsx.
    4. Recalculates all levels across all rows from index 0 to len(df)-1.
    """
    sync_res = sync_and_append_daily_master_csv(sec_id=sec_id, today_bar=today_bar, force_rewrite=True, generate_excel=True)
    _GANN_CACHE.clear()
    daily_bars = load_historical_daily_bars(sec_id=sec_id, sync=False)
    levels = compute_master_levels_from_daily(
        daily_bars=daily_bars,
        current_spot=current_spot,
        mode="auto",
    )
    return {
        "ok": True,
        "message": "Full multi-year dataset recalculation complete",
        "sync": sync_res,
        "recalc_audit": levels.get("recalc_audit"),
        "levels": levels,
    }


async def run_eod_recalc_scheduler(poll_interval_s: float = 20.0) -> None:
    """
    Background asyncio loop that runs continuously.
    Monitors IST clock and detects when 15:40 IST is crossed on trading days (Monday - Friday).
    Finalizes today's daily OHLC, appends/updates nifty50_5yr_master_daily.csv,
    and runs full-dataset recalculation across all historical bars (0..N-1) and regenerates Master Excel.
    """
    global _SCHEDULER_RUNNING, _LAST_EOD_SETTLED_DATE
    if _SCHEDULER_RUNNING:
        log.warning("Gann EOD scheduler is already running")
        return
    _SCHEDULER_RUNNING = True
    log.info("Started Gann Pivots & Gaps 15:40 IST EOD background scheduler (interval: %.1fs)", poll_interval_s)
    try:
        while True:
            await asyncio.sleep(poll_interval_s)
            n = now_ist()
            day = n.date().isoformat()
            hhmm = n.strftime("%H:%M")
            weekday = n.weekday()  # 0=Monday, 4=Friday

            # Crosses 15:40 IST on a trading day (Mon-Fri)
            if weekday < 5 and hhmm >= "15:40" and _LAST_EOD_SETTLED_DATE != day:
                log.info("15:40 IST EOD settlement triggered for %s (time: %s IST)", day, hhmm)
                try:
                    res = await asyncio.to_thread(force_recalculate, sec_id="13")
                    _LAST_EOD_SETTLED_DATE = day
                    log.info("15:40 IST EOD full recalculation completed for %s: %s", day, res.get("recalc_audit"))
                except Exception as e:
                    log.error("Failed during 15:40 IST EOD full recalculation: %s", e)
    except asyncio.CancelledError:
        log.info("Gann Pivots & Gaps 15:40 IST EOD background scheduler stopped")
    except Exception as e:
        log.error("Unhandled error in Gann EOD scheduler: %s", e)
    finally:
        _SCHEDULER_RUNNING = False
