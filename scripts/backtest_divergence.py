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
    mfe = mae = 0.0
    hit2r = stopped = False
    for b in after:
        fav = (entry - float(b["l"])) if short else (float(b["h"]) - entry)
        adv = (float(b["h"]) - entry) if short else (entry - float(b["l"]))
        mfe, mae = max(mfe, fav), max(mae, adv)
        if not stopped and adv >= risk:
            stopped = True
        if not stopped and fav >= 2 * risk:
            hit2r = True
            break
    return {"mfe": mfe, "mae": mae, "r": mfe / risk, "risk": risk,
            "hit2r": hit2r, "stopped_first": stopped and not hit2r}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="3m", choices=list(rd.BAR_SECONDS))
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--sec", default=NIFTY_SEC)
    ap.add_argument("--regime-gate", action="store_true",
                    help="apply the day-regime veto using each session's own net direction")
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

    for d in sorted(by_day):
        m1 = by_day[d]
        bars = rd.aggregate_1m(m1, step, m1[-1]["ts"] + step)
        if len(bars) < 30:
            continue
        # A crude but causal stand-in for the desk's cumulative Net PE-CE regime: the direction the
        # session had actually taken by its midpoint. Only used when --regime-gate is passed.
        regime = "none"
        if a.regime_gate:
            mid = bars[len(bars) // 2]["c"]
            move = mid - bars[0]["c"]
            regime = "bull" if move > 60 else "bear" if move < -60 else "none"
        res = rd.compute_rsi_divergence(bars, tf=a.tf, day_regime=regime)
        if not res.get("ok"):
            continue
        evs = res["events"]
        for e in evs:
            per_type[e["type"]][e["state"]] += 1
            if e["state"] == "STRUCTURE_CONFIRMED":
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
    print(f"\nconfirmed signals : {n}")
    print(f"reached 2R        : {hit}  ({hit / n * 100:.0f}%)")
    print(f"stopped first     : {stop}  ({stop / n * 100:.0f}%)")
    print(f"median R (MFE)    : {sorted(s['r'] for s in scored)[n // 2]:.2f}")
    rev = [s for s in scored if s["type"].startswith("REGULAR")]
    hid = [s for s in scored if s["type"].startswith("HIDDEN")]
    for name, grp in (("reversal (regular)", rev), ("continuation (hidden)", hid)):
        if grp:
            g = len(grp)
            gh = sum(1 for s in grp if s["hit2r"])
            print(f"{name:<22}: {g:>3} signals, {gh} reached 2R ({gh / g * 100:.0f}%), "
                  f"median R {sorted(s['r'] for s in grp)[g // 2]:.2f}")
    print("\nA small sample is still a small sample. Read the per-type split before the headline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
