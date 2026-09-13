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
AI_WEIGHT = 2.5


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
                max_width: float | None = None,
                ai_levels: list[tuple[float, str, str]] | None = None) -> list[Zone]:
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
    ] + [
        (p, k, lab, AI_WEIGHT) for p, k, lab in (ai_levels or [])
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
# Tactical Battlefield & Price-Level Decision Engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TacticalBattlefield:
    spot: float
    immediate_support: Zone | None
    immediate_resistance: Zone | None
    major_support: Zone | None
    major_resistance: Zone | None
    corridor_pts: float
    position_pct: float
    tactical_regime: str           # 'VALUE_BUY_ZONE', 'RESISTANCE_SELL_ZONE', 'MID_AIR_CHOP'
    risk_reward_long: float
    risk_reward_short: float
    headline: str
    action_advice: str
    playbook_dip_buy: dict
    playbook_wall_fade: dict
    playbook_squeeze_breakout: dict
    resistances_above: list[Zone]
    supports_below: list[Zone]


def get_tactical_battlefield(zones: list[Zone], spot: float) -> TacticalBattlefield:
    """
    Computes real-time tactical battlefield metrics for Nifty spot:
    Identifies immediate & major S&R, corridor width, positioning percentage,
    Risk:Reward ratios, and actionable trade playbooks at this exact price level.
    """
    valid_zones = [z for z in zones if z.score >= 2.5]

    imm_sup = nearest([z for z in valid_zones if z.kind == "support" and z.score >= 3.0], spot, above=False)
    imm_res = nearest([z for z in valid_zones if z.kind == "resistance" and z.score >= 3.0], spot, above=True)

    maj_sup = nearest([z for z in valid_zones if z.kind == "support" and z.score >= 6.0], spot, above=False) or imm_sup
    maj_res = nearest([z for z in valid_zones if z.kind == "resistance" and z.score >= 6.0], spot, above=True) or imm_res

    sup_p = imm_sup.price if imm_sup else spot - 100.0
    res_p = imm_res.price if imm_res else spot + 100.0

    dist_sup = spot - sup_p
    dist_res = res_p - spot
    corridor = max(res_p - sup_p, 10.0)
    pos_pct = max(0.0, min(100.0, (dist_sup / corridor) * 100.0))

    # Risk:Reward calculations at current spot
    long_risk = max(dist_sup + 20.0, 15.0)
    long_reward = max(dist_res, 15.0)
    rr_long = round(long_reward / long_risk, 2)

    short_risk = max(dist_res + 20.0, 15.0)
    short_reward = max(dist_sup, 15.0)
    rr_short = round(short_reward / short_risk, 2)

    if pos_pct <= 25.0:
        regime = "VALUE_BUY_ZONE"
        headline = f"🟢 VALUE BUY ZONE: Spot is within {dist_sup:.1f} pts of Support Floor ({sup_p:,.0f})."
        advice = f"High-asymmetry buying opportunity. Favorable R:R ({rr_long}:1) for Long entries. Keep SL tight below {sup_p - 20:,.0f}."
    elif pos_pct >= 75.0:
        regime = "RESISTANCE_SELL_ZONE"
        headline = f"🔴 CEILING REJECTION ZONE: Spot is within {dist_res:.1f} pts of Resistance Wall ({res_p:,.0f})."
        advice = f"Profit-taking / short rejection zone. Avoid chasing Longs here. Watch for rejection wicks or a confirmed squeeze breakout above {res_p + 10:,.0f}."
    else:
        regime = "MID_AIR_CHOP"
        headline = f"⚖️ MID-CORRIDOR ZONE: Spot is {pos_pct:.0f}% inside the Floor ({sup_p:,.0f}) to Ceiling ({res_p:,.0f}) corridor."
        advice = f"Market orders here carry ~1:1 R:R (50% midpoint). Best practice: wait for a dip into Support ({sup_p:,.0f}) or rally into Resistance ({res_p:,.0f})."

    pb_buy = {
        "action": "LONG RELOAD ON DIP",
        "entry_zone": f"{sup_p:,.0f} – {sup_p + 25:,.0f}",
        "sl": f"{sup_p - 25:,.0f} (Risk ~25-35 pts)",
        "target": f"{res_p - 20:,.0f} (+{corridor - 25:.0f} pts)",
        "rr": f"> {round(max(corridor - 25, 30) / 30, 1)} : 1",
        "rationale": f"Supported by {len(imm_sup.members) if imm_sup else 1} confluence levels including {imm_sup.members[0] if imm_sup and imm_sup.members else 'Support Wall'}."
    }

    pb_sell = {
        "action": "WALL REJECTION FADE",
        "entry_zone": f"{res_p - 25:,.0f} – {res_p:,.0f}",
        "sl": f"{res_p + 25:,.0f} (Risk ~25-35 pts)",
        "target": f"{sup_p + 25:,.0f} (+{corridor - 25:.0f} pts)",
        "rr": f"> {round(max(corridor - 25, 30) / 30, 1)} : 1",
        "rationale": f"Capped by {len(imm_res.members) if imm_res else 1} confluence levels including {imm_res.members[0] if imm_res and imm_res.members else 'Resistance Wall'}."
    }

    pb_breakout = {
        "action": "SHORT SQUEEZE BREAKOUT",
        "trigger": f"Confirmed 5m Close > {res_p + 5:,.0f}",
        "entry": f"Retest of {res_p:,.0f}",
        "sl": f"{res_p - 25:,.0f} (Ratchet to Cost at +20 pts)",
        "target": f"{res_p + 150:,.0f}+ (Next Liquidity Pool)",
        "rationale": "Forced unwinding of call writers triggers explosive momentum expansion."
    }

    res_above = sorted([z for z in valid_zones if z.price > spot], key=lambda z: z.price)
    sup_below = sorted([z for z in valid_zones if z.price < spot], key=lambda z: -z.price)

    return TacticalBattlefield(
        spot=spot,
        immediate_support=imm_sup,
        immediate_resistance=imm_res,
        major_support=maj_sup,
        major_resistance=maj_res,
        corridor_pts=round(corridor, 1),
        position_pct=round(pos_pct, 1),
        tactical_regime=regime,
        risk_reward_long=rr_long,
        risk_reward_short=rr_short,
        headline=headline,
        action_advice=advice,
        playbook_dip_buy=pb_buy,
        playbook_wall_fade=pb_sell,
        playbook_squeeze_breakout=pb_breakout,
        resistances_above=res_above,
        supports_below=sup_below
    )


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


# ═══════════════════════════════════════════════════════════════════════════
# Google TimesFM 3.0 AI Quantile Confluence Gate & Cost-to-Cost (CTC) Filter
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AIQuantileGateResult:
    status: str                         # "CLEAR_LONG", "CLEAR_SHORT", "BLOCKED_CAP", "BLOCKED_FLOOR", "NEUTRAL"
    spot: float
    q10_floor: float                    # Statistical floor / Support
    q50_median: float                   # Dynamic Target 1 / Mean reversion anchor
    q90_cap: float                      # Statistical cap / Resistance
    dist_to_cap: float                  # Points between spot and q90 cap
    dist_to_floor: float                # Points between spot and q10 floor
    blocked_ce: bool                    # True if within 15 pts of q90 cap (upside saturated)
    blocked_pe: bool                    # True if within 15 pts of q10 floor (downside saturated)
    clear_for_ce: bool                  # True if ample headroom (>= 20 pts to cap)
    clear_for_pe: bool                  # True if ample runway (>= 20 pts to floor)
    target_1_median: float              # Target 1 (q50 anchor)
    ctc_trigger_pts: float = 20.0       # Excursion threshold to shift SL to Cost-to-Cost
    engine: str = "TimesFM 3.0"
    message: str = ""
    gate_color: str = "#2FBF71"


# Module-level cache for TimesFM forecast to avoid recomputing repeatedly in the same poll
_AI_GATE_CACHE: dict = {}


def evaluate_ai_confluence_gate(
    spot: float,
    df_15m: pd.DataFrame | None = None,
    df_5m: pd.DataFrame | None = None,
    horizon: int = 25,
    gate_margin: float = 15.0,
    ctc_trigger_pts: float = 20.0,
) -> AIQuantileGateResult:
    """
    Evaluates the Google TimesFM 3.0 Quantile Cone Envelope & Cost-to-Cost (CTC) Filter.

    Empirical Backtest Findings:
      • Coverage: 87.0% of candles remain strictly contained inside the q10-q90 cone.
      • Cap Filter: Never buy CE into the q90 Cap. If Nifty is within 10-15 pts of q90,
        upside is statistically saturated (only ~13% chance of sustained expansion).
      • Floor Filter: Never short PE into the q10 Floor. If Nifty is within 10-15 pts of q10,
        downside is statistically saturated.
      • Target 1: Anchor Target 1 to the Median (q50) to lock early gains.
      • Cost-to-Cost (CTC): Shift SL to entry/cost once Spot reaches +20 pts favorable excursion.
    """
    if spot <= 0:
        return AIQuantileGateResult(
            status="NEUTRAL", spot=spot, q10_floor=0.0, q50_median=0.0, q90_cap=0.0,
            dist_to_cap=0.0, dist_to_floor=0.0, blocked_ce=False, blocked_pe=False,
            clear_for_ce=False, clear_for_pe=False, target_1_median=0.0,
            engine="None", message="Awaiting valid spot price", gate_color="#8FA6B8",
        )

    close_series = None
    timeframe = "15m"
    if df_15m is not None and not df_15m.empty and "close" in df_15m.columns and len(df_15m) >= 20:
        close_series = df_15m["close"]
        timeframe = "15m"
    elif df_5m is not None and not df_5m.empty and "close" in df_5m.columns and len(df_5m) >= 20:
        close_series = df_5m["close"]
        timeframe = "5m"
    else:
        try:
            import timesfm_nifty as tfn
            close_series = tfn.load_nifty_data("15m")
            timeframe = "15m"
        except Exception:
            pass

    if close_series is None or len(close_series) < 10:
        q10_floor = round(spot - 70.0, 1)
        q50_median = round(spot, 1)
        q90_cap = round(spot + 70.0, 1)
        engine_name = "Static Volatility Fallback"
    else:
        cache_key = (str(close_series.index[-1]), len(close_series), horizon, timeframe)
        if cache_key in _AI_GATE_CACHE:
            fc = _AI_GATE_CACHE[cache_key]
        else:
            try:
                import timesfm_tab as tft
                fc = tft._compute_forecast(close_series, horizon=horizon, timeframe=timeframe)
                _AI_GATE_CACHE[cache_key] = fc
            except Exception:
                fc = None

        if fc is not None:
            q10_floor = round(float(fc["q10"][-1]), 1)
            q50_median = round(float(fc["median"][-1]), 1)
            q90_cap = round(float(fc["q90"][-1]), 1)
            engine_name = fc.get("engine", "TimesFM 3.0")
        else:
            q10_floor = round(spot - 70.0, 1)
            q50_median = round(spot, 1)
            q90_cap = round(spot + 70.0, 1)
            engine_name = "Volatility Fallback"

    dist_to_cap = round(q90_cap - spot, 1)
    dist_to_floor = round(spot - q10_floor, 1)

    blocked_ce = dist_to_cap <= gate_margin
    blocked_pe = dist_to_floor <= gate_margin

    clear_for_ce = (not blocked_ce) and (dist_to_cap >= 20.0)
    clear_for_pe = (not blocked_pe) and (dist_to_floor >= 20.0)

    if blocked_ce and not blocked_pe:
        status = "BLOCKED_CAP"
        gate_color = "#E8503A"
        message = (
            f"⚠️ CE BLOCKED: Spot ({spot:,.1f}) is within {dist_to_cap:.1f} pts of TimesFM q90 Cap ({q90_cap:,.1f}). "
            f"Upside statistically saturated (>87% containment). Skip Calls; look for reversals or PE."
        )
    elif blocked_pe and not blocked_ce:
        status = "BLOCKED_FLOOR"
        gate_color = "#E8503A"
        message = (
            f"⚠️ PE BLOCKED: Spot ({spot:,.1f}) is within {dist_to_floor:.1f} pts of TimesFM q10 Floor ({q10_floor:,.1f}). "
            f"Downside statistically saturated (>87% containment). Skip Puts; look for bounces or CE."
        )
    elif clear_for_ce and (q50_median >= spot):
        status = "CLEAR_LONG"
        gate_color = "#2FBF71"
        message = (
            f"✅ LONG CLEARANCE: Ample runway (+{dist_to_cap:.1f} pts) to q90 Cap ({q90_cap:,.1f}). "
            f"Target 1 at Median {q50_median:,.1f}. Enforce Cost-to-Cost (CTC) lock at +{ctc_trigger_pts:.0f} pts."
        )
    elif clear_for_pe and (q50_median <= spot):
        status = "CLEAR_SHORT"
        gate_color = "#4A9BD1"
        message = (
            f"✅ SHORT CLEARANCE: Ample runway (-{dist_to_floor:.1f} pts) to q10 Floor ({q10_floor:,.1f}). "
            f"Target 1 at Median {q50_median:,.1f}. Enforce Cost-to-Cost (CTC) lock at +{ctc_trigger_pts:.0f} pts."
        )
    else:
        status = "NEUTRAL"
        gate_color = "#E0A030"
        message = (
            f"⚖️ CONE INTERIOR: Spot is centered inside TimesFM 3.0 envelope "
            f"[{q10_floor:,.1f} · {q50_median:,.1f} · {q90_cap:,.1f}]. Follow price action setup triggers."
        )

    return AIQuantileGateResult(
        status=status,
        spot=spot,
        q10_floor=q10_floor,
        q50_median=q50_median,
        q90_cap=q90_cap,
        dist_to_cap=dist_to_cap,
        dist_to_floor=dist_to_floor,
        blocked_ce=blocked_ce,
        blocked_pe=blocked_pe,
        clear_for_ce=clear_for_ce,
        clear_for_pe=clear_for_pe,
        target_1_median=q50_median,
        ctc_trigger_pts=ctc_trigger_pts,
        engine=engine_name,
        message=message,
        gate_color=gate_color,
    )

