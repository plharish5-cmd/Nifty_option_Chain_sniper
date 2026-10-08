"""
api/main.py — FastAPI app.  Run:  .venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8600

REST (all GET unless noted)
  /api/status                 feed / token / chain / backfill health
  /api/quote                  spot, change, VIX
  /api/candles?tf=5m&days=3   candles for the chart (last one may be forming: closed=false)
  /api/analysis?tf=5m         zones, pools, markers, swing lines, range/OTE  (closed candles only)
  /api/signals                playbook evaluation on the primary timeframe (TRADE / SKIP / INFO)
  /api/chain?expiry=…         full option chain; /api/expiries
  /api/dashboard              everything the home screen needs in one call
  /api/alerts?grade=trade     alert log
  /api/paper                  paper trades + stats
  POST /api/repair            force a REST backfill of today's candles
WebSocket
  /ws                         pushes {type: quote | candle | analysis | chain | alert | paper}
The built React app (frontend/dist) is served at /.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..signals import alerts as alerts_mod
from ..signals import paper
from ..timeutil import TF_SECONDS
from .hub import Hub, _cj

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
hub: Optional[Hub] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub
    config.load_env(force=True)
    hub = Hub()
    await hub.start()
    yield
    await hub.stop()


app = FastAPI(title="NIFTY ICT/SMC", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


def _h() -> Hub:
    if hub is None:
        raise HTTPException(503, "starting")
    return hub


@app.get("/api/status")
def status():
    return _h().status_view()


@app.get("/api/quote")
def quote():
    return _h().quote()


@app.get("/api/candles")
def candles(tf: str = Query("5m"), days: int = Query(3, ge=1, le=60)):
    if tf not in TF_SECONDS:
        raise HTTPException(400, f"tf must be one of {list(TF_SECONDS)}")
    h = _h()
    rows = h.candles.frame(h.nifty, tf, days=days, include_forming=True)
    return {"tf": tf, "candles": [_cj(r) for r in rows]}


@app.get("/api/analysis")
def analysis(tf: str = Query("5m")):
    h = _h()
    j = h.json.get(tf)
    if j is None:
        h.recompute(tf)
        j = h.json.get(tf)
    return j or {"tf": tf, "zones": [], "pools": [], "markers": [], "zigzag": [], "range": None}


@app.get("/api/signals")
def signals():
    h = _h()
    return {"tf": h.primary, "bias_tf": h.bias_tf, "signals": [s.to_dict() for s in h.today_signals()]}


@app.get("/api/expiries")
def expiries():
    return {"expiries": _h().chain.expiries}


@app.get("/api/chain")
def chain(expiry: Optional[str] = None):
    h = _h()
    exp = expiry or (h.chain.expiries[0] if h.chain.expiries else (sorted(h.chain.chains)[0] if h.chain.chains else None))
    ch = h.chain.chains.get(exp) if exp else None
    if not ch:
        return JSONResponse({"expiry": exp, "rows": [], "status": h.chain.status, "error": h.chain.last_error})
    return ch


@app.get("/api/dashboard")
def dashboard():
    return _h().dashboard()


@app.get("/api/alerts")
def alerts(grade: Optional[str] = None, limit: int = 60):
    return {"alerts": alerts_mod.recent(limit, grade)}


@app.get("/api/paper")
def paper_view():
    return {"trades": paper.trades(100), "stats": paper.stats(), "open": paper.open_trade()}


@app.post("/api/repair")
async def repair():
    h = _h()
    await h.repair(full=False)
    h.recompute_all()
    return h.status_view()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    h = _h()
    await websocket.accept()
    h.clients.add(websocket)
    try:
        await websocket.send_json({"type": "quote", "quote": h.quote()})
        while True:
            await websocket.receive_text()                   # the browser only sends keep-alive pings
    except WebSocketDisconnect:
        pass
    finally:
        h.clients.discard(websocket)


# ── the built frontend ───────────────────────────────────────────────────────
if (config.FRONTEND_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(config.FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        root = config.FRONTEND_DIST.resolve()
        f = (root / path).resolve()
        if path and f.is_file() and root in f.parents:            # no path traversal out of dist/
            return FileResponse(str(f))
        return FileResponse(str(config.FRONTEND_DIST / "index.html"))
