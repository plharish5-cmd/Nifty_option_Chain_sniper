"""
backfill_dhan_history.py — Backfills 90 days of 1m, 5m, and 15m historical candles from Dhan.
Saves each completed session into:
  - data/candles/YYYY-MM-DD.csv (1-minute)
  - data/candles_5m/YYYY-MM-DD.csv (5-minute)
  - data/candles_15m/YYYY-MM-DD.csv (15-minute)
Includes Index OHLC, real Futures Volume, and true session-anchored VWAP.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import dhan_client as dc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill")


def _clean_session(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    d = d.dropna(subset=["timestamp"])
    t = d["timestamp"].dt.time
    d = d[(t >= config.MARKET_OPEN) & (t <= config.MARKET_CLOSE)].copy()
    d["date"] = d["timestamp"].dt.date
    return d


def _process_and_save(idx_raw: pd.DataFrame, fut_raw: pd.DataFrame | None,
                      out_dir: Path, timeframe_name: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = _clean_session(idx_raw)
    fut = _clean_session(fut_raw) if fut_raw is not None else None

    if idx.empty:
        log.warning(f"No index data for {timeframe_name}")
        return 0

    saved_count = 0
    unique_dates = sorted(idx["date"].unique())
    for d in unique_dates:
        idx_d = idx[idx["date"] == d].sort_values("timestamp").reset_index(drop=True)
        if fut is not None and not fut.empty:
            fut_d = fut[fut["date"] == d].sort_values("timestamp").reset_index(drop=True)
            if not fut_d.empty:
                m = pd.merge_asof(
                    idx_d,
                    fut_d[["timestamp", "volume"]].rename(columns={"volume": "fut_volume"}),
                    on="timestamp", direction="nearest"
                )
                fvol = m["fut_volume"].fillna(0).astype(float)
                tp = (m["high"] + m["low"] + m["close"]) / 3.0
                cum_pv = (tp * fvol).cumsum()
                cum_v = fvol.cumsum()
                m["vwap"] = np.where(cum_v > 0, (cum_pv / cum_v).round(2), np.nan)
                m["volume"] = fvol
            else:
                m = idx_d.copy()
                m["vwap"] = np.nan
        else:
            m = idx_d.copy()
            m["vwap"] = np.nan

        cols = ["timestamp", "open", "high", "low", "close", "volume", "vwap"]
        out_df = m[cols].copy()
        out_df["timestamp"] = out_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

        p = out_dir / f"{d:%Y-%m-%d}.csv"
        out_df.to_csv(p, index=False)
        saved_count += 1

    log.info(f"{timeframe_name}: successfully saved {saved_count} session files to {out_dir}")
    return saved_count


def run_backfill(days_back: int = 90) -> None:
    log.info(f"Starting historical backfill for {days_back} days from Dhan...")

    # 1. Fetch Index
    log.info("Fetching Index candles (1m, 5m, 15m)...")
    idx_1m = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX", days_back=days_back, interval="1")
    idx_5m = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX", days_back=days_back, interval="5")
    idx_15m = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX", days_back=days_back, interval="15")

    # 2. Fetch Futures
    sid = dc.nifty_futures_security_id()
    log.info(f"Fetching Futures candles (SID: {sid})...")
    fut_1m = dc.intraday(sid, "NSE_FNO", "FUTIDX", days_back=days_back, interval="1") if sid else None
    fut_5m = dc.intraday(sid, "NSE_FNO", "FUTIDX", days_back=days_back, interval="5") if sid else None
    fut_15m = dc.intraday(sid, "NSE_FNO", "FUTIDX", days_back=days_back, interval="15") if sid else None

    # 3. Process & Save
    c1 = _process_and_save(idx_1m, fut_1m, config.CANDLE_DIR, "1-minute (1m)")
    c5 = _process_and_save(idx_5m, fut_5m, getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m"), "5-minute (5m)")
    c15 = _process_and_save(idx_15m, fut_15m, getattr(config, "CANDLE_15M_DIR", config.DATA_DIR / "candles_15m"), "15-minute (15m)")

    log.info(f"Backfill complete! 1m: {c1} sessions, 5m: {c5} sessions, 15m: {c15} sessions.")


if __name__ == "__main__":
    run_backfill()
