"""
test_oi_alerts.py — Unit & Integration tests for OI Dominance Telegram Alerts in scheduler.py
"""
import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import scheduler
import store

_ORIG_LOAD_DAY = store.load_day


@pytest.fixture(autouse=True)
def isolate_store():
    """Ensure tests run against a clean slate without loading disk parquet files."""
    with patch("store.load_day", return_value=[]):
        yield


def test_format_spread():
    """Verify spread formatting in contracts (e.g. +51k, -53k, +500)."""
    assert scheduler._format_spread(50795.0) == "+51k"
    assert scheduler._format_spread(-53147.0) == "-53k"
    assert scheduler._format_spread(1000.0) == "+1k"
    assert scheduler._format_spread(-1000.0) == "-1k"
    assert scheduler._format_spread(500.0) == "+500"
    assert scheduler._format_spread(-500.0) == "-500"
    assert scheduler._format_spread(0.0) == "+0"


def test_send_telegram_alert_missing_credentials(monkeypatch):
    """Verify that send_telegram_alert cleanly logs and returns False if credentials absent."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)

    with patch("scheduler._load_env"):
        ok = scheduler.send_telegram_alert("Test message")
        assert ok is False


def test_send_telegram_alert_success(monkeypatch):
    """Verify that send_telegram_alert calls Telegram API and succeeds on 200."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_bot_token_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock_chat_id_456")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        ok = scheduler.send_telegram_alert("⚡ OI SHIFT | 23300 ATM | PE_DOMINANT | PCR 7.2 | Spread +51k | Spot 23347 | 12:42")
        assert ok is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "mock_bot_token_123" in args[0]
        assert kwargs["json"]["chat_id"] == "mock_chat_id_456"
        assert "PE_DOMINANT" in kwargs["json"]["text"]


def test_send_telegram_alert_failure_and_exception(monkeypatch):
    """Verify that HTTP failure or network exception never raises and returns False."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_bot_token_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock_chat_id_456")

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request: chat not found"

    with patch("requests.post", return_value=mock_resp):
        ok = scheduler.send_telegram_alert("Test fail")
        assert ok is False

    with patch("requests.post", side_effect=ConnectionError("Network down")):
        ok = scheduler.send_telegram_alert("Test exception")
        assert ok is False


def _make_chain_df(strike: float, spot: float, ce_chg: float, pe_chg: float, ce_oi: float = 50000.0, pe_oi: float = 50000.0) -> pd.DataFrame:
    """Helper to build minimal option chain row for a strike."""
    return pd.DataFrame([{
        "strike": strike,
        "spot": spot,
        "ce_oi": ce_oi,
        "pe_oi": pe_oi,
        "ce_chg_oi": ce_chg,
        "pe_chg_oi": pe_chg,
        "ce_ltp": 120.0,
        "pe_ltp": 80.0,
        "ce_chg": 5.0,
        "pe_chg": -4.0,
        "expiry": "2026-09-24",
    }])


def test_check_oi_dominance_alert_balanced_to_pe_dominant(monkeypatch):
    """Verify alert triggers when state shifts BALANCED -> PE_DOMINANT."""
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = None

    sent_alerts = []
    def mock_send(msg):
        sent_alerts.append(msg)
        return True

    monkeypatch.setattr(scheduler, "send_telegram_alert", mock_send)

    today = date(2026, 9, 17)
    # 1. First snapshot at 09:55 (BALANCED: dPE=10k, dCE=10k, spread=0)
    t1 = datetime.combine(today, dtime(9, 55, 0))
    df1 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=10000.0, ce_oi=20000.0, pe_oi=144000.0)

    # 2. Second snapshot at 09:58 (BALANCED)
    t2 = datetime.combine(today, dtime(9, 58, 0))
    df2 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=10000.0, ce_oi=20000.0, pe_oi=144000.0)

    # 3. Third snapshot at 10:15 (PE_DOMINANT: dPE=61k, dCE=10k, spread=+51k, PCR=7.2)
    t3 = datetime.combine(today, dtime(10, 15, 0))
    df3 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=61000.0, ce_oi=20000.0, pe_oi=144000.0)

    st = {"last_oi_dominance": "BALANCED"}

    scheduler.check_oi_dominance_alert(df1, 23347.0, t1, st)
    assert len(sent_alerts) == 0  # Only 1 snap so far

    scheduler.check_oi_dominance_alert(df2, 23347.0, t2, st)
    assert len(sent_alerts) == 0  # Still BALANCED -> BALANCED

    scheduler.check_oi_dominance_alert(df3, 23347.0, t3, st)
    assert len(sent_alerts) == 1  # BALANCED -> PE_DOMINANT triggers alert!
    assert "⚡ OI SHIFT | 23300 ATM | PE_DOMINANT" in sent_alerts[0]
    assert "Spread +51k" in sent_alerts[0]
    assert "PCR 7.2" in sent_alerts[0]
    assert "Spot 23347" in sent_alerts[0]
    assert st["last_oi_dominance"] == "PE_DOMINANT"

    # Next poll with same state: must NOT alert again
    t4 = datetime.combine(today, dtime(10, 16, 0))
    df4 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=61000.0, ce_oi=20000.0, pe_oi=144000.0)
    scheduler.check_oi_dominance_alert(df4, 23347.0, t4, st)
    assert len(sent_alerts) == 1  # Strictly 1 alert, no duplicate


def test_check_oi_dominance_alert_crossover(monkeypatch):
    """Verify alert triggers when state shifts to CROSSOVER."""
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = None

    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    st = {"last_oi_dominance": "BALANCED"}

    # Establish initial CE_DOMINANT state across confirm_minutes (9m)
    # Steps at 3m: 09:52, 09:55, 09:58
    for i, minute in enumerate([52, 55, 58]):
        t = datetime.combine(today, dtime(9, minute, 0))
        df = _make_chain_df(23300.0, 23300.0, ce_chg=60000.0, pe_chg=5000.0, ce_oi=80000.0, pe_oi=40000.0)
        scheduler.check_oi_dominance_alert(df, 23300.0, t, st)

    # Now flip to PE_DOMINANT holding for crossover_steps (9 mins -> 3 steps: 10:01, 10:04, 10:07)
    for i, minute in enumerate([1, 4, 7]):
        t = datetime.combine(today, dtime(10, minute, 0))
        df = _make_chain_df(23300.0, 23350.0, ce_chg=5000.0, pe_chg=65000.0, ce_oi=40000.0, pe_oi=80000.0)
        scheduler.check_oi_dominance_alert(df, 23350.0, t, st)

    # CROSSOVER should have fired once confirmed
    crossover_alerts = [m for m in sent_alerts if "CROSSOVER" in m]
    assert len(crossover_alerts) == 1
    assert "CROSSOVER" in crossover_alerts[0]


def test_suppressed_during_warmup(monkeypatch):
    """Verify that shifts before 09:50 (WARMUP) never send Telegram alerts."""
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = None

    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    t1 = datetime.combine(today, dtime(9, 25, 0))
    t2 = datetime.combine(today, dtime(9, 30, 0))
    df = _make_chain_df(23300.0, 23300.0, ce_chg=5000.0, pe_chg=80000.0)

    st = {"last_oi_dominance": "WARMUP"}
    scheduler.check_oi_dominance_alert(df, 23300.0, t1, st)
    scheduler.check_oi_dominance_alert(df, 23300.0, t2, st)

    assert len(sent_alerts) == 0


def test_suppressed_during_eod_unwind(monkeypatch):
    """Verify that shifts after 15:00 (EOD_UNWIND) never send Telegram alerts."""
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = None

    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    t1 = datetime.combine(today, dtime(15, 5, 0))
    t2 = datetime.combine(today, dtime(15, 10, 0))
    df = _make_chain_df(23300.0, 23300.0, ce_chg=5000.0, pe_chg=80000.0)

    st = {"last_oi_dominance": "BALANCED"}
    scheduler.check_oi_dominance_alert(df, 23300.0, t1, st)
    scheduler.check_oi_dominance_alert(df, 23300.0, t2, st)

    assert len(sent_alerts) == 0


def test_suppressed_during_buyer_freeze_window(monkeypatch):
    """
    Verify that state shifts during 11:30 - 13:15 are suppressed from Telegram,
    and update internal state so they do not falsely fire upon exiting the window.
    """
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = None

    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    # Pre-freeze setup (11:20 and 11:25 BALANCED)
    t1 = datetime.combine(today, dtime(11, 20, 0))
    df1 = _make_chain_df(23300.0, 23300.0, ce_chg=10000.0, pe_chg=10000.0)
    t2 = datetime.combine(today, dtime(11, 25, 0))
    df2 = _make_chain_df(23300.0, 23300.0, ce_chg=10000.0, pe_chg=10000.0)

    st = {"last_oi_dominance": "BALANCED"}
    scheduler.check_oi_dominance_alert(df1, 23300.0, t1, st)
    scheduler.check_oi_dominance_alert(df2, 23300.0, t2, st)
    assert len(sent_alerts) == 0

    # Inside freeze window (12:42: shift to PE_DOMINANT)
    t3 = datetime.combine(today, dtime(12, 42, 0))
    df3 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=61000.0)
    scheduler.check_oi_dominance_alert(df3, 23347.0, t3, st)

    # Must be SUPPRESSED
    assert len(sent_alerts) == 0
    assert st["last_oi_dominance"] == "PE_DOMINANT"

    # After freeze window at 13:16 (state is still PE_DOMINANT)
    t4 = datetime.combine(today, dtime(13, 16, 0))
    df4 = _make_chain_df(23300.0, 23347.0, ce_chg=10000.0, pe_chg=61000.0)
    scheduler.check_oi_dominance_alert(df4, 23347.0, t4, st)

    # Must NOT send retroactive alert
    assert len(sent_alerts) == 0


def _make_multi_strike_df(rows: list) -> pd.DataFrame:
    """Helper to build multi-strike option chain DataFrame."""
    records = []
    for r in rows:
        records.append({
            "strike": r["strike"],
            "spot": r.get("spot", 23350.0),
            "ce_oi": r.get("ce_oi", 10000.0),
            "pe_oi": r.get("pe_oi", 10000.0),
            "ce_chg_oi": r.get("ce_chg_oi", 0.0),
            "pe_chg_oi": r.get("pe_chg_oi", 0.0),
            "ce_ltp": 120.0,
            "pe_ltp": 80.0,
            "ce_chg": 5.0,
            "pe_chg": -4.0,
            "expiry": "2026-09-24",
        })
    return pd.DataFrame(records)


def test_check_institutional_barrier_alert_resistance(monkeypatch):
    """Verify alert fires when spot approaches solid resistance wall (max CE OI)."""
    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    t = datetime.combine(today, dtime(10, 15, 0))
    spot = 23480.0  # 20 pts away from 23500 CE

    df = _make_multi_strike_df([
        {"strike": 23400.0, "ce_oi": 30000.0, "pe_oi": 50000.0},
        {"strike": 23500.0, "ce_oi": 125000.0, "ce_chg_oi": 25000.0, "pe_oi": 10000.0},  # Max CE wall
        {"strike": 23600.0, "ce_oi": 40000.0, "pe_oi": 5000.0},
    ])

    st = {}
    scheduler.check_institutional_barrier_alert(df, spot, t, st)

    assert len(sent_alerts) == 1
    assert "Approaching Solid Resistance 23500 CE" in sent_alerts[0]
    assert "20 pts away" in sent_alerts[0]
    assert "+125k CE OI" in sent_alerts[0]
    assert "Writers Defending" in sent_alerts[0]

    # Re-polling immediately within cooldown (60m) must NOT re-alert
    scheduler.check_institutional_barrier_alert(df, spot, t + timedelta(minutes=1), st)
    assert len(sent_alerts) == 1


def test_check_institutional_barrier_alert_support(monkeypatch):
    """Verify alert fires when spot approaches solid support wall (max PE OI)."""
    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    t = datetime.combine(today, dtime(10, 20, 0))
    spot = 23220.0  # 20 pts away from 23200 PE

    df = _make_multi_strike_df([
        {"strike": 23100.0, "ce_oi": 5000.0, "pe_oi": 30000.0},
        {"strike": 23200.0, "ce_oi": 10000.0, "pe_oi": 140000.0, "pe_chg_oi": 35000.0},  # Max PE wall
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 40000.0},
    ])

    st = {}
    scheduler.check_institutional_barrier_alert(df, spot, t, st)

    assert len(sent_alerts) == 1
    assert "Approaching Solid Support 23200 PE" in sent_alerts[0]
    assert "20 pts away" in sent_alerts[0]
    assert "+140k PE OI" in sent_alerts[0]
    assert "Writers Defending" in sent_alerts[0]


def test_check_institutional_barrier_alert_suppression(monkeypatch):
    """Verify barrier alerts are suppressed during warmup, EOD, and freeze window."""
    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    df = _make_multi_strike_df([
        {"strike": 23500.0, "ce_oi": 125000.0, "pe_oi": 10000.0},
    ])

    # 1. Warmup (09:30)
    st = {}
    t_warmup = datetime.combine(today, dtime(9, 30, 0))
    scheduler.check_institutional_barrier_alert(df, 23485.0, t_warmup, st)
    assert len(sent_alerts) == 0

    # 2. EOD Unwind (15:10)
    t_eod = datetime.combine(today, dtime(15, 10, 0))
    scheduler.check_institutional_barrier_alert(df, 23485.0, t_eod, st)
    assert len(sent_alerts) == 0

    # 3. Buyer freeze window (12:30)
    t_freeze = datetime.combine(today, dtime(12, 30, 0))
    scheduler.check_institutional_barrier_alert(df, 23485.0, t_freeze, st)
    assert len(sent_alerts) == 0


def test_check_liquidity_trap_alert_bearish(monkeypatch):
    """Verify alert fires when price sweeps a resistance level by wick and reclaims back inside."""
    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = today

    df = _make_multi_strike_df([
        {"strike": 23380.0, "ce_oi": 80000.0, "pe_oi": 20000.0},
    ])

    # Mock extract_key_liquidity_levels to return known level
    import liquidity_sweep_engine as lse
    mock_level = lse.LiquidityLevelItem(
        name="PDH (Prev Day High)",
        price=23380.0,
        level_type="BUY_SIDE_LIQUIDITY",
        distance_pts=0.0,
        status="INTACT",
        badge_color="#ef4444"
    )
    monkeypatch.setattr(lse, "extract_key_liquidity_levels", lambda spot: [mock_level])

    # Build sequence of snapshots:
    # 10:00: at 23375
    # 10:02: wick sweeps to 23385 (+5 pts past 23380)
    # 10:05: reclaims back to 23374 (reclaimed below 23380)
    t1 = datetime.combine(today, dtime(10, 0, 0))
    t2 = datetime.combine(today, dtime(10, 2, 0))
    t3 = datetime.combine(today, dtime(10, 5, 0))

    scheduler._SESSION_SNAPSHOTS = [
        (t1, 23375.0, df),
        (t2, 23385.0, df),
        (t3, 23374.0, df),
    ]

    st = {}
    scheduler.check_liquidity_trap_alert(df, 23374.0, t3, st)

    assert len(sent_alerts) == 1
    assert "🪤 LIQUIDITY TRAP" in sent_alerts[0]
    assert "PDH (Prev Day High)" in sent_alerts[0]
    assert "Swept" in sent_alerts[0]
    assert "High 23385" in sent_alerts[0]
    assert "Reclaim 23374" in sent_alerts[0]
    assert "Breakout Buyers Trapped" in sent_alerts[0]
    assert "Bias: FADE (PUT)" in sent_alerts[0]

    # Cooldown check: Immediate second poll does not spam
    scheduler.check_liquidity_trap_alert(df, 23374.0, t3 + timedelta(minutes=1), st)
    assert len(sent_alerts) == 1


def test_check_liquidity_trap_alert_bullish(monkeypatch):
    """Verify alert fires when price sweeps a support level by wick and reclaims back inside."""
    sent_alerts = []
    monkeypatch.setattr(scheduler, "send_telegram_alert", lambda msg: sent_alerts.append(msg) or True)

    today = date(2026, 9, 17)
    scheduler._SESSION_SNAPSHOTS.clear()
    scheduler._SESSION_DATE = today

    df = _make_multi_strike_df([
        {"strike": 23200.0, "ce_oi": 20000.0, "pe_oi": 90000.0},
    ])

    import liquidity_sweep_engine as lse
    mock_level = lse.LiquidityLevelItem(
        name="PDL (Prev Day Low)",
        price=23200.0,
        level_type="SELL_SIDE_LIQUIDITY",
        distance_pts=0.0,
        status="INTACT",
        badge_color="#10b981"
    )
    monkeypatch.setattr(lse, "extract_key_liquidity_levels", lambda spot: [mock_level])

    # Sequence:
    # 09:56: at 23180 (t-9m)
    # 09:59: at 23190 (t-6m)
    # 10:02: wick sweeps to 23194 (-6 pts below 23200) (t-3m)
    # 10:05: reclaims back to 23220 (+20 pts above 23200, strong bounce >= 15 pts)
    t0 = datetime.combine(today, dtime(9, 56, 0))
    t1 = datetime.combine(today, dtime(9, 59, 0))
    t2 = datetime.combine(today, dtime(10, 2, 0))
    t3 = datetime.combine(today, dtime(10, 5, 0))

    scheduler._SESSION_SNAPSHOTS = [
        (t0, 23180.0, df),
        (t1, 23190.0, df),
        (t2, 23194.0, df),
        (t3, 23220.0, df),
    ]

    st = {}
    scheduler.check_liquidity_trap_alert(df, 23220.0, t3, st)

    assert len(sent_alerts) == 1
    assert "🪤 LIQUIDITY TRAP" in sent_alerts[0]
    assert "PDL (Prev Day Low)" in sent_alerts[0]
    assert "Swept" in sent_alerts[0]
    assert "Low 23190" in sent_alerts[0] or "Low 23194" in sent_alerts[0]
    assert "Reclaim 23220" in sent_alerts[0]
    assert "Breakout Sellers Trapped" in sent_alerts[0]
    assert "Bias: FADE (CALL)" in sent_alerts[0]

    # Verification: Weak bounce (< 15 pts, e.g. 23204) must be SUPPRESSED
    sent_alerts.clear()
    st.clear()
    scheduler._SESSION_SNAPSHOTS[-1] = (t3, 23204.0, df)
    scheduler.check_liquidity_trap_alert(df, 23204.0, t3, st)
    assert len(sent_alerts) == 0, "Weak bounce (< 15 pts) must be suppressed"


def test_format_floor_weakening_warning():
    import agt_briefing
    assert agt_briefing.format_floor_weakening_warning(0) is None
    assert agt_briefing.format_floor_weakening_warning(1) is None
    assert agt_briefing.format_floor_weakening_warning(2) == "⚠️ Floor weakening — 2nd consecutive warning"
    assert agt_briefing.format_floor_weakening_warning(3) == "⚠️ Floor weakening — 3rd consecutive warning"
    assert agt_briefing.format_floor_weakening_warning(4) == "⚠️ Floor weakening — 4th consecutive warning"


def test_telegram_briefing_floor_weakening_warning():
    import agt_briefing
    df = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 150000.0, "ce_chg_oi": 10000.0, "pe_chg_oi": -15000.0},
    ])
    now = datetime(2026, 9, 18, 10, 35)
    snaps = [(now - timedelta(minutes=15), 23315.0, df), (now, 23310.0, df)]
    rules = agt_briefing.load_briefing_rules()

    # With streak = 1 -> no warning line
    tg1 = agt_briefing.build_telegram_briefing(
        latest_ts=now,
        latest_spot=23310.0,
        latest_df=df,
        eff_dom="BALANCED",
        pcr_standing=1.1,
        pcr_trend="Falling ↓",
        d_pe_15m=-15000.0,
        d_ce_15m=10000.0,
        mult=65.0 / 100000.0,
        rules=rules,
        eval_snaps=snaps,
        is_stale=False,
        floor_weakening_streak=1,
    )
    assert "⚠️ Floor weakening" not in tg1

    # With streak = 2 -> 2nd consecutive warning line
    tg2 = agt_briefing.build_telegram_briefing(
        latest_ts=now,
        latest_spot=23310.0,
        latest_df=df,
        eff_dom="BALANCED",
        pcr_standing=1.1,
        pcr_trend="Falling ↓",
        d_pe_15m=-15000.0,
        d_ce_15m=10000.0,
        mult=65.0 / 100000.0,
        rules=rules,
        eval_snaps=snaps,
        is_stale=False,
        floor_weakening_streak=2,
    )
    assert "⚠️ Floor weakening — 2nd consecutive warning" in tg2


def test_finding_1_freeze_end_alert():
    import agt_briefing
    df_1130 = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 150000.0, "pe_oi": 200000.0},
        {"strike": 23350.0, "ce_oi": 250000.0, "pe_oi": 90000.0},
        {"strike": 23400.0, "ce_oi": 200000.0, "pe_oi": 60000.0},
    ])
    df_1315 = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 140000.0, "pe_oi": 250000.0},
        {"strike": 23350.0, "ce_oi": 200000.0, "pe_oi": 140000.0},
        {"strike": 23400.0, "ce_oi": 180000.0, "pe_oi": 70000.0},
    ])
    t_1130 = datetime(2026, 9, 18, 11, 30)
    t_1315 = datetime(2026, 9, 18, 13, 15)
    snaps = [(t_1130, 23310.0, df_1130), (t_1315, 23330.0, df_1315)]
    rules = agt_briefing.load_briefing_rules()

    alert = agt_briefing.build_freeze_end_alert(
        latest_ts=t_1315,
        latest_spot=23330.0,
        latest_df=df_1315,
        s1_strike=23300.0,
        r1_strike=23400.0,
        bias_word="BULLISH ▲",
        action_word="CE BUY SIGNAL — confirm 8 EMA on chart",
        eval_snaps=snaps,
        rules=rules,
    )

    assert "🔔 FREEZE WINDOW ENDED — Market open for trading" in alert
    assert "Spot: 23,330 | Bias: BULLISH ▲" in alert
    assert "Floor: 23,300 | Ceiling: 23,400" in alert
    assert "OI since 11:30: Puts +" in alert
    assert "Calls -" in alert
    assert "➡️ CE BUY SIGNAL — confirm 8 EMA on chart" in alert


def test_finding_2_approaching_ceiling_and_floor():
    import agt_briefing
    rules = agt_briefing.load_briefing_rules()
    mult = 65.0 / 100000.0

    df = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 160000.0},
        {"strike": 23400.0, "ce_oi": 230000.0, "pe_oi": 50000.0},
    ])

    r1_row = df[df["strike"] == 23400.0].iloc[0]
    s1_row = df[df["strike"] == 23300.0].iloc[0]

    # Spot approaching ceiling (23,388 is 12 pts away from 23,400)
    ceil_alert = agt_briefing.check_approaching_ceiling_floor(
        latest_spot=23388.0,
        latest_df=df,
        r1_row=r1_row,
        r2_row=None,
        s1_row=s1_row,
        s2_row=None,
        rules=rules,
        mult=mult,
    )
    assert ceil_alert is not None
    assert "⚠️ APPROACHING CEILING" in ceil_alert
    assert "Spot 23,388 → R1 23,400 CE" in ceil_alert
    assert "only 12 pts away" in ceil_alert
    assert "If rejection candle forms → PE buy opportunity" in ceil_alert
    assert "➡️ WATCH for rejection at 23,400" in ceil_alert

    # Spot approaching floor (23,308 is 8 pts away from 23,300)
    floor_alert = agt_briefing.check_approaching_ceiling_floor(
        latest_spot=23308.0,
        latest_df=df,
        r1_row=r1_row,
        r2_row=None,
        s1_row=s1_row,
        s2_row=None,
        rules=rules,
        mult=mult,
    )
    assert floor_alert is not None
    assert "⚠️ APPROACHING FLOOR" in floor_alert
    assert "Spot 23,308 → S1 23,300 PE" in floor_alert
    assert "only 8 pts away" in floor_alert
    assert "If bounce candle forms → CE buy opportunity" in floor_alert
    assert "➡️ WATCH for bounce at 23,300" in floor_alert


def test_finding_3_day_range_and_s1_liquidity_trap():
    import agt_briefing
    rules = agt_briefing.load_briefing_rules()
    now = datetime(2026, 9, 18, 14, 0)

    df = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 150000.0},
    ])
    snaps = [
        (datetime(2026, 9, 18, 9, 35), 23288.0, df),
        (datetime(2026, 9, 18, 9, 45), 23295.0, df),
        (now, 23365.0, df),
    ]

    tg = agt_briefing.build_telegram_briefing(
        latest_ts=now,
        latest_spot=23365.0,
        latest_df=df,
        eff_dom="PE_DOMINANT",
        pcr_standing=1.3,
        pcr_trend="Rising ↑",
        d_pe_15m=10000.0,
        d_ce_15m=-5000.0,
        mult=65.0 / 100000.0,
        rules=rules,
        eval_snaps=snaps,
        is_stale=False,
        day_low=23270.0,
        day_high=23389.0,
        s1_strike=23300.0,
        r1_strike=23400.0,
    )

    assert "📊 NIFTY 14:00 | Spot 23,365 | BULLISH ▲" in tg
    assert "Day range: Low 23,270 ↔ High 23,389" in tg

    # S1 Liquidity Trap trigger check:
    # Requires higher lows over last 3 snapshots (9m) and bounce >= 15 pts within 6m
    trap_snaps = [
        (datetime(2026, 9, 18, 9, 41), 23280.0, df),
        (datetime(2026, 9, 18, 9, 44), 23288.0, df),
        (datetime(2026, 9, 18, 9, 47), 23295.0, df),
        (datetime(2026, 9, 18, 9, 50), 23325.0, df),
    ]
    trap = agt_briefing.check_s1_liquidity_trap(
        eval_snaps=trap_snaps,
        s1_strike=23300.0,
        latest_spot=23325.0,
        rules=rules,
    )
    assert trap is not None
    assert "🪤 LIQUIDITY TRAP" in trap
    assert "S1 23,300 Swept" in trap
    assert "Low 23,288" in trap or "Low 23,295" in trap
    assert "Reclaim 23,325" in trap
    assert "Breakout Sellers Trapped" in trap
    assert "CE BUY SIGNAL" in trap

    # Weak bounce (< 15 pts, e.g. 23305) must be SUPPRESSED
    weak_trap = agt_briefing.check_s1_liquidity_trap(
        eval_snaps=trap_snaps,
        s1_strike=23300.0,
        latest_spot=23305.0,
        rules=rules,
    )
    assert weak_trap is None, "Weak bounce (< 15 pts) must be suppressed"


def test_finding_4_writers_ratio_early_session():
    import agt_briefing
    rules = agt_briefing.load_briefing_rules()
    assert rules["writers_ratio_early_session_threshold"] == 1.3
    assert rules["writers_ratio_early_session_until"] == "10:45"
    assert rules["writers_ratio_pe_threshold"] == 1.5


def test_finding_5_eod_summary():
    import agt_briefing
    rules = agt_briefing.load_briefing_rules()
    target_date = date(2026, 9, 18)
    now = datetime(2026, 9, 18, 15, 25)

    df = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 150000.0},
        {"strike": 23400.0, "ce_oi": 180000.0, "pe_oi": 60000.0},
    ])
    snaps = [
        (datetime(2026, 9, 18, 8, 19), 23270.6, df),
        (datetime(2026, 9, 18, 14, 55), 23389.15, df),
        (now, 23346.0, df),
    ]

    summary = agt_briefing.build_eod_summary(
        target_date=target_date,
        eval_snaps=snaps,
        all_snaps=snaps,
        rules=rules,
        latest_spot=23346.0,
        latest_df=df,
        r1_strike=23400.0,
        r2_strike=23500.0,
        s1_strike=23300.0,
        s2_strike=23200.0,
    )

    assert "📋 TODAY'S SUMMARY | 18 Sep" in summary
    assert "Day range: 23,270 → 23,389 (119 pts)" in summary
    assert "Floor held: 23,300 ✅" in summary
    assert "Ceiling held: 23,389 (R1 23,400) ✅" in summary
    assert "Best CE signal: 09:50 false breakdown ✅" in summary
    assert "Missed move: 13:15 breakout (freeze window)" in summary
    assert "App accuracy: Floors/ceilings correct ✅" in summary
    assert "Tomorrow watch: 23,300 and 23,400" in summary


def test_eod_no_trade_suppression():
    import agt_briefing
    rules = agt_briefing.load_briefing_rules()
    target_date = date(2026, 9, 18)
    now = datetime(2026, 9, 18, 15, 5)

    df = _make_multi_strike_df([
        {"strike": 23300.0, "ce_oi": 50000.0, "pe_oi": 250000.0},
        {"strike": 23400.0, "ce_oi": 180000.0, "pe_oi": 60000.0},
    ])
    snaps = [
        (datetime(2026, 9, 18, 14, 55), 23350.0, df),
        (datetime(2026, 9, 18, 15, 0), 23352.0, df),
        (now, 23355.0, df),
    ]

    tg = agt_briefing.build_telegram_briefing(
        latest_ts=now,
        latest_spot=23355.0,
        latest_df=df,
        eff_dom="PE_DOMINANT",
        pcr_standing=1.65,
        pcr_trend="Rising ↑",
        d_pe_15m=10000.0,
        d_ce_15m=-38000.0,
        mult=65.0 / 100000.0,
        rules=rules,
        eval_snaps=snaps,
        is_stale=False,
        day_low=23270.0,
        day_high=23389.0,
        s1_strike=23300.0,
        r1_strike=23400.0,
    )

    assert "CE BUY SIGNAL" not in tg
    assert "PE BUY SIGNAL" not in tg
    assert "➡️ EOD — no new trades after 15:00" in tg


def test_institutional_context_generation():
    import agt_briefing
    target_date = date(2026, 9, 18)
    rules = agt_briefing.load_briefing_rules()
    gex_data = {"regime": "SHORT"}
    iv_data = {"label": "CHEAP"}

    inst_block = agt_briefing.build_institutional_context(
        target_date=target_date,
        gex_data=gex_data,
        iv_data=iv_data,
        rules=rules,
    )

    assert inst_block is not None
    assert "🏛️ INSTITUTIONAL CONTEXT (prev session EOD)" in inst_block
    assert "PRO bias: BULLISH ▲" in inst_block
    assert "fut long 99th pct" in inst_block
    assert "FII hedge: HEAVY (PE long 99th pct — normal posture)" in inst_block
    assert "Retail: WRONG SIDE (CE long 99th pct extreme)" in inst_block
    assert "Spread: EXTREME (94th pct) — big move coming" in inst_block or "Spread: EXTREME (95th pct)" in inst_block
    assert "➡️ Smart money: BULLISH" in inst_block
    assert "➡️ Retail trap: Call buyers may get squeezed" in inst_block
    assert "➡️ STRONG CE BUY SETUP — all layers aligned" in inst_block
    assert "➡️ Watch for: Upside breakout OR call buyer washout" in inst_block


def test_institutional_context_rule7_combinations():
    import agt_briefing
    import participant_history as ph
    target_date = date(2026, 9, 18)
    rules = agt_briefing.load_briefing_rules()

    # Combo 1: PRO bullish + GEX long + IV cheap
    block1 = agt_briefing.build_institutional_context(
        target_date=target_date,
        gex_data={"regime": "LONG"},
        iv_data={"label": "CHEAP"},
        rules=rules,
    )
    assert "➡️ CE bias but rangebound — wait for wall break" in block1

    # Combo 2: Mock PRO bearish + any GEX
    orig_pcts = ph.compute_percentiles
    def mock_bearish_pcts(*args, **kwargs):
        df = orig_pcts(*args, **kwargs)
        # Flip PRO fut_long to 15th pct
        df.loc[(df["participant"] == "PRO") & (df["series"] == "fut_long"), "percentile_rank"] = 15.0
        return df

    try:
        ph.compute_percentiles = mock_bearish_pcts
        block2 = agt_briefing.build_institutional_context(
            target_date=target_date,
            gex_data={"regime": "SHORT"},
            iv_data={"label": "CHEAP"},
            rules=rules,
        )
        # In this mock: Retail is CE extreme (99th pct) and PRO is BEARISH (15th pct)
        # Retail extreme + PRO opposite = TRAP SETUP!
        assert "➡️ TRAP SETUP — retail will get squeezed" in block2
    finally:
        ph.compute_percentiles = orig_pcts


def test_institutional_context_briefing_timing():
    import agt_briefing
    from datetime import time

    with patch("store.load_day", side_effect=_ORIG_LOAD_DAY):
        # At 09:15: institutional context is included
        agt_briefing.build_briefing(date(2026, 9, 18), time(9, 15, 59))
        tg_0915 = agt_briefing.build_briefing.telegram_briefing
        assert tg_0915 is not None
        assert "🏛️ INSTITUTIONAL CONTEXT (prev session EOD)" in tg_0915
        assert tg_0915.startswith("🏛️ INSTITUTIONAL CONTEXT (prev session EOD)")
        assert "➡️ STRONG CE BUY SETUP — all layers aligned" in tg_0915

        # At 09:18: institutional context is NOT included (Rule 1 & 9)
        agt_briefing.build_briefing(date(2026, 9, 18), time(9, 18, 59))
        tg_0918 = agt_briefing.build_briefing.telegram_briefing
        assert tg_0918 is not None
        assert "🏛️ INSTITUTIONAL CONTEXT" not in tg_0918
        assert agt_briefing.build_briefing.institutional_context is None


def test_institutional_context_missing_data_skipped():
    import agt_briefing
    import participant_history as ph

    orig_load = ph.load
    try:
        ph.load = lambda: None
        inst_block = agt_briefing.build_institutional_context(
            target_date=date(2026, 9, 18),
            gex_data={"regime": "SHORT"},
            iv_data={"label": "CHEAP"},
        )
        assert inst_block is None
    finally:
        ph.load = orig_load




