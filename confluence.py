"""
Confluence: where the option chain and the price chart agree.

The whole point. An OI wall at 24,200 is one crowd's positioning. A prior
swing low at 24,190 is where price actually turned. When they land within a
few points of each other, you have two independent reasons for the same
level — and independence is what makes a level worth trading.

Deliberately NOT folded into the bias score in analytics.py. That score
measures option-flow direction; this measures price structure. They are
different axes, and mixing them would make both impossible to evaluate
separately when you backtest. Read them side by side instead: "flow says
bullish, and there is a heavy confluence ceiling 40 points above" is a more
useful sentence than a single blended number that hides both.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import config
from levels import PriceLevel, WEIGHTS

#: OI walls get a weight comparable to an unfilled gap — they are the single
#: best-evidenced level type available intraday.
OI_WEIGHT = 3.0


@dataclass
class Zone:
    price: float
    kind: str                       # support | resistance
    score: float
    members: list[str] = field(default_factory=list)

    @property
    def strength(self) -> str:
        if self.score >= 7:  return "VERY STRONG"
        if self.score >= 5:  return "STRONG"
        if self.score >= 3:  return "MODERATE"
        return "WEAK"


def build_zones(price_levels: list[PriceLevel],
                oi_levels: list[tuple[float, str, str]],
                tolerance: float | None = None,
                max_width: float | None = None) -> list[Zone]:
    """
    Cluster nearby levels into zones.

    Two constraints, and the second one matters more than it looks:

      tolerance  max gap between a level and its nearest neighbour in the zone
      max_width  max TOTAL span of a zone, first member to last

    Without the width cap this is single-linkage clustering, which chains: if
    levels sit every 30 points across a 500-point stretch, each one is within
    tolerance of the previous, and they all collapse into a single "zone"
    spanning the whole stretch with a meaningless score in the thirties. A
    zone has to be narrow enough to actually put a stop behind, so anything
    wider than max_width gets split.
    """
    tol = tolerance if tolerance is not None else config.CONFLUENCE_TOLERANCE
    span_cap = max_width if max_width is not None else config.MAX_ZONE_WIDTH

    items: list[tuple[float, str, str, float]] = [
        (l.price, l.kind, l.label, l.weight) for l in price_levels
    ] + [
        (p, k, lab, OI_WEIGHT) for p, k, lab in oi_levels
    ]
    if not items:
        return []

    items.sort(key=lambda x: x[0])
    zones: list[Zone] = []
    bucket: list[tuple[float, str, str, float]] = [items[0]]

    def flush(b):
        if not b:
            return
        total = sum(w for _, _, _, w in b)
        # weighted centre, so a 3.0-weight OI wall pulls the zone toward itself
        centre = sum(p * w for p, _, _, w in b) / total
        res = sum(w for _, k, _, w in b if k == "resistance")
        sup = sum(w for _, k, _, w in b if k == "support")
        zones.append(Zone(round(centre, 1),
                          "resistance" if res >= sup else "support",
                          round(total, 1),
                          [lab for _, _, lab, _ in b]))

    for it in items[1:]:
        near_enough = it[0] - bucket[-1][0] <= tol
        still_narrow = it[0] - bucket[0][0] <= span_cap
        if near_enough and still_narrow:
            bucket.append(it)
        else:
            flush(bucket)
            bucket = [it]
    flush(bucket)

    return sorted(zones, key=lambda z: -z.score)


def nearest(zones: list[Zone], spot: float,
            kind: str | None = None, above: bool | None = None) -> Zone | None:
    pool = [z for z in zones if kind is None or z.kind == kind]
    if above is True:
        pool = [z for z in pool if z.price > spot]
    elif above is False:
        pool = [z for z in pool if z.price < spot]
    return min(pool, key=lambda z: abs(z.price - spot)) if pool else None


def proximity_alerts(zones: list[Zone], spot: float,
                     distance: float | None = None) -> list[str]:
    """
    Fire when spot is close enough to a zone for it to matter to a trade.

    Only zones scoring MODERATE or better raise an alert — the whole point of
    confluence is to filter, and alerting on every isolated Gann band would
    put you straight back where you started.
    """
    dist = distance if distance is not None else config.PROXIMITY_POINTS
    tol = config.CONFLUENCE_TOLERANCE
    out: list[str] = []
    for z in zones:
        if z.score < 3:
            continue
        gap = z.price - spot
        if abs(gap) > dist:
            continue
        if abs(gap) <= tol / 2:
            # Price is sitting inside the zone. It is not a ceiling or a
            # floor right now — it is the level being decided. This is the
            # moment to watch, not to pre-commit to a direction.
            out.append(
                f"{z.strength} ZONE at {z.price:,.0f} — spot is IN it "
                f"({len(z.members)} levels agree). Wait for acceptance or "
                f"rejection before taking a side."
            )
        else:
            where = "above" if gap > 0 else "below"
            out.append(
                f"{z.strength} {z.kind.upper()} at {z.price:,.0f} — "
                f"{abs(gap):.0f} pts {where} spot ({len(z.members)} levels agree)"
            )
    return out


def zones_frame(zones: list[Zone], spot: float, limit: int = 12) -> pd.DataFrame:
    rows = [{
        "Zone": f"{z.price:,.0f}",
        "Type": z.kind,
        "Strength": z.strength,
        "Score": z.score,
        "Levels": len(z.members),
        "Distance": f"{z.price - spot:+,.0f}",
        "Agreeing levels": " · ".join(z.members),
    } for z in zones[:limit]]
    return pd.DataFrame(rows)


def summarise(zones: list[Zone], spot: float) -> str:
    """One line naming the nearest meaningful ceiling and floor."""
    r = nearest([z for z in zones if z.score >= 3], spot, "resistance", above=True)
    s = nearest([z for z in zones if z.score >= 3], spot, "support", above=False)
    parts = []
    if s:
        parts.append(f"floor {s.price:,.0f} ({s.strength.lower()})")
    if r:
        parts.append(f"ceiling {r.price:,.0f} ({r.strength.lower()})")
    return " · ".join(parts) if parts else "no strong confluence zones nearby"


# ═══════════════════════════════════════════════════════════════════════════
# Polarity flips
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Flip:
    """A zone that price has crossed during this session."""
    price: float
    was: str                     # support | resistance — before the break
    broken_at: str               # HH:MM
    strength: str
    members: int
    reclaimed: bool = False      # price went back through it afterwards
    retests: int = 0             # times price has come back to it since

    @property
    def now(self) -> str:
        """Polarity after the break — the inversion is the whole point."""
        return "resistance" if self.was == "support" else "support"

    @property
    def headline(self) -> str:
        if self.reclaimed:
            return (f"{self.price:,.0f} broke {self.was} at {self.broken_at} "
                    f"but has been RECLAIMED — the break failed")
        r = f", {self.retests} retest{'s' if self.retests != 1 else ''}" if self.retests else ""
        return (f"{self.price:,.0f} FLIPPED — was {self.was}, broken "
                f"{self.broken_at}, now acting as {self.now}{r}")

    @property
    def note(self) -> str:
        if self.reclaimed:
            return ("A break that gets reclaimed traps the traders who chased "
                    "it. Failed breaks often reverse hard the other way.")
        side = "long" if self.was == "support" else "short"
        return (f"Traders who were {side} above this level are now underwater. "
                f"They tend to sell into any return to their entry, which is "
                f"why a broken level so often holds from the other side. A "
                f"retest that REJECTS is the setup; a retest that goes "
                f"straight through is the break failing.")


class FlipTracker:
    """
    Watches confluence zones across polls and records when spot crosses one.

    Necessary because every poll re-derives levels from scratch — the app has
    no idea a zone was support at 09:30 and got broken at 11:42 unless
    something remembers. The history is the signal: a level nobody has tested
    is a guess, a level that has been broken and rejected from the far side is
    evidence.

    Only zones scoring MODERATE or better are tracked. A lone Gann band
    flipping is not news.
    """

    def __init__(self) -> None:
        self.flips: dict[float, Flip] = {}
        # Persistent side per zone: "above" | "below". Updated ONLY when spot
        # is decisively clear of the buffer band, so a price sitting inside
        # the band does not erase which side we were last on. Comparing to
        # just the previous poll loses the transition whenever the poll before
        # the break happened to land inside the band.
        self._side: dict[float, str] = {}

    @staticmethod
    def _key(price: float) -> float:
        return round(price / 10) * 10          # tolerate small re-centring

    def update(self, zones: list[Zone], spot: float, when) -> list[Flip]:
        """Call once per poll, after zones are built. Returns fresh flips."""
        fresh: list[Flip] = []
        stamp = when.strftime("%H:%M")
        tol = config.CONFLUENCE_TOLERANCE
        buf = config.RECLAIM_BUFFER

        for z in zones:
            if z.score < 3:
                continue
            k = self._key(z.price)

            # Which side of the level are we decisively on? None means inside
            # the buffer band — undecided, so we neither register a break nor
            # forget the side we were on.
            if spot > z.price + buf:
                side = "above"
            elif spot < z.price - buf:
                side = "below"
            else:
                side = None

            prev_side = self._side.get(k)

            if k in self.flips:
                f = self.flips[k]
                # Reclaim needs the buffer too. Without it, a retest that
                # merely reaches the level registers as a reclaim and the
                # retest signal destroys itself.
                back_through = ((f.was == "support" and side == "above") or
                                (f.was == "resistance" and side == "below"))
                if back_through and not f.reclaimed:
                    f.reclaimed = True
                elif side is None and abs(spot - z.price) <= tol and not f.reclaimed:
                    f.retests += 1
            elif side and prev_side and side != prev_side:
                # A level price was ABOVE and is now BELOW was acting as
                # support; the mirror for resistance.
                was = "support" if prev_side == "above" else "resistance"
                f = Flip(price=z.price, was=was, broken_at=stamp,
                         strength=z.strength, members=len(z.members))
                self.flips[k] = f
                fresh.append(f)

            if side:
                self._side[k] = side
        return fresh

    def active(self, spot: float, within: float | None = None) -> list[Flip]:
        """Flips near enough to spot to matter, unreclaimed first."""
        d = within if within is not None else config.PROXIMITY_POINTS * 2
        out = [f for f in self.flips.values() if abs(f.price - spot) <= d]
        return sorted(out, key=lambda f: (f.reclaimed, abs(f.price - spot)))

    def frame(self) -> pd.DataFrame:
        rows = [{
            "Level": f"{f.price:,.0f}",
            "Was": f.was,
            "Now": "reclaimed" if f.reclaimed else f.now,
            "Broken": f.broken_at,
            "Retests": f.retests,
            "Strength": f.strength,
        } for f in sorted(self.flips.values(), key=lambda x: -x.price)]
        return pd.DataFrame(rows)
