"""Four more reads from the recorded option-chain snapshots, for the 8600 board.

    pcr_zone      Put-call ratio of open interest near spot.
                  0.5-0.8 call writers in control (PE) · 0.8-1.2 balanced ·
                  1.2-1.5 put writers in control (CE) · below 0.5 / above 1.5
                  is an extreme and is read as NEUTRAL (stretched, bounce risk).
    price_oi      Option premium against open interest over the last 15 minutes,
                  at the strikes around the money:
                    calls  premium up + OI up   long buildup      bullish  +2
                           premium down + OI up call writing      bearish  -2
                           premium up + OI down short covering    bullish  +1
                           premium down + OI down long unwinding  bearish  -1
                    puts   the mirror image.
                  Buildups count double because fresh positions carry more
                  follow-through than exits.
    volume_oi     Is the last 15 minutes of OI change backed by volume, or is it
                  churn? Churn = this window's (OI change / volume) is under half
                  of the day's usual. Churn reads NEUTRAL; otherwise the read is
                  the direction of the 15-minute PE-CE change.
    gamma_regime  Net gamma exposure from Black-Scholes gammas (implied
                  volatility solved from each strike's last price). Positive =
                  hedging dampens moves (range, pinning): it reports "no room"
                  for both sides. Negative = hedging amplifies moves: room both
                  sides. A MODEL, not a measurement: it assumes calls are held
                  long and puts short by hedgers.

Also reported as plain facts (no switch): PCR, max pain, gamma flip level.

Pure functions. No locked module is imported. With missing data a read is
"no data" and never causes a skip. Every threshold is a first guess.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_CFG: Dict[str, Any] = {
    "band_pts": 1000.0,
    "atm_strikes_each_side": 3,
    "window_minutes": 15,
    "pcr_bear_below": 0.8,
    "pcr_bull_above": 1.2,
    "pcr_extreme_low": 0.5,
    "pcr_extreme_high": 1.5,
    "churn_fraction_of_usual": 0.5,
    "risk_free_rate": 0.065,
    "max_snapshot_age_sec": 600,
}


def load_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    out.update((scanner_cfg or {}).get("chain_reads") or {})
    return out


# ── Black-Scholes pieces (self-contained) ────────────────────────────────────
def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(s: float, k: float, sigma: float, t: float, r: float, call: bool) -> float:
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if call:
        return s * _cdf(d1) - k * math.exp(-r * t) * _cdf(d2)
    return k * math.exp(-r * t) * _cdf(-d2) - s * _cdf(-d1)


def implied_vol(s: float, k: float, price: float, t: float, r: float, call: bool) -> Optional[float]:
    """Annual volatility as a fraction, or None when the price has no time value to invert."""
    if s <= 0 or k <= 0 or price <= 0 or t <= 0:
        return None
    intrinsic = max(0.0, s - k * math.exp(-r * t)) if call else max(0.0, k * math.exp(-r * t) - s)
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 0.01, 3.0
    if _bs_price(s, k, lo, t, r, call) > price or _bs_price(s, k, hi, t, r, call) < price:
        return None
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _bs_price(s, k, mid, t, r, call) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_gamma(s: float, k: float, sigma: float, t: float, r: float) -> float:
    if s <= 0 or k <= 0 or sigma <= 0 or t <= 0:
        return 0.0
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _pdf(d1) / (s * sigma * math.sqrt(t))


def _years_to_expiry(expiry: str, now_ts: int) -> float:
    try:
        end = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").replace(tzinfo=IST) + timedelta(hours=15, minutes=30)
    except ValueError:
        return 0.0
    return max(0.0, (end.timestamp() - now_ts) / (365.0 * 86400.0))


def max_pain(chain: Dict[float, Dict[str, float]]) -> Optional[float]:
    """The strike at which option buyers, in total, are paid the least."""
    strikes = sorted(chain)
    if not strikes:
        return None
    best, best_pay = None, None
    for settle in strikes:
        pay = 0.0
        for k, v in chain.items():
            pay += max(0.0, settle - k) * v["ce_oi"] + max(0.0, k - settle) * v["pe_oi"]
        if best_pay is None or pay < best_pay:
            best, best_pay = settle, pay
    return best


def _net_gex(chain: Dict[float, Dict[str, float]], ivs: Dict[float, Tuple[Optional[float], Optional[float]]],
             s: float, t: float, r: float) -> float:
    total = 0.0
    for k, v in chain.items():
        ce_iv, pe_iv = ivs.get(k, (None, None))
        if ce_iv:
            total += bs_gamma(s, k, ce_iv, t, r) * v["ce_oi"]
        if pe_iv:
            total -= bs_gamma(s, k, pe_iv, t, r) * v["pe_oi"]
    return total * s * s * 0.01


def compute_reads(rows: List[Dict[str, Any]], spot: float, cfg: Dict[str, Any], *, day: str,
                  now_ts: int) -> Dict[str, Any]:
    """{"status": "OK"|"NO_DATA", "reads": {id: {direction, detail, [blocks]}}, "facts": {...}}"""
    out: Dict[str, Any] = {"status": "NO_DATA", "reason": "", "reads": {}, "facts": {}}
    if not rows or spot <= 0:
        out["reason"] = "no option-chain snapshots recorded for this day"
        return out
    by_ts: Dict[int, Dict[float, Dict[str, float]]] = {}
    for r in rows:
        t = int(float(r["ts"]))
        if t > now_ts:
            continue
        by_ts.setdefault(t, {})[float(r["strike"])] = {
            key: float(r.get(key) or 0.0) for key in ("ce_oi", "pe_oi", "ce_vol", "pe_vol", "ce_ltp", "pe_ltp")
        }
    ts_sorted = sorted(by_ts)
    if not ts_sorted:
        out["reason"] = "no snapshot at or before this time"
        return out
    last_t = ts_sorted[-1]
    age = now_ts - last_t
    if age > int(cfg.get("max_snapshot_age_sec", 600)):
        out["reason"] = f"latest snapshot is {age // 60} min old"
        return out
    out["status"] = "OK"
    out["snapshot_age_sec"] = int(age)
    band = float(cfg.get("band_pts", 1000.0))
    now_chain = {k: v for k, v in by_ts[last_t].items() if abs(k - spot) <= band}
    reads: Dict[str, Dict[str, Any]] = {}
    facts: Dict[str, Any] = {}
    nodata = lambda why: {"direction": None, "detail": why}

    # ── PCR zone ─────────────────────────────────────────────────────────────
    ce_total = sum(v["ce_oi"] for v in now_chain.values())
    pe_total = sum(v["pe_oi"] for v in now_chain.values())
    if ce_total > 0:
        pcr = pe_total / ce_total
        facts["pcr"] = round(pcr, 2)
        if pcr < float(cfg["pcr_extreme_low"]):
            reads["pcr_zone"] = {"direction": "NEUTRAL", "detail": f"PCR {pcr:.2f} — extreme call dominance, bounce risk"}
        elif pcr < float(cfg["pcr_bear_below"]):
            reads["pcr_zone"] = {"direction": "PE", "detail": f"PCR {pcr:.2f} — call writers in control"}
        elif pcr <= float(cfg["pcr_bull_above"]):
            reads["pcr_zone"] = {"direction": "NEUTRAL", "detail": f"PCR {pcr:.2f} — balanced"}
        elif pcr <= float(cfg["pcr_extreme_high"]):
            reads["pcr_zone"] = {"direction": "CE", "detail": f"PCR {pcr:.2f} — put writers in control"}
        else:
            reads["pcr_zone"] = {"direction": "NEUTRAL", "detail": f"PCR {pcr:.2f} — extreme put dominance, pullback risk"}
    else:
        reads["pcr_zone"] = nodata("no call OI near spot")

    facts["max_pain"] = max_pain(now_chain)

    # ── the 15-minute window ─────────────────────────────────────────────────
    win = 60 * int(cfg.get("window_minutes", 15))
    prev_t = None
    for t in ts_sorted:
        if t <= last_t - win:
            prev_t = t
    strikes = sorted(now_chain)
    n_side = int(cfg.get("atm_strikes_each_side", 3))
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot)) if strikes else 0
    atm = strikes[max(0, atm_i - n_side): atm_i + n_side + 1]

    def window_change(t0: int, t1: int) -> Optional[Dict[str, float]]:
        a, b = by_ts[t0], by_ts[t1]
        ks = [k for k in atm if k in a and k in b]
        if not ks:
            return None
        g = lambda side, field: sum(b[k][f"{side}_{field}"] - a[k][f"{side}_{field}"] for k in ks)
        return {"ce_oi": g("ce", "oi"), "pe_oi": g("pe", "oi"), "ce_ltp": g("ce", "ltp"), "pe_ltp": g("pe", "ltp"),
                "vol": max(0.0, g("ce", "vol")) + max(0.0, g("pe", "vol"))}

    ch = window_change(prev_t, last_t) if prev_t is not None else None
    if ch is None:
        reads["price_oi"] = nodata("needs 15 minutes of snapshots")
        reads["volume_oi"] = nodata("needs 15 minutes of snapshots")
    else:
        have_ltp = any(v["ce_ltp"] > 0 or v["pe_ltp"] > 0 for v in now_chain.values())
        if not have_ltp:
            reads["price_oi"] = nodata("snapshots carry no option prices")
        else:
            def tag(prem: float, oi: float, call: bool) -> Tuple[int, str]:
                if oi > 0 and prem > 0:
                    return (2, "long buildup") if call else (-2, "put buying")
                if oi > 0 and prem < 0:
                    return (-2, "call writing") if call else (2, "put writing")
                if oi < 0 and prem > 0:
                    return (1, "short covering") if call else (-1, "put short covering")
                if oi < 0 and prem < 0:
                    return (-1, "long unwinding") if call else (1, "put unwinding")
                return 0, "flat"
            cs, cname = tag(ch["ce_ltp"], ch["ce_oi"], True)
            ps, pname = tag(ch["pe_ltp"], ch["pe_oi"], False)
            score = cs + ps
            reads["price_oi"] = {
                "direction": "CE" if score >= 2 else ("PE" if score <= -2 else "NEUTRAL"),
                "detail": f"calls: {cname} · puts: {pname} (score {score:+d})",
            }
        # volume backing: compare with the day's earlier windows
        ratios: List[float] = []
        t_end = prev_t
        while t_end is not None and len(ratios) < 40:
            t_start = None
            for t in ts_sorted:
                if t <= t_end - win:
                    t_start = t
            if t_start is None:
                break
            w = window_change(t_start, t_end)
            if w and w["vol"] > 0:
                ratios.append((abs(w["ce_oi"]) + abs(w["pe_oi"])) / w["vol"])
            t_end = t_start
        if ch["vol"] <= 0:
            reads["volume_oi"] = nodata("no volume in the snapshots")
        elif len(ratios) < 3:
            reads["volume_oi"] = nodata("needs an hour of snapshots to know the day's usual")
        else:
            ratio = (abs(ch["ce_oi"]) + abs(ch["pe_oi"])) / ch["vol"]
            usual = median(ratios)
            net = ch["pe_oi"] - ch["ce_oi"]
            if usual > 0 and ratio < float(cfg["churn_fraction_of_usual"]) * usual:
                reads["volume_oi"] = {"direction": "NEUTRAL",
                                      "detail": f"churn — OI change per unit volume is {ratio / usual:.0%} of the day's usual"}
            else:
                reads["volume_oi"] = {"direction": "CE" if net > 0 else ("PE" if net < 0 else "NEUTRAL"),
                                      "detail": f"OI change backed by volume ({ratio / usual:.0%} of usual)" if usual > 0
                                      else "OI change backed by volume"}

    # ── gamma regime ─────────────────────────────────────────────────────────
    t_years = _years_to_expiry(str(rows[0].get("expiry") or ""), now_ts)
    r = float(cfg.get("risk_free_rate", 0.065))
    ivs: Dict[float, Tuple[Optional[float], Optional[float]]] = {}
    if t_years > 0:
        for k, v in now_chain.items():
            ivs[k] = (implied_vol(spot, k, v["ce_ltp"], t_years, r, True),
                      implied_vol(spot, k, v["pe_ltp"], t_years, r, False))
    solved = sum(1 for a, b in ivs.values() if a or b)
    if t_years <= 0:
        reads["gamma_regime"] = nodata("expiry has passed or is unknown")
    elif solved < 5:
        reads["gamma_regime"] = nodata("not enough option prices to solve volatility")
    else:
        net = _net_gex(now_chain, ivs, spot, t_years, r)
        flip = None
        grid = [spot + d for d in range(-400, 401, 10)]
        prev_s, prev_v = None, None
        for s in grid:
            v = _net_gex(now_chain, ivs, s, t_years, r)
            if prev_v is not None and (prev_v < 0) != (v < 0):
                cand = prev_s + (s - prev_s) * (0 - prev_v) / (v - prev_v) if v != prev_v else s
                if flip is None or abs(cand - spot) < abs(flip - spot):
                    flip = cand
            prev_s, prev_v = s, v
        facts["net_gex"] = round(net, 0)
        facts["gamma_flip"] = None if flip is None else round(flip, 0)
        flip_txt = "" if flip is None else f" · flip {flip:.0f}"
        if net > 0:
            reads["gamma_regime"] = {"direction": "ROOM", "blocks": ["CE", "PE"],
                                     "detail": f"positive gamma — moves dampened, range likely{flip_txt}"}
        else:
            reads["gamma_regime"] = {"direction": "ROOM", "blocks": [],
                                     "detail": f"negative gamma — moves amplified, trend can run{flip_txt}"}

    for v in reads.values():
        v["age_sec"] = int(age) if v.get("direction") is not None else None
    out["reads"] = reads
    out["facts"] = facts
    return out
