"""Fresh REST depth only: never use signal prices or position LTP as fills."""
import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Optional, Tuple, Union

log = logging.getLogger("paper_quote_8510")


async def executable_paper_quote(
    broker: Any,
    security_id: Union[str, int],
    side: str,
    detail: bool = False,
    max_retries: int = 1,
) -> Union[Optional[float], Tuple[Optional[float], Optional[str], float]]:
    """Fetch executable paper quote for exact security_id on demand.

    If detail=False (default):
        Returns float quote (ask for BUY, bid for SELL) or None.
    If detail=True:
        Returns tuple: (price, price_source, quote_age)
        where price_source is "REAL_QUOTE" on success, or (None, None, quote_age) on failure.
    """
    rules = json.loads(Path(__file__).with_name('paper_quote_rules_8510.json').read_text())
    max_request_sec = float(rules.get('max_request_seconds', 5.0))
    started = time.monotonic()

    valid = False
    bid, ask = None, None
    elapsed = 0.0

    # On-demand quote fetch loop with retries for live robustness
    for attempt in range(max_retries + 1):
        try:
            sec_int = int(security_id)
            depth = await broker.get_marketfeed_quote({'NSE_FNO': [sec_int]})
            elapsed = time.monotonic() - started
            if isinstance(depth, dict):
                quote = (
                    depth.get(str(security_id))
                    or depth.get(sec_int)
                    or depth.get(security_id)
                    or {}
                )
            else:
                quote = {}
            bid, ask = quote.get('bid'), quote.get('ask')
            try:
                bid, ask = float(bid), float(ask)
                valid = math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
            except (ValueError, TypeError):
                valid = False
            if valid and elapsed <= max_request_sec:
                break
        except Exception as exc:
            log.debug("On-demand quote attempt %d failed for %s: %s", attempt + 1, security_id, exc)

    if not valid or elapsed > max_request_sec:
        if detail:
            return None, None, elapsed
        return None

    fill_price = ask if str(side).upper() == 'BUY' else bid
    if detail:
        return fill_price, "REAL_QUOTE", elapsed
    return fill_price
