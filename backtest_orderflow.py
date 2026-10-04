"""
backtest_orderflow.py — do the NIFTY 50 order-flow signals actually predict the next move?

For every recorded session (data/orderflow/, written by orderflow_recorder.py) and every signal type
(stacked imbalances, absorption, delta divergence, CVD divergence), it measures the NIFTY 50 index move
over the next N minutes AFTER the signal candle closed, signed in the signal's direction
(bullish signal → up is a win). The control is the same number of RANDOM candle times on the same days
(1000 shuffles). A signal type passes only if it beats random (p < 0.05), is positive on average,
has ≥ 30 events, and is positive in both halves of the recorded days.

Run (from the OC folder, after ~20+ recorded sessions):
  .venv/bin/python backtest_orderflow.py            # 5-min candles, 15-min horizon (orderflow_rules.json)
  .venv/bin/python backtest_orderflow.py --tf 3 --horizon 10
Writes data/backtests/orderflow.json — the Order Flow tab then labels each signal VALIDATED / FAILED.
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import nifty_orderflow as nf                                   # noqa: E402

TESTED = ["stacked_buy", "stacked_sell", "absorb_bull", "absorb_bear", "div_bull", "div_bear",
          "cvd_div_bull", "cvd_div_bear", "surge_bull", "surge_bear", "absorb_zone_bull", "absorb_zone_bear",
          "fade_buying", "fade_selling"]


def main():
    import runlog
    runlog.start("backtest_orderflow")
    allr = json.loads(nf.RULES_PATH.read_text()) if nf.RULES_PATH.exists() else {}
    V = allr.get("validation", {})
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=int(V.get("horizon_min", 15)))
    ap.add_argument("--reps", type=int, default=int(V.get("reps", 1000)))
    a = ap.parse_args()
    days = nf.recorded_days()
    need = int(V.get("min_sessions", 20))
    print(f"Recorded sessions: {len(days)}" + (f" ({days[0]} → {days[-1]})" if days else ""))
    if len(days) < need:
        sys.exit(f"Need at least {need} recorded sessions for an honest test — keep orderflow_recorder.py running daily.")

    R = nf.rules()
    rows, pool = [], []                      # pool = every candle close that has a full horizon ahead
    setups, cands = [], {}                   # Delta + VWAP setup trades, candles per day
    for d in days:
        data = nf.load_day(d)
        f = nf.compute(data, a.tf, R, day=d)
        if f is None or f.candles.empty:
            continue
        cands[d] = f.candles.reset_index(drop=True)
        if len(f.trades):
            setups += [dict(day=d, **r) for r in f.trades.to_dict("records")]
        ix = data["index"] if len(data["index"]) else data["ticks"][["t", "idx"]].rename(columns={"idx": "ltp"})
        s = ix.set_index("t")["ltp"].sort_index()
        s = s[~s.index.duplicated(keep="last")]

        def px_at(ts):
            j = s.index.searchsorted(ts, side="right") - 1
            return float(s.iloc[j]) if j >= 0 else np.nan
        last_t = s.index.max()
        for i, cr in f.candles.iterrows():
            close_t = cr["t"] + pd.Timedelta(minutes=a.tf)
            end_t = close_t + pd.Timedelta(minutes=a.horizon)
            if end_t > last_t:
                continue
            mv = px_at(end_t) - px_at(close_t)
            pool.append(dict(day=d, i=i, move=mv))
        mv_map = {(p["day"], p["i"]): p["move"] for p in pool if p["day"] == d}
        for e in f.events.itertuples():
            if e.kind not in TESTED or (d, e.i) not in mv_map:
                continue
            sgn = 1 if e.kind in nf.BULLISH else -1
            rows.append(dict(day=d, i=e.i, kind=e.kind, sgn=sgn, move=mv_map[(d, e.i)], signed=sgn * mv_map[(d, e.i)]))
    ev, pool = pd.DataFrame(rows), pd.DataFrame(pool)
    print(f"{len(pool)} candle closes scored · {len(ev)} signal events · horizon {a.horizon} min after a {a.tf}-min candle\n")
    rng = np.random.default_rng(17)
    by_day = {d: g["move"].to_numpy() for d, g in pool.groupby("day")}
    half = days[len(days) // 2]
    out = dict(generated=datetime.now().isoformat(timespec="seconds"), sessions=len(days), tf=a.tf,
               horizon_min=a.horizon, signals={})
    print(f"{'signal':<28}{'n':>5}{'avg pts':>9}{'hit %':>8}{'random':>9}{'p':>8}  halves        verdict")
    for k in TESTED:
        g = ev[ev["kind"] == k] if len(ev) else ev
        n = len(g)
        if n == 0:
            print(f"{nf.SIGNALS[k][0]:<28}{0:>5}   no events")
            out["signals"][k] = dict(n=0, passed=False)
            continue
        real = float(g["signed"].mean())
        sgn = int(g["sgn"].iloc[0])
        cnt = g.groupby("day").size()
        null = np.empty(a.reps)
        for r in range(a.reps):
            null[r] = np.mean(np.concatenate([sgn * rng.choice(by_day[d], c) for d, c in cnt.items()]))
        p = float(((null >= real).sum() + 1) / (a.reps + 1))
        h1, h2 = g[g["day"] < half]["signed"].mean(), g[g["day"] >= half]["signed"].mean()
        ok = bool(n >= 30 and real > 0 and p < float(V.get("p_max", 0.05)) and h1 > 0 and h2 > 0)
        out["signals"][k] = dict(n=n, avg_pts=round(real, 2), hit=round(float((g["signed"] > 0).mean()), 3),
                                 null_avg=round(float(null.mean()), 2), p=round(p, 4),
                                 half1=None if np.isnan(h1) else round(float(h1), 2),
                                 half2=None if np.isnan(h2) else round(float(h2), 2), passed=ok)
        verdict = "✔ EDGE" if ok else ("too few" if n < 30 else "✘ no edge")
        print(f"{nf.SIGNALS[k][0]:<28}{n:>5}{real:>+9.1f}{100 * (g['signed'] > 0).mean():>7.0f}%{null.mean():>+9.1f}{p:>8.3f}  "
              f"{(h1 if not np.isnan(h1) else 0):+.1f} / {(h2 if not np.isnan(h2) else 0):+.1f}   {verdict}")
    nf.VALIDATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ── Delta + VWAP setup as a strategy ─────────────────────────────────
    st = pd.DataFrame(setups)
    S = R["setup"]
    exit_at = nf._hm(S["exit_at"])
    stop_new = nf._hm(S["no_new_after"])
    print("\nDELTA + VWAP SETUP (buy above VWAP on positive 5-candle delta, sell below on negative) — as a full strategy")
    if st.empty:
        print("  no trades")
        out["setup"] = dict(n=0, passed=False)
    else:
        real = float(st["R"].mean())
        nt, ns = np.empty(a.reps), np.empty(a.reps)
        recs = list(st.itertuples())
        for r in range(a.reps):
            ra, rb = [], []
            for t_ in recs:
                c = cands[t_.day]
                side = 1 if t_.side == "LONG" else -1
                risk = abs(t_.entry - t_.sl)
                ok_i = [k for k in range(1, len(c)) if c["t"].iloc[k].time() < stop_new]
                k = int(rng.choice(ok_i))
                e = float(c["o"].iloc[k])
                px, _, _ = nf.simulate(c, k, side, e, e - side * risk, e + side * float(S["rr"]) * risk, exit_at)
                ra.append(side * (px - e) / risk)
                s2 = 1 if rng.random() < 0.5 else -1
                j = int(c.index[c["t"] == t_.entry_t][0]) if (c["t"] == t_.entry_t).any() else k
                px, _, _ = nf.simulate(c, j, s2, t_.entry, t_.entry - s2 * risk, t_.entry + s2 * float(S["rr"]) * risk, exit_at)
                rb.append(s2 * (px - t_.entry) / risk)
            nt[r], ns[r] = np.mean(ra), np.mean(rb)
        p_t = float(((nt >= real).sum() + 1) / (a.reps + 1))
        p_s = float(((ns >= real).sum() + 1) / (a.reps + 1))
        h1, h2 = st[st["day"] < half]["R"].mean(), st[st["day"] >= half]["R"].mean()
        eq = st["R"].cumsum()
        ok = bool(len(st) >= 30 and real > 0 and p_t < 0.05 and p_s < 0.05 and (h1 or 0) > 0 and (h2 or 0) > 0)
        out["setup"] = dict(n=int(len(st)), win=round(float((st["R"] > 0).mean()), 3), avg_R=round(real, 3),
                            total_R=round(float(st["R"].sum()), 1), max_dd_R=round(float((eq.cummax() - eq).max()), 1),
                            random_time=round(float(nt.mean()), 3), p_time=round(p_t, 4),
                            random_side=round(float(ns.mean()), 3), p_side=round(p_s, 4),
                            half1=None if np.isnan(h1) else round(float(h1), 3),
                            half2=None if np.isnan(h2) else round(float(h2), 3), passed=ok)
        print(f"  {len(st)} trades · win {100 * (st['R'] > 0).mean():.0f}% · avg {real:+.2f}R · total {st['R'].sum():+.1f}R · "
              f"maxDD {out['setup']['max_dd_R']:.1f}R")
        print(f"  random time {nt.mean():+.2f}R (p={p_t:.3f}) · random side {ns.mean():+.2f}R (p={p_s:.3f}) · "
              f"halves {(h1 if not np.isnan(h1) else 0):+.2f} / {(h2 if not np.isnan(h2) else 0):+.2f}R → "
              f"{'✔ EDGE' if ok else ('too few' if len(st) < 30 else '✘ no edge')}")
        st.to_csv(nf.VALIDATION_PATH.parent / "orderflow_setup_trades.csv", index=False)
    nf.VALIDATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    nf.VALIDATION_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nSaved {nf.VALIDATION_PATH}. With {len(TESTED)} signal types tested, expect ~{0.05 * len(TESTED):.1f} to pass by luck at p<0.05 —")
    print("trust a signal more if it also passes again after 20 MORE sessions. Information only — not financial advice.")


if __name__ == "__main__":
    main()
