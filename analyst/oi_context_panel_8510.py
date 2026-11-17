"""8600 context panel — display-only. Must NEVER gate a trade.

Consumes readings already built by desk8600 board / bridge / chain reads. No protected
modules are imported or modified.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_SHOW = list(
    (
        "pcr_zone",
        "walls",
        "day_oi",
        "pcr_futures",
        "ticket",
        "high_conviction",
        "pcr_trinity",
        "smart_oi",
        "pe_ce_matrix",
        "cvd",
    )
)

LABELS: Dict[str, str] = {
    "pcr_zone": "PCR zone",
    "walls": "OI walls — room",
    "day_oi": "Day OI (09:15)",
    "pcr_futures": "PCR OI vs Futures",
    "ticket": "8600 ticket",
    "high_conviction": "8600 high conviction",
    "pcr_trinity": "PCR Trinity (8600)",
    "smart_oi": "Smart OI",
    "pe_ce_matrix": "PE-CE Matrix 2.0",
    "cvd": "CVD divergence",
}


def _row(label: str, item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    it = item if isinstance(item, dict) else {}
    d = it.get("direction")
    detail = str(it.get("detail") or "").strip()
    if d is None:
        value = "—"
        direction = None
    else:
        value = detail[:120] if detail else str(d)
        direction = str(d).upper()
    return {"id": label, "label": LABELS.get(label, label), "value": value, "direction": direction}


def _board_by_id(board: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for s in (board or {}).get("sources") or []:
        if isinstance(s, dict) and s.get("id"):
            out[str(s["id"])] = s
    return out


def _bridge_group(bridge: Optional[Dict[str, Any]], group: str) -> Dict[str, Any]:
    rows = (bridge or {}).get("rows") or []
    for r in rows:
        if isinstance(r, dict) and str(r.get("signal_group") or "") == group:
            d = str(r.get("direction") or "NEUTRAL").upper()
            if d not in ("CE", "PE"):
                d = "NEUTRAL"
            return {
                "direction": d,
                "detail": str(r.get("verdict") or r.get("reason") or "")[:120],
            }
    return {"direction": None, "detail": "8600 bridge — no row"}


def build_context_panel(
    *,
    board: Optional[Dict[str, Any]],
    bridge_last: Optional[Dict[str, Any]] = None,
    show: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Read-only rows for cockpit. Cannot block."""
    ids = [str(x) for x in (show or DEFAULT_SHOW)]
    by_id = _board_by_id(board)
    bridge = bridge_last or {}
    out: List[Dict[str, Any]] = []
    for rid in ids:
        if rid == "pcr_trinity":
            fresh = _bridge_group(bridge, "Fresh Change in OI")
            total = _bridge_group(bridge, "Cumulative Total OI")
            fut = _bridge_group(bridge, "PCR OI vs Futures")
            parts = []
            for name, it in (("Fresh", fresh), ("Total", total), ("Fut", fut)):
                if it.get("direction"):
                    parts.append(f"{name} {it['direction']}")
            item = {
                "direction": fresh.get("direction") or total.get("direction"),
                "detail": " · ".join(parts) if parts else "—",
            }
            out.append(_row(rid, item))
        elif rid == "smart_oi":
            out.append(_row(rid, _bridge_group(bridge, "Smart OI")))
        elif rid == "pe_ce_matrix":
            out.append(_row(rid, _bridge_group(bridge, "PE-CE Matrix 2.0")))
        elif rid == "cvd":
            out.append(_row(rid, _bridge_group(bridge, "CVD")))
        else:
            out.append(_row(rid, by_id.get(rid)))
    return out