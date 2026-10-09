"""
strikes/selector.py — the plugin seam:   select_strikes(snapshot) → {calls, puts, reasons, picks}

PSEUDOCODE
    ch      ← priyanka.chain_frame(snapshot.chain)                      # same columns as OC chain_now()
    ch      ← optional pre-filters (rules strikes.filters: |Δ| range, min OI, min ΔOI, min volume) — OFF by default,
              so the OC rule is untouched unless you switch one on
    levels  ← put wall, call wall, max pain, gamma flip, gamma pin, prev close, open    (the OC move-map levels)
    for side in CE, PE:
        move  ← fixed target pts (OC "Fixed pts")  or  |first level in the way − spot|   (OC "First level", path_levels)
        sc    ← priyanka.scenarios(ch, spot, side, move, stop, hold_min, ΔIV win, ΔIV loss)
        best, fast ← priyanka.pick(sc)                                   # MJ course pick + fast pick, unchanged
        reasons += "22400 CE selected: Δ 0.62 · ΔOI +4.1 L · spread 0.4 % · vol 12.3 L · R:R 1.80 · fit 1.62 …"
    return calls=[best CE, fast CE], puts=[best PE, fast PE]

StrikeBook adds the ONLY behaviour the OC tab does not have — stability for live use:
    scores refresh on every chain update (~60 s, no extra API calls), but the ACTIVE strike may switch only on a
    3-minute candle close and only when the new best beats the current one by `switch_min_improve` (or the current
    one stops qualifying). A manual pick is pinned until you clear it.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from .. import config
from ..market.instruments import Instrument, option
from ..timeutil import bucket_start, hm
from . import priyanka as pk


def _rules() -> Dict[str, Any]:
    return config.rules().get("strikes", {})


def _lk(x: float) -> str:
    a = abs(x)
    s = f"{a / 1e7:.2f} Cr" if a >= 1e7 else f"{a / 1e5:.1f} L" if a >= 1e5 else f"{a / 1e3:.1f} K" if a >= 1e3 else f"{a:.0f}"
    return ("−" if x < 0 else "+") + s


def levels_from(snapshot: Dict[str, Any], ch_df) -> List[tuple]:
    """The OC move-map levels this app can see (name, price, colour)."""
    ch = snapshot["chain"]
    spot = float(snapshot["spot"])
    lv = []
    if ch.get("put_wall"):
        lv.append(("Put wall", float(ch["put_wall"]), "green"))
    if ch.get("call_wall"):
        lv.append(("Call wall", float(ch["call_wall"]), "red"))
    if ch.get("max_pain"):
        lv.append(("Max pain", float(ch["max_pain"]), "orange"))
    g = pk.gex(ch_df, spot, int(config.rule("paper", "lot_size", 65)))
    if g is not None:
        fl = pk.gamma_flip(g)
        if fl:
            lv.append(("Gamma flip", float(fl), "purple"))
        lv.append(("Gamma pin", float(g.loc[g["abs_gex"].idxmax(), "strike"]), "blue"))
    if snapshot.get("prev_close"):
        lv.append(("Prev close", float(snapshot["prev_close"]), "muted"))
    if snapshot.get("open"):
        lv.append(("Open", float(snapshot["open"]), "gold"))
    return lv


def _prefilter(ch, side: str, F: Dict[str, Any]):
    p = side.lower()
    m = np.ones(len(ch), dtype=bool)
    dr = F.get(f"delta_range_{p}")
    if dr:
        d = ch[f"{p}_delta"]
        m &= (d >= min(dr)).to_numpy() & (d <= max(dr)).to_numpy()
    if F.get("min_oi"):
        m &= (ch[f"{p}_oi"] >= float(F["min_oi"])).to_numpy()
    if F.get("min_oi_chg") is not None:
        m &= (ch[f"{p}_chg_oi"] >= float(F["min_oi_chg"])).to_numpy()
    if F.get("min_volume"):
        m &= (ch[f"{p}_vol"] >= float(F["min_volume"])).to_numpy()
    return ch[m]


def _why(side: str, label: str, r, row) -> str:
    sp = f"{r.spread:.1f} %" if _fin(r.spread) else "—"
    return (f"{int(r.strike)} {side} {label}: Δ {r.delta:+.2f} · {r.kind} {abs(r.itm):,.0f} · premium {r.ltp:,.1f} · "
            f"ΔOI {_lk(float(row.get(f'{side.lower()}_chg_oi', 0) or 0))} · spread {sp} · "
            f"vol {_lk(float(row.get(f'{side.lower()}_vol', 0) or 0)).lstrip('+')} · R:R {r.rr:,.2f}"
            + (f" · fit {r.fit:,.2f}" if "fit" in r and _fin(r.fit) else f" · return {r.ret_pct:+.0f} %"))


def _fin(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _scores(sc, P: Dict[str, Any], exp: str, side: str) -> Dict[str, float]:
    """Course-pick score (pick()'s `fit`) for EVERY strike that pick() would consider for the best course pick —
    so StrikeBook can compare the strike it holds against the new best, not just the top two."""
    if sc is None or sc.empty:
        return {}
    P = {**pk.DEFAULTS, **P}
    ok = sc[(sc.spread.fillna(0) <= P["max_spread_pct"]) & (sc.win > 0)].copy()
    ok["fit"] = ok.rr * (1 - (ok.ltp - P["course_premium"]).abs() / P["course_premium_span"]).clip(P["fit_floor"], 1)
    pool = ok[ok.course] if ok.course.any() else ok[ok.kind == "ITM"]
    return {option(exp, k, side).key: round(float(f), 4) for k, f in zip(pool.strike, pool.fit) if _fin(f)}


def select_strikes(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """snapshot = {expiry, spot, chain (ICT normalized), open?, prev_close?}. Pure: same snapshot → same answer."""
    R = _rules()
    P = {k: v for k, v in R.get("priyanka", {}).items() if not k.startswith("_")}
    F = R.get("filters", {})
    spot, exp = float(snapshot["spot"]), snapshot["expiry"]
    ch = pk.chain_frame(snapshot["chain"])
    out: Dict[str, Any] = {"expiry": exp, "spot": spot, "calls": [], "puts": [], "reasons": [], "picks": {},
                           "scores": {"CE": {}, "PE": {}}}
    if ch.empty or not spot:
        out["reasons"].append("No option chain for this expiry yet.")
        return out
    lv = levels_from(snapshot, ch)
    reach = float(R.get("reach_fallback_pts", 60.0))
    stop, mins = float(R.get("stop_pts", 40)), float(R.get("hold_min", 30))
    dw, dl = float(R.get("iv_change_win", 0.0)), float(R.get("iv_change_loss", -0.5))
    rows = {int(r.strike): r for r in ch.itertuples(index=False)}
    for side, key in (("CE", "calls"), ("PE", "puts")):
        if R.get("target_mode", "fixed") == "first_level":
            tgt, why_t, _ = pk.path_levels(lv, spot, side, reach)
            move = max(5.0, abs(tgt - spot))
            tline = f"target {tgt:,.0f} ({why_t})"
        else:
            move = float(R.get("target_pts", 40))
            tline = f"fixed target {move:.0f} pts"
        sc = pk.scenarios(_prefilter(ch, side, F), spot, side, move, stop, mins, dw, dl,
                          float(P.get("window_pts", 300)), P)
        best, fast = pk.pick(sc, P)
        out["picks"][side] = {"move": move, "target": tline, "n_strikes": int(len(sc))}
        out["scores"][side] = _scores(sc, P, exp, side)
        for label, r in (("best course pick", best), ("fast pick", fast)):
            if r is None:
                out["reasons"].append(f"{side} {label}: no strike passes (liquidity / premium / filters).")
                continue
            row = rows.get(int(r.strike))
            sid = getattr(row, f"{side.lower()}_sid", None) if row is not None else None
            inst = option(exp, r.strike, side, sid)
            rec = {"instrument": inst.to_dict(), "role": "best" if label.startswith("best") else "fast",
                   "ltp": round(float(r.ltp), 2), "delta": round(float(r.delta), 3), "gamma": float(r.gamma),
                   "theta": float(r.theta), "vega": float(r.vega), "iv": float(r.iv), "oi": float(r.oi),
                   "itm": float(r.itm), "kind": str(r.kind), "rr": round(float(r.rr), 2),
                   "fit": round(float(r.fit), 3) if "fit" in r and _fin(r.fit) else None,
                   "ret_pct": round(float(r.ret_pct), 1), "spread": round(float(r.spread), 2) if _fin(r.spread) else None,
                   "prem_tgt": round(float(r.prem_tgt), 2), "prem_sl": round(float(r.prem_sl), 2),
                   "course": bool(r.course), "why": _why(side, label, r, row._asdict() if row is not None else {})}
            out[key].append(rec)
            out["reasons"].append(rec["why"] + f" · {tline}, stop {stop:.0f} pts, hold {mins:.0f} min")
    return out


class StrikeBook:
    """Live selection with stability. state: {CE: rec | None, PE: rec | None, manual: {CE, PE}, at: bucket ts}."""

    def __init__(self) -> None:
        self.active: Dict[str, Optional[Dict[str, Any]]] = {"CE": None, "PE": None}
        self.manual: Dict[str, Optional[Dict[str, Any]]] = {"CE": None, "PE": None}
        self.last: Optional[Dict[str, Any]] = None
        self.switched_bucket: Dict[str, int] = {"CE": 0, "PE": 0}
        self.log: List[str] = []

    def update(self, result: Dict[str, Any], now: float, force: bool = False) -> List[str]:
        """Feed a fresh select_strikes() result. Returns human-readable switch lines."""
        R = _rules()
        self.last = result
        sw_tf = R.get("switch_tf", "3m")
        imp = float(R.get("switch_min_improve", 0.15))
        b = bucket_start(now, sw_tf)
        notes = []
        for side, key in (("CE", "calls"), ("PE", "puts")):
            if self.manual[side]:
                continue
            cand = next((r for r in result[key] if r["role"] == "best"), None) or (result[key][0] if result[key] else None)
            cur = self.active[side]
            if cand is None:
                continue
            if cur is None or force:
                self.active[side] = cand
                self.switched_bucket[side] = b
                notes.append(f"{hm(now)} {cand['instrument']['label']} selected — {cand['why']}")
                continue
            if cand["instrument"]["key"] == cur["instrument"]["key"]:
                self.active[side] = cand                         # same strike, fresher numbers
                continue
            scores = (result.get("scores") or {}).get(side, {})
            old = scores.get(cur["instrument"]["key"])            # None = no longer passes the course rule
            new = scores.get(cand["instrument"]["key"], cand.get("fit") or 0)
            better = old is None or new > old * (1 + imp)
            if better and b != self.switched_bucket[side] and _bucket_closed(now, sw_tf):
                self.active[side] = cand
                self.switched_bucket[side] = b
                notes.append(f"{hm(now)} {side} switched {cur['instrument']['label']} → {cand['instrument']['label']}"
                             f" ({'old strike no longer passes the course rule' if old is None else f'score {new:.2f} vs {old:.2f}'})")
        self.log = (self.log + notes)[-30:]
        return notes

    def set_manual(self, side: str, rec: Optional[Dict[str, Any]]) -> None:
        self.manual[side] = rec
        if rec:
            self.active[side] = rec

    def current(self) -> Dict[str, Optional[Dict[str, Any]]]:
        return {s: (self.manual[s] or self.active[s]) for s in ("CE", "PE")}

    def instruments(self) -> List[Instrument]:
        out = []
        for s, rec in self.current().items():
            if rec:
                i = rec["instrument"]
                out.append(option(i["expiry"], i["strike"], i["right"], i.get("security_id")))
        return out


def _bucket_closed(now: float, tf: str) -> bool:
    """True in the first minute after a switch-tf candle closed (selection is evaluated on that close)."""
    from ..timeutil import TF_SECONDS
    return (now - bucket_start(now, tf)) < 60 or TF_SECONDS.get(tf, 180) <= 60
