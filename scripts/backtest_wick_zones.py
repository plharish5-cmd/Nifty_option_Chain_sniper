#!/usr/bin/env python3
"""backtest_wick_zones.py — do 1-hour candle WICKS act as support and resistance?

WHAT A WICK ZONE IS
For each completed 1-hour candle:

    UPPER WICK ZONE = [ max(Open, Close) , High ]      the rejected territory above the body
    LOWER WICK ZONE = [ Low , min(Open, Close) ]       the rejected territory below the body

Both ends of each zone and its size in points are tabulated first (--csv dumps every row).

WHAT IS THEN TESTED
Each zone goes live when its hourly candle closes. The script then walks forward on 15-MINUTE bars
and finds the FIRST bar that touches the zone — virgin test only; once touched, the zone is spent.
The approach direction comes from the previous 15-minute close:

    UPPER zone touched FROM BELOW  -> tested as RESISTANCE -> expect DOWN
    UPPER zone touched FROM ABOVE  -> tested as SUPPORT    -> expect UP
    LOWER zone touched FROM ABOVE  -> tested as SUPPORT    -> expect UP
    LOWER zone touched FROM BELOW  -> tested as RESISTANCE -> expect DOWN

All four are reported. The middle two are off-hypothesis and are exactly the comparison that says
whether a wick is a LEVEL or merely a place price happens to be.

Entry is the close of the touching 15-minute bar. Forward measurement walks 1-MINUTE bars starting
strictly AFTER that bar closes, so stop-vs-target ordering is exact and there is no look-ahead.

FOUR CONTROLS, all carried over from scripts/backtest_ma_touch.py where each one was needed:

  1. Random-entry baseline MATCHED ON HOUR-OF-DAY. An unmatched baseline flatters the result,
     because a late-session random bar has no runway before the horizon truncates at the close.
  2. Direction control: every event re-run the opposite way on identical bars, with the gap scored
     against the gap random entries produce for free (on a random walk that gap is ~+7 points from
     nothing, so the raw gap is not usable alone).
  3. Concentration: expectancy excluding the best 1 / 3 / 5 events.
  4. Chronological split-half.

Plus a per-event table and time-to-resolution, because a look-ahead bug in the sibling script was
invisible in every summary statistic and was caught only by events resolving "in 0 min".

READ-ONLY. No network, no Dhan call, no order path.

USAGE
    python3 scripts/backtest_wick_zones.py
    python3 scripts/backtest_wick_zones.py --sec BNFUT --min-wick 5
    python3 scripts/backtest_wick_zones.py --csv ~/wick_ranges.csv
    python3 scripts/backtest_wick_zones.py --carry-days 1      # zones live into the next session
"""
from __future__ import annotations

import argparse
import csv as csvmod
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_ma_touch import (                                    # noqa: E402
    DB, IST, STOPS, TARGETS, contribution, describe, expectancy, session_bars,
    walk_forward, zscore,
)

GROUPS = [
    ("UPPER", False, "UPPER WICK tested as RESISTANCE — price came up into it", "fall DOWN"),
    ("LOWER", True, "LOWER WICK tested as SUPPORT — price came down into it", "bounce UP"),
    ("UPPER", True, "UPPER WICK tested as SUPPORT — price fell back into it", "bounce UP"),
    ("LOWER", False, "LOWER WICK tested as RESISTANCE — price rose into it", "fall DOWN"),
]


def wick_table(h1_by_day: dict[str, list[dict]], min_wick: float, csv_path: str | None) -> None:
    """The table asked for: both ends of every wick and its size, upper and lower side by side."""
    rows = []
    for day in sorted(h1_by_day):
        for b in h1_by_day[day]:
            body_hi, body_lo = max(b["o"], b["c"]), min(b["o"], b["c"])
            rows.append({
                "date": day,
                "time": datetime.fromtimestamp(b["ts"], IST).strftime("%H:%M"),
                "open": round(b["o"], 2), "high": round(b["h"], 2),
                "low": round(b["l"], 2), "close": round(b["c"], 2),
                "upper_wick_from": round(body_hi, 2), "upper_wick_to": round(b["h"], 2),
                "upper_wick_pts": round(b["h"] - body_hi, 2),
                "lower_wick_from": round(b["l"], 2), "lower_wick_to": round(body_lo, 2),
                "lower_wick_pts": round(body_lo - b["l"], 2),
            })

    up = [r["upper_wick_pts"] for r in rows]
    lo = [r["lower_wick_pts"] for r in rows]
    print(f"\n1-HOUR WICK RANGES  ({len(rows)} candles)")
    print(f"  {'':<24}{'max':>9}{'min':>9}{'mean':>9}{'median':>9}")
    print(f"  {'upper wick (points)':<24}{describe(up)}")
    print(f"  {'lower wick (points)':<24}{describe(lo)}")

    def pct(xs, p):
        s = sorted(xs)
        return s[min(len(s) - 1, int(len(s) * p / 100))]

    print(f"\n  {'percentile':<14}{'10th':>8}{'25th':>8}{'50th':>8}{'75th':>8}{'90th':>8}")
    print(f"  {'upper wick':<14}" + "".join(f"{pct(up, p):>8.1f}" for p in (10, 25, 50, 75, 90)))
    print(f"  {'lower wick':<14}" + "".join(f"{pct(lo, p):>8.1f}" for p in (10, 25, 50, 75, 90)))
    nu = sum(1 for v in up if v >= min_wick)
    nl = sum(1 for v in lo if v >= min_wick)
    print(f"\n  zones at least {min_wick:.0f} pts deep:  upper {nu}  ·  lower {nl}"
          f"   (shorter wicks are skipped — a 1-point wick is not a level)")

    if csv_path:
        p = Path(csv_path).expanduser()
        with p.open("w", newline="") as fh:
            w = csvmod.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n  full per-candle table written to {p}")


def analyse(evs: list[dict], samples: list[list[dict]], loose: list[list[dict]],
            name: str, dirn: str) -> None:
    n = len(evs)
    nd = len({e["day"] for e in evs})
    print(f"\n{name}")
    print(f"  {n} first touches across {nd} sessions   ·   expected: {dirn}")
    if n and nd < n * 0.6:
        print(f"  note: {n} events on only {nd} sessions — same-day events share one market path,")
        print("  which is why the baseline resamples whole sessions rather than single bars.")
    if n < 5:
        print("  too few events to say anything")
        return

    mfe = [e["mfe"] for e in evs]
    print(f"\n  {'':<24}{'max':>9}{'min':>9}{'mean':>9}{'median':>9}")
    print(f"  {'move in favour (MFE)':<24}{describe(mfe)}")
    print(f"  {'move against (MAE)':<24}{describe([e['mae'] for e in evs])}")
    print(f"  {'zone depth (points)':<24}{describe([e['depth'] for e in evs])}")

    if samples:
        m, sd, z = zscore(statistics.median(mfe),
                          [statistics.median([x["mfe"] for x in s]) for s in samples])
        print(f"\n  median MFE vs random entries, same hour and same side: "
              f"{statistics.median(mfe):.0f} vs {m:.0f} ± {sd:.1f}   ({z:+.1f} sd)")
    else:
        print("\n  no baseline available for these slots")

    # Net directional pull. MFE - MAE uses the WHOLE excursion instead of discretising at one
    # stop and one target, so it resolves direction where the fixed-rule flip test cannot: that
    # test is a difference of two noisy quantities and is badly underpowered at n < 50.
    net_z = None
    if samples:
        net = statistics.median([e["mfe"] - e["mae"] for e in evs])
        m, sd, net_z = zscore(net, [statistics.median([x["mfe"] - x["mae"] for x in s])
                                    for s in samples])
        print(f"  median (MFE - MAE), the net pull in the expected direction: {net:+.0f} "
              f"vs {m:+.0f} ± {sd:.1f}   ({net_z:+.1f} sd)")

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
    if not best:
        return

    score, s, t, exp, winpct = best
    print(f"\n  STRONGEST PAIR: stop {s:.0f} / target {t:.0f} → {exp:+.1f} pts/trade, "
          f"{winpct:.0f}% wins, {score:+.1f} sd above random")

    if loose and samples and loose is not samples:
        _, _, zl = zscore(exp, [expectancy(sm, s, t)[3] for sm in loose])
        print(f"  session-resampled baseline says {score:+.1f} sd; treating events as independent\n          would have said {zl:+.1f} sd")

    fl = expectancy([e["flip"] for e in evs], s, t)[3]
    dir_z = None
    if samples:
        gm, gsd, dir_z = zscore(exp - fl,
                                [expectancy(sm, s, t)[3]
                                 - expectancy([x["flip"] for x in sm], s, t)[3] for sm in samples])
        print(f"  opposite side on the same bars: {fl:+.1f} (stated side {exp - fl:+.1f} better; "
              f"random gives {gm:+.1f} ± {gsd:.1f} free → {dir_z:+.1f} sd)")

    contribs = sorted((contribution(e, s, t) for e in evs), reverse=True)
    tot = sum(contribs)
    drops = [(k, (tot - sum(contribs[:k])) / (n - k)) for k in (1, 3, 5) if n - k >= 3]
    print("  excluding   " + "   ".join(f"best {k}: {v:+.1f}" for k, v in drops) + "   pts/trade")

    udays = sorted({e["day"] for e in evs})
    if len(udays) >= 6:
        cut = udays[len(udays) // 2]
        h1 = [e for e in evs if e["day"] < cut]
        h2 = [e for e in evs if e["day"] >= cut]
        if len(h1) >= 3 and len(h2) >= 3:
            print(f"  first half ({len(h1)}): {expectancy(h1, s, t)[3]:+.1f}   "
                  f"second half ({len(h2)}): {expectancy(h2, s, t)[3]:+.1f}   pts/trade")

    tt = [e["tgt_at"][t] for e in evs if e["tgt_at"][t] is not None]
    ts = [e["stop_at"][s] for e in evs if e["stop_at"][s] is not None]
    if tt:
        print(f"  median time to the {t:.0f}-pt target: {statistics.median(tt):.0f} min"
              + (f"   ·   to the {s:.0f}-pt stop: {statistics.median(ts):.0f} min" if ts else ""))

    bad = []
    if score < 2.0:
        bad.append("random entries at the same hour do as well")
    if drops and drops[-1][1] <= 0:
        bad.append("the edge dies without its best few events")
    if bad:
        print(f"  NOT A LEVEL — {'; '.join(bad)}.")
    elif net_z is not None and net_z >= 2.0:
        print("  DIRECTIONAL AND SIGNIFICANT. The net excursion asymmetry carries the direction")
        print("  even where the fixed-rule flip test cannot resolve it. Candidate only: check how")
        print("  many groups were tested, and whether the other instrument agrees.")
    elif dir_z is not None and dir_z < 2.0:
        print("  SIGNIFICANT BUT DIRECTION UNRESOLVED — bigger moves follow these touches than")
        print("  follow random bars at the same hour, but neither directional test clears its")
        print("  noise at this sample size. This marks where volatility expands; it does not yet")
        print("  tell you which way. Treat it as a heads-up, not a trade direction.")
    else:
        print("  SURVIVES all three controls. Candidate only: see how many groups were tested.")

    if n <= 45:
        print(f"\n  {'date':<12}{'time':<7}{'side':<6}{'zone':>17}{'entry':>9}"
              f"{'MFE':>6}{'MAE':>6}   outcome at {s:.0f}/{t:.0f}")
        for e in sorted(evs, key=lambda x: (x["day"], x["time"])):
            si, ti = e["stop_at"][s], e["tgt_at"][t]
            if ti is not None and (si is None or ti < si):
                out = f"WIN  +{t:.0f} in {ti} min"
            elif si is not None:
                out = f"LOSS -{s:.0f} in {si} min"
            else:
                out = f"open, marked {contribution(e, s, t):+.0f}"
            print(f"  {e['day']:<12}{e['time']:<7}{'LONG' if e['long'] else 'SHORT':<6}"
                  f"{e['zone_lo']:>8.0f}-{e['zone_hi']:<8.0f}{e['entry']:>9.1f}"
                  f"{e['mfe']:>6.0f}{e['mae']:>6.0f}   {out}")


def run(conn, a) -> int:
    rows = conn.execute(
        "SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id=? ORDER BY ts", (a.sec,)).fetchall()
    if not rows:
        have = [r[0] for r in conn.execute("SELECT DISTINCT sec_id FROM candles_1m")]
        print(f"no candles for sec_id {a.sec}. available: {have or '(empty)'}", file=sys.stderr)
        return 2

    by_day: dict[str, list[dict]] = defaultdict(list)
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({"ts": int(ts), "o": float(o), "h": float(h),
                          "l": float(l), "c": float(c)})
    days = sorted(by_day)
    rng = random.Random(a.seed)

    h1_by_day = {d: session_bars(by_day[d], 60) for d in days}
    m15_by_day = {d: session_bars(by_day[d], a.tf) for d in days}

    print(f"\n1-HOUR WICK ZONES AS SUPPORT / RESISTANCE   ·   sec {a.sec}")
    print(f"database : {a.db}")
    print(f"sessions : {len(days)}  ({days[0]} → {days[-1]})")
    print(f"zones    : upper = [max(O,C), High]   lower = [Low, min(O,C)]   "
          f"min depth {a.min_wick:.0f} pts")
    print(f"tested on: {a.tf}-minute bars, first touch only, zone live for "
          f"{'the rest of its own session' if not a.carry_days else f'{a.carry_days} further session(s)'}")
    print(f"forward  : 1-minute bars from after the {a.tf}m bar closes, horizon {a.horizon}h")
    print(f"baseline : {a.runs} runs, matched on hour-of-day AND side")

    wick_table(h1_by_day, a.min_wick, a.csv)

    # ---- every 15m bar, walked both ways, cached. Serves events and baseline alike. ----
    cache: dict[tuple[str, int], dict | None] = {}

    def walked(day: str, idx: int) -> dict | None:
        key = (day, idx)
        if key in cache:
            return cache[key]
        cache[key] = None
        b = m15_by_day[day][idx]
        end = b["ts_end"] + a.horizon * 3600
        after = [r for r in by_day[day] if b["ts_end"] < r["ts"] <= end]
        if len(after) < 10:
            return None
        cache[key] = {
            "slot": b["slot"], "day": day, "entry": b["c"],
            "time": datetime.fromtimestamp(b["ts"], IST).strftime("%H:%M"),
            "long": walk_forward(after, b["c"], True),
            "short": walk_forward(after, b["c"], False),
        }
        return cache[key]

    def side(w: dict, long: bool) -> dict:
        base = w["long"] if long else w["short"]
        other = w["short"] if long else w["long"]
        return {**base, "long": long, "entry": w["entry"], "day": w["day"], "slot": w["slot"],
                "time": w["time"], "flip": {**other, "long": not long, "entry": w["entry"]}}

    pool_by_slot: dict[int, list[dict]] = defaultdict(list)
    pool_by_day_slot: dict[tuple[str, int], dict] = {}
    for d in days:
        for i in range(len(m15_by_day[d])):
            w = walked(d, i)
            if w is not None:
                pool_by_slot[w["slot"]].append(w)
                pool_by_day_slot[(d, w["slot"])] = w

    # ---- build zones and find each one's first touch ----
    events: dict[tuple[str, bool], list[dict]] = defaultdict(list)
    made = touched = 0
    for di, d in enumerate(days):
        horizon_days = days[di:di + 1 + a.carry_days]
        for hb in h1_by_day[d]:
            body_hi, body_lo = max(hb["o"], hb["c"]), min(hb["o"], hb["c"])
            for kind, zlo, zhi in (("UPPER", body_hi, hb["h"]), ("LOWER", hb["l"], body_lo)):
                depth = zhi - zlo
                if depth < a.min_wick or (a.max_wick and depth > a.max_wick):
                    continue
                made += 1
                hit = None
                for hd in horizon_days:
                    bars = m15_by_day[hd]
                    for i in range(1, len(bars)):
                        b = bars[i]
                        if b["ts"] <= hb["ts_end"]:
                            continue
                        if b["l"] > zhi or b["h"] < zlo:       # no overlap
                            continue
                        prev_c = bars[i - 1]["c"]
                        if zlo <= prev_c <= zhi:               # already inside: ambiguous
                            hit = "skip"
                            break
                        hit = (hd, i, prev_c > zhi)            # from_above?
                        break
                    if hit is not None:
                        break
                if hit is None or hit == "skip":
                    continue
                hd, i, from_above = hit
                w = walked(hd, i)
                if w is None:
                    continue
                # UPPER from above -> support -> long. LOWER from above -> support -> long.
                # Either zone approached from below is a resistance test -> short.
                long = from_above
                ev = side(w, long)
                ev.update({"zone_lo": zlo, "zone_hi": zhi, "depth": depth, "kind": kind,
                           "bar": i})
                events[(kind, long)].append(ev)
                touched += 1

    # DEDUPE. Two zones from different hourly candles can be first-touched by the SAME 15-minute
    # bar — adjacent wicks, or one nested in another. Their forward path is literally the same
    # bar sequence, so the outcome is identical and counting it twice both inflates n and shrinks
    # the apparent standard error. One event per (session, bar, side); the deeper zone wins.
    dropped = 0
    for key, evs in events.items():
        seen: dict[tuple[str, int], dict] = {}
        for e in evs:
            k = (e["day"], e["bar"])
            if k not in seen or e["depth"] > seen[k]["depth"]:
                seen[k] = e
        dropped += len(evs) - len(seen)
        events[key] = list(seen.values())

    print(f"\n  {made} zones created, {touched} received a first touch within the window"
          + (f"\n  {dropped} dropped as duplicates — a different zone resolved by the same "
             f"15m bar, so the same outcome" if dropped else ""))

    for gi, (kind, long, name, dirn) in enumerate(GROUPS):
        evs = events[(kind, long)]
        if not evs and gi >= 2:
            print("\n" + "=" * 100)
            print(f"\n{name}\n  structurally impossible for a FIRST touch, as expected: a wick zone")
            print("  lies on the far side of the candle that made it, so price starts on one side")
            print("  only. Reaching it from the other side means passing through it first, which")
            print("  spends the zone. The direction control inside each live group does this job.")
            print("  Raise --carry-days to let a zone survive into a session that gaps past it.")
            continue
        # ---- baselines ----
        # LOOSE: each event independently matched to a random bar at the same hour. This is what
        # the script reported before, and it OVERSTATES significance, because events in one
        # session share a single market path and are not independent draws.
        # BLOCK: whole sessions are resampled. For every real session, a donor session is drawn
        # and its bars at the same slots are taken together, so the null carries the same
        # within-day dependence as the real sample. This is the figure to trust.
        samples: list[list[dict]] = []
        loose: list[list[dict]] = []
        if evs and all(len(pool_by_slot[e["slot"]]) >= 3 for e in evs):
            loose = [[side(rng.choice(pool_by_slot[e["slot"]]), long) for e in evs]
                     for _ in range(a.runs)]

            per_day: dict[str, list[int]] = defaultdict(list)
            for e in evs:
                per_day[e["day"]].append(e["slot"])
            donors: dict[tuple[int, ...], list[str]] = {}
            ok = True
            for slots in per_day.values():
                key = tuple(sorted(slots))
                if key not in donors:
                    donors[key] = [d for d in days
                                   if all((d, s) in pool_by_day_slot for s in key)]
                if not donors[key]:
                    ok = False
                    break
            if ok:
                blocks = list(per_day.values())
                samples = [[side(pool_by_day_slot[(rng.choice(donors[tuple(sorted(sl))]), s)], long)
                            for sl in blocks for s in sl]
                           for _ in range(a.runs)]
            else:
                samples = loose
        print("\n" + "=" * 100)
        analyse(evs, samples, loose, name, dirn)

    print("\n" + "=" * 100)
    print("TWO live groups x 9 stop/target cells = 18 cells were tested. With that many, one cell")
    print("clearing +2 sd by chance alone is ordinary, so a group counts only if it survives the")
    print("same-hour baseline, the direction control AND the removal of its best five events. The")
    print("direction control is the one that decides whether a wick is a LEVEL: if trading the")
    print("same bars the opposite way pays equally, the wick is not support or resistance, it is")
    print("just somewhere price happens to be.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--sec", default="13")
    ap.add_argument("--tf", type=int, default=15, help="bar size the touch is detected on")
    ap.add_argument("--min-wick", type=float, default=3.0, help="minimum zone depth in points")
    ap.add_argument("--max-wick", type=float, default=0.0,
                    help="skip zones DEEPER than this (0 = no cap). A 150-point wick zone is a "
                         "neighbourhood, not a level — almost anything touches it. Try 40 on "
                         "NIFTY, 120 on BANKNIFTY, roughly the 90th percentile of wick depth.")
    ap.add_argument("--carry-days", type=int, default=0, help="extra sessions a zone stays live")
    ap.add_argument("--horizon", type=int, default=2, help="hours to follow the trade")
    ap.add_argument("--runs", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--csv", default="", help="write the full per-candle wick table here")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"database not found: {a.db}", file=sys.stderr)
        return 2
    return run(sqlite3.connect(a.db), a)


if __name__ == "__main__":
    raise SystemExit(main())
