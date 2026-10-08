"""
engine/ranges.py — Trading range (ICT) = Price range / Fibonacci (SMC); premium / discount; OTE; sessions.

Dealing range = the leg that produced the latest structure break:
  after a bullish break: from the leg's low (origin) to the highest high since (extreme)
  after a bearish break: from the leg's high (origin) to the lowest low since
  Equilibrium (EQ, 50 %) = "fair price". Below EQ = DISCOUNT (where a bullish trader buys),
  above EQ = PREMIUM (where a bearish trader sells).
  OTE (optimal trade entry) = the 61.8 % – 79 % retracement of the leg, 70.5 % the sweet spot
  ("Entry at 61.8 %" in SMC language).
The range is invalid once a CLOSE goes beyond the origin (the leg failed).

Session levels (IST): previous day high/low/close, today's open, opening range 09:15–09:30, initial balance
09:15–10:15, running day high/low. Computed from 1-minute candles so every timeframe sees the same numbers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import config
from ..timeutil import IST, ist_date, parse_hm, session_bounds
from .primitives import Bars
from .structure import StructEvent


def dealing_range(b: Bars, events: List[StructEvent], ote: List[float]) -> Optional[Dict[str, Any]]:
    N = len(b)
    for ev in reversed(events):
        if ev.dir == "bull":
            ext_i = ev.origin_i + int(b.h[ev.origin_i:].argmax())
            lo, hi = ev.origin_price, float(b.h[ext_i])
            if any(b.c[j] < lo for j in range(ev.i + 1, N)):
                continue                                   # failed leg — look at the previous one
        else:
            ext_i = ev.origin_i + int(b.l[ev.origin_i:].argmin())
            hi, lo = ev.origin_price, float(b.l[ext_i])
            if any(b.c[j] > hi for j in range(ev.i + 1, N)):
                continue
        rng = hi - lo
        if rng <= 0:
            continue
        eq = lo + rng / 2
        if ev.dir == "bull":       # retrace DOWN from the high
            levels = {f"{r:.3f}": hi - r * rng for r in ote}
        else:                      # retrace UP from the low
            levels = {f"{r:.3f}": lo + r * rng for r in ote}
        px = float(b.c[-1])
        pos = (px - lo) / rng
        return {
            "dir": ev.dir, "event": ev.kind, "event_ts": ev.ts, "origin_ts": int(b.ts[ev.origin_i]),
            "extreme_ts": int(b.ts[ext_i]), "high": hi, "low": lo, "eq": eq, "ote": levels,
            "ote_band": [min(levels.values()), max(levels.values())],
            "pos_pct": round(100 * pos, 1),
            "zone": "premium" if px > eq else "discount",
            "origin_i": ev.origin_i, "event_i": ev.i, "extreme_i": ext_i,
        }
    return None


def session_levels(rows_1m: List[Dict[str, float]], daily: List[Dict[str, Any]], day=None) -> Dict[str, Any]:
    """rows_1m: closed 1m candles (several days). daily: [{'d','o','h','l','c'}] sorted. day: IST date (default:
    the last date in rows)."""
    if not rows_1m:
        return {}
    day = day or ist_date(rows_1m[-1]["ts"])
    today = [r for r in rows_1m if ist_date(r["ts"]) == day]
    prev_days = sorted({ist_date(r["ts"]) for r in rows_1m if ist_date(r["ts"]) < day})
    prev = None
    if prev_days:
        pd_rows = [r for r in rows_1m if ist_date(r["ts"]) == prev_days[-1]]
        prev = {"d": prev_days[-1].isoformat(), "h": max(r["h"] for r in pd_rows), "l": min(r["l"] for r in pd_rows),
                "c": _pdc(pd_rows)}
    if daily:
        older = [d for d in daily if d["d"] < day.isoformat()]
        if older and (prev is None or older[-1]["d"] > prev["d"]):
            prev = {"d": older[-1]["d"], "h": older[-1]["h"], "l": older[-1]["l"], "c": older[-1]["c"]}
    o_ts, _ = session_bounds(day)
    or_end = datetime.combine(day, parse_hm(config.rule("session", "opening_range_end", "09:30")), IST).timestamp()
    ib_end = datetime.combine(day, parse_hm(config.rule("session", "initial_balance_end", "10:15")), IST).timestamp()
    orr = [r for r in today if r["ts"] < or_end]
    ib = [r for r in today if r["ts"] < ib_end]
    out: Dict[str, Any] = {"date": day.isoformat(), "open_ts": int(o_ts)}
    if today:
        out.update({"open": today[0]["o"], "high": max(r["h"] for r in today), "low": min(r["l"] for r in today),
                    "last": today[-1]["c"]})
    if prev:
        out["prev"] = prev
    if orr and today[-1]["ts"] >= or_end - 60:
        out["or"] = {"h": max(r["h"] for r in orr), "l": min(r["l"] for r in orr), "known_ts": int(or_end)}
    if ib and today[-1]["ts"] >= ib_end - 60:
        out["ib"] = {"h": max(r["h"] for r in ib), "l": min(r["l"] for r in ib), "known_ts": int(ib_end)}
    return out


def _pdc(rows: List[Dict[str, float]]) -> float:
    """Previous-day close for LEVELS = close of the 15:00 15-minute candle (i.e. the last 1m before 15:15),
    the same definition as your Streamlit app's PDC fix — the 15:15–15:30 auction prints are excluded."""
    regular = [r for r in rows if (r["ts"] + 19800) % 86400 < (15 * 3600 + 15 * 60)]
    return (regular or rows)[-1]["c"]


def external_pools(sess: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    if sess.get("prev"):
        k = sess["open_ts"]
        out += [{"src": "PDH", "side": "BSL", "price": sess["prev"]["h"], "known_ts": k},
                {"src": "PDL", "side": "SSL", "price": sess["prev"]["l"], "known_ts": k}]
    if sess.get("or"):
        k = sess["or"]["known_ts"]
        out += [{"src": "ORH", "side": "BSL", "price": sess["or"]["h"], "known_ts": k},
                {"src": "ORL", "side": "SSL", "price": sess["or"]["l"], "known_ts": k}]
    if sess.get("ib"):
        k = sess["ib"]["known_ts"]
        out += [{"src": "IBH", "side": "BSL", "price": sess["ib"]["h"], "known_ts": k},
                {"src": "IBL", "side": "SSL", "price": sess["ib"]["l"], "known_ts": k}]
    return out
