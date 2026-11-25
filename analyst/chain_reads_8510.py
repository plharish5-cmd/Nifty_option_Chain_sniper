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

DEFAULT_MOMENTUM_CFG: Dict[str, Any] = {
    "window_min": 10,
    "sig_min": 0.15,
    "sig_extreme": 0.30,
    "require_cumd_confirm": False,
    "require_volume_confirm": False,
    "require_price_confirm": False,
    "deadzone_cr_per_min": 0.05,  # retired — ignored; kept for config back-compat
}

LAKH_DIVISOR = 1e5  # 1 lakh contracts; 1 crore = 100 lakh


def _to_lakhs(oi_raw: float) -> float:
    """Single unit conversion at the OI read boundary (spec: compute/display in lakhs)."""
    return float(oi_raw) / LAKH_DIVISOR

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


def _hm_ts(day: str, hm: str) -> int:
    return int(datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def _snapshot_at(ts_sorted: List[int], want: int, *, at_or_before: bool = True) -> Optional[int]:
    if at_or_before:
        best = None
        for t in ts_sorted:
            if t <= want:
                best = t
            else:
                break
        return best
    for t in ts_sorted:
        if t >= want:
            return t
    return None


def _chain_totals(chain: Dict[float, Dict[str, float]], spot: float, band: float) -> Tuple[float, float]:
    ce = pe = 0.0
    for k, v in chain.items():
        if abs(k - spot) <= band:
            ce += float(v.get("ce_oi") or 0.0)
            pe += float(v.get("pe_oi") or 0.0)
    return ce, pe


def _chain_totals_full(chain: Dict[float, Dict[str, float]]) -> Tuple[float, float]:
    """Sum Call/Put OI across every strike in the snapshot (matches 8600 total-OI charts)."""
    ce = pe = 0.0
    for v in chain.values():
        ce += float(v.get("ce_oi") or 0.0)
        pe += float(v.get("pe_oi") or 0.0)
    return ce, pe


def _snapshot_in_momentum_window(ts_sorted: List[int], last_t: int, w_min: int) -> Optional[int]:
    """Closest snapshot to (last_t − window_min), only if it lies in [w±2] min band — no stale fallback."""
    target = last_t - w_min * 60
    lo = last_t - (w_min + 2) * 60
    hi = last_t - (w_min - 2) * 60
    candidates = [t for t in ts_sorted if lo <= t <= hi and t < last_t]
    if not candidates:
        return None
    return min(candidates, key=lambda t: abs(t - target))


def momentum_direction_from_significance(
    s: float,
    *,
    sig_min: float,
    sig_extreme: float,
) -> Tuple[str, str]:
    """Gate on normalized significance S = dD/T (Option A). B is never used here."""
    if s >= float(sig_extreme):
        return "CE", "EXTREME"
    if s >= float(sig_min):
        return "CE", "STRONG"
    if s <= -float(sig_extreme):
        return "PE", "EXTREME"
    if s <= -float(sig_min):
        return "PE", "STRONG"
    return "NEUTRAL", "NEUTRAL"


def volume_backing_between_snapshots(
    by_ts: Dict[int, Dict[float, Dict[str, float]]],
    ts_sorted: List[int],
    t0: int,
    t1: int,
    spot: float,
    band: float,
    cfg: Dict[str, Any],
) -> Tuple[Optional[bool], str]:
    """Same churn test as volume_oi, on window [t0,t1]. None = unknown → fail-open (do not downgrade)."""
    if t0 not in by_ts or t1 not in by_ts or t0 >= t1:
        return None, "unknown"
    atm = [k for k in by_ts[t1] if abs(k - spot) <= band]
    if not atm:
        return None, "unknown"
    a, b = by_ts[t0], by_ts[t1]
    ks = [k for k in atm if k in a and k in b]
    if not ks:
        return None, "unknown"

    def g(side: str, field: str) -> float:
        return sum(b[k][f"{side}_{field}"] - a[k][f"{side}_{field}"] for k in ks)

    ch_vol = max(0.0, g("ce", "vol")) + max(0.0, g("pe", "vol"))
    if ch_vol <= 0:
        return None, "no vol"
    win = 60 * int(cfg.get("window_minutes", 15))
    ratios: List[float] = []
    t_end = t0
    while t_end is not None and len(ratios) < 40:
        t_start = None
        for t in ts_sorted:
            if t <= t_end - win:
                t_start = t
        if t_start is None:
            break
        a2, b2 = by_ts.get(t_start), by_ts.get(t_end)
        if not a2 or not b2:
            break
        ks2 = [k for k in atm if k in a2 and k in b2]
        if not ks2:
            t_end = t_start
            continue
        vol = max(
            0.0,
            sum(b2[k]["ce_vol"] - a2[k]["ce_vol"] for k in ks2)
            + sum(b2[k]["pe_vol"] - a2[k]["pe_vol"] for k in ks2),
        )
        oi = abs(sum(b2[k]["ce_oi"] - a2[k]["ce_oi"] for k in ks2)) + abs(
            sum(b2[k]["pe_oi"] - a2[k]["pe_oi"] for k in ks2)
        )
        if vol > 0:
            ratios.append(oi / vol)
        t_end = t_start
    if len(ratios) < 3:
        return None, "unknown"
    oi_chg = abs(g("ce", "oi")) + abs(g("pe", "oi"))
    ratio = oi_chg / ch_vol
    usual = median(ratios)
    if usual > 0 and ratio < float(cfg.get("churn_fraction_of_usual", 0.5)) * usual:
        return False, "churn"
    return True, "backed"


def _apply_cumd_confirm(direction: str, strength: str, cumd: float, required: bool) -> Tuple[str, str]:
    if not required or direction == "NEUTRAL":
        return direction, strength
    if direction == "CE" and cumd <= 0:
        return "NEUTRAL", "NEUTRAL"
    if direction == "PE" and cumd >= 0:
        return "NEUTRAL", "NEUTRAL"
    return direction, strength


def _momentum_significance(
    ce_t: float,
    pe_t: float,
    ce_prev: float,
    pe_prev: float,
    ce_open: float,
    pe_open: float,
) -> Optional[Dict[str, float]]:
    c = _to_lakhs(ce_t)
    p = _to_lakhs(pe_t)
    t = p + c
    if t <= 0:
        return None
    d = p - c
    d_prev = _to_lakhs(pe_prev) - _to_lakhs(ce_prev)
    dd = d - d_prev
    s = dd / t
    b = d / t
    d_open = _to_lakhs(pe_open) - _to_lakhs(ce_open)
    cumd = d - d_open
    return {"C": c, "P": p, "T": t, "D": d, "S": s, "B": b, "CumD": cumd}


def _apply_fresh_oi_regime(read: Dict[str, Any], desk8600_regime: Optional[str]) -> None:
    """Align Fresh OI direction/gate with S, bias, and optional 8600 regime banner."""
    from analyst.nifty_four_oi_8510 import (
        classify_fresh_oi_regime,
        fresh_oi_regime_to_direction,
    )

    if read.get("S") is None or read.get("B") is None:
        return
    regime = classify_fresh_oi_regime(
        float(read["S"]), float(read["B"]), desk8600_regime=desk8600_regime
    )
    read["regime"] = regime
    read["direction"] = fresh_oi_regime_to_direction(regime)
    detail = str(read.get("detail") or "")
    if "→" in detail:
        head = detail.split("→", 1)[0].rstrip()
        read["detail"] = f"{head} → {regime}"


def _momentum_item(
    *,
    metrics: Dict[str, float],
    sig_min: float,
    sig_extreme: float,
    require_cumd: bool,
    require_volume: bool,
    volume_label: str,
    volume_backed: Optional[bool],
    age_sec: int,
    label: str,
) -> Dict[str, Any]:
    s = float(metrics["S"])
    b = float(metrics["B"])
    cumd = float(metrics["CumD"])
    c = float(metrics["C"])
    p = float(metrics["P"])
    direction, strength = momentum_direction_from_significance(s, sig_min=sig_min, sig_extreme=sig_extreme)
    direction, strength = _apply_cumd_confirm(direction, strength, cumd, require_cumd)
    if require_volume and direction in ("CE", "PE") and volume_backed is False:
        direction, strength = "NEUTRAL", "NEUTRAL"
    vol_note = f" · vol: {volume_label}" if volume_label else ""
    detail = (
        f"{label}: S {s:+.2f} · bias {b:+.2f} · CumD {cumd:+.0f}L "
        f"(C {c:.0f} P {p:.0f}){vol_note} → {direction}"
        + (f" {strength}" if strength and strength != "NEUTRAL" else "")
    )
    return {
        "direction": direction,
        "strength": strength,
        "detail": detail,
        "age_sec": age_sec,
        "S": round(s, 4),
        "B": round(b, 4),
        "CumD_lakh": round(cumd, 2),
        "C_lakh": round(c, 1),
        "P_lakh": round(p, 1),
        "volume_backing": volume_label or None,
    }


def compute_momentum_oi_reads(
    by_ts: Dict[int, Dict[float, Dict[str, float]]],
    ts_sorted: List[int],
    *,
    spot: float,
    band: float,
    day: str,
    now_ts: int,
    last_t: int,
    age_sec: int,
    momentum_cfg: Optional[Dict[str, Any]] = None,
    spot_window_bars: Optional[List[Dict[str, float]]] = None,
    desk8600_regime: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Normalized S = dD/T momentum for total_oi and fresh_oi. Classic Writer Flow stays on the 8600 bridge."""
    cfg = dict(DEFAULT_MOMENTUM_CFG)
    if isinstance(momentum_cfg, dict):
        cfg.update({k: v for k, v in momentum_cfg.items() if not k.startswith("_")})
    w_min = max(1, int(cfg.get("window_min", 10)))
    sig_min = float(cfg.get("sig_min", 0.15))
    sig_extreme = float(cfg.get("sig_extreme", 0.30))
    require_cumd = bool(cfg.get("require_cumd_confirm", False))
    require_vol = bool(cfg.get("require_volume_confirm", False))
    require_price = bool(cfg.get("require_price_confirm", False))
    miss = lambda why: {
        "direction": None,
        "detail": why,
        "age_sec": age_sec if age_sec >= 0 else None,
        "S": None,
        "B": None,
        "CumD_lakh": None,
    }
    prev_t = _snapshot_in_momentum_window(ts_sorted, last_t, w_min)
    if prev_t is None:
        return {
            "total_oi": miss(f"no snapshot in {w_min}±2 min window for momentum"),
            "fresh_oi": miss(f"no snapshot in {w_min}±2 min window for momentum"),
        }
    session_open = _hm_ts(day, "09:15")
    # Recorded snapshots carry seconds; requiring <=09:15:00 loses an
    # entire session when the first real observation was 09:15:16.
    # Use the actual first session observation and disclose its timestamp.
    base_t = next((t for t in ts_sorted if session_open <= t <= last_t), None)
    if base_t is None or base_t >= last_t:
        return {
            "total_oi": miss("waiting for 09:15 baseline snapshot"),
            "fresh_oi": miss("waiting for 09:15 baseline snapshot"),
        }
    vol_backed: Optional[bool] = None
    vol_lbl = ""
    if require_vol:
        vol_backed, vol_lbl = volume_backing_between_snapshots(
            by_ts, ts_sorted, prev_t, last_t, spot, band, DEFAULT_CFG
        )
    ce_l, pe_l = _chain_totals_full(by_ts[last_t])
    ce_p, pe_p = _chain_totals_full(by_ts[prev_t])
    ce_b, pe_b = _chain_totals_full(by_ts[base_t])
    total_m = _momentum_significance(ce_l, pe_l, ce_p, pe_p, ce_b, pe_b)
    if total_m is None:
        return {
            "total_oi": miss("zero total OI — cannot normalize"),
            "fresh_oi": miss("zero total OI — cannot normalize"),
        }
    total = _momentum_item(
        metrics=total_m,
        sig_min=sig_min,
        sig_extreme=sig_extreme,
        require_cumd=require_cumd,
        require_volume=require_vol,
        volume_label=vol_lbl,
        volume_backed=vol_backed,
        age_sec=age_sec,
        label="Total OI",
    )
    call_f_l = ce_l - ce_b
    put_f_l = pe_l - pe_b
    call_f_p = ce_p - ce_b
    put_f_p = pe_p - pe_b
    fresh_m = _momentum_significance(call_f_l, put_f_l, call_f_p, put_f_p, 0.0, 0.0)
    if fresh_m is None:
        fresh = miss("zero fresh OI components")
    else:
        fresh = _momentum_item(
            metrics=fresh_m,
            sig_min=sig_min,
            sig_extreme=sig_extreme,
            require_cumd=require_cumd,
            require_volume=require_vol,
            volume_label=vol_lbl,
            volume_backed=vol_backed,
            age_sec=age_sec,
            label="OI chg since recorded " + datetime.fromtimestamp(base_t, IST).strftime("%H:%M:%S"),
        )
        _apply_fresh_oi_regime(fresh, desk8600_regime)
    out_reads = {"total_oi": total, "fresh_oi": fresh}
    for read in out_reads.values():
        read["baseline_ts"] = base_t
        read["baseline_kind"] = "FIRST_RECORDED_SESSION_SNAPSHOT"
        read["baseline_delay_sec"] = base_t - session_open
        read["detail"] += " · baseline recorded " + datetime.fromtimestamp(base_t, IST).strftime("%H:%M:%S")
    if require_price:
        from analyst.oi_price_confirm_8510 import apply_price_confirm_to_momentum_read

        bars = spot_window_bars or []
        for key in out_reads:
            out_reads[key] = apply_price_confirm_to_momentum_read(
                out_reads[key], require=True, window_bars=bars
            )
    return out_reads


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


def compute_reads(
    rows: List[Dict[str, Any]],
    spot: float,
    cfg: Dict[str, Any],
    *,
    day: str,
    now_ts: int,
    momentum_cfg: Optional[Dict[str, Any]] = None,
    spot_window_bars: Optional[List[Dict[str, float]]] = None,
    desk8600_regime: Optional[str] = None,
    scanner_cfg: Optional[Dict[str, Any]] = None,
    prior_day_close: Optional[Dict[float, Dict[str, float]]] = None,
) -> Dict[str, Any]:
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
    out["snapshot_ts"] = datetime.fromtimestamp(last_t, IST).isoformat()
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
    try:
        out["momentum_oi"] = compute_momentum_oi_reads(
            by_ts,
            ts_sorted,
            spot=spot,
            band=band,
            day=day,
            now_ts=now_ts,
            last_t=last_t,
            age_sec=int(age),
            momentum_cfg=momentum_cfg,
            spot_window_bars=spot_window_bars,
            desk8600_regime=desk8600_regime,
        )
    except Exception:
        out["momentum_oi"] = {}
    try:
        from analyst.writer_bias_live_reads_8510 import apply_writer_bias_to_board_reads

        expiry = str(rows[0].get("expiry") or "") if rows else ""
        wb = apply_writer_bias_to_board_reads(
            by_ts=by_ts,
            ts_sorted=ts_sorted,
            spot=spot,
            band=band,
            day=day,
            now_ts=now_ts,
            last_t=last_t,
            age_sec=int(age),
            scanner_cfg=scanner_cfg,
            momentum_oi=out.get("momentum_oi") or {},
            expiry=expiry,
            prior_day_close=prior_day_close,
        )
        out["writer_bias"] = wb
        for key, patch in (wb.get("momentum_patch") or {}).items():
            if key in out["momentum_oi"] and isinstance(patch, dict):
                out["momentum_oi"][key] = {**(out["momentum_oi"][key] or {}), **patch}
        out["facts"].update(wb.get("facts") or {})
    except Exception:
        out.setdefault("writer_bias", {})
    return out
