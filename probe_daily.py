"""Run each daily-data route on its own and show exactly what comes back."""
import config, pandas as pd
pd.set_option("display.width", 120)

print(f"CANDLE_SOURCE = {config.CANDLE_SOURCE}\n")

print("── A. Dhan intraday, 60-min, last 10 days ──────────────────────")
try:
    import dhan_client as dc
    raw = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                      days_back=10, interval="60")
    print(f"  {len(raw)} bars, {raw.timestamp.min()} to {raw.timestamp.max()}")
    ts = pd.to_datetime(raw["timestamp"])
    print(f"  distinct dates: {sorted(set(ts.dt.date))}")
    outside = raw[(ts.dt.time < config.MARKET_OPEN) | (ts.dt.time > config.MARKET_CLOSE)]
    print(f"  bars outside 09:15-15:30: {len(outside)}")
    if len(outside):
        print(outside.head(3).to_string(index=False))
except Exception as exc:
    print(f"  FAILED — {exc}")

print("\n── B. daily_from_intraday ──────────────────────────────────────")
try:
    d = dc.daily_from_intraday(10)
    print(f"  {len(d)} sessions")
    print(d.to_string(index=False))
except Exception as exc:
    print(f"  FAILED — {exc}")

print("\n── C. merge into OHLC.csv (dry run, nothing written) ───────────")
try:
    import build_master as bm
    have = bm.load_all(config.OHLC_DIR)
    known = set(pd.to_datetime(have["Date"]).dt.date)
    new = [x for x in pd.to_datetime(d["Date"]).dt.date if x not in known]
    print(f"  history newest: {max(known)}")
    print(f"  would add: {new}")
except Exception as exc:
    print(f"  FAILED — {exc}")
