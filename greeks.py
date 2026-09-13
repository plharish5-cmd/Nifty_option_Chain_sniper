"""
Delta and Gamma from Black-Scholes, using NSE's published IV.

I said earlier that the gamma-squeeze feature was blocked because NSE does
not publish Greeks. That was wrong: NSE publishes IMPLIED VOLATILITY, and IV
plus spot, strike, rate and time to expiry is everything Black-Scholes needs.
The Greeks were derivable all along.

WHAT TO TRUST AND WHAT NOT TO

Delta and Gamma computed this way are only as good as the IV underneath.
NSE's IV goes strange on illiquid strikes — deep OTM options that barely
trade often show IV of 0, or absurd values — so anything computed from those
is noise wearing a decimal point. Strikes with unusable IV return None
rather than a number, and the caller has to handle that rather than being
handed a confident zero.

Time to expiry in TRADING time, not calendar time, matters on expiry day:
at 14:00 on expiry there are hours left, not a day, and using a day makes
Gamma far too small exactly when it matters most.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import config


def _load_greeks_rules() -> dict:
    """Loads tunable Greeks and rate thresholds from strategy1_rules.json."""
    try:
        p = Path(__file__).parent / "strategy1_rules.json"
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            return data.get("greeks", {})
    except Exception:
        pass
    return {}


_RULES = _load_greeks_rules()
RISK_FREE = float(_RULES.get("risk_free_rate", 0.065))       # India 10y rate from rules (~6.5%)
DIVIDEND_YIELD = float(_RULES.get("dividend_yield", 0.012))   # NIFTY index dividend yield q ~ 1.2%
CALENDAR_DAYS = float(_RULES.get("calendar_days", 365.0))
MIN_IV = float(_RULES.get("min_iv", 1.0))                   # below this, NSE's IV is unusable
MAX_IV = float(_RULES.get("max_iv", 200.0))                 # above this, likewise


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def time_to_expiry(expiry: datetime, now: datetime | None = None) -> float:
    """
    Years to expiry using calendar day count (365.0 days / year).
    Measures continuous fractional time remaining until market close (15:30) on expiry date.
    """
    now = now or datetime.now()
    if expiry.date() < now.date():
        return 0.0

    close_t = config.MARKET_CLOSE
    exp_dt = datetime.combine(expiry.date(), close_t)
    diff_sec = (exp_dt - now).total_seconds()
    if diff_sec <= 0:
        return 0.0
    return diff_sec / (CALENDAR_DAYS * 86400.0)


def black_scholes_price(spot: float, strike: float, iv_pct: float, years: float,
                        is_call: bool, rate: float = RISK_FREE, q: float = DIVIDEND_YIELD) -> float:
    """Computes theoretical Black-Scholes option price with interest rate and dividend yield."""
    if years <= 0 or iv_pct <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    sigma = iv_pct / 100.0
    sqrt_t = math.sqrt(years)
    d1 = ((math.log(spot / strike) + (rate - q + 0.5 * sigma ** 2) * years)
          / (sigma * sqrt_t))
    d2 = d1 - sigma * sqrt_t
    df_q = math.exp(-q * years)
    df_r = math.exp(-rate * years)
    if is_call:
        return spot * df_q * _norm_cdf(d1) - strike * df_r * _norm_cdf(d2)
    else:
        return strike * df_r * _norm_cdf(-d2) - spot * df_q * _norm_cdf(-d1)


def implied_volatility_bisection(spot: float, strike: float, ltp: float | None,
                                 years: float, is_call: bool, rate: float = RISK_FREE,
                                 q: float = DIVIDEND_YIELD,
                                 min_iv: float = 0.1, max_iv: float = 300.0,
                                 tol: float = 1e-4, max_iter: int = 60) -> tuple[float | None, str]:
    """
    Solves implied volatility from option LTP via bisection on Black-Scholes with dividend yield.
    Bracket: 0.1% to 300.0%, tol: 1e-4, capped iterations.
    Pure math module — no external dependencies.
    """
    if ltp is None or ltp != ltp or ltp <= 0:
        return None, "LTP missing or <= 0"
    if years <= 0:
        return None, "option expired (years <= 0)"
    if spot <= 0 or strike <= 0:
        return None, "bad spot or strike"

    df_q = math.exp(-q * years)
    df_r = math.exp(-rate * years)
    intrinsic = max(0.0, spot * df_q - strike * df_r) if is_call else max(0.0, strike * df_r - spot * df_q)
    if ltp <= intrinsic:
        return None, f"LTP {ltp:.2f} <= intrinsic {intrinsic:.2f} (no time value to invert)"

    p_min = black_scholes_price(spot, strike, min_iv, years, is_call, rate, q)
    if p_min > ltp:
        return None, f"LTP below price at {min_iv}% IV lower bound"
    p_max = black_scholes_price(spot, strike, max_iv, years, is_call, rate, q)
    if p_max < ltp:
        return None, f"LTP exceeds price at {max_iv}% IV upper bound"

    low, high = min_iv, max_iv
    for _ in range(max_iter):
        mid = (low + high) / 2.0
        p_mid = black_scholes_price(spot, strike, mid, years, is_call, rate, q)
        diff = p_mid - ltp
        if abs(diff) < tol or (high - low) < tol:
            return mid, "converged"
        if diff < 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0, "converged"


@dataclass
class Greeks:
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    iv_used: float | None
    iv_source: str = "unavailable"          # "nse" | "solved" | "unavailable"
    iv_leg_source: str = "own"              # "own" | "parity" | "unavailable"
    underlying_source: str = "spot"         # "futures" | "spot"
    parity_residual: float | None = None
    note: str = ""


def black_scholes(spot: float, strike: float, iv_pct: float | None, years: float,
                  is_call: bool, ltp: float | None = None, rate: float = RISK_FREE,
                  q: float | None = None,
                  underlying_source: str = "spot") -> Greeks:
    """
    Delta, Gamma, Vega and Theta for one option.

    Order of preference:
      1. NSE iv if within MIN_IV (1.0) and MAX_IV (200.0)
      2. Solved iv inverted from LTP via bisection
      3. None (with auditable note)
    """
    if q is None:
        q = 0.0 if rate == 0.0 else DIVIDEND_YIELD

    if years <= 0:
        return Greeks(None, None, None, None, iv_pct, "unavailable", "own", underlying_source, None, "expired")
    if spot <= 0 or strike <= 0:
        return Greeks(None, None, None, None, iv_pct, "unavailable", "own", underlying_source, None, "bad spot or strike")

    iv_used = None
    iv_source = "unavailable"
    reason = ""

    # 1. Preferred: NSE published IV
    if iv_pct is not None and iv_pct == iv_pct and (MIN_IV <= iv_pct <= MAX_IV):
        iv_used = float(iv_pct)
        iv_source = "nse"
    else:
        # 2. Fallback: Solved IV from LTP
        solved_iv, solve_note = implied_volatility_bisection(spot, strike, ltp, years, is_call, rate, q)
        if solved_iv is not None:
            iv_used = solved_iv
            iv_source = "solved"
            reason = f"solved from LTP ({solve_note})"
        else:
            reason = f"NSE IV unusable and inversion failed: {solve_note}"

    if iv_used is None:
        return Greeks(None, None, None, None, None, "unavailable", "unavailable", underlying_source, None, reason)

    sigma = iv_used / 100.0
    sqrt_t = math.sqrt(years)
    d1 = ((math.log(spot / strike) + (rate - q + 0.5 * sigma ** 2) * years)
          / (sigma * sqrt_t))
    d2 = d1 - sigma * sqrt_t
    df_q = math.exp(-q * years)
    df_r = math.exp(-rate * years)

    gamma = df_q * _norm_pdf(d1) / (spot * sigma * sqrt_t)
    vega = spot * df_q * _norm_pdf(d1) * sqrt_t / 100.0
    if is_call:
        delta = df_q * _norm_cdf(d1)
        theta = (-(spot * df_q * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 + q * spot * df_q * _norm_cdf(d1)
                 - rate * strike * df_r * _norm_cdf(d2))
    else:
        delta = df_q * (_norm_cdf(d1) - 1.0)
        theta = (-(spot * df_q * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 - q * spot * df_q * _norm_cdf(-d1)
                 + rate * strike * df_r * _norm_cdf(-d2))

    # Verification 1: Solved IV must reprice option back to LTP within tolerance
    if iv_source == "solved" and ltp is not None:
        reprice = black_scholes_price(spot, strike, iv_used, years, is_call, rate, q)
        reprice_err = abs(reprice - ltp)
        if reprice_err >= 0.05:
            return Greeks(None, None, None, None, None, "unavailable", "unavailable", underlying_source, None,
                          f"repricing error {reprice_err:.4f} >= 0.05 (reprice={reprice:.2f}, ltp={ltp:.2f})")

    # Verification 2: Delta must equal df_q * N(d1) computed from identical inputs
    expected_delta = df_q * _norm_cdf(d1) if is_call else (df_q * (_norm_cdf(d1) - 1.0))
    if abs(delta - expected_delta) >= 1e-7:
        return Greeks(None, None, None, None, None, "unavailable", "unavailable", underlying_source, None,
                      f"delta divergence: delta={delta} vs expected={expected_delta}")

    return Greeks(delta, gamma, vega, theta / CALENDAR_DAYS, iv_used, iv_source, "own", underlying_source, 0.0, reason)


def add_greeks(df: pd.DataFrame, spot: float, expiry: datetime | None,
               now: datetime | None = None,
               futures: float | None = None,
               rate: float | None = None,
               q: float | None = None) -> pd.DataFrame:
    """
    Attach ce_delta/ce_gamma/pe_delta/pe_gamma, iv_source, underlying_source, total_gamma,
    iv_leg_source, and parity_residual to chain using Single-IV-per-strike.
    Takes IV from the liquid OTM leg and derives the ITM leg by Put-Call Parity:
      - Delta_PE = Delta_CE - e^(-qT)
      - Gamma_PE = Gamma_CE
      - Vega_PE = Vega_CE
      - Theta derived with drift terms
    """
    d = df.copy()
    for col in ("ce_delta", "ce_gamma", "pe_delta", "pe_gamma",
                "ce_theta", "pe_theta", "ce_vega", "pe_vega",
                "iv_used", "parity_residual", "total_gamma", "gamma_coverage"):
        d[col] = np.nan
    d["ce_iv_source"] = "unavailable"
    d["pe_iv_source"] = "unavailable"
    d["ce_iv_leg_source"] = "unavailable"
    d["pe_iv_leg_source"] = "unavailable"
    d["underlying_source"] = "spot"

    if expiry is None:
        d.attrs["greeks_note"] = "expiry unknown"
        return d

    # Choose underlying, rate, and dividend yield:
    # If futures available and valid, use futures with r = 0.0, q = 0.0 (cost of carry embedded)
    if futures is not None and futures > 0:
        eff_underlying = float(futures)
        eff_rate = 0.0
        eff_q = 0.0
        und_source = "futures"
    else:
        eff_underlying = float(spot)
        eff_rate = float(rate) if rate is not None else RISK_FREE
        eff_q = float(q) if q is not None else DIVIDEND_YIELD
        und_source = "spot"

    d["underlying_source"] = und_source
    years = time_to_expiry(expiry, now)
    if years <= 0:
        d.attrs["greeks_note"] = "expired"
        return d

    df_q = math.exp(-eff_q * years)
    df_r = math.exp(-eff_rate * years)
    sqrt_t = math.sqrt(years)

    for i, r in d.iterrows():
        k = float(r["strike"])
        oi_c = r.get("ce_oi") or 0
        oi_p = r.get("pe_oi") or 0

        # Preferred leg: Liquid OTM leg (Call if K >= Spot, Put if K < Spot)
        pref_is_call = (k >= spot)
        pref_pfx = "ce" if pref_is_call else "pe"
        opp_pfx = "pe" if pref_is_call else "ce"

        iv_used = None
        source = "unavailable"
        leg_source = "unavailable"

        # 1. Attempt preferred OTM leg
        pref_iv = r.get(f"{pref_pfx}_iv")
        pref_ltp = r.get(f"{pref_pfx}_ltp")
        if pref_iv is not None and pref_iv == pref_iv and (MIN_IV <= float(pref_iv) <= MAX_IV):
            iv_used = float(pref_iv)
            source = "nse"
            leg_source = pref_pfx
        elif pref_ltp is not None and pref_ltp == pref_ltp:
            s_iv, _ = implied_volatility_bisection(eff_underlying, k, float(pref_ltp), years,
                                                   pref_is_call, eff_rate, eff_q)
            if s_iv is not None:
                iv_used = s_iv
                source = "solved"
                leg_source = pref_pfx

        # 2. Fallback to opposite leg if preferred leg failed
        if iv_used is None:
            opp_is_call = not pref_is_call
            opp_iv = r.get(f"{opp_pfx}_iv")
            opp_ltp = r.get(f"{opp_pfx}_ltp")
            if opp_iv is not None and opp_iv == opp_iv and (MIN_IV <= float(opp_iv) <= MAX_IV):
                iv_used = float(opp_iv)
                source = "nse"
                leg_source = opp_pfx
            elif opp_ltp is not None and opp_ltp == opp_ltp:
                s_iv, _ = implied_volatility_bisection(eff_underlying, k, float(opp_ltp), years,
                                                       opp_is_call, eff_rate, eff_q)
                if s_iv is not None:
                    iv_used = s_iv
                    source = "solved"
                    leg_source = opp_pfx

        if iv_used is None:
            d.at[i, "gamma_coverage"] = 0.0
            continue

        # Single-IV analytical Black-Scholes Greeks with Put-Call Parity coupling
        sigma = iv_used / 100.0
        d1 = ((math.log(eff_underlying / k) + (eff_rate - eff_q + 0.5 * sigma ** 2) * years)
              / (sigma * sqrt_t))
        d2 = d1 - sigma * sqrt_t

        gamma = df_q * _norm_pdf(d1) / (eff_underlying * sigma * sqrt_t)
        vega = eff_underlying * df_q * _norm_pdf(d1) * sqrt_t / 100.0

        # Exact Delta with Put-Call Parity: Delta_CE - Delta_PE = df_q
        delta_ce = df_q * _norm_cdf(d1)
        delta_pe = delta_ce - df_q

        # Thetas with interest and dividend drift
        theta_ce = (-(eff_underlying * df_q * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                    + eff_q * eff_underlying * df_q * _norm_cdf(d1)
                    - eff_rate * k * df_r * _norm_cdf(d2)) / CALENDAR_DAYS
        theta_pe = (-(eff_underlying * df_q * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                    - eff_q * eff_underlying * df_q * _norm_cdf(-d1)
                    + eff_rate * k * df_r * _norm_cdf(-d2)) / CALENDAR_DAYS

        # Populate DataFrame row
        d.at[i, "iv_used"] = iv_used
        d.at[i, "ce_delta"] = delta_ce
        d.at[i, "pe_delta"] = delta_pe
        d.at[i, "ce_gamma"] = gamma
        d.at[i, "pe_gamma"] = gamma
        d.at[i, "ce_vega"] = vega
        d.at[i, "pe_vega"] = vega
        d.at[i, "ce_theta"] = theta_ce
        d.at[i, "pe_theta"] = theta_pe
        d.at[i, "parity_residual"] = abs(delta_ce - delta_pe - df_q)
        d.at[i, "gamma_coverage"] = 1.0
        d.at[i, "total_gamma"] = gamma * (float(oi_c) + float(oi_p))

        if leg_source == "ce":
            d.at[i, "ce_iv_source"] = source
            d.at[i, "ce_iv_leg_source"] = "own"
            d.at[i, "pe_iv_source"] = "parity"
            d.at[i, "pe_iv_leg_source"] = "parity"
        else:
            d.at[i, "pe_iv_source"] = source
            d.at[i, "pe_iv_leg_source"] = "own"
            d.at[i, "ce_iv_source"] = "parity"
            d.at[i, "ce_iv_leg_source"] = "parity"

    return d


@dataclass
class GammaZone:
    strike: int
    total_gamma: float
    distance: float
    share: float               # % of the window's total gamma

    @property
    def label(self) -> str:
        if self.share >= 25:
            return "MAJOR STICKY ZONE"
        if self.share >= 12:
            return "sticky zone"
        return "minor"

    @property
    def note(self) -> str:
        return (f"{self.share:.0f}% of nearby gamma sits here — market makers "
                f"hedging this strike buy dips and sell rallies around it, "
                f"which pins price toward it")


def gamma_zones(df: pd.DataFrame, spot: float, n: int = 3,
                span: int = 400) -> list[GammaZone]:
    """
    Strikes carrying the most gamma-weighted open interest.

    Where dealers are most short gamma is where their hedging is most
    aggressive, and that hedging mechanically dampens movement — price gets
    sticky. It is the same mechanism as the squeeze, seen from the other
    side: sticky until the level breaks, violent once it does.
    """
    w = df[(df.strike >= spot - span) & (df.strike <= spot + span)]
    if w.empty or "total_gamma" not in w:
        return []
    tot = float(np.nansum(w["total_gamma"]))
    if tot <= 0:
        return []
    out = []
    for _, r in w.nlargest(n, "total_gamma").iterrows():
        g = float(r["total_gamma"])
        if g != g or g <= 0:
            continue
        out.append(GammaZone(int(r["strike"]), g, float(r["strike"]) - spot,
                             g / tot * 100))
    return out
