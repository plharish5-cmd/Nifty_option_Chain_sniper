"""
check_candles.py — print Dhan's 15-minute candles so you can eyeball them
against TradingView.

    python check_candles.py

Open your 15-minute NIFTY chart, hover each candle at the time shown, and
compare the four numbers. They should match to the paisa.

Skips the final 15:15 candle, which contains the closing auction and prints
oddly — comparing that one tells you nothing.
"""
import config
import dhan_client as dc
import pandas as pd
from datetime import time


def main() -> int:
    print("\nDhan 15-minute candles — compare these against TradingView\n")
    try:
        d = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                        days_back=1, interval="15")
    except Exception as exc:
        print(f"  fetch failed: {exc}")
        return 1
    if d is None or d.empty:
        print("  no candles returned")
        return 1

    ts = pd.to_datetime(d["timestamp"])
    keep = (ts.dt.time >= config.MARKET_OPEN) & (ts.dt.time <= time(15, 0))
    d = d[keep].copy()
    d["time"] = pd.to_datetime(d["timestamp"]).dt.strftime("%d-%b %H:%M")

    last = d.tail(6)[["time", "open", "high", "low", "close"]]
    print(last.to_string(index=False,
                         float_format=lambda x: f"{x:,.2f}"))
    print("\n  On TradingView: NIFTY 50, 15 minute, hover each candle above.")
    print("  The 15:15 candle is excluded — it carries the closing auction")
    print("  and will not match anything.")
    print("\n  If they match  -> the Dhan feed is good, nothing more to do.")
    print("  If they differ -> paste both here and we will work out which")
    print("                    is wrong before you trade off either.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
