"""Per-lane real money: desk LIVE + lane route + lane opt-in (Prototype, Reversal, 1H PA)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

LIVE_ROUTE_VALUES = frozenset({"LIVE_ALLOWED", "LIVE", "REAL", "BOTH"})


def _desk_live_armed(backend: Any) -> bool:
    return (
        str(getattr(backend, "execution_mode", "") or "").upper() == "BOTH"
        and bool(getattr(backend, "_real_money_enabled", False))
        and not bool(getattr(backend, "kill_switch_active", False))
    )


def _lane_block(scanner_cfg: Optional[Dict[str, Any]], lane: str) -> Dict[str, Any]:
    sc = scanner_cfg or {}
    if lane == "prototype":
        return dict(sc.get("ema8_prototype") or {})
    if lane == "reversal":
        return dict(sc.get("reversal") or {})
    if lane == "flagship_pa":
        return dict(sc.get("flagship_1h_pa") or sc.get("h1_8ema_homework") or {})
    return {}


def lane_route_allows_live(block: Dict[str, Any]) -> bool:
    mode = str(block.get("execution_mode") or "PAPER_ONLY").upper()
    return mode in LIVE_ROUTE_VALUES


def lane_live_opt_in(block: Dict[str, Any]) -> bool:
    return bool(block.get("live_trade_enabled", False))


def lane_label_for_cockpit(
    *,
    desk_live: bool,
    route_allows_live: bool,
    opt_in: bool,
    order_will_be_paper: bool,
) -> str:
    if desk_live and route_allows_live and opt_in and not order_will_be_paper:
        return "REAL MONEY — this lane may send broker orders when the desk is armed"
    if route_allows_live and opt_in and not desk_live:
        return "PAPER now — desk not armed live (lane is enabled for real money when you arm LIVE)"
    if route_allows_live and not opt_in:
        return "PAPER only — check “Real money for this lane” to allow broker orders when the desk is live"
    return "Paper only — lane route is PAPER_ONLY"


def strategy_id_to_lane(strategy_id: str) -> Optional[str]:
    sid = str(strategy_id or "").upper()
    if sid.startswith("EMA8_PROTOTYPE"):
        return "prototype"
    if sid.startswith("CROSSOVER"):
        return "reversal"
    if "FLAGSHIP" in sid or sid.startswith("H1_") or "PA_SR" in sid:
        return "flagship_pa"
    return None


def order_will_be_paper(backend: Any, strategy_id: str) -> bool:
    """True = no real broker leg (paper logging only)."""
    sid = str(strategy_id or "").upper()
    if sid.startswith("RIBBON_SCALP") or sid.startswith("MATRIX_SCALP"):
        return True
    lane = strategy_id_to_lane(strategy_id)
    if not lane:
        return True
    block = _lane_block(getattr(backend, "scanner_cfg", None), lane)
    if not lane_route_allows_live(block):
        return True
    if not lane_live_opt_in(block):
        return True
    return not _desk_live_armed(backend)


def cockpit_route_tuple(backend: Any, lane: str) -> Tuple[bool, bool, str]:
    """(order_paper, live_capable_display, route sentence)."""
    block = _lane_block(getattr(backend, "scanner_cfg", None), lane)
    desk = _desk_live_armed(backend)
    route_ok = lane_route_allows_live(block)
    opt = lane_live_opt_in(block)
    paper = not (desk and route_ok and opt)
    label = lane_label_for_cockpit(
        desk_live=desk,
        route_allows_live=route_ok,
        opt_in=opt,
        order_will_be_paper=paper,
    )
    return paper, (route_ok and opt), label


def signal_will_be_paper(backend: Any, sig: Dict[str, Any]) -> bool:
    sid = str(sig.get("strategy_id") or "")
    if order_will_be_paper(backend, sid):
        return True
    if sig.get("paper_only") or sig.get("ema8_prototype_paper_only") or sig.get("mj_sir_paper_only"):
        return True
    if sig.get("matrix_scalp") or sig.get("pa_homework"):
        if lane := strategy_id_to_lane(sid):
            if order_will_be_paper(backend, sid):
                return True
    return False