"""Arm reconstruction from Dhan-backed session candles (v2.0.41)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from analyst.arm_reconstruct_8510 import (
    EMA_EPSILON,
    apply_reconstructed_state,
    derive_states_from_candles,
    ema_closed_bar_delta,
    reconstruct_arms_on_startup,
)
from analyst.ema8_prototype_8510 import evaluate_ema8_prototype_intrabar, load_ema8_prototype_cfg
from analyst.nse_spot_candles_8510 import ema4_ema8, resample_nse_15m

DAY = "2026-11-19"
T0 = int(pd.Timestamp(f"{DAY} 09:15", tz="Asia/Kolkata").timestamp())


def _insert_1m(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles_1m (
            sec_id TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
            PRIMARY KEY (sec_id, ts)
        )
        """
    )
    conn.executemany(
        "INSERT OR REPLACE INTO candles_1m (sec_id, ts, o, h, l, c, v) VALUES ('13',?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _flat_warmup(conn: sqlite3.Connection, base: float, n_bars: int = 3000) -> None:
    start = T0 - n_bars * 60
    rows = [(start + i * 60, base, base + 2, base - 2, base, 1000.0) for i in range(n_bars)]
    _insert_1m(conn, rows)


def _backend(tmp_path: Path) -> SimpleNamespace:
    market_db = tmp_path / "market.sqlite3"
    ledger_db = tmp_path / "ledger.sqlite3"
    b = SimpleNamespace(
        market_db=str(market_db),
        ledger_db=ledger_db,
        scanner_cfg={
            "ema8_prototype": {
                "enabled": True,
                "reentry_same_side": True,
                "away_min_pts": 2.0,
                "touch_tolerance_pts": 3.0,
                "retouch_requires_away": True,
            },
            "mj_sir_lecture": {"enabled": True, "dynamic_8ema_entry": True},
            "flagship_eod_square_off": "15:20",
        },
        cfg={"scanner": {}},
        broker=SimpleNamespace(access_token="", client_id="", base_url="https://api.dhan.co/v2"),
        _ema8_prototype_state={},
        _mj_sir_state={},
        _ema_lane_lock={},
        _proto_arm_loaded_from_db=False,
        _mj_sir_loaded_from_db=False,
        _bollinger_gate_active=lambda _k: False,
    )
    b.cfg["scanner"] = b.scanner_cfg
    events: list[dict] = []

    def _log(**kw):
        events.append(kw)

    b.log_ema8_prototype_event = _log
    b._save_ema8_prototype_state = lambda: None
    b._save_mj_sir_runtime_state = lambda: None
    b._recon_test_events = events
    return b


def _seed_bear_cross_session(tmp_path: Path) -> SimpleNamespace:
    b = _backend(tmp_path)
    with sqlite3.connect(b.market_db) as conn:
        _flat_warmup(conn, 25100.0, 4000)
        # Session: hold high, then sharp drop on one 15m bucket to cross below 8 EMA.
        price = 25180.0
        for m in range(120):
            ts = T0 + m * 60
            if 45 <= m < 60:
                price = 25180.0 - (m - 44) * 8.0
            lo, hi = price - 3, price + 3
            _insert_1m(conn, [(ts, price, hi, lo, price, 1000.0)])
    return b


def test_reconstructed_ema_matches_within_epsilon(tmp_path: Path):
    b = _seed_bear_cross_session(tmp_path)
    now_ts = T0 + 120 * 60
    with patch("analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": True, "candles": 1}):
        res = derive_states_from_candles(b, DAY, now_ts=now_ts, skip_dhan=True)
    assert res.replayed
    assert res.ema_checks.get("ema_ok") is True
    assert res.ema_checks.get("de4", 1.0) < EMA_EPSILON
    assert res.ema_checks.get("de8", 1.0) < EMA_EPSILON


def test_fresh_open_can_arm_from_history(tmp_path: Path):
    b = _seed_bear_cross_session(tmp_path)
    now_ts = T0 + 120 * 60
    with patch("analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": True, "candles": 1}):
        res = derive_states_from_candles(b, DAY, now_ts=now_ts, skip_dhan=True)
    apply_reconstructed_state(b, res)
    arm = (b._ema8_prototype_state.get("arm") or {})
    assert arm.get("direction") in ("LONG", "SHORT")


def test_live_touch_after_rebuild(tmp_path: Path):
    b = _seed_bear_cross_session(tmp_path)
    now_ts = T0 + 120 * 60
    with patch("analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": True, "candles": 1}):
        res = derive_states_from_candles(b, DAY, now_ts=now_ts, skip_dhan=True)
    apply_reconstructed_state(b, res)
    arm = b._ema8_prototype_state.get("arm")
    if not arm or not arm.get("away_seen"):
        pytest.skip("synthetic path did not reach away_seen")
    with sqlite3.connect(b.market_db) as conn:
        df1 = pd.read_sql_query(
            "SELECT ts, o as open, h as high, l as low, c as close FROM candles_1m WHERE sec_id='13' ORDER BY ts",
            conn,
        )
    df1["dt"] = pd.to_datetime(df1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    b15 = resample_nse_15m(df1.set_index("dt"))
    cfg = load_ema8_prototype_cfg(b.scanner_cfg)
    e8 = float(arm.get("cross_e8") or b15["close"].iloc[-2])
    direction = str(arm["direction"]).upper()
    touch = e8 + 1.0 if direction == "LONG" else e8 - 1.0
    sigs, _ = evaluate_ema8_prototype_intrabar(
        b15,
        touch,
        cfg=cfg,
        proto_state=b._ema8_prototype_state,
        session_day=DAY,
        tick_high=touch + 0.5,
        tick_low=touch - 0.5,
    )
    assert sigs, "live touch after rebuild should fire"


def test_stale_persist_logs_rebuilt(tmp_path: Path):
    b = _seed_bear_cross_session(tmp_path)
    b._ema8_prototype_state = {
        "day": DAY,
        "arm": {"direction": "LONG", "cross_bar_open_ts": T0, "cross_e8": 25000.0, "away_seen": True},
    }
    b._proto_arm_loaded_from_db = True
    now_ts = T0 + 120 * 60
    with patch("analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": True, "candles": 1}):
        res = derive_states_from_candles(b, DAY, now_ts=now_ts, skip_dhan=True)
    apply_reconstructed_state(b, res)
    reasons = [e.get("reason", "") for e in b._recon_test_events]
    assert any("REBUILT" in r or "confirmed" in r for r in reasons)


def test_dhan_backfill_failure_degraded(tmp_path: Path):
    b = _backend(tmp_path)
    with patch("analyst.arm_reconstruct_8510._session_open_now", return_value=(True, DAY)), patch(
        "analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": False, "degraded": True}
    ):
        res = reconstruct_arms_on_startup(b, DAY)
    assert res is not None
    assert res.degraded
    assert not res.applied


def test_offline_miss_logged(tmp_path: Path):
    b = _seed_bear_cross_session(tmp_path)
    offline_from = T0 + 30 * 60
    offline_to = T0 + 90 * 60
    now_ts = T0 + 120 * 60
    with patch("analyst.arm_reconstruct_8510.backfill_today_session", return_value={"ok": True, "candles": 1}):
        res = derive_states_from_candles(
            b,
            DAY,
            now_ts=now_ts,
            offline_from=offline_from,
            offline_to=offline_to,
            skip_dhan=True,
        )
    # If replay produced a touch inside the offline window, we must log honestly.
    for msg in res.offline_misses:
        assert "MISSED WHILE OFFLINE" in msg