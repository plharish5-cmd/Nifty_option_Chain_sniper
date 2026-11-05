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
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# Path resolution
ROOT_DIR = Path(__file__).resolve().parent
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
    allow_origins=["*"],
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

    backend.scanner_task = asyncio.create_task(backend.start_strategy_scanner_loop())
    log.info("✔ 4-Strategy Live Scanner daemon launched.")

    # Start 60s background polling daemon for Institutional live direct OI and folder watcher
    backend.stockmojo_scraper.start_background_polling()
    log.info("✔ Institutional 60s Live OI Polling daemon launched.")


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
    return JSONResponse(content={
        "status": "ONLINE",
        "port": 8510,
        "dhan_connected": backend.broker.is_configured(),
        "execution_mode": backend.execution_mode,
        "paper_enabled": backend.paper_execution_enabled,
        "directional_bias": backend.directional_bias,
        "kill_switch_active": backend.kill_switch_active,
    })


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
async def api_order_submit(req: Request):
    """Places Aggressive Limit order with Chasing Engine."""
    body = await req.json()
    contract_id = body.get("contract_id")
    side = body.get("side", "BUY")
    strategy_name = body.get("strategy_name", "Manual Matrix")
    target_pts = float(body.get("target_pts", 40.0))
    sl_pts = float(body.get("sl_pts", 40.0))
    is_algo_trade = bool(body.get("is_algo_trade", False))

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
    )
    return JSONResponse(content=res)


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
async def api_position_modify(req: Request):
    body = await req.json()
    position_id = str(body.get("position_id", ""))
    action = str(body.get("action", ""))
    res = backend.modify_open_position(position_id=position_id, action=action)
    return JSONResponse(content=res)


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
    """1-click manual trigger to scan downloads folder and poll live Institutional OI API."""
    dl_res = backend.oi_scraper.check_download_folder()
    api_res = await backend.oi_scraper.poll_direct_stockmojo_api()
    return JSONResponse(content={
        "ok": True,
        "downloads_scanned": dl_res,
        "live_api": api_res,
        "table_summary": backend.oi_scraper.get_pe_ce_table(limit=1),
    })


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


def main():
    port = int(backend.cfg.get("port", 8510))
    host = str(backend.cfg.get("host", "0.0.0.0"))
    log.info("Starting Port %d server on %s...", port, host)
    uvicorn.run("engine_8510_live:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
