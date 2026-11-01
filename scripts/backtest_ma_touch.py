#!/usr/bin/env python3
"""backtest_ma_touch.py — what actually happens when the 1-hour candle first touches the 8 MA.

THE CLAIM BEING TESTED
From the 31 Oct price-action session: enter at the 8 MA / 10 EMA with the hourly trend, 40-point
stop, 40-point first target, trail the rest. The MA is treated as dynamic support when price comes
down to it and dynamic resistance when price comes up to it.

WHAT IS MEASURED
A FIRST TOUCH is an hourly bar whose range contains the MA, when the previous `--gap` hourly bars
did NOT touch it. That is what makes it "first" — price has been away and has come back.

  from ABOVE  (prior close above the MA)  -> the MA is tested as SUPPORT    -> expect UP
  from BELOW  (prior close below the MA)  -> the MA is tested as RESISTANCE -> expect DOWN

Entry is the CLOSE of the touching hourly bar: you see the candle touch, then you act. The idealised
"filled exactly at the MA" entry is reported alongside it, because that is the number the method
implies and it is always flattering.

Forward measurement walks the ONE-MINUTE bars after that hourly close, so whether the stop or the
target came first is known rather than assumed.

WHY THERE IS A BASELINE
Run on a pure random walk, a naive version of this test reports stop 25 / target 20 winning 53% of
the time for +4.4 points a trade. That is not an edge, it is arithmetic: a target closer than the
stop wins often by construction. So every number is compared against RANDOM ENTRIES — the identical
direction rule and the identical stop/target machinery applied to hourly bars picked at random
instead of to first touches. The first touch has to beat that by more than the noise, or it has
earned nothing. The margin is reported in standard deviations.

READ-ONLY. Opens the database, computes, prints. Writes nothing. No network, no Dhan call.

USAGE
    python3 scripts/backtest_ma_touch.py                      # 1h, 8 SMA and 8 EMA, NIFTY
    python3 scripts/backtest_ma_touch.py --ma 10 --ma-type ema
    python3 scripts/backtest_ma_touch.py --sec FUT --horizon 4
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
DB = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"

SESSION_OPEN_MIN = 9 * 60 + 15          # 09:15 IST
STOPS = [15.0, 25.0, 40.0]
TARGETS = [20.0, 40.0, 60.0]


def session_bars(m1: list[dict], minutes: int) -> list[dict]:
    """Aggregate to `minutes` bars ALIGNED TO 09:15, which is how an NSE hourly chart is drawn.

    Bucketing on the raw epoch would start bars at 09:00/10:00 and put the opening 15 minutes in a
    bar of its own, which is not the candle the mentor is pointing at.
    """
    buckets: dict[int, dict] = {}
    for r in m1:
        t = datetime.fromtimestamp(r["ts"], IST)
        mins = t.hour * 60 + t.minute - SESSION_OPEN_MIN
        if mins < 0:
            continue
        k = mins // minutes
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"ts": r["ts"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            b["h"] = max(b["h"], r["h"])
            b["l"] = min(b["l"], r["l"])
            b["c"] = r["c"]
    return [buckets[k] for k in sorted(buckets)]


def moving_average(vals: list[float], n: int, kind: str) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    if len(vals) < n:
        return out
    if kind == "sma":
        run = sum(vals[:n])
        out[n - 1] = run / n
        for i in range(n, len(vals)):
            run += vals[i] - vals[i - n]
            out[i] = run / n
    else:                                        # ema, seeded on the first n-value average
        k = 2.0 / (n + 1)
        e = sum(vals[:n]) / n
        out[n - 1] = e
        for i in range(n, len(vals)):
            e = vals[i] * k + e * (1 - k)
            out[i] = e
    return out


def walk_forward(m1_after: list[dict], entry: float, long: bool) -> dict:
    """Minute-by-minute from entry. Exact ordering of stop vs target, no intrabar guessing."""
    mfe = mae = 0.0
    stop_at: dict[float, int | None] = {s: None for s in STOPS}
    tgt_at: dict[float, int | None] = {t: None for t in TARGETS}
    for i, b in enumerate(m1_after):
        fav = (b["h"] - entry) if long else (entry - b["l"])
        adv = (entry - b["l"]) if long else (b["h"] - entry)
        if fav > mfe:
            mfe = fav
        if adv > mae:
            mae = adv
        for s in STOPS:
            if stop_at[s] is None and adv >= s:
                stop_at[s] = i
        for t in TARGETS:
            if tgt_at[t] is None and fav >= t:
                tgt_at[t] = i
    return {"mfe": mfe, "mae": mae, "stop_at": stop_at, "tgt_at": tgt_at,
            "last": m1_after[-1]["c"] if m1_after else entry}


def expectancy(evs: list[dict], s: float, t: float) -> tuple[int, int, int, float]:
    """Win/loss/open counts and points per trade, open trades marked out at the horizon."""
    win = loss = opn = 0
    pnl = 0.0
    for e in evs:
        si, ti = e["stop_at"][s], e["tgt_at"][t]
        if ti is not None and (si is None or ti < si):
            win += 1
            pnl += t
        elif si is not None:
            loss += 1
            pnl -= s
        else:
            opn += 1
            pnl += (e["last"] - e["entry"]) if e["long"] else (e["entry"] - e["last"])
    return win, loss, opn, (pnl / len(evs) if evs else 0.0)


def describe(xs: list[float]) -> str:
    if not xs:
        return "       —        —        —        —"
    return (f"{max(xs):>9.0f}{min(xs):>9.0f}{statistics.mean(xs):>9.1f}"
            f"{statistics.median(xs):>9.1f}")


def zscore(real: float, sample: list[float]) -> tuple[float, float, float]:
    mean = statistics.mean(sample) if sample else 0.0
    sd = statistics.pstdev(sample) if len(sample) > 1 else 0.0
    return mean, sd, ((real - mean) / sd if sd > 1e-9 else 0.0)


def analyse(evs: list[dict], base_pool: list[dict], runs: int, rng: random.Random,
            name: str, dirn: str) -> None:
    n = len(evs)
    print(f"\n{name}   ({n} events)   expected: {dirn}")
    if n < 5:
        print("  too few events to say anything")
        return

    mfe = [e["mfe"] for e in evs]
    print(f"  {'':<22}{'max':>9}{'min':>9}{'mean':>9}{'median':>9}")
    print(f"  {'move in favour (MFE)':<22}{describe(mfe)}")
    print(f"  {'move against (MAE)':<22}{describe([e['mae'] for e in evs])}")
    print(f"  {'MFE if filled at MA':<22}{describe([e['ideal_mfe'] for e in evs])}")

    # Random-entry baseline: same direction rule, same machinery, bars chosen at random.
    samples: list[list[dict]] = []
    if len(base_pool) > n:
        samples = [rng.sample(base_pool, n) for _ in range(runs)]
    if samples:
        m, sd, z = zscore(statistics.median(mfe),
                          [statistics.median([e["mfe"] for e in s]) for s in samples])
        print(f"\n  median MFE vs random entries: {statistics.median(mfe):.0f} pts "
              f"vs {m:.0f} ± {sd:.1f}   ({z:+.1f} sd)")
    else:
        print("\n  not enough non-touch bars for a baseline")

    print(f"\n  {'stop':<8}{'hit':>7}{'hit %':>8}    {'target':<9}{'reached':>9}{'reached %':>11}")
    for s, t in zip(STOPS, TARGETS):
        sh = sum(1 for e in evs if e["stop_at"][s] is not None)
        th = sum(1 for e in evs if e["tgt_at"][t] is not None)
        print(f"  {s:<8.0f}{sh:>7}{sh/n*100:>7.0f}%    {t:<9.0f}{th:>9}{th/n*100:>10.0f}%")

    print(f"\n  {'stop':<6}{'target':>7}{'win':>5}{'loss':>6}{'open':>6}{'win %':>7}"
          f"{'real':>10}{'random':>17}{'edge':>10}")
    best = None
    for s in STOPS:
        for t in TARGETS:
            win, loss, opn, exp = expectancy(evs, s, t)
            line = f"  {s:<6.0f}{t:>7.0f}{win:>5}{loss:>6}{opn:>6}{win/n*100:>6.0f}%{exp:>+9.1f}"
            if samples:
                m, sd, z = zscore(exp, [expectancy(sm, s, t)[3] for sm in samples])
                line += f"{m:>+11.1f} ± {sd:<4.1f}{z:>+7.1f} sd"
                score = z
            else:
                line += f"{'—':>17}{'—':>10}"
                score = exp
            print(line)
            if best is None or score > best[0]:
                best = (score, s, t, exp, win / n * 100)

    if best and samples:
        print(f"\n  STRONGEST PAIR: stop {best[1]:.0f} / target {best[2]:.0f} → "
              f"{best[3]:+.1f} pts/trade, {best[4]:.0f}% wins, {best[0]:+.1f} sd above random")
        if best[0] < 2.0:
            print("  NOT SIGNIFICANT — random entries do about as well. The first touch is adding")
            print("  nothing here; any apparent profit is the stop/target geometry, not the MA.")

    wick = [e for e in evs if not e["closed_through"]]
    thru = [e for e in evs if e["closed_through"]]
    if len(wick) >= 5 and len(thru) >= 5:
        print(f"\n  wick touch only ({len(wick)}): median MFE "
              f"{statistics.median([e['mfe'] for e in wick]):.0f}, median MAE "
              f"{statistics.median([e['mae'] for e in wick]):.0f}")
        print(f"  closed through  ({len(thru)}): median MFE "
              f"{statistics.median([e['mfe'] for e in thru]):.0f}, median MAE "
              f"{statistics.median([e['mae'] for e in thru]):.0f}")


def run(conn, a) -> int:
    rows = conn.execute(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id=? ORDER BY ts", (a.sec,)).fetchall()
    if not rows:
        have = [r[0] for r in conn.execute("SELECT DISTINCT sec_id FROM candles_1m")]
        print(f"no candles for sec_id {a.sec}. available: {have or '(table empty)'}", file=sys.stderr)
        return 2

    by_day: dict[str, list[dict]] = defaultdict(list)
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({"ts": int(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    days = sorted(by_day)
    rng = random.Random(a.seed)

    print(f"\n8 MA FIRST-TOUCH TEST  ·  {a.tf}-minute bars  ·  sec {a.sec}")
    print(f"database : {a.db}")
    print(f"sessions : {len(days)}  ({days[0]} → {days[-1]})")
    print(f"entry    : close of the touching bar   ·   horizon {a.horizon} hours or session end")
    print(f"first    : no touch in the previous {a.gap} bars")
    print(f"forward  : walked on 1-minute bars, so stop-vs-target order is exact")
    print(f"baseline : {a.runs} runs of random hourly entries, same direction rule\n")

    for kind in (["sma", "ema"] if a.ma_type == "both" else [a.ma_type]):
        # One continuous hourly series across sessions. The MA must not reset each morning or the
        # first bars of every day would carry a meaningless average.
        hourly: list[dict] = []
        for d in days:
            for b in session_bars(by_day[d], a.tf):
                b["day"] = d
                hourly.append(b)
        ma = moving_average([b["c"] for b in hourly], a.ma, kind)

        cache: dict[int, dict | None] = {}

        def event_at(i: int) -> dict | None:
            """Measured event for hourly bar i, or None if unusable. Cached, so the baseline runs
            cost almost nothing."""
            if i in cache:
                return cache[i]
            cache[i] = None
            m, prev_m = ma[i], ma[i - 1]
            if m is None or prev_m is None:
                return None
            b = hourly[i]
            end_ts = b["ts"] + a.horizon * 3600
            after = [r for r in by_day[b["day"]] if b["ts"] < r["ts"] <= end_ts]
            if len(after) < 10:
                return None
            long = hourly[i - 1]["c"] > prev_m
            w = walk_forward(after, b["c"], long)
            cache[i] = {**w, "long": long, "entry": b["c"], "ma": m, "day": b["day"],
                        "ideal_mfe": walk_forward(after, m, long)["mfe"],
                        "closed_through": (b["c"] < m) if long else (b["c"] > m),
                        "time": datetime.fromtimestamp(b["ts"], IST).strftime("%H:%M")}
            return cache[i]

        touches: list[dict] = []
        pool: list[dict] = []
        last_touch = -99
        for i in range(1, len(hourly)):
            if ma[i] is None:
                continue
            e = event_at(i)
            if e is not None:
                pool.append(e)
            if hourly[i]["l"] <= ma[i] <= hourly[i]["h"]:
                if i - last_touch > a.gap and e is not None:
                    touches.append(e)
                last_touch = i

        print("=" * 100)
        print(f"{a.ma} {'SMA' if kind == 'sma' else 'EMA'}  ({a.ma}-period {kind.upper()} on "
              f"{a.tf}m closes)   {len(touches)} first touches out of {len(pool)} usable bars")
        print("=" * 100)
        if not touches:
            print("no first-touch events found\n")
            continue

        for long, name, dirn in (
                (True, "SUPPORT TEST — price came DOWN to the MA", "bounce UP"),
                (False, "RESISTANCE TEST — price came UP to the MA", "fall DOWN")):
            analyse([e for e in touches if e["long"] == long],
                    [e for e in pool if e["long"] == long],
                    a.runs, rng, name, dirn)
        print()

    print("=" * 100)
    print("Read the MEDIAN before the mean — one trending session moves the mean a long way. The")
    print("'edge' column is the only one that matters: anything under +2 sd is noise, however good")
    print("the raw points look. And every number is the index itself, not option premium, which")
    print("decays while you hold.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--sec", default="13")
    ap.add_argument("--tf", type=int, default=60, help="bar size in minutes, aligned to 09:15")
    ap.add_argument("--ma", type=int, default=8)
    ap.add_argument("--ma-type", default="both", choices=["sma", "ema", "both"])
    ap.add_argument("--gap", type=int, default=3, help="bars clear of the MA before a touch is 'first'")
    ap.add_argument("--horizon", type=int, default=6, help="hours to follow the trade")
    ap.add_argument("--runs", type=int, default=400, help="random-entry baseline runs")
    ap.add_argument("--seed", type=int, default=20261101)
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"database not found: {a.db}", file=sys.stderr)
        return 2
    return run(sqlite3.connect(a.db), a)


if __name__ == "__main__":
    raise SystemExit(main())
