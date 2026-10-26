"""
tests/test_prahaar.py — Unit tests for the Prahaar Indicator & ATM Option-Buying Engine.
Tests:
  - Fast/slow EMA trend calculation
  - MACD crossover & histogram slope
  - ADX 14-period trend filter (chop filtering for ADX < 22)
  - India VIX filter (bounds and IV crush check)
  - Dynamic ATM strike recommendations (50-point step)
  - Signal generation: BUY ATM CALL vs BUY ATM PUT
  - API endpoint /api/prahaar
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import numpy as np
import pytest

from app.engine.prahaar import (
    PrahaarEngine,
    compute_adx,
    compute_ema,
    compute_macd,
    compute_prahaar,
)
from app.engine.primitives import Bars


def _make_bars(closes, highs=None, lows=None, opens=None, wick=2.0, step_s=300) -> Bars:
    rows = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = opens[k] if opens is not None else prev
        h = highs[k] if highs is not None else max(o, c) + wick
        l = lows[k] if lows is not None else min(o, c) - wick
        rows.append({"ts": 1_700_000_000 + step_s * k, "o": o, "h": h, "l": l, "c": c})
        prev = c
    return Bars.from_rows(rows)


def test_ema_computation():
    """Verify EMA responds properly to price moves."""
    arr = np.array([100.0, 102.0, 104.0, 106.0, 108.0, 110.0])
    ema3 = compute_ema(arr, 3)
    assert len(ema3) == len(arr)
    assert ema3[0] == 100.0
    assert ema3[-1] > ema3[0]
    assert ema3[-1] < arr[-1]  # EMA lags raw price in uptrend


def test_macd_computation():
    """Verify MACD line, signal line, and histogram."""
    # Strong upward ramp creates positive MACD
    arr = np.linspace(100.0, 150.0, 40)
    macd, signal, hist = compute_macd(arr, fast_period=12, slow_period=26, signal_period=9)
    assert len(macd) == 40
    assert len(signal) == 40
    assert len(hist) == 40
    # In sustained uptrend, fast EMA > slow EMA => macd > 0
    assert macd[-1] > 0
    assert np.allclose(hist, macd - signal)


def test_adx_computation():
    """Verify ADX measures trend strength."""
    # Flat chop market
    flat_c = np.array([100.0, 100.5, 99.8, 100.2, 100.1, 99.9] * 8)
    flat_h = flat_c + 0.5
    flat_l = flat_c - 0.5
    adx_flat, p_di_flat, m_di_flat = compute_adx(flat_h, flat_l, flat_c, period=14)
    # Strong trending market
    trend_c = np.linspace(100.0, 200.0, 48)
    trend_h = trend_c + 2.0
    trend_l = trend_c - 1.0
    adx_trend, p_di_trend, m_di_trend = compute_adx(trend_h, trend_l, trend_c, period=14)

    assert len(adx_flat) == 48
    assert len(adx_trend) == 48
    # Trending ADX should be substantially higher than flat chop
    assert adx_trend[-1] > adx_flat[-1]
    assert adx_trend[-1] > 20.0
    # In uptrend, +DI should dominate -DI
    assert p_di_trend[-1] > m_di_trend[-1]


def test_vix_filter():
    """Verify India VIX filter logic."""
    engine = PrahaarEngine({
        "vix_filter_enabled": True,
        "vix_min": 10.0,
        "vix_max": 28.0,
        "vix_drop_max": -1.5,
    })
    # Normal stable VIX
    ok, msg = engine.check_vix(14.5, 0.2)
    assert ok is True
    assert "supportive" in msg

    # VIX too low (anemic premium expansion)
    ok, msg = engine.check_vix(8.5, 0.0)
    assert ok is False
    assert "< 10.0" in msg

    # VIX too high (panic / crush risk)
    ok, msg = engine.check_vix(32.0, 1.0)
    assert ok is False
    assert "> 28.0" in msg

    # VIX crashing (-2.5 pts) -> IV crush risk
    ok, msg = engine.check_vix(15.0, -2.5)
    assert ok is False
    assert "dropping sharply" in msg

    # Filter disabled
    engine_disabled = PrahaarEngine({"vix_filter_enabled": False})
    ok, msg = engine_disabled.check_vix(8.0, -5.0)
    assert ok is True


def test_atm_strike_calculation():
    """Verify dynamic 50-step strike rounding."""
    engine = PrahaarEngine({"strike_step": 50})
    assert engine.calculate_atm_strike(24512.0) == 24500
    assert engine.calculate_atm_strike(24528.0) == 24550
    assert engine.calculate_atm_strike(24500.0) == 24500
    assert engine.calculate_atm_strike(24476.0) == 24500
    assert engine.calculate_atm_strike(24474.0) == 24450


def test_bullish_call_signal_generation():
    """Verify BUY ATM CALL signal on strong bullish expansion."""
    # Build 40 bars starting flat then surging upward
    base = [24000.0] * 20
    surge = [24000.0 + (i * 25.0) for i in range(1, 25)]
    closes = base + surge
    bars = _make_bars(closes, step_s=300)

    cfg = {
        "fast_ema_len": 5,
        "slow_ema_len": 12,
        "macd_fast": 6,
        "macd_slow": 14,
        "macd_signal": 5,
        "adx_period": 10,
        "adx_threshold": 18.0,
        "vix_filter_enabled": True,
        "vix_min": 10.0,
        "vix_max": 28.0,
        "strike_step": 50,
        "signal_cooldown_bars": 2,
    }
    engine = PrahaarEngine(cfg)
    res = engine.process(bars, tf="5m", vix=14.0, vix_chg=0.1)

    assert res["available"] is True
    assert res["latest_state"]["ema_trend"] == "bullish"
    assert res["latest_state"]["macd_momentum"] == "bullish"
    assert res["latest_state"]["current_verdict"] == "BUY ATM CALL"

    signals = res["signals"]
    assert len(signals) > 0
    bull_sigs = [s for s in signals if s["dir"] == "bull"]
    assert len(bull_sigs) > 0
    first_bull = bull_sigs[0]
    assert first_bull["action"] == "BUY ATM CALL"
    assert first_bull["option_type"] == "CE"
    assert first_bull["strike"] % 50 == 0
    assert "CE" in first_bull["option_symbol"]
    assert first_bull["tp"] > first_bull["entry"]
    assert first_bull["sl"] < first_bull["entry"]


def test_bearish_put_signal_generation():
    """Verify BUY ATM PUT signal on strong bearish breakdown."""
    base = [24500.0] * 20
    dump = [24500.0 - (i * 25.0) for i in range(1, 25)]
    closes = base + dump
    bars = _make_bars(closes, step_s=300)

    cfg = {
        "fast_ema_len": 5,
        "slow_ema_len": 12,
        "macd_fast": 6,
        "macd_slow": 14,
        "macd_signal": 5,
        "adx_period": 10,
        "adx_threshold": 18.0,
        "vix_filter_enabled": True,
        "vix_min": 10.0,
        "vix_max": 28.0,
        "strike_step": 50,
        "signal_cooldown_bars": 2,
    }
    engine = PrahaarEngine(cfg)
    res = engine.process(bars, tf="5m", vix=15.5, vix_chg=0.5)

    assert res["available"] is True
    assert res["latest_state"]["ema_trend"] == "bearish"
    assert res["latest_state"]["macd_momentum"] == "bearish"
    assert res["latest_state"]["current_verdict"] == "BUY ATM PUT"

    signals = res["signals"]
    assert len(signals) > 0
    bear_sigs = [s for s in signals if s["dir"] == "bear"]
    assert len(bear_sigs) > 0
    first_bear = bear_sigs[0]
    assert first_bear["action"] == "BUY ATM PUT"
    assert first_bear["option_type"] == "PE"
    assert first_bear["strike"] % 50 == 0
    assert "PE" in first_bear["option_symbol"]
    assert first_bear["tp"] < first_bear["entry"]
    assert first_bear["sl"] > first_bear["entry"]


def test_adx_chop_filter_blocks_signals():
    """Verify that choppy sideways range without ADX trend strength generates NO signals."""
    np.random.seed(42)
    closes = 24000.0 + np.cumsum(np.random.uniform(-3, 3, 60))
    highs = closes + np.random.uniform(1, 4, 60)
    lows = closes - np.random.uniform(1, 4, 60)
    bars = _make_bars(closes, highs=highs, lows=lows, step_s=300)

    cfg = {
        "adx_threshold": 22.0,
        "vix_filter_enabled": True,
    }
    engine = PrahaarEngine(cfg)
    res = engine.process(bars, tf="5m", vix=13.0)

    assert res["available"] is True
    assert res["latest_state"]["adx_trending"] is False
    assert res["latest_state"]["current_verdict"] == "NEUTRAL"
    assert len(res["signals"]) == 0
    assert len(res["markers"]) == 0


def test_compute_prahaar_helper():
    """Verify compute_prahaar functional wrapper."""
    closes = [24000.0 + (i * 10.0) for i in range(35)]
    bars = _make_bars(closes, step_s=300)
    res = compute_prahaar(bars, tf="5m", vix=14.0)
    assert res["available"] is True
    assert "latest_state" in res
    assert "markers" in res
    assert "stats" in res


def test_adx_impulse_gate_morning_breakdown():
    """
    Verify that a sharp multi-candle breakdown following a flat consolidation
    triggers a Prahaar BUY PE signal via the DX/Impulse gate without getting
    blocked by lagging 14-period smoothed ADX < 20.0.
    """
    # 25 flat consolidation bars around 22,740 (ADX smoothed drops to ~10-14)
    closes = [22740.0 + (i % 3) * 2.0 - 2.0 for i in range(25)]
    highs = [c + 3.0 for c in closes]
    lows = [c - 3.0 for c in closes]

    # 5 sharp breakdown bars dropping ~280 points from 22,740 to 22,460
    breakdown = [22700.0, 22650.0, 22590.0, 22520.0, 22460.0]
    for k, c in enumerate(breakdown):
        prev = closes[-1]
        closes.append(c)
        highs.append(prev + 2.0)
        lows.append(c - 2.0)

    rows = [{"ts": 1700000000 + 300 * i, "o": closes[i - 1] if i > 0 else closes[0], "h": highs[i], "l": lows[i], "c": closes[i]} for i in range(len(closes))]
    bars = Bars.from_rows(rows)

    # Standard engine using default rules (adx_threshold = 20.0, adx_impulse_threshold = 18.0)
    engine = PrahaarEngine({
        "adx_threshold": 20.0,
        "adx_impulse_threshold": 18.0,
        "adx_impulse_dx_min": 25.0,
        "adx_impulse_di_spread_min": 12.0,
        "adx_impulse_atr_mult": 1.3,
        "vix_filter_enabled": True,
        "strike_step": 50,
        "signal_cooldown_bars": 2,
    })
    res = engine.process(bars, tf="5m", vix=15.0)

    assert res["available"] is True
    signals = res["signals"]
    assert len(signals) > 0, "Expected signals to trigger via impulse gate"

    # Signals are sorted strictly descending by Unix timestamp
    for idx in range(len(signals) - 1):
        assert signals[idx]["time"] >= signals[idx + 1]["time"], "Signals must be sorted descending by time"

    bear_sigs = [s for s in signals if s["dir"] == "bear"]
    assert len(bear_sigs) > 0
    first_breakdown_sig = [s for s in bear_sigs if s["strike"] >= 22650][0]
    assert first_breakdown_sig["action"] == "BUY ATM PUT"
    assert first_breakdown_sig["option_type"] == "PE"
    assert first_breakdown_sig["is_impulse_active"] is True
    # The early breakdown signal triggered when ADX was below 20.0 thanks to the Impulse gate!
    assert first_breakdown_sig["adx"] < 20.0

    # Latest state diagnostics
    assert res["latest_state"]["adx_trending"] is True
    assert res["latest_state"]["current_verdict"] == "BUY ATM PUT"
    assert res["latest_state"]["is_impulse_active"] is True
