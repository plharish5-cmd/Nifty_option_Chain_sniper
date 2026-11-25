"""Hard fail-closed gate before any live Dhan entry (BUY) HTTP order."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from analyst.lane_live_routing_8510 import order_will_be_paper

log = logging.getLogger("terminal8510")


def live_dhan_entry_allowed(backend: Any, strategy_id: Optional[str]) -> Dict[str, Any]:
    """Return {ok, reason}. Only the three cockpit lanes may send live entry orders."""
    sid = str(strategy_id or "").strip().upper()
    if not sid:
        return {
            "ok": False,
            "reason": "LIVE_GATE_BLOCKED: missing strategy_id on broker entry order",
        }
    if order_will_be_paper(backend, sid):
        return {
            "ok": False,
            "reason": f"LIVE_GATE_BLOCKED: strategy {sid} is paper-only on this desk",
        }
    return {"ok": True, "reason": ""}