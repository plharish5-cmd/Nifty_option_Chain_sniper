"""
oi_matrix.py — Institutional Open Interest Matrix & Strike Map 2.0.

Provides:
  1. Strike-Wise Open Interest distribution (Call vs Put).
  2. Automated Buildup Classification (Long Buildup, Short Buildup, Short Covering, Long Unwinding).
  3. Institutional Support (Emerald #10b981) and Resistance (Crimson #ef4444) wall metrics.
  4. Max Pain level & ATM distance mapping.
  5. Real-Time Market Sentiment Gauge (0–100% Bullish / Bearish / Neutral percentage score).
  6. Multi-expiry aggregation ('cur', 'next', 'both') and strike scope filtering ('atm_10', 'atm_5', 'full').
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .. import config

log = logging.getLogger("oi_matrix")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("oi_matrix", {})


def classify_buildup(chg: float, oi_chg: float, is_ce: bool = True) -> Tuple[str, str, str]:
    """
    Classifies strike buildup type and institutional market implication.
    Returns:
      (code, label, institutional_meaning)
    """
    if oi_chg > 0:
        if chg > 0:
            code = "LONG_BUILDUP"
            label = "Long Buildup"
            meaning = "Bullish Call Demand" if is_ce else "Downside Hedging"
        elif chg < 0:
            code = "SHORT_BUILDUP"
            label = "Short Buildup"
            meaning = "Call Writers Building Resistance" if is_ce else "Put Writers Building Support Floor"
        else:
            code = "LONG_BUILDUP"
            label = "Long Buildup"
            meaning = "Call Buyers" if is_ce else "Put Buyers"
    elif oi_chg < 0:
        if chg > 0:
            code = "SHORT_COVERING"
            label = "Short Covering"
            meaning = "Call Short Squeeze / Wall Breached" if is_ce else "Put Writers Covering"
        elif chg < 0:
            code = "LONG_UNWINDING"
            label = "Long Unwinding"
            meaning = "Call Longs Booking / Weakening" if is_ce else "Put Longs Booking / Relief"
        else:
            code = "LONG_UNWINDING"
            label = "Long Unwinding"
            meaning = "Unwinding"
    else:
        code = "NEUTRAL"
        label = "Neutral"
        meaning = "No Change"

    return code, label, meaning


def compute_max_pain(strikes: List[Dict[str, Any]]) -> float:
    """
    Computes option max pain: strike where total intrinsic payout across all option buyers is minimized.
    """
    if not strikes:
        return 0.0

    strike_vals = [s["strike"] for s in strikes]

    def total_pain(candidate_spot: float) -> float:
        return sum(
            s["ce_oi"] * max(candidate_spot - s["strike"], 0.0)
            + s["pe_oi"] * max(s["strike"] - candidate_spot, 0.0)
            for s in strikes
        )

    return min(strike_vals, key=total_pain)


def compute_sentiment_score(
    totals: Dict[str, Any],
    strikes: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes institutional market sentiment score (0–100%) and conviction bias.
    """
    c = cfg or get_cfg()
    bull_thresh = float(c.get("bullish_sentiment_threshold", 55.0))
    bear_thresh = float(c.get("bearish_sentiment_threshold", 45.0))

    pe_oi_chg = float(totals.get("pe_oi_chg") or 0)
    ce_oi_chg = float(totals.get("ce_oi_chg") or 0)
    pcr_oi = float(totals.get("pcr_oi") or 1.0)

    # 1. Net OI Delta Ratio (45% weight)
    abs_chg_sum = abs(pe_oi_chg) + abs(ce_oi_chg)
    if abs_chg_sum > 0:
        delta_ratio = (pe_oi_chg - ce_oi_chg) / abs_chg_sum
        score_delta = 50.0 + delta_ratio * 40.0
    else:
        score_delta = 50.0

    # 2. Strike-Wise Buildup Aggregation (35% weight)
    bullish_pts = 0.0
    bearish_pts = 0.0
    for s in strikes:
        # Put short buildup is bullish support
        if s.get("pe_buildup") == "SHORT_BUILDUP":
            bullish_pts += max(0.0, float(s.get("pe_oi_chg") or 0))
        elif s.get("pe_buildup") == "SHORT_COVERING":
            bearish_pts += abs(float(s.get("pe_oi_chg") or 0))
        elif s.get("pe_buildup") == "LONG_BUILDUP":
            bearish_pts += max(0.0, float(s.get("pe_oi_chg") or 0)) * 0.5

        # Call short buildup is bearish resistance
        if s.get("ce_buildup") == "SHORT_BUILDUP":
            bearish_pts += max(0.0, float(s.get("ce_oi_chg") or 0))
        elif s.get("ce_buildup") == "SHORT_COVERING":
            bullish_pts += abs(float(s.get("ce_oi_chg") or 0))
        elif s.get("ce_buildup") == "LONG_BUILDUP":
            bullish_pts += max(0.0, float(s.get("ce_oi_chg") or 0)) * 0.5

    tot_pts = bullish_pts + bearish_pts
    if tot_pts > 0:
        score_buildup = (bullish_pts / tot_pts) * 100.0
    else:
        score_buildup = 50.0

    # 3. PCR Normalized (20% weight)
    pcr_clamped = max(0.40, min(1.60, pcr_oi))
    score_pcr = ((pcr_clamped - 0.40) / 1.20) * 100.0

    # Composite
    score = round(0.45 * score_delta + 0.35 * score_buildup + 0.20 * score_pcr, 1)
    score = max(5.0, min(95.0, score))

    if score >= bull_thresh:
        bias = "BULLISH"
        bullish_pct = score
        bearish_pct = round(100.0 - score, 1)
        gauge_label = f"Bullish {bullish_pct:.1f}%"
        summary = (
            f"Bullish positioning: Put writers defending support (+{pe_oi_chg/1e5:.1f}L PE ΔOI vs +{ce_oi_chg/1e5:.1f}L CE ΔOI)."
        )
    elif score <= bear_thresh:
        bias = "BEARISH"
        bearish_pct = round(100.0 - score, 1)
        bullish_pct = score
        gauge_label = f"Bearish {bearish_pct:.1f}%"
        summary = (
            f"Bearish positioning: Call writers defending overhead resistance (+{ce_oi_chg/1e5:.1f}L CE ΔOI vs +{pe_oi_chg/1e5:.1f}L PE ΔOI)."
        )
    else:
        bias = "NEUTRAL"
        bullish_pct = score
        bearish_pct = round(100.0 - score, 1)
        gauge_label = f"Balanced {score:.1f}%"
        summary = "Balanced market positioning: Call and Put writers evenly matched across key strikes."

    return {
        "score": score,
        "bias": bias,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "gauge_label": gauge_label,
        "summary": summary,
        "score_delta": round(score_delta, 1),
        "score_buildup": round(score_buildup, 1),
        "score_pcr": round(score_pcr, 1),
    }


def build_oi_matrix(
    chain_dict: Optional[Dict[str, Any]],
    next_chain_dict: Optional[Dict[str, Any]] = None,
    expiry_mode: str = "cur",
    scope: str = "atm_10",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Constructs the full Institutional Open Interest Matrix & Strike Map 2.0 payload.
    """
    c = cfg or get_cfg()
    atm_10_count = int(c.get("atm_10_count", 10))
    atm_5_count = int(c.get("atm_5_count", 5))

    if not chain_dict or not chain_dict.get("rows"):
        return {
            "ok": False,
            "message": "No option chain available for active session",
            "day": "",
            "expiry": "",
            "selected_expiry": expiry_mode,
            "scope": scope,
            "strike_range_label": "Waiting for chain data...",
            "spot": 0.0,
            "atm_strike": 0.0,
            "step": 50.0,
            "max_pain": 0.0,
            "call_wall": 0.0,
            "put_wall": 0.0,
            "totals": {
                "ce_oi": 0,
                "pe_oi": 0,
                "ce_oi_chg": 0,
                "pe_oi_chg": 0,
                "net_oi": 0,
                "net_oi_chg": 0,
                "pcr_oi": 1.0,
                "pcr_vol": 1.0,
            },
            "sentiment": {
                "score": 50.0,
                "bias": "NEUTRAL",
                "bullish_pct": 50.0,
                "bearish_pct": 50.0,
                "gauge_label": "Neutral 50.0%",
                "summary": "Waiting for live chain snapshot...",
            },
            "strikes": [],
            "top_buildups": [],
        }

    spot = float(chain_dict.get("spot") or chain_dict.get("atm") or 0.0)
    step = float(chain_dict.get("step") or 50.0)
    atm_strike = round(spot / step) * step if step > 0 else spot
    raw_rows = chain_dict.get("rows") or []

    # If mode == 'both' and next_chain_dict is present, merge strikes
    if expiry_mode == "both" and next_chain_dict and next_chain_dict.get("rows"):
        cur_map = {r["strike"]: r for r in raw_rows}
        next_map = {r["strike"]: r for r in next_chain_dict.get("rows", [])}
        all_strikes = sorted(set(cur_map.keys()) | set(next_map.keys()))

        merged_rows = []
        for st in all_strikes:
            c_r = cur_map.get(st)
            n_r = next_map.get(st)

            c_ce = c_r.get("ce", {}) if c_r else {}
            c_pe = c_r.get("pe", {}) if c_r else {}
            n_ce = n_r.get("ce", {}) if n_r else {}
            n_pe = n_r.get("pe", {}) if n_r else {}

            merged_rows.append({
                "strike": st,
                "ce": {
                    "oi": float(c_ce.get("oi") or 0) + float(n_ce.get("oi") or 0),
                    "oi_chg": float(c_ce.get("oi_chg") or 0) + float(n_ce.get("oi_chg") or 0),
                    "ltp": float(c_ce.get("ltp") or n_ce.get("ltp") or 0),
                    "chg": float(c_ce.get("chg") or n_ce.get("chg") or 0),
                    "vol": float(c_ce.get("vol") or 0) + float(n_ce.get("vol") or 0),
                    "iv": float(c_ce.get("iv") or n_ce.get("iv") or 0),
                },
                "pe": {
                    "oi": float(c_pe.get("oi") or 0) + float(n_pe.get("oi") or 0),
                    "oi_chg": float(c_pe.get("oi_chg") or 0) + float(n_pe.get("oi_chg") or 0),
                    "ltp": float(c_pe.get("ltp") or n_pe.get("ltp") or 0),
                    "chg": float(c_pe.get("chg") or n_pe.get("chg") or 0),
                    "vol": float(c_pe.get("vol") or 0) + float(n_pe.get("vol") or 0),
                    "iv": float(c_pe.get("iv") or n_pe.get("iv") or 0),
                },
            })
        active_raw_rows = merged_rows
    elif expiry_mode == "next" and next_chain_dict and next_chain_dict.get("rows"):
        active_raw_rows = next_chain_dict.get("rows")
    else:
        active_raw_rows = raw_rows

    # Scope filtering
    if scope == "atm_5":
        filtered_raw = [r for r in active_raw_rows if abs(r["strike"] - atm_strike) <= atm_5_count * step]
        range_label = f"{int(atm_strike - atm_5_count * step):,} – {int(atm_strike + atm_5_count * step):,} (±5 ATM)"
    elif scope == "full":
        filtered_raw = active_raw_rows
        min_k = min((r["strike"] for r in active_raw_rows), default=atm_strike)
        max_k = max((r["strike"] for r in active_raw_rows), default=atm_strike)
        range_label = f"Full Chain ({int(min_k):,} – {int(max_k):,})"
    else:  # default atm_10
        scope = "atm_10"
        filtered_raw = [r for r in active_raw_rows if abs(r["strike"] - atm_strike) <= atm_10_count * step]
        range_label = f"{int(atm_strike - atm_10_count * step):,} – {int(atm_strike + atm_10_count * step):,} (±10 ATM)"

    # Build strike breakdown
    strikes: List[Dict[str, Any]] = []
    tot_ce_oi = 0.0
    tot_pe_oi = 0.0
    tot_ce_oi_chg = 0.0
    tot_pe_oi_chg = 0.0
    tot_ce_vol = 0.0
    tot_pe_vol = 0.0

    for r in filtered_raw:
        st = float(r["strike"])
        ce = r.get("ce", {}) or {}
        pe = r.get("pe", {}) or {}

        ce_oi = float(ce.get("oi") or 0)
        pe_oi = float(pe.get("oi") or 0)
        ce_oi_chg = float(ce.get("oi_chg") or 0)
        pe_oi_chg = float(pe.get("oi_chg") or 0)
        ce_ltp = float(ce.get("ltp") or 0)
        pe_ltp = float(pe.get("ltp") or 0)
        ce_chg = float(ce.get("chg") or 0)
        pe_chg = float(pe.get("chg") or 0)
        ce_vol = float(ce.get("vol") or 0)
        pe_vol = float(pe.get("vol") or 0)

        tot_ce_oi += ce_oi
        tot_pe_oi += pe_oi
        tot_ce_oi_chg += ce_oi_chg
        tot_pe_oi_chg += pe_oi_chg
        tot_ce_vol += ce_vol
        tot_pe_vol += pe_vol

        ce_bcode, ce_blabel, ce_bmeaning = classify_buildup(ce_chg, ce_oi_chg, is_ce=True)
        pe_bcode, pe_blabel, pe_bmeaning = classify_buildup(pe_chg, pe_oi_chg, is_ce=False)

        # Dominant strike institutional implication
        net_oi = pe_oi - ce_oi
        net_oi_chg = pe_oi_chg - ce_oi_chg

        if pe_oi_chg > ce_oi_chg and pe_oi_chg > 0:
            dominant_bias = "BULLISH_SUPPORT"
            dominant_tag = "🟢 Put Support Building"
        elif ce_oi_chg > pe_oi_chg and ce_oi_chg > 0:
            dominant_bias = "BEARISH_RESISTANCE"
            dominant_tag = "🔴 Call Resistance Building"
        elif ce_oi_chg < 0 and pe_oi_chg >= 0:
            dominant_bias = "BULLISH_SQUEEZE"
            dominant_tag = "⚡ Call Short Covering Squeeze"
        elif pe_oi_chg < 0 and ce_oi_chg >= 0:
            dominant_bias = "BEARISH_UNWINDING"
            dominant_tag = "🩸 Put Unwinding Breakdown"
        elif net_oi > 0:
            dominant_bias = "SUPPORT_WALL"
            dominant_tag = "Put Lead (Support)"
        else:
            dominant_bias = "RESISTANCE_WALL"
            dominant_tag = "Call Lead (Resistance)"

        dominant_buildup = pe_blabel if abs(pe_oi_chg) > abs(ce_oi_chg) and pe_blabel != "Neutral" else (ce_blabel if ce_blabel != "Neutral" else "Neutral")

        strikes.append({
            "strike": st,
            "is_atm": abs(st - atm_strike) < 1.0,
            "dist_atm": int(st - atm_strike),
            "ce_oi": int(round(ce_oi)),
            "pe_oi": int(round(pe_oi)),
            "ce_oi_chg": int(round(ce_oi_chg)),
            "pe_oi_chg": int(round(pe_oi_chg)),
            "call_oi": int(round(ce_oi)),
            "call_oi_chg": int(round(ce_oi_chg)),
            "put_oi": int(round(pe_oi)),
            "put_oi_chg": int(round(pe_oi_chg)),
            "ce_ltp": round(ce_ltp, 2),
            "pe_ltp": round(pe_ltp, 2),
            "ce_chg": round(ce_chg, 2),
            "pe_chg": round(pe_chg, 2),
            "ce_vol": int(round(ce_vol)),
            "pe_vol": int(round(pe_vol)),
            "ce_buildup": ce_bcode,
            "ce_buildup_label": ce_blabel,
            "ce_meaning": ce_bmeaning,
            "pe_buildup": pe_bcode,
            "pe_buildup_label": pe_blabel,
            "pe_meaning": pe_bmeaning,
            "buildup": dominant_buildup,
            "call_buildup": ce_blabel,
            "put_buildup": pe_blabel,
            "net_oi": int(round(net_oi)),
            "net_oi_chg": int(round(net_oi_chg)),
            "dominant_bias": dominant_bias,
            "dominant_tag": dominant_tag,
            "spot": spot,
        })

    # Sort strikes by strike ascending
    strikes.sort(key=lambda s: s["strike"])

    # Max pain & walls
    max_pain = compute_max_pain(strikes)
    for s in strikes:
        s["max_pain"] = max_pain
    call_wall_row = max(strikes, key=lambda s: s["ce_oi"], default=None)
    put_wall_row = max(strikes, key=lambda s: s["pe_oi"], default=None)

    call_wall = call_wall_row["strike"] if call_wall_row else 0.0
    put_wall = put_wall_row["strike"] if put_wall_row else 0.0
    call_wall_oi = call_wall_row["ce_oi"] if call_wall_row else 0
    put_wall_oi = put_wall_row["pe_oi"] if put_wall_row else 0

    pcr_oi = round(tot_pe_oi / max(tot_ce_oi, 1.0), 3)
    pcr_vol = round(tot_pe_vol / max(tot_ce_vol, 1.0), 3) if tot_ce_vol > 0 else pcr_oi

    totals = {
        "ce_oi": int(round(tot_ce_oi)),
        "pe_oi": int(round(tot_pe_oi)),
        "ce_oi_chg": int(round(tot_ce_oi_chg)),
        "pe_oi_chg": int(round(tot_pe_oi_chg)),
        "net_oi": int(round(tot_pe_oi - tot_ce_oi)),
        "net_oi_chg": int(round(tot_pe_oi_chg - tot_ce_oi_chg)),
        "pcr_oi": pcr_oi,
        "pcr_vol": pcr_vol,
        "ce_vol": int(round(tot_ce_vol)),
        "pe_vol": int(round(tot_pe_vol)),
    }

    sentiment = compute_sentiment_score(totals, strikes, c)

    # Top Buildups list (sorted by largest fresh volume / OI change activity)
    top_buildups = sorted(
        strikes,
        key=lambda s: abs(s["ce_oi_chg"]) + abs(s["pe_oi_chg"]),
        reverse=True,
    )[:10]

    active_exp_str = (
        f"{chain_dict.get('expiry', '')} + {next_chain_dict.get('expiry', '')}"
        if expiry_mode == "both" and next_chain_dict
        else chain_dict.get("expiry", "")
    )

    return {
        "ok": True,
        "day": chain_dict.get("day", ""),
        "expiry": active_exp_str,
        "selected_expiry": expiry_mode,
        "scope": scope,
        "strike_range_label": range_label,
        "spot": round(spot, 2),
        "atm_strike": round(atm_strike, 1),
        "step": step,
        "max_pain": max_pain,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "call_wall_oi": call_wall_oi,
        "put_wall_oi": put_wall_oi,
        "totals": totals,
        "sentiment": sentiment,
        "strikes": strikes,
        "strike_map": strikes,
        "top_buildups": top_buildups,
    }
