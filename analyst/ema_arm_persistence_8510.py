"""Persist Prototype / Reversal lane runtime across 8510 restarts (continuity only)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("ema_arm_persist_8510")

MJ_PERSIST_KEYS: Tuple[str, ...] = (
    "day",
    "reversal_fired_key",
    "stage2_fired_key",
    "stage3_fired_key",
    "reversal_pre_arm_15m",
)


def format_broker_rejection(raw: Any) -> str:
    """Human-readable broker rejection for orders book and audit (no secrets)."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if s.startswith("{"):
        try:
            j = json.loads(s)
            if not isinstance(j, dict):
                return s
            code = str(j.get("errorCode") or j.get("errorType") or "").strip()
            msg = str(
                j.get("errorMessage")
                or j.get("omsErrorDescription")
                or j.get("message")
                or j.get("error")
                or ""
            ).strip()
            if code and msg:
                return f"{code}: {msg}"
            return msg or code or s
        except (json.JSONDecodeError, TypeError):
            pass
    return s


# Markers that identify who rejected an order. LOCAL = the 8510 app blocked it before Dhan
# (it never reaches the broker); BROKER = Dhan said no. Lets the owner tell "the app stopped it"
# from "my broker stopped it" at a glance in the orders book.
_LOCAL_REJECT_MARKERS = (
    "available cash below",
    "not aligned to instrument tick size",
    "explicit live confirmation required",
    "directional bias",
    "funds snapshot unavailable",
    "already submitted",
    "option stop price would be non-positive",
    "daily gross loss limit",
)
_BROKER_REJECT_MARKERS = (
    "dh-",            # Dhan error codes, e.g. DH-905
    "invalid ip",
    "rms",
    "margin",
    "insufficient",
    "omserror",
    "exchange",
    "order param",
    "square off",
    "freeze",
)


def classify_rejection(raw: Any) -> Dict[str, str]:
    """Return {origin, reason, display} for an order rejection.

    origin: 'LOCAL' (8510 blocked it before Dhan), 'BROKER' (Dhan rejected it), or 'UNKNOWN'.
    reason: the cleaned human reason (format_broker_rejection). display: '[ORIGIN] reason'.
    Pure, never raises; does not change any trade decision.
    """
    reason = format_broker_rejection(raw)
    low = reason.lower()
    origin = "UNKNOWN"
    if any(m in low for m in _LOCAL_REJECT_MARKERS):
        origin = "LOCAL"
    elif any(m in low for m in _BROKER_REJECT_MARKERS):
        origin = "BROKER"
    display = (f"[{origin}] {reason}" if reason else f"[{origin}]") if origin != "UNKNOWN" else reason
    return {"origin": origin, "reason": reason, "display": display}


def mj_state_persist_slice(mj_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    src = mj_state if isinstance(mj_state, dict) else {}
    return {k: src[k] for k in MJ_PERSIST_KEYS if k in src}


def apply_mj_persist_slice(mj_state: Dict[str, Any], payload: Dict[str, Any]) -> None:
    for k, v in (payload or {}).items():
        if k in MJ_PERSIST_KEYS:
            mj_state[k] = v


def bootstrap_prototype_arm(
    backend: Any,
    b15: Any,
    proto_cfg: Dict[str, Any],
    session_day: str,
) -> str:
    """Resume persisted arm or fall back to cross recovery. Returns bootstrap mode."""
    state = getattr(backend, "_ema8_prototype_state", None) or {}
    resumed = bool(getattr(backend, "_proto_arm_loaded_from_db", False))
    arm = state.get("arm")
    persisted_day = str(state.get("day") or "").strip()
    if arm and persisted_day and persisted_day != session_day:
        # New session: let the evaluator apply its day rollover rules; do not rebuild here.
        resumed = False

    if resumed and arm:
        backend.log_ema8_prototype_event(
            event="ARM RESUMED",
            direction=arm.get("direction"),
            dynamic_8ema=arm.get("cross_e8"),
            reason="ARM RESUMED (persisted) — evaluating live touches from current tick",
        )
        state["day"] = session_day
        backend._save_ema8_prototype_state()
        return "resumed"

    if arm:
        state.setdefault("day", session_day)
        return "live"

    from analyst.prototype_arm_recovery_8510 import recover_missing_arm

    if recover_missing_arm(b15, state=state, cfg=proto_cfg, session_day=session_day):
        state["day"] = session_day
        backend._save_ema8_prototype_state()
        backend.log_ema8_prototype_event(
            event="ARM RECOVERED",
            direction=(state.get("arm") or {}).get("direction"),
            reason=(
                "ARM RECOVERED (rebuilt from cross — gap; touches during the gap are not traded). "
                "Persistence miss if this appears after a normal mid-session restart."
            ),
        )
        return "recovered"

    return "none"


def bootstrap_reversal_runtime(backend: Any, session_day: str) -> None:
    """Log honest reversal resume when mj_state was reloaded."""
    mj = getattr(backend, "_mj_sir_state", None) or {}
    if not getattr(backend, "_mj_sir_loaded_from_db", False):
        return
    if str(mj.get("day") or "") and str(mj.get("day")) != session_day:
        return
    if any(mj.get(k) for k in MJ_PERSIST_KEYS):
        log.info(
            "Reversal runtime RESUMED (persisted) for %s — fired_keys and pre-arm state restored",
            session_day,
        )