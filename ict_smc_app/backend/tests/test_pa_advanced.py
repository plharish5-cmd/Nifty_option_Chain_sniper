"""
tests/test_pa_advanced.py — Unit tests for the 10 Advanced Price Action & Institutional Concepts.
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

from app.engine.fvg import FVG
from app.engine.orderblocks import OrderBlock
from app.engine.pa_advanced import (
    classify_sfp_tests,
    compute_gpx,
    compute_pa_advanced,
    detect_dragon_fruit,
    detect_ftr_zones,
    detect_mitigation_blocks,
    detect_msb_quality,
    detect_rote,
    detect_sd_bases,
    detect_traps_inducements,
    detect_tt3,
    detect_bprs,
    _synthesize_sniper_view,
)
from app.engine.primitives import Bars, Swing
from app.engine.liquidity import Pool
from oct8_data import five_min, fifteen_min


def _make_bars(closes, highs=None, lows=None, opens=None, wick=1.0, step_s=300) -> Bars:
    rows = []
    prev = closes[0]
    for k, c in enumerate(closes):
        o = opens[k] if opens is not None else prev
        h = highs[k] if highs is not None else max(o, c) + wick
        l = lows[k] if lows is not None else min(o, c) - wick
        rows.append({"ts": 1_700_000_000 + step_s * k, "o": o, "h": h, "l": l, "c": c})
        prev = c
    return Bars.from_rows(rows)


# ── 1. Strong vs. Weak MSB & 7-Point Scorer ───────────────────────────────────

def test_strong_vs_weak_msb_and_7point_scorer():
    # Downtrend: LH at 22200 -> LL at 22100 -> Strong impulsive launch past 22200
    closes = [22250, 22200, 22180, 22150, 22120, 22100, 22140, 22180, 22230, 22220]
    b = _make_bars(closes, wick=2.0)
    a = np.full(len(b), 15.0)

    # Mock swings: LH at bar 1 (22200), LL at bar 5 (22100)
    swings = [
        Swing(kind="H", i=1, price=22200.0, ts=int(b.ts[1]), confirm_i=2),
        Swing(kind="L", i=5, price=22100.0, ts=int(b.ts[5]), confirm_i=6),
    ]
    obs = [OrderBlock(dir="bull", i=5, ts=int(b.ts[5]), bottom=22100.0, top=22120.0, created_i=6, kind="OB")]
    fvgs = [FVG(dir="bull", i=8, ts=int(b.ts[8]), bottom=22190.0, top=22215.0)]

    cfg = {
        "displacement": {"disp_body_atr_mult": 1.05, "disp_body_ratio_min": 0.60},
        "msb_break_quality": {
            "decisive_close_atr": 0.20,
            "consolidation_max_bars": 2,
            "pullback_slowness_ratio": 1.40,
            "engulfing_launch_mult": 1.25,
        },
        "liquidity_magnets": {"nifty_round_minor": 100},
    }

    msbs = detect_msb_quality(b, a, swings, obs, fvgs, [22100.0, 22200.0], cfg)
    assert len(msbs) >= 1
    m = msbs[0]
    assert m.dir == "bull"
    assert m.origin_swing_price == 22200.0
    assert m.extreme_swing_price == 22100.0
    assert m.score >= 4
    assert m.scorecard["strong_body_close"] is True
    assert m.scorecard["fvg_created"] is True


# ── 2. Dragon Fruit Confluence Detector ───────────────────────────────────────

def test_dragon_fruit_confluence_detector():
    # OB: top 22150, bottom 22130; FVG touching directly: bottom 22152, top 22170 (gap = 2.0 pts <= 5.0 pts)
    obs = [OrderBlock(dir="bull", i=3, ts=1700000900, bottom=22130.0, top=22150.0, created_i=4, kind="OB")]
    fvgs = [
        FVG(dir="bull", i=4, ts=1700001200, bottom=22152.0, top=22170.0),
        FVG(dir="bull", i=10, ts=1700003000, bottom=22250.0, top=22280.0),
    ]
    a = np.full(15, 12.0)
    cfg = {"dragon_fruit": {"max_touch_gap_pts": 5.0, "touch_tolerance_atr": 0.15}}

    dfs = detect_dragon_fruit(obs, fvgs, a, cfg)
    assert len(dfs) == 2
    # The touching one must be LOW risk (Dragon Fruit)
    touching = [d for d in dfs if d.risk == "LOW"]
    assert len(touching) == 1
    assert "Dragon Fruit A+" in touching[0].label
    assert touching[0].gap_pts == 2.0


# ── 3. Mitigation Block (MiB) Engine ──────────────────────────────────────────

def test_mitigation_block_engine():
    # Triplet: H1 (22300) -> L1 (22200) -> H2 (22260, failed HH) -> Break below L1 (22200)
    closes = [22250, 22300, 22250, 22200, 22230, 22260, 22210, 22170, 22190]
    b = _make_bars(closes, wick=2.0)
    a = np.full(len(b), 14.0)

    swings = [
        Swing(kind="H", i=1, price=22300.0, ts=int(b.ts[1]), confirm_i=2),
        Swing(kind="L", i=3, price=22200.0, ts=int(b.ts[3]), confirm_i=4),
        Swing(kind="H", i=5, price=22260.0, ts=int(b.ts[5]), confirm_i=6),
    ]

    mibs = detect_mitigation_blocks(b, swings, a, {"mitigation_block": {"max_age_bars": 50}})
    assert len(mibs) >= 1
    mib = mibs[0]
    assert mib.dir == "bear"
    assert mib.fail_swing_price == 22260.0
    assert mib.interim_swing_price == 22200.0
    assert mib.broken_i == 7  # bar 7 closed at 22170 < 22200


# ── 4. ROTE — Reverse Optimal Trade Entry ─────────────────────────────────────

def test_rote_reverse_optimal_trade_entry():
    # Target: EQL pool at 22100. Opposing sweep high at 22300.
    # 0.50 EQ = 22200. ROTE 0.70-0.80 zone = [22240, 22260].
    closes = [22150, 22100, 22180, 22250, 22300, 22260, 22190, 22245, 22230]
    b = _make_bars(closes, wick=3.0)
    a = np.full(len(b), 12.0)

    pools = [Pool(side="SSL", src="EQL", price=22100.0, known_i=1, ts=int(b.ts[1]))]
    swings = [
        Swing(kind="L", i=1, price=22100.0, ts=int(b.ts[1]), confirm_i=2),
        Swing(kind="H", i=4, price=22300.0, ts=int(b.ts[4]), confirm_i=5),
    ]

    cfg = {
        "rote": {
            "rote_fib_entry_low": 0.70,
            "rote_fib_entry_high": 0.80,
            "rote_tp1_low": 0.20,
            "rote_tp1_high": 0.30,
            "sl_buffer_atr": 0.10,
        }
    }

    rotes = detect_rote(b, a, swings, pools, cfg)
    assert len(rotes) >= 1
    r = rotes[0]
    assert r.dir == "bear"
    assert r.target_price == 22100.0
    assert r.sweep_price == 22300.0
    assert r.eq_050 == 22200.0
    assert r.entry_low == 22240.0
    assert r.entry_high == 22260.0
    assert r.status in ("IN_ZONE", "TRIGGERED")


# ── 5. Pure SFP vs. cSFP vs. Gray Accumulation ────────────────────────────────

def test_sfp_classifier_pure_csfp_gray_accumulation():
    # Test swing high at 22200
    # Bar 2: Pure SFP (high 22205, close 22195)
    b = _make_bars(
        closes=[22180, 22200, 22195, 22150, 22140, 22130],
        highs=[22185, 22200, 22205, 22160, 22145, 22135],
        lows=[22170, 22190, 22185, 22140, 22130, 22120],
    )
    a = np.full(len(b), 10.0)
    swings = [Swing(kind="H", i=1, price=22200.0, ts=int(b.ts[1]), confirm_i=2)]
    cfg = {"sfp_classifier": {"csfp_max_pierce_atr": 0.18, "gray_accum_min_bars": 3, "gray_accum_dev_atr": 0.20}}

    sfps = classify_sfp_tests(b, swings, a, cfg)
    assert len(sfps) >= 1
    assert sfps[0].state == "PURE_SFP"
    assert sfps[0].reversal_confirmed is True


# ── 6. RBR / DBD / RBD / DBR S/D Base Scanner ─────────────────────────────────

def test_sd_bases_scanner():
    # RBR: Leg 1 green rally (22100 -> 22140) -> 3-bar tight base (22138-22144) -> Leg 2 green breakout (22170)
    closes = [22100, 22140, 22142, 22140, 22143, 22175, 22180]
    b = _make_bars(closes, wick=2.0)
    a = np.full(len(b), 12.0)
    fvgs = [FVG(dir="bull", i=5, ts=int(b.ts[5]), bottom=22150.0, top=22175.0)]

    cfg = {
        "supply_demand_base": {"base_min_bars": 3, "base_max_bars": 10, "base_height_max_atr": 1.60},
        "displacement": {"disp_body_atr_mult": 1.05},
    }

    bases = detect_sd_bases(b, a, fvgs, cfg)
    assert len(bases) >= 1
    assert bases[0].kind == "RBR"
    assert bases[0].polarity == "demand"


# ── 7. FTR — Failed To Return / Indecision Candle Zones ───────────────────────

def test_ftr_indecision_candle_zones():
    # Bullish FTR: Strong green impulse (bar 3) -> 1 red pause candle (bar 4) -> Strong green continuation (bar 5) -> test (bar 6)
    opens =  [22100, 22105, 22110, 22115, 22145, 22142, 22180, 22160, 22170, 22175]
    closes = [22105, 22110, 22115, 22145, 22142, 22180, 22165, 22170, 22175, 22185]
    highs =  [22110, 22115, 22120, 22150, 22147, 22185, 22170, 22175, 22180, 22190]
    lows =   [22095, 22100, 22105, 22110, 22138, 22140, 22142, 22155, 22165, 22170]
    b = _make_bars(closes, highs=highs, lows=lows, opens=opens)
    a = np.full(len(b), 10.0)

    cfg = {"ftr": {"ftr_min_bars": 1, "ftr_max_bars": 3, "max_age_bars": 50}, "displacement": {"disp_body_atr_mult": 1.05}}
    ftrs = detect_ftr_zones(b, a, cfg)
    assert len(ftrs) >= 1
    assert ftrs[0].dir == "bull"
    assert ftrs[0].pause_bars in (1, 2)


# ── 8. TT3 — Three Tap Range Extremity Setup ──────────────────────────────────

def test_tt3_three_tap_setup():
    # Range: 22100 to 22300. Tap 1 at 22105 -> Tap 2 deviation at 22090 -> Tap 3 retest at 22102
    closes = [22200, 22105, 22150, 22090, 22140, 22102, 22160]
    b = _make_bars(closes, wick=3.0)
    a = np.full(len(b), 15.0)

    swings = [
        Swing(kind="L", i=1, price=22105.0, ts=int(b.ts[1]), confirm_i=2),
        Swing(kind="L", i=3, price=22090.0, ts=int(b.ts[3]), confirm_i=4),
    ]

    rng_dict = {"high": 22300.0, "low": 22100.0}
    cfg = {"tt3": {"deviation_sl_buffer_atr": 0.10}}

    tt3s = detect_tt3(b, a, swings, rng_dict, cfg)
    assert len(tt3s) >= 1
    t = tt3s[0]
    assert t.side == "bull"
    assert t.tap1_price == 22105.0
    assert t.tap2_extreme == 22090.0
    assert t.tp1 == 22200.0  # Range mid
    assert t.tp2 == 22300.0  # Range high


# ── 9. GPX — Gameplay Box Corridor & VWAP Wave Rider ──────────────────────────

def test_gpx_corridor_and_vwap_wave_rider():
    closes = [22200, 22220, 22250, 22240]
    b = _make_bars(closes)
    a = np.full(len(b), 10.0)

    # Mock HTF analysis with supply ceiling at 22400 and demand floor at 22100
    class MockHTF:
        tf = "15m"
        bars = b
        obs = [
            OrderBlock(dir="bear", i=1, ts=int(b.ts[1]), bottom=22380.0, top=22400.0, created_i=2, kind="OB"),
            OrderBlock(dir="bull", i=0, ts=int(b.ts[0]), bottom=22100.0, top=22120.0, created_i=1, kind="OB"),
        ]
        fvgs = []

    spot = 22240.0
    gpx = compute_gpx(spot, a, vwap_val=22230.0, htf_an=MockHTF(), cfg={"gpx": {"mean_reversion_overextended_atr": 2.20}})
    assert gpx is not None
    assert gpx.corridor_high == 22400.0
    assert gpx.corridor_low == 22100.0
    assert gpx.corridor_mid == 22250.0
    assert gpx.zone == "DISCOUNT"
    assert gpx.veto_short is True  # Never short in discount!
    assert gpx.vwap_state == "ABOVE_VWAP"


# ── 10. Trap & Inducement Veto Engine ──────────────────────────────────────────

def test_trap_and_inducement_veto():
    # Candidate Bear OB: top 22250, bottom 22230.
    # Untaken EQH sitting right above at 22260 (within 1.5 * ATR = 18 pts)
    obs = [OrderBlock(dir="bear", i=3, ts=1700000900, bottom=22230.0, top=22250.0, created_i=4, kind="OB")]
    pools = [Pool(side="BSL", src="EQH", price=22260.0, known_i=2, ts=1700000600)]
    a = np.full(10, 12.0)

    cfg = {"liquidity_magnets": {"inducement_proximity_atr": 1.50}}
    traps = detect_traps_inducements(obs, pools, [], a, cfg)
    assert len(traps) >= 1
    tr = traps[0]
    assert tr.zone_kind == "BEAR_OB"
    assert tr.magnet_type == "EQH"
    assert tr.veto_entry is True
    assert "TRAP WARNING" in tr.warning


# ── 11. End-to-End Real NIFTY Data Test ───────────────────────────────────────

def test_pa_advanced_on_real_nifty_oct8_data():
    from app.engine.analyze import analyze
    rows_5m = five_min()
    rows_15m = fifteen_min()

    an_5m = analyze(rows_5m, "5m")
    an_15m = analyze(rows_15m, "15m")

    res = compute_pa_advanced(
        an_5m.bars,
        an_5m.atr,
        an_5m.swings,
        an_5m.obs,
        an_5m.fvgs,
        an_5m.pools,
        an_5m.rng,
        vwap_val=float(an_5m.bars.c[-1]),
        htf_an=an_15m,
        tf="5m",
        chain_summary={"call_wall": 22300, "put_wall": 22100},
    )

    assert res["available"] is True
    assert "zones" in res
    assert "markers" in res
    assert "gpx" in res
    assert isinstance(res["msb_quality"], list)
    assert isinstance(res["dragon_fruit"], list)
    assert isinstance(res["mitigation_blocks"], list)
    assert "bpr_zones" in res
    assert isinstance(res["bpr_zones"], list)


# ── 12. BPR & Super BPR Detection Test ────────────────────────────────────────

def test_bpr_and_super_bpr_detection():
    # Bull FVG: bottom 22100, top 22130; Bear FVG: bottom 22115, top 22150
    # Overlap region: [22115, 22130]
    fvgs = [
        FVG(dir="bull", i=2, ts=1700000600, bottom=22100.0, top=22130.0),
        FVG(dir="bear", i=5, ts=1700001500, bottom=22115.0, top=22150.0),
    ]
    bprs = detect_bprs(fvgs)
    assert len(bprs) == 1
    assert bprs[0]["kind"] == "BPR"
    assert bprs[0]["bot"] == 22115.0
    assert bprs[0]["top"] == 22130.0
    assert bprs[0]["overlaps"] == 2

    # Add a 3rd FVG covering the intersection -> Upgrades to SUPER BPR
    fvgs.append(FVG(dir="bear", i=8, ts=1700002400, bottom=22110.0, top=22140.0))
    super_bprs = detect_bprs(fvgs)
    assert len(super_bprs) >= 1
    super_zone = next((b for b in super_bprs if b["kind"] == "SUPER BPR"), None)
    assert super_zone is not None
    assert super_zone["overlaps"] >= 3
    assert "SUPER BPR" in super_zone["label"]


# ── 13. Sniper Decision & Confluence Cluster Merger Test ──────────────────────

def test_sniper_view_confluence_and_capping():
    # 1. Verify compute_pa_advanced attaches sniper view
    from oct8_data import five_min, fifteen_min
    from app.engine.analyze import analyze

    rows_5m = five_min()
    rows_15m = fifteen_min()
    an_5m = analyze(rows_5m, "5m")
    an_15m = analyze(rows_15m, "15m")

    res = compute_pa_advanced(
        an_5m.bars,
        an_5m.atr,
        an_5m.swings,
        an_5m.obs,
        an_5m.fvgs,
        an_5m.pools,
        an_5m.rng,
        vwap_val=float(an_5m.bars.c[-1]),
        htf_an=an_15m,
        tf="5m",
    )

    assert "sniper" in res
    sniper = res["sniper"]
    assert "state" in sniper
    assert "behavior" in sniper
    assert "action" in sniper
    assert "headline" in sniper
    assert "best_supply" in sniper
    assert "best_demand" in sniper

    # 2. Synthetic confluence merger & scoring test
    spot = 22900.0
    atr_val = 20.0
    pa_synthetic = {
        "msb_events": [
            {
                "dir": "bear",
                "super_ob_score": 6,
                "ob": {"top": 22960.0, "bot": 22945.0},
                "dragon_fruit": True,
                "ts": 1700000000,
            }
        ],
        "bpr_zones": [
            {
                "kind": "SUPER BPR",
                "top": 22955.0,
                "bot": 22942.0,
                "overlap_count": 3,
                "ts": 1700000100,
            }
        ],
        "rote_setups": [
            {
                "side": "SHORT",
                "ote_zone": [22948.0, 22958.0],
                "ts": 1700000200,
            }
        ],
        "gpx_corridor": {
            "corridor_mid": 22850.0,  # Supply is at 22950 > 22850 (GPX Premium)
            "range_eq": 22850.0,
        },
    }

    # Candles tapping into the supply zone and rejecting (close < open, close <= top)
    closed = [
        {"ts": 1700000300, "o": 22910.0, "h": 22930.0, "l": 22900.0, "c": 22920.0},
        {"ts": 1700000600, "o": 22950.0, "h": 22958.0, "l": 22938.0, "c": 22942.0},  # Bearish rejection candle inside zone
    ]

    syn_sniper = _synthesize_sniper_view(pa_synthetic, closed, atr_val, spot)
    best_sup = syn_sniper["best_supply"]
    assert best_sup is not None
    assert best_sup["side"] == "SUPPLY"
    # Confluence: Super OB (2.5) + Dragon Fruit (2.5) + Super BPR (2.5) + ROTE (1.8) + GPX Premium (1.0) = 10.0 (capped at 10)
    assert best_sup["score"] >= 8.5
    assert "A+ SNIPER" in best_sup["grade"]
    assert len(best_sup["tags"]) >= 3

    # Verification of state classifier: Bearish rejection inside A+ supply POI
    assert syn_sniper["state"] == "SNIPER REJECTION HOLD"
    assert "BUY PE" in syn_sniper["action"]
    assert syn_sniper["trade_plan"] is not None
    plan = syn_sniper["trade_plan"]
    assert plan["side"] == "SHORT"
    assert plan["entry"] == 22942.0
    assert plan["sl"] > plan["entry"]
    assert plan["be_1r"] < plan["entry"]

    # Verification of Bias 20 expansion and Sniper Confluence triggers
    assert "bias_20" in syn_sniper
    b20 = syn_sniper["bias_20"]
    assert b20["total_factors"] == 20
    assert b20["max_score"] == 20.0
    assert "triggers" in b20
    triggers = b20["triggers"]

    for trig_key in ("IFVG", "RJB+", "CRT", "DOL", "HTF_Wick", "APlus"):
        assert trig_key in triggers
        t = triggers[trig_key]
        assert t["status"] in ("ACTIVE", "VETOED", "NEUTRAL")
        assert "weight" in t
        assert "desc" in t

    # Check exact course weights
    assert triggers["IFVG"]["weight"] == 0.5
    assert triggers["RJB+"]["weight"] == 0.5
    assert triggers["CRT"]["weight"] == 1.0
    assert triggers["DOL"]["weight"] == 1.0
    assert triggers["HTF_Wick"]["weight"] == 0.75
    assert triggers["APlus"]["weight"] == 1.5

    # Check factors_20 length and structure
    assert len(b20["factors_20"]) == 20
    assert b20["factors_20"][19]["name"] == "★ A+ Confluence"
    assert b20["factors_20"][19]["weight"] == 1.5


def test_compute_pa_advanced_enriches_bias_20():
    closes = [22800 + i * 5.0 for i in range(30)]
    b = _make_bars(closes)
    atr = np.full(len(b), 15.0)
    swings = []
    obs = []
    fvgs = []
    pools = []

    res = compute_pa_advanced(b, atr, swings, obs, fvgs, pools, tf="5m")
    assert res["available"] is True
    assert "bias_20" in res
    assert "sniper" in res
    assert "bias_20" in res["sniper"]
    assert res["bias_20"]["total_factors"] == 20
    assert res["bias_20"]["max_score"] == 20.0



