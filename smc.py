"""
smc.py — Smart Money Concepts, computed strictly.

DELIBERATELY SEPARATE FROM STRATEGY 1
─────────────────────────────────────
This is a different school. Strategy 1 says: candle closes above the 8 EMA,
follow-through confirms, enter long. SMC says: a wick above a swing high
that closes back inside is a stop hunt, enter short. Given the same candle
those two produce opposite instructions.

So this lives in its own module and its own tab, and nothing here feeds the
decision bar. Run both, log both in the Timeline, and after some weeks the
outcome scoring will tell you which suits your market. Mixing them on one
screen would only produce hesitation.

THE PIVOT CONFIRMATION LAG — THE MOST IMPORTANT THING IN THIS FILE
──────────────────────────────────────────────────────────────────
The standard swing definition is:

    Swing High at t:  High[t] > max(High[t-N..t-1]) and High[t] > max(High[t+1..t+N])

That second clause reads N bars INTO THE FUTURE relative to bar t. The
definition is fine; using it carelessly is not. A pivot at bar t cannot be
known until bar t+N has closed, so anything that treats it as available at
bar t is using information that did not exist.

That single mistake makes a backtest look superb and a live session look
nothing like it, because in a backtest the future bars are already sitting
in the array.

Every pivot here therefore carries `confirmed_at = t + N`, and
`active_at(bar)` refuses to return a pivot before its confirmation bar.
Structure, sweeps and order blocks all go through that filter. The cost is
that on a 15-minute chart with N=2 a pivot is known 30 minutes after it
formed — which is simply the truth about fractal pivots, not a defect to be
engineered away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd

Trend = Literal["BULLISH", "BEARISH", "TRANSITION"]


def _times(df: pd.DataFrame) -> pd.Series:
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    return pd.Series(pd.to_datetime(df.index), index=df.index)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Swing pivots
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Pivot:
    kind: str                     # HIGH | LOW
    i: int                        # bar it formed on
    price: float
    at: datetime
    confirmed_i: int              # bar after which it is knowable
    violated_i: int | None = None
    subkind: str = ""             # HH | LH | HL | LL

    def known_at(self, bar: int) -> bool:
        return bar >= self.confirmed_i

    def active_at(self, bar: int) -> bool:
        """
        Knowable, and still standing AS OF the start of `bar`.

        The comparison is `bar <= violated_i`, not `<`. That off-by-one is
        not cosmetic: with `<`, a pivot stopped being active on the exact
        bar that broke it, so the break had no level to break. Structure
        stayed permanently in TRANSITION and no liquidity RUN could ever be
        detected — both were found by a test that hand-built two closes
        above a swing high and got nothing back.

        A level is live until the close that takes it out, inclusive.
        """
        if not self.known_at(bar):
            return False
        return self.violated_i is None or bar <= self.violated_i


def pivots(df: pd.DataFrame, n: int = 2) -> list[Pivot]:
    """
    Fractal highs and lows, each stamped with the bar that confirms it.

    Strict inequality on both sides. A high equal to a neighbour is not a
    pivot: on a quiet 15-minute chart equal highs are common, and admitting
    them produces clusters of pivots at the same price that then generate a
    cluster of structure breaks from one move.
    """
    if df is None or len(df) < 2 * n + 1:
        return []
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    out: list[Pivot] = []

    for i in range(n, len(df) - n):
        left_h, right_h = h[i - n:i], h[i + 1:i + 1 + n]
        if h[i] > left_h.max() and h[i] > right_h.max():
            out.append(Pivot("HIGH", i, float(h[i]), ts[i], i + n))
        left_l, right_l = l[i - n:i], l[i + 1:i + 1 + n]
        if l[i] < left_l.min() and l[i] < right_l.min():
            out.append(Pivot("LOW", i, float(l[i]), ts[i], i + n))

    out = sorted(out, key=lambda p: p.i)

    # Classify structural swings (HH, LH, HL, LL)
    last_h: float | None = None
    last_l: float | None = None
    for p in out:
        if p.kind == "HIGH":
            if last_h is not None:
                p.subkind = "HH" if p.price > last_h else "LH"
            else:
                p.subkind = "HIGH"
            last_h = p.price
        elif p.kind == "LOW":
            if last_l is not None:
                p.subkind = "HL" if p.price > last_l else "LL"
            else:
                p.subkind = "LOW"
            last_l = p.price

    # mark violations, so a broken level stops being offered as active
    c = df["close"].astype(float).to_numpy()
    for p in out:
        for j in range(p.confirmed_i, len(df)):
            if (p.kind == "HIGH" and c[j] > p.price) or \
               (p.kind == "LOW" and c[j] < p.price):
                p.violated_i = j
                break
    return out


def active(ps: list[Pivot], bar: int, kind: str) -> Pivot | None:
    """Most recent unviolated pivot of `kind` knowable at `bar`."""
    cands = [p for p in ps if p.kind == kind and p.active_at(bar)]
    return cands[-1] if cands else None


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Structure: BOS, CHoCH / MSS & Trendlines
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StructureEvent:
    kind: str                     # BOS | CHOCH | MSS
    direction: str                # BULLISH | BEARISH
    i: int
    at: datetime
    level: float
    close: float
    origin_level: float | None = None
    origin_i: int | None = None
    is_mss: bool = False

    @property
    def label(self) -> str:
        tag = "MSS" if self.is_mss else self.kind
        return f"{self.direction} {tag} @ {self.level:,.1f}"


@dataclass
class Structure:
    trend: Trend = "TRANSITION"
    events: list = field(default_factory=list)
    bars_since_event: int | None = None

    @property
    def last(self) -> StructureEvent | None:
        return self.events[-1] if self.events else None


def structure(df: pd.DataFrame, ps: list[Pivot]) -> Structure:
    """
    Walk the bars, tracking trend, emitting BOS and CHoCH / MSS.

    Starts in TRANSITION rather than guessing a direction. The first break
    of a confirmed pivot sets the trend — before that there is genuinely no
    structure, and a default of BULLISH would manufacture one.
    """
    st = Structure()
    if df is None or df.empty or not ps:
        return st
    c = df["close"].astype(float).to_numpy()
    ts = _times(df).tolist()

    for i in range(len(df)):
        sh = active(ps, i, "HIGH")
        sl = active(ps, i, "LOW")

        if sh is not None and c[i] > sh.price:
            if st.trend == "BEARISH":
                st.events.append(StructureEvent(
                    "CHOCH", "BULLISH", i, ts[i], sh.price, float(c[i]),
                    origin_level=sl.price if sl else None,
                    origin_i=sl.i if sl else None,
                    is_mss=True))
                st.trend = "BULLISH"
            elif st.trend in ("BULLISH", "TRANSITION"):
                st.events.append(StructureEvent(
                    "BOS", "BULLISH", i, ts[i], sh.price, float(c[i]),
                    origin_level=sl.price if sl else None,
                    origin_i=sl.i if sl else None,
                    is_mss=False))
                st.trend = "BULLISH"
        elif sl is not None and c[i] < sl.price:
            if st.trend == "BULLISH":
                st.events.append(StructureEvent(
                    "CHOCH", "BEARISH", i, ts[i], sl.price, float(c[i]),
                    origin_level=sh.price if sh else None,
                    origin_i=sh.i if sh else None,
                    is_mss=True))
                st.trend = "BEARISH"
            elif st.trend in ("BEARISH", "TRANSITION"):
                st.events.append(StructureEvent(
                    "BOS", "BEARISH", i, ts[i], sl.price, float(c[i]),
                    origin_level=sh.price if sh else None,
                    origin_i=sh.i if sh else None,
                    is_mss=False))
                st.trend = "BEARISH"

    if st.events:
        st.bars_since_event = len(df) - 1 - st.events[-1].i
    return st


@dataclass
class Trendline:
    kind: str                     # SUPPORT | RESISTANCE
    start_i: int
    start_at: datetime
    start_price: float
    end_i: int
    end_at: datetime
    end_price: float
    slope: float                  # points per bar
    projected_price: float        # Price projected at the latest bar
    is_active: bool = True
    pivots_count: int = 2
    label: str = ""


def calculate_trendlines(df: pd.DataFrame, ps: list[Pivot]) -> dict[str, Trendline | None]:
    """
    Algorithmically draws:
      - Support Trendline: connecting recent Higher Lows (HLs) in ascending trajectory
      - Resistance Trendline: connecting recent Lower Highs (LHs) in descending trajectory
    Projected forward to the current bar with active/violated state.
    """
    if df is None or len(df) < 5 or not ps:
        return {"support": None, "resistance": None}

    c = df["close"].astype(float).to_numpy()
    last_i = len(df) - 1

    # 1. Support Trendline (Connecting HLs)
    support_tl = None
    hls = [p for p in ps if p.kind == "LOW" and p.subkind == "HL"]
    if len(hls) < 2:
        lows = [p for p in ps if p.kind == "LOW"]
        if len(lows) >= 2 and lows[-1].price > lows[-2].price:
            hls = [lows[-2], lows[-1]]

    if len(hls) >= 2:
        p1, p2 = hls[-2], hls[-1]
        if p2.i > p1.i and p2.price > p1.price:
            slope = (p2.price - p1.price) / (p2.i - p1.i)
            proj = p2.price + slope * (last_i - p2.i)
            is_active = True
            for k in range(p2.i + 1, len(df)):
                curr_line_val = p2.price + slope * (k - p2.i)
                if c[k] < curr_line_val - 6.0:
                    is_active = False
                    break
            support_tl = Trendline(
                kind="SUPPORT",
                start_i=p1.i, start_at=p1.at, start_price=p1.price,
                end_i=p2.i, end_at=p2.at, end_price=p2.price,
                slope=slope, projected_price=round(proj, 1),
                is_active=is_active, pivots_count=len(hls),
                label=f"Support Trendline (HLs @ {p1.price:,.0f} & {p2.price:,.0f})"
            )

    # 2. Resistance Trendline (Connecting LHs)
    resistance_tl = None
    lhs = [p for p in ps if p.kind == "HIGH" and p.subkind == "LH"]
    if len(lhs) < 2:
        highs = [p for p in ps if p.kind == "HIGH"]
        if len(highs) >= 2 and highs[-1].price < highs[-2].price:
            lhs = [highs[-2], highs[-1]]

    if len(lhs) >= 2:
        p1, p2 = lhs[-2], lhs[-1]
        if p2.i > p1.i and p2.price < p1.price:
            slope = (p2.price - p1.price) / (p2.i - p1.i)
            proj = p2.price + slope * (last_i - p2.i)
            is_active = True
            for k in range(p2.i + 1, len(df)):
                curr_line_val = p2.price + slope * (k - p2.i)
                if c[k] > curr_line_val + 6.0:
                    is_active = False
                    break
            resistance_tl = Trendline(
                kind="RESISTANCE",
                start_i=p1.i, start_at=p1.at, start_price=p1.price,
                end_i=p2.i, end_at=p2.at, end_price=p2.price,
                slope=slope, projected_price=round(proj, 1),
                is_active=is_active, pivots_count=len(lhs),
                label=f"Resistance Trendline (LHs @ {p1.price:,.0f} & {p2.price:,.0f})"
            )

    return {"support": support_tl, "resistance": resistance_tl}


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Sweeps and runs
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Liquidity:
    kind: str                     # SWEEP | RUN
    direction: str                # ABOVE | BELOW
    i: int
    at: datetime
    level: float
    wick: float
    close: float

    @property
    def note(self) -> str:
        if self.kind == "SWEEP":
            return (f"wick {self.wick:,.1f} took out {self.level:,.1f} but "
                    f"closed {self.close:,.1f} back inside — stops hunted, "
                    f"value not accepted")
        return (f"two consecutive closes beyond {self.level:,.1f} — "
                f"acceptance, not a hunt")


def liquidity(df: pd.DataFrame, ps: list[Pivot]) -> list[Liquidity]:
    """
    Sweeps (wick through, close back inside) and runs (two closes beyond).

    A run needs bar i+1, so the newest bar can never produce one — it is
    reported only once the following candle has closed. The same restraint
    as the follow-through rule in Strategy 1, arrived at independently.
    """
    if df is None or len(df) < 2 or not ps:
        return []
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    ts = _times(df).tolist()
    out = []

    consumed_sweeps: set[float] = set()
    for i in range(len(df)):
        sh = active(ps, i, "HIGH")
        sweep_above = False
        run_above = False
        if sh is not None:
            sh_key = round(sh.price, 1)
            if h[i] > sh.price and c[i] <= sh.price and sh_key not in consumed_sweeps:
                sweep_above = True
            elif (c[i] > sh.price and i + 1 < len(df) and c[i + 1] > sh.price):
                run_above = True

        sl = active(ps, i, "LOW")
        sweep_below = False
        run_below = False
        if sl is not None:
            sl_key = round(sl.price, 1)
            if l[i] < sl.price and c[i] >= sl.price and sl_key not in consumed_sweeps:
                sweep_below = True
            elif (c[i] < sl.price and i + 1 < len(df) and c[i + 1] < sl.price):
                run_below = True

        # Conflict resolution on single candle:
        # If a candle dips below a low (sweep) but expands to close strongly above a high (run),
        # prioritize closing displacement (RUN) as the accepted market direction.
        if run_above and sweep_below:
            out.append(Liquidity("RUN", "ABOVE", i, ts[i], sh.price, float(h[i]), float(c[i])))
            consumed_sweeps.add(round(sl.price, 1))
        elif run_below and sweep_above:
            out.append(Liquidity("RUN", "BELOW", i, ts[i], sl.price, float(l[i]), float(c[i])))
            consumed_sweeps.add(round(sh.price, 1))
        else:
            if sweep_above:
                out.append(Liquidity("SWEEP", "ABOVE", i, ts[i], sh.price, float(h[i]), float(c[i])))
                consumed_sweeps.add(round(sh.price, 1))
            elif run_above:
                out.append(Liquidity("RUN", "ABOVE", i, ts[i], sh.price, float(h[i]), float(c[i])))

            if sweep_below:
                out.append(Liquidity("SWEEP", "BELOW", i, ts[i], sl.price, float(l[i]), float(c[i])))
                consumed_sweeps.add(round(sl.price, 1))
            elif run_below:
                out.append(Liquidity("RUN", "BELOW", i, ts[i], sl.price, float(l[i]), float(c[i])))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Fair value gaps & Retracement Tracker
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FVG:
    direction: str                # BULLISH | BEARISH
    i: int
    at: datetime
    low: float                    # Bottom boundary of gap
    high: float                   # Top boundary of gap
    mitigated_i: int | None = None
    mitigated_pct: float = 0.0
    fill_state: str = "unmitigated"  # unmitigated | iofed_filled | ce_filled | full_filled
    iofed_i: int | None = None
    ce_i: int | None = None
    full_fill_i: int | None = None
    iofed_at: datetime | None = None
    ce_at: datetime | None = None
    full_fill_at: datetime | None = None

    @property
    def top(self) -> float:
        return self.high

    @property
    def bottom(self) -> float:
        return self.low

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def ce_level(self) -> float:
        return self.mid

    @property
    def size(self) -> float:
        return self.high - self.low

    @property
    def iofed_level(self) -> float:
        """
        25% retracement level into the FVG:
        - Bullish: Top - (Size * 0.25)
        - Bearish: Bottom + (Size * 0.25)
        """
        if self.direction == "BULLISH":
            return self.high - (self.size * 0.25)
        else:
            return self.low + (self.size * 0.25)

    @property
    def full_level(self) -> float:
        """100% full fill level (opposite boundary)."""
        return self.low if self.direction == "BULLISH" else self.high

    @property
    def open(self) -> bool:
        """Still standing (not fully filled or exceeded threshold)."""
        return self.fill_state != "full_filled" and self.mitigated_i is None

    @property
    def is_unmitigated(self) -> bool:
        return self.fill_state == "unmitigated"

    @property
    def is_iofed_filled(self) -> bool:
        return self.fill_state in ("iofed_filled", "ce_filled", "full_filled")

    @property
    def is_ce_filled(self) -> bool:
        return self.fill_state in ("ce_filled", "full_filled")

    @property
    def is_full_filled(self) -> bool:
        return self.fill_state == "full_filled"


def fvgs(df: pd.DataFrame, mitigate_at: float = 0.5) -> list[FVG]:
    """
    Three-bar imbalances, tracking IOFED (25%), CE (50%), and Full Fill (100%).
    """
    tracker = FVG_Tracker(df, mitigate_at=mitigate_at)
    return tracker.fvgs


class FVG_Tracker:
    """
    Vectorized and stateful Fair Value Gap (FVG) tracker.
    Tracks FVG detection, exact retracement depths (IOFED 25%, CE 50%, Full Fill 100%),
    and maintains an active ledger of open, partially mitigated, and fully filled FVGs.
    """
    def __init__(self, df: pd.DataFrame | None = None, mitigate_at: float = 0.5):
        self.fvgs: list[FVG] = []
        self.mitigate_at = mitigate_at
        if df is not None and not df.empty:
            self.track(df, mitigate_at=mitigate_at)

    def track(self, df: pd.DataFrame, mitigate_at: float = 0.5) -> list[FVG]:
        if df is None or len(df) < 3:
            self.fvgs = []
            return []

        c_map = {c: str(c).lower() for c in df.columns}
        d = df.rename(columns=c_map)
        h = d["high"].astype(float).to_numpy()
        l = d["low"].astype(float).to_numpy()

        ts = _times(d).tolist()
        out: list[FVG] = []
        n = len(df)

        for i in range(2, n):
            if l[i] > h[i - 2]:
                out.append(FVG("BULLISH", i, ts[i], float(h[i - 2]), float(l[i])))
            elif h[i] < l[i - 2]:
                out.append(FVG("BEARISH", i, ts[i], float(h[i]), float(l[i - 2])))

        for g in out:
            for j in range(g.i + 1, n):
                if g.direction == "BULLISH":
                    pen = (g.high - l[j]) / g.size if g.size else 1.0
                    # 1. Bullish IOFED trigger (25% fill)
                    if l[j] <= g.iofed_level and g.iofed_i is None:
                        g.iofed_i = j
                        g.iofed_at = ts[j]
                        if g.fill_state == "unmitigated":
                            g.fill_state = "iofed_filled"
                    # 2. Bullish CE trigger (50% midpoint)
                    if l[j] <= g.ce_level and g.ce_i is None:
                        g.ce_i = j
                        g.ce_at = ts[j]
                        if g.fill_state in ("unmitigated", "iofed_filled"):
                            g.fill_state = "ce_filled"
                    # 3. Bullish Full Fill trigger (100%)
                    if l[j] <= g.bottom and g.full_fill_i is None:
                        g.full_fill_i = j
                        g.full_fill_at = ts[j]
                        g.fill_state = "full_filled"
                else:
                    pen = (h[j] - g.low) / g.size if g.size else 1.0
                    # 1. Bearish IOFED trigger (25% fill)
                    if h[j] >= g.iofed_level and g.iofed_i is None:
                        g.iofed_i = j
                        g.iofed_at = ts[j]
                        if g.fill_state == "unmitigated":
                            g.fill_state = "iofed_filled"
                    # 2. Bearish CE trigger (50% midpoint)
                    if h[j] >= g.ce_level and g.ce_i is None:
                        g.ce_i = j
                        g.ce_at = ts[j]
                        if g.fill_state in ("unmitigated", "iofed_filled"):
                            g.fill_state = "ce_filled"
                    # 3. Bearish Full Fill trigger (100%)
                    if h[j] >= g.top and g.full_fill_i is None:
                        g.full_fill_i = j
                        g.full_fill_at = ts[j]
                        g.fill_state = "full_filled"

                if pen >= mitigate_at and g.mitigated_i is None:
                    g.mitigated_i = j
                g.mitigated_pct = float(max(g.mitigated_pct, min(max(pen, 0.0), 1.0)))

                if g.full_fill_i is not None:
                    break

        self.fvgs = out
        return out

    def get_ledger(self) -> pd.DataFrame:
        """
        Returns complete ledger of all detected FVGs, their geometry,
        exact retracement levels, and fill states.
        """
        records = []
        for g in self.fvgs:
            records.append({
                "fvg_id": f"FVG_{g.direction}_{g.i}",
                "direction": g.direction,
                "bar_i": g.i,
                "timestamp": g.at,
                "top": g.top,
                "bottom": g.bottom,
                "size": g.size,
                "iofed_level": g.iofed_level,
                "ce_level": g.ce_level,
                "full_level": g.full_level,
                "fill_state": g.fill_state,
                "mitigated_pct": g.mitigated_pct,
                "iofed_i": g.iofed_i,
                "iofed_at": g.iofed_at,
                "ce_i": g.ce_i,
                "ce_at": g.ce_at,
                "full_fill_i": g.full_fill_i,
                "full_fill_at": g.full_fill_at,
                "open": g.open,
            })
        return pd.DataFrame(records)

    def active_fvgs(self, bar: int | None = None) -> list[FVG]:
        """FVGs open / standing as of bar."""
        if bar is None:
            return [g for g in self.fvgs if g.open]
        return [g for g in self.fvgs if g.i <= bar and (g.full_fill_i is None or g.full_fill_i > bar)]

    def unmitigated_fvgs(self, bar: int | None = None) -> list[FVG]:
        """FVGs completely unmitigated as of bar."""
        if bar is None:
            return [g for g in self.fvgs if g.is_unmitigated]
        return [g for g in self.fvgs if g.i <= bar and (g.iofed_i is None or g.iofed_i > bar)]

    def iofed_filled_fvgs(self, bar: int | None = None) -> list[FVG]:
        """FVGs currently at IOFED (25%) filled state."""
        if bar is None:
            return [g for g in self.fvgs if g.fill_state == "iofed_filled"]
        return [g for g in self.fvgs if g.i <= bar and g.fill_state == "iofed_filled"]

    def ce_filled_fvgs(self, bar: int | None = None) -> list[FVG]:
        """FVGs currently at CE (50%) filled state."""
        if bar is None:
            return [g for g in self.fvgs if g.fill_state == "ce_filled"]
        return [g for g in self.fvgs if g.i <= bar and g.fill_state == "ce_filled"]

    def full_filled_fvgs(self, bar: int | None = None) -> list[FVG]:
        """FVGs fully mitigated (100%)."""
        if bar is None:
            return [g for g in self.fvgs if g.is_full_filled]
        return [g for g in self.fvgs if g.full_fill_i is not None and g.full_fill_i <= bar]

    def vectorized_fvg_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Bar-by-bar vectorized features aligned with df."""
        n = len(df)
        c_map = {c: str(c).lower() for c in df.columns}
        d = df.rename(columns=c_map)
        h = d["high"].astype(float).to_numpy()
        l = d["low"].astype(float).to_numpy()

        has_bullish = np.zeros(n, dtype=bool)
        has_bearish = np.zeros(n, dtype=bool)
        bullish_top = np.full(n, np.nan)
        bullish_bottom = np.full(n, np.nan)
        bullish_iofed = np.full(n, np.nan)
        bullish_ce = np.full(n, np.nan)
        bearish_top = np.full(n, np.nan)
        bearish_bottom = np.full(n, np.nan)
        bearish_iofed = np.full(n, np.nan)
        bearish_ce = np.full(n, np.nan)

        for i in range(2, n):
            if l[i] > h[i - 2]:
                has_bullish[i] = True
                bullish_bottom[i] = h[i - 2]
                bullish_top[i] = l[i]
                sz = l[i] - h[i - 2]
                bullish_iofed[i] = l[i] - (0.25 * sz)
                bullish_ce[i] = l[i] - (0.50 * sz)
            elif h[i] < l[i - 2]:
                has_bearish[i] = True
                bearish_bottom[i] = h[i]
                bearish_top[i] = l[i - 2]
                sz = l[i - 2] - h[i]
                bearish_iofed[i] = h[i] + (0.25 * sz)
                bearish_ce[i] = h[i] + (0.50 * sz)

        return pd.DataFrame({
            "has_bullish_fvg": has_bullish,
            "has_bearish_fvg": has_bearish,
            "bullish_fvg_top": bullish_top,
            "bullish_fvg_bottom": bullish_bottom,
            "bullish_fvg_iofed": bullish_iofed,
            "bullish_fvg_ce": bullish_ce,
            "bearish_fvg_top": bearish_top,
            "bearish_fvg_bottom": bearish_bottom,
            "bearish_fvg_iofed": bearish_iofed,
            "bearish_fvg_ce": bearish_ce,
        }, index=df.index)


# Alias for PEP-8 / camel-case compatibility
FVGTracker = FVG_Tracker


def detect_sweep_iofed_setup(
    ohlcv_df: pd.DataFrame,
    pivot_n: int = 2,
    max_displacement_bars: int = 5,
    max_retrace_bars: int = 15,
    min_fvg_size: float = 0.0,
    return_mask: bool = False,
) -> pd.DataFrame | pd.Series:
    """
    ICT High-Low Sweep & IOFED Retracement Scanner.

    Implements the 3-phase institutional sequence:
      1. Liquidity Sweep: Price wicks beyond a swing low/high pivot but closes back inside the range.
      2. Displacement: Immediate subsequent candles create an impulsive move leaving behind an unmitigated FVG.
      3. IOFED Entry: Price retraces into the 25% IOFED zone of that new FVG and reverses in displacement direction.

    Parameters:
      - ohlcv_df: DataFrame with OHLCV data (case-insensitive columns: open, high, low, close).
      - pivot_n: Swing pivot confirmation half-window.
      - max_displacement_bars: Max candles after sweep to form displacement FVG.
      - max_retrace_bars: Max candles after FVG to retrace into IOFED zone.
      - min_fvg_size: Minimum points required for FVG imbalance.
      - return_mask: If True, returns boolean Series (mask) of trigger points.
                     If False (default), returns full signal DataFrame where
                     `res['setup_detected']` can also be used as a mask.

    Returns:
      pd.DataFrame or pd.Series identifying High-Low Sweep IOFED setups.
    """
    if ohlcv_df is None or len(ohlcv_df) < 5:
        empty_res = pd.DataFrame(index=ohlcv_df.index if ohlcv_df is not None else None)
        empty_res["setup_detected"] = False
        return empty_res["setup_detected"] if return_mask else empty_res

    col_map = {c: str(c).lower() for c in ohlcv_df.columns}
    d = ohlcv_df.rename(columns=col_map)
    for col in ["open", "high", "low", "close"]:
        if col not in d.columns:
            raise ValueError(f"Missing required OHLCV column: '{col}'")

    n = len(d)
    h = d["high"].astype(float).to_numpy()
    l = d["low"].astype(float).to_numpy()
    o = d["open"].astype(float).to_numpy()
    c = d["close"].astype(float).to_numpy()
    ts = _times(d).tolist()

    ps = pivots(d, n=pivot_n)
    all_sweeps = [x for x in liquidity(d, ps) if x.kind == "SWEEP"]

    tracker = FVG_Tracker(d)
    gaps = tracker.fvgs

    res = pd.DataFrame({
        "setup_detected": pd.Series(False, index=ohlcv_df.index, dtype=bool),
        "bullish_setup": pd.Series(False, index=ohlcv_df.index, dtype=bool),
        "bearish_setup": pd.Series(False, index=ohlcv_df.index, dtype=bool),
        "direction": pd.Series("", index=ohlcv_df.index, dtype=str),
        "sweep_bar": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "sweep_time": pd.Series(pd.NaT, index=ohlcv_df.index, dtype="datetime64[ns]"),
        "sweep_level": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "sweep_wick": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "fvg_bar": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "fvg_time": pd.Series(pd.NaT, index=ohlcv_df.index, dtype="datetime64[ns]"),
        "fvg_bottom": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "fvg_top": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "fvg_size": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "iofed_level": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "ce_level": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "entry_bar": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "entry_time": pd.Series(pd.NaT, index=ohlcv_df.index, dtype="datetime64[ns]"),
        "entry_price": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "stop_loss": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "risk_pts": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "tp_ce": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "tp_1_5r": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "tp_3r": pd.Series(np.nan, index=ohlcv_df.index, dtype="float64"),
        "status": pd.Series("", index=ohlcv_df.index, dtype=str),
    })

    used_sweeps = set()
    for sw in all_sweeps:
        sw_key = (sw.direction, sw.i)
        if sw_key in used_sweeps:
            continue

        if sw.direction == "BELOW":
            # 1. Bullish Low Sweep
            # 2. Immediate displacement FVG after sweep
            cands = [
                g for g in gaps
                if g.direction == "BULLISH"
                and sw.i < g.i <= sw.i + max_displacement_bars + 2
                and g.bottom >= sw.wick
                and g.size >= min_fvg_size
            ]
            cand_found = False
            for g in cands:
                # 3. IOFED Entry: retrace into IOFED zone and reverse
                for j in range(g.i + 1, min(n, g.i + max_retrace_bars + 1)):
                    hit_iofed = l[j] <= g.iofed_level
                    respected_gap = c[j] >= g.bottom and l[j] >= sw.wick
                    reversal = (c[j] >= o[j]) or (c[j] > c[j - 1]) or (c[j] >= g.iofed_level)

                    if hit_iofed and respected_gap and reversal:
                        idx = ohlcv_df.index[j]
                        res.loc[idx, "setup_detected"] = True
                        res.loc[idx, "bullish_setup"] = True
                        res.loc[idx, "direction"] = "BULLISH"
                        res.loc[idx, "sweep_bar"] = sw.i
                        res.loc[idx, "sweep_time"] = sw.at
                        res.loc[idx, "sweep_level"] = sw.level
                        res.loc[idx, "sweep_wick"] = sw.wick
                        res.loc[idx, "fvg_bar"] = g.i
                        res.loc[idx, "fvg_time"] = g.at
                        res.loc[idx, "fvg_bottom"] = g.bottom
                        res.loc[idx, "fvg_top"] = g.top
                        res.loc[idx, "fvg_size"] = g.size
                        res.loc[idx, "iofed_level"] = g.iofed_level
                        res.loc[idx, "ce_level"] = g.ce_level
                        res.loc[idx, "entry_bar"] = j
                        res.loc[idx, "entry_time"] = ts[j]
                        entry = float(c[j])
                        sl = float(sw.wick)
                        risk = max(1.0, entry - sl)
                        res.loc[idx, "entry_price"] = entry
                        res.loc[idx, "stop_loss"] = sl
                        res.loc[idx, "risk_pts"] = risk
                        res.loc[idx, "tp_ce"] = g.ce_level
                        res.loc[idx, "tp_1_5r"] = round(entry + 1.5 * risk, 2)
                        res.loc[idx, "tp_3r"] = round(entry + 3.0 * risk, 2)
                        res.loc[idx, "status"] = "TRIGGERED"
                        used_sweeps.add(sw_key)
                        cand_found = True
                        break
                if cand_found:
                    break

        elif sw.direction == "ABOVE":
            # 1. Bearish High Sweep
            # 2. Immediate displacement FVG after sweep
            cands = [
                g for g in gaps
                if g.direction == "BEARISH"
                and sw.i < g.i <= sw.i + max_displacement_bars + 2
                and g.top <= sw.wick
                and g.size >= min_fvg_size
            ]
            cand_found = False
            for g in cands:
                for j in range(g.i + 1, min(n, g.i + max_retrace_bars + 1)):
                    hit_iofed = h[j] >= g.iofed_level
                    respected_gap = c[j] <= g.top and h[j] <= sw.wick
                    reversal = (c[j] <= o[j]) or (c[j] < c[j - 1]) or (c[j] <= g.iofed_level)

                    if hit_iofed and respected_gap and reversal:
                        idx = ohlcv_df.index[j]
                        res.loc[idx, "setup_detected"] = True
                        res.loc[idx, "bearish_setup"] = True
                        res.loc[idx, "direction"] = "BEARISH"
                        res.loc[idx, "sweep_bar"] = sw.i
                        res.loc[idx, "sweep_time"] = sw.at
                        res.loc[idx, "sweep_level"] = sw.level
                        res.loc[idx, "sweep_wick"] = sw.wick
                        res.loc[idx, "fvg_bar"] = g.i
                        res.loc[idx, "fvg_time"] = g.at
                        res.loc[idx, "fvg_bottom"] = g.bottom
                        res.loc[idx, "fvg_top"] = g.top
                        res.loc[idx, "fvg_size"] = g.size
                        res.loc[idx, "iofed_level"] = g.iofed_level
                        res.loc[idx, "ce_level"] = g.ce_level
                        res.loc[idx, "entry_bar"] = j
                        res.loc[idx, "entry_time"] = ts[j]
                        entry = float(c[j])
                        sl = float(sw.wick)
                        risk = max(1.0, sl - entry)
                        res.loc[idx, "entry_price"] = entry
                        res.loc[idx, "stop_loss"] = sl
                        res.loc[idx, "risk_pts"] = risk
                        res.loc[idx, "tp_ce"] = g.ce_level
                        res.loc[idx, "tp_1_5r"] = round(entry - 1.5 * risk, 2)
                        res.loc[idx, "tp_3r"] = round(entry - 3.0 * risk, 2)
                        res.loc[idx, "status"] = "TRIGGERED"
                        used_sweeps.add(sw_key)
                        cand_found = True
                        break
                if cand_found:
                    break

    if return_mask:
        return res["setup_detected"].astype(bool)
    return res


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# 5 · Order blocks & Breaker Blocks
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBlock:
    direction: str                # BULLISH | BEARISH
    i: int
    at: datetime
    low: float
    high: float
    bos_i: int
    invalidated_i: int | None = None
    mitigated_i: int | None = None
    test_count: int = 0
    # Breaker Block enhancements
    is_breaker: bool = False
    breaker_i: int | None = None
    breaker_at: datetime | None = None
    breaker_direction: str = ""   # Inverted polarity: failed BULLISH -> BEARISH Breaker, failed BEARISH -> BULLISH Breaker
    breaker_mitigated_i: int | None = None
    breaker_test_count: int = 0

    @property
    def open(self) -> bool:
        """Still standing (not invalidated)."""
        return self.invalidated_i is None

    @property
    def unmitigated(self) -> bool:
        """Standing and fresh (never entered by subsequent candles)."""
        return self.invalidated_i is None and self.mitigated_i is None

    @property
    def breaker_active(self) -> bool:
        """Breaker block still standing (not cleanly violated back)."""
        return self.is_breaker and (self.breaker_mitigated_i is None)

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def order_blocks(df: pd.DataFrame, st: Structure,
                 gaps: list[FVG], lookback: int = 6,
                 pick: Literal["extreme", "last_opposite"] = "extreme",
                 require_fvg: bool = True) -> list[OrderBlock]:
    """
    The origin candle of a move that produced a structural break.

    Differences from earlier version:
      1. One block per break level: `crossed` deduplicates so repeated breaks of the
         same level in choppy conditions do not generate redundant blocks.
      2. Candle selection rule:
         - "extreme" (default, matching reference library): candle with lowest low
           (bullish) or highest high (bearish) between the level and the break —
           the actual origin of the leg.
         - "last_opposite": walks back for the last opposite-coloured candle.
      3. `require_fvg`: toggleable (default True) to enforce both FVG imbalance and BOS.

    Tracks:
      - Invalidation: when a close penetrates fully through the far edge
      - Mitigation/Retest: when price pulls back into the [low, high] zone
      - Breaker Block: Inverts polarity upon invalidation (failed Demand -> Supply Breaker)
    """
    if df is None or df.empty or not st.events:
        return []
    o = df["open"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    out: list[OrderBlock] = []

    crossed: set = set()          # levels already used as a break origin

    for ev in st.events:
        if ev.kind not in ("BOS", "CHOCH", "MSS"):
            continue
        want = "BULLISH" if ev.direction == "BULLISH" else "BEARISH"

        # One block per level, ever.
        key = (round(ev.level, 2), want)
        if key in crossed:
            continue
        crossed.add(key)

        if require_fvg:
            near_gap = any(g.direction == want and abs(g.i - ev.i) <= lookback
                           for g in gaps)
            if not near_gap:
                continue

        found = None
        if pick == "extreme":
            # The candle that made the extreme between the level being
            # broken and the break itself — the origin of the leg.
            start = max(ev.i - lookback, 0)
            end = ev.i
            if end > start:
                seg = (l[start:end] if want == "BULLISH" else h[start:end])
                found = int(start + (np.argmin(seg) if want == "BULLISH"
                                     else np.argmax(seg)))
        else:
            for j in range(ev.i, max(ev.i - lookback, 0) - 1, -1):
                down = c[j] < o[j]
                if (want == "BULLISH" and down) or (want == "BEARISH" and not down):
                    found = j
                    break
        if found is None:
            continue
        ob = OrderBlock(want, found, ts[found], float(l[found]),
                        float(h[found]), ev.i)
        for k in range(ev.i + 1, len(df)):
            # Invalidation takes priority -> flips to Breaker Block
            if (want == "BULLISH" and c[k] < ob.low) or \
               (want == "BEARISH" and c[k] > ob.high):
                ob.invalidated_i = k
                ob.is_breaker = True
                ob.breaker_i = k
                ob.breaker_at = ts[k]
                ob.breaker_direction = "BEARISH" if want == "BULLISH" else "BULLISH"

                # Track subsequent retests / mitigation of the Breaker Block
                for m in range(k + 1, len(df)):
                    # Breaker retest
                    if (ob.breaker_direction == "BEARISH" and h[m] >= ob.low and l[m] <= ob.high) or \
                       (ob.breaker_direction == "BULLISH" and l[m] <= ob.high and h[m] >= ob.low):
                        ob.breaker_test_count += 1
                    # Full penetration through the breaker
                    if (ob.breaker_direction == "BEARISH" and c[m] > ob.high) or \
                       (ob.breaker_direction == "BULLISH" and c[m] < ob.low):
                        ob.breaker_mitigated_i = m
                        break
                break

            # Mitigation / test check: price enters the block
            if (want == "BULLISH" and l[k] <= ob.high and h[k] >= ob.low) or \
               (want == "BEARISH" and h[k] >= ob.low and l[k] <= ob.high):
                if ob.mitigated_i is None:
                    ob.mitigated_i = k
                ob.test_count += 1
        out.append(ob)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 6 · Liquidity Pools (Equal Highs / Equal Lows)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiquidityPool:
    kind: str                     # EQH (Buy-side Liquidity) | EQL (Sell-side Liquidity)
    level: float
    pivots: list[Pivot]
    first_i: int
    last_i: int
    at: datetime
    swept_i: int | None = None

    @property
    def swept(self) -> bool:
        return self.swept_i is not None

    @property
    def label(self) -> str:
        state = f"SWEPT (bar {self.swept_i})" if self.swept else "UNCOLLECTED"
        return f"{self.kind} @ {self.level:,.1f} [{state}]"


def liquidity_pools(df: pd.DataFrame, ps: list[Pivot],
                    tolerance_pct: float = 0.001) -> list[LiquidityPool]:
    """
    Equal Highs (EQH) and Equal Lows (EQL).
    Flags resting buy-side / sell-side liquidity where stops cluster.
    `tolerance_pct`: max difference between pivots (default 0.10% ~24 pts on Nifty).
    """
    if df is None or len(df) < 2 or not ps:
        return []
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()

    pools: list[LiquidityPool] = []

    highs = [p for p in ps if p.kind == "HIGH"]
    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            p1, p2 = highs[i], highs[j]
            if p2.i - p1.i < 3:
                continue
            avg = (p1.price + p2.price) / 2.0
            if abs(p1.price - p2.price) / avg <= tolerance_pct:
                swept_i = None
                max_lvl = max(p1.price, p2.price)
                for k in range(p2.confirmed_i, len(df)):
                    if h[k] > max_lvl:
                        swept_i = k
                        break
                pools.append(LiquidityPool("EQH", avg, [p1, p2], p1.i, p2.i,
                                           p2.at, swept_i))

    lows = [p for p in ps if p.kind == "LOW"]
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            p1, p2 = lows[i], lows[j]
            if p2.i - p1.i < 3:
                continue
            avg = (p1.price + p2.price) / 2.0
            if abs(p1.price - p2.price) / avg <= tolerance_pct:
                swept_i = None
                min_lvl = min(p1.price, p2.price)
                for k in range(p2.confirmed_i, len(df)):
                    if l[k] < min_lvl:
                        swept_i = k
                        break
                pools.append(LiquidityPool("EQL", avg, [p1, p2], p1.i, p2.i,
                                           p2.at, swept_i))

    # Cluster equal pools within 8 points to prevent duplicate close pools
    def _consolidate_pools(raw: list[LiquidityPool], cluster_pts: float = 8.0) -> list[LiquidityPool]:
        if not raw:
            return []
        raw.sort(key=lambda p: p.level)
        out: list[LiquidityPool] = []
        cur = [raw[0]]
        for p in raw[1:]:
            if p.level - cur[-1].level <= cluster_pts:
                cur.append(p)
            else:
                avg_l = sum(x.level for x in cur) / len(cur)
                all_pvs = [pv for x in cur for pv in x.pivots]
                latest = max(cur, key=lambda x: x.last_i)
                first_i = min(x.first_i for x in cur)
                swept_i = next((x.swept_i for x in cur if x.swept_i is not None), None)
                out.append(LiquidityPool(latest.kind, round(avg_l, 1), all_pvs, first_i, latest.last_i, latest.at, swept_i))
                cur = [p]
        if cur:
            avg_l = sum(x.level for x in cur) / len(cur)
            all_pvs = [pv for x in cur for pv in x.pivots]
            latest = max(cur, key=lambda x: x.last_i)
            first_i = min(x.first_i for x in cur)
            swept_i = next((x.swept_i for x in cur if x.swept_i is not None), None)
            out.append(LiquidityPool(latest.kind, round(avg_l, 1), all_pvs, first_i, latest.last_i, latest.at, swept_i))
        return out

    eqh_pools = _consolidate_pools([p for p in pools if p.kind == "EQH"])
    eql_pools = _consolidate_pools([p for p in pools if p.kind == "EQL"])
    return sorted(eqh_pools + eql_pools, key=lambda p: p.last_i)


# ═══════════════════════════════════════════════════════════════════════════
# 7 · Dealing range (Premium / Discount / Equilibrium & OTE)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DealingRange:
    high: float
    low: float
    equilibrium: float
    size: float
    current_price: float
    position_pct: float           # 0.0 = at low, 1.0 = at high, 0.5 = eq
    zone: str                     # PREMIUM | DISCOUNT | EQUILIBRIUM
    ote_bullish_618: float = 0.0  # 61.8% retracement level for longs (in discount)
    ote_bullish_786: float = 0.0  # 78.6% retracement level for longs (in discount)
    ote_bearish_618: float = 0.0  # 61.8% retracement level for shorts (in premium)
    ote_bearish_786: float = 0.0  # 78.6% retracement level for shorts (in premium)
    in_ote_bullish: bool = False
    in_ote_bearish: bool = False


def dealing_range(spot: float, active_high: Pivot | None,
                  active_low: Pivot | None) -> DealingRange | None:
    """Classifies current price within the active dealing range and calculates OTE zones."""
    if active_high is None or active_low is None:
        return None
    h, l = active_high.price, active_low.price
    if h <= l:
        return None
    rng = h - l
    eq = (h + l) / 2.0
    pos = (spot - l) / rng
    if spot > eq + (rng * 0.02):
        zone = "PREMIUM"
    elif spot < eq - (rng * 0.02):
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"

    # Bullish OTE in Discount (retracing down from High towards Low)
    ote_b_618 = h - (0.618 * rng)
    ote_b_786 = h - (0.786 * rng)
    in_ote_b = (ote_b_786 <= spot <= ote_b_618)

    # Bearish OTE in Premium (retracing up from Low towards High)
    ote_s_618 = l + (0.618 * rng)
    ote_s_786 = l + (0.786 * rng)
    in_ote_s = (ote_s_618 <= spot <= ote_s_786)

    return DealingRange(
        high=h, low=l, equilibrium=eq, size=rng,
        current_price=spot, position_pct=pos, zone=zone,
        ote_bullish_618=round(ote_b_618, 1),
        ote_bullish_786=round(ote_b_786, 1),
        ote_bearish_618=round(ote_s_618, 1),
        ote_bearish_786=round(ote_s_786, 1),
        in_ote_bullish=in_ote_b,
        in_ote_bearish=in_ote_s,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 7.1 · Automated Market Phase Engine (Wyckoff / SMC Phases)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MarketPhase:
    phase: Literal["ACCUMULATION", "MARK_UP", "DISTRIBUTION", "MARK_DOWN", "TRANSITION"]
    confidence: float             # Heuristic weight (deprecated, use factor_count)
    characteristics: list[str]
    summary: str
    badge_color: str
    factor_count: int = 2         # Count of strictly confirmed structural factors
    total_factors: int = 4


def detect_market_phase(df: pd.DataFrame, ps: list[Pivot], st: Structure,
                        dr: DealingRange | None = None,
                        liq: list[Liquidity] | None = None) -> MarketPhase:
    """
    Autonomously classifies institutional market phase into:
      - Accumulation (Wyckoff absorption at discount / range lows)
      - Mark-up / Uptrend (Sustained HH/HL progression and bullish expansion)
      - Distribution (Wyckoff distribution at premium / range highs)
      - Mark-down / Downtrend (Sustained LH/LL progression and bearish expansion)

    Reports concrete factor counts (e.g. 3/4 factors confirmed) rather than
    unvalidated pseudo-scientific confidence percentages.
    """
    if df is None or len(df) < 5:
        return MarketPhase("TRANSITION", 0.5, ["Insufficient candle data"], "Market phase indeterminate.", "#94A3B8", 1, 4)

    recent_ps = ps[-8:] if len(ps) >= 8 else ps
    h_subkinds = [p.subkind for p in recent_ps if p.kind == "HIGH"]
    l_subkinds = [p.subkind for p in recent_ps if p.kind == "LOW"]

    recent_sweeps = [x for x in (liq or []) if x.kind == "SWEEP"][-6:]
    ssl_sweeps = [s for s in recent_sweeps if s.direction == "BELOW"]
    bsl_sweeps = [s for s in recent_sweeps if s.direction == "ABOVE"]

    trend = getattr(st, "trend", "TRANSITION")
    pos_pct = getattr(dr, "position_pct", 0.5)

    # 1. MARK_UP (Uptrend)
    if trend == "BULLISH" and h_subkinds.count("HH") >= 1 and (pos_pct > 0.40 or trend == "BULLISH"):
        factors = 1  # Trend is Bullish
        if h_subkinds.count("HH") >= 1:
            factors += 1
        if l_subkinds.count("HL") >= 1:
            factors += 1
        if pos_pct >= 0.50:
            factors += 1
        chars = [
            "Structural trend is BULLISH (BOS / MSS confirmed)",
            f"Higher Highs ({h_subkinds.count('HH')}) and Higher Lows ({l_subkinds.count('HL')}) sequence",
            "Price respecting Demand Order Blocks as dynamic support",
            f"Trading at {pos_pct*100:.0f}% of dealing range",
        ]
        return MarketPhase("MARK_UP", factors / 4.0, chars,
                           "Institutional Mark-Up Phase: Buyers in aggressive control. Focus strictly on Bullish OB/FVG dip buys.",
                           "#10B981", factors, 4)

    # 2. MARK_DOWN (Downtrend)
    if trend == "BEARISH" and l_subkinds.count("LL") >= 1 and (pos_pct < 0.60 or trend == "BEARISH"):
        factors = 1  # Trend is Bearish
        if l_subkinds.count("LL") >= 1:
            factors += 1
        if h_subkinds.count("LH") >= 1:
            factors += 1
        if pos_pct <= 0.50:
            factors += 1
        chars = [
            "Structural trend is BEARISH (BOS / MSS confirmed)",
            f"Lower Highs ({h_subkinds.count('LH')}) and Lower Lows ({l_subkinds.count('LL')}) sequence",
            "Price respecting Supply Order Blocks as dynamic ceiling",
            f"Trading at {pos_pct*100:.0f}% of dealing range",
        ]
        return MarketPhase("MARK_DOWN", factors / 4.0, chars,
                           "Institutional Mark-Down Phase: Sellers in aggressive control. Focus strictly on Supply OB/FVG rally shorts.",
                           "#EF4444", factors, 4)

    # 3. ACCUMULATION (At Lows / Discount)
    if pos_pct <= 0.42 or (ssl_sweeps and pos_pct <= 0.50):
        factors = 1  # Discount compression
        if ssl_sweeps:
            factors += 1
        if l_subkinds.count("HL") >= 1 or l_subkinds.count("LL") >= 1:
            factors += 1
        if trend in ("TRANSITION", "BULLISH"):
            factors += 1
        chars = [
            f"Price compressed in Discount Zone ({pos_pct*100:.0f}% of range)",
            f"Sell-Side Liquidity (SSL) absorption: {len(ssl_sweeps)} stop sweeps below range lows",
            "Smart money absorbing retail panic selling into Demand zones",
            "Early basing structure forming before prospective Mark-Up",
        ]
        return MarketPhase("ACCUMULATION", factors / 4.0, chars,
                           "Institutional Accumulation Phase: Smart money accumulating inventory at discount. Stalk Bullish MSS shifts.",
                           "#00BCD4", factors, 4)

    # 4. DISTRIBUTION (At Highs / Premium)
    if pos_pct >= 0.58 or (bsl_sweeps and pos_pct >= 0.50):
        factors = 1  # Premium compression
        if bsl_sweeps:
            factors += 1
        if h_subkinds.count("LH") >= 1 or h_subkinds.count("HH") >= 1:
            factors += 1
        if trend in ("TRANSITION", "BEARISH"):
            factors += 1
        chars = [
            f"Price compressed in Premium Zone ({pos_pct*100:.0f}% of range)",
            f"Buy-Side Liquidity (BSL) distribution: {len(bsl_sweeps)} stop sweeps above range highs",
            "Smart money distributing inventory to retail breakout buyers",
            "Early rounding top structure forming before prospective Mark-Down",
        ]
        return MarketPhase("DISTRIBUTION", factors / 4.0, chars,
                           "Institutional Distribution Phase: Smart money unloading longs into retail FOMO. Stalk Bearish MSS shifts.",
                           "#F59E0B", factors, 4)

    return MarketPhase("TRANSITION", 0.50,
                       ["Market rotating around 50% Equilibrium", "No sustained directional impulse"],
                       "Equilibrium Consolidation: Stand aside until liquidity boundary is swept.",
                       "#94A3B8", 2, 4)


# ═══════════════════════════════════════════════════════════════════════════
# 7.2 · Institutional Liquidity Zones (PDH/PDL, PWH/PWL, Asian H/L, EQH/EQL)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiquidityLevel:
    name: str                     # "PDH", "PDL", "PWH", "PWL", "Asian High", "Asian Low", "EQH", "EQL"
    kind: str                     # "BUY_SIDE" (High) | "SELL_SIDE" (Low)
    level: float
    at: datetime | None = None
    swept: bool = False
    swept_i: int | None = None
    swept_at: datetime | None = None
    swept_wick: float | None = None
    swept_close: float | None = None
    color: str = "#FF9800"
    source: str = ""

    @property
    def direction(self) -> str:
        """Compatibility: SELL_SIDE sweeps low from BELOW, BUY_SIDE sweeps high from ABOVE."""
        return "BELOW" if self.kind == "SELL_SIDE" else "ABOVE"

    @property
    def swept_time(self) -> datetime | None:
        """Compatibility alias for swept_at."""
        return self.swept_at


def institutional_liquidity_zones(df: pd.DataFrame, ps: list[Pivot] | None = None,
                                  tolerance_pct: float = 0.001) -> list[LiquidityLevel]:
    """
    Extracts high-probability institutional liquidity pools:
      1. Previous Day High / Low (PDH / PDL)
      2. Previous Week High / Low (PWH / PWL)
      3. Asian Session High / Low (5:30 AM to 2:30 PM IST)
      4. Equal Highs (EQH - Buy-Side Liquidity) & Equal Lows (EQL - Sell-Side Liquidity)
    Tracks real-time stop-loss sweeps (wicks through level, closes back inside).
    """
    if df is None or df.empty:
        return []

    levels: list[LiquidityLevel] = []
    ts = _times(df).tolist()
    dts = _times(df)
    dates = sorted(dts.dt.date.unique())
    last_date = dates[-1]

    # 1. PDH / PDL (Previous Day High / Low)
    pdh, pdl, pd_date = None, None, None
    if len(dates) >= 2:
        prev_date = dates[-2]
        prev_mask = dts.dt.date == prev_date
        pdh = float(df.loc[prev_mask, "high"].max())
        pdl = float(df.loc[prev_mask, "low"].min())
        pd_date = prev_date
    else:
        try:
            import price_action as _pa
            hist = _pa.load_history_candles(days=3, timeframe="15m")
            if not hist.empty:
                h_dts = pd.to_datetime(hist["timestamp"])
                h_dates = sorted(h_dts.dt.date.unique())
                prior = [d for d in h_dates if d < last_date]
                if prior:
                    p_mask = h_dts.dt.date == prior[-1]
                    pdh = float(hist.loc[p_mask, "high"].max())
                    pdl = float(hist.loc[p_mask, "low"].min())
                    pd_date = prior[-1]
        except Exception:
            pass

    if pdh is not None and pdl is not None:
        levels.append(LiquidityLevel(
            name="PDH", kind="BUY_SIDE", level=round(pdh, 1),
            color="#FF9800", source=f"Prior Day High ({pd_date})"
        ))
        levels.append(LiquidityLevel(
            name="PDL", kind="SELL_SIDE", level=round(pdl, 1),
            color="#FF9800", source=f"Prior Day Low ({pd_date})"
        ))

    # 2. PWH / PWL (Previous Week High / Low)
    pwh, pwl = None, None
    try:
        curr_year, curr_week, _ = last_date.isocalendar()
        years = dts.dt.isocalendar().year
        weeks = dts.dt.isocalendar().week
        prev_w_mask = (years < curr_year) | ((years == curr_year) & (weeks < curr_week))
        if prev_w_mask.any():
            last_pw_year = years[prev_w_mask].iloc[-1]
            last_pw_week = weeks[prev_w_mask].iloc[-1]
            pw_mask = (years == last_pw_year) & (weeks == last_pw_week)
            pwh = float(df.loc[pw_mask, "high"].max())
            pwl = float(df.loc[pw_mask, "low"].min())
        else:
            import price_action as _pa
            hist15 = _pa.load_history_candles(days=15, timeframe="15m")
            if not hist15.empty:
                h_dts = pd.to_datetime(hist15["timestamp"])
                h_years = h_dts.dt.isocalendar().year
                h_weeks = h_dts.dt.isocalendar().week
                h_prev = (h_years < curr_year) | ((h_years == curr_year) & (h_weeks < curr_week))
                if h_prev.any():
                    l_yr = h_years[h_prev].iloc[-1]
                    l_wk = h_weeks[h_prev].iloc[-1]
                    m = (h_years == l_yr) & (h_weeks == l_wk)
                    pwh = float(hist15.loc[m, "high"].max())
                    pwl = float(hist15.loc[m, "low"].min())
    except Exception:
        pass

    if pwh is not None and pwl is not None:
        levels.append(LiquidityLevel(
            name="PWH", kind="BUY_SIDE", level=round(pwh, 1),
            color="#AB47BC", source="Previous Week High"
        ))
        levels.append(LiquidityLevel(
            name="PWL", kind="SELL_SIDE", level=round(pwl, 1),
            color="#AB47BC", source="Previous Week Low"
        ))

    # 3. Initial Balance High / Low (IBH / IBL: 09:15 to 10:15 IST)
    ibh, ibl = None, None
    try:
        from datetime import time as _time
        today_mask = dts.dt.date == last_date
        today_df = df[today_mask]
        today_times = pd.to_datetime(today_df["timestamp"] if "timestamp" in today_df.columns else today_df.index).dt.time
        ib_mask = (today_times >= _time(9, 15)) & (today_times <= _time(10, 15))
        ib_df = today_df[ib_mask]
        if not ib_df.empty and len(ib_df) >= 2:
            ibh = float(ib_df["high"].max())
            ibl = float(ib_df["low"].min())
        elif len(dates) >= 2:
            prev_mask = dts.dt.date == dates[-2]
            p_df = df[prev_mask]
            p_times = pd.to_datetime(p_df["timestamp"] if "timestamp" in p_df.columns else p_df.index).dt.time
            p_ib = p_df[(p_times >= _time(9, 15)) & (p_times <= _time(10, 15))]
            if not p_ib.empty:
                ibh = float(p_ib["high"].max())
                ibl = float(p_ib["low"].min())
    except Exception:
        pass

    if ibh is not None and ibl is not None:
        levels.append(LiquidityLevel(
            name="IBH", kind="BUY_SIDE", level=round(ibh, 1),
            color="#00BCD4", source="Initial Balance High (09:15–10:15 IST)"
        ))
        levels.append(LiquidityLevel(
            name="IBL", kind="SELL_SIDE", level=round(ibl, 1),
            color="#00BCD4", source="Initial Balance Low (09:15–10:15 IST)"
        ))

    # 4. Equal Highs (EQH) and Equal Lows (EQL) with 8-Point Cluster Consolidation
    if ps:
        high_cands: list[tuple[float, datetime, str]] = []
        highs = [p for p in ps if p.kind == "HIGH"]
        for i in range(len(highs)):
            for j in range(i + 1, len(highs)):
                p1, p2 = highs[i], highs[j]
                if p2.i - p1.i < 3:
                    continue
                avg = (p1.price + p2.price) / 2.0
                if abs(p1.price - p2.price) / avg <= tolerance_pct:
                    high_cands.append((avg, p2.at, f"Equal Highs ({p1.price:,.1f} & {p2.price:,.1f})"))

        # Cluster equal highs within 8 points into a single consolidated pool
        if high_cands:
            high_cands.sort(key=lambda x: x[0])
            cur_cluster = [high_cands[0]]
            for c in high_cands[1:]:
                if c[0] - cur_cluster[-1][0] <= 8.0:
                    cur_cluster.append(c)
                else:
                    avg_lvl = sum(x[0] for x in cur_cluster) / len(cur_cluster)
                    latest_at = max(x[1] for x in cur_cluster)
                    levels.append(LiquidityLevel(
                        name="EQH", kind="BUY_SIDE", level=round(avg_lvl, 1),
                        at=latest_at, color="#00E5FF", source=f"Consolidated Equal Highs ({len(cur_cluster)} touches @ {avg_lvl:,.1f})"
                    ))
                    cur_cluster = [c]
            if cur_cluster:
                avg_lvl = sum(x[0] for x in cur_cluster) / len(cur_cluster)
                latest_at = max(x[1] for x in cur_cluster)
                levels.append(LiquidityLevel(
                    name="EQH", kind="BUY_SIDE", level=round(avg_lvl, 1),
                    at=latest_at, color="#00E5FF", source=f"Consolidated Equal Highs ({len(cur_cluster)} touches @ {avg_lvl:,.1f})"
                ))

        low_cands: list[tuple[float, datetime, str]] = []
        lows = [p for p in ps if p.kind == "LOW"]
        for i in range(len(lows)):
            for j in range(i + 1, len(lows)):
                p1, p2 = lows[i], lows[j]
                if p2.i - p1.i < 3:
                    continue
                avg = (p1.price + p2.price) / 2.0
                if abs(p1.price - p2.price) / avg <= tolerance_pct:
                    low_cands.append((avg, p2.at, f"Equal Lows ({p1.price:,.1f} & {p2.price:,.1f})"))

        # Cluster equal lows within 8 points into a single consolidated pool
        if low_cands:
            low_cands.sort(key=lambda x: x[0])
            cur_cluster = [low_cands[0]]
            for c in low_cands[1:]:
                if c[0] - cur_cluster[-1][0] <= 8.0:
                    cur_cluster.append(c)
                else:
                    avg_lvl = sum(x[0] for x in cur_cluster) / len(cur_cluster)
                    latest_at = max(x[1] for x in cur_cluster)
                    levels.append(LiquidityLevel(
                        name="EQL", kind="SELL_SIDE", level=round(avg_lvl, 1),
                        at=latest_at, color="#FFB300", source=f"Consolidated Equal Lows ({len(cur_cluster)} touches @ {avg_lvl:,.1f})"
                    ))
                    cur_cluster = [c]
            if cur_cluster:
                avg_lvl = sum(x[0] for x in cur_cluster) / len(cur_cluster)
                latest_at = max(x[1] for x in cur_cluster)
                levels.append(LiquidityLevel(
                    name="EQL", kind="SELL_SIDE", level=round(avg_lvl, 1),
                    at=latest_at, color="#FFB300", source=f"Consolidated Equal Lows ({len(cur_cluster)} touches @ {avg_lvl:,.1f})"
                ))

    # 5. Check Liquidity Sweeps against OHLC (Pool consumed upon first raid)
    h_arr = df["high"].astype(float).to_numpy()
    l_arr = df["low"].astype(float).to_numpy()
    c_arr = df["close"].astype(float).to_numpy()
    n_bars = len(df)

    for lvl in levels:
        for k in range(max(0, n_bars - 40), n_bars):
            if lvl.kind == "BUY_SIDE":
                if h_arr[k] > lvl.level and c_arr[k] <= lvl.level:
                    lvl.swept = True
                    lvl.swept_i = k
                    lvl.swept_at = ts[k]
                    lvl.swept_wick = float(h_arr[k])
                    lvl.swept_close = float(c_arr[k])
                    break  # Pool consumed on first raid!
            elif lvl.kind == "SELL_SIDE":
                if l_arr[k] < lvl.level and c_arr[k] >= lvl.level:
                    lvl.swept = True
                    lvl.swept_i = k
                    lvl.swept_at = ts[k]
                    lvl.swept_wick = float(l_arr[k])
                    lvl.swept_close = float(c_arr[k])
                    break  # Pool consumed on first raid!

    return levels


# ═══════════════════════════════════════════════════════════════════════════
# 7.3 · The "Sniper Entry" Confluence Scanner
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SniperConfluenceSetup:
    direction: str                # BULLISH | BEARISH
    status: str                   # TRIGGERED (4/4) | ARMED (3/4) | FORMING (2/4) | MONITORING
    score: int                    # 0 to 4
    conditions_met: list[str]
    conditions_pending: list[str]
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    target_name: str
    risk_pts: float
    rr_ratio: float
    time: datetime
    summary: str
    trigger_alert: bool = False   # True when 4/4 triggers to sound audio


def detect_sniper_confluence(df: pd.DataFrame, spot: float, r: dict) -> dict[str, SniperConfluenceSetup]:
    """
    Evaluates institutional SMC alignment for both Long and Short setups:
      1. Price in Discount Zone (<50%) for Long / Premium Zone (>50%) for Short
      2. Sell-Side Liquidity (SSL) Sweep for Long / Buy-Side Liquidity (BSL) Sweep for Short
      3. Bullish Market Structure Shift (MSS) for Long / Bearish MSS for Short
      4. Price pulling back into Bullish Order Block/FVG/OTE for Long, or Bearish OB/FVG/OTE for Short
    Returns {'bullish': SniperConfluenceSetup, 'bearish': SniperConfluenceSetup}.
    """
    dr = r.get("dealing_range")
    liq_levels = r.get("liquidity_levels", [])
    stx = r.get("structure")
    obs = r.get("order_blocks", [])
    fvgs = r.get("fvgs", [])
    raw_liq = r.get("liquidity", [])
    last_dt = df["timestamp"].iloc[-1] if "timestamp" in df.columns else datetime.now()
    n_bars = len(df)

    # ── 1. EVALUATE BULLISH SNIPER LONG ──────────────────────────────────
    b_met, b_pending = [], []

    # Condition 1: Discount Zone
    in_discount = False
    if dr:
        in_discount = spot < dr.equilibrium
        diff = dr.equilibrium - spot
        if in_discount:
            b_met.append(f"Discount Zone: Spot {spot:,.1f} is {diff:.0f} pts below 50% EQ ({dr.equilibrium:,.0f})")
        else:
            b_pending.append(f"Price in {dr.zone} ({dr.position_pct*100:.0f}%); not in Discount (<50%)")
    else:
        b_pending.append("Dealing range equilibrium undefined")

    # Condition 2: Sell-Side Liquidity (SSL) Sweep
    # Must be a recent sweep where price is respecting the sweep low (spot >= ssl_wick)
    ssl_swept = False
    ssl_info = ""
    ssl_wick = 0.0
    for lvl in reversed(liq_levels):
        if lvl.kind == "SELL_SIDE" and lvl.swept:
            if lvl.swept_i and (n_bars - 1 - lvl.swept_i <= 20):
                wick = lvl.swept_wick or lvl.level
                if spot >= wick:  # Price must be holding above the swept wick low
                    ssl_swept = True
                    ssl_wick = wick
                    ssl_info = f"SSL Sweep: {lvl.name} ({lvl.level:,.1f}) swept (wick {ssl_wick:,.1f}, close {lvl.swept_close:,.1f})"
                    break
    if not ssl_swept:
        for x in reversed(raw_liq):
            if x.kind == "SWEEP" and x.direction == "BELOW":
                if (n_bars - 1 - x.i) <= 20:
                    if spot >= x.wick:
                        ssl_swept = True
                        ssl_wick = x.wick
                        ssl_info = f"SSL Sweep: Swing Low @ {x.level:,.1f} swept (wick {x.wick:,.1f})"
                        break
    if ssl_swept:
        b_met.append(ssl_info)
    else:
        b_pending.append("Awaiting Sell-Side Liquidity (SSL) stop hunt sweep below key low")

    # Condition 3: Bullish MSS printed
    bull_mss = False
    bull_mss_info = ""
    if stx and stx.events:
        for ev in reversed(stx.events):
            if ev.direction == "BULLISH" and (getattr(ev, "is_mss", False) or ev.kind in ("CHOCH", "MSS")):
                if (n_bars - 1 - ev.i) <= 25:
                    bull_mss = True
                    bull_mss_info = f"Bullish MSS: Broke previous LH @ {ev.level:,.1f} ({ev.at:%H:%M})"
                    break
    if bull_mss:
        b_met.append(bull_mss_info)
    else:
        b_pending.append("Awaiting Bullish Market Structure Shift (MSS break above previous LH)")

    # Condition 4: Pullback into Demand OB / FVG / OTE
    bull_demand = False
    bull_demand_info = ""
    chosen_bull_ob = None
    open_bull_obs = [b for b in obs if b.direction == "BULLISH" and b.open]
    for ob in open_bull_obs:
        if ob.low - 6.0 <= spot <= ob.high + 12.0:
            bull_demand = True
            chosen_bull_ob = ob
            bull_demand_info = f"Demand OB Retest: Spot inside [{ob.low:,.0f} – {ob.high:,.0f}]"
            break
    if not bull_demand:
        for g in [f for f in fvgs if f.direction == "BULLISH" and f.open]:
            if g.low - 5.0 <= spot <= g.high + 8.0:
                bull_demand = True
                bull_demand_info = f"Bullish FVG Retest: Spot inside [{g.low:,.0f} – {g.high:,.0f}]"
                break
    if not bull_demand and dr and getattr(dr, "in_ote_bullish", False):
        bull_demand = True
        bull_demand_info = f"Optimal Trade Entry (OTE): Inside 61.8%–78.6% pocket [{dr.ote_bullish_786:,.0f}–{dr.ote_bullish_618:,.0f}]"

    if bull_demand:
        b_met.append(bull_demand_info)
    else:
        b_pending.append("Awaiting pullback into Bullish Order Block, FVG, or OTE zone")

    b_score = len(b_met)
    b_status = "TRIGGERED" if b_score == 4 else ("ARMED" if b_score == 3 else ("FORMING" if b_score == 2 else "MONITORING"))
    b_entry = spot

    # Strict Invalidation Stop Loss for Long: MUST be strictly BELOW Entry
    recent_low = float(df["low"].tail(20).min()) if len(df) else spot - 25.0
    cand_stops = []
    if chosen_bull_ob:
        cand_stops.append(chosen_bull_ob.low - 5.0)
    if ssl_swept and ssl_wick and ssl_wick < spot:
        cand_stops.append(ssl_wick - 5.0)
    if recent_low < spot:
        cand_stops.append(recent_low - 5.0)
    cand_stops.append(spot - 25.0)

    valid_stops = [s for s in cand_stops if s < b_entry - 3.0]
    b_stop = max(valid_stops) if valid_stops else (b_entry - 25.0)
    if b_stop >= b_entry:
        b_stop = b_entry - 25.0
    b_risk = max(10.0, b_entry - b_stop)

    # Targets for Long: Must be strictly above entry
    b_t1 = dr.equilibrium if (dr and dr.equilibrium > spot + 10.0) else spot + 35.0
    upper_targets = [l.level for l in liq_levels if l.kind == "BUY_SIDE" and l.level > spot + 15.0]
    b_t2 = min(upper_targets) if upper_targets else spot + 85.0
    b_tname = next((l.name for l in liq_levels if l.level == b_t2), "BSL Liquidity")
    b_rr = round((b_t2 - b_entry) / b_risk, 2) if b_risk > 0 else 3.0

    bullish_setup = SniperConfluenceSetup(
        direction="BULLISH", status=b_status, score=b_score,
        conditions_met=b_met, conditions_pending=b_pending,
        entry_price=round(b_entry, 1), stop_loss=round(b_stop, 1),
        target_1=round(b_t1, 1), target_2=round(b_t2, 1), target_name=b_tname,
        risk_pts=round(b_risk, 1), rr_ratio=b_rr, time=last_dt,
        summary=f"Bullish Sniper Long: {b_score}/4 Conditions Confirmed. Target {b_tname} ({b_t2:,.0f}).",
        trigger_alert=(b_status == "TRIGGERED")
    )

    # ── 2. EVALUATE BEARISH SNIPER SHORT ─────────────────────────────────
    s_met, s_pending = [], []

    # Condition 1: Premium Zone
    in_premium = False
    if dr:
        in_premium = spot > dr.equilibrium
        diff = spot - dr.equilibrium
        if in_premium:
            s_met.append(f"Premium Zone: Spot {spot:,.1f} is {diff:.0f} pts above 50% EQ ({dr.equilibrium:,.0f})")
        else:
            s_pending.append(f"Price in {dr.zone} ({dr.position_pct*100:.0f}%); not in Premium (>50%)")
    else:
        s_pending.append("Dealing range equilibrium undefined")

    # Condition 2: Buy-Side Liquidity (BSL) Sweep
    # Must be a recent sweep where price is respecting the sweep high (spot <= bsl_wick)
    bsl_swept = False
    bsl_info = ""
    bsl_wick = 0.0
    for lvl in reversed(liq_levels):
        if lvl.kind == "BUY_SIDE" and lvl.swept:
            if lvl.swept_i and (n_bars - 1 - lvl.swept_i <= 20):
                wick = lvl.swept_wick or lvl.level
                if spot <= wick:  # Price must be holding below the swept wick high
                    bsl_swept = True
                    bsl_wick = wick
                    bsl_info = f"BSL Sweep: {lvl.name} ({lvl.level:,.1f}) swept (wick {bsl_wick:,.1f}, close {lvl.swept_close:,.1f})"
                    break
    if not bsl_swept:
        for x in reversed(raw_liq):
            if x.kind == "SWEEP" and x.direction == "ABOVE":
                if (n_bars - 1 - x.i) <= 20:
                    if spot <= x.wick:
                        bsl_swept = True
                        bsl_wick = x.wick
                        bsl_info = f"BSL Sweep: Swing High @ {x.level:,.1f} swept (wick {x.wick:,.1f})"
                        break
    if bsl_swept:
        s_met.append(bsl_info)
    else:
        s_pending.append("Awaiting Buy-Side Liquidity (BSL) stop hunt sweep above key high")

    # Condition 3: Bearish MSS printed
    bear_mss = False
    bear_mss_info = ""
    if stx and stx.events:
        for ev in reversed(stx.events):
            if ev.direction == "BEARISH" and (getattr(ev, "is_mss", False) or ev.kind in ("CHOCH", "MSS")):
                if (n_bars - 1 - ev.i) <= 25:
                    bear_mss = True
                    bear_mss_info = f"Bearish MSS: Broke previous HL @ {ev.level:,.1f} ({ev.at:%H:%M})"
                    break
    if bear_mss:
        s_met.append(bear_mss_info)
    else:
        s_pending.append("Awaiting Bearish Market Structure Shift (MSS break below previous HL)")

    # Condition 4: Pullback into Supply OB / FVG / OTE
    bear_supply = False
    bear_supply_info = ""
    chosen_bear_ob = None
    open_bear_obs = [b for b in obs if b.direction == "BEARISH" and b.open]
    for ob in open_bear_obs:
        if ob.low - 12.0 <= spot <= ob.high + 6.0:
            bear_supply = True
            chosen_bear_ob = ob
            bear_supply_info = f"Supply OB Retest: Spot inside [{ob.low:,.0f} – {ob.high:,.0f}]"
            break
    if not bear_supply:
        for g in [f for f in fvgs if f.direction == "BEARISH" and f.open]:
            if g.low - 8.0 <= spot <= g.high + 5.0:
                bear_supply = True
                bear_supply_info = f"Bearish FVG Retest: Spot inside [{g.low:,.0f} – {g.high:,.0f}]"
                break
    if not bear_supply and dr and getattr(dr, "in_ote_bearish", False):
        bear_supply = True
        bear_supply_info = f"Optimal Trade Entry (OTE): Inside 61.8%–78.6% pocket [{dr.ote_bearish_618:,.0f}–{dr.ote_bearish_786:,.0f}]"

    if bear_supply:
        s_met.append(bear_supply_info)
    else:
        s_pending.append("Awaiting pullback into Bearish Order Block, FVG, or OTE zone")

    s_score = len(s_met)
    s_status = "TRIGGERED" if s_score == 4 else ("ARMED" if s_score == 3 else ("FORMING" if s_score == 2 else "MONITORING"))
    s_entry = spot

    # Strict Invalidation Stop Loss for Short: MUST be strictly ABOVE Entry
    recent_high = float(df["high"].tail(20).max()) if len(df) else spot + 25.0
    cand_stops = []
    if chosen_bear_ob:
        cand_stops.append(chosen_bear_ob.high + 5.0)
    if bsl_swept and bsl_wick and bsl_wick > spot:
        cand_stops.append(bsl_wick + 5.0)
    if recent_high > spot:
        cand_stops.append(recent_high + 5.0)
    cand_stops.append(spot + 25.0)

    valid_stops = [s for s in cand_stops if s > s_entry + 3.0]
    s_stop = min(valid_stops) if valid_stops else (s_entry + 25.0)
    if s_stop <= s_entry:
        s_stop = s_entry + 25.0
    s_risk = max(10.0, s_stop - s_entry)

    # Targets for Short: Must be strictly below entry
    s_t1 = dr.equilibrium if (dr and dr.equilibrium < spot - 10.0) else spot - 35.0
    lower_targets = [l.level for l in liq_levels if l.kind == "SELL_SIDE" and l.level < spot - 15.0]
    s_t2 = max(lower_targets) if lower_targets else spot - 85.0
    s_tname = next((l.name for l in liq_levels if l.level == s_t2), "SSL Liquidity")
    s_rr = round((s_entry - s_t2) / s_risk, 2) if s_risk > 0 else 3.0

    bearish_setup = SniperConfluenceSetup(
        direction="BEARISH", status=s_status, score=s_score,
        conditions_met=s_met, conditions_pending=s_pending,
        entry_price=round(s_entry, 1), stop_loss=round(s_stop, 1),
        target_1=round(s_t1, 1), target_2=round(s_t2, 1), target_name=s_tname,
        risk_pts=round(s_risk, 1), rr_ratio=s_rr, time=last_dt,
        summary=f"Bearish Sniper Short: {s_score}/4 Conditions Confirmed. Target {s_tname} ({s_t2:,.0f}).",
        trigger_alert=(s_status == "TRIGGERED")
    )

    return {"bullish": bullish_setup, "bearish": bearish_setup}


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════

def read(df: pd.DataFrame, n: int = 2, spot: float | None = None,
         pick_ob: Literal["extreme", "last_opposite"] = "extreme",
         require_fvg_ob: bool = True) -> dict:
    """Everything, for the tab."""
    ps = pivots(df, n)
    st = structure(df, ps)
    tracker = FVG_Tracker(df)
    gaps = tracker.fvgs
    obs = order_blocks(df, st, gaps, pick=pick_ob, require_fvg=require_fvg_ob)
    liq = liquidity(df, ps)
    pools = liquidity_pools(df, ps)
    trendlines = calculate_trendlines(df, ps)
    liq_levels = institutional_liquidity_zones(df, ps)

    last = len(df) - 1 if df is not None and len(df) else 0
    ah = active(ps, last, "HIGH")
    al = active(ps, last, "LOW")

    current_spot = spot
    if current_spot is None and df is not None and len(df):
        current_spot = float(df["close"].iloc[-1])

    dr = dealing_range(current_spot or 0.0, ah, al) if (current_spot and ah and al) else None
    phase = detect_market_phase(df, ps, st, dr, liq)

    # Macro dealing range across full session / visible window
    macro_dr = None
    if df is not None and len(df) >= 5:
        m_high = float(df["high"].max())
        m_low = float(df["low"].min())
        if m_high > m_low:
            macro_dr = dealing_range(
                current_spot or 0.0,
                Pivot("HIGH", 0, m_high, datetime.now(), 0),
                Pivot("LOW", 0, m_low, datetime.now(), 0)
            )

    intermediate_r = {
        "pivots": ps, "structure": st, "fvgs": gaps,
        "fvg_tracker": tracker, "order_blocks": obs,
        "liquidity": liq, "liquidity_pools": pools,
        "dealing_range": dr or macro_dr, "liquidity_levels": liq_levels,
    }
    sniper_confluence = detect_sniper_confluence(df, current_spot or 0.0, intermediate_r)

    breaker_obs = [b for b in obs if b.is_breaker]
    active_breakers = [b for b in breaker_obs if b.breaker_active]
    swept_levels = [l for l in liq_levels if l.swept]

    return {
        "pivots": ps, "structure": st, "fvgs": gaps,
        "fvg_tracker": tracker,
        "order_blocks": obs, "liquidity": liq,
        "liquidity_pools": pools,
        "open_pools": [p for p in pools if not p.swept],
        "dealing_range": dr,
        "macro_dealing_range": macro_dr,
        "active_high": ah,
        "active_low": al,
        "open_fvgs": [g for g in gaps if g.open],
        "unmitigated_fvgs": [g for g in gaps if g.is_unmitigated],
        "iofed_fvgs": [g for g in gaps if g.fill_state == "iofed_filled"],
        "ce_fvgs": [g for g in gaps if g.fill_state == "ce_filled"],
        "full_filled_fvgs": [g for g in gaps if g.is_full_filled],
        "open_obs": [b for b in obs if b.open],
        "unmitigated_obs": [b for b in obs if b.unmitigated],
        "breaker_obs": breaker_obs,
        "active_breakers": active_breakers,
        "trendlines": trendlines,
        "market_phase": phase,
        "liquidity_levels": liq_levels,
        "liquidity_sweeps": swept_levels,
        "sniper_confluence": sniper_confluence,
        "confirm_lag": n,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 8 · The Quantitative Bridge (SMC × Option Chain Confluence)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BridgeSignal:
    title: str                    # e.g. "FAILED LIQUIDITY SWEEP (BULL TRAP)"
    verdict: str                  # "HIGH CONVICTION SHORT", etc.
    sentiment: str                # "BULLISH" | "BEARISH"
    confidence: str               # "HIGH" | "MODERATE"
    trigger_detail: str           # Price action behavior on OHLCV
    chain_detail: str             # Institutional writing / covering on chain
    strike: int
    level: float
    time: str


def strike_imbalance(pe_chg_oi: float, ce_chg_oi: float) -> float | None:
    """Strike Imbalance: (ΔOI_PE - ΔOI_CE) / (|ΔOI_PE| + |ΔOI_CE|)"""
    denom = abs(pe_chg_oi) + abs(ce_chg_oi)
    if denom == 0 or np.isnan(denom):
        return None
    return (pe_chg_oi - ce_chg_oi) / denom


def confluence_bridge(chain_df: pd.DataFrame | None, smc_res: dict, spot: float,
                      step: int = 50) -> list[BridgeSignal]:
    """
    The Quantitative Bridge: combines OHLCV SMC signals with Option Chain positioning.
      1. Failed Liquidity Sweep (Bull Trap / Bear Trap confirmation with Short Buildup)
      2. Active Order Block Defense (Price in OB confirmed by writer reinforcement)
      3. True Breakout / Breakdown (BOS confirmed by Wall Surrender / Short Covering)
    """
    if chain_df is None or chain_df.empty or not smc_res:
        return []

    signals: list[BridgeSignal] = []

    def get_strike(k: int):
        rows = chain_df[chain_df.strike == k]
        return rows.iloc[0] if not rows.empty else None

    # 1 · Liquidity Sweeps × Strike Defense (Failed Sweeps / Traps)
    sweeps = [x for x in smc_res.get("liquidity", []) if x.kind == "SWEEP"]
    for sw in sweeps[-4:]:
        k = int(round(sw.level / step) * step)
        row = get_strike(k)
        if row is None:
            continue
        t_str = sw.at.strftime("%H:%M") if hasattr(sw.at, "strftime") else str(sw.at)

        if sw.direction == "ABOVE":
            ce_oi = getattr(row, "ce_chg_oi", 0.0) or 0.0
            ce_p = getattr(row, "ce_chg", 0.0) or 0.0
            if ce_oi > 2000 and ce_p <= 0:
                signals.append(BridgeSignal(
                    title="FAILED LIQUIDITY SWEEP (BULL TRAP)",
                    verdict="HIGH CONVICTION SHORT",
                    sentiment="BEARISH",
                    confidence="HIGH" if ce_oi > 20000 else "MODERATE",
                    trigger_detail=(f"15m wick pushed to {sw.wick:,.1f} above prior high {sw.level:,.1f} "
                                    f"but closed back inside ({sw.close:,.1f})"),
                    chain_detail=(f"Nearest {k:,} CE absorbed retail breakout with Short Buildup "
                                  f"(+{int(ce_oi):,} OI, ΔP {ce_p:+.1f})"),
                    strike=k, level=sw.level, time=t_str
                ))
        elif sw.direction == "BELOW":
            pe_oi = getattr(row, "pe_chg_oi", 0.0) or 0.0
            pe_p = getattr(row, "pe_chg", 0.0) or 0.0
            if pe_oi > 2000 and pe_p <= 0:
                signals.append(BridgeSignal(
                    title="FAILED LIQUIDITY SWEEP (BEAR TRAP)",
                    verdict="HIGH CONVICTION LONG",
                    sentiment="BULLISH",
                    confidence="HIGH" if pe_oi > 20000 else "MODERATE",
                    trigger_detail=(f"15m wick dipped to {sw.wick:,.1f} below prior low {sw.level:,.1f} "
                                    f"but closed back inside ({sw.close:,.1f})"),
                    chain_detail=(f"Nearest {k:,} PE defended the floor with Short Buildup "
                                  f"(+{int(pe_oi):,} OI, ΔP {pe_p:+.1f})"),
                    strike=k, level=sw.level, time=t_str
                ))

    # 2 · Order Block Retest × Strike Defense
    for ob in smc_res.get("open_obs", []):
        if ob.contains(spot):
            k = int(round(spot / step) * step)
            row = get_strike(k)
            if row is None:
                continue
            t_str = ob.at.strftime("%H:%M") if hasattr(ob.at, "strftime") else str(ob.at)
            if ob.direction == "BEARISH":
                ce_oi = getattr(row, "ce_chg_oi", 0.0) or 0.0
                if ce_oi > 5000:
                    signals.append(BridgeSignal(
                        title="BEARISH ORDER BLOCK DEFENSE",
                        verdict="SUPPLY REJECTION SHORT",
                        sentiment="BEARISH",
                        confidence="HIGH" if ce_oi > 30000 else "MODERATE",
                        trigger_detail=f"Spot {spot:,.1f} is currently inside Bearish OB [{ob.low:,.0f} – {ob.high:,.0f}]",
                        chain_detail=f"{k:,} CE writers are actively reinforcing (+{int(ce_oi):,} OI building ceiling)",
                        strike=k, level=(ob.low + ob.high) / 2.0, time=t_str
                    ))
            elif ob.direction == "BULLISH":
                pe_oi = getattr(row, "pe_chg_oi", 0.0) or 0.0
                if pe_oi > 5000:
                    signals.append(BridgeSignal(
                        title="BULLISH ORDER BLOCK DEFENSE",
                        verdict="DEMAND BOUNCE LONG",
                        sentiment="BULLISH",
                        confidence="HIGH" if pe_oi > 30000 else "MODERATE",
                        trigger_detail=f"Spot {spot:,.1f} is currently inside Bullish OB [{ob.low:,.0f} – {ob.high:,.0f}]",
                        chain_detail=f"{k:,} PE writers are actively reinforcing (+{int(pe_oi):,} OI building floor)",
                        strike=k, level=(ob.low + ob.high) / 2.0, time=t_str
                    ))

    # 3 · Break of Structure × Wall Surrender (True Breakout vs Suspect)
    stx = smc_res.get("structure")
    if stx and stx.events and (stx.bars_since_event is None or stx.bars_since_event <= 3):
        last_ev = stx.last
        if last_ev and last_ev.kind in ("BOS", "CHOCH"):
            k = int(round(last_ev.level / step) * step)
            row = get_strike(k)
            if row is not None:
                t_str = last_ev.at.strftime("%H:%M") if hasattr(last_ev.at, "strftime") else str(last_ev.at)
                if last_ev.direction == "BULLISH":
                    ce_oi = getattr(row, "ce_chg_oi", 0.0) or 0.0
                    ce_p = getattr(row, "ce_chg", 0.0) or 0.0
                    if ce_oi < -2000 and ce_p > 0:
                        signals.append(BridgeSignal(
                            title=f"TRUE BREAKOUT ({last_ev.kind})",
                            verdict="GAMMA EXPANSION LONG",
                            sentiment="BULLISH",
                            confidence="HIGH",
                            trigger_detail=f"Candle closed at {last_ev.close:,.1f} breaking above {last_ev.level:,.1f}",
                            chain_detail=f"{k:,} CE call writers surrendered ({int(ce_oi):,} OI unwound, Short Covering). Wall collapsed.",
                            strike=k, level=last_ev.level, time=t_str
                        ))
                elif last_ev.direction == "BEARISH":
                    pe_oi = getattr(row, "pe_chg_oi", 0.0) or 0.0
                    pe_p = getattr(row, "pe_chg", 0.0) or 0.0
                    if pe_oi < -2000 and pe_p > 0:
                        signals.append(BridgeSignal(
                            title=f"TRUE BREAKDOWN ({last_ev.kind})",
                            verdict="GAMMA EXPANSION SHORT",
                            sentiment="BEARISH",
                            confidence="HIGH",
                            trigger_detail=f"Candle closed at {last_ev.close:,.1f} breaking below {last_ev.level:,.1f}",
                            chain_detail=f"{k:,} PE put writers surrendered ({int(pe_oi):,} OI unwound, Long Unwinding). Floor collapsed.",
                            strike=k, level=last_ev.level, time=t_str
                        ))

    # Deduplicate signals by title and strike
    deduped = []
    seen = set()
    for s in signals:
        key = (s.title, s.strike, s.sentiment)
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped

