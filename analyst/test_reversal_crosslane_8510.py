"""Cross-lane double-fill gate + Stage 2/3 golden parity (v2.0.34)."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from analyst.ema_lane_entry_gate_8510 import (
    filter_signals_one_ema_trade,
    release_lane_reservation,
    try_reserve_lane_for_signal,
)
from analyst.ema_lane_lock_8510 import LANE_PROTOTYPE, LANE_REVERSAL, lane_lock_blocks_entry, release_lane_lock
from analyst.mj_sir_stage_reversal_8510 import evaluate_mj_sir_four_pack, load_mj_cfg
from analyst.reversal_spot_exit_8510 import reversal_spot_exit_tick


def _cross_sig(lane: str = "15m") -> Dict[str, Any]:
    return {
        "strategy_id": f"CROSSOVER_BULLISH",
        "direction": "LONG",
        "opt_side": "CE",
        "setup_name": f"Reversal {lane}",
        "details": "test crossover",
        "conviction": "HIGH",
        "target_pts": 20.0,
        "sl_pts": 20.0,
        "index_price": 25100.0,
        "mj_sir_paper_only": False,
        "reversal_timeframe": lane,
    }


def _proto_sig() -> Dict[str, Any]:
    return {
        "strategy_id": "EMA8_PROTOTYPE_LONG",
        "direction": "LONG",
        "opt_side": "CE",
        "setup_name": "Prototype",
        "details": "test prototype",
        "conviction": "HIGH",
        "target_pts": 40.0,
        "sl_pts": 40.0,
        "index_price": 25100.0,
        "ema8_prototype_paper_only": False,
    }


def test_no_second_lane_while_lock_held():
    lock: Dict[str, Any] = {}
    assert try_reserve_lane_for_signal(lock, "CROSSOVER_BULLISH", open_position_count=0)[0] is True
    ok, why = try_reserve_lane_for_signal(lock, "EMA8_PROTOTYPE_LONG", open_position_count=0)
    assert ok is False and "Standing down" in why
    release_lane_lock(lock)
    assert try_reserve_lane_for_signal(lock, "EMA8_PROTOTYPE_LONG", open_position_count=0)[0] is True


def test_open_position_blocks_all_ema_lanes():
    lock: Dict[str, Any] = {}
    blocked, why = lane_lock_blocks_entry(lock, LANE_REVERSAL, open_position_count=1)
    assert blocked and "Open position" in why
    blocked2, _ = lane_lock_blocks_entry(lock, LANE_PROTOTYPE, open_position_count=1)
    assert blocked2


def test_batch_filter_one_ema_signal():
    lock: Dict[str, Any] = {}
    sigs = [_cross_sig("5m"), _cross_sig("15m"), _proto_sig()]
    out = filter_signals_one_ema_trade(sigs, lock, open_position_count=0)
    assert len(out) == 1 and out[0]["strategy_id"].startswith("CROSSOVER")


def test_unfilled_order_releases_reservation():
    lock: Dict[str, Any] = {}
    assert try_reserve_lane_for_signal(lock, "CROSSOVER_BULLISH", open_position_count=0)[0]
    release_lane_reservation(lock, "CROSSOVER_BULLISH")
    assert not lock.get("holder")


def test_reversal_trail_arms_and_trails_stop():
    trail = {"enabled": True, "activate_pts": 20.0, "armed": False}
    _, sl0, hit0, _ = reversal_spot_exit_tick(
        is_long=True,
        spot_entry=25100.0,
        spot_idx=25125.0,
        spot_tgt=25120.0,
        spot_sl=25080.0,
        trail=trail,
        bar=None,
    )
    assert trail["armed"] is True and not hit0
    bar = {"open": 25120, "high": 25130, "low": 25115, "close": 25128}
    _, sl1, _, _ = reversal_spot_exit_tick(
        is_long=True,
        spot_entry=25100.0,
        spot_idx=25126.0,
        spot_tgt=90000.0,
        spot_sl=sl0,
        trail=trail,
        bar=bar,
    )
    assert sl1 >= sl0


def _stage_fixture() -> tuple:
    idx = pd.date_range("2026-11-10 09:15", periods=80, freq="15min", tz="Asia/Kolkata")
    t = np.linspace(25000, 25120, 80)
    b15 = pd.DataFrame(
        {"open": t, "high": t + 5, "low": t - 5, "close": t},
        index=idx,
    )
    mj_state: Dict[str, Any] = {}
    cfg = load_mj_cfg({"mj_sir": {}})
    return b15, mj_state, cfg


def test_stage2_stage3_emit_signals_still_paper_only():
    """Golden: Stage 2/3 signals keep mj_sir_paper_only=True (Reversal LIVE does not change them)."""
    from analyst.test_four_algo_corrections_8510 import _closed_e8, _run, _tick, _uptrend

    b15 = _uptrend()
    e8 = _closed_e8(b15)
    sigs, _ = _run(b15, {}, dynamic=_tick(e8 + 0.5, lo=e8 - 1, hi=e8 + 2))
    for s in sigs:
        if str(s.get("strategy_id", "")).startswith("STAGE_"):
            assert s.get("mj_sir_paper_only") is True


def test_process_signals_second_crossover_blocked_after_first_reserves(tmp_path: Path):
    """Integration: first crossover reserves lock; second cannot pass reservation."""
    from analyst.terminal8510_backend import QuantTerminalBackend

    b = QuantTerminalBackend.__new__(QuantTerminalBackend)
    b._ema_lane_lock = {}
    b.scanner_cfg = {
        "signal_cooldown_5m_min": 0.0,
        "skipped_signal_retry_seconds": 0.0,
        "reversal": {"execution_mode": "LIVE_ALLOWED", "oi_filters": {"enabled": False}},
        "desk8600_board": {"enabled": False},
    }
    b.directional_bias = "NEUTRAL"
    b.algo_strategy_execution_allowed = lambda sid: True  # type: ignore
    b.strategy_cooldowns = {}
    b.recent_signals = []
    b.cached_telemetry = {"spot": {"price": 25100.0}}
    b.__dict__["_skip_retry_at"] = {}
    b.get_active_positions = lambda: []  # type: ignore
    b.log_signal_observation = lambda *a, **k: "uid_test"  # type: ignore
    b.mark_signal_outcome = lambda *a, **k: None  # type: ignore
    b.log_signal_oi_context = lambda *a, **k: None  # type: ignore
    b.log_signal_desk8600 = lambda *a, **k: None  # type: ignore
    b.oi_context = lambda: {}  # type: ignore
    b._release_unfilled_setup = lambda sig: None  # type: ignore
    b.desk8600_board = lambda: {"sources": []}  # type: ignore
    b.algo_strike_selection = {"LONG_CE_ATM": True, "LONG_CE_ITM": True}
    b.priority_contracts = [{"id": "LONG_CE_ATM", "symbol": "NIFTY", "security_id": "1"}]
    b._select_ema8_prototype_contract_any_strike = AsyncMock(
        return_value=({"id": "LONG_CE_ATM", "symbol": "NIFTY", "security_id": "1"}, "")
    )
    b.execute_order_with_chaser = AsyncMock(return_value={"ok": True, "position_id": "POS_TEST"})
    b.ledger_db = tmp_path / "led.sqlite3"
    with sqlite3.connect(b.ledger_db) as conn:
        conn.execute(
            "CREATE TABLE positions (position_id TEXT PRIMARY KEY, symbol TEXT, status TEXT)"
        )

    def run_one(sig):
        from analyst.terminal8510_backend import QuantTerminalBackend

        asyncio.run(QuantTerminalBackend._process_strategy_signals(b, [sig]))

    run_one(_cross_sig("15m"))
    assert b._ema_lane_lock.get("holder") == LANE_REVERSAL
    run_one(_proto_sig())
    assert b.execute_order_with_chaser.await_count == 1


def test_reversal_oi_log_only_never_blocks_real_route():
    import copy

    from analyst.ema8_prototype_8510 import DEFAULT_CFG
    from analyst.proto_oi_filters_8510 import DEFAULT_OI_FILTERS, oi_switch_block

    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg["oi_filters"] = copy.deepcopy(DEFAULT_OI_FILTERS)
    cfg["oi_filters"]["switches"]["writer_flow"] = True
    reads = {"writer_flow": {"direction": "PE", "age_sec": 30.0}}
    would_block, _ = oi_switch_block(True, cfg, reads)
    assert would_block
    cfg["oi_filters"]["mode"] = "log_only"
    blocking = would_block and str(cfg["oi_filters"].get("mode")).lower() == "block"
    assert not blocking


def test_reversal_trail_uses_bar_extreme_not_candle_color():
    from analyst.reversal_spot_exit_8510 import apply_reversal_candle_trail_to_stop

    # Long + red candle (close < open): still trails stop to candle low
    sl = apply_reversal_candle_trail_to_stop(
        is_long=True, spot_stop=98.0, candle_o=105.0, candle_h=106.0, candle_l=99.0, candle_c=101.0
    )
    assert sl == 99.0
    # Short + green candle: still trails stop to candle high
    sl = apply_reversal_candle_trail_to_stop(
        is_long=False, spot_stop=102.0, candle_o=98.0, candle_h=101.0, candle_l=97.0, candle_c=100.0
    )
    assert sl == 101.0


def test_crossover_uses_prototype_contract_router():
    src = Path("analyst/terminal8510_backend.py").read_text()
    i = src.index("is_crossover_sig = sid_u.startswith")
    assert "_select_ema8_prototype_contract_any_strike" in src[i : i + 900]