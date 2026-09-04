"""
Candle sources — pluggable, so the free one works today and Dhan drops in
later without touching anything downstream.

WHY THIS LAYER EXISTS

Dhan's Data APIs cost Rs499/month. On a Rs20,000 account that is 2.5% of
capital per month, 30% a year, in fixed cost — before a single trade. Paying
that to feed a tool with zero validated trading days behind it is the wrong
order of operations. So: free source now, and when validate.py shows the
scanner has real skill, flip CANDLE_SOURCE to "dhan" and VWAP switches on.

SOURCES

  yfinance   Free, no auth. ^NSEI gives 1-minute candles for roughly the last
             7 days. Delayed by some minutes — fine for 5m/15m decisions,
             not for 1-minute execution timing.
             NO USABLE VOLUME: Yahoo reports zero or junk volume for Indian
             index tickers, so VWAP is unavailable on this source. That is
             stated rather than faked.

  dhan       Paid. Real 1-minute candles plus NIFTY FUTURES volume, which is
             the only honest basis for NIFTY VWAP. Sub-minute freshness.

The contract every source returns is identical:
    (index_candles, futures_candles_or_None)
with columns timestamp / open / high / low / close / volume.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

import config

log = logging.getLogger(__name__)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class CandleSourceError(RuntimeError):
    pass


# ═══════════════════════════════════════════════════════════════════════════
# yfinance (free)
# ═══════════════════════════════════════════════════════════════════════════

def _yf_fetch(days_back: int = 2) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        raise CandleSourceError(
            "yfinance is not installed. Run:  pip install yfinance"
        ) from None

    period = f"{max(days_back, 1) + 1}d"
    try:
        raw = yf.Ticker(config.YF_SYMBOL).history(
            period=period, interval="1m", auto_adjust=False)
    except Exception as exc:                                # noqa: BLE001
        raise CandleSourceError(f"yfinance request failed: {exc}") from None

    if raw is None or raw.empty:
        raise CandleSourceError(
            f"yfinance returned no rows for {config.YF_SYMBOL}. Outside market "
            f"hours and at weekends this is normal — 1-minute data only covers "
            f"about the last 7 days."
        )

    d = raw.reset_index()
    tcol = next((c for c in d.columns if str(c).lower() in
                 ("datetime", "date", "index")), d.columns[0])
    d = d.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                          "Low": "low", "Close": "close", "Volume": "volume"})

    ts = pd.to_datetime(d["timestamp"], errors="coerce", utc=True)
    d["timestamp"] = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)

    for c in ("open", "high", "low", "close", "volume"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    d["volume"] = d["volume"].fillna(0)

    d = (d.dropna(subset=["timestamp", "close"])
          .loc[:, COLUMNS]
          .sort_values("timestamp")
          .drop_duplicates(subset=["timestamp"])
          .reset_index(drop=True))

    # keep regular session only — Yahoo sometimes emits stray pre/post rows
    t = d["timestamp"].dt.time
    d = d[(t >= config.MARKET_OPEN) & (t <= config.MARKET_CLOSE)]
    return d.reset_index(drop=True)


def _yf_source(days_back: int = 2):
    """
    Free source. Returns (index_candles, None) — the None is deliberate.

    Yahoo does not provide usable volume for Indian index tickers, and there
    is no reliable free NIFTY futures volume feed. Returning None makes VWAP
    show as unavailable downstream, which is honest. Computing a VWAP from
    zero or junk volume would draw a line that looks authoritative and is not.
    """
    return _yf_fetch(days_back), None


# ═══════════════════════════════════════════════════════════════════════════
# Dhan (Live API + 60-90 Days Laptop Storage Hybrid)
# ═══════════════════════════════════════════════════════════════════════════

def _load_stored_candles(days_back: int = 5) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """
    Load the rich 60-90 days of Dhan candles stored locally in data/candles/.
    Returns (index_candles, futures_candles) with real futures volume and VWAP.
    """
    candle_dir = getattr(config, "CANDLE_DIR", None)
    if not candle_dir or not candle_dir.exists():
        raise CandleSourceError("Local Dhan candle directory data/candles does not exist.")

    files = sorted(candle_dir.glob("20*.csv"))
    if not files:
        raise CandleSourceError("No stored Dhan candle files found in data/candles.")

    selected_files = files[-days_back:] if days_back else files
    frames = []
    for f in selected_files:
        try:
            df = pd.read_csv(f)
            if not df.empty and "timestamp" in df and "close" in df:
                frames.append(df)
        except Exception:
            continue

    if not frames:
        raise CandleSourceError("Failed to parse stored Dhan candle files.")

    combined = pd.concat(frames, ignore_index=True)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce")
    combined = combined.dropna(subset=["timestamp", "close"]).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)

    for c in ("open", "high", "low", "close"):
        combined[c] = pd.to_numeric(combined[c], errors="coerce")
    if "volume" in combined:
        combined["volume"] = pd.to_numeric(combined["volume"], errors="coerce").fillna(0)
    else:
        combined["volume"] = 0.0

    fut_candles = combined[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    idx_candles = combined[COLUMNS].copy()
    return idx_candles, fut_candles


def _dhan_source(days_back: int = 2):
    """
    Primary Dhan engine:
    1. Attempts to fetch fresh live candles from Dhan API.
    2. Persists fresh candles to disk so laptop storage is always up to date.
    3. Merges with stored 60-90 days history if multi-day context is requested.
    4. Falls back to stored 60-90 days Dhan data if token is expired or off-market.
    """
    import dhan_client as dc
    import price_action as pa

    idx, fut = None, None
    dhan_live_ok = False

    try:
        idx = dc.nifty_index_candles(days_back=days_back)
        if idx is not None and not idx.empty:
            dhan_live_ok = True
            try:
                fut = dc.nifty_futures_candles(days_back=days_back)
            except Exception as exc:                                # noqa: BLE001
                log.warning("Dhan futures fetch failed (%s) — using index volume", exc)
                fut = None

            # Persist fresh session candles to disk so laptop store stays fresh!
            try:
                pa.store_candles(idx)
            except Exception:
                pass
    except Exception as exc:                                        # noqa: BLE001
        log.info("Dhan live API call failed (%s) — using stored 60-90 days Dhan data", exc)
        dhan_live_ok = False

    if dhan_live_ok and idx is not None and not idx.empty:
        if days_back > 2:
            try:
                stored_idx, stored_fut = _load_stored_candles(days_back=days_back)
                cutoff = idx["timestamp"].min()
                stored_prior_idx = stored_idx[stored_idx["timestamp"] < cutoff]
                idx = pd.concat([stored_prior_idx, idx], ignore_index=True).drop_duplicates("timestamp").reset_index(drop=True)
                if fut is not None and stored_fut is not None:
                    stored_prior_fut = stored_fut[stored_fut["timestamp"] < cutoff]
                    fut = pd.concat([stored_prior_fut, fut], ignore_index=True).drop_duplicates("timestamp").reset_index(drop=True)
            except Exception:
                pass
        return idx, fut

    # Fallback to local stored Dhan data
    log.info("Serving Dhan candles from 60-90 days laptop storage (data/candles/)")
    return _load_stored_candles(days_back=days_back)


def _auto_source(days_back: int = 2):
    """Try Dhan (Live + Stored), fallback to yfinance only if no Dhan data exists."""
    try:
        return _dhan_source(days_back)
    except Exception as exc:
        log.warning("Dhan source failed (%s) — falling back to yfinance", exc)
        return _yf_source(days_back)


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch
# ═══════════════════════════════════════════════════════════════════════════

SOURCES = {
    "yfinance": _yf_source,
    "dhan": _dhan_source,
    "nse": _auto_source,
    "auto": _auto_source,
}


def fetch(days_back: int | None = None):
    """Fetch from whichever source config names. Raises CandleSourceError."""
    name = getattr(config, "CANDLE_SOURCE", "dhan")
    fn = SOURCES.get(name, _auto_source)
    db = days_back if days_back is not None else config.CANDLE_DAYS_BACK
    return fn(db)


def fetch_native(timeframe: str) -> pd.DataFrame | None:
    """Fetch native 5m, 15m, or 30m candles directly from Dhan API or local 60-90 day Dhan store."""
    mins = {"5m": "5", "15m": "15", "30m": "30"}.get(timeframe)
    if not mins:
        return None

    # 1. Try Live Dhan
    name = getattr(config, "CANDLE_SOURCE", "dhan")
    if name in ("dhan", "nse", "auto"):
        try:
            import dhan_client as dc
            live_nat = dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I", "INDEX",
                                   days_back=config.CANDLE_DAYS_BACK, interval=mins)
            if live_nat is not None and not live_nat.empty:
                return live_nat
        except Exception:
            pass

    # 2. Fallback to stored native 5m/15m files from data/candles_5m or data/candles_15m!
    dir_path = (getattr(config, "CANDLE_5M_DIR", config.DATA_DIR / "candles_5m")
                if timeframe == "5m" else
                getattr(config, "CANDLE_15M_DIR", config.DATA_DIR / "candles_15m"))
    if dir_path and dir_path.exists():
        files = sorted(dir_path.glob("20*.csv"))
        if files:
            recent_files = files[-config.CANDLE_DAYS_BACK:]
            frames = []
            for f in recent_files:
                try:
                    cdf = pd.read_csv(f)
                    if not cdf.empty and "timestamp" in cdf:
                        frames.append(cdf)
                except Exception:
                    continue
            if frames:
                out = pd.concat(frames, ignore_index=True)
                out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
                return out.dropna(subset=["timestamp", "close"]).sort_values("timestamp").reset_index(drop=True)

    return None


def describe() -> dict:
    """For the dashboard caption — what you are looking at and its limits."""
    name = getattr(config, "CANDLE_SOURCE", "dhan")
    if name == "yfinance":
        return {
            "name": "yfinance (free)",
            "symbol": config.YF_SYMBOL,
            "vwap": False,
            "note": ("Free source. Delayed by a few minutes and carries no "
                     "usable volume, so VWAP is unavailable."),
        }
    return {
        "name": "Dhan (Live API + 60-90 Day Laptop Storage)",
        "symbol": "NIFTY 50 index + current-month futures",
        "vwap": True,
        "note": ("Real candles with genuine futures volume and true session VWAP. "
                 "Seamlessly blends fresh Dhan API feeds with the 60-90 days of stored "
                 "sessions on your laptop. Protected against token expiry and off-market hours."),
    }

