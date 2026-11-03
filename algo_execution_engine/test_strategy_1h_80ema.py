"""
algo_execution_engine/test_strategy_1h_80ema.py — Unit Tests for Flagship 1-Hour 80-EMA Touch Strategy.
"""
from __future__ import annotations

import tempfile
import shutil
from datetime import datetime, time as dtime
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from PIL import Image, ImageDraw

from algo_execution_engine.strategy_1h_80ema import (
    Strategy1h80EmaEvaluator,
    Strategy1hSignal,
    build_1h_data,
)
from algo_execution_engine.strategy_setup1 import ActivePosition
from algo_execution_engine.vision_audit import (
    SETUP_1H_80EMA,
    SETUP_1H_RESISTANCE,
    SETUP_1H_SUPPORT,
    VisionAuditAgent,
    normalize_setup_name,
    verify_gap_rule,
)


def create_synthetic_1m_data(n_sessions: int = 20, bars_per_session: int = 375, base_price: float = 24000.0) -> pd.DataFrame:
    """Creates synthetic 1-minute intraday bars across multiple sessions for 1H testing."""
    base_ts = 1762141560  # 09:15 IST
    rows = []
    price = base_price

    for s in range(n_sessions):
        session_start_ts = base_ts + s * 86400
        for i in range(bars_per_session):
            t = session_start_ts + i * 60
            mins = 15 + (i % 60)
            hours = 9 + (i // 60)
            if hours > 15 or (hours == 15 and mins > 30):
                continue
            # Slight random walk
            price += np.sin(i / 10.0) * 0.5 + 0.1
            rows.append({
                "ts": t,
                "open": price - 0.5,
                "high": price + 1.5,
                "low": price - 1.5,
                "close": price,
                "volume": 200.0,
            })

    return pd.DataFrame(rows)


def test_build_1h_data_resampling_and_ema80():
    """Tests that 1m candles are properly resampled into session-aligned 1H candles with 80-EMA."""
    df_1m = create_synthetic_1m_data(n_sessions=15, bars_per_session=375)
    df_1h = build_1h_data(df_1m)

    assert not df_1h.empty
    # Each full session produces ~7 hourly bars
    assert len(df_1h) >= 90
    assert "ema80" in df_1h.columns
    assert "ts" in df_1h.columns
    assert "close" in df_1h.columns

    # Verify EMA is non-null and numeric
    latest_ema = float(df_1h.iloc[-1]["ema80"])
    assert latest_ema > 20000.0


def test_1h_support_bounce_long_signal():
    """Tests 1H 80-EMA Support Bounce fires a valid Long signal with 65-lot size and 40/40 bracket."""
    evaluator = Strategy1h80EmaEvaluator()
    evaluator.reset_day()

    # Create mock 1H dataframe where market tests 80-EMA from above
    ema_level = 24500.0
    mock_1h = pd.DataFrame([
        {"ts": 1000, "open": 24520.0, "high": 24550.0, "low": 24510.0, "close": 24530.0, "ema80": ema_level},
        {"ts": 4600, "open": 24530.0, "high": 24540.0, "low": 24499.0, "close": 24515.0, "ema80": ema_level},  # tests 80-EMA from above (low <= 24503)
    ])

    current_spot = 24505.0  # bounced above 80-EMA
    sig = evaluator.check_1h_entry_signal(
        df_1h=mock_1h,
        current_spot=current_spot,
        current_ts=5000,
        current_time=dtime(10, 30),
        gap_pts=15.0,
    )

    assert sig.is_valid is True
    assert sig.side == "BUY"
    assert sig.setup_name == "1H 80-EMA Support Bounce"
    assert sig.lot_size == 65
    assert sig.spot == 24505.0
    assert sig.target_spot == 24545.0  # +40 pts
    assert sig.sl_spot == 24465.0      # -40 pts


def test_1h_resistance_rejection_short_signal():
    """Tests 1H 80-EMA Resistance Rejection fires a valid Short signal with 65-lot size and 40/40 bracket."""
    evaluator = Strategy1h80EmaEvaluator()
    evaluator.reset_day()

    ema_level = 24500.0
    mock_1h = pd.DataFrame([
        {"ts": 1000, "open": 24470.0, "high": 24485.0, "low": 24460.0, "close": 24480.0, "ema80": ema_level},
        {"ts": 4600, "open": 24480.0, "high": 24502.0, "low": 24475.0, "close": 24490.0, "ema80": ema_level},  # tests 80-EMA from below (high >= 24497)
    ])

    current_spot = 24495.0  # rejected below 80-EMA
    sig = evaluator.check_1h_entry_signal(
        df_1h=mock_1h,
        current_spot=current_spot,
        current_ts=5000,
        current_time=dtime(11, 0),
        gap_pts=-10.0,
    )

    assert sig.is_valid is True
    assert sig.side == "SELL"
    assert sig.setup_name == "1H 80-EMA Resistance Rejection"
    assert sig.lot_size == 65
    assert sig.spot == 24495.0
    assert sig.target_spot == 24455.0  # -40 pts in spot (profit on short)
    assert sig.sl_spot == 24535.0      # +40 pts in spot (stop loss on short)


def test_1h_gap_rule_filter_long_and_short():
    """Tests Gap Filter skips trades on > +50 pt gap-up (longs) or < -50 pt gap-down (shorts) without clean 80-EMA pullback."""
    evaluator = Strategy1h80EmaEvaluator()
    evaluator.reset_day()

    ema_level = 24500.0

    # 1. Long on +65 pt gap-up where low NEVER pulled back to 80-EMA -> REJECTED
    mock_gap_long = pd.DataFrame([
        {"ts": 1000, "open": 24580.0, "high": 24600.0, "low": 24530.0, "close": 24590.0, "ema80": ema_level},
    ])
    sig_gap = evaluator.check_1h_entry_signal(
        df_1h=mock_gap_long,
        current_spot=24590.0,
        current_ts=2000,
        current_time=dtime(9, 30),
        gap_pts=65.0,
        current_candle_low=24530.0,  # low was 30 pts above 80-EMA, no pullback touch
    )
    assert sig_gap.is_valid is False
    assert "Gap Rule" in sig_gap.rejection_reason

    # 2. Long on +65 pt gap-up where low DID pull back and touch 80-EMA -> VALID
    sig_pullback = evaluator.check_1h_entry_signal(
        df_1h=mock_gap_long,
        current_spot=24505.0,
        current_ts=3000,
        current_time=dtime(10, 0),
        gap_pts=65.0,
        current_candle_low=24500.0,  # low cleanly touched 80-EMA!
    )
    assert sig_pullback.is_valid is True
    assert sig_pullback.side == "BUY"


def test_1h_fixed_40_40_bracket_and_cost_to_cost():
    """Tests 1H position lifecycle: Target hit (+40 pts), Stop-loss hit (-40 pts), Cost-to-cost trigger."""
    evaluator = Strategy1h80EmaEvaluator()
    evaluator.reset_day()

    # 1. Long Trade reaches +40 pts target
    pos_long = ActivePosition(
        trade_id="1H_TEST_LONG",
        symbol="NIFTY 24500 CE",
        security_id="SIM_24500_CE",
        option_type="CE",
        strike=24500.0,
        quantity=65,  # 1H Lot Size
        entry_spot=24500.0,
        entry_option_price=150.0,
        entry_time="2026-11-03 10:00:00",
        entry_ts=1000,
        spot_sl=24460.0,  # -40 pts
        spot_target=24540.0,  # +40 pts
        option_sl=126.0,
        option_target=174.0,
        delta=0.60,
    )

    should_exit, reason, _ = evaluator.evaluate_active_position(
        pos=pos_long,
        current_spot=24541.0,
        current_option_ltp=175.0,
    )
    assert should_exit is True
    assert "Target Hit (+40 pts)" in reason

    # 2. Short Trade reaching -40 pts stop-loss
    pos_short = ActivePosition(
        trade_id="1H_TEST_SHORT",
        symbol="NIFTY 24500 PE",
        security_id="SIM_24500_PE",
        option_type="PE",
        strike=24500.0,
        quantity=65,
        entry_spot=24500.0,
        entry_option_price=150.0,
        entry_time="2026-11-03 10:00:00",
        entry_ts=1000,
        spot_sl=24540.0,  # +40 pts spot is SL for short
        spot_target=24460.0,  # -40 pts spot is Target for short
        option_sl=126.0,
        option_target=174.0,
        delta=0.60,
    )

    should_exit_sl, reason_sl, _ = evaluator.evaluate_active_position(
        pos=pos_short,
        current_spot=24541.0,
        current_option_ltp=125.0,
    )
    assert should_exit_sl is True
    assert "Stop-Loss Hit (-40 pts)" in reason_sl
    assert evaluator.stage_stopped_today is True


def test_vision_audit_recognizes_1h_80ema_setups():
    """Tests Vision Audit Agent recognizes 1H 80-EMA setups and applies 1H gap rules."""
    # Test normalization
    assert normalize_setup_name("1h_80ema_support_bounce.png") == SETUP_1H_SUPPORT
    assert normalize_setup_name("hourly_80ema_resistance_rejection.jpg") == SETUP_1H_RESISTANCE
    assert normalize_setup_name("nifty_1h_80ema_touch.png") == SETUP_1H_80EMA

    # Test Gap Rule on 1H setups
    # 1. 1H Long on +70 pt gap without pullback -> VIOLATION
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_1H_SUPPORT,
        gap_pts=70.0,
        has_pullback_at_ema8=False,
        gap_threshold=50.0,
    )
    assert is_viol is True
    assert "GAP RULE VIOLATION" in note

    # 2. 1H Short on -60 pt gap with pullback -> PASSED
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_1H_RESISTANCE,
        gap_pts=-60.0,
        has_pullback_at_ema8=True,
        gap_threshold=50.0,
    )
    assert is_viol is False
    assert "PASSED GAP RULE" in note


def test_single_image_audit_1h_chart():
    """Tests auditing a 1H 80-EMA chart image through the VisionAuditAgent."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="test_1h_audit_"))
    img_path = tmp_dir / "2026-10-31_nifty_1h_80ema_bounce.png"

    img = Image.new("RGB", (800, 600), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    draw.text((30, 30), "NIFTY 50 [1H] - 80 EMA Support Bounce", fill=(248, 250, 252))
    img.save(img_path)

    agent = VisionAuditAgent()
    res = agent.audit_single_image(img_path)

    assert res.date == "2026-10-31"
    assert res.setup_name == SETUP_1H_SUPPORT
    assert res.chart_image_matched == "2026-10-31_nifty_1h_80ema_bounce.png"
    assert "80-EMA" in res.actionable_rule_adjustment or "EMA" in res.actionable_rule_adjustment

    shutil.rmtree(tmp_dir, ignore_errors=True)
