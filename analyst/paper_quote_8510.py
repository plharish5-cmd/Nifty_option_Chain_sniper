"""Fresh REST depth only: never use signal prices or position LTP as fills."""
import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

log = logging.getLogger("paper_quote_8510")


def _rules() -> Dict[str, Any]:
    return json.loads(Path(__file__).with_name("paper_quote_rules_8510.json").read_text())


def _lookup_quote(depth: Any, security_id: Union[str, int]) -> Dict[str, Any]:
    if not isinstance(depth, dict) or not depth:
        return {}
    sec_int = int(security_id)
    keys = (str(security_id), str(sec_int), sec_int, security_id)
    for key in keys:
        hit = depth.get(key)
        if isinstance(hit, dict) and hit:
            return hit
    # Raw Dhan payload sometimes arrives nested under segment (Paper Desk parses NSE_FNO).
    for seg in ("NSE_FNO", "NSE_EQ", "IDX_I"):
        seg_map = depth.get(seg)
        if not isinstance(seg_map, dict):
            continue
        for key in keys:
            hit = seg_map.get(key) if key in seg_map else seg_map.get(str(key))
            if isinstance(hit, dict) and hit:
                return hit
    return {}


def _bid_ask_from_quote(q: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Parse top-of-book the same way as paper_desk._quotes_fetch (8502/8504)."""
    dp = q.get("depth") or {}
    buys = dp.get("buy") or []
    sells = dp.get("sell") or []
    bid = ask = None
    if isinstance(buys, list) and buys and isinstance(buys[0], dict):
        bid = buys[0].get("price")
    if isinstance(sells, list) and sells and isinstance(sells[0], dict):
        ask = sells[0].get("price")
    bid = bid if bid is not None else q.get("best_bid_price") or q.get("buy_price") or q.get("bid")
    ask = ask if ask is not None else q.get("best_ask_price") or q.get("sell_price") or q.get("ask")
    try:
        bid_f = float(bid) if bid not in (None, "") else None
    except (TypeError, ValueError):
        bid_f = None
    try:
        ask_f = float(ask) if ask not in (None, "") else None
    except (TypeError, ValueError):
        ask_f = None
    return bid_f, ask_f


def _book_valid(bid: Optional[float], ask: Optional[float]) -> bool:
    return (
        bid is not None
        and ask is not None
        and math.isfinite(bid)
        and math.isfinite(ask)
        and 0 < bid <= ask
    )


async def _desk_parity_marketfeed_quote(broker: Any, sec_int: int, timeout: float) -> Dict[str, Any]:
    """On-demand POST /marketfeed/quote — same REST contract as paper_desk._quotes_fetch (8504).

    Used when DhanBroker.get_marketfeed_quote short-circuits (e.g. 429 backoff) so paper
    fills still attempt the live pull the Paper Desk would make on Buy/Sell.
    """
    if not callable(getattr(broker, "is_configured", None)) or not broker.is_configured():
        return {}
    base = str(getattr(broker, "base_url", "") or "").rstrip("/")
    headers = getattr(broker, "_headers", lambda: {})()
    if not base or not headers:
        return {}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(
                f"{base}/marketfeed/quote",
                json={"NSE_FNO": [sec_int]},
                headers=headers,
            )
        if res.status_code != 200:
            log.debug("Desk-parity quote HTTP %s for %s", res.status_code, sec_int)
            return {}
        data = res.json().get("data") or {}
        if not isinstance(data, dict):
            return {}
        seg = data.get("NSE_FNO") or {}
        if not isinstance(seg, dict):
            return {}
        raw = seg.get(str(sec_int)) or seg.get(sec_int) or {}
        if not isinstance(raw, dict):
            return {}
        bid, ask = _bid_ask_from_quote(raw)
        out: Dict[str, Any] = {
            "bid": bid,
            "ask": ask,
        }
        lp = raw.get("last_price")
        if lp not in (None, ""):
            try:
                out["last_price"] = float(lp)
            except (TypeError, ValueError):
                pass
        return out
    except Exception as exc:
        log.debug("Desk-parity quote failed for %s: %s", sec_int, exc)
        return {}


async def executable_paper_quote(
    broker: Any,
    security_id: Union[str, int],
    side: str,
    detail: bool = False,
    max_retries: Optional[int] = None,
) -> Union[Optional[float], Tuple[Optional[float], Optional[str], float]]:
    """Fetch executable paper quote for exact security_id on demand.

    If detail=False (default):
        Returns float quote (ask for BUY, bid for SELL) or None.
    If detail=True:
        Returns tuple: (price, price_source, quote_age)
        where price_source is "REAL_QUOTE" on success, or (None, None, quote_age) on failure.
    """
    rules = _rules()
    max_request_sec = float(rules.get("max_request_seconds", 5.0))
    retries = int(max_retries if max_retries is not None else rules.get("max_retries", 1))
    desk_parity = bool(rules.get("desk_parity_direct_on_miss", True))
    started = time.monotonic()

    valid = False
    bid, ask = None, None
    elapsed = 0.0
    sec_int = int(security_id)

    for attempt in range(retries + 1):
        try:
            depth = await broker.get_marketfeed_quote({"NSE_FNO": [sec_int]})
            quote = _lookup_quote(depth, security_id)
            bid, ask = _bid_ask_from_quote(quote)
            if desk_parity and not _book_valid(bid, ask):
                desk_q = await _desk_parity_marketfeed_quote(broker, sec_int, max_request_sec)
                if desk_q:
                    quote = desk_q
                    bid, ask = _bid_ask_from_quote(quote)
            elapsed = time.monotonic() - started
            valid = _book_valid(bid, ask) and elapsed <= max_request_sec
            if valid:
                break
        except Exception as exc:
            log.debug("On-demand quote attempt %d failed for %s: %s", attempt + 1, security_id, exc)
        if attempt < retries:
            await asyncio.sleep(0.05 * (attempt + 1))

    if not valid:
        if detail:
            return None, None, time.monotonic() - started
        return None

    fill_price = ask if str(side).upper() == "BUY" else bid
    if detail:
        return fill_price, "REAL_QUOTE", elapsed
    return fill_price