"""
Market state — the engines that interpret rather than display.

Breakout, fake breakout, range trap, pinning, trend structure, market
pressure, and a narrative assembled from actual evidence.

THE PRINCIPLE THAT SHAPES ALL OF IT

Nothing here concludes from a single input. A level crossed is not a
breakout; OI rising is not fresh selling; two touches is not a range. Each
engine requires several independent conditions and reports which ones were
met, so a verdict can always be taken apart and argued with.

Every engine returns SUPPORTING and CONTRADICTING evidence separately. That
is deliberate: a reading with four supports and three contradictions is not
the same as one with four supports and none, even though both would show as
"bullish" on any scoring system that nets them off.

CONFIDENCE IS WORDS, NOT NUMBERS. WEAK / MODERATE / STRONG throughout. A
percentage implies a calibration nobody has performed, and false precision
in a trading tool is worse than admitted vagueness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config


def _conf(supporting: int, contradicting: int) -> str:
    """Confidence from the balance and the volume of evidence."""
    if supporting < 2:
        return "WEAK"
    net = supporting - contradicting
    if supporting >= 4 and net >= 3:
        return "STRONG"
    if supporting >= 3 and net >= 2:
        return "MODERATE"
    return "WEAK"


@dataclass
class Verdict:
    state: str
    confidence: str
    supporting: list[str] = field(default_factory=list)
    contradicting: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def summary(self) -> str:
        return f"{self.state} [{self.confidence}]"


# ═══════════════════════════════════════════════════════════════════════════
# Market structure — HH / HL / LH / LL
# ═══════════════════════════════════════════════════════════════════════════

def swings(df: pd.DataFrame, lookback: int = 2) -> list[dict]:
    """
    Confirmed swing highs and lows.

    A pivot needs `lookback` bars either side, which means the most recent
    `lookback` bars can never form one yet. That lag is not a flaw to
    engineer away — a swing that has not been confirmed by subsequent price
    is not a swing, and treating it as one is how repainting indicators
    mislead people.
    """
    d = df[df["closed"]] if "closed" in df else df
    out = []
    for i in range(lookback, len(d) - lookback):
        hi = float(d["high"].iloc[i])
        lo = float(d["low"].iloc[i])
        left, right = d.iloc[i - lookback:i], d.iloc[i + 1:i + 1 + lookback]
        if hi > left["high"].max() and hi > right["high"].max():
            out.append({"kind": "H", "price": hi,
                        "when": d["timestamp"].iloc[i], "idx": i})
        if lo < left["low"].min() and lo < right["low"].min():
            out.append({"kind": "L", "price": lo,
                        "when": d["timestamp"].iloc[i], "idx": i})
    return sorted(out, key=lambda x: x["idx"])


def trend_structure(df: pd.DataFrame) -> Verdict:
    """
    UPTREND / DOWNTREND / SIDEWAYS / TRANSITION from swing structure.

    Not from an EMA cross. An EMA crossing says the average moved; it says
    nothing about whether the market is still making higher lows. The
    transition state exists because trends do not flip in one bar — a broken
    higher low is a warning, and confirmation needs a lower high after it.
    """
    sw = swings(df)
    highs = [s for s in sw if s["kind"] == "H"]
    lows = [s for s in sw if s["kind"] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return Verdict("INSUFFICIENT STRUCTURE", "WEAK",
                       detail=f"{len(highs)} swing highs, {len(lows)} lows — "
                              f"need 2 of each")

    # A meaningful swing comparison needs a threshold. Two swing highs a
    # quarter-point apart are the same high, and calling that a "Lower High"
    # produced readings like "Lower High 24,250 -> 24,250" — technically true,
    # analytically meaningless, and it flipped the whole verdict.
    tol = max(float(np.nanmedian(np.abs(np.diff([s["price"] for s in sw])))) * 0.2,
              5.0) if len(sw) > 2 else 5.0

    def cmp(a, b):
        if a > b + tol:
            return "higher"
        if a < b - tol:
            return "lower"
        return "equal"

    h_rel = cmp(highs[-1]["price"], highs[-2]["price"])
    l_rel = cmp(lows[-1]["price"], lows[-2]["price"])
    hh, lh = h_rel == "higher", h_rel == "lower"
    hl, ll = l_rel == "higher", l_rel == "lower"

    sup, con = [], []
    close = float((df[df["closed"]] if "closed" in df else df)["close"].iloc[-1])

    if hh:
        sup.append(f"Higher High — {highs[-2]['price']:,.0f} → {highs[-1]['price']:,.0f}")
    if hl:
        sup.append(f"Higher Low — {lows[-2]['price']:,.0f} → {lows[-1]['price']:,.0f}")
    if lh:
        con.append(f"Lower High — {highs[-2]['price']:,.0f} → {highs[-1]['price']:,.0f}")
    if ll:
        con.append(f"Lower Low — {lows[-2]['price']:,.0f} → {lows[-1]['price']:,.0f}")

    if hh and hl:
        state = "UPTREND"
    elif lh and ll:
        state = "DOWNTREND"
        sup, con = con, sup
    elif hh and ll:
        state = "TRANSITION"
        con.append("higher high but lower low — structure is broadening, "
                   "not trending")
    elif lh and hl:
        state = "SIDEWAYS"
        con.append("lower high and higher low — compressing into a range")
    else:
        state = "TRANSITION"

    # a broken last swing low inside an uptrend is the early warning
    if state == "UPTREND" and close < lows[-1]["price"]:
        state = "TRANSITION"
        con.append(f"price {close:,.0f} has broken the last Higher Low "
                   f"({lows[-1]['price']:,.0f}) — waiting on a Lower High to "
                   f"confirm the turn")
    if state == "DOWNTREND" and close > highs[-1]["price"]:
        state = "TRANSITION"
        con.append(f"price {close:,.0f} has reclaimed the last Lower High "
                   f"({highs[-1]['price']:,.0f})")

    # Cross-check against the net move over the same span. The last two
    # swings are a local read and can point against the session; when they
    # disagree, say so rather than quietly trusting the swings.
    span_from = min(highs[-2]["idx"], lows[-2]["idx"])
    d2 = df[df["closed"]] if "closed" in df else df
    net = close - float(d2["close"].iloc[span_from])
    if abs(net) > tol * 4:
        drift = "up" if net > 0 else "down"
        if (state == "UPTREND" and drift == "down") or \
           (state == "DOWNTREND" and drift == "up"):
            con.append(f"swing structure says {state.lower()} but price is "
                       f"net {net:+,.0f} over the same span — the local swings "
                       f"disagree with the session, treat as unresolved")
            state = "TRANSITION"

    if h_rel == "equal":
        con.append(f"swing highs are level ({highs[-1]['price']:,.1f} vs "
                   f"{highs[-2]['price']:,.1f}) — no new direction")
    if l_rel == "equal":
        con.append(f"swing lows are level ({lows[-1]['price']:,.1f} vs "
                   f"{lows[-2]['price']:,.1f})")

    return Verdict(state, _conf(len(sup), len(con)), sup, con,
                   detail=f"{len(highs)} swing highs, {len(lows)} swing lows, "
                          f"tolerance {tol:,.0f} pts, net {net:+,.0f} over span")


# ═══════════════════════════════════════════════════════════════════════════
# Breakout / fake breakout
# ═══════════════════════════════════════════════════════════════════════════

def breakout(df: pd.DataFrame, spot: float, level: float, kind: str,
             chain_level=None, next_building: bool = False,
             accept_bars: int = 2) -> Verdict:
    """
    REAL / POSSIBLE / UNCONFIRMED / FAILED / FAKE — never "price crossed".

    Six conditions are examined and the verdict depends on how many hold:

      1. the level was actually crossed
      2. a bar CLOSED beyond it, not just wicked through
      3. price has ACCEPTED — stayed beyond for `accept_bars` closes
      4. the old level's OI is weakening or unwinding
      5. the next level out is building — positioning has handed over
      6. follow-through — the move continued rather than stalling

    Conditions 4 and 5 together are the structure the notes call stronger
    evidence than the price break itself, and they are what separates a real
    breakout from a stop-run: in a stop-run the writers at that strike do not
    move at all.
    """
    d = df[df["closed"]] if "closed" in df else df
    if len(d) < accept_bars + 2:
        return Verdict("INSUFFICIENT DATA", "WEAK")

    up = kind == "resistance"
    recent = d.tail(accept_bars + 3)
    last = d.iloc[-1]
    close = float(last["close"])

    sup, con = [], []

    crossed = bool((recent["high"] > level).any() if up
                   else (recent["low"] < level).any())
    if not crossed:
        return Verdict("NO BREAK", "STRONG",
                       detail=f"price has not reached {level:,.0f}")
    sup.append(f"{level:,.0f} was reached")

    closes_beyond = d.tail(accept_bars)["close"]
    closed_beyond = bool((closes_beyond > level).all() if up
                         else (closes_beyond < level).all())
    any_closed = bool((closes_beyond > level).any() if up
                      else (closes_beyond < level).any())

    if any_closed:
        sup.append("a bar CLOSED beyond the level, not merely wicked through")
    else:
        con.append("no close beyond the level — wick only, which is a test "
                   "rather than a break")

    if closed_beyond:
        sup.append(f"price ACCEPTED — {accept_bars} consecutive closes beyond")
    else:
        con.append(f"acceptance incomplete — fewer than {accept_bars} closes "
                   f"held beyond the level")

    back_inside = (close < level) if up else (close > level)
    if back_inside and any_closed:
        con.append(f"price has returned inside — now {close:,.0f} against "
                   f"the level at {level:,.0f}")

    if chain_level is not None:
        st = getattr(chain_level, "state", "")
        if st in ("WEAKENING", "UNWINDING", "MIGRATING"):
            sup.append(f"old level is {st} — positioning is leaving it")
        elif st in ("BUILDING", "HOLDING"):
            con.append(f"old level is {st} — writers have NOT stepped back, "
                       f"so nothing has structurally changed")

    if next_building:
        sup.append("the next level out is building — positioning has handed "
                   "over, not merely vanished")
    else:
        con.append("no buildup at the next level — positioning has not "
                   "migrated, which weakens the case")

    if len(d) >= 2:
        prev = float(d["close"].iloc[-2])
        moved_on = (close > prev) if up else (close < prev)
        if moved_on:
            sup.append("follow-through — the move continued on the next bar")
        else:
            con.append("no follow-through — the next bar stalled or reversed")

    n_s, n_c = len(sup), len(con)
    if back_inside and any_closed and n_c >= n_s:
        state = "FAKE BREAKOUT"
    elif back_inside and any_closed:
        state = "FAILED BREAKOUT"
    elif n_s >= 5 and n_c <= 1:
        state = "REAL BREAKOUT"
    elif n_s >= 4:
        state = "POSSIBLE BREAKOUT"
    else:
        state = "UNCONFIRMED BREAKOUT"

    return Verdict(state, _conf(n_s, n_c), sup, con,
                   detail=f"{kind} at {level:,.0f}, spot {spot:,.0f}")


# ═══════════════════════════════════════════════════════════════════════════
# Range trap
# ═══════════════════════════════════════════════════════════════════════════

def range_trap(df: pd.DataFrame, upper: float, lower: float,
               tol: float = 15.0) -> Verdict:
    """
    Repeated failure at both boundaries — the condition that eats accounts.

    Counts touches that FAILED at each edge. A range only qualifies once
    both boundaries have rejected price at least twice, because one rejection
    at each is simply a market that has not gone anywhere yet.
    """
    d = df[df["closed"]] if "closed" in df else df
    if len(d) < 8:
        return Verdict("INSUFFICIENT DATA", "WEAK")

    up_fail = dn_fail = 0
    armed_u = armed_d = True
    for _, b in d.iterrows():
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
        if armed_u and hi >= upper - tol:
            if cl < upper:
                up_fail += 1
            armed_u = False
        elif not armed_u and hi < upper - tol * 2:
            armed_u = True
        if armed_d and lo <= lower + tol:
            if cl > lower:
                dn_fail += 1
            armed_d = False
        elif not armed_d and lo > lower + tol * 2:
            armed_d = True

    width = upper - lower
    close = float(d["close"].iloc[-1])
    inside = lower <= close <= upper

    sup, con = [], []
    if up_fail >= 2:
        sup.append(f"{up_fail} failed attempts at {upper:,.0f}")
    if dn_fail >= 2:
        sup.append(f"{dn_fail} failed attempts at {lower:,.0f}")
    if inside:
        sup.append(f"price {close:,.0f} is back inside the range")
    else:
        con.append(f"price {close:,.0f} is OUTSIDE the range — it may have "
                   f"resolved")
    if width < 60:
        sup.append(f"range only {width:,.0f} pts wide — tight and choppy")
    if up_fail < 2 and dn_fail < 2:
        con.append("boundaries have not each rejected twice — not yet a range")

    state = ("RANGE TRAP" if up_fail >= 2 and dn_fail >= 2 and inside
             else "POSSIBLE RANGE" if (up_fail >= 2 or dn_fail >= 2) and inside
             else "NO RANGE")
    v = Verdict(state, _conf(len(sup), len(con)), sup, con,
                detail=f"{lower:,.0f} – {upper:,.0f}, {width:,.0f} pts wide")
    if state == "RANGE TRAP":
        v.detail += " · 95% of losses come from over-trading this condition"
    return v


# ═══════════════════════════════════════════════════════════════════════════
# Pinning / ATM fight
# ═══════════════════════════════════════════════════════════════════════════

def pinning(chain: pd.DataFrame, spot: float, candles: pd.DataFrame | None,
            atm: int) -> Verdict:
    """
    Both sides adding at the same strike while price sits still.

    Explicitly NOT called bullish or bearish. It is a contested zone, and the
    honest output is to say both sides are committed and wait for the
    resolution rather than pick one.
    """
    row = chain[chain.strike == atm]
    if row.empty:
        return Verdict("NO DATA", "WEAK")
    r = row.iloc[0]

    def v(c):
        x = r.get(c)
        return float(x) if x == x and x is not None else 0.0

    ce_chg, pe_chg = v("ce_chg_oi"), v("pe_chg_oi")
    sup, con = [], []

    both = ce_chg > 0 and pe_chg > 0
    if both:
        sup.append(f"both sides adding at {atm} — CE {ce_chg:+,.0f}, "
                   f"PE {pe_chg:+,.0f}")
        balance = min(ce_chg, pe_chg) / max(ce_chg, pe_chg)
        if balance > 0.6:
            sup.append(f"sides are evenly matched ({balance * 100:.0f}% balance)")
        else:
            con.append(f"one side dominates — {balance * 100:.0f}% balance, "
                       f"so not an even fight")
    else:
        con.append("only one side is adding — not a contested strike")

    if candles is not None and len(candles) >= 6:
        c = candles[candles["closed"]] if "closed" in candles else candles
        rng = float(c["high"].tail(6).max() - c["low"].tail(6).min())
        if rng < 45:
            sup.append(f"price range only {rng:,.0f} pts over 6 bars — pinned")
        else:
            con.append(f"price is moving {rng:,.0f} pts over 6 bars — not pinned")

    if abs(spot - atm) < config.STRIKE_STEP * 0.4:
        sup.append(f"spot {spot:,.0f} is sitting on the {atm} strike")

    state = ("PINNING / ATM FIGHT" if both and len(sup) >= 3
             else "CONTESTED" if both else "NOT PINNED")
    return Verdict(state, _conf(len(sup), len(con)), sup, con,
                   detail=f"ATM {atm}, spot {spot:,.0f}")


# ═══════════════════════════════════════════════════════════════════════════
# What changed
# ═══════════════════════════════════════════════════════════════════════════

def what_changed(log: pd.DataFrame, now: datetime,
                 windows=(1, 3, 5)) -> list[dict]:
    """
    Deltas over the last N minutes, from the signal log.

    Answers the question a trader actually asks on returning to the screen:
    not "what is the state" but "what MOVED while I was away". A score that
    has sat at +4 all morning is a different thing from one that reached +4
    ninety seconds ago.
    """
    if log is None or log.empty or "timestamp" not in log:
        return []
    d = log.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"]).sort_values("timestamp")
    if d.empty:
        return []

    latest = d.iloc[-1]
    out = []
    for mins in windows:
        cutoff = now - timedelta(minutes=mins)
        past = d[d["timestamp"] <= cutoff]
        if past.empty:
            out.append({"window": f"{mins}m", "available": False})
            continue
        then = past.iloc[-1]
        row = {"window": f"{mins}m", "available": True,
               "spot": float(latest.get("spot", 0)) - float(then.get("spot", 0)),
               "score": float(latest.get("bias_score", 0)) - float(then.get("bias_score", 0)),
               "regime_then": str(then.get("regime", "")),
               "regime_now": str(latest.get("regime", "")),
               "r1_then": then.get("r1"), "r1_now": latest.get("r1"),
               "s1_then": then.get("s1"), "s1_now": latest.get("s1")}
        row["regime_changed"] = row["regime_then"] != row["regime_now"]
        row["walls_moved"] = (row["r1_then"] != row["r1_now"]
                              or row["s1_then"] != row["s1_now"])
        out.append(row)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Narrative
# ═══════════════════════════════════════════════════════════════════════════

def narrative(spot: float, atm: int, pillars: dict, levels: dict,
              trend: Verdict | None = None,
              brk: Verdict | None = None,
              pin: Verdict | None = None,
              rng: Verdict | None = None) -> str:
    """
    A paragraph assembled from the evidence, not a template with numbers
    dropped in.

    Only states that were actually detected appear. If nothing conclusive
    happened the narrative says so — a tool that always has something
    dramatic to report teaches you to ignore it.
    """
    parts = [f"NIFTY is trading at {spot:,.0f}, near ATM {atm}."]

    res = levels.get("resistance", [])
    sup_l = levels.get("support", [])
    if res:
        r = res[0]
        parts.append(
            f"{r.strike} resistance is {r.state.lower()}"
            + (f" through {r.pillar_name.lower()}"
               if r.pillar not in ("unknown", "flat") else "")
            + f" (OI {r.oi_pct:+.1f}%).")
    if sup_l:
        s_ = sup_l[0]
        parts.append(
            f"{s_.strike} support is {s_.state.lower()}"
            + (f" through {s_.pillar_name.lower()}"
               if s_.pillar not in ("unknown", "flat") else "")
            + f" (OI {s_.oi_pct:+.1f}%).")

    drive = pillars.get("drive")
    if drive and drive != "NO FLOW":
        parts.append(f"Flow across the near strikes reads {drive.lower()} — "
                     f"{pillars.get('drive_note', '')}")

    if trend and trend.state not in ("INSUFFICIENT STRUCTURE",):
        parts.append(f"Market structure is {trend.state.lower()}.")
    if pin and pin.state.startswith(("PINNING", "CONTESTED")):
        parts.append(f"The ATM strike is contested — {pin.state.lower()}.")
    if rng and rng.state == "RANGE TRAP":
        parts.append(f"Price is range-bound between {rng.detail.split(',')[0]}; "
                     f"this is the condition most losses come from.")
    if brk and brk.state in ("REAL BREAKOUT", "POSSIBLE BREAKOUT",
                             "FAKE BREAKOUT", "FAILED BREAKOUT"):
        parts.append(f"Breakout engine reads {brk.state.lower()} "
                     f"[{brk.confidence.lower()} confidence].")

    if len(parts) <= 2:
        parts.append("Nothing conclusive is happening — most sessions are "
                     "ordinary, and the honest reading is to wait.")
    return " ".join(parts)


def market_pressure(pillars: dict, levels: dict, trend: Verdict | None,
                    vwap_side: str | None = None) -> Verdict:
    """
    Bullish against bearish factors, every one traceable to its source.

    Not netted into a single number. Four supports with three contradictions
    is a genuinely different situation from four supports with none, and
    netting them to "+1" throws that away.
    """
    sup, con = [], []

    for lv in levels.get("resistance", [])[:2]:
        if lv.state in ("WEAKENING", "UNWINDING", "MIGRATING"):
            sup.append(f"resistance {lv.strike} is {lv.state.lower()}")
        elif lv.state == "BUILDING":
            con.append(f"resistance {lv.strike} is building ({lv.oi_pct:+.0f}%)")
    for lv in levels.get("support", [])[:2]:
        if lv.state in ("BUILDING", "HOLDING"):
            sup.append(f"support {lv.strike} is {lv.state.lower()}")
        elif lv.state in ("WEAKENING", "UNWINDING", "MIGRATING"):
            con.append(f"support {lv.strike} is {lv.state.lower()}")

    ce, pe = pillars.get("ce_dominant"), pillars.get("pe_dominant")
    if ce == "short_covering":
        sup.append("call side is short covering")
    if ce == "short_buildup":
        con.append("call side is short buildup — writers defending upside")
    if pe == "short_buildup":
        sup.append("put side is short buildup — writers defending downside")
    if pe == "short_covering":
        con.append("put side is short covering — floor being removed")

    if pillars.get("drive") == "FRESH PARTICIPATION":
        sup.append("move is fresh participation, not exits")
    elif pillars.get("drive") == "EXIT-DRIVEN":
        con.append("move is exit-driven — tends to run out")

    if trend:
        if trend.state == "UPTREND":
            sup.append("market structure is an uptrend")
        elif trend.state == "DOWNTREND":
            con.append("market structure is a downtrend")
        elif trend.state == "TRANSITION":
            con.append("market structure is in transition — unresolved")

    if vwap_side == "above":
        sup.append("price is above VWAP")
    elif vwap_side == "below":
        con.append("price is below VWAP")

    n_s, n_c = len(sup), len(con)
    state = ("BULLISH" if n_s > n_c * 1.5 else
             "BEARISH" if n_c > n_s * 1.5 else "NEUTRAL")
    return Verdict(state, _conf(max(n_s, n_c), min(n_s, n_c)), sup, con,
                   detail=f"{n_s} bullish factors, {n_c} bearish")


# ═══════════════════════════════════════════════════════════════════════════
# Opening session — 09:15 to 10:00
# ═══════════════════════════════════════════════════════════════════════════

OPENING_BUCKETS = [
    ("09:15-09:20", (9, 15), (9, 20), "algo rebalancing, SL hunting, retail "
                                      "market orders — noise, not information"),
    ("09:20-09:25", (9, 20), (9, 25), "first genuinely fresh OI snapshot lands"),
    ("09:25-09:30", (9, 25), (9, 30), "opening range completing"),
    ("09:30-09:45", (9, 30), (9, 45), "confirmation window — range break plus "
                                      "OI agreement"),
    ("09:45-10:00", (9, 45), (10, 0), "FII commit window closing; direction "
                                      "usually set by now"),
]


@dataclass
class Bucket:
    label: str
    note: str
    bars: int = 0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    complete: bool = False

    @property
    def range(self) -> float | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    @property
    def direction(self) -> str:
        if self.open is None or self.close is None:
            return "—"
        if self.close > self.open:
            return "UP"
        if self.close < self.open:
            return "DOWN"
        return "FLAT"

    @property
    def move(self) -> float | None:
        if self.open is None or self.close is None:
            return None
        return self.close - self.open


@dataclass
class OpeningRange:
    minutes: int
    high: float | None = None
    low: float | None = None
    complete: bool = False
    bars_used: int = 0

    # what price has done since the range closed
    broke: str = "INSIDE"          # ABOVE | BELOW | INSIDE
    accepted: bool = False
    rejected: bool = False
    follow_through: bool = False
    closes_beyond: int = 0

    @property
    def width(self) -> float | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    @property
    def thin(self) -> bool:
        """
        A range built from one or two bars is not a range.

        On a 5-minute chart the "5-minute opening range" is a SINGLE candle,
        so its high and low are just that bar's extremes. Price clears it
        almost immediately and the break means nothing. Flagged rather than
        hidden, because a 12-point range reporting ACCEPTED ABOVE looks like
        a signal and is noise. For a usable 5-minute range you need 1-minute
        candles.
        """
        return self.bars_used < 3

    @property
    def state(self) -> str:
        """
        The distinction the notes insist on: crossing is not breaking.

        A break with acceptance and follow-through is a different event from
        a break that got rejected, and both look the same at the moment the
        line is crossed. Naming them apart is the whole point.
        """
        if not self.complete:
            return "FORMING"
        if self.thin:
            return f"TOO THIN ({self.bars_used} bar) — not a usable range"
        if self.broke == "INSIDE":
            return "INSIDE RANGE"
        if self.rejected:
            return f"REJECTED FROM {self.broke}"
        if self.accepted and self.follow_through:
            return f"ACCEPTED {self.broke}"
        if self.accepted:
            return f"ACCEPTED {self.broke}, NO FOLLOW-THROUGH"
        return f"CROSSED {self.broke}, NOT ACCEPTED"


def opening_range(df: pd.DataFrame, minutes: int = 15,
                  accept_closes: int = 2) -> OpeningRange:
    """
    Opening range for the first N minutes, plus what happened afterwards.

    Both the 5-minute and 15-minute ranges are supported because they answer
    different questions: the 5-minute is a tripwire that fires early and often,
    the 15-minute is slower and holds better. Neither is right on its own,
    which is why the app shows both rather than picking.
    """
    d = df[df["closed"]] if "closed" in df else df
    if d.empty:
        return OpeningRange(minutes)

    day = d[d["timestamp"].dt.date == d["timestamp"].iloc[-1].date()]
    if day.empty:
        return OpeningRange(minutes)

    start = day["timestamp"].iloc[0].replace(hour=9, minute=15, second=0)
    cutoff = start + timedelta(minutes=minutes)
    inside = day[day["timestamp"] < cutoff]
    if inside.empty:
        return OpeningRange(minutes)

    orr = OpeningRange(
        minutes=minutes,
        high=float(inside["high"].max()),
        low=float(inside["low"].min()),
        bars_used=len(inside),
        complete=bool(day["timestamp"].iloc[-1] >= cutoff))

    if not orr.complete:
        return orr

    after = day[day["timestamp"] >= cutoff]
    if after.empty:
        return orr

    above = after[after["close"] > orr.high]
    below = after[after["close"] < orr.low]
    if len(above) and (not len(below) or
                       above.index[0] < below.index[0]):
        orr.broke = "ABOVE"
        beyond = after["close"] > orr.high
    elif len(below):
        orr.broke = "BELOW"
        beyond = after["close"] < orr.low
    else:
        return orr

    orr.closes_beyond = int(beyond.sum())
    # acceptance = consecutive closes held beyond, not merely a total count
    run = 0
    best = 0
    for x in beyond:
        run = run + 1 if x else 0
        best = max(best, run)
    orr.accepted = best >= accept_closes

    last = after.iloc[-1]
    back_inside = (orr.low <= float(last["close"]) <= orr.high)
    orr.rejected = bool(orr.closes_beyond > 0 and back_inside)

    if len(after) >= 2 and orr.accepted:
        prev, cur = float(after["close"].iloc[-2]), float(after["close"].iloc[-1])
        orr.follow_through = (cur > prev) if orr.broke == "ABOVE" else (cur < prev)
    return orr


def opening_buckets(df: pd.DataFrame, now: datetime | None = None) -> list[Bucket]:
    """
    The five windows, each summarised separately.

    Kept separate rather than averaged because they are not equivalent
    periods: 09:15-09:20 is mechanical noise and 09:30-09:45 is where the
    session's direction usually declares itself. Blending them hides exactly
    the contrast worth seeing.
    """
    now = now or datetime.now()
    d = df[df["closed"]] if "closed" in df else df
    out = []
    if d.empty:
        return [Bucket(lbl, note) for lbl, _, _, note in OPENING_BUCKETS]

    today = d[d["timestamp"].dt.date == d["timestamp"].iloc[-1].date()]
    base = (today["timestamp"].iloc[0] if len(today)
            else d["timestamp"].iloc[-1])

    for label, (h1, m1), (h2, m2), note in OPENING_BUCKETS:
        s_ = base.replace(hour=h1, minute=m1, second=0, microsecond=0)
        e_ = base.replace(hour=h2, minute=m2, second=0, microsecond=0)
        sub = today[(today["timestamp"] >= s_) & (today["timestamp"] < e_)]
        b = Bucket(label, note, bars=len(sub))
        if len(sub):
            b.open = float(sub["open"].iloc[0])
            b.high = float(sub["high"].max())
            b.low = float(sub["low"].min())
            b.close = float(sub["close"].iloc[-1])
            b.complete = bool(now.time() >= e_.time())
        out.append(b)
    return out


def opening_verdict(or5: OpeningRange, or15: OpeningRange,
                    buckets: list[Bucket], chain_levels_: dict | None = None,
                    pillars: dict | None = None) -> Verdict:
    """
    One read on the opening, from the ranges, the buckets and positioning.

    The rule the notes are emphatic about — do not blindly trade the first
    large candle — is enforced here: a big 09:15-09:20 bar becomes a
    CONTRADICTING factor, not a supporting one, because the stop it implies
    is far too wide to be worth taking.
    """
    sup, con = [], []

    first = buckets[0] if buckets else None
    if first and first.range and first.range >= 100:
        con.append(f"first candle is {first.range:,.0f} pts — too wide to "
                   f"chase; the stop it implies is unacceptable. Wait for a "
                   f"pullback to the EMA instead")
    elif first and first.range:
        sup.append(f"first candle {first.range:,.0f} pts — normal, not a "
                   f"blow-off")

    for orr, name in ((or5, "5-min"), (or15, "15-min")):
        if not orr.complete:
            con.append(f"{name} range still forming")
            continue
        if orr.thin:
            con.append(f"{name} range is only {orr.bars_used} bar wide — "
                       f"ignore it, needs 1-minute candles to be real")
            continue
        st = orr.state
        if st.startswith("ACCEPTED") and orr.follow_through:
            sup.append(f"{name} range {st.lower()} with follow-through")
        elif st.startswith("ACCEPTED"):
            con.append(f"{name} range accepted but no follow-through yet")
        elif st.startswith("REJECTED"):
            con.append(f"{name} range {st.lower()} — the break failed")
        elif st.startswith("CROSSED"):
            con.append(f"{name} range crossed but not accepted")
        else:
            con.append(f"{name} range: price still inside")

    if (or5.complete and or15.complete and not or5.thin
            and or5.broke != "INSIDE"):
        if or5.broke == or15.broke:
            sup.append(f"both ranges broke {or5.broke.lower()} — agreement "
                       f"across timeframes")
        elif or15.broke != "INSIDE":
            con.append("the 5-min and 15-min ranges broke in OPPOSITE "
                       "directions — treat as unresolved")

    if pillars:
        drive = pillars.get("drive")
        if drive == "FRESH PARTICIPATION":
            sup.append("opening flow is fresh participation")
        elif drive == "EXIT-DRIVEN":
            con.append("opening flow is exit-driven — moves off exits fade")

    if chain_levels_:
        for lv in chain_levels_.get("resistance", [])[:1]:
            if lv.state in ("WEAKENING", "UNWINDING", "MIGRATING"):
                sup.append(f"resistance {lv.strike} is {lv.state.lower()}")
            elif lv.state == "BUILDING":
                con.append(f"resistance {lv.strike} is building overhead")

    done = sum(1 for b in buckets if b.complete)
    if done < 3:
        con.append(f"only {done} of 5 opening windows complete — too early "
                   f"for a settled read")

    n_s, n_c = len(sup), len(con)
    if n_s >= 3 and n_s > n_c:
        state = "OPENING DIRECTION ESTABLISHED"
    elif n_c > n_s * 1.5:
        state = "OPENING UNRESOLVED — STAY OUT"
    else:
        state = "OPENING FORMING"
    return Verdict(state, _conf(n_s, n_c), sup, con,
                   detail=f"{done}/5 windows complete")


# ═══════════════════════════════════════════════════════════════════════════
# The decision bar — four layers, deliberately not merged
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Layer:
    name: str
    value: str
    confidence: str
    why: list[str] = field(default_factory=list)


@dataclass
class Decision:
    bias: Layer
    structure: Layer
    breakout: Layer
    actionability: Layer
    trust: str = "LIVE"
    trust_note: str = ""

    @property
    def one_line(self) -> str:
        return (f"{self.bias.value.lower()} bias, "
                f"{self.structure.value.lower()} structure, "
                f"{self.breakout.value.lower()}, "
                f"{self.actionability.value.lower()}")


def data_trust(now: datetime, nse_stamp: str = "",
               polls_held: int = 0, chain_stale: bool = False) -> tuple[str, str]:
    """
    How far can this data be trusted right now?

    LIVE            in-session, chain refreshing, enough polls to confirm
    UNCONFIRMED     live but the bias has not held for enough polls
    PRE-MARKET      before the bell — real context, not a trade input
    STALE CHAIN     09:15-09:20, when the OI column is yesterday's
    AFTER HOURS     session over

    This exists to gate ACTIONABILITY separately from bias. A pre-market
    reading can be perfectly good analysis and still be an unacceptable basis
    for an entry, and collapsing those two into one verdict is how a context
    signal gets traded.
    """
    t = now.time()
    if chain_stale:
        return "STALE CHAIN", ("09:15-09:20 — the OI column is still "
                               "yesterday's close. Price action only.")
    if t < config.MARKET_OPEN:
        return "PRE-MARKET", ("before the bell — this is overnight context, "
                              "not a live read")
    if t > config.MARKET_CLOSE:
        return "AFTER HOURS", "session closed — analysis only"
    if polls_held < config.CONFIRM_SNAPSHOTS:
        return "UNCONFIRMED", (f"bias has held for {polls_held} poll(s), "
                               f"needs {config.CONFIRM_SNAPSHOTS}")
    return "LIVE", "in session, confirmed across polls"


def decision(pressure: Verdict, trend: Verdict | None, brk: Verdict | None,
             pin: Verdict | None, rng: Verdict | None,
             trust: str, trust_note: str,
             blocks: list[str] | None = None) -> Decision:
    """
    Assemble the four layers WITHOUT letting them contaminate each other.

    The rule that makes this worth doing: a strong bias does not make a
    session actionable. Bias is a read on positioning; actionability is a
    question about whether you should have money on it right now, and the
    answer can be no while the bias is perfectly clear — stale data, an
    unresolved structure, a range, or a hard block like the midday freeze.
    Merging them into one line is how "bearish" quietly becomes "sell".
    """
    blocks = blocks or []

    # A — BIAS, from positioning only
    bias = Layer("Bias", pressure.state, pressure.confidence,
                 pressure.supporting[:3] + [f"⚠ {x}" for x in
                                            pressure.contradicting[:2]])

    # B — STRUCTURE, from price and contest state
    if pin and pin.state.startswith("PINNING"):
        s_val, s_conf, s_why = "PINNED / ATM FIGHT", pin.confidence, pin.supporting[:2]
    elif rng and rng.state == "RANGE TRAP":
        s_val, s_conf, s_why = "RANGE-BOUND", rng.confidence, rng.supporting[:2]
    elif trend:
        s_val, s_conf, s_why = trend.state, trend.confidence, trend.supporting[:2]
    else:
        s_val, s_conf, s_why = "UNRESOLVED", "WEAK", ["no structure data"]
    structure = Layer("Structure", s_val, s_conf, s_why)

    # C — BREAKOUT QUALITY, independent of whether you would trade it
    if brk is None:
        b_val, b_conf, b_why = "NO BREAKOUT", "MODERATE", ["no level under test"]
    else:
        b_val = {"REAL BREAKOUT": "REAL BREAKOUT LIKELY",
                 "POSSIBLE BREAKOUT": "BREAKOUT FORMING",
                 "UNCONFIRMED BREAKOUT": "SHIFT NOT CONFIRMED",
                 "FAILED BREAKOUT": "FAKE BREAKOUT RISK",
                 "FAKE BREAKOUT": "FAKE BREAKOUT RISK",
                 "NO BREAK": "NO BREAKOUT"}.get(brk.state, brk.state)
        b_conf = brk.confidence
        b_why = brk.supporting[:2] + [f"⚠ {x}" for x in brk.contradicting[:2]]
    breakout = Layer("Breakout quality", b_val, b_conf, b_why)

    # D — ACTIONABILITY, gated by trust and by structure, never by bias alone
    a_why = []
    if blocks:
        a_val, a_conf = "BLOCKED", "STRONG"
        a_why = blocks[:3]
    elif trust in ("PRE-MARKET", "AFTER HOURS"):
        a_val, a_conf = "CONTEXT ONLY", "STRONG"
        a_why = [trust_note]
    elif trust == "STALE CHAIN":
        a_val, a_conf = "NO-TRADE ZONE", "STRONG"
        a_why = [trust_note]
    elif trust == "UNCONFIRMED":
        a_val, a_conf = "WATCH ONLY", "MODERATE"
        a_why = [trust_note]
    elif s_val in ("PINNED / ATM FIGHT", "RANGE-BOUND"):
        a_val, a_conf = "WAIT", "MODERATE"
        a_why = [f"structure is {s_val.lower()} — most losses come from "
                 f"trading this condition"]
    elif s_val in ("TRANSITION", "UNRESOLVED", "INSUFFICIENT STRUCTURE"):
        a_val, a_conf = "WATCH ONLY", "MODERATE"
        a_why = ["structure unresolved — wait for it to declare"]
    elif b_val == "FAKE BREAKOUT RISK":
        a_val, a_conf = "WAIT", "MODERATE"
        a_why = ["breakout quality is poor — the level did not hand over"]
    elif pressure.confidence == "WEAK":
        a_val, a_conf = "WATCH ONLY", "MODERATE"
        a_why = ["bias evidence is thin — too few independent confirmations"]
    else:
        a_val, a_conf = "ACTIONABLE", pressure.confidence
        a_why = ["data live and confirmed", f"structure {s_val.lower()}",
                 f"bias {pressure.confidence.lower()}"]

    return Decision(bias, structure, breakout,
                    Layer("Actionability", a_val, a_conf, a_why),
                    trust, trust_note)


# ═══════════════════════════════════════════════════════════════════════════
# Strongest vs nearest levels, and the migration ladder
# ═══════════════════════════════════════════════════════════════════════════

def strongest_vs_nearest(chain: pd.DataFrame, spot: float,
                         levels: dict, span: int = 500) -> dict:
    """
    The biggest OI concentration is often NOT the level price meets first.

    Conflating them is a real source of confusion: a trader watching "the
    strongest resistance" 300 points away misses the smaller one 40 points
    overhead that price actually has to get through. Both are reported,
    separately labelled.
    """
    w = chain[(chain.strike >= spot - span) & (chain.strike <= spot + span)]
    out = {"strongest_res": None, "strongest_sup": None,
           "nearest_res": None, "nearest_sup": None, "same": {}}
    if w.empty:
        return out

    above, below = w[w.strike > spot], w[w.strike < spot]
    if not above.empty and "ce_oi" in above:
        out["strongest_res"] = int(above.loc[above["ce_oi"].idxmax(), "strike"])
    if not below.empty and "pe_oi" in below:
        out["strongest_sup"] = int(below.loc[below["pe_oi"].idxmax(), "strike"])

    res = levels.get("resistance", [])
    sup = levels.get("support", [])
    out["nearest_res"] = res[0].strike if res else None
    out["nearest_sup"] = sup[0].strike if sup else None
    out["same"] = {
        "resistance": out["strongest_res"] == out["nearest_res"],
        "support": out["strongest_sup"] == out["nearest_sup"]}
    return out


def migration_ladder(chain: pd.DataFrame, spot: float, side: str = "ce",
                     n: int = 5) -> list[dict]:
    """
    Where interest is moving, strike by strike.

    Reading the ladder top to bottom shows whether the battleground is
    shifting up or down — which the notes call out as essential and which no
    single-strike view can show.
    """
    from analytics import quadrant_strict
    step = config.STRIKE_STEP
    atm = int(round(spot / step) * step)
    out = []
    for i in range(-n, n + 1):
        k = atm + i * step
        row = chain[chain.strike == k]
        if row.empty:
            continue
        r = row.iloc[0]

        def v(c):
            x = r.get(c)
            return float(x) if x == x and x is not None else 0.0

        oi, chg = v(f"{side}_oi"), v(f"{side}_chg_oi")
        pct = r.get(f"{side}_pchg_oi")
        pct = float(pct) if pct == pct and pct is not None else (
            chg / (oi - chg) * 100 if oi - chg > 0 else 0.0)
        ltp_chg = r.get(f"{side}_chg")
        pillar, conf = quadrant_strict(
            float(ltp_chg) if ltp_chg == ltp_chg and ltp_chg is not None else None,
            chg, v(f"{side}_ltp"))

        if pct >= 25:
            tone = "AGGRESSIVE BUILD-UP"
        elif pct >= 10:
            tone = "building"
        elif pct <= -20:
            tone = "UNWINDING"
        elif pct <= -5:
            tone = "weakening"
        else:
            tone = "neutral"

        out.append({"strike": k, "distance": k - spot, "oi": oi,
                    "chg": chg, "pct": pct, "tone": tone,
                    "pillar": pillar, "confidence": conf,
                    "is_atm": k == atm})
    return out


def journal_entry(now: datetime, spot: float, atm: int, dec: Decision,
                  levels: dict, changed: list[dict]) -> dict:
    """One row per poll, in the shape a session review actually needs."""
    res = levels.get("resistance", [])
    sup = levels.get("support", [])
    recent = next((c for c in changed if c.get("available")), None)
    return {
        "time": now.strftime("%H:%M:%S"),
        "spot": round(spot, 2),
        "atm": atm,
        "strongest_support": sup[0].strike if sup else None,
        "support_state": sup[0].state if sup else None,
        "strongest_resistance": res[0].strike if res else None,
        "resistance_state": res[0].state if res else None,
        "bias": dec.bias.value,
        "structure": dec.structure.value,
        "breakout": dec.breakout.value,
        "actionability": dec.actionability.value,
        "trust": dec.trust,
        "changed": (f"{recent['spot']:+.0f} pts in {recent['window']}"
                    if recent else ""),
    }
