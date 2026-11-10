"""
8 EMA Prototype — standalone lane (NOT Stage 2/3, NOT crossover, NOT FLAGSHIP).

15m Nifty spot · close cross → arm → re-touch dynamic 8 EMA (30s refresh) → entry on touch.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from analyst.mj_sir_stage_reversal_8510 import _idx_abs
from analyst.nse_spot_candles_8510 import ema4_ema8

log = logging.getLogger("ema8_prototype_8510")

SPEC_TAG = "EMA8_PROTOTYPE"
SID_LONG = "EMA8_PROTOTYPE_LONG"
SID_SHORT = "EMA8_PROTOTYPE_SHORT"

DEFAULT_CFG: Dict[str, Any] = {
    "enabled": True,
    "execution_mode": "LIVE_ALLOWED",
    "touch_tol_pts": 5.0,
    "target_pts": 40.0,
    "sl_pts": 40.0,
    "e8_refresh_seconds": 30.0,
    "min_option_premium_inr": 200.0,
    "ribbon_filter_enabled": False,
    "ribbon_max_pts": 3.0,
    "ema_separation_enabled": False,
    "supertrend_enabled": False,
    "supertrend_length": 5,
    "supertrend_factor": 1.0,
    "arm_until_opposite_close": True,
}


def load_ema8_prototype_cfg(scanner_cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_CFG)
    block = scanner_cfg.get("ema8_prototype")
    if isinstance(block, dict):
        out.update(block)
    return out


def _execution_mode_labels(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    mode = str(cfg.get("execution_mode", "LIVE_ALLOWED")).upper()
    paper_only = mode not in ("LIVE_ALLOWED", "LIVE", "REAL")
    label = "PAPER_ONLY" if paper_only else "LIVE_ALLOWED"
    return paper_only, label


def apply_forming_bar_to_b15(
    b15: pd.DataFrame,
    *,
    bar_ci: int,
    live_spot: float,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> pd.DataFrame:
    """Copy of b15 with forming bar OHLC patched (30s live spot / replay ticks)."""
    work = b15.copy()
    n = len(work)
    i = _idx_abs(bar_ci, n)
    work.iat[i, work.columns.get_loc("close")] = float(live_spot)
    if form_high is not None:
        work.iat[i, work.columns.get_loc("high")] = max(float(form_high), float(live_spot))
    if form_low is not None:
        work.iat[i, work.columns.get_loc("low")] = min(float(form_low), float(live_spot))
    return work


def compute_dynamic_emas_on_bar(
    b15: pd.DataFrame,
    *,
    bar_ci: int,
    live_spot: float,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> Tuple[float, float, float, float]:
    """EMA(4/8) with forming bar close = live spot (Dhan/TV style, adjust=False)."""
    work = apply_forming_bar_to_b15(
        b15, bar_ci=bar_ci, live_spot=live_spot, form_high=form_high, form_low=form_low
    )
    n = len(work)
    i = _idx_abs(bar_ci, n)
    e4, e8 = ema4_ema8(work["close"])
    return (
        float(e4.iloc[i]),
        float(e8.iloc[i]),
        float(work["high"].iloc[i]),
        float(work["low"].iloc[i]),
    )


def forming_ohlc_from_1m(
    day_1m: pd.DataFrame,
    *,
    bar_open_ts: int,
    tick_ts: int,
) -> Tuple[float, float, float]:
    """High/low/close of the forming 15m bar from 1m candles up to tick_ts."""
    if day_1m is None or day_1m.empty:
        return 0.0, 0.0, 0.0
    w = day_1m[(day_1m["ts"] >= int(bar_open_ts)) & (day_1m["ts"] <= int(tick_ts))]
    if w.empty:
        prior = day_1m[day_1m["ts"] <= int(tick_ts)]
        if prior.empty:
            return 0.0, 0.0, 0.0
        row = prior.iloc[-1]
        c = float(row["close"])
        return c, c, c
    hi = float(w["high"].max())
    lo = float(w["low"].min())
    spot = float(w.iloc[-1]["close"])
    return hi, lo, spot


def intrabar_tick_timestamps(bar_open_ts: int, refresh_seconds: float = 30.0) -> List[int]:
    """30s grid inside a 15m NSE bar (bar_open_ts … bar_open+900)."""
    step = max(5, int(refresh_seconds))
    end = int(bar_open_ts) + 900
    ticks: List[int] = []
    t = int(bar_open_ts) + step
    while t <= end:
        ticks.append(t)
        t += step
    return ticks


def supertrend_last(
    b15: pd.DataFrame,
    *,
    length: int = 5,
    factor: float = 1.0,
    bar_ci: int = -1,
) -> Tuple[float, str]:
    """SuperTrend (ATR length, factor) — returns (line, direction UP/DOWN)."""
    n = len(b15)
    if n < length + 2:
        return 0.0, "NEUTRAL"
    i = _idx_abs(bar_ci, n)
    high = b15["high"].astype(float)
    low = b15["low"].astype(float)
    close = b15["close"].astype(float)
    tr = pd.concat(
        [(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / max(1, length), adjust=False).mean()
    hl2 = (high + low) / 2.0
    upper = hl2 + factor * atr
    lower = hl2 - factor * atr
    st = np.zeros(n)
    direction = np.ones(n)
    for j in range(1, n):
        if close.iloc[j] > upper.iloc[j - 1]:
            direction[j] = 1
        elif close.iloc[j] < lower.iloc[j - 1]:
            direction[j] = -1
        else:
            direction[j] = direction[j - 1]
        if direction[j] == 1:
            st[j] = max(lower.iloc[j], st[j - 1] if j > 0 else lower.iloc[j])
        else:
            st[j] = min(upper.iloc[j], st[j - 1] if j > 0 else upper.iloc[j])
    di = "UP" if direction[i] >= 0 else "DOWN"
    return float(st[i]), di


def _ribbon_blocks(e4: float, e8: float, cfg: Dict[str, Any]) -> bool:
    if not bool(cfg.get("ribbon_filter_enabled", False)):
        return False
    max_pts = float(cfg.get("ribbon_max_pts", 3.0))
    return abs(e4 - e8) <= max_pts


def _separation_blocks(bullish: bool, e4: float, e8: float, cfg: Dict[str, Any]) -> bool:
    if not bool(cfg.get("ema_separation_enabled", False)):
        return False
    if bullish:
        return e4 <= e8
    return e4 >= e8


def _supertrend_blocks(
    bullish: bool,
    b15: pd.DataFrame,
    cfg: Dict[str, Any],
    bar_ci: int,
    *,
    live_spot: Optional[float] = None,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> bool:
    if not bool(cfg.get("supertrend_enabled", False)):
        return False
    length = int(cfg.get("supertrend_length", 5))
    factor = float(cfg.get("supertrend_factor", 1.0))
    frame = (
        apply_forming_bar_to_b15(
            b15, bar_ci=bar_ci, live_spot=float(live_spot), form_high=form_high, form_low=form_low
        )
        if live_spot is not None and live_spot > 0
        else b15
    )
    _line, di = supertrend_last(frame, length=length, factor=factor, bar_ci=bar_ci)
    if bullish:
        return di != "UP"
    return di != "DOWN"


def _status(
    state: str,
    *,
    phase: str = "",
    armed: bool = False,
    block_reason: str = "",
    direction: str = "",
    extra: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    paper_only, exec_label = _execution_mode_labels(cfg or DEFAULT_CFG)
    row = {
        "name": "8 EMA Prototype",
        "state": state,
        "phase": phase,
        "armed": armed,
        "block_reason": block_reason,
        "mj_no_trade": block_reason or "",
        "direction": direction,
        "spec": SPEC_TAG,
        "execution_mode": exec_label,
        "paper_only_lane": paper_only,
    }
    if cfg:
        row.update(
            {
                "ribbon_filter_enabled": bool(cfg.get("ribbon_filter_enabled", False)),
                "ema_separation_enabled": bool(cfg.get("ema_separation_enabled", False)),
                "supertrend_enabled": bool(cfg.get("supertrend_enabled", False)),
                "supertrend_length": int(cfg.get("supertrend_length", 5)),
                "supertrend_factor": float(cfg.get("supertrend_factor", 1.0)),
            }
        )
    if extra:
        row.update(extra)
    return row


def _close_crossed_above(prev_c: float, prev_e8: float, c: float, e8: float) -> bool:
    return prev_c <= prev_e8 and c > e8


def _close_crossed_below(prev_c: float, prev_e8: float, c: float, e8: float) -> bool:
    return prev_c >= prev_e8 and c < e8


def _touch_retest(*, bullish: bool, low: float, high: float, e8: float, tol: float) -> bool:
    if bullish:
        return low <= e8 + tol
    return high >= e8 - tol


def _filter_blocks(
    bullish: bool,
    e4: float,
    e8: float,
    b15: pd.DataFrame,
    cfg: Dict[str, Any],
    bar_ci: int,
    *,
    live_spot: Optional[float] = None,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> str:
    if live_spot is not None and live_spot > 0:
        e4, e8, _, _ = compute_dynamic_emas_on_bar(
            b15,
            bar_ci=bar_ci,
            live_spot=float(live_spot),
            form_high=form_high,
            form_low=form_low,
        )
    if _ribbon_blocks(e4, e8, cfg):
        return 'NO TRADE — 4/8 ribbon (filter ON)'
    if _separation_blocks(bullish, e4, e8, cfg):
        return "NO TRADE — EMA separation filter (4 must be above 8 for long, below for short)"
    if _supertrend_blocks(
        bullish,
        b15,
        cfg,
        bar_ci,
        live_spot=live_spot,
        form_high=form_high,
        form_low=form_low,
    ):
        return "NO TRADE — SuperTrend confirmation failed"
    return ""


def _arm_new(proto_state: Dict[str, Any], *, direction: str, cross_i: int, e8: float, day: str) -> None:
    proto_state["arm"] = {
        "direction": direction,
        "cross_i": cross_i,
        "cross_e8": e8,
        "fired_key": f"{day}:PROTO_{direction}:{cross_i}",
    }


def _maybe_flip_arm(proto_state: Dict[str, Any], bull_cross: bool, bear_cross: bool, cross_i: int, e8: float, day: str) -> None:
    arm = proto_state.get("arm")
    if bull_cross:
        if arm and str(arm.get("direction")).upper() == "SHORT":
            proto_state["last_flip"] = "FLIPPED_TO_LONG_ARM"
        _arm_new(proto_state, direction="LONG", cross_i=cross_i, e8=e8, day=day)
    elif bear_cross:
        if arm and str(arm.get("direction")).upper() == "LONG":
            proto_state["last_flip"] = "FLIPPED_TO_SHORT_ARM"
        _arm_new(proto_state, direction="SHORT", cross_i=cross_i, e8=e8, day=day)


def _build_signal(
    *,
    bullish: bool,
    cfg: Dict[str, Any],
    index_price: float,
    e8_val: float,
    cross_i: int,
    slot: int,
    intrabar: bool,
    gate_meta: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    paper_only, exec_label = _execution_mode_labels(cfg)
    sid = SID_LONG if bullish else SID_SHORT
    setup = (
        "8 EMA Prototype (Long) — retouch"
        if bullish
        else "8 EMA Prototype (Short) — retouch"
    )
    touch_mode = "LIVE_TOUCH" if intrabar else "CLOSE_BAR_TOUCH"
    return {
        "strategy_id": sid,
        "setup_name": setup,
        "direction": "LONG" if bullish else "SHORT",
        "opt_side": "CE" if bullish else "PE",
        "target_pts": float(cfg["target_pts"]),
        "sl_pts": float(cfg["sl_pts"]),
        "index_price": index_price,
        "conviction": SPEC_TAG,
        "paper_only": paper_only,
        "ema8_prototype_paper_only": paper_only,
        "prototype_lane": True,
        "execution_mode": exec_label,
        "min_option_premium_inr": float(cfg.get("min_option_premium_inr", 200)),
        "details": (
            f"{SPEC_TAG} | {touch_mode} | cross bar {cross_i} | slot {slot} | "
            f"8EMA {e8_val:.1f} | spot {index_price:.1f}"
        ),
        **gate_meta("ema8_prototype"),
    }


def evaluate_ema8_prototype(
    b15: pd.DataFrame,
    *,
    bar_ci: int = -2,
    cfg: Dict[str, Any],
    proto_state: Dict[str, Any],
    session_day: str,
    bb_gate_active: bool = False,
    gate_meta: Optional[Callable[[str], Dict[str, Any]]] = None,
    live_spot: Optional[float] = None,
    intrabar: bool = False,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Closed-bar cross/flip + optional intrabar touch on dynamic 8 EMA."""
    gate_meta = gate_meta or (lambda _k: {})
    tol = float(cfg.get("touch_tol_pts", 5.0))

    if session_day and proto_state.get("day") != session_day:
        proto_state.clear()
        proto_state["day"] = session_day

    n = len(b15)
    if n < 5:
        return [], _status(
            "SCANNING",
            phase="WAITING_FOR_CLOSE",
            block_reason="NO TRADE — Insufficient 15m bars",
            cfg=cfg,
        )

    i = _idx_abs(bar_ci, n)
    if i < 1:
        return [], _status("SCANNING", phase="WAITING_FOR_CLOSE", block_reason="NO TRADE — Need prior bar", cfg=cfg)

    if live_spot and live_spot > 0 and (intrabar or bar_ci == -1):
        e4_val, e8_val, high, low = compute_dynamic_emas_on_bar(
            b15,
            bar_ci=bar_ci,
            live_spot=float(live_spot),
            form_high=form_high,
            form_low=form_low,
        )
        c = float(live_spot)
        prev_i = i - 1
        prev_c = float(b15["close"].iloc[prev_i])
        _, prev_e8_s = ema4_ema8(b15["close"])
        prev_e8 = float(prev_e8_s.iloc[prev_i])
    else:
        e4_s, e8_s = ema4_ema8(b15["close"])
        c = float(b15["close"].iloc[bar_ci])
        low = float(b15["low"].iloc[bar_ci])
        high = float(b15["high"].iloc[bar_ci])
        e4_val = float(e4_s.iloc[bar_ci])
        e8_val = float(e8_s.iloc[bar_ci])
        prev_c = float(b15["close"].iloc[bar_ci - 1])
        prev_e8 = float(e8_s.iloc[bar_ci - 1])

    signals: List[Dict[str, Any]] = []

    if not intrabar:
        bull_cross = _close_crossed_above(prev_c, prev_e8, c, e8_val)
        bear_cross = _close_crossed_below(prev_c, prev_e8, c, e8_val)
        if bull_cross or bear_cross:
            _maybe_flip_arm(proto_state, bull_cross, bear_cross, i, e8_val, session_day)

    arm = proto_state.get("arm")
    if not arm:
        return [], _status(
            "SCANNING",
            phase="WAITING_FOR_CLOSE",
            block_reason="NO TRADE — Waiting for 15m close cross of 8 EMA",
            cfg=cfg,
        )

    direction = str(arm.get("direction", "")).upper()
    cross_i = int(arm.get("cross_i", i))
    slot = i - cross_i
    bullish = direction == "LONG"

    if slot <= 0:
        return [], _status(
            "ARMED",
            phase="ARMED_CE" if bullish else "ARMED_PE",
            armed=True,
            direction=direction,
            block_reason="NO TRADE — No entry on crossing candle; wait for re-touch",
            cfg=cfg,
            extra={"dynamic_e8": round(e8_val, 2), "e8_refresh_seconds": cfg.get("e8_refresh_seconds")},
        )

    block = _filter_blocks(
        bullish,
        e4_val,
        e8_val,
        b15,
        cfg,
        bar_ci,
        live_spot=live_spot if intrabar else None,
        form_high=form_high if intrabar else None,
        form_low=form_low if intrabar else None,
    )
    if block:
        return [], _status(
            "ARMED",
            phase="WAITING_FOR_RETOUCH",
            armed=True,
            direction=direction,
            block_reason=block,
            cfg=cfg,
        )

    touched = _touch_retest(bullish=bullish, low=low, high=high, e8=e8_val, tol=tol)
    if touched:
        signals.append(
            _build_signal(
                bullish=bullish,
                cfg=cfg,
                index_price=c,
                e8_val=e8_val,
                cross_i=cross_i,
                slot=slot,
                intrabar=intrabar,
                gate_meta=gate_meta,
            )
        )
        proto_state.pop("arm", None)
        return signals, _status(
            "TRIGGERED",
            phase="ENTRY",
            armed=True,
            direction=direction,
            cfg=cfg,
        )

    wait = f"NO TRADE — Waiting for re-touch to dynamic 8 EMA (slot {slot})"
    return [], _status(
        "ARMED",
        phase="WAITING_FOR_RETOUCH",
        armed=True,
        direction=direction,
        block_reason=wait,
        cfg=cfg,
        extra={"dynamic_e8": round(e8_val, 2)},
    )


def evaluate_ema8_prototype_intrabar(
    b15: pd.DataFrame,
    live_spot: float,
    *,
    cfg: Dict[str, Any],
    proto_state: Dict[str, Any],
    session_day: str,
    gate_meta: Optional[Callable[[str], Dict[str, Any]]] = None,
    form_high: Optional[float] = None,
    form_low: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if live_spot <= 0:
        return [], _status("SCANNING", phase="WAITING_FOR_CLOSE", cfg=cfg)
    return evaluate_ema8_prototype(
        b15,
        bar_ci=-1,
        cfg=cfg,
        proto_state=proto_state,
        session_day=session_day,
        gate_meta=gate_meta,
        live_spot=live_spot,
        intrabar=True,
        form_high=form_high,
        form_low=form_low,
    )


def _proto_signal_meta(
    cfg: Dict[str, Any],
    *,
    entry_spot: float,
    entry_ts: int,
    entry_day: str,
    entry_hm: str,
    e8: float,
) -> Dict[str, Any]:
    return {
        "ema8_prototype": True,
        "prototype_paper_only": _execution_mode_labels(cfg)[0],
        "flagship": False,
        "sr_levels": {},
        "bollinger_applied": False,
        "entry_spot": entry_spot,
        "entry_ts": entry_ts,
        "entry_day": entry_day,
        "entry_hm": entry_hm,
        "dynamic_e8_at_entry": round(e8, 2),
        "intrabar_30s": True,
    }


def backtest_ema8_prototype_at_bar(
    bars: List[Dict[str, Any]],
    idx: int,
    proto_state: Dict[str, Any],
    scanner_cfg: Dict[str, Any],
    *,
    bb_gate_active: bool = True,
    b15_df: Optional[pd.DataFrame] = None,
    day_1m: Optional[pd.DataFrame] = None,
) -> List[Tuple[str, str, str, float, float, Dict[str, Any]]]:
    """Historical step — 15m close cross + 30s retouch (ribbon / separation / SuperTrend on forming bar)."""
    cfg = load_ema8_prototype_cfg(scanner_cfg)
    if not cfg.get("enabled", True) or idx < 1 or idx >= len(bars):
        return []

    day_key = str(bars[idx].get("day") or "")
    if day_key and proto_state.get("day") != day_key:
        proto_state.clear()
        proto_state["day"] = day_key

    b = bars[idx]
    b_prev = bars[idx - 1]
    c = float(b["close"])
    e8 = float(b["e8"])
    prev_c = float(b_prev["close"])
    prev_e8 = float(b_prev["e8"])
    tol = float(cfg.get("touch_tol_pts", 5.0))
    refresh = float(cfg.get("e8_refresh_seconds", 30.0) or 30.0)

    bull_cross = _close_crossed_above(prev_c, prev_e8, c, e8)
    bear_cross = _close_crossed_below(prev_c, prev_e8, c, e8)
    if bull_cross or bear_cross:
        _maybe_flip_arm(proto_state, bull_cross, bear_cross, idx, e8, day_key)

    arm = proto_state.get("arm")
    if not arm:
        return []

    direction = str(arm["direction"]).upper()
    cross_i = int(arm["cross_i"])
    slot = idx - cross_i
    bullish = direction == "LONG"
    if slot <= 0:
        return []

    bar_open_ts = int(b.get("ts") or 0)
    use_intrabar = b15_df is not None and not b15_df.empty and day_1m is not None and bar_open_ts > 0

    if use_intrabar:
        bar_ci = idx
        ticks = intrabar_tick_timestamps(bar_open_ts, refresh)
        for tick_ts in ticks:
            fh, fl, spot = forming_ohlc_from_1m(day_1m, bar_open_ts=bar_open_ts, tick_ts=tick_ts)
            if spot <= 0:
                continue
            e4, e8_tick, high, low = compute_dynamic_emas_on_bar(
                b15_df,
                bar_ci=bar_ci,
                live_spot=spot,
                form_high=fh,
                form_low=fl,
            )
            block = _filter_blocks(
                bullish,
                e4,
                e8_tick,
                b15_df,
                cfg,
                bar_ci,
                live_spot=spot,
                form_high=fh,
                form_low=fl,
            )
            if block:
                continue
            if _touch_retest(bullish=bullish, low=fl, high=fh, e8=e8_tick, tol=tol):
                tick_hm = (
                    pd.to_datetime(tick_ts, unit="s", utc=True)
                    .tz_convert("Asia/Kolkata")
                    .strftime("%H:%M:%S")
                )
                sid = SID_LONG if bullish else SID_SHORT
                sname = (
                    "8 EMA Prototype (Long) — retouch"
                    if bullish
                    else "8 EMA Prototype (Short) — retouch"
                )
                meta = _proto_signal_meta(
                    cfg,
                    entry_spot=spot,
                    entry_ts=tick_ts,
                    entry_day=day_key,
                    entry_hm=str(tick_hm),
                    e8=e8_tick,
                )
                proto_state.pop("arm", None)
                return [(sid, sname, direction, float(cfg["target_pts"]), float(cfg["sl_pts"]), meta)]

    e4 = float(b.get("e4") or b["close"])
    low = float(b["low"])
    high = float(b["high"])
    frame = b15_df if b15_df is not None and not b15_df.empty else pd.DataFrame(
        {"open": [0], "high": [high], "low": [low], "close": [c]}
    )
    bar_ci = idx if b15_df is not None and idx < len(b15_df) else -1
    block = _filter_blocks(bullish, e4, e8, frame, cfg, bar_ci)
    if block:
        return []

    if not _touch_retest(bullish=bullish, low=low, high=high, e8=e8, tol=tol):
        return []

    sid = SID_LONG if bullish else SID_SHORT
    sname = "8 EMA Prototype (Long) — retouch" if bullish else "8 EMA Prototype (Short) — retouch"
    meta = _proto_signal_meta(
        cfg,
        entry_spot=c,
        entry_ts=bar_open_ts + 900,
        entry_day=day_key,
        entry_hm=str(b.get("hm") or ""),
        e8=e8,
    )
    meta["intrabar_30s"] = False
    proto_state.pop("arm", None)
    return [(sid, sname, direction, float(cfg["target_pts"]), float(cfg["sl_pts"]), meta)]
