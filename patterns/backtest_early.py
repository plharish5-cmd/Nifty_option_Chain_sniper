"""Historical test of the 15-min early read.

For every 15-min candle in your 1-min history, freeze it at minute `elapsed` (default 12),
compute the lock probability for each pattern present, then check the final candle.
The path library for each day is built ONLY from earlier days (no look-ahead).

Output: calibration table (predicted prob bucket -> actual hold rate) and per-pattern
hold rates. If the 0.85+ bucket does not hold ~85%+ of the time, do not act early on it.

Usage:
    python -m patterns.backtest_early --csv data/candles_history_1m.csv --elapsed 12 --sims 200
The CSV needs a datetime column (or index) plus open/high/low/close. Dhan 5-year 1-min
history from fetch_history.py works directly.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .common import load_rules, normalize, resample
from .early_read import TF, _eval_last, build_path_library, simulate, vol_unit


def run(df_1m: pd.DataFrame, rules: dict, elapsed: int = 12, n_sims: int = 200,
        min_lib: int = 2000) -> pd.DataFrame:
    er = rules["early_read"]
    names = list(rules["talib_patterns"])
    depth = rules["bars_for_talib"]
    r = TF - elapsed
    rng = np.random.default_rng(er["random_seed"])
    days = sorted(set(df_1m.index.date))
    libs, rows = [], []
    full15 = resample(df_1m, TF)
    full15 = full15[full15["n1m"] >= TF]  # only complete buckets are testable
    for d in days:
        day = df_1m[df_1m.index.date == d]
        lib = np.vstack(libs) if libs else np.empty((0, 3))
        libs.append(build_path_library(day, r, er["vol_lookback_1m"]))  # added AFTER testing d
        if len(lib) < min_lib:
            continue
        for start in full15.index[full15.index.date == d]:
            pos = full15.index.get_loc(start)
            if pos < depth - 1:
                continue
            hist = full15.iloc[pos - depth + 1:pos]
            part = day[(day.index >= start) & (day.index < start + pd.Timedelta(minutes=elapsed))]
            if len(part) < elapsed:
                continue
            before = day[day.index < start + pd.Timedelta(minutes=elapsed)]
            prov = {"open": part["open"].iloc[0], "high": part["high"].max(),
                    "low": part["low"].min(), "close": part["close"].iloc[-1]}
            now_vals = _eval_last(hist, (prov["open"], prov["high"], prov["low"], prov["close"]), names, depth)
            present = [n for n in names if now_vals[n] != 0]
            if not present:
                continue
            unit = vol_unit(before, er["vol_lookback_1m"])
            sims = simulate(prov, lib, unit, n_sims, rng)
            sim_vals = np.array([[_eval_last(hist, c, present, depth)[n] for n in present] for c in sims])
            fb = full15.iloc[pos]
            final = _eval_last(hist, (fb["open"], fb["high"], fb["low"], fb["close"]), present, depth)
            for j, n in enumerate(present):
                rows.append({"bucket": start, "code": n, "prob": float(np.mean(sim_vals[:, j] == now_vals[n])),
                             "held": final[n] == now_vals[n]})
    return pd.DataFrame(rows)


def report(res: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if res.empty:
        return pd.DataFrame(), pd.DataFrame()
    bins = [0, 0.5, 0.7, 0.85, 0.95, 1.0001]
    labels = ["<0.50", "0.50-0.70", "0.70-0.85", "0.85-0.95", "0.95+"]
    res = res.assign(bucket_p=pd.cut(res["prob"], bins=bins, labels=labels, right=False))
    calib = res.groupby("bucket_p", observed=False).agg(n=("held", "size"), predicted=("prob", "mean"),
                                                        actual_hold=("held", "mean")).round(3)
    per = res.groupby("code").agg(n=("held", "size"), mean_prob=("prob", "mean"),
                                  hold_rate=("held", "mean"),
                                  hold_rate_when_p85=("held", lambda s: s[res.loc[s.index, "prob"] >= 0.85].mean()),
                                  n_p85=("prob", lambda s: int((s >= 0.85).sum()))).round(3)
    return calib, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--elapsed", type=int, default=12)
    ap.add_argument("--sims", type=int, default=200)
    ap.add_argument("--out", default="data/patterns/early_backtest.csv")
    a = ap.parse_args()
    rules = load_rules()

    import glob
    target_path = Path(a.csv)
    if any(c in a.csv for c in "*?[]"):
        files = sorted(glob.glob(a.csv))
        if not files:
            raise FileNotFoundError(f"No files matched pattern: {a.csv}")
        dfs = [pd.read_csv(f) for f in files if not Path(f).name.startswith("vol_")]
        df = normalize(pd.concat(dfs, ignore_index=True))
    else:
        if not target_path.exists():
            raise FileNotFoundError(f"File not found: {a.csv}")
        df = normalize(pd.read_csv(a.csv))
        # If input has fewer than 7 days, pull prior sessions from data/candles to build path library
        day_count = len(set(df.index.date))
        if day_count < 7:
            min_date = str(df.index[0].date())
            all_files = sorted(glob.glob("data/candles/*.csv"))
            reg = [f for f in all_files if not Path(f).name.startswith("vol_")]
            prior_files = [f for f in reg if Path(f).stem < min_date]
            if prior_files:
                p_dfs = [pd.read_csv(f) for f in prior_files[-20:]]
                df = normalize(pd.concat(p_dfs + [pd.read_csv(a.csv)], ignore_index=True))
                print(f"Loaded {len(prior_files[-20:])} prior session(s) from data/candles/ to supply the empirical path library.")

    res = run(df, rules, a.elapsed, a.sims)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(a.out, index=False)
    calib, per = report(res)
    print(f"\nEarly read at minute {a.elapsed}/15, {len(res)} pattern-reads\n")
    print("Calibration (predicted vs actual):\n", calib.to_string())
    print("\nPer pattern:\n", per.to_string())
    print(f"\nRaw rows saved to {Path(a.out).resolve()}")


if __name__ == "__main__":
    main()
