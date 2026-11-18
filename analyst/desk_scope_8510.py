"""Desk lane scope — which scanner lanes may emit signals (config-only, reversible)."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from analyst.offhours_cleanup_8510 import parse_contract_expiry_date

DEFAULT_DESK_SCOPE: Dict[str, Any] = {
    "enabled_strategy_lanes": None,  # None = all legacy lanes on
    "stage2_enabled": True,
    "stage3_enabled": True,
    "flagship_1h_enabled": True,
    "reversal_enabled": True,
    "prototype_enabled": True,
    "ticket_lane_enabled": True,
    "matrix_scalp_enabled": True,
    "pa_sr_live_enabled": True,
}


def load_desk_scope(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_DESK_SCOPE)
    raw = (scanner_cfg or {}).get("desk_scope_8510")
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULT_DESK_SCOPE})
    rev = (scanner_cfg or {}).get("reversal") or {}
    if isinstance(rev, dict) and "enabled" in rev:
        out["reversal_enabled"] = bool(rev.get("enabled"))
    proto = (scanner_cfg or {}).get("ema8_prototype") or {}
    if isinstance(proto, dict) and "enabled" in proto:
        out["prototype_enabled"] = bool(proto.get("enabled"))
    ticket = (scanner_cfg or {}).get("desk8600_ticket_lane") or {}
    if isinstance(ticket, dict) and "enabled" in ticket:
        out["ticket_lane_enabled"] = bool(ticket.get("enabled"))
    return out


def enabled_equity_lane_labels(scanner_cfg: Optional[Dict[str, Any]]) -> List[str]:
    ds = load_desk_scope(scanner_cfg)
    labels: List[str] = []
    if ds.get("prototype_enabled"):
        labels.append("8 EMA Prototype")
    if ds.get("reversal_enabled"):
        labels.append("4/8 Crossover")
    if ds.get("flagship_1h_enabled"):
        labels.append("1H 8 EMA")
    if ds.get("stage2_enabled"):
        labels.append("Stage 2")
    if ds.get("stage3_enabled"):
        labels.append("Stage 3")
    return labels


def strategy_id_allowed(scanner_cfg: Optional[Dict[str, Any]], strategy_id: str) -> bool:
    sid = str(strategy_id or "").upper()
    if not sid:
        return True
    ds = load_desk_scope(scanner_cfg)
    if sid.startswith("EMA8_PROTOTYPE"):
        return bool(ds.get("prototype_enabled"))
    if sid.startswith("CROSSOVER"):
        return bool(ds.get("reversal_enabled"))
    if sid.startswith("STAGE_2"):
        return bool(ds.get("stage2_enabled"))
    if sid.startswith("STAGE_3"):
        return bool(ds.get("stage3_enabled"))
    if sid.startswith("PA_SR"):
        return bool(ds.get("pa_sr_live_enabled"))
    if "FLAGSHIP" in sid or sid.startswith("H1_"):
        return bool(ds.get("flagship_1h_enabled"))
    if sid.startswith("DESK8600_TICKET"):
        return bool(ds.get("ticket_lane_enabled"))
    if sid.startswith("MATRIX") or "RIBBON" in sid:
        return bool(ds.get("matrix_scalp_enabled"))
    if sid.startswith("PA_SR"):
        return bool(ds.get("pa_sr_live_enabled"))
    return True


def is_test_fixture_text(*parts: Any) -> bool:
    blob = " ".join(str(p or "") for p in parts).upper()
    if "TEST FIXTURE" in blob:
        return True
    if "Q_GATE" in blob or "OD_V15" in blob:
        return True
    if "_PYTEST" in blob or "PYTEST" in blob and "FIXTURE" in blob:
        return True
    return False


def is_disabled_lane_setup(scanner_cfg: Optional[Dict[str, Any]], strategy_id: str, setup: str = "") -> bool:
    sid = str(strategy_id or "").upper()
    if not sid and setup:
        blob = setup.upper()
        if "STAGE 2" in blob or "STAGE_2" in blob:
            sid = "STAGE_2"
        elif "STAGE 3" in blob or "STAGE_3" in blob:
            sid = "STAGE_3"
        elif "CROSSOVER" in blob or "REVERSAL" in blob:
            sid = "CROSSOVER"
        elif "PROTOTYPE" in blob:
            sid = "EMA8_PROTOTYPE"
        elif "TICKET" in blob:
            sid = "DESK8600_TICKET"
    if not sid:
        return False
    return not strategy_id_allowed(scanner_cfg, sid)


def order_book_row_visible(
    scanner_cfg: Optional[Dict[str, Any]],
    row: Dict[str, Any],
    *,
    today: Optional[date] = None,
    hidden_ids: Optional[set] = None,
) -> bool:
    rid = str(row.get("order_id") or row.get("position_id") or "")
    if hidden_ids is not None and rid and rid in hidden_ids:
        return False
    if row.get("execution_hidden") or row.get("book_hidden"):
        return False
    if is_test_fixture_text(row.get("strategy_setup"), row.get("strategy_name"), row.get("order_id")):
        return False
    if is_disabled_lane_setup(
        scanner_cfg,
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_setup") or row.get("strategy_name") or ""),
    ):
        return False
    if today is not None:
        exp = parse_contract_expiry_date(str(row.get("symbol") or ""))
        if exp is not None and exp < today:
            return False
    return True