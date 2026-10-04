"""
turtle_soup.py — "fake breakout" / stop-hunt events: price wicks PAST a liquidity pool by a few points and the
candle then CLOSES back inside.

  pools   pd   previous-day high / low
          or   opening-range high / low (first 15 min)
          swing  confirmed 5m swing high / low made earlier today
          open the day's opening price, only during the first open_pool_minutes (off by default — add "open" to liquidity)
          eq   relative EQUAL highs / lows (two swings within eq_tol_pts) — the classic stop-hunt magnet
  event   high-side: high > pool + min_pts and (close < pool → fake breakout, expect DOWN)
          low-side : low  < pool - min_pts and (close > pool → fake breakdown, expect UP)
          breaches that CLOSE beyond the pool are kept too (close_inside=False) as the "real breakout" comparison.
  causal  the event exists only once the 5m candle has CLOSED (t = candle close time). Each pool is used once:
          the first candle that trades through it consumes it.

  .venv/bin/python backtest_turtle_soup.py     # does it actually predict a reversal on NIFTY? (random controls)
Information only — not financial advice.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

import ict_model as im

PRIORITY = {"eq": 0, "pd": 1, "or": 2, "swing": 3, "open": 4}


def _events_long(t, o, h, l, c, pdl, P, T, ref) -> List[dict]:
    N, n, tf = len(c), int(P["swing_n"]), int(P["tf_minutes"])
    n_or = max(1, int(P["or_minutes"]) // tf)
    liq = set(T["liquidity"])
    tol = float(T["eq_tol_pts"])
    _, pl = im._pivots(h, l, n)
    consumed, out = set(), []
    for i in range(1, N):
        act = []
        if "pd" in liq and not np.isnan(pdl):
            act.append((("pd",), float(pdl), "pd"))
        if "open" in liq and i < int(T.get("open_pool_minutes", 90)) // tf:
            act.append((("open",), float(o[0]), "open"))          # the day's opening price (ICT "true day open")
        if "or" in liq and i >= n_or:
            act.append((("or",), float(l[:n_or].min()), "or"))
        known = [p for p in pl if p + n < i]
        if "swing" in liq:
            act += [(("sw", p), float(l[p]), "swing") for p in known]
        if "eq" in liq:
            for a in range(len(known)):
                for b in range(a + 1, len(known)):
                    if abs(l[known[a]] - l[known[b]]) <= tol:
                        act.append((("eq", known[a], known[b]), float(min(l[known[a]], l[known[b]])), "eq"))
        hit = [(price, kind, key) for key, price, kind in act if key not in consumed and l[i] < price]
        if not hit:
            continue
        for _, _, key in hit:
            consumed.add(key)
        # every pool the candle traded through is used up; report the highest-priority one
        price, kind, key = sorted(hit, key=lambda x: (PRIORITY[x[1]], -x[0]))[0]
        depth = price - float(l[i])
        if depth < float(T["min_pts"]):
            continue
        out.append(dict(i=i, t=pd.Timestamp(t[i]) + pd.Timedelta(minutes=tf), kind=kind, level=price,
                        depth=round(depth, 2), close_inside=bool(c[i] > price), close=float(c[i]),
                        smt=None if key[0] == "open" else im.smt_flag(("sw", key[1]) if key[0] == "sw" else key, i, i, n, n_or, ref)))
    return out


def events(d5: pd.DataFrame, P: Optional[dict] = None, ref5: Optional[pd.DataFrame] = None) -> List[dict]:
    """All turtle-soup events in a multi-day 5-minute frame. side = the direction the sweep SHOULD reverse to."""
    P = P or im.rules()
    T = P["turtle_soup"]
    if d5 is None or d5.empty:
        return []
    d5 = d5.sort_values("timestamp").reset_index(drop=True)
    day = d5["timestamp"].dt.date
    rhl = im.day_hilo(ref5)
    rdays = sorted(rhl)
    out, prev_hi, prev_lo = [], np.nan, np.nan
    for dd in sorted(day.unique()):
        g = d5[day == dd]
        t = g["timestamp"].to_numpy()
        o, h, l, c = (g[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        if len(c) >= 20:
            pr = [x for x in rdays if x < dd]
            ref = im.ref_arrays(g, ref5, rhl[pr[-1]] if pr else None)
            for e in _events_long(t, o, h, l, c, prev_lo, P, T, ref):
                e.update(day=str(dd), side="BULLISH")
                out.append(e)
            for e in _events_long(t, -o, -l, -h, -c, -prev_hi if not np.isnan(prev_hi) else np.nan, P, T, im._neg_ref(ref)):
                e["level"], e["close"] = -e["level"], -e["close"]
                e.update(day=str(dd), side="BEARISH")
                out.append(e)
        prev_hi, prev_lo = float(h.max()), float(l.min())
    out.sort(key=lambda e: e["t"])
    return out
