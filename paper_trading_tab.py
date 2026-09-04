"""
paper_trading_tab.py — Streamlit UI Tab for Paper Trading & Impulse Control.

Features:
  1. Real-time account equity, cash balance, and margin tracking starting with ₹1,00,000.
  2. One-click Capital Refill / Reset to any amount.
  3. Live Order Placement for Nifty CE/PE with Dhan option prices and lot size selection.
  4. The "Discipline Gate": Live 15m Win Rate score, midday theta warnings, and setup tagging.
  5. Live Open Positions terminal with real-time P&L and 1-click square-off.
  6. Performance Analytics: Win Rate %, Profit Factor, Equity Curve, and Strategy vs Impulse breakdown.
  7. Persistent Trade Journal logged to data/paper_trading/trades.csv.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import paper_trading as pt
import trade_advisor_15m as ta

GREEN = "#2FBF71"
RED = "#E8503A"
SLATE = "#8FA6B8"
GOLD = "#C9A227"
BLUE = "#4A9BD1"
AMBER = "#E0A030"
PURPLE = "#9B51E0"

NIFTY_LOT_SIZE = getattr(config, "NIFTY_LOT_SIZE", 75)


def render_paper_trading_tab(
    pa_frames: Optional[Dict[str, pd.DataFrame]],
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
) -> None:
    """Renders the Paper Trading and Impulse Control Tab in Streamlit."""
    cur_time = now or datetime.now()
    atm_strike = round(spot / 50.0) * 50.0 if spot > 0 else 24150.0

    # 1. Load & Sync Account
    acc = pt.load_paper_account()
    acc = pt.sync_open_positions(acc, spot=spot, chain_df=chain_df)
    pt.save_paper_account(acc)

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 1: HEADER & VIRTUAL CAPITAL BAR
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, #071626 0%, #0E2238 100%);
                    border: 1px solid #4A9BD1; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h2 style="margin: 0; color: #FFFFFF; font-size: 22px; font-weight: 800;">
                        📝 Paper Trading & Impulse Control Terminal
                    </h2>
                    <p style="margin: 4px 0 0 0; color: #8FA6B8; font-size: 13px;">
                        Risk-Free Live Market Execution · Real-Time Dhan Pricing · Discipline Gate & Impulse Prevention
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
        """,
        unsafe_allow_html=True,
    )

    # Capital Overview Metrics
    c_eq, c_cash, c_used, c_unreal, c_real = st.columns(5)

    tot_pnl = acc.realized_pnl + acc.unrealized_pnl
    pnl_col = GREEN if tot_pnl >= 0 else RED
    pnl_sign = "+" if tot_pnl >= 0 else ""

    c_eq.metric("Total Equity", f"₹{acc.total_equity:,.2f}", delta=f"{pnl_sign}₹{tot_pnl:,.2f}")
    c_cash.metric("Available Cash", f"₹{acc.cash_balance:,.2f}")
    c_used.metric("Margin In Use", f"₹{acc.used_margin:,.2f}")

    unr_col = GREEN if acc.unrealized_pnl >= 0 else RED
    unr_sign = "+" if acc.unrealized_pnl >= 0 else ""
    c_unreal.metric("Unrealized P&L", f"₹{acc.unrealized_pnl:,.2f}", delta=f"{unr_sign}₹{acc.unrealized_pnl:,.2f}")

    rel_sign = "+" if acc.realized_pnl >= 0 else ""
    c_real.metric("Realized P&L", f"₹{acc.realized_pnl:,.2f}", delta=f"{rel_sign}₹{acc.realized_pnl:,.2f}")

    # Refill / Reset Capital Expander
    with st.expander("💳 Manage Paper Capital (Refill / Reset Balance)", expanded=False):
        rf_col1, rf_col2, rf_col3 = st.columns([1.2, 1.2, 1.2])

        with rf_col1:
            if st.button("➕ Add ₹50,000 to Cash Balance", key="refill_50k"):
                acc = pt.refill_paper_capital(50000.0, reset_all=False)
                st.success("Successfully added ₹50,000 to paper cash balance!")
                st.rerun()

        with rf_col2:
            if st.button("➕ Add ₹1,00,000 to Cash Balance", key="refill_100k"):
                acc = pt.refill_paper_capital(100000.0, reset_all=False)
                st.success("Successfully added ₹1,00,000 to paper cash balance!")
                st.rerun()

        with rf_col3:
            if st.button("🔄 Reset Account to ₹1,00,000 Clean", key="reset_account_clean"):
                acc = pt.refill_paper_capital(100000.0, reset_all=True)
                st.warning("Account reset to ₹1,00,000. Open positions cleared.")
                st.rerun()

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 2: ORDER PLACEMENT & DISCIPLINE GATE
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 🛒 Place Paper Trade Order")

    col_order, col_discipline = st.columns([1.3, 1.1])

    # Generate strikes around ATM (+/- 8 strikes)
    strike_options = [atm_strike + (i * 50.0) for i in range(-8, 9)]
    default_s_idx = strike_options.index(atm_strike) if atm_strike in strike_options else 8

    with col_order:
        st.markdown("##### 1. Select Instrument & Lots")
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
            order_type = st.radio(
                "Option Type",
                options=["CE (Call)", "PE (Put)"],
                index=0,
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
        req_margin = round(live_ltp * total_qty, 2)

        p_c1, p_c2, p_c3 = st.columns(3)
        p_c1.metric("Live Option LTP", f"₹{live_ltp:.2f}", help="Current price fetched from Dhan Option Chain")
        p_c2.metric("Total Quantity", f"{total_qty} units")
        p_c3.metric("Required Margin", f"₹{req_margin:,.2f}", delta=f"Available: ₹{acc.cash_balance:,.0f}")

        # Stop Loss & Target Settings
        st.markdown("##### 2. Stop Loss & Targets")
        s_c1, s_c2 = st.columns(2)

        # Smart pre-calculated defaults based on 15m risk (15 pts * delta)
        default_sl = max(3.0, round(live_ltp - 8.0, 1))
        default_t1 = round(live_ltp + 12.0, 1)

        with s_c1:
            order_sl = st.number_input(
                "Option Stop Loss (₹)",
                min_value=0.0,
                max_value=3000.0,
                value=default_sl,
                step=0.5,
                key="pt_order_sl",
            )
        with s_c2:
            order_t1 = st.number_input(
                "Option Target (₹)",
                min_value=0.0,
                max_value=3000.0,
                value=default_t1,
                step=0.5,
                key="pt_order_t1",
            )

        # Strategy Tag (Mandatory for Discipline Tracking)
        setup_tag_choices = [
            "🔴 Setup 5: Asian Liquidity Sweep (Dr. Devendra)",
            "🔴 Setup 1: Opening POB Retest",
            "⚡ 15m 8-EMA Pullback Reversal",
            "⚔️ WAR Setup (30m FII First Candle)",
            "🌊 1m 3-Drive Institutional Scalp",
            "📦 SMC Order Block / Fair Value Gap",
            "⚠️ Impulse / Gut Feel (No Rule)",
        ]
        chosen_tag = st.selectbox(
            "🏷️ Why are you taking this trade? (Impulse Gate)",
            options=setup_tag_choices,
            index=0,
            key="pt_order_tag",
        )

        # Place Order Button
        can_afford = req_margin <= acc.cash_balance
        btn_label = f"🚀 BUY {lots} LOT(S) {int(order_strike)} {clean_type} @ ₹{live_ltp:.1f}"

        if st.button(btn_label, type="primary", disabled=not can_afford, use_container_width=True):
            ok, msg, acc = pt.place_paper_order(
                strike=order_strike,
                opt_type=clean_type,
                lots=lots,
                spot=spot,
                chain_df=chain_df,
                stop_loss=order_sl,
                target_1=order_t1,
                setup_tag=chosen_tag,
            )
            if ok:
                st.success(f"✅ {msg}")
                st.rerun()
            else:
                st.error(f"❌ {msg}")

        if not can_afford:
            st.error(f"⚠️ Insufficient capital! Required ₹{req_margin:,.2f} but available cash is ₹{acc.cash_balance:,.2f}. Please refill paper capital.")

    # ── THE DISCIPLINE GATE (Impulse Control Checklist) ───────────────────────
    with col_discipline:
        st.markdown("##### 🛡️ The Impulse Control Gate")

        # 1. 15m Live Win Rate Preview
        df_15m = pa_frames.get("15m") if pa_frames else None
        advice_res = ta.evaluate_15m_trade_advisor(
            df_15m=df_15m,
            spot=spot,
            chain_df=chain_df,
            selected_strike=order_strike,
            selected_type=clean_type,
            now=cur_time,
        )

        wr_color = GREEN if advice_res.win_rate >= 62.0 else (RED if advice_res.win_rate < 50.0 else AMBER)

        st.markdown(
            f"""
            <div style="background: #0E2238; border: 1px solid {wr_color}; border-radius: 8px; padding: 12px 14px; margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 13px; font-weight: 700; color: #FFFFFF;">15m Structural Win Rate:</span>
                    <span style="font-size: 16px; font-weight: 800; color: {wr_color}; font-family: monospace;">
                        {advice_res.win_rate:.1f}%
                    </span>
                </div>
                <div style="font-size: 12px; color: #CCD6F6; margin-top: 4px;">
                    {advice_res.headline} — {advice_res.sub_headline}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 2. Midday Freeze Check
        cur_t = cur_time.time()
        is_midday = (cur_t >= datetime.strptime("11:30", "%H:%M").time()) and (cur_t <= datetime.strptime("13:15", "%H:%M").time())
        if is_midday:
            st.markdown(
                f"""
                <div style="background: rgba(232, 80, 58, 0.15); border: 1px solid {RED}; border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                    <b style="color: {RED}; font-size: 12px;">⚠️ MIDDAY FREEZE ACTIVE (11:30 – 13:15)</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 2px;">
                        Theta decay is at maximum speed while volume dries up. 80% of retail losses occur right now. Wait for 13:15 European flow.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # 3. Contratrend Warning
        if advice_res.confluences_negative:
            st.markdown(
                f"""
                <div style="background: rgba(224, 160, 48, 0.15); border: 1px solid {AMBER}; border-radius: 6px; padding: 10px; margin-bottom: 10px;">
                    <b style="color: {AMBER}; font-size: 12px;">⚠️ RISK FACTORS DETECTED:</b>
                """,
                unsafe_allow_html=True,
            )
            for neg in advice_res.confluences_negative:
                st.markdown(f"<div style='font-size: 11px; color: #FFFFFF;'>{neg}</div>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)

        # 4. Capital Risk Guard
        risk_pct = (req_margin / max(1.0, acc.total_equity)) * 100.0
        if risk_pct > 25.0:
            st.markdown(
                f"""
                <div style="background: rgba(232, 80, 58, 0.15); border: 1px solid {RED}; border-radius: 6px; padding: 10px;">
                    <b style="color: {RED}; font-size: 12px;">🚨 OVERSIZED POSITION WARNING</b>
                    <div style="font-size: 11px; color: #FFFFFF; margin-top: 2px;">
                        This trade uses <b>{risk_pct:.1f}%</b> of your total capital. Professional risk management allows max 15–20% capital allocation per intraday option trade.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
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

        for pos in acc.open_positions:
            pnl_c = GREEN if pos.unrealized_pnl >= 0 else RED
            pnl_s = "+" if pos.unrealized_pnl >= 0 else ""

            st.markdown(
                f"""
                <div style="background: #0E2238; border: 1px solid #20436B; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div>
                            <span style="font-size: 18px; font-weight: 800; color: #FFFFFF; font-family: monospace;">
                                {pos.symbol}
                            </span>
                            <span style="background: #1B3A5B; color: #00D2D3; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 4px; margin-left: 8px;">
                                {pos.lots} Lot(s) · {pos.quantity} Qty
                            </span>
                            <span style="color: #8FA6B8; font-size: 12px; margin-left: 10px;">
                                Entry: {pos.entry_time} · Tag: {pos.setup_tag}
                            </span>
                        </div>
                        <div style="text-align: right;">
                            <span style="font-size: 20px; font-weight: 800; color: {pnl_c}; font-family: monospace;">
                                {pnl_s}₹{pos.unrealized_pnl:,.2f} ({pnl_s}{pos.unrealized_pct:.1f}%)
                            </span>
                        </div>
                    </div>
                    <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; font-size: 13px; color: #CCD6F6;">
                        <div><b>Avg Buy Price:</b> ₹{pos.entry_price:.2f}</div>
                        <div><b>Current LTP:</b> <span style="color: #FFD166; font-weight: 700;">₹{pos.current_price:.2f}</span></div>
                        <div><b>Stop Loss:</b> <span style="color: {RED};">₹{pos.stop_loss:.1f}</span></div>
                        <div><b>Target:</b> <span style="color: {GREEN};">₹{pos.target_1:.1f}</span></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Individual Exit Button
            btn_col1, btn_col2 = st.columns([4, 1])
            with btn_col2:
                if st.button(f"🔴 Exit / Sell {pos.symbol}", key=f"exit_{pos.position_id}", use_container_width=True):
                    ok, msg, acc = pt.exit_paper_position(pos.position_id, spot, chain_df, exit_reason="Manual Square Off")
                    if ok:
                        st.success(msg)
                        st.rerun()

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION 4: TRADER PERFORMANCE ANALYTICS & STATS
    # ═══════════════════════════════════════════════════════════════════════
    st.markdown("### 🏆 Performance Statistics & Trader Scorecard")

    stats = pt.get_paper_performance_stats(acc)

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Win Rate", f"{stats['win_rate_pct']:.1f}%", f"{stats['wins_count']}W / {stats['losses_count']}L")
    s2.metric("Profit Factor", f"{stats['profit_factor']:.2f}", "Target >= 1.50")
    s3.metric("Total Net P&L", f"₹{stats['total_realized_pnl']:,.2f}")
    s4.metric("Average Win", f"₹{stats['avg_win_rupees']:,.2f}")
    s5.metric("Average Loss", f"-₹{abs(stats['avg_loss_rupees']):,.2f}")

    # Interactive Equity Curve
    trade_df = pt.load_trade_history()
    if not trade_df.empty:
        st.markdown("##### 📈 Virtual Account Equity Growth Curve")
        trade_df["cum_pnl"] = trade_df["pnl_rupees"].cumsum()
        trade_df["equity"] = acc.initial_capital + trade_df["cum_pnl"]

        fig_eq = go.Figure()
        fig_eq.add_trace(
            go.Scatter(
                x=list(range(len(trade_df))),
                y=trade_df["equity"],
                mode="lines+markers",
                name="Account Equity (₹)",
                line=dict(color=GREEN if stats["total_realized_pnl"] >= 0 else RED, width=2.5),
                marker=dict(size=6),
            )
        )
        fig_eq.add_hline(
            y=acc.initial_capital,
            line=dict(color=SLATE, dash="dash", width=1.5),
            annotation_text=f"Initial Capital (₹{acc.initial_capital:,.0f})",
            annotation_position="bottom right",
        )
        fig_eq.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0A192F",
            plot_bgcolor="#0E2238",
            margin=dict(l=10, r=20, t=20, b=20),
            height=300,
            xaxis_title="Trade Number",
            yaxis_title="Account Equity (₹)",
        )
        st.plotly_chart(fig_eq, use_container_width=True)

        # Strategy vs Impulse Breakdown Table
        if stats.get("tag_breakdown"):
            st.markdown("##### 🔍 Performance Breakdown: Strategy Rules vs Impulse Trades")
            breakdown_rows = []
            for tag, d in stats["tag_breakdown"].items():
                breakdown_rows.append({
                    "Strategy / Setup Tag": tag,
                    "Trades": d["trades"],
                    "Win Rate %": f"{d['win_rate']:.1f}%",
                    "Net P&L (₹)": f"₹{d['net_pnl']:,.2f}",
                })
            st.dataframe(pd.DataFrame(breakdown_rows), use_container_width=True)

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
