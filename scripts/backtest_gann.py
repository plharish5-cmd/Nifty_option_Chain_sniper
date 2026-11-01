#!/usr/bin/env python3
"""backtest_gann.py — score Gann Square-of-9 levels and time cycles against a random baseline.

WHY
The three Booksyard titles prescribe exactly one test, and both books repeat it: a level or date
list is only interesting if it beats dates/levels you did NOT choose. That comparison is the part
everyone skips, and it is the only part that matters. This runs it.

  "With 14 dates, a swing of two hits changes the rate by 14 points. A result inside the random
   spread tells you nothing at all. That is the most common honest outcome."

WHAT IT MEASURES

  --mode levels   (default, intraday)
      For each stored session: build Sq9 levels from a pivot that existed BEFORE the session
      (previous day's close, high or low), find that session's intraday swing pivots, and ask how
      many landed within --tol points of a Gann level.
      Baseline: the same NUMBER of levels, drawn at random across the same price span, scored
      identically, --runs times. Density is held equal, which is what makes the comparison fair.

  --mode cycles   (daily / positional)
      From the biggest high and low in the lead-in window, project the configured day-counts
      forward. Ask how many landed within --day-tol days of a daily turning point.
      Baseline: the same number of dates drawn at random across the test period.

HONESTY RULES BAKED IN, because the books name these as the usual ways people fool themselves:
  - tolerance is a command-line argument, fixed before the run, and printed in the header
  - the pivot is always one that existed before the day being scored (no hindsight pivots)
  - the random baseline uses the same count and the same span
  - every source is scored separately, so one good source cannot hide behind the others
  - nothing is dropped for "there was news that day"

READ-ONLY. Opens the database, computes, prints. Writes nothing.

USAGE
    python3 scripts/backtest_gann.py                      # intraday levels, 3m pivots, ±10 pts
    python3 scripts/backtest_gann.py --tol 15 --runs 20
    python3 scripts/backtest_gann.py --mode cycles --day-tol 3
    python3 scripts/backtest_gann.py --pivot pdh          # score levels off previous-day HIGH
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ict_smc_app" / "backend"))

IST = timezone(timedelta(hours=5, minutes=30))
DB = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
NIFTY_SEC = "13"

# The desk's own defaults, so the test scores what the chart actually draws.
DEGREES = [45, 90, 135, 180]
CYCLE_DAYS = [30, 45, 60, 90, 120, 135, 144, 180, 225, 270, 315, 360]


def gann_sq9(price: float, degrees: float) -> float:
    """(sqrt(price) + degrees/180)^2 — identical to engine/gann_pivots_gaps.py:57."""
    return round((math.sqrt(price) + (degrees / 180.0)) ** 2, 2)


def sq9_ladder(pivot: float) -> list[float]:
    """The eight levels the desk draws: ±45, ±90, ±135, ±180 from one pivot."""
    out = []
    for d in DEGREES:
        out.append(gann_sq9(pivot, d))
        out.append(gann_sq9(pivot, -d))
    return sorted(out)


# ── bars ──────────────────────────────────────────────────────────────────────────────────────────

def aggregate(m1: list[dict], step: int) -> list[dict]:
    """1-minute rows into `step`-second bars. The forming bucket is dropped."""
    buckets: dict[int, dict] = {}
    for r in m1:
        k = (int(r["ts"]) // step) * step
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"ts": k, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            b["h"] = max(b["h"], r["h"])
            b["l"] = min(b["l"], r["l"])
            b["c"] = r["c"]
    return [buckets[k] for k in sorted(buckets)]


def swing_pivots(bars: list[dict], window: int) -> list[tuple[int, float, str]]:
    """Turning points, defined BEFORE looking at any level — the books' step 1.

    A pivot high is a bar whose high is the highest of the `window` bars either side. Strict on the
    left and inclusive on the right so a plateau yields one pivot, not several.
    """
    out = []
    n = len(bars)
    for i in range(window, n - window):
        hi, lo = bars[i]["h"], bars[i]["l"]
        left = bars[i - window:i]
        right = bars[i + 1:i + window + 1]
        if all(b["h"] < hi for b in left) and all(b["h"] <= hi for b in right):
            out.append((i, hi, "high"))
        if all(b["l"] > lo for b in left) and all(b["l"] >= lo for b in right):
            out.append((i, lo, "low"))
    return out


def near_any(price: float, levels: list[float], tol: float) -> bool:
    return any(abs(price - lv) <= tol for lv in levels)


# ── mode: intraday levels ─────────────────────────────────────────────────────────────────────────

def run_levels(conn, a) -> int:
    rows = conn.execute(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id=? ORDER BY ts", (a.sec,)).fetchall()
    if not rows:
        print(f"no candles for sec_id {a.sec} in {a.db}", file=sys.stderr)
        return 2

    by_day: dict[str, list[dict]] = defaultdict(list)
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({"ts": int(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})

    days = sorted(by_day)
    step = a.tf * 60
    rng = random.Random(a.seed)

    tested = 0
    tot_pivots = 0
    gann_hits = 0
    in_range_total = 0
    random_hits = [0] * a.runs
    per_day = []
    span_sum = 0.0

    for i in range(1, len(days)):          # from the 2nd day on: day 1 has no previous session
        d, prev = days[i], days[i - 1]
        bars = aggregate(by_day[d], step)
        if len(bars) < 2 * a.window + 6:
            continue
        pv = by_day[prev]
        prev_close = pv[-1]["c"]
        prev_high = max(b["h"] for b in pv)
        prev_low = min(b["l"] for b in pv)
        pivot = {"pdc": prev_close, "pdh": prev_high, "pdl": prev_low}[a.pivot]

        levels = sq9_ladder(pivot)
        pivots = swing_pivots(bars, a.window)
        if not pivots:
            continue

        lo = min(b["l"] for b in bars)
        hi = max(b["h"] for b in bars)
        span = max(1.0, hi - lo)

        # DENSITY MUST MATCH, or the comparison is worthless. The Sq9 ladder spans roughly ±300
        # points around the pivot, so on a 200-point day most of it sits where price never traded.
        # Scoring all eight against eight random levels drawn INSIDE the day's range gives random
        # far more usable levels and guarantees it wins — a fairness bug, not a finding. So only
        # the levels price could actually have reached are scored, and the baseline draws exactly
        # that many. How often the ladder lands in range is itself reported below.
        in_range = [lv for lv in levels if lo - a.tol <= lv <= hi + a.tol]
        if not in_range:
            continue

        span_sum += span
        g = sum(1 for _, px, _ in pivots if near_any(px, in_range, a.tol))

        for r in range(a.runs):
            rl = [rng.uniform(lo, hi) for _ in range(len(in_range))]
            random_hits[r] += sum(1 for _, px, _ in pivots if near_any(px, rl, a.tol))

        tested += 1
        tot_pivots += len(pivots)
        gann_hits += g
        in_range_total += len(in_range)
        per_day.append((d, len(pivots), g, len(in_range), round(span)))

    if not tested:
        print("not enough sessions with pivots to score", file=sys.stderr)
        return 2

    print(f"\nGann Sq9 levels vs intraday turning points")
    print(f"database  : {a.db}")
    print(f"pivot     : previous session's {a.pivot.upper()}  ·  angles ±{', ±'.join(map(str, DEGREES))}")
    print(f"bars      : {a.tf}m   pivot window: ±{a.window} bars")
    print(f"tolerance : ±{a.tol} points   (fixed before the run)")
    print(f"sessions  : {tested}   turning points: {tot_pivots}   mean session range: {span_sum/tested:.0f} pts")
    print(f"levels in range: {in_range_total/tested:.1f} of 8 per session "
          f"(only these are scored; the baseline draws the same number)\n")

    print(f"{'day':<12}{'pivots':>8}{'on a level':>13}{'levels in range':>17}{'range':>8}")
    for d, np_, g, nir, sp in per_day:
        print(f"{d:<12}{np_:>8}{g:>13}{nir:>17}{sp:>8}")

    g_rate = gann_hits / tot_pivots * 100
    r_rates = [h / tot_pivots * 100 for h in random_hits]
    r_mean = statistics.mean(r_rates)
    r_sd = statistics.pstdev(r_rates) if len(r_rates) > 1 else 0.0

    print(f"\n{'':<24}{'hits':>8}{'rate':>9}")
    print(f"{'Gann Sq9 ladder':<24}{gann_hits:>8}{g_rate:>8.1f}%")
    print(f"{'random, same count':<24}{statistics.mean(random_hits):>8.1f}{r_mean:>8.1f}%"
          f"   (sd {r_sd:.1f}, {a.runs} runs, best {max(r_rates):.1f}%, worst {min(r_rates):.1f}%)")

    edge = g_rate - r_mean
    z = (edge / r_sd) if r_sd > 0 else float("nan")
    print(f"\nedge over random : {edge:+.1f} percentage points"
          + (f"   ({z:+.1f} sd)" if r_sd > 0 else ""))
    beat = sum(1 for r in r_rates if r >= g_rate)
    print(f"random runs matching or beating Gann: {beat} of {a.runs}")

    print()
    if beat >= max(1, a.runs // 5) or edge <= 0:
        print("READ: the Gann ladder sits inside the random spread. On this data it is not")
        print("      locating turning points better than arbitrary levels at the same density.")
    elif r_sd > 0 and z < 2:
        print("READ: Gann is ahead of random but by less than 2 sd. Suggestive, not established —")
        print("      the books' bar is a CLEAR and REPEATED difference, on more than one instrument.")
    else:
        print("READ: Gann clearly beats the random baseline here. Before trusting it, repeat on")
        print("      BANKNIFTY and on a different tolerance; one instrument is not a result.")
    print("\nA level says LOOK; price says ACT. Nothing here produces a direction.")
    return 0


# ── mode: daily time cycles ───────────────────────────────────────────────────────────────────────

def run_cycles(conn, a) -> int:
    rows = conn.execute(
        "SELECT d,o,h,l,c FROM daily WHERE sec_id=? ORDER BY d", (a.sec,)).fetchall()
    bars = [{"d": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
            for r in rows]
    if len(bars) < 120:
        print(f"only {len(bars)} daily bars for sec_id {a.sec}; need 120+ for a cycle test.",
              file=sys.stderr)
        print("The desk stores daily bars in the `daily` table — backfill it before running this.",
              file=sys.stderr)
        return 2

    rng = random.Random(a.seed)
    lead = a.lead                                  # bars used to find the source high/low
    src, test = bars[:lead], bars[lead:]
    if len(test) < 60:
        print("lead-in window leaves too little to test; lower --lead.", file=sys.stderr)
        return 2

    hi_i = max(range(len(src)), key=lambda i: src[i]["h"])
    lo_i = min(range(len(src)), key=lambda i: src[i]["l"])
    hi_d = datetime.fromisoformat(src[hi_i]["d"]).date()
    lo_d = datetime.fromisoformat(src[lo_i]["d"]).date()

    # turning points in the test window, defined before any date is generated
    turns = set()
    w = a.window
    for i in range(w, len(test) - w):
        hi, lo = test[i]["h"], test[i]["l"]
        if all(test[j]["h"] < hi for j in range(i - w, i)) and \
           all(test[j]["h"] <= hi for j in range(i + 1, i + w + 1)):
            turns.add(test[i]["d"])
        if all(test[j]["l"] > lo for j in range(i - w, i)) and \
           all(test[j]["l"] >= lo for j in range(i + 1, i + w + 1)):
            turns.add(test[i]["d"])
    turn_dates = sorted(datetime.fromisoformat(d).date() for d in turns)
    if not turn_dates:
        print("no turning points found in the test window", file=sys.stderr)
        return 2

    first = datetime.fromisoformat(test[0]["d"]).date()
    last = datetime.fromisoformat(test[-1]["d"]).date()

    def hits(dates):
        return sum(1 for d in dates
                   if any(abs((d - t).days) <= a.day_tol for t in turn_dates))

    per_source = []
    all_dates = []
    for name, anchor in (("major high", hi_d), ("major low", lo_d)):
        ds = [anchor + timedelta(days=n) for n in CYCLE_DAYS]
        ds = [d for d in ds if first <= d <= last]
        if ds:
            per_source.append((name, len(ds), hits(ds)))
            all_dates += ds

    if not all_dates:
        print("no projected dates land inside the test window; widen --lead or the data range.",
              file=sys.stderr)
        return 2

    n = len(all_dates)
    g_hits = hits(all_dates)
    span = (last - first).days
    r_hits = []
    for _ in range(a.runs):
        rd = [first + timedelta(days=rng.randint(0, span)) for _ in range(n)]
        r_hits.append(hits(rd))

    print(f"\nGann time cycles vs daily turning points")
    print(f"database   : {a.db}")
    print(f"daily bars : {len(bars)}   lead-in {lead}   test window {first} → {last}")
    print(f"anchors    : major high {hi_d}, major low {lo_d}  (both inside the lead-in only)")
    print(f"day counts : {', '.join(map(str, CYCLE_DAYS))}")
    print(f"tolerance  : ±{a.day_tol} days   (fixed before the run)")
    print(f"turning points in window: {len(turn_dates)}\n")

    print(f"{'source':<16}{'dates':>7}{'hits':>7}{'rate':>9}")
    for name, nd, h in per_source:
        print(f"{name:<16}{nd:>7}{h:>7}{h/nd*100:>8.0f}%")

    g_rate = g_hits / n * 100
    r_rates = [h / n * 100 for h in r_hits]
    r_mean = statistics.mean(r_rates)
    r_sd = statistics.pstdev(r_rates) if len(r_rates) > 1 else 0.0
    print(f"\n{'all cycle dates':<16}{n:>7}{g_hits:>7}{g_rate:>8.0f}%")
    print(f"{'random dates':<16}{n:>7}{statistics.mean(r_hits):>7.1f}{r_mean:>8.0f}%"
          f"   (sd {r_sd:.1f}, {a.runs} runs)")

    beat = sum(1 for r in r_rates if r >= g_rate)
    print(f"\nedge over random : {g_rate - r_mean:+.1f} percentage points")
    print(f"random runs matching or beating the cycle dates: {beat} of {a.runs}")
    print()
    if beat >= max(1, a.runs // 5) or g_rate <= r_mean:
        print("READ: the cycle dates sit inside the random spread — no detectable edge here.")
    else:
        print("READ: ahead of random. Repeat on another instrument and another lead-in before")
        print("      treating it as real; a single window is one sample, not a finding.")
    print("\nWhy the random rate is high: with ±{} days you cover {} days around every date, and a"
          .format(a.day_tol, a.day_tol * 2 + 1))
    print("liquid index makes swing pivots constantly. Any date list 'hits' often.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="levels", choices=["levels", "cycles"])
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--sec", default=NIFTY_SEC)
    ap.add_argument("--tf", type=int, default=3, help="intraday bar size in minutes (levels mode)")
    ap.add_argument("--window", type=int, default=5, help="bars either side that define a pivot")
    ap.add_argument("--tol", type=float, default=10.0, help="points either side of a level (levels)")
    ap.add_argument("--day-tol", type=int, default=3, help="days either side of a date (cycles)")
    ap.add_argument("--pivot", default="pdc", choices=["pdc", "pdh", "pdl"],
                    help="which previous-session price anchors the Sq9 ladder")
    ap.add_argument("--lead", type=int, default=120,
                    help="daily bars reserved for finding the anchors (cycles)")
    ap.add_argument("--runs", type=int, default=10, help="random baseline runs")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    if not Path(a.db).exists():
        print(f"database not found: {a.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(a.db)
    return run_levels(conn, a) if a.mode == "levels" else run_cycles(conn, a)


if __name__ == "__main__":
    raise SystemExit(main())
