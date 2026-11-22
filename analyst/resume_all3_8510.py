"""Resume-on-open contract for Prototype, Reversal, and 1H Flagship (Track 4)."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger("resume_all3_8510")


def record_lane_resume(
    backend: Any,
    lane: str,
    event: str,
    reason: str,
    *,
    direction: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
    session_day: Optional[str] = None,
) -> None:
    """Decision-audit row for ARM RESUMED / ARM RECOVERED (never blocks execution)."""
    try:
        from analyst.decision_audit_8510 import record

        sid = {
            "prototype": "EMA8_PROTOTYPE",
            "reversal": "CROSSOVER_BULLISH",
            "flagship": "FLAGSHIP_1H_LONG",
        }.get(lane, "EMA8_PROTOTYPE")
        row = dict(state or {})
        if direction:
            row["direction"] = direction
        if session_day:
            row["session_day"] = session_day
        elif not row.get("session_day"):
            mj = getattr(backend, "_mj_sir_state", None) or {}
            proto = getattr(backend, "_ema8_prototype_state", None) or {}
            row["session_day"] = str(
                mj.get("day") or proto.get("day") or row.get("day") or ""
            )
        record(
            backend,
            {"strategy_id": sid, "direction": direction or row.get("direction")},
            str(event).upper().replace(" ", "_"),
            reason,
            lane=lane,
            state=row,
        )
    except Exception as exc:
        log.debug("resume audit skip (%s): %s", lane, exc)


def _reversal_has_persisted_slice(mj: Dict[str, Any], session_day: str) -> bool:
    if str(mj.get("day") or "") and str(mj.get("day")) != session_day:
        return False
    if isinstance(mj.get("reversal_arm"), dict) and mj["reversal_arm"].get("side"):
        return True
    if isinstance(mj.get("reversal_pre_arm_15m"), dict):
        return True
    return bool(mj.get("reversal_fired_key"))


def _flagship_has_persisted_slice(mj: Dict[str, Any], session_day: str) -> bool:
    fr = mj.get("flagship_resume")
    if not isinstance(fr, dict):
        return False
    if str(fr.get("day") or mj.get("day") or "") and str(fr.get("day") or mj.get("day")) != session_day:
        return False
    return bool(fr.get("armed") or fr.get("direction"))


def bootstrap_all_strategies_on_open(
    backend: Any,
    b15: Any,
    proto_cfg: Dict[str, Any],
    session_day: str,
) -> Dict[str, str]:
    """Prototype + Reversal + Flagship bootstrap; mirrors backend reconstruct fork."""
    from analyst.arm_reconstruct_8510 import reconstruct_arms_on_startup
    from analyst.ema_arm_persistence_8510 import (
        bootstrap_flagship_arm,
        bootstrap_prototype_arm,
        bootstrap_reversal_arm,
    )

    modes: Dict[str, str] = {"prototype": "none", "reversal": "none", "flagship": "none"}
    scfg = getattr(backend, "scanner_cfg", None) or {}

    if getattr(backend, "_proto_arm_loaded_from_db", False) and (
        backend._ema8_prototype_state.get("arm") or {}
    ).get("direction"):
        modes["prototype"] = bootstrap_prototype_arm(backend, b15, proto_cfg, session_day)
        modes["reversal"] = bootstrap_reversal_arm(backend, b15, scfg, session_day)
        modes["flagship"] = bootstrap_flagship_arm(backend, b15, scfg, session_day)
    else:
        recon = reconstruct_arms_on_startup(backend, session_day)
        if recon and recon.applied:
            modes["reversal"] = bootstrap_reversal_arm(backend, b15, scfg, session_day)
            modes["flagship"] = bootstrap_flagship_arm(backend, b15, scfg, session_day)
            if backend._ema8_prototype_state.get("arm"):
                modes["prototype"] = "reconstructed"
        else:
            modes["prototype"] = bootstrap_prototype_arm(backend, b15, proto_cfg, session_day)
            modes["reversal"] = bootstrap_reversal_arm(backend, b15, scfg, session_day)
            modes["flagship"] = bootstrap_flagship_arm(backend, b15, scfg, session_day)
    return modes