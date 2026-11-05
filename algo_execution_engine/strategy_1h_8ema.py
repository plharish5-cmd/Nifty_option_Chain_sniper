"""
algo_execution_engine/strategy_1h_8ema.py — 1-Hour 8-EMA Touch Strategy Engine (Silent Context / Alert-Only).

Statistical Context (claude/MA_Touch_Verdict.md):
  - Hourly first-touch measures +1.0 sd with direction not informative.
  - Demoted to a silent context marker / alert-only.
  - Strictly ALERT-ONLY. Never places orders on Dhan.
  - Span: 8-period EMA on 1-hour session-aligned candles (09:15 to 15:30 IST).
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
            log.warning("Could not read config.json: %s. Using default 1H 8-EMA config.", e)
    return {}


@dataclass
class Strategy1hSignal:
    side: str  # "BUY" (Long) or "SELL" (Short)
    setup_name: str  # "1H 8-EMA Support Bounce" or "1H 8-EMA Resistance Rejection"
    spot: float
    ema8: float
    target_spot: float
    sl_spot: float
    lot_size: int = 65
    is_valid: bool = False
    rejection_reason: Optional[str] = None
    timestamp: int = 0
    time_str: str = ""
    gap_pts: Optional[float] = None

    @property
    def ema80(self) -> float:
        """Backward-compatible property for legacy consumers."""
        return self.ema8

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ema80"] = self.ema8
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 1. 1-HOUR RESAMPLING & 8-EMA CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

def build_1h_data(df_1m: pd.DataFrame, cfg: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """
    Resamples 1m continuous OHLCV candles into session-aligned 1-Hour candles:
      09:15-10:15, 10:15-11:15, 11:15-12:15, 12:15-13:15, 13:15-14:15, 14:15-15:15, 15:15-15:30.
    Computes 8-period EMA on 1-hour close prices with zero lookahead bias.
    """
    if df_1m.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema8", "ema80"])

    cfg = cfg or load_config()
    strat_cfg = cfg.get("strategy_1h_8ema") or cfg.get("strategy_1h_80ema") or {}
    ema_period = int(strat_cfg.get("ema_period", 8))

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
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema8", "ema80"])

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

    # Calculate 8-period EMA on 1-hour close
    agg_df["ema8"] = agg_df["close"].ewm(span=ema_period, adjust=False).mean()
    agg_df["ema80"] = agg_df["ema8"]  # Backward compatibility

    return agg_df


# ─────────────────────────────────────────────────────────────────────────────
# 2. 1-HOUR 8-EMA TOUCH STRATEGY EVALUATOR
# ─────────────────────────────────────────────────────────────────────────────

class Strategy1h8EmaEvaluator:
    """Evaluates 1-Hour 8-EMA Support Bounce (Long) and Resistance Rejection (Short) rules (Alert-Only)."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_config()
        self.strat_cfg = self.cfg.get("strategy_1h_8ema") or self.cfg.get("strategy_1h_80ema") or {}

        self.ema_period = int(self.strat_cfg.get("ema_period", 8))
        self.touch_proximity_pts = float(self.strat_cfg.get("touch_proximity_pts", 3.0))
        self.fixed_sl_pts = float(self.strat_cfg.get("fixed_sl_pts", 40.0))
        self.target_pts = float(self.strat_cfg.get("target_pts", 40.0))
        self.cost_to_cost_trigger_pts = float(self.strat_cfg.get("cost_to_cost_trigger_pts", 40.0))
        self.cost_to_cost_brokerage_buffer_pts = float(self.strat_cfg.get("cost_to_cost_brokerage_buffer_pts", 9.0))
        self.lot_size = int(self.strat_cfg.get("lot_size", 65))
        self.gap_up_max_pts = float(self.strat_cfg.get("gap_up_max_pts", 50.0))
        self.gap_down_max_pts = float(self.strat_cfg.get("gap_down_max_pts", -50.0))
        self.no_entry_after = self.strat_cfg.get("no_entry_after", "15:00")

        self.stage_stopped_today: bool = False
        self.daily_trades_count: int = 0

    def reset_day(self) -> None:
        self.stage_stopped_today = False
        self.daily_trades_count = 0

    def check_1h_entry_signal(
        self,
        df_1h: pd.DataFrame,
        current_spot: float,
        current_ts: int,
        current_time: Optional[dtime] = None,
        gap_pts: float = 0.0,
        current_candle_low: Optional[float] = None,
        current_candle_high: Optional[float] = None,
    ) -> Strategy1hSignal:
        """
        Evaluates 1H 8-EMA Support Bounce (Long) and Resistance Rejection (Short).
        Returns Strategy1hSignal.
        """
        if current_time is None:
            current_time = datetime.fromtimestamp(current_ts).time()

        time_str = current_time.strftime("%H:%M:%S")

        # Time Cut-off: No new entries after 15:00
        cutoff_parts = [int(p) for p in self.no_entry_after.split(":")]
        cutoff_time = dtime(cutoff_parts[0], cutoff_parts[1])
        if current_time > cutoff_time:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 8-EMA Touch",
                spot=current_spot,
                ema8=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason=f"Time Cut-off: {time_str} > {self.no_entry_after}",
                timestamp=current_ts,
                gap_pts=gap_pts,
            )

        if self.stage_stopped_today:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 8-EMA Touch",
                spot=current_spot,
                ema8=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="Daily Stop Hit: 1H strategy halted for rest of session",
                timestamp=current_ts,
                gap_pts=gap_pts,
            )

        if df_1h.empty or len(df_1h) < 1:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 8-EMA Touch",
                spot=current_spot,
                ema8=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="Insufficient 1H candle history",
                timestamp=current_ts,
                gap_pts=gap_pts,
            )

        latest_bar = df_1h.iloc[-1].to_dict()
        bar_high = current_candle_high if current_candle_high is not None else float(latest_bar["high"])
        bar_low = current_candle_low if current_candle_low is not None else float(latest_bar["low"])

        ema8 = float(latest_bar.get("ema8") or latest_bar.get("ema80", 0.0))
        if ema8 <= 0.0:
            return Strategy1hSignal(
                side="NONE",
                setup_name="1H 8-EMA Touch",
                spot=current_spot,
                ema8=0.0,
                target_spot=0.0,
                sl_spot=0.0,
                lot_size=self.lot_size,
                is_valid=False,
                rejection_reason="1H 8-EMA uncalculated or zero",
                timestamp=current_ts,
                gap_pts=gap_pts,
            )

        prior_close = float(df_1h.iloc[-2]["close"]) if len(df_1h) >= 2 else float(latest_bar["open"])

        # Market approaching from above vs below
        market_was_above = prior_close >= (ema8 - self.touch_proximity_pts)
        market_was_below = prior_close <= (ema8 + self.touch_proximity_pts)

        # 1. LONG: Support Bounce
        if market_was_above:
            if gap_pts > self.gap_up_max_pts:
                if bar_low > (ema8 + self.touch_proximity_pts):
                    return Strategy1hSignal(
                        side="BUY",
                        setup_name="1H 8-EMA Support Bounce",
                        spot=current_spot,
                        ema8=ema8,
                        target_spot=round(current_spot + self.target_pts, 1),
                        sl_spot=round(current_spot - self.fixed_sl_pts, 1),
                        lot_size=self.lot_size,
                        is_valid=False,
                        rejection_reason=f"Gap Rule: +{gap_pts:.1f} pt gap-up without clean 8-EMA pullback touch (Low: {bar_low:.1f}, 8-EMA: {ema8:.1f})",
                        timestamp=current_ts,
                        gap_pts=gap_pts,
                    )

            tested_support = (bar_low <= (ema8 + self.touch_proximity_pts)) and (bar_high >= (ema8 - self.touch_proximity_pts))
            bounced_up = current_spot >= ema8 or (current_spot >= bar_low + 2.0)
            if tested_support and bounced_up:
                self.daily_trades_count += 1
                return Strategy1hSignal(
                    side="BUY",
                    setup_name="1H 8-EMA Support Bounce",
                    spot=current_spot,
                    ema8=ema8,
                    target_spot=round(current_spot + self.target_pts, 1),
                    sl_spot=round(current_spot - self.fixed_sl_pts, 1),
                    lot_size=self.lot_size,
                    is_valid=True,
                    rejection_reason=None,
                    timestamp=current_ts,
                    gap_pts=gap_pts,
                )

        # 2. SHORT: Resistance Rejection
        if market_was_below:
            if gap_pts < self.gap_down_max_pts:
                if bar_high < (ema8 - self.touch_proximity_pts):
                    return Strategy1hSignal(
                        side="SELL",
                        setup_name="1H 8-EMA Resistance Rejection",
                        spot=current_spot,
                        ema8=ema8,
                        target_spot=round(current_spot - self.target_pts, 1),
                        sl_spot=round(current_spot + self.fixed_sl_pts, 1),
                        lot_size=self.lot_size,
                        is_valid=False,
                        rejection_reason=f"Gap Rule: {gap_pts:.1f} pt gap-down without clean 8-EMA pullback touch (High: {bar_high:.1f}, 8-EMA: {ema8:.1f})",
                        timestamp=current_ts,
                        gap_pts=gap_pts,
                    )

            tested_resistance = (bar_high >= (ema8 - self.touch_proximity_pts)) and (bar_low <= (ema8 + self.touch_proximity_pts))
            rejected_down = current_spot <= ema8 or (current_spot <= bar_high - 2.0)
            if tested_resistance and rejected_down:
                self.daily_trades_count += 1
                return Strategy1hSignal(
                    side="SELL",
                    setup_name="1H 8-EMA Resistance Rejection",
                    spot=current_spot,
                    ema8=ema8,
                    target_spot=round(current_spot - self.target_pts, 1),
                    sl_spot=round(current_spot + self.fixed_sl_pts, 1),
                    lot_size=self.lot_size,
                    is_valid=True,
                    rejection_reason=None,
                    timestamp=current_ts,
                    gap_pts=gap_pts,
                )

        dist = current_spot - ema8
        return Strategy1hSignal(
            side="NONE",
            setup_name="1H 8-EMA Touch",
            spot=current_spot,
            ema8=ema8,
            target_spot=0.0,
            sl_spot=0.0,
            lot_size=self.lot_size,
            is_valid=False,
            rejection_reason=f"No 8-EMA Touch (Distance: {dist:+.1f} pts, Proximity: {self.touch_proximity_pts} pts)",
            timestamp=current_ts,
            gap_pts=gap_pts,
        )

    def evaluate_active_position(
        self,
        pos: Any,
        current_spot: float,
        current_option_ltp: float,
        df_1h: Optional[pd.DataFrame] = None,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        is_short = getattr(pos, "option_type", "") == "PE" or getattr(pos, "side", "") in ("SELL", "SHORT")
        is_long = not is_short
        new_sl = pos.spot_sl

        # Peak excursion
        if is_long:
            if current_spot > pos.highest_spot:
                pos.highest_spot = current_spot
        else:
            lowest = getattr(pos, "lowest_spot", pos.entry_spot)
            if current_spot < lowest:
                pos.lowest_spot = current_spot

        # Cost-to-Cost Trigger (+40 pts move in favor)
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

        # Target Hit (+40 pts)
        if is_long and current_spot >= pos.spot_target:
            log.info("🎉 1H Long Target +40 pts Hit! Spot %.1f >= Target %.1f", current_spot, pos.spot_target)
            return True, "1H Target Hit (+40 pts)", None
        elif not is_long and current_spot <= pos.spot_target:
            log.info("🎉 1H Short Target +40 pts Hit! Spot %.1f <= Target %.1f", current_spot, pos.spot_target)
            return True, "1H Short Target Hit (+40 pts)", None

        # Stop Loss Hit (-40 pts)
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
        ema8 = float(df_1h.iloc[-1].get("ema8") or df_1h.iloc[-1].get("ema80", 0.0)) if not df_1h.empty else 0.0
        dist = round(current_spot - ema8, 2) if ema8 > 0.0 else 0.0

        if abs(dist) <= self.touch_proximity_pts:
            market_state = "TESTING_8EMA_TOUCH"
        elif dist > 0:
            market_state = "ABOVE_8EMA_SUPPORT"
        else:
            market_state = "BELOW_8EMA_RESISTANCE"

        return {
            "strategy": "Flagship 1-Hour 8-EMA Touch (Alert-Only)",
            "execution_policy": "ALERT_ONLY (Silent Context Marker)",
            "timeframe": "1h",
            "ema8": round(ema8, 2),
            "ema80": round(ema8, 2),
            "spot": round(current_spot, 2),
            "dist_to_ema8_pts": dist,
            "market_state": market_state,
            "touch_proximity_pts": self.touch_proximity_pts,
            "lot_size": self.lot_size,
            "stage_stopped_today": self.stage_stopped_today,
            "daily_trades_count": self.daily_trades_count,
        }


# Aliases for backward compatibility
Strategy1h80EmaEvaluator = Strategy1h8EmaEvaluator
