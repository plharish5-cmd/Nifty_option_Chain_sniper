"""
algo_execution_engine/stage3_sandbox.py — Stage 3: 30-Trade Forward Paper-Trading Sandbox & Graduation Tracker.

Implements Trading Bible Part L Graduation Rules:
  - Gate 1: Minimum 25-30 sequential completed paper trades logged under live conditions.
  - Gate 2: Rolling Win Rate >= 65.0% over completed trades (strictly > 59.0% break-even).
  - Gate 3: Cumulative Net P&L > ₹0 and Expectancy >= +₹300/trade (after ₹400/trade brokerage).
  - Gate 4: Zero rule violations (0 Second-Opportunity violations, 0 Daily-Stop violations).
  - Gate 5: Max drawdown within limits (no single trade loss exceeding -₹2,900).
  - Graduation Gatekeeper: Determines if system is authorized for Stage 4 Live 1-Lot execution.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("algo.sandbox")


@dataclass
class SandboxTrade:
    trade_id: str
    session_date: str
    entry_ts: int
    entry_time: str
    spot_entry: float
    option_strike: float  # Multiples of 100 only
    option_entry_ltp: float  # ~₹200 ITM
    bid_ask_spread_pts: float
    delta_est: float
    initial_sl_spot: float
    target_spot: float
    confluence_snapshot: Dict[str, Any]
    exit_ts: Optional[int] = None
    exit_time: Optional[str] = None
    exit_spot: Optional[float] = None
    exit_option_ltp: Optional[float] = None
    spot_pnl_pts: float = 0.0
    gross_option_pnl_inr: float = 0.0
    brokerage_inr: float = 400.0
    slippage_inr: float = 0.0
    net_pnl_inr: float = 0.0
    exit_reason: Optional[str] = None
    status: str = "OPEN"  # "OPEN" | "CLOSED_TARGET" | "CLOSED_SL" | "CLOSED_CTC" | "CLOSED_VOLATILITY" | "CLOSED_MANUAL"
    is_violation: int = 0
    violation_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Stage3GraduationTracker:
    """Manages persistent SQLite sandbox tables and computes the 5 Graduation Gates."""

    def __init__(self, db_path: str = "ict_smc_app/backend/data/ict_smc.sqlite3") -> None:
        full_path = Path(db_path)
        if not full_path.is_absolute():
            full_path = Path(__file__).resolve().parent.parent / db_path
        self.db_path = str(full_path)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_tables(self) -> None:
        """Initializes sandbox tables if they do not exist."""
        try:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS algo_setup1_sandbox_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT UNIQUE NOT NULL,
                        session_date TEXT NOT NULL,
                        entry_ts INTEGER NOT NULL,
                        entry_time TEXT NOT NULL,
                        spot_entry REAL NOT NULL,
                        option_strike REAL NOT NULL,
                        option_entry_ltp REAL NOT NULL,
                        bid_ask_spread_pts REAL NOT NULL,
                        delta_est REAL NOT NULL,
                        initial_sl_spot REAL NOT NULL,
                        target_spot REAL NOT NULL,
                        confluence_snapshot TEXT,
                        exit_ts INTEGER,
                        exit_time TEXT,
                        exit_spot REAL,
                        exit_option_ltp REAL,
                        spot_pnl_pts REAL DEFAULT 0.0,
                        gross_option_pnl_inr REAL DEFAULT 0.0,
                        brokerage_inr REAL DEFAULT 400.0,
                        slippage_inr REAL DEFAULT 0.0,
                        net_pnl_inr REAL DEFAULT 0.0,
                        exit_reason TEXT,
                        status TEXT DEFAULT 'OPEN',
                        is_violation INTEGER DEFAULT 0,
                        violation_reason TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS algo_setup1_daily_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_date TEXT UNIQUE NOT NULL,
                        trades_count INTEGER DEFAULT 0,
                        wins_count INTEGER DEFAULT 0,
                        losses_count INTEGER DEFAULT 0,
                        gross_pnl_inr REAL DEFAULT 0.0,
                        brokerage_inr REAL DEFAULT 0.0,
                        net_pnl_inr REAL DEFAULT 0.0,
                        max_drawdown_inr REAL DEFAULT 0.0,
                        stage2_stopped_hit INTEGER DEFAULT 0,
                        rule_violations_count INTEGER DEFAULT 0
                    );
                """)
                conn.commit()
                log.info("Initialized Stage 3 Sandbox tables in SQLite: %s", self.db_path)
        except Exception as e:
            log.error("Failed to initialize sandbox tables: %s", e)

    def record_entry(self, trade: SandboxTrade) -> bool:
        """Records a new paper trade entry into algo_setup1_sandbox_trades."""
        try:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO algo_setup1_sandbox_trades (
                        trade_id, session_date, entry_ts, entry_time, spot_entry,
                        option_strike, option_entry_ltp, bid_ask_spread_pts, delta_est,
                        initial_sl_spot, target_spot, confluence_snapshot, status,
                        is_violation, violation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade.trade_id,
                    trade.session_date,
                    trade.entry_ts,
                    trade.entry_time,
                    trade.spot_entry,
                    trade.option_strike,
                    trade.option_entry_ltp,
                    trade.bid_ask_spread_pts,
                    trade.delta_est,
                    trade.initial_sl_spot,
                    trade.target_spot,
                    json.dumps(trade.confluence_snapshot),
                    trade.status,
                    trade.is_violation,
                    trade.violation_reason,
                ))
                conn.commit()
                log.info("📝 Logged Sandbox Trade Entry: %s (Strike %.0f CE @ ₹%.1f)",
                         trade.trade_id, trade.option_strike, trade.option_entry_ltp)
                return True
        except Exception as e:
            log.error("Failed to record sandbox entry: %s", e)
            return False

    def record_exit(
        self,
        trade_id: str,
        exit_spot: float,
        exit_option_ltp: float,
        exit_reason: str,
        slippage_pts: float = 0.5,
    ) -> Optional[SandboxTrade]:
        """Records exit for an open sandbox trade and calculates final net P&L after ₹400 brokerage & slippage."""
        try:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM algo_setup1_sandbox_trades WHERE trade_id = ?", (trade_id,))
                row = cur.fetchone()
                if not row:
                    log.error("Sandbox trade %s not found for exit", trade_id)
                    return None

                col_names = [d[0] for d in cur.description]
                r_dict = dict(zip(col_names, row))

                spot_entry = float(r_dict["spot_entry"])
                opt_entry = float(r_dict["option_entry_ltp"])
                qty = 75  # 1 lot

                spot_pnl = round(exit_spot - spot_entry, 2)
                opt_pnl_pts = round(exit_option_ltp - opt_entry - slippage_pts, 2)
                gross_inr = round(opt_pnl_pts * qty, 2)
                brokerage = 400.0
                slippage_inr = round(slippage_pts * qty, 2)
                net_inr = round(gross_inr - brokerage, 2)

                now_ts = int(time.time())
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                status = "CLOSED_TARGET" if "Target" in exit_reason else (
                    "CLOSED_SL" if "Stop-Loss" in exit_reason else (
                        "CLOSED_CTC" if "Cost-to-Cost" in exit_reason else "CLOSED_VOLATILITY"
                    )
                )

                cur.execute("""
                    UPDATE algo_setup1_sandbox_trades SET
                        exit_ts = ?,
                        exit_time = ?,
                        exit_spot = ?,
                        exit_option_ltp = ?,
                        spot_pnl_pts = ?,
                        gross_option_pnl_inr = ?,
                        brokerage_inr = ?,
                        slippage_inr = ?,
                        net_pnl_inr = ?,
                        exit_reason = ?,
                        status = ?
                    WHERE trade_id = ?
                """, (
                    now_ts, now_str, exit_spot, exit_option_ltp,
                    spot_pnl, gross_inr, brokerage, slippage_inr,
                    net_inr, exit_reason, status, trade_id
                ))
                conn.commit()
                log.info("🏁 Closed Sandbox Trade %s (%s): Net ₹%+.1f (after ₹400 brokerage)",
                         trade_id, status, net_inr)

                return SandboxTrade(
                    trade_id=trade_id,
                    session_date=r_dict["session_date"],
                    entry_ts=r_dict["entry_ts"],
                    entry_time=r_dict["entry_time"],
                    spot_entry=spot_entry,
                    option_strike=float(r_dict["option_strike"]),
                    option_entry_ltp=opt_entry,
                    bid_ask_spread_pts=float(r_dict["bid_ask_spread_pts"]),
                    delta_est=float(r_dict["delta_est"]),
                    initial_sl_spot=float(r_dict["initial_sl_spot"]),
                    target_spot=float(r_dict["target_spot"]),
                    confluence_snapshot=json.loads(r_dict["confluence_snapshot"] or "{}"),
                    exit_ts=now_ts,
                    exit_time=now_str,
                    exit_spot=exit_spot,
                    exit_option_ltp=exit_option_ltp,
                    spot_pnl_pts=spot_pnl,
                    gross_option_pnl_inr=gross_inr,
                    brokerage_inr=brokerage,
                    slippage_inr=slippage_inr,
                    net_pnl_inr=net_inr,
                    exit_reason=exit_reason,
                    status=status,
                    is_violation=int(r_dict["is_violation"]),
                    violation_reason=r_dict["violation_reason"],
                )
        except Exception as e:
            log.error("Failed to record sandbox exit: %s", e)
            return None

    def get_completed_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns completed sandbox trades ordered by exit_ts DESC."""
        try:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT * FROM algo_setup1_sandbox_trades
                    WHERE status != 'OPEN'
                    ORDER BY exit_ts DESC
                    LIMIT ?
                """, (limit,))
                col_names = [d[0] for d in cur.description]
                return [dict(zip(col_names, row)) for row in cur.fetchall()]
        except Exception as e:
            log.error("Failed to get completed sandbox trades: %s", e)
            return []

    def evaluate_graduation_status(self) -> Dict[str, Any]:
        """
        Evaluates the rolling paper trade history against the 5 Mandatory Graduation Gates:
          - Gate 1: At least 25 to 30 sequential completed paper trades logged.
          - Gate 2: Rolling Win Rate >= 65.0% (and strictly > 59.0% break-even).
          - Gate 3: Cumulative Net P&L > ₹0 and Expectancy >= +₹300/trade (after ₹400 brokerage).
          - Gate 4: Zero rule violations (Second-Opportunity, Daily-Stop).
          - Gate 5: Max drawdown within limits (no single loss exceeding -₹2,900).
        """
        trades = self.get_completed_trades(limit=100)
        n = len(trades)

        if n == 0:
            return {
                "graduation_ready": False,
                "graduation_badge": "🔒 LOCKED IN SANDBOX (0/30 TRADES)",
                "completed_trades": 0,
                "target_trades": 30,
                "rolling_win_rate_pct": 0.0,
                "cumulative_net_pnl_inr": 0.0,
                "net_expectancy_inr": 0.0,
                "violations_count": 0,
                "max_single_loss_inr": 0.0,
                "gates": {
                    "gate_1_min_trades": {"pass": False, "detail": "0 / 25 trades completed"},
                    "gate_2_win_rate": {"pass": False, "detail": "0.0% (target >= 65.0%)"},
                    "gate_3_net_expectancy": {"pass": False, "detail": "₹0.0 / trade (target >= +₹300.0)"},
                    "gate_4_zero_rule_violations": {"pass": True, "detail": "0 violations"},
                    "gate_5_max_drawdown_ok": {"pass": True, "detail": "Max loss: ₹0.0 (limit <= ₹2,900)"},
                },
                "rejection_reasons": ["Gate 1: Minimum 25 completed paper trades required before live evaluation"],
            }

        recent = trades[:30]
        n_recent = len(recent)

        wins = [t for t in recent if float(t.get("net_pnl_inr", 0)) > 0]
        losses = [t for t in recent if float(t.get("net_pnl_inr", 0)) < 0]
        win_rate = (len(wins) / n_recent * 100.0) if n_recent > 0 else 0.0

        net_pnls = [float(t.get("net_pnl_inr", 0)) for t in recent]
        cum_net_pnl = sum(net_pnls)
        expectancy = cum_net_pnl / n_recent if n_recent > 0 else 0.0

        violations = sum(int(t.get("is_violation", 0)) for t in recent)
        max_loss = min(net_pnls) if net_pnls else 0.0  # negative number

        # Evaluate Gates
        gate_1 = bool(n >= 25)
        gate_2 = bool(win_rate >= 65.0)
        gate_3 = bool(cum_net_pnl > 0 and expectancy >= 300.0)
        gate_4 = bool(violations == 0)
        gate_5 = bool(max_loss >= -2900.0)  # no single loss worse than -₹2,900

        all_passed = gate_1 and gate_2 and gate_3 and gate_4 and gate_5

        rejection_reasons = []
        if not gate_1:
            rejection_reasons.append(f"Gate 1: Only {n}/25 minimum sequential paper trades completed")
        if not gate_2:
            rejection_reasons.append(f"Gate 2: Rolling Win Rate {win_rate:.1f}% < 65.0% graduation hurdle")
        if not gate_3:
            rejection_reasons.append(f"Gate 3: Net Expectancy ₹{expectancy:+,.1f} < +₹300.0 / trade after brokerage")
        if not gate_4:
            rejection_reasons.append(f"Gate 4: {violations} rule violation(s) detected in paper trading log")
        if not gate_5:
            rejection_reasons.append(f"Gate 5: Single trade loss ₹{abs(max_loss):,.1f} exceeded risk budget of ₹2,900")

        badge = "🟢 READY FOR 1-LOT LIVE" if all_passed else f"🔒 LOCKED IN SANDBOX ({n}/30 TRADES)"

        return {
            "graduation_ready": all_passed,
            "graduation_badge": badge,
            "completed_trades": n,
            "target_trades": 30,
            "rolling_win_rate_pct": round(win_rate, 1),
            "cumulative_net_pnl_inr": round(cum_net_pnl, 2),
            "net_expectancy_inr": round(expectancy, 2),
            "violations_count": violations,
            "max_single_loss_inr": round(max_loss, 2),
            "gates": {
                "gate_1_min_trades": {"pass": gate_1, "detail": f"{n} / 25 trades completed"},
                "gate_2_win_rate": {"pass": gate_2, "detail": f"{win_rate:.1f}% (target >= 65.0%)"},
                "gate_3_net_expectancy": {"pass": gate_3, "detail": f"₹{expectancy:+,.1f} / trade (target >= +₹300.0)"},
                "gate_4_zero_rule_violations": {"pass": gate_4, "detail": f"{violations} violations"},
                "gate_5_max_drawdown_ok": {"pass": gate_5, "detail": f"Max single loss: ₹{abs(max_loss):,.1f} (limit <= ₹2,900)"},
            },
            "rejection_reasons": rejection_reasons,
        }
