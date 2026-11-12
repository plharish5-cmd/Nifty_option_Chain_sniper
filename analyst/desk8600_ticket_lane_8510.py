"""8600 Ticket lane: an 8600 entry ticket opens a trade in 8510.

WHAT A TICKET IS
    On every closed 3-minute bar the 8600 app's tape gives a badge. Four of
    them are entry tickets:
        🎯 DIP BUY CE        🎯 SNIPER BREAKOUT      -> buy CE
        🎯 PULLBACK SELL     🎯 SNIPER BREAKDOWN     -> buy PE
    The 8600 app itself already withholds tickets before 09:30, from 15:10,
    on a conflict with an opposite ticket in the last three bars, and on a
    repeat of the same ticket at nearly the same price.

WHAT THIS LANE DOES
    When the newest closed bar carries a ticket, and that bar has not been
    acted on already, it emits ONE ordinary 8510 signal. From there the trade
    goes down the same path as every other lane: board filters, contract
    selection, the order chaser, the paper ledger, the live route.
      strategy ids   DESK8600_TICKET_LONG / DESK8600_TICKET_SHORT
      exit           spot target/stop points OR option target/stop points,
                     whichever comes first, plus the 15:20 square-off
                     (the same fixed exit as Stage 2/3 and the Prototype)
      one at a time  no new ticket trade while a ticket trade is open

OWNER SWITCHES (cockpit)
    enabled               lane on / off                     (off until pressed)
    route                 LIVE_ALLOWED: follows the desk — real order when the
                          desk is armed live, paper otherwise.
                          PAPER_ONLY: never sends a real order.
    high_conviction_only  take a ticket only when 8600 marks it high conviction
    target_pts / sl_pts   exit distances

It reads the signal bridge's last result and nothing else. It imports no
locked module. Never raises.

STATUS: unproven. Never run against a live 8600 app.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

SID_LONG = "DESK8600_TICKET_LONG"
SID_SHORT = "DESK8600_TICKET_SHORT"
PREFIX = "DESK8600_TICKET"
ROUTES = ("LIVE_ALLOWED", "PAPER_ONLY")

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": False,
    "route": "LIVE_ALLOWED",
    "high_conviction_only": False,
    "target_pts": 40.0,
    "sl_pts": 40.0,
    "max_ticket_age_sec": 200,       # the ticket belongs to the bar that has just closed
}


def load_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    out.update((scanner_cfg or {}).get("desk8600_ticket_lane") or {})
    out["route"] = str(out.get("route") or "LIVE_ALLOWED").upper()
    if out["route"] not in ROUTES:
        out["route"] = "LIVE_ALLOWED"
    out["enabled"] = bool(out.get("enabled"))
    out["high_conviction_only"] = bool(out.get("high_conviction_only"))
    return out


def apply_update(scanner_cfg: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Cockpit change, in place. Unknown keys and bad values are ignored."""
    cur = dict((scanner_cfg or {}).get("desk8600_ticket_lane") or {})
    u = updates or {}
    truthy = lambda v: v is True or str(v).lower() == "true"
    for key in ("enabled", "high_conviction_only"):
        if key in u:
            cur[key] = truthy(u[key])
    if str(u.get("route") or "").upper() in ROUTES:
        cur["route"] = str(u["route"]).upper()
    for key in ("target_pts", "sl_pts"):
        if key in u:
            try:
                val = float(u[key])
            except (TypeError, ValueError):
                continue
            if 1.0 <= val <= 500.0:
                cur[key] = val
    scanner_cfg["desk8600_ticket_lane"] = cur
    return load_cfg(scanner_cfg)


def is_ticket_lane(strategy_id: Any) -> bool:
    return str(strategy_id or "").upper().startswith(PREFIX)


def read_ticket(bridge_last: Optional[Dict[str, Any]], now: datetime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The newest closed bar's ticket, or why there is none.

    {"ticket": bool, "direction": "CE"|"PE"|None, "badge": str, "high": bool,
     "spot": float|None, "bar": iso str|None, "age_sec": float|None, "why": str}
    """
    out: Dict[str, Any] = {"ticket": False, "direction": None, "badge": "", "high": False,
                           "spot": None, "bar": None, "age_sec": None, "why": ""}
    rows = {str(r.get("signal_group")): r for r in ((bridge_last or {}).get("rows") or []) if isinstance(r, dict)}
    act = rows.get("Actionable Setups")
    if not act or str(act.get("verdict") or "") == "UNAVAILABLE":
        out["why"] = str((act or {}).get("reason") or "8600 app not answering")[:120]
        return out
    badge = str(act.get("verdict") or "")
    out["badge"] = badge
    out["bar"] = act.get("source_ts")
    out["spot"] = act.get("spot")
    try:
        age = max(0.0, (now - datetime.fromisoformat(str(act.get("source_ts")))).total_seconds())
    except (TypeError, ValueError):
        out["why"] = "8600 bar time unreadable"
        return out
    out["age_sec"] = round(age)
    direction = str(act.get("direction") or "").upper()
    hc = rows.get("High Conviction") or {}
    out["high"] = bool(hc.get("significant")) and str(hc.get("verdict") or "") == badge
    if not badge.startswith("🎯") or direction not in ("CE", "PE") or not bool(act.get("significant")):
        out["why"] = f"no entry ticket on the latest bar ({badge or 'nothing'})"
        return out
    if age > float(cfg.get("max_ticket_age_sec", 200)):
        out["why"] = f"ticket bar closed {int(age)} s ago — too old to act on"
        return out
    if cfg.get("high_conviction_only") and not out["high"]:
        out["why"] = f"{badge} — not high conviction"
        return out
    out.update(ticket=True, direction=direction)
    return out


def evaluate(bridge_last: Optional[Dict[str, Any]], scanner_cfg: Optional[Dict[str, Any]],
             state: Dict[str, Any], now: datetime, *, has_open_position: bool,
             spot: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """(signals, status). `state` remembers which bar was acted on, so a ticket fires once."""
    cfg = load_cfg(scanner_cfg)
    status: Dict[str, Any] = {
        "enabled": cfg["enabled"], "route": cfg["route"], "high_conviction_only": cfg["high_conviction_only"],
        "target_pts": float(cfg["target_pts"]), "sl_pts": float(cfg["sl_pts"]),
        "state": "OFF", "detail": "lane is switched off", "last_fired": state.get("last_fired"),
    }
    try:
        t = read_ticket(bridge_last, now, cfg)
        status.update(badge=t["badge"], bar=t["bar"], bar_age_sec=t["age_sec"], high=t["high"])
        if not cfg["enabled"]:
            if t["ticket"]:
                status["detail"] = f"lane is off — 8600 ticket seen: {t['badge']}"
            return [], status
        if not t["ticket"]:
            status.update(state="WAITING", detail=t["why"])
            return [], status
        key = f"{t['bar']}|{t['direction']}"
        if state.get("last_key") == key:
            status.update(state="TAKEN", detail=f"{t['badge']} — already acted on this bar")
            return [], status
        if has_open_position:
            status.update(state="IN TRADE", detail=f"{t['badge']} seen — a ticket trade is already open")
            return [], status
        px = float(spot or t["spot"] or 0.0)
        if px <= 0:
            status.update(state="WAITING", detail="no spot price")
            return [], status
        bullish = t["direction"] == "CE"
        paper_only = cfg["route"] == "PAPER_ONLY"
        sig = {
            "strategy_id": SID_LONG if bullish else SID_SHORT,
            "setup_name": f"8600 Ticket — {t['badge'].replace('🎯', '').strip().title()}",
            "direction": "LONG" if bullish else "SHORT",
            "opt_side": t["direction"],
            "target_pts": float(cfg["target_pts"]),
            "sl_pts": float(cfg["sl_pts"]),
            "index_price": px,
            "spot_entry": px,
            "conviction": "8600 TICKET" + (" · HIGH" if t["high"] else ""),
            "paper_only": paper_only,
            "desk8600_ticket_lane": True,
            "execution_mode": cfg["route"],
            "details": f"8600 3-min bar {str(t['bar'])[11:16]} | {t['badge']} | spot {px:.1f}",
        }
        state["last_key"] = key
        state["last_fired"] = f"{now.strftime('%H:%M:%S')} {t['badge']}"
        status.update(state="TICKET", detail=f"{t['badge']} — signal sent", last_fired=state["last_fired"])
        return [sig], status
    except Exception as exc:  # the lane must never break the scanner
        status.update(state="ERROR", detail=f"error: {type(exc).__name__}")
        return [], status
