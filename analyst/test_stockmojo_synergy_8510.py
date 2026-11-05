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


def test_7_crore_spread_and_auth_poller():
    print("Testing 7: Crore spread metrics, Who Is More dominance, and auth poller...")
    scraper = StockMojoScraper()

    # 1. Check Crore Spread extraction
    crore = scraper.get_crore_spread_metrics()
    assert "call_oi_chg_cr" in crore
    assert "put_oi_chg_cr" in crore
    assert "oi_chg_diff_cr" in crore
    assert "oi_chg_15m_slope" in crore
    assert "call_total_oi_cr" in crore
    assert "put_total_oi_cr" in crore
    assert "total_oi_diff_cr" in crore
    assert "who_is_more_chg" in crore
    assert "who_is_more_total" in crore
    assert "writer_dominance" in crore
    assert "auth_status" in crore
    assert crore["auth_status"]["valid_until"] == "Nov 2027"

    # 2. Check Who Is More logic on synthetic Bullish Put Surge
    scraper.oi_change_day_buffer.insert(0, {
        "time_ist": "15:40",
        "put_oi_change_day": 485.0,  # 4.85 Cr
        "call_oi_change_day": 300.0, # 3.00 Cr
        "pe_minus_ce_day": 185.0,
        "created_at": "2026-11-05 15:40:00 IST"
    })
    scraper.total_oi_buffer.insert(0, {
        "time_ist": "15:40",
        "put_oi_total": 1527.0,  # 15.27 Cr
        "call_oi_total": 1422.0, # 14.22 Cr
        "pe_ce_oi_total": 105.0,
        "created_at": "2026-11-05 15:40:00 IST"
    })
    c_bull = scraper.get_crore_spread_metrics()
    assert c_bull["call_total_oi_cr"] == 14.22
    assert c_bull["put_total_oi_cr"] == 15.27
    assert c_bull["total_oi_diff_cr"] == 1.05
    assert c_bull["who_is_more_total_side"] == "PUT"
    assert "BULLISH CROSSOVER" in c_bull["who_is_more_total"]
    assert c_bull["writer_dominance"] == "PUT_WRITERS_DOMINANT"

    # 3. Check Who Is More logic on synthetic Bearish Call Surge
    scraper.oi_change_day_buffer.insert(0, {
        "time_ist": "11:14",
        "put_oi_change_day": 473.0,  # 4.73 Cr
        "call_oi_change_day": 740.0, # 7.40 Cr
        "pe_minus_ce_day": -267.0,
        "created_at": "2026-11-05 11:14:00 IST"
    })
    scraper.total_oi_buffer.insert(0, {
        "time_ist": "11:14",
        "put_oi_total": 1521.0,  # 15.21 Cr
        "call_oi_total": 1651.0, # 16.51 Cr
        "pe_ce_oi_total": -130.0,
        "created_at": "2026-11-05 11:14:00 IST"
    })
    c_bear = scraper.get_crore_spread_metrics()
    assert c_bear["call_total_oi_cr"] == 16.51
    assert c_bear["put_total_oi_cr"] == 15.21
    assert c_bear["total_oi_diff_cr"] == -1.30
    assert c_bear["who_is_more_total_side"] == "CALL"
    assert "BEARISH BASE" in c_bear["who_is_more_total"]
    assert c_bear["writer_dominance"] == "CALL_WRITERS_DOMINANT"

    # 4. REST Endpoints for Auth and Crore Spread
    client = TestClient(app)
    r_spread = client.get("/api/stockmojo/crore_spread")
    assert r_spread.status_code == 200
    d_spread = r_spread.json()
    assert "call_oi_chg_cr" in d_spread
    assert "put_oi_chg_cr" in d_spread

    # Test update_auth endpoint live
    r_auth = client.post("/api/stockmojo/update_auth", json={
        "j": "test_j_token",
        "t": "test_t_session",
        "poll_seconds": 60,
        "vast_diff_threshold_cr": 0.50
    })
    assert r_auth.status_code == 200
    d_auth = r_auth.json()
    assert d_auth["ok"] is True
    assert d_auth["auth_summary"]["header_t"] == "test_t_session"

    # Restore authentic session headers
    real_j = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzZXNzaW9uX3Rva2VuIjoiYnRzajZrdXl2OHprbzFwN2pkejE3OTM3ODcxNjA0NDQiLCJ0aWQiOiIwM2YwMTIzOC05MDgyLTRkOWItYWVjNy1hNmMwZDQ3MDc3MzMiLCJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiaWF0IjoxNzkzNzg3MTYwLCJleHAiOjE4MjUzMjMxNjB9.wVQJZbadHctmwvPFvxfLj89nlp4XPAcTjS3NpVszczg"
    real_t = "btsj6kuyv8zko1p7jdz1793787160444"
    client.post("/api/stockmojo/update_auth", json={
        "j": real_j,
        "t": real_t,
        "payload": {"symbol": "NIFTY"},
        "poll_seconds": 60,
        "vast_diff_threshold_cr": 0.50
    })

    print("✔ Test 7 Passed: Crore spread metrics, Who Is More writer dominance, and update_auth API verified.")


def test_8_multi_strike_direct_endpoint_and_synergy_gating():
    print("Testing 8: Stock Mojo /live/multi-strike/ce-pe-oi-diff direct endpoint & synergy gating...")
    scraper = StockMojoScraper()

    # 1. Config and endpoint integrity verification
    assert "https://data.stockmojo.in/live/multi-strike/ce-pe-oi-diff" in scraper.multi_strike_url
    assert scraper.header_t == "btsj6kuyv8zko1p7jdz1793787160444"
    assert scraper.poll_interval == 60
    assert scraper.payload == {"symbol": "NIFTY"}

    # 2. Parsing multi-strike payload: Positive & expanding difference -> BULLISH_SYNERGY
    mock_bull_payload = [
        {"strike": 24000, "pe_oi": "150.0 L", "ce_oi": "50.0 L"},   # +100 L
        {"strike": 24100, "pe_oi": "120.0 L", "ce_oi": "60.0 L"},   # +60 L
        {"strike": 23900, "pe_oi": "180.0 L", "ce_oi": "40.0 L"},   # +140 L
    ]
    snap_bull = scraper.parse_multi_strike_data(mock_bull_payload)
    assert snap_bull["net_diff_lakhs"] == 300.0
    assert snap_bull["net_diff_cr"] == 3.0
    assert snap_bull["bias"] == "BULLISH_SYNERGY"
    assert scraper.multi_strike_bias == "BULLISH_SYNERGY"

    syn_bull = scraper.evaluate_synergy_status()
    assert syn_bull["synergy_status"] == "BULLISH_SYNERGY"
    assert "🟢 Stock Mojo Multi-Strike API Connected" in syn_bull["multi_strike_card_text"]
    assert "BULLISH_SYNERGY" in syn_bull["multi_strike_card_text"]

    # 3. Parsing multi-strike payload: Negative & expanding difference -> BEARISH_SYNERGY
    # Pre-seed buffer with 0 to test expanding negative slope
    scraper.multi_strike_buffer = [{"net_diff_lakhs": 0.0}]
    mock_bear_payload = [
        {"strike": 24000, "pe_oi": "40.0 L", "ce_oi": "180.0 L"},   # -140 L
        {"strike": 24100, "pe_oi": "30.0 L", "ce_oi": "150.0 L"},   # -120 L
        {"strike": 23900, "pe_oi": "50.0 L", "ce_oi": "190.0 L"},   # -140 L
    ]
    snap_bear = scraper.parse_multi_strike_data(mock_bear_payload)
    assert snap_bear["net_diff_lakhs"] == -400.0
    assert snap_bear["net_diff_cr"] == -4.0
    assert snap_bear["bias"] == "BEARISH_SYNERGY"
    assert scraper.multi_strike_bias == "BEARISH_SYNERGY"

    syn_bear = scraper.evaluate_synergy_status()
    assert syn_bear["synergy_status"] == "BEARISH_SYNERGY"
    assert "BEARISH_SYNERGY" in syn_bear["multi_strike_card_text"]

    # 4. Strategy Execution Gating (terminal8510_backend.py)
    backend = QuantTerminalBackend()
    backend.stockmojo_synergy_cfg["block_pe_when_bullish"] = True
    backend.stockmojo_synergy_cfg["block_ce_when_bearish"] = True

    # Case A: Live Multi-Strike BULLISH_SYNERGY
    backend.stockmojo_scraper.inject_multi_strike_snapshot(net_diff_lakhs=250.0, expanding=True, bias="BULLISH_SYNERGY")
    assert backend.stockmojo_scraper.evaluate_synergy_status()["synergy_status"] == "BULLISH_SYNERGY"

    # Under BULLISH_SYNERGY, PE Short MUST be blocked
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

    # Under BULLISH_SYNERGY, CE Long MUST be allowed and authorized
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
    assert res_ce["ok"] is True
    assert res_ce["status"] in ("FILLED", "PENDING_CHASE")

    # Case B: Live Multi-Strike BEARISH_SYNERGY
    backend.stockmojo_scraper.inject_multi_strike_snapshot(net_diff_lakhs=-350.0, expanding=True, bias="BEARISH_SYNERGY")
    assert backend.stockmojo_scraper.evaluate_synergy_status()["synergy_status"] == "BEARISH_SYNERGY"

    # Under BEARISH_SYNERGY, CE Long MUST be blocked
    res_ce_bear = asyncio.run(backend.execute_order_with_chaser(ce_contract, side="BUY"))
    assert res_ce_bear["ok"] is False
    assert "BLOCKED BY STOCK MOJO" in res_ce_bear["error"]
    assert "CE Long rejected due to Bearish PE-CE" in res_ce_bear["error"]

    # Under BEARISH_SYNERGY, PE Short MUST be allowed and authorized
    res_pe_bear = asyncio.run(backend.execute_order_with_chaser(pe_contract, side="BUY"))
    assert res_pe_bear["ok"] is True
    assert res_pe_bear["status"] in ("FILLED", "PENDING_CHASE")

    # 5. REST Endpoint /api/stockmojo/multi_strike
    client = TestClient(app)
    r_ms = client.get("/api/stockmojo/multi_strike")
    assert r_ms.status_code == 200
    d_ms = r_ms.json()
    assert d_ms["ok"] is True
    assert "🟢 Stock Mojo Multi-Strike API Connected" in d_ms["status_card_text"]
    assert "Last Sync:" in d_ms["status_card_text"]

    # Clean up scraper state and restore default synergy config
    backend.stockmojo_scraper.multi_strike_bias = "NEUTRAL"
    backend.stockmojo_scraper.multi_strike_connected = False
    backend.update_synergy_config({
        "filter_stage2_8ema_synergy": True,
        "filter_strategy2_reversal_synergy": True,
        "filter_strategy3_1h_synergy": True,
        "use_as_direct_15m_trigger": False,
        "block_pe_when_bullish": True,
        "block_ce_when_bearish": True,
        "show_pe_ce_table_in_tab1": True
    })

    print("✔ Test 8 Passed: /live/multi-strike/ce-pe-oi-diff parsing, expanding bias, and order gating verified 100%.")


def test_9_live_graph_extraction_and_mapping():
    print("Testing 9: Live graph extraction ([-1]), line mapping toggle, and waiting for sync state...")
    scraper = StockMojoScraper()

    # 1. Check WAITING FOR LIVE SYNC when only seed rows are present
    scraper.has_live_sync = False
    scraper.live_graph_data = {}
    metrics_wait = scraper.get_crore_spread_metrics()
    assert metrics_wait["is_live_synced"] is False
    assert metrics_wait["who_is_more_chg"] == "WAITING FOR LIVE SYNC"
    assert metrics_wait["who_is_more_total"] == "WAITING FOR LIVE SYNC"
    assert metrics_wait["call_oi_chg_cr"] is None
    assert metrics_wait["put_oi_chg_cr"] is None
    assert metrics_wait["call_total_oi_cr"] is None
    assert metrics_wait["put_total_oi_cr"] is None

    # 2. Ingest multi-candle time-series array: historical points + latest candle at 10:48 AM
    mock_graph_series = [
        # Historical candle 9:31 AM
        {
            "time_ist": "09:31",
            "oi_change_red_cr": "1.55 Cr",
            "oi_change_green_cr": "1.29 Cr",
            "total_oi_red_cr": "15.00 Cr",
            "total_oi_green_cr": "14.20 Cr"
        },
        # Historical candle 10:15 AM
        {
            "time_ist": "10:15",
            "oi_change_red_cr": "2.20 Cr",
            "oi_change_green_cr": "1.75 Cr",
            "total_oi_red_cr": "16.80 Cr",
            "total_oi_green_cr": "15.10 Cr"
        },
        # LATEST candle 10:48 AM (data[-1]) — EXACT VALUES FROM LIVE CHART
        {
            "time_ist": "10:48",
            "oi_change_red_cr": "3.02 Cr",
            "oi_change_green_cr": "2.13 Cr",
            "total_oi_red_cr": "18.33 Cr",
            "total_oi_green_cr": "16.26 Cr"
        }
    ]

    # Ingest under STANDARD mapping (Red=Put, Green=Call)
    scraper.set_line_mapping("STANDARD")
    res_ingest = scraper.ingest_live_graph_data(mock_graph_series, source="TEST_GRAPH")
    assert res_ingest["ok"] is True
    assert scraper.has_live_sync is True

    crore = scraper.get_crore_spread_metrics()
    assert crore["is_live_synced"] is True
    assert crore["time_ist"] == "10:48"
    assert crore["put_oi_chg_cr"] == 3.02
    assert crore["call_oi_chg_cr"] == 2.13
    assert crore["put_total_oi_cr"] == 18.33
    assert crore["call_total_oi_cr"] == 16.26
    assert crore["oi_chg_diff_cr"] == 0.89  # 3.02 - 2.13
    assert crore["total_oi_diff_cr"] == 2.07  # 18.33 - 16.26
    assert crore["who_is_more_chg_side"] == "PUT"
    assert "Put +0.89 Cr" in crore["who_is_more_chg"]
    assert crore["who_is_more_total_side"] == "PUT"
    assert "Put +2.07 Cr" in crore["who_is_more_total"]
    assert crore["writer_dominance"] == "PUT_WRITERS_DOMINANT"

    # 3. Test INVERTED mapping (Red=Call, Green=Put)
    scraper.set_line_mapping("INVERTED")
    crore_inv = scraper.get_crore_spread_metrics()
    assert crore_inv["line_mapping"] == "INVERTED"
    assert crore_inv["call_oi_chg_cr"] == 3.02
    assert crore_inv["put_oi_chg_cr"] == 2.13
    assert crore_inv["call_total_oi_cr"] == 18.33
    assert crore_inv["put_total_oi_cr"] == 16.26
    assert crore_inv["oi_chg_diff_cr"] == -0.89  # 2.13 - 3.02
    assert crore_inv["total_oi_diff_cr"] == -2.07  # 16.26 - 18.33
    assert crore_inv["who_is_more_chg_side"] == "CALL"
    assert "Call +0.89 Cr" in crore_inv["who_is_more_chg"]
    assert crore_inv["who_is_more_total_side"] == "CALL"
    assert "Call +2.07 Cr" in crore_inv["who_is_more_total"]
    assert crore_inv["writer_dominance"] == "CALL_WRITERS_DOMINANT"

    # Restore standard mapping
    scraper.set_line_mapping("STANDARD")

    # 4. Test REST Endpoints
    client = TestClient(app)

    # Ingest via REST
    r_ingest = client.post("/api/stockmojo/live_graph_ingest", json={
        "request_headers": {"j": "new_jwt_captured", "t": "new_t_captured"},
        "request_payload": {"symbol": "NIFTY", "timeframe": "1m"},
        "response_data": mock_graph_series
    })
    assert r_ingest.status_code == 200
    d_ingest = r_ingest.json()
    assert d_ingest["ok"] is True
    assert d_ingest["crore_spread"]["put_oi_chg_cr"] == 3.02

    # Line mapping GET/POST
    r_map = client.get("/api/stockmojo/line_mapping")
    assert r_map.status_code == 200

    r_set_map = client.post("/api/stockmojo/line_mapping", json={"mapping": "STANDARD"})
    assert r_set_map.status_code == 200
    assert r_set_map.json()["line_mapping"] == "STANDARD"

    # Relay script endpoint
    r_relay = client.get("/api/stockmojo/relay_script")
    assert r_relay.status_code == 200
    assert "Stock Mojo Live Graph & Auth Relay" in r_relay.text
    assert "127.0.0.1:8510" in r_relay.text

    print("✔ Test 9 Passed: Latest graph point extraction ([-1]), mapping inversion, WAITING state, and REST relay verified.")


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
    test_7_crore_spread_and_auth_poller()
    test_8_multi_strike_direct_endpoint_and_synergy_gating()
    test_9_live_graph_extraction_and_mapping()
    print("======================================================================")
    print("🎉 ALL 9 STOCK MOJO PE-CE SYNERGY TESTS PASSED WITH 100% PRECISION!")
    print("======================================================================")
