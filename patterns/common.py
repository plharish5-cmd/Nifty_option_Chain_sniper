"""Shared helpers: config, column normalisation, resampling, forming-candle flag.

Conventions (match the rest of nifty-scanner):
  * timestamps are naive IST, bar index = bar START time
  * input 1-min frame has columns open/high/low/close[/volume] (any case)
  * only CLOSED 1-min bars are used; the minute in progress is dropped
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("patterns")
RULES_PATH = Path(__file__).with_name("patterns_rules.json")
OHLC = ["open", "high", "low", "close"]


def load_rules(path: str | Path | None = None) -> dict:
    with open(path or RULES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case columns, datetime index, sorted, de-duplicated."""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if not isinstance(out.index, pd.DatetimeIndex):
        for cand in ("datetime", "timestamp", "time", "date"):
            if cand in out.columns:
                out = out.set_index(pd.to_datetime(out[cand])).drop(columns=[cand])
                break
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("1-min frame needs a DatetimeIndex or a datetime/timestamp column")
    if out.index.tz is not None:
        out.index = out.index.tz_convert("Asia/Kolkata").tz_localize(None)
    missing = [c for c in OHLC if c not in out.columns]
    if missing:
        raise ValueError(f"1-min frame missing columns: {missing}")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out[OHLC + ["volume"]].astype(float)


def closed_1m(df_1m: pd.DataFrame, now: pd.Timestamp | None) -> pd.DataFrame:
    """Drop the 1-min bar still in progress (start + 1 min > now)."""
    if now is None:
        return df_1m
    return df_1m[df_1m.index + pd.Timedelta(minutes=1) <= now]


def resample(df_1m: pd.DataFrame, minutes: int, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Aggregate CLOSED 1-min bars to N-min bars aligned to 09:15.

    Adds `forming` (True if the bucket has not finished) and `n1m` (1-min bars inside).
    09:15 is a multiple of 5 and 15 minutes from midnight, so default alignment is correct.
    """
    rule = f"{minutes}min"
    agg = df_1m.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    agg["n1m"] = df_1m["close"].resample(rule, label="left", closed="left").count()
    agg = agg[agg["n1m"] > 0].copy()
    bucket_end = agg.index + pd.Timedelta(minutes=minutes)
    if now is not None:
        agg["forming"] = bucket_end > now
    else:
        last_end = df_1m.index[-1] + pd.Timedelta(minutes=1)
        agg["forming"] = bucket_end > last_end
    return agg


def nearest_level(px: float, levels, tol: float):
    """levels: list of floats or dicts {'px':..,'name':..}. Returns (name, level_px, dist) or None."""
    best = None
    for lv in levels or []:
        lpx, name = (lv["px"], lv.get("name", "level")) if isinstance(lv, dict) else (float(lv), "level")
        d = px - lpx
        if abs(d) <= tol and (best is None or abs(d) < abs(best[2])):
            best = (name, lpx, round(d, 2))
    return best
