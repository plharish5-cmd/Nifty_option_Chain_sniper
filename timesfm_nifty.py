"""
TimesFM 3.0 / 2.5 NIFTY 50 Universal Forecaster (5m, 15m, Daily)

Runs on your local Dhan intraday store (data/candles/) and daily master (data/ohlc/OHLC.csv).
Outputs:
1. Forecast chart with q10-q90 uncertainty cone.
2. Formatted TradingView Pine Script v6 arrays for direct copy-paste.

Usage:
    python timesfm_nifty.py 5m       # Forecast next 25 5-minute candles (~2 hours)
    python timesfm_nifty.py 15m      # Forecast next 25 15-minute candles (full trading day)
    python timesfm_nifty.py 1d       # Forecast next 30 daily trading sessions
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
OHLC_FILE = DATA_DIR / "ohlc" / "OHLC.csv"
CANDLE_DIR = DATA_DIR / "candles"


def generate_future_nse_intraday_timestamps(
    last_ts: pd.Timestamp, 
    interval_mins: int, 
    count: int
) -> list[pd.Timestamp]:
    """
    Generate future NSE trading timestamps, strictly skipping overnight hours
    (after 15:30 IST) and weekends (Saturday/Sunday).
    """
    future = []
    curr = last_ts
    cutoff_min = 30 - interval_mins
    cutoff_time = datetime.time(15, cutoff_min)

    while len(future) < count:
        curr += pd.Timedelta(minutes=interval_mins)
        # Check if past session close (15:30) or before open (09:15)
        if curr.time() > cutoff_time or curr.time() < datetime.time(9, 15):
            # Advance to next morning 09:15
            curr = curr.replace(hour=9, minute=15, second=0, microsecond=0) + pd.Timedelta(days=1)
            # Skip Saturday (5) and Sunday (6)
            while curr.weekday() >= 5:
                curr += pd.Timedelta(days=1)
        future.append(curr)
    return future


def generate_future_nse_daily_timestamps(
    last_date: pd.Timestamp, 
    count: int
) -> list[pd.Timestamp]:
    """Generate future business days (skipping weekends)."""
    dates = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=count)
    return list(dates)


def to_epoch_millis(ts: pd.Timestamp, is_daily: bool = False) -> int:
    """
    Convert timestamp to UTC epoch milliseconds matching TradingView internal bar times.
    - Intraday bars (NSE): localized to Asia/Kolkata (IST), so 09:15 IST becomes 03:45 UTC.
    - Daily bars: normalized to midnight UTC of the date (TradingView daily bar standard).
    """
    t = pd.Timestamp(ts)
    if is_daily:
        if t.tzinfo is not None:
            t = t.tz_convert(None)
        t = pd.Timestamp(t.date()).tz_localize("UTC")
    else:
        if t.tzinfo is None:
            t = t.tz_localize("Asia/Kolkata")
        else:
            t = t.tz_convert("Asia/Kolkata")
    return int(t.timestamp() * 1000)


def load_nifty_data(timeframe: str) -> pd.Series:
    """
    Load data from local Dhan files or OHLC.csv depending on the chosen timeframe.
    """
    tf = timeframe.lower()
    if tf in ("1d", "d", "daily"):
        if not OHLC_FILE.exists():
            raise FileNotFoundError(f"Missing {OHLC_FILE}")
        df = pd.read_csv(OHLC_FILE)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").dropna(subset=["Close"])
        return df.set_index("Date")["Close"]

    # Intraday: Load stored Dhan 1-minute candles and resample
    files = sorted(CANDLE_DIR.glob("20*.csv"))
    if not files:
        raise FileNotFoundError(f"No candle files found in {CANDLE_DIR}")

    selected_files = files[-15:]
    frames = []
    for f in selected_files:
        try:
            d = pd.read_csv(f)
            if not d.empty and "timestamp" in d and "close" in d:
                frames.append(d)
        except Exception:
            continue

    if not frames:
        raise RuntimeError("Could not parse local candle CSVs.")

    combined = pd.concat(frames, ignore_index=True)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"])
    combined = (
        combined.dropna(subset=["timestamp", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )

    df_indexed = combined.set_index("timestamp")

    if tf in ("5m", "5min"):
        resampled = df_indexed.resample("5min", offset="15min").agg({"close": "last"}).dropna()
        return resampled["close"]
    elif tf in ("15m", "15min"):
        resampled = df_indexed.resample("15min", offset="15min").agg({"close": "last"}).dropna()
        return resampled["close"]
    elif tf in ("1m", "1min"):
        return df_indexed["close"]
    else:
        raise ValueError(f"Unsupported timeframe '{timeframe}'. Use '5m', '15m', or '1d'.")


def run_forecast(timeframe: str = "5m", horizon: int | None = None, model_name: str = "google/timesfm-3.0-pytorch"):
    tf = timeframe.lower()
    default_horizon = {
        "1m": 30,
        "5m": 25,     # ~2 hours
        "15m": 25,    # 1 full trading session
        "1d": 30,     # 30 trading days
        "daily": 30
    }
    h = horizon or default_horizon.get(tf, 25)

    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Loading NIFTY 50 data for timeframe: {timeframe.upper()}...")
    close_series = load_nifty_data(timeframe)
    last_price = float(close_series.iloc[-1])
    last_ts = close_series.index[-1]

    print(f"Loaded {len(close_series)} bars. Last timestamp: {last_ts}, Last Close: {last_price:.2f}")

    # TimesFM context window: last 500-1000 points
    context = close_series.values.astype(np.float32)[-1000:]

    # Generate future timestamps
    if tf in ("1d", "d", "daily"):
        future_dates = generate_future_nse_daily_timestamps(pd.Timestamp(last_ts), h)
    elif tf in ("5m", "5min"):
        future_dates = generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 5, h)
    elif tf in ("15m", "15min"):
        future_dates = generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 15, h)
    else:
        future_dates = generate_future_nse_intraday_timestamps(pd.Timestamp(last_ts), 1, h)

    # Check if timesfm is installed
    try:
        from timesfm3 import TimesFM3Evaluator, ModelConfig
        use_timesfm3 = True
    except ImportError:
        try:
            import timesfm
            use_timesfm3 = False
        except ImportError:
            print("\n[!] PyTorch / timesfm not yet installed in local environment.")
            print("To run inference locally: pip install torch timesfm")
            print("Generating quantile projection based on recent volatility cone for Pine Script export...")
            returns = np.diff(np.log(context[-100:]))
            mu, sigma = np.mean(returns), np.std(returns)
            steps = np.arange(1, h + 1)
            drift = steps * mu
            diffusion = sigma * np.sqrt(steps)
            median = last_price * np.exp(drift)
            q10 = last_price * np.exp(drift - 1.28 * diffusion)
            q90 = last_price * np.exp(drift + 1.28 * diffusion)
            _render_output(timeframe, close_series, future_dates, last_price, median, q10, q90, is_sim=True)
            return

    print(f"Running TimesFM inference ({model_name}) for horizon = {h}...")
    if use_timesfm3:
        model = TimesFM3Evaluator(ModelConfig(checkpoint_path=model_name, device="cpu"))
        out = list(model.predict_batch(contexts=[context[-512:]], horizon=h, return_quantiles=True, use_symmetric_averaging=False))[0]
        median = out.quantiles[:, 4]
        q10 = out.quantiles[:, 0]
        q90 = out.quantiles[:, 8]
    else:
        tfm = timesfm.TimesFm(context_len=min(len(context), 512), horizon_len=h, input_patch_len=32, output_patch_len=128)
        tfm.load_from_checkpoint(repo_id="google/timesfm-1.0-200m")
        _, out = tfm.forecast(np.array([context]))
        median = out[0, :, 4]
        q10 = out[0, :, 0]
        q90 = out[0, :, 8]

    _render_output(timeframe, close_series, future_dates, last_price, median, q10, q90, is_sim=False)


def _render_output(timeframe, close_series, future_dates, last_price, median, q10, q90, is_sim: bool):
    h = len(future_dates)
    pct_chg = ((median[-1] / last_price) - 1) * 100
    tag = "Statistical Volatility Cone" if is_sim else "TimesFM 3.0"

    print("\n" + "=" * 60)
    print(f"NIFTY 50 FORECAST SUMMARY ({timeframe.upper()} · {h} Bars Ahead)")
    print(f"Engine: {tag}")
    print(f"Current Price: {last_price:.2f}")
    print(f"Median Target: {median[-1]:.2f} ({pct_chg:+.2f}%)")
    print(f"q10 (Support/Floor): {q10[-1]:.2f}")
    print(f"q90 (Resistance/Cap): {q90[-1]:.2f}")
    print("=" * 60)

    # Plot Chart
    plt.figure(figsize=(12, 6))
    lookback = min(len(close_series), 80)
    plt.plot(close_series.index[-lookback:], close_series.values[-lookback:], color="#E0E0E0", label="Historical Close", lw=1.5)
    plt.plot(future_dates, median, color="#00E5FF", label="Forecast Median", lw=2)
    plt.fill_between(future_dates, q10, q90, color="#00E5FF", alpha=0.18, label="q10–q90 Uncertainty Cone")
    plt.gca().set_facecolor("#0A0B10")
    plt.gcf().set_facecolor("#0A0B10")
    plt.legend(facecolor="#1A1C24", edgecolor="#333344", labelcolor="white")
    plt.title(f"NIFTY 50 — {timeframe.upper()} {h}-Bar Forecast Cone ({tag})", color="white", fontsize=13, pad=12)
    plt.tick_params(colors="white")
    plt.grid(color="#1F2330", linestyle="--", alpha=0.7)
    
    out_img = DATA_DIR / f"timesfm_forecast_{timeframe}.png"
    plt.savefig(out_img, dpi=150, bbox_inches="tight")
    print(f"\n[+] Saved chart to: {out_img}")

    # Generate TradingView Pine Script arrays
    is_daily = timeframe.lower() in ("1d", "d", "daily")
    anchor_ts = close_series.index[-1]
    all_dates = [anchor_ts] + list(future_dates)
    all_millis = [str(to_epoch_millis(d, is_daily=is_daily)) for d in all_dates]

    all_q50 = [last_price] + list(median)
    all_q10 = [last_price] + list(q10)
    all_q90 = [last_price] + list(q90)

    j = lambda v: ", ".join(f"{x:.2f}" for x in v)

    print("\n" + "─" * 60)
    print(f"📋 TRADINGVIEW PINE SCRIPT v6 ARRAYS ({timeframe.upper()})")
    print(f"Total Points: {len(all_millis)} (1 Anchor Bar + {h} Forecast Bars)")
    print("Copy the lines below and paste into your TradingView indicator:")
    print("─" * 60)
    print("var int[]   f_t   = array.from(" + ", ".join(all_millis) + ")")
    print("var float[] f_q50 = array.from(" + j(all_q50) + ")")
    print("var float[] f_q10 = array.from(" + j(all_q10) + ")")
    print("var float[] f_q90 = array.from(" + j(all_q90) + ")")
    print("─" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TimesFM NIFTY Forecaster")
    parser.add_argument("timeframe", nargs="?", default="5m", help="Timeframe: 5m, 15m, 1d")
    parser.add_argument("--horizon", "-H", type=int, default=None, help="Number of future bars to forecast")
    args = parser.parse_args()

    run_forecast(timeframe=args.timeframe, horizon=args.horizon)
