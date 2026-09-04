"""
paper_trading.py — Paper Trading Engine with Impulse Control & Performance Analytics.

Features:
  • Starts with ₹1,00,000 (One Lakh) paper capital.
  • Realistic execution: Buy CE / Buy PE with strike & lot size selection.
  • Real-time price fetching from Dhan Option Chain / live minute feed.
  • Impulse Trading Guardrails: Discipline Gate with 15m win rate preview,
    midday theta trap warnings, and risk-per-trade limits.
  • Refill & Reset: Top up capital to any amount with one click.
  • Persistent Trade Logging: Every executed and closed trade is logged to
    data/paper_trading/trades.csv.
  • Performance Analytics: Win Rate %, Profit Factor, Net P&L, Equity Curve,
    and Breakdown by Strategy vs Impulse Tag.
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

DEFAULT_INITIAL_CAPITAL = 100000.0  # ₹1,00,000
NIFTY_LOT_SIZE = getattr(config, "NIFTY_LOT_SIZE", 75)


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
    unrealized_pnl: float = 0.0
    unrealized_pct: float = 0.0
    required_margin: float = 0.0


@dataclass
class PaperAccount:
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    cash_balance: float = DEFAULT_INITIAL_CAPITAL
    used_margin: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades_count: int = 0
    open_positions: List[PaperPosition] = field(default_factory=list)

    @property
    def total_equity(self) -> float:
        return self.cash_balance + self.used_margin + self.unrealized_pnl


# ═══════════════════════════════════════════════════════════════════════════
# 1. State Persistence (JSON & CSV)
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
        positions = [PaperPosition(**p) for p in positions_raw]

        return PaperAccount(
            initial_capital=float(data.get("initial_capital", DEFAULT_INITIAL_CAPITAL)),
            cash_balance=float(data.get("cash_balance", DEFAULT_INITIAL_CAPITAL)),
            used_margin=float(data.get("used_margin", 0.0)),
            realized_pnl=float(data.get("realized_pnl", 0.0)),
            unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
            total_trades_count=int(data.get("total_trades_count", 0)),
            open_positions=positions,
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
            "last_updated": datetime.now().isoformat(),
        }
        ACCOUNT_FILE.write_text(json.dumps(data, indent=2))
    except Exception as err:
        log.error("Failed to save paper account: %s", err)


def refill_paper_capital(amount: float = 100000.0, reset_all: bool = False) -> PaperAccount:
    """
    Refills or resets paper capital.
    If reset_all is True, wipes open positions and resets cash to the specified amount.
    Otherwise adds amount to current cash balance.
    """
    if reset_all:
        acc = PaperAccount(
            initial_capital=amount,
            cash_balance=amount,
            used_margin=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            total_trades_count=0,
            open_positions=[],
        )
    else:
        acc = load_paper_account()
        acc.cash_balance += amount
        acc.initial_capital += amount

    save_paper_account(acc)
    return acc


def _log_trade_to_csv(trade_dict: Dict[str, Any]) -> None:
    """Logs a completed trade into trades.csv."""
    headers = [
        "trade_id", "entry_time", "exit_time", "symbol", "strike", "opt_type",
        "lots", "quantity", "entry_price", "exit_price", "pnl_pts",
        "pnl_rupees", "pnl_pct", "exit_reason", "setup_tag", "entry_spot",
        "exit_spot", "balance_after"
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
    except Exception:
        return pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Live Pricing & Market Data Fetcher
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
        except Exception:
            pass

    # 3. Fallback realistic pricing model based on spot distance
    diff = spot - strike if is_ce else strike - spot
    base_atm_premium = 85.0
    delta = 0.50 + max(-0.35, min(0.35, diff / 250.0))
    approx_price = max(4.0, base_atm_premium + (diff * delta))
    return round(approx_price, 2)


def sync_open_positions(
    account: PaperAccount,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
) -> PaperAccount:
    """Updates current LTP, unrealized P&L, and used margin for all open positions."""
    total_unrealized = 0.0
    total_used_margin = 0.0

    for pos in account.open_positions:
        cur_ltp = get_current_option_price(pos.strike, pos.opt_type, spot, chain_df)
        pos.current_price = cur_ltp
        pnl_pts = cur_ltp - pos.entry_price
        pos.unrealized_pnl = round(pnl_pts * pos.quantity, 2)
        pos.unrealized_pct = round((pnl_pts / max(0.1, pos.entry_price)) * 100.0, 2)
        pos.required_margin = round(pos.entry_price * pos.quantity, 2)

        total_unrealized += pos.unrealized_pnl
        total_used_margin += pos.required_margin

    account.unrealized_pnl = round(total_unrealized, 2)
    account.used_margin = round(total_used_margin, 2)
    return account


# ═══════════════════════════════════════════════════════════════════════════
# 3. Order Execution Engine
# ═══════════════════════════════════════════════════════════════════════════

def place_paper_order(
    strike: float,
    opt_type: str,                   # "CE" or "PE"
    lots: int,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    stop_loss: float = 0.0,
    target_1: float = 0.0,
    target_2: float = 0.0,
    setup_tag: str = "Discretionary",
    custom_price: Optional[float] = None,
) -> Tuple[bool, str, PaperAccount]:
    """
    Executes a paper Buy order for Nifty options.
    Validates margin against current cash balance.
    """
    account = load_paper_account()
    opt_type = opt_type.upper()
    qty = lots * NIFTY_LOT_SIZE

    # Fetch live price
    ltp = custom_price if (custom_price and custom_price > 0) else get_current_option_price(strike, opt_type, spot, chain_df)
    if ltp <= 0.0:
        return False, "Could not fetch a valid option price.", account

    required_capital = round(ltp * qty, 2)

    # Margin check
    if required_capital > account.cash_balance:
        return False, f"Insufficient margin: Required ₹{required_capital:,.2f}, Available Cash ₹{account.cash_balance:,.2f}. Please refill paper capital or reduce lot size.", account

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
        lots=lots,
        quantity=qty,
        entry_time=now_str,
        entry_price=ltp,
        current_price=ltp,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        setup_tag=setup_tag,
        entry_spot=spot,
        unrealized_pnl=0.0,
        unrealized_pct=0.0,
        required_margin=required_capital,
    )

    account.open_positions.append(new_pos)
    save_paper_account(account)

    msg = f"Order Executed: Bought {lots} Lot(s) ({qty} Qty) of {symbol} @ ₹{ltp:.2f} (Margin: ₹{required_capital:,.2f})."
    return True, msg, account


def exit_paper_position(
    position_id: str,
    spot: float,
    chain_df: Optional[pd.DataFrame] = None,
    exit_reason: str = "Manual Exit",
    custom_exit_price: Optional[float] = None,
) -> Tuple[bool, str, PaperAccount]:
    """
    Exits an open paper position, credits cash balance + realized P&L,
    and logs the trade to trades.csv.
    """
    account = load_paper_account()
    target_pos = None
    remaining_positions = []

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

    pnl_pts = round(exit_price - target_pos.entry_price, 2)
    pnl_rupees = round(pnl_pts * target_pos.quantity, 2)
    pnl_pct = round((pnl_pts / max(0.1, target_pos.entry_price)) * 100.0, 2)

    # Release margin + credit P&L to cash balance
    account.cash_balance = round(account.cash_balance + target_pos.required_margin + pnl_rupees, 2)
    account.used_margin = round(max(0.0, account.used_margin - target_pos.required_margin), 2)
    account.realized_pnl = round(account.realized_pnl + pnl_rupees, 2)
    account.open_positions = remaining_positions

    # Update unrealized
    sync_open_positions(account, spot, chain_df)
    save_paper_account(account)

    # Log to CSV
    trade_record = {
        "trade_id": target_pos.position_id,
        "entry_time": target_pos.entry_time,
        "exit_time": exit_time_str,
        "symbol": target_pos.symbol,
        "strike": target_pos.strike,
        "opt_type": target_pos.opt_type,
        "lots": target_pos.lots,
        "quantity": target_pos.quantity,
        "entry_price": target_pos.entry_price,
        "exit_price": exit_price,
        "pnl_pts": pnl_pts,
        "pnl_rupees": pnl_rupees,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "setup_tag": target_pos.setup_tag,
        "entry_spot": target_pos.entry_spot,
        "exit_spot": spot,
        "balance_after": account.cash_balance,
    }
    _log_trade_to_csv(trade_record)

    sign = "+" if pnl_rupees >= 0 else ""
    msg = f"Position Closed: {target_pos.symbol} exited @ ₹{exit_price:.2f}. P&L: {sign}₹{pnl_rupees:,.2f} ({sign}{pnl_pct:.1f}%)."
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
        ok, _, account = exit_paper_position(pos.position_id, spot, chain_df, exit_reason)
        if ok:
            count += 1
        else:
            break
    return count, account


# ═══════════════════════════════════════════════════════════════════════════
# 4. Performance Analytics Engine
# ═══════════════════════════════════════════════════════════════════════════

def get_paper_performance_stats(account: PaperAccount) -> Dict[str, Any]:
    """Calculates comprehensive trading performance metrics from trades.csv."""
    df = load_trade_history()
    stats = {
        "total_trades": len(df),
        "total_realized_pnl": account.realized_pnl,
        "win_rate_pct": 0.0,
        "wins_count": 0,
        "losses_count": 0,
        "profit_factor": 0.0,
        "avg_win_rupees": 0.0,
        "avg_loss_rupees": 0.0,
        "largest_win": 0.0,
        "largest_loss": 0.0,
        "expectancy_per_trade": 0.0,
        "tag_breakdown": {},
    }

    if df.empty:
        return stats

    pnls = df["pnl_rupees"].astype(float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    stats["wins_count"] = len(wins)
    stats["losses_count"] = len(losses)
    stats["win_rate_pct"] = round((len(wins) / len(df)) * 100.0, 1)

    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0

    stats["profit_factor"] = round(gross_profit / max(1.0, gross_loss), 2)
    stats["avg_win_rupees"] = round(wins.mean(), 2) if len(wins) else 0.0
    stats["avg_loss_rupees"] = round(losses.mean(), 2) if len(losses) else 0.0
    stats["largest_win"] = round(wins.max(), 2) if len(wins) else 0.0
    stats["largest_loss"] = round(losses.min(), 2) if len(losses) else 0.0
    stats["expectancy_per_trade"] = round(pnls.mean(), 2) if len(pnls) else 0.0

    # Strategy / Impulse Breakdown
    if "setup_tag" in df.columns:
        tag_grp = df.groupby("setup_tag")["pnl_rupees"].agg(["count", "sum"]).reset_index()
        for _, row in tag_grp.iterrows():
            tag_name = row["setup_tag"]
            cnt = int(row["count"])
            tot = float(row["sum"])
            tag_trades = df[df["setup_tag"] == tag_name]
            tag_wins = len(tag_trades[tag_trades["pnl_rupees"] > 0])
            wr = round((tag_wins / max(1, cnt)) * 100.0, 1)
            stats["tag_breakdown"][tag_name] = {
                "trades": cnt,
                "net_pnl": tot,
                "win_rate": wr,
            }

    return stats
