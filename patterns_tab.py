"""
patterns_tab.py — Root forwarding adapter for patterns.tab.render.

Conforms to standard tab contract:
    render(pa_frames, spot, read, prev_close, df, ctx)
"""
from __future__ import annotations

from typing import Any
import pandas as pd
from patterns.tab import render as _render_patterns_tab


def render(
    pa_frames: dict | None = None,
    spot: float | None = None,
    read: Any = None,
    prev_close: float | None = None,
    df: pd.DataFrame | None = None,
    ctx: Any = None,
) -> None:
    """Renders the Candlestick Patterns, SMC & 15m Early Read tab."""
    _render_patterns_tab(
        pa_frames=pa_frames,
        spot=spot,
        read=read,
        prev_close=prev_close,
        df=df,
        ctx=ctx,
    )
