"""
MJ SIR LECTURE UNDERSTANDING — Stage 2 / Stage 3 / 8 EMA Reversal (15m only).

Paper-only four-pack. Does not implement 1H FLAGSHIP or homework lanes.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from analyst.nse_spot_candles_8510 import ema4_ema8

log = logging.getLogger("mj_sir_8510")

SPEC_TAG = "MJ_SIR_LECTURE_UNDERSTANDING"

DEFAULT_MJ_CFG: Dict[str, Any] = {
    "enabled": True,
    "touch_tol_pts": 20.0,
    "bb_width_min_pts": 60.0,
    "ribbon_max_pts": 5.0,
    "ribbon_max_atr_ratio": 0.13,
    "band_slope_tol_pts": 1.8,
    "bb_inward_min_shrink_pts": 1.0,
    "stage_target_pts": 40.0,
    "stage_sl_pts": 40.0,
    "reversal_target_pts": 20.0,
    "reversal_sl_pts": 20.0,
    "reversal_wait_bars_after_cross": 2,
    "reversal_touch_window_bars": 2,
    "reversal_lookback_bars": 8,
    "sr_touch_tol_pts": 25.0,
    "h1_ema_resist_tol_pts": 12.0,
    # Owner rules, 11 Nov 2026: entries are taken on the FIRST touch of the
    # 8 EMA as it moves with the running candle (recomputed every 30s, same
    # as the 8 EMA Prototype), within a few points, and not before 09:30.
    "dynamic_8ema_entry": True,
    "dynamic_touch_tol_pts": 3.0,
    "dynamic_refresh_seconds": 3.0,   # live touch check cadence (owner, 12 Nov)
    "backtest_tick_seconds": 30.0,     # backtest grid inside a 15m bar
    "no_entry_before_hm": "09:30",
    # The 4-8 "attached" (ribbon) block does not apply to the Reversal.
    "reversal_ribbon_block": False,
}


def load_mj_cfg(scanner_cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_MJ_CFG)
    block = scanner_cfg.get("mj_sir_lecture")
    if isinstance(block, dict):
        out.update(block)
    return out


def _bb_width(sma20: pd.Series, std20: pd.Series) -> pd.Series:
    ubb = sma20 + 2 * std20
    lbb = sma20 - 2 * std20
    return ubb - lbb


def _bb_bands(b15: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
    sma20 = b15["close"].rolling(20, min_periods=5).mean()
    std20 = b15["close"].rolling(20, min_periods=5).std().fillna(15.0)
    ubb = sma20 + 2 * std20
    lbb = sma20 - 2 * std20
    return ubb, lbb, _bb_width(sma20, std20)


def _atr14(b15: pd.DataFrame) -> pd.Series:
    h15, l15, c15 = b15["high"], b15["low"], b15["close"]
    tr0 = h15 - l15
    tr1 = (h15 - c15.shift(1)).abs()
    tr2 = (l15 - c15.shift(1)).abs()
    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / 14, adjust=False).mean()


def _idx_abs(bar_ci: int, length: int) -> int:
    return length + bar_ci if bar_ci < 0 else bar_ci


def ribbon_attached(e4: float, e8: float, atr: float, cfg: Dict[str, Any]) -> bool:
    gap = abs(e4 - e8)
    if gap <= float(cfg["ribbon_max_pts"]):
        return True
    if atr > 0 and gap / atr <= float(cfg["ribbon_max_atr_ratio"]):
        return True
    return False


def ubb_up_two_candles(ubb: pd.Series, bar_ci: int, tol: float) -> bool:
    if len(ubb) < abs(bar_ci) + 2:
        return False
    d1 = float(ubb.iloc[bar_ci] - ubb.iloc[bar_ci - 1])
    d0 = float(ubb.iloc[bar_ci - 1] - ubb.iloc[bar_ci - 2])
    return d1 > tol and d0 > tol


def ubb_not_down_recent(ubb: pd.Series, bar_ci: int, tol: float) -> bool:
    """Reject when UBB clearly falling (buyers weak)."""
    if len(ubb) < abs(bar_ci) + 1:
        return True
    d1 = float(ubb.iloc[bar_ci] - ubb.iloc[bar_ci - 1])
    return d1 >= -tol


def lbb_down_two_candles(lbb: pd.Series, bar_ci: int, tol: float) -> bool:
    if len(lbb) < abs(bar_ci) + 2:
        return False
    d1 = float(lbb.iloc[bar_ci] - lbb.iloc[bar_ci - 1])
    d0 = float(lbb.iloc[bar_ci - 1] - lbb.iloc[bar_ci - 2])
    return d1 < -tol and d0 < -tol


def lbb_not_up_recent(lbb: pd.Series, bar_ci: int, tol: float) -> bool:
    if len(lbb) < abs(bar_ci) + 1:
        return True
    d1 = float(lbb.iloc[bar_ci] - lbb.iloc[bar_ci - 1])
    return d1 <= tol


def bb_inward_two(width: pd.Series, bar_ci: int, shrink: float) -> bool:
    if len(width) < abs(bar_ci) + 2:
        return False
    w0 = float(width.iloc[bar_ci])
    w1 = float(width.iloc[bar_ci - 1])
    w2 = float(width.iloc[bar_ci - 2])
    return (w1 - w0) >= shrink and (w2 - w1) >= shrink


def touch_8ema_support(low: float, close: float, e8: float, tol: float) -> bool:
    return low <= e8 + tol and close >= e8 - tol * 0.5


def touch_8ema_resistance(high: float, close: float, e8: float, tol: float) -> bool:
    return high >= e8 - tol and close <= e8 + tol * 0.5


def touch_fixed_level(low: float, high: float, fixed_e8: float, tol: float, long_side: bool) -> bool:
    if long_side:
        return low <= fixed_e8 + tol and low >= fixed_e8 - tol
    return high >= fixed_e8 - tol and high <= fixed_e8 + tol


def _status(
    state: str,
    armed: bool = False,
    block_reason: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = {
        "state": state,
        "armed": armed,
        "block_reason": block_reason,
        "mj_no_trade": block_reason or "",
        "spec": SPEC_TAG,
    }
    if extra:
        row.update(extra)
    return row


def _last_stack_flip(e4: pd.Series, e8: pd.Series, bullish: bool, end_i: int) -> Optional[int]:
    """Last candle at or before end_i on which the 4 EMA moved to this side of the 8 EMA."""
    for i in range(end_i, 0, -1):
        if bullish and e4.iloc[i - 1] <= e8.iloc[i - 1] and e4.iloc[i] > e8.iloc[i]:
            return i
        if not bullish and e4.iloc[i - 1] >= e8.iloc[i - 1] and e4.iloc[i] < e8.iloc[i]:
            return i
    return None


def dynamic_8ema(b15: pd.DataFrame, live_spot: float) -> Tuple[float, float]:
    """(4 EMA, 8 EMA) on the running candle with its close set to live spot."""
    close = b15["close"].astype(float).copy()
    close.iloc[-1] = float(live_spot)
    e4d, e8d = ema4_ema8(close)
    return float(e4d.iloc[-1]), float(e8d.iloc[-1])


def _find_cross_index(
    e4: pd.Series, e8: pd.Series, close: pd.Series, bullish: bool, end_i: int
) -> Optional[int]:
    """Last cross at or before end_i (inclusive)."""
    for i in range(end_i, 1, -1):
        if bullish:
            if e4.iloc[i - 1] <= e8.iloc[i - 1] and e4.iloc[i] > e8.iloc[i] and close.iloc[i] > e8.iloc[i]:
                return i
        else:
            if e4.iloc[i - 1] >= e8.iloc[i - 1] and e4.iloc[i] < e8.iloc[i] and close.iloc[i] < e8.iloc[i]:
                return i
    return None


def evaluate_mj_sir_four_pack(
    b15: pd.DataFrame,
    *,
    bar_ci: int = -2,
    cfg: Dict[str, Any],
    bb_stage2: bool,
    bb_stage3: bool,
    bb_cross: bool,
    party_switch_enabled: bool,
    active_party: str,
    mj_state: Dict[str, Any],
    session_day: str,
    daily_sr: Optional[Dict[str, Any]] = None,
    b60: Optional[pd.DataFrame] = None,
    gate_meta: Optional[Callable[[str], Dict[str, Any]]] = None,
    dynamic: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
  Returns (signals, strategy_status_updates for stage2, stage3, crossover_reversal).

    With cfg["dynamic_8ema_entry"] on, the preset conditions (4/8 stack, BB
    width, band slope) are read on the last closed candle at bar_ci, and the
    touch is read on the running candle b15.iloc[-1] against the 8 EMA
    recomputed with live spot. `dynamic` carries that tick:
    {"live_spot", "tick_high", "tick_low", "now_hm"}. A call without it (the
    closed-candle scanner) reports status only and never fires.
    """
    gate_meta = gate_meta or (lambda _k: {})
    signals: List[Dict[str, Any]] = []
    tol = float(cfg["touch_tol_pts"])
    bb_min = float(cfg["bb_width_min_pts"])
    slope_tol = float(cfg["band_slope_tol_pts"])
    shrink = float(cfg["bb_inward_min_shrink_pts"])

    if session_day and mj_state.get("day") != session_day:
        mj_state.clear()
        mj_state["day"] = session_day

    e4, e8 = ema4_ema8(b15["close"])
    ubb, lbb, bb_width = _bb_bands(b15)
    atr_s = _atr14(b15)

    n = len(b15)
    if n < 11:
        empty = {
            "stage2_long": _status("SCANNING", block_reason="NO TRADE — Insufficient 15m bars"),
            "stage3_short": _status("SCANNING", block_reason="NO TRADE — Insufficient 15m bars"),
            "crossover_reversal": _status("SCANNING", block_reason="NO TRADE — Insufficient 15m bars"),
        }
        return [], empty

    i = _idx_abs(bar_ci, n)
    e4_val = float(e4.iloc[bar_ci])
    e8_val = float(e8.iloc[bar_ci])
    close_15 = float(b15["close"].iloc[bar_ci])
    low_15 = float(b15["low"].iloc[bar_ci])
    high_15 = float(b15["high"].iloc[bar_ci])
    width_val = float(bb_width.iloc[bar_ci]) if pd.notnull(bb_width.iloc[bar_ci]) else 0.0
    atr_val = float(atr_s.iloc[bar_ci]) if pd.notnull(atr_s.iloc[bar_ci]) else 15.0

    status: Dict[str, Dict[str, Any]] = {}

    def common_blocks(ribbon: bool = True) -> Optional[str]:
        if width_val < bb_min:
            return "NO TRADE — BB width < 60 pts"
        if ribbon and ribbon_attached(e4_val, e8_val, atr_val, cfg):
            return "NO TRADE — 4-8 EMA attached (ribbon)"
        return None

    # ── Touch source: running candle vs dynamic 8 EMA, or the closed candle ──
    dyn_on = bool(cfg.get("dynamic_8ema_entry", True))
    dtol = float(cfg.get("dynamic_touch_tol_pts", 3.0))
    no_before = str(cfg.get("no_entry_before_hm", "09:30") or "")
    wait_msg = "NO TRADE — Price did not touch 8 EMA"
    early = False
    e8_dyn = e8_val
    entry_px = close_15
    if dyn_on and dynamic and float(dynamic.get("live_spot") or 0.0) > 0:
        spot = float(dynamic["live_spot"])
        t_hi = float(dynamic.get("tick_high") or spot)
        t_lo = float(dynamic.get("tick_low") or spot)
        _e4_dyn, e8_dyn = dynamic_8ema(b15, spot)
        # Reached the line from the trade's side and has not gone through it.
        touch_long = t_lo <= e8_dyn + dtol and spot >= e8_dyn - dtol
        touch_short = t_hi >= e8_dyn - dtol and spot <= e8_dyn + dtol
        entry_px = spot
        early = bool(no_before) and str(dynamic.get("now_hm") or "") < no_before
        wait_msg = f"NO TRADE — Waiting for first touch of dynamic 8 EMA {e8_dyn:.1f}"
    elif dyn_on:
        touch_long = touch_short = False
        wait_msg = "NO TRADE — Waiting for first touch of dynamic 8 EMA (30s)"
    else:
        touch_long = touch_8ema_support(low_15, close_15, e8_val, tol)
        touch_short = touch_8ema_resistance(high_15, close_15, e8_val, tol)
    early_msg = f"NO TRADE — No entries before {no_before}"

    def _setup_key(tag: str, bullish: bool) -> str:
        if not dyn_on:
            return f"{session_day}:{tag}:{round(e8_val, 1)}"
        # One setup per 4/8 stack: from the candle the 4 EMA moved to this side
        # of the 8 EMA until it moves back. The first touch uses it up.
        flip = _last_stack_flip(e4, e8, bullish, i)
        return f"{session_day}:{tag}:{b15.index[flip] if flip is not None else 'session'}"

    taken_msg = (
        "NO TRADE — First touch already taken for this setup"
        if dyn_on
        else "NO TRADE — Second touch on same 8 EMA setup"
    )

    # ── STAGE 2 ─────────────────────────────────────────────────────────────
    s2_block = common_blocks()
    setup_key_s2 = _setup_key("S2", True)
    if mj_state.get("stage2_fired_key") == setup_key_s2:
        s2_block = s2_block or taken_msg

    if e4_val <= e8_val:
        status["stage2_long"] = _status("BLOCKED_MA_STACK", block_reason="NO TRADE — 4 EMA not above 8 EMA")
    elif s2_block:
        status["stage2_long"] = _status("NO_TRADE", block_reason=s2_block)
    else:
        ubb_block = ""
        if bb_stage2:
            if bb_inward_two(bb_width, bar_ci, shrink):
                ubb_block = "NO TRADE — UBB inward (BB shrinking 2 candles)"
            elif not ubb_up_two_candles(ubb, bar_ci, slope_tol):
                ubb_block = "NO TRADE — UBB not UP for 2 closed candles"
            elif not ubb_not_down_recent(ubb, bar_ci, slope_tol):
                ubb_block = "NO TRADE — UBB downward (buyers weak)"
        if party_switch_enabled and active_party == "BEARISH":
            ubb_block = ubb_block or "NO TRADE — Party-Switch BEARISH blocks CE"
        if early:
            status["stage2_long"] = _status("NO_TRADE", block_reason=ubb_block or early_msg)
        elif not touch_long:
            status["stage2_long"] = _status(
                "ARMED" if not ubb_block else "NO_TRADE",
                armed=not bool(ubb_block),
                block_reason=ubb_block or wait_msg,
            )
        elif ubb_block:
            status["stage2_long"] = _status("NO_TRADE", block_reason=ubb_block)
        else:
            status["stage2_long"] = _status("TRIGGERED", armed=True, block_reason="")
            sig = {
                "strategy_id": "STAGE_2_LONG",
                "setup_name": "MJ Sir Stage 2 8-EMA (Long) — first touch",
                "direction": "LONG",
                "opt_side": "CE",
                "target_pts": float(cfg["stage_target_pts"]),
                "sl_pts": float(cfg["stage_sl_pts"]),
                "index_price": entry_px,
                "conviction": "MJ_SIR",
                "paper_only": True,
                "mj_sir_paper_only": True,
                "dynamic_e8_at_entry": round(e8_dyn, 2),
                "details": (
                    f"{SPEC_TAG} | 4>{e4_val:.1f}>{e8_val:.1f} | "
                    + (f"first touch dynamic 8EMA {e8_dyn:.1f} @ spot {entry_px:.1f}" if dyn_on
                       else f"touch low {low_15:.1f}")
                    + f" | BB width {width_val:.1f}"
                ),
            }
            sig.update(gate_meta("stage2"))
            signals.append(sig)
            mj_state["stage2_fired_key"] = setup_key_s2

    # ── STAGE 3 ─────────────────────────────────────────────────────────────
    s3_block = common_blocks()
    setup_key_s3 = _setup_key("S3", False)
    if mj_state.get("stage3_fired_key") == setup_key_s3:
        s3_block = s3_block or taken_msg

    if e4_val >= e8_val:
        status["stage3_short"] = _status("BLOCKED_MA_STACK", block_reason="NO TRADE — 4 EMA not below 8 EMA")
    elif s3_block:
        status["stage3_short"] = _status("NO_TRADE", block_reason=s3_block)
    else:
        lbb_block = ""
        if bb_stage3:
            if bb_inward_two(bb_width, bar_ci, shrink):
                lbb_block = "NO TRADE — LBB inward (BB shrinking 2 candles)"
            elif not lbb_down_two_candles(lbb, bar_ci, slope_tol):
                lbb_block = "NO TRADE — LBB not DOWN for 2 closed candles"
            elif not lbb_not_up_recent(lbb, bar_ci, slope_tol):
                lbb_block = "NO TRADE — LBB upward (sellers weak)"
        if party_switch_enabled and active_party == "BULLISH":
            lbb_block = lbb_block or "NO TRADE — Party-Switch BULLISH blocks PE"
        if early:
            status["stage3_short"] = _status("NO_TRADE", block_reason=lbb_block or early_msg)
        elif not touch_short:
            status["stage3_short"] = _status(
                "ARMED" if not lbb_block else "NO_TRADE",
                armed=not bool(lbb_block),
                block_reason=lbb_block or wait_msg,
            )
        elif lbb_block:
            status["stage3_short"] = _status("NO_TRADE", block_reason=lbb_block)
        else:
            status["stage3_short"] = _status("TRIGGERED", armed=True, block_reason="")
            sig = {
                "strategy_id": "STAGE_3_SHORT",
                "setup_name": "MJ Sir Stage 3 8-EMA (Short) — first touch",
                "direction": "SHORT",
                "opt_side": "PE",
                "target_pts": float(cfg["stage_target_pts"]),
                "sl_pts": float(cfg["stage_sl_pts"]),
                "index_price": entry_px,
                "conviction": "MJ_SIR",
                "paper_only": True,
                "mj_sir_paper_only": True,
                "dynamic_e8_at_entry": round(e8_dyn, 2),
                "details": (
                    f"{SPEC_TAG} | 4<{e4_val:.1f}<{e8_val:.1f} | "
                    + (f"first touch dynamic 8EMA {e8_dyn:.1f} @ spot {entry_px:.1f}" if dyn_on
                       else f"touch high {high_15:.1f}")
                    + f" | BB width {width_val:.1f}"
                ),
            }
            sig.update(gate_meta("stage3"))
            signals.append(sig)
            mj_state["stage3_fired_key"] = setup_key_s3

    # ── REVERSALS (fixed 8 EMA at cross) ────────────────────────────────────
    wait_bars = int(cfg["reversal_wait_bars_after_cross"])
    touch_win = int(cfg["reversal_touch_window_bars"])
    lookback = int(cfg["reversal_lookback_bars"])

    cross_up = _find_cross_index(e4, e8, b15["close"], True, i)
    cross_dn = _find_cross_index(e4, e8, b15["close"], False, i)

    rev_block = common_blocks(ribbon=bool(cfg.get("reversal_ribbon_block", False)))
    rev_status = _status("SCANNING", block_reason=rev_block or "")
    # Candle on which the touch is looked for: the running one when dynamic.
    t_i = (n - 1) if dyn_on else i

    def eval_reversal(bullish: bool, cross_i: Optional[int]) -> Optional[Dict[str, Any]]:
        if cross_i is None:
            return None
        if i - cross_i > lookback:
            return None
        fixed_e8 = float(e8.iloc[cross_i])
        fixed_e4 = float(e4.iloc[cross_i])
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
        if bb_cross and bullish and not ubb_not_down_recent(ubb, bar_ci, slope_tol):
            return {"pending": False, "reason": "NO TRADE — UBB down (reversal CE)", "fixed_e8": fixed_e8}
        if bb_cross and not bullish and not lbb_not_up_recent(lbb, bar_ci, slope_tol):
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
            f"{session_day}:REV_{'UP' if bullish else 'DN'}:{b15.index[cross_i]}"
            if dyn_on
            else f"{session_day}:REV_{'UP' if bullish else 'DN'}:{round(fixed_e8, 1)}"
        )
        if mj_state.get("reversal_fired_key") == fired_key:
            return {"pending": False, "reason": "NO TRADE — Reversal already fired for this fixed EMA", "fixed_e8": fixed_e8}
        sid = "CROSSOVER_BULLISH" if bullish else "CROSSOVER_BEARISH"
        name = "8 EMA Reversal Upside" if bullish else "8 EMA Reversal Downside"
        mj_state["reversal_fired_key"] = fired_key
        return {
            "signal": {
                "strategy_id": sid,
                "setup_name": f"MJ Sir {name} — " + ("dynamic 8 EMA touch" if dyn_on else "fixed 8 EMA touch"),
                "direction": "LONG" if bullish else "SHORT",
                "opt_side": "CE" if bullish else "PE",
                "target_pts": float(cfg["reversal_target_pts"]),
                "sl_pts": float(cfg["reversal_sl_pts"]),
                "index_price": entry_px,
                "conviction": "MJ_SIR",
                "paper_only": True,
                "mj_sir_paper_only": True,
                "fixed_8ema_entry": fixed_e8,
                "dynamic_e8_at_entry": round(e8_dyn, 2),
                "details": (
                    f"{SPEC_TAG} | cross age {age} | "
                    + (f"dynamic 8EMA {e8_dyn:.1f} @ spot {entry_px:.1f}" if dyn_on else f"fixed 8EMA {fixed_e8:.1f}")
                    + " | "
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
        rev_status = _status("TRIGGERED", armed=True, extra={"reversal_side": "UP"})
    elif dn_eval and dn_eval.get("signal"):
        rev_signals.append(dn_eval["signal"])
        rev_status = _status("TRIGGERED", armed=True, extra={"reversal_side": "DOWN"})
    else:
        for ev in (up_eval, dn_eval):
            if ev and ev.get("reason"):
                reasons.append(str(ev["reason"]))
        if reasons:
            rev_status = _status("NO_TRADE" if "Missed" in reasons[0] else "ARMED", block_reason=reasons[0])
        elif cross_up is None and cross_dn is None:
            rev_status = _status("SCANNING", block_reason="NO TRADE — No recent 4/8 cross")

    status["crossover_reversal"] = rev_status
    signals.extend(rev_signals)

    return signals, status


def backtest_mj_dynamic_at_bar(
    bars: List[Dict[str, Any]],
    idx: int,
    mj_bt_state: Dict[str, Any],
    scanner_cfg: Dict[str, Any],
    *,
    b15_df: Optional[pd.DataFrame],
    day_1m: Optional[pd.DataFrame],
    bb_stage2: bool = False,
    bb_stage3: bool = False,
    bb_cross: bool = False,
    window_bars: int = 160,
    prefilter_pts: float = 15.0,
) -> List[Tuple[str, str, str, float, float, Dict[str, Any]]]:
    """Historical step for Stage 2 / Stage 3 / 8 EMA Reversal.

    It does not re-state the rules. Candle `idx` is treated as the running
    candle and walked on a 30-second grid built from completed 1-minute
    candles; each tick is handed to `evaluate_mj_sir_four_pack`, the same
    function the live worker calls, with the same kind of `dynamic` tick. So
    first touch, the 3-point tolerance, the 09:30 start, BB width, the band
    rule and the Reversal wait/window are whatever live does.

    `mj_bt_state` is the backtest's own memory; live state is never passed in.
    Returns (strategy_id, setup_name, direction, target_pts, sl_pts, meta).
    """
    cfg = load_mj_cfg(scanner_cfg)
    if (
        not bool(cfg.get("enabled", True))
        or not bool(cfg.get("dynamic_8ema_entry", True))
        or b15_df is None
        or b15_df.empty
        or day_1m is None
        or day_1m.empty
        or idx < 21
        or idx >= len(bars)
    ):
        return []
    from analyst.ema8_prototype_8510 import intrabar_tick_timestamps

    b, prev = bars[idx], bars[idx - 1]
    bar_open_ts = int(b.get("ts") or 0)
    if bar_open_ts <= 0:
        return []
    # A touch needs price within a few points of the moving 8 EMA, which sits
    # between the last closed 8 EMA and price. A candle that never comes
    # within prefilter_pts of the closed 8 EMA cannot touch; skip its ticks.
    e8_closed = float(prev.get("e8") or 0.0)
    if e8_closed > 0 and not (
        float(b["low"]) <= e8_closed + prefilter_pts and float(b["high"]) >= e8_closed - prefilter_pts
    ):
        return []

    frame = b15_df.iloc[max(0, idx - window_bars): idx + 1][["open", "high", "low", "close"]].copy()
    day_key = str(b.get("day") or "")
    refresh = float(cfg.get("backtest_tick_seconds", 30.0) or 30.0)   # backtest grid; live cadence is dynamic_refresh_seconds
    out: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
    # A 1-minute candle is stamped with the minute it OPENS (the live desk
    # treats the newest row as still forming). It is known only once that
    # minute is over, so a tick may read a candle only if ts + 60 <= tick.
    m_ts = day_1m["ts"].astype("int64")
    m_end = m_ts + 60
    prev_tick_ts = bar_open_ts
    for tick_ts in intrabar_tick_timestamps(bar_open_ts, refresh):
        known = day_1m[m_end <= tick_ts]
        if known.empty:
            prev_tick_ts = tick_ts
            continue
        spot = float(known.iloc[-1]["close"])
        if spot <= 0:
            prev_tick_ts = tick_ts
            continue
        recent = day_1m[(m_end > prev_tick_ts) & (m_end <= tick_ts) & (m_ts >= bar_open_ts)]
        tick_high = max(float(recent["high"].max()), spot) if not recent.empty else spot
        tick_low = min(float(recent["low"].min()), spot) if not recent.empty else spot
        prev_tick_ts = tick_ts
        t_ist = pd.to_datetime(tick_ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
        sigs, _status_rows = evaluate_mj_sir_four_pack(
            frame,
            bar_ci=-2,
            cfg=cfg,
            bb_stage2=bb_stage2,
            bb_stage3=bb_stage3,
            bb_cross=bb_cross,
            party_switch_enabled=False,
            active_party="NEUTRAL",
            mj_state=mj_bt_state,
            session_day=day_key,
            dynamic={
                "live_spot": spot,
                "tick_high": tick_high,
                "tick_low": tick_low,
                "now_hm": t_ist.strftime("%H:%M"),
            },
        )
        for sig in sigs:
            out.append((
                str(sig["strategy_id"]),
                str(sig["setup_name"]),
                str(sig["direction"]),
                float(sig["target_pts"]),
                float(sig["sl_pts"]),
                {
                    "mj_dynamic": True,
                    "flagship": False,
                    "sr_levels": {},
                    "entry_spot": float(sig["index_price"]),
                    "entry_ts": int(tick_ts),
                    "entry_day": day_key,
                    "entry_hm": t_ist.strftime("%H:%M:%S"),
                    "dynamic_e8_at_entry": sig.get("dynamic_e8_at_entry"),
                    "bollinger_applied": bool(bb_stage2 or bb_stage3 or bb_cross),
                },
            ))
    return out
