"""
chain_metrics.py — weight balance, IV demand across expiries, strike VWAP.

Three things the app was missing, plus one it could only approximate.

WEIGHT BALANCE
    Cumulative change in OI for the five strikes above and five below the
    centre. The heavier side is where writers are building, and the read is
    contrarian: you favour the side with LESS weight, because that is the
    direction with less written resistance in front of it.

IV DEMAND
    Average implied volatility of the top five call and put strikes, across
    every available expiry rather than the front one alone. Higher IV on a
    side means people are paying up there.

STRIKE VWAP — moved out
    Previously approximated here from NSE polling. Now computed properly in
    option_vwap.py from Dhan's per-bar OHLCV. See the note further down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

import config


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Weight balance
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Weight:
    centre: int
    n: int
    ce_chg: float
    pe_chg: float
    ce_strikes: list
    pe_strikes: list

    @property
    def total(self) -> float:
        return abs(self.ce_chg) + abs(self.pe_chg)

    @property
    def heavier(self) -> str:
        if self.total == 0:
            return "NEITHER"
        return "CALLS" if self.ce_chg > self.pe_chg else "PUTS"

    @property
    def tilt_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return abs(self.ce_chg - self.pe_chg) / self.total * 100

    @property
    def favour(self) -> str:
        """The contrarian read — the lighter side."""
        if self.tilt_pct < 15:
            return "NEITHER"
        return "PE" if self.heavier == "CALLS" else "CE"

    @property
    def note(self) -> str:
        if self.heavier == "NEITHER" or self.favour == "NEITHER":
            return (f"Both sides within {self.tilt_pct:.0f}% of each other — "
                    f"balanced. No directional read from weight.")
        side = "above" if self.heavier == "CALLS" else "below"
        return (
            f"{self.heavier} carry {self.tilt_pct:.0f}% more new OI across "
            f"the {self.n} strikes {side} the centre — writers are building "
            f"there, which is resistance in that direction. The lighter side "
            f"is {self.favour}: less written into its path. This is a "
            f"contrarian read of positioning, not a signal — writers defend "
            f"their strikes right up until they do not.")


def weight_balance(df: pd.DataFrame, centre: int, n: int = 5) -> Weight | None:
    """
    Cumulative change in OI, n strikes above and n below the centre.

    Calls are summed ABOVE the centre and puts BELOW it, because those are
    the strikes each side actually writes. Summing both sides across the
    whole window would mix defensive writing with hedging and produce a
    number that moves for reasons you cannot separate.
    """
    if df is None or df.empty:
        return None
    from analytics import deduplicate_strikes_live
    df = deduplicate_strikes_live(df, "weight_balance")
    step = getattr(config, "STRIKE_STEP", 50)
    up = [centre + i * step for i in range(1, n + 1)]
    dn = [centre - i * step for i in range(1, n + 1)]

    def total(strikes, col):
        rows = df[df.strike.isin(strikes)]
        return float(np.nansum(rows[col].to_numpy(float))) if len(rows) else 0.0

    return Weight(centre, n, total(up, "ce_chg_oi"), total(dn, "pe_chg_oi"),
                  up, dn)


# ═══════════════════════════════════════════════════════════════════════════
# 3 · IV demand across expiries
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class IVDemand:
    per_expiry: dict = field(default_factory=dict)
    ce_avg: float = 0.0
    pe_avg: float = 0.0

    @property
    def diff(self) -> float:
        return self.ce_avg - self.pe_avg

    @property
    def leaning(self) -> str:
        if abs(self.diff) < 0.5:
            return "BALANCED"
        return "CALLS" if self.diff > 0 else "PUTS"

    @property
    def note(self) -> str:
        if self.leaning == "BALANCED":
            return (f"Call IV {self.ce_avg:.1f} against put IV "
                    f"{self.pe_avg:.1f} — no meaningful difference in demand.")
        hi, lo = ((self.ce_avg, self.pe_avg) if self.diff > 0
                  else (self.pe_avg, self.ce_avg))
        extra = ("Puts carrying higher IV is the NORMAL state — downside "
                 "insurance costs more than upside lottery tickets, and it "
                 "means nothing on its own."
                 if self.leaning == "PUTS" else
                 "Calls carrying higher IV is the UNUSUAL state — demand has "
                 "moved to upside speculation rather than hedging. Worth "
                 "noticing; not tested as predictive.")
        return (f"{self.leaning} priced higher: {hi:.1f} against {lo:.1f} "
                f"({abs(self.diff):.1f} IV points). {extra}")


def iv_demand(chains: dict, centre: int, n: int = 5) -> IVDemand:
    """
    Average IV of the top n call and put strikes, per expiry and overall.

    `chains` maps an expiry label to that expiry's chain frame. Passing a
    single expiry is valid and gives the front-month reading; the point of
    accepting several is that a front-month IV spike can be an expiry
    artefact rather than demand, and comparing expiries separates them.

    Strikes are the n nearest to the centre on each side. Zero and absurd
    IVs are dropped rather than averaged in — NSE publishes 0.0 for strikes
    with no quotes, and letting those into a mean drags it toward nothing.
    """
    out = IVDemand()
    step = getattr(config, "STRIKE_STEP", 50)
    ce_all, pe_all = [], []

    for label, df in (chains or {}).items():
        if df is None or df.empty:
            continue
        up = [centre + i * step for i in range(1, n + 1)]
        dn = [centre - i * step for i in range(1, n + 1)]

        def clean(strikes, col):
            r = df[df.strike.isin(strikes)]
            if not len(r):
                return []
            v = r[col].to_numpy(float)
            return [float(x) for x in v if 1.0 < x < 200.0]

        ce = clean(up, "ce_iv")
        pe = clean(dn, "pe_iv")
        if not ce and not pe:
            continue
        out.per_expiry[label] = {
            "ce": float(np.mean(ce)) if ce else None,
            "pe": float(np.mean(pe)) if pe else None,
            "n_ce": len(ce), "n_pe": len(pe),
        }
        ce_all += ce
        pe_all += pe

    out.ce_avg = float(np.mean(ce_all)) if ce_all else 0.0
    out.pe_avg = float(np.mean(pe_all)) if pe_all else 0.0
    return out


# ── 4b · sampled per-strike VWAP — REMOVED ─────────────────────────────
#
# This module used to accumulate a VWAP approximation from NSE by watching
# cumulative volume move between polls. It has been deleted, not disabled.
#
# Dhan returns real OHLCV per bar for the option contract, so option_vwap.py
# computes VWAP by its actual definition. Once a correct number exists, an
# approximation that is wrong in three known ways — misses trades inside an
# interval, loses volume from before the app started, merges intervals on a
# skipped poll — is not a cheaper alternative. It is a second figure calling
# itself VWAP, and the one nearer to hand is the one that gets read.
#
# Use option_vwap.vwap() for a single strike or option_vwap.watchlist() for
# a small band around the centre.
