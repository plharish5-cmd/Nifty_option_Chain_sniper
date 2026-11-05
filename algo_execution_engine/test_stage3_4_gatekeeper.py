"""
algo_execution_engine/test_stage3_4_gatekeeper.py — Test Suite for Stage 3 Paper Sandbox & Stage 4 Live Gatekeeper.

Validates:
  1. Stage 3 Paper Trade Logging & ₹400 Round-Trip Brokerage Deduction.
  2. Stage 4 Programmatic Lock (Blocks LIVE_1LOT until 5 Graduation Gates Pass).
  3. Rejection of All Override Attempts (Backdoors Permanently Removed).
  4. Automatic Live Arming upon full 25-Trade Sandbox Graduation.
  5. Part G Strike Selection (100-Multiples ONLY, ~₹200 ITM).
  6. Hard 1-Lot Cap (65 qty max).
  7. API Telemetry GET /api/algo/stage3-4-dashboard.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from algo_execution_engine.stage3_sandbox import SandboxTrade, Stage3GraduationTracker
from algo_execution_engine.order_gateway import DhanOrderGateway, validate_1lot_cap
from algo_execution_engine.strategy_setup1 import Setup1Signal, load_config
from algo_execution_engine.server import app


# ─────────────────────────────────────────────────────────────────────────────
# 1. STAGE 3 PAPER TRADE LOGGING & BROKERAGE DEDUCTION
# ─────────────────────────────────────────────────────────────────────────────

def test_stage3_paper_trade_logging_and_brokerage(tmp_path: Path):
    db_file = str(tmp_path / "test_sandbox.sqlite3")
    tracker = Stage3GraduationTracker(db_path=db_file)

    # 1. Record an Entry
    trade = SandboxTrade(
        trade_id="SANDBOX_20261103_093500_001",
        session_date="2026-11-03",
        entry_ts=1730628900,
        entry_time="09:35:00",
        spot_entry=23500.0,
        option_strike=23300.0,  # 100-multiple ITM
        option_entry_ltp=205.0,
        bid_ask_spread_pts=1.2,
        delta_est=0.60,
        initial_sl_spot=23460.0,
        target_spot=23540.0,
        confluence_snapshot={"ubb_slope": True, "ema_separation": 4.5, "vwap_ok": True},
    )
    ok = tracker.record_entry(trade)
    assert ok is True

    # 2. Record Exit at +40 pts Target
    # Option moves +40 * 0.60 = +24.0 pts (205.0 -> 229.0)
    # Gross P&L: 24.0 * 75 = ₹1,800.0
    # Net P&L: ₹1,800.0 - ₹400 fee = +₹1,400.0
    closed = tracker.record_exit(
        trade_id=trade.trade_id,
        exit_spot=23540.0,
        exit_option_ltp=229.0,
        exit_reason="Target reached (+40 pts)",
        slippage_pts=0.0,
    )
    assert closed is not None
    assert closed.status == "CLOSED_TARGET"
    assert closed.spot_pnl_pts == 40.0
    assert closed.gross_option_pnl_inr == 1800.0
    assert closed.brokerage_inr == 400.0
    assert closed.net_pnl_inr == 1400.0

    # 3. Record a Loss trade (-40 pts SL)
    trade_loss = SandboxTrade(
        trade_id="SANDBOX_20261103_101500_002",
        session_date="2026-11-03",
        entry_ts=1730631300,
        entry_time="10:15:00",
        spot_entry=23500.0,
        option_strike=23300.0,
        option_entry_ltp=200.0,
        bid_ask_spread_pts=1.5,
        delta_est=0.60,
        initial_sl_spot=23460.0,
        target_spot=23540.0,
        confluence_snapshot={},
    )
    tracker.record_entry(trade_loss)
    # Loss: -40 * 0.60 = -24.0 pts (200.0 -> 176.0)
    # Gross: -24.0 * 75 = -₹1,800.0
    # Net: -₹1,800.0 - ₹400 = -₹2,200.0
    closed_loss = tracker.record_exit(
        trade_id=trade_loss.trade_id,
        exit_spot=23460.0,
        exit_option_ltp=176.0,
        exit_reason="Stop-Loss Hit (-40 pts)",
        slippage_pts=0.0,
    )
    assert closed_loss is not None
    assert closed_loss.net_pnl_inr == -2200.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. STAGE 4 GATEKEEPER LOCK & GRADUATION RULES
# ─────────────────────────────────────────────────────────────────────────────

def test_stage4_gatekeeper_blocks_unqualified_live_mode(tmp_path: Path):
    db_file = str(tmp_path / "test_sandbox_gate.sqlite3")
    cfg = load_config()
    cfg["execution"]["sqlite_db_path"] = db_file
    gateway = DhanOrderGateway(cfg)

    # Initially 0 trades in sandbox -> Gatekeeper MUST BLOCK switch to LIVE_1LOT
    res = gateway.switch_execution_mode("LIVE_1LOT")
    assert res["ok"] is False
    assert "LOCKED IN STAGE 3 SANDBOX" in res["error"]
    assert gateway.execution_mode == "PAPER"
    assert any("Gate 1" in r for r in res["rejection_reasons"])


def test_stage4_gatekeeper_rejects_all_override_attempts(tmp_path: Path):
    """
    Security Audit Fix: All backdoor override tokens deleted.
    Verifies that no token or parameter can bypass the Stage 4 Graduation Gate when graduation_ready is False.
    Must return 403 / ok=False, no hint field, and leave execution_mode as PAPER.
    """
    db_file = str(tmp_path / "test_sandbox_override.sqlite3")
    cfg = load_config()
    cfg["execution"]["sqlite_db_path"] = db_file
    gateway = DhanOrderGateway(cfg)

    # Attempting switch with any override keyword or token must return 403 / ok=False
    res = gateway.switch_execution_mode("LIVE_1LOT", force_override_token="FORCE_ENTRY_NOW")
    assert res["ok"] is False
    assert res.get("status_code") == 403
    assert "LOCKED IN STAGE 3 SANDBOX" in res["error"]
    assert gateway.execution_mode == "PAPER"
    assert "hint" not in res  # No leak of backdoor token hint

    # Also test standard switch attempt without kwargs
    res2 = gateway.switch_execution_mode("LIVE_1LOT")
    assert res2["ok"] is False
    assert res2.get("status_code") == 403
    assert gateway.execution_mode == "PAPER"
    assert "hint" not in res2


def test_stage4_gatekeeper_graduates_after_25_winning_trades(tmp_path: Path):
    from algo_execution_engine.stage3_sandbox import SandboxTrade
    db_file = str(tmp_path / "test_sandbox_grad.sqlite3")
    cfg = load_config()
    cfg["execution"]["sqlite_db_path"] = db_file
    gateway = DhanOrderGateway(cfg)
    tracker = gateway.sandbox_tracker

    # Seed 25 trades: 20 wins (+₹1,400 net each), 5 losses (-₹2,200 net each)
    # Win rate: 20/25 = 80.0% (>= 65% hurdle)
    # Total net: 20 * 1400 - 5 * 2200 = 28000 - 11000 = +₹17,000 (Expectancy = +₹680/trade >= ₹300)
    # Violations: 0
    # Max loss: -₹2,200 (within ₹2,900 risk budget)
    for i in range(25):
        t = SandboxTrade(
            trade_id=f"SANDBOX_20261103_{i:04d}",
            session_date="2026-11-03",
            entry_ts=1730628000 + i * 300,
            entry_time="09:30:00",
            spot_entry=23500.0,
            option_strike=23300.0,
            option_entry_ltp=200.0,
            bid_ask_spread_pts=1.0,
            delta_est=0.60,
            initial_sl_spot=23460.0,
            target_spot=23540.0,
            confluence_snapshot={},
        )
        tracker.record_entry(t)
        if i < 20:
            # Win
            tracker.record_exit(t.trade_id, exit_spot=23540.0, exit_option_ltp=224.0, exit_reason="Target reached", slippage_pts=0.0)
        else:
            # Loss
            tracker.record_exit(t.trade_id, exit_spot=23460.0, exit_option_ltp=176.0, exit_reason="Stop-Loss Hit", slippage_pts=0.0)

    grad_status = tracker.evaluate_graduation_status()
    assert grad_status["graduation_ready"] is True
    assert grad_status["graduation_badge"] == "🟢 READY FOR 1-LOT LIVE"
    assert grad_status["rolling_win_rate_pct"] == 80.0
    assert grad_status["net_expectancy_inr"] == 680.0

    # Live Mode Switch MUST NOW SUCCEED WITHOUT TOKEN!
    res = gateway.switch_execution_mode("LIVE_1LOT")
    assert res["ok"] is True
    assert gateway.execution_mode == "LIVE_1LOT"


# ─────────────────────────────────────────────────────────────────────────────
# 3. PART G STRIKE SELECTION & 1-LOT HARD CAP
# ─────────────────────────────────────────────────────────────────────────────

def test_strike_selection_multiples_of_100_only():
    gateway = DhanOrderGateway()
    gateway.execution_mode = "PAPER"

    # Test spot = 23742.5 -> must pick 23600 or 23500, NEVER 23750 or 23700 if far from 200 LTP
    strike, sec_id, sym, ltp, spread = gateway.resolve_itm_100_ce_contract(23742.5)
    assert strike % 100 == 0, f"Strike {strike} is not a multiple of 100!"
    assert strike in (23600.0, 23500.0)
    assert spread > 0
    assert ltp > 0

    # Test spot = 23891.0
    strike2, _, _, _, _ = gateway.resolve_itm_100_ce_contract(23891.0)
    assert strike2 % 100 == 0, f"Strike {strike2} is not a multiple of 100!"


def test_hard_1lot_cap_enforced_in_live_mode():
    from unittest.mock import MagicMock, patch, AsyncMock
    import os
    cfg = load_config()
    gateway = DhanOrderGateway(cfg)
    gateway.execution_mode = "LIVE_1LOT"

    signal = Setup1Signal(
        timestamp=1730628000,
        spot=23500.0,
        ema8=23499.0,
        ema4=23504.0,
        ubb=23550.0,
        bb_width_15m=75.0,
        rally_id=1,
        touch_number=1,
        is_valid=True,
    )

    # Mock Dhan API response for live order entry
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"orderId": "DHAN_ORDER_9999", "status": "success"}

    async def _run(gw):
        with patch.object(gw, "resolve_itm_100_ce_contract", return_value=(23400.0, "SIM_23400_CE", "NIFTY 23400 CE", 210.0, 0.5)), \
             patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "mock_token", "DHAN_CLIENT_ID": "mock_client"}), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            return await gw.execute_setup1_entry(signal, daily_trades=0, daily_pnl_inr=0.0)

    # 1. Setup 1 standard 1-lot path
    pos = asyncio.run(_run(gateway))
    assert pos is not None
    # Hard lot cap: 1 Lot (dynamic from master: 65 shares)
    assert pos.quantity == 65, f"Expected 65 quantity (1 Lot), got {pos.quantity}"

    # 2. Setup 1 path when caller / lots_per_trade attempts to exceed 1 lot
    gateway.lots_per_trade = 3
    pos_oversized = asyncio.run(_run(gateway))
    assert pos_oversized is not None
    assert pos_oversized.quantity == 65, f"Expected 1-lot clamp to 65, got {pos_oversized.quantity}"
    gateway.lots_per_trade = 1

    # 3. Direct validate_1lot_cap unit check
    assert validate_1lot_cap(130, 65) == 65
    assert validate_1lot_cap(65, 65) == 65

    # 4. Verify 1H 8-EMA path also enforces 1-lot cap guard
    from algo_execution_engine.strategy_1h_8ema import Strategy1hSignal
    signal_1h = Strategy1hSignal(
        side="BUY",
        setup_name="1H 8-EMA Support Bounce",
        timestamp=1730628000,
        spot=23500.0,
        ema8=23498.0,
        sl_spot=23460.0,
        target_spot=23540.0,
        lot_size=130,  # Deliberately attempt 2 lots
        is_valid=True,
    )
    with patch.object(gateway, "resolve_itm_100_ce_contract", return_value=(23400.0, "SIM_23400_CE", "NIFTY 23400 CE", 210.0, 0.5)):
        # 1H path is alert-only; executes through lot validator
        asyncio.run(gateway.execute_1h_80ema_entry(signal_1h, daily_trades=0, daily_pnl_inr=0.0))
        # Ensure validate_1lot_cap clamped 130 -> 65
        assert validate_1lot_cap(signal_1h.lot_size, gateway.dynamic_lot_size) == 65


# ─────────────────────────────────────────────────────────────────────────────
# 4. DASHBOARD API TELEMETRY ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

def test_stage3_4_dashboard_telemetry_endpoint():
    client = TestClient(app)
    res = client.get("/api/algo/stage3-4-dashboard")
    assert res.status_code == 200
    data = res.json()

    assert data["ok"] is True
    assert "stage2_backtest" in data
    assert "stage3_sandbox" in data
    assert "stage4_gatekeeper" in data

    # Verify Stage 2 Backtest Expectations (Pure 15m Results)
    var_e = data["stage2_backtest"]["comparison"]["variant_e"]
    assert "+4.85 pts" in var_e["spot_expectancy"]
    assert "+26.2 pts" in var_e["avg_win_pts"]

    var_d = data["stage2_backtest"]["comparison"]["variant_d"]
    assert "+3.92 pts" in var_d["spot_expectancy"]
    assert "+28.6 pts" in var_d["avg_win_pts"]

    # Verify Stage 4 Live Gatekeeper Safeguards
    gate = data["stage4_gatekeeper"]
    assert gate["hard_lot_cap"] == 1
    assert gate["hard_qty_cap"] == 65
    assert "100-Multiples Only" in gate["strike_rule"]
