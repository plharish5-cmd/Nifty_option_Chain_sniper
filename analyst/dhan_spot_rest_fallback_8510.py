"""On-demand Dhan REST LTP for stale WebSocket spot / option quotes at entry only."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from analyst.ema8_display_audit_8510 import spot_quote_stale

log = logging.getLogger("dhan_spot_rest_fallback_8510")

IST = timezone(timedelta(hours=5, minutes=30))

NIFTY_IDX_SEC_ID = 13
MIN_REST_INTERVAL_SEC = 1.5
REST_CACHE_TTL_SEC = 2.0
REST_TIMEOUT_SEC = 2.5
SPOT_MAX_AGE_SEC = 5.0
# Keep-alive refreshes BEFORE the entry-block threshold so spot is already fresh
# when a touch arrives, instead of a single cold at-entry REST attempt.
SPOT_KEEPALIVE_AGE_SEC = 3.0

def _reset_throttle_for_tests() -> None:
    from analyst.marketfeed_scheduler_8510 import _reset_for_tests

    _reset_for_tests()


def _parse_ltp_payload(data: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for _seg, sec_map in data.items():
        if not isinstance(sec_map, dict):
            continue
        for sid, quote in sec_map.items():
            if isinstance(quote, dict) and quote.get("last_price") not in (None, ""):
                try:
                    px = float(quote["last_price"])
                    if px > 0:
                        out[str(sid)] = px
                except (TypeError, ValueError):
                    continue
    return out


def _error_from_body(body: Any, status_code: int) -> str:
    if status_code == 429:
        return "RL001"
    if not isinstance(body, dict):
        return f"HTTP {status_code}"
    for key in ("errorCode", "error_code", "internalErrorCode", "internal_error_code"):
        code = str(body.get(key) or "").strip().upper()
        if code:
            return code
    for key in ("remarks", "message", "error", "errorMessage"):
        msg = str(body.get(key) or "")
        if "RL001" in msg.upper():
            return "RL001"
    return f"HTTP {status_code}" if status_code != 200 else "invalid JSON"


async def entry_rest_ltp_lookup(
    broker: Any,
    segment_ids: Dict[str, List[int]],
) -> Tuple[Dict[str, float], Optional[str]]:
    """Rate-limited POST /v2/marketfeed/ltp for entry recovery. Returns ({sec_id: price}, error).

    Serialised through the shared per-account marketfeed arbiter so the spot
    keep-alive, entry recovery, paper quotes and broker LTP calls all share one
    1-req/s budget. Cache freshness is PER-SYMBOL, so a spot refresh never
    renews an option quote's age.
    """
    from analyst.marketfeed_scheduler_8510 import get_gate

    req_sids = [str(s) for sids in segment_ids.values() if isinstance(sids, list) for s in sids]

    if not callable(getattr(broker, "is_configured", None)) or not broker.is_configured():
        return {}, "broker not configured"

    base = str(getattr(broker, "base_url", "") or "").rstrip("/")
    headers_fn = getattr(broker, "_headers", None)
    if not base or not callable(headers_fn):
        return {}, "broker not configured"

    gate = get_gate(broker, min_interval=MIN_REST_INTERVAL_SEC)

    # Fresh per-symbol cache for exactly the requested ids → no HTTP.
    cached = gate.cached_prices(req_sids, REST_CACHE_TTL_SEC) if req_sids else None
    if cached is not None:
        return dict(cached), None

    # Shared 429 backoff — serve cache if any, else report throttle without hitting the API.
    if gate.in_backoff():
        return {}, "rate limited (backoff)"

    try:
        from analyst.dhan_ipv4_guard_8510 import ipv4_async_client

        # Reservation is held across the await: a second coroutine blocks here
        # and then spaces off THIS send, instead of both firing in one second.
        async with gate.slot():
            async with ipv4_async_client(REST_TIMEOUT_SEC) as client:
                res = await client.post(
                    f"{base}/marketfeed/ltp",
                    json=segment_ids,
                    headers=headers_fn(),
                )
            body = {}
            try:
                body = res.json()
            except Exception:
                body = {}
            if res.status_code == 429:
                gate.note_rate_limited()
                return {}, _error_from_body(body, res.status_code)
            if res.status_code != 200:
                return {}, _error_from_body(body, res.status_code)
            data = body.get("data") if isinstance(body, dict) else None
            prices = _parse_ltp_payload(data)
            if not prices:
                return {}, _error_from_body(body, res.status_code)
            gate.note_success()
            gate.store_prices(prices)
            return dict(prices), None
    except Exception as exc:
        log.warning("Entry REST LTP fallback failed: %s", exc)
        return {}, "timeout" if "timeout" in str(exc).lower() else str(exc)


def spot_blob_needs_rest_fallback(spot_blob: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[float]]:
    blob = spot_blob if isinstance(spot_blob, dict) else {}
    if blob.get("is_stale") is True:
        stale, age = spot_quote_stale(blob, max_age_sec=SPOT_MAX_AGE_SEC)
        return True, age
    stale, age = spot_quote_stale(blob, max_age_sec=SPOT_MAX_AGE_SEC)
    return stale, age


async def recover_nifty_spot_for_entry(
    backend: Any,
    *,
    market_open: bool,
) -> Tuple[bool, str]:
    """Try REST NIFTY spot when WebSocket/cache is stale (>5s). Fail closed on miss."""
    if not market_open:
        return True, ""
    blob = (getattr(backend, "cached_telemetry", None) or {}).get("spot")
    needs, age = spot_blob_needs_rest_fallback(blob)
    if not needs:
        return True, ""
    prices, err = await entry_rest_ltp_lookup(
        backend.broker,
        {"IDX_I": [NIFTY_IDX_SEC_ID]},
    )
    spot = float(prices.get(str(NIFTY_IDX_SEC_ID)) or prices.get(NIFTY_IDX_SEC_ID) or 0.0)
    if err or spot <= 0:
        age_s = f"{age:.1f}s" if age is not None else "unknown"
        return False, f"Spot feed STALE ({age_s}) — REST fallback failed"
    now = time.time()
    telem = getattr(backend, "cached_telemetry", None)
    if not isinstance(telem, dict):
        telem = {}
        backend.cached_telemetry = telem
    prev = telem.get("spot") if isinstance(telem.get("spot"), dict) else {}
    telem["spot"] = {
        **prev,
        "price": spot,
        "is_stale": False,
        "status": "LIVE",
        "updated_epoch": now,
        "time_ist": datetime.now(IST).strftime("%H:%M:%S IST"),
        "rest_fallback_recovered": True,
    }
    broker = getattr(backend, "broker", None)
    if broker is not None:
        broker._ltp_cache[str(NIFTY_IDX_SEC_ID)] = spot
        broker._cache_timestamps[str(NIFTY_IDX_SEC_ID)] = now
    log.info("Spot recovered via Dhan REST fallback (%.2f)", spot)
    return True, "Spot recovered via Dhan REST fallback"


def _apply_spot_to_backend(backend: Any, spot: float) -> None:
    """Write a recovered spot into cached_telemetry + broker LTP cache as a LIVE quote."""
    now = time.time()
    telem = getattr(backend, "cached_telemetry", None)
    if not isinstance(telem, dict):
        telem = {}
        backend.cached_telemetry = telem
    prev = telem.get("spot") if isinstance(telem.get("spot"), dict) else {}
    telem["spot"] = {
        **prev,
        "price": spot,
        "is_stale": False,
        "status": "LIVE",
        "updated_epoch": now,
        "time_ist": datetime.now(IST).strftime("%H:%M:%S IST"),
        "rest_fallback_recovered": True,
    }
    broker = getattr(backend, "broker", None)
    if broker is not None:
        try:
            broker._ltp_cache[str(NIFTY_IDX_SEC_ID)] = spot
            broker._cache_timestamps[str(NIFTY_IDX_SEC_ID)] = now
        except Exception:
            pass


async def keepalive_nifty_spot(backend: Any, *, market_open: bool) -> bool:
    """Proactive background spot refresh: when the WebSocket spot is older than
    SPOT_KEEPALIVE_AGE_SEC (3s), pull a fresh REST LTP so spot never reaches the 5s
    entry-block threshold. Called continuously from a background worker, NOT at entry.
    Returns True when a refresh was applied. Silent/throttled misses are ignored —
    the entry-time recover_nifty_spot_for_entry remains the hard fail-closed guard."""
    if not market_open:
        return False
    blob = (getattr(backend, "cached_telemetry", None) or {}).get("spot")
    stale, _age = spot_quote_stale(blob if isinstance(blob, dict) else {}, max_age_sec=SPOT_KEEPALIVE_AGE_SEC)
    if not stale and not (isinstance(blob, dict) and blob.get("is_stale") is True):
        return False
    prices, err = await entry_rest_ltp_lookup(backend.broker, {"IDX_I": [NIFTY_IDX_SEC_ID]})
    if err:
        return False
    spot = float(prices.get(str(NIFTY_IDX_SEC_ID)) or prices.get(NIFTY_IDX_SEC_ID) or 0.0)
    if spot <= 0:
        return False
    _apply_spot_to_backend(backend, spot)
    return True


async def recover_option_ltp_for_entry(broker: Any, security_id: str) -> Tuple[Optional[float], Optional[str]]:
    """Single rate-limited REST lookup for an option contract LTP at entry."""
    try:
        sec_int = int(str(security_id).strip())
    except (TypeError, ValueError):
        return None, "invalid security_id"
    if sec_int <= 0:
        return None, "invalid security_id"
    prices, err = await entry_rest_ltp_lookup(broker, {"NSE_FNO": [sec_int]})
    px = float(prices.get(str(sec_int)) or prices.get(sec_int) or 0.0)
    if err or px <= 0:
        return None, err or "no price"
    return px, None