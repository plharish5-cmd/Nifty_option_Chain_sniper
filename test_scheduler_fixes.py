"""
test_scheduler_fixes.py — Tests for scheduler bug fixes:
1. No log.<attr>( pattern exists anywhere in scheduler.py (crashed OHLC error handler).
2. scheduler.py compiles cleanly with py_compile.
3. job_ohlc failure path runs without raising when Dhan/NSE fails.
4. Retention path and RVOL EOD path run without AttributeError or NameError.
5. Retention never selects the most recent N sessions for deletion.
6. EOD row in data/rvol_gate_log.jsonl reflects a non-zero count after a session with suppressed signals.
"""
from __future__ import annotations

import importlib
import json
import py_compile
import re
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import rvol_gate
import scheduler
import store


def test_no_log_attr_in_scheduler():
    """Guard test: Assert no log.<attr>( pattern exists anywhere in scheduler.py."""
    scheduler_path = Path(__file__).resolve().parent / "scheduler.py"
    code = scheduler_path.read_text(encoding="utf-8")

    # Strip line comments so comments mentioning log.warning don't trigger
    code_no_comments = re.sub(r"#.*", "", code)

    # Search for log.<attr>( pattern where attr is an identifier
    # Exclude log.warning definition itself at lines 120-124 (log.warning = ...)
    matches = re.findall(r"\blog\.[a-zA-Z_]\w*\s*\(", code_no_comments)
    assert not matches, f"Found forbidden log.<attr>( call(s) in scheduler.py: {matches}"


def test_scheduler_compiles():
    """Guard test: Assert scheduler.py compiles cleanly with py_compile."""
    scheduler_path = Path(__file__).resolve().parent / "scheduler.py"
    py_compile.compile(str(scheduler_path), doraise=True)


def test_job_ohlc_failure_path_runs_without_raising(monkeypatch):
    """
    Simulate Dhan failing with ConnectionResetError (the real-world failure)
    and NSE failing with 503, plus empty local candles.
    job_ohlc must run through its entire fallback chain and return without raising.
    """
    import market_data as md
    import levels

    st = scheduler.load_state()
    st["ohlc_done"] = False
    initial_attempts = st.get("ohlc_attempts", 0)

    # Simulate Dhan failure
    def mock_dhan_fail(*args, **kwargs):
        raise ConnectionResetError(104, "Connection reset by peer")

    # Simulate NSE failure
    def mock_nse_fail(*args, **kwargs):
        raise RuntimeError("NSE 503 Service Unavailable")

    # Simulate empty local candles
    import pandas as pd
    def mock_empty_levels():
        return pd.DataFrame()

    monkeypatch.setattr(md, "backfill_ohlc_dhan", mock_dhan_fail)
    monkeypatch.setattr(md, "backfill_ohlc", mock_nse_fail)
    monkeypatch.setattr(levels, "load_ohlc", mock_empty_levels)

    captured_logs = []
    monkeypatch.setattr(scheduler, "log", lambda msg: captured_logs.append(msg))

    # Must NOT raise any AttributeError or exception (pass force=True to test error path)
    scheduler.job_ohlc(st, force=True)

    assert st["ohlc_attempts"] == initial_attempts + 1
    assert not st.get("ohlc_done", False)

    # Verify real Dhan error was logged and not masked by AttributeError
    dhan_logs = [m for m in captured_logs if "dhan backfill error" in m]
    assert len(dhan_logs) == 1
    assert "Connection reset by peer" in dhan_logs[0]


def test_job_ohlc_at_1200_does_not_change_ohlc(monkeypatch):
    """Running job_ohlc at 12:00 must refuse before 15:30 and not touch OHLC.csv."""
    st = {"ohlc_done": False, "ohlc_attempts": 0}
    ohlc_file = config.OHLC_DIR / config.OHLC_FILE
    content_before = ohlc_file.read_bytes() if ohlc_file.exists() else b""

    class MockDt(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 12, 0, 0)

    monkeypatch.setattr(scheduler, "datetime", MockDt)
    scheduler.job_ohlc(st)

    assert not st.get("ohlc_done", False)
    assert "refused before" in st.get("ohlc_message", "")
    content_after = ohlc_file.read_bytes() if ohlc_file.exists() else b""
    assert content_before == content_after


def test_config_has_root_setting():
    """Verify config.ROOT is a valid setting and points to project root directory."""
    assert hasattr(config, "ROOT")
    assert config.ROOT.exists()
    assert (config.ROOT / "strategy1_rules.json").exists()


def test_startup_retention_check_succeeds():
    """Verify retention check logic reads rules and executes prune_multi_snapshots without error."""
    rules_path = getattr(config, "ROOT", config.DATA_DIR.parent) / "strategy1_rules.json"
    assert rules_path.exists()
    cfg = json.loads(rules_path.read_text(encoding="utf-8"))
    retention = cfg.get("greeks", {}).get("multi_expiry_retention_days", 90)
    assert retention == 90

    # Running prune with dry_run must succeed without raising missing config key
    pruned = store.prune_multi_snapshots(retention, dry_run=True)
    assert isinstance(pruned, list)
    # Oldest data is Aug 2026, within 90 days, so dry-run must find 0 candidates
    assert len(pruned) == 0


def test_startup_retention_failure_does_not_crash_scheduler(monkeypatch):
    """
    Verify that if prune_multi_snapshots fails at startup, scheduler.main() logs WARNING,
    does NOT crash or raise, and completes its execution cleanly without stopping data capture.
    """
    import sys

    def mock_prune_fail(retention, **kwargs):
        raise ValueError("Corrupt multi-expiry snapshot directory")

    monkeypatch.setattr(store, "prune_multi_snapshots", mock_prune_fail)

    captured_logs = []
    monkeypatch.setattr(scheduler, "log", lambda msg: captured_logs.append(msg))
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--once"])

    # Directly run scheduler.main() — must return 0 and NOT crash/raise
    retcode = scheduler.main()
    assert retcode == 0

    # Must log visible warning
    warning_logs = [m for m in captured_logs if "WARNING: startup retention check failed:" in m]
    assert len(warning_logs) == 1
    assert "Corrupt multi-expiry snapshot directory" in warning_logs[0]


def test_retention_and_rvol_eod_path_runs_without_attribute_or_name_error(tmp_path, monkeypatch):
    """Guard test: Retention path and RVOL EOD path both run without AttributeError or NameError."""
    # Test retention execution
    multi_dir = tmp_path / "snapshots_multi"
    multi_dir.mkdir()
    res = store.prune_multi_snapshots(retention_days=90, dry_run=True, base_dir=multi_dir)
    assert isinstance(res, list)

    # Test RVOL EOD execution
    log_file = tmp_path / "rvol_gate_log.jsonl"
    monkeypatch.setattr(rvol_gate, "GATE_LOG_PATH", log_file)
    entry = rvol_gate.log_session_gate_decision(
        target_date="2026-09-23",
        signals_suppressed=12,
        signals_allowed=3,
    )
    assert isinstance(entry, dict)
    assert entry["signals_suppressed"] == 12
    assert entry["signals_allowed"] == 3
    assert log_file.exists()


def test_retention_never_selects_recent_n_sessions(tmp_path):
    """Guard test: Retention never selects the most recent N sessions for deletion."""
    multi_dir = tmp_path / "snapshots_multi"
    multi_dir.mkdir()

    # Create 25 session dates
    for d in range(1, 26):
        (multi_dir / f"2026-01-{d:02d}").mkdir()

    # Even with retention_days=0, min_keep_sessions=15 MUST protect the 15 most recent
    candidates = store.prune_multi_snapshots(retention_days=0, dry_run=True, base_dir=multi_dir, min_keep_sessions=15)
    # Total 25, keep 15 -> exactly 10 oldest (days 01 to 10) selected
    assert len(candidates) == 10
    assert candidates == [f"2026-01-{d:02d}" for d in range(1, 11)]

    # Most recent 15 (days 11 to 25) must NEVER be in candidates
    for d in range(11, 26):
        assert f"2026-01-{d:02d}" not in candidates


def test_eod_row_reflects_nonzero_count_after_suppressed_signals(tmp_path, monkeypatch):
    """Guard test: EOD row in rvol_gate_log.jsonl reflects a non-zero count after a session with suppressed signals."""
    log_file = tmp_path / "rvol_gate_log.jsonl"
    monkeypatch.setattr(rvol_gate, "GATE_LOG_PATH", log_file)

    st = scheduler.load_state()
    st["rvol_signals_suppressed"] = 42
    st["rvol_signals_allowed"] = 8

    # Simulate EOD call as job_ohlc does
    rvol_gate.log_session_gate_decision(
        date(2026, 9, 23),
        signals_suppressed=st.get("rvol_signals_suppressed", 0),
        signals_allowed=st.get("rvol_signals_allowed", 0),
    )

    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["date"] == "2026-09-23"
    assert row["signals_suppressed"] == 42
    assert row["signals_allowed"] == 8


def test_rvol_gate_log_schema_and_backfilled_flag(tmp_path, monkeypatch):
    """Guard test: rvol_gate_log schema includes comment, handles null counters, and tracks counters_backfilled."""
    log_file = tmp_path / "rvol_gate_log.jsonl"
    monkeypatch.setattr(rvol_gate, "GATE_LOG_PATH", log_file)

    # 1. Null counters with counters_backfilled=False
    entry_null = rvol_gate.log_session_gate_decision(
        target_date="2026-09-22",
        signals_suppressed=None,
        signals_allowed=None,
        counters_backfilled=False,
    )
    assert entry_null["signals_suppressed"] is None
    assert entry_null["signals_allowed"] is None
    assert entry_null["counters_backfilled"] is False
    assert "signal-minutes" in entry_null["comment"]
    assert "per-poll counts" in entry_null["comment"].lower()

    # 2. Backfilled counters with counters_backfilled=True
    entry_bf = rvol_gate.log_session_gate_decision(
        target_date="2026-09-18",
        signals_suppressed=4,
        signals_allowed=3,
        counters_backfilled=True,
    )
    assert entry_bf["signals_suppressed"] == 4
    assert entry_bf["signals_allowed"] == 3
    assert entry_bf["counters_backfilled"] is True
    assert "signal-minutes" in entry_bf["comment"]


def test_production_rvol_gate_log_integrity():
    """Verify data/rvol_gate_log.jsonl resets 21, 22, 23 Sep to null, labels estimated data, and includes schema comments."""
    log_file = Path("data/rvol_gate_log.jsonl")
    assert log_file.exists(), "data/rvol_gate_log.jsonl must exist"

    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_date = {row["date"]: row for row in lines}

    assert "2026-09-18" in by_date
    assert "2026-09-21" in by_date
    assert "2026-09-22" in by_date
    assert "2026-09-23" in by_date

    # 2026-09-18 is labelled as backfilled/estimated
    assert by_date["2026-09-18"]["counters_backfilled"] is True

    # 21, 22, 23 Sep rows are reset to null (not 0) with counters_backfilled=False
    for d in ("2026-09-21", "2026-09-22", "2026-09-23"):
        row = by_date[d]
        assert row["signals_suppressed"] is None, f"{d} signals_suppressed must be null"
        assert row["signals_allowed"] is None, f"{d} signals_allowed must be null"
        assert row["counters_backfilled"] is False, f"{d} counters_backfilled must be False"
        assert "comment" in row, f"{d} must have a comment field"
        assert "signal-minutes" in row["comment"], f"{d} comment must explain signal-minutes / per-poll"


