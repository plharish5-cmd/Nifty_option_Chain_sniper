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
  - GET  /api/dhan_oi/matrix : DhanOI PE-CE confluence snapshot
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import logging
import os
import socket
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

    async def oi_poller():
        while True:
            try:
                await asyncio.to_thread(backend.dhan_oi.get_dhan_oi_bias)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception('Dhan OI poll failed; confluence OI vote unavailable')
            await asyncio.sleep(backend.cfg['dhan_oi']['poll_seconds'])
    app.state.dhan_oi_task = asyncio.create_task(oi_poller())

    def should_run_post_market_ingest(now_hhmm, schedule_ist, today_s,
                                      done_for_day, last_attempt_ts,
                                      retry_seconds, now_ts):
        """Pure guard for the post-market ingest. Extracted so it is testable.

        A closure inside an infinite loop is exactly where the 505-ingests-per-
        evening bug lived unnoticed, so the decision is a function with tests
        rather than an inline condition.
        """
        if now_hhmm < schedule_ist:
            return False
        if done_for_day == today_s:
            return False
        if last_attempt_ts and (now_ts - last_attempt_ts) < retry_seconds:
            return False
        return True

    app.state.should_run_post_market_ingest = should_run_post_market_ingest

    async def auto_ingest_background_scheduler():
        """Post-market ingest, ONCE per day, with a bounded retry on failure.

        The previous version fired whenever the clock read >= schedule_ist, a
        condition that stays true until midnight. daily_ingest()'s own early
        return is guarded by `date_str < today`, which is False for today, so
        nothing stopped it: that was a full ingest every 60 seconds from 15:35
        to 23:59 — roughly 505 per evening, each pulling Dhan and rewriting
        analyst/replay_data/<today>.json. The archive subprocesses were safe
        (run_post_market_archivers is idempotent via the post_market_archives
        table) but the Dhan pulls around them were not, which is a plausible
        contributor to the 429 rate-limit stalls.

        Now: at most one successful ingest per calendar day, and on failure a
        retry no sooner than retry_seconds, so a broken feed cannot turn into a
        tight loop against the broker.
        """
        done_for_day = None          # date string of the last SUCCESSFUL ingest
        last_attempt_ts = 0.0
        while True:
            try:
                await asyncio.sleep(60)
                now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
                today_s = now_ist.strftime("%Y-%m-%d")
                arch_cfg = backend.cfg['post_market_archivers']
                retry_after = float(arch_cfg.get('retry_seconds', 300))
                if not should_run_post_market_ingest(
                        now_ist.strftime('%H:%M'), arch_cfg['schedule_ist'],
                        today_s, done_for_day, last_attempt_ts,
                        retry_after, time.time()):
                    continue
                last_attempt_ts = time.time()
                res = await asyncio.to_thread(backend.daily_ingest, today_s)
                if isinstance(res, dict) and res.get('eod_summary'):
                    done_for_day = today_s
                    log.info("✔ Post-market ingest complete for %s; no further "
                             "ingest will run today.", today_s)
                else:
                    log.warning("Post-market ingest for %s did not complete; "
                                "retrying in %.0fs.", today_s, retry_after)
            except asyncio.CancelledError:
                break
            except Exception as ex:
                log.error("Auto ingest scheduler error: %s", ex)

    app.state.archive_task = asyncio.create_task(auto_ingest_background_scheduler())
    log.info("✔ Auto-Ingest post-market scheduler daemon launched.")


@app.get('/api/trend_capture/status')
async def trend_capture_status():
    return await asyncio.to_thread(backend.trend_capture_service().status)

@app.get('/api/trend_capture/backtest')
async def trend_capture_backtest():
    return backend.trend_capture_service().backtest()

@app.get('/api/trend_capture/config')
async def trend_capture_config():
    return backend.trend_capture_service().cfg

@app.post('/api/trend_capture/config')
async def trend_capture_update(request: Request):
    from analyst.trend_capture_service_8510 import update_config
    try:
        settings=update_config(backend.config_path,await request.json())
        backend.cfg['trend_capture_v192']=settings
        service=backend.trend_capture_service();service.cfg=settings;service.at=0
        return {'ok':True,'config':settings,'execution_authorized':False}
    except (ValueError,TypeError) as exc:
        return JSONResponse(status_code=400,content={'ok':False,'error':str(exc)})

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
        "algo_only_execution": backend.algo_only_execution,
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


@app.post("/api/funds/paper/reset_ledger")
async def api_paper_reset_ledger(req: Request):
    """Start P&L tracking fresh. Explicit action only — never fires on deploy.

    Open positions are preserved and their margin recomputed, so this is safe to
    run mid-session: only the realised-P&L history is cleared.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    res = backend.reset_paper_ledger(
        clear_orders=bool(body.get("clear_orders", False)),
        clear_closed_positions=bool(body.get("clear_closed_positions", False)),
    )
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
    if "algo_only" in body or "algo_only_execution" in body:
        val = body.get("algo_only", body.get("algo_only_execution"))
        backend.algo_only_execution = bool(val)
        backend.cfg["algo_only_execution"] = backend.algo_only_execution
        try:
            backend.config_path.write_text(json.dumps(backend.cfg, indent=2))
        except OSError as exc:
            log.error("Could not persist algo_only_execution: %s", exc)
        log.warning("Algo-Only execution set to %s (manual orders %s)",
                    backend.algo_only_execution,
                    "BLOCKED" if backend.algo_only_execution else "allowed")
    return JSONResponse(content={
        "ok": True,
        "execution_mode": backend.execution_mode,
        "paper_execution_enabled": backend.paper_execution_enabled,
        "directional_bias": backend.directional_bias,
        "algo_only_execution": backend.algo_only_execution,
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


@app.get("/api/backtest/date_range")
@app.post("/api/backtest/date_range")
async def api_backtest_date_range(req: Request):
    """Runs Super Trail vs Fixed 40/40 backtest over historical date range."""
    from_date = "2026-06-01"
    to_date = "2026-11-05"
    custom_cfg = None
    enforce_ma = False
    bias = None
    include_today_paper = True

    if req.method == "POST":
        try:
            body = await req.json()
            from_date = str(body.get("from_date", from_date))
            to_date = str(body.get("to_date", to_date))
            custom_cfg = body.get("super_trail_cfg") or body.get("custom_cfg")
            enforce_ma = bool(body.get("enforce_ma_stack", False))
            bias = body.get("directional_bias") or body.get("bias")
            if "include_today_paper" in body:
                include_today_paper = bool(body.get("include_today_paper"))
        except Exception:
            pass
    else:
        from_date = str(req.query_params.get("from_date", from_date))
        to_date = str(req.query_params.get("to_date", to_date))
        enforce_ma = req.query_params.get("enforce_ma_stack") == "true"
        bias = req.query_params.get("directional_bias") or req.query_params.get("bias")
        if "include_today_paper" in req.query_params:
            include_today_paper = req.query_params.get("include_today_paper") == "true"

    if enforce_ma:
        custom_cfg = custom_cfg or {}
        custom_cfg["enforce_ma_stack"] = True
    res = backend.run_date_range_backtest(
        from_date=from_date, 
        to_date=to_date, 
        custom_cfg=custom_cfg, 
        bias=bias, 
        include_today_paper=include_today_paper
    )
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


@app.get("/api/proven_edges/config")
async def api_get_proven_edges_config():
    """Returns the status and configuration for Goldilocks and Futures Volume Surge."""
    return JSONResponse(content=backend.get_proven_edges_config())


@app.post("/api/proven_edges/config")
async def api_post_proven_edges_config(req: Request):
    """Updates toggleable configuration for Goldilocks and Futures Volume Surge."""
    try:
        data = await req.json()
    except Exception:
        data = {}
    return JSONResponse(content=backend.update_proven_edges_config(data))


@app.get("/api/ema_proximity/table")
async def api_ema_proximity_table(req: Request):
    """Returns minute-by-minute 4-EMA & 8-EMA Proximity Matrix with summary KPIs (v1.8)."""
    date_str = str(req.query_params.get("date", "LIVE")).strip()
    timeframe = str(req.query_params.get("timeframe", "1m")).strip()
    filter_state = str(req.query_params.get("filter_state", "ALL")).strip()
    res = backend.get_ema_proximity_table(date_str=date_str, timeframe=timeframe, filter_state=filter_state)
    return JSONResponse(content=res)


@app.get("/api/ema_proximity/config")
async def api_ema_proximity_get_config():
    """Returns persisted 4/8 EMA Proximity Radar configuration."""
    return JSONResponse(content=backend.get_ema_proximity_config())


@app.post("/api/ema_proximity/config")
async def api_ema_proximity_post_config(req: Request):
    """Updates and persists 4/8 EMA Proximity Radar configuration."""
    try:
        data = await req.json()
    except Exception:
        data = {}
    return JSONResponse(content=backend.update_ema_proximity_config(data))


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
async def api_replay_dates(req: Request):
    dates = backend.get_available_dates()
    if req.query_params.get("raw") == "true":
        return JSONResponse(content=dates)
    return JSONResponse(content={"ok": True, "dates": dates, "latest": dates[0] if dates else None})


@app.post("/api/daily_ingest")
@app.post("/api/ingest/pull_date")
async def api_ingest_pull_date(req: Request):
    """Pulls and persists 8 daily entities for date into unified store."""
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    target_date = str(body.get("date", datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat())).strip()
    overwrite = bool(body.get("overwrite", False))
    res = await asyncio.to_thread(backend.daily_ingest,target_date,overwrite=overwrite)
    return JSONResponse(content=res)


@app.get("/api/ingest/status")
async def api_ingest_status(req: Request):
    """Returns ingestion status and checklist for target date."""
    target_date = req.query_params.get("date", "2026-11-05").strip()
    res = await asyncio.to_thread(backend.daily_ingest,target_date,overwrite=False)
    return JSONResponse(content=res)


@app.get("/api/ingest/eod_summary")
async def api_ingest_eod_summary(req: Request):
    """Returns structured End-of-Day digest and WhatsApp plain-text."""
    target_date = req.query_params.get("date", "2026-11-05").strip()
    summary = backend.get_eod_summary(target_date)
    return JSONResponse(content=summary)


@app.get("/api/trend_lab/run")
@app.post("/api/trend_lab/run")
async def api_trend_lab_run(req: Request):
    """Dynamically evaluates multi-factor trend and Dhan OI biases."""
    from_date = "2026-11-04"
    to_date = "2026-11-05"
    if req.method == "POST":
        try:
            body = await req.json()
            from_date = str(body.get("from_date") or body.get("start_date") or from_date).strip()
            to_date = str(body.get("to_date") or body.get("end_date") or to_date).strip()
        except Exception:
            pass
    else:
        from_date = req.query_params.get("from_date") or req.query_params.get("start_date") or from_date
        to_date = req.query_params.get("to_date") or req.query_params.get("end_date") or to_date
    res = backend.run_trend_lab(from_date=from_date, to_date=to_date)
    return JSONResponse(content=res)


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


# ─────────────────────────────────────────────────────────────────────────────
# DHAN OI / PE-CE FLOW — direct official option-chain observations.
# Compatibility writer-flow routes expose this same source; no third-party feed.
# Receipt timestamps and freshness are retained; missing history stays UNKNOWN.
# ─────────────────────────────────────────────────────────────────────────────

@app.get('/api/dhan_oi/bias')
async def api_dhan_oi_bias(force: bool = False):
    return JSONResponse(content=await asyncio.to_thread(backend.dhan_oi.get_dhan_oi_bias,force))

@app.get('/api/writer_flow')
@app.get('/api/oi/writer_flow')
async def api_writer_flow(force: bool = False):
    return JSONResponse(content=await asyncio.to_thread(backend.dhan_oi.get_snapshot,force))

@app.get('/api/oi/pe_ce_table')
@app.get('/api/oi/pe_ce_dual')
@app.get('/api/dhan_oi/pe_ce_dual')
async def api_oi_pe_ce_table(limit: int = 60):
    return JSONResponse(content=backend.dhan_oi.get_pe_ce_table(limit))

@app.get('/api/dhan_oi/live_oi')
@app.get('/api/dhan_oi/synergy_status')
async def api_oi_live():
    return JSONResponse(content=await asyncio.to_thread(backend.dhan_oi.get_dhan_oi_bias))

@app.get('/api/oi/synergy_config')
@app.get('/api/dhan_oi/synergy_config')
async def api_get_synergy_config():
    return JSONResponse(content={'ok':True,'dhan_oi_synergy':backend.get_synergy_config()})

@app.post('/api/oi/synergy_config')
@app.post('/api/dhan_oi/synergy_config')
async def api_post_synergy_config(req: Request):
    return JSONResponse(content=backend.update_synergy_config(await req.json()))

@app.on_event('shutdown')
async def stop_background_tasks():
    for task in (getattr(app.state,'dhan_oi_task',None),getattr(app.state,'archive_task',None),backend.scanner_task):
        if task:task.cancel()

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
    return JSONResponse(content=res, status_code=200)


def main() -> int:
    port = int(backend.cfg.get("port", 8510))
    host = str(backend.cfg.get("host", "127.0.0.1"))

    # ── CLAIM THE PORT BEFORE THE STARTUP EVENT CAN TRADE ────────────────────
    # The startup event launches the strategy scanner, which can place a FILLED
    # order within milliseconds. uvicorn runs that event BEFORE it binds, so a
    # second instance started by accident would place a live trade and only then
    # die on "address already in use" — leaving a real position behind with no
    # running app to manage it.
    #
    # Binding here, and handing the bound socket to uvicorn, means a duplicate
    # launch exits before the scanner or the broker are ever touched, and leaves
    # no bind race for uvicorn to lose.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        log.error("Port %d on %s is already in use (%s).", port, host, exc)
        log.error("8510 is ALREADY RUNNING. Nothing was started: no scanner, "
                  "no broker call, no order placed.")
        log.error("To see what holds the port:   ss -ltnp \"sport = :%d\"", port)
        return 1
    sock.listen(2048)

    log.info("Starting Port %d server on %s...", port, host)
    cfg = uvicorn.Config(app, host=host, port=port, reload=False, log_level="info")
    uvicorn.Server(cfg).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
