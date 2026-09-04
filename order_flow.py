"""
order_flow.py — tape speed and volume profile from the futures candles.

WHAT IS HERE, AND WHAT IS NOT
─────────────────────────────
Two of the three requested pieces are built:

  SPEED. Volume in the last minute against the rolling average of the prior
  fifteen. A break on accelerating volume is being taken; the same break on
  stalling volume is price drifting through a level nobody is defending and
  nobody is buying.

  VOLUME PROFILE. Where the day's volume actually traded — point of control,
  and the 70% value area. Price returning to the POC is returning to where
  business was done; price leaving the value area is leaving where everyone
  agreed on price.

The third — AGGRESSION VERSUS PASSIVE LIQUIDITY — is NOT here, and the
reason is worth stating rather than substituting something.

That metric needs Level 2 depth: bid and ask quantities at five levels,
updating continuously. Three things stand between this app and that:

  Depth arrives over Dhan's websocket feed, not the REST endpoints this app
  uses. Streamlit reruns its whole script on every interaction, which is
  close to the worst possible host for a persistent socket.

  Depth is a SNAPSHOT of resting orders. Polling it every three minutes
  tells you almost nothing — the book reloads in milliseconds, and the
  quantity you sampled was gone before you read it.

  The suggested fallback, "calculate proxy aggressive delta from 1-minute
  price/volume momentum", is not an imbalance ratio. It is a different
  measurement wearing the same name. Bid-ask imbalance says what is RESTING
  in the book; price-volume momentum says what has ALREADY TRADED. Labelling
  the second as the first would put a number called "liquidity imbalance" on
  screen that contains no liquidity data, and you would reasonably read it
  as depth.

So speed is reported honestly as speed. If depth becomes worth building, it
needs a separate always-on process feeding a file this app reads — not a
proxy inside a Streamlit rerun.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

import config


# ═══════════════════════════════════════════════════════════════════════════
# Tape speed
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Speed:
    last: float
    baseline: float
    bars_used: int

    @property
    def ratio(self) -> float | None:
        return self.last / self.baseline if self.baseline > 0 else None

    @property
    def state(self) -> str:
        r = self.ratio
        if r is None:
            return "NO BASELINE"
        if r >= 2.0:
            return "ACCELERATED"
        if r >= 0.8:
            return "STEADY"
        return "STALLED"

    @property
    def colour(self) -> str:
        return {"ACCELERATED": "#0F9D58", "STEADY": "#8A8F98",
                "STALLED": "#E8503A"}.get(self.state, "#8A8F98")

    @property
    def note(self) -> str:
        r = self.ratio
        if r is None:
            return "no baseline volume yet"
        base = (f"{self.last:,.0f} against a {self.baseline:,.0f} average "
                f"over {self.bars_used} prior minutes ({r:.2f}x).")
        if self.state == "ACCELERATED":
            return base + (" Volume is arriving faster than usual — whatever "
                           "is happening is being participated in.")
        if self.state == "STALLED":
            return base + (" Volume has dried up. A level break on this is "
                           "price drifting through, not being taken.")
        return base + " Ordinary pace, nothing to read from it."


def speed(candles: pd.DataFrame, lookback: int = 15) -> Speed | None:
    """
    Last minute's volume against the average of the prior `lookback`.

    The baseline EXCLUDES the last bar. Including it would put the value
    being measured into its own denominator, which drags every reading
    toward 1.0 and flattens exactly the spikes the metric exists to catch.

    Bars outside market hours are dropped first — an overnight bar with zero
    volume in the baseline makes the ratio meaningless.
    """
    if candles is None or candles.empty or "volume" not in candles.columns:
        return None
    d = candles.copy()
    ts = pd.to_datetime(d["timestamp"] if "timestamp" in d.columns else d.index)
    d = d[(ts.dt.time >= config.MARKET_OPEN)
          & (ts.dt.time <= config.MARKET_CLOSE)]
    if len(d) < 3:
        return None

    vol = d["volume"].astype(float).to_numpy()
    last = float(vol[-1])
    prior = vol[max(0, len(vol) - 1 - lookback):-1]
    prior = prior[prior > 0]
    if prior.size == 0:
        return None
    return Speed(last, float(np.mean(prior)), int(prior.size))


# ═══════════════════════════════════════════════════════════════════════════
# Volume profile
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Profile:
    poc: float
    vah: float
    val: float
    total_volume: float
    buckets: pd.DataFrame = field(default_factory=pd.DataFrame)
    bucket_pts: int = 10

    @property
    def value_width(self) -> float:
        return self.vah - self.val

    def where(self, price: float) -> str:
        if price > self.vah:
            return (f"above the value area ({self.vah:,.0f}) — trading where "
                    f"little business was done today")
        if price < self.val:
            return (f"below the value area ({self.val:,.0f}) — trading where "
                    f"little business was done today")
        d = price - self.poc
        return (f"inside the value area, {abs(d):.0f} pts "
                f"{'above' if d > 0 else 'below'} the POC ({self.poc:,.0f})")


def volume_profile(candles: pd.DataFrame, bucket: int = 10,
                   value_pct: float = 0.70) -> Profile | None:
    """
    Where the session's volume traded, bucketed by price.

    Each bar's volume is assigned to the bucket containing its TYPICAL price
    (H+L+C)/3, not its close. A one-minute bar with a twenty-point range did
    not trade entirely at the close, and putting all its volume there builds
    a profile with spikes that never existed.

    This remains an approximation — a true profile needs tick data, and
    spreading a bar's volume across its range would be a different
    approximation, not a better one. It is close enough to locate the POC
    within a bucket or two, which is what the POC is used for.
    """
    if candles is None or candles.empty or "volume" not in candles.columns:
        return None
    d = candles.copy()
    ts = pd.to_datetime(d["timestamp"] if "timestamp" in d.columns else d.index)
    d = d[(ts.dt.time >= config.MARKET_OPEN)
          & (ts.dt.time <= config.MARKET_CLOSE)]
    if d.empty:
        return None

    tp = (d["high"].astype(float) + d["low"].astype(float)
          + d["close"].astype(float)) / 3.0
    vol = d["volume"].astype(float)
    if vol.sum() <= 0:
        return None

    lvl = (tp / bucket).round().astype(int) * bucket
    grp = (pd.DataFrame({"price": lvl, "volume": vol})
           .groupby("price", as_index=False)["volume"].sum()
           .sort_values("price").reset_index(drop=True))
    if grp.empty:
        return None

    total = float(grp["volume"].sum())
    poc_i = int(grp["volume"].idxmax())
    poc = float(grp.loc[poc_i, "price"])

    # Value area: expand outward from the POC, always taking the heavier
    # neighbour, until the target share of volume is enclosed. This is the
    # standard construction and it matters that it grows from the POC rather
    # than trimming the tails — a bimodal session has its volume in two
    # places, and trimming would straddle a gap where nothing traded.
    lo = hi = poc_i
    acc = float(grp.loc[poc_i, "volume"])
    target = total * value_pct
    while acc < target and (lo > 0 or hi < len(grp) - 1):
        below = float(grp.loc[lo - 1, "volume"]) if lo > 0 else -1.0
        above = float(grp.loc[hi + 1, "volume"]) if hi < len(grp) - 1 else -1.0
        if above >= below:
            hi += 1
            acc += above
        else:
            lo -= 1
            acc += below

    return Profile(poc=poc, vah=float(grp.loc[hi, "price"]),
                   val=float(grp.loc[lo, "price"]), total_volume=total,
                   buckets=grp, bucket_pts=bucket)


# ═══════════════════════════════════════════════════════════════════════════
# Proximity
# ═══════════════════════════════════════════════════════════════════════════

def proximity(spot: float, levels: dict, within: float = 15.0) -> list:
    """
    Levels the price is sitting on, nearest first.

    Deliberately not merged with speed or the profile. "Price is near R1" and
    "volume is accelerating" are separate facts, and combining them into one
    verdict inside this module would hide which of the two was actually
    driving it.
    """
    out = []
    for name, lv in (levels or {}).items():
        try:
            v = float(lv)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        d = spot - v
        if abs(d) <= within:
            out.append({"name": name, "level": v, "distance": d,
                        "side": "above" if d > 0 else "below"})
    return sorted(out, key=lambda x: abs(x["distance"]))


# ═══════════════════════════════════════════════════════════════════════════
# Level 2 Depth (Aggression vs Passive Liquidity)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DepthImbalance:
    bid_qty_5: int
    ask_qty_5: int
    imbalance_5: float           # -1.0 to +1.0
    total_buy: int
    total_sell: int
    ltp: float

    @property
    def state(self) -> str:
        if self.imbalance_5 >= 0.20:
            return "BUYERS DOMINATING"
        if self.imbalance_5 <= -0.20:
            return "SELLERS DOMINATING"
        return "BALANCED"

    @property
    def colour(self) -> str:
        if self.imbalance_5 >= 0.20:
            return "#0F9D58"
        if self.imbalance_5 <= -0.20:
            return "#E8503A"
        return "#8A8F98"

    @property
    def note(self) -> str:
        pct = self.imbalance_5 * 100
        return (f"Top 5 Bids: {self.bid_qty_5:,} vs Asks: {self.ask_qty_5:,} "
                f"({pct:+.1f}% imbalance). {self.state} on Level 2 depth.")


def market_depth() -> DepthImbalance | None:
    """Fetch live 5-depth order book from Dhan when configured."""
    try:
        import dhan_client as dc
        res = dc.futures_market_depth()
        if not res:
            return None
        return DepthImbalance(
            bid_qty_5=res["bid_qty_5"],
            ask_qty_5=res["ask_qty_5"],
            imbalance_5=res["imbalance_5"],
            total_buy=res["total_buy"],
            total_sell=res["total_sell"],
            ltp=res["ltp"]
        )
    except Exception:
        return None


def read(candles: pd.DataFrame, spot: float, levels: dict | None = None,
         within: float = 15.0) -> dict:
    """Everything in one call, for the dashboard card."""
    sp = speed(candles)
    pr = volume_profile(candles)
    dp = market_depth()
    near = proximity(spot, levels or {}, within)
    return {
        "speed": sp, "profile": pr, "depth": dp, "near": near,
        "headline": _headline(sp, pr, dp, near, spot),
    }


def _headline(sp, pr, dp, near, spot) -> str:
    bits = []
    if sp:
        bits.append(f"tape {sp.state}"
                    + (f" ({sp.ratio:.1f}x)" if sp.ratio else ""))
    if dp:
        bits.append(f"depth {dp.imbalance_5:+.0%}")
    if pr:
        bits.append(f"POC {pr.poc:,.0f}")
    if near:
        n = near[0]
        bits.append(f"{abs(n['distance']):.0f} pts from {n['name']}")
    return " · ".join(bits) if bits else "no order-flow data"
