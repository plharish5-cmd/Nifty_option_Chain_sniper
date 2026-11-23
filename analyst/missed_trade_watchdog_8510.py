"""Live Missed-Trade Watchdog — observe + alert only; never places orders."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from analyst.missed_trades_log_8510 import append_miss_event, try_telegram_alert

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("missed_trade_watchdog_8510")


def load_watchdog_cfg(scanner_cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = dict(scanner_cfg.get("miss_watchdog") or {})
    defaults = {
        "enabled": True,
        "qualification_timeout_sec": 12.0,
        "armed_idle_heartbeat_min": 10.0,
        "quote_retry_max": 5,
    }
    defaults.update({k: block[k] for k in block if k in defaults or k.startswith("armed") or k.startswith("qual")})
    if "enabled" not in block:
        defaults["enabled"] = bool(scanner_cfg.get("miss_watchdog_enabled", True))
    if "armed_idle_heartbeat_min" in scanner_cfg:
        defaults["armed_idle_heartbeat_min"] = float(scanner_cfg["armed_idle_heartbeat_min"])
    return defaults


def _bar_dedupe_key(backend: Any, setup: str, side: str) -> str:
    from analyst.nse_spot_candles_8510 import current_nse_15m_open_ts

    return f"{datetime.now(IST).strftime('%Y-%m-%d')}|{setup}|{side}|{current_nse_15m_open_ts()}"


def _legitimate_block_reason(backend: Any, *, setup: str, direction: str) -> Optional[str]:
    """Return reason string if a miss alarm must stay silent."""
    n_open = len(backend.get_active_positions())
    if n_open > 0:
        return "lane held by open position"
    hm = datetime.now(IST).strftime("%H:%M")
    if hm < "09:30" or hm >= "15:20":
        return "outside 09:30–15:20 entry window"
    st = getattr(backend, "_ema8_prototype_state", None) or {}
    if setup == "prototype":
        sl_side = str(st.get("sl_blocked_side") or "").upper()
        if sl_side and sl_side == str(direction).upper():
            return "SL-blocked side"
    sid = "EMA8_PROTOTYPE_LONG" if setup == "prototype" else ""
    if sid and not backend.algo_strategy_execution_allowed(sid):
        return "not in algo allowlist"
    cd = getattr(backend, "strategy_cooldowns", {}).get(sid, 0.0)
    cool = float(getattr(backend, "scanner_cfg", {}).get("signal_cooldown_5m_min", 0) or 0) * 60
    if cool and (time.time() - cd) < cool:
        return "active cooldown"
    return None


def _mechanical_cause(backend: Any, setup: str) -> str:
    dec = (getattr(backend, "_ema_entry_decisions", None) or {}).get(
        "ema8_prototype" if setup == "prototype" else "crossover_reversal"
    )
    if isinstance(dec, dict):
        stage = str(dec.get("stage") or "")
        reason = str(dec.get("reason") or "")
        if "Contract selection" in reason or "NO_ENABLED_CONTRACT" in reason:
            return "no option quote / strike selection failed"
        if "ORDER_REJECTED" in stage or "DH-" in reason:
            return f"broker rejected: {reason[:120]}"
        if "stale" in reason.lower():
            return "stale spot at touch"
        if reason:
            return reason[:160]
    if getattr(backend, "_proto_ema_audit", {}).get("stale"):
        return "stale spot at touch"
    return "arm present but entry path not reached"


def note_signals_submitted(backend: Any, signals: List[Dict[str, Any]]) -> None:
    """Start qualification timers when live signals enter the order path."""
    wd = backend.__dict__.setdefault("_miss_watchdog", {"pending": {}, "alerted": set(), "idle_last": {}})
    cfg = load_watchdog_cfg(getattr(backend, "scanner_cfg", {}) or {})
    if not cfg.get("enabled"):
        return
    for sig in signals or []:
        sid = str(sig.get("strategy_id") or "").upper()
        setup = (
            "prototype"
            if sid.startswith("EMA8_PROTOTYPE")
            else "reversal"
            if sid.startswith("CROSSOVER") or sid.startswith("STAGE_")
            else ""
        )
        if not setup:
            continue
        side = "CE" if str(sig.get("direction")).upper() == "LONG" else "PE"
        wd["pending"][_bar_dedupe_key(backend, setup, side)] = {
            "t0": time.time(),
            "setup": setup,
            "side": side,
            "hm": datetime.now(IST).strftime("%H:%M"),
        }


def run_watchdog_timeouts(backend: Any) -> None:
    cfg = load_watchdog_cfg(getattr(backend, "scanner_cfg", {}) or {})
    if not cfg.get("enabled"):
        return
    wd = backend.__dict__.setdefault("_miss_watchdog", {"pending": {}, "alerted": set(), "idle_last": {}})
    timeout = float(cfg.get("qualification_timeout_sec", 12))
    now = time.time()
    for key, meta in list(wd.get("pending", {}).items()):
        if now - float(meta.get("t0", now)) < timeout:
            continue
        setup = str(meta.get("setup"))
        side = str(meta.get("side"))
        direction = "LONG" if side == "CE" else "SHORT"
        legit = _legitimate_block_reason(backend, setup=setup, direction=direction)
        if legit:
            wd["pending"].pop(key, None)
            continue
        if key in wd["alerted"]:
            wd["pending"].pop(key, None)
            continue
        cause = _mechanical_cause(backend, setup)
        hm = str(meta.get("hm") or datetime.now(IST).strftime("%H:%M"))
        banner = f"⚠ MISS: {setup.title()} {side} qualified {hm}, no order — {cause}"
        append_miss_event(
            event="WOULD_TAKE_UNFILLED",
            setup=setup,
            side=side,
            detail=cause,
            bar_key=key,
        )
        try_telegram_alert(banner)
        log.warning(banner)
        backend.__dict__.setdefault("_miss_watchdog_banner", []).append(banner)
        wd["alerted"].add(key)
        wd["pending"].pop(key, None)
    _armed_idle_heartbeat(backend, cfg)


def _armed_idle_heartbeat(backend: Any, cfg: Dict[str, Any]) -> None:
    arm = (getattr(backend, "_ema8_prototype_state", None) or {}).get("arm")
    if not arm:
        return
    away = bool(arm.get("away_seen"))
    if away:
        return
    idle_min = float(cfg.get("armed_idle_heartbeat_min", 10))
    wd = backend.__dict__.setdefault("_miss_watchdog", {})
    cross_ts = int(arm.get("cross_bar_open_ts") or 0)
    if cross_ts <= 0:
        return
    age_min = (time.time() - cross_ts) / 60.0
    if age_min < idle_min:
        return
    last = float(wd.get("idle_last", {}).get("prototype") or 0)
    if time.time() - last < idle_min * 60:
        return
    msg = "ARMED_IDLE (Prototype still waiting for away/touch)"
    append_miss_event(event="ARMED_IDLE", setup="prototype", side="", detail=msg)
    log.info(msg)
    wd.setdefault("idle_last", {})["prototype"] = time.time()


def on_proto_evaluation(
    backend: Any,
    sigs: List[Dict[str, Any]],
    st: Dict[str, Any],
) -> None:
    """Track qualified touches that did not emit orders this tick."""
    cfg = load_watchdog_cfg(getattr(backend, "scanner_cfg", {}) or {})
    if not cfg.get("enabled"):
        return
    if sigs:
        note_signals_submitted(backend, sigs)
        return
    actions = st.get("actions") or []
    retouch = any(str(a.get("type")).upper() == "RETOUCH_SEEN" for a in actions)
    if retouch and not sigs:
        direction = str((getattr(backend, "_ema8_prototype_state", {}).get("arm") or {}).get("direction") or "")
        side = "CE" if direction.upper() == "LONG" else "PE"
        key = _bar_dedupe_key(backend, "prototype", side)
        wd = backend.__dict__.setdefault("_miss_watchdog", {"pending": {}, "alerted": set(), "idle_last": {}})
        wd["pending"][key] = {
            "t0": time.time(),
            "setup": "prototype",
            "side": side,
            "hm": datetime.now(IST).strftime("%H:%M"),
        }


def run_desk_self_audit_startup(backend: Any) -> Dict[str, Any]:
    """Part A: lane reconcile + exit sweep once after reconstruct."""
    from analyst.ema_lane_reconcile_8510 import reconcile_lane_lock_on_startup
    from analyst.open_position_exit_sweep_8510 import run_startup_exit_sweep
    from analyst.prior_session_position_reconcile_8510 import reconcile_prior_session_paper_positions

    prior = reconcile_prior_session_paper_positions(backend)
    max_min = float(getattr(backend, "scanner_cfg", {}).get("lane_reservation_max_min", 15) or 15)
    released = reconcile_lane_lock_on_startup(backend, max_reservation_min=max_min)
    closed = run_startup_exit_sweep(backend)
    return {
        "prior_session_closed": len(prior),
        "prior_session": prior,
        "lane_released": released,
        "exits_recovered": len(closed),
        "closed": closed,
    }