"""
paper_trading_tab.py — Streamlit UI Tab for Paper Trading, Impulse Control & Execution.

Features:
  1. Real-time account equity, available cash, margin, and Indian F&O charges tracking.
  2. Order Types & Execution:
     - Product Type: INTRADAY (MIS with auto square-off at 15:20 IST) vs NORMAL (NRML / Carry Forward / BTST / STBT).
     - Order Action: BUY (Long Option) vs SELL (Short / Option Writing / STBT).
  3. Live Order Placement with Dhan option prices, 65 lot size, and 1-click advisor pre-fill.
  4. Real-World Indian Market Realism: Accurately calculates Brokerage, STT, NSE Txn Fee,
     Stamp Duty, SEBI, IPFT, and 18% GST as per Dhan App official tariff.
  5. The "Discipline Gate": Live 15m Win Rate preview, TimesFM 3.0 Quantile & CTC Filter,
     Midday freeze checks, Daily Max Loss Circuit Breaker, and position sizing risk alerts.
  6. Live Open Positions terminal: Auto-SL/Target triggers, MIS 3:20 PM square-off,
     dynamic Trailing SL, 1-click product conversion (MIS ↔ NRML), and scale-out partial profit booking.
  7. Advanced Performance Analytics: Dual Equity Curve (Gross vs Net post-charges),
     BUY vs SELL breakdown, MIS vs NRML stats, Session breakdown, and Trade Journal CSV.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import paper_trading as pt
import trade_advisor_15m as ta
from ui_utils import render_html, _render_html

GREEN = "#2FBF71"
RED = "#E8503A"
SLATE = "#8FA6B8"
GOLD = "#C9A227"
BLUE = "#4A9BD1"
AMBER = "#E0A030"
PURPLE = "#9B51E0"

NIFTY_LOT_SIZE = getattr(config, "LOT_SIZE", getattr(config, "NIFTY_LOT_SIZE", 65))


def render_best_instruments_paper(
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    lot_size: int = 65,
) -> None:
    """
    Renders Top 3 CE and Top 3 PE instruments with exact Delta, Theta, Gamma,
    gain per 50-pt move, holding cost, and 1-click execution prefill for Paper Trading.
    """
    st.markdown("### 🎯 Best Instruments to Trade (Copy-Paste Watchlist)")
    st.caption("Categorized by trader risk profile with exact Delta, Theta, Gamma, and profit math.")

    atm = int(round(spot / 50.0) * 50) if spot > 0 else 24150
    ce_strikes = [
        (atm - 50, "🛡️ ITM 1 (Safe / High Delta)", "Moves fast with spot, lowest time decay risk.", "ITM"),
        (atm,      "⚖️ ATM (Balanced Sweet Spot)", "Best balance of capital cost, delta, and liquidity.", "ATM"),
        (atm + 50, "⚡ OTM 1 (High Gamma / Momentum)", "High leverage on fast breakouts. DO NOT hold during chop!", "OTM")
    ]
    pe_strikes = [
        (atm + 50, "🛡️ ITM 1 (Safe / High Delta)", "Moves fast with spot, lowest time decay risk.", "ITM"),
        (atm,      "⚖️ ATM (Balanced Sweet Spot)", "Best balance of capital cost, delta, and liquidity.", "ATM"),
        (atm - 50, "⚡ OTM 1 (High Gamma / Momentum)", "High leverage on fast breakdowns. DO NOT hold during chop!", "OTM")
    ]

    def get_strike_stats(k: int, side: str):
        ltp, delta, theta, gamma, vega = 120.0, 0.50, -10.0, 0.0015, 10.0
        if chain_df is not None and hasattr(chain_df, "empty") and not chain_df.empty:
            m = chain_df[chain_df["strike"] == k]
            if not m.empty:
                r = m.iloc[0]
                ltp = float(r.get(f"{side.lower()}_ltp", 120.0) or 120.0)
                delta = float(r.get(f"{side.lower()}_delta", 0.50) or 0.50)
                theta = float(r.get(f"{side.lower()}_theta", -10.0) or -10.0)
                gamma = float(r.get(f"{side.lower()}_gamma", 0.0015) or 0.0015)
                vega = float(r.get(f"{side.lower()}_vega", 10.0) or 10.0)
        else:
            diff = (spot - k) if side.lower() == "ce" else (k - spot)
            raw_d = round(0.50 + (diff / 100.0) * 0.15, 2)
            delta = min(0.85, max(0.20, raw_d)) if side.lower() == "ce" else -min(0.85, max(0.20, raw_d))
            ltp = max(5.0, round(100.0 + diff * 0.6, 1))
            theta = -round(max(4.0, 14.0 - abs(diff) * 0.05), 1)

        cost_lot = ltp * lot_size
        gain_50 = abs(delta) * 50 * lot_size
        decay_day = abs(theta) * lot_size
        return ltp, delta, theta, gamma, vega, cost_lot, gain_50, decay_day

    def build_df(strikes_info, side="ce"):
        rows = []
        for k, role, rationale, tag in strikes_info:
            ltp, delta, theta, gamma, vega, cost_lot, gain_50, decay_day = get_strike_stats(k, side)
            watch_sym = f"NIFTY {k} {side.upper()}"
            rows.append({
                "Trader Persona / Role": role,
                "Watchlist Symbol": watch_sym,
                "LTP": f"₹{ltp:,.2f}",
                "Cost / Lot": f"₹{cost_lot:,.0f}",
                "Delta (Δ)": f"{delta:.2f}",
                "Daily Theta Loss": f"-₹{decay_day:,.0f} / day",
                "Gamma (Γ)": f"{gamma:.4f}",
                "+50p NIFTY Move Gain": f"+₹{gain_50:,.0f}",
                "Rationale": rationale
            })
        return pd.DataFrame(rows)

    tab_ce, tab_pe, tab_greeks = st.tabs([
        "🟢 Top 3 Call Options (CE) to Trade",
        "🔴 Top 3 Put Options (PE) to Trade",
        "📚 The Greeks Order of Importance & Rules"
    ])

    with tab_ce:
        st.markdown("**Top Recommended Call Contracts (Choose One Based on Your Style)**")
        df_ce = build_df(ce_strikes, "ce")
        st.dataframe(
            df_ce,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Trader Persona / Role": st.column_config.TextColumn("Profile", width="medium"),
                "Watchlist Symbol": st.column_config.TextColumn("Symbol", width="medium"),
                "LTP": st.column_config.TextColumn("Premium", width="small"),
                "Cost / Lot": st.column_config.TextColumn("Capital / Lot (65 Qty)", width="small"),
                "Delta (Δ)": st.column_config.TextColumn("Delta (Speed)", width="small"),
                "Daily Theta Loss": st.column_config.TextColumn("Holding Cost", width="small"),
                "+50p NIFTY Move Gain": st.column_config.TextColumn("+50p Move Profit", width="small"),
                "Rationale": st.column_config.TextColumn("Why This Strike?", width="large"),
            }
        )
        st.info(f"💡 **Quick Copy for Broker Search**: `NIFTY {atm-50} CE` · `NIFTY {atm} CE` · `NIFTY {atm+50} CE`")

        cols_ce = st.columns(3)
        for idx, (k, role, rationale, tag) in enumerate(ce_strikes):
            ltp, delta, theta, gamma, vega, cost_lot, gain_50, decay_day = get_strike_stats(k, "ce")
            with cols_ce[idx]:
                if st.button(f"⚡ Trade {tag}: {k} CE (₹{ltp:.1f})", key=f"trade_ce_btn_{k}", use_container_width=True):
                    st.session_state["pt_order_strike"] = float(k)
                    st.session_state["pt_order_type"] = "CE (Call)"
                    st.session_state["pt_order_side"] = "🟢 BUY (Long)"
                    st.session_state["pt_order_sl"] = max(3.0, round(ltp * 0.85, 1))
                    st.session_state["pt_order_t1"] = round(ltp * 1.25, 1)
                    st.session_state["pt_order_t2"] = round(ltp * 1.50, 1)
                    st.session_state["pt_order_tag"] = f"🎯 Watchlist: {tag} ({k} CE)"
                    st.rerun()

    with tab_pe:
        st.markdown("**Top Recommended Put Contracts (Choose One Based on Your Style)**")
        df_pe = build_df(pe_strikes, "pe")
        st.dataframe(
            df_pe,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Trader Persona / Role": st.column_config.TextColumn("Profile", width="medium"),
                "Watchlist Symbol": st.column_config.TextColumn("Symbol", width="medium"),
                "LTP": st.column_config.TextColumn("Premium", width="small"),
                "Cost / Lot": st.column_config.TextColumn("Capital / Lot (65 Qty)", width="small"),
                "Delta (Δ)": st.column_config.TextColumn("Delta (Speed)", width="small"),
                "Daily Theta Loss": st.column_config.TextColumn("Holding Cost", width="small"),
                "+50p NIFTY Move Gain": st.column_config.TextColumn("+50p Move Profit", width="small"),
                "Rationale": st.column_config.TextColumn("Why This Strike?", width="large"),
            }
        )
        st.info(f"💡 **Quick Copy for Broker Search**: `NIFTY {atm+50} PE` · `NIFTY {atm} PE` · `NIFTY {atm-50} PE`")

        cols_pe = st.columns(3)
        for idx, (k, role, rationale, tag) in enumerate(pe_strikes):
            ltp, delta, theta, gamma, vega, cost_lot, gain_50, decay_day = get_strike_stats(k, "pe")
            with cols_pe[idx]:
                if st.button(f"⚡ Trade {tag}: {k} PE (₹{ltp:.1f})", key=f"trade_pe_btn_{k}", use_container_width=True):
                    st.session_state["pt_order_strike"] = float(k)
                    st.session_state["pt_order_type"] = "PE (Put)"
                    st.session_state["pt_order_side"] = "🟢 BUY (Long)"
                    st.session_state["pt_order_sl"] = max(3.0, round(ltp * 0.85, 1))
                    st.session_state["pt_order_t1"] = round(ltp * 1.25, 1)
                    st.session_state["pt_order_t2"] = round(ltp * 1.50, 1)
                    st.session_state["pt_order_tag"] = f"🎯 Watchlist: {tag} ({k} PE)"
                    st.rerun()

    with tab_greeks:
        st.markdown(
            """
            #### 🏆 The Quantitative Order of Importance for Option Buyers
            
            | Rank | Greek / Metric | Importance | What It Does | Pro-Trader Golden Rule |
            | :---: | :--- | :---: | :--- | :--- |
            | **#1** | **DELTA ($\Delta$)** | **40%** | **The Profit Engine**: Points your premium gains per 1 point NIFTY move. | **Never buy options with Delta $< 0.40$ for day trading.** Deep OTM options barely move even when your direction is right. |
            | **#2** | **THETA ($\Theta$)** | **30%** | **The Holding Tax**: Money you bleed per hour/day just by holding the contract. | **Always check $\Delta / \|\Theta\|$ ratio.** ITM options lose a smaller percentage of their value to time decay than cheap OTM lotteries. |
            | **#3** | **GAMMA ($\Gamma$)** | **20%** | **The Turbocharger**: How fast Delta expands when price moves in your favor. | **Highest at ATM on Expiry Days.** If trading an explosive breakout with high volume, ATM Gamma gives the highest percentage return. |
            | **#4** | **VEGA ($\nu$) & VIX** | **10%** | **The Volatility Wind**: Premium change per 1% change in India VIX. | **When VIX is crashing, avoid buying options.** IV crush will offset your directional gains. Buy when VIX is steady or turning up. |
            """
        )


def render_paper_trading_tab(
    pa_frames: Optional[Dict[str, pd.DataFrame]],
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
) -> None:
    """Renders the upgraded Paper Trading & Impulse Control Tab in Streamlit."""
    cur_time = now or datetime.now()
    atm_strike = round(spot / 50.0) * 50.0 if spot > 0 else 24150.0

    # 1. Load & Sync Account with Automated Triggers (SL / Target / Trailing / MIS Cutoff)
    acc = pt.load_paper_account()
    cfg = pt.load_paper_config()
    acc = pt.sync_open_positions(acc, spot=spot, chain_df=chain_df, auto_execute=True, now=cur_time)
    pt.save_paper_account(acc)

    daily_summary = pt.get_daily_pnl_summary(acc, now=cur_time)

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 1: HEADER & VIRTUAL CAPITAL BAR
    # ═══════════════════════════════════════════════════════════════════════
    _render_html(
        f"""
        <div style="background: linear-gradient(135deg, #071626 0%, #0E2238 100%);
                    border: 1px solid #4A9BD1; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h2 style="margin: 0; color: #FFFFFF; font-size: 22px; font-weight: 800;">
                        📝 Paper Trading Terminal & Execution Lab
                    </h2>
                    <p style="margin: 4px 0 0 0; color: #8FA6B8; font-size: 13px;">
                        Risk-Free Live Execution · MIS (Intraday @ 15:20) & NRML (Overnight / BTST / STBT) · Two-Sided Buy/Sell · Dhan Pricing & Taxes
                    </p>
                </div>
                <div style="text-align: right;">
                    <div style="font-size: 11px; color: #8FA6B8;">SPOT NIFTY</div>
                    <div style="font-size: 20px; font-weight: 800; color: #00D2D3; font-family: monospace;">
                        {spot:,.2f}
                    </div>
                </div>
            </div>
        </div>
        """
    )

    # Automated Trigger Notification Banners
    if acc.recent_alerts:
        for alert_msg in acc.recent_alerts[-3:]:
            if "🛑" in alert_msg or "⏰" in alert_msg:
                st.warning(alert_msg)
            elif "🎯" in alert_msg:
                st.success(alert_msg)
            else:
                st.info(alert_msg)

    # Circuit Breaker Banner
    if daily_summary["is_circuit_breaker_triggered"]:
        st.error(
            f"🚨 **DAILY LOSS CIRCUIT BREAKER ACTIVE**: Today's loss (-₹{abs(daily_summary['today_total_net_pnl']):,.2f}) "
            f"exceeded your daily risk threshold (₹{daily_summary['daily_loss_limit']:,.2f}). "
            "Trading is locked to protect your capital and eliminate tilt/revenge trading."
        )

    # Capital Overview Metrics Bar
    c_eq, c_cash, c_used, c_today, c_real, c_charges = st.columns(6)

    tot_pnl = acc.realized_pnl + acc.unrealized_pnl
    pnl_sign = "+" if tot_pnl >= 0 else ""

    c_eq.metric("Total Equity", f"₹{acc.total_equity:,.2f}", delta=f"{pnl_sign}₹{tot_pnl:,.2f}")
    c_cash.metric("Available Cash", f"₹{acc.cash_balance:,.2f}")
    c_used.metric("Margin In Use", f"₹{acc.used_margin:,.2f}")

    today_pnl = daily_summary["today_total_net_pnl"]
    today_sign = "+" if today_pnl >= 0 else ""
    c_today.metric("Today's Net P&L", f"₹{today_pnl:,.2f}", delta=f"{today_sign}₹{today_pnl:,.2f}")

    rel_sign = "+" if acc.realized_pnl >= 0 else ""
    c_real.metric("Total Net Realized", f"₹{acc.realized_pnl:,.2f}", delta=f"{rel_sign}₹{acc.realized_pnl:,.2f}")
    c_charges.metric(
        "All Charges (Dhan)",
        f"₹{acc.total_charges_paid:,.2f}",
        help="All Charges include Brokerage, GST, Securities Transaction Tax, SEBI Fees, Exchange Transaction Charges, Stamp Duty and other charges as mandated by the government, statutory and regulatory bodies.",
    )

    # Account Controls & Configuration Expanders
    col_exp1, col_exp2 = st.columns(2)

    with col_exp1:
        with st.expander("💳 Manage Virtual Capital (Refill / Reset Balance)", expanded=False):
            rf_col1, rf_col2, rf_col3 = st.columns(3)
            with rf_col1:
                if st.button("➕ Add ₹50,000", key="refill_50k", use_container_width=True):
                    acc = pt.refill_paper_capital(50000.0, reset_all=False)
                    st.success("Added ₹50,000 to cash balance!")
                    st.rerun()
            with rf_col2:
                if st.button("➕ Add ₹1,00,000", key="refill_100k", use_container_width=True):
                    acc = pt.refill_paper_capital(100000.0, reset_all=False)
                    st.success("Added ₹1,00,000 to cash balance!")
                    st.rerun()
            with rf_col3:
                if st.button("🔄 Clean Reset (₹1L)", key="reset_account_clean", use_container_width=True):
                    acc = pt.refill_paper_capital(100000.0, reset_all=True)
                    st.warning("Account reset to ₹1,00,000 clean. Positions cleared.")
                    st.rerun()

    with col_exp2:
        with st.expander("⚙️ Execution & Risk Guard Settings", expanded=False):
            cfg_col1, cfg_col2 = st.columns(2)
            with cfg_col1:
                new_daily_loss = st.number_input(
                    "Daily Max Loss Limit (₹)",
                    min_value=500.0,
                    max_value=50000.0,
                    value=float(cfg.get("daily_max_loss_limit", 3000.0)),
                    step=500.0,
                    key="cfg_daily_loss_input",
                )
                auto_sl_val = st.checkbox("Enable Auto Stop-Loss Exit", value=bool(cfg.get("auto_execute_sl", True)), key="cfg_auto_sl")
                mis_sq_val = st.text_input("MIS Auto Square-Off Time (IST)", value=str(cfg.get("mis_auto_squareoff_time", "15:20")), key="cfg_mis_time")
            with cfg_col2:
                auto_tgt_val = st.checkbox("Enable Auto Target Exit / Scale-Out", value=bool(cfg.get("auto_execute_target", True)), key="cfg_auto_tgt")
                slippage_val = st.number_input(
                    "Slippage Simulation (₹ pts)",
                    min_value=0.0,
                    max_value=2.0,
                    value=float(cfg.get("slippage_pts", 0.1)),
                    step=0.05,
                    key="cfg_slippage_input",
                )
                sell_margin_val = st.number_input(
                    "Option Selling Margin / Lot (₹)",
                    min_value=50000.0,
                    max_value=250000.0,
                    value=float(cfg.get("selling_margin_per_lot", 125000.0)),
                    step=5000.0,
                    key="cfg_sell_margin_input",
                )

            if st.button("💾 Save Risk Settings", key="save_cfg_btn", use_container_width=True):
                cfg["daily_max_loss_limit"] = new_daily_loss
                cfg["auto_execute_sl"] = auto_sl_val
                cfg["auto_execute_target"] = auto_tgt_val
                cfg["mis_auto_squareoff_time"] = mis_sq_val
                cfg["slippage_pts"] = slippage_val
                cfg["selling_margin_per_lot"] = sell_margin_val
                pt.save_paper_config(cfg)
                st.success("Risk settings updated successfully!")
                st.rerun()

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 2: BEST INSTRUMENTS WATCHLIST & ORDER PLACEMENT
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("🎯 Best Instruments to Trade (Copy-Paste Watchlist & Greeks Hierarchy)", expanded=True):
        render_best_instruments_paper(spot=spot, chain_df=chain_df, lot_size=NIFTY_LOT_SIZE)

    st.markdown("### 🛒 Place Paper Trade Order")

    col_order, col_discipline = st.columns([1.3, 1.1])

    # 15m Trade Advisor Analysis for Discipline Gate
    df_15m = pa_frames.get("15m") if pa_frames else None
    advice_res = ta.evaluate_15m_trade_advisor(
        df_15m=df_15m,
        spot=spot,
        chain_df=chain_df,
        selected_strike=atm_strike,
        selected_type="CE",
        now=cur_time,
    )

    # Session state defaults
    if "pt_order_strike" not in st.session_state:
        st.session_state["pt_order_strike"] = atm_strike
    if "pt_order_type" not in st.session_state:
        st.session_state["pt_order_type"] = "CE (Call)"
    if "pt_order_side" not in st.session_state:
        st.session_state["pt_order_side"] = "🟢 BUY (Long)"
    if "pt_order_product" not in st.session_state:
        st.session_state["pt_order_product"] = "⚡ INTRADAY (MIS - Auto Square-off @ 15:20)"
    if "pt_order_lots" not in st.session_state:
        st.session_state["pt_order_lots"] = 1
    if "pt_order_sl" not in st.session_state:
        st.session_state["pt_order_sl"] = max(3.0, round(advice_res.opt_sl_price, 1) if advice_res.opt_sl_price > 0 else 25.0)
    if "pt_order_t1" not in st.session_state:
        st.session_state["pt_order_t1"] = round(advice_res.opt_t1_price, 1) if advice_res.opt_t1_price > 0 else 45.0
    if "pt_order_t2" not in st.session_state:
        st.session_state["pt_order_t2"] = round(advice_res.opt_t2_price, 1) if advice_res.opt_t2_price > 0 else 65.0
    if "pt_order_tag" not in st.session_state:
        st.session_state["pt_order_tag"] = "🤖 Setup: AI TimesFM 3.0 + Price Action Confluence (CTC Rule)"

    # Generate strikes around ATM (+/- 8 strikes)
    strike_options = [atm_strike + (i * 50.0) for i in range(-8, 9)]
    current_selected_strike = st.session_state["pt_order_strike"]
    if current_selected_strike not in strike_options:
        strike_options.append(current_selected_strike)
        strike_options.sort()
    default_s_idx = strike_options.index(current_selected_strike)

    with col_order:
        # Quick 1-Click Fill from Advisor Banner
        if advice_res.action in ["BUY_CE", "BUY_PE"]:
            adv_type_clean = "CE" if "CE" in advice_res.action else "PE"
            adv_btn_text = f"⚡ 1-Click Pre-fill Advisor Setup: {advice_res.headline} ({int(advice_res.opt_strike)} {adv_type_clean})"

            if st.button(adv_btn_text, key="prefill_advisor_btn", use_container_width=True):
                st.session_state["pt_order_strike"] = float(advice_res.opt_strike)
                st.session_state["pt_order_type"] = f"{adv_type_clean} ({'Call' if adv_type_clean == 'CE' else 'Put'})"
                st.session_state["pt_order_side"] = "🟢 BUY (Long)"
                st.session_state["pt_order_sl"] = max(3.0, round(advice_res.opt_sl_price, 1))
                st.session_state["pt_order_t1"] = round(advice_res.opt_t1_price, 1)
                st.session_state["pt_order_t2"] = round(advice_res.opt_t2_price, 1)
                st.session_state["pt_order_tag"] = f"🤖 15m Advisor: {advice_res.headline}"
                st.rerun()

        st.markdown("##### 1. Select Product & Order Type")
        p_row1, p_row2 = st.columns(2)

        with p_row1:
            order_product = st.radio(
                "Product Type",
                options=[
                    "⚡ INTRADAY (MIS - Auto Square-off @ 15:20)",
                    "🌙 NORMAL (NRML / Carry Forward / BTST / STBT)",
                ],
                index=0 if "INTRADAY" in st.session_state.get("pt_order_product", "INTRADAY") else 1,
                key="pt_order_product",
            )
            clean_product = "INTRADAY" if "INTRADAY" in order_product else "NORMAL"

        with p_row2:
            order_side = st.radio(
                "Order Action",
                options=["🟢 BUY (Long)", "🔴 SELL (Short / Writing / STBT)"],
                index=0 if "BUY" in st.session_state.get("pt_order_side", "BUY") else 1,
                key="pt_order_side",
            )
            clean_side = "BUY" if "BUY" in order_side else "SELL"

        st.markdown("##### 2. Select Instrument & Lots")
        o_c1, o_c2, o_c3 = st.columns(3)

        with o_c1:
            order_strike = st.selectbox(
                "Nifty Strike",
                options=strike_options,
                index=default_s_idx,
                format_func=lambda s: f"{int(s)} {'(ATM)' if s == atm_strike else ''}",
                key="pt_order_strike",
            )

        with o_c2:
            type_idx = 0 if "CE" in st.session_state.get("pt_order_type", "CE") else 1
            order_type = st.radio(
                "Option Type",
                options=["CE (Call)", "PE (Put)"],
                index=type_idx,
                horizontal=True,
                key="pt_order_type",
            )
            clean_type = "CE" if "CE" in order_type else "PE"

        with o_c3:
            lots = st.selectbox(
                "Lot Size",
                options=[1, 2, 3, 4, 5, 6, 8, 10],
                index=0,
                format_func=lambda l: f"{l} Lot{'s' if l > 1 else ''} ({l * NIFTY_LOT_SIZE} Qty)",
                key="pt_order_lots",
            )
            total_qty = lots * NIFTY_LOT_SIZE

        # Live Option LTP
        live_ltp = pt.get_current_option_price(order_strike, clean_type, spot, chain_df)

        # Margin Requirement calculation
        if clean_side == "SELL":
            margin_per_lot = float(cfg.get("selling_margin_per_lot", 125000.0))
            req_margin = round(lots * margin_per_lot, 2)
            margin_info = f"Required: ₹{req_margin:,.0f} (₹{margin_per_lot:,.0f}/lot option selling SPAN margin)"
        else:
            req_margin = round(live_ltp * total_qty, 2)
            margin_info = f"Required: ₹{req_margin:,.2f} (Available: ₹{acc.cash_balance:,.0f})"

        # Estimated Dhan App Brokerage, STT & Statutory Charges
        est_exit = live_ltp + 15.0 if clean_side == "BUY" else max(1.0, live_ltp - 15.0)
        est_charges = pt.calculate_trade_charges(live_ltp, est_exit, total_qty, side=clean_side)

        p_c1, p_c2, p_c3 = st.columns(3)
        p_c1.metric("Live Option LTP", f"₹{live_ltp:.2f}", help="Current price fetched from live Dhan option chain")
        p_c2.metric("Total Quantity", f"{total_qty} units", help=f"{lots} Lot(s) × {NIFTY_LOT_SIZE} Qty")
        p_c3.metric("Required Margin", f"₹{req_margin:,.2f}", delta=f"Cash: ₹{acc.cash_balance:,.0f}")

        # Stop Loss & Target Settings
        st.markdown("##### 3. Stop Loss, Targets & Trailing SL")
        s_c1, s_c2, s_c3 = st.columns(3)

        if clean_side == "BUY":
            default_sl_calc = max(3.0, round(live_ltp - 8.0, 1))
            default_t1_calc = round(live_ltp + 12.0, 1)
            default_t2_calc = round(live_ltp + 24.0, 1)
        else:
            # For Option Sellers: SL is higher, Target is lower
            default_sl_calc = round(live_ltp + 8.0, 1)
            default_t1_calc = max(1.0, round(live_ltp - 12.0, 1))
            default_t2_calc = max(0.5, round(live_ltp - 24.0, 1))

        with s_c1:
            order_sl = st.number_input(
                f"Stop Loss (₹) {'[Trigger if price drops]' if clean_side == 'BUY' else '[Trigger if price rises]'}",
                min_value=0.0,
                max_value=3000.0,
                value=float(st.session_state.get("pt_order_sl", default_sl_calc)),
                step=0.5,
                key="pt_order_sl",
            )
        with s_c2:
            order_t1 = st.number_input(
                f"Target 1 (₹) {'[Trigger if price rises]' if clean_side == 'BUY' else '[Trigger if price drops]'}",
                min_value=0.0,
                max_value=3000.0,
                value=float(st.session_state.get("pt_order_t1", default_t1_calc)),
                step=0.5,
                key="pt_order_t1",
            )
        with s_c3:
            order_t2 = st.number_input(
                "Target 2 / Runner (₹)",
                min_value=0.0,
                max_value=3000.0,
                value=float(st.session_state.get("pt_order_t2", default_t2_calc)),
                step=0.5,
                key="pt_order_t2",
            )

        tr_c1, tr_c2 = st.columns([1.2, 1.8])
        with tr_c1:
            trailing_sl_pts = st.number_input(
                "Dynamic Trailing SL (pts)",
                min_value=0.0,
                max_value=50.0,
                value=0.0,
                step=1.0,
                help="0 = Disabled. If set to 5, SL automatically trails 5 pts behind favorable peak price.",
                key="pt_order_trail",
            )

        with tr_c2:
            with st.expander(f"🧾 Dhan App Charges Breakdown (~₹{est_charges['total_charges']:.2f})", expanded=False):
                st.markdown(
                    f"""
                    <div style="font-size: 11px; color: #CCD6F6; line-height: 1.6;">
                        • <b>Brokerage:</b> ₹{est_charges['brokerage']:.2f} (Buy ₹20 + Sell ₹20)<br>
                        • <b>STT (0.1% on Sell leg turnover):</b> ₹{est_charges['stt']:.2f}<br>
                        • <b>Exchange Txn Fee (NSE 0.03503%):</b> ₹{est_charges['exchange_fee']:.2f}<br>
                        • <b>Stamp Duty (0.003% on Buy leg):</b> ₹{est_charges['stamp_duty']:.2f}<br>
                        • <b>SEBI Turnover Fee (0.0001%):</b> ₹{est_charges['sebi_fee']:.2f}<br>
                        • <b>GST (18% on Brokerage+NSE+SEBI):</b> ₹{est_charges['gst']:.2f}<br>
                        • <b>Total Round-Trip Charges:</b> <b style="color: #FFD166;">₹{est_charges['total_charges']:.2f}</b><br>
                        • <b>Net Breakeven Target:</b> <b style="color: #00D2D3;">+{est_charges['breakeven_pts']:.2f} pts</b>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            st.caption(f"💡 Total Dhan Charges: **₹{est_charges['total_charges']:.2f}** · Breakeven: **+{est_charges['breakeven_pts']:.2f} pts**")

        # Strategy Tag (Mandatory for Discipline Tracking)
        setup_tag_choices = [
            "🤖 Setup: AI TimesFM 3.0 + Price Action Confluence (CTC Rule)",
            "⚡ 15m 8-EMA Pullback Reversal",
            "⚔️ WAR Setup (30m FII First Candle)",
            "🌊 1m 3-Drive Institutional Scalp",
            "📦 SMC Order Block / Fair Value Gap",
            "🌙 BTST / STBT Overnight Setup",
            "⚠️ Impulse / Gut Feel (No Rule)",
        ]
        tag_idx = 0
        if st.session_state.get("pt_order_tag") in setup_tag_choices:
            tag_idx = setup_tag_choices.index(st.session_state["pt_order_tag"])

        chosen_tag = st.selectbox(
            "🏷️ Why are you taking this trade? (Impulse Gate)",
            options=setup_tag_choices,
            index=tag_idx,
            key="pt_order_tag",
        )

        trade_notes = st.text_input("📝 Trade Thesis / Note (Optional)", placeholder="e.g. 8-EMA bounce + bullish hammer on 15m", key="pt_trade_note")

        # Circuit Breaker Override Checkbox
        override_lock = False
        if daily_summary["is_circuit_breaker_triggered"]:
            override_lock = st.checkbox(
                "⚠️ I acknowledge the daily loss limit is hit and consciously choose to override lock",
                key="pt_override_risk_check",
            )

        # RVOL Gate evaluation (Layer 3 Chop Filter — Ported from Validated Build)
        import rvol_gate
        cur_d = cur_time.date()
        cur_t = cur_time.time()
        rvol_dec = rvol_gate.get_gate_decision(cur_d, cur_t)
        rvol_val = rvol_dec.get("rvol_at_gate")
        rvol_regime = rvol_dec.get("regime", "NA")

        # Range structure check: wall fades, iron condors, strangles are allowed in chop
        is_range_structure = any(term in chosen_tag.lower() for term in ["fade", "range", "condor", "bounce", "strangle", "iron", "scalp"])
        is_breakout_type = not is_range_structure

        # Box 6: RVOL gate: not CHOP ZONE (auto-checked if RVOL >= 0.85 or trade is range-type)
        box6_passed = (rvol_dec.get("status") != "SUPPRESSED") or is_range_structure
        rvol_blocked = (not box6_passed) and is_breakout_type

        # Place Order Button ("Log trade" disabled if Box 6 fails and trade is breakout-type)
        can_afford = req_margin <= acc.cash_balance
        breaker_blocked = daily_summary["is_circuit_breaker_triggered"] and not override_lock
        btn_disabled = (not can_afford) or breaker_blocked or rvol_blocked

        action_emoji = "🚀" if clean_side == "BUY" else "🔻"
        btn_label = f"{action_emoji} {clean_side} {lots} LOT(S) ({total_qty} Qty) [{clean_product}] @ ₹{live_ltp:.1f}"

        if st.button(btn_label, type="primary", disabled=btn_disabled, use_container_width=True):
            ok, msg, acc = pt.place_paper_order(
                strike=order_strike,
                opt_type=clean_type,
                lots=lots,
                spot=spot,
                side=clean_side,
                product_type=clean_product,
                chain_df=chain_df,
                stop_loss=order_sl,
                target_1=order_t1,
                target_2=order_t2,
                setup_tag=chosen_tag,
                trailing_sl_pts=trailing_sl_pts,
                notes=trade_notes,
                override_risk_limit=override_lock,
                rvol=rvol_val,
                rvol_regime=rvol_regime,
            )
            if ok:
                st.success(f"✅ {msg}")
                st.rerun()
            else:
                st.error(f"❌ {msg}")

        if rvol_blocked:
            rvol_str = f"{rvol_val:.2f}×" if rvol_val is not None else "N/A"
            st.error(
                f"⚠️ **Log Trade Disabled by RVOL Gate (Box 6 Fail):** RVOL is {rvol_str} ({rvol_regime}). "
                "False breakouts common below 1.30. Wait for RVOL >= 1.30 before any entry, or select a Range Structure (e.g. Wall Fade / Condor)."
            )

        if not can_afford:
            st.error(f"⚠️ Insufficient capital! Required ₹{req_margin:,.2f} for {clean_side} but available cash is ₹{acc.cash_balance:,.2f}.")

    # ── THE DISCIPLINE GATE (Impulse Control Checklist) ───────────────────────
    with col_discipline:
        st.markdown("##### 🛡️ The Impulse Control Gate")

        eval_advice = ta.evaluate_15m_trade_advisor(
            df_15m=df_15m,
            spot=spot,
            chain_df=chain_df,
            selected_strike=order_strike,
            selected_type=clean_type,
            now=cur_time,
        )

        wr_color = GREEN if eval_advice.win_rate >= 62.0 else (RED if eval_advice.win_rate < 50.0 else AMBER)

        _render_html(
            f"""
            <div style="background: #0E2238; border: 1px solid {wr_color}; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 13px; font-weight: 700; color: #FFFFFF;">15m Structural Win Rate:</span>
                    <span style="font-size: 16px; font-weight: 800; color: {wr_color}; font-family: monospace;">
                        {eval_advice.win_rate:.1f}%
                    </span>
                </div>
                <div style="font-size: 12px; color: #CCD6F6; margin-top: 4px;">
                    {eval_advice.headline} — {eval_advice.sub_headline}
                </div>
            </div>
            """
        )

        # Pre-trade Checklist Box 6: RVOL Trend Zone (>=1.30)
        if rvol_dec.get("status") == "HOLD":
            b6_badge = "HOLD ⏸️ (<09:30)"
            b6_color = SLATE
            b6_desc = "First RVOL reading available from 09:30 onward."
        elif box6_passed:
            b6_badge = f"PASS ✅ ({rvol_val:.2f}× · {rvol_regime})" if rvol_val else f"PASS ✅ ({rvol_regime})"
            b6_color = GREEN
            b6_desc = "RVOL in trend zone (>=1.30) — volume runway clear." if not is_range_structure else "Range structure permitted in chop/trap zone."
        else:
            b6_badge = f"FAIL ❌ ({rvol_val:.2f}× · {rvol_regime})"
            b6_color = RED
            b6_desc = "⚠️ RVOL < 1.30 (DEAD/TRAP ZONE). Wait for RVOL >= 1.30 before any entry."

        _render_html(
            f"""
            <div style="background: #0E2238; border: 1.5px solid {b6_color}; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 12.5px; font-weight: 700; color: #FFFFFF;">Checklist Box 6: RVOL Trend Zone (>=1.30)</span>
                    <span style="font-size: 11.5px; font-weight: 800; color: {b6_color}; font-family: monospace;">{b6_badge}</span>
                </div>
                <div style="font-size: 11px; color: #CCD6F6; margin-top: 3px;">
                    {b6_desc}
                </div>
            </div>
            """
        )

        # Google TimesFM 3.0 Quantile & CTC Filter
        ai_stat = eval_advice.ai_gate_status
        if clean_type == "CE" and "BLOCKED" in ai_stat:
            _render_html(
                f"""
                <div style="background: rgba(232, 80, 58, 0.2); border: 2px solid {RED}; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;">
                    <b style="color: {RED}; font-size: 12px;">⛔ AI GATE VETO: CE BLOCKED AT Q90 CAP</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 3px;">
                        Spot ({spot:,.1f}) is within {eval_advice.ai_dist_to_cap:.1f} pts of TimesFM q90 Cap ({eval_advice.ai_q90_cap:,.1f}). 
                        Historical backtesting confirms <b>87% containment</b>. Upside is statistically exhausted!
                    </div>
                </div>
                """
            )
        elif clean_type == "PE" and "BLOCKED" in ai_stat:
            _render_html(
                f"""
                <div style="background: rgba(232, 80, 58, 0.2); border: 2px solid {RED}; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;">
                    <b style="color: {RED}; font-size: 12px;">⛔ AI GATE VETO: PE BLOCKED AT Q10 FLOOR</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 3px;">
                        Spot ({spot:,.1f}) is within {eval_advice.ai_dist_to_floor:.1f} pts of TimesFM q10 Floor ({eval_advice.ai_q10_floor:,.1f}). 
                        Downside is statistically exhausted!
                    </div>
                </div>
                """
            )
        else:
            _render_html(
                f"""
                <div style="background: rgba(47, 191, 113, 0.15); border: 1px solid {GREEN}; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <b style="color: {GREEN}; font-size: 12px;">✅ AI CONFLUENCE RUNWAY CLEAR</b>
                        <span style="background: {GREEN}; color: #FFFFFF; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px;">{ai_stat.replace('_', ' ')}</span>
                    </div>
                    <div style="font-size: 11px; color: #CCD6F6; margin-top: 4px;">
                        Runway to boundary: <b>{eval_advice.ai_dist_to_cap if clean_type == 'CE' else eval_advice.ai_dist_to_floor:.1f} pts</b> · 
                        Dynamic T1: <b>{eval_advice.ai_q50_median:,.1f}</b> · 
                        <b>CTC Rule:</b> Shift SL to cost at +20 pts spot excursion.
                    </div>
                </div>
                """
            )

        # Midday Freeze Check
        cur_t = cur_time.time()
        is_midday = (cur_t >= datetime.strptime("11:30", "%H:%M").time()) and (cur_t <= datetime.strptime("13:15", "%H:%M").time())
        if is_midday and clean_product == "INTRADAY":
            _render_html(
                f"""
                <div style="background: rgba(232, 80, 58, 0.15); border: 1px solid {RED}; border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                    <b style="color: {RED}; font-size: 12px;">⚠️ MIDDAY FREEZE ACTIVE (11:30 – 13:15)</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 2px;">
                        Theta decay is at peak speed while volume dries up. 80% of retail losses occur right now. Wait for 13:15 European flow.
                    </div>
                </div>
                """
            )

        # Risk Factors Warning
        if eval_advice.confluences_negative:
            risk_factors_inner = "".join(f"<div style='font-size: 11px; color: #FFFFFF;'>• {neg}</div>" for neg in eval_advice.confluences_negative[:4])
            _render_html(
                f"""
                <div style="background: rgba(224, 160, 48, 0.15); border: 1px solid {AMBER}; border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                    <b style="color: {AMBER}; font-size: 12px;">⚠️ NEGATIVE CONFLUENCES DETECTED:</b>
                    {risk_factors_inner}
                </div>
                """
            )

        # Capital Risk Guard
        risk_pct = (req_margin / max(1.0, acc.total_equity)) * 100.0
        if risk_pct > float(cfg.get("max_capital_allocation_pct", 25.0)):
            _render_html(
                f"""
                <div style="background: rgba(232, 80, 58, 0.15); border: 1px solid {RED}; border-radius: 6px; padding: 10px;">
                    <b style="color: {RED}; font-size: 12px;">🚨 OVERSIZED POSITION WARNING</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 2px;">
                        This trade uses <b>{risk_pct:.1f}%</b> of your total capital. Professional risk management permits max 15–20% allocation per intraday option trade.
                    </div>
                </div>
                """
            )

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 3: LIVE OPEN POSITIONS TERMINAL
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 📊 Active Open Positions")

    if not acc.open_positions:
        st.info("No open paper positions right now. You are 100% in cash. Follow the rulebook and wait for high-probability setups!")
    else:
        # Panic Button: Square Off All
        c_p1, c_p2 = st.columns([3, 1])
        with c_p2:
            if st.button("🚨 Square Off All Positions", type="secondary", key="sq_off_all", use_container_width=True):
                cnt, acc = pt.exit_all_positions(spot, chain_df, exit_reason="Panic Square Off")
                st.warning(f"Squared off {cnt} open position(s).")
                st.rerun()

        for idx, pos in enumerate(acc.open_positions):
            pnl_c = GREEN if pos.unrealized_pnl >= 0 else RED
            pnl_s = "+" if pos.unrealized_pnl >= 0 else ""

            # Estimated Net P&L after round-trip charges
            pos_charges = pt.calculate_trade_charges(pos.entry_price, pos.current_price, pos.quantity, side=pos.side)
            est_net_pnl = round(pos.unrealized_pnl - pos_charges["total_charges"], 2)
            net_c = GREEN if est_net_pnl >= 0 else RED
            net_s = "+" if est_net_pnl >= 0 else ""

            is_seller = pos.side.upper() == "SELL"
            ctc_locked = (abs(pos.stop_loss - pos.entry_price) <= 0.5) or (pos.stop_loss > pos.entry_price if not is_seller else pos.stop_loss < pos.entry_price)
            trailing_active = pos.trailing_sl_pts > 0.0

            # Side and Product Badges
            side_badge = f"<span style='background: {GREEN if not is_seller else RED}; color: #FFFFFF; font-size: 11px; font-weight: 800; padding: 3px 8px; border-radius: 4px;'>{pos.side}</span>"
            prod_badge = f"<span style='background: {'#1B3A5B' if pos.product_type == 'INTRADAY' else '#6B21A8'}; color: {'#00D2D3' if pos.product_type == 'INTRADAY' else '#F3E8FF'}; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; margin-left: 6px;'>{'⚡ MIS' if pos.product_type == 'INTRADAY' else '🌙 NRML (Carry Forward)'}</span>"

            badge_html = f"{side_badge} {prod_badge}"
            if ctc_locked:
                badge_html += f"<span style='background: {GREEN}; color: #FFFFFF; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-left: 6px;'>🔒 CTC LOCKED</span>"
            if trailing_active:
                badge_html += f"<span style='background: {PURPLE}; color: #FFFFFF; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-left: 6px;'>📈 TRAILING ({pos.trailing_sl_pts:.0f} pts)</span>"
            if pos.target_1 > 0.0:
                badge_html += f"<span style='background: {BLUE}; color: #FFFFFF; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-left: 6px;'>🎯 AUTO T1 @ ₹{pos.target_1:.1f}</span>"

            watermark_label = "Watermark High" if not is_seller else "Watermark Low"
            watermark_val = pos.highest_price if not is_seller else pos.lowest_price

            _render_html(
                f"""
                <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div>
                            <span style="font-size: 18px; font-weight: 800; color: #FFFFFF; font-family: monospace;">
                                {pos.symbol}
                            </span>
                            <span style="background: #1B3A5B; color: #CCD6F6; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; margin-left: 8px;">
                                {pos.lots} Lot(s) · {pos.quantity} Qty
                            </span>
                            {badge_html}
                            <span style="color: #8FA6B8; font-size: 12px; margin-left: 10px;">
                                Entry: {pos.entry_time} · {pos.setup_tag}
                            </span>
                        </div>
                        <div style="text-align: right;">
                            <span style="font-size: 20px; font-weight: 800; color: {pnl_c}; font-family: monospace;">
                                {pnl_s}₹{pos.unrealized_pnl:,.2f} ({pnl_s}{pos.unrealized_pct:.1f}%)
                            </span>
                            <div style="font-size: 11px; color: #8FA6B8;">
                                Net Post-Charges: <span style="color: {net_c}; font-weight: 700;">{net_s}₹{est_net_pnl:,.2f}</span>
                            </div>
                        </div>
                    </div>
                    <div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; font-size: 13px; color: #CCD6F6;">
                        <div><b>Avg Entry:</b> ₹{pos.entry_price:.2f}</div>
                        <div><b>Current LTP:</b> <span style="color: #FFD166; font-weight: 700;">₹{pos.current_price:.2f}</span></div>
                        <div><b>{watermark_label}:</b> ₹{watermark_val:.2f}</div>
                        <div><b>Stop Loss:</b> <span style="color: {RED}; font-weight: 700;">₹{pos.stop_loss:.1f}</span></div>
                        <div><b>Target 1 / 2:</b> <span style="color: {GREEN}; font-weight: 700;">₹{pos.target_1:.1f} / ₹{pos.target_2:.1f}</span></div>
                    </div>
                </div>
                """
            )

            # Interactive Actions Bar
            a_col1, a_col2, a_col3, a_col4, a_col5, a_col6 = st.columns([1.1, 0.9, 0.9, 1.2, 1.1, 1.1])

            with a_col1:
                if not ctc_locked:
                    if st.button(f"🔒 Lock CTC (₹{pos.entry_price:.1f})", key=f"ctc_{pos.position_id}", use_container_width=True):
                        ok, msg, acc = pt.update_paper_position_sl(pos.position_id, pos.entry_price)
                        if ok:
                            st.success(msg)
                            st.rerun()
                else:
                    st.caption("🔒 CTC Protected")

            with a_col2:
                trail_label = "📈 Trail +5" if not is_seller else "📉 Trail -5"
                if st.button(trail_label, key=f"trail5_{pos.position_id}", use_container_width=True):
                    ok, msg, acc = pt.trail_paper_position_sl(pos.position_id, 5.0)
                    if ok:
                        st.success(msg)
                        st.rerun()

            with a_col3:
                trail10_label = "📈 Trail +10" if not is_seller else "📉 Trail -10"
                if st.button(trail10_label, key=f"trail10_{pos.position_id}", use_container_width=True):
                    ok, msg, acc = pt.trail_paper_position_sl(pos.position_id, 10.0)
                    if ok:
                        st.success(msg)
                        st.rerun()

            with a_col4:
                # 1-Click Product Conversion: MIS ↔ NRML
                if pos.product_type == "INTRADAY":
                    if st.button("🌙 Convert to NRML", key=f"conv_{pos.position_id}", use_container_width=True, help="Carry forward overnight as BTST/STBT without 3:20 PM auto square-off"):
                        ok, msg, acc = pt.convert_paper_position_product(pos.position_id, "NORMAL")
                        if ok:
                            st.success(msg)
                            st.rerun()
                else:
                    if st.button("⚡ Convert to MIS", key=f"conv_{pos.position_id}", use_container_width=True, help="Convert to Intraday with 3:20 PM auto square-off"):
                        ok, msg, acc = pt.convert_paper_position_product(pos.position_id, "INTRADAY")
                        if ok:
                            st.info(msg)
                            st.rerun()

            with a_col5:
                # Scale Out Partial Lots
                if pos.lots > 1:
                    with st.popover(f"✂️ Scale Out ({pos.lots}L)", use_container_width=True):
                        st.write("##### Scale Out Partial Lots")
                        lots_to_close = st.number_input("Lots to close", min_value=1, max_value=pos.lots - 1, value=1, key=f"scale_lots_{pos.position_id}")
                        if st.button(f"Confirm Close {lots_to_close} Lot(s)", key=f"btn_scale_{pos.position_id}", type="primary"):
                            ok, msg, acc = pt.partial_exit_paper_position(
                                position_id=pos.position_id,
                                exit_lots=lots_to_close,
                                spot=spot,
                                chain_df=chain_df,
                                exit_reason="Manual Scale Out",
                                emotion_tag="Disciplined (Scale Out)",
                            )
                            if ok:
                                st.success(msg)
                                st.rerun()
                else:
                    st.caption("1 Lot (N/A)")

            with a_col6:
                # Full Exit with Emotion Review Popover
                with st.popover("🔴 Square Off", use_container_width=True):
                    st.write(f"##### Close {pos.symbol} ({pos.side})")
                    exit_emo = st.selectbox(
                        "Trade Mindset / Emotion",
                        options=[
                            "Disciplined (Target / Setup Achieved)",
                            "Disciplined (SL Executed)",
                            "FOMO / Panic Exit",
                            "Greed / Overheld Past Target",
                            "Revenge / Impatience",
                        ],
                        key=f"exit_emo_{pos.position_id}",
                    )
                    exit_review_note = st.text_input("Review Note", placeholder="What did you learn?", key=f"exit_note_{pos.position_id}")
                    if st.button("Confirm Square Off", key=f"btn_exit_conf_{pos.position_id}", type="primary"):
                        ok, msg, acc = pt.exit_paper_position(
                            position_id=pos.position_id,
                            spot=spot,
                            chain_df=chain_df,
                            exit_reason="Manual Square Off",
                            emotion_tag=exit_emo,
                            notes=exit_review_note,
                        )
                        if ok:
                            st.success(msg)
                            st.rerun()

            # Inline SL / Target Editor Expander
            with st.expander(f"✏️ Modify SL / Targets for {pos.symbol}", expanded=False):
                ed_c1, ed_c2, ed_c3, ed_c4 = st.columns(4)
                with ed_c1:
                    new_sl_val = st.number_input("Edit SL (₹)", min_value=0.0, value=float(pos.stop_loss), step=0.5, key=f"ed_sl_{pos.position_id}")
                with ed_c2:
                    new_t1_val = st.number_input("Edit Target 1 (₹)", min_value=0.0, value=float(pos.target_1), step=0.5, key=f"ed_t1_{pos.position_id}")
                with ed_c3:
                    new_t2_val = st.number_input("Edit Target 2 (₹)", min_value=0.0, value=float(pos.target_2), step=0.5, key=f"ed_t2_{pos.position_id}")
                with ed_c4:
                    new_trail_val = st.number_input("Edit Trailing (pts)", min_value=0.0, value=float(pos.trailing_sl_pts), step=1.0, key=f"ed_tr_{pos.position_id}")

                if st.button("Update Position Levels", key=f"btn_upd_{pos.position_id}"):
                    ok, msg, acc = pt.update_paper_position_sl_target(
                        position_id=pos.position_id,
                        new_sl=new_sl_val,
                        new_t1=new_t1_val,
                        new_t2=new_t2_val,
                        trailing_sl_pts=new_trail_val,
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 4: TRADER SCORECARD & ADVANCED ANALYTICS
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 🏆 Performance Statistics & Trader Scorecard")

    stats = pt.get_paper_performance_stats(acc)

    s1, s2, s3, s4, s5, s6 = st.columns(6)
    s1.metric("Win Rate", f"{stats['win_rate_pct']:.1f}%", f"{stats['wins_count']}W / {stats['losses_count']}L")
    s2.metric("Profit Factor", f"{stats['profit_factor']:.2f}", "Target >= 1.50")
    s3.metric("Total Net P&L", f"₹{stats['total_realized_pnl']:,.2f}", f"Gross: ₹{stats['total_gross_pnl']:,.2f}")
    s4.metric("Avg Win / Loss", f"₹{stats['avg_win_rupees']:,.0f} / -₹{abs(stats['avg_loss_rupees']):,.0f}")
    s5.metric("Max Drawdown", f"-₹{stats['max_drawdown_rupees']:,.2f}", f"{stats['max_drawdown_pct']:.1f}% decline")
    s6.metric("Current Streak", f"{stats['current_streak']}", f"Best: {stats['best_win_streak']}W / Worst: {stats['worst_loss_streak']}L")

    trade_df = pt.load_trade_history()
    if not trade_df.empty:
        # Dual Trace Interactive Equity Curve: Gross vs Net Post-Charges
        st.markdown("##### 📈 Virtual Account Equity Growth Curve (Gross vs Net Post-Charges)")
        trade_df["cum_gross"] = trade_df["pnl_rupees"].astype(float).cumsum()
        trade_df["cum_net"] = trade_df["net_pnl_rupees"].astype(float).cumsum() if "net_pnl_rupees" in trade_df.columns else trade_df["cum_gross"]
        trade_df["equity_gross"] = acc.initial_capital + trade_df["cum_gross"]
        trade_df["equity_net"] = acc.initial_capital + trade_df["cum_net"]

        fig_eq = go.Figure()
        fig_eq.add_trace(
            go.Scatter(
                x=list(range(len(trade_df))),
                y=trade_df["equity_net"],
                mode="lines+markers",
                name="Net Equity (Post-Charges)",
                line=dict(color=GREEN if stats["total_realized_pnl"] >= 0 else RED, width=3),
                marker=dict(size=6),
            )
        )
        fig_eq.add_trace(
            go.Scatter(
                x=list(range(len(trade_df))),
                y=trade_df["equity_gross"],
                mode="lines",
                name="Gross Equity (Pre-Charges)",
                line=dict(color=SLATE, dash="dot", width=1.5),
            )
        )
        fig_eq.add_hline(
            y=acc.initial_capital,
            line=dict(color=AMBER, dash="dash", width=1.5),
            annotation_text=f"Initial Capital (₹{acc.initial_capital:,.0f})",
            annotation_position="bottom right",
        )
        fig_eq.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0A192F",
            plot_bgcolor="#0E2238",
            margin=dict(l=10, r=20, t=20, b=20),
            height=320,
            xaxis_title="Completed Trade Number",
            yaxis_title="Account Equity (₹)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig_eq, use_container_width=True)

        # 4 Detailed Deep-Dive Analytics Tabs
        tab_sess, tab_side, tab_opt, tab_disc = st.tabs([
            "⏰ Time-of-Day Performance",
            "⚖️ BUY vs SELL & MIS vs NRML",
            "🎯 CE vs PE Performance",
            "🧠 Discipline & Emotion Matrix",
        ])

        with tab_sess:
            st.markdown("##### ⏰ Win Rate & P&L by Market Session")
            if stats.get("session_breakdown"):
                sess_rows = []
                for s_name, d in stats["session_breakdown"].items():
                    sess_rows.append({
                        "Market Session": s_name,
                        "Trades": d["trades"],
                        "Win Rate %": f"{d['win_rate']:.1f}%",
                        "Net P&L (₹)": f"₹{d['net_pnl']:,.2f}",
                    })
                st.dataframe(pd.DataFrame(sess_rows), use_container_width=True)
            else:
                st.caption("No session breakdown data yet.")

        with tab_side:
            st.markdown("##### ⚖️ Order Side & Product Performance")
            c_s1, c_s2 = st.columns(2)
            buy_d = stats.get("buy_stats", {})
            sell_d = stats.get("sell_stats", {})
            mis_d = stats.get("mis_stats", {})
            nrml_d = stats.get("nrml_stats", {})

            with c_s1:
                _render_html(
                    f"""
                    <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 12px; margin-bottom: 8px;">
                        <h4 style="color: {GREEN}; margin: 0;">🟢 BUY (Option Buying)</h4>
                        <div style="font-size: 20px; font-weight: 800; color: #FFFFFF; margin-top: 4px;">
                            {buy_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 12px; color: #8FA6B8;">
                            {buy_d.get('trades', 0)} Trades · Net P&L: ₹{buy_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 12px;">
                        <h4 style="color: #00D2D3; margin: 0;">⚡ MIS (Intraday)</h4>
                        <div style="font-size: 20px; font-weight: 800; color: #FFFFFF; margin-top: 4px;">
                            {mis_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 12px; color: #8FA6B8;">
                            {mis_d.get('trades', 0)} Trades · Net P&L: ₹{mis_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    """
                )

            with c_s2:
                _render_html(
                    f"""
                    <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 12px; margin-bottom: 8px;">
                        <h4 style="color: {RED}; margin: 0;">🔴 SELL (Option Writing / Shorting)</h4>
                        <div style="font-size: 20px; font-weight: 800; color: #FFFFFF; margin-top: 4px;">
                            {sell_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 12px; color: #8FA6B8;">
                            {sell_d.get('trades', 0)} Trades · Net P&L: ₹{sell_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 12px;">
                        <h4 style="color: #A855F7; margin: 0;">🌙 NRML (Carry Forward / BTST)</h4>
                        <div style="font-size: 20px; font-weight: 800; color: #FFFFFF; margin-top: 4px;">
                            {nrml_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 12px; color: #8FA6B8;">
                            {nrml_d.get('trades', 0)} Trades · Net P&L: ₹{nrml_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    """
                )

        with tab_opt:
            st.markdown("##### 🎯 Call (CE) vs Put (PE) Performance")
            c1_opt, c2_opt = st.columns(2)
            ce_d = stats.get("ce_stats", {})
            pe_d = stats.get("pe_stats", {})

            with c1_opt:
                _render_html(
                    f"""
                    <div style="background: #0E2238; border: 1px solid {GREEN}; border-radius: 8px; padding: 14px; text-align: center;">
                        <h4 style="color: {GREEN}; margin: 0;">🟢 CE (Call Contracts)</h4>
                        <div style="font-size: 22px; font-weight: 800; color: #FFFFFF; margin-top: 6px;">
                            {ce_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 13px; color: #8FA6B8; margin-top: 4px;">
                            {ce_d.get('trades', 0)} Trades · Net P&L: ₹{ce_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    """
                )

            with c2_opt:
                _render_html(
                    f"""
                    <div style="background: #0E2238; border: 1px solid {RED}; border-radius: 8px; padding: 14px; text-align: center;">
                        <h4 style="color: {RED}; margin: 0;">🔴 PE (Put Contracts)</h4>
                        <div style="font-size: 22px; font-weight: 800; color: #FFFFFF; margin-top: 6px;">
                            {pe_d.get('win_rate', 0.0):.1f}% WR
                        </div>
                        <div style="font-size: 13px; color: #8FA6B8; margin-top: 4px;">
                            {pe_d.get('trades', 0)} Trades · Net P&L: ₹{pe_d.get('net_pnl', 0.0):,.2f}
                        </div>
                    </div>
                    """
                )

        with tab_disc:
            st.markdown("##### 🧠 Discipline & Psychology Scorecard")
            c_tg, c_em = st.columns(2)

            with c_tg:
                st.markdown("###### Setup Rules vs Impulse Trades")
                if stats.get("tag_breakdown"):
                    breakdown_rows = []
                    for tag, d in stats["tag_breakdown"].items():
                        breakdown_rows.append({
                            "Setup Tag": tag,
                            "Trades": d["trades"],
                            "Win Rate %": f"{d['win_rate']:.1f}%",
                            "Net P&L (₹)": f"₹{d['net_pnl']:,.2f}",
                        })
                    st.dataframe(pd.DataFrame(breakdown_rows), use_container_width=True)
                else:
                    st.caption("No setup tag data yet.")

            with c_em:
                st.markdown("###### Psychological Mindset Breakdown")
                if stats.get("emotion_breakdown"):
                    emo_rows = []
                    for emo, d in stats["emotion_breakdown"].items():
                        emo_rows.append({
                            "Emotion / Mindset": emo,
                            "Trades": d["trades"],
                            "Win Rate %": f"{d['win_rate']:.1f}%",
                            "Net P&L (₹)": f"₹{d['net_pnl']:,.2f}",
                        })
                    st.dataframe(pd.DataFrame(emo_rows), use_container_width=True)
                else:
                    st.caption("No emotion tag records yet.")

    # ── SECTION 5: PERSISTENT TRADE JOURNAL (CSV LOG) ────────────────────────
    with st.expander("📓 Detailed Paper Trading Journal Log (CSV)", expanded=False):
        if trade_df.empty:
            st.caption("No closed trades recorded yet.")
        else:
            st.dataframe(trade_df.sort_values(by="exit_time", ascending=False), use_container_width=True)
            csv_bytes = trade_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Download Paper Trading Journal (CSV)",
                data=csv_bytes,
                file_name="paper_trading_journal.csv",
                mime="text/csv",
                key="dl_paper_csv",
            )
