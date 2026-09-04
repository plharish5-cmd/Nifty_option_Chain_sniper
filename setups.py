"""
The 8 Golden Setups — eligibility and skip-condition checker.

DESIGN DECISION, STATED UP FRONT

This does NOT tell you to enter. Your entries are visual and belong to you:
the 8 EMA touch, the rejection, the pattern break, read off your own chart.
Automating that would replace the skill you are building and would trust a
resampled feed over your eyes.

What it DOES is check the objective preconditions and — more importantly —
the SKIP conditions. That is where money actually leaks. "After 3 PM",
"within 40 points of R1", "15-min trend conflicts", "second opportunity in
this rally", "already took a Stage 2 stop today". Every one of those is
checkable, and every one of them is what gets rationalised away in the moment
with a live position in front of you.

So: the app holds the discipline, you hold the trigger.

DATA HONESTY — READ THIS BEFORE TRUSTING ANY OUTPUT

Candles come from NSE's price series at ~1 sample per minute:
  * 5m and 15m highs/lows aggregate 5 and 15 samples -> usable, but they are
    the extremes OF THE SAMPLES, not the true tick extremes. A wick that
    formed and reversed between samples is invisible.
  * 1m candles have open==high==low==close. NO wicks at all.

Consequences:
  * BB, EMAs, BB width, crossovers, slopes: reliable (close-based).
  * 8 EMA touch: approximate. Treat as "near" not "touched".
  * Candlestick patterns on 5m: indicative only. Verify on your Kite chart.
  * Setups #7/#8 (1-min patterns in the first 5 candles): NOT SUPPORTED.
    There is no wick data to detect a hammer on. Do those by eye, as taught.

If you later pay for Dhan's data API you get true OHLC and patterns become
trustworthy. Until then, this module deliberately refuses to claim pattern
detections it cannot support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

import config

# ── course constants ────────────────────────────────────────────────────────
BB_LENGTH = 20
BB_MULT = 2.0
EMA_FAST = 4
EMA_SLOW = 8
BB_WIDTH_MIN = 60          # points; below this = no fuel
LEVEL_PROXIMITY = 40       # points; skip if R/S is this close in the way
NO_ENTRY_AFTER = dtime(15, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Indicators the course actually uses
# ═══════════════════════════════════════════════════════════════════════════

def add_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    mid = d["close"].rolling(BB_LENGTH).mean()
    sd = d["close"].rolling(BB_LENGTH).std(ddof=0)
    d["bb_mid"] = mid
    d["ubb"] = mid + BB_MULT * sd
    d["lbb"] = mid - BB_MULT * sd
    d["bb_width"] = d["ubb"] - d["lbb"]
    return d


def add_course_emas(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d[f"ema{EMA_FAST}"] = d["close"].ewm(span=EMA_FAST, adjust=False).mean()
    d[f"ema{EMA_SLOW}"] = d["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    return d


def band_slope(series: pd.Series, candles: int = 2, tol: float = 1.5) -> str:
    """
    UP / DOWN / FLAT over the last N candles.

    FLAT matters as much as direction here: setups #1-#4 require a steep band
    and are void when it is flat, while #5/#6 require the band to BE flat.
    The tolerance is in points per candle — below it, the band is not moving
    with intent.
    """
    s = series.dropna()
    if len(s) < candles + 1:
        return "UNKNOWN"
    steps = s.diff().tail(candles)
    if (steps > tol).all():
        return "UP"
    if (steps < -tol).all():
        return "DOWN"
    return "FLAT"


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    return add_course_emas(add_bollinger(df))


# ═══════════════════════════════════════════════════════════════════════════
# Option chain -> the 5 conditions
# ═══════════════════════════════════════════════════════════════════════════

CHAIN_CONDITIONS = {
    ("G", "G", "R", "R"): ("SUPER BULLISH", +2, "TRAIL to 100-400",
                           ["#1 (trail)", "#3"], ["#4 extension"]),
    ("R", "R", "G", "G"): ("SUPER BEARISH", -2, "TRAIL to 100-400",
                           ["#2 (trail)", "#4"], ["#3 extension"]),
    ("G", "R", "R", "R"): ("BULLISH CONSOLIDATION", +1, "exit at 40",
                           ["#1 (40 only)", "#5"], ["#3", "#4", "#6"]),
    ("R", "R", "G", "R"): ("BEARISH CONSOLIDATION", -1, "exit at 40",
                           ["#2 (40 only)", "#6"], ["#3", "#4", "#5"]),
    ("R", "R", "R", "R"): ("CONSOLIDATION", 0, "exit at 40",
                           ["#1, #2 (40 only)"], ["all extensions", "all reversals"]),
}


@dataclass
class ChainCondition:
    call_itm: str
    call_otm: str
    put_itm: str
    put_otm: str
    name: str
    score: int
    trail: str
    best: list[str]
    avoid: list[str]
    exact: bool                    # False = nearest match, not one of the 5

    @property
    def pattern(self) -> str:
        return f"CallITM {self.call_itm} · CallOTM {self.call_otm} · " \
               f"PutITM {self.put_itm} · PutOTM {self.put_otm}"


def _group_colour(df: pd.DataFrame, side: str, itm: bool, spot: float,
                  span: int = 500, mode: str | None = None) -> str:
    """
    GREEN / RED for one quadrant of the chain.

    WHAT CHANGED AND WHY IT MATTERED
    ────────────────────────────────
    This originally summed CHANGE IN OI and called a positive sum green. It
    was wrong, and wrong in a way that hid itself: writers add OTM options on
    both sides of spot on almost every session, so the four quadrants come
    out (R, G, R, G) whether the market rises, falls or does nothing at all.
    The reading was direction-blind by construction. It was also not one of
    the five named conditions, so #15 scored +0 every single day — the exact
    symptom you saw.

    The colours are change in PREMIUM (NSE's CHNG column), not change in OI.
    Two things make that reading the coherent one:

      A rising market lifts call premiums and crushes put premiums, giving
      (G, G, R, R) — which is precisely how the course defines SUPER
      BULLISH. The OI reading has no such correspondence.

      All four quadrants red means every option lost value, which is theta
      decay with no directional move: CONSOLIDATION, exactly as the course
      has it. Under the OI reading, all-four-red would mean every quadrant
      unwinding at once, which is rare and means something else entirely.

    STILL WORTH CONFIRMING WITH MJ. Set CHAIN_COLOUR_MODE = "oi" in config
    to restore the old behaviour, and the app shows both readings side by
    side so you can check them against the screen he actually uses.
    """
    mode = mode or getattr(config, "CHAIN_COLOUR_MODE", "ltp")
    if side == "ce":
        mask = (df.strike < spot) if itm else (df.strike > spot)
    else:
        mask = (df.strike > spot) if itm else (df.strike < spot)
    mask &= (df.strike >= spot - span) & (df.strike <= spot + span)
    if not mask.any():
        return "R"

    col = f"{side}_chg" if mode == "ltp" else f"{side}_chg_oi"
    if col not in df.columns:
        return "R"
    net = float(np.nansum(df.loc[mask, col]))
    return "G" if net > 0 else "R"


def chain_condition(df: pd.DataFrame, spot: float,
                    mode: str | None = None) -> ChainCondition:
    """Map the live chain onto the course's five named conditions."""
    ci = _group_colour(df, "ce", True, spot, mode=mode)
    co = _group_colour(df, "ce", False, spot, mode=mode)
    pi = _group_colour(df, "pe", True, spot, mode=mode)
    po = _group_colour(df, "pe", False, spot, mode=mode)
    key = (ci, co, pi, po)

    if key in CHAIN_CONDITIONS:
        name, score, trail, best, avoid = CHAIN_CONDITIONS[key]
        return ChainCondition(ci, co, pi, po, name, score, trail, best, avoid, True)

    # Not one of the five. Say so rather than forcing a fit — an unmapped
    # combination is real information, and silently rounding it to the
    # nearest named condition would be a lie of convenience.
    bull = (ci == "G") + (co == "G") + (pi == "R") + (po == "R")
    lean = "leans bullish" if bull >= 3 else ("leans bearish" if bull <= 1 else "mixed")
    return ChainCondition(
        ci, co, pi, po,
        f"UNMAPPED ({lean})", 0, "no guidance",
        [], ["extensions", "reversals"], False)


# ═══════════════════════════════════════════════════════════════════════════
# Setup eligibility
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SetupCheck:
    number: int
    name: str
    direction: str                 # LONG | SHORT
    win_rate: str
    target_stop: str
    indication: list[tuple[str, bool | None]] = field(default_factory=list)
    confirmation: list[tuple[str, bool | None]] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    supported: bool = True

    @property
    def i_ok(self) -> bool:
        return all(v for _, v in self.indication if v is not None)

    @property
    def c_ok(self) -> bool:
        return all(v for _, v in self.confirmation if v is not None)

    @property
    def stage(self) -> str:
        if self.skips:
            return "BLOCKED"
        if not self.supported:
            return "MANUAL ONLY"
        if self.i_ok and self.c_ok:
            return "I+C READY — watch for your entry trigger"
        if self.i_ok:
            return "INDICATION only — not a trade"
        return "NOT SET UP"


def _near_level(spot: float, levels: list[float], above: bool,
                within: float = LEVEL_PROXIMITY) -> float | None:
    """Nearest level in the way, if it is close enough to matter."""
    pool = [l for l in levels if (l > spot if above else l < spot)]
    if not pool:
        return None
    nearest = min(pool, key=lambda l: abs(l - spot))
    return nearest if abs(nearest - spot) <= within else None


def check_setups(f5: pd.DataFrame, f15: pd.DataFrame, spot: float,
                 now: datetime,
                 resistances: list[float] | None = None,
                 supports: list[float] | None = None,
                 chain: ChainCondition | None = None,
                 stage2_stopped: bool = False,
                 stage3_stopped: bool = False,
                 trades_this_rally: int = 0) -> list[SetupCheck]:
    """
    Evaluate all 8 setups against the current state.

    f5 / f15 must already be through prepare(). Only CLOSED candles are used —
    a forming candle can still change its mind, and the course is explicit
    that you wait.
    """
    resistances = resistances or []
    supports = supports or []
    out: list[SetupCheck] = []

    c5 = f5[f5["closed"]] if "closed" in f5 else f5
    c15 = f15[f15["closed"]] if "closed" in f15 else f15
    if c5.empty or len(c5) < BB_LENGTH + 3:
        return out

    last = c5.iloc[-1]
    e4, e8 = last[f"ema{EMA_FAST}"], last[f"ema{EMA_SLOW}"]
    width = last["bb_width"]
    ubb_slope = band_slope(c5["ubb"])
    lbb_slope = band_slope(c5["lbb"])
    close = float(last["close"])

    # 15-min major trend — the course calls this the single biggest edge
    trend15 = "UNKNOWN"
    if not c15.empty and len(c15) >= EMA_SLOW:
        l15 = c15.iloc[-1]
        if l15["close"] > l15[f"ema{EMA_SLOW}"]:
            trend15 = "UPTREND"
        elif l15["close"] < l15[f"ema{EMA_SLOW}"]:
            trend15 = "DOWNTREND"
        else:
            trend15 = "CONSOLIDATION"

    # crossover recency, for #5/#6 (max 3 candles) and trend age
    cross_age = None
    diff = (c5[f"ema{EMA_FAST}"] - c5[f"ema{EMA_SLOW}"]).dropna()
    if len(diff) > 2:
        sign = np.sign(diff.values)
        flips = np.where(sign[1:] != sign[:-1])[0]
        if len(flips):
            cross_age = len(sign) - 1 - flips[-1]

    above_r = _near_level(spot, resistances, above=True)
    below_s = _near_level(spot, supports, above=False)
    after3 = now.time() >= NO_ENTRY_AFTER

    def universal(long: bool, setup_no: int) -> list[str]:
        s: list[str] = []
        if after3:
            s.append("After 3:00 PM — no new entries")
        if width < BB_WIDTH_MIN:
            s.append(f"BB width {width:.0f} < {BB_WIDTH_MIN} — no fuel")
        if trend15 == "CONSOLIDATION":
            s.append("15-min shows consolidation — skip the day")
        if long and trend15 == "DOWNTREND":
            s.append("15-min trend is DOWN — longs blocked")
        if not long and trend15 == "UPTREND":
            s.append("15-min trend is UP — shorts blocked")
        if long and above_r:
            s.append(f"Resistance at {above_r:,.0f} is {above_r - spot:.0f} pts "
                     f"overhead (< {LEVEL_PROXIMITY})")
        if not long and below_s:
            s.append(f"Support at {below_s:,.0f} is {spot - below_s:.0f} pts "
                     f"below (< {LEVEL_PROXIMITY})")
        if trades_this_rally >= 1 and setup_no in (1, 2, 3, 4):
            s.append(f"Already {trades_this_rally} trade(s) in this rally — "
                     f"2nd is 50/50, a net loss after brokerage")
        if chain and not chain.exact and setup_no in (3, 4):
            s.append("Chain not in a named condition — extensions need "
                     "Super Bullish/Bearish")
        return s

    # ── #1 Stage 2 8 EMA (LONG) ─────────────────────────────────────────────
    s1 = SetupCheck(1, "Stage 2 · 8 EMA touch", "LONG", "80%", "+40 / −40")
    s1.indication = [("UBB pointing steeply UP", ubb_slope == "UP"),
                     ("Candles closing ABOVE 8 EMA", close > e8)]
    s1.confirmation = [("4 EMA above 8 EMA", e4 > e8),
                       ("UBB up ≥ 2 candles", ubb_slope == "UP"),
                       (f"BB width > {BB_WIDTH_MIN}", width > BB_WIDTH_MIN)]
    s1.skips = universal(True, 1)
    if stage2_stopped:
        s1.skips.append("Stage 2 stop already taken today — done with this setup")
    if chain and "#1" not in " ".join(chain.best) and chain.exact:
        s1.skips.append(f"Chain is {chain.name} — #1 not among its best setups")
    s1.manual = ["ENTRY is yours: price TOUCHES the 8 EMA "
                 f"({e8:,.0f}). No candle-close wait.",
                 "Second touch in the same rally = skip."]
    out.append(s1)

    # ── #2 Stage 3 8 EMA (SHORT) ────────────────────────────────────────────
    s2 = SetupCheck(2, "Stage 3 · 8 EMA rejection", "SHORT", "80%", "+40 / −40")
    s2.indication = [("LBB pointing steeply DOWN", lbb_slope == "DOWN"),
                     ("Candles closing BELOW 8 EMA", close < e8)]
    s2.confirmation = [("4 EMA below 8 EMA", e4 < e8),
                       ("LBB down ≥ 2 candles", lbb_slope == "DOWN"),
                       (f"BB width > {BB_WIDTH_MIN}", width > BB_WIDTH_MIN)]
    s2.skips = universal(False, 2)
    if stage3_stopped:
        s2.skips.append("Stage 3 stop already taken today — done with this setup")
    if chain and "#2" not in " ".join(chain.best) and chain.exact:
        s2.skips.append(f"Chain is {chain.name} — #2 not among its best setups")
    s2.manual = [f"ENTRY is yours: price hits RESISTANCE at the 8 EMA ({e8:,.0f}).",
                 "Second rejection in the same down-move = skip."]
    out.append(s2)

    # ── #3 / #4 Extensions ──────────────────────────────────────────────────
    for no, nm, dirn, slope_ok, side_ok, pats in (
        (3, "Stage 2 Extension", "LONG", ubb_slope == "UP", close > e8,
         "HAMMER / DOJI / BULLISH ENGULF"),
        (4, "Stage 3 Extension", "SHORT", lbb_slope == "DOWN", close < e8,
         "INV HAMMER / DOJI / BEARISH ENGULF"),
    ):
        s = SetupCheck(no, nm, dirn, "60%", "+40 / −40")
        long = dirn == "LONG"
        s.indication = [(f"{'UBB' if long else 'LBB'} pointing "
                         f"{'UP' if long else 'DOWN'}", slope_ok),
                        (f"Candles {'above' if long else 'below'} 8 EMA", side_ok)]
        s.confirmation = [(f"Pattern: {pats}", None)]   # None = your eyes
        s.skips = universal(long, no)
        s.manual = [f"CONFIRMATION is yours: only {pats}. A regular candle is invalid.",
                    "Breakout of the pattern high/low must come within 2 candles, "
                    "or cancel.",
                    "Skip near the Day High (#3) / Day Low (#4) — that is a wall.",
                    "Skip entirely on gap days over 60 pts — MJ: 8 of 10 were stops."]
        s.supported = False    # pattern detection needs true wicks
        out.append(s)

    # ── #5 / #6 Reversals ───────────────────────────────────────────────────
    for no, nm, dirn, crossed, band, flat in (
        (5, "8 EMA Reversal Up", "LONG", e4 > e8, "UBB", ubb_slope == "FLAT"),
        (6, "8 EMA Reversal Down", "SHORT", e4 < e8, "LBB", lbb_slope == "FLAT"),
    ):
        s = SetupCheck(no, nm, dirn, "60-70%", "+20 / −20")
        long = dirn == "LONG"
        recent = cross_age is not None and cross_age <= 3
        s.indication = [(f"4 EMA crossed {'ABOVE' if long else 'BELOW'} 8 EMA", crossed),
                        (f"Crossover within 3 candles"
                         + (f" (age {cross_age})" if cross_age is not None else ""),
                         recent)]
        s.confirmation = [(f"{band} is FLAT (not steep yet)", flat),
                          (f"BB width > {BB_WIDTH_MIN}", width > BB_WIDTH_MIN),
                          (f"Visible gap between 4 and 8 EMA "
                           f"({abs(e4 - e8):.0f} pts)", abs(e4 - e8) > 2)]
        s.skips = universal(long, no)
        s.manual = [f"ENTRY is yours: within max 3 candles price touches the "
                    f"8 EMA ({e8:,.0f}).",
                    "At +20 move the stop to cost-to-cost.",
                    f"If the {band} is still flat, extend the target to the band.",
                    "This does NOT consume your first-opportunity ticket."]
        out.append(s)

    # ── #7 / #8 Gap setups — deliberately not automated ─────────────────────
    for no, nm, dirn in ((7, "Gap Up Setup", "SHORT"), (8, "Gap Down Setup", "LONG")):
        s = SetupCheck(no, nm, dirn, "varies", "8 EMA / candle-based")
        s.supported = False
        s.manual = [
            "NOT AUTOMATED, and it should not be: this needs candlestick "
            "patterns on the 1-MINUTE chart in the 9:15-9:20 window. The NSE "
            "feed samples about once a minute, so 1-min candles here have no "
            "wicks at all — a hammer is undetectable. Do this by eye on Kite.",
            "Common gaps over 60 pts only. Breakaway/runaway = trade WITH the gap.",
            "Stop = entry candle high/low, minimum 20. No cost-to-cost.",
            "Option chain does not apply to gap setups.",
        ]
        out.append(s)

    return out


def context_summary(f5: pd.DataFrame, f15: pd.DataFrame, now: datetime) -> dict:
    """The handful of numbers the course wants visible at all times."""
    c5 = f5[f5["closed"]] if "closed" in f5 else f5
    c15 = f15[f15["closed"]] if "closed" in f15 else f15
    if c5.empty:
        return {}
    last = c5.iloc[-1]
    trend15 = "UNKNOWN"
    if not c15.empty:
        l15 = c15.iloc[-1]
        trend15 = ("UPTREND" if l15["close"] > l15[f"ema{EMA_SLOW}"]
                   else "DOWNTREND" if l15["close"] < l15[f"ema{EMA_SLOW}"]
                   else "CONSOLIDATION")
    e4, e8 = last[f"ema{EMA_FAST}"], last[f"ema{EMA_SLOW}"]
    return {
        "close": float(last["close"]),
        "ema4": float(e4), "ema8": float(e8),
        "ubb": float(last["ubb"]), "lbb": float(last["lbb"]),
        "bb_width": float(last["bb_width"]),
        "bb_width_ok": bool(last["bb_width"] > BB_WIDTH_MIN),
        "ubb_slope": band_slope(c5["ubb"]),
        "lbb_slope": band_slope(c5["lbb"]),
        "party": "UPPER BB only" if e4 > e8 else "LOWER BB only",
        "trend15": trend15,
        "after_3pm": now.time() >= NO_ENTRY_AFTER,
        "dist_to_ema8": float(last["close"] - e8),
    }
