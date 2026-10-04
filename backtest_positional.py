"""
backtest_positional.py — does "breakout from a consolidation inside an uptrend" work on the NIFTY 50 stocks?

Event: the BREAKOUT rule in positional_rules.json (tight 30-day box, uptrend intact the day before, close above the box on volume).
Trade: buy the next open · stop = box low · target 2R · exit after 60 sessions · 0.2% cost.  Result in R and in % after 20/40/60 sessions.

Controls (same stock, same stop distance in %, same exit rules):
  A  a RANDOM date on the same stock
  B  a random date on the same stock when the uptrend rule was already true  (isolates the box breakout from "it was trending")
PASS = n >= 30 events, avg R > 0, p < 0.05 vs BOTH controls, and both halves of the period positive.
Caveats printed with the result: today's NIFTY 50 are past winners (survivorship) — the controls use the same stocks so the
comparison stays fair, but absolute returns are flattered; breakouts cluster in time, so n overstates independent evidence.

  .venv/bin/python positional_scan.py --fetch            # first: download the daily history
  .venv/bin/python backtest_positional.py
  .venv/bin/python backtest_positional.py --before 2023-01-01 --tag train   /   --after 2023-01-01 --tag test
Output: data/backtests/positional[<tag>].json and positional[<tag>]_events.csv.  Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import positional_scan as ps             # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
BIG = 10 ** 9


class Stock:
    def __init__(self, sym: str, df: pd.DataFrame, R: dict):
        self.sym, self.R = sym, R
        self.d = ps.prep(df, R)
        self.o, self.h, self.l, self.c = (self.d[x].to_numpy(float) for x in ("Open", "High", "Low", "Close"))
        self.date = self.d["Date"].to_numpy("datetime64[ns]")
        self.trend_prev = self.d["trend"].shift(1).fillna(False).to_numpy(bool)
        self.N = len(self.d)

    def valid(self, j: int) -> bool:
        return j >= int(self.R["warmup_days"]) and j + 1 + int(self.R["max_hold_days"]) <= self.N

    def sim(self, j: int, risk_pct: float, tgt_r: float, stop: float = None):
        """Enter at the open of day j+1. Returns (R, fwd20, fwd40, fwd60) or None."""
        R, H = self.R, int(self.R["max_hold_days"])
        e = j + 1
        if e + H > self.N:
            return None
        entry = self.o[e]
        stop = entry * (1 - risk_pct / 100) if stop is None else stop
        risk = entry - stop
        if risk <= 0:
            return None
        tgt = entry + tgt_r * risk
        lo, hi = self.l[e:e + H], self.h[e:e + H]
        i_sl = int(np.argmax(lo <= stop)) if (lo <= stop).any() else BIG
        i_tp = int(np.argmax(hi >= tgt)) if (hi >= tgt).any() else BIG
        if i_sl <= i_tp and i_sl < BIG:
            exit_ = min(self.o[e + i_sl], stop)
        elif i_tp < BIG:
            exit_ = tgt
        else:
            exit_ = self.c[e + H - 1]
        r = (exit_ - entry) / risk - float(R["cost_pct"]) / 100 * entry / risk
        f = [(self.c[e + k - 1] / entry - 1) * 100 for k in (20, 40, 60)]
        return (r, f[0], f[1], f[2])


def find_events(stocks: dict, R: dict, need_volume=True, setup_only=False, after=None, before=None) -> list:
    out = []
    cool = int(R["cooldown_days"])
    for sym, S in stocks.items():
        flag = ps.breakout_flag(S.d, R, need_volume)
        if setup_only:
            flag &= ps.setup_flag(S.d, R).shift(1).fillna(False).astype(bool)
        idx = np.where(flag.to_numpy(bool))[0]
        last = -10 ** 9
        for t in idx:
            if t - last < cool or not S.valid(t):
                continue
            day = pd.Timestamp(S.date[t]).date()
            if (after and day < after) or (before and day >= before):
                continue
            stop = float(S.d["box_lo"].iloc[t])
            entry = S.o[t + 1]
            if entry <= stop:
                continue
            risk_pct = (entry - stop) / entry * 100
            if risk_pct > float(R["max_risk_pct"]):
                continue
            last = t
            out.append(dict(sym=sym, t=t, day=str(day), stop=stop, risk_pct=risk_pct))
    return out


def evaluate(events: list, stocks: dict, tgt_r: float) -> pd.DataFrame:
    rows = []
    for e in events:
        S = stocks[e["sym"]]
        x = S.sim(e["t"], e["risk_pct"], tgt_r, e["stop"])
        if x is None:
            continue
        rows.append(dict(e, R=x[0], f20=x[1], f40=x[2], f60=x[3]))
    return pd.DataFrame(rows)


def controls(ev: pd.DataFrame, stocks: dict, tgt_r: float, reps: int, seed=5):
    rng = np.random.default_rng(seed)
    cand_a, cand_b = {}, {}
    for sym in ev["sym"].unique():
        S = stocks[sym]
        js = np.array([j for j in range(int(S.R["warmup_days"]), S.N) if S.valid(j)])
        cand_a[sym] = js
        cand_b[sym] = js[S.trend_prev[js]] if len(js) else js
    a_r, a_f, b_r, b_f = (np.empty(reps) for _ in range(4))
    rows = list(ev.itertuples())
    for r in range(reps):
        ra, fa, rb, fb = [], [], [], []
        for e in rows:
            S = stocks[e.sym]
            for cand, rr, ff in ((cand_a[e.sym], ra, fa), (cand_b[e.sym], rb, fb)):
                if len(cand):
                    x = S.sim(int(rng.choice(cand)), e.risk_pct, tgt_r)
                    if x:
                        rr.append(x[0])
                        ff.append(x[2])
        a_r[r], a_f[r] = np.mean(ra) if ra else np.nan, np.mean(fa) if fa else np.nan
        b_r[r], b_f[r] = np.mean(rb) if rb else np.nan, np.mean(fb) if fb else np.nan
    return a_r, a_f, b_r, b_f


def analyse(ev: pd.DataFrame, stocks: dict, tgt_r: float, reps: int, min_n=30, p_max=0.05) -> dict:
    if ev.empty:
        return dict(n=0, passed=False)
    real_r, real_f = float(ev["R"].mean()), float(ev["f40"].mean())
    s = dict(n=int(len(ev)), stocks=int(ev["sym"].nunique()), win_rate=round(float((ev["R"] > 0).mean()), 3),
             avg_R=round(real_r, 3), total_R=round(float(ev["R"].sum()), 1), avg_f20=round(float(ev["f20"].mean()), 2),
             avg_f40=round(real_f, 2), avg_f60=round(float(ev["f60"].mean()), 2))
    days = sorted(ev["day"].unique())
    half = days[len(days) // 2]
    h1, h2 = ev[ev["day"] < half], ev[ev["day"] >= half]
    s["half1_avg_R"] = round(float(h1["R"].mean()), 3) if len(h1) else None
    s["half2_avg_R"] = round(float(h2["R"].mean()), 3) if len(h2) else None
    if len(ev) >= 5 and reps > 0:
        a_r, a_f, b_r, b_f = controls(ev, stocks, tgt_r, reps)
        for tag, arr, real in (("a_R", a_r, real_r), ("b_R", b_r, real_r), ("a_f40", a_f, real_f), ("b_f40", b_f, real_f)):
            arr = arr[~np.isnan(arr)]
            s[f"null_{tag}"] = round(float(arr.mean()), 3)
            s[f"p_{tag}"] = round(float(((arr >= real).sum() + 1) / (len(arr) + 1)), 4)
    ok = bool(s["n"] >= min_n and real_r > 0 and s.get("p_a_R", 1) < p_max and s.get("p_b_R", 1) < p_max
              and (s["half1_avg_R"] or 0) > 0 and (s["half2_avg_R"] or 0) > 0)
    s["passed"] = ok
    return s


HEAD = (f"  {'':<38}{'n':>5}{'win':>6}{'avg R':>8}{'+20d':>7}{'+40d':>7}{'+60d':>7}  {'ctrl A/B R':>12}  {'p A/B':<12}{'halves':<13}verdict")


def row(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:<38}{0:>5}   —"
    v = "✔ EDGE" if s["passed"] else ("too few" if s["n"] < 30 else "✘ no edge")
    ps_ = f"{s['p_a_R']:.3f}/{s['p_b_R']:.3f}" if "p_a_R" in s else "n/a"
    nl = f"{s['null_a_R']:+.2f}/{s['null_b_R']:+.2f}" if "null_a_R" in s else "n/a"
    h1, h2 = s.get("half1_avg_R"), s.get("half2_avg_R")
    hv = f"{h1:+.2f}/{h2:+.2f}" if h1 is not None and h2 is not None else "—"
    return (f"  {name:<38}{s['n']:>5}{100 * s['win_rate']:>5.0f}%{s['avg_R']:>+8.2f}{s['avg_f20']:>+6.1f}%{s['avg_f40']:>+6.1f}%"
            f"{s['avg_f60']:>+6.1f}%  {nl:>12}  {ps_:<12}{hv:<13}{v}")


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if np.isnan(o) else float(o)
    return o


def main():
    import runlog
    runlog.start("backtest_positional")
    R = ps.rules()
    V = R["validation"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=int(V["reps"]))
    ap.add_argument("--after", default=None)
    ap.add_argument("--before", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--data-dir", default=None)
    a = ap.parse_args()
    after = pd.Timestamp(a.after).date() if a.after else None
    before = pd.Timestamp(a.before).date() if a.before else None
    raw = ps.load_all(Path(a.data_dir) if a.data_dir else None)
    if not raw:
        sys.exit("No daily stock data. Run:  .venv/bin/python positional_scan.py --fetch")
    stocks = {s: Stock(s, d, R) for s, d in raw.items()}
    first = min(S.date[0] for S in stocks.values())
    last = max(S.date[-1] for S in stocks.values())
    print(f"{len(stocks)} stocks · daily history {pd.Timestamp(first):%Y-%m-%d} → {pd.Timestamp(last):%Y-%m-%d}"
          f" · testing {after or 'start'} → {before or 'end'}")
    tgt = float(R["target_r"])
    out = dict(generated=datetime.now().isoformat(timespec="seconds"), tag=a.tag, stocks=len(stocks),
               **{"from": str(pd.Timestamp(first).date()), "to": str(pd.Timestamp(last).date())},
               rules={k: R[k] for k in ("box_days", "box_max_width_pct", "vol_mult", "target_r", "max_hold_days", "cost_pct")})
    evs = find_events(stocks, R, True, False, after, before)
    ev = evaluate(evs, stocks, tgt)
    mn, pm = int(V["min_events"]), float(V["p_max"])
    print(f"\nHEADLINE: breakout from a tight box in an uptrend on volume · buy next open · stop box low · {tgt:g}R · "
          f"{R['max_hold_days']}-session limit · {a.reps} random reps")
    print(HEAD)
    head = analyse(ev, stocks, tgt, a.reps, mn, pm)
    print(row("ALL BREAKOUTS", head))
    out["headline"] = head
    out["by_year"] = {}
    if not ev.empty:
        ev["year"] = ev["day"].str[:4]
        print("  by year (shown for stability, not for picking)")
        for y, g in ev.groupby("year"):
            s = analyse(g, stocks, tgt, 0, mn, pm)
            out["by_year"][y] = {k: s.get(k) for k in ("n", "win_rate", "avg_R", "avg_f40")}
            print(row("  " + y, s))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ev.drop(columns=["t"]).to_csv(OUT_DIR / f"positional{('_' + a.tag) if a.tag else ''}_events.csv", index=False)
    # the video's own pre-breakout candidate filter
    evs2 = find_events(stocks, R, True, True, after, before)
    ev2 = evaluate(evs2, stocks, tgt)
    print("\n  the video's SETUP filter the day before (+3.5..7% in a week, RSI 40..60, top of the box) — a subset, shown not picked")
    s2 = analyse(ev2, stocks, tgt, max(200, a.reps // 2), mn, pm)
    out["setup_subset"] = s2
    print(row("breakout after SETUP", s2))
    out["grid"], out["grid_passed"] = [], 0
    if not a.no_grid:
        print("\n  GRID (transparency, not a menu): volume rule × target")
        for vol, t_ in product([True, False], [2.0, 3.0]):
            e3 = evaluate(find_events(stocks, R, vol, False, after, before), stocks, t_)
            s3 = analyse(e3, stocks, t_, int(V["grid_reps"]), mn, pm)
            out["grid_passed"] += int(bool(s3.get("passed")))
            out["grid"].append(dict(volume=vol, target_r=t_, **{k: s3.get(k) for k in (
                "n", "win_rate", "avg_R", "avg_f40", "p_a_R", "p_b_R", "half1_avg_R", "half2_avg_R", "passed")}))
            print(row(f"  volume {'on' if vol else 'off'} · {t_:g}R", s3))
        print(f"  → {out['grid_passed']} of 4 variants pass (about 0.01 expected by luck on both controls)")
    print("\n  Caveats: today's NIFTY 50 are past winners, so absolute returns are flattered (the controls use the same stocks, so the "
          "comparison stays fair).\n  Breakouts cluster in time (market-wide), so the events are less independent than n suggests — "
          "trust a pass only if it repeats in the untouched period.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"positional{('_' + a.tag) if a.tag else ''}.json"
    path.write_text(json.dumps(_clean(out), indent=2))
    print(f"Saved {path}. Information only — not financial advice.")


if __name__ == "__main__":
    main()
