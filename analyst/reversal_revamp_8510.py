"""8 EMA Reversal revamp — 5m/15m, separation, pre-arm, OI (reuse Prototype patterns)."""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from analyst.mj_sir_stage_reversal_8510 import (
    SPEC_TAG,
    _find_cross_index,
    _idx_abs,
    _status,
    ema4_ema8,
    touch_fixed_level,
    ubb_not_down_recent,
    lbb_not_up_recent,
)
from analyst.nse_spot_candles_8510 import ema4_ema8 as _ema_shared

DEFAULT_REVERSAL_CFG: Dict[str, Any] = {
    "enabled": True,
    "timeframe_mode": "15m",
    "pre_arm_15m_from_5m": False,
    "ema_separation_enabled": False,
    "ema_separation_min_pts": 5.0,
    "candle_trail_enabled": False,
    "candle_trail_activate_pts": 20.0,
    "execution_mode": "PAPER_ONLY",
    "oi_filters": {
        "enabled": False,
        "log_only": True,
        "mode": "log_only",
        "rule": "all_agree",
        "neutral_blocks": False,
        "all_agree": False,
        "momentum_window": 10,
        "momentum_sig_bull": 0.15,
        "momentum_sig_bear": 0.30,
        "require_price_confirm": False,
        "switches": {
            "writer_flow": False,
            "total_oi": False,
            "fresh_oi": False,
            "price_oi": False,
            "volume_oi": False,
        },
    },
}


def reversal_oi_filters_for_gate(scanner_cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Merge Reversal OI toggles onto Prototype filter defaults for oi_switch_block."""
    rev = load_reversal_cfg(scanner_cfg)
    raw = rev.get("oi_filters") or {}
    if not raw.get("enabled"):
        return None
    from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg
    from analyst.proto_oi_filters_8510 import load_oi_filters_cfg

    base = load_oi_filters_cfg(load_ema8_prototype_cfg(scanner_cfg or {}))
    mode = str(raw.get("mode") or ("log_only" if raw.get("log_only", True) else "block")).lower()
    base["mode"] = mode if mode in ("log_only", "block") else "log_only"
    if raw.get("rule"):
        base["rule"] = raw["rule"]
    if "neutral_blocks" in raw:
        base["neutral_blocks"] = bool(raw.get("neutral_blocks"))
    sw = dict(base.get("switches") or {})
    for k, v in (raw.get("switches") or {}).items():
        sw[k] = bool(v)
    base["switches"] = sw
    mom = dict(base.get("momentum") or {})
    mom["window_min"] = int(raw.get("momentum_window", mom.get("window_min", 10)))
    mom["sig_min"] = float(raw.get("momentum_sig_bull", raw.get("sig_min", mom.get("sig_min", 0.15))))
    mom["sig_extreme"] = float(raw.get("momentum_sig_bear", raw.get("sig_extreme", mom.get("sig_extreme", 0.30))))
    if "require_price_confirm" in raw:
        mom["require_price_confirm"] = bool(raw.get("require_price_confirm"))
    base["momentum"] = mom
    return base


def load_reversal_cfg(scanner_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = copy.deepcopy(DEFAULT_REVERSAL_CFG)
    if not isinstance(scanner_cfg, dict):
        return out
    rev = scanner_cfg.get("reversal")
    if isinstance(rev, dict):
        out.update({k: v for k, v in rev.items() if k != "oi_filters"})
        oi = rev.get("oi_filters")
        if isinstance(oi, dict):
            out["oi_filters"].update(oi)
    return out


def needs_reversal_revamp(scanner_cfg: Optional[Dict[str, Any]]) -> bool:
    rc = load_reversal_cfg(scanner_cfg)
    if str(rc.get("timeframe_mode", "15m")).lower() != "15m":
        return True
    if bool(rc.get("pre_arm_15m_from_5m")):
        return True
    if bool(rc.get("ema_separation_enabled")):
        return True
    if bool(rc.get("candle_trail_enabled")):
        return True
    if str(rc.get("execution_mode", "PAPER_ONLY")).upper() in ("LIVE_ALLOWED", "LIVE", "REAL"):
        return True
    return False


def _execution_labels(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    mode = str(cfg.get("execution_mode", "PAPER_ONLY")).upper()
    paper_only = mode not in ("LIVE_ALLOWED", "LIVE", "REAL")
    return paper_only, "PAPER_ONLY" if paper_only else "LIVE_ALLOWED"


def ema_separation_ok(e4: float, e8: float, min_pts: float) -> bool:
    return abs(float(e4) - float(e8)) >= float(min_pts)


def _eval_on_frame(
    b_df: pd.DataFrame,
    *,
    timeframe: str,
    cfg: Dict[str, Any],
    rev_cfg: Dict[str, Any],
    bar_ci: int,
    bb_cross: bool,
    party_switch_enabled: bool,
    active_party: str,
    mj_state: Dict[str, Any],
    session_day: str,
    dyn_on: bool,
    dynamic: Optional[Dict[str, Any]],
    touch_long: bool,
    touch_short: bool,
    entry_px: float,
    e8_dyn: float,
    early: bool,
    early_msg: str,
    wait_msg: str,
    rev_block: Optional[str],
    ubb,
    lbb,
    bar_ci_15: int,
    slope_tol: float,
    tol: float,
    low_15: float,
    high_15: float,
    gate_meta: Callable[[str], Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    e4, e8 = ema4_ema8(b_df["close"])
    n = len(b_df)
    i = _idx_abs(bar_ci, n)
    wait_bars = int(cfg["reversal_wait_bars_after_cross"])
    touch_win = int(cfg["reversal_touch_window_bars"])
    lookback = int(cfg["reversal_lookback_bars"])
    cross_up = _find_cross_index(e4, e8, b_df["close"], True, i)
    cross_dn = _find_cross_index(e4, e8, b_df["close"], False, i)
    # A newer opposite cross supersedes the previous setup.
    if cross_up is not None and cross_dn is not None:
        if cross_up > cross_dn:
            cross_dn = None
        else:
            cross_up = None
    t_i = (n - 1) if dyn_on else i
    rev_status = _status("SCANNING", block_reason=rev_block or "", extra={"timeframe": timeframe})
    sep_on = bool(rev_cfg.get("ema_separation_enabled"))
    sep_min = float(rev_cfg.get("ema_separation_min_pts", 5.0))
    paper_only, exec_label = _execution_labels(rev_cfg)

    if timeframe == "5m" and cross_up is not None and bool(rev_cfg.get("pre_arm_15m_from_5m")):
        mj_state["reversal_pre_arm_15m"] = {
            "side": "UP",
            "cross_index": str(b_df.index[cross_up]),
            "timeframe": "5m",
        }
    if timeframe == "5m" and cross_dn is not None and bool(rev_cfg.get("pre_arm_15m_from_5m")):
        mj_state["reversal_pre_arm_15m"] = {
            "side": "DOWN",
            "cross_index": str(b_df.index[cross_dn]),
            "timeframe": "5m",
        }

    def eval_reversal(bullish: bool, cross_i: Optional[int]) -> Optional[Dict[str, Any]]:
        if cross_i is None:
            return None
        if i - cross_i > lookback:
            return None
        fixed_e8 = float(e8.iloc[cross_i])
        fixed_e4 = float(e4.iloc[cross_i])
        if sep_on and not ema_separation_ok(fixed_e4, fixed_e8, sep_min):
            return {"pending": False, "reason": f"NO TRADE — 4/8 separation < {sep_min} pts", "fixed_e8": fixed_e8}
        if bullish and fixed_e4 <= fixed_e8:
            return None
        if not bullish and fixed_e4 >= fixed_e8:
            return None
        age = t_i - cross_i
        if age < wait_bars:
            return {
                "pending": True,
                "reason": f"NO TRADE — Wait {wait_bars - age} more bar(s) after cross",
                "fixed_e8": fixed_e8,
            }
        win_start = cross_i + wait_bars
        win_end = cross_i + wait_bars + touch_win - 1
        if t_i < win_start:
            return {"pending": True, "reason": "NO TRADE — Before reversal touch window", "fixed_e8": fixed_e8}
        if t_i > win_end:
            return {"pending": False, "reason": "NO TRADE — Missed 8 EMA touch window", "fixed_e8": fixed_e8}
        if early:
            return {"pending": True, "reason": early_msg, "fixed_e8": fixed_e8}
        if rev_block:
            return {"pending": False, "reason": rev_block, "fixed_e8": fixed_e8}
        if bb_cross and bullish and not ubb_not_down_recent(ubb, bar_ci_15, slope_tol):
            return {"pending": False, "reason": "NO TRADE — UBB down (reversal CE)", "fixed_e8": fixed_e8}
        if bb_cross and not bullish and not lbb_not_up_recent(lbb, bar_ci_15, slope_tol):
            return {"pending": False, "reason": "NO TRADE — LBB up (reversal PE)", "fixed_e8": fixed_e8}
        if party_switch_enabled and bullish and active_party == "BEARISH":
            return {"pending": False, "reason": "NO TRADE — Party-Switch BEARISH", "fixed_e8": fixed_e8}
        if party_switch_enabled and not bullish and active_party == "BULLISH":
            return {"pending": False, "reason": "NO TRADE — Party-Switch BULLISH", "fixed_e8": fixed_e8}
        if dyn_on:
            touched = touch_long if bullish else touch_short
            miss = wait_msg
        else:
            touched = touch_fixed_level(low_15, high_15, fixed_e8, tol, bullish)
            miss = f"NO TRADE — Price did not touch fixed 8 EMA {fixed_e8:.1f}"
        if not touched:
            return {"pending": True, "reason": miss, "fixed_e8": fixed_e8}
        fired_key = (
            f"{session_day}:REV_{timeframe}_{'UP' if bullish else 'DN'}:{b_df.index[cross_i]}"
            if dyn_on
            else f"{session_day}:REV_{timeframe}_{'UP' if bullish else 'DN'}:{round(fixed_e8, 1)}"
        )
        if mj_state.get("reversal_fired_key") == fired_key:
            return {"pending": False, "reason": "NO TRADE — Reversal already fired for this setup", "fixed_e8": fixed_e8}
        sid = "CROSSOVER_BULLISH" if bullish else "CROSSOVER_BEARISH"
        name = "8 EMA Reversal Upside" if bullish else "8 EMA Reversal Downside"
        mj_state["reversal_fired_key"] = fired_key
        return {
            "signal": {
                "strategy_id": sid,
                "setup_name": f"MJ Sir {name} ({timeframe}) — dynamic 8 EMA touch",
                "direction": "LONG" if bullish else "SHORT",
                "opt_side": "CE" if bullish else "PE",
                "target_pts": float(cfg["reversal_target_pts"]),
                "sl_pts": float(cfg["reversal_sl_pts"]),
                "index_price": entry_px,
                "conviction": "MJ_SIR",
                "paper_only": paper_only,
                "mj_sir_paper_only": paper_only,
                "reversal_timeframe": timeframe,
                "reversal_candle_trail": bool(rev_cfg.get("candle_trail_enabled")),
                "execution_mode": exec_label,
                "fixed_8ema_entry": fixed_e8,
                "dynamic_e8_at_entry": round(e8_dyn, 2),
                "details": (
                    f"{SPEC_TAG} | {timeframe} | cross age {age} | "
                    f"dynamic 8EMA {e8_dyn:.1f} @ spot {entry_px:.1f} | "
                    f"gap 4{'>' if bullish else '<'}8 at cross"
                ),
                **gate_meta("crossover"),
            }
        }

    up_eval = eval_reversal(True, cross_up)
    dn_eval = eval_reversal(False, cross_dn)
    rev_signals: List[Dict[str, Any]] = []
    reasons: List[str] = []
    if up_eval and up_eval.get("signal"):
        rev_signals.append(up_eval["signal"])
        rev_status = _status("TRIGGERED", armed=True, extra={"reversal_side": "UP", "timeframe": timeframe})
    elif dn_eval and dn_eval.get("signal"):
        rev_signals.append(dn_eval["signal"])
        rev_status = _status("TRIGGERED", armed=True, extra={"reversal_side": "DOWN", "timeframe": timeframe})
    else:
        for ev in (up_eval, dn_eval):
            if ev and ev.get("reason"):
                reasons.append(str(ev["reason"]))
        if reasons:
            rev_status = _status("NO_TRADE" if "Missed" in reasons[0] else "ARMED", block_reason=reasons[0], extra={"timeframe": timeframe, "reversal_side": "UP" if up_eval else "DOWN"})
        elif cross_up is None and cross_dn is None:
            rev_status = _status("SCANNING", block_reason="NO TRADE — No recent 4/8 cross", extra={"timeframe": timeframe})
    pre = mj_state.get("reversal_pre_arm_15m")
    if isinstance(pre, dict) and timeframe == "15m" and rev_status.get("state") in ("ARMED", "NO_TRADE"):
        rev_status["pre_arm_note"] = "15m armed early via 5m lead"
        rev_status["pre_arm_side"] = pre.get("side")
    return rev_signals, rev_status


def run_reversal_revamp(
    *,
    scanner_cfg: Optional[Dict[str, Any]],
    b15: pd.DataFrame,
    b5: Optional[pd.DataFrame],
    cfg: Dict[str, Any],
    bar_ci: int,
    bb_cross: bool,
    party_switch_enabled: bool,
    active_party: str,
    mj_state: Dict[str, Any],
    session_day: str,
    dyn_on: bool,
    dynamic: Optional[Dict[str, Any]],
    touch_long: bool,
    touch_short: bool,
    entry_px: float,
    e8_dyn: float,
    early: bool,
    early_msg: str,
    wait_msg: str,
    rev_block: Optional[str],
    ubb,
    lbb,
    slope_tol: float,
    tol: float,
    low_15: float,
    high_15: float,
    gate_meta: Callable[[str], Dict[str, Any]],
    legacy_runner: Callable[[], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not bool(load_reversal_cfg(scanner_cfg).get("enabled", True)):
        return [], _status("OFF", block_reason="Reversal manually disabled", armed=False)
    if not needs_reversal_revamp(scanner_cfg):
        return legacy_runner()
    rev_cfg = load_reversal_cfg(scanner_cfg)
    mode = str(rev_cfg.get("timeframe_mode", "15m")).lower()
    shared = dict(
        cfg=cfg,
        rev_cfg=rev_cfg,
        bb_cross=bb_cross,
        party_switch_enabled=party_switch_enabled,
        active_party=active_party,
        mj_state=mj_state,
        session_day=session_day,
        dyn_on=dyn_on,
        dynamic=dynamic,
        touch_long=touch_long,
        touch_short=touch_short,
        entry_px=entry_px,
        e8_dyn=e8_dyn,
        early=early,
        early_msg=early_msg,
        wait_msg=wait_msg,
        rev_block=rev_block,
        ubb=ubb,
        lbb=lbb,
        bar_ci_15=bar_ci,
        slope_tol=slope_tol,
        tol=tol,
        low_15=low_15,
        high_15=high_15,
        gate_meta=gate_meta,
    )
    lanes: List[Tuple[str, pd.DataFrame, int]] = []
    if mode in ("15m", "both"):
        lanes.append(("15m", b15, bar_ci))
    if mode in ("5m", "both") and b5 is not None and not b5.empty and len(b5) >= 11:
        lanes.append(("5m", b5, -2))
    if not lanes:
        return legacy_runner()
    all_sigs: List[Dict[str, Any]] = []
    merged_status: Dict[str, Any] = _status("SCANNING", extra={"timeframe_mode": mode})
    for tf, frame, ci in lanes:
        sigs, st = _eval_on_frame(frame, timeframe=tf, **shared, bar_ci=ci)
        if sigs:
            all_sigs.extend(sigs[:1])
            merged_status = st
            break
        if st.get("state") == "ARMED" or (merged_status.get("state") == "SCANNING" and st.get("block_reason")):
            merged_status = st
    if not all_sigs and mode == "both":
        merged_status["note"] = "one trade at a time — first lane to touch fires"
    return all_sigs, merged_status


def dynamic_8ema_on_frame(b_df: pd.DataFrame, live_spot: float) -> Tuple[float, float]:
    """Same forming-bar dynamic EMA as 15m Prototype on any OHLC frame."""
    close = b_df["close"].astype(float).copy()
    close.iloc[-1] = float(live_spot)
    e4d, e8d = _ema_shared(close)
    return float(e4d.iloc[-1]), float(e8d.iloc[-1])