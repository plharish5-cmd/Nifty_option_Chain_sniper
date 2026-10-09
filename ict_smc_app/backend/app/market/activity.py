"""
market/activity.py — OPTIONS ACTIVITY gauge (labelled "ACTIVITY (options, direction unknown)" everywhere).

Raw CE + PE contract counts can't be added: a ₹300 in-the-money call and a ₹2 far call are not the same exposure,
and a buyer and a writer print the same volume. So each strike's NEW volume is weighted by its |delta| →
index-equivalent volume. Summed over the strikes within ±window of ATM of the nearest expiry, per minute.

Source: the option chain the desk already polls (cumulative day volume per strike). For each poll:
    added = max(0, volume_now − volume_at_previous_poll) × |delta|       (first sight of a strike adds nothing)
and the sum goes to the minute the poll happened in. It is LIVE ONLY (Dhan has no history of chain snapshots),
so it starts the first minute the desk sees the chain and is "unavailable" before that.

It measures participation (EFFORT as activity), never who won (that is delta, or the response of price).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .. import config, db
from ..timeutil import bucket_start, market_open_now


class Activity:
    def __init__(self) -> None:
        self.prev: Dict[tuple, float] = {}                  # (expiry, strike, side) → cumulative volume
        self.day: Optional[str] = None
        self.minute: Optional[int] = None
        self.acc = [0.0, 0.0, 0.0, 0]                       # ce, pe, raw, samples

    def _flush(self) -> None:
        if self.minute is not None and self.acc[3]:
            db.execute("INSERT OR REPLACE INTO opt_activity (ts, ce, pe, raw, samples) VALUES (?,?,?,?,?)",
                       (self.minute, *[round(x, 2) for x in self.acc[:3]], self.acc[3]))

    def ingest(self, ch: Dict[str, Any], force: bool = False) -> None:
        if not (force or market_open_now()) or not ch.get("rows"):
            return
        ts = int(ch["ts"])
        day = str(ts // 86400)
        if day != self.day:                                 # cumulative volumes restart each session
            self.prev, self.day = {}, day
        m = bucket_start(ts, "1m")
        if m != self.minute:
            self._flush()
            self.minute, self.acc = m, [0.0, 0.0, 0.0, 0]
        W = int(config.rule("activity", "strikes_each_side", 10))
        rows = ch["rows"]
        atm = ch.get("atm") or 0
        ks = sorted(rows, key=lambda r: abs(r["strike"] - atm))[: 2 * W + 1]
        exp = ch.get("expiry")
        for r in ks:
            for side, slot in (("ce", 0), ("pe", 1)):
                x = r[side]
                k = (exp, r["strike"], side)
                vol = float(x.get("vol") or 0.0)
                p = self.prev.get(k)
                self.prev[k] = vol
                if p is None or vol <= p:
                    continue
                add = vol - p
                self.acc[slot] += add * min(1.0, abs(float(x.get("delta") or 0.0)))
                self.acc[2] += add
        self.acc[3] += 1
        self._flush()                                       # minute row is rewritten as it grows


def rows_since(t0: int) -> List[Dict[str, float]]:
    return [dict(r) for r in db.query("SELECT ts, ce, pe, raw, samples FROM opt_activity WHERE ts>=? ORDER BY ts", (int(t0),))]


def per_bar(rows: List[Dict[str, float]], tf: str, window: int = 30) -> Dict[int, Dict[str, Any]]:
    """tf bucket → {act (delta-weighted), raw, z vs the previous `window` bars (never itself)}."""
    agg: Dict[int, List[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    for r in rows:
        b = bucket_start(r["ts"], tf)
        agg[b][0] += r["ce"] + r["pe"]
        agg[b][1] += r["raw"]
        agg[b][2] += 1
    out, hist = {}, []
    for k in sorted(agg):
        a, raw, n = agg[k]
        base = hist[-window:]
        z = None
        if len(base) >= 8:
            mu = sum(base) / len(base)
            sd = math.sqrt(sum((x - mu) ** 2 for x in base) / len(base))
            z = round((a - mu) / sd, 2) if sd > 1e-9 else 0.0
        out[k] = {"act": round(a), "raw": round(raw), "z": z, "minutes": n}
        hist.append(a)
    return out
