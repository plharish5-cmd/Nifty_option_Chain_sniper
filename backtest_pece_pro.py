"""
backtest_pece_pro.py — does the PE − CE table's Signal / ★ PRO READ carry any edge on YOUR stored days?

For every stored session: rebuild the PE − CE table (ALL strikes — no look-ahead from "latest ATM"), take every candle's
Signal and PRO READ tag, and measure what NIFTY did over the next 15 and 30 minutes, signed in the tag's direction
(bull = +, bear = −).  Rows after 15:30, expiry-unwind rows and candles whose look-ahead would cross 15:30 are skipped.

  .venv/bin/python backtest_pece_pro.py                 # 3-minute candles, all stored days
  .venv/bin/python backtest_pece_pro.py --tf 5 --days 40
Writes data/backtest_pece_pro.csv (one row per candle) and prints a summary.

How to read the summary: "avg30" = average points NIFTY moved in the tag's direction over 30 min. "hit30" = % of times
it moved the right way. A tag is worth trusting only if it has ≥ 30 cases, hit% clearly above 50 and avg clearly above
the "ALL directional" baseline — and stays that way when you add more days.
"""
from __future__ import annotations

import argparse
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

import config
import options_lab_tab as ol
import pe_ce_panel as pc
import store


def one_day(d, tf: int) -> pd.DataFrame:
    snaps = store.load_day(d)
    if len(snaps) < 20:
        return pd.DataFrame()
    s = pc.per_snapshot(snaps, None, 1.0)
    if s.empty:
        return s
    t = pc.table_rows(s, tf)
    if "pro" not in t:
        raise SystemExit("run apply_pece_pro.py first — the PRO READ column is missing")
    k15, k30 = max(1, round(15 / tf)), max(1, round(30 / tf))
    live = t[t["ts"].dt.time < time(15, 30)].reset_index(drop=True)
    live["fwd15"] = live["close"].shift(-k15) - live["close"]
    live["fwd30"] = live["close"].shift(-k30) - live["close"]
    live["day"] = d
    live["signal"] = [r[0] for r in live["read"]]
    live["sig_dir"] = [{"bull": 1, "bear": -1}.get(r[1], 0) for r in live["read"]]
    live["pro_tag"] = [p[0].split(" — ")[0] for p in live["pro"]]
    live["pro_dir"] = [{"bull": 1, "bear": -1}.get(p[1], 0) for p in live["pro"]]
    live["dots"] = [p[2] for p in live["pro"]]
    live["skip"] = [p[1] == "skip" for p in live["pro"]]
    return live


def summarise(df: pd.DataFrame, tag: str, dcol: str) -> pd.DataFrame:
    x = df[(df[dcol] != 0) & ~df["skip"]].dropna(subset=["fwd30"])
    rows = []
    for name, g in [("ALL directional", x)] + list(x.groupby(tag)):
        s15, s30 = g["fwd15"] * g[dcol], g["fwd30"] * g[dcol]
        rows.append(dict(tag=name, n=len(g), avg15=s15.mean(), hit15=100 * (s15 > 0).mean(),
                         avg30=s30.mean(), hit30=100 * (s30 > 0).mean()))
    return pd.DataFrame(rows).sort_values("n", ascending=False).round(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, default=3)
    ap.add_argument("--days", type=int, default=0, help="last N days only (0 = all)")
    a = ap.parse_args()
    days = sorted(ol._days_available())
    if a.days:
        days = days[-a.days:]
    parts = []
    for i, d in enumerate(days, 1):
        try:
            x = one_day(d, a.tf)
            if len(x):
                parts.append(x)
            print(f"  {i}/{len(days)} {d}  {len(x)} candles", end="\r")
        except SystemExit:
            raise
        except Exception as exc:                                # noqa: BLE001
            print(f"\n  {d}: skipped ({exc})")
    if not parts:
        print("no usable days"); return
    df = pd.concat(parts, ignore_index=True)
    out = Path(config.DATA_DIR) / "backtest_pece_pro.csv"
    df.drop(columns=["read", "pro"]).to_csv(out, index=False)
    print(f"\n{df['day'].nunique()} sessions · {len(df)} candles · {a.tf}m · saved {out}\n")
    pd.set_option("display.width", 160)
    print("── OLD Signal column ──────────────────────────────")
    print(summarise(df, "signal", "sig_dir").to_string(index=False))
    print("\n── ★ PRO READ tags ─────────────────────────────────")
    print(summarise(df, "pro_tag", "pro_dir").to_string(index=False))
    print("\n── ★ PRO READ by strength (directional tags) ───────")
    print(summarise(df, "dots", "pro_dir").to_string(index=False))
    sk = df["skip"].mean() * 100
    print(f"\n{sk:.0f}% of candles were flagged 'not a signal' (after close / expiry unwind).")
    print("Trust a tag only with n ≥ 30, hit% clearly > 50 and avg clearly above the ALL baseline — "
          "then re-check after 20 more sessions (out-of-sample).")


if __name__ == "__main__":
    main()
