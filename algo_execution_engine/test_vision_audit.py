"""
algo_execution_engine/test_vision_audit.py — Unit Tests for Multi-Modal Vision Audit Agent.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from algo_execution_engine.server import app
from algo_execution_engine.vision_audit import (
    SETUP_REV_DOWN,
    SETUP_REV_UP,
    SETUP_STAGE2_8EMA,
    SETUP_STAGE3_8EMA,
    HeuristicChartParser,
    SqliteTradeMatcher,
    VisionAuditAgent,
    VisionAuditResult,
    extract_date_from_string,
    extract_time_from_string,
    normalize_setup_name,
    verify_gap_rule,
)


@pytest.fixture
def temp_homework_dir():
    """Creates a temporary directory with synthetic chart screenshots for testing."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="test_homework_"))

    # Generate 4 synthetic 15-minute Nifty chart images with different setups
    test_cases = [
        ("2026-10-24_nifty_15m_stage2_win.png", "Stage 2 8-EMA Bullish Trend (+40 pts)"),
        ("2026-10-25_nifty_15m_stage3_gap_violation.png", "Stage 3 8-EMA Gap Down Violation"),
        ("2026-10-26_nifty_15m_reversal_up.png", "8-EMA Reversal Up (LBB Bounce)"),
        ("2026-10-27_nifty_15m_reversal_down.png", "8-EMA Reversal Down (UBB Rejection)"),
    ]

    for fname, label in test_cases:
        img_path = tmp_dir / fname
        img = Image.new("RGB", (800, 600), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)
        # Draw mock candlesticks and 4/8 EMA curves
        draw.text((30, 30), f"NIFTY 50 [15m] - {label}", fill=(248, 250, 252))
        draw.line([(50, 400), (200, 350), (400, 250), (600, 180)], fill=(59, 130, 246), width=2)  # 4 EMA
        draw.line([(50, 420), (200, 370), (400, 270), (600, 200)], fill=(234, 179, 8), width=3)   # 8 EMA
        # Mock candle body
        draw.rectangle([(380, 240), (420, 290)], fill=(16, 185, 129), outline=(5, 150, 105))
        img.save(img_path)

    yield tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_setup_name_normalization():
    """Tests normalizer maps varied user setup phrases into canonical setup names."""
    assert normalize_setup_name("Stage 2 8-EMA") == SETUP_STAGE2_8EMA
    assert normalize_setup_name("stage2_8ema_long") == SETUP_STAGE2_8EMA
    assert normalize_setup_name("Stage 3 8-EMA") == SETUP_STAGE3_8EMA
    assert normalize_setup_name("stage3_short_touch") == SETUP_STAGE3_8EMA
    assert normalize_setup_name("8-EMA Reversal Up") == SETUP_REV_UP
    assert normalize_setup_name("reversal_up_lbb_bounce") == SETUP_REV_UP
    assert normalize_setup_name("8-EMA Reversal Down") == SETUP_REV_DOWN
    assert normalize_setup_name("reversal_down_ubb_rejection") == SETUP_REV_DOWN


def test_date_and_time_extraction():
    """Tests date and time regex extractors handle standard file naming patterns."""
    assert extract_date_from_string("2026-10-24_nifty_15m.png") == "2026-10-24"
    assert extract_date_from_string("nifty_homework_20261025.jpg") == "2026-10-25"
    assert extract_date_from_string("chart_26-10-2026_stage2.png") == "2026-10-26"
    assert extract_date_from_string("random_chart_name.png") is None

    assert extract_time_from_string("chart_10:15_entry.png") == "10:15"
    assert extract_time_from_string("11_45_stage3.jpg") == "11:45"
    assert extract_time_from_string("no_time.png") == "09:30"


def test_gap_rule_verification_longs_and_shorts():
    """
    Tests Trading Bible Gap Rule:
      - Flag trade if gap > +50 pts (longs) or < -50 pts (shorts) unless 8-EMA pullback is present!
    """
    # 1. Long on +65 pt gap WITHOUT pullback -> VIOLATION
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_STAGE2_8EMA,
        gap_pts=65.0,
        has_pullback_at_ema8=False,
        gap_threshold=50.0,
    )
    assert is_viol is True
    assert "GAP RULE VIOLATION" in note
    assert "+65.0 pt gap-up" in note

    # 2. Long on +65 pt gap WITH pullback -> PASSED
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_STAGE2_8EMA,
        gap_pts=65.0,
        has_pullback_at_ema8=True,
        gap_threshold=50.0,
    )
    assert is_viol is False
    assert "PASSED GAP RULE" in note

    # 3. Long on +35 pt gap (below threshold) -> PASSED
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_STAGE2_8EMA,
        gap_pts=35.0,
        has_pullback_at_ema8=False,
        gap_threshold=50.0,
    )
    assert is_viol is False
    assert "PASSED GAP RULE" in note

    # 4. Short on -75 pt gap-down WITHOUT pullback -> VIOLATION
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_STAGE3_8EMA,
        gap_pts=-75.0,
        has_pullback_at_ema8=False,
        gap_threshold=50.0,
    )
    assert is_viol is True
    assert "GAP RULE VIOLATION" in note
    assert "-75.0 pt gap-down" in note

    # 5. Short on -75 pt gap-down WITH pullback -> PASSED
    is_viol, note = verify_gap_rule(
        setup_name=SETUP_STAGE3_8EMA,
        gap_pts=-75.0,
        has_pullback_at_ema8=True,
        gap_threshold=50.0,
    )
    assert is_viol is False
    assert "PASSED GAP RULE" in note


@pytest.fixture
def mock_ground_truth_db(tmp_path):
    """Fixture providing a mock SQLite DB with ground-truth session open and previous close candles."""
    import sqlite3
    db_file = tmp_path / "mock_ground_truth.sqlite3"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE candles_1m (
            sec_id TEXT NOT NULL, ts INTEGER NOT NULL,
            o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (sec_id, ts)
        ) WITHOUT ROWID;
    """)
    # Prev day (2026-11-02 15:29 IST) close: 23900.0
    cur.execute("INSERT INTO candles_1m VALUES ('13', 1793613540, 23895.0, 23905.0, 23890.0, 23900.0, 500)")
    # Target day (2026-11-03 09:15 IST) open: 23945.0
    cur.execute("INSERT INTO candles_1m VALUES ('13', 1793677500, 23945.0, 23960.0, 23940.0, 23955.0, 600)")
    conn.commit()
    conn.close()
    return str(db_file)


def test_sqlite_ground_truth_gap_calculation(mock_ground_truth_db):
    """Tests SQLite cross-matcher calculates actual open gap for historical sessions via fixture."""
    matcher = SqliteTradeMatcher(db_path=mock_ground_truth_db)
    gap = matcher.calculate_ground_truth_gap("2026-11-03")
    assert gap is not None
    assert isinstance(gap, float)
    assert round(gap, 1) == 45.0


def test_single_image_audit_pipeline(temp_homework_dir):
    """Tests audit_single_image processes a 15m chart and returns complete VisionAuditResult."""
    agent = VisionAuditAgent()
    img_path = temp_homework_dir / "2026-10-24_nifty_15m_stage2_win.png"

    res = agent.audit_single_image(img_path)
    assert isinstance(res, VisionAuditResult)
    assert res.date == "2026-10-24"
    assert res.setup_name == SETUP_STAGE2_8EMA
    assert res.chart_image_matched == "2026-10-24_nifty_15m_stage2_win.png"
    assert res.manual_result == "WIN (+40 pts)"
    assert res.ema8_touch_state == "RESPECTED"
    assert res.actionable_rule_adjustment != ""
    assert res.discrepancy_reason != ""


def test_directory_batch_audit_and_csv_generation(temp_homework_dir):
    """
    Tests batch scanning over directory and verified export to 15m_homework_audit_summary.csv.
    Asserts all 8 required column headers match user requirements exactly:
      Date, Setup_Name, Chart_Image_Matched, Gap_Pts, Manual_Result, Algo_Result, Discrepancy_Reason, Actionable_Rule_Adjustment.
    """
    out_csv = temp_homework_dir / "test_15m_homework_audit_summary.csv"
    agent = VisionAuditAgent()

    summary = agent.audit_directory(
        directory_path=str(temp_homework_dir),
        output_csv_path=str(out_csv),
        lookback_days=90,
    )

    assert summary["ok"] is True
    assert summary["total_images_processed"] == 4
    assert out_csv.exists()

    # Read and verify CSV columns and rows
    with open(out_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        expected_fields = [
            "Date",
            "Setup_Name",
            "Chart_Image_Matched",
            "Gap_Pts",
            "Manual_Result",
            "Algo_Result",
            "Discrepancy_Reason",
            "Actionable_Rule_Adjustment",
        ]
        assert reader.fieldnames == expected_fields
        rows = list(reader)
        assert len(rows) == 4

        # Check setup variety
        setups_in_csv = {r["Setup_Name"] for r in rows}
        assert SETUP_STAGE2_8EMA in setups_in_csv
        assert SETUP_STAGE3_8EMA in setups_in_csv
        assert SETUP_REV_UP in setups_in_csv
        assert SETUP_REV_DOWN in setups_in_csv


def test_api_audit_screenshots_endpoint(temp_homework_dir):
    """Tests POST /api/algo/audit-screenshots FastAPI endpoint on Port 8500."""
    client = TestClient(app)
    payload = {
        "directory_path": str(temp_homework_dir),
        "output_csv": str(temp_homework_dir / "api_audit_summary.csv"),
        "lookback_days": 90,
    }

    res = client.post("/api/algo/audit-screenshots", json=payload)
    assert res.status_code == 200
    data = res.json()

    assert data["ok"] is True
    assert data["total_images_processed"] == 4
    assert "setup_breakdown" in data
    assert "gap_violations_count" in data
    assert "records" in data
    assert len(data["records"]) == 4

    # Verify first record schema
    rec = data["records"][0]
    assert "Date" in rec or "date" in rec
    assert "setup_name" in rec
    assert "discrepancy_reason" in rec
    assert "actionable_rule_adjustment" in rec
