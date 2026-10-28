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
from pathlib import Path
from functools import lru_cache
import json
import math
from ..timeutil import now_epoch, now_ist, session_bounds
from ..timeutil import TF_SECONDS, bucket_start, ist_date

NAMES = {("up", "up"): "long build-up", ("down", "up"): "short build-up",
         ("up", "down"): "short covering", ("down", "down"): "long unwinding"}
BIAS = {"long build-up": "bull", "short covering": "bull", "short build-up": "bear", "long unwinding": "bear"}


@lru_cache(maxsize=1)
def display_config():
    return json.loads((Path(__file__).resolve().parents[4]/"analyst"/"analyst_config.json").read_text())["futures_oi_display"]


def per_bar(an, oi_by_min: Dict[int, Any]) -> Dict[int, Dict[str, Any]]:
    if not oi_by_min or an is None:
        return {}
    min_pct = float(config.rule("oi", "oi_min_pct", 0.05)) / 100
    pmin = float(config.rule("oi", "price_min_atr", 0.1))
    b, A = an.bars, an.atr
    step = TF_SECONDS.get(an.tf, 300)
    out = {}
    prev = None
    for i in range(len(b)):
        ts = int(b.ts[i]); day = ist_date(ts); close = float(b.c[i])
        sample_ts = next((m for m in range(ts+step-60,ts-60,-60) if m in oi_by_min),None)
        if sample_ts is None:
            out[ts] = {"quality":"missing", "sample_ts":None}
            prev = None
            continue
        sample = oi_by_min[sample_ts]
        meta = sample if isinstance(sample,dict) else {"oi":sample, "source":"legacy", "contract":None}
        value = float(meta["oi"])
        quality = "ok" if not isinstance(sample,dict) or meta.get("verified") else "unverified_contract"
        if not math.isfinite(value) or value<=0:
            quality = "invalid"
        elif ts+step-60-sample_ts > display_config()["sample_max_lag_s"]:
            quality = "stale_sample"
        rec = dict(oi=round(value) if math.isfinite(value) else None,quality=quality,
                   contract=meta.get("contract"),source=meta.get("source"),sample_ts=sample_ts,doi=None,doi_pct=None,dp=None)
        if quality != "ok":
            out[ts]=rec;prev=None;continue
        continuous = prev and prev["day"]==day and prev["ts"]+step==ts
        if continuous and prev["contract"] != rec["contract"]:
            rec["quality"]="contract_change";continuous=False
        if continuous:
            doi=value-prev["oi"];dp=close-prev["close"]
            rec.update(doi=round(doi),doi_pct=round(100*doi/prev["oi"],3),dp=round(dp,2))
            rec["build"] = NAMES[("up" if dp>0 else "down", "up" if doi>0 else "down")] if abs(doi)>=min_pct*prev["oi"] and abs(dp)>=pmin*float(A[i]) else "flat"
        out[ts]=rec
        prev=dict(day=day,ts=ts,oi=value,close=close,contract=rec["contract"])
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
    """Signed relative ΔOI uses only earlier same-session continuous samples."""
    n=int(config.rule("oi","ema_len",8)); k=float(config.rule("oi","surge_k",2.0))
    W=int(config.rule("oi","surge_window",20)); settings=display_config()
    out=[];hist=[];relative_hist=[];ev=None;previous=None
    for timestamp in an.bars.ts:
        ts=int(timestamp);r=bars.get(ts,{})
        if r.get("oi") is None:
            hist=[];relative_hist=[];ev=None;previous=None
            continue
        identity=(ist_date(ts),r.get("contract"))
        valid=r.get("quality","ok")=="ok"
        if previous is None or previous[0]!=identity or previous[1]+TF_SECONDS[an.tf]!=ts or r.get("doi") is None or not valid:
            hist=[];relative_hist=[];ev=None
        ev=float(r["oi"]) if ev is None else ev+2/(n+1)*(r["oi"]-ev)
        doi=r.get("doi") if valid else None
        base=sum(hist[-W:])/len(hist[-W:]) if len(hist)>=settings["relative_min_bars"] else None
        rb=relative_hist[-settings["relative_window"]:]
        relative_base=sum(rb)/len(rb) if len(rb)>=settings["relative_min_bars"] else None
        relative=doi/relative_base if doi is not None and relative_base else None
        expansion=bool(doi is not None and doi>0 and base and doi>=k*base and r["oi"]>ev)
        reduction=bool(doi is not None and doi<0 and base and -doi>=k*base)
        out.append(dict(r,ts=ts,ema=round(ev),doi=doi,build=r.get("build"),surge=expansion,
                        reduction=reduction,surge_x=round(abs(doi)/base,1) if doi is not None and base else None,
                        relative=round(relative,3) if relative is not None else None,
                        relative_base=round(relative_base,2) if relative_base else None))
        if doi is not None:
            hist.append(abs(doi));relative_hist.append(abs(doi))
        previous=(identity,ts) if valid else None
    return out


def health(an,bars):
    cfg=display_config();last=bars.get(int(an.bars.ts[-1]),{}) if len(an.bars.ts) else {}
    sampled=last.get("sample_ts");now=int(now_epoch());_,end=session_bounds(now_ist().date())
    age=max(0,now-(sampled+60)) if sampled is not None else None
    state=last.get("quality","missing")
    if now>=end:
        state="market_closed" if sampled is not None else "missing"
    elif age is not None and age>cfg["freshness_max_age_s"]:
        state="stale"
    return dict(state=state,sample_ts=sampled,age_s=age,contract=last.get("contract"),source=last.get("source"),
                sample_quality=last.get("quality","missing"),pane_height=cfg["pane_height"],
                relative_window=cfg["relative_window"],freshness_max_age_s=cfg["freshness_max_age_s"],inference_only=True)


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
