"""
terminal_app.py — Standalone Institutional Prop-Desk Trading Terminal.

Run:
    streamlit run terminal_app.py

Merges real-time Options Flow (OI migration, GEX, sticky/squeeze zones)
with advanced Smart Money Concepts (MSB, FVG, OB, Breakers, SFP, Dragon Fruit combos).
"""

from __future__ import annotations

import glob
from pathlib import Path
import pandas as pd
import streamlit as st

import config
from views.terminal_tab import render_institutional_terminal

st.set_page_config(
    page_title="NIFTY Institutional Quant Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

def load_market_data():
    """Loads latest available candle data and option chain data."""
    # 1. Load Candle Data
    candle_files = sorted(glob.glob("data/candles/*.csv"))
    candle_df = None
    if candle_files:
        # Load the latest candle file
        latest_file = candle_files[-1]
        try:
            candle_df = pd.read_csv(latest_file)
            if "timestamp" in candle_df.columns:
                candle_df["timestamp"] = pd.to_datetime(candle_df["timestamp"])
        except Exception:
            pass

    if candle_df is None or len(candle_df) < 10:
        # Fallback to synthetic candle engine
        from engine.test_terminal_engine import generate_synthetic_candles
        candle_df = generate_synthetic_candles(n=75, base_price=24150.0)

    # 2. Load Option Chain
    chain_df = None
    chain_path = Path("data/sample_chain.csv")
    if chain_path.exists():
        try:
            import chain_parser as cp
            chain_df = cp.load_csv(chain_path)
        except Exception:
            pass

    spot = float(candle_df["close"].iloc[-1]) if (candle_df is not None and not candle_df.empty) else 24150.0
    return candle_df, chain_df, spot

def main():
    candle_df, chain_df, spot = load_market_data()

    # Sidebar controls
    with st.sidebar:
        st.markdown("### ⚡ Terminal Configuration")
        mode = st.selectbox("Data Source", ["Live / Latest Session", "Synthetic Stress Test"], index=0)
        if mode == "Synthetic Stress Test":
            from engine.test_terminal_engine import generate_synthetic_candles
            candle_df = generate_synthetic_candles(n=90, base_price=24100.0)
            spot = float(candle_df["close"].iloc[-1])

        st.caption("Institutional Prop-Desk v2.5 · Tabular Numerals Active")

    render_institutional_terminal(candle_df=candle_df, chain_df=chain_df, spot=spot)

if __name__ == "__main__":
    main()
