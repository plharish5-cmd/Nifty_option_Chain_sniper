"""
basis.py — the spread between NIFTY futures and spot, tracked through the day.

Futures trade at a premium to spot that decays toward zero at expiry — the
cost of carry. That much is arithmetic. What makes the basis worth watching
intraday is that it moves BEFORE spot does, because futures is where size
goes first: it is one contract instead of fifty, it needs less margin than
the cash basket, and it does not move the underlying while you build.

So a basis expanding while spot flatlines means someone is paying up in
futures ahead of a move. A basis collapsing while spot pushes into
resistance means someone is selling into that strength without touching the
cash market.

WHAT THIS IS NOT
────────────────
It is not proof of "smart money". It is a spread, and spreads move for
mundane reasons too — dividend expectations, a roll into the next series
near expiry, a temporary imbalance in one leg's liquidity. Near expiry the
basis compresses toward zero regardless of what anyone thinks, and reading
that decay as bearish distribution would be a straightforward error.

The divergence readings below are therefore stated as OBSERVATIONS with the
alternative explanation attached, not as signals. Nothing here has been
tested against outcomes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import config

DIR = config.DATA_DIR / "basis"


@dataclass
class Basis:
    spot: float
    futures: float
    at: datetime
    days_to_expiry: float | None = None

    @property
    def points(self) -> float:
        return self.futures - self.spot

    @property
    def pct(self) -> float:
        return self.points / self.spot * 100 if self.spot else 0.0

    @property
    def state(self) -> str:
        if self.points > 0:
            return "PREMIUM"
        if self.points < 0:
            return "DISCOUNT"
        return "FLAT"

    @property
    def annualised(self) -> float | None:
        """Carry implied by this basis, for sanity rather than for trading."""
        if not self.days_to_expiry or self.days_to_expiry <= 0:
            return None
        return self.pct * (365.0 / self.days_to_expiry)


def _path(d: date) -> "object":
    return DIR / f"{d.isoformat()}.csv"


def record(spot: float, futures: float, now: datetime | None = None,
           days_to_expiry: float | None = None,
           min_gap_s: int = 60) -> Basis | None:
    """
    Append one basis reading, no more than once a minute.

    The rate limit matters because Streamlit reruns on every click. Without
    it the series fills with duplicate points from the same instant and the
    trend calculation below starts measuring your mouse rather than the
    market.
    """
    now = now or datetime.now()
    b = Basis(float(spot), float(futures), now, days_to_expiry)
    # WEEKEND IMMUNITY: Never store basis data on Saturday (5) or Sunday (6) no matter what!
    if now.weekday() >= 5:
        return b
    DIR.mkdir(parents=True, exist_ok=True)
    p = _path(now.date())

    if p.exists():
        try:
            last = pd.read_csv(p).iloc[-1]
            prev = datetime.fromisoformat(str(last["at"]))
            if (now - prev).total_seconds() < min_gap_s:
                return b
        except Exception:                                   # noqa: BLE001
            pass

    row = pd.DataFrame([{"at": now.isoformat(timespec="seconds"),
                         "spot": round(b.spot, 2),
                         "futures": round(b.futures, 2),
                         "basis": round(b.points, 2)}])
    row.to_csv(p, mode="a", header=not p.exists(), index=False)
    return b


def series(day: date | None = None) -> pd.DataFrame:
    p = _path(day or date.today())
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p)
        df["at"] = pd.to_datetime(df["at"])
        return df
    except Exception:                                       # noqa: BLE001
        return pd.DataFrame()


def divergence(day: date | None = None, window: int = 20,
               spot_flat_pts: float = 15.0,
               basis_move_pts: float = 3.0) -> dict:
    """
    Is the basis moving in a direction spot is not?

    Both thresholds have to be met — the basis must move meaningfully AND
    spot must not. Requiring only the first would flag every ordinary trend,
    where basis and spot rise together and there is no divergence at all,
    just two correlated things doing the same thing.

    `window` is in samples, so at one-minute recording it is roughly the
    last twenty minutes.
    """
    df = series(day)
    if len(df) < max(window, 5):
        return {"known": False,
                "note": f"only {len(df)} readings — need {window}"}

    w = df.tail(window)
    spot_chg = float(w["spot"].iloc[-1] - w["spot"].iloc[0])
    basis_chg = float(w["basis"].iloc[-1] - w["basis"].iloc[0])
    basis_now = float(w["basis"].iloc[-1])
    flat = abs(spot_chg) <= spot_flat_pts

    out = {"known": True, "spot_chg": spot_chg, "basis_chg": basis_chg,
           "basis_now": basis_now, "window": window,
           "diverging": False, "kind": "", "note": ""}

    if abs(basis_chg) < basis_move_pts:
        out["note"] = (f"basis {basis_chg:+.1f} over {window} readings — "
                       f"steady at {basis_now:+.1f}. Nothing to read.")
        return out

    if flat and basis_chg > 0:
        out.update(diverging=True, kind="BASIS EXPANDING, SPOT FLAT", note=(
            f"Basis widened {basis_chg:+.1f} to {basis_now:+.1f} while spot "
            f"moved only {spot_chg:+.1f}. Someone is paying up in futures "
            f"without lifting cash. Often precedes a move up — but a roll "
            f"into the next series does exactly the same thing, so check "
            f"how close expiry is before reading anything into it."))
    elif flat and basis_chg < 0:
        out.update(diverging=True, kind="BASIS COLLAPSING, SPOT FLAT", note=(
            f"Basis narrowed {basis_chg:+.1f} to {basis_now:+.1f} while spot "
            f"held. Futures being sold or hedged without touching spot. Near "
            f"expiry the basis decays to zero on its own — that is carry, "
            f"not selling."))
    elif spot_chg > spot_flat_pts and basis_chg < -basis_move_pts:
        out.update(diverging=True, kind="SPOT UP, BASIS FALLING", note=(
            f"Spot rose {spot_chg:+.0f} but the basis fell {basis_chg:+.1f}. "
            f"The move up is not being confirmed in futures — the classic "
            f"shape of distribution into strength. It is also what happens "
            f"when a cash-heavy buyer lifts the index while futures sits "
            f"still, which is not the same thing at all."))
    elif spot_chg < -spot_flat_pts and basis_chg > basis_move_pts:
        out.update(diverging=True, kind="SPOT DOWN, BASIS RISING", note=(
            f"Spot fell {spot_chg:+.0f} while the basis widened "
            f"{basis_chg:+.1f}. Futures are refusing to follow the decline."))
    else:
        out["note"] = (f"spot {spot_chg:+.0f}, basis {basis_chg:+.1f} — "
                       f"moving together, no divergence")
    return out


def summary(day: date | None = None) -> dict:
    df = series(day)
    if df.empty:
        return {"known": False}
    b = df["basis"]
    return {
        "known": True, "n": len(df),
        "now": float(b.iloc[-1]), "open": float(b.iloc[0]),
        "high": float(b.max()), "low": float(b.min()),
        "mean": float(b.mean()),
        "range": float(b.max() - b.min()),
        "first": df["at"].iloc[0], "last": df["at"].iloc[-1],
    }
