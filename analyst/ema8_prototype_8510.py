"""
8 EMA Prototype — standalone lane (NOT Stage 2/3, NOT crossover, NOT FLAGSHIP).

15m Nifty spot · close cross → arm → re-touch dynamic 8 EMA (30s refresh) → entry on touch.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
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
    "touch_tol_pts": 3.0,
    "touch_tol_slow_pts": 0.0,
    "slow_market_exact_line": False,
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
    "retouch_requires_away": True,
}


def get_effective_touch_tolerance(cfg: Dict[str, Any]) -> float:
    """Return touch tolerance in points (3.0 default, 0.0 when slow market exact line is ON)."""
    if bool(cfg.get("slow_market_exact_line", False)):
        return float(cfg.get("touch_tol_slow_pts", 0.0) or 0.0)
    return float(cfg.get("touch_tol_pts", 3.0) or 3.0)


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
    cur_h = float(form_high) if form_high is not None else float(work.iat[i, work.columns.get_loc("high")])
    cur_l = float(form_low) if form_low is not None else float(work.iat[i, work.columns.get_loc("low")])
    work.iat[i, work.columns.get_loc("high")] = max(cur_h, float(live_spot))
    work.iat[i, work.columns.get_loc("low")] = min(cur_l, float(live_spot))
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
                "retouch_requires_away": bool(cfg.get("retouch_requires_away", True)),
                "slow_market_exact_line": bool(cfg.get("slow_market_exact_line", False)),
                "touch_tol_pts": float(cfg.get("touch_tol_pts", 3.0)),
                "touch_tol_slow_pts": float(cfg.get("touch_tol_slow_pts", 0.0)),
                "effective_touch_tol_pts": get_effective_touch_tolerance(cfg),
            }
        )
    if extra:
        row.update(extra)
    return row


def _close_crossed_above(
    prev_c: float, prev_e8: float, c: float, e8: float, open_p: Optional[float] = None
) -> bool:
    if prev_c <= prev_e8 and c > e8:
        return True
    if open_p is not None and open_p <= e8 and c > e8:
        return True
    return False


def _close_crossed_below(
    prev_c: float, prev_e8: float, c: float, e8: float, open_p: Optional[float] = None
) -> bool:
    if prev_c >= prev_e8 and c < e8:
        return True
    if open_p is not None and open_p >= e8 and c < e8:
        return True
    return False


def _touch_retest(*, bullish: bool, low: float, high: float, e8: float, tol: float) -> bool:
    """Price bar must bracket the 8 EMA line (within tol), not merely trade on one side."""
    band_lo = e8 - tol
    band_hi = e8 + tol
    if bullish:
        return low <= band_hi and high >= band_lo
    return high >= band_lo and low <= band_hi


def check_retouch(
    arm: Dict[str, Any],
    *,
    bullish: bool,
    e8_val: float,
    tol: float,
    spot: float,
    tick_high: Optional[float] = None,
    tick_low: Optional[float] = None,
    retouch_requires_away: bool = True,
    candle_high: Optional[float] = None,
    candle_low: Optional[float] = None,
) -> bool:
    """Evaluates whether price retouches dynamic 8 EMA from the correct side.

    If retouch_requires_away is True:
      - Price must have moved away from dynamic 8 EMA:
        For CE (bullish): tick_high > dynamic_e8 + tol
        For PE (bearish): tick_low < dynamic_e8 - tol
      - Retouch fires only when away_seen is True and:
        For CE (bullish): tick_low <= dynamic_e8 + tol
        For PE (bearish): tick_high >= dynamic_e8 - tol
    If retouch_requires_away is False:
      - Uses legacy overlap check on candle_high and candle_low (or tick).
    """
    if not retouch_requires_away:
        lo = candle_low if candle_low is not None else (tick_low if tick_low is not None else spot)
        hi = candle_high if candle_high is not None else (tick_high if tick_high is not None else spot)
        return _touch_retest(bullish=bullish, low=lo, high=hi, e8=e8_val, tol=tol)

    chk_high = float(tick_high) if tick_high is not None else float(spot)
    chk_low = float(tick_low) if tick_low is not None else float(spot)

    if bullish:
        if not arm.get("away_seen", False):
            if chk_high > e8_val + tol:
                arm["away_seen"] = True
        if arm.get("away_seen", False):
            return chk_low <= e8_val + tol
        return False
    else:
        if not arm.get("away_seen", False):
            if chk_low < e8_val - tol:
                arm["away_seen"] = True
        if arm.get("away_seen", False):
            return chk_high >= e8_val - tol
        return False


def current_nse_15m_open_ts(now: Optional[datetime] = None) -> int:
    """IST wall-clock floor to current 15m bucket open (09:15 anchor)."""
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    now = now or datetime.now(ist)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ist)
    else:
        now = now.astimezone(ist)
    day = now.date()
    session_open = datetime.combine(day, dtime(9, 15), tzinfo=ist)
    if now < session_open:
        return int(session_open.timestamp())
    elapsed = int((now - session_open).total_seconds())
    bucket = max(0, (elapsed // 900) * 900)
    return int(session_open.timestamp()) + bucket


def ensure_forming_b15_bar(
    b15: pd.DataFrame,
    *,
    bar_open_ts: int,
    live_spot: float,
) -> Tuple[pd.DataFrame, int]:
    """Append a synthetic forming 15m row when resample lags the live bucket."""
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    open_dt = pd.Timestamp(bar_open_ts, unit="s", tz=ist)
    spot = float(live_spot)
    if b15.empty:
        row = pd.DataFrame(
            {"open": [spot], "high": [spot], "low": [spot], "close": [spot], "volume": [0]},
            index=[open_dt],
        )
        return row, 0
    last_open = b15.index[-1]
    if hasattr(last_open, "tzinfo") and last_open.tzinfo is None:
        last_open = last_open.tz_localize(ist)
    if open_dt > last_open:
        extra = pd.DataFrame(
            {"open": [spot], "high": [spot], "low": [spot], "close": [spot], "volume": [0]},
            index=[open_dt],
        )
        b15 = pd.concat([b15, extra])
    return b15, -1


def prototype_spot_is_long(
    spot_entry: float,
    spot_target: float,
    *,
    strategy_id: str = "",
    symbol: str = "",
) -> bool:
    """Infer Nifty spot direction (CE long vs PE short); option side is always BUY."""
    if spot_entry > 0 and spot_target > 0:
        return spot_target > spot_entry
    sid = str(strategy_id).upper()
    sym = str(symbol).upper()
    return "LONG" in sid or sid.endswith("_LONG") or " CE" in sym or sym.endswith("CE")


def _opposite_8ema_close(open_dir: str, close: float, e8: float) -> bool:
    d = str(open_dir).upper()
    if d == "LONG":
        return close < e8
    if d == "SHORT":
        return close > e8
    return False


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


def bar_open_timestamp(b15: pd.DataFrame, i: int) -> int:
    """Extract unix timestamp of b15[i] open time."""
    n = len(b15)
    if n == 0:
        return 0
    idx = _idx_abs(i, n)
    val = b15.index[idx]
    if hasattr(val, "timestamp"):
        return int(val.timestamp())
    try:
        return int(val)
    except (ValueError, TypeError):
        return int(pd.Timestamp(val).timestamp())


def _arm_new(
    proto_state: Dict[str, Any],
    *,
    direction: str,
    cross_bar_open_ts: int,
    e8: float,
    away_seen: bool = False,
    day: str = "",
) -> None:
    proto_state["arm"] = {
        "direction": direction,
        "cross_bar_open_ts": int(cross_bar_open_ts),
        "cross_e8": float(e8),
        "away_seen": bool(away_seen),
    }


def _maybe_flip_arm(
    proto_state: Dict[str, Any],
    bull_cross: bool,
    bear_cross: bool,
    cross_bar_open_ts: int,
    e8: float,
    day: str = "",
) -> None:
    arm = proto_state.get("arm")
    last_dir = str(proto_state.get("last_traded_direction") or "").upper()
    if bull_cross:
        if last_dir == "LONG":
            return
        if arm and str(arm.get("direction")).upper() == "SHORT":
            proto_state["last_flip"] = "FLIPPED_TO_LONG_ARM"
        proto_state.pop("last_traded_direction", None)
        _arm_new(proto_state, direction="LONG", cross_bar_open_ts=cross_bar_open_ts, e8=e8, away_seen=False, day=day)
    elif bear_cross:
        if last_dir == "SHORT":
            return
        if arm and str(arm.get("direction")).upper() == "LONG":
            proto_state["last_flip"] = "FLIPPED_TO_SHORT_ARM"
        proto_state.pop("last_traded_direction", None)
        _arm_new(proto_state, direction="SHORT", cross_bar_open_ts=cross_bar_open_ts, e8=e8, away_seen=False, day=day)


def _build_signal(
    *,
    bullish: bool,
    cfg: Dict[str, Any],
    index_price: float,
    e8_val: float,
    cross_bar_open_ts: int = 0,
    intrabar: bool = True,
    gate_meta: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    gate_meta = gate_meta or (lambda _k: {})
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
        "index_price": float(index_price),
        "spot_entry": float(index_price),
        "dynamic_e8_at_entry": round(float(e8_val), 2),
        "conviction": SPEC_TAG,
        "paper_only": paper_only,
        "ema8_prototype_paper_only": paper_only,
        "prototype_lane": True,
        "execution_mode": exec_label,
        "min_option_premium_inr": float(cfg.get("min_option_premium_inr", 200)),
        "details": (
            f"{SPEC_TAG} | {touch_mode} | cross_ts {cross_bar_open_ts} | "
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
    tick_high: Optional[float] = None,
    tick_low: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Closed-bar cross/flip + optional intrabar touch on dynamic 8 EMA."""
    gate_meta = gate_meta or (lambda _k: {})
    tol = get_effective_touch_tolerance(cfg)

    if session_day:
        if proto_state.get("day") != session_day:
            if not bool(cfg.get("arm_until_opposite_close", True)):
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
    actions: List[Dict[str, Any]] = []

    if not intrabar:
        open_pos = proto_state.get("open")
        if open_pos and _opposite_8ema_close(str(open_pos.get("direction")), c, e8_val):
            actions.append(
                {
                    "type": "SQUARE_OFF",
                    "reason": "OPPOSITE_8EMA_CLOSE",
                    "direction": str(open_pos.get("direction")).upper(),
                }
            )
            proto_state.pop("open", None)
            proto_state.pop("arm", None)

        o_bar = float(b15["open"].iloc[bar_ci]) if "open" in b15.columns else None
        bull_cross = _close_crossed_above(prev_c, prev_e8, c, e8_val, o_bar)
        bear_cross = _close_crossed_below(prev_c, prev_e8, c, e8_val, o_bar)
        if bull_cross or bear_cross:
            if _ribbon_blocks(e4_val, e8_val, cfg):
                proto_state.pop("arm", None)
                st = _status(
                    "SCANNING",
                    phase="WAITING_FOR_CLOSE",
                    armed=False,
                    block_reason="NO TRADE — 4/8 ribbon (filter ON) — do not arm",
                    cfg=cfg,
                )
                st["actions"] = actions
                return [], st
            cross_open_ts = bar_open_timestamp(b15, bar_ci)
            _maybe_flip_arm(proto_state, bull_cross, bear_cross, cross_open_ts, e8_val, session_day)
            if bull_cross:
                actions.append({"type": "ARM", "direction": "LONG", "cross_bar_open_ts": cross_open_ts})
            elif bear_cross:
                actions.append({"type": "ARM", "direction": "SHORT", "cross_bar_open_ts": cross_open_ts})

    arm = proto_state.get("arm")
    if not arm:
        st = _status(
            "SCANNING",
            phase="WAITING_FOR_CLOSE",
            block_reason="NO TRADE — Waiting for 15m close cross of 8 EMA",
            cfg=cfg,
        )
        st["actions"] = actions
        return [], st

    direction = str(arm.get("direction", "")).upper()
    cross_bar_open_ts = int(arm.get("cross_bar_open_ts", 0))
    if cross_bar_open_ts == 0 and "cross_i" in arm:
        ci = int(arm["cross_i"])
        cross_bar_open_ts = bar_open_timestamp(b15, ci) if 0 <= ci < len(b15) else ci

    current_bar_open_ts = bar_open_timestamp(b15, bar_ci)
    bullish = direction == "LONG"

    # 1. No entry on crossing candle
    if current_bar_open_ts <= cross_bar_open_ts:
        st = _status(
            "ARMED",
            phase="ARMED_CE" if bullish else "ARMED_PE",
            armed=True,
            direction=direction,
            block_reason="NO TRADE — No entry on crossing candle; wait for re-touch",
            cfg=cfg,
            extra={"dynamic_e8": round(e8_val, 2), "e8_refresh_seconds": cfg.get("e8_refresh_seconds")},
        )
        st["actions"] = actions
        return [], st

    # 2. One entry per cross
    if proto_state.get("consumed_cross_ts") == cross_bar_open_ts:
        st = _status(
            "ARMED",
            phase="ARMED_CE" if bullish else "ARMED_PE",
            armed=True,
            direction=direction,
            block_reason="NO TRADE — Cross already consumed by an entry",
            cfg=cfg,
            extra={"dynamic_e8": round(e8_val, 2)},
        )
        st["actions"] = actions
        return [], st

    # 3. Position already open
    if proto_state.get("open"):
        st = _status(
            "IN_TRADE",
            phase="IN_POSITION",
            armed=False,
            direction=direction,
            block_reason="NO TRADE — Prototype position already open",
            cfg=cfg,
            extra={"dynamic_e8": round(e8_val, 2)},
        )
        st["actions"] = actions
        return [], st

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
        st = _status(
            "ARMED",
            phase="WAITING_FOR_RETOUCH",
            armed=True,
            direction=direction,
            block_reason=block,
            cfg=cfg,
        )
        st["actions"] = actions
        return [], st

    retouch_requires_away = bool(cfg.get("retouch_requires_away", True))
    real_spot = float(live_spot) if (live_spot is not None and live_spot > 0) else c
    touched = check_retouch(
        arm,
        bullish=bullish,
        e8_val=e8_val,
        tol=tol,
        spot=real_spot,
        tick_high=tick_high,
        tick_low=tick_low,
        retouch_requires_away=retouch_requires_away,
        candle_high=high,
        candle_low=low,
    )
    if touched:
        signals.append(
            _build_signal(
                bullish=bullish,
                cfg=cfg,
                index_price=real_spot,
                e8_val=e8_val,
                cross_bar_open_ts=cross_bar_open_ts,
                intrabar=intrabar,
                gate_meta=gate_meta,
            )
        )
        proto_state["consumed_cross_ts"] = cross_bar_open_ts
        proto_state["last_traded_direction"] = direction
        proto_state.pop("arm", None)
        st = _status(
            "TRIGGERED",
            phase="ENTRY",
            armed=True,
            direction=direction,
            cfg=cfg,
        )
        st["actions"] = actions
        return signals, st

    away_flag = bool(arm.get("away_seen", False))
    if retouch_requires_away and not away_flag:
        wait = "NO TRADE — Waiting for price to move away from dynamic 8 EMA"
    else:
        wait = "NO TRADE — Waiting for re-touch to dynamic 8 EMA"

    st = _status(
        "ARMED",
        phase="WAITING_FOR_RETOUCH",
        armed=True,
        direction=direction,
        block_reason=wait,
        cfg=cfg,
        extra={"dynamic_e8": round(e8_val, 2), "away_seen": away_flag},
    )
    st["actions"] = actions
    return [], st


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
    tick_high: Optional[float] = None,
    tick_low: Optional[float] = None,
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
        tick_high=tick_high,
        tick_low=tick_low,
    )


def _proto_signal_meta(
    cfg: Dict[str, Any],
    *,
    entry_spot: float,
    entry_ts: int,
    entry_day: str,
    entry_hm: str,
    e8: float,
    cross_bar_open_ts: int = 0,
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
        "exit_on_opposite_8ema_close": True,
        "cross_bar_open_ts": cross_bar_open_ts,
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
        if not bool(cfg.get("arm_until_opposite_close", True)):
            proto_state.clear()
        proto_state["day"] = day_key

    b = bars[idx]
    b_prev = bars[idx - 1]
    c = float(b["close"])
    e8 = float(b["e8"])
    prev_c = float(b_prev["close"])
    prev_e8 = float(b_prev["e8"])
    tol = get_effective_touch_tolerance(cfg)
    refresh = float(cfg.get("e8_refresh_seconds", 30.0) or 30.0)

    bar_open_ts = int(b.get("ts") or 0)
    use_intrabar = b15_df is not None and not b15_df.empty and day_1m is not None and bar_open_ts > 0

    signals: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
    arm = proto_state.get("arm")
    has_open = bool(proto_state.get("open") or proto_state.get("bt_open"))
    if arm and not has_open:
        direction = str(arm["direction"]).upper()
        cross_ts = int(arm.get("cross_bar_open_ts", 0))
        if cross_ts == 0 and "cross_i" in arm:
            ci = int(arm["cross_i"])
            cross_ts = int(bars[ci].get("ts") or 0) if 0 <= ci < len(bars) else ci
        bullish = direction == "LONG"
        if bar_open_ts > cross_ts and proto_state.get("consumed_cross_ts") != cross_ts:
            if use_intrabar:
                bar_ci = idx
                ticks = intrabar_tick_timestamps(bar_open_ts, refresh)
                prev_tick_ts = bar_open_ts
                for tick_ts in ticks:
                    fh, fl, spot = forming_ohlc_from_1m(day_1m, bar_open_ts=bar_open_ts, tick_ts=tick_ts)
                    if spot <= 0:
                        prev_tick_ts = tick_ts
                        continue
                    tick_slice = day_1m[(day_1m["ts"] >= prev_tick_ts) & (day_1m["ts"] <= tick_ts)]
                    if not tick_slice.empty:
                        tick_h = max(float(tick_slice["high"].max()), spot)
                        tick_l = min(float(tick_slice["low"].min()), spot)
                    else:
                        tick_h = tick_l = spot
                    prev_tick_ts = tick_ts

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
                    retouch_requires_away = bool(cfg.get("retouch_requires_away", True))
                    if check_retouch(
                        arm,
                        bullish=bullish,
                        e8_val=e8_tick,
                        tol=tol,
                        spot=spot,
                        tick_high=tick_h,
                        tick_low=tick_l,
                        retouch_requires_away=retouch_requires_away,
                        candle_high=fh,
                        candle_low=fl,
                    ):
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
                            entry_spot=float(spot),
                            entry_ts=tick_ts,
                            entry_day=day_key,
                            entry_hm=str(tick_hm),
                            e8=e8_tick,
                            cross_bar_open_ts=cross_ts,
                        )
                        proto_state["consumed_cross_ts"] = cross_ts
                        proto_state["last_traded_direction"] = direction
                        proto_state.pop("arm", None)
                        signals.append((sid, sname, direction, float(cfg["target_pts"]), float(cfg["sl_pts"]), meta))
                        break
            else:
                e4 = float(b.get("e4") or b["close"])
                low = float(b["low"])
                high = float(b["high"])
                frame = b15_df if b15_df is not None and not b15_df.empty else pd.DataFrame(
                    {"open": [0], "high": [high], "low": [low], "close": [c]}
                )
                bar_ci = idx if b15_df is not None and idx < len(b15_df) else -1
                block = _filter_blocks(bullish, e4, e8, frame, cfg, bar_ci)
                retouch_requires_away = bool(cfg.get("retouch_requires_away", True))
                if not block and check_retouch(
                    arm,
                    bullish=bullish,
                    e8_val=e8,
                    tol=tol,
                    spot=c,
                    tick_high=high,
                    tick_low=low,
                    retouch_requires_away=retouch_requires_away,
                    candle_high=high,
                    candle_low=low,
                ):
                    sid = SID_LONG if bullish else SID_SHORT
                    sname = "8 EMA Prototype (Long) — retouch" if bullish else "8 EMA Prototype (Short) — retouch"
                    meta = _proto_signal_meta(
                        cfg,
                        entry_spot=float(c),
                        entry_ts=bar_open_ts + 900,
                        entry_day=day_key,
                        entry_hm=str(b.get("hm") or ""),
                        e8=e8,
                        cross_bar_open_ts=cross_ts,
                    )
                    meta["intrabar_30s"] = False
                    proto_state["consumed_cross_ts"] = cross_ts
                    proto_state["last_traded_direction"] = direction
                    proto_state.pop("arm", None)
                    signals.append((sid, sname, direction, float(cfg["target_pts"]), float(cfg["sl_pts"]), meta))

    # At the close of this 15m bar: check close-cross of 8 EMA to arm / flip for subsequent bars
    e4_cross = float(b.get("e4") or c)
    o_bar = float(b.get("open") or c)
    bull_cross = _close_crossed_above(prev_c, prev_e8, c, e8, o_bar)
    bear_cross = _close_crossed_below(prev_c, prev_e8, c, e8, o_bar)
    if bull_cross or bear_cross:
        if _ribbon_blocks(e4_cross, e8, cfg):
            proto_state.pop("arm", None)
        else:
            _maybe_flip_arm(proto_state, bull_cross, bear_cross, bar_open_ts, e8, day_key)

    return signals
