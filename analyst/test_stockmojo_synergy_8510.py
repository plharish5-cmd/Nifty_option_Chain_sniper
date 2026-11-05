"""
analyst/test_stockmojo_synergy_8510.py — Comprehensive Test Suite for Stock Mojo PE-CE Dual-Entity & Synergy Gating.

Verifies:
  1. HTML table parsing for Entity 1 (Total OI) & Entity 2 (OI Change Day).
  2. Dual-entity in-memory buffers and aligned combined side-by-side view.
  3. 04 Nov Quantitative Checkpoints (10:30, 11:30, 12:30, 14:57).
  4. Synergy Gate Enforcement (BULLISH_SYNERGY blocks PE Shorts, BEARISH_SYNERGY blocks CE Longs).
  5. Master Control Card configuration persistence and telemetry injection.
  6. REST API Endpoints end-to-end via Starlette TestClient.
"""
import os
import sys
import json
import asyncio
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyst.stockmojo_scraper import StockMojoScraper, parse_indian_unit_to_lakhs, parse_html_table_rows
from analyst.terminal8510_backend import QuantTerminalBackend
from starlette.testclient import TestClient
from engine_8510_live import app


def test_1_html_parsing_and_unit_conversion():
    print("Testing 1: HTML table parser and Indian unit conversion...")
    assert parse_indian_unit_to_lakhs("2.5 Cr") == 250.0
    assert parse_indian_unit_to_lakhs("-13.5 L") == -13.5
    assert parse_indian_unit_to_lakhs("51011610") == 510.1161
    assert parse_indian_unit_to_lakhs("-360165") == -3.6016

    f1 = ROOT / "data/stockmojo_downloads/PE_CE_NIFTY_2026-11-04 (1).xls"
    f2 = ROOT / "data/stockmojo_downloads/PE_CE_NIFTY_2026-11-05.xls"

    h1, r1 = parse_html_table_rows(f1.read_text(encoding="utf-8", errors="ignore"))
    h2, r2 = parse_html_table_rows(f2.read_text(encoding="utf-8", errors="ignore"))

    expected_h1 = ['Time', 'Put OI Total', 'Put OI Change', 'Call OI Total', 'Call OI Change', 'PE-CE OI Total', 'PE-CE OI Change', 'PE-CE Bar Total', 'PE-CE Bar Change', 'PCR Total', 'Future LTP', 'Future Total OI']
    expected_h2 = ['Time', 'Put OI Change (Day)', 'Put OI Change', 'Call OI Change (Day)', 'Call OI Change', 'PE-CE OI Change (Day)', 'PE-CE OI Change', 'PE-CE Bar Change (Day)', 'PE-CE Bar Change', 'PCR Total', 'Future LTP', 'Future OI Change (Day)']

    assert h1 == expected_h1, f"Entity 1 headers mismatch: {h1}"
    assert h2 == expected_h2, f"Entity 2 headers mismatch: {h2}"
    assert len(r1) == 129, f"Entity 1 row count mismatch: {len(r1)}"
    assert len(r2) == 130, f"Entity 2 row count mismatch: {len(r2)}"
    print("✔ Test 1 Passed: Both HTML .xls files parse exact 12 reference columns and rows.")


def test_2_dual_entity_buffers_and_combined_view():
    print("Testing 2: Dual-entity in-memory buffers and aligned combined side-by-side view...")
    scraper = StockMojoScraper()
    assert len(scraper.total_oi_buffer) >= 120
    assert len(scraper.oi_change_day_buffer) >= 120

    dual = scraper.get_dual_tables(limit=60)
    assert dual["ok"] is True
    assert len(dual["total_oi_rows"]) > 0
    assert len(dual["oi_change_day_rows"]) > 0
    assert len(dual["combined_rows"]) > 0

    first_comb = dual["combined_rows"][0]
    assert "pe_ce_total_cr" in first_comb
    assert "pe_ce_day_cr" in first_comb
    assert "synergy_tag" in first_comb
    print("✔ Test 2 Passed: Dual buffers and combined side-by-side view constructed properly.")


def test_3_04nov_checkpoint_audit_accuracy():
    print("Testing 3: 04 Nov Checkpoint Audit data accuracy...")
    scraper = StockMojoScraper()
    audit = scraper.get_04nov_audit_data()
    assert audit["ok"] is True
    assert len(audit["checkpoints"]) == 4

    cps = {cp["time"]: cp for cp in audit["checkpoints"]}
    assert "10:30" in cps
    assert "11:30" in cps
    assert "12:30" in cps
    assert "14:57" in cps

    # 10:30 Checkpoint Audit
    cp1030 = cps["10:30"]
    assert cp1030["synergy_verdict"] == "STRONG BEARISH 🔴🔴"
    assert "BLOCKED" in cp1030["filter_action"]

    # 11:30 Checkpoint Audit
    cp1130 = cps["11:30"]
    assert cp1130["synergy_verdict"] == "STRONG BEARISH 🔴🔴"
    assert "PE Short ALLOWED" in cp1130["filter_action"]

    # 12:30 Checkpoint Audit
    cp1230 = cps["12:30"]
    assert "BULLISH" in cp1230["synergy_verdict"]
    assert "ALLOWED" in cp1230["filter_action"]

    # 14:57 Checkpoint Audit
    cp1457 = cps["14:57"]
    assert "TRIPLE CONFIRMATION" in cp1457["synergy_verdict"]
    print("✔ Test 3 Passed: 04 Nov audit checkpoints match quantitative ground truth.")


def test_4_synergy_gate_enforcement():
    print("Testing 4: Synergy gate order execution and signal filtering...")
    backend = QuantTerminalBackend()
    backend.stockmojo_synergy_cfg["block_pe_when_bullish"] = True
    backend.stockmojo_synergy_cfg["block_ce_when_bearish"] = True

    # Case A: Force BULLISH_SYNERGY state
    backend.stockmojo_scraper.oi_change_day_buffer.insert(0, {
        "time_ist": "15:45",
        "put_oi_change_day": 150.0,
        "put_oi_change_interval": 12.0,
        "call_oi_change_day": -20.0,
        "call_oi_change_interval": -8.0,
        "pe_minus_ce_day": 170.0,
        "pe_minus_ce_interval": 20.0,
        "pcr_total": 1.25,
        "future_ltp": 23980.0,
        "future_oi_change_day": 5.0,
        "writer_signal": "BULLISH_PUT_SURGE",
        "signal_hint": "BUY_CE_BIAS",
        "created_at": "2026-11-05 15:45:00 IST",
    })
    backend.stockmojo_scraper.total_oi_buffer.insert(0, {
        "time_ist": "15:45",
        "put_oi_total": 600.0,
        "put_oi_change": 15.0,
        "call_oi_total": 450.0,
        "call_oi_change": -5.0,
        "pe_ce_oi_total": 150.0,
        "pe_ce_oi_change": 20.0,
        "pe_ce_bar_total": 150.0,
        "pe_ce_bar_change": 20.0,
        "pcr_total": 1.25,
        "future_ltp": 23980.0,
        "future_total_oi": 140.0,
        "created_at": "2026-11-05 15:45:00 IST",
    })

    syn = backend.stockmojo_scraper.evaluate_synergy_status()
    assert syn["synergy_status"] == "BULLISH_SYNERGY"

    # Attempt PE order under BULLISH_SYNERGY -> MUST BE BLOCKED
    pe_contract = {
        "id": "LONG_PE_ITM",
        "symbol": "NIFTY-24000-PE",
        "opt_type": "PE",
        "security_id": "40001",
        "lot_size": 65,
        "ltp": 180.0,
        "delta": -0.60
    }
    res_pe = asyncio.run(backend.execute_order_with_chaser(pe_contract, side="BUY"))
    assert res_pe["ok"] is False
    assert "BLOCKED BY STOCK MOJO" in res_pe["error"]
    assert "PE Short rejected due to Bullish PE-CE" in res_pe["error"]

    # Case B: Force BEARISH_SYNERGY state
    backend.stockmojo_scraper.oi_change_day_buffer.insert(0, {
        "time_ist": "15:46",
        "put_oi_change_day": -50.0,
        "put_oi_change_interval": -15.0,
        "call_oi_change_day": 200.0,
        "call_oi_change_interval": 25.0,
        "pe_minus_ce_day": -250.0,
        "pe_minus_ce_interval": -40.0,
        "pcr_total": 0.55,
        "future_ltp": 23720.0,
        "future_oi_change_day": -5.0,
        "writer_signal": "BEARISH_CALL_SURGE",
        "signal_hint": "BUY_PE_BIAS",
        "created_at": "2026-11-05 15:46:00 IST",
    })
    backend.stockmojo_scraper.total_oi_buffer.insert(0, {
        "time_ist": "15:46",
        "put_oi_total": 350.0,
        "put_oi_change": -15.0,
        "call_oi_total": 650.0,
        "call_oi_change": 25.0,
        "pe_ce_oi_total": -300.0,
        "pe_ce_oi_change": -40.0,
        "pe_ce_bar_total": -300.0,
        "pe_ce_bar_change": -40.0,
        "pcr_total": 0.55,
        "future_ltp": 23720.0,
        "future_total_oi": 150.0,
        "created_at": "2026-11-05 15:46:00 IST",
    })

    syn_bear = backend.stockmojo_scraper.evaluate_synergy_status()
    assert syn_bear["synergy_status"] == "BEARISH_SYNERGY"

    # Attempt CE order under BEARISH_SYNERGY -> MUST BE BLOCKED
    ce_contract = {
        "id": "LONG_CE_ITM",
        "symbol": "NIFTY-23700-CE",
        "opt_type": "CE",
        "security_id": "39999",
        "lot_size": 65,
        "ltp": 195.0,
        "delta": 0.60
    }
    res_ce = asyncio.run(backend.execute_order_with_chaser(ce_contract, side="BUY"))
    assert res_ce["ok"] is False
    assert "BLOCKED BY STOCK MOJO" in res_ce["error"]
    assert "CE Long rejected due to Bearish PE-CE" in res_ce["error"]
    print("✔ Test 4 Passed: Order execution properly rejects counter-synergy trades with exact log messages.")


def test_5_master_config_and_telemetry():
    print("Testing 5: Master control card configuration and telemetry injection...")
    backend = QuantTerminalBackend()
    cfg = backend.get_synergy_config()
    assert "filter_stage2_8ema_synergy" in cfg
    assert "filter_strategy2_reversal_synergy" in cfg
    assert "filter_strategy3_1h_synergy" in cfg
    assert "use_as_direct_15m_trigger" in cfg
    assert "block_pe_when_bullish" in cfg
    assert "block_ce_when_bearish" in cfg

    # Update config
    res = backend.update_synergy_config({"use_as_direct_15m_trigger": True})
    assert res["ok"] is True
    assert backend.get_synergy_config()["use_as_direct_15m_trigger"] is True

    # Revert back to default
    backend.update_synergy_config({"use_as_direct_15m_trigger": False})

    # Telemetry test
    telem = asyncio.run(backend.get_live_telemetry())
    assert "stockmojo_synergy" in telem
    assert "stockmojo_synergy_config" in telem
    print("✔ Test 5 Passed: Configuration getters/setters and telemetry injection verified.")


def test_6_api_endpoints():
    print("Testing 6: Live REST API endpoints end-to-end...")
    client = TestClient(app)

    # 1. Dual tables endpoint
    r_dual = client.get("/api/stockmojo/pe_ce_dual?limit=10")
    assert r_dual.status_code == 200
    d_dual = r_dual.json()
    assert d_dual["ok"] is True
    assert len(d_dual["total_oi_rows"]) > 0
    assert len(d_dual["oi_change_day_rows"]) > 0

    # 2. Synergy status endpoint
    r_syn = client.get("/api/stockmojo/synergy_status")
    assert r_syn.status_code == 200
    d_syn = r_syn.json()
    assert "synergy_status" in d_syn
    assert "total_oi_summary" in d_syn
    assert "oi_change_day_summary" in d_syn

    # 3. Synergy config endpoints
    r_cfg = client.get("/api/stockmojo/synergy_config")
    assert r_cfg.status_code == 200

    r_post = client.post("/api/stockmojo/synergy_config", json={"block_pe_when_bullish": True})
    assert r_post.status_code == 200

    # 4. 04 Nov audit endpoint
    r_audit = client.get("/api/stockmojo/audit_04nov")
    assert r_audit.status_code == 200
    d_audit = r_audit.json()
    assert d_audit["ok"] is True
    assert len(d_audit["checkpoints"]) == 4

    print("✔ Test 6 Passed: All Stock Mojo REST API endpoints return HTTP 200 with complete datasets.")


if __name__ == "__main__":
    print("======================================================================")
    print("Running Stock Mojo Dual-Entity PE-CE & Synergy Gate Test Suite (v1.3)...")
    print("======================================================================")
    test_1_html_parsing_and_unit_conversion()
    test_2_dual_entity_buffers_and_combined_view()
    test_3_04nov_checkpoint_audit_accuracy()
    test_4_synergy_gate_enforcement()
    test_5_master_config_and_telemetry()
    test_6_api_endpoints()
    print("======================================================================")
    print("🎉 ALL 6 STOCK MOJO PE-CE SYNERGY TESTS PASSED WITH 100% PRECISION!")
    print("======================================================================")
