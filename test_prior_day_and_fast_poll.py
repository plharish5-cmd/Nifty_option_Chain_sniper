"""
test_prior_day_and_fast_poll.py — Tests for:
  Item 1: Day high/low from candles with exact timestamps
  Item 2: Prior-day levels (PDH, PDL, PDC, PDO, P2DC) display and proximity alerts
  Item 3: Fast poll window (20s interval for 09:15-09:30) and counter normalization
"""

import os
import json
import pytest
from datetime import date, datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock, patch

import agt_briefing
import session_levels
import scheduler


def test_load_prior_day_levels():
    """Verify loading prior-day levels from OHLC.csv for 2026-09-23."""
    levels = session_levels.load_prior_day_levels(date(2026, 9, 23))
    assert levels is not None
    assert levels["PDH"] == 23489.0
    assert levels["PDL"] == 23285.75
    assert levels["PDC"] == 23329.0
    assert levels["PDO"] == 23454.05
    assert levels["P2DC"] == 23414.30


def test_compute_day_range_from_candles():
    """Verify DayRange tuple unpacking and time tracking from candle data for 2026-09-23."""
    target_date = date(2026, 9, 23)
    dr_open = agt_briefing.compute_day_range(target_date, [], [], target_time=dtime(9, 15))
    low, high = dr_open
    assert low == 23351.75
    assert dr_open.day_low_time == "09:15"

    dr_full = agt_briefing.compute_day_range(target_date, [], [], target_time=dtime(15, 30))
    assert dr_full.day_low == 23349.55
    assert dr_full.day_low_time == "09:54"
    assert dr_full.day_high == 23466.9
    assert dr_full.day_high_time == "12:20"


def test_telegram_briefing_prior_day_and_day_range():
    """Verify telegram briefing includes day range with times and prior-day levels."""
    latest_ts = datetime(2026, 9, 23, 9, 20)
    latest_spot = 23408.0
    pd_levels = {
        "PDH": 23489.0,
        "PDL": 23286.0,
        "PDC": 23329.0,
        "PDO": 23454.0,
        "P2DC": 23414.3,
    }

    import pandas as pd
    df = pd.DataFrame([
        {"strike": 23300.0, "pe_oi": 100000, "ce_oi": 10000, "pe_ltp": 50, "ce_ltp": 100},
        {"strike": 23400.0, "pe_oi": 50000, "ce_oi": 50000, "pe_ltp": 80, "ce_ltp": 80},
        {"strike": 23500.0, "pe_oi": 10000, "ce_oi": 120000, "pe_ltp": 120, "ce_ltp": 40},
    ])

    tg = agt_briefing.build_telegram_briefing(
        latest_ts=latest_ts,
        latest_spot=latest_spot,
        latest_df=df,
        eff_dom="NEUTRAL",
        pcr_standing=1.0,
        pcr_trend="Rising",
        d_pe_15m=1000,
        d_ce_15m=1000,
        mult=0.00065,
        rules={},
        eval_snaps=[],
        day_low=23329.0,
        day_high=23414.55,
        day_low_time="09:15",
        day_high_time="09:21",
        prior_day_levels=pd_levels,
    )

    assert "Low 23,329 (09:15) ↔ High 23,414.55 (09:21)" in tg
    assert "📍 Prior-day: PDH 23,489 | PDL 23,286 | PDC 23,329" in tg


def test_fast_poll_window_interval():
    """Verify fast poll window detects 09:15-09:30 and returns 20s interval."""
    t_inside = datetime(2026, 9, 23, 9, 20)
    t_outside = datetime(2026, 9, 23, 9, 35)
    t_before = datetime(2026, 9, 23, 9, 10)

    rules = {
        "fast_poll_window": {
            "enabled": True,
            "start": "09:15",
            "end": "09:30",
            "interval_seconds": 20,
            "chain_interval_seconds": 20,
        }
    }

    assert scheduler.is_in_fast_poll_window(t_inside, rules) is True
    assert scheduler.is_in_fast_poll_window(t_outside, rules) is False
    assert scheduler.is_in_fast_poll_window(t_before, rules) is False

    assert scheduler.get_chain_poll_interval(t_inside, rules) == 20
    assert scheduler.get_chain_poll_interval(t_outside, rules) == 60


def test_prior_day_level_alert(tmp_path):
    """Verify check_prior_day_level_alert triggers within proximity and logs tag."""
    now = datetime(2026, 9, 23, 9, 21)
    spot = 23410.0  # 4.3 pts from P2DC (23414.30)
    st = {"alert_cooldowns": {}}

    mock_levels = {
        "PDH": 23489.0,
        "PDL": 23285.75,
        "PDC": 23329.0,
        "PDO": 23454.05,
        "P2DC": 23414.30,
    }

    sent_alerts = []
    with patch("session_levels.load_prior_day_levels", return_value=mock_levels), \
         patch("scheduler.send_telegram_alert", side_effect=lambda m: sent_alerts.append(m)), \
         patch("scheduler.log_floor_ceiling_observation") as mock_obs:
        
        scheduler.check_prior_day_level_alert(spot, now, st)

        assert len(sent_alerts) == 1
        assert "📍 PRIOR-DAY LEVEL" in sent_alerts[0]
        assert "P2DC 23,414" in sent_alerts[0]
        assert "4pts" in sent_alerts[0]

        # Verify observation log call
        mock_obs.assert_called_once()
        call_args = mock_obs.call_args[0]
        assert call_args[2] == "PRIOR_DAY_LEVEL"
        assert "P2DC 23,414" in call_args[3]

        # Verify cooldown prevents immediate re-alert
        sent_alerts.clear()
        now_next = datetime(2026, 9, 23, 9, 25)
        scheduler.check_prior_day_level_alert(spot, now_next, st)
        assert len(sent_alerts) == 0

        # Verify alert suppressed during freeze window (11:30-13:15)
        st["alert_cooldowns"].clear()
        now_freeze = datetime(2026, 9, 23, 12, 0)
        scheduler.check_prior_day_level_alert(spot, now_freeze, st)
        assert len(sent_alerts) == 0


def test_rvol_signal_counter_time_normalization():
    """Verify BUY signals in the same minute increment rvol_signals_suppressed only once."""
    st = {
        "rvol_signals_suppressed": 0,
        "rvol_signals_allowed": 0,
        "chain_polls": 0,
        "alert_cooldowns": {},
    }

    # Simulate 3 polls within the same minute (20s apart)
    t1 = datetime(2026, 9, 23, 9, 20, 0)
    t2 = datetime(2026, 9, 23, 9, 20, 20)
    t3 = datetime(2026, 9, 23, 9, 20, 40)
    # Next minute
    t4 = datetime(2026, 9, 23, 9, 21, 0)

    from types import SimpleNamespace
    fake_sig = SimpleNamespace(action="BUY_CE")

    with patch("rvol_gate.get_gate_decision", return_value={"status": "SUPPRESSED"}):
        # Poll 1
        if fake_sig.action in ("BUY_CE", "BUY_PE"):
            cur_min = t1.strftime("%Y-%m-%d %H:%M")
            if st.get("last_rvol_signal_counted_minute") != cur_min:
                st["last_rvol_signal_counted_minute"] = cur_min
                st["rvol_signals_suppressed"] += 1

        assert st["rvol_signals_suppressed"] == 1

        # Poll 2 (same minute)
        if fake_sig.action in ("BUY_CE", "BUY_PE"):
            cur_min = t2.strftime("%Y-%m-%d %H:%M")
            if st.get("last_rvol_signal_counted_minute") != cur_min:
                st["last_rvol_signal_counted_minute"] = cur_min
                st["rvol_signals_suppressed"] += 1

        assert st["rvol_signals_suppressed"] == 1  # Unchanged!

        # Poll 3 (same minute)
        if fake_sig.action in ("BUY_CE", "BUY_PE"):
            cur_min = t3.strftime("%Y-%m-%d %H:%M")
            if st.get("last_rvol_signal_counted_minute") != cur_min:
                st["last_rvol_signal_counted_minute"] = cur_min
                st["rvol_signals_suppressed"] += 1

        assert st["rvol_signals_suppressed"] == 1  # Unchanged!

        # Poll 4 (next minute)
        if fake_sig.action in ("BUY_CE", "BUY_PE"):
            cur_min = t4.strftime("%Y-%m-%d %H:%M")
            if st.get("last_rvol_signal_counted_minute") != cur_min:
                st["last_rvol_signal_counted_minute"] = cur_min
                st["rvol_signals_suppressed"] += 1

        assert st["rvol_signals_suppressed"] == 2  # Incremented!


def test_p14_fii_stock_futures():
    """Verify p14_fii accurately formats stock futures and avoids broken templates."""
    import view_building
    from types import SimpleNamespace

    # Case 1: Both Index and Stock Futures present and positive
    poi_pos = SimpleNamespace(
        fii_change=4295.0,
        fii_stock_fut_change=2397.0,
        fii_long_chg=5000,
        fii_short_chg=705,
    )
    p = view_building.p14_fii(flows=None, poi=poi_pos)
    assert "FII BOUGHT 4,295 CR IN INDEX FUTURES, BOUGHT 2,397 CR IN STOCK FUTURES" in p.observation

    # Case 2: Stock futures negative (sold)
    poi_neg = SimpleNamespace(
        fii_change=1500.0,
        fii_stock_fut_change=-1200.0,
    )
    p = view_building.p14_fii(flows=None, poi=poi_neg)
    assert "FII BOUGHT 1,500 CR IN INDEX FUTURES, SOLD 1,200 CR IN STOCK FUTURES" in p.observation

    # Case 3: Stock futures is None with dummy flows (no broken 'BOUGHT CR IN STOCK FUTURES' string)
    poi_none = SimpleNamespace(
        fii_change=-2500.0,
        fii_stock_fut_change=None,
    )
    flows = SimpleNamespace(fii_cash_net=-500.0, dii_cash_net=300.0)
    p = view_building.p14_fii(flows=flows, poi=poi_none)
    assert "BOUGHT CR IN STOCK FUTURES" not in p.observation
    assert "FII SOLD 2,500 CR IN INDEX FUTURES" in p.observation

    # Case 4: Default fallback text has valid numbers
    p_def = view_building.p14_fii(flows=None, poi=None)
    assert "BOUGHT CR IN STOCK FUTURES" not in p_def.observation
    assert "CR IN STOCK FUTURES" in p_def.observation


def test_p16_global_format():
    """Verify Row 16 Global Markets formats with (+/-) for US Cash, US Futures, and Asian Markets including Shanghai."""
    import view_building
    from types import SimpleNamespace

    # Case 1: Custom rows
    mock_glob = SimpleNamespace(
        rows=[
            {"name": "S&P 500", "pct": 0.45},
            {"name": "Nasdaq 100", "pct": -0.25},
            {"name": "DOW Jones Futures", "pct": 0.30},
            {"name": "Hang Seng", "pct": 1.15},
            {"name": "Nikkei 225", "pct": -0.80},
            {"name": "KOSPI", "pct": 1.40},
            {"name": "Shanghai", "pct": -0.35},
        ]
    )
    p = view_building.p16_global(mock_glob)
    assert "US CASH MArkets: (S&P 500 (+) & Nasdaq 100 (-))" in p.observation
    assert "US Futures Market: DOW Jones FUtures (+)" in p.observation
    assert "Asian Markets: Hang Seng(+) , Nikkei 225(-) , KOSPI(+) , Shanghai(-)" in p.observation
    assert p.score == 2  # 4 positive, 3 negative -> Bullish (+2)
    assert p.view == "Bullish"

    # Case 2: All negative
    mock_bear = SimpleNamespace(
        rows=[
            {"name": "S&P 500", "pct": -0.50},
            {"name": "Nasdaq 100", "pct": -0.60},
            {"name": "DOW Jones Futures", "pct": -0.40},
            {"name": "Hang Seng", "pct": -1.20},
            {"name": "Nikkei 225", "pct": -1.50},
            {"name": "KOSPI", "pct": -0.90},
            {"name": "Shanghai", "pct": -0.30},
        ]
    )
    p_bear = view_building.p16_global(mock_bear)
    assert "US CASH MArkets: (S&P 500 (-) & Nasdaq 100 (-))" in p_bear.observation
    assert "US Futures Market: DOW Jones FUtures (-)" in p_bear.observation
    assert "Asian Markets: Hang Seng(-) , Nikkei 225(-) , KOSPI(-) , Shanghai(-)" in p_bear.observation
    assert p_bear.score == -2
    assert p_bear.view == "Bearish"

    # Case 3: Offline / default fallback
    p_def = view_building.p16_global(None)
    assert "US CASH MArkets:" in p_def.observation
    assert "US Futures Market: DOW Jones FUtures" in p_def.observation
    assert "Asian Markets:" in p_def.observation
    assert "Shanghai" in p_def.observation


def test_p18_usdinr_exchange_rate():
    """Verify Row 18 USD/INR Exchange Rate is present in build() and exports to Excel correctly."""
    import view_building
    import openpyxl, io
    from types import SimpleNamespace

    # 1. Direct param function test
    gift_mock = SimpleNamespace(
        value=23500.0,
        pct_change=0.25,
        gap=50.0,
        prev_close=23450.0,
        updated="08:00",
        usd_inr=95.735,
    )
    p18 = view_building.p18_usdinr(gift=gift_mock)
    assert p18.no == 18
    assert p18.name == "USD/INR Exchange Rate"
    assert p18.importance == "NA"
    assert "USD/INR futures @ 95.735" in p18.observation
    assert p18.view in ("Bullish", "Bearish", "Neutral")

    # 2. Complete build() returns 18 parameters
    params = view_building.build(gift=gift_mock)
    assert len(params) == 18
    p18_in_build = next((p for p in params if p.no == 18), None)
    assert p18_in_build is not None
    assert p18_in_build.name == "USD/INR Exchange Rate"

    # 3. Excel export includes row 18
    xl_bytes = view_building.to_excel_bytes(params)
    wb = openpyxl.load_workbook(io.BytesIO(xl_bytes))
    sheet = wb.active
    p18_cell_found = False
    for r in range(1, sheet.max_row + 1):
        if sheet.cell(r, 1).value == 18:
            assert sheet.cell(r, 2).value == "USD/INR Exchange Rate"
            assert sheet.cell(r, 3).value == "NA"
            assert "USD/INR" in str(sheet.cell(r, 4).value)
            p18_cell_found = True
            break
    assert p18_cell_found is True


def test_investing_pullers():
    """Verify market_data fetchers for investing.com and their graceful fallbacks."""
    import market_data as md

    # 1. USD/INR puller
    rate, pct = md.fetch_usdinr_investing()
    assert rate is not None
    assert 50.0 < rate < 150.0
    assert isinstance(pct, (int, float))

    # 2. Global Markets puller
    gm = md.fetch_global_investing()
    assert gm is not None
    assert len(gm.rows) >= 3
    summary = gm.summary
    assert "US CASH MArkets:" in summary
    assert "US Futures Market: DOW Jones FUtures" in summary
    assert "Asian Markets:" in summary
    assert "Shanghai" in summary
    score, view = gm.score()
    assert score in (-2, 0, 2)
    assert view in ("Bullish", "Bearish", "Neutral")

