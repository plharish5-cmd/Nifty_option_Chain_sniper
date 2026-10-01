"""
test_obs_system.py — Unit tests for the Daily Observation Log System and CLI Query Tool.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, time as dtime
from pathlib import Path

import pandas as pd
import pytest

import agt_briefing
import obs_query


def test_observation_rules_loaded():
    """Verify strategy1_rules.json includes observations block and loads properly."""
    rules = agt_briefing.load_briefing_rules()
    assert rules.get("observations_enabled") is True
    assert "observations_dir" in rules
    assert rules.get("observations_freeze_end_time") == "13:15"
    assert rules.get("observations_eod_summary_time") == "15:25"


def test_format_observation_entry_matches_schema():
    """Verify entry matches the exact required format."""
    rules = agt_briefing.load_briefing_rules()
    dt = datetime(2026, 9, 21, 10, 16, 0)
    s1_row = pd.Series({"strike": 23300.0, "pe_oi": 360000.0})
    r1_row = pd.Series({"strike": 23400.0, "ce_oi": 376923.0})
    mult = 65 / 100000.0  # 0.00065 -> 234L, 245L

    entry = agt_briefing.format_observation_entry(
        target_date=dt.date(),
        latest_ts=dt,
        latest_spot=23373.0,
        bias_word="CAUTION ⚠️",
        gex_data={"status": "ok", "total_gex_cr": -18.0, "regime": "LONG"},
        iv_data={"percentile": 82.0, "label": "EXPENSIVE"},
        s1_row=s1_row,
        r1_row=r1_row,
        mult=mult,
        d_pe_15m=-19000.0,
        d_ce_15m=13000.0,
        floor_weakening_streak=4,
        chk_data={
            "score": 4,
            "boxes": [1, 2, 3, 4, 5, 6],
            "failed_tags": ["IV", "RVOL"],
        },
        action_line="➡️ NO TRADE — caution, ratio falling",
        rules=rules,
    )

    lines = entry.splitlines()
    assert lines[0] == "## 10:16 | Spot 23,373 | CAUTION ⚠️"
    assert lines[1] == "- GEX: -₹18Cr LONG gamma (rangebound)"
    assert lines[2].startswith("- RVOL:")
    assert lines[3] == "- IV: 82nd pct EXPENSIVE"
    assert lines[4] == "- Floor: 23,300 PE [234L] | Ceiling: 23,400 CE [245L]"
    assert lines[5] == "- Flow: Puts leaving -19k | Calls adding +13k"
    assert "- Floor weakening: 4th consecutive" in entry
    assert "- Checklist: 4/6 WAIT (IV, RVOL failing)" in entry
    assert "- Action: NO TRADE" in entry
    assert any(l.startswith("- mj_shadow:") for l in lines)
    assert any("CE setup 23,300 PE [234L]" in l for l in lines)
    assert any("PE setup 23,400 CE [245L]" in l for l in lines)
    assert any("RVOL 0.38 DEAD" in l for l in lines)
    assert "> Note: [chart observation / what price did]" in entry
    assert "---" in entry


def test_pre_market_observation_and_telegram_suppression():
    """Verify pre-market (< 09:15) entries suppress directional signals and mark spot stale."""
    rules = agt_briefing.load_briefing_rules()
    dt = datetime(2026, 9, 23, 7, 4, 0)
    s1_row = pd.Series({"strike": 23300.0, "pe_oi": 219000.0})
    r1_row = pd.Series({"strike": 23350.0, "ce_oi": 223000.0})

    entry = agt_briefing.format_observation_entry(
        target_date=dt.date(),
        latest_ts=dt,
        latest_spot=23329.0,
        bias_word="BEARISH ▼",  # Even if caller passed BEARISH
        gex_data=None,
        iv_data=None,
        s1_row=s1_row,
        r1_row=r1_row,
        mult=0.00065,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data={"score": 4, "boxes": [1, 2, 3, 4, 5, 6], "failed_tags": ["Flow", "Timing"]},
        action_line="➡️ PE BUY SIGNAL — confirm 8 EMA on chart",
        rules=rules,
    )

    lines = entry.splitlines()
    assert lines[0] == "## 07:04 | Spot 23,329 (prev close) | PRE-MARKET"
    assert "- Action: PRE-MARKET — no signal (stale spot, no RVOL/GEX)" in lines
    assert not any("BUY" in ln for ln in lines if ln.startswith("- Action:"))
    # mj_shadow must be absent on pre-market entries
    assert not any("mj_shadow" in ln for ln in lines)


def test_rvol_na_suppresses_directional_action():
    """Verify market-hours entry with RVOL N/A suppresses BUY action to NO TRADE."""
    rules = agt_briefing.load_briefing_rules()
    dt = datetime(2026, 9, 23, 9, 20, 0)
    entry = agt_briefing.format_observation_entry(
        target_date=dt.date(),
        latest_ts=dt,
        latest_spot=23398.0,
        bias_word="BEARISH ▼",
        gex_data=None,
        iv_data=None,
        s1_row=None,
        r1_row=None,
        mult=0.00065,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data={"score": 4, "boxes": [1, 2, 3, 4, 5, 6], "failed_tags": ["Flow", "Timing"]},
        action_line="➡️ PE BUY SIGNAL — confirm 8 EMA on chart",
        rules=rules,
        rvol_val=None,  # RVOL N/A
    )
    lines = entry.splitlines()
    assert "- Action: NO TRADE" in lines
    assert not any("PE BUY" in ln or "CE BUY" in ln for ln in lines if ln.startswith("- Action:"))


def test_append_observation_log_and_milestones(tmp_path: Path):
    """Verify log file creation, idempotency, and milestone separators."""
    rules = agt_briefing.load_briefing_rules()
    rules["observations_dir"] = str(tmp_path)

    d = date(2026, 9, 21)
    ts_morning = datetime(2026, 9, 21, 10, 15)

    # First append
    res1 = agt_briefing.append_observation_log(
        target_date=d,
        latest_ts=ts_morning,
        latest_spot=23370.0,
        bias_word="NEUTRAL →",
        gex_data={"status": "ok", "total_gex_cr": 5.0, "regime": "SHORT"},
        iv_data={"percentile": 50.0, "label": "FAIR"},
        s1_row=None,
        r1_row=None,
        mult=0.00065,
        d_pe_15m=1000,
        d_ce_15m=1000,
        floor_weakening_streak=0,
        chk_data={"score": 6, "boxes": [1, 2, 3, 4, 5, 6], "failed_tags": []},
        action_line="NO TRADE",
        rules=rules,
    )
    assert res1 is not None

    log_file = tmp_path / "2026-09-21.md"
    assert log_file.exists()
    content1 = log_file.read_text()
    assert "## 10:15 | Spot 23,370 | NEUTRAL →" in content1

    # Idempotent re-append of same minute should return None and not duplicate
    res_dup = agt_briefing.append_observation_log(
        target_date=d,
        latest_ts=ts_morning,
        latest_spot=23370.0,
        bias_word="NEUTRAL →",
        gex_data=None,
        iv_data=None,
        s1_row=None,
        r1_row=None,
        mult=0.00065,
        d_pe_15m=0,
        d_ce_15m=0,
        floor_weakening_streak=0,
        chk_data=None,
        action_line="NO TRADE",
        rules=rules,
    )
    assert res_dup is None
    assert content1.count("## 10:15") == 1

    # Freeze end marker (at 13:15)
    ts_freeze = datetime(2026, 9, 21, 13, 15)
    agt_briefing.append_observation_log(
        target_date=d,
        latest_ts=ts_freeze,
        latest_spot=23380.0,
        bias_word="BULLISH ▲",
        gex_data=None,
        iv_data=None,
        s1_row=None,
        r1_row=None,
        mult=0.00065,
        d_pe_15m=0,
        d_ce_15m=0,
        floor_weakening_streak=0,
        chk_data=None,
        action_line="NO TRADE",
        rules=rules,
    )
    content2 = log_file.read_text()
    assert "== FREEZE END ==" in content2
    assert "## 13:15 | Spot 23,380 | BULLISH ▲" in content2


def test_obs_query_window(tmp_path: Path, monkeypatch, capsys):
    """Test obs_query retrieves closest entry and 5-entry window."""
    log_file = tmp_path / "2026-09-21.md"
    # Create sample log with 7 entries
    lines = ["# Log\n\n"]
    times = ["10:06", "10:09", "10:12", "10:15", "10:18", "10:21", "10:24"]
    for t in times:
        lines.append(f"## {t} | Spot 23,370 | TEST\n- Action: NO TRADE\n> Note: []\n\n---\n\n")
    log_file.write_text("".join(lines))

    # Point obs_query to tmp_path
    monkeypatch.setattr(obs_query, "load_observation_config", lambda: {"dir": str(tmp_path)})

    # Query for 10:16 (nearest is 10:15, delta 1m)
    code = obs_query.query_observations("10:16", date(2026, 9, 21))
    assert code == 0

    out = capsys.readouterr().out
    assert "OBSERVATION QUERY: 10:16 on 2026-09-21" in out
    assert "TARGET -> ## 10:15" in out
    # Window should contain 10:09, 10:12, 10:15, 10:18, 10:21 (5 entries)
    assert "## 10:09" in out
    assert "## 10:12" in out
    assert "## 10:15" in out
    assert "## 10:18" in out
    assert "## 10:21" in out
    # 10:06 and 10:24 should be outside the 5-entry window
    assert "## 10:06" not in out
    assert "## 10:24" not in out


def test_telegram_pre_market_suppression():
    """Verify build_telegram_briefing suppresses directional signals and marks spot stale pre-market."""
    rules = agt_briefing.load_briefing_rules()
    ts_pre = datetime(2026, 9, 23, 7, 30)
    df = pd.DataFrame([
        {"strike": 23300.0, "ce_oi": 100000.0, "pe_oi": 200000.0},
        {"strike": 23400.0, "ce_oi": 300000.0, "pe_oi": 100000.0},
    ])
    tg = agt_briefing.build_telegram_briefing(
        latest_ts=ts_pre,
        latest_spot=23329.0,
        latest_df=df,
        eff_dom="CE_DOMINANT",
        pcr_standing=0.8,
        pcr_trend="Falling ↓",
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        mult=0.00065,
        rules=rules,
        eval_snaps=[(ts_pre, 23329.0, df)],
        is_stale=False,
        s1_strike=23300.0,
        r1_strike=23400.0,
    )
    assert "PRE-MARKET" in tg
    assert "(prev close)" in tg
    assert "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)" in tg
    assert "PE BUY" not in tg
    assert "CE BUY" not in tg


def test_mj_shadow_config_toggle():
    """Verify mj_shadow obeys shadow_logging config flag."""
    dt = datetime(2026, 9, 21, 10, 16, 0)
    rules_on = agt_briefing.load_briefing_rules()
    rules_on["mj_mode"]["shadow_logging"] = True
    entry_on = agt_briefing.format_observation_entry(
        target_date=dt.date(),
        latest_ts=dt,
        latest_spot=23373.0,
        bias_word="CAUTION ⚠️",
        gex_data=None,
        iv_data=None,
        s1_row=pd.Series({"strike": 23300.0, "pe_oi": 360000.0}),
        r1_row=pd.Series({"strike": 23400.0, "ce_oi": 376923.0}),
        mult=0.00065,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data={"score": 6, "boxes": [1, 2, 3, 4, 5, 6], "failed_tags": []},
        action_line="NO TRADE",
        rules=rules_on,
    )
    assert "- mj_shadow:" in entry_on

    rules_off = agt_briefing.load_briefing_rules()
    rules_off["mj_mode"]["shadow_logging"] = False
    entry_off = agt_briefing.format_observation_entry(
        target_date=dt.date(),
        latest_ts=dt,
        latest_spot=23373.0,
        bias_word="CAUTION ⚠️",
        gex_data=None,
        iv_data=None,
        s1_row=pd.Series({"strike": 23300.0, "pe_oi": 360000.0}),
        r1_row=pd.Series({"strike": 23400.0, "ce_oi": 376923.0}),
        mult=0.00065,
        d_pe_15m=0.0,
        d_ce_15m=0.0,
        floor_weakening_streak=0,
        chk_data={"score": 6, "boxes": [1, 2, 3, 4, 5, 6], "failed_tags": []},
        action_line="NO TRADE",
        rules=rules_off,
    )
    assert "- mj_shadow:" not in entry_off
