"""Adapter around joshyattridge/smart-money-concepts (pip: smartmoneyconcepts).

Look-ahead facts, checked in the library source (v0.0.27) and handled here:
  * swing_highs_lows marks bar i using `swing_length` bars AFTER it, so a swing only
    becomes knowable swing_length bars later. We report each swing's confirm time.
  * bos_choch stores the event on the SWING bar; the actual break is BrokenIndex.
    Unbroken BOS/CHoCH are deleted by the library, so events are keyed on the BREAK bar.
  * fvg marks the middle candle and needs the next candle, so it confirms one bar later.
Run on CLOSED bars only; the forming candle is never passed in.

Do not confuse with the project's own smc.py. Use this as a cross-check against it.
"""
from __future__ import annotations

import os

os.environ.setdefault("SMC_CREDIT", "0")  # silence the import banner

import numpy as np
import pandas as pd
from smartmoneyconcepts import smc as _smc


def run(df_closed: pd.DataFrame, swing_length: int, close_break=True,
        close_mitigation=False, liquidity_range_percent=0.01) -> dict:
    """Returns dict of DataFrames: swings, structure, fvg, ob, liquidity (with timestamps)."""
    if len(df_closed) < swing_length * 2 + 3:
        return {"swings": pd.DataFrame(), "structure": pd.DataFrame(), "fvg": pd.DataFrame(),
                "ob": pd.DataFrame(), "liquidity": pd.DataFrame(),
                "note": f"need at least {swing_length * 2 + 3} closed bars"}
    times = df_closed.index
    ohlc = df_closed[["open", "high", "low", "close", "volume"]].reset_index(drop=True)
    n = len(ohlc)

    shl = _smc.swing_highs_lows(ohlc, swing_length=swing_length)
    sw_idx = np.where(~shl["HighLow"].isna())[0]
    swings = pd.DataFrame({
        "time": times[sw_idx],
        "type": np.where(shl["HighLow"].iloc[sw_idx] > 0, "Swing high", "Swing low"),
        "level": shl["Level"].iloc[sw_idx].to_numpy(),
        "confirmed_at": [times[min(i + swing_length, n - 1)] for i in sw_idx],
    })

    bc = _smc.bos_choch(ohlc, shl, close_break=close_break)
    ev_idx = np.where(~bc["BrokenIndex"].isna())[0]
    structure = pd.DataFrame({
        "swing_time": times[ev_idx],
        "break_time": [times[int(bc["BrokenIndex"].iloc[i])] for i in ev_idx],
        "event": ["CHoCH" if not np.isnan(bc["CHOCH"].iloc[i]) else "BOS" for i in ev_idx],
        "side": ["BULL" if np.nansum([bc["BOS"].iloc[i], bc["CHOCH"].iloc[i]]) > 0 else "BEAR"
                 for i in ev_idx],
        "level": bc["Level"].iloc[ev_idx].to_numpy(),
    }).sort_values("break_time")

    fv = _smc.fvg(ohlc)
    f_idx = np.where(~fv["FVG"].isna())[0]
    f_idx = f_idx[f_idx < n - 1]  # needs the next closed candle
    fvg = pd.DataFrame({
        "time": times[f_idx], "confirmed_at": times[f_idx + 1],
        "side": np.where(fv["FVG"].iloc[f_idx] > 0, "BULL", "BEAR"),
        "top": fv["Top"].iloc[f_idx].to_numpy(), "bottom": fv["Bottom"].iloc[f_idx].to_numpy(),
        "mitigated": [bool(fv["MitigatedIndex"].iloc[i]) and fv["MitigatedIndex"].iloc[i] > 0
                      for i in f_idx],
    })

    obs = _smc.ob(ohlc, shl, close_mitigation=close_mitigation)
    o_idx = np.where(~obs["OB"].isna())[0]
    ob = pd.DataFrame({
        "time": times[o_idx], "side": np.where(obs["OB"].iloc[o_idx] > 0, "BULL", "BEAR"),
        "top": obs["Top"].iloc[o_idx].to_numpy(), "bottom": obs["Bottom"].iloc[o_idx].to_numpy(),
        "strength_pct": obs["Percentage"].iloc[o_idx].to_numpy(),
    })

    lq = _smc.liquidity(ohlc, shl, range_percent=liquidity_range_percent)
    l_idx = np.where(~lq["Liquidity"].isna())[0]
    liquidity = pd.DataFrame({
        "time": times[l_idx], "side": np.where(lq["Liquidity"].iloc[l_idx] > 0, "Buy-side", "Sell-side"),
        "level": lq["Level"].iloc[l_idx].to_numpy(),
        "swept_at": [times[int(lq["Swept"].iloc[i])] if lq["Swept"].iloc[i] and not np.isnan(lq["Swept"].iloc[i])
                     and int(lq["Swept"].iloc[i]) < n else pd.NaT for i in l_idx],
    })
    return {"swings": swings, "structure": structure, "fvg": fvg, "ob": ob, "liquidity": liquidity,
            "note": f"swings confirm {swing_length} bars after the swing bar"}
