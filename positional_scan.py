"""
positional_scan.py — the "positional trading screener" idea on the NIFTY 50 stocks (daily candles).

The video's setup, written as explicit rules (positional_rules.json):
  TREND     price above the 20 and 50 EMA, 50 EMA rising over 20 days, higher swing high AND higher swing low (pivots of 5 days)
  BOX       the last 30 sessions' high/low form a sideways box no wider than 12% of price
  SETUP     (the video's candidate filter) +3.5..7% in the last 5 days, RSI(14) 40..60, price in the top quarter of the box
  BREAKOUT  a close above the box high, with volume >= 1.2 x its 20-day average
  TRADE     buy the next open · stop = box low · target 2R · exit after 60 sessions if neither is hit · 0.2% round-trip cost

  .venv/bin/python positional_scan.py --fetch          # download ~10 years of daily candles for the 50 stocks from Dhan (needs .env token)
  .venv/bin/python positional_scan.py                  # today's list: BREAKOUT / SETUP candidates with level, stop, targets
  .venv/bin/python positional_scan.py --all            # every stock with its status
Whether any of this works is decided by backtest_positional.py (Evidence Board). Until it passes the list is research only.
Information only — not financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data" / "daily_stocks"
BT = ROOT / "data" / "backtests"


def rules() -> dict:
    return json.loads((ROOT / "positional_rules.json").read_text())


# ═══════════════════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════════════════
def fetch(years: int) -> int:
    """Daily candles for the 50 constituents → data/daily_stocks/<SYMBOL>.csv (Date,Open,High,Low,Close,Volume)."""
    import dhan_client as dc
    import orderflow_recorder as orf
    orr = orf.rules()
    syms = orf.constituents(orr)
    ids = orf.security_ids(syms, orr)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    end = datetime.now().date()
    saved = 0
    for sym in syms:
        sid = ids.get(sym)
        if not sid:
            continue
        frames, cur = [], end - timedelta(days=365 * years)
        while cur < end:
            stop = min(cur + timedelta(days=365 * 2), end)
            body = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False,
                    "fromDate": cur.strftime("%Y-%m-%d"), "toDate": stop.strftime("%Y-%m-%d")}
            try:
                df = dc._to_frame(dc._post("/charts/historical", body))
                frames.append(df)
            except Exception as e:                                        # noqa: BLE001
                print(f"  ! {sym} {cur}→{stop}: {str(e)[:100]}")
            cur = stop + timedelta(days=1)
        if not frames:
            continue
        d = pd.concat(frames)
        d["Date"] = pd.to_datetime(d["timestamp"]).dt.normalize()
        d = (d.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
             [["Date", "Open", "High", "Low", "Close", "Volume"]].drop_duplicates("Date").sort_values("Date"))
        d.to_csv(DATA_DIR / f"{sym}.csv", index=False)
        saved += 1
        print(f"  {sym:<12}{len(d):>6} sessions {d['Date'].iloc[0]:%Y-%m-%d} → {d['Date'].iloc[-1]:%Y-%m-%d}")
    return saved


def load_all(data_dir: Optional[Path] = None) -> dict:
    out = {}
    for f in sorted((data_dir or DATA_DIR).glob("*.csv")):
        try:
            d = pd.read_csv(f, parse_dates=["Date"])
            if len(d) > 200:
                out[f.stem] = d.sort_values("Date").reset_index(drop=True)
        except Exception:
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Indicators + flags (all causal: a flag on row t uses data up to t only)
# ═══════════════════════════════════════════════════════════════════════════
def _structure(h: np.ndarray, l: np.ndarray, n: int):
    """True where the last two CONFIRMED swing highs rose and the last two swing lows rose. A pivot is known n days later."""
    N = len(h)
    ph, pl = np.full(N, np.nan), np.full(N, np.nan)
    for p in range(n, N - n):
        if h[p] > h[p - n:p].max() and h[p] >= h[p + 1:p + n + 1].max():
            ph[p + n] = h[p]
        if l[p] < l[p - n:p].min() and l[p] <= l[p + 1:p + n + 1].min():
            pl[p + n] = l[p]

    def rising(a):
        s = pd.Series(a)
        prev = s.dropna().shift(1).reindex(s.index).ffill()
        return (s.ffill() > prev).to_numpy()
    return rising(ph), rising(pl)


def prep(df: pd.DataFrame, R: dict) -> pd.DataFrame:
    d = df.sort_values("Date").reset_index(drop=True).copy()
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    d["ema_f"] = c.ewm(span=int(R["ema_fast"]), adjust=False).mean()
    d["ema_s"] = c.ewm(span=int(R["ema_slow"]), adjust=False).mean()
    d["slope_up"] = d["ema_s"] > d["ema_s"].shift(int(R["ema_slope_days"]))
    delta = c.diff()
    n = int(R["rsi_n"])
    au = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    ad = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    d["vol_avg"] = v.rolling(int(R["vol_avg_n"])).mean().shift(1)
    L = int(R["box_days"])
    d["box_hi"] = h.rolling(L).max().shift(1)
    d["box_lo"] = l.rolling(L).min().shift(1)
    d["width"] = (d["box_hi"] - d["box_lo"]) / c.shift(1) * 100
    d["ret5"] = (c / c.shift(5) - 1) * 100
    hh, hl = _structure(h.to_numpy(float), l.to_numpy(float), int(R["pivot_n"]))
    d["trend"] = (c > d["ema_f"]) & (c > d["ema_s"]) & d["slope_up"] & hh & hl
    return d


def setup_flag(d: pd.DataFrame, R: dict) -> pd.Series:
    S = R["setup"]
    pos = (d["Close"] - d["box_lo"]) / (d["box_hi"] - d["box_lo"]).replace(0, np.nan)
    return (d["trend"] & (d["width"] <= float(R["box_max_width_pct"])) & (d["ret5"] >= S["ret5_min"]) & (d["ret5"] <= S["ret5_max"])
            & (d["rsi"] >= S["rsi_min"]) & (d["rsi"] <= S["rsi_max"]) & (pos >= 1 - S["near_top_frac"]) & (d["Close"] <= d["box_hi"]))


def breakout_flag(d: pd.DataFrame, R: dict, need_volume: bool = True) -> pd.Series:
    """Close above the 30-day box, tight box, trend intact the day BEFORE, volume surge."""
    f = (d["Close"] > d["box_hi"]) & (d["width"] <= float(R["box_max_width_pct"])) & d["trend"].shift(1).fillna(False).astype(bool)
    if need_volume:
        f &= (d["Volume"] >= float(R["vol_mult"]) * d["vol_avg"])
    return f


# ═══════════════════════════════════════════════════════════════════════════
# Today's list
# ═══════════════════════════════════════════════════════════════════════════
def scan(R: Optional[dict] = None, show_all: bool = False) -> pd.DataFrame:
    R = R or rules()
    rows = []
    for sym, df in load_all().items():
        d = prep(df, R)
        t = len(d) - 1
        if t < int(R["warmup_days"]):
            continue
        r = d.iloc[t]
        br, su = bool(breakout_flag(d, R).iloc[t]), bool(setup_flag(d, R).iloc[t])
        status = "BREAKOUT" if br else ("SETUP" if su else ("trend + box" if bool(r["trend"]) and r["width"] <= R["box_max_width_pct"] else "—"))
        risk = (r["Close"] - r["box_lo"]) / r["Close"] * 100 if status == "BREAKOUT" else np.nan
        rows.append(dict(symbol=sym, date=r["Date"].date(), close=round(float(r["Close"]), 2), status=status,
                         box=f"{r['box_lo']:,.1f}–{r['box_hi']:,.1f}", width_pct=round(float(r["width"]), 1),
                         rsi=round(float(r["rsi"]), 0), ret5=round(float(r["ret5"]), 1),
                         break_above=round(float(r["box_hi"]), 2), stop=round(float(r["box_lo"]), 2),
                         tgt_2r=round(float(r["Close"] + 2 * (r["Close"] - r["box_lo"])), 2) if status == "BREAKOUT" else np.nan,
                         risk_pct=round(float(risk), 1) if status == "BREAKOUT" else np.nan))
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    order = {"BREAKOUT": 0, "SETUP": 1, "trend + box": 2, "—": 3}
    out = out.sort_values(by="status", key=lambda s: s.map(order))
    return out if show_all else out[out["status"].isin(["BREAKOUT", "SETUP"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--years", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    R = rules()
    if a.fetch:
        print(f"saved {fetch(a.years or int(R['history_years']))} stock file(s) to {DATA_DIR}")
        return
    out = scan(R, a.all)
    if out.empty:
        print("No candidates today (or no daily data yet — run with --fetch)." if not load_all() else "No BREAKOUT or SETUP candidates today.")
    else:
        print(out.to_string(index=False))
    v = None
    try:
        v = json.loads((BT / "positional.json").read_text()).get("headline", {})
    except Exception:
        pass
    status = ("backtest PASSED" if v and v.get("passed") else ("backtest: no edge" if v else "not backtested yet — run backtest_positional.py"))
    print(f"\nPositional list is RESEARCH ONLY ({status}). Stop = box low, target 2R, review after 60 sessions. "
          f"Information only — not financial advice.")


if __name__ == "__main__":
    main()
