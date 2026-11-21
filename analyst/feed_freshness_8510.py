"""8600→8510 feed freshness contract (LIVE / STALE / WAITING_FOR_SYNC)."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("feed_freshness_8510")

FEED_PE_CE_TABLE = "pe_ce_table"
FEED_FRESH_OI = "fresh_change_oi"
FEED_TOTAL_OI = "cumulative_total_oi"
FEED_WRITER_FLOW = "classic_writer_flow"

REQUIRED_FEED_IDS = (FEED_PE_CE_TABLE, FEED_FRESH_OI, FEED_TOTAL_OI, FEED_WRITER_FLOW)

DEFAULT_CFG: Dict[str, Any] = {
    "fast_feed_seconds": 10,
    "stale_after_seconds": 30,
    "enabled": True,
}

_BRIDGE_GROUP = {
    FEED_FRESH_OI: "Fresh Change in OI",
    FEED_TOTAL_OI: "Cumulative Total OI",
    FEED_WRITER_FLOW: "Classic Writer Flow",
}

_last_stale_log: Dict[str, float] = {}


def load_feed_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    root = dict(DEFAULT_CFG)
    block = dict((cfg or {}).get("feed_freshness") or {})
    root.update({k: v for k, v in block.items() if k in DEFAULT_CFG or k.endswith("_seconds")})
    ics = (cfg or {}).get("ict_smc_desk_signals") or {}
    if "fast_feed_seconds" in ics:
        root["fast_feed_seconds"] = float(ics["fast_feed_seconds"])
    return root


def parse_source_ts(raw: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    if raw is None or raw == "WAITING FOR LIVE SYNC":
        return None
    now = now or datetime.now(IST)
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), IST)
        s = str(raw).strip()
        if not s:
            return None
        if s.endswith(" IST"):
            s = s.replace(" IST", "")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except (ValueError, TypeError, OverflowError):
        return None


def age_seconds(source_ts: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if source_ts is None:
        return None
    now = now or datetime.now(IST)
    return max(0.0, (now - source_ts).total_seconds())


def classify_state(
    *,
    source_ts: Optional[datetime],
    waiting: bool = False,
    stale_after: float = 30.0,
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[float]]:
    """Returns (state, age_sec). STALE never implies a trade direction."""
    if waiting or source_ts is None:
        return "WAITING_FOR_SYNC", None
    age = age_seconds(source_ts, now)
    if age is None:
        return "WAITING_FOR_SYNC", None
    if age > float(stale_after):
        return "STALE", age
    return "LIVE", age


def strip_direction_for_contract(state: str, direction: Any) -> Any:
    if state in ("STALE", "WAITING_FOR_SYNC"):
        return None
    d = str(direction or "").upper()
    return d if d in ("CE", "PE", "LONG", "SHORT") else None


def log_stale_throttled(feed_id: str, message: str, *, interval: float = 60.0) -> None:
    """Avoid log/DB flooding when feed stays stale."""
    now = time.monotonic()
    if now - _last_stale_log.get(feed_id, 0.0) < interval:
        return
    _last_stale_log[feed_id] = now
    log.info(message)


def _bridge_row(rows: List[Dict[str, Any]], group: str) -> Optional[Dict[str, Any]]:
    for r in rows or []:
        if str(r.get("signal_group") or "") == group:
            return r
    return None


def _feed_from_bridge(
    bridge_last: Optional[Dict[str, Any]],
    feed_id: str,
    fcfg: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    rows = (bridge_last or {}).get("rows") or []
    group = _BRIDGE_GROUP[feed_id]
    row = _bridge_row(rows, group)
    stale_after = float(fcfg.get("stale_after_seconds", 30))
    if row is None:
        return {
            "feed_id": feed_id,
            "value_summary": "8600 bridge has no row for this feed",
            "source_ts": None,
            "age_sec": None,
            "state": "WAITING_FOR_SYNC",
            "direction": None,
        }
    verdict = str(row.get("verdict") or "")
    if verdict == "UNAVAILABLE":
        reason = str(row.get("reason") or "not available")[:200]
        return {
            "feed_id": feed_id,
            "value_summary": reason,
            "source_ts": row.get("source_ts"),
            "age_sec": None,
            "state": "WAITING_FOR_SYNC",
            "direction": None,
        }
    src = parse_source_ts(row.get("source_ts"), now)
    state, age = classify_state(source_ts=src, stale_after=stale_after, now=now)
    direction = strip_direction_for_contract(state, row.get("direction"))
    summary = verdict if direction else (f"{verdict} (unavailable — {state})" if verdict else state)
    if state == "STALE":
        log_stale_throttled(feed_id, f"Feed {feed_id} STALE age={age:.0f}s — direction withheld")
    return {
        "feed_id": feed_id,
        "value_summary": summary[:240],
        "source_ts": row.get("source_ts"),
        "age_sec": round(age, 1) if age is not None else None,
        "state": state,
        "direction": direction,
    }


def _feed_pe_ce_table(backend: Any, fcfg: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    stale_after = float(fcfg.get("stale_after_seconds", 30))
    try:
        payload = backend.dhan_oi.get_pe_ce_table(60)
    except Exception as exc:
        return {
            "feed_id": FEED_PE_CE_TABLE,
            "value_summary": f"PE-CE table unavailable ({type(exc).__name__})",
            "source_ts": None,
            "age_sec": None,
            "state": "WAITING_FOR_SYNC",
            "direction": None,
        }
    if not payload.get("available") and payload.get("status") in ("FAILED", "STALE"):
        return {
            "feed_id": FEED_PE_CE_TABLE,
            "value_summary": str(payload.get("reason") or payload.get("status") or "no table"),
            "source_ts": payload.get("source_ts"),
            "age_sec": payload.get("age_sec"),
            "state": "WAITING_FOR_SYNC" if not payload.get("rows") else "STALE",
            "direction": None,
        }
    raw_ts = payload.get("source_ts") or payload.get("last_sync_time") or payload.get("timestamp")
    src = parse_source_ts(raw_ts, now)
    state, age = classify_state(source_ts=src, stale_after=stale_after, now=now)
    rows = payload.get("rows") or []
    summary = f"{len(rows)} rows · source {payload.get('source', 'DHAN_API')}"
    if state == "STALE":
        log_stale_throttled(FEED_PE_CE_TABLE, f"PE-CE table STALE age={age:.0f}s")
    return {
        "feed_id": FEED_PE_CE_TABLE,
        "value_summary": summary,
        "source_ts": payload.get("source_ts") or (src.isoformat() if src else None),
        "age_sec": round(age, 1) if age is not None else payload.get("age_sec"),
        "state": state,
        "direction": None,
        "positional_columns": payload.get("positional_columns"),
        "intraday_columns": payload.get("intraday_columns"),
    }


def build_feeds_health(backend: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(IST)
    fcfg = load_feed_cfg(getattr(backend, "cfg", {}) or {})
    bridge = getattr(backend, "desk8600_bridge", None)
    bridge_last = getattr(bridge, "last", None) if bridge is not None else None
    feeds = [
        _feed_pe_ce_table(backend, fcfg, now),
        _feed_from_bridge(bridge_last, FEED_FRESH_OI, fcfg, now),
        _feed_from_bridge(bridge_last, FEED_TOTAL_OI, fcfg, now),
        _feed_from_bridge(bridge_last, FEED_WRITER_FLOW, fcfg, now),
    ]
    by_id = {f["feed_id"]: f for f in feeds}
    return {
        "ok": True,
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "fast_feed_seconds": float(fcfg.get("fast_feed_seconds", 10)),
        "stale_after_seconds": float(fcfg.get("stale_after_seconds", 30)),
        "feeds": feeds,
        "by_id": by_id,
    }


def apply_source_freshness_meta(
    payload: Dict[str, Any],
    *,
    source_ts: Any,
    stale_after: float = 30.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attach source_ts, age_sec, feed_state to API payloads."""
    now = now or datetime.now(IST)
    src = parse_source_ts(source_ts, now)
    state, age = classify_state(source_ts=src, stale_after=stale_after, now=now)
    out = dict(payload)
    out["source_ts"] = source_ts if source_ts is not None else (src.isoformat() if src else None)
    out["age_sec"] = round(age, 1) if age is not None else None
    out["feed_state"] = state
    if state != "LIVE":
        out["direction"] = None
        if "bias" in out:
            out["bias"] = "UNKNOWN"
            out["available"] = False
    return out