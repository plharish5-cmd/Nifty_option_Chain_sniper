"""
test_backfill_observations.py — Unit and integration tests for backfill_observations.py.

Verifies:
- Backfill a date with snapshots creates entries.
- Re-running is idempotent: entry count unchanged.
- Existing hand-written "> Note:" text is preserved.
- A pre-populated log is never shortened by a run.
- Missing snapshot folder exits non-zero and creates no file.
- Telegram send is not called unless --telegram.
- Backup file created before writing.
- Dry-run touches nothing.
- Marker placement in chronological order.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import backfill_observations


def test_missing_snapshot_folder_exits_nonzero(tmp_path: Path, capsys):
    """Missing snapshot folder exits non-zero and creates no file."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    nonexistent_date = date(1999, 1, 1)
    res = backfill_observations.backfill_day(
        target_date=nonexistent_date,
        base_dir=tmp_path,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is None
    log_file = obs_dir / "1999-01-01.md"
    assert not log_file.exists()

    # Also verify CLI returns code 1
    rc = backfill_observations.main(["--date", "1999-01-01"])
    assert rc == 1


def test_empty_snapshot_folder_exits_nonzero(tmp_path: Path):
    """Empty snapshot folder (no parquet files) exits non-zero and creates no file."""
    snap_dir = tmp_path / "snapshots" / "2026-01-01"
    snap_dir.mkdir(parents=True, exist_ok=True)
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    res = backfill_observations.backfill_day(
        target_date=date(2026, 1, 1),
        base_dir=tmp_path,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is None
    log_file = obs_dir / "2026-01-01.md"
    assert not log_file.exists()


def test_backfill_creates_entries(tmp_path: Path):
    """Backfill a date with snapshots creates observation entries."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    target_date = date(2026, 9, 18)
    res = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is not None
    assert res["entries_written"] > 0
    log_file = obs_dir / "2026-09-18.md"
    assert log_file.exists()

    content = log_file.read_text(encoding="utf-8")
    assert "## 09:15" in content or "## 09:18" in content


def test_backfill_idempotent(tmp_path: Path):
    """Re-running backfill is idempotent: entry count unchanged."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    target_date = date(2026, 9, 18)
    rules_ovr = {"observations": {"dir": str(obs_dir)}}

    # First run
    res1 = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        rules_override=rules_ovr,
    )
    assert res1 is not None
    initial_written = res1["entries_written"]
    assert initial_written > 0

    log_file = obs_dir / "2026-09-18.md"
    _, entries1, _, _ = backfill_observations.parse_obs_file(log_file.read_text(encoding="utf-8"))
    count1 = len(entries1)

    # Second run without overwrite
    res2 = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        overwrite=False,
        rules_override=rules_ovr,
    )
    assert res2 is not None
    assert res2["entries_written"] == 0
    assert res2["entries_already_present"] >= count1

    _, entries2, _, _ = backfill_observations.parse_obs_file(log_file.read_text(encoding="utf-8"))
    assert len(entries2) == count1


def test_handwritten_note_preserved(tmp_path: Path):
    """Existing hand-written '> Note:' text is preserved even with overwrite=True."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    target_date = date(2026, 9, 18)
    log_file = obs_dir / "2026-09-18.md"

    custom_note = "> Note: Observed heavy call writing rejection at VWAP!"
    pre_existing_content = f"""# NIFTY Daily Observation Log — 2026-09-18

## 09:15 | Spot 23,335 | NEUTRAL →
- GEX: +₹31Cr SHORT gamma (trending)
- RVOL: 0.99× TRAP ZONE
- IV: 17th pct CHEAP
- Floor: 23,200 PE [87L] | Ceiling: 23,500 CE [90L]
- Flow: Puts flat | Calls flat
- Checklist: 4/6 WAIT (Flow, Timing failing)
- Action: NO TRADE
{custom_note}

---
"""
    log_file.write_text(pre_existing_content, encoding="utf-8")

    # Run with overwrite=True
    res = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        overwrite=True,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is not None

    after_content = log_file.read_text(encoding="utf-8")
    assert custom_note in after_content


def test_prepopulated_log_never_shortened(tmp_path: Path):
    """A pre-populated log is never shortened by a backfill run."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)

    target_date = date(2026, 9, 18)
    log_file = obs_dir / "2026-09-18.md"

    # Pre-populate with 4 entries
    pre_lines = [f"# NIFTY Daily Observation Log — 2026-09-18\n\n"]
    for t_str in ["08:30", "09:00", "09:15", "10:00"]:
        pre_lines.append(
            f"## {t_str} | Spot 23,300 | BULLISH ▲\n- GEX: N/A\n> Note: [chart observation / what price did]\n\n---\n\n"
        )
    log_file.write_text("".join(pre_lines), encoding="utf-8")

    # Run backfill for only 09:15 to 09:18
    res = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is not None

    after_content = log_file.read_text(encoding="utf-8")
    _, entries_after, _, _ = backfill_observations.parse_obs_file(after_content)
    # Must contain original 08:30, 09:00, 10:00 plus newly merged entries
    assert "08:30" in entries_after
    assert "09:00" in entries_after
    assert "10:00" in entries_after
    assert len(entries_after) >= 4


def test_telegram_suppressed_by_default(tmp_path: Path, monkeypatch):
    """Telegram send is not called unless --telegram is explicitly specified."""
    mock_send = MagicMock()
    monkeypatch.setattr(backfill_observations, "send_telegram_alert", mock_send)

    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)
    target_date = date(2026, 9, 18)
    rules_ovr = {"observations": {"dir": str(obs_dir)}}

    # Run with telegram=False (default)
    backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 15),
        interval_seconds=180,
        telegram=False,
        rules_override=rules_ovr,
    )
    assert not mock_send.called

    # Run with telegram=True
    backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 15),
        interval_seconds=180,
        overwrite=True,
        telegram=True,
        rules_override=rules_ovr,
    )
    assert mock_send.called


def test_dry_run_touches_nothing(tmp_path: Path):
    """--dry-run prints what it would write and touches nothing on disk."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)
    target_date = date(2026, 9, 18)

    res = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        dry_run=True,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is not None
    log_file = obs_dir / "2026-09-18.md"
    assert not log_file.exists()
    assert res["backup_file_path"] is None


def test_backup_created_before_writing(tmp_path: Path):
    """Existing file is copied to <date>.md.bak-<timestamp> before writing."""
    obs_dir = tmp_path / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)
    target_date = date(2026, 9, 18)
    log_file = obs_dir / "2026-09-18.md"
    log_file.write_text("# Initial Log\n\n## 08:30 | Spot 23,300\n\n---\n", encoding="utf-8")

    res = backfill_observations.backfill_day(
        target_date=target_date,
        from_time=dtime(9, 15),
        to_time=dtime(9, 18),
        interval_seconds=180,
        rules_override={"observations": {"dir": str(obs_dir)}},
    )
    assert res is not None
    assert res["backup_file_path"] is not None
    bak_path = Path(res["backup_file_path"])
    assert bak_path.exists()
    assert "2026-09-18.md.bak-" in bak_path.name
    assert bak_path.read_text(encoding="utf-8") == "# Initial Log\n\n## 08:30 | Spot 23,300\n\n---\n"


def test_markers_chronological_position():
    """Verify == FREEZE END == and == EOD SUMMARY == stay in chronological order."""
    entries = {
        "10:00": "## 10:00 | Spot 23,300 | NEUTRAL\n---",
        "14:00": "## 14:00 | Spot 23,350 | BULLISH\n---",
        "15:28": "## 15:28 | Spot 23,360 | UNWIND\n---",
    }
    eod_summary = "📋 TODAY'S SUMMARY\nDay range: 23,300 -> 23,360"

    content = backfill_observations.assemble_obs_content(
        date_str="2026-09-18",
        entries=entries,
        freeze_end=True,
        eod_summary=eod_summary,
        freeze_time="13:15",
        eod_time="15:25",
    )

    idx_1000 = content.find("## 10:00")
    idx_freeze = content.find("== FREEZE END ==")
    idx_1400 = content.find("## 14:00")
    idx_eod = content.find("== EOD SUMMARY ==")
    idx_1528 = content.find("## 15:28")

    assert idx_1000 < idx_freeze < idx_1400 < idx_eod < idx_1528


def test_cli_argument_handling(capsys):
    """Test CLI argument validations."""
    # Neither date nor last
    rc1 = backfill_observations.main([])
    assert rc1 == 1

    # Both date and last
    rc2 = backfill_observations.main(["--date", "2026-09-18", "--last", "2"])
    assert rc2 == 1

    # Invalid date
    rc3 = backfill_observations.main(["--date", "2026-99-99"])
    assert rc3 == 1

    # Invalid time
    rc4 = backfill_observations.main(["--date", "2026-09-18", "--from", "25:00"])
    assert rc4 == 1
