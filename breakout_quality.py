"""
breakout_quality.py — is this break backed by the chain, or just price?

WHAT THE APP ALREADY DOES, SO THIS DOES NOT REPEAT IT
────────────────────────────────────────────────────
market_state.breakout() classifies a level break as REAL / POSSIBLE /
UNCONFIRMED / FAILED / FAKE from price and volume, including the check that
separates a trap from a squeeze — whether writers ADDED OI at the level.
session_levels.breaks() does close-plus-follow-through on the day extremes.

Neither looks at how the chain CHANGED across the break. That is what this
adds, and it needs two snapshots rather than one.

THE FOUR CHECKS
───────────────
  1  IV trajectory. Expanding IV means participants are paying up. A break
     into flat or falling IV is price moving without anyone bidding for it.

  2  The wall being broken is surrendering. At a resistance break, call
     writers UNWINDING is bullish — the wall is being removed.

  3  The far side is building. Puts stacking below a resistance break means
     a floor is being placed under the new range.

  4  The wall is migrating, not just thinning. Real acceptance shows OI
     appearing at the next strike out, not merely leaving the old one.

A CORRECTION TO THE COMMON FORMULATION
──────────────────────────────────────
"Price up on falling OI means short covering, therefore fakeout" is a
FUTURES rule, where open interest is a single number. An option chain has
two sides and they mean opposite things.

At a resistance break:
    CALL OI falling  = writers surrendering        = BULLISH
    PUT  OI rising   = floor being built           = BULLISH
    CALL OI rising   = writers reinforcing         = BEARISH, the wall holds

Applying the futures rule here would score a genuine breakout as a fakeout
precisely because the wall was being dismantled. The direction of the check
depends on which side owns the level, so this module always takes
`level_type` and never sums the two sides.

WHAT IT CANNOT DO
─────────────────
Two snapshots three minutes apart is a small sample. A single poll of call
unwinding is not surrender; it is one poll. `polls` is reported with every
verdict so a two-snapshot reading is never mistaken for a trend, and the
verdict is withheld entirely until there is a prior snapshot to compare.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

import config


@dataclass
class Check:
    name: str
    passed: bool | None          # None = could not be evaluated
    detail: str
    weight: int = 1


@dataclass
class Quality:
    level: float
    level_type: str              # RESISTANCE | SUPPORT
    checks: list = field(default_factory=list)
    polls: int = 0
    minutes: float = 0.0

    @property
    def scored(self) -> list:
        return [c for c in self.checks if c.passed is not None]

    @property
    def score(self) -> int:
        return sum(c.weight for c in self.scored if c.passed)

    @property
    def possible(self) -> int:
        return sum(c.weight for c in self.scored)

    @property
    def verdict(self) -> str:
        if self.polls < 2:
            return "NO PRIOR SNAPSHOT"
        if not self.scored:
            return "NOTHING MEASURABLE"
        if self.score >= 3:
            return "BACKED BY THE CHAIN"
        if self.score <= 0:
            return "NOT BACKED — treat as suspect"
        return "PARTLY BACKED"

    @property
    def colour(self) -> str:
        return {"BACKED BY THE CHAIN": "#0F9D58",
                "NOT BACKED — treat as suspect": "#E8503A",
                "PARTLY BACKED": "#E65100"}.get(self.verdict, "#8A8F98")

    @property
    def note(self) -> str:
        if self.polls < 2:
            return ("Only one snapshot so far. This compares the chain "
                    "ACROSS a break, so it needs a previous poll — nothing "
                    "can be said yet.")
        base = (f"{self.score} of {self.possible} checks pass, across "
                f"{self.polls} snapshots spanning {self.minutes:.0f} minutes.")
        if self.polls <= 2:
            base += (" That is a single interval. One poll of writers "
                     "unwinding is one poll, not a surrender — read it as a "
                     "first reading rather than a trend.")
        return base


def _side_oi(df: pd.DataFrame, strike: float, side: str) -> float | None:
    row = df[df.strike == strike]
    if row.empty:
        return None
    v = row.iloc[0].get(f"{side}_oi")
    return float(v) if v == v else None


def _atm_iv(df: pd.DataFrame, spot: float, n: int = 3) -> float | None:
    """Mean of usable ATM-area IVs. Zeros and absurd values dropped."""
    step = getattr(config, "STRIKE_STEP", 50)
    atm = round(spot / step) * step
    near = df[(df.strike >= atm - n * step) & (df.strike <= atm + n * step)]
    vals = []
    for col in ("ce_iv", "pe_iv"):
        if col in near.columns:
            vals += [float(x) for x in near[col].to_numpy()
                     if x == x and 1.0 < float(x) < 200.0]
    return float(np.mean(vals)) if vals else None


def evaluate(curr: pd.DataFrame, prev: pd.DataFrame, spot: float,
             level: float, level_type: str = "RESISTANCE",
             polls: int = 2, minutes: float = 3.0,
             iv_move: float = 0.2, oi_move_pct: float = 3.0) -> Quality:
    """
    Compare two chain snapshots across a level break.

    `prev` should be the snapshot from before price reached the level, not
    merely the previous poll — comparing two snapshots both taken after the
    break measures nothing about the break itself. The caller chooses.
    """
    q = Quality(float(level), level_type.upper(), polls=polls,
                minutes=minutes)
    if prev is None or curr is None or prev.empty or curr.empty:
        # polls is forced to 1 so the verdict reads NO PRIOR SNAPSHOT
        # rather than NOTHING MEASURABLE. Those look similar and are not:
        # the first means come back after another poll, the second means
        # the data is there and unusable. Sending you to check the chain
        # when you only had to wait three minutes is a wasted trip.
        q.polls = 1
        return q

    up = level_type.upper() == "RESISTANCE"
    step = getattr(config, "STRIKE_STEP", 50)
    wall_side = "ce" if up else "pe"       # the side that owns the level
    far_side = "pe" if up else "ce"
    next_strike = level + step if up else level - step

    # 1 · IV trajectory
    iv_now, iv_then = _atm_iv(curr, spot), _atm_iv(prev, spot)
    if iv_now is None or iv_then is None:
        q.checks.append(Check("IV expanding", None,
                              "no usable IV in the ATM band"))
    else:
        d = iv_now - iv_then
        q.checks.append(Check(
            "IV expanding", d > iv_move,
            f"{iv_then:.2f} to {iv_now:.2f} ({d:+.2f}) — "
            + ("participants paying up" if d > iv_move else
               "flat or falling; price is moving with nobody bidding for it"
               if d < -iv_move else "flat")))

    # 2 · the wall surrendering
    w_now, w_then = (_side_oi(curr, level, wall_side),
                     _side_oi(prev, level, wall_side))
    if w_now is None or w_then is None:
        q.checks.append(Check(f"{wall_side.upper()} writers unwinding", None,
                              f"strike {level:,.0f} not in one snapshot"))
    else:
        pct = (w_now - w_then) / w_then * 100 if w_then else 0.0
        q.checks.append(Check(
            f"{wall_side.upper()} writers unwinding at {level:,.0f}",
            pct < -oi_move_pct,
            f"{w_then:,.0f} to {w_now:,.0f} ({pct:+.1f}%) — "
            + ("writers leaving, the wall is being removed" if pct < -oi_move_pct
               else "writers REINFORCING — the wall is being defended"
               if pct > oi_move_pct else "barely changed")))

    # 3 · the far side building
    f_now, f_then = (_side_oi(curr, level, far_side),
                     _side_oi(prev, level, far_side))
    if f_now is None or f_then is None:
        q.checks.append(Check(f"{far_side.upper()} building", None,
                              "strike missing"))
    else:
        pct = (f_now - f_then) / f_then * 100 if f_then else 0.0
        q.checks.append(Check(
            f"{far_side.upper()} building at {level:,.0f}", pct > oi_move_pct,
            f"{f_then:,.0f} to {f_now:,.0f} ({pct:+.1f}%) — "
            + ("a floor is being placed behind the move" if up else
               "a ceiling is being placed behind the move")
            if pct > oi_move_pct else f"{pct:+.1f}%, not building"))

    # 4 · migration, not just thinning
    n_now, n_then = (_side_oi(curr, next_strike, wall_side),
                     _side_oi(prev, next_strike, wall_side))
    if n_now is None or n_then is None:
        q.checks.append(Check(f"wall migrating to {next_strike:,.0f}", None,
                              "next strike missing from one snapshot"))
    else:
        pct = (n_now - n_then) / n_then * 100 if n_then else 0.0
        q.checks.append(Check(
            f"wall migrating to {next_strike:,.0f}", pct > oi_move_pct,
            f"{n_then:,.0f} to {n_now:,.0f} ({pct:+.1f}%) — "
            + ("writers have moved their line out; the new zone is accepted"
               if pct > oi_move_pct else
               "no new wall forming out there. The old one thinning without "
               "a new one appearing is unwinding, not acceptance")))
    return q


def from_snapshots(snaps: list, spot: float, level: float,
                   level_type: str = "RESISTANCE",
                   lookback: int = 3) -> Quality | None:
    """
    Convenience wrapper over store.load_snapshots().

    Compares the newest snapshot against one `lookback` polls earlier rather
    than the immediately previous one. Three-minute polling makes a
    single-interval comparison very noisy, and the question — did the chain
    change across this break — is about the move, not about one poll.
    """
    if not snaps or len(snaps) < 2:
        return None
    curr_ts, _, curr = snaps[-1]
    i = max(0, len(snaps) - 1 - lookback)
    prev_ts, _, prev = snaps[i]
    mins = abs((curr_ts - prev_ts).total_seconds()) / 60.0
    return evaluate(curr, prev, spot, level, level_type,
                    polls=len(snaps) - i, minutes=mins)
