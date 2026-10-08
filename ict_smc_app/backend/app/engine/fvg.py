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


def find_fvgs(b: Bars, atr_arr: np.ndarray, min_pts: float, min_atr: float) -> List[FVG]:
    out: List[FVG] = []
    N = len(b)
    for i in range(2, N):
        need = max(min_pts, min_atr * float(atr_arr[i]))
        if b.l[i] > b.h[i - 2] and b.l[i] - b.h[i - 2] >= need:
            out.append(FVG("bull", i, int(b.ts[i]), float(b.h[i - 2]), float(b.l[i])))
        elif b.h[i] < b.l[i - 2] and b.l[i - 2] - b.h[i] >= need:
            out.append(FVG("bear", i, int(b.ts[i]), float(b.h[i]), float(b.l[i - 2])))
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
