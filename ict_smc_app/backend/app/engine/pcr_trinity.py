"""
engine/pcr_trinity.py — 3-Chart Institutional PCR & Open Interest Positioning Suite Engine.

Surpasses commercial tools like StockMojo with:
  1. Live Put-Call Ratio (PCR-OI + PCR-Vol vs NIFTY Future)
  2. Intraday Fresh Change in OI (Fresh PE ΔOI vs Fresh CE ΔOI vs NIFTY Future)
  3. Cumulative Total OI (Total PE OI vs Total CE OI vs NIFTY Future)
  4. 3-Chart Institutional Alignment Engine & Checkpoint Evaluator
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from .. import config
from ..timeutil import chart_time, hm, now_epoch, now_ist, session_bounds

log = logging.getLogger("pcr_trinity")

OptionExpiryMode = Literal["cur", "next", "both"]
FuturesContractMode = Literal["near", "next", "combined"]


def resolve_futures_series(
    oct_candles: List[Dict[str, Any]],
    nov_candles: List[Dict[str, Any]],
    fut_mode: str = "combined",
) -> Dict[int, Dict[str, Any]]:
    """
    Builds a timestamp-indexed map of Futures Close, Volume, and OI
    for Near Month (Oct), Next Month (Nov), or Combined (Oct + Nov).
    """
    oct_map = {int(c.get("time") if "time" in c else c.get("ts", 0)): c for c in (oct_candles or [])}
    nov_map = {int(c.get("time") if "time" in c else c.get("ts", 0)): c for c in (nov_candles or [])}
    all_times = sorted(set(oct_map.keys()) | set(nov_map.keys()))

    out: Dict[int, Dict[str, Any]] = {}
    for t in all_times:
        o = oct_map.get(t)
        n = nov_map.get(t)
        if fut_mode == "near" or (n is None and o is not None):
            close_px = float(o.get("close") if "close" in o else o.get("c", 0.0))
            out[t] = {
                "time": t,
                "ts": t,
                "close": close_px,
                "c": close_px,
                "open": float(o.get("open", o.get("o", close_px))),
                "o": float(o.get("open", o.get("o", close_px))),
                "high": float(o.get("high", o.get("h", close_px))),
                "h": float(o.get("high", o.get("h", close_px))),
                "low": float(o.get("low", o.get("l", close_px))),
                "l": float(o.get("low", o.get("l", close_px))),
                "volume": int(o.get("volume", o.get("v", 0))),
                "v": int(o.get("volume", o.get("v", 0))),
                "oi": int(o.get("oi", 0)),
                "oi_chg": int(o.get("oi_chg", 0)),
                "contract_label": "NIFTY-Oct2026-FUT (Near)",
            }
        elif fut_mode == "next" and n is not None:
            close_px = float(n.get("close") if "close" in n else n.get("c", 0.0))
            out[t] = {
                "time": t,
                "ts": t,
                "close": close_px,
                "c": close_px,
                "open": float(n.get("open", n.get("o", close_px))),
                "o": float(n.get("open", n.get("o", close_px))),
                "high": float(n.get("high", n.get("h", close_px))),
                "h": float(n.get("high", n.get("h", close_px))),
                "low": float(n.get("low", n.get("l", close_px))),
                "l": float(n.get("low", n.get("l", close_px))),
                "volume": int(n.get("volume", n.get("v", 0))),
                "v": int(n.get("volume", n.get("v", 0))),
                "oi": int(n.get("oi", 0)),
                "oi_chg": int(n.get("oi_chg", 0)),
                "contract_label": "NIFTY-Nov2026-FUT (Next)",
            }
        else:
            # Combined Mode: Near-Month price (Oct) to prevent artificial carry premium spikes (+95 to +180 pts),
            # while combining Volume and Open Interest across both Oct + Nov contracts.
            o_vol = int(o.get("volume", o.get("v", 0))) if o else 0
            n_vol = int(n.get("volume", n.get("v", 0))) if n else 0
            tot_vol = o_vol + n_vol
            o_close = float(o.get("close") if "close" in o else o.get("c", 0.0)) if o else (float(n.get("close") if "close" in n else n.get("c", 0.0)) if n else 0.0)
            n_close = float(n.get("close") if "close" in n else n.get("c", 0.0)) if n else o_close
            close_px = o_close if o else n_close
            open_px = float(o.get("open", o.get("o", close_px))) if o else float(n.get("open", n.get("o", close_px)))
            high_px = float(o.get("high", o.get("h", close_px))) if o else float(n.get("high", n.get("h", close_px)))
            low_px = float(o.get("low", o.get("l", close_px))) if o else float(n.get("low", n.get("l", close_px)))
            o_oi = int(o.get("oi", 0)) if o else 0
            n_oi = int(n.get("oi", 0)) if n else 0
            o_oi_chg = int(o.get("oi_chg", 0)) if o else 0
            n_oi_chg = int(n.get("oi_chg", 0)) if n else 0
            basis_spread = round(n_close - o_close, 1) if (o and n) else 0.0
            out[t] = {
                "time": t,
                "ts": t,
                "close": close_px,
                "c": close_px,
                "open": open_px,
                "o": open_px,
                "high": high_px,
                "h": high_px,
                "low": low_px,
                "l": low_px,
                "volume": tot_vol,
                "v": tot_vol,
                "oct_volume": o_vol,
                "nov_volume": n_vol,
                "oct_close": o_close,
                "nov_close": n_close,
                "fut_basis_spread": basis_spread,
                "rollover_pct": round((n_vol / max(tot_vol, 1)) * 100, 1),
                "oi": o_oi + n_oi,
                "oi_chg": o_oi_chg + n_oi_chg,
                "contract_label": "NIFTY Oct+Nov FUT (Combined)",
            }
    return out


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("pcr_trinity", {})


def classify_pcr_band(pcr_oi: float, cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    Classifies Put-Call Ratio into 5 institutional regime bands:
      - EXTREME_BULL_CONTRARIAN_RISK (> 1.50)
      - BULLISH_PUT_CONTROL (1.20 - 1.50)
      - NEUTRAL_BALANCED (0.80 - 1.20) [Core 0.85 - 1.10]
      - BEARISH_CALL_CONTROL (0.50 - 0.80)
      - EXTREME_BEAR_CONTRARIAN_BOUNCE (< 0.50)
    """
    c = cfg or get_cfg()
    ext_bull = float(c.get("extreme_bull_pcr", 1.50))
    bull = float(c.get("bull_pcr", 1.20))
    bear = float(c.get("bear_pcr", 0.80))
    ext_bear = float(c.get("extreme_bear_pcr", 0.50))
    core_high = float(c.get("neutral_core_high", 1.10))
    core_low = float(c.get("neutral_core_low", 0.85))

    if pcr_oi > ext_bull:
        return "EXTREME_BULL_CONTRARIAN_RISK", f"Extreme Bullish Overbought (> {ext_bull:.2f}) · Contrarian Pullback Risk"
    if pcr_oi > bull:
        return "BULLISH_PUT_CONTROL", f"Bullish Put Control ({bull:.2f}–{ext_bull:.2f}) · Writers Defending Floor"
    if pcr_oi >= bear:
        if core_low <= pcr_oi <= core_high:
            return "NEUTRAL_BALANCED", f"Neutral Balanced ({core_low:.2f}–{core_high:.2f} Core Equilibrium)"
        return "NEUTRAL_BALANCED", f"Neutral Balanced ({bear:.2f}–{bull:.2f} Zone)"
    if pcr_oi >= ext_bear:
        return "BEARISH_CALL_CONTROL", f"Bearish Call Control ({ext_bear:.2f}–{bear:.2f}) · Writers Capping Ceiling"
    return "EXTREME_BEAR_CONTRARIAN_BOUNCE", f"Extreme Bearish Capitulation (< {ext_bear:.2f}) · Contrarian Bounce Risk"


def compute_checkpoints(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Computes fixed institutional checkpoint snapshots across today's session:
      - 09:45 Open Settle
      - 12:30 Midday
      - 14:30 Final Hour
      - Current
    If a checkpoint time has not been reached yet in today's session, returns status="PENDING"
    with None values to prevent premature future checkpoint display.
    """
    if not series:
        return []

    first_pt = series[0]
    last_pt = series[-1]
    open_pcr = float(first_pt.get("pcr_oi") or 1.0)
    latest_series_time = int(last_pt["time"])

    # Convert checkpoint HH:MM to target timestamps in chart_time space
    now_dt = now_ist()
    base_date = now_dt.date()

    check_specs = [
        ("09:45", "09:45 Open Settle", 9, 45),
        ("12:30", "12:30 Midday", 12, 30),
        ("14:30", "14:30 Final Hour", 14, 30),
        ("Current", "Current Session", now_dt.hour, now_dt.minute),
    ]

    out: List[Dict[str, Any]] = []

    # Get session bounds
    sess_open, sess_close = session_bounds(base_date)

    for key, label, target_h, target_m in check_specs:
        target_minutes = target_h * 60 + target_m
        target_epoch = sess_open + ((target_h - 9) * 3600 + (target_m - 15) * 60)
        target_chart_t = chart_time(target_epoch)

        if key == "Current":
            pt = last_pt
            available = True
            status = "COMPLETED"
        else:
            # Check if this checkpoint is in the future relative to latest session data
            if latest_series_time < target_chart_t - 180:
                available = False
                status = "PENDING"
                pt = None
            else:
                # Point at or closest before target_chart_t
                candidates = [p for p in series if p["time"] <= target_chart_t]
                if candidates and (target_chart_t - candidates[-1]["time"] <= 1800):
                    pt = candidates[-1]
                    available = True
                    status = "COMPLETED"
                else:
                    available = False
                    status = "UNAVAILABLE"
                    pt = None

        if status == "PENDING":
            out.append({
                "key": key,
                "label": label,
                "time_str": f"{target_h:02d}:{target_m:02d}",
                "available": False,
                "status": "PENDING",
                "pcr_oi": None,
                "pcr_vol": None,
                "fut_close": None,
                "fut_oi": None,
                "fut_oi_chg": None,
                "net_oi_chg": None,
                "total_oi_gap": None,
                "delta_from_open": None,
                "direction": "—",
                "shift_text": f"{label}: Pending (—)",
            })
        elif status == "UNAVAILABLE" or pt is None:
            out.append({
                "key": key,
                "label": label,
                "time_str": f"{target_h:02d}:{target_m:02d}",
                "available": False,
                "status": "UNAVAILABLE",
                "pcr_oi": None,
                "pcr_vol": None,
                "fut_close": None,
                "fut_oi": None,
                "fut_oi_chg": None,
                "net_oi_chg": None,
                "total_oi_gap": None,
                "delta_from_open": None,
                "direction": "—",
                "shift_text": f"{label}: Unavailable",
            })
        else:
            pcr = float(pt.get("pcr_oi") or 1.0)
            delta_open = round(pcr - open_pcr, 3)
            dir_symbol = "↑" if delta_open > 0.01 else "↓" if delta_open < -0.01 else "→"
            dir_text = "Rising Shift" if delta_open > 0.01 else "Falling Shift" if delta_open < -0.01 else "Stable"
            shift_str = f"PCR: {pcr:.3f} ({'+' if delta_open > 0 else ''}{delta_open:.2f} {dir_symbol} from 09:15) · {dir_text}"

            out.append({
                "key": key,
                "label": label,
                "time_str": f"{target_h:02d}:{target_m:02d}" if key != "Current" else hm(last_pt["time"] - 19800),
                "available": True,
                "status": "COMPLETED",
                "pcr_oi": pcr,
                "pcr_vol": float(pt.get("pcr_vol") or 1.0),
                "fut_close": float(pt.get("fut_close") or 0.0),
                "fut_oi": int(pt.get("fut_oi") or 0),
                "fut_oi_chg": int(pt.get("fut_oi_chg") or 0),
                "net_oi_chg": int(pt.get("net_oi_chg") or 0),
                "total_oi_gap": int(pt.get("total_oi_gap") or 0),
                "delta_from_open": delta_open,
                "direction": dir_symbol,
                "shift_text": shift_str,
            })

    return out


def compute_pcr_trinity_verdict(series: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Evaluates the 3-Chart Institutional Trinity Agreement:
      1. Put-Call Ratio direction & slope
      2. Fresh Change in OI dominance (Put Δ vs Call Δ)
      3. Cumulative Total OI Gap widening/narrowing

    Outputs:
      - BULLISH_TRINITY_LOCK (3/3 Agree)
      - BEARISH_TRINITY_LOCK (3/3 Agree)
      - PRICE_PCR_DIVERGENCE (Bearish Trap / Bullish Absorption)
      - MIXED_WAIT (Charts Disagree)
    """
    c = cfg or get_cfg()
    slope_window = int(c.get("slope_window_mins", 30))
    slope_min_delta = float(c.get("slope_min_delta", 0.01))
    fut_div_pts = float(c.get("fut_divergence_pts", 10.0))

    if not series:
        band_key, band_label = classify_pcr_band(1.0, c)
        return {
            "alignment_state": "MIXED_WAIT",
            "verdict_badge": "⚠️ DIVERGENCE / MIXED — WAIT",
            "verdict_summary": "Waiting for intraday series data...",
            "pcr_oi": 1.0,
            "pcr_vol": 1.0,
            "pcr_band": band_key,
            "pcr_band_label": band_label,
            "pcr_slope_30m": 0.0,
            "pcr_delta_open": 0.0,
            "chg_oi_slope": "NEUTRAL",
            "net_oi_slope_30m": 0,
            "total_oi_gap": 0,
            "total_oi_gap_delta": 0,
            "net_oi_chg": 0,
            "checkpoints": [],
        }

    last_pt = series[-1]
    first_pt = series[0]

    # Lookback point for 30-min window
    last_time = last_pt["time"]
    target_lookback = last_time - (slope_window * 60)
    p30_cands = [p for p in series if p["time"] <= target_lookback]
    p30 = p30_cands[-1] if p30_cands else first_pt

    pcr_oi = float(last_pt.get("pcr_oi") or 1.0)
    pcr_vol = float(last_pt.get("pcr_vol") or 1.0)
    open_pcr = float(first_pt.get("pcr_oi") or 1.0)
    p30_pcr = float(p30.get("pcr_oi") or open_pcr)

    pcr_slope_30m = round(pcr_oi - p30_pcr, 3)
    pcr_delta_open = round(pcr_oi - open_pcr, 3)

    call_oi = int(last_pt.get("call_oi") or 0)
    put_oi = int(last_pt.get("put_oi") or 0)
    call_oi_chg = int(last_pt.get("call_oi_chg") or 0)
    put_oi_chg = int(last_pt.get("put_oi_chg") or 0)
    net_oi_chg = int(last_pt.get("net_oi_chg") or 0)
    total_oi_gap = int(last_pt.get("total_oi_gap") or 0)

    p30_call_chg = int(p30.get("call_oi_chg") or 0)
    p30_put_chg = int(p30.get("put_oi_chg") or 0)
    p30_total_gap = int(p30.get("total_oi_gap") or 0)

    put_oi_chg_diff_30m = put_oi_chg - p30_put_chg
    call_oi_chg_diff_30m = call_oi_chg - p30_call_chg
    net_oi_slope_30m = put_oi_chg_diff_30m - call_oi_chg_diff_30m
    total_oi_gap_delta = total_oi_gap - p30_total_gap

    fut_close = float(last_pt.get("fut_close") or 0.0)
    fut_oi = int(last_pt.get("fut_oi") or 0)
    fut_oi_chg = int(last_pt.get("fut_oi_chg") or 0)
    fut_vol = int(last_pt.get("fut_vol") or 0)
    p30_fut_close = float(p30.get("fut_close") or fut_close)
    fut_delta_30m = fut_close - p30_fut_close

    # Fresh Change in OI Dominance
    if net_oi_slope_30m > 0:
        chg_oi_slope = "PUT_DOMINANT"
    elif net_oi_slope_30m < 0:
        chg_oi_slope = "CALL_DOMINANT"
    else:
        chg_oi_slope = "NEUTRAL"

    # Band classification
    band_key, band_label = classify_pcr_band(pcr_oi, c)

    # 3-Chart Conditions
    # 1. PCR trend
    pcr_rising = pcr_slope_30m >= slope_min_delta or (pcr_slope_30m >= 0 and pcr_delta_open > 0.03)
    pcr_falling = pcr_slope_30m <= -slope_min_delta or (pcr_slope_30m <= 0 and pcr_delta_open < -0.03)

    # 2. Fresh OI Change dominance
    put_fresh_leads = put_oi_chg > call_oi_chg and put_oi_chg_diff_30m >= call_oi_chg_diff_30m
    call_fresh_leads = call_oi_chg > put_oi_chg and call_oi_chg_diff_30m >= put_oi_chg_diff_30m

    # 3. Total OI Gap trend
    short_cov_ce_thresh = float(c.get("short_covering_ce_chg_threshold", -500000))
    short_cov_min_net = float(c.get("short_covering_min_net_oi", 1000000))
    unwinding_pe_thresh = float(c.get("unwinding_pe_chg_threshold", -500000))
    unwinding_min_net = float(c.get("unwinding_min_net_oi", -1000000))
    total_oi_gap_min_bull = float(c.get("total_oi_gap_min_bull", 1000000))
    mutual_unwind_thresh = float(c.get("mutual_unwind_thresh", -500000))

    gap_widening_bull = total_oi_gap_delta > 0 or total_oi_gap >= total_oi_gap_min_bull or (total_oi_gap > 0 and total_oi_gap_delta >= -1500000)
    gap_narrowing_bear = total_oi_gap_delta < 0 or total_oi_gap <= -total_oi_gap_min_bull or (total_oi_gap < 0 and total_oi_gap_delta <= 1500000)

    # Short-Covering Squeeze & Unwinding detection
    is_call_short_squeeze = (
        (call_oi_chg < short_cov_ce_thresh and net_oi_chg > short_cov_min_net)
        or (call_oi_chg_diff_30m < -2000000 and net_oi_slope_30m > 0 and pcr_oi >= 0.95)
    )
    is_put_unwinding_flush = (
        (put_oi_chg < unwinding_pe_thresh and net_oi_chg < unwinding_min_net)
        or (put_oi_chg_diff_30m < -2000000 and net_oi_slope_30m < 0 and pcr_oi <= 1.05)
    )

    # Mutual unwinding into expiry / EOD: BOTH Call and Put writers unwinding significantly
    is_mutual_unwinding = (
        put_oi_chg_diff_30m <= mutual_unwind_thresh
        and call_oi_chg_diff_30m <= mutual_unwind_thresh
    )

    # Price / PCR Divergence
    # Bearish Trap: Price climbing higher while PCR is tumbling and fresh writers are selling calls
    divergence_bear_trap = (fut_delta_30m >= fut_div_pts) and (pcr_slope_30m <= -0.015) and (net_oi_slope_30m < 0)
    # Bullish Absorption: Price falling while PCR is rising and put writers are actively absorbing selling
    divergence_bull_absorp = (fut_delta_30m <= -fut_div_pts) and (pcr_slope_30m >= 0.015) and (net_oi_slope_30m > 0)

    if divergence_bear_trap:
        alignment_state = "PRICE_PCR_DIVERGENCE"
        verdict_badge = "⚠️ BEARISH TRAP DIVERGENCE"
        verdict_summary = f"Futures up {fut_delta_30m:+.1f}pts but PCR dropping ({pcr_slope_30m:+.3f}/30m) & Call writers adding heavily. Bearish trap risk."
    elif divergence_bull_absorp:
        alignment_state = "PRICE_PCR_DIVERGENCE"
        verdict_badge = "⚠️ BULLISH ABSORPTION DIVERGENCE"
        verdict_summary = f"Futures down {fut_delta_30m:+.1f}pts but PCR rising ({pcr_slope_30m:+.3f}/30m) & Put writers absorbing. Bullish bounce floor building."
    elif is_mutual_unwinding:
        alignment_state = "MUTUAL_UNWINDING"
        verdict_badge = "🏁 EXPIRY / EOD MUTUAL UNWINDING (PROFIT BOOKING)"
        verdict_summary = (
            f"Both Call (Δ30m {call_oi_chg_diff_30m / 1e7:+.2f}Cr) and Put (Δ30m {put_oi_chg_diff_30m / 1e7:+.2f}Cr) writers are unwinding positions. "
            "Expiry mutual profit booking; avoid chasing directional momentum."
        )
    elif is_call_short_squeeze and net_oi_chg < 0:
        alignment_state = "COUNTER_TREND_COVERING"
        verdict_badge = "🟡 COUNTER-TREND CALL PROFIT BOOKING (NET REGIME STILL BEARISH)"
        verdict_summary = (
            f"Call writers unwinding off highs (CE Δ30m {call_oi_chg_diff_30m / 1e7:+.2f}Cr), but cumulative Net ΔOI remains negative ({net_oi_chg / 1e7:+.2f}Cr). "
            "Counter-trend Call profit booking within a bearish regime; treat bounces with caution."
        )
    elif is_put_unwinding_flush and net_oi_chg > 0:
        alignment_state = "COUNTER_TREND_UNWINDING"
        verdict_badge = "🟡 COUNTER-TREND PUT PROFIT BOOKING (NET REGIME STILL BULLISH)"
        verdict_summary = (
            f"Put writers unwinding (PE Δ30m {put_oi_chg_diff_30m / 1e7:+.2f}Cr), but cumulative Net ΔOI remains positive (+{net_oi_chg / 1e7:.2f}Cr). "
            "Counter-trend Put profit booking within a bullish regime."
        )
    elif is_call_short_squeeze and pcr_oi >= 0.95 and total_oi_gap > 0 and net_oi_chg >= 0 and not is_mutual_unwinding:
        alignment_state = "BULLISH_TRINITY_LOCK"
        verdict_badge = "🚀 3/3 BULLISH SHORT-COVERING SQUEEZE"
        verdict_summary = (
            f"Call writers in full retreat (CE Δ {call_oi_chg / 1e7:+.2f}Cr) with net ΔOI +{net_oi_chg / 1e7:.2f}Cr & PCR {pcr_oi:.3f}. "
            "Aggressive Call Short-Covering Squeeze propelling rally."
        )
    elif is_put_unwinding_flush and pcr_oi <= 1.05 and total_oi_gap < 0 and net_oi_chg <= 0 and not is_mutual_unwinding:
        alignment_state = "BEARISH_TRINITY_LOCK"
        verdict_badge = "🩸 3/3 BEARISH LONG-UNWINDING FLUSH"
        verdict_summary = (
            f"Put writers capitulating (PE Δ {put_oi_chg / 1e7:+.2f}Cr) with net ΔOI {net_oi_chg / 1e7:.2f}Cr & PCR {pcr_oi:.3f}. "
            "Aggressive Put Unwinding Flush accelerating breakdown."
        )
    elif pcr_rising and put_fresh_leads and gap_widening_bull:
        alignment_state = "BULLISH_TRINITY_LOCK"
        verdict_badge = "🔥 3/3 BULLISH TRINITY CONFIRMED"
        verdict_summary = "3/3 Agree: PCR rising, fresh Put writing dominating (+net ΔOI), and Total Put OI gap widening. Institutional buyers in full control."
    elif pcr_falling and call_fresh_leads and gap_narrowing_bear:
        alignment_state = "BEARISH_TRINITY_LOCK"
        verdict_badge = "🩸 3/3 BEARISH TRINITY CONFIRMED"
        verdict_summary = "3/3 Agree: PCR falling, fresh Call writing dominating (-net ΔOI), and Total Call OI resistance capping upside. Institutional sellers in control."
    else:
        alignment_state = "MIXED_WAIT"
        verdict_badge = "⚠️ DIVERGENCE / MIXED — WAIT"
        reasons = []
        if pcr_rising:
            reasons.append("PCR rising")
        elif pcr_falling:
            reasons.append("PCR falling")
        else:
            reasons.append("PCR flat")
        if put_fresh_leads:
            reasons.append("Put Δ leads")
        elif call_fresh_leads:
            reasons.append("Call Δ leads")
        else:
            reasons.append("ΔOI balanced")
        verdict_summary = f"Mixed signals ({', '.join(reasons)}). Awaiting 3-chart institutional alignment before entry."

    checkpoints = compute_checkpoints(series)

    return {
        "alignment_state": alignment_state,
        "verdict_badge": verdict_badge,
        "verdict_summary": verdict_summary,
        "pcr_oi": pcr_oi,
        "pcr_vol": pcr_vol,
        "pcr_band": band_key,
        "pcr_band_label": band_label,
        "pcr_slope_30m": pcr_slope_30m,
        "pcr_delta_open": pcr_delta_open,
        "chg_oi_slope": chg_oi_slope,
        "net_oi_slope_30m": net_oi_slope_30m,
        "total_oi_gap": total_oi_gap,
        "total_oi_gap_delta": total_oi_gap_delta,
        "net_oi_chg": net_oi_chg,
        "call_oi_chg": call_oi_chg,
        "put_oi_chg": put_oi_chg,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "fut_close": fut_close,
        "fut_oi": fut_oi,
        "fut_oi_chg": fut_oi_chg,
        "fut_vol": fut_vol,
        "is_call_short_squeeze": bool(is_call_short_squeeze),
        "is_put_unwinding_flush": bool(is_put_unwinding_flush),
        "checkpoints": checkpoints,
    }


def build_intraday_pcr_series(
    raw_snaps: List[Tuple],
    fut_candles: List[Dict[str, Any]],
    baseline_totals: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """
    Builds the structured time-series array pcr_oi_series from SQLite snapshots and FUT candles.
    Ensures seamless backfill-to-live stitching, linear gap bridging, outlier guards,
    and a 3-point rolling median filter to eliminate cliff drops and glitches.
    """
    # Build candle lookup by timestamp
    candle_by_ts: Dict[int, Dict[str, Any]] = {}
    for c in fut_candles:
        t_key = int(c.get("ts") if "ts" in c else c.get("time", 0))
        candle_by_ts[t_key] = {
            "close": float(c.get("c") if "c" in c else c.get("close", 0.0)),
            "oi": int(c.get("oi", 0)),
            "oi_chg": int(c.get("oi_chg", 0)),
            "vol": int(c.get("v") if "v" in c else c.get("volume", 0)),
        }

    # Sort candles by timestamp
    sorted_candle_ts = sorted(candle_by_ts.keys())
    first_fut = candle_by_ts[sorted_candle_ts[0]]["close"] if sorted_candle_ts else 22500.0

    def find_fut_item(ts: int) -> Dict[str, Any]:
        if ts in candle_by_ts:
            return candle_by_ts[ts]
        prev_ts = [t for t in sorted_candle_ts if t <= ts]
        if prev_ts:
            return candle_by_ts[prev_ts[-1]]
        if sorted_candle_ts:
            return candle_by_ts[sorted_candle_ts[0]]
        return {"close": first_fut, "oi": 0, "oi_chg": 0, "vol": 0}

    # Outlier / Partial-Snapshot Guard on raw snapshots:
    # When iterating through sequential option chain snapshots, if call_oi or put_oi suddenly
    # drops or spikes by > 35% in a single step (partial API fetch or zeroed strikes),
    # carry forward the previous valid snapshot's values.
    sanitized_dict: Dict[int, Dict[str, float]] = {}
    prev_valid: Optional[Dict[str, float]] = None

    for r in sorted(raw_snaps, key=lambda x: int(x[0])):
        ts = int(r[0])
        c_oi = float(r[1] or 0)
        p_oi = float(r[2] or 0)
        c_vol = float(r[3] or 0)
        p_vol = float(r[4] or 0)

        if prev_valid is not None:
            if prev_valid["ce_oi"] > 0 and abs(c_oi - prev_valid["ce_oi"]) / prev_valid["ce_oi"] > 0.35:
                c_oi = prev_valid["ce_oi"]
            if prev_valid["pe_oi"] > 0 and abs(p_oi - prev_valid["pe_oi"]) / prev_valid["pe_oi"] > 0.35:
                p_oi = prev_valid["pe_oi"]
            if prev_valid["ce_vol"] > 0 and c_vol < prev_valid["ce_vol"] * 0.4:
                c_vol = prev_valid["ce_vol"]
            if prev_valid["pe_vol"] > 0 and p_vol < prev_valid["pe_vol"] * 0.4:
                p_vol = prev_valid["pe_vol"]

        curr_valid = {
            "ce_oi": max(1.0, c_oi),
            "pe_oi": max(1.0, p_oi),
            "ce_vol": c_vol,
            "pe_vol": p_vol,
        }
        prev_valid = curr_valid
        sanitized_dict[ts] = curr_valid

    sorted_snap_ts = sorted(sanitized_dict.keys())

    # Baseline open OI
    base = baseline_totals or {}
    base_ce_oi = float(base.get("ce_oi", 0))
    base_pe_oi = float(base.get("pe_oi", 0))
    base_ce_chg = float(base.get("ce_oi_chg", 0))
    base_pe_chg = float(base.get("pe_oi_chg", 0))

    if base_ce_oi > 0 and base_ce_chg != 0:
        open_ce_oi = max(1.0, base_ce_oi - base_ce_chg)
    elif sorted_snap_ts:
        open_ce_oi = max(1.0, sanitized_dict[sorted_snap_ts[0]]["ce_oi"])
    else:
        open_ce_oi = max(1.0, base_ce_oi or 10000000.0)

    if base_pe_oi > 0 and base_pe_chg != 0:
        open_pe_oi = max(1.0, base_pe_oi - base_pe_chg)
    elif sorted_snap_ts:
        open_pe_oi = max(1.0, sanitized_dict[sorted_snap_ts[0]]["pe_oi"])
    else:
        open_pe_oi = max(1.0, base_pe_oi or 10000000.0)

    # Main snapshot alignment onto uniform grid matching FUT candles
    if sorted_candle_ts:
        dt = sorted_candle_ts[1] - sorted_candle_ts[0] if len(sorted_candle_ts) >= 2 else 180
        cfg_r = get_cfg()
        resample_mins = int(cfg_r.get("resample_interval_mins", 3))
        step_sec = max(60, resample_mins * 60)
        if dt <= 60 and len(sorted_candle_ts) > 60:
            target_ts_list = [
                t for i, t in enumerate(sorted_candle_ts)
                if (t - sorted_candle_ts[0]) % step_sec == 0 or i == len(sorted_candle_ts) - 1
            ]
        else:
            target_ts_list = sorted_candle_ts
    else:
        target_ts_list = sorted_snap_ts

    first_real = sanitized_dict[sorted_snap_ts[0]] if sorted_snap_ts else None
    first_real_ts = sorted_snap_ts[0] if sorted_snap_ts else None

    raw_series = []
    tot_steps = max(1, len(target_ts_list) - 1)

    for i, ts in enumerate(target_ts_list):
        fut_item = find_fut_item(ts)
        fut_c = fut_item["close"]
        fut_oi = fut_item["oi"]
        fut_oi_chg = fut_item["oi_chg"]
        fut_vol = fut_item["vol"]
        price_drift = (fut_c - first_fut) / max(first_fut * 0.01, 10.0)
        bull_factor = max(-0.25, min(0.25, price_drift * 0.10))

        if first_real_ts is not None and ts < first_real_ts:
            # Seamless Backfill-to-Live Stitching:
            # Anchor synthesized morning points between open_oi (at 09:15) and the FIRST REAL SNAPSHOT (at 13:45).
            # This mathematically guarantees zero jump/discontinuity at the stitch timestamp where live recording began.
            span_pre = max(1, first_real_ts - target_ts_list[0])
            frac_pre = max(0.0, min(1.0, (ts - target_ts_list[0]) / span_pre))
            # Smooth blend: at frac_pre = 1.0, (1 - frac_pre) is 0 so it lands exactly on first_real values
            drift_blend = (1.0 - frac_pre) * frac_pre * (bull_factor * 0.5)
            ce_oi = open_ce_oi + (first_real["ce_oi"] - open_ce_oi) * (frac_pre - drift_blend)
            pe_oi = open_pe_oi + (first_real["pe_oi"] - open_pe_oi) * (frac_pre + drift_blend)
            ce_vol = first_real["ce_vol"] * frac_pre
            pe_vol = first_real["pe_vol"] * frac_pre
        elif first_real_ts is not None:
            # Within or after live recording range: sequential snapshot interpolation
            prev_cands = [st for st in sorted_snap_ts if st <= ts]
            next_cands = [st for st in sorted_snap_ts if st > ts]

            if prev_cands and next_cands:
                st_prev = prev_cands[-1]
                st_next = next_cands[0]
                span = max(1, st_next - st_prev)
                frac = max(0.0, min(1.0, (ts - st_prev) / span))
                p_snap = sanitized_dict[st_prev]
                n_snap = sanitized_dict[st_next]
                ce_oi = p_snap["ce_oi"] + (n_snap["ce_oi"] - p_snap["ce_oi"]) * frac
                pe_oi = p_snap["pe_oi"] + (n_snap["pe_oi"] - p_snap["pe_oi"]) * frac
                ce_vol = p_snap["ce_vol"] + (n_snap["ce_vol"] - p_snap["ce_vol"]) * frac
                pe_vol = p_snap["pe_vol"] + (n_snap["pe_vol"] - p_snap["pe_vol"]) * frac
            elif prev_cands:
                p_snap = sanitized_dict[prev_cands[-1]]
                ce_oi = p_snap["ce_oi"]
                pe_oi = p_snap["pe_oi"]
                ce_vol = p_snap["ce_vol"]
                pe_vol = p_snap["pe_vol"]
            else:
                n_snap = sanitized_dict[next_cands[0]]
                ce_oi = n_snap["ce_oi"]
                pe_oi = n_snap["pe_oi"]
                ce_vol = n_snap["ce_vol"]
                pe_vol = n_snap["pe_vol"]
        else:
            # Fully synthetic curve when 0 snapshots exist
            frac = i / tot_steps
            target_ce = base_ce_oi if base_ce_oi > 0 else open_ce_oi
            target_pe = base_pe_oi if base_pe_oi > 0 else open_pe_oi
            ce_oi = open_ce_oi + (target_ce - open_ce_oi) * frac * (1.0 - bull_factor * 0.5)
            pe_oi = open_pe_oi + (target_pe - open_pe_oi) * frac * (1.0 + bull_factor * 0.5)
            ce_vol = float(base.get("ce_vol") or 0) * frac
            pe_vol = float(base.get("pe_vol") or 0) * frac

        ce_oi = max(1.0, ce_oi)
        pe_oi = max(1.0, pe_oi)
        call_chg = int(round(ce_oi - open_ce_oi))
        put_chg = int(round(pe_oi - open_pe_oi))

        raw_series.append({
            "time": chart_time(ts),
            "ts": int(ts),
            "fut_close": round(fut_c, 2),
            "fut_oi": fut_oi,
            "fut_oi_chg": fut_oi_chg,
            "fut_vol": fut_vol,
            "pcr_oi": round(pe_oi / max(ce_oi, 1.0), 3),
            "pcr_vol": round(pe_vol / max(ce_vol, 1.0), 3) if ce_vol > 0 else round(pe_oi / max(ce_oi, 1.0), 3),
            "call_oi": int(round(ce_oi)),
            "put_oi": int(round(pe_oi)),
            "total_oi_gap": int(round(pe_oi - ce_oi)),
            "call_oi_chg": call_chg,
            "put_oi_chg": put_chg,
            "net_oi_chg": put_chg - call_chg,
        })

    # Outlier / Single-Step Jump Guard on the generated series:
    # If any single-step jumps by > 35%, clamp to previous step
    if len(raw_series) >= 2:
        for i in range(1, len(raw_series)):
            p_ce = raw_series[i - 1]["call_oi"]
            c_ce = raw_series[i]["call_oi"]
            if p_ce > 0 and abs(c_ce - p_ce) / p_ce > 0.35:
                raw_series[i]["call_oi"] = p_ce

            p_pe = raw_series[i - 1]["put_oi"]
            c_pe = raw_series[i]["put_oi"]
            if p_pe > 0 and abs(c_pe - p_pe) / p_pe > 0.35:
                raw_series[i]["put_oi"] = p_pe

    # Apply 3-point rolling median filter to eliminate micro-blips across the whole series
    if len(raw_series) >= 3:
        ce_list = [p["call_oi"] for p in raw_series]
        pe_list = [p["put_oi"] for p in raw_series]
        filt_ce = list(ce_list)
        filt_pe = list(pe_list)
        for i in range(len(raw_series)):
            sub_c = [ce_list[max(0, i - 1)], ce_list[i], ce_list[min(len(raw_series) - 1, i + 1)]]
            sub_p = [pe_list[max(0, i - 1)], pe_list[i], pe_list[min(len(raw_series) - 1, i + 1)]]
            filt_ce[i] = int(round(sorted(sub_c)[1]))
            filt_pe[i] = int(round(sorted(sub_p)[1]))

        for i, p in enumerate(raw_series):
            ce = max(1, filt_ce[i])
            pe = max(1, filt_pe[i])
            c_chg = int(round(ce - open_ce_oi))
            p_chg = int(round(pe - open_pe_oi))
            p["call_oi"] = ce
            p["put_oi"] = pe
            p["total_oi_gap"] = pe - ce
            p["call_oi_chg"] = c_chg
            p["put_oi_chg"] = p_chg
            p["net_oi_chg"] = p_chg - c_chg
            p["pcr_oi"] = round(pe / ce, 3)

    return raw_series


def combine_multi_expiry_series(
    cur_series: List[Dict[str, Any]],
    next_series: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Combines cur (This wk) and next (Next wk) series into 'both' series.
    Sum Call OI, Put OI, Call ΔOI, Put ΔOI, and recalculate PCR-OI & PCR-Vol.
    """
    if not cur_series:
        return next_series
    if not next_series:
        return cur_series

    next_map = {p["time"]: p for p in next_series}
    both: List[Dict[str, Any]] = []

    last_next = next_series[0]

    for p in cur_series:
        t = p["time"]
        n = next_map.get(t)
        if n:
            last_next = n
        else:
            # Find closest next point at or before t
            cands = [x for x in next_series if x["time"] <= t]
            n = cands[-1] if cands else last_next

        c_oi = p["call_oi"] + n["call_oi"]
        p_oi = p["put_oi"] + n["put_oi"]
        c_chg = p["call_oi_chg"] + n["call_oi_chg"]
        p_chg = p["put_oi_chg"] + n["put_oi_chg"]
        gap = p_oi - c_oi
        net_chg = p_chg - c_chg

        pcr = round(p_oi / max(c_oi, 1), 3)
        # PCR vol weighted or averaged
        pcr_v = round((p["pcr_vol"] + n["pcr_vol"]) / 2.0, 3)

        both.append({
            "time": t,
            "ts": int(p.get("ts", t - 19800)),
            "fut_close": p.get("fut_close", 0.0),
            "fut_oi": p.get("fut_oi", 0),
            "fut_oi_chg": p.get("fut_oi_chg", 0),
            "fut_vol": p.get("fut_vol", 0),
            "pcr_oi": pcr,
            "pcr_vol": pcr_v,
            "call_oi": c_oi,
            "put_oi": p_oi,
            "total_oi_gap": gap,
            "call_oi_chg": c_chg,
            "put_oi_chg": p_chg,
            "net_oi_chg": net_chg,
        })

    return both
