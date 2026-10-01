"""
test_floor_ceiling_alerts.py — Unit tests for FLOOR/CEILING APPROACH alerts in scheduler.py
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import alerts
import obs_query
import scheduler


def test_rules_loaded_from_json():
    """Verify strategy1_rules.json includes floor_ceiling_alerts block with all thresholds."""
    rules_path = Path(__file__).parent / "strategy1_rules.json"
    assert rules_path.exists()
    data = json.loads(rules_path.read_text())
    assert "floor_ceiling_alerts" in data
    cfg = data["floor_ceiling_alerts"]
    assert cfg.get("enabled") is True
    assert float(cfg.get("proximity_pts")) == 25.0
    assert float(cfg.get("min_wall_oi_lakhs")) == 100.0
    assert int(cfg.get("cooldown_minutes")) == 30
    assert cfg.get("window_start") == "09:20"
    assert cfg.get("window_end") == "14:45"
    assert cfg.get("freeze_start") == "11:30"
    assert cfg.get("freeze_end") == "13:15"
    assert float(cfg.get("target_offset_pts")) == 50.0
    assert int(cfg.get("lot_size")) == 65


def test_cooldown_tracker_in_alerts():
    """Verify floor_test and ceiling_test are in alerts.COOLDOWN dictionary."""
    assert "floor_test" in alerts.COOLDOWN
    assert "ceiling_test" in alerts.COOLDOWN
    assert alerts.COOLDOWN["floor_test"] == timedelta(minutes=30)
    assert alerts.COOLDOWN["ceiling_test"] == timedelta(minutes=30)


def create_sample_chain() -> pd.DataFrame:
    """
    Creates a sample option chain:
      23,300: PE 196L (301,538 contracts), CE 50L
      23,350: PE 162L (249,230 contracts), CE 80L
      23,400: PE 80L, CE 219L (336,923 contracts)
      23,450: PE 40L, CE 137L (210,769 contracts)
    """
    mult = 65.0 / 100000.0  # contracts * mult = Lakhs
    rows = [
        {"strike": 23250.0, "pe_oi": 50000.0 / mult, "ce_oi": 20000.0 / mult},
        {"strike": 23300.0, "pe_oi": 196.0 / mult, "ce_oi": 50.0 / mult},
        {"strike": 23350.0, "pe_oi": 162.0 / mult, "ce_oi": 80.0 / mult},
        {"strike": 23400.0, "pe_oi": 80.0 / mult, "ce_oi": 219.0 / mult},
        {"strike": 23450.0, "pe_oi": 40.0 / mult, "ce_oi": 137.0 / mult},
        {"strike": 23500.0, "pe_oi": 30.0 / mult, "ce_oi": 90.0 / mult},
    ]
    return pd.DataFrame(rows)


def test_floor_test_alert_trigger_and_format(monkeypatch, tmp_path: Path):
    """
    Spot 23,358 approaching 23,350 PE floor [162L] from above (falling).
    Verifies alert fires, format matches exact user specification, and cooldown records.
    """
    df = create_sample_chain()
    now = datetime(2026, 9, 21, 10, 15, 0)
    st = scheduler.load_state()

    # Setup snapshots: previous spot 23,370 -> current spot 23,358 (falling)
    scheduler._SESSION_SNAPSHOTS = [
        (now - timedelta(minutes=3), 23370.0, df.copy()),
        (now, 23358.0, df.copy()),
    ]

    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))

    # Point observations to tmp_path for isolation
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, now, st)

    assert len(sent_messages) == 1
    msg = sent_messages[0]

    expected = (
        "🎯 FLOOR TEST: Spot 23,358 → 23,350 PE floor [162L]\n"
        "   Watch for 8 EMA bounce on 15m chart\n"
        "   If holds → CE buy, target +50pts (23,400)\n"
        "   If breaks → next floor 23,300 PE [196L]"
    )
    assert msg.strip() == expected.strip()
    assert st["last_floor_test_msg"] == msg
    assert "floor_test_time_23350" in st
    assert "floor_test_23350" in st.get("alert_cooldowns", {})


def test_ceiling_test_alert_trigger_and_format(monkeypatch):
    """
    Spot 23,378 approaching 23,400 CE wall [219L] from below (rising).
    Verifies alert fires, format matches exact user specification, and cooldown records.
    """
    df = create_sample_chain()
    now = datetime(2026, 9, 21, 10, 30, 0)
    st = scheduler.load_state()

    # Setup snapshots: previous spot 23,365 -> current spot 23,378 (rising)
    scheduler._SESSION_SNAPSHOTS = [
        (now - timedelta(minutes=3), 23365.0, df.copy()),
        (now, 23378.0, df.copy()),
    ]

    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    scheduler.check_floor_ceiling_approach_alert(df, 23378.0, now, st)

    assert len(sent_messages) == 1
    msg = sent_messages[0]

    expected = (
        "🚧 CEILING TEST: Spot 23,378 → 23,400 CE wall [219L]\n"
        "   Watch for rejection or breakout on 15m chart\n"
        "   If rejects → PE buy, target -50pts (23,350)\n"
        "   If breaks → CE buy, target 23,450 CE [137L]"
    )
    assert msg.strip() == expected.strip()
    assert st["last_ceiling_test_msg"] == msg
    assert "ceiling_test_time_23400" in st
    assert "ceiling_test_23400" in st.get("alert_cooldowns", {})


def test_time_window_suppression(monkeypatch):
    """Alerts must only fire during 09:20 - 14:45 window."""
    df = create_sample_chain()
    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    # 1. Before 09:20 (e.g. 09:15) -> SUPPRESSED
    t_early = datetime(2026, 9, 21, 9, 15, 0)
    st_early = scheduler.load_state()
    scheduler._SESSION_SNAPSHOTS = [(t_early, 23358.0, df.copy())]
    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, t_early, st_early)
    assert len(sent_messages) == 0

    # 2. After 14:45 (e.g. 14:46) -> SUPPRESSED
    t_late = datetime(2026, 9, 21, 14, 46, 0)
    st_late = scheduler.load_state()
    scheduler._SESSION_SNAPSHOTS = [(t_late, 23358.0, df.copy())]
    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, t_late, st_late)
    assert len(sent_messages) == 0


def test_freeze_window_suppression(monkeypatch):
    """Alerts must be skipped during freeze window 11:30 - 13:15."""
    df = create_sample_chain()
    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    # Inside freeze window (e.g. 12:00) -> SUPPRESSED
    t_freeze = datetime(2026, 9, 21, 12, 0, 0)
    st = scheduler.load_state()
    scheduler._SESSION_SNAPSHOTS = [
        (t_freeze - timedelta(minutes=3), 23370.0, df.copy()),
        (t_freeze, 23358.0, df.copy()),
    ]
    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, t_freeze, st)
    assert len(sent_messages) == 0


def test_direction_filter(monkeypatch):
    """
    Floor test requires approaching FROM ABOVE (falling).
    Ceiling test requires approaching FROM BELOW (rising).
    """
    df = create_sample_chain()
    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    now = datetime(2026, 9, 21, 10, 0, 0)

    # 1. Floor test: Spot is 23,358 (near 23,350 PE) but spot is RISING from 23,352 -> SUPPRESSED
    st = scheduler.load_state()
    scheduler._SESSION_SNAPSHOTS = [
        (now - timedelta(minutes=3), 23352.0, df.copy()),
        (now, 23358.0, df.copy()),
    ]
    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, now, st)
    assert len(sent_messages) == 0

    # 2. Ceiling test: Spot is 23,378 (near 23,400 CE) but spot is FALLING from 23,390 -> SUPPRESSED
    st2 = scheduler.load_state()
    scheduler._SESSION_SNAPSHOTS = [
        (now - timedelta(minutes=3), 23390.0, df.copy()),
        (now, 23378.0, df.copy()),
    ]
    scheduler.check_floor_ceiling_approach_alert(df, 23378.0, now, st2)
    assert len(sent_messages) == 0


def test_cooldown_enforced(monkeypatch):
    """Cooldown of 30 minutes per level prevents repeat spam."""
    df = create_sample_chain()
    sent_messages = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_messages.append(msg))
    monkeypatch.setattr(scheduler, "log_floor_ceiling_observation", lambda *args, **kwargs: None)

    now = datetime(2026, 9, 21, 10, 0, 0)
    st = scheduler.load_state()

    scheduler._SESSION_SNAPSHOTS = [
        (now - timedelta(minutes=3), 23370.0, df.copy()),
        (now, 23358.0, df.copy()),
    ]

    # 1st fire -> allowed
    scheduler.check_floor_ceiling_approach_alert(df, 23358.0, now, st)
    assert len(sent_messages) == 1

    # 2nd fire at 10:10 (10 mins later, < 30 mins) -> SUPPRESSED
    now_10m = now + timedelta(minutes=10)
    scheduler._SESSION_SNAPSHOTS.append((now_10m, 23355.0, df.copy()))
    scheduler.check_floor_ceiling_approach_alert(df, 23355.0, now_10m, st)
    assert len(sent_messages) == 1  # Still 1, no duplicate

    # 3rd fire at 10:35 (35 mins later, >= 30 mins) -> ALLOWED
    now_35m = now + timedelta(minutes=35)
    scheduler._SESSION_SNAPSHOTS.append((now_35m, 23352.0, df.copy()))
    scheduler.check_floor_ceiling_approach_alert(df, 23352.0, now_35m, st)
    assert len(sent_messages) == 2


def test_observation_logging_and_query_integration(tmp_path: Path, monkeypatch, capsys):
    """
    Verifies that alerts log to data/observations/YYYY-MM-DD.md with tag,
    and obs_query.py displays the alert inline in context.
    """
    # Point observation dir to tmp_path
    monkeypatch.setattr(
        obs_query,
        "load_observation_config",
        lambda: {"enabled": True, "dir": str(tmp_path)},
    )

    d = date(2026, 9, 21)
    log_file = tmp_path / "2026-09-21.md"

    # Seed 2 entries before and 2 entries after
    lines = [
        "# NIFTY Daily Observation Log — 2026-09-21\n",
        "## 10:09 | Spot 23,380 | NEUTRAL →",
        "- GEX: ±₹1Cr NEUTRAL gamma (mixed)",
        "- Action: NO TRADE",
        "> Note: [price consolidating]",
        "---",
        "## 10:12 | Spot 23,370 | NEUTRAL →",
        "- GEX: +₹5Cr SHORT gamma (trending)",
        "- Action: NO TRADE",
        "> Note: [moving down towards floor]",
        "---",
        "## 10:15 | Spot 23,358 | BEARISH ▼",
        "- GEX: +₹10Cr SHORT gamma (trending)",
        "- Action: NO TRADE",
        "> Note: [testing support]",
        "---",
        "## 10:18 | Spot 23,355 | BEARISH ▼",
        "- GEX: +₹12Cr SHORT gamma (trending)",
        "- Action: NO TRADE",
        "> Note: [holding 23350]",
        "---",
        "## 10:21 | Spot 23,365 | BULLISH ▲",
        "- GEX: +₹8Cr SHORT gamma (trending)",
        "- Action: CE BUY",
        "> Note: [bounced off 23350]",
        "---",
    ]
    log_file.write_text("\n".join(lines), encoding="utf-8")

    # Now simulate scheduler logging FLOOR_TEST alert at 10:15
    now_1015 = datetime(2026, 9, 21, 10, 15, 0)
    summary = "Spot 23,358 → 23,350 PE floor [162L]"

    content = log_file.read_text(encoding="utf-8")
    assert "FLOOR_TEST" not in content

    # Execute logging helper directly with obs_dir=tmp_path
    scheduler.log_floor_ceiling_observation(now_1015, 23358.0, "FLOOR_TEST", summary, obs_dir=tmp_path)

    # Check that log_file now has inline alert line
    updated_content = log_file.read_text(encoding="utf-8")
    assert "- Alert: [FLOOR_TEST] Spot 23,358 → 23,350 PE floor [162L]" in updated_content

    # Query observations using obs_query at 10:15
    ret = obs_query.query_observations("10:15", d)
    assert ret == 0

    captured = capsys.readouterr().out
    assert "OBSERVATION QUERY: 10:15 on 2026-09-21" in captured
    assert "🎯 TARGET -> ## 10:15 | Spot 23,358 | BEARISH ▼" in captured
    assert "- Alert: [FLOOR_TEST] Spot 23,358 → 23,350 PE floor [162L]" in captured
