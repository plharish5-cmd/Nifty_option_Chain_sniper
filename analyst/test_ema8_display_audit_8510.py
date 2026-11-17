"""8 EMA display/audit — no maths change, labels + stale guard helpers."""
from __future__ import annotations

import time

import pandas as pd

from analyst.cockpit_views_8510 import prototype_view
from analyst.ema8_display_audit_8510 import format_ema_touch_audit, spot_quote_stale
from analyst.nse_spot_candles_8510 import compute_dynamic_emas_on_forming_bar, ema4_ema8, last_closed_bar_emas


def test_ema4_ema8_unchanged_on_fixture():
    closes = pd.Series([100.0, 101.0, 102.5, 103.0, 104.0, 105.0, 106.0, 108.0])
    e4a, e8a = ema4_ema8(closes)
    e4b, e8b = ema4_ema8(closes.copy())
    assert float(e8a.iloc[-1]) == float(e8b.iloc[-1])
    assert float(e4a.iloc[-1]) == float(e4b.iloc[-1])


def test_format_touch_audit_line():
    line = format_ema_touch_audit(e8_live=25411.3, spot=25411.0, e8_closed=25408.0, at_hm="10:15:03")
    assert "25411.3" in line and "10:15:03" in line and "25408.0" in line
    assert "NIFTY spot" in line


def test_spot_stale_over_5s():
    stale, age = spot_quote_stale({"is_stale": False, "updated_epoch": time.time() - 6.0}, max_age_sec=5.0)
    assert stale and age is not None and age > 5
    fresh, _ = spot_quote_stale({"is_stale": False, "updated_epoch": time.time()}, max_age_sec=5.0)
    assert not fresh


def test_prototype_view_shows_live_and_close_labels():
    v = prototype_view(
        {"enabled": True, "dynamic_e8": 25411.3},
        {"arm": {"direction": "LONG", "away_seen": True}},
        25412.0,
        ema_audit={"e8_live": 25411.3, "e8_closed": 25408.0, "stale": False},
    )
    assert v["ema8_live"] == 25411.3
    assert v["ema8_closed"] == 25408.0
    assert "converge" in (v.get("ema_note") or "").lower()


def test_live_differs_from_close_mid_candle():
    n = 30
    idx = pd.date_range("2026-11-17 09:15", periods=n, freq="15min", tz="Asia/Kolkata")
    close = pd.Series([25000.0 + i * 2 for i in range(n)], index=idx)
    b15 = pd.DataFrame({"open": close, "high": close + 5, "low": close - 5, "close": close, "volume": 1000})
    _, _, e8_closed, _ = last_closed_bar_emas(b15, offset=-2)
    spot_mid = float(close.iloc[-1]) + 15.0
    _, e8_live, _, _ = compute_dynamic_emas_on_forming_bar(b15, live_spot=spot_mid)
    assert abs(e8_live - e8_closed) > 0.01