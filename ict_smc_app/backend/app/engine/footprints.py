"""
engine/footprints.py — ESTIMATED footprint ladder per closed candle (5m · 15m · 30m · 1h), pure Python / NumPy.

Source: the desk's own live recorder (delta/recorder.py, source "FUTQR"). Every NIFTY-future tick was classified
buy / sell by the quote rule (trade at ask = buy, at bid = sell, in between = tick rule) and booked at its
basis-adjusted NIFTY price row (delta.fut_level_step points). So every number here is an ESTIMATE of aggressor
side — NSE does not publish who hit whom — and it exists ONLY for minutes the desk was running and recording.
Nothing is back-filled or invented: a candle with too few recorded minutes is marked partial / no data.

Per candle:
  ladder        price rows (high → low): sell × buy, delta, volume; POC (max volume row), value area (70 %)
  delta         Σbuy − Σsell, delta %, CVD (session), max / min intra-candle cumulative delta (from 1-minute rows)
  imbalances    diagonal: buy[p] ≥ k × sell[p − step]  (buy imbalance) · sell[p] ≥ k × buy[p + step] (sell imb.)
                stacked = ≥ 3 consecutive imbalance rows (initiative zone)
  unfinished    both buyers and sellers traded at the candle's extreme row → the auction there is not finished
  absorption    heavy volume (≥ 1.5× the average candle) + strong delta one way, but price did not follow
                (small body / closed against the delta) → passive orders soaked the aggression up
  divergence    new N-candle high with negative delta (bearish) · new low with positive delta (bullish)
  manipulation  wick through the previous candle's high/low, close back inside, and the volume in that wick was
                aggression INTO the sweep (trapped buyers above / trapped sellers below) → stop-run
  distribution  POC in the upper third, negative delta, close in the lower half → sellers unloading into strength
  accumulation  POC in the lower third, positive delta, close in the upper half → buyers absorbing weakness
  initiative    |delta| ≥ 25 % of volume, close in the delta direction, range ≥ average → aggression that moved price
Information only — no orders, never feeds Setups A/B/C/D.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

import numpy as np

from .. import db
from ..timeutil import TF_SECONDS, bucket_start, chart_time, hm, ist_date

INST = "FUTQR"
DEF = {"imb_k": 3.0, "imb_min": 50.0, "stack": 3, "abs_vol_x": 1.5, "abs_body": 0.35, "div_lookback": 6,
       "init_delta_pct": 25.0, "min_coverage": 0.6}


def _levels(t0: int, t1: int) -> Dict[int, Dict[float, List[float]]]:
    out: Dict[int, Dict[float, List[float]]] = defaultdict(dict)
    for r in db.query("SELECT ts, price, buy, sell FROM footprint_levels WHERE inst=? AND ts>=? AND ts<? ORDER BY ts",
                      (INST, t0, t1)):
        out[int(r["ts"])][float(r["price"])] = [float(r["buy"]), float(r["sell"])]
    return out


def _minutes(t0: int, t1: int) -> Dict[int, float]:
    return {int(r["ts"]): float(r["delta"]) for r in
            db.query("SELECT ts, delta FROM delta_bars WHERE inst=? AND ts>=? AND ts<? ORDER BY ts", (INST, t0, t1))}


def compute(rows_1m: List[Dict[str, Any]], tf: str, n_bars: int = 16, step: float = 5.0,
            zones: Optional[List[Dict[str, Any]]] = None, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    C = {**DEF, **(cfg or {})}
    sec = TF_SECONDS[tf]
    closed = [r for r in rows_1m if r.get("closed", True)]
    if not closed:
        return {"tf": tf, "available": False, "message": "no closed 1-minute candles yet", "bars": []}
    # aggregate CLOSED 1m index candles into tf candles; drop the still-forming tf candle
    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in closed:
        groups[bucket_start(int(r["ts"]), tf)].append(r)
    last_min = int(closed[-1]["ts"])
    keys = sorted(groups)
    if keys and last_min + 60 < keys[-1] + sec:                 # last tf candle not finished yet
        keys = keys[:-1]
    keys = keys[-(n_bars + int(C["div_lookback"])):]
    if not keys:
        return {"tf": tf, "available": False, "message": "no closed candles for this timeframe yet", "bars": []}
    lv = _levels(keys[0], keys[-1] + sec)
    dm = _minutes(keys[0], keys[-1] + sec)
    bars: List[Dict[str, Any]] = []
    for k in keys:
        g = groups[k]
        o, c = float(g[0]["o"]), float(g[-1]["c"])
        h, l = max(float(x["h"]) for x in g), min(float(x["l"]) for x in g)
        mins = [int(x["ts"]) for x in g]
        rec = [m for m in mins if m in lv]
        lad: Dict[float, List[float]] = defaultdict(lambda: [0.0, 0.0])
        for m in rec:
            for p, (bv, sv) in lv[m].items():
                lad[p][0] += bv
                lad[p][1] += sv
        cum, mx, mn = 0.0, 0.0, 0.0
        for m in mins:
            cum += dm.get(m, 0.0)
            mx, mn = max(mx, cum), min(mn, cum)
        bars.append({"ts": k, "o": o, "h": h, "l": l, "c": c, "lad": dict(lad), "cov": len(rec) / max(1, len(mins)),
                     "max_d": mx, "min_d": mn})
    vols = [sum(b_ + s_ for b_, s_ in b["lad"].values()) for b in bars]
    avg_v = float(np.mean([v for v in vols if v > 0])) if any(v > 0 for v in vols) else 0.0
    avg_r = float(np.mean([b["h"] - b["l"] for b in bars])) or 1.0
    zones = zones or []
    out: List[Dict[str, Any]] = []
    cvd, day = 0.0, None
    for i, b in enumerate(bars):
        d_ = ist_date(b["ts"])
        if d_ != day:
            cvd, day = 0.0, d_
        lad = b["lad"]
        buy = sum(x[0] for x in lad.values())
        sell = sum(x[1] for x in lad.values())
        vol, delta = buy + sell, buy - sell
        cvd += delta
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        rng = max(h - l, 1e-9)
        base = {"time": chart_time(b["ts"]), "at": hm(b["ts"]), "o": o, "h": h, "l": l, "c": c,
                "coverage": round(b["cov"], 2), "vol": round(vol), "buy": round(buy), "sell": round(sell),
                "delta": round(delta), "cvd": round(cvd), "max_d": round(b["max_d"]), "min_d": round(b["min_d"])}
        if b["cov"] < C["min_coverage"] or vol <= 0:
            out.append({**base, "partial": True, "rows": [], "tags": [],
                        "read": (f"only {b['cov']:.0%} of this candle's minutes were recorded — no footprint"
                                 if vol > 0 else "no recorded ticks (desk was not running / recorder off)")})
            continue
        prices = sorted(lad, reverse=True)
        poc = max(prices, key=lambda p: lad[p][0] + lad[p][1])
        # value area: grow from POC until 70 % of volume
        order = sorted(prices, key=lambda p: -(lad[p][0] + lad[p][1]))
        acc, va = 0.0, []
        for p in order:
            va.append(p)
            acc += lad[p][0] + lad[p][1]
            if acc >= 0.7 * vol:
                break
        k_, mn_ = C["imb_k"], C["imb_min"]
        rows = []
        for p in prices:
            bv, sv = lad[p]
            below = lad.get(round(p - step, 2), [0.0, 0.0])[1]
            above = lad.get(round(p + step, 2), [0.0, 0.0])[0]
            bi = bv >= mn_ and bv >= k_ * max(below, 1.0)
            si = sv >= mn_ and sv >= k_ * max(above, 1.0)
            rows.append({"p": p, "buy": round(bv), "sell": round(sv), "d": round(bv - sv), "v": round(bv + sv),
                         "bi": bool(bi), "si": bool(si), "poc": p == poc, "va": p in va})
        tags: List[Dict[str, str]] = []

        def stacked(key):
            run, best, at = 0, 0, None
            for r in rows:
                run = run + 1 if r[key] else 0
                if run > best:
                    best, at = run, r["p"]
            return best, at
        sb, sbp = stacked("bi")
        ss, ssp = stacked("si")
        if sb >= C["stack"]:
            tags.append({"k": "STACKED_BUY", "dir": "bull", "t": f"{sb} stacked buy imbalances (top row {sbp:,.0f}) — buyers initiating"})
        if ss >= C["stack"]:
            tags.append({"k": "STACKED_SELL", "dir": "bear", "t": f"{ss} stacked sell imbalances (top row {ssp:,.0f}) — sellers initiating"})
        top, bot = rows[0], rows[-1]
        ufm = 2 * C["imb_min"]                                   # a few stray contracts at the extreme are not "unfinished"
        if top["buy"] >= ufm and top["sell"] >= ufm:
            tags.append({"k": "UNFINISHED_HIGH", "dir": "bull", "t": f"unfinished high {top['p']:,.0f} — likely revisited"})
        if bot["buy"] >= ufm and bot["sell"] >= ufm:
            tags.append({"k": "UNFINISHED_LOW", "dir": "bear", "t": f"unfinished low {bot['p']:,.0f} — likely revisited"})
        dp = 100.0 * delta / vol
        body = abs(c - o) / rng
        heavy = avg_v > 0 and vol >= C["abs_vol_x"] * avg_v
        if heavy and abs(dp) >= 15 and (body <= C["abs_body"] or (delta > 0) != (c > o)):
            zone = next((z for z in zones if z.get("bottom", 0) - 0.2 * avg_r <= (h if delta > 0 else l) <= z.get("top", 0) + 0.2 * avg_r), None)
            who = "buyers absorbed by passive SELLERS" if delta > 0 else "sellers absorbed by passive BUYERS"
            tags.append({"k": "ABSORPTION", "dir": "bear" if delta > 0 else "bull",
                         "t": f"ABSORPTION — {vol / avg_v:.1f}× volume, Δ {dp:+.0f}%, price did not follow: {who}"
                              + (f" at {zone.get('label', zone.get('type'))}" if zone else "")})
        L = int(C["div_lookback"])
        prev = bars[max(0, i - L):i]
        if prev:
            if h > max(x["h"] for x in prev) and delta < 0:
                tags.append({"k": "DIVERGENCE", "dir": "bear", "t": "bearish delta divergence — new high on negative delta"})
            if l < min(x["l"] for x in prev) and delta > 0:
                tags.append({"k": "DIVERGENCE", "dir": "bull", "t": "bullish delta divergence — new low on positive delta"})
        if i > 0:
            pb = bars[i - 1]
            if h > pb["h"] and c < pb["h"]:
                wb = sum(lad[p][0] for p in prices if p >= pb["h"])
                ws = sum(lad[p][1] for p in prices if p >= pb["h"])
                if wb > ws and wb > 0:
                    tags.append({"k": "MANIPULATION", "dir": "bear",
                                 "t": f"MANIPULATION — swept {pb['h']:,.0f}, buyers bought the wick ({wb:,.0f} vs {ws:,.0f}) and closed back below: trapped longs"})
            if l < pb["l"] and c > pb["l"]:
                wb = sum(lad[p][0] for p in prices if p <= pb["l"])
                ws = sum(lad[p][1] for p in prices if p <= pb["l"])
                if ws > wb and ws > 0:
                    tags.append({"k": "MANIPULATION", "dir": "bull",
                                 "t": f"MANIPULATION — swept {pb['l']:,.0f}, sellers sold the wick ({ws:,.0f} vs {wb:,.0f}) and closed back above: trapped shorts"})
        third = (poc - l) / rng
        if third >= 0.66 and delta < 0 and (c - l) / rng <= 0.5:
            tags.append({"k": "DISTRIBUTION", "dir": "bear", "t": "DISTRIBUTION — volume built at the highs, negative delta, close in the lower half"})
        elif third <= 0.34 and delta > 0 and (c - l) / rng >= 0.5:
            tags.append({"k": "ACCUMULATION", "dir": "bull", "t": "ACCUMULATION — volume built at the lows, positive delta, close in the upper half"})
        if abs(dp) >= C["init_delta_pct"] and (delta > 0) == (c > o) and rng >= avg_r:
            tags.append({"k": "INITIATIVE", "dir": "bull" if delta > 0 else "bear",
                         "t": f"INITIATIVE {'BUYING' if delta > 0 else 'SELLING'} — Δ {dp:+.0f}% moved price {c - o:+.0f} pts"})
        lead = next((t for t in tags if t["k"] in ("ABSORPTION", "MANIPULATION", "DISTRIBUTION", "ACCUMULATION", "INITIATIVE")), None)
        read = (lead["t"] if lead else
                f"{'buyers' if delta > 0 else 'sellers'} slightly ahead (Δ {dp:+.0f}%) — no strong footprint pattern")
        out.append({**base, "partial": b["cov"] < 0.95, "poc": poc, "va_hi": max(va), "va_lo": min(va),
                    "delta_pct": round(dp, 1), "rows": rows, "tags": tags, "read": read})
    out = out[-n_bars:]
    have = [x for x in out if x["rows"]]
    if not have:
        msg = ("not enough recorded ticks for these candles yet — the footprint exists only for minutes the desk was "
               "running with the 'Estimated (FUT quote rule)' recorder (a candle needs ≥ 60 % of its minutes)")
        return {"tf": tf, "available": False, "message": msg, "bars": out, "step": step}
    last = have[-1]
    s = (f"Last closed {tf} candle {last['at']}: Δ {last['delta']:+,} ({last['delta_pct']:+.0f}%), POC {last['poc']:,.0f}, "
         f"CVD {last['cvd']:+,}. {last['read']}.")
    return {"tf": tf, "available": True, "estimated": True, "source": "NIFTY FUT ticks · quote rule (estimated aggressor)",
            "step": step, "bars": out, "summary": s,
            "markers": [{"time": x["time"], "kind": "FP_" + t["k"], "dir": t["dir"], "price": x["h"] if t["dir"] == "bear" else x["l"],
                         "text": t["t"], "at": x["at"]}
                        for x in have for t in x["tags"] if t["k"] in ("ABSORPTION", "MANIPULATION", "DISTRIBUTION", "ACCUMULATION")]}
