"""
backtest_olab_pro.py — does the ★ PRO trade filter help? Tested on YOUR stored sessions, no look-ahead.

Every 15 minutes from 09:45 to 14:45 on each stored day, the filter is computed from the snapshots up to that moment,
then NIFTY's move over the next 30 minutes is measured.  For CE grades a rise counts as right, for PE grades a fall.
If the filter works, ✅ FAVOURED should beat ⚠ CAUTION, which should beat ⛔ AVOID — and FAVOURED should beat 50 %.

  .venv/bin/python backtest_olab_pro.py              # all stored days
  .venv/bin/python backtest_olab_pro.py --days 30
Writes data/backtest_olab_pro.csv. Trust it only with ≥ 30 cases per grade, then re-check on 20 new sessions.
"""
from __future__ import annotations

import argparse
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

import config
import olab_pro as op
import options_lab_tab as ol
import store


def one_day(d, mult):
    snaps = store.load_day(d)
    if len(snaps) < 60:
        return []
    P = op.panel(snaps, mult)
    times = sorted(P["ts"].unique())
    spot = P.groupby("ts")["spot"].first()
    out = []
    for hh, mm in [(h, m) for h in range(9, 15) for m in (0, 15, 30, 45)]:
        t = pd.Timestamp(times[0]).normalize() + pd.Timedelta(hours=hh, minutes=mm)
        if not (time(9, 45) <= t.time() <= time(14, 45)):
            continue
        idx = [i for i, x in enumerate(times) if x <= t]
        if len(idx) < 20:
            continue
        i = idx[-1]
        fut = spot[spot.index >= times[i] + pd.Timedelta(minutes=30)]
        if fut.empty:
            continue
        a = op.assess(snaps[: i + 1], mult, p=P[P["ts"] <= times[i]], with_iv=False)
        if not a:
            continue
        mv = float(fut.iloc[0] - spot.iloc[i])
        for side in ("CE", "PE"):
            g = a["filter"][side]["grade"]
            out.append(dict(day=d, ts=times[i], side=side, grade=g, move30=mv,
                            right=(mv > 0) if side == "CE" else (mv < 0), signed=mv if side == "CE" else -mv,
                            regime30=a["b30"].iloc[-1]["regime"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0)
    a = ap.parse_args()
    days = sorted(ol._days_available())
    if a.days:
        days = days[-a.days:]
    mult = float(ol._rules()["oi_multiplier"])
    rows = []
    for k, d in enumerate(days, 1):
        try:
            rows += one_day(d, mult)
        except Exception as exc:                                # noqa: BLE001
            print(f"\n  {d}: skipped ({exc})")
        print(f"  {k}/{len(days)} {d}", end="\r")
    if not rows:
        print("no usable sessions (need ≥ 60 snapshots per day)"); return
    df = pd.DataFrame(rows)
    out = Path(config.DATA_DIR) / "backtest_olab_pro.csv"
    df.to_csv(out, index=False)
    print(f"\n{df['day'].nunique()} sessions · {len(df) // 2} checkpoints · saved {out}\n")
    s = (df.groupby("grade").agg(n=("right", "size"), hit=("right", "mean"), avg_pts=("signed", "mean"))
         .assign(hit=lambda x: (100 * x["hit"]).round(1), avg_pts=lambda x: x["avg_pts"].round(1)))
    print("Grade → was the 30-min move in the graded direction?")
    print(s.to_string())
    print("\nBy writers regime (30 m) — does following the writers pay?")
    r = df[df["side"] == "CE"].groupby("regime30").agg(n=("move30", "size"), avg_move=("move30", "mean"),
                                                         up=("move30", lambda v: 100 * (v > 0).mean())).round(1)
    print(r.to_string())
    print("\nIf FAVOURED is not clearly better than CAUTION/AVOID (and > 50 %), the filter is NOT an edge — "
          "use it only as a checklist, not a signal.")


if __name__ == "__main__":
    main()
