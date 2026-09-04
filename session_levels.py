"""
session_levels.py — the structure of today, and the break of it.

WHY THIS EXISTS
───────────────
The app already prints Day High and Day Low as numbers. It has never watched
what happens WHEN PRICE REACHES THEM, and that is the setup MJ demonstrated
three times in one session and called the whole system:

    Mark Day High and Day Low on the 15-minute.
    Wait for a candle to CLOSE beyond one of them.
    The NEXT candle must stay beyond. That is the entry.

    "Candle close, follow through — it works. Then candle close below,
     follow through — even that worked. Again close above, follow through —
     even that has worked. 20, 20, 20 points."

Everything in the named setups is a refinement of this. It was missing.

Three more things from the same notes are built here because they share the
same data and are meaningless apart from it:

  MORNING WICK ZONES. The first candle's high and low become the sellers'
  and buyers' zones for the rest of the session. "Every day when Nifty
  creates wicks, throughout the day most of the time it will take supports
  over there."

  THE EXTREMES GATE. "Don't attend a trade that comes near the Day High or
  Day Low. If you trade at the extreme, most days it will be a loss."

  WHICH END IS PENDING. Both ends print on most days. If the Day Low came at
  10:00 and it is now 14:00 with no new high, the high is still owed —
  "it may come at 3:15, 3:20 also, but it'll come."

WHAT THIS DELIBERATELY DOES NOT DO
──────────────────────────────────
It does not tell you to enter. A REAL break is a state, not an instruction:
the gap rule, the rally-already-over check, the trade count and the band
slope all still apply, and they live elsewhere. This module answers one
question — did the level break properly or not — and leaves the rest alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

import numpy as np
import pandas as pd


def _times(df: pd.DataFrame) -> pd.Series:
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    return pd.Series(pd.to_datetime(df.index), index=df.index)


# ═══════════════════════════════════════════════════════════════════════════
# Day High / Day Low
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Extreme:
    kind: str                      # HIGH | LOW
    price: float
    at: datetime
    candle_i: int
    minutes_ago: float

    @property
    def label(self) -> str:
        return (f"Day {self.kind.lower()} {self.price:,.1f} — made by the "
                f"{self.at:%H:%M} candle, {self.minutes_ago:.0f} min ago")


def extremes(df: pd.DataFrame, now: datetime | None = None
             ) -> tuple[Extreme | None, Extreme | None]:
    """
    The candles that MADE today's high and low — not the tallest bodies.

    "Not the highest body on screen. The Day High Candle is the candle that
    MADE today's high." The distinction matters when you go to mark the
    chart: it is the wick that defines the level.
    """
    if df is None or df.empty:
        return None, None
    now = now or datetime.now()
    ts = _times(df)
    hi_i = int(np.nanargmax(df["high"].to_numpy()))
    lo_i = int(np.nanargmin(df["low"].to_numpy()))

    def mk(kind, i, col):
        at = ts.iloc[i].to_pydatetime()
        return Extreme(kind, float(df[col].iloc[i]), at, i,
                       max((now - at).total_seconds() / 60.0, 0.0))

    return mk("HIGH", hi_i, "high"), mk("LOW", lo_i, "low")


def pending_end(hi: Extreme | None, lo: Extreme | None,
                now: datetime | None = None) -> str:
    """
    Which end of the day has not printed recently.

    Both ends arrive on most sessions. If one was made in the first hour and
    nothing has challenged it since, the other is the one still owed — and
    it can arrive very late, which is precisely when people have given up on
    the day.
    """
    if hi is None or lo is None:
        return ""
    now = now or datetime.now()
    late = now.time() >= time(14, 0)
    if hi.minutes_ago >= lo.minutes_ago + 60:
        return ("The day HIGH is old and the LOW is recent — the high is the "
                "end still owed." + (" It can print at 15:15 or later."
                                     if late else ""))
    if lo.minutes_ago >= hi.minutes_ago + 60:
        return ("The day LOW is old and the HIGH is recent — the low is the "
                "end still owed." + (" It can print at 15:15 or later."
                                     if late else ""))
    return "Both ends are recent — no clear pending side."


# ═══════════════════════════════════════════════════════════════════════════
# Close + follow-through
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Break:
    side: str                      # ABOVE DAY HIGH | BELOW DAY LOW
    level: float
    close_i: int
    close_at: datetime
    state: str                     # WAITING | REAL | FAILED
    note: str = ""

    @property
    def direction(self) -> str:
        return "CE" if self.side.startswith("ABOVE") else "PE"


def breaks(df: pd.DataFrame, lookback: int = 12) -> list[Break]:
    """
    Every close beyond an extreme, and whether the next candle held it.

    The level used is the high or low as it stood BEFORE the breaking candle.
    Using today's final extreme would make the test circular — the candle
    that broke the high would then be compared against a high it had already
    set, and nothing could ever qualify.

    A break with no candle after it yet is WAITING, not REAL. Front-running
    it is the mistake the rule exists to prevent.
    """
    if df is None or len(df) < 3:
        return []
    ts = _times(df)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    out = []
    start = max(2, len(df) - lookback)

    for i in range(start, len(df)):
        prior_hi = float(np.nanmax(h[:i]))
        prior_lo = float(np.nanmin(l[:i]))
        at = ts.iloc[i].to_pydatetime()

        for side, lvl, beyond in (("ABOVE DAY HIGH", prior_hi, c[i] > prior_hi),
                                  ("BELOW DAY LOW", prior_lo, c[i] < prior_lo)):
            if not beyond:
                continue
            if i + 1 >= len(df):
                out.append(Break(side, lvl, i, at, "WAITING",
                                 "closed beyond — the next candle decides"))
                continue
            nxt = c[i + 1]
            held = nxt > lvl if side.startswith("ABOVE") else nxt < lvl
            out.append(Break(
                side, lvl, i, at, "REAL" if held else "FAILED",
                ("next candle held beyond the level — follow-through confirmed"
                 if held else
                 "next candle closed back inside — no follow-through, this is "
                 "the fake the rule is built to catch")))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Morning wick zones
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Zones:
    sellers: float | None = None
    buyers: float | None = None
    source_at: datetime | None = None
    tested: int = 0

    @property
    def width(self) -> float | None:
        if self.sellers is None or self.buyers is None:
            return None
        return self.sellers - self.buyers


def wick_zones(df: pd.DataFrame, tolerance: float = 12.0) -> Zones:
    """
    The first candle's high and low, and how often price came back to them.

    These are zones for the whole session, not just the opening. The count
    of revisits is the useful part: a level touched three times and held is
    doing real work, while one touched once is a coincidence you should not
    be trading against.

    Note the first candle is REFERENCE, not a trade — Sushen is explicit that
    its high and low are not to be traded on the break. These zones are for
    knowing where you are, and for refusing to buy calls into the sellers'
    side.
    """
    if df is None or df.empty:
        return Zones()
    ts = _times(df)
    z = Zones(float(df["high"].iloc[0]), float(df["low"].iloc[0]),
              ts.iloc[0].to_pydatetime())
    if len(df) > 1:
        h = df["high"].to_numpy(float)[1:]
        l = df["low"].to_numpy(float)[1:]
        z.tested = int(np.sum(np.abs(h - z.sellers) <= tolerance)
                       + np.sum(np.abs(l - z.buyers) <= tolerance))
    return z


def zone_read(price: float, z: Zones) -> str:
    if z.sellers is None:
        return ""
    if price >= z.sellers:
        return (f"above the sellers' zone ({z.sellers:,.0f}) — it is support "
                f"now, not resistance")
    if price <= z.buyers:
        return (f"below the buyers' zone ({z.buyers:,.0f}) — it is resistance "
                f"now, not support")
    return (f"inside the opening range {z.buyers:,.0f}–{z.sellers:,.0f}"
            + (f", tested {z.tested}x" if z.tested else ""))


# ═══════════════════════════════════════════════════════════════════════════
# The extremes gate
# ═══════════════════════════════════════════════════════════════════════════

def near_extreme(price: float, hi: Extreme | None, lo: Extreme | None,
                 within: float = 25.0) -> tuple[bool, str]:
    """
    Is a setup here sitting on top of the day's high or low?

    "Don't attend a trade that comes near the Day High / Day Low. If you
    trade at the extreme, most days it will be a loss."

    Returns True when price is within `within` points of either end. This is
    a warning, not a block — the same proximity that ruins a continuation
    trade is what a reversal trade needs, and this module cannot tell which
    one you are looking at.
    """
    bits = []
    if hi is not None and abs(price - hi.price) <= within:
        bits.append(f"{abs(price - hi.price):.0f} pts from the day high "
                    f"({hi.price:,.0f})")
    if lo is not None and abs(price - lo.price) <= within:
        bits.append(f"{abs(price - lo.price):.0f} pts from the day low "
                    f"({lo.price:,.0f})")
    if not bits:
        return False, ""
    return True, ("Price is " + " and ".join(bits)
                  + ". A continuation setup taken here usually loses — "
                    "most days the extreme holds. A reversal setup is a "
                    "different question and this check cannot tell them "
                    "apart.")
