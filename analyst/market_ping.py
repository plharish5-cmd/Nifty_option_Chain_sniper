"""
analyst/market_ping.py — Real-time 5-minute Market Status & Trade Action Evaluator.

Alert-only utility reading live option chain signals, 15m/5m price action,
and Strategy 1 rules to provide clear BUY / SELL / WAIT guidance.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

# Path setup to reuse repo modules
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
import store
import strategy1 as s1

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("market_ping")

CONFIG_FILE = Path(__file__).parent / "analyst_config.json"


def load_config() -> Dict[str, Any]:
    default_cfg = {
        "ping_cadence_minutes": 5,
        "market_open": "09:15",
        "market_close": "15:30",
        "buyer_freeze_start": "11:30",
        "buyer_freeze_end": "13:15",
        "max_stop_pts": 40.0,
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                default_cfg.update(loaded)
        except Exception as e:
            logger.error(f"Failed to read {CONFIG_FILE}: {e}")
    return default_cfg


def is_in_freeze(now_time: dtime, freeze_start_str: str, freeze_end_str: str) -> bool:
    try:
        t_start = datetime.strptime(freeze_start_str, "%H:%M").time()
        t_end = datetime.strptime(freeze_end_str, "%H:%M").time()
        return t_start <= now_time <= t_end
    except Exception as e:
        logger.error(f"Error checking freeze time: {e}")
        return False


def get_market_ping(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now()
    cfg = load_config()

    res: Dict[str, Any] = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "spot": 0.0,
        "atm": 0.0,
        "regime": "UNKNOWN",
        "bias_score": 0.0,
        "s1": 0.0,
        "r1": 0.0,
        "max_pain": 0.0,
        "pcr_oi": 0.0,
        "chain_action": "NO_TRADE",
        "chain_strike": 0.0,
        "chain_premium": 0.0,
        "chain_blocked": "",
        "s1_stage": "UNKNOWN",
        "s1_ema_order": "UNKNOWN",
        "s1_state": "IDLE",
        "s1_direction": "",
        "s1_trigger": 0.0,
        "s1_stop": 0.0,
        "s1_risk_pts": 0.0,
        "s1_open_points": 0.0,
        "s1_last_alert": "",
        "in_freeze": False,
        "verdict": "WAIT_WATCH",
        "verdict_detail": "",
        "actionable_plan": "",
    }

    # 1. Option Chain Signal Log (Latest)
    try:
        log_df = store.load_log()
        if not log_df.empty:
            last_row = log_df.iloc[-1].to_dict()
            res["spot"] = float(last_row.get("spot", 0.0))
            res["atm"] = float(last_row.get("atm", 0.0))
            res["regime"] = str(last_row.get("regime", "UNKNOWN"))
            res["bias_score"] = float(last_row.get("bias_score", 0.0))
            res["s1"] = float(last_row.get("s1", 0.0))
            res["r1"] = float(last_row.get("r1", 0.0))
            res["max_pain"] = float(last_row.get("max_pain", 0.0))
            res["pcr_oi"] = float(last_row.get("pcr_oi", 0.0))
            res["chain_action"] = str(last_row.get("action", "NO_TRADE"))
            res["chain_strike"] = float(last_row.get("strike", 0.0) or 0.0)
            res["chain_premium"] = float(last_row.get("premium", 0.0) or 0.0)
            res["chain_blocked"] = str(last_row.get("blocked", "") or "")
    except Exception as e:
        logger.error(f"Error loading signal log: {e}")

    # 2. Check Midday Freeze
    in_freeze = is_in_freeze(now.time(), cfg["buyer_freeze_start"], cfg["buyer_freeze_end"])
    res["in_freeze"] = in_freeze

    # 3. Strategy 1 Evaluation from 15m candles
    c15_path = config.DATA_DIR / "candles_15m" / f"{now.strftime('%Y-%m-%d')}.csv"
    if c15_path.exists():
        try:
            c15 = pd.read_csv(c15_path)
            if not c15.empty:
                s1_res = s1.evaluate(c15, now=now)
                res["s1_stage"] = s1_res.stage
                res["s1_ema_order"] = s1_res.ema_order

                if s1_res.alerts:
                    res["s1_last_alert"] = f"[{s1_res.alerts[-1].kind}] {s1_res.alerts[-1].text}"

                # Check open signal
                op_sig = s1_res.open_signal
                arm_sig = s1_res.armed_signal

                if op_sig:
                    res["s1_state"] = "OPEN"
                    res["s1_direction"] = op_sig.direction
                    res["s1_trigger"] = op_sig.trigger
                    res["s1_stop"] = op_sig.stop
                    res["s1_risk_pts"] = abs(op_sig.trigger - op_sig.stop)
                    res["s1_open_points"] = op_sig.points if op_sig.points is not None else 0.0
                elif arm_sig:
                    res["s1_state"] = "ARMED"
                    res["s1_direction"] = arm_sig.direction
                    res["s1_trigger"] = arm_sig.trigger
                    res["s1_stop"] = arm_sig.stop
                    res["s1_risk_pts"] = abs(arm_sig.trigger - arm_sig.stop)
                else:
                    res["s1_state"] = "IDLE"
        except Exception as e:
            logger.error(f"Error evaluating Strategy 1: {e}")

    # 4. Synthesize Final Action Verdict
    spot = res["spot"]
    s_state = res["s1_state"]
    s_dir = res["s1_direction"]
    s_trigger = res["s1_trigger"]
    s_stop = res["s1_stop"]

    if s_state == "OPEN":
        res["verdict"] = f"HOLD {s_dir} (ACTIVE)"
        res["verdict_detail"] = f"Position is OPEN with trailing stop at {s_stop:,.2f}."
        res["actionable_plan"] = (
            f"Hold {s_dir} with trailing stop at {s_stop:,.2f}. "
            f"Exit immediately if 15m candle closes through stop or Doji forms at extreme."
        )
    elif s_state == "ARMED":
        # Check if spot has crossed the trigger
        if s_dir == "LONG":
            if spot >= s_trigger:
                if in_freeze:
                    res["verdict"] = "CAUTION: CE TRIGGERED (MIDDAY FREEZE)"
                    res["verdict_detail"] = (
                        f"Spot {spot:,.2f} traded above trigger {s_trigger:,.2f}, but 11:30-13:15 theta freeze is active. "
                        f"Option buying has accelerated decay."
                    )
                    res["actionable_plan"] = (
                        f"Trigger {s_trigger:,.2f} broken. If entering, use reduced size or in-the-money options. "
                        f"Strict SL at {s_stop:,.2f} ({res['s1_risk_pts']:.1f} pts risk)."
                    )
                else:
                    res["verdict"] = "BUY CE (TRIGGERED)"
                    res["verdict_detail"] = f"Spot {spot:,.2f} crossed trigger {s_trigger:,.2f}. Entry valid!"
                    res["actionable_plan"] = (
                        f"Enter CE (ATM {res['atm']:,.0f} CE). Entry: {s_trigger:,.2f}, SL: {s_stop:,.2f}. "
                        f"Risk: {res['s1_risk_pts']:.1f} pts."
                    )
            else:
                pts_away = s_trigger - spot
                res["verdict"] = "WAIT & WATCH (CE ARMED)"
                res["verdict_detail"] = (
                    f"15m candle closed above 8 EMA. CE is ARMED. Entry triggers on break of {s_trigger:,.2f} "
                    f"({pts_away:.1f} pts away). Stop: {s_stop:,.2f}."
                )
                res["actionable_plan"] = (
                    f"Do NOT enter yet. Wait for spot to cross {s_trigger:,.2f}. "
                    f"{'Note: Midday freeze active until 13:15.' if in_freeze else ''}"
                )
        elif s_dir == "SHORT":
            if spot <= s_trigger:
                if in_freeze:
                    res["verdict"] = "CAUTION: PE TRIGGERED (MIDDAY FREEZE)"
                    res["verdict_detail"] = (
                        f"Spot {spot:,.2f} traded below trigger {s_trigger:,.2f}, but 11:30-13:15 theta freeze is active."
                    )
                    res["actionable_plan"] = (
                        f"Trigger {s_trigger:,.2f} broken. Enter with strict stop at {s_stop:,.2f}."
                    )
                else:
                    res["verdict"] = "BUY PE (TRIGGERED)"
                    res["verdict_detail"] = f"Spot {spot:,.2f} crossed trigger {s_trigger:,.2f}. Entry valid!"
                    res["actionable_plan"] = (
                        f"Enter PE (ATM {res['atm']:,.0f} PE). Entry: {s_trigger:,.2f}, SL: {s_stop:,.2f}. "
                        f"Risk: {res['s1_risk_pts']:.1f} pts."
                    )
            else:
                pts_away = spot - s_trigger
                res["verdict"] = "WAIT & WATCH (PE ARMED)"
                res["verdict_detail"] = (
                    f"15m candle closed below 8 EMA. PE is ARMED. Entry triggers on break of {s_trigger:,.2f} "
                    f"({pts_away:.1f} pts away). Stop: {s_stop:,.2f}."
                )
                res["actionable_plan"] = f"Do NOT enter yet. Wait for spot to trade below {s_trigger:,.2f}."
    else:
        # IDLE Strategy 1
        if in_freeze:
            res["verdict"] = "NO TRADE (MIDDAY FREEZE & CHOP)"
            res["verdict_detail"] = (
                f"Market in {res['regime']} regime. 11:30-13:15 buyer freeze window active. "
                f"No Strategy 1 setup armed."
            )
            res["actionable_plan"] = "Preserve capital. Wait for afternoon trend expansion (after 13:15 IST)."
        else:
            res["verdict"] = "WAIT & WATCH (NO SETUP)"
            res["verdict_detail"] = f"Market in {res['regime']} regime. No 15m EMA trigger active."
            res["actionable_plan"] = f"Wait for 15m candle close beyond 8 EMA. Key levels: S1 {res['s1']:,.0f}, R1 {res['r1']:,.0f}."

    return res


def format_markdown_ping(data: Dict[str, Any]) -> str:
    freeze_badge = "⚠️ **MIDDAY FREEZE ACTIVE (11:30 - 13:15 IST)**" if data["in_freeze"] else "🟢 **MARKET HOURS ACTIVE**"
    
    verdict = data["verdict"]
    if "BUY" in verdict or "TRIGGERED" in verdict:
        badge = f"🎯 **{verdict}**"
    elif "WAIT" in verdict or "ARMED" in verdict:
        badge = f"⏳ **{verdict}**"
    else:
        badge = f"⏸️ **{verdict}**"

    lines = [
        f"### 📡 NIFTY 5-Min Market Ping — {data['timestamp']}",
        f"{badge} | {freeze_badge}",
        "",
        f"**Actionable Directive:** {data['actionable_plan']}",
        "",
        "| Metric | Current Value | Context / Meaning |",
        "| :--- | :--- | :--- |",
        f"| **Spot Price** | `{data['spot']:,.2f}` | ATM Strike: `{data['atm']:,.0f}` |",
        f"| **Strategy 1 State** | `{data['s1_state']} ({data['s1_direction'] or 'NONE'})` | `{data['s1_stage']}` |",
        f"| **Trigger Level** | `{data['s1_trigger']:,.2f}` | Stop: `{data['s1_stop']:,.2f}` (Risk: `{data['s1_risk_pts']:.1f}` pts) |",
        f"| **Chain Regime** | `{data['regime']}` | Bias Score: `{data['bias_score']:+.2f}` |",
        f"| **Option Walls** | `S1: {data['s1']:,.0f}` \| `R1: {data['r1']:,.0f}` | Max Pain: `{data['max_pain']:,.0f}` \| PCR: `{data['pcr_oi']:.2f}` |",
    ]
    if data["chain_strike"] > 0:
        lines.append(f"| **Option Pick** | `{data['chain_strike']:,.0f} {data['chain_action'].replace('BUY_', '')}` | Premium: `₹{data['chain_premium']:.2f}` |")
    if data["s1_last_alert"]:
        lines.extend(["", f"> ℹ️ *Latest Engine Alert:* {data['s1_last_alert']}"])

    return "\n".join(lines)


if __name__ == "__main__":
    ping_data = get_market_ping()
    print(format_markdown_ping(ping_data))
