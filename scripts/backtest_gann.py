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
    python3 scripts/backtest_gann.py --mode sweep         # tolerance x pivot grid
    python3 scripts/backtest_gann.py --mode validate --pivot pdh   # split + concentration
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

def load_sessions(conn, sec):
    rows = conn.execute(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id=? ORDER BY ts", (sec,)).fetchall()
    by_day = defaultdict(list)
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({"ts": int(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return by_day


def score_levels(by_day, a, tol, pivot_kind):
    """One configuration, scored. Returns the numbers; printing is the caller's job."""
    days = sorted(by_day)
    step = a.tf * 60
    rng = random.Random(a.seed)
    tested = tot_pivots = gann_hits = in_range_total = 0
    random_hits = [0] * a.runs
    span_sum = 0.0
    per_day = []

    for i in range(1, len(days)):
        d, prev = days[i], days[i - 1]
        bars = aggregate(by_day[d], step)
        if len(bars) < 2 * a.window + 6:
            continue
        pv = by_day[prev]
        pivot = {"pdc": pv[-1]["c"],
                 "pdh": max(b["h"] for b in pv),
                 "pdl": min(b["l"] for b in pv)}[pivot_kind]

        levels = sq9_ladder(pivot)
        pivots = swing_pivots(bars, a.window)
        if not pivots:
            continue

        lo = min(b["l"] for b in bars)
        hi = max(b["h"] for b in bars)

        # DENSITY MUST MATCH, or the comparison is worthless. The Sq9 ladder spans roughly +/-300
        # points around the pivot, so on a 200-point day most of it sits where price never traded.
        # Scoring all eight against eight random levels drawn INSIDE the day's range hands random
        # far more usable levels and guarantees it wins -- a fairness bug, not a finding. So only
        # levels price could have reached are scored, and the baseline draws exactly that many.
        in_range = [lv for lv in levels if lo - tol <= lv <= hi + tol]
        if not in_range:
            continue

        span_sum += max(1.0, hi - lo)
        g = sum(1 for _, px, _ in pivots if near_any(px, in_range, tol))
        for r in range(a.runs):
            rl = [rng.uniform(lo, hi) for _ in range(len(in_range))]
            random_hits[r] += sum(1 for _, px, _ in pivots if near_any(px, rl, tol))

        tested += 1
        tot_pivots += len(pivots)
        gann_hits += g
        in_range_total += len(in_range)
        per_day.append((d, len(pivots), g, len(in_range), round(hi - lo)))

    if not tested or not tot_pivots:
        return None

    g_rate = gann_hits / tot_pivots * 100
    r_rates = [h / tot_pivots * 100 for h in random_hits]
    r_mean = statistics.mean(r_rates)
    r_sd = statistics.pstdev(r_rates) if len(r_rates) > 1 else 0.0
    return {
        "tested": tested, "pivots": tot_pivots, "hits": gann_hits,
        "g_rate": g_rate, "r_mean": r_mean, "r_sd": r_sd,
        "r_best": max(r_rates), "r_worst": min(r_rates),
        "beat": sum(1 for r in r_rates if r >= g_rate),
        "edge": g_rate - r_mean, "z": (g_rate - r_mean) / r_sd if r_sd > 0 else float("nan"),
        "in_range": in_range_total / tested, "mean_span": span_sum / tested,
        "per_day": per_day,
    }


def run_levels(conn, a) -> int:
    by_day = load_sessions(conn, a.sec)
    if not by_day:
        print(f"no candles for sec_id {a.sec} in {a.db}", file=sys.stderr)
        return 2
    s = score_levels(by_day, a, a.tol, a.pivot)
    if s is None:
        print("not enough sessions with pivots to score", file=sys.stderr)
        return 2

    print(f"\nGann Sq9 levels vs intraday turning points")
    print(f"database  : {a.db}")
    print(f"pivot     : previous session's {a.pivot.upper()}  ·  angles ±{', ±'.join(map(str, DEGREES))}")
    print(f"bars      : {a.tf}m   pivot window: ±{a.window} bars")
    print(f"tolerance : ±{a.tol} points   (fixed before the run)")
    print(f"sessions  : {s['tested']}   turning points: {s['pivots']}   mean session range: {s['mean_span']:.0f} pts")
    print(f"levels in range: {s['in_range']:.1f} of 8 per session "
          f"(only these are scored; the baseline draws the same number)\n")

    print(f"{'day':<12}{'pivots':>8}{'on a level':>13}{'levels in range':>17}{'range':>8}")
    for d, np_, g, nir, sp in s["per_day"]:
        print(f"{d:<12}{np_:>8}{g:>13}{nir:>17}{sp:>8}")

    print(f"\n{'':<24}{'hits':>8}{'rate':>9}")
    print(f"{'Gann Sq9 ladder':<24}{s['hits']:>8}{s['g_rate']:>8.1f}%")
    print(f"{'random, same count':<24}{s['r_mean']*s['pivots']/100:>8.1f}{s['r_mean']:>8.1f}%"
          f"   (sd {s['r_sd']:.1f}, {a.runs} runs, best {s['r_best']:.1f}%, worst {s['r_worst']:.1f}%)")
    print(f"\nedge over random : {s['edge']:+.1f} percentage points"
          + (f"   ({s['z']:+.1f} sd)" if s["r_sd"] > 0 else ""))
    print(f"random runs matching or beating Gann: {s['beat']} of {a.runs}")

    print()
    if s["beat"] >= max(1, a.runs // 5) or s["edge"] <= 0:
        print("READ: the Gann ladder sits inside the random spread. On this data it is not")
        print("      locating turning points better than arbitrary levels at the same density.")
    elif s["r_sd"] > 0 and s["z"] < 2:
        print("READ: Gann is ahead of random but by less than 2 sd. Suggestive, not established —")
        print("      the books' bar is a CLEAR and REPEATED difference, on more than one instrument.")
    else:
        print("READ: Gann clearly beats the random baseline here. Before trusting it, repeat on")
        print("      BANKNIFTY and on a different tolerance; one instrument is not a result.")
    print("\nRun --sweep to see whether that edge survives a change of tolerance or pivot.")
    print("A level says LOOK; price says ACT. Nothing here produces a direction.")
    return 0


def run_sweep(conn, a) -> int:
    """The decisive test. An edge that exists at one tolerance and one pivot is noise.

    The books list "widening the tolerance until it works" and "testing one instrument" as the two
    commonest ways people fool themselves. A real effect does not care that you moved the band from
    10 points to 12, or anchored on the previous high instead of the close. This prints the whole
    grid at once so no single flattering cell can be reported on its own.
    """
    by_day = load_sessions(conn, a.sec)
    if not by_day:
        print(f"no candles for sec_id {a.sec} in {a.db}", file=sys.stderr)
        return 2

    tols = [6.0, 8.0, 10.0, 12.0, 15.0, 20.0]
    pivots = ["pdc", "pdh", "pdl"]
    print(f"\nSensitivity sweep · {a.tf}m bars · ±{a.window}-bar pivots · {a.runs} random runs each")
    print(f"database: {a.db}\n")
    print(f"{'pivot':<7}{'tol':>6}{'Gann':>8}{'random':>9}{'edge':>8}{'sd':>7}{'beat':>7}{'in-range':>10}")

    cells = []
    for pk in pivots:
        for t in tols:
            s = score_levels(by_day, a, t, pk)
            if s is None:
                continue
            cells.append((pk, t, s))
            print(f"{pk:<7}{t:>6.0f}{s['g_rate']:>7.1f}%{s['r_mean']:>8.1f}%"
                  f"{s['edge']:>+7.1f}{s['z']:>7.1f}{s['beat']:>4}/{a.runs}{s['in_range']:>10.1f}")

    if not cells:
        print("nothing scorable", file=sys.stderr)
        return 2

    pos = [c for c in cells if c[2]["edge"] > 0]
    strong = [c for c in cells if c[2]["r_sd"] > 0 and c[2]["z"] >= 2]
    print(f"\ncells tested            : {len(cells)}")
    print(f"edge above zero         : {len(pos)}")
    print(f"edge at 2 sd or better  : {len(strong)}")
    edges = [c[2]["edge"] for c in cells]
    print(f"median edge             : {statistics.median(edges):+.1f} pp"
          f"   (range {min(edges):+.1f} to {max(edges):+.1f})")

    print()
    if len(strong) >= max(2, len(cells) // 3):
        print("READ: the edge holds across tolerances and pivots. That is the shape of a real")
        print("      effect. Next step is BANKNIFTY — one instrument is still one instrument.")
    elif len(pos) >= len(cells) * 0.7:
        print("READ: mostly positive but mostly weak. Consistent sign across settings is worth")
        print("      something; the size is not yet separable from noise.")
    else:
        print("READ: the sign flips with the settings. An effect that appears at one tolerance and")
        print("      vanishes at the next is the definition of curve-fitting, and the books name")
        print("      'widening the tolerance until it works' as the first way people fool themselves.")
    return 0


# ── mode: daily time cycles ───────────────────────────────────────────────────────────────────────

def run_cycles(conn, a) -> int:
    rows = conn.execute(
        "SELECT d,o,h,l,c FROM daily WHERE sec_id=? ORDER BY d", (a.sec,)).fetchall()
    bars = [{"d": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])}
            for r in rows]
    src_name = "the `daily` table"

    # The desk keeps a canonical daily CSV for exactly this kind of history, so fall back to it
    # rather than refusing. sync=False: no Dhan call is made from a backtest.
    if len(bars) < 120:
        try:
            from app.engine.gann_pivots_gaps import load_historical_daily_bars
            csv_bars = load_historical_daily_bars(sec_id=a.sec, sync=False) or []
            if len(csv_bars) > len(bars):
                bars = [{"d": b["date"], "o": float(b["open"]), "h": float(b["high"]),
                         "l": float(b["low"]), "c": float(b["close"])} for b in csv_bars]
                src_name = "the desk's canonical daily CSV"
        except Exception as e:
            print(f"(daily CSV loader unavailable: {type(e).__name__}: {e})", file=sys.stderr)

    if len(bars) < 120:
        print(f"only {len(bars)} daily bars for sec_id {a.sec}; need 120+ for a cycle test.",
              file=sys.stderr)
        print("Neither the `daily` table nor the canonical CSV has enough history. Time cycles are",
              file=sys.stderr)
        print("a positional tool — 30 to 360 CALENDAR days — so a year or more is the minimum that",
              file=sys.stderr)
        print("can say anything. Backfill daily bars, then re-run.", file=sys.stderr)
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
    print(f"daily bars : {len(bars)} from {src_name}   lead-in {lead}   test window {first} → {last}")
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



def run_validate(conn, a) -> int:
    """The two tests that separate a real effect from a lucky sample.

    1. OUT OF SAMPLE. Split the sessions chronologically and score each half on its own, each with
       its own random baseline. An effect present in the first half and absent in the second is a
       property of those particular days, not of the method.
    2. CONCENTRATION. Drop the sessions that contributed most and see what is left. An edge carried
       by three good days is a sample that happened to contain three good days -- the same test
       that dismantled the divergence engine's headline on 30 Oct.
    """
    by_day = load_sessions(conn, a.sec)
    if not by_day:
        print(f"no candles for sec_id {a.sec} in {a.db}", file=sys.stderr)
        return 2
    days = sorted(by_day)
    mid = len(days) // 2
    halves = [("first half", days[:mid]), ("second half", days[mid:])]

    print(f"\nValidation · pivot {a.pivot.upper()} · tolerance ±{a.tol} pts · {a.tf}m bars")
    print(f"database: {a.db}")
    print(f"sessions: {len(days)}  ({days[0]} → {days[-1]})\n")

    full = score_levels(by_day, a, a.tol, a.pivot)
    if full is None:
        print("nothing scorable", file=sys.stderr)
        return 2

    print("OUT OF SAMPLE")
    print(f"{'window':<14}{'sessions':>9}{'pivots':>8}{'Gann':>8}{'random':>9}{'edge':>8}{'sd':>7}")
    print(f"{'all':<14}{full['tested']:>9}{full['pivots']:>8}{full['g_rate']:>7.1f}%"
          f"{full['r_mean']:>8.1f}%{full['edge']:>+7.1f}{full['z']:>7.1f}")
    halves_ok = 0
    for name, ds in halves:
        sub = {d: by_day[d] for d in ds}
        h = score_levels(sub, a, a.tol, a.pivot)
        if h is None:
            print(f"{name:<14}{'—':>9}")
            continue
        if h["edge"] > 0:
            halves_ok += 1
        print(f"{name:<14}{h['tested']:>9}{h['pivots']:>8}{h['g_rate']:>7.1f}%"
              f"{h['r_mean']:>8.1f}%{h['edge']:>+7.1f}{h['z']:>7.1f}")

    # concentration: per-session surplus over that session's expected random hits
    per = full["per_day"]
    exp_rate = full["r_mean"] / 100.0
    contrib = sorted(((d, g - np_ * exp_rate, np_, g) for d, np_, g, _nir, _sp in per),
                     key=lambda x: x[1], reverse=True)
    tot_surplus = sum(c[1] for c in contrib)

    print(f"\nCONCENTRATION  (surplus = hits above what the random rate predicts for that session)")
    print(f"{'total surplus':<26}{tot_surplus:>+8.1f} hits across {len(per)} sessions")
    print(f"{'best session':<26}{contrib[0][0]} {contrib[0][1]:>+7.1f}")
    for drop in (1, 3, 5):
        if len(contrib) > drop:
            rest = contrib[drop:]
            rp = sum(c[2] for c in rest)
            rh = sum(c[3] for c in rest)
            rr = rh / rp * 100 if rp else 0.0
            print(f"{'excluding best ' + str(drop):<26}{rr - full['r_mean']:>+8.1f} pp"
                  f"   ({rh}/{rp} = {rr:.1f}% vs random {full['r_mean']:.1f}%)")

    pos_days = sum(1 for c in contrib if c[1] > 0)
    print(f"{'sessions with surplus > 0':<26}{pos_days} of {len(per)}")

    print()
    if halves_ok == 2 and pos_days >= len(per) * 0.55:
        print("READ: the edge appears in both halves and is spread across sessions rather than")
        print("      carried by a few. That is what a real effect looks like. The next test the")
        print("      books demand is a SECOND INSTRUMENT — rerun with --sec for BANKNIFTY.")
    elif halves_ok == 2:
        print("READ: present in both halves but concentrated in a minority of sessions. Worth")
        print("      continuing to record; not yet worth sizing against.")
    else:
        print("READ: the edge does not survive the split. It is a property of some of these days,")
        print("      not of the method. This is the most common honest outcome.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="levels", choices=["levels", "cycles", "sweep", "validate"])
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
    if a.mode == "sweep":
        return run_sweep(conn, a)
    if a.mode == "validate":
        return run_validate(conn, a)
    return run_levels(conn, a) if a.mode == "levels" else run_cycles(conn, a)


if __name__ == "__main__":
    raise SystemExit(main())
