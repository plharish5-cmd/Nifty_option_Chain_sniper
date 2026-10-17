"""
engine/fvg.py — Fair Value Gap (ICT) = Imbalance / market inefficiency (SMC).

  Bullish FVG at bar i:  low[i]  > high[i-2]   gap = (high[i-2], low[i])   — price left upward too fast
  Bearish FVG at bar i:  high[i] < low[i-2]    gap = (high[i], low[i-2])

Minimum size = max(fvg_min_pts, fvg_min_atr × ATR) — the 0.3-point "killzone" from 8 Oct does not qualify.

Fill tracking ("market efficiency" — price returning to rebalance the gap), measured from bar i+1:
  touched   price first trades into the gap
  ce        50 % "consequent encroachment" reached
  filled    gap fully traded through (efficient again)
  invalid   a CLOSE beyond the far edge — the gap failed as support/resistance
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .primitives import Bars


@dataclass
class FVG:
    dir: str                 # "bull" | "bear"
    i: int                   # third candle (gap is known once bar i closes)
    ts: int
    bottom: float
    top: float
    touched_i: Optional[int] = None
    ce_i: Optional[int] = None
    filled_i: Optional[int] = None
    invalid_i: Optional[int] = None
    body_ratio: float = 0.0          # 3rd-candle rule: body ÷ range of the gap-creating candle (displacement strength)
    displ: float = 0.0               # 3rd-candle body ÷ ATR (how big the move that opened the gap was)
    grade: str = ""                  # "A" strong institutional gap · "B" ok · "C" weak (long wick / small body)

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def ce(self) -> float:
        return (self.top + self.bottom) / 2

    def state_at(self, j: int) -> str:
        if self.invalid_i is not None and self.invalid_i <= j:
            return "invalid"
        if self.filled_i is not None and self.filled_i <= j:
            return "filled"
        if self.ce_i is not None and self.ce_i <= j:
            return "ce"
        if self.touched_i is not None and self.touched_i <= j:
            return "touched"
        return "open"


def find_fvgs(b: Bars, atr_arr: np.ndarray, min_pts: float, min_atr: float,
              min_body_ratio: float = 0.0, min_displ_atr: float = 0.0, need_dir: bool = False) -> List[FVG]:
    """The 3rd-candle rule: the gap-creating candle (bar i) must be a real DISPLACEMENT candle — a strong body
    (not a long counter-wick), moving in the gap's direction. Each FVG is graded A/B/C on that. The three quality
    params default OFF (0.0 / False) → output and the 104 tests are unchanged; pass them to FILTER weak gaps."""
    def _quality(i: int, direction: str):
        rng = float(b.h[i] - b.l[i])
        body = abs(float(b.c[i] - b.o[i]))
        br = body / rng if rng > 0 else 0.0
        A = float(atr_arr[i])
        dp = body / A if A > 0 else 0.0
        good_dir = (b.c[i] >= b.o[i]) if direction == "bull" else (b.c[i] <= b.o[i])
        g = "A" if (br >= 0.5 and dp >= 1.0 and good_dir) else "B" if (br >= 0.3 and good_dir) else "C"
        return br, dp, good_dir, g

    def _keep(br: float, dp: float, good_dir: bool) -> bool:
        if need_dir and not good_dir:
            return False
        return br >= min_body_ratio and dp >= min_displ_atr

    out: List[FVG] = []
    N = len(b)
    for i in range(2, N):
        need = max(min_pts, min_atr * float(atr_arr[i]))
        if b.l[i] > b.h[i - 2] and b.l[i] - b.h[i - 2] >= need:
            br, dp, gd, gr = _quality(i, "bull")
            if _keep(br, dp, gd):
                out.append(FVG("bull", i, int(b.ts[i]), float(b.h[i - 2]), float(b.l[i]), body_ratio=round(br, 2), displ=round(dp, 2), grade=gr))
        elif b.h[i] < b.l[i - 2] and b.l[i - 2] - b.h[i] >= need:
            br, dp, gd, gr = _quality(i, "bear")
            if _keep(br, dp, gd):
                out.append(FVG("bear", i, int(b.ts[i]), float(b.h[i]), float(b.l[i - 2]), body_ratio=round(br, 2), displ=round(dp, 2), grade=gr))
    for g in out:
        for j in range(g.i + 1, N):
            if g.dir == "bull":
                if g.touched_i is None and b.l[j] <= g.top:
                    g.touched_i = j
                if g.ce_i is None and b.l[j] <= g.ce:
                    g.ce_i = j
                if g.filled_i is None and b.l[j] <= g.bottom:
                    g.filled_i = j
                if b.c[j] < g.bottom:
                    g.invalid_i = j
                    break
            else:
                if g.touched_i is None and b.h[j] >= g.bottom:
                    g.touched_i = j
                if g.ce_i is None and b.h[j] >= g.ce:
                    g.ce_i = j
                if g.filled_i is None and b.h[j] >= g.top:
                    g.filled_i = j
                if b.c[j] > g.top:
                    g.invalid_i = j
                    break
    return out


# ═══════════════════════════════════════════════════════════════════════════
# IFVG — Inversion Fair Value Gap (ICT)
# ═══════════════════════════════════════════════════════════════════════════
# When price CLOSES through an FVG, the gap failed — and flips role:
#   bullish FVG closed below its bottom  → BEARISH IFVG (old support = new resistance → SELL on the retest)
#   bearish FVG closed above its top     → BULLISH IFVG (old resistance = new support → BUY on the retest)
# Tracked from the bar after the inversion (no look-ahead):
#   retest     price trades back into the zone
#   rejected   a retest bar CLOSES back on the inversion side → the trade signal (first rejection only)
#   invalid    a CLOSE beyond the far edge — the inversion failed
@dataclass
class IFVG:
    dir: str                 # "bear" = sell zone, "bull" = buy zone
    src_i: int               # bar of the original FVG
    i: int                   # inversion bar (close through the gap)
    ts: int
    bottom: float
    top: float
    retest_i: Optional[int] = None
    reject_i: Optional[int] = None
    invalid_i: Optional[int] = None

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def state_at(self, j: int) -> str:
        if self.invalid_i is not None and self.invalid_i <= j:
            return "invalid"
        if self.reject_i is not None and self.reject_i <= j:
            return "rejected"
        if self.retest_i is not None and self.retest_i <= j:
            return "retest"
        return "fresh"


def find_ifvgs(b: Bars, fvgs: List[FVG], max_age: int = 120, atr_arr: Optional[np.ndarray] = None,
               max_atr: float = 3.0, body_atr: float = 0.3) -> List[IFVG]:
    """Quality filters: the gap must be ≤ max_atr × ATR (no overnight-gap 'FVGs'), the inversion must be a real
    candle body ≥ body_atr × ATR, and never the session's first candle (an opening gap is not an inversion)."""
    out: List[IFVG] = []
    N = len(b)
    day = (b.ts + 19800) // 86400
    for g in fvgs:
        if g.invalid_i is None:
            continue
        k = g.invalid_i
        A = float(atr_arr[k]) if atr_arr is not None else 0.0
        if A > 0 and (g.size > max_atr * A or abs(b.c[k] - b.o[k]) < body_atr * A):
            continue
        if k > 0 and day[k] != day[k - 1]:
            continue
        if day[g.i] != day[g.i - 2]:                          # the gap itself spans the overnight gap
            continue
        x = IFVG("bear" if g.dir == "bull" else "bull", g.i, g.invalid_i, int(b.ts[g.invalid_i]), g.bottom, g.top)
        for j in range(x.i + 1, min(N, x.i + 1 + max_age)):
            if x.dir == "bear":                              # sell zone above price
                if b.c[j] > x.top:
                    x.invalid_i = j
                    break
                if b.h[j] >= x.bottom:
                    if x.retest_i is None:
                        x.retest_i = j
                    if x.reject_i is None and b.c[j] < x.bottom:
                        x.reject_i = j
            else:                                            # buy zone below price
                if b.c[j] < x.bottom:
                    x.invalid_i = j
                    break
                if b.l[j] <= x.top:
                    if x.retest_i is None:
                        x.retest_i = j
                    if x.reject_i is None and b.c[j] > x.top:
                        x.reject_i = j
        out.append(x)
    return out
