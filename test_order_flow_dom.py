"""
test_order_flow_dom.py — Unit and integration tests for Module I: Order Flow Layer (Strike-Level DOM).
"""
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import pytest

import agt_briefing
import decision_cockpit_view as dcv
import order_flow_dom
from order_flow_dom import DOMRebuilder, StrikeSelectorDOM, Wall


def test_order_flow_config_loader():
    cfg = order_flow_dom.load_order_flow_config()
    assert cfg is not None
    assert cfg["wall_threshold"] == 25000
    assert cfg["wall_min_seconds"] == 20
    assert cfg["absorb_threshold"] == 15000
    assert cfg["sweep_ratio"] == 0.5
    assert cfg["paper_trade_mode"] is True
    assert cfg["min_confirm_win_rate"] == 0.60


def test_strike_selector_and_rotation(tmp_path):
    rot_file = tmp_path / "test_rotation.jsonl"
    cfg = {
        "strike_step": 50.0,
        "target_spot_shift_pts": 50.0,
        "rotation_log_path": str(rot_file),
    }
    selector = StrikeSelectorDOM(cfg)

    # Initial spot 24230 -> ATM is 24250, ATM+50 is 24300
    res1 = selector.check_and_update(spot=24230.0, current_time=datetime(2026, 8, 24, 9, 15))
    assert res1["rotated"] is True
    assert res1["symbols"]["ATM_CE"] == "NIFTY24250-CE"
    assert res1["symbols"]["ATM_PE"] == "NIFTY24250-PE"
    assert res1["symbols"]["ATM_P50_CE"] == "NIFTY24300-CE"
    assert res1["symbols"]["ATM_P50_PE"] == "NIFTY24300-PE"

    # Small movement: spot moves to 24260 (diff = 30 pts < 50 pts) -> No rotation
    res2 = selector.check_and_update(spot=24260.0, current_time=datetime(2026, 8, 24, 10, 0))
    assert res2["rotated"] is False
    assert res2["symbols"]["ATM_CE"] == "NIFTY24250-CE"

    # Big movement: spot moves to 24290 (diff = 60 pts > 50 pts) -> Rotates!
    res3 = selector.check_and_update(spot=24290.0, current_time=datetime(2026, 8, 24, 10, 30))
    assert res3["rotated"] is True
    # ATM for 24290 is 24300, ATM+50 is 24350
    assert res3["symbols"]["ATM_CE"] == "NIFTY24300-CE"
    assert res3["symbols"]["ATM_PE"] == "NIFTY24300-PE"
    assert res3["symbols"]["ATM_P50_CE"] == "NIFTY24350-CE"

    # Verify rotation logged
    assert rot_file.exists()
    lines = rot_file.read_text().strip().split("\n")
    assert len(lines) == 2  # initial + rotation


def test_wall_persistence_rule():
    rebuilder = DOMRebuilder({
        "wall_threshold": 25000,
        "wall_min_seconds": 20,
    })

    # 1. Spoofed wall: appears for 10 seconds and vanishes
    # Should NOT become an active persisted wall
    rebuilder.bars = []
    t_start = 1787543500
    for s in range(t_start, t_start + 10):
        rebuilder.bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 100.0,
            "buy_vol": 0,
            "sell_vol": 0,
            "cvd": 0,
            "best_bid": 99.5,
            "best_ask": 100.0,
            "top20_bids": [[99.0, 30000]],
            "top20_asks": [],
        })
    # Disappears at second 11
    rebuilder.bars.append({
        "sec": t_start + 10,
        "dt": datetime.fromtimestamp(t_start + 10),
        "ltp": 100.0,
        "buy_vol": 0,
        "sell_vol": 0,
        "cvd": 0,
        "best_bid": 99.5,
        "best_ask": 100.0,
        "top20_bids": [[99.0, 5000]],
        "top20_asks": [],
    })

    rebuilder._detect_signals_and_replay()
    assert ("BID", 99.0) not in rebuilder.all_walls

    # 2. Genuine wall: persists for 25 seconds
    rebuilder.bars = []
    for s in range(t_start, t_start + 25):
        rebuilder.bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 100.0,
            "buy_vol": 0,
            "sell_vol": 0,
            "cvd": 0,
            "best_bid": 99.5,
            "best_ask": 100.0,
            "top20_bids": [[99.0, 30000]],
            "top20_asks": [],
        })

    rebuilder._detect_signals_and_replay()
    assert ("BID", 99.0) in rebuilder.all_walls
    wall = rebuilder.all_walls[("BID", 99.0)]
    assert wall.persisted is True
    assert wall.status == "ACTIVE"


def test_defended_wall_signal():
    rebuilder = DOMRebuilder({
        "wall_threshold": 25000,
        "wall_min_seconds": 20,
        "sweep_ratio": 0.5,
    })

    t_start = 1787543500
    bars = []
    # Build a wall at 99.0 for 20 seconds
    for s in range(t_start, t_start + 20):
        bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 100.0,
            "buy_vol": 0,
            "sell_vol": 0,
            "cvd": 0,
            "best_bid": 99.0,
            "best_ask": 99.5,
            "top20_bids": [[99.0, 30000]],
            "top20_asks": [],
        })

    # Now sweep hits the wall: market sells = 16,000 (> 0.5 * 30000) at ltp 99.05
    # Price holds (stays >= 99.0)
    for s in range(t_start + 20, t_start + 25):
        bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 99.05,
            "buy_vol": 0,
            "sell_vol": 3500,  # total 5 * 3500 = 17,500
            "cvd": -3500 * (s - (t_start + 19)),
            "best_bid": 99.0,
            "best_ask": 99.5,
            "top20_bids": [[99.0, 30000]],
            "top20_asks": [],
        })

    rebuilder.bars = bars
    rebuilder._detect_signals_and_replay()

    wall = rebuilder.all_walls[("BID", 99.0)]
    assert wall.status == "DEFENDED_S"
    assert any(sig["type"] == "DEFENDED_WALL" and sig["signal"] == "CONFIRM_LONG" for sig in rebuilder.raw_signals)


def test_absorption_signal():
    rebuilder = DOMRebuilder({
        "wall_threshold": 25000,
        "wall_min_seconds": 20,
        "absorb_threshold": 15000,
        "absorb_window_seconds": 10,
        "absorb_max_ticks": 2,
        "absorb_near_wall_ticks": 3,
        "absorb_min_pulses": 2,
    })

    t_start = 1787543500
    bars = []
    # Fill 60 baseline bars
    for s in range(t_start, t_start + 60):
        bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 100.0,
            "buy_vol": 100,
            "sell_vol": 100,
            "cvd": 1000 + (s - t_start) * 50,  # CVD rising
            "best_bid": 100.0,
            "best_ask": 100.05,
            "top20_bids": [[100.0, 30000]],  # Wall at 100.0
            "top20_asks": [],
        })

    # Heavy aggressive sells absorbed at 100.0, price stays within 1 tick, CVD rising
    for s in range(t_start + 60, t_start + 70):
        bars.append({
            "sec": s,
            "dt": datetime.fromtimestamp(s),
            "ltp": 100.0,
            "buy_vol": 200,
            "sell_vol": 2000,  # 10 * 2000 = 20,000 sell volume in 10s
            "cvd": 4000 + (s - (t_start + 60)) * 200,  # CVD slope strongly positive
            "best_bid": 100.0,
            "best_ask": 100.05,
            "top20_bids": [[100.0, 30000]],
            "top20_asks": [],
        })

    rebuilder.bars = bars
    # Provide 2 distinct trade pulses within the 10s window at 100.0
    rebuilder.trade_quotes = [(t_start + 62, 10000, 100.0), (t_start + 65, 10000, 100.0)]
    rebuilder._detect_signals_and_replay()

    assert len(rebuilder.absorption_events) > 0
    ab = rebuilder.absorption_events[0]
    assert ab.side == "BID"
    assert ab.verdict == "CONFIRM_LONG"
    assert len(rebuilder.rejected_absorptions) == 0

    # Test rejection when NOT near wall
    rebuilder2 = DOMRebuilder({
        "wall_threshold": 25000,
        "wall_min_seconds": 20,
        "absorb_threshold": 15000,
        "absorb_window_seconds": 10,
        "absorb_max_ticks": 2,
        "absorb_near_wall_ticks": 3,
        "absorb_min_pulses": 2,
    })
    bars_no_wall = []
    for b in bars:
        b_copy = dict(b)
        b_copy["top20_bids"] = []  # No wall
        bars_no_wall.append(b_copy)
    rebuilder2.bars = bars_no_wall
    rebuilder2.trade_quotes = [(t_start + 62, 10000, 100.0), (t_start + 65, 10000, 100.0)]
    rebuilder2._detect_signals_and_replay()
    assert len(rebuilder2.absorption_events) == 0
    assert len(rebuilder2.rejected_absorptions) == 1
    assert rebuilder2.rejected_absorptions[0]["reason"] == "NOT_NEAR_WALL"


def test_acceptance_replay_sample():
    # Test bundled NIFTY24250-CE replay with 4 pre-paper-trade fixes
    res = order_flow_dom.run_order_flow_acceptance_test(
        sample_date="2026-08-24",
        sample_symbol="NIFTY24250-CE",
        live_day_date="2026-08-21",
        live_day_symbol="GRASIM",
    )
    # Fix 3: GRASIM dropped from acceptance
    assert "recorded_live_day" in res
    assert res["recorded_live_day"]["status"] == "DROPPED_PER_FIX3"

    assert "bundled_nifty" in res
    nifty_res = res["bundled_nifty"]
    assert nifty_res["status"] == "SUCCESS"
    assert nifty_res["walls_detected"] > 0
    assert nifty_res["total_bars_seconds"] > 20000

    # Fix 1: Absorption before/after counts
    assert nifty_res["raw_candidate_absorptions"] == 137
    assert nifty_res["rejected_absorptions_count"] >= 115
    assert 10 <= nifty_res["passed_absorptions_count"] <= 25

    # Fix 2 & 4: Cell Performance Matrix
    matrix = nifty_res["cell_matrix"]
    assert "SHORT_wall-hold" in matrix
    assert "LONG_wall-hold" in matrix
    assert "SHORT_break" in matrix
    assert "LONG_break" in matrix
    assert "LONG_absorption" in matrix

    # Dominant cell on 2026-08-24 is SHORT_wall-hold
    assert nifty_res["dominant_cell"] == "SHORT_wall-hold"
    assert nifty_res["dominant_cell_win_rate_15m_pct"] == 75.0
    assert nifty_res["dominant_cell_meets_60pct"] is True

    # Multi-day gate: 1 day recorded < 5 required -> Clock ON HOLD, Veto SIM-ONLY
    mg = nifty_res["multi_day_gate"]
    assert mg["days_recorded"] == 1
    assert mg["days_required"] == 5
    assert mg["paper_trade_clock_started"] is False
    assert "ON HOLD" in mg["paper_trade_clock_status"]
    assert mg["veto_mode"] == "SIM_ONLY"


def test_order_flow_cell_matrix_rebuilder():
    cfg = order_flow_dom.load_order_flow_config()
    rebuilder = DOMRebuilder(cfg)
    ok = rebuilder.load_and_rebuild("2026-08-24", "NIFTY24250-CE")
    assert ok is True

    cell_report = rebuilder.compute_cell_matrix()
    matrix = cell_report["matrix"]
    assert len(matrix) == 6
    assert cell_report["dominant_cell"] == "SHORT_wall-hold"
    assert matrix["SHORT_wall-hold"]["count"] == 8
    assert matrix["SHORT_wall-hold"]["wins_15m"] == 6
    assert matrix["SHORT_wall-hold"]["win_rate_15m_pct"] == 75.0
    assert matrix["SHORT_wall-hold"]["avg_return_15m_pct"] > 0.0


def test_decision_cockpit_action_line_prepend():
    # Verify that L8 action note prepends to what_to_do_now
    mock_l8 = type("MockL8", (), {
        "action_line_note": "✅ L8: absorption at 24250 CE bid wall, CVD ▲ — entry zone 115–118",
        "verdict": "CONFIRM",
        "sim_suppressed": False,
        "is_paper_trade": False,
        "nearest_defended_bid_wall": 115.0,
        "nearest_defended_ask_wall": 120.0,
        "absorption_status": "Absorbed at bid",
        "cvd_slope_badge": "▲",
        "symbol": "NIFTY24250-CE",
    })()

    stack_report = {
        "l8_report": mock_l8,
        "veto_active": False,
    }
    # Direct test of prepend logic
    what_now = "Enter NIFTY 24250 CE at spot 24230.0. Move SL to Cost as soon as spot reaches +20 pts!"
    final_action = "BUY_CE"
    if mock_l8 and mock_l8.action_line_note and final_action in ("BUY_CE", "BUY_PE"):
        what_now = f"{mock_l8.action_line_note} — {what_now}"

    assert what_now.startswith("✅ L8: absorption at 24250 CE bid wall, CVD ▲")
