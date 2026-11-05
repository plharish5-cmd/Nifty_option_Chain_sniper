#!/usr/bin/env python3
"""
engine_8510_live.py — NIFTY Quant Terminal Port 8510 Live Server.

Endpoints:
  - GET/HEAD /                 : Serves Algorithmic_Trading_Workspace_8510.html (no-store)
  - GET  /api/health           : Daemon status
  - GET  /api/telemetry        : Live 1-second tick telemetry & trend confluence
  - GET  /api/funds            : Live Dhan funds + Paper wallet
  - POST /api/funds/paper/update: Add / Decrease / Reset Paper funds
  - POST /api/mode             : 3-way mode switch + Paper ON/OFF toggle
  - POST /api/kill             : Emergency Kill Switch
  - POST /api/kill/reset       : Disarm Kill Switch
  - POST /api/chaser/config    : Configure aggressive limit chaser
  - POST /api/order/submit     : Aggressive Limit Chaser order submission
  - POST /api/position/modify  : In-flight SL trail, target widen, or square off
  - GET  /api/orders           : Institutional Orders Book
  - GET  /api/positions        : Real-time fluctuating Positions
  - GET  /api/replay/dates     : Available historical replay dates
  - POST /api/replay/run       : Run historical simulation replay
  - GET  /api/stockmojo/matrix : StockMojo PE-CE confluence snapshot
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# Path resolution
ROOT_DIR = Path(__file__).resolve().parent
ROOT = ROOT_DIR
sys.path.insert(0, str(ROOT_DIR))

from analyst.terminal8510_backend import QuantTerminalBackend, load_env_safe

load_env_safe()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] 8510_engine: %(message)s"
)
log = logging.getLogger("8510_engine")

app = FastAPI(title="NIFTY Quant Terminal — Port 8510", version="8.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8510",
        "http://127.0.0.1:8510",
        "http://localhost:8500",
        "http://127.0.0.1:8500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

backend = QuantTerminalBackend()


@app.on_event("startup")
async def startup_event():
    log.info("⚡ NIFTY Quant Terminal 8510 Engine starting up...")
    backend.inst_mgr.ensure_loaded()
    try:
        await backend.get_live_telemetry()
        log.info("✔ Initial market telemetry loaded successfully.")
    except Exception as e:
        log.error("Telemetry startup error: %s", e)

    # Dhan connection auto-load & 10-second timeout check
    if backend.broker.is_configured():
        log.info("Auto-loaded saved Dhan API — No need to re-enter.")
        try:
            conn_res = await asyncio.wait_for(backend.test_dhan_connection(), timeout=10.0)
            if conn_res.get("ok"):
                log.info("✔ Dhan connection verified on startup: %s", conn_res.get("message"))
            else:
                log.warning("⚠ Dhan connection test on startup returned: %s", conn_res.get("message"))
        except asyncio.TimeoutError:
            log.warning("❌ Dhan connection check timed out after 10s on startup")
        except Exception as e:
            log.warning("⚠ Dhan connection check exception on startup: %s", e)

    backend.scanner_task = asyncio.create_task(backend.start_strategy_scanner_loop())
    log.info("✔ 4-Strategy Live Scanner daemon launched.")

    # Start 60s background polling daemon for Stock Mojo live direct OI and folder watcher
    backend.stockmojo_scraper.start_background_polling()
    log.info("✔ Stock Mojo 60s Live OI & Multi-Strike Polling daemon launched.")


@app.get("/")
@app.head("/")
async def serve_workspace_html():
    """Serves Algorithmic_Trading_Workspace_8510.html with absolute path resolution and no-store headers."""
    html_file = ROOT_DIR / "Algorithmic_Trading_Workspace_8510.html"
    if not html_file.exists():
        html_file = Path(__file__).resolve().parent / "Algorithmic_Trading_Workspace_8510.html"

    if not html_file.exists():
        return HTMLResponse(
            content=f"<h1>Error: Algorithmic_Trading_Workspace_8510.html not found at {html_file}</h1>",
            status_code=404
        )

    content = html_file.read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.get("/api/health")
async def api_health():
    m_status = backend.is_nse_market_open()
    return JSONResponse(content={
        "status": "ONLINE",
        "port": 8510,
        "dhan_connected": backend.broker.is_configured(),
        "execution_mode": backend.execution_mode,
        "paper_enabled": backend.paper_execution_enabled,
        "directional_bias": backend.directional_bias,
        "kill_switch_active": backend.kill_switch_active,
        "market": m_status,
        "allow_off_hours_paper_testing": backend.allow_off_hours_paper_testing,
    })


@app.get("/api/market/status")
async def api_market_status():
    return JSONResponse(content=backend.is_nse_market_open())


@app.post("/api/settings/testing_override")
async def api_settings_testing_override(req: Request):
    body = await req.json()
    enabled = bool(body.get("allow_off_hours_paper_testing", False))
    res = backend.set_off_hours_paper_testing(enabled)
    return JSONResponse(content=res)


@app.get("/api/bias")
@app.get("/api/directional_bias")
async def api_get_bias():
    return JSONResponse(content={
        "ok": True,
        "directional_bias": backend.directional_bias,
    })


@app.post("/api/bias")
@app.post("/api/directional_bias")
async def api_set_bias(req: Request):
    body = await req.json()
    bias = str(body.get("directional_bias") or body.get("bias", "")).upper()
    if bias in ("LONG_ONLY", "SHORT_ONLY", "BOTH"):
        res = backend.set_directional_bias(bias)
        return JSONResponse(content=res)
    return JSONResponse(
        content={"ok": False, "error": f"Invalid bias: {bias}. Must be LONG_ONLY, SHORT_ONLY, or BOTH."},
        status_code=400
    )


@app.get("/api/telemetry")
async def api_telemetry():
    """1-second ticking telemetry stream."""
    data = await backend.get_live_telemetry()
    return JSONResponse(content=data)


@app.get("/api/funds")
async def api_funds():
    paper = backend.get_paper_wallet()
    live = await backend.broker.get_fundlimit()
    return JSONResponse(content={
        "paper": paper,
        "live": live,
    })


@app.post("/api/funds/paper/update")
async def api_paper_funds_update(req: Request):
    body = await req.json()
    delta = float(body.get("delta", 0.0))
    reset = bool(body.get("reset", False))
    res = backend.update_paper_wallet(delta_amount=delta, reset=reset)
    return JSONResponse(content=res)


@app.post("/api/mode")
async def api_set_mode(req: Request):
    body = await req.json()
    if "mode" in body:
        new_mode = str(body["mode"])
        if new_mode in ("ALGO_LIVE_ONLY", "PAPER_ONLY", "BOTH"):
            backend.execution_mode = new_mode
            log.info("Execution mode updated to %s", new_mode)
    if "paper_enabled" in body:
        backend.paper_execution_enabled = bool(body["paper_enabled"])
        log.info("Paper execution enabled updated to %s", backend.paper_execution_enabled)
    if "directional_bias" in body or "bias" in body:
        bias_val = str(body.get("directional_bias") or body.get("bias", "")).upper()
        if bias_val in ("LONG_ONLY", "SHORT_ONLY", "BOTH"):
            backend.set_directional_bias(bias_val)
    return JSONResponse(content={
        "ok": True,
        "execution_mode": backend.execution_mode,
        "paper_execution_enabled": backend.paper_execution_enabled,
        "directional_bias": backend.directional_bias,
    })


@app.post("/api/kill")
async def api_kill_switch():
    """Emergency Kill Switch."""
    res = await backend.trigger_emergency_kill_switch()
    return JSONResponse(content=res)


@app.post("/api/kill/reset")
async def api_reset_kill():
    """Disarms Kill Switch."""
    res = backend.reset_kill_switch()
    return JSONResponse(content=res)


@app.get("/api/desk/alarm-status")
async def api_desk_alarm_status():
    """Returns current pre-open check and alarm status from data/desk_alarm.json."""
    alarm_file = ROOT / "data" / "desk_alarm.json"
    if alarm_file.exists():
        try:
            return JSONResponse(content=json.loads(alarm_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse(content={"status": "UNKNOWN", "message": "Pre-open alarm not yet triggered"})


@app.post("/api/desk/pre-open-check")
async def api_run_pre_open_check():
    """Runs 08:55 IST Pre-Open Health Check on demand."""
    from scripts.pre_open_alarm import run_pre_open_check
    res = run_pre_open_check()
    return JSONResponse(content=res)


@app.get("/api/candles")
async def api_candles(tf: str = "5m", days: int = 1, inst: str = "NIFTY"):
    """Heartbeat-enabled candle endpoint for chart monitoring and stale checks."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://127.0.0.1:8600/api/candles?tf={tf}&days={days}&inst={inst}")
            if resp.status_code == 200:
                return JSONResponse(content=resp.json())
    except Exception:
        pass

    spot = backend.cached_telemetry.get("spot", {}).get("price", 24000.0)
    now_ts = int(time.time())
    candle = {
        "ts": now_ts - (now_ts % 300),
        "open": spot,
        "high": spot,
        "low": spot,
        "close": spot,
        "volume": 0,
        "tf": tf,
        "source": "8510_live_telemetry",
    }
    return JSONResponse(content={"candles": [candle], "inst": inst, "tf": tf, "ok": True})


@app.post("/api/chaser/config")
async def api_chaser_config(req: Request):
    body = await req.json()
    if "aggressive_limit_buffer_pts" in body:
        backend.chasing_cfg["aggressive_limit_buffer_pts"] = float(body["aggressive_limit_buffer_pts"])
    if "auto_modify_interval_ms" in body:
        backend.chasing_cfg["auto_modify_interval_ms"] = int(body["auto_modify_interval_ms"])
    if "max_chase_retries" in body:
        backend.chasing_cfg["max_chase_retries"] = int(body["max_chase_retries"])
    if "sl_limit_buffer_pts" in body:
        backend.chasing_cfg["sl_limit_buffer_pts"] = float(body["sl_limit_buffer_pts"])
    return JSONResponse(content={"ok": True, "chasing_cfg": backend.chasing_cfg})


@app.post("/api/order/submit")
@app.post("/api/orders/place")
@app.post("/api/paper/order")
async def api_order_submit(req: Request):
    """Places Aggressive Limit order with Chasing Engine."""
    body = await req.json()
    contract_id = body.get("contract_id")
    side = body.get("side", "BUY")
    strategy_name = body.get("strategy_name", "Manual Matrix")
    target_pts = float(body.get("target_pts", 40.0))
    sl_pts = float(body.get("sl_pts", 40.0))
    is_algo_trade = bool(body.get("is_algo_trade", False))
    source = body.get("source") or body.get("placed_by")
    strategy_setup = body.get("strategy_setup")

    selected_contract = None
    if contract_id:
        for c in backend.priority_contracts:
            if c.get("id") == contract_id:
                selected_contract = c
                break

    if not selected_contract:
        selected_contract = body.get("contract")

    if not selected_contract:
        return JSONResponse(content={"ok": False, "error": f"Contract not resolved for id {contract_id}"}, status_code=400)

    # Directional Bias Enforcement Gate (Live Execution / Strike Matrix)
    opt_type = str(selected_contract.get("opt_type", "")).upper()
    cid = str(selected_contract.get("id", "")).upper()
    sym = str(selected_contract.get("symbol", "")).upper()
    is_pe = (opt_type == "PE" or "PE" in cid or "PE" in sym)
    is_ce = (opt_type == "CE" or "CE" in cid or "CE" in sym)

    if backend.directional_bias == "LONG_ONLY" and is_pe:
        err_msg = f"[DIRECTIONAL BIAS REJECTED] PE / Short trade for {selected_contract.get('symbol', cid)} blocked by LONG_ONLY bias filter."
        log.warning(err_msg)
        return JSONResponse(
            content={"ok": False, "status": "REJECTED", "error": err_msg, "rejection_reason": "Directional Bias: LONG_ONLY blocks all PE / Short trades"},
            status_code=400
        )

    if backend.directional_bias == "SHORT_ONLY" and is_ce:
        err_msg = f"[DIRECTIONAL BIAS REJECTED] CE / Long trade for {selected_contract.get('symbol', cid)} blocked by SHORT_ONLY bias filter."
        log.warning(err_msg)
        return JSONResponse(
            content={"ok": False, "status": "REJECTED", "error": err_msg, "rejection_reason": "Directional Bias: SHORT_ONLY blocks all CE / Long trades"},
            status_code=400
        )

    res = await backend.execute_order_with_chaser(
        contract=selected_contract,
        side=side,
        strategy_name=strategy_name,
        target_pts=target_pts,
        sl_pts=sl_pts,
        is_algo_trade=is_algo_trade,
        source=source,
        strategy_setup=strategy_setup,
        enforce_market_hours=True,
    )
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(content=res, status_code=status_code)


@app.post("/api/super_trail/config")
async def api_super_trail_config(req: Request):
    """Updates Super Trail parameters mid-trade without restart."""
    body = await req.json()
    res = backend.update_super_trail_config(body)
    return JSONResponse(content=res)


@app.post("/api/strike_matrix/selection")
async def api_strike_matrix_selection(req: Request):
    """Updates and persists Algo Trade strike matrix checkboxes."""
    body = await req.json()
    res = backend.update_strike_selection(body)
    return JSONResponse(content=res)


@app.post("/api/backtest/date_range")
async def api_backtest_date_range(req: Request):
    """Runs Super Trail vs Fixed 40/40 backtest over historical date range."""
    body = await req.json()
    from_date = str(body.get("from_date", "2026-06-01"))
    to_date = str(body.get("to_date", "2026-11-04"))
    custom_cfg = body.get("super_trail_cfg") or body.get("custom_cfg")
    enforce_ma = bool(body.get("enforce_ma_stack", False))
    bias = body.get("directional_bias") or body.get("bias")
    if enforce_ma:
        custom_cfg = custom_cfg or {}
        custom_cfg["enforce_ma_stack"] = True
    res = backend.run_date_range_backtest(from_date=from_date, to_date=to_date, custom_cfg=custom_cfg, bias=bias)
    return JSONResponse(content=res)


@app.get("/api/scanner/status")
async def api_scanner_status():
    """Returns active 4-strategy scanner telemetry and Party-Switch state."""
    return JSONResponse(content=backend.get_scanner_status())


@app.post("/api/scanner/toggle")
async def api_scanner_toggle(req: Request):
    """Enables or pauses the live 4-strategy scanner."""
    try:
        body = await req.json()
        enabled = bool(body.get("enabled", not backend.scanner_enabled))
    except Exception:
        enabled = not backend.scanner_enabled
    backend.scanner_enabled = enabled
    log.info("Scanner enabled toggled to: %s", enabled)
    return JSONResponse(content={"ok": True, "enabled": backend.scanner_enabled})


@app.post("/api/position/modify")
@app.post("/api/positions/squareoff")
@app.post("/api/paper/squareoff")
async def api_position_modify(req: Request):
    body = await req.json()
    position_id = str(body.get("position_id") or body.get("id") or "")
    action = str(body.get("action") or "SQUARE_OFF")
    res = backend.modify_open_position(position_id=position_id, action=action)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(content=res, status_code=status_code)


@app.get("/api/orders")
async def api_orders():
    orders = backend.get_order_book()
    return JSONResponse(content=orders)


@app.get("/api/positions")
async def api_positions():
    positions = backend.get_active_positions({})
    return JSONResponse(content=positions)


@app.get("/api/replay/dates")
async def api_replay_dates():
    dates = backend.get_available_replay_dates()
    return JSONResponse(content=dates)


@app.get("/api/replay/trades")
@app.post("/api/replay/trades")
async def api_replay_trades(req: Request):
    target_date = "2026-11-04"
    bias = None
    if req.method == "POST":
        try:
            body = await req.json()
            target_date = str(body.get("date", "2026-11-04"))
            bias = body.get("directional_bias") or body.get("bias")
        except Exception:
            pass
    else:
        target_date = req.query_params.get("date", "2026-11-04")
        bias = req.query_params.get("bias") or req.query_params.get("directional_bias")
    trades = backend.get_replay_trades_for_date(target_date, bias=bias)
    return JSONResponse(content={"ok": True, "date": target_date, "directional_bias": bias or backend.directional_bias, "trades": trades, "trades_count": len(trades)})


@app.get("/api/replay")
async def api_replay_get(req: Request):
    date = req.query_params.get("date", "2026-11-04")
    bias = req.query_params.get("bias") or req.query_params.get("directional_bias")
    try:
        speed = float(req.query_params.get("speed", 10.0))
    except Exception:
        speed = 10.0
    res = await backend.run_date_replay(selected_date=date, speed_multiplier=speed, bias=bias)
    return JSONResponse(content=res)


@app.post("/api/replay/run")
async def api_replay_run(req: Request):
    body = await req.json()
    target_date = str(body.get("date", ""))
    speed = float(body.get("speed", 10.0))
    bias = body.get("directional_bias") or body.get("bias")
    res = await backend.run_date_replay(selected_date=target_date, speed_multiplier=speed, bias=bias)
    return JSONResponse(content=res)


@app.post("/api/reconcile/homework")
@app.get("/api/reconcile/homework")
async def api_reconcile_homework(req: Request):
    """
    Scans mentor homework chart screenshots from folder (default: data/UI_screenshot)
    and reconciles them side-by-side with Algo Backtest/Replay ledger.
    """
    folder = "data/UI_screenshot"
    if req.method == "POST":
        try:
            body = await req.json()
            folder = str(body.get("folder") or body.get("path") or body.get("folder_path") or folder)
        except Exception:
            pass
    elif req.method == "GET":
        q_folder = req.query_params.get("folder") or req.query_params.get("path")
        if q_folder:
            folder = q_folder
    res = backend.homework_reconciler.scan_and_reconcile(folder_path=folder, backend=backend)
    return JSONResponse(content=res)


@app.get("/api/oi/matrix")
@app.get("/api/stockmojo/matrix")
async def api_oi_matrix():
    return JSONResponse(content={
        "pcr": backend.cached_telemetry.get("pcr", {}).get("ratio", 1.08),
        "tag": backend.cached_telemetry.get("pcr", {}).get("tag", "BULLISH"),
        "gap_cr": backend.cached_telemetry.get("pcr", {}).get("gap_cr", 1.14),
    })


@app.post("/api/oi/ingest")
@app.post("/api/stockmojo/ingest")
async def api_oi_ingest(req: Request):
    """Ingests 1-minute Institutional table rows from browser bookmarklet, scraper, or devtools."""
    try:
        body = await req.json()
        res = backend.oi_scraper.ingest_rows(body)
        return JSONResponse(content=res)
    except Exception as e:
        log.error("Live OI ingest error: %s", e)
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})


@app.get("/api/oi/pe_ce_table")
@app.get("/api/stockmojo/pe_ce_table")
async def api_oi_pe_ce_table(limit: int = 60):
    """Returns newest-first Institutional PE - CE table (last 60 minutes) + strike breakdown."""
    res = backend.oi_scraper.get_pe_ce_table(limit=limit)
    return JSONResponse(content=res)


@app.post("/api/oi/sync_now")
@app.post("/api/stockmojo/sync_now")
async def api_oi_sync_now():
    """1-click manual trigger to scan downloads folder and poll live Stock Mojo OI & Multi-Strike APIs."""
    dl_res = backend.oi_scraper.check_download_folder()
    api_res = await backend.oi_scraper.poll_direct_stockmojo_api()
    ms_res = await backend.stockmojo_scraper.poll_multi_strike_api()
    return JSONResponse(content={
        "ok": True,
        "downloads_scanned": dl_res,
        "live_api": api_res,
        "multi_strike_api": ms_res,
        "multi_strike_bias": backend.stockmojo_scraper.multi_strike_bias,
        "table_summary": backend.oi_scraper.get_pe_ce_table(limit=1),
        "synergy": backend.stockmojo_scraper.evaluate_synergy_status(),
    })


@app.get("/api/oi/multi_strike")
@app.get("/api/stockmojo/multi_strike")
async def api_stockmojo_multi_strike():
    """Returns real-time multi-strike PE-CE difference metrics and status."""
    return JSONResponse(content=backend.stockmojo_scraper.get_multi_strike_status())


@app.get("/api/oi/live_curves")
@app.get("/api/oi/live_oi")
@app.get("/api/stockmojo/live_oi")
async def api_oi_live_curves():
    """Returns live time-series arrays for Institutional OI curves."""
    res = backend.oi_scraper.get_live_oi_curves()
    return JSONResponse(content=res)


@app.post("/api/oi/update_token")
@app.post("/api/stockmojo/update_token")
async def api_oi_update_token(req: Request):
    """Updates Live Ingestion JWT Bearer token."""
    body = await req.json()
    token = body.get("jwt_token") or body.get("token") or ""
    res = backend.oi_scraper.update_token(token)
    return JSONResponse(content=res)


@app.post("/api/oi/update_auth")
@app.post("/api/stockmojo/update_auth")
async def api_oi_update_auth(req: Request):
    """Live update of Stock Mojo session headers (j, t), payload, or cURL without server restart."""
    body = await req.json()
    res = await backend.stockmojo_scraper.update_auth(
        j=body.get("j") or body.get("header_j") or body.get("STOCKMOJO_HEADER_J"),
        t=body.get("t") or body.get("header_t") or body.get("STOCKMOJO_HEADER_T"),
        payload=body.get("payload") or body.get("STOCKMOJO_PAYLOAD"),
        poll_seconds=body.get("poll_seconds") or body.get("STOCKMOJO_POLL_SECONDS"),
        vast_thresh=body.get("vast_diff_threshold_cr") or body.get("VAST_DIFF_THRESHOLD_CR"),
        curl_command=body.get("curl") or body.get("curl_command") or body.get("command"),
    )
    return JSONResponse(content=res)


@app.get("/api/oi/crore_spread")
@app.get("/api/stockmojo/crore_spread")
async def api_oi_crore_spread():
    """Returns the latest 4 Crore values (Call vs Put) and Who Is More writer dominance."""
    res = backend.stockmojo_scraper.get_crore_spread_metrics()
    return JSONResponse(content=res)


@app.post("/api/oi/configure_curl")
@app.post("/api/stockmojo/configure_curl")
async def api_oi_configure_curl(req: Request):
    """Configures Live Ingestion auth from pasted Chrome DevTools cURL command."""
    body = await req.json()
    curl_str = body.get("curl") or body.get("command") or ""
    res = backend.oi_scraper.configure_curl(curl_str)
    return JSONResponse(content=res)


@app.get("/api/oi/pe_ce_dual")
@app.get("/api/stockmojo/pe_ce_dual")
async def api_oi_pe_ce_dual(limit: int = 60):
    """Returns dual-entity tables (Entity 1 Total OI & Entity 2 OI Change Day) + aligned Combined view."""
    res = backend.stockmojo_scraper.get_dual_tables(limit=limit)
    return JSONResponse(content=res)


@app.get("/api/oi/synergy_status")
@app.get("/api/stockmojo/synergy_status")
async def api_oi_synergy_status():
    """Returns real-time quantitative synergy gate evaluation across Total OI & Day OI."""
    res = backend.stockmojo_scraper.evaluate_synergy_status()
    return JSONResponse(content=res)


@app.get("/api/oi/synergy_config")
@app.get("/api/stockmojo/synergy_config")
async def api_get_synergy_config():
    """Returns active Stock Mojo synergy gate filter switches."""
    return JSONResponse(content=backend.get_synergy_config())


@app.post("/api/oi/synergy_config")
@app.post("/api/stockmojo/synergy_config")
async def api_post_synergy_config(req: Request):
    """Updates Stock Mojo synergy gate filter switches and persists them."""
    body = await req.json()
    res = backend.update_synergy_config(body)
    return JSONResponse(content=res)


@app.get("/api/oi/audit_04nov")
@app.get("/api/stockmojo/audit_04nov")
async def api_oi_audit_04nov():
    """Returns built-in 04 Nov trading session verification checkpoints and audit actions."""
    res = backend.stockmojo_scraper.get_04nov_audit_data()
    return JSONResponse(content=res)


@app.post("/api/stockmojo/live_graph_ingest")
async def api_stockmojo_live_graph_ingest(req: Request):
    """
    Ingests live graph response data, request payload, and auth headers from browser relay script or manual input.
    Auto-saves captured payload and headers into terminal8510_config.json so background httpx polling uses exact payload without 403.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}

    if isinstance(body, dict):
        req_headers = body.get("request_headers") or body.get("headers") or {}
        req_payload = body.get("request_payload") or body.get("payload") or None
        resp_data = body.get("response_data") or body.get("data") or body
        ms_data = body.get("multi_strike_data")
    else:
        req_headers = {}
        req_payload = None
        resp_data = body
        ms_data = None

    # Auto-save credentials if captured from browser
    header_j = req_headers.get("j") or req_headers.get("authorization") or req_headers.get("Authorization") or ""
    header_t = req_headers.get("t") or ""
    if header_j or header_t or req_payload is not None:
        await backend.stockmojo_scraper.update_auth(
            j=header_j if header_j else None,
            t=header_t if header_t else None,
            payload=req_payload,
        )
        log.info("✔ Stock Mojo headers & payload auto-captured and saved to config.")

    # Parse multi-strike if present
    if ms_data:
        backend.stockmojo_scraper.parse_multi_strike_data(ms_data)
    elif isinstance(resp_data, dict) and "t" in resp_data and "co" in resp_data and "po" in resp_data:
        backend.stockmojo_scraper.parse_multi_strike_data(resp_data)

    # Ingest live graph data
    res = backend.stockmojo_scraper.ingest_live_graph_data(resp_data, source="BROWSER_RELAY")
    return JSONResponse(content={
        "ok": True,
        "message": "Stock Mojo live graph ingested successfully",
        "ingest_result": res,
        "crore_spread": backend.stockmojo_scraper.get_crore_spread_metrics(),
        "synergy": backend.stockmojo_scraper.evaluate_synergy_status(),
        "multi_strike": backend.stockmojo_scraper.get_multi_strike_status(),
    })


@app.get("/api/stockmojo/multi_strike_table")
async def api_stockmojo_multi_strike_table(timeframe: str = "1m", mode: str = "all", limit: int = 120):
    res = backend.stockmojo_scraper.get_multi_strike_table(timeframe=timeframe, mode=mode, limit=limit)
    return JSONResponse(content=res)


@app.post("/api/stockmojo/line_mapping")
async def api_stockmojo_set_line_mapping(req: Request):
    """Sets line mapping: 'STANDARD' (Red=Put, Green=Call) or 'INVERTED' (Red=Call, Green=Put)."""
    body = await req.json()
    mapping = body.get("mapping") or body.get("line_mapping") or "STANDARD"
    res = backend.stockmojo_scraper.set_line_mapping(mapping)
    return JSONResponse(content=res)


@app.get("/api/stockmojo/line_mapping")
async def api_stockmojo_get_line_mapping():
    """Gets current line mapping setting."""
    return JSONResponse(content={
        "ok": True,
        "line_mapping": backend.stockmojo_scraper.line_mapping,
        "crore_spread": backend.stockmojo_scraper.get_crore_spread_metrics()
    })


@app.get("/api/stockmojo/relay_script")
async def api_stockmojo_relay_script():
    """Returns the browser auto-relay JavaScript snippet for 1-click clipboard copy."""
    script = backend.stockmojo_scraper.get_browser_relay_script()
    return PlainTextResponse(content=script, media_type="text/javascript")


@app.get("/api/dhan/config")
async def api_dhan_get_config():
    """Returns Dhan credentials metadata (masked) and IP match status."""
    return JSONResponse(content=backend.get_dhan_config())


@app.get("/api/dhan/status")
async def api_dhan_status():
    """Returns Dhan status, masked credentials, token validity and auto-load details."""
    return JSONResponse(content=backend.get_dhan_status())


@app.post("/api/dhan/config")
@app.post("/api/config/dhan")
async def api_dhan_post_config(req: Request):
    """Safely updates Dhan credentials into .env and config without leaking secrets."""
    body = await req.json()
    res = backend.update_dhan_config(body)
    return JSONResponse(content=res)


@app.post("/api/dhan/logout")
async def api_dhan_logout():
    """Explicitly clears Dhan config from disk and env upon user request."""
    res = backend.logout_dhan()
    return JSONResponse(content=res)


@app.post("/api/dhan/connect")
@app.post("/api/dhan/test")
async def api_dhan_connect_test():
    """Runs a live connection test against Dhan HQ v2 API, querying /fundlimit and /marketfeed/ltp with 10s timeout."""
    res = await backend.test_dhan_connection()
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(content=res, status_code=status_code)


def main():
    port = int(backend.cfg.get("port", 8510))
    host = str(backend.cfg.get("host", "127.0.0.1"))
    log.info("Starting Port %d server on %s...", port, host)
    uvicorn.run("engine_8510_live:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
