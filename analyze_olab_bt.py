"""
analyze_olab_bt.py — is the "fade the writers" pattern in backtest_olab_pro real, or luck?
Reads data/backtest_olab_pro.csv (run backtest_olab_pro.py first). No new data needed.

Checks, using CE rows only (CE and PE grades are mirror images — counting both doubles the same signal):
  1. first half of your sessions vs second half      → a real pattern shows up in BOTH halves
  2. expiry Tuesdays vs other days                    → expiry mechanics can fake a pattern
  3. morning vs afternoon
  4. day-level: one vote per day (fraction of checkpoints that went the writers' way) → removes overlap
  .venv/bin/python analyze_olab_bt.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

import config

f = Path(config.DATA_DIR) / "backtest_olab_pro.csv"
df = pd.read_csv(f, parse_dates=["ts"])
df["day"] = pd.to_datetime(df["day"])
x = df[(df["side"] == "CE") & df["regime30"].isin(["BULL", "BEAR"])].copy()
x["writers_right"] = np.where(x["regime30"] == "BULL", x["move30"] > 0, x["move30"] < 0)
x["pts_writers_way"] = np.where(x["regime30"] == "BULL", x["move30"], -x["move30"])
days = sorted(x["day"].unique())
half = days[len(days) // 2]


def show(name, g):
    if len(g) == 0:
        print(f"  {name:<28} n=0"); return
    se = 50 / np.sqrt(len(g))
    print(f"  {name:<28} n={len(g):>4} · writers right {100 * g['writers_right'].mean():5.1f}% (±{se:.0f}) · "
          f"avg {g['pts_writers_way'].mean():+6.1f} pts in writers' direction")


print(f"{len(days)} sessions · {len(x)} checkpoints where writers had a clear side (BULL/BEAR)\n")
print("Following the writers' side for 30 min:")
show("ALL", x)
show(f"first half (< {pd.Timestamp(half):%d %b})", x[x["day"] < half])
show(f"second half (≥ {pd.Timestamp(half):%d %b})", x[x["day"] >= half])
show("expiry Tuesdays", x[x["day"].dt.weekday == 1])
show("other days", x[x["day"].dt.weekday != 1])
show("before 12:00", x[x["ts"].dt.hour < 12])
show("12:00 and later", x[x["ts"].dt.hour >= 12])
d = x.groupby("day")["writers_right"].mean()
print(f"\nOne vote per day: writers right on more than half the checkpoints on {int((d > 0.5).sum())} of {len(d)} days "
      f"({100 * (d > 0.5).mean():.0f}%).")
print("\nHow to read: '±' is the luck band. A real 'fade the writers' pattern = writers right clearly BELOW 50% in BOTH "
      "halves and on non-expiry days, and on fewer than ~35% of days. Anything else = no reliable pattern either way.")
