"""1-Hour 8-EMA Touch Strategy Engine (Active Live Strategy, Frozen Closed-Bar EMA)."""
from __future__ import annotations
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import pandas as pd

log = logging.getLogger("algo.strategy_1h")

def load_config() -> Dict[str, Any]:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read config.json: %s", e)
    return {}

@dataclass
class Strategy1hSignal:
    side: str
    setup_name: str
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def build_1h_data(df_1m: pd.DataFrame, cfg: Optional[Dict[str, Any]] = None, as_of_ts=None) -> pd.DataFrame:
    if df_1m.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema8", "is_closed"])
    cfg = cfg or load_config()
    strat_cfg = cfg.get("strategy_1h_8ema") or {}
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
    df = df[(df["time_str"] >= "09:15:00") & (df["time_str"] <= "15:30:00")].copy()
    if df.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema8", "is_closed"])

    session_minutes = (df["datetime"].dt.hour - 9) * 60 + (df["datetime"].dt.minute - 15)
    df["hour_bucket"] = session_minutes // 60
    df = df[df["hour_bucket"] < 6].copy()
    if df.empty:
        return pd.DataFrame(columns=["datetime", "ts", "open", "high", "low", "close", "volume", "ema8", "is_closed"])

    grouped = df.groupby(["session_date", "hour_bucket"], sort=False)
    agg_df = grouped.agg(
        ts=("ts", "first"),
        datetime=("datetime", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True).sort_values("ts").reset_index(drop=True)

    # Hour completeness requires all60 distinct minute observations and an elapsed end.
    available=int(as_of_ts) if as_of_ts is not None else int(df['ts'].max())+60
    complete=[]
    for (_,bucket),part in grouped:
        first=part['datetime'].iloc[0].normalize()+pd.Timedelta(hours=9,minutes=15)+pd.Timedelta(hours=int(bucket))
        end=int(first.timestamp())+3600
        expected=set(range(int(first.timestamp()),end,60))
        complete.append(expected.issubset(set(int(t)for t in part['ts'])) and available>=end)
    agg_df['is_closed']=complete
    closed_mask = agg_df["is_closed"]
    agg_df["ema8"] = 0.0
    if closed_mask.any():
        agg_df.loc[closed_mask, "ema8"] = agg_df.loc[closed_mask, "close"].ewm(span=ema_period, adjust=False).mean()
        agg_df["ema8"] = agg_df["ema8"].replace(0.0, pd.NA).ffill().fillna(0.0)
    return agg_df

class Strategy1h8EmaEvaluator:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_config()
        self.reload_config()
        self.stage_stopped_today: bool = False
        self.daily_trades_count: int = 0

    def reload_config(self) -> None:
        self.strat_cfg = self.cfg.get("strategy_1h_8ema") or {}
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
        if current_time is None:
            current_time = datetime.fromtimestamp(current_ts).time()
        time_str = current_time.strftime("%H:%M:%S")
        cutoff_parts = [int(p) for p in self.no_entry_after.split(":")]
        if current_time > dtime(cutoff_parts[0], cutoff_parts[1]):
            return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, 0.0, 0.0, 0.0, self.lot_size, False, f"Time Cut-off: {time_str}", current_ts, gap_pts=gap_pts)
        if self.stage_stopped_today:
            return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, 0.0, 0.0, 0.0, self.lot_size, False, "Daily Stop Hit: 1H halted", current_ts, gap_pts=gap_pts)
        if df_1h.empty or len(df_1h) < 9:
            return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, 0.0, 0.0, 0.0, self.lot_size, False, f"Insufficient 1H history ({len(df_1h)}/9)", current_ts, gap_pts=gap_pts)

        if 'is_closed' in df_1h.columns:
            closed=df_1h[df_1h['is_closed']]
            if len(closed)<8 or bool(df_1h.iloc[-1]['is_closed']):
                return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, 0., 0., 0., self.lot_size, False, "Need8closedhours_and_forminghour", current_ts)
            closed_bar=closed.iloc[-1].to_dict()
        else:
            closed_bar = df_1h.iloc[-2].to_dict()
        forming_bar = df_1h.iloc[-1].to_dict()
        ema8 = float(closed_bar.get("ema8", 0.0))
        prior_close = float(closed_bar.get("close", 0.0))
        bar_high = current_candle_high if current_candle_high is not None else float(forming_bar["high"])
        bar_low = current_candle_low if current_candle_low is not None else float(forming_bar["low"])

        if ema8 <= 0.0:
            return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, 0.0, 0.0, 0.0, self.lot_size, False, "1H 8-EMA zero", current_ts, gap_pts=gap_pts)

        if prior_close >= ema8:
            if gap_pts > self.gap_up_max_pts and bar_low > (ema8 + self.touch_proximity_pts):
                return Strategy1hSignal("BUY", "1H 8-EMA Support Bounce", current_spot, ema8, round(current_spot + self.target_pts, 1), round(current_spot - self.fixed_sl_pts, 1), self.lot_size, False, f"Gap Rule: +{gap_pts:.1f} pt gap-up without pullback", current_ts, gap_pts=gap_pts)
            tested_support = (bar_low <= (ema8 + self.touch_proximity_pts)) and (bar_high >= (ema8 - self.touch_proximity_pts))
            bounced_up = (current_spot >= ema8) and ((current_spot - bar_low) >= 8.0)
            if tested_support and bounced_up:
                return Strategy1hSignal("BUY", "1H 8-EMA Support Bounce", current_spot, ema8, round(current_spot + self.target_pts, 1), round(current_spot - self.fixed_sl_pts, 1), self.lot_size, True, None, current_ts, gap_pts=gap_pts)
        else:
            if gap_pts < self.gap_down_max_pts and bar_high < (ema8 - self.touch_proximity_pts):
                return Strategy1hSignal("SELL", "1H 8-EMA Resistance Rejection", current_spot, ema8, round(current_spot - self.target_pts, 1), round(current_spot + self.fixed_sl_pts, 1), self.lot_size, False, f"Gap Rule: {gap_pts:.1f} pt gap-down without pullback", current_ts, gap_pts=gap_pts)
            tested_resistance = (bar_high >= (ema8 - self.touch_proximity_pts)) and (bar_low <= (ema8 + self.touch_proximity_pts))
            rejected_down = (current_spot <= ema8) and ((bar_high - current_spot) >= 8.0)
            if tested_resistance and rejected_down:
                return Strategy1hSignal("SELL", "1H 8-EMA Resistance Rejection", current_spot, ema8, round(current_spot - self.target_pts, 1), round(current_spot + self.fixed_sl_pts, 1), self.lot_size, True, None, current_ts, gap_pts=gap_pts)

        dist = current_spot - ema8
        return Strategy1hSignal("NONE", "1H 8-EMA Touch", current_spot, ema8, 0.0, 0.0, self.lot_size, False, f"No 8-EMA Touch (Dist: {dist:+.1f} pts)", current_ts, gap_pts=gap_pts)

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
        highest = getattr(pos, "highest_spot", pos.entry_spot)

        if is_long:
            if current_spot > highest:
                pos.highest_spot = current_spot
                highest = current_spot
        else:
            lowest = getattr(pos, "lowest_spot", pos.entry_spot)
            if current_spot < lowest:
                pos.lowest_spot = current_spot
                lowest = current_spot

        if not pos.cost_to_cost_active:
            if is_long and (highest - pos.entry_spot >= self.cost_to_cost_trigger_pts):
                pos.cost_to_cost_active = True
                new_sl = max(new_sl, pos.entry_spot + self.cost_to_cost_brokerage_buffer_pts)
            elif not is_long:
                lowest = getattr(pos, "lowest_spot", pos.entry_spot)
                if (pos.entry_spot - lowest >= self.cost_to_cost_trigger_pts):
                    pos.cost_to_cost_active = True
                    new_sl = min(new_sl, pos.entry_spot - self.cost_to_cost_brokerage_buffer_pts)

        if is_long and current_spot >= pos.spot_target:
            return True, "1H Target Hit (+40 pts)", None
        elif not is_long and current_spot <= pos.spot_target:
            return True, "1H Short Target Hit (+40 pts)", None

        if is_long and current_spot <= new_sl:
            self.stage_stopped_today = True
            return True, "1H Stop-Loss Hit (-40 pts)", None
        elif not is_long and current_spot >= new_sl:
            self.stage_stopped_today = True
            return True, "1H Short Stop-Loss Hit (-40 pts)", None

        return False, None, new_sl

    def get_state(self, df_1h: pd.DataFrame, current_spot: float) -> Dict[str, Any]:
        ema8 = float(df_1h.iloc[-2].get("ema8", 0.0)) if len(df_1h) >= 2 else (float(df_1h.iloc[-1].get("ema8", 0.0)) if not df_1h.empty else 0.0)
        dist = round(current_spot - ema8, 2) if ema8 > 0.0 else 0.0
        market_state = "TESTING_8EMA_TOUCH" if abs(dist) <= self.touch_proximity_pts else ("ABOVE_8EMA_SUPPORT" if dist > 0 else "BELOW_8EMA_RESISTANCE")
        return {
            "strategy": "Flagship 1-Hour 8-EMA Touch (Live)",
            "execution_policy": "LIVE_AND_PAPER",
            "timeframe": "1h",
            "ema8": round(ema8, 2),
            "spot": round(current_spot, 2),
            "dist_to_ema8_pts": dist,
            "market_state": market_state,
            "touch_proximity_pts": self.touch_proximity_pts,
            "lot_size": self.lot_size,
            "stage_stopped_today": self.stage_stopped_today,
            "daily_trades_count_1h": self.daily_trades_count,
        }

# Historical import compatibility only; all active smoothing remains8period.
Strategy1h80EmaEvaluator=Strategy1h8EmaEvaluator
