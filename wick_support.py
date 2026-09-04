"""
wick_support.py — previous daily lower wicks as support on a gap-down day.

THE RULE
────────
    "There is a large gap down, around 100-150 points, and it is the lowest
     price in this week or month. Go back on the ONE DAY timeframe, look at
     the lower wicks. It should have rejected and price went up. Mark that
     lower wick as a rectangle drawn across to the current day's price — that
     acts as support, and you will see the rejection of the bearishness."

Three conditions have to hold together, and the app is well placed to check
all three because it already carries seven years of daily bars:

  1. A large gap down — not any red open.
  2. At or near the lowest price of the week or month, not mid-range.
  3. A prior daily candle with a long lower wick that REJECTED — meaning
     price actually turned up from it afterwards.

WHY THE REJECTION TEST MATTERS MORE THAN THE WICK
─────────────────────────────────────────────────
Long lower wicks are common. Long lower wicks that MARKED A TURN are not.
Selecting on the wick alone would hand you a dozen lines a month, most of
them places where price paused and then carried on down — which is a
support zone in appearance only.

So a wick only becomes a zone here if price closed higher over the following
days. That is a backward-looking test and it is allowed to be: the zone is
being drawn from history onto today, exactly as the rule describes.

WHAT IT DOES NOT DO
───────────────────
It does not say buy. A zone is where a bounce has a reason to happen, not a
reason for it to happen today. The gap rule already says a gap-down day is a
CE-hunting day; this narrows WHERE, and the entry still has to come from a
candle close and a follow-through like any other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

import config


@dataclass
class WickZone:
    session: date
    low: float                 # bottom of the wick — the rectangle's floor
    top: float                 # body bottom — the rectangle's ceiling
    wick_pts: float
    body_pts: float
    bounce_pts: float          # how far price rose afterwards
    confirm_days: int

    @property
    def mid(self) -> float:
        return (self.low + self.top) / 2

    @property
    def height(self) -> float:
        return self.top - self.low

    def contains(self, price: float, pad: float = 0.0) -> bool:
        return (self.low - pad) <= price <= (self.top + pad)

    @property
    def label(self) -> str:
        return (f"{self.session:%d-%b} · {self.low:,.0f}–{self.top:,.0f} "
                f"({self.height:.0f} pts wide) · wick {self.wick_pts:.0f} pts "
                f"· bounced {self.bounce_pts:.0f} pts over "
                f"{self.confirm_days}d")


def rejection_wicks(daily: pd.DataFrame, lookback: int = 120,
                    min_wick_ratio: float = 1.3,
                    min_wick_pts: float = 40.0,
                    confirm_days: int = 3,
                    min_bounce_pts: float = 60.0) -> list[WickZone]:
    """
    Daily candles whose lower wick marked a turn.

    `min_wick_ratio` compares the lower wick to the candle's body. A wick
    longer than the body is the visual the rule describes — a candle that
    went down, was rejected, and closed well off its low.

    `min_bounce_pts` over `confirm_days` is what separates a rejection from
    a pause. Without it this returns every candle with a tail, and a list
    that long is the same as no list at all.
    """
    if daily is None or len(daily) < confirm_days + 2:
        return []
    d = daily.tail(lookback + confirm_days).reset_index(drop=True)
    o = d["Open"].to_numpy(float)
    h = d["High"].to_numpy(float)
    l = d["Low"].to_numpy(float)
    c = d["Close"].to_numpy(float)
    dates = pd.to_datetime(d["Date"]).dt.date.to_numpy()

    out: list[WickZone] = []
    for i in range(len(d) - confirm_days):
        body_lo = min(o[i], c[i])
        wick = body_lo - l[i]
        body = abs(c[i] - o[i])
        if wick < min_wick_pts:
            continue
        if body > 0 and wick < body * min_wick_ratio:
            continue

        fwd_high = float(np.nanmax(h[i + 1:i + 1 + confirm_days]))
        bounce = fwd_high - l[i]
        held = float(np.nanmin(l[i + 1:i + 1 + confirm_days])) >= l[i]
        if bounce < min_bounce_pts or not held:
            continue

        out.append(WickZone(dates[i], float(l[i]), float(body_lo),
                            float(wick), float(body), float(bounce),
                            confirm_days))
    return out


def is_extreme_low(daily: pd.DataFrame, price: float,
                   window: int = 22) -> tuple[bool, str]:
    """
    Is this price at or near the low of the recent window?

    The rule applies only when the gap down lands at the lowest price of the
    week or month. Twenty-two sessions is roughly a month. Mid-range gap
    downs are a different situation and this zone logic does not apply to
    them.
    """
    if daily is None or len(daily) < 5:
        return False, "not enough daily history"
    w = daily.tail(window)
    lo = float(w["Low"].min())
    if price <= lo * 1.002:
        return True, (f"at the {len(w)}-session low ({lo:,.0f}) — the rule "
                      f"applies")
    gap = (price - lo) / lo * 100
    return False, (f"{gap:.1f}% above the {len(w)}-session low ({lo:,.0f}). "
                   f"This rule is for a gap down AT the weekly or monthly "
                   f"low; mid-range gap downs are a different situation.")


def check(daily: pd.DataFrame, spot: float, prev_close: float | None,
          gap_pts_min: float = 100.0, near: float = 40.0) -> dict:
    """
    The whole rule in one call: gap, extreme, and any zone price is entering.

    Returns a dict rather than a verdict. Every condition is reported
    separately so a partial match reads as a partial match — two of three is
    not the setup, and collapsing them into one boolean would hide which
    part was missing.
    """
    out = {"applies": False, "gap": None, "reasons": [], "zones": [],
           "in_zone": None, "approaching": []}

    if prev_close:
        gap = spot - prev_close
        out["gap"] = gap
        if gap <= -abs(gap_pts_min):
            out["reasons"].append(f"gap down {gap:+.0f} pts — large enough")
        else:
            out["reasons"].append(
                f"gap {gap:+.0f} pts — the rule wants a gap DOWN of at least "
                f"{gap_pts_min:.0f}")
            return out
    else:
        out["reasons"].append("no previous close supplied — gap unknown")
        return out

    ok_low, why = is_extreme_low(daily, spot)
    out["reasons"].append(why)
    if not ok_low:
        return out

    zones = rejection_wicks(daily)
    below = [z for z in zones if z.top <= spot + near]
    below.sort(key=lambda z: abs(spot - z.mid))
    out["zones"] = below[:4]

    for z in below:
        if z.contains(spot, pad=5.0):
            out["in_zone"] = z
            break
    out["approaching"] = [z for z in below[:3]
                          if not z.contains(spot) and 0 < spot - z.top <= near]

    out["applies"] = bool(out["zones"])
    if not out["zones"]:
        out["reasons"].append(
            "no prior daily rejection wick below this price in the last 120 "
            "sessions — there is no historical zone to lean on here")
    return out
