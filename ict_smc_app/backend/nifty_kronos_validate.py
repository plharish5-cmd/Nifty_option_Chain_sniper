# NIFTY-specific Kronos validation — Steps 2 and 3 of the brief, in one script, to run on YOUR
# machine with the desk venv (torch is already there) and your Dhan-fetched hourly history.
#
# It does NOT call Dhan and it NEVER touches your token. It reads an hourly CSV you export first
# (see --csv), runs the SAME sample_paths loop your desk uses, and compares Kronos against random
# paths resampled from NIFTY's own past hourly returns — the identical method as the published
# Kronos backtest (compare_random.py), so the comparison is apples-to-apples.
#
#   # Step 2 — one real forecast, raw numbers:
#   ./ict_smc_app/backend/.venv/bin/python nifty_kronos_validate.py --csv NIFTY_1h.csv --step2
#
#   # Step 3 — honest walk-forward edge test vs random (~200 reps):
#   ./ict_smc_app/backend/.venv/bin/python nifty_kronos_validate.py --csv NIFTY_1h.csv --step3 --days 200
#
# The NIFTY CSV must be the desk's basis-adjusted FUTURE-volume hourly series (market/futures.py
# mapped()/combined_1m()), columns date,open,high,low,close,volume, date = IST candle start.
# Thresholds come from rules.json (never hardcoded); missing keys fall back to the shipped defaults.
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kronos_paths as kp
from backtest_hourly_data import load_hourly, SLOTS

DEFAULTS = dict(hist_candles=360, pred_candles=24, n_paths=30, T=1.0, top_p=0.9,
                eval_candle=7, random_reps=200, band_pct=90, vol_coverage_min=0.95)


def load_rules(path):
    r = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            j = json.load(open(path))
            k = j.get("kronos", j)
            for key in DEFAULTS:
                if key in k:
                    r[key] = k[key]
            print(f"[rules] loaded {path}; kronos block: "
                  + ", ".join(f"{key}={r[key]}" for key in DEFAULTS))
        except Exception as e:
            print(f"[rules] could not parse {path} ({e}); using defaults")
    else:
        print("[rules] no rules.json given; using shipped defaults "
              "(move these into rules.json before shipping)")
    # hard ceiling from _infer_fast assert: hist + pred <= 512
    if r["hist_candles"] + r["pred_candles"] > 512:
        raise SystemExit(f"[rules] hist_candles({r['hist_candles']}) + pred_candles"
                         f"({r['pred_candles']}) = {r['hist_candles']+r['pred_candles']} > 512; "
                         "the fast loop will assert. Lower hist_candles to <= "
                         f"{512 - r['pred_candles']}.")
    return r


def volume_coverage(past, hist):
    v = past["volume"].values[-hist:]
    return float((v > 0).mean())


def step2(df, tok, mdl, r):
    hist, pred, n = r["hist_candles"], r["pred_candles"], r["n_paths"]
    if len(df) < hist + 1:
        raise SystemExit(f"only {len(df)} candles; need {hist}+1")
    past = df.iloc[-hist:]
    last = float(past.close.iloc[-1])
    cov = volume_coverage(df, hist)
    use_vol = cov >= r["vol_coverage_min"]
    cols = ["open", "high", "low", "close", "volume"] if use_vol else ["open", "high", "low", "close"]
    # future slots (weekends skipped; NSE holidays NOT — labels only, see audit B5)
    fut_ts = _next_slots(past.date.iloc[-1], pred)
    seed = int(fut_ts.iloc[0].strftime("%Y%m%d"))
    P = kp.sample_paths(tok, mdl, past[cols], past.date, fut_ts, n_paths=n, T=r["T"],
                        top_p=r["top_p"], seed=seed)
    end = P[:, -1, 3]
    print("\n================ STEP 2: ONE REAL NIFTY FORECAST ================")
    print(f"as of {past.date.iloc[-1]}  last close {last:.2f}")
    print(f"volume coverage over {hist}-bar context: {cov:.1%}  -> "
          f"{'USED' if use_vol else 'DROPPED (price-only forecast)'}")
    print(f"\n{n} run endpoints (close at +{pred} candles):")
    print("  " + "  ".join(f"{x:.0f}" for x in np.sort(end)))
    up = int((end > last).sum())
    print(f"\n  up/down split : {up} up / {n-up} down   (up share {up/n:.0%})")
    print(f"  mean endpoint : {end.mean():.2f}  ({(end.mean()/last-1)*100:+.2f}%)")
    print(f"  band (min..max): {end.min():.2f} .. {end.max():.2f}")
    print(f"  90% band       : {np.percentile(end,5):.2f} .. {np.percentile(end,95):.2f}")
    if use_vol:
        fcvol = P[:, :, 4].sum(1).mean()
        print(f"  forecast total volume over horizon (mean of runs): {fcvol:,.0f}")
    print("================================================================\n")


def _next_slots(last_ts, n):
    out, d, k = [], last_ts.normalize(), SLOTS.index(last_ts.strftime("%H:%M")) + 1
    while len(out) < n:
        if k == len(SLOTS):
            d, k = d + pd.Timedelta(days=1), 0
            while d.weekday() >= 5:
                d += pd.Timedelta(days=1)
            continue
        hh, mm = map(int, SLOTS[k].split(":"))
        out.append(d + pd.Timedelta(hours=hh, minutes=mm))
        k += 1
    return pd.Series(out)


def step3(df, tok, mdl, r):
    hist, pred, n, H = r["hist_candles"], r["pred_candles"], r["n_paths"], r["eval_candle"]
    REPS, days = r["random_reps"], args.days
    first = df.groupby("day").head(1).index.values
    origins = [int(i) for i in first if i >= hist and i + pred <= len(df)][-days:]
    if not origins:
        raise SystemExit("not enough history for even one walk-forward origin")
    print(f"\n================ STEP 3: HONEST WALK-FORWARD ({len(origins)} origins) ================")
    rng = np.random.default_rng(2026)
    k_up, k_in90, k_inmm = [], [], []
    rw_up = np.zeros((REPS, len(origins)))
    rw_in90 = np.zeros((REPS, len(origins)))
    act_up = []
    kc_fcvol, kc_actvol = [], []
    for j, i in enumerate(origins):
        past, fut = df.iloc[i - hist:i], df.iloc[i:i + pred]
        last = float(past.close.iloc[-1])
        act = float(fut.close.iloc[H - 1])
        cov = volume_coverage(df.iloc[:i], hist)
        cols = ["open", "high", "low", "close", "volume"] if cov >= r["vol_coverage_min"] else ["open", "high", "low", "close"]
        seed = int(fut.date.iloc[0].strftime("%Y%m%d"))
        P = kp.sample_paths(tok, mdl, past[cols], past.date, fut.date.reset_index(drop=True),
                            n_paths=n, T=r["T"], top_p=r["top_p"], seed=seed)
        end = P[:, H - 1, 3]
        k_up.append((end > last).mean() > 0.5)
        q05, q95 = np.percentile(end, [5, 95])
        k_in90.append(q05 <= act <= q95)
        k_inmm.append(end.min() <= act <= end.max())
        act_up.append(act > last)
        if cov >= r["vol_coverage_min"]:
            kc_fcvol.append(P[:, :H, 4].sum(1).mean()); kc_actvol.append(fut.volume.iloc[:H].sum())
        # random control: resample this window's own hourly log-returns
        logret = np.diff(np.log(past.close.values))
        draws = logret[rng.integers(0, len(logret), size=(REPS, n, H))]
        paths = last * np.exp(np.cumsum(draws, axis=2))
        rend = paths[:, :, H - 1]
        rw_up[:, j] = (rend > last).mean(1)
        rq05, rq95 = np.percentile(rend, [5, 95], axis=1)
        rw_in90[:, j] = (act >= rq05) & (act <= rq95)
        if (j + 1) % 25 == 0:
            print(f"  ...{j+1}/{len(origins)} origins done", flush=True)
    act_up = np.array(act_up)
    k_dir = (np.array(k_up) == act_up).mean()
    rw_dir = ((rw_up > 0.5) == act_up[None, :]).mean(1)
    coin = rng.binomial(len(origins), 0.5, 10000) / len(origins)

    def band(x):
        return f"luck range {np.percentile(x,5):.1%} .. {np.percentile(x,95):.1%}"
    print("\nQ1  direction (up/down at candle +%d):" % H)
    print(f"   Kronos         {k_dir:.1%}")
    print(f"   random paths   {rw_dir.mean():.1%}   ({band(rw_dir)})")
    print(f"   coin flip      {coin.mean():.1%}   ({band(coin)})")
    print(f"   always-up base {act_up.mean():.1%}")
    beat = (rw_dir >= k_dir).mean()
    print(f"   -> random matched or beat Kronos in {beat:.0%} of {REPS} tries")
    print("\nQ4  band coverage (target %d%%):" % r["band_pct"])
    print(f"   Kronos 90%% band   {np.mean(k_in90):.1%}   | full 30-run range {np.mean(k_inmm):.1%}")
    print(f"   random 90%% band   {rw_in90.mean():.1%}")
    if kc_fcvol:
        c = np.corrcoef(kc_fcvol, kc_actvol)[0, 1]
        print(f"\nQ3  volume: Kronos forecast-vs-actual corr {c:+.2f} "
              f"(n={len(kc_fcvol)} origins with >= {r['vol_coverage_min']:.0%} coverage)")
    else:
        print("\nQ3  volume: coverage never reached threshold on any origin -> volume forecast not evaluable")
    print("\nVERDICT GUIDE: if Kronos Q1 sits inside the random luck band and random matched/beat it")
    print("in a large share of tries, and Q4 coverage is well under %d%%, this tab has NO edge on" % r["band_pct"])
    print("NIFTY and is a visualization of uncertainty, not a signal.")
    print("====================================================================\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="NIFTY hourly CSV (future-volume-attached)")
    ap.add_argument("--rules", default="rules.json")
    ap.add_argument("--step2", action="store_true")
    ap.add_argument("--step3", action="store_true")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--model-dir", default=None, help="folder holding Kronos-base / Kronos-Tokenizer-base")
    args = ap.parse_args()
    r = load_rules(args.rules)
    df = load_hourly("NIFTY", path=args.csv)
    print(f"[data] {len(df)} candles, {df.day.nunique()} days, {df.date.iloc[0]} .. {df.date.iloc[-1]}")
    tok, mdl = kp.load(model_dir=args.model_dir)
    if args.step2 or not args.step3:
        step2(df, tok, mdl, r)
    if args.step3:
        step3(df, tok, mdl, r)
