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
  /api/zones?tf=5m&active=1   zone registry (pools / OBs / FVGs in one shape)
  /api/delta?tf=5m            delta per closed candle + delta events + adapter status
  /api/events?tf=5m           one timeline: BOS / MSS / sweeps + delta events (inspector)
  POST /api/delta/adapter     {"name": "footprint_feed" | "csv" | "mock"}
  POST /api/delta/csv?bar_seconds=60&ts_is=end&misaligned=floor&inst=NIFTY|2026-10-13|22400|CE   body = CSV text
  /api/strikes                selected CE/PE, candidates + "why" lines, focused strike panel, context verdict
  POST /api/strikes/select    re-run the Priyanka selector now (uses the stored chain — no extra Dhan call)
  POST /api/strikes/manual    {"right": "CE", "strike": 22400, "expiry": "2026-10-13"} | {"right": "CE", "clear": true}
  POST /api/strikes/auto      {"on": true}         POST /api/strikes/expiry {"expiry": "2026-10-13"}
  POST /api/mode              {"mode": "INDEX" | "OPTION_CHART"}   (option chart = EXPERIMENTAL view only)
  POST /api/proxy             {"enabled": true}    Options Flow Proxy — PROXY (not real delta)
  /api/candles?inst=KEY  /api/analysis?inst=KEY   option candles / experimental option-chart analysis
WebSocket
  /ws                         pushes {type: quote | candle | analysis | chain | alert | paper}
The built React app (frontend/dist) is served at /.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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


def _opt(h: Hub, key: str):
    from ..market import instruments as instr
    try:
        return instr.resolve(instr.parse_key(key), h.chain.chains)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/candles")
def candles(tf: str = Query("5m"), days: int = Query(3, ge=1, le=60), inst: str = Query("NIFTY")):
    if tf not in TF_SECONDS:
        raise HTTPException(400, f"tf must be one of {list(TF_SECONDS)}")
    h = _h()
    if inst not in ("NIFTY", "INDEX", ""):
        from ..market import options as optc
        if tf == "1D":
            raise HTTPException(400, "option charts are intraday only")
        o = _opt(h, inst)
        return {"tf": tf, "instrument": o.to_dict(), "candles": [_cj(r) for r in optc.frame(o, tf, days)]}
    rows = h.candles.frame(h.nifty, tf, days=days, include_forming=True)
    return {"tf": tf, "candles": [_cj(r) for r in rows]}


@app.get("/api/analysis")
def analysis(tf: str = Query("5m"), inst: str = Query("NIFTY")):
    h = _h()
    if inst not in ("NIFTY", "INDEX", ""):
        from ..engine.analyze import analyze, to_json
        from ..market import options as optc
        o = _opt(h, inst)
        an = analyze(optc.frame(o, tf, 5), tf)                 # no session levels, no registry, no playbook
        j = to_json(an) if an else {"tf": tf, "zones": [], "pools": [], "markers": [], "zigzag": [], "range": None}
        j["experimental"] = ("EXPERIMENTAL — structure on option candles is noisy: premium moves with IV and theta, "
                             "not only with order flow. Zones/structure for trading stay on the NIFTY index.")
        j["instrument"] = o.to_dict()
        return j
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
    h.recompute_all(after_repair=True)
    return h.status_view()


@app.get("/api/zones")
def zones(tf: str = Query("5m"), active: int = Query(0)):
    h = _h()
    an = h.analyses.get(tf)
    zs = h.zones.get(tf, [])
    if active and an is not None:
        nxt = int(an.bars.ts[-1]) + 1
        zs = [z for z in zs if z.active_for(nxt)]
    return {"tf": tf, "zones": [z.to_dict() for z in zs]}


@app.get("/api/delta")
def delta(tf: str = Query("5m")):
    h = _h()
    d = h.delta.get(tf) or {"tf": tf, "available": False, "events": [], "bars": []}
    return {**d, "status": h.delta_status()}


@app.get("/api/events")
def events(tf: str = Query("5m"), limit: int = Query(200, ge=1, le=1000)):
    from ..delta.engine import structure_events
    h = _h()
    an = h.analyses.get(tf)
    ev = (structure_events(an) if an else []) + list((h.delta.get(tf) or {}).get("events", []))
    for r in (h.flow.get(tf) or {}).values():
        ev += list(r.get("events", []))
    ev += list((h.proxy.get(tf) or {}).get("events", []))
    ev.sort(key=lambda e: e["ts"])
    zmap = {z.id: z.to_dict() for z in h.zones.get(tf, [])}
    out = []
    for e in ev[-limit:]:
        e = dict(e)
        if e.get("zone_id") in zmap:
            e["zone_record"] = zmap[e["zone_id"]]
        out.append(e)
    return {"tf": tf, "events": out}


@app.post("/api/delta/adapter")
async def delta_adapter(request: Request):
    body = await request.json()
    try:
        st = _h().set_adapter(str(body.get("name", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return st


@app.post("/api/delta/csv")
async def delta_csv(request: Request, bar_seconds: int = Query(60), ts_is: str = Query("end"),
                    misaligned: str = Query("floor"), inst: str = Query("NIFTY")):
    """ts_is=end (default): timestamps are candle CLOSE times (IST) and are stored as the candle start."""
    from ..delta.adapters import CsvError, parse_csv, store_csv
    from ..market import instruments as instr
    raw = await request.body()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "CSV larger than 20 MB")
    try:
        target = None if inst in ("NIFTY", "INDEX", "") else instr.parse_key(inst)
        rep = store_csv(parse_csv(raw.decode("utf-8", errors="replace"), bar_seconds, ts_is, misaligned), target)
    except (CsvError, ValueError) as e:
        raise HTTPException(400, str(e))
    h = _h()
    if h.delta_adapter.name != "csv":
        h.set_adapter("csv")
    else:
        h.recompute_all()
    return {"report": rep, "status": h.delta_status()}


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


# ── strike mode ──────────────────────────────────────────────────────────────
_last_select = {"t": 0.0}


@app.get("/api/crt")
def crt(tf: str = Query("5m")):
    """Setup D — CRT + TBS: every pair's CRT ranges, TBS / Model #1 / entry events and signals."""
    from .. import config as _cfg
    h = _h()
    pairs = [{**{k: v for k, v in r.items() if k != "signals"}, "signals": [s.to_dict() for s in r["signals"]]}
             for r in h.crt.values()]
    return {"pairs": pairs, "view": h.crt_view(tf),
            "rules": {k: v for k, v in _cfg.rules().get("crt_tbs", {}).items() if not k.startswith("_")}}


@app.get("/api/strikes")
def strikes():
    return _h().strikes_view()


@app.post("/api/strikes/select")
async def strikes_select():
    import time as _t
    h = _h()
    if _t.time() - _last_select["t"] < 5:
        raise HTTPException(429, "selector ran < 5 s ago — it reuses the stored chain, wait a moment")
    _last_select["t"] = _t.time()
    res = await h.reselect(force=True)
    return {"result": res, "strikes": h.strikes_view()}


@app.post("/api/strikes/manual")
async def strikes_manual(request: Request):
    from ..market import instruments as instr
    from ..strikes.selector import select_strikes
    h = _h()
    b = await request.json()
    right = str(b.get("right", "")).upper()
    if right not in ("CE", "PE"):
        raise HTTPException(400, "right must be CE or PE")
    if b.get("clear"):
        h.book.set_manual(right, None)
        await h.reselect(force=True)
        return h.strikes_view()
    try:
        inst = instr.resolve(instr.option(str(b.get("expiry") or h.sel_expiry()), float(b["strike"]), right),
                             h.chain.chains)
    except (KeyError, ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    row = instr.chain_row(inst, h.chain.chains) or {}
    rec = {"instrument": inst.to_dict(), "role": "manual", "ltp": row.get("ltp"), "delta": row.get("delta"),
           "gamma": row.get("gamma"), "theta": row.get("theta"), "vega": row.get("vega"), "iv": row.get("iv"),
           "oi": row.get("oi"), "fit": None, "rr": None, "why": f"{inst.label} chosen manually (pinned until cleared)."}
    h.book.set_manual(right, rec)
    h.book.log.append(f"manual: {inst.label}")
    import asyncio as _a
    _a.create_task(h.refresh_options())
    return h.strikes_view()


@app.post("/api/strikes/auto")
async def strikes_auto(request: Request):
    from .. import db
    h = _h()
    h.auto_strikes = bool((await request.json()).get("on", True))
    db.kv_set("auto_strikes", h.auto_strikes)
    return h.strikes_view()


@app.post("/api/strikes/expiry")
async def strikes_expiry(request: Request):
    from .. import db
    h = _h()
    e = str((await request.json()).get("expiry", ""))
    if e and e not in h.chain.chains:
        raise HTTPException(400, f"expiry {e} not loaded — choose one of {sorted(h.chain.chains)}")
    h.strike_expiry = e or None
    db.kv_set("strike_expiry", h.strike_expiry)
    h.book.manual = {"CE": None, "PE": None}
    await h.reselect(force=True)
    return h.strikes_view()


@app.post("/api/mode")
async def set_mode(request: Request):
    from .. import db
    h = _h()
    m = str((await request.json()).get("mode", "INDEX")).upper()
    if m not in ("INDEX", "OPTION_CHART"):
        raise HTTPException(400, "mode must be INDEX or OPTION_CHART")
    h.mode = m
    db.kv_set("inst_mode", m)
    return {"mode": m}


@app.post("/api/proxy")
async def set_proxy(request: Request):
    from .. import db
    h = _h()
    h.proxy_on = bool((await request.json()).get("enabled", False))
    db.kv_set("proxy_on", h.proxy_on)
    for tf in h.tfs:
        h.recompute_flow(tf)
    if h.analyses.get(h.primary):
        h.signals = h.evaluate_primary()
    return {"enabled": h.proxy_on, "label": "PROXY (not real delta)"}


@app.get("/api/proxy")
def get_proxy(tf: str = Query("5m")):
    h = _h()
    return h.proxy.get(tf) or {"enabled": h.proxy_on, "available": False, "events": [], "bars": [],
                               "label": "PROXY (not real delta)"}


@app.get("/api/flow")
def get_flow(tf: str = Query("5m")):
    """Strike delta per selected CE/PE (Delta unavailable unless a real source / CSV / mock is selected)."""
    return {"tf": tf, "flow": _h().flow.get(tf) or {}}


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
