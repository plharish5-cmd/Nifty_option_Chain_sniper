"""
paper_trading.py — Professional Paper Trading Engine with Indian F&O Realism,
MIS (Intraday with 3:20 PM Square-off) vs NRML (Carry Forward / BTST / STBT),
Two-Sided Execution (BUY & SELL/Shorting), and Automated Trigger Protection.

Features:
  • Realistic execution: BUY (Long) and SELL (Short / Option Writing / STBT) for CE & PE.
  • Product Types:
      - INTRADAY (MIS): Auto-squares off at 15:20 (3:20 PM IST) at prevailing market price.
      - NORMAL (NRML / BTST / STBT): Carry forward across days overnight! Theta, delta,
        and vega decay are seamlessly captured directly from live Dhan market feed.
  • Dhan App Regulatory Cost Realism: Brokerage (₹20/order), STT (0.1% on Sell leg),
    NSE Options Txn Fee (0.03503%), Stamp Duty (0.003% on Buy leg), SEBI (0.0001%),
    IPFT (0.00005%), and 18% GST.
  • Automated Execution: Auto Stop-Loss, Auto Target 1, Auto Target 2, Dynamic Trailing SL,
    and Scale-Out Partial Profit Booking.
  • Position Conversion: 1-click toggle between INTRADAY (MIS) and NORMAL (NRML).
  • Capital & Discipline Protection: Daily Max Loss Circuit Breaker, 15m win rate preview,
    and capital allocation guardrails.
  • Persistent Trade Logging: data/paper_trading/trades.csv with side and product type tracking.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

log = logging.getLogger("paper_trading")

DATA_DIR = getattr(config, "DATA_DIR", Path(__file__).parent / "data")
PAPER_DIR = DATA_DIR / "paper_trading"
PAPER_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNT_FILE = PAPER_DIR / "account.json"
TRADES_CSV = PAPER_DIR / "trades.csv"
CONFIG_FILE = PAPER_DIR / "paper_config.json"

DEFAULT_INITIAL_CAPITAL = 100000.0  # ₹1,00,000
NIFTY_LOT_SIZE = getattr(config, "LOT_SIZE", getattr(config, "NIFTY_LOT_SIZE", 65))

DEFAULT_CONFIG: Dict[str, Any] = {
    "initial_capital": 100000.0,
    "default_lot_size": 65,
    "brokerage_per_order": 20.0,
    "stt_sell_rate": 0.001,
    "exchange_txn_fee_rate": 0.0003503,
    "sebi_turnover_rate": 0.000001,
    "stamp_duty_buy_rate": 0.00003,
    "ipft_rate": 0.0000005,
    "gst_rate": 0.18,
    "daily_max_loss_limit": 3000.0,
    "max_capital_allocation_pct": 25.0,
    "ctc_spot_trigger_pts": 20.0,
    "ctc_opt_trigger_pts": 10.0,
    "auto_execute_sl": True,
    "auto_execute_target": True,
    "default_sl_pts": 8.0,
    "default_target_pts": 12.0,
    "slippage_pts": 0.1,
    "mis_auto_squareoff_time": "15:20",
    "selling_margin_per_lot": 125000.0,
    "default_product_type": "INTRADAY",
}


def load_paper_config() -> Dict[str, Any]:
    """Loads configuration settings from paper_config.json with fallback defaults."""
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        except Exception as err:
            log.error("Failed to write default paper_config: %s", err)
        return DEFAULT_CONFIG.copy()

    try:
        data = json.loads(CONFIG_FILE.read_text())
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(data)
        return cfg
    except Exception as err:
        log.error("Failed to load paper config: %s", err)
        return DEFAULT_CONFIG.copy()


def save_paper_config(cfg: Dict[str, Any]) -> None:
    """Saves configuration settings to paper_config.json."""
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception as err:
        log.error("Failed to save paper config: %s", err)


@dataclass
class PaperPosition:
    position_id: str
    symbol: str
    strike: float
    opt_type: str                   # "CE" or "PE"
    lots: int
    quantity: int
    entry_time: str
    entry_price: float
    current_price: float
    stop_loss: float
    target_1: float
    target_2: float
    setup_tag: str
    entry_spot: float
    side: str = "BUY"               # "BUY" (Long) or "SELL" (Short / Option Writing / STBT)
    product_type: str = "INTRADAY"  # "INTRADAY" (MIS) or "NORMAL" (NRML / BTST / STBT)
    unrealized_pnl: float = 0.0
    unrealized_pct: float = 0.0
    required_margin: float = 0.0
    highest_price: float = 0.0       # Watermark high LTP
    lowest_price: float = 999999.0   # Watermark low LTP
    trailing_sl_pts: float = 0.0     # Dynamic trailing offset (0 = disabled)
    emotion_tag: str = "Disciplined"
    notes: str = ""


@dataclass
class PaperAccount:
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    cash_balance: float = DEFAULT_INITIAL_CAPITAL
    used_margin: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades_count: int = 0
    open_positions: List[PaperPosition] = field(default_factory=list)
    recent_alerts: List[str] = field(default_factory=list)
    total_charges_paid: float = 0.0

    @property
    def total_equity(self) -> float:
        return self.cash_balance + self.used_margin + self.unrealized_pnl


# ═══════════════════════════════════════════════════════════════════════════
# 1. Exact Dhan App Indian F&O Brokerage & Regulatory Charges Calculator
# ═══════════════════════════════════════════════════════════════════════════

def calculate_trade_charges(
    entry_price: float,
    exit_price: float,
    quantity: int,
    side: str = "BUY",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """
    Computes exact Indian F&O regulatory charges & brokerage as per Dhan App:
      - Brokerage: ₹20 buy + ₹20 sell = ₹40 flat per round trip
      - STT: 0.1% strictly on Sell-side premium turnover (Buy side is ₹0)
      - Exchange Transaction Charges (NSE): 0.03503% on total premium turnover
      - SEBI Turnover Charges: ₹10 per crore (0.0001%) on total premium turnover
      - Stamp Duty: 0.003% strictly on Buy-side premium turnover (Sell side is ₹0)
      - IPFT: ₹0.5 per crore (0.00005%) on total premium turnover
      - GST: 18% on (Brokerage + Exchange Transaction Charges + SEBI charges)
    """
    if cfg is None:
        cfg = load_paper_config()

    is_sell_first = side.upper() == "SELL"
    if is_sell_first:
        # Short position: opened with SELL (entry), covered with BUY (exit)
        sell_turnover = max(0.0, float(entry_price * quantity))
        buy_turnover = max(0.0, float(exit_price * quantity))
    else:
        # Long position: opened with BUY (entry), closed with SELL (exit)
        buy_turnover = max(0.0, float(entry_price * quantity))
        sell_turnover = max(0.0, float(exit_price * quantity))

    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage as per Dhan App (flat ₹20 per executed order leg)
    brokerage = float(cfg.get("brokerage_per_order", 20.0) * 2)

    # 2. STT: Applicable ONLY on Sell side of options (Buy is ₹0)
    stt = round(float(sell_turnover * cfg.get("stt_sell_rate", 0.001)), 2)

    # 3. Exchange Transaction Charges (NSE Options 0.03503%)
    exchange_fee = round(float(total_turnover * cfg.get("exchange_txn_fee_rate", 0.0003503)), 2)

    # 4. SEBI Turnover Charges (₹10/crore = 0.0001%)
    sebi_fee = round(float(total_turnover * cfg.get("sebi_turnover_rate", 0.000001)), 2)

    # 5. Stamp Duty: Applicable ONLY on Buy side of options (0.003%)
    stamp_duty = round(float(buy_turnover * cfg.get("stamp_duty_buy_rate", 0.00003)), 2)

    # 6. IPFT (NSE ₹0.5/crore = 0.00005%)
    ipft_fee = round(float(total_turnover * cfg.get("ipft_rate", 0.0000005)), 2)

    # 7. GST: 18% on (Brokerage + Exchange Txn Fee + SEBI Fee)
    gst_base = brokerage + exchange_fee + sebi_fee
    gst = round(float(gst_base * cfg.get("gst_rate", 0.18)), 2)

    total_charges = round(brokerage + stt + exchange_fee + sebi_fee + stamp_duty + ipft_fee + gst, 2)
    breakeven_pts = round(total_charges / max(1, quantity), 2)

    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_fee": exchange_fee,
        "sebi_fee": sebi_fee,
        "stamp_duty": stamp_duty,
        "ipft_fee": ipft_fee,
        "gst": gst,
        "total_charges": total_charges,
        "breakeven_pts": breakeven_pts,
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
        "total_turnover": round(total_turnover, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 2. State Persistence (JSON & CSV)
# ═══════════════════════════════════════════════════════════════════════════

def load_paper_account() -> PaperAccount:
    """Loads current paper trading account state from disk."""
    if not ACCOUNT_FILE.exists():
        acc = PaperAccount()
        save_paper_account(acc)
        return acc

    try:
        data = json.loads(ACCOUNT_FILE.read_text())
        positions_raw = data.get("open_positions", [])
        positions: List[PaperPosition] = []
        for p in positions_raw:
            try:
                positions.append(PaperPosition(
                    position_id=p.get("position_id", ""),
                    symbol=p.get("symbol", ""),
                    strike=float(p.get("strike", 0.0)),
                    opt_type=p.get("opt_type", "CE"),
                    side=p.get("side", "BUY"),
                    product_type=p.get("product_type", "INTRADAY"),
                    lots=int(p.get("lots", 1)),
                    quantity=int(p.get("quantity", NIFTY_LOT_SIZE)),
                    entry_time=p.get("entry_time", ""),
                    entry_price=float(p.get("entry_price", 0.0)),
                    current_price=float(p.get("current_price", 0.0)),
                    stop_loss=float(p.get("stop_loss", 0.0)),
                    target_1=float(p.get("target_1", 0.0)),
                    target_2=float(p.get("target_2", 0.0)),
                    setup_tag=p.get("setup_tag", "Discretionary"),
                    entry_spot=float(p.get("entry_spot", 0.0)),
                    unrealized_pnl=float(p.get("unrealized_pnl", 0.0)),
                    unrealized_pct=float(p.get("unrealized_pct", 0.0)),
                    required_margin=float(p.get("required_margin", 0.0)),
                    highest_price=float(p.get("highest_price", p.get("entry_price", 0.0))),
                    lowest_price=float(p.get("lowest_price", p.get("entry_price", 0.0))),
                    trailing_sl_pts=float(p.get("trailing_sl_pts", 0.0)),
                    emotion_tag=p.get("emotion_tag", "Disciplined"),
                    notes=p.get("notes", ""),
                ))
            except Exception as p_err:
                log.error("Failed to parse position record: %s", p_err)

        return PaperAccount(
            initial_capital=float(data.get("initial_capital", DEFAULT_INITIAL_CAPITAL)),
            cash_balance=float(data.get("cash_balance", DEFAULT_INITIAL_CAPITAL)),
            used_margin=float(data.get("used_margin", 0.0)),
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
            total_trades_count=int(data.get("total_trades_count", 0)),
            open_positions=positions,
            recent_alerts=data.get("recent_alerts", []),
            total_charges_paid=float(data.get("total_charges_paid", 0.0)),
        )
    except Exception as err:
        log.error("Failed to load paper account: %s", err)
        acc = PaperAccount()
        save_paper_account(acc)
        return acc


def save_paper_account(account: PaperAccount) -> None:
    """Saves paper trading account state to disk."""
    try:
        data = {
            "initial_capital": account.initial_capital,
            "cash_balance": account.cash_balance,
            "used_margin": account.used_margin,
            "realized_pnl": account.realized_pnl,
            "unrealized_pnl": account.unrealized_pnl,
            "total_trades_count": account.total_trades_count,
            "open_positions": [asdict(p) for p in account.open_positions],
            "recent_alerts": account.recent_alerts[-15:] if account.recent_alerts else [],
            "total_charges_paid": account.total_charges_paid,
            "last_updated": datetime.now().isoformat(),
        }
        ACCOUNT_FILE.write_text(json.dumps(data, indent=2))
    except Exception as err:
        log.error("Failed to save paper account: %s", err)


def refill_paper_capital(amount: float = 100000.0, reset_all: bool = False) -> PaperAccount:
    """Refills or resets paper capital."""
    if reset_all:
        acc = PaperAccount(
            initial_capital=amount,
            cash_balance=amount,
            used_margin=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_trades_count=0,
            open_positions=[],
            recent_alerts=[],
            total_charges_paid=0.0,
        )
    else:
        acc = load_paper_account()
        acc.cash_balance += amount
        acc.initial_capital += amount

    save_paper_account(acc)
    return acc


def _log_trade_to_csv(trade_dict: Dict[str, Any]) -> None:
    """Logs a completed or partial trade into trades.csv."""
    headers = [
        "trade_id", "entry_time", "exit_time", "symbol", "strike", "opt_type",
        "side", "product_type", "lots", "quantity", "entry_price", "exit_price",
        "pnl_pts", "pnl_rupees", "charges_rupees", "net_pnl_rupees", "pnl_pct",
        "exit_reason", "setup_tag", "emotion_tag", "entry_spot", "exit_spot",
        "balance_after", "notes"
    ]
    file_exists = TRADES_CSV.exists()
    try:
        with open(TRADES_CSV, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            row = {h: trade_dict.get(h, "") for h in headers}
            writer.writerow(row)
    except Exception as err:
        log.error("Failed to log trade to CSV: %s", err)


def load_trade_history() -> pd.DataFrame:
    """Loads all closed paper trades from CSV into a DataFrame."""
    if not TRADES_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADES_CSV)
        return df
    except Exception as err:
        log.error("Failed to read trades.csv: %s", err)
        return pd.DataFrame()


def convert_paper_position_product(
    position_id: str,
    new_product_type: str,
) -> Tuple[bool, str, PaperAccount]:
    """Converts open position product type between INTRADAY (MIS) and NORMAL (NRML / BTST / STBT)."""
    account = load_paper_account()
    target_pos = next((p for p in account.open_positions if p.position_id == position_id), None)
    if not target_pos:
        return False, f"Position ID {position_id} not found.", account

    old_type = target_pos.product_type
    target_pos.product_type = new_product_type.upper()
    save_paper_account(account)
    msg = f"Converted {target_pos.symbol} ({target_pos.side}) from {old_type} to {target_pos.product_type}."
    return True, msg, account


# ═══════════════════════════════════════════════════════════════════════════
# 3. Live Pricing & Market Data Fetcher
# ═══════════════════════════════════════════════════════════════════════════

def get_current_option_price(
    strike: float,
    opt_type: str,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Retrieves current LTP for an option contract:
      1. From live in-memory option chain (chain_df)
      2. From data/dhan_option_chains/latest_chain.csv
      3. Fallback realistic intrinsic/extrinsic estimation
    """
    is_ce = opt_type.upper() == "CE"
    col_ltp = "ce_ltp" if is_ce else "pe_ltp"

    # 1. In-memory chain
    if chain_df is not None and not chain_df.empty:
        row = chain_df[chain_df["strike"] == strike]
        if not row.empty:
            val = float(row[col_ltp].iloc[0] or 0.0)
            if val > 0.0:
                return round(val, 2)

    # 2. Dhan latest chain CSV
    dhan_csv = DATA_DIR / "dhan_option_chains" / "latest_chain.csv"
    if dhan_csv.exists():
        try:
            dhan_df = pd.read_csv(dhan_csv)
            row = dhan_df[dhan_df["strike"] == strike]
            if not row.empty:
                val = float(row[col_ltp].iloc[0] or 0.0)
                if val > 0.0:
                    return round(val, 2)
        except Exception as err:
            log.error("Error reading latest_chain.csv: %s", err)

    # 3. Fallback realistic pricing model based on spot distance
    diff = spot - strike if is_ce else strike - spot
    base_atm_premium = 85.0
    delta = 0.50 + max(-0.35, min(0.35, diff / 250.0))
    approx_price = max(4.0, base_atm_premium + (diff * delta))
    return round(approx_price, 2)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Position Synchronization & Automated Trigger Engine
# ═══════════════════════════════════════════════════════════════════════════

def sync_open_positions(
    account: PaperAccount,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    auto_execute: bool = True,
    now: Optional[datetime] = None,
) -> PaperAccount:
    """
    Updates current LTP, highest/lowest price, unrealized P&L, and used margin.
    Auto-triggers:
      1. MIS 3:20 PM Cutoff: Auto-squares off INTRADAY positions at 15:20 IST.
      2. NORMAL positions: Carried forward overnight (BTST / STBT) across market sessions.
      3. Stop-Loss & Target triggers for both BUY and SELL sides.
      4. Dynamic Trailing Stop-Loss.
    """
    cfg = load_paper_config()
    auto_sl_enabled = bool(cfg.get("auto_execute_sl", True)) and auto_execute
    auto_tgt_enabled = bool(cfg.get("auto_execute_target", True)) and auto_execute

    now_dt = now or datetime.now()
    cur_t = now_dt.time()
    mis_cutoff_time = datetime.strptime(cfg.get("mis_auto_squareoff_time", "15:20"), "%H:%M").time()
    is_mis_squareoff_due = cur_t >= mis_cutoff_time

    total_unrealized = 0.0
    total_used_margin = 0.0
    retained_positions: List[PaperPosition] = []
    positions_to_process = list(account.open_positions)

    for pos in positions_to_process:
        cur_ltp = get_current_option_price(pos.strike, pos.opt_type, spot, chain_df)
        pos.current_price = cur_ltp

        # ── 1. MIS Auto Square-off at 15:20 IST ─────────────────────────────
        if auto_execute and pos.product_type == "INTRADAY" and is_mis_squareoff_due:
            exit_reason = f"⏰ Auto Square-Off (MIS 3:20 PM Cutoff) @ ₹{cur_ltp:.2f}"
            _, msg, account = exit_paper_position(
                position_id=pos.position_id,
                spot=spot,
                chain_df=chain_df,
                exit_reason=exit_reason,
                custom_exit_price=cur_ltp,
                emotion_tag="Disciplined (MIS 3:20 PM Cutoff)",
            )
            account.recent_alerts.append(f"⏰ {pos.symbol} ({pos.side} MIS): Auto-squared off at 3:20 PM @ ₹{cur_ltp:.2f}.")
            continue

        # Watermark tracking
        if pos.highest_price <= 0.0:
            pos.highest_price = max(pos.entry_price, cur_ltp)
        else:
            pos.highest_price = max(pos.highest_price, cur_ltp)
        pos.lowest_price = min(pos.lowest_price, cur_ltp)

        is_seller = pos.side.upper() == "SELL"

        # ── 2. P&L and Trailing SL Calculation ──────────────────────────────
        if is_seller:
            # SHORT / OPTION WRITING: Profit when price drops
            pnl_pts = pos.entry_price - cur_ltp
            # Trailing SL for seller trails down behind lowest price
            if pos.trailing_sl_pts > 0.0:
                cand_sl = round(pos.lowest_price + pos.trailing_sl_pts, 2)
                if (pos.stop_loss <= 0.0) or (cand_sl < pos.stop_loss):
                    old_sl = pos.stop_loss
                    pos.stop_loss = cand_sl
                    account.recent_alerts.append(
                        f"📈 Trailing SL for {pos.symbol} (SELL) trailed down: ₹{old_sl:.1f} → ₹{pos.stop_loss:.1f} (Low: ₹{pos.lowest_price:.1f})."
                    )

            sl_hit = auto_sl_enabled and (pos.stop_loss > 0.0) and (cur_ltp >= pos.stop_loss)
            t1_hit = auto_tgt_enabled and (pos.target_1 > 0.0) and (cur_ltp <= pos.target_1)
            t2_hit = auto_tgt_enabled and (pos.target_2 > 0.0) and (cur_ltp <= pos.target_2)
        else:
            # LONG / OPTION BUYING: Profit when price rises
            pnl_pts = cur_ltp - pos.entry_price
            # Trailing SL for buyer trails up behind highest price
            if pos.trailing_sl_pts > 0.0:
                cand_sl = round(pos.highest_price - pos.trailing_sl_pts, 2)
                if cand_sl > pos.stop_loss:
                    old_sl = pos.stop_loss
                    pos.stop_loss = cand_sl
                    account.recent_alerts.append(
                        f"📈 Trailing SL for {pos.symbol} (BUY) trailed up: ₹{old_sl:.1f} → ₹{pos.stop_loss:.1f} (High: ₹{pos.highest_price:.1f})."
                    )

            sl_hit = auto_sl_enabled and (pos.stop_loss > 0.0) and (cur_ltp <= pos.stop_loss)
            t1_hit = auto_tgt_enabled and (pos.target_1 > 0.0) and (cur_ltp >= pos.target_1)
            t2_hit = auto_tgt_enabled and (pos.target_2 > 0.0) and (cur_ltp >= pos.target_2)

        pos.unrealized_pnl = round(pnl_pts * pos.quantity, 2)
        pos.unrealized_pct = round((pnl_pts / max(0.1, pos.entry_price)) * 100.0, 2)

        # ── 3. Automated SL / Target Triggers ───────────────────────────────
        if sl_hit:
            exit_reason = f"🛑 Auto SL Hit @ ₹{cur_ltp:.2f} (SL: ₹{pos.stop_loss:.2f})"
            _, msg, account = exit_paper_position(
                position_id=pos.position_id,
                spot=spot,
                chain_df=chain_df,
                exit_reason=exit_reason,
                custom_exit_price=cur_ltp,
                emotion_tag="Disciplined (SL Executed)",
            )
            account.recent_alerts.append(f"🛑 {pos.symbol} ({pos.side}): Stop Loss triggered @ ₹{cur_ltp:.2f}.")
            continue

        elif t2_hit:
            exit_reason = f"🎯 Auto Target 2 Hit @ ₹{cur_ltp:.2f} (T2: ₹{pos.target_2:.2f})"
            _, msg, account = exit_paper_position(
                position_id=pos.position_id,
                spot=spot,
                chain_df=chain_df,
                exit_reason=exit_reason,
                custom_exit_price=cur_ltp,
                emotion_tag="Disciplined (T2 Target Hit)",
            )
            account.recent_alerts.append(f"🎯 {pos.symbol} ({pos.side}): Target 2 reached @ ₹{cur_ltp:.2f}.")
            continue

        elif t1_hit:
            if pos.lots > 1 and pos.target_2 > 0.0:
                half_lots = max(1, pos.lots // 2)
                _, msg, account = partial_exit_paper_position(
                    position_id=pos.position_id,
                    exit_lots=half_lots,
                    spot=spot,
                    chain_df=chain_df,
                    exit_reason=f"🎯 Auto Target 1 Hit (Booked {half_lots} Lots) @ ₹{cur_ltp:.2f}",
                    custom_exit_price=cur_ltp,
                    emotion_tag="Disciplined (Scaled Out at T1)",
                )
                for rem_p in account.open_positions:
                    if rem_p.position_id == pos.position_id:
                        rem_p.stop_loss = rem_p.entry_price  # CTC Lock
                        rem_p.target_1 = 0.0
                        break
                account.recent_alerts.append(
                    f"🎯 {pos.symbol} ({pos.side}): Target 1 hit! Booked {half_lots} lot(s) @ ₹{cur_ltp:.2f}, locked remainder to CTC."
                )
                continue
            else:
                exit_reason = f"🎯 Auto Target 1 Hit @ ₹{cur_ltp:.2f} (T1: ₹{pos.target_1:.2f})"
                _, msg, account = exit_paper_position(
                    position_id=pos.position_id,
                    spot=spot,
                    chain_df=chain_df,
                    exit_reason=exit_reason,
                    custom_exit_price=cur_ltp,
                    emotion_tag="Disciplined (Target 1 Hit)",
                )
                account.recent_alerts.append(f"🎯 {pos.symbol} ({pos.side}): Target 1 reached @ ₹{cur_ltp:.2f}.")
                continue

        retained_positions.append(pos)
        total_unrealized += pos.unrealized_pnl
        total_used_margin += pos.required_margin

    account.open_positions = retained_positions
    account.unrealized_pnl = round(total_unrealized, 2)
    account.used_margin = round(total_used_margin, 2)
    return account


# ═══════════════════════════════════════════════════════════════════════════
# 5. Order Execution & Position Management Engine
# ═══════════════════════════════════════════════════════════════════════════

def place_paper_order(
    strike: float,
    opt_type: str,                   # "CE" or "PE"
    lots: int,
    spot: float,
    side: str = "BUY",               # "BUY" (Long) or "SELL" (Short / Option Writing / STBT)
    product_type: str = "INTRADAY",  # "INTRADAY" (MIS) or "NORMAL" (NRML / BTST / STBT)
    chain_df: Optional[pd.DataFrame] = None,
    stop_loss: float = 0.0,
    target_1: float = 0.0,
    target_2: float = 0.0,
    setup_tag: str = "Discretionary",
    custom_price: Optional[float] = None,
    trailing_sl_pts: float = 0.0,
    notes: str = "",
    override_risk_limit: bool = False,
) -> Tuple[bool, str, PaperAccount]:
    """
    Executes a paper order for Nifty options:
      - Supports BUY (Long) and SELL (Short / Option Writing / STBT).
      - Supports INTRADAY (MIS - auto square off at 3:20 PM) and NORMAL (NRML - carry forward).
      - Checks margin and daily circuit breaker limits.
    """
    account = load_paper_account()
    cfg = load_paper_config()
    opt_type = opt_type.upper()
    side = side.upper()
    product_type = product_type.upper()
    qty = lots * NIFTY_LOT_SIZE

    # Daily Loss Limit Circuit Breaker Check
    if not override_risk_limit:
        daily_summary = get_daily_pnl_summary(account)
        if daily_summary["is_circuit_breaker_triggered"]:
            limit_val = daily_summary["daily_loss_limit"]
            curr_loss = abs(daily_summary["today_total_net_pnl"])
            return False, f"🚨 DAILY LOSS CIRCUIT BREAKER: Today's loss (-₹{curr_loss:,.2f}) exceeded ₹{limit_val:,.2f}. Trading locked to protect capital.", account

    # Fetch live price
    ltp = custom_price if (custom_price and custom_price > 0) else get_current_option_price(strike, opt_type, spot, chain_df)
    if ltp <= 0.0:
        return False, "Could not fetch a valid option price.", account

    # Slippage simulation
    slippage = float(cfg.get("slippage_pts", 0.1))
    execution_price = round(ltp + slippage, 2) if side == "BUY" else round(max(0.5, ltp - slippage), 2)

    # Margin check
    if side == "SELL":
        margin_per_lot = float(cfg.get("selling_margin_per_lot", 125000.0))
        required_capital = round(lots * margin_per_lot, 2)
    else:
        required_capital = round(execution_price * qty, 2)

    if required_capital > account.cash_balance:
        return False, f"Insufficient margin: Required ₹{required_capital:,.2f} ({side} {lots} lot(s)), Available Cash ₹{account.cash_balance:,.2f}.", account

    # Deduct margin from cash balance
    account.cash_balance = round(account.cash_balance - required_capital, 2)
    account.used_margin = round(account.used_margin + required_capital, 2)
    account.total_trades_count += 1

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade_id = f"PT-{datetime.now().strftime('%m%d%H%M%S')}-{account.total_trades_count}"
    symbol = f"NIFTY {int(strike)} {opt_type}"

    new_pos = PaperPosition(
        position_id=trade_id,
        symbol=symbol,
        strike=strike,
        opt_type=opt_type,
        side=side,
        product_type=product_type,
        lots=lots,
        quantity=qty,
        entry_time=now_str,
        entry_price=execution_price,
        current_price=execution_price,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        setup_tag=setup_tag,
        entry_spot=spot,
        unrealized_pnl=0.0,
        unrealized_pct=0.0,
        required_margin=required_capital,
        highest_price=execution_price,
        lowest_price=execution_price,
        trailing_sl_pts=trailing_sl_pts,
        emotion_tag="Disciplined Entry",
        notes=notes,
    )

    account.open_positions.append(new_pos)
    account.recent_alerts.append(f"🛒 {side} {lots} Lot(s) of {symbol} [{product_type}] @ ₹{execution_price:.2f}.")
    save_paper_account(account)

    msg = f"Order Executed: {side} {lots} Lot(s) ({qty} Qty) of {symbol} [{product_type}] @ ₹{execution_price:.2f} (Margin: ₹{required_capital:,.2f})."
    return True, msg, account


def exit_paper_position(
    position_id: str,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    exit_reason: str = "Manual Exit",
    custom_exit_price: Optional[float] = None,
    emotion_tag: str = "Disciplined",
    notes: str = "",
) -> Tuple[bool, str, PaperAccount]:
    """Exits an open paper position (BUY or SELL), credits cash + Net P&L, logs to trades.csv."""
    account = load_paper_account()
    target_pos: Optional[PaperPosition] = None
    remaining_positions: List[PaperPosition] = []

    for pos in account.open_positions:
        if pos.position_id == position_id:
            target_pos = pos
        else:
            remaining_positions.append(pos)

    if not target_pos:
        return False, f"Position ID {position_id} not found.", account

    # Fetch exit LTP
    exit_price = custom_exit_price if (custom_exit_price and custom_exit_price > 0) else get_current_option_price(target_pos.strike, target_pos.opt_type, spot, chain_df)
    exit_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Gross & Net P&L based on BUY vs SELL side
    if target_pos.side.upper() == "SELL":
        pnl_pts = round(target_pos.entry_price - exit_price, 2)
    else:
        pnl_pts = round(exit_price - target_pos.entry_price, 2)

    gross_pnl_rupees = round(pnl_pts * target_pos.quantity, 2)
    charges = calculate_trade_charges(target_pos.entry_price, exit_price, target_pos.quantity, side=target_pos.side)
    charges_amt = charges["total_charges"]
    net_pnl_rupees = round(gross_pnl_rupees - charges_amt, 2)
    pnl_pct = round((pnl_pts / max(0.1, target_pos.entry_price)) * 100.0, 2)

    # Release margin + credit Net P&L to cash balance
    account.cash_balance = round(account.cash_balance + target_pos.required_margin + net_pnl_rupees, 2)
    account.used_margin = round(max(0.0, account.used_margin - target_pos.required_margin), 2)
    account.realized_pnl = round(account.realized_pnl + net_pnl_rupees, 2)
    account.total_charges_paid = round(account.total_charges_paid + charges_amt, 2)
    account.open_positions = remaining_positions

    sync_open_positions(account, spot, chain_df, auto_execute=False)
    save_paper_account(account)

    # Log to CSV
    trade_record = {
        "trade_id": target_pos.position_id,
        "entry_time": target_pos.entry_time,
        "exit_time": exit_time_str,
        "symbol": target_pos.symbol,
        "strike": target_pos.strike,
        "opt_type": target_pos.opt_type,
        "side": target_pos.side,
        "product_type": target_pos.product_type,
        "lots": target_pos.lots,
        "quantity": target_pos.quantity,
        "entry_price": target_pos.entry_price,
        "exit_price": exit_price,
        "pnl_pts": pnl_pts,
        "pnl_rupees": gross_pnl_rupees,
        "charges_rupees": charges_amt,
        "net_pnl_rupees": net_pnl_rupees,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "setup_tag": target_pos.setup_tag,
        "emotion_tag": emotion_tag or target_pos.emotion_tag,
        "entry_spot": target_pos.entry_spot,
        "exit_spot": spot,
        "balance_after": account.cash_balance,
        "notes": notes or target_pos.notes,
    }
    _log_trade_to_csv(trade_record)

    sign = "+" if net_pnl_rupees >= 0 else ""
    msg = f"Closed {target_pos.symbol} ({target_pos.side}) @ ₹{exit_price:.2f}. Net P&L: {sign}₹{net_pnl_rupees:,.2f} ({sign}{pnl_pct:.1f}%) [Charges: ₹{charges_amt:.2f}]."
    return True, msg, account


def partial_exit_paper_position(
    position_id: str,
    exit_lots: int,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    exit_reason: str = "Partial Exit",
    custom_exit_price: Optional[float] = None,
    emotion_tag: str = "Disciplined",
    notes: str = "",
) -> Tuple[bool, str, PaperAccount]:
    """Closes a subset of lots in an active position (scaling out)."""
    account = load_paper_account()
    target_pos: Optional[PaperPosition] = None

    for pos in account.open_positions:
        if pos.position_id == position_id:
            target_pos = pos
            break

    if not target_pos:
        return False, f"Position ID {position_id} not found.", account

    if exit_lots <= 0:
        return False, "Exit lots must be at least 1.", account

    if exit_lots >= target_pos.lots:
        return exit_paper_position(
            position_id=position_id,
            spot=spot,
            chain_df=chain_df,
            exit_reason=exit_reason,
            custom_exit_price=custom_exit_price,
            emotion_tag=emotion_tag,
            notes=notes,
        )

    exit_qty = exit_lots * NIFTY_LOT_SIZE
    exit_price = custom_exit_price if (custom_exit_price and custom_exit_price > 0) else get_current_option_price(target_pos.strike, target_pos.opt_type, spot, chain_df)
    exit_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if target_pos.side.upper() == "SELL":
        pnl_pts = round(target_pos.entry_price - exit_price, 2)
    else:
        pnl_pts = round(exit_price - target_pos.entry_price, 2)

    gross_pnl_rupees = round(pnl_pts * exit_qty, 2)
    charges = calculate_trade_charges(target_pos.entry_price, exit_price, exit_qty, side=target_pos.side)
    charges_amt = charges["total_charges"]
    net_pnl_rupees = round(gross_pnl_rupees - charges_amt, 2)
    pnl_pct = round((pnl_pts / max(0.1, target_pos.entry_price)) * 100.0, 2)

    # Proportional released margin
    released_margin = round(target_pos.required_margin * (exit_lots / target_pos.lots), 2)

    account.cash_balance = round(account.cash_balance + released_margin + net_pnl_rupees, 2)
    account.used_margin = round(max(0.0, account.used_margin - released_margin), 2)
    account.realized_pnl = round(account.realized_pnl + net_pnl_rupees, 2)
    account.total_charges_paid = round(account.total_charges_paid + charges_amt, 2)

    target_pos.lots -= exit_lots
    target_pos.quantity -= exit_qty
    target_pos.required_margin = round(max(0.0, target_pos.required_margin - released_margin), 2)
    pts_unreal = (target_pos.entry_price - target_pos.current_price) if target_pos.side == "SELL" else (target_pos.current_price - target_pos.entry_price)
    target_pos.unrealized_pnl = round(pts_unreal * target_pos.quantity, 2)

    sync_open_positions(account, spot, chain_df, auto_execute=False)
    save_paper_account(account)

    trade_record = {
        "trade_id": f"{target_pos.position_id}-P{exit_lots}",
        "entry_time": target_pos.entry_time,
        "exit_time": exit_time_str,
        "symbol": target_pos.symbol,
        "strike": target_pos.strike,
        "opt_type": target_pos.opt_type,
        "side": target_pos.side,
        "product_type": target_pos.product_type,
        "lots": exit_lots,
        "quantity": exit_qty,
        "entry_price": target_pos.entry_price,
        "exit_price": exit_price,
        "pnl_pts": pnl_pts,
        "pnl_rupees": gross_pnl_rupees,
        "charges_rupees": charges_amt,
        "net_pnl_rupees": net_pnl_rupees,
        "pnl_pct": pnl_pct,
        "exit_reason": f"Partial: {exit_reason}",
        "setup_tag": target_pos.setup_tag,
        "emotion_tag": emotion_tag,
        "entry_spot": target_pos.entry_spot,
        "exit_spot": spot,
        "balance_after": account.cash_balance,
        "notes": notes,
    }
    _log_trade_to_csv(trade_record)

    sign = "+" if net_pnl_rupees >= 0 else ""
    msg = f"Partial Exit: Closed {exit_lots} Lot(s) of {target_pos.symbol} @ ₹{exit_price:.2f}. Net P&L: {sign}₹{net_pnl_rupees:,.2f}. {target_pos.lots} Lot(s) remaining."
    return True, msg, account


def exit_all_positions(
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    exit_reason: str = "Square Off All",
) -> Tuple[int, PaperAccount]:
    """Exits all currently open positions at market prices."""
    account = load_paper_account()
    count = 0
    while account.open_positions:
        pos = account.open_positions[0]
        ok, _, account = exit_paper_position(
            position_id=pos.position_id,
            spot=spot,
            chain_df=chain_df,
            exit_reason=exit_reason,
            emotion_tag="Emergency Panic Square Off",
        )
        if ok:
            count += 1
        else:
            break
    return count, account


def update_paper_position_sl(
    position_id: str,
    new_sl: float,
) -> Tuple[bool, str, PaperAccount]:
    """Updates stop loss price for an open position."""
    return update_paper_position_sl_target(position_id=position_id, new_sl=new_sl)


def update_paper_position_sl_target(
    position_id: str,
    new_sl: Optional[float] = None,
    new_t1: Optional[float] = None,
    new_t2: Optional[float] = None,
    trailing_sl_pts: Optional[float] = None,
) -> Tuple[bool, str, PaperAccount]:
    """Updates Stop Loss, Target 1, Target 2, or Trailing SL offset for an active position."""
    account = load_paper_account()
    target_pos = next((p for p in account.open_positions if p.position_id == position_id), None)
    if not target_pos:
        return False, f"Position ID {position_id} not found.", account

    updates = []
    if new_sl is not None and new_sl > 0:
        old_sl = target_pos.stop_loss
        target_pos.stop_loss = round(new_sl, 2)
        updates.append(f"SL: ₹{old_sl:.1f} → ₹{target_pos.stop_loss:.1f}")

    if new_t1 is not None and new_t1 > 0:
        old_t1 = target_pos.target_1
        target_pos.target_1 = round(new_t1, 2)
        updates.append(f"T1: ₹{old_t1:.1f} → ₹{target_pos.target_1:.1f}")

    if new_t2 is not None and new_t2 > 0:
        old_t2 = target_pos.target_2
        target_pos.target_2 = round(new_t2, 2)
        updates.append(f"T2: ₹{old_t2:.1f} → ₹{target_pos.target_2:.1f}")

    if trailing_sl_pts is not None:
        target_pos.trailing_sl_pts = round(trailing_sl_pts, 2)
        updates.append(f"Trailing SL: {target_pos.trailing_sl_pts} pts")

    save_paper_account(account)
    msg = f"Updated {target_pos.symbol}: " + ", ".join(updates)
    return True, msg, account


def trail_paper_position_sl(
    position_id: str,
    trail_pts: float,
) -> Tuple[bool, str, PaperAccount]:
    """Trails SL by trail_pts points."""
    account = load_paper_account()
    target_pos = next((p for p in account.open_positions if p.position_id == position_id), None)
    if not target_pos:
        return False, f"Position ID {position_id} not found.", account

    old_sl = target_pos.stop_loss
    if target_pos.side.upper() == "SELL":
        # For seller, tighter SL moves down
        new_sl = round(old_sl - trail_pts, 2)
        min_sl = target_pos.current_price + 1.0
        new_sl = max(new_sl, min_sl)
        target_pos.stop_loss = new_sl
        save_paper_account(account)
        return True, f"Trailed SL for {target_pos.symbol} (SELL) down by -{trail_pts:.1f} pts (₹{old_sl:.1f} → ₹{new_sl:.1f}).", account
    else:
        # For buyer, tighter SL moves up
        new_sl = round(old_sl + trail_pts, 2)
        max_sl = max(0.5, target_pos.current_price - 1.0)
        new_sl = min(new_sl, max_sl)
        target_pos.stop_loss = new_sl
        save_paper_account(account)
        return True, f"Trailed SL for {target_pos.symbol} (BUY) up by +{trail_pts:.1f} pts (₹{old_sl:.1f} → ₹{new_sl:.1f}).", account


# ═══════════════════════════════════════════════════════════════════════════
# 6. Daily P&L Summary & Circuit Breaker Engine
# ═══════════════════════════════════════════════════════════════════════════

def get_daily_pnl_summary(account: PaperAccount, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Computes today's trading summary and checks circuit breaker."""
    today_str = (now or datetime.now()).strftime("%Y-%m-%d")
    df = load_trade_history()
    cfg = load_paper_config()
    daily_limit = float(cfg.get("daily_max_loss_limit", 3000.0))

    today_realized_net = 0.0
    today_gross_pnl = 0.0
    today_charges = 0.0
    today_trades = 0
    today_wins = 0
    today_losses = 0

    if not df.empty and "exit_time" in df.columns:
        mask = df["exit_time"].astype(str).str.startswith(today_str)
        today_df = df[mask]
        today_trades = len(today_df)

        if not today_df.empty:
            if "net_pnl_rupees" in today_df.columns:
                today_realized_net = float(pd.to_numeric(today_df["net_pnl_rupees"], errors="coerce").fillna(0).sum())
            else:
                today_realized_net = float(pd.to_numeric(today_df["pnl_rupees"], errors="coerce").fillna(0).sum())

            if "charges_rupees" in today_df.columns:
                today_charges = float(pd.to_numeric(today_df["charges_rupees"], errors="coerce").fillna(0).sum())

            today_gross_pnl = float(pd.to_numeric(today_df["pnl_rupees"], errors="coerce").fillna(0).sum())
            pnls = pd.to_numeric(today_df["pnl_rupees"], errors="coerce").fillna(0)
            today_wins = int((pnls > 0).sum())
            today_losses = int((pnls < 0).sum())

    today_total_net = today_realized_net + account.unrealized_pnl
    is_breached = today_total_net <= -daily_limit

    return {
        "today_date": today_str,
        "today_trades_count": today_trades,
        "today_wins": today_wins,
        "today_losses": today_losses,
        "today_gross_pnl": round(today_gross_pnl, 2),
        "today_charges": round(today_charges, 2),
        "today_realized_net": round(today_realized_net, 2),
        "today_unrealized_pnl": round(account.unrealized_pnl, 2),
        "today_total_net_pnl": round(today_total_net, 2),
        "daily_loss_limit": daily_limit,
        "is_circuit_breaker_triggered": is_breached,
        "remaining_daily_risk": max(0.0, round(daily_limit + today_total_net, 2)),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 7. Advanced Performance Analytics Engine
# ═══════════════════════════════════════════════════════════════════════════

def get_paper_performance_stats(account: PaperAccount) -> Dict[str, Any]:
    """Calculates comprehensive trading performance metrics from trades.csv."""
    df = load_trade_history()
    stats: Dict[str, Any] = {
        "total_trades": len(df),
        "total_realized_pnl": account.realized_pnl,
        "total_gross_pnl": 0.0,
        "total_charges": account.total_charges_paid,
        "win_rate_pct": 0.0,
        "wins_count": 0,
        "losses_count": 0,
        "profit_factor": 0.0,
        "avg_win_rupees": 0.0,
        "avg_loss_rupees": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "expectancy_per_trade": 0.0,
        "max_drawdown_rupees": 0.0,
        "max_drawdown_pct": 0.0,
        "current_streak": "0",
        "best_win_streak": 0,
        "worst_loss_streak": 0,
        "ce_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "pe_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "buy_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "sell_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "mis_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "nrml_stats": {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0},
        "session_breakdown": {},
        "tag_breakdown": {},
        "emotion_breakdown": {},
    }

    if df.empty:
        return stats

    gross_pnls = pd.to_numeric(df["pnl_rupees"], errors="coerce").fillna(0.0)
    net_pnls = pd.to_numeric(df["net_pnl_rupees"], errors="coerce").fillna(gross_pnls) if "net_pnl_rupees" in df.columns else gross_pnls
    charges = pd.to_numeric(df["charges_rupees"], errors="coerce").fillna(0.0) if "charges_rupees" in df.columns else pd.Series(0.0, index=df.index)

    stats["total_gross_pnl"] = round(float(gross_pnls.sum()), 2)
    stats["total_charges"] = round(float(charges.sum()), 2)
    stats["total_realized_pnl"] = round(float(net_pnls.sum()), 2)

    df["pnl_rupees"] = gross_pnls
    df["net_pnl_rupees"] = net_pnls

    wins = net_pnls[net_pnls > 0]
    losses = net_pnls[net_pnls < 0]

    stats["wins_count"] = len(wins)
    stats["losses_count"] = len(losses)
    stats["win_rate_pct"] = round((len(wins) / len(df)) * 100.0, 1)

    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0

    stats["profit_factor"] = round(gross_profit / max(1.0, gross_loss), 2)
    stats["avg_win_rupees"] = round(wins.mean(), 2) if len(wins) else 0.0
    stats["avg_loss_rupees"] = round(losses.mean(), 2) if len(losses) else 0.0
    stats["largest_win"] = round(net_pnls.max(), 2) if len(net_pnls) else 0.0
    stats["largest_loss"] = round(net_pnls.min(), 2) if len(net_pnls) else 0.0
    stats["expectancy_per_trade"] = round(net_pnls.mean(), 2) if len(net_pnls) else 0.0

    # Drawdown Calculation
    cum_pnl = net_pnls.cumsum()
    equity_series = account.initial_capital + cum_pnl
    peak_series = equity_series.cummax()
    drawdowns = peak_series - equity_series
    stats["max_drawdown_rupees"] = round(float(drawdowns.max()), 2) if not drawdowns.empty else 0.0
    stats["max_drawdown_pct"] = round(float((drawdowns / peak_series).max() * 100.0), 1) if (not drawdowns.empty and peak_series.max() > 0) else 0.0

    # Win / Loss Streaks
    cur_streak_type = None
    cur_streak_count = 0
    best_win = 0
    worst_loss = 0
    for pnl in net_pnls:
        is_win = pnl > 0
        if cur_streak_type == is_win:
            cur_streak_count += 1
        else:
            cur_streak_type = is_win
            cur_streak_count = 1
        if is_win and cur_streak_count > best_win:
            best_win = cur_streak_count
        elif (not is_win) and cur_streak_count > worst_loss:
            worst_loss = cur_streak_count

    stats["best_win_streak"] = best_win
    stats["worst_loss_streak"] = worst_loss
    if len(df) > 0 and cur_streak_type is not None:
        stats["current_streak"] = f"+{cur_streak_count} W" if cur_streak_type else f"-{cur_streak_count} L"

    # CE vs PE Breakdown
    if "opt_type" in df.columns:
        for opt in ["CE", "PE"]:
            m = df["opt_type"].astype(str).str.upper() == opt
            sub_df = df[m]
            c_cnt = len(sub_df)
            if c_cnt > 0:
                sub_net = pd.to_numeric(sub_df["net_pnl_rupees"], errors="coerce").fillna(0.0)
                c_wins = len(sub_net[sub_net > 0])
                stats[f"{opt.lower()}_stats"] = {
                    "trades": c_cnt,
                    "win_rate": round((c_wins / c_cnt) * 100.0, 1),
                    "net_pnl": round(float(sub_net.sum()), 2),
                }

    # BUY vs SELL Breakdown
    if "side" in df.columns:
        for s in ["BUY", "SELL"]:
            m = df["side"].astype(str).str.upper() == s
            sub_df = df[m]
            c_cnt = len(sub_df)
            if c_cnt > 0:
                sub_net = pd.to_numeric(sub_df["net_pnl_rupees"], errors="coerce").fillna(0.0)
                c_wins = len(sub_net[sub_net > 0])
                stats[f"{s.lower()}_stats"] = {
                    "trades": c_cnt,
                    "win_rate": round((c_wins / c_cnt) * 100.0, 1),
                    "net_pnl": round(float(sub_net.sum()), 2),
                }

    # MIS vs NORMAL Breakdown
    if "product_type" in df.columns:
        for p_type, p_key in [("INTRADAY", "mis_stats"), ("NORMAL", "nrml_stats")]:
            m = df["product_type"].astype(str).str.upper() == p_type
            sub_df = df[m]
            c_cnt = len(sub_df)
            if c_cnt > 0:
                sub_net = pd.to_numeric(sub_df["net_pnl_rupees"], errors="coerce").fillna(0.0)
                c_wins = len(sub_net[sub_net > 0])
                stats[p_key] = {
                    "trades": c_cnt,
                    "win_rate": round((c_wins / c_cnt) * 100.0, 1),
                    "net_pnl": round(float(sub_net.sum()), 2),
                }

    # Time-of-Day Session Breakdown
    if "entry_time" in df.columns:
        sessions = {
            "Morning (09:15-11:30)": [],
            "Midday Trap (11:30-13:15)": [],
            "Afternoon Trend (13:15-15:30)": [],
        }
        for _, row in df.iterrows():
            t_str = str(row.get("entry_time", ""))
            try:
                time_part = t_str.split()[1] if " " in t_str else t_str
                hh, mm = int(time_part.split(":")[0]), int(time_part.split(":")[1])
                t_val = hh * 60 + mm
                pnl_val = float(row.get("net_pnl_rupees", 0.0) or 0.0)
                if t_val < (11 * 60 + 30):
                    sessions["Morning (09:15-11:30)"].append(pnl_val)
                elif t_val <= (13 * 60 + 15):
                    sessions["Midday Trap (11:30-13:15)"].append(pnl_val)
                else:
                    sessions["Afternoon Trend (13:15-15:30)"].append(pnl_val)
            except Exception:
                pass

        for s_name, pnl_list in sessions.items():
            if pnl_list:
                s_arr = np.array(pnl_list)
                stats["session_breakdown"][s_name] = {
                    "trades": len(s_arr),
                    "win_rate": round((np.sum(s_arr > 0) / len(s_arr)) * 100.0, 1),
                    "net_pnl": round(float(np.sum(s_arr)), 2),
                }

    # Strategy Setup Tag Breakdown
    if "setup_tag" in df.columns:
        tag_grp = df.groupby("setup_tag")["net_pnl_rupees"].agg(["count", "sum"]).reset_index()
        for _, row in tag_grp.iterrows():
            tag_name = row["setup_tag"]
            cnt = int(row["count"])
            tot = float(row["sum"])
            tag_trades = df[df["setup_tag"] == tag_name]
            tag_p = pd.to_numeric(tag_trades["net_pnl_rupees"], errors="coerce").fillna(0.0)
            tag_wins = len(tag_p[tag_p > 0])
            wr = round((tag_wins / max(1, cnt)) * 100.0, 1)
            stats["tag_breakdown"][tag_name] = {
                "trades": cnt,
                "net_pnl": round(tot, 2),
                "win_rate": wr,
            }

    # Emotion Tag Breakdown
    if "emotion_tag" in df.columns:
        emo_grp = df.groupby("emotion_tag")["net_pnl_rupees"].agg(["count", "sum"]).reset_index()
        for _, row in emo_grp.iterrows():
            emo_name = str(row["emotion_tag"]).strip()
            if not emo_name or emo_name == "nan":
                continue
            cnt = int(row["count"])
            tot = float(row["sum"])
            emo_trades = df[df["emotion_tag"] == emo_name]
            emo_p = pd.to_numeric(emo_trades["net_pnl_rupees"], errors="coerce").fillna(0.0)
            emo_wins = len(emo_p[emo_p > 0])
            stats["emotion_breakdown"][emo_name] = {
                "trades": cnt,
                "net_pnl": round(tot, 2),
                "win_rate": round((emo_wins / max(1, cnt)) * 100.0, 1),
            }

    return stats
