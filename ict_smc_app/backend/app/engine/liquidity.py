"""
engine/liquidity.py — Liquidity zones (ICT) and Liquidity sweeps / stop-loss hunts (SMC).

Where stops sit (pools):
  BSL (buy-side, above price): swing highs, EQUAL highs (EQH), previous day high (PDH), opening-range high
                               (ORH, 09:15–09:30), initial-balance high (IBH, 09:15–10:15)
  SSL (sell-side, below price): the mirror — swing lows, EQL, PDL, ORL, IBL
  Equal highs/lows = two or more swings within max(eq_tolerance_pts, eq_tolerance_atr × ATR): retail double tops
  with stops stacked above — the most obvious pool, which is why it is hunted.

What happens to a pool (first time price reaches it, closed candles only):
  SWEEP   wick beyond by ≥ sweep_min_pen_pts but the candle CLOSES back inside → stops taken, price rejected
  RUN     candle CLOSES beyond → liquidity taken and accepted (a breakout, not a hunt)
  FALSE BREAKOUT   a RUN followed within false_breakout_max_bars by a CLOSE back inside — breakout traders
                   trapped. (Your table pairs this with ICT's "highs & lows connection".)
A pool is consumed by the first sweep or run. A later CLOSE back beyond a swept level cancels the sweep
(checked by the playbook, which is where it matters).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .primitives import Bars, Swing

PRIORITY = {"PDH": 5, "PDL": 5, "EQH": 4, "EQL": 4, "IBH": 3, "IBL": 3, "ORH": 3, "ORL": 3, "SWING": 1}


@dataclass
class Pool:
    side: str                   # "BSL" | "SSL"
    src: str                    # SWING | EQH | EQL | PDH | PDL | ORH | ORL | IBH | IBL
    price: float
    known_i: int                # first bar index at which the pool exists
    ts: int                     # time it became known (or of the swing)
    members: int = 1
    taken_i: Optional[int] = None
    taken_how: str = ""         # "sweep" | "run"
    wick: Optional[float] = None
    fb_i: Optional[int] = None  # false-breakout bar (close back inside after a run)

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.src, 1)


@dataclass
class LiqEvent:
    kind: str                   # "SWEEP" | "RUN" | "FALSE_BREAKOUT"
    side: str                   # pool side: BSL / SSL
    src: str
    i: int
    ts: int
    level: float
    wick: float
    close: float
    priority: int

    @property
    def dir(self) -> str:
        """Expected move AFTER the event: a BSL sweep / BSL false breakout is bearish, SSL ones bullish,
        a run continues in its own direction."""
        if self.kind == "RUN":
            return "bull" if self.side == "BSL" else "bear"
        return "bear" if self.side == "BSL" else "bull"


def build_pools(b: Bars, swings: List[Swing], atr_arr: np.ndarray, tol_pts: float, tol_atr: float,
                external: List[Dict]) -> List[Pool]:
    """external: [{'src': 'PDH', 'side': 'BSL', 'price': ..., 'known_ts': epoch}] from session levels."""
    pools: List[Pool] = []
    clustered = set()
    for kind, side, src in (("H", "BSL", "EQH"), ("L", "SSL", "EQL")):
        sw = [s for s in swings if s.kind == kind]
        for a_idx in range(len(sw)):
            s2 = sw[a_idx]
            tol = max(tol_pts, tol_atr * float(atr_arr[s2.i]))
            group = [s2]
            for s1 in sw[:a_idx]:
                # the earlier swing must still be untaken when the later one is confirmed
                if abs(s1.price - s2.price) <= tol and (s1.taken_i is None or s1.taken_i > s2.confirm_i):
                    group.append(s1)
            if len(group) >= 2:
                price = max(g.price for g in group) if kind == "H" else min(g.price for g in group)
                pools.append(Pool(side, src, price, s2.confirm_i, s2.ts, members=len(group)))
                clustered.update((kind, g.i) for g in group)
        for s in sw:
            if (kind, s.i) not in clustered:
                pools.append(Pool(side, "SWING", s.price, s.confirm_i, s.ts))
    for x in external:
        idx = int(np.searchsorted(b.ts, int(x["known_ts"]), side="left"))
        if idx < len(b):
            pools.append(Pool(x["side"], x["src"], float(x["price"]), idx, int(x["known_ts"])))
    # de-duplicate EQ clusters that describe the same price
    uniq: Dict[tuple, Pool] = {}
    for p in pools:
        k = (p.side, p.src, round(p.price, 1))
        if k not in uniq or p.known_i < uniq[k].known_i:
            uniq[k] = p
    return sorted(uniq.values(), key=lambda p: p.known_i)


def scan(b: Bars, pools: List[Pool], min_pen: float, fb_bars: int) -> List[LiqEvent]:
    events: List[LiqEvent] = []
    N = len(b)
    for p in pools:
        for j in range(p.known_i, N):
            if p.side == "BSL":
                if b.h[j] > p.price + min_pen and b.c[j] <= p.price:
                    p.taken_i, p.taken_how, p.wick = j, "sweep", float(b.h[j])
                elif b.c[j] > p.price:
                    p.taken_i, p.taken_how, p.wick = j, "run", float(b.h[j])
            else:
                if b.l[j] < p.price - min_pen and b.c[j] >= p.price:
                    p.taken_i, p.taken_how, p.wick = j, "sweep", float(b.l[j])
                elif b.c[j] < p.price:
                    p.taken_i, p.taken_how, p.wick = j, "run", float(b.l[j])
            if p.taken_i is not None:
                break
        if p.taken_i is None:
            continue
        j = p.taken_i
        events.append(LiqEvent("SWEEP" if p.taken_how == "sweep" else "RUN", p.side, p.src, j, int(b.ts[j]),
                               p.price, p.wick, float(b.c[j]), p.priority))
        if p.taken_how == "run":
            for k in range(j + 1, min(N, j + 1 + fb_bars)):
                back = (b.c[k] < p.price) if p.side == "BSL" else (b.c[k] > p.price)
                if back:
                    p.fb_i = k
                    ext = float(b.h[j:k + 1].max()) if p.side == "BSL" else float(b.l[j:k + 1].min())
                    events.append(LiqEvent("FALSE_BREAKOUT", p.side, p.src, k, int(b.ts[k]), p.price, ext,
                                           float(b.c[k]), p.priority))
                    break
    events.sort(key=lambda e: (e.i, -e.priority))
    return events


def untaken_at(pools: List[Pool], j: int, side: str) -> List[Pool]:
    """Pools that exist and are still untouched as of bar j — what a trader could see at that moment."""
    return [p for p in pools if p.side == side and p.known_i <= j and (p.taken_i is None or p.taken_i > j)]
