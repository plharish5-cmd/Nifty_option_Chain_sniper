"""
tests/test_gtf.py — Unit tests for GTF Institutional Demand & Supply Indicator 2.0 Equivalent.
Tests:
  - Boring candle detection (body < abr * 0.6)
  - Explosive candle detection (body > abr * 1.5)
  - Demand zone construction (RBR and DBR patterns)
  - Supply zone construction (DBD and RBD patterns)
  - Conservative, Aggressive, and Dynamic marking modes
  - Freshness lifecycle: FRESH, TESTED, BROKEN/MITIGATED
  - compute_gtf module helper
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

from app.engine.gtf import (
    GtfSupplyDemandEngine,
    compute_abr,
    compute_gtf,
)
from app.engine.primitives import Bars


def _make_bars(rows) -> Bars:
    return Bars.from_rows(rows)


def test_compute_abr():
    """Verify average body range computation."""
    closes = np.array([100.0, 105.0, 110.0, 112.0, 115.0])
    opens = np.array([99.0, 100.0, 105.0, 110.0, 113.0])
    abr = compute_abr(closes, opens, period=3)
    assert len(abr) == 5
    # First body = 1.0, second = 5.0, third = 5.0 -> mean = (1+5+5)/3 = 3.67
    assert np.isclose(abr[2], (1.0 + 5.0 + 5.0) / 3.0)


def test_boring_and_explosive_detection():
    """Verify classification of boring consolidation candles vs explosive institutional candles."""
    engine = GtfSupplyDemandEngine({
        "boring_body_ratio": 0.60,
        "explosive_body_ratio": 1.50,
        "abr_period": 5,
    })

    # 10 candles with ~10 pt body, then 1 tiny 2 pt candle (boring), then 1 huge 30 pt candle (explosive)
    rows = []
    t0 = 1_700_000_000
    for i in range(10):
        rows.append({"ts": t0 + i * 300, "o": 24000.0 + i * 10, "h": 24012.0 + i * 10, "l": 23998.0 + i * 10, "c": 24010.0 + i * 10})

    # Boring candle: open 24100, close 24102, high 24105, low 24098 (body = 2.0 pts)
    rows.append({"ts": t0 + 10 * 300, "o": 24100.0, "h": 24105.0, "l": 24098.0, "c": 24102.0})

    # Explosive candle: open 24102, close 24140, high 24145, low 24100 (body = 38.0 pts)
    rows.append({"ts": t0 + 11 * 300, "o": 24102.0, "h": 24145.0, "l": 24100.0, "c": 24140.0})

    # Normal follow through
    rows.append({"ts": t0 + 12 * 300, "o": 24140.0, "h": 24150.0, "l": 24135.0, "c": 24145.0})

    bars = _make_bars(rows)
    res = engine.detect_zones(bars, tf="5m", mode_override="conservative")

    assert res["available"] is True
    assert len(res["zones"]) > 0

    # There should be at least one DEMAND zone identified from the boring + explosive up sequence
    demand_zones = [z for z in res["zones"] if z["type"] == "DEMAND"]
    assert len(demand_zones) >= 1
    dz = demand_zones[0]
    assert dz["dir"] == "bull"
    assert dz["status"] in ("FRESH", "TESTED")
    assert dz["top"] >= dz["bottom"]


def test_marking_modes_conservative_vs_aggressive_vs_dynamic():
    """Verify boundary differences between Conservative, Aggressive, and Dynamic modes."""
    t0 = 1_700_000_000
    rows = []
    # 10 baseline bars
    for i in range(10):
        rows.append({"ts": t0 + i * 300, "o": 24000.0, "h": 24012.0, "l": 23998.0, "c": 24010.0})

    # Boring candle with distinct body and wicks:
    # Open: 24000, Close: 24003, High: 24015 (upper wick), Low: 23985 (lower wick)
    rows.append({"ts": t0 + 10 * 300, "o": 24000.0, "h": 24015.0, "l": 23985.0, "c": 24003.0})

    # Explosive green candle
    rows.append({"ts": t0 + 11 * 300, "o": 24003.0, "h": 24050.0, "l": 24002.0, "c": 24045.0})
    rows.append({"ts": t0 + 12 * 300, "o": 24045.0, "h": 24055.0, "l": 24040.0, "c": 24050.0})

    bars = _make_bars(rows)
    engine = GtfSupplyDemandEngine({"boring_body_ratio": 0.60, "explosive_body_ratio": 1.50})

    # 1. Conservative Mode: Proximal = Highest Body (24003.0), Distal = Lowest Wick (23985.0)
    res_cons = engine.detect_zones(bars, tf="5m", mode_override="conservative")
    dz_cons = [z for z in res_cons["zones"] if z["type"] == "DEMAND"][0]
    assert dz_cons["proximal"] == 24003.0
    assert dz_cons["distal"] == 23985.0

    # 2. Aggressive Mode: Proximal = Highest Wick (24015.0), Distal = Lowest Wick (23985.0)
    res_aggr = engine.detect_zones(bars, tf="5m", mode_override="aggressive")
    dz_aggr = [z for z in res_aggr["zones"] if z["type"] == "DEMAND"][0]
    assert dz_aggr["proximal"] == 24015.0
    assert dz_aggr["distal"] == 23985.0

    # 3. Dynamic Mode: Proximal and distal adjusted by ATR buffer
    res_dyn = engine.detect_zones(bars, tf="5m", mode_override="dynamic")
    dz_dyn = [z for z in res_dyn["zones"] if z["type"] == "DEMAND"][0]
    # In dynamic mode, proximal is padded higher and distal lower
    assert dz_dyn["proximal"] > dz_cons["proximal"]
    assert dz_dyn["distal"] < dz_cons["distal"]


def test_supply_zone_construction():
    """Verify Supply Zone construction (DBD/RBD) on strong downward expansion."""
    t0 = 1_700_000_000
    rows = []
    for i in range(8):
        rows.append({"ts": t0 + i * 300, "o": 24500.0, "h": 24510.0, "l": 24490.0, "c": 24502.0})

    # Boring candle: body = 2 pts (24500 to 24498)
    rows.append({"ts": t0 + 8 * 300, "o": 24500.0, "h": 24515.0, "l": 24495.0, "c": 24498.0})

    # Explosive red candle down: open 24498, close 24450 (body = 48 pts)
    rows.append({"ts": t0 + 9 * 300, "o": 24498.0, "h": 24500.0, "l": 24445.0, "c": 24450.0})
    rows.append({"ts": t0 + 10 * 300, "o": 24450.0, "h": 24455.0, "l": 24440.0, "c": 24445.0})

    bars = _make_bars(rows)
    engine = GtfSupplyDemandEngine()
    res = engine.detect_zones(bars, tf="5m", mode_override="conservative")

    supply_zones = [z for z in res["zones"] if z["type"] == "SUPPLY"]
    assert len(supply_zones) >= 1
    sz = supply_zones[0]
    assert sz["dir"] == "bear"
    assert sz["pattern"] in ("DBD", "RBD")
    # In conservative mode: Proximal = Lowest Body (24498.0), Distal = Highest Wick (24515.0)
    assert sz["proximal"] == 24498.0
    assert sz["distal"] == 24515.0


def test_freshness_lifecycle_tested_and_broken():
    """Verify that zones transition from FRESH to TESTED and BROKEN."""
    t0 = 1_700_000_000
    rows = []
    for i in range(10):
        rows.append({"ts": t0 + i * 300, "o": 24000.0, "h": 24010.0, "l": 23990.0, "c": 24005.0})

    # Boring candle: 24000 to 24002, low: 23985
    rows.append({"ts": t0 + 10 * 300, "o": 24000.0, "h": 24010.0, "l": 23985.0, "c": 24002.0})

    # Explosive candle up: 24002 to 24050
    rows.append({"ts": t0 + 11 * 300, "o": 24002.0, "h": 24055.0, "l": 24001.0, "c": 24050.0})

    # Bar 12: High above zone (24050 to 24060) -> Zone is FRESH
    rows.append({"ts": t0 + 12 * 300, "o": 24050.0, "h": 24060.0, "l": 24040.0, "c": 24055.0})

    bars_fresh = _make_bars(rows)
    engine = GtfSupplyDemandEngine()
    res_fresh = engine.detect_zones(bars_fresh, tf="5m", mode_override="conservative")
    assert res_fresh["zones"][0]["status"] == "FRESH"

    # Bar 13: Retests zone (dips into 23995, above distal 23985, closes at 24010) -> TESTED
    rows.append({"ts": t0 + 13 * 300, "o": 24055.0, "h": 24055.0, "l": 23995.0, "c": 24010.0})
    bars_tested = _make_bars(rows)
    res_tested = engine.detect_zones(bars_tested, tf="5m", mode_override="conservative")
    assert res_tested["zones"][0]["status"] == "TESTED"
    assert res_tested["zones"][0]["test_count"] >= 1

    # Bar 14: Smashes through zone (closes below distal: 23970) -> BROKEN
    rows.append({"ts": t0 + 14 * 300, "o": 24010.0, "h": 24012.0, "l": 23965.0, "c": 23970.0})
    bars_broken = _make_bars(rows)
    res_broken = engine.detect_zones(bars_broken, tf="5m", mode_override="conservative")
    assert res_broken["zones"][0]["status"] == "BROKEN"


def test_compute_gtf_helper():
    """Verify compute_gtf module helper returns structured payload."""
    t0 = 1_700_000_000
    rows = []
    for i in range(15):
        rows.append({"ts": t0 + i * 300, "o": 24000.0 + i * 5, "h": 24010.0 + i * 5, "l": 23995.0 + i * 5, "c": 24008.0 + i * 5})
    bars = _make_bars(rows)
    res = compute_gtf(bars, tf="5m", marking_mode="dynamic")
    assert res["available"] is True
    assert "zones" in res
    assert "stats" in res
    assert res["marking_mode"] == "dynamic"


def test_gtf_massive_legout_relaxation_and_proximity_sorting():
    """
    Verify 23 Oct morning breakdown behavior:
    1. Base candle with body ratio ~0.58 at 22,710–22,755 qualifies for base relaxation
       when followed by a massive leg-out crash (>2.0x ATR).
    2. Supply zone created is Fresh with grade A/A+ (RBD pattern).
    3. Continuation pause at 22,510–22,530 is detected as DBD Supply zone.
    4. Proximity ranking selects the nearest fresh overhead supply to Spot (22,420)
       for the HUD pill rather than far-away historical supply at 22,955.
    5. Broken demand zones include flipped resistance tags.
    """
    t0 = 1_729_656_000  # 23 Oct timestamp
    rows = []

    # 1. Historical baseline (around 22,950-23,000) creating an older Supply zone
    # Bar 0-9: Baseline around 22,980
    for i in range(10):
        rows.append({"ts": t0 + i * 900, "o": 22970.0, "h": 22985.0, "l": 22965.0, "c": 22980.0})
    # Bar 10: Boring base at 22,980 (tiny body 2 pts)
    rows.append({"ts": t0 + 10 * 900, "o": 22980.0, "h": 22990.0, "l": 22975.0, "c": 22982.0})
    # Bar 11: Drop to 22,850 (explosive move creating historical supply ~22,980)
    rows.append({"ts": t0 + 11 * 900, "o": 22980.0, "h": 22982.0, "l": 22840.0, "c": 22850.0})

    # Bar 12-14: Consolidation that creates an old Demand zone at 22,600-22,640
    rows.append({"ts": t0 + 12 * 900, "o": 22620.0, "h": 22640.0, "l": 22600.0, "c": 22625.0})  # Base
    rows.append({"ts": t0 + 13 * 900, "o": 22625.0, "h": 22730.0, "l": 22620.0, "c": 22720.0})  # Explosive rally up to 22,720

    # Bar 14: Rally into morning high (22,720 to 22,740)
    rows.append({"ts": t0 + 14 * 900, "o": 22720.0, "h": 22755.0, "l": 22715.0, "c": 22740.0})

    # Bar 15: Morning pivot base candle at 22,710–22,755
    # Open: 22,740, Close: 22,718 (body = 22 pts), High: 22,755, Low: 22,710 (range = 45 pts).
    # Body ratio = 22 / 45 = 0.488 (exceeds old rigid 0.45 threshold, but within relaxed 0.65 threshold!)
    rows.append({"ts": t0 + 15 * 900, "o": 22740.0, "h": 22755.0, "l": 22710.0, "c": 22718.0})

    # Bar 16: Massive crash leg-out candle (drops 168 pts from 22,718 to 22,550! >> 2.0x ATR)
    # This also smashes through the old Demand zone at 22,600-22,640, breaking it!
    rows.append({"ts": t0 + 16 * 900, "o": 22718.0, "h": 22720.0, "l": 22540.0, "c": 22550.0})

    # Bar 17: Mid-crash DBD pause at 22,520–22,550
    # Open: 22,550, Close: 22,532 (body = 18 pts), High: 22,555, Low: 22,520 (range = 35 pts).
    # Body ratio = 18 / 35 = 0.514 (qualifies under relaxed base relaxation)
    rows.append({"ts": t0 + 17 * 900, "o": 22550.0, "h": 22555.0, "l": 22520.0, "c": 22532.0})

    # Bar 18: Second explosive drop from 22,532 to 22,420 (Spot)
    rows.append({"ts": t0 + 18 * 900, "o": 22532.0, "h": 22535.0, "l": 22410.0, "c": 22420.0})

    bars = _make_bars(rows)
    engine = GtfSupplyDemandEngine()
    res = engine.detect_zones(bars, tf="15m", mode_override="conservative")

    assert res["available"] is True
    zones = res["zones"]
    assert len(zones) > 0

    # 1. Verify 22,710–22,755 RBD Supply zone was detected
    rbd_zones = [
        z for z in zones
        if z["type"] == "SUPPLY" and 22700.0 <= z["proximal"] <= 22755.0
    ]
    assert len(rbd_zones) >= 1, f"Expected 22,710-22,755 Supply zone, got {zones}"
    rbd = rbd_zones[0]
    assert rbd["pattern"] in ("RBD", "DBD")
    assert rbd["status"] == "FRESH"
    assert rbd["grade"] in ("A", "A+")

    # 2. Verify 22,520–22,555 DBD Supply zone was detected
    dbd_zones = [
        z for z in zones
        if z["type"] == "SUPPLY" and 22510.0 <= z["proximal"] <= 22560.0
    ]
    assert len(dbd_zones) >= 1, f"Expected 22,520-22,555 Supply zone, got {zones}"
    dbd = dbd_zones[0]
    assert dbd["status"] == "FRESH"

    # 3. Verify the old 22,600-22,640 Demand zone is marked BROKEN with Flipped to Resistance tag
    broken_demands = [z for z in zones if z["type"] == "DEMAND" and z["status"] == "BROKEN"]
    assert len(broken_demands) >= 1
    assert "Flipped to Resistance" in broken_demands[0]["label"]

    # 4. Verify nearest_supply for HUD pill:
    # Current Spot is 22,420. Nearest supply must be today's zone (22,532 or 22,718),
    # NOT the historical 22,980 supply!
    nearest_supply = res["nearest_supply"]
    assert nearest_supply is not None
    assert nearest_supply["proximal"] < 22800.0, f"Expected nearest supply < 22800, got {nearest_supply['proximal']}"
    assert nearest_supply["status"] == "FRESH"

