"""
Level hypothesis backtester.

    python backtest_levels.py

Tests two specific, falsifiable claims against historical intraday data:

  HYPOTHESIS A — "PDC on gap days"
      On a gap day of roughly 60-80 points, the PREVIOUS day's final 15-minute
      candle acts as support or resistance. Price touches it and reverses,
      touches a second time and reverses (double top / double bottom), and on
      the THIRD attempt it breaks through.

  HYPOTHESIS B — "opening candle colour"
      On a gap day, the colour of the first (or second) 5-minute candle sets a
      level: a red candle acts as resistance, a green one as support.

WHY THE BASE RATE IS THE WHOLE POINT

The trap in "the third touch breaks" is that SOME touch always breaks
eventually — that is what makes it the last touch. If 60% of all touches
break regardless of number, then a third touch breaking 60% of the time tells
you precisely nothing. You would remember the pattern anyway, because the
break is the memorable part and the failures are forgettable.

So every test here reports the BASE RATE alongside the by-touch-number rate.
The hypothesis only survives if touch 3 breaks MORE often than touches 1 and
2 — by a margin larger than the sample can produce by chance.

SAMPLE SIZE, HONESTLY

Gap days of 60-80 points are perhaps 10% of sessions, and they split into up
and down. One year (~250 days) yields maybe 12 per direction. That is not
enough to establish a 10-point difference in break rate. Two to five years is
what makes this answerable, which is why the Dhan download is worth doing
properly. The script states its own sample size and refuses to sound
confident on thin data.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import config

# ── tunables ────────────────────────────────────────────────────────────────
GAP_BANDS = [(40, 60), (60, 80), (80, 120), (120, 10_000)]
TOUCH_TOLERANCE = 15      # points: within this of the level counts as a touch
BREAK_BUFFER = 15         # points beyond the level to count as a genuine break
REARM_DISTANCE = 25       # price must move this far away before a new touch counts


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════

def load_intraday(folder: Path | str, pattern: str = "*.csv") -> pd.DataFrame:
    """
    Load historical intraday candles from a folder of CSVs.

    Expects columns: timestamp, open, high, low, close (volume optional).
    Any timeframe — the resolution is inferred and reported.
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"No such folder: {folder}")
    files = sorted(folder.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No CSVs matching {pattern} in {folder}")

    frames = []
    for f in files:
        d = pd.read_csv(f)
        d.columns = [c.strip().lower() for c in d.columns]
        need = {"timestamp", "open", "high", "low", "close"}
        if not need <= set(d.columns):
            continue
        d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
        for c in ("open", "high", "low", "close"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        frames.append(d[["timestamp", "open", "high", "low", "close"]])

    if not frames:
        raise ValueError(f"No usable CSVs in {folder}")
    out = (pd.concat(frames, ignore_index=True)
             .dropna(subset=["timestamp", "close"])
             .sort_values("timestamp")
             .drop_duplicates(subset=["timestamp"])
             .reset_index(drop=True))
    out["date"] = out["timestamp"].dt.date
    return out


def resample_to(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    d = df.set_index("timestamp").sort_index()
    out = (d.resample(f"{minutes}min")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
             .dropna(subset=["close"]).reset_index())
    out["date"] = out["timestamp"].dt.date
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Touch / break sequencing
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Episode:
    date: object
    kind: str                  # resistance | support
    level: float
    gap: float
    touches: int = 0
    broke_on: int | None = None      # touch number the break happened on
    outcomes: list[str] = field(default_factory=list)   # per touch


def sequence_touches(bars: pd.DataFrame, level: float, kind: str) -> Episode:
    """
    Walk the session recording each touch and whether it REJECTED or BROKE.

    Two rules that took a failed self-test to get right:

    1. TOUCH is judged on the bar's extreme (did price reach the level), but
       BREAK is judged on the bar's CLOSE (did price accept beyond it). The
       first version tested both on the extreme, so a bar that sliced clean
       through the level counted as "touch 1" AND "break" at once. That made a
       dataset with a planted third-touch effect look identical to one with no
       effect at all — the harness could not tell truth from noise.

       Using the close matches what is actually being claimed: "it touches and
       comes back down" means it closed back on the original side.

    2. REARMING: price must move REARM_DISTANCE away before another touch
       counts, otherwise price loitering at the level registers dozens of
       touches and the count becomes meaningless.
    """
    ep = Episode(date=bars["date"].iloc[0] if len(bars) else None,
                 kind=kind, level=level, gap=np.nan)
    armed = True
    for _, b in bars.iterrows():
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])

        if kind == "resistance":
            touched = hi >= level - TOUCH_TOLERANCE
            accepted = cl >= level + BREAK_BUFFER      # CLOSE, not high
            away = hi < level - REARM_DISTANCE
        else:
            touched = lo <= level + TOUCH_TOLERANCE
            accepted = cl <= level - BREAK_BUFFER      # CLOSE, not low
            away = lo > level + REARM_DISTANCE

        if armed and touched:
            ep.touches += 1
            armed = False
            if accepted:
                ep.broke_on = ep.touches
                ep.outcomes.append("BREAK")
                break
            ep.outcomes.append("REJECT")
        elif not armed:
            if accepted:
                # broke away later without a fresh approach — still a break,
                # attributed to the touch that preceded it
                ep.broke_on = ep.touches
                ep.outcomes.append("BREAK")
                break
            if away:
                armed = True
    return ep


# ═══════════════════════════════════════════════════════════════════════════
# Hypothesis A — previous day's last 15-min candle
# ═══════════════════════════════════════════════════════════════════════════

def test_pdc(bars5: pd.DataFrame, bars15: pd.DataFrame,
             gap_band: tuple[float, float]) -> list[Episode]:
    """
    On gap days in the band, does the previous day's final 15-min candle hold?

    LEVEL DEFINITION: on a gap-DOWN day price opens below the candle, so the
    first edge it meets climbing is that candle's LOW — that is the
    resistance. On a gap-UP day the first edge it meets falling is the HIGH.
    Using the near edge rather than the close because that is the price level
    the market actually has to overcome first.
    """
    lo_gap, hi_gap = gap_band
    eps: list[Episode] = []

    days = sorted(bars5["date"].unique())
    for i in range(1, len(days)):
        today, prev = days[i], days[i - 1]
        d5 = bars5[bars5["date"] == today]
        p15 = bars15[bars15["date"] == prev]
        p5 = bars5[bars5["date"] == prev]
        if d5.empty or p15.empty or p5.empty:
            continue

        prev_close = float(p5["close"].iloc[-1])
        open_ = float(d5["open"].iloc[0])
        gap = open_ - prev_close
        if not (lo_gap <= abs(gap) < hi_gap):
            continue

        last15 = p15.iloc[-1]
        if gap < 0:                      # gap down -> PDC above -> resistance
            level, kind = float(last15["low"]), "resistance"
        else:                            # gap up -> PDC below -> support
            level, kind = float(last15["high"]), "support"

        ep = sequence_touches(d5, level, kind)
        ep.gap = gap
        ep.date = today
        if ep.touches:
            eps.append(ep)
    return eps


# ═══════════════════════════════════════════════════════════════════════════
# Hypothesis B — opening candle colour
# ═══════════════════════════════════════════════════════════════════════════

def test_opening_candle(bars5: pd.DataFrame, gap_band: tuple[float, float],
                        which: int = 1) -> list[Episode]:
    """
    Does the Nth 5-minute candle's colour define a level that holds?

    `which` is 1-based: 1 = the 09:15-09:20 candle, 2 = 09:20-09:25.

    AMBIGUITY WORTH RESOLVING: your description said "the second candle" but
    then gave the 09:15-09:20 window, which is the FIRST. Both are tested so
    the data can settle which one you meant — and whether it matters.

    A red candle is taken as resistance at its HIGH, a green one as support at
    its LOW.
    """
    lo_gap, hi_gap = gap_band
    eps: list[Episode] = []
    days = sorted(bars5["date"].unique())

    for i in range(1, len(days)):
        today, prev = days[i], days[i - 1]
        d5 = bars5[bars5["date"] == today].reset_index(drop=True)
        p5 = bars5[bars5["date"] == prev]
        if len(d5) < which + 3 or p5.empty:
            continue

        gap = float(d5["open"].iloc[0]) - float(p5["close"].iloc[-1])
        if not (lo_gap <= abs(gap) < hi_gap):
            continue

        c = d5.iloc[which - 1]
        red = float(c["close"]) < float(c["open"])
        level = float(c["high"]) if red else float(c["low"])
        kind = "resistance" if red else "support"

        ep = sequence_touches(d5.iloc[which:], level, kind)
        ep.gap = gap
        ep.date = today
        if ep.touches:
            eps.append(ep)
    return eps


# ═══════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════

def summarise(eps: list[Episode], label: str) -> None:
    if not eps:
        print(f"\n  {label}: no qualifying days found")
        return

    total_touches = sum(e.touches for e in eps)
    breaks = sum(1 for e in eps if e.broke_on)
    base = breaks / total_touches * 100 if total_touches else 0

    print(f"\n  {label}")
    print(f"  {'-' * 66}")
    print(f"    qualifying days      {len(eps)}")
    print(f"    total touches        {total_touches}")
    print(f"    days that broke      {breaks}")
    print(f"    BASE RATE            {base:.1f}% of touches break")

    # break rate conditional on reaching touch N
    print(f"\n    {'touch':<8}{'reached':>9}{'broke':>8}{'break rate':>12}")
    for n in (1, 2, 3, 4):
        reached = sum(1 for e in eps if e.touches >= n)
        broke = sum(1 for e in eps if e.broke_on == n)
        if not reached:
            continue
        rate = broke / reached * 100
        flag = ""
        if n == 3 and reached >= 10:
            r1 = sum(1 for e in eps if e.broke_on == 1) / \
                 max(sum(1 for e in eps if e.touches >= 1), 1) * 100
            if rate > r1 + 15:
                flag = "  <-- higher than touch 1"
            elif rate < r1 + 5:
                flag = "  <-- NOT higher than touch 1"
        print(f"    {n:<8}{reached:>9}{broke:>8}{rate:>11.1f}%{flag}")

    n3 = sum(1 for e in eps if e.touches >= 3)
    if n3 < 10:
        print(f"\n    ⚠ Only {n3} episodes reached a third touch. Nothing here "
              f"is conclusive.\n      Needs 30+ for a 15-point difference to "
              f"mean anything.")


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else config.HISTORY_DIR
    print("=" * 72)
    print("LEVEL HYPOTHESIS BACKTEST")
    print("=" * 72)

    try:
        raw = load_intraday(folder)
    except Exception as exc:                                # noqa: BLE001
        print(f"\nCould not load history: {exc}")
        print(f"\nPut historical intraday CSVs in {folder}")
        print("Columns: timestamp, open, high, low, close")
        print("\nGet them with:  python fetch_history.py   (needs Dhan data API)")
        return 1

    bars5 = resample_to(raw, 5)
    bars15 = resample_to(raw, 15)
    days = bars5["date"].nunique()
    print(f"\nloaded  {len(raw):,} bars across {days} sessions")
    print(f"        {raw.timestamp.min():%d-%b-%Y} to {raw.timestamp.max():%d-%b-%Y}")
    print(f"        -> {len(bars5):,} five-min bars, {len(bars15):,} fifteen-min bars")

    if days < 120:
        print(f"\n⚠ {days} sessions is thin. Gap days in any single band will be "
              f"few,\n  and nothing below ~30 episodes per band is interpretable.")

    for band in GAP_BANDS:
        print("\n" + "=" * 72)
        print(f"GAP BAND {band[0]}-{band[1] if band[1] < 9999 else '+'} POINTS")
        print("=" * 72)
        summarise(test_pdc(bars5, bars15, band),
                  "A · previous day's last 15-min candle")
        summarise(test_opening_candle(bars5, band, 1),
                  "B1 · first 5-min candle (09:15-09:20)")
        summarise(test_opening_candle(bars5, band, 2),
                  "B2 · second 5-min candle (09:20-09:25)")

    print("\n" + "=" * 72)
    print("""HOW TO READ THIS

  Compare each touch's break rate against the BASE RATE, not against zero.
  Some touch always breaks eventually — that is what makes it the last one.
  The claim "the third touch breaks" only survives if touch 3 breaks
  MEANINGFULLY more often than touches 1 and 2.

  If touch 3 sits at or below touch 1's rate, the pattern you remember is
  recall bias: the breaks are memorable and the quiet rejections are not.
  That would not mean the level is worthless — a level that rejects twice is
  still a level — only that the "third time breaks" part is not real.

  Check the different gap bands too. It is entirely possible the effect
  exists at 60-80 points and vanishes at 120+, which would itself be a
  useful and tradeable finding.""")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
