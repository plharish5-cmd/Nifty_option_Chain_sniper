"""
delta/engine.py — where aggression hits, and whether it is absorbed. CLOSED candles only, no look-ahead.

Reading (fixed):  delta > 0 → buyers attacking  ·  delta < 0 → sellers attacking.
Events go into the same log as BOS / MSS / sweeps (shape: id, event_type, ts, time, tf, zone_id, direction,
confidence, explanation). `direction` is what the event implies for PRICE:
    aggression up → bull · absorption of buyers → bear · flip to sellers → bear …

PSEUDOCODE — compute(an, zones, adapter, now)
    for each bar i of the timeframe (oldest → newest):
        if bar i has not CLOSED by `now`: stop                      # hard guard, even if a caller passes it
        d[i] = adapter.get_delta(bar i)  ;  levels[i] = adapter.get_footprint_levels(bar i)
        base = d[i-W … i-1] (non-null, ≥ min bars)                  # current bar never in its own baseline
        z[i] = (d[i] − mean(base)) / std(base)        (or percentile rank of |d|)
        extreme = |z| ≥ aggression_z  and  |d| ≥ aggression_min_abs
        hit = active zones (registry) that bar i's range overlaps (± tolerance)
        if extreme:                 emit DELTA_AGGRESSION (dir = sign d)
        RULE 1 (high delta, low progress):
            if extreme and hit and (body/range ≤ max_body_ratio  or  close far from the extreme d pushed toward):
                emit DELTA_ABSORPTION @ i   (dir = −sign d)
        RULE 2 (failure to continue), evaluated on bar j = i+1 once it has closed:
            if bar i ENTERED a zone (overlaps it, bar i−1 did not) with extreme delta
               and close[j] fails to extend close[i] by max(pts, k×ATR) in the delta's direction:
                emit DELTA_ABSORPTION @ j   (dir = −sign d[i])
        FLIP: sign(d[i−1]) ≠ sign(d[i]), both |z| ≥ flip.min_z, at a zone or ≤ max_bars after a sweep → DELTA_FLIP
        IMBALANCE: with levels → stacked ratio imbalances touching a zone boundary → FOOTPRINT_IMBALANCE
                   without levels → delta spike + long wick in the attack direction at a zone → proxy (labelled)
    unavailable adapter → no events, status says "Delta unavailable"
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .. import config, db
from ..engine.registry import Zone, at_price, liq_event_id, struct_event_id
from ..timeutil import TF_SECONDS, chart_time, hm, now_epoch
from .adapters import DeltaAdapter, Level

KINDS = ("DELTA_AGGRESSION", "DELTA_ABSORPTION", "DELTA_FLIP", "FOOTPRINT_IMBALANCE")


def _f(x: float) -> str:
    return f"{x:+,.0f}"


def _baseline(vals: List[Optional[float]], i: int, W: int, min_n: int) -> Optional[List[float]]:
    base = [v for v in vals[max(0, i - W):i] if v is not None]
    return base if len(base) >= min_n else None


def _score(d: float, base: List[float], method: str) -> float:
    """z-score, or for 'percentile' a signed pseudo-z so the same thresholds plumbing works."""
    if method == "percentile":
        a = abs(d)
        rank = sum(1 for v in base if abs(v) <= a) / len(base) * 100
        return math.copysign(rank, d)
    m = sum(base) / len(base)
    sd = math.sqrt(sum((v - m) ** 2 for v in base) / len(base))
    if sd <= 1e-9:
        return 0.0
    return (d - m) / sd


def mark_imbalances(levels: List[Level], ratio: float, mode: str, min_vol: float) -> List[Level]:
    """Footprint imbalance. diagonal: ask (buy) at p vs bid (sell) one level BELOW; horizontal: same price."""
    lv = sorted(levels, key=lambda x: x.price)
    for k, L in enumerate(lv):
        L.imb_buy = L.imb_sell = False
        if mode == "diagonal":
            below = lv[k - 1] if k > 0 else None
            above = lv[k + 1] if k + 1 < len(lv) else None
            if below is not None and L.buy_vol >= min_vol and L.buy_vol >= ratio * max(below.sell_vol, 1e-9):
                L.imb_buy = True
            if above is not None and L.sell_vol >= min_vol and L.sell_vol >= ratio * max(above.buy_vol, 1e-9):
                L.imb_sell = True
        else:
            if L.buy_vol >= min_vol and L.buy_vol >= ratio * max(L.sell_vol, 1e-9):
                L.imb_buy = True
            if L.sell_vol >= min_vol and L.sell_vol >= ratio * max(L.buy_vol, 1e-9):
                L.imb_sell = True
    return lv


def stacked(levels: List[Level], side: str, n_min: int) -> List[List[Level]]:
    runs, cur = [], []
    for L in levels:
        if (L.imb_buy if side == "buy" else L.imb_sell):
            cur.append(L)
        else:
            if len(cur) >= n_min:
                runs.append(cur)
            cur = []
    if len(cur) >= n_min:
        runs.append(cur)
    return runs


def _zone_txt(z: Optional[Zone]) -> str:
    if z is None:
        return ""
    nm = {"ORDER_BLOCK": f"{z.side.lower()} {'breaker' if z.extra.get('kind') == 'BREAKER' else 'OB'}",
          "FVG": f"{'bearish' if z.side == 'SUPPLY' else 'bullish'} FVG", "LIQ_POOL": f"{z.side} {z.extra.get('src', '')}"}[z.type]
    rng = f"{z.price_low:,.1f}" if z.is_level else f"{z.price_low:,.1f}–{z.price_high:,.1f}"
    return f"{nm} {rng}"


def _conf(z_abs: float, thr: float, zone: Optional[Zone], cap: float = 0.95) -> float:
    c = 0.45 + 0.12 * max(0.0, z_abs - thr)
    if zone is not None:
        c += 0.15 if zone.type in ("ORDER_BLOCK", "FVG") else 0.08
    return round(min(cap, max(0.05, c)), 2)


def _atr_list(rows: List[Optional[Dict[str, float]]], n: int = 14) -> List[float]:
    """Wilder ATR over a (possibly gappy) candle list, past-only. Gaps carry the last value."""
    out, prev_c, a, k = [], None, None, 0
    for r in rows:
        if r is None:
            out.append(a if a is not None else 0.0)
            continue
        tr = r["h"] - r["l"] if prev_c is None else max(r["h"] - r["l"], abs(r["h"] - prev_c), abs(r["l"] - prev_c))
        k += 1
        a = tr if a is None else (a + (tr - a) / min(k, n))
        prev_c = r["c"]
        out.append(a)
    return out


def compute(an, zones: List[Zone], adapter: DeltaAdapter, now: Optional[float] = None,
            flow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """flow = None → delta of the NIFTY index candles themselves.
    flow = {"instrument": Instrument (OPTION), "candles": {index bar ts: option candle at the same tf}} →
           delta + progress are read on the SELECTED STRIKE's candle, zone contact on the INDEX candle,
           and the implied NIFTY direction is mapped (buyers attacking a PE = bearish for NIFTY)."""
    D = config.rules()["delta"]
    Z = config.rules().get("zones", {})
    A = D["absorption"]
    tf, b = an.tf, an.bars
    N, step = len(b), TF_SECONDS[an.tf]
    now = now_epoch() if now is None else now
    st = adapter.status()
    res: Dict[str, Any] = {"tf": tf, "adapter": st["name"], "available": False, "mock": bool(st.get("mock")),
                           "message": st.get("message", ""), "bars": [], "events": [], "has_levels": False}
    if tf not in D.get("timeframes", ["5m"]) or N == 0:
        res["message"] = f"delta not computed on {tf}"
        return res
    closed_n = 0
    for i in range(N):                                        # hard closed-candle guard
        if int(b.ts[i]) + step > now:
            break
        closed_n = i + 1
    inst = flow["instrument"] if flow else None
    fc = flow["candles"] if flow else {}
    res["instrument"] = inst.key if inst else "NIFTY"
    res["instrument_label"] = inst.label if inst else "NIFTY 50"
    xi = (inst,) if inst else ()                               # old 2-arg adapters keep working in index mode
    adapter.prepare(tf, int(b.ts[0]), int(b.ts[max(closed_n - 1, 0)]), *xi)

    def cand(i: int) -> Optional[Dict[str, float]]:
        ts = int(b.ts[i])
        if flow:
            x = fc.get(ts)
            return None if x is None else {"ts": ts, "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]),
                                           "c": float(x["c"])}
        return {"ts": ts, "o": float(b.o[i]), "h": float(b.h[i]), "l": float(b.l[i]), "c": float(b.c[i])}

    cands = [cand(i) for i in range(closed_n)]
    patr = _atr_list(cands) if flow else [float(an.atr[i]) for i in range(closed_n)]
    put = bool(inst and inst.right == "PE")
    imp = lambda up: ("bear" if up else "bull") if put else ("bull" if up else "bear")   # noqa: E731  NIFTY direction
    opp = lambda dd: "bear" if dd == "bull" else "bull"                                  # noqa: E731
    on = f" in {inst.label}" if inst else ""
    d: List[Optional[float]] = [adapter.get_delta(cands[i], tf, *xi) if cands[i] else None for i in range(closed_n)]
    if not any(v is not None for v in d):
        res["message"] = st.get("message") if not st.get("available") else f"Delta unavailable for these {tf} candles"
        return res
    res["available"] = True
    method = D.get("baseline_method", "zscore")
    thr = float(D["aggression_percentile"] if method == "percentile" else D["aggression_z"])
    min_abs = float(D.get("aggression_min_abs", 0))
    flip_thr = float(D["flip"]["min_z"]) if method == "zscore" else float(D["aggression_percentile"]) * 0.6
    W, min_n = int(D["baseline_window"]), int(D["baseline_min_bars"])
    FP, PX = D["footprint"], D["proxy"]
    sweeps = [x for x in an.liq if x.kind in ("SWEEP", "FALSE_BREAKOUT")]

    zsc: List[Optional[float]] = [None] * closed_n
    for i in range(closed_n):
        if d[i] is None:
            continue
        base = _baseline(d, i, W, min_n)
        if base is not None:
            zsc[i] = round(_score(d[i], base, method), 2)

    events: List[Dict[str, Any]] = []

    def emit(kind: str, i: int, direction: str, zone: Optional[Zone], conf: float, text: str, rule: str,
             price: float, extra: Optional[Dict[str, Any]] = None) -> None:
        ts = int(b.ts[i])
        ev = {"id": f"{kind}|{tf}|{ts}|{rule}|{zone.id if zone else '-'}|{st['name']}" + (f"|{inst.key}" if inst else ""),
              "event_type": kind, "instrument": inst.key if inst else "NIFTY",
              "ts": ts, "time": hm(ts), "chart_time": chart_time(ts), "tf": tf, "zone_id": zone.id if zone else None,
              "zone": _zone_txt(zone) or None, "zone_type": zone.type if zone else None,
              "zone_side": zone.side if zone else None, "direction": direction, "confidence": conf,
              "explanation": text, "rule": rule, "price": round(price, 2), "delta": d[i], "z": zsc[i],
              "mock": res["mock"], "source": st["name"]}
        if extra:
            ev.update(extra)
        events.append(ev)

    def tol(i: int) -> float:
        return max(float(Z.get("touch_tolerance_pts", 1.0)), float(Z.get("touch_tolerance_atr", 0.05)) * float(an.atr[i]))

    def hits(i: int) -> List[Zone]:
        return at_price(zones, int(b.ts[i]), float(b.l[i]), float(b.h[i]), tol(i))

    def extreme(i: int) -> bool:
        return d[i] is not None and zsc[i] is not None and abs(zsc[i]) >= thr and abs(d[i]) >= min_abs

    for i in range(closed_n):
        lv = adapter.get_footprint_levels(cands[i], tf, *xi) if cands[i] else None
        if lv:
            res["has_levels"] = True
        res["bars"].append({"time": chart_time(int(b.ts[i])), "ts": int(b.ts[i]), "delta": d[i], "z": zsc[i],
                            "levels": bool(lv)})
        if d[i] is None or zsc[i] is None:
            continue
        cc = cands[i]
        o, h, l, c = cc["o"], cc["h"], cc["l"], cc["c"]               # progress candle: index, or the strike in flow mode
        if h <= 0 or h - l <= 0 or h - l < float(D.get("min_candle_range_atr", 0.15)) * float(patr[i]):
            continue                    # flat / stalled candle: its delta proves nothing about progress
        rng = max(h - l, 1e-9)
        up = d[i] > 0
        who = "Buyers" if up else "Sellers"
        hz = hits(i)
        z0 = hz[0] if hz else None
        za = abs(zsc[i])
        zs = f"z {zsc[i]:+.1f}" if method == "zscore" else f"{za:.0f}th pct"

        at_z = (f" while NIFTY was at {_zone_txt(z0)}" if inst else f" at {_zone_txt(z0)}") if z0 else ""
        if extreme(i):
            emit("DELTA_AGGRESSION", i, imp(up), z0, _conf(za, thr, z0, 0.8),
                 f"{who} attacking{on}: delta {_f(d[i])} ({zs} vs last {W} candles){at_z}.", "aggression",
                 float(b.h[i]) if imp(up) == "bull" else float(b.l[i]))

            # RULE 1 — high delta, low progress, at a zone
            body_r = abs(c - o) / rng
            off = ((h - c) if up else (c - l)) / rng
            low_prog = body_r <= float(A["rule1_max_body_ratio"]) or off >= float(A["rule1_min_close_off_extreme"])
            if z0 is not None and low_prog:
                why = (f"body only {body_r:.0%} of range" if body_r <= float(A["rule1_max_body_ratio"])
                       else f"closed {off:.0%} of the range away from the {'high' if up else 'low'}")
                if inst:
                    txt = (f"{who} attacked{on} (delta {_f(d[i])}) while NIFTY was at {_zone_txt(z0)}, but the premium "
                           f"didn't {'rise' if up else 'fall'} ({why}) → absorbed → {opp(imp(up))}ish for NIFTY.")
                else:
                    txt = (f"{who} attacked inside {_zone_txt(z0)} (delta {_f(d[i])}) but price didn't "
                           f"{'move up' if up else 'move down'} ({why}) → absorbed by {'sellers' if up else 'buyers'}.")
                emit("DELTA_ABSORPTION", i, opp(imp(up)), z0, _conf(za, thr, z0), txt, "rule1_low_progress",
                     float(b.h[i]) if imp(up) == "bull" else float(b.l[i]),
                     {"body_ratio": round(body_r, 2), "close_off": round(off, 2)})

        # RULE 2 — failure to continue: bar k = i-1 entered a zone with extreme delta; bar i is the next close
        k = i - 1
        if k >= 0 and extreme(k):
            hk = hits(k)
            prev = set(z.id for z in hits(k - 1)) if k >= 1 else set()
            entered = [z for z in hk if z.id not in prev]
            if entered:
                zk = entered[0]
                upk = d[k] > 0
                need = max(float(A["rule2_min_follow_pts"]), float(A["rule2_min_follow_atr"]) * float(patr[k]))
                ck = cands[k]["c"]
                ext = (c - ck) if upk else (ck - c)
                if ext < need:
                    emit("DELTA_ABSORPTION", i, opp(imp(upk)), zk, _conf(abs(zsc[k]), thr, zk),
                         f"{'Buyers' if upk else 'Sellers'}{on} hit {_zone_txt(zk)} at {hm(int(b.ts[k]))} (delta {_f(d[k])}), "
                         f"next candle {'closed back / ' if ext < 0 else ''}no follow-through ({ext:+.1f} pts < {need:.1f}) "
                         f"→ aggression absorbed" + (f" → {opp(imp(upk))}ish for NIFTY." if inst else "."),
                         "rule2_no_follow_through", float(b.c[k]),
                         {"entry_ts": int(b.ts[k]), "follow_pts": round(ext, 2), "delta": d[k], "z": zsc[k],
                          "next_delta": d[i]})

        # FLIP — sign change at a zone, or right after a sweep
        if i >= 1 and d[i - 1] is not None and zsc[i - 1] is not None and (d[i - 1] > 0) != up \
                and abs(zsc[i]) >= flip_thr and abs(zsc[i - 1]) >= flip_thr:
            sw = next((x for x in reversed(sweeps) if 0 <= i - x.i <= int(D["flip"]["max_bars"])), None)
            zf = z0 or (hits(i - 1)[0] if hits(i - 1) else None)
            if zf is not None or sw is not None:
                where = _zone_txt(zf) if zf else f"the {sw.src} {sw.side} sweep {sw.level:,.1f}"
                emit("DELTA_FLIP", i, imp(up), zf, _conf(za, flip_thr, zf, 0.85),
                     f"Delta{on} flipped {'sellers → buyers' if up else 'buyers → sellers'} ({_f(d[i - 1])} → {_f(d[i])}) "
                     f"at {where}" + (f" → {imp(up)}ish for NIFTY." if inst else "."),
                     "flip", float(b.c[i]), {"sweep_id": liq_event_id(tf, sw.kind, sw.side, sw.ts, sw.level) if sw else None})

        # IMBALANCE — real footprint if levels exist, else a labelled proxy
        if hz:
            if lv:
                lv2 = mark_imbalances(lv, float(FP["imbalance_ratio"]), FP.get("imbalance_mode", "diagonal"),
                                      float(FP.get("min_level_volume", 1)))
                btol = float(FP["boundary_tolerance_pts"])
                for side in ("buy", "sell"):
                    for run in stacked(lv2, side, int(FP["stacked_min"])):
                        p_lo, p_hi = run[0].price, run[-1].price
                        # index mode: the stacked levels must touch the zone edge; strike mode: option prices are a
                        # different scale, so contact = the INDEX candle being at the zone
                        zz = z0 if inst else next((z for z in hz if p_lo <= z.price_high + btol
                                                   and p_hi >= z.price_low - btol), None)
                        if zz is None:
                            continue
                        emit("FOOTPRINT_IMBALANCE", i, imp(side == "buy"), zz,
                             _conf(za, 0, zz, 0.85) if extreme(i) else 0.5,
                             f"{len(run)} stacked {'buy' if side == 'buy' else 'sell'} imbalances{on} "
                             f"{p_lo:,.1f}–{p_hi:,.1f} at {_zone_txt(zz)}: aggression hit here.",
                             f"stacked_{side}", float(b.c[i]) if inst else (p_lo + p_hi) / 2,
                             {"proxy": False, "levels_n": len(run)})
            elif PX.get("enabled", True) and abs(zsc[i]) >= float(PX["min_z"] if method == "zscore" else thr):
                wick = (h - max(o, c)) if up else (min(o, c) - l)
                if wick / rng >= float(PX["min_wick_ratio"]):
                    emit("FOOTPRINT_IMBALANCE", i, imp(up), z0, min(0.6, _conf(za, thr, z0)),
                         f"Proxy imbalance (no footprint levels): {who.lower()}{on} spiked delta {_f(d[i])} and "
                         f"left a {wick / rng:.0%} wick at {_zone_txt(z0)} — where aggression hit.",
                         "proxy_wick", float(b.h[i]) if imp(up) == "bull" else float(b.l[i]), {"proxy": True})
    res["events"] = events
    res["message"] = st.get("message", "")
    return res


def structure_events(an) -> List[Dict[str, Any]]:
    """BOS/MSS/sweeps in the same event shape, so the inspector shows one timeline."""
    out = []
    for e in an.events:
        out.append({"id": struct_event_id(an.tf, e.ts, e.dir), "event_type": "MSS" if e.is_mss else "BOS",
                    "ts": e.ts, "time": hm(e.ts), "chart_time": chart_time(e.ts), "tf": an.tf, "zone_id": None,
                    "direction": e.dir, "confidence": None, "price": e.level,
                    "explanation": f"close {e.close:,.1f} beyond swing {e.level:,.1f}"
                                   + (" with displacement" if e.displacement else " (no displacement)")})
    for x in an.liq:
        if x.kind == "RUN":
            continue
        out.append({"id": liq_event_id(an.tf, x.kind, x.side, x.ts, x.level), "event_type": x.kind, "ts": x.ts,
                    "time": hm(x.ts), "chart_time": chart_time(x.ts), "tf": an.tf, "zone_id": None, "direction": x.dir,
                    "confidence": None, "price": x.wick,
                    "explanation": f"{x.kind.replace('_', ' ').lower()} of {x.side} {x.src} {x.level:,.1f} (wick {x.wick:,.1f})"})
    return out


def persist(events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    import json
    db.execmany("INSERT OR IGNORE INTO events (id, ts, tf, type, dir, zone_id, payload) VALUES (?,?,?,?,?,?,?)",
                [(e["id"], e["ts"], e["tf"], e["event_type"], e["direction"], e["zone_id"], json.dumps(e)) for e in events])


def markers(events: List[Dict[str, Any]], since_ts: int) -> List[Dict[str, Any]]:
    lab = {"DELTA_AGGRESSION": lambda e: "Δ+" if e["direction"] == "bull" else "Δ−",
           "DELTA_ABSORPTION": lambda e: "ABS", "DELTA_FLIP": lambda e: "FLIP",
           "FOOTPRINT_IMBALANCE": lambda e: "pIMB" if e.get("proxy") else "IMB"}
    return [{"time": e["chart_time"], "kind": e["event_type"], "dir": e["direction"], "price": e["price"],
             "text": lab[e["event_type"]](e), "at": e["time"], "id": e["id"]}
            for e in events if e["ts"] >= since_ts]
