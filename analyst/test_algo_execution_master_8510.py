"""Immutable algo execution master + replay mapping."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from analyst.algo_execution_master_8510 import (
    DATA_SOURCE_IMMUTABLE,
    DATA_SOURCE_SYNTHESIZED,
    append_algo_execution_master,
    ensure_algo_execution_master,
    duration_between,
    fetch_master_trades,
    merge_master_and_synthetic,
    normalize_synthetic_backtest_row,
)
from analyst.terminal8510_backend import QuantTerminalBackend


def test_duration_between_distinct_times():
    d = duration_between("2026-11-09T11:30:00+05:30", "2026-11-09T12:15:00+05:30")
    assert d == "00h 45m 00s"


def test_master_append_only_unique_position(tmp_path: Path):
    db = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(db) as conn:
        ensure_algo_execution_master(conn)
    row = {
        "position_id": "POS_TEST_1",
        "trade_date": "2026-11-09",
        "setup_name": "8 EMA Prototype",
        "direction": "LONG",
        "contract_symbol": "NIFTY 25000 CE",
        "index_entry": 24997.8,
        "index_exit": 24957.8,
        "option_entry": 200.0,
        "option_exit": 176.0,
        "qty": 65,
        "entry_time_ist": "2026-11-09 11:30:00 IST",
        "exit_time_ist": "2026-11-09 11:45:00 IST",
        "gross_pnl_inr": -1560.0,
        "dhan_charges_inr": 78.16,
        "net_pnl_inr": -1638.16,
        "outcome": "SL_HIT",
        "source": "AUTO",
        "provenance": "LIVE_ALGO_PAPER",
    }
    rid1 = append_algo_execution_master(db, row)
    rid2 = append_algo_execution_master(db, row)
    assert rid1
    assert rid2 is None
    trades = fetch_master_trades(db, "2026-11-09", "2026-11-09")
    assert len(trades) == 1
    assert trades[0]["entry_ist"] != trades[0]["exit_ist"]


def test_replay_row_separate_entry_exit_ist():
    backend = QuantTerminalBackend()
    row = {
        "Date": "2026-11-09 11:30",
        "date_only": "2026-11-09",
        "entry_day": "2026-11-09",
        "entry_hm": "11:30",
        "exit_day": "2026-11-09",
        "exit_hm": "11:45",
        "option_entry": 200.0,
        "option_exit": 176.0,
        "gross_pnl_inr": -1560.0,
        "net_pnl": -1638.16,
        "Index_Entry": "24997.80",
        "Index_Exit": "24957.80",
        "Exit_Reason": "SL_HIT",
        "Setup_Name": "8 EMA Prototype (Long)",
        "Contract_Symbol": "NIFTY 25000 CE (Synthesized Strike)",
        "Direction": "LONG",
        "data_source_tag": DATA_SOURCE_IMMUTABLE,
        "price_type": "REAL_ALGO_MASTER",
    }
    t = backend._replay_trade_from_backtest_row(row)
    assert t["entry_ist"] != t["exit_ist"]
    assert t["duration"] == "00h 15m 00s"
    assert t["gross_pnl"] == pytest.approx(-1560.0, abs=0.01)

    row["data_source_tag"] = DATA_SOURCE_SYNTHESIZED
    row["source"] = "SYNTHESIZED"
    row["st_pnl_pts"] = -40.0
    ts = backend._replay_trade_from_backtest_row(row)
    assert ts["option_entry"] is None
    assert ts["gross_pnl"] is None


def test_merge_master_suppresses_synth_same_day():
    master = [{"trade_date": "2026-11-09", "date_only": "2026-11-09", "Setup_Name": "Live"}]
    synth = [
        {
            "date_only": "2026-11-09",
            "st_pnl_pts": 40,
            "source": "SYNTHESIZED",
            "option_entry": 200,
            "Contract_Symbol": "X",
        },
        {
            "date_only": "2026-11-08",
            "st_pnl_pts": -20,
            "source": "SYNTHESIZED",
            "option_entry": 200,
            "Contract_Symbol": "Y",
        },
    ]
    merged = merge_master_and_synthetic(
        [{"date_only": "2026-11-09", "data_source_tag": DATA_SOURCE_IMMUTABLE}],
        synth,
    )
    assert len(merged) == 2
    by_day = {r["date_only"]: r for r in merged}
    assert by_day["2026-11-09"]["data_source_tag"] == DATA_SOURCE_IMMUTABLE
    assert by_day["2026-11-08"]["data_source_tag"] == DATA_SOURCE_SYNTHESIZED
    assert by_day["2026-11-08"].get("option_entry") is None


def test_normalize_synthetic_strips_options():
    row = normalize_synthetic_backtest_row({"st_pnl_pts": 12.5, "option_entry": 99, "Contract_Symbol": "NIFTY (Synthesized Strike)"})
    assert row["option_entry"] is None
    assert row["data_source_tag"] == DATA_SOURCE_SYNTHESIZED
