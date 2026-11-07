#!/usr/bin/env python3
"""
engine_8510_live.py — NIFTY Quant Terminal Port 8510 Live Server.

Endpoints:
  - GET/HEAD /                 : Serves Algorithmic_Trading_Workspace_8510.html (no-store)
  - GET  /api/health           : Daemon status
  - GET  /api/telemetry        : Live 1-second tick telemetry & trend confluence
  - GET  /api/funds            : Live Dhan funds + Paper wallet
  - POST /api/funds/paper/update: Add / Decrease / Reset Paper funds
  - POST /api/mode             : Explicit real-money opt-in; paper permanently ON
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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

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
    backend.start_recording()
    async def grok_poller():
        # Optional advisory failures must not prevent the trading terminal starting.
        while True:
            try:
                if getattr(backend, 'grok_regime_bridge', None) is None:
                    from analyst.grok_regime_bridge_8510 import GrokRegimeBridge
                    backend.grok_regime_bridge = await asyncio.to_thread(
                        GrokRegimeBridge, backend.ledger_db, backend.market_db,
                        cache_path=ROOT_DIR / 'analyst/grok_regime_cache.json',
                        volume_engine=backend.futures_volume_engine)
                await asyncio.to_thread(backend.grok_regime_bridge.evaluate)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception('Advisory Grok bridge unavailable')
            bridge = getattr(backend, 'grok_regime_bridge', None)
            await asyncio.sleep(bridge.rules['poll_seconds'] if bridge else 30)
    app.state.grok_task = asyncio.create_task(grok_poller())

    from analyst.ict_smc_signals_8510 import SignalBridge
    app.state.ict_smc_bridge = SignalBridge(backend.cfg['ict_smc_desk_signals'],
        backend.ledger_db.parent / 'ict_smc_signals.sqlite3')
    async def ict_signal_poller():
        while True:
            try:
                await asyncio.to_thread(app.state.ict_smc_bridge.evaluate)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception('ICT SMC Desk Signals evaluation unavailable')
            await asyncio.sleep(backend.cfg['ict_smc_desk_signals']['cache_seconds'])
    app.state.ict_smc_task = asyncio.create_task(ict_signal_poller())
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


@app.get('/api/ict-smc-signals/research')
async def ict_smc_research_report():
    from analyst.ict_smc_research_view_8510 import render
    evidence=ROOT/'analyst/ict_smc_backtest_evidence_8510.json'
    return HTMLResponse(render(json.loads(evidence.read_text())))

@app.get('/api/ict-smc-signals')
async def ict_smc_status():
    bridge=getattr(app.state,'ict_smc_bridge',None)
    return bridge.last if bridge and bridge.last else {'name':'ICT SMC Desk Signals','status':'UNAVAILABLE','rows':[], 'use_for_algo_trades':False}

@app.post('/api/ict-smc-signals/config')
async def ict_smc_settings(req: Request):
    data=await req.json()
    if data.get('use_for_algo_trades') is not False:
        return JSONResponse({'ok':False,'error':'Unvalidated advisory: algo use remains OFF'},status_code=409)
    backend.cfg['ict_smc_desk_signals']['use_for_algo_trades']=False
    return {'ok':True,'use_for_algo_trades':False}

@app.get("/api/health")
async def api_health():
    m_status = backend.is_nse_market_open()
    return JSONResponse(content={
        "status": "ONLINE",
        "port": 8510,
        "dhan_connected": backend.broker.is_configured(),
        "execution_mode": backend.execution_mode,
        "paper_enabled": True,
        "real_money_enabled": backend.execution_mode == "BOTH",
        "real_money_available": backend.cfg.get("execution_safety_v194", {}).get("live_validation_status") == "APPROVED",
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


def _grok_service():
    from fastapi import HTTPException
    service = getattr(backend, 'grok_regime_bridge', None)
    if service is None:
        raise HTTPException(status_code=503, detail='Bridge not started')
    return service


def _grok_local_write(req):
    from fastapi import HTTPException
    if req.client is None or req.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(status_code=403, detail='Local desk only')
    origin = req.headers.get('origin')
    if origin and origin not in ('http://localhost:8510', 'http://127.0.0.1:8510'):
        raise HTTPException(status_code=403, detail='Invalid origin')


@app.get('/api/grok_bridge/status')
async def grok_status():
    return await asyncio.to_thread(_grok_service().status)


@app.post('/api/grok_bridge/evaluate')
async def grok_evaluate(req: Request):
    _grok_local_write(req)
    # No network awaits on the event loop. Concurrent requests coalesce in the worker.
    return await asyncio.to_thread(_grok_service().evaluate, True)


@app.post('/api/grok_bridge/config')
async def grok_config(req: Request):
    from fastapi import HTTPException
    _grok_local_write(req)
    try:
        return await asyncio.to_thread(_grok_service().update_config, await req.json())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post('/api/grok_bridge/webhook')
async def grok_webhook(req: Request):
    import hmac
    from fastapi import HTTPException
    secret = os.environ.get('GROK_WEBHOOK_SECRET', '')
    if not secret or not hmac.compare_digest(req.headers.get('authorization',''), 'Bearer '+secret):
        raise HTTPException(status_code=401, detail='Webhook token required')
    try:
        return await asyncio.to_thread(_grok_service().ingest_grok_bot_webhook, await req.json())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/telemetry")
async def api_telemetry():
    """1-second ticking telemetry stream."""
    data = await backend.get_live_telemetry()
    data["grok_regime_bridge"] = backend.get_grok_regime_cached()
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
    forbidden = {'mode','paper_enabled','paper_execution_enabled','algo_only','algo_only_execution'}
    if forbidden.intersection(body):
        return JSONResponse(status_code=400, content={'ok': False, 'error': 'Paper and algo-only are permanently ON; legacy mode switches are removed'})
    if 'real_money_enabled' in body:
        result = backend.configure_real_money(body['real_money_enabled'], body.get('confirmation',''))
        return JSONResponse(status_code=200 if result['ok'] else 403, content=result)
    if 'directional_bias' in body or 'bias' in body:
        backend.set_directional_bias(str(body.get('directional_bias') or body.get('bias')).upper())
    return {'ok': True, 'execution_mode': backend.execution_mode, 'paper_execution_enabled': True,
            'algo_only_execution': True, 'real_money_enabled': backend.execution_mode == 'BOTH'}


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
    # Scanner calls the engine internally. A browser cannot impersonate it by supplying a flag.
    return JSONResponse(status_code=403, content={'ok': False, 'status': 'REJECTED',
        'algo_only_blocked': True, 'error': 'Manual trading is permanently disabled; select watchlist strikes for enabled algo setups instead'})


@app.get("/api/matrix_scalp/config")
async def api_matrix_scalp_get():
    """Matrix Scalp settings plus what has been recorded so far."""
    cfg = dict(backend.cfg.get("matrix_scalp", {}) or {})
    out = {"ok": True, "config": cfg}
    try:
        import sqlite3 as _s
        with _s.connect("file:{}?mode=ro".format(backend.ledger_db), uri=True) as c:
            c.row_factory = _s.Row
            out["open_positions"] = [dict(r) for r in c.execute(
                "SELECT * FROM positions WHERE status='OPEN' "
                "AND strategy_id LIKE 'matrix_scalp%'")]
            out["closed_count"] = c.execute(
                "SELECT COUNT(*) FROM positions WHERE status LIKE 'CLOSED%' "
                "AND strategy_id LIKE 'matrix_scalp%'").fetchone()[0]
            out["sessions_recorded"] = c.execute(
                "SELECT COUNT(DISTINCT substr(entry_time,1,10)) FROM positions "
                "WHERE strategy_id LIKE 'matrix_scalp%'").fetchone()[0]
    except Exception as exc:
        out["ledger_error"] = str(exc)
    return JSONResponse(content=out)


@app.post("/api/matrix_scalp/config")
async def api_matrix_scalp_set(req: Request):
    """Enable/disable and set the spot target and stop width.

    Only these keys are writable. The evidence notes, the deltas and the tier
    filter are not settable from the browser, so the recorded study cannot be
    silently redefined mid-flight.
    """
    body = await req.json()
    cfg = dict(backend.cfg.get("matrix_scalp", {}) or {})
    allowed = {"enabled": bool, "spot_target_pts": float, "spot_stop_pts": float,
               "cooldown_minutes": int, "use_engine_micro_sl": bool,
               "last_entry_ist": str}
    applied, rejected = {}, {}
    for k, v in (body or {}).items():
        if k not in allowed:
            rejected[k] = "not settable from the UI"
            continue
        try:
            cfg[k] = allowed[k](v)
            applied[k] = cfg[k]
        except (TypeError, ValueError):
            rejected[k] = "expected {}".format(allowed[k].__name__)
    if cfg.get("spot_stop_pts", 0) <= 0 or cfg.get("spot_target_pts", 0) <= 0:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "target and stop must both be positive"})
    backend.cfg["matrix_scalp"] = cfg
    try:
        backend.config_path.write_text(json.dumps(backend.cfg, indent=2))
    except OSError as exc:
        return JSONResponse(status_code=500, content={
            "ok": False, "error": "could not persist: {}".format(exc)})
    return JSONResponse(content={
        "ok": True, "applied": applied, "rejected": rejected, "config": cfg,
        "note": ("Paper only. The stop width is an unvalidated starting point; "
                 "30-60 recorded sessions are needed before any verdict.")})


@app.get("/api/live/status")
async def api_live_status():
    """Header state for the live switch. Read-only; arms nothing."""
    return JSONResponse(content=backend.get_live_status())


@app.post("/api/live/arm")
async def api_live_arm(req: Request):
    """Arms or disarms real money. Delegates to the audited gate.

    The gate is NOT reimplemented here: configure_real_money still requires
    live_validation_status == 'APPROVED' plus the exact typed confirmation, and
    still persists real_money_enabled=False so a restart never re-arms. While
    the verdict is NO_GO this returns 403 with the backend's own wording.
    """
    body = await req.json()
    enabled = body.get("enabled", body.get("real_money_enabled"))
    if not isinstance(enabled, bool):
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "enabled must be a boolean"})
    result = backend.configure_real_money(enabled, str(body.get("confirmation", "")))
    result["live_status"] = backend.get_live_status()
    return JSONResponse(status_code=200 if result.get("ok") else 403, content=result)


@app.post("/api/live/stop")
async def api_live_stop(req: Request):
    """'STOP LIVE — Switch to PAPER ONLY'.

    Disarms live and cancels pending live orders unconditionally. Does NOT
    auto-square-off: pass {"square_off": true} only after the user answers the
    prompt. The 15:20 EOD square-off is independent of this and still runs, so
    an unanswered prompt cannot leave a live position open overnight.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    sq = body.get("square_off", None)
    res = await backend.stop_live_trading(sq if isinstance(sq, bool) else None)
    res["live_status"] = backend.get_live_status()
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


@app.get('/api/recording/coverage')
async def recording_coverage():
    recorder = getattr(backend, 'recording_service', None)
    return {**recorder.health, 'last_error': recorder.store.last_error, 'last_write': recorder.store.last_write, 'queue_dropped': recorder.dropped, 'last_cycle_at': recorder.last_cycle_at} if recorder else {'status': 'UNAVAILABLE', 'reason': 'Recorder not started; check server log'}

@app.get('/api/recording/history')
async def recording_history():
    recorder = getattr(backend, 'recording_service', None)
    return {'ema_rows': recorder.today_history, 'radar': recorder.radars} if recorder else {'ema_rows': [], 'radar': {}}

async def recording_export(request, table, prefix):
    recorder = getattr(backend, 'recording_service', None)
    if not recorder: return JSONResponse(status_code=503, content={'error': 'Recorder unavailable'})
    today = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    start = request.query_params.get('from', today); end = request.query_params.get('to', today)
    fmt = request.query_params.get('format', 'csv')
    try:
        recorder.store.validate_range(start, end)
        if fmt not in ('csv', 'xlsx'): raise ValueError('format must be csv or xlsx')
    except ValueError as exc:
        return JSONResponse(status_code=400, content={'error': str(exc)})
    name = prefix + '_' + start + ('_to_' + end if end != start else '') + '.' + fmt
    if fmt == 'csv':
        chunks = recorder.store.export_csv(table, start, end); mime = 'text/csv; charset=utf-8'
    else:
        try: file = await asyncio.to_thread(recorder.store.export_xlsx, table, start, end)
        except Exception:
            log.exception('Recording xlsx export failed')
            return JSONResponse(status_code=503, content={'error': 'Excel export unavailable; CSV remains available'})
        def chunks_file():
            try:
                while True:
                    chunk = file.read(65536)
                    if not chunk: break
                    yield chunk
            finally: file.close()
        chunks = chunks_file(); mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return StreamingResponse(chunks, media_type=mime, headers={'Content-Disposition': 'attachment; filename="'+name+'"'})

@app.get('/api/ema_proximity/export')
async def ema_export(request: Request):
    return await recording_export(request, 'ema_proximity_history', 'EMA_Proximity')

@app.get('/api/radar_tier/export')
async def radar_export(request: Request):
    return await recording_export(request, 'radar_tier_signals', 'Radar_Tier_Signals')

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


@app.get('/api/history/recorded')
async def api_recorded_history(from_date: str = '1900-01-01', to_date: str = '9999-12-31'):
    try:
        return await asyncio.to_thread(backend._recorded_history().report, from_date, to_date)
    except ValueError as exc:
        return JSONResponse({'ok':False,'error':str(exc)},status_code=400)

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
    recorder = getattr(backend, "recording_service", None)
    if recorder: recorder.stop()
    for task in (getattr(app.state,'grok_task',None),getattr(app.state,'ict_smc_task',None),getattr(app.state,'dhan_oi_task',None),getattr(app.state,'archive_task',None),backend.scanner_task):
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
    return JSONResponse(content=backend._public_dhan_status(res), status_code=200)


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
