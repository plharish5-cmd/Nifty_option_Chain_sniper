"""TA-Lib candlestick pattern detection (closed bars only).

TA-Lib returns +100 (bullish) / -100 (bearish) / 0 per bar; some patterns return ±200
for a stronger variant. Doji returns +100 with no direction, so it is reported as NEUTRAL.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import talib

from .common import nearest_level

NEUTRAL = {"CDLDOJI"}


def _fn(name: str):
    fn = getattr(talib, name, None)
    if fn is None:
        raise ValueError(f"TA-Lib has no function {name}; fix patterns_rules.json")
    return fn


def detect_arrays(o, h, l, c, names) -> dict[str, np.ndarray]:
    """Run each pattern on raw arrays. Returns {name: int array}."""
    return {n: _fn(n)(o, h, l, c) for n in names}


def detect(df: pd.DataFrame, names) -> pd.DataFrame:
    """Pattern values per bar (index = bar start)."""
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
    res = detect_arrays(o, h, l, c, names)
    return pd.DataFrame(res, index=df.index)


def side_of(name: str, value: float) -> str | None:
    if value == 0:
        return None
    if name in NEUTRAL:
        return "NEUTRAL"
    return "BULL" if value > 0 else "BEAR"


def hits(df_tf: pd.DataFrame, rules: dict, levels=None, last_n: int = 20) -> pd.DataFrame:
    """Pattern hits on CLOSED bars of an already-resampled frame (needs `forming`).

    A pattern is flagged `at_level` when the bar's relevant extreme is within
    level_tolerance_pts of a supplied level: low for bullish, high for bearish, close for neutral.
    """
    names = list(rules["talib_patterns"])
    closed = df_tf[~df_tf["forming"]]
    if len(closed) < 3:
        return pd.DataFrame(columns=["time", "pattern", "side", "value", "at_level", "level", "dist"])
    vals = detect(closed, names)
    tol = rules["level_tolerance_pts"]
    rows = []
    for ts, row in vals.iloc[-last_n:].iterrows():
        bar = closed.loc[ts]
        for n in names:
            s = side_of(n, row[n])
            if not s:
                continue
            ref = bar["low"] if s == "BULL" else bar["high"] if s == "BEAR" else bar["close"]
            nl = nearest_level(ref, levels, tol)
            rows.append({
                "time": ts, "pattern": rules["talib_patterns"][n], "code": n, "side": s,
                "value": int(row[n]), "at_level": nl is not None,
                "level": f"{nl[0]} {nl[1]:.2f}" if nl else "", "dist": nl[2] if nl else np.nan,
            })
    return pd.DataFrame(rows).sort_values("time", ascending=False) if rows else pd.DataFrame(
        columns=["time", "pattern", "code", "side", "value", "at_level", "level", "dist"])
