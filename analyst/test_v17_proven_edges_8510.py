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


def test_goldilocks_and_volume_surge_are_removed(test_backend):
    """Owner removed the Goldilocks ATR gate and the Futures Volume Surge engine on 12 Nov 2026.
    The four tests of those features were removed with them."""
    assert not hasattr(test_backend, "compute_goldilocks_separation")
    assert not hasattr(test_backend, "get_latest_goldilocks_status")
    assert test_backend.futures_volume_engine is None
    assert test_backend.update_proven_edges_config({"default_cost_model": "STATUTORY_DHAN"})["default_cost_model"] == "STATUTORY_DHAN"
