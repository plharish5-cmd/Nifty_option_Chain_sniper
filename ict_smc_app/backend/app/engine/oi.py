"""
engine/oi.py — futures OI build-up per candle (current-month NIFTY future). Context only, never a trigger.

    price ↑ + OI ↑  LONG BUILD-UP     new longs are paying up
    price ↓ + OI ↑  SHORT BUILD-UP    new shorts are pressing
    price ↑ + OI ↓  SHORT COVERING    shorts are buying back (rallies on covering fade once it's done)
    price ↓ + OI ↓  LONG UNWINDING    longs are bailing out
A candle counts only if |ΔOI| ≥ oi_min_pct of OI and |Δprice| ≥ price_min_atr × ATR, else "flat".
ΔOI = OI at the candle's last minute − OI at the previous candle's last minute (same session only: OI is
reported per contract and a rollover / new day would fake a huge change). Only earlier data → no look-ahead.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import config
from ..timeutil import TF_SECONDS, bucket_start, ist_date

NAMES = {("up", "up"): "long build-up", ("down", "up"): "short build-up",
         ("up", "down"): "short covering", ("down", "down"): "long unwinding"}
BIAS = {"long build-up": "bull", "short covering": "bull", "short build-up": "bear", "long unwinding": "bear"}


def per_bar(an, oi_by_min: Dict[int, float]) -> Dict[int, Dict[str, Any]]:
    if not oi_by_min or an is None:
        return {}
    min_pct = float(config.rule("oi", "oi_min_pct", 0.05)) / 100
    pmin = float(config.rule("oi", "price_min_atr", 0.1))
    b, A = an.bars, an.atr
    step = TF_SECONDS.get(an.tf, 300)
    mins = sorted(oi_by_min)
    out: Dict[int, Dict[str, Any]] = {}
    prev: Optional[tuple] = None                       # (day, oi, close)
    for i in range(len(b)):
        ts = int(b.ts[i])
        last = None
        for m in range(ts + step - 60, ts - 60, -60):   # OI at the candle's last minute that has one
            if m in oi_by_min:
                last = oi_by_min[m]
                break
        day = ist_date(ts)
        c = float(b.c[i])
        if last is None:
            prev = None if prev and prev[0] != day else prev
            continue
        rec: Dict[str, Any] = {"oi": round(last)}
        if prev and prev[0] == day:
            doi, dp = last - prev[1], c - prev[2]
            rec["doi"] = round(doi)
            rec["doi_pct"] = round(100 * doi / prev[1], 3) if prev[1] else None
            if abs(doi) >= min_pct * prev[1] and abs(dp) >= pmin * float(A[i]):
                rec["build"] = NAMES[("up" if dp > 0 else "down", "up" if doi > 0 else "down")]
            else:
                rec["build"] = "flat"
        out[ts] = rec
        prev = (day, last, c)
    return out


def summary(an, bars: Dict[int, Dict[str, Any]], lookback: int = 12) -> Optional[Dict[str, Any]]:
    """Net picture over the last `lookback` candles of today: which build-up dominates, net ΔOI."""
    if not bars:
        return None
    b = an.bars
    today = ist_date(int(b.ts[-1]))
    recs = [bars[int(t)] for t in b.ts[-lookback:] if int(t) in bars and ist_date(int(t)) == today and "build" in bars[int(t)]]
    if not recs:
        return None
    net = sum(r.get("doi", 0) for r in recs)
    counts: Dict[str, int] = {}
    for r in recs:
        if r["build"] != "flat":
            counts[r["build"]] = counts.get(r["build"], 0) + 1
    top = max(counts, key=counts.get) if counts else "flat"
    last = recs[-1]
    return {"net_doi": round(net), "dominant": top, "bias": BIAS.get(top), "counts": counts, "bars": len(recs),
            "last_build": last["build"], "last_doi": last.get("doi"), "oi": last["oi"]}
