"""
smart_oi.py — Smart OI Filtering & Price-Action Confluence Engine.

Features:
  1. Retail Noise & Stale Position Filtering:
     - Deep OTM Noise Exclusion (min absolute OI + min session volume).
     - Stale Position Filter (excludes inactive high-OI strikes with negligible intraday change).
     - Single-Bar Spike Filter (requires volume follow-through confirmation).
     - Identifies institutional "Signal Strikes" (Put Support Floors & Call Resistance Ceilings).
  2. Smart OI Price-Action Confluence Scoring:
     - Cross-references signal levels against NIFTY Futures candlestick price action & volume.
     - Flags Bullish Confluence (support bounces with volume & +ΔOI).
     - Flags Bearish Confluence (resistance rejections with volume & -ΔOI).
     - Flags Divergence Warnings: Bearish Trap (price up, calls written) & Bullish Absorption (price down, puts written).
  3. Intraday Multi-Line Trend Telemetry & Multi-PCR Sub-Chains:
     - Aligned time-series for candlestick chart, Net Delta bars, Cumulative Smart OI, and dual PCR.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..timeutil import now_epoch, now_ist, session_bounds

log = logging.getLogger("smart_oi")


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("smart_oi", {})


def filter_institutional_smart_oi(
    chain_dict: Optional[Dict[str, Any]],
    next_chain_dict: Optional[Dict[str, Any]] = None,
    expiry_mode: str = "cur",
    range_mode: str = "atm",
    span: int = 10,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Filters retail noise and stale positioning to isolate high-conviction institutional Signal Strikes.
    """
    c = cfg or get_cfg()
    min_abs_oi = int(c.get("min_absolute_oi", 500000))
    min_vol = int(c.get("min_session_volume", 10000))
    min_oi_chg = int(c.get("min_oi_change", 1000))

    if not chain_dict or not chain_dict.get("rows"):
        return {
            "ok": False,
            "message": "Option chain not available for Smart OI analysis",
            "spot": 0.0,
            "atm_strike": 0.0,
            "signal_strikes": [],
            "all_strikes": [],
            "key_support_level": 0.0,
            "key_support_oi": 0,
            "key_resistance_level": 0.0,
            "key_resistance_oi": 0,
            "filtered_out_count": 0,
            "signal_strike_count": 0,
        }

    spot = float(chain_dict.get("spot") or chain_dict.get("atm") or 0.0)
    step = float(chain_dict.get("step") or 50.0)
    atm_strike = round(spot / step) * step if step > 0 else spot

    raw_rows = chain_dict.get("rows") or []

    # Handle multi-expiry merge if mode is 'both'
    if expiry_mode == "both" and next_chain_dict and next_chain_dict.get("rows"):
        cur_map = {r["strike"]: r for r in raw_rows}
        next_map = {r["strike"]: r for r in next_chain_dict.get("rows", [])}
        all_ks = sorted(set(cur_map.keys()) | set(next_map.keys()))
        merged_rows = []
        for k in all_ks:
            c_r, n_r = cur_map.get(k), next_map.get(k)
            c_ce, c_pe = (c_r.get("ce", {}) if c_r else {}), (c_r.get("pe", {}) if c_r else {})
            n_ce, n_pe = (n_r.get("ce", {}) if n_r else {}), (n_r.get("pe", {}) if n_r else {})
            merged_rows.append({
                "strike": k,
                "ce": {
                    "oi": (c_ce.get("oi") or 0) + (n_ce.get("oi") or 0),
                    "oi_chg": (c_ce.get("oi_chg") or 0) + (n_ce.get("oi_chg") or 0),
                    "ltp": c_ce.get("ltp") or n_ce.get("ltp") or 0.0,
                    "chg": c_ce.get("chg") or n_ce.get("chg") or 0.0,
                    "vol": (c_ce.get("vol") or 0) + (n_ce.get("vol") or 0),
                },
                "pe": {
                    "oi": (c_pe.get("oi") or 0) + (n_pe.get("oi") or 0),
                    "oi_chg": (c_pe.get("oi_chg") or 0) + (n_pe.get("oi_chg") or 0),
                    "ltp": c_pe.get("ltp") or n_pe.get("ltp") or 0.0,
                    "chg": c_pe.get("chg") or n_pe.get("chg") or 0.0,
                    "vol": (c_pe.get("vol") or 0) + (n_pe.get("vol") or 0),
                },
            })
        active_raw = merged_rows
    elif expiry_mode == "next" and next_chain_dict and next_chain_dict.get("rows"):
        active_raw = next_chain_dict.get("rows")
    else:
        active_raw = raw_rows

    # Range filtering
    if range_mode == "atm" and span > 0:
        filtered_scope = [r for r in active_raw if abs(r["strike"] - atm_strike) <= span * step]
    elif range_mode == "fixed" and span > 0:
        filtered_scope = [r for r in active_raw if abs(r["strike"] - atm_strike) <= span * step]
    else:
        filtered_scope = active_raw

    signal_strikes: List[Dict[str, Any]] = []
    all_evaluated: List[Dict[str, Any]] = []
    filtered_out_count = 0

    for r in filtered_scope:
        st = float(r["strike"])
        ce = r.get("ce", {}) or {}
        pe = r.get("pe", {}) or {}

        ce_oi = int(round(ce.get("oi") or 0))
        pe_oi = int(round(pe.get("oi") or 0))
        ce_oi_chg = int(round(ce.get("oi_chg") or 0))
        pe_oi_chg = int(round(pe.get("oi_chg") or 0))
        ce_vol = int(round(ce.get("vol") or 0))
        pe_vol = int(round(pe.get("vol") or 0))
        ce_chg = float(ce.get("chg") or 0.0)
        pe_chg = float(pe.get("chg") or 0.0)

        # 1. Deep OTM Noise Exclusion Filter
        ce_is_noise = (ce_oi < min_abs_oi) and (ce_vol < min_vol)
        pe_is_noise = (pe_oi < min_abs_oi) and (pe_vol < min_vol)

        # 2. Stale Position Filter: inactive strikes with negligible intraday change
        ce_is_stale = abs(ce_oi_chg) < min_oi_chg
        pe_is_stale = abs(pe_oi_chg) < min_oi_chg

        # Overall strike status
        is_retail_noise = (ce_is_noise and pe_is_noise)
        is_stale = (ce_is_stale and pe_is_stale)

        is_signal = False
        signal_type = "NEUTRAL"
        signal_label = "Neutral"
        conviction = 50.0

        if not is_retail_noise and not is_stale:
            # Check for high-conviction institutional accumulations
            net_delta = pe_oi_chg - ce_oi_chg

            if pe_oi_chg >= min_oi_chg and pe_oi_chg > ce_oi_chg:
                is_signal = True
                signal_type = "PUT_SUPPORT_FLOOR"
                signal_label = "🟢 Put Support Floor"
                conviction = min(98.0, 60.0 + (pe_oi_chg / 500000.0) * 15.0)
            elif ce_oi_chg >= min_oi_chg and ce_oi_chg > pe_oi_chg:
                is_signal = True
                signal_type = "CALL_RESISTANCE_CEILING"
                signal_label = "🔴 Call Resistance Ceiling"
                conviction = min(98.0, 60.0 + (ce_oi_chg / 500000.0) * 15.0)
            elif ce_oi_chg <= -min_oi_chg and ce_chg > 0:
                is_signal = True
                signal_type = "CALL_SQUEEZE_BREAKOUT"
                signal_label = "⚡ Call Short Squeeze"
                conviction = 78.0
            elif pe_oi_chg <= -min_oi_chg and pe_chg < 0:
                is_signal = True
                signal_type = "PUT_UNWINDING_BREAKDOWN"
                signal_label = "🩸 Put Unwinding Breakdown"
                conviction = 78.0
            elif pe_oi >= min_abs_oi * 2 or ce_oi >= min_abs_oi * 2:
                is_signal = True
                signal_type = "BALANCED_WALL"
                signal_label = "⚖ Structural Wall"
                conviction = 65.0
        else:
            filtered_out_count += 1

        strike_item = {
            "strike": st,
            "is_atm": abs(st - atm_strike) < 1.0,
            "dist_atm": int(st - atm_strike),
            "call_oi": ce_oi,
            "call_oi_chg": ce_oi_chg,
            "call_vol": ce_vol,
            "put_oi": pe_oi,
            "put_oi_chg": pe_oi_chg,
            "put_vol": pe_vol,
            "net_oi": pe_oi - ce_oi,
            "net_oi_chg": pe_oi_chg - ce_oi_chg,
            "is_signal": is_signal,
            "signal_type": signal_type,
            "signal_label": signal_label,
            "conviction": round(conviction, 1),
            "is_retail_noise": is_retail_noise,
            "is_stale": is_stale,
        }
        all_evaluated.append(strike_item)
        if is_signal:
            signal_strikes.append(strike_item)

    # Sort signal strikes
    signal_strikes.sort(key=lambda s: s["strike"])

    # Determine dominant support floor and resistance ceiling
    put_supports = [s for s in signal_strikes if s["signal_type"] == "PUT_SUPPORT_FLOOR" or s["net_oi_chg"] > 0]
    call_resistances = [s for s in signal_strikes if s["signal_type"] == "CALL_RESISTANCE_CEILING" or s["net_oi_chg"] < 0]

    dominant_put = max(put_supports, key=lambda s: (s["put_oi"] + s["put_oi_chg"] * 2), default=None)
    dominant_call = max(call_resistances, key=lambda s: (s["call_oi"] + s["call_oi_chg"] * 2), default=None)

    key_support_level = dominant_put["strike"] if dominant_put else (atm_strike - 100.0)
    key_support_oi = dominant_put["put_oi"] if dominant_put else 0
    key_resistance_level = dominant_call["strike"] if dominant_call else (atm_strike + 100.0)
    key_resistance_oi = dominant_call["call_oi"] if dominant_call else 0

    return {
        "ok": True,
        "spot": spot,
        "atm_strike": atm_strike,
        "key_support_level": key_support_level,
        "key_support_oi": key_support_oi,
        "key_resistance_level": key_resistance_level,
        "key_resistance_oi": key_resistance_oi,
        "signal_strikes": signal_strikes,
        "all_strikes": all_evaluated,
        "filtered_out_count": filtered_out_count,
        "signal_strike_count": len(signal_strikes),
    }


def compute_smart_oi_confluence(
    signal_data: Dict[str, Any],
    candles: List[Dict[str, Any]],
    pcr_oi_series: Optional[List[Dict[str, Any]]] = None,
    pcr_series: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Cross-references filtered Smart OI signal levels against NIFTY Futures candlestick price action and volume.
    Generates confluence and divergence markers.
    """
    c = cfg or get_cfg()
    tol = float(c.get("confluence_level_tolerance_pts", 25.0))
    div_oi_thresh = int(c.get("divergence_threshold_oi", 250000))

    sup_level = float(signal_data.get("key_support_level") or 0.0)
    res_level = float(signal_data.get("key_resistance_level") or 0.0)

    # Build lookup map for time-series pcr/smart oi flow if available
    active_series = pcr_oi_series or pcr_series or []
    series_map: Dict[int, Dict[str, Any]] = {}
    if active_series:
        for pt in active_series:
            series_map[int(pt["time"])] = pt

    confluence_candles: List[Dict[str, Any]] = []
    confluence_markers: List[Dict[str, Any]] = []
    bullish_confluences = 0
    bearish_confluences = 0
    divergence_warnings = 0

    running_cum_smart = 0
    avg_vol = (sum(float(cd.get("v") or 0) for cd in candles) / max(1, len(candles))) if candles else 1000.0

    for i, cd in enumerate(candles):
        t = int(cd.get("ts") or cd.get("time") or 0)
        o = float(cd.get("o") or 0.0)
        h = float(cd.get("h") or 0.0)
        l = float(cd.get("l") or 0.0)
        cl = float(cd.get("c") or 0.0)
        v = float(cd.get("v") or 0.0)

        # Match closest pcr point if exact minute not found
        p_match = series_map.get(t)
        if not p_match and pcr_oi_series:
            # find nearest point within 300 seconds
            closest = min(pcr_oi_series, key=lambda x: abs(int(x["time"]) - t), default=None)
            if closest and abs(int(closest["time"]) - t) <= 300:
                p_match = closest

        net_delta = int(p_match.get("net_oi_chg") or 0) if p_match else 0
        running_cum_smart += net_delta

        candle_markers: List[Dict[str, Any]] = []
        is_green = cl >= o
        vol_expanding = v >= avg_vol * 1.05

        # 1. Bullish Confluence: Support Tested / Bounced with +ΔOI
        if sup_level > 0 and abs(l - sup_level) <= tol:
            if is_green or net_delta > 0 or vol_expanding:
                bullish_confluences += 1
                marker = {
                    "time": t,
                    "position": "belowBar",
                    "color": "#10b981",
                    "shape": "arrowUp",
                    "text": f"🟢 Bullish Support Bounce @ {int(sup_level):,}",
                    "kind": "BULLISH_CONFLUENCE",
                    "level": sup_level,
                }
                confluence_markers.append(marker)
                candle_markers.append(marker)

        # 2. Bearish Confluence: Resistance Tested / Rejected with -ΔOI
        if res_level > 0 and abs(h - res_level) <= tol:
            if not is_green or net_delta < 0 or vol_expanding:
                bearish_confluences += 1
                marker = {
                    "time": t,
                    "position": "aboveBar",
                    "color": "#ef4444",
                    "shape": "arrowDown",
                    "text": f"🔴 Bearish Resistance Rejection @ {int(res_level):,}",
                    "kind": "BEARISH_CONFLUENCE",
                    "level": res_level,
                }
                confluence_markers.append(marker)
                candle_markers.append(marker)

        # 3. Divergence Warnings:
        # Bearish Trap: Price rising but Smart OI is strongly negative (heavy Call writing capping)
        if is_green and (cl - o) >= 15.0 and net_delta < -div_oi_thresh:
            divergence_warnings += 1
            marker = {
                "time": t,
                "position": "aboveBar",
                "color": "#f59e0b",
                "shape": "circle",
                "text": "⚠️ Bearish Trap: Rally Capped by Heavy Call Writing",
                "kind": "BEARISH_TRAP_DIVERGENCE",
            }
            confluence_markers.append(marker)
            candle_markers.append(marker)
        # Bullish Absorption: Price falling but Smart OI is strongly positive (institutions absorbing dip)
        elif not is_green and (o - cl) >= 15.0 and net_delta > div_oi_thresh:
            divergence_warnings += 1
            marker = {
                "time": t,
                "position": "belowBar",
                "color": "#10b981",
                "shape": "circle",
                "text": "🟢 Bullish Absorption: Dip Defended by Heavy Put Writing",
                "kind": "BULLISH_ABSORPTION_DIVERGENCE",
            }
            confluence_markers.append(marker)
            candle_markers.append(marker)

        confluence_candles.append({
            "time": t,
            "open": o,
            "high": h,
            "low": l,
            "close": cl,
            "volume": v,
            "smart_oi_delta": net_delta,
            "cum_smart_oi": running_cum_smart,
            "pcr_oi": float(p_match.get("pcr_oi") or 1.0) if p_match else 1.0,
            "pcr_vol": float(p_match.get("pcr_vol") or 1.0) if p_match else 1.0,
            "markers": candle_markers,
        })

    # Confluence narrative summary
    if bullish_confluences > bearish_confluences and bullish_confluences > 0:
        confluence_summary = f"🟢 Bullish Confluence Dominant: Institutional Put writers defending {int(sup_level):,} floor."
    elif bearish_confluences > bullish_confluences and bearish_confluences > 0:
        confluence_summary = f"🔴 Bearish Confluence Dominant: Institutional Call writers capping {int(res_level):,} ceiling."
    elif divergence_warnings > 0:
        confluence_summary = "⚠️ Active Price/OI Divergence detected: Watch for institutional absorption or trap."
    else:
        confluence_summary = f"⚖ Equilibrium: Price navigating between {int(sup_level):,} Support and {int(res_level):,} Resistance."

    return {
        "candles": confluence_candles,
        "markers": confluence_markers,
        "confluence_summary": confluence_summary,
        "stats": {
            "bullish_confluences": bullish_confluences,
            "bearish_confluences": bearish_confluences,
            "divergence_warnings": divergence_warnings,
        },
    }


def build_smart_oi_payload(
    chain_dict: Optional[Dict[str, Any]],
    next_chain_dict: Optional[Dict[str, Any]] = None,
    candles: Optional[List[Dict[str, Any]]] = None,
    pcr_oi_series: Optional[List[Dict[str, Any]]] = None,
    expiry_mode: str = "cur",
    tf: str = "3m",
    range_mode: str = "atm",
    span: int = 10,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Constructs the complete Smart OI Suite payload for the trading desk.
    """
    c = cfg or get_cfg()

    # Step 1: Institutional Smart OI noise filtering
    filtered = filter_institutional_smart_oi(
        chain_dict=chain_dict,
        next_chain_dict=next_chain_dict,
        expiry_mode=expiry_mode,
        range_mode=range_mode,
        span=span,
        cfg=c,
    )

    if not filtered.get("ok"):
        return {
            "ok": False,
            "message": filtered.get("message", "Smart OI analysis unavailable"),
            "tf": tf,
            "range_mode": range_mode,
            "span": span,
            "expiry_mode": expiry_mode,
            "candles": [],
            "markers": [],
            "signal_strikes": [],
            "time_series": [],
            "stats": {},
        }

    # Step 2: Price action confluence scoring
    confluence = compute_smart_oi_confluence(
        signal_data=filtered,
        candles=candles or [],
        pcr_oi_series=pcr_oi_series,
        cfg=c,
    )

    # Step 3: Format aligned time series for multi-line telemetry sub-charts
    time_series: List[Dict[str, Any]] = []
    for cd in confluence.get("candles", []):
        t = cd["time"]
        time_series.append({
            "time": t,
            "fut_close": cd["close"],
            "smart_oi_delta": cd["smart_oi_delta"],
            "cum_smart_oi": cd["cum_smart_oi"],
            "pcr_oi": cd["pcr_oi"],
            "pcr_vol": cd["pcr_vol"],
            "volume": cd["volume"],
        })

    active_exp = (
        f"{chain_dict.get('expiry', '')} + {next_chain_dict.get('expiry', '')}"
        if expiry_mode == "both" and next_chain_dict
        else (chain_dict.get("expiry") if chain_dict else "")
    )

    return {
        "ok": True,
        "day": chain_dict.get("day", "") if chain_dict else "",
        "expiry": active_exp,
        "expiry_mode": expiry_mode,
        "tf": tf,
        "range_mode": range_mode,
        "span": span,
        "spot": filtered.get("spot", 0.0),
        "atm_strike": filtered.get("atm_strike", 0.0),
        "key_support_level": filtered.get("key_support_level", 0.0),
        "key_support_oi": filtered.get("key_support_oi", 0),
        "key_resistance_level": filtered.get("key_resistance_level", 0.0),
        "key_resistance_oi": filtered.get("key_resistance_oi", 0),
        "confluence_summary": confluence.get("confluence_summary", ""),
        "signal_strikes": filtered.get("signal_strikes", []),
        "all_strikes": filtered.get("all_strikes", []),
        "candles": confluence.get("candles", []),
        "markers": confluence.get("markers", []),
        "time_series": time_series,
        "stats": {
            "total_strikes": len(filtered.get("all_strikes", [])),
            "signal_strikes": filtered.get("signal_strike_count", 0),
            "retail_noise_filtered": filtered.get("filtered_out_count", 0),
            **confluence.get("stats", {}),
        },
    }
