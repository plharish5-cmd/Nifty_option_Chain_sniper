"""
Why did the app choose these levels?

    python why_levels.py

Prints the exact walk-back the level finder performs, bar by bar, so you can
see which session produced each level and — more usefully — which sessions
were considered and rejected.

WHAT THIS IS FOR

When a level disagrees with your chart, there are only three possible causes
and this separates them:

  1. THE ROW IS MISSING from your CSV. The walk-back cannot pick a session
     that is not there, so it silently skips to an older one and everything
     below shifts by one. This is the commonest cause and the hardest to spot
     from the output alone, because the result looks perfectly reasonable.

  2. THE PRICE DIFFERS on a row that IS present. Same date on both, different
     high or low. That is a data-source difference — NSE's published OHLC
     against whatever feed your chart uses.

  3. THE LOGIC IS WRONG. Only worth considering once 1 and 2 are ruled out.

The trace below shows every bar between the newest level and the oldest, with
the running threshold, so a gap in the dates is immediately visible.
"""
from __future__ import annotations

import sys

import pandas as pd

import config


def trace(df: pd.DataFrame, kind: str, n_levels: int = 4, show: int = 45) -> None:
    """Walk backwards exactly as build_master.dated_levels does, narrating."""
    i = len(df) - 1
    highs, lows = df["High"].values, df["Low"].values
    dates = df["Date"]
    col = highs if kind == "R" else lows
    better = (lambda a, b: a > b) if kind == "R" else (lambda a, b: a < b)
    word = "higher high" if kind == "R" else "lower low"

    print(f"\n{'=' * 72}")
    print(f"{'RESISTANCES' if kind == 'R' else 'SUPPORTS'} — walking back for "
          f"progressively {word}s")
    print("=" * 72)

    picked = [(dates.iloc[i], float(col[i]), i)]
    print(f"\n  {kind}1  {dates.iloc[i]:%d-%m-%Y}  {col[i]:>10,.2f}   "
          f"(the latest session)")

    threshold = float(col[i])
    considered = 0
    for j in range(i - 1, -1, -1):
        considered += 1
        val = float(col[j])
        if better(val, threshold):
            picked.append((dates.iloc[j], val, j))
            print(f"  {kind}{len(picked)}  {dates.iloc[j]:%d-%m-%Y}  {val:>10,.2f}"
                  f"   <-- new {word}, {considered} bar(s) back")
            threshold = val
            considered = 0
            if len(picked) == n_levels:
                break

    lo_i = picked[-1][2]
    print(f"\n  Every session from {dates.iloc[lo_i]:%d-%m-%Y} to "
          f"{dates.iloc[i]:%d-%m-%Y}:")
    print(f"  {'date':<14}{'high':>11}{'low':>11}   note")
    print("  " + "-" * 62)

    prev_date = None
    for k in range(lo_i, i + 1):
        d = dates.iloc[k]
        chosen = any(p[2] == k for p in picked)
        lbl = ""
        if chosen:
            which = next(f"{kind}{n+1}" for n, p in enumerate(picked) if p[2] == k)
            lbl = f"<-- {which}"
        gap = ""
        if prev_date is not None:
            days = (d - prev_date).days
            if days > 3:
                gap = f"  ** {days}-day gap before this row **"
        prev_date = d
        print(f"  {d:%d-%m-%Y}  {highs[k]:>11,.2f}{lows[k]:>11,.2f}   {lbl}{gap}")

    if len(picked) < n_levels:
        print(f"\n  Only found {len(picked)} of {n_levels} — history ran out.")


def main() -> int:
    try:
        import build_master as bm
        d = bm.load_all(config.OHLC_DIR)
    except Exception as exc:                                # noqa: BLE001
        print(f"Could not load OHLC: {exc}")
        return 1

    print("=" * 72)
    print("LEVEL SELECTION TRACE")
    print("=" * 72)
    print(f"\n  {len(d):,} sessions · {d.Date.min():%d-%b-%Y} to "
          f"{d.Date.max():%d-%b-%Y}")

    # missing weekdays are the usual culprit, so surface them first
    span = pd.bdate_range(d.Date.max() - pd.Timedelta(days=45), d.Date.max())
    have = set(d.Date.dt.date)
    missing = [x for x in span if x.date() not in have]
    if missing:
        print(f"\n  >>> {len(missing)} WEEKDAY(S) MISSING in the last 45 days:")
        for m in missing:
            print(f"        {m:%d-%b-%Y} ({m:%A})")
        print("      Some will be exchange holidays and are fine. Any that are")
        print("      NOT holidays are absent rows, and the walk-back cannot")
        print("      pick a session that is not in the file.")
    else:
        print("\n  No weekday gaps in the last 45 sessions.")

    trace(d, "R")
    trace(d, "S")

    print(f"\n{'=' * 72}")
    print("""IF A LEVEL STILL DISAGREES WITH YOUR CHART

  Find the date in the table above and compare its high and low against the
  same candle on TradingView.

  Date present, price differs  -> data-source difference. NSE's published
                                  OHLC and your chart's feed are not always
                                  identical to the paisa.
  Date absent entirely         -> a missing row. Re-download that period
                                  into data/ohlc/ and rebuild.
  Date present, price matches,
  level still wrong            -> then it is the logic, and I want to know.""")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
