"""
engine/mmxm.py — Market Maker Models (MMBM / MMSM) State Engine.
Based on Chapter 23 of the Ultimate ICT & SMC Course Masterclass.

Mechanical Principles:
1. Frozen Dealing Range at MSS Confirmation (M1):
   - Freeze dealing range strictly between Orig Cons extreme and SMR extreme at MSS bar.
   - Zero repainting / post-hoc expansion as new bars arrive.
2. Bar-by-Bar Phase State Machine (M2, M3):
   - Transitions tracked bar-by-bar chronologically:
     SMR_CONFIRMED -> LOW_RISK_ENTRY -> REACCUMULATION -> TARGET_REACHED.
   - Deterministic first touch of 20%–38% LRE zone recorded.
3. R-Potential Distinction (M4):
   - Reports realized R if LRE was traded, else theoretical.
4. Curve Quality Enforced (M5):
   - Multi-leg engineering curve requiring >= min_engineering_swings Lower Highs & Lows.
   - Rejects single-leg flushes / stop runs.
5. Dual-Marker Timing (M6):
   - Hollow dot at tentative SMR pivot.
   - Solid action badge strictly on candle where MSS body closes.
6. Confluence Transparency (M7):
   - Structured checklist (MSS, Displacement, FVG, SMT, POI) with minimum confluence gating.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .fvg import FVG
from .orderblocks import OrderBlock
from .primitives import Bars, Swing
from .structure import StructEvent

log = logging.getLogger(__name__)


def compute_mmxm(
    bars: Bars,
    atr_arr: np.ndarray,
    swings: List[Swing],
    events: List[StructEvent],
    fvgs: List[FVG],
    obs: List[OrderBlock],
    tf: str,
    rules: Optional[Dict[str, Any]] = None,
    smt_markers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Detects and tracks MMBM (Buy Model) and MMSM (Sell Model) curve lifecycles.
    Strictly chronological — zero repainting.
    """
    rules = rules or config.rules().get("mmxm", {})
    N = len(bars)
    if N < 10:
        return {"available": False, "models": [], "markers": [], "lines": [], "message": "Need >= 10 bars"}

    min_cons_bars = int(rules.get("orig_cons_min_bars", 4))
    max_cons_bars = int(rules.get("orig_cons_max_bars", 24))
    max_cons_atr = float(rules.get("orig_cons_max_atr", 3.5))
    min_eng_swings = int(rules.get("min_engineering_swings", 2))
    min_confluences = int(rules.get("min_smr_confluences", 2))
    allow_mss_only = bool(rules.get("allow_mss_only", False))
    target_buf = float(rules.get("target_buffer_pts", 4.0))
    lre_fib_min = float(rules.get("lre_fib_min", 0.20))
    lre_fib_max = float(rules.get("lre_fib_max", 0.38))
    reacc_fib_min = float(rules.get("reacc_fib_min", 0.50))
    reacc_fib_max = float(rules.get("reacc_fib_max", 0.75))

    smt_times = {int(m.get("time", 0)) for m in (smt_markers or [])}

    models: List[Dict[str, Any]] = []
    markers: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []

    # Iterate through structure events chronologically
    for mss in events:
        if mss.kind not in ("CHOCH", "MSS"):
            continue

        # ══════════════════════════════════════════════════════════════════════
        # 1. MARKET MAKER BUY MODEL (MMBM)
        # ══════════════════════════════════════════════════════════════════════
        if mss.dir == "bull":
            # SMR must be a prior swing low before this bullish MSS
            prior_lows = [s for s in swings if s.kind == "L" and s.i < mss.i]
            if not prior_lows:
                continue

            smr_swing = min(prior_lows[-4:], key=lambda s: s.price)
            smr_idx = smr_swing.i
            smr_price = smr_swing.price
            smr_ts = int(bars.ts[smr_idx])

            # 1. Search for Original Consolidation at highs before smr_idx
            start_i = max(0, smr_idx - 60)
            best_cons = None
            best_cons_high = -float("inf")
            best_lhs = 0
            best_lls = 0

            # Scan possible end bars for original consolidation
            for c_end in range(smr_idx - 2, max(start_i + min_cons_bars - 1, 0), -1):
                # Swings between c_end and smr_idx must satisfy multi-leg engineering curve
                curve_highs = [s for s in swings if s.kind == "H" and c_end < s.i < smr_idx]
                curve_lows = [s for s in swings if s.kind == "L" and c_end < s.i < smr_idx]

                for w in range(min_cons_bars, max_cons_bars + 1):
                    c_start = c_end - w
                    if c_start < start_i:
                        break
                    box_hi = float(np.max(bars.h[c_start : c_end + 1]))
                    box_lo = float(np.min(bars.l[c_start : c_end + 1]))
                    box_atr = float(np.mean(atr_arr[c_start : c_end + 1]))

                    if (box_hi - box_lo) <= max_cons_atr * box_atr and box_lo > smr_price + 1.5 * box_atr:
                        # Verify descending engineering structure (M5)
                        descending_lhs = 0
                        prev_h = box_hi
                        for sh in curve_highs:
                            if sh.price < prev_h:
                                descending_lhs += 1
                                prev_h = sh.price

                        descending_lls = 0
                        prev_l = box_lo
                        for sl in curve_lows:
                            if sl.price < prev_l:
                                descending_lls += 1
                                prev_l = sl.price

                        if descending_lhs >= min_eng_swings and descending_lls >= min_eng_swings:
                            if box_hi > best_cons_high:
                                best_cons_high = box_hi
                                best_lhs = descending_lhs
                                best_lls = descending_lls
                                best_cons = {
                                    "from_i": c_start,
                                    "to_i": c_end,
                                    "top": round(box_hi, 2),
                                    "bottom": round(box_lo, 2),
                                    "eq": round((box_hi + box_lo) / 2.0, 2),
                                    "start_time": chart_time(int(bars.ts[c_start])),
                                    "end_time": chart_time(int(bars.ts[c_end])),
                                }

            if not best_cons:
                continue

            orig_cons = best_cons
            descending_lhs = best_lhs
            descending_lls = best_lls

            # M7: Confluence Transparency & Checklist
            has_disp_fvg = any(g.dir == "bull" and smr_idx <= g.i <= mss.i + 3 for g in fvgs)
            has_smt = chart_time(smr_ts) in smt_times or any(
                abs(int(bars.ts[smr_idx]) - int(m.get("time", 0))) < 600 for m in (smt_markers or [])
            )
            poi_touched = any(g.bottom <= smr_price <= g.top for g in fvgs if g.i < smr_idx) or any(
                o.bottom <= smr_price <= o.top for o in obs if o.created_i < smr_idx
            )

            confluences = ["MSS Shift"]
            if mss.displacement:
                confluences.append("Displacement Candle")
            if has_disp_fvg:
                confluences.append("Displacement FVG")
            if has_smt:
                confluences.append("SMT Divergence")
            if poi_touched:
                confluences.append("HTF POI Key Level")

            confluence_score = len(confluences)
            if confluence_score < min_confluences and not allow_mss_only:
                continue

            # M1: Freeze Dealing Range at MSS confirmation (Orig Cons High to SMR Low)
            range_low = smr_price
            range_high = orig_cons["top"]
            dealing_range = range_high - range_low
            if dealing_range <= 0:
                continue

            target_px = orig_cons["top"]
            target_eq = orig_cons["eq"]

            lre_lo = smr_price + lre_fib_min * dealing_range
            lre_hi = smr_price + lre_fib_max * dealing_range
            reacc_lo = smr_price + reacc_fib_min * dealing_range
            reacc_hi = smr_price + reacc_fib_max * dealing_range

            # M2 & M3: Bar-by-Bar Phase State Machine starting strictly from MSS confirmation
            phase = "SMR_CONFIRMED"
            tested_lre = False
            lre_bar_i: Optional[int] = None
            lre_touch_price: Optional[float] = None
            transitions = [
                {
                    "phase": "SMR_CONFIRMED",
                    "bar_i": mss.i,
                    "time": chart_time(int(bars.ts[mss.i])),
                    "at": hm(int(bars.ts[mss.i])),
                    "price": round(float(bars.c[mss.i]), 2),
                }
            ]

            for k in range(mss.i + 1, N):
                # 1. Test for Low-Risk Entry (20%–38% retracement into discount)
                if phase == "SMR_CONFIRMED":
                    if bars.l[k] <= lre_hi and bars.h[k] >= lre_lo:
                        tested_lre = True
                        lre_bar_i = k
                        lre_touch_price = round(min(float(bars.o[k]), lre_hi), 2)
                        phase = "LOW_RISK_BUY"
                        transitions.append({
                            "phase": "LOW_RISK_BUY",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": lre_touch_price,
                        })

                # 2. Advance to Reaccumulation (50%–75% expansion)
                elif phase == "LOW_RISK_BUY":
                    if bars.h[k] >= reacc_lo:
                        phase = "REACCUMULATION"
                        transitions.append({
                            "phase": "REACCUMULATION",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": round(reacc_lo, 2),
                        })

                # 3. Delivery to Target
                elif phase == "REACCUMULATION":
                    if bars.h[k] >= target_px - target_buf:
                        phase = "TARGET_REACHED"
                        transitions.append({
                            "phase": "TARGET_REACHED",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": round(target_px, 2),
                        })

            # M4: R-Potential Realized vs Theoretical
            sl_price = round(smr_price - 2.0, 1)
            if tested_lre:
                entry_fill = lre_touch_price if lre_touch_price is not None else lre_hi
                r_type = "realized"
                r_potential = round((target_eq - entry_fill) / max(1.0, (entry_fill - sl_price)), 1)
            else:
                entry_fill = lre_hi
                r_type = "theoretical"
                r_potential = round((target_eq - entry_fill) / max(1.0, (entry_fill - sl_price)), 1)

            mss_ts = int(bars.ts[mss.i])
            models.append({
                "id": f"MMBM_{smr_ts}",
                "model": "MMBM",
                "name": "Market Maker Buy Model",
                "dir": "bull",
                "phase": phase,
                "transitions": transitions,
                "orig_cons": orig_cons,
                "smr": {
                    "time": chart_time(smr_ts),
                    "at": hm(smr_ts),
                    "price": smr_price,
                    "confluences": confluences,
                    "confluence_score": confluence_score,
                },
                "mss": {
                    "time": chart_time(mss_ts),
                    "at": hm(mss_ts),
                    "level": mss.level,
                    "displacement": mss.displacement,
                },
                "dealing_range": {
                    "low": round(range_low, 2),
                    "high": round(range_high, 2),
                    "range": round(dealing_range, 2),
                },
                "lre_zone": [round(lre_lo, 1), round(lre_hi, 1)],
                "tested_lre": tested_lre,
                "lre_touch_bar": lre_bar_i,
                "target": target_px,
                "target_eq": target_eq,
                "sl": sl_price,
                "r_potential": r_potential,
                "r_type": r_type,
                "summary": (
                    f"MMBM with {descending_lhs} distribution legs. Phase: {phase.replace('_', ' ')}. "
                    f"Target Orig Cons @ {target_px:.0f} (Target EQ {target_eq:.0f})."
                ),
            })

            # M6: Distinct Markers:
            # MARKER 1: HOLLOW DOT at Tentative SMR Pivot Low (timestamp of pivot)
            markers.append({
                "time": chart_time(smr_ts),
                "kind": "MMBM_SMR_PIVOT",
                "dir": "bull",
                "price": smr_price,
                "shape": "circle",
                "color": "rgba(232,195,90,0.45)",
                "text": f"SMR Pivot Low ({hm(smr_ts)})",
                "at": hm(smr_ts),
                "tooltip": f"Tentative SMR Pivot Low at {hm(smr_ts)}. Confirmed at {hm(mss_ts)}",
            })

            # MARKER 2: SOLID BADGE strictly at MSS Confirmation bar (timestamp of confirmation)
            markers.append({
                "time": chart_time(mss_ts),
                "kind": "MMBM_SMR_CONFIRMED",
                "dir": "bull",
                "price": mss.level,
                "shape": "arrowUp",
                "color": "#e8c35a",
                "text": f"MMBM Confirmed ({confluence_score} Conf)",
                "at": hm(mss_ts),
                "confirmation_at": hm(mss_ts),
                "confluences": confluences,
                "anchor_price": smr_price,
                "tooltip": f"MMBM confirmed at {hm(mss_ts)} after SMR low {smr_price:.1f} ({hm(smr_ts)})",
            })

        # ══════════════════════════════════════════════════════════════════════
        # 2. MARKET MAKER SELL MODEL (MMSM)
        # ══════════════════════════════════════════════════════════════════════
        elif mss.dir == "bear":
            prior_highs = [s for s in swings if s.kind == "H" and s.i < mss.i]
            if not prior_highs:
                continue

            smr_swing = max(prior_highs[-4:], key=lambda s: s.price)
            smr_idx = smr_swing.i
            smr_price = smr_swing.price
            smr_ts = int(bars.ts[smr_idx])

            start_i = max(0, smr_idx - 60)
            best_cons = None
            best_cons_low = float("inf")
            best_lhs = 0
            best_lls = 0

            # Scan possible end bars for original consolidation
            for c_end in range(smr_idx - 2, max(start_i + min_cons_bars - 1, 0), -1):
                curve_highs = [s for s in swings if s.kind == "H" and c_end < s.i < smr_idx]
                curve_lows = [s for s in swings if s.kind == "L" and c_end < s.i < smr_idx]

                for w in range(min_cons_bars, max_cons_bars + 1):
                    c_start = c_end - w
                    if c_start < start_i:
                        break
                    box_hi = float(np.max(bars.h[c_start : c_end + 1]))
                    box_lo = float(np.min(bars.l[c_start : c_end + 1]))
                    box_atr = float(np.mean(atr_arr[c_start : c_end + 1]))

                    if (box_hi - box_lo) <= max_cons_atr * box_atr and box_hi < smr_price - 1.5 * box_atr:
                        # Verify ascending engineering structure (M5)
                        ascending_lhs = 0
                        prev_h = box_hi
                        for sh in curve_highs:
                            if sh.price > prev_h:
                                ascending_lhs += 1
                                prev_h = sh.price

                        ascending_lls = 0
                        prev_l = box_lo
                        for sl in curve_lows:
                            if sl.price > prev_l:
                                ascending_lls += 1
                                prev_l = sl.price

                        if ascending_lhs >= min_eng_swings and ascending_lls >= min_eng_swings:
                            if box_lo < best_cons_low:
                                best_cons_low = box_lo
                                best_lhs = ascending_lhs
                                best_lls = ascending_lls
                                best_cons = {
                                    "from_i": c_start,
                                    "to_i": c_end,
                                    "top": round(box_hi, 2),
                                    "bottom": round(box_lo, 2),
                                    "eq": round((box_hi + box_lo) / 2.0, 2),
                                    "start_time": chart_time(int(bars.ts[c_start])),
                                    "end_time": chart_time(int(bars.ts[c_end])),
                                }

            if not best_cons:
                continue

            orig_cons = best_cons
            ascending_lhs = best_lhs
            ascending_lls = best_lls

            has_disp_fvg = any(g.dir == "bear" and smr_idx <= g.i <= mss.i + 3 for g in fvgs)
            has_smt = chart_time(smr_ts) in smt_times or any(
                abs(int(bars.ts[smr_idx]) - int(m.get("time", 0))) < 600 for m in (smt_markers or [])
            )
            poi_touched = any(g.bottom <= smr_price <= g.top for g in fvgs if g.i < smr_idx) or any(
                o.bottom <= smr_price <= o.top for o in obs if o.created_i < smr_idx
            )

            confluences = ["MSS Shift"]
            if mss.displacement:
                confluences.append("Displacement Candle")
            if has_disp_fvg:
                confluences.append("Displacement FVG")
            if has_smt:
                confluences.append("SMT Divergence")
            if poi_touched:
                confluences.append("HTF POI Key Level")

            confluence_score = len(confluences)
            if confluence_score < min_confluences and not allow_mss_only:
                continue

            # M1: Freeze Dealing Range at MSS confirmation (SMR High to Orig Cons Low)
            range_high = smr_price
            range_low = orig_cons["bottom"]
            dealing_range = range_high - range_low
            if dealing_range <= 0:
                continue

            target_px = orig_cons["bottom"]
            target_eq = orig_cons["eq"]

            lre_hi = smr_price - lre_fib_min * dealing_range
            lre_lo = smr_price - lre_fib_max * dealing_range
            redist_hi = smr_price - reacc_fib_min * dealing_range
            redist_lo = smr_price - reacc_fib_max * dealing_range

            phase = "SMR_CONFIRMED"
            tested_lre = False
            lre_bar_i: Optional[int] = None
            lre_touch_price: Optional[float] = None
            transitions = [
                {
                    "phase": "SMR_CONFIRMED",
                    "bar_i": mss.i,
                    "time": chart_time(int(bars.ts[mss.i])),
                    "at": hm(int(bars.ts[mss.i])),
                    "price": round(float(bars.c[mss.i]), 2),
                }
            ]

            for k in range(mss.i + 1, N):
                if phase == "SMR_CONFIRMED":
                    if bars.h[k] >= lre_lo and bars.l[k] <= lre_hi:
                        tested_lre = True
                        lre_bar_i = k
                        lre_touch_price = round(max(float(bars.o[k]), lre_lo), 2)
                        phase = "LOW_RISK_SELL"
                        transitions.append({
                            "phase": "LOW_RISK_SELL",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": lre_touch_price,
                        })
                elif phase == "LOW_RISK_SELL":
                    if bars.l[k] <= redist_hi:
                        phase = "REDISTRIBUTION"
                        transitions.append({
                            "phase": "REDISTRIBUTION",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": round(redist_hi, 2),
                        })
                elif phase == "REDISTRIBUTION":
                    if bars.l[k] <= target_px + target_buf:
                        phase = "TARGET_REACHED"
                        transitions.append({
                            "phase": "TARGET_REACHED",
                            "bar_i": k,
                            "time": chart_time(int(bars.ts[k])),
                            "at": hm(int(bars.ts[k])),
                            "price": round(target_px, 2),
                        })

            sl_price = round(smr_price + 2.0, 1)
            if tested_lre:
                entry_fill = lre_touch_price if lre_touch_price is not None else lre_lo
                r_type = "realized"
                r_potential = round(abs(entry_fill - target_eq) / max(1.0, (sl_price - entry_fill)), 1)
            else:
                entry_fill = lre_lo
                r_type = "theoretical"
                r_potential = round(abs(entry_fill - target_eq) / max(1.0, (sl_price - entry_fill)), 1)

            mss_ts = int(bars.ts[mss.i])
            models.append({
                "id": f"MMSM_{smr_ts}",
                "model": "MMSM",
                "name": "Market Maker Sell Model",
                "dir": "bear",
                "phase": phase,
                "transitions": transitions,
                "orig_cons": orig_cons,
                "smr": {
                    "time": chart_time(smr_ts),
                    "at": hm(smr_ts),
                    "price": smr_price,
                    "confluences": confluences,
                    "confluence_score": confluence_score,
                },
                "mss": {
                    "time": chart_time(mss_ts),
                    "at": hm(mss_ts),
                    "level": mss.level,
                    "displacement": mss.displacement,
                },
                "dealing_range": {
                    "low": round(range_low, 2),
                    "high": round(range_high, 2),
                    "range": round(dealing_range, 2),
                },
                "lre_zone": [round(lre_lo, 1), round(lre_hi, 1)],
                "tested_lre": tested_lre,
                "lre_touch_bar": lre_bar_i,
                "target": target_px,
                "target_eq": target_eq,
                "sl": sl_price,
                "r_potential": r_potential,
                "r_type": r_type,
                "summary": (
                    f"MMSM with {ascending_lhs} accumulation legs. Phase: {phase.replace('_', ' ')}. "
                    f"Target Orig Cons @ {target_px:.0f} (Target EQ {target_eq:.0f})."
                ),
            })

            markers.append({
                "time": chart_time(smr_ts),
                "kind": "MMSM_SMR_PIVOT",
                "dir": "bear",
                "price": smr_price,
                "shape": "circle",
                "color": "rgba(244,114,182,0.45)",
                "text": f"SMR Pivot High ({hm(smr_ts)})",
                "at": hm(smr_ts),
                "tooltip": f"Tentative SMR Pivot High at {hm(smr_ts)}. Confirmed at {hm(mss_ts)}",
            })

            markers.append({
                "time": chart_time(mss_ts),
                "kind": "MMSM_SMR_CONFIRMED",
                "dir": "bear",
                "price": mss.level,
                "shape": "arrowDown",
                "color": "#f472b6",
                "text": f"MMSM Confirmed ({confluence_score} Conf)",
                "at": hm(mss_ts),
                "confirmation_at": hm(mss_ts),
                "confluences": confluences,
                "anchor_price": smr_price,
                "tooltip": f"MMSM confirmed at {hm(mss_ts)} after SMR high {smr_price:.1f} ({hm(smr_ts)})",
            })

    # Add Target Lines for the most recent model
    if models:
        last_m = models[-1]
        lines.append({
            "label": f"{last_m['model']} Target ({last_m['target']:.0f})",
            "price": last_m["target"],
            "dir": last_m["dir"],
            "style": "dashed",
        })

    # Stats Summary
    completed = sum(1 for m in models if m["phase"] == "TARGET_REACHED")
    completion_rate = round(completed / len(models) * 100, 1) if models else 0.0
    avg_r = round(float(np.mean([m["r_potential"] for m in models])), 1) if models else 0.0

    stats = {
        "total_cycles": len(models),
        "completed": completed,
        "completion_rate": completion_rate,
        "avg_r_potential": avg_r,
    }

    return {
        "available": True,
        "models": models,
        "primary": models[-1] if models else None,
        "markers": markers,
        "lines": lines,
        "stats": stats,
        "message": f"{len(models)} active MMXM cycles tracked" if models else "No active MMXM cycle in window",
    }
