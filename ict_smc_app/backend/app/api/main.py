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
from .hub import Hub, _cj, _jdefault

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")
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
        return JSONResponse({"expiry": exp, "rows": [], "status": h.chain.status, "error": h.chain.last_error, "is_stale": True})
    live_spot = h.candles.last_price.get(h.nifty)
    is_stale = h.chain.is_stale(exp, live_spot)
    return {**ch, "is_stale": is_stale, "spot_at_fetch": ch.get("spot_at_fetch", ch.get("spot")),
            "fetched_at": ch.get("fetched_at", ch.get("ts"))}


@app.get("/api/dashboard")
def dashboard():
    return _h().dashboard()


@app.post("/api/alerts/test-rvol")
async def test_rvol_alert():
    """Sends a TEST RVOL alert through the real path: alert log → live push (sound / voice / desktop) → Telegram (if on)."""
    from ..signals import alerts as _al
    from ..timeutil import chart_time, hm, now_epoch
    h = _h()
    t = int(now_epoch())
    e = {"kind": "RVOL_ACTIVATED", "ts": t, "value": 1.8, "time": chart_time(t), "at": hm(t), "key": f"RVOL|TEST|{t}",
         "title": "TEST — RVOL ACTIVATED 1.80× (this is a test)",
         "body": "If you hear a chime + voice and see a desktop notification (and Telegram, if enabled), RVOL alerts work."}
    out = await _al.notify_event(h.alerts, e)
    return {"sent": bool(out)}


@app.get("/api/alerts")
def alerts(grade: Optional[str] = None, limit: int = 150):
    return {"alerts": alerts_mod.recent(limit, grade)}


@app.post("/api/alerts")
async def record_client_alert(req: Request):
    """Stores a client-side signal or alert in the persistent SQLite alerts table."""
    import json
    from .. import db
    from ..timeutil import now_epoch
    try:
        body = await req.json()
    except Exception as e:
        log.warning("invalid alert json: %s", e)
        return {"ok": False, "error": "invalid json"}
    ts = int(body.get("ts") or now_epoch())
    tf = str(body.get("tf") or "5m")
    kind = str(body.get("kind") or "SIG")
    side = str(body.get("side") or "")
    grade = str(body.get("grade") or "info")
    title = str(body.get("title") or "")
    text = str(body.get("body") or "")
    price = float(body.get("price") or 0.0)
    key = str(body.get("key") or f"{tf}|{ts}|{kind}|{side}")

    row = {
        "ts": ts,
        "key": key,
        "tf": tf,
        "kind": kind,
        "side": side,
        "grade": grade,
        "title": title,
        "body": text,
        "price": price,
        "payload": json.dumps(body.get("payload") or {}),
    }
    db.execute(
        "INSERT OR IGNORE INTO alerts (ts,key,tf,kind,side,grade,title,body,price,payload) "
        "VALUES (:ts,:key,:tf,:kind,:side,:grade,:title,:body,:price,:payload)",
        row,
    )
    return {"ok": True, "key": key}


@app.get("/api/paper")
def paper_view():
    return {"trades": paper.trades(100), "stats": paper.stats(), "open": paper.open_trade(),
            "open_all": paper.open_trades(), "scorecard": paper.scorecard()}


@app.get("/api/paper/scorecard")
def paper_scorecard():
    return paper.scorecard()


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


@app.get("/api/replay")
def replay(from_seq: int = Query(..., ge=0)):
    h = _h()
    return {"from_seq": from_seq, "messages": h.get_replay(from_seq)}


@app.websocket("/ws")
async def ws(websocket: WebSocket, format: Optional[str] = Query(None)):
    h = _h()
    proto = websocket.headers.get("sec-websocket-protocol", "")
    requested_protos = [p.strip() for p in proto.split(",") if p.strip()]
    client_fmt = (format or "").lower()
    chosen_subprotocol = None
    if client_fmt in ("msgpack", "binary") or "msgpack" in requested_protos:
        client_fmt = "msgpack"
        if "msgpack" in requested_protos:
            chosen_subprotocol = "msgpack"
    elif "json" in requested_protos:
        client_fmt = "json"
        chosen_subprotocol = "json"
    else:
        client_fmt = "json"

    await websocket.accept(subprotocol=chosen_subprotocol)
    h.clients.add(websocket)
    h.client_formats[websocket] = client_fmt
    try:
        init_quote = {"type": "quote", "quote": h.quote(), "seq": h._ws_seq}
        if client_fmt == "msgpack":
            try:
                import msgpack
                await websocket.send_bytes(msgpack.packb(init_quote, default=_jdefault, use_bin_type=True))
            except WebSocketDisconnect:
                raise
            except Exception:
                await websocket.send_json(init_quote)
        else:
            await websocket.send_json(init_quote)
        while True:
            msg = await websocket.receive()
            # browser pings or control / replay messages
            text = msg.get("text")
            raw_bytes = msg.get("bytes")
            req = None
            if text:
                try:
                    import json
                    req = json.loads(text)
                except Exception:
                    pass
            elif raw_bytes:
                try:
                    import msgpack
                    req = msgpack.unpackb(raw_bytes)
                except Exception:
                    pass
            if isinstance(req, dict) and req.get("action") == "replay":
                from_seq = int(req.get("from_seq", 0))
                missed = h.get_replay(from_seq)
                for m in missed:
                    if client_fmt == "msgpack":
                        try:
                            import msgpack
                            await websocket.send_bytes(msgpack.packb(m, default=_jdefault, use_bin_type=True))
                        except WebSocketDisconnect:
                            raise
                        except Exception:
                            await websocket.send_json(m)
                    else:
                        await websocket.send_json(m)
    except WebSocketDisconnect:
        pass
    finally:
        h.clients.discard(websocket)
        h.client_formats.pop(websocket, None)


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


@app.get("/api/crt-phase")
def crt_phase(keep: int = Query(4)):
    """DISPLAY-ONLY CRT phase cockpit: where each Setup D range is in its 3-candle story. Changes nothing."""
    from ..engine import crt_phase as cp
    h = _h()
    absorb = {int(e["ts"]) for d in h.delta.values() for e in (d.get("events") or [])
              if e.get("event_type") == "DELTA_ABSORPTION"}
    try:
        strikes = h.book.current()
    except Exception:                                        # noqa: BLE001
        strikes = None
    out, errs = [], []
    for r in list(h.crt.values()):
        try:
            ltf = r.get("ltf") or "1m"
            rows = h.candles.frame(h.nifty, ltf, days=2, include_forming=False)
            rv = h.rvol_tf(ltf) if h.mapped_1m() else {}
            out += cp.phases(r, rows, rv, absorb, strikes, max(1, min(int(keep), 10)))
        except Exception as e:                               # noqa: BLE001
            errs.append(f"{r.get('pair')}: {e}")
    # D.O.L (draw on liquidity): after the ERL sweep the next target is the nearest unfilled FVG (IRL) in the trade
    # direction on the CRT's lower timeframe. Display only — Setup D's own TP-1 (50 % CRM) / TP-2 are unchanged.
    for row in out:
        try:
            zs = [z for z in ((h.json.get(row["ltf"]) or {}).get("zones") or [])
                  if z.get("type") in ("FVG", "IFVG") and z.get("state") not in ("filled", "invalid")]
            sell = row["trade"]["side"].startswith("SHORT") if row.get("trade") else row.get("bias") == "bear"
            ref = row["trade"]["entry"] if row.get("trade") else (row["sweep"]["price"] if row.get("sweep") else None)
            if ref is None or row.get("bias") == "both" and not row.get("trade"):
                continue
            cand = [z for z in zs if (z["top"] < ref if sell else z["bottom"] > ref)]
            if cand:
                z = max(cand, key=lambda z: z["top"]) if sell else min(cand, key=lambda z: z["bottom"])
                row["dol"] = {"kind": "IRL", "lo": round(z["bottom"], 1), "hi": round(z["top"], 1),
                              "text": f"D.O.L → {z['type']} {z['bottom']:,.0f}–{z['top']:,.0f} ({abs(ref - (z['top'] if sell else z['bottom'])):,.0f} pts)"}
        except Exception:                                    # noqa: BLE001
            pass
    rank = {"TRADE LIVE": 0, "WATCH — sweep done, waiting for the LTF MSS": 1, "WAITING for the manipulation candle": 2}
    out.sort(key=lambda x: (rank.get(x["ready"], 3), -x["k_ts"]))
    return {"rows": out, "errors": errs, "order": cp.ORDER}


_SMT_CACHE: dict = {}


@app.get("/api/smt")
async def smt_view(tf: str = Query("5m")):
    """SMT divergence NIFTY vs BANKNIFTY (index, closed candles only). Information only."""
    import time as _t
    from datetime import timedelta
    from ..engine import smt as smt_mod
    from ..market.candles import resample, session_filter
    from ..timeutil import TF_SECONDS as TS, now_ist
    if tf not in ("5m", "15m", "1h"):
        raise HTTPException(400, "tf must be 5m, 15m or 1h")
    h = _h()
    days = 3 if tf != "1h" else 6
    c = _SMT_CACHE.get(tf)
    if not c or _t.time() - c[0] > 60:
        try:
            to = now_ist()
            raw = await h.rest.intraday("25", to - timedelta(days=days + 3), to, "1", "IDX_I", "INDEX")
            _SMT_CACHE[tf] = c = (_t.time(), raw)
        except Exception as e:                               # noqa: BLE001
            return {"tf": tf, "available": False, "message": f"BANKNIFTY candles unavailable: {e}", "markers": []}
    now = _t.time()
    bank = [b for b in resample(session_filter(c[1]), tf) if int(b["ts"]) + TS[tf] <= now]
    nifty = h.candles.frame(h.nifty, tf, days=days, include_forming=False)
    r = smt_mod.detect(nifty, bank)
    from ..timeutil import chart_time
    line = [{"time": chart_time(int(b["ts"])), "c": round(float(b["c"]), 2), "h": float(b["h"]), "l": float(b["l"])} for b in bank]
    return {"tf": tf, "available": True, "bank": line, **r}


@app.get("/api/cct")
def cct_view(tf: str = Query("5m")):
    """Candle Continuity Theory (CCT) & 4-Candle Execution Triggers. Information only."""
    h = _h()
    j = h.json.get(tf) or {}
    c = j.get("cct")
    if c:
        return c
    an = h.analyses.get(tf)
    if not an:
        return {"tf": tf, "available": False, "message": f"Analysis for {tf} not ready"}
    from ..engine import cct as cct_mod
    return cct_mod.compute_cct(an.bars, an.atr, an.swings, an.fvgs, an.obs, an.sess, tf, config.rules().get("cct", {}))


@app.get("/api/mmxm")
def mmxm_view(tf: str = Query("5m")):
    """Market Maker Models (MMBM / MMSM) State Engine & Target Tracker. Information only."""
    h = _h()
    j = h.json.get(tf) or {}
    m = j.get("mmxm")
    if m:
        return m
    an = h.analyses.get(tf)
    if not an:
        return {"tf": tf, "available": False, "message": f"Analysis for {tf} not ready"}
    from ..engine import mmxm as mmxm_mod
    return mmxm_mod.compute_mmxm(an.bars, an.atr, an.swings, an.events, an.fvgs, an.obs, tf, config.rules().get("mmxm", {}), j.get("smt", {}).get("markers") if isinstance(j.get("smt"), dict) else None)


@app.get("/api/volume")
def volume(tf: str = Query("5m")):
    """Real volume from the current-month NIFTY future (basis-adjusted): profiles, VWAP, AVWAPs, RVOL, activity."""
    h = _h()
    v = h.volume.get(tf)
    if not v:
        return {"tf": tf, "available": False, "message": f"NIFTY future: {h.fut_status}"}
    return {"tf": tf, **v, "futures": h.fut_status, "recorder": h.recorder.status()}


@app.get("/api/footprints")
def footprints(tf: str = Query("5m"), n: int = Query(16)):
    """ESTIMATED footprint ladder per closed candle from the desk's own recorded futures ticks (observation only)."""
    if tf not in ("5m", "15m", "30m", "1h"):
        raise HTTPException(400, "tf must be 5m, 15m, 30m or 1h")
    from ..engine import footprints as fp
    h = _h()
    days = 3 if tf in ("5m", "15m") else 5
    zones = ((h.json.get("5m") or {}).get("zones") or []) + ((h.json.get("15m") or {}).get("zones") or [])
    return fp.compute(h.rows_1m(days), tf, max(4, min(int(n), 80)), float(config.rule("delta", "fut_level_step", 5.0)),
                      zones, config.rules().get("footprints", {}))


@app.get("/api/qml")
def qml(tf: str = Query("5m")):
    """Line-chart QML · RBS / SBR · SNR fakeouts for one timeframe (observation only)."""
    j = _h().json.get(tf)
    if not j:
        return {"tf": tf, "available": False, "message": "no closed candles analysed yet for this timeframe"}
    return {"tf": tf, "available": True, **(j.get("qml") or {"setups": [], "markers": []})}


@app.get("/api/oclab-summary")
async def oclab_summary():
    """PE − CE table + writers-flow, condensed into narrative lines and story beats."""
    from ..oclab import summary
    return await summary()


@app.get("/api/oclab/{what}")
async def oclab(what: str, tf: str = Query("3m"), mode: str = Query("auto"), n: int = Query(10), day: Optional[str] = Query(None)):
    """Options Lab views from the OC app (PE − CE OI table, OI-change writer flow). Fresh on every new snapshot."""
    if what not in ("pece", "flow", "gex") or mode not in ("auto", "all") or tf not in ("1m", "3m", "5m", "15m", "1h"):
        raise HTTPException(400, "bad parameters")
    from ..oclab import run
    return await run(what, tf, mode, max(3, min(int(n), 25)), day)


@app.get("/api/auction")
def auction(tf: str = Query("5m")):
    """Auction read (observation only): TPO profile, balance boxes, accept / reject events, the 5 questions."""
    from .. import config as _cfg
    h = _h()
    a = h.auction.get(tf)
    if not a:
        return {"tf": tf, "available": False, "message": "no closed candles analysed yet for this timeframe"}
    return {"available": True, **a,
            "rules": {k: v for k, v in _cfg.rules().get("auction", {}).items() if not k.startswith("_")}}


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


from .nodes_api import router as _nodes_router
app.include_router(_nodes_router)

from .book_api import router as _book_router
app.include_router(_book_router)

from .mtf_api import router as _mtf_router
app.include_router(_mtf_router)
from .combined_api import router as _combined_router
app.include_router(_combined_router)
from .option_tape_api import router as _opt_tape_router
app.include_router(_opt_tape_router)
# ── the built frontend ───────────────────────────────────────────────────────
if (config.FRONTEND_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(config.FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        root = config.FRONTEND_DIST.resolve()
        f = (root / path).resolve()
        if path and f.is_file() and root in f.parents:            # no path traversal out of dist/
            return FileResponse(str(f))
        # never cache index.html — it names the current JS bundle, so a cached copy would keep loading an old chart
        return FileResponse(str(config.FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-store, must-revalidate"})
