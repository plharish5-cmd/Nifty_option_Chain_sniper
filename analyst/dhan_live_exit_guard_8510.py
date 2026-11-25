"""Pre-exit checks against Dhan /positions before live SELL churn."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _sid(raw: Any) -> str:
    return str(raw or "").strip()


def broker_net_qty(positions: Optional[List[Dict[str, Any]]], security_id: str) -> int:
    """Net open qty at broker for security_id; missing row => flat (0)."""
    sid = _sid(security_id)
    if not sid:
        return 0
    for p in positions or []:
        if _sid(p.get("securityId") or p.get("security_id")) == sid:
            return int(p.get("netQty") or p.get("net_qty") or 0)
    return 0


def broker_sell_avg(positions: Optional[List[Dict[str, Any]]], security_id: str) -> Optional[float]:
    sid = _sid(security_id)
    for p in positions or []:
        if _sid(p.get("securityId") or p.get("security_id")) != sid:
            continue
        for key in ("sellAvg", "sell_avg", "averageSellPrice", "daySellAvg"):
            v = p.get(key)
            if v is not None:
                try:
                    f = float(v)
                    if f > 0:
                        return f
                except (TypeError, ValueError):
                    pass
    return None