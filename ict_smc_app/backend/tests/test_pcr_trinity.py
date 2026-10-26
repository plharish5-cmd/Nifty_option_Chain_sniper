"""
tests/test_pcr_trinity.py — Unit tests for 3-Chart Institutional PCR & Open Interest Positioning Suite.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from app.engine import pcr_trinity
from app.timeutil import chart_time


def test_pcr_band_classification():
    cfg = {
        "extreme_bull_pcr": 1.50,
        "bull_pcr": 1.20,
        "bear_pcr": 0.80,
        "extreme_bear_pcr": 0.50,
        "neutral_core_high": 1.10,
        "neutral_core_low": 0.85,
    }

    # > 1.50 Extreme Bullish Contrarian Risk
    k, lbl = pcr_trinity.classify_pcr_band(1.65, cfg)
    assert k == "EXTREME_BULL_CONTRARIAN_RISK"
    assert "Contrarian Pullback Risk" in lbl

    # 1.20 - 1.50 Bullish Put Control
    k, lbl = pcr_trinity.classify_pcr_band(1.35, cfg)
    assert k == "BULLISH_PUT_CONTROL"
    assert "Defending Floor" in lbl

    # 0.85 - 1.10 Neutral Balanced Core
    k, lbl = pcr_trinity.classify_pcr_band(0.95, cfg)
    assert k == "NEUTRAL_BALANCED"
    assert "Core Equilibrium" in lbl

    # 0.80 - 0.85 Neutral Balanced Outer
    k, lbl = pcr_trinity.classify_pcr_band(0.82, cfg)
    assert k == "NEUTRAL_BALANCED"

    # 0.50 - 0.80 Bearish Call Control
    k, lbl = pcr_trinity.classify_pcr_band(0.68, cfg)
    assert k == "BEARISH_CALL_CONTROL"
    assert "Capping Ceiling" in lbl

    # < 0.50 Extreme Bearish Contrarian Bounce
    k, lbl = pcr_trinity.classify_pcr_band(0.42, cfg)
    assert k == "EXTREME_BEAR_CONTRARIAN_BOUNCE"
    assert "Contrarian Bounce Risk" in lbl


def test_pcr_oi_series_generation():
    raw_snaps = [
        (1792640700, 20000000, 20000000, 1000000, 1000000),
        (1792641000, 20500000, 21500000, 1500000, 1800000),
        (1792641300, 21000000, 23000000, 2000000, 2600000),
    ]
    fut_candles = [
        {"ts": 1792640700, "c": 22500.0},
        {"ts": 1792641000, "c": 22530.0},
        {"ts": 1792641300, "c": 22560.0},
    ]

    series = pcr_trinity.build_intraday_pcr_series(raw_snaps, fut_candles)
    assert len(series) >= 3

    for pt in series:
        assert "time" in pt
        assert "fut_close" in pt
        assert "pcr_oi" in pt
        assert "pcr_vol" in pt
        assert "call_oi" in pt
        assert "put_oi" in pt
        assert "total_oi_gap" in pt
        assert "call_oi_chg" in pt
        assert "put_oi_chg" in pt
        assert "net_oi_chg" in pt

        # Mathematical relationships
        assert pt["total_oi_gap"] == pt["put_oi"] - pt["call_oi"]
        assert pt["net_oi_chg"] == pt["put_oi_chg"] - pt["call_oi_chg"]
        expected_pcr = round(pt["put_oi"] / max(pt["call_oi"], 1), 3)
        assert abs(pt["pcr_oi"] - expected_pcr) <= 0.002


def test_bullish_trinity_lock():
    # 3/3 Bullish Trinity Lock scenario:
    # 1. PCR rising
    # 2. Fresh Put writing dominates (+net ΔOI and rising)
    # 3. Total OI gap widening upward toward bulls
    t0 = 1792640700
    series = []
    for i in range(35):
        t = t0 + i * 60
        fut = 22500.0 + i * 2.0
        c_oi = 20000000 + i * 10000
        p_oi = 20000000 + i * 50000
        c_chg = i * 10000
        p_chg = i * 50000
        series.append({
            "time": chart_time(t),
            "fut_close": fut,
            "pcr_oi": round(p_oi / c_oi, 3),
            "pcr_vol": 1.2,
            "call_oi": c_oi,
            "put_oi": p_oi,
            "total_oi_gap": p_oi - c_oi,
            "call_oi_chg": c_chg,
            "put_oi_chg": p_chg,
            "net_oi_chg": p_chg - c_chg,
        })

    verdict = pcr_trinity.compute_pcr_trinity_verdict(series)
    assert verdict["alignment_state"] == "BULLISH_TRINITY_LOCK"
    assert "🔥 3/3 BULLISH TRINITY CONFIRMED" in verdict["verdict_badge"]
    assert verdict["net_oi_slope_30m"] > 0
    assert verdict["total_oi_gap_delta"] > 0
    assert verdict["pcr_slope_30m"] > 0


def test_bearish_trinity_lock():
    # 3/3 Bearish Trinity Lock scenario:
    # 1. PCR falling
    # 2. Fresh Call writing dominates (-net ΔOI and call Δ accelerating)
    # 3. Total OI gap narrowing / expanding negatively in favor of bears
    t0 = 1792640700
    series = []
    for i in range(35):
        t = t0 + i * 60
        fut = 22600.0 - i * 2.0
        c_oi = 20000000 + i * 60000
        p_oi = 18000000 + i * 5000
        c_chg = i * 60000
        p_chg = i * 5000
        series.append({
            "time": chart_time(t),
            "fut_close": fut,
            "pcr_oi": round(p_oi / c_oi, 3),
            "pcr_vol": 0.7,
            "call_oi": c_oi,
            "put_oi": p_oi,
            "total_oi_gap": p_oi - c_oi,
            "call_oi_chg": c_chg,
            "put_oi_chg": p_chg,
            "net_oi_chg": p_chg - c_chg,
        })

    verdict = pcr_trinity.compute_pcr_trinity_verdict(series)
    assert verdict["alignment_state"] == "BEARISH_TRINITY_LOCK"
    assert "🩸 3/3 BEARISH TRINITY CONFIRMED" in verdict["verdict_badge"]
    assert verdict["net_oi_slope_30m"] < 0
    assert verdict["total_oi_gap_delta"] < 0
    assert verdict["pcr_slope_30m"] < 0


def test_price_pcr_divergence():
    # Bearish Trap: Price climbing higher while PCR is tumbling and Call writers are aggressive
    t0 = 1792640700
    series = []
    for i in range(35):
        t = t0 + i * 60
        fut = 22500.0 + i * 2.0  # Futures up +70 pts!
        c_oi = 20000000 + i * 70000  # Aggressive Call writing
        p_oi = 20000000 - i * 5000
        c_chg = i * 70000
        p_chg = -i * 5000
        series.append({
            "time": chart_time(t),
            "fut_close": fut,
            "pcr_oi": round(p_oi / c_oi, 3),
            "pcr_vol": 0.8,
            "call_oi": c_oi,
            "put_oi": p_oi,
            "total_oi_gap": p_oi - c_oi,
            "call_oi_chg": c_chg,
            "put_oi_chg": p_chg,
            "net_oi_chg": p_chg - c_chg,
        })

    verdict = pcr_trinity.compute_pcr_trinity_verdict(series)
    assert verdict["alignment_state"] == "PRICE_PCR_DIVERGENCE"
    assert "BEARISH TRAP" in verdict["verdict_badge"]


def test_historical_backfill_session_bootstrap():
    # When fewer than 10 snapshots exist (e.g. server restarted mid-session),
    # build_intraday_pcr_series must synthesize a full-day curve across all FUT candles from 09:15 AM
    t0 = 1792640700
    fut_candles = [{"ts": t0 + i * 60, "c": 22500.0 + i * 0.5} for i in range(60)]
    sparse_snaps = [
        (t0 + 3000, 24000000, 18000000, 5000000, 4500000)
    ]
    base_totals = {
        "ce_oi": 24000000, "pe_oi": 18000000,
        "ce_oi_chg": 1000000, "pe_oi_chg": 500000,
        "ce_vol": 5000000, "pe_vol": 4500000,
    }

    series = pcr_trinity.build_intraday_pcr_series(sparse_snaps, fut_candles, base_totals)
    assert len(series) == len(fut_candles)
    assert series[0]["time"] == chart_time(t0)
    assert series[-1]["time"] == chart_time(fut_candles[-1]["ts"])
    # Verifies smooth progression
    assert series[0]["call_oi"] > 0
    assert series[-1]["call_oi"] > 0


def test_multi_expiry_combination():
    t0 = chart_time(1792640700)
    cur = [{
        "time": t0, "fut_close": 22500.0, "pcr_oi": 1.0, "pcr_vol": 1.0,
        "call_oi": 100000, "put_oi": 120000, "total_oi_gap": 20000,
        "call_oi_chg": 5000, "put_oi_chg": 8000, "net_oi_chg": 3000,
    }]
    next_w = [{
        "time": t0, "fut_close": 22500.0, "pcr_oi": 0.8, "pcr_vol": 0.8,
        "call_oi": 50000, "put_oi": 40000, "total_oi_gap": -10000,
        "call_oi_chg": 2000, "put_oi_chg": 1000, "net_oi_chg": -1000,
    }]

    both = pcr_trinity.combine_multi_expiry_series(cur, next_w)
    assert len(both) == 1
    pt = both[0]
    assert pt["call_oi"] == 150000
    assert pt["put_oi"] == 160000
    assert pt["total_oi_gap"] == 10000
    assert pt["call_oi_chg"] == 7000
    assert pt["put_oi_chg"] == 9000
    assert pt["net_oi_chg"] == 2000
    assert pt["pcr_oi"] == round(160000 / 150000, 3)


def test_checkpoint_future_pending_status():
    """
    Verify that compute_checkpoints returns PENDING and None for 14:30 Final Hour
    when the session data only extends up to 12:30 IST.
    """
    from app.timeutil import now_ist, session_bounds
    base_date = now_ist().date()
    sess_open, _ = session_bounds(base_date)

    # Build series from 09:15 (t=0) to 12:30 (t=195 mins) in 3m steps
    series = []
    for m in range(0, 196, 3):
        t_epoch = sess_open + m * 60
        series.append({
            "time": chart_time(t_epoch),
            "fut_close": 22500.0 + m * 0.5,
            "pcr_oi": round(0.85 - m * 0.001, 3),
            "pcr_vol": 0.80,
            "call_oi": 20000000 + m * 5000,
            "put_oi": 17000000 - m * 5000,
            "total_oi_gap": -3000000,
            "call_oi_chg": m * 5000,
            "put_oi_chg": -m * 5000,
            "net_oi_chg": -m * 10000,
        })

    checkpoints = pcr_trinity.compute_checkpoints(series)
    cp_by_key = {cp["key"]: cp for cp in checkpoints}

    assert "09:45" in cp_by_key
    assert "12:30" in cp_by_key
    assert "14:30" in cp_by_key
    assert "Current" in cp_by_key

    # 09:45 and 12:30 must be COMPLETED
    assert cp_by_key["09:45"]["status"] == "COMPLETED"
    assert cp_by_key["09:45"]["available"] is True
    assert cp_by_key["09:45"]["pcr_oi"] is not None

    assert cp_by_key["12:30"]["status"] == "COMPLETED"
    assert cp_by_key["12:30"]["available"] is True
    assert cp_by_key["12:30"]["pcr_oi"] is not None

    # 14:30 Final Hour must be PENDING since session is only at 12:30 IST
    cp_1430 = cp_by_key["14:30"]
    assert cp_1430["status"] == "PENDING"
    assert cp_1430["available"] is False
    assert cp_1430["pcr_oi"] is None
    assert cp_1430["delta_from_open"] is None
    assert "Pending (—)" in cp_1430["shift_text"]


def test_pcr_trinity_seamless_stitching_and_outlier_guard():
    """
    Verifies that:
      1. Synthesized morning bars anchor seamlessly to the first real snapshot with no cliff jump > 15%.
      2. Transient single-snapshot glitches (e.g. 50% drop from partial fetch) are rejected and smoothed.
      3. No single-step step jump > 20% exists across the entire stitched series.
    """
    t0 = 1792640700  # 09:15 AM
    fut_candles = [{"ts": t0 + i * 180, "c": 22500.0 - i * 2.0} for i in range(50)]

    # Real snapshots start only at bar 15 (t0 + 2700s = 10:00 AM)
    snaps = []
    for i in range(15, 50):
        t = t0 + i * 180
        ce = 25000000 + (i - 15) * 100000
        pe = 22000000 - (i - 15) * 80000
        # Inject transient outlier glitch at bar 30 (partial chain fetch)
        if i == 30:
            ce = ce * 0.45  # 55% cliff drop
        snaps.append((t, ce, pe, 5000000, 4000000))

    base_totals = {
        "ce_oi": 28500000,
        "pe_oi": 19200000,
        "ce_oi_chg": 5500000,
        "pe_oi_chg": -2800000,
    }

    series = pcr_trinity.build_intraday_pcr_series(snaps, fut_candles, base_totals)
    assert len(series) == len(fut_candles)

    # Check that stitched transition at bar 15 has no cliff drop
    stitch_bar = 15
    ce_before = series[stitch_bar - 1]["call_oi"]
    ce_stitch = series[stitch_bar]["call_oi"]
    jump_pct = abs(ce_stitch - ce_before) / ce_before
    assert jump_pct < 0.10, f"Stitch cliff jump too large: {jump_pct * 100:.2f}%"

    # Check that glitched bar 30 was smoothed and rejected
    glitch_bar = 30
    ce_glitch = series[glitch_bar]["call_oi"]
    ce_prev = series[glitch_bar - 1]["call_oi"]
    glitch_jump = abs(ce_glitch - ce_prev) / ce_prev
    assert glitch_jump < 0.15, f"Glitch was not smoothed: jump is {glitch_jump * 100:.2f}%"

    # Verify no single-step jump > 20% anywhere in the series
    for i in range(1, len(series)):
        prev_ce = series[i - 1]["call_oi"]
        curr_ce = series[i]["call_oi"]
        assert abs(curr_ce - prev_ce) / prev_ce < 0.20, f"Step jump at bar {i} exceeds 20%"


def test_stitched_backfill_live_no_jump_and_positive_scaled():
    """
    Verifies that:
      1. Stitched backfill + live series has no single-step jump > 30% anywhere across
         the transition (e.g. 13:45) or near-close partial snapshot drop (e.g. 15:21).
      2. call_oi and put_oi values are positive and properly scaled (> 1e6 contracts).
    """
    t0 = 1792640700  # 09:15 AM
    # 75 bars of 3-min futures candles covering 09:15 to 13:00 to 15:30
    fut_candles = [{"ts": t0 + i * 180, "c": 22650.0 - i * 3.0} for i in range(75)]

    # Real recording started only at bar 45 (13:45 IST)
    # At 13:45, call_oi is 24.7Cr (+6.2Cr from open of ~18.5Cr)
    snaps = []
    for i in range(45, 75):
        t = t0 + i * 180
        c_oi = 24700000 + (i - 45) * 150000
        p_oi = 21000000 - (i - 45) * 100000
        # Inject realistic 15:21 partial API glitch at bar 72: Put OI drops suddenly from 18.3Cr to 7.3Cr (60% drop)
        if i == 72:
            p_oi = p_oi * 0.40
        snaps.append((t, c_oi, p_oi, 8000000, 7000000))

    base_totals = {
        "ce_oi": 29330000,
        "pe_oi": 23650000,
        "ce_oi_chg": 10800000,
        "pe_oi_chg": 4700000,
    }

    series = pcr_trinity.build_intraday_pcr_series(snaps, fut_candles, base_totals)
    assert len(series) == len(fut_candles)

    for i in range(len(series)):
        pt = series[i]
        # Positive and properly scaled
        assert pt["call_oi"] > 1000000, f"Bar {i} call_oi not properly scaled: {pt['call_oi']}"
        assert pt["put_oi"] > 1000000, f"Bar {i} put_oi not properly scaled: {pt['put_oi']}"

        # Mathematical coherence
        assert pt["total_oi_gap"] == pt["put_oi"] - pt["call_oi"]
        assert pt["net_oi_chg"] == pt["put_oi_chg"] - pt["call_oi_chg"]

        # No single-step jump > 30%
        if i > 0:
            prev = series[i - 1]
            c_jump = abs(pt["call_oi"] - prev["call_oi"]) / prev["call_oi"]
            p_jump = abs(pt["put_oi"] - prev["put_oi"]) / prev["put_oi"]
            assert c_jump < 0.30, f"Step jump at bar {i} ({pt['time']}) for call_oi was {c_jump * 100:.1f}% > 30%"
            assert p_jump < 0.30, f"Step jump at bar {i} ({pt['time']}) for put_oi was {p_jump * 100:.1f}% > 30%"


def test_resolve_futures_series_contract_modes():
    t0 = 1792640700
    oct_candles = [
        {"time": t0, "close": 22748.3, "open": 22740.0, "high": 22755.0, "low": 22735.0, "volume": 50000, "oi": 14000000, "oi_chg": 500000},
        {"time": t0 + 180, "close": 22760.0, "open": 22748.3, "high": 22765.0, "low": 22745.0, "volume": 60000, "oi": 14100000, "oi_chg": 600000},
    ]
    nov_candles = [
        {"time": t0, "close": 22872.0, "open": 22865.0, "high": 22880.0, "low": 22860.0, "volume": 20000, "oi": 3000000, "oi_chg": 200000},
        {"time": t0 + 180, "close": 22885.0, "open": 22872.0, "high": 22890.0, "low": 22870.0, "volume": 25000, "oi": 3100000, "oi_chg": 300000},
    ]

    # 1. Near Contract Mode (Oct FUT)
    near_res = pcr_trinity.resolve_futures_series(oct_candles, nov_candles, fut_mode="near")
    assert t0 in near_res
    assert near_res[t0]["close"] == 22748.3
    assert near_res[t0]["volume"] == 50000
    assert near_res[t0]["oi"] == 14000000
    assert near_res[t0]["oi_chg"] == 500000
    assert "Near" in near_res[t0]["contract_label"]

    # 2. Next Contract Mode (Nov FUT)
    next_res = pcr_trinity.resolve_futures_series(oct_candles, nov_candles, fut_mode="next")
    assert t0 in next_res
    assert next_res[t0]["close"] == 22872.0
    assert next_res[t0]["volume"] == 20000
    assert next_res[t0]["oi"] == 3000000
    assert next_res[t0]["oi_chg"] == 200000
    assert "Next" in next_res[t0]["contract_label"]

    # 3. Combined Contract Mode (Oct + Nov FUT)
    comb_res = pcr_trinity.resolve_futures_series(oct_candles, nov_candles, fut_mode="combined")
    assert t0 in comb_res
    assert comb_res[t0]["volume"] == 70000
    assert comb_res[t0]["oi"] == 17000000
    assert comb_res[t0]["oi_chg"] == 700000
    # In combined mode, Near-Month price (Oct) is preserved to prevent carry distortion
    assert comb_res[t0]["close"] == 22748.3
    assert comb_res[t0]["fut_basis_spread"] == 123.7
    assert comb_res[t0]["oct_close"] == 22748.3
    assert comb_res[t0]["nov_close"] == 22872.0
    assert comb_res[t0]["rollover_pct"] == round((20000 / 70000) * 100, 1)
    assert "Combined" in comb_res[t0]["contract_label"]


def test_compute_pcr_trinity_verdict_short_covering_squeeze():
    """
    Verifies that when Call writers aggressively unwind/cover (CE Δ < 0)
    while Put writers hold ground (PE Δ > 0, net Δ > 0), the engine identifies
    a 3/3 BULLISH SHORT-COVERING SQUEEZE (BULLISH_TRINITY_LOCK) rather than mixed/wait.
    """
    t0 = 1792640700
    series = [
        {
            "time": t0,
            "pcr_oi": 1.00,
            "call_oi": 30000000,
            "put_oi": 30000000,
            "call_oi_chg": 4000000,
            "put_oi_chg": 10000000,
            "net_oi_chg": 6000000,
            "total_oi_gap": 0,
            "fut_close": 22600.0,
            "fut_oi": 15000000,
            "fut_vol": 50000,
        },
        {
            "time": t0 + 1800,  # +30m
            "pcr_oi": 1.12,     # PCR rising
            "call_oi": 28560000,
            "put_oi": 31830000,
            "call_oi_chg": -1350000,  # Negative CE Δ (Call writers covered!)
            "put_oi_chg": 9500000,    # Strong Put support
            "net_oi_chg": 10850000,   # Massive positive spread (+1.08Cr)
            "total_oi_gap": 3270000,  # PE Total > CE Total
            "fut_close": 22750.0,
            "fut_oi": 15500000,
            "fut_vol": 70000,
        },
    ]
    res = pcr_trinity.compute_pcr_trinity_verdict(series)
    assert res["alignment_state"] == "BULLISH_TRINITY_LOCK"
    assert "SHORT-COVERING SQUEEZE" in res["verdict_badge"]
    assert res["is_call_short_squeeze"] is True


def test_build_intraday_pcr_series_with_futures_oi_chg():
    t0 = 1792640700
    fut_candles = [
        {"ts": t0, "c": 22748.3, "v": 50000, "oi": 14000000, "oi_chg": 500000},
        {"ts": t0 + 180, "c": 22760.0, "v": 60000, "oi": 14100000, "oi_chg": 600000},
        {"ts": t0 + 360, "c": 22765.0, "v": 55000, "oi": 14200000, "oi_chg": 700000},
    ]
    raw_snaps = [
        (t0, 31000000, 33000000, 1000000, 1500000),
        (t0 + 180, 31200000, 33500000, 1200000, 1800000),
        (t0 + 360, 31620000, 34090000, 1710000, 11760000),
    ]

    series = pcr_trinity.build_intraday_pcr_series(raw_snaps, fut_candles)
    assert len(series) == 3

    for pt in series:
        assert "fut_close" in pt
        assert "fut_oi" in pt
        assert "fut_oi_chg" in pt
        assert "fut_vol" in pt

    assert series[0]["fut_close"] == 22748.3
    assert series[0]["fut_oi"] == 14000000
    assert series[0]["fut_oi_chg"] == 500000
    assert series[0]["fut_vol"] == 50000

    verdict = pcr_trinity.compute_pcr_trinity_verdict(series)
    assert verdict["fut_close"] == 22765.0
    assert verdict["fut_oi"] == 14200000
    assert verdict["fut_oi_chg"] == 700000
    assert verdict["fut_vol"] == 55000


def test_dual_expiry_and_futures_meta_integration():
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-10-27", "2026-11-03"]
    poller.chains = {
        "2026-10-27": {
            "totals": {"ce_oi": 31620000, "pe_oi": 34090000, "ce_oi_chg": 1710000, "pe_oi_chg": 11760000},
            "rows": [],
        },
        "2026-11-03": {
            "totals": {"ce_oi": 15000000, "pe_oi": 16000000, "ce_oi_chg": 500000, "pe_oi_chg": 800000},
            "rows": [],
        },
    }

    class MockHub:
        fut = {"symbol": "NIFTY-Oct2026-FUT"}
        fut2 = {"symbol": "NIFTY-Nov2026-FUT"}

    res = poller.get_pcr_trinity(exp="both", fut_src="combined", hub=MockHub())
    assert "expiries_meta" in res
    meta = res["expiries_meta"]
    assert meta["cur_date"] == "27 Oct"
    assert meta["next_date"] == "03 Nov"
    assert meta["near_fut"] == "NIFTY-Oct2026-FUT"
    assert meta["next_fut"] == "NIFTY-Nov2026-FUT"
    assert "rollover_pct" in meta
    assert res["selected_expiry"] == "both"
    assert "both" in res["series"]


def test_get_writer_flow_integration():
    from app.market.chain import ChainPoller
    from app.dhan.rest import DhanREST

    rest = DhanREST()
    poller = ChainPoller(rest, lambda *a, **k: None)
    poller.expiries = ["2026-10-27", "2026-11-03"]
    poller.chains = {
        "2026-10-27": {
            "totals": {"ce_oi": 31620000, "pe_oi": 34090000, "ce_oi_chg": 1710000, "pe_oi_chg": 11760000},
            "rows": [],
        },
        "2026-11-03": {
            "totals": {"ce_oi": 15000000, "pe_oi": 16000000, "ce_oi_chg": 500000, "pe_oi_chg": 800000},
            "rows": [],
        },
    }

    class MockHub:
        fut = {"symbol": "NIFTY-Oct2026-FUT"}
        fut2 = {"symbol": "NIFTY-Nov2026-FUT"}

    # Test Next Wk (03 Nov) writer flow
    flow_next = poller.get_writer_flow(exp="next", fut_src="combined", tf="1m", hub=MockHub())
    assert flow_next["ok"] is True
    assert "03 Nov" in flow_next["expiry"]
    assert len(flow_next["points"]) > 0
    # Verify first point structure
    p0 = flow_next["points"][0]
    assert "t" in p0 and "hm" in p0 and "ce" in p0 and "pe" in p0 and "flow" in p0 and "bal" in p0
    assert p0["t"] > 0
    # Verify timestamps are strictly increasing
    times = [p["t"] for p in flow_next["points"]]
    assert times == sorted(times)
    assert len(times) == len(set(times))  # No duplicates

    # Test Both expiries writer flow
    flow_both = poller.get_writer_flow(exp="both", fut_src="combined", tf="3m", hub=MockHub())
    assert flow_both["ok"] is True
    assert "Both" in flow_both["expiry"]
    assert len(flow_both["points"]) > 0



