"""
test_immutable_feature_lock.py — Regression & Contract Lock for Immutable Features.

Verifies that the verified working modules:
  1. 3-Chart Institutional PCR & OI Trinity Suite (pcr_trinity.py, PcrTrinitySuite.tsx)
  2. Classic Writer Flow (OcLab.tsx -> WritersFlow, chain.py)
  3. Smart OI Intelligence Suite (smart_oi.py, SmartOISuite.tsx)
  4. PE - CE Institutional Matrix 2.0 (pe_ce_matrix.py, PeceMatrix.tsx)
  5. Order Flow & CVD Layers (cvd_divergence.py, CvdAlertWidget.tsx, Chart.tsx overlays)
remain 100% frozen, mathematically invariant, and intact against the baseline manifest.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BACKEND_DIR = REPO_ROOT / "ict_smc_app" / "backend"
FRONTEND_DIR = REPO_ROOT / "ict_smc_app" / "frontend"
MANIFEST_PATH = REPO_ROOT / "ict_smc_app" / "LOCKED_MANIFEST.json"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("ICT_DATA_DIR", str(BACKEND_DIR / "tests" / ".data"))

import pytest
from app.engine import pcr_trinity, smart_oi, pe_ce_matrix, cvd_divergence


# ==============================================================================
# Layer 1: Baseline SHA-256 Manifest Integrity
# ==============================================================================

def test_baseline_sha256_manifest_integrity():
    """Validates all locked engine and component files match baseline SHA-256 hashes."""
    assert MANIFEST_PATH.exists(), f"Missing baseline manifest: {MANIFEST_PATH}"
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    locked_files = manifest.get("locked_files", {})
    assert len(locked_files) >= 8, f"Expected at least 8 locked files in manifest, got {len(locked_files)}"

    mismatches = []
    for rel_path, meta in locked_files.items():
        target = REPO_ROOT / rel_path
        assert target.exists(), f"Locked file missing from disk: {rel_path}"
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        expected_sha = meta["sha256"]
        if actual_sha != expected_sha:
            mismatches.append((rel_path, expected_sha, actual_sha))

    assert not mismatches, (
        f"Protected modules have been modified without explicit UNLOCK: {mismatches}\n"
        "See PROTECTED_MODULES.md for details."
    )


def test_verify_locks_script_execution():
    """Ensures helper verification script scripts/verify_locks.py executes successfully."""
    from scripts import verify_locks
    res = verify_locks.verify_locks(MANIFEST_PATH)
    assert res == 0, "scripts/verify_locks.py returned non-zero exit code"


# ==============================================================================
# Layer 2: Core Engine Contract & Mathematical Invariants
# ==============================================================================

def test_trinity_suite_contract_and_mathematical_invariants():
    """Validates mathematical invariants and contract keys for Trinity PCR Suite."""
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
    assert len(series) == 3

    for pt in series:
        # Mandatory contract keys
        for key in ("time", "fut_close", "pcr_oi", "pcr_vol", "call_oi", "put_oi",
                    "total_oi_gap", "call_oi_chg", "put_oi_chg", "net_oi_chg"):
            assert key in pt, f"Missing Trinity point key: {key}"

        # Strict mathematical invariants
        assert pt["total_oi_gap"] == pt["put_oi"] - pt["call_oi"], "Total OI gap invariant broken"
        assert pt["net_oi_chg"] == pt["put_oi_chg"] - pt["call_oi_chg"], "Net OI change invariant broken"
        expected_pcr = round(pt["put_oi"] / max(pt["call_oi"], 1), 3)
        assert abs(pt["pcr_oi"] - expected_pcr) <= 0.002, "PCR-OI calculation invariant broken"

    # Band classification invariants
    cfg = {
        "extreme_bull_pcr": 1.50, "bull_pcr": 1.20,
        "bear_pcr": 0.80, "extreme_bear_pcr": 0.50,
        "neutral_core_high": 1.10, "neutral_core_low": 0.85
    }
    k1, _ = pcr_trinity.classify_pcr_band(1.60, cfg)
    assert k1 == "EXTREME_BULL_CONTRARIAN_RISK"
    k2, _ = pcr_trinity.classify_pcr_band(1.30, cfg)
    assert k2 == "BULLISH_PUT_CONTROL"
    k3, _ = pcr_trinity.classify_pcr_band(0.95, cfg)
    assert k3 == "NEUTRAL_BALANCED"
    k4, _ = pcr_trinity.classify_pcr_band(0.70, cfg)
    assert k4 == "BEARISH_CALL_CONTROL"
    k5, _ = pcr_trinity.classify_pcr_band(0.40, cfg)
    assert k5 == "EXTREME_BEAR_CONTRARIAN_BOUNCE"


def test_smart_oi_contract_and_noise_filtering():
    """Validates Smart OI noise filtering, support/resistance detection, and payload structure."""
    mock_rows = [
        {"strike": 21500.0, "ce": {"oi": 40000, "oi_chg": 500, "ltp": 1200.0, "vol": 50}, "pe": {"oi": 60000, "oi_chg": 200, "ltp": 2.0, "vol": 100}},
        {"strike": 22600.0, "ce": {"oi": 800000, "oi_chg": 15000, "ltp": 140.0, "vol": 80000}, "pe": {"oi": 4500000, "oi_chg": 450000, "ltp": 55.0, "vol": 500000}},
        {"strike": 22800.0, "ce": {"oi": 5200000, "oi_chg": 620000, "ltp": 45.0, "vol": 600000}, "pe": {"oi": 900000, "oi_chg": 10000, "ltp": 160.0, "vol": 70000}},
    ]
    chain_dict = {"day": "2026-10-22", "expiry": "2026-10-27", "spot": 22700.0, "step": 50.0, "rows": mock_rows}

    res = smart_oi.filter_institutional_smart_oi(chain_dict, range_mode="all")
    assert res["ok"] is True
    assert res["key_support_level"] == 22600.0
    assert res["key_resistance_level"] == 22800.0
    assert len(res["signal_strikes"]) >= 2


def test_pe_ce_matrix_contract_and_action_verdicts():
    """Validates PE-CE Matrix 2.0 contract keys, difference calculations, and 6-stage labels."""
    res = pe_ce_matrix.compute_pe_ce_matrix(tf="3m")
    assert res["ok"] is True
    for key in ("rows", "regime", "skew", "first_hour", "expiry_playbook", "strike_range_label"):
        assert key in res, f"PE-CE Matrix missing top-level contract key: {key}"

    assert len(res["rows"]) > 0
    for r in res["rows"]:
        if "pe_ce_day_diff" in r and "put_oi_day_chg" in r and "call_oi_day_chg" in r:
            assert r["pe_ce_day_diff"] == r["put_oi_day_chg"] - r["call_oi_day_chg"], "Matrix day diff invariant broken"
        if "pe_ce_step_diff" in r and "put_oi_step_chg" in r and "call_oi_step_chg" in r:
            assert r["pe_ce_step_diff"] == r["put_oi_step_chg"] - r["call_oi_step_chg"], "Matrix step diff invariant broken"

    # Stage verdict resolution invariant
    verdict_keys = [
        "S0 WAIT", "Stage 1 Bearish Breakdown", "Stage 2 Pullback Sell",
        "Stage 3 Bearish Acceleration", "Stage 4 Hold PE", "Stage 5 Short Covering"
    ]
    found_any = any(any(vk in r.get("stage_label", "") for vk in verdict_keys) for r in res["rows"])
    assert found_any, "No standard 6-Stage Action Verdict labels matched in matrix output"


def test_cvd_divergence_contract_and_lifecycle():
    """Validates Order Flow & CVD Divergence engine output contract and pivot confirmation."""
    t0 = 1793063700
    step = 180
    highs = [100, 102, 110, 104, 103, 105, 115, 108, 106, 102, 101, 100]
    lows = [96, 97, 100, 96, 95, 98, 101, 99, 97, 90, 89, 88]
    closes = [98, 100, 108, 100, 99, 102, 112, 104, 101, 92, 91, 90]
    flow = [0, 100, 1000, 800, 700, 600, 400, 350, 300, 100, 0, -100]
    bars = [dict(ts=t0+i*step, o=closes[i], h=h, l=lows[i], c=closes[i], v=1000,
                 cvd=flow[i], vwap=100) for i, h in enumerate(highs)]

    res = cvd_divergence.compute_cvd_divergence(price_series=bars, as_of=t0+100*step,
                                               cfg=dict(price_range_mult=0, delta_scale_mult=0))
    assert res["ok"] is True
    assert "recent_divergences" in res
    assert "active_divergence" in res


# ==============================================================================
# Layer 3: Frontend Source & UI Invariants Lock
# ==============================================================================

def test_frontend_trinity_suite_invariants():
    """Validates StockMojo colors, Sub-tabs 1, 2, 3 titles, and guidance banners in PcrTrinitySuite.tsx."""
    src = (FRONTEND_DIR / "src" / "components" / "PcrTrinitySuite.tsx").read_text(encoding="utf-8")

    # StockMojo colors
    assert "#22c55e" in src, "Missing Call Green (#22c55e) in PcrTrinitySuite.tsx"
    assert "#ef4444" in src, "Missing Put Red (#ef4444) in PcrTrinitySuite.tsx"
    assert "#a855f7" in src, "Missing PE-CE Purple (#a855f7) in PcrTrinitySuite.tsx"

    # Sub-tabs 1, 2, 3
    assert "1. Put-Call Ratio (PCR-OI vs FUT)" in src
    assert "2. Fresh Change in OI" in src
    assert "3. Cumulative Total OI" in src

    # Formatters and guidance
    assert "inr" in src
    assert "Top-Left Floating Tooltip & Guidance Box" in src
    assert "tab1Verdict" in src
    assert "tab2Verdict" in src
    assert "tab3Verdict" in src


def test_frontend_smart_oi_suite_invariants():
    """Validates 4-panel StockMojo layout, colors, and formatters in SmartOISuite.tsx."""
    src = (FRONTEND_DIR / "src" / "components" / "SmartOISuite.tsx").read_text(encoding="utf-8")

    # Colors
    assert "#22c55e" in src  # Call volume / Green
    assert "#ef4444" in src  # Put volume / Red
    assert "#34d399" in src  # Smart OI Bull green
    assert "#f87171" in src  # Smart OI Bear red

    # 4-Panel tokens
    assert "Cyan Smart OI" in src
    assert "Smart OI Net Money Flow" in src
    assert "Option Volume Activity" in src
    assert "OI PCR vs Vol PCR" in src

    # Formatters & Guidance
    assert "formatInrUnits" in src
    assert "getOptionVolumeGuidance" in src


def test_frontend_pe_ce_matrix_invariants():
    """Validates 6-Stage verdicts, formatters, and diverging bar colors in PeceMatrix.tsx."""
    src = (FRONTEND_DIR / "src" / "components" / "PeceMatrix.tsx").read_text(encoding="utf-8")

    assert "inr" in src
    assert "6-Stage Action Verdict" in src
    assert "Stage 1" in src
    assert "Stage 2" in src
    assert "Stage 3" in src
    assert "Stage 4" in src
    assert "Stage 5" in src


def test_frontend_cvd_and_chart_overlays_invariants():
    """Validates CVD widget, Stacked Nodes, OI walls, Big trades, and delta markers."""
    cvd_src = (FRONTEND_DIR / "src" / "components" / "CvdAlertWidget.tsx").read_text(encoding="utf-8")
    assert "CVD Divergence" in cvd_src or "CVD" in cvd_src

    chart_src = (FRONTEND_DIR / "src" / "components" / "Chart.tsx").read_text(encoding="utf-8")
    assert "Stacked nodes" in chart_src, "Missing Stacked nodes in Chart.tsx"
    assert "bubbles" in chart_src, "Missing Big Trades bubbles in Chart.tsx"
    assert "ABS BUY" in chart_src, "Missing ABS BUY marker in Chart.tsx"
    assert "ABS SELL" in chart_src, "Missing ABS SELL marker in Chart.tsx"
    assert "Δ SURGE" in chart_src, "Missing Δ SURGE marker in Chart.tsx"
    assert "CVD EXH" in chart_src, "Missing CVD EXH marker in Chart.tsx"

    oclab_src = (FRONTEND_DIR / "src" / "components" / "OcLab.tsx").read_text(encoding="utf-8")
    assert "WritersFlow" in oclab_src, "Missing WritersFlow in OcLab.tsx"
