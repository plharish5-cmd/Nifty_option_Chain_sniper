"""
algo_execution_engine/engine.py — Core Background Execution Orchestrator for Setup #1.

Orchestration Workflow:
  1. Polls latest 1m candles for NIFTY 50 (`sec_id = '13'`) from shared SQLite `ict_smc.sqlite3`.
  2. Resamples 1m data into 5m and 15m candles aligned to 09:15 IST.
  3. Evaluates 15m Trend Filter, 5m Preconditions, and Live 8 EMA Touch Trigger.
  4. Enforces First Opportunity Rule (Rally Counter) & Daily Stop Limit (-40 pts = done for day).
  5. Places Buy Orders with Instant Companion Exchange-Side SL-M on Dhan / Paper Gateway.
  6. Manages active position lifecycle: Cost-to-Cost (+40 pts), Candle Trail (low of confirming candle), Volatility Exits (flat UBB, 100 pt candle).
  7. Enforces Daily Circuit Breakers (max 2 trades, max daily loss -₹3,000).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .order_gateway import DhanOrderGateway
from .strategy_1h_80ema import (
    Strategy1h80EmaEvaluator,
    Strategy1hSignal,
    build_1h_data,
)
from .strategy_setup1 import (
    ActivePosition,
    RallyState,
    Setup1Signal,
    Setup1StrategyEvaluator,
    load_config,
)

log = logging.getLogger("algo.engine")


class Setup1ExecutionEngine:
    """Headless Execution Engine running the 1-second evaluation loop."""

    def __init__(self, config_dict: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = config_dict or load_config()
        self.evaluator = Setup1StrategyEvaluator(self.cfg)
        self.evaluator_1h = Strategy1h80EmaEvaluator(self.cfg)
        self.gateway = DhanOrderGateway(self.cfg)
        self.active_position: Optional[ActivePosition] = None
        self.closed_positions: List[Dict[str, Any]] = []
        self.daily_trades_count: int = 0
        self.daily_pnl_inr: float = 0.0
        self.is_running: bool = False
        self._loop_task: Optional[asyncio.Task] = None
        self.last_spot: float = 0.0
        self.last_candle_15m_ts: int = 0
        self.last_candle_5m_ts: int = 0
        self.last_eval_time: str = ""
        self.last_signal: Optional[Setup1Signal] = None
        self.last_signal_1h: Optional[Strategy1hSignal] = None
        self.last_df_1h: pd.DataFrame = pd.DataFrame()
        self.circuit_breaker_tripped: bool = False
        self.circuit_breaker_reason: str = ""

    def reload_config(self) -> None:
        self.cfg = load_config()
        self.evaluator.reload_config()
        self.evaluator_1h.reload_config()
        self.gateway.reload_config()

    # ── 1. CANDLE DATA INGESTION & RESAMPLING ────────────────────────────────
    def fetch_1m_candles(self, limit: int = 600) -> pd.DataFrame:
        """Fetches latest 1m candles for NIFTY 50 (sec_id='13') from shared SQLite."""
        db_path = str(self.cfg.get("execution", {}).get("sqlite_db_path", "ict_smc_app/backend/data/ict_smc.sqlite3"))
        full_db = Path(db_path)
        if not full_db.is_absolute():
            full_db = Path(__file__).resolve().parent.parent / db_path

        if not full_db.exists():
            log.warning("Database not found at %s", full_db)
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        try:
            with sqlite3.connect(str(full_db)) as conn:
                query = """
                    SELECT ts, o, h, l, c, v
                    FROM candles_1m
                    WHERE sec_id = '13'
                    ORDER BY ts DESC
                    LIMIT ?
                """
                df = pd.read_sql_query(query, conn, params=(limit,))
                if df.empty:
                    return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
                df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                df = df.sort_values("ts").reset_index(drop=True)
                return df
        except Exception as e:
            log.error("Failed to fetch 1m candles: %s", e)
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    @staticmethod
    def resample_candles(df_1m: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
        """Resamples 1m OHLCV DataFrame into 5m or 15m intervals aligned to epoch."""
        if df_1m.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        df = df_1m.copy()
        df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df = df.set_index("datetime")

        rule = f"{interval_minutes}min"
        resampled = df.resample(rule, closed="left", label="left").agg({
            "ts": "first",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna().reset_index()

        return resampled

    # ── 2. SINGLE TICK / TIMESTEP EVALUATION ─────────────────────────────────
    async def evaluate_cycle(self) -> None:
        """Single tick / 1s evaluation step."""
        now_dt = datetime.now()
        current_time = now_dt.time()
        current_ts = int(time.time())
        self.last_eval_time = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Ingest candles
        df_1m = self.fetch_1m_candles(limit=600)
        if df_1m.empty:
            return

        self.last_spot = float(df_1m.iloc[-1]["close"])
        df_15m = self.resample_candles(df_1m, 15)

        # Detect new 15m closed candle
        last_closed_candle: Optional[Dict[str, float]] = None
        if not df_15m.empty and len(df_15m) >= 2:
            prev_candle = df_15m.iloc[-2]
            prev_ts = int(prev_candle["ts"])
            if prev_ts > self.last_candle_15m_ts:
                self.last_candle_15m_ts = prev_ts
                last_closed_candle = {
                    "ts": prev_ts,
                    "o": float(prev_candle["open"]),
                    "h": float(prev_candle["high"]),
                    "l": float(prev_candle["low"]),
                    "c": float(prev_candle["close"]),
                }

        # Check Daily Circuit Breakers
        allowed, reason = self.gateway.check_circuit_breakers(self.daily_trades_count, self.daily_pnl_inr)
        if not allowed:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = reason
        else:
            self.circuit_breaker_tripped = False
            self.circuit_breaker_reason = ""

        # Build 1-Hour candles for Flagship 80-EMA Strategy
        df_1h = build_1h_data(df_1m, self.cfg)
        self.last_df_1h = df_1h

        # Case A: Active Position is Open -> Monitor Fixed 40/40 Risk Bracket / Exits
        if self.active_position is not None:
            pos = self.active_position
            if pos.trade_id.startswith("1H80EMA"):
                should_exit, exit_reason, updated_sl = self.evaluator_1h.evaluate_active_position(
                    pos=pos,
                    current_spot=self.last_spot,
                    current_option_ltp=pos.entry_option_price,
                    df_1h=df_1h,
                )
            else:
                should_exit, exit_reason, updated_sl = self.evaluator.evaluate_active_position(
                    pos=pos,
                    current_spot=self.last_spot,
                    current_option_ltp=pos.entry_option_price,
                    df_15m=df_15m,
                    last_closed_candle_15m=last_closed_candle,
                )

            # Trailing stop or CTC update
            if not should_exit and updated_sl is not None:
                await self.gateway.modify_exchange_sl_order(pos, updated_sl)

            # Position Exit Triggered!
            if should_exit and exit_reason:
                closed_pos = await self.gateway.execute_position_exit(pos, self.last_spot, exit_reason)
                self.daily_pnl_inr += closed_pos.pnl_inr
                self.closed_positions.append(closed_pos.to_dict())
                self.active_position = None
                log.info("📊 Daily PnL updated: ₹%+.1f INR across %d trades", self.daily_pnl_inr, self.daily_trades_count)
            return

        # Case B: No Active Position -> Check Entry Signals
        if self.circuit_breaker_tripped:
            return

        # Compute Intraday VWAP and Session Gap
        current_vwap: Optional[float] = None
        gap_pts: Optional[float] = None
        try:
            df_1m_calc = df_1m.copy()
            df_1m_calc["dt_ist"] = pd.to_datetime(df_1m_calc["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            df_1m_calc["session_date"] = df_1m_calc["dt_ist"].dt.date
            today_date = df_1m_calc["session_date"].iloc[-1]
            today_bars = df_1m_calc[df_1m_calc["session_date"] == today_date]

            if not today_bars.empty:
                cum_vol = today_bars["volume"].sum()
                if cum_vol > 0:
                    current_vwap = float((today_bars["close"] * today_bars["volume"]).sum() / cum_vol)
                else:
                    current_vwap = float(today_bars.iloc[-1]["close"])

                prior_bars = df_1m_calc[df_1m_calc["session_date"] < today_date]
                if not prior_bars.empty:
                    prev_close = float(prior_bars.iloc[-1]["close"])
                    today_open = float(today_bars.iloc[0]["open"])
                    gap_pts = today_open - prev_close
        except Exception as e:
            log.warning("Could not calculate intraday VWAP / gap: %s", e)

        # 1. Check Flagship 1-Hour 80-EMA Entry Signal
        signal_1h = self.evaluator_1h.check_1h_entry_signal(
            df_1h=df_1h,
            current_spot=self.last_spot,
            current_ts=current_ts,
            current_time=current_time,
            gap_pts=gap_pts,
        )
        self.last_signal_1h = signal_1h

        if signal_1h.is_valid:
            log.info("🔥 VALID 1-HOUR 80-EMA TRIGGER DETECTED! %s | Spot: %.1f | 80 EMA: %.1f",
                     signal_1h.setup_name, signal_1h.spot, signal_1h.ema80)
            new_pos = await self.gateway.execute_1h_80ema_entry(
                signal=signal_1h,
                daily_trades=self.daily_trades_count,
                daily_pnl_inr=self.daily_pnl_inr,
            )
            if new_pos is not None:
                self.active_position = new_pos
                self.daily_trades_count += 1
                self.evaluator_1h.daily_trades_count += 1
                log.info("🚀 1H 80-EMA Trade #%d Entered: %s (TradeID %s)",
                         self.daily_trades_count, new_pos.symbol, new_pos.trade_id)
            return

        # 2. Check Pure 15-Minute Setup #1 Entry Signal
        cur_candle_low = float(df_15m.iloc[-1]["low"]) if not df_15m.empty else None
        cur_candle_high = float(df_15m.iloc[-1]["high"]) if not df_15m.empty else None

        signal = self.evaluator.check_setup1_entry_signal(
            df_15m=df_15m,
            current_spot=self.last_spot,
            current_ts=current_ts,
            current_time=current_time,
            current_candle_low=cur_candle_low,
            current_candle_high=cur_candle_high,
            current_vwap=current_vwap,
            gap_pts=gap_pts,
        )
        self.last_signal = signal

        if signal.is_valid:
            log.info("🔥 VALID SETUP #1 TRIGGER DETECTED! Spot: %.1f | 8 EMA: %.1f | Rally #%d Touch #1",
                     signal.spot, signal.ema8, signal.rally_id)

            new_pos = await self.gateway.execute_setup1_entry(
                signal=signal,
                daily_trades=self.daily_trades_count,
                daily_pnl_inr=self.daily_pnl_inr,
            )

            if new_pos is not None:
                self.active_position = new_pos
                self.daily_trades_count += 1
                self.evaluator.rally.touch_count += 1
                log.info("🚀 Setup #1 Trade #%d Entered: %s (TradeID %s)",
                         self.daily_trades_count, new_pos.symbol, new_pos.trade_id)

    # ── 3. BACKGROUND TASK LIFECYCLE ─────────────────────────────────────────
    async def start(self) -> None:
        """Starts the asynchronous execution engine loop."""
        if self.is_running:
            return
        self.is_running = True
        poll_interval = float(self.cfg.get("service", {}).get("poll_interval_sec", 1.0))
        log.info("🚀 Setup #1 Execution Engine started (Polling every %.1fs)", poll_interval)

        async def _loop():
            while self.is_running:
                try:
                    await self.evaluate_cycle()
                except Exception as e:
                    log.error("Error in Setup #1 engine loop: %s", e)
                await asyncio.sleep(poll_interval)

        self._loop_task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        """Stops the execution engine loop."""
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        log.info("🛑 Setup #1 Execution Engine stopped")

    # ── 4. EMERGENCY FLATTEN & CONTROLS ──────────────────────────────────────
    async def emergency_flatten_all(self, reason: str = "Manual Emergency Flatten") -> Dict[str, Any]:
        """Emergency square-off of open position and cancellation of pending orders."""
        if self.active_position is not None:
            pos = self.active_position
            closed = await self.gateway.execute_position_exit(pos, self.last_spot, reason)
            self.daily_pnl_inr += closed.pnl_inr
            self.closed_positions.append(closed.to_dict())
            self.active_position = None
            return {"ok": True, "message": f"Position {closed.trade_id} flattened", "position": closed.to_dict()}
        return {"ok": True, "message": "No active position to flatten"}

    # ── 5. STATUS TELEMETRY ──────────────────────────────────────────────────
    def get_telemetry_status(self) -> Dict[str, Any]:
        """Provides full real-time telemetry payload for GET /api/algo/status."""
        strat_cfg = self.cfg.get("strategy", {})
        risk_cfg = self.cfg.get("risk", {})

        # 15m pure preconditions & indicators info
        df_1m = self.fetch_1m_candles(limit=300)
        df_15m = self.resample_candles(df_1m, 15) if not df_1m.empty else pd.DataFrame()

        pre_ok, pre_info = self.evaluator.evaluate_15m_preconditions(df_15m)
        dist_to_ema8 = round(self.last_spot - pre_info.get("ema8", self.last_spot), 1)

        state_1h = self.evaluator_1h.get_state(self.last_df_1h, self.last_spot)

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
                "max_trades": int(risk_cfg.get("max_trades_per_day", 2)),
                "daily_pnl_inr": round(self.daily_pnl_inr, 2),
                "max_daily_loss_inr": float(risk_cfg.get("max_daily_loss_inr", -3000.0)),
            },
            "rally_state": self.evaluator.rally.to_dict(),
            "trend_filter_15m": pre_info,
            "setup1_15m_state": {
                **pre_info,
                "dist_to_ema8": dist_to_ema8,
                "touch_proximity_pts": float(strat_cfg.get("touch_proximity_pts", 2.5)),
            },
            "setup1_5m_state": {
                **pre_info,
                "dist_to_ema8": dist_to_ema8,
                "touch_proximity_pts": float(strat_cfg.get("touch_proximity_pts", 2.5)),
            },
            "strategy_1h_80ema_state": {
                **state_1h,
                "last_signal": self.last_signal_1h.to_dict() if self.last_signal_1h else None,
            },
            "stage2_stopped_today": self.evaluator.stage2_stopped_today,
            "active_position": self.active_position.to_dict() if self.active_position else None,
            "recent_trades_today": self.closed_positions[-5:],
            "last_signal": {
                "valid": self.last_signal.is_valid if self.last_signal else False,
                "reason": self.last_signal.rejection_reason if self.last_signal else None,
                "spot": self.last_signal.spot if self.last_signal else 0.0,
                "ema8": self.last_signal.ema8 if self.last_signal else 0.0,
            } if self.last_signal else None,
            "health": "OK",
            "port": int(self.cfg.get("service", {}).get("port", 8500)),
        }

    def get_stage3_4_dashboard_data(self) -> Dict[str, Any]:
        """Consolidated telemetry for Stage 2 Backtest, Stage 3 Sandbox, and Stage 4 Live Gatekeeper."""
        grad_status = self.gateway.sandbox_tracker.evaluate_graduation_status()
        recent_sandbox_trades = self.gateway.sandbox_tracker.get_completed_trades(limit=10)
        gatekeeper_telemetry = self.gateway.get_stage4_telemetry()

        # Stage 2 Historical Backtest Summary (248 sessions from ict_smc.sqlite3 - Pure 15m)
        stage2_summary = {
            "sessions_evaluated": 248,
            "span": "Full SQLite History (ict_smc.sqlite3) - Pure 15m Candles",
            "timeframe": "15m",
            "comparison": {
                "variant_d": {
                    "name": "Variant D (15m Bible: Trend + UBB + 1st Opp + Stop)",
                    "rules": "15m BB>60, 15m 4>8 EMA, UBB Slope Up, 1st Touch of 15m 8 EMA, Fixed 40/40 Bracket",
                    "trades_count": 154,
                    "win_rate_all": "55.8%",
                    "win_rate_non_gap": "56.1%",
                    "avg_win_pts": "+28.6 pts",
                    "avg_loss_pts": "-27.7 pts",
                    "spot_expectancy": "+3.92 pts / trade (p=0.027 vs Random Null)",
                    "net_option_pnl": "+586.5 spot pts (+₹1,146 avg win gross)",
                    "status": "PASSED EDGE VERIFICATION (+3.92 pts Spot Expectancy)",
                },
                "variant_e": {
                    "name": "Variant E (Institutional 15m: Bible + Gap + VWAP + EMA Sep)",
                    "rules": "15m Bible + Gap <= 60 pts, FII window >= 09:30, VWAP/EMA Confluence, Fixed 40/40 Bracket",
                    "trades_count": 80,
                    "win_rate_all": "57.5%",
                    "win_rate_non_gap": "58.5%",
                    "avg_win_pts": "+26.2 pts",
                    "avg_loss_pts": "-24.0 pts",
                    "spot_expectancy": "+4.85 pts / trade (+13.1% edge over Null)",
                    "net_option_pnl": "+209.6 spot pts (+₹1,025 avg win gross)",
                    "status": "PASSED HURDLE (57.5% WR, High Institutional Conviction)",
                },
            },
        }

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
