"""
api/option_tape_api.py — REAL CE/PE option tape (volume + OI + greek delta) for the ATM±ITM buckets of the
current-week and next-week expiries. Read-only & additive. Foundation for option RVOL / big-trades / heatmap /
footprint that use ACTUAL option volume instead of the futures estimate.

Data sources (all already collected — no new Dhan connection):
  * live chain in memory (h.chain.chains[exp])  → current ladder: per-strike ce/pe {vol (cum day), oi, oi_chg, ltp,
                                                   greek delta (Dhan-supplied, REAL), build/unwind class}
  * chain_snap table (per-minute, ~5 days)      → per-minute option VOLUME series (diff of cumulative vol) for RVOL

HONEST LIMITS:
  * Per-minute option volume history has NO backfill — it starts the minute this build's recorder first runs, so
    option cumulative-RVOL reads UNKNOWN until a few earlier sessions have been banked (same as the futures tape).
  * The chain is a SNAPSHOT (~6s/expiry), not a trade stream, so order-flow aggression (buy/sell delta, CVD) can
    only be ESTIMATED later; it is labelled estimated. Greek delta here is Dhan's own value and is real.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from .. import db
from ..engine import rvol as rvol_mod
from ..market.chain import build_class
from ..timeutil import ist_date

router = APIRouter()


def _tag(strike: float, spot: float, atm: float, step: float, band: int) -> Optional[str]:
    """Keep only ATM±`band` strikes; tag each by moneyness. CE is ITM below spot, PE ITM above."""
    if step <= 0 or abs(strike - atm) > band * step:
        return None
    if abs(strike - atm) < step / 2:
        return "ATM"
    return "below" if strike < spot else "above"      # below spot: CE-ITM / PE-OTM; above: PE-ITM / CE-OTM


def _minute_vol_series(exp: str, strikes: List[float], since: float) -> List[Dict[str, float]]:
    """Total option volume per minute across the selected strikes (CE+PE), from chain_snap cumulative-vol diffs.
    Cumulative vol resets each session, so diffs are taken within a day only; the first minute of each day in the
    data is skipped (no prior point to diff → no backfill)."""
    rows = db.query(
        "SELECT ts, strike, ce_vol, pe_vol FROM chain_snap WHERE expiry=? AND ts>=? "
        "AND (ce_vol IS NOT NULL OR pe_vol IS NOT NULL) ORDER BY strike, ts",
        (exp, int(since)))
    keep = set(round(s) for s in strikes)
    per_min: Dict[int, float] = {}
    prev_cum: Dict[float, Any] = {}           # strike → (day, cum) of previous row for that strike
    for r in rows:
        strike = r["strike"]
        if round(strike) not in keep:
            continue
        ts = int(r["ts"])
        day = ts // 86400
        cum = float(r["ce_vol"] or 0) + float(r["pe_vol"] or 0)
        pd = prev_cum.get(strike)
        if pd and pd[0] == day and cum >= pd[1]:
            per_min[ts] = per_min.get(ts, 0.0) + (cum - pd[1])
        prev_cum[strike] = (day, cum)
    return [{"ts": t, "v": v} for t, v in sorted(per_min.items())]


def _heat_matrix(exp: str, strikes: List[float], since: float) -> Dict[str, List[Dict[str, float]]]:
    """Per-strike option volume (CE+PE) per minute for TODAY only → {strike: [{ts, v}, ...]}. For the strike×time
    heatmap. Same within-day diff of cumulative vol as the RVOL series; empty until the tape has recorded today."""
    rows = db.query(
        "SELECT ts, strike, ce_vol, pe_vol FROM chain_snap WHERE expiry=? AND ts>=? "
        "AND (ce_vol IS NOT NULL OR pe_vol IS NOT NULL) ORDER BY strike, ts",
        (exp, int(since)))
    keep = set(round(s) for s in strikes)
    out: Dict[str, List[Dict[str, float]]] = {}
    prev: Dict[float, Any] = {}
    today = max((int(r["ts"]) // 86400 for r in rows), default=None)
    for r in rows:
        strike = r["strike"]
        if round(strike) not in keep:
            continue
        ts = int(r["ts"])
        day = ts // 86400
        cum = float(r["ce_vol"] or 0) + float(r["pe_vol"] or 0)
        pd = prev.get(strike)
        if day == today and pd and pd[0] == day and cum >= pd[1]:
            out.setdefault(str(round(strike)), []).append({"ts": ts, "v": round(cum - pd[1])})
        prev[strike] = (day, cum)
    return out


def _classify(ltp, prev_ltp, prev_bid, prev_ask, last_side):
    """Lee-Ready quote rule, else tick rule. Returns 'buy' / 'sell' / last_side. Coarse at 1-min snapshot cadence."""
    if prev_ask and ltp >= prev_ask:
        return "buy"
    if prev_bid and ltp <= prev_bid:
        return "sell"
    if prev_ltp is not None:
        if ltp > prev_ltp:
            return "buy"
        if ltp < prev_ltp:
            return "sell"
    return last_side


def _aggression(exp: str, strikes: List[float], since: float, greeks: Dict[float, Dict[str, float]]):
    """ESTIMATED option buy/sell delta & CVD per strike for TODAY, via Lee-Ready on consecutive minute snapshots.
    Also a delta-weighted net directional flow (CE +δ, PE −δ) in futures-equivalent contracts. All estimated."""
    rows = db.query(
        "SELECT ts, strike, ce_ltp, pe_ltp, ce_vol, pe_vol, ce_bid, ce_ask, pe_bid, pe_ask "
        "FROM chain_snap WHERE expiry=? AND ts>=? AND (ce_vol IS NOT NULL OR pe_vol IS NOT NULL) "
        "ORDER BY strike, ts", (exp, int(since)))
    keep = set(round(s) for s in strikes)
    today = max((int(r["ts"]) // 86400 for r in rows), default=None)
    prev: Dict[float, Any] = {}                 # strike → prev row fields per side
    per: Dict[float, Dict[str, float]] = {}     # strike → {ce_buy,ce_sell,pe_buy,pe_sell}
    cvd_min: Dict[int, Dict[str, float]] = {}   # ts → {ce,pe,net} minute delta (summed across strikes)
    last_side = {"ce": None, "pe": None}
    for r in rows:
        s = r["strike"]
        if round(s) not in keep:
            continue
        ts = int(r["ts"]); day = ts // 86400
        cur = {"ce_ltp": r["ce_ltp"], "pe_ltp": r["pe_ltp"], "ce_vol": r["ce_vol"] or 0, "pe_vol": r["pe_vol"] or 0,
               "ce_bid": r["ce_bid"], "ce_ask": r["ce_ask"], "pe_bid": r["pe_bid"], "pe_ask": r["pe_ask"], "day": day}
        pd = prev.get(s)
        if day == today and pd and pd["day"] == day:
            d = per.setdefault(s, {"ce_buy": 0.0, "ce_sell": 0.0, "pe_buy": 0.0, "pe_sell": 0.0})
            cm = cvd_min.setdefault(ts, {"ce": 0.0, "pe": 0.0, "net": 0.0})
            for side in ("ce", "pe"):
                dv = (cur[f"{side}_vol"]) - (pd[f"{side}_vol"])
                if dv <= 0:
                    continue
                sd = _classify(cur[f"{side}_ltp"], pd[f"{side}_ltp"], pd[f"{side}_bid"], pd[f"{side}_ask"], last_side[side])
                last_side[side] = sd
                d[f"{side}_buy" if sd == "buy" else f"{side}_sell"] += dv
                signed = dv if sd == "buy" else -dv
                cm[side] += signed
                g = (greeks.get(s, {}) or {}).get(side) or 0.0
                cm["net"] += signed * g            # delta-weighted, futures-equivalent (CE +δ, PE −δ)
        prev[s] = cur

    ladder = []
    for s in sorted(per):
        d = per[s]
        ce_delta = d["ce_buy"] - d["ce_sell"]
        pe_delta = d["pe_buy"] - d["pe_sell"]
        ladder.append({"strike": s, "ce_buy": round(d["ce_buy"]), "ce_sell": round(d["ce_sell"]),
                       "ce_delta": round(ce_delta), "pe_buy": round(d["pe_buy"]), "pe_sell": round(d["pe_sell"]),
                       "pe_delta": round(pe_delta)})
    # cumulative CVD series
    cvd, run = [], {"ce": 0.0, "pe": 0.0, "net": 0.0}
    for ts in sorted(cvd_min):
        for k in run:
            run[k] += cvd_min[ts][k]
        cvd.append({"ts": ts, "ce": round(run["ce"]), "pe": round(run["pe"]), "net": round(run["net"])})
    net_flow = round(run["net"])
    return {"ladder": ladder, "cvd": cvd, "net_flow": net_flow,
            "totals": {"ce_delta": round(sum(x["ce_delta"] for x in ladder)),
                       "pe_delta": round(sum(x["pe_delta"] for x in ladder))}}


_TF = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def _time_footprint(exp: str, strikes: List[float], since: float, tf_secs: int, greeks: Dict[float, Dict[str, float]]):
    """Candle-by-candle option footprint for TODAY: time buckets × strikes, each with CE/PE volume and ESTIMATED
    buy/sell (Lee-Ready on consecutive minute snapshots). Per bucket: POC strike, net delta-weighted flow, CVD."""
    rows = db.query(
        "SELECT ts, strike, ce_ltp, pe_ltp, ce_vol, pe_vol, ce_bid, ce_ask, pe_bid, pe_ask "
        "FROM chain_snap WHERE expiry=? AND ts>=? AND (ce_vol IS NOT NULL OR pe_vol IS NOT NULL) "
        "ORDER BY strike, ts", (exp, int(since)))
    keep = set(round(s) for s in strikes)
    today = max((int(r["ts"]) // 86400 for r in rows), default=None)
    prev: Dict[float, Any] = {}
    last_side = {"ce": None, "pe": None}
    # cells[bucket][strike] = {ce_vol,pe_vol,ce_buy,ce_sell,pe_buy,pe_sell}
    cells: Dict[int, Dict[float, Dict[str, float]]] = {}
    for r in rows:
        s = r["strike"]
        if round(s) not in keep:
            continue
        ts = int(r["ts"]); day = ts // 86400
        cur = {"ce_ltp": r["ce_ltp"], "pe_ltp": r["pe_ltp"], "ce_vol": r["ce_vol"] or 0, "pe_vol": r["pe_vol"] or 0,
               "ce_bid": r["ce_bid"], "ce_ask": r["ce_ask"], "pe_bid": r["pe_bid"], "pe_ask": r["pe_ask"], "day": day}
        pd = prev.get(s)
        if day == today and pd and pd["day"] == day:
            b = ts // tf_secs * tf_secs
            cell = cells.setdefault(b, {}).setdefault(s, {"ce_vol": 0.0, "pe_vol": 0.0, "ce_buy": 0.0, "ce_sell": 0.0, "pe_buy": 0.0, "pe_sell": 0.0})
            for side in ("ce", "pe"):
                dv = cur[f"{side}_vol"] - pd[f"{side}_vol"]
                if dv <= 0:
                    continue
                cell[f"{side}_vol"] += dv
                sd = _classify(cur[f"{side}_ltp"], pd[f"{side}_ltp"], pd[f"{side}_bid"], pd[f"{side}_ask"], last_side[side])
                last_side[side] = sd
                cell[f"{side}_buy" if sd == "buy" else f"{side}_sell"] += dv
        prev[s] = cur

    out, cvd = [], 0.0
    for b in sorted(cells):
        cmap = cells[b]
        poc = max(cmap, key=lambda s: cmap[s]["ce_vol"] + cmap[s]["pe_vol"], default=None)
        net, bvol = 0.0, 0.0
        cell_list = []
        for s in sorted(cmap):
            c = cmap[s]
            ce_d = c["ce_buy"] - c["ce_sell"]
            pe_d = c["pe_buy"] - c["pe_sell"]
            bvol += c["ce_vol"] + c["pe_vol"]
            g = greeks.get(s, {}) or {}
            net += ce_d * (g.get("ce") or 0.0) + pe_d * (g.get("pe") or 0.0)   # delta-weighted, futures-equiv
            cell_list.append({"strike": s, "ce_vol": round(c["ce_vol"]), "pe_vol": round(c["pe_vol"]),
                              "ce_delta": round(ce_d), "pe_delta": round(pe_d)})
        cvd += net
        out.append({"ts": b, "poc": poc, "net_delta": round(net), "cvd": round(cvd), "vol": round(bvol), "cells": cell_list})
    # per-bucket intraday RVOL = bucket vol ÷ running mean of bucket vols so far (no multi-day baseline exists yet)
    run_sum = 0.0
    for i, bk in enumerate(out):
        run_sum += bk["vol"]
        mean = run_sum / (i + 1)
        bk["rvol"] = round(bk["vol"] / mean, 2) if mean > 0 else None
    return out


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


@router.get("/api/option_bigtrades")
def option_bigtrades(which: str = Query("cur"), band: int = Query(10), mult: float = Query(3.0)):
    """Unusual option volume bursts TODAY: per-minute per-strike volume ≥ `mult`× that strike-side's own median
    minute (the honest option 'big trades' — a participation burst, not a tagged print). Estimated buy/sell side."""
    from .main import _h
    h = _h()
    try:
        exps = list(getattr(h.chain, "expiries", []) or [])
        if not exps:
            return {"available": False, "error": "no expiries yet", "events": []}
        exp = exps[0] if which != "next" else (exps[1] if len(exps) > 1 else exps[0])
        ch = h.chain.chains.get(exp)
        if not ch or not ch.get("rows"):
            return {"available": False, "error": "no chain rows", "events": []}
        spot, atm, step = ch.get("spot") or 0, ch.get("atm") or 0, ch.get("step") or 50.0
        keep = [r["strike"] for r in ch["rows"] if _tag(r["strike"], spot, atm, step, band) is not None]
        keepset = set(round(s) for s in keep)
        since = (ch.get("ts", 0) or 0) - 2 * 86400
        rows = db.query(
            "SELECT ts, strike, ce_ltp, pe_ltp, ce_vol, pe_vol, ce_bid, ce_ask, pe_bid, pe_ask "
            "FROM chain_snap WHERE expiry=? AND ts>=? AND (ce_vol IS NOT NULL OR pe_vol IS NOT NULL) "
            "ORDER BY strike, ts", (exp, int(since)))
        today = max((int(r["ts"]) // 86400 for r in rows), default=None)
        prev: Dict[float, Any] = {}
        last_side = {"ce": None, "pe": None}
        # per strike-side: list of (ts, vol, est_side)
        mins: Dict[tuple, List[tuple]] = {}
        for r in rows:
            s = r["strike"]
            if round(s) not in keepset:
                continue
            ts = int(r["ts"]); day = ts // 86400
            cur = {"ce_ltp": r["ce_ltp"], "pe_ltp": r["pe_ltp"], "ce_vol": r["ce_vol"] or 0, "pe_vol": r["pe_vol"] or 0,
                   "ce_bid": r["ce_bid"], "ce_ask": r["ce_ask"], "pe_bid": r["pe_bid"], "pe_ask": r["pe_ask"], "day": day}
            pd = prev.get(s)
            if day == today and pd and pd["day"] == day:
                for side in ("ce", "pe"):
                    dv = cur[f"{side}_vol"] - pd[f"{side}_vol"]
                    if dv <= 0:
                        continue
                    sd = _classify(cur[f"{side}_ltp"], pd[f"{side}_ltp"], pd[f"{side}_bid"], pd[f"{side}_ask"], last_side[side])
                    last_side[side] = sd
                    mins.setdefault((s, side), []).append((ts, dv, sd))
            prev[s] = cur

        events = []
        for (s, side), lst in mins.items():
            vols = [v for _, v, _ in lst]
            base = _median(vols)
            if base <= 0:
                continue
            for ts, v, sd in lst:
                m = v / base
                if m >= mult and v > base:
                    events.append({"ts": ts, "strike": s, "side": side.upper(), "vol": round(v),
                                   "mult": round(m, 1), "est": sd})
        events.sort(key=lambda e: -e["ts"])
        return {"available": True, "expiry": exp, "mult": mult, "events": events[:60],
                "note": "ESTIMATED: a burst is a minute whose option volume ≥ mult× that strike-side's median minute "
                        "today (no backfill). Buy/sell is Lee-Ready snapshot, not a tagged print."}
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "events": []}


@router.get("/api/option_footprint")
def option_footprint(which: str = Query("cur"), band: int = Query(6), tf: str = Query("5m")):
    """Time-based (candle-by-candle) option footprint. which=cur|next (single expiry), band strikes each side, tf bucket."""
    from .main import _h
    h = _h()
    try:
        exps = list(getattr(h.chain, "expiries", []) or [])
        if not exps:
            return {"available": False, "error": "no expiries yet", "buckets": []}
        exp = exps[0] if which != "next" else (exps[1] if len(exps) > 1 else exps[0])
        ch = h.chain.chains.get(exp)
        if not ch or not ch.get("rows"):
            return {"available": False, "error": "no chain rows", "buckets": []}
        spot, atm, step = ch.get("spot") or 0, ch.get("atm") or 0, ch.get("step") or 50.0
        strikes, greeks = [], {}
        for r in ch["rows"]:
            if _tag(r["strike"], spot, atm, step, band) is None:
                continue
            strikes.append(r["strike"])
            greeks[r["strike"]] = {"ce": r["ce"].get("delta") or 0.0, "pe": r["pe"].get("delta") or 0.0}
        tf_secs = _TF.get(tf, 300)
        since = (ch.get("ts", 0) or 0) - 2 * 86400
        buckets = _time_footprint(exp, strikes, since, tf_secs, greeks)
        return {"available": True, "expiry": exp, "spot": spot, "atm": atm, "step": step, "tf": tf,
                "strikes": sorted(set(round(s) for s in strikes), reverse=True), "buckets": buckets,
                "note": "ESTIMATED: per-strike buy/sell inferred via Lee-Ready on 1-min snapshots (not a trade feed); "
                        "no backfill, so columns start when recording began. Net delta is delta-weighted (futures-equiv)."}
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "buckets": []}


@router.get("/api/option_tape")
def option_tape(which: str = Query("both"), band: int = Query(6), heat: int = Query(0), flow: int = Query(0)):
    """which = cur | next | both. band = strikes each side of ATM to keep (ATM + ITM/OTM buckets)."""
    from .main import _h
    h = _h()
    try:
        exps: List[str] = list(getattr(h.chain, "expiries", []) or [])
        if not exps:
            return {"available": False, "error": "no expiries loaded yet", "ladders": []}
        chosen = exps[:1] if which == "cur" else exps[1:2] if which == "next" else exps[:2]
        days_back = int(14 * 86400)
        since = (h.chain.chains.get(chosen[0], {}).get("ts", 0) or 0) - days_back

        ladders, all_strikes_by_exp, greeks_by_exp = [], {}, {}
        for exp in chosen:
            ch = h.chain.chains.get(exp)
            if not ch or not ch.get("rows"):
                continue
            spot, atm, step = ch.get("spot") or 0, ch.get("atm") or 0, ch.get("step") or 50.0
            ladder, kept, gmap = [], [], {}
            for r in ch["rows"]:
                t = _tag(r["strike"], spot, atm, step, band)
                if t is None:
                    continue
                kept.append(r["strike"])
                gmap[r["strike"]] = {"ce": r["ce"].get("delta") or 0.0, "pe": r["pe"].get("delta") or 0.0}
                ce, pe = r["ce"], r["pe"]
                ladder.append({
                    "strike": r["strike"], "zone": t,
                    "ce": {"vol": ce.get("vol") or 0, "oi": ce.get("oi") or 0, "oi_chg": ce.get("oi_chg") or 0,
                           "ltp": ce.get("ltp") or 0, "delta": ce.get("delta"),
                           "class": build_class(ce.get("chg") or 0, ce.get("oi_chg") or 0)},
                    "pe": {"vol": pe.get("vol") or 0, "oi": pe.get("oi") or 0, "oi_chg": pe.get("oi_chg") or 0,
                           "ltp": pe.get("ltp") or 0, "delta": pe.get("delta"),
                           "class": build_class(pe.get("chg") or 0, pe.get("oi_chg") or 0)},
                })
            ladder.sort(key=lambda x: x["strike"])
            ce_vol = sum(x["ce"]["vol"] for x in ladder)
            pe_vol = sum(x["pe"]["vol"] for x in ladder)
            ce_oi = sum(x["ce"]["oi"] for x in ladder)
            pe_oi = sum(x["pe"]["oi"] for x in ladder)
            all_strikes_by_exp[exp] = kept
            greeks_by_exp[exp] = gmap
            ladders.append({
                "expiry": exp, "spot": spot, "atm": atm, "step": step, "ladder": ladder,
                "totals": {"ce_vol": round(ce_vol), "pe_vol": round(pe_vol),
                           "vol_pcr": round(pe_vol / ce_vol, 2) if ce_vol else None,
                           "ce_oi": round(ce_oi), "pe_oi": round(pe_oi),
                           "oi_pcr": round(pe_oi / ce_oi, 2) if ce_oi else None},
            })

        # Option cumulative RVOL on the combined selected buckets (real where baseline exists, else UNKNOWN)
        combined_series: Dict[int, float] = {}
        rec_minutes, rec_first = 0, None
        for exp in chosen:
            s = _minute_vol_series(exp, all_strikes_by_exp.get(exp, []), since)
            for p in s:
                combined_series[p["ts"]] = combined_series.get(p["ts"], 0.0) + p["v"]
        series = [{"ts": t, "v": v} for t, v in sorted(combined_series.items())]
        if series:
            today = max(int(p["ts"]) // 86400 for p in series)
            today_pts = [p for p in series if int(p["ts"]) // 86400 == today]
            rec_minutes = len(today_pts)
            rec_first = min((int(p["ts"]) for p in today_pts), default=None)
        crvol = None
        try:
            st = rvol_mod.state(rvol_mod.cumulative(series))
            crvol = {"value": st.get("value"), "state": st.get("state"), "trend": st.get("trend"),
                     "text": st.get("text")}
        except Exception:                                    # noqa: BLE001
            crvol = None

        heatmap = None
        if heat:
            heatmap = {}
            for exp in chosen:
                heatmap[exp] = _heat_matrix(exp, all_strikes_by_exp.get(exp, []), since)

        aggression = None
        if flow:
            aggression = {}
            net_total = 0.0
            for exp in chosen:
                a = _aggression(exp, all_strikes_by_exp.get(exp, []), since, greeks_by_exp.get(exp, {}))
                aggression[exp] = a
                net_total += a.get("net_flow") or 0
            aggression["_net_flow_total"] = round(net_total)
            aggression["_note"] = ("ESTIMATED: option aggression is inferred with the Lee-Ready quote rule on "
                                   "1-minute snapshots (not a trade feed), and net flow is delta-weighted using "
                                   "Dhan greeks (0 for ITM calls, so those are understated). Use as a lean, not a tape.")

        return {"available": True, "which": which, "expiries": chosen, "ladders": ladders,
                "option_crvol": crvol, "heatmap": heatmap, "aggression": aggression,
                "recording": {"minutes_today": rec_minutes, "first_ts": rec_first,
                              "note": "Option volume tape has no backfill — cumulative RVOL needs a few earlier "
                                      "sessions before it reads; today shows real accumulating volume."}}
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "ladders": []}
