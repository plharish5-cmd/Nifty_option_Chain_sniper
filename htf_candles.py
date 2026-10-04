"""
htf_candles.py — 30-minute, 1-hour and 4-hour NIFTY candles (built from stored 1m, anchored to the 09:15 open, one
session at a time) plus the zones the AirForexOne guide trades: order-block "traps" and "break blocks".

NSE hours give 1h bars 09:15…14:15 (the 15-minute stub at 15:15 is dropped), 30m bars 09:15…14:45 (15:15 stub dropped),
and two 4h bars (09:15–13:15 and 13:15–15:30). A bar is used only once complete; its `ct` (close time) is when it becomes known.

Zones (long space; shorts are the same code on negated prices):
  impulse      a complete HTF bar with body >= impulse_body_atr x ATR(atr_n, bars before it) that closes above the highest
               high of the previous bos_lookback bars (a break of structure)
  order block  the last down-candle among the ob_lookback bars before the impulse → zone = its high/low
  TRAP         bullish impulse with a fresh demand order block                       → entry 1 (bear trap → buy)  /  mirror entry 2
  BREAK BLOCK  the impulse also closes above a still-unbroken SUPPLY block formed in the last flip_lookback bars: that
               supply zone flips to support                                          → entry 3 (buy)  /  mirror entry 4

  .venv/bin/python htf_candles.py --day 2026-10-01      # print the 30m/1h/4h bars and zones for one session
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
SESSION_END = pd.Timedelta(hours=15, minutes=30)
SESSION_START = pd.Timedelta(hours=9, minutes=15)


def rules() -> dict:
    return json.loads((ROOT / "htf_rules.json").read_text())


def htf(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Complete HTF bars only. Columns: timestamp (open), ct (close time), open, high, low, close."""
    out = []
    for day, g in m1.groupby(m1["timestamp"].dt.date):
        d0 = pd.Timestamp(day)
        r = (g.set_index("timestamp").resample(f"{minutes}min", origin=d0 + SESSION_START, label="left")
             .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                  n=("close", "count")).dropna(subset=["close"]).reset_index())
        for row in r.itertuples():
            start = row.timestamp
            expected = min(minutes, int((d0 + SESSION_END - start) / pd.Timedelta(minutes=1)))
            if expected <= 0.5 * minutes or row.n < 0.95 * expected:
                continue                                                        # stub or data hole
            out.append(dict(timestamp=start, ct=start + pd.Timedelta(minutes=expected), open=row.open, high=row.high,
                            low=row.low, close=row.close))
    return pd.DataFrame(out)


def _atr(h, l, c, n):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def _impulses(o, h, l, c, P):
    """Bullish impulses in the given price space → list of dict(k, lo, hi) (zone = the order block before it)."""
    atr = _atr(h, l, c, int(P["atr_n"]))
    out = []
    L, obl = int(P["bos_lookback"]), int(P["ob_lookback"])
    for k in range(max(L, int(P["atr_n"])) + 1, len(c)):
        a = atr[k - 1]
        if np.isnan(a) or (c[k] - o[k]) < float(P["impulse_body_atr"]) * a:
            continue
        if c[k] <= h[k - L:k].max():
            continue
        ob = next((j for j in range(k - 1, max(k - obl - 1, -1), -1) if c[j] < o[j]), None)
        if ob is None:
            continue
        out.append(dict(k=k, lo=float(l[ob]), hi=float(h[ob])))
    return out


def zones(bars: pd.DataFrame, P: dict | None = None) -> list:
    """Zones known at each bar's close. Each: side, kind ('trap'|'break_block'), lo, hi, known_t, k, day."""
    P = P or rules()
    if len(bars) < 30:
        return []
    o, h, l, c = (bars[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    up = _impulses(o, h, l, c, P)                                   # bullish impulses, demand blocks
    dn_neg = _impulses(-o, -l, -h, -c, P)                           # bearish impulses in negated space
    dn = [dict(k=z["k"], lo=-z["hi"], hi=-z["lo"]) for z in dn_neg]  # back to real prices: supply blocks
    fl = int(P["flip_lookback"])
    ct = bars["ct"].to_numpy()
    out = []
    for u in up:
        k, kind, lo, hi = u["k"], "trap", u["lo"], u["hi"]
        for d in reversed(dn):
            if d["k"] >= k or d["k"] < k - fl:
                continue
            if c[d["k"] + 1:k].size and c[d["k"] + 1:k].max() > d["hi"]:
                continue                                              # supply already broken before this impulse
            if c[k] > d["hi"]:
                kind, lo, hi = "break_block", d["lo"], d["hi"]
                break
        out.append(dict(side="BULLISH", kind=kind, lo=lo, hi=hi, known_t=pd.Timestamp(ct[k]), k=k,
                        day=str(pd.Timestamp(ct[k]).date())))
    for d in dn:
        k, kind, lo, hi = d["k"], "trap", d["lo"], d["hi"]
        for u in reversed(up):
            if u["k"] >= k or u["k"] < k - fl:
                continue
            if c[u["k"] + 1:k].size and c[u["k"] + 1:k].min() < u["lo"]:
                continue
            if c[k] < u["lo"]:
                kind, lo, hi = "break_block", u["lo"], u["hi"]
                break
        out.append(dict(side="BEARISH", kind=kind, lo=lo, hi=hi, known_t=pd.Timestamp(ct[k]), k=k,
                        day=str(pd.Timestamp(ct[k]).date())))
    return sorted(out, key=lambda z: z["known_t"])


def main():
    import backtest_five_rule as fr
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    a = ap.parse_args()
    m1 = fr.load_1m()
    P = rules()
    for tf in P["timeframes"]:
        b = htf(m1, int(tf))
        print(f"\n{tf}-minute bars on {a.day}")
        print(b[b["timestamp"].dt.strftime("%Y-%m-%d") == a.day].to_string(index=False))
        for z in zones(b, P):
            if z["day"] == a.day:
                print(f"  zone known {z['known_t']:%H:%M}  {z['side']:<8} {z['kind']:<11} {z['lo']:,.1f} – {z['hi']:,.1f}")


if __name__ == "__main__":
    main()
