"""Unit tests for 8 EMA Prototype state machine."""
from __future__ import annotations

import pandas as pd

from analyst.ema8_prototype_8510 import (
    _close_crossed_above,
    _opposite_8ema_close,
    _ribbon_blocks,
    _touch_retest,
    prototype_spot_is_long,
    backtest_ema8_prototype_at_bar,
    evaluate_ema8_prototype,
    load_ema8_prototype_cfg,
    ensure_forming_b15_bar,
)


def _bars_from_closes(closes):
    idx = pd.date_range("2026-11-05 09:15", periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 2 for c in closes],
            "low": [c - 2 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def test_touch_retest_long_requires_bracketing_8ema():
    e8 = 100.0
    tol = 5.0
    assert not _touch_retest(bullish=True, low=90, high=92, e8=e8, tol=tol)
    assert _touch_retest(bullish=True, low=98, high=103, e8=e8, tol=tol)


def test_touch_retest_short_requires_bracketing_8ema():
    e8 = 100.0
    tol = 5.0
    assert not _touch_retest(bullish=False, low=88, high=90, e8=e8, tol=tol)
    assert _touch_retest(bullish=False, low=97, high=102, e8=e8, tol=tol)


def test_ribbon_blocks_arm_on_cross_candle():
    closes = [100.0] * 12
    closes[10] = 100.0
    closes[11] = 108.0
    b15 = _bars_from_closes(closes)
    b15.loc[b15.index[11], "close"] = 108.0
    b15.loc[b15.index[10], "close"] = 100.0
    b15.loc[b15.index[11], "high"] = 108.5
    b15.loc[b15.index[10], "high"] = 100.5
    cfg = load_ema8_prototype_cfg(
        {"ema8_prototype": {"ribbon_filter_enabled": True, "ribbon_max_pts": 50.0}}
    )
    state: dict = {}
    _, st = evaluate_ema8_prototype(
        b15,
        bar_ci=11,
        cfg=cfg,
        proto_state=state,
        session_day="2026-11-05",
    )
    assert state.get("arm") is None
    assert "ribbon" in str(st.get("block_reason", "")).lower()


def test_opposite_close_exit_action_on_open_long():
    cfg = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False}})
    closes = [102.0] * 12
    closes[10] = 100.0
    closes[11] = 99.0
    b15 = _bars_from_closes(closes)
    state = {"day": "2026-11-05", "open": {"direction": "LONG"}}
    _, st = evaluate_ema8_prototype(
        b15,
        bar_ci=11,
        cfg=cfg,
        proto_state=state,
        session_day="2026-11-05",
    )
    actions = st.get("actions") or []
    assert any(a.get("type") == "SQUARE_OFF" for a in actions)
    assert state.get("open") is None


def test_opposite_8ema_close_detector():
    assert _opposite_8ema_close("LONG", 99.0, 100.0)
    assert _opposite_8ema_close("SHORT", 101.0, 100.0)
    assert not _opposite_8ema_close("LONG", 101.0, 100.0)


def test_ensure_forming_bar_appends_when_resample_lags():
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    old = pd.DataFrame(
        {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1]},
        index=[pd.Timestamp("2026-11-05 11:45", tz=ist)],
    )
    bar_open = int(pd.Timestamp("2026-11-05 12:00", tz=ist).timestamp())
    out, ci = ensure_forming_b15_bar(old, bar_open_ts=bar_open, live_spot=101.5)
    assert len(out) == 2
    assert ci == -1
    assert float(out.iloc[-1]["close"]) == 101.5


def test_backtest_skips_entry_when_bt_open():
    from analyst.nse_spot_candles_8510 import ema4_ema8

    closes = [100.0 + i * 0.4 for i in range(8)]
    b15 = _bars_from_closes(closes)
    e4, e8 = ema4_ema8(b15["close"])
    bars = []
    for i in range(len(b15)):
        bars.append(
            {
                "close": float(b15["close"].iloc[i]),
                "high": float(b15["high"].iloc[i]),
                "low": float(b15["low"].iloc[i]),
                "e4": float(e4.iloc[i]),
                "e8": float(e8.iloc[i]),
                "day": "2026-11-05",
                "ts": 1000 + i * 900,
            }
        )
    state = {"bt_open": {"direction": "LONG"}, "arm": {"direction": "LONG", "cross_i": 0}}
    assert backtest_ema8_prototype_at_bar(bars, 3, state, {"ema8_prototype": {}}) == []


def test_prototype_spot_is_long_from_levels_not_option_side():
    assert prototype_spot_is_long(24500.0, 24540.0)
    assert not prototype_spot_is_long(24500.0, 24460.0)
    assert prototype_spot_is_long(0, 0, strategy_id="EMA8_PROTOTYPE_LONG")
    assert not prototype_spot_is_long(0, 0, strategy_id="EMA8_PROTOTYPE_SHORT")


def test_ribbon_blocks_helper_respects_toggle():
    cfg_on = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": True, "ribbon_max_pts": 3}})
    cfg_off = load_ema8_prototype_cfg({"ema8_prototype": {"ribbon_filter_enabled": False}})
    assert _ribbon_blocks(100.0, 101.5, cfg_on)
    assert not _ribbon_blocks(100.0, 101.5, cfg_off)
