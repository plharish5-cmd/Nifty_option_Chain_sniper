"""
engine/auction.py — Auction read (Market Profile / order-flow context). OBSERVATION ONLY: it never opens a paper
trade and never changes Setups A / B / C / D. CLOSED candles only, no look-ahead.

Ideas implemented (Viswas Yadav's 10 cards, "Market profile · orderflow · auction · context"):
  * Volume ≠ control. Activity is visible; control is only inferred from the RESPONSE of price.
  * LOCATION first: above value / inside value / below value, at a reference or at a balance extreme.
  * EFFORT vs RESULT: effort = real delta |z| (only when a real delta source is connected); result = price progress
    in ATR. High effort + low result = "activity without control". No delta source → effort = "unavailable"
    (HARD TRUTH RULE — we never invent delta; the NIFTY index has no volume).
  * STALL (balance): a run of closed candles inside ≤ k × ATR. Stalling is not the answer — the escape is.
  * RESPONSE at a reference: break → ACCEPTANCE (closes hold beyond + follow-through ≥ x ATR) or
    REJECTION (close back inside / wick probe that closes back). Holding beyond without progress = NO FOLLOW-THROUGH.
  * DECISION: a short text that combines the four — never a signal on its own.

Profile: NIFTY index has no volume, so this is a TPO / TIME-AT-PRICE profile built from closed 1-minute candles
(every minute adds 1 to each price bucket it traded through). Labelled "TPO (time)" everywhere, never "volume".

PSEUDOCODE
  profiles = tpo_profile(1m rows of each IST day)           → POC, VAH, VAL (value = 70 % of time around the POC)
  refs for a bar on day d = previous day's VAH / VAL / POC + PDH / PDL (fully known before day d opens)
  for each closed bar i (oldest → newest):
      balance: shrink window start s until range(s..i) ≤ k × ATR[i]; if ≥ min_bars long → a box (known at close i)
      a close beyond the box (box as of i−1) → break test on that edge
      for each ref L with no test pending:  close crosses L → test starts at i
                                            wick pokes ≥ pen beyond L and closes back → REJECT (probe) at i
      for a pending test started at k:      close back on the old side → REJECT at i
                                            i−k ≥ accept_bars, still beyond, progress ≥ follow_atr × ATR → ACCEPT at i
                                            i−k ≥ accept_bars + max_wait without progress → NO_FOLLOW at i
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .. import config
from ..timeutil import chart_time, hm, ist_date

KINDS = ("AUCTION_ACCEPT", "AUCTION_REJECT", "AUCTION_NO_FOLLOW")

DEFAULTS = {
    "step_pts": 5.0, "value_area_pct": 0.70,
    "balance_min_bars": 8, "balance_max_atr": 2.0,
    "accept_bars": 2, "follow_atr": 0.6, "max_wait_bars": 4, "probe_min_pen_pts": 3.0,
    "near_ref_atr": 0.35, "effort_high_z": 2.0, "result_low_atr": 0.35, "response_lookback_bars": 6,
}


def cfg() -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (config.rules().get("auction") or {}).items() if not k.startswith("_")})
    return out


# ── TPO profile ──────────────────────────────────────────────────────────────
def tpo_profile(rows: List[Dict[str, float]], step: float, va_pct: float = 0.70) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    counts: Dict[int, int] = defaultdict(int)
    for r in rows:
        for k in range(int(math.floor(r["l"] / step)), int(math.floor(r["h"] / step)) + 1):
            counts[k] += 1
    ks = sorted(counts)
    hi_k, lo_k = ks[-1], ks[0]
    mid = (hi_k + lo_k) / 2
    poc = max(ks, key=lambda k: (counts[k], -abs(k - mid)))
    total = sum(counts.values())
    a = b = poc
    got = counts[poc]
    while got < va_pct * total and (a > lo_k or b < hi_k):
        up = counts.get(b + 1, 0) if b < hi_k else -1
        dn = counts.get(a - 1, 0) if a > lo_k else -1
        if up >= dn:
            b += 1
            got += max(up, 0)
        else:
            a -= 1
            got += max(dn, 0)
    hi, lo = max(r["h"] for r in rows), min(r["l"] for r in rows)
    return {"poc": round(min(max((poc + 0.5) * step, lo), hi), 2), "vah": round(min((b + 1) * step, hi), 2),
            "val": round(max(a * step, lo), 2), "high": hi, "low": lo, "minutes": len(rows),
            "step": step, "hist": [[round(k * step, 2), counts[k]] for k in ks]}


def day_profiles(rows_1m: List[Dict[str, float]], step: float, va_pct: float) -> Dict[str, Dict[str, Any]]:
    by: Dict[str, List[Dict[str, float]]] = defaultdict(list)
    for r in rows_1m:
        if r.get("closed", True):
            by[ist_date(r["ts"]).isoformat()].append(r)
    return {d: p for d, rs in sorted(by.items()) if (p := tpo_profile(rs, step, va_pct))}


def refs_for(day: str, profiles: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    prev = [d for d in profiles if d < day]
    if not prev:
        return []
    p = profiles[prev[-1]]
    return [{"name": "pVAH", "price": p["vah"]}, {"name": "pVAL", "price": p["val"]}, {"name": "pPOC", "price": p["poc"]},
            {"name": "PDH", "price": p["high"]}, {"name": "PDL", "price": p["low"]}]


# ── bar scan ─────────────────────────────────────────────────────────────────
def compute(an, rows_1m: List[Dict[str, float]], delta_bars: Optional[Dict[int, float]] = None,
            delta_label: str = "unavailable", vol: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """vol (optional): {"profiles": day → VOLUME profile (NIFTY FUT, basis-adjusted), "rvol": bar ts → {rvol},
    "act": bar ts → {z} (options activity)}. With volume profiles the references are volume-based; else TPO."""
    """an: engine Analysis of the chart tf (closed bars). delta_bars: {bar ts → delta z} from a REAL delta source,
    or None. Returns profiles, balance boxes, events, the 5-question read, and chart markers."""
    C = cfg()
    b, A = an.bars, an.atr
    N = len(b)
    step = float(C["step_pts"])
    vol = vol or {}
    use_vol = bool(vol.get("profiles"))
    profiles = vol["profiles"] if use_vol else day_profiles(rows_1m, step, float(C["value_area_pct"]))
    events: List[Dict[str, Any]] = []
    boxes: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}
    s = 0
    box: Optional[Dict[str, Any]] = None
    cur_day = None
    refs: List[Dict[str, Any]] = []
    acc_n, foll, wait, pen = int(C["accept_bars"]), float(C["follow_atr"]), int(C["max_wait_bars"]), float(C["probe_min_pen_pts"])

    def emit(kind: str, i: int, ref: Dict[str, Any], direction: str, text: str, **extra) -> None:
        events.append({"kind": kind, "i": i, "ts": int(b.ts[i]), "time": chart_time(int(b.ts[i])), "at": hm(int(b.ts[i])),
                       "day": cur_day, "ref": ref["name"], "level": round(ref["price"], 2), "dir": direction, "text": text, **extra})

    for i in range(1, N):
        d = ist_date(int(b.ts[i])).isoformat()
        if d != cur_day:                                    # new session: new refs, old tests and boxes end
            cur_day, refs, pending, s, box = d, refs_for(d, profiles), {}, i, None
        h, l, c, pc, atr = float(b.h[i]), float(b.l[i]), float(b.c[i]), float(b.c[i - 1]), max(float(A[i]), 1e-9)

        # 1) balance box known at the close of i−1 → an escape on bar i is a break test of its edge
        if box is not None and box["end_i"] == i - 1 and (c > box["hi"] or c < box["lo"]):
            up = c > box["hi"]
            ref = {"name": "Bal-H" if up else "Bal-L", "price": box["hi"] if up else box["lo"]}
            box["escaped"] = "up" if up else "down"
            pending[ref["name"] + f"@{box['start_i']}"] = {"ref": ref, "k": i, "up": up}
            s, box = i, None
        # 2) pending tests
        resolved = set()                                    # a ref resolved on this bar can't start a new test on it
        for key in list(pending):
            p = pending[key]
            L, up, k = p["ref"]["price"], p["up"], p["k"]
            if i == k:
                continue
            back = c < L if up else c > L
            if back:
                emit("AUCTION_REJECT", i, p["ref"], "bear" if up else "bull",
                     f"Rejected {p['ref']['name']} {L:,.1f}: broke {'above' if up else 'below'} at {hm(int(b.ts[k]))}, "
                     f"closed back {'below' if up else 'above'} — no acceptance, the other side still controls.",
                     start_ts=int(b.ts[k]))
                del pending[key]
                resolved.add(key)
                continue
            prog = float(max(b.c[k:i + 1]) - L) if up else float(L - min(b.c[k:i + 1]))   # closes, not wicks
            if i - k >= acc_n and prog >= foll * atr:
                emit("AUCTION_ACCEPT", i, p["ref"], "bull" if up else "bear",
                     f"Accepted {'above' if up else 'below'} {p['ref']['name']} {L:,.1f}: {i - k + 1} closes held beyond "
                     f"with {prog:,.1f} pts follow-through ({prog / atr:.1f} ATR).", start_ts=int(b.ts[k]),
                     progress_atr=round(float(prog) / atr, 2))
                del pending[key]
                resolved.add(key)
            elif i - k >= acc_n + wait:
                emit("AUCTION_NO_FOLLOW", i, p["ref"], "flat",
                     f"Holding {'above' if up else 'below'} {p['ref']['name']} {L:,.1f} but only {prog:,.1f} pts "
                     f"({prog / atr:.1f} ATR) of progress — breakout without follow-through.", start_ts=int(b.ts[k]))
                del pending[key]
                resolved.add(key)
        # 3) fresh tests / probes at the day's references
        for ref in refs:
            if ref["name"] in pending or ref["name"] in resolved:
                continue
            L = ref["price"]
            if pc <= L < c or pc >= L > c:
                pending[ref["name"]] = {"ref": ref, "k": i, "up": c > L}
            elif pc < L and c < L and h >= L + pen:
                emit("AUCTION_REJECT", i, ref, "bear", f"Probe above {ref['name']} {L:,.1f} (high {h:,.1f}) closed back "
                     f"below — rejected at the reference.", probe=True)
            elif pc > L and c > L and l <= L - pen:
                emit("AUCTION_REJECT", i, ref, "bull", f"Probe below {ref['name']} {L:,.1f} (low {l:,.1f}) closed back "
                     f"above — rejected at the reference.", probe=True)
        # 4) balance window ending at i
        while s < i and (max(b.h[s:i + 1]) - min(b.l[s:i + 1])) > float(C["balance_max_atr"]) * atr:
            s += 1
        if i - s + 1 >= int(C["balance_min_bars"]):
            hi_, lo_ = float(max(b.h[s:i + 1])), float(min(b.l[s:i + 1]))
            if boxes and boxes[-1]["escaped"] is None and s <= boxes[-1]["end_i"] and ist_date(int(b.ts[boxes[-1]["end_i"]])).isoformat() == d:
                box = boxes[-1]                              # the same stall, its window slid forward
                box.update({"start_i": min(s, box["start_i"]), "end_i": i, "hi": max(hi_, box["hi"]), "lo": min(lo_, box["lo"])})
            else:
                box = {"start_i": s, "end_i": i, "hi": hi_, "lo": lo_, "escaped": None}
                boxes.append(box)
        else:
            box = None

    last_i = N - 1
    for x in boxes:
        x.update({"from": chart_time(int(b.ts[x["start_i"]])), "to": chart_time(int(b.ts[x["end_i"]])),
                  "start": hm(int(b.ts[x["start_i"]])), "end": hm(int(b.ts[x["end_i"]])), "bars": x["end_i"] - x["start_i"] + 1,
                  "active": x["end_i"] == last_i and x["escaped"] is None})
    today = ist_date(int(b.ts[last_i])).isoformat()
    res = {"tf": an.tf, "profile_kind": ("VOLUME profile — current-month NIFTY future, basis-adjusted to index prices"
                                         if use_vol else "TPO (time at price) — no futures volume loaded yet"),
           "today": profiles.get(today), "prev": profiles.get(max([d for d in profiles if d < today], default="")) if profiles else None,
           "refs": refs_for(today, profiles), "boxes": [x for x in boxes if ist_date(int(b.ts[x["end_i"]])).isoformat() == today][-6:],
           "events": events,
           "pending": [{"ref": p["ref"]["name"], "level": p["ref"]["price"], "since": hm(int(b.ts[p["k"]])),
                        "side": "above" if p["up"] else "below"} for p in pending.values()],
           "delta_label": delta_label, "last_close": float(b.c[last_i])}
    res["read"] = read(an, res, delta_bars, delta_label, C, vol)
    for k in ("today", "prev"):
        if res[k]:
            res[k] = {kk: v for kk, v in res[k].items() if kk != "hist"} | {"hist": res[k]["hist"][-160:]}
    return res


# ── the 5 questions ──────────────────────────────────────────────────────────
def read(an, res: Dict[str, Any], delta_bars: Optional[Dict[int, float]], delta_label: str, C: Dict[str, Any],
         vol: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    b, A = an.bars, an.atr
    i = len(b) - 1
    c, atr = float(b.c[i]), max(float(A[i]), 1e-9)
    prev, today = res["prev"], res["today"]
    near = float(C["near_ref_atr"]) * atr

    # 1 LOCATION
    if prev:
        where = "above previous value" if c > prev["vah"] else "below previous value" if c < prev["val"] else "inside previous value"
    else:
        where = "no previous-day profile yet"
    if today:
        where += f" · {'above' if c > today['vah'] else 'below' if c < today['val'] else 'inside'} today's developing value"
    at = [r for r in res["refs"] if abs(c - r["price"]) <= near]
    box = next((x for x in res["boxes"] if x["active"]), None)
    edge = None
    if box:
        edge = "at the balance high" if box["hi"] - c <= near else "at the balance low" if c - box["lo"] <= near else "mid-balance"
        where += f" · in a {box['bars']}-bar balance {box['lo']:,.1f}–{box['hi']:,.1f} ({edge})"
    loc = {"text": where + (f" · at {', '.join(r['name'] + ' ' + format(r['price'], ',.1f') for r in at)}" if at else ""),
           "at_ref": bool(at) or edge in ("at the balance high", "at the balance low")}

    # 2 EFFORT — delta (who attacked) if a source is connected; volume + options activity (how much traded)
    vol = vol or {}
    parts, high = [], False
    z = None
    if delta_bars is not None:
        zs = [delta_bars.get(int(b.ts[k])) for k in range(max(0, i - 2), i + 1)]
        zs = [x for x in zs if x is not None]
        z = zs[-1] if zs else None
        if z is not None:
            parts.append(f"delta z {z:+.1f} ({delta_label})")
            high |= abs(z) >= float(C["effort_high_z"])
        else:
            parts.append(f"no delta reading on the last candle yet ({delta_label} — needs ≥ 80 % of its minutes recorded)")
    rv = (vol.get("rvol") or {}).get(int(b.ts[i])) or {}
    rvol = rv.get("rvol")
    if rvol is not None:
        parts.append(f"FUT volume {rvol:.1f}× normal for this time of day")
        high |= rvol >= float(C.get("effort_high_rvol", 1.5))
    act = (vol.get("act") or {}).get(int(b.ts[i])) or {}
    az = act.get("z")
    if az is not None:
        parts.append(f"options activity z {az:+.1f} (direction unknown)")
        high |= az >= float(C.get("effort_high_act_z", 2.0))
    ob = (vol.get("oi") or {}).get(int(b.ts[i])) or {}
    if ob.get("build") and ob["build"] != "flat":
        parts.append(f"futures {ob['build']} (OI {ob.get('doi', 0):+,})")
    ol = (vol.get("oi_line") or {}).get(int(b.ts[i])) or {}
    if ol.get("surge"):
        parts.append(f"OI SURGE {ol.get('surge_x')}× (OI above its 8-EMA) — big move building, direction from structure")
        high = True
    cp = vol.get("oi_compare") or {}
    if cp.get("verdict") in ("aligned", "diverging"):
        parts.append(f"BANKNIFTY: {cp['text']}")
    if parts:
        eff = {"available": True, "z": z, "rvol": rvol, "act_z": az, "high": high, "text": " · ".join(parts)
               + ("" if delta_bars is not None else " — volume says how much traded, not who won: no delta source yet")}
    else:
        eff = {"available": False, "z": None, "rvol": None, "act_z": None, "high": False,
               "text": "Unavailable — no futures volume, options activity or delta source yet. Judge on location + "
                       "response only (structure only)."}

    # 3 RESULT
    k0 = max(0, i - 2)
    move = c - float(b.o[k0])
    res_atr = abs(move) / atr
    result = {"atr": round(res_atr, 2), "pts": round(move, 1),
              "text": f"last 3 closed candles moved {move:+,.1f} pts ({res_atr:.1f} ATR)"
                      + (" — limited progress" if res_atr <= float(C["result_low_atr"]) else "")}
    it = vol.get("iv_trend") or {}
    if it.get("word") in ("falling", "rising"):
        result["text"] += (f" · ATM IV {it['word']} {it['from']:.1f}→{it['to']:.1f} in {it['minutes']} min ("
                           + ("orderly, cleaner move" if it["word"] == "falling" else "fear: wider swings") + ")")
    if eff["high"] and res_atr <= float(C["result_low_atr"]):
        result["text"] += " · HIGH EFFORT, LOW RESULT: activity without control (possible absorption — wait for the response)"

    # 4 RESPONSE
    lb = int(C["response_lookback_bars"])
    recent = [e for e in res["events"] if e["i"] >= i - lb]
    last = recent[-1] if recent else None
    if last:
        resp = {"kind": last["kind"], "text": f"{last['at']} {last['text']}"}
    elif res["pending"]:
        p = res["pending"][-1]
        resp = {"kind": "TESTING", "text": f"testing {p['ref']} {p['level']:,.1f} from {p['side']} since {p['since']} — "
                                           f"not accepted or rejected yet"}
    else:
        resp = {"kind": None, "text": "no reference tested in the last few candles"}

    # 5 DECISION (observation, never a trade on its own)
    k = resp["kind"]
    if k == "AUCTION_ACCEPT":
        up = last["dir"] == "bull"
        dec = (f"Auction is accepting {'higher' if up else 'lower'} prices beyond {last['ref']}. Favour "
               f"{'longs' if up else 'shorts'} only on a pullback that holds {last['ref']} {last['level']:,.1f} "
               f"and only with a playbook / CRT entry.")
        bias = "bull" if up else "bear"
    elif k == "AUCTION_REJECT":
        up = last["dir"] == "bull"
        dec = (f"Failed auction at {last['ref']} {last['level']:,.1f} — the move was rejected. Expect rotation "
               f"{'back up' if up else 'back down'} into value (POC next); don't chase the failed side.")
        bias = "bull" if up else "bear"
    elif k == "AUCTION_NO_FOLLOW":
        dec = "Breakout without follow-through — apparent strength may disappear. Stand aside until a clean accept or reject."
        bias = None
    elif box:
        dec = "In balance: buyers and sellers are matched. No edge inside the box — wait for an escape and its acceptance."
        bias = None
    elif k == "TESTING":
        dec = "A reference is under test. One candle beyond it doesn't confirm acceptance — wait for the response."
        bias = None
    else:
        dec = "Insufficient evidence — no reference in play. Let structure (Setups A / C / D) lead."
        bias = None
    return {"location": loc, "effort": eff, "result": result, "response": resp,
            "decision": {"text": dec, "bias": bias, "note": "Observation only — never a trade on its own."}}


def markers(res: Dict[str, Any], since_ts: int) -> List[Dict[str, Any]]:
    """One marker per candle and outcome: refs resolved together are joined ("ACC pVAH·pPOC·PDH")."""
    short = {"AUCTION_ACCEPT": "ACC", "AUCTION_REJECT": "REJ", "AUCTION_NO_FOLLOW": "NoFT"}
    grp: Dict[tuple, Dict[str, Any]] = {}
    for e in res.get("events", []):
        if e["ts"] < since_ts:
            continue
        k = (e["time"], e["kind"], e["dir"])
        if k in grp:
            grp[k]["refs"].append(e["ref"])
            continue
        grp[k] = {"time": e["time"], "kind": e["kind"], "dir": e["dir"], "price": e["level"], "at": e["at"], "refs": [e["ref"]]}
    out = []
    for m in grp.values():
        m["text"] = f"{short[m['kind']]} {'·'.join(m.pop('refs'))}"
        out.append(m)
    return out
