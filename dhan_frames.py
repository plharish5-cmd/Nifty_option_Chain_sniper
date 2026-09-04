"""
dhan_frames.py — 15-minute OHLCV straight from Dhan, for the VWAP/EMA tab.

WHY THIS EXISTS SEPARATELY
──────────────────────────
Tab 18's whole rule is "VWAP or 8 EMA, whichever comes first". Without VWAP
it silently becomes an 8-EMA-only strategy — a different strategy that
enters at different times and different prices.

The app's global CANDLE_SOURCE has been "nse" throughout, and NSE's index
feed gives the app candles built from a price line. This module ignores that
setting and goes to Dhan directly, so the tab gets real volume regardless of
what the rest of the app is configured to use.

THE INDEX-HAS-NO-VOLUME PROBLEM
───────────────────────────────
An index is a calculation, not an instrument. Nothing trades "NIFTY 50", so
there is no such thing as its volume, and any VWAP on the index has to
borrow volume from somewhere.

Two honest choices, and this module offers both because they answer
different questions:

  FUTURES (default). Real traded volume on a real contract. The prices run
  15-30 points above spot — that is the basis, not an error — so a VWAP
  computed here sits above a spot chart by roughly the same amount. Right if
  you want the line institutions actually watch; wrong if you are marking up
  a spot chart.

  INDEX with futures volume. Spot prices weighted by futures volume, which
  is what price_action.add_vwap already does. The prices match your chart.
  The weighting is a proxy, and on a day when futures and cash diverge it is
  a worse proxy than usual.

Neither is "the" VWAP. Pick one, compare it against TradingView once, and
then stay with whichever matched — a line you have checked beats a line you
assumed.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

import config


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Market hours only, sorted, deduped."""
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    ts = pd.to_datetime(d["timestamp"])
    keep = ((ts.dt.time >= config.MARKET_OPEN)
            & (ts.dt.time <= config.MARKET_CLOSE))
    d = d[keep].copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    return (d.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True))


def fetch(interval: str = "15", days_back: int = 5,
          mode: str | None = None) -> tuple[pd.DataFrame, dict]:
    """
    OHLCV ready for indicators, plus a report of where each part came from.

    The report is returned rather than logged because the tab has to be able
    to say which VWAP it is showing. A VWAP with no stated source is the
    thing that caused this whole problem.
    """
    mode = mode or getattr(config, "TAB18_VWAP_MODE", "futures")
    info = {"mode": mode, "interval": f"{interval}m", "errors": [],
            "price_source": None, "volume_source": None}

    try:
        import dhan_client as dc
    except Exception as exc:                                # noqa: BLE001
        info["errors"].append(f"dhan_client unavailable: {exc}")
        return pd.DataFrame(), info

    # futures leg — needed for volume in both modes
    fut = pd.DataFrame()
    try:
        sid = dc.nifty_futures_security_id()
        if sid:
            fut = _clean(dc.intraday(sid, "NSE_FNO", "FUTIDX",
                                     days_back=days_back, interval=interval))
            info["futures_id"] = sid
        else:
            info["errors"].append("futures security id not resolved")
    except Exception as exc:                                # noqa: BLE001
        info["errors"].append(f"futures fetch failed: {exc}")

    if mode == "futures":
        if fut.empty:
            info["errors"].append("no futures candles — cannot build VWAP")
            return pd.DataFrame(), info
        info["price_source"] = "Dhan NIFTY futures"
        info["volume_source"] = "Dhan NIFTY futures (real traded volume)"
        return fut, info

    # index prices, futures volume
    try:
        idx = _clean(dc.intraday(config.DHAN_INDEX_SECURITY_ID, "IDX_I",
                                 "INDEX", days_back=days_back,
                                 interval=interval))
    except Exception as exc:                                # noqa: BLE001
        info["errors"].append(f"index fetch failed: {exc}")
        idx = pd.DataFrame()
    if idx.empty:
        info["errors"].append("no index candles")
        return fut if not fut.empty else pd.DataFrame(), info

    if fut.empty:
        info["price_source"] = "Dhan NIFTY index"
        info["volume_source"] = None
        info["errors"].append(
            "no futures volume available — VWAP will be missing, and this "
            "tab's whichever-comes-first rule degenerates to 8 EMA only")
        return idx, info

    merged = idx.merge(fut[["timestamp", "volume"]], on="timestamp",
                       how="left", suffixes=("_idx", ""))
    if "volume_idx" in merged.columns:
        merged = merged.drop(columns=["volume_idx"])
    matched = int(merged["volume"].notna().sum())
    merged["volume"] = merged["volume"].fillna(0.0)
    info["price_source"] = "Dhan NIFTY index"
    info["volume_source"] = (f"Dhan NIFTY futures, matched on {matched} of "
                             f"{len(merged)} bars")
    if matched < len(merged) * 0.9:
        info["errors"].append(
            f"only {matched}/{len(merged)} bars matched a futures bar — the "
            f"unmatched ones carry zero volume and contribute nothing to "
            f"VWAP, which pulls the line toward the bars that did match")
    return merged, info


def frame(interval: str = "15", days_back: int = 5,
          mode: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Fetch and load indicators — EMAs, bands, VWAP — ready for the tab."""
    import price_action as pa
    import setups as su

    raw, info = fetch(interval, days_back, mode)
    if raw.empty:
        return raw, info

    d = su.prepare(raw.set_index("timestamp"))
    d = d.reset_index()
    try:
        d = pa.add_vwap_self(d, price="hlc3")
        bars = int(d["vwap_bars"].iloc[-1]) if "vwap_bars" in d else 0
        info["vwap_bars"] = bars
        info["vwap_ok"] = bool(d["vwap"].notna().any())
    except Exception as exc:                                # noqa: BLE001
        info["errors"].append(f"vwap failed: {exc}")
        info["vwap_ok"] = False
    return d.set_index("timestamp"), info
