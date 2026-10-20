"""
tests/test_pa_course.py — Unit tests for Institutional Price Action Course Engine (pa_course_engine.py).
"""
import sys
from pathlib import Path

# Add backend and parent directories to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir.parent))
sys.path.insert(0, str(backend_dir))

from app.engine.pa_course_engine import (
    analyze_pa_course,
    _chain_metrics,
    _atr,
    _swings,
    _detect_fvgs,
    _detect_bprs,
    _detect_sbb,
)


def test_pa_course_insufficient_candles():
    rows = [{"ts": 1000 + i * 300, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "closed": True} for i in range(15)]
    res = analyze_pa_course(rows, tf="5m", spot=100.5)
    assert res["available"] is False
    assert "Insufficient" in res["reason"]


def test_pa_course_chain_metrics():
    mock_chain = [
        {"strike": 22400, "ce": {"oi": 50000, "ltp": 180.0, "delta": 0.65}, "pe": {"oi": 150000, "ltp": 45.0, "delta": -0.35}},
        {"strike": 22500, "ce": {"oi": 200000, "ltp": 110.0, "delta": 0.52}, "pe": {"oi": 120000, "ltp": 95.0, "delta": -0.48}},
        {"strike": 22600, "ce": {"oi": 300000, "ltp": 60.0, "delta": 0.38}, "pe": {"oi": 40000, "ltp": 160.0, "delta": -0.62}},
    ]
    m = _chain_metrics(mock_chain, spot=22510.0)
    assert m["available"] is True
    assert m["call_wall"] == 22600
    assert m["put_wall"] == 22400
    assert m["pcr"] > 0
    assert m["best_ce"]["strike"] == 22500
    assert m["best_pe"]["strike"] == 22500


def test_pa_course_synthetic_market():
    # Build 45 candles with a swing high, drop to swing low, then strong MSB impulse
    rows = []
    base_ts = 1700000000
    price = 22400.0
    for i in range(45):
        if i < 15:
            # Rise to swing high
            price += 10.0
        elif i < 28:
            # Deep drop to lower low
            price -= 18.0
        else:
            # Strong impulsive pump breaking swing high
            price += 22.0
        rows.append({
            "ts": base_ts + i * 300,
            "o": price - 5.0,
            "h": price + 6.0,
            "l": price - 7.0,
            "c": price + 4.0,
            "closed": True,
        })

    res = analyze_pa_course(rows, tf="5m", spot=rows[-1]["c"])
    assert res["available"] is True
    assert "signal" in res
    assert "gpx_box" in res
    assert res["gpx_box"]["range_high"] >= res["gpx_box"]["range_low"]
    assert "msb_setups" in res
    assert "rote_setups" in res
    assert "chain_metrics" in res


def test_pa_course_oct8_real_data():
    from oct8_data import five_min
    raw = five_min()
    # 28 candles in single session replay; combine with offset to simulate 2-session replay (56 candles)
    rows = list(raw) + [{**r, "ts": r["ts"] + 86400} for r in raw]
    assert len(rows) >= 30
    res = analyze_pa_course(rows, tf="5m", spot=rows[-1]["c"])
    assert res["available"] is True
    assert res["spot"] > 0
    assert res["atr"] > 0
    assert res["gpx_box"]["zone"] in ("PREMIUM", "DISCOUNT")
    assert "bpr_zones" in res
    assert "sbb_setups" in res
    assert "trade_management" in res
    assert "directive" in res["trade_management"]


def test_pa_course_bpr_and_super_bpr():
    # 2 opposing FVGs overlapping: Bull [22200, 22250] + Bear [22220, 22270] -> BPR [22220, 22250]
    fvgs = [
        {"i": 5, "ts": 1700001500, "dir": "BULL", "bot": 22200.0, "top": 22250.0, "mid": 22225.0},
        {"i": 10, "ts": 1700003000, "dir": "BEAR", "bot": 22220.0, "top": 22270.0, "mid": 22245.0},
    ]
    bprs = _detect_bprs(fvgs)
    assert len(bprs) == 1
    assert bprs[0]["kind"] == "BPR"
    assert bprs[0]["bot"] == 22220.0
    assert bprs[0]["top"] == 22250.0
    assert bprs[0]["overlaps"] == 2

    # Add a 3rd FVG covering the intersection -> Upgrades to SUPER BPR (overlaps >= 3)
    fvgs.append({"i": 14, "ts": 1700004200, "dir": "BEAR", "bot": 22215.0, "top": 22260.0, "mid": 22237.5})
    super_bprs = _detect_bprs(fvgs)
    assert len(super_bprs) >= 1
    best_super = next((b for b in super_bprs if b["kind"] == "SUPER BPR"), None)
    assert best_super is not None
    assert best_super["overlaps"] >= 3
    assert "SUPER BPR" in best_super["label"]


def test_pa_course_sbb_detection():
    # Bearish breaker: H1 -> L1 (interim HL) -> H2 (sweeps H1) -> breaks below L1
    highs = [
        {"i": 2, "price": 22300.0, "ts": 1700000600},
        {"i": 8, "price": 22325.0, "ts": 1700002400},
    ]
    lows = [
        {"i": 5, "price": 22250.0, "ts": 1700001500},
    ]
    # Build closed bars: at bar 5 (interim L1), bar 8 (H2 sweep), and bar 10 (breaks below 22250)
    closed = []
    for i in range(15):
        if i == 5:
            closed.append({"ts": 1700000000 + i * 300, "o": 22260.0, "h": 22265.0, "l": 22250.0, "c": 22255.0})
        elif i == 10:
            closed.append({"ts": 1700000000 + i * 300, "o": 22252.0, "h": 22254.0, "l": 22230.0, "c": 22235.0})
        else:
            closed.append({"ts": 1700000000 + i * 300, "o": 22280.0, "h": 22290.0, "l": 22270.0, "c": 22285.0})

    sfp_events = [{"side": "SHORT", "ts": 1700002400, "level": 22300.0}]
    # Bearish FVG touching breaker bounds [22250, 22265]
    fvgs = [{"i": 9, "ts": 1700002700, "dir": "BEAR", "bot": 22245.0, "top": 22260.0}]

    sbbs = _detect_sbb(highs, lows, closed, fvgs, sfp_events, atr=15.0, in_premium=True)
    assert len(sbbs) >= 1
    sbb = sbbs[0]
    assert sbb["dir"] == "BEAR"
    assert sbb["side"] == "SHORT"
    assert sbb["is_super"] is True
    assert sbb["kind"] == "SUPER BREAKER (SBB)"
    assert sbb["sfp_confirmed"] is True
    assert sbb["fvg_touch"] is True
    assert sbb["in_aligned_zone"] is True


def test_pa_course_trade_management_calculation():
    # Test Trade Management 1R Breakeven and directive
    rows = []
    base_ts = 1700000000
    price = 22400.0
    for i in range(45):
        if i < 15:
            price += 10.0
        elif i < 28:
            price -= 18.0
        else:
            price += 22.0
        rows.append({
            "ts": base_ts + i * 300,
            "o": price - 5.0,
            "h": price + 6.0,
            "l": price - 7.0,
            "c": price + 4.0,
            "closed": True,
        })
    res = analyze_pa_course(rows, tf="5m", spot=rows[-1]["c"])
    tm = res["trade_management"]
    assert "directive" in tm
    if tm["entry"] is not None and tm["sl"] is not None:
        # Check 1R math: distance = abs(entry - sl), be_1r = entry +/- distance
        r_dist = tm["r_dist"]
        assert r_dist > 0
        if "BUY CE" in res["signal"]:
            assert tm["be_1r"] == round(tm["entry"] + r_dist, 1)
        elif "BUY PE" in res["signal"]:
            assert tm["be_1r"] == round(tm["entry"] - r_dist, 1)

