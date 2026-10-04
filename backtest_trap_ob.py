"""
backtest_trap_ob.py — do the AirForexOne trap / break-block entries work on NIFTY, with zones from 30m, 1h and 4h candles?

Entries (guide wording → the explicit rule used here; all numbers in htf_rules.json, fixed before any result):
  1 BEAR TRAP → BULLISH CONTINUATION   bullish impulse on the HTF bar leaves a fresh demand order block (zone)
  2 BULL TRAP → BEARISH CONTINUATION   the mirror (the guide's text for entry 2 is copy-pasted from entry 1; read as the mirror)
  3 BULLISH BREAK BLOCK REVERSAL       the impulse also closes above a still-unbroken supply block → that block flips to support
  4 BEARISH BREAK BLOCK REVERSAL       the mirror
  Pullback: first retest only — price must trade into the zone, and the signal must come before it leaves the zone or closes through it.
  Liquidity grab + confirmation (15m): a 15m candle wicks below the lowest low of the last sweep_lookback candles (same session)
                                       by >= 1 pt, closes back above that low, and closes bullish (mirror for shorts).
  Entry at the open of the next 1m bar · stop = sweep extreme − 2 pts · target 2R · break-even after 75% of the way (be75)
  Risk 8-60 pts · max 2 trades/day · flat 15:15 · 1 pt cost · signals 09:45-14:45.

Same controls and pass rule as the ICT test: random minute on the same day / random side at the same fill; PASS needs
n >= 30, avg R > 0, p < 0.05 vs BOTH, and both halves positive. 3 timeframes are tested, so a lone pass is weak evidence.

  .venv/bin/python backtest_trap_ob.py
  .venv/bin/python backtest_trap_ob.py --before 2025-10-03 --tag train   /   --after 2025-10-03 --tag test
Output: data/backtests/trap_ob[<tag>].json and trap_ob[<tag>]_trades.csv.  Information only — not financial advice.
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
import backtest_five_rule as fr          # noqa: E402
import backtest_ict as bi                # noqa: E402
import htf_candles as hc                 # noqa: E402
import ict_model as im                   # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
ENTRY_NAME = {("BULLISH", "trap"): "1 bear trap → buy", ("BEARISH", "trap"): "2 bull trap → sell",
              ("BULLISH", "break_block"): "3 bull break block → buy", ("BEARISH", "break_block"): "4 bear break block → sell"}


def signals(zs: list, d15: pd.DataFrame, P: dict, tf: int) -> list:
    """First-retest liquidity-grab confirmations on 15m candles for every zone."""
    t = d15["timestamp"].to_numpy()
    ct = d15["timestamp"].to_numpy() + np.timedelta64(int(P["conf_tf"]), "m")
    o, h, l, c = (d15[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    day = d15["timestamp"].dt.date.astype(str).to_numpy()
    days = sorted(set(day))
    didx = {d: i for i, d in enumerate(days)}
    lb, mn = int(P["sweep_lookback"]), float(P["sweep_min_pts"])
    exp = int(P["expiry_days"][str(tf)])
    t_lo, t_hi = im._hm(P["min_signal"]), im._hm(P["last_signal"])
    out = []
    for z in zs:
        sgn = 1 if z["side"] == "BULLISH" else -1
        lo, hi = z["lo"], z["hi"]
        ht = max(hi - lo, 1e-9)
        if z["day"] not in didx:
            first_day = next((i for i, d in enumerate(days) if d >= z["day"]), None)
            if first_day is None:
                continue
        else:
            first_day = didx[z["day"]]
        j0 = int(np.searchsorted(t, np.datetime64(z["known_t"]), side="left"))      # first 15m candle that OPENS at/after known
        touched = False
        for j in range(j0, len(c)):
            if didx[day[j]] - first_day > exp:
                break
            if sgn > 0:
                if c[j] < lo:
                    break                                                  # zone broken
                near = l[j] <= hi
                left = l[j] > hi + float(P["zone_exit_frac"]) * ht
            else:
                if c[j] > hi:
                    break
                near = h[j] >= lo
                left = h[j] < lo - float(P["zone_exit_frac"]) * ht
            if not touched:
                if not near:
                    continue
                touched = True
            elif left:
                break                                                      # left the zone without a signal → consumed
            if not (t_lo <= pd.Timestamp(ct[j]).time() <= t_hi):
                continue
            same = [m for m in range(max(0, j - lb), j) if day[m] == day[j]]
            if len(same) < 3:
                continue
            if sgn > 0:
                ref = l[same].min()
                ok = l[j] <= ref - mn and c[j] > ref and c[j] > o[j]
                sl = l[j] - float(P["sl_buffer_pts"])
            else:
                ref = h[same].max()
                ok = h[j] >= ref + mn and c[j] < ref and c[j] < o[j]
                sl = h[j] + float(P["sl_buffer_pts"])
            if ok:
                out.append(dict(day=day[j], sig_t=pd.Timestamp(ct[j]), side=z["side"], kind=z["kind"], zone_lo=lo, zone_hi=hi,
                                ref=float(ref), sl=float(sl), tf=tf))
                break
    return out


def simulate(sigs: list, days: dict, P: dict, ICT: dict, tp: str, mode: str, trend: dict | None = None,
             after=None, before=None, cost=fr.COST_PTS) -> pd.DataFrame:
    rows, by_day = [], {}
    for s in sigs:
        if fr.in_range(pd.Timestamp(s["day"]).date(), after, before):
            by_day.setdefault(s["day"], []).append(s)
    for day, lst in sorted(by_day.items()):
        D = days.get(day)
        if D is None:
            continue
        busy, taken = -1, 0
        for s in sorted(lst, key=lambda x: x["sig_t"]):
            if taken >= int(P["max_trades_day"]):
                break
            sgn = 1 if s["side"] == "BULLISH" else -1
            if trend is not None and trend.get(day) not in (None, sgn):
                continue
            i0 = D.idx(s["sig_t"])
            if i0 <= busy or i0 > D.eod:
                continue
            e = float(D.o[i0])
            risk = sgn * (e - s["sl"])
            if risk < float(P["min_risk_pts"]) or risk > float(P["max_risk_pts"]):
                continue
            tgt = e + sgn * float(tp) * risk
            res = bi.run_ict(D, i0, sgn, e, s["sl"], tgt, mode)
            if res is None:
                continue
            pnl, jx, outcome = res
            busy = jx
            taken += 1
            rows.append(dict(day=day, time=s["sig_t"].strftime("%H:%M"), window=bi.window_name(s["sig_t"], ICT), side=s["side"],
                             entry_type=ENTRY_NAME[(s["side"], s["kind"])], tf=s["tf"], entry=round(e, 1), sl=round(s["sl"], 1),
                             tp=round(tgt, 1), risk_pts=round(risk, 1), reward_pts=round(sgn * (tgt - e), 1), start_i=i0,
                             exit_time=D.t[jx].strftime("%H:%M"), outcome=outcome, pnl_pts=round(pnl - cost, 1),
                             R=round((pnl - cost) / risk, 3)))
    return pd.DataFrame(rows)


def daily_trend(m1: pd.DataFrame) -> dict:
    """+1 / −1 per session: yesterday's close vs the mean of the 5 closes before it. Known before the open."""
    cl = m1.groupby(m1["timestamp"].dt.date)["close"].last()
    ds = list(cl.index)
    out = {}
    for i in range(6, len(ds)):
        out[str(ds[i])] = 1 if cl.iloc[i - 1] > cl.iloc[i - 6:i - 1].mean() else -1
    return out


def main():
    import runlog
    runlog.start("backtest_trap_ob")
    P = hc.rules()
    ICT = im.rules()
    V = ICT["validation"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--reps", type=int, default=int(V["reps"]))
    ap.add_argument("--after", default=None)
    ap.add_argument("--before", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-grid", action="store_true")
    a = ap.parse_args()
    after = pd.Timestamp(a.after).date() if a.after else None
    before = pd.Timestamp(a.before).date() if a.before else None
    if a.fetch:
        print(f"saved {fr.fetch_history(a.fetch)} session file(s)")
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles. Run with --fetch 730 first.")
    days_all = sorted(m1["timestamp"].dt.date.unique())
    sel = [d for d in days_all if fr.in_range(d, after, before)]
    print(f"{len(days_all)} stored sessions · testing {len(sel)} ({sel[0]} → {sel[-1]})")
    if len(sel) < 40:
        sys.exit("Need at least 40 sessions for an honest test.")
    days = {str(d): fr.Day(g.reset_index(drop=True)) for d, g in m1.groupby(m1["timestamp"].dt.date)}
    d15 = im.resample(m1, int(P["conf_tf"]))
    trend = daily_trend(m1)
    mt, pm = int(V["min_trades"]), float(V["p_max"])
    out = dict(generated=datetime.now().isoformat(timespec="seconds"), tag=a.tag, sessions=len(sel),
               **{"from": str(sel[0]), "to": str(sel[-1])}, rules={k: P[k] for k in ("impulse_body_atr", "expiry_days", "tp", "mode")},
               tfs={}, grid_passed=0, grid_n=0)
    all_tr, all_sigs = [], []
    for tf in P["timeframes"]:
        bars = hc.htf(m1, int(tf))
        zs = hc.zones(bars, P)
        zs = [z for z in zs if fr.in_range(pd.Timestamp(z["day"]).date(), after, before)]
        sigs = signals(zs, d15, P, int(tf))
        all_sigs += sigs
        print(f"\n{'═' * 100}\n{tf}-MINUTE ZONES: {len(bars)} bars → {len(zs)} zones "
              f"({sum(z['kind'] == 'trap' for z in zs)} trap / {sum(z['kind'] == 'break_block' for z in zs)} break block) → "
              f"{len(sigs)} confirmed first-retest signals")
        tr = simulate(sigs, days, P, ICT, P["tp"], P["mode"], None, after, before)
        print(f"PRIMARY: target {P['tp']}R · {P['mode']} · no trend filter · {a.reps} random reps")
        print(bi.HEAD)
        head = bi.analyse(tr, days, ICT, P["mode"], a.reps, mt, pm)
        print(bi._row(f"ALL {tf}m ZONES", head))
        res = dict(zones=len(zs), signals=len(sigs), headline=head, entries={}, windows={}, grid=[])
        if not tr.empty:
            all_tr.append(tr)
            for en, sub in tr.groupby("entry_type"):
                s = bi.analyse(sub, days, ICT, P["mode"], max(200, a.reps // 3), mt, pm)
                res["entries"][en] = s
                print(bi._row(en, s))
            for w, sub in tr.groupby("window"):
                s = bi.analyse(sub, days, ICT, P["mode"], max(200, a.reps // 3), mt, pm)
                res["windows"][w] = s
                print(bi._row("window " + w, s))
        if not a.no_grid and sigs:
            print("  grid (transparency, not a menu): target × management × trend filter")
            for tp_, md, tfl in product(["2", "3"], ["tp", "be75"], ["none", "d5"]):
                t2 = simulate(sigs, days, P, ICT, tp_, md, trend if tfl == "d5" else None, after, before)
                s = bi.analyse(t2, days, ICT, md, int(V["grid_reps"]), mt, pm)
                out["grid_n"] += 1
                out["grid_passed"] += int(bool(s.get("passed")))
                res["grid"].append(dict(tp=tp_, mode=md, trend=tfl, **{k: s.get(k) for k in (
                    "n", "win_rate", "avg_R", "total_R", "p_time", "p_side", "half1_avg_R", "half2_avg_R", "passed")}))
                print(bi._row(f"  {tp_}R · {md} · trend {tfl}", s))
        out["tfs"][str(tf)] = res
    # ── ONE pre-declared headline: all three timeframes pooled (each alone has too few trades to judge) ──
    print(f"\n{'═' * 100}\nHEADLINE — ALL TIMEFRAMES POOLED (30m + 1h + 4h zones, max {P['max_trades_day']} trades/day) · "
          f"target {P['tp']}R · {P['mode']} · {a.reps} random reps")
    print(bi.HEAD)
    trc = simulate(all_sigs, days, P, ICT, P["tp"], P["mode"], None, after, before)
    comb = bi.analyse(trc, days, ICT, P["mode"], a.reps, mt, pm)
    print(bi._row("ALL ZONES POOLED", comb))
    out["headline"], out["entries"], out["sides"] = comb, {}, {}
    if not trc.empty:
        for en, sub in trc.groupby("entry_type"):
            out["entries"][en] = bi.analyse(sub, days, ICT, P["mode"], max(200, a.reps // 3), mt, pm)
            print(bi._row(en, out["entries"][en]))
        tdf = simulate(all_sigs, days, P, ICT, P["tp"], P["mode"], trend, after, before)
        out["trend_d5"] = bi.analyse(tdf, days, ICT, P["mode"], max(200, a.reps // 3), mt, pm)
        print(bi._row("only with daily trend (d5)", out["trend_d5"]))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        trc.drop(columns=["start_i"]).to_csv(OUT_DIR / f"trap_ob{('_' + a.tag) if a.tag else ''}_trades.csv", index=False)
    passed = [tf for tf, r in out["tfs"].items() if r["headline"].get("passed")]
    out["passed_tfs"] = passed
    print(f"\nPer-timeframe passes: {', '.join(t + 'm' for t in passed) if passed else 'none'} · grid {out['grid_passed']} of "
          f"{out['grid_n']} variants pass (≈{out['grid_n'] * 0.0025:.2f} expected by luck on both controls). "
          f"The pooled line above is the single pre-declared verdict.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"trap_ob{('_' + a.tag) if a.tag else ''}.json"
    path.write_text(json.dumps(bi._clean(out), indent=2))
    print(f"Saved {path}. Information only — not financial advice.")


if __name__ == "__main__":
    main()
