# The loader that compare_random.py imports but the Tradehull bundle shipped without.
# It returns the hourly candle frame for one symbol, oldest-first, filtered to the real
# 60-minute session slots and tagged with a `day` column -- exactly the shape
# backtest_hourly.py / kronos_service.py build and that compare_random.py indexes by `day`.
#
# Two sources are supported:
#   1) A plain CSV of hourly candles: columns date,open,high,low,close,volume  (date = IST start).
#      This is what fetch_hourly.py writes to data_hourly/<SYM>_1h.csv.
#   2) For NIFTY on the live desk, point HOURLY_DIR at the desk's own hourly export, or pass a
#      direct path. The desk builds NIFTY hourly from the basis-adjusted FUTURE (market/futures.py
#      mapped()/combined_1m()) because the index itself has no volume -- so the CSV you feed here
#      for NIFTY must already be that future-volume-attached hourly series, not raw index candles.
#
# Alert-only / research-only. No Dhan calls happen here; this reads candles already on disk.
import os
import pandas as pd

HOURLY_DIR = os.environ.get("KRONOS_HOURLY_DIR", "data_hourly")

# Six true-60-minute NIFTY slots. Dhan's intraday feed drops the 15:15-15:30 stub (since 3 Aug
# 2026), so 15:15 is intentionally excluded -- every retained candle is a full hour.
SLOTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]


def _candidate_paths(sym):
    s = sym.replace(" ", "_")
    for name in (f"{s}_1h.csv", f"{s}.csv", f"{s}_1h.CSV"):
        yield os.path.join(HOURLY_DIR, name)


def load_hourly(sym, path=None, slots=SLOTS):
    """Return a DataFrame with columns date, open, high, low, close, volume, day -- oldest first,
    one row per retained hourly slot. `day` is the YYYY-MM-DD the candle belongs to, which is the
    key compare_random.py groups on."""
    if path is None:
        for p in _candidate_paths(sym):
            if os.path.exists(p):
                path = p
                break
    if path is None or not os.path.exists(path):
        raise FileNotFoundError(
            f"no hourly CSV for {sym} (looked in {HOURLY_DIR}). "
            f"For NIFTY, export the desk's future-volume-attached hourly series to "
            f"{os.path.join(HOURLY_DIR, sym.replace(' ', '_') + '_1h.csv')} first.")

    df = pd.read_csv(path)
    # tolerate a 'timestamp'/'datetime' column name instead of 'date'
    if "date" not in df.columns:
        for alt in ("timestamp", "datetime", "time"):
            if alt in df.columns:
                df = df.rename(columns={alt: "date"})
                break
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    need = ["date", "open", "high", "low", "close"]
    df = df.dropna(subset=need)
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if slots is not None:
        df = df[df["date"].dt.strftime("%H:%M").isin(slots)]
    df = df.sort_values("date").reset_index(drop=True)
    df["day"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


if __name__ == "__main__":
    import sys
    d = load_hourly(sys.argv[1] if len(sys.argv) > 1 else "NIFTY")
    print(f"{len(d)} candles, {d.day.nunique()} days, {d.date.iloc[0]} .. {d.date.iloc[-1]}")
    print(d.head())
