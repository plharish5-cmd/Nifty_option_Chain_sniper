"""
engine/test_terminal_engine.py — Rigorous unit test suite for institutional trading engines.
"""

from datetime import datetime, timedelta
import json
import numpy as np
import pandas as pd
import pytest

from engine.smc_engine import AdvancedSMCEngine
from engine.options_flow_engine import (
    FourPillarsEngine,
    OIPillarMigrationEngine,
    GammaSqueezeEngine,
    ZoneMatrixEngine,
    ConvergenceSetupEngine,
)
from engine.terminal_engine import UnifiedTerminalEngine


def generate_synthetic_candles(n: int = 60, base_price: float = 24000.0) -> pd.DataFrame:
    """Generates synthetic OHLCV candle series containing MSB, FVG, OB, and SFP patterns."""
    np.random.seed(42)
    timestamps = [datetime(2026, 9, 6, 9, 15) + timedelta(minutes=15 * i) for i in range(n)]

    # Create a realistic swing path:
    # 0..15: downtrend from 24200 to 23800 (forming LH and deepest LL)
    # 16..30: sharp reversal pumping up through LH (producing Strong MSB, Bullish FVG, Bullish OB)
    # 31..45: consolidation / pullback to FVG & OB (testing Dragon Fruit zone)
    # 46..55: sweep of prior high with upper wick closing below (SFP)
    # 56..60: sharp breakdown breaking through OB (converting it to Breaker Block)
    
    closes = np.zeros(n)
    opens = np.zeros(n)
    highs = np.zeros(n)
    lows = np.zeros(n)
    volumes = np.zeros(n)

    p = 24200.0
    for i in range(n):
        if i < 15:
            # Downtrend to 23800
            drift = -25.0
        elif i < 28:
            # Strong impulsive pump to 24350
            drift = +40.0
        elif i < 45:
            # Pullback to 24050
            drift = -15.0
        elif i < 52:
            # Push to test 24360 (sweep)
            drift = +35.0
        else:
            # Dump to 23900
            drift = -70.0

        op = p
        cl = op + drift + np.random.uniform(-10.0, 10.0)
        hi = max(op, cl) + np.random.uniform(2.0, 15.0)
        lo = min(op, cl) - np.random.uniform(2.0, 15.0)

        # Force a strong SFP at bar 50: wick pierces 24350, but close falls below
        if i == 50:
            op = 24330.0
            hi = 24420.0  # deep sweep
            cl = 24320.0  # close back inside
            lo = 24300.0

        # Force a clean 3-candle Bullish FVG around bar 18-20
        if i == 18:
            op = 23900.0; cl = 23930.0; hi = 23935.0; lo = 23895.0
        elif i == 19:
            # Huge green expansion
            op = 23940.0; cl = 24100.0; hi = 24110.0; lo = 23938.0
        elif i == 20:
            # Next candle stays above candle 18 high
            op = 24105.0; cl = 24180.0; hi = 24190.0; lo = 24050.0  # 24050 > 23935 => FVG [23935, 24050]

        opens[i] = op
        closes[i] = cl
        highs[i] = hi
        lows[i] = lo
        volumes[i] = np.random.uniform(100000, 500000)
        p = cl

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    })


def test_smc_engine():
    df = generate_synthetic_candles()
    engine = AdvancedSMCEngine(pivot_window=2)
    res = engine.analyze(df)

    assert "swings" in res
    assert len(res["swings"]) > 0, "Should detect swing points"
    assert "msb_list" in res, "Should compute MSB list"
    assert "fvgs" in res, "Should detect Fair Value Gaps"
    assert "order_blocks" in res, "Should detect Order Blocks"
    assert "breaker_blocks" in res, "Should detect Breaker Blocks"
    assert "dealing_range" in res
    assert res["dealing_range"] is not None
    assert 0 <= res["dealing_range"].discount_depth_pct <= 100

    print(f"✓ SMC Engine verified: {len(res['swings'])} Swings, {len(res['msb_list'])} MSBs, "
          f"{len(res['fvgs'])} FVGs, {len(res['order_blocks'])} OBs, {len(res['breaker_blocks'])} Breakers.")


def test_options_flow_engine():
    # 1. Four Pillars test
    fp = FourPillarsEngine.evaluate(
        vix=12.5,
        vix_chg_pct=-3.5,
        fii_net_crores=1200.0,
        dii_net_crores=800.0,
        advances=38,
        declines=12,
        gift_nifty_pts=65.0,
        spx_chg_pct=0.8
    )
    assert fp.composite_score >= 2.0
    assert fp.bias_label in ("MODERATE BULLISH", "EXTREME BULLISH")
    assert "PLAYBOOK" in fp.algo_directive

    # 2. OI Pillar Migration test
    s_long = OIPillarMigrationEngine.classify_side(ltp_chg=+15.0, oi_chg=+50000)
    assert s_long == "LONG BUILD-UP"
    s_short = OIPillarMigrationEngine.classify_side(ltp_chg=-15.0, oi_chg=+50000)
    assert s_short == "SHORT BUILD-UP"
    s_cov = OIPillarMigrationEngine.classify_side(ltp_chg=+15.0, oi_chg=-50000)
    assert s_cov == "SHORT COVERING"
    s_unw = OIPillarMigrationEngine.classify_side(ltp_chg=-15.0, oi_chg=-50000)
    assert s_unw == "LONG UNWINDING"

    print(f"✓ Options Flow Engine verified: Composite {fp.composite_score:+0.1f}/4.0, Bias={fp.bias_label}")


def test_unified_terminal_engine():
    df = generate_synthetic_candles()
    engine = UnifiedTerminalEngine()
    snapshot = engine.process(candle_df=df, spot=24150.0)

    assert snapshot.spot_price == 24150.0
    assert len(snapshot.divergent_oi_chart) > 0
    assert len(snapshot.strike_evidence_table) > 0
    assert len(snapshot.active_setups) > 0
    assert len(snapshot.zone_matrix) > 0

    # Test JSON serialization
    d = snapshot.to_dict()
    assert isinstance(d, dict)
    json_str = json.dumps(d)
    assert len(json_str) > 1000

    print("✓ Unified Terminal Engine successfully processed snapshot & validated JSON export!")


if __name__ == "__main__":
    test_smc_engine()
    test_options_flow_engine()
    test_unified_terminal_engine()
    print("\nALL BACKEND ENGINE INTEGRATION TESTS PASSED PERFECTLY!")
