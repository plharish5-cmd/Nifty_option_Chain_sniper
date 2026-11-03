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

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
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
    from ..engine import gann_pivots_gaps, dhan_algo_executor
    gann_eod_task = asyncio.create_task(gann_pivots_gaps.run_eod_recalc_scheduler())
    algo_task = asyncio.create_task(dhan_algo_executor.executor.start_loop(hub))
    try:
        yield
    finally:
        algo_task.cancel()
        dhan_algo_executor.executor.stop_loop()
        gann_eod_task.cancel()
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
def candles(tf: str = Query("5m"), days: int = Query(3, ge=1, le=400), inst: str = Query("NIFTY"),
            # Optional[str], NOT `str | None`: the runtime venv is Python 3.9 and FastAPI
            # EVALUATES annotations when it registers the route, so PEP 604 unions raise
            # TypeError at import and the service crash-loops. `from __future__ import
            # annotations` does not save you here — it defers the evaluation, it does not
            # make 3.9 able to perform it.
            asof: Optional[str] = Query(None, description="ISO date; returns the window ENDING "
                                                          "that day, from SQLite. Review mode.")):
    if tf not in TF_SECONDS:
        raise HTTPException(400, f"tf must be one of {list(TF_SECONDS)}")
    h = _h()
    if inst not in ("NIFTY", "INDEX", ""):
        from ..market import options as optc
        if tf == "1D":
            raise HTTPException(400, "option charts are intraday only")
        o = _opt(h, inst)
        return {"tf": tf, "instrument": o.to_dict(), "candles": [_cj(r) for r in optc.frame(o, tf, days)]}
    rows = h.candles.frame(h.nifty, tf, days=days, include_forming=not asof, asof=asof)
    return {"tf": tf, "candles": [_cj(r) for r in rows], "asof": asof}


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
    if j is None or (j.get("pa_advanced") and "sniper" not in j["pa_advanced"]):
        h.recompute(tf)
        j = h.json.get(tf)
    if j and "pcr_trinity" not in j and hasattr(h, "chain"):
        try:
            j["pcr_trinity"] = h.chain.get_pcr_trinity()
        except Exception as e:
            log.warning("enrich analysis pcr_trinity failed: %s", e)
    return j or {"tf": tf, "zones": [], "pools": [], "markers": [], "zigzag": [], "range": None}


@app.get("/api/signals")
def signals():
    h = _h()
    return {"tf": h.primary, "bias_tf": h.bias_tf, "signals": [s.to_dict() for s in h.today_signals()]}


@app.get("/api/expiries")
def expiries():
    return {"expiries": _h().chain.expiries}


@app.get("/api/chain")
def chain(
    expiry: Optional[str] = None,
    exp: Optional[str] = None,
    opt_expiry: Optional[str] = None,
    fut_contract: Optional[str] = None,
    fut_src: Optional[str] = None,
):
    h = _h()
    target_exp = opt_expiry or exp or expiry
    target_fut = fut_contract or fut_src or "combined"
    active_exp = target_exp or (h.chain.expiries[0] if h.chain.expiries else (sorted(h.chain.chains)[0] if h.chain.chains else None))
    ch = h.chain.chains.get(active_exp) if active_exp else None
    if not ch:
        return JSONResponse({"expiry": active_exp, "rows": [], "status": h.chain.status, "error": h.chain.last_error, "is_stale": True})
    live_spot = h.candles.last_price.get(h.nifty)
    is_stale = h.chain.is_stale(active_exp, live_spot)
    trinity = h.chain.get_pcr_trinity(active_exp, fut_src=target_fut, hub=h) if hasattr(h, "chain") else {}
    strike_map = ch.get("strike_map")
    if not strike_map and ch.get("rows"):
        mp = float(ch.get("max_pain") or 0.0)
        sp = float(ch.get("spot") or live_spot or 0.0)
        from ..market.chain import build_class
        strike_map = []
        for r in ch["rows"]:
            ce_x, pe_x = r.get("ce", {}), r.get("pe", {})
            ce_oi_val = int(round(ce_x.get("oi") or r.get("call_oi") or 0))
            ce_chg_val = int(round(ce_x.get("oi_chg") or r.get("call_oi_chg") or 0))
            pe_oi_val = int(round(pe_x.get("oi") or r.get("put_oi") or 0))
            pe_chg_val = int(round(pe_x.get("oi_chg") or r.get("put_oi_chg") or 0))
            ce_chg_f = float(ce_x.get("chg") or 0)
            pe_chg_f = float(pe_x.get("chg") or 0)
            ce_b = "Long Buildup" if ce_chg_val > 0 and ce_chg_f >= 0 else ("Short Buildup" if ce_chg_val > 0 else ("Short Covering" if ce_chg_val < 0 and ce_chg_f >= 0 else ("Long Unwinding" if ce_chg_val < 0 else "Neutral")))
            pe_b = "Long Buildup" if pe_chg_val > 0 and pe_chg_f >= 0 else ("Short Buildup" if pe_chg_val > 0 else ("Short Covering" if pe_chg_val < 0 and pe_chg_f >= 0 else ("Long Unwinding" if pe_chg_val < 0 else "Neutral")))
            dom_b = pe_b if abs(pe_chg_val) > abs(ce_chg_val) and pe_b != "Neutral" else (ce_b if ce_b != "Neutral" else "Neutral")
            strike_map.append({
                "strike": r["strike"],
                "call_oi": ce_oi_val,
                "call_oi_chg": ce_chg_val,
                "put_oi": pe_oi_val,
                "put_oi_chg": pe_chg_val,
                "buildup": dom_b,
                "max_pain": mp,
                "spot": sp,
            })
    return {
        **ch,
        "is_stale": is_stale,
        "spot_at_fetch": ch.get("spot_at_fetch", ch.get("spot")),
        "fetched_at": ch.get("fetched_at", ch.get("ts")),
        "pcr_trinity": trinity,
        "pcr_oi_series": trinity.get("pcr_oi_series", []),
        "expiries_meta": trinity.get("expiries_meta"),
        "strike_map": strike_map or [],
    }


@app.get("/api/oc/trinity")
@app.get("/api/chain/trinity")
def chain_trinity(
    expiry: Optional[str] = None,
    exp: Optional[str] = None,
    opt_expiry: Optional[str] = None,
    fut_contract: Optional[str] = None,
    fut_src: Optional[str] = None,
):
    h = _h()
    target_exp = opt_expiry or exp or expiry or "both"
    target_fut = fut_contract or fut_src or "combined"
    return h.chain.get_pcr_trinity(exp=target_exp, fut_src=target_fut, hub=h)


@app.get("/api/desk/stockmojo-oi-change")
def desk_stockmojo_oi_change(
    expiry: Optional[str] = None,
    exp: Optional[str] = None,
    opt_expiry: Optional[str] = None,
    fut_contract: Optional[str] = None,
    fut_src: Optional[str] = None,
    baseline_mode: Optional[str] = None,
):
    """
    Dedicated StockMojo-matching OI Change (Call vs Put) endpoint.
    Computes true Exchange ΔOI (oi - previous_eod_oi) with contract/expiry selectors
    and full checkpoint telemetry.
    """
    h = _h()
    target_exp = opt_expiry or exp or expiry or "cur"
    target_fut = fut_contract or fut_src or "combined"
    mode = baseline_mode or "exchange"
    if hasattr(h, "chain") and hasattr(h.chain, "get_stockmojo_oi_change"):
        return h.chain.get_stockmojo_oi_change(exp=target_exp, fut_src=target_fut, baseline_mode=mode, hub=h)
    return {"status": "error", "detail": "Chain engine unavailable", "series": []}


@app.get("/api/gamma-exposure-review")
async def gamma_exposure_review_endpoint(
    day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    expiry: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    tf: str = Query("5m"),
    end_ts: Optional[float] = Query(None, ge=0),
    sign: int = Query(1),
    study: bool = Query(False),
):
    """Independent, read-only gamma view and timestamp-aligned BOS context."""
    from ..oclab import gamma_exposure_review
    import json
    settings = json.loads((config.BACKEND_DIR.parent.parent / "analyst/analyst_config.json").read_text())["gamma_exposure"]
    if tf not in settings["timeframes"] or sign not in (-1, 1):
        raise HTTPException(status_code=422, detail="Unsupported gamma timeframe or sign convention")
    from datetime import date
    try:
        if day:
            date.fromisoformat(day)
        if expiry:
            date.fromisoformat(expiry)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session or expiry date")
    return await gamma_exposure_review(day, expiry, tf, end_ts, sign, study)


def _get_oi_walls_replay_mod():
    import importlib.util
    import sys
    mod = sys.modules.get("oi_walls_replay_cached")
    if mod is None:
        path = config.BACKEND_DIR.parent.parent / "analyst/oi_walls_replay.py"
        spec = importlib.util.spec_from_file_location("oi_walls_replay_cached", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["oi_walls_replay_cached"] = mod
    return mod


@app.get("/api/desk/oi-walls-replay")
def oi_walls_replay(day: str, at: Optional[int] = None, expiry: Optional[str] = None, mode: Optional[str] = None):
    """Read archived original daily OI inputs; never consult the current chain."""
    from datetime import date
    try:
        date.fromisoformat(day)
        if expiry:
            date.fromisoformat(expiry)
    except ValueError:
        raise HTTPException(422, "Invalid session or expiry date")

    mod = _get_oi_walls_replay_mod()

    if mode == "batch" or at is None:
        try:
            return mod.replay_batch(day, expiry)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:
            raise HTTPException(503, "Recorded OI batch replay unavailable") from exc

    try:
        return mod.replay(day, at, expiry)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(503, "Recorded OI replay unavailable") from exc


@app.get("/api/desk/oi-walls-replay-batch")
def oi_walls_replay_batch(day: str, expiry: Optional[str] = None):
    """Batch-cached retrieval of all recorded 1-minute OI frames and candles for instant slider scrubbing."""
    from datetime import date
    try:
        date.fromisoformat(day)
        if expiry:
            date.fromisoformat(expiry)
    except ValueError:
        raise HTTPException(422, "Invalid session or expiry date")

    mod = _get_oi_walls_replay_mod()
    try:
        return mod.replay_batch(day, expiry)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(503, "Recorded OI batch replay unavailable") from exc


@app.get("/api/desk/time-machine")
def desk_time_machine(day: Optional[str] = None, at: Optional[int] = None):
    import importlib.util
    import sys
    mod = sys.modules.get("desk_time_machine_cached")
    if mod is None:
        path = config.BACKEND_DIR.parent.parent / "analyst/desk_time_machine.py"
        spec = importlib.util.spec_from_file_location("desk_time_machine_cached", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["desk_time_machine_cached"] = mod
    try:
        return mod.metadata(day, at)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/chain/oi-studio")
@app.get("/api/chain/stockmojo-oi-studio")
def oi_studio_endpoint(
    expiry_mode: str = Query("this_wk"),
    day: Optional[str] = Query(None),
    start_ts: Optional[int] = Query(None),
    end_ts: Optional[int] = Query(None),
    strike_range: str = Query("10"),
    min_strike: Optional[float] = Query(None),
    max_strike: Optional[float] = Query(None),
    include_positioning: bool = Query(False),
    bucket_minutes: Optional[int] = Query(None),
):
    """StockMojo 1:1 Open Interest & Writer Map Studio telemetry."""
    from ..engine import oi_studio
    if include_positioning and bucket_minutes is not None and bucket_minutes not in oi_studio.get_cfg()["positioning"]["bucket_options"]:
        raise HTTPException(status_code=422, detail="Unsupported positioning bucket")
    if day is not None:
        from datetime import date
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(422, "Invalid session date")
    return oi_studio.get_oi_studio_data(
        expiry_mode=expiry_mode,
        session_day=day,
        start_ts=start_ts,
        end_ts=end_ts,
        strike_range=strike_range,
        min_strike=min_strike,
        max_strike=max_strike,
        include_positioning=include_positioning,
        bucket_minutes=bucket_minutes,
    )


@app.get("/api/chain/pece-matrix")
def pece_matrix(
    expiry: Optional[str] = None,
    tf: str = Query("3m"),
    scope: str = Query("atm_10"),
    force_expiry_mode: bool = Query(False),
):
    """Institutional PE-CE Difference & Skew Matrix 2.0 with Auto ATM ±10 / ±5 scope,
    center-zero diverging ledger series, magnitude regime sizing, slope-flip detection,
    equidistant premium skew, and Expiry 0-DTE Playbook integration."""
    from ..engine import pe_ce_matrix as matrix_mod
    return matrix_mod.compute_pe_ce_matrix(
        expiry=expiry,
        tf=tf,
        scope=scope,
        force_expiry_mode=force_expiry_mode,
    )


@app.get("/api/chain/expiry-playbook")
def expiry_playbook_endpoint(
    expiry: Optional[str] = None,
    force_expiry_mode: bool = Query(False),
    tf: str = Query("3m"),
):
    """Institutional Expiry-Day (Zero-DTE) Playbook Engine:
    Straddle Decay & Breakeven Box, Gamma Pin vs Squeeze Detector,
    Trapped Option Writer Radar, 5-Factor Hero-Zero Sniper Ticket,
    and 15:00–15:30 IST NSE Settlement VWAP Tracker."""
    from ..engine import expiry_playbook
    return expiry_playbook.compute_expiry_cockpit_telemetry(
        expiry=expiry,
        force_expiry_mode=force_expiry_mode,
        tf=tf,
    )


@app.get("/api/desk/bias")
def desk_bias_endpoint(
    opt_expiry: Optional[str] = Query(None),
    fut_contract: Optional[str] = Query(None),
):
    """LIVE DESK BIAS: Unified Institutional Flash-Card Synthesis.
    Integrates Trinity PCR, Writer Flow, Structural Walls, Matrix 2.0 Spikes, and Gamma Pin."""
    from ..engine import desk_bias
    h = _h()
    return desk_bias.compute_live_desk_bias(
        opt_expiry=opt_expiry,
        fut_contract=fut_contract,
        hub=h,
    )


@app.get("/api/desk/gap-confluence")
def gap_confluence_endpoint(
    gift_nifty: Optional[float] = Query(None, description="Optional custom GIFT Nifty LTP for pre-market simulation"),
    sim_gap_pts: Optional[float] = Query(None, description="Optional simulated gap in points (e.g. -100.0, +80.0)"),
    opt_expiry: Optional[str] = Query(None, description="Options expiry: 'cur', 'next', or 'both'"),
    fut_contract: Optional[str] = Query(None, description="Futures contract mode: 'near', 'next', or 'combined'"),
):
    """GIFT NIFTY GAP & CONFLUENCE SNIPER:
    Provides live & simulated pre-market comparison across GIFT Nifty, NIFTY 50 Spot,
    Near-Month Future, and Next-Month Future.
    Evaluates opening confluence, structural wall interaction (22,600 Put Wall vs 23,000 Call Wall),
    1m/3m rejection wicks, fresh writer positioning, and 09:15–09:45 AM execution plan."""
    from ..engine import gap_sniper
    # rules.json has carried gap_sniper.enabled since this was built and nothing read it, exactly as
    # with kronos.enabled. It is honoured here. This matters more than a hidden tab: the engine
    # reaches api.dhan.co with raw urllib on a 2s cache while the tab polls every 3s, OUTSIDE the
    # desk data budget. On 30 Oct that drew a Dhan 805 "Too many requests ... may result in the user
    # being blocked" at 08:24, fifty minutes before the open. Disabled means no request is made.
    if not bool((config.rules().get("gap_sniper") or {}).get("enabled", True)):
        return {
            "ok": False, "enabled": False,
            "message": ("Gap Sniper is switched off in rules.json (gap_sniper.enabled false). "
                        "No GIFT Nifty, Dhan or TradingView request is made while it is false."),
        }
    h = _h()
    return gap_sniper.compute_gap_confluence(
        gift_nifty=gift_nifty,
        sim_gap_pts=sim_gap_pts,
        opt_expiry=opt_expiry or "cur",
        fut_contract=fut_contract or "combined",
        hub=h,
    )


@app.get("/api/desk/gift-nifty-live")
def gift_nifty_live_endpoint():
    """Direct live quote feed for GIFT Nifty from Dhan WebSocket bridge or cache."""
    from ..engine.gift_nifty_feed import get_gift_nifty_bridge
    return get_gift_nifty_bridge().get_quote()


@app.get("/api/desk/cvd-quality")
def cvd_quality_endpoint():
    """Contract-bound shadow recorder quality; no live signal or order side effects."""
    from ..delta.recorder import provenance_status
    return provenance_status()


@app.get("/api/desk/cvd-divergence")
def cvd_divergence_endpoint(
    tf: str = Query("3m", pattern="^(3m|5m)$", description="Timeframe: '3m' or '5m'"),
):
    """CVD vs. VWAP Divergence Alert Engine:
    Tracks Cumulative Volume Delta (CVD) over 3m/5m intervals, detects
    Bearish (Short Trap) and Bullish (Dip Accumulation) divergences, and
    evaluates Session VWAP confluence for actionable entries, SL, and targets."""
    from ..engine import cvd_divergence
    return cvd_divergence.compute_cvd_divergence(tf=tf)


@app.get("/api/desk/institutional-levels")
def institutional_levels_endpoint(
    sec_id: str = Query("13", description="Security ID: 13 for NIFTY"),
    rvol: Optional[float] = Query(None, description="Optional override RVOL"),
    pe_ce_delta_l: Optional[float] = Query(None, description="Optional override Net PE-CE delta in Lakhs"),
):
    """Institutional ICT Reference Levels & True Breakout Engine:
    Computes Previous Day High (PDH), Previous Day Low (PDL), Session Open (09:15 AM IST),
    Midnight Open, and classifies Judas Swing Liquidity Sweeps vs True Breakout Expansions."""
    from ..engine import institutional_levels
    return institutional_levels.compute_institutional_levels(
        sec_id=sec_id,
        override_rvol=rvol,
        override_pe_ce_delta_l=pe_ce_delta_l,
    )


@app.get("/api/desk/gann-pivots-gaps")
def gann_pivots_gaps_endpoint(
    sec_id: str = Query("13", description="Security ID: 13 for NIFTY"),
    spot: Optional[float] = Query(None, description="Optional override current spot price"),
    target_date: Optional[str] = Query(None, description="Optional target date YYYY-MM-DD"),
    mode: str = Query("auto", description="intraday | next_day | auto (default auto switches at 15:40 IST)"),
):
    """Historical Monotonic R1-R4 / S1-S4, Unfilled Gaps, and Gann Sq9 + Time Cycles."""
    from ..engine import gann_pivots_gaps
    current_spot = spot
    if current_spot is None and hub is not None:
        try:
            q = hub.quote()
            if q and q.get("spot"):
                current_spot = float(q["spot"])
        except Exception:
            pass
    return gann_pivots_gaps.get_gann_pivots_gaps(
        sec_id=sec_id,
        current_spot=current_spot,
        target_date_str=target_date,
        mode=mode,
    )


@app.post("/api/desk/gann-pivots-gaps/recalculate")
async def gann_pivots_gaps_recalculate(
    sec_id: str = Query("13", description="Security ID: 13 for NIFTY"),
):
    """Triggers a full 5-year CSV sync, settles today's EOD OHLC bar, re-scans the full history from index 0,
    and regenerates NIFTY50_All_In_One_Master_View.xlsx."""
    from ..engine import gann_pivots_gaps
    return await asyncio.to_thread(gann_pivots_gaps.force_recalculate, sec_id=sec_id)


@app.get("/api/desk/htf-wick-ema-rb")
def htf_wick_ema_rb_endpoint(
    sec_id: str = Query("13", description="Security ID: 13 for NIFTY"),
    spot: Optional[float] = Query(None, description="Optional override current spot price"),
):
    """Institutional 1H / 4H Wick & Shaved Extremes + 1H 8-EMA first-touch bounce/rejection rules & backtest."""
    from ..engine import htf_wick_ema_rb
    current_spot = spot
    if current_spot is None and hub is not None:
        try:
            q = hub.quote()
            if q and q.get("spot"):
                current_spot = float(q["spot"])
        except Exception as e:
            log.warning("Could not get spot from hub for htf_wick_ema_rb: %s", e)
    return htf_wick_ema_rb.get_htf_wick_ema_rb(
        sec_id=sec_id,
        spot=current_spot,
    )


@app.get("/api/desk/itm-atm-oi-walls")
def itm_atm_oi_walls_endpoint(
    spot: Optional[float] = Query(None, description="Optional override current spot price"),
    gift_nifty: Optional[float] = Query(None, description="Optional GIFT Nifty price for basis calculator"),
):
    """Institutional ATM/ITM Writer Conviction, OTM Vacuum, Panic Unwinding, and Futures RSI Divergence."""
    from ..engine import itm_atm_oi_walls
    current_spot = spot
    if current_spot is None and hub is not None:
        try:
            q = hub.quote()
            if q and q.get("spot"):
                current_spot = float(q["spot"])
        except Exception as e:
            log.warning("Could not get spot from hub for itm_atm_oi_walls: %s", e)

    g_nifty = gift_nifty
    if g_nifty is None and hub is not None and getattr(hub, "gift_nifty", None):
        try:
            g_nifty = float(hub.gift_nifty.get("price", 0.0) or 0.0) or None
        except Exception:
            pass

    return itm_atm_oi_walls.compute_itm_atm_oi_walls(
        spot=current_spot,
        gift_nifty=g_nifty,
    )



@app.get("/api/desk/divergence")
def divergence_endpoint(
    tf: str = Query("3m", description="1m | 3m | 5m | 15m | 1h"),
    day_regime: str = Query("none", description='"bull" | "bear" | "none" — the desk\'s cumulative Net PE−CE regime'),
):
    """RSI/price divergence (watch -> trigger -> expire) plus the price/VIX context read.

    The RSI engine deliberately mirrors cvd_divergence.py: a divergence ARMS a watch with a trigger
    level and only becomes anything when price CLOSES through it. On 29 Oct the OI divergence fired
    five bearish warnings with no such gate on a session that ran +217 points; the CVD engine, which
    has the gate, armed twice and ended INVALIDATED and EXPIRED with no bad trade. Pass day_regime so
    a reversal watch pointing against a decided day is vetoed rather than surfaced.

    VIX is context only and never an entry; when the VIX series is missing it reports UNAVAILABLE
    rather than assuming a relationship.
    """
    from .. import db
    from ..engine import rsi_divergence, vix_divergence
    from ..timeutil import now_epoch, now_ist, session_bounds
    if tf not in rsi_divergence.BAR_SECONDS:
        raise HTTPException(400, f"bad tf: {tf}")

    h = _h()
    step = rsi_divergence.BAR_SECONDS[tf]
    t0, _t1 = session_bounds(now_ist().date())
    one_min = db.candles_1m(h.nifty, int(t0)) or []

    bars = rsi_divergence.aggregate_1m(one_min, step, int(now_epoch()))

    vix_bars = db.candles_1m(h.vix, int(t0)) or []
    return {
        "ok": True, "tf": tf, "day_regime": day_regime,
        "rsi": rsi_divergence.compute_rsi_divergence(bars, tf=tf, day_regime=day_regime),
        "vix": vix_divergence.compute_vix_divergence(bars, vix_bars),
    }


@app.get("/api/oi/matrix")
@app.get("/api/chain/oi-matrix")
def oi_matrix(
    expiry: Optional[str] = None,
    scope: str = Query("atm_10"),
):
    """Institutional Open Interest Matrix & Strike Map 2.0:
    Real-time strike-wise OI distribution, automated buildup classification
    (Long Buildup, Short Buildup, Short Covering, Long Unwinding),
    institutional colors (Put Support Emerald, Call Resistance Crimson),
    Max Pain & Spot Price overlays, and Market Sentiment Gauge."""
    h = _h()
    return h.chain.get_oi_matrix(exp=expiry, scope=scope)


@app.get("/api/smart_oi")
@app.get("/api/chain/smart_oi")
def smart_oi_endpoint(
    expiry: Optional[str] = None,
    exp: Optional[str] = None,
    opt_expiry: Optional[str] = None,
    fut_contract: Optional[str] = None,
    fut_src: Optional[str] = None,
    tf: str = Query("3m"),
    range_mode: str = Query("atm"),
    span: int = Query(10),
):
    """Smart OI Noise-Filtering & Candlestick Price-Action Confluence Engine:
    Excludes deep OTM retail noise and stale positioning, highlights institutional Signal Strikes,
    flags Bullish/Bearish Confluences and Divergence Traps/Absorptions directly on NIFTY candles,
    and returns multi-line time-series telemetry."""
    h = _h()
    candles = []
    target_exp = opt_expiry or exp or expiry or "cur"
    target_fut = fut_contract or fut_src or "combined"
    try:
        candles = h.candles.frame(h.nifty, tf, days=1, include_forming=True) if hasattr(h, "candles") else []
    except Exception as ce:
        log.warning("smart_oi candles fetch failed: %s", ce)
    return h.chain.get_smart_oi(
        exp=target_exp,
        fut_src=target_fut,
        tf=tf,
        range_mode=range_mode,
        span=span,
        candles=candles,
        hub=h,
    )


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
def alerts(grade: Optional[str] = None, limit: int = 500):
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

    # Normalize bar timestamp to true UTC epoch seconds (detecting double +5h30m / +19800s shift)
    now_e = int(now_epoch())
    if ts - now_e > 10000:
        ts -= 19800
    elif title:
        import re
        from ..timeutil import to_ist
        m = re.search(r'\b(\d{2}:\d{2})\b', title)
        if m:
            target_hm = m.group(1)
            if to_ist(ts - 19800).strftime("%H:%M") == target_hm and to_ist(ts).strftime("%H:%M") != target_hm:
                ts -= 19800
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
def footprints(tf: str = Query("5m"), n: int = Query(16), cover: str = Query("")):
    """ESTIMATED footprint ladder per closed candle from the desk's own recorded futures ticks (observation only).

    cover="session" asks for enough bars to cover whole sessions rather than a flat count. A fixed count
    truncates by timeframe — 30 bars is 7h30 on 15m but only 2h30 on 5m, which is why the 5m heatmap used
    to begin at 13:00 while 15m showed the whole day from the very same recorded ticks. footprint_levels is
    never pruned, so earlier sessions are genuinely there to be read."""
    if tf not in ("1m", "3m", "5m", "15m", "30m", "1h"):
        raise HTTPException(400, "tf must be 1m, 3m, 5m, 15m, 30m or 1h")
    from ..engine import footprints as fp
    from ..timeutil import TF_SECONDS, now_ist, session_bounds
    fcfg = config.rules().get("footprints", {})
    h = _h()
    days = 3 if tf in ("1m", "3m", "5m", "15m") else 5
    if cover == "session":
        t0, t1 = session_bounds(now_ist().date())
        sess_bars = max(1, -(-(int(t1) - int(t0)) // TF_SECONDS.get(tf, 300)))      # ceil
        n = sess_bars * max(1, int(fcfg.get("heat_sessions", 2)))
        # Calendar days, not sessions: a weekend or a holiday must not silently cost us a session.
        days = max(days, int(fcfg.get("history_days", 8)))
    n_bars = max(4, min(int(n), int(fcfg.get("max_bars", 220))))
    zones = ((h.json.get("5m") or {}).get("zones") or []) + ((h.json.get("15m") or {}).get("zones") or [])
    return fp.compute(h.rows_1m(days), tf, n_bars, float(config.rule("delta", "fut_level_step", 5.0)),
                      zones, fcfg)


@app.get("/api/qml")
def qml(tf: str = Query("5m")):
    """Line-chart QML · RBS / SBR · SNR fakeouts for one timeframe (observation only)."""
    j = _h().json.get(tf)
    if not j:
        return {"tf": tf, "available": False, "message": "no closed candles analysed yet for this timeframe"}
    return {"tf": tf, "available": True, **(j.get("qml") or {"setups": [], "markers": []})}


@app.get("/api/prahaar")
def prahaar_view(tf: str = Query("5m")):
    """Prahaar Indicator & ATM Option-Buying Directional Engine. Information and alert only."""
    h = _h()
    j = h.json.get(tf) or {}
    p = j.get("prahaar")
    if p:
        return p
    an = h.analyses.get(tf)
    if not an:
        return {"tf": tf, "available": False, "message": f"Analysis for {tf} not ready"}
    from ..engine import prahaar as prahaar_mod
    vix = h.candles.last_price.get(h.vix)
    vpc = h.prev_close.get(h.vix)
    vix_chg = round(vix - vpc, 2) if vix and vpc else None
    return prahaar_mod.compute_prahaar(
        an.bars, an.atr, tf=tf, rules_cfg=config.rules().get("prahaar", {}),
        vix=vix, vix_chg=vix_chg
    )


@app.get("/api/gtf")
def gtf_view(tf: str = Query("5m"), mode: str = Query("conservative")):
    """GTF Institutional Demand & Supply Zones (v2.0 Equivalent). Information and alert only."""
    h = _h()
    j = h.json.get(tf) or {}
    g = j.get("gtf")
    if g and g.get("marking_mode") == mode.lower():
        return g
    an = h.analyses.get(tf)
    if not an:
        return {"tf": tf, "available": False, "message": f"Analysis for {tf} not ready"}
    from ..engine import gtf as gtf_mod
    return gtf_mod.compute_gtf(
        an.bars, an.atr, tf=tf, rules_cfg=config.rules().get("gtf", {}),
        marking_mode=mode
    )


@app.get("/api/oclab-summary")
async def oclab_summary():
    """PE − CE table + writers-flow, condensed into narrative lines and story beats."""
    from ..oclab import summary
    return await summary()


@app.get("/api/time-series-chain")
async def time_series_chain(
    day: Optional[str] = Query(None), expiry: Optional[str] = Query(None),
    tf: Optional[str] = Query(None), mode: Optional[str] = Query(None),
    columns: Optional[int] = Query(None), range_n: Optional[int] = Query(None),
    selection: Optional[str] = Query(None), custom: str = Query(""),
    end: Optional[str] = Query(None), signals: bool = Query(True), settings: str = Query("{}"),
):
    """8501 Time-series Chain, using its unchanged engines in a separate read-only adapter."""
    import json
    from ..oclab import time_series_chain as export
    try:
        opts = json.loads(settings)
        if not isinstance(opts, dict) or set(opts) - {"full", "day", "pct", "lot", "hl", "font"}:
            raise ValueError("Invalid settings")
        if any(not isinstance(value, bool) for key, value in opts.items() if key != "font"):
            raise ValueError("Invalid settings value")
        strikes = [int(k) for k in custom.split(",") if k.strip()]
        if day:
            from datetime import date
            date.fromisoformat(day)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    params = {k: v for k, v in dict(day=day, expiry=expiry, tf=tf, mode=mode, columns=columns,
              range_n=range_n, selection=selection, end=end).items() if v is not None}
    params.update(custom=strikes, signals=signals, settings=opts)
    return await export(params)


@app.get("/api/oclab/{what}")
async def oclab(
    what: str,
    tf: str = Query("3m"),
    mode: str = Query("auto"),
    n: int = Query(10),
    day: Optional[str] = Query(None),
    opt_expiry: Optional[str] = Query(None),
    fut_contract: Optional[str] = Query(None),
):
    """Options Lab views from the OC app (PE − CE OI table, OI-change writer flow). Fresh on every new snapshot."""
    if what not in ("pece", "flow", "gex") or mode not in ("auto", "all") or tf not in ("1m", "3m", "5m", "15m", "1h"):
        raise HTTPException(400, "bad parameters")

    if what == "flow":
        h = _h()
        if hasattr(h, "chain") and hasattr(h.chain, "get_writer_flow"):
            scope_val = "atm_10" if mode == "auto" and n == 10 else ("atm_5" if mode == "auto" and n == 5 else "all")
            return h.chain.get_writer_flow(exp=opt_expiry, fut_src=fut_contract or "combined", tf=tf, hub=h, scope=scope_val)

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


@app.get("/api/pa-advanced")
def pa_advanced(tf: str = Query("5m")):
    """10 Advanced Price Action & Institutional Concepts (observation only — alert/inspection)."""
    h = _h()
    res = h.pa_advanced.get(tf)
    if not res:
        j = h.json.get(tf)
        if j and "pa_advanced" in j:
            res = j["pa_advanced"]
    if not res:
        h.recompute(tf)
        res = h.pa_advanced.get(tf)
        if not res and h.json.get(tf):
            res = h.json.get(tf).get("pa_advanced")
    if not res:
        return {"tf": tf, "available": False, "message": "no closed candles analysed yet for this timeframe"}
    from ..engine.pa_advanced import _clean_json, _synthesize_sniper_view
    try:
        cur = h.book.current() or {}
        foci_list = h.foci() if hasattr(h, "foci") else []
        foci_map = {f.get("label", "").split()[-1]: f for f in foci_list}
        ce_rec = cur.get("CE") or {}
        pe_rec = cur.get("PE") or {}
        res["strikes"] = {
            "CE": {
                "strike": ce_rec.get("instrument", {}).get("strike"),
                "label": ce_rec.get("instrument", {}).get("label"),
                "ltp": ce_rec.get("ltp"),
                "delta": ce_rec.get("delta"),
                "theta": ce_rec.get("theta"),
                "iv": ce_rec.get("iv"),
                "oi": ce_rec.get("oi"),
                "focus": foci_map.get("CE"),
            } if ce_rec else None,
            "PE": {
                "strike": pe_rec.get("instrument", {}).get("strike"),
                "label": pe_rec.get("instrument", {}).get("label"),
                "ltp": pe_rec.get("ltp"),
                "delta": pe_rec.get("delta"),
                "theta": pe_rec.get("theta"),
                "iv": pe_rec.get("iv"),
                "oi": pe_rec.get("oi"),
                "focus": foci_map.get("PE"),
            } if pe_rec else None,
        }
        an = h.analyses.get(tf)
        if an and hasattr(an, "bars") and len(an.bars) > 0:
            closed_bars = [
                {"ts": int(an.bars.ts[i]), "o": float(an.bars.o[i]), "h": float(an.bars.h[i]), "l": float(an.bars.l[i]), "c": float(an.bars.c[i])}
                for i in range(len(an.bars))
            ]
            atr_val = float(an.atr[-1]) if len(an.atr) else 15.0
            res["sniper"] = _synthesize_sniper_view(res, closed_bars, atr_val, float(an.bars.c[-1]))
    except Exception as se:
        import logging
        logging.getLogger("uvicorn.error").warning("pa_advanced strikes enrichment failed: %s", se)

    return _clean_json(res)


@app.get("/api/pa-course")
def pa_course(tf: str = Query("5m")):
    """Institutional Price Action Course Engine (Ch. 2–20 standalone evaluator)."""
    from ..engine import pa_course_engine
    from ..engine.pa_advanced import _clean_json
    h = _h()
    rows = h.candles.frame(h.nifty, tf, days=5, include_forming=False)
    spot = h.candles.last_price.get(h.nifty)
    exp = h.sel_expiry()
    ch = h.chain.chains.get(exp) if exp else None
    return _clean_json(pa_course_engine.analyze_pa_course(rows, tf=tf, spot=spot, chain_rows=ch))


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


# ── Custom Dhan Algo Execution Engine Endpoints ──────────────────────────────
class AlgoConfigIn(BaseModel):
    execution_mode: Optional[str] = None
    strategies: Optional[Dict[str, bool]] = None
    max_trades_per_day: Optional[int] = None
    lots_per_trade: Optional[int] = None
    max_daily_loss_inr: Optional[float] = None


class ArmSlIn(BaseModel):
    spot_sl_pts: Optional[float] = None
    broker_sl_mode: Optional[str] = None
    broker_sl_pts: Optional[float] = None
    broker_sl_type: Optional[str] = None
    broker_sl_limit_buffer_pts: Optional[float] = None
    exchange_sl_armed: Optional[bool] = None


class TargetModeIn(BaseModel):
    mode: str


class SlotToggleIn(BaseModel):
    slot_id: str
    enabled: bool


@app.get("/api/algo/status")
def algo_status_endpoint():
    """Custom Dhan API Algo Execution Engine status, active position, and kill-switch state."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.get_status_view()


@app.post("/api/algo/config")
def algo_config_endpoint(cfg: AlgoConfigIn):
    """Updates mode (PAPER | SEMI_AUTO | FULL_AUTO), strategy toggles, lots, and risk limits."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.update_config(
        execution_mode=cfg.execution_mode,
        strategies=cfg.strategies,
        max_trades_per_day=cfg.max_trades_per_day,
        lots_per_trade=cfg.lots_per_trade,
        max_daily_loss_inr=cfg.max_daily_loss_inr,
    )


@app.post("/api/algo/exit")
async def algo_manual_exit_endpoint():
    """Manual emergency exit for active position."""
    from ..engine import dhan_algo_executor
    return await dhan_algo_executor.executor.manual_exit_now()


@app.post("/api/algo/flatten-and-lock")
async def algo_flatten_and_lock_endpoint():
    """Emergency Panic Button: Flatten all open positions and lock executor for session."""
    from ..engine import dhan_algo_executor
    return await dhan_algo_executor.executor.flatten_all_and_lock()


@app.post("/api/algo/execute-pending")
async def algo_execute_pending_endpoint():
    """1-Click Execute pending signal in SEMI_AUTO mode."""
    from ..engine import dhan_algo_executor
    return await dhan_algo_executor.executor.execute_pending_signal()


@app.post("/api/algo/dismiss-pending")
def algo_dismiss_pending_endpoint():
    """Dismiss pending signal in SEMI_AUTO mode."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.dismiss_pending_signal()


@app.post("/api/algo/unlock")
def algo_unlock_endpoint():
    """Reset daily lock for algo executor."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.unlock_engine()


@app.get("/api/algo/test-connection")
async def algo_test_connection_endpoint():
    """Live Dhan API connectivity test & ATM Strike SecurityID resolution."""
    from ..engine import dhan_algo_executor
    return await dhan_algo_executor.executor.test_dhan_connection()


@app.post("/api/algo/backfill")
async def algo_backfill_endpoint(fromDate: str = "2026-10-25", toDate: str = "2026-11-02"):
    """Pings Dhan Historical Intraday API to backfill 1m candles into SQLite."""
    from ..engine import htf_wick_ema_rb
    return await asyncio.to_thread(htf_wick_ema_rb.backfill_dhan_intraday, "13", "IDX_I", "INDEX", "1", fromDate, toDate)


@app.get("/api/algo/today-audit")
def algo_today_audit_endpoint(date: str = "2026-11-02"):
    """Returns today's 1H 8-EMA support bounce zero-lookahead audit."""
    from ..engine import htf_wick_ema_rb
    return htf_wick_ema_rb.audit_today_ema_bounce(date_str=date)


@app.get("/api/algo/premarket-check")
async def algo_premarket_check_endpoint():
    """Interactive 6-point pre-market diagnostic checklist & broker protection verification."""
    from ..engine import dhan_algo_executor
    import app.api.main as _m
    h = getattr(_m, "hub", None)
    return await dhan_algo_executor.executor.run_premarket_check(hub=h)


@app.post("/api/algo/arm-sl")
def algo_arm_sl_endpoint(payload: ArmSlIn):
    """Configures dual-layer stop loss: local spot tracker and exchange-side SL-M / SL-L."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.arm_stop_loss(
        spot_sl_pts=payload.spot_sl_pts,
        broker_sl_mode=payload.broker_sl_mode,
        broker_sl_pts=payload.broker_sl_pts,
        broker_sl_type=payload.broker_sl_type,
        broker_sl_limit_buffer_pts=payload.broker_sl_limit_buffer_pts,
        exchange_sl_armed=payload.exchange_sl_armed,
    )


@app.post("/api/algo/target-mode")
def algo_target_mode_endpoint(payload: TargetModeIn):
    """Sets dynamic target engine mode (DYNAMIC, FIXED, TRAIL_RUNNER)."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.set_target_mode(payload.mode)


@app.get("/api/algo/smart-target-preview")
def algo_smart_target_preview_endpoint(option_type: str = "CE", mode: Optional[str] = None):
    """Live confluence target preview: checks Net PE-CE, Call/Put Writer walls, and S/R levels."""
    from ..engine import dhan_algo_executor
    import app.api.main as _m
    h = getattr(_m, "hub", None)
    spot = dhan_algo_executor.executor._last_evaluated_spot or 23440.8
    net_pe_ce = 0.0
    if h and hasattr(h, "get_live_writer_flow_state"):
        try:
            _, net_pe_ce = h.get_live_writer_flow_state()
        except Exception:
            pass
    return dhan_algo_executor.executor.compute_smart_target(
        spot=spot,
        option_type=option_type,
        net_pe_ce=net_pe_ce,
        hub=h,
        mode=mode,
    )


@app.get("/api/algo/strategy-slots")
def algo_strategy_slots_endpoint():
    """Lists modular strategy expansion slots."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.get_strategy_slots()


@app.post("/api/algo/strategy-slot/toggle")
def algo_strategy_slot_toggle_endpoint(payload: SlotToggleIn):
    """Toggles or configures a strategy expansion slot."""
    from ..engine import dhan_algo_executor
    return dhan_algo_executor.executor.toggle_strategy_slot(payload.slot_id, payload.enabled)


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
# Registered BEFORE the SPA catch-all below, or /api/setups/* would be swallowed by it and served
# index.html instead of JSON.
from .setup_log_api import router as _setup_log_router
app.include_router(_setup_log_router)

# Kronos NIFTY forecast tab. Registered BEFORE the SPA catch-all below, or /api/kronos/* would be
# swallowed by it and served index.html.
from .kronos_api import router as _kronos_router
app.include_router(_kronos_router)
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
