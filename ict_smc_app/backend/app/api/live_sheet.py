"""Display-only Dhan cache adapter. No Dhan HTTP client or order dependency."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .. import config

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
router = APIRouter()
_hub = None
_gate_cache = None
_gate_at = 0.0
_gate_lock = None
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def bind(get_hub):
    global _hub
    _hub = get_hub


def settings() -> Dict[str, Any]:
    return json.loads((config.APP_DIR / "live_sheet_rules.json").read_text())


def _sheet_db() -> Path:
    return config.DATA_DIR / "dhan_live_sheet.sqlite3"


def model_rho(spot, strike, iv, expiry, side, now):
    try:
        r = settings()
        end = datetime.fromisoformat(f"{expiry}T{r['expiry_time']}:00").replace(tzinfo=IST).timestamp()
        t = (end - now) / (r["day_count"] * 86400)
        sigma = float(iv) / 100
        if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
            return None
        d2 = (math.log(spot / strike) + (r["risk_free_rate"] - sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
        cdf = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
        value = strike * t * math.exp(-r["risk_free_rate"] * t) / 100
        return value * cdf(d2) if side == "ce" else -value * cdf(-d2)
    except (TypeError, ValueError, OverflowError):
        return None


def chain_view(ch, now=None, market_open=False):
    now = time.time() if now is None else now
    stamp = ch.get("fetched_at")
    age = now - stamp if isinstance(stamp, (int, float)) else None
    fresh = market_open and age is not None and 0 <= age <= settings()["stale_after_seconds"] and not ch.get("is_stale")
    out = []
    for row in ch.get("rows", []):
        for side in ("ce", "pe"):
            x = row.get(side) or {}
            oi, change, ltp, net = (x.get(k) for k in ("oi", "oi_chg", "ltp", "chg"))
            buildup = "neutral"
            if net is not None and change is not None and net != 0 and change != 0:
                buildup = ("long buildup" if net > 0 else "short buildup") if change > 0 else (
                    "short covering" if net > 0 else "long unwinding"
                )
            strike = row["strike"]
            spot = ch.get("spot")
            intrinsic = max(0, (spot - strike) if side == "ce" else (strike - spot)) if spot is not None else None
            out.append(
                dict(
                    strike=strike,
                    side=side.upper(),
                    security_id=x.get("sid"),
                    symbol_name=f"NIFTY {ch.get('expiry')} {strike:g} {side.upper()}",
                    expiry_date=ch.get("expiry"),
                    oi=oi,
                    change_oi=change,
                    previous_oi=oi - change if oi is not None and change is not None else None,
                    volume=x.get("vol"),
                    iv=x.get("iv"),
                    ltp=ltp,
                    net_change=net,
                    change_oi_value=change * ltp if change is not None and ltp is not None else None,
                    delta=x.get("delta"),
                    gamma=x.get("gamma"),
                    vega=x.get("vega"),
                    theta=x.get("theta"),
                    rho=model_rho(spot, strike, x.get("iv"), ch.get("expiry"), side, now),
                    buildup=buildup,
                    time_value=ltp - intrinsic if ltp is not None and intrinsic is not None else None,
                    bid=x.get("bid"),
                    ask=x.get("ask"),
                )
            )
    return dict(
        instrument="NIFTY",
        expiry=ch.get("expiry"),
        rows=out,
        spot=ch.get("spot"),
        source_ts=None,
        received_at=stamp,
        age_sec=age,
        timestamp_kind="HTTP_RECEIPT_NOT_EXCHANGE_TICK",
        state="RECEIPT_RECENT" if fresh else "STALE",
        totals=ch.get("totals") or {},
        limits=[
            "Cached normalized fields may default missing provider values to zero; not executable quote evidence.",
            "rho is local European Black–Scholes per 1% rate move: no dividends, ACT/365, configured rate.",
            "change_oi = OI minus previous-day OI; change_oi_value = change_oi × current LTP, not money flow.",
            "Buildup is a price/OI heuristic; OI does not identify writer intent.",
        ],
    )


def _ensure_db(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS minute_observations "
        "(session TEXT, minute TEXT, expiry TEXT, observed_at REAL, spot REAL, totals TEXT, row_json TEXT, "
        "PRIMARY KEY(session,minute,expiry))"
    )


def _advance_decline(hub) -> Dict[str, str]:
    """Context-only breadth; unavailable without verified aligned constituent closes."""
    try:
        kv = hub and getattr(hub, "json", {}).get("5m") or {}
        breadth = (kv.get("breadth") or {}) if isinstance(kv, dict) else {}
        if breadth.get("advances") is not None and breadth.get("declines") is not None:
            return {"nifty": f"{breadth['advances']}/{breadth['declines']}"}
    except Exception:
        pass
    return {"nifty": "unavailable", "banknifty": "unavailable", "mid": "unavailable", "all_nse": "unavailable"}


def _futures_snapshot(hub) -> Dict[str, Any]:
    out: Dict[str, Any] = {"oi": None, "ltp": None}
    try:
        fut = getattr(hub, "fut", None) or {}
        sym = str(fut.get("security_id") or "")
        ltp = hub.candles.last_price.get(sym) if hub else None
        today = int(datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        from .. import db as db_mod

        rows = db_mod.query("SELECT oi FROM fut_oi WHERE ts >= ? ORDER BY ts DESC LIMIT 1", (today,))
        if rows:
            out["oi"] = rows[0].get("oi")
        out["ltp"] = ltp
    except Exception:
        pass
    return out


def record_observation(ch, path=None, now=None, market_open=False, hub=None):
    if not market_open or not ch.get("rows"):
        return
    stamp = ch.get("fetched_at")
    now = time.time() if now is None else now
    if stamp is None or not 0 <= now - stamp <= settings()["stale_after_seconds"]:
        return
    dt = datetime.fromtimestamp(stamp, IST)
    if not settings()["session_open"] <= dt.strftime("%H:%M") < settings()["session_close"]:
        return
    path = path or _sheet_db()
    minute = dt.strftime("%H:%M")
    session = dt.date().isoformat()
    prev = _prev_chain_row(path, session, ch.get("expiry"), minute)
    from analyst.dhan_live_sheet_minutes_8510 import _chain_maps, build_minute_row

    row = build_minute_row(
        ch,
        prev_ch=prev,
        minute=minute,
        market_open=market_open,
        stale_after=float(settings()["stale_after_seconds"]),
        advance_decline=_advance_decline(hub),
        futures=_futures_snapshot(hub),
        scanner_cfg=None,
    )
    row["_chain_maps"] = _chain_maps(ch)
    totals = dict(ch.get("totals") or {})
    with sqlite3.connect(path, timeout=0.1) as db:
        _ensure_db(db)
        db.execute(
            "INSERT OR REPLACE INTO minute_observations VALUES (?,?,?,?,?,?,?)",
            (session, minute, ch.get("expiry"), stamp, ch.get("spot"), json.dumps(totals), json.dumps(row)),
        )


def _prev_chain_row(path: Path, session: str, expiry: str, minute: str) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1) as db:
        rows = list(
            db.execute(
                "SELECT row_json FROM minute_observations WHERE session=? AND expiry=? AND minute<? ORDER BY minute DESC LIMIT 1",
                (session, expiry, minute),
            )
        )
    if not rows:
        return None
    try:
        from analyst.dhan_live_sheet_minutes_8510 import _maps_to_fake_chain

        prev = json.loads(rows[0][0] or "{}")
        maps = prev.get("_chain_maps") or {}
        if not maps:
            return None
        return _maps_to_fake_chain(maps, float(prev.get("observed_at") or 0), expiry, float(prev.get("spot") or 0))
    except Exception:
        return None


def minute_table(expiry: str, path=None) -> List[Dict[str, Any]]:
    path = path or _sheet_db()
    if not path.exists():
        return []
    day = datetime.now(IST).date().isoformat()
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1) as db:
        _ensure_db(db)
        for minute, row_json, observed_at, spot in db.execute(
            "SELECT minute, row_json, observed_at, spot FROM minute_observations WHERE session=? AND expiry=? ORDER BY minute",
            (day, expiry),
        ):
            try:
                row = json.loads(row_json or "{}")
            except Exception:
                row = {}
            row.setdefault("time", minute)
            row.setdefault("spot", spot)
            age = max(0.0, time.time() - float(observed_at)) if observed_at else None
            row["age_sec"] = round(age) if age is not None else None
            if age is not None and age > settings()["stale_after_seconds"]:
                row["feed_state"] = "STALE"
            row.pop("_chain_maps", None)
            out.append(row)
    return out


def history(expiry, path=None):
    return minute_table(expiry, path)


async def gates():
    global _gate_cache, _gate_at, _gate_lock
    if _gate_lock is None:
        _gate_lock = asyncio.Lock()
    async with _gate_lock:
        age = time.monotonic() - _gate_at
        if _gate_cache is not None and age < settings()["gate_cache_seconds"]:
            return dict(state="AVAILABLE", receipt_age_sec=age, **_gate_cache)
        try:
            async with httpx.AsyncClient(timeout=settings()["gate_timeout_seconds"], trust_env=False) as client:
                response = await client.get("http://127.0.0.1:8510/api/dhan-live-sheet/gates")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload.get("lanes"), dict):
                    raise ValueError("gate schema unavailable")
            _gate_cache, _gate_at = payload, time.monotonic()
            return dict(state="AVAILABLE", receipt_age_sec=0, **payload)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Live-sheet gate mirror unavailable: %s", type(exc).__name__)
            return dict(state="UNAVAILABLE", lanes={}, sources=[], reason="8510 gate mirror unavailable; no permission inferred")


@router.get("/api/dhan-live-sheet")
async def sheet(expiry: str = ""):
    from ..timeutil import market_open_now

    h = _hub()
    exp = expiry or h.sel_expiry()
    ch = h.chain.chains.get(exp) or {}
    view = chain_view(ch, market_open=market_open_now())
    view["expiries"] = h.chain.expiries
    view["gate_snapshot"] = await gates()
    view["minute_table"] = await asyncio.to_thread(minute_table, exp)
    view["advance_decline"] = dict(
        state="PARTIAL" if _advance_decline(h).get("nifty") != "unavailable" else "UNAVAILABLE",
        fields=_advance_decline(h),
        reason="Breadth is context-only and never a gate; BankNifty/Midselect unavailable without verified feeds",
        context_only=True,
    )
    return view


@router.get("/api/dhan-live-sheet/minutes.csv")
async def sheet_csv(expiry: str = ""):
    from analyst.dhan_live_sheet_minutes_8510 import minute_row_csv_header, minute_row_to_csv_cells

    h = _hub()
    exp = expiry or h.sel_expiry()
    rows = await asyncio.to_thread(minute_table, exp)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(minute_row_csv_header())
    for row in rows:
        w.writerow(minute_row_to_csv_cells(row))
    return PlainTextResponse(buf.getvalue(), media_type="text/csv; charset=utf-8")