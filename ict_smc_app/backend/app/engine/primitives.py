"""
engine/primitives.py — bars, ATR, swing pivots and the "Highs & Lows connection" (zig-zag).

THE NO-LOOKAHEAD CONTRACT (applies to the whole engine)
  * Input is CLOSED candles only.
  * A swing high at bar i needs n lower highs on each side, so it is only KNOWN at bar i+n (confirm_i).
    Every rule that uses a swing at bar j checks `swing.confirm_i <= j`. Using a swing before it is
    confirmed is the classic way an SMC backtest looks brilliant and trades terribly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np


@dataclass
class Bars:
    ts: np.ndarray            # epoch seconds, candle start
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray

    @classmethod
    def from_rows(cls, rows: List[Dict[str, float]]) -> "Bars":
        a = lambda k: np.array([float(r[k]) for r in rows], dtype=float)   # noqa: E731
        return cls(ts=np.array([int(r["ts"]) for r in rows], dtype=np.int64), o=a("o"), h=a("h"), l=a("l"), c=a("c"))

    def __len__(self) -> int:
        return len(self.c)

    def tail(self, n: int) -> "Bars":
        if len(self) <= n:
            return self
        return Bars(self.ts[-n:], self.o[-n:], self.h[-n:], self.l[-n:], self.c[-n:])


def atr(b: Bars, n: int = 14) -> np.ndarray:
    """Wilder-style ATR using only past bars (value at i uses bars ≤ i)."""
    if len(b) == 0:
        return np.array([])
    pc = np.r_[b.c[0], b.c[:-1]]
    tr = np.maximum(b.h - b.l, np.maximum(np.abs(b.h - pc), np.abs(b.l - pc)))
    out = np.empty_like(tr)
    for i in range(len(tr)):
        if i < n:
            out[i] = tr[: i + 1].mean()                  # warm-up: simple mean
        else:
            out[i] = out[i - 1] + (tr[i] - out[i - 1]) / n
    return out


@dataclass
class Swing:
    kind: str                 # "H" | "L"
    i: int                    # bar that made the extreme
    price: float
    ts: int
    confirm_i: int            # first bar at which the swing is knowable
    label: str = ""           # HH / LH / HL / LL (vs the previous swing of the same kind)
    broken_i: Optional[int] = None    # first CLOSE beyond it (structure break)
    taken_i: Optional[int] = None     # first WICK beyond it (liquidity taken: sweep or run)


def pivots(b: Bars, n: int = 2) -> List[Swing]:
    """Strict fractal pivots. Ties are not pivots — equal highs are handled as liquidity, not structure."""
    out: List[Swing] = []
    N = len(b)
    for i in range(n, N - n):
        if b.h[i] > b.h[i - n:i].max() and b.h[i] > b.h[i + 1:i + 1 + n].max():
            out.append(Swing("H", i, float(b.h[i]), int(b.ts[i]), i + n))
        if b.l[i] < b.l[i - n:i].min() and b.l[i] < b.l[i + 1:i + 1 + n].min():
            out.append(Swing("L", i, float(b.l[i]), int(b.ts[i]), i + n))
    out.sort(key=lambda s: (s.i, s.kind))
    last = {"H": None, "L": None}
    for s in out:
        p = last[s.kind]
        if p is None:
            s.label = "H" if s.kind == "H" else "L"
        elif s.kind == "H":
            s.label = "HH" if s.price > p else "LH"
        else:
            s.label = "HL" if s.price > p else "LL"
        last[s.kind] = s.price
    # first close / first wick beyond each swing (after it is confirmed)
    for s in out:
        for j in range(s.confirm_i, N):
            if s.taken_i is None and ((s.kind == "H" and b.h[j] > s.price) or (s.kind == "L" and b.l[j] < s.price)):
                s.taken_i = j
            if (s.kind == "H" and b.c[j] > s.price) or (s.kind == "L" and b.c[j] < s.price):
                s.broken_i = j
                break
    return out


def zigzag(swings: List[Swing]) -> List[Swing]:
    """Alternating H/L sequence for drawing the swing structure: of consecutive same-kind swings keep the
    more extreme one. This is the 'Highs & Lows connection' line on the chart."""
    zz: List[Swing] = []
    for s in swings:
        if zz and zz[-1].kind == s.kind:
            if (s.kind == "H" and s.price >= zz[-1].price) or (s.kind == "L" and s.price <= zz[-1].price):
                zz[-1] = s
            continue
        zz.append(s)
    return zz


def known(swings: List[Swing], j: int, kind: Optional[str] = None) -> List[Swing]:
    return [s for s in swings if s.confirm_i <= j and (kind is None or s.kind == kind)]


def body_ratio(b: Bars, i: int) -> float:
    rng = b.h[i] - b.l[i]
    return float(abs(b.c[i] - b.o[i]) / rng) if rng > 0 else 0.0
