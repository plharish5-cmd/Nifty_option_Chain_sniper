"""Causal, alert-only price/CVD divergence with independent structure and VWAP gates.

Live inputs use the selected NIFTY future and its estimated quote-rule delta.
Missing delta never becomes candle-derived flow. Custom series are a replay API:
callers supply closed bars, explicit delta/CVD, and the same instrument throughout.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional
import math

from .. import config, db
from ..delta import adapters
from ..market import futures as fut_mod
from ..timeutil import chart_time, hm, now_epoch, now_ist, session_bounds

TERMINAL = {"EXPIRED", "INVALIDATED"}


def get_cfg() -> Dict[str, Any]:
    return dict(config.rules()["cvd_divergence"])


def _empty(tf, message, quality=None):
    return dict(ok=False, message=message, timeframe=tf, current_price=0,
                current_cvd=0, current_vwap=0, vwap_diff_pts=0,
                cvd_slope_status="DATA UNAVAILABLE", cvd_slope_delta=0,
                active_divergence=None, summary_card=None, recent_divergences=[],
                transitions=[], bars=[], total_bars=0, data_quality=quality or {})


def _custom(prices, cvds, vwaps, step, as_of):
    cm = {int(x.get("ts", x.get("time", 0))): x for x in (cvds or [])}
    vm = {int(x.get("ts", x.get("time", 0))): x for x in (vwaps or [])}
    bars, running, pv, volume = [], 0.0, 0.0, 0.0
    previous = None
    for p in sorted(prices, key=lambda x: int(x.get("ts", x.get("time", 0)))):
        ts = int(p.get("ts", p.get("time", 0)))
        if p.get("closed") is False or ts + step > as_of:
            continue
        if previous is not None and ts - previous != step:
            raise ValueError("Custom bars must be unique and contiguous")
        previous = ts
        b = {k: float(p.get(long, p.get(k, 0))) for k, long in
             (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))}
        v = float(p.get("volume", p.get("vol", p.get("v", 0))))
        flow = cm.get(ts, p)
        if "cvd" in flow:
            new = float(flow["cvd"])
            delta, running = new - running, new
        elif "delta" in flow:
            delta = float(flow["delta"])
            running += delta
        else:
            raise ValueError("Explicit delta/CVD is required for every closed bar")
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        volume += v
        supplied = vm.get(ts, p).get("vwap")
        if supplied is None and volume <= 0:
            raise ValueError("Positive volume or explicit VWAP is required")
        vw = float(supplied) if supplied is not None else pv / volume
        values = [*b.values(), v, delta, running, vw]
        if not all(math.isfinite(x) for x in values) or v < 0 or vw <= 0:
            raise ValueError("Non-finite or invalid market data")
        bars.append(dict(b, ts=ts, time=chart_time(ts), time_str=hm(ts),
                         vol=v, delta=delta, cvd=running, vwap=vw))
    return bars


def _live(tf, step, c, as_of):
    start, end = map(int, session_bounds(now_ist().date()))
    sid = db.kv_get(f"fut_sid_{fut_mod.SEC}")
    quality = dict(source=c["delta_source"], estimated=True, units="reported futures volume",
                   instrument=fut_mod.SEC, contract_id=sid, coverage=0.0)
    if c["delta_source"] != "fut_quote_rule":
        raise ValueError("Live CVD requires fut_quote_rule; other instruments need an explicit binding")
    rows = db.query("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<? ORDER BY ts",
                    (fut_mod.SEC, start, min(end, as_of)))
    if not rows or not sid:
        return [], dict(quality, reason="Selected futures contract or session candles unavailable")
    # FUTQR currently has no per-contract key. Refuse a rollover session rather than mix flow.
    binding_key = f"cvd_contract:{start}"
    binding = db.kv_get(binding_key)
    if binding is not None and str(binding) != str(sid):
        return [], dict(quality, reason="Contract changed during session; evaluation suspended")
    db.kv_set(binding_key, str(sid))
    adapter = adapters.make(c["delta_source"])
    adapter.prepare("1m", start, min(end, as_of))
    grouped = {}
    pv = volume = running = 0.0
    bars = []
    missing_delta = []
    segment = start
    price_minutes = 0
    last_price_ts = None
    for row in rows:
        r = dict(row)
        ts = int(r["ts"])
        if ts + 60 > as_of:
            continue
        if ts != start + price_minutes * 60:
            return [], dict(quality, reason="Missing session price minutes")
        price_minutes += 1
        last_price_ts = ts
        delta = adapter.get_delta(r, "1m")
        v = float(r["v"])
        if v < 0 or not all(math.isfinite(float(r[k])) for k in ("o", "h", "l", "c", "v")) or (delta is not None and not math.isfinite(delta)):
            return [], dict(quality, reason="Invalid futures market data")
        pv += (r["h"] + r["l"] + r["c"]) / 3 * v
        volume += v
        if delta is None:
            missing_delta.append(ts)
            running = 0.0
            segment = ts + 60
            continue
        running += delta
        grouped[ts] = dict(r, delta=delta, cvd=running, segment=segment, vwap=pv / volume if volume else 0)
    for ts in range(start, min(end, as_of) - step + 1, step):
        sub = [grouped.get(t) for t in range(ts, ts + step, 60)]
        if any(x is None for x in sub) or len({x["segment"] for x in sub}) != 1:
            continue
        bars.append(dict(ts=ts, time=chart_time(ts), time_str=hm(ts), o=sub[0]["o"],
                         h=max(x["h"] for x in sub), l=min(x["l"] for x in sub),
                         c=sub[-1]["c"], vol=sum(x["v"] for x in sub),
                         delta=sum(x["delta"] for x in sub), cvd=sub[-1]["cvd"], vwap=sub[-1]["vwap"], segment=sub[-1]["segment"]))
    latest = max(grouped, default=start) + 60
    age = max(0, as_of - latest)
    latest_segment_bars = [b for b in bars if b["segment"] == segment]
    quality.update(coverage=len(grouped)/price_minutes if price_minutes else 0.0, age_s=age,
                   missing_delta_minutes=[hm(t) for t in missing_delta],
                   cvd_anchor=hm(bars[-1]["segment"]) if bars else None,
                   cvd_anchor_ts=bars[-1]["segment"] if bars else None,
                   provenance_verified=False, price_minutes=price_minutes,
                   delta_minutes=len(grouped), segment_bars=len(latest_segment_bars))
    quality["alerts_enabled"] = (as_of < end and age <= c["max_data_age_s"]
                                and last_price_ts in grouped and bool(latest_segment_bars))
    if not quality["alerts_enabled"]:
        quality["reason"] = ("Recorded session data — market closed" if as_of >= end else
                             "Delta gap — waiting for a complete new segment" if last_price_ts not in grouped or not latest_segment_bars else
                             "Recorded data — futures feed stale; alerts suspended")
    elif missing_delta:
        quality["reason"] = f"Partial session delta; CVD re-anchored at {quality['cvd_anchor']}. No comparisons across gaps."
    if not bars or bars[-1]["vwap"] <= 0:
        return [], dict(quality, reason="Insufficient complete bars or volume")
    return bars, quality


def _evaluate(bars, tf, c, identity):
    """Replay forward. Pivots become knowable only after all right bars close."""
    half = max(1, int(c["swing_window"]) // 2)
    pivots = {"short": [], "long": []}
    events, transitions = [], []

    def transition(e, state, i):
        e.update(state=state, updated_ts=bars[i]["ts"], time_str=bars[i]["time_str"])
        e["name"] = f"{e['type'].replace('_', ' ')} — {state}"
        transitions.append(dict(e, key=f"{e['setup_id']}:{state}", ts=bars[i]["ts"] + (180 if tf == "3m" else 300)))

    for i, bar in enumerate(bars):
        j = i - half
        if j >= half:
            window = bars[j-half:j+half+1]
            p = bars[j]
            for direction, field in (("short", "h"), ("long", "l")):
                values = [x[field] for x in window]
                extreme = max(values) if direction == "short" else min(values)
                if p[field] != extreme or values.count(extreme) != 1:
                    continue
                bar_key = "is_swing_high" if direction == "short" else "is_swing_low"
                # Mark on the confirmation bar, never retroactively on the pivot bar.
                bar[bar_key] = True
                candidates = pivots[direction]
                sign = -1 if direction == "short" else 1
                for prev in reversed(candidates):
                    gap = j - prev
                    if gap > c["max_pivot_gap_bars"]:
                        break
                    if gap < c["min_pivot_gap_bars"]:
                        continue
                    sample = bars[max(0, j-c["normalization_bars"]+1):j+1]
                    price_floor = max(c["min_price_diff_pts"], median(x["h"]-x["l"] for x in sample) * c["price_range_mult"])
                    flow_floor = max(c["min_cvd_diff"], median(abs(x["delta"]) for x in sample) * c["delta_scale_mult"])
                    if sign * (p[field]-bars[prev][field]) > -price_floor or sign * (p["cvd"]-bars[prev]["cvd"]) < flow_floor:
                        continue
                    between = bars[prev+1:j]
                    trigger = min(x["l"] for x in between) if direction == "short" else max(x["h"] for x in between)
                    invalidation = p[field] - sign * c["sl_buffer_pts"]
                    setup_id = f"CVD:{identity}:{tf}:{direction}:{bars[prev]['ts']}:{p['ts']}"
                    e = dict(setup_id=setup_id, key=setup_id, type="BEARISH_DIVERGENCE" if direction == "short" else "BULLISH_DIVERGENCE",
                             dir=direction, timeframe=tf, bar_index=i, ts=bar["ts"], pivot_ts=p["ts"], confirmed_at=bar["ts"] + (180 if tf == "3m" else 300),
                             headline="Price and estimated flow disagree; reversal remains unconfirmed.",
                             action=f"Wait for close {'below' if sign < 0 else 'above'} {trigger:.1f}",
                             structure_trigger=trigger, invalidation=invalidation, expires_ts=bar["ts"]+c["expiry_bars"]*(180 if tf == "3m" else 300),
                             structure_confirmed=False, vwap_confirmed=False, entry=None, sl=None, tp=None, risk_pts=None, rr=None,
                             vwap_status="Awaiting confirmation", price_swing=dict(prev=bars[prev][field], curr=p[field], diff=p[field]-bars[prev][field]),
                             cvd_swing=dict(prev=bars[prev]["cvd"], curr=p["cvd"], diff=p["cvd"]-bars[prev]["cvd"]))
                    events.append(e)
                    transition(e, "WATCH", i)
                    break
                candidates.append(j)
        for e in events:
            if e["state"] in TERMINAL:
                continue
            sign = -1 if e["dir"] == "short" else 1
            # Invalidation wins over any confirmation on the same candle.
            adverse = bar["h"] if sign < 0 else bar["l"]
            if sign * (adverse - e["invalidation"]) <= 0:
                transition(e, "INVALIDATED", i)
                continue
            if i - e["bar_index"] >= c["expiry_bars"]:
                transition(e, "EXPIRED", i)
                continue
            e["vwap_status"] = "Below VWAP" if bar["c"] < bar["vwap"] else "Above VWAP"
            if i <= e["bar_index"]:
                continue
            structure = sign * (bar["c"]-e["structure_trigger"]) > c["structure_buffer_pts"]
            vwap = sign * (bar["c"]-bar["vwap"]) > c["vwap_buffer_pts"]
            e["vwap_confirmed"] = vwap
            if structure and not e["structure_confirmed"]:
                e["structure_confirmed"] = True
                e["action"] = "Structure confirmed; wait for VWAP confluence"
                transition(e, "STRUCTURE_CONFIRMED", i)
            if e["structure_confirmed"] and structure and vwap and e["state"] != "VWAP_CONFIRMED":
                entry = bar["c"]
                risk = sign * (entry-e["invalidation"])
                if risk > 0:
                    e.update(entry=entry, sl=e["invalidation"], risk_pts=risk,
                             tp=entry+sign*risk*c["target_rr"], rr=c["target_rr"],
                             action="Confirmed alert — hypothetical futures levels; not an order")
                    transition(e, "VWAP_CONFIRMED", i)
    return events, transitions


def compute_cvd_divergence(price_series: Optional[List[Dict[str, Any]]] = None,
                           cvd_series=None, vwap_series=None, tf="3m", cfg=None, as_of=None):
    c = dict(get_cfg(), **(cfg or {}))
    tf = tf or c["timeframe"]
    if tf not in ("3m", "5m"):
        return _empty(tf, "Unsupported timeframe; choose 3m or 5m")
    if not c["enabled"]:
        return _empty(tf, "CVD divergence disabled")
    step = 180 if tf == "3m" else 300
    as_of = int(now_epoch() if as_of is None else as_of)
    quality = dict(source="custom", estimated=True, units="caller supplied", instrument="custom")
    try:
        if price_series is not None:
            bars = _custom(price_series, cvd_series, vwap_series, step, as_of)
        else:
            bars, quality = _live(tf, step, c, as_of)
    except ValueError as exc:
        return _empty(tf, str(exc), quality)
    if not bars:
        return _empty(tf, quality.get("reason", "No complete bars available"), quality)
    identity = f"{quality.get('contract_id', 'custom')}:{bars[0]['ts']}:{quality['source']}"
    events, transitions = [], []
    segments = []
    for bar in bars:
        if not segments or segments[-1][0].get("segment") != bar.get("segment"):
            segments.append([])
        segments[-1].append(bar)
    for idx, part in enumerate(segments):
        ev, tr = _evaluate(part, tf, c, identity)
        if idx < len(segments)-1:
            for event in ev:
                if event["state"] not in TERMINAL:
                    event.update(state="INVALIDATED", action="Cancelled by a data gap", name=event["type"].replace("_", " ")+" — DATA GAP")
        events.extend(ev)
        if idx == len(segments)-1:
            transitions.extend(tr)
    active = next((e for e in reversed(events) if e["state"] not in TERMINAL), None)
    alerts_enabled = quality.get("alerts_enabled", True)
    if not alerts_enabled:
        active = None
        transitions = []
    last = bars[-1]
    current_segment = segments[-1]
    lookback = min(len(current_segment), c["slope_bars"]+1)
    slope = last["cvd"]-current_segment[-lookback]["cvd"]
    status = "CVD RISING" if slope > c["slope_min_delta"] else "CVD FALLING" if slope < -c["slope_min_delta"] else "CVD BALANCED"
    return dict(ok=True, alerts_enabled=alerts_enabled, message=quality.get("reason", "Data ready"), timeframe=tf, current_price=last["c"], current_cvd=last["cvd"],
                current_vwap=last["vwap"], vwap_diff_pts=round(last["c"]-last["vwap"], 1), cvd_slope_status=status,
                cvd_slope_delta=slope, active_divergence=active, summary_card=active,
                recent_divergences=events[-10:][::-1], transitions=transitions,
                bars=bars[-60:], total_bars=len(bars), data_quality=quality)
