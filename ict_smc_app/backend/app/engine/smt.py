"""
engine/smt.py — SMT divergence between NIFTY and BANKNIFTY (information only, closed candles only).

Bearish SMT: at a confirmed swing high one index makes a HIGHER high than its previous swing high while the other
makes a LOWER high (one fails to confirm) → the sweep of the high is weak → bias down.
Bullish SMT: at a confirmed swing low one index makes a LOWER low while the other makes a HIGHER low → bias up.

Pure: same input → same output. Swing = n-bar pivot (n bars each side), so a pivot at bar i is only known at bar i+n
— the marker is stamped at the CONFIRMATION bar (no look-ahead).
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..timeutil import chart_time, hm


def _pivots(vals: List[float], n: int, high: bool) -> List[int]:
    out = []
    for i in range(n, len(vals) - n):
        w = vals[i - n:i + n + 1]
        if (high and vals[i] == max(w) and w.count(vals[i]) == 1) or (not high and vals[i] == min(w) and w.count(vals[i]) == 1):
            out.append(i)
    return out


def detect(nifty: List[Dict[str, float]], bank: List[Dict[str, float]], n: int = 2, lookback: int = 30,
           tol_bars: int = 1) -> Dict[str, Any]:
    """nifty / bank: closed candles {ts, o, h, l, c} on the same timeframe. Returns {markers, last, aligned}."""
    bm = {int(r["ts"]): r for r in bank}
    rows = [(r, bm[int(r["ts"])]) for r in nifty if int(r["ts"]) in bm]       # only bars both indices have
    if len(rows) < 2 * n + 3:
        return {"markers": [], "last": None, "aligned": len(rows)}
    ts = [int(a["ts"]) for a, _ in rows]
    nh, nl = [float(a["h"]) for a, _ in rows], [float(a["l"]) for a, _ in rows]
    bh, bl = [float(b["h"]) for _, b in rows], [float(b["l"]) for _, b in rows]

    def near(vals, i, high):                     # the other index's extreme within ±tol bars of the pivot
        w = vals[max(0, i - tol_bars): i + tol_bars + 1]
        return max(w) if high else min(w)

    out: List[Dict[str, Any]] = []
    for high in (True, False):
        piv = _pivots(nh if high else nl, n, high)
        for k in range(1, len(piv)):
            i, j = piv[k], piv[k - 1]
            if i - j > lookback:
                continue
            a_now, a_prev = (nh[i], nh[j]) if high else (nl[i], nl[j])
            b_now, b_prev = (near(bh, i, True), near(bh, j, True)) if high else (near(bl, i, False), near(bl, j, False))
            if high:
                n_hh, b_hh = a_now > a_prev, b_now > b_prev
                if n_hh != b_hh:
                    who = "NIFTY higher high, BANKNIFTY lower high" if n_hh else "BANKNIFTY higher high, NIFTY lower high"
                    out.append(_mk(ts, i, n, "bear", f"Bearish SMT — {who}: weak sweep of the highs", a_now))
            else:
                n_ll, b_ll = a_now < a_prev, b_now < b_prev
                if n_ll != b_ll:
                    who = "NIFTY lower low, BANKNIFTY higher low" if n_ll else "BANKNIFTY lower low, NIFTY higher low"
                    out.append(_mk(ts, i, n, "bull", f"Bullish SMT — {who}: weak sweep of the lows", a_now))
    out.sort(key=lambda m: m["ts"])
    forming = _forming(ts, nh, nl, bh, bl, n, lookback)
    return {"markers": out, "last": out[-1] if out else None, "aligned": len(rows), "forming": forming}


def _forming(ts: List[int], nh: List[float], nl: List[float], bh: List[float], bl: List[float],
             n: int, lookback: int) -> List[Dict[str, Any]]:
    """PROVISIONAL SMT: a divergence printing at the current running extreme BEFORE the swing pivot confirms.
    Repaints as new bars print — explicitly marked provisional. NEVER mixed into `markers` (the confirmed, closed-
    candle signal), so the confirmed path never repaints and 'closed candles only' still holds for real signals."""
    out: List[Dict[str, Any]] = []
    for high in (True, False):
        vn, vb = (nh, bh) if high else (nl, bl)
        piv = _pivots(vn, n, high)
        if not piv:
            continue
        j = piv[-1]
        tail = list(range(j + 1, len(vn)))
        if not tail:
            continue
        k = max(tail, key=lambda x: vn[x]) if high else min(tail, key=lambda x: vn[x])
        if (len(vn) - 1 - k) > n + 2 or (k - j) > lookback:        # only a genuinely RECENT forming extreme
            continue
        if high:
            n_div, b_div = vn[k] > vn[j], max(vb[t] for t in tail) > vb[j]
        else:
            n_div, b_div = vn[k] < vn[j], min(vb[t] for t in tail) < vb[j]
        if n_div == b_div:                                         # both confirm or both fail → no divergence
            continue
        d = "bear" if high else "bull"
        now = ts[-1]
        out.append({"ts": now, "time": chart_time(now), "at": hm(now), "pivot_at": hm(ts[k]),
                    "kind": "SMT_FORMING", "dir": d, "provisional": True, "price": round(vn[k], 2),
                    "text": f"forming {'Bearish' if high else 'Bullish'} SMT — divergence printing, pivot not yet "
                            f"confirmed; wait for the candle to close before acting"})
    return out


def _mk(ts: List[int], i: int, n: int, d: str, text: str, price: float) -> Dict[str, Any]:
    c = ts[min(i + n, len(ts) - 1)]                  # stamped when the pivot is CONFIRMED
    return {"ts": c, "time": chart_time(c), "pivot_time": chart_time(ts[i]), "at": hm(c), "pivot_at": hm(ts[i]),
            "kind": "SMT", "dir": d, "price": round(price, 2), "text": text}
