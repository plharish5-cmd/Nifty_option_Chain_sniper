"""
backtest_oi_lab.py — does the Options Lab action (oi_lab_view.py) say anything about where NIFTY goes next,
and does a candlestick / price-action confirmation make it better?

For every stored option-chain day it replays the day (no look-ahead: only snapshots up to that time), computes the same
LEAN CE / LEAN PE / WAIT FOR REVERSAL / lean action that the briefing terminal shows, every `sample_every_min` minutes, and measures the
NIFTY 1-minute-candle move over the next 30 and 60 minutes, signed in the direction the action points.

Variants (all fixed before any result, in oi_lab_view_rules.json -> "backtest"):
  A  OI side            LEAN CE side / LEAN PE side
  B  OI reversal calls  WAIT FOR REVERSAL ▲ / ▼  (traded in the direction of the writers' lean once price has turned)
  C  OI any lean        every action with score at or beyond the lean level
  each one: alone · + a candlestick pattern on the last closed 15m bar pointing the same way (candle_patterns.py)
                  · + a price-action break (last closed 15m bar closes beyond the previous 15m bar's high / low, same way)
  D  candle / price action ALONE (no OI) — the baseline: does the OI add anything beyond the candle?

Control: same time-of-day slots on a RANDOM OTHER DAY, signed in the same direction, drawn day-by-day so that overlapping windows on one
day are not counted as independent evidence (500 draws). Verdicts count distinct DAYS, not overlapping signals, and the p-value is
multiplied by the number of variants tested.

  .venv/bin/python backtest_oi_lab.py
  .venv/bin/python backtest_oi_lab.py --after 2026-09-01 --before 2026-10-05 --tag recent

Saves data/backtests/oi_lab[_tag].json (the briefing terminal shows its headline) and oi_lab[_tag]_events.csv.
Information only — not financial advice.   Python 3.9 compatible.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_BT = {"horizons_min": [30, 60], "primary_h": 60, "sample_every_min": 15, "start": "10:00", "end": "14:45", "max_snapshot_age_min": 6,
       "reps": 2000, "min_days": 15, "p_max": 0.05, "context_days": 6}


def bt_rules() -> dict:
    import oi_lab_view as ov
    try:
        r = json.loads(ov.RULES_PATH.read_text()).get("backtest", {})
    except Exception:
        r = {}
    return {**_BT, **{k: v for k, v in r.items() if not k.startswith("_")}}


def _dir_of(action: str) -> int:
    a = str(action)
    if "▲" in a or a.startswith(("BUY CE", "LEAN CE side")):
        return 1
    if "▼" in a or a.startswith(("BUY PE", "LEAN PE side")):
        return -1
    return 0


def _slot_times(day: pd.Timestamp, B: dict):
    t = datetime.combine(day.date(), datetime.strptime(B["start"], "%H:%M").time())
    end = datetime.combine(day.date(), datetime.strptime(B["end"], "%H:%M").time())
    while t <= end:
        yield t
        t += timedelta(minutes=int(B["sample_every_min"]))


def _confirm(bars: pd.DataFrame, D: dict, cp, t: datetime):
    """(bull_pattern, bear_pattern, bull_break, bear_break) on the last closed 15m bar at time t, or None if there is none today."""
    if bars is None or bars.empty:
        return None
    ok = (bars["ct"] <= pd.Timestamp(t)).to_numpy() & (bars["timestamp"].dt.date == t.date()).to_numpy()
    if not ok.any():
        return None
    i = int(np.where(ok)[0][-1])
    drift = D["_drift"][i]
    bp = br = False
    for n in cp.PATTERNS:
        if D[n][i]:
            d = cp.direction(n, float(drift) if np.isfinite(drift) else 0.0)
            bp |= d > 0
            br |= d < 0
    kb = ks = False
    if i > 0 and bars["timestamp"].dt.date.iloc[i - 1] == t.date():
        kb = bool(bars["close"].iloc[i] > bars["high"].iloc[i - 1])
        ks = bool(bars["close"].iloc[i] < bars["low"].iloc[i - 1])
    return bp, br, kb, ks


def collect(args) -> pd.DataFrame:
    import config
    import store
    import smc_brief
    import htf_candles as hc
    import candle_patterns as cp
    import options_lab_tab as ol
    import oi_lab_view as ov

    B = bt_rules()
    R = ov.rules()
    rl = {**ol._rules(), "lookback_minutes": int(R["lookback_minutes"])}
    today = f"{datetime.now():%Y-%m-%d}"
    m1 = smc_brief._read_1m(4000, None)
    if m1 is None or m1.empty:
        print("No stored 1-minute NIFTY candles (data/candles_hist). Run backtest_five_rule.py --fetch first.")
        return pd.DataFrame()
    m1_days = {d: g.reset_index(drop=True) for d, g in m1.groupby(m1["timestamp"].dt.strftime("%Y-%m-%d"))}
    all_days = sorted(m1_days)
    if getattr(args, "price_only", False):
        days = [d for d in all_days if len(m1_days[d]) >= 300 and d < today and (not args.after or d >= args.after) and (not args.before or d < args.before)]
    else:
        root = Path(config.SNAPSHOT_DIR)
        days = sorted(p.name for p in root.iterdir() if p.is_dir() and len(p.name) == 10 and any(p.glob("*.parquet")))
        days = [d for d in days if (args.include_today or d < today) and (not args.after or d >= args.after) and (not args.before or d < args.before)]
    if not days:
        print("No days to test for that range.")
        return pd.DataFrame()
    Rc = cp.rules()
    H = [int(h) for h in B["horizons_min"]]
    rows = []
    for di, d in enumerate(days):
        if d not in m1_days:
            print(f"  {d}: no 1-minute candles stored — skipped")
            continue
        g = m1_days[d]
        PO = bool(getattr(args, "price_only", False))
        snaps, s_full = [], None
        if not PO:
            snaps = store.load_day(pd.Timestamp(d).date())
            if len(snaps) < 5:
                print(f"  {d}: only {len(snaps)} snapshots — skipped")
                continue
            try:
                s_full = ol.build_series(snaps, rl)
            except Exception as e:                              # noqa: BLE001
                print(f"  {d}: series failed ({e}) — skipped")
                continue
        # context for candle patterns: a few earlier sessions + this one (ATR and prior-move need history)
        pos = all_days.index(d)
        ctx = pd.concat([m1_days[x] for x in all_days[max(0, pos - int(B["context_days"])): pos + 1]], ignore_index=True)
        bars = hc.htf(ctx, 15)
        Dp = cp.detect(bars, Rc) if len(bars) else None
        ts_arr = g["timestamp"].to_numpy()
        cl = g["close"].to_numpy(float)
        n_d = 0
        for t in _slot_times(pd.Timestamp(d), B):
            prefix = [x for x in snaps if x[0] <= t]
            if not PO and (len(prefix) < 5 or (t - prefix[-1][0]).total_seconds() > 60 * float(B["max_snapshot_age_min"])):
                continue
            tt = np.datetime64(t)
            i0 = int(np.searchsorted(ts_arr, tt, side="left")) - 1                     # last 1-minute bar that closed before t
            if i0 < 0 or (tt - ts_arr[i0]) > np.timedelta64(3, "m"):
                continue
            mv = {}
            for h in H:
                th = np.datetime64(t + timedelta(minutes=h))
                i1 = int(np.searchsorted(ts_arr, th, side="left")) - 1
                if i1 > i0 and (th - ts_arr[i1]) <= np.timedelta64(3, "m") and pd.Timestamp(ts_arr[i1]).date() == t.date():
                    mv[h] = float(cl[i1] - cl[i0])
                else:
                    mv[h] = np.nan
            if PO:
                res = {"ok": True, "action": "-", "total": 0}
            else:
                res = ov.compute_from(prefix, t, s_full, R)
            if not res.get("ok"):
                continue
            conf = _confirm(bars, Dp, cp, t) if Dp is not None else None
            rec = dict(day=d, t=f"{t:%H:%M}", slot=f"{t:%H:%M}", action=res["action"], score=res["total"], oi_dir=_dir_of(res["action"]),
                       bull_pat=bool(conf and conf[0]), bear_pat=bool(conf and conf[1]), bull_brk=bool(conf and conf[2]), bear_brk=bool(conf and conf[3]),
                       has_conf=conf is not None)
            for h in H:
                rec[f"mv{h}"] = mv[h]
            rows.append(rec)
            n_d += 1
        if not PO or di % 50 == 0:
            print(f"  {d}: {n_d} sample times")
    return pd.DataFrame(rows)


# ── variants (fixed before any result) ────────────────────────────────────────────────────────────────────────────────────
def variants():
    def conf_dir(r, kind):
        bull, bear = (r["bull_pat"], r["bear_pat"]) if kind == "pat" else (r["bull_brk"], r["bear_brk"])
        return 1 if (bull and not bear) else (-1 if (bear and not bull) else 0)

    base = {
        "A OI side (LEAN CE / LEAN PE)": lambda r: r["oi_dir"] if str(r["action"]).startswith(("BUY", "LEAN CE side", "LEAN PE side")) else 0,
        "B OI reversal calls": lambda r: r["oi_dir"] if str(r["action"]).startswith("WAIT FOR REVERSAL") else 0,
        "C OI any lean": lambda r: r["oi_dir"],
    }
    out = {}
    for nm, f in base.items():
        out[f"{nm} · alone"] = f
        out[f"{nm} · + candle pattern"] = (lambda f: lambda r: f(r) if (f(r) != 0 and r["has_conf"] and conf_dir(r, "pat") == f(r)) else 0)(f)
        out[f"{nm} · + price-action break"] = (lambda f: lambda r: f(r) if (f(r) != 0 and r["has_conf"] and conf_dir(r, "brk") == f(r)) else 0)(f)
    out["D candle pattern ALONE (no OI)"] = lambda r: conf_dir(r, "pat") if r["has_conf"] else 0
    out["D price-action break ALONE (no OI)"] = lambda r: conf_dir(r, "brk") if r["has_conf"] else 0
    return out


def analyse(df: pd.DataFrame, f, h: int, reps: int, seed: int = 7):
    """Signed forward move of the variant's signals vs the same slots on random other days. Returns a dict or None."""
    col = f"mv{h}"
    d = df.dropna(subset=[col]).copy()
    d["dir"] = d.apply(f, axis=1)
    ev = d[d["dir"] != 0]
    if ev.empty:
        return None
    pool = {k: g.set_index("slot")[col].to_dict() for k, g in d.groupby("day")}            # day -> slot -> move
    days_all = sorted(pool)
    ev_days = sorted(ev["day"].unique())
    sig = (ev["dir"] * ev[col]).to_numpy()
    mean_e = float(sig.mean())
    hit_e = float((sig > 0).mean() * 100)
    # expected value under "random day, same slot, same direction" (deterministic) — also used for the halves test
    slot_mean = d.groupby("slot")[col].mean().to_dict()
    exp_e = np.array([r.dir * slot_mean.get(r.slot, 0.0) for r in ev.itertuples()])
    rng = np.random.default_rng(seed)
    by_day = {k: list(zip(g["slot"], g["dir"])) for k, g in ev.groupby("day")}
    ctrl = np.empty(reps)
    ctrl_hit = np.empty(reps)
    for k in range(reps):
        vals = []
        for dd, sl in by_day.items():
            others = [x for x in days_all if x != dd] or days_all
            r_day = others[int(rng.integers(len(others)))]
            for slot, dr in sl:
                v = pool[r_day].get(slot)
                if v is None or not np.isfinite(v):
                    v = pool[others[int(rng.integers(len(others)))]].get(slot)
                if v is not None and np.isfinite(v):
                    vals.append(dr * v)
        arr = np.array(vals) if vals else np.array([0.0])
        ctrl[k] = arr.mean()
        ctrl_hit[k] = (arr > 0).mean() * 100
    p_hi = float((np.sum(ctrl >= mean_e) + 1) / (reps + 1))
    p_lo = float((np.sum(ctrl <= mean_e) + 1) / (reps + 1))
    mid = ev_days[len(ev_days) // 2]
    e1 = ev["day"] < mid
    h1 = float((sig[e1.to_numpy()] - exp_e[e1.to_numpy()]).mean()) if e1.any() else float("nan")
    h2 = float((sig[~e1.to_numpy()] - exp_e[~e1.to_numpy()]).mean()) if (~e1).any() else float("nan")
    return dict(n=int(len(ev)), n_days=int(len(ev_days)), hit=hit_e, hit_ctrl=float(ctrl_hit.mean()), mean=mean_e, ctrl=float(ctrl.mean()),
                edge=mean_e - float(ctrl.mean()), p_hi=p_hi, p_lo=p_lo, half1=h1, half2=h2)


def oi_adds(df: pd.DataFrame, kind: str, h: int, boots: int = 1000, seed: int = 11):
    """Among the candle / price-action signals (no OI used to pick them): is the move better when the OI action AGREES with the candle's direction
    than when OI is neutral or opposite?  Day-bootstrap (whole days resampled) so overlapping signals on a day are not counted as independent."""
    col = f"mv{h}"
    d = df.dropna(subset=[col]).copy()
    if kind == "pat":
        d["dir"] = np.where(d["has_conf"] & (d["bull_pat"] ^ d["bear_pat"]), np.where(d["bull_pat"], 1, -1), 0)
    else:
        d["dir"] = np.where(d["has_conf"] & (d["bull_brk"] ^ d["bear_brk"]), np.where(d["bull_brk"], 1, -1), 0)
    ev = d[d["dir"] != 0].copy()
    if ev.empty:
        return None
    ev["sig"] = ev["dir"] * ev[col]
    ev["agree"] = ev["oi_dir"] == ev["dir"]
    a, b = ev[ev["agree"]], ev[~ev["agree"]]
    if len(a) < 10 or len(b) < 10:
        return dict(n_agree=int(len(a)), n_other=int(len(b)), verdict="FEW")
    diff = float(a["sig"].mean() - b["sig"].mean())
    days = sorted(ev["day"].unique())
    groups = {k: g for k, g in ev.groupby("day")}
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(boots):
        pick = [groups[days[int(i)]] for i in rng.integers(0, len(days), len(days))]
        x = pd.concat(pick)
        xa, xb = x[x["agree"]], x[~x["agree"]]
        if len(xa) and len(xb):
            bs.append(xa["sig"].mean() - xb["sig"].mean())
    bs = np.array(bs)
    lo, hi = float(np.percentile(bs, 5)), float(np.percentile(bs, 95))
    return dict(n_agree=int(len(a)), n_other=int(len(b)), mean_agree=float(a["sig"].mean()), mean_other=float(b["sig"].mean()), diff=diff, lo=lo, hi=hi,
                verdict="OI ADDS" if lo > 0 else ("OI HURTS" if hi < 0 else "NOT SHOWN"))


def score_buckets(df: pd.DataFrame, h: int):
    """Mean NIFTY move (points, up = +) by OI score bucket — a diagnostic with nothing tuned: if the score carried information the means would rise with it."""
    col = f"mv{h}"
    d = df.dropna(subset=[col])
    edges = [(-99, -6, "score ≤ -6"), (-5, -3, "-5 … -3"), (-2, 2, "-2 … +2"), (3, 5, "+3 … +5"), (6, 99, "score ≥ +6")]
    rows = []
    for lo, hi, nm in edges:
        g = d[(d["score"] >= lo) & (d["score"] <= hi)]
        if len(g):
            rows.append(dict(bucket=nm, n=int(len(g)), days=int(g["day"].nunique()), mean_up=float(g[col].mean()), up_pct=float((g[col] > 0).mean() * 100)))
    return rows, float(d[col].mean())


def verdict(a, n_tests: int, B: dict) -> str:
    if a is None or a["n_days"] < int(B["min_days"]):
        return "FEW"
    same = (np.sign(a["half1"]) == np.sign(a["half2"])) and a["half1"] != 0
    padj = min(1.0, a["p_hi"] * n_tests)
    if a["edge"] > 0 and padj < float(B["p_max"]) and same and a["half1"] > 0:
        return "EDGE"
    if a["edge"] > 0 and a["p_hi"] < float(B["p_max"]) and same and a["half1"] > 0:
        return "LEAD"
    if a["edge"] < 0 and a["p_lo"] < float(B["p_max"]) and same and a["half1"] < 0:
        return "INVERSE"
    return "NONE"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after")
    ap.add_argument("--before")
    ap.add_argument("--tag", default="")
    ap.add_argument("--reps", type=int)
    ap.add_argument("--include-today", action="store_true")
    ap.add_argument("--price-only", action="store_true", help="test only the candle / price-action variants (no option-chain needed) on every stored 1-minute session")
    args = ap.parse_args()
    B = bt_rules()
    if args.reps:
        B["reps"] = args.reps
    import config
    if args.price_only and not args.tag:
        args.tag = "price_only"
    print("Replaying stored days (no look-ahead) …" if args.price_only else "Replaying stored option-chain days (no look-ahead) …")
    df = collect(args)
    if df.empty:
        return 1
    sessions = df["day"].nunique()
    print(f"\n{len(df)} sample times over {sessions} sessions ({df['day'].min()} → {df['day'].max()}), every {B['sample_every_min']} min {B['start']}–{B['end']}")
    if not args.price_only:
        print("How often each action showed up:")
        print(df["action"].value_counts().to_string())
        fire = float((df["action"].astype(str).str.startswith(("BUY", "LEAN CE side", "LEAN PE side"))).mean() * 100)
        print(f"-> a LEAN CE / LEAN PE side is printed at {fire:.0f}% of sample times: a signal that is on most of the time cannot be selective.")
    V = variants()
    if args.price_only:
        V = {k: f for k, f in V.items() if k.startswith("D ")}
    H = [int(h) for h in B["horizons_min"]]
    # number of tests = variants with at least min_days days of signals (multiple-test correction)
    probe = {}
    for nm, f in V.items():
        for h in H:
            probe[(nm, h)] = analyse(df, f, h, 40)
    n_tests = max(1, sum(1 for a in probe.values() if a is not None and a["n_days"] >= int(B["min_days"])))
    res, lines = {}, []
    hdr = f"{'variant':<50}{'H':>4}{'n':>5}{'days':>5}{'hit%':>6}{'ctl%':>6}{'mean':>8}{'ctl':>8}{'edge':>8}{'p':>7}{'p_adj':>7}{'½1':>7}{'½2':>7}  verdict"
    print(f"\nSigned NIFTY move after the signal, in POINTS (long = +). Control = same slots, random other day. Tests counted: {n_tests}\n{hdr}")
    for nm, f in V.items():
        res[nm] = {}
        for h in H:
            a = analyse(df, f, h, int(B["reps"]))
            vd = verdict(a, n_tests, B)
            if a is None:
                print(f"{nm:<50}{h:>4}{'0':>5}{'0':>5}  no signals  FEW")
                res[nm][str(h)] = dict(verdict="FEW", n=0, n_days=0)
                continue
            a["verdict"] = vd
            a["p_adj"] = min(1.0, a["p_hi"] * n_tests)
            res[nm][str(h)] = a
            print(f"{nm:<50}{h:>4}{a['n']:>5}{a['n_days']:>5}{a['hit']:>6.0f}{a['hit_ctrl']:>6.0f}{a['mean']:>+8.1f}{a['ctrl']:>+8.1f}{a['edge']:>+8.1f}"
                  f"{a['p_hi']:>7.3f}{a['p_adj']:>7.3f}{a['half1']:>+7.1f}{a['half2']:>+7.1f}  {vd}")
    flat = [(nm, int(h), a) for nm, d in res.items() for h, a in d.items()]
    edge = [x for x in flat if x[2].get("verdict") == "EDGE"]
    lead = [x for x in flat if x[2].get("verdict") == "LEAD"]
    inv = [x for x in flat if x[2].get("verdict") == "INVERSE"]
    few = all(x[2].get("verdict") == "FEW" for x in flat)
    if edge:
        n, h, a = edge[0]
        head = f"EDGE on {sessions} sessions: {n} ({h} min) {a['mean']:+.1f} pts vs {a['ctrl']:+.1f} at random times, p_adj {a['p_adj']:.3f}"
    elif lead:
        n, h, a = lead[0]
        head = f"LEAD only (not proven) on {sessions} sessions: {n} ({h} min) {a['mean']:+.1f} pts vs {a['ctrl']:+.1f} at random times, p {a['p_hi']:.3f} but p_adj {a['p_adj']:.2f}"
    elif inv:
        n, h, a = inv[0]
        head = f"INVERSE on {sessions} sessions: {n} ({h} min) did the opposite, {a['mean']:+.1f} pts vs {a['ctrl']:+.1f} at random times"
    elif few:
        head = f"too few sessions ({sessions}) to judge — need {int(B['min_days'])}+ days with signals per variant"
    else:
        head = f"no edge: none of {n_tests} variants beat random times on {sessions} sessions"
    extra = {}
    if not args.price_only:
        print("\nDoes the OI action ADD to the candle? (candle / price-action signals only; move when OI agrees vs when OI is neutral or opposite; day-bootstrap 90% interval)")
        for kind, nm in (("pat", "candle pattern"), ("brk", "price-action break")):
            for h in H:
                r_ = oi_adds(df, kind, h)
                extra[f"{kind}_{h}"] = r_
                if r_ is None:
                    continue
                if r_["verdict"] == "FEW":
                    print(f"  {nm:<20}{h:>3} min: OI agrees n={r_['n_agree']}, other n={r_['n_other']} — too few")
                else:
                    print(f"  {nm:<20}{h:>3} min: OI agrees {r_['mean_agree']:+.1f} pts (n={r_['n_agree']}) vs other {r_['mean_other']:+.1f} (n={r_['n_other']})  diff {r_['diff']:+.1f}  [{r_['lo']:+.1f}, {r_['hi']:+.1f}]  {r_['verdict']}")
        print("\nMean NIFTY move by OI score bucket (points, up = +; nothing tuned — a useful score would rise from top to bottom):")
        for h in H:
            rows_b, base = score_buckets(df, h)
            extra[f"buckets_{h}"] = rows_b
            print(f"  {h} min  (all sample times: {base:+.1f} pts)")
            for r_ in rows_b:
                print(f"    {r_['bucket']:<12} n={r_['n']:>4}  days={r_['days']:>3}  up {r_['mean_up']:+6.1f} pts  up-moves {r_['up_pct']:.0f}%")
    try:
        for h in H:
            oi = res["C OI any lean · alone"][str(h)]
            oic = res["C OI any lean · + candle pattern"][str(h)]
            oib = res["C OI any lean · + price-action break"][str(h)]
            if oi.get("n") and oic.get("n") and oib.get("n"):
                print(f"Does the candle help? {h} min, any-lean signals: alone {oi['mean']:+.1f} pts (n={oi['n']}) · +pattern {oic['mean']:+.1f} (n={oic['n']}) · +break {oib['mean']:+.1f} (n={oib['n']})")
    except Exception:                                           # noqa: BLE001
        pass
    if not args.price_only:
        try:
            oi_none = all(res[k][str(h)].get("verdict") in ("NONE", "FEW") for k in ("A OI side (LEAN CE / LEAN PE) · alone", "C OI any lean · alone") for h in H)
            adds = [f"{k}" for k, v in extra.items() if v and v.get("verdict") == "OI ADDS"]
            tail = "OI alone: no edge" if oi_none else "OI alone: see table"
            if lead or edge:
                base_break = res.get("D price-action break ALONE (no OI)", {}).get(str(H[0]), {})
                if base_break.get("mean") is not None and not adds:
                    tail += f"; the same break/candle confirmation ALONE gives {base_break['mean']:+.1f} pts, so OI is not shown to add"
            head = head + " | " + tail
        except Exception:                                       # noqa: BLE001
            pass
    print("\nHEADLINE:", head)
    out_dir = Path(config.DATA_DIR) / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    (out_dir / f"oi_lab{tag}.json").write_text(json.dumps(dict(
        headline=head, sessions=int(sessions), period=[df["day"].min(), df["day"].max()], n_tests=int(n_tests),
        passed=[[n, h] for n, h, _ in edge], lead=[[n, h] for n, h, _ in lead], inverse=[[n, h] for n, h, _ in inv],
        variants=res, diagnostics=extra, price_only=bool(args.price_only), generated=f"{datetime.now():%Y-%m-%d %H:%M}"), indent=1, default=float))
    df.to_csv(out_dir / f"oi_lab{tag}_events.csv", index=False)
    if args.price_only:
        print(f"Saved {out_dir / f'oi_lab{tag}.json'} (price-action only; the terminal's OPTIONS LAB headline comes from the full run without --price-only).")
    else:
        print(f"Saved {out_dir / f'oi_lab{tag}.json'} — the briefing terminal shows the headline under the OPTIONS LAB action table.")
    print("A small sample is a lead, not a verdict. Information only — not financial advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
