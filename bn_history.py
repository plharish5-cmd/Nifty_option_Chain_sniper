"""
bn_history.py — Bank Nifty 1-minute history for the SMT (smart-money divergence) tests.

  .venv/bin/python bn_history.py --fetch 730      # downloads ~2 years from Dhan into data/candles_hist_bn/
  (or: .venv/bin/python backtest_ict.py --fetch-bn 730)

load() returns the same 1-minute frame shape as backtest_five_rule.load_1m(), for BANKNIFTY (Dhan id 25).
Information only — not financial advice.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
HIST_DIR = ROOT / "data" / "candles_hist_bn"


def fetch_history(days_back: int) -> int:
    """Download 1m BANKNIFTY index candles from Dhan in 85-day chunks → data/candles_hist_bn/YYYY-MM-DD.csv."""
    import config
    import dhan_client as dc
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    sec = str(getattr(config, "DHAN_BANKNIFTY_SECURITY_ID", "25"))
    end = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    cur, saved = end - timedelta(days=days_back), 0
    while cur < end:
        stop = min(cur + timedelta(days=85), end)
        try:
            payload = dc._post("/charts/intraday", {
                "securityId": sec, "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "1", "oi": False,
                "fromDate": cur.strftime("%Y-%m-%d 09:15:00"), "toDate": stop.strftime("%Y-%m-%d 15:30:00")})
            df = dc._to_frame(payload)
        except Exception as e:                                           # noqa: BLE001
            print(f"  ! Bank Nifty fetch {cur:%Y-%m-%d}→{stop:%Y-%m-%d} failed: {e}")
            df = pd.DataFrame()
        if len(df):
            df["day"] = df["timestamp"].dt.date
            for day, g in df.groupby("day"):
                p = HIST_DIR / f"{day:%Y-%m-%d}.csv"
                g.drop(columns="day").assign(timestamp=g["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")).to_csv(p, index=False)
                saved += 1
            print(f"  fetched {cur:%Y-%m-%d} → {stop:%Y-%m-%d}: {df['day'].nunique()} sessions")
        cur = stop + timedelta(days=1)
    return saved


def load() -> pd.DataFrame:
    """All stored Bank Nifty sessions (history + live collector folder), oldest first. Empty frame if none."""
    import backtest_five_rule as fr
    dirs = [HIST_DIR]
    try:
        import config
        dirs.append(Path(config.CANDLE_BANKNIFTY_DIR))
    except Exception:
        dirs.append(ROOT / "data" / "candles_banknifty")
    days = {}
    for folder in dirs:
        if not folder.exists():
            continue
        for f in sorted(folder.glob("20*.csv")):
            d = fr._read_day(f)
            if d is not None:
                days[d["timestamp"].iloc[0].date()] = d
    if not days:
        return pd.DataFrame()
    return pd.concat([days[k] for k in sorted(days)], ignore_index=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", type=int, default=0)
    a = ap.parse_args()
    if a.fetch:
        print(f"saved {fetch_history(a.fetch)} session file(s)")
    d = load()
    print(f"{d['timestamp'].dt.date.nunique() if len(d) else 0} Bank Nifty sessions stored")
