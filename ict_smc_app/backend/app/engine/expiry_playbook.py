"""
engine/expiry_playbook.py — Institutional Expiry-Day (Zero-DTE) Playbook Engine.

Provides:
  1. Auto-detection of Expiry Day (0-DTE / 1-DTE / Simulation Mode).
  2. Live ATM Straddle Premium, Decay % & Market-Maker Breakeven Pin Box.
  3. Gamma Pin / Theta Crush Detector (Pin Strike & Pin Zone bounds).
  4. Trapped Option Writer Radar (Under-Water Call/Put Writers Covering).
  5. 5-Factor "Hero-Zero Sniper" Qualification Engine (12:45–14:50 IST prime window).
  6. 15:00–15:30 IST NSE Expiry Settlement VWAP Average Price Tracker.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .. import config, db
from ..timeutil import chart_time, hm, now_epoch, now_ist, session_bounds

log = logging.getLogger("expiry_playbook")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("expiry_playbook", {})


def is_expiry_day(
    cur_expiry: str,
    session_date: Optional[date] = None,
    force_expiry_mode: bool = False,
) -> bool:
    """
    Returns True if current session date matches the active expiry date,
    or if manual rehearsal/preview mode is forced via force_expiry_mode=True.
    """
    if force_expiry_mode:
        return True
    s_date = session_date or now_ist().date()
    return str(s_date) == str(cur_expiry)


def _normalize_rows(rows: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    return [dict(r) if hasattr(r, "keys") and not isinstance(r, dict) else r for r in rows]


def compute_straddle_telemetry(
    spot: float,
    chain_rows: List[Dict[str, Any]],
    open_chain_rows: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Calculates live ATM Straddle Premium (CE + PE), Implied Breakeven Box,
    and Theta Decay % from market open.
    """
    c = cfg or get_cfg()
    box_mult = float(c.get("straddle_tight_box_mult", 0.35))
    atm_strike = round(spot / 50.0) * 50.0

    c_rows = _normalize_rows(chain_rows)
    o_rows = _normalize_rows(open_chain_rows)

    ce_row = next((r for r in c_rows if float(r.get("strike", 0.0)) == atm_strike), None)
    pe_row = next((r for r in c_rows if float(r.get("strike", 0.0)) == atm_strike), None)

    atm_ce_ltp = float(ce_row.get("ce_ltp") or 0.0) if ce_row else 70.0
    atm_pe_ltp = float(pe_row.get("pe_ltp") or 0.0) if pe_row else 70.0
    atm_straddle_ltp = round(atm_ce_ltp + atm_pe_ltp, 1)

    upper_breakeven = round(atm_strike + atm_straddle_ltp, 1)
    lower_breakeven = round(atm_strike - atm_straddle_ltp, 1)

    pin_zone_high = round(atm_strike + atm_straddle_ltp * box_mult, 1)
    pin_zone_low = round(atm_strike - atm_straddle_ltp * box_mult, 1)

    # Track open straddle premium and intraday decay
    open_straddle_ltp = atm_straddle_ltp
    straddle_decay_pct = 0.0

    if o_rows:
        o_ce = next((r for r in o_rows if float(r.get("strike", 0.0)) == atm_strike), None)
        o_pe = next((r for r in o_rows if float(r.get("strike", 0.0)) == atm_strike), None)
        if o_ce and o_pe:
            open_ce_ltp = float(o_ce.get("ce_ltp") or 0.0)
            open_pe_ltp = float(o_pe.get("pe_ltp") or 0.0)
            open_straddle_ltp = round(open_ce_ltp + open_pe_ltp, 1)
            if open_straddle_ltp > 0:
                straddle_decay_pct = round(
                    ((open_straddle_ltp - atm_straddle_ltp) / open_straddle_ltp) * 100.0, 1
                )

    return {
        "atm_strike": atm_strike,
        "atm_ce_ltp": atm_ce_ltp,
        "atm_pe_ltp": atm_pe_ltp,
        "atm_straddle_ltp": atm_straddle_ltp,
        "open_straddle_ltp": open_straddle_ltp,
        "straddle_decay_pct": straddle_decay_pct,
        "upper_breakeven": upper_breakeven,
        "lower_breakeven": lower_breakeven,
        "pin_zone_high": pin_zone_high,
        "pin_zone_low": pin_zone_low,
    }


def detect_gamma_pin_or_squeeze(
    spot: float,
    atm_strike: float,
    atm_straddle_ltp: float,
    straddle_decay_pct: float,
    chain_rows: List[Dict[str, Any]],
    pe_ce_step_diff: int = 0,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Identifies the institutional Pin Strike and evaluates whether NIFTY
    is in an active Gamma Pin / Theta Crush or breaking out toward Breakeven.
    """
    c = cfg or get_cfg()
    pin_range_pts = float(c.get("pin_range_pts", 35.0))
    pin_step_diff_max = int(c.get("pin_step_diff_max", 1500000))
    pin_decay_min_pct = float(c.get("pin_decay_min_pct", 6.0))
    box_mult = float(c.get("straddle_tight_box_mult", 0.35))
    min_dual_oi = int(c.get("min_writer_fortify_oi", 1500000))

    c_rows = _normalize_rows(chain_rows)

    # Candidate strikes within Spot ± 150
    nearby_strikes = [
        r for r in c_rows if abs(float(r.get("strike", 0.0)) - spot) <= 150.0
    ]

    pin_strike = atm_strike
    max_combined_oi = 0.0

    for r in nearby_strikes:
        ce_oi = float(r.get("ce_oi") or 0.0)
        pe_oi = float(r.get("pe_oi") or 0.0)
        combined = ce_oi + pe_oi
        # Pin strike requires dual-sided participation
        if combined > max_combined_oi and ce_oi >= min_dual_oi and pe_oi >= min_dual_oi:
            max_combined_oi = combined
            pin_strike = float(r.get("strike", atm_strike))

    pin_zone_high = round(pin_strike + atm_straddle_ltp * box_mult, 1)
    pin_zone_low = round(pin_strike - atm_straddle_ltp * box_mult, 1)

    dist_to_pin = abs(spot - pin_strike)
    is_pinned = (
        dist_to_pin <= pin_range_pts
        and abs(pe_ce_step_diff) <= pin_step_diff_max
        and straddle_decay_pct >= pin_decay_min_pct
    )

    if is_pinned:
        state = "GAMMA_PIN_ACTIVE"
        label = f"🧲 GAMMA PIN LOCKED @ {pin_strike:,.0f}"
        text = (
            f"Theta Decay Zone ({pin_zone_low:,.0f}–{pin_zone_high:,.0f}) · "
            f"Straddle ₹{atm_straddle_ltp:.1f} (-{straddle_decay_pct:.1f}% Decay). "
            f"Avoid buying naked options inside pin box!"
        )
    elif spot > pin_zone_high and pe_ce_step_diff > 0:
        state = "UPPER_BREAKEVEN_EXPANSION"
        label = f"🚀 BREAKING UPPER PIN BOX (> {pin_zone_high:,.0f})"
        text = f"Spot escaping upper pin box toward Upper Breakeven {atm_strike + atm_straddle_ltp:,.0f}."
    elif spot < pin_zone_low and pe_ce_step_diff < 0:
        state = "LOWER_BREAKEVEN_EXPANSION"
        label = f"🩸 BREAKING LOWER PIN BOX (< {pin_zone_low:,.0f})"
        text = f"Spot escaping lower pin box toward Lower Breakeven {atm_strike - atm_straddle_ltp:,.0f}."
    else:
        state = "UNPINNED_BALANCED"
        label = "🔓 UNPINNED (Open Trading Range)"
        text = f"Spot trading between {pin_zone_low:,.0f} and {pin_zone_high:,.0f} without pin lock."

    return {
        "pin_state": state,
        "pin_label": label,
        "pin_text": text,
        "pin_strike": pin_strike,
        "pin_zone_high": pin_zone_high,
        "pin_zone_low": pin_zone_low,
        "dist_to_pin": round(dist_to_pin, 1),
        "is_pinned": is_pinned,
    }


def _oi_map(rows: Optional[List[Dict[str, Any]]]) -> Dict[float, Dict[str, float]]:
    """Builds {strike: {ce_oi, pe_oi}} for ΔOI arithmetic."""
    out: Dict[float, Dict[str, float]] = {}
    for r in _normalize_rows(rows):
        try:
            k = float(r.get("strike", 0.0))
        except (TypeError, ValueError):
            continue
        out[k] = {
            "ce_oi": float(r.get("ce_oi") or 0.0),
            "pe_oi": float(r.get("pe_oi") or 0.0),
        }
    return out


def detect_trapped_writers(
    spot: float,
    chain_rows: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    prev_chain_rows: Optional[List[Dict[str, Any]]] = None,
    open_chain_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Scans option chain strikes around spot to identify underwater option writers
    forced into short-covering or delta-hedging.

    A writer is only reported as COVERING when real interval ΔOI (vs prev_chain_rows)
    clears ``min_writer_unwind_oi``. Without ΔOI inputs the function degrades to a
    STANDING-wall read and says so, rather than implying a squeeze that was never measured.
    """
    c = cfg or get_cfg()
    min_unwind_oi = int(c.get("min_writer_unwind_oi", 1000000))
    min_fortify_oi = int(c.get("min_writer_fortify_oi", 1500000))

    c_rows = _normalize_rows(chain_rows)
    prev_map = _oi_map(prev_chain_rows)
    open_map = _oi_map(open_chain_rows)
    has_delta = bool(prev_map) or bool(open_map)

    trapped_side = "BALANCED"
    trapped_strike = round(spot / 50.0) * 50.0
    unwinding_str = "No major writer evacuation"
    fortified_str = "Walls balanced"
    is_covering = False
    covering_oi = 0.0
    day_chg_oi = 0.0

    def _chg(k: float, leg: str) -> Tuple[float, float]:
        """Returns (step_chg, day_chg) for a leg at strike k; 0.0 when unavailable."""
        cur_row = next((r for r in c_rows if float(r.get("strike", 0.0)) == k), None)
        cur_oi = float(cur_row.get(leg) or 0.0) if cur_row else 0.0
        step = cur_oi - prev_map[k][leg] if k in prev_map else 0.0
        day = cur_oi - open_map[k][leg] if k in open_map else 0.0
        return step, day

    # Scan nearby strikes within spot ± 150
    nearby = [r for r in c_rows if abs(float(r.get("strike", 0.0)) - spot) <= 150.0]

    # Call writers are underwater when spot has traded ABOVE their strike
    call_trapped = [
        r for r in nearby
        if float(r.get("strike", 0.0)) < spot
        and float(r.get("ce_oi") or 0.0) >= min_fortify_oi
    ]
    # Put writers are underwater when spot has traded BELOW their strike
    put_trapped = [
        r for r in nearby
        if float(r.get("strike", 0.0)) > spot
        and float(r.get("pe_oi") or 0.0) >= min_fortify_oi
    ]

    def _rank(rows: List[Dict[str, Any]], leg: str) -> Optional[Dict[str, Any]]:
        """Prefer the strike covering fastest; fall back to the largest standing wall."""
        if not rows:
            return None
        if has_delta:
            by_unwind = sorted(rows, key=lambda r: _chg(float(r["strike"]), leg)[0])
            top = by_unwind[0]
            if _chg(float(top["strike"]), leg)[0] <= -min_unwind_oi:
                return top
        return max(rows, key=lambda r: float(r.get(leg) or 0.0))

    best_call = _rank(call_trapped, "ce_oi")
    best_put = _rank(put_trapped, "pe_oi")

    # Whichever side is actually evacuating faster owns the verdict
    call_step = _chg(float(best_call["strike"]), "ce_oi")[0] if best_call else 0.0
    put_step = _chg(float(best_put["strike"]), "pe_oi")[0] if best_put else 0.0

    pick_calls = best_call is not None and (best_put is None or call_step <= put_step)

    if pick_calls and best_call is not None:
        k = float(best_call["strike"])
        ce_oi = float(best_call.get("ce_oi") or 0.0)
        step, day = _chg(k, "ce_oi")
        trapped_side = "CALLS"
        trapped_strike = k
        covering_oi, day_chg_oi = step, day
        is_covering = has_delta and step <= -min_unwind_oi
        if is_covering:
            unwinding_str = (
                f"🔥 {k:,.0f} CE Writers Trapped & COVERING "
                f"({ce_oi/1e5:.1f}L standing · {step/1e5:+.1f}L this interval)"
            )
        elif has_delta:
            unwinding_str = (
                f"⚠️ {k:,.0f} CE Writers Trapped — STANDING, not covering "
                f"({ce_oi/1e5:.1f}L standing · {step/1e5:+.1f}L this interval)"
            )
        else:
            unwinding_str = f"⚠️ {k:,.0f} CE Writers Trapped ({ce_oi/1e5:.1f}L standing · ΔOI unavailable)"

    elif best_put is not None:
        k = float(best_put["strike"])
        pe_oi = float(best_put.get("pe_oi") or 0.0)
        step, day = _chg(k, "pe_oi")
        trapped_side = "PUTS"
        trapped_strike = k
        covering_oi, day_chg_oi = step, day
        is_covering = has_delta and step <= -min_unwind_oi
        if is_covering:
            unwinding_str = (
                f"🩸 {k:,.0f} PE Writers Trapped & COVERING "
                f"({pe_oi/1e5:.1f}L standing · {step/1e5:+.1f}L this interval)"
            )
        elif has_delta:
            unwinding_str = (
                f"⚠️ {k:,.0f} PE Writers Trapped — STANDING, not covering "
                f"({pe_oi/1e5:.1f}L standing · {step/1e5:+.1f}L this interval)"
            )
        else:
            unwinding_str = f"⚠️ {k:,.0f} PE Writers Trapped ({pe_oi/1e5:.1f}L standing · ΔOI unavailable)"

    # Real walls from the chain, never strike ± 50 arithmetic
    floor_rows = [r for r in c_rows if float(r.get("strike", 0.0)) < spot and float(r.get("pe_oi") or 0.0) > 0]
    ceil_rows = [r for r in c_rows if float(r.get("strike", 0.0)) > spot and float(r.get("ce_oi") or 0.0) > 0]
    floor_row = max(floor_rows, key=lambda r: float(r.get("pe_oi") or 0.0)) if floor_rows else None
    ceil_row = max(ceil_rows, key=lambda r: float(r.get("ce_oi") or 0.0)) if ceil_rows else None

    put_floor_strike = float(floor_row["strike"]) if floor_row else 0.0
    call_ceiling_strike = float(ceil_row["strike"]) if ceil_row else 0.0

    if floor_row and ceil_row:
        fortified_str = (
            f"🛡️ Put Floor {put_floor_strike:,.0f} ({float(floor_row.get('pe_oi') or 0.0)/1e7:.2f}Cr) · "
            f"🧱 Call Ceiling {call_ceiling_strike:,.0f} ({float(ceil_row.get('ce_oi') or 0.0)/1e7:.2f}Cr)"
        )
    elif floor_row:
        fortified_str = f"🛡️ Put Floor {put_floor_strike:,.0f} ({float(floor_row.get('pe_oi') or 0.0)/1e7:.2f}Cr)"
    elif ceil_row:
        fortified_str = f"🧱 Call Ceiling {call_ceiling_strike:,.0f} ({float(ceil_row.get('ce_oi') or 0.0)/1e7:.2f}Cr)"

    return {
        "trapped_side": trapped_side,
        "trapped_strike": trapped_strike,
        "unwinding_str": unwinding_str,
        "fortified_str": fortified_str,
        "is_covering": is_covering,
        "delta_available": has_delta,
        "covering_step_oi": int(round(covering_oi)),
        "covering_day_oi": int(round(day_chg_oi)),
        "put_floor_strike": put_floor_strike,
        "call_ceiling_strike": call_ceiling_strike,
    }


def evaluate_hero_zero_setup(
    spot: float,
    fut_ltp: float,
    vwap: float,
    swing_high_30m: float,
    swing_low_30m: float,
    slot_rvol: float,
    c_rvol: float,
    chain_rows: List[Dict[str, Any]],
    pe_ce_step_diff: int,
    pe_ce_day_diff: int,
    time_label: str,
    expiry: str = "cur",
    force_expiry_mode: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    5-Factor Institutional Zero-DTE Hero-Zero Sniper Engine.
    Evaluates optimal strike (₹10–₹55 sweet spot), strict -35% premium SL,
    and 2.0x / 3.5x target ladder during the prime 12:45–14:50 IST window.
    """
    c = cfg or get_cfg()
    start_hhmm = str(c.get("hero_zero_start_hhmm", "12:45"))
    end_hhmm = str(c.get("hero_zero_end_hhmm", "14:50"))
    settle_lock = str(c.get("settlement_lock_hhmm", "15:05"))
    min_slot_rvol = float(c.get("min_slot_rvol", 1.4))
    min_c_rvol = float(c.get("min_c_rvol", 1.15))
    min_step_diff = int(c.get("min_pe_ce_day_diff", 15000000))
    sl_pct = float(c.get("premium_sl_pct", 0.35))
    tp1_mult = float(c.get("tp1_mult", 2.0))
    tp2_mult = float(c.get("tp2_mult", 3.5))
    idx_sl_pts = float(c.get("index_sl_pts", 22.0))

    # Hard 15:05+ IST settlement lock
    if time_label >= settle_lock and time_label != "Prev Day":
        return {
            "state": "EXPIRY_SETTLEMENT_LOCK",
            "score": 0,
            "checklist": {
                "window": False,
                "writer_squeeze": False,
                "breakout": False,
                "rvol": False,
                "sweet_spot": False,
            },
            "contract": "None",
            "badge": "🏁 15:05+ SETTLEMENT LOCK",
            "message": "NSE 15:00–15:30 IST Settlement VWAP averaging active. Close all Zero-DTE positions (OTM goes to ₹0.05).",
            "entry_ltp": 0.0,
            "sl_ltp": 0.0,
            "tp1_ltp": 0.0,
            "tp2_ltp": 0.0,
            "index_sl": 0.0,
        }

    # Factor 1: Window
    window_ok = (start_hhmm <= time_label <= end_hhmm) or force_expiry_mode

    # Factor 2: Writer Squeeze & Flow
    bull_flow = pe_ce_day_diff > 500000 and pe_ce_step_diff >= 150000
    bear_flow = pe_ce_day_diff < -500000 and pe_ce_step_diff <= -150000
    writer_squeeze_ok = bull_flow or bear_flow

    # Factor 3: Breakout & VWAP
    bull_break = spot >= (vwap - 5.0) and spot >= (swing_high_30m - 10.0)
    bear_break = spot <= (vwap + 5.0) and spot <= (swing_low_30m + 10.0)
    breakout_ok = (bull_flow and bull_break) or (bear_flow and bear_break)

    # Factor 4: RVOL — never auto-passed by rehearsal mode; an unmeasured RVOL is a FAIL,
    # otherwise force_expiry_mode silently gifts the ticket a point it never earned.
    rvol_measured = (slot_rvol or 0.0) > 0.0 or (c_rvol or 0.0) > 0.0
    rvol_ok = rvol_measured and ((slot_rvol >= min_slot_rvol) or (c_rvol >= min_c_rvol))

    # Factor 5: Strike & Premium Sweet Spot (from JSON rules)
    min_prem = float(c.get("sweet_spot_min_premium", 10.0))
    max_prem = float(c.get("sweet_spot_max_premium", 75.0))
    atm_strike = round(spot / 50.0) * 50.0
    candidate_strikes = [atm_strike, atm_strike + 50.0, atm_strike - 50.0]

    chosen_strike = atm_strike
    opt_type = "CE" if pe_ce_day_diff >= 0 else "PE"
    entry_ltp = 40.0

    c_rows = _normalize_rows(chain_rows)
    chain_map = {float(r.get("strike", 0.0)): r for r in c_rows}

    # Pick strike in sweet spot
    for k in candidate_strikes:
        if k in chain_map:
            ltp = float(chain_map[k].get("ce_ltp" if opt_type == "CE" else "pe_ltp") or 0.0)
            if min_prem <= ltp <= max_prem:
                chosen_strike = k
                entry_ltp = ltp
                break
    else:
        # Fallback to ATM
        if atm_strike in chain_map:
            entry_ltp = float(chain_map[atm_strike].get("ce_ltp" if opt_type == "CE" else "pe_ltp") or 40.0)

    sweet_spot_ok = min_prem <= entry_ltp <= max_prem

    checklist = {
        "window": window_ok,
        "writer_squeeze": writer_squeeze_ok,
        "breakout": breakout_ok,
        "rvol": rvol_ok,
        "sweet_spot": sweet_spot_ok,
    }
    score = sum(1 for v in checklist.values() if v)

    sl_ltp = round(max(entry_ltp * (1.0 - sl_pct), 0.5), 1)
    tp1_ltp = round(entry_ltp * tp1_mult, 1)
    tp2_ltp = round(entry_ltp * tp2_mult, 1)
    index_sl = round(spot - idx_sl_pts if opt_type == "CE" else spot + idx_sl_pts, 1)

    contract = f"NIFTY {chosen_strike:,.0f} {opt_type}"

    if score >= 4:
        state = f"ACTIVE_HERO_ZERO_BUY_{opt_type}"
        badge = f"🚀 0-DTE HERO-ZERO ({score}/5 A+)"
        msg = (
            f"BUY {contract} @ ₹{entry_ltp:.1f} · Prem SL ₹{sl_ltp:.1f} (-35%) "
            f"→ T1 ₹{tp1_ltp:.1f} (2.0x) / T2 ₹{tp2_ltp:.1f} (3.5x) · Index SL {index_sl:,.1f}"
        )
    else:
        state = "HERO_ZERO_STANDBY"
        badge = f"⏳ HERO-ZERO STANDBY ({score}/5)"
        msg = "Waiting for Trapped Writer Capitulation + 12:45–14:50 IST Breakout Window."

    return {
        "state": state,
        "score": score,
        "checklist": checklist,
        "contract": contract,
        "opt_type": opt_type,
        "chosen_strike": chosen_strike,
        "badge": badge,
        "message": msg,
        "entry_ltp": entry_ltp,
        "sl_ltp": sl_ltp,
        "tp1_ltp": tp1_ltp,
        "tp2_ltp": tp2_ltp,
        "index_sl": index_sl,
    }


def compute_settlement_vwap(
    spot_candles_1m_1500_1530: List[Dict[str, Any]],
    atm_strike: float,
) -> Dict[str, Any]:
    """
    On Expiry Day after 15:00 IST, calculates the running mean of 1m NIFTY Spot closes
    from 15:00 to current bar to project the official NSE Settlement Price.
    """
    if not spot_candles_1m_1500_1530:
        return {
            "active": False,
            "samples": 0,
            "estimated_settlement_price": round(atm_strike, 1),
            "itm_ce_strikes": [],
            "itm_pe_strikes": [],
            "message": "Awaiting 15:00–15:30 IST settlement window.",
        }

    closes = [float(c.get("c") or 0.0) for c in spot_candles_1m_1500_1530 if float(c.get("c") or 0.0) > 0]
    if not closes:
        return {
            "active": False,
            "samples": 0,
            "estimated_settlement_price": round(atm_strike, 1),
            "itm_ce_strikes": [],
            "itm_pe_strikes": [],
            "message": "Awaiting valid 15:00+ closes.",
        }

    est_settlement = round(sum(closes) / len(closes), 2)
    nearby_strikes = [atm_strike - 100, atm_strike - 50, atm_strike, atm_strike + 50, atm_strike + 100]

    itm_ce = [k for k in nearby_strikes if est_settlement > k]
    itm_pe = [k for k in nearby_strikes if est_settlement < k]

    return {
        "active": True,
        "samples": len(closes),
        "estimated_settlement_price": est_settlement,
        "itm_ce_strikes": itm_ce,
        "itm_pe_strikes": itm_pe,
        "message": f"Running 15:00–15:30 IST Settlement Estimate: {est_settlement:,.2f} ({len(closes)}/30 1m samples).",
    }


def compute_expiry_cockpit_telemetry(
    expiry: Optional[str] = None,
    force_expiry_mode: bool = False,
    tf: str = "3m",
    cfg: Optional[Dict[str, Any]] = None,
    pe_ce_step_diff: Optional[int] = None,
    pe_ce_day_diff: Optional[int] = None,
    slot_rvol: Optional[float] = None,
    c_rvol: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Master function: Computes the complete Expiry Zero-DTE Cockpit telemetry payload
    for API response and frontend cockpit strip.

    PE-CE flow is taken from the caller when supplied (pe_ce_matrix passes its own
    ledger values so the cockpit and the table can never disagree); otherwise it is
    derived here from the session's own chain_snap rows using the identical
    open-baseline method. RVOL has no chain-side derivation: when the caller cannot
    supply it, the RVOL gate fails honestly instead of being stubbed to a pass.
    """
    c = cfg or get_cfg()
    today_dt = now_ist().date()
    today_start, today_end = session_bounds(today_dt)
    today_iso = today_dt.isoformat()

    # Determine active expiry
    target_exp = expiry or "2026-10-27"
    if target_exp in ("cur", "This wk", "This Wk"):
        exp_rows = db.query(
            "SELECT DISTINCT expiry FROM chain_snap WHERE expiry >= ? AND ts >= ? ORDER BY expiry ASC",
            (today_iso, today_start),
        )
        target_exp = exp_rows[0]["expiry"] if exp_rows else "2026-10-27"

    is_exp = is_expiry_day(target_exp, today_dt, force_expiry_mode=force_expiry_mode)

    # 1. Fetch latest and open chain snapshots
    latest_snap = db.query("""
        SELECT ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp
        FROM chain_snap
        WHERE expiry = ? AND ts >= ? AND ts <= ?
        ORDER BY ts DESC, strike ASC
    """, (target_exp, today_start, today_end))

    if not latest_snap:
        # Fallback to single latest session in database
        latest_t_row = db.query("SELECT ts FROM chain_snap WHERE expiry = ? ORDER BY ts DESC LIMIT 1", (target_exp,))
        if latest_t_row:
            s_dt = datetime.fromtimestamp(latest_t_row[0]["ts"], tz=ZoneInfo("Asia/Kolkata")).date()
            s_start, s_end = session_bounds(s_dt)
            latest_snap = db.query("""
                SELECT ts, strike, ce_oi, pe_oi, ce_ltp, pe_ltp
                FROM chain_snap
                WHERE expiry = ? AND ts >= ? AND ts <= ?
                ORDER BY ts DESC, strike ASC
            """, (target_exp, s_start, s_end))
            today_start, today_end = s_start, s_end

    if not latest_snap:
        return {
            "ok": False,
            "message": "No snapshot data available for expiry playbook.",
            "is_expiry_day": is_exp,
            "expiry": target_exp,
        }

    # Group latest snapshot by strike
    latest_ts = latest_snap[0]["ts"]
    current_chain = [dict(r) for r in latest_snap if r["ts"] == latest_ts]

    # Group open snapshot (earliest of the session)
    open_ts = min(r["ts"] for r in latest_snap)
    open_chain = [dict(r) for r in latest_snap if r["ts"] == open_ts]

    # Previous snapshot — the interval (step) basis for ΔOI
    prior_ts_all = sorted({int(r["ts"]) for r in latest_snap if int(r["ts"]) < int(latest_ts)})
    prev_ts = prior_ts_all[-1] if prior_ts_all else None
    prev_chain = [dict(r) for r in latest_snap if r["ts"] == prev_ts] if prev_ts else []

    # Derive PE-CE day & step flow with the same open-baseline arithmetic as pe_ce_matrix:
    #   day  = (ΣPE_now - ΣPE_open) - (ΣCE_now - ΣCE_open)
    #   step = (ΣPE_now - ΣPE_prev) - (ΣCE_now - ΣCE_prev)
    def _sum_leg(rows: List[Dict[str, Any]], leg: str) -> float:
        return sum(float(r.get(leg) or 0.0) for r in rows)

    derived_day_diff = 0
    derived_step_diff = 0
    flow_source = "unavailable"

    if open_chain:
        day_pe = _sum_leg(current_chain, "pe_oi") - _sum_leg(open_chain, "pe_oi")
        day_ce = _sum_leg(current_chain, "ce_oi") - _sum_leg(open_chain, "ce_oi")
        derived_day_diff = int(round(day_pe - day_ce))
        flow_source = "derived_from_chain_snap"
    if prev_chain:
        step_pe = _sum_leg(current_chain, "pe_oi") - _sum_leg(prev_chain, "pe_oi")
        step_ce = _sum_leg(current_chain, "ce_oi") - _sum_leg(prev_chain, "ce_oi")
        derived_step_diff = int(round(step_pe - step_ce))

    live_day_diff = int(pe_ce_day_diff) if pe_ce_day_diff is not None else derived_day_diff
    live_step_diff = int(pe_ce_step_diff) if pe_ce_step_diff is not None else derived_step_diff
    if pe_ce_day_diff is not None or pe_ce_step_diff is not None:
        flow_source = "caller_ledger"

    live_slot_rvol = float(slot_rvol) if slot_rvol is not None else 0.0
    live_c_rvol = float(c_rvol) if c_rvol is not None else 0.0
    rvol_available = live_slot_rvol > 0.0 or live_c_rvol > 0.0

    # 2. Futures and Spot
    fut_candles = db.candles_1m("FUT", today_start)
    if not fut_candles:
        fut_candles = db.candles_1m("13", today_start)

    latest_fut = float(fut_candles[-1]["c"]) if fut_candles else 22750.0
    spot = latest_fut

    # Calculate session VWAP
    tot_vol = sum(float(c.get("v") or 0.0) for c in fut_candles)
    tot_pv = sum(float(c.get("c") or 0.0) * float(c.get("v") or 0.0) for c in fut_candles)
    vwap = round(tot_pv / max(tot_vol, 1.0), 1) if tot_vol > 0 else spot

    # 30m high / low
    recent_30 = fut_candles[-30:] if len(fut_candles) >= 30 else fut_candles
    swing_high_30m = max((float(c.get("h") or c.get("c")) for c in recent_30), default=spot + 20.0)
    swing_low_30m = min((float(c.get("l") or c.get("c")) for c in recent_30), default=spot - 20.0)

    # 3. Straddle Telemetry
    straddle_data = compute_straddle_telemetry(spot, current_chain, open_chain, c)
    atm_strike = straddle_data["atm_strike"]

    # 4. Pin vs Squeeze Detector
    pin_data = detect_gamma_pin_or_squeeze(
        spot,
        atm_strike,
        straddle_data["atm_straddle_ltp"],
        straddle_data["straddle_decay_pct"],
        current_chain,
        pe_ce_step_diff=live_step_diff,
        cfg=c,
    )

    # 5. Trapped Writers — with real interval & day ΔOI, not standing OI alone
    trapped_data = detect_trapped_writers(
        spot,
        current_chain,
        c,
        prev_chain_rows=prev_chain,
        open_chain_rows=open_chain,
    )

    # 6. Hero-Zero Setup
    time_label = hm(latest_ts)
    hero_zero_data = evaluate_hero_zero_setup(
        spot=spot,
        fut_ltp=latest_fut,
        vwap=vwap,
        swing_high_30m=swing_high_30m,
        swing_low_30m=swing_low_30m,
        slot_rvol=live_slot_rvol,
        c_rvol=live_c_rvol,
        chain_rows=current_chain,
        pe_ce_step_diff=live_step_diff,
        pe_ce_day_diff=live_day_diff,
        time_label=time_label,
        expiry=target_exp,
        force_expiry_mode=force_expiry_mode,
        cfg=c,
    )

    # 7. Settlement VWAP (if >= 15:00 IST)
    settle_candles = [c for c in fut_candles if hm(int(c.get("ts", 0))) >= "15:00"]
    settle_data = compute_settlement_vwap(settle_candles, atm_strike)

    return {
        "ok": True,
        "is_expiry_day": is_exp,
        "force_expiry_mode": force_expiry_mode,
        "expiry": target_exp,
        "time_label": time_label,
        "spot": spot,
        "vwap": vwap,
        "straddle": straddle_data,
        "pin": pin_data,
        "trapped": trapped_data,
        "hero_zero": hero_zero_data,
        "settlement": settle_data,
        "flow": {
            "pe_ce_day_diff": live_day_diff,
            "pe_ce_step_diff": live_step_diff,
            "source": flow_source,
            "slot_rvol": live_slot_rvol,
            "c_rvol": live_c_rvol,
            "rvol_available": rvol_available,
            "snapshots_in_session": len({int(r["ts"]) for r in latest_snap}),
        },
    }
