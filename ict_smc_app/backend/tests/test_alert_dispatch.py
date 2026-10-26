import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine.primitives import Bars, Swing
from app.engine.structure import StructEvent
from app.engine.analyze import Analysis
from app.signals.playbook import setup_b, setup_deep_reversal, evaluate
from app.signals import alerts as alerts_mod
from app.engine import rvol as rvol_mod


def _make_mock_analysis(candles, events, atr_val=20.0, tf="5m"):
    b = Bars.from_rows(candles)
    atr_arr = np.full(len(b), atr_val)
    an = Analysis(
        tf=tf,
        bars=b,
        atr=atr_arr,
        swings=[],
        zz=[],
        events=events,
        trend="bull",
        fvgs=[],
        obs=[],
        pools=[],
        liq=[],
        rng=None,
    )
    return an


def test_setup_b_giant_candle_exhaustion_guard():
    """Verify that a giant candle (> 65 pts or > 1.8x ATR) closing > 25 pts past swing level triggers the exhaustion guard."""
    base_ts = 1729674000  # 14:30 IST (09:00 UTC)
    candles = [
        {"ts": base_ts, "o": 22550.0, "h": 22566.0, "l": 22540.0, "c": 22555.0},
        {"ts": base_ts + 300, "o": 22566.0, "h": 22566.0, "l": 22460.0, "c": 22470.0},
    ]
    # Broken swing level at 22,500.0, close at 22,470.0 -> dist_past = 30.0 pts >= 25.0, range = 106 pts >= 65 pts
    ev = StructEvent(
        kind="CHOCH",
        dir="bear",
        i=1,
        ts=base_ts + 300,
        level=22500.0,
        swing_i=0,
        close=22470.0,
        displacement=True,
        origin_i=0,
        origin_price=22566.0,
    )
    an = _make_mock_analysis(candles, [ev], atr_val=20.0)
    sigs = setup_b(an)

    assert len(sigs) == 1
    sig = sigs[0]
    assert "⚡ DISPLACEMENT BREAKDOWN" in sig.name
    assert "DO NOT CHASE" in sig.name
    assert "Retest of" in sig.name
    assert sig.entry == 22500.0


def test_setup_b_normal_displacement_no_exhaustion():
    """Verify that a normal displacement candle does not trigger the exhaustion guard."""
    base_ts = 1729674000
    candles = [
        {"ts": base_ts, "o": 22550.0, "h": 22560.0, "l": 22540.0, "c": 22555.0},
        {"ts": base_ts + 300, "o": 22555.0, "h": 22565.0, "l": 22530.0, "c": 22532.0},
    ]
    # Broken swing level at 22,540.0, close at 22,532.0 -> range = 35 pts < 65 pts, dist_past = 8 pts < 25 pts
    ev = StructEvent(
        kind="CHOCH",
        dir="bear",
        i=1,
        ts=base_ts + 300,
        level=22540.0,
        swing_i=0,
        close=22532.0,
        displacement=True,
        origin_i=0,
        origin_price=22560.0,
    )
    an = _make_mock_analysis(candles, [ev], atr_val=20.0)
    sigs = setup_b(an)

    assert len(sigs) == 1
    sig = sigs[0]
    assert "MSS bearish through 22,540.0 (displacement)" in sig.name
    assert "DO NOT CHASE" not in sig.name


def test_setup_deep_reversal_engine():
    """Verify that deep session drop >= 100 pts + sweep/HL + MSS break fires 🚨 A+ DEEP REVERSAL SNIPER."""
    base_ts = 1729660000
    # Simulate session drop: starts at 22,590, drops to low 22,372 (218 pt drop), then Higher Low at 22,388, and MSS break at 22,442
    candles = [
        {"ts": base_ts + i * 300, "o": 22590.0 - i * 15, "h": 22595.0 - i * 15, "l": 22575.0 - i * 15, "c": 22580.0 - i * 15}
        for i in range(10)
    ]
    # Candle 10: Deep sweep low at 22,372 with rejection
    candles.append({"ts": base_ts + 3000, "o": 22440.0, "h": 22445.0, "l": 22372.0, "c": 22410.0})
    # Candle 11: Higher low at 22,388
    candles.append({"ts": base_ts + 3300, "o": 22410.0, "h": 22425.0, "l": 22388.0, "c": 22420.0})
    # Candle 12: MSS breakout candle through 22,430.0 closing at 22,445.0
    candles.append({"ts": base_ts + 3600, "o": 22420.0, "h": 22450.0, "l": 22415.0, "c": 22445.0})

    ev = StructEvent(
        kind="CHOCH",
        dir="bull",
        i=12,
        ts=base_ts + 3600,
        level=22430.0,
        swing_i=10,
        close=22445.0,
        displacement=True,
        origin_i=10,
        origin_price=22372.0,
    )
    an = _make_mock_analysis(candles, [ev], atr_val=20.0)
    sigs = setup_deep_reversal(an, None)

    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.setup == "DEEP_REV"
    assert sig.status == "TRADE"
    assert "🚨 A+ DEEP REVERSAL SNIPER: BUY" in sig.name
    assert sig.entry == 22445.0
    assert sig.sl == round(22372.0 - 12.0, 1)  # Stop loss below 22,372 sweep low
    assert sig.tp > sig.entry  # TP targeted toward 50% Fib recovery


def test_eod_settlement_window_tag():
    """Verify that signals triggered between 15:15 and 15:30 IST receive the EOD settlement warning."""
    # 15:20 IST corresponds to UTC 09:50
    dt_eod = datetime(2026, 10, 23, 9, 50, 0, tzinfo=timezone.utc)
    ts_eod = int(dt_eod.timestamp())

    candles = [
        {"ts": ts_eod - 300, "o": 22500.0, "h": 22510.0, "l": 22495.0, "c": 22505.0},
        {"ts": ts_eod, "o": 22505.0, "h": 22530.0, "l": 22500.0, "c": 22525.0},
    ]
    ev = StructEvent(
        kind="CHOCH",
        dir="bull",
        i=1,
        ts=ts_eod,
        level=22515.0,
        swing_i=0,
        close=22525.0,
        displacement=True,
        origin_i=0,
        origin_price=22500.0,
    )
    an = _make_mock_analysis(candles, [ev], atr_val=20.0)
    sigs = setup_b(an)

    assert len(sigs) == 1
    assert "[EOD SETTLEMENT / NEXT-DAY BIAS ONLY — DO NOT OPEN INTRADAY OPTIONS]" in sigs[0].name


def test_alerts_recent_default_capacity_500():
    """Verify that alerts_mod.recent() defaults to a capacity limit of 500 rows."""
    import inspect
    sig = inspect.signature(alerts_mod.recent)
    assert sig.parameters["limit"].default == 500


def test_rvol_spike_floor_and_cap():
    """Verify that volume.rvol() floors denominator and caps rvol at 10.0x max."""
    from app.engine import volume
    # Build 1m rows with realistic session volume (median 5m volume > 15,000)
    # Followed by a 40,000 volume burst today in a thin slot
    base_ts = 1729656900  # 09:15 IST
    day1_ts = base_ts
    day2_ts = base_ts + 86400
    rows = []
    # Day 1: normal volume (6,000 per minute = 30,000 per 5m bar)
    for m in range(60):
        rows.append({"ts": day1_ts + m * 60, "v": 6000.0})
    # Slot 12:40 on Day 1 has only 10 volume
    slot_ts = day1_ts + (3 * 3600 + 25 * 60)  # 12:40 IST
    rows.append({"ts": slot_ts, "v": 10.0})

    # Day 2: 40,000 volume burst at 12:40
    slot2_ts = day2_ts + (3 * 3600 + 25 * 60)
    rows.append({"ts": slot2_ts, "v": 40000.0})

    rv_res = volume.rvol(rows, "5m")
    # Even if historical baseline was tiny, slot_rvol must NOT exceed 10.0x
    for k, v in rv_res.items():
        if v.get("slot_rvol") is not None:
            assert v["slot_rvol"] <= 10.0
            assert v["slot_base"] >= 10500.0


def test_rvol_spike_suppressed_on_same_candle_breakout():
    """Verify that rvol_mod.events() suppresses standalone RVOL_SPIKE if a breakout occurred on that candle."""
    from app.engine import rvol as rvol_engine
    from app.timeutil import ist_date
    t = 1729668000
    d = ist_date(t).isoformat()
    res = {"available": True, "day": d, "points": [{"ts": t + 240, "c20": 1.6}]}
    bar_rvol = {t: {"slot_rvol": 4.2, "rvol": 4.2, "v": 50000}}
    # Breakout occurred on same candle
    breakouts = [{"ts": t, "kind": "BOS", "dir": "bull", "text": "BOS bull through 22,600"}]

    evs = rvol_engine.events(res, bar_rvol, breakouts, "5m")
    kinds = [e["kind"] for e in evs]
    # BREAKOUT_WITH_RVOL should be present, but standalone RVOL_SPIKE should be suppressed
    assert "BREAKOUT_WITH_RVOL" in kinds
    assert "RVOL_SPIKE" not in kinds


def test_midday_ote_override_with_crvol():
    """Verify that a 12:45 IST OTE setup with cRVOL >= 1.2x is NOT skipped as outside trade windows."""
    from app.signals.playbook import setup_c
    # 12:45 IST is 07:15 UTC
    dt_1245 = datetime(2026, 10, 26, 7, 10, 0, tzinfo=timezone.utc)
    ts_1245 = int(dt_1245.timestamp())

    # Range: 22,572.5 to 22,636.7 (EQ 22,604.6, OTE 61.8-79%: 22,586.0–22,597.1)
    candles = [
        {"ts": ts_1245 - 600, "o": 22572.5, "h": 22636.7, "l": 22572.5, "c": 22630.0},
        {"ts": ts_1245 - 300, "o": 22630.0, "h": 22635.0, "l": 22600.0, "c": 22605.0},
        # 12:45 candle dips into OTE: low 22,590.2, close 22,592.0
        {"ts": ts_1245, "o": 22605.0, "h": 22610.0, "l": 22590.2, "c": 22592.0},
    ]
    b = Bars.from_rows(candles)
    an = Analysis(
        tf="5m",
        bars=b,
        atr=np.full(len(b), 18.0),
        swings=[],
        zz=[],
        events=[],
        trend="bull",
        fvgs=[],
        obs=[],
        pools=[],
        liq=[],
        rng={
            "dir": "bull",
            "low": 22572.5,
            "high": 22636.7,
            "eq": 22604.6,
            "ote_band": (22586.0, 22597.1),
            "extreme_i": 0,
            "event_ts": ts_1245 - 600,
            "event": "BOS",
            "zone": "discount",
        },
    )

    # 1. Without context, midday is outside trade windows -> status == SKIP
    sigs_no_ctx = setup_c(an, None, ctx=None)
    assert len(sigs_no_ctx) == 1
    assert sigs_no_ctx[0].status == "SKIP"
    assert any("outside the trade windows" in r for r in sigs_no_ctx[0].reasons)

    # 2. With cRVOL >= 1.20, Institutional Override allows the trade!
    sigs_with_ctx = setup_c(an, None, ctx={"crvol": 1.51, "slot_rvol": 1.8})
    assert len(sigs_with_ctx) == 1
    assert sigs_with_ctx[0].status == "TRADE"
    assert not any("outside the trade windows" in r for r in sigs_with_ctx[0].reasons)


def test_ote_wick_tap_rejection_and_tactical_sl():
    """Verify that a candle whose wick tapped the OTE discount band and closed above EQ is accepted with tactical SL."""
    from app.signals.playbook import setup_c
    base_ts = 1729668000
    # Range: 22,566.0 to 22,718.0 (152 pt range, EQ 22,642, OTE band 22,598 - 22,624)
    # Candle wicks into OTE discount (low 22,615.0), rejects bullishly and closes at 22,683.3
    candles = [
        {"ts": base_ts, "o": 22566.0, "h": 22718.0, "l": 22566.0, "c": 22710.0},
        {"ts": base_ts + 300, "o": 22710.0, "h": 22712.0, "l": 22615.0, "c": 22683.3},
    ]
    b = Bars.from_rows(candles)
    an = Analysis(
        tf="5m",
        bars=b,
        atr=np.full(len(b), 20.0),
        swings=[],
        zz=[],
        events=[],
        trend="bull",
        fvgs=[],
        obs=[],
        pools=[],
        liq=[],
        rng={
            "dir": "bull",
            "low": 22566.0,
            "high": 22718.0,
            "eq": 22642.0,
            "ote_band": (22598.0, 22624.0),
            "extreme_i": 0,
            "event_ts": base_ts,
            "event": "BOS",
            "zone": "discount",
        },
    )

    sigs = setup_c(an, None, ctx={"crvol": 1.4})
    assert len(sigs) == 1
    # Check that story template correctly describes wick tap rather than claiming close was inside OTE
    story_text = " ".join(sigs[0].story)
    assert "wick tapped OTE" in story_text
    assert "closed 22,683.3 inside the OTE band" not in story_text
