"""
backtest_ict.py — does the ICT 2022 model (ict_model.py) actually make money on YOUR NIFTY 1m candles,
and do the morning / lunch / afternoon windows really behave differently?

  1. ict_model finds every setup (sweep → displacement/MSS → FVG) on 5m candles, causally.
  2. Each setup becomes a LIMIT order (FVG top | midpoint | OTE), live for fill_wait_min, cancelled if price
     takes the stop level first or runs to the target without filling. Simulated on the 1m bars: stop wins
     ties, flat at 15:15, 1 pt cost per trade — the same rules as backtest_five_rule.py.
  3. Two RANDOM controls with the same stop/target geometry:
        A · random TIME — same day, same window, same side, random minute      (does the setup timing matter?)
        B · random SIDE — same fill, coin-flip BUY/SELL                        (does the direction matter?)
     PASS = positive average R, p < 0.05 against BOTH controls, and positive in BOTH halves of the sample.
  4. Window split (A morning 09:15-11:45 · B lunch 11:45-13:30 · C afternoon 13:30-15:00) and BUY vs SELL.
  5. Variant grid (3 entries x 3 targets x 2 management styles): shown for transparency. 18 variants means
     about one passes by luck at p<0.05 — the PRIMARY variant in ict_rules.json was fixed in advance;
     trust the grid only where it agrees with it.
  6. Time-window study (no strategy at all): per 30-minute bucket, how big is the move and does the
     previous 30 minutes CONTINUE or REVERSE? (daily means, t-stat across sessions)

Run (OC folder):
  .venv/bin/python backtest_ict.py                       # all stored sessions
  .venv/bin/python backtest_ict.py --before 2025-10-03 --tag train    # fit year
  .venv/bin/python backtest_ict.py --after  2025-10-03 --tag test     # untouched year
  options: --reps 1000 --entry ce|fvg|ote --tp 2|3|liq --mode tp|be75 --no-grid --fetch 730

Output: data/backtests/ict[<tag>].json, ict[<tag>]_trades.csv   (Evidence Board reads ict.json)
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime
from itertools import product
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import backtest_five_rule as fr          # noqa: E402  (loaders, 1m day arrays, COST_PTS, EOD)
import ict_model as im                   # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
BIG = 10 ** 9


# ═══════════════════════════════════════════════════════════════════════════
# Execution on 1m bars
# ═══════════════════════════════════════════════════════════════════════════
def run_ict(D: "fr.Day", start: int, sgn: int, e: float, sl: float, tp: float, mode: str):
    """(pnl_pts, exit_index, outcome). mode 'tp' = plain stop/target; 'be75' = stop to entry after 75% of the way
    to target; 'be1r' = stop to entry after +1R. Stop wins ties. The position is live from bar `start`."""
    end = D.eod
    if start > end:
        return None
    H, L = D.h[start:end + 1], D.l[start:end + 1]
    risk = sgn * (e - sl)
    hit_sl = (L <= sl) if sgn > 0 else (H >= sl)
    hit_tp = (H >= tp) if sgn > 0 else (L <= tp)
    i_sl, i_tp = fr._first(hit_sl), fr._first(hit_tp)

    def eod():
        return sgn * (D.c[end] - e), end, "EOD"

    if mode in ("be75", "be1r"):
        trig = e + sgn * (0.75 * sgn * (tp - e) if mode == "be75" else risk)
        i_tr = fr._first((H >= trig) if sgn > 0 else (L <= trig))
        if i_sl <= i_tr:
            return (-risk, start + i_sl, "SL") if i_sl < BIG else eod()
        if i_tr >= BIG:
            return eod()
        if i_tp == i_tr:
            return sgn * (tp - e), start + i_tp, "TP"
        H2, L2 = H[i_tr + 1:], L[i_tr + 1:]
        j_be = fr._first((L2 <= e) if sgn > 0 else (H2 >= e))
        j_tp = fr._first((H2 >= tp) if sgn > 0 else (L2 <= tp))
        if j_be < BIG and j_be <= j_tp:
            return 0.0, start + i_tr + 1 + j_be, "BE"
        if j_tp < BIG:
            return sgn * (tp - e), start + i_tr + 1 + j_tp, "TP"
        return eod()
    if i_sl < BIG and i_sl <= i_tp:
        return -risk, start + i_sl, "SL"
    if i_tp < BIG:
        return sgn * (tp - e), start + i_tp, "TP"
    return eod()


def window_name(ts: pd.Timestamp, P: dict) -> str:
    t = ts.time()
    for name, (a, b) in P["windows"].items():
        if im._hm(a) <= t < im._hm(b):
            return name
    return "OUT"


def simulate(setups, days: dict, P: dict, entry: str, tp: str, mode: str, cost=fr.COST_PTS,
             after=None, before=None, pdf: str = "none", smt: str = "none") -> pd.DataFrame:
    rows = []
    by_day = {}
    for s in setups:
        d = pd.Timestamp(s["day"]).date()
        if fr.in_range(d, after, before):
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
            i0 = D.idx(s["sig_t"])
            if i0 <= busy or i0 > D.eod:
                continue
            e_px, sl = float(s["entry_" + entry]), float(s["sl"])
            if sgn * (e_px - sl) <= 0:
                continue
            risk0 = sgn * (e_px - sl)
            if tp == "liq":
                if np.isnan(s["liq"]):
                    continue
                tgt = float(s["liq"])
                if sgn * (tgt - e_px) < 1.5 * risk0:
                    continue
            else:
                tgt = e_px + sgn * float(tp) * risk0
            fill = None
            for j in range(i0, min(i0 + int(P["fill_wait_min"]), D.eod + 1)):
                if (D.l[j] <= sl) if sgn > 0 else (D.h[j] >= sl):
                    break                                                    # the sweep extreme failed — setup dead
                if (D.h[j] >= tgt) if sgn > 0 else (D.l[j] <= tgt):
                    break                                                    # ran to target without us
                if (D.l[j] <= e_px) if sgn > 0 else (D.h[j] >= e_px):
                    fill = (j, min(e_px, D.o[j]) if sgn > 0 else max(e_px, D.o[j]))
                    break
            if fill is None:
                continue
            start, e = fill
            risk, reward = sgn * (e - sl), sgn * (tgt - e)
            if pdf in ("eq", "open"):                                        # premium / discount rule
                ref = float(s["eq"] if pdf == "eq" else s["day_open"])
                if sgn * (ref - e) <= 0:
                    continue
            if smt == "require" and s.get("smt") is not True:             # Bank Nifty must NOT have confirmed the sweep
                continue
            if risk < float(P["min_risk_pts"]) or risk > float(P["max_risk_pts"]) or reward <= 0:
                continue
            res = run_ict(D, start, sgn, e, sl, tgt, mode)
            if res is None:
                continue
            pnl, jx, outcome = res
            busy = jx
            taken += 1
            rows.append(dict(day=day, time=s["sig_t"].strftime("%H:%M"), window=window_name(s["sig_t"], P),
                             side=s["side"], level=s["level_kind"], smt=s.get("smt"), entry=round(e, 1), sl=round(sl, 1),
                             tp=round(tgt, 1), risk_pts=round(risk, 1), reward_pts=round(reward, 1), start_i=start,
                             fill_time=D.t[start].strftime("%H:%M"), exit_time=D.t[jx].strftime("%H:%M"),
                             outcome=outcome, pnl_pts=round(pnl - cost, 1), R=round((pnl - cost) / risk, 3)))
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Controls + stats
# ═══════════════════════════════════════════════════════════════════════════
def _minutes_in_window(D: "fr.Day", win: str, P: dict) -> np.ndarray:
    if win == "OUT":
        return np.array([], dtype=int)
    a, b = (im._hm(x) for x in P["windows"][win])
    tt = D.t.dt.time.to_numpy()
    last = im._hm(P["last_signal"])
    return np.where([(a <= x < b) and dtime(9, 31) <= x <= last for x in tt])[0]


def nulls(tr: pd.DataFrame, days: dict, P: dict, mode: str, reps: int, seed=11, cost=fr.COST_PTS) -> dict:
    if tr.empty:
        return {}
    rng = np.random.default_rng(seed)
    real = float(tr["R"].mean())
    cache, rows = {}, list(tr.itertuples())
    a_m, b_m = np.empty(reps), np.empty(reps)
    for r in range(reps):
        ra, rb = [], []
        for t in rows:
            D = days[t.day]
            sgn = 1 if t.side == "BULLISH" else -1
            key = (t.day, t.window)
            if key not in cache:
                cache[key] = _minutes_in_window(D, t.window, P)
            mins = cache[key]
            if len(mins):
                j = int(rng.choice(mins))
                e = float(D.o[j])
                x = run_ict(D, j, sgn, e, e - sgn * t.risk_pts, e + sgn * t.reward_pts, mode)
                if x:
                    ra.append((x[0] - cost) / t.risk_pts)
            s2 = 1 if rng.random() < 0.5 else -1
            x = run_ict(D, int(t.start_i), s2, t.entry, t.entry - s2 * t.risk_pts, t.entry + s2 * t.reward_pts, mode)
            if x:
                rb.append((x[0] - cost) / t.risk_pts)
        a_m[r] = np.mean(ra) if ra else np.nan
        b_m[r] = np.mean(rb) if rb else np.nan
    a_m, b_m = a_m[~np.isnan(a_m)], b_m[~np.isnan(b_m)]
    return dict(real=real, time_null=float(a_m.mean()), p_time=float(((a_m >= real).sum() + 1) / (len(a_m) + 1)),
                side_null=float(b_m.mean()), p_side=float(((b_m >= real).sum() + 1) / (len(b_m) + 1)))


def analyse(tr: pd.DataFrame, days: dict, P: dict, mode: str, reps: int, min_trades: int, p_max: float) -> dict:
    s = fr.stats(tr)
    if not s.get("n"):
        return dict(n=0, passed=False)
    nl = nulls(tr, days, P, mode, reps) if s["n"] >= 5 else {}
    s.update({k: round(v, 4) for k, v in nl.items()})
    h1, h2 = s.get("half1_avg_R"), s.get("half2_avg_R")
    s["passed"] = bool(s["n"] >= min_trades and s["avg_R"] > 0 and nl and nl["p_time"] < p_max and nl["p_side"] < p_max
                       and (h1 or 0) > 0 and (h2 or 0) > 0)
    return s


def _row(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:<34}{0:>5}   —"
    p1, p2 = s.get("p_time"), s.get("p_side")
    ps = f"{p1:.3f}/{p2:.3f}" if p1 is not None else "  n/a  "
    h1, h2 = s.get("half1_avg_R"), s.get("half2_avg_R")
    hv = f"{h1:+.2f}/{h2:+.2f}" if h1 is not None and h2 is not None else "   —   "
    v = "✔ EDGE" if s["passed"] else ("too few" if s["n"] < 30 else "✘ no edge")
    return (f"  {name:<34}{s['n']:>5}{100 * s['win_rate']:>6.0f}%{s['avg_R']:>+8.2f}R{s['total_R']:>+8.1f}R"
            f"{s.get('time_null', float('nan')):>+8.2f}{s.get('side_null', float('nan')):>+8.2f}  {ps:<13}{hv:<13}{v}")


HEAD = (f"  {'':<34}{'n':>5}{'win':>7}{'avg':>9}{'total':>9}{'rnd-time':>8}{'rnd-side':>8}  "
        f"{'p time/side':<13}{'halves':<13}verdict")


# ═══════════════════════════════════════════════════════════════════════════
# Time-window study (no strategy): size of move and continuation vs reversal per 30-minute bucket
# ═══════════════════════════════════════════════════════════════════════════
def _p_two_sided(t: float) -> float:
    return 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))


def window_study(m1: pd.DataFrame, bucket=30) -> list:
    per = {}
    for day, g in m1.groupby(m1["timestamp"].dt.date):
        if len(g) < 300:
            continue
        o, h, l, c = (g[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        nb = len(c) // bucket
        rets = [c[(b + 1) * bucket - 1] - o[b * bucket] for b in range(nb)]
        rngs = [h[b * bucket:(b + 1) * bucket].max() - l[b * bucket:(b + 1) * bucket].min() for b in range(nb)]
        day_range = h.max() - l.min()
        for b in range(nb):
            lab = f"{(9 * 60 + 15 + b * bucket) // 60:02d}:{(9 * 60 + 15 + b * bucket) % 60:02d}"
            d = per.setdefault(lab, dict(rng=[], share=[], cont=[], up=[]))
            d["rng"].append(rngs[b])
            d["share"].append(rngs[b] / day_range if day_range else np.nan)
            d["up"].append(1.0 if rets[b] > 0 else 0.0)
            if b > 0 and rets[b - 1] != 0:
                d["cont"].append(np.sign(rets[b - 1]) * rets[b])
    out = []
    for lab in sorted(per):
        d = per[lab]
        cont = np.array(d["cont"], float)
        if len(cont) >= 20 and cont.std() > 0:
            t = float(cont.mean() / (cont.std(ddof=1) / sqrt(len(cont))))
        else:
            t = float("nan")
        out.append(dict(bucket=lab, days=len(d["rng"]), avg_range=round(float(np.mean(d["rng"])), 1),
                        share_of_day=round(float(np.nanmean(d["share"])), 3),
                        cont_pts=round(float(cont.mean()), 2) if len(cont) else None,
                        cont_t=round(t, 2) if not np.isnan(t) else None,
                        cont_p=round(_p_two_sided(t), 3) if not np.isnan(t) else None))
    return out


# ═══════════════════════════════════════════════════════════════════════════
def _clean(o):
    """JSON-safe copy: numpy scalars → python, NaN → None."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return str(o)
    return o


def main():
    import runlog
    runlog.start("backtest_ict")
    P = im.rules()
    V = P["validation"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=0, help="download N days of 1m history from Dhan first")
    ap.add_argument("--reps", type=int, default=int(V["reps"]))
    ap.add_argument("--entry", default=P["entry"], choices=["fvg", "ce", "ote"])
    ap.add_argument("--tp", default=str(P["tp"]), choices=["2", "3", "liq"])
    ap.add_argument("--mode", default=P["mode"], choices=["tp", "be75", "be1r"])
    ap.add_argument("--pd", default=P["pd_filter"], choices=["none", "eq", "open"], help="premium/discount filter")
    ap.add_argument("--after", default=None)
    ap.add_argument("--before", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--fetch-bn", type=int, default=0, help="download N days of Bank Nifty 1m first (SMT)")
    ap.add_argument("--smt", default=P.get("smt_filter", "none"), choices=["none", "require"], help="SMT filter")
    a = ap.parse_args()
    after = pd.Timestamp(a.after).date() if a.after else None
    before = pd.Timestamp(a.before).date() if a.before else None
    if a.fetch:
        print(f"Fetching ~{a.fetch} days of 1m NIFTY from Dhan …")
        print(f"  saved {fr.fetch_history(a.fetch)} session file(s)")
    import bn_history as bn
    if a.fetch_bn:
        print(f"  saved {bn.fetch_history(a.fetch_bn)} Bank Nifty session file(s)")
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles (data/candles, data/candles_hist). Run with --fetch 730 first.")
    days_all = sorted(m1["timestamp"].dt.date.unique())
    sel = [d for d in days_all if fr.in_range(d, after, before)]
    print(f"{len(days_all)} stored sessions ({days_all[0]} → {days_all[-1]}) · testing {len(sel)}"
          + (f" ({sel[0]} → {sel[-1]})" if sel else ""))
    if len(sel) < 40:
        sys.exit("Need at least 40 sessions for an honest test.")
    days = {str(d): fr.Day(g.reset_index(drop=True)) for d, g in m1.groupby(m1["timestamp"].dt.date)}
    d5 = im.resample(m1, int(P["tf_minutes"]))
    bn1 = bn.load()
    r5 = im.resample(bn1, int(P["tf_minutes"])) if len(bn1) else None
    setups = im.detect_all(d5, P, r5)
    setups = [s for s in setups if fr.in_range(pd.Timestamp(s["day"]).date(), after, before)]
    print(f"ICT model found {len(setups)} setups ({sum(s['side'] == 'BULLISH' for s in setups)} buy / "
          f"{sum(s['side'] == 'BEARISH' for s in setups)} sell) on {len({s['day'] for s in setups})} sessions\n")
    mt, pm = int(V["min_trades"]), float(V["p_max"])
    out = dict(generated=datetime.now().isoformat(timespec="seconds"), tag=a.tag, from_=str(sel[0]), to=str(sel[-1]),
               sessions=len(sel), setups=len(setups), rules={k: P[k] for k in ("entry", "tp", "mode", "liquidity", "disp_body_atr",
                                                                              "min_fvg_pts", "max_trades_day")},
               primary=dict(entry=a.entry, tp=a.tp, mode=a.mode, pd_filter=a.pd))

    # ── primary variant ────────────────────────────────────────────────
    tr = simulate(setups, days, P, a.entry, a.tp, a.mode, after=after, before=before, pdf=a.pd, smt=a.smt)
    print(f"PRIMARY: limit at FVG-{a.entry.upper()} · target {a.tp}{'R' if a.tp != 'liq' else ' (next liquidity)'} · "
          f"management {a.mode} · premium/discount filter: {a.pd} · {a.reps} random reps")
    print(HEAD)
    head = analyse(tr, days, P, a.mode, a.reps, mt, pm)
    print(_row("ALL TRADES", head))
    out["headline"] = head
    out["windows"], out["sides"] = {}, {}
    if not tr.empty:
        for w in list(P["windows"]) + ["OUT"]:
            sub = tr[tr["window"] == w]
            if len(sub):
                s = analyse(sub, days, P, a.mode, max(200, a.reps // 3), mt, pm)
                out["windows"][w] = s
                print(_row("window " + w, s))
        for sd in ("BULLISH", "BEARISH"):
            sub = tr[tr["side"] == sd]
            if len(sub):
                s = analyse(sub, days, P, a.mode, max(200, a.reps // 3), mt, pm)
                out["sides"][sd] = s
                print(_row("side " + ("BUY" if sd == "BULLISH" else "SELL"), s))
        for k in ("level",):
            for v, sub in tr.groupby(k):
                print(_row(f"pool {v}", analyse(sub, days, P, a.mode, 200, mt, pm)))
        out["trades_csv"] = f"ict{('_' + a.tag) if a.tag else ''}_trades.csv"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tr.drop(columns=["start_i"]).to_csv(OUT_DIR / out["trades_csv"], index=False)
    # ── variant grid ───────────────────────────────────────────────────
    out["grid"] = []
    if not a.no_grid and setups:
        print(f"\nVARIANT GRID (all {len(list(product(['fvg', 'ce', 'ote'], ['2', '3', 'liq'], ['tp', 'be75'])))} "
              f"variants, {V['grid_reps']} reps each — transparency, not a menu to pick the winner from)")
        print(HEAD)
        npass = 0
        for en, tp_, md in product(["fvg", "ce", "ote"], ["2", "3", "liq"], ["tp", "be75"]):
            t2 = simulate(setups, days, P, en, tp_, md, after=after, before=before, pdf=a.pd)
            s = analyse(t2, days, P, md, int(V["grid_reps"]), mt, pm)
            npass += int(bool(s.get("passed")))
            out["grid"].append(dict(entry=en, tp=tp_, mode=md, **{k: s.get(k) for k in (
                "n", "win_rate", "avg_R", "total_R", "p_time", "p_side", "half1_avg_R", "half2_avg_R", "passed")}))
            print(_row(f"{en} · {tp_}{'R' if tp_ != 'liq' else ''} · {md}", s))
        out["grid_passed"] = npass
        print(f"  → {npass} of 18 variants pass; about {18 * 0.05 * 0.05:.2f} would be expected by luck on both controls "
              f"(0.05 × 0.05 × 18), so a single lone pass is weak evidence.")
    # ── premium / discount filter on the primary variant ───────────────
    out["pd_variants"] = {}
    if setups:
        print("\nPREMIUM / DISCOUNT FILTER on the primary variant (buy only in discount, sell only in premium)")
        print(HEAD)
        for f in ("none", "eq", "open"):
            t3 = simulate(setups, days, P, a.entry, a.tp, a.mode, after=after, before=before, pdf=f)
            s = analyse(t3, days, P, a.mode, max(200, a.reps // 3), mt, pm)
            out["pd_variants"][f] = s
            print(_row(f"filter {f}", s))
    # ── SMT split (Bank Nifty did not confirm the sweep) ───────────────
    out["smt_split"] = {}
    if len(bn1) and not tr.empty and "smt" in tr:
        print("\nSMT SPLIT on the primary variant (SMT = Bank Nifty did NOT take its matching low/high)")
        print(HEAD)
        for lab, sub in (("SMT present", tr[tr["smt"] == True]), ("no SMT", tr[tr["smt"] == False]),   # noqa: E712
                         ("unknown", tr[tr["smt"].isna()])):
            if len(sub):
                s = analyse(sub, days, P, a.mode, max(200, a.reps // 3), mt, pm)
                out["smt_split"][lab] = s
                print(_row(lab, s))
    # ── time-window study ──────────────────────────────────────────────
    ws = window_study(m1[m1["timestamp"].dt.date.isin(sel)])
    out["window_study"] = ws
    print("\nTIME-WINDOW STUDY (no strategy) — per 30-minute bucket: avg range · share of the day's range · "
          "does the PREVIOUS 30 min continue (+) or reverse (−)?")
    print(f"  {'bucket':<8}{'days':>6}{'avg range':>11}{'% of day':>10}{'continue pts':>14}{'t':>7}{'p':>8}")
    for r in ws:
        flag = "  ◀" if (r["cont_p"] is not None and r["cont_p"] < 0.05) else ""
        print(f"  {r['bucket']:<8}{r['days']:>6}{r['avg_range']:>11.1f}{100 * r['share_of_day']:>9.1f}%"
              f"{(r['cont_pts'] if r['cont_pts'] is not None else float('nan')):>+14.2f}"
              f"{(r['cont_t'] if r['cont_t'] is not None else float('nan')):>7.2f}"
              f"{(r['cont_p'] if r['cont_p'] is not None else float('nan')):>8.3f}{flag}")
    nsig = sum(1 for r in ws if r["cont_p"] is not None and r["cont_p"] < 0.05)
    out["window_study_sig"] = nsig
    print(f"  → {nsig} of {len(ws)} buckets show p<0.05; about {0.05 * len(ws):.1f} would by luck alone.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"ict{('_' + a.tag) if a.tag else ''}.json"
    out["from"] = out.pop("from_")
    path.write_text(json.dumps(_clean(out), indent=2))
    print(f"\nSaved {path}. A model passes only with positive average R, p<0.05 against BOTH random controls and both "
          f"halves positive. Information only — not financial advice.")


if __name__ == "__main__":
    main()
