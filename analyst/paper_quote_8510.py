"""Fresh REST depth only: never use signal prices or position LTP as fills."""
import json
import math
import time
from pathlib import Path

async def executable_paper_quote(broker, security_id, side):
    rules = json.loads(Path(__file__).with_name('paper_quote_rules_8510.json').read_text())
    started = time.monotonic()
    depth = await broker.get_marketfeed_quote({'NSE_FNO': [int(security_id)]})
    elapsed = time.monotonic() - started
    quote = depth.get(str(security_id)) or {}
    bid, ask = quote.get('bid'), quote.get('ask')
    try:
        bid, ask = float(bid), float(ask)
        valid = math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
    except (ValueError, TypeError):
        valid = False
    if not valid or elapsed > float(rules['max_request_seconds']):
        return None
    return ask if str(side).upper() == 'BUY' else bid
