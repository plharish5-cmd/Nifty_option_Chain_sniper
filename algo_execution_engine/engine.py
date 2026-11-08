"""Core Background Execution Orchestrator for Setup #1 and 1H 8-EMA on Port 8510 with Intrabar Catcher."""
from __future__ import annotations
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from analyst.algo_state_v206 import save as persist_state, restore as restore_state
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd

from .ema8_catcher import Ema8CatchingState, check_intrabar_touch, should_enter_or_skip, update_or_arm_state
from .order_gateway import DhanOrderGateway
from .strategy_1h_8ema import Strategy1h8EmaEvaluator, Strategy1hSignal, build_1h_data
from .strategy_setup1 import ActivePosition, Setup1Signal, Setup1StrategyEvaluator, load_config, prepare_frame

log = logging.getLogger("algo.engine")

class Setup1ExecutionEngine:
    def __init__(self, config_dict: Optional[Dict[str, Any]] = None, clock=None) -> None:
        self.cfg = config_dict or load_config()
        self.clock=clock or time.time
        self.current_session_date=datetime.fromtimestamp(self.clock(),ZoneInfo("Asia/Kolkata")).date().isoformat()
        self.recovery_error=""
        self.evaluator = Setup1StrategyEvaluator(self.cfg)
        self.evaluator_1h = Strategy1h8EmaEvaluator(self.cfg)
        self.gateway = DhanOrderGateway(self.cfg)
        self.gateway.clock=self.clock
        self.active_position: Optional[ActivePosition] = None
        self.closed_positions: List[Dict[str, Any]] = []
        self.daily_trades_count: int = 0
        self.daily_pnl_inr: float = 0.0
        self.is_running: bool = False
        self._loop_task: Optional[asyncio.Task] = None
        self.last_spot: float = 0.0
        self.last_candle_15m_ts: int = 0
        self.last_eval_time: str = ""
        self.last_signal: Optional[Setup1Signal] = None
        self.last_signal_1h: Optional[Strategy1hSignal] = None
        self.last_df_1h: pd.DataFrame = pd.DataFrame()
        self.circuit_breaker_tripped: bool = False
        self.circuit_breaker_reason: str = ""
        self.catcher_15m = Ema8CatchingState(timeframe="15m", **self.cfg['catcher']['15m'])
        self.catcher_1h = Ema8CatchingState(timeframe="1h", **self.cfg['catcher']['1h'])
        restore_state(self)

    def fetch_1m_candles(self, limit: int = 600) -> pd.DataFrame:
        db_path = str(self.cfg.get("execution", {}).get("sqlite_db_path", "ict_smc_app/backend/data/ict_smc.sqlite3"))
        full_db = Path(db_path) if Path(db_path).is_absolute() else (Path(__file__).resolve().parent.parent / db_path)
        if not full_db.exists():
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        try:
            with sqlite3.connect(str(full_db)) as conn:
                df = pd.read_sql_query("SELECT ts, o as open, h as high, l as low, c as close, v as volume FROM candles_1m WHERE sec_id = '13' ORDER BY ts DESC LIMIT ?", conn, params=(limit,))
                return df.sort_values("ts").reset_index(drop=True) if not df.empty else df
        except Exception:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    @staticmethod
    def resample_candles(df_1m: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
        if df_1m.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = df_1m.copy()
        df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return df.set_index("datetime").resample(f"{interval_minutes}min", closed="left", label="left").agg({"ts": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index()

    async def evaluate_cycle(self) -> None:
        day=datetime.fromtimestamp(self.clock(),ZoneInfo("Asia/Kolkata")).date().isoformat()
        if day!=self.current_session_date:
            self.evaluator.reset_day();self.evaluator_1h.reset_day()
            self.daily_trades_count=0;self.daily_pnl_inr=0.;self.current_session_date=day
            self.circuit_breaker_tripped=False;self.gateway.circuit_breaker_tripped=False
            self.catcher_15m.reset_for_new_bar(0,0.);self.catcher_1h.reset_for_new_bar(0,0.)
        try:
            if self.recovery_error:return
            if not self.active_position:
                recovered=await self.gateway.reconcile_pending_entries()
                if recovered:
                    self.active_position=recovered;self.daily_trades_count+=1
            await self._evaluate_cycle()
        finally:persist_state(self)

    async def _evaluate_cycle(self) -> None:
        now_dt = datetime.fromtimestamp(self.clock(),ZoneInfo("Asia/Kolkata"))
        current_time = now_dt.time()
        current_ts = int(self.clock())
        self.last_eval_time = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        df_1m = await asyncio.to_thread(self.fetch_1m_candles,3000)
        if df_1m.empty:
            return
        last_1m = df_1m.iloc[-1]
        self.last_spot = float(last_1m["close"])
        bar_1m_high, bar_1m_low = float(last_1m["high"]), float(last_1m["low"])
        # Never arm on an old session's final candle or on future timestamps.
        if current_ts-int(last_1m["ts"])>self.cfg['execution']['max_signal_age_seconds'] or current_ts<int(last_1m["ts"]):return
        df_15m = self.resample_candles(df_1m, 15)

        last_closed_candle: Optional[Dict[str, float]] = None
        if len(df_15m) >= 2:
            prev_candle = df_15m.iloc[-2]
            prev_ts = int(prev_candle["ts"])
            if prev_ts > self.last_candle_15m_ts:
                self.last_candle_15m_ts = prev_ts
                last_closed_candle = {"ts": prev_ts, "o": float(prev_candle["open"]), "h": float(prev_candle["high"]), "l": float(prev_candle["low"]), "c": float(prev_candle["close"])}

        allowed, reason = self.gateway.check_circuit_breakers(self.daily_trades_count, self.daily_pnl_inr)
        self.circuit_breaker_tripped = not allowed
        self.circuit_breaker_reason = "" if allowed else reason

        df_1h = build_1h_data(df_1m, self.cfg, as_of_ts=current_ts)
        self.last_df_1h = df_1h

        eod_parts = [int(x) for x in str(self.cfg.get("risk", {}).get("eod_square_off_time", "15:20")).split(":")[:2]]
        if self.active_position is not None:
            pos = self.active_position
            if pos.status=="EXIT_PENDING" or current_time >= dtime(eod_parts[0], eod_parts[1], 0):
                closed_pos = await self.gateway.execute_position_exit(pos, self.last_spot, "EOD_SQUARE_OFF")
                if not closed_pos.status.startswith("CLOSED"):return
                self.daily_pnl_inr += closed_pos.pnl_inr
                self.closed_positions.append(closed_pos.to_dict())
                self.active_position = None
                return
            if pos.trade_id.startswith("1H_8EMA"):
                should_exit, exit_reason, updated_sl = self.evaluator_1h.evaluate_active_position(pos, self.last_spot, pos.entry_option_price, df_1h)
            else:
                should_exit, exit_reason, updated_sl = self.evaluator.evaluate_active_position(pos, self.last_spot, pos.entry_option_price, df_15m, last_closed_candle)
            if not should_exit and updated_sl is not None:
                await self.gateway.modify_exchange_sl_order(pos, updated_sl)
            if should_exit and exit_reason:
                closed_pos = await self.gateway.execute_position_exit(pos, self.last_spot, exit_reason)
                if not closed_pos.status.startswith("CLOSED"):return
                self.daily_pnl_inr += closed_pos.pnl_inr
                self.closed_positions.append(closed_pos.to_dict())
                self.active_position = None
            return

        if self.circuit_breaker_tripped:
            return

        current_vwap, gap_pts = None, 0.0
        try:
            df_1m_calc = df_1m.copy()
            df_1m_calc["session_date"] = pd.to_datetime(df_1m_calc["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
            today_date = df_1m_calc["session_date"].iloc[-1]
            today_bars = df_1m_calc[df_1m_calc["session_date"] == today_date]
            if not today_bars.empty:
                v_sum = today_bars["volume"].sum()
                current_vwap = float((today_bars["close"] * today_bars["volume"]).sum() / v_sum) if v_sum > 0 else float(today_bars.iloc[-1]["close"])
                prior_bars = df_1m_calc[df_1m_calc["session_date"] < today_date]
                if not prior_bars.empty:
                    gap_pts = float(today_bars.iloc[0]["open"]) - float(prior_bars.iloc[-1]["close"])
        except Exception:
            pass

        if len(df_1h) >= 9:
            closed_1h = df_1h.iloc[-2]
            frozen_1h_ema8 = float(closed_1h["ema8"])
            side_1h = "BUY" if float(closed_1h["close"]) >= frozen_1h_ema8 else "SELL"
            update_or_arm_state(self.catcher_1h, int(closed_1h["ts"]), frozen_1h_ema8, self.last_spot, current_ts)
            check_intrabar_touch(self.catcher_1h, bar_1m_high, bar_1m_low, self.last_spot, current_ts, side=side_1h)
            can_enter_1h, _, _ = should_enter_or_skip(self.catcher_1h, self.last_spot, current_ts)

            signal_1h = self.evaluator_1h.check_1h_entry_signal(df_1h, self.last_spot, current_ts, current_time, gap_pts)
            self.last_signal_1h = signal_1h
            if signal_1h.is_valid and can_enter_1h:
                new_pos = await self.gateway.execute_1h_8ema_entry(signal_1h, self.daily_trades_count, self.daily_pnl_inr)
                if new_pos is not None:
                    self.active_position = new_pos
                    self.catcher_1h.action_taken_for_bar=True
                    self.daily_trades_count += 1
                    self.evaluator_1h.daily_trades_count += 1
                    return

        if len(df_15m) >= 20:
            f15 = prepare_frame(df_15m, self.cfg)
            closed_15m = f15.iloc[-2]
            frozen_15m_ema8 = float(closed_15m["ema8"])
            update_or_arm_state(self.catcher_15m, int(closed_15m["ts"]), frozen_15m_ema8, self.last_spot, current_ts)
            check_intrabar_touch(self.catcher_15m, bar_1m_high, bar_1m_low, self.last_spot, current_ts, side="BUY")
            can_enter_15m, _, _ = should_enter_or_skip(self.catcher_15m, self.last_spot, current_ts)

            signal = self.evaluator.check_setup1_entry_signal(
                df_15m, self.last_spot, current_ts, current_time,
                float(df_15m.iloc[-1]["low"]), float(df_15m.iloc[-1]["high"]), current_vwap, gap_pts
            )
            self.last_signal = signal
            if signal.is_valid and can_enter_15m:
                new_pos = await self.gateway.execute_setup1_entry(signal, self.daily_trades_count, self.daily_pnl_inr)
                if new_pos is not None:
                    self.active_position = new_pos
                    self.catcher_15m.action_taken_for_bar=True
                    self.daily_trades_count += 1

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        poll_interval = float(self.cfg.get("service", {}).get("poll_interval_sec", 1.0))
        async def _loop():
            while self.is_running:
                try:
                    await self.evaluate_cycle()
                except Exception as e:
                    log.error("Engine loop error: %s", e)
                await asyncio.sleep(poll_interval)
        self._loop_task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()

    async def emergency_flatten_all(self, reason: str = "Manual Emergency Flatten") -> Dict[str, Any]:
        if self.active_position is not None:
            closed = await self.gateway.execute_position_exit(self.active_position, self.last_spot, reason)
            if not closed.status.startswith("CLOSED"):return {"ok":False,"status":"EXIT_PENDING"}
            self.daily_pnl_inr += closed.pnl_inr
            self.closed_positions.append(closed.to_dict())
            self.active_position = None
            persist_state(self)
            return {"ok": True, "position": closed.to_dict()}
        return {"ok": True, "message": "No active position"}

    def get_telemetry_status(self) -> Dict[str, Any]:
        df_1m = self.fetch_1m_candles(limit=300)
        df_15m = self.resample_candles(df_1m, 15) if not df_1m.empty else pd.DataFrame()
        _, pre_info = self.evaluator.evaluate_15m_preconditions(df_15m)
        return {
            "status": "RUNNING" if self.is_running else "STOPPED",
            "timeframe": "15m_and_1h",
            "execution_mode": self.gateway.execution_mode,
            "last_spot": round(self.last_spot, 2),
            "last_eval_time": self.last_eval_time,
            "circuit_breaker": {
                "tripped": self.circuit_breaker_tripped,
                "reason": self.circuit_breaker_reason,
                "daily_trades": self.daily_trades_count,
                "max_trades": 2,
                "daily_pnl_inr": round(self.daily_pnl_inr, 2),
            },
            "rally_state": self.evaluator.rally.to_dict(),
            "ema8_catcher_15m": self.catcher_15m.to_dict(),
            "ema8_catcher_1h": self.catcher_1h.to_dict(),
            "trend_filter_15m": pre_info,
            "strategy_1h_8ema_state": self.evaluator_1h.get_state(self.last_df_1h, self.last_spot),
            "active_position": self.active_position.to_dict() if self.active_position else None,
            "health": self.recovery_error or ("OPEN_UNPROTECTED" if self.active_position and self.active_position.status=="OPEN_UNPROTECTED" else "OK"),
            "port": int(self.cfg.get("service", {}).get("port", 8510)),
        }

    def get_stage3_4_dashboard_data(self) -> Dict[str, Any]:
            """Consolidated telemetry for Stage 2 Backtest, Stage 3 Sandbox, and Stage 4 Live Gatekeeper."""
            grad_status = self.gateway.sandbox_tracker.evaluate_graduation_status()
            recent_sandbox_trades = self.gateway.sandbox_tracker.get_completed_trades(limit=10)
            gatekeeper_telemetry = self.gateway.get_stage4_telemetry()
    
            # Prior implementation embedded performance constants without source evidence.
            stage2_summary={"status":"NO_VERIFIED_BACKTEST_REPORT","comparison":{},"timeframe":"15m"}
    
            return {
                "ok": True,
                "status": "RUNNING" if self.is_running else "STOPPED",
                "last_eval_time": self.last_eval_time,
                "last_spot": round(self.last_spot, 2),
                "stage2_backtest": stage2_summary,
                "stage3_sandbox": {
                    **grad_status,
                    "recent_trades": recent_sandbox_trades,
                },
                "stage4_gatekeeper": gatekeeper_telemetry,
            }
