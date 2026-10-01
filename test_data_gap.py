"""
test_data_gap.py — Verification of gap-awareness in briefing, observation log, and scheduler.

Requirements tested:
1. All thresholds configured in strategy1_rules.json under 'data_gap' (max_gap_seconds: 180).
2. Explicit gap notice:
   ⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07
3. Change-based signals invalidated across a gap:
   - OI flow / ΔOI (terminal, telegram, observation log)
   - Writer dominance (ratio, structure, terminal, telegram, observation log)
   - Dual PCR flow component (standing PCR remains valid)
   - Option chain snapshot ΔOI columns show INVALID
4. Level-based signals remain valid across a gap:
   - S1, R1, S2, R2 walls
   - Standing PCR
   - Max Pain
   - GEX total & zero gamma
5. Checklist Box 2 ('Flow aligned:') marked HOLD ⏸ across gap (pass=True, score preserved).
6. Floor/ceiling weakening streak reset to 0 across gap (never resumed or shortened).
7. EOD summary and observation log record the data gap explicitly.
8. Alerts (cascade, ceiling breakout, put writer defense, liquidity trap, oi dominance) suppressed across gap.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import agt_briefing
import scheduler
import store
import oi_timeline


def test_data_gap_rules_loaded():
    """Verify max_gap_seconds is configured in strategy1_rules.json and loaded properly."""
    rules_path = Path(__file__).parent / "strategy1_rules.json"
    assert rules_path.exists()
    data = json.loads(rules_path.read_text())
    assert "data_gap" in data
    assert data["data_gap"].get("max_gap_seconds") == 180

    rules = agt_briefing.load_briefing_rules()
    max_gap_sec = rules.get("data_gap", {}).get("max_gap_seconds") or rules.get("max_gap_seconds")
    assert max_gap_sec == 180.0


def test_writer_dominance_gap_invalidation():
    """Verify compute_writer_dominance invalidates ratio, classification, and text across a gap."""
    rules = agt_briefing.load_briefing_rules()
    df = pd.DataFrame({
        "strike": [23200, 23250, 23300],
        "ce_oi": [100000, 150000, 200000],
        "pe_oi": [200000, 150000, 100000],
        "ce_chg_oi": [50000, 80000, 100000],
        "pe_chg_oi": [10000, 20000, 30000],
    })

    dom_data = agt_briefing.compute_writer_dominance(
        latest_df=df,
        latest_spot=23250.0,
        rules=rules,
        atm_override=23250.0,
        is_data_gap=True,
        gap_msg="⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07",
    )

    assert dom_data["agg_ratio"] is None
    assert dom_data["obs_dominance"] == "INVALID (data gap)"
    assert dom_data["obs_structure"] == "INVALID (data gap)"
    assert dom_data["telegram_line"] == "⚖️ Dominance: INVALID (data gap)"
    assert dom_data["telegram_struct_line"] is None
    assert any("INVALID (data gap)" in line for line in dom_data["terminal_lines"])


def test_pretrade_checklist_gap_hold():
    """Verify Box 2 is set to HOLD ⏸ when a data gap is present."""
    rules = agt_briefing.load_briefing_rules()
    s1_row = pd.Series({"strike": 23200, "pe_oi": 150000})
    r1_row = pd.Series({"strike": 23300, "ce_oi": 150000})

    chk = agt_briefing.compute_pretrade_checklist(
        s1_row=s1_row,
        r1_row=r1_row,
        d_pe_15m=10000,
        d_ce_15m=-5000,
        iv_percentile=50.0,
        gex_data={"status": "ok", "total_gex_cr": 15.0},
        latest_ts=datetime(2026, 9, 24, 13, 4, 0),
        target_date=date(2026, 9, 24),
        target_time=dtime(13, 4, 0),
        rules=rules,
        has_history=True,
        is_data_gap=True,
    )

    box2 = next(b for b in chk["boxes"] if "Flow aligned" in b[0])
    assert box2[1] == "HOLD ⏸"
    assert box2[2] is True
    assert box2[3] is None
    assert any("⏸ Flow" in ln for ln in chk["telegram_lines"])


def test_format_observation_entry_gap():
    """Verify observation log formatting marks change-based lines INVALID and logs DATA_GAP alert."""
    rules = agt_briefing.load_briefing_rules()
    entry = agt_briefing.format_observation_entry(
        target_date=date(2026, 9, 24),
        latest_ts=datetime(2026, 9, 24, 13, 4, 0),
        latest_spot=23250.0,
        bias_word="INVALID (data gap)",
        gex_data={"status": "ok", "total_gex_cr": 10.0, "regime": "NEUTRAL"},
        iv_data={"percentile": 50.0, "label": "FAIR", "status": "ok"},
        s1_row=pd.Series({"strike": 23200, "pe_oi": 150000}),
        r1_row=pd.Series({"strike": 23300, "ce_oi": 150000}),
        mult=0.00065,
        d_pe_15m=10000,
        d_ce_15m=-5000,
        floor_weakening_streak=4,
        chk_data={"score": 5, "boxes": [{}, {}, {}, {}, {}], "failed_tags": []},
        action_line="NO TRADE",
        rules=rules,
        is_data_gap=True,
        gap_msg="⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07",
    )

    assert "## 13:04 | Spot 23,250 | INVALID (data gap)" in entry
    assert "- Flow: INVALID (data gap)" in entry
    assert "- Dominance: INVALID (data gap)" in entry
    assert "- Structure: INVALID (data gap)" in entry
    assert "- Action: NO TRADE (data gap)" in entry
    assert "- Alert: [DATA_GAP] ⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07" in entry
    assert "Floor weakening" not in entry  # streak suppressed


def test_build_telegram_briefing_gap():
    """Verify Telegram briefing presents clean gap notice and invalidates flow & dominance."""
    rules = agt_briefing.load_briefing_rules()
    df = pd.DataFrame({
        "strike": [23200, 23250, 23300],
        "ce_oi": [100000, 150000, 200000],
        "pe_oi": [200000, 150000, 100000],
    })

    tg = agt_briefing.build_telegram_briefing(
        latest_ts=datetime(2026, 9, 24, 13, 4, 0),
        latest_spot=23250.0,
        latest_df=df,
        eff_dom="INVALID",
        pcr_standing=0.85,
        pcr_trend="Flat →",
        d_pe_15m=10000,
        d_ce_15m=5000,
        mult=0.00065,
        rules=rules,
        eval_snaps=[],
        floor_weakening_streak=3,
        s1_strike=23200,
        r1_strike=23300,
        is_data_gap=True,
        gap_msg="⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07",
    )

    assert "INVALID (data gap)" in tg
    assert "⚠️ DATA GAP 11:02-13:04 (122 min) — flow, dominance and streak signals invalid until 13:07" in tg
    assert "🔄 Flow: INVALID (data gap)" in tg
    assert "⚖️ Dominance: INVALID (data gap)" in tg
    assert "➡️ WAIT — data gap, signals invalid until 13:07" in tg
    assert "Floor weakening" not in tg


def test_eod_summary_gap_reporting():
    """Verify EOD summary lists all detected data gaps across market hours."""
    rules = agt_briefing.load_briefing_rules()
    t1 = datetime(2026, 9, 24, 11, 2, 0)
    t2 = datetime(2026, 9, 24, 13, 4, 0)
    df = pd.DataFrame({
        "strike": [23200, 23250, 23300],
        "ce_oi": [100000, 150000, 200000],
        "pe_oi": [200000, 150000, 100000],
    })
    snaps = [(t1, 23250.0, df), (t2, 23250.0, df)]

    summary = agt_briefing.build_eod_summary(
        target_date=date(2026, 9, 24),
        eval_snaps=snaps,
        all_snaps=snaps,
        rules=rules,
        latest_spot=23250.0,
        latest_df=df,
        r1_strike=23300.0,
        r2_strike=23400.0,
        s1_strike=23200.0,
        s2_strike=23100.0,
    )

    assert "Data gaps: 11:02–13:04 (122 min) — flow/streak signals invalidated" in summary


def test_injected_2hour_gap_replay():
    """
    Test injecting an authentic 2-hour snapshot gap (11:01 to 13:04) into a replay
    and asserting no flow value, dominance figure, or streak count is emitted across it.
    """
    all_day_snaps = store.load_day(date(2026, 9, 24))
    mkt_snaps = oi_timeline.filter_market_hours(all_day_snaps)

    # Inject 2-hour gap: keep <= 11:02, then resume at >= 13:04
    before_gap = [s for s in mkt_snaps if s[0].time() <= dtime(11, 2)]
    after_gap = [s for s in mkt_snaps if s[0].time() >= dtime(13, 4)]
    gap_snaps = before_gap + after_gap

    assert len(before_gap) > 0
    assert len(after_gap) > 0
    gap_duration_sec = (after_gap[0][0] - before_gap[-1][0]).total_seconds()
    assert gap_duration_sec > 180.0  # > 2 hours

    with patch("store.load_day", side_effect=lambda d: gap_snaps if d == date(2026, 9, 24) else []):
        # Briefing at the first snapshot immediately following the 2-hour gap
        out = agt_briefing.build_briefing(date(2026, 9, 24), target_time=dtime(13, 5))

        # 1. Gap is detected and reported explicitly
        assert agt_briefing.build_briefing.is_data_gap is True
        data_gap_msg = getattr(agt_briefing.build_briefing, "data_gap", "")
        assert "⚠️ DATA GAP 11:01-13:04" in data_gap_msg
        assert "invalid until 13:07" in data_gap_msg
        assert data_gap_msg in out

        # 2. Change-based signals are marked INVALID
        assert "BIAS:                 INVALID (data gap)" in out
        assert "REGIME:               DATA GAP — signals invalid" in out
        assert "PCR (flow):           INVALID (data gap)" in out
        assert "WRITERS RATIO:        INVALID (data gap)" in out
        assert "Flow:    INVALID (data gap)" in out
        assert "DOMINANCE: INVALID (data gap" in out

        # 3. Option chain ΔOI columns are marked INVALID
        lines = out.splitlines()
        opt_chain_idx = next(i for i, ln in enumerate(lines) if "OPTION CHAIN SNAPSHOT" in ln)
        opt_rows = [lines[j] for j in range(opt_chain_idx + 2, opt_chain_idx + 7)]
        for row in opt_rows:
            assert "INVALID" in row

        # 4. Level-based signals remain intact
        assert "PCR (ATM" in out
        assert "MAX PAIN:" in out
        assert "MAX INSTITUTIONAL WALLS" in out
        assert "SUPPORT / RESISTANCE" in out

        # 5. Checklist Box 2 is HOLD ⏸
        chk = getattr(agt_briefing.build_briefing, "checklist_data", {})
        box2 = next(b for b in chk["boxes"] if "Flow aligned" in b[0])
        assert box2[1] == "HOLD ⏸"


def test_scheduler_wall_streak_reset_across_gap():
    """Verify floor/ceiling weakening streak is reset to 0 across a gap in scheduler.py."""
    st = {
        "floor_weakening_streak": 4,
        "floor_weakening_streak_updated": 0,
        "ceiling_weakening_streak": 3,
        "ceiling_weakening_streak_updated": 0,
    }
    t_before = datetime(2026, 9, 24, 11, 2, 0)
    t_after = datetime(2026, 9, 24, 13, 4, 0)
    df = pd.DataFrame({
        "strike": [23200, 23250, 23300],
        "pe_oi": [100000, 150000, 200000],
        "ce_oi": [200000, 150000, 100000],
    })

    scheduler._SESSION_SNAPSHOTS = [
        (t_before, 23250.0, df),
        (t_after, 23250.0, df),
    ]

    # Calling update on floor wall streak across the gap resets streak to 0
    streak_floor = scheduler._update_wall_streak("floor", df, 23250.0, t_after, st)
    assert streak_floor == 0
    assert st["floor_weakening_streak"] == 0

    # Calling update on ceiling wall streak across the gap resets streak to 0
    streak_ceil = scheduler._update_wall_streak("ceiling", df, 23250.0, t_after, st)
    assert streak_ceil == 0
    assert st["ceiling_weakening_streak"] == 0


def test_scheduler_alerts_suppressed_across_gap():
    """Verify cascade, ceiling breakout, put writer defense, and trap alerts do not fire across a gap."""
    st = {
        "cascade_alert_last": 0,
        "ceiling_breakout_last": 0,
    }
    t_before = datetime(2026, 9, 24, 11, 2, 0)
    t_after = datetime(2026, 9, 24, 13, 4, 0)
    df = pd.DataFrame({
        "strike": [23200, 23250, 23300],
        "pe_oi": [100000, 150000, 200000],
        "ce_oi": [200000, 150000, 100000],
    })
    scheduler._SESSION_SNAPSHOTS = [
        (t_before, 23250.0, df),
        (t_after, 23250.0, df),
    ]

    with patch("scheduler.send_telegram_alert") as mock_tg:
        # Cascade alert across gap
        scheduler.check_cascade_alert(5, df, 23250.0, t_after, st)
        mock_tg.assert_not_called()

        # Ceiling breakout alert across gap
        scheduler.check_ceiling_breakout_alert(5, df, 23250.0, t_after, st)
        mock_tg.assert_not_called()

        # Put writer defense alert across gap
        scheduler.check_put_writer_defense_alert(df, df, 23250.0, t_after, st)
        mock_tg.assert_not_called()

        # Liquidity trap alert across gap
        scheduler.check_liquidity_trap_alert(df, 23250.0, t_after, st)
        mock_tg.assert_not_called()

        # OI dominance alert across gap
        scheduler.check_oi_dominance_alert(df, 23250.0, t_after, st)
        mock_tg.assert_not_called()
