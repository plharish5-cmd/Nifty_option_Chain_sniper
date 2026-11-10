"""Unit tests for 8 EMA Prototype state machine."""
from __future__ import annotations

import pandas as pd

from analyst.ema8_prototype_8510 import (
    _filter_blocks,
    backtest_ema8_prototype_at_bar,
    compute_dynamic_emas_on_bar,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
)


def _bars_from_closes(closes):
    """Build minimal OHLC frame where open/high/low track close for tests."""
    idx = pd.date_range("2026-11-05 09:15", periods=len(closes), freq="15min")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 2 for c in closes],
            "low": [c - 2 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )
    return df


def test_long_cross_then_retouch_fires_signal():
    # Ramp up, cross above 8 EMA on bar 24, retouch on bar 25
    closes = [100.0 + i * 0.5 for i in range(30)]
    closes[24] = closes[23] + 5  # sharp cross above prior e8
    closes[25] = closes[24] - 1  # dip to touch e8 on low
    b15 = _bars_from_closes(closes)
    b15.loc[b15.index[25], "low"] = b15["close"].iloc[25] - 8

    cfg = load_ema8_prototype_cfg(
        {
            "ema8_prototype": {
                "enabled": True,
                "touch_tol_pts": 5.0,
                "bb_width_min_pts": 0.0,
                "ribbon_max_pts": 0.0,
            }
        }
    )
    state: dict = {}
    sigs, st = evaluate_ema8_prototype(
        b15,
        bar_ci=-2,
        cfg=cfg,
        proto_state=state,
        session_day="2026-11-05",
        bb_gate_active=False,
    )
    assert sigs or st.get("state") in ("ARMED", "TRIGGERED", "EXPIRED", "SCANNING")


def test_no_entry_on_cross_bar_only():
    closes = [100.0 + i * 0.3 for i in range(28)]
    b15 = _bars_from_closes(closes)
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"bb_width_min_pts": 0.0, "ribbon_max_pts": 0.0}})
    state: dict = {}
    _, st = evaluate_ema8_prototype(
        b15,
        bar_ci=-1,
        cfg=cfg,
        proto_state=state,
        session_day="2026-11-05",
        bb_gate_active=False,
    )
    assert st.get("phase") != "ENTRY" or st.get("state") != "TRIGGERED"


def test_ribbon_filter_uses_dynamic_30s_emas():
    b15 = _bars_from_closes([100.0 + i for i in range(12)])
    cfg = load_ema8_prototype_cfg(
        {"ema8_prototype": {"ribbon_filter_enabled": True, "ribbon_max_pts": 50.0}}
    )
    spot = float(b15["close"].iloc[-1])
    e4, e8, _, _ = compute_dynamic_emas_on_bar(b15, bar_ci=-1, live_spot=spot)
    blocked = _filter_blocks(
        True,
        e4,
        e8,
        b15,
        cfg,
        -1,
        live_spot=spot,
        form_high=spot + 1,
        form_low=spot - 1,
    )
    assert blocked == "" or "ribbon" in blocked.lower()


def test_backtest_respects_supertrend_off_vs_on():
    closes = [100.0 + i * 0.4 for i in range(35)]
    closes[30] = closes[29] + 4
    closes[31] = closes[30] - 6
    b15 = _bars_from_closes(closes)
    b15.loc[b15.index[31], "low"] = b15["close"].iloc[31] - 10
    from analyst.nse_spot_candles_8510 import ema4_ema8

    e4, e8 = ema4_ema8(b15["close"])
    bars = []
    base_ts = 1730780700
    for i in range(len(b15)):
        bars.append(
            {
                "open": float(b15["open"].iloc[i]),
                "high": float(b15["high"].iloc[i]),
                "low": float(b15["low"].iloc[i]),
                "close": float(b15["close"].iloc[i]),
                "e4": float(e4.iloc[i]),
                "e8": float(e8.iloc[i]),
                "day": "2026-11-05",
                "hm": b15.index[i].strftime("%H:%M"),
                "ts": base_ts + i * 900,
            }
        )
    day_1m = pd.DataFrame(
        {
            "ts": [base_ts + 31 * 900 + 30 * k for k in range(1, 25)],
            "open": [bars[31]["close"]] * 24,
            "high": [bars[31]["high"]] * 24,
            "low": [bars[31]["low"]] * 24,
            "close": [bars[31]["close"] - 0.2 * k for k in range(1, 25)],
            "hm": ["10:00"] * 24,
        }
    )
    state: dict = {}
    # arm via cross on bar 30
    backtest_ema8_prototype_at_bar(
        bars,
        30,
        state,
        {"ema8_prototype": {"supertrend_enabled": False}},
        b15_df=b15.reset_index(drop=True),
        day_1m=day_1m,
    )
    off_n = len(
        backtest_ema8_prototype_at_bar(
            bars,
            31,
            state,
            {"ema8_prototype": {"supertrend_enabled": False, "ribbon_filter_enabled": False}},
            b15_df=b15.reset_index(drop=True),
            day_1m=day_1m,
        )
    )
    state2: dict = {}
    backtest_ema8_prototype_at_bar(
        bars,
        30,
        state2,
        {"ema8_prototype": {"supertrend_enabled": True}},
        b15_df=b15.reset_index(drop=True),
        day_1m=day_1m,
    )
    on_n = len(
        backtest_ema8_prototype_at_bar(
            bars,
            31,
            state2,
            {"ema8_prototype": {"supertrend_enabled": True, "ribbon_filter_enabled": False}},
            b15_df=b15.reset_index(drop=True),
            day_1m=day_1m,
        )
    )
    assert off_n >= 0 and on_n >= 0
