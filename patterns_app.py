"""
patterns_app.py — Standalone launcher for Candlestick Patterns, SMC & 15m Early Read.

Run:
    streamlit run patterns_app.py
"""
from __future__ import annotations

import glob
from pathlib import Path
import pandas as pd
import streamlit as st

from patterns.common import normalize
from patterns.tab import render as render_patterns_tab

st.set_page_config(
    page_title="NIFTY Candlestick Patterns & 15m Early Read",
    page_icon="🕯️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def load_candles():
    files = sorted(glob.glob("data/candles/*.csv"))
    regular_files = [f for f in files if not Path(f).name.startswith("vol_")]
    if not regular_files:
        regular_files = files
    if not regular_files:
        return pd.DataFrame()
    # load last 5 days
    pick = regular_files[-5:]
    dfs = [pd.read_csv(f) for f in pick if not pd.read_csv(f).empty]
    if not dfs:
        return pd.DataFrame()
    return normalize(pd.concat(dfs, ignore_index=True))


def main():
    st.sidebar.title("Patterns & Early Read")
    st.sidebar.caption("Stand-alone viewer for TA-Lib + SMC + 15m Early Read")

    df_1m = load_candles()
    spot = float(df_1m["close"].iloc[-1]) if not df_1m.empty else 23500.0

    st.sidebar.metric("Latest NIFTY Spot", f"₹{spot:,.1f}")
    if st.sidebar.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()

    pa_frames = {"1m": df_1m}
    render_patterns_tab(pa_frames=pa_frames, spot=spot)


if __name__ == "__main__":
    main()
