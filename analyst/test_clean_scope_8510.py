"""Ledger hygiene + desk scope (v2.0.36)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from analyst.desk_scope_8510 import (
    is_test_fixture_text,
    load_desk_scope,
    order_book_row_visible,
    strategy_id_allowed,
)
from analyst.ledger_guard_8510 import assert_not_production_ledger_under_pytest, production_ledger_path
from analyst.offhours_cleanup_8510 import parse_contract_expiry_date, resolve_position_cleanup_reason
from datetime import date, datetime
from zoneinfo import ZoneInfo

from analyst.ledger_execution_hide_8510 import is_execution_hidden

IST = ZoneInfo("Asia/Kolkata")


def test_production_ledger_guard_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_clean_scope_8510.py::test_production_ledger_guard_under_pytest")
    with pytest.raises(AssertionError, match="production ledger"):
        assert_not_production_ledger_under_pytest(production_ledger_path())


def test_fixture_detection():
    assert is_test_fixture_text("Stage 2 Test Fixture")
    assert is_test_fixture_text("", "", "ORDER_Q_GATE_X")
    assert not is_test_fixture_text("8 EMA Prototype long")


def test_desk_scope_blocks_stage2_not_prototype():
    sc = {
        "desk_scope_8510": {
            "stage2_enabled": False,
            "stage3_enabled": False,
            "prototype_enabled": True,
            "reversal_enabled": True,
            "flagship_1h_enabled": True,
        }
    }
    ds = load_desk_scope(sc)
    assert ds["stage2_enabled"] is False
    assert not strategy_id_allowed(sc, "STAGE_2_LONG")
    assert strategy_id_allowed(sc, "EMA8_PROTOTYPE_LONG")
    assert strategy_id_allowed(sc, "CROSSOVER_BULLISH")
    assert strategy_id_allowed(sc, "H1_FLAGSHIP_LONG")


def test_order_book_hides_fixture():
    sc = {"desk_scope_8510": {"stage2_enabled": False}}
    assert not order_book_row_visible(sc, {"strategy_setup": "Stage 2 Test Fixture", "mode": "PAPER_ONLY"})
    assert order_book_row_visible(sc, {"strategy_setup": "8 EMA Prototype", "mode": "PAPER_ONLY"})


def test_order_book_hides_expired_and_provenance():
    sc = {}
    sym = "10 Nov 2025 — NIFTY 11 Nov 2025 24300 CE"
    assert not order_book_row_visible(sc, {"symbol": sym, "mode": "PAPER_ONLY"}, today=date(2026, 11, 18))
    assert order_book_row_visible(sc, {"symbol": sym, "mode": "PAPER_ONLY"}, today=date(2025, 11, 10))
    assert not order_book_row_visible(
        sc,
        {"order_id": "ORD_X", "symbol": "live", "mode": "PAPER_ONLY"},
        hidden_ids={"ORD_X"},
    )


def test_expired_contract_cleanup_reason():
    sym = "11 Nov 2025 — NIFTY 25 Nov 2025 24300.0 CE"
    exp = parse_contract_expiry_date(sym)
    assert exp is not None
    now = datetime(2025, 11, 18, 10, 0, tzinfo=IST)
    reason = resolve_position_cleanup_reason(
        symbol=sym, now=now, market_open=True, eod_square_off_time=datetime.strptime("15:20", "%H:%M").time()
    )
    assert reason == "EXPIRED_CONTRACT_CLEANUP"


def test_clean_script_preview(tmp_path: Path):
    led = tmp_path / "led.sqlite3"
    with sqlite3.connect(led) as conn:
        conn.execute(
            "CREATE TABLE orders (order_id TEXT PRIMARY KEY, mode TEXT, strategy_setup TEXT, symbol TEXT)"
        )
        conn.execute(
            "CREATE TABLE positions (position_id TEXT PRIMARY KEY, mode TEXT, strategy_setup TEXT, symbol TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO orders VALUES ('OD_V15_1','PAPER_ONLY','Stage 2 Test Fixture','10 Nov 2025 — NIFTY 11 Nov 2025 24300 CE')"
        )
        conn.commit()
    cfg = tmp_path / "terminal8510_config.json"
    cfg.write_text(
        json.dumps(
            {
                "ledger_db_path": str(led),
                "scanner": {
                    "desk_scope_8510": {"stage2_enabled": False, "stage3_enabled": False},
                },
            }
        ),
        encoding="utf-8",
    )
    import scripts.clean_paper_ledger_8510 as clean

    old_cfg = clean.CFG
    clean.CFG = cfg
    try:
        rc = clean.main(["--preview"])
        assert rc == 0
    finally:
        clean.CFG = old_cfg


def test_clean_script_hide_not_delete(tmp_path: Path):
    led = tmp_path / "led.sqlite3"
    with sqlite3.connect(led) as conn:
        conn.execute(
            "CREATE TABLE orders (order_id TEXT PRIMARY KEY, mode TEXT, strategy_setup TEXT, symbol TEXT, order_status TEXT, fill_price REAL, qty INTEGER, created_at TEXT, security_id TEXT)"
        )
        conn.execute(
            "INSERT INTO orders VALUES ('OD_V15_1','PAPER_ONLY','Stage 2 Test Fixture','10 Nov 2025 — NIFTY 11 Nov 2025 24300 CE','FILLED',1,25,'2025-11-10','0')"
        )
        conn.execute(
            """
            CREATE TRIGGER protect_orders_delete
            BEFORE DELETE ON orders BEGIN
            SELECT RAISE(ABORT,'Execution history cannot be deleted; hide off-market paper tests instead');
            END
            """
        )
        conn.commit()
    cfg = tmp_path / "terminal8510_config.json"
    cfg.write_text(json.dumps({"ledger_db_path": str(led), "scanner": {}}), encoding="utf-8")
    import scripts.clean_paper_ledger_8510 as clean

    old_cfg = clean.CFG
    clean.CFG = cfg
    try:
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            with sqlite3.connect(led) as conn:
                conn.execute("DELETE FROM orders WHERE order_id='OD_V15_1'")
        assert clean.main(["--hide"]) == 0
        with sqlite3.connect(led) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
            assert is_execution_hidden(conn, "orders", "OD_V15_1")
        assert not order_book_row_visible({}, {"order_id": "OD_V15_1", "strategy_setup": "Stage 2 Test Fixture"}, hidden_ids={"OD_V15_1"})
    finally:
        clean.CFG = old_cfg