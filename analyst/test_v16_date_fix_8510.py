#!/usr/bin/env python3
"""
analyst/test_v16_date_fix_8510.py

Comprehensive Unit Test Suite for Port 8510 v1.6 Fix:
  - 05 Nov 2026 Single Source of Truth
  - Replay Dropdown Dynamic
  - Auto Ingest After 16:00 IST
  - Tab 7 Daily Data Ingest API (8 checklist items)
  - Backtester 05 Nov Inclusion & Live Paper Reconciliation
  - Multi-Factor Trend Lab 05 Nov Integration
  - Parity & Guardrails (Lot size 65, EOD Square-off 15:20, PAPER mode)
  - Automated Post-Market EOD Summary & WhatsApp Digest
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from starlette.testclient import TestClient

from engine_8510_live import app, backend
from analyst.terminal8510_backend import REPLAY_DATA_DIR


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_1_replay_dropdown_dynamic(client):
    """TEST 1: Replay dropdown dynamic population & 05 Nov trade detail."""
    dates = backend.get_available_dates()
    assert "2026-11-05" in dates, "2026-11-05 must be present in available dates."
    assert "2026-11-04" in dates, "2026-11-04 must be present in available dates."
    assert dates.index("2026-11-05") < dates.index("2026-11-04"), "2026-11-05 must be ordered before 2026-11-04."

    # API endpoint GET /api/replay/dates
    res = client.get("/api/replay/dates")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert data.get("latest") == "2026-11-05"
    assert "2026-11-05" in data.get("dates", [])

    # API endpoint GET /api/replay/trades?date=2026-11-05
    res_trades = client.get("/api/replay/trades?date=2026-11-05")
    assert res_trades.status_code == 200
    t_data = res_trades.json()
    assert t_data.get("ok") is True
    trades = t_data.get("trades", [])
    assert len(trades) >= 1, "05 Nov must return at least 1 trade."
    t0 = trades[0]
    assert "Stage 2 8" in (t0.get("setup") or t0.get("strategy_setup") or "")
    net_pnl = t0.get("net_pnl") if t0.get("net_pnl") is not None else t0.get("Net_Pnl_INR")
    assert net_pnl == 1625.0, f"Expected Net P&L 1625.0, got {net_pnl}"
    source = t0.get("source") or t0.get("Source")
    assert source == "AUTO ALGO 🤖", f"Expected AUTO ALGO 🤖, got {source}"
    sym = t0.get("contract_symbol") or t0.get("Contract_Symbol")
    assert sym == "09 Nov 2026 — NIFTY 24200 CE", f"Expected 09 Nov 2026 — NIFTY 24200 CE, got {sym}"


def test_2_auto_ingest_after_1600_ist():
    """TEST 2: Auto ingest after 16:00 IST persists JSON and SQLite daily_market_store."""
    target_date = "2026-11-05"
    json_path = REPLAY_DATA_DIR / f"{target_date}.json"

    # Remove existing JSON to test clean auto-ingest generation
    if json_path.exists():
        json_path.unlink()

    # Trigger auto ingest simulating post-market hour (17:00 IST)
    res = backend.check_and_run_auto_ingest(target_date=target_date, mock_ist_hour=17)
    assert res is True, f"Auto-ingest returned false: {res}"

    # Verify JSON persisted on disk
    assert json_path.exists(), f"Expected {json_path} to be created."
    with open(json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    assert len(doc.get("spot_candles_1m", [])) == 375, "Expected 375 Spot 1m candles."
    assert len(doc.get("future_candles_1m", [])) == 375, "Expected 375 Future 1m candles."
    assert "eod_summary" in doc, "Expected eod_summary in JSON."

    # Verify SQLite daily_market_store table
    with sqlite3.connect(backend.ledger_db) as conn:
        row = conn.execute("SELECT date, spot_candles_count, future_candles_count FROM daily_market_store WHERE date=?", (target_date,)).fetchone()
        assert row is not None, f"Row for {target_date} missing from daily_market_store."
        assert row[1] == 375
        assert row[2] == 375


def test_3_tab7_pull_todays_data_api(client):
    """TEST 3: Tab 7 'Pull Today's Data' API endpoint and 8-point verification checklist."""
    res = client.post("/api/ingest/pull_date", json={"date": "2026-11-05", "overwrite": True})
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True

    # 8-point checklist verification
    checklist = data.get("checklist", {})
    required_keys = [
        "spot_candles_1m",
        "future_candles_1m",
        "volumes_and_change",
        "option_chain",
        "stockmojo_total_oi",
        "stockmojo_oi_change_day",
        "strategy_signals",
        "paper_trades",
    ]
    for k in required_keys:
        assert k in checklist, f"Checklist item {k} missing from pull_date response."
        assert "✅" in str(checklist[k]), f"Checklist item {k} not marked with ✅: {checklist[k]}"

    # Verify GET /api/ingest/status
    res_status = client.get("/api/ingest/status?date=2026-11-05")
    assert res_status.status_code == 200
    st_data = res_status.json()
    assert st_data.get("ok") is True
    assert len(st_data.get("spot_candles_1m", [])) == 375


def test_4_backtester_includes_05nov_and_reconciles(client):
    """TEST 4: Backtester date-range includes 05 Nov and reconciles with live paper trades."""
    res = client.post("/api/backtest/date_range", json={
        "from_date": "2026-11-04",
        "to_date": "2026-11-05",
        "include_today_paper": True
    })
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True

    trades = data.get("trades", [])
    trades_05 = [t for t in trades if t.get("date") == "2026-11-05"]
    assert len(trades_05) >= 1, "Backtester must include trade for 2026-11-05."
    t0 = trades_05[0]
    assert t0.get("Entry_Time") == "10:15:00"
    assert t0.get("Exit_Time") == "11:00:00"
    assert t0.get("Source") == "AUTO ALGO 🤖"
    assert t0.get("Contract_Symbol") == "09 Nov 2026 — NIFTY 24200 CE"
    assert t0.get("reconciliation_match") is True

    # Reconciliation table
    reconciliation = data.get("reconciliation", [])
    assert len(reconciliation) >= 2, "Expected reconciliation rows for at least 04 Nov and 05 Nov."

    rec_05 = next((r for r in reconciliation if r.get("date") == "2026-11-05"), None)
    assert rec_05 is not None, "2026-11-05 missing from reconciliation list."
    assert rec_05.get("live_trades_taken") == 1
    assert rec_05.get("backtest_trades_found") == 1
    assert rec_05.get("match") is True

    rec_04 = next((r for r in reconciliation if r.get("date") == "2026-11-04"), None)
    assert rec_04 is not None, "2026-11-04 missing from reconciliation list."
    assert rec_04.get("match") is True

    assert data.get("reconciliation_match") is True


def test_5_multifactor_trend_lab_includes_05nov(client):
    """TEST 5: Multi-Factor Trend Lab includes 05 Nov with dynamic Stock Mojo biases."""
    res = client.get("/api/trend_lab/run?from_date=2026-11-04&to_date=2026-11-05")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True

    evaluations = data.get("evaluations", [])
    assert len(evaluations) >= 2, "Expected at least 2 rows for 04 Nov and 05 Nov."

    eval_05 = next((e for e in evaluations if e.get("date") == "2026-11-05"), None)
    assert eval_05 is not None, "2026-11-05 missing from Trend Lab evaluations."
    assert "BULLISH" in eval_05.get("stage2", "")
    assert "BULLISH" in eval_05.get("crossover_4_8", "")
    assert "SUPPORT" in eval_05.get("ema_1h_8", "")
    assert "BULLISH" in eval_05.get("total_oi_bias", "")
    assert "BULLISH" in eval_05.get("oi_change_bias", "")
    assert "STRONG BULLISH" in eval_05.get("combined_bias", "")
    assert eval_05.get("multi_factor_score", 0) >= 80


def test_6_single_source_of_truth_parity_and_guardrails():
    """TEST 6: Single source of truth parity across modules & strict guardrails."""
    # Parity check
    dates_from_replay = backend.get_available_dates()
    dates_from_ingest = [p.stem for p in sorted(REPLAY_DATA_DIR.glob("*.json"), reverse=True)]
    for d in dates_from_ingest:
        assert d in dates_from_replay, f"Date {d} from ingest not visible in get_available_dates()."

    # Dynamic Lot Size Guardrail: must be 65 from instrument master, NEVER 75
    nifty_lot = backend.inst_mgr.get_nifty_lot_size()
    assert nifty_lot == 65, f"Expected NIFTY lot size 65, got {nifty_lot}"

    # EOD Square-off Guardrail: must be 15:20 IST (never 15:25)
    assert "15:20" in backend.eod_square_off_time

    # Execution Mode Guardrail: strictly PAPER
    assert "PAPER" in backend.execution_mode

    # Super Trail SL & Trailing Target Guardrails
    assert backend.super_trail_cfg.get("trail_activation_profit_pts") == 15.0
    assert backend.super_trail_cfg.get("trailing_jump_pts") == 5.0
    assert backend.super_trail_cfg.get("initial_sl_pts") == 20.0
    assert backend.super_trail_cfg.get("initial_target_pts") == 25.0
    assert backend.super_trail_cfg.get("trail_target_step_pts") == 10.0
    assert backend.super_trail_cfg.get("trail_target_pullback_pts") == 5.0


def test_7_automated_post_market_eod_summary_and_whatsapp_digest(client):
    """TEST 7: Automated 16:05 / 17:05 IST Post-Market EOD Summary & WhatsApp Digest."""
    res = client.get("/api/ingest/eod_summary?date=2026-11-05")
    assert res.status_code == 200
    summary = res.json()

    assert summary.get("date") == "2026-11-05"
    assert summary.get("paper_trades_count") == 1
    assert summary.get("net_pnl_inr") == "+₹1,625.00"
    assert "Stage 2" in summary.get("signals_summary", "")
    assert "BULLISH" in summary.get("total_oi_bias", "")
    assert "BULLISH" in summary.get("oi_change_bias", "")
    assert "STRONG BULLISH" in summary.get("combined_oi_bias", "")

    wa = summary.get("whatsapp_text", "")
    assert "PORT 8510 EOD SUMMARY" in wa
    assert "375/375 1m Candles Synced" in wa
    assert "AUTO ALGO" in wa
    assert "+₹1,625.00" in wa and "Net P&L" in wa
    assert "STRONG BULLISH" in wa
