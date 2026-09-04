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

import math
from dataclasses import dataclass
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config

RISK_FREE = 0.065          # ~India 10y; Greeks are barely sensitive to this
TRADING_DAYS = 252
MIN_IV = 1.0               # below this, NSE's IV is unusable
MAX_IV = 200.0             # above this, likewise


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def time_to_expiry(expiry: datetime, now: datetime | None = None) -> float:
    """
    Years to expiry, counting only trading time.

    On expiry day the difference is not cosmetic. At 14:00 with 75 minutes
    of trading left, calendar maths gives roughly a full day and understates
    Gamma badly — which is precisely the session where Gamma dominates
    everything else.
    """
    now = now or datetime.now()
    if expiry.date() < now.date():
        return 0.0

    open_t, close_t = config.MARKET_OPEN, config.MARKET_CLOSE
    session_mins = ((close_t.hour * 60 + close_t.minute) -
                    (open_t.hour * 60 + open_t.minute))

    if expiry.date() == now.date():
        t = now.time()
        if t >= close_t:
            return 0.0
        left = ((close_t.hour * 60 + close_t.minute) -
                max(open_t.hour * 60 + open_t.minute, t.hour * 60 + t.minute))
        return max(left, 1) / session_mins / TRADING_DAYS

    days = np.busday_count(now.date(), expiry.date())
    frac = 1.0
    if config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE:
        t = now.time()
        left = ((close_t.hour * 60 + close_t.minute) -
                (t.hour * 60 + t.minute))
        frac = max(left, 0) / session_mins
    return max(float(days) - 1 + frac, 1e-6) / TRADING_DAYS


@dataclass
class Greeks:
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    iv_used: float | None
    note: str = ""


def black_scholes(spot: float, strike: float, iv_pct: float, years: float,
                  is_call: bool, rate: float = RISK_FREE) -> Greeks:
    """
    Delta, Gamma, Vega and Theta for one option.

    Returns None values with a reason rather than a plausible-looking number
    when the inputs cannot support a calculation. A Gamma of 0.0 that means
    "the IV was garbage" is indistinguishable from one that means "this
    option has no convexity", and only one of those is information.
    """
    if iv_pct is None or iv_pct != iv_pct:
        return Greeks(None, None, None, None, None, "no IV published")
    if not (MIN_IV <= iv_pct <= MAX_IV):
        return Greeks(None, None, None, None, iv_pct,
                      f"IV {iv_pct:.1f} outside {MIN_IV}-{MAX_IV} — NSE's IV "
                      f"is unreliable on illiquid strikes")
    if years <= 0:
        return Greeks(None, None, None, None, iv_pct, "expired")
    if spot <= 0 or strike <= 0:
        return Greeks(None, None, None, None, iv_pct, "bad spot or strike")

    sigma = iv_pct / 100.0
    sqrt_t = math.sqrt(years)
    d1 = ((math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * years)
          / (sigma * sqrt_t))
    d2 = d1 - sigma * sqrt_t

    gamma = _norm_pdf(d1) / (spot * sigma * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t / 100.0
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 - rate * strike * math.exp(-rate * years) * _norm_cdf(d2))
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(spot * _norm_pdf(d1) * sigma) / (2 * sqrt_t)
                 + rate * strike * math.exp(-rate * years) * _norm_cdf(-d2))
    return Greeks(delta, gamma, vega, theta / TRADING_DAYS, iv_pct)


def add_greeks(df: pd.DataFrame, spot: float, expiry: datetime,
               now: datetime | None = None) -> pd.DataFrame:
    """Attach ce_delta/ce_gamma/pe_delta/pe_gamma to the chain."""
    years = time_to_expiry(expiry, now)
    d = df.copy()
    for col in ("ce_delta", "ce_gamma", "pe_delta", "pe_gamma",
                "ce_theta", "pe_theta", "total_gamma"):
        d[col] = np.nan

    for i, r in d.iterrows():
        k = float(r["strike"])
        for pfx, is_call in (("ce", True), ("pe", False)):
            iv = r.get(f"{pfx}_iv")
            g = black_scholes(spot, k,
                              float(iv) if iv == iv and iv is not None else None,
                              years, is_call)
            if g.delta is not None:
                d.at[i, f"{pfx}_delta"] = g.delta
                d.at[i, f"{pfx}_gamma"] = g.gamma
                d.at[i, f"{pfx}_theta"] = g.theta
        ce_g, pe_g = d.at[i, "ce_gamma"], d.at[i, "pe_gamma"]
        oi_c = r.get("ce_oi") or 0
        oi_p = r.get("pe_oi") or 0
        parts = []
        if ce_g == ce_g:
            parts.append(ce_g * float(oi_c))
        if pe_g == pe_g:
            parts.append(pe_g * float(oi_p))
        if parts:
            d.at[i, "total_gamma"] = sum(parts)
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
