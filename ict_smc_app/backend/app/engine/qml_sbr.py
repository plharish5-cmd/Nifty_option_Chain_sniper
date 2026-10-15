"""
engine/qml_sbr.py — Line-chart Quasimodo (QML) · RBS · SBR · SNR fakeouts (MSNR school). Observation layer only:
it never places orders and never feeds Setups A/B/C/D, paper trades or the scorecard.

All structure is read from CLOSE prices (the "line chart") — wicks are treated as liquidity, not structure.

  Pivots     close[i] is the highest (lowest) close of i−n … i+n  → line-chart peak (valley). Known at i+n (no look-ahead).
  Bear QML   peaks/valleys  H → L → HH (HH close above H) → a CLOSE below L (the BOS)  ⇒ ARMED at the BOS bar.
             QML level = the H close. TRIGGERED: first later candle whose high reaches QML − tol and closes red, below
             QML. INVALID: a close above the HH close (or above QML + tol on the test candle = full displacement
             through the level), or no test within max_age bars (EXPIRED).
  Bull QML   mirror: L → H → LL → a CLOSE above H.  QML level = the L close.
  RBS        a confirmed line peak closed through by ≥ min_break → old resistance = support. TRIGGERED on the first
             retest: low ≤ peak + tol, close green and above the peak. INVALID on a close back below peak − min_break.
  SBR        mirror for a broken valley → resistance.
  SNR fakeout a candle whose WICK pierces an unbroken line peak (valley) but whose BODY stays inside:
             upthrust (bearish) / spring (bullish).

Trade plan per setup (information, not backtested): entry = level, SL beyond the structure wick + buffer, TP1 = the
opposing structure, TP2 = 2.2 R.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .primitives import Bars

DEF = {"tol_atr": 0.25, "min_break_atr": 0.15, "sl_buf_atr": 0.1, "tp2_r": 2.2, "max_age": 40, "keep": 4,
       "min_risk_atr": 0.5, "max_risk_atr": 2.5}


def line_pivots(c: np.ndarray, n: int) -> List[Dict[str, Any]]:
    """Alternating close-price pivots: [{kind 'H'|'L', i, price, known}] — consecutive same-kind pivots collapse to
    the more extreme one."""
    N = len(c)
    raw: List[Dict[str, Any]] = []
    for i in range(n, N - n):
        w = c[i - n:i + n + 1]
        if c[i] == w.max() and (w == c[i]).sum() == 1:
            raw.append({"kind": "H", "i": i, "price": float(c[i]), "known": i + n})
        elif c[i] == w.min() and (w == c[i]).sum() == 1:
            raw.append({"kind": "L", "i": i, "price": float(c[i]), "known": i + n})
    out: List[Dict[str, Any]] = []
    for p in raw:
        if out and out[-1]["kind"] == p["kind"]:
            better = p["price"] > out[-1]["price"] if p["kind"] == "H" else p["price"] < out[-1]["price"]
            if better:
                out[-1] = p
        else:
            out.append(p)
    return out


def _wick(b: Bars, i: int, kind: str, n: int) -> float:
    lo, hi = max(0, i - 1), min(len(b) - 1, i + 1)
    return float(b.h[lo:hi + 1].max()) if kind == "H" else float(b.l[lo:hi + 1].min())


def compute(b: Bars, atr: np.ndarray, n: int, T, hm, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """T(i) → chart time, hm(ts) → 'HH:MM'. Returns {setups, markers, spine_n, summary}."""
    C = {**DEF, **(cfg or {})}
    N = len(b)
    if N < 4 * n + 6:
        return {"setups": [], "markers": [], "summary": "not enough candles"}
    c, o, h, l = b.c, b.o, b.h, b.l
    A = lambda i: float(atr[min(max(i, 0), N - 1)]) or 1.0          # noqa: E731
    P = line_pivots(c, n)
    setups: List[Dict[str, Any]] = []
    markers: List[Dict[str, Any]] = []

    def mk(i, kind, d, price, text):
        markers.append({"time": T(i), "kind": kind, "dir": d, "price": round(price, 2), "text": text, "at": hm(int(b.ts[i]))})

    # ── QML (bear: H, L, HH then close < L ; bull: L, H, LL then close > H) ──
    for k in range(len(P) - 2):
        a, m, z = P[k], P[k + 1], P[k + 2]
        if a["kind"] == "H" and m["kind"] == "L" and z["kind"] == "H" and z["price"] > a["price"]:
            d, qml, swing, ext = "bear", a["price"], m["price"], z
        elif a["kind"] == "L" and m["kind"] == "H" and z["kind"] == "L" and z["price"] < a["price"]:
            d, qml, swing, ext = "bull", a["price"], m["price"], z
        else:
            continue
        start = z["known"] + 1
        bos = next((j for j in range(max(start, z["i"] + 1), N) if (c[j] < swing if d == "bear" else c[j] > swing)), None)
        if bos is None:
            continue
        # structure must not have been invalidated before the BOS (close beyond the extreme)
        if any((c[j] > ext["price"] if d == "bear" else c[j] < ext["price"]) for j in range(z["i"] + 1, bos)):
            continue
        tol, buf = C["tol_atr"] * A(bos), C["sl_buf_atr"] * A(bos)
        wick = _wick(b, ext["i"], "H" if d == "bear" else "L", n)
        sl = wick + buf if d == "bear" else wick - buf
        risk = abs(sl - qml) or 1.0
        lo_after = float(l[bos:N].min()) if d == "bear" else float(h[bos:N].max())
        s = {"kind": "QML", "dir": d, "level": round(qml, 2), "tol": round(tol, 2), "bos_i": bos, "status": "ARMED",
             "entry": round(qml, 2), "sl": round(sl, 2),
             "tp1": round(min(swing, lo_after) if d == "bear" else max(swing, lo_after), 2),
             "tp2": round(qml - C["tp2_r"] * risk if d == "bear" else qml + C["tp2_r"] * risk, 2),
             "structure": [{"label": x, "i": p["i"], "price": round(p["price"], 2)} for x, p in
                           zip(("H", "L", "HH") if d == "bear" else ("L", "H", "LL"), (a, m, z))],
             "trig_i": None, "end_i": None}
        s["tp1_r"] = round(abs(s["entry"] - s["tp1"]) / risk, 2)
        s["rr"] = C["tp2_r"]
        for j in range(bos + 1, min(N, bos + 1 + int(C["max_age"]))):
            if (c[j] > ext["price"] if d == "bear" else c[j] < ext["price"]):
                s["status"], s["end_i"] = "INVALID", j
                break
            touch = h[j] >= qml - tol if d == "bear" else l[j] <= qml + tol
            if not touch:
                continue
            if (c[j] > qml + tol if d == "bear" else c[j] < qml - tol):        # displacement through the level
                s["status"], s["end_i"] = "INVALID", j
                break
            if (c[j] < o[j] and c[j] < qml) if d == "bear" else (c[j] > o[j] and c[j] > qml):
                s["status"], s["trig_i"] = "TRIGGERED", j
                break
        else:
            if N - 1 - bos >= int(C["max_age"]):
                s["status"] = "EXPIRED"
        setups.append(s)

    # ── RBS / SBR from broken line pivots ──
    for p in P:
        brk_need = C["min_break_atr"]
        d = "bull" if p["kind"] == "H" else "bear"                           # H broken up → RBS (buy); L broken down → SBR
        lvl = p["price"]
        brk = next((j for j in range(p["known"] + 1, N)
                    if (c[j] > lvl + brk_need * A(j) if d == "bull" else c[j] < lvl - brk_need * A(j))), None)
        if brk is None:
            continue
        tol, buf = C["tol_atr"] * A(brk), C["sl_buf_atr"] * A(brk)
        s = {"kind": "RBS" if d == "bull" else "SBR", "dir": d, "level": round(lvl, 2), "tol": round(tol, 2), "bos_i": brk,
             "status": "ARMED", "entry": round(lvl, 2), "trig_i": None, "end_i": None,
             "structure": [{"label": "peak" if d == "bull" else "valley", "i": p["i"], "price": round(lvl, 2)}]}
        trig = None
        for j in range(brk + 1, min(N, brk + 1 + int(C["max_age"]))):
            if (c[j] < lvl - brk_need * A(j) if d == "bull" else c[j] > lvl + brk_need * A(j)):
                s["status"], s["end_i"] = "INVALID", j
                break
            if (l[j] <= lvl + tol and c[j] > o[j] and c[j] > lvl) if d == "bull" else (h[j] >= lvl - tol and c[j] < o[j] and c[j] < lvl):
                trig = j
                s["status"], s["trig_i"] = "TRIGGERED", j
                break
        else:
            if N - 1 - brk >= int(C["max_age"]):
                s["status"] = "EXPIRED"
        ref = trig if trig is not None else N - 1
        wick = float(l[brk:ref + 1].min()) if d == "bull" else float(h[brk:ref + 1].max())
        sl = min(wick, lvl - tol) - buf if d == "bull" else max(wick, lvl + tol) + buf
        risk = abs(lvl - sl) or 1.0
        ext = float(h[p["known"]:brk + 1].max()) if d == "bull" else float(l[p["known"]:brk + 1].min())
        s.update(sl=round(sl, 2), tp1=round(ext, 2),
                 tp2=round(lvl + C["tp2_r"] * risk if d == "bull" else lvl - C["tp2_r"] * risk, 2), rr=C["tp2_r"])
        s["tp1_r"] = round(abs(s["tp1"] - lvl) / risk, 2)
        setups.append(s)

    # ── SNR fakeouts on still-unbroken line pivots (only the latest few bars matter) ──
    first = max(0, N - 150)
    for p in P:
        lvl = p["price"]
        broken = False
        for j in range(p["known"] + 1, N):
            if (c[j] > lvl if p["kind"] == "H" else c[j] < lvl):
                broken = True
                break
            if j < first:
                continue
            if p["kind"] == "H" and h[j] > lvl and max(o[j], c[j]) <= lvl:
                mk(j, "SNR_UPTHRUST", "bear", lvl, f"Upthrust — wick above line peak {lvl:,.0f}, body closed back below (bearish fakeout)")
            elif p["kind"] == "L" and l[j] < lvl and min(o[j], c[j]) >= lvl:
                mk(j, "SNR_SPRING", "bull", lvl, f"Spring — wick below line valley {lvl:,.0f}, body closed back above (bullish fakeout)")
        _ = broken

    # ── risk sanity + what happened after the trigger (closed candles only) ──
    for s in setups:
        d, e = s["dir"], s["entry"]
        a_ = A(s["bos_i"])
        risk = abs(e - s["sl"])
        if risk < C["min_risk_atr"] * a_:                                   # stop too tight to survive noise
            s["sl"] = round(e + C["min_risk_atr"] * a_ if d == "bear" else e - C["min_risk_atr"] * a_, 2)
            risk = abs(e - s["sl"])
        sg = -1 if d == "bear" else 1
        if (s["tp1"] - e) * sg < risk:                                       # TP1 at least 1R away
            s["tp1"] = round(e + sg * risk, 2)
        s["tp2"] = round(e + sg * C["tp2_r"] * risk, 2)
        if abs(s["tp1"] - e) > abs(s["tp2"] - e):                          # structure target beyond 2.2R → it becomes TP2
            s["tp1"], s["tp2"] = s["tp2"], s["tp1"]
        s["tp1_r"] = round(abs(s["tp1"] - e) / risk, 2)
        s["rr"] = round(abs(s["tp2"] - e) / risk, 2)
        if risk > C["max_risk_atr"] * a_ and s["status"] in ("ARMED", "TRIGGERED"):
            s["status"], s["veto"] = "NO-TRADE", f"stop {risk:.0f} pts = {risk / a_:.1f}× ATR — too wide"
        if s["status"] == "TRIGGERED" and s["trig_i"] is not None:
            for j in range(s["trig_i"] + 1, N):
                if (h[j] >= s["sl"] if d == "bear" else l[j] <= s["sl"]):
                    s["status"], s["end_i"] = "STOPPED", j
                    break
                if (l[j] <= s["tp2"] if d == "bear" else h[j] >= s["tp2"]):
                    s["status"], s["end_i"] = "TP2 HIT", j
                    break
                if s.get("tp1_i") is None and (l[j] <= s["tp1"] if d == "bear" else h[j] >= s["tp1"]):
                    s["tp1_i"] = j
            if s["status"] == "TRIGGERED" and s.get("tp1_i") is not None:
                s["status"] = "TP1 HIT"
    # one setup per (kind, side, break bar): several pivots broken by the same candle → keep the newest pivot
    uniq: Dict[tuple, Dict[str, Any]] = {}
    for s in setups:
        k_ = (s["kind"], s["dir"], s["bos_i"])
        if k_ not in uniq or s["structure"][0]["i"] > uniq[k_]["structure"][0]["i"]:
            uniq[k_] = s
    setups = list(uniq.values())
    # an armed level more than near_atr × ATR from price is not tradable now
    px, a_now = float(c[-1]), A(N - 1)
    for s in setups:
        if s["status"] == "ARMED" and abs(s["level"] - px) > float(C.get("near_atr", 4.0)) * a_now:
            s["status"], s["veto"] = "FAR", f"{abs(s['level'] - px):.0f} pts from price"
    # keep the most recent setups of each kind, newest last
    setups.sort(key=lambda s: s["bos_i"])
    keep: List[Dict[str, Any]] = []
    for kind in ("QML", "RBS", "SBR"):
        keep += [s for s in setups if s["kind"] == kind][-int(C["keep"]):]
    keep.sort(key=lambda s: s["bos_i"])
    for s in keep:
        side = "SELL · buy PE" if s["dir"] == "bear" else "BUY · buy CE"
        nm = {"QML": f"{'Bearish' if s['dir'] == 'bear' else 'Bullish'} QML", "RBS": "RBS (resistance → support)",
              "SBR": "SBR (support → resistance)"}[s["kind"]]
        s["name"] = nm
        s["side"] = side
        s["from"], s["to"] = T(s["structure"][0]["i"]), T(N - 1 if s["status"] in ("ARMED", "TRIGGERED", "TP1 HIT") else (s.get("end_i") or s.get("trig_i") or N - 1))
        s["bos_at"] = hm(int(b.ts[s["bos_i"]]))
        s["trig_at"] = hm(int(b.ts[s["trig_i"]])) if s["trig_i"] is not None else None
        s["rule"] = (f"Enter on the first candle that tests {s['level']:,.0f} (±{s['tol']:.0f}) and CLOSES "
                     + ("red below it" if s["dir"] == "bear" else "green above it") + f"; SL {s['sl']:,.0f}, TP1 {s['tp1']:,.0f}, TP2 {s['tp2']:,.0f}.")
        s["no_trade"] = ("No trade if a candle closes with a full body " + ("above" if s["dir"] == "bear" else "below")
                         + f" {s['level']:,.0f} (displacement through the level) or price closes beyond the SL structure.")
        bi = s["bos_i"]
        if bi >= first:
            mk(bi, f"{s['kind']}_ARMED", s["dir"], s["level"],
               f"{nm} armed at {s['level']:,.0f} — " + ("BOS close below the L" if s["kind"] == "QML" and s["dir"] == "bear" else
                                                       "BOS close above the H" if s["kind"] == "QML" else "level broken by a body close"))
        if s["trig_i"] is not None and s["trig_i"] >= first:
            mk(s["trig_i"], f"{s['kind']}_TRIG", s["dir"], s["level"], f"{nm} TRIGGERED — {side} · SL {s['sl']:,.0f} · TP1 {s['tp1']:,.0f}")
        if s["kind"] == "QML":
            for st in s["structure"]:
                if st["i"] >= first:
                    mk(st["i"], "QML_PT", s["dir"], st["price"], f"{st['label']} {st['price']:,.0f} (line close)")
        for k_ in ("bos_i", "trig_i", "end_i", "tp1_i"):
            s.pop(k_, None)
        for st in s["structure"]:
            st["time"] = T(st.pop("i"))
    markers.sort(key=lambda m: m["time"])
    live = [s for s in keep if s["status"] in ("ARMED", "TRIGGERED", "TP1 HIT")]
    if live:
        x = live[-1]
        summary = (f"{x['name']} {x['status'].lower()} at {x['level']:,.0f} → {x['side']}; SL {x['sl']:,.0f}, "
                   f"TP1 {x['tp1']:,.0f}, TP2 {x['tp2']:,.0f}. {len(live)} live level(s) on the line chart.")
    else:
        summary = "No live QML / RBS / SBR level on the line chart right now."
    return {"setups": keep, "markers": markers[-60:], "summary": summary, "pivot_n": n}
