"""
engine/gann_pivots_gaps.py — True Historical Monotonic Swing Pivots (R1-R4, S1-S4),
Unfilled Daily Opening Gaps (Upper & Lower), and Gann Square of 9 + Time Cycles.

Exact port of user's master algorithm:
1. Monotonic Resistances (R1..R4) and Supports (S1..S4) walking backwards across daily bars.
2. Unfilled Opening Gaps (> 5.0 pt threshold) tracked row by row and removed when filled.
3. Gann Square of 9 levels: (sqrt(C) + (degrees / 180.0)) ** 2 for +/- 45, 90, 135, 180 degrees.
4. Gann Time Cycles from Major High & Major Low with alert badge triggers within +/- 1 day.
"""
from __future__ import annotations

import csv
import logging
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config, db

log = logging.getLogger("gann_pivots_gaps")

DEFAULT_TIME_CYCLES = [30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360]
GANN_TIME_CYCLES = DEFAULT_TIME_CYCLES
DEFAULT_DEGREES = [45, 90, 135, 180]
DEFAULT_GAP_PTS = 5.0


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


def gann_sq9(price: float, degrees: float) -> Optional[float]:
    """
    Calculates the Gann Square of 9 value based on a starting price and degrees of rotation.
    Formula: (sqrt(price) + (degrees / 180.0)) ** 2
    """
    if price is None or price <= 0 or math.isnan(price):
        return None
    val = math.sqrt(price) + (degrees / 180.0)
    return round(val ** 2, 2) if val > 0 else 0.0


def compute_master_levels_from_daily(
    daily_bars: List[Dict[str, Any]],
    current_spot: Optional[float] = None,
    target_date_str: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Expects daily_bars sorted chronologically:
    [{'date': 'YYYY-MM-DD', 'open': float, 'high': float, 'low': float, 'close': float}, ...]

    Important: For live intraday trading on day `T`, the projected levels (R1-R4, S1-S4,
    Gann R1-R4, Gann S1-S4, and unfilled Upper/Lower Gaps) are computed from the completed
    daily bars up through `T-1` (yesterday's close/high/low), AND we also check today's
    open (`T`) for any newly created morning gap (`O_today - C_yesterday`) and check whether
    today's intraday High/Low has hit or filled each level!
    """
    c = cfg or get_cfg()
    gap_thresh = float(c.get("gap_threshold_pts", DEFAULT_GAP_PTS))
    time_cycles = c.get("time_cycles", DEFAULT_TIME_CYCLES)
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

    # Global major high and low dates across history for Gann Time Cycles
    major_high_bar = max(df, key=lambda x: x["high"])
    major_low_bar = min(df, key=lambda x: x["low"])
    major_high_date = datetime.strptime(major_high_bar["date"], "%Y-%m-%d").date()
    major_low_date = datetime.strptime(major_low_bar["date"], "%Y-%m-%d").date()

    # Determine reference completed daily bar index `ref_idx`:
    # If the last bar in `df` is today's active session and we have >= 2 bars,
    # `ref_idx = len(df) - 2` (yesterday's completed daily bar) is used to project R1-R4, S1-S4, and Gann Sq9
    # so levels stay anchored to previous days' highs/lows/close during today's session!
    today_str = target_date_str or datetime.now().strftime("%Y-%m-%d")
    if len(df) >= 2 and df[-1]["date"] == today_str:
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

    # Gann Time Cycles from Major High & Major Low
    curr_dt = datetime.strptime(today_bar["date"], "%Y-%m-%d").date()
    days_from_high = (curr_dt - major_high_date).days
    days_from_low = (curr_dt - major_low_date).days

    cycle_alerts = []
    if days_from_high > 0:
        for c_day in time_cycles:
            if abs(days_from_high - c_day) <= 1:
                cycle_alerts.append(f"Top+{c_day}D (from {major_high_bar['date']} @ {major_high_bar['high']:.2f})")
    if days_from_low > 0:
        for c_day in time_cycles:
            if abs(days_from_low - c_day) <= 1:
                cycle_alerts.append(f"Btm+{c_day}D (from {major_low_bar['date']} @ {major_low_bar['low']:.2f})")

    # Evaluate whether today's intraday price hit any of the projected levels
    eval_high = today_bar["high"]
    eval_low = today_bar["low"]
    if current_spot is not None:
        eval_high = max(eval_high, current_spot)
        eval_low = min(eval_low, current_spot)

    for r in r_lvls:
        r["hit"] = bool(eval_high >= r["price"])

    for s in s_lvls:
        s["hit"] = bool(eval_low <= s["price"])

    for ug in upper_gaps:
        ug["hit"] = bool(eval_high >= ug["price"])

    for lg in lower_gaps:
        lg["hit"] = bool(eval_low <= lg["price"])

    for gr in gann_r:
        if gr["price"] is not None:
            gr["hit"] = bool(eval_high >= gr["price"])
        else:
            gr["hit"] = False

    for gs in gann_s:
        if gs["price"] is not None:
            gs["hit"] = bool(eval_low <= gs["price"])
        else:
            gs["hit"] = False

    return {
        "ok": True,
        "ref_date": ref_row["date"],
        "ref_close": round(ref_C, 2),
        "today_date": today_bar["date"],
        "gap_points": today_gap_points,
        "today_high": round(eval_high, 2),
        "today_low": round(eval_low, 2),
        "current_spot": round(current_spot, 2) if current_spot is not None else None,
        "resistances": r_lvls,       # R1, R2, R3, R4 (Red)
        "supports": s_lvls,          # S1, S2, S3, S4 (Green)
        "upper_gaps": upper_gaps,    # Upper_Gap_1..3 (Amber/Purple)
        "lower_gaps": lower_gaps,    # Lower_Gap_1..3 (Amber/Purple)
        "gann_resistances": gann_r,  # Gann_R1..R4 (45°, 90°, 135°, 180°)
        "gann_supports": gann_s,     # Gann_S1..S4 (-45°, -90°, -135°, -180°)
        "time_cycles": {
            "major_high_date": major_high_bar["date"],
            "major_high_price": round(major_high_bar["high"], 2),
            "days_from_major_high": days_from_high,
            "major_low_date": major_low_bar["date"],
            "major_low_price": round(major_low_bar["low"], 2),
            "days_from_major_low": days_from_low,
            "active_alerts": cycle_alerts,
            "alert_text": " · ".join(cycle_alerts) if cycle_alerts else "None",
        },
    }


def load_historical_daily_bars(sec_id: str = "13") -> List[Dict[str, Any]]:
    """
    Combines daily bars from:
    1. Historical CSV files (e.g. data/ohlc/OHLC.csv, data/ohlc/*.csv)
    2. SQLite store `daily` table
    3. Today's intraday 1m candles if forming/available in SQLite
    Returns a unified, chronologically sorted list of daily bars.
    """
    bars: List[Dict[str, Any]] = []

    # 1. Search common CSV paths
    repo_root = config.BACKEND_DIR.parent.parent
    possible_csvs = [
        repo_root / "data" / "ohlc" / "OHLC.csv",
        Path("data/ohlc/OHLC.csv"),
        repo_root / "data" / "ohlc" / "NIFTY 50_Historical_PR_19082025to18082026.csv",
        Path("data/ohlc/NIFTY 50_Historical_PR_19082025to18082026.csv"),
    ]

    for p in possible_csvs:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        # Clean key names
                        r_clean = {k.strip().replace('"', ''): v for k, v in r.items() if k}
                        d_val = r_clean.get("Date") or r_clean.get("date")
                        o_val = r_clean.get("Open") or r_clean.get("open")
                        h_val = r_clean.get("High") or r_clean.get("high")
                        l_val = r_clean.get("Low") or r_clean.get("low")
                        c_val = r_clean.get("Close") or r_clean.get("close")
                        if d_val and o_val and h_val and l_val and c_val:
                            try:
                                bars.append({
                                    "date": str(d_val).strip(),
                                    "open": float(str(o_val).replace(",", "")),
                                    "high": float(str(h_val).replace(",", "")),
                                    "low": float(str(l_val).replace(",", "")),
                                    "close": float(str(c_val).replace(",", "")),
                                })
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                log.warning("Error reading historical CSV %s: %s", p, e)

    # 2. Daily rows from SQLite db
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


def get_gann_pivots_gaps(
    sec_id: str = "13",
    current_spot: Optional[float] = None,
    target_date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for GET /api/desk/gann-pivots-gaps.
    Loads deep daily history and computes master levels.
    """
    daily_bars = load_historical_daily_bars(sec_id=sec_id)
    return compute_master_levels_from_daily(
        daily_bars=daily_bars,
        current_spot=current_spot,
        target_date_str=target_date_str,
    )
