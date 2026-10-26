"""
engine/rvol.py — CUMULATIVE relative volume (the "stock in play" test), on the current-month NIFTY future.

    cRVOL(t) = today's volume from 09:15 up to minute t  ÷  average volume from 09:15 up to the SAME minute t
               over the previous N sessions                     (N = 20 → "20-day", N = 5 → "5-day")

Why cumulative and time-matched: NIFTY volume is U-shaped (09:15 is always busy, 13:00 always quiet), so a raw bar
volume or a whole-day average lies. 2.0 at 10:15 means "twice the volume NIFTY normally has done by 10:15".
Only earlier sessions form the baseline and only closed minutes are counted → no look-ahead.

The reading (participation, never direction):
  ACTIVE     cRVOL ≥ rvol.active (1.5)  — the market cares today: levels get respected, moves travel
  NORMAL     1.0 ≤ cRVOL < 1.5
  ABSENT     cRVOL < 1.0               — below a normal day: breakouts happen "in an empty room" and fail more
  trend over the last rvol.trend_minutes: BUILDING (rising) · HOLDING · FADING (falling)
Events (each fires once per session, deduplicated):
  RVOL_ACTIVATED     cRVOL crosses up through `active` and holds for `confirm_minutes`
  RVOL_SPIKE         one candle's own RVOL ≥ rvol.spike (2.5) — a burst of participation right now
  RVOL_FADING        after ACTIVE, cRVOL falls back below `fade_below` (1.2)
  BREAKOUT_NO_RVOL   a BOS / MSS / CRT TBS candle while cRVOL < 1.0 → "breakout with nobody behind it"
  BREAKOUT_WITH_RVOL a BOS / MSS / CRT TBS candle while cRVOL ≥ active and the candle's RVOL ≥ 1.5
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional

from .. import config
from ..timeutil import TF_SECONDS, bucket_start, chart_time, hm, ist_date, session_bounds, to_ist

D = {"active": 1.5, "fade_below": 1.2, "spike": 2.5, "confirm_minutes": 3, "trend_minutes": 15, "trend_min_chg": 0.1,
     "min_days_5": 3, "min_days_20": 10, "breakout_bar_rvol": 1.5}


def cfg() -> Dict[str, Any]:
    out = dict(D)
    out.update({k: v for k, v in (config.rules().get("rvol") or {}).items() if not k.startswith("_")})
    return out


def _by_day(rows: List[Dict[str, float]]) -> Dict[str, Dict[int, float]]:
    """IST day → {minute offset from 09:15 → that minute's volume}."""
    out: Dict[str, Dict[int, float]] = defaultdict(dict)
    for r in rows:
        d = ist_date(int(r["ts"]))
        o, _ = session_bounds(d)
        out[d.isoformat()][int((int(r["ts"]) - o) // 60)] = float(r.get("v") or 0.0)
    return out


def _cum(mins: Dict[int, float], upto: int) -> float:
    return sum(v for k, v in mins.items() if k <= upto)


def cumulative(rows: List[Dict[str, float]], day: Optional[str] = None) -> Dict[str, Any]:
    """rows: 1-minute FUT candles (several sessions, closed). Returns today's minute series of cRVOL 20 / 5."""
    C = cfg()
    days = _by_day(rows)
    if not days:
        return {"available": False, "message": "no futures volume yet"}
    day = day or max(days)
    prev = sorted(d for d in days if d < day)
    today = days.get(day, {})
    if not today:
        return {"available": False, "message": "no futures minutes today yet", "days": len(prev)}
    # running totals of the previous sessions, minute by minute (prefix sums — fast for 20 days × 375 minutes)
    def prefix(m: Dict[int, float]) -> List[float]:
        acc, run = [], 0.0
        for k in range(0, 376):
            run += m.get(k, 0.0)
            acc.append(run)
        return acc
    P = {d: prefix(days[d]) for d in prev[-20:]}
    o, _ = session_bounds(date.fromisoformat(day))
    tp = prefix(today)
    last_min = max(today)
    pts = []
    for m in range(0, last_min + 1):
        if m not in today:
            continue
        t = tp[m]
        base20 = [P[d][m] for d in prev[-20:]]
        base5 = [P[d][m] for d in prev[-5:]]
        c20 = t / (sum(base20) / len(base20)) if len(base20) >= C["min_days_20"] and sum(base20) > 0 else None
        c5 = t / (sum(base5) / len(base5)) if len(base5) >= C["min_days_5"] and sum(base5) > 0 else None
        cum_base20 = round(sum(base20) / len(base20)) if (len(base20) >= C["min_days_20"] and sum(base20) > 0) else None
        cum_base5 = round(sum(base5) / len(base5)) if (len(base5) >= C["min_days_5"] and sum(base5) > 0) else None
        pts.append({"ts": int(o + 60 * m), "m": m, "c20": round(c20, 2) if c20 else None, "c5": round(c5, 2) if c5 else None,
                    "cum": round(t), "cum_base": cum_base20 or cum_base5})
    return {"available": bool(pts), "day": day, "days": len(prev), "points": pts,
            "baseline": {"d20": min(20, len(prev)), "d5": min(5, len(prev))}}


def value(p: Dict[str, Any]) -> Optional[float]:
    """The headline number: 20-day when it exists, else 5-day."""
    return p.get("c20") if p.get("c20") is not None else p.get("c5")


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
    C = cfg()
    spike_thresh = float(C.get("tier_earthquake_spike", C.get("spike", 2.5)))
    above_thresh = float(C.get("tier_above_average", C.get("active", 1.5)))
    normal_thresh = float(C.get("tier_normal_min", 0.8))

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


def breakout_authenticity(slot_rvol: Optional[float], cum_rvol: Optional[float]) -> Dict[str, Any]:
    """
    Evaluates breakout authenticity based on Hedge-Fund 3-Pillar RVOL methodology:
    - Real breakout fuel: slot_rvol >= 1.8x OR cum_rvol >= 1.5x
    - Fakeout warning: slot_rvol < 0.9x (nobody behind the move)
    """
    C = cfg()
    real_min = float(C.get("breakout_real_min", 1.8))
    cum_min = float(C.get("active", 1.5))
    fakeout_max = float(C.get("breakout_fakeout_max", 0.9))

    if (slot_rvol is not None and slot_rvol >= real_min) or (cum_rvol is not None and cum_rvol >= cum_min):
        return {
            "is_real_breakout": True,
            "status": "REAL_BREAKOUT_FUEL",
            "verdict_text": "✅ REAL BREAKOUT FUEL — Institutional volume validates expansion",
        }
    elif (slot_rvol is not None and slot_rvol < fakeout_max) or (cum_rvol is not None and cum_rvol < fakeout_max):
        return {
            "is_real_breakout": False,
            "status": "FAKEOUT_WARNING",
            "verdict_text": "⚠️ LOW RVOL — FAKEOUT TRAP RISK (Nobody behind the move)",
        }
    return {
        "is_real_breakout": None,
        "status": "NEUTRAL_FLOW",
        "verdict_text": "Normal participation at level",
    }


def state(res: Dict[str, Any]) -> Dict[str, Any]:
    C = cfg()
    pts = [p for p in res.get("points", []) if value(p) is not None]
    if not pts:
        need = C["min_days_5"]
        return {"state": "UNKNOWN", "trend": None, "value": None,
                "text": f"cumulative RVOL needs ≥ {need} earlier sessions of futures volume (have {res.get('days', 0)})"}
    v = value(pts[-1])
    back = [p for p in pts if p["ts"] >= pts[-1]["ts"] - 60 * C["trend_minutes"]]
    d = v - value(back[0]) if back else 0.0
    trend = "BUILDING" if d >= C["trend_min_chg"] else "FADING" if d <= -C["trend_min_chg"] else "HOLDING"
    st = "ACTIVE" if v >= C["active"] else "ABSENT" if v < 1.0 else "NORMAL"
    base = "20-day" if pts[-1].get("c20") is not None else "5-day"
    words = {"ACTIVE": "NIFTY is IN PLAY — participation well above a normal day: levels matter, moves can travel",
             "NORMAL": "normal participation", "ABSENT": "below-normal participation — breakouts now happen in an empty room"}
    tier_info = classify_rvol(v)
    return {
        "state": st,
        "trend": trend,
        "value": round(v, 2),
        "c20": pts[-1].get("c20"),
        "c5": pts[-1].get("c5"),
        "base": base,
        "text": f"cRVOL {v:.2f}× ({base}) · {trend.lower()} — {words[st]}",
        "at": hm(pts[-1]["ts"]),
        "tier": tier_info["tier"],
        "tier_label": tier_info["label"],
        "tier_color": tier_info["color"],
    }


def per_bar(res: Dict[str, Any], tf: str, bar_ts: List[int]) -> Dict[int, Dict[str, Any]]:
    """cRVOL at each candle's CLOSE (its last minute) — what the chart histogram shows."""
    by = {p["ts"]: p for p in res.get("points", [])}
    step = TF_SECONDS.get(tf, 300)
    out = {}
    for t in bar_ts:
        for mm in range(t + step - 60, t - 60, -60):
            if mm in by:
                out[t] = by[mm]
                break
    return out


def events(res: Dict[str, Any], bar_rvol: Dict[int, Dict[str, Any]], breakouts: List[Dict[str, Any]], tf: str) -> List[Dict[str, Any]]:
    """Today's RVOL events, in time order. breakouts: [{ts (bar start), kind, dir, text}] of BOS / MSS / CRT TBS."""
    C = cfg()
    pts = [p for p in res.get("points", []) if value(p) is not None]
    out: List[Dict[str, Any]] = []
    above, run, was_active = False, 0, False
    for p in pts:
        v = value(p)
        if v >= C["active"]:
            run += 1
            if not above and run >= int(C["confirm_minutes"]):
                above = was_active = True
                out.append({"kind": "RVOL_ACTIVATED", "ts": p["ts"], "value": v,
                            "title": f"RVOL ACTIVATED {v:.2f}× — NIFTY is in play",
                            "body": f"{hm(p['ts'])}: futures volume so far is {v:.2f}× a normal day by this time "
                                    f"({'20' if p.get('c20') is not None else '5'}-day). Breakouts now have participation."})
        else:
            run = 0
            if above and v < C["fade_below"]:
                above = False
                out.append({"kind": "RVOL_FADING", "ts": p["ts"], "value": v,
                            "title": f"RVOL FADING {v:.2f}×", "body": f"{hm(p['ts'])}: participation dropped back toward "
                                                                    f"normal — be careful with new breakouts."})
    day = res.get("day")
    breakout_ts = {int(bo["ts"]) for bo in breakouts}
    last_spike_ts: Optional[int] = None
    last_spike_val: Optional[float] = None
    spike_cooldown_s = int(C.get("spike_cooldown_min", 15)) * 60

    for t, x in sorted(bar_rvol.items()):
        if day and ist_date(t).isoformat() != day:
            continue
        # Consolidate: if this candle already has a structural breakout, RVOL is merged into it
        if t in breakout_ts:
            continue

        r = x.get("slot_rvol") or x.get("rvol")
        if r is not None and r >= C["spike"]:
            # Cooldown check: prevent 9-candle continuous RVOL spike spam
            if last_spike_ts is not None and (t - last_spike_ts) < spike_cooldown_s:
                if last_spike_val is not None and r < 1.5 * last_spike_val and r < 4.0:
                    continue

            ist_dt = to_ist(t)
            is_eod = (ist_dt.hour == 15 and ist_dt.minute >= 15) or ist_dt.hour > 15
            vol_str = f" ({x.get('v', 0):,} FUT vol)" if x.get("v") else ""
            if is_eod:
                out.append({"kind": "RVOL_SPIKE", "ts": t, "value": r,
                            "title": f"RVOL SPIKE {r:.1f}× on {hm(t)} {tf} [EOD SETTLEMENT AUCTION — CLOSING VOLUME BURST]",
                            "body": f"EOD closing settlement / index adjustment volume ({r:.1f}× normal){vol_str}. Do not open fresh intraday options positions."})
            else:
                out.append({"kind": "RVOL_SPIKE", "ts": t, "value": r, "title": f"RVOL SPIKE {r:.1f}× on the {hm(t)} {tf} candle",
                            "body": f"That candle traded {r:.1f}× the normal volume for its time slot{vol_str} — a burst of "
                                    f"participation. Check WHERE it happened (level / OB / CRT edge) and who won the next candle."})
            last_spike_ts = t
            last_spike_val = r
    by = {p["ts"]: p for p in pts}
    step = TF_SECONDS.get(tf, 300)
    for bo in breakouts:
        t = int(bo["ts"])
        if day and ist_date(t).isoformat() != day:
            continue
        p = next((by[mm] for mm in range(t + step - 60, t - 60, -60) if mm in by), None)
        if not p:
            continue
        v = value(p)
        br = (bar_rvol.get(t) or {}).get("rvol")
        if v < 1.0:
            out.append({"kind": "BREAKOUT_NO_RVOL", "ts": t, "value": v,
                        "title": f"{bo['kind']} {bo.get('dir', '')} on LOW participation (cRVOL {v:.2f}×)",
                        "body": f"{hm(t)} {bo['text']} — but today's volume is BELOW normal. A breakout nobody "
                                f"joined fails more often: wait for a retest / confirmation, size down or skip."})
        elif v >= C["active"] and br is not None and br >= C["breakout_bar_rvol"]:
            ist_dt = to_ist(t)
            is_eod = (ist_dt.hour == 15 and ist_dt.minute >= 15) or ist_dt.hour > 15
            eod_suffix = " [EOD SETTLEMENT / NEXT-DAY BIAS ONLY — DO NOT OPEN INTRADAY OPTIONS]" if is_eod else ""
            if bo.get("extended"):
                rng = bo.get("range", 0)
                c = bo.get("close", 0)
                lvl = bo.get("level", 0)
                if bo.get("dir") == "bear":
                    retest_lo = min(lvl, lvl + 15)
                    retest_hi = max(lvl, lvl + 15)
                    sl = max(retest_hi + 15, bo.get("bar_h", lvl + 45))
                    title = f"⚡ DISPLACEMENT BREAKDOWN ({v:.2f}× RVOL) — Extended {rng:.0f}pt bar!{eod_suffix}"
                    body = f"{hm(t)}: DO NOT CHASE @ {c:,.1f}; Set Limit Entry on Retest of {retest_lo:,.0f}–{retest_hi:,.0f} (SL {sl:,.0f})."
                else:
                    retest_lo = min(lvl - 15, lvl)
                    retest_hi = max(lvl - 15, lvl)
                    sl = min(retest_lo - 15, bo.get("bar_l", lvl - 45))
                    title = f"⚡ DISPLACEMENT BREAKOUT ({v:.2f}× RVOL) — Extended {rng:.0f}pt bar!{eod_suffix}"
                    body = f"{hm(t)}: DO NOT CHASE @ {c:,.1f}; Set Limit Entry on Retest of {retest_lo:,.0f}–{retest_hi:,.0f} (SL {sl:,.0f})."
                out.append({"kind": "BREAKOUT_WITH_RVOL", "ts": t, "value": v, "title": title, "body": body})
            else:
                out.append({"kind": "BREAKOUT_WITH_RVOL", "ts": t, "value": v,
                            "title": f"{bo['kind']} {bo.get('dir', '')} CONFIRMED by RVOL ({v:.2f}×, candle {br:.1f}×){eod_suffix}",
                            "body": f"{hm(t)} {bo['text']} with strong participation — the move has attention."})
    out.sort(key=lambda e: e["ts"])
    for e in out:
        e["time"] = chart_time(e["ts"])
        e["at"] = hm(e["ts"])
        e["key"] = f"RVOL|{e['kind']}|{e['ts']}"
    return out
