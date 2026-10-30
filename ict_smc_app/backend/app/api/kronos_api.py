"""
api/kronos_api.py — /api/kronos : NIFTY 50 Kronos forecast tab (display only).

The desk owns the candles and the summary; the neural network lives in the sidecar
(ict_smc_app/kronos, 127.0.0.1:8701). If the sidecar is not running, every route here returns
`ok: false` with a message the tab can render — the rest of the desk is untouched.

Routes:
  GET  /api/kronos/status              sidecar + model state (for the tab's setup strip)
  POST /api/kronos/download-model      ask the sidecar to fetch Kronos-base (SHA-256 pinned)
  GET  /api/kronos/forecast            NIFTY 50 forecast, with step_back / reveal

Alert-only: nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Query

from .. import config
from ..engine import kronos_forecast as kf
from ..timeutil import IST, now_ist

router = APIRouter()
log = logging.getLogger("kronos_api")

NIFTY_SEC_ID = "13"          # NIFTY 50 index, IDX_I
_candle_cache: Dict[str, Any] = {"t": 0.0, "rows": []}
_forecast_cache: Dict[str, Any] = {}


def _cfg() -> Dict[str, Any]:
    return kf.get_cfg(config.rules().get("kronos"))


def _sidecar(path: str) -> str:
    return _cfg()["sidecar_url"].rstrip("/") + path


# ── candles ───────────────────────────────────────────────────────────────────────────────────
async def _hourly_candles(days: int = 300, force: bool = False) -> List[Dict[str, Any]]:
    """
    NIFTY 50 one-hour index candles from Dhan, cached for 5 minutes.

    Fetched in 90-day chunks because Dhan's /charts/intraday refuses a wider window. 360 candles
    of context is ~60 trading days on its own; Step back needs that much again behind it, so the
    default reaches back ~300 calendar days and the chunks are merged and de-duplicated by ts.

    NOTE: Dhan returns volume 0 for the index. That is correct, not a bug — an index has no
    traded quantity. The volume panel stays switched off unless a real volume series is attached.
    """
    c = _cfg()
    if not force and _candle_cache["rows"] and time.time() - _candle_cache["t"] < 300:
        return _candle_cache["rows"]

    from .main import _h
    h = _h()
    to = now_ist()
    start = to - timedelta(days=int(days))

    merged: Dict[int, Dict[str, Any]] = {}
    win_start = start
    while win_start < to:
        win_end = min(win_start + timedelta(days=89), to)
        try:
            raw = await h.rest.intraday(NIFTY_SEC_ID, win_start, win_end, interval="60",
                                        segment="IDX_I", instrument="INDEX")
            for r in raw:
                merged[int(r["ts"])] = r
        except Exception as e:
            # one bad window must not lose the windows that did arrive
            log.warning("kronos: 1h chunk %s..%s failed: %s",
                        win_start.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d"), e)
        win_start = win_end + timedelta(days=1)

    rows = kf.filter_hourly(list(merged.values()), include_stub=bool(c["include_stub_1515"]))
    _candle_cache.update(t=time.time(), rows=rows)
    log.info("kronos: %d NIFTY 1h session candles (%s .. %s)", len(rows),
             datetime.fromtimestamp(rows[0]["ts"], IST).strftime("%Y-%m-%d") if rows else "-",
             datetime.fromtimestamp(rows[-1]["ts"], IST).strftime("%Y-%m-%d %H:%M") if rows else "-")
    return rows


# ── routes ────────────────────────────────────────────────────────────────────────────────────

def _disabled(c: Dict[str, Any]) -> Dict[str, Any]:
    """The payload every Kronos route returns while rules.json has kronos.enabled false.

    `enabled` existed in rules.json from the start but nothing ever read it, so the documented off
    switch did nothing -- the routes still reached for the sidecar. It is honoured here, and no
    route touches the network or the Dhan budget while it is false.
    """
    return {
        "ok": False, "enabled": False, "sidecar": "disabled",
        "message": ("Kronos is switched off in rules.json (kronos.enabled false). No forecast is "
                    "fetched and the sidecar is never contacted. Set it true to bring the tab back."),
        "settings": {"hist": c["hist_candles"], "pred": c["pred_candles"], "paths": c["paths"],
                     "T": c["temperature"], "top_p": c["top_p"]},
    }


@router.get("/api/kronos/status")
async def kronos_status() -> Dict[str, Any]:
    c = _cfg()
    if not c.get("enabled", True):
        return _disabled(c)
    try:
        async with httpx.AsyncClient(timeout=5.0) as cl:
            r = await cl.get(_sidecar("/health"))
            j = r.json()
        return {"ok": True, "sidecar": "up", **j, "settings": {
            "hist": c["hist_candles"], "pred": c["pred_candles"], "paths": c["paths"],
            "T": c["temperature"], "top_p": c["top_p"],
        }}
    except Exception as e:
        return {
            "ok": False,
            "sidecar": "down",
            "message": (f"Kronos sidecar is not answering on {c['sidecar_url']}. "
                        f"Start it with ict_smc_app/kronos/run_kronos.sh — the rest of the desk is unaffected."),
            "error": f"{type(e).__name__}: {e}",
        }


@router.post("/api/kronos/download-model")
async def kronos_download() -> Dict[str, Any]:
    c = _cfg()
    if not c.get("enabled", True):
        return _disabled(c)
    try:
        async with httpx.AsyncClient(timeout=30.0) as cl:
            r = await cl.post(_sidecar("/model/download"))
            return r.json()
    except Exception as e:
        log.warning("kronos model download request failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/api/kronos/forecast")
async def kronos_forecast(
    step_back: int = Query(0, ge=0, le=400),
    paths: Optional[int] = Query(None, ge=2, le=100),
    pred: Optional[int] = Query(None, ge=1, le=120),
    seed: Optional[int] = Query(None),
    refresh: bool = Query(False),
) -> Dict[str, Any]:
    """
    NIFTY 50 forecast. step_back > 0 rewinds 'now' so Reveal can mark the forecast against
    what actually happened.
    """
    c = _cfg()
    if not c.get("enabled", True):
        return _disabled(c)
    n_paths = int(paths or c["paths"])
    n_pred = int(pred or c["pred_candles"])
    n_hist = int(c["hist_candles"])

    key = f"{step_back}:{n_paths}:{n_pred}:{seed}"
    hit = _forecast_cache.get(key)
    if hit and not refresh and time.time() - hit["t"] < 600:
        return hit["j"]

    try:
        candles = await _hourly_candles(force=refresh)
    except Exception as e:
        log.warning("kronos candle fetch failed: %s", e)
        return {"ok": False, "message": f"could not fetch NIFTY 1h candles: {type(e).__name__}: {e}"}

    split = kf.split_for_stepback(candles, n_hist, n_pred, step_back)
    if not split.get("ok"):
        return {"ok": False, "message": split.get("message", "not enough history")}

    context = split["context"]
    future_ts = kf.next_session_ts(split["origin_ts"], n_pred, include_stub=bool(c["include_stub_1515"]))

    body = {
        "candles": [{"ts": int(r["ts"]), "o": float(r["o"]), "h": float(r["h"]),
                     "l": float(r["l"]), "c": float(r["c"]), "v": float(r.get("v") or 0.0)}
                    for r in context],
        "pred": n_pred,
        "paths": n_paths,
        "temperature": float(c["temperature"]),
        "top_p": float(c["top_p"]),
        "seed": seed,
        "future_ts": future_ts,
    }

    try:
        async with httpx.AsyncClient(timeout=float(c["sidecar_timeout_s"])) as cl:
            r = await cl.post(_sidecar("/forecast"), json=body)
            fc = r.json()
    except Exception as e:
        log.warning("kronos sidecar call failed: %s", e)
        return {
            "ok": False,
            "sidecar": "down",
            "message": (f"Kronos sidecar is not answering on {c['sidecar_url']}. "
                        f"Start it with ict_smc_app/kronos/run_kronos.sh."),
            "error": f"{type(e).__name__}: {e}",
        }

    if not fc.get("ok"):
        return {"ok": False, "message": fc.get("error", "forecast failed"), "model": fc.get("model")}

    has_vol = any(float(r.get("v") or 0.0) > 0 for r in context)
    last_close = float(context[-1]["c"])
    summary = kf.summarize_paths(
        fc["paths"], last_close, [float(r["c"]) for r in context],
        has_real_volume=has_vol, cfg=c,
    )
    verdict = kf.verdict(summary, cfg=c)
    reveal = kf.score_reveal(summary, split["actual"]) if split["actual"] else {"ok": False}

    j = {
        "ok": True,
        "symbol": "NIFTY 50",
        "tf": "1h",
        "origin_ts": split["origin_ts"],
        "origin_label": datetime.fromtimestamp(split["origin_ts"], IST).strftime("%d %b %H:%M"),
        "step_back": step_back,
        "is_live": split["is_live"],
        "context": [{"ts": int(r["ts"]), "o": float(r["o"]), "h": float(r["h"]),
                     "l": float(r["l"]), "c": float(r["c"]), "v": float(r.get("v") or 0.0)}
                    for r in context[-int(c.get("show_candles", 140)):]],
        "future_ts": future_ts,
        "paths": fc["paths"],
        "summary": summary,
        "verdict": verdict,
        "actual": [{"ts": int(r["ts"]), "o": float(r["o"]), "h": float(r["h"]),
                    "l": float(r["l"]), "c": float(r["c"])} for r in split["actual"]],
        "reveal": reveal,
        "settings": fc.get("settings", {}),
        "max_step_back": max(0, len(candles) - n_hist),
        "disclaimer": "Educational only — not investment advice. This desk never places an order.",
    }
    _forecast_cache[key] = {"t": time.time(), "j": j}
    if len(_forecast_cache) > 40:
        oldest = min(_forecast_cache, key=lambda k: _forecast_cache[k]["t"])
        _forecast_cache.pop(oldest, None)
    return j
