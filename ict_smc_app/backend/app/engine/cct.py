"""
engine/cct.py — Candle Continuity Theory (CCT) & 4-Candle Execution Framework.
Based on Chapter 25 of the Ultimate ICT & SMC Course Masterclass.

Mechanical Principles:
1. Open-Price Polarity:
   - Bullish CCT: HTF candle is green (Close > Open). Price expands upward.
     The highest-probability entry is BELOW the opening price (discount manipulation).
   - Bearish CCT: HTF candle is red (Close < Open). Price expands downward.
     The highest-probability entry is ABOVE the opening price (premium manipulation).
2. The 4-Candle Execution Trigger & Grading (A vs B):
   - Grade A: Confirming Candle 3 closes beyond Candle 2's Open (full body engulfing).
   - Grade B: Confirming Candle 3 fails to close beyond Candle 2's Open (weak / inside close).
3. Risk / Reward & Outcome Measurement:
   - SL placed under Candle 2 sweep wick extreme.
   - TP1 placed at 1:2 R or nearest Draw on Liquidity.
   - Measures real-time and historical MAE (Max Adverse Excursion), MFE, and R-multiple.
4. MMXM Context Awareness:
   - Tags setups as 'aligned', 'late', or 'counter'.
5. Zero Repainting / No Lookahead Contract:
   - Evaluates only fully closed bars (never unclosed forming bar).
   - True clock-hour open (not rolling window).
   - DST-aware timezone conversion for killzones and benchmarks.
   - POI mitigation tracked bar-by-bar strictly prior to pivot bar.
"""
from __future__ import annotations

import datetime
import logging
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .fvg import FVG
from .orderblocks import OrderBlock
from .primitives import Bars, Swing

log = logging.getLogger(__name__)


def get_tz(tz_name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return zoneinfo.ZoneInfo("America/New_York")


def to_local_dt(ts: int, zi: zoneinfo.ZoneInfo) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).astimezone(zi)


def is_in_killzone(ts: int, killzones: List[List[str]], zi: zoneinfo.ZoneInfo) -> Tuple[bool, str]:
    """Check if the given unix timestamp falls into an institutional trade window in target timezone."""
    dt = to_local_dt(ts, zi)
    hhmm_str = dt.strftime("%H:%M")
    for kz in killzones:
        if len(kz) == 2 and kz[0] <= hhmm_str <= kz[1]:
            name = f"Killzone ({kz[0]}–{kz[1]})"
            return True, name
    return False, "Outside Active Killzone"


def get_clock_hour_open(bars: Bars, zi: zoneinfo.ZoneInfo, target_idx: int = -1) -> float:
    """Find the clock-hour open price (e.g. 14:00:00 bar open), NOT a rolling window."""
    if len(bars) == 0:
        return 0.0
    idx = target_idx if target_idx >= 0 else len(bars) + target_idx
    idx = max(0, min(len(bars) - 1, idx))
    dt_target = to_local_dt(int(bars.ts[idx]), zi)
    target_date_hour = (dt_target.year, dt_target.month, dt_target.day, dt_target.hour)

    for k in range(idx, -1, -1):
        dt_k = to_local_dt(int(bars.ts[k]), zi)
        if (dt_k.year, dt_k.month, dt_k.day, dt_k.hour) != target_date_hour:
            return float(bars.o[k + 1])
    return float(bars.o[0])


def get_day_open(
    bars: Bars,
    zi: zoneinfo.ZoneInfo,
    benchmark_type: str = "18:00_ny",
    sess: Optional[Dict[str, Any]] = None,
    target_idx: int = -1,
) -> float:
    """
    Determines the explicit Day Open benchmark:
    - '18:00_ny': Globex futures session open at 18:00 NY previous calendar day.
    - 'midnight_ny': 00:00 NY calendar day open.
    - 'session_open': Open from session dict (e.g. 09:15 for equity) or first bar of day.
    """
    if len(bars) == 0:
        return 0.0
    idx = target_idx if target_idx >= 0 else len(bars) + target_idx
    idx = max(0, min(len(bars) - 1, idx))
    dt_target = to_local_dt(int(bars.ts[idx]), zi)

    if benchmark_type == "session_open" and sess and sess.get("open"):
        return float(sess["open"])

    if benchmark_type == "18:00_ny":
        if dt_target.hour >= 18:
            session_start_dt = dt_target.replace(hour=18, minute=0, second=0, microsecond=0)
        else:
            prev_day = dt_target - datetime.timedelta(days=1)
            session_start_dt = prev_day.replace(hour=18, minute=0, second=0, microsecond=0)
        session_start_ts = int(session_start_dt.timestamp())

        for k in range(idx, -1, -1):
            if int(bars.ts[k]) < session_start_ts:
                return float(bars.o[k + 1])
        return float(bars.o[0])

    if benchmark_type == "midnight_ny":
        midnight_dt = dt_target.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = int(midnight_dt.timestamp())
        for k in range(idx, -1, -1):
            if int(bars.ts[k]) < midnight_ts:
                return float(bars.o[k + 1])
        return float(bars.o[0])

    # Default fallback
    if sess and sess.get("open"):
        return float(sess["open"])
    target_date = dt_target.date()
    for k in range(idx, -1, -1):
        if to_local_dt(int(bars.ts[k]), zi).date() != target_date:
            return float(bars.o[k + 1])
    return float(bars.o[0])


def compute_cct(
    bars: Bars,
    atr_arr: np.ndarray,
    swings: List[Swing],
    fvgs: List[FVG],
    obs: List[OrderBlock],
    sess: Dict[str, Any],
    tf: str,
    rules: Optional[Dict[str, Any]] = None,
    mmxm_data: Optional[Dict[str, Any]] = None,
    is_closed: bool = True,
) -> Dict[str, Any]:
    """
    Computes CCT status, multi-timeframe open benchmarks, graded 4-candle execution setups,
    and MMXM context alignment.
    """
    rules = rules or config.rules().get("cct", {})
    N = len(bars)
    if N < 5:
        return {"available": False, "message": "Insufficient bars for CCT"}

    tz_name = rules.get("timezone", "America/New_York")
    zi = get_tz(tz_name)
    benchmark_type = rules.get("day_open_benchmark", "18:00_ny")

    curr_close = float(bars.c[-1])
    curr_ts = int(bars.ts[-1])
    killzones = rules.get("killzones", [["09:30", "11:30"], ["13:30", "16:00"]])
    in_kz, kz_name = is_in_killzone(curr_ts, killzones, zi)

    # 1. Higher Timeframe Open Benchmarks
    day_open = get_day_open(bars, zi, benchmark_type, sess)
    day_delta = curr_close - day_open

    # ATR-relative threshold for day bias
    atr_now = float(atr_arr[-1]) if len(atr_arr) > 0 else 20.0
    day_bias_mult = float(rules.get("day_bias_atr_mult", 0.1))
    day_bias_thresh = max(float(rules.get("day_bias_threshold_pts", 5.0)), day_bias_mult * atr_now)
    day_bias = "BULL" if day_delta > day_bias_thresh else "BEAR" if day_delta < -day_bias_thresh else "NEUTRAL"

    # True clock-hour open (e.g. 14:00 bar open)
    h1_open = get_clock_hour_open(bars, zi)
    h1_delta = curr_close - h1_open
    h1_bias_thresh = max(1.0, 0.05 * atr_now)
    h1_bias = "BULL" if h1_delta > h1_bias_thresh else "BEAR" if h1_delta < -h1_bias_thresh else "NEUTRAL"

    bar_open = float(bars.o[-1])
    bar_bias = "BULL" if curr_close > bar_open else "BEAR" if curr_close < bar_open else "NEUTRAL"

    if day_bias == "BULL" and h1_bias == "BULL":
        continuity = "STRONG_BULL"
        guidance = "HTF expansion bullish. Look for longs strictly BELOW the opening price (discount pullbacks)."
    elif day_bias == "BEAR" and h1_bias == "BEAR":
        continuity = "STRONG_BEAR"
        guidance = "HTF expansion bearish. Look for shorts strictly ABOVE the opening price (premium rallies)."
    elif day_bias == "BULL":
        continuity = "BULL_EXPANSION"
        guidance = "Daily candle green. Counter-trend shorts are hazardous; buy dips into discount."
    elif day_bias == "BEAR":
        continuity = "BEAR_EXPANSION"
        guidance = "Daily candle red. Counter-trend longs are hazardous; sell rallies into premium."
    else:
        continuity = "CHOP"
        guidance = "Price oscillating at opening benchmark. Wait for displacement away from open."

    # 2. 4-Candle Execution Triggers with Grading & Risk/Reward
    sl_buf = float(rules.get("sl_buffer_pts", 2.0))
    min_risk = float(rules.get("min_risk_pts", 5.0))
    max_risk = float(rules.get("max_risk_pts", 40.0))
    rr_target = float(rules.get("rr_target", 2.0))
    max_age = int(rules.get("max_trigger_age_bars", 15))
    enforce_polarity = bool(rules.get("enforce_polarity", True))
    require_strong = bool(rules.get("require_strong_confirmation", False))
    skip_oversized = bool(rules.get("skip_oversized", True))

    triggers: List[Dict[str, Any]] = []
    markers: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []

    # Extract MMXM primary model context
    mmxm_primary = (mmxm_data or {}).get("primary")
    mmxm_phase = mmxm_primary.get("phase") if mmxm_primary else None
    mmxm_target = mmxm_primary.get("target") if mmxm_primary else None
    mmxm_dir = mmxm_primary.get("dir") if mmxm_primary else None
    mmxm_dr = mmxm_primary.get("dealing_range") if mmxm_primary else None

    # P3: No repainting on last bar.
    # If is_closed is False, the live bar at N-1 is unclosed and cannot be C3.
    max_c3_idx = N - 2 if not is_closed else N - 1
    scan_lookback = int(rules.get("lookback_bars", 60))
    start_scan = max(2, max_c3_idx - scan_lookback)

    for i in range(start_scan, max_c3_idx + 1):
        c1_l, c2_l, c3_l = bars.l[i - 2], bars.l[i - 1], bars.l[i]
        c1_h, c2_h, c3_h = bars.h[i - 2], bars.h[i - 1], bars.h[i]
        c2_idx = i - 1

        # ── Bullish 3-bar Swing Low (strict inequality) ─────────────────────────
        if c2_l < c1_l and c2_l < c3_l:
            # P4: Hindsight-free POI check:
            # POI must be created before/at pivot and NOT mitigated/filled prior to pivot bar
            touched_poi = None
            for g in fvgs:
                if g.dir == "bull" and g.i <= c2_idx:
                    if c2_idx > 1 and g.state_at(c2_idx - 1) in ("invalid", "filled"):
                        continue
                    if g.bottom <= c2_l <= g.top or g.bottom <= c1_l <= g.top:
                        touched_poi = f"Bullish FVG {g.bottom:.0f}–{g.top:.0f}"
                        break
            if not touched_poi:
                for o in obs:
                    if o.dir == "bull" and o.created_i < c2_idx:
                        if o.broken_i is not None and o.broken_i < c2_idx:
                            continue
                        if o.mitigated_i is not None and o.mitigated_i < c2_idx:
                            continue
                        if o.bottom <= c2_l <= o.top:
                            touched_poi = f"Bullish OB {o.bottom:.0f}–{o.top:.0f}"
                            break

            if touched_poi:
                # Arming candle is C3 (index i)
                bar4_idx = i + 1
                entry_level = float(bars.o[bar4_idx]) if bar4_idx < N else float(bars.c[i])

                # P7: Polarity check: longs must be below Day Open (discount)
                day_open_at_arm = get_day_open(bars, zi, benchmark_type, sess, target_idx=i)
                if enforce_polarity and entry_level >= day_open_at_arm:
                    continue

                # P5: Risk clamping does NOT tighten structural stop
                sl = float(c2_l - sl_buf)
                risk = entry_level - sl

                if risk < min_risk:
                    continue
                if risk > max_risk:
                    if skip_oversized:
                        continue
                is_oversized = risk > max_risk

                tp1 = float(entry_level + rr_target * risk)

                # P8: Confirmation Quality: C3 close > C2 open
                is_strong = bars.c[i] > bars.o[i - 1]
                if require_strong and not is_strong:
                    continue
                confirmation_quality = "strong" if is_strong else "weak"
                grade = "A" if is_strong else "B"
                grade_desc = (
                    "Grade A (Strong: C3 close > C2 Open)"
                    if is_strong
                    else "Grade B (Weak: C3 close <= C2 Open)"
                )

                # X1: MMXM Context Integration ("aligned" | "late" | "counter")
                context = "aligned"
                if mmxm_primary:
                    if mmxm_dir == "bear" and mmxm_phase in ("LOW_RISK_SELL", "REDISTRIBUTION"):
                        context = "counter"
                    elif mmxm_phase == "TARGET_REACHED" or (mmxm_target and entry_level >= mmxm_target - 5.0):
                        context = "late"
                    elif mmxm_dr and mmxm_dr.get("high") and entry_level >= mmxm_dr["high"] - 0.25 * mmxm_dr.get("range", 100):
                        context = "late"
                else:
                    if entry_level > day_open_at_arm + 1.2 * atr_now:
                        context = "late"
                    elif day_bias == "BEAR":
                        context = "counter"

                is_counter = context in ("late", "counter")
                if rules.get("suppress_counter_context", False) and is_counter:
                    continue

                # P1 & P2: State machine: ARMED -> TRIGGERED -> STOPPED / TARGET_HIT / EXPIRED / INVALIDATED
                status = "ARMED"
                r_achieved = 0.0
                mae_pts = 0.0
                mfe_pts = 0.0
                fill_bar_idx: Optional[int] = None

                # Compute fill check over bars strictly after the arming bar (j > i)
                for j in range(i + 1, N):
                    if j - i > max_age:
                        status = "EXPIRED"
                        break

                    # Check if invalidated before fill (structure broken)
                    if bars.l[j] <= sl:
                        status = "INVALIDATED"
                        break

                    # Check fill condition
                    # For bar 4 (j == i + 1), price opens at entry_level; to trigger it must trade strictly below open into discount
                    filled = False
                    if j == i + 1:
                        if bars.l[j] < entry_level:
                            filled = True
                    else:
                        if bars.l[j] <= entry_level:
                            filled = True

                    if filled:
                        fill_bar_idx = j
                        status = "TRIGGERED"
                        break

                if status == "TRIGGERED" and fill_bar_idx is not None:
                    # Once filled, evaluate trade outcome on subsequent price action
                    for k in range(fill_bar_idx, N):
                        adverse = entry_level - float(bars.l[k])
                        favorable = float(bars.h[k]) - entry_level
                        mae_pts = max(mae_pts, adverse)
                        mfe_pts = max(mfe_pts, favorable)

                        if bars.l[k] <= sl:
                            status = "STOPPED"
                            r_achieved = -1.0
                            break
                        elif bars.h[k] >= tp1:
                            status = "TARGET_HIT"
                            r_achieved = rr_target
                            break
                    if status == "TRIGGERED":
                        r_achieved = round(float((bars.c[-1] - entry_level) / max(1.0, risk)), 2)

                t_time = chart_time(int(bars.ts[i]))
                dt_arm = to_local_dt(int(bars.ts[i]), zi)
                arm_hhmm = dt_arm.strftime("%H:%M")

                triggers.append({
                    "id": f"CCT_BUY_{bars.ts[i]}",
                    "dir": "bull",
                    "grade": grade,
                    "grade_desc": grade_desc,
                    "confirmation_quality": confirmation_quality,
                    "context": context,
                    "is_counter": is_counter,
                    "is_oversized": is_oversized,
                    "time": t_time,
                    "at": arm_hhmm,
                    "bar4_open": round(entry_level, 2),
                    "sl": round(sl, 2),
                    "tp1": round(tp1, 2),
                    "risk_pts": round(risk, 1),
                    "rr": rr_target,
                    "poi": touched_poi,
                    "status": status,
                    "outcome": status,
                    "r_achieved": r_achieved,
                    "mae_pts": round(mae_pts, 1),
                    "mfe_pts": round(mfe_pts, 1),
                    "instruction": f"Buy below {entry_level:.1f} in discount; SL {sl:.1f} · TP1 {tp1:.1f}",
                })

                short_lbl = f"CCT [{grade}] Long @ {entry_level:.0f}"
                if is_counter:
                    short_lbl += " ⚠"
                markers.append({
                    "time": t_time,
                    "kind": "CCT_BUY",
                    "dir": "bull",
                    "price": entry_level,
                    "grade": grade,
                    "context": context,
                    "is_counter": is_counter,
                    "status": status,
                    "shape": "circle" if is_counter else "arrowUp",
                    "color": "rgba(16,185,129,0.5)" if is_counter else "#10b981",
                    "text": short_lbl,
                    "at": arm_hhmm,
                    "sl": round(sl, 1),
                    "tp1": round(tp1, 1),
                })

        # ── Bearish 3-bar Swing High (strict inequality) ─────────────────────────
        if c2_h > c1_h and c2_h > c3_h:
            touched_poi = None
            for g in fvgs:
                if g.dir == "bear" and g.i <= c2_idx:
                    if c2_idx > 1 and g.state_at(c2_idx - 1) in ("invalid", "filled"):
                        continue
                    if g.bottom <= c2_h <= g.top or g.bottom <= c1_h <= g.top:
                        touched_poi = f"Bearish FVG {g.bottom:.0f}–{g.top:.0f}"
                        break
            if not touched_poi:
                for o in obs:
                    if o.dir == "bear" and o.created_i < c2_idx:
                        if o.broken_i is not None and o.broken_i < c2_idx:
                            continue
                        if o.mitigated_i is not None and o.mitigated_i < c2_idx:
                            continue
                        if o.bottom <= c2_h <= o.top:
                            touched_poi = f"Bearish OB {o.bottom:.0f}–{o.top:.0f}"
                            break

            if touched_poi:
                bar4_idx = i + 1
                entry_level = float(bars.o[bar4_idx]) if bar4_idx < N else float(bars.c[i])

                # P7: Polarity check: shorts must be above Day Open (premium)
                day_open_at_arm = get_day_open(bars, zi, benchmark_type, sess, target_idx=i)
                if enforce_polarity and entry_level <= day_open_at_arm:
                    continue

                sl = float(c2_h + sl_buf)
                risk = sl - entry_level

                if risk < min_risk:
                    continue
                if risk > max_risk:
                    if skip_oversized:
                        continue
                is_oversized = risk > max_risk

                tp1 = float(entry_level - rr_target * risk)

                is_strong = bars.c[i] < bars.o[i - 1]
                if require_strong and not is_strong:
                    continue
                confirmation_quality = "strong" if is_strong else "weak"
                grade = "A" if is_strong else "B"
                grade_desc = (
                    "Grade A (Strong: C3 close < C2 Open)"
                    if is_strong
                    else "Grade B (Weak: C3 close >= C2 Open)"
                )

                context = "aligned"
                if mmxm_primary:
                    if mmxm_dir == "bull" and mmxm_phase in ("LOW_RISK_BUY", "REACCUMULATION"):
                        context = "counter"
                    elif mmxm_phase == "TARGET_REACHED" or (mmxm_target and entry_level <= mmxm_target + 5.0):
                        context = "late"
                    elif mmxm_dr and mmxm_dr.get("low") and entry_level <= mmxm_dr["low"] + 0.25 * mmxm_dr.get("range", 100):
                        context = "late"
                else:
                    if entry_level < day_open_at_arm - 1.2 * atr_now:
                        context = "late"
                    elif day_bias == "BULL":
                        context = "counter"

                is_counter = context in ("late", "counter")
                if rules.get("suppress_counter_context", False) and is_counter:
                    continue

                status = "ARMED"
                r_achieved = 0.0
                mae_pts = 0.0
                mfe_pts = 0.0
                fill_bar_idx: Optional[int] = None

                for j in range(i + 1, N):
                    if j - i > max_age:
                        status = "EXPIRED"
                        break

                    if bars.h[j] >= sl:
                        status = "INVALIDATED"
                        break

                    filled = False
                    if j == i + 1:
                        if bars.h[j] > entry_level:
                            filled = True
                    else:
                        if bars.h[j] >= entry_level:
                            filled = True

                    if filled:
                        fill_bar_idx = j
                        status = "TRIGGERED"
                        break

                if status == "TRIGGERED" and fill_bar_idx is not None:
                    for k in range(fill_bar_idx, N):
                        adverse = float(bars.h[k]) - entry_level
                        favorable = entry_level - float(bars.l[k])
                        mae_pts = max(mae_pts, adverse)
                        mfe_pts = max(mfe_pts, favorable)

                        if bars.h[k] >= sl:
                            status = "STOPPED"
                            r_achieved = -1.0
                            break
                        elif bars.l[k] <= tp1:
                            status = "TARGET_HIT"
                            r_achieved = rr_target
                            break
                    if status == "TRIGGERED":
                        r_achieved = round(float((entry_level - bars.c[-1]) / max(1.0, risk)), 2)

                t_time = chart_time(int(bars.ts[i]))
                dt_arm = to_local_dt(int(bars.ts[i]), zi)
                arm_hhmm = dt_arm.strftime("%H:%M")

                triggers.append({
                    "id": f"CCT_SELL_{bars.ts[i]}",
                    "dir": "bear",
                    "grade": grade,
                    "grade_desc": grade_desc,
                    "confirmation_quality": confirmation_quality,
                    "context": context,
                    "is_counter": is_counter,
                    "is_oversized": is_oversized,
                    "time": t_time,
                    "at": arm_hhmm,
                    "bar4_open": round(entry_level, 2),
                    "sl": round(sl, 2),
                    "tp1": round(tp1, 2),
                    "risk_pts": round(risk, 1),
                    "rr": rr_target,
                    "poi": touched_poi,
                    "status": status,
                    "outcome": status,
                    "r_achieved": r_achieved,
                    "mae_pts": round(mae_pts, 1),
                    "mfe_pts": round(mfe_pts, 1),
                    "instruction": f"Sell above {entry_level:.1f} in premium; SL {sl:.1f} · TP1 {tp1:.1f}",
                })

                short_lbl = f"CCT [{grade}] Short @ {entry_level:.0f}"
                if is_counter:
                    short_lbl += " ⚠"
                markers.append({
                    "time": t_time,
                    "kind": "CCT_SELL",
                    "dir": "bear",
                    "price": entry_level,
                    "grade": grade,
                    "context": context,
                    "is_counter": is_counter,
                    "status": status,
                    "shape": "circle" if is_counter else "arrowDown",
                    "color": "rgba(239,68,68,0.5)" if is_counter else "#ef4444",
                    "text": short_lbl,
                    "at": arm_hhmm,
                    "sl": round(sl, 1),
                    "tp1": round(tp1, 1),
                })

    # Add lines for the most recent active trigger
    if triggers:
        last_trig = triggers[-1]
        lines.append({
            "label": f"CCT SL ({last_trig['sl']:.0f})",
            "price": last_trig["sl"],
            "color": "#ef4444",
            "style": "dashed",
        })
        lines.append({
            "label": f"CCT TP1 ({last_trig['tp1']:.0f})",
            "price": last_trig["tp1"],
            "color": "#10b981",
            "style": "dashed",
        })

    # Stats Summary strictly over filled trades (P2)
    filled_trigs = [t for t in triggers if t["status"] in ("TRIGGERED", "STOPPED", "TARGET_HIT")]
    closed_trigs = [t for t in triggers if t["status"] in ("STOPPED", "TARGET_HIT")]
    wins = sum(1 for t in closed_trigs if t["status"] == "TARGET_HIT")
    losses = sum(1 for t in closed_trigs if t["status"] == "STOPPED")
    win_rate = round(wins / len(closed_trigs) * 100, 1) if closed_trigs else None
    avg_r = round(float(np.mean([t["r_achieved"] for t in closed_trigs])), 2) if closed_trigs else 0.0
    avg_mae = round(float(np.mean([t["mae_pts"] for t in filled_trigs])), 1) if filled_trigs else 0.0

    stats = {
        "total_setups": len(triggers),
        "filled_setups": len(filled_trigs),
        "armed_count": sum(1 for t in triggers if t["status"] == "ARMED"),
        "expired_count": sum(1 for t in triggers if t["status"] == "EXPIRED"),
        "invalidated_count": sum(1 for t in triggers if t["status"] == "INVALIDATED"),
        "grade_a_count": sum(1 for t in triggers if t["grade"] == "A"),
        "grade_b_count": sum(1 for t in triggers if t["grade"] == "B"),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "avg_mae_pts": avg_mae,
    }

    return {
        "available": True,
        "curr_price": curr_close,
        "day_open": round(day_open, 2),
        "day_delta": round(day_delta, 2),
        "day_bias": day_bias,
        "h1_open": round(h1_open, 2),
        "h1_delta": round(h1_delta, 2),
        "h1_bias": h1_bias,
        "bar_open": round(bar_open, 2),
        "bar_bias": bar_bias,
        "continuity": continuity,
        "guidance": guidance,
        "in_killzone": in_kz,
        "killzone_name": kz_name,
        "triggers": triggers,
        "markers": markers,
        "lines": lines,
        "stats": stats,
    }
