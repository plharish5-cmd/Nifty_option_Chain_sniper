"""
backtest_turtle_soup.py — do "fake breakouts" (stop-hunts) on NIFTY actually reverse?

An event = price wicks 2-10 points PAST a liquidity pool (previous-day high/low, opening range, swing, relative
equal highs/lows) and the 5-minute candle CLOSES back inside (turtle_soup.py). For every event this measures the
NIFTY move 15 / 30 / 60 minutes after the candle closed, SIGNED in the reversal direction
(a fake breakout above → down is a win; a fake breakdown below → up is a win).

Controls: the same number of RANDOM 5-minute closes at the same time of day (30-min bucket, any session; 1000 shuffles), same signs.
PASS = positive average, p < 0.05 vs random, n >= 30, positive in BOTH halves of the sample.
Also shown: the breaches that CLOSED BEYOND the pool (a "real" breakout) so you can see fake vs real side by side,
and the split with / without SMT (Bank Nifty did not confirm the sweep) when Bank Nifty history is stored.

  .venv/bin/python backtest_turtle_soup.py                    # all stored sessions
  .venv/bin/python backtest_turtle_soup.py --fetch-bn 730     # first download Bank Nifty history (for the SMT split)
  .venv/bin/python backtest_turtle_soup.py --before 2025-10-03 --tag train   /   --after 2025-10-03 --tag test
Output: data/backtests/turtle_soup[<tag>].json and turtle_soup[<tag>]_events.csv   (Evidence Board reads turtle_soup.json)
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
import backtest_five_rule as fr          # noqa: E402
import ict_model as im                   # noqa: E402
import turtle_soup as ts                 # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"


class Prices:
    """1-minute closes per session → price at a clock time (the bar that closed just before it, no look-ahead)."""

    def __init__(self, m1: pd.DataFrame):
        self.d = {}
        for day, g in m1.groupby(m1["timestamp"].dt.date):
            self.d[str(day)] = (g["timestamp"].to_numpy("datetime64[ns]"), g["close"].to_numpy(float))

    def at(self, day: str, when: pd.Timestamp):
        ts_, cl = self.d.get(day, (None, None))
        if ts_ is None:
            return np.nan
        target = np.datetime64(when - pd.Timedelta(minutes=1))
        j = int(np.searchsorted(ts_, target, side="right")) - 1
        if j < 0 or (target - ts_[j]) > np.timedelta64(2, "m") or when - pd.Timedelta(minutes=1) > pd.Timestamp(ts_[-1]):
            return np.nan
        return float(cl[j])


def forward(px: Prices, day: str, t: pd.Timestamp, h: int) -> float:
    a, b = px.at(day, t), px.at(day, t + pd.Timedelta(minutes=h))
    return b - a if not (np.isnan(a) or np.isnan(b)) else np.nan


def window_of(t: pd.Timestamp, P: dict) -> str:
    for name, (a, b) in P["windows"].items():
        if im._hm(a) <= t.time() < im._hm(b):
            return name
    return "OUT"


def bucket(t: pd.Timestamp) -> int:
    return (t.hour * 60 + t.minute) // 30


def build_pool(d5: pd.DataFrame, px: Prices, horizons, days_ok) -> dict:
    """Raw (unsigned) forward move after EVERY 5m close, grouped by 30-minute time-of-day bucket across all sessions.
    The control draws from the same bucket on ANY session. It deliberately does not stay on the event's own day: a day that
    trended up is the day that sweeps highs, so a same-day control would be biased against reversals (we saw this on pure
    random-walk data)."""
    pool = {h: {} for h in horizons}
    for day, g in d5.groupby(d5["timestamp"].dt.date):
        if day not in days_ok:
            continue
        for t in g["timestamp"]:
            tc = t + pd.Timedelta(minutes=5)
            for h in horizons:
                m = forward(px, str(day), tc, h)
                if not np.isnan(m):
                    pool[h].setdefault(bucket(tc), []).append(m)
    return {h: {k: np.array(v, float) for k, v in d.items()} for h, d in pool.items()}


def analyse(ev: pd.DataFrame, pool: dict, h: int, reps: int, seed=7, min_n=30, p_max=0.05) -> dict:
    col = f"m{h}"
    g = ev.dropna(subset=[col])
    keep = np.array([bucket(t) in pool[h] for t in g["t"]], bool) if len(g) else np.array([], bool)
    g = g[keep]
    n = len(g)
    if n == 0:
        return dict(n=0, passed=False)
    sg = g["sgn"].to_numpy(float)
    signed = sg * g[col].to_numpy(float)
    real = float(signed.mean())
    rng = np.random.default_rng(seed)
    bk = np.array([bucket(t) for t in g["t"]])
    null = np.zeros(reps)
    for b_ in np.unique(bk):
        m = bk == b_
        arr = pool[h][int(b_)]
        draws = arr[(rng.random((reps, int(m.sum()))) * len(arr)).astype(int)]
        null += (draws * sg[m]).sum(axis=1)
    null /= n
    p = float(((null >= real).sum() + 1) / (reps + 1))
    days_sorted = sorted(g["day"].unique())
    half = days_sorted[len(days_sorted) // 2]
    h1, h2 = g[g["day"] < half], g[g["day"] >= half]
    m1_, m2_ = (float((h1["sgn"] * h1[col]).mean()) if len(h1) else np.nan,
                float((h2["sgn"] * h2[col]).mean()) if len(h2) else np.nan)
    ok = bool(n >= min_n and real > 0 and p < p_max and m1_ > 0 and m2_ > 0)
    return dict(n=int(n), avg_pts=round(real, 2), hit=round(float((signed > 0).mean()), 3), null_avg=round(float(null.mean()), 2),
                p=round(p, 4), half1=None if np.isnan(m1_) else round(m1_, 2), half2=None if np.isnan(m2_) else round(m2_, 2),
                passed=ok)


def row(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:<34}{0:>5}   —"
    v = "✔ EDGE" if s["passed"] else ("too few" if s["n"] < 30 else "✘ no edge")
    h1, h2 = s.get("half1"), s.get("half2")
    hv = f"{h1:+.1f}/{h2:+.1f}" if h1 is not None and h2 is not None else "—"
    return (f"  {name:<34}{s['n']:>5}{s['avg_pts']:>+9.1f}{100 * s['hit']:>7.0f}%{s['null_avg']:>+9.1f}{s['p']:>8.3f}  {hv:<13}{v}")


HEAD = f"  {'':<34}{'n':>5}{'avg pts':>9}{'hit':>8}{'random':>9}{'p':>8}  {'halves':<13}verdict"


def main():
    import runlog
    runlog.start("backtest_turtle_soup")
    P = im.rules()
    T = P["turtle_soup"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=0, help="download N days of NIFTY 1m from Dhan first")
    ap.add_argument("--fetch-bn", type=int, default=0, help="download N days of Bank Nifty 1m from Dhan first (SMT split)")
    ap.add_argument("--reps", type=int, default=int(P["validation"]["reps"]))
    ap.add_argument("--after", default=None)
    ap.add_argument("--before", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--pools", default="", help="comma list overriding the pools tested, e.g. open  (Episode-1 fake move through the day open)")
    a = ap.parse_args()
    if a.pools:
        T["liquidity"] = [x.strip() for x in a.pools.split(",") if x.strip()]
    after = pd.Timestamp(a.after).date() if a.after else None
    before = pd.Timestamp(a.before).date() if a.before else None
    if a.fetch:
        print(f"saved {fr.fetch_history(a.fetch)} NIFTY session file(s)")
    import bn_history as bn
    if a.fetch_bn:
        print(f"saved {bn.fetch_history(a.fetch_bn)} Bank Nifty session file(s)")
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles. Run with --fetch 730 first.")
    bn1 = bn.load()
    days_all = sorted(m1["timestamp"].dt.date.unique())
    sel = [d for d in days_all if fr.in_range(d, after, before)]
    print(f"{len(days_all)} NIFTY sessions stored · testing {len(sel)} ({sel[0]} → {sel[-1]})"
          f" · Bank Nifty sessions stored: {bn1['timestamp'].dt.date.nunique() if len(bn1) else 0}")
    if len(sel) < 40:
        sys.exit("Need at least 40 sessions for an honest test.")
    d5 = im.resample(m1, int(P["tf_minutes"]))
    r5 = im.resample(bn1, int(P["tf_minutes"])) if len(bn1) else None
    evs = [e for e in ts.events(d5, P, r5) if fr.in_range(pd.Timestamp(e["day"]).date(), after, before)]
    px = Prices(m1)
    H = [int(x) for x in T["horizons_min"]]
    ph = int(T["primary_horizon_min"])
    rows = []
    for e in evs:
        r = dict(day=e["day"], t=e["t"], side=e["side"], sgn=1 if e["side"] == "BULLISH" else -1, kind=e["kind"], level=e["level"],
                 depth=e["depth"], close_inside=e["close_inside"], smt=e["smt"], window=window_of(e["t"], P))
        for h in H:
            r[f"m{h}"] = forward(px, e["day"], e["t"], h)
        rows.append(r)
    ev = pd.DataFrame(rows)
    if ev.empty:
        sys.exit("No events found.")
    pool = build_pool(d5, px, H, set(sel))
    lo, hi = float(T["min_pts"]), float(T["max_pts"])
    prim = ev[ev["close_inside"] & (ev["depth"] >= lo) & (ev["depth"] <= hi)]
    brk = ev[~ev["close_inside"] & (ev["depth"] >= lo) & (ev["depth"] <= hi)]
    print(f"{len(ev)} sweep events ({int(ev['close_inside'].sum())} closed back inside / {int((~ev['close_inside']).sum())} closed beyond) · "
          f"primary set (depth {lo:g}-{hi:g} pts, closed inside): {len(prim)} events on {prim['day'].nunique()} sessions\n")
    V = P["validation"]
    out = dict(generated=datetime.now().isoformat(timespec="seconds"), tag=a.tag, sessions=len(sel), events=len(ev),
               **{"from": str(sel[0]), "to": str(sel[-1])}, primary_horizon_min=ph, depth=[lo, hi],
               bn_sessions=int(bn1["timestamp"].dt.date.nunique()) if len(bn1) else 0)
    print(f"PRIMARY: fake breakout/breakdown, depth {lo:g}-{hi:g} pts, closed inside, {ph} min after the close, {a.reps} random reps")
    print(HEAD)
    head = analyse(prim, pool, ph, a.reps, min_n=int(V["min_trades"]), p_max=float(V["p_max"]))
    print(row("ALL pools", head))
    out["headline"] = head
    small = max(200, a.reps // 3)
    out["horizons"], out["kinds"], out["sides"], out["depths"], out["windows"], out["smt"] = {}, {}, {}, {}, {}, {}
    print("\n  by horizon")
    for h in H:
        s = analyse(prim, pool, h, small, min_n=int(V["min_trades"]))
        out["horizons"][str(h)] = s
        print(row(f"  {h} min after the close", s))
    print("  by pool")
    for k, g in prim.groupby("kind"):
        s = analyse(g, pool, ph, small, min_n=int(V["min_trades"]))
        out["kinds"][k] = s
        print(row(f"  {k}", s))
    print("  by direction")
    for sd, g in prim.groupby("side"):
        s = analyse(g, pool, ph, small, min_n=int(V["min_trades"]))
        out["sides"][sd] = s
        print(row("  " + ("fake breakDOWN → up (buy)" if sd == "BULLISH" else "fake breakOUT → down (sell)"), s))
    print("  by sweep depth (closed inside)")
    for lab, (x, y) in {"2-10 pts": (lo, hi), "10-25 pts": (hi, 25.0), "25+ pts": (25.0, 1e9)}.items():
        g = ev[ev["close_inside"] & (ev["depth"] > x if lab != "2-10 pts" else ev["depth"] >= x) & (ev["depth"] <= y)]
        s = analyse(g, pool, ph, small, min_n=int(V["min_trades"]))
        out["depths"][lab] = s
        print(row("  " + lab, s))
    print("  by time of day")
    for w, g in prim.groupby("window"):
        s = analyse(g, pool, ph, small, min_n=int(V["min_trades"]))
        out["windows"][w] = s
        print(row("  " + w, s))
    print("  fake vs REAL breakout (same sign convention: positive = reversed)")
    s = analyse(brk, pool, ph, small, min_n=int(V["min_trades"]))
    out["closed_beyond"] = s
    print(row("  closed BEYOND the pool", s))
    if len(bn1):
        print("  SMT (Bank Nifty did not confirm the sweep)")
        for lab, g in (("SMT present", prim[prim["smt"] == True]), ("no SMT", prim[prim["smt"] == False])):   # noqa: E712
            s = analyse(g, pool, ph, small, min_n=int(V["min_trades"]))
            out["smt"][lab] = s
            print(row("  " + lab, s))
    nt = 1 + len(H) + len(out["kinds"]) + len(out["sides"]) + len(out["depths"]) + len(out["windows"]) + 3
    print(f"\n  ~{nt} cuts shown, so about {0.05 * nt:.1f} would show p<0.05 by luck alone — trust the PRIMARY line and anything that repeats "
          f"in the untouched year.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"_{a.tag}" if a.tag else ""
    ev.assign(t=ev["t"].astype(str)).to_csv(OUT_DIR / f"turtle_soup{tag}_events.csv", index=False)

    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.bool_, bool)):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, (np.floating, float)):
            return None if np.isnan(o) else float(o)
        return o
    (OUT_DIR / f"turtle_soup{tag}.json").write_text(json.dumps(clean(out), indent=2))
    print(f"Saved {OUT_DIR / f'turtle_soup{tag}.json'}. Information only — not financial advice.")


if __name__ == "__main__":
    main()
