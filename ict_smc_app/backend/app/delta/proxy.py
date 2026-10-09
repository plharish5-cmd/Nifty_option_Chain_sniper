"""
delta/proxy.py — OPTIONS FLOW PROXY. **PROXY (not real delta).** Opt-in (rules options_flow_proxy.enabled).

It uses only numbers Dhan really publishes for the SELECTED strikes' closed candles: premium change and traded
volume (optionally × the strike's |option delta| so a deep ITM lot counts like more index exposure).
It never sees who was the aggressor, so it is never called delta and never emits DELTA_* events.

PSEUDOCODE
    for each CLOSED index bar t (same tf):
        ce = CE candle at t, pe = PE candle at t                    (from REST /charts/intraday NSE_FNO)
        ce_flow = (ce.close − ce.open) × ce.volume × w_ce            w = |option delta| if weight_by_delta else 1
        pe_flow = (pe.close − pe.open) × pe.volume × w_pe
        net[t]  = ce_flow − pe_flow                                 > 0 = money flowing to calls / out of puts → bull
        z[t]    = (net[t] − mean(net[t−W … t−1])) / std(...)        current bar never in its own baseline
        |z| ≥ surge_z                       → PROXY_FLOW_SURGE  (dir = sign net)  + index zone if NIFTY is at one
        bar t−1 surged INTO an index zone and NIFTY's close at t did not extend by max(pts, k×ATR)
                                            → PROXY_FLOW_FADE   (dir = opposite)  stamped on bar t (no look-ahead)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .. import config
from ..engine.registry import Zone, at_price
from ..timeutil import TF_SECONDS, chart_time, hm, now_epoch

LABEL = "PROXY (not real delta)"
KINDS = ("PROXY_FLOW_SURGE", "PROXY_FLOW_FADE")


def _lk(x: float) -> str:
    a = abs(x)
    s = f"{a / 1e7:.2f} Cr" if a >= 1e7 else f"{a / 1e5:.1f} L" if a >= 1e5 else f"{a / 1e3:.1f} K" if a >= 1e3 else f"{a:.0f}"
    return ("−" if x < 0 else "+") + s


def _ztxt(z: Optional[Zone]) -> str:
    if z is None:
        return ""
    from .engine import _zone_txt
    return _zone_txt(z)


def compute(an, zones: List[Zone], ce: Optional[Dict[str, Any]], pe: Optional[Dict[str, Any]],
            now: Optional[float] = None, enabled: Optional[bool] = None) -> Dict[str, Any]:
    """ce / pe = {"instrument": Instrument, "candles": {index bar ts: option candle (o,h,l,c,v)}, "abs_delta": float}"""
    X = config.rules().get("options_flow_proxy", {})
    Z = config.rules().get("zones", {})
    on = X.get("enabled", False) if enabled is None else enabled
    res: Dict[str, Any] = {"tf": an.tf, "label": LABEL, "enabled": bool(on), "available": False, "bars": [],
                           "events": [], "message": ""}
    if not on:
        res["message"] = f"Options Flow Proxy is OFF — {LABEL}. Turn it on in the Delta / Footprint tab."
        return res
    if not ce and not pe:
        res["message"] = "No CE / PE strike selected yet."
        return res
    b, tf, step = an.bars, an.tf, TF_SECONDS[an.tf]
    now = now_epoch() if now is None else now
    n = 0
    for i in range(len(b)):
        if int(b.ts[i]) + step > now:
            break
        n = i + 1
    wd = bool(X.get("weight_by_delta", True))

    def flow(side: Optional[Dict[str, Any]], ts: int) -> Optional[float]:
        if not side:
            return 0.0
        c = side["candles"].get(ts)
        if c is None or not c.get("v") or float(c.get("h") or 0) <= 0:   # no trade / no price → no reading
            return None
        w = abs(float(side.get("abs_delta") or 1.0)) if wd else 1.0
        return (float(c["c"]) - float(c["o"])) * float(c["v"]) * w

    net: List[Optional[float]] = []
    parts: List[tuple] = []
    for i in range(n):
        t = int(b.ts[i])
        a, p = flow(ce, t), flow(pe, t)
        if a is None or p is None:
            net.append(None)
            parts.append((a, p))
            continue
        net.append(a - p)
        parts.append((a, p))
    if not any(v is not None for v in net):
        res["message"] = f"{LABEL}: no option candles with volume for the selected strikes yet."
        return res
    res["available"] = True
    W, min_n = int(X.get("baseline_window", 30)), int(X.get("baseline_min_bars", 10))
    zthr = float(X.get("surge_z", 2.0))
    z: List[Optional[float]] = [None] * n
    for i in range(n):
        if net[i] is None:
            continue
        base = [v for v in net[max(0, i - W):i] if v is not None]
        if len(base) < min_n:
            continue
        m = sum(base) / len(base)
        sd = math.sqrt(sum((v - m) ** 2 for v in base) / len(base))
        z[i] = round((net[i] - m) / sd, 2) if sd > 1e-9 else 0.0

    tol = lambda i: max(float(Z.get("touch_tolerance_pts", 1.0)), float(Z.get("touch_tolerance_atr", 0.05)) * float(an.atr[i]))  # noqa: E731
    hits = lambda i: at_price(zones, int(b.ts[i]), float(b.l[i]), float(b.h[i]), tol(i)) if i >= 0 else []   # noqa: E731
    lab = " / ".join(x["instrument"].label for x in (ce, pe) if x)
    ev: List[Dict[str, Any]] = []

    def emit(kind, i, direction, zone, conf, text, extra=None):
        ts = int(b.ts[i])
        e = {"id": f"{kind}|{tf}|{ts}|{zone.id if zone else '-'}|{lab}", "event_type": kind, "ts": ts, "time": hm(ts),
             "chart_time": chart_time(ts), "tf": tf, "zone_id": zone.id if zone else None,
             "zone": _ztxt(zone) or None, "zone_type": zone.type if zone else None,
             "zone_side": zone.side if zone else None, "direction": direction, "confidence": conf,
             "explanation": f"{LABEL} · {text}", "rule": kind.lower(), "price": float(b.c[i]), "net_flow": net[i],
             "z": z[i], "proxy": True, "label": LABEL, "instruments": lab}
        if extra:
            e.update(extra)
        ev.append(e)

    def surged(i: int) -> bool:
        return 0 <= i < n and z[i] is not None and abs(z[i]) >= zthr

    for i in range(n):
        res["bars"].append({"time": chart_time(int(b.ts[i])), "ts": int(b.ts[i]), "net": net[i], "z": z[i],
                            "ce": parts[i][0], "pe": parts[i][1]})
        hz = hits(i)
        z0 = hz[0] if hz else None
        if surged(i) and float(b.h[i]) - float(b.l[i]) >= float(X.get("min_candle_range_atr", 0.15)) * float(an.atr[i]):
            up = net[i] > 0
            a, p = parts[i]
            emit("PROXY_FLOW_SURGE", i, "bull" if up else "bear", z0, round(min(0.6, 0.35 + 0.08 * abs(z[i])), 2),
                 f"{'call' if up else 'put'}-side flow surge: CE {_lk(a or 0)} vs PE {_lk(p or 0)} "
                 f"(premium change × volume, z {z[i]:+.1f})" + (f" with NIFTY at {_ztxt(z0)}." if z0 else "."))
        k = i - 1
        if surged(k):
            hk = hits(k)
            prev = {zz.id for zz in hits(k - 1)}
            entered = [zz for zz in hk if zz.id not in prev]
            if entered:
                upk = net[k] > 0
                need = max(float(X.get("fade_min_follow_pts", 2.0)), float(X.get("fade_min_follow_atr", 0.1)) * float(an.atr[k]))
                ext = (float(b.c[i]) - float(b.c[k])) if upk else (float(b.c[k]) - float(b.c[i]))
                if ext < need:
                    emit("PROXY_FLOW_FADE", i, "bear" if upk else "bull", entered[0], 0.5,
                         f"{'call' if upk else 'put'}-side flow surged into {_ztxt(entered[0])} at {hm(int(b.ts[k]))} "
                         f"but NIFTY did not follow on the next candle ({ext:+.1f} pts < {need:.1f}) → fade.",
                         {"entry_ts": int(b.ts[k])})
    res["events"] = ev
    res["message"] = f"{LABEL} on {lab}"
    return res


def markers(res: Dict[str, Any], since_ts: int) -> List[Dict[str, Any]]:
    return [{"time": e["chart_time"], "kind": e["event_type"], "dir": e["direction"], "price": e["price"],
             "text": "pSURGE" if e["event_type"] == "PROXY_FLOW_SURGE" else "pFADE", "at": e["time"], "id": e["id"]}
            for e in res.get("events", []) if e["ts"] >= since_ts]
