"""
engine/pe_ce_matrix.py — Institutional PE-CE Difference & Skew Matrix 2.0.

Provides:
  1. Auto-ATM Strike Range Filtering (atm_10, atm_5, full)
  2. Per-Interval & Cumulative PE-CE Ledger Series with Center-Zero Diverging Bars
  3. Magnitude Position-Sizing & Regime Classifier (<5L, 5L-20L, 20L-30L, >30L)
  4. Slope Lead-Time Reversal Detector (15-30m early warning of zero-line flip)
  5. Equidistant PE vs CE Premium Skew Meter
  6. First-Hour 10:30 IST Anchor Lock
"""
from __future__ import annotations

import bisect
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .. import config, db
from ..timeutil import chart_time, hm, now_epoch, now_ist, session_bounds

log = logging.getLogger("pe_ce_matrix")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("pe_ce_matrix", {})


def classify_magnitude_regime(pe_ce_day_diff: int, cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str, float]:
    """
    Classifies the cumulative PE-CE Day Difference magnitude into institutional sizing guidance:
      - BALANCED_RANGE (< 5L): 0.5x size / Range-bound premium selling
      - MODERATE_CONVICTION (5L - 20L): 1.0x standard directional size
      - LARGE_HIGH_CONVICTION (20L - 30L): 1.5x high conviction size
      - EXTREME_CROWDED_ALERT (>= 30L): 1.0x crowded alert / contrarian caution
    """
    c = cfg or get_cfg()
    bal_thresh = int(c.get("balanced_range_threshold", 500000))
    mod_thresh = int(c.get("moderate_conviction_threshold", 2000000))
    large_thresh = int(c.get("large_conviction_threshold", 3000000))

    abs_diff = abs(pe_ce_day_diff)
    sign = "Bullish (Put Dominance)" if pe_ce_day_diff > 0 else "Bearish (Call Dominance)" if pe_ce_day_diff < 0 else "Neutral"

    if abs_diff < bal_thresh:
        return (
            "BALANCED_RANGE",
            f"Small difference (<{bal_thresh/1e5:.0f}L) — Market in equilibrium. Favors range-bound premium selling (Iron Condors/Strangles) or 0.5x size.",
            0.5,
        )
    if abs_diff < mod_thresh:
        return (
            "MODERATE_CONVICTION",
            f"Significant difference ({bal_thresh/1e5:.0f}L–{mod_thresh/1e5:.0f}L) — 1.0x standard directional size ({sign}).",
            1.0,
        )
    if abs_diff < large_thresh:
        return (
            "LARGE_HIGH_CONVICTION",
            f"Large institutional commitment ({mod_thresh/1e5:.0f}L+) — 1.5x high conviction size aligned with slope ({sign}).",
            1.5,
        )
    return (
        "EXTREME_CROWDED_ALERT",
        f"Extreme reading ({large_thresh/1e5:.0f}L+) — Short-term strong, but crowded positioning. Do not chase late; watch for slope mean-reversion flip.",
        1.0,
    )


def detect_slope_flip(
    interval_diffs: List[int],
    day_diff: int,
    call_steps: Optional[Any] = None,
    put_steps: Optional[List[int]] = None,
    current_time_label: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Detects early 15-30m slope lead-time reversals before the cumulative zero-line flips:
      - Bullish Slope Flip: Day diff is still negative (e.g. -20.8L), but the last 3 interval bars are strongly positive (+30.6L, +10.1L, +10.7L).
      - Bearish Slope Flip: Day diff is still positive (e.g. +15.4L), but the last 3 interval bars are strongly negative with genuine Call writing (call_step > 0) before 15:05 IST.
      - EOD Mutual Unwinding: Post-15:05 or when both Puts and Calls are closing positions (put_step < 0 and call_step < 0).
    """
    if isinstance(call_steps, dict):
        cfg = call_steps
        call_steps = None

    c = cfg or get_cfg()
    bars = int(c.get("slope_flip_bars", 3))
    min_impulse = int(c.get("slope_flip_min_impulse", 100000))
    min_genuine_oi = int(c.get("slope_flip_min_genuine_oi", 100000))
    eod_cutoff = str(c.get("eod_cutoff_hhmm", "15:05"))

    if len(interval_diffs) < bars:
        return "NONE", ""

    last_diffs = interval_diffs[-bars:]
    last_calls = call_steps[-bars:] if call_steps and len(call_steps) >= bars else []
    last_puts = put_steps[-bars:] if put_steps and len(put_steps) >= bars else []

    # Check for EOD mutual unwinding / profit-booking:
    is_eod_window = current_time_label >= eod_cutoff and current_time_label != "Prev Day"
    mutual_unwinding = (
        bool(last_calls and last_puts)
        and sum(last_calls) <= 0
        and sum(last_puts) <= 0
    )

    if (is_eod_window or mutual_unwinding) and mutual_unwinding:
        bias_side = "Bullish" if day_diff > 0 else "Bearish" if day_diff < 0 else "Neutral"
        return (
            "EOD_MUTUAL_UNWINDING",
            f"🏁 EOD Mutual Profit-Booking (Both Put & Call Writers Unwinding into Close — {bias_side} Day Structure Intact)",
        )

    # Bullish flip: Cumulative is negative, but every recent bar has strong positive PE writing impulse
    if day_diff < 0 and all(x >= min_impulse for x in last_diffs):
        if not is_eod_window:
            if not last_puts or sum(last_puts) >= -min_genuine_oi:
                return (
                    "BULLISH_SLOPE_FLIP",
                    "🚀 BULLISH SLOPE FLIP (15-30m Lead Reversal Warning — Put writers aggressively adding while Day Diff is still negative). Squeeze ahead!",
                )

    # Bearish flip: Cumulative is positive, but every recent bar has strong negative Call writing impulse
    # AND Call writers are genuinely adding fresh OI (call_steps > 0), NOT just covering/unwinding!
    if day_diff > 0 and all(x <= -min_impulse for x in last_diffs):
        if not is_eod_window:
            if last_calls and sum(last_calls) >= min_genuine_oi:
                return (
                    "BEARISH_SLOPE_FLIP",
                    "🩸 BEARISH SLOPE FLIP (15-30m Lead Reversal Warning — Call writers aggressively adding while Day Diff is still positive). Breakdown risk!",
                )
            elif not last_calls:
                return (
                    "BEARISH_SLOPE_FLIP",
                    "🩸 BEARISH SLOPE FLIP (15-30m Lead Reversal Warning — Call writers aggressively adding while Day Diff is still positive). Breakdown risk!",
                )

    return "NONE", ""


def calculate_premium_skew(
    atm_pe_ltp: float,
    atm_ce_ltp: float,
    spot: float = 0.0,
    atm_strike: float = 0.0,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compares equidistant ATM / 1-OTM Put vs Call premiums:
      - Strips intrinsic value when spot and atm_strike are provided to compare pure extrinsic time-value.
      - STEEP_PUT_SKEW (PE >> CE by >20%): Fearful / downside hedging
      - NORMAL_PUT_SKEW (PE > CE by 5%-20%): Routine protective bias
      - FLAT_SKEW (PE ≈ CE within ±5%): Balanced expectations
      - CALL_SKEW_NARROWING (CE >= PE by 5%-20%): Upside momentum building / complacency
      - EXTREME_CALL_SKEW (CE >> PE by >20%): Euphoric upside chase
    """
    if isinstance(spot, dict):
        cfg = spot
        spot = 0.0
        atm_strike = 0.0

    c = cfg or get_cfg()
    steep_pct = float(c.get("premium_skew_steep_pct", 20.0))
    norm_pct = float(c.get("premium_skew_normal_pct", 5.0))

    if atm_ce_ltp <= 0 and atm_pe_ltp <= 0:
        return {
            "skew_state": "FLAT_SKEW",
            "skew_label": "⚖️ Balanced Premium Skew",
            "skew_pct": 0.0,
            "pe_ltp": 0.0,
            "ce_ltp": 0.0,
            "pe_extrinsic": 0.0,
            "ce_extrinsic": 0.0,
            "interpretation": "Insufficient quote data for premium skew.",
        }

    # If spot and strike provided, compute pure extrinsic time-value by stripping intrinsic value
    if spot > 0 and atm_strike > 0:
        ce_intrinsic = max(0.0, spot - atm_strike)
        pe_intrinsic = max(0.0, atm_strike - spot)
        ce_extrinsic = max(atm_ce_ltp - ce_intrinsic, 0.5)
        pe_extrinsic = max(atm_pe_ltp - pe_intrinsic, 0.5)
        eval_pe = pe_extrinsic
        eval_ce = ce_extrinsic
    else:
        ce_extrinsic = atm_ce_ltp
        pe_extrinsic = atm_pe_ltp
        eval_pe = atm_pe_ltp
        eval_ce = atm_ce_ltp

    ref = max(min(eval_ce, eval_pe), 1.0)
    skew_pct = round(((eval_pe - eval_ce) / ref) * 100.0, 1)

    if skew_pct > steep_pct:
        state = "STEEP_PUT_SKEW"
        label = f"🛡️ Steep Put Skew (+{skew_pct:.1f}%)"
        interp = "Risk-Off / Active Downside Hedging (Market Paying Up for Downside Puts)"
    elif skew_pct >= norm_pct:
        state = "NORMAL_PUT_SKEW"
        label = f"🛡️ Normal Put Skew (+{skew_pct:.1f}%)"
        interp = "Routine Protective Bias (Orderly Market with Standard Hedging)"
    elif skew_pct >= -norm_pct:
        state = "FLAT_SKEW"
        label = f"⚖️ Flat Skew ({skew_pct:+.1f}%)"
        interp = "Balanced Expectations (Puts & Calls Equally Priced)"
    elif skew_pct >= -steep_pct:
        state = "CALL_SKEW_NARROWING"
        label = f"🚀 Call Skew Rising ({skew_pct:.1f}%)"
        interp = "Upside Momentum Building / Complacency Rising (Call Buyers Paying Up)"
    else:
        state = "EXTREME_CALL_SKEW"
        label = f"🔥 Extreme Call Skew ({skew_pct:.1f}%)"
        interp = "Euphoric Upside Chase / Call Buying Froth (Watch for Exhaustion)"

    return {
        "skew_state": state,
        "skew_label": label,
        "skew_pct": skew_pct,
        "pe_ltp": round(atm_pe_ltp, 2),
        "ce_ltp": round(atm_ce_ltp, 2),
        "pe_extrinsic": round(pe_extrinsic, 2),
        "ce_extrinsic": round(ce_extrinsic, 2),
        "interpretation": interp,
    }


def determine_fut_buildup(price_delta: float, oi_delta: float) -> str:
    """
    Standard institutional buildup classifier:
      - Price ↑ OI ↑: LONG_BUILDUP (LB)
      - Price ↓ OI ↑: SHORT_BUILDUP (SB)
      - Price ↑ OI ↓: SHORT_COVERING (SC)
      - Price ↓ OI ↓: LONG_UNWINDING (LU)
    """
    if oi_delta > 0:
        return "LONG_BUILDUP" if price_delta >= 0 else "SHORT_BUILDUP"
    if oi_delta < 0:
        return "SHORT_COVERING" if price_delta >= 0 else "LONG_UNWINDING"
    return "NEUTRAL"


def compute_first_hour_anchor(rows: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Evaluates 09:15 to 10:30 IST first-hour institutional flow anchor.
    """
    c = cfg or get_cfg()
    anchor_hhmm = str(c.get("first_hour_anchor_hhmm", "10:30"))

    if not rows:
        return {"bias": "FORMING", "label": "Anchor Forming", "diff_l": 0.0, "text": "Awaiting session data."}

    # Find row at or closest before 10:30
    h1030_rows = [r for r in rows if r.get("time_label", "") <= anchor_hhmm and r.get("time_label") != "Prev Day"]
    first_row = [r for r in rows if r.get("time_label") != "Prev Day"]

    if not h1030_rows or not first_row:
        return {"bias": "FORMING", "label": "Anchor Forming", "diff_l": 0.0, "text": "Session anchor forming."}

    anchor_pt = h1030_rows[-1]
    open_pt = first_row[0]

    anchor_diff = int(anchor_pt.get("pe_ce_day_diff") or 0)
    open_diff = int(open_pt.get("pe_ce_day_diff") or 0)
    net_1030 = anchor_diff - open_diff
    net_l = round(net_1030 / 1e5, 1)

    if net_1030 > 500000:
        bias = "BULLISH_FIRST_HOUR"
        label = f"🟢 10:30 Bullish Anchor (+{net_l:.1f}L)"
        text = f"First-hour institutional bias established BULLISH (+{net_l:.1f}L net Put writing)."
    elif net_1030 < -500000:
        bias = "BEARISH_FIRST_HOUR"
        label = f"🔴 10:30 Bearish Anchor ({net_l:.1f}L)"
        text = f"First-hour institutional bias established BEARISH ({net_l:.1f}L net Call writing)."
    else:
        bias = "BALANCED_FIRST_HOUR"
        label = f"⚖️ 10:30 Balanced Anchor ({net_l:+.1f}L)"
        text = f"First-hour institutional bias BALANCED ({net_l:+.1f}L difference)."

    return {
        "bias": bias,
        "label": label,
        "diff_l": net_l,
        "text": text,
        "anchor_hhmm": anchor_hhmm,
    }


def classify_action_stage(
    pe_ce_day_diff: int,
    pe_ce_step_diff: int,
    time_label: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """
    6-Stage Action Lifecycle Verdict:
      Stage 0: WAIT (Market balanced / equilibrium)
      Stage 1: Trend Ignition / Breakout (Day diff small, strong single-bar impulse)
      Stage 2: Dip Buy (CE) / Pullback Sell (PE)
      Stage 3: Acceleration (Add CE / Add PE)
      Stage 4: Hold aligned position (Orderly continuation)
      Stage 5: Digestion / Short-Covering Bounce (Trail SL / Partial Profit)
      Post-15:10: EOD Closing Window (Do not open fresh entries, trail/book)
    """
    if time_label >= "15:10" and time_label != "Prev Day":
        return "🏁 EOD Closing (15:10+) — Trail / Book"

    c = cfg or get_cfg()
    regime_thresh = int(c.get("stage_regime_threshold", 500000))
    accel_thresh = int(c.get("stage_step_accel_threshold", 250000))
    digest_thresh = int(c.get("stage_step_digestion_threshold", -250000))

    if pe_ce_day_diff > regime_thresh:
        # Bullish Regime (Put Dominance)
        if pe_ce_step_diff >= accel_thresh:
            return "🔥 Stage 3 Bullish Acceleration (Add CE)"
        elif pe_ce_step_diff > 0:
            return "🟢 Stage 2 Dip Buy (CE)"
        elif pe_ce_step_diff <= digest_thresh:
            return "🟡 Stage 5 Bullish Digestion / Trail CE"
        else:
            return "🛡️ Stage 4 Hold CE"

    elif pe_ce_day_diff < -regime_thresh:
        # Bearish Regime (Call Dominance)
        if pe_ce_step_diff <= -accel_thresh:
            return "🩸 Stage 3 Bearish Acceleration (Add PE)"
        elif pe_ce_step_diff < 0:
            return "🔴 Stage 2 Pullback Sell (PE)"
        elif pe_ce_step_diff >= -digest_thresh:
            return "🟡 Stage 5 Short Covering / Book PE"
        else:
            return "🛡️ Stage 4 Hold PE"

    else:
        # Balanced / S0 / Breakout Regime
        if pe_ce_step_diff >= accel_thresh:
            return "🟢 Stage 1 Bullish Breakout (CE)"
        elif pe_ce_step_diff <= -accel_thresh:
            return "🔴 Stage 1 Bearish Breakdown (PE)"
        return "⚖️ S0 WAIT"


def compute_pe_ce_matrix(
    expiry: Optional[str] = None,
    tf: str = "3m",
    scope: str = "atm_10",
    fut_candles: Optional[List[Dict[str, Any]]] = None,
    force_expiry_mode: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Builds the full Institutional PE-CE Difference & Skew Matrix 2.0.
    """
    c = cfg or get_cfg()
    today_dt = now_ist().date()
    today_start, today_end = session_bounds(today_dt)
    today_iso = today_dt.isoformat()

    # 1. Resolve active expiries >= today_iso
    active_rows = db.query(
        "SELECT DISTINCT expiry FROM chain_snap WHERE expiry >= ? AND ts >= ? ORDER BY expiry ASC",
        (today_iso, today_start),
    )
    if not active_rows:
        active_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_latest WHERE expiry >= ? ORDER BY expiry ASC",
            (today_iso,),
        )
    if not active_rows:
        active_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_snap WHERE expiry >= ? ORDER BY expiry ASC",
            (today_iso,),
        )
    if not active_rows:
        fallback_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_snap ORDER BY expiry DESC LIMIT 2"
        )
        active_exps = sorted([r["expiry"] for r in fallback_rows]) if fallback_rows else ["2026-10-27"]
    else:
        active_exps = [r["expiry"] for r in active_rows]

    cur_exp = active_exps[0] if len(active_exps) > 0 else "2026-10-27"
    next_exp = active_exps[1] if len(active_exps) > 1 else cur_exp

    if not expiry or expiry in ("cur", "This wk", "This Wk", "this_wk"):
        target_exps = [cur_exp]
        display_exp = cur_exp
    elif expiry in ("next", "Next wk", "Next Wk", "next_wk"):
        target_exps = [next_exp]
        display_exp = next_exp
    elif expiry in ("both", "Both", "both_wk"):
        target_exps = [cur_exp, next_exp] if next_exp != cur_exp else [cur_exp]
        display_exp = f"{cur_exp} + {next_exp}" if next_exp != cur_exp else cur_exp
    else:
        target_exps = [expiry]
        display_exp = expiry

    # Step interval in seconds
    step_s = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}.get(tf, 180)

    # 2. Query snapshots strictly within session bounds
    placeholders = ",".join("?" for _ in target_exps)
    snap_rows = db.query(f"""
        SELECT expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_vol, pe_vol
        FROM chain_snap
        WHERE expiry IN ({placeholders}) AND ts >= ? AND ts <= ?
        ORDER BY ts ASC, strike ASC
    """, (*target_exps, today_start, today_end))

    # Safe fallback: If no snapshots today, identify single latest session date in db
    if not snap_rows:
        latest_ts_row = db.query(f"""
            SELECT ts FROM chain_snap
            WHERE expiry IN ({placeholders})
            ORDER BY ts DESC LIMIT 1
        """, (*target_exps,))
        if latest_ts_row:
            latest_dt = datetime.fromtimestamp(latest_ts_row[0]["ts"], tz=ZoneInfo("Asia/Kolkata")).date()
            s_start, s_end = session_bounds(latest_dt)
            snap_rows = db.query(f"""
                SELECT expiry, ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp, ce_vol, pe_vol
                FROM chain_snap
                WHERE expiry IN ({placeholders}) AND ts >= ? AND ts <= ?
                ORDER BY ts ASC, strike ASC
            """, (*target_exps, s_start, s_end))
            today_start, today_end = s_start, s_end

    # 3. Fetch FUT candles and OI series with sorted timestamps
    if fut_candles is None:
        fut_candles = db.candles_1m("FUT", today_start)
        if not fut_candles:
            fut_candles = db.candles_1m(str(config.rules().get("instruments", {}).get("nifty_security_id", "13")), today_start)
        if not fut_candles:
            fut_candles = db.candles_1m("13", today_start)

    fut_candles = sorted(fut_candles or [], key=lambda c: int(c["ts"]))
    sorted_candle_ts = [int(c["ts"]) for c in fut_candles]

    fut_oi_rows = db.query(
        "SELECT ts, oi FROM fut_oi WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
        (today_start, today_end),
    )
    fut_oi_list = [(int(r["ts"]), float(r["oi"])) for r in fut_oi_rows]
    sorted_oi_ts = [x[0] for x in fut_oi_list]
    open_fut_oi = fut_oi_list[0][1] if fut_oi_list else (
        float(fut_candles[0].get("oi", 14000000.0)) if fut_candles and fut_candles[0].get("oi") else 14000000.0
    )

    def get_asof_candle(t: int) -> Optional[Dict[str, Any]]:
        idx = bisect.bisect_right(sorted_candle_ts, t) - 1
        if idx >= 0:
            return fut_candles[idx]
        return fut_candles[0] if fut_candles else None

    def get_asof_oi(t: int) -> float:
        idx = bisect.bisect_right(sorted_oi_ts, t) - 1
        if idx >= 0:
            return fut_oi_list[idx][1]
        return open_fut_oi

    # Group snapshots by expiry and by timestamp
    snaps_by_exp: Dict[str, Dict[int, List[sqlite3.Row]]] = {}
    for r in snap_rows:
        snaps_by_exp.setdefault(r["expiry"], {}).setdefault(int(r["ts"]), []).append(r)

    ts_by_exp: Dict[str, List[int]] = {e: sorted(m.keys()) for e, m in snaps_by_exp.items()}
    all_snap_ts = sorted(set(ts for ts_list in ts_by_exp.values() for ts in ts_list))

    if not all_snap_ts:
        return {
            "ok": False,
            "message": "No snapshots recorded for this expiry yet.",
            "rows": [],
            "regime": {},
            "skew": {},
            "first_hour": {},
            "strike_range_label": "All Strikes",
        }

    # Determine ATM strike and strike bounds for the scope
    latest_ts = all_snap_ts[-1]
    c_latest = get_asof_candle(latest_ts)
    latest_fut = float(c_latest["c"]) if c_latest else (fut_candles[-1]["c"] if fut_candles else 22600.0)
    atm_strike = round(latest_fut / 50.0) * 50.0

    if scope == "atm_10":
        min_strike = atm_strike - 500.0
        max_strike = atm_strike + 500.0
        range_label = f"{min_strike:,.0f} – {max_strike:,.0f} (±10 ATM)"
    elif scope == "atm_5":
        min_strike = atm_strike - 250.0
        max_strike = atm_strike + 250.0
        range_label = f"{min_strike:,.0f} – {max_strike:,.0f} (±5 ATM)"
    else:
        min_strike = 0.0
        max_strike = 100000.0
        range_label = "Full Chain (All Strikes)"

    # Compute baseline open OI across selected strikes at open
    baseline_ce_oi = 0.0
    baseline_pe_oi = 0.0
    for e in target_exps:
        if e in ts_by_exp and ts_by_exp[e]:
            first_t = ts_by_exp[e][0]
            s_rows = [r for r in snaps_by_exp[e][first_t] if min_strike <= float(r["strike"]) <= max_strike]
            baseline_ce_oi += sum(float(r["ce_oi"] or 0) for r in s_rows)
            baseline_pe_oi += sum(float(r["pe_oi"] or 0) for r in s_rows)

    # Aggregate across the requested timeframe intervals
    bucket_ts_set = set((ts // step_s) * step_s for ts in all_snap_ts)
    bucket_keys = sorted(bucket_ts_set)

    ledger_rows: List[Dict[str, Any]] = []
    # Buckets exist only where a snapshot exists, so a poller outage leaves NO row and the table closes the
    # hole up silently. The step deltas below are computed against the previous ROW, not the previous clock
    # interval, so the row after an outage reports (say) a 30-minute change in the same column, units and
    # thresholds as a genuine 3-minute one — and that value feeds the slope-flip detector and conviction.
    # Carry each bucket's real span so the row can say so instead of pretending to be a normal interval.
    prev_b_ts: Optional[int] = None
    gap_factor = float(c.get("gap_flag_steps", 1.0))
    prev_cum_ce_chg = 0
    prev_cum_pe_chg = 0
    prev_fut_ltp = float(fut_candles[0]["c"]) if fut_candles else latest_fut
    prev_fut_oi = open_fut_oi
    prev_curr_ce_oi = baseline_ce_oi
    prev_curr_pe_oi = baseline_pe_oi

    step_diffs: List[int] = []
    call_step_diffs: List[int] = []
    put_step_diffs: List[int] = []

    pcr_min = float(c.get("pcr_min_clamp", 0.10))
    pcr_max = float(c.get("pcr_max_clamp", 5.00))

    for b_ts in bucket_keys:
        curr_ce_oi = 0.0
        curr_pe_oi = 0.0
        effective_snap_t = b_ts

        # Forward-fill latest snapshot for each target expiry at or before interval end
        for e in target_exps:
            if e in ts_by_exp and ts_by_exp[e]:
                exp_ts_list = ts_by_exp[e]
                idx = bisect.bisect_right(exp_ts_list, b_ts + step_s - 1) - 1
                if idx >= 0:
                    snap_t = exp_ts_list[idx]
                    effective_snap_t = max(effective_snap_t, snap_t)
                    s_rows = [r for r in snaps_by_exp[e][snap_t] if min_strike <= float(r["strike"]) <= max_strike]
                    curr_ce_oi += sum(float(r["ce_oi"] or 0) for r in s_rows)
                    curr_pe_oi += sum(float(r["pe_oi"] or 0) for r in s_rows)

        # Dropped-frame glitch filter
        if prev_curr_ce_oi > 0 and curr_ce_oi < 0.25 * prev_curr_ce_oi:
            curr_ce_oi = prev_curr_ce_oi
        if prev_curr_pe_oi > 0 and curr_pe_oi < 0.25 * prev_curr_pe_oi:
            curr_pe_oi = prev_curr_pe_oi

        # Day Change = Current Total OI minus Open Baseline
        cum_ce_chg = int(round(curr_ce_oi - baseline_ce_oi))
        cum_pe_chg = int(round(curr_pe_oi - baseline_pe_oi))

        # Interval Step Change = Change since previous bar interval
        step_ce_chg = cum_ce_chg - prev_cum_ce_chg
        step_pe_chg = cum_pe_chg - prev_cum_pe_chg

        # Differences
        pe_ce_day_diff = cum_pe_chg - cum_ce_chg
        pe_ce_step_diff = step_pe_chg - step_ce_chg
        step_diffs.append(pe_ce_step_diff)
        call_step_diffs.append(step_ce_chg)
        put_step_diffs.append(step_pe_chg)

        # Clamped PCR Day
        raw_pcr = curr_pe_oi / max(curr_ce_oi, 1.0)
        pcr_day = round(min(max(raw_pcr, pcr_min), pcr_max), 2)

        # Futures price & OI via Asof lookup
        c_asof = get_asof_candle(effective_snap_t)
        fut_ltp = float(c_asof["c"]) if c_asof else prev_fut_ltp
        fut_oi_val = get_asof_oi(effective_snap_t)
        fut_oi_day_chg = int(round(fut_oi_val - open_fut_oi))

        fut_price_delta = fut_ltp - prev_fut_ltp
        fut_oi_delta = fut_oi_val - prev_fut_oi

        # Ensure buildup tag uses the exact same basis as displayed fut_oi_day_chg
        open_fut_price = float(fut_candles[0]["c"]) if fut_candles else fut_ltp
        day_price_delta = fut_ltp - open_fut_price
        eval_p_delta = day_price_delta if abs(day_price_delta) >= 1.0 else fut_price_delta
        eval_oi_delta = fut_oi_day_chg if fut_oi_day_chg != 0 else fut_oi_delta
        buildup = determine_fut_buildup(eval_p_delta, eval_oi_delta)

        # Time label formatted as HH:MM IST
        t_label = hm(effective_snap_t)

        # 6-Stage Action Classification
        stage_label = classify_action_stage(pe_ce_day_diff, pe_ce_step_diff, t_label, c)

        # How much wall-clock this row's step deltas actually cover. The first row has no predecessor, so it
        # spans one nominal interval by definition and is never a gap.
        step_span_s = int(step_s if prev_b_ts is None else b_ts - prev_b_ts)
        step_is_gap = prev_b_ts is not None and step_span_s > step_s * gap_factor

        ledger_rows.append({
            "time": chart_time(effective_snap_t),
            "time_label": t_label,
            "put_oi_day_chg": cum_pe_chg,
            "put_oi_step_chg": step_pe_chg,
            "call_oi_day_chg": cum_ce_chg,
            "call_oi_step_chg": step_ce_chg,
            "pe_ce_day_diff": pe_ce_day_diff,
            "pe_ce_step_diff": pe_ce_step_diff,
            "pcr_day": pcr_day,
            "step_span_s": step_span_s,
            "step_is_gap": step_is_gap,
            "fut_ltp": round(fut_ltp, 1),
            "fut_oi_day_chg": fut_oi_day_chg,
            "fut_buildup": buildup,
            "stage_label": stage_label,
        })

        prev_b_ts = b_ts
        prev_cum_ce_chg = cum_ce_chg
        prev_cum_pe_chg = cum_pe_chg
        prev_fut_ltp = fut_ltp
        prev_fut_oi = fut_oi_val
        prev_curr_ce_oi = curr_ce_oi
        prev_curr_pe_oi = curr_pe_oi

    # Append "Prev Day" row at the bottom (oldest anchor)
    open_ts = all_snap_ts[0]
    prev_day_row = {
        "time": chart_time(open_ts - 60),
        "time_label": "Prev Day",
        "put_oi_day_chg": 0,
        "put_oi_step_chg": 0,
        "call_oi_day_chg": 0,
        "call_oi_step_chg": 0,
        "pe_ce_day_diff": 0,
        "pe_ce_step_diff": 0,
        "pcr_day": round(min(max(baseline_pe_oi / max(baseline_ce_oi, 1.0), pcr_min), pcr_max), 2) if baseline_ce_oi > 0 else 1.0,
        "fut_ltp": round(fut_candles[0]["c"] if fut_candles else 22500.0, 1),
        "fut_oi_day_chg": 0,
        "fut_buildup": "NEUTRAL",
        "stage_label": "Baseline",
    }

    # Evaluate Premium Skew from latest snapshot around ATM
    latest_rows: List[sqlite3.Row] = []
    if cur_exp in snaps_by_exp and ts_by_exp.get(cur_exp):
        latest_cur_t = ts_by_exp[cur_exp][-1]
        latest_rows = snaps_by_exp[cur_exp][latest_cur_t]
    elif target_exps[0] in snaps_by_exp and ts_by_exp.get(target_exps[0]):
        t0_exp = target_exps[0]
        latest_rows = snaps_by_exp[t0_exp][ts_by_exp[t0_exp][-1]]

    atm_pe_row = [r for r in latest_rows if float(r["strike"]) == atm_strike - 50.0] or [r for r in latest_rows if float(r["strike"]) == atm_strike]
    atm_ce_row = [r for r in latest_rows if float(r["strike"]) == atm_strike + 50.0] or [r for r in latest_rows if float(r["strike"]) == atm_strike]

    atm_pe_ltp = float(atm_pe_row[0]["pe_ltp"] or 0.0) if atm_pe_row else 100.0
    atm_ce_ltp = float(atm_ce_row[0]["ce_ltp"] or 0.0) if atm_ce_row else 100.0
    skew_data = calculate_premium_skew(
        atm_pe_ltp,
        atm_ce_ltp,
        spot=latest_fut,
        atm_strike=atm_strike,
        cfg=c,
    )

    # Classify current magnitude regime and slope-flip
    current_day_diff = ledger_rows[-1]["pe_ce_day_diff"] if ledger_rows else 0
    tier_key, tier_text, sizing = classify_magnitude_regime(current_day_diff, c)
    latest_time_label = ledger_rows[-1]["time_label"] if ledger_rows else ""
    flip_state, flip_text = detect_slope_flip(
        step_diffs,
        current_day_diff,
        call_steps=call_step_diffs,
        put_steps=put_step_diffs,
        current_time_label=latest_time_label,
        cfg=c,
    )
    first_hour = compute_first_hour_anchor(ledger_rows, c)

    regime_payload = {
        "tier_key": tier_key,
        "tier_text": tier_text,
        "sizing_mult": sizing,
        "current_diff": current_day_diff,
        "slope_flip_state": flip_state,
        "slope_flip_text": flip_text,
    }

    # Return ledger with newest row on top (matching StockMojo & Options Lab table layout)
    display_rows = ledger_rows[::-1] + [prev_day_row]

    # Expiry Day Playbook Integration
    try:
        from . import expiry_playbook
        # Hand the cockpit this ledger's own PE-CE flow so the 0-DTE cards and the
        # matrix table can never report different numbers for the same interval.
        expiry_telemetry = expiry_playbook.compute_expiry_cockpit_telemetry(
            expiry=display_exp,
            force_expiry_mode=force_expiry_mode,
            tf=tf,
            cfg=config.rules().get("expiry_playbook"),
            pe_ce_day_diff=current_day_diff,
            pe_ce_step_diff=(step_diffs[-1] if step_diffs else 0),
        )
    except Exception as e_pb:
        log.warning("expiry_playbook compute error: %s", e_pb)
        expiry_telemetry = {"ok": False, "error": str(e_pb)}

    return {
        "ok": True,
        "day": str(now_ist().date()),
        "expiry": display_exp,
        "tf": tf,
        "scope": scope,
        "strike_range_label": range_label,
        "spot": round(latest_fut, 1),
        "atm_strike": atm_strike,
        "regime": regime_payload,
        "skew": skew_data,
        "first_hour": first_hour,
        "rows": display_rows,
        "snapshots": len(all_snap_ts),
        "expiry_playbook": expiry_telemetry,
    }
