"""
engine/structure.py — Break of Structure (BOS) and Change of Character / Market Structure Shift (CHoCH = MSS).

Definitions used (ICT ↔ SMC naming from your table):
  BOS    a CLOSE beyond the latest confirmed swing in the direction of the current trend → trend continues.
  CHoCH  (ICT: Change of Character, SMC: Market Structure Shift) a CLOSE beyond the latest confirmed swing
         AGAINST the current trend → first sign the trend changed.
  Displacement  the breaking leg was forceful: the break candle's body ≥ displacement_body_atr × ATR with a
         body ≥ 60 % of its range, OR the leg left a Fair Value Gap. ICT only trusts an MSS with displacement;
         a lazy one-tick close through a swing is how most "MSS" signals fail.

Closes, not wicks: a wick through a swing is a liquidity event (see liquidity.py), not a structure break.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .primitives import Bars, Swing, body_ratio


@dataclass
class StructEvent:
    kind: str            # "BOS" | "CHOCH"
    dir: str             # "bull" | "bear"
    i: int
    ts: int
    level: float         # the swing price that was broken
    swing_i: int         # bar of that swing
    close: float
    displacement: bool
    origin_i: int        # start of the leg that broke structure (dealing-range anchor)
    origin_price: float

    @property
    def is_mss(self) -> bool:
        return self.kind == "CHOCH"


def structure(b: Bars, swings: List[Swing], atr_arr: np.ndarray, fvg_bars: dict,
              disp_body_atr: float = 1.0, disp_body_ratio: float = 0.6):
    """Walk closed bars once. fvg_bars: {bar index of FVG's 3rd candle: 'bull'|'bear'} for displacement."""
    events: List[StructEvent] = []
    trend = "none"
    used_h, used_l = set(), set()
    N = len(b)
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    for j in range(N):
        sh = _latest(highs, j, used_h)
        sl = _latest(lows, j, used_l)
        if sh is not None and b.c[j] > sh.price:
            kind = "CHOCH" if trend == "bear" else "BOS"
            k0 = sh.i + int(np.argmin(b.l[sh.i:j + 1]))
            disp = _disp(b, j, atr_arr, disp_body_atr, disp_body_ratio, "bull", fvg_bars)
            events.append(StructEvent(kind, "bull", j, int(b.ts[j]), sh.price, sh.i, float(b.c[j]), disp,
                                      k0, float(b.l[k0])))
            trend = "bull"
            used_h.update(s.i for s in highs if s.confirm_i <= j and s.price < b.c[j])
        elif sl is not None and b.c[j] < sl.price:
            kind = "CHOCH" if trend == "bull" else "BOS"
            k0 = sl.i + int(np.argmax(b.h[sl.i:j + 1]))
            disp = _disp(b, j, atr_arr, disp_body_atr, disp_body_ratio, "bear", fvg_bars)
            events.append(StructEvent(kind, "bear", j, int(b.ts[j]), sl.price, sl.i, float(b.c[j]), disp,
                                      k0, float(b.h[k0])))
            trend = "bear"
            used_l.update(s.i for s in lows if s.confirm_i <= j and s.price > b.c[j])
    return events, trend


def _latest(sw: List[Swing], j: int, used: set) -> Optional[Swing]:
    for s in reversed(sw):
        if s.confirm_i <= j and s.i not in used:
            return s
    return None


def _disp(b: Bars, j: int, atr_arr: np.ndarray, k_atr: float, k_ratio: float, d: str, fvg_bars: dict) -> bool:
    a = float(atr_arr[j - 1]) if j >= 1 else float(atr_arr[j])
    body = abs(b.c[j] - b.o[j])
    right_way = (b.c[j] > b.o[j]) if d == "bull" else (b.c[j] < b.o[j])
    strong = right_way and body >= k_atr * a and body_ratio(b, j) >= k_ratio
    gap = fvg_bars.get(j) == d or fvg_bars.get(j - 1) == d        # gap left by the breaking leg (no lookahead)
    return bool(strong or gap)
