"""Recover missed intraday spot exits and free the lane (reconciliation only)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("open_position_exit_sweep_8510")


def _spot_index(backend: Any) -> float:
    return float((getattr(backend, "cached_telemetry", {}) or {}).get("spot", {}).get("price") or 0.0)


def spot_exit_should_close(
    backend: Any,
    p: Dict[str, Any],
    *,
    spot_idx: float,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Mirror live position monitor exit tests without mutating trail state."""
    from analyst.terminal8510_backend import uses_fixed_spot_or_option_exit

    now = now or datetime.now(IST)
    if spot_idx <= 0:
        return False, ""
    sid_pos = str(p.get("strategy_id") or "").upper()
    is_flagship_pos = "FLAGSHIP" in sid_pos
    is_proto_pos = uses_fixed_spot_or_option_exit(p.get("strategy_id"))
    if not is_proto_pos and not is_flagship_pos:
        return False, ""

    if is_proto_pos:
        from analyst.ema8_prototype_8510 import prototype_spot_is_long

        spot_ent = float(p.get("spot_entry") or 0.0)
        spot_tgt = float(p.get("spot_target_level") or 0.0)
        spot_sl = float(p.get("spot_stop_level") or 0.0)
        if spot_ent <= 0 or spot_tgt <= 0 or spot_sl <= 0:
            return False, ""
        is_long = prototype_spot_is_long(
            spot_ent,
            spot_tgt,
            strategy_id=sid_pos,
            symbol=str(p.get("symbol") or ""),
        )
        use_rev_trail = sid_pos.startswith("CROSSOVER") and bool(int(p.get("reversal_candle_trail") or 0))
        if use_rev_trail:
            from analyst.reversal_spot_exit_8510 import parse_reversal_trail_fields, reversal_spot_exit_tick

            trail = parse_reversal_trail_fields(p)
            _, _, hit, hit_reason = reversal_spot_exit_tick(
                is_long=is_long,
                spot_entry=spot_ent,
                spot_idx=spot_idx,
                spot_tgt=spot_tgt,
                spot_sl=spot_sl,
                trail=trail,
                bar=None,
            )
            if hit:
                return True, hit_reason
        elif is_long and spot_idx >= spot_tgt:
            return True, "SPOT_TARGET_HIT"
        elif is_long and spot_idx <= spot_sl:
            return True, "SPOT_SL_HIT"
        elif (not is_long) and spot_idx <= spot_tgt:
            return True, "SPOT_TARGET_HIT"
        elif (not is_long) and spot_idx >= spot_sl:
            return True, "SPOT_SL_HIT"
        proto_eod = str(getattr(backend, "cfg", {}).get("eod_square_off_time", "15:20")).strip()[:5]
        if now.strftime("%H:%M") >= proto_eod:
            return True, f"EOD_SQUARE_OFF_{proto_eod}"

    if is_flagship_pos and spot_idx > 0:
        from analyst.ema8_prototype_8510 import prototype_spot_is_long

        spot_entry = float(p.get("spot_entry") or 0.0)
        spot_tgt = float(p.get("spot_target_level") or 0.0)
        spot_sl = float(p.get("spot_stop_level") or 0.0)
        if spot_entry > 0 and spot_tgt > 0 and spot_sl > 0:
            is_long = prototype_spot_is_long(
                spot_entry,
                spot_tgt,
                strategy_id=sid_pos,
                symbol=str(p.get("symbol") or ""),
            )
            if is_long and spot_idx >= spot_tgt:
                return True, "SPOT_TARGET_HIT"
            if is_long and spot_idx <= spot_sl:
                return True, "SPOT_SL_HIT"
            if (not is_long) and spot_idx <= spot_tgt:
                return True, "SPOT_TARGET_HIT"
            if (not is_long) and spot_idx >= spot_sl:
                return True, "SPOT_SL_HIT"
        eod = str(getattr(backend, "scanner_cfg", {}).get("flagship_eod_square_off", "15:20"))[:5]
        if now.strftime("%H:%M") >= eod:
            return True, f"EOD_SQUARE_OFF_{eod}"
    return False, ""


def _release_lane_after_close(backend: Any, p: Dict[str, Any]) -> None:
    from analyst.ema_lane_entry_gate_8510 import release_lane_reservation
    from analyst.ema_lane_lock_8510 import release_lane_lock

    sid = str(p.get("strategy_id") or "").upper()
    lock = getattr(backend, "_ema_lane_lock", None) or {}
    if sid.startswith("EMA8_PROTOTYPE") or sid.startswith("CROSSOVER"):
        release_lane_reservation(lock, sid)
    else:
        release_lane_lock(lock)
    backend._ema_lane_lock = lock
    if hasattr(backend, "_save_ema_lane_lock_state"):
        backend._save_ema_lane_lock_state()
    st = getattr(backend, "_ema8_prototype_state", None) or {}
    if sid.startswith("EMA8_PROTOTYPE"):
        st.pop("open", None)
        backend._ema8_prototype_state = st
        if hasattr(backend, "_save_ema8_prototype_state"):
            backend._save_ema8_prototype_state()


def sweep_open_positions(
    backend: Any,
    *,
    now: Optional[datetime] = None,
    enforce_market_hours: bool = False,
) -> List[Dict[str, Any]]:
    """Close OPEN rows whose spot exit is already satisfied (feed-gap recovery)."""
    now = now or datetime.now(IST)
    spot_idx = _spot_index(backend)
    closed: List[Dict[str, Any]] = []
    for p in backend.get_active_positions():
        hit, base_reason = spot_exit_should_close(backend, p, spot_idx=spot_idx, now=now)
        if not hit:
            continue
        pid = str(p.get("position_id") or "")
        if not pid:
            continue
        hm = now.strftime("%H:%M")
        reason = f"EXIT RECOVERED (missed at {hm}, feed gap) — {base_reason}"
        res = backend.modify_open_position(
            pid,
            "CLOSE",
            enforce_market_hours=enforce_market_hours,
            reason=reason,
        )
        if res.get("ok"):
            _release_lane_after_close(backend, p)
            log.warning("Recovered exit for %s: %s", pid, reason)
            closed.append({"position_id": pid, "reason": reason, **res})
        else:
            log.warning("Exit sweep could not close %s: %s", pid, res.get("error") or res)
    return closed


def run_startup_exit_sweep(backend: Any) -> List[Dict[str, Any]]:
    return sweep_open_positions(backend, enforce_market_hours=False)