"""v2.0.47 — real-order PREFLIGHT: would a real order be accepted right now? (places nothing).

Pure aggregation over the pieces that decide whether a real Dhan order succeeds or rejects:
Dhan connected, token valid, machine IP allowlisted, funds vs the order's cost, and whether the
desk is actually LIVE-armed (else a "real" order downgrades to paper). The backend gathers the
inputs and calls build_order_preflight(); this module touches no network and never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _num(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_order_preflight(
    masked_cfg: Optional[Dict[str, Any]],
    funds_balance: Any,
    est_order_cost: Any,
    *,
    desk_live_armed: bool = False,
    lane_live: bool = True,
) -> Dict[str, Any]:
    """Return a structured readiness report. Does NOT place an order.

    Shape: {ready_to_place, would_be_real, checks: [{key, label, ok, detail}], blockers: [..],
            verdict, summary}. ready_to_place = Dhan would ACCEPT the order (connected+token+IP+funds).
    would_be_real = it would route to real money (desk live-armed + lane live) rather than paper.
    """
    cfg = masked_cfg or {}
    bal = _num(funds_balance)
    cost = _num(est_order_cost)

    connected = bool(cfg.get("dhan_connected"))
    token_ok = bool(cfg.get("token_configured")) and not bool(cfg.get("is_expired"))
    ip_ok = bool(cfg.get("ip_matched"))
    funds_ok = bal > 0 and bal >= cost

    checks = [
        {"key": "connected", "label": "Dhan API connected", "ok": connected,
         "detail": cfg.get("last_status") or ("connected" if connected else "not connected")},
        {"key": "token", "label": "Access token valid", "ok": token_ok,
         "detail": f"valid till {cfg.get('token_valid_till', '--')} ({cfg.get('days_left', '--')})"
                   if token_ok else "token missing or EXPIRED — regenerate it in Dhan"},
        {"key": "ip", "label": "Machine IP allowlisted", "ok": ip_ok,
         "detail": (f"IP {cfg.get('current_ip', '?')} matches allowlist" if ip_ok
                    else f"IP {cfg.get('current_ip', '?')} NOT in Dhan allowlist "
                         f"(allowlisted: {cfg.get('whitelisted_ip', '?')}) — DH-905 will reject")},
        {"key": "funds", "label": "Sufficient funds", "ok": funds_ok,
         "detail": (f"₹{bal:,.2f} available vs ₹{cost:,.2f} needed" if funds_ok
                    else (f"₹{bal:,.2f} available, need ₹{cost:,.2f}" if bal > 0
                          else "funds read ₹0 — check the Dhan funds feed / key"))},
    ]
    blockers = [c["label"] for c in checks if not c["ok"]]
    ready = not blockers

    live_armed = bool(desk_live_armed) and bool(lane_live)
    checks.append({
        "key": "live_armed", "label": "Desk LIVE-armed (else paper)", "ok": live_armed,
        "detail": "desk + lane are LIVE — order would route REAL" if live_armed
                  else "desk/lane is PAPER — a test order would fill as PAPER, not real money",
    })

    if ready and live_armed:
        verdict, summary = "READY_REAL", "✅ A real order would be ACCEPTED and routed to real money."
    elif ready and not live_armed:
        verdict, summary = "READY_PAPER", "✅ Order checks pass, but the desk is PAPER — it would fill as paper, not real."
    else:
        verdict = "NOT_READY"
        summary = "🔴 A real order would be REJECTED: " + ", ".join(blockers) + "."

    return {
        "ready_to_place": ready,
        "would_be_real": live_armed,
        "checks": checks,
        "blockers": blockers,
        "verdict": verdict,
        "summary": summary,
    }
