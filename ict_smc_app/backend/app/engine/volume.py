"""
engine/volume.py — real volume (current-month NIFTY future, basis-adjusted to index prices) for every layer.

Volume says HOW MUCH traded, never WHO was aggressive (card 2: every buyer needs a seller). So everything here is
ACTIVITY. Direction still comes from price structure, or from delta when a delta source is connected.

  volume_profile   each minute's volume spread evenly over the 5-pt buckets between its low and high →
                   POC, value area (70 %), HVNs (acceptance — price slows there) and LVNs (rejection — price moves
                   fast through them), per IST day
  naked POCs       earlier days' POCs that price has NOT traded through since — magnets, like untaken liquidity
  vwap             session VWAP from 09:15 with volume-weighted σ bands (±1σ, ±2σ)
  anchored vwap    VWAP from any bar (day open, last MSS origin, the active CRT candle): the average price of
                   everyone who traded since that event — above it they are in profit, below it they are trapped
  rvol             TIME-OF-DAY relative volume: a bar's volume ÷ the average of the SAME clock slot over the previous
                   days (NIFTY volume is U-shaped; 09:15 is always busy, 13:00 always quiet). Falls back to the
                   previous 20 bars of the same timeframe. Only earlier data is used → no look-ahead.
  annotate         adds RVOL to order blocks (displacement candle), FVGs (middle candle), BOS / MSS, sweeps and
                   CRT candles. Display only — never changes a trigger, a zone, or a signal.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from .. import config
from ..timeutil import TF_SECONDS, bucket_start, chart_time, ist_date, session_bounds, to_ist

DEFAULTS = {"step_pts": 5.0, "value_area_pct": 0.70, "rvol_days": 10, "rvol_min_days": 2, "rvol_fallback_bars": 20,
            "rvol_high": 1.5, "rvol_low": 0.7, "min_coverage": 0.8, "hvn_ratio": 1.3, "lvn_ratio": 0.5,
            "max_nodes": 3, "naked_poc_days": 5}


def cfg() -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (config.rules().get("volume") or {}).items() if not k.startswith("_")})
    return out


# ── profile ──────────────────────────────────────────────────────────────────
def volume_profile(rows: List[Dict[str, float]], step: float, va_pct: float = 0.70) -> Optional[Dict[str, Any]]:
    rows = [r for r in rows if (r.get("v") or 0) > 0]
    if not rows:
        return None
    hist: Dict[int, float] = defaultdict(float)
    for r in rows:
        a, b = int(math.floor(r["l"] / step)), int(math.floor(r["h"] / step))
        share = float(r["v"]) / (b - a + 1)
        for k in range(a, b + 1):
            hist[k] += share
    ks = sorted(hist)
    lo_k, hi_k = ks[0], ks[-1]
    mid = (lo_k + hi_k) / 2
    poc = max(ks, key=lambda k: (hist[k], -abs(k - mid)))
    total = sum(hist.values())
    a = b = poc
    got = hist[poc]
    while got < va_pct * total and (a > lo_k or b < hi_k):
        up = hist.get(b + 1, 0.0) if b < hi_k else -1.0
        dn = hist.get(a - 1, 0.0) if a > lo_k else -1.0
        if up >= dn:
            b += 1
            got += max(up, 0.0)
        else:
            a -= 1
            got += max(dn, 0.0)
    hi, lo = max(r["h"] for r in rows), min(r["l"] for r in rows)
    C = cfg()
    full = [hist.get(k, 0.0) for k in range(lo_k, hi_k + 1)]
    sm = [sum(full[max(0, i - 1):i + 2]) / len(full[max(0, i - 1):i + 2]) for i in range(len(full))]
    mean = sum(full) / len(full)
    nb = lambda i: sm[i] if 0 <= i < len(sm) else 0.0          # noqa: E731 — outside the profile = no volume
    hvn = [i for i in range(len(sm)) if sm[i] >= nb(i - 1) and sm[i] >= nb(i + 1) and sm[i] >= C["hvn_ratio"] * mean]
    hvn = sorted(hvn, key=lambda i: -sm[i])[: int(C["max_nodes"])]
    lvn = [i for i in range(1, len(sm) - 1) if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] <= C["lvn_ratio"] * mean
           and any(h < i for h in hvn) and any(h > i for h in hvn)]
    lvn = [i for k, i in enumerate(lvn) if k == 0 or i - lvn[k - 1] > 1]     # one node per thin stretch
    lvn = sorted(lvn, key=lambda i: sm[i])[: int(C["max_nodes"])]
    px = lambda i: round((lo_k + i + 0.5) * step, 2)          # noqa: E731
    return {"kind": "volume", "poc": round(min(max((poc + 0.5) * step, lo), hi), 2),
            "vah": round(min((b + 1) * step, hi), 2), "val": round(max(a * step, lo), 2), "high": round(hi, 2), "low": round(lo, 2),
            "minutes": len(rows), "volume": round(total), "step": step,
            "hvn": sorted(px(i) for i in hvn), "lvn": sorted(px(i) for i in lvn),
            "hist": [[round(k * step, 2), round(hist[k])] for k in ks]}


def day_profiles(rows: List[Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    C = cfg()
    by: Dict[str, List[Dict[str, float]]] = defaultdict(list)
    for r in rows:
        by[ist_date(r["ts"]).isoformat()].append(r)
    return {d: p for d, rs in sorted(by.items()) if (p := volume_profile(rs, float(C["step_pts"]), float(C["value_area_pct"])))}


def naked_pocs(profiles: Dict[str, Dict[str, Any]], rows: List[Dict[str, float]], today: str) -> List[Dict[str, Any]]:
    """POCs of earlier days that no later minute has traded through (up to the last closed minute)."""
    out = []
    days = [d for d in sorted(profiles) if d < today][-int(cfg()["naked_poc_days"]):]
    for d in days:
        poc = profiles[d]["poc"]
        later = [r for r in rows if ist_date(r["ts"]).isoformat() > d]
        if not any(r["l"] <= poc <= r["h"] for r in later):
            out.append({"day": d, "price": poc})
    return out


# ── VWAP ─────────────────────────────────────────────────────────────────────
def vwap_series(rows: List[Dict[str, float]], from_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """Cumulative VWAP from from_ts (default: first row) with volume-weighted σ. One point per minute."""
    pv = vv = pv2 = 0.0
    out = []
    for r in rows:
        if from_ts is not None and r["ts"] < from_ts:
            continue
        v = float(r.get("v") or 0.0)
        tp = (r["h"] + r["l"] + r["c"]) / 3.0
        pv += tp * v
        vv += v
        pv2 += tp * tp * v
        if vv <= 0:
            continue
        w = pv / vv
        sd = math.sqrt(max(pv2 / vv - w * w, 0.0))
        out.append({"ts": int(r["ts"]), "vwap": round(w, 2), "sd": round(sd, 2)})
    return out


def session_vwap(rows: List[Dict[str, float]], day: str) -> List[Dict[str, Any]]:
    return vwap_series([r for r in rows if ist_date(r["ts"]).isoformat() == day])


# ── per-bar volume and RVOL ──────────────────────────────────────────────────
def bar_volume(rows: List[Dict[str, float]], tf: str) -> Dict[int, Tuple[float, int]]:
    """tf bucket start → (volume, minutes with data)."""
    out: Dict[int, List[float]] = {}
    for r in rows:
        b = bucket_start(r["ts"], tf)
        x = out.setdefault(b, [0.0, 0])
        x[0] += float(r.get("v") or 0.0)
        x[1] += 1
    return {k: (v[0], int(v[1])) for k, v in out.items()}


def classify_rvol(r: Optional[float]) -> Dict[str, Any]:
    """Semantic volume classification into institutional regimes."""
    if r is None:
        return {
            "tier": "UNKNOWN",
            "label": "Unknown",
            "color": "#64748b",
            "is_high_volume": False,
            "is_spike": False,
            "is_low_volume": False,
        }
    cfg_rule = config.rules().get("rvol", {})
    spike_thresh = float(cfg_rule.get("tier_earthquake_spike", 2.5))
    above_thresh = float(cfg_rule.get("tier_above_average", 1.5))
    normal_thresh = float(cfg_rule.get("tier_normal_min", 0.8))

    if r >= spike_thresh:
        return {
            "tier": "EARTHQUAKE_SPIKE",
            "label": "Earthquake Spike (≥2.5x)",
            "color": "#f59e0b",
            "neon_color": "#10b981",
            "is_high_volume": True,
            "is_spike": True,
            "is_low_volume": False,
        }
    elif r >= above_thresh:
        return {
            "tier": "ABOVE_AVERAGE",
            "label": "Above Average (≥1.5x)",
            "color": "#10b981",
            "is_high_volume": True,
            "is_spike": False,
            "is_low_volume": False,
        }
    elif r >= normal_thresh:
        return {
            "tier": "NORMAL_PARTICIPATION",
            "label": "Normal Flow (0.8x–1.5x)",
            "color": "#3b82f6",
            "is_high_volume": False,
            "is_spike": False,
            "is_low_volume": False,
        }
    else:
        return {
            "tier": "ASLEEP_LULL",
            "label": "Asleep / Lull (<0.8x)",
            "color": "#64748b",
            "is_high_volume": False,
            "is_spike": False,
            "is_low_volume": True,
        }


def rvol(rows: List[Dict[str, float]], tf: str) -> Dict[int, Dict[str, Any]]:
    """tf bucket start → {v, rvol, base, method, slot_rvol, slot_base, tier, ...}. Bars with < min_coverage of their minutes → rvol None."""
    C = cfg()
    bv = bar_volume(rows, tf)
    step_min = TF_SECONDS.get(tf, 22500) / 60
    slot = lambda ts: to_ist(ts).strftime("%H:%M")            # noqa: E731
    by_slot: Dict[str, List[Tuple[str, float]]] = defaultdict(list)   # slot → [(day, v)] in time order
    keys = sorted(bv)
    out: Dict[int, Dict[str, Any]] = {}
    hist: List[float] = []

    # Enforce minimum historical baseline floor to permanently prevent thin-slot denominator explosion (e.g. 35.9x)
    all_vols = [float(val[0]) for val in bv.values() if val[0] > 0]
    median_vol = float(np.median(all_vols)) if all_vols else 25000.0
    min_slot_base_ratio = float(C.get("min_slot_base_ratio", 0.35))
    min_slot_base_abs = float(C.get("min_slot_base_abs", 15000.0))
    if median_vol >= min_slot_base_abs:
        min_baseline = max(median_vol * min_slot_base_ratio, min_slot_base_abs)
    else:
        min_baseline = median_vol * min_slot_base_ratio
    max_slot_rvol = float(C.get("max_slot_rvol", 10.0))

    for k in keys:
        v, n = bv[k]
        _, close = session_bounds(ist_date(k))
        expect = max(1.0, min(step_min, (close - k) / 60))      # the session's last bucket is short
        full = n >= C["min_coverage"] * expect
        d = ist_date(k).isoformat()
        prev = [x for dd, x in by_slot[slot(k)] if dd < d][-int(C["rvol_days"]):]
        base, method = None, None
        min_slot_days = int(C.get("rvol_min_days", 2))
        if len(prev) >= min_slot_days:
            base, method = sum(prev) / len(prev), "time-of-day"
        elif len(prev) >= 1 and len(hist) >= 5:
            slot_avg = sum(prev) / len(prev)
            h = hist[-int(C["rvol_fallback_bars"]):]
            rolling_avg = sum(h) / len(h)
            weight = len(prev) / float(min_slot_days)
            base, method = (slot_avg * weight) + (rolling_avg * (1.0 - weight)), "blended"
        elif len(hist) >= 5:
            h = hist[-int(C["rvol_fallback_bars"]):]
            base, method = sum(h) / len(h), "rolling"
        elif len(prev) >= 1:
            base, method = sum(prev) / len(prev), "time-of-day"

        if base is not None:
            effective_base = max(base, min_baseline)
        elif hist:
            effective_base = min_baseline
        else:
            effective_base = None

        r = min(round(v / effective_base, 2), max_slot_rvol) if (full and effective_base and effective_base > 0) else None
        tier_info = classify_rvol(r)
        is_bo = True if (r is not None and r >= 1.8) else (False if (r is not None and r < 0.9) else None)
        bo_verdict = (
            "REAL_BREAKOUT_FUEL" if (r is not None and r >= 1.8)
            else ("FAKEOUT_WARNING" if (r is not None and r < 0.9) else "NEUTRAL_FLOW")
        )
        out[k] = {
            "v": round(v),
            "rvol": r,
            "slot_rvol": r,
            "base": round(effective_base) if effective_base else None,
            "slot_base": round(effective_base) if effective_base else None,
            "slot": slot(k),
            "method": method,
            "full": full,
            "tier": tier_info["tier"],
            "tier_label": tier_info["label"],
            "tier_color": tier_info["color"],
            "is_real_breakout": is_bo,
            "breakout_verdict": bo_verdict,
        }
        if full:
            by_slot[slot(k)].append((d, v))
            hist.append(v)
    return out


def tag(r: Optional[float]) -> str:
    if r is None:
        return ""
    C = cfg()
    return "high" if r >= C["rvol_high"] else "low" if r <= C["rvol_low"] else "normal"


# ── annotate the existing ICT / SMC layers (display only) ───────────────────
def annotate(j: Dict[str, Any], an, rv: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Adds rvol to zones / markers of to_json() output in place. Returns a summary for the narrative."""
    b = an.bars
    N = len(b)

    def at(i: Optional[int]) -> Optional[float]:
        if i is None or not (0 <= i < N):
            return None
        x = rv.get(int(b.ts[i]))
        return x["rvol"] if x else None

    def sfx(r: Optional[float]) -> str:
        return f" · vol {r:.1f}×" if r is not None else ""

    obs = {(round(o.top, 2), round(o.bottom, 2)): o for o in an.obs}
    fvs = {(round(g.top, 2), round(g.bottom, 2)): g for g in an.fvgs}
    for z in j.get("zones", []):
        key = (round(z["top"], 2), round(z["bottom"], 2))
        if z["type"] == "FVG" and key in fvs:
            r = at(fvs[key].i - 1)                            # the displacement (middle) candle
            z["rvol"], z["vol_note"] = r, "gap candle"
        elif key in obs:
            o = obs[key]
            r = at(o.created_i)                               # the candle that broke structure from the OB
            z["rvol"], z["vol_note"] = r, "displacement"
            z["ob_rvol"] = at(o.i)
        else:
            continue
        z["vol_tag"] = tag(z["rvol"])
        z["label"] += sfx(z["rvol"])
    ev_by_t = {chart_time(int(b.ts[e.i])): e for e in an.events}
    lq_by_t = {chart_time(int(b.ts[x.i])): x for x in an.liq}
    for m in j.get("markers", []):
        e = ev_by_t.get(m["time"]) if m["kind"] in ("BOS", "MSS") else lq_by_t.get(m["time"]) if m["kind"] in ("SWEEP", "FALSE_BREAKOUT") else None
        if e is None:
            continue
        r = at(e.i)
        m["rvol"] = r
        if r is not None and tag(r) == "high":
            m["text"] += f" V{r:.1f}"
        elif r is not None and tag(r) == "low" and m["kind"] in ("BOS", "MSS"):
            m["text"] += " lowV"
    last = rv.get(int(b.ts[-1]))
    return {"last_rvol": last.get("rvol") if last else None, "last_v": last.get("v") if last else None,
            "method": last.get("method") if last else None}
