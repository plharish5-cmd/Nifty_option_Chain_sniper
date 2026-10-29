#!/usr/bin/env python3
"""backtest_divergence.py — replay the RSI divergence engine over EVERY stored session.

WHY
One day is an anecdote. The desk has months of 1-minute candles sitting in candles_1m, so there is
no excuse for judging a signal engine on a single session. This walks every day in the database,
runs the engine exactly as the live endpoint does, and scores what actually happened AFTER each
confirmation using the bars that followed.

It is read-only. It opens the database, computes, prints, and writes nothing.

WHAT IT MEASURES
For every STRUCTURE_CONFIRMED event, using only bars after the confirmation bar:
  MFE   maximum favourable excursion -- the best the trade ever looked, in points
  MAE   maximum adverse excursion -- the worst it ever looked
  R     MFE / risk, where risk is the engine's own entry-to-invalidation distance
  hit   did price reach 2R before touching the stop?
It also counts how often each divergence TYPE confirms at all, because an engine that never fires is
not thereby a good engine -- it is just silent.

USAGE
    python3 scripts/backtest_divergence.py                 # all sessions, 3m
    python3 scripts/backtest_divergence.py --tf 5m
    python3 scripts/backtest_divergence.py --regime-gate   # also apply the day-regime veto
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ict_smc_app" / "backend"))

from app.engine import rsi_divergence as rd  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DB = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
NIFTY_SEC = "13"


def sessions(conn, sec_id):
    """Every distinct IST trading date present in candles_1m, with its bar count."""
    rows = conn.execute(
        "SELECT ts, o, h, l, c FROM candles_1m WHERE sec_id=? ORDER BY ts", (sec_id,)).fetchall()
    by_day = defaultdict(list)
    for ts, o, h, l, c in rows:
        d = datetime.fromtimestamp(ts, IST).date().isoformat()
        by_day[d].append({"ts": int(ts), "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return by_day


def score(bars, ev):
    """MFE / MAE / R for one confirmed event, from the bars AFTER it resolved."""
    i = next((k for k, b in enumerate(bars) if b["ts"] >= ev.get("resolved_ts", 0)), None)
    if i is None or i >= len(bars) - 1:
        return None
    after = bars[i + 1:]
    entry, sl = float(ev["entry"]), float(ev["sl"])
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    short = ev["side"] == "short"
    # Walk forward in order so "stopped before target" is answered causally, not by comparing extremes.
    #
    # The walk STOPS at the stop-loss. It used to keep accumulating MFE after the stop was hit, which
    # credited the signal with moves it could never have been in for -- a trade stopped out at 10:15
    # was still collecting favourable excursion from the 14:00 rally. That inflated median R while
    # leaving the 2R hit rate correct (hit2r was always gated on `not stopped`), so the headline
    # percentages were sound and the R figure was not.
    mfe = mae = 0.0
    hit2r = stopped = False
    for b in after:
        fav = (entry - float(b["l"])) if short else (float(b["h"]) - entry)
        adv = (float(b["h"]) - entry) if short else (entry - float(b["l"]))
        if adv >= risk:
            # Stop first within this bar, conservatively: a bar that spans both levels is a loss.
            mae = max(mae, risk)
            stopped = True
            break
        mfe, mae = max(mfe, fav), max(mae, adv)
        if fav >= 2 * risk:
            hit2r = True
            break
    # Realized R under a fixed rule, which is what an account actually books: +2R on a target hit,
    # -1R on a stop, and mark-to-last otherwise. MFE is the best the trade ever LOOKED and is kept
    # only as an upper bound.
    if hit2r:
        realized = 2.0
    elif stopped:
        realized = -1.0
    else:
        last = float(after[-1]["c"])
        realized = ((entry - last) if short else (last - entry)) / risk
    return {"mfe": mfe, "mae": mae, "r": mfe / risk, "realized": realized, "risk": risk,
            "hit2r": hit2r, "stopped_first": stopped and not hit2r}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="3m", choices=list(rd.BAR_SECONDS))
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--sec", default=NIFTY_SEC)
    ap.add_argument("--regime-gate", action="store_true",
                    help="apply the day-regime veto using each session's opening-window direction")
    ap.add_argument("--cutoff", default="",
                    help="IST HH:MM after which a confirmation is ignored, e.g. 14:45. A signal "
                         "confirming near the close has no room to reach its target.")
    ap.add_argument("--regime-bars", type=int, default=20,
                    help="bars of the open used to fix the regime under --regime-gate (default 20). "
                         "Events armed inside this window are dropped, not judged.")
    a = ap.parse_args()

    if not Path(a.db).exists():
        print(f"database not found: {a.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(a.db)
    by_day = sessions(conn, a.sec)
    if not by_day:
        print(f"no candles for sec_id {a.sec} in {a.db}", file=sys.stderr)
        return 2

    step = rd.BAR_SECONDS[a.tf]
    per_type = defaultdict(lambda: defaultdict(int))
    scored = []
    day_rows = []
    dropped_early = 0
    late_dropped = 0

    for d in sorted(by_day):
        m1 = by_day[d]
        bars = rd.aggregate_1m(m1, step, m1[-1]["ts"] + step)
        if len(bars) < 30:
            continue
        # A crude stand-in for the desk's cumulative Net PE-CE regime. It reads the direction the
        # session had taken by the END OF ITS OPENING WINDOW -- not by its midpoint, which was the
        # original and which leaked: a regime fixed at ~12:20 was being applied to events armed at
        # 10:00, telling the morning what the afternoon would do. Events inside the window are
        # dropped rather than judged by a regime that did not exist yet.
        regime, gate_from = "none", 0
        if a.regime_gate:
            gate_from = min(a.regime_bars, len(bars) - 1)
            move = bars[gate_from]["c"] - bars[0]["c"]
            regime = "bull" if move > 60 else "bear" if move < -60 else "none"
        res = rd.compute_rsi_divergence(bars, tf=a.tf, day_regime=regime)
        if not res.get("ok"):
            continue
        evs = res["events"]
        if a.regime_gate and gate_from:
            # Causality: an event armed at or before the gate window cannot be judged by a regime
            # derived from that same window.
            cutoff = int(bars[gate_from]["ts"])
            dropped_early += sum(1 for e in evs if int(e["armed_ts"]) <= cutoff)
            evs = [e for e in evs if int(e["armed_ts"]) > cutoff]
        for e in evs:
            per_type[e["type"]][e["state"]] += 1
            if e["state"] == "STRUCTURE_CONFIRMED":
                # A confirmation at 15:09 has twenty minutes to pay 2R on a 55-point risk. It cannot,
                # so counting it measures the clock, not the signal. --cutoff drops those.
                if a.cutoff and str(e.get("resolved_time", "")) >= a.cutoff:
                    late_dropped += 1
                    continue
                sc = score(bars, e)
                if sc:
                    scored.append({**sc, "day": d, "type": e["type"], "side": e["side"],
                                   "time": e.get("resolved_time", "")})
        day_rows.append((d, len(bars), len(evs),
                         sum(1 for e in evs if e["state"] == "STRUCTURE_CONFIRMED")))

    print(f"\nRSI divergence backtest · {a.tf} · sec {a.sec} · regime gate "
          f"{'ON' if a.regime_gate else 'off'}")
    print(f"database: {a.db}")
    print(f"sessions with enough data: {len(day_rows)}\n")
    print(f"{'day':<12}{'bars':>6}{'events':>8}{'confirmed':>11}")
    for d, nb, ne, nc in day_rows:
        print(f"{d:<12}{nb:>6}{ne:>8}{nc:>11}")

    print(f"\n{'divergence type':<18}{'armed':>7}{'confirm':>9}{'invalid':>9}{'expired':>9}{'vetoed':>8}")
    for t in sorted(per_type):
        s = per_type[t]
        print(f"{t:<18}{s['ARMED']:>7}{s['STRUCTURE_CONFIRMED']:>9}"
              f"{s['INVALIDATED']:>9}{s['EXPIRED']:>9}{s['VETOED']:>8}")

    if not scored:
        print("\nNo confirmed events to score. That is a result, not a failure: this engine is built "
              "to stay silent unless price closes through structure.")
        return 0

    print(f"\n{'what happened after each confirmation':<42}")
    print(f"{'day':<12}{'time':>7}{'type':>18}{'side':>7}{'risk':>7}{'MFE':>8}{'MAE':>8}{'R':>7}{'2R?':>5}")
    for s in scored:
        print(f"{s['day']:<12}{s['time']:>7}{s['type']:>18}{s['side']:>7}"
              f"{s['risk']:>7.0f}{s['mfe']:>8.0f}{s['mae']:>8.0f}{s['r']:>7.2f}"
              f"{'yes' if s['hit2r'] else 'no':>5}")

    n = len(scored)
    hit = sum(1 for s in scored if s["hit2r"])
    stop = sum(1 for s in scored if s["stopped_first"])
    exp = sum(s["realized"] for s in scored) / n
    print(f"\nconfirmed signals : {n}")
    print(f"reached 2R        : {hit}  ({hit / n * 100:.0f}%)")
    print(f"stopped first     : {stop}  ({stop / n * 100:.0f}%)")
    print(f"median R (MFE)    : {sorted(s['r'] for s in scored)[n // 2]:.2f}   <- upper bound only")
    print(f"median R (fixed)  : {sorted(s['realized'] for s in scored)[n // 2]:.2f}   <- 2R target / 1R stop")
    print(f"expectancy (fixed): {exp:+.2f}R per signal")
    if dropped_early:
        print(f"dropped pre-gate  : {dropped_early} (armed inside the regime window)")
    if late_dropped:
        print(f"dropped late      : {late_dropped} (confirmed at or after {a.cutoff})")

    # Is the edge the method, or is it two good afternoons?
    #
    # An expectancy carried by a handful of trades is not an edge, it is a sample that happened to
    # contain a trend. The test: drop the best signals, and drop the single best SESSION, and see
    # whether anything is left. If expectancy goes negative without its top three, the honest
    # statement is "this did not lose money over 35 sessions", not "this makes money".
    print("\nedge concentration (does this survive losing its best trades?)")
    ranked = sorted(scored, key=lambda s: s["realized"], reverse=True)
    for drop in (1, 3, 5):
        if n > drop:
            rest = ranked[drop:]
            print(f"  excluding best {drop:>2}      : {sum(s['realized'] for s in rest) / len(rest):+.2f}R "
                  f"over {len(rest)} signals")
    by_session = defaultdict(float)
    for s in scored:
        by_session[s["day"]] += s["realized"]
    if by_session:
        best_day = max(by_session, key=lambda k: by_session[k])
        rest = [s for s in scored if s["day"] != best_day]
        print(f"  best session       : {best_day} contributed {by_session[best_day]:+.2f}R total")
        if rest:
            print(f"  excluding it       : {sum(s['realized'] for s in rest) / len(rest):+.2f}R "
                  f"over {len(rest)} signals")
    rev = [s for s in scored if s["type"].startswith("REGULAR")]
    hid = [s for s in scored if s["type"].startswith("HIDDEN")]
    for name, grp in (("reversal (regular)", rev), ("continuation (hidden)", hid)):
        if grp:
            g = len(grp)
            gh = sum(1 for s in grp if s["hit2r"])
            print(f"{name:<22}: {g:>3} signals, {gh} reached 2R ({gh / g * 100:.0f}%), "
                  f"median MFE R {sorted(s['r'] for s in grp)[g // 2]:.2f}, "
                  f"expectancy {sum(s['realized'] for s in grp) / g:+.2f}R")
    print("\nMFE is the best each trade ever LOOKED, not an exit you could take. Judge this engine on "
          "the fixed-rule expectancy line; MFE is here only to bound the best case.")
    print("A small sample is still a small sample. Read the per-type split before the headline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
