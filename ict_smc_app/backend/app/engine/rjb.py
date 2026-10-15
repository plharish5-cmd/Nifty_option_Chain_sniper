"""
engine/rjb.py — Rejection Block (RJB): the long-wick part of a confirmed swing candle — where price got rejected.

  Bearish RJB (swing HIGH with a long upper wick):  zone = body top → wick high   (sellers' rejection, SELL zone)
  Bullish RJB (swing LOW with a long lower wick):   zone = wick low → body bottom (buyers' rejection, BUY zone)

Wick must be ≥ rb_min_wick_ratio of the candle range (same rule the CRT keylevels use). Known only once the swing is
CONFIRMED (no look-ahead). State after that: fresh → touched (price trades back into it) → invalid (a CLOSE beyond the
wick extreme — the rejection failed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .primitives import Bars


@dataclass
class RJB:
    dir: str                 # "bear" = sell zone (upper wick), "bull" = buy zone (lower wick)
    i: int                   # swing candle
    known_i: int             # bar on which the swing was confirmed
    ts: int
    bottom: float
    top: float
    touched_i: Optional[int] = None
    invalid_i: Optional[int] = None

    def state_at(self, j: int) -> str:
        if self.invalid_i is not None and self.invalid_i <= j:
            return "invalid"
        if self.touched_i is not None and self.touched_i <= j:
            return "touched"
        return "fresh"


def find_rjbs(b: Bars, swings, min_wick_ratio: float = 0.5, max_age: int = 150) -> List[RJB]:
    out: List[RJB] = []
    N = len(b)
    for s in swings:
        i = s.i
        rng = float(b.h[i] - b.l[i])
        if rng <= 0:
            continue
        if s.kind == "H":
            top, bot, d = float(b.h[i]), float(max(b.o[i], b.c[i])), "bear"
        else:
            top, bot, d = float(min(b.o[i], b.c[i])), float(b.l[i]), "bull"
        if (top - bot) / rng < min_wick_ratio:
            continue
        x = RJB(d, i, int(s.confirm_i), int(b.ts[i]), bot, top)
        for j in range(x.known_i + 1, min(N, x.known_i + 1 + max_age)):
            if d == "bear":
                if b.c[j] > top:
                    x.invalid_i = j
                    break
                if x.touched_i is None and b.h[j] >= bot:
                    x.touched_i = j
            else:
                if b.c[j] < bot:
                    x.invalid_i = j
                    break
                if x.touched_i is None and b.l[j] <= top:
                    x.touched_i = j
        out.append(x)
    return out
