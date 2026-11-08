"""Setup #1 (Stage 2 8 EMA Long-Only by Design) — Pure 15m with Symmetric Touch & Gate 4 Counter."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

log = logging.getLogger("algo.setup1")
CONFIG_FILE = Path(__file__).parent / "config.json"

def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def add_bollinger(df: pd.DataFrame, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    d = df.copy()
    if len(d) == 0:
        for col in ("bb_mid", "ubb", "lbb", "bb_width"):
            d[col] = []
        return d
    mid = d["close"].rolling(length).mean()
    sd = d["close"].rolling(length).std(ddof=0)
    d["bb_mid"] = mid
    d["ubb"] = mid + mult * sd
    d["lbb"] = mid - mult * sd
    d["bb_width"] = d["ubb"] - d["lbb"]
    return d

def add_course_emas(df: pd.DataFrame, fast: int = 4, slow: int = 8) -> pd.DataFrame:
    d = df.copy()
    if len(d) == 0:
        d[f"ema{fast}"] = []
        d[f"ema{slow}"] = []
        return d
    d[f"ema{fast}"] = d["close"].ewm(span=fast, adjust=False).mean()
    d[f"ema{slow}"] = d["close"].ewm(span=slow, adjust=False).mean()
    return d

def band_slope(series: pd.Series, candles: int = 2, tol: float = 1.5) -> str:
    s = series.dropna()
    if len(s) < candles + 1:
        return "UNKNOWN"
    steps = s.diff().tail(candles)
    if (steps > tol).all():
        return "UP"
    if (steps < -tol).all():
        return "DOWN"
    return "FLAT"

def prepare_frame(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    strat = cfg.get("strategy", {})
    return add_course_emas(
        add_bollinger(df, int(strat.get("bb_length", 20)), float(strat.get("bb_mult", 2.0))),
        int(strat.get("ema_fast", 4)),
        int(strat.get("ema_slow", 8)),
    )

@dataclass
class RallyState:
    rally_id: int = 0
    in_bullish_party: bool = False
    touch_count: int = 0
    last_touch_bar_ts: int = 0
    crossover_ts: int = 0
    last_closed_bar_ts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rally_id": self.rally_id,
            "in_bullish_party": self.in_bullish_party,
            "touch_count": self.touch_count,
            "first_opportunity_available": (self.in_bullish_party and self.touch_count == 0),
            "last_touch_bar_ts": self.last_touch_bar_ts,
            "crossover_ts": self.crossover_ts,
        }

@dataclass
class Setup1Signal:
    timestamp: int
    spot: float
    ema8: float
    ema4: float
    ubb: float
    bb_width_15m: float
    rally_id: int
    touch_number: int
    is_valid: bool
    rejection_reason: Optional[str] = None
    target_spot: float = 0.0
    sl_spot: float = 0.0

@dataclass
class ActivePosition:
    trade_id: str
    symbol: str
    security_id: str
    option_type: str
    strike: float
    quantity: int
    entry_spot: float
    entry_option_price: float
    entry_time: str
    entry_ts: int
    spot_sl: float
    spot_target: float
    option_sl: float
    option_target: float
    delta: float
    cost_to_cost_active: bool = False
    highest_spot: float = 0.0
    lowest_spot: float = 0.0
    exchange_sl_order_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    status: str = "OPEN"
    exit_spot: Optional[float] = None
    exit_option_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    execution_mode: str = "PAPER"
    expiry: str = ""
    exit_order_id: Optional[str] = None
    pnl_pts: float = 0.0
    pnl_inr: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "security_id": self.security_id,
            "option_type": self.option_type,
            "strike": self.strike,
            "quantity": self.quantity,
            "entry_spot": round(self.entry_spot, 2),
            "entry_option_price": round(self.entry_option_price, 2),
            "entry_time": self.entry_time,
            "entry_ts": self.entry_ts,
            "spot_sl": round(self.spot_sl, 2),
            "spot_target": round(self.spot_target, 2),
            "option_sl": round(self.option_sl, 2),
            "option_target": round(self.option_target, 2),
            "delta": round(self.delta, 2),
            "cost_to_cost_active": self.cost_to_cost_active,
            "highest_spot": round(self.highest_spot, 2),
            "lowest_spot": round(self.lowest_spot, 2),
            "exchange_sl_order_id": self.exchange_sl_order_id,
            "status": self.status,
            "exit_spot": round(self.exit_spot, 2) if self.exit_spot is not None else None,
            "exit_option_price": round(self.exit_option_price, 2) if self.exit_option_price is not None else None,
            "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "pnl_pts": round(self.pnl_pts, 2),
            "pnl_inr": round(self.pnl_inr, 2),
        }

class Setup1StrategyEvaluator:
    """Setup #1 is LONG-ONLY by design (Stage 2 Bullish Continuation)."""
    def __init__(self, config_dict: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = config_dict or load_config()
        self.rally = RallyState()
        self.stage2_stops_count: int = 0
        self.stage2_stopped_today: bool = False
        self.daily_trade_count: int = 0
        self.daily_pnl_inr: float = 0.0
        self.closed_trades: List[Dict[str, Any]] = []

    def reload_config(self) -> None:
        self.cfg = load_config()

    def reset_day(self) -> None:
        self.rally = RallyState()
        self.stage2_stops_count = 0
        self.stage2_stopped_today = False
        self.daily_trade_count = 0
        self.daily_pnl_inr = 0.0
        self.closed_trades = []

    def evaluate_15m_preconditions(self, df_15m: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        strat = self.cfg.get("strategy", {})
        bb_width_min = float(strat.get("bb_width_min_15m", 60.0))
        fast = int(strat.get("ema_fast", 4))
        slow = int(strat.get("ema_slow", 8))
        slope_candles = int(strat.get("ubb_slope_min_candles", 2))
        slope_tol = float(strat.get("ubb_slope_tol", 1.5))

        if df_15m.empty or len(df_15m) < 20:
            return False, {"ready": False, "bullish": False, "reason": "Insufficient 15m candles (< 20)"}

        f15 = prepare_frame(df_15m, self.cfg)
        ref_bar = f15.iloc[-2] if len(f15) >= 2 else f15.iloc[-1]
        e4 = float(ref_bar[f"ema{fast}"])
        e8 = float(ref_bar[f"ema{slow}"])
        bb_width = float(ref_bar["bb_width"])
        ubb_series = f15["ubb"].iloc[:-1] if len(f15) >= 2 else f15["ubb"]
        ubb_slope = band_slope(ubb_series, candles=slope_candles, tol=slope_tol)

        party_switch_ok = (e4 > e8)
        width_ok = (bb_width > bb_width_min)
        ubb_slope_ok = (ubb_slope == "UP")
        ready = (party_switch_ok and width_ok and ubb_slope_ok)

        return ready, {
            "ready": ready,
            "bullish": party_switch_ok and width_ok,
            "ema4": round(e4, 2),
            "ema8": round(e8, 2),
            "party_switch_ok": party_switch_ok,
            "party_bullish": party_switch_ok,
            "bb_width": round(bb_width, 2),
            "bb_width_ok": width_ok,
            "bb_width_min": bb_width_min,
            "ubb": round(float(ref_bar["ubb"]), 2),
            "lbb": round(float(ref_bar["lbb"]), 2),
            "ubb_slope": ubb_slope,
            "ubb_slope_ok": ubb_slope_ok,
            "reason": "OK" if ready else "15m preconditions not met",
        }

    def evaluate_15m_trend(self, df_15m: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        return self.evaluate_15m_preconditions(df_15m)

    def update_rally_state(self, df_15m: pd.DataFrame, current_ts: int) -> RallyState:
        strat = self.cfg.get("strategy", {})
        fast = int(strat.get("ema_fast", 4))
        slow = int(strat.get("ema_slow", 8))
        if df_15m.empty or len(df_15m) < 4:
            return self.rally
        f15 = prepare_frame(df_15m, self.cfg)
        closed_ts=int(f15.iloc[-2]["ts"])
        if self.rally.last_closed_bar_ts==closed_ts:return self.rally
        self.rally.last_closed_bar_ts=closed_ts
        diff = f15[f"ema{fast}"] - f15[f"ema{slow}"]
        last_diff = diff.iloc[-2]
        prev_diff = diff.iloc[-3]
        is_bullish = bool(last_diff > 0)
        if is_bullish and prev_diff <= 0:
            self.rally.rally_id += 1
            self.rally.in_bullish_party = True
            self.rally.touch_count = 0
            self.rally.last_touch_bar_ts = 0
            self.rally.crossover_ts = current_ts
        elif not is_bullish and self.rally.in_bullish_party:
            self.rally.in_bullish_party = False
        else:
            self.rally.in_bullish_party = is_bullish
        return self.rally

    def evaluate_8ema_touch(
        self,
        df_15m: pd.DataFrame,
        current_spot: float,
        current_candle_low: Optional[float] = None,
        current_candle_high: Optional[float] = None,
    ) -> Tuple[bool, float, float]:
        strat = self.cfg.get("strategy", {})
        slow = int(strat.get("ema_slow", 8))
        touch_proximity = float(strat.get("touch_proximity_pts", 2.5))
        if df_15m.empty or len(df_15m) < 20:
            return False, 0.0, 999.0
        f15 = prepare_frame(df_15m, self.cfg)
        ref_bar = f15.iloc[-2] if len(f15) >= 2 else f15.iloc[-1]
        ema8_val = float(ref_bar[f"ema{slow}"])
        dist = abs(current_spot - ema8_val)
        spot_touches = (dist <= touch_proximity)
        candle_touches = False
        if current_candle_low is not None and current_candle_high is not None:
            candle_touches = (current_candle_low <= ema8_val <= current_candle_high) and (current_spot >= ema8_val - touch_proximity)
        return (spot_touches or candle_touches), ema8_val, dist

    def check_setup1_entry_signal(
        self,
        df_15m: pd.DataFrame,
        current_spot: float,
        current_ts: int,
        current_time: dtime,
        current_candle_low: Optional[float] = None,
        current_candle_high: Optional[float] = None,
        current_vwap: Optional[float] = None,
        gap_pts: Optional[float] = None,
    ) -> Setup1Signal:
        strat = self.cfg.get("strategy", {})
        risk = self.cfg.get("risk", {})
        fixed_sl = float(strat.get("fixed_sl_pts", 40.0))
        target_pts = float(strat.get("target_pts", 40.0))
        h, m = map(int, risk.get("no_entry_after", "15:00").split(":"))
        no_entry_time = dtime(h, m)
        fii_h, fii_m = map(int, strat.get("fii_commit_start_time", "09:30").split(":"))
        fii_start_time = dtime(fii_h, fii_m)
        ema_sep_min = float(strat.get("ema_separation_min_pts", 3.0))
        gap_max = float(strat.get("gap_up_max_pts", 60.0))

        self.update_rally_state(df_15m, current_ts)
        f15 = prepare_frame(df_15m, self.cfg) if not df_15m.empty else df_15m
        ref_bar = f15.iloc[-2] if len(f15) >= 2 else (f15.iloc[-1] if not df_15m.empty else None)
        current_bar_ts = int(df_15m.iloc[-1]["ts"]) if not df_15m.empty else 0

        ema8_val = float(ref_bar[f"ema{strat.get('ema_slow', 8)}"]) if ref_bar is not None else 0.0
        ema4_val = float(ref_bar[f"ema{strat.get('ema_fast', 4)}"]) if ref_bar is not None else 0.0
        ubb_val = float(ref_bar["ubb"]) if ref_bar is not None else 0.0
        bb_width_15m = float(ref_bar["bb_width"]) if ref_bar is not None else 0.0

        def reject(reason: str) -> Setup1Signal:
            return Setup1Signal(current_ts, current_spot, ema8_val, ema4_val, ubb_val, bb_width_15m, self.rally.rally_id, self.rally.touch_count, False, reason)

        is_touch, touch_ema8, dist = self.evaluate_8ema_touch(df_15m, current_spot, current_candle_low, current_candle_high)
        if not is_touch:
            return reject(f"15m 8 EMA not touched (dist {dist:.1f} pts)")
        if not self.rally.in_bullish_party:
            return reject("Not in bullish 15m 4/8 EMA rally")

        if self.rally.last_touch_bar_ts != current_bar_ts:
            self.rally.touch_count += 1
            self.rally.last_touch_bar_ts = current_bar_ts

        if self.rally.touch_count > 1:
            return reject(f"Touch #{self.rally.touch_count} in Rally #{self.rally.rally_id} — second opportunity skipped")
        if current_time < fii_start_time:
            return reject("Before 09:30 IST")
        if current_time >= no_entry_time:
            return reject("After no_entry_after cutoff")
        if gap_pts is not None and gap_pts > gap_max:
            return reject(f"Gap-Up Trap (+{gap_pts:.1f} pts)")

        stop_limit = int(risk.get("stage2_daily_stop_limit", 1))
        if self.stage2_stops_count >= stop_limit or self.stage2_stopped_today:
            return reject("Stage 2 daily stop already hit")

        pre_ok, pre_info = self.evaluate_15m_preconditions(df_15m)
        if not pre_ok:
            return reject(f"Preconditions rejected: {pre_info.get('reason')}")
        if (ema4_val - ema8_val) < ema_sep_min:
            return reject("EMA separation below minimum")
        if current_vwap is not None and current_spot < current_vwap:
            return reject("Spot below intraday VWAP")

        return Setup1Signal(current_ts, current_spot, touch_ema8, ema4_val, ubb_val, bb_width_15m, self.rally.rally_id, self.rally.touch_count, True, None, current_spot + target_pts, current_spot - fixed_sl)

    def evaluate_active_position(
        self,
        pos: ActivePosition,
        current_spot: float,
        current_option_ltp: float,
        df_15m: Optional[pd.DataFrame] = None,
        last_closed_candle_15m: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        strat = self.cfg.get("strategy", {})
        ctc_trigger = float(strat.get("cost_to_cost_trigger_pts", 40.0))
        vol_candle_pts = float(strat.get("volatility_candle_pts", 100.0))
        slope_candles = int(strat.get("ubb_slope_min_candles", 2))
        slope_tol = float(strat.get("ubb_slope_tol", 1.5))
        cand = last_closed_candle_15m

        if current_spot > pos.highest_spot:
            pos.highest_spot = current_spot
        new_sl = pos.spot_sl

        if cand is not None and (cand.get("h", 0.0) - cand.get("l", 0.0)) >= vol_candle_pts:
            return True, "Volatility Exit: Candle range >= 100 pts", None

        brokerage_buffer_pts = float(strat.get("cost_to_cost_brokerage_buffer_pts", 9.0))
        if not pos.cost_to_cost_active and (pos.highest_spot - pos.entry_spot >= ctc_trigger):
            pos.cost_to_cost_active = True
            new_sl = max(new_sl, pos.entry_spot + brokerage_buffer_pts)

        if pos.cost_to_cost_active and cand is not None:
            if cand.get("c", 0.0) > pos.entry_spot and cand.get("l", 0.0) > new_sl:
                new_sl = max(new_sl, cand.get("l", 0.0))

        if df_15m is not None and not df_15m.empty and len(df_15m) >= 20:
            f15 = prepare_frame(df_15m, self.cfg)
            ubb_series = f15["ubb"].iloc[:-1] if len(f15) >= 2 else f15["ubb"]
            if band_slope(ubb_series, candles=slope_candles, tol=slope_tol) == "FLAT":
                return True, "Volatility Exit: UBB curled FLAT", None

        if current_spot >= pos.spot_target:
            return True, "Target Hit (+40 pts)", None

        if current_spot <= new_sl:
            if new_sl == pos.entry_spot - float(strat.get("fixed_sl_pts", 40.0)):
                self.stage2_stops_count += 1
                if self.stage2_stops_count >= int(self.cfg.get("risk", {}).get("stage2_daily_stop_limit", 1)):
                    self.stage2_stopped_today = True
                return True, "Initial Stop-Loss Hit (-40 pts)", None
            return True, f"Trailing/CTC Stop Hit (SL {new_sl:.1f})", None

        return (False, None, new_sl) if new_sl != pos.spot_sl else (False, None, None)
