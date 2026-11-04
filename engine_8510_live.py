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
        "kill_switch_active": backend.kill_switch_active,
    })


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
    return JSONResponse(content={
        "ok": True,
        "execution_mode": backend.execution_mode,
        "paper_execution_enabled": backend.paper_execution_enabled,
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
    custom_cfg = body.get("super_trail_cfg")
    res = backend.run_date_range_backtest(from_date=from_date, to_date=to_date, custom_cfg=custom_cfg)
    return JSONResponse(content=res)


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
    if req.method == "POST":
        try:
            body = await req.json()
            target_date = str(body.get("date", "2026-11-04"))
        except Exception:
            pass
    else:
        target_date = req.query_params.get("date", "2026-11-04")
    trades = backend.get_replay_trades_for_date(target_date)
    return JSONResponse(content={"ok": True, "date": target_date, "trades": trades, "trades_count": len(trades)})


@app.get("/api/replay")
async def api_replay_get(req: Request):
    date = req.query_params.get("date", "2026-11-04")
    try:
        speed = float(req.query_params.get("speed", 10.0))
    except Exception:
        speed = 10.0
    res = await backend.run_date_replay(selected_date=date, speed_multiplier=speed)
    return JSONResponse(content=res)


@app.post("/api/replay/run")
async def api_replay_run(req: Request):
    body = await req.json()
    target_date = str(body.get("date", ""))
    speed = float(body.get("speed", 10.0))
    res = await backend.run_date_replay(selected_date=target_date, speed_multiplier=speed)
    return JSONResponse(content=res)


@app.get("/api/stockmojo/matrix")
async def api_stockmojo_matrix():
    return JSONResponse(content={
        "pcr": backend.cached_telemetry.get("pcr", {}).get("ratio", 1.08),
        "tag": backend.cached_telemetry.get("pcr", {}).get("tag", "BULLISH"),
        "gap_cr": backend.cached_telemetry.get("pcr", {}).get("gap_cr", 1.14),
    })


def main():
    port = int(backend.cfg.get("port", 8510))
    host = str(backend.cfg.get("host", "0.0.0.0"))
    log.info("Starting Port %d server on %s...", port, host)
    uvicorn.run("engine_8510_live:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
