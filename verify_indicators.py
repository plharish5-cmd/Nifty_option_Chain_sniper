"""
Indicator cross-check — compare the app against your TradingView chart.

    python verify_indicators.py

Prints every indicator the app computes from your daily OHLC, so you can put
this next to your chart ONCE and confirm they agree. After that you do not
need to send screenshots — the numbers come from the same data your chart
draws from.

WHAT SHOULD MATCH EXACTLY

  RSI (14)          Wilder smoothing, same as TradingView's RSI
  Stoch RSI         14, 14, 3, 3 — the settings on your chart
  Bollinger Bands   20, 2, population stdev — TradingView's default
  4 / 8 / 10 EMA    standard exponential

WHERE SMALL DIFFERENCES ARE NORMAL

  EMA seeding. TradingView seeds the first EMA value from an SMA; this seeds
  from the first close. After a few hundred bars the two converge to well
  under a point on NIFTY, but on a short history they can differ visibly.

  Data. Your chart draws from TradingView's NIFTY feed; this reads your
  data/ohlc CSVs from NSE. Daily OHLC should be identical, but a missing or
  stale session in your CSV will move every derived number. The row count and
  last date are printed first for that reason.

IF SOMETHING DISAGREES

  Check the last date and close first — nine times out of ten the CSV is
  simply missing the most recent session, which `python update_daily.py`
  fixes. Only then suspect the calculation.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config
import view_building as vb


def emas(close: pd.Series, spans=(4, 8, 10, 20)) -> dict:
    return {s: float(close.ewm(span=s, adjust=False).mean().iloc[-1]) for s in spans}


def main() -> int:
    try:
        import levels
        d = levels.load_ohlc()
    except Exception as exc:                                # noqa: BLE001
        print(f"Could not load OHLC: {exc}")
        print(f"Expected CSVs in {config.OHLC_DIR}")
        return 1

    d = d.sort_values("Date").reset_index(drop=True)
    last = d.iloc[-1]

    print("=" * 68)
    print("INDICATOR CROSS-CHECK")
    print("=" * 68)
    print(f"\nsessions      {len(d):,}")
    print(f"range         {d.Date.min():%d-%b-%Y} to {d.Date.max():%d-%b-%Y}")
    print(f"\nLAST SESSION  {last['Date']:%d-%b-%Y}")
    print(f"  O {last['Open']:>10,.2f}   H {last['High']:>10,.2f}")
    print(f"  L {last['Low']:>10,.2f}   C {last['Close']:>10,.2f}")
    print("\n  ^ check these four against your chart FIRST. If they differ,")
    print("    nothing below will match and the fix is `python update_daily.py`.")

    idx = d.set_index("Date")
    wk = idx.resample("W").agg({"Open": "first", "High": "max",
                                "Low": "min", "Close": "last"}).dropna()
    mo = idx.resample("ME").agg({"Open": "first", "High": "max",
                                 "Low": "min", "Close": "last"}).dropna()

    print("\n" + "=" * 68)
    print("RSI (14) — Wilder")
    print("=" * 68)
    for lbl, series in (("DAILY", idx["Close"]), ("WEEKLY", wk["Close"]),
                        ("MONTHLY", mo["Close"])):
        print(f"  {lbl:<9} {vb._rsi(series):>7.2f}")

    print("\n" + "=" * 68)
    print("STOCH RSI (14, 14, 3, 3) — %K and %D, the blue and orange lines")
    print("=" * 68)
    for lbl, series in (("DAILY", idx["Close"]), ("WEEKLY", wk["Close"]),
                        ("MONTHLY", mo["Close"])):
        k, dd = vb._stoch_rsi(series)
        print(f"  {lbl:<9} %K {k:>7.2f}   %D {dd:>7.2f}")

    print("\n" + "=" * 68)
    print("MOVING AVERAGES (daily)")
    print("=" * 68)
    e = emas(idx["Close"])
    for span, val in e.items():
        gap = float(last["Close"]) - val
        print(f"  {span:>2} EMA    {val:>10,.2f}   close is {gap:+.0f} pts "
              f"{'above' if gap > 0 else 'below'}")

    print("\n" + "=" * 68)
    print("BOLLINGER BANDS (20, 2) — daily")
    print("=" * 68)
    bb = vb.band_behaviour(d)
    if bb:
        print(f"  Upper      {bb['ubb']:>10,.2f}   direction {bb['ubb_dir']}")
        print(f"  Lower      {bb['lbb']:>10,.2f}   direction {bb['lbb_dir']}")
        print(f"  Width      {bb['width']:>10,.2f}   "
              f"{bb['width_change_pct']:+.1f}% over 3 sessions")
        print(f"  Shape      {bb['shape']}")
        print(f"\n  Read: {bb['read']}")
    else:
        print("  not enough history for a 20-period band")

    print("\n" + "=" * 68)
    print("WHAT THE SHEET WILL SAY")
    print("=" * 68)
    p6 = vb.p6_daily_trend(d)
    p7 = vb.p7_rsi(d)
    for p in (p6, p7):
        print(f"\n  #{p.no} score {p.score:+d}  [{p.verdict_word}]")
        print(f"       {p.observation}")
        if p.note:
            print(f"       {p.note}")

    print("\n" + "=" * 68)
    print("Put this next to your TradingView chart once. If the last-session")
    print("OHLC matches and the indicators do not, tell me which one and by")
    print("how much — that is a calculation bug worth fixing.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
