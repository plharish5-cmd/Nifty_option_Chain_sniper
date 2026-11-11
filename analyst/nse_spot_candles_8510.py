"""NSE Nifty spot OHLC: session-anchored 15m / 1H bars and 4/8 EMA (sec_id=13 only).

TradingView Nifty spot 15m uses 09:15 session buckets; 1H uses 09:15–10:15, not 09:00–10:00.
Matrix, scanner, and party-switch must share this module — not pandas default resample.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

_DEFAULT_CFG_PATH = Path(__file__).resolve().parent / "terminal8510_config.json"

NSE_SESSION_OPEN = "09:15"
NSE_SESSION_CLOSE = "15:30"
NSE_15M_OFFSET = "9h15min"
# Enough 1m history so 15m/1H EMA(4/8) match a TV chart with long history (~3–4 weeks).
EMA_WARMUP_1M_BARS = 12000

# TradingView built-in "MACD + EMA Cross" inputs (NIFTY spot chart reference).
TRADINGVIEW_EMA_CROSS_DEFAULTS: Dict[str, Any] = {
    "indicator": "MACD + EMA Cross",
    "fast_source": "close",
    "fast_length": 4,
    "slow_source": "close",
    "slow_length": 8,
    "momentum_source": "close",
    "momentum_ema_period": 12,
    "direction_source": "close",
    "direction_ema_period": 200,
    "macd_fast_length": 4,
    "macd_slow_length": 8,
    "macd_source": "close",
    "signal_smoothing": 9,
    "adjust": False,
}

_AGG_OHLCV = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def load_tradingview_ema_cross_config(cfg_path: Optional[Path] = None) -> Dict[str, Any]:
    out = dict(TRADINGVIEW_EMA_CROSS_DEFAULTS)
    path = cfg_path or _DEFAULT_CFG_PATH
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            block = data.get("tradingview_ema_cross")
            if isinstance(block, dict):
                out.update(block)
    except Exception:
        pass
    return out


def ema_tv(close: pd.Series, length: int, adjust: bool = False) -> pd.Series:
    """Pine `ta.ema(source, length)` — source Close, span=length, adjust=False."""
    return close.astype(float).ewm(span=int(length), adjust=bool(adjust)).mean()


def ema4_ema8(
    close: pd.Series,
    tv_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.Series, pd.Series]:
    cfg = tv_cfg or load_tradingview_ema_cross_config()
    fast = int(cfg.get("fast_length", 4))
    slow = int(cfg.get("slow_length", 8))
    adjust = bool(cfg.get("adjust", False))
    c = close.astype(float)
    return ema_tv(c, fast, adjust), ema_tv(c, slow, adjust)


def ema_momentum_direction(
    close: pd.Series, tv_cfg: Optional[Dict[str, Any]] = None
) -> Tuple[pd.Series, pd.Series]:
    """TV panel: momentum EMA(12) and direction EMA(200) on close — advisory only."""
    cfg = tv_cfg or load_tradingview_ema_cross_config()
    c = close.astype(float)
    mom = int(cfg.get("momentum_ema_period", 12))
    dire = int(cfg.get("direction_ema_period", 200))
    adjust = bool(cfg.get("adjust", False))
    return ema_tv(c, mom, adjust), ema_tv(c, dire, adjust)


def _session_slice(g: pd.DataFrame) -> pd.DataFrame:
    if g.empty:
        return g
    try:
        return g.between_time(NSE_SESSION_OPEN, NSE_SESSION_CLOSE)
    except Exception:
        return g


def epoch_seconds(values: Any) -> np.ndarray:
    """Datetimes -> whole epoch seconds, whatever unit pandas stores them in.

    `astype("int64") // 10**9` is only right when the values are held in
    nanoseconds. Newer pandas keeps seconds or microseconds, and the same
    line then returns 1 for every candle, which silently empties every
    backtest that walks 1-minute candles by timestamp.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(values))
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return np.asarray((idx - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1), dtype="int64")


def resample_nse_15m(df_indexed: pd.DataFrame, agg: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Build 15m bars anchored at 09:15 IST per calendar day."""
    agg = agg or _AGG_OHLCV
    use_cols = {k: v for k, v in agg.items() if k in df_indexed.columns}
    if not use_cols:
        use_cols = {"close": "last"}
    chunks = []
    for _, g in df_indexed.groupby(df_indexed.index.date):
        g = _session_slice(g)
        if g.empty:
            continue
        r = g.resample("15min", origin="start_day", offset=NSE_15M_OFFSET).agg(use_cols).dropna()
        if not r.empty:
            chunks.append(r)
    if not chunks:
        return pd.DataFrame()
    return pd.concat(chunks).sort_index()


def resample_nse_1h(df_indexed: pd.DataFrame, agg: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Build 1H bars anchored at 09:15 IST (09:15–10:15, 10:15–11:15, …)."""
    agg = agg or _AGG_OHLCV
    use_cols = {k: v for k, v in agg.items() if k in df_indexed.columns}
    if not use_cols:
        use_cols = {"close": "last"}
    chunks = []
    for _, g in df_indexed.groupby(df_indexed.index.date):
        g = _session_slice(g)
        if g.empty:
            continue
        r = g.resample("60min", origin="start_day", offset=NSE_15M_OFFSET).agg(use_cols).dropna()
        if not r.empty:
            chunks.append(r)
    if not chunks:
        return pd.DataFrame()
    return pd.concat(chunks).sort_index()


def attach_bar_timestamps(df_bars: pd.DataFrame, bar_seconds: int) -> pd.DataFrame:
    """Add ts (open) and close_ts for closed-bar lookups."""
    out = df_bars.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        out["ts"] = epoch_seconds(out.index)
    elif "ts" not in out.columns:
        out["ts"] = 0
    out["close_ts"] = out["ts"] + int(bar_seconds)
    return out.reset_index(drop=False).rename(columns={"index": "datetime"})


def resample_spot_from_1m(
    df_1m: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    df_1m must have IST DatetimeIndex and OHLC columns (open/high/low/close; volume optional).
    Returns (df_1m unchanged index, df_15m, df_1h) with ts + close_ts on resampled frames.
    """
    b15 = resample_nse_15m(df_1m)
    b1h = resample_nse_1h(df_1m)
    df15 = attach_bar_timestamps(b15, 900) if not b15.empty else b15
    df1h = attach_bar_timestamps(b1h, 3600) if not b1h.empty else b1h
    return df_1m, df15, df1h


def last_closed_bar_emas(
    bars: pd.DataFrame, offset: int = -2
) -> Tuple[Optional[pd.Timestamp], float, float, float]:
    """EMA 4/8 on closed bar at index offset (default -2 = last fully closed)."""
    if bars is None or len(bars) < abs(offset):
        return None, 0.0, 0.0, 0.0
    close_col = "close" if "close" in bars.columns else "c"
    c = bars[close_col].astype(float)
    e4, e8 = ema4_ema8(c)
    ts_idx = bars.index[offset]
    return (
        ts_idx,
        float(bars[close_col].iloc[offset]),
        float(e4.iloc[offset]),
        float(e8.iloc[offset]),
    )
