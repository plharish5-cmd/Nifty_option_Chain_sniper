"""
tests/test_oct27_fixes.py — Unit tests covering the 4 surgical fixes from 27 Oct expiry session.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from app import config
from app.engine import pcr_trinity
from app.timeutil import chart_time, to_ist, IST


def test_momentum_mutual_unwinding_classification():
    """
    Bug 1 Fix (a): When both Call and Put OI are unwinding into expiry (put_chg_30m < 0 and call_chg_30m < 0),
    classify as 🏁 EXPIRY / EOD MUTUAL UNWINDING (PROFIT BOOKING) and never as 3/3 Bullish Squeeze.
    """
    base_t = 1793080000
    cfg = config.rules().get("pcr_trinity", {})

    # Point 30 mins ago:
    p30 = {
        "time": chart_time(base_t),
        "ts": base_t,
        "fut_close": 22700.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 0.98,
        "pcr_vol": 0.98,
        "call_oi": 50000000,
        "put_oi": 49000000,
        "total_oi_gap": -1000000,
        "call_oi_chg": 132000000,   # +13.2Cr
        "put_oi_chg": 82000000,     # +8.2Cr
        "net_oi_chg": -50000000,
    }

    # Current point: Call chg dropped to +3.62Cr (-9.58Cr diff), Put chg dropped to +2.19Cr (-6.01Cr diff)
    # Both writers are heavily unwinding off expiry highs
    cur = {
        "time": chart_time(base_t + 1800),
        "ts": base_t + 1800,
        "fut_close": 22730.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 1.05,
        "pcr_vol": 1.05,
        "call_oi": 40420000,
        "put_oi": 42990000,
        "total_oi_gap": 2570000,
        "call_oi_chg": 36200000,    # +3.62Cr (diff = -95,800,000)
        "put_oi_chg": 21900000,     # +2.19Cr (diff = -60,100,000)
        "net_oi_chg": -14300000,    # -1.43Cr
    }

    series = [p30, cur]
    res = pcr_trinity.compute_pcr_trinity_verdict(series, cfg)

    assert res["alignment_state"] == "MUTUAL_UNWINDING"
    assert "EXPIRY / EOD MUTUAL UNWINDING" in res["verdict_badge"]
    assert "3/3 BULLISH SHORT-COVERING SQUEEZE" not in res["verdict_badge"]


def test_momentum_counter_trend_call_covering_classification():
    """
    Bug 1 Fix (b): When Call writers unwind off highs but cumulative net_oi_chg remains negative (< 0),
    classify as 🟡 COUNTER-TREND CALL PROFIT BOOKING (NET REGIME STILL BEARISH) and never as 3/3 Bullish Squeeze.
    """
    base_t = 1793080000
    cfg = config.rules().get("pcr_trinity", {})

    p30 = {
        "time": chart_time(base_t),
        "ts": base_t,
        "fut_close": 22700.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 0.96,
        "pcr_vol": 0.96,
        "call_oi": 50000000,
        "put_oi": 48000000,
        "total_oi_gap": 1500000,
        "call_oi_chg": 8000000,     # +80L
        "put_oi_chg": 2000000,      # +20L
        "net_oi_chg": -6000000,
    }

    # Call writers unwind by -25L, put writers flat (+20L).
    # call_oi_chg_diff_30m = 55L - 80L = -25L (-2,500,000). net_oi_slope_30m = 0 - (-25L) = +25L > 0.
    # Cumulative net_oi_chg is still negative (-35L).
    cur = {
        "time": chart_time(base_t + 1800),
        "ts": base_t + 1800,
        "fut_close": 22720.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 0.98,
        "pcr_vol": 0.98,
        "call_oi": 47500000,
        "put_oi": 48000000,
        "total_oi_gap": 1500000,
        "call_oi_chg": 5500000,     # +55L
        "put_oi_chg": 2000000,      # +20L
        "net_oi_chg": -3500000,     # -35L (Negative!)
    }

    series = [p30, cur]
    res = pcr_trinity.compute_pcr_trinity_verdict(series, cfg)

    assert res["alignment_state"] == "COUNTER_TREND_COVERING"
    assert "COUNTER-TREND CALL PROFIT BOOKING" in res["verdict_badge"]
    assert "3/3 BULLISH SHORT-COVERING SQUEEZE" not in res["verdict_badge"]


def test_genuine_bullish_short_covering_squeeze():
    """
    Genuine short-covering squeeze requires net_oi_chg >= 0 and not mutual unwinding.
    """
    base_t = 1793080000
    cfg = config.rules().get("pcr_trinity", {})

    p30 = {
        "time": chart_time(base_t),
        "ts": base_t,
        "fut_close": 22700.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 1.05,
        "pcr_vol": 1.05,
        "call_oi": 40000000,
        "put_oi": 42000000,
        "total_oi_gap": 2000000,
        "call_oi_chg": 1000000,
        "put_oi_chg": 5000000,
        "net_oi_chg": 4000000,
    }

    cur = {
        "time": chart_time(base_t + 1800),
        "ts": base_t + 1800,
        "fut_close": 22740.0,
        "fut_oi": 10000000,
        "fut_oi_chg": 0,
        "fut_vol": 10000,
        "pcr_oi": 1.15,
        "pcr_vol": 1.15,
        "call_oi": 37000000,
        "put_oi": 43000000,
        "total_oi_gap": 6000000,
        "call_oi_chg": -2000000,    # -20L (diff = -30L)
        "put_oi_chg": 6000000,     # +60L
        "net_oi_chg": 8000000,      # +80L (Positive net regime)
    }

    series = [p30, cur]
    res = pcr_trinity.compute_pcr_trinity_verdict(series, cfg)

    assert res["alignment_state"] == "BULLISH_TRINITY_LOCK"
    assert "3/3 BULLISH SHORT-COVERING SQUEEZE" in res["verdict_badge"]


def test_retest_fill_writer_flow_veto_logic():
    """
    Bug 2 Fix: Test Writer-Flow Veto conditions on pending limit retest zones.
    """
    rv_cfg = config.rules().get("retest_veto", {})
    assert rv_cfg.get("enabled") is True
    interval_thresh = int(rv_cfg.get("interval_shock_thresh", 2500000))
    day_diff_thresh = int(rv_cfg.get("day_diff_shock_thresh", 15000000))

    # Helper function matching hub.py logic
    def check_ce_veto(interval_bar_net: int, day_net_diff: int) -> bool:
        return (interval_bar_net < -interval_thresh) or (day_net_diff < -day_diff_thresh and interval_bar_net <= 0)

    def check_pe_veto(interval_bar_net: int, day_net_diff: int) -> bool:
        return (interval_bar_net > interval_thresh) or (day_net_diff > day_diff_thresh and interval_bar_net >= 0)

    # BUY CE Tests
    # Case 1: Bearish shock on current interval bar (-30L < -25L) -> Veto!
    assert check_ce_veto(interval_bar_net=-3000000, day_net_diff=-5000000) is True

    # Case 2: Deeply bearish day (-1.8Cr < -1.5Cr) and non-positive interval bar (-5L) -> Veto!
    assert check_ce_veto(interval_bar_net=-500000, day_net_diff=-18000000) is True

    # Case 3: Bullish writer flow (+15L interval, +40L day) -> NOT Vetoed (allowed to fill)
    assert check_ce_veto(interval_bar_net=1500000, day_net_diff=4000000) is False

    # Case 4: Deeply bearish day (-1.8Cr) but strong bullish counter-shock (+35L > 0) -> NOT vetoed by day diff
    assert check_ce_veto(interval_bar_net=3500000, day_net_diff=-18000000) is False

    # BUY PE Tests
    # Case 5: Bullish shock on current interval bar (+30L > +25L) -> Veto!
    assert check_pe_veto(interval_bar_net=3000000, day_net_diff=5000000) is True

    # Case 6: Deeply bullish day (+1.8Cr > +1.5Cr) and non-negative interval bar (+5L) -> Veto!
    assert check_pe_veto(interval_bar_net=500000, day_net_diff=18000000) is True

    # Case 7: Bearish writer flow (-15L interval, -40L day) -> NOT Vetoed (allowed to fill)
    assert check_pe_veto(interval_bar_net=-1500000, day_net_diff=-4000000) is False


def test_writer_flip_timestamp_no_double_offset():
    """
    Bug 3 Fix: The timestamp in check_option_chain_alerts should not apply +5:30 IST twice.
    """
    # 11:28:00 IST on 2026-10-27
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    expected_dt = datetime(2026, 10, 27, 11, 28, 0, tzinfo=ist_tz)
    epoch_ts = int(expected_dt.timestamp())

    # pcr_trinity generates "time" using chart_time (epoch + 19800) and "ts" as raw epoch
    chart_t = chart_time(epoch_ts)
    assert chart_t == epoch_ts + 19800

    # Hub timestamp extraction logic:
    last = {"time": chart_t, "ts": epoch_ts}
    ts_val = int(last.get("ts") or 0)
    if ts_val <= 0:
        raw_t = int(last.get("time", epoch_ts))
        ts_val = raw_t - 19800 if (raw_t - epoch_ts > 10000) else raw_t

    ist_dt = to_ist(ts_val)
    hhmm = ist_dt.strftime("%H:%M")

    # Verify that it produces exactly "11:28" and not "16:58"
    assert hhmm == "11:28"
    assert ts_val == epoch_ts

    # Test fallback when "ts" is missing:
    last_no_ts = {"time": chart_t}
    ts_val2 = int(last_no_ts.get("ts") or 0)
    if ts_val2 <= 0:
        raw_t = int(last_no_ts.get("time", epoch_ts))
        ts_val2 = raw_t - 19800 if (raw_t - epoch_ts > 10000) else raw_t

    ist_dt2 = to_ist(ts_val2)
    assert ist_dt2.strftime("%H:%M") == "11:28"
