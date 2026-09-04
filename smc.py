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
# 2 · Structure: BOS and CHoCH
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StructureEvent:
    kind: str                     # BOS | CHOCH
    direction: str                # BULLISH | BEARISH
    i: int
    at: datetime
    level: float
    close: float

    @property
    def label(self) -> str:
        return f"{self.direction} {self.kind} @ {self.level:,.1f}"


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
    Walk the bars, tracking trend, emitting BOS and CHoCH.

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
                    "CHOCH", "BULLISH", i, ts[i], sh.price, float(c[i])))
                st.trend = "BULLISH"
            elif st.trend in ("BULLISH", "TRANSITION"):
                st.events.append(StructureEvent(
                    "BOS", "BULLISH", i, ts[i], sh.price, float(c[i])))
                st.trend = "BULLISH"
        elif sl is not None and c[i] < sl.price:
            if st.trend == "BULLISH":
                st.events.append(StructureEvent(
                    "CHOCH", "BEARISH", i, ts[i], sl.price, float(c[i])))
                st.trend = "BEARISH"
            elif st.trend in ("BEARISH", "TRANSITION"):
                st.events.append(StructureEvent(
                    "BOS", "BEARISH", i, ts[i], sl.price, float(c[i])))
                st.trend = "BEARISH"

    if st.events:
        st.bars_since_event = len(df) - 1 - st.events[-1].i
    return st


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

    for i in range(len(df)):
        sh = active(ps, i, "HIGH")
        if sh is not None:
            if h[i] > sh.price and c[i] <= sh.price:
                out.append(Liquidity("SWEEP", "ABOVE", i, ts[i], sh.price,
                                     float(h[i]), float(c[i])))
            elif (c[i] > sh.price and i + 1 < len(df)
                    and c[i + 1] > sh.price):
                out.append(Liquidity("RUN", "ABOVE", i, ts[i], sh.price,
                                     float(h[i]), float(c[i])))
        sl = active(ps, i, "LOW")
        if sl is not None:
            if l[i] < sl.price and c[i] >= sl.price:
                out.append(Liquidity("SWEEP", "BELOW", i, ts[i], sl.price,
                                     float(l[i]), float(c[i])))
            elif (c[i] < sl.price and i + 1 < len(df)
                    and c[i + 1] < sl.price):
                out.append(Liquidity("RUN", "BELOW", i, ts[i], sl.price,
                                     float(l[i]), float(c[i])))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Fair value gaps
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FVG:
    direction: str                # BULLISH | BEARISH
    i: int
    at: datetime
    low: float
    high: float
    mitigated_i: int | None = None
    mitigated_pct: float = 0.0

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    @property
    def size(self) -> float:
        return self.high - self.low

    @property
    def open(self) -> bool:
        return self.mitigated_i is None


def fvgs(df: pd.DataFrame, mitigate_at: float = 0.5) -> list[FVG]:
    """
    Three-bar imbalances, with mitigation tracked to a threshold.

    `mitigate_at` is the fraction of the zone price must penetrate to count
    as mitigated — 0.5 by default, so a gap is spent when price has taken
    half of it. Treating first touch as mitigation would close nearly every
    gap on the next bar; treating full fill as the bar would leave gaps open
    that no longer offer anything.
    """
    if df is None or len(df) < 3:
        return []
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    out: list[FVG] = []

    for i in range(2, len(df)):
        if l[i] > h[i - 2]:
            out.append(FVG("BULLISH", i, ts[i], float(h[i - 2]), float(l[i])))
        elif h[i] < l[i - 2]:
            out.append(FVG("BEARISH", i, ts[i], float(h[i]), float(l[i - 2])))

    for g in out:
        for j in range(g.i + 1, len(df)):
            if g.direction == "BULLISH":
                pen = (g.high - l[j]) / g.size if g.size else 1.0
            else:
                pen = (h[j] - g.low) / g.size if g.size else 1.0
            if pen >= mitigate_at:
                g.mitigated_i = j
                g.mitigated_pct = float(min(pen, 1.0))
                break
            g.mitigated_pct = float(max(g.mitigated_pct, max(pen, 0.0)))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# 5 · Order blocks
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

    @property
    def open(self) -> bool:
        """Still standing (not invalidated)."""
        return self.invalidated_i is None

    @property
    def unmitigated(self) -> bool:
        """Standing and fresh (never entered by subsequent candles)."""
        return self.invalidated_i is None and self.mitigated_i is None

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def order_blocks(df: pd.DataFrame, st: Structure,
                 gaps: list[FVG], lookback: int = 6) -> list[OrderBlock]:
    """
    The origin candle of a move that produced both an FVG and a BOS.

    Both conditions are required. An FVG alone is a gap; a BOS alone is a
    break. The order block is specifically the last opposite-coloured candle
    before a move that did both — that conjunction is what distinguishes it
    from any random candle before a rally.

    Tracks:
      - Invalidation: when a close penetrates fully through the far edge
      - Mitigation/Retest: when price pulls back into the [low, high] zone
    """
    if df is None or df.empty or not st.events:
        return []
    o = df["open"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    out: list[OrderBlock] = []

    for ev in st.events:
        if ev.kind not in ("BOS", "CHOCH"):
            continue
        want = "BULLISH" if ev.direction == "BULLISH" else "BEARISH"
        near_gap = any(g.direction == want and abs(g.i - ev.i) <= lookback
                       for g in gaps)
        if not near_gap:
            continue
        # walk back for the last candle of the opposite colour
        found = None
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
            # Invalidation takes priority
            if (want == "BULLISH" and c[k] < ob.low) or \
               (want == "BEARISH" and c[k] > ob.high):
                ob.invalidated_i = k
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

    return sorted(pools, key=lambda p: p.last_i)


# ═══════════════════════════════════════════════════════════════════════════
# 7 · Dealing range (Premium / Discount / Equilibrium)
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


def dealing_range(spot: float, active_high: Pivot | None,
                  active_low: Pivot | None) -> DealingRange | None:
    """Classifies current price within the active dealing range."""
    if active_high is None or active_low is None:
        return None
    h, l = active_high.price, active_low.price
    if h <= l:
        return None
    rng = h - l
    eq = (h + l) / 2.0
    pos = (spot - l) / rng
    if spot > eq + (rng * 0.03):
        zone = "PREMIUM"
    elif spot < eq - (rng * 0.03):
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"
    return DealingRange(
        high=h, low=l, equilibrium=eq, size=rng,
        current_price=spot, position_pct=pos, zone=zone
    )


# ═══════════════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════════════

def read(df: pd.DataFrame, n: int = 2, spot: float | None = None) -> dict:
    """Everything, for the tab."""
    ps = pivots(df, n)
    st = structure(df, ps)
    gaps = fvgs(df)
    obs = order_blocks(df, st, gaps)
    liq = liquidity(df, ps)
    pools = liquidity_pools(df, ps)
    last = len(df) - 1 if df is not None and len(df) else 0
    ah = active(ps, last, "HIGH")
    al = active(ps, last, "LOW")

    current_spot = spot
    if current_spot is None and df is not None and len(df):
        current_spot = float(df["close"].iloc[-1])

    dr = dealing_range(current_spot or 0.0, ah, al) if (current_spot and ah and al) else None

    return {
        "pivots": ps, "structure": st, "fvgs": gaps,
        "order_blocks": obs, "liquidity": liq,
        "liquidity_pools": pools,
        "open_pools": [p for p in pools if not p.swept],
        "dealing_range": dr,
        "active_high": ah,
        "active_low": al,
        "open_fvgs": [g for g in gaps if g.open],
        "open_obs": [b for b in obs if b.open],
        "unmitigated_obs": [b for b in obs if b.unmitigated],
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

    return signals

