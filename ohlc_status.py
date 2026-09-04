"""
What is actually in the OHLC file — run this when you doubt the levels.

    python ohlc_status.py

Shows the newest rows, how many sessions behind today it is, and whether any
dates failed to parse. The levels are only ever as current as this file: the
dashboard's Refresh button re-reads it, it does not fetch a new bar.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

import pandas as pd

import config


def main() -> int:
    try:
        import build_master as bm
        d = bm.load_all(config.OHLC_DIR)
    except Exception as exc:                                # noqa: BLE001
        print(f"Could not load: {exc}")
        return 1

    today = pd.Timestamp(date.today())
    last = d["Date"].max()
    age = (today - last).days

    print("=" * 60)
    print("OHLC FILE STATUS")
    print("=" * 60)
    print(f"  rows            {len(d):,}")
    print(f"  range           {d.Date.min():%d-%b-%Y} to {last:%d-%b-%Y}")
    print(f"  today           {today:%d-%b-%Y}")
    print(f"  newest bar is   {age} day(s) old")

    if age >= 1:
        print("\n  >>> The levels shown in the app come from this newest bar.")
        print("      They are NOT wrong — they are simply the levels as of")
        print(f"      {last:%d-%b-%Y}. To bring them up to date, after 15:30 run:")
        print("        python update_daily.py")
        print("      or press 'Pull latest bar now' on the Chart levels tab.")
    else:
        print("\n  Up to date.")

    print("\n  last 5 sessions")
    print("  " + "-" * 56)
    for _, r in d.tail(5).iterrows():
        print(f"  {r['Date']:%d-%b-%Y}   O {r['Open']:>9,.2f}  H {r['High']:>9,.2f}"
              f"  L {r['Low']:>9,.2f}  C {r['Close']:>9,.2f}")

    print("\n  rows per year")
    print("  " + "-" * 56)
    for yr, n in d["Date"].dt.year.value_counts().sort_index().items():
        flag = "" if n > 200 or yr == date.today().year else "   <-- low, check for dropped rows"
        print(f"  {yr}   {n:>4}{flag}")

    gaps = d["Date"].diff().dt.days
    big = d[gaps > 7]
    if len(big):
        print(f"\n  {len(big)} gap(s) longer than a week inside the series:")
        for _, r in big.head(5).iterrows():
            print(f"    before {r['Date']:%d-%b-%Y}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
