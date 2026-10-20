"""
engine/pa_advanced.py — 10 Advanced Institutional Price Action & Market Structure Concepts
derived from the 25-chapter master curriculum.

Strictly adheres to the engine contract:
  * Closed candles only (Bars, Swing, FVG, OrderBlock). Zero lookahead.
  * Pure Python + NumPy. NO pandas / scipy imports.
  * Informational & alert-only: NEVER touches Playbook Setups A/B/C/D or paper trades.

The 10 Concepts:
  1. Strong vs. Weak MSB Classifier & 7-Point Break Quality Scorer (Ch. 2, Ch. 3, Ch. 12, Ch. 25)
  2. "Dragon Fruit" Confluence Detector (OB + FVG Touching Sweet Spot — Ch. 4, Ch. 15)
  3. Mitigation Block (MiB) Engine (Failure Swings vs. Breaker Sweeps — Ch. 16)
  4. ROTE — Reverse Optimal Trade Entry 4-Step Algorithmic Counter-Bot (Ch. 9, Ch. 11)
  5. Pure SFP vs. Considerably-SFP (cSFP) vs. Gray Accumulation Engine (Ch. 11)
  6. RBR / DBD / RBD / DBR Institutional Supply & Demand Base Scanner (Ch. 15)
  7. FTR — Failed To Return / Indecision Candle "Bagholder" Zone (Ch. 20)
  8. TT3 — Three Tap Range Extremity Setup (Ch. 18, Ch. 19)
  9. GPX — Gameplay Box HTF Corridor + LTF VWAP Wave Rider (Ch. 8, Ch. 17)
  10. Trap & Inducement Veto Engine (EQH/EQL + FVG Magnet — Ch. 10, Ch. 25)
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .fvg import FVG
from .liquidity import Pool
from .orderblocks import OrderBlock
from .primitives import Bars, Swing, body_ratio


def R() -> Dict[str, Any]:
    return config.rules().get("pa_advanced", {})


# ── Concept 1: Strong vs. Weak MSB & 7-Point Scorer ───────────────────────────

@dataclass
class MSBQuality:
    i: int
    ts: int
    time_str: str
    dir: str                       # "bull" | "bear"
    level: float
    origin_swing_price: float
    extreme_swing_price: float
    strong: bool
    weak_type: Optional[str]       # None | "WICK_STRUGGLE" | "INTERNAL_SUB_RANGE"
    score: int                     # 0 to 7
    scorecard: Dict[str, bool]
    close_price: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_msb_quality(
    b: Bars,
    a: np.ndarray,
    swings: List[Swing],
    obs: List[OrderBlock],
    fvgs: List[FVG],
    round_levels: List[float],
    cfg: Dict[str, Any],
) -> List[MSBQuality]:
    """Evaluates every structural break relative to the true origin swing of the deepest extreme."""
    N = len(b)
    if N < 3 or not swings:
        return []

    c_disp = cfg.get("displacement", {})
    disp_mult = float(c_disp.get("disp_body_atr_mult", 1.05))
    disp_min_ratio = float(c_disp.get("disp_body_ratio_min", 0.60))

    c_msb = cfg.get("msb_break_quality", {})
    decisive_atr = float(c_msb.get("decisive_close_atr", 0.20))
    consol_max_bars = int(c_msb.get("consolidation_max_bars", 2))
    consol_band_atr = float(c_msb.get("consolidation_band_atr", 0.25))
    pullback_ratio = float(c_msb.get("pullback_slowness_ratio", 1.40))
    launch_mult = float(c_msb.get("engulfing_launch_mult", 1.25))

    results: List[MSBQuality] = []
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]

    # 1. Bullish MSB: Breaks Lower High that created the deepest Lower Low
    for k, ll in enumerate(lows):
        # Find preceding swing high that led to this low
        preceding_highs = [h for h in highs if h.i < ll.i]
        if not preceding_highs:
            continue
        lh = preceding_highs[-1]
        level = lh.price

        # Search for first break after confirmation of LH
        start_i = max(lh.confirm_i, ll.confirm_i)
        for i in range(start_i, N):
            pierced = b.h[i] > level
            closed_above = b.c[i] > level
            if not pierced:
                continue

            # Evaluate break
            atr_i = float(a[i]) if i < len(a) else 10.0
            body_i = abs(b.c[i] - b.o[i])
            rng_i = max(b.h[i] - b.l[i], 1e-4)
            b_ratio = body_i / rng_i

            is_green = b.c[i] > b.o[i]
            is_disp = is_green and (body_i >= disp_mult * atr_i) and (b_ratio >= disp_min_ratio)
            decisive_close = closed_above and (b.c[i] >= level + decisive_atr * atr_i)

            # Check weak type
            weak_type = None
            if not closed_above:
                weak_type = "WICK_STRUGGLE"
            elif not is_disp and not decisive_close:
                weak_type = "WICK_STRUGGLE"

            # Check consolidation bars prior to break
            consol_bars = 0
            for j in range(max(0, i - 4), i):
                if abs(b.c[j] - level) <= consol_band_atr * atr_i:
                    consol_bars += 1
            one_shot = consol_bars <= consol_max_bars

            # Check FVG created in departure leg (within +- 2 bars)
            has_fvg = any(abs(g.i - i) <= 2 and g.dir == "bull" for g in fvgs)

            # Check engulfing launch candle at origin OB
            engulfing_launch = False
            for o in obs:
                if o.dir == "bull" and abs(o.i - ll.i) <= 3:
                    if o.i > 0 and abs(b.c[o.i] - b.o[o.i]) >= launch_mult * max(abs(b.c[o.i - 1] - b.o[o.i - 1]), 1.0):
                        engulfing_launch = True
                        break

            # Fast impulse vs slow pullback
            impulse_bars = max(1, i - ll.i)
            # Look at subsequent bars for pullback slowness
            pullback_bars = 0
            for p_idx in range(i + 1, min(N, i + 1 + int(impulse_bars * pullback_ratio) + 2)):
                if b.c[p_idx] < b.c[i]:
                    pullback_bars += 1
            slow_pullback = (pullback_bars >= int(impulse_bars * pullback_ratio)) or (i >= N - 2)

            # Confluence with round number or keylevel
            confluence = any(abs(ll.price - lvl) <= 12.0 for lvl in round_levels) or (abs(level % 100.0) <= 6.0)

            # Freshness / First Time Back
            ftb = True  # first break instance

            scorecard = {
                "strong_body_close": bool(decisive_close),
                "one_shot_break": bool(one_shot),
                "fvg_created": bool(has_fvg),
                "engulfing_launch": bool(engulfing_launch),
                "fast_impulse_slow_pullback": bool(slow_pullback),
                "key_level_confluence": bool(confluence),
                "first_time_back": bool(ftb),
            }
            score = sum(1 for v in scorecard.values() if v)
            strong = decisive_close and is_disp and (score >= 4)

            results.append(
                MSBQuality(
                    i=i,
                    ts=int(b.ts[i]),
                    time_str=hm(int(b.ts[i])),
                    dir="bull",
                    level=round(level, 2),
                    origin_swing_price=round(lh.price, 2),
                    extreme_swing_price=round(ll.price, 2),
                    strong=strong,
                    weak_type=weak_type,
                    score=score,
                    scorecard=scorecard,
                    close_price=round(float(b.c[i]), 2),
                )
            )
            break  # only process first break of this swing

    # 2. Bearish MSB: Breaks Higher Low that created the deepest Highest High
    for k, hh in enumerate(highs):
        preceding_lows = [l for l in lows if l.i < hh.i]
        if not preceding_lows:
            continue
        hl = preceding_lows[-1]
        level = hl.price

        start_i = max(hl.confirm_i, hh.confirm_i)
        for i in range(start_i, N):
            pierced = b.l[i] < level
            closed_below = b.c[i] < level
            if not pierced:
                continue

            atr_i = float(a[i]) if i < len(a) else 10.0
            body_i = abs(b.c[i] - b.o[i])
            rng_i = max(b.h[i] - b.l[i], 1e-4)
            b_ratio = body_i / rng_i

            is_red = b.c[i] < b.o[i]
            is_disp = is_red and (body_i >= disp_mult * atr_i) and (b_ratio >= disp_min_ratio)
            decisive_close = closed_below and (b.c[i] <= level - decisive_atr * atr_i)

            weak_type = None
            if not closed_below:
                weak_type = "WICK_STRUGGLE"
            elif not is_disp and not decisive_close:
                weak_type = "WICK_STRUGGLE"

            consol_bars = 0
            for j in range(max(0, i - 4), i):
                if abs(b.c[j] - level) <= consol_band_atr * atr_i:
                    consol_bars += 1
            one_shot = consol_bars <= consol_max_bars

            has_fvg = any(abs(g.i - i) <= 2 and g.dir == "bear" for g in fvgs)

            engulfing_launch = False
            for o in obs:
                if o.dir == "bear" and abs(o.i - hh.i) <= 3:
                    if o.i > 0 and abs(b.c[o.i] - b.o[o.i]) >= launch_mult * max(abs(b.c[o.i - 1] - b.o[o.i - 1]), 1.0):
                        engulfing_launch = True
                        break

            impulse_bars = max(1, i - hh.i)
            pullback_bars = 0
            for p_idx in range(i + 1, min(N, i + 1 + int(impulse_bars * pullback_ratio) + 2)):
                if b.c[p_idx] > b.c[i]:
                    pullback_bars += 1
            slow_pullback = (pullback_bars >= int(impulse_bars * pullback_ratio)) or (i >= N - 2)

            confluence = any(abs(hh.price - lvl) <= 12.0 for lvl in round_levels) or (abs(level % 100.0) <= 6.0)
            ftb = True

            scorecard = {
                "strong_body_close": bool(decisive_close),
                "one_shot_break": bool(one_shot),
                "fvg_created": bool(has_fvg),
                "engulfing_launch": bool(engulfing_launch),
                "fast_impulse_slow_pullback": bool(slow_pullback),
                "key_level_confluence": bool(confluence),
                "first_time_back": bool(ftb),
            }
            score = sum(1 for v in scorecard.values() if v)
            strong = decisive_close and is_disp and (score >= 4)

            results.append(
                MSBQuality(
                    i=i,
                    ts=int(b.ts[i]),
                    time_str=hm(int(b.ts[i])),
                    dir="bear",
                    level=round(level, 2),
                    origin_swing_price=round(hl.price, 2),
                    extreme_swing_price=round(hh.price, 2),
                    strong=strong,
                    weak_type=weak_type,
                    score=score,
                    scorecard=scorecard,
                    close_price=round(float(b.c[i]), 2),
                )
            )
            break

    results.sort(key=lambda x: x.i)
    return results


# ── Concept 2: "Dragon Fruit" Confluence Detector (OB + FVG Touching) ─────────

@dataclass
class DragonFruitZone:
    ob_i: int
    fvg_i: int
    dir: str                       # "bull" | "bear"
    top: float
    bottom: float
    risk: str                      # "LOW" (Dragon fruit sweet spot) | "MID" | "HIGH"
    gap_pts: float
    ts: int
    time_str: str
    label: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_dragon_fruit(
    obs: List[OrderBlock],
    fvgs: List[FVG],
    a: np.ndarray,
    cfg: Dict[str, Any],
) -> List[DragonFruitZone]:
    """Stratifies FVG risk and flags the Dragon Fruit sweet spot where OB & FVG touch."""
    c_df = cfg.get("dragon_fruit", {})
    max_touch_gap = float(c_df.get("max_touch_gap_pts", 5.0))
    touch_tol_atr = float(c_df.get("touch_tolerance_atr", 0.15))

    out: List[DragonFruitZone] = []
    for o in obs:
        if o.state in ("broken", "expired"):
            continue
        same_dir_fvgs = [g for g in fvgs if g.dir == o.dir and abs(g.i - o.i) <= 12]
        if not same_dir_fvgs:
            continue

        atr_val = float(a[min(o.i, len(a) - 1)]) if len(a) else 10.0
        eff_gap_thresh = max(max_touch_gap, touch_tol_atr * atr_val)

        # Sort FVGs by proximity to OB
        for g in same_dir_fvgs:
            if o.dir == "bull":
                gap = g.bottom - o.top
                is_touching = (-0.25 * atr_val <= gap <= eff_gap_thresh)
            else:
                gap = o.bottom - g.top
                is_touching = (-0.25 * atr_val <= gap <= eff_gap_thresh)

            if is_touching:
                risk = "LOW"
                label = f"Dragon Fruit A+ ({'Demand' if o.dir == 'bull' else 'Supply'})"
            elif abs(g.i - o.i) <= 4:
                risk = "MID"
                label = f"Mid-Risk FVG ({'Bull' if g.dir == 'bull' else 'Bear'})"
            else:
                risk = "HIGH"
                label = f"Break-Proximity FVG ({'Bull' if g.dir == 'bull' else 'Bear'})"

            combined_top = max(o.top, g.top)
            combined_bottom = min(o.bottom, g.bottom)

            out.append(
                DragonFruitZone(
                    ob_i=o.i,
                    fvg_i=g.i,
                    dir=o.dir,
                    top=round(combined_top, 2),
                    bottom=round(combined_bottom, 2),
                    risk=risk,
                    gap_pts=round(float(gap), 2),
                    ts=int(o.ts),
                    time_str=hm(int(o.ts)),
                    label=label,
                )
            )
    return out


# ── Concept 3: Mitigation Block (MiB) Engine ──────────────────────────────────

@dataclass
class MitigationBlock:
    dir: str                       # "bull" | "bear"
    i: int                         # candle of the failed swing
    ts: int
    time_str: str
    top: float
    bottom: float
    broken_i: int                  # bar that broke the interim swing
    fail_swing_price: float
    interim_swing_price: float
    mitigated: bool = False
    mitigated_i: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_mitigation_blocks(
    b: Bars,
    swings: List[Swing],
    a: np.ndarray,
    cfg: Dict[str, Any],
) -> List[MitigationBlock]:
    """Identifies failure swing Mitigation Blocks (unlike Breakers, no sweep occurs)."""
    N = len(b)
    if N < 4 or len(swings) < 3:
        return []

    c_mib = cfg.get("mitigation_block", {})
    max_age = int(c_mib.get("max_age_bars", 120))
    out: List[MitigationBlock] = []

    # Iterate through triplet sequences of swings
    for idx in range(2, len(swings)):
        s1, s2, s3 = swings[idx - 2], swings[idx - 1], swings[idx]

        # Bearish MiB: H1 -> L1 -> LH (Failure swing: H2 < H1) -> break below L1
        if s1.kind == "H" and s2.kind == "L" and s3.kind == "H":
            if s3.price < s1.price:  # Failure swing (Lower High, no sweep!)
                interim_level = s2.price
                # Check when price breaks below s2.price
                for j in range(s3.confirm_i, min(N, s3.confirm_i + max_age)):
                    if b.c[j] < interim_level:
                        # Origin candle at s3
                        top = max(float(b.h[s3.i]), float(b.c[s3.i]))
                        bot = min(float(b.l[s3.i]), float(b.o[s3.i]))
                        # Check if mitigated later
                        mitigated = False
                        mit_i = None
                        for m in range(j + 1, N):
                            if b.h[m] >= bot:
                                mitigated = True
                                mit_i = m
                                break
                        out.append(
                            MitigationBlock(
                                dir="bear",
                                i=s3.i,
                                ts=int(b.ts[s3.i]),
                                time_str=hm(int(b.ts[s3.i])),
                                top=round(top, 2),
                                bottom=round(bot, 2),
                                broken_i=j,
                                fail_swing_price=round(s3.price, 2),
                                interim_swing_price=round(interim_level, 2),
                                mitigated=mitigated,
                                mitigated_i=mit_i,
                            )
                        )
                        break

        # Bullish MiB: L1 -> H1 -> HL (Failure swing: L2 > L1) -> break above H1
        elif s1.kind == "L" and s2.kind == "H" and s3.kind == "L":
            if s3.price > s1.price:  # Failure swing (Higher Low, no sweep!)
                interim_level = s2.price
                for j in range(s3.confirm_i, min(N, s3.confirm_i + max_age)):
                    if b.c[j] > interim_level:
                        top = max(float(b.h[s3.i]), float(b.o[s3.i]))
                        bot = min(float(b.l[s3.i]), float(b.c[s3.i]))
                        mitigated = False
                        mit_i = None
                        for m in range(j + 1, N):
                            if b.l[m] <= top:
                                mitigated = True
                                mit_i = m
                                break
                        out.append(
                            MitigationBlock(
                                dir="bull",
                                i=s3.i,
                                ts=int(b.ts[s3.i]),
                                time_str=hm(int(b.ts[s3.i])),
                                top=round(top, 2),
                                bottom=round(bot, 2),
                                broken_i=j,
                                fail_swing_price=round(s3.price, 2),
                                interim_swing_price=round(interim_level, 2),
                                mitigated=mitigated,
                                mitigated_i=mit_i,
                            )
                        )
                        break

    out.sort(key=lambda x: x.i)
    return out


# ── Concept 4: ROTE — Reverse Optimal Trade Entry ─────────────────────────────

@dataclass
class ROTESetup:
    dir: str                       # "bull" | "bear"
    target_type: str               # "EQH" | "EQL" | "POOL"
    target_price: float
    sweep_price: float
    eq_050: float
    entry_low: float
    entry_high: float
    tp1_low: float
    tp1_high: float
    tp2: float
    sl: float
    status: str                    # "ARMED" | "IN_ZONE" | "TRIGGERED" | "EXPIRED"
    i: int
    ts: int
    time_str: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_rote(
    b: Bars,
    a: np.ndarray,
    swings: List[Swing],
    pools: List[Pool],
    cfg: Dict[str, Any],
) -> List[ROTESetup]:
    """4-step algorithmic counter-bot framework targeting pre-identified liquidity magnets."""
    N = len(b)
    if N < 4 or not pools or not swings:
        return []

    c_rote = cfg.get("rote", {})
    entry_lo_pct = float(c_rote.get("rote_fib_entry_low", 0.70))
    entry_hi_pct = float(c_rote.get("rote_fib_entry_high", 0.80))
    tp1_lo_pct = float(c_rote.get("rote_tp1_low", 0.20))
    tp1_hi_pct = float(c_rote.get("rote_tp1_high", 0.30))
    sl_buf_atr = float(c_rote.get("sl_buffer_atr", 0.10))

    out: List[ROTESetup] = []

    # 1. Bearish ROTE: Target is EQL below, Sweep is Swing High above
    eql_pools = [p for p in pools if p.side == "SSL" and p.src in ("EQL", "SWING")]
    for p in eql_pools:
        target_px = p.price
        # Look for opposing sweep high above target
        for sh in [s for s in swings if s.kind == "H" and s.i >= p.known_i and s.price > target_px + 20.0]:
            rng = sh.price - target_px
            if rng < 15.0:
                continue
            eq = target_px + 0.50 * rng
            e_hi = target_px + entry_hi_pct * rng
            e_lo = target_px + entry_lo_pct * rng
            tp1_lo = target_px + tp1_lo_pct * rng
            tp1_hi = target_px + tp1_hi_pct * rng
            sl = sh.price + sl_buf_atr * (float(a[sh.i]) if sh.i < len(a) else 10.0)

            # Check if price crossed 0.50 EQ after sweep
            crossed_eq = False
            status = "ARMED"
            for k in range(sh.confirm_i, N):
                if b.l[k] < eq:
                    crossed_eq = True
                if crossed_eq and (e_lo <= b.h[k] <= e_hi):
                    status = "IN_ZONE"
                if status == "IN_ZONE" and b.c[k] < b.o[k]:
                    status = "TRIGGERED"
                if b.c[k] > sl or b.l[k] <= target_px:
                    status = "EXPIRED"

            if crossed_eq:
                out.append(
                    ROTESetup(
                        dir="bear",
                        target_type=p.src,
                        target_price=round(target_px, 2),
                        sweep_price=round(sh.price, 2),
                        eq_050=round(eq, 2),
                        entry_low=round(e_lo, 2),
                        entry_high=round(e_hi, 2),
                        tp1_low=round(tp1_lo, 2),
                        tp1_high=round(tp1_hi, 2),
                        tp2=round(target_px, 2),
                        sl=round(sl, 2),
                        status=status,
                        i=sh.i,
                        ts=int(b.ts[sh.i]),
                        time_str=hm(int(b.ts[sh.i])),
                    )
                )

    # 2. Bullish ROTE: Target is EQH above, Sweep is Swing Low below
    eqh_pools = [p for p in pools if p.side == "BSL" and p.src in ("EQH", "SWING")]
    for p in eqh_pools:
        target_px = p.price
        for sl_pt in [s for s in swings if s.kind == "L" and s.i >= p.known_i and s.price < target_px - 20.0]:
            rng = target_px - sl_pt.price
            if rng < 15.0:
                continue
            eq = target_px - 0.50 * rng
            e_hi = target_px - entry_lo_pct * rng
            e_lo = target_px - entry_hi_pct * rng
            tp1_lo = target_px - tp1_hi_pct * rng
            tp1_hi = target_px - tp1_lo_pct * rng
            sl = sl_pt.price - sl_buf_atr * (float(a[sl_pt.i]) if sl_pt.i < len(a) else 10.0)

            crossed_eq = False
            status = "ARMED"
            for k in range(sl_pt.confirm_i, N):
                if b.h[k] > eq:
                    crossed_eq = True
                if crossed_eq and (e_lo <= b.l[k] <= e_hi):
                    status = "IN_ZONE"
                if status == "IN_ZONE" and b.c[k] > b.o[k]:
                    status = "TRIGGERED"
                if b.c[k] < sl or b.h[k] >= target_px:
                    status = "EXPIRED"

            if crossed_eq:
                out.append(
                    ROTESetup(
                        dir="bull",
                        target_type=p.src,
                        target_price=round(target_px, 2),
                        sweep_price=round(sl_pt.price, 2),
                        eq_050=round(eq, 2),
                        entry_low=round(e_lo, 2),
                        entry_high=round(e_hi, 2),
                        tp1_low=round(tp1_lo, 2),
                        tp1_high=round(tp1_hi, 2),
                        tp2=round(target_px, 2),
                        sl=round(sl, 2),
                        status=status,
                        i=sl_pt.i,
                        ts=int(b.ts[sl_pt.i]),
                        time_str=hm(int(b.ts[sl_pt.i])),
                    )
                )

    out.sort(key=lambda x: x.i)
    return out[-6:]  # keep recent setups


# ── Concept 5: Pure SFP vs. cSFP vs. Gray Accumulation ────────────────────────

@dataclass
class SFPClassification:
    swing_i: int
    swing_price: float
    kind: str                      # "H" | "L"
    test_i: int
    ts: int
    time_str: str
    state: str                     # "PURE_SFP" | "CSFP" | "GRAY_ACCUMULATION" | "GENUINE_BREAK"
    pierce_atr: float
    bars_hovered: int
    reversal_confirmed: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_sfp_tests(
    b: Bars,
    swings: List[Swing],
    a: np.ndarray,
    cfg: Dict[str, Any],
) -> List[SFPClassification]:
    """Classifies tests of major swings into 4 institutional states."""
    N = len(b)
    if N < 3 or not swings:
        return []

    c_sfp = cfg.get("sfp_classifier", {})
    csfp_max_pierce = float(c_sfp.get("csfp_max_pierce_atr", 0.18))
    gray_min_bars = int(c_sfp.get("gray_accum_min_bars", 3))
    gray_dev_atr = float(c_sfp.get("gray_accum_dev_atr", 0.20))

    out: List[SFPClassification] = []

    for s in swings:
        level = s.price
        test_bars = [j for j in range(s.confirm_i, N) if abs(b.c[j] - level) <= 0.80 * float(a[j])]
        if not test_bars:
            continue

        for idx, j in enumerate(test_bars):
            atr_j = float(a[j]) if j < len(a) else 10.0

            # 1. Gray Area Accumulation: 3+ candles hovering tightly
            hover_count = 0
            for h_idx in range(max(0, j - 4), min(N, j + 1)):
                if abs(b.c[h_idx] - level) <= gray_dev_atr * atr_j:
                    hover_count += 1
            if hover_count >= gray_min_bars:
                out.append(
                    SFPClassification(
                        swing_i=s.i,
                        swing_price=round(level, 2),
                        kind=s.kind,
                        test_i=j,
                        ts=int(b.ts[j]),
                        time_str=hm(int(b.ts[j])),
                        state="GRAY_ACCUMULATION",
                        pierce_atr=round(abs(b.c[j] - level) / max(atr_j, 1.0), 2),
                        bars_hovered=hover_count,
                        reversal_confirmed=False,
                    )
                )
                break

            # 2. Pure SFP vs cSFP for Swing High
            if s.kind == "H":
                if b.h[j] > level and b.c[j] < level:
                    # Pure SFP: wick pierces, body strictly inside
                    pierce = (b.h[j] - level) / max(atr_j, 1.0)
                    out.append(
                        SFPClassification(
                            swing_i=s.i,
                            swing_price=round(level, 2),
                            kind="H",
                            test_i=j,
                            ts=int(b.ts[j]),
                            time_str=hm(int(b.ts[j])),
                            state="PURE_SFP",
                            pierce_atr=round(pierce, 2),
                            bars_hovered=1,
                            reversal_confirmed=True,
                        )
                    )
                    break
                elif b.c[j] > level and (b.c[j] - level) <= csfp_max_pierce * atr_j:
                    # Tiny close beyond: check if next candle aggressively reverses
                    if j + 1 < N and b.c[j + 1] < level and b.c[j + 1] < b.o[j + 1]:
                        out.append(
                            SFPClassification(
                                swing_i=s.i,
                                swing_price=round(level, 2),
                                kind="H",
                                test_i=j,
                                ts=int(b.ts[j]),
                                time_str=hm(int(b.ts[j])),
                                state="CSFP",
                                pierce_atr=round((b.c[j] - level) / max(atr_j, 1.0), 2),
                                bars_hovered=2,
                                reversal_confirmed=True,
                            )
                        )
                        break

            # 3. Pure SFP vs cSFP for Swing Low
            elif s.kind == "L":
                if b.l[j] < level and b.c[j] > level:
                    pierce = (level - b.l[j]) / max(atr_j, 1.0)
                    out.append(
                        SFPClassification(
                            swing_i=s.i,
                            swing_price=round(level, 2),
                            kind="L",
                            test_i=j,
                            ts=int(b.ts[j]),
                            time_str=hm(int(b.ts[j])),
                            state="PURE_SFP",
                            pierce_atr=round(pierce, 2),
                            bars_hovered=1,
                            reversal_confirmed=True,
                        )
                    )
                    break
                elif b.c[j] < level and (level - b.c[j]) <= csfp_max_pierce * atr_j:
                    if j + 1 < N and b.c[j + 1] > level and b.c[j + 1] > b.o[j + 1]:
                        out.append(
                            SFPClassification(
                                swing_i=s.i,
                                swing_price=round(level, 2),
                                kind="L",
                                test_i=j,
                                ts=int(b.ts[j]),
                                time_str=hm(int(b.ts[j])),
                                state="CSFP",
                                pierce_atr=round((level - b.c[j]) / max(atr_j, 1.0), 2),
                                bars_hovered=2,
                                reversal_confirmed=True,
                            )
                        )
                        break

    out.sort(key=lambda x: x.test_i)
    return out


# ── Concept 6: RBR / DBD / RBD / DBR Supply & Demand Base Scanner ────────────

@dataclass
class SDBaseZone:
    kind: str                      # "RBR" | "DBD" | "RBD" | "DBR"
    polarity: str                  # "demand" | "supply"
    base_start_i: int
    base_end_i: int
    ts: int
    time_str: str
    top: float
    bottom: float
    bars_count: int
    height_atr: float
    has_internal_sfp: bool
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_sd_bases(
    b: Bars,
    a: np.ndarray,
    fvgs: List[FVG],
    cfg: Dict[str, Any],
) -> List[SDBaseZone]:
    """Detects 3-stage institutional structures: Leg 1 -> Base Consolidation -> Leg 2 Displacement."""
    N = len(b)
    if N < 4:
        return []

    c_base = cfg.get("supply_demand_base", {})
    min_bars = int(c_base.get("base_min_bars", 3))
    max_bars = int(c_base.get("base_max_bars", 14))
    max_ht_atr = float(c_base.get("base_height_max_atr", 1.60))

    c_disp = cfg.get("displacement", {})
    disp_mult = float(c_disp.get("disp_body_atr_mult", 1.05))

    out: List[SDBaseZone] = []

    for i in range(min_bars + 2, N - 1):
        atr_i = float(a[i]) if i < len(a) else 10.0

        # Scan backwards for base
        for k in range(min_bars, max_bars + 1):
            start = i - k
            if start < 1:
                break
            base_high = float(np.max(b.h[start:i]))
            base_low = float(np.min(b.l[start:i]))
            ht = base_high - base_low
            if ht > max_ht_atr * atr_i:
                continue

            # Leg 1: Candle before base
            leg1_body = b.c[start - 1] - b.o[start - 1]
            leg1_bull = leg1_body > 0 and abs(leg1_body) >= 0.8 * atr_i
            leg1_bear = leg1_body < 0 and abs(leg1_body) >= 0.8 * atr_i

            # Leg 2: Exit candle from base (bar i)
            leg2_body = b.c[i] - b.o[i]
            leg2_bull = leg2_body > 0 and abs(leg2_body) >= disp_mult * atr_i and b.c[i] > base_high
            leg2_bear = leg2_body < 0 and abs(leg2_body) >= disp_mult * atr_i and b.c[i] < base_low

            if not (leg2_bull or leg2_bear):
                continue

            # Determine pattern kind
            kind = None
            polarity = None
            if leg1_bull and leg2_bull:
                kind, polarity = "RBR", "demand"
            elif leg1_bear and leg2_bear:
                kind, polarity = "DBD", "supply"
            elif leg1_bull and leg2_bear:
                kind, polarity = "RBD", "supply"
            elif leg1_bear and leg2_bull:
                kind, polarity = "DBR", "demand"
            else:
                continue

            # Check if internal SFP occurred inside base
            internal_sfp = False
            for bi in range(start + 1, i):
                if b.h[bi] > b.h[bi - 1] and b.c[bi] < b.c[bi - 1]:
                    internal_sfp = True
                    break

            score = 5.0 + (1.5 if internal_sfp else 0.0) + (1.0 if k >= 5 else 0.0)

            out.append(
                SDBaseZone(
                    kind=kind,
                    polarity=polarity,
                    base_start_i=start,
                    base_end_i=i - 1,
                    ts=int(b.ts[start]),
                    time_str=hm(int(b.ts[start])),
                    top=round(base_high, 2),
                    bottom=round(base_low, 2),
                    bars_count=k,
                    height_atr=round(ht / max(atr_i, 1.0), 2),
                    has_internal_sfp=internal_sfp,
                    score=score,
                )
            )
            break

    # Deduplicate overlapping bases
    dedup: List[SDBaseZone] = []
    for z in out:
        if not dedup or abs(z.base_end_i - dedup[-1].base_end_i) > 3:
            dedup.append(z)
    return dedup[-8:]


# ── Concept 7: FTR — Failed To Return / Indecision Candle "Bagholder" Zone ────

@dataclass
class FTRZone:
    dir: str                       # "bull" | "bear"
    pause_start_i: int
    pause_end_i: int
    ts: int
    time_str: str
    top: float
    bottom: float
    pause_bars: int
    ftb_tested: bool
    mitigated: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_ftr_zones(
    b: Bars,
    a: np.ndarray,
    cfg: Dict[str, Any],
) -> List[FTRZone]:
    """Detects 1 to 3 opposite-colored pause candles trapping 'bagholders' before continuation."""
    N = len(b)
    if N < 4:
        return []

    c_ftr = cfg.get("ftr", {})
    min_pause = int(c_ftr.get("ftr_min_bars", 1))
    max_pause = int(c_ftr.get("ftr_max_bars", 3))
    max_age = int(c_ftr.get("max_age_bars", 80))

    c_disp = cfg.get("displacement", {})
    disp_mult = float(c_disp.get("disp_body_atr_mult", 1.05))

    out: List[FTRZone] = []

    for i in range(max_pause + 2, N - 1):
        atr_i = float(a[i]) if i < len(a) else 10.0

        # Bullish FTR: Green impulse -> 1-3 red pause bars -> Green continuation
        for k in range(min_pause, max_pause + 1):
            p_start = i - k
            impulse_i = p_start - 1
            if impulse_i < 0:
                continue

            impulse_green = (b.c[impulse_i] - b.o[impulse_i]) >= 0.8 * atr_i
            all_pause_red = all(b.c[j] <= b.o[j] for j in range(p_start, i))
            cont_green = (b.c[i] - b.o[i]) >= disp_mult * atr_i and b.c[i] > np.max(b.h[p_start:i])

            if impulse_green and all_pause_red and cont_green:
                top = float(np.max(b.h[p_start:i]))
                bot = float(np.min(b.l[p_start:i]))

                # Check FTB test
                ftb = False
                mitigated = False
                for m in range(i + 1, min(N, i + 1 + max_age)):
                    if b.l[m] <= top:
                        ftb = True
                    if b.c[m] < bot:
                        mitigated = True
                        break

                out.append(
                    FTRZone(
                        dir="bull",
                        pause_start_i=p_start,
                        pause_end_i=i - 1,
                        ts=int(b.ts[p_start]),
                        time_str=hm(int(b.ts[p_start])),
                        top=round(top, 2),
                        bottom=round(bot, 2),
                        pause_bars=k,
                        ftb_tested=ftb,
                        mitigated=mitigated,
                    )
                )
                break

            # Bearish FTR: Red impulse -> 1-3 green pause bars -> Red continuation
            impulse_red = (b.o[impulse_i] - b.c[impulse_i]) >= 0.8 * atr_i
            all_pause_green = all(b.c[j] >= b.o[j] for j in range(p_start, i))
            cont_red = (b.o[i] - b.c[i]) >= disp_mult * atr_i and b.c[i] < np.min(b.l[p_start:i])

            if impulse_red and all_pause_green and cont_red:
                top = float(np.max(b.h[p_start:i]))
                bot = float(np.min(b.l[p_start:i]))

                ftb = False
                mitigated = False
                for m in range(i + 1, min(N, i + 1 + max_age)):
                    if b.h[m] >= bot:
                        ftb = True
                    if b.c[m] > top:
                        mitigated = True
                        break

                out.append(
                    FTRZone(
                        dir="bear",
                        pause_start_i=p_start,
                        pause_end_i=i - 1,
                        ts=int(b.ts[p_start]),
                        time_str=hm(int(b.ts[p_start])),
                        top=round(top, 2),
                        bottom=round(bot, 2),
                        pause_bars=k,
                        ftb_tested=ftb,
                        mitigated=mitigated,
                    )
                )
                break

    return out[-6:]


# ── Concept 8: TT3 — Three Tap Range Extremity Setup ─────────────────────────

@dataclass
class TT3Setup:
    side: str                      # "bull" | "bear"
    range_high: float
    range_low: float
    range_mid: float
    tap1_price: float
    tap2_extreme: float
    hidden_zone_top: float
    hidden_zone_bot: float
    status: str                    # "ARMED" | "TRIGGERED" | "EXPIRED"
    sl: float
    tp1: float
    tp2: float
    ts: int
    time_str: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_tt3(
    b: Bars,
    a: np.ndarray,
    swings: List[Swing],
    rng_dict: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> List[TT3Setup]:
    """Detects Three Tap Range Extremity setups: Tap 1 swing -> Tap 2 deviation -> Tap 3 hidden zone entry."""
    N = len(b)
    if N < 4 or not rng_dict:
        return []

    r_high = float(rng_dict.get("high") or rng_dict.get("top") or 0.0)
    r_low = float(rng_dict.get("low") or rng_dict.get("bottom") or 0.0)
    if r_high <= r_low or (r_high - r_low) < 20.0:
        return []

    r_mid = (r_high + r_low) / 2.0
    c_tt3 = cfg.get("tt3", {})
    sl_buf = float(c_tt3.get("deviation_sl_buffer_atr", 0.10))

    out: List[TT3Setup] = []

    # 1. Bullish TT3 at Range Low
    rl_swings = [s for s in swings if s.kind == "L" and abs(s.price - r_low) <= 15.0]
    if len(rl_swings) >= 2:
        tap1 = rl_swings[-2]
        tap2 = rl_swings[-1]
        if tap2.price < tap1.price:  # Tap 2 deviation sweep
            hidden_top = float(b.h[tap1.i])
            hidden_bot = float(b.l[tap1.i])
            atr_val = float(a[tap2.i]) if tap2.i < len(a) else 10.0
            sl = tap2.price - sl_buf * atr_val

            # Check Tap 3: price re-enters Hidden Zone without breaking Tap 2 low
            status = "ARMED"
            for k in range(tap2.confirm_i, N):
                if hidden_bot <= b.l[k] <= hidden_top and b.l[k] > tap2.price:
                    status = "TRIGGERED"
                if b.c[k] < sl or b.h[k] >= r_high:
                    status = "EXPIRED"

            out.append(
                TT3Setup(
                    side="bull",
                    range_high=round(r_high, 2),
                    range_low=round(r_low, 2),
                    range_mid=round(r_mid, 2),
                    tap1_price=round(tap1.price, 2),
                    tap2_extreme=round(tap2.price, 2),
                    hidden_zone_top=round(hidden_top, 2),
                    hidden_zone_bot=round(hidden_bot, 2),
                    status=status,
                    sl=round(sl, 2),
                    tp1=round(r_mid, 2),
                    tp2=round(r_high, 2),
                    ts=int(b.ts[tap2.i]),
                    time_str=hm(int(b.ts[tap2.i])),
                )
            )

    # 2. Bearish TT3 at Range High
    rh_swings = [s for s in swings if s.kind == "H" and abs(s.price - r_high) <= 15.0]
    if len(rh_swings) >= 2:
        tap1 = rh_swings[-2]
        tap2 = rh_swings[-1]
        if tap2.price > tap1.price:  # Tap 2 deviation sweep
            hidden_top = float(b.h[tap1.i])
            hidden_bot = float(b.l[tap1.i])
            atr_val = float(a[tap2.i]) if tap2.i < len(a) else 10.0
            sl = tap2.price + sl_buf * atr_val

            status = "ARMED"
            for k in range(tap2.confirm_i, N):
                if hidden_bot <= b.h[k] <= hidden_top and b.h[k] < tap2.price:
                    status = "TRIGGERED"
                if b.c[k] > sl or b.l[k] <= r_low:
                    status = "EXPIRED"

            out.append(
                TT3Setup(
                    side="bear",
                    range_high=round(r_high, 2),
                    range_low=round(r_low, 2),
                    range_mid=round(r_mid, 2),
                    tap1_price=round(tap1.price, 2),
                    tap2_extreme=round(tap2.price, 2),
                    hidden_zone_top=round(hidden_top, 2),
                    hidden_zone_bot=round(hidden_bot, 2),
                    status=status,
                    sl=round(sl, 2),
                    tp1=round(r_mid, 2),
                    tp2=round(r_low, 2),
                    ts=int(b.ts[tap2.i]),
                    time_str=hm(int(b.ts[tap2.i])),
                )
            )

    return out


# ── Concept 9: GPX — Gameplay Box HTF Corridor + LTF VWAP Wave Rider ─────────

@dataclass
class GPXCorridor:
    htf: str
    corridor_high: float
    corridor_low: float
    corridor_mid: float
    zone: str                      # "PREMIUM" | "DISCOUNT" | "EQ"
    veto_short: bool
    veto_long: bool
    vwap_state: str                # "BELOW_VWAP" | "ABOVE_VWAP" | "OVEREXTENDED"
    vwap_val: Optional[float]
    wave_bias: str                 # "bull" | "bear" | "neutral"
    status_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_gpx(
    spot: float,
    a_ltf: np.ndarray,
    vwap_val: Optional[float],
    htf_an: Optional[Any],
    cfg: Dict[str, Any],
    bars_ltf: Optional[Bars] = None,
) -> Optional[GPXCorridor]:
    """Establishes the macro ping-pong corridor and applies directional vetoes."""
    hb = None
    htf_tf = "15m"
    if htf_an and getattr(htf_an, "bars", None) and len(htf_an.bars) > 0:
        hb = htf_an.bars
        htf_tf = getattr(htf_an, "tf", "HTF")
    elif bars_ltf is not None and len(bars_ltf) > 0:
        hb = bars_ltf
        htf_tf = "Macro"

    if hb is None or len(hb) == 0:
        return None

    # Search for nearest unfilled HTF supply above and demand below
    supplies = [float(o.top) for o in getattr(htf_an, "obs", []) if o.dir == "bear" and o.top > spot]
    supplies += [float(g.top) for g in getattr(htf_an, "fvgs", []) if g.dir == "bear" and g.top > spot]
    if not supplies:
        win_h = hb.h[-60:] if len(hb.h) >= 60 else hb.h
        supplies = [float(np.max(win_h))]

    demands = [float(o.bottom) for o in getattr(htf_an, "obs", []) if o.dir == "bull" and o.bottom < spot]
    demands += [float(g.bottom) for g in getattr(htf_an, "fvgs", []) if g.dir == "bull" and g.bottom < spot]
    if not demands:
        win_l = hb.l[-60:] if len(hb.l) >= 60 else hb.l
        demands = [float(np.min(win_l))]

    c_high = min(supplies)
    c_low = max(demands)
    if c_high <= c_low:
        win_h = hb.h[-60:] if len(hb.h) >= 60 else hb.h
        win_l = hb.l[-60:] if len(hb.l) >= 60 else hb.l
        c_high, c_low = float(np.max(win_h)), float(np.min(win_l))
    if c_high <= c_low:
        return None

    c_mid = (c_high + c_low) / 2.0
    c_gpx = cfg.get("gpx", {})
    overext_mult = float(c_gpx.get("mean_reversion_overextended_atr", 2.20))
    atr_val = float(a_ltf[-1]) if len(a_ltf) else 10.0

    zone = "PREMIUM" if spot > c_mid else "DISCOUNT" if spot < c_mid else "EQ"
    veto_short = (spot < c_mid) or (abs(spot - c_low) <= 15.0)
    veto_long = (spot > c_mid) or (abs(spot - c_high) <= 15.0)

    vwap_state = "UNKNOWN"
    wave_bias = "neutral"
    if vwap_val is not None:
        diff = spot - vwap_val
        if abs(diff) >= overext_mult * atr_val:
            vwap_state = "OVEREXTENDED"
            wave_bias = "mean_reversion_to_vwap"
        elif diff > 0:
            vwap_state = "ABOVE_VWAP"
            wave_bias = "bull" if not veto_long else "neutral"
        else:
            vwap_state = "BELOW_VWAP"
            wave_bias = "bear" if not veto_short else "neutral"

    status_text = (
        f"GPX {htf_tf}: [{c_low:.1f} – {c_high:.1f}], Zone: {zone}. "
        f"{'VETO SHORT (Discount)' if veto_short else ''} {'VETO LONG (Premium)' if veto_long else ''}"
    ).strip()

    return GPXCorridor(
        htf=htf_tf,
        corridor_high=round(c_high, 2),
        corridor_low=round(c_low, 2),
        corridor_mid=round(c_mid, 2),
        zone=zone,
        veto_short=veto_short,
        veto_long=veto_long,
        vwap_state=vwap_state,
        vwap_val=round(vwap_val, 2) if vwap_val is not None else None,
        wave_bias=wave_bias,
        status_text=status_text,
    )


# ── Concept 10: Trap & Inducement Veto Engine ─────────────────────────────────

@dataclass
class TrapInducement:
    zone_kind: str                 # "OB" | "SUPPLY" | "DEMAND"
    zone_top: float
    zone_bottom: float
    magnet_type: str               # "EQH" | "EQL" | "UNFILLED_FVG"
    magnet_price: float
    distance_atr: float
    veto_entry: bool
    warning: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_traps_inducements(
    obs: List[OrderBlock],
    pools: List[Pool],
    fvgs: List[FVG],
    a: np.ndarray,
    cfg: Dict[str, Any],
) -> List[TrapInducement]:
    """Flags lower zones as inducements when equal highs/lows or FVGs sit directly behind them."""
    if not obs:
        return []

    c_liq = cfg.get("liquidity_magnets", {})
    induce_prox_atr = float(c_liq.get("inducement_proximity_atr", 1.50))

    out: List[TrapInducement] = []

    for o in obs:
        if o.state in ("broken", "expired"):
            continue
        atr_o = float(a[min(o.i, len(a) - 1)]) if len(a) else 10.0
        prox_dist = induce_prox_atr * atr_o

        if o.dir == "bear":
            # Check for EQH or unfilled bearish FVG directly above OB
            eqh_above = [p for p in pools if p.side == "BSL" and p.src == "EQH" and 0 < (p.price - o.top) <= prox_dist]
            fvg_above = [g for g in fvgs if g.dir == "bear" and 0 < (g.bottom - o.top) <= prox_dist]

            if eqh_above:
                target = eqh_above[0]
                out.append(
                    TrapInducement(
                        zone_kind="BEAR_OB",
                        zone_top=round(o.top, 2),
                        zone_bottom=round(o.bottom, 2),
                        magnet_type="EQH",
                        magnet_price=round(target.price, 2),
                        distance_atr=round((target.price - o.top) / max(atr_o, 1.0), 2),
                        veto_entry=True,
                        warning=f"TRAP WARNING: EQH Liquidity Magnet at {target.price:.1f} sits directly above OB — sweep expected first",
                    )
                )
            elif fvg_above:
                target_g = fvg_above[0]
                out.append(
                    TrapInducement(
                        zone_kind="BEAR_OB",
                        zone_top=round(o.top, 2),
                        zone_bottom=round(o.bottom, 2),
                        magnet_type="UNFILLED_FVG",
                        magnet_price=round(target_g.bottom, 2),
                        distance_atr=round((target_g.bottom - o.top) / max(atr_o, 1.0), 2),
                        veto_entry=True,
                        warning=f"TRAP WARNING: Unfilled FVG at {target_g.bottom:.1f} sits directly above OB — stop-hunt expected",
                    )
                )

        elif o.dir == "bull":
            eql_below = [p for p in pools if p.side == "SSL" and p.src == "EQL" and 0 < (o.bottom - p.price) <= prox_dist]
            fvg_below = [g for g in fvgs if g.dir == "bull" and 0 < (o.bottom - g.top) <= prox_dist]

            if eql_below:
                target = eql_below[0]
                out.append(
                    TrapInducement(
                        zone_kind="BULL_OB",
                        zone_top=round(o.top, 2),
                        zone_bottom=round(o.bottom, 2),
                        magnet_type="EQL",
                        magnet_price=round(target.price, 2),
                        distance_atr=round((o.bottom - target.price) / max(atr_o, 1.0), 2),
                        veto_entry=True,
                        warning=f"TRAP WARNING: EQL Liquidity Magnet at {target.price:.1f} sits directly below OB — sweep expected first",
                    )
                )
            elif fvg_below:
                target_g = fvg_below[0]
                out.append(
                    TrapInducement(
                        zone_kind="BULL_OB",
                        zone_top=round(o.top, 2),
                        zone_bottom=round(o.bottom, 2),
                        magnet_type="UNFILLED_FVG",
                        magnet_price=round(target_g.top, 2),
                        distance_atr=round((o.bottom - target_g.top) / max(atr_o, 1.0), 2),
                        veto_entry=True,
                        warning=f"TRAP WARNING: Unfilled FVG at {target_g.top:.1f} sits directly below OB — dip sweep expected",
                    )
                )

    return out


# ── Unified Engine Compute & Serialization ───────────────────────────────────

def detect_bprs(fvgs: List[FVG]) -> List[Dict[str, Any]]:
    """Detects BPR (Balanced Price Range) & Super BPR (collision of 3+ opposing FVGs) — Ch. 24."""
    bprs: List[Dict[str, Any]] = []
    bull_fvgs = [f for f in fvgs if f.dir == "bull"]
    bear_fvgs = [f for f in fvgs if f.dir == "bear"]

    for b_fvg in bull_fvgs:
        for s_fvg in bear_fvgs:
            ov_bot = max(float(b_fvg.bottom), float(s_fvg.bottom))
            ov_top = min(float(b_fvg.top), float(s_fvg.top))
            if ov_bot < ov_top:
                overlap_count = sum(
                    1 for f in fvgs if max(ov_bot, float(f.bottom)) < min(ov_top, float(f.top))
                )
                is_super = overlap_count >= 3
                t_stamp = max(int(b_fvg.ts), int(s_fvg.ts))
                bprs.append({
                    "kind": "SUPER BPR" if is_super else "BPR",
                    "top": round(ov_top, 1),
                    "bot": round(ov_bot, 1),
                    "mid": round((ov_top + ov_bot) / 2.0, 1),
                    "bull_fvg_i": b_fvg.i,
                    "bear_fvg_i": s_fvg.i,
                    "ts": t_stamp,
                    "hm": hm(t_stamp),
                    "overlaps": overlap_count,
                    "label": f"{'SUPER BPR' if is_super else 'BPR'} S/R Flip [{ov_bot:.1f}–{ov_top:.1f}]",
                })
    unique_bprs: List[Dict[str, Any]] = []
    for b in sorted(bprs, key=lambda x: -x["ts"]):
        if not any(abs(b["mid"] - u["mid"]) <= 2.0 and b["kind"] == u["kind"] for u in unique_bprs):
            unique_bprs.append(b)
    return unique_bprs[:6]


def compute_pa_advanced(
    b: Bars,
    a: np.ndarray,
    swings: List[Swing],
    obs: List[OrderBlock],
    fvgs: List[FVG],
    pools: List[Pool],
    ranges: Optional[Dict[str, Any]] = None,
    vwap_val: Optional[float] = None,
    htf_an: Optional[Any] = None,
    tf: str = "5m",
    chain_summary: Optional[Dict[str, Any]] = None,
    rules_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Runs all 10 advanced concepts and formats UI-ready zones and markers."""
    cfg = rules_cfg or R()
    if not cfg.get("enabled", True):
        return {"available": False, "message": "pa_advanced is disabled in rules.json"}

    N = len(b)
    if N < 5:
        return {"available": False, "message": "insufficient candle history for pa_advanced"}

    spot = float(b.c[-1])

    # Round levels from strikes + OI walls
    round_step = float(cfg.get("liquidity_magnets", {}).get("nifty_round_minor", 100.0))
    round_levels = [round(spot / round_step) * round_step]
    round_levels += [round_levels[0] - round_step, round_levels[0] + round_step]
    if chain_summary:
        if chain_summary.get("call_wall"):
            round_levels.append(float(chain_summary["call_wall"]))
        if chain_summary.get("put_wall"):
            round_levels.append(float(chain_summary["put_wall"]))

    # 1. Strong vs. Weak MSB
    msb_list = detect_msb_quality(b, a, swings, obs, fvgs, round_levels, cfg)

    # 2. Dragon Fruit
    df_list = detect_dragon_fruit(obs, fvgs, a, cfg)

    # 3. Mitigation Blocks
    mib_list = detect_mitigation_blocks(b, swings, a, cfg)

    # 4. ROTE
    rote_list = detect_rote(b, a, swings, pools, cfg)

    # 5. SFP Tests
    sfp_list = classify_sfp_tests(b, swings, a, cfg)

    # 6. S/D Bases
    base_list = detect_sd_bases(b, a, fvgs, cfg)

    # 7. FTR Zones
    ftr_list = detect_ftr_zones(b, a, cfg)

    # 8. TT3
    tt3_list = detect_tt3(b, a, swings, ranges, cfg)

    # 9. GPX Corridor
    gpx = compute_gpx(spot, a, vwap_val, htf_an, cfg, bars_ltf=b)

    # 10. Traps & Inducements
    traps = detect_traps_inducements(obs, pools, fvgs, a, cfg)

    # Format Lightweight-Charts Zones
    end_t = chart_time(int(b.ts[-1]))
    zones = []

    # Add Super Order Blocks (Concept 1)
    for msb in msb_list[-4:]:
        score_text = f"Q:{msb.score}/7"
        ob_label = f"{'Bull' if msb.dir == 'bull' else 'Bear'} Super OB ({score_text})"
        if msb.scorecard.get("dragon_fruit"):
            ob_label += " · 🍉 Dragon Fruit"
        atr_p = float(a[min(msb.i, len(a) - 1)]) if len(a) else 15.0
        if msb.dir == "bull":
            ob_bot = msb.extreme_swing_price
            ob_top = msb.extreme_swing_price + atr_p * 0.55
        else:
            ob_top = msb.extreme_swing_price
            ob_bot = msb.extreme_swing_price - atr_p * 0.55
        zones.append({
            "type": "SUPER_OB",
            "dir": msb.dir,
            "from": chart_time(msb.ts),
            "to": end_t,
            "top": round(ob_top, 1),
            "bottom": round(ob_bot, 1),
            "label": ob_label,
            "fill": "rgba(16, 185, 129, 0.22)" if msb.dir == "bull" else "rgba(244, 63, 94, 0.22)",
            "stroke": "#10b981" if msb.dir == "bull" else "#f43f5e",
        })

    # Add ROTE Fibonacci Zones (Concept 4)
    for r in rote_list[-2:]:
        zones.append({
            "type": "ROTE_ZONE",
            "dir": r.dir,
            "from": chart_time(r.ts),
            "to": end_t,
            "top": round(r.entry_high, 1),
            "bottom": round(r.entry_low, 1),
            "label": f"ROTE 0.70–0.80 {'BUY CE' if r.dir == 'bull' else 'BUY PE'} [Target {r.target_price:.0f}]",
            "fill": "rgba(234, 179, 8, 0.20)",
            "stroke": "#eab308",
        })

    # Add TT3 Hidden Zones (Concept 8)
    for t in tt3_list[-2:]:
        zones.append({
            "type": "TT3_HIDDEN_ZONE",
            "dir": t.side,
            "from": chart_time(t.ts),
            "to": end_t,
            "top": round(t.hidden_zone_top, 1),
            "bottom": round(t.hidden_zone_bot, 1),
            "label": f"TT3 Hidden {'Demand' if t.side == 'bull' else 'Supply'} (Tap 1)",
            "fill": "rgba(249, 115, 22, 0.22)",
            "stroke": "#f97316",
        })

    # Add Dragon Fruit Zones (Concept 2)
    for df_z in df_list[-3:]:
        zones.append({
            "type": "DRAGON_FRUIT",
            "dir": df_z.dir,
            "from": chart_time(df_z.ts),
            "to": end_t,
            "top": df_z.top,
            "bottom": df_z.bottom,
            "risk": df_z.risk,
            "label": f"{df_z.label} [{df_z.bottom:.1f}–{df_z.top:.1f}]",
            "fill": "rgba(236, 72, 153, 0.25)" if df_z.risk == "LOW" else "rgba(168, 85, 247, 0.15)",
            "stroke": "#ec4899" if df_z.risk == "LOW" else "#a855f7",
        })

    # Add Mitigation Blocks
    for mib in mib_list[-3:]:
        if not mib.mitigated:
            zones.append({
                "type": "MITIGATION_BLOCK",
                "dir": mib.dir,
                "from": chart_time(mib.ts),
                "to": end_t,
                "top": mib.top,
                "bottom": mib.bottom,
                "label": f"MiB {'Supply' if mib.dir == 'bear' else 'Demand'} [{mib.bottom:.1f}–{mib.top:.1f}]",
                "fill": "rgba(244, 63, 94, 0.20)" if mib.dir == "bear" else "rgba(16, 185, 129, 0.20)",
                "stroke": "#f43f5e" if mib.dir == "bear" else "#10b981",
            })

    # Add S/D Bases
    for sd in base_list[-3:]:
        zones.append({
            "type": f"SD_BASE_{sd.kind}",
            "dir": "bull" if sd.polarity == "demand" else "bear",
            "from": chart_time(sd.ts),
            "to": end_t,
            "top": sd.top,
            "bottom": sd.bottom,
            "label": f"{sd.kind} {sd.polarity.upper()} Base (Q={sd.score:.1f})",
            "fill": "rgba(34, 197, 94, 0.18)" if sd.polarity == "demand" else "rgba(239, 68, 68, 0.18)",
            "stroke": "#22c55e" if sd.polarity == "demand" else "#ef4444",
        })

    # Add FTR Zones
    for fz in ftr_list[-3:]:
        if not fz.mitigated:
            zones.append({
                "type": "FTR_ZONE",
                "dir": fz.dir,
                "from": chart_time(fz.ts),
                "to": end_t,
                "top": fz.top,
                "bottom": fz.bottom,
                "label": f"FTR {'Demand' if fz.dir == 'bull' else 'Supply'} [FTB {'Tested' if fz.ftb_tested else 'Fresh'}]",
                "fill": "rgba(245, 158, 11, 0.20)",
                "stroke": "#f59e0b",
            })

    # Add BPR & Super BPR Zones (Concept Ch. 24)
    bpr_list = detect_bprs(fvgs)
    for bpr in bpr_list:
        zones.append({
            "type": "SUPER_BPR" if bpr["kind"] == "SUPER BPR" else "BPR",
            "dir": "bull",
            "from": chart_time(bpr["ts"]),
            "to": end_t,
            "top": bpr["top"],
            "bottom": bpr["bot"],
            "label": bpr["label"],
            "fill": "rgba(20, 184, 166, 0.22)" if bpr["kind"] == "SUPER BPR" else "rgba(20, 184, 166, 0.15)",
            "stroke": "#14b8a6",
        })

    # Format Lightweight-Charts Markers
    markers = []
    first_ts = int(b.ts[max(0, N - 120)])

    for msb in msb_list:
        if msb.ts >= first_ts:
            markers.append({
                "time": chart_time(msb.ts),
                "side": "above" if msb.dir == "bear" else "below",
                "kind": "MSB_STRONG" if msb.strong else "MSB_WEAK",
                "dir": msb.dir,
                "price": msb.close_price,
                "text": f"{'Strong' if msb.strong else 'Weak'} MSB Q:{msb.score}/7",
                "score": msb.score,
                "full": f"MSB ({msb.dir.upper()}) at {msb.level:.1f}. Score {msb.score}/7. {'Strong impulse close.' if msb.strong else f'Weak break: {msb.weak_type}'}",
                "color": "#10b981" if msb.dir == "bull" else "#f43f5e",
                "shape": "arrowUp" if msb.dir == "bull" else "arrowDown",
            })

    for sfp in sfp_list:
        if sfp.ts >= first_ts:
            label = "Pure SFP" if sfp.state == "PURE_SFP" else "cSFP" if sfp.state == "CSFP" else "Gray Accum"
            markers.append({
                "time": chart_time(sfp.ts),
                "side": "above" if sfp.kind == "H" else "below",
                "kind": sfp.state,
                "dir": "bear" if sfp.kind == "H" else "bull",
                "price": sfp.swing_price,
                "text": label,
                "full": f"{sfp.state} at {sfp.swing_price:.1f} (Pierce: {sfp.pierce_atr:.2f} ATR)",
                "color": "#eab308" if sfp.state == "GRAY_ACCUMULATION" else "#06b6d4",
                "shape": "circle" if sfp.state == "GRAY_ACCUMULATION" else "square",
            })

    # Summary text
    active_traps = len(traps)
    active_mibs = sum(1 for m in mib_list if not m.mitigated)
    active_ftrs = sum(1 for f in ftr_list if not f.mitigated)
    active_dfs = sum(1 for d in df_list if d.risk == "LOW")

    summary = (
        f"Adv PA ({tf}): {len(msb_list)} MSBs · {active_dfs} Dragon Fruits · {active_mibs} MiBs · "
        f"{len(rote_list)} ROTE · {active_ftrs} FTRs · {len(tt3_list)} TT3"
    )
    if active_traps:
        summary += f" · ⚠ {active_traps} TRAP VETOES"

    return {
        "tf": tf,
        "available": True,
        "summary": summary,
        "msb_quality": [m.to_dict() for m in msb_list],
        "dragon_fruit": [d.to_dict() for d in df_list],
        "mitigation_blocks": [m.to_dict() for m in mib_list],
        "rote": [r.to_dict() for r in rote_list],
        "sfp_tests": [s.to_dict() for s in sfp_list],
        "sd_bases": [s.to_dict() for s in base_list],
        "ftr_zones": [f.to_dict() for f in ftr_list],
        "tt3": [t.to_dict() for t in tt3_list],
        "gpx": gpx.to_dict() if gpx else None,
        "traps": [t.to_dict() for t in traps],
        "bpr_zones": bpr_list,
        "zones": zones,
        "markers": markers,
    }
