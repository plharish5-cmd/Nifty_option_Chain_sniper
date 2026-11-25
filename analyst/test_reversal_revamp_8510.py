"""8 EMA Reversal revamp — unit tests (default OFF preserves legacy)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.ema_lane_lock_8510 import (  # noqa: E402
    LANE_PROTOTYPE,
    LANE_REVERSAL,
    lane_lock_blocks_entry,
    release_lane_lock,
    try_acquire_lane_lock,
)
from analyst.nse_spot_candles_8510 import ema4_ema8, resample_nse_5m  # noqa: E402
from analyst.reversal_revamp_8510 import (  # noqa: E402
    dynamic_8ema_on_frame,
    ema_separation_ok,
    load_reversal_cfg,
    needs_reversal_revamp,
)


def _synthetic_1m(n: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2026-11-10 09:15", periods=n, freq="1min", tz="Asia/Kolkata")
    close = 25000.0 + np.cumsum(np.random.RandomState(1).randn(n) * 2)
    return pd.DataFrame({"open": close, "high": close + 3, "low": close - 3, "close": close}, index=idx)


def test_resample_5m_and_dynamic_ema_matches_method():
    b5 = resample_nse_5m(_synthetic_1m(200))
    assert len(b5) >= 10
    live = float(b5["close"].iloc[-1]) + 5.0
    from analyst.mj_sir_stage_reversal_8510 import dynamic_8ema

    e4, e8 = dynamic_8ema_on_frame(b5, live)
    e4m, e8m = dynamic_8ema(b5, live)
    assert abs(e8 - e8m) < 0.01
    assert abs(e4 - e4m) < 0.01


def test_default_reversal_is_legacy_path():
    assert needs_reversal_revamp({}) is False
    assert needs_reversal_revamp({"reversal": {}}) is False
    rc = load_reversal_cfg({})
    assert rc["timeframe_mode"] == "15m" and rc["ema_separation_enabled"] is False


def test_ema_separation_blocks_tight_ribbon():
    assert ema_separation_ok(25000.0, 25002.0, 5.0) is False
    assert ema_separation_ok(25000.0, 24990.0, 5.0) is True


def test_lane_lock_does_not_block_other_lane_on_holder():
    lock: dict = {}
    assert try_acquire_lane_lock(lock, LANE_REVERSAL) is True
    blocked, _ = lane_lock_blocks_entry(lock, LANE_PROTOTYPE, open_position_count=0)
    assert blocked is False
    release_lane_lock(lock)


def test_lane_lock_blocks_when_position_open():
    lock: dict = {}
    blocked, why = lane_lock_blocks_entry(lock, LANE_REVERSAL, open_position_count=1)
    assert blocked and "Open position" in why


def test_needs_revamp_when_5m_mode():
    assert needs_reversal_revamp({"reversal": {"timeframe_mode": "5m"}}) is True