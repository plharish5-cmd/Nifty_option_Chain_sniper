"""
algo_execution_engine/strategy_1h_80ema.py — Flagship 1-Hour 80-EMA Touch Strategy Engine.

Strategy Rules:
  1. Timeframe Resampling & Evaluation:
     - Aggregates continuous 1-minute intraday candles into 1-Hour candles aligned strictly
       to NSE market sessions (09:15-10:15, 10:15-11:15, 11:15-12:15, 12:15-13:15, 13:15-14:15, 14:15-15:15, 15:15-15:30).
     - Tracks the 80 EMA on the 1-hour timeframe (80 EMA on 1H).
  2. Setup Rules & Triggers:
     - Support Bounce (Long): When a 1-hour candle touches or tests the 1H 80-EMA from above (acting as support)
       and bounces, fire a Long signal targeting +40 points with a -40 point stop-loss.
       Lot Size = 65, exact statutory charges deducted.
     - Resistance Rejection (Short): When a 1-hour candle touches or tests the 1H 80-EMA from below (acting as resistance)
       and gets rejected, fire a Short signal targeting +40 points with a -40 point stop-loss.
     - Gap Filter Integration: Skip long entries on days opening with a > +50 pt gap-up unless a clean
       pullback/bounce to the 80-EMA occurs. Skip short entries on days opening with a < -50 pt gap-down
       unless a clean pullback/rejection to the 80-EMA occurs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
log = logging.getLogger("algo.strategy_1h")


def load_config() -> Dict[str, Any]:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read config.json: %s. Using default 1H 80-EMA config.", e)
    return {}


@dataclass
class Strategy1hSignal:
    side: str  # "BUY" (Long) or "SELL" (Short)
    setup_name: str  # "1H 80-EMA Support Bounce" or "1H 80-EMA Resistance Rejection"
    spot: float
    ema80: float
    target_spot: float
    sl_spot: float
    lot_size: int = 65
    is_valid: bool = False
    rejection_reason: Optional[str] = None
    timestamp: int = 0
    time_str: str = ""
    gap_pts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 1-HOUR RESAMPLING & 80-EMA CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def build_1h_data(df_1m: pd.DataFrame, cfg: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    Resamples 1m continuous OHLCV candles into session-aligned 1-Hour candles:
      09:15-10:15, 10:15-11:15, 11:15-12:15, 12:15-13:15, 13:15-14:15, 14:15-15:15, 15:15-15:30.
    Computes 80-period EMA on 1-hour close prices with zero lookahead bias.
    """
    if df_1m.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema80"])

    cfg = cfg or load_config()
    strat_cfg = cfg.get("strategy_1h_80ema", {})
    ema_period = int(strat_cfg.get("ema_period", 80))

    df = df_1m.copy()
    if "datetime" not in df.columns:
        df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    else:
        df["datetime"] = pd.to_datetime(df["datetime"])
        if df["datetime"].dt.tz is None:
            df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")

    df["session_date"] = df["datetime"].dt.date
    df["time_str"] = df["datetime"].dt.strftime("%H:%M:%S")

    # Filter to intraday market hours 09:15 to 15:30 IST
    df = df[(df["time_str"] >= "09:15:00") & (df["time_str"] <= "15:30:00")].copy()
    if df.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema80"])

    # Compute minute offset from session open 09:15 (0 to 374)
    session_minutes = (df["datetime"].dt.hour - 9) * 60 + (df["datetime"].dt.minute - 15)
    df["hour_bucket"] = session_minutes // 60
    # The last 15 minutes (15:15 to 15:30) fall in bucket 6
    df["hour_bucket"] = df["hour_bucket"].clip(0, 6)

    # Group by session_date and hour_bucket
    grouped = df.groupby(["session_date", "hour_bucket"], sort=False)
    agg_df = grouped.agg(
        ts=("ts", "first"),
        datetime=("datetime", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)

    agg_df = agg_df.sort_values("ts").reset_index(drop=True)

    # Calculate 80-period EMA on 1-hour close
    agg_df["ema80"] = agg_df["close"].ewm(span=ema_period, adjust=False).mean()

    return agg_df


# ─────────────────────────────────────────────────────────────────────────────
# 2. 1-HOUR 80-EMA TOUCH STRATEGY EVALUATOR
# ─────────────────────────────────────────────────────────────────────────────

class Strategy1h80EmaEvaluator:
    """Evaluates 1-Hour 80-EMA Support Bounce (Long) and Resistance Rejection (Short) rules."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_config()
        self.strat_cfg = self.cfg.get("strategy_1h_80ema", {})

        self.ema_period = int(self.strat_cfg.get("ema_period", 80))
        self.touch_proximity_pts = float(self.strat_cfg.get("touch_proximity_pts", 3.0))
        self.fixed_sl_pts = float(self.strat_cfg.get("fixed_sl_pts", 40.0))
        self.target_pts = float(self.strat_cfg.get("target_pts", 40.0))
        self.cost_to_cost_trigger_pts = float(self.strat_cfg.get("cost_to_cost_trigger_pts", 40.0))
        self.cost_to_cost_brokerage_buffer_pts = float(self.strat_cfg.get("cost_to_cost_brokerage_buffer_pts", 9.0))
        self.lot_size = int(self.strat_cfg.get("lot_size", 65))
        self.gap_up_max_pts = float(self.strat_cfg.get("gap_up_max_pts", 50.0))
        self.gap_down_max_pts = float(self.strat_cfg.get("gap_down_max_pts", -50.0))

        # Daily state tracking
        self.daily_trades_count = 0
        self.stage_stopped_today = False
        self.last_signal_time: Optional[str] = None
        self.last_candle_1h_ts: int = 0

    def reload_config(self) -> None:
        self.cfg = load_config()
        self.strat_cfg = self.cfg.get("strategy_1h_80ema", {})
        self.ema_period = int(self.strat_cfg.get("ema_period", 80))
        self.touch_proximity_pts = float(self.strat_cfg.get("touch_proximity_pts", 3.0))
        self.fixed_sl_pts = float(self.strat_cfg.get("fixed_sl_pts", 40.0))
        self.target_pts = float(self.strat_cfg.get("target_pts", 40.0))
        self.lot_size = int(self.strat_cfg.get("lot_size", 65))

    def reset_day(self) -> None:
        self.daily_trades_count = 0
        self.stage_stopped_today = False
        self.last_signal_time = None
        self.last_candle_1h_ts = 0

    def check_1h_entry_signal(
        self,
        df_1h: pd.DataFrame,
        current_spot: float,
        current_ts: int,
        current_time: Optional[dtime] = None,
        gap_pts: Optional[float] = None,
        current_candle_low: Optional[float] = None,
        current_candle_high: Optional[float] = None,
    ) -> Strategy1hSignal:
        """
        Evaluates 1-Hour 80-EMA rules:
          1. Support Bounce (Long): Candle touches/tests 80-EMA from above and bounces.
          2. Resistance Rejection (Short): Candle touches/tests 80-EMA from below and gets rejected.
          3. Gap filter: Skip long on gap > +50 pts unless clean 80-EMA pullback is verified;
             skip short on gap < -50 pts unless clean 80-EMA pullback is verified.
        """
        # Time filter: No entry after 15:00 IST
        if current_time is not None and current_time >= dtime(15, 0):
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 80-EMA Touch",
                spot=current_spot,
                ema80=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="Time Filter: No new entries after 15:00 IST",
                timestamp=current_ts,
            )

        if self.stage_stopped_today:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 80-EMA Touch",
                spot=current_spot,
                ema80=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="Daily Stop Limit: Strategy disabled for today after SL hit",
                timestamp=current_ts,
            )

        if df_1h.empty or len(df_1h) < 1:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 80-EMA Touch",
                spot=current_spot,
                ema80=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="Insufficient 1-hour candle data",
                timestamp=current_ts,
            )

        latest_bar = df_1h.iloc[-1]
        ema80 = float(latest_bar.get("ema80", 0.0))
        if ema80 <= 0.0:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 80-EMA Touch",
                spot=current_spot,
                ema80=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="1H 80-EMA not calculated",
                timestamp=current_ts,
            )

        bar_low = current_candle_low if current_candle_low is not None else float(latest_bar["low"])
        bar_high = current_candle_high if current_candle_high is not None else float(latest_bar["high"])
        bar_open = float(latest_bar["open"])

        # Determine if prior candle was above or below 80-EMA
        prior_close = float(df_1h.iloc[-2]["close"]) if len(df_1h) >= 2 else bar_open
        market_was_above = prior_close >= (ema80 - self.touch_proximity_pts)
        market_was_below = prior_close <= (ema80 + self.touch_proximity_pts)

        # ── GAP RULE PRE-CHECKS ──────────────────────────────────────────────
        # Gap Filter Check: If day opened with huge gap, skip unless candle low/high touched 80-EMA as pullback
        if gap_pts is not None and gap_pts > self.gap_up_max_pts:
            if bar_low > (ema80 + self.touch_proximity_pts):
                return Strategy1hSignal(
                    side="BUY",
                    setup_name="1H 80-EMA Support Bounce",
                    spot=current_spot,
                    ema80=ema80,
                    target_spot=current_spot + self.target_pts,
                    sl_spot=current_spot - self.fixed_sl_pts,
                    lot_size=self.lot_size,
                    is_valid=False,
                    rejection_reason=f"Gap Rule: +{gap_pts:.1f} pt gap-up without clean 80-EMA pullback touch (Low: {bar_low:.1f}, 80-EMA: {ema80:.1f})",
                    timestamp=current_ts,
                    gap_pts=gap_pts,
                )

        if gap_pts is not None and gap_pts < self.gap_down_max_pts:
            if bar_high < (ema80 - self.touch_proximity_pts):
                return Strategy1hSignal(
                    side="SELL",
                    setup_name="1H 80-EMA Resistance Rejection",
                    spot=current_spot,
                    ema80=ema80,
                    target_spot=current_spot - self.target_pts,
                    sl_spot=current_spot + self.fixed_sl_pts,
                    lot_size=self.lot_size,
                    is_valid=False,
                    rejection_reason=f"Gap Rule: {gap_pts:.1f} pt gap-down without clean 80-EMA pullback touch (High: {bar_high:.1f}, 80-EMA: {ema80:.1f})",
                    timestamp=current_ts,
                    gap_pts=gap_pts,
                )

        # ── SETUP A: SUPPORT BOUNCE (LONG) ───────────────────────────────────
        # Candle tests 80-EMA from above (acting as support)
        tested_support = (bar_low <= (ema80 + self.touch_proximity_pts)) and (bar_high >= (ema80 - self.touch_proximity_pts))
        bounced_up = current_spot >= ema80 or (current_spot >= bar_low + 2.0)

        if market_was_above and tested_support and bounced_up:
            target = current_spot + self.target_pts
            sl = current_spot - self.fixed_sl_pts
            return Strategy1hSignal(
                side="BUY",
                setup_name="1H 80-EMA Support Bounce",
                spot=current_spot,
                ema80=ema80,
                target_spot=target,
                sl_spot=sl,
                lot_size=self.lot_size,
                is_valid=True,
                rejection_reason=None,
                timestamp=current_ts,
                time_str=datetime.fromtimestamp(current_ts).strftime("%H:%M:%S") if current_ts else "",
                gap_pts=gap_pts,
            )

        # ── SETUP B: RESISTANCE REJECTION (SHORT) ─────────────────────────────
        # Candle tests 80-EMA from below (acting as resistance)
        tested_resistance = (bar_high >= (ema80 - self.touch_proximity_pts)) and (bar_low <= (ema80 + self.touch_proximity_pts))
        rejected_down = current_spot <= ema80 or (current_spot <= bar_high - 2.0)

        if market_was_below and tested_resistance and rejected_down:
            target = current_spot - self.target_pts
            sl = current_spot + self.fixed_sl_pts
            return Strategy1hSignal(
                side="SELL",
                setup_name="1H 80-EMA Resistance Rejection",
                spot=current_spot,
                ema80=ema80,
                target_spot=target,
                sl_spot=sl,
                lot_size=self.lot_size,
                is_valid=True,
                rejection_reason=None,
                timestamp=current_ts,
                time_str=datetime.fromtimestamp(current_ts).strftime("%H:%M:%S") if current_ts else "",
                gap_pts=gap_pts,
            )

        dist = current_spot - ema80
        return Strategy1hSignal(
            side="NONE",
            setup_name="1H 80-EMA Touch",
            spot=current_spot,
            ema80=ema80,
            target_spot=0.0,
            sl_spot=0.0,
            lot_size=self.lot_size,
            is_valid=False,
            rejection_reason=f"No 80-EMA Touch (Distance: {dist:+.1f} pts, Proximity: {self.touch_proximity_pts} pts)",
            timestamp=current_ts,
            gap_pts=gap_pts,
        )

    # ── 3. POSITION MONITORING & FIXED 40/40 RISK BRACKET ─────────────────────
    def evaluate_active_position(
        self,
        pos: Any,
        current_spot: float,
        current_option_ltp: float,
        df_1h: Optional[pd.DataFrame] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        """
        Monitors active 1H 80-EMA position:
          - Target (+40 pts): Exit immediately.
          - Stop Loss (-40 pts): Exit immediately, marks stage_stopped_today = True.
          - Cost-to-Cost trigger: once spot moves +40 pts in favor, lock SL to entry price (+ brokerage buffer).
        Returns: (should_exit, exit_reason, updated_sl_spot)
        """
        is_short = getattr(pos, "option_type", "") == "PE" or getattr(pos, "side", "") in ("SELL", "SHORT")
        is_long = not is_short
        new_sl = pos.spot_sl

        # Update peak excursion
        if is_long:
            if current_spot > pos.highest_spot:
                pos.highest_spot = current_spot
        else:
            lowest = getattr(pos, "lowest_spot", pos.entry_spot)
            if current_spot < lowest:
                pos.lowest_spot = current_spot

        # A. Cost-to-Cost Trigger (+40 pts move in favor)
        if not pos.cost_to_cost_active:
            if is_long and (pos.highest_spot - pos.entry_spot >= self.cost_to_cost_trigger_pts):
                pos.cost_to_cost_active = True
                new_sl = max(new_sl, pos.entry_spot + self.cost_to_cost_brokerage_buffer_pts)
                log.info("🎯 1H Trade %s reached +40 pts! Cost-to-Cost activated (SL %.1f)", pos.trade_id, new_sl)
            elif not is_long:
                lowest = getattr(pos, "lowest_spot", pos.entry_spot)
                if (pos.entry_spot - lowest >= self.cost_to_cost_trigger_pts):
                    pos.cost_to_cost_active = True
                    new_sl = min(new_sl, pos.entry_spot - self.cost_to_cost_brokerage_buffer_pts)
                    log.info("🎯 1H Short Trade %s reached +40 pts! Cost-to-Cost activated (SL %.1f)", pos.trade_id, new_sl)

        # B. Target Hit (+40 pts)
        if is_long and current_spot >= pos.spot_target:
            log.info("🎉 1H Long Target +40 pts Hit! Spot %.1f >= Target %.1f", current_spot, pos.spot_target)
            return True, "1H Target Hit (+40 pts)", None
        elif not is_long and current_spot <= pos.spot_target:
            log.info("🎉 1H Short Target +40 pts Hit! Spot %.1f <= Target %.1f", current_spot, pos.spot_target)
            return True, "1H Short Target Hit (+40 pts)", None

        # C. Stop Loss Hit (-40 pts)
        if is_long and current_spot <= new_sl:
            self.stage_stopped_today = True
            log.warning("🛑 1H Long Stop-Loss Hit! Spot %.1f <= SL %.1f", current_spot, new_sl)
            return True, "1H Stop-Loss Hit (-40 pts)", None
        elif not is_long and current_spot >= new_sl:
            self.stage_stopped_today = True
            log.warning("🛑 1H Short Stop-Loss Hit! Spot %.1f >= SL %.1f", current_spot, new_sl)
            return True, "1H Short Stop-Loss Hit (-40 pts)", None

        return False, None, new_sl

    def get_state(self, df_1h: pd.DataFrame, current_spot: float) -> Dict[str, Any]:
        """Telemetry representation for engine and API endpoints."""
        ema80 = float(df_1h.iloc[-1].get("ema80", 0.0)) if not df_1h.empty else 0.0
        dist = round(current_spot - ema80, 2) if ema80 > 0.0 else 0.0

        if abs(dist) <= self.touch_proximity_pts:
            market_state = "TESTING_80EMA_TOUCH"
        elif dist > 0:
            market_state = "ABOVE_80EMA_SUPPORT"
        else:
            market_state = "BELOW_80EMA_RESISTANCE"

        return {
            "strategy": "Flagship 1-Hour 80-EMA Touch",
            "timeframe": "1h",
            "ema80": round(ema80, 2),
            "spot": round(current_spot, 2),
            "dist_to_ema80_pts": dist,
            "market_state": market_state,
            "touch_proximity_pts": self.touch_proximity_pts,
            "lot_size": self.lot_size,
            "stage_stopped_today": self.stage_stopped_today,
            "daily_trades_count": self.daily_trades_count,
        }
