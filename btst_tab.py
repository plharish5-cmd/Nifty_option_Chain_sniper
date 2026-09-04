"""
btst_tab.py — Streamlit UI for the Buy Today, Sell Tomorrow (BTST) Suite.

Implements Neeraj Joshi's two BTST strategies:
  1. Stock Momentum Basket: 5-6 stocks from distinct sectors closing near day high.
  2. Index Long Straddle: Overnight ATM CE + PE evaluation with theta drag and gap breakeven.

Cleanly separated from dashboard.py. Rendered via:
    import btst_tab
    btst_tab.render(chain_df, spot, expiry, dte, vix)
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import btst_scanner as bs
import config
import market_data as md
import nse_client

GREEN, RED, SLATE, GOLD = "#2FBF71", "#E8503A", "#8FA6B8", "#C9A227"
BLUE, AMBER = "#4A9BD1", "#E0A030"


@st.cache_data(ttl=120, show_spinner=False)
def _fetch_stock_universe(symbol: str = "NIFTY 100") -> list[dict[str, Any]]:
    """Fetches constituent rows from NSE for the chosen index."""
    try:
        client = nse_client.NSEClient()
        r = client._get(
            md.MARKET_WATCH_URL,
            {"functionName": "getIndicesData", "symbol": symbol},
        )
        if r.status_code == 200:
            block = client._json(r, f"marketWatch {symbol}").get("data", {})
            rows = block.get("data") or []
            if len(rows) > 1:
                return rows[1:]  # Exclude row 0 (the index itself)
    except Exception as e:
        pass
    return []


def _render_timing_banner(timing: bs.TimingStatus) -> None:
    """Displays the market timing status badge."""
    st.markdown(
        f"""
        <div style="
            background: #1e222d;
            border-left: 5px solid {timing.color};
            padding: 12px 18px;
            border-radius: 6px;
            margin-bottom: 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        ">
            <div>
                <span style="font-size: 1.1em; font-weight: 700; color: {timing.color};">
                    {timing.status}
                </span>
                <div style="color: #c9ced6; font-size: 0.88em; margin-top: 3px;">
                    {timing.message}
                </div>
            </div>
            <div style="text-align: right; color: #8fa6b8; font-size: 0.8em;">
                Neeraj Joshi BTST Window<br/>
                <span style="color: #ffffff; font-weight: 600;">3:10 PM - 3:25 PM</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_stock_btst_section() -> None:
    """Section 1: Stock Momentum BTST Basket (5-6 Stocks, Multi-Sector)."""
    st.subheader("1. Momentum Stock BTST Basket (Neeraj Joshi Rule)")
    st.caption(
        "Screen high-momentum stocks closing in the top 15-20% of their day's range with volume surges. "
        "Enforces **Sector Diversification** (max 1 stock per sector) to protect capital from sector shocks."
    )

    # Controls Row
    c1, c2, c3, c4 = st.columns([1.5, 1.2, 1.2, 1.5])
    with c1:
        universe = st.selectbox(
            "Universe",
            ["NIFTY 100", "NIFTY 500"],
            index=0,
            help="NIFTY 100 guarantees superior liquidity for morning exits.",
        )
    with c2:
        basket_size = st.selectbox("Basket Size", [5, 6], index=0)
    with c3:
        min_pct = st.slider("Min Momentum %", 0.5, 3.0, 1.0, 0.1)
    with c4:
        total_capital = st.number_input(
            "Total Capital (₹)",
            min_value=10000,
            max_value=10000000,
            value=100000,
            step=25000,
            help="Capital is divided equally across the 5-6 diversified stocks.",
        )

    # Fetch and Scan
    with st.spinner("Screening market constituents..."):
        raw_rows = _fetch_stock_universe(universe)

    if not raw_rows:
        st.warning(
            f"Could not load live {universe} constituents from NSE right now. "
            "Markets may be in maintenance, or connection throttled. "
            "Below is a demonstration layout using recent session leaders."
        )
        # Offline demonstration sample
        raw_rows = [
            {"symbol": "TATAMOTORS", "companyName": "Tata Motors Ltd", "lastPrice": 985.0, "open": 955.0, "dayHigh": 988.0, "dayLow": 950.0, "pChange": 3.2, "totalTradedValue": 12000000000},
            {"symbol": "TATASTEEL", "companyName": "Tata Steel Ltd", "lastPrice": 156.5, "open": 151.0, "dayHigh": 157.0, "dayLow": 150.0, "pChange": 3.1, "totalTradedValue": 6500000000},
            {"symbol": "HDFCBANK", "companyName": "HDFC Bank Ltd", "lastPrice": 1665.0, "open": 1630.0, "dayHigh": 1670.0, "dayLow": 1625.0, "pChange": 2.1, "totalTradedValue": 25000000000},
            {"symbol": "TCS", "companyName": "Tata Consultancy Services", "lastPrice": 4220.0, "open": 4140.0, "dayHigh": 4230.0, "dayLow": 4120.0, "pChange": 1.9, "totalTradedValue": 8500000000},
            {"symbol": "SUNPHARMA", "companyName": "Sun Pharmaceutical Ind", "lastPrice": 1810.0, "open": 1780.0, "dayHigh": 1815.0, "dayLow": 1775.0, "pChange": 1.7, "totalTradedValue": 4500000000},
            {"symbol": "LT", "companyName": "Larsen & Toubro Ltd", "lastPrice": 3650.0, "open": 3590.0, "dayHigh": 3660.0, "dayLow": 3580.0, "pChange": 1.6, "totalTradedValue": 7200000000},
        ]

    result = bs.scan_stock_btst(
        raw_rows,
        min_pchange=min_pct,
        max_pchange=7.0,
        max_upper_wick_ratio=0.22,
        min_turnover_cr=15.0,
        basket_size=basket_size,
    )

    # Summary KPI Bar
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("Total Screened", f"{result.all_screened_count} stocks")
    with k2:
        st.metric("Qualified Candidates", f"{result.qualified_count} stocks")
    with k3:
        st.metric("Sectors Represented", f"{len(result.sector_counts)} distinct")
    with k4:
        st.metric("Basket Avg Momentum", f"+{result.avg_pchange:.2f}%")

    if not result.basket:
        st.info("No stocks currently satisfy all strict BTST criteria (green close near day-high with turnover).")
        return

    st.markdown("#### 🎯 Selected Diversified BTST Basket")
    per_stock_cap = total_capital / len(result.basket)

    table_data = []
    for c in result.basket:
        shares = int(per_stock_cap // c.ltp) if c.ltp > 0 else 0
        allocated = shares * c.ltp
        table_data.append({
            "Stock": c.symbol,
            "Sector": c.sector,
            "LTP (₹)": f"₹{c.ltp:,.2f}",
            "Change (%)": f"+{c.pchange:.2f}%",
            "Proximity to High": f"{c.close_to_high_pct:.1f}%",
            "Morning Target (+1.8%)": f"₹{c.target_price:,.2f}",
            "Stop Loss (-1.2%)": f"₹{c.stop_loss:,.2f}",
            "Allocated Shares": f"{shares} qty",
            "Invested (₹)": f"₹{allocated:,.0f}",
            "Turnover": f"₹{c.turnover_cr:,.0f} Cr",
            "Score": c.score,
        })

    basket_df = pd.DataFrame(table_data)
    st.dataframe(basket_df, hide_index=True, use_container_width=True)

    # Breakdown & Allocation Visuals
    c_pie, c_notes = st.columns([1.2, 1.8])
    with c_pie:
        sec_df = pd.DataFrame(
            [{"Sector": k, "Stocks": v} for k, v in result.sector_counts.items()]
        )
        fig_sec = go.Figure(
            go.Pie(
                labels=sec_df["Sector"],
                values=sec_df["Stocks"],
                hole=0.45,
                marker=dict(colors=["#2FBF71", "#4A9BD1", "#C9A227", "#E0A030", "#9B51E0", "#E8503A"]),
            )
        )
        fig_sec.update_layout(
            title=dict(text="Sector Breadth Allocation", font=dict(color="#ffffff", size=13)),
            paper_bgcolor="#161922",
            plot_bgcolor="#161922",
            font=dict(color="#c9ced6", size=11),
            margin=dict(l=10, r=10, t=35, b=10),
            height=240,
            showlegend=True,
        )
        st.plotly_chart(fig_sec, use_container_width=True)

    with c_notes:
        st.markdown(
            """
            **Neeraj Joshi BTST Stock Execution Rules:**
            1. **Entry Timing:** Buy between **3:10 PM and 3:25 PM** (right before market close).
            2. **Order Type:** Use Limit orders around LTP or Market-on-Close (MOC).
            3. **Diversification:** Never invest all capital in one stock or single sector. A 5-6 sector basket insulates you from sudden overnight sectoral news.
            4. **Exit Window:** Next morning between **9:15 AM and 9:30 AM**. Pre-market gap-ups usually face profit taking after 9:30 AM.
            5. **Risk Rule:** If a stock opens gap-down by >1.2%, cut the loss immediately at 9:18 AM.
            """
        )


def _render_straddle_btst_section(
    chain_df: pd.DataFrame,
    spot: float,
    expiry: str,
    dte: float,
    vix: float | None,
) -> None:
    """Section 2: Index Long Straddle BTST Analyzer (ATM CE + ATM PE)."""
    st.subheader("2. Index Long Straddle BTST Analyzer (ATM CE + PE)")
    st.caption(
        "Buying both ATM Call and ATM Put overnight to profit from large market gaps. "
        "Evaluates the **Overnight Theta Drag** and calculates the exact **Breakeven Gap %** required to make a profit."
    )

    lot_size = getattr(config, "LOT_SIZE", 65)
    analysis = bs.evaluate_options_straddle(
        chain_df=chain_df,
        spot=spot,
        expiry=expiry,
        dte=dte,
        vix=vix,
        lot_size=lot_size,
    )

    if not analysis:
        st.info("Option chain data is not available or spot price is zero. Awaiting live feed.")
        return

    # Verdict Card
    vcol = "#2FBF71" if analysis.verdict == "FAVORABLE" else ("#E8503A" if analysis.verdict == "AVOID" else "#E0A030")
    st.markdown(
        f"""
        <div style="
            background: #1e222d;
            border-left: 5px solid {vcol};
            padding: 14px 18px;
            border-radius: 6px;
            margin-bottom: 16px;
        ">
            <span style="font-size: 1.15em; font-weight: 700; color: {vcol};">
                {analysis.verdict_badge}
            </span>
            <div style="color: #e1e4ea; font-size: 0.9em; margin-top: 4px;">
                {analysis.verdict_reason}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Key Metrics
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.metric("ATM Strike", f"{analysis.atm_strike:,.0f}", f"Spot: {spot:,.1f}")
    with m2:
        st.metric(
            "Straddle Cost",
            f"{analysis.straddle_pts:,.1f} pts",
            f"₹{analysis.straddle_cost_rs:,.0f} / lot",
        )
    with m3:
        st.metric(
            "Est Overnight Theta",
            f"-{analysis.est_overnight_theta_pts:,.1f} pts",
            f"-₹{analysis.est_overnight_theta_rs:,.0f} ({analysis.decay_pct}%)",
            delta_color="inverse",
        )
    with m4:
        st.metric(
            "Req Morning Gap",
            f"±{analysis.breakeven_gap_pct:.2f}%",
            f"±{analysis.breakeven_gap_pts:,.0f} pts",
            delta_color="off",
        )
    with m5:
        st.metric(
            "Upper / Lower BE",
            f"{analysis.upper_breakeven:,.0f}",
            f"{analysis.lower_breakeven:,.0f}",
        )

    # Payoff Scenario Chart & Table
    st.markdown("#### 📈 Morning Gap Payoff Simulator (9:18 AM Open)")

    sc_data = []
    gap_vals = []
    pnl_vals = []

    for s in analysis.scenarios:
        gap_vals.append(s.gap_pct)
        pnl_vals.append(s.pnl_per_lot_rs)
        col_tag = "🟢 Profit" if s.outcome == "PROFIT" else ("🔴 Loss" if s.outcome == "LOSS" else "⚪ Breakeven")
        sc_data.append({
            "Opening Gap (%)": f"{s.gap_pct:+.1f}%",
            "Index Open": f"{s.sim_spot:,.0f}",
            "Points Move": f"{s.gap_pts:+,.1f}",
            "Est Straddle Value": f"{s.est_straddle_val:,.1f} pts",
            "P&L per Lot (₹)": f"₹{s.pnl_per_lot_rs:+,.0f}",
            "P&L (%)": f"{s.pnl_pct:+,.1f}%",
            "Verdict": col_tag,
        })

    col_chart, col_tbl = st.columns([1.5, 1.2])

    with col_chart:
        fig_payoff = go.Figure()
        # Zero line
        fig_payoff.add_hline(y=0, line_dash="dash", line_color="#8fa6b8", line_width=1)
        
        # Upper & Lower BE vertical lines
        fig_payoff.add_vline(x=analysis.breakeven_gap_pct, line_dash="dot", line_color="#2FBF71",
                             annotation_text=f"+{analysis.breakeven_gap_pct}% BE", annotation_position="top right")
        fig_payoff.add_vline(x=-analysis.breakeven_gap_pct, line_dash="dot", line_color="#2FBF71",
                             annotation_text=f"-{analysis.breakeven_gap_pct}% BE", annotation_position="top left")

        # PnL Line
        fig_payoff.add_trace(go.Scatter(
            x=gap_vals,
            y=pnl_vals,
            mode="lines+markers",
            name="P&L per Lot (₹)",
            line=dict(color="#4A9BD1", width=2.5),
            marker=dict(
                size=8,
                color=["#2FBF71" if p > 0 else "#E8503A" for p in pnl_vals],
            ),
        ))

        fig_payoff.update_layout(
            title="Estimated P&L at 9:18 AM Market Open vs Overnight Gap %",
            xaxis_title="Index Opening Gap (%)",
            yaxis_title="Net P&L (₹ per lot)",
            paper_bgcolor="#161922",
            plot_bgcolor="#161922",
            font=dict(color="#c9ced6", size=11),
            margin=dict(l=20, r=20, t=35, b=20),
            height=300,
        )
        st.plotly_chart(fig_payoff, use_container_width=True)

    with col_tbl:
        sc_df = pd.DataFrame(sc_data)
        st.dataframe(sc_df, hide_index=True, use_container_width=True)

    st.markdown(
        """
        > [!IMPORTANT]
        > **Neeraj Joshi's Straddle Golden Rule:**
        > Never hold an overnight long straddle past **9:30 AM**. If the market opens with a gap, the winning leg spikes instantly—book profits immediately. If the market opens flat, cut both legs before regular session theta decay accelerates!
        """
    )


def _render_playbook_section() -> None:
    """Section 3: Neeraj Joshi Execution Playbook & Checklist."""
    with st.expander("📘 Neeraj Joshi BTST Playbook & Trader Checklist", expanded=False):
        c_a, c_b = st.columns(2)
        with c_a:
            st.markdown(
                """
                ### 📌 Stock BTST Strategy Rules
                1. **Screening Criteria:**
                   - Strong intraday closing in upper 15-20% of range ($LTP \ge 0.985 \times High$).
                   - Day candle must be green ($Close > Open$).
                   - Turnover $> ₹15 \text{ Cr}$ to guarantee institutional exit liquidity.
                2. **Sector Diversification:**
                   - Select 5–6 stocks from distinct sectors (e.g. IT, Auto, Metals, Pharma, Banks).
                   - Never allocate >25% to a single sector overnight.
                3. **Target & SL:**
                   - **Target:** $+1.5\%$ to $+2.0\%$ at 9:18 AM open.
                   - **Stop Loss:** $-1.2\%$ from entry.
                """
            )
        with c_b:
            st.markdown(
                """
                ### 📌 Index Long Straddle Strategy Rules
                1. **Position:** Buy ATM CE + ATM PE simultaneously between 3:15 PM and 3:25 PM.
                2. **Catalysts:** Best deployed prior to high-volatility event nights (Budget, US Fed, RBI, Elections, US CPI).
                3. **Critical Warning:**
                   - ❌ **AVOID** on pre-expiry days (DTE $\le 1$) due to massive overnight theta destruction.
                   - ❌ **AVOID** when India VIX $> 22$ (options already overpriced; morning IV crush hazard).
                4. **Morning Exit:** Exit both legs strictly between **9:16 AM and 9:25 AM**.
                """
            )


def render(
    chain_df: pd.DataFrame | None = None,
    spot: float = 0.0,
    expiry: str = "",
    dte: float = 4.0,
    vix: float | None = None,
) -> None:
    """Entry point for the BTST Suite tab in dashboard.py."""
    now = datetime.now()
    timing = bs.get_timing_status(now)
    _render_timing_banner(timing)

    # Two main sub-views
    sub_tab1, sub_tab2 = st.tabs([
        "📊 5-6 Stock Momentum Basket",
        "⚡ Index Long Straddle Analyzer",
    ])

    with sub_tab1:
        _render_stock_btst_section()

    with sub_tab2:
        if chain_df is not None and not chain_df.empty and spot > 0:
            _render_straddle_btst_section(chain_df, spot, expiry, dte, vix)
        else:
            st.warning("Live option chain data is currently not loaded. Showing offline straddle simulator.")
            # Build an illustrative synthetic chain
            mock_spot = 24500.0
            mock_data = []
            for s in range(24300, 24750, 50):
                d = abs(s - 24500)
                mock_data.append({
                    "strike": s,
                    "ce_ltp": max(15.0, 115.0 - (s - 24500) * 0.5),
                    "pe_ltp": max(15.0, 105.0 + (s - 24500) * 0.5),
                    "ce_iv": 13.5,
                    "pe_iv": 14.0,
                })
            mock_df = pd.DataFrame(mock_data)
            _render_straddle_btst_section(mock_df, mock_spot, "SAMPLE-EXPIRY", 3.0, 13.5)

    _render_playbook_section()
