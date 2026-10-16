"""
mtf_api.py — /api/mtf : Multi-timeframe read (Daily / Weekly / Monthly). Display only — never places orders.

Every number is computed from the SAME live basis-adjusted NIFTY-FUT candles the chart uses
(hub rows → futures.mapped), so the panel always agrees with the tape. Three genuinely different reads:
    Daily    = today's session, 5-minute bars   → intraday trend / ATR
    Weekly   = last 5 sessions, 1-hour bars      → swing trend / ATR
    Monthly  = last 20 sessions, per-session bars→ positional trend / ATR
Confluence (★ A+ / ★ A) is REUSED from engine.nodes.stacked() — the real D/W/M volume-node overlap,
not a second estimate. Trend = EMA 9/21/50 stack. Volatility = recent ATR vs older ATR.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from .. import config
from ..engine.nodes import stacked
from ..market import futures as fut_mod
from ..timeutil import ist_date

router = APIRouter()
_cache: Dict[str, Any] = {"t": 0.0, "j": None}


# ── aggregation ───────────────────────────────────────────────────────────────
def _agg(rows: List[Dict[str, float]], bucket: int) -> List[Dict[str, float]]:
    """Aggregate 1-minute mapped rows into OHLCV bars of `bucket` seconds."""
    by: Dict[int, Dict[str, float]] = {}
    for r in rows:
        k = int(r["ts"]) // bucket * bucket
        b = by.get(k)
        if b is None:
            by[k] = {"ts": k, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": float(r.get("v") or 0)}
        else:
            b["h"] = max(b["h"], r["h"]); b["l"] = min(b["l"], r["l"]); b["c"] = r["c"]; b["v"] += float(r.get("v") or 0)
    return [by[k] for k in sorted(by)]


def _by_session(rows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """One OHLCV bar per IST session (for the monthly/positional read)."""
    by: Dict[str, Dict[str, float]] = {}
    for r in rows:
        d = ist_date(r["ts"]).isoformat()
        b = by.get(d)
        if b is None:
            by[d] = {"d": d, "ts": int(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": float(r.get("v") or 0)}
        else:
            b["h"] = max(b["h"], r["h"]); b["l"] = min(b["l"], r["l"]); b["c"] = r["c"]; b["v"] += float(r.get("v") or 0)
    return [by[d] for d in sorted(by)]


# ── indicators ────────────────────────────────────────────────────────────────
def _atr(bars: List[Dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    tr = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    n = min(period, len(tr))
    return round(sum(tr[-n:]) / n, 2) if n else 0.0


def _ema(vals: List[float], period: int) -> Optional[float]:
    if len(vals) < 2:
        return None
    p = min(period, len(vals))
    mult = 2 / (p + 1)
    e = sum(vals[:p]) / p
    for v in vals[p:]:
        e = v * mult + e * (1 - mult)
    return round(e, 2)


def _trend(bars: List[Dict[str, float]]) -> Dict[str, Any]:
    closes = [b["c"] for b in bars]
    e9, e21, e50 = _ema(closes, 9), _ema(closes, 21), _ema(closes, 50)
    if None in (e9, e21, e50):
        return {"ema9": e9, "ema21": e21, "ema50": e50, "status": "—", "bars": len(bars)}
    status = "BULLISH" if e9 > e21 > e50 else "BEARISH" if e9 < e21 < e50 else "CHOPPY"
    return {"ema9": e9, "ema21": e21, "ema50": e50, "status": status, "bars": len(bars)}


def _volatility(bars: List[Dict[str, float]]) -> str:
    if len(bars) < 10:
        return "—"
    recent = _atr(bars[-5:], 5)
    older = _atr(bars[-20:-5], 10) if len(bars) >= 20 else _atr(bars[:-5], 10)
    if not older:
        return "STABLE"
    return "EXPANDING" if recent > older * 1.2 else "CONTRACTING" if recent < older * 0.8 else "STABLE"


def _tf_block(bars: List[Dict[str, float]], label: str, note: str) -> Dict[str, Any]:
    if not bars:
        return {"tf": label, "available": False, "note": note}
    delta = round(sum(b["c"] - b["o"] for b in bars[-10:]), 1)   # order-flow proxy: net body of last 10 bars
    return {
        "tf": label, "available": True, "bars": len(bars),
        "last": round(bars[-1]["c"], 1),
        "atr": _atr(bars), "trend": _trend(bars), "volatility": _volatility(bars),
        "delta10": delta, "delta_dir": "BUY" if delta > 0 else "SELL" if delta < 0 else "FLAT",
        "note": note,
    }


# ── confluence from the real stacked nodes ────────────────────────────────────
def _confluence(mp: List[Dict[str, float]], spot: Optional[float]) -> List[Dict[str, Any]]:
    try:
        s = stacked(mp, float(config.rule("nodes", "step_pts", 5.0)),
                    float(config.rule("nodes", "tol_pts", 15.0)),
                    float(config.rule("nodes", "half_pts", 7.5)))
    except Exception:
        return []
    if not s.get("available"):
        return []
    out = []
    for z in s.get("zones", []):
        out.append({
            "price": z["mid"], "lo": z["lo"], "hi": z["hi"],
            "strength": 3 if z["grade"] == "A+" else 2,
            "label": f"★ {z['grade']} ({'+'.join(z['members'])})",
            "members": z["members"],
            "dist": round(z["mid"] - spot, 1) if spot else None,
        })
    # nearest to spot first
    if spot is not None:
        out.sort(key=lambda z: abs(z["price"] - spot))
    else:
        out.sort(key=lambda z: -z["strength"])
    return out


@router.get("/api/mtf")
def mtf():
    if _cache["j"] is not None and time.time() - _cache["t"] < 120:
        return _cache["j"]
    try:
        from .main import _h
        h = _h()
        mp = fut_mod.mapped(h.rows_1m(32), fut_mod.rows_1m(32))
        if not mp:
            j = {"available": False, "message": "no futures candles yet (market closed or feed starting)"}
            _cache.update(t=time.time(), j=j)
            return j

        spot = round(mp[-1]["c"], 1)
        sessions = _by_session(mp)
        today = sessions[-1]["d"] if sessions else None
        today_rows = [r for r in mp if ist_date(r["ts"]).isoformat() == today]
        last5 = {s["d"] for s in sessions[-5:]}
        last5_rows = [r for r in mp if ist_date(r["ts"]).isoformat() in last5]

        daily = _agg(today_rows, 300)                 # today, 5-minute bars
        weekly = _agg(last5_rows, 3600)               # last 5 sessions, 1-hour bars
        monthly = sessions[-20:]                       # last 20 sessions, daily bars

        j = {
            "available": True,
            "spot": spot,
            "asof": today,
            "D": _tf_block(daily, "Daily", "Today · 5-minute bars · intraday trend"),
            "W": _tf_block(weekly, "Weekly", "Last 5 sessions · 1-hour bars · swing trend"),
            "M": _tf_block(monthly, "Monthly", "Last 20 sessions · daily bars · positional trend"),
            "confluence": _confluence(mp, spot),
            "source": "live NIFTY-FUT (basis-adjusted) — matches the chart",
        }
    except Exception as e:                            # never break the desk over a display layer
        j = {"available": False, "error": f"mtf error: {e}"}
    _cache.update(t=time.time(), j=j)
    return j
