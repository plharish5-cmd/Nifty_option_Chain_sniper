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
