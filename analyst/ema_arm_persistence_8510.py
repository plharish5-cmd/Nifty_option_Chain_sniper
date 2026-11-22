"""Persist Prototype / Reversal lane runtime across 8510 restarts (continuity only)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Tuple

log = logging.getLogger("ema_arm_persist_8510")

MJ_PERSIST_KEYS: Tuple[str, ...] = (
    "day",
    "reversal_fired_key",
    "stage2_fired_key",
    "stage3_fired_key",
    "reversal_pre_arm_15m",
    "reversal_arm",
    "reversal_sl_blocked_side",
    "flagship_resume",
    "flagship_fired_key",
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
    state = getattr(backend, "_ema8_prototype_state", None)
    if state is None:
        state = {}
        backend._ema8_prototype_state = state
    resumed = bool(getattr(backend, "_proto_arm_loaded_from_db", False))
    arm = state.get("arm")
    persisted_day = str(state.get("day") or "").strip()
    if arm and persisted_day and persisted_day != session_day:
        # New session: let the evaluator apply its day rollover rules; do not rebuild here.
        resumed = False

    if resumed and arm:
        reason = "ARM RESUMED (persisted) — evaluating live touches from current tick"
        backend.log_ema8_prototype_event(
            event="ARM RESUMED",
            direction=arm.get("direction"),
            dynamic_8ema=arm.get("cross_e8"),
            reason=reason,
        )
        from analyst.resume_all3_8510 import record_lane_resume

        record_lane_resume(
            backend,
            "prototype",
            "ARM RESUMED",
            reason,
            direction=arm.get("direction"),
            state=state,
            session_day=session_day,
        )
        state["day"] = session_day
        backend._ema8_prototype_state = state
        backend._save_ema8_prototype_state()
        return "resumed"

    if arm:
        state.setdefault("day", session_day)
        backend._ema8_prototype_state = state
        return "live"

    from analyst.prototype_arm_recovery_8510 import recover_missing_arm

    if recover_missing_arm(b15, state=state, cfg=proto_cfg, session_day=session_day):
        state["day"] = session_day
        backend._ema8_prototype_state = state
        backend._save_ema8_prototype_state()
        reason = (
            "ARM RECOVERED (rebuilt from cross — gap; touches during the gap are not traded). "
            "Persistence miss if this appears after a normal mid-session restart."
        )
        backend.log_ema8_prototype_event(
            event="ARM RECOVERED",
            direction=(state.get("arm") or {}).get("direction"),
            reason=reason,
        )
        from analyst.resume_all3_8510 import record_lane_resume

        record_lane_resume(
            backend,
            "prototype",
            "ARM RECOVERED",
            reason,
            direction=(state.get("arm") or {}).get("direction"),
            state=state,
            session_day=session_day,
        )
        return "recovered"

    return "none"


def _reversal_persisted(backend: Any, session_day: str) -> bool:
    mj = getattr(backend, "_mj_sir_state", None) or {}
    if not getattr(backend, "_mj_sir_loaded_from_db", False):
        return False
    if str(mj.get("day") or "") and str(mj.get("day")) != session_day:
        return False
    if isinstance(mj.get("reversal_arm"), dict) and mj["reversal_arm"].get("side"):
        return True
    if isinstance(mj.get("reversal_pre_arm_15m"), dict):
        return True
    return bool(mj.get("reversal_fired_key") or mj.get("reversal_sl_blocked_side"))


def bootstrap_reversal_arm(
    backend: Any,
    b15: Any,
    scanner_cfg: Dict[str, Any],
    session_day: str,
) -> str:
    """Resume persisted reversal slice or recover from completed candles."""
    mj = getattr(backend, "_mj_sir_state", None)
    if mj is None:
        mj = {}
        backend._mj_sir_state = mj

    if _reversal_persisted(backend, session_day):
        from analyst.resume_all3_8510 import record_lane_resume

        arm = mj.get("reversal_arm") or {}
        side = arm.get("side") or (mj.get("reversal_pre_arm_15m") or {}).get("side")
        record_lane_resume(
            backend,
            "reversal",
            "ARM RESUMED",
            "ARM RESUMED (persisted) — evaluating live touches from current tick",
            direction="LONG" if str(side).upper() in ("UP", "LONG") else "SHORT" if side else None,
            state={"reversal_arm": arm, "reversal_pre_arm_15m": mj.get("reversal_pre_arm_15m")},
            session_day=session_day,
        )
        mj["day"] = session_day
        backend._save_mj_sir_runtime_state()
        log.info("Reversal ARM RESUMED (persisted) for %s", session_day)
        return "resumed"

    from analyst.reversal_arm_recovery_8510 import recover_reversal_arm
    from analyst.resume_all3_8510 import record_lane_resume

    if b15 is not None and recover_reversal_arm(b15, mj_state=mj, scanner_cfg=scanner_cfg, session_day=session_day):
        record_lane_resume(
            backend,
            "reversal",
            "ARM RECOVERED",
            (
                "ARM RECOVERED (rebuilt from cross — gap; touches during the gap are not traded). "
                "Persistence miss if this appears after a normal mid-session restart."
            ),
            direction="LONG" if mj.get("reversal_arm", {}).get("side") == "UP" else "SHORT",
            state=mj,
            session_day=session_day,
        )
        backend._save_mj_sir_runtime_state()
        log.info("Reversal ARM RECOVERED for %s", session_day)
        return "recovered"

    return "none"


def bootstrap_flagship_arm(
    backend: Any,
    b15: Any,
    scanner_cfg: Dict[str, Any],
    session_day: str,
) -> str:
    """Resume persisted 1H Flagship slice or recover from closed 15m confirmation."""
    mj = getattr(backend, "_mj_sir_state", None)
    if mj is None:
        mj = {}
        backend._mj_sir_state = mj

    fr = mj.get("flagship_resume")
    loaded = getattr(backend, "_mj_sir_loaded_from_db", False)
    if loaded and isinstance(fr, dict) and fr.get("armed") and str(fr.get("day") or mj.get("day") or session_day) == session_day:
        from analyst.resume_all3_8510 import record_lane_resume

        record_lane_resume(
            backend,
            "flagship",
            "ARM RESUMED",
            "ARM RESUMED (persisted) — next valid live 15m evaluation may trigger",
            direction=str(fr.get("direction") or "").upper() or None,
            state=fr,
            session_day=session_day,
        )
        mj["day"] = session_day
        fr["day"] = session_day
        status = getattr(backend, "strategy_status", None)
        if isinstance(status, dict):
            status.setdefault("flagship_1h", {})
            status["flagship_1h"]["state"] = "ARMED"
            status["flagship_1h"]["armed"] = True
        backend._save_mj_sir_runtime_state()
        return "resumed"

    no_entry_after = str((scanner_cfg or {}).get("flagship_no_entry_after") or "15:15")
    from analyst.flagship_arm_recovery_8510 import recover_flagship_arm
    from analyst.resume_all3_8510 import record_lane_resume

    if b15 is not None and recover_flagship_arm(b15, mj_state=mj, session_day=session_day, no_entry_after=no_entry_after):
        fr = mj.get("flagship_resume") or {}
        record_lane_resume(
            backend,
            "flagship",
            "ARM RECOVERED",
            (
                "ARM RECOVERED (rebuilt from 15m confirmation — gap; downtime triggers are not traded). "
                "Persistence miss if this appears after a normal mid-session restart."
            ),
            direction=str(fr.get("direction") or "").upper() or None,
            state=fr,
            session_day=session_day,
        )
        status = getattr(backend, "strategy_status", None)
        if isinstance(status, dict):
            status.setdefault("flagship_1h", {})
            status["flagship_1h"]["state"] = "ARMED"
            status["flagship_1h"]["armed"] = True
        backend._save_mj_sir_runtime_state()
        return "recovered"

    return "none"


def bootstrap_reversal_runtime(backend: Any, session_day: str) -> None:
    """Backward-compatible shim — use bootstrap_reversal_arm when b15 is available."""
    bootstrap_reversal_arm(backend, None, getattr(backend, "scanner_cfg", None) or {}, session_day)