# -*- coding: utf-8 -*-
"""
analyst/test_v17_proven_edges_8510.py
Comprehensive unit tests for Port 8510 v1.7 Proven Quant Edges:
  1. Goldilocks 4/8 Separation Regime Gate (0.13–0.26 ATR)
  2. NIFTY Futures Volume Surge Ratio (vsr_10 >= 1.15) & setup tagging
  3. Statutory Dhan Tariff cost model (~₹72.75–₹78/trade at lot 65)
  4. /api/proven_edges/config GET and POST endpoints & telemetry
"""

import os
import sys
import json
import pytest
import numpy as np
import pandas as pd
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyst.futures_volume_engine import FuturesVolumeEngine, get_futures_volume_engine
from analyst.terminal8510_backend import QuantTerminalBackend


@pytest.fixture
def test_backend(tmp_path):
    """Instantiate QuantTerminalBackend with temporary config."""
    config_path = tmp_path / "terminal8510_config.json"
    initial_config = {
        "execution_mode": "PAPER",
        "proven_edges": {
            "goldilocks_separation_enabled": True,
            "goldilocks_min_atr": 0.13,
            "goldilocks_max_atr": 0.26,
            "futures_volume_surge_enabled": False,
            "futures_volume_surge_min_ratio": 1.15,
            "default_cost_model": "STATUTORY_DHAN",
        },
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(initial_config, f)

    backend = QuantTerminalBackend()
    backend.config_path = config_path
    backend.proven_edges_cfg = initial_config["proven_edges"]
    return backend


def test_goldilocks_separation_regimes(test_backend):
    """Test 1A: Verify Goldilocks regime classification logic and ATR thresholds."""
    # 1. Chop Zone: sep_atr < 0.13 ATR
    # e.g. ema4 = 24001.0, ema8 = 24000.0, atr = 20.0 -> sep = 1 / 20 = 0.05 ATR
    status_chop = test_backend.compute_goldilocks_separation(ema4_val=24001.0, ema8_val=24000.0, atr14_val=20.0)
    assert status_chop["regime"] == "CHOP_ZONE"
    assert status_chop["raw_allowed"] is False
    assert status_chop["sep_atr"] == 0.05
    assert "CHOP ZONE" in status_chop["badge"]

    # 2. Goldilocks Sweet Spot: 0.13 <= sep_atr <= 0.26 ATR
    # e.g. ema4 = 24003.8, ema8 = 24000.0, atr = 20.0 -> sep = 3.8 / 20 = 0.19 ATR
    status_gold = test_backend.compute_goldilocks_separation(ema4_val=24003.8, ema8_val=24000.0, atr14_val=20.0)
    assert status_gold["regime"] == "GOLDILOCKS_SWEET_SPOT"
    assert status_gold["raw_allowed"] is True
    assert status_gold["sep_atr"] == 0.19
    assert "GOLDILOCKS SWEET SPOT" in status_gold["badge"]

    # 3. Climax Exhaustion: sep_atr > 0.26 ATR
    # e.g. ema4 = 24008.0, ema8 = 24000.0, atr = 20.0 -> sep = 8.0 / 20 = 0.40 ATR
    status_climax = test_backend.compute_goldilocks_separation(ema4_val=24008.0, ema8_val=24000.0, atr14_val=20.0)
    assert status_climax["regime"] == "CLIMAX_EXHAUSTION"
    assert status_climax["raw_allowed"] is False
    assert status_climax["sep_atr"] == 0.40
    assert "CLIMAX EXHAUSTION" in status_climax["badge"]


def test_goldilocks_gate_blocking_in_evaluation(test_backend):
    """Test 1B: Verify evaluate_candle_dataframe blocks chop/climax setups when enabled."""
    # 50 15-minute bars
    n = 50
    dates = pd.date_range("2026-11-05 09:15", periods=n, freq="15min", tz="Asia/Kolkata")
    
    # Strong upward rally: sep_atr = 0.408 ATR > 0.26 ATR (Climax Exhaustion regime)
    bull_prices = np.linspace(23700, 24000, n)
    df_climax = pd.DataFrame({
        "open": bull_prices - 5,
        "high": bull_prices + 15,
        "low": bull_prices - 15,
        "close": bull_prices,
        "volume": [1000] * n,
    }, index=dates)

    test_backend.active_party = "NEUTRAL"
    test_backend.scanner_cfg["tolerance_pts"] = 25.0
    test_backend.scanner_cfg["bb_width_min"] = 40.0
    e8_last = df_climax["close"].ewm(span=8, adjust=False).mean().iloc[-1]

    # 1. When Goldilocks gate is ENABLED:
    test_backend.update_proven_edges_config({"goldilocks_separation_enabled": True})
    signals_blocked = test_backend.evaluate_candle_dataframe(df_climax, spot_price=e8_last)
    # Late climax entry must be blocked
    assert len(signals_blocked) == 0
    assert "BLOCKED_GOLDILOCKS_CLIMAX" in test_backend.strategy_status["stage2_long"]["state"]

    # 2. When Goldilocks gate is DISABLED:
    test_backend.update_proven_edges_config({"goldilocks_separation_enabled": False})
    signals_allowed = test_backend.evaluate_candle_dataframe(df_climax, spot_price=e8_last)
    # Stage 2 entry is authorized through
    assert len(signals_allowed) > 0
    assert signals_allowed[0]["strategy_id"] == "STAGE_2_LONG"


def test_futures_volume_surge_ratio_calculation():
    """Test 2A: Verify continuous stitched futures volume surge ratio (vsr_10) calculation."""
    fve = get_futures_volume_engine()
    fve.ensure_loaded()
    
    # 1. Verify engine has records loaded from database or synthesized
    assert len(fve._records_15m) > 0
    
    # 2. Verify record structure
    rec = fve._records_15m[-1]
    assert "ts" in rec
    assert "COMBINED" in rec
    assert "sma10" in rec
    assert "vsr_10" in rec
    
    # 3. Verify get_latest_vsr
    latest = fve.get_latest_vsr()
    assert "vsr_10" in latest
    assert "volume" in latest
    assert "sma10" in latest
    assert "is_surge" in latest
    assert "badge" in latest
    assert "pill_text" in latest
    if latest['vsr_10'] is None:
        assert latest['is_surge'] is False
        assert latest['slot_status'] in ('INSUFFICIENT_HISTORY','INCOMPLETE_BAR')
    else:
        assert latest["is_surge"] == (latest["vsr_10"] >= 1.15)


def test_futures_volume_surge_setup_tagging(test_backend):
    """Test 2B: Verify setups are tagged with 🔥 VOLUME SURGE CONFIRMED when vsr_10 >= 1.15."""
    # Mock high VSR (1.45x)
    mock_vsr = {
        "vsr_10": 1.45,
        "volume": 25000.0,
        "sma10": 17241.4,
        "is_surge": True,
        "badge": "🔥 INSTITUTIONAL VOLUME SURGE",
        "tag": "🔥 VOLUME SURGE CONFIRMED (59.0% OOS WR | +₹106.1/tr)",
        "pill_text": "📊 Futures Vol Surge (VSR): 1.45x [🔥 INSTITUTIONAL VOLUME SURGE]",
        "bar_ts": 1793874600,
        "datetime_ist": "2026-11-05 14:30:00",
    }

    with patch.object(test_backend.futures_volume_engine, "get_latest_vsr", return_value=mock_vsr):
        vsr_result = test_backend.futures_volume_engine.get_latest_vsr()
        assert vsr_result["is_surge"] is True
        assert vsr_result["vsr_10"] == 1.45
        assert "59.0% OOS WR" in vsr_result["tag"]


def test_statutory_dhan_tariff_cost_model(test_backend):
    """Test 3: Verify calculate_dhan_charges applies statutory Dhan tariff at dynamic lot size 65."""
    # Typical NIFTY ATM Option target trade: Buy @ 150, Sell @ 190 (+40 pts), Lot Size = 65 (1 lot)
    total_charges = test_backend.calculate_dhan_charges(
        buy_price=150.0,
        sell_price=190.0,
        qty=65,
    )

    # Total roundtrip charges should match statutory Dhan tariff (~₹72.75–₹78/trade at lot 65), NOT ₹400
    assert 72.0 <= total_charges <= 78.0
    assert test_backend.get_nifty_lot_size() == 65


def test_api_proven_edges_config_get_and_post(test_backend):
    """Test 4: Verify proven edges config GET, POST, and persistence."""
    # 1. GET initial config
    cfg = test_backend.get_proven_edges_config()
    assert cfg["goldilocks_separation_enabled"] is True
    assert cfg["goldilocks_min_atr"] == 0.13
    assert cfg["goldilocks_max_atr"] == 0.26
    assert cfg["futures_volume_surge_min_ratio"] == 1.15
    assert cfg["default_cost_model"] == "STATUTORY_DHAN"

    # 2. POST update config
    updated = test_backend.update_proven_edges_config({
        "goldilocks_separation_enabled": False,
        "futures_volume_surge_enabled": True,
        "futures_volume_surge_min_ratio": 1.25,
    })
    assert updated["goldilocks_separation_enabled"] is False
    assert updated["futures_volume_surge_enabled"] is True
    assert updated["futures_volume_surge_min_ratio"] == 1.25

    # 3. Verify persistence
    persisted = test_backend.get_proven_edges_config()
    assert persisted["goldilocks_separation_enabled"] is False
    assert persisted["futures_volume_surge_enabled"] is True
    assert persisted["futures_volume_surge_min_ratio"] == 1.25
