"""
strategy1.py — Price Action Strategy 1, live.

MJ's 4/8 EMA system on the 15-minute chart with Sushen's overlay, evaluated
against today's candles as they close. `backtest_strategy1.py` runs the same
family of rules over history; this one runs them now and tells you when
something has happened.

WHAT THIS DOES AND DOES NOT DO
──────────────────────────────
It watches closed 15-minute candles, detects the candlestick patterns you
care about, applies the gates, and raises alerts. It does not place orders
and it does not tell you to enter. Every alert means: look at the chart, the
condition you asked to be told about has occurred. You take the call.

That distinction is the whole design. An app that says ENTER is an app you
stop thinking in front of, and the strategy is a thinking strategy — Sushen's
own homework had two losses in three trades and was still net positive,
because the process ran, not because the signal was right.

THE FORMING-CANDLE RULE
───────────────────────
Same as price_action.py: nothing here ever reads the candle that has not
closed. A 15-minute candle at minute 11 can look like a textbook hammer and
close a bearish engulfing. `evaluate()` takes only rows where closed is True.
The forming candle is shown on the chart and excluded from every decision.

ENTRY MODEL — THE TWO-STEP
──────────────────────────
This is the part most easily got wrong, so it is spelled out.

    Step 1, ARM.     A 15m candle CLOSES beyond the 8 EMA. This candle is the
                     signal candle. Nothing has been entered.
    Step 2, TRIGGER. A LATER candle trades through that signal candle's high
                     (long) or low (short). That break is the follow-through
                     and it is the entry.

    Entry  = signal candle high (long) / low (short)
    Stop   = signal candle low  (long) / high (short)
    Trail  = after every subsequent close, stop moves to that candle's low
             (long) or high (short). Never backwards.
    Exit   = price touches the trailed stop, or a Doji closes, or the band
             is reached.

WHAT I HAD TO CHOOSE
────────────────────
The pattern geometry is exact arithmetic, but "small body" and "long wick"
are thresholds nobody stated as numbers. Every one of them is in PARAMS and
overridable from strategy1_rules.json. If a session's read flips when you
move doji_body_pct from 0.12 to 0.15, that read was about my threshold and
not about the market.

ONE AMBIGUITY I CANNOT RESOLVE FROM 15-MINUTE DATA
──────────────────────────────────────────────────
If a single candle both triggers the entry and breaks the stop, the order of
those two events inside the candle is unknowable at this resolution. Such a
trade is marked AMBIGUOUS rather than being scored as a win or a loss. On a
live session it does not matter — you are watching and you know which came
first. It matters for the day summary, so it is counted separately.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import setups as su

# ── thresholds ──────────────────────────────────────────────────────────────
PARAMS: dict = {
    # candle geometry
    "doji_body_pct":      0.28,   # body <= this fraction of range = Doji / Spinning Top
    "hammer_wick_ratio":  2.0,    # long wick >= this x body
    "hammer_opp_ratio":   0.6,    # opposite wick <= this x body
    "engulf_ratio":       1.1,    # body must be this much bigger than prior
    "star_penetration":   0.5,    # 3rd candle closes this far into the 1st

    # the numbers you gave
    "big_candle_pts":     55.0,   # >= this = large candle. SL is 40, so a
                                  # stop behind a candle this size is refused.
    "exhaust_pts":        60.0,   # move already run = rally is spent
    "fixed_sl_pts":       40.0,   # the reference stop the sizing assumes
    "cost_to_cost_pts":   20.0,   # favorable excursion >= this locks stop at entry
    "gap_pts":            30.0,   # open vs prev close = a gap worth gating on
    "small_target_pts":   20.0,   # take-what-it-gives after an exhausted move

    # bands and EMAs
    "band_slope_tol":     1.5,    # pts/candle before a band counts as sloping
    "band_touch_pts":     10.0,   # within this of UBB/LBB = at the band
    "flat_bb_width":      60.0,   # BB width below this = Stage 1, rangebound

    # session
    "no_entry_after":     "15:00",
    "max_trades_range":   3,
}

_RULES_FILE = Path(__file__).parent / "strategy1_rules.json"


def load_params(path: Path | None = None) -> dict:
    """PARAMS, with any overrides from strategy1_rules.json layered on top."""
    p = dict(PARAMS)
    f = path or _RULES_FILE
    try:
        if f.exists():
            raw = json.loads(f.read_text())
            p.update({k: v for k, v in raw.items() if not k.startswith("_")})
    except Exception:
        pass                      # a broken rules file must not stop the app
    return p


# ═══════════════════════════════════════════════════════════════════════════
# Candlestick patterns
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    """One candle's geometry, computed once so nothing recomputes it."""
    i: int
    ts: pd.Timestamp
    o: float
    h: float
    l: float
    c: float

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def rng(self) -> float:
        return self.h - self.l

    @property
    def upper(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def bull(self) -> bool:
        return self.c > self.o

    @property
    def bear(self) -> bool:
        return self.c < self.o


def _times(df: pd.DataFrame) -> pd.Series:
    """
    The candle times, from wherever this frame keeps them.

    Written once because the assumption was wrong in more than one place and
    a second copy of the fix is a second thing to forget.
    """
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(df.index, index=df.index)
    if "time" in df.columns:
        try:
            return pd.to_datetime(df["time"])
        except Exception:
            pass
    return pd.Series(pd.to_datetime(df.index), index=df.index)


def _row(df: pd.DataFrame, i: int) -> Candle:
    """
    One candle, whichever way the frame carries its time.

    This module was written expecting a "timestamp" COLUMN. Everything the
    app actually produces — price_action.build_all, resample, the frames in
    pa_frames — carries a DatetimeIndex instead, so the original raised
    KeyError on the very first candle of the very first run. Both shapes are
    accepted now rather than requiring every caller to reshape its frame,
    because the caller that forgets is the one that fails at 09:15.
    """
    r = df.iloc[i]
    ts = r["timestamp"] if "timestamp" in df.columns else df.index[i]
    return Candle(i, ts, float(r["open"]), float(r["high"]),
                  float(r["low"]), float(r["close"]))


def is_doji(k: Candle, p: dict) -> bool:
    return k.rng > 0 and k.body <= p["doji_body_pct"] * k.rng


def is_hammer(k: Candle, p: dict) -> bool:
    """Long lower wick, small body up top. Bullish rejection of lower prices."""
    if k.rng <= 0 or k.body <= 0:
        return False
    return (k.lower >= p["hammer_wick_ratio"] * k.body
            and k.upper <= p["hammer_opp_ratio"] * k.body)


def is_inverted_hammer(k: Candle, p: dict) -> bool:
    """Long upper wick, small body at the bottom. Rejection of higher prices."""
    if k.rng <= 0 or k.body <= 0:
        return False
    return (k.upper >= p["hammer_wick_ratio"] * k.body
            and k.lower <= p["hammer_opp_ratio"] * k.body)


def is_engulf(cur: Candle, prev: Candle, bullish: bool, p: dict) -> bool:
    if prev.body <= 0 or cur.body < p["engulf_ratio"] * prev.body:
        return False
    hi, lo = max(cur.o, cur.c), min(cur.o, cur.c)
    phi, plo = max(prev.o, prev.c), min(prev.o, prev.c)
    covered = hi >= phi and lo <= plo
    return covered and (cur.bull if bullish else cur.bear) and \
           (prev.bear if bullish else prev.bull)


def is_star(a: Candle, b: Candle, c: Candle, evening: bool, p: dict) -> bool:
    """
    Three-candle reversal. Big body, small-bodied pause, big opposite body
    closing back through the first candle's midpoint.
    """
    if a.body <= 0 or c.body <= 0:
        return False
    small_middle = b.body <= 0.5 * a.body
    mid = (a.o + a.c) / 2
    if evening:
        return (a.bull and small_middle and c.bear
                and c.c <= a.c - p["star_penetration"] * (a.c - mid))
    return (a.bear and small_middle and c.bull
            and c.c >= a.c + p["star_penetration"] * (mid - a.c))


PATTERN_MEANING = {
    "DOJI":            ("neutral", "Indecision. In a trade this is your exit."),
    "DOUBLE DOJI":     ("neutral", "Two in a row. Exit if positioned. Sushen: not an entry on its own."),
    "HAMMER":          ("bullish", "Lower prices rejected."),
    "DOUBLE HAMMER":   ("bullish", "Two rejections at the same area."),
    "INVERTED HAMMER": ("bearish", "Higher prices rejected."),
    "DOUBLE IH":       ("bearish", "Two rejections at the same area."),
    "BULLISH ENGULF":  ("bullish", "Valid entry when it closes above the 8 EMA."),
    "BEARISH ENGULF":  ("bearish", "Valid entry when it closes below the 8 EMA."),
    "MORNING STAR":    ("bullish", "Three-candle reversal at a low."),
    "EVENING STAR":    ("bearish", "Three-candle reversal at a high."),
    "BIG CANDLE":      ("neutral", "Move may be spent. Stop behind it is too wide."),
}


def detect(df: pd.DataFrame, p: dict | None = None) -> list[list[str]]:
    """
    Patterns per candle, positionally aligned with df.

    Location is deliberately NOT applied here — a hammer mid-range and a
    hammer at the LBB are the same geometry and different trades. `context()`
    adds the location, so the two stay separable and auditable.
    """
    p = p or load_params()
    out: list[list[str]] = [[] for _ in range(len(df))]
    if df.empty:
        return out
    ks = [_row(df, i) for i in range(len(df))]

    for i, k in enumerate(ks):
        tags = out[i]
        if is_doji(k, p):
            tags.append("DOJI")
        if is_hammer(k, p):
            tags.append("HAMMER")
        if is_inverted_hammer(k, p):
            tags.append("INVERTED HAMMER")
        if k.rng >= p["big_candle_pts"]:
            tags.append("BIG CANDLE")
        if i >= 1:
            if is_engulf(k, ks[i - 1], True, p):
                tags.append("BULLISH ENGULF")
            if is_engulf(k, ks[i - 1], False, p):
                tags.append("BEARISH ENGULF")
            if "DOJI" in tags and "DOJI" in out[i - 1]:
                tags.append("DOUBLE DOJI")
            if "HAMMER" in tags and "HAMMER" in out[i - 1]:
                tags.append("DOUBLE HAMMER")
            if "INVERTED HAMMER" in tags and "INVERTED HAMMER" in out[i - 1]:
                tags.append("DOUBLE IH")
        if i >= 2:
            if is_star(ks[i - 2], ks[i - 1], k, True, p):
                tags.append("EVENING STAR")
            if is_star(ks[i - 2], ks[i - 1], k, False, p):
                tags.append("MORNING STAR")
    return out


def context(df: pd.DataFrame, i: int, tags: list[str], p: dict) -> str:
    """Where a pattern happened, which is what decides whether it matters."""
    if not tags:
        return ""
    r = df.iloc[i]
    hi = float(df["high"].iloc[:i + 1].max())
    lo = float(df["low"].iloc[:i + 1].min())
    bits = []
    near = p["band_touch_pts"]
    # Before a few candles have printed, every candle is trivially both the
    # day high and the day low. Saying so is noise, not context.
    if i >= 2:
        if abs(float(r["high"]) - hi) <= near:
            bits.append("at day HIGH")
        if abs(float(r["low"]) - lo) <= near:
            bits.append("at day LOW")
    if "ubb" in df.columns and pd.notna(r.get("ubb")) and float(r["high"]) >= float(r["ubb"]) - near:
        bits.append("at UBB")
    if "lbb" in df.columns and pd.notna(r.get("lbb")) and float(r["low"]) <= float(r["lbb"]) + near:
        bits.append("at LBB")
    if abs(float(r["close"]) - round(float(r["close"]) / 100) * 100) <= 12:
        bits.append("at a 100s level")
    return " · ".join(bits)


# ═══════════════════════════════════════════════════════════════════════════
# Gates — the trades you are not allowed to take today
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Gates:
    gap: float = 0.0
    gap_kind: str = "FLAT"            # GAP UP | GAP DOWN | FLAT
    block_ce: bool = False
    block_pe: bool = False
    first_candle_pts: float = 0.0
    first_candle_colour: str = ""
    first_candle_big: bool = False
    reasons: list[str] = field(default_factory=list)

    def blocked(self, direction: str) -> str | None:
        if direction == "LONG" and self.block_ce:
            return f"{self.gap_kind} — no CE trades today"
        if direction == "SHORT" and self.block_pe:
            return f"{self.gap_kind} — no PE trades today"
        return None


def gates(df: pd.DataFrame, prev_close: float | None, p: dict) -> Gates:
    """
    Gap direction and the first candle. Both are decided before 9:30 and both
    constrain the entire session.
    """
    g = Gates()
    if df.empty:
        return g

    # Whether a gap forbids a direction, or merely flags it.
    #
    # BLOCK IS CORRECT, AND I HAD THIS WRONG.
    #
    # I softened this to 'warn' after reading the v4 notes, which mention
    # the gap only as a resistance zone (Part 7), and concluded a
    # whole-session directional ban was over-reach imported from the
    # separate 8-Setups system.
    #
    # The LTA transcript says otherwise, in Sushen's own words, twice: "On
    # a gap up days, don't look for calls. On a gap down days, don't look
    # for puts. If you change this attitude itself, most of your losses
    # will come down." Then: "On a gap up day, look for puts, not calls. On
    # a gap down day, look for calls."
    #
    # That is a session-long rule about which direction to hunt, not a fade
    # trade at the open — and it is offered as one of the four changes that
    # would most reduce student losses.
    #
    # The reasoning that misled me: I treated absence from the v4 PDF as
    # evidence against the rule. The PDF is a summary; the live sessions
    # are where the rules are actually set. Absence from a summary is not
    # absence from the system.
    mode = p.get("gap_mode", "warn")

    # Find today's first candle when history contains multiple sessions
    ts = _times(df)
    today_date = ts.dt.date.iloc[-1]
    today_mask = (ts.dt.date == today_date)
    first_i = int(today_mask.values.argmax()) if today_mask.any() else 0

    if prev_close and mode != "off":
        g.gap = float(df["open"].iloc[first_i]) - float(prev_close)
        if g.gap >= p["gap_pts"]:
            g.gap_kind = "GAP UP"
            g.block_ce = (mode == "block")
            g.reasons.append(
                (f"Gap up {g.gap:+.0f} pts. No CE trades — a gap-up opening "
                 f"looks for PE. Yesterday's close is now resistance overhead."
                 if mode == "block" else
                 f"Gap up {g.gap:+.0f} pts. Yesterday's close is now "
                 f"resistance overhead, so a CE has that in its way — worth "
                 f"knowing, not a reason to refuse the setup."))
        elif g.gap <= -p["gap_pts"]:
            g.gap_kind = "GAP DOWN"
            g.block_pe = (mode == "block")
            g.reasons.append(
                (f"Gap down {g.gap:+.0f} pts. No PE trades — it can jump back "
                 f"up at any time."
                 if mode == "block" else
                 f"Gap down {g.gap:+.0f} pts. A gap-down open can snap back, "
                 f"so a PE carries that risk — worth knowing, not a reason "
                 f"to refuse the setup."))

    k = _row(df, first_i)
    g.first_candle_pts = k.rng
    g.first_candle_colour = "GREEN" if k.bull else ("RED" if k.bear else "FLAT")
    g.first_candle_big = k.rng >= p["big_candle_pts"]
    g.reasons.append(
        f"First 15m candle {g.first_candle_colour}, {k.rng:.0f} pts — "
        f"reference only. Its high and low are not a trade.")
    if g.first_candle_big:
        g.reasons.append(
            f"First candle is {k.rng:.0f} pts, at or over the {p['big_candle_pts']:.0f}-pt "
            f"threshold. A stop behind it is wider than the {p['fixed_sl_pts']:.0f} pts "
            f"you size for. Wait for price to come back to the 8 EMA.")
    return g


# ═══════════════════════════════════════════════════════════════════════════
# The signal engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    direction: str                    # LONG | SHORT
    armed_i: int
    armed_ts: pd.Timestamp
    trigger: float                    # signal candle high (long) / low (short)
    stop: float                       # LIVE stop — trails, so it moves
    stop0: float = 0.0                # the original stop, kept for the journal
    entered: bool = False
    entry_i: int | None = None
    entry_ts: pd.Timestamp | None = None
    exit_i: int | None = None
    exit_ts: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    points: float | None = None
    ambiguous: bool = False
    small_target: bool = False
    smc_runner: bool = False
    smc_target: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.exit_i is not None:
            return "CLOSED"
        if self.entered:
            return "OPEN"
        return "ARMED"


@dataclass
class Alert:
    kind: str                         # ARM | TRIGGER | EXIT | PATTERN | GATE
    urgency: str                      # info | action | warn
    ts: pd.Timestamp
    text: str
    detail: str = ""


@dataclass
class Result:
    frame: pd.DataFrame
    patterns: list[list[str]]
    gates: Gates
    signals: list[Signal] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    stage: str = "UNKNOWN"
    ubb_slope: str = "UNKNOWN"
    lbb_slope: str = "UNKNOWN"
    ema_order: str = "UNKNOWN"
    notes: list[str] = field(default_factory=list)

    @property
    def open_signal(self) -> Signal | None:
        for s in self.signals:
            if s.state == "OPEN":
                return s
        return None

    @property
    def armed_signal(self) -> Signal | None:
        for s in self.signals:
            if s.state == "ARMED":
                return s
        return None


def _stage(d: pd.DataFrame, p: dict) -> tuple[str, str, str]:
    """
    Stage 2 = UBB sloping up. Stage 3 = LBB sloping down. Stage 1 = both flat.

    The LBB reading for Stage 3 is deliberate — v4 Part 2 said UBB and Part 12
    said LBB, and Part 12 is the one that was confirmed.
    """
    ubb = su.band_slope(d["ubb"], tol=p["band_slope_tol"]) if "ubb" in d else "UNKNOWN"
    lbb = su.band_slope(d["lbb"], tol=p["band_slope_tol"]) if "lbb" in d else "UNKNOWN"
    width = float(d["bb_width"].iloc[-1]) if "bb_width" in d and len(d) else np.nan
    if width == width and width < p["flat_bb_width"]:
        return "STAGE 1 — rangebound", ubb, lbb
    if ubb == "UP":
        return "STAGE 2 — uptrend", ubb, lbb
    if lbb == "DOWN":
        return "STAGE 3 — downtrend", ubb, lbb
    return "STAGE 1 — rangebound", ubb, lbb


def evaluate(df15: pd.DataFrame, prev_close: float | None = None,
             p: dict | None = None, now: datetime | None = None) -> Result:
    """
    Replay today's CLOSED 15-minute candles and report where things stand.

    Replay rather than incremental state: the same input always produces the
    same output, which means a disagreement with your chart is reproducible
    instead of depending on which polls the app happened to be awake for.
    """
    p = p or load_params()
    now = now or datetime.now()

    d = df15.copy()
    if "timestamp" not in d.columns:
        if isinstance(d.index, pd.DatetimeIndex):
            d["timestamp"] = d.index
        elif d.index.name in ("timestamp", "time"):
            d["timestamp"] = pd.to_datetime(d.index)
    if "timestamp" in d.columns and not pd.api.types.is_datetime64_any_dtype(d["timestamp"]):
        d["timestamp"] = pd.to_datetime(d["timestamp"])
    if "closed" in d.columns:
        d = d[d["closed"].fillna(True).astype(bool)].reset_index(drop=True)
    if "timestamp" in d.columns:
        d = d.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    if d.empty:
        return Result(frame=d, patterns=[], gates=Gates(),
                      notes=["No closed 15-minute candles yet."])

    d = su.prepare(d)                       # BB(20,2) + 4 EMA + 8 EMA
    pats = detect(d, p)
    g = gates(d, prev_close, p)
    res = Result(frame=d, patterns=pats, gates=g)
    res.stage, res.ubb_slope, res.lbb_slope = _stage(d, p)

    e4 = float(d["ema4"].iloc[-1])
    e8 = float(d["ema8"].iloc[-1])
    res.ema_order = "4 EMA ABOVE 8 EMA — bullish" if e4 > e8 else \
                    "4 EMA BELOW 8 EMA — bearish"

    cutoff = dtime(*map(int, p["no_entry_after"].split(":")))
    last = len(d) - 1

    for r in g.reasons:
        res.alerts.append(Alert("GATE", "info", _times(d).iloc[0], r))

    # ── walk the day ────────────────────────────────────────────────────────
    active: Signal | None = None
    rally_dir: str | None = None            # direction of the rally in progress
    traded_this_rally = False

    session_first_i = 0
    for i in range(1, len(d)):
        k = _row(d, i)
        prev = _row(d, i - 1)
        ema8 = float(d["ema8"].iloc[i])
        t = k.ts.time() if hasattr(k.ts, "time") else now.time()

        # Session boundary check
        k_date = k.ts.date() if hasattr(k.ts, "date") else None
        prev_date = prev.ts.date() if hasattr(prev.ts, "date") else None
        if k_date is not None and prev_date is not None and k_date != prev_date:
            session_first_i = i
            rally_dir, traded_this_rally = None, False
            if active and active.state == "OPEN":
                active.exit_i, active.exit_ts = i - 1, prev.ts
                active.exit_price = prev.c
                active.exit_reason = "EOD square-off"
                active.points = (prev.c - active.trigger) if active.direction == "LONG" else (active.trigger - prev.c)
                active = None

        # Track rally direction on every candle
        crossed_up = prev.c <= float(d["ema8"].iloc[i - 1]) and k.c > ema8
        crossed_dn = prev.c >= float(d["ema8"].iloc[i - 1]) and k.c < ema8
        if crossed_up and rally_dir != "LONG":
            rally_dir, traded_this_rally = "LONG", False
        elif crossed_dn and rally_dir != "SHORT":
            rally_dir, traded_this_rally = "SHORT", False

        # 1 ── manage an open position first
        if active and active.state == "OPEN":
            if active.direction == "LONG":
                if k.l <= active.stop:
                    active.exit_i, active.exit_ts = i, k.ts
                    active.exit_price, active.exit_reason = active.stop, "trailing stop"
                    active.points = active.stop - active.trigger
                    active = None
                else:
                    active.stop = max(active.stop, prev.l)
                    # Cost-to-Cost rule: once moved favorably >= cost_to_cost_pts, lock entry price
                    if (k.h - active.trigger) >= p.get("cost_to_cost_pts", 20.0):
                        active.stop = max(active.stop, active.trigger)
            else:
                if k.h >= active.stop:
                    active.exit_i, active.exit_ts = i, k.ts
                    active.exit_price, active.exit_reason = active.stop, "trailing stop"
                    active.points = active.trigger - active.stop
                    active = None
                else:
                    active.stop = min(active.stop, prev.h)
                    # Cost-to-Cost rule: once moved favorably >= cost_to_cost_pts, lock entry price
                    if (active.trigger - k.l) >= p.get("cost_to_cost_pts", 20.0):
                        active.stop = min(active.stop, active.trigger)

        # Reversal candles (Doji/Hammer/IH) and Band — both are exits, not stops
        if active and active.state == "OPEN":
            day_high = float(d["high"].iloc[:i + 1].max())
            day_low = float(d["low"].iloc[:i + 1].min())

            # SMC Runner Target Check
            if active.smc_runner and active.smc_target is not None:
                hit_target = (k.h >= active.smc_target) if active.direction == "LONG" else (k.l <= active.smc_target)
                if hit_target:
                    active.exit_i, active.exit_ts = i, k.ts
                    active.exit_price = active.smc_target
                    active.exit_reason = f"SMC Runner Target Reached ({active.direction} Liquidity Expansion)"
                    active.points = (active.smc_target - active.trigger) if active.direction == "LONG" else (active.trigger - active.smc_target)
                    active = None

        if active and active.state == "OPEN":
            revs = ["DOJI", "DOUBLE DOJI"]
            if active.direction == "SHORT":
                revs.extend(["HAMMER", "DOUBLE HAMMER"])
                near_ext = abs(k.l - day_low) <= p.get("band_touch_pts", 15.0)
                cur_pts = active.trigger - k.c
            else:
                revs.extend(["INVERTED HAMMER", "DOUBLE IH"])
                near_ext = abs(k.h - day_high) <= p.get("band_touch_pts", 15.0)
                cur_pts = k.c - active.trigger

            matched = [pat for pat in revs if pat in pats[i]]
            # Exit if reversal candle closed at Day Extreme or in profit
            if matched and (near_ext or cur_pts >= 10.0):
                is_ctc_locked = (active.stop >= active.trigger) if active.direction == "LONG" else (active.stop <= active.trigger)
                if active.smc_runner and cur_pts < 40.0 and is_ctc_locked:
                    # Runner protected at CTC: trail stop tighter behind candle extreme rather than cutting runner early
                    if active.direction == "LONG":
                        active.stop = max(active.stop, prev.l)
                    else:
                        active.stop = min(active.stop, prev.h)
                else:
                    active.exit_i, active.exit_ts = i, k.ts
                    active.exit_price = k.c
                    active.exit_reason = f"{matched[0]} at extreme / in profit — profit secured"
                    active.points = cur_pts
                    active = None
            else:
                band = "ubb" if active.direction == "LONG" else "lbb"
                if band in d.columns and pd.notna(d[band].iloc[i]):
                    lvl = float(d[band].iloc[i])
                    hit = (k.h >= lvl - p["band_touch_pts"]) if active.direction == "LONG" \
                          else (k.l <= lvl + p["band_touch_pts"])
                    if hit:
                        active.exit_i, active.exit_ts = i, k.ts
                        active.exit_price = lvl
                        active.exit_reason = f"reached {band.upper()}"
                        active.points = (lvl - active.trigger) if active.direction == "LONG" \
                                        else (active.trigger - lvl)
                        active = None

        # 2 ── trigger a previously armed signal
        armed = res.armed_signal
        if armed and not active:
            armed_date = armed.armed_ts.date() if hasattr(armed.armed_ts, "date") else None
            is_new_day = (k_date is not None and armed_date is not None and k_date != armed_date)
            is_stale = (i - armed.armed_i > 2)
            invalidated = ((armed.direction == "LONG" and k.c < ema8) or
                           (armed.direction == "SHORT" and k.c > ema8))

            hit = (k.h >= armed.trigger) if armed.direction == "LONG" \
                  else (k.l <= armed.trigger)
            if hit and not is_new_day:
                broke_stop = (k.l <= armed.stop) if armed.direction == "LONG" \
                             else (k.h >= armed.stop)
                armed.entered, armed.entry_i, armed.entry_ts = True, i, k.ts
                armed.ambiguous = broke_stop
                if broke_stop:
                    armed.notes.append(
                        "This candle broke both the trigger and the stop. Which "
                        "came first is unknowable from 15-minute data.")
                active = armed
                traded_this_rally = True

                # Option B: SMC BOS/CHoCH Runner Multiplier
                if p.get("enable_smc_runner", False):
                    try:
                        import smc as _smc_eval
                        _smc_r = _smc_eval.read(d.iloc[:i + 1], n=2)
                        _smc_st = _smc_r.get("structure")
                        _smc_pools = _smc_r.get("liquidity_pools", [])
                        if _smc_st and _smc_st.trend in ("BULLISH", "BEARISH"):
                            if (active.direction == "LONG" and _smc_st.trend == "BULLISH") or \
                               (active.direction == "SHORT" and _smc_st.trend == "BEARISH"):
                                active.smc_runner = True
                                opp_k = "EQH" if active.direction == "LONG" else "EQL"
                                cand_pools = [p for p in _smc_pools if p.kind == opp_k and not p.swept]
                                if cand_pools:
                                    active.smc_target = cand_pools[-1].level
                                else:
                                    active.smc_target = active.trigger + 75.0 if active.direction == "LONG" else active.trigger - 75.0
                                res.alerts.append(Alert("RUNNER", "info", k.ts,
                                    f"SMC Runner Multiplier Active ({_smc_st.trend} Structure). Extended target {active.smc_target:,.0f}."))
                    except Exception:
                        pass
            elif is_new_day or is_stale or invalidated:
                armed.exit_i = i
                armed.exit_ts = k.ts
                armed.exit_reason = ("Expired at session close" if is_new_day else
                                     "Invalidated — closed back across 8 EMA" if invalidated else
                                     "Expired — no trigger within 2 candles")
                armed = None

        # 3 ── arm a new signal on a close beyond the 8 EMA
        if not active and not res.armed_signal and t < cutoff:
            if crossed_up or crossed_dn:
                direction = "LONG" if crossed_up else "SHORT"
                block = g.blocked(direction)
                k_date = k.ts.date() if hasattr(k.ts, "date") else None
                trades_done = sum(1 for s in res.signals if s.entered and (k_date is None or (hasattr(s.armed_ts, "date") and s.armed_ts.date() == k_date)))
                max_day = p.get("max_trades_day", 0)
                if block:
                    res.alerts.append(Alert(
                        "GATE", "info", k.ts,
                        f"{k.ts:%H:%M} closed {'above' if crossed_up else 'below'} "
                        f"the 8 EMA — but skipped.", block))
                elif max_day and trades_done >= max_day:
                    res.alerts.append(Alert(
                        "GATE", "info", k.ts,
                        f"{k.ts:%H:%M} closed beyond the 8 EMA — but skipped.",
                        f"Daily cap reached ({trades_done}/{max_day} trades taken). Sushen: '1 or 2 trades a day is enough'."))
                elif traded_this_rally:
                    res.alerts.append(Alert(
                        "GATE", "info", k.ts,
                        f"{k.ts:%H:%M} closed beyond the 8 EMA — but skipped.",
                        "One trade per rally. This rally has already had its trade."))
                elif i == session_first_i or (g.first_candle_big and i == session_first_i + 1):
                    res.alerts.append(Alert(
                        "GATE", "info", k.ts,
                        f"{k.ts:%H:%M} skipped.",
                        "First candle was large. Wait for the pullback to the 8 EMA."))
                else:
                    s = Signal(
                        direction=direction, armed_i=i, armed_ts=k.ts,
                        trigger=k.h if crossed_up else k.l,
                        stop=k.l if crossed_up else k.h,
                        stop0=k.l if crossed_up else k.h)
                    if k.rng >= p["big_candle_pts"]:
                        s.small_target = True
                        s.notes.append(
                            f"Signal candle is {k.rng:.0f} pts. Stop would be "
                            f"{k.rng:.0f} pts against a {p['fixed_sl_pts']:.0f}-pt "
                            f"budget. Either skip this, or take "
                            f"{p['small_target_pts']:.0f} pts and get out.")
                    run = abs(k.c - float(d["open"].iloc[session_first_i]))
                    if run >= p["exhaust_pts"]:
                        s.small_target = True
                        s.notes.append(
                            f"Move has already run {run:.0f} pts from the open. "
                            f"The rally is spent — small target only.")
                    res.signals.append(s)

    # ── alerts from the last closed candle only ─────────────────────────────
    kl = _row(d, last)
    tags = pats[last]
    if tags:
        for tg in tags:
            tone, meaning = PATTERN_MEANING.get(tg, ("neutral", ""))
            where = context(d, last, [tg], p)
            urg = "action" if tg in ("DOJI", "DOUBLE DOJI", "DOUBLE IH",
                                     "DOUBLE HAMMER", "EVENING STAR",
                                     "MORNING STAR") else "info"
            res.alerts.append(Alert(
                "PATTERN", urg, kl.ts,
                f"{tg} on the {kl.ts:%H:%M} candle" + (f" — {where}" if where else ""),
                meaning))

    for s in res.signals:
        if s.armed_i == last and s.state == "ARMED":
            res.alerts.append(Alert(
                "ARM", "action", s.armed_ts,
                f"{s.armed_ts:%H:%M} closed "
                f"{'ABOVE' if s.direction == 'LONG' else 'BELOW'} the 8 EMA — "
                f"{'CE' if s.direction == 'LONG' else 'PE'} armed.",
                f"Entry on a break of {s.trigger:,.0f}. Stop {s.stop:,.0f} "
                f"({abs(s.trigger - s.stop):.0f} pts). Not entered yet — the "
                f"break is the entry."))
        if s.entry_i == last:
            res.alerts.append(Alert(
                "TRIGGER", "action", s.entry_ts,
                f"{s.entry_ts:%H:%M} broke {s.trigger:,.0f} — "
                f"{'CE' if s.direction == 'LONG' else 'PE'} triggered.",
                f"Stop {s.stop:,.0f}. Trail to each candle's "
                f"{'low' if s.direction == 'LONG' else 'high'} from here."))
        if s.exit_i == last:
            res.alerts.append(Alert(
                "EXIT", "action", s.exit_ts,
                f"{s.exit_ts:%H:%M} exit — {s.exit_reason}.",
                f"{s.points:+.0f} pts." if s.points is not None else ""))

    t_last = kl.ts.time() if hasattr(kl.ts, "time") else now.time()
    if t_last >= cutoff:
        res.notes.append(f"Past {p['no_entry_after']} — no fresh entries.")
    if res.stage.startswith("STAGE 1"):
        done = sum(1 for s in res.signals if s.entered)
        res.notes.append(
            f"Bands are flat — rangebound. Max {p['max_trades_range']} attempts; "
            f"{done} taken. 95% of losses come from trading this.")
    return res


def summary(res: Result) -> dict:
    """
    Day totals. Ambiguous trades are excluded from wins, losses AND points.

    A 15-minute candle that broke both the trigger and the stop cannot be
    scored: the data does not record which happened first, and guessing
    would quietly manufacture a result. So those trades are set aside
    entirely rather than counted either way.

    That consistency creates a reading hazard the caller must handle.
    "closed 4 · wins 2 · losses 0" is arithmetically correct and looks like
    a perfect record; the two trades that are missing were simply
    unscoreable. Any display of these numbers must show `ambiguous`
    alongside them, and `scored` below is provided so the denominator is
    never in doubt.
    """
    done = [s for s in res.signals if s.exit_i is not None and s.points is not None]
    clean = [s for s in done if not s.ambiguous]
    wins = [s for s in clean if s.points > 0]
    losses = [s for s in clean if s.points < 0]
    be = [s for s in clean if s.points == 0]
    return {
        "armed": len(res.signals),
        "taken": sum(1 for s in res.signals if s.entered),
        "closed": len(done),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(be),
        "ambiguous": sum(1 for s in done if s.ambiguous),
        "scored": len(clean),
        "points": sum(s.points for s in clean),
        "open": 1 if res.open_signal else 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Trend strength — whether an exit signal deserves to be obeyed
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Strength:
    checks: dict                      # name -> (passed, detail)
    score: int
    verdict: str                      # HOLD THROUGH | RESPECT THE EXIT
    note: str

    @property
    def hold(self) -> bool:
        return self.verdict.startswith("HOLD")


def trend_strength(df: pd.DataFrame, i: int, direction: str,
                   p: dict | None = None) -> Strength:
    """
    Should a Doji, Hammer or engulfing candle end the trade, or be ignored?

    Part 12 of the notes answers this and the answer is conditional, which is
    why the same candle means opposite things on different days: "UBB/LBB
    TRENDING STRONGLY = opposite candles are NOISE. UBB/LBB FLAT = opposite
    candles are SIGNALS."

    Four checks. The first three are the ones written down; VWAP is the
    fourth, added because on 01-Sep it was carrying real information — price
    held below it for the whole decline, so every bounce was running into it
    as resistance, and that is a reason to stay short that the three written
    checks do not capture on their own.

    EVERY available check must pass to hold. Part 12 is unambiguous: "ALL 3
    strong = ignore opposite candles; ANY 1 weak = respect them." An earlier
    version here required three of four, which sounds close and is not: it
    returned HOLD on a frame where the 4/8 gap had closed and was pointing
    the wrong way, and a closing 4/8 gap is the exact condition Rule 5 names
    as the reason to get out. Exiting is the default; holding is the
    exception, and it needs everything agreeing.

    VWAP missing does NOT count as a failed check. An unavailable input is
    not evidence against the trend, and scoring it as one would make the
    verdict quietly stricter whenever the Dhan feed hiccuped.
    """
    p = p or load_params()
    if i < 1 or i >= len(df):
        return Strength({}, 0, "RESPECT THE EXIT", "not enough candles")

    r = df.iloc[i]
    checks: dict[str, tuple[bool, str]] = {}
    long_ = direction in ("LONG", "CE")

    def val(*names):
        for n in names:
            if n in df.columns and pd.notna(r.get(n)):
                return float(r[n])
        return None

    e4, e8 = val("ema4", "EMA4"), val("ema8", "EMA8")
    close = float(r["close"])
    atr = val("atr", "ATR") or 0.0

    # 1 · 4/8 EMA separation — Rule 5
    if e4 is not None and e8 is not None:
        gap = e4 - e8
        wide = abs(gap) >= max(atr * 0.5, 8.0)
        right_way = (gap < 0) if not long_ else (gap > 0)
        ok = wide and right_way
        checks["4/8 EMA separated"] = (ok, (
            f"gap {gap:+.1f} pts, {'wide' if wide else 'narrow'}"
            + ("" if right_way else " and pointing the WRONG way for this "
                                    "trade — the gap is closing")))
    else:
        checks["4/8 EMA separated"] = (False, "EMAs unavailable")

    # 2 · band slope — Part 2
    band = "lbb" if not long_ else "ubb"
    bcol = band if band in df.columns else band.upper()
    if bcol in df.columns:
        slope = su.band_slope(df[bcol].iloc[max(0, i - 3):i + 1])
        want = "DOWN" if not long_ else "UP"
        ok = slope == want
        checks[f"{band.upper()} sloping {want.lower()}"] = (ok, f"slope {slope}")
    else:
        checks["band slope"] = (False, "bands unavailable")

    # 3 · price staying on one side of the 8 EMA
    if e8 is not None:
        look = df.iloc[max(0, i - 3):i + 1]
        e8s = look["ema8"] if "ema8" in look else look.get("EMA8")
        side = (look["close"] < e8s) if not long_ else (look["close"] > e8s)
        ok = bool(side.all())
        checks["price held one side of 8 EMA"] = (
            ok, f"{int(side.sum())} of {len(side)} recent candles")
    else:
        checks["price held one side of 8 EMA"] = (False, "8 EMA unavailable")

    # 4 · VWAP as the wall behind the move
    vw = val("vwap", "VWAP")
    if vw is None:
        checks["VWAP on your side"] = (None, (
            "VWAP unavailable — not counted either way. Fix the Dhan futures "
            "security id and this becomes a real check."))
    else:
        ok = (close < vw) if not long_ else (close > vw)
        checks["VWAP on your side"] = (ok, (
            f"close {close:,.0f} {'below' if close < vw else 'above'} VWAP "
            f"{vw:,.0f}"
            + (" — every bounce runs into it as resistance"
               if ok and not long_ else
               " — every dip finds it as support" if ok else
               " — VWAP is against this trade")))

    counted = [v[0] for v in checks.values() if v[0] is not None]
    score = sum(1 for x in counted if x)
    hold = bool(counted) and score == len(counted)

    return Strength(
        checks, score,
        "HOLD THROUGH the exit candle" if hold else "RESPECT THE EXIT",
        (f"{score} of {len(counted)} strength checks pass"
         + (f" — trend is strong enough that an opposite candle is noise. "
            f"Rule 5: do not exit early while the 4/8 gap is open."
            if hold else
            f" — at least one thing is weak, so an opposite candle is a "
            f"signal rather than a pause. Part 12: take the exit.")))
