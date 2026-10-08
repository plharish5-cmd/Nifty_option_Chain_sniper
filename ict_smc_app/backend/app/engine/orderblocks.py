"""
engine/orderblocks.py — Order Blocks (ICT) = Supply & Demand zones (SMC).

  Bullish OB (demand): the last DOWN-close candle at the low of the leg that then broke structure upward
                       with displacement. Zone = that candle's low … high; mean threshold = 50 %.
  Bearish OB (supply): the last UP-close candle at the high of the leg that broke structure downward.

Only structure breaks WITH displacement create an OB — an OB without a break is just a candle.

Validation on retest (all from CLOSED candles after the break):
  fresh        not revisited yet — the best ones
  mitigated    price traded back into the zone (first retest)
  respected    the retest candle closed back out of the zone in the OB's direction (validation ✓)
  broken       a CLOSE through the far side → the OB failed and flips into a BREAKER of the opposite side
  expired      older than ob_max_age_bars
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .primitives import Bars
from .structure import StructEvent


@dataclass
class OrderBlock:
    dir: str                  # "bull" (demand) | "bear" (supply)
    i: int                    # the OB candle
    ts: int
    bottom: float
    top: float
    created_i: int            # bar of the structure break that validated it
    kind: str = "OB"          # "OB" | "BREAKER"
    state: str = "fresh"
    mitigated_i: Optional[int] = None
    respected_i: Optional[int] = None
    broken_i: Optional[int] = None

    @property
    def mean(self) -> float:
        return (self.top + self.bottom) / 2


def find_order_blocks(b: Bars, events: List[StructEvent], max_age: int = 120) -> List[OrderBlock]:
    out: List[OrderBlock] = []
    N = len(b)
    seen = set()
    for ev in events:
        if not ev.displacement:
            continue
        k0 = ev.origin_i
        want_down = ev.dir == "bull"            # bullish OB = last down-close candle at the low
        k = None
        for kk in range(k0, max(-1, k0 - 4), -1):   # at the extreme or up to 3 bars before it
            if (b.c[kk] < b.o[kk]) if want_down else (b.c[kk] > b.o[kk]):
                k = kk
                break
        if k is None:
            k = k0
        if k in seen:
            continue
        seen.add(k)
        ob = OrderBlock(ev.dir, k, int(b.ts[k]), float(b.l[k]), float(b.h[k]), ev.i)
        _track(ob, b, max_age)
        out.append(ob)
        if ob.broken_i is not None:                      # failed OB → breaker of the other side
            br = OrderBlock("bear" if ob.dir == "bull" else "bull", ob.i, ob.ts, ob.bottom, ob.top,
                            ob.broken_i, kind="BREAKER")
            _track(br, b, max_age)
            out.append(br)
    return out


def _track(ob: OrderBlock, b: Bars, max_age: int) -> None:
    N = len(b)
    for j in range(ob.created_i + 1, N):
        if j - ob.created_i > max_age:
            ob.state = "expired" if ob.state in ("fresh", "mitigated", "respected") else ob.state
            break
        if ob.dir == "bull":
            if b.c[j] < ob.bottom:
                ob.broken_i, ob.state = j, "broken"
                return
            if ob.mitigated_i is None and b.l[j] <= ob.top:
                ob.mitigated_i, ob.state = j, "mitigated"
            if ob.mitigated_i is not None and ob.respected_i is None and b.c[j] > ob.top:
                ob.respected_i, ob.state = j, "respected"
        else:
            if b.c[j] > ob.top:
                ob.broken_i, ob.state = j, "broken"
                return
            if ob.mitigated_i is None and b.h[j] >= ob.bottom:
                ob.mitigated_i, ob.state = j, "mitigated"
            if ob.mitigated_i is not None and ob.respected_i is None and b.c[j] < ob.bottom:
                ob.respected_i, ob.state = j, "respected"
