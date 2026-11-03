"""
tests/test_htf_wick_ema_rb.py — Unit and integration tests for
Institutional 1H / 4H Wick & Shaved Extremes + 1H 8-EMA First-Touch Engine
Scored Against Null Baselines (PHASE 1B).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from app.api.main import app
from app.engine.htf_wick_ema_rb import (
    HtfLevel,
    compute_1h_8ema,
    extract_htf_levels,
    generate_live_alerts_and_voice,
    get_htf_wick_ema_rb,
    get_thresholds,
    run_null_scored_backtest,
    run_strict_trade_simulation,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def seed_candles_if_empty():
    from app import db
    candles = db.candles_1m("13", since=0)
    if len(candles) < 100:
        prod_db = Path(__file__).resolve().parent.parent / "data" / "ict_smc.sqlite3"
        if prod_db.exists():
            conn = sqlite3.connect(str(prod_db))
            rows = conn.execute("SELECT ts,o,h,l,c FROM candles_1m WHERE sec_id='13' ORDER BY ts").fetchall()
            if rows:
                seed = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": 100.0} for r in rows]
                db.upsert_candles("13", seed)


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


def test_null_baseline_backtest_scoring_and_voice_gating():
    """Verify that null-scored backtest runs, matches on time of day, computes z-scores, and gates voice alerts."""
    from app import db
    thresh = get_thresholds()
    raw_1m = db.candles_1m("13", since=0)
    assert len(raw_1m) > 1000

    null_summary = run_null_scored_backtest(raw_1m, thresh)
    assert null_summary["ok"] is True

    meta = null_summary["metadata"]
    assert meta["categories_tested"] == 10
    assert meta["cells_tested"] == 20
    assert "Multiple-comparison discount applies" in meta["statement"]

    cats = null_summary["categories"]
    assert len(cats) == 10
    assert "1H_UPPER_WICK" in cats
    assert "1H_8EMA_SUPPORT" in cats

    # Check that each evaluated category carries edge_z, dir_z, drop5, oos split
    cat_1h_w = cats["1H_UPPER_WICK"]
    assert cat_1h_w["sample_n"] > 50
    assert "s15_t20" in cat_1h_w
    assert "edge_z" in cat_1h_w["s15_t20"]
    assert "dir_z" in cat_1h_w["s15_t20"]
    assert "drop5" in cat_1h_w["s15_t20"]
    assert "oos_h1_exp" in cat_1h_w["s15_t20"]

    # All unproven categories must have voice_armed == False
    for cat_name, cat in cats.items():
        if cat.get("voice_armed") is False:
            assert cat["edge_status"] == "UNPROVEN_EDGE (SILENT CONTEXT ONLY)"


def test_zero_lookahead_forward_walk():
    """Verify that forward walk starts strictly AFTER the signal candle closes (time to target >= 1 min)."""
    from app import db
    thresh = get_thresholds()
    raw_1m = db.candles_1m("13", since=0)
    null_summary = run_null_scored_backtest(raw_1m, thresh)

    tables = null_summary.get("per_event_tables", {})
    assert len(tables) > 0

    for cat_name, rows in tables.items():
        for r in rows:
            # If target was hit, time to target must be >= 1 minute
            if r.get("time_to_target_20") is not None:
                assert r["time_to_target_20"] >= 1, f"Lookahead in {cat_name}: {r}"
            if r.get("time_to_target_40") is not None:
                assert r["time_to_target_40"] >= 1, f"Lookahead in {cat_name}: {r}"
            if r.get("time_to_stop_15") is not None:
                assert r["time_to_stop_15"] >= 1, f"Lookahead in {cat_name}: {r}"


def test_live_alert_gating():
    """Verify that unproven levels produce silent alerts without voice text."""
    thresh = get_thresholds()
    lvl_silent = HtfLevel(
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

    fake_null = {
        "categories": {
            "1H_UPPER_WICK": {
                "voice_armed": False,
                "best_edge_z": -0.3,
            }
        }
    }

    alerts = generate_live_alerts_and_voice([lvl_silent], ema8_val=None, current_spot=22793.0, thresholds=thresh, null_summary=fake_null)
    assert len(alerts) == 1
    assert alerts[0]["voice_armed"] is False
    assert alerts[0]["silent"] is True
    assert alerts[0]["voice_text"] == ""
    assert "Silent" in alerts[0]["title"]
    assert "No chime or voice" in alerts[0]["body"]


def test_strict_trade_simulation():
    """Verify strict stop-first trade simulation eliminates post-stop bounces and outputs audit ledger."""
    from app import db
    thresh = get_thresholds()
    raw_1m = db.candles_1m("13", since=0)
    sim = run_strict_trade_simulation(raw_1m, thresh)

    assert sim["ok"] is True
    assert "audit_report" in sim
    audit = sim["audit_report"]
    assert audit["bug_a_mfe_after_stop"]["status"] == "PRESENT_IN_OLD_CALCULATION"
    assert audit["bug_b_same_bar_ambiguity"]["status"] == "PRESENT_IN_OLD_CALCULATION"
    assert audit["bug_c_1h_ema_lookahead"]["status"] == "PRESENT_IN_OLD_CALCULATION"
    assert audit["bug_d_reference_price_offset"]["status"] == "PRESENT_IN_OLD_CALCULATION"

    ema_sim = sim["ema8_1h"]
    overall = ema_sim["overall"]
    assert overall["n"] > 50
    # Stop-first must eliminate post-stop bounces: strict win rate must be strictly less than raw MFE
    assert overall["strict_20_pct"] < overall["raw_20_pct"]
    assert overall["strict_40_pct"] < overall["raw_40_pct"]
    assert overall["stopped_pct"] > 30.0  # Conservative stop-out rate confirmed
    assert overall["exp_pts"] > 0.0      # Net positive edge (+5.7 pts)

    # Directional sub-groups
    assert ema_sim["support_bounce"]["n"] > 0
    assert ema_sim["resistance_rejection"]["n"] > 0
    assert ema_sim["support_bounce"]["strict_20_pct"] >= 50.0

    # Last 15 audit ledger verification
    ledger = ema_sim["audit_ledger_last_15"]
    assert len(ledger) == 15
    for tr in ledger:
        assert "date" in tr
        assert "time" in tr
        assert "dir" in tr
        assert "entry_price" in tr
        assert "ema_val" in tr
        assert "mfe_before_sl" in tr
        assert "hit_20_first" in tr
        assert "hit_sl_first" in tr
        assert "final_outcome" in tr


def test_api_endpoint_htf_wick_ema_rb(client):
    """Integration test verifying GET /api/desk/htf-wick-ema-rb returns ok=True, null_baseline_summary, per_event_tables, and strict_trade_simulation."""
    import app.engine.htf_wick_ema_rb as engine_mod

    # Invalidate cache
    engine_mod._cached_payload = None
    engine_mod._cached_last_ts = 0

    res = client.get("/api/desk/htf-wick-ema-rb?sec_id=13")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "ema8_1h" in data
    assert "active_levels" in data
    assert "live_alerts" in data
    assert "backtest_summary" in data
    assert "null_baseline_summary" in data
    assert "per_event_tables" in data
    assert "strict_trade_simulation" in data

    null_s = data["null_baseline_summary"]
    assert null_s["metadata"]["categories_tested"] == 10
    assert null_s["metadata"]["cells_tested"] == 20

    strict_s = data["strict_trade_simulation"]
    assert strict_s["ok"] is True
    assert "audit_report" in strict_s
    assert "ema8_1h" in strict_s
    assert "audit_ledger_last_15" in strict_s["ema8_1h"]
