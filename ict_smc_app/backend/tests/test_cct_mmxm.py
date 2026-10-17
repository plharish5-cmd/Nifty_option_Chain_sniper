"""
pytest — behavioral unit tests for cct.py (P1–P9) and mmxm.py (M1–M7, X1).
Tests behavior, state machine transitions, zero hindsight, and zero repainting.
"""
from __future__ import annotations

import datetime
import os
import sys
import zoneinfo
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine.cct import compute_cct, get_clock_hour_open, get_day_open, is_in_killzone
from app.engine.fvg import FVG
from app.engine.mmxm import compute_mmxm
from app.engine.orderblocks import OrderBlock
from app.engine.primitives import Bars, Swing, atr
from app.engine.structure import StructEvent


def make_bars(ohlc_list: list[tuple[float, float, float, float]], start_ts: int = 1760400000, step_s: int = 900) -> Bars:
    rows = []
    for idx, (o, h, l, c) in enumerate(ohlc_list):
        rows.append({"ts": start_ts + idx * step_s, "o": o, "h": h, "l": l, "c": c})
    return Bars.from_rows(rows)


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 TESTS — cct.py (P1–P9)
# ══════════════════════════════════════════════════════════════════════════════

def test_p1_synthetic_4_candle_long_armed_then_triggered():
    """
    P1 & P9 (a): Synthetic 4-candle long that must be ARMED on bar 4,
    and becomes TRIGGERED only on a later bar that trades through the entry level.
    """
    # Base timestamp at 14:00 UTC (10:00 AM EDT) on 2025-10-14
    base_ts = 1760440800
    ohlc = [
        # Bar 0, 1: prior bars establishing day open = 1000.0
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        # Bar 2: FVG created between 930 and 950
        (975.0, 980.0, 970.0, 975.0),
        # Bar 3 (C1):
        (975.0, 978.0, 955.0, 960.0),
        # Bar 4 (C2): Sweep low into FVG (low = 940)
        (960.0, 965.0, 940.0, 950.0),
        # Bar 5 (C3): Confirming candle (low = 948 > 940, close = 965 > C2 open 960) -> ARMED at close of C3
        (950.0, 968.0, 948.0, 965.0),
        # Bar 6 (C4): Opens at 965.0. Rallies without trading below 965! (low = 965.0, high = 980.0)
        (965.0, 980.0, 965.0, 975.0),
    ]

    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    # Active FVG created at bar 2
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)
    rules = {
        "timezone": "America/New_York",
        "day_open_benchmark": "midnight_ny",
        "enforce_polarity": False,  # focus on state transition
        "sl_buffer_pts": 2.0,
        "min_risk_pts": 5.0,
        "max_risk_pts": 50.0,
        "rr_target": 2.0,
        "max_trigger_age_bars": 10,
    }

    # Evaluate up to Bar 6 (the 4th candle of the CCT sequence)
    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    assert res["available"] is True
    assert len(res["triggers"]) == 1
    trig = res["triggers"][0]
    # Bar 6 opened at 965.0 and stayed >= 965.0: MUST BE ARMED, NOT TRIGGERED!
    assert trig["status"] == "ARMED"
    assert trig["bar4_open"] == 965.0

    # Now add Bar 7: Dips to 960.0 (trades strictly through entry level 965.0 into discount!)
    ohlc_extended = ohlc + [(975.0, 978.0, 960.0, 970.0)]
    bars_ext = make_bars(ohlc_extended, start_ts=base_ts)
    atr_ext = atr(bars_ext)
    res_ext = compute_cct(bars_ext, atr_ext, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    assert len(res_ext["triggers"]) == 1
    trig_ext = res_ext["triggers"][0]
    # On Bar 7, price traded through 965.0 -> MUST BE TRIGGERED!
    assert trig_ext["status"] == "TRIGGERED"


def test_p2_never_fills_becomes_expired():
    """
    P2 & P9 (b): A setup that is ARMED but never fills within max_trigger_age_bars
    must become EXPIRED (not INVALID), and must NOT count in filled trade stats.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        # C1, C2, C3
        (975.0, 978.0, 955.0, 960.0),
        (960.0, 965.0, 940.0, 950.0),
        (950.0, 968.0, 948.0, 965.0),
        # C4 opens at 965
        (965.0, 980.0, 965.0, 975.0),
    ]
    # Add 12 bars that stay well above 965 (e.g. 980 - 1020) without ever touching 965 or SL 938
    for _ in range(12):
        ohlc.append((980.0, 1010.0, 975.0, 1000.0))

    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)
    rules = {
        "timezone": "America/New_York",
        "enforce_polarity": False,
        "max_trigger_age_bars": 5,  # expires after 5 bars
    }

    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    assert len(res["triggers"]) == 1
    trig = res["triggers"][0]
    assert trig["status"] == "EXPIRED"
    assert trig["outcome"] == "EXPIRED"
    # Stats must only count filled trades
    assert res["stats"]["filled_setups"] == 0
    assert res["stats"]["expired_count"] == 1


def test_p2_structure_broken_before_fill_becomes_invalidated():
    """
    P2: If price breaks structural stop before ever filling, status must be INVALIDATED.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        (975.0, 978.0, 955.0, 960.0),
        (960.0, 965.0, 940.0, 950.0),  # SL = 938.0
        (950.0, 968.0, 948.0, 965.0),  # C3 confirms
        # C4 opens at 965.0
        (965.0, 980.0, 965.0, 975.0),
        # C5 gaps down and trades below SL 938.0 directly
        (935.0, 936.0, 920.0, 925.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)
    rules = {"enforce_polarity": False}
    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    assert len(res["triggers"]) == 1
    trig = res["triggers"][0]
    assert trig["status"] == "INVALIDATED"
    assert res["stats"]["filled_setups"] == 0
    assert res["stats"]["invalidated_count"] == 1


def test_p3_last_unclosed_bar_never_produces_trigger():
    """
    P3 & P9 (c): The last bar when unclosed (is_closed=False) can never be used as C3.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        # C1 at bar 3:
        (975.0, 978.0, 955.0, 960.0),
        # C2 at bar 4:
        (960.0, 965.0, 940.0, 950.0),
        # C3 at bar 5 (LAST BAR IN DATASET):
        (950.0, 968.0, 948.0, 965.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)
    rules = {"enforce_polarity": False}

    # Case 1: is_closed = False (bar 5 is forming live candle)
    res_live = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules, is_closed=False)
    # Must NOT produce a trigger from the unclosed bar 5!
    assert len(res_live["triggers"]) == 0

    # Case 2: is_closed = True (bar 5 is fully closed)
    res_closed = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules, is_closed=True)
    assert len(res_closed["triggers"]) == 1


def test_p4_mitigated_fvg_must_not_qualify_as_poi():
    """
    P4 & P9 (d): An FVG mitigated/filled prior to the pivot candle must not qualify as POI.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        # Bar 2: FVG created between 930 and 950
        (975.0, 980.0, 970.0, 975.0),
        # Bar 3: Price already mitigated and filled the FVG!
        (970.0, 972.0, 925.0, 960.0),
        # Bar 4 (C1):
        (960.0, 965.0, 950.0, 955.0),
        # Bar 5 (C2): Low reaches 935 (in old FVG range), but FVG was already filled at bar 3!
        (955.0, 958.0, 935.0, 945.0),
        # Bar 6 (C3):
        (945.0, 965.0, 942.0, 960.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    # FVG mitigated at bar 3 (filled_i = 3)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0, filled_i=3)
    rules = {"enforce_polarity": False}
    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    # Since the FVG was mitigated prior to C2 (bar 5), it must NOT qualify as POI
    assert len(res["triggers"]) == 0


def test_p5_risk_clamping_skips_instead_of_tightening_stop():
    """
    P5: If structural stop exceeds max risk, it must SKIP the trade (not tighten stop).
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        (975.0, 978.0, 955.0, 960.0),
        # C2 low is 880 (risk = 965 - (880 - 2) = 87 points!)
        (960.0, 965.0, 880.0, 950.0),
        (950.0, 968.0, 948.0, 965.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=870.0, top=950.0)
    rules = {"enforce_polarity": False, "max_risk_pts": 40.0, "skip_oversized": True}
    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    # Oversized trade must be skipped
    assert len(res["triggers"]) == 0


def test_p6_h1_open_equals_clock_hour_bar_open():
    """
    P6 & P9 (e): H1 open must equal the clock-hour open (e.g. 14:00 bar open),
    not a rolling 4-bar window.
    """
    zi = zoneinfo.ZoneInfo("America/New_York")
    # Construct 15m bars across 14:00 to 14:45
    # 2025-10-14 14:00:00 EDT = 1760464800 UTC
    dt_1400 = datetime.datetime(2025, 10, 14, 14, 0, 0, tzinfo=zi)
    ts_1400 = int(dt_1400.timestamp())

    ohlc = [
        (22500.0, 22520.0, 22490.0, 22510.0),  # 14:00 (H1 open = 22500.0)
        (22510.0, 22530.0, 22505.0, 22525.0),  # 14:15
        (22525.0, 22550.0, 22520.0, 22540.0),  # 14:30
        (22540.0, 22560.0, 22535.0, 22555.0),  # 14:45
    ]
    bars = make_bars(ohlc, start_ts=ts_1400, step_s=900)
    h1_open = get_clock_hour_open(bars, zi, target_idx=3)
    # At 14:45, clock-hour open must strictly be 22500.0 (the 14:00 bar open)
    assert h1_open == 22500.0


def test_p6_dst_boundary_killzone_mapping():
    """
    P6 & P9 (f): A DST-boundary timestamp maps to the correct killzone.
    """
    zi = zoneinfo.ZoneInfo("America/New_York")
    killzones = [["09:30", "11:30"], ["13:30", "16:00"]]

    # 1. During Daylight Saving Time (EDT, UTC-4), e.g. Oct 15, 2025
    # 14:00 UTC = 10:00 AM EDT -> Inside Morning Killzone (09:30–11:30)
    dt_edt = datetime.datetime(2025, 10, 15, 10, 0, 0, tzinfo=zi)
    in_kz_edt, kz_name_edt = is_in_killzone(int(dt_edt.timestamp()), killzones, zi)
    assert in_kz_edt is True
    assert "09:30" in kz_name_edt

    # 2. During Standard Time (EST, UTC-5), e.g. Nov 15, 2025
    # 14:00 UTC = 09:00 AM EST -> Outside Morning Killzone (which starts at 09:30)
    dt_est = datetime.datetime(2025, 11, 15, 9, 0, 0, tzinfo=zi)
    in_kz_est, _ = is_in_killzone(int(dt_est.timestamp()), killzones, zi)
    assert in_kz_est is False


def test_p7_polarity_gates_entries():
    """
    P7: Polarity must gate entries: longs only below Day Open (discount),
    shorts only above Day Open (premium).
    """
    base_ts = 1760440800
    ohlc = [
        # Day open is 900.0 (below current prices)
        (900.0, 905.0, 895.0, 900.0),
        (950.0, 955.0, 945.0, 950.0),
        (970.0, 975.0, 965.0, 970.0),
        (975.0, 978.0, 955.0, 960.0),
        (960.0, 965.0, 940.0, 950.0),
        # C3 confirms at 965.0. Entry level = 965.0 > Day Open 900.0 (PREMIUM!)
        (950.0, 968.0, 948.0, 965.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)

    # Case 1: enforce_polarity = True -> Long in premium must be GATED
    rules_gate = {"enforce_polarity": True, "day_open_benchmark": "session_open"}
    res_gate = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 900.0}, tf="15m", rules=rules_gate)
    assert len(res_gate["triggers"]) == 0

    # Case 2: enforce_polarity = False -> Allowed
    rules_allow = {"enforce_polarity": False, "day_open_benchmark": "session_open"}
    res_allow = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 900.0}, tf="15m", rules=rules_allow)
    assert len(res_allow["triggers"]) == 1


def test_p8_confirmation_quality_oct16():
    """
    P8: On Oct 16 style weak confirmation where C3 does not close beyond C2 open,
    tag trigger as Grade B / weak.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        (975.0, 978.0, 955.0, 960.0),
        # C2 opens at 960.0, low = 940.0
        (960.0, 965.0, 940.0, 950.0),
        # C3 low = 945.0, close = 955.0 (<= C2 open 960.0! Weak confirmation)
        (950.0, 958.0, 945.0, 955.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)
    rules = {"enforce_polarity": False}

    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules)
    assert len(res["triggers"]) == 1
    trig = res["triggers"][0]
    assert trig["grade"] == "B"
    assert trig["confirmation_quality"] == "weak"


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 TESTS — mmxm.py (M1–M7)
# ══════════════════════════════════════════════════════════════════════════════

def test_m1_m2_m3_m4_frozen_dealing_range_and_bar_by_bar_phases():
    """
    M1, M2, M3, M4:
    - Dealing range frozen at MSS confirmation (Orig Cons extreme to SMR extreme).
    - Phases transition bar-by-bar: SMR_CONFIRMED -> LOW_RISK_BUY -> REACCUMULATION -> TARGET_REACHED.
    - Deterministic LRE first touch bar recorded.
    - Realized vs theoretical R.
    """
    base_ts = 1760440800
    ohlc = [
        # Bars 0-4: Original Consolidation at highs (1000.0 - 1020.0)
        (1010.0, 1020.0, 1005.0, 1015.0),
        (1015.0, 1020.0, 1008.0, 1012.0),
        (1012.0, 1020.0, 1005.0, 1018.0),
        (1018.0, 1020.0, 1006.0, 1010.0),
        (1010.0, 1020.0, 1005.0, 1012.0),
        # Bars 5-9: Multi-leg sell-side engineering curve
        (1005.0, 1008.0, 970.0, 975.0),   # LH1 / LL1
        (975.0, 985.0, 972.0, 980.0),
        (980.0, 982.0, 940.0, 945.0),     # LH2 / LL2
        (945.0, 955.0, 942.0, 950.0),
        (950.0, 952.0, 900.0, 905.0),     # SMR Swing Low at 900.0 (Bar 9)
        # Bars 10-12: Reversal leg & MSS break at 955.0 (Bar 12)
        (905.0, 930.0, 902.0, 925.0),
        (925.0, 950.0, 920.0, 945.0),
        (945.0, 965.0, 940.0, 962.0),     # MSS bar (Bar 12)
        # Bar 13: Retraces to 925.0 (inside LRE 20%-38% = 924-945.6) -> LOW_RISK_BUY!
        (962.0, 965.0, 925.0, 935.0),
        # Bar 14: Expands to 980.0 (inside Reaccumulation 50%-75% = 960-990) -> REACCUMULATION!
        (935.0, 980.0, 930.0, 975.0),
        # Bar 15: Delivers to Orig Cons Target (1020.0) -> TARGET_REACHED!
        (975.0, 1022.0, 970.0, 1020.0),
    ]

    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = np.full(len(bars), 10.0)

    # Swings establishing multi-leg engineering curve
    swings = [
        Swing(kind="H", i=1, price=1020.0, ts=int(bars.ts[1]), confirm_i=2),
        Swing(kind="H", i=6, price=985.0, ts=int(bars.ts[6]), confirm_i=7),   # LH 1
        Swing(kind="L", i=5, price=970.0, ts=int(bars.ts[5]), confirm_i=6),   # LL 1
        Swing(kind="H", i=8, price=955.0, ts=int(bars.ts[8]), confirm_i=9),   # LH 2
        Swing(kind="L", i=7, price=940.0, ts=int(bars.ts[7]), confirm_i=8),   # LL 2
        Swing(kind="L", i=9, price=900.0, ts=int(bars.ts[9]), confirm_i=10),  # SMR Pivot Low
    ]

    mss_ev = StructEvent(
        kind="CHOCH", dir="bull", i=12, ts=int(bars.ts[12]),
        level=955.0, swing_i=8, close=962.0, displacement=True,
        origin_i=9, origin_price=900.0,
    )
    # Displacement FVG near SMR
    fvg = FVG(dir="bull", i=11, ts=int(bars.ts[11]), bottom=920.0, top=940.0)

    res = compute_mmxm(bars, atr_arr, swings, [mss_ev], [fvg], [], tf="15m")
    assert res["available"] is True
    assert len(res["models"]) == 1
    m = res["models"][0]

    # M1: Dealing range is frozen between Orig Cons Top (1020.0) and SMR Low (900.0) = 120.0
    assert m["dealing_range"]["low"] == 900.0
    assert m["dealing_range"]["high"] == 1020.0
    assert m["dealing_range"]["range"] == 120.0

    # M2 & M3: Check chronological phase progression & LRE touch
    assert m["phase"] == "TARGET_REACHED"
    assert m["tested_lre"] is True
    assert m["lre_touch_bar"] == 13

    phase_sequence = [t["phase"] for t in m["transitions"]]
    assert phase_sequence == ["SMR_CONFIRMED", "LOW_RISK_BUY", "REACCUMULATION", "TARGET_REACHED"]

    # M4: Realized R because LRE was traded
    assert m["r_type"] == "realized"
    assert m["r_potential"] > 0


def test_m5_single_leg_drop_rejected():
    """
    M5: A single-leg fast drop (like Oct 14) without multi-leg engineering swings
    must NOT produce an MMBM.
    """
    base_ts = 1760440800
    ohlc = [
        # Orig cons
        (1010.0, 1020.0, 1005.0, 1015.0),
        (1015.0, 1020.0, 1008.0, 1012.0),
        (1012.0, 1020.0, 1005.0, 1018.0),
        (1018.0, 1020.0, 1006.0, 1010.0),
        (1010.0, 1020.0, 1005.0, 1012.0),
        # Single 2-bar fast drop straight to SMR low without structure!
        (1005.0, 1006.0, 940.0, 942.0),
        (942.0, 943.0, 900.0, 905.0),     # SMR low at bar 6
        # MSS reversal
        (905.0, 940.0, 902.0, 935.0),
        (935.0, 970.0, 930.0, 965.0),     # MSS bar 8
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = np.full(len(bars), 10.0)

    # NO intermediate lower highs or lower lows between Orig Cons and SMR low!
    swings = [
        Swing(kind="H", i=1, price=1020.0, ts=int(bars.ts[1]), confirm_i=2),
        Swing(kind="L", i=6, price=900.0, ts=int(bars.ts[6]), confirm_i=7),   # SMR Low
    ]
    mss_ev = StructEvent(
        kind="CHOCH", dir="bull", i=8, ts=int(bars.ts[8]),
        level=950.0, swing_i=1, close=965.0, displacement=True,
        origin_i=6, origin_price=900.0,
    )
    fvg = FVG(dir="bull", i=7, ts=int(bars.ts[7]), bottom=920.0, top=935.0)

    res = compute_mmxm(bars, atr_arr, swings, [mss_ev], [fvg], [], tf="15m")
    # Single-leg drop must be REJECTED!
    assert len(res["models"]) == 0


def test_m6_smr_timing_and_markers():
    """
    M6: At the SMR swing low, marker must be a hollow dot (MMBM_SMR_PIVOT),
    and solid badge (MMBM_SMR_CONFIRMED) must strictly be at MSS confirmation bar.
    """
    base_ts = 1760440800
    ohlc = [
        (1010.0, 1020.0, 1005.0, 1015.0),
        (1015.0, 1020.0, 1008.0, 1012.0),
        (1012.0, 1020.0, 1005.0, 1018.0),
        (1018.0, 1020.0, 1006.0, 1010.0),
        (1010.0, 1020.0, 1005.0, 1012.0),
        (1005.0, 1008.0, 970.0, 975.0),
        (975.0, 985.0, 972.0, 980.0),
        (980.0, 982.0, 940.0, 945.0),
        (945.0, 955.0, 942.0, 950.0),
        (950.0, 952.0, 900.0, 905.0),     # SMR low at bar 9
        (905.0, 930.0, 902.0, 925.0),
        (925.0, 950.0, 920.0, 945.0),
        (945.0, 965.0, 940.0, 962.0),     # MSS at bar 12
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = np.full(len(bars), 10.0)
    swings = [
        Swing(kind="H", i=1, price=1020.0, ts=int(bars.ts[1]), confirm_i=2),
        Swing(kind="H", i=6, price=985.0, ts=int(bars.ts[6]), confirm_i=7),
        Swing(kind="L", i=5, price=970.0, ts=int(bars.ts[5]), confirm_i=6),
        Swing(kind="H", i=8, price=955.0, ts=int(bars.ts[8]), confirm_i=9),
        Swing(kind="L", i=7, price=940.0, ts=int(bars.ts[7]), confirm_i=8),
        Swing(kind="L", i=9, price=900.0, ts=int(bars.ts[9]), confirm_i=10),   # SMR low
    ]
    mss_ev = StructEvent(
        kind="CHOCH", dir="bull", i=12, ts=int(bars.ts[12]),
        level=955.0, swing_i=8, close=962.0, displacement=True,
        origin_i=9, origin_price=900.0,
    )
    fvg = FVG(dir="bull", i=11, ts=int(bars.ts[11]), bottom=920.0, top=940.0)

    res = compute_mmxm(bars, atr_arr, swings, [mss_ev], [fvg], [], tf="15m")
    assert len(res["markers"]) == 2

    pivot_m = [m for m in res["markers"] if m["kind"] == "MMBM_SMR_PIVOT"][0]
    conf_m = [m for m in res["markers"] if m["kind"] == "MMBM_SMR_CONFIRMED"][0]

    # Pivot marker timestamp must match SMR bar 9
    assert pivot_m["time"] == int(bars.ts[9]) + 19800  # IST offset in chart_time
    assert pivot_m["shape"] == "circle"

    # Confirmed marker timestamp must match MSS bar 12
    assert conf_m["time"] == int(bars.ts[12]) + 19800
    assert conf_m["shape"] == "arrowUp"
    assert "confirmed at" in conf_m["tooltip"].lower()


def test_m7_confluence_gating():
    """
    M7: A model without minimum confluence score (e.g. MSS alone) must be rejected.
    """
    base_ts = 1760440800
    ohlc = [
        (1010.0, 1020.0, 1005.0, 1015.0),
        (1015.0, 1020.0, 1008.0, 1012.0),
        (1012.0, 1020.0, 1005.0, 1018.0),
        (1018.0, 1020.0, 1006.0, 1010.0),
        (1010.0, 1020.0, 1005.0, 1012.0),
        (1005.0, 1008.0, 970.0, 975.0),
        (975.0, 985.0, 972.0, 980.0),
        (980.0, 982.0, 940.0, 945.0),
        (945.0, 955.0, 942.0, 950.0),
        (950.0, 952.0, 900.0, 905.0),
        (905.0, 930.0, 902.0, 925.0),
        (925.0, 950.0, 920.0, 945.0),
        (945.0, 965.0, 940.0, 962.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = np.full(len(bars), 10.0)
    swings = [
        Swing(kind="H", i=1, price=1020.0, ts=int(bars.ts[1]), confirm_i=2),
        Swing(kind="H", i=6, price=985.0, ts=int(bars.ts[6]), confirm_i=7),
        Swing(kind="L", i=5, price=970.0, ts=int(bars.ts[5]), confirm_i=6),
        Swing(kind="H", i=8, price=955.0, ts=int(bars.ts[8]), confirm_i=9),
        Swing(kind="L", i=7, price=940.0, ts=int(bars.ts[7]), confirm_i=8),
        Swing(kind="L", i=9, price=900.0, ts=int(bars.ts[9]), confirm_i=10),
    ]
    # MSS without displacement, no FVG, no SMT, no POI -> Score = 1 (MSS Shift only)
    mss_no_disp = StructEvent(
        kind="CHOCH", dir="bull", i=12, ts=int(bars.ts[12]),
        level=955.0, swing_i=8, close=962.0, displacement=False,
        origin_i=9, origin_price=900.0,
    )
    rules = {"min_smr_confluences": 2, "allow_mss_only": False}
    res = compute_mmxm(bars, atr_arr, swings, [mss_no_disp], [], [], tf="15m", rules=rules)
    assert len(res["models"]) == 0


# ══════════════════════════════════════════════════════════════════════════════
# PART 3 TESTS — CCT × MMXM Context Interaction (X1)
# ══════════════════════════════════════════════════════════════════════════════

def test_x1_cct_mmxm_context_interaction():
    """
    X1: On Oct 16 style scenario: when MMBM has reached Target Delivery (Phase 5),
    new CCT longs in premium must be tagged context: 'late'.
    """
    base_ts = 1760440800
    ohlc = [
        (1000.0, 1005.0, 995.0, 1000.0),
        (1000.0, 1002.0, 970.0, 975.0),
        (975.0, 980.0, 970.0, 975.0),
        (975.0, 978.0, 955.0, 960.0),
        (960.0, 965.0, 940.0, 950.0),
        (950.0, 968.0, 948.0, 965.0),
    ]
    bars = make_bars(ohlc, start_ts=base_ts)
    atr_arr = atr(bars)
    fvg = FVG(dir="bull", i=2, ts=int(bars.ts[2]), bottom=930.0, top=950.0)

    # Active MMBM model that has already reached Target Delivery
    mmxm_data = {
        "primary": {
            "model": "MMBM",
            "dir": "bull",
            "phase": "TARGET_REACHED",
            "target": 960.0,
            "dealing_range": {"low": 900.0, "high": 960.0, "range": 60.0},
        }
    }
    rules = {"enforce_polarity": False}
    res = compute_cct(bars, atr_arr, [], [fvg], [], sess={"open": 1000.0}, tf="15m", rules=rules, mmxm_data=mmxm_data)
    assert len(res["triggers"]) == 1
    trig = res["triggers"][0]
    # In premium after target delivery -> MUST BE TAGGED 'late'
    assert trig["context"] == "late"
    assert trig["is_counter"] is True
