"""
algo_execution_engine/order_gateway.py — Dhan API v2 & Paper Trading Execution Gateway with Server-Side SL-M.

Safeguards & Features:
  1. Live Dhan v2 Orders API (`/v2/orders`) & companion exchange-side `SL-M` (`STOP_LOSS_MARKET`) order placement.
  2. Automatic SL-M modification on Dhan servers when stop-loss trails or moves to Cost-to-Cost.
  3. Automatic SL-M cancellation upon Target or Volatility exit.
  4. Real-time ATM CE strike & contract security ID resolution from shared SQLite `chain_snap`/`opt_activity` or Dhan API.
  5. Full Paper Trading Simulation recording to `paper_trades` in shared SQLite `ict_smc.sqlite3`.
  6. Daily Circuit Breakers: Max 2 trades/day, Max daily loss (-₹3,000).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .strategy_setup1 import ActivePosition, Setup1Signal, load_config

log = logging.getLogger("algo.gateway")


def load_env_if_needed() -> None:
    """Reads .env without leaking secrets into logs or console."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        except Exception as e:
            log.error("Error loading .env: %s", e)


class DhanOrderGateway:
    """Manages order placement, exchange-side companion SL-M, and paper trading execution."""

    def __init__(self, config_dict: Optional[Dict[str, Any]] = None) -> None:
        load_env_if_needed()
        self.cfg = config_dict or load_config()
        self.exec_cfg = self.cfg.get("execution", {})
        self.risk_cfg = self.cfg.get("risk", {})
        self.base_url = str(self.exec_cfg.get("dhan_base_url", "https://api.dhan.co/v2"))
        self.execution_mode = str(self.exec_cfg.get("execution_mode", "PAPER")).upper()
        self.db_path = str(self.exec_cfg.get("sqlite_db_path", "ict_smc_app/backend/data/ict_smc.sqlite3"))
        self.circuit_breaker_tripped: bool = False
        self.circuit_breaker_reason: str = ""
        self._cached_contracts: Dict[str, Tuple[float, str, str, float, float]] = {}

    def reload_config(self) -> None:
        self.cfg = load_config()
        self.exec_cfg = self.cfg.get("execution", {})
        self.risk_cfg = self.cfg.get("risk", {})

    def _dhan_headers(self) -> Dict[str, str]:
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client_id = os.environ.get("DHAN_CLIENT_ID", "")
        return {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── 1. CIRCUIT BREAKER CHECKS ────────────────────────────────────────────
    def check_circuit_breakers(self, daily_trades: int, daily_pnl_inr: float) -> Tuple[bool, str]:
        """
        Daily Circuit Breakers:
          - Max 2 trades per day
          - Max daily loss limit (-₹3,000)
        """
        max_trades = int(self.risk_cfg.get("max_trades_per_day", 2))
        max_daily_loss = float(self.risk_cfg.get("max_daily_loss_inr", -3000.0))

        if daily_trades >= max_trades:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = f"Max daily trades reached ({daily_trades}/{max_trades})"
            return False, self.circuit_breaker_reason

        if daily_pnl_inr <= max_daily_loss:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = f"Max daily loss limit breached (₹{daily_pnl_inr:,.1f} <= ₹{max_daily_loss:,.1f})"
            return False, self.circuit_breaker_reason

        return True, "OK"

    # ── 2. ATM STRIKE & CONTRACT RESOLUTION ──────────────────────────────────
    def resolve_atm_ce_contract(self, spot: float) -> Tuple[float, str, str, float]:
        """
        Determines ATM Strike, Option Security ID, Display Symbol, and estimated LTP.
        Returns: (strike, security_id, symbol, ltp)
        """
        strike = float(int(round(spot / 50.0)) * 50)
        symbol = f"NIFTY {int(strike)} CE"
        sec_id = f"SIM_{int(strike)}_CE"
        est_ltp = max(50.0, round(120.0 + (spot - strike) * 0.55, 1))

        # Check fast RAM cache (valid 60 seconds)
        cache_key = f"{int(strike)}_CE"
        now_t = time.time()
        cached = self._cached_contracts.get(cache_key)
        if cached and (now_t - cached[4] < 60.0):
            return cached[0], cached[1], cached[2], cached[3]

        # Check shared SQLite DB (chain_snap or opt_activity)
        try:
            full_db = Path(self.db_path)
            if not full_db.is_absolute():
                full_db = Path(__file__).resolve().parent.parent / self.db_path
            if full_db.exists():
                with sqlite3.connect(str(full_db)) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT payload FROM chain_snap WHERE payload LIKE ? ORDER BY ts DESC LIMIT 1",
                        (f"%{int(strike)}%",)
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        try:
                            snap_data = json.loads(row[0])
                            rows = snap_data.get("rows", [])
                            for r in rows:
                                if abs(float(r.get("strike", 0)) - strike) < 0.1:
                                    ce_data = r.get("ce", {})
                                    sid = str(ce_data.get("sid") or "")
                                    ltp = float(ce_data.get("ltp") or est_ltp)
                                    if sid and not sid.startswith("SIM_"):
                                        self._cached_contracts[cache_key] = (strike, sid, symbol, ltp, now_t)
                                        return strike, sid, symbol, ltp
                        except Exception as e:
                            log.debug("chain_snap parse error: %s", e)
        except Exception as e:
            log.debug("SQLite contract resolution lookup error: %s", e)

        # Fallback to direct Dhan API /optionchain if token is available
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client_id = os.environ.get("DHAN_CLIENT_ID", "")
        if token and client_id:
            try:
                headers = self._dhan_headers()
                with httpx.Client(timeout=4.0) as client:
                    r_exp = client.post(
                        f"{self.base_url}/optionchain/expirylist",
                        json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
                        headers=headers,
                    )
                    exp_list = r_exp.json().get("data", []) if r_exp.status_code == 200 else []
                    if exp_list:
                        active_exp = exp_list[0]
                        r_oc = client.post(
                            f"{self.base_url}/optionchain",
                            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I", "Expiry": active_exp},
                            headers=headers,
                        )
                        if r_oc.status_code == 200:
                            data = r_oc.json().get("data", {})
                            oc = data.get("oc", {})
                            stk_data = oc.get(f"{strike:.6f}") or oc.get(str(int(strike)))
                            if stk_data:
                                opt_info = stk_data.get("ce", {})
                                real_sid = str(opt_info.get("security_id") or "")
                                real_ltp = float(opt_info.get("last_price") or est_ltp)
                                if real_sid and real_sid != "0":
                                    self._cached_contracts[cache_key] = (strike, real_sid, symbol, real_ltp, now_t)
                                    return strike, real_sid, symbol, real_ltp
            except Exception as exc:
                log.warning("Direct Dhan option chain strike lookup error: %s", exc)

        return strike, sec_id, symbol, est_ltp

    # ── 3. ORDER EXECUTION WITH COMPANION SL-M ───────────────────────────────
    async def execute_setup1_entry(
        self,
        signal: Setup1Signal,
        daily_trades: int,
        daily_pnl_inr: float,
    ) -> Optional[ActivePosition]:
        """
        Executes Setup #1 Long entry:
          1. Verifies circuit breakers (max 2 trades, max loss -₹3,000).
          2. Resolves ATM NIFTY CE strike and security ID.
          3. Places Entry BUY Order (PAPER or LIVE).
          4. Instantly places Exchange-Side companion SL-M order on Dhan servers (LIVE) or simulates it (PAPER).
          5. Returns ActivePosition dataclass.
        """
        # Circuit Breaker Guard
        allowed, reason = self.check_circuit_breakers(daily_trades, daily_pnl_inr)
        if not allowed:
            log.warning("🚨 Execution blocked by Circuit Breakers: %s", reason)
            return None

        # Resolve ATM CE Contract
        strike, sec_id, symbol, est_ltp = self.resolve_atm_ce_contract(signal.spot)
        strat = self.cfg.get("strategy", {})
        delta = float(strat.get("option_delta_default", 0.55))
        fixed_sl_pts = float(strat.get("fixed_sl_pts", 40.0))
        target_pts = float(strat.get("target_pts", 40.0))
        lot_size = int(self.exec_cfg.get("lot_size", 75))
        lots = int(self.exec_cfg.get("lots_per_trade", 1))
        qty = lot_size * lots

        # Option Target and SL calculation using Delta
        opt_sl = max(0.50, round(est_ltp - (fixed_sl_pts * delta), 1))
        opt_target = round(est_ltp + (target_pts * delta), 1)

        trade_id = f"SETUP1_{int(time.time())}"
        entry_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        entry_order_id = f"PAPER_ENTRY_{trade_id}"
        exchange_sl_order_id = f"PAPER_SLM_{trade_id}"

        # LIVE MODE EXECUTION
        if self.execution_mode == "LIVE":
            token = os.environ.get("DHAN_ACCESS_TOKEN")
            client_id = os.environ.get("DHAN_CLIENT_ID")
            if not token or not client_id:
                log.error("Cannot execute LIVE order: DHAN credentials missing from environment!")
                return None

            # 1. Place Entry Market Buy Order
            headers = self._dhan_headers()
            url_orders = f"{self.base_url}/orders"
            payload_entry = {
                "dhanClientId": client_id,
                "correlationId": f"entry_{trade_id}",
                "transactionType": "BUY",
                "exchangeSegment": str(self.exec_cfg.get("exchange_segment", "NSE_FNO")),
                "productType": str(self.exec_cfg.get("product_type", "INTRADAY")),
                "orderType": "MARKET",
                "validity": "DAY",
                "securityId": str(sec_id),
                "quantity": int(qty),
                "price": 0.0,
                "triggerPrice": 0.0,
                "disclosedQuantity": 0,
                "afterMarketOrder": False,
                "amoTime": "OPEN",
                "boProfitValue": 0.0,
                "boStopLossValue": 0.0,
            }

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post(url_orders, json=payload_entry, headers=headers)
                    data = res.json() if res.status_code == 200 else {}
                    if res.status_code == 200 and data.get("status") != "failure":
                        entry_order_id = str(data.get("orderId") or data.get("data", {}).get("orderId", "LIVE_ENTRY"))
                        log.info("✅ Live Dhan Entry Order Placed: %s (SecID %s, Qty %d)", entry_order_id, sec_id, qty)
                    else:
                        err = data.get("remarks") or data.get("errorMessage") or res.text[:200]
                        log.error("❌ Live Dhan Entry Order Rejected: %s", err)
                        return None
            except Exception as e:
                log.error("Exception placing live Dhan Entry order: %s", e)
                return None

            # 2. Instantly Place Companion Server-Side Exchange SL-M Order
            payload_slm = {
                "dhanClientId": client_id,
                "correlationId": f"slm_{trade_id}",
                "transactionType": "SELL",
                "exchangeSegment": str(self.exec_cfg.get("exchange_segment", "NSE_FNO")),
                "productType": str(self.exec_cfg.get("product_type", "INTRADAY")),
                "orderType": "STOP_LOSS_MARKET",
                "validity": "DAY",
                "securityId": str(sec_id),
                "quantity": int(qty),
                "price": 0.0,
                "triggerPrice": float(opt_sl),
                "disclosedQuantity": 0,
                "afterMarketOrder": False,
                "amoTime": "OPEN",
                "boProfitValue": 0.0,
                "boStopLossValue": 0.0,
            }

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res_sl = await client.post(url_orders, json=payload_slm, headers=headers)
                    data_sl = res_sl.json() if res_sl.status_code == 200 else {}
                    if res_sl.status_code == 200 and data_sl.get("status") != "failure":
                        exchange_sl_order_id = str(data_sl.get("orderId") or data_sl.get("data", {}).get("orderId", "LIVE_SLM"))
                        log.info("🛡️ Live Server-Side Companion SL-M Placed: %s (Trigger ₹%.1f)", exchange_sl_order_id, opt_sl)
                    else:
                        err_sl = data_sl.get("remarks") or data_sl.get("errorMessage") or res_sl.text[:200]
                        log.error("⚠️ Companion SL-M rejected on Dhan server: %s", err_sl)
            except Exception as exc:
                log.error("Exception placing companion SL-M on Dhan: %s", exc)

        else:
            # PAPER TRADING MODE
            log.info("📝 [PAPER TRADE] Setup #1 BUY %s (%s) @ ₹%.1f | Companion SL-M Trigger ₹%.1f",
                     symbol, sec_id, est_ltp, opt_sl)

        position = ActivePosition(
            trade_id=trade_id,
            symbol=symbol,
            security_id=sec_id,
            option_type="CE",
            strike=strike,
            quantity=qty,
            entry_spot=signal.spot,
            entry_option_price=est_ltp,
            entry_time=entry_time_str,
            entry_ts=signal.timestamp,
            spot_sl=signal.sl_spot,
            spot_target=signal.target_spot,
            option_sl=opt_sl,
            option_target=opt_target,
            delta=delta,
            cost_to_cost_active=False,
            highest_spot=signal.spot,
            exchange_sl_order_id=exchange_sl_order_id,
            entry_order_id=entry_order_id,
            status="OPEN",
        )
        return position

    # ── 4. STOP-LOSS MODIFICATION ON DHAN / PAPER ────────────────────────────
    async def modify_exchange_sl_order(self, pos: ActivePosition, new_spot_sl: float) -> None:
        """
        Modifies companion exchange-side SL-M order on Dhan servers when stop-loss trails or moves to CTC.
        """
        delta = pos.delta
        pts_diff = new_spot_sl - pos.entry_spot
        new_opt_sl = max(0.50, round(pos.entry_option_price + (pts_diff * delta), 1))

        old_opt_sl = pos.option_sl
        pos.spot_sl = new_spot_sl
        pos.option_sl = new_opt_sl

        log.info("🔄 Trailing Stop-Loss update: Spot SL %.1f (Option Trigger ₹%.1f -> ₹%.1f)",
                 new_spot_sl, old_opt_sl, new_opt_sl)

        if self.execution_mode == "LIVE" and pos.exchange_sl_order_id and not pos.exchange_sl_order_id.startswith("PAPER_"):
            token = os.environ.get("DHAN_ACCESS_TOKEN")
            client_id = os.environ.get("DHAN_CLIENT_ID")
            if token and client_id:
                headers = self._dhan_headers()
                url_modify = f"{self.base_url}/orders/{pos.exchange_sl_order_id}"
                payload = {
                    "dhanClientId": client_id,
                    "orderId": pos.exchange_sl_order_id,
                    "orderType": "STOP_LOSS_MARKET",
                    "quantity": int(pos.quantity),
                    "price": 0.0,
                    "triggerPrice": float(new_opt_sl),
                    "validity": "DAY",
                }
                try:
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        res = await client.put(url_modify, json=payload, headers=headers)
                        if res.status_code == 200:
                            log.info("✅ Live SL-M modified on Dhan server: %s -> ₹%.1f", pos.exchange_sl_order_id, new_opt_sl)
                        else:
                            log.warning("⚠️ Failed to modify live SL-M on Dhan (HTTP %d): %s", res.status_code, res.text[:150])
                except Exception as e:
                    log.error("Exception modifying live SL-M order: %s", e)

    # ── 5. POSITION EXIT & SL-M CANCELLATION ──────────────────────────────────
    async def execute_position_exit(
        self,
        pos: ActivePosition,
        exit_spot: float,
        reason: str,
    ) -> ActivePosition:
        """
        Closes position:
          1. Cancels companion exchange-side SL-M order on Dhan (if not already triggered by SL).
          2. Sends Market SELL order on Dhan (if live) or computes simulated fill (if paper).
          3. Computes final P&L in points and INR.
          4. Persists record in shared SQLite `paper_trades` table.
        """
        now_dt = datetime.now()
        exit_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Estimate option exit price using Delta
        spot_pnl_pts = exit_spot - pos.entry_spot
        est_option_exit = max(0.05, round(pos.entry_option_price + (spot_pnl_pts * pos.delta), 1))
        option_pnl_pts = est_option_exit - pos.entry_option_price
        pnl_inr = round(option_pnl_pts * pos.quantity, 2)

        pos.exit_spot = exit_spot
        pos.exit_option_price = est_option_exit
        pos.exit_time = exit_time_str
        pos.exit_reason = reason
        pos.pnl_pts = spot_pnl_pts
        pos.pnl_inr = pnl_inr

        if "Target" in reason:
            pos.status = "CLOSED_TARGET"
        elif "Stop-Loss" in reason or "Stop Hit" in reason:
            pos.status = "CLOSED_SL"
        elif "Volatility" in reason:
            pos.status = "CLOSED_VOLATILITY"
        else:
            pos.status = "CLOSED_MANUAL"

        # Cancel companion SL-M if closing on Target or Volatility
        if self.execution_mode == "LIVE":
            token = os.environ.get("DHAN_ACCESS_TOKEN")
            client_id = os.environ.get("DHAN_CLIENT_ID")
            if token and client_id and pos.exchange_sl_order_id and not pos.exchange_sl_order_id.startswith("PAPER_"):
                # If reason is NOT SL hit, cancel the pending SL-M and place Market Exit Sell
                if "Stop" not in reason:
                    try:
                        headers = self._dhan_headers()
                        async with httpx.AsyncClient(timeout=8.0) as client:
                            await client.delete(f"{self.base_url}/orders/{pos.exchange_sl_order_id}", headers=headers)
                            log.info("🛑 Cancelled pending companion SL-M on Dhan: %s", pos.exchange_sl_order_id)
                    except Exception as e:
                        log.error("Error cancelling SL-M order: %s", e)

                    # Send Market Sell Order
                    try:
                        headers = self._dhan_headers()
                        payload_exit = {
                            "dhanClientId": client_id,
                            "correlationId": f"exit_{pos.trade_id}",
                            "transactionType": "SELL",
                            "exchangeSegment": str(self.exec_cfg.get("exchange_segment", "NSE_FNO")),
                            "productType": str(self.exec_cfg.get("product_type", "INTRADAY")),
                            "orderType": "MARKET",
                            "validity": "DAY",
                            "securityId": str(pos.security_id),
                            "quantity": int(pos.quantity),
                            "price": 0.0,
                            "triggerPrice": 0.0,
                            "disclosedQuantity": 0,
                            "afterMarketOrder": False,
                            "amoTime": "OPEN",
                            "boProfitValue": 0.0,
                            "boStopLossValue": 0.0,
                        }
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            res = await client.post(f"{self.base_url}/orders", json=payload_exit, headers=headers)
                            log.info("✅ Live Market Sell Order Executed: %s", res.text[:150])
                    except Exception as e:
                        log.error("Exception sending Market Sell exit order: %s", e)

        log.info("🏁 Trade %s Closed (%s): Spot PnL: %+.1f pts | Option PnL: %+.1f pts (₹%+.1f INR)",
                 pos.trade_id, pos.status, spot_pnl_pts, option_pnl_pts, pnl_inr)

        # Persist trade to shared SQLite DB (paper_trades table)
        self._persist_to_sqlite(pos)
        return pos

    def _persist_to_sqlite(self, pos: ActivePosition) -> None:
        """Saves trade record into shared ict_smc.sqlite3 database."""
        try:
            full_db = Path(self.db_path)
            if not full_db.is_absolute():
                full_db = Path(__file__).resolve().parent.parent / self.db_path
            if full_db.exists():
                with sqlite3.connect(str(full_db)) as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO paper_trades (
                            signal_key, setup, tf, side, opened_ts, entry, sl, tp,
                            risk, rr, status, closed_ts, exit, pnl_pts,
                            notes, instrument_mode, opt_key, opt_sid, opt_entry,
                            opt_exit, opt_pnl_pts, opt_pnl_rs
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pos.trade_id,
                        "Setup #1 · Stage 2 8 EMA",
                        "5m",
                        "BUY",
                        pos.entry_ts,
                        pos.entry_spot,
                        pos.spot_sl,
                        pos.spot_target,
                        40.0,
                        1.0,
                        pos.status,
                        int(time.time()),
                        pos.exit_spot,
                        pos.pnl_pts,
                        f"Reason: {pos.exit_reason} | Execution: {self.execution_mode}",
                        "OPTIONS",
                        pos.symbol,
                        pos.security_id,
                        pos.entry_option_price,
                        pos.exit_option_price,
                        (pos.exit_option_price - pos.entry_option_price) if pos.exit_option_price else 0.0,
                        pos.pnl_inr,
                    ))
                    conn.commit()
                    log.info("💾 Trade %s persisted to shared SQLite paper_trades table", pos.trade_id)
        except Exception as e:
            log.error("Failed to persist trade to SQLite: %s", e)
