"""
algo_execution_engine/strategy_setup1.py — Strategy Logic & State Machine for Setup #1 (Stage 2 8 EMA Long).

Exact Trading Bible Rules:
  1. Trend Filter: 15-Minute major trend must be bullish (4 EMA > 8 EMA on 15m) and Bollinger Bands width > 60.
  2. Entry Conditions (5-Minute Chart / Live Tick Evaluation):
     - Upper Bollinger Band (UBB) must be sloping upward for 2+ consecutive candles.
     - Party-Switch Rule: 4 EMA > 8 EMA.
     - Trigger: Exact touch of the 8 EMA on the 5-minute bar (instant entry on touch, no waiting for candle close).
     - Rally Counter / First Opportunity Rule: Must be the first touch/entry in the current rally.
       If a second touch occurs in the same rally without an interim 4/8 EMA crossover reset, skip the trade.
     - Daily Stop Limit: If Setup #1 hits a stop-loss (-40 pts) once today, disable Setup #1 for the rest of the session.
  3. Risk Management & Trailing Stop Automation:
     - Initial stop-loss: Fixed -40 points.
     - Target: +40 points (with option delta conversion for ATM/ITM options).
     - Cost-to-Cost Trigger: Once price moves +40 points in favor, automatically move stop-loss to entry price.
     - Candle Trail: On each subsequent confirming candle close, trail the stop-loss to that candle's low.
     - Volatility Exit: Exit immediately if the Bollinger Band curls flat or if a single candle expands by 100+ points.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("algo.setup1")

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to read %s: %s, using defaults", CONFIG_FILE, e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR FUNCTIONS (CLOSE-ALIGNED & RESAMPLED)
# ─────────────────────────────────────────────────────────────────────────────

def add_bollinger(df: pd.DataFrame, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    """Computes Bollinger Bands on 'close'."""
    d = df.copy()
    if len(d) == 0:
        d["bb_mid"] = []
        d["ubb"] = []
        d["lbb"] = []
        d["bb_width"] = []
        return d
    mid = d["close"].rolling(length).mean()
    sd = d["close"].rolling(length).std(ddof=0)
    d["bb_mid"] = mid
    d["ubb"] = mid + mult * sd
    d["lbb"] = mid - mult * sd
    d["bb_width"] = d["ubb"] - d["lbb"]
    return d


def add_course_emas(df: pd.DataFrame, fast: int = 4, slow: int = 8) -> pd.DataFrame:
    """Computes 4 and 8 EMAs on 'close'."""
    d = df.copy()
    if len(d) == 0:
        d[f"ema{fast}"] = []
        d[f"ema{slow}"] = []
        return d
    d[f"ema{fast}"] = d["close"].ewm(span=fast, adjust=False).mean()
    d[f"ema{slow}"] = d["close"].ewm(span=slow, adjust=False).mean()
    return d


def band_slope(series: pd.Series, candles: int = 2, tol: float = 1.5) -> str:
    """
    Evaluates UP / DOWN / FLAT over the last N candles.
    FLAT is defined as absolute change <= tolerance per candle.
    """
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
    bb_len = int(strat.get("bb_length", 20))
    bb_mult = float(strat.get("bb_mult", 2.0))
    ema_fast = int(strat.get("ema_fast", 4))
    ema_slow = int(strat.get("ema_slow", 8))
    return add_course_emas(add_bollinger(df, bb_len, bb_mult), ema_fast, ema_slow)


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RallyState:
    rally_id: int = 0
    in_bullish_party: bool = False
    touch_count: int = 0
    last_touch_ts: int = 0
    crossover_ts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rally_id": self.rally_id,
            "in_bullish_party": self.in_bullish_party,
            "touch_count": self.touch_count,
            "first_opportunity_available": (self.in_bullish_party and self.touch_count == 0),
            "last_touch_ts": self.last_touch_ts,
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
    option_type: str  # "CE"
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
    exchange_sl_order_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    status: str = "OPEN"  # "OPEN" | "CLOSED_TARGET" | "CLOSED_SL" | "CLOSED_VOLATILITY" | "CLOSED_MANUAL"
    exit_spot: Optional[float] = None
    exit_option_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
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
            "exchange_sl_order_id": self.exchange_sl_order_id,
            "status": self.status,
            "exit_spot": round(self.exit_spot, 2) if self.exit_spot is not None else None,
            "exit_option_price": round(self.exit_option_price, 2) if self.exit_option_price is not None else None,
            "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "pnl_pts": round(self.pnl_pts, 2),
            "pnl_inr": round(self.pnl_inr, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY EVALUATOR CLASS
# ─────────────────────────────────────────────────────────────────────────────

class Setup1StrategyEvaluator:
    """Encapsulates Setup #1 rules, state management, rally counting, and risk exits."""

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
        """Resets intraday counters at session open."""
        self.rally = RallyState()
        self.stage2_stops_count = 0
        self.stage2_stopped_today = False
        self.daily_trade_count = 0
        self.daily_pnl_inr = 0.0
        self.closed_trades = []
        log.info("Setup #1 Strategy Evaluator session state reset")

    # ── 1. PRECONDITIONS & TREND (15-Minute Pure Setup #1) ───────────────────
    def evaluate_15m_preconditions(self, df_15m: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        """
        Setup #1 Pure 15-Minute Preconditions & Trend Filter:
          1. Party-Switch Rule: 4 EMA > 8 EMA on 15m (Bullish regime).
          2. Trend & Volatility: 15-Minute Bollinger Band width > 60 points.
          3. UBB Slope: Upper Bollinger Band (UBB) sloping upward for 2+ consecutive 15m candles.
        """
        strat = self.cfg.get("strategy", {})
        bb_width_min = float(strat.get("bb_width_min_15m", 60.0))
        fast = int(strat.get("ema_fast", 4))
        slow = int(strat.get("ema_slow", 8))
        slope_candles = int(strat.get("ubb_slope_min_candles", 2))
        slope_tol = float(strat.get("ubb_slope_tol", 1.5))

        if df_15m.empty or len(df_15m) < 20:
            return False, {"ready": False, "bullish": False, "reason": "Insufficient 15m candles (< 20)"}

        f15 = prepare_frame(df_15m, self.cfg)
        last = f15.iloc[-1]
        e4 = float(last[f"ema{fast}"])
        e8 = float(last[f"ema{slow}"])
        bb_width = float(last["bb_width"])
        ubb_slope = band_slope(f15["ubb"], candles=slope_candles, tol=slope_tol)

        party_switch_ok = (e4 > e8)
        width_ok = (bb_width > bb_width_min)
        ubb_slope_ok = (ubb_slope == "UP")
        ready = (party_switch_ok and width_ok and ubb_slope_ok)

        info = {
            "ready": ready,
            "bullish": party_switch_ok and width_ok,
            "ema4": round(e4, 2),
            "ema8": round(e8, 2),
            "party_switch_ok": party_switch_ok,
            "party_bullish": party_switch_ok,
            "bb_width": round(bb_width, 2),
            "bb_width_ok": width_ok,
            "bb_width_min": bb_width_min,
            "ubb": round(float(last["ubb"]), 2),
            "lbb": round(float(last["lbb"]), 2),
            "ubb_slope": ubb_slope,
            "ubb_slope_ok": ubb_slope_ok,
            "reason": "OK" if ready else (
                f"15m 4/8 EMA not bullish ({e4:.1f} <= {e8:.1f})" if not party_switch_ok
                else f"15m BB width {bb_width:.1f} <= {bb_width_min} (insufficient volatility)" if not width_ok
                else f"15m UBB slope is {ubb_slope} (needs UP for >= {slope_candles} candles)"
            ),
        }
        return ready, info

    # Backwards-compatible aliases
    def evaluate_15m_trend(self, df_15m: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        return self.evaluate_15m_preconditions(df_15m)

    def evaluate_5m_preconditions(self, df_frame: pd.DataFrame) -> Tuple[bool, Dict[str, Any]]:
        return self.evaluate_15m_preconditions(df_frame)

    # ── 2. RALLY STATE & FIRST OPPORTUNITY TRACKING (15-Minute) ──────────────
    def update_rally_state(self, df_15m: pd.DataFrame, current_ts: int) -> RallyState:
        """
        Tracks the current rally strictly on 15m.
        When 4 EMA crosses above 8 EMA on 15m, a new rally begins (touch_count=0).
        When 4 EMA crosses below 8 EMA on 15m, rally ends.
        """
        strat = self.cfg.get("strategy", {})
        fast = int(strat.get("ema_fast", 4))
        slow = int(strat.get("ema_slow", 8))

        if df_15m.empty or len(df_15m) < 3:
            return self.rally

        f15 = prepare_frame(df_15m, self.cfg)
        diff = f15[f"ema{fast}"] - f15[f"ema{slow}"]
        last_diff = diff.iloc[-1]
        prev_diff = diff.iloc[-2] if len(diff) >= 2 else last_diff

        # Bullish party detection
        is_bullish = bool(last_diff > 0)

        # Fresh crossover detection
        if is_bullish and prev_diff <= 0:
            self.rally.rally_id += 1
            self.rally.in_bullish_party = True
            self.rally.touch_count = 0
            self.rally.crossover_ts = current_ts
            log.info("Fresh 15m 4/8 EMA Bullish Crossover detected! Rally #%d initialized", self.rally.rally_id)
        elif not is_bullish and self.rally.in_bullish_party:
            self.rally.in_bullish_party = False
            log.info("15m 4/8 EMA Bearish flip — Rally #%d terminated", self.rally.rally_id)
        else:
            self.rally.in_bullish_party = is_bullish

        return self.rally

    # ── 3. TOUCH TRIGGER EVALUATION (15-Minute 8 EMA) ────────────────────────
    def evaluate_8ema_touch(
        self,
        df_15m: pd.DataFrame,
        current_spot: float,
        current_candle_low: Optional[float] = None,
        current_candle_high: Optional[float] = None,
    ) -> Tuple[bool, float, float]:
        """
        Evaluates exact touch of the 15-minute 8 EMA.
        Returns: (is_touch, ema8_value, distance_pts)
        """
        strat = self.cfg.get("strategy", {})
        slow = int(strat.get("ema_slow", 8))
        touch_proximity = float(strat.get("touch_proximity_pts", 2.5))

        if df_15m.empty or len(df_15m) < 20:
            return False, 0.0, 999.0

        f15 = prepare_frame(df_15m, self.cfg)
        ema8_val = float(f15.iloc[-1][f"ema{slow}"])
        dist = abs(current_spot - ema8_val)

        # 1. Direct spot touch within proximity
        spot_touches = (dist <= touch_proximity) or (current_spot <= ema8_val and current_spot >= ema8_val - 10.0)

        # 2. Candle range touches 15m 8 EMA
        candle_touches = False
        if current_candle_low is not None and current_candle_high is not None:
            candle_touches = (current_candle_low <= ema8_val <= current_candle_high)

        is_touch = spot_touches or candle_touches
        return is_touch, ema8_val, dist

    # ── 4. FULL 15-MINUTE SETUP #1 SIGNAL CHECK ──────────────────────────────
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
        df_5m: Optional[pd.DataFrame] = None,  # Backward compatibility, ignored
    ) -> Setup1Signal:
        """
        Evaluates Setup #1 rules exclusively on the 15-Minute timeframe:
          1. 15m Preconditions: 4 EMA > 8 EMA, BB width > 60 pts, UBB sloping UP >= 2 bars.
          2. 15m 8-EMA Touch Trigger: Exact touch of 15m 8 EMA on current bar.
          3. Rally Counter / First Opportunity: Only first touch in current 15m 4/8 rally.
          4. Daily Stop Limit: If stopped out today (-40 pts), disabled for day.
          5. Risk Bracket: Fixed +40 pt Target, Fixed -40 pt Stop-Loss.
          6. Time & Confluence: 09:30 to 15:00 IST, Gap <= 60 pts, EMA sep >= 3 pts, Spot >= VWAP.
        """
        strat = self.cfg.get("strategy", {})
        risk = self.cfg.get("risk", {})
        fixed_sl = float(strat.get("fixed_sl_pts", 40.0))
        target_pts = float(strat.get("target_pts", 40.0))
        no_entry_after_str = risk.get("no_entry_after", "15:00")
        h, m = map(int, no_entry_after_str.split(":"))
        no_entry_time = dtime(h, m)

        fii_start_str = strat.get("fii_commit_start_time", "09:30")
        fii_h, fii_m = map(int, fii_start_str.split(":"))
        fii_start_time = dtime(fii_h, fii_m)

        ema_sep_min = float(strat.get("ema_separation_min_pts", 3.0))
        gap_max = float(strat.get("gap_up_max_pts", 60.0))

        # Update rally state strictly on 15m
        self.update_rally_state(df_15m, current_ts)

        f15 = prepare_frame(df_15m, self.cfg) if not df_15m.empty else df_15m
        ema8_val = float(f15.iloc[-1][f"ema{strat.get('ema_slow', 8)}"]) if not df_15m.empty else 0.0
        ema4_val = float(f15.iloc[-1][f"ema{strat.get('ema_fast', 4)}"]) if not df_15m.empty else 0.0
        ubb_val = float(f15.iloc[-1]["ubb"]) if not df_15m.empty else 0.0
        bb_width_15m = float(f15.iloc[-1]["bb_width"]) if not df_15m.empty else 0.0

        # Base reject signal constructor
        def reject(reason: str) -> Setup1Signal:
            return Setup1Signal(
                timestamp=current_ts,
                spot=current_spot,
                ema8=ema8_val,
                ema4=ema4_val,
                ubb=ubb_val,
                bb_width_15m=bb_width_15m,
                rally_id=self.rally.rally_id,
                touch_number=self.rally.touch_count,
                is_valid=False,
                rejection_reason=reason,
            )

        # Gate 0: Time of Day & FII Commit Window (09:30 to 15:00 IST)
        if current_time < fii_start_time:
            return reject(f"Before {fii_start_str} IST — waiting for initial market structure to settle")

        if current_time >= no_entry_time:
            return reject(f"After {no_entry_after_str} IST — no new entries permitted")

        # Gate 0B: Gap-Up Trap Filter
        if gap_pts is not None and gap_pts > gap_max:
            return reject(f"Gap-Up Trap (+{gap_pts:.1f} pts > {gap_max:.1f} pts) — common gap rule blocks Longs")

        # Gate 1: Daily Stop Limit
        stop_limit = int(self.cfg.get("risk", {}).get("stage2_daily_stop_limit", 1))
        if self.stage2_stops_count >= stop_limit or self.stage2_stopped_today:
            return reject(f"Stage 2 stop already taken today ({self.stage2_stops_count}/{stop_limit}) — Setup #1 disabled for rest of session")

        # Gate 2: 15-Minute Preconditions (4>8, BB width > 60, UBB sloping UP)
        pre_ok, pre_info = self.evaluate_15m_preconditions(df_15m)
        if not pre_ok:
            return reject(f"15m Preconditions rejected: {pre_info.get('reason')}")

        # Gate 2B: 15m Momentum Separation (EMA separation >= 3.0 pts)
        sep = ema4_val - ema8_val
        if sep < ema_sep_min:
            return reject(f"15m 4/8 EMA separation ({sep:.1f} pts) < minimum {ema_sep_min:.1f} pts threshold")

        # Gate 2C: Volume Anchor Confluence (Spot >= VWAP)
        if current_vwap is not None and current_spot < current_vwap:
            return reject(f"Spot ({current_spot:.1f}) is below intraday VWAP ({current_vwap:.1f})")

        # Gate 3: 15-Minute 8 EMA Touch Trigger Evaluation
        is_touch, touch_ema8, dist = self.evaluate_8ema_touch(df_15m, current_spot, current_candle_low, current_candle_high)
        if not is_touch:
            return reject(f"15m 8 EMA not touched (Spot {current_spot:.1f} vs 15m 8 EMA {touch_ema8:.1f}, dist {dist:.1f} pts)")

        # Gate 4: Rally Counter / First Opportunity Rule
        if not self.rally.in_bullish_party:
            return reject("Not in a bullish 15m 4/8 EMA rally")

        if self.rally.touch_count >= 1:
            return reject(f"Already {self.rally.touch_count} touch(es) in Rally #{self.rally.rally_id} — second opportunity rule skipped")

        # All Gates Passed! Valid 15-Minute Setup #1 Entry Trigger!
        target_spot = current_spot + target_pts
        sl_spot = current_spot - fixed_sl

        signal = Setup1Signal(
            timestamp=current_ts,
            spot=current_spot,
            ema8=touch_ema8,
            ema4=ema4_val,
            ubb=ubb_val,
            bb_width_15m=bb_width_15m,
            rally_id=self.rally.rally_id,
            touch_number=1,
            is_valid=True,
            rejection_reason=None,
            target_spot=target_spot,
            sl_spot=sl_spot,
        )
        return signal

    # ── 5. POSITION LIFECYCLE & FIXED 40/40 RISK BRACKET ─────────────────────
    def evaluate_active_position(
        self,
        pos: ActivePosition,
        current_spot: float,
        current_option_ltp: float,
        df_15m: Optional[pd.DataFrame] = None,
        last_closed_candle_15m: Optional[Dict[str, float]] = None,
        df_5m: Optional[pd.DataFrame] = None,  # Backward compatibility
        last_closed_candle_5m: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        """
        Monitors active position and applies the Fixed 40/40 Risk Bracket:
          1. Fixed target (+40 pts): Exit immediately upon reaching +40 pts!
          2. Fixed stop-loss (-40 pts): Exit immediately upon hitting -40 pts, marks stage2_stopped_today = True.
          3. Cost-to-Cost trigger: once spot moves +40 pts in favor, move SL to entry price (+9 pt buffer for fees).
          4. No premature candle trailing before +40 pts!
          5. Volatility Exit: If candle expands >= 100 pts or UBB curls FLAT -> exit immediately.

        Returns: (should_exit, exit_reason, updated_sl_spot)
        """
        strat = self.cfg.get("strategy", {})
        ctc_trigger = float(strat.get("cost_to_cost_trigger_pts", 40.0))
        vol_candle_pts = float(strat.get("volatility_candle_pts", 100.0))
        slope_candles = int(strat.get("ubb_slope_min_candles", 2))
        slope_tol = float(strat.get("ubb_slope_tol", 1.5))

        if df_15m is None and df_5m is not None:
            df_15m = df_5m
        cand = last_closed_candle_15m or last_closed_candle_5m

        # Update highest spot reached
        if current_spot > pos.highest_spot:
            pos.highest_spot = current_spot

        new_sl = pos.spot_sl

        # A. Volatility check 1: Candle range >= 100 points (immediate exit on huge expansion)
        if cand is not None:
            c_close = cand.get("c", 0.0)
            c_low = cand.get("l", 0.0)
            c_high = cand.get("h", 0.0)
            if (c_high - c_low) >= vol_candle_pts:
                log.warning("🚨 Trade %s Volatility Exit: Candle range %.1f >= %.1f pts!",
                            pos.trade_id, c_high - c_low, vol_candle_pts)
                return True, f"Volatility Exit: Candle range {c_high - c_low:.1f} >= {vol_candle_pts:.1f} pts", None

        # B. Cost-to-Cost Trigger (+40 pts move in favor)
        # Locks in entry + 9.0 pts buffer to cover round-trip fees (₹400)
        brokerage_buffer_pts = float(strat.get("cost_to_cost_brokerage_buffer_pts", 9.0))
        if not pos.cost_to_cost_active and (pos.highest_spot - pos.entry_spot >= ctc_trigger):
            pos.cost_to_cost_active = True
            new_sl = max(new_sl, pos.entry_spot + brokerage_buffer_pts)
            log.info("🎯 Trade %s reached +40 pts! Cost-to-Cost Trigger activated (SL locked at %.1f)",
                     pos.trade_id, new_sl)

        # C. Candle Trail (ONLY once +40 pts / CTC activated!)
        # Do NOT trail candle lows before reaching +40 pts!
        if pos.cost_to_cost_active and cand is not None:
            c_close = cand.get("c", 0.0)
            c_low = cand.get("l", 0.0)
            if c_close > pos.entry_spot and c_low > new_sl:
                new_sl = max(new_sl, c_low)
                log.info("📈 Trade %s Candle Trail: Trailed SL to confirming candle low %.1f", pos.trade_id, new_sl)

        # D. Volatility check 2: UBB curls FLAT
        if df_15m is not None and not df_15m.empty and len(df_15m) >= 20:
            f15 = prepare_frame(df_15m, self.cfg)
            current_ubb_slope = band_slope(f15["ubb"], candles=slope_candles, tol=slope_tol)
            if current_ubb_slope == "FLAT":
                log.warning("🚨 Trade %s Volatility Exit: Upper Bollinger Band curled FLAT!", pos.trade_id)
                return True, "Volatility Exit: Upper Bollinger Band curled FLAT", None

        # D. Fixed Target Hit (+40 pts)
        if current_spot >= pos.spot_target:
            log.info("🎉 Trade %s Target +40 pts Hit! Spot %.1f >= Target %.1f", pos.trade_id, current_spot, pos.spot_target)
            return True, "Target Hit (+40 pts)", None

        # E. Fixed Stop-Loss Hit (-40 pts)
        if current_spot <= new_sl:
            is_initial_sl = (new_sl == pos.entry_spot - float(strat.get("fixed_sl_pts", 40.0)))
            if is_initial_sl:
                self.stage2_stops_count += 1
                stop_limit = int(self.cfg.get("risk", {}).get("stage2_daily_stop_limit", 1))
                if self.stage2_stops_count >= stop_limit:
                    self.stage2_stopped_today = True
                log.warning("🛑 Trade %s Initial Stop-Loss (-40 pts) Hit (%d/%d stops today)! Setup #1 disabled for rest of day.",
                            pos.trade_id, self.stage2_stops_count, stop_limit)
                return True, "Initial Stop-Loss Hit (-40 pts)", None
            else:
                log.info("🛡️ Trade %s Trailing/CTC Stop Hit at %.1f", pos.trade_id, new_sl)
                return True, f"Trailing/CTC Stop Hit (SL {new_sl:.1f})", None

        # If SL was adjusted by CTC or Candle Trail, return updated SL
        if new_sl != pos.spot_sl:
            return False, None, new_sl

        return False, None, None
