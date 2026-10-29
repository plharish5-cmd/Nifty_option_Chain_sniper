"""
test_gap_sniper.py — Unit tests for GIFT Nifty Opening Gap & Confluence Sniper Playbook.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import gap_sniper


def test_gap_sniper_absorption_scenario(monkeypatch):
    """-100 pt Gap Down landing on Put Wall S1 (22,600) with Put writer defense -> Absorption & Dip Buy."""
    cfg = {
        "significant_gap_pts": 80.0,
        "major_gap_pts": 100.0,
        "wall_proximity_pts": 40.0,
        "wick_rejection_ratio": 0.35,
        "writer_fortify_oi": 1500000,
        "writer_unwind_oi": -1500000,
        "writer_panic_cover_oi": -2000000,
        "default_sim_gap_pts": -100.0,
    }

    monkeypatch.setattr(gap_sniper.desk_bias, "get_structural_walls", lambda *a, **k: (22600.0, 22800.0))
    # Simulate GIFT Nifty opening near 22,650 (approx -100 pts against 22,750 spot)
    res = gap_sniper.compute_gap_confluence(sim_gap_pts=-100.0, cfg=cfg)
    assert res["ok"] is True
    assert res["gap_type"] == "GAP_DOWN"
    assert res["is_significant_gap"] is True
    assert res["is_major_gap"] is True

    playbook = res["playbook"]
    assert playbook["code"] == "GAP_DOWN_ABSORPTION"
    assert "ABSORPTION" in playbook["title"]
    assert playbook["theme"] == "bullish"
    assert "BUY" in playbook["action"] and "CE" in playbook["action"]
    assert playbook["stop_loss"] <= 22600.0
    assert playbook["target_1"] > res["expected_open_spot"]

    # Verify 5-point checklist
    assert len(res["checklist"]) == 5
    assert int(res["confluence_score"].split("/")[0]) >= 4


def test_gap_sniper_cascade_breakdown():
    """-100 pt Gap Down with heavy Put unwinding -> Cascade Breakdown PE momentum scalp."""
    cfg = {
        "significant_gap_pts": 80.0,
        "major_gap_pts": 100.0,
        "wall_proximity_pts": 40.0,
        "wick_rejection_ratio": 0.35,
        "writer_fortify_oi": 1500000,
        "writer_unwind_oi": -1500000,
        "writer_panic_cover_oi": -2000000,
        "default_sim_gap_pts": -100.0,
    }

    class MockHub:
        class MockChain:
            def get_pcr_trinity(self, exp=None, fut_src="combined", hub=None):
                return {
                    "pcr_oi_series": [
                        {
                            "put_oi_chg": -2500000,  # Put writers panic unwinding (> 15L unwound)
                            "call_oi_chg": 800000,
                            "pcr_oi": 0.72,
                        }
                    ]
                }
        chain = MockChain()

    # Pass actual gift_nifty price slicing below support
    res = gap_sniper.compute_gap_confluence(gift_nifty=22450.0, hub=MockHub(), cfg=cfg)
    assert res["ok"] is True
    assert res["gap_type"] == "GAP_DOWN"
    playbook = res["playbook"]
    assert playbook["code"] == "GAP_DOWN_CASCADE"
    assert "CASCADE" in playbook["title"]
    assert playbook["theme"] == "bearish"
    assert "PE" in playbook["action"]


def test_gap_sniper_gap_up_fade():
    """+100 pt Gap Up into Call Wall ceiling (23,000) with Call writer defense -> Fade Rejection."""
    cfg = {
        "significant_gap_pts": 80.0,
        "major_gap_pts": 100.0,
        "wall_proximity_pts": 40.0,
        "wick_rejection_ratio": 0.35,
        "writer_fortify_oi": 1500000,
        "writer_unwind_oi": -1500000,
        "writer_panic_cover_oi": -2000000,
        "default_sim_gap_pts": -100.0,
    }

    res = gap_sniper.compute_gap_confluence(sim_gap_pts=+100.0, cfg=cfg)
    assert res["ok"] is True
    assert res["gap_type"] == "GAP_UP"
    assert res["is_significant_gap"] is True
    playbook = res["playbook"]
    assert playbook["code"] == "GAP_UP_FADE"
    assert "FADE" in playbook["title"]
    assert playbook["theme"] == "bearish"
    assert "PE" in playbook["action"]


def test_gap_sniper_gap_up_squeeze():
    """+100 pt Gap Up with Call writers panic covering (>20L) -> Short Squeeze continuation."""
    cfg = {
        "significant_gap_pts": 80.0,
        "major_gap_pts": 100.0,
        "wall_proximity_pts": 40.0,
        "wick_rejection_ratio": 0.35,
        "writer_fortify_oi": 1500000,
        "writer_unwind_oi": -1500000,
        "writer_panic_cover_oi": -2000000,
        "default_sim_gap_pts": -100.0,
    }

    class MockHub:
        class MockChain:
            def get_pcr_trinity(self, exp=None, fut_src="combined", hub=None):
                return {
                    "pcr_oi_series": [
                        {
                            "put_oi_chg": 500000,
                            "call_oi_chg": -2800000,  # Panic covering (>20L calls covered)
                            "pcr_oi": 1.18,
                        }
                    ]
                }
        chain = MockChain()

    res = gap_sniper.compute_gap_confluence(sim_gap_pts=+100.0, hub=MockHub(), cfg=cfg)
    assert res["ok"] is True
    assert res["gap_type"] == "GAP_UP"
    playbook = res["playbook"]
    assert playbook["code"] == "GAP_UP_SQUEEZE"
    assert "SHORT SQUEEZE" in playbook["title"]
    assert playbook["theme"] == "bullish"
    assert "CE" in playbook["action"]


def test_gap_sniper_flat_open():
    """Small/flat gap (+15 pts) -> Equilibrium Open & Wait for 09:30 AM."""
    cfg = {
        "significant_gap_pts": 80.0,
        "major_gap_pts": 100.0,
        "wall_proximity_pts": 40.0,
        "wick_rejection_ratio": 0.35,
        "writer_fortify_oi": 1500000,
        "writer_unwind_oi": -1500000,
        "writer_panic_cover_oi": -2000000,
        "default_sim_gap_pts": -100.0,
    }

    res = gap_sniper.compute_gap_confluence(sim_gap_pts=+15.0, cfg=cfg)
    assert res["ok"] is True
    assert res["gap_type"] == "FLAT"
    assert res["is_significant_gap"] is False
    playbook = res["playbook"]
    assert playbook["code"] == "FLAT_EQUILIBRIUM"
    assert "EQUILIBRIUM" in playbook["title"]
    assert playbook["theme"] == "neutral"


def test_gap_sniper_live_query():
    """End-to-end integration test against live database state."""
    res = gap_sniper.compute_gap_confluence()
    assert res["ok"] is True
    assert "gift_nifty_ltp" in res
    assert "playbook" in res
    assert "checklist" in res
    assert "timeline" in res
    assert "multi_contract_table" in res
    assert len(res["multi_contract_table"]) == 4


def test_gap_sniper_multi_contract_table():
    """Validates multi-contract comparison across GIFT, Spot, Near-Month FUT, and Next-Month FUT."""
    res = gap_sniper.compute_gap_confluence(sim_gap_pts=-134.2)
    assert res["ok"] is True
    table = res["multi_contract_table"]
    assert len(table) == 4

    gift_row = next(r for r in table if r["symbol"] == "GIFT NIFTY")
    assert gift_row["diff_vs_oct"] == -134.2
    assert "ANCHOR" in gift_row["classification"]

    spot_row = next(r for r in table if r["symbol"] == "NIFTY 50")
    assert spot_row["ltp"] > 20000.0

    oct_row = next(r for r in table if "Oct" in r["instrument"])
    assert oct_row["diff_vs_oct"] == 0.0
    assert "Baseline" in oct_row["live_gap_status"]

    nov_row = next(r for r in table if "Nov" in r["instrument"])
    assert nov_row["ltp"] > spot_row["ltp"]  # Nov carries carry
    assert "Carry" in nov_row["live_gap_status"]


def test_gap_sniper_live_spot_anchor_and_tiers():
    """Validates Spot vs PDC anchor switch and 5 gap tiers."""
    # 1. Major Gap Up (>= 80 pts)
    res_maj_up = gap_sniper.compute_gap_confluence(sim_gap_pts=85.0)
    assert res_maj_up["gap_tier"] == "MAJOR_GAP_UP"
    assert "MAJOR GAP UP" in res_maj_up["gap_tier_label"]
    assert res_maj_up["primary_anchor"] == "SPOT"

    # 2. Moderate Gap Up (+25 to +80 pts)
    res_mod_up = gap_sniper.compute_gap_confluence(sim_gap_pts=45.0)
    assert res_mod_up["gap_tier"] == "MODERATE_GAP_UP"
    assert "MODERATE GAP UP" in res_mod_up["gap_tier_label"]

    # 3. Flat Open (-25 to +25 pts)
    res_flat = gap_sniper.compute_gap_confluence(sim_gap_pts=10.0)
    assert res_flat["gap_tier"] == "FLAT"
    assert "FLAT / EQUILIBRIUM OPEN" in res_flat["gap_tier_label"]

    # 4. Moderate Gap Down (-80 to -25 pts)
    res_mod_down = gap_sniper.compute_gap_confluence(sim_gap_pts=-45.0)
    assert res_mod_down["gap_tier"] == "MODERATE_GAP_DOWN"
    assert "MODERATE GAP DOWN" in res_mod_down["gap_tier_label"]

    # 5. Major Gap Down (<= -80 pts)
    res_maj_down = gap_sniper.compute_gap_confluence(sim_gap_pts=-95.0)
    assert res_maj_down["gap_tier"] == "MAJOR_GAP_DOWN"
    assert "MAJOR GAP DOWN" in res_maj_down["gap_tier_label"]


def test_gap_sniper_bullish_pre_open_testing_pdh():
    """Validates live case: live_spot = 22777.8, PDC = 22753.2, Gap = +24.6 pts testing PDH = 22780.0."""
    class MockHub:
        def quote(self):
            return {"spot": 22777.8, "prev_close": 22753.2}

    res = gap_sniper.compute_gap_confluence(hub=MockHub())
    assert res["ok"] is True
    assert res["live_spot"] == 22777.8
    assert res["pdc_spot"] == 22753.2
    assert res["spot_gap_pts"] == 24.6
    assert res["primary_anchor"] == "SPOT"

    # Row 1 must be NIFTY 50 Cash (Spot) [PRIMARY ANCHOR]
    table = res["multi_contract_table"]
    assert table[0]["symbol"] == "NIFTY 50"
    assert table[0]["is_anchor"] is True
    assert "PRIMARY ANCHOR" in table[0]["classification"]
    assert "2.2 pts below PDH" in table[0]["proximity"]

    # Playbook should be Bullish Pre-Open testing PDH
    pb = res["playbook"]
    assert pb["code"] == "BULLISH_ABOVE_PDC_PDH"
    assert "TESTING PDH" in pb["title"]
    assert "22,750 CE" in pb["action"] or "CE" in pb["action"]

    # Voice alert text contains spot, PDC, gap, and testing PDH
    voice = res["voice_message"]
    assert "22777.8" in voice
    assert "22753.2" in voice
    assert "Testing Previous Day High" in voice

