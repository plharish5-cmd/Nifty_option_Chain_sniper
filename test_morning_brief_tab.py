"""
test_morning_brief_tab.py — Unit tests for morning_brief_tab.py
"""

from datetime import date, datetime
import pandas as pd
import pytest

import morning_brief_tab
import store


def test_morning_brief_tab_data_loading():
    target_date, latest_ts, latest_spot, latest_df, all_snaps, rules = morning_brief_tab._get_active_session_data()
    assert isinstance(target_date, date)
    assert isinstance(latest_ts, datetime)
    assert latest_spot > 0
    assert isinstance(rules, dict)


def test_morning_brief_tab_smart_money():
    rules = {"window_sessions": 65, "institutional_context": {"pro_bias": {"bullish_pct": 70.0, "bearish_pct": 30.0}}}
    valid_biases = {
        "BEARISH (PE Bias)",
        "BULLISH (CE Bias)",
        "TRAP SETUP (Retail Squeeze)",
        "TRAP SETUP (Retail Short Squeeze)",
        "NEUTRAL",
    }
    sm_sep18 = morning_brief_tab._compute_smart_money(date(2026, 9, 18), rules)
    assert "pro_str" in sm_sep18
    assert "fii_fut_str" in sm_sep18
    assert "fii_hedge_str" in sm_sep18
    assert "retail_str" in sm_sep18
    assert "spread_str" in sm_sep18
    assert "day_bias" in sm_sep18
    assert sm_sep18["day_bias"] in valid_biases
    assert sm_sep18["day_bias"] == "BULLISH (CE Bias)"

    sm_oct01 = morning_brief_tab._compute_smart_money(date(2026, 10, 1), rules)
    assert "fii_fut_str" in sm_oct01
    assert "fii_hedge_str" in sm_oct01
    assert sm_oct01["day_bias"] in valid_biases
    assert sm_oct01["day_bias"] == "TRAP SETUP (Retail Squeeze)"


def test_morning_brief_tab_conditions():
    snaps = store.load_day(date(2026, 9, 18))
    assert len(snaps) > 0
    latest_ts, latest_spot, latest_df = snaps[-1]
    rules = {"window_sessions": 65}
    cond, s1_row, r1_row, mult = morning_brief_tab._compute_conditions(latest_df, latest_spot, latest_ts, date(2026, 9, 18), rules)
    assert "gex_str" in cond
    assert "iv_str" in cond
    assert "floor_str" in cond
    assert "ceiling_str" in cond
    assert s1_row is not None
    assert r1_row is not None
    # Verify ceiling is capped to spot + 500 points
    assert float(r1_row["strike"]) <= latest_spot + 500.0
    assert float(s1_row["strike"]) >= latest_spot - 500.0


def test_corrupt_snapshot_resilience(tmp_path):
    """Verify that corrupt or partially-written snappy parquet files are skipped gracefully."""
    import config
    old_snap_dir = config.SNAPSHOT_DIR
    config.SNAPSHOT_DIR = tmp_path
    store.enable_writes("pytest")
    try:
        today_str = date.today().strftime("%Y-%m-%d")
        day_dir = tmp_path / today_str
        day_dir.mkdir(parents=True)

        # 1. Valid snapshot
        df = pd.DataFrame([{"strike": 23400.0, "ce_oi": 100, "pe_oi": 200, "ce_iv": 15.0, "pe_iv": 16.0}])
        store.save_snapshot(df, 23400.0, datetime.now(), base_dir=tmp_path)

        # 2. Corrupt snappy / truncated parquet snapshot
        bad_meta = day_dir / "091500.meta.json"
        bad_meta.write_text('{"spot": 23350.0, "timestamp": "2026-09-24T09:15:00"}')
        bad_pq = day_dir / "091500.parquet"
        bad_pq.write_bytes(b"PAR1corrupt_snappy_compressed_bytes_here")

        # 3. Ensure store.load_day returns the valid snapshot without raising OSError
        snaps = store.load_day()
        assert len(snaps) == 1
        assert snaps[0][1] == 23400.0

        # 4. Ensure morning_brief_tab._get_active_session_data succeeds without crashing
        t_d, l_ts, spot, l_df, all_s, rules = morning_brief_tab._get_active_session_data()
        assert len(all_s) == 1
        assert spot == 23400.0
    finally:
        store.disable_writes()
        config.SNAPSHOT_DIR = old_snap_dir

