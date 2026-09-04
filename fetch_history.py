"""
Bulk historical intraday downloader.

    python fetch_history.py            # default: 2 years of 5-min NIFTY
    python fetch_history.py 5 1        # 5 years of 1-min

Downloads NIFTY 50 intraday candles from Dhan in 90-day chunks (their per-
request cap) and writes one CSV per chunk into data/history/.

THE POINT OF THIS SCRIPT — AND WHY IT IS WORTH RS499 ONCE

Dhan's data API costs Rs499+tax a month. Paying that EVERY month to feed a
live dashboard is poor value while the tool has no validated track record.
Paying it ONCE to pull five years of history you keep forever is a different
proposition entirely — and it is the only way to answer questions like "does
the previous day's last 15-min candle really break on the third touch", which
cannot be answered from a 60-day free window.

So: subscribe, run this, verify the files, cancel. The data is yours.

NSE's own free feed serves the CURRENT SESSION ONLY, so it cannot help here
no matter how it is queried.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config

CHUNK_DAYS = 88          # Dhan caps a single poll at 90 days; leave margin


def main() -> int:
    years = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    interval = sys.argv[2] if len(sys.argv) > 2 else "5"

    if interval not in ("1", "5", "15", "25", "60"):
        print(f"interval must be one of 1, 5, 15, 25, 60 — got {interval!r}")
        return 1

    try:
        import dhan_client as dc
        dc.token()
    except Exception as exc:                                # noqa: BLE001
        print("Dhan token not usable:", exc)
        print("\nThis needs the PAID Dhan Data API subscription (Rs499/month).")
        print("Subscribe, run this once, verify the files, then cancel —")
        print("the downloaded history is yours to keep.")
        return 1

    out_dir = config.HISTORY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    end = datetime.now()
    start = end - timedelta(days=int(years * 365))
    print("=" * 68)
    print(f"DOWNLOADING {interval}-MINUTE NIFTY 50 HISTORY")
    print("=" * 68)
    print(f"range     {start:%d-%b-%Y} to {end:%d-%b-%Y}  ({years} years)")
    print(f"chunks    {CHUNK_DAYS} days each")
    print(f"output    {out_dir}\n")

    cursor = start
    total_rows = 0
    chunk_no = 0
    failures = 0

    while cursor < end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        chunk_no += 1
        tag = f"{cursor:%Y%m%d}_{chunk_end:%Y%m%d}"
        path = out_dir / f"nifty_{interval}m_{tag}.csv"

        if path.exists():
            print(f"  [{chunk_no:>3}] {tag}  already downloaded, skipping")
            cursor = chunk_end
            continue

        try:
            payload = dc._post("/charts/intraday", {
                "securityId": config.DHAN_INDEX_SECURITY_ID,
                "exchangeSegment": "IDX_I",
                "instrument": "INDEX",
                "interval": interval,
                "oi": False,
                "fromDate": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            })
            df = dc._to_frame(payload)
            if df.empty:
                print(f"  [{chunk_no:>3}] {tag}  no data (holidays only?)")
            else:
                out = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
                out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
                out.to_csv(path, index=False)
                total_rows += len(out)
                print(f"  [{chunk_no:>3}] {tag}  {len(out):>7,} bars  -> {path.name}")
        except Exception as exc:                            # noqa: BLE001
            failures += 1
            print(f"  [{chunk_no:>3}] {tag}  FAILED: {exc}")
            if failures >= 3:
                print("\nThree failures — stopping. Check the token and the "
                      "Data API subscription.")
                return 1

        cursor = chunk_end
        time.sleep(0.4)          # polite, though minute timeframes are unlimited

    print(f"\n{'=' * 68}")
    print(f"done — {total_rows:,} bars across {chunk_no} chunks")
    print(f"files in {out_dir}")
    print("\nNext:  python backtest_levels.py")
    print("\nYou can cancel the Dhan Data API subscription now. The CSVs are")
    print("yours permanently, and the backtester never needs the network.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
