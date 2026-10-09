"""
strikes/priyanka.py — the OC "🧭 Strike Advisor" decision logic, carried over from strike_advisor_pro.py
(OC folder) WITHOUT changing the rule. Source functions:

    strike_advisor_pro.chain_now      → chain_frame (only the input adapter differs: ICT chain dict → same columns)
    strike_advisor_pro.scenarios      → scenarios   (identical maths; constants exposed as parameters)
    strike_advisor_pro.pick           → pick        (identical; MJ course constants exposed as parameters)
    strike_advisor_pro.path_levels    → path_levels (identical)
    options_lab_tab.gamma_flip        → gamma_flip  (identical)

    premium change ≈ Δ·move + ½·Γ·move² + θ·(minutes/375) + ν·ΔIV   (first-order Greeks scenario, per strike)
    BEST COURSE PICK  MJ: ITM 50–250 pts, strike multiple of 100, premium 120–320 (≈ ₹200), spread ≤ 3 %, best fit
                      fit = R:R × (1 − |premium − 200| / 400) clipped to 0.2…1, tie-break by OI
    FAST PICK         ATM … 100 pts OTM, best % return on premium

Every default equals the OC value, so with rules.json untouched the picks are the same as the OC tab's
(pinned by tests/test_strikes.py against a verbatim copy of the OC functions).
NOT carried over: olab_pro.move_map (straddle σ / rest-of-day move) and the olab_pro side grade — that file was not
provided. The ICT playbook decides the side; the target uses `path_levels` over the levels this app has
(walls, max pain, gamma flip / pin, previous close, open) with the OC fallback reach.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

DEFAULTS: Dict[str, Any] = {
    "window_pts": 300, "min_ltp": 1.0, "max_spread_pct": 3.0,
    "course_strike_multiple": 100, "course_itm_min": 50, "course_itm_max": 250,
    "course_ltp_min": 120, "course_ltp_max": 320, "course_premium": 200, "course_premium_span": 400,
    "fit_floor": 0.2, "fast_itm_min": -100, "fast_itm_max": 25, "kind_band_pts": 25,
}


def chain_frame(ch: Dict[str, Any]) -> pd.DataFrame:
    """ICT normalized chain → the column layout strike_advisor_pro.chain_now() returns."""
    rows = []
    for r in ch.get("rows", []):
        d = {"strike": float(r["strike"])}
        for side in ("ce", "pe"):
            x = r.get(side) or {}
            for k_src, k in (("ltp", "ltp"), ("bid", "bid"), ("ask", "ask"), ("delta", "delta"), ("gamma", "gamma"),
                             ("theta", "theta"), ("vega", "vega"), ("iv", "iv"), ("oi", "oi"), ("oi_chg", "chg_oi"),
                             ("vol", "vol"), ("chg", "chg")):
                v = x.get(k_src)
                d[f"{side}_{k}"] = float(v) if v is not None else np.nan
            d[f"{side}_sid"] = x.get("sid")
        rows.append(d)
    if not rows:
        return pd.DataFrame(columns=["strike"])
    df = pd.DataFrame(rows)
    for c_ in df.columns:
        if not c_.endswith("_sid"):
            df[c_] = pd.to_numeric(df[c_], errors="coerce")
    return df.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)


def scenarios(ch: pd.DataFrame, spot: float, side: str, move: float, sl: float, mins: float,
              div_win: float, div_loss: float, window: float = 300, P: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    P = {**DEFAULTS, **(P or {})}
    p = side.lower()
    x = ch[(ch.strike - spot).abs() <= window].copy()
    g = lambda c: x[f"{p}_{c}"] if f"{p}_{c}" in x else pd.Series(np.nan, index=x.index)   # noqa: E731
    out = pd.DataFrame({"strike": x.strike.astype(int), "ltp": g("ltp"), "bid": g("bid"), "ask": g("ask"),
                        "delta": g("delta"), "gamma": g("gamma"), "theta": g("theta"), "vega": g("vega"),
                        "iv": g("iv"), "oi": g("oi")})
    out = out[(out.ltp > P["min_ltp"]) & out.delta.notna()]
    if out.empty:
        return out
    sgn = 1 if side == "CE" else -1
    out["itm"] = (spot - out.strike) * sgn
    D, G, T, V = out.delta.abs(), out.gamma.fillna(0), out.theta.fillna(0), out.vega.fillna(0)
    tcost = T * mins / 375.0                                     # θ is per day → share of a 375-min session (negative)
    out["win"] = D * move + 0.5 * G * move ** 2 + tcost + V * div_win
    out["loss"] = -D * sl + 0.5 * G * sl ** 2 + tcost + V * div_loss          # usually negative
    out["rr"] = out.win / out.loss.abs().replace(0, np.nan)
    out["ret_pct"] = 100 * out.win / out.ltp
    out["theta_pct_day"] = 100 * T.abs() / out.ltp
    atm_iv = float(out.loc[(out.strike - spot).abs().idxmin(), "iv"]) if out.iv.notna().any() else np.nan
    out["ivskew"] = out.iv - atm_iv
    out["spread"] = 100 * (out.ask - out.bid) / out.ltp
    out["course"] = ((out.strike % P["course_strike_multiple"] == 0)
                     & out.itm.between(P["course_itm_min"], P["course_itm_max"])
                     & out.ltp.between(P["course_ltp_min"], P["course_ltp_max"]))
    kb = P["kind_band_pts"]
    out["kind"] = np.where(out.itm > kb, "ITM", np.where(out.itm < -kb, "OTM", "ATM"))
    out["prem_tgt"] = out.ltp + out.win
    out["prem_sl"] = out.ltp + out.loss
    return out.sort_values("strike")


def pick(sc: pd.DataFrame, P: Optional[Dict[str, Any]] = None) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    P = {**DEFAULTS, **(P or {})}
    if sc is None or sc.empty:
        return None, None
    ok = sc[(sc.spread.fillna(0) <= P["max_spread_pct"]) & (sc.win > 0)].copy()
    # course pick: reward:risk, weighted toward MJ's ≈ ₹200 premium (deeper ITM always scores a little more R:R)
    ok["fit"] = ok.rr * (1 - (ok.ltp - P["course_premium"]).abs() / P["course_premium_span"]).clip(P["fit_floor"], 1)
    course = ok[ok.course]
    itm = ok[ok.kind == "ITM"]
    best = (course.sort_values(["fit", "oi"], ascending=False).iloc[0] if len(course) else
            itm.sort_values("fit", ascending=False).iloc[0] if len(itm) else None)
    # fast pick: ATM or 1–2 strikes OTM only (far OTM = lottery tickets)
    fast = ok[(ok.itm <= P["fast_itm_max"]) & (ok.itm >= P["fast_itm_min"])].sort_values("ret_pct", ascending=False)
    return best, (fast.iloc[0] if len(fast) else None)


def path_levels(levels: List[tuple], spot: float, side: str, reach: float) -> Tuple[float, str, list]:
    """First level in the trade's direction (CE = up, PE = down) within reach; else spot ± reach.
    `levels` = [(name, price, colour)] exactly like move_map()['levels']."""
    up = side == "CE"
    cand = sorted([(n, v) for n, v, _ in levels if (v > spot + 5 if up else v < spot - 5)],
                  key=lambda x: (x[1] - spot) * (1 if up else -1))
    inside = [x for x in cand if abs(x[1] - spot) <= reach]
    if inside:
        return inside[0][1], inside[0][0], cand
    return spot + (reach if up else -reach), "rest-of-day expected move", cand


def gamma_flip(g: pd.DataFrame) -> Optional[float]:
    """Strike where cumulative net GEX changes sign (linear interpolation)."""
    cum = g["net_gex"].cumsum().to_numpy()
    ks = g["strike"].to_numpy(float)
    for i in range(1, len(cum)):
        if np.sign(cum[i]) != np.sign(cum[i - 1]) and cum[i] != cum[i - 1]:
            return float(ks[i - 1] + (ks[i] - ks[i - 1]) * (-cum[i - 1]) / (cum[i] - cum[i - 1]))
    return None


def gex(ch: pd.DataFrame, spot: float, lot: int = 65, each_side: int = 10) -> Optional[pd.DataFrame]:
    """options_lab_tab.gex_by_strike core (Dhan chains always carry gamma, so no Black-Scholes fallback)."""
    d = ch.dropna(subset=["ce_gamma", "pe_gamma"]).sort_values("strike")
    if d.empty or (d["ce_gamma"].abs().sum() == 0 and d["pe_gamma"].abs().sum() == 0):
        return None
    atm_i = (d["strike"] - spot).abs().values.argmin()
    d = d.iloc[max(0, atm_i - each_side): atm_i + each_side + 1].copy()
    unit = spot * spot * 0.01 * lot / 1e7
    d["call_gex"] = d["ce_gamma"] * d["ce_oi"] * unit
    d["put_gex"] = d["pe_gamma"] * d["pe_oi"] * unit
    d["net_gex"] = d["call_gex"] - d["put_gex"]
    d["abs_gex"] = d["call_gex"] + d["put_gex"]
    return d[["strike", "call_gex", "put_gex", "net_gex", "abs_gex"]].reset_index(drop=True)
