"""
tests/test_htf_wick_ema_rb.py — Unit and integration tests for
Institutional 1H / 4H Wick & Shaved Extremes + 1H 8-EMA First-Touch Engine.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.api.main import app
from app.engine.htf_wick_ema_rb import (
    compute_1h_8ema,
    evaluate_1h_8ema_first_touches,
    evaluate_level_touches_and_backtest,
    extract_htf_levels,
    generate_live_alerts_and_voice,
    get_htf_wick_ema_rb,
    get_thresholds,
    HtfLevel,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_shaved_high_and_shaved_low_detection():
    """Verify that candles with zero-wicks or wicks < 3.0 pts are correctly flagged as shaved extremes."""
    thresh = get_thresholds()
    sample_candles = [
        # Candle 1: Shaved Top (High == Close) and Shaved Bottom (Low == Open)
        {
            "ts": 1792419300,
            "o": 23000.0,
            "h": 23050.0,
            "l": 23000.0,
            "c": 23050.0,
            "closed": True,
        },
        # Candle 2: Standard Upper and Lower Wicks (wick >= 3.0 pts)
        {
            "ts": 1792422900,
            "o": 23020.0,
            "h": 23080.0,  # upper wick = 23080 - 23060 = 20 pts
            "l": 23000.0,  # lower wick = 23020 - 23000 = 20 pts
            "c": 23060.0,
            "closed": True,
        },
    ]

    levels = extract_htf_levels(sample_candles, "1H", thresh)
    assert len(levels) == 4

    # Candle 1 Upper: Shaved High
    c1_upper = next(l for l in levels if l.created_ts == 1792419300 and l.side == "RESISTANCE")
    assert c1_upper.is_shaved is True
    assert c1_upper.kind == "1H_SHAVED_HIGH"
    assert c1_upper.ref_price == 23050.0
    assert c1_upper.top == 23054.0  # +4.0 pt band
    assert c1_upper.bottom == 23046.0  # -4.0 pt band
    assert "Shaved High 23050.0" in c1_upper.label

    # Candle 1 Lower: Shaved Low
    c1_lower = next(l for l in levels if l.created_ts == 1792419300 and l.side == "SUPPORT")
    assert c1_lower.is_shaved is True
    assert c1_lower.kind == "1H_SHAVED_LOW"
    assert c1_lower.ref_price == 23000.0
    assert c1_lower.top == 23004.0  # +4.0 pt band
    assert c1_lower.bottom == 22996.0  # -4.0 pt band
    assert "Shaved Low 23000.0" in c1_lower.label

    # Candle 2 Upper: Standard Wick
    c2_upper = next(l for l in levels if l.created_ts == 1792422900 and l.side == "RESISTANCE")
    assert c2_upper.is_shaved is False
    assert c2_upper.kind == "1H_UPPER_WICK"
    assert c2_upper.ref_price == 23080.0
    assert c2_upper.wick_pts == 20.0
    assert c2_upper.top == 23080.0
    assert c2_upper.bottom == 23060.0
    assert "1H Wick Res [23060.0 - 23080.0]" in c2_upper.label

    # Candle 2 Lower: Standard Wick
    c2_lower = next(l for l in levels if l.created_ts == 1792422900 and l.side == "SUPPORT")
    assert c2_lower.is_shaved is False
    assert c2_lower.kind == "1H_LOWER_WICK"
    assert c2_lower.ref_price == 23000.0
    assert c2_lower.wick_pts == 20.0
    assert c2_lower.top == 23020.0
    assert c2_lower.bottom == 23000.0
    assert "1H Wick Supp [23000.0 - 23020.0]" in c2_lower.label


def test_1h_8ema_calculation_and_slope():
    """Verify that 8-period EMA computes correctly on 1H candle series and sets RISING/FALLING slope."""
    h1_candles = [
        {"ts": 1000 + i * 3600, "c": 22000.0 + i * 15.0}
        for i in range(12)
    ]
    ema_val, slope, hist = compute_1h_8ema(h1_candles, period=8)
    assert ema_val is not None
    assert len(hist) == 12
    # In an uptrend, slope must be RISING
    assert slope == "RISING"
    assert hist[-1]["val"] > hist[-2]["val"]

    # Downtrend test
    down_candles = [
        {"ts": 1000 + i * 3600, "c": 22000.0 - i * 15.0}
        for i in range(12)
    ]
    ema_val_down, slope_down, _ = compute_1h_8ema(down_candles, period=8)
    assert slope_down == "FALLING"


def test_first_touch_bounce_and_mfe_evaluation():
    """Verify first-touch reaction: marks touch_count=1, measures MFE, and records >=20 and >=40 pt targets."""
    thresh = get_thresholds()

    # Create a 1H support level at 22,500
    lvl = HtfLevel(
        id="1H-1000-lower",
        tf="1H",
        kind="1H_LOWER_WICK",
        side="SUPPORT",
        top=22510.0,
        bottom=22490.0,
        ref_price=22490.0,
        wick_pts=20.0,
        is_shaved=False,
        created_ts=1000,
        created_ist="2026-10-30 09:15",
        label="1H Wick Supp [22490.0 - 22510.0]",
    )

    # Intraday bars after candle close (1000 + 3600 = 4600)
    intraday = [
        {"ts": 4600, "o": 22550, "h": 22560, "l": 22540, "c": 22545},  # above
        {"ts": 4900, "o": 22545, "h": 22545, "l": 22505, "c": 22508},  # FIRST TOUCH into [22490-22510]
        {"ts": 5200, "o": 22520, "h": 22535, "l": 22520, "c": 22530},  # bounce +25 pts (low > top + prox)
        {"ts": 5500, "o": 22530, "h": 22555, "l": 22525, "c": 22550},  # bounce +45 pts (low > top + prox)
    ]

    stats = evaluate_level_touches_and_backtest([lvl], intraday, thresh, candle_duration_s=3600)

    assert stats["first_touches"] == 1
    assert stats["target_20_wins"] == 1
    assert stats["target_40_wins"] == 1
    assert lvl.touch_count == 1
    assert lvl.is_virgin is False
    assert lvl.hit_20 is True
    assert lvl.hit_40 is True
    assert lvl.mfe_bounce_pts >= 40.0
    assert lvl.direction_of_approach == "FROM_ABOVE_SUPPORT"


def test_exact_voice_alert_texts():
    """Verify that generated voice alerts match the exact spoken English required strings."""
    thresh = get_thresholds()

    # 1. Upper Wick (approaching from below)
    lvl_upper_wick = HtfLevel(
        id="1H-1-upper",
        tf="1H",
        kind="1H_UPPER_WICK",
        side="RESISTANCE",
        top=22795.0,
        bottom=22780.0,
        ref_price=22795.0,
        wick_pts=15.0,
        is_shaved=False,
        created_ts=1000,
        created_ist="2026-10-30 09:15",
        touch_count=0,
    )

    # 2. Shaved High (approaching from below)
    lvl_shaved_high = HtfLevel(
        id="1H-2-upper",
        tf="1H",
        kind="1H_SHAVED_HIGH",
        side="RESISTANCE",
        top=22804.0,
        bottom=22796.0,
        ref_price=22800.0,
        wick_pts=0.0,
        is_shaved=True,
        created_ts=1000,
        created_ist="2026-10-30 09:15",
        touch_count=0,
    )

    # 3. Lower Wick (approaching from above)
    lvl_lower_wick = HtfLevel(
        id="1H-3-lower",
        tf="1H",
        kind="1H_LOWER_WICK",
        side="SUPPORT",
        top=22650.0,
        bottom=22640.0,
        ref_price=22640.0,
        wick_pts=10.0,
        is_shaved=False,
        created_ts=1000,
        created_ist="2026-10-30 09:15",
        touch_count=0,
    )

    # 4. Shaved Low (approaching from above)
    lvl_shaved_low = HtfLevel(
        id="1H-4-lower",
        tf="1H",
        kind="1H_SHAVED_LOW",
        side="SUPPORT",
        top=22634.0,
        bottom=22626.0,
        ref_price=22630.0,
        wick_pts=0.0,
        is_shaved=True,
        created_ts=1000,
        created_ist="2026-10-30 09:15",
        touch_count=0,
    )

    # Test Upper Wick voice
    al_uw = generate_live_alerts_and_voice([lvl_upper_wick], ema8_val=None, current_spot=22793.0, thresholds=thresh)
    assert len(al_uw) == 1
    assert "approaching the one hour upper wick resistance at 22,795.0 from below for the first time" in al_uw[0]["voice_text"]
    assert "Watch for a 20 to 40 point rejection downward." in al_uw[0]["voice_text"]

    # Test Shaved High voice
    al_sh = generate_live_alerts_and_voice([lvl_shaved_high], ema8_val=None, current_spot=22798.0, thresholds=thresh)
    assert len(al_sh) == 1
    assert "approaching the one hour highest open-close level at 22,800.0 from below for the first time" in al_sh[0]["voice_text"]
    assert "Watch for resistance rejection." in al_sh[0]["voice_text"]

    # Test Lower Wick voice
    al_lw = generate_live_alerts_and_voice([lvl_lower_wick], ema8_val=None, current_spot=22643.0, thresholds=thresh)
    assert len(al_lw) == 1
    assert "coming near the one hour lower wick support at 22,640.0 from above for the first time" in al_lw[0]["voice_text"]
    assert "Watch for a 20 to 40 point bounce upward." in al_lw[0]["voice_text"]

    # Test Shaved Low voice
    al_sl = generate_live_alerts_and_voice([lvl_shaved_low], ema8_val=None, current_spot=22633.0, thresholds=thresh)
    assert len(al_sl) == 1
    assert "coming near the one hour lowest open-close price at 22,630.0 from above for the first time" in al_sl[0]["voice_text"]
    assert "Watch for support bounce." in al_sl[0]["voice_text"]

    # Test 1H 8-EMA Support voice
    al_ema_supp = generate_live_alerts_and_voice([], ema8_val=22700.0, current_spot=22703.0, thresholds=thresh)
    assert len(al_ema_supp) == 1
    assert "above the one hour 8 EMA and pulling back to test 8 EMA support at 22,700.0" in al_ema_supp[0]["voice_text"]
    assert "Watch for bullish bounce continuation." in al_ema_supp[0]["voice_text"]

    # Test 1H 8-EMA Resistance voice
    al_ema_res = generate_live_alerts_and_voice([], ema8_val=22700.0, current_spot=22697.0, thresholds=thresh)
    assert len(al_ema_res) == 1
    assert "coming from below the one hour 8 EMA and touching 8 EMA resistance at 22,700.0" in al_ema_res[0]["voice_text"]
    assert "Watch for bearish rejection." in al_ema_res[0]["voice_text"]


def test_api_endpoint_htf_wick_ema_rb(client):
    """Integration test verifying GET /api/desk/htf-wick-ema-rb returns ok=True, ema8, levels, and backtest."""
    from app import db
    from app.engine.htf_wick_ema_rb import _cached_payload
    import app.engine.htf_wick_ema_rb as engine_mod

    # Invalidate any in-memory cache for clean test
    engine_mod._cached_payload = None
    engine_mod._cached_last_ts = 0

    # Seed 1m candles for sec_id "13" spanning 10 hours so 1H & 4H candles form
    start_ts = 1792401300  # Monday 09:15
    seed = []
    for i in range(600):  # 10 hours of 1m candles
        t = start_ts + i * 60
        base = 22700.0 + (i // 60) * 10.0
        seed.append({
            "ts": t,
            "o": base,
            "h": base + 15.0,
            "l": base - 10.0,
            "c": base + 5.0,
            "v": 1000.0,
        })
    db.upsert_candles("13", seed)

    res = client.get("/api/desk/htf-wick-ema-rb?sec_id=13")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "ema8_1h" in data
    assert "active_levels" in data
    assert "live_alerts" in data
    assert "backtest_summary" in data

    # Verify backtest summary structure
    bt = data["backtest_summary"]
    assert "wick_1h" in bt
    assert "wick_4h" in bt
    assert "ema8_1h" in bt
    assert bt["total_1h_candles"] >= 8
    assert "win_rate_20_pct" in bt["wick_1h"]

