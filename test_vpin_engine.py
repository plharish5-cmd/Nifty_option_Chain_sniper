"""
test_vpin_engine.py — Unit tests for VPINEngine
================================================
Covers:
  - Step 1: Data Preparation & column normalization
  - Step 2: Price Change & Volatility
  - Step 3: Bulk Volume Classification (BVC) & Z calculation via norm.cdf
  - Step 4: The Volume Clock (Bucketing, strict uniform V, multi-bucket row splits)
  - Step 5: VPIN calculation, theoretical bounds [0, 1], and CDF toxicity
  - Signals & Regimes (Option buying, option selling, alert-only)
  - Full pipeline integration & time-bar mapping
"""

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from vpin_engine import VPINEngine, load_vpin_rules


@pytest.fixture
def sample_ohlcv():
    """Create reproducible synthetic OHLCV data."""
    np.random.seed(101)
    n = 100
    dates = pd.date_range("2026-09-11 09:15", periods=n, freq="1min")
    price_innovations = np.random.normal(0, 4, n)
    prices = 24000.0 + np.cumsum(price_innovations)
    volumes = np.random.uniform(2000, 10000, n)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + 3,
        "low": prices - 3,
        "close": prices,
        "volume": volumes,
    })
    return df


class TestDataPreparation:
    def test_column_normalization_uppercase(self):
        engine = VPINEngine(bucket_volume=10000, window_length=10)
        df_upper = pd.DataFrame({
            "TIMESTAMP": ["2026-09-11 09:15", "2026-09-11 09:16"],
            "OPEN": [100.0, 101.0],
            "HIGH": [102.0, 103.0],
            "LOW": [99.0, 100.0],
            "CLOSE": [101.0, 102.0],
            "VOLUME": [5000, 6000],
        })
        clean = engine.prepare_data(df_upper)
        assert "close" in clean.columns
        assert "volume" in clean.columns
        assert "timestamp" in clean.columns
        assert len(clean) == 2

    def test_missing_required_columns_raises(self):
        engine = VPINEngine(bucket_volume=10000, window_length=10)
        df_bad = pd.DataFrame({"open": [100.0], "high": [101.0]})
        with pytest.raises(ValueError, match="Missing required columns"):
            engine.prepare_data(df_bad)

    def test_empty_dataframe_raises(self):
        engine = VPINEngine()
        with pytest.raises(ValueError, match="empty or None"):
            engine.prepare_data(pd.DataFrame())


class TestBulkVolumeClassification:
    def test_bvc_buy_sell_conservation(self, sample_ohlcv):
        """Verify V_i^B + V_i^S == V_i for all bars."""
        engine = VPINEngine(volatility_window=20)
        clean = engine.prepare_data(sample_ohlcv)
        bvc = engine.calculate_bvc(clean)

        total_reconstructed = bvc["buy_volume"] + bvc["sell_volume"]
        np.testing.assert_allclose(total_reconstructed, bvc["volume"], rtol=1e-6)

    def test_bvc_directional_properties(self):
        """Price rise must yield V^B > V^S; price drop must yield V^S > V^B."""
        engine = VPINEngine()
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-11 09:15", periods=5, freq="1min"),
            "open": [100.0, 100.0, 110.0, 90.0, 90.0],
            "high": [100.0, 110.0, 115.0, 95.0, 91.0],
            "low": [99.0, 99.0, 105.0, 85.0, 89.0],
            "close": [100.0, 110.0, 105.0, 95.0, 95.0],
            "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        })
        clean = engine.prepare_data(df)
        bvc = engine.calculate_bvc(clean, volatility_window=5)

        # Bar 1 has close 110 vs prev 100 (+10 move) -> buy volume > 500
        assert bvc.loc[1, "buy_volume"] > 500.0
        assert bvc.loc[1, "sell_volume"] < 500.0

        # Bar 2 has close 105 vs prev 110 (-5 move) -> sell volume > 500
        assert bvc.loc[2, "sell_volume"] > 500.0
        assert bvc.loc[2, "buy_volume"] < 500.0

        # Bar 4 has close 95 vs prev 95 (0 move) -> 50/50 split
        assert math.isclose(bvc.loc[4, "buy_volume"], 500.0, rel_tol=1e-5)
        assert math.isclose(bvc.loc[4, "sell_volume"], 500.0, rel_tol=1e-5)

    def test_zero_volatility_graceful_handling(self):
        """Flat price series (sigma = 0) must not trigger ZeroDivisionError."""
        engine = VPINEngine()
        df_flat = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-11 09:15", periods=10, freq="1min"),
            "close": [100.0] * 10,
            "volume": [500.0] * 10,
        })
        clean = engine.prepare_data(df_flat)
        bvc = engine.calculate_bvc(clean)
        # All bars should cleanly split 50/50
        assert (bvc["buy_volume"] == 250.0).all()
        assert (bvc["sell_volume"] == 250.0).all()


class TestVolumeBucketing:
    def test_strict_uniform_bucket_volume(self, sample_ohlcv):
        """Every completed bucket must have volume == V exactly."""
        V = 15000.0
        engine = VPINEngine(bucket_volume=V)
        clean = engine.prepare_data(sample_ohlcv)
        bvc = engine.calculate_bvc(clean)
        buckets = engine.build_volume_buckets(bvc, bucket_volume=V)

        assert not buckets.empty
        # Check every bucket volume is exactly V
        np.testing.assert_allclose(buckets["total_volume"], V, rtol=1e-7)
        # Check buy + sell == V
        np.testing.assert_allclose(
            buckets["buy_volume"] + buckets["sell_volume"], V, rtol=1e-7
        )

    def test_multi_bucket_spanning_bar(self):
        """A single large bar with volume = 3.5 V must cleanly fill 3 full buckets."""
        V = 1000.0
        engine = VPINEngine(bucket_volume=V)
        df_large = pd.DataFrame({
            "timestamp": ["2026-09-11 09:15:00"],
            "close": [100.0],
            "volume": [3500.0],
            "buy_volume": [2100.0],  # 60% buy
            "sell_volume": [1400.0], # 40% sell
        })
        buckets = engine.build_volume_buckets(df_large, bucket_volume=V)

        assert len(buckets) == 3
        for b in buckets.itertuples():
            assert math.isclose(b.total_volume, 1000.0, abs_tol=1e-6)
            # 60% buy ratio preserved in each bucket
            assert math.isclose(b.buy_volume, 600.0, abs_tol=1e-6)
            assert math.isclose(b.sell_volume, 400.0, abs_tol=1e-6)

        # Leftover in engine state should be 500 volume (300 buy, 200 sell)
        assert math.isclose(engine._current_v_total, 500.0, abs_tol=1e-6)
        assert math.isclose(engine._current_v_buy, 300.0, abs_tol=1e-6)
        assert math.isclose(engine._current_v_sell, 200.0, abs_tol=1e-6)

    def test_total_volume_conservation(self, sample_ohlcv):
        """Sum of volume in all completed buckets + leftover == total volume of input bars."""
        V = 12500.0
        engine = VPINEngine(bucket_volume=V)
        clean = engine.prepare_data(sample_ohlcv)
        bvc = engine.calculate_bvc(clean)
        buckets = engine.build_volume_buckets(bvc, bucket_volume=V)

        completed_volume = buckets["total_volume"].sum()
        leftover_volume = engine._current_v_total
        total_input_volume = clean["volume"].sum()

        assert math.isclose(completed_volume + leftover_volume, total_input_volume, rel_tol=1e-5)


class TestVPINCalculation:
    def test_vpin_bounds(self, sample_ohlcv):
        """VPIN must strictly reside within [0.0, 1.0]."""
        engine = VPINEngine(bucket_volume=10000.0, window_length=15)
        res = engine.calculate(sample_ohlcv)
        valid_vpin = res.buckets["vpin"].dropna()

        assert not valid_vpin.empty
        assert (valid_vpin >= 0.0).all()
        assert (valid_vpin <= 1.0).all()

    def test_vpin_theoretical_extremes(self):
        """
        Pure one-sided flow: VPIN must equal 1.0.
        Pure balanced flow: VPIN must equal 0.0.
        """
        V = 1000.0
        n = 5
        engine = VPINEngine(bucket_volume=V, window_length=n)

        # 1. Pure one-sided buy flow
        one_sided_buckets = pd.DataFrame({
            "bucket_idx": range(10),
            "timestamp": pd.date_range("2026-09-11 09:15", periods=10, freq="1min"),
            "price": [100.0] * 10,
            "buy_volume": [1000.0] * 10,
            "sell_volume": [0.0] * 10,
            "total_volume": [1000.0] * 10,
            "abs_imbalance": [1000.0] * 10,
            "signed_imbalance": [1000.0] * 10,
            "bar_count": [1] * 10,
        })
        res_one_sided = engine.calculate_vpin(one_sided_buckets)
        valid_vpin = res_one_sided["vpin"].dropna()
        assert (valid_vpin == 1.0).all()
        assert (res_one_sided["vpin_direction"].dropna() == 1.0).all()

        # 2. Pure balanced flow
        balanced_buckets = pd.DataFrame({
            "bucket_idx": range(10),
            "timestamp": pd.date_range("2026-09-11 09:15", periods=10, freq="1min"),
            "price": [100.0] * 10,
            "buy_volume": [500.0] * 10,
            "sell_volume": [500.0] * 10,
            "total_volume": [1000.0] * 10,
            "abs_imbalance": [0.0] * 10,
            "signed_imbalance": [0.0] * 10,
            "bar_count": [1] * 10,
        })
        res_balanced = engine.calculate_vpin(balanced_buckets)
        assert (res_balanced["vpin"].dropna() == 0.0).all()
        assert (res_balanced["vpin_direction"].dropna() == 0.0).all()


class TestSignalsAndRules:
    def test_signals_option_regimes(self):
        engine = VPINEngine(bucket_volume=10000.0, window_length=5)
        # Create a series with low toxicity then high bullish toxicity
        records = []
        for i in range(15):
            if i < 7:
                # Balanced
                records.append({
                    "timestamp": f"2026-09-11 09:{15+i:02d}:00",
                    "close": 24000.0,
                    "volume": 10000.0,
                })
            else:
                # Big rallies
                records.append({
                    "timestamp": f"2026-09-11 09:{15+i:02d}:00",
                    "close": 24000.0 + (i - 7) * 30.0,
                    "volume": 20000.0,
                })
        df = pd.DataFrame(records)
        signals = engine.generate_signals(df)
        assert not signals.empty
        # Actions must be from valid enum and never execute orders (alert only)
        allowed_actions = {
            "WAIT", "OPTION_BUY_CE", "OPTION_BUY_PE",
            "OPTION_BUY_STRADDLE", "OPTION_SELL_STRADDLE", "ALERT_ONLY"
        }
        assert set(signals["action"].unique()).issubset(allowed_actions)

    def test_rules_json_loaded(self):
        rules = load_vpin_rules()
        assert "bucket_volume" in rules
        assert "window_length" in rules
        assert "toxicity_threshold_high" in rules
        assert rules["toxicity_threshold_high"] == 0.35
