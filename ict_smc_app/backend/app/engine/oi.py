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


# ── OI line with its 8-EMA, ΔOI bars and the OI SURGE flag ───────────────────
def ema(vals: List[float], n: int) -> List[float]:
    k = 2.0 / (n + 1)
    out: List[float] = []
    for v in vals:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def oi_line(an, bars: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per candle that has OI: {ts, oi, ema, doi, build, surge}. EMA length = oi.ema_len (8).
    SURGE = ΔOI ≥ oi.surge_k × the average |ΔOI| of the previous oi.surge_window candles (same session, earlier
    candles only) AND OI above its EMA → positions are being added unusually fast: a big move is building.
    Direction is NOT implied — the build-up type (price + OI) and structure decide that."""
    n = int(config.rule("oi", "ema_len", 8))
    k = float(config.rule("oi", "surge_k", 2.0))
    W = int(config.rule("oi", "surge_window", 20))
    pts = [(int(t), bars[int(t)]) for t in an.bars.ts if int(t) in bars]
    if not pts:
        return []
    e = ema([float(r["oi"]) for _, r in pts], n)
    out: List[Dict[str, Any]] = []
    hist: List[float] = []
    day = None
    for (ts, r), ev in zip(pts, e):
        d = ist_date(ts)
        if d != day:
            day, hist = d, []
        doi = r.get("doi")
        base = sum(hist[-W:]) / len(hist[-W:]) if len(hist) >= 5 else None
        surge = bool(doi is not None and doi > 0 and base and doi >= k * base and r["oi"] > ev)
        out.append({"ts": ts, "oi": r["oi"], "ema": round(ev), "doi": doi, "build": r.get("build"), "surge": surge,
                    "surge_x": round(doi / base, 1) if (doi and base) else None})
        if doi is not None:
            hist.append(abs(doi))
    return out


def compare(n_sum: Optional[Dict[str, Any]], b_sum: Optional[Dict[str, Any]],
            n_last: Optional[str], b_last: Optional[str]) -> Optional[Dict[str, Any]]:
    """NIFTY vs BANKNIFTY futures build-up. Same side → broad move (higher confidence); opposite → narrow move
    (more likely to fail — where CRT traps work)."""
    if not n_sum or not b_sum:
        return None
    nb, bb = n_sum.get("bias"), b_sum.get("bias")
    if nb and bb and nb == bb:
        verdict, word = "aligned", f"broad {'buying' if nb == 'bull' else 'selling'}: both show {n_sum['dominant']} / {b_sum['dominant']}"
    elif nb and bb:
        verdict, word = "diverging", (f"narrow move: NIFTY {n_sum['dominant']} but BANKNIFTY {b_sum['dominant']} — "
                                     f"more likely to fail")
    else:
        verdict, word = "unclear", "one side flat — no confirmation"
    return {"verdict": verdict, "text": word, "nifty": n_sum.get("dominant"), "banknifty": b_sum.get("dominant"),
            "nifty_last": n_last, "banknifty_last": b_last}
