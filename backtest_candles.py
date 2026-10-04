"""
backtest_candles.py — after a candlestick pattern forms on a 15m / 30m / 1h bar, does price really move the textbook way?

  event     the bar where a pattern completes (candle_patterns.py; rules in candle_rules.json)
  measure   close of that bar → close H bars later (H = 1, 2, 4; primary 2), same session only, divided by the bar's ATR(14)
            so 15m, 30m and 1h events can be pooled; signed in the textbook direction (up for bullish, down for bearish;
            doji / spinning top: against the prior move)
  control   a random bar with the SAME timeframe, the SAME time-of-day slot and the SAME prior-move sign (so "after a fall in
            the morning" is compared with other falls at that time, not with the whole day), signed the same way; 500 draws
  pass      n >= 60, edge (pattern minus control, in ATR) > 0, p < 0.05 AFTER multiplying by the number of patterns tested
            (22 patterns → one would "pass" by luck at plain p<0.05), and both halves of the period positive
  INVERSE   the same test the other way round: price reliably moves AGAINST the textbook (also a finding, not a signal)

  .venv/bin/python backtest_candles.py
  .venv/bin/python backtest_candles.py --before 2025-10-03 --tag train
  .venv/bin/python backtest_candles.py --after 2025-10-03 --tag test        # the untouched period
Caveats: overlapping bars make p optimistic (a 15m and a 1h hammer can be the same event); results are for NIFTY index candles, 2 years.
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
import candle_patterns as cp             # noqa: E402
import htf_candles as hc                 # noqa: E402

BT = ROOT / "data" / "backtests"


def build(m1: pd.DataFrame, R: dict, sel: set):
    """→ (events DataFrame, pools dict). Moves are computed for every bar so the controls come from the same data."""
    V = R["validation"]
    H = [int(x) for x in V["horizons_bars"]]
    ev, pool, pool_near = [], {}, {}
    for tf in R["timeframes"]:
        bars = hc.htf(m1, int(tf))
        if bars.empty:
            continue
        A = cp.prepare(bars, R)
        c, atr, day = A["c"], A["atr"], A["day"]
        D = cp.detect(bars, R)
        drift = D["_drift"]
        LV = cp.level_table(bars, m1, R)
        nlo, nhi = LV["near_lo"], LV["near_hi"]
        ts = bars["timestamp"]
        slot = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
        ctx = np.where(np.isfinite(drift), np.sign(drift), 0).astype(int)
        dates = ts.dt.date.to_numpy()
        insel = np.array([d in sel for d in dates])
        fwd = {}
        for h in H:
            f = np.full(len(c), np.nan)
            if h < len(c):
                f[:-h] = c[h:] - c[:-h]
                same = np.r_[day[h:] == day[:-h], np.zeros(h, bool)]
                f[~same] = np.nan
            fwd[h] = f
        for i in range(len(c)):                                          # pools (all selected bars, any outcome)
            if not insel[i] or not np.isfinite(atr[i]) or not np.isfinite(drift[i]):
                continue
            for h in H:
                if np.isfinite(fwd[h][i]):
                    pool.setdefault((tf, int(slot[i]), int(ctx[i]), h), []).append(fwd[h][i] / atr[i])
                    if nlo[i]:
                        pool_near.setdefault((tf, int(slot[i]), int(ctx[i]), h, 1), []).append(fwd[h][i] / atr[i])
                    if nhi[i]:
                        pool_near.setdefault((tf, int(slot[i]), int(ctx[i]), h, -1), []).append(fwd[h][i] / atr[i])
        for name in cp.PATTERNS:
            idx = np.flatnonzero(D[name] & insel)
            for i in idx:
                d = cp.direction(name, drift[i])
                if d == 0:
                    continue
                row = dict(pattern=name, tf=tf, day=dates[i], t=ts.iloc[i], dir=d, near_lo=bool(nlo[i]), near_hi=bool(nhi[i]), slot=int(slot[i]), ctx=int(ctx[i]), atr=float(atr[i]))
                for h in H:
                    row[f"pts{h}"] = float(fwd[h][i]) if np.isfinite(fwd[h][i]) else np.nan
                    row[f"atr{h}"] = float(fwd[h][i] / atr[i]) if np.isfinite(fwd[h][i]) else np.nan
                ev.append(row)
    pool = {k: np.asarray(v, float) for k, v in pool.items()}
    pool_near = {k: np.asarray(v, float) for k, v in pool_near.items()}
    return pd.DataFrame(ev), pool, pool_near


def analyse(ev: pd.DataFrame, pool: dict, h: int, reps: int, seed: int = 7, near: bool = False):
    """Event mean vs same-slot / same-prior-move controls, signed in the pattern direction. → dict (or None if no usable events)."""
    e = ev[np.isfinite(ev[f"atr{h}"])]
    if near:                                           # bullish pattern whose low is at a level / bearish pattern whose high is at a level
        e = e[((e["dir"] > 0) & e["near_lo"]) | ((e["dir"] < 0) & e["near_hi"])]
    if e.empty:
        return None
    rng = np.random.default_rng(seed)
    keys = [(r.tf, r.slot, r.ctx, h, int(r.dir)) if near else (r.tf, r.slot, r.ctx, h) for r in e.itertuples()]
    ok = np.array([k in pool and len(pool[k]) >= 5 for k in keys])
    e, keys = e[ok], [k for k, o in zip(keys, ok) if o]
    if e.empty:
        return None
    s = e["dir"].to_numpy(float)
    x = s * e[f"atr{h}"].to_numpy(float)                                  # signed move in ATR
    cm = np.array([pool[k].mean() for k in keys]) * s                       # control expectation per event
    edge = x - cm
    draws = np.empty((len(e), reps))
    for j, k in enumerate(keys):
        p = pool[k]
        draws[j] = s[j] * p[rng.integers(0, len(p), reps)]
    ctrl_means = (draws - cm[:, None]).mean(axis=0)
    obs = float(edge.mean())
    p_hi = float((np.sum(ctrl_means >= obs) + 1) / (reps + 1))
    p_lo = float((np.sum(ctrl_means <= obs) + 1) / (reps + 1))
    days = pd.to_datetime(e["day"])
    half = days <= days.sort_values().iloc[len(days) // 2]
    h1, h2 = float(edge[half.to_numpy()].mean()) if half.any() else np.nan, float(edge[~half.to_numpy()].mean()) if (~half).any() else np.nan
    pts = (s * e[f"pts{h}"].to_numpy(float))
    ctrl_hit = float(np.mean([(pool[k] > 0).mean() if sg > 0 else (pool[k] < 0).mean() for k, sg in zip(keys, s)]))
    return dict(n=int(len(e)), days=int(days.nunique()), avg_atr=float(x.mean()), avg_pts=float(pts.mean()), edge=obs,
                hit=float((x > 0).mean()), ctrl_hit=ctrl_hit, p_up=p_hi, p_dn=p_lo, half1=h1, half2=h2)


def verdict(a: dict | None, n_tests: int, V: dict, min_n: int | None = None) -> str:
    if not a or a["n"] < int(min_n or V["min_events"]):
        return "FEW"
    pa = min(1.0, a["p_up"] * n_tests)
    pl = min(1.0, a["p_dn"] * n_tests)
    if a["edge"] > 0 and pa < float(V["p_max"]) and a["half1"] > 0 and a["half2"] > 0:
        return "EDGE"
    if a["edge"] < 0 and pl < float(V["p_max"]) and a["half1"] < 0 and a["half2"] < 0:
        return "INVERSE"
    if a["edge"] > 0 and a["p_up"] < float(V["p_max"]) and a["half1"] > 0 and a["half2"] > 0:
        return "LEAN"                                   # significant on its own, not after counting how many patterns were tried
    if a["edge"] < 0 and a["p_dn"] < float(V["p_max"]) and a["half1"] < 0 and a["half2"] < 0:
        return "LEAN_INV"
    return "NONE"


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 4)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def main():
    import runlog
    runlog.start("backtest_candles")
    R = cp.rules()
    V = R["validation"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=int(V["reps"]))
    ap.add_argument("--after", default=None)
    ap.add_argument("--before", default=None)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    after = pd.Timestamp(a.after).date() if a.after else None
    before = pd.Timestamp(a.before).date() if a.before else None
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles.")
    days_all = sorted(m1["timestamp"].dt.date.unique())
    sel = [d for d in days_all if fr.in_range(d, after, before)]
    print(f"{len(days_all)} NIFTY sessions stored · testing {len(sel)} ({sel[0]} → {sel[-1]}) · timeframes {R['timeframes']} min")
    if len(sel) < 60:
        sys.exit("Need at least 60 sessions for an honest test.")
    ev, pool, pool_near = build(m1, R, set(sel))
    if ev.empty:
        sys.exit("No pattern events found.")
    H = [int(x) for x in V["horizons_bars"]]
    ph = int(V["primary_bars"])
    names = [n for n in cp.PATTERNS if (ev["pattern"] == n).any()]
    res = {n: {} for n in names}
    for n in names:
        e = ev[ev["pattern"] == n]
        for h in H:
            res[n][h] = analyse(e, pool, h, a.reps if h == ph else max(100, a.reps // 4))
    n_tests = sum(1 for n in names if res[n][ph] and res[n][ph]["n"] >= int(V["min_events"]))
    resl = {n: analyse(ev[ev["pattern"] == n], pool_near, ph, a.reps, near=True) for n in names}
    mel = int(V["min_events_level"])
    n_tests_l = sum(1 for n in names if resl[n] and resl[n]["n"] >= mel)
    print(f"{len(ev)} pattern events · {n_tests} patterns have >= {V['min_events']} events (p is multiplied by {n_tests}) · "
          f"primary: {ph} bars after the pattern bar closes, in ATR units · {a.reps} control draws\n")
    print(f"{'pattern':<22}{'textbook':>9}{'n':>7}{'moved textbook way':>20}{'control':>9}{'avg move':>10}{'edge ATR':>10}{'p_adj':>8}{'halves':>14}  verdict")
    out_p = {}
    for n in names:
        r = res[n][ph]
        if not r:
            continue
        vd = verdict(r, n_tests, V)
        d = cp.PATTERNS[n][0]
        pa = min(1.0, (r["p_up"] if r["edge"] >= 0 else r["p_dn"]) * n_tests)
        print(f"{n:<22}{cp.ARROW[d]:>9}{r['n']:>7}{r['hit']*100:>19.0f}%{r['ctrl_hit']*100:>8.0f}%{r['avg_pts']:>+9.1f}p{r['edge']:>+10.3f}{pa:>8.3f}"
              f"{r['half1']:>+8.2f}/{r['half2']:+.2f}  {vd}")
        out_p[n] = dict(r, verdict=vd, p_adj=pa, textbook=d, by_h={str(h): res[n][h] for h in H})
    print(f"\nSAME TEST, ONLY WHEN THE PATTERN SITS AT A LEVEL (bullish: bar low, bearish: bar high within {cp.rules()['wick']['level_tol_atr']} ATR of "
          f"prev-day high/low, opening-range high/low, or the day's high/low so far) · control = random bars at a level, same time slot and prior move · "
          f"{n_tests_l} patterns have >= {mel} events\n")
    print(f"{'pattern':<22}{'textbook':>9}{'n':>7}{'moved textbook way':>20}{'control':>9}{'avg move':>10}{'edge ATR':>10}{'p_adj':>8}{'halves':>14}  verdict")
    for n in names:
        r = resl[n]
        if not r:
            continue
        vd = verdict(r, n_tests_l, V, mel)
        pa = min(1.0, (r["p_up"] if r["edge"] >= 0 else r["p_dn"]) * n_tests_l)
        print(f"{n:<22}{cp.ARROW[cp.PATTERNS[n][0]]:>9}{r['n']:>7}{r['hit']*100:>19.0f}%{r['ctrl_hit']*100:>8.0f}%{r['avg_pts']:>+9.1f}p{r['edge']:>+10.3f}{pa:>8.3f}"
              f"{r['half1']:>+8.2f}/{r['half2']:+.2f}  {vd}")
        out_p[n]["lvl"] = dict(r, verdict=vd, p_adj=pa)
    print("\nEdge (ATR) by timeframe — pattern · 15m / 30m / 1h  (n, edge):")
    for n in names:
        e = ev[ev["pattern"] == n]
        cells = []
        for tf in R["timeframes"]:
            r = analyse(e[e["tf"] == tf], pool, ph, 150)
            cells.append(f"{r['n']:>4} {r['edge']:+.2f}" if r else "   -     ")
        print(f"  {n:<22}" + "   |   ".join(cells))
    print("\nEdge (ATR) by horizon — pattern · 1 / 2 / 4 bars after:")
    for n in names:
        cells = [f"{res[n][h]['edge']:+.3f}" if res[n][h] else "    -  " for h in H]
        print(f"  {n:<22}" + "   |   ".join(cells))
    passed = [n for n, v in out_p.items() if v["verdict"] == "EDGE"]
    inverse = [n for n, v in out_p.items() if v["verdict"] == "INVERSE"]
    lean = [n for n, v in out_p.items() if v["verdict"] in ("LEAN", "LEAN_INV")]
    passed_l = [n for n, v in out_p.items() if (v.get("lvl") or {}).get("verdict") == "EDGE"]
    lean_l = [n for n, v in out_p.items() if (v.get("lvl") or {}).get("verdict") in ("LEAN", "LEAN_INV")]
    inv_l = [n for n, v in out_p.items() if (v.get("lvl") or {}).get("verdict") == "INVERSE"]
    print(f"\nAT A LEVEL: {len(passed_l)} of {n_tests_l} patterns pass" + (f": {', '.join(passed_l)}" if passed_l else "")
          + (f" · weak leads: {', '.join(lean_l)}" if lean_l else "") + (f" · opposite to textbook: {', '.join(inv_l)}" if inv_l else ""))
    exp_false = 0.05 * n_tests
    if lean:
        print(f"\nLEAN (significant alone, NOT after the multiple-test correction — not proven, check the untouched period): {', '.join(lean)}")
    print(f"\nHEADLINE: {len(passed)} of {n_tests} patterns show a textbook-direction edge after the multiple-test correction"
          + (f": {', '.join(passed)}" if passed else "") + (f" · {len(inverse)} move the OPPOSITE way: {', '.join(inverse)}" if inverse else "")
          + f"\n(With {n_tests} patterns you would expect ~{exp_false:.1f} 'passes' at plain p<0.05 from luck alone — that is why p is multiplied.)")
    BT.mkdir(parents=True, exist_ok=True)
    fn = f"candles{('_' + a.tag) if a.tag else ''}.json"
    (BT / fn).write_text(json.dumps(_clean(dict(
        generated=datetime.now().isoformat(timespec="seconds"), tag=a.tag, sessions=len(sel), events=len(ev),
        **{"from": str(sel[0]), "to": str(sel[-1])}, primary_bars=ph, n_tests=n_tests, passed=passed, inverse=inverse, lean=lean, passed_level=passed_l, lean_level=lean_l, inverse_level=inv_l, n_tests_level=n_tests_l,
        headline=dict(n=len(ev), n_tests=n_tests, passed=bool(passed or passed_l), passed_names=passed, inverse_names=inverse),
        rules={k: R[k] for k in ("timeframes", "trend_bars", "trend_atr")}, patterns=out_p)), indent=1))
    ev.drop(columns=["slot", "ctx"]).to_csv(BT / fn.replace(".json", "_events.csv"), index=False)
    print(f"\nsaved data/backtests/{fn}")
    print("Information only — not financial advice.")


if __name__ == "__main__":
    main()
