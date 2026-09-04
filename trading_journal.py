"""
trading_journal.py — Trading Journal, Dhan Brokerage Calculator & Anti-Impulse Behavioral Coach.

Provides:
  1. Universal Dhan trade-book parser (handles copy-paste from Dhan web, CSV, Excel).
  2. FIFO trade pairing (Buy & Sell matches).
  3. Official Dhan Brokerage & Regulatory Charges calculator (STT, GST, Exchange, SEBI, Stamp Duty).
  4. Behavioral Impulsive Trading Diagnostic:
     - Revenge Trading Detector (re-entering within 3 minutes of a loss)
     - Overtrading Meter (flagging sessions with > 4 trades)
     - Panic Scalp Detector (exits < 90s)
     - Midday Freeze Violation Detector (trading between 11:30 and 13:15)
     - Brokerage Drain metric (% of edge lost to commissions)
  5. Persistent journal storage in data/journal/trade_log.csv.
"""
from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("journal")

JOURNAL_DIR = config.DATA_DIR / "journal"
JOURNAL_FILE = JOURNAL_DIR / "trade_log.csv"
DAILY_PNL_FILE = JOURNAL_DIR / "daily_pnl.csv"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Dhan Brokerage & Statutory Charges Calculator
# ═══════════════════════════════════════════════════════════════════════════

def calculate_dhan_charges(buy_price: float, sell_price: float, qty: int) -> dict:
    """
    Computes exact Dhan charges for Equity Option round-trip trade:
      - Brokerage: ₹20 per executed order (Buy ₹20 + Sell ₹20 = ₹40)
      - STT: 0.125% on Sell Turnover
      - Exchange Transaction Charges (NSE): 0.05% on total turnover
      - SEBI Turnover Charges: ₹10 per crore (0.0001%)
      - Stamp Duty: 0.003% on Buy Turnover
      - GST: 18% on (Brokerage + Exchange + SEBI)
    """
    buy_turnover = float(buy_price) * int(qty)
    sell_turnover = float(sell_price) * int(qty)
    total_turnover = buy_turnover + sell_turnover

    brokerage = 40.0  # ₹20 per leg
    stt = round(sell_turnover * 0.00125, 2)
    exchange = round(total_turnover * 0.0005, 2)
    sebi = round(total_turnover * 0.000001, 2)
    stamp_duty = round(buy_turnover * 0.00003, 2)
    gst = round((brokerage + exchange + sebi) * 0.18, 2)

    total_charges = round(brokerage + stt + exchange + sebi + stamp_duty + gst, 2)

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange": exchange,
        "sebi": sebi,
        "stamp_duty": stamp_duty,
        "gst": gst,
        "total_charges": total_charges,
        "buy_turnover": buy_turnover,
        "sell_turnover": sell_turnover,
        "total_turnover": total_turnover,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 2. Universal Dhan Order / Trade Book Parser
# ═══════════════════════════════════════════════════════════════════════════

def parse_dhan_input(raw_input: str | pd.DataFrame) -> pd.DataFrame:
    """
    Parses pasted text or an uploaded DataFrame from Dhan.
    Extracts: time, symbol, side (BUY/SELL), qty, price.
    """
    if isinstance(raw_input, pd.DataFrame):
        df = raw_input.copy()
        cols = {c.strip().lower(): c for c in df.columns}

        time_c = cols.get("time") or cols.get("trade time") or cols.get("order time")
        sym_c = cols.get("trading symbol") or cols.get("symbol") or cols.get("scrip") or cols.get("instrument")
        side_c = cols.get("type") or cols.get("side") or cols.get("buy/sell") or cols.get("transaction type")
        qty_c = cols.get("qty") or cols.get("quantity") or cols.get("traded qty")
        price_c = cols.get("price") or cols.get("avg price") or cols.get("avg. price") or cols.get("execution price")

        if not all([side_c, qty_c, price_c]):
            return pd.DataFrame()

        records = []
        for _, r in df.iterrows():
            side_val = str(r[side_c]).strip().upper()
            if side_val not in ("BUY", "SELL", "B", "S"):
                continue
            side = "BUY" if side_val in ("BUY", "B") else "SELL"
            try:
                qty = int(float(str(r[qty_c]).replace(",", "")))
                price = float(str(r[price_c]).replace(",", "").replace("₹", ""))
            except (ValueError, TypeError):
                continue

            t_val = str(r[time_c]).strip() if time_c else datetime.now().strftime("%H:%M:%S")
            s_val = str(r[sym_c]).strip() if sym_c else "NIFTY OPTION"

            records.append({
                "time": t_val,
                "symbol": s_val,
                "side": side,
                "qty": qty,
                "price": price,
            })
        return pd.DataFrame(records)

    # Pasted string parsing
    text = str(raw_input).strip()
    if not text:
        return pd.DataFrame()

    records = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or "symbol" in line.lower() or "price" in line.lower() and "qty" in line.lower():
            continue

        # Split on tabs, commas, or 2+ spaces
        parts = [p.strip() for p in re.split(r"[\t,]|(?:\s{2,})", line) if p.strip()]
        if len(parts) < 4:
            # Fallback: single spaces
            parts = [p.strip() for p in line.split() if p.strip()]

        if len(parts) >= 4:
            side = None
            side_idx = -1
            for idx, p in enumerate(parts):
                u = p.upper()
                if u in ("BUY", "B"):
                    side = "BUY"; side_idx = idx; break
                elif u in ("SELL", "S"):
                    side = "SELL"; side_idx = idx; break

            if side is None:
                continue

            # Time is usually first or near start
            time_cand = parts[0]
            # Symbol is typically before side or first text token
            sym_parts = [parts[i] for i in range(len(parts)) if i != side_idx and not re.match(r"^[\d:.\-APM\s]+$", parts[i], re.I)]
            sym = " ".join(sym_parts[:3]) if sym_parts else "NIFTY OPTION"

            # Numbers for qty and price
            nums = []
            for p in parts:
                clean = p.replace(",", "").replace("₹", "").strip()
                if re.match(r"^\d+(\.\d+)?$", clean):
                    try:
                        val = float(clean)
                        # skip if it looks like a year e.g. 2026
                        if val != 2026 and val != 2025:
                            nums.append(val)
                    except ValueError:
                        pass

            if len(nums) >= 2:
                # Qty is usually integer >= 25, price has decimals or smaller
                qty = int(nums[0]) if nums[0] >= 25 or nums[0].is_integer() else int(nums[1])
                price = nums[1] if qty == int(nums[0]) else nums[0]

                records.append({
                    "time": time_cand,
                    "symbol": sym,
                    "side": side,
                    "qty": qty,
                    "price": price,
                })

    return pd.DataFrame(records)


# ═══════════════════════════════════════════════════════════════════════════
# 3. FIFO Trade Matcher & PnL Engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CompletedTrade:
    trade_id: str
    symbol: str
    entry_time: str
    exit_time: str
    hold_duration_mins: float
    buy_price: float
    sell_price: float
    qty: int
    gross_pnl: float
    charges: dict
    net_pnl: float
    trade_type: str        # "LONG" | "SHORT"
    impulse_flags: list[str] # ["REVENGE", "PANIC_SCALP", "MIDDAY_FREEZE", "DISCIPLINED"]


def match_trades_fifo(raw_df: pd.DataFrame, session_date: str = "") -> list[CompletedTrade]:
    """
    Pairs Buy and Sell executions using FIFO accounting on matching instruments.
    Attaches charges, P&L, hold duration, and emotional impulse tags.
    """
    if raw_df.empty:
        return []

    df = raw_df.copy()
    if not session_date:
        session_date = datetime.now().strftime("%Y-%m-%d")

    completed: list[CompletedTrade] = []
    last_exit_time = None
    last_trade_loss = False

    # Normalize symbols for matching
    df["norm_sym"] = df["symbol"].str.upper().str.replace(r"\s+", " ", regex=True)

    # Group by instrument
    for sym, group in df.groupby("norm_sym"):
        buys = group[group["side"] == "BUY"].to_dict("records")
        sells = group[group["side"] == "SELL"].to_dict("records")

        b_idx, s_idx = 0, 0
        while b_idx < len(buys) and s_idx < len(sells):
            b = buys[b_idx]
            s = sells[s_idx]

            matched_qty = min(b["qty"], s["qty"])
            if matched_qty <= 0:
                break

            # Timestamps
            t_buy = b["time"]
            t_sell = s["time"]

            # Standard Long option: Buy first, Sell later
            is_long = True
            entry_t, exit_t = t_buy, t_sell
            b_price, s_price = float(b["price"]), float(s["price"])

            gross_pnl = round((s_price - b_price) * matched_qty, 2)
            ch = calculate_dhan_charges(b_price, s_price, matched_qty)
            net_pnl = round(gross_pnl - ch["total_charges"], 2)

            # Hold duration
            duration = 5.0  # default
            try:
                fmt = "%H:%M:%S"
                t1 = datetime.strptime(entry_t.split()[-1], fmt)
                t2 = datetime.strptime(exit_t.split()[-1], fmt)
                duration = round(abs((t2 - t1).total_seconds()) / 60.0, 1)
            except Exception:
                pass

            # Behavioral / Impulse Diagnostics
            flags = []
            if duration <= 1.5:
                flags.append("⚡ Panic Scalp (< 90s)")

            # Check Midday Freeze (11:30 - 13:15)
            try:
                t_chk = datetime.strptime(entry_t.split()[-1], "%H:%M:%S").time()
                if config.FREEZE_START <= t_chk <= config.FREEZE_END:
                    flags.append("❄️ Midday Freeze Violation")
            except Exception:
                pass

            if not flags:
                flags.append("🟢 Disciplined")

            trade_obj = CompletedTrade(
                trade_id=f"{session_date}_{len(completed)+1}",
                symbol=sym,
                entry_time=entry_t,
                exit_time=exit_t,
                hold_duration_mins=duration,
                buy_price=b_price,
                sell_price=s_price,
                qty=matched_qty,
                gross_pnl=gross_pnl,
                charges=ch,
                net_pnl=net_pnl,
                trade_type="LONG",
                impulse_flags=flags,
            )
            completed.append(trade_obj)

            # Decrement quantities
            buys[b_idx]["qty"] -= matched_qty
            sells[s_idx]["qty"] -= matched_qty

            if buys[b_idx]["qty"] <= 0:
                b_idx += 1
            if sells[s_idx]["qty"] <= 0:
                s_idx += 1

    # Sort completed trades chronologically by entry_time
    def _parse_t(t_str):
        try:
            return datetime.strptime(str(t_str).split()[-1], "%H:%M:%S")
        except Exception:
            return datetime.min

    completed.sort(key=lambda t: _parse_t(t.entry_time))

    # Cross-trade chronological impulse audit
    for i in range(len(completed)):
        t = completed[i]
        if i > 0:
            prev = completed[i - 1]
            if prev.net_pnl < 0:
                t_prev_exit = _parse_t(prev.exit_time)
                t_curr_entry = _parse_t(t.entry_time)
                gap = (t_curr_entry - t_prev_exit).total_seconds()
                if 0 <= gap <= 180:
                    t.impulse_flags.append(f"🚨 Revenge Trade ({int(gap)}s after loss)")
                    if "🟢 Disciplined" in t.impulse_flags:
                        t.impulse_flags.remove("🟢 Disciplined")

    return completed


# ═══════════════════════════════════════════════════════════════════════════
# 4. Behavioral Diagnostic & Habit Coaching Engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class JournalAudit:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_pnl: float
    total_charges: float
    net_pnl: float
    brokerage_drag_pct: float
    overtrading_level: str       # "DISCIPLINED" | "MODERATE" | "SEVERE OVERTRADING"
    revenge_trades_count: int
    revenge_pnl: float
    panic_scalps_count: int
    midday_trades_count: int
    midday_pnl: float
    coach_feedback: list[str]


def audit_behavior(trades: list[CompletedTrade]) -> JournalAudit:
    """
    Analyzes emotional habits, leaks, and provides direct trader coaching.
    """
    if not trades:
        return JournalAudit(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "DISCIPLINED", 0, 0.0, 0, 0, 0.0, [])

    total = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / total) * 100, 1)

    gross = round(sum(t.gross_pnl for t in trades), 2)
    charges = round(sum(t.charges["total_charges"] for t in trades), 2)
    net = round(sum(t.net_pnl for t in trades), 2)

    # Brokerage drag: charges as % of total capital / edge
    drag = round((charges / abs(gross)) * 100, 1) if abs(gross) > 0 else 100.0

    # Overtrading scale
    if total <= 3:
        ot_level = "DISCIPLINED (1–3 Trades)"
    elif total <= 6:
        ot_level = "MODERATE (4–6 Trades)"
    else:
        ot_level = "SEVERE OVERTRADING (7+ Trades) ⚠️"

    # Revenge trades
    rev = [t for t in trades if any("Revenge" in f for f in t.impulse_flags)]
    rev_count = len(rev)
    rev_pnl = round(sum(t.net_pnl for t in rev), 2)

    # Panic scalps
    scalps = [t for t in trades if any("Panic" in f for f in t.impulse_flags)]
    scalp_count = len(scalps)

    # Midday freeze trades
    midday = [t for t in trades if any("Midday" in f for f in t.impulse_flags)]
    midday_count = len(midday)
    midday_pnl = round(sum(t.net_pnl for t in midday), 2)

    # Direct Behavioral Coaching Hints
    hints = []
    if rev_count > 0:
        hints.append(
            f"🚨 **Revenge Trading Leak**: You placed **{rev_count} trades** within 3 minutes of a loss, "
            f"costing you **₹{rev_pnl:,.2f}**. *Rule*: After taking a stop loss, step away from the screen for 10 minutes."
        )
    if total >= 6:
        hints.append(
            f"⚠️ **Overtrading Alert**: You placed **{total} trades** today. Brokerage and taxes swallowed "
            f"**₹{charges:,.2f}** ({drag:.0f}% of your gross performance). Focus on taking only the 1 or 2 best A+ setups."
        )
    if midday_count > 0 and midday_pnl < 0:
        hints.append(
            f"❄️ **Midday Freeze Leak**: You took **{midday_count} trades** during the 11:30–13:15 theta trap, "
            f"losing **₹{midday_pnl:,.2f}**. Staying completely out of the market during midday would immediately save your capital."
        )
    if scalp_count >= 2:
        hints.append(
            f"⚡ **Panic Fidgeting**: You had **{scalp_count} panic exits** held under 90 seconds. "
            f"Give your trade room to breathe by trusting your calculated stop loss rather than reacting to every 5-second tick."
        )

    if not hints:
        hints.append("🏆 **Excellent Discipline**: Clean execution, no revenge trades, and healthy trade spacing!")

    return JournalAudit(
        total_trades=total,
        winning_trades=win_count,
        losing_trades=loss_count,
        win_rate_pct=win_rate,
        gross_pnl=gross,
        total_charges=charges,
        net_pnl=net,
        brokerage_drag_pct=drag,
        overtrading_level=ot_level,
        revenge_trades_count=rev_count,
        revenge_pnl=rev_pnl,
        panic_scalps_count=scalp_count,
        midday_trades_count=midday_count,
        midday_pnl=midday_pnl,
        coach_feedback=hints,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Persistent Journal Store
# ═══════════════════════════════════════════════════════════════════════════

def save_to_journal(trades: list[CompletedTrade], session_date: str = "") -> int:
    """
    Appends completed trades to data/journal/trade_log.csv deduplicated by trade_id.
    """
    if not trades:
        return 0

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if not session_date:
        session_date = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for t in trades:
        rows.append({
            "trade_id": t.trade_id,
            "date": session_date,
            "symbol": t.symbol,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "hold_mins": t.hold_duration_mins,
            "buy_price": t.buy_price,
            "sell_price": t.sell_price,
            "qty": t.qty,
            "gross_pnl": t.gross_pnl,
            "brokerage": t.charges["brokerage"],
            "stt": t.charges["stt"],
            "exchange": t.charges["exchange"],
            "gst": t.charges["gst"],
            "total_charges": t.charges["total_charges"],
            "net_pnl": t.net_pnl,
            "behavior_tags": " · ".join(t.impulse_flags),
        })

    new_df = pd.DataFrame(rows)
    if JOURNAL_FILE.exists():
        try:
            old_df = pd.read_csv(JOURNAL_FILE)
            merged = pd.concat([old_df, new_df]).drop_duplicates(subset=["trade_id", "symbol", "entry_time"], keep="last")
        except Exception:
            merged = new_df
    else:
        merged = new_df

    merged.to_csv(JOURNAL_FILE, index=False)
    log.info(f"Journal updated: {len(merged)} total historical trades saved in {JOURNAL_FILE}")
    return len(rows)


def load_journal() -> pd.DataFrame:
    """Loads historical trade journal from disk."""
    if JOURNAL_FILE.exists():
        try:
            return pd.read_csv(JOURNAL_FILE)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════════════
# 6. Streamlit Tab Renderer
# ═══════════════════════════════════════════════════════════════════════════

def render_journal_tab():
    """
    Renders the complete Trading Journal and Anti-Impulse Coach in Streamlit.
    """
    import streamlit as st

    st.subheader("📓 Trading Journal & Anti-Impulse Coach")
    st.caption(
        "Paste your raw Dhan orders or upload your trade book. Computes exact Dhan brokerage, "
        "detects emotional overtrading/revenge leaks, and builds your permanent track record."
    )

    # 1. Ingestion Section (Text Area or File Upload)
    with st.expander("📥 **Paste or Upload Dhan Orders / Trade Book**", expanded=True):
        in_col1, in_col2 = st.columns([2, 1])
        with in_col1:
            raw_paste = st.text_area(
                "Paste copied orders from Dhan web (or press 'Load Sample' to test)",
                height=130,
                placeholder="Example paste from Dhan:\n09:25:14\tNIFTY 03 Sep 24150 CE\tBUY\t65\t120.50\tTRADED\n09:42:30\tNIFTY 03 Sep 24150 CE\tSELL\t65\t145.00\tTRADED",
                key="dhan_journal_paste"
            )
        with in_col2:
            uploaded_file = st.file_uploader("Or upload Dhan CSV/Excel", type=["csv", "xlsx"])
            c_btn1, c_btn2 = st.columns(2)
            load_sample = c_btn1.button("📋 Load Sample Trades", use_container_width=True)
            clear_btn = c_btn2.button("🧹 Clear", use_container_width=True)

    if load_sample:
        raw_paste = (
            "09:25:14\tNIFTY 08 Sep 23800 CE\tBUY\t65\t160.00\tTRADED\n"
            "09:42:30\tNIFTY 08 Sep 23800 CE\tSELL\t65\t188.50\tTRADED\n"
            "10:15:02\tNIFTY 08 Sep 23850 PE\tBUY\t130\t89.70\tTRADED\n"
            "10:18:15\tNIFTY 08 Sep 23850 PE\tSELL\t130\t74.20\tTRADED\n"
            "10:19:10\tNIFTY 08 Sep 23800 CE\tBUY\t130\t155.00\tTRADED\n"
            "10:24:50\tNIFTY 08 Sep 23800 CE\tSELL\t130\t142.00\tTRADED\n"
        )
        st.session_state["dhan_journal_paste"] = raw_paste

    parsed_df = pd.DataFrame()
    if uploaded_file:
        try:
            if uploaded_file.name.endswith(".csv"):
                up_df = pd.read_csv(uploaded_file)
            else:
                up_df = pd.read_excel(uploaded_file)
            parsed_df = parse_dhan_input(up_df)
        except Exception as exc:
            st.error(f"Failed to read file: {exc}")
    elif raw_paste:
        parsed_df = parse_dhan_input(raw_paste)

    trades = match_trades_fifo(parsed_df) if not parsed_df.empty else []

    if trades:
        # Save to persistent journal
        save_to_journal(trades)
        audit = audit_behavior(trades)

        # 2. Key Metrics Row
        pnl_col = "#0F9D58" if audit.net_pnl > 0 else "#E8503A"
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Net P&L (After Charges)", f"₹{audit.net_pnl:+,.2f}")
        m2.metric("Gross P&L", f"₹{audit.gross_pnl:+,.2f}")
        m3.metric("Dhan Brokerage & Taxes", f"₹{audit.total_charges:,.2f}", f"{audit.brokerage_drag_pct:.0f}% of gross", delta_color="inverse")
        m4.metric("Win Rate", f"{audit.win_rate_pct:.0f}%", f"{audit.winning_trades}W / {audit.losing_trades}L")
        m5.metric("Session Discipline", audit.overtrading_level)

        # 3. Behavioral Anti-Impulse Coach Card
        st.markdown(
            f"""
            <div style="background: #1E222D; border: 2px solid {'#E8503A' if audit.revenge_trades_count > 0 or audit.total_trades >= 6 else '#0F9D58'}; border-radius: 10px; padding: 16px 22px; margin: 15px 0;">
                <b style="font-size: 16px; color: #FFF;">🧠 Anti-Impulse Psychological Audit</b>
                <div style="margin-top: 8px;">
            """,
            unsafe_allow_html=True
        )
        for h in audit.coach_feedback:
            st.markdown(h)
        st.markdown("</div></div>", unsafe_allow_html=True)

        # 4. Detailed Trade-by-Trade Table
        st.markdown("### 📊 Completed Trades & Brokerage Audit")
        t_rows = []
        for t in trades:
            t_col = "🟢" if t.net_pnl > 0 else "🔴"
            t_rows.append({
                "Result": t_col,
                "Instrument": t.symbol,
                "Qty": t.qty,
                "Entry": f"₹{t.buy_price:.2f} ({t.entry_time})",
                "Exit": f"₹{t.sell_price:.2f} ({t.exit_time})",
                "Hold Time": f"{t.hold_duration_mins:.1f}m",
                "Gross P&L": f"₹{t.gross_pnl:+,.2f}",
                "Charges (Dhan)": f"₹{t.charges['total_charges']:,.2f}",
                "Net P&L": f"₹{t.net_pnl:+,.2f}",
                "Psychology Tag": " · ".join(t.impulse_flags)
            })

        st.dataframe(
            pd.DataFrame(t_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Result": st.column_config.TextColumn("Win/Loss", width="small"),
                "Instrument": st.column_config.TextColumn("Contract", width="medium"),
                "Qty": st.column_config.NumberColumn("Qty", width="small"),
                "Hold Time": st.column_config.TextColumn("Duration", width="small"),
                "Gross P&L": st.column_config.TextColumn("Gross", width="small"),
                "Charges (Dhan)": st.column_config.TextColumn("Dhan Fees", width="small"),
                "Net P&L": st.column_config.TextColumn("Net In Hand", width="small"),
                "Psychology Tag": st.column_config.TextColumn("Behavioral Habit", width="large"),
            }
        )

        # 5. Charges Breakdown Detail Expander
        with st.expander("🧾 **Dhan Brokerage & Regulatory Charges Breakdown**"):
            tot_b = sum(t.charges["brokerage"] for t in trades)
            tot_s = sum(t.charges["stt"] for t in trades)
            tot_e = sum(t.charges["exchange"] for t in trades)
            tot_g = sum(t.charges["gst"] for t in trades)
            st.markdown(
                f"""
                - **Dhan Brokerage** (₹20/order): **₹{tot_b:,.2f}**
                - **STT (Securities Transaction Tax - 0.125%)**: **₹{tot_s:,.2f}**
                - **NSE Exchange Charges (0.05%)**: **₹{tot_e:,.2f}**
                - **GST (18% on Brokerage + Exch)**: **₹{tot_g:,.2f}**
                - **Total Drain on Account**: <span style="color:#E8503A;font-weight:700;">₹{audit.total_charges:,.2f}</span>
                """,
                unsafe_allow_html=True
            )
    else:
        st.info("💡 Paste your copied Dhan order lines above or click **'📋 Load Sample Trades'** to see the P&L calculator and Anti-Impulse Coach in action.")

    # 6. Historical Journal Archive
    hist_df = load_journal()
    if not hist_df.empty:
        with st.expander(f"📜 **Historical Journal Track Record ({len(hist_df)} Trades Recorded)**", expanded=False):
            st.dataframe(hist_df, use_container_width=True, hide_index=True)
