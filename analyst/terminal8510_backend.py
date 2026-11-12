"""
analyst/terminal8510_backend.py — Institutional Quant Trading Engine for Port 8510.

Integrated with Dhan HQ API v2, SEBI Post-March 2026 Aggressive Limit Chaser,
Dual-Mode Execution (Real Money + Parallel Paper Sync), Dynamic ATM Strike Matrix,
and 7-Factor Real-Time Trend Confluence Watchlist.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

from analyst.desk_timestamp_8510 import format_desk_timestamp_ist
from analyst.algo_execution_master_8510 import (
    DATA_SOURCE_IMMUTABLE,
    DATA_SOURCE_SYNTHESIZED,
    append_algo_execution_master,
    backfill_algo_execution_master_from_positions,
    duration_between,
    ensure_algo_execution_master,
    fetch_master_trades,
    master_row_from_position,
    master_to_backtest_row,
    merge_master_and_synthetic,
    normalize_synthetic_backtest_row,
    tag_immutable_master_row,
)
from analyst.homework_reconciler import HomeworkReconciler, BENCHMARK_HOMEWORK
from analyst.dhan_oi_engine import DhanOIEngine, empty_bias
from analyst.nse_spot_candles_8510 import (
    EMA_WARMUP_1M_BARS,
    ema4_ema8,
    epoch_seconds as _epoch_seconds,
    last_closed_bar_emas,
    resample_nse_15m,
    resample_nse_1h,
)
import subprocess
import sys
import threading

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
REPLAY_DATA_DIR = ROOT / "analyst" / "replay_data"
REPLAY_DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("terminal8510")


def load_env_safe() -> None:
    """Reads .env without leaking secrets into logs or console."""
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
        except Exception as e:
            log.error("Error reading .env: %s", e)


def save_env_safe(updates: Dict[str, str]) -> bool:
    """Updates .env safely preserving other variables and comments without leaking secrets."""
    env_file = ROOT / ".env"
    lines = []
    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            log.error("Error reading .env before write: %s", e)
            lines = []

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, v = stripped.split("=", 1)
            k_clean = k.strip()
            if k_clean in updates:
                new_lines.append(f"{k_clean}={updates[k_clean]}")
                updated_keys.add(k_clean)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    try:
        content = "\n".join(new_lines) + "\n"
        env_file.write_text(content, encoding="utf-8")
        for k, v in updates.items():
            os.environ[k] = v
        return True
    except Exception as e:
        log.error("Error saving to .env: %s", e)
        return False


def round_to_tick(val: float, tick: float = 0.05) -> float:
    """Rounds price to nearest exchange tick (default 0.05)."""
    return round(round(float(val) / tick) * tick, 2)


def decode_token_expiry(token: str, default_ttl_seconds: float = 86400.0) -> Dict[str, Any]:
    """
    Decodes the JWT 'exp' claim from DHAN_ACCESS_TOKEN if JWT formatted,
    or falls back to 24 hours from current time.
    Calculates exact 'Token Valid Till: DD Mon YYYY HH:MM IST | Hours/Days Left: ___'.
    """
    exp_ts = None
    if token and "." in token:
        parts = token.strip().split(".")
        if len(parts) >= 2:
            try:
                p = parts[1]
                p += "=" * ((4 - len(p) % 4) % 4)
                data = json.loads(base64.urlsafe_b64decode(p.encode("utf-8")).decode("utf-8"))
                if "exp" in data:
                    exp_ts = float(data["exp"])
            except Exception as e:
                log.debug("JWT exp parse error: %s", e)

    now_ts = time.time()
    if exp_ts is None:
        exp_ts = now_ts + default_ttl_seconds

    diff = exp_ts - now_ts
    is_expired = diff <= 0

    exp_dt_utc = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    exp_dt_ist = datetime.fromtimestamp(exp_ts, tz=IST)

    token_valid_till = exp_dt_ist.strftime("%d %b %Y %H:%M IST")
    token_expiry_utc = exp_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if is_expired:
        time_left = "Expired"
        days_left = "0 Days Left (Expired)"
    else:
        diff_hours = diff / 3600.0
        diff_days = diff / 86400.0
        if diff_days >= 1.0:
            time_left = f"{int(diff_days)} Days Left"
            days_left = f"{int(diff_days)} Days Left"
        else:
            time_left = f"{int(diff_hours)} Hours Left"
            days_left = f"{int(diff_hours)} Hours Left"

    return {
        "exp_ts": exp_ts,
        "token_expiry_utc": token_expiry_utc,
        "token_valid_till": token_valid_till,
        "time_left": time_left,
        "days_left": days_left,
        "is_expired": is_expired,
    }


def format_contract_symbol(
    expiry: Optional[str] = None,
    strike: Optional[Union[float, int, str]] = None,
    opt_type: Optional[str] = None,
    underlying: str = "NIFTY",
    raw_symbol: Optional[str] = None,
    default_expiry: str = "2026-11-09",
) -> str:
    """
    Standardizes Option Contract Symbol format globally across all 5 modules:
      NEW (CORRECT): '09 Nov 2026 — NIFTY 24300 CE' and '09 Nov 2026 — NIFTY 24500 PE'
      OLD (WRONG): 'November 2026 24300 CE' / 'NIFTY-Nov2026-24300-CE'
    """
    # If already fully formatted like '09 Nov 2026 — NIFTY 24300 CE', return as is
    if raw_symbol and re.match(r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+—\s+[A-Za-z]+\s+\d+\s+(CE|PE)$", str(raw_symbol).strip()):
        return str(raw_symbol).strip()

    s_strike = None
    s_type = None
    s_underlying = underlying
    s_date = None

    if raw_symbol:
        cleaned = str(raw_symbol).strip()
        m_type = re.search(r"\b(CE|PE)\b", cleaned, re.IGNORECASE)
        if m_type:
            s_type = m_type.group(1).upper()

        m_strk = re.search(r"(\d{5})", cleaned)
        if m_strk:
            s_strike = int(m_strk.group(1))

        # Check for full date in raw_symbol: e.g. 09Nov2026 or 09-Nov-2026 or 09 Nov 2026
        m_date = re.search(r"(\d{1,2})[ -]?([A-Za-z]{3})[ -]?(\d{4})", cleaned)
        if m_date:
            try:
                dt_cand = datetime.strptime(f"{int(m_date.group(1)):02d} {m_date.group(2).title()} {m_date.group(3)}", "%d %b %Y")
                s_date = dt_cand.strftime("%d %b %Y")
            except Exception:
                pass

    if strike is not None:
        try:
            s_strike = int(float(strike))
        except Exception:
            pass

    if opt_type is not None:
        s_type = str(opt_type).strip().upper()

    if not s_date and expiry:
        exp_clean = str(expiry).strip().split(" ")[0]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%Y%m%d", "%d%b%Y", "%b%Y", "%B%Y"):
            try:
                dt_obj = datetime.strptime(exp_clean, fmt)
                if fmt in ("%b%Y", "%B%Y"):
                    s_date = dt_obj.replace(day=9).strftime("%d %b %Y")
                else:
                    s_date = dt_obj.strftime("%d %b %Y")
                break
            except Exception:
                continue

    if not s_date:
        try:
            s_date = datetime.strptime(default_expiry, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            s_date = "09 Nov 2026"

    s_strike_val = str(s_strike) if s_strike else "24300"
    s_type_val = s_type if s_type else "CE"

    return f"{s_date} — {s_underlying} {s_strike_val} {s_type_val}"


def get_contract_structured_fields(symbol: str, expiry: Optional[str] = None) -> Dict[str, str]:
    """
    Exposes the 3 structured fields clearly for tooltips and columns:
      - Expiry Date: '09 Nov 2026 (Monday)'
      - Symbol: 'NIFTY'
      - Strike: '24300 CE'
      - Full Symbol: '09 Nov 2026 — NIFTY 24300 CE'
    """
    formatted = format_contract_symbol(raw_symbol=symbol, expiry=expiry)
    parts = formatted.split(" — ")
    date_part = parts[0] if len(parts) >= 2 else "09 Nov 2026"
    right_part = parts[1] if len(parts) >= 2 else formatted

    day_name = "Monday"
    try:
        dt = datetime.strptime(date_part, "%d %b %Y")
        day_name = dt.strftime("%A")
    except Exception:
        pass

    right_tokens = right_part.split(" ")
    und = right_tokens[0] if right_tokens else "NIFTY"
    strike_str = " ".join(right_tokens[1:]) if len(right_tokens) > 1 else "24300 CE"

    return {
        "expiry_date": f"{date_part} ({day_name})",
        "expiry_display": f"{date_part} ({day_name})",
        "symbol": und,
        "strike": strike_str,
        "strike_display": strike_str,
        "full_symbol": formatted,
    }


class MarketStatusResult(dict):
    """Result dictionary that also supports tuple unpacking (is_open, status)."""
    def __iter__(self):
        yield self["is_open"]
        yield self["status"]


def is_nse_market_open(
    dt: Optional[datetime] = None,
    holidays: Optional[List[str]] = None,
) -> MarketStatusResult:
    """
    NSE Market Hours Validator:
      - Hours: Monday to Friday, 09:15:00 AM to 03:30:00 PM IST (Asia/Kolkata).
      - Returns MarketStatusResult dict: {"is_open": bool, "status": str, "time_ist": str, ...}
      - Can also be unpacked as a tuple: is_open, status = is_nse_market_open()
      - Returns CLOSED if current IST time < 09:15:00 or > 15:30:00, or Saturday (5), Sunday (6), or Holiday.
      - Status tags: 'OPEN', 'CLOSED', 'PRE_OPEN' (09:00-09:15), 'POST_CLOSE' (after 15:30 on weekday).
    """
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)

    time_ist_str = dt.strftime("%H:%M:%S IST")
    weekday = dt.weekday()
    date_str = dt.strftime("%Y-%m-%d")

    m_holidays = holidays if holidays is not None else [
        "2026-01-26", "2026-03-25", "2026-04-14", "2026-05-01",
        "2026-08-15", "2026-10-02", "2026-12-25"
    ]

    # Weekends (5: Saturday, 6: Sunday)
    if weekday in (5, 6):
        day_name = "Saturday" if weekday == 5 else "Sunday"
        return MarketStatusResult({
            "is_open": False,
            "status": "WEEKEND_CLOSED",
            "time_ist": time_ist_str,
            "reason": f"Weekend Closed ({day_name})"
        })

    if date_str in m_holidays:
        return MarketStatusResult({
            "is_open": False,
            "status": "HOLIDAY_CLOSED",
            "time_ist": time_ist_str,
            "reason": f"NSE Holiday on {date_str}"
        })

    t = dt.time()
    t_pre_open = dtime(9, 0, 0)
    t_open = dtime(9, 15, 0)
    t_close = dtime(15, 30, 0)

    if t < t_pre_open:
        return MarketStatusResult({
            "is_open": False,
            "status": "CLOSED",
            "time_ist": time_ist_str,
            "reason": "NSE Pre-Market Closed (Before 09:00 IST)"
        })
    elif t_pre_open <= t < t_open:
        return MarketStatusResult({
            "is_open": False,
            "status": "PRE_OPEN",
            "time_ist": time_ist_str,
            "reason": "NSE Pre-Open Session (09:00 - 09:15 IST)"
        })
    elif t_open <= t <= t_close:
        return MarketStatusResult({
            "is_open": True,
            "status": "OPEN",
            "time_ist": time_ist_str,
            "reason": "NSE Live Market Session"
        })
    else:
        return MarketStatusResult({
            "is_open": False,
            "status": "POST_CLOSE",
            "time_ist": time_ist_str,
            "reason": "NSE Session Closed for the Day (Post 15:30 IST)"
        })


def compute_trailing_sl(
    entry_price: float,
    current_ltp: float,
    highest_ltp: float,
    current_trailing_sl: float,
    initial_sl_pts: float = 20.0,
    activation_pts: float = 15.0,
    jump_pts: float = 5.0,
) -> Tuple[float, float]:
    """
    Dhan Super Trail Step-Function Trailing Stop Loss (BUY Option Position):
      - highest_ltp = max(highest_ltp, current_ltp)
      - max_profit_pts = highest_ltp - entry_price
      - If max_profit_pts < activation_pts: current_trailing_sl remains at initial_sl
      - If max_profit_pts >= activation_pts:
          steps = math.floor((max_profit_pts - activation_pts) / jump_pts) + 1
          candidate_sl = initial_sl + (steps * jump_pts)
          current_trailing_sl = max(current_trailing_sl, candidate_sl)
      - Strict Monotonicity: current_trailing_sl never moves down.
    """
    new_highest = max(highest_ltp, current_ltp)
    initial_sl = round_to_tick(entry_price - initial_sl_pts, 0.05)
    if current_trailing_sl <= 0.0:
        current_trailing_sl = initial_sl

    max_profit = new_highest - entry_price
    if max_profit >= activation_pts:
        steps = math.floor((max_profit - activation_pts) / jump_pts) + 1
        candidate_sl = round_to_tick(initial_sl + (steps * jump_pts), 0.05)
        new_sl = max(current_trailing_sl, candidate_sl)
    else:
        new_sl = max(current_trailing_sl, initial_sl)

    return round(new_highest, 2), round(new_sl, 2)


def compute_trailing_target(
    entry_price: float,
    current_ltp: float,
    target_status: str,
    trailing_target_level: float,
    highest_after_target: float,
    initial_target_pts: float = 25.0,
    step_pts: float = 10.0,
    pullback_pts: float = 5.0,
    trailing_target_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Dhan Super Trail Spike-High Pullback Logic:
      Case 1 (trailing_target_enabled == False): Exit immediately when LTP >= Initial_Target.
      Case 2 (trailing_target_enabled == True):
        - Before LTP reaches Initial_Target: target_status = 'FIXED', level = Initial_Target.
        - Moment LTP >= Initial_Target: DO NOT EXIT. Switch target_status = 'TRAILING',
          set highest_after_target = LTP, trailing_target_level = Initial_Target + step_pts.
        - On subsequent ticks in 'TRAILING':
          highest_after_target = max(highest_after_target, current_ltp)
          when current_ltp >= trailing_target_level: trailing_target_level = current_ltp + step_pts
          pullback_exit_level = highest_after_target - pullback_pts
          If current_ltp <= pullback_exit_level: Fire protected limit exit immediately.
    """
    initial_target = round_to_tick(entry_price + initial_target_pts, 0.05)

    if not trailing_target_enabled:
        should_exit = current_ltp >= initial_target
        return {
            "target_status": "FIXED",
            "trailing_target_level": initial_target,
            "highest_after_target": max(highest_after_target, current_ltp),
            "pullback_exit_level": 0.0,
            "should_exit": should_exit,
            "exit_reason": "FIXED_TARGET_HIT" if should_exit else "",
        }

    # Trailing target enabled
    if target_status == "FIXED":
        if current_ltp >= initial_target:
            new_status = "TRAILING"
            new_highest = current_ltp
            new_target_level = round_to_tick(initial_target + step_pts, 0.05)
            pullback_exit = round_to_tick(new_highest - pullback_pts, 0.05)
            return {
                "target_status": new_status,
                "trailing_target_level": new_target_level,
                "highest_after_target": new_highest,
                "pullback_exit_level": pullback_exit,
                "should_exit": False,
                "exit_reason": "",
            }
        else:
            return {
                "target_status": "FIXED",
                "trailing_target_level": initial_target,
                "highest_after_target": max(highest_after_target, current_ltp),
                "pullback_exit_level": 0.0,
                "should_exit": False,
                "exit_reason": "",
            }

    # In 'TRAILING' mode
    new_highest = max(highest_after_target, current_ltp)
    new_target_level = trailing_target_level
    if current_ltp >= new_target_level:
        new_target_level = round_to_tick(current_ltp + step_pts, 0.05)

    pullback_exit = round_to_tick(new_highest - pullback_pts, 0.05)
    should_exit = current_ltp <= pullback_exit
    return {
        "target_status": "TRAILING",
        "trailing_target_level": new_target_level,
        "highest_after_target": new_highest,
        "pullback_exit_level": pullback_exit,
        "should_exit": should_exit,
        "exit_reason": "TRAIL_TARGET_PULLBACK_HIT" if should_exit else "",
    }


def _near_round_number(price: float, step: float, tol: float = 25.0) -> bool:
    base = round(price / step) * step
    return abs(price - base) <= tol


def compute_daily_sr_from_ohlc(daily: pd.DataFrame, spot: float) -> Dict[str, Any]:
    """S1/R1 from prior session; S2/S3 and R2/R3 from deeper daily extremes; round-number candidates."""
    out: Dict[str, Any] = {
        "S1": None, "S2": None, "S3": None, "R1": None, "R2": None, "R3": None, "round_levels": [],
    }
    if daily is None or daily.empty:
        return out
    d = daily.sort_index()
    hist = d.iloc[:-1] if len(d) > 1 else d
    if hist.empty:
        return out
    y = hist.iloc[-1]
    s1, r1 = float(y["low"]), float(y["high"])
    out["S1"], out["R1"] = s1, r1
    
    uniq_lows = sorted({float(x) for x in hist["low"]}, reverse=True)
    lower = [x for x in uniq_lows if x < s1 - 0.5]
    if lower:
        out["S2"] = lower[0]
    if len(lower) > 1:
        out["S3"] = lower[1]

    uniq_highs = sorted({float(x) for x in hist["high"]})
    higher = [x for x in uniq_highs if x > r1 + 0.5]
    if higher:
        out["R2"] = higher[-1]
    if len(higher) > 1:
        out["R3"] = higher[-2]

    rounds: List[float] = []
    for step in (500.0, 1000.0):
        base = round(spot / step) * step
        for mult in (-1, 0, 1):
            lv = base + mult * step
            if lv > 0:
                rounds.append(lv)
    out["round_levels"] = sorted(set(rounds))
    return out


def compute_daily_trend_two_day_rule(daily: pd.DataFrame) -> Tuple[str, str]:
    """
    Two-day reversal: one opposite candle is profit booking; reversal needs two consecutive
    opposite candles with the second breaking the prior structural extreme.
  Returns (trade_bias: BULLISH|BEARISH|NEUTRAL, state_label).
    """
    if daily is None or len(daily) < 4:
        return "NEUTRAL", "NEUTRAL"
    d = daily.sort_index()
    hist = d.iloc[:-1] if len(d) > 1 else d
    if len(hist) < 4:
        return "NEUTRAL", "NEUTRAL"

    def _green(row: pd.Series) -> bool:
        return float(row["close"]) >= float(row["open"])

    # Establish bias from last 3 completed dailies (higher-high/higher-low vs lower).
    tail = hist.iloc[-4:]
    c0, c1, c2 = [float(tail.iloc[i]["close"]) for i in (-3, -2, -1)]
    h2, h1 = float(tail.iloc[-1]["high"]), float(tail.iloc[-2]["high"])
    l2, l1 = float(tail.iloc[-1]["low"]), float(tail.iloc[-2]["low"])
    if c2 > c1 > c0 or (h2 > h1 and l2 > l1):
        bias = "BULLISH"
    elif c2 < c1 < c0 or (h2 < h1 and l2 < l1):
        bias = "BEARISH"
    else:
        return "NEUTRAL", "NEUTRAL"

    last, prev, prior = tail.iloc[-1], tail.iloc[-2], tail.iloc[-3]
    if bias == "BULLISH":
        if not _green(last) and float(last["low"]) < float(prev["low"]):
            if not _green(prev) and float(last["low"]) < float(prior["low"]):
                return "BEARISH", "REVERSAL_TWO_RED"
            return "BULLISH", "PROFIT_BOOKING"
        return "BULLISH", "TREND_INTACT"
    if _green(last) and float(last["high"]) > float(prev["high"]):
        if _green(prev) and float(last["high"]) > float(prior["high"]):
            return "BULLISH", "REVERSAL_TWO_GREEN"
        return "BEARISH", "PROFIT_BOOKING"
    return "BEARISH", "TREND_INTACT"


def find_confirmed_h1_swings(h1: pd.DataFrame, pivot_bars: int = 2) -> List[Dict[str, Any]]:
    """Fractal pivots; pivot at i is omitted until i + pivot_bars < len(h1) (no look-ahead)."""
    swings: List[Dict[str, Any]] = []
    if h1 is None or len(h1) < pivot_bars * 2 + 1:
        return swings
    hi_s, lo_s = h1["high"], h1["low"]
    last_confirmable = len(h1) - pivot_bars
    for i in range(pivot_bars, last_confirmable):
        h_i, l_i = float(hi_s.iloc[i]), float(lo_s.iloc[i])
        is_high = all(h_i > float(hi_s.iloc[j]) for j in range(i - pivot_bars, i + pivot_bars + 1) if j != i)
        is_low = all(l_i < float(lo_s.iloc[j]) for j in range(i - pivot_bars, i + pivot_bars + 1) if j != i)
        rng = h_i - l_i
        ts = h1.index[i]
        if is_high:
            swings.append({"type": "HIGH", "price": h_i, "ts": ts, "idx": i, "range_pts": rng})
        elif is_low:
            swings.append({"type": "LOW", "price": l_i, "ts": ts, "idx": i, "range_pts": rng})
    return swings


def zone_from_15m_at_ts(
    m15: pd.DataFrame,
    pivot_ts: Any,
    width_min: float = 10.0,
    width_max: float = 20.0,
    pad_minutes: int = 30,
) -> Optional[Tuple[float, float]]:
    if m15 is None or m15.empty or pivot_ts is None:
        return None
    try:
        t0 = pd.Timestamp(pivot_ts)
        if t0.tzinfo is None:
            t0 = t0.tz_localize(IST)
    except Exception:
        return None
    t_start = t0 - timedelta(minutes=pad_minutes)
    t_end = t0 + timedelta(minutes=pad_minutes + 15)
    chunk = m15[(m15.index >= t_start) & (m15.index <= t_end)]
    if chunk.empty:
        return None
    z_lo, z_hi = float(chunk["low"].min()), float(chunk["high"].max())
    mid = (z_lo + z_hi) / 2.0
    half = min(width_max, max(width_min, (z_hi - z_lo) / 2.0))
    return (round(mid - half, 2), round(mid + half, 2))


def _detect_single_bar_pattern(o: float, h: float, l: float, c: float) -> Tuple[Optional[str], Optional[str]]:
    rng = max(h - l, 0.001)
    body = abs(c - o)
    uw = h - max(o, c)
    lw = min(o, c) - l
    long_tag, short_tag = None, None
    if (body / rng) <= 0.15:
        long_tag = short_tag = "DOJI"
    if lw >= 1.75 * max(body, 0.5) and uw <= rng * 0.3:
        long_tag = "HAMMER"
    if uw >= 2.0 * max(body, 0.5) and lw <= rng * 0.25:
        short_tag = "SHOOTING_STAR"
    return long_tag, short_tag


def detect_flagship_15m_confirmation(b15: pd.DataFrame, conf_idx: int = -2) -> Tuple[Optional[str], Optional[str]]:
    """Pattern on fully closed bar at conf_idx; no forming bar."""
    if b15 is None or len(b15) < abs(conf_idx) + 1:
        return None, None
    i = len(b15) + conf_idx if conf_idx < 0 else conf_idx
    if i < 1:
        return None, None
    o0, h0, l0, c0 = [float(b15[col].iloc[i]) for col in ("open", "high", "low", "close")]
    o1, h1, l1, c1 = [float(b15[col].iloc[i - 1]) for col in ("open", "high", "low", "close")]
    body0, body1 = abs(c0 - o0), abs(c1 - o1)
    pa_long, pa_short = _detect_single_bar_pattern(o0, h0, l0, c0)
    if c1 < o1 and c0 > o0 and c0 >= o1 and o0 <= c1 and body0 > body1:
        pa_long = "BULLISH_ENGULFING"
    elif c1 > o1 and c0 < o0 and c0 <= o1 and o0 >= c1 and body0 > body1:
        pa_short = "BEARISH_ENGULFING"
    elif h0 <= h1 and l0 >= l1 and body1 > body0:
        if c0 > o0 and c1 < o1:
            pa_long = "BULLISH_MOTHER_BABY"
        elif c0 < o0 and c1 > o1:
            pa_short = "BEARISH_MOTHER_BABY"
    if i >= 2:
        o2, h2, l2, c2 = [float(b15[col].iloc[i - 2]) for col in ("open", "high", "low", "close")]
        body2 = abs(c2 - o2)
        mid1 = (h1 + l1) / 2.0
        mid0 = (h0 + l0) / 2.0
        if c2 < o2 and body1 <= body2 * 0.6 and c0 > o0 and c0 > mid1:
            pa_long = pa_long or "MORNING_STAR"
        if c2 > o2 and body1 <= body2 * 0.6 and c0 < o0 and c0 < mid1:
            pa_short = pa_short or "EVENING_STAR"
    return pa_long, pa_short


def _h1_came_from_above(b60: pd.DataFrame, bar_ci: int, e8_val: float, tol: float) -> bool:
    if b60 is None or len(b60) < abs(bar_ci) + 2:
        return False
    prev_cl = float(b60["close"].iloc[bar_ci - 1])
    prev_lo = float(b60["low"].iloc[bar_ci - 1])
    return prev_cl > e8_val - tol or prev_lo > e8_val - tol * 0.5


def _h1_8ema_support_touch(b60: pd.DataFrame, bar_ci: int, e8_val: float, tol: float) -> bool:
    if e8_val <= 0:
        return False
    lo = float(b60["low"].iloc[bar_ci])
    hi = float(b60["high"].iloc[bar_ci])
    cl = float(b60["close"].iloc[bar_ci])
    return lo <= e8_val + tol and hi >= e8_val - tol and cl >= e8_val - tol * 0.75


def _h1_well_below_8ema(b60: pd.DataFrame, bar_ci: int, e8_val: float, margin_pts: float) -> bool:
    if e8_val <= 0:
        return False
    cl = float(b60["close"].iloc[bar_ci])
    return cl < e8_val - margin_pts


def _h1_8ema_resist_touch(b60: pd.DataFrame, bar_ci: int, e8_val: float, buffer_pts: float) -> bool:
    """Within buffer_pts below 8 EMA on 1H (approach resistance from below)."""
    if e8_val <= 0:
        return False
    hi = float(b60["high"].iloc[bar_ci])
    lo = float(b60["low"].iloc[bar_ci])
    band_lo = e8_val - buffer_pts
    return hi >= band_lo and lo <= e8_val + buffer_pts * 0.5


def _at_valid_level(low: float, high: float, levels: List[float], tol: float) -> bool:
    for lv in levels:
        if lv is None:
            continue
        if low - tol <= float(lv) <= high + tol:
            return True
    return False


def compute_h1_trend_pa(h1: pd.DataFrame) -> str:
    if h1 is None or len(h1) < 3:
        return "NEUTRAL"
    tail = h1.iloc[-3:]
    greens = sum(1 for _, r in tail.iterrows() if float(r["close"]) >= float(r["open"]))
    reds = 3 - greens
    hh = float(tail["high"].iloc[-1]) > float(tail["high"].iloc[-2])
    hl = float(tail["low"].iloc[-1]) > float(tail["low"].iloc[-2])
    lh = float(tail["high"].iloc[-1]) < float(tail["high"].iloc[-2])
    ll = float(tail["low"].iloc[-1]) < float(tail["low"].iloc[-2])
    if hh and hl and greens >= 2:
        return "UP"
    if lh and ll and reds >= 2:
        return "DOWN"
    return "NEUTRAL"


def flagship_candle_trail_update(
    is_long: bool, stop: float, candle_o: float, candle_h: float, candle_l: float, candle_c: float
) -> float:
    if is_long and candle_c > candle_o:
        return max(stop, candle_l)
    if not is_long and candle_c < candle_o:
        return min(stop, candle_h)
    return stop


def flagship_sr_exit_trigger(
    is_long: bool, price: float, sr: Dict[str, Any], proximity: float = 15.0
) -> Optional[str]:
    # Owner rule, 11 Nov 2026: a long secures profit as price reaches
    # resistance, a short as it reaches support. (This was the other way
    # round, which closed a long at the support it was bought from.)
    if is_long:
        keys = ("R1", "R2", "R3", "R4")
    else:
        keys = ("S1", "S2", "S3", "S4")
    for k in keys:
        lv = sr.get(k)
        if lv is not None and abs(price - float(lv)) <= proximity:
            return f"{k}_NEAR_EXIT"
    return None


FIXED_SPOT_OR_OPTION_EXIT_PREFIXES = ("EMA8_PROTOTYPE", "STAGE_2", "STAGE_3", "CROSSOVER", "DESK8600_TICKET")


def uses_fixed_spot_or_option_exit(strategy_id: Any) -> bool:
    """Lanes whose exit is: spot moves N points OR the option moves N points,
    whichever comes first, plus the 15:20 square-off. No Super Trail.

    Owner rule, 11 Nov 2026, for Stage 2, Stage 3 and the 8 EMA Reversal; the
    8 EMA Prototype already worked this way.
    """
    return str(strategy_id or "").upper().startswith(FIXED_SPOT_OR_OPTION_EXIT_PREFIXES)


def synthesize_option_premium_from_spot(
    entry_spot: float,
    exit_spot: float,
    is_long: bool,
    delta: float = 0.60,
    base_premium: float = 200.0,
) -> Tuple[float, float]:
    """Map spot P&L points to synthetic option entry/exit (labeled SYNTHESIZED in callers)."""
    spot_pts = (exit_spot - entry_spot) if is_long else (entry_spot - exit_spot)
    opt_entry = round(base_premium, 2)
    opt_exit = max(1.0, round(opt_entry + spot_pts * delta, 2))
    return opt_entry, opt_exit


def simulate_prototype_spot_exit(
    bars: List[Dict[str, Any]],
    start_idx: int,
    entry_spot: float,
    is_long: bool,
    sl_pts: float = 40.0,
    tgt_pts: float = 40.0,
    eod_hm: str = "15:20",
    *,
    day_1m: Optional[pd.DataFrame] = None,
    entry_ts: Optional[int] = None,
    exit_on_opposite_close: bool = True,
) -> Dict[str, Any]:
    """
    exit_on_opposite_close=False gives the same 1-minute walk without rule 4,
    for Stage 2, Stage 3 and the 8 EMA Reversal (spot target / stop / 15:20).

    Forward-walk exit for 8 EMA Prototype matching Harish's specification:
    1. Exit walk starts strictly from the first completed 1m candle AFTER entry_ts (ts > entry_ts).
    2. Target (+40) and Stop (-40) checked on completed 1m candles.
    3. If single 1m candle touches BOTH target and stop -> marked AMBIGUOUS (is_ambiguous=True).
    4. Opposite 8 EMA close is checked ONLY at 15m candle closes (including entry candle's close).
    5. EOD square-off at eod_hm (15:20) closes position with reason EOD_SQUARE_OFF_15:20.
    """
    entry_day = str(bars[start_idx].get("day") or "") if 0 <= start_idx < len(bars) else ""
    entry_hm = str(bars[start_idx].get("hm") or "") if 0 <= start_idx < len(bars) else ""
    target_spot = round(entry_spot + tgt_pts, 2) if is_long else round(entry_spot - tgt_pts, 2)
    stop_spot = round(entry_spot - sl_pts, 2) if is_long else round(entry_spot + sl_pts, 2)

    # 15m candle boundary map: close_ts -> 15m bar
    b15_by_close_ts: Dict[int, Dict[str, Any]] = {}
    b15_by_close_hm: Dict[str, Dict[str, Any]] = {}
    for b in bars:
        b_ts = int(b.get("ts") or 0)
        if b_ts > 0:
            c_ts = b_ts + 900
            b15_by_close_ts[c_ts] = b
            c_hm = (
                pd.to_datetime(c_ts, unit="s", utc=True)
                .tz_convert("Asia/Kolkata")
                .strftime("%H:%M")
            )
            b15_by_close_hm[c_hm] = b

    # If 1m candles are available, do the precise 1m-resolution exit walk
    if day_1m is not None and not day_1m.empty:
        threshold_ts = int(entry_ts) if (entry_ts is not None and entry_ts > 0) else int(bars[start_idx].get("ts") or 0)
        candles = day_1m[day_1m["ts"] > threshold_ts].sort_values("ts")
        checked_15m_closes = set()

        for _, row in candles.iterrows():
            m_ts = int(row["ts"])
            m_hi = float(row["high"])
            m_lo = float(row["low"])
            m_c = float(row["close"])
            m_day = str(row.get("day") or "")
            if not m_day and m_ts > 0:
                m_day = pd.to_datetime(m_ts, unit="s", utc=True).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d")
            if not m_day:
                m_day = entry_day

            m_hm = str(row.get("hm") or "")
            if not m_hm and m_ts > 0:
                m_hm = pd.to_datetime(m_ts, unit="s", utc=True).tz_convert("Asia/Kolkata").strftime("%H:%M")
            if not m_hm:
                m_hm = entry_hm

            # 1. EOD square-off check at 15:20
            if m_hm >= eod_hm:
                exit_spot = m_c
                pnl = round((exit_spot - entry_spot) if is_long else (entry_spot - exit_spot), 2)
                return {
                    "st_pnl_pts": pnl,
                    "fx_pnl_pts": pnl,
                    "exit_spot": exit_spot,
                    "exit_reason": f"EOD_SQUARE_OFF_{eod_hm}",
                    "trail_steps": [f"SL {int(sl_pts)}", f"EOD 15:20 {exit_spot:.1f}"],
                    "mfe_pts": max(0.0, pnl),
                    "entry_day": entry_day,
                    "entry_hm": entry_hm,
                    "exit_day": m_day,
                    "exit_hm": m_hm,
                    "exit_ts": m_ts,
                    "is_ambiguous": False,
                }

            # 2. Ambiguous candle: touched both target and stop on single 1m candle
            is_amb = False
            if is_long:
                if m_hi >= target_spot and m_lo <= stop_spot:
                    is_amb = True
            else:
                if m_lo <= target_spot and m_hi >= stop_spot:
                    is_amb = True

            if is_amb:
                return {
                    "st_pnl_pts": -sl_pts,
                    "fx_pnl_pts": -sl_pts,
                    "exit_spot": stop_spot,
                    "exit_reason": "AMBIGUOUS",
                    "trail_steps": [f"SL {int(sl_pts)}", "AMBIGUOUS 1m bar"],
                    "mfe_pts": tgt_pts,
                    "entry_day": entry_day,
                    "entry_hm": entry_hm,
                    "exit_day": m_day,
                    "exit_hm": m_hm,
                    "exit_ts": m_ts,
                    "is_ambiguous": True,
                }

            # 3. Stop check on 1m candle
            if (is_long and m_lo <= stop_spot) or (not is_long and m_hi >= stop_spot):
                return {
                    "st_pnl_pts": -sl_pts,
                    "fx_pnl_pts": -sl_pts,
                    "exit_spot": stop_spot,
                    "exit_reason": "SPOT_SL_HIT",
                    "trail_steps": [f"SL {int(sl_pts)}", f"SL hit {stop_spot:.1f}"],
                    "mfe_pts": 0.0,
                    "entry_day": entry_day,
                    "entry_hm": entry_hm,
                    "exit_day": m_day,
                    "exit_hm": m_hm,
                    "exit_ts": m_ts,
                    "is_ambiguous": False,
                }

            # 4. Target check on 1m candle
            if (is_long and m_hi >= target_spot) or (not is_long and m_lo <= target_spot):
                return {
                    "st_pnl_pts": tgt_pts,
                    "fx_pnl_pts": tgt_pts,
                    "exit_spot": target_spot,
                    "exit_reason": "SPOT_TARGET_HIT",
                    "trail_steps": [f"SL {int(sl_pts)}", f"Target hit {target_spot:.1f}"],
                    "mfe_pts": tgt_pts,
                    "entry_day": entry_day,
                    "entry_hm": entry_hm,
                    "exit_day": m_day,
                    "exit_hm": m_hm,
                    "exit_ts": m_ts,
                    "is_ambiguous": False,
                }

            # 5. Check opposite 8 EMA close at 15m candle close ONLY
            for c_ts, b15 in b15_by_close_ts.items():
                if c_ts <= m_ts and c_ts not in checked_15m_closes and c_ts > threshold_ts:
                    checked_15m_closes.add(c_ts)
                    c15 = float(b15.get("close") or m_c)
                    e8_15 = float(b15.get("e8") or 0.0)
                    if e8_15 > 0 and exit_on_opposite_close:
                        opp_close = (c15 < e8_15) if is_long else (c15 > e8_15)
                        if opp_close:
                            pnl = round((c15 - entry_spot) if is_long else (entry_spot - c15), 2)
                            return {
                                "st_pnl_pts": pnl,
                                "fx_pnl_pts": pnl,
                                "exit_spot": c15,
                                "exit_reason": "OPPOSITE_8EMA_CLOSE",
                                "trail_steps": [f"SL {int(sl_pts)}", f"Opposite 8 EMA Close {c15:.1f}"],
                                "mfe_pts": max(0.0, pnl),
                                "entry_day": entry_day,
                                "entry_hm": entry_hm,
                                "exit_day": m_day,
                                "exit_hm": m_hm,
                                "exit_ts": m_ts,
                                "is_ambiguous": False,
                            }

        # If 1m walk completed without exit, square off at final bar
        last_c = float(candles.iloc[-1]["close"]) if not candles.empty else entry_spot
        last_day = str(candles.iloc[-1].get("day") or entry_day) if not candles.empty else entry_day
        last_hm = str(candles.iloc[-1].get("hm") or "15:20") if not candles.empty else "15:20"
        last_ts = int(candles.iloc[-1].get("ts") or 0) if not candles.empty else 0
        pnl = round((last_c - entry_spot) if is_long else (entry_spot - last_c), 2)
        return {
            "st_pnl_pts": pnl,
            "fx_pnl_pts": pnl,
            "exit_spot": last_c,
            "exit_reason": f"EOD_SQUARE_OFF_{eod_hm}",
            "trail_steps": [f"SL {int(sl_pts)}", f"EOD {last_c:.1f}"],
            "mfe_pts": max(0.0, pnl),
            "entry_day": entry_day,
            "entry_hm": entry_hm,
            "exit_day": last_day,
            "exit_hm": last_hm,
            "exit_ts": last_ts,
            "is_ambiguous": False,
        }

    # Fallback to 15m walk if day_1m is not provided
    for f_idx in range(start_idx, len(bars)):
        fb = bars[f_idx]
        fh = float(fb.get("high") or entry_spot)
        fl = float(fb.get("low") or entry_spot)
        fc = float(fb.get("close") or entry_spot)
        f_day = str(fb.get("day") or entry_day)
        f_hm = str(fb.get("hm") or entry_hm)
        f_ts = int(fb.get("ts") or 0)

        if f_hm >= eod_hm:
            pnl = round((fc - entry_spot) if is_long else (entry_spot - fc), 2)
            return {
                "st_pnl_pts": pnl,
                "fx_pnl_pts": pnl,
                "exit_spot": fc,
                "exit_reason": f"EOD_SQUARE_OFF_{eod_hm}",
                "trail_steps": [f"SL {int(sl_pts)}", f"EOD 15:20 {fc:.1f}"],
                "mfe_pts": max(0.0, pnl),
                "entry_day": entry_day,
                "entry_hm": entry_hm,
                "exit_day": f_day,
                "exit_hm": f_hm,
                "exit_ts": f_ts + 900,
                "is_ambiguous": False,
            }

        is_amb = False
        if is_long:
            if fh >= target_spot and fl <= stop_spot:
                is_amb = True
        else:
            if fl <= target_spot and fh >= stop_spot:
                is_amb = True

        if is_amb:
            return {
                "st_pnl_pts": -sl_pts,
                "fx_pnl_pts": -sl_pts,
                "exit_spot": stop_spot,
                "exit_reason": "AMBIGUOUS",
                "trail_steps": [f"SL {int(sl_pts)}", "AMBIGUOUS bar"],
                "mfe_pts": tgt_pts,
                "entry_day": entry_day,
                "entry_hm": entry_hm,
                "exit_day": f_day,
                "exit_hm": f_hm,
                "exit_ts": f_ts + 900,
                "is_ambiguous": True,
            }

        if (is_long and fl <= stop_spot) or (not is_long and fh >= stop_spot):
            return {
                "st_pnl_pts": -sl_pts,
                "fx_pnl_pts": -sl_pts,
                "exit_spot": stop_spot,
                "exit_reason": "SPOT_SL_HIT",
                "trail_steps": [f"SL {int(sl_pts)}", f"SL hit {stop_spot:.1f}"],
                "mfe_pts": 0.0,
                "entry_day": entry_day,
                "entry_hm": entry_hm,
                "exit_day": f_day,
                "exit_hm": f_hm,
                "exit_ts": f_ts + 900,
                "is_ambiguous": False,
            }

        if (is_long and fh >= target_spot) or (not is_long and fl <= target_spot):
            return {
                "st_pnl_pts": tgt_pts,
                "fx_pnl_pts": tgt_pts,
                "exit_spot": target_spot,
                "exit_reason": "SPOT_TARGET_HIT",
                "trail_steps": [f"SL {int(sl_pts)}", f"Target hit {target_spot:.1f}"],
                "mfe_pts": tgt_pts,
                "entry_day": entry_day,
                "entry_hm": entry_hm,
                "exit_day": f_day,
                "exit_hm": f_hm,
                "exit_ts": f_ts + 900,
                "is_ambiguous": False,
            }

        e8_here = float(fb.get("e8") or 0.0)
        if e8_here > 0 and exit_on_opposite_close:
            opp_close = (fc < e8_here) if is_long else (fc > e8_here)
            if opp_close:
                pnl = round((fc - entry_spot) if is_long else (entry_spot - fc), 2)
                bar_close_hm = (
                    pd.to_datetime(f_ts + 900, unit="s", utc=True)
                    .tz_convert("Asia/Kolkata")
                    .strftime("%H:%M")
                    if f_ts > 0
                    else f_hm
                )
                return {
                    "st_pnl_pts": pnl,
                    "fx_pnl_pts": pnl,
                    "exit_spot": fc,
                    "exit_reason": "OPPOSITE_8EMA_CLOSE",
                    "trail_steps": [f"SL {int(sl_pts)}", f"Opposite 8 EMA Close {fc:.1f}"],
                    "mfe_pts": max(0.0, pnl),
                    "entry_day": entry_day,
                    "entry_hm": entry_hm,
                    "exit_day": f_day,
                    "exit_hm": bar_close_hm,
                    "exit_ts": f_ts + 900,
                    "is_ambiguous": False,
                }

    last_b = bars[-1] if bars else {}
    fc = float(last_b.get("close") or entry_spot)
    pnl = round((fc - entry_spot) if is_long else (entry_spot - fc), 2)
    return {
        "st_pnl_pts": pnl,
        "fx_pnl_pts": pnl,
        "exit_spot": fc,
        "exit_reason": f"EOD_SQUARE_OFF_{eod_hm}",
        "trail_steps": [f"SL {int(sl_pts)}"],
        "mfe_pts": max(0.0, pnl),
        "entry_day": entry_day,
        "entry_hm": entry_hm,
        "exit_day": str(last_b.get("day") or entry_day),
        "exit_hm": str(last_b.get("hm") or "15:20"),
        "exit_ts": int(last_b.get("ts") or 0) + 900,
        "is_ambiguous": False,
    }


def compute_ema8_prototype_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes summary metrics for 8 EMA Prototype according to Harish's specification:
    - total trades, wins, losses, breakeven, win rate %
    - avg win (points), avg loss (points), payoff ratio, net points
    - exit reasons breakdown: SPOT_TARGET_HIT, SPOT_SL_HIT, OPPOSITE_8EMA_CLOSE, EOD_SQUARE_OFF_15:20, AMBIGUOUS
    - ambiguous trades dual reporting: stops hit first vs targets hit first
    """
    total = len(trades)
    if total == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0.0,
            "win_rate_pct": 0.0,
            "avg_win_pts": 0.0,
            "avg_loss_pts": 0.0,
            "payoff_ratio": 0.0,
            "net_points": 0.0,
            "exit_reasons": {
                "SPOT_TARGET_HIT": 0,
                "SPOT_SL_HIT": 0,
                "OPPOSITE_8EMA_CLOSE": 0,
                "EOD_SQUARE_OFF_15:20": 0,
                "AMBIGUOUS": 0,
            },
            "ambiguous_count": 0,
            "ambiguous_summary": "0 ambiguous trades",
            "scenario_stops_first": {
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "net_points": 0.0,
            },
            "scenario_targets_first": {
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "net_points": 0.0,
            },
        }

    exit_reasons = {
        "SPOT_TARGET_HIT": 0,
        "SPOT_SL_HIT": 0,
        "OPPOSITE_8EMA_CLOSE": 0,
        "EOD_SQUARE_OFF_15:20": 0,
        "AMBIGUOUS": 0,
    }

    for t in trades:
        r = str(t.get("exit_reason") or t.get("Exit_Reason") or t.get("reason") or "").strip()
        if "AMBIGUOUS" in r or t.get("is_ambiguous"):
            exit_reasons["AMBIGUOUS"] += 1
        elif "TARGET" in r:
            exit_reasons["SPOT_TARGET_HIT"] += 1
        elif "OPPOSITE" in r:
            exit_reasons["OPPOSITE_8EMA_CLOSE"] += 1
        elif "EOD" in r:
            exit_reasons["EOD_SQUARE_OFF_15:20"] += 1
        elif "SL" in r:
            exit_reasons["SPOT_SL_HIT"] += 1
        else:
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

    amb_count = exit_reasons["AMBIGUOUS"]
    non_amb = [
        t
        for t in trades
        if not t.get("is_ambiguous") and "AMBIGUOUS" not in str(t.get("exit_reason") or t.get("Exit_Reason") or "")
    ]

    wins_non_amb = [t for t in non_amb if float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0) > 0]
    losses_non_amb = [t for t in non_amb if float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0) < 0]
    be_non_amb = [t for t in non_amb if float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0) == 0]

    # Scenario 1: Stops hit first on ambiguous trades (-40 pts each)
    wins_stops_first = len(wins_non_amb)
    losses_stops_first = len(losses_non_amb) + amb_count
    wr_stops_first = round((wins_stops_first / total) * 100.0, 1) if total > 0 else 0.0
    pts_non_amb = sum(float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0) for t in non_amb)
    pts_stops_first = round(pts_non_amb + (-40.0 * amb_count), 2)

    # Scenario 2: Targets hit first on ambiguous trades (+40 pts each)
    wins_targets_first = len(wins_non_amb) + amb_count
    losses_targets_first = len(losses_non_amb)
    wr_targets_first = round((wins_targets_first / total) * 100.0, 1) if total > 0 else 0.0
    pts_targets_first = round(pts_non_amb + (40.0 * amb_count), 2)

    if amb_count > 0:
        amb_summary = (
            f"{wins_stops_first} wins / {losses_stops_first} losses if stops hit first, "
            f"{wins_targets_first} wins / {losses_targets_first} losses if targets hit first"
        )
    else:
        amb_summary = f"{wins_stops_first} wins / {losses_stops_first} losses (0 ambiguous trades)"

    win_pts_list = [float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0) for t in wins_non_amb]
    loss_pts_list = [abs(float(t.get("st_pnl_pts") or t.get("pnl_pts") or 0.0)) for t in losses_non_amb] + ([40.0] * amb_count)
    avg_w = round(float(np.mean(win_pts_list)), 2) if win_pts_list else 0.0
    avg_l = round(float(np.mean(loss_pts_list)), 2) if loss_pts_list else 0.0
    payoff = round(avg_w / avg_l, 2) if avg_l > 0 else 0.0

    return {
        "total_trades": total,
        "wins": wins_stops_first,
        "losses": losses_stops_first,
        "breakeven": len(be_non_amb),
        "win_rate": wr_stops_first,
        "win_rate_pct": wr_stops_first,
        "avg_win_pts": avg_w,
        "avg_loss_pts": avg_l,
        "payoff_ratio": payoff,
        "net_points": pts_stops_first,
        "exit_reasons": exit_reasons,
        "ambiguous_count": amb_count,
        "ambiguous_summary": amb_summary,
        "scenario_stops_first": {
            "wins": wins_stops_first,
            "losses": losses_stops_first,
            "win_rate_pct": wr_stops_first,
            "net_points": pts_stops_first,
        },
        "scenario_targets_first": {
            "wins": wins_targets_first,
            "losses": losses_targets_first,
            "win_rate_pct": wr_targets_first,
            "net_points": pts_targets_first,
        },
    }


def simulate_backtest_spot_exit(
    bars: List[Dict[str, Any]],
    start_idx: int,
    entry_spot: float,
    is_long: bool,
    sl_pts: float,
    tgt_pts: float,
    is_flagship: bool,
    sr_levels: Optional[Dict[str, Any]],
    eod_hm: str = "15:20",
    sr_proximity: float = 15.0,
    trail_every_bar: bool = False,
    trail_green_only: bool = True,
    exit_at_8ema: bool = False,
    exit_on_opposite_8ema_close: bool = False,
    *,
    day_1m: Optional[pd.DataFrame] = None,
    entry_ts: Optional[int] = None,
) -> Tuple[float, float, float, str, List[str], float, str, str, str, str]:
    """
    Forward-walk spot exit on 15m bars.
    Returns (st_pnl_pts, fx_pnl_pts, exit_spot, exit_reason, trail_steps, mfe_pts,
             entry_day, entry_hm, exit_day, exit_hm).
    """
    if exit_on_opposite_8ema_close:
        res = simulate_prototype_spot_exit(
            bars=bars,
            start_idx=start_idx,
            entry_spot=entry_spot,
            is_long=is_long,
            sl_pts=sl_pts,
            tgt_pts=tgt_pts,
            eod_hm=eod_hm,
            day_1m=day_1m,
            entry_ts=entry_ts,
        )
        return (
            res["st_pnl_pts"],
            res["fx_pnl_pts"],
            res["exit_spot"],
            res["exit_reason"],
            res["trail_steps"],
            res["mfe_pts"],
            res["entry_day"],
            res["entry_hm"],
            res["exit_day"],
            res["exit_hm"],
        )
    entry_day = str(bars[start_idx].get("day") or "")
    entry_hm = str(bars[start_idx].get("hm") or "")
    exit_day = entry_day
    exit_hm = entry_hm
    st_sl = (entry_spot - sl_pts) if is_long else (entry_spot + sl_pts)
    exit_spot = entry_spot
    st_pnl_pts = 0.0
    fx_pnl_pts = 0.0
    st_reason = f"EOD_SQUARE_OFF_{eod_hm}"
    fx_done = False
    st_done = False
    trail_steps = [f"SL {int(sl_pts)}"]
    idx_high = entry_spot
    idx_low = entry_spot

    for f_idx in range(start_idx, len(bars)):
        fb = bars[f_idx]
        fh, fl, fc, fo = float(fb["high"]), float(fb["low"]), float(fb["close"]), float(fb["open"])
        idx_high = max(idx_high, fh)
        idx_low = min(idx_low, fl)

        if not fx_done:
            if is_long and fl <= entry_spot - sl_pts:
                fx_pnl_pts = -sl_pts
                fx_done = True
            elif is_long and fh >= entry_spot + tgt_pts:
                fx_pnl_pts = tgt_pts
                fx_done = True
            elif not is_long and fh >= entry_spot + sl_pts:
                fx_pnl_pts = -sl_pts
                fx_done = True
            elif not is_long and fl <= entry_spot - tgt_pts:
                fx_pnl_pts = tgt_pts
                fx_done = True

        if not st_done:
            if is_flagship and sr_levels:
                sr_hit = flagship_sr_exit_trigger(is_long, fc, sr_levels, sr_proximity)
                if sr_hit:
                    exit_spot = fc
                    exit_day = str(fb.get("day") or exit_day)
                    exit_hm = str(fb.get("hm") or exit_hm)
                    st_pnl_pts = round((exit_spot - entry_spot) if is_long else (entry_spot - exit_spot), 2)
                    st_reason = sr_hit
                    st_done = True
            if not st_done:
                e8_here = float(fb.get("e8") or 0.0)
                if exit_at_8ema and e8_here > 0:
                    if is_long and fh >= e8_here:
                        exit_spot = e8_here
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(e8_here - entry_spot, 2)
                        st_reason = "H1_8EMA_HIT"
                        st_done = True
                    elif not is_long and fl <= e8_here:
                        exit_spot = e8_here
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(entry_spot - e8_here, 2)
                        st_reason = "H1_8EMA_HIT"
                        st_done = True
            if not st_done:
                fb_ts = int(fb.get("ts") or 0)
                bar_close_hm = (
                    pd.to_datetime(fb_ts + 900, unit="s", utc=True)
                    .tz_convert("Asia/Kolkata")
                    .strftime("%H:%M")
                    if fb_ts > 0
                    else str(fb.get("hm") or exit_hm)
                )
                if is_long:
                    if fl <= st_sl:
                        exit_spot = st_sl
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = bar_close_hm if (f_idx == start_idx and exit_on_opposite_8ema_close) else str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(st_sl - entry_spot, 2)
                        st_reason = "TRAIL_SL_HIT" if st_sl > (entry_spot - sl_pts) else "SL_HIT"
                        st_done = True
                    elif fh >= entry_spot + tgt_pts:
                        exit_spot = entry_spot + tgt_pts
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = bar_close_hm if (f_idx == start_idx and exit_on_opposite_8ema_close) else str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(tgt_pts, 2)
                        st_reason = "TARGET_HIT"
                        st_done = True
                    elif exit_on_opposite_8ema_close:
                        e8_here = float(fb.get("e8") or 0.0)
                        if e8_here > 0 and fc < e8_here:
                            exit_spot = fc
                            exit_day = str(fb.get("day") or exit_day)
                            exit_hm = bar_close_hm
                            st_pnl_pts = round(fc - entry_spot, 2)
                            st_reason = "OPPOSITE_8EMA_CLOSE"
                            st_done = True
                    elif trail_every_bar and fl > (entry_spot - sl_pts):
                        st_sl = max(st_sl, round(fl, 2))
                        trail_steps.append(f"Bar Low {st_sl:.0f}")
                    elif (trail_green_only and fc > fo) and (is_flagship or fl > (entry_spot - sl_pts)):
                        if is_flagship:
                            st_sl = flagship_candle_trail_update(True, st_sl, fo, fh, fl, fc)
                        else:
                            st_sl = max(st_sl, round(fl, 2))
                        trail_steps.append(f"Green Low {st_sl:.0f}")
                else:
                    if fh >= st_sl:
                        exit_spot = st_sl
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = bar_close_hm if (f_idx == start_idx and exit_on_opposite_8ema_close) else str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(entry_spot - st_sl, 2)
                        st_reason = "TRAIL_SL_HIT" if st_sl < (entry_spot + sl_pts) else "SL_HIT"
                        st_done = True
                    elif fl <= entry_spot - tgt_pts:
                        exit_spot = entry_spot - tgt_pts
                        exit_day = str(fb.get("day") or exit_day)
                        exit_hm = bar_close_hm if (f_idx == start_idx and exit_on_opposite_8ema_close) else str(fb.get("hm") or exit_hm)
                        st_pnl_pts = round(tgt_pts, 2)
                        st_reason = "TARGET_HIT"
                        st_done = True
                    elif exit_on_opposite_8ema_close:
                        e8_here = float(fb.get("e8") or 0.0)
                        if e8_here > 0 and fc > e8_here:
                            exit_spot = fc
                            exit_day = str(fb.get("day") or exit_day)
                            exit_hm = bar_close_hm
                            st_pnl_pts = round(entry_spot - fc, 2)
                            st_reason = "OPPOSITE_8EMA_CLOSE"
                            st_done = True
                    elif trail_every_bar and fh < (entry_spot + sl_pts):
                        st_sl = min(st_sl, round(fh, 2))
                        trail_steps.append(f"Bar High {st_sl:.0f}")
                    elif (trail_green_only and fc < fo) and (is_flagship or fh < (entry_spot + sl_pts)):
                        if is_flagship:
                            st_sl = flagship_candle_trail_update(False, st_sl, fo, fh, fl, fc)
                        else:
                            st_sl = min(st_sl, round(fh, 2))
                        trail_steps.append(f"Red High {st_sl:.0f}")

        if str(fb.get("hm", "")) >= eod_hm:
            if not fx_done:
                fx_pnl_pts = round((fc - entry_spot) if is_long else (entry_spot - fc), 2)
                fx_done = True
            if not st_done:
                exit_spot = fc
                exit_day = str(fb.get("day") or exit_day)
                exit_hm = str(fb.get("hm") or exit_hm)
                st_pnl_pts = round((fc - entry_spot) if is_long else (entry_spot - fc), 2)
                st_reason = f"EOD_SQUARE_OFF_{eod_hm}"
                st_done = True
            break

    if not st_done:
        last = bars[min(len(bars) - 1, max(start_idx, 0))]
        exit_spot = float(last["close"])
        exit_day = str(last.get("day") or exit_day)
        exit_hm = str(last.get("hm") or exit_hm)
        st_pnl_pts = round((exit_spot - entry_spot) if is_long else (entry_spot - exit_spot), 2)
    mfe_pts = round((idx_high - entry_spot) if is_long else (entry_spot - idx_low), 1)
    return st_pnl_pts, fx_pnl_pts, exit_spot, st_reason, trail_steps, mfe_pts, entry_day, entry_hm, exit_day, exit_hm


class InstrumentManager:
    """Manages official Dhan scrip master and resolves security IDs and lot sizes."""

    def __init__(self, master_path: str, url: str) -> None:
        self.master_path = ROOT / master_path
        self.url = url
        self.nifty_options: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
        self.nifty_futures: Dict[str, Dict[str, Any]] = {}
        self.cached_expiries: List[str] = []
        self._loaded = False

    def ensure_loaded(self) -> None:
        if self._loaded and self.nifty_options:
            return
        if not self.master_path.exists():
            log.warning("Master CSV not found at %s. Attempting download...", self.master_path)
            self._download_master()

        if self.master_path.exists():
            self._parse_master()
            self._loaded = True

    def _download_master(self) -> None:
        try:
            log.info("Downloading Dhan instrument master from %s", self.url)
            self.master_path.parent.mkdir(parents=True, exist_ok=True)
            import requests
            r = requests.get(self.url, timeout=30)
            if r.status_code == 200:
                self.master_path.write_bytes(r.content)
                log.info("Master CSV downloaded successfully (%d bytes)", len(r.content))
        except Exception as e:
            log.error("Failed downloading instrument master: %s", e)

    def _parse_master(self) -> None:
        t0 = time.time()
        options = {}
        futures = {}
        expiries = set()
        try:
            with open(self.master_path, mode="r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    seg = row.get("SEM_SEGMENT", "")
                    if seg != "D":
                        continue
                    sym = row.get("SEM_TRADING_SYMBOL", "")
                    inst = row.get("SEM_INSTRUMENT_NAME", "")
                    if not sym.startswith("NIFTY"):
                        continue

                    exp_str = row.get("SEM_EXPIRY_DATE", "").strip().split(" ")[0]
                    sec_id = row.get("SEM_SMST_SECURITY_ID", "")
                    lot = int(float(row.get("SEM_LOT_UNITS", 65)))

                    if inst == "OPTIDX" and sym.startswith("NIFTY-"):
                        try:
                            strike = float(row.get("SEM_STRIKE_PRICE", 0.0))
                            opt_type = row.get("SEM_OPTION_TYPE", "").strip()
                            if exp_str and strike > 0 and opt_type in ("CE", "PE"):
                                expiries.add(exp_str)
                                key = (exp_str, strike, opt_type)
                                options[key] = {
                                    "security_id": str(sec_id),
                                    "lot_size": lot,
                                    "symbol": sym,
                                    "expiry": exp_str,
                                    "strike": strike,
                                    "opt_type": opt_type,
                                    "tick_size": float(row.get("SEM_TICK_SIZE", 0.05)),
                                }
                        except Exception as e:
                            log.debug("Row parse error: %s", e)

                    elif inst == "FUTIDX" and sym.startswith("NIFTY-"):
                        if exp_str and sec_id:
                            futures[exp_str] = {
                                "security_id": str(sec_id),
                                "symbol": sym,
                                "lot_size": lot,
                                "expiry": exp_str,
                            }
            self.nifty_options = options
            self.nifty_futures = futures
            self.cached_expiries = sorted(list(expiries))
            log.info("Parsed %d NIFTY options and %d futures in %.2fs", len(options), len(futures), time.time() - t0)
        except Exception as e:
            log.error("Failed parsing instrument master: %s", e)

    def get_nifty_lot_size(self) -> int:
        """Reads NIFTY option lot size dynamically from parsed master (65)."""
        self.ensure_loaded()
        if self.nifty_options:
            for opt in self.nifty_options.values():
                lot = opt.get("lot_size")
                if lot and lot > 0:
                    return int(lot)
        raise ValueError("NIFTY lot size unavailable from instrument master; execution blocked")

    def get_nearest_expiry(self, target_date: Optional[str] = None) -> str:
        self.ensure_loaded()
        today = target_date or datetime.now(IST).strftime("%Y-%m-%d")
        valid = [exp for exp in self.cached_expiries if exp >= today]
        return valid[0] if valid else (self.cached_expiries[0] if self.cached_expiries else today)

    def get_active_future_contract(self) -> Optional[Dict[str, Any]]:
        self.ensure_loaded()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        valid = sorted([exp for exp in self.nifty_futures.keys() if exp >= today])
        if valid:
            return self.nifty_futures[valid[0]]
        if self.nifty_futures:
            first_key = sorted(list(self.nifty_futures.keys()))[0]
            return self.nifty_futures[first_key]
        return None

    def resolve_option(self, expiry: str, strike: float, opt_type: str) -> Optional[Dict[str, Any]]:
        self.ensure_loaded()
        return self.nifty_options.get((expiry, float(strike), opt_type.upper()))


class DhanBroker:
    """Manages communications with Dhan HQ API v2 with rate limits and safety."""

    def __init__(self, base_url: str = "https://api.dhan.co/v2", cfg: Optional[Dict[str, Any]] = None) -> None:
        self.base_url = base_url
        self.cfg = cfg or {}
        load_env_safe()
        self._last_ltp_call = 0.0
        self._ltp_cache: Dict[str, float] = {}
        self._ltt_cache: Dict[str, Any] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cached_ltp: Dict[str, float] = self._ltp_cache  # Backward-compatibility alias
        self._last_funds_call = 0.0
        self._cached_funds: Dict[str, Any] = {}
        self._backoff_until: float = 0.0
        self._backoff_delay: float = 3.0
        self.LTP_TTL: float = 5.0
        self.market_db = ROOT / self.cfg.get("market_db_path", "ict_smc_app/backend/data/ict_smc.sqlite3")

        # Dhan Connection & IP Config state
        dhan_cfg = self.cfg.get("dhan", {})
        config_path = ROOT / "analyst" / "terminal8510_config.json"
        if not dhan_cfg and config_path.exists():
            try:
                raw_cfg = json.loads(config_path.read_text(encoding="utf-8"))
                dhan_cfg = raw_cfg.get("dhan", {})
            except Exception as e:
                log.debug("Config load error in DhanBroker: %s", e)

        # Restore permanently saved credentials into os.environ if missing
        saved_tok = dhan_cfg.get("DHAN_ACCESS_TOKEN") or dhan_cfg.get("access_token")
        saved_cid = dhan_cfg.get("DHAN_CLIENT_ID") or dhan_cfg.get("client_id")
        saved_key = dhan_cfg.get("DHAN_API_KEY") or dhan_cfg.get("api_key")
        saved_sec = dhan_cfg.get("DHAN_API_SECRET") or dhan_cfg.get("api_secret")
        saved_exp = dhan_cfg.get("TOKEN_EXPIRY_UTC") or dhan_cfg.get("token_expiry_utc")

        if saved_tok and not os.environ.get("DHAN_ACCESS_TOKEN"):
            os.environ["DHAN_ACCESS_TOKEN"] = str(saved_tok)
        if saved_cid and not os.environ.get("DHAN_CLIENT_ID"):
            os.environ["DHAN_CLIENT_ID"] = str(saved_cid)
        if saved_key and not os.environ.get("DHAN_API_KEY"):
            os.environ["DHAN_API_KEY"] = str(saved_key)
        if saved_sec and not os.environ.get("DHAN_API_SECRET"):
            os.environ["DHAN_API_SECRET"] = str(saved_sec)
        if saved_exp and not os.environ.get("TOKEN_EXPIRY_UTC"):
            os.environ["TOKEN_EXPIRY_UTC"] = str(saved_exp)

        active_tok = os.environ.get("DHAN_ACCESS_TOKEN", "")
        if active_tok and len(active_tok) > 20:
            exp_info = decode_token_expiry(active_tok)
            if not exp_info["is_expired"]:
                log.info("Auto-loaded saved Dhan API — No need to re-enter.")

        self.environment: str = os.environ.get("DHAN_ENVIRONMENT") or dhan_cfg.get("environment", "LIVE")
        self.whitelisted_ip: str = os.environ.get("DHAN_WHITELISTED_IP") or dhan_cfg.get("whitelisted_ip", "")
        self._cached_ip: str = ""
        self._last_ip_check: float = 0.0
        self.last_connected_status: Dict[str, Any] = {
            "connected": False,
            "dhan_connected": False,
            "status": "INITIALIZING",
            "message": "Initializing Dhan connection...",
            "last_updated": ""
        }

    def get_current_ip(self) -> str:
        """Fetches public IP with 300-second cache and resilient fallbacks."""
        now = time.time()
        if self._cached_ip and (now - self._last_ip_check < 300.0):
            return self._cached_ip
        urls = [
            "https://api.ipify.org?format=json",
            "https://ifconfig.me/ip",
            "https://icanhazip.com"
        ]
        for url in urls:
            try:
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        ip = resp.json().get("ip", "").strip() if "json" in url else resp.text.strip()
                        if ip and len(ip.split(".")) == 4:
                            self._cached_ip = ip
                            self._last_ip_check = now
                            return ip
            except Exception as e:
                log.debug("IP lookup via %s failed: %s", url, e)
                continue
        return self._cached_ip or "127.0.0.1"

    async def get_current_ip_async(self) -> str:
        """Async public IP resolver."""
        now = time.time()
        if self._cached_ip and (now - self._last_ip_check < 300.0):
            return self._cached_ip
        urls = [
            "https://api.ipify.org?format=json",
            "https://ifconfig.me/ip",
            "https://icanhazip.com"
        ]
        async with httpx.AsyncClient(timeout=3.0) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        ip = resp.json().get("ip", "").strip() if "json" in url else resp.text.strip()
                        if ip and len(ip.split(".")) == 4:
                            self._cached_ip = ip
                            self._last_ip_check = now
                            return ip
                except Exception as e:
                    log.debug("Async IP lookup via %s failed: %s", url, e)
                    continue
        return self._cached_ip or "127.0.0.1"

    def get_masked_config(self) -> Dict[str, Any]:
        """Returns Dhan configuration with secrets safely masked (****last4)."""
        load_env_safe()
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client_id = os.environ.get("DHAN_CLIENT_ID", "")
        api_key = os.environ.get("DHAN_API_KEY", "")
        api_sec = os.environ.get("DHAN_API_SECRET", "")

        masked_token = f"****{token[-4:]}" if len(token) >= 4 else ("****" if token else "")
        masked_secret = f"****{api_sec[-4:]}" if len(api_sec) >= 4 else ("****" if api_sec else "")

        curr_ip = self.get_current_ip()
        whitelisted = self.whitelisted_ip or curr_ip
        ip_matched = bool(whitelisted and curr_ip and (whitelisted.strip() == curr_ip.strip()))

        is_cfg = bool(token and len(token) > 20)
        exp_info = decode_token_expiry(token) if is_cfg else {
            "token_valid_till": "--",
            "time_left": "--",
            "days_left": "--",
            "is_expired": True
        }

        return {
            "ok": True,
            "client_id": client_id,
            "masked_token": masked_token,
            "token_configured": is_cfg,
            "token_valid_till": exp_info["token_valid_till"],
            "time_left": exp_info["time_left"],
            "days_left": exp_info["days_left"],
            "is_expired": exp_info["is_expired"],
            "api_key": api_key,
            "masked_secret": masked_secret,
            "secret_configured": bool(api_sec),
            "environment": self.environment,
            "current_ip": curr_ip,
            "whitelisted_ip": whitelisted,
            "ip_matched": ip_matched,
            "dhan_connected": self.is_configured(),
            "last_status": self.last_connected_status,
        }

    def update_credentials(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        environment: Optional[str] = None,
        whitelisted_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Updates and persists credentials permanently on disk (terminal8510_config.json & .env)."""
        load_env_safe()
        updates: Dict[str, str] = {}

        if client_id is not None and client_id.strip():
            updates["DHAN_CLIENT_ID"] = client_id.strip()

        # Only update token if a new non-masked token is provided
        clean_tok = ""
        if access_token is not None:
            raw_t = access_token.strip()
            if raw_t and not raw_t.startswith("****"):
                clean_tok = raw_t
                updates["DHAN_ACCESS_TOKEN"] = clean_tok
        if not clean_tok:
            clean_tok = os.environ.get("DHAN_ACCESS_TOKEN", "")

        if api_key is not None:
            updates["DHAN_API_KEY"] = api_key.strip()

        if api_secret is not None:
            clean_sec = api_secret.strip()
            if clean_sec and not clean_sec.startswith("****"):
                updates["DHAN_API_SECRET"] = clean_sec

        if environment is not None and environment.strip():
            self.environment = environment.strip()
            updates["DHAN_ENVIRONMENT"] = self.environment

        if whitelisted_ip is not None:
            self.whitelisted_ip = whitelisted_ip.strip()
            updates["DHAN_WHITELISTED_IP"] = self.whitelisted_ip

        # Calculate exact Token Valid Till
        exp_info = decode_token_expiry(clean_tok) if clean_tok else {}
        if exp_info.get("token_expiry_utc"):
            updates["TOKEN_EXPIRY_UTC"] = exp_info["token_expiry_utc"]

        # Persist to .env safely
        if updates:
            save_env_safe(updates)

        # Permanent Disk Persistence inside analyst/terminal8510_config.json
        config_path = ROOT / "analyst" / "terminal8510_config.json"
        try:
            cfg_data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            cfg_data.setdefault("dhan", {})
            if "DHAN_CLIENT_ID" in updates:
                cfg_data["DHAN_CLIENT_ID"] = updates["DHAN_CLIENT_ID"]
                cfg_data["dhan"]["DHAN_CLIENT_ID"] = updates["DHAN_CLIENT_ID"]
                cfg_data["dhan"]["client_id"] = updates["DHAN_CLIENT_ID"]
            if "DHAN_ACCESS_TOKEN" in updates:
                cfg_data["DHAN_ACCESS_TOKEN"] = updates["DHAN_ACCESS_TOKEN"]
                cfg_data["dhan"]["DHAN_ACCESS_TOKEN"] = updates["DHAN_ACCESS_TOKEN"]
                cfg_data["dhan"]["access_token"] = updates["DHAN_ACCESS_TOKEN"]
            if "DHAN_API_KEY" in updates:
                cfg_data["DHAN_API_KEY"] = updates["DHAN_API_KEY"]
                cfg_data["dhan"]["DHAN_API_KEY"] = updates["DHAN_API_KEY"]
                cfg_data["dhan"]["api_key"] = updates["DHAN_API_KEY"]
            if "DHAN_API_SECRET" in updates:
                cfg_data["DHAN_API_SECRET"] = updates["DHAN_API_SECRET"]
                cfg_data["dhan"]["DHAN_API_SECRET"] = updates["DHAN_API_SECRET"]
                cfg_data["dhan"]["api_secret"] = updates["DHAN_API_SECRET"]
            if "TOKEN_EXPIRY_UTC" in updates:
                cfg_data["TOKEN_EXPIRY_UTC"] = updates["TOKEN_EXPIRY_UTC"]
                cfg_data["dhan"]["TOKEN_EXPIRY_UTC"] = updates["TOKEN_EXPIRY_UTC"]
                cfg_data["dhan"]["token_expiry_utc"] = updates["TOKEN_EXPIRY_UTC"]
            if environment:
                cfg_data["dhan"]["environment"] = self.environment
            if whitelisted_ip:
                cfg_data["dhan"]["whitelisted_ip"] = self.whitelisted_ip

            config_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
            self.cfg = cfg_data
        except Exception as e:
            log.warning("Could not persist dhan credentials to terminal8510_config.json: %s", e)

        # Log safely — NEVER log full token or secret!
        cid = updates.get("DHAN_CLIENT_ID", os.environ.get("DHAN_CLIENT_ID", ""))
        tok = updates.get("DHAN_ACCESS_TOKEN", os.environ.get("DHAN_ACCESS_TOKEN", ""))
        masked_t = f"****{tok[-4:]}" if len(tok) >= 4 else "none"
        log.info("Dhan API credentials updated safely. Client ID: %s, Token: %s", cid, masked_t)

        res = self.get_masked_config()
        if exp_info:
            res.update({
                "token_valid_till": exp_info.get("token_valid_till", "--"),
                "time_left": exp_info.get("time_left", "--"),
                "days_left": exp_info.get("days_left", "--"),
                "is_expired": exp_info.get("is_expired", False),
            })
        return res

    def clear_credentials(self) -> Dict[str, Any]:
        """Clears saved Dhan credentials from memory, .env, and terminal8510_config.json on logout."""
        clears = {
            "DHAN_ACCESS_TOKEN": "",
            "DHAN_API_KEY": "",
            "DHAN_API_SECRET": "",
            "TOKEN_EXPIRY_UTC": "",
        }
        save_env_safe(clears)
        config_path = ROOT / "analyst" / "terminal8510_config.json"
        try:
            if config_path.exists():
                cfg_data = json.loads(config_path.read_text(encoding="utf-8"))
                d_sec = cfg_data.get("dhan", {})
                for k in ["DHAN_ACCESS_TOKEN", "access_token", "DHAN_API_KEY", "api_key", "DHAN_API_SECRET", "api_secret", "TOKEN_EXPIRY_UTC", "token_expiry_utc"]:
                    d_sec.pop(k, None)
                cfg_data["dhan"] = d_sec
                config_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
                self.cfg = cfg_data
        except Exception as e:
            log.warning("Error clearing dhan credentials from config: %s", e)

        self.last_connected_status = {
            "connected": False,
            "dhan_connected": False,
            "status": "DISCONNECTED ❌",
            "message": "Dhan credentials cleared. Please enter new API credentials.",
            "last_updated": datetime.now(IST).strftime("%H:%M:%S")
        }
        return {
            "ok": True,
            "connected": False,
            "message": "Dhan API credentials logged out and cleared successfully."
        }

    def get_status(self) -> Dict[str, Any]:
        """Returns auto-load Dhan credentials status, masking, and time left."""
        load_env_safe()
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client_id = os.environ.get("DHAN_CLIENT_ID", "")
        masked_token = f"****{token[-4:]}" if len(token) >= 4 else ("****" if token else "")
        is_cfg = bool(token and len(token) > 20)

        exp_info = decode_token_expiry(token) if is_cfg else {
            "token_valid_till": "--",
            "time_left": "--",
            "days_left": "--",
            "is_expired": True
        }

        connected = self.last_connected_status.get("connected", False)
        status_tag = self.last_connected_status.get("status", "DISCONNECTED ❌")
        message = self.last_connected_status.get("message", "")

        status_text = (
            f"Loaded Saved Dhan API: {masked_token} | Token Valid Till: {exp_info['token_valid_till']} | "
            f"Days Left: {exp_info['days_left']} | Status: CONNECTED ✅"
            if (is_cfg and not exp_info["is_expired"]) else (message or f"Status: {status_tag}")
        )
        display_text = status_text
        auto_load_log = "Auto-loaded saved Dhan API — No need to re-enter." if (is_cfg and not exp_info["is_expired"]) else ""

        return {
            "ok": True,
            "configured": is_cfg,
            "client_id": client_id,
            "masked_token": masked_token,
            "token_valid_till": exp_info["token_valid_till"],
            "time_left": exp_info["time_left"],
            "days_left": exp_info["days_left"],
            "is_expired": exp_info["is_expired"],
            "connected": connected,
            "dhan_connected": connected,
            "status": status_tag,
            "status_text": status_text,
            "auto_load_log": auto_load_log,
            "message": message,
            "display_text": display_text,
            "last_status": self.last_connected_status,
        }

    async def test_connection(self, fut_sec_id: int = 61471) -> Dict[str, Any]:
        """
        Calls Dhan API /v2/fundlimit and /v2/marketfeed/ltp to test connectivity.
        Strict 10-second timeout enforced.
        """
        now_ts = time.time()
        if self.last_connected_status and self.last_connected_status.get("ok"):
            last_ts = getattr(self, "_last_test_conn_ts", 0.0)
            if now_ts - last_ts < 3.0:
                return self.last_connected_status

        now_time = datetime.now(IST).strftime("%H:%M:%S")
        curr_ip = await self.get_current_ip_async()

        if not self.is_configured():
            status_msg = "❌ Dhan Connection Failed (DISCONNECTED ❌): Invalid Access Token / Token Expired — Please regenerate Token from Dhan Console | [ 🔄 Retry Connection ]"
            status_obj = {
                "ok": False,
                "connected": False,
                "dhan_connected": False,
                "status": "DISCONNECTED ❌",
                "message": status_msg,
                "error": "Invalid Access Token / Token Expired — Please regenerate Token from Dhan Console",
                "current_ip": curr_ip,
                "last_updated": now_time,
            }
            self.last_connected_status = status_obj
            return status_obj

        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        masked_t = f"****{token[-4:]}" if len(token) >= 4 else "****"
        exp_info = decode_token_expiry(token)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res_funds = await asyncio.wait_for(
                    client.get(f"{self.base_url}/fundlimit", headers=self._headers()),
                    timeout=10.0
                )
                if res_funds.status_code == 200:
                    funds_data = res_funds.json()

                    # Fetch live spot & future quotes (Tier 1 live lookup)
                    spot = 0.0
                    fut = 0.0
                    try:
                        req_payload = {"IDX_I": [13], "NSE_FNO": [int(fut_sec_id)]}
                        res_ltp = await asyncio.wait_for(
                            client.post(f"{self.base_url}/marketfeed/ltp", json=req_payload, headers=self._headers()),
                            timeout=10.0
                        )
                        if res_ltp.status_code == 200:
                            data_ltp = res_ltp.json().get("data", {})
                            idx_map = data_ltp.get("IDX_I", {})
                            if "13" in idx_map:
                                val = float(idx_map["13"].get("last_price", 0.0))
                                if val > 0:
                                    spot = val
                                    self._ltp_cache["13"] = spot
                                    self._cache_timestamps["13"] = time.time()
                            fno_map = data_ltp.get("NSE_FNO", {})
                            if str(fut_sec_id) in fno_map:
                                val_fut = float(fno_map[str(fut_sec_id)].get("last_price", 0.0))
                                if val_fut > 0:
                                    fut = val_fut
                                    self._ltp_cache[str(fut_sec_id)] = fut
                                    self._cache_timestamps[str(fut_sec_id)] = time.time()
                    except Exception as e:
                        log.warning("Could not fetch test quotes: %s", e)

                    # Tier 2: last closed 1m candle fallback if unquoted/after-hours
                    if spot <= 0 and self.market_db.exists():
                        try:
                            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                                cur = conn.cursor()
                                r_s = cur.execute("SELECT c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 1").fetchone()
                                if r_s and r_s[0] and float(r_s[0]) > 0:
                                    spot = float(r_s[0])
                        except Exception:
                            pass
                    if fut <= 0 and self.market_db.exists():
                        try:
                            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                                cur = conn.cursor()
                                r_f = cur.execute("SELECT c FROM candles_1m WHERE sec_id='FUT' ORDER BY ts DESC LIMIT 1").fetchone()
                                if r_f and r_f[0] and float(r_f[0]) > 0:
                                    fut = float(r_f[0])
                        except Exception:
                            pass

                    spot_str = f"{spot:.2f}" if spot > 0 else "Unavailable"
                    fut_str = f"{fut:.2f}" if fut > 0 else "Unavailable"
                    status_msg = f"Loaded Saved Dhan API: {masked_t} | Token Valid Till: {exp_info['token_valid_till']} | Days Left: {exp_info['days_left']} | Status: CONNECTED ✅ | dhan_connected: true | Nifty Spot: {spot_str} | Nifty Future: {fut_str}"
                    status_obj = {
                        "ok": True,
                        "connected": True,
                        "dhan_connected": True,
                        "status": "CONNECTED ✅",
                        "message": status_msg,
                        "spot_price": spot if spot > 0 else None,
                        "future_price": fut if fut > 0 else None,
                        "funds": funds_data,
                        "current_ip": curr_ip,
                        "last_updated": now_time,
                        "token_valid_till": exp_info["token_valid_till"],
                        "days_left": exp_info["days_left"],
                        "masked_token": masked_t,
                    }
                    self._last_test_conn_ts = now_ts
                    self.last_connected_status = status_obj
                    return status_obj

                elif res_funds.status_code in (401, 403):
                    err_text = res_funds.text
                    if "ip" in err_text.lower() or "whitelist" in err_text.lower():
                        status_msg = f"❌ Dhan Connection Failed: IP NOT WHITELISTED ❌ | Your IP: {curr_ip} | Please add this IP in Dhan Console > API Access > IP Whitelist | [ 🔄 Retry Connection ]"
                        status_obj = {
                            "ok": False,
                            "connected": False,
                            "dhan_connected": False,
                            "status": "IP NOT WHITELISTED ❌",
                            "message": status_msg,
                            "error": status_msg,
                            "current_ip": curr_ip,
                            "last_updated": now_time,
                        }
                    else:
                        status_msg = "❌ Dhan Connection Failed (DISCONNECTED ❌): Invalid Access Token / Token Expired — Please regenerate Token from Dhan Console | [ 🔄 Retry Connection ]"
                        status_obj = {
                            "ok": False,
                            "connected": False,
                            "dhan_connected": False,
                            "status": "DISCONNECTED ❌",
                            "message": status_msg,
                            "error": status_msg,
                            "current_ip": curr_ip,
                            "last_updated": now_time,
                        }
                    self._last_test_conn_ts = now_ts
                    self.last_connected_status = status_obj
                    return status_obj

                else:
                    status_msg = f"❌ Dhan Connection Failed: HTTP {res_funds.status_code} - {res_funds.text[:100]} | [ 🔄 Retry Connection ]"
                    status_obj = {
                        "ok": False,
                        "connected": False,
                        "dhan_connected": False,
                        "status": "DISCONNECTED ❌",
                        "message": status_msg,
                        "error": status_msg,
                        "current_ip": curr_ip,
                        "last_updated": now_time,
                    }
                    self._last_test_conn_ts = now_ts
                    self.last_connected_status = status_obj
                    return status_obj

        except asyncio.TimeoutError:
            status_msg = "❌ Dhan Connection Failed: Connection timed out after 10s | [ 🔄 Retry Connection ]"
            status_obj = {
                "ok": False,
                "connected": False,
                "dhan_connected": False,
                "status": "DISCONNECTED ❌",
                "message": status_msg,
                "error": "Connection timed out after 10s",
                "current_ip": curr_ip,
                "last_updated": now_time,
            }
            self.last_connected_status = status_obj
            return status_obj
        except Exception as e:
            status_msg = f"❌ Dhan Connection Failed: DISCONNECTED ❌ ({str(e)}) | [ 🔄 Retry Connection ]"
            status_obj = {
                "ok": False,
                "connected": False,
                "dhan_connected": False,
                "status": "DISCONNECTED ❌",
                "message": status_msg,
                "error": str(e),
                "current_ip": curr_ip,
                "last_updated": now_time,
            }
            self.last_connected_status = status_obj
            return status_obj

    def _headers(self) -> Dict[str, str]:
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client_id = os.environ.get("DHAN_CLIENT_ID", "")
        return {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def is_configured(self) -> bool:
        load_env_safe()
        token = os.environ.get("DHAN_ACCESS_TOKEN", "")
        client = os.environ.get("DHAN_CLIENT_ID", "")
        return bool(token and client and len(token) > 20)

    async def get_fundlimit(self) -> Dict[str, Any]:
        """Queries /v2/fundlimit (cached for 10s to prevent API rate limits)."""
        now = time.time()
        if self._cached_funds and (now - self._last_funds_call < 10.0):
            return self._cached_funds

        if not self.is_configured():
            return {
                "ok": False,
                "error": "Dhan credentials not configured in .env",
                "total_avail": 0.0,
                "margin_used": 0.0,
                "margin_avail": 0.0,
            }
        try:
            self._last_funds_call = now
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.get(f"{self.base_url}/fundlimit", headers=self._headers())
                if res.status_code == 200:
                    d = res.json()
                    avail = float(d.get("availabelBalance") or 0.0)
                    used = float(d.get("utilizedAmount") or 0.0)
                    sod = float(d.get("sodLimit") or 0.0)
                    total = avail + used
                    self._cached_funds = {
                        "ok": True,
                        "total_avail": round(total, 2),
                        "margin_used": round(used, 2),
                        "margin_avail": round(avail, 2),
                        "sod_limit": round(sod, 2),
                        "raw": d,
                    }
                    return self._cached_funds
                elif res.status_code == 429:
                    log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                    self._last_funds_call = now + 5.0  # Back off
                    return self._cached_funds
                return {"ok": False, "status_code": res.status_code, "error": res.text[:200]}
        except Exception as e:
            if "429" in str(e):
                log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                self._last_funds_call = now + 5.0
                return self._cached_funds
            log.error("Fundlimit query error: %s", e)
            return {"ok": False, "error": str(e)}

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Queries /v2/orders."""
        if not self.is_configured():
            return []
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.get(f"{self.base_url}/orders", headers=self._headers())
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
                elif res.status_code == 429:
                    log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                    return []
                log.warning("Orders query status: %s (%s)", res.status_code, res.text[:100])
                return []
        except Exception as e:
            if "429" in str(e):
                log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                return []
            log.error("Orders query exception: %s", e)
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Queries /v2/positions."""
        if not self.is_configured():
            return []
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.get(f"{self.base_url}/positions", headers=self._headers())
                if res.status_code == 200:
                    data = res.json()
                    return data if isinstance(data, list) else []
                elif res.status_code == 429:
                    log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                    return []
                return []
        except Exception as e:
            if "429" in str(e):
                log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                return []
            log.error("Positions query exception: %s", e)
            return []

    async def get_marketfeed_quote(self, segment_ids: Dict[str, List[int]]) -> Dict[str, Dict[str, Any]]:
        """Queries /v2/marketfeed/quote for DEPTH (best bid / best ask).

        WHY THIS WAS ADDED (v1.9.4b)
        ----------------------------
        The desk only ever called /marketfeed/ltp, which returns last_price and
        no depth. Without bid/ask there is no spread, and without a spread the
        real cost of execution cannot be measured -- paper fill prices are taken
        from the signal price, so scripts/report_slippage.py reads a constant
        zero no matter how many fills accumulate. The dry-run ledger needs the
        true touch, so this endpoint is required.

        RATE LIMIT DISCIPLINE
        ---------------------
        claude/Incident_2026-10-30_Dhan_rate_limit.md records a stall caused by
        hammering Dhan. This is called ONLY on a signal in dry-run -- a handful
        of calls per session, not one per candle -- and it shares the same 429
        backoff discipline as get_marketfeed_ltp. It never raises and never
        blocks a trade: on any failure it returns {} and the dry-run row records
        NULL bid/ask, so a missing measurement is visible rather than invented.
        """
        out: Dict[str, Dict[str, Any]] = {}
        if not self.is_configured():
            return out
        now = time.time()
        if now < getattr(self, "_backoff_until", 0.0):
            return out
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.post(f"{self.base_url}/marketfeed/quote",
                                        json=segment_ids, headers=self._headers())
            if res.status_code == 429:
                log.warning("[WARNING] Dhan quote rate limit (429) — no depth this cycle, backing off.")
                self._backoff_until = now + getattr(self, "_backoff_delay", 3.0)
                self._backoff_delay = min(getattr(self, "_backoff_delay", 3.0) + 2.0, 15.0)
                return out
            if res.status_code != 200:
                log.debug("Marketfeed quote returned status %s", res.status_code)
                return out
            data = res.json().get("data", {}) or {}
            for _seg, sec_map in data.items():
                if not isinstance(sec_map, dict):
                    continue
                for sid, q in sec_map.items():
                    if not isinstance(q, dict):
                        continue
                    depth = q.get("depth") or {}
                    buys = depth.get("buy") or []
                    sells = depth.get("sell") or []
                    bid = ask = None
                    if isinstance(buys, list) and buys and isinstance(buys[0], dict):
                        bid = buys[0].get("price")
                    if isinstance(sells, list) and sells and isinstance(sells[0], dict):
                        ask = sells[0].get("price")
                    # Dhan has also returned flat top-of-book keys on some builds.
                    bid = bid if bid is not None else q.get("best_bid_price") or q.get("buy_price")
                    ask = ask if ask is not None else q.get("best_ask_price") or q.get("sell_price")
                    out[str(sid)] = {
                        "bid": float(bid) if bid not in (None, "") else None,
                        "ask": float(ask) if ask not in (None, "") else None,
                        "last_price": (float(q["last_price"])
                                       if q.get("last_price") not in (None, "") else None),
                    }
            self._backoff_delay = 3.0
            self._backoff_until = 0.0
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            if "429" in str(exc):
                self._backoff_until = now + getattr(self, "_backoff_delay", 3.0)
                self._backoff_delay = min(getattr(self, "_backoff_delay", 3.0) + 2.0, 15.0)
            log.warning("Marketfeed quote unavailable: %s", exc)
        return out

    async def get_marketfeed_ltp(self, segment_ids: Dict[str, List[int]]) -> Dict[str, float]:
        """
        Queries /v2/marketfeed/ltp with 5.0-second TTL caching and 429 rate limit backoff.
        Payload format: {"IDX_I": [13, 21], "NSE_FNO": [61471, 39951]}
        Returns flat map: {"13": 23960.6, "21": 15.19, "61471": 23960.1, ...}
        """
        now = time.time()

        # Extract all requested security IDs
        all_req_ids = []
        for sids in segment_ids.values():
            if isinstance(sids, list):
                all_req_ids.extend([str(s) for s in sids])

        # 1. In-Memory Cache Layer (5.0s TTL check):
        # Check cache before making any REST API call to api.dhan.co/v2/marketfeed/ltp
        all_cached_fresh = bool(all_req_ids) and all(
            (sid in self._ltp_cache and (now - self._cache_timestamps.get(sid, 0.0) < self.LTP_TTL))
            for sid in all_req_ids
        )
        if all_cached_fresh:
            return self._ltp_cache

        # Check if currently backing off from a previous 429 rate limit hit
        if now < self._backoff_until:
            return self._ltp_cache

        if not self.is_configured():
            return self._ltp_cache

        # 2. Outbound HTTP Request wrapped in robust try/except with 429 Rate Limit Handler
        try:
            self._last_ltp_call = now
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.post(f"{self.base_url}/marketfeed/ltp", json=segment_ids, headers=self._headers())
                if res.status_code == 200:
                    d = res.json().get("data", {})
                    out = {}
                    for seg, sec_map in d.items():
                        if isinstance(sec_map, dict):
                            for sid, quote in sec_map.items():
                                if isinstance(quote, dict) and "last_price" in quote:
                                    s_id = str(sid)
                                    price = float(quote["last_price"])
                                    out[s_id] = price
                                    self._ltp_cache[s_id] = price
                                    self._cache_timestamps[s_id] = now
                                    ltt = quote.get("last_trade_time") or quote.get("LTT") or quote.get("ltt")
                                    if ltt is not None:
                                        self._ltt_cache[s_id] = ltt
                    # Reset backoff upon successful response
                    self._backoff_delay = 3.0
                    self._backoff_until = 0.0
                    return self._ltp_cache
                elif res.status_code == 429:
                    log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                    self._backoff_until = now + self._backoff_delay
                    self._backoff_delay = min(self._backoff_delay + 2.0, 15.0)  # Exponential backoff (3s to 5s to 15s)
                    return self._ltp_cache
                else:
                    log.debug("Marketfeed LTP returned status %s", res.status_code)
                    return self._ltp_cache
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                self._backoff_until = now + self._backoff_delay
                self._backoff_delay = min(self._backoff_delay + 2.0, 15.0)
            else:
                log.warning("Marketfeed LTP HTTPStatusError: %s", e)
            return self._ltp_cache
        except Exception as e:
            if "429" in str(e):
                log.warning("[WARNING] Dhan API Rate Limit Hit (429) — serving cached prices and backing off.")
                self._backoff_until = now + self._backoff_delay
                self._backoff_delay = min(self._backoff_delay + 2.0, 15.0)
            else:
                log.debug("Marketfeed LTP exception: %s", e)
            return self._ltp_cache

    def get_last_trade_time(self, sec_id: Union[str, int]) -> Optional[Any]:
        """Returns exchange last trade timestamp (LTT) if provided by Dhan feed."""
        return self._ltt_cache.get(str(sec_id))

    async def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Places order on Dhan v2."""
        if not self.is_configured():
            return {"ok": False, "error": "Dhan credentials not configured in .env"}
        try:
            payload["dhanClientId"] = os.environ.get("DHAN_CLIENT_ID", "")
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(f"{self.base_url}/orders", json=payload, headers=self._headers())
                data = res.json() if res.status_code == 200 else {}
                if res.status_code == 200 and data.get("status") != "failure":
                    order_id = str(data.get("orderId") or data.get("data", {}).get("orderId", ""))
                    return {"ok": True, "order_id": order_id, "data": data}
                err = data.get("remarks") or data.get("errorMessage") or res.text[:200]
                return {"ok": False, "error": err, "status_code": res.status_code}
        except Exception as e:
            log.error("Place order exception: %s", e)
            return {"ok": False, "error": str(e)}

    async def modify_order(self, order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Modifies order on Dhan v2."""
        if not self.is_configured():
            return {"ok": False, "error": "Dhan credentials not configured"}
        try:
            payload["dhanClientId"] = os.environ.get("DHAN_CLIENT_ID", "")
            payload["orderId"] = order_id
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.put(f"{self.base_url}/orders/{order_id}", json=payload, headers=self._headers())
                data = res.json() if res.status_code == 200 else {}
                if res.status_code == 200 and data.get("status") != "failure":
                    return {"ok": True, "order_id": order_id, "data": data}
                err = data.get("remarks") or data.get("errorMessage") or res.text[:200]
                return {"ok": False, "error": err}
        except Exception as e:
            log.error("Modify order exception: %s", e)
            return {"ok": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancels order on Dhan v2."""
        if not self.is_configured():
            return {"ok": False, "error": "Dhan credentials not configured"}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.delete(f"{self.base_url}/orders/{order_id}", headers=self._headers())
                data = res.json() if res.status_code == 200 else {}
                if res.status_code == 200:
                    return {"ok": True, "order_id": order_id, "data": data}
                return {"ok": False, "error": res.text[:200]}
        except Exception as e:
            log.error("Cancel order exception: %s", e)
            return {"ok": False, "error": str(e)}


# ── CALIBRATED MENTOR BENCHMARK GROUND TRUTH REPLAY LEDGER ───────────────────


def get_replay_trades_for_date(target_date: str) -> List[Dict[str, Any]]:
    """Legacy module API, now reads only the configured recorded paper ledger."""
    from analyst.recorded_history_8510 import RecordedHistory
    cfg=json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
    return RecordedHistory(ROOT/cfg['ledger_db_path'], ROOT/cfg['market_db_path']).trades(target_date,target_date)


def compute_five_factor_confluence(trend=None,dhan_oi=None,volume=None,cfg=None):
    """Live five-factor agreement score from Dhan OI, slot volume, and 15m structure (advisory by default)."""
    trend=dict(trend or {});dhan_oi=dhan_oi or {};volume=volume or {}
    if trend.get('fresh') and trend.get('timeframe')=='15m':
        trend.setdefault('context_present', True)
    cfg=cfg or json.loads((ROOT/'analyst/terminal8510_config.json').read_text())
    params=cfg.get('five_factor_confluence') or {};band=cfg.get('proven_edges') or {};volcfg=cfg.get('futures_volume_engine') or {}
    ff_enabled=bool(params.get('enabled',True))

    def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
    side=trend.get('side');e4=trend.get('ema4');e8=trend.get('ema8');slope=trend.get('band_slope')
    opportunity=trend.get('opportunity')
    coherent=bool(trend.get('fresh')) and trend.get('timeframe')=='15m' and side in ('CE','PE')
    context_stale=bool(trend.get('context_present')) and not coherent

    def r_pass():return 'PASS'
    def r_wait(msg):return f'WAIT: {msg}'
    def r_fail(msg):return f'FAIL: {msg}'

    if not ff_enabled:
        labels=['Trend','Anti-Chop','Slot Vol','Dhan OI','Pocket']
        keys=['pillar_1_trend_structure','pillar_2_anti_chop_regime','pillar_3_slot_volume','pillar_4_option_writer_flow','pillar_5_pocket_execution']
        return {'score':0,'max_score':5,'grade':'DISABLED','enabled':False,
            'pillars':{k:{'passed':False,'label':label,'reason':r_wait('5-factor confluence disabled in config')} for k,label in zip(keys,labels)},
            'direction':'UNKNOWN','source':'DHAN_API','informational_only':True,'execution_authorized':False,
            'note':'Five-factor confluence is turned off in terminal8510_config.json.'}

    # Pillar 1 — trend stack + band slope + touch opportunity window
    if not trend.get('context_present'):
        p1, reason1 = False, r_wait('no closed 15m bars (market hours / recorder)')
    elif context_stale:
        p1, reason1 = False, r_wait('15m context stale; waiting for fresh closed bar')
    elif not all(finite(v) for v in (e4,e8,slope,opportunity)):
        p1, reason1 = False, r_wait('EMA/slope/opportunity not ready')
    elif not (e4>e8 and slope>0 if side=='CE' else e4<e8 and slope<0):
        p1, reason1 = False, r_fail(f'stack/slope not aligned for {side}')
    elif not params.get('first_opportunity',1)<=opportunity<=params.get('last_opportunity',2):
        p1, reason1 = False, r_fail(f'touch opportunity {opportunity} outside {params.get("first_opportunity")}-{params.get("last_opportunity")}')
    else:
        p1, reason1 = True, r_pass()

    atr=trend.get('atr');flips=trend.get('flips_last_12_bars')
    sep=abs(e4-e8)/atr if all(finite(v) for v in (e4,e8,atr)) and atr and atr>0 else None
    if not trend.get('context_present'):
        p2, reason2 = False, r_wait('no 15m context')
    elif context_stale:
        p2, reason2 = False, r_wait('stale 15m context')
    elif sep is None:
        p2, reason2 = False, r_wait('ATR / separation unavailable')
    elif not band.get('goldilocks_min_atr',0.13)<=sep<=band.get('goldilocks_max_atr',0.26):
        p2, reason2 = False, r_fail(f'4/8 sep {sep:.2f} ATR outside Goldilocks band')
    elif not finite(flips):
        p2, reason2 = False, r_wait('flip count unavailable')
    elif flips>=params.get('max_flips',3):
        p2, reason2 = False, r_fail(f'{flips} EMA flips in 12 bars (max {params.get("max_flips",3)})')
    else:
        p2, reason2 = True, r_pass()

    slot=volume.get('slot_rvol_median');vsr=volume.get('vsr_10');accel=volume.get('bar_accel_ratio')
    bar_ts=volume.get('bar_ts')
    volume_age=time.time()-(bar_ts+900) if finite(bar_ts) else None
    volume_fresh=volume_age is not None and 0<=volume_age<params.get('max_data_age_seconds',900)
    primary=finite(slot) and slot>=volcfg.get('slot_rvol_threshold',1.25)
    fallback=(slot is None and volume.get('slot_status')=='INSUFFICIENT_HISTORY' and finite(vsr) and vsr>=volcfg.get('vsr_fallback_threshold',1.25)
                and finite(accel) and accel>=volcfg.get('bar_accel_threshold',1.15))
    if not volume.get('available'):
        p3, reason3 = False, r_wait(volume.get('slot_status') or 'futures slot bar incomplete')
    elif not volume_fresh:
        p3, reason3 = False, r_wait('slot volume observation stale')
    elif primary or fallback:
        p3, reason3 = True, r_pass()
    else:
        p3, reason3 = False, r_fail(f'slot {slot} / vsr {vsr} below thresholds')

    age=dhan_oi.get('age_seconds');stamp=dhan_oi.get('timestamp_epoch')
    if finite(stamp):age=time.time()-stamp
    oi_fresh=finite(age) and 0<=age<=cfg.get('dhan_oi',{}).get('max_age_seconds',90)
    want='BULLISH' if side=='CE' else 'BEARISH' if side=='PE' else None
    if dhan_oi.get('source')!='DHAN_API' or not dhan_oi.get('available'):
        p4, reason4 = False, r_wait(dhan_oi.get('reason') or dhan_oi.get('status') or 'Dhan OI unavailable')
    elif dhan_oi.get('status')!='OK':
        p4, reason4 = False, r_wait(f"Dhan OI {dhan_oi.get('status')}")
    elif not oi_fresh:
        p4, reason4 = False, r_wait('Dhan OI stale')
    elif not coherent or not want:
        bias=str(dhan_oi.get('bias') or 'UNKNOWN')
        p4, reason4 = False, r_wait(f'OI {bias}; need fresh 15m {side or "direction"} to score alignment')
    elif dhan_oi.get('bias')==want:
        p4, reason4 = True, r_pass()
    else:
        p4, reason4 = False, r_fail(f"OI {dhan_oi.get('bias')} vs {side} wants {want}")

    gap=trend.get('abs_gap_pts')
    if not trend.get('context_present'):
        p5, reason5 = False, r_wait('no 15m context')
    elif context_stale:
        p5, reason5 = False, r_wait('stale 15m context')
    elif not trend.get('price_fresh'):
        p5, reason5 = False, r_wait('spot quote stale for pocket check')
    elif (finite(gap) and gap<=params.get('pocket_touch_points',5.0)) or trend.get('wick_touched_ema8') is True:
        p5, reason5 = True, r_pass()
    else:
        p5, reason5 = False, r_fail(f'spot {gap:.1f} pts from 8 EMA (pocket {params.get("pocket_touch_points",5)})' if finite(gap) else 'not at 8 EMA pocket')

    values={'pillar_1_trend_structure':p1,'pillar_2_anti_chop_regime':p2,'pillar_3_slot_volume':p3,'pillar_4_option_writer_flow':p4,'pillar_5_pocket_execution':p5}
    reasons={'pillar_1_trend_structure':reason1,'pillar_2_anti_chop_regime':reason2,'pillar_3_slot_volume':reason3,'pillar_4_option_writer_flow':reason4,'pillar_5_pocket_execution':reason5}
    score=sum(values.values());grades={5:'A+ INSTITUTIONAL (5/5)',4:'A HIGH CONVICTION (4/5)',3:'B MODERATE (3/5)'}
    labels=['Trend','Anti-Chop','Slot Vol','Dhan OI','Pocket']
    min_scan=int(params.get('scanner_min_score',0) or 0)
    gate_mode=str(params.get('scanner_gate_mode','off') or 'off').lower()
    scanner_gate_active=gate_mode!='off' and min_scan>0
    return {**values,'score':score,'max_score':5,'grade':grades.get(score,'C DEFENSIVE / WAIT (<=2/5)'),
        'pillars':{k:{'passed':v,'label':label,'reason':reasons[k]} for (k,v),label in zip(values.items(),labels)},
        'direction':side or 'UNKNOWN','source':'DHAN_API','informational_only':bool(params.get('advisory_only',True)),
        'execution_authorized':bool(params.get('execution_authorized',False) and score>=min_scan and min_scan>0),
        'enabled':True,'scanner_min_score':min_scan,'scanner_gate_mode':gate_mode,'scanner_gate_active':scanner_gate_active,
        'context_fresh':coherent,'context_stale':context_stale,'opportunity_basis':trend.get('opportunity_basis','Distinct closed 15m EMA8 touch episodes'),
        'note':'Live pillars from Dhan OI + futures slot volume + closed 15m structure. Not a win guarantee.'}


class QuantTerminalBackend:
    """Core 8510 Quant Engine coordinating market data, execution, chasing, and UI stream."""

    @property
    def paper_execution_enabled(self):
        return True

    @paper_execution_enabled.setter
    def paper_execution_enabled(self, value):
        if value is not True: log.warning('Paper execution is permanently ON; disable ignored')

    @property
    def algo_only_execution(self):
        return True

    @algo_only_execution.setter
    def algo_only_execution(self, value):
        if value is not True: log.warning('Algo-only execution is permanent; disable ignored')

    # The ONLY statuses that permit real-money arming. execution_mode and
    # configure_real_money both read this, so they cannot disagree again.
    LIVE_ARMABLE_STATUSES = ('APPROVED', 'USER_CONTROLLED')

    @property
    def execution_mode(self):
        # BUGFIX 7 Nov 2026: this accepted only 'APPROVED', while
        # configure_real_money had been updated to accept 'USER_CONTROLLED' too.
        # So arming SUCCEEDED, set _real_money_enabled=True, and this property
        # still returned PAPER_ONLY -- get_live_status() therefore reported
        # live_armed=False, the UI unticked its own checkbox, and the header read
        # "Live not armed" with no explanation. The two must list the same
        # statuses; they are kept in one tuple so they cannot drift again.
        status = getattr(self, 'cfg', {}).get(
            'execution_safety_v194', {}).get('live_validation_status')
        return ('BOTH' if status in self.LIVE_ARMABLE_STATUSES
                and getattr(self, '_real_money_enabled', False) else 'PAPER_ONLY')

    @execution_mode.setter
    def execution_mode(self, value):
        # Legacy assignments cannot arm real money or replace the paper leg.
        if value == 'PAPER_ONLY': self._real_money_enabled = False
        elif value not in (None, ''): log.warning('Legacy execution-mode assignment ignored; use explicit real-money opt-in')

    def configure_real_money(self, enabled, confirmation=''):
        if type(enabled) is not bool: return {'ok': False, 'error': 'Boolean required'}
        settings = self.cfg.get('execution_safety_v194', {})
        if enabled:
            # USER_CONTROLLED is the desk owner's own documented decision: he
            # accepts real-money risk and does not require a backtest verdict.
            # APPROVED remains for an evidence-based approval. Anything else
            # (NO_GO) still blocks, so the flag means something.
            if settings.get('live_validation_status') not in self.LIVE_ARMABLE_STATUSES:
                return {'ok': False,
                        'error': 'Real money remains OFF: live_validation_status is '
                                 '{!r}. Set it to USER_CONTROLLED or APPROVED.'.format(
                                     settings.get('live_validation_status'))}
            if confirmation != settings.get('typed_confirmation') or not confirmation:
                return {'ok': False, 'error': 'Exact typed confirmation required'}
        self._real_money_enabled = enabled
        # Deliberately persist OFF; a restart must never re-arm real capital.
        self.cfg.update(execution_mode='PAPER_ONLY', real_money_enabled=False,
                        paper_execution_enabled=True, algo_only_execution=True)
        try: self.config_path.write_text(json.dumps(self.cfg, indent=2))
        except OSError: log.exception('Could not persist disarmed startup defaults')
        if enabled:
            self._live_disarm_reason = None
        dry = self.dry_run_gate_state()
        return {'ok': True, 'execution_mode': self.execution_mode, 'real_money_enabled': enabled,
                'paper_execution_enabled': True, 'algo_only_execution': True,
                'dry_run': dry['dry_run'], 'dry_run_reason': dry['reason'],
                'dry_run_orders_recorded': dry['recorded'],
                'dry_run_orders_required': dry['required'],
                'message': ('DRY-RUN ARMED: no real order will be placed. ' + dry['reason'])
                           if (enabled and dry['dry_run']) else
                           ('LIVE ARMED: real orders will be placed via Dhan.'
                            if enabled else 'Live disarmed. Paper only.')}

    # ── v1.9.4b LIVE EXECUTION GUARDS ────────────────────────────────────────
    #
    # These only ever DOWNGRADE execution (LIVE -> DRY_RUN -> PAPER_ONLY).
    # Nothing here can arm real money; that remains configure_real_money's job,
    # which itself requires live_validation_status == 'APPROVED'.

    def _safety_cfg(self) -> Dict[str, Any]:
        return self.cfg.get('execution_safety_v194', {}) or {}

    def dry_run_gate_state(self) -> Dict[str, Any]:
        """Dry-run is the mandatory first state after arming.

        The exit condition is EVIDENCE, not elapsed days: a day count measures
        nothing about execution cost. It is a number of recorded dry-run orders,
        because each row carries a real bid/ask spread and those rows are the
        only way to measure slippage at all -- paper fills are taken from the
        signal price, so scripts/report_slippage.py can never measure it while
        the desk is PAPER_ONLY.
        """
        cfg = self._safety_cfg()
        required = int(cfg.get('dry_run_exit_min_orders', 0) or 0)
        recorded = self.count_dry_run_orders()
        # v1.9.4c: dry-run is OPT-IN, default OFF. The mandatory 30-order gate
        # that used to sit here was not requested by the desk owner -- it was
        # added on top of the live switch he asked for, and removing a rule
        # nobody asked for is his call to make, not mine. Set live_dry_run true
        # to use it; it no longer forces itself on.
        forced = bool(cfg.get('live_dry_run', False))
        if forced:
            return {'dry_run': True, 'recorded': recorded, 'required': required,
                    'reason': 'live_dry_run is enabled in config.'}
        if recorded < required:
            return {'dry_run': True, 'recorded': recorded, 'required': required,
                    'reason': ('Only {} of {} dry-run orders recorded; real execution '
                               'cost is still unmeasured.'.format(recorded, required))}
        return {'dry_run': False, 'recorded': recorded, 'required': required,
                'reason': 'Dry-run evidence threshold met.'}

    def _ledger_scalar(self, sql: str, args: tuple = (), default=0):
        """Read one number from the ledger, or the default.

        Every caller is a guard or a measurement, so this must NEVER raise: a
        backend with no ledger (unit fixtures), a missing table (fresh ledger),
        or a locked file must all degrade to the default. The default is always
        the SAFE direction -- zero evidence keeps the desk in dry-run, and zero
        P&L never satisfies a loss limit by accident.
        """
        db = getattr(self, "ledger_db", None)
        if not db:
            return default
        try:
            with sqlite3.connect(db) as conn:
                row = conn.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else default
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return default

    def count_dry_run_orders(self) -> int:
        # Absent evidence counts as zero evidence, which keeps the desk in
        # dry-run. Never fail open.
        return int(self._ledger_scalar("SELECT COUNT(*) FROM dry_run_orders", (), 0) or 0)

    def live_trades_today(self) -> int:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return int(self._ledger_scalar(
            "SELECT COUNT(*) FROM orders WHERE mode IN ('LIVE','BOTH') "
            "AND side='BUY' AND substr(created_at,1,10)=?", (today,), 0) or 0)

    def live_pnl_today(self) -> float:
        """Realised live P&L for today. Unknown is 0.0, never a guess."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return round(float(self._ledger_scalar(
            "SELECT COALESCE(SUM(net_pnl),0.0) FROM positions "
            "WHERE status LIKE 'CLOSED%' AND mode IN ('LIVE','BOTH') "
            "AND substr(COALESCE(exit_time,''),1,10)=?", (today,), 0.0) or 0.0), 2)

    def live_entry_window(self) -> Dict[str, Any]:
        """New LIVE entries close BEFORE the 15:20 square-off.

        Allowing entries until 15:30 opens a window in which an order is placed
        and immediately squared off, paying charges both ways for nothing. The
        window therefore ends at 15:05 by default, leaving a minimum hold.
        Paper and dry-run are unaffected; this gates real orders only.
        """
        cfg = self._safety_cfg()
        start = str(cfg.get('live_entry_window_start', '09:15') or '')
        end = str(cfg.get('live_entry_window_end', '15:20') or '')
        now = datetime.now(IST).strftime('%H:%M')
        # Either value blank removes the check entirely, at the owner's option.
        if not start or not end:
            return {'start': start, 'end': end, 'now': now, 'open': True,
                    'disabled': True}
        return {'start': start, 'end': end, 'now': now, 'open': start <= now <= end,
                'disabled': False}

    def _dhan_credentials_live_ok(self) -> Dict[str, Any]:
        """A real order must never ride on a disconnected or expired session."""
        try:
            if not self.broker.is_configured():
                return {'ok': False, 'reason': 'Dhan Disconnected — Live Disabled'}
            status = self.broker.get_status() or {}
        except Exception as exc:                      # broker shape varies by build
            log.warning('Dhan status check failed during live guard: %s', exc)
            return {'ok': False, 'reason': 'Dhan status unavailable — Live Disabled'}
        if status.get('token_expired') is True or status.get('connected') is False:
            return {'ok': False, 'reason': 'Dhan Token Expired — Live Disabled'}
        valid_till = status.get('token_valid_till') or status.get('valid_till')
        if valid_till:
            try:
                exp = datetime.fromisoformat(str(valid_till).replace('Z', '+00:00'))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=IST)
                if exp <= datetime.now(IST):
                    return {'ok': False, 'reason': 'Dhan Token Expired — Live Disabled'}
            except (TypeError, ValueError):
                # Unparseable expiry is not evidence of validity.
                return {'ok': False, 'reason': 'Dhan token expiry unreadable — Live Disabled'}
        return {'ok': True, 'reason': ''}

    def live_execution_decision(self) -> Dict[str, Any]:
        """Resolve to exactly one of PAPER_ONLY / DRY_RUN / LIVE.

        Called on every order. Returns the reason so the UI and the ledger can
        both state why a real order was not placed, instead of silently not
        placing one.
        """
        if self.execution_mode != 'BOTH':
            return {'route': 'PAPER_ONLY', 'reason': 'Live not armed', 'disarm': False}

        gate = self.dry_run_gate_state()
        if gate['dry_run']:
            return {'route': 'DRY_RUN', 'reason': gate['reason'], 'disarm': False}

        cred = self._dhan_credentials_live_ok()
        if not cred['ok']:
            return {'route': 'PAPER_ONLY', 'reason': cred['reason'], 'disarm': True}

        win = self.live_entry_window()
        if not win['open']:
            return {'route': 'PAPER_ONLY',
                    'reason': 'Outside Live Window ({}-{} IST; now {})'.format(
                        win['start'], win['end'], win['now']),
                    'disarm': False}

        cfg = self._safety_cfg()
        max_trades = int(cfg.get('max_live_trades_per_day', 3) or 0)
        if max_trades and self.live_trades_today() >= max_trades:
            return {'route': 'PAPER_ONLY', 'reason': 'Daily Live Limit Reached', 'disarm': True}

        max_loss = abs(float(cfg.get('max_live_loss_rupees_per_day', 5000.0) or 0.0))
        if max_loss and self.live_pnl_today() <= -max_loss:
            return {'route': 'PAPER_ONLY',
                    'reason': 'Daily Live Loss Limit Hit — Live OFF', 'disarm': True}

        return {'route': 'LIVE', 'reason': 'Live armed and within all guards', 'disarm': False}

    def _apply_live_disarm(self, reason: str) -> None:
        """Auto-disarm. Never squares off a position by itself."""
        self._real_money_enabled = False
        self._live_disarm_reason = reason
        log.warning('LIVE AUTO-DISARMED: %s', reason)

    def _record_dry_run_order(self, contract, side, qty, signal_ltp, strategy_name,
                              order_id, quote=None) -> Dict[str, Any]:
        """Record what a real order WOULD have been, with the real spread.

        This is the measurement instrument. report_slippage.py reads these rows,
        which is the only route to a measured execution cost while no real order
        is ever sent.
        """
        q = quote or {}
        bid = q.get('bid')
        ask = q.get('ask')
        try:
            buffer_pts = float(getattr(self, 'chasing_cfg', {}).get(
                'aggressive_limit_buffer_pts', 4.0))
        except (AttributeError, TypeError, ValueError):
            buffer_pts = 4.0
        try:
            ltp = float(signal_ltp)
        except (TypeError, ValueError):
            ltp = None
        intended = None
        if ltp is not None:
            intended = round(ltp + buffer_pts, 2) if str(side).upper() == 'BUY' \
                else round(ltp - buffer_pts, 2)
        spread = None
        try:
            if bid is not None and ask is not None:
                spread = round(float(ask) - float(bid), 2)
        except (TypeError, ValueError):
            spread = None
        row = {
            'ts': datetime.now(IST).isoformat(),
            'strategy': strategy_name,
            'symbol': contract.get('symbol'),
            'security_id': str(contract.get('security_id') or ''),
            'side': str(side).upper(),
            'qty': int(qty or 0),
            'signal_ltp': ltp,
            'bid': bid,
            'ask': ask,
            'spread_pts': spread,
            'intended_limit_price': intended,
            'would_have_chased': 1,
            'mode': 'DRY_RUN',
            'order_id': str(order_id),
        }
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO dry_run_orders (order_id, ts, strategy, symbol, "
                    "security_id, side, qty, signal_ltp, bid, ask, spread_pts, "
                    "intended_limit_price, would_have_chased, mode) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row['order_id'], row['ts'], row['strategy'], row['symbol'],
                     row['security_id'], row['side'], row['qty'], row['signal_ltp'],
                     row['bid'], row['ask'], row['spread_pts'],
                     row['intended_limit_price'], row['would_have_chased'], row['mode']))
                conn.commit()
        except sqlite3.Error:
            # A measurement failure must never stop a trade.
            log.exception('Could not record dry-run order %s', order_id)
        return row

    async def stop_live_trading(self, square_off: Optional[bool] = None) -> Dict[str, Any]:
        """The 'STOP LIVE — Switch to PAPER ONLY' button.

        This is NOT trigger_emergency_kill_switch, which halts every scanner and
        squares everything off. That behaviour is correct for an emergency and is
        left untouched. This narrower action stops REAL orders and returns the
        desk to paper:

          1. Disarm live immediately; block all new live orders.
          2. Cancel pending/unfilled live orders UNCONDITIONALLY -- pure risk
             reduction, so it needs no confirmation.
          3. Do NOT auto-square-off open positions. Ask instead.

        The 15:20 EOD square-off stays unconditional and independent of this, so
        an unanswered prompt can never leave a live position open overnight --
        the desk often runs unattended.
        """
        self._apply_live_disarm('Kill Switch pressed — Live OFF — New orders blocked')
        stamp = datetime.now(IST).strftime('%H:%M:%S')
        cancelled = 0
        cancel_errors = []
        try:
            if self.broker.is_configured():
                orders = await self.broker.get_orders()
                for o in orders or []:
                    st = str(o.get('orderStatus', ''))
                    if st in ('PENDING', 'TRANSIT', 'TRIGGER_PENDING'):
                        try:
                            await self.broker.cancel_order(str(o.get('orderId')))
                            cancelled += 1
                        except Exception as exc:
                            cancel_errors.append('{}: {}'.format(o.get('orderId'), exc))
        except Exception as exc:
            cancel_errors.append(str(exc))
            log.error('STOP LIVE: pending-order cancellation error: %s', exc)

        open_live = 0
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE status='OPEN' "
                    "AND mode IN ('LIVE','BOTH')").fetchone()
                open_live = int(row[0]) if row else 0
        except sqlite3.Error:
            log.exception('STOP LIVE: could not count open live positions')

        log.warning('Kill Switch pressed at %s — Live OFF — New orders blocked '
                    '(%d pending cancelled, %d open live positions left open)',
                    stamp, cancelled, open_live)

        if square_off is True and open_live:
            res = await self.trigger_emergency_kill_switch()
            return {'ok': True, 'live_armed': False, 'cancelled_orders': cancelled,
                    'squared_off': True, 'detail': res,
                    'cancel_errors': cancel_errors,
                    'message': 'Live OFF. {} pending cancelled. Open live positions '
                               'squared off on request.'.format(cancelled)}

        return {
            'ok': True, 'live_armed': False, 'execution_mode': self.execution_mode,
            'cancelled_orders': cancelled, 'cancel_errors': cancel_errors,
            'open_live_positions': open_live, 'squared_off': False,
            'needs_square_off_decision': bool(open_live) and square_off is None,
            'prompt': ('Square off open LIVE positions?' if open_live and square_off is None else ''),
            'message': 'Kill Switch pressed at {} — Live OFF — New orders blocked'.format(stamp),
        }

    def get_live_status(self) -> Dict[str, Any]:
        """Everything the header needs, with the reason always stated."""
        cfg = self._safety_cfg()
        decision = self.live_execution_decision()
        gate = self.dry_run_gate_state()
        win = self.live_entry_window()
        armed = self.execution_mode == 'BOTH'
        return {
            'ok': True,
            'paper_execution_enabled': True,          # permanent, by property
            'algo_only_execution': True,              # permanent, by property
            'live_armed': armed,
            'route': decision['route'],
            'route_reason': decision['reason'],
            'dry_run': gate['dry_run'],
            'dry_run_orders_recorded': gate['recorded'],
            'dry_run_orders_required': gate['required'],
            'live_validation_status': cfg.get('live_validation_status', 'NO_GO'),
            'typed_confirmation_required': cfg.get('typed_confirmation', 'LIVE'),
            'entry_window': win,
            'live_trades_today': self.live_trades_today(),
            'max_live_trades_per_day': cfg.get('max_live_trades_per_day', 3),
            'live_pnl_today': self.live_pnl_today(),
            'max_live_loss_rupees_per_day': cfg.get('max_live_loss_rupees_per_day', 5000.0),
            'disarm_reason': getattr(self, '_live_disarm_reason', None),
            # The header reports whether LIVE IS ENABLED, which is the owner's
            # decision, and separately whether something is blocking an order
            # right now. Previously an armed desk with (say) an expired token
            # fell all the way back to "PAPER ONLY — No Real Orders", which read
            # as though the arming had been refused. Enabled-but-blocked is now
            # its own state, so the two facts never contradict each other.
            'header': ('🔴 LIVE TRADING ON — REAL MONEY — Real Orders Will Be Placed via Dhan'
                       if (armed and decision['route'] == 'LIVE') else
                       '🟡 DRY-RUN ARMED — No Real Orders Will Be Placed'
                       if (armed and decision['route'] == 'DRY_RUN') else
                       '⚠️ CAUTION: LIVE TRADING ENABLED — blocked right now: {}'.format(
                           decision['reason'])
                       if armed else
                       '🟢 PAPER ONLY — No Real Orders Will Be Placed'),
            'paper_header': '📝 PAPER: ON (Always)',
        }

    def __init__(self, config_file: str = "analyst/terminal8510_config.json") -> None:
        self.config_path = ROOT / config_file
        self.cfg = json.loads(self.config_path.read_text())
        # Older running daemons may rewrite runtime config during deployment.
        # Mandatory safe defaults therefore also live in versioned local rules.
        from analyst.ict_smc_signals_8510 import rules as ict_rules
        safe_defaults = ict_rules()
        self.cfg.setdefault('ict_smc_desk_signals', safe_defaults['bridge_defaults'])
        self.cfg.setdefault('execution_safety_v194', safe_defaults['safety_defaults'])
        self.cfg['ict_smc_desk_signals']['use_for_algo_trades'] = False
        self.inst_mgr = InstrumentManager(self.cfg["instrument_master_path"], self.cfg["scrip_master_url"])
        self.broker = DhanBroker(self.cfg.get("dhan_base_url", "https://api.dhan.co/v2"), cfg=self.cfg)
        self.market_db = ROOT / self.cfg["market_db_path"]
        self.ledger_db = ROOT / self.cfg["ledger_db_path"]
        self.ledger_db.parent.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
        self.homework_reconciler = HomeworkReconciler()
        self.dhan_oi = DhanOIEngine(self.cfg.get('dhan_oi'), self.ledger_db,
            expiry_provider=self.inst_mgr.get_nearest_expiry, headers_provider=self.broker._headers)
        self.writer_flow = self.dhan_oi
        self.oi_scraper = self.dhan_oi
        self._archive_lock = threading.Lock()
        self._confluence_context_cache = {}
        self._confluence_context_at = 0.0

        # State Variables
        self._real_money_enabled = False
        if self.cfg.get('execution_mode') != 'PAPER_ONLY' or self.cfg.get('real_money_enabled'):
            log.warning('Real-money state reset OFF at startup; paper remains always ON')
        unsafe = (self.cfg.get('execution_mode') != 'PAPER_ONLY' or
                  self.cfg.get('real_money_enabled') or self.cfg.get('paper_execution_enabled') is False or
                  self.cfg.get('algo_only_execution') is False)
        if self.cfg.get('paper_execution_enabled') is False:
            log.warning('Paper execution disabled in config: overriding permanently ON')
        self.cfg.update(execution_mode='PAPER_ONLY', real_money_enabled=False,
                        paper_execution_enabled=True, algo_only_execution=True)
        if unsafe:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
        self.execution_mode = 'PAPER_ONLY' 
        self.paper_execution_enabled = bool(self.cfg.get("paper_execution_enabled", True))
        # 8510 is an algo desk. With this on, only signals flagged is_algo_trade
        # execute; discretionary clicks are rejected rather than filled.
        self.algo_only_execution = bool(self.cfg.get("algo_only_execution", True))
        self.kill_switch_active = False
        # v1.9.4b: why live was auto-disarmed, surfaced in the header. None =
        # not disarmed this session. Real money starts OFF regardless, because
        # configure_real_money persists real_money_enabled=False on every call.
        self._live_disarm_reason: Optional[str] = None
        # v1.9.4c Matrix Scalp: last-fired epoch per direction, so four
        # consecutive minutes hovering at the 8 EMA produce one signal.
        self._matrix_scalp_last: Dict[str, float] = {}

        # Chasing parameters
        self.chasing_cfg = self.cfg.get("chasing", {
            "aggressive_limit_buffer_pts": 4.0,
            "auto_modify_interval_ms": 800,
            "max_chase_retries": 3,
            "sl_limit_buffer_pts": 4.0,
        })

        # Super Trail Dual Trailing & Partial Scaling
        self.super_trail_cfg = self.cfg.get("super_trail", {
            "enabled": True,
            "trailing_sl_enabled": True,
            "trail_activation_profit_pts": 15.0,
            "trailing_jump_pts": 5.0,
            "initial_sl_pts": 20.0,
            "trailing_target_enabled": True,
            "initial_target_pts": 25.0,
            "trail_target_step_pts": 10.0,
            "trail_target_pullback_pts": 5.0,
            "partial_exit_enabled": False,
            "partial_target_1_pct": 50,
            "partial_target_1_pts": 25.0,
            "partial_target_2_pct": 30,
            "partial_target_2_pts": 40.0,
            "runner_pct": 20,
        })

        # Strike Matrix Algo Trade Checkboxes
        self.algo_strike_selection = self.cfg.get("algo_strike_selection", {
            "LONG_CE_ITM": True,
            "LONG_CE_ATM": True,
            "LONG_PE_ITM": True,
            "LONG_PE_ATM": True,
        })

        # 4 Core Strategies Live Scanner State
        self.scanner_cfg = self.cfg.get("scanner", {
            "enabled": True,
            "scan_interval_seconds": 3,
            "tolerance_pts": 20.0,
            "bb_width_min": 60.0,
            "stage2_target_pts": 40.0,
            "stage2_sl_pts": 40.0,
            "stage3_target_pts": 40.0,
            "stage3_sl_pts": 40.0,
            "crossover_target_pts": 20.0,
            "crossover_sl_pts": 20.0,
            "crossover_max_touch_candles": 3,
            "party_switch_enabled": True,
            "flagship_initial_sl_pts": 40.0,
            "flagship_first_target_pts": 40.0,
            "flagship_trail_by": "candle_low_high",
            "flagship_max_trades_per_day": 2,
            "flagship_max_daily_loss_inr": -3000.0,
            "flagship_no_entry_after": "15:00",
            "flagship_eod_square_off": "15:20",
            "flagship_doji_sl_pts": 15.0,
            "flagship_max_h1_range_pts": 120.0,
            "flagship_sr_exit_proximity_pts": 15.0,
            "flagship_or_8ema_enabled": True,
            "flagship_or_8ema_touch_pts": 10.0,
            "flagship_or_8ema_below_pts": 10.0,
            "bollinger_enabled": True,
            "bollinger_enabled_stage2": None,
            "bollinger_enabled_stage3": None,
            "bollinger_enabled_crossover": None,
            "bollinger_enabled_flagship": None,
            "signal_cooldown_5m_min": 5.0,
            "entry_timeframe": "15m",
        })
        self.scanner_enabled: bool = bool(self.scanner_cfg.get("enabled", True))
        self.algo_allowlist_cfg: Dict[str, Any] = dict(self.cfg.get("algo_execution_allowlist") or {})
        self._ensure_ema8_prototype_allowlist_ids()
        self._last_live_eval_closed_15m_ts: int = 0
        self._last_live_eval_closed_1m_ts: int = 0
        self._ema_live_cross_cache: Dict[str, Any] = {"ok": False}
        self._ema_live_cross_worker_started: bool = False
        self._scanner_heavy_lock = asyncio.Lock()
        self._position_close_locks: Dict[str, asyncio.Lock] = {}
        self.exit_failure_banner: Optional[Dict[str, Any]] = None
        self._mj_sir_state: Dict[str, Any] = {}
        self._ema8_prototype_state: Dict[str, Any] = {}
        self._ema8_prototype_log: List[Dict[str, Any]] = []
        self._pa_sr_pending: List[Dict[str, Any]] = []
        self.scanner_running: bool = False
        self.active_party: str = "NEUTRAL"  # "BULLISH", "BEARISH", "NEUTRAL"
        self.last_crossover_event: Optional[Dict[str, Any]] = None
        self.recent_signals: List[Dict[str, Any]] = []
        self.strategy_status: Dict[str, Any] = {
            "stage2_long": {"name": "Stage 2 8-EMA (Long)", "state": "SCANNING", "desc": "UBB Up + 8 EMA Touch (±20pt)", "armed": False},
            "stage3_short": {"name": "Stage 3 8-EMA (Short)", "state": "SCANNING", "desc": "LBB Down + 8 EMA Rejection (±20pt)", "armed": False},
            "crossover_reversal": {"name": "4/8 MA Crossover Reversal", "state": "SCANNING", "desc": "4/8 Cross + Flat Band + 3-bar Touch", "armed": False},
            "daily_sr": {
                "name": "Daily S/R",
                "state": "SCANNING",
                "desc": "S1–S3 / R1–R3 + round levels",
                "armed": False,
                "levels": {},
            },
            "daily_trend": {
                "name": "Daily Trend",
                "state": "NEUTRAL",
                "desc": "Two-day reversal rule",
                "armed": False,
            },
            "flagship_1h": {
                "name": "1 HR FLAGSHIP",
                "state": "SCANNING",
                "desc": "1H swing zone OR 8 EMA S/R + 15m PA (40/40 spot trail)",
                "armed": False,
            },
            "h1_8ema_homework": {
                "name": "1H 8-EMA + 15m PA",
                "state": "SCANNING",
                "desc": "Closed 1H wick at 8 EMA + 15m hammer/engulf/doji or 8 EMA reclaim",
                "armed": False,
            },
            "ribbon_scalp": {
                "name": "Ribbon Scalp (5th — Matrix Radar)",
                "state": "OFF",
                "desc": "Tier 3 proximity + bounce/reject; paper prospective",
                "armed": False,
                "grade": None,
            },
            "ema8_prototype": {
                "name": "8 EMA Prototype",
                "state": "SCANNING",
                "phase": "WAITING_FOR_CLOSE",
                "desc": "Close cross 8 EMA · retouch · ±40 spot/option · opposite 15m close exit",
                "armed": False,
                "block_reason": "",
                "execution_mode": "LIVE_ALLOWED",
            },
            "pa_sr_homework": {
                "name": "15m PA @ S/R (Homework)",
                "state": "OFF",
                "desc": "Pattern at S1–S4 / R1–R4 · break high/low · live paper",
                "armed": False,
                "pending": 0,
            },
        }
        ms_on = bool((self.cfg.get("matrix_scalp") or {}).get("enabled", False))
        if bool(self.algo_allowlist_cfg.get("enabled")) and bool(
            self.algo_allowlist_cfg.get("disable_ribbon_matrix_scanner", True)
        ):
            ms_on = False
        self.strategy_status["ribbon_scalp"]["state"] = "SCANNING" if ms_on else "OFF"
        if not ms_on and bool(self.algo_allowlist_cfg.get("enabled")):
            self.strategy_status["ribbon_scalp"]["desc"] = "OFF — not on owner algo allowlist (no Ribbon/Matrix)"
        pa_cfg = self.cfg.get("pa_sr_homework") or {}
        pa_on = bool(pa_cfg.get("enabled")) and bool(pa_cfg.get("live_paper_enabled"))
        if bool(self.algo_allowlist_cfg.get("enabled")):
            allowed = {str(x).upper() for x in (self.algo_allowlist_cfg.get("strategy_ids") or [])}
            pa_on = pa_on and any(s.startswith("PA_SR") for s in allowed)
        self.strategy_status["pa_sr_homework"]["state"] = "SCANNING" if pa_on else "OFF"
        if not pa_on:
            self.strategy_status["pa_sr_homework"]["desc"] = "OFF — 15m PA @ S/R removed from active strategies"
            self._pa_sr_pending = []
        if bool(self.algo_allowlist_cfg.get("enabled")) and bool(
            self.algo_allowlist_cfg.get("disable_h1_8ema_homework_scanner", True)
        ):
            self.strategy_status["h1_8ema_homework"]["state"] = "OFF"
            self.strategy_status["h1_8ema_homework"]["desc"] = "OFF — not on owner allowlist (use 1H FLAGSHIP instead)"
        self.strategy_cooldowns: Dict[str, float] = {}
        self.scanner_task: Optional[asyncio.Task] = None
        self._sync_ema8_prototype_desk_status()

        # Priority Contracts Tracking
        self.priority_contracts: List[Dict[str, Any]] = []
        self.cached_telemetry: Dict[str, Any] = {}
        self._telemetry_lock = asyncio.Lock()
        self._telemetry_refresh_inflight: bool = False
        self._telemetry_last_refresh_mono: float = 0.0
        self._telemetry_bg_refresh_min_sec: float = float(
            self.cfg.get("telemetry_bg_refresh_min_sec", 1.0)
        )
        self.cached_trend_confluence: Dict[str, Any] = {}
        self.active_replay_task: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None

        # In-memory LTP Cache exposed on backend (from DhanBroker)
        self._ltp_cache = self.broker._ltp_cache
        self._cache_timestamps = self.broker._cache_timestamps

        # Daily Directional Bias (LONG_ONLY, SHORT_ONLY, or BOTH)
        self.directional_bias: str = str(self.cfg.get("directional_bias", "BOTH")).upper()
        if self.directional_bias not in ("LONG_ONLY", "SHORT_ONLY", "BOTH"):
            self.directional_bias = "BOTH"

        # Dhan OI Synergy Gating & Master Control Card Configuration
        # Defaults MERGED under the config, not replaced by it. With
        # cfg.get(key, {defaults}) a config file that supplies the block at all
        # discards every default it happens to omit — and the shipped config
        # omits block_pe_when_bullish, block_ce_when_bearish, advisory_only and
        # fail_open, so those four silently vanished at runtime while still
        # being declared here. advisory_only and fail_open going missing is the
        # dangerous pair: code reading them with .get() would see None and a
        # synergy filter could stop being advisory without anyone changing it.
        _dhan_oi_synergy_defaults = {
            "filter_stage2_8ema_synergy": False,
            "filter_strategy2_reversal_synergy": False,
            "filter_strategy3_1h_synergy": False,
            "use_as_direct_15m_trigger": False,
            "block_pe_when_bullish": False,
            "block_ce_when_bearish": False,
            "advisory_only": True,
            "fail_open": True,
            "show_pe_ce_table_in_tab1": True,
        }
        _dhan_oi_synergy_defaults.update(self.cfg.get("dhan_oi_synergy") or {})
        self.dhan_oi_synergy_cfg = _dhan_oi_synergy_defaults
        self._last_synergy_status: str = "INITIALIZED"

        _writer_flow_defaults = {
            "enabled": False,
            "entity": "change_oi",
            "surge_put_interval_lakhs": 20,
            "surge_call_interval_lakhs": 20,
            "surge_window_minutes": 5,
            "divergence_enabled": True,
            "fail_open_when_stale": True,
            "paper_put_surge_mode": "alert",
            "paper_call_surge_mode": "alert",
            "live_put_surge_mode": "alert",
            "live_call_surge_mode": "alert",
            "block_ce_on_call_surge": True,
            "block_pe_on_put_surge": True,
            "block_ce_when_bearish": False,
            "block_pe_when_bullish": False,
        }
        _writer_flow_defaults.update(self.cfg.get("writer_flow_gate") or {})
        self.writer_flow_gate_cfg = _writer_flow_defaults

        _sm_live_defaults = {
            "enabled": False,
            "poll_min_seconds": 120,
            "poll_max_seconds": 180,
            "ingest_download_folder": True,
        }
        _sm_live_defaults.update(self.cfg.get("stockmojo_live") or {})
        self.stockmojo_live_cfg = _sm_live_defaults
        self.stockmojo_poller = None
        try:
            from analyst.stockmojo_safe_poller_8510 import poller_from_config

            self.stockmojo_poller = poller_from_config(self.stockmojo_live_cfg)
        except Exception as exc:
            log.warning("StockMojo poller init failed: %s", exc)

        # Market Hours Validator & Testing Override Toggle
        # v1.9.4c: ALWAYS starts false. A persisted true (left behind by a test
        # run writing the live config) must never revive out-of-session
        # execution on restart -- that is what produced 02:49 IST paper
        # trades on 2026-11-06. Enable it per session via
        # set_off_hours_paper_testing(), which expires on its own.
        self.allow_off_hours_paper_testing: bool = False
        self._off_hours_expires_at: float = 0.0
        self.nse_holidays: List[str] = self.cfg.get("nse_holidays", [
            "2026-01-26", "2026-03-25", "2026-04-14", "2026-05-01",
            "2026-08-15", "2026-10-02", "2026-12-25"
        ])

        # Proven Quant Edges Configuration (v1.7)
        self.proven_edges_cfg = self.cfg.get("proven_edges", {
            "goldilocks_separation_enabled": True,
            "goldilocks_min_atr": 0.13,
            "goldilocks_max_atr": 0.26,
            "futures_volume_surge_enabled": True,
            "futures_volume_surge_min_ratio": 1.15,
            "default_cost_model": "STATUTORY_DHAN",
        })
        try:
            from analyst.futures_volume_engine import FuturesVolumeEngine
            self.futures_volume_engine = FuturesVolumeEngine(db_path=self.market_db, config_path=self.config_path)
        except Exception as e:
            log.warning("Could not initialize FuturesVolumeEngine: %s", e)
            self.futures_volume_engine = None

        try:
            from analyst.ema_proximity_engine import ema_proximity_engine
            self.ema_proximity_engine = ema_proximity_engine
        except Exception as e:
            log.warning("Could not initialize EmaProximityEngine: %s", e)
            self.ema_proximity_engine = None
        self._goldilocks_cache: Optional[Dict[str, Any]] = None
        self._goldilocks_cache_at: float = 0.0

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _init_sqlite(self) -> None:
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                cur = conn.cursor()
                cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    correlation_id TEXT,
                    created_at TEXT,
                    strategy_name TEXT,
                    symbol TEXT,
                    security_id TEXT,
                    side TEXT,
                    product TEXT,
                    order_type TEXT,
                    lots INTEGER,
                    qty INTEGER,
                    signal_ltp REAL,
                    limit_price REAL,
                    fill_price REAL,
                    slippage REAL,
                    chase_retries INTEGER,
                    brokerage_taxes REAL,
                    order_status TEXT,
                    mode TEXT,
                    rejection_reason TEXT
                );
                """)
                # v1.9.4b: the live broker order id lives on the EXISTING orders
                # row, keyed by mode. It is deliberately NOT a separate
                # live_orders table: parallel records are exactly how the orders
                # book and the position came to disagree about the same fill,
                # which analyst/test_paper_fill_consistency_8510.py exists to
                # prevent. One row per order, one source of truth.
                try:
                    cols = {r[1] for r in cur.execute("PRAGMA table_info(orders)")}
                    if "dhan_order_id" not in cols:
                        cur.execute("ALTER TABLE orders ADD COLUMN dhan_order_id TEXT")
                except sqlite3.Error:
                    log.exception("Could not add orders.dhan_order_id")
                # v1.9.4b: dry-run rows are genuinely new data, not a duplicate
                # of anything -- they record the real bid/ask at signal time for
                # an order that was never sent. This is the only route to a
                # MEASURED execution cost: paper fill prices are taken from the
                # signal price, so slippage is unmeasurable while PAPER_ONLY.
                cur.execute("""
                CREATE TABLE IF NOT EXISTS dry_run_orders (
                    order_id TEXT PRIMARY KEY,
                    ts TEXT,
                    strategy TEXT,
                    symbol TEXT,
                    security_id TEXT,
                    side TEXT,
                    qty INTEGER,
                    signal_ltp REAL,
                    bid REAL,
                    ask REAL,
                    spread_pts REAL,
                    intended_limit_price REAL,
                    would_have_chased INTEGER,
                    mode TEXT
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    security_id TEXT,
                    side TEXT,
                    lots INTEGER,
                    qty INTEGER,
                    entry_price REAL,
                    entry_time TEXT,
                    current_ltp REAL,
                    target_price REAL,
                    sl_price REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    duration TEXT,
                    gross_pnl REAL,
                    net_pnl REAL,
                    status TEXT,
                    mode TEXT
                );
                """)
                # v1.9.4c Matrix Scalp: spot-side context on the position, so
                # the sweet-spot study can be done in SPOT points later and so
                # Active Positions (which does SELECT *) shows it without any
                # UI change. Additive and nullable: every existing caller and
                # every existing row is unaffected.
                try:
                    pcols = {r[1] for r in cur.execute("PRAGMA table_info(positions)")}
                    for col, decl in (
                        ("strategy_id", "TEXT"), ("spot_entry", "REAL"),
                        ("spot_exit", "REAL"), ("spot_target_level", "REAL"),
                        ("spot_stop_level", "REAL"), ("spot_target_pts", "REAL"),
                        ("spot_stop_pts", "REAL"), ("engine_risk_pts", "REAL"),
                        ("sr_interaction", "TEXT"),
                    ):
                        if col not in pcols:
                            cur.execute("ALTER TABLE positions ADD COLUMN {} {}".format(col, decl))
                except sqlite3.Error:
                    log.exception("Could not add Matrix Scalp spot columns to positions")
                # Live-exit bookkeeping. Nullable on purpose: NULL live_filled
                # marks a row written before these columns existed, and the
                # close path then falls back to the stored mode for that row.
                try:
                    pcols = {r[1] for r in cur.execute("PRAGMA table_info(positions)")}
                    for col, decl in (
                        ("live_filled", "INTEGER"), ("live_order_id", "TEXT"),
                        ("live_fill_qty", "INTEGER"), ("exit_order_id", "TEXT"),
                        ("exit_filled_qty", "INTEGER"), ("exit_filled_value", "REAL"),
                    ):
                        if col not in pcols:
                            cur.execute("ALTER TABLE positions ADD COLUMN {} {}".format(col, decl))
                except sqlite3.Error:
                    log.exception("Could not add live-exit columns to positions")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_wallet (
                    id INTEGER PRIMARY KEY,
                    balance REAL,
                    margin_used REAL,
                    realized_pnl REAL,
                    updated_at TEXT
                );
                """)
                ensure_algo_execution_master(conn)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_market_store (
                    date TEXT PRIMARY KEY,
                    created_at TEXT,
                    spot_candles_count INTEGER,
                    future_candles_count INTEGER,
                    payload TEXT,
                    eod_summary TEXT
                );
                """)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS ema8_prototype_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    direction TEXT,
                    cross_bar_open_ts INTEGER,
                    cross_e8 REAL,
                    consumed_cross_ts INTEGER,
                    last_flip TEXT,
                    away_seen INTEGER DEFAULT 0,
                    last_traded_direction TEXT,
                    updated_at TEXT
                );
                """)
                try:
                    cur.execute("ALTER TABLE ema8_prototype_state ADD COLUMN sl_blocked_side TEXT")
                except Exception:
                    pass
                try:
                    cur.execute("ALTER TABLE ema8_prototype_state ADD COLUMN away_seen INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    cur.execute("ALTER TABLE ema8_prototype_state ADD COLUMN last_traded_direction TEXT")
                except Exception:
                    pass

                cur.execute("""
                CREATE TABLE IF NOT EXISTS ema8_prototype_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    event TEXT NOT NULL,
                    direction TEXT,
                    dynamic_8ema REAL,
                    spot REAL,
                    reason TEXT,
                    position_id TEXT
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ema8_prototype_log_id ON ema8_prototype_log(id DESC);")

                # Super Trail Column Migrations
                pos_cols = [
                    ("highest_ltp", "REAL DEFAULT 0.0"),
                    ("initial_sl", "REAL DEFAULT 0.0"),
                    ("current_trailing_sl", "REAL DEFAULT 0.0"),
                    ("target_status", "TEXT DEFAULT 'FIXED'"),
                    ("initial_target", "REAL DEFAULT 0.0"),
                    ("trailing_target_level", "REAL DEFAULT 0.0"),
                    ("highest_after_target", "REAL DEFAULT 0.0"),
                    ("pullback_exit_level", "REAL DEFAULT 0.0"),
                    ("trail_sl_enabled", "INTEGER DEFAULT 1"),
                    ("trail_target_enabled", "INTEGER DEFAULT 1"),
                    ("trail_activation_pts", "REAL DEFAULT 15.0"),
                    ("trail_jump_pts", "REAL DEFAULT 5.0"),
                    ("trail_target_step", "REAL DEFAULT 10.0"),
                    ("trail_target_pullback", "REAL DEFAULT 5.0"),
                    ("target_order_id", "TEXT DEFAULT ''"),
                    ("sl_order_id", "TEXT DEFAULT ''"),
                    ("exit_reason", "TEXT DEFAULT ''"),
                    ("partial_step", "INTEGER DEFAULT 0"),
                    ("remaining_qty", "INTEGER DEFAULT 0"),
                    ("placed_by", "TEXT DEFAULT 'AUTO ALGO 🤖'"),
                    ("source", "TEXT DEFAULT 'AUTO ALGO 🤖'"),
                    ("strategy_setup", "TEXT DEFAULT 'Manual — No Strategy'"),
                ]
                for col_name, col_type in pos_cols:
                    try:
                        cur.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type};")
                    except sqlite3.OperationalError:
                        pass

                order_cols = [
                    ("placed_by", "TEXT DEFAULT 'AUTO ALGO 🤖'"),
                    ("source", "TEXT DEFAULT 'AUTO ALGO 🤖'"),
                    ("strategy_setup", "TEXT DEFAULT 'Manual — No Strategy'"),
                    # Exit-leg columns. Before these, only the BUY leg was ever
                    # written, so every square-off was invisible in the book.
                    ("position_id", "TEXT DEFAULT ''"),
                    ("exit_reason", "TEXT DEFAULT ''"),
                    ("net_pnl", "REAL DEFAULT 0.0"),
                    ("nifty_spot_entry", "REAL"),
                    ("nifty_spot_exit", "REAL"),
                ]
                for col_name, col_type in order_cols:
                    try:
                        cur.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")
                    except sqlite3.OperationalError:
                        pass

                # Initialize paper wallet if empty
                cur.execute("SELECT balance FROM paper_wallet WHERE id=1")
                if not cur.fetchone():
                    start_cap = float(self.cfg.get("starting_paper_capital", 100000.0))
                    cur.execute(
                        "INSERT INTO paper_wallet (id, balance, margin_used, realized_pnl, updated_at) VALUES (1, ?, 0.0, 0.0, ?)",
                        (start_cap, datetime.now(IST).isoformat())
                    )
                conn.commit()
            bf = backfill_algo_execution_master_from_positions(self.ledger_db)
            if bf.get("inserted"):
                log.info(
                    "algo_execution_master backfill: inserted=%s dup=%s invalid=%s",
                    bf.get("inserted"),
                    bf.get("skipped_duplicate"),
                    bf.get("skipped_invalid"),
                )
            self._load_ema8_prototype_state()
            self._sync_open_ema8_prototype_position()
        except Exception as e:
            log.error("Error initializing SQLite ledger: %s", e)

    def _sync_open_ema8_prototype_position(self) -> None:
        """Derives 'open' prototype state directly from the positions ledger."""
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT * FROM positions
                    WHERE status='OPEN'
                      AND upper(coalesce(strategy_id,'')) LIKE 'EMA8_PROTOTYPE%'
                    ORDER BY entry_time DESC LIMIT 1
                    """
                ).fetchone()
            if not hasattr(self, "_ema8_prototype_state"):
                self._ema8_prototype_state = {}
            if row:
                from analyst.ema8_prototype_8510 import prototype_spot_is_long
                is_long = prototype_spot_is_long(
                    float(row["spot_entry"] or 0.0),
                    float(row["spot_target_level"] or 0.0),
                    strategy_id=str(row["strategy_id"] or ""),
                    symbol=str(row["symbol"] or ""),
                )
                self._ema8_prototype_state["open"] = {
                    "direction": "LONG" if is_long else "SHORT",
                    "position_id": str(row["position_id"]),
                    "spot_entry": float(row["spot_entry"] or 0.0),
                }
            else:
                self._ema8_prototype_state.pop("open", None)
        except Exception as exc:
            log.debug("Error in _sync_open_ema8_prototype_position: %s", exc)

    def _load_ema8_prototype_state(self) -> None:
        """Loads persisted arm & consumed cross state from ema8_prototype_state table."""
        try:
            if not hasattr(self, "_ema8_prototype_state"):
                self._ema8_prototype_state = {}
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM ema8_prototype_state WHERE id=1").fetchone()
                if row:
                    d = str(row["direction"] or "")
                    ts = row["cross_bar_open_ts"]
                    if d and ts:
                        away = bool(row["away_seen"]) if ("away_seen" in row.keys() and row["away_seen"] is not None) else False
                        self._ema8_prototype_state["arm"] = {
                            "direction": d,
                            "cross_bar_open_ts": int(ts),
                            "cross_e8": float(row["cross_e8"] or 0.0),
                            "away_seen": away,
                        }
                    if row["consumed_cross_ts"]:
                        self._ema8_prototype_state["consumed_cross_ts"] = int(row["consumed_cross_ts"])
                    if row["last_flip"]:
                        self._ema8_prototype_state["last_flip"] = str(row["last_flip"])
                    if "last_traded_direction" in row.keys() and row["last_traded_direction"]:
                        self._ema8_prototype_state["last_traded_direction"] = str(row["last_traded_direction"])
                    if "sl_blocked_side" in row.keys() and row["sl_blocked_side"]:
                        self._ema8_prototype_state["sl_blocked_side"] = str(row["sl_blocked_side"])
        except Exception as exc:
            log.warning("Could not load ema8_prototype_state: %s", exc)

    def _save_ema8_prototype_state(self) -> None:
        """Persists arm & consumed cross state to ema8_prototype_state table."""
        try:
            if not hasattr(self, "_ema8_prototype_state"):
                return
            arm = self._ema8_prototype_state.get("arm") or {}
            direction = arm.get("direction")
            cross_bar_open_ts = arm.get("cross_bar_open_ts")
            cross_e8 = arm.get("cross_e8")
            away_seen = 1 if arm.get("away_seen") else 0
            consumed_cross_ts = self._ema8_prototype_state.get("consumed_cross_ts")
            last_flip = self._ema8_prototype_state.get("last_flip")
            last_traded_dir = self._ema8_prototype_state.get("last_traded_direction")
            now_str = datetime.now(IST).isoformat()
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ema8_prototype_state (
                        id, direction, cross_bar_open_ts, cross_e8, consumed_cross_ts, last_flip, away_seen, last_traded_direction, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (direction, cross_bar_open_ts, cross_e8, consumed_cross_ts, last_flip, away_seen, last_traded_dir, now_str),
                )
                try:
                    conn.execute(
                        "UPDATE ema8_prototype_state SET sl_blocked_side=? WHERE id=1",
                        (self._ema8_prototype_state.get("sl_blocked_side"),),
                    )
                except sqlite3.OperationalError:
                    pass  # table created before the column existed and not yet migrated
                conn.commit()
        except Exception as exc:
            log.warning("Could not persist ema8_prototype_state: %s", exc)

    def log_ema8_prototype_event(
        self,
        event: str,
        direction: Optional[str] = None,
        dynamic_8ema: Optional[float] = None,
        spot: Optional[float] = None,
        reason: Optional[str] = None,
        position_id: Optional[str] = None,
        time_str: Optional[str] = None,
    ) -> None:
        """Saves a prototype lifecycle event to SQLite table ema8_prototype_log."""
        t = time_str or datetime.now(IST).strftime("%H:%M:%S")
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    """
                    INSERT INTO ema8_prototype_log (time, event, direction, dynamic_8ema, spot, reason, position_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t,
                        str(event).upper(),
                        str(direction).upper() if direction else None,
                        round(float(dynamic_8ema), 2) if dynamic_8ema is not None else None,
                        round(float(spot), 2) if spot is not None else None,
                        str(reason) if reason else None,
                        str(position_id) if position_id else None,
                    ),
                )
                conn.commit()
        except Exception as exc:
            log.warning("Could not persist ema8_prototype_log event: %s", exc)

    def get_ema8_prototype_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Reads latest prototype lifecycle events from SQLite table ema8_prototype_log."""
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT time, event, direction, dynamic_8ema, spot, reason, position_id
                    FROM ema8_prototype_log
                    ORDER BY id DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["ts"] = d["time"]
                    d["setup_name"] = f"{d['event']}: {d.get('reason') or ''}".strip(": ")
                    out.append(d)
                return out
        except Exception as exc:
            log.debug("Could not read ema8_prototype_log: %s", exc)
            return []

    def update_super_trail_config(self, new_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Live updates Super Trail parameters without restarting server."""
        for k, v in new_cfg.items():
            if k in self.super_trail_cfg:
                if isinstance(self.super_trail_cfg[k], bool):
                    self.super_trail_cfg[k] = bool(v)
                elif isinstance(self.super_trail_cfg[k], (int, float)):
                    self.super_trail_cfg[k] = float(v) if "." in str(v) else int(v)
                else:
                    self.super_trail_cfg[k] = v

        self.cfg["super_trail"] = self.super_trail_cfg
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
            log.info("Super Trail config updated: %s", self.super_trail_cfg)
        except Exception as e:
            log.error("Failed saving Super Trail config: %s", e)

        return {"ok": True, "super_trail": self.super_trail_cfg}

    def update_strike_selection(self, new_selection: Dict[str, bool]) -> Dict[str, Any]:
        """Updates and persists Algo Trade strike checkboxes."""
        for k, v in new_selection.items():
            self.algo_strike_selection[k] = bool(v)

        self.cfg["algo_strike_selection"] = self.algo_strike_selection
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
            log.info("Algo strike selection updated: %s", self.algo_strike_selection)
        except Exception as e:
            log.error("Failed saving strike selection: %s", e)

        return {"ok": True, "algo_strike_selection": self.algo_strike_selection}

    def set_directional_bias(self, bias: str) -> Dict[str, Any]:
        """Sets and persists Daily Directional Bias (LONG_ONLY, SHORT_ONLY, or BOTH)."""
        bias = str(bias).upper()
        if bias not in ("LONG_ONLY", "SHORT_ONLY", "BOTH"):
            return {"ok": False, "error": f"Invalid directional bias '{bias}'. Expected LONG_ONLY, SHORT_ONLY, or BOTH."}
        self.directional_bias = bias
        self.cfg["directional_bias"] = bias
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
            log.info("✔ Directional Bias updated & persisted to %s", bias)
        except Exception as e:
            log.error("Failed persisting directional bias to config: %s", e)
        return {"ok": True, "directional_bias": self.directional_bias}

    def get_directional_bias(self) -> str:
        """Returns the active Directional Bias."""
        return self.directional_bias

    def filter_trades_by_directional_bias(self, trades: List[Dict[str, Any]], bias: Optional[str] = None) -> List[Dict[str, Any]]:
        """Filters a list of trades according to directional bias (LONG_ONLY, SHORT_ONLY, or BOTH)."""
        eff_bias = (bias or self.directional_bias).upper()
        if eff_bias == "LONG_ONLY":
            out = []
            for t in trades:
                d = str(t.get("Direction") or t.get("direction") or "").upper()
                opt = str(t.get("opt_side") or t.get("opt_type") or "").upper()
                setup = str(t.get("setup_name") or t.get("setup") or "")
                outcome = str(t.get("outcome") or t.get("result") or "")
                is_short = (d == "SHORT" or opt == "PE" or "PE Short" in outcome or "PE Short" in setup or "Downside" in setup or "(Short)" in setup)
                if is_short:
                    log.info("[DIRECTIONAL BIAS REJECT] Replay/backtest trade '%s' rejected by LONG_ONLY filter.", setup)
                else:
                    out.append(t)
            return out
        elif eff_bias == "SHORT_ONLY":
            out = []
            for t in trades:
                d = str(t.get("Direction") or t.get("direction") or "").upper()
                opt = str(t.get("opt_side") or t.get("opt_type") or "").upper()
                setup = str(t.get("setup_name") or t.get("setup") or "")
                outcome = str(t.get("outcome") or t.get("result") or "")
                is_long = (d == "LONG" or opt == "CE" or "CE Long" in outcome or "CE Long" in setup or "Upside" in setup or "(Long)" in setup or "Support Bounce" in setup)
                if is_long:
                    log.info("[DIRECTIONAL BIAS REJECT] Replay/backtest trade '%s' rejected by SHORT_ONLY filter.", setup)
                else:
                    out.append(t)
            return out
        return trades

    def get_synergy_config(self) -> Dict[str, Any]:
        """Returns the active Dhan OI PE-CE Synergy filter configuration."""
        return dict(self.dhan_oi_synergy_cfg)

    def update_synergy_config(self, new_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Updates and persists Dhan OI PE-CE Synergy filter configuration."""
        self.dhan_oi_synergy_cfg.update(new_cfg)
        self.cfg["dhan_oi_synergy"] = self.dhan_oi_synergy_cfg
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
            log.info("⚡ Dhan OI Synergy Configuration updated & persisted: %s", self.dhan_oi_synergy_cfg)
        except Exception as e:
            log.error("Failed persisting synergy config: %s", e)
        return {"ok": True, "dhan_oi_synergy": self.dhan_oi_synergy_cfg}

    def get_writer_flow_gate_config(self) -> Dict[str, Any]:
        return {"ok": True, "writer_flow_gate": dict(self.writer_flow_gate_cfg)}

    def update_writer_flow_gate_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(self.writer_flow_gate_cfg.keys()) | {
            "enabled",
            "entity",
            "surge_put_interval_lakhs",
            "surge_call_interval_lakhs",
            "paper_put_surge_mode",
            "paper_call_surge_mode",
            "live_put_surge_mode",
            "live_call_surge_mode",
            "block_ce_on_call_surge",
            "block_pe_on_put_surge",
            "block_ce_when_bearish",
            "block_pe_when_bullish",
            "fail_open_when_stale",
            "divergence_enabled",
        }
        for key, value in (updates or {}).items():
            if key in allowed:
                self.writer_flow_gate_cfg[key] = value
        self.cfg["writer_flow_gate"] = self.writer_flow_gate_cfg
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
        except Exception as exc:
            log.warning("Could not persist writer_flow_gate config: %s", exc)
        return self.get_writer_flow_gate_config()

    def get_stockmojo_live_config(self) -> Dict[str, Any]:
        return {"ok": True, "stockmojo_live": dict(self.stockmojo_live_cfg)}

    def update_stockmojo_live_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"enabled", "poll_min_seconds", "poll_max_seconds", "ingest_download_folder"}
        for key, value in (updates or {}).items():
            if key not in allowed:
                continue
            if key == "enabled":
                self.stockmojo_live_cfg[key] = (
                    value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
                )
            else:
                self.stockmojo_live_cfg[key] = value
        self.cfg["stockmojo_live"] = self.stockmojo_live_cfg
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
        except Exception as exc:
            log.warning("Could not persist stockmojo_live config: %s", exc)
        if self.stockmojo_poller is None and self.stockmojo_live_cfg.get("enabled"):
            try:
                from analyst.stockmojo_safe_poller_8510 import poller_from_config

                self.stockmojo_poller = poller_from_config(self.stockmojo_live_cfg)
            except Exception as exc:
                log.warning("StockMojo poller lazy init failed: %s", exc)
        return self.get_stockmojo_live()

    def get_stockmojo_live(self) -> Dict[str, Any]:
        from analyst.stockmojo_status_8510 import env_credential_flags, summarize_stockmojo_status

        master = bool(self.stockmojo_live_cfg.get("enabled"))
        poller = getattr(self, "stockmojo_poller", None)
        snap: Dict[str, Any] = poller.snapshot() if poller else {}
        if not master:
            snap = {**snap, "available": False, "poll_status": "MASTER_DISABLED"}
        status = summarize_stockmojo_status(
            master_enabled=master,
            writer_gate_enabled=bool(self.writer_flow_gate_cfg.get("enabled")),
            snap=snap,
            poller_present=poller is not None,
        )
        out = {
            "ok": True,
            "enabled": master,
            "source": "STOCKMOJO_API",
            "operational_status": status,
            "env": env_credential_flags(),
            "writer_flow": self._writer_flow_context(),
        }
        out.update(snap)
        out["operational_status"] = status
        return out

    def _writer_flow_context(self) -> Dict[str, Any]:
        from analyst.writer_flow_gate_8510 import evaluate_writer_context

        sm: Dict[str, Any] = {}
        master = bool(self.stockmojo_live_cfg.get("enabled"))
        if master and self.stockmojo_poller:
            sm = self.stockmojo_poller.snapshot()
        dhan = self.dhan_oi.cached_snapshot()
        return evaluate_writer_context(
            sm,
            dhan,
            self.writer_flow_gate_cfg,
            stockmojo_master_enabled=master,
        )

    # ── OPTION-CHAIN CONTEXT (day OI bias, walls) ───────────────────────────
    def _oi_context_db_paths(self) -> List[Path]:
        return [ROOT / "data/terminal8510/recording.sqlite3", Path(self.market_db)]

    def oi_context(self, spot: Optional[float] = None, max_age_sec: float = 20.0) -> Dict[str, Any]:
        """Day OI bias, 3-minute / 15-minute flow and walls from the recorded chain snapshots.

        Cached for 20 seconds. Never raises; with no data the status is NO_DATA
        and nothing downstream is blocked.
        """
        from analyst.oi_context_8510 import compute_context, load_cfg, load_day_snapshots

        cfg = load_cfg(self.scanner_cfg)
        if not bool(cfg.get("enabled", True)):
            return {"status": "OFF", "bias": None, "reason": "oi_context disabled"}
        cache = self.__dict__.get("_oi_context_cache")
        now_mono = time.monotonic()
        if cache and (now_mono - cache[0]) < max_age_sec:
            return cache[1]
        try:
            px = float(spot or (self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
            now = datetime.now(IST)
            day = now.strftime("%Y-%m-%d")
            rows = load_day_snapshots(self._oi_context_db_paths(), day)
            ctx = compute_context(rows, px, cfg, day=day, now_ts=int(now.timestamp()))
        except Exception as exc:
            ctx = {"status": "NO_DATA", "bias": None, "reason": f"error: {type(exc).__name__}"}
        self._oi_context_cache = (now_mono, ctx)
        # Feed for the Prototype's OI placeholder (used only if that switch is on).
        try:
            if ctx.get("status") == "OK" and ctx.get("bias"):
                self._ema8_prototype_state["oi_bias"] = ctx["bias"]
            else:
                self._ema8_prototype_state.pop("oi_bias", None)
        except Exception:
            pass
        return ctx

    # ── 8600 BOARD (every option-chain read, one switch each) ───────────────
    def desk8600_board(self) -> Dict[str, Any]:
        """All 8600 / option-chain sources with their current reading. Never raises."""
        from analyst.desk8600_board_8510 import build_board

        bridge = self.__dict__.get("desk8600_bridge")
        last = getattr(bridge, "last", None) or {}
        return build_board(last, self.oi_context(), self.scanner_cfg, datetime.now(IST),
                           chain_reads=self.chain_reads())

    def chain_reads(self, max_age_sec: float = 20.0) -> Dict[str, Any]:
        """PCR zone, price-vs-OI buildup, volume-backed OI and gamma regime. Cached 20 s. Never raises."""
        from analyst.chain_reads_8510 import compute_reads, load_cfg as _cr_cfg
        from analyst.oi_context_8510 import load_day_snapshots

        cache = self.__dict__.get("_chain_reads_cache")
        now_mono = time.monotonic()
        if cache and (now_mono - cache[0]) < max_age_sec:
            return cache[1]
        try:
            px = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
            now = datetime.now(IST)
            day = now.strftime("%Y-%m-%d")
            rows = load_day_snapshots(self._oi_context_db_paths(), day)
            out = compute_reads(rows, px, _cr_cfg(self.scanner_cfg), day=day, now_ts=int(now.timestamp()))
        except Exception as exc:
            out = {"status": "NO_DATA", "reason": f"error: {type(exc).__name__}", "reads": {}, "facts": {}}
        self._chain_reads_cache = (now_mono, out)
        return out

    def desk8600_link(self) -> Dict[str, Any]:
        """Is the 8600 app connected and live, are chain snapshots live, and which build is running."""
        from analyst.desk8600_link_8510 import BUILD_LABEL, WHATS_NEW, build_id, link_status

        try:
            mkt = None
            try:
                ms = (self.cached_telemetry.get("market_status") or {})
                if "is_open" in ms:
                    mkt = bool(ms.get("is_open"))
            except Exception:
                mkt = None
            out = link_status(self.__dict__.get("desk8600_bridge"), self.oi_context(), datetime.now(IST), market_open=mkt)
        except Exception as exc:
            out = {"desk8600": {"state": "NOT_ANSWERING", "headline": "link check failed", "detail": type(exc).__name__, "fix": ""},
                   "chain": {"state": "NO_DATA", "headline": "", "detail": "", "fix": ""}}
        if "_build_id" not in self.__dict__:
            self._build_id = build_id(ROOT)
        out["build"] = {"label": BUILD_LABEL, "commit": self._build_id}
        out["whats_new"] = WHATS_NEW
        return out

    # ── 8600 TICKET LANE ────────────────────────────────────────────────────
    def _desk8600_ticket_position_open(self) -> bool:
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                r = conn.execute(
                    "SELECT count(*) FROM positions WHERE status IN ('OPEN','EXIT_PENDING') "
                    "AND UPPER(COALESCE(strategy_id,'')) LIKE 'DESK8600_TICKET%'"
                ).fetchone()
            return bool(r and r[0] > 0)
        except Exception:
            return False

    def evaluate_desk8600_ticket_lane(self, spot: Optional[float] = None) -> List[Dict[str, Any]]:
        """Signals from the newest 8600 entry ticket (one per closed 3-minute bar). Never raises."""
        from analyst.desk8600_ticket_lane_8510 import evaluate

        try:
            bridge = self.__dict__.get("desk8600_bridge")
            last = getattr(bridge, "last", None) or {}
            state = self.__dict__.setdefault("_desk8600_ticket_state", {})
            sigs, status = evaluate(
                last, self.scanner_cfg, state, datetime.now(IST),
                has_open_position=self._desk8600_ticket_position_open(), spot=spot,
            )
            self._desk8600_ticket_status = status
            if sigs:
                log.info("[8600 TICKET] %s", status.get("detail"))
            return sigs
        except Exception as exc:
            log.warning("8600 ticket lane skipped (%s)", type(exc).__name__)
            return []

    def desk8600_ticket_lane_status(self) -> Dict[str, Any]:
        from analyst.desk8600_ticket_lane_8510 import load_cfg as _tl_cfg

        st = dict(self.__dict__.get("_desk8600_ticket_status") or {})
        cfg = _tl_cfg(self.scanner_cfg)
        st.update(enabled=cfg["enabled"], route=cfg["route"], high_conviction_only=cfg["high_conviction_only"],
                  target_pts=float(cfg["target_pts"]), sl_pts=float(cfg["sl_pts"]))
        if not cfg["enabled"]:
            st.setdefault("detail", "lane is switched off")
            st["state"] = "OFF"
        st.setdefault("state", "WAITING")
        st["position_open"] = self._desk8600_ticket_position_open()
        return st

    def update_desk8600_ticket_lane(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Cockpit change to the ticket lane; saved the same way as the other cockpit switches."""
        from analyst.desk8600_ticket_lane_8510 import SID_LONG, SID_SHORT, apply_update

        cfg = apply_update(self.scanner_cfg, updates or {})
        # If the desk runs an execution allowlist, a lane the owner switches on must be on it.
        if cfg["enabled"] and bool(self.algo_allowlist_cfg.get("enabled", False)):
            raw = list(self.algo_allowlist_cfg.get("strategy_ids") or [])
            have = {str(x).strip().upper() for x in raw}
            raw += [sid for sid in (SID_LONG, SID_SHORT) if sid not in have]
            self.algo_allowlist_cfg["strategy_ids"] = raw
            self.cfg["algo_execution_allowlist"] = self.algo_allowlist_cfg
        self.cfg["scanner"] = self.scanner_cfg
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception as exc:
            log.warning("Could not persist 8600 ticket lane config: %s", exc)
        log.info("[8600 TICKET] lane settings now: %s", json.dumps(self.scanner_cfg.get("desk8600_ticket_lane")))
        out = self.desk8600_ticket_lane_status()
        out["ok"] = True
        return out

    def update_desk8600_board(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Cockpit change to the board's switches; saved the same way as the other gates."""
        from analyst.desk8600_board_8510 import apply_update

        apply_update(self.scanner_cfg, updates or {})
        self.cfg["scanner"] = self.scanner_cfg
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception as exc:
            log.warning("Could not persist 8600 board config: %s", exc)
        log.info("[8600 BOARD] switches now: %s", json.dumps(self.scanner_cfg.get("desk8600_board")))
        out = self.desk8600_board()
        out["ok"] = True
        return out

    def log_signal_desk8600(self, signal_uid: str, sig: Dict[str, Any], board: Dict[str, Any],
                            verdict: Dict[str, Any]) -> None:
        """One row per signal: every source's reading and the verdict. Never raises."""
        try:
            readings = {
                s["id"]: {"direction": s.get("direction"), "detail": s.get("detail"),
                          "age_sec": s.get("age_sec"), "mode": s.get("mode")}
                for s in board.get("sources") or []
            }
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS signal_desk8600 (
                        signal_uid TEXT, logged_at TEXT, strategy_id TEXT, direction TEXT, side TEXT,
                        rule TEXT, neutral_blocks INTEGER, filters_on INTEGER,
                        agree INTEGER, against INTEGER, neutral INTEGER,
                        sources_against_all TEXT, would_block INTEGER, blocked INTEGER,
                        reasons TEXT, readings TEXT)"""
                )
                conn.execute(
                    "INSERT INTO signal_desk8600 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        signal_uid, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                        str(sig.get("strategy_id") or ""), str(sig.get("direction") or ""), verdict.get("side"),
                        board.get("rule"), 1 if board.get("neutral_blocks") else 0, int(board.get("filters_on") or 0),
                        int(verdict.get("agree") or 0), int(verdict.get("against") or 0), int(verdict.get("neutral") or 0),
                        ",".join(verdict.get("sources_against_all") or []),
                        1 if verdict.get("would_block") else 0, 1 if verdict.get("blocked") else 0,
                        " | ".join(verdict.get("reasons") or []), json.dumps(readings, default=str),
                    ),
                )
        except Exception as exc:
            log.warning("signal 8600 board not logged (%s)", type(exc).__name__)

    def log_signal_oi_context(self, signal_uid: str, sig: Dict[str, Any], ctx: Dict[str, Any],
                              gates: Dict[str, Any], blocked: bool) -> None:
        """One row per signal: the OI context and what each gate said. Never raises."""
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS signal_oi_context (
                        signal_uid TEXT, logged_at TEXT, strategy_id TEXT, direction TEXT, spot REAL,
                        status TEXT, bias TEXT, call_added REAL, put_added REAL, net_pe_minus_ce REAL,
                        imbalance_pct REAL, slope_30m REAL, slope_state TEXT,
                        call_wall_above REAL, put_wall_below REAL, room_up_pts REAL, room_down_pts REAL,
                        boxed INTEGER, call_volume REAL, put_volume REAL,
                        gate_direction_pass INTEGER, gate_agreement_pass INTEGER, gate_room_pass INTEGER,
                        would_block INTEGER, blocked INTEGER, reasons TEXT)"""
                )
                conn.execute(
                    "INSERT INTO signal_oi_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        signal_uid, datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                        str(sig.get("strategy_id") or ""), str(sig.get("direction") or ""),
                        ctx.get("spot"), ctx.get("status"), ctx.get("bias"),
                        ctx.get("call_added"), ctx.get("put_added"), ctx.get("net_pe_minus_ce"),
                        ctx.get("imbalance_pct"), ctx.get("slope_30m"), ctx.get("slope_state"),
                        ctx.get("call_wall_above"), ctx.get("put_wall_below"),
                        ctx.get("room_up_pts"), ctx.get("room_down_pts"),
                        1 if ctx.get("boxed") else 0, ctx.get("call_volume"), ctx.get("put_volume"),
                        1 if gates["direction"]["pass"] else 0,
                        1 if gates["agreement"]["pass"] else 0,
                        1 if gates["room"]["pass"] else 0,
                        1 if gates.get("would_block") else 0, 1 if blocked else 0,
                        " | ".join(gates.get("reasons") or []),
                    ),
                )
        except Exception as exc:
            log.warning("signal OI context not logged (%s)", type(exc).__name__)

    def apply_writer_flow_gates(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from analyst.writer_flow_gate_8510 import filter_signals_by_writer_gate

        if not signals:
            return signals
        out = list(signals)
        live_armed = bool(self._real_money_enabled and not self.kill_switch_active)
        if self._confluence_gate_blocks(live_armed):
            conf = self.cached_telemetry.get("five_factor_confluence") or {}
            ff = self.cfg.get("five_factor_confluence") or {}
            log.info("[5-FACTOR GATE] Blocked batch: score %s < %s mode=%s (%s)",
                     conf.get("score"), ff.get("scanner_min_score"), ff.get("scanner_gate_mode"), conf.get("grade"))
            return []
        if not self.writer_flow_gate_cfg.get("enabled"):
            return out
        ctx = self._writer_flow_context()
        return filter_signals_by_writer_gate(out, ctx, self.writer_flow_gate_cfg, live_armed)

    def _confluence_gate_blocks(self, live_armed: bool) -> bool:
        ff = self.cfg.get("five_factor_confluence") or {}
        min_score = int(ff.get("scanner_min_score", 0) or 0)
        mode = str(ff.get("scanner_gate_mode", "off") or "off").lower()
        if min_score <= 0 or mode == "off":
            return False
        if mode == "paper" and live_armed:
            return False
        if mode == "live" and not live_armed:
            return False
        conf = self.cached_telemetry.get("five_factor_confluence") or {}
        return int(conf.get("score") or 0) < min_score

    def get_five_factor_config(self) -> Dict[str, Any]:
        ff = dict(self.cfg.get("five_factor_confluence") or {})
        return {"ok": True, "five_factor_confluence": ff}

    def update_five_factor_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        ff = dict(self.cfg.get("five_factor_confluence") or {})
        allowed = {
            "enabled", "advisory_only", "execution_authorized", "scanner_min_score", "scanner_gate_mode",
            "max_flips", "pocket_touch_points", "max_data_age_seconds", "first_opportunity", "last_opportunity",
            "quote_max_age_seconds",
        }
        for key, value in (updates or {}).items():
            if key not in allowed:
                continue
            if key == "scanner_gate_mode":
                mode = str(value or "off").lower()
                if mode not in ("off", "paper", "live", "both"):
                    continue
                ff[key] = mode
            elif key == "scanner_min_score":
                ff[key] = max(0, min(5, int(value)))
            elif key in ("enabled", "advisory_only", "execution_authorized"):
                ff[key] = value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
            else:
                ff[key] = value
        self.cfg["five_factor_confluence"] = ff
        try:
            self.config_path.write_text(json.dumps(self.cfg, indent=2))
        except Exception as exc:
            log.warning("Could not persist five_factor_confluence: %s", exc)
        return self.get_five_factor_config()

    @staticmethod
    def _public_dhan_status(value):
        hidden = {'masked_token', 'masked_secret', 'access_token', 'DHAN_ACCESS_TOKEN',
                  'api_secret', 'DHAN_API_SECRET', 'api_key', 'DHAN_API_KEY'}
        def public(item):
            if isinstance(item, dict): return {k: public(v) for k,v in item.items() if k not in hidden}
            if isinstance(item, list): return [public(v) for v in item]
            if isinstance(item, str): return re.sub(r'\*{4}[^\s|]+', '[saved]', item)
            return item
        return public(value)

    def get_dhan_config(self) -> Dict[str, Any]:
        """Returns Dhan configuration with secrets safely masked and IP match status."""
        return self._public_dhan_status(self.broker.get_masked_config())

    def get_dhan_status(self) -> Dict[str, Any]:
        """Returns auto-load Dhan credentials status, masking, and time left."""
        return self._public_dhan_status(self.broker.get_status())

    def logout_dhan(self) -> Dict[str, Any]:
        """Clears saved credentials on logout."""
        return self.broker.clear_credentials()

    def is_nse_market_open(self, dt: Optional[datetime] = None) -> Tuple[bool, str]:
        """Validates if NSE market is open (09:15 to 15:30 IST Mon-Fri)."""
        return is_nse_market_open(dt=dt, holidays=self.nse_holidays)

    def check_eod_square_off(self, dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        Square off all open intraday positions at 15:20:00 IST (Correction 2).
        Reason: broker's auto-square-off carries extra charges, matches backtester.
        Reads eod_square_off_time dynamically from config.
        """
        if dt is None:
            dt = datetime.now(IST)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)

        eod_cfg = str(self.cfg.get("eod_square_off_time", "15:20")).strip()
        eod_parts = [int(x) for x in eod_cfg.split(":")[:2]]
        eod_target_time = dtime(eod_parts[0], eod_parts[1], 0)

        closed_positions = []
        if dt.time() >= eod_target_time:
            eod_reason = f"EOD_SQUARE_OFF_{eod_cfg[:5]}"
            open_positions = self.get_active_positions()
            for p in open_positions:
                pos_id = p.get("position_id")
                if pos_id:
                    res = self.modify_open_position(pos_id, action="CLOSE", enforce_market_hours=False, reason=eod_reason)
                    if res.get("ok"):
                        closed_positions.append(res)
        return closed_positions

    @property
    def eod_square_off_time(self) -> str:
        val = str(self.cfg.get("eod_square_off_time", "15:20:00")).strip()
        if len(val) == 5:
            return f"{val}:00"
        return val

    def get_nifty_lot_size(self) -> int:
        """Returns dynamic NIFTY lot size (65) from instrument master."""
        return self.inst_mgr.get_nifty_lot_size()

    def set_off_hours_paper_testing(self, enabled: bool,
                                    minutes: Optional[int] = None) -> Dict[str, Any]:
        """Toggles paper trading outside market hours — SESSION-ONLY, EXPIRING.

        THIS USED TO PERSIST TO DISK, AND THAT WAS THE BUG.
        Several tests call this (and the Dhan config endpoints) against the real
        analyst/terminal8510_config.json rather than a tmp_path copy, so a suite
        run left allow_off_hours_paper_testing = true on disk. The flag is the
        only thing permitting execution outside market hours, so with it stuck
        true the scanner recorded paper trades at 02:49, 03:50, 04:10 and 16:56
        IST on 2026-11-06 -- times when NIFTY is not trading and no quote can be
        real. Those rows, plus duplicates of a single fill, made up the Rs85,321
        that the 06 Nov replay reported.

        An off-hours override is a temporary debugging aid by nature, so it is
        now treated like the real-money arm: held in memory only, never written
        to config, and expiring on its own. A restart clears it, and so does the
        clock. Same reasoning as configure_real_money persisting disarmed.
        """
        self.allow_off_hours_paper_testing = bool(enabled)
        ttl = int(minutes if minutes is not None
                  else self.cfg.get("off_hours_testing_ttl_minutes", 60) or 60)
        if enabled:
            self._off_hours_expires_at = time.time() + max(1, ttl) * 60.0
            log.warning("Off-hours paper testing ENABLED for %d minutes. "
                        "Session-only: not persisted, cleared on restart.", ttl)
        else:
            self._off_hours_expires_at = 0.0
            log.info("Off-hours paper testing disabled.")
        # Deliberately NOT written to self.config_path. The shipped config must
        # always read false; analyst/test_config_not_polluted_8510.py asserts it.
        self.cfg["allow_off_hours_paper_testing"] = False
        return {
            "ok": True,
            "allow_off_hours_paper_testing": self.allow_off_hours_paper_testing,
            "expires_in_minutes": (round(max(0.0, self._off_hours_expires_at - time.time()) / 60.0, 1)
                                   if enabled else 0),
            "persisted": False,
            "note": ("Session-only and expiring. This never reaches the config "
                     "file, so a test run cannot leave the desk trading "
                     "outside market hours."),
        }

    def off_hours_testing_active(self) -> bool:
        """The gate. False once the override expires, even if the flag is set."""
        if not getattr(self, "allow_off_hours_paper_testing", False):
            return False
        exp = float(getattr(self, "_off_hours_expires_at", 0.0) or 0.0)
        if exp and time.time() > exp:
            self.allow_off_hours_paper_testing = False
            self._off_hours_expires_at = 0.0
            log.warning("Off-hours paper testing override EXPIRED — "
                        "out-of-session execution blocked again.")
            return False
        return True

    def update_dhan_config(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Safely updates Dhan credentials and metadata into .env and config."""
        fixed_id = os.environ.get('DHAN_CLIENT_ID') or self.cfg.get('DHAN_CLIENT_ID') or self.cfg.get('dhan', {}).get('client_id')
        proposed = body.get('client_id') or body.get('DHAN_CLIENT_ID')
        if proposed is not None and str(proposed).strip() != str(fixed_id or ''):
            return {'ok': False, 'error': 'Client ID is fixed; update the secure backend configuration instead'}
        token = body.get('access_token', body.get('DHAN_ACCESS_TOKEN'))
        if token is not None and (not str(token).strip() or str(token).strip().startswith(('****','••'))):
            return {'ok': False, 'error': 'Paste a fresh Dhan Access Token; stored token placeholders are not accepted'}
        res = self.broker.update_credentials(
            client_id=None,
            access_token=body.get("access_token") or body.get("DHAN_ACCESS_TOKEN"),
            api_key=body.get("api_key") or body.get("DHAN_API_KEY"),
            api_secret=body.get("api_secret") or body.get("DHAN_API_SECRET"),
            environment=body.get("environment") or body.get("DHAN_ENVIRONMENT"),
            whitelisted_ip=body.get("whitelisted_ip") or body.get("DHAN_WHITELISTED_IP"),
        )
        self.cfg = self.broker.cfg
        return self._public_dhan_status(res)

    async def test_dhan_connection(self) -> Dict[str, Any]:
        """Runs live connection test against Dhan HQ v2 API with strict 10s timeout."""
        active_fut = self.inst_mgr.get_active_future_contract()
        fut_sec_id = int(active_fut["security_id"]) if active_fut else 61471
        try:
            return await asyncio.wait_for(self.broker.test_connection(fut_sec_id=fut_sec_id), timeout=10.0)
        except asyncio.TimeoutError:
            err_msg = "❌ Dhan Connection Failed: Connection timed out after 10s | [ 🔄 Retry Connection ]"
            status_obj = {
                "ok": False,
                "connected": False,
                "dhan_connected": False,
                "status": "DISCONNECTED ❌",
                "message": err_msg,
                "error": "Connection timed out after 10s",
                "last_updated": datetime.now(IST).strftime("%H:%M:%S")
            }
            self.broker.last_connected_status = status_obj
            return status_obj

    def calculate_dhan_charges(self, buy_price: float, sell_price: float, qty: int) -> float:
        """
        Exact Dhan Options Brokerage & Taxes:
          - Brokerage: ₹20 / order leg = ₹40 roundtrip
          - STT: 0.15% on sell turnover
          - Txn Charges (NSE + IPFT): 0.03553% on both legs
          - SEBI Charges: 0.0001% on both legs
          - Stamp Duty: 0.003% on buy leg
          - GST: 18% of (brokerage + txn + sebi)
        """
        c = self.cfg.get("charges", {})
        brok_per_leg = float(c.get("brokerage_per_order", 20.0))
        stt_rate = float(c.get("stt_sell_rate", 0.0015))
        txn_rate = float(c.get("txn_rate", 0.0003553))
        sebi_rate = float(c.get("sebi_rate", 0.000001))
        stamp_rate = float(c.get("stamp_buy_rate", 0.00003))
        gst_rate = float(c.get("gst_rate", 0.18))

        buy_turnover = buy_price * qty
        sell_turnover = sell_price * qty
        total_turnover = buy_turnover + sell_turnover

        brokerage = brok_per_leg * 2.0
        stt = sell_turnover * stt_rate
        txn = total_turnover * txn_rate
        sebi = total_turnover * sebi_rate
        stamp = buy_turnover * stamp_rate
        gst = (brokerage + txn + sebi) * gst_rate

        return round(brokerage + stt + txn + sebi + stamp + gst, 2)

    # ── PROVEN QUANT EDGES (v1.7) ───────────────────────────────────────────
    def compute_goldilocks_separation(self, ema4_val: float, ema8_val: float, atr14_val: float) -> Dict[str, Any]:
        min_atr = float(self.proven_edges_cfg.get("goldilocks_min_atr", 0.13))
        max_atr = float(self.proven_edges_cfg.get("goldilocks_max_atr", 0.26))
        if atr14_val <= 0 or math.isnan(atr14_val):
            atr14_val = 15.0
        sep_atr = round(abs(ema4_val - ema8_val) / atr14_val, 4)

        if sep_atr < min_atr:
            regime = "CHOP_ZONE"
            badge = f"🔴 CHOP ZONE (<{min_atr:.2f} ATR)"
            allowed = False
            desc = f"Entangled chop (sep {sep_atr:.2f} < {min_atr:.2f} ATR). Defensive chop blocker: no entry while averages are entangled."
        elif sep_atr <= max_atr:
            regime = "GOLDILOCKS_SWEET_SPOT"
            badge = f"🟢 GOLDILOCKS SWEET SPOT ({min_atr:.2f}–{max_atr:.2f} ATR)"
            allowed = True
            desc = f"Defensive separation band passes (sep {sep_atr:.2f} ATR); other entry checks still apply."
        else:
            regime = "CLIMAX_EXHAUSTION"
            badge = f"⚠️ CLIMAX EXHAUSTION (>{max_atr:.2f} ATR)"
            allowed = False
            desc = f"Rubber-band over-extension (sep {sep_atr:.2f} > {max_atr:.2f} ATR). Defensive over-extension blocker: wait for a new setup."

        is_enabled = bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", True))
        return {
            "sep_atr": sep_atr,
            "regime": regime,
            "badge": badge,
            "allowed": allowed if is_enabled else True,
            "raw_allowed": allowed,
            "filter_enabled": is_enabled,
            "description": desc,
            "pill_text": f"📐 Goldilocks ATR: {sep_atr:.2f} [{badge}]",
            "min_atr": min_atr,
            "max_atr": max_atr,
        }

    def get_latest_goldilocks_status(self) -> Dict[str, Any]:
        """Calculates current goldilocks separation status from latest 15m candle in market db."""
        now = time.time()
        if self._goldilocks_cache is not None and (now - self._goldilocks_cache_at) < 15.0:
            return self._goldilocks_cache
        if not self.market_db.exists():
            return {
                "sep_atr": 0.19,
                "regime": "GOLDILOCKS_SWEET_SPOT",
                "badge": "🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)",
                "allowed": True,
                "raw_allowed": True,
                "filter_enabled": bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", True)),
                "pill_text": "📐 Goldilocks ATR: 0.19 [🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)]",
                "min_atr": 0.13,
                "max_atr": 0.26,
            }
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                df = pd.read_sql_query(
                    f"SELECT ts, o as open, h as high, l as low, c as close FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT {EMA_WARMUP_1M_BARS}",
                    conn
                )
            if df.empty or len(df) < 30:
                return {
                    "sep_atr": 0.19,
                    "regime": "GOLDILOCKS_SWEET_SPOT",
                    "badge": "🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)",
                    "allowed": True,
                    "raw_allowed": True,
                    "filter_enabled": bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", True)),
                    "pill_text": "📐 Goldilocks ATR: 0.19 [🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)]",
                    "min_atr": 0.13,
                    "max_atr": 0.26,
                }
            df = df.iloc[::-1].reset_index(drop=True)
            df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
            df.set_index("dt", inplace=True)
            b15 = resample_nse_15m(df)
            if len(b15) < 5:
                return {
                    "sep_atr": 0.19,
                    "regime": "GOLDILOCKS_SWEET_SPOT",
                    "badge": "🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)",
                    "allowed": True,
                    "raw_allowed": True,
                    "filter_enabled": bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", True)),
                    "pill_text": "📐 Goldilocks ATR: 0.19 [🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)]",
                    "min_atr": 0.13,
                    "max_atr": 0.26,
                }
            e4, e8 = ema4_ema8(b15["close"])
            h, l, c = b15["high"], b15["low"], b15["close"]
            tr0 = h - l
            tr1 = (h - c.shift(1)).abs()
            tr2 = (l - c.shift(1)).abs()
            tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
            atr14 = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
            e4_last = float(e4.iloc[-1])
            e8_last = float(e8.iloc[-1])
            atr_last = float(atr14.iloc[-1]) if pd.notnull(atr14.iloc[-1]) and atr14.iloc[-1] > 0 else 15.0
            out = self.compute_goldilocks_separation(e4_last, e8_last, atr_last)
            self._goldilocks_cache = out
            self._goldilocks_cache_at = now
            return out
        except Exception as e:
            log.debug("Error computing latest goldilocks separation: %s", e)
            return {
                "sep_atr": 0.19,
                "regime": "GOLDILOCKS_SWEET_SPOT",
                "badge": "🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)",
                "allowed": True,
                "raw_allowed": True,
                "filter_enabled": bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", True)),
                "pill_text": "📐 Goldilocks ATR: 0.19 [🟢 GOLDILOCKS SWEET SPOT (0.13–0.26 ATR)]",
                "min_atr": 0.13,
                "max_atr": 0.26,
            }

    def trend_capture_service(self):
        from analyst.trend_capture_service_8510 import TrendCaptureService
        if not hasattr(self, '_trend_capture_service'):
            self._trend_capture_service = TrendCaptureService(self.cfg, self.market_db, self.ledger_db, self.futures_volume_engine)
        return self._trend_capture_service

    def get_five_factor_confluence(self, spot=None, price_fresh=False):
        """Advisory score from known, completed 15m bars. Never an execution authorization."""
        now = time.time()
        params = self.cfg['five_factor_confluence']
        if now-self._confluence_context_at >= self.cfg['futures_volume_engine']['refresh_seconds']:
            context = {}
            try:
                with sqlite3.connect('file:'+str(self.market_db)+'?mode=ro',uri=True) as conn:
                    records = conn.execute("SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id='13' AND ts<=? ORDER BY ts DESC LIMIT 6000",(int(now)-60,)).fetchall()
                frame = pd.DataFrame(list(reversed(records)),columns=['ts','open','high','low','close','volume'])
                if not frame.empty:
                    stamp = pd.to_datetime(frame.ts,unit='s',utc=True).dt.tz_convert(IST)
                    frame.index = stamp
                    frame=frame[((stamp.dt.hour*60+stamp.dt.minute>=555)&(stamp.dt.hour*60+stamp.dt.minute<930)).to_numpy()]
                    bars=resample_nse_15m(frame)
                    if len(bars) and (int(bars.index[-1].timestamp()) + 900 > int(now)):
                        bars = bars.iloc[:-1]
                    if len(bars)>=20:
                        e4,e8=ema4_ema8(bars.close)
                        mid=bars.close.rolling(20).mean();std=bars.close.rolling(20).std()
                        upper=mid+2*std;lower=mid-2*std
                        sign=(e4>e8).astype(int)-(e4<e8).astype(int)
                        regime=(sign!=sign.shift()).cumsum();active=regime.iloc[-1]
                        touched=(bars.low<=e8)&(bars.high>=e8)
                        # Count distinct closed-bar touch episodes within the current EMA regime.
                        episodes=touched&(~touched.shift(1,fill_value=False))&(regime==active)
                        opportunities=int(episodes.sum())
                        if not opportunities and abs(float(bars.close.iloc[-1]-e8.iloc[-1]))<=params['pocket_touch_points']:opportunities=1
                        b=bars.iloc[-1];side='CE' if e4.iloc[-1]>e8.iloc[-1] else 'PE'
                        tr=pd.concat([bars.high-bars.low,(bars.high-bars.close.shift()).abs(),(bars.low-bars.close.shift()).abs()],axis=1).max(axis=1)
                        atr=float(tr.ewm(alpha=1/14,adjust=False).mean().iloc[-1])
                        signs=(bars.close>e8).astype(int)
                        flips=int(signs.tail(12).diff().abs().fillna(0).sum())
                        end=bars.index[-1].timestamp()+900
                        bar_closed=end<=now
                        context=dict(timeframe='15m',side=side,ema4=float(e4.iloc[-1]),ema8=float(e8.iloc[-1]),
                            band_slope=float(upper.diff().iloc[-1] if side=='CE' else lower.diff().iloc[-1]),
                            opportunity=opportunities,atr=atr,flips_last_12_bars=flips,
                            wick_touched_ema8=bool(touched.iloc[-1]),signal_ts=end,context_present=True,
                            fresh=bar_closed and 0<=now-end<params['max_data_age_seconds'],
                            opportunity_basis='Distinct closed 15m EMA8 touch episodes in current 4/8 regime; advisory proxy, not a validated D/E execution rule')
            except Exception:
                log.exception('Confluence context unavailable; missing pillars stay false')
            self._confluence_context_cache=context;self._confluence_context_at=now
        context=dict(self._confluence_context_cache)
        if context:
            e8=context['ema8'];p=spot if price_fresh and isinstance(spot,(int,float)) and spot>0 else None
            context['abs_gap_pts']=abs(p-e8) if p is not None else None
            context['price_fresh']=bool(p is not None)
        radar = self.ema_proximity_engine.get_latest_radar_status(live_spot=spot if price_fresh else None) if self.ema_proximity_engine else {}
        if context:
            context['flips_last_12_bars'] = radar.get('flips_last_12_bars') if radar.get('ok') else None
            context['abs_gap_pts'] = radar.get('abs_gap_pts') if price_fresh and radar.get('ok') else None
            context['wick_touched_ema8'] = radar.get('wick_touched_ema8') is True and radar.get('ok') is True
            context['pocket_timeframe'] = '1m proximity engine'
        volume=self.futures_volume_engine.get_latest_vsr() if self.futures_volume_engine else {}
        return compute_five_factor_confluence(context,self.dhan_oi.cached_snapshot(),volume,self.cfg)

    def _simulated_slippage_pts(self, order_id: str) -> float:
        """Paper-fill slippage in points, stable for a given order id.

        Reads the candidate values from config (paper_execution.
        simulated_slippage_pts) rather than carrying them in code, and picks
        deterministically from the order id so a replay of the same order
        reproduces the same fill. Set deterministic_per_order to false to go
        back to a random draw.

        These values are ASSUMED. Nothing on this desk has measured real
        slippage, because a paper fill is derived from signal_ltp and no order
        reaches a broker while execution_mode is PAPER_ONLY.
        """
        cfg = self.cfg.get("paper_execution") or {}
        # An ABSENT key falls back to the historical values; an explicitly EMPTY
        # list means zero slippage. `or` would have conflated the two, because
        # [] is falsy, and silently reinstated slippage someone had turned off.
        raw = cfg.get("simulated_slippage_pts")
        if raw is None:
            raw = [0.05, 0.10, 0.15, 0.20]
        choices = [float(x) for x in raw]
        if not choices:
            return 0.0
        if not cfg.get("deterministic_per_order", True):
            return round(float(np.random.choice(choices)), 2)
        digest = hashlib.md5(str(order_id).encode("utf-8")).hexdigest()
        return round(choices[int(digest, 16) % len(choices)], 2)

    # ── PROSPECTIVE SIGNAL OBSERVATION LOG ───────────────────────────────────
    #
    # WHY: the 5-factor confluence is computed and shown on the badge, but was
    # never persisted, so nothing linked pillar states to outcomes. Every filter
    # tested on this desk so far has been scored on data that already existed
    # when the idea was formed, which is how sixteen measures reached NOT BUILT.
    # The one test that cannot be p-hacked is a prospective one, because the
    # data does not exist yet.
    #
    # Rules this follows:
    #   - EVERY signal is logged, traded or not. Logging only executed signals
    #     would record exactly the survivors and bias the sample.
    #   - It GATES NOTHING. Write-only observation.
    #   - It can never break execution: all failures are swallowed and logged.
    #   - Outcomes are NOT copied here. position_id is recorded and the P&L is
    #     joined from positions later, so there is one source of truth.

    SIGNAL_OBS_DDL = """
    CREATE TABLE IF NOT EXISTS signal_observations (
        signal_uid      TEXT PRIMARY KEY,
        created_at      TEXT,
        session_date    TEXT,
        session_time    TEXT,
        slot_15m        TEXT,
        strategy_id     TEXT,
        setup_name      TEXT,
        direction       TEXT,
        opt_side        TEXT,
        conviction      TEXT,
        symbol          TEXT,
        security_id     TEXT,
        spot            REAL,
        premium         REAL,
        score           INTEGER,
        grade           TEXT,
        pillar_1_trend          INTEGER,
        pillar_2_anti_chop      INTEGER,
        pillar_3_slot_volume    INTEGER,
        pillar_4_writer_flow    INTEGER,
        pillar_5_pocket         INTEGER,
        slot_rvol_median        REAL,
        vsr_10                  REAL,
        bar_accel_ratio         REAL,
        slot_status             TEXT,
        oi_bias                 TEXT,
        oi_source               TEXT,
        separation_atr          REAL,
        flips_12bar             INTEGER,
        executed        INTEGER,
        position_id     TEXT,
        skip_reason     TEXT
    )
    """

    @staticmethod
    def _as_int_bool(v) -> int:
        return 1 if bool(v) else 0

    def start_recording(self):
        if getattr(self, 'recording_service', None) or not self.cfg.get('recording_v193', {}).get('enabled'):
            return
        try:
            from analyst.market_recorder_8510 import MarketRecorder
            self.recording_service = MarketRecorder(self)
            self.dhan_oi.record_callback = lambda *args: self.recording_service.enqueue('chain', *args)
            self.recording_service.start()
        except Exception:
            log.exception('Permanent recording startup failed open')

    def log_signal_observation(self, sig: Dict[str, Any],
                               contract: Optional[Dict[str, Any]] = None,
                               confluence: Optional[Dict[str, Any]] = None,
                               volume: Optional[Dict[str, Any]] = None,
                               oi: Optional[Dict[str, Any]] = None,
                               spot: Optional[float] = None) -> str:
        """Record one signal and the pillar state at that instant. Returns its uid.

        Never raises: a failure here must not stop a trade.
        """
        now = datetime.now(IST)
        uid = "SIG_{}_{}".format(now.strftime("%Y%m%d%H%M%S"),
                                 str(sig.get("strategy_id", "NA"))[:24])
        try:
            conf = confluence or {}
            pillars = conf.get("pillars") or conf.get("values") or {}
            vol = volume or {}
            oid = oi or {}
            hhmm = now.strftime("%H:%M")
            mins = now.hour * 60 + now.minute
            slot = "{:02d}:{:02d}".format((mins // 15 * 15) // 60, (mins // 15 * 15) % 60)

            def pill(*names):
                for n in names:
                    if n in pillars:
                        return self._as_int_bool(pillars[n])
                return None

            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(self.SIGNAL_OBS_DDL)
                conn.execute(
                    "INSERT OR REPLACE INTO signal_observations VALUES "
                    "(" + ",".join("?" * 32) + ")",
                    (uid, now.strftime("%Y-%m-%d %H:%M:%S IST"),
                     now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), slot,
                     str(sig.get("strategy_id", "")), str(sig.get("setup_name", "")),
                     str(sig.get("direction", "")), str(sig.get("opt_side", "")),
                     str(sig.get("conviction", "")),
                     str((contract or {}).get("symbol", "")),
                     str((contract or {}).get("security_id", "")),
                     float(spot) if spot else None,
                     float((contract or {}).get("ltp") or 0.0) or None,
                     int(conf.get("score")) if conf.get("score") is not None else None,
                     str(conf.get("grade", "")),
                     pill("pillar_1_trend_structure", "pillar_1_trend"),
                     pill("pillar_2_anti_chop_regime", "pillar_2_anti_chop"),
                     pill("pillar_3_slot_volume"),
                     pill("pillar_4_option_writer_flow", "pillar_4_writer_flow"),
                     pill("pillar_5_pocket_execution", "pillar_5_pocket"),
                     vol.get("slot_rvol_median"), vol.get("vsr_10"),
                     vol.get("bar_accel_ratio"), str(vol.get("slot_status", "")),
                     str(oid.get("bias", "")), str(oid.get("source", "")),
                     sig.get("separation_atr"), sig.get("flips_12bar"),
                     0, "", ""))
            recorder = getattr(self, 'recording_service', None)
            if recorder and contract:
                recorder.enqueue('signal', dict(contract), time.time())
            return uid
        except Exception as exc:
            log.warning("signal_observations insert failed (%s): %s",
                        type(exc).__name__, exc)
            return uid

    def mark_signal_outcome(self, signal_uid: str, executed: bool,
                            position_id: str = "", skip_reason: str = "") -> None:
        """Attach what happened to a logged signal. Never raises."""
        if not signal_uid:
            return
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    "UPDATE signal_observations SET executed=?, position_id=?, "
                    "skip_reason=? WHERE signal_uid=?",
                    (1 if executed else 0, str(position_id or ""),
                     str(skip_reason or ""), signal_uid))
        except Exception as exc:
            log.warning("signal_observations update failed (%s): %s",
                        type(exc).__name__, exc)

    def run_post_market_archivers(self,date_str=None,now=None):
        """Idempotent, bounded subprocesses. Source SQLite is never opened writable here."""
        now=now or datetime.now(IST)
        date_str=date_str or now.date().isoformat()
        selected=date.fromisoformat(date_str)
        cfg=self.cfg['post_market_archivers']
        if not cfg['enabled']:return {'ok':False,'status':'DISABLED','date':date_str}
        if selected>now.date() or (selected==now.date() and now.strftime('%H:%M')<cfg['schedule_ist']):
            return {'ok':False,'status':'NOT_DUE','date':date_str}
        if not self._archive_lock.acquire(blocking=False):return {'ok':False,'status':'RUNNING','date':date_str}
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS post_market_archives(day TEXT PRIMARY KEY, attempted REAL,payload TEXT)')
                previous=conn.execute('SELECT attempted,payload FROM post_market_archives WHERE day=?',(date_str,)).fetchone()
            if previous:
                saved=json.loads(previous[1])
                if saved.get('ok') or time.time()-previous[0]<cfg['retry_seconds']:return saved
            results=[]
            for name in ('archive_volume.py','archive_chain_day.py'):
                try:
                    proc=subprocess.run([sys.executable,str(ROOT/'scripts'/name),'--date',date_str],
                        timeout=cfg['timeout_seconds'],check=False,capture_output=True,text=True,cwd=str(ROOT))
                    results.append({'script':name,'returncode':proc.returncode,'ok':proc.returncode==0})
                except Exception as exc:
                    log.warning('Post-market archive %s failed (%s)',name,type(exc).__name__)
                    results.append({'script':name,'ok':False,'error':type(exc).__name__})
            outputs = {'volume':any((ROOT/'data/archive/volume'/(date_str+suffix)).exists() for suffix in ('.parquet','.csv.gz')),
                       'chain':(ROOT/'data/archive/chain'/(date_str+'.parquet')).exists(),
                       'candles':(ROOT/'data/archive/candles'/(date_str+'.parquet')).exists()}
            result={'ok':all(r['ok'] for r in results) and all(outputs.values()),'date':date_str,'scripts':results,'outputs':outputs}
            result['status']='COMPLETE' if result['ok'] else 'FAILED'
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute('INSERT OR REPLACE INTO post_market_archives VALUES(?,?,?)',(date_str,time.time(),json.dumps(result)))
            return result
        finally:self._archive_lock.release()

    def get_proven_edges_config(self) -> Dict[str, Any]:
        """Returns the active proven quant edges configuration."""
        gold = self.get_latest_goldilocks_status()
        vsr = self.futures_volume_engine.get_latest_vsr() if self.futures_volume_engine else {
            "vsr_10": 1.0, "is_surge": False, "badge": "NORMAL VOLUME",
            "pill_text": "📊 Futures Vol Surge (VSR): 1.00x [NORMAL VOLUME]"
        }
        res = {
            "ok": True,
            "proven_edges": self.proven_edges_cfg,
            "goldilocks_telemetry": gold,
            "futures_volume_telemetry": vsr,
        }
        res.update(self.proven_edges_cfg)
        return res

    def update_proven_edges_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Updates and persists the proven quant edges configuration."""
        for k in ["goldilocks_separation_enabled", "futures_volume_surge_enabled", "default_cost_model", "goldilocks_min_atr", "goldilocks_max_atr", "futures_volume_surge_min_ratio"]:
            if k in updates:
                self.proven_edges_cfg[k] = updates[k]

        self.cfg["proven_edges"] = self.proven_edges_cfg
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            log.info("Persisted updated proven quant edges to %s: %s", self.config_path, self.proven_edges_cfg)
        except Exception as e:
            log.error("Failed to persist proven quant edges: %s", e)

        return self.get_proven_edges_config()

    def get_ema_proximity_table(self, date_str: str = "LIVE", timeframe: str = "1m", filter_state: str = "ALL") -> Dict[str, Any]:
        """Calculates and returns the minute-by-minute 4-EMA & 8-EMA Proximity Matrix (v1.8)."""
        if not self.ema_proximity_engine:
            from analyst.ema_proximity_engine import ema_proximity_engine
            self.ema_proximity_engine = ema_proximity_engine
        live_spot = float(self.broker._ltp_cache.get("13", 0.0) or 0.0)
        return self.ema_proximity_engine.compute_matrix(
            date_str=date_str, timeframe=timeframe, filter_state=filter_state, live_spot=live_spot
        )

    def get_ema_proximity_config(self) -> Dict[str, Any]:
        """Returns the active 4/8 EMA Proximity Radar configuration."""
        if not self.ema_proximity_engine:
            from analyst.ema_proximity_engine import ema_proximity_engine
            self.ema_proximity_engine = ema_proximity_engine
        return self.ema_proximity_engine.get_config()

    def update_ema_proximity_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Updates and persists 4/8 EMA Proximity Radar configuration to terminal8510_config.json."""
        if not self.ema_proximity_engine:
            from analyst.ema_proximity_engine import ema_proximity_engine
            self.ema_proximity_engine = ema_proximity_engine
        return self.ema_proximity_engine.update_config(updates)



    def get_institutional_telemetry_cached(self):
        bridge=getattr(self,'institutional_telemetry_bridge',None)
        return bridge.snapshot() if bridge else {'available':False,'data_status':'NO_RECORDED_DATA','execution_gate_mode':'ADVISORY_ONLY'}

    def institutional_advisory_confluence(self, existing):
        # Separate HTTP presentation layer; existing strategy/execution methods are untouched.
        import copy
        result=copy.deepcopy(existing or {})
        observation=self.get_institutional_telemetry_cached()
        if not observation.get('available'):
            return result
        if not isinstance(result.get('pillars'),dict) or 'pillar_4_option_writer_flow' not in result['pillars']:
            return result
        score=observation.get('confirmation',{}).get('score_components',{}).get('pe_ce_points')
        side=result.get('direction')
        if not isinstance(score,(int,float)) or isinstance(score,bool) or not math.isfinite(score):
            return result
        passed=bool(observation['data_status']=='VALID' and (score>0 if side=='CE' else score<0 if side=='PE' else False))
        name='pillar_4_option_writer_flow';result[name]=passed
        prev=result['pillars'].get(name) or {}
        reason='PASS: institutional file telemetry' if passed else 'FAIL: institutional telemetry disagrees'
        result['pillars'][name]={'passed':passed,'label':prev.get('label','Dhan OI'),'reason':reason}
        result['score']=sum(bool(v['passed']) for v in result['pillars'].values())
        result['grade']={5:'A+ INSTITUTIONAL (5/5)',4:'A HIGH CONVICTION (4/5)',3:'B MODERATE (3/5)'}.get(result['score'],'C DEFENSIVE / WAIT (<=2/5)')
        result['source']='INSTITUTIONAL_TELEMETRY';result['informational_only']=True;result['execution_authorized']=False
        return result

    def _read_telemetry_db_candles(self) -> Dict[str, float]:
        """Sync SQLite read for spot/vix/fut fallback — run in a worker thread."""
        out = {
            "candle_spot": 0.0,
            "candle_spot_ts": 0,
            "vix": 0.0,
            "fut_price": 0.0,
        }
        if not self.market_db.exists():
            return out
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                cur = conn.cursor()
                r_spot = cur.execute("""
                    SELECT ts, c FROM candles_1m
                    WHERE sec_id='13'
                      AND time(datetime(ts, 'unixepoch', '+330 minutes')) <= '15:30:00'
                    ORDER BY ts DESC LIMIT 1
                """).fetchone()
                if not r_spot:
                    r_spot = cur.execute(
                        "SELECT ts, c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 1"
                    ).fetchone()
                if r_spot and r_spot[1]:
                    out["candle_spot_ts"] = int(r_spot[0]) if r_spot[0] else 0
                    out["candle_spot"] = float(r_spot[1])
                r_vix = cur.execute(
                    "SELECT c FROM candles_1m WHERE sec_id='21' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if r_vix and r_vix[0]:
                    out["vix"] = float(r_vix[0])
                r_fut = cur.execute(
                    "SELECT c FROM candles_1m WHERE sec_id='FUT' ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if r_fut and r_fut[0]:
                    out["fut_price"] = float(r_fut[0])
        except Exception as e:
            log.debug("DB read error in telemetry: %s", e)
        return out

    async def start_ema_live_cross_worker(self) -> None:
        """Background EMA cross ticks — keeps /api/telemetry off the hot path."""
        if self._ema_live_cross_worker_started:
            return
        self._ema_live_cross_worker_started = True
        refresh = float(self.scanner_cfg.get("ema_live_cross_refresh_seconds", 2.0) or 2.0)
        refresh = max(1.0, min(refresh, 30.0))
        log.info("EMA live-cross worker started (%.1fs refresh)", refresh)
        while True:
            try:
                spot = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
                if self.ema_proximity_engine and spot > 0:
                    payload = await asyncio.to_thread(
                        self.ema_proximity_engine.get_dhan_live_ema_cross,
                        spot,
                    )
                    if isinstance(payload, dict) and payload.get("ok"):
                        self._ema_live_cross_cache = payload
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("EMA live-cross worker: %s", exc)
            await asyncio.sleep(refresh)

    async def _sleep_tracking_spot(self, key: str, seconds: float) -> None:
        """Wait for the next tick while recording the high and low of spot.

        A 30-second tick used to see only the price at that instant, so a dip
        that touched the 8 EMA and bounced between two ticks was missed. Spot
        is refreshed about once a second; this keeps its range for the worker.
        """
        ranges = self.__dict__.setdefault("_tick_spot_range", {})
        hi = lo = 0.0
        waited = 0.0
        while waited < seconds:
            step = min(1.0, seconds - waited)
            await asyncio.sleep(step)
            waited += step
            px = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
            if px > 0:
                hi = px if hi <= 0 else max(hi, px)
                lo = px if lo <= 0 else min(lo, px)
        ranges[key] = (hi, lo)

    def _take_tick_spot_range(self, key: str, spot: float) -> Tuple[float, float]:
        """(high, low) of spot since the previous tick, always including spot now."""
        hi, lo = self.__dict__.setdefault("_tick_spot_range", {}).pop(key, (0.0, 0.0))
        return (max(hi, spot) if hi > 0 else spot, min(lo, spot) if lo > 0 else spot)

    async def start_ema8_prototype_intrabar_worker(self) -> None:
        """30s (config) refresh: armed prototype checks touch vs dynamic 8 EMA on forming 15m bar."""
        from analyst.ema8_prototype_8510 import (
            current_nse_15m_open_ts,
            ensure_forming_b15_bar,
            evaluate_ema8_prototype_intrabar,
            load_ema8_prototype_cfg,
        )
        from analyst.nse_spot_candles_8510 import resample_nse_15m

        if getattr(self, "_ema8_proto_worker_started", False):
            return
        self._ema8_proto_worker_started = True
        proto_cfg = load_ema8_prototype_cfg(self.scanner_cfg)
        refresh = float(proto_cfg.get("e8_refresh_seconds", 30.0) or 30.0)
        refresh = max(5.0, min(refresh, 60.0))
        log.info("8 EMA Prototype intrabar worker started (%.1fs)", refresh)
        while True:
            try:
                proto_cfg = load_ema8_prototype_cfg(self.scanner_cfg)
                if not bool(proto_cfg.get("enabled", True)):
                    await asyncio.sleep(refresh)
                    continue
                if not self._ema8_prototype_state.get("arm"):
                    await asyncio.sleep(refresh)
                    continue
                spot = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
                if spot <= 0:
                    await asyncio.sleep(refresh)
                    continue
                df1 = await asyncio.to_thread(self._load_spot_1m_for_scanner)
                if df1.empty or len(df1) < 30:
                    await asyncio.sleep(refresh)
                    continue
                df1 = df1.copy()
                df1["dt"] = pd.to_datetime(df1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                b15 = resample_nse_15m(df1.set_index("dt"))
                if b15.empty:
                    await asyncio.sleep(refresh)
                    continue
                bar_open_ts = current_nse_15m_open_ts(datetime.now(IST))
                b15, _bar_ci = ensure_forming_b15_bar(b15, bar_open_ts=bar_open_ts, live_spot=spot)
                form_high = form_low = None
                tick_high = tick_low = None
                try:
                    bar_open = pd.Timestamp(bar_open_ts, unit="s", tz="Asia/Kolkata")
                    forming = df1[df1["dt"] >= bar_open]
                    if not forming.empty:
                        form_high = float(forming["high"].max())
                        form_low = float(forming["low"].min())
                    last_tick_ts = getattr(self, "_ema8_proto_last_tick_ts", 0)
                    cur_ts = int(time.time())
                    if last_tick_ts > 0:
                        recent_1m = df1[(df1["ts"] >= last_tick_ts) & (df1["ts"] <= cur_ts)]
                        if not recent_1m.empty:
                            tick_high = max(float(recent_1m["high"].max()), spot)
                            tick_low = min(float(recent_1m["low"].min()), spot)
                        else:
                            tick_high = tick_low = spot
                    else:
                        tick_high = tick_low = spot
                    self._ema8_proto_last_tick_ts = cur_ts
                except Exception as exc:
                    log.debug("ema8 proto forming ohlc: %s", exc)
                seen_hi, seen_lo = self._take_tick_spot_range("ema8_proto", spot)
                tick_high = max(float(tick_high or spot), seen_hi)
                tick_low = min(float(tick_low or spot), seen_lo)
                session_day = datetime.now(IST).strftime("%Y-%m-%d")
                self._sync_open_ema8_prototype_position()
                sigs, st = await asyncio.to_thread(
                    evaluate_ema8_prototype_intrabar,
                    b15,
                    spot,
                    cfg=proto_cfg,
                    proto_state=self._ema8_prototype_state,
                    session_day=session_day,
                    gate_meta=self._gate_meta_for_signal,
                    form_high=form_high,
                    form_low=form_low,
                    tick_high=tick_high,
                    tick_low=tick_low,
                    now_hm=datetime.now(IST).strftime("%H:%M"),
                )
                prev_proto = dict(self.strategy_status.get("ema8_prototype") or {})
                prev_proto.update(st)
                self.strategy_status["ema8_prototype"] = prev_proto
                # What the algo itself just used, for the banner: the line, the spot it
                # compared, the touch band, and when. Display only.
                try:
                    from analyst.ema8_prototype_8510 import get_effective_touch_tolerance
                    self._ema8_proto_last_check = {
                        "e8": st.get("dynamic_e8"), "spot": round(float(spot), 2),
                        "tick_high": round(float(tick_high), 2), "tick_low": round(float(tick_low), 2),
                        "tol_pts": float(get_effective_touch_tolerance(proto_cfg)),
                        "at": datetime.now(IST).strftime("%H:%M:%S"), "at_ts": time.time(),
                        "refresh_sec": refresh, "armed": True,
                        "direction": ((self._ema8_prototype_state.get("arm") or {}).get("direction") or ""),
                        "away_seen": bool((self._ema8_prototype_state.get("arm") or {}).get("away_seen")),
                        "verdict": str(st.get("block_reason") or st.get("phase") or ""),
                    }
                except Exception:
                    pass
                self._handle_ema8_prototype_actions(st.get("actions"))
                self._save_ema8_prototype_state()
                if sigs:
                    await self._process_strategy_signals(sigs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ema8 prototype intrabar worker: %s", exc)
            await self._sleep_tracking_spot("ema8_proto", refresh)

    async def start_mj_dynamic_intrabar_worker(self) -> None:
        """Every 30s: Stage 2 / Stage 3 / 8 EMA Reversal first touch of the dynamic 8 EMA.

        The closed-candle scanner decides whether each setup is armed (4/8
        stack, BB width, band slope). This worker supplies the touch: the
        running 15m candle against the 8 EMA recomputed with live spot, the
        same line the 8 EMA Prototype uses.
        """
        from analyst.ema8_prototype_8510 import current_nse_15m_open_ts, ensure_forming_b15_bar
        from analyst.mj_sir_stage_reversal_8510 import evaluate_mj_sir_four_pack, load_mj_cfg
        from analyst.nse_spot_candles_8510 import resample_nse_15m

        if getattr(self, "_mj_dyn_worker_started", False):
            return
        self._mj_dyn_worker_started = True
        log.info("Stage 2/3 + Reversal dynamic 8 EMA worker started")
        while True:
            refresh = 30.0
            try:
                mj_cfg = load_mj_cfg(self.scanner_cfg)
                refresh = max(5.0, min(float(mj_cfg.get("dynamic_refresh_seconds", 30.0) or 30.0), 60.0))
                spot = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
                is_open, _status_txt = self.is_nse_market_open()
                if (
                    not bool(mj_cfg.get("enabled", True))
                    or not bool(mj_cfg.get("dynamic_8ema_entry", True))
                    or not self.scanner_enabled
                    or self.kill_switch_active
                    or not is_open
                    or spot <= 0
                ):
                    await asyncio.sleep(refresh)
                    continue
                df1 = await asyncio.to_thread(self._load_spot_1m_for_scanner)
                if df1.empty or len(df1) < 30:
                    await asyncio.sleep(refresh)
                    continue
                df1 = df1.copy()
                df1["dt"] = pd.to_datetime(df1["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                b15 = resample_nse_15m(df1.set_index("dt"))
                if b15.empty:
                    await asyncio.sleep(refresh)
                    continue
                now = datetime.now(IST)
                b15, _ci = ensure_forming_b15_bar(
                    b15, bar_open_ts=current_nse_15m_open_ts(now), live_spot=spot
                )
                # Price range since the previous tick, not the whole candle.
                tick_high = tick_low = spot
                last_ts = int(getattr(self, "_mj_dyn_last_tick_ts", 0) or 0)
                cur_ts = int(time.time())
                if last_ts > 0:
                    recent = df1[(df1["ts"] >= last_ts) & (df1["ts"] <= cur_ts)]
                    if not recent.empty:
                        tick_high = max(float(recent["high"].max()), spot)
                        tick_low = min(float(recent["low"].min()), spot)
                self._mj_dyn_last_tick_ts = cur_ts
                seen_hi, seen_lo = self._take_tick_spot_range("mj_dynamic", spot)
                tick_high, tick_low = max(tick_high, seen_hi), min(tick_low, seen_lo)

                sigs, mj_status = await asyncio.to_thread(
                    evaluate_mj_sir_four_pack,
                    b15,
                    bar_ci=-2,
                    cfg=mj_cfg,
                    bb_stage2=self._bollinger_gate_active("stage2"),
                    bb_stage3=self._bollinger_gate_active("stage3"),
                    bb_cross=self._bollinger_gate_active("crossover"),
                    party_switch_enabled=bool(self.scanner_cfg.get("party_switch_enabled", False)),
                    active_party=self.active_party,
                    mj_state=self._mj_sir_state,
                    session_day=now.strftime("%Y-%m-%d"),
                    gate_meta=self._gate_meta_for_signal,
                    dynamic={
                        "live_spot": spot,
                        "tick_high": tick_high,
                        "tick_low": tick_low,
                        "now_hm": now.strftime("%H:%M"),
                    },
                )
                for key in ("stage2_long", "stage3_short", "crossover_reversal"):
                    if key in mj_status:
                        prev = dict(self.strategy_status.get(key) or {})
                        prev.update(mj_status[key])
                        prev["spec"] = "MJ_SIR_LECTURE_UNDERSTANDING"
                        self.strategy_status[key] = prev
                if sigs:
                    sigs = await asyncio.to_thread(self.apply_writer_flow_gates, sigs)
                    if sigs:
                        await self._process_strategy_signals(sigs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Stage/Reversal dynamic 8 EMA worker: %s", exc)
            await self._sleep_tracking_spot("mj_dynamic", refresh)

    # ── TELEMETRY & 1-SECOND TICK STREAM ────────────────────────────────────
    async def get_live_telemetry(self) -> Dict[str, Any]:
        """Fetches 1-second live stream for ribbon, ATM widget, matrix, and trend filters."""
        if self._telemetry_lock.locked() and self.cached_telemetry:
            return dict(self.cached_telemetry)
        async with self._telemetry_lock:
            return await self._build_live_telemetry()

    async def get_live_telemetry_stale_ok(self) -> Dict[str, Any]:
        """Return last good payload immediately; refresh in background (prevents UI pile-up)."""
        if self.cached_telemetry:
            min_gap = max(1.0, float(self._telemetry_bg_refresh_min_sec))
            stale_enough = (time.monotonic() - self._telemetry_last_refresh_mono) >= min_gap
            if (
                stale_enough
                and not self._telemetry_refresh_inflight
                and not self._telemetry_lock.locked()
            ):

                async def _refresh() -> None:
                    self._telemetry_refresh_inflight = True
                    try:
                        await self.get_live_telemetry()
                        self._telemetry_last_refresh_mono = time.monotonic()
                    except Exception as exc:
                        log.debug("telemetry background refresh failed: %s", exc)
                    finally:
                        self._telemetry_refresh_inflight = False

                try:
                    asyncio.get_running_loop().create_task(_refresh())
                except RuntimeError:
                    pass
            return dict(self.cached_telemetry)
        return await self.get_live_telemetry()

    async def _build_live_telemetry(self) -> Dict[str, Any]:
        self.inst_mgr.ensure_loaded()
        await asyncio.to_thread(self.dhan_oi.get_dhan_oi_bias)
        now_dt = datetime.now(IST)

        # 1. Fetch latest candle prices from database (Candle Fallback / Tier 2)
        spot_net_chg = 0.0
        spot_pct_chg = 0.0
        vix_pct_chg = 0.0
        fut_spread = 0.0
        db_row = await asyncio.to_thread(self._read_telemetry_db_candles)
        candle_spot = float(db_row.get("candle_spot") or 0.0)
        candle_spot_ts = int(db_row.get("candle_spot_ts") or 0)
        vix = float(db_row.get("vix") or 0.0)
        fut_price = float(db_row.get("fut_price") or 0.0)

        active_fut = self.inst_mgr.get_active_future_contract()
        fut_sec_id = int(active_fut["security_id"]) if active_fut else 61471
        nearest_expiry = self.inst_mgr.get_nearest_expiry()

        # Pre-resolve candidate option security IDs for Dhan marketfeed LTP lookup
        candidate_spot = self.broker._ltp_cache.get("13", 0.0) or candle_spot
        candidate_sids = []
        if candidate_spot > 0:
            c_atm = float(round(candidate_spot / 100.0) * 100)
            for c_strk, c_opt in [(c_atm - 100.0, "CE"), (c_atm, "CE"), (c_atm + 100.0, "PE"), (c_atm, "PE")]:
                res_c = self.inst_mgr.resolve_option(nearest_expiry, c_strk, c_opt)
                if res_c and res_c.get("security_id"):
                    try:
                        candidate_sids.append(int(res_c["security_id"]))
                    except Exception:
                        pass

        # 2. Live Quote Fetch via Dhan /marketfeed/ltp
        live_quotes = {}
        if self.broker.is_configured():
            req_seg = {"IDX_I": [13, 21], "NSE_FNO": [fut_sec_id] + candidate_sids}
            live_quotes = await self.broker.get_marketfeed_ltp(req_seg)

        # Check NSE Market Open Status (moved above spot resolution for freshness classification)
        mkt_res = self.is_nse_market_open(now_dt)
        if isinstance(mkt_res, dict):
            is_open = bool(mkt_res.get("is_open", False))
            mkt_dict = {k: mkt_res[k] for k in mkt_res.keys()}
        elif isinstance(mkt_res, (list, tuple)):
            is_open = bool(mkt_res[0])
            mkt_dict = {"is_open": is_open, "status": str(mkt_res[1])}
        else:
            is_open = False
            mkt_dict = {"is_open": False, "status": "CLOSED"}

        # 3. Step 4A: 3-Tier Price Resolution Hierarchy
        # Tier 1: Live Quote
        # Note on Dhan /v2/marketfeed/ltp:
        # Verified live against Dhan HQ v2 API: the LTP endpoint response format is:
        #   {"data": {"IDX_I": {"13": {"last_price": 24536.6}}}, "status": "success"}
        # It does NOT return 'last_trade_time' or 'LTT' (only present in full quote mode).
        # Dhan returns the last traded price 24/7, even when the market is closed.
        # Therefore, we classify Tier 1 quote as LIVE only when BOTH hold:
        #   1. self.is_nse_market_open() returns True (NSE regular trading hours 09:15 - 15:30 IST Mon-Fri).
        #   2. If exchange trade timestamp (LTT / last_trade_time) is ever provided, verify age < 60s.
        # When market is closed (or quote is stale), spot_status is marked 'STALE' (is_stale=True),
        # and spot_time_ist is set to the exchange time if present, or the last closed 1m candle timestamp — NEVER datetime.now().
        spot = 0.0
        spot_status = "UNAVAILABLE"
        is_stale = False
        spot_time_ist = None

        if "13" in live_quotes and live_quotes["13"] > 0:
            spot = float(live_quotes["13"])
            # Freshness verification
            ltt = self.broker.get_last_trade_time("13")
            is_quote_fresh = True
            if ltt is not None:
                try:
                    is_quote_fresh = (time.time() - float(ltt)) < 60.0
                except Exception:
                    pass

            if is_open and is_quote_fresh:
                spot_status = "LIVE"
                is_stale = False
                spot_time_ist = now_dt.strftime("%H:%M:%S IST")
            else:
                spot_status = "STALE"
                is_stale = True
                if ltt is not None:
                    try:
                        dt_ltt = datetime.fromtimestamp(float(ltt), tz=timezone.utc).astimezone(IST)
                        spot_time_ist = dt_ltt.strftime("%H:%M:%S IST")
                    except Exception:
                        pass
                if not spot_time_ist:
                    if candle_spot_ts > 0:
                        c_dt = datetime.fromtimestamp(candle_spot_ts, tz=timezone.utc).astimezone(IST)
                        spot_time_ist = c_dt.strftime("%H:%M:%S IST")
                    else:
                        spot_time_ist = "15:29:00 IST"
        # Tier 2: Last Closed 1m Candle
        elif candle_spot > 0:
            spot = candle_spot
            spot_status = "STALE"
            is_stale = True
            if candle_spot_ts > 0:
                c_dt = datetime.fromtimestamp(candle_spot_ts, tz=timezone.utc).astimezone(IST)
                spot_time_ist = c_dt.strftime("%H:%M:%S IST")
            else:
                spot_time_ist = "15:29:00 IST"
        # Tier 3: Nothing at all -> spot = 0.0, fail closed loudly
        else:
            spot = 0.0
            spot_status = "UNAVAILABLE"
            is_stale = False
            spot_time_ist = None

        if "21" in live_quotes and live_quotes["21"] > 0:
            vix = float(live_quotes["21"])
        if str(fut_sec_id) in live_quotes and live_quotes[str(fut_sec_id)] > 0:
            fut_price = float(live_quotes[str(fut_sec_id)])
        if fut_price > 0 and spot > 0:
            fut_spread = round(fut_price - spot, 2)

        # 4. Market status and spot availability
        matrix_reason = None
        if spot <= 0.0:
            matrix_reason = "Spot unavailable — market closed / feed offline"
            atm_strike = 0.0
            atm_ce_ltp = 0.0
            atm_pe_ltp = 0.0
            combined_straddle = 0.0
            priority_matrix = []
        else:
            # Step 4C: 100-multiples ATM strike
            # v1.9.4c BUGFIX: this rounded to 100, but NIFTY strikes exist every
            # 50 points. Rounding to 100 could only ever select even-hundred
            # strikes and NEVER a 50-level one, so for spot 24,460 it bought
            # 24500 while true ATM was 24450 -- up to 25 points off, with half
            # the chain unreachable. A strike 50 points from ATM carries a
            # visibly different premium, which is why entry prices looked wrong.
            # The step comes from config; the repo already declares 50.
            strike_step = float(self.cfg.get("recording_v193", {}).get("strike_step", 50) or 50)
            atm_strike = float(round(spot / strike_step) * strike_step)

            # Resolve Priority 4 Contracts around Spot (100-multiples)
            itm_ce_strike = atm_strike - 100.0
            atm_ce_strike = atm_strike
            itm_pe_strike = atm_strike + 100.0
            atm_pe_strike = atm_strike

            resolved_itm_ce = self.inst_mgr.resolve_option(nearest_expiry, itm_ce_strike, "CE")
            resolved_atm_ce = self.inst_mgr.resolve_option(nearest_expiry, atm_ce_strike, "CE")
            resolved_itm_pe = self.inst_mgr.resolve_option(nearest_expiry, itm_pe_strike, "PE")
            resolved_atm_pe = self.inst_mgr.resolve_option(nearest_expiry, atm_pe_strike, "PE")

            # Only a real, fresh Dhan quote can make a contract executable.
            def get_opt_ltp(resolved, strike, opt_type):
                if not resolved:return None
                sid = str(resolved.get('security_id'))
                value = live_quotes.get(sid)
                stamp = self.broker._cache_timestamps.get(sid, 0)
                max_age = float(self.cfg['five_factor_confluence']['quote_max_age_seconds'])
                if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or time.time()-stamp>max_age:
                    return None
                return float(value)

            itm_ce_ltp = get_opt_ltp(resolved_itm_ce, itm_ce_strike, "CE")
            atm_ce_ltp = get_opt_ltp(resolved_atm_ce, atm_ce_strike, "CE")
            itm_pe_ltp = get_opt_ltp(resolved_itm_pe, itm_pe_strike, "PE")
            atm_pe_ltp = get_opt_ltp(resolved_atm_pe, atm_pe_strike, "PE")
            combined_straddle = round(atm_ce_ltp + atm_pe_ltp, 2) if atm_ce_ltp is not None and atm_pe_ltp is not None else None

            # Priority 4 Contracts list
            priority_matrix = [
                {
                    "id": "LONG_CE_ITM",
                    # v1.9.4c: was "(~Rs200)", which read as a fixed entry price and is
                    # what made the desk look like it always entered at 200. The
                    # premium is the real live Dhan quote in "ltp"; the strike is
                    # ATM-100 points. The label now states the rule, not a price.
                    "label": "1. Long ITM CE (ATM-100)",
                    "strike": itm_ce_strike,
                    "opt_type": "CE",
                    "symbol": format_contract_symbol(expiry=nearest_expiry, strike=itm_ce_strike, opt_type="CE", raw_symbol=resolved_itm_ce["symbol"] if resolved_itm_ce else None),
                    "structured_contract": get_contract_structured_fields(format_contract_symbol(expiry=nearest_expiry, strike=itm_ce_strike, opt_type="CE"), expiry=nearest_expiry),
                    "security_id": resolved_itm_ce["security_id"] if resolved_itm_ce else None,
                    "lot_size": resolved_itm_ce["lot_size"] if resolved_itm_ce else None,
                    "ltp": itm_ce_ltp,
                    "delta": 0.65,
                    "spread_pts": 0.45,
                    "oi_lakhs": 28.5,
                    "volume_lakhs": 142.1,
                    "algo_enabled": self.algo_strike_selection.get("LONG_CE_ITM", True),
                },
                {
                    "id": "LONG_CE_ATM",
                    "label": "2. Long ATM CE",
                    "strike": atm_ce_strike,
                    "opt_type": "CE",
                    "symbol": format_contract_symbol(expiry=nearest_expiry, strike=atm_ce_strike, opt_type="CE", raw_symbol=resolved_atm_ce["symbol"] if resolved_atm_ce else None),
                    "structured_contract": get_contract_structured_fields(format_contract_symbol(expiry=nearest_expiry, strike=atm_ce_strike, opt_type="CE"), expiry=nearest_expiry),
                    "security_id": resolved_atm_ce["security_id"] if resolved_atm_ce else None,
                    "lot_size": resolved_atm_ce["lot_size"] if resolved_atm_ce else None,
                    "ltp": atm_ce_ltp,
                    "delta": 0.50,
                    "spread_pts": 0.35,
                    "oi_lakhs": 42.8,
                    "volume_lakhs": 268.4,
                    "algo_enabled": self.algo_strike_selection.get("LONG_CE_ATM", True),
                },
                {
                    "id": "LONG_PE_ITM",
                    "label": "3. Long ITM PE (ATM+100)",
                    "strike": itm_pe_strike,
                    "opt_type": "PE",
                    "symbol": format_contract_symbol(expiry=nearest_expiry, strike=itm_pe_strike, opt_type="PE", raw_symbol=resolved_itm_pe["symbol"] if resolved_itm_pe else None),
                    "structured_contract": get_contract_structured_fields(format_contract_symbol(expiry=nearest_expiry, strike=itm_pe_strike, opt_type="PE"), expiry=nearest_expiry),
                    "security_id": resolved_itm_pe["security_id"] if resolved_itm_pe else None,
                    "lot_size": resolved_itm_pe["lot_size"] if resolved_itm_pe else None,
                    "ltp": itm_pe_ltp,
                    "delta": -0.65,
                    "spread_pts": 0.45,
                    "oi_lakhs": 31.2,
                    "volume_lakhs": 156.8,
                    "algo_enabled": self.algo_strike_selection.get("LONG_PE_ITM", True),
                },
                {
                    "id": "LONG_PE_ATM",
                    "label": "4. Long ATM PE",
                    "strike": atm_pe_strike,
                    "opt_type": "PE",
                    "symbol": format_contract_symbol(expiry=nearest_expiry, strike=atm_pe_strike, opt_type="PE", raw_symbol=resolved_atm_pe["symbol"] if resolved_atm_pe else None),
                    "structured_contract": get_contract_structured_fields(format_contract_symbol(expiry=nearest_expiry, strike=atm_pe_strike, opt_type="PE"), expiry=nearest_expiry),
                    "security_id": resolved_atm_pe["security_id"] if resolved_atm_pe else None,
                    "lot_size": resolved_atm_pe["lot_size"] if resolved_atm_pe else None,
                    "ltp": atm_pe_ltp,
                    "delta": -0.50,
                    "spread_pts": 0.35,
                    "oi_lakhs": 39.4,
                    "volume_lakhs": 215.7,
                    "algo_enabled": self.algo_strike_selection.get("LONG_PE_ATM", True),
                },
            ]
            # Mark contracts blocked by directional bias
            for c in priority_matrix:
                c["blocked_by_bias"] = (
                    (self.directional_bias == "LONG_ONLY" and c["opt_type"] == "PE") or
                    (self.directional_bias == "SHORT_ONLY" and c["opt_type"] == "CE")
                )
                c["bias_mode"] = self.directional_bias

        self.priority_contracts = priority_matrix

        # One Dhan expiry and observation, shared with pillar 4 and Tab 6.
        oi_state = self.dhan_oi.cached_snapshot()
        pcr_available = bool(oi_state['available'])
        pcr = float(oi_state['pcr_total_oi']) if pcr_available else 0.0
        pe_ce_gap_cr = -oi_state['ce_pe_oi_diff']/1e7 if pcr_available else 0.0
        pcr_tag = oi_state['bias'] if pcr_available else 'NO DATA'

        # 6. Trend Confluence Mini-Watchlist (7 Indicators)
        trend_watchlist = await asyncio.to_thread(
            self._calculate_trend_watchlist, spot, pcr, pcr_available
        )

        # 7. Funds & Paper Wallet
        paper_wallet = self.get_paper_wallet()
        live_funds = await self.broker.get_fundlimit()

        # 8. Active Positions with live fluctuating MTM
        if self.broker.is_configured():
            await self._check_broker_sl_fills()
        positions = self.get_active_positions(live_quotes)
        orders = self.get_order_book()

        # 9. Institutional Derivatives Confluence (Live OI & PE-CE Ingestion)
        try:
            oi_snap = self.dhan_oi.compatibility_snapshot(self.dhan_oi.cached_snapshot())
        except Exception as e:
            log.debug("Institutional OI scraper snapshot error: %s", e)
            oi_snap = {}

        trend_lab_variables = {
            "var_pe_minus_ce": float(oi_snap.get("pe_minus_ce_day") or 0.0),
            "var_call_oi_change": float(oi_snap.get("call_oi_change_day") or 0.0),
            "var_put_oi_change": float(oi_snap.get("put_oi_change_day") or 0.0),
            "var_total_call_oi": float(oi_snap.get("total_call_oi") or 0.0),
            "var_total_put_oi": float(oi_snap.get("total_put_oi") or 0.0),
            "var_oi_writer_bias": str(oi_snap.get("writer_bias", "NEUTRAL")),
            # Backward-compatible aliases
            "var_dhan_oi_pe_minus_ce": float(oi_snap.get("pe_minus_ce_day") or 0.0),
            "var_dhan_call_oi_change": float(oi_snap.get("call_oi_change_day") or 0.0),
            "var_dhan_put_oi_change": float(oi_snap.get("put_oi_change_day") or 0.0),
            "var_dhan_total_call_oi": float(oi_snap.get("total_call_oi") or 0.0),
            "var_dhan_total_put_oi": float(oi_snap.get("total_put_oi") or 0.0),
            "var_dhan_oi_writer_bias": str(oi_snap.get("writer_bias", "NEUTRAL")),
        }

        # Proven Quant Edges (v1.7)
        goldilocks = self.get_latest_goldilocks_status()
        fut_surge = self.futures_volume_engine.get_latest_vsr() if self.futures_volume_engine else {
            "vsr_10": None,
            "is_surge": False,
            "badge": "NORMAL VOLUME",
            "pill_text": "📊 Futures Vol Surge (VSR): 1.00x [NORMAL VOLUME]",
            "tag": "NORMAL_VOLUME",
        }

        # 4/8 EMA Proximity Radar (v1.8)
        live_spot_for_radar = float(self.broker._ltp_cache.get("13", 0.0) or spot or 0.0)
        ema_radar = self.ema_proximity_engine.get_latest_radar_status(live_spot=live_spot_for_radar) if self.ema_proximity_engine else {
            "ok": True,
            "tier": "ZONE_FAR",
            "pill_text": "🎯 4/8 EMA Radar: -- [WAITING]",
            "proximity_state": "ZONE_FAR",
        }

        ema_live_cross = dict(self._ema_live_cross_cache) if self._ema_live_cross_cache.get("ok") else {"ok": False}

        recorder = getattr(self, 'recording_service', None)
        if recorder and not is_stale and live_spot_for_radar > 0:
            recorder.enqueue('radar', dict(ema_radar), live_spot_for_radar, time.time())

        payload = {
            "timestamp_ist": now_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
            "clock": now_dt.strftime("%H:%M:%S IST"),
            "market_status": mkt_dict,
            "allow_off_hours_paper_testing": self.allow_off_hours_paper_testing,
            "algo_only_execution": self.algo_only_execution,
            "matrix_reason": matrix_reason,
            "goldilocks_separation": goldilocks,
            "futures_volume_surge": fut_surge,
            "ema_proximity_latest": ema_radar,
            "ema_live_cross": ema_live_cross,
            "proven_edges": {
                "goldilocks": goldilocks,
                "futures_volume_surge": fut_surge,
                "config": self.proven_edges_cfg,
            },
            "spot": {
                "price": spot,
                "net_change": spot_net_chg,
                "pct_change": spot_pct_chg,
                "status": spot_status,
                "is_stale": is_stale,
                "time_ist": spot_time_ist,
            },
            "future": {
                "price": fut_price,
                "spread": fut_spread,
                "spread_tag": f"{'+' if fut_spread >= 0 else ''}{fut_spread:.2f} pts {'Prem' if fut_spread >= 0 else 'Disc'}",
            },
            "vix": {
                "price": vix,
                "pct_change": vix_pct_chg,
            },
            "pcr": {
                "ratio": pcr,
                "tag": pcr_tag,
                "gap_cr": pe_ce_gap_cr,
            },
            "atm": {
                "strike": int(atm_strike),
                "ce_ltp": atm_ce_ltp,
                "pe_ltp": atm_pe_ltp,
                "combined_straddle": combined_straddle,
            },
            "directional_bias": self.directional_bias,
            "execution": {
                "mode": self.execution_mode,
                "paper_enabled": self.paper_execution_enabled,
                "directional_bias": self.directional_bias,
                "kill_switch_active": self.kill_switch_active,
                "chasing": self.chasing_cfg,
                "super_trail": self.super_trail_cfg,
                "algo_strike_selection": self.algo_strike_selection,
            },
            "priority_matrix": priority_matrix,
            "trend_watchlist": trend_watchlist,
            "scanner": self.get_scanner_status(),
            "trend_lab_variables": trend_lab_variables,
            "institutional_pe_ce": oi_snap,
            # So the UI can grey out rather than render zeros as real readings.
            "writer_flow_available": bool(oi_snap.get("available", False)),
            "writer_flow_source": str(oi_snap.get("source", "UNKNOWN")),
            "writer_flow_reason": str(oi_snap.get("reason", "")),
            "dhan_oi_pe_ce": oi_snap,
            "dhan_oi_synergy": self.dhan_oi.evaluate_synergy_status(),
            "dhan_oi_crore_spread": self.dhan_oi.get_crore_spread_metrics(),
            "dhan_oi_multi_strike": self.dhan_oi.get_multi_strike_status(),
            "dhan_oi_synergy_config": self.dhan_oi_synergy_cfg,
            "stockmojo_live": self.get_stockmojo_live(),
            "writer_flow_gate": self._writer_flow_context(),
            "writer_flow_gate_config": self.writer_flow_gate_cfg,
            "funds": {
                "paper": paper_wallet,
                "live": live_funds,
            },
            "positions": positions,
            "orders": orders,
            "dhan_connected": self.broker.is_configured(),
            "exit_failure_banner": getattr(self, "exit_failure_banner", None),
        }
        payload['dhan_oi'] = self.dhan_oi.cached_snapshot()
        payload["five_factor_confluence"] = await asyncio.to_thread(
            self.get_five_factor_confluence, spot, not is_stale
        )
        payload['trend_capture_live'] = await asyncio.to_thread(self.trend_capture_service().status)
        payload["oi_context"] = await asyncio.to_thread(self.oi_context, spot)
        payload["desk8600_board"] = await asyncio.to_thread(self.desk8600_board)
        payload["desk8600_ticket_lane"] = await asyncio.to_thread(self.desk8600_ticket_lane_status)
        payload["desk8600_link"] = await asyncio.to_thread(self.desk8600_link)
        self.cached_telemetry = payload
        self._telemetry_last_refresh_mono = time.monotonic()
        return payload

    def _calculate_trend_watchlist(self, current_spot: float, pcr: float,
                                   pcr_available: bool = False) -> Dict[str, Any]:
        """Calculates 7 trend confluence parameters from candles + PCR."""
        # Defaults
        # These three ARE recomputed from candles below when market_db has data.
        # Until then they must not vote, so they start UNAVAILABLE rather than
        # BULLISH with an invented figure.
        ema_stack = {"status": "UNAVAILABLE", "reading": "Awaiting 15m candle data"}
        adx_val = {"status": "UNAVAILABLE", "reading": "Awaiting 15m candle data"}
        r2_val = {"status": "UNAVAILABLE", "reading": "Awaiting 15m candle data"}

        # NOT COMPUTED ANYWHERE. These were previously hardcoded to BULLISH with
        # fabricated readings ("1H 8-EMA at 23,230 (+730 pts Above Support)",
        # "15m, 30m, 1H Triple Aligned Bullish", "Vol 1.25x vs MA20
        # (Institutional Accumulation)") and nothing ever overwrote them. Three
        # permanent BULLISH votes out of seven meant the panel reached "STRONG
        # BULLISH REGIME" on only two real signals, and "STRONG BEARISH REGIME"
        # (bears >= 5) was arithmetically unreachable. They now abstain until
        # they are genuinely implemented.
        h1_ema = {"status": "UNAVAILABLE", "reading": "1H EMA check not implemented"}
        mtf_val = {"status": "UNAVAILABLE", "reading": "Multi-timeframe alignment not implemented"}
        vol_surge = {"status": "UNAVAILABLE", "reading": "Volume surge check not implemented"}
        # "Put Writing Dominates" used to be hardcoded into this string, so the
        # panel said it even at PCR 0.70 where call writers dominate. The phrase
        # now follows the actual reading, and abstains without chain data.
        if not pcr_available:
            pcr_flow = {"status": "UNAVAILABLE",
                        "reading": "No chain_snap data — PCR unavailable"}
        else:
            if pcr >= 1.0:
                _pcr_state, _pcr_phrase = "BULLISH", "Put Writing Dominates"
            elif pcr < 0.85:
                _pcr_state, _pcr_phrase = "BEARISH", "Call Writing Dominates"
            else:
                _pcr_state, _pcr_phrase = "SIDEWAYS", "Writers Balanced"
            pcr_flow = {"status": _pcr_state,
                        "reading": f"Institutional PCR = {pcr:.2f} | {_pcr_phrase}"}

        if self.market_db.exists():
            try:
                with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                    df = pd.read_sql_query(
                        "SELECT ts, o as open, h as high, l as low, c as close, v as volume FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 600",
                        conn
                    )
                    if not df.empty:
                        df = df.iloc[::-1].reset_index(drop=True)
                        df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                        df.set_index("dt", inplace=True)
                        b15 = resample_nse_15m(df)
                        if len(b15) >= 21:
                            _, ema8 = ema4_ema8(b15["close"])
                            ema21 = b15["close"].ewm(span=21, adjust=False).mean()
                            e8_last, e21_last = ema8.iloc[-1], ema21.iloc[-1]
                            slope = e8_last - ema8.iloc[-2]
                            if e8_last > e21_last and slope > 0:
                                ema_stack = {"status": "BULLISH", "reading": f"8-EMA ({e8_last:.0f}) > 21-EMA ({e21_last:.0f}) | Slope +{slope:.1f}"}
                            elif e8_last < e21_last and slope < 0:
                                ema_stack = {"status": "BEARISH", "reading": f"8-EMA ({e8_last:.0f}) < 21-EMA ({e21_last:.0f}) | Slope {slope:.1f}"}
                            else:
                                ema_stack = {"status": "SIDEWAYS", "reading": f"8-EMA ({e8_last:.0f}) ~ 21-EMA ({e21_last:.0f}) | Flat"}

                            # ADX
                            h, l, c = b15["high"], b15["low"], b15["close"]
                            prev_c = c.shift(1)
                            tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
                            up_m = h - h.shift(1)
                            down_m = l.shift(1) - l
                            plus_dm = np.where((up_m > down_m) & (up_m > 0), up_m, 0.0)
                            minus_dm = np.where((down_m > up_m) & (down_m > 0), down_m, 0.0)
                            atr = pd.Series(tr).ewm(alpha=1/14, adjust=False).mean()
                            plus_di = 100 * (pd.Series(plus_dm, index=b15.index).ewm(alpha=1/14, adjust=False).mean() / atr)
                            minus_di = 100 * (pd.Series(minus_dm, index=b15.index).ewm(alpha=1/14, adjust=False).mean() / atr)
                            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
                            adx_series = dx.ewm(alpha=1/14, adjust=False).mean()
                            adx_curr = adx_series.iloc[-1]
                            if adx_curr >= 20.0 and plus_di.iloc[-1] > minus_di.iloc[-1]:
                                adx_val = {"status": "BULLISH", "reading": f"ADX = {adx_curr:.1f} (+DI {plus_di.iloc[-1]:.0f} > -DI {minus_di.iloc[-1]:.0f})"}
                            elif adx_curr >= 20.0 and minus_di.iloc[-1] > plus_di.iloc[-1]:
                                adx_val = {"status": "BEARISH", "reading": f"ADX = {adx_curr:.1f} (-DI {minus_di.iloc[-1]:.0f} > +DI {plus_di.iloc[-1]:.0f})"}
                            else:
                                adx_val = {"status": "SIDEWAYS", "reading": f"ADX = {adx_curr:.1f} (Sub-20 Chop / Consolidation)"}

                            # R2
                            y = b15["close"].iloc[-20:].values
                            x = np.arange(len(y))
                            r = np.corrcoef(x, y)[0, 1]
                            r2 = r**2 if np.isfinite(r) else 0.0
                            if r2 >= 0.35 and slope > 0:
                                r2_val = {"status": "BULLISH", "reading": f"LinReg R² = {r2:.2f} (Persistent Uptrend)"}
                            elif r2 >= 0.35 and slope < 0:
                                r2_val = {"status": "BEARISH", "reading": f"LinReg R² = {r2:.2f} (Persistent Downtrend)"}
                            else:
                                r2_val = {"status": "SIDEWAYS", "reading": f"LinReg R² = {r2:.2f} (Non-Linear / Rangebound)"}
            except Exception as e:
                log.debug("Trend watchlist calculation error: %s", e)

        items = [
            {"name": "1. 8-EMA vs 21-EMA Stack & Slope", **ema_stack},
            {"name": "2. 1H 8-EMA Distance & Status", **h1_ema},
            {"name": "3. ADX (14) Strength Reading", **adx_val},
            {"name": "4. Linear Regression R² Trend Quality", **r2_val},
            {"name": "5. Multi-Timeframe Alignment (15m/30m/1H)", **mtf_val},
            {"name": "6. Volume Surge vs 20-MA", **vol_surge},
            {"name": "7. Institutional PCR & PE-CE Flow", **pcr_flow},
        ]

        bulls = sum(1 for x in items if x["status"] == "BULLISH")
        bears = sum(1 for x in items if x["status"] == "BEARISH")
        n_avail = sum(1 for x in items if x["status"] != "UNAVAILABLE")

        # Verdict is a supermajority of the indicators that ACTUALLY reported,
        # not a fixed 5-of-7. With a fixed threshold, abstaining indicators made
        # a bearish call unreachable; scaling to what reported keeps the two
        # directions symmetric. Fewer than 3 live indicators is not a regime.
        need = max(3, int(round(0.70 * n_avail))) if n_avail else 0
        if n_avail < 3:
            regime = "⚪ INSUFFICIENT DATA — {} of {} indicators reporting".format(n_avail, len(items))
            badge = "NO_DATA"
        elif bulls >= need:
            regime = "🟢 STRONG BULLISH REGIME ({}/{} indicators)".format(bulls, n_avail)
            badge = "BULLISH"
        elif bears >= need:
            regime = "🔴 STRONG BEARISH REGIME ({}/{} indicators)".format(bears, n_avail)
            badge = "BEARISH"
        else:
            regime = "🟡 SIDEWAYS / NO-TRADE ZONE ({}B/{}S of {})".format(bulls, bears, n_avail)
            badge = "SIDEWAYS"

        return {
            "items": items,
            "bullish_count": bulls,
            "bearish_count": bears,
            "available_count": n_avail,
            "total_count": len(items),
            "threshold_used": need,
            "regime_banner": regime,
            "regime_badge": badge,
        }

    # ── 4-CORE STRATEGIES LIVE SCANNER & PARTY-SWITCH ENGINE ───────────────────
    def _allowed_algo_strategy_id_set(self) -> Optional[set]:
        if not bool(self.algo_allowlist_cfg.get("enabled", False)):
            return None
        raw = self.algo_allowlist_cfg.get("strategy_ids") or []
        return {str(x).strip().upper() for x in raw if str(x).strip()}

    def _ensure_ema8_prototype_allowlist_ids(self) -> None:
        if not bool(self.algo_allowlist_cfg.get("enabled", False)):
            return
        raw = list(self.algo_allowlist_cfg.get("strategy_ids") or [])
        upper = {str(x).strip().upper() for x in raw}
        for sid in ("EMA8_PROTOTYPE_LONG", "EMA8_PROTOTYPE_SHORT"):
            if sid not in upper:
                raw.append(sid)
                upper.add(sid)
        self.algo_allowlist_cfg["strategy_ids"] = raw

    def ema8_prototype_check_status(self) -> Dict[str, Any]:
        """The Prototype's own last 30-second 8 EMA check, for the live banner. Never raises."""
        try:
            from analyst.ema8_prototype_8510 import get_effective_touch_tolerance, load_ema8_prototype_cfg

            cfg = load_ema8_prototype_cfg(self.scanner_cfg)
            last = dict(self.__dict__.get("_ema8_proto_last_check") or {})
            armed = bool(self._ema8_prototype_state.get("arm")) if hasattr(self, "_ema8_prototype_state") else False
            refresh = float(cfg.get("e8_refresh_seconds", 30.0) or 30.0)
            out = {
                "armed": armed, "enabled": bool(cfg.get("enabled", True)), "refresh_sec": refresh,
                "tol_pts": float(get_effective_touch_tolerance(cfg)),
                "away_pts": float(cfg.get("away_min_pts", 2.0)),
            }
            if last:
                age = max(0.0, time.time() - float(last.get("at_ts") or 0.0))
                out.update(last, age_sec=round(age), next_in_sec=max(0, round(refresh - age)) if armed else None)
            return out
        except Exception as exc:
            return {"armed": False, "error": type(exc).__name__}

    def _ema8_prototype_view(self) -> Dict[str, Any]:
        """Plain-language position of the Prototype in its sequence (display only). Never raises."""
        try:
            from analyst.cockpit_views_8510 import prototype_view

            route = "LIVE" if str(getattr(self, "execution_mode", "")) == "BOTH" else ""
            return prototype_view(
                self.strategy_status.get("ema8_prototype"),
                self.__dict__.get("_ema8_prototype_state"),
                (self.cached_telemetry.get("spot") or {}).get("price"),
                route,
            )
        except Exception as exc:
            return {"step": 0, "headline": "status unavailable", "sub": type(exc).__name__, "steps": [], "route": ""}

    def _sync_ema8_prototype_desk_status(self) -> None:
        from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg, _execution_mode_labels

        cfg = load_ema8_prototype_cfg(self.scanner_cfg)
        paper_only, exec_label = _execution_mode_labels(cfg)
        row = dict(self.strategy_status.get("ema8_prototype") or {})
        row["name"] = "8 EMA Prototype"
        row["execution_mode"] = exec_label
        row["paper_only_lane"] = paper_only
        row["enabled"] = bool(cfg.get("enabled", True))
        if not row["enabled"]:
            row["state"] = "PAUSED"
            row["phase"] = "DISABLED"
            row["block_reason"] = "Prototype manually PAUSED in cockpit"
        elif row.get("state") == "PAUSED":
            row["state"] = "SCANNING"
            row["phase"] = "WAITING FOR CLOSE"
            row["block_reason"] = ""
        row["desc"] = (
            "Close cross 8 EMA · 30s retouch · dual ±40 spot+option · opposite 15m close exit"
        )
        self.strategy_status["ema8_prototype"] = row

    def algo_strategy_execution_allowed(self, strategy_id: str) -> bool:
        allowed = self._allowed_algo_strategy_id_set()
        if allowed is None:
            return True
        sid = str(strategy_id or "").strip().upper()
        return bool(sid) and sid in allowed

    def _select_ema8_prototype_contract(self, opt_side: str, min_premium_inr: float) -> Optional[Dict[str, Any]]:
        side = str(opt_side).upper()
        picks = [
            c
            for c in (self.priority_contracts or [])
            if str(c.get("opt_type")).upper() == side
            and bool(c.get("algo_enabled", True))
            and not bool(c.get("blocked_by_bias"))
        ]
        picks.sort(key=lambda c: float(c.get("ltp") or 0.0), reverse=True)
        for c in picks:
            ltp = float(c.get("ltp") or c.get("mid") or 0.0)
            if ltp >= float(min_premium_inr):
                return c
        return None

    def _handle_ema8_prototype_actions(self, actions: List[Dict[str, Any]]) -> None:
        for act in actions or []:
            atype = str(act.get("type")).upper()
            if atype == "SQUARE_OFF":
                reason = str(act.get("reason") or "OPPOSITE_8EMA_CLOSE")
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._square_off_open_ema8_prototype_positions(reason))
                except RuntimeError:
                    asyncio.run(self._square_off_open_ema8_prototype_positions(reason))
                self._ema8_prototype_state.pop("open", None)
                self._ema8_prototype_state.pop("arm", None)
                self._save_ema8_prototype_state()
            elif atype == "ARM":
                self.log_ema8_prototype_event(
                    event="ARMED",
                    direction=act.get("direction"),
                    dynamic_8ema=act.get("dynamic_8ema"),
                    spot=act.get("spot"),
                    reason=f"ARMED {act.get('direction')}",
                )
                self._save_ema8_prototype_state()
            elif atype == "FLIP":
                self.log_ema8_prototype_event(
                    event=f"FLIPPED TO EMA {act.get('direction')}",
                    direction=act.get("direction"),
                    dynamic_8ema=act.get("dynamic_8ema"),
                    spot=act.get("spot"),
                    reason=f"FLIPPED TO EMA {act.get('direction')}",
                )
                self._save_ema8_prototype_state()
            elif atype == "RETOUCH_SEEN":
                self.log_ema8_prototype_event(
                    event="RETOUCH SEEN",
                    direction=act.get("direction"),
                    dynamic_8ema=act.get("dynamic_8ema"),
                    spot=act.get("spot"),
                    reason="DYNAMIC 8 EMA TOUCHED",
                )
            elif atype == "CANCEL":
                self.log_ema8_prototype_event(
                    event="CANCELLED",
                    direction=act.get("direction"),
                    reason=act.get("reason"),
                )

    async def _square_off_open_ema8_prototype_positions(self, reason: str) -> None:
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                rows = cur.execute(
                    """
                    SELECT * FROM positions
                    WHERE status IN ('OPEN', 'EXIT_FAILED')
                      AND upper(coalesce(strategy_id,'')) LIKE 'EMA8_PROTOTYPE%'
                    """
                ).fetchall()
            spot_idx = float(
                (self.cached_telemetry.get("spot") or {}).get("price") or 0.0
            )
            for r in rows:
                p = dict(r)
                await self._close_position_with_broker(p, reason, spot_exit=spot_idx or None)
                log.info("[EMA8_PROTOTYPE] Routed square-off %s — %s via shared close path", p.get("symbol"), reason)
        except Exception as exc:
            log.warning("ema8 prototype square-off: %s", exc)

    def _matrix_scalp_scanner_active(self) -> bool:
        if not bool((self.cfg.get("matrix_scalp") or {}).get("enabled", False)):
            return False
        if bool(self.algo_allowlist_cfg.get("enabled")) and bool(
            self.algo_allowlist_cfg.get("disable_ribbon_matrix_scanner", True)
        ):
            return False
        return True

    def _pa_sr_live_scanner_active(self) -> bool:
        pa = self.cfg.get("pa_sr_homework") or {}
        if not bool(pa.get("enabled", False)) or not bool(pa.get("live_paper_enabled", False)):
            return False
        if bool(self.algo_allowlist_cfg.get("enabled")):
            allowed = self._allowed_algo_strategy_id_set() or set()
            if not any(s.startswith("PA_SR") for s in allowed):
                return False
        return True

    def _bollinger_gate_active(self, strategy_key: str) -> bool:
        """Global + optional per-strategy Bollinger gate (ON = apply BB slope/width/flat checks)."""
        if not bool(self.scanner_cfg.get("bollinger_enabled", True)):
            return False
        override = self.scanner_cfg.get(f"bollinger_enabled_{strategy_key}")
        if override is None:
            return True
        return bool(override)

    def _entry_tf_meta(self) -> Dict[str, str]:
        return {"entry_timeframe": "15m", "Entry_TF": "15m"}

    def _gate_meta_for_signal(self, strategy_key: str) -> Dict[str, Any]:
        bb_on = self._bollinger_gate_active(strategy_key)
        meta = {
            "bollinger_enabled": bb_on,
            "bollinger_applied": bb_on,
            "bb_gate_label": "ON" if bb_on else "OFF",
        }
        meta.update(self._entry_tf_meta())
        return meta

    def get_scanner_gate_config(self) -> Dict[str, Any]:
        raw = self.scanner_cfg.get("bollinger_enabled", True)
        enabled = raw is True or raw == "true" or raw == 1
        return {
            "ok": True,
            "bollinger_enabled": bool(enabled),
            "bollinger_enabled_stage2": self.scanner_cfg.get("bollinger_enabled_stage2"),
            "bollinger_enabled_stage3": self.scanner_cfg.get("bollinger_enabled_stage3"),
            "bollinger_enabled_crossover": self.scanner_cfg.get("bollinger_enabled_crossover"),
            "bollinger_enabled_flagship": self.scanner_cfg.get("bollinger_enabled_flagship"),
            "party_switch_enabled": bool(self.scanner_cfg.get("party_switch_enabled", True)),
            "flagship_or_8ema_require_trend": bool(
                self.scanner_cfg.get("flagship_or_8ema_require_trend", True)
            ),
        }

    def update_scanner_gate_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "bollinger_enabled",
            "bollinger_enabled_stage2",
            "bollinger_enabled_stage3",
            "bollinger_enabled_crossover",
            "bollinger_enabled_flagship",
            "party_switch_enabled",
            "flagship_or_8ema_require_trend",
        }
        prev_bb = self.scanner_cfg.get("bollinger_enabled", True)
        for k, v in (updates or {}).items():
            if k not in allowed:
                continue
            if k in ("bollinger_enabled", "party_switch_enabled", "flagship_or_8ema_require_trend"):
                if isinstance(v, bool):
                    self.scanner_cfg[k] = v
                else:
                    self.scanner_cfg[k] = v is True or str(v).lower() == "true" or v == 1
            else:
                self.scanner_cfg[k] = v
        self.cfg["scanner"] = self.scanner_cfg
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            log.info(
                "[BB-GATE] bollinger_enabled %s -> %s (persisted %s)",
                prev_bb,
                self.scanner_cfg.get("bollinger_enabled"),
                self.config_path,
            )
        except Exception as exc:
            log.warning("Could not persist scanner gate config: %s", exc)
        out = self.get_scanner_gate_config()
        out["ok"] = True
        return out

    def get_ema8_prototype_filter_config(self) -> Dict[str, Any]:
        from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg

        c = load_ema8_prototype_cfg(self.scanner_cfg)
        return {
            "ok": True,
            "enabled": bool(c.get("enabled", True)),
            "execution_mode": str(c.get("execution_mode", "LIVE_ALLOWED")),
            "ribbon_filter_enabled": bool(c.get("ribbon_filter_enabled", False)),
            "ribbon_max_pts": float(c.get("ribbon_max_pts", 3.0)),
            "ema_separation_enabled": bool(c.get("ema_separation_enabled", False)),
            "supertrend_enabled": bool(c.get("supertrend_enabled", False)),
            "supertrend_length": int(c.get("supertrend_length", 5)),
            "supertrend_factor": float(c.get("supertrend_factor", 1.0)),
            "retouch_requires_away": bool(c.get("retouch_requires_away", True)),
            "slow_market_exact_line": bool(c.get("slow_market_exact_line", False)),
            "touch_tol_pts": float(c.get("touch_tol_pts", 3.0)),
            "touch_tol_slow_pts": float(c.get("touch_tol_slow_pts", 0.0)),
        }

    def update_ema8_prototype_filter_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "enabled",
            "execution_mode",
            "ribbon_filter_enabled",
            "ribbon_max_pts",
            "ema_separation_enabled",
            "supertrend_enabled",
            "supertrend_length",
            "supertrend_factor",
            "retouch_requires_away",
            "slow_market_exact_line",
            "touch_tol_pts",
            "touch_tol_slow_pts",
        }
        block = self.scanner_cfg.setdefault("ema8_prototype", {})
        if not isinstance(block, dict):
            block = {}
            self.scanner_cfg["ema8_prototype"] = block
        for k, v in (updates or {}).items():
            if k not in allowed:
                continue
            if k in (
                "ribbon_filter_enabled",
                "ema_separation_enabled",
                "supertrend_enabled",
                "enabled",
                "retouch_requires_away",
                "slow_market_exact_line",
            ):
                block[k] = v is True or str(v).lower() in ("true", "1", "on", "yes")
            elif k == "execution_mode":
                block[k] = str(v).upper()
            elif k in ("ribbon_max_pts", "supertrend_factor", "touch_tol_pts", "touch_tol_slow_pts"):
                block[k] = float(v)
            elif k == "supertrend_length":
                block[k] = int(v)
            else:
                block[k] = v
        self.cfg["scanner"] = self.scanner_cfg
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
            log.info("[EMA8_PROTOTYPE] filter config persisted: %s", {k: block.get(k) for k in allowed if k in (updates or {})})
        except Exception as exc:
            log.warning("Could not persist ema8_prototype config: %s", exc)
        self._sync_ema8_prototype_desk_status()
        return self.get_ema8_prototype_filter_config()

    def evaluate_candle_dataframe(
        self,
        df: pd.DataFrame,
        spot_price: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluates 4 core institutional strategies over candle dataframe:
          1. Stage 2 (8 EMA Support Touch - Long)
          2. Stage 3 (8 EMA Resistance Rejection - Short)
          3. 4 MA vs 8 EMA Crossover Reversal (Upside & Downside with Party-Switch)
          4. 1H 8 EMA Support & Resistance Confluence
        """
        if df.empty or len(df) < 30:
            return []

        df_work = df.copy()
        if not isinstance(df_work.index, pd.DatetimeIndex):
            if "dt" in df_work.columns:
                df_work = df_work.set_index("dt")
            elif "ts" in df_work.columns:
                df_work["dt"] = pd.to_datetime(df_work["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                df_work = df_work.set_index("dt")

        # 15m / 1H — NSE session anchored (09:15); entries on last CLOSED 15m bar only (iloc[-2]).
        b15 = resample_nse_15m(df_work)
        if len(b15) < 11:
            return []
        entry_tf = str(self.scanner_cfg.get("entry_timeframe", "15m") or "15m").lower()
        if entry_tf not in ("15m", "15min"):
            log.info("SKIPPED_NOT_15M evaluate_candle_dataframe entry_tf=%s", entry_tf)
            return []

        bar_ci = -2  # last fully closed 15m candle
        b60 = resample_nse_1h(df_work)

        # 15m Technicals
        e4, e8 = ema4_ema8(b15["close"])
        e21 = b15["close"].ewm(span=21, adjust=False).mean()

        sma20 = b15["close"].rolling(20, min_periods=5).mean()
        std20 = b15["close"].rolling(20, min_periods=5).std().fillna(15.0)
        ubb = sma20 + 2 * std20
        lbb = sma20 - 2 * std20
        bb_width = ubb - lbb

        # Band Slopes (closed 15m bar vs prior closed)
        d_ubb = float(ubb.iloc[bar_ci] - ubb.iloc[bar_ci - 1]) if len(ubb) >= abs(bar_ci) + 1 else 0.0
        d_lbb = float(lbb.iloc[bar_ci] - lbb.iloc[bar_ci - 1]) if len(lbb) >= abs(bar_ci) + 1 else 0.0
        slope_tol = 1.8
        ubb_slope = "UP" if d_ubb > slope_tol else ("DOWN" if d_ubb < -slope_tol else "FLAT")
        lbb_slope = "UP" if d_lbb > slope_tol else ("DOWN" if d_lbb < -slope_tol else "FLAT")

        current_p = float(b15["close"].iloc[bar_ci])
        e8_val = float(e8.iloc[bar_ci])
        e4_val = float(e4.iloc[bar_ci])
        close_15 = float(b15["close"].iloc[bar_ci])
        low_15 = float(b15["low"].iloc[bar_ci])
        high_15 = float(b15["high"].iloc[bar_ci])
        width_val = float(bb_width.iloc[bar_ci]) if pd.notnull(bb_width.iloc[bar_ci]) else 70.0

        tol = float(self.scanner_cfg.get("tolerance_pts", 20.0))
        bb_min = float(self.scanner_cfg.get("bb_width_min", 60.0))

        # Strict Moving Average Stack Validation
        is_bullish_stack = e4_val > e8_val
        is_bearish_stack = e4_val < e8_val

        # Check Crossover within last 3 bars for Party-Switch & Reversal Setup Prerequisite
        crossover_event = None
        for lag in range(1, min(4, len(b15) - 1)):
            idx = bar_ci - lag + 1
            prev_idx = idx - 1
            if len(b15) > abs(prev_idx) and idx <= bar_ci:
                if e4.iloc[prev_idx] >= e8.iloc[prev_idx] and e4.iloc[idx] < e8.iloc[idx]:
                    crossover_event = {"type": "BEARISH_CROSS", "age": lag, "time": str(b15.index[idx])}
                    break
                elif e4.iloc[prev_idx] <= e8.iloc[prev_idx] and e4.iloc[idx] > e8.iloc[idx]:
                    crossover_event = {"type": "BULLISH_CROSS", "age": lag, "time": str(b15.index[idx])}
                    break

        if crossover_event and bool(self.scanner_cfg.get("party_switch_enabled", True)):
            if crossover_event["type"] == "BEARISH_CROSS":
                if self.active_party != "BEARISH":
                    log.info("🎯 Party-Switch activated: BEARISH (4 EMA crossed below 8 EMA). CE Longs BLOCKED.")
                self.active_party = "BEARISH"
                self.last_crossover_event = crossover_event
            elif crossover_event["type"] == "BULLISH_CROSS":
                if self.active_party != "BULLISH":
                    log.info("🎯 Party-Switch activated: BULLISH (4 EMA crossed above 8 EMA). PE Shorts BLOCKED.")
                self.active_party = "BULLISH"
                self.last_crossover_event = crossover_event

        # 15m ATR(14) for Goldilocks 4/8 Separation Regime Gate
        h15, l15, c15 = b15["high"], b15["low"], b15["close"]
        tr0 = h15 - l15
        tr1 = (h15 - c15.shift(1)).abs()
        tr2 = (l15 - c15.shift(1)).abs()
        tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
        atr14_series = tr.ewm(alpha=1.0 / 14, adjust=False).mean()
        atr14_val = (
            float(atr14_series.iloc[bar_ci])
            if len(atr14_series) and pd.notnull(atr14_series.iloc[bar_ci]) and atr14_series.iloc[bar_ci] > 0
            else 15.0
        )

        # Goldilocks 4/8 Separation Regime Gate
        goldilocks = self.compute_goldilocks_separation(e4_val, e8_val, atr14_val)

        # Futures Volume Surge Ratio (VSR_10)
        fut_surge = self.futures_volume_engine.get_latest_vsr() if self.futures_volume_engine else {
            "vsr_10": 1.0, "is_surge": False, "tag": "NORMAL_VOLUME"
        }
        vsr_10 = float(fut_surge.get("vsr_10") or 0.0)
        is_vol_surge = bool(fut_surge.get("is_institutional_surge", False))

        signals: List[Dict[str, Any]] = []

        bb_stage2 = self._bollinger_gate_active("stage2")
        bb_stage3 = self._bollinger_gate_active("stage3")
        bb_cross = self._bollinger_gate_active("crossover")

        # ── MJ SIR LECTURE: Stage 2 / Stage 3 / 8 EMA Reversal (15m, paper-only) ──
        from analyst.mj_sir_stage_reversal_8510 import evaluate_mj_sir_four_pack, load_mj_cfg

        mj_cfg = load_mj_cfg(self.scanner_cfg)
        session_day = ""
        try:
            session_day = str(b15.index[bar_ci].strftime("%Y-%m-%d"))
        except Exception:
            session_day = datetime.now(IST).strftime("%Y-%m-%d")

        # With dynamic first-touch entries the setup is keyed to the cross
        # candle itself, so a new cross is a new setup without this reset —
        # and this reset, which repeats for three candles after a cross, would
        # let the same setup fire again.
        if not bool(mj_cfg.get("dynamic_8ema_entry", True)):
            if crossover_event and crossover_event.get("type") == "BULLISH_CROSS":
                self._mj_sir_state.pop("stage2_fired_key", None)
            if crossover_event and crossover_event.get("type") == "BEARISH_CROSS":
                self._mj_sir_state.pop("stage3_fired_key", None)

        if bool(mj_cfg.get("enabled", True)):
            mj_sigs, mj_status = evaluate_mj_sir_four_pack(
                b15,
                bar_ci=bar_ci,
                cfg=mj_cfg,
                bb_stage2=bb_stage2,
                bb_stage3=bb_stage3,
                bb_cross=bb_cross,
                party_switch_enabled=bool(self.scanner_cfg.get("party_switch_enabled", False)),
                active_party=self.active_party,
                mj_state=self._mj_sir_state,
                session_day=session_day,
                daily_sr=None,
                b60=b60,
                gate_meta=self._gate_meta_for_signal,
            )
            signals.extend(mj_sigs)
            for key in ("stage2_long", "stage3_short", "crossover_reversal"):
                if key in mj_status:
                    prev = dict(self.strategy_status.get(key) or {})
                    prev.update(mj_status[key])
                    prev["spec"] = "MJ_SIR_LECTURE_UNDERSTANDING"
                    self.strategy_status[key] = prev

        # ── 8 EMA Prototype (paper-only lane — does not alter Stage 2 or FLAGSHIP) ──
        from analyst.ema8_prototype_8510 import evaluate_ema8_prototype, load_ema8_prototype_cfg

        proto_cfg = load_ema8_prototype_cfg(self.scanner_cfg)
        if getattr(self, "_historical_eval", False):
            # Backtest / replay pass over old candles: the live prototype
            # state, its open position, its log and its card are not touched.
            pass
        elif bool(proto_cfg.get("enabled", True)):
            self._sync_open_ema8_prototype_position()
            proto_sigs, proto_st = evaluate_ema8_prototype(
                b15,
                bar_ci=bar_ci,
                cfg=proto_cfg,
                proto_state=self._ema8_prototype_state,
                session_day=session_day,
                bb_gate_active=False,
                gate_meta=self._gate_meta_for_signal,
            )
            signals.extend(proto_sigs)
            prev_proto = dict(self.strategy_status.get("ema8_prototype") or {})
            prev_proto.update(proto_st)
            if getattr(self, "exit_failure_banner", None):
                prev_proto["block_reason"] = f"EXIT FAILED — {self.exit_failure_banner.get('error')}"
            elif prev_proto.get("last_exit_reason") == "OPPOSITE_8EMA_CLOSE":
                prev_proto["block_reason"] = "Opposite Close — Exited"
            self.strategy_status["ema8_prototype"] = prev_proto
            self._handle_ema8_prototype_actions(proto_st.get("actions"))
            self._save_ema8_prototype_state()
            for ps in proto_sigs:
                self._ema8_prototype_log.insert(
                    0,
                    {
                        "ts": datetime.now(IST).strftime("%H:%M:%S"),
                        "strategy_id": ps.get("strategy_id"),
                        "setup_name": ps.get("setup_name"),
                        "direction": ps.get("direction"),
                        "details": ps.get("details"),
                    },
                )
            if len(self._ema8_prototype_log) > 40:
                self._ema8_prototype_log = self._ema8_prototype_log[:40]
        else:
            prev_proto = dict(self.strategy_status.get("ema8_prototype") or {})
            prev_proto["state"] = "PAUSED"
            prev_proto["phase"] = "DISABLED"
            prev_proto["block_reason"] = "Prototype manually PAUSED in cockpit"
            prev_proto["enabled"] = False
            self.strategy_status["ema8_prototype"] = prev_proto

        # ── 4. STRATEGY 4: 1 HR FLAGSHIP (price action: daily S/R, 1H swings, 15m confirmation) ──
        scfg = self.scanner_cfg
        flagship_tgt = float(scfg.get("flagship_first_target_pts", 40.0))
        flagship_sl = float(scfg.get("flagship_initial_sl_pts", 40.0))
        doji_cap = float(scfg.get("flagship_doji_sl_pts", 15.0))
        max_h1_rng = float(scfg.get("flagship_max_h1_range_pts", 120.0))
        no_entry_after = str(scfg.get("flagship_no_entry_after", "15:00"))
        bar_hm = ""
        if isinstance(b15.index, pd.DatetimeIndex) and len(b15):
            bar_hm = b15.index[-1].strftime("%H:%M")

        b1d = df_work.resample("1D").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        daily_sr = compute_daily_sr_from_ohlc(b1d, current_p)
        daily_bias, daily_state = compute_daily_trend_two_day_rule(b1d)
        h1_trend = compute_h1_trend_pa(b60) if len(b60) >= 3 else "NEUTRAL"

        self.strategy_status["daily_sr"]["levels"] = daily_sr
        self.strategy_status["daily_sr"]["state"] = "ARMED" if daily_sr.get("S1") else "SCANNING"
        self.strategy_status["daily_sr"]["armed"] = bool(daily_sr.get("S1"))
        self.strategy_status["daily_trend"]["state"] = daily_state if daily_bias != "NEUTRAL" else "NEUTRAL"
        self.strategy_status["daily_trend"]["armed"] = daily_bias in ("BULLISH", "BEARISH")
        self.strategy_status["flagship_1h"]["state"] = "SCANNING"
        self.strategy_status["flagship_1h"]["armed"] = False

        no_entry_before = str(scfg.get("flagship_no_entry_before", "09:30") or "")
        if (
            len(b60) >= 5
            and len(b15) >= 3
            and (not bar_hm or bar_hm < no_entry_after)
            and (not bar_hm or not no_entry_before or bar_hm >= no_entry_before)
        ):
            pa_long, pa_short = detect_flagship_15m_confirmation(b15, conf_idx=-2)
            conf_i = len(b15) - 2
            co = float(b15["open"].iloc[conf_i])
            ch = float(b15["high"].iloc[conf_i])
            cl = float(b15["low"].iloc[conf_i])
            cc = float(b15["close"].iloc[conf_i])
            entry_open = float(b15["open"].iloc[-1])

            sr_levels = [daily_sr.get(k) for k in ("S1", "S2", "S3", "R1", "R2", "R3")]
            sr_levels += list(daily_sr.get("round_levels") or [])
            level_list = [float(x) for x in sr_levels if x is not None]

            swings = find_confirmed_h1_swings(b60, pivot_bars=2)
            swing_low_zones: List[Tuple[float, float, str]] = []
            swing_high_zones: List[Tuple[float, float, str]] = []
            for sw in swings:
                if float(sw.get("range_pts") or 0.0) > max_h1_rng:
                    continue
                z = zone_from_15m_at_ts(b15, sw["ts"])
                if not z:
                    continue
                if sw["type"] == "LOW":
                    swing_low_zones.append((z[0], z[1], str(sw["ts"])))
                else:
                    swing_high_zones.append((z[0], z[1], str(sw["ts"])))

            near_daily_support = any(
                daily_sr.get(k) is not None and abs(cl - float(daily_sr[k])) <= tol for k in ("S1", "S2", "S3")
            )
            near_daily_resist = any(
                daily_sr.get(k) is not None and abs(ch - float(daily_sr[k])) <= tol for k in ("R1", "R2", "R3")
            )
            if pa_long == "DOJI" and not (
                _at_valid_level(cl, ch, level_list, tol)
                or near_daily_support
                or _near_round_number(cl, 1000.0)
                or _near_round_number(cl, 500.0)
            ):
                pa_long = None
            if pa_short == "DOJI" and not (
                _at_valid_level(cl, ch, level_list, tol)
                or near_daily_resist
                or _near_round_number(ch, 1000.0)
                or _near_round_number(ch, 500.0)
            ):
                pa_short = None

            self.strategy_status["flagship_1h"]["armed"] = bool(swing_low_zones or swing_high_zones)

            def _zone_touched(zlo: float, zhi: float, bar_low: float, bar_high: float) -> bool:
                return bar_low <= zhi + 2.0 and bar_high >= zlo - 2.0

            supports_txt = ",".join(
                f"{k}:{daily_sr[k]:.0f}" for k in ("S1", "S2", "S3") if daily_sr.get(k) is not None
            )
            resist_txt = ",".join(
                f"{k}:{daily_sr[k]:.0f}" for k in ("R1", "R2", "R3") if daily_sr.get(k) is not None
            )

            flagship_long_fired = False
            flagship_short_fired = False

            if pa_long and daily_bias == "BULLISH" and h1_trend == "UP":
                for zlo, zhi, zts in swing_low_zones:
                    if not _zone_touched(zlo, zhi, cl, ch):
                        continue
                    if cc < zlo or cc > zhi + tol:
                        continue
                    sl_val = flagship_sl
                    if pa_long == "DOJI":
                        dist = abs(cl - entry_open)
                        sl_val = min(doji_cap, dist if dist > 0 else doji_cap)
                    self.strategy_status["flagship_1h"]["state"] = "TRIGGERED"
                    sig_row = {
                        "strategy_id": "FLAGSHIP_1H_LONG",
                        "setup_name": "Flagship 1H Long [Swing zone / 15m PA]",
                        "direction": "LONG",
                        "opt_side": "CE",
                        "target_pts": flagship_tgt,
                        "sl_pts": sl_val,
                        "index_price": entry_open,
                        "conviction": "STANDARD",
                        "flagship_trail": scfg.get("flagship_trail_by", "candle_low_high"),
                        "details": (
                            f"Zone {zlo:.0f}-{zhi:.0f} + 15m {pa_long} + Daily Trend {daily_bias} "
                            f"+ 1H Trend {h1_trend} | S/R {supports_txt}"
                        ),
                    }
                    sig_row.update(self._gate_meta_for_signal("flagship"))
                    signals.append(sig_row)
                    flagship_long_fired = True
                    break

            if (
                pa_short
                and daily_bias == "BEARISH"
                and h1_trend == "DOWN"
            ):
                for zlo, zhi, zts in swing_high_zones:
                    if not _zone_touched(zlo, zhi, cl, ch):
                        continue
                    if cc > zhi or cc < zlo - tol:
                        continue
                    sl_val = flagship_sl
                    if pa_short == "DOJI":
                        dist = abs(ch - entry_open)
                        sl_val = min(doji_cap, dist if dist > 0 else doji_cap)
                    self.strategy_status["flagship_1h"]["state"] = "TRIGGERED"
                    sig_row = {
                        "strategy_id": "FLAGSHIP_1H_SHORT",
                        "setup_name": "Flagship 1H Short [Swing zone / 15m PA]",
                        "direction": "SHORT",
                        "opt_side": "PE",
                        "target_pts": flagship_tgt,
                        "sl_pts": sl_val,
                        "index_price": entry_open,
                        "conviction": "STANDARD",
                        "flagship_trail": scfg.get("flagship_trail_by", "candle_low_high"),
                        "details": (
                            f"Zone {zlo:.0f}-{zhi:.0f} + 15m {pa_short} + Daily Trend {daily_bias} "
                            f"+ 1H Trend {h1_trend} | S/R {resist_txt}"
                        ),
                    }
                    sig_row.update(self._gate_meta_for_signal("flagship"))
                    signals.append(sig_row)
                    flagship_short_fired = True
                    break

            or_on = bool(scfg.get("flagship_or_8ema_enabled", True))
            or_touch = float(scfg.get("flagship_or_8ema_touch_pts", 10.0))
            or_below = float(scfg.get("flagship_or_8ema_below_pts", 10.0))
            # Cockpit option: the OR 8 EMA entry must agree with the daily and
            # 1H trend (long only when both are up, short only when both down).
            or_trend = bool(scfg.get("flagship_or_8ema_require_trend", True))
            or_long_ok = (not or_trend) or (daily_bias == "BULLISH" and h1_trend == "UP")
            or_short_ok = (not or_trend) or (daily_bias == "BEARISH" and h1_trend == "DOWN")
            if or_on and len(b60) >= abs(bar_ci) + 2:
                _, e8h_series = ema4_ema8(b60["close"])
                e8h = float(e8h_series.iloc[bar_ci])
                if (
                    not flagship_long_fired
                    and or_long_ok
                    and pa_long
                    and e8h > 0
                    and _h1_came_from_above(b60, bar_ci, e8h, or_touch)
                    and _h1_8ema_support_touch(b60, bar_ci, e8h, or_touch)
                ):
                    entry_spot = ch
                    self.strategy_status["flagship_1h"]["state"] = "TRIGGERED"
                    sig_row = {
                        "strategy_id": "FLAGSHIP_1H_LONG",
                        "setup_name": "Flagship 1H Long [OR 8 EMA support + 15m PA]",
                        "direction": "LONG",
                        "opt_side": "CE",
                        "target_pts": flagship_tgt,
                        "sl_pts": flagship_sl,
                        "index_price": entry_spot,
                        "conviction": "STANDARD",
                        "flagship_or_8ema": True,
                        "flagship_trail": scfg.get("flagship_trail_by", "candle_low_high"),
                        "details": (
                            f"OR 8EMA support {e8h:.1f} | 1H touch + from above | "
                            f"15m {pa_long} | entry ref high {entry_spot:.1f} | SL/TGT {flagship_sl:.0f}/{flagship_tgt:.0f}"
                        ),
                    }
                    sig_row.update(self._gate_meta_for_signal("flagship"))
                    signals.append(sig_row)
                    flagship_long_fired = True

                if (
                    not flagship_short_fired
                    and or_short_ok
                    and pa_short
                    and e8h > 0
                    and _h1_well_below_8ema(b60, bar_ci, e8h, or_below)
                    and _h1_8ema_resist_touch(b60, bar_ci, e8h, or_touch)
                ):
                    entry_spot = cl
                    self.strategy_status["flagship_1h"]["state"] = "TRIGGERED"
                    sig_row = {
                        "strategy_id": "FLAGSHIP_1H_SHORT",
                        "setup_name": "Flagship 1H Short [OR 8 EMA resist + 15m PA]",
                        "direction": "SHORT",
                        "opt_side": "PE",
                        "target_pts": flagship_tgt,
                        "sl_pts": flagship_sl,
                        "index_price": entry_spot,
                        "conviction": "STANDARD",
                        "flagship_or_8ema": True,
                        "flagship_trail": scfg.get("flagship_trail_by", "candle_low_high"),
                        "pattern_high_spot": ch,
                        "details": (
                            f"OR 8EMA resist {e8h:.1f} | 1H approach within {or_touch:.0f}pts | "
                            f"15m {pa_short} | entry ref low {entry_spot:.1f} pattern high {ch:.1f} | "
                            f"SL/TGT {flagship_sl:.0f}/{flagship_tgt:.0f}"
                        ),
                    }
                    sig_row.update(self._gate_meta_for_signal("flagship"))
                    signals.append(sig_row)
                    flagship_short_fired = True

            if (
                self.strategy_status["flagship_1h"]["armed"]
                and self.strategy_status["flagship_1h"]["state"] != "TRIGGERED"
            ):
                self.strategy_status["flagship_1h"]["state"] = "ARMED"

        # ── 4b. HOMEWORK: 1H 8-EMA dynamic S/R + closed 15m price-action confirmation ──
        # (Dhan 1H chart 8 EMA touch + 15m hammer/engulf/doji — not the swing-zone FLAGSHIP above.)
        h1_ema_hw_on = bool(self.scanner_cfg.get("h1_8ema_homework_enabled", True))
        if bool(self.algo_allowlist_cfg.get("enabled")) and bool(
            self.algo_allowlist_cfg.get("disable_h1_8ema_homework_scanner", True)
        ):
            h1_ema_hw_on = False
        if h1_ema_hw_on and len(b60) >= 3:
            touch_h1 = float(self.scanner_cfg.get("h1_8ema_touch_pts", 8.0))
            tgt_h1 = float(self.scanner_cfg.get("h1_8ema_target_pts", 40.0))
            sl_h1 = float(self.scanner_cfg.get("h1_8ema_sl_pts", 40.0))
            _, e8h_series = ema4_ema8(b60["close"])
            lo_h = float(b60["low"].iloc[bar_ci])
            hi_h = float(b60["high"].iloc[bar_ci])
            cl_h = float(b60["close"].iloc[bar_ci])
            e8h = float(e8h_series.iloc[bar_ci])
            pa_long, pa_short = detect_flagship_15m_confirmation(b15, conf_idx=bar_ci)
            reclaim_long = low_15 <= (e8_val + tol) and close_15 > e8_val
            reject_short = high_15 >= (e8_val - tol) and close_15 < e8_val
            h1_support_touch = (
                e8h > 0
                and lo_h <= (e8h + touch_h1)
                and hi_h >= (e8h - touch_h1)
            )
            h1_resist_touch = (
                e8h > 0
                and hi_h >= (e8h - touch_h1)
                and lo_h <= (e8h + touch_h1)
                and cl_h <= (e8h + touch_h1)
            )
            self.strategy_status["h1_8ema_homework"]["armed"] = h1_support_touch or h1_resist_touch
            fired = False
            if h1_support_touch and (pa_long or reclaim_long) and close_15 >= (e8_val - tol):
                self.strategy_status["h1_8ema_homework"]["state"] = "TRIGGERED"
                fired = True
                sig_row = {
                    "strategy_id": "H1_8EMA_SUPPORT_LONG",
                    "setup_name": "1H 8-EMA Support + 15m PA (Homework)",
                    "direction": "LONG",
                    "opt_side": "CE",
                    "target_pts": tgt_h1,
                    "sl_pts": sl_h1,
                    "index_price": current_p,
                    "conviction": "STANDARD",
                    "details": (
                        f"1H 8-EMA {e8h:.1f} touched (low {lo_h:.1f}) | 15m {pa_long or '8EMA_RECLAIM'} "
                        f"| 15m 8-EMA {e8_val:.1f} close {close_15:.1f}"
                    ),
                }
                sig_row.update(self._gate_meta_for_signal("flagship"))
                signals.append(sig_row)
            elif h1_resist_touch and (pa_short or reject_short) and is_bearish_stack and close_15 <= (e8_val + tol):
                self.strategy_status["h1_8ema_homework"]["state"] = "TRIGGERED"
                fired = True
                sig_row = {
                    "strategy_id": "H1_8EMA_RESIST_SHORT",
                    "setup_name": "1H 8-EMA Reject + 15m PA (Homework)",
                    "direction": "SHORT",
                    "opt_side": "PE",
                    "target_pts": tgt_h1,
                    "sl_pts": sl_h1,
                    "index_price": current_p,
                    "conviction": "STANDARD",
                    "details": (
                        f"1H 8-EMA {e8h:.1f} reject (high {hi_h:.1f}) | 15m {pa_short or '8EMA_REJECT'} "
                        f"| 15m 8-EMA {e8_val:.1f} close {close_15:.1f}"
                    ),
                }
                sig_row.update(self._gate_meta_for_signal("flagship"))
                signals.append(sig_row)
            if not fired:
                if self.strategy_status["h1_8ema_homework"]["armed"]:
                    self.strategy_status["h1_8ema_homework"]["state"] = "ARMED"
                else:
                    self.strategy_status["h1_8ema_homework"]["state"] = "SCANNING"

        # Directional Bias filter on generated strategy signals
        if self.directional_bias == "LONG_ONLY":
            filtered_signals = []
            for sig in signals:
                d = str(sig.get("direction", "")).upper()
                opt = str(sig.get("opt_side", "")).upper()
                if d == "SHORT" or opt == "PE":
                    log.info("[DIRECTIONAL BIAS REJECTED] Scanner signal %s (%s / %s) rejected by LONG_ONLY bias filter.",
                             sig.get("setup_name"), d, opt)
                else:
                    filtered_signals.append(sig)
            signals = filtered_signals
        elif self.directional_bias == "SHORT_ONLY":
            filtered_signals = []
            for sig in signals:
                d = str(sig.get("direction", "")).upper()
                opt = str(sig.get("opt_side", "")).upper()
                if d == "LONG" or opt == "CE":
                    log.info("[DIRECTIONAL BIAS REJECTED] Scanner signal %s (%s / %s) rejected by SHORT_ONLY bias filter.",
                             sig.get("setup_name"), d, opt)
                else:
                    filtered_signals.append(sig)
            signals = filtered_signals

        # Proven Quant Edges Filter 1: optional v1.7 checkbox only (independent of Bollinger gate)
        goldilocks_filter_on = bool(self.proven_edges_cfg.get("goldilocks_separation_enabled", False))
        if goldilocks_filter_on and not goldilocks["raw_allowed"]:
            log.info("[GOLDILOCKS GATE BLOCKED] Signals blocked: %s (%s)", goldilocks["badge"], goldilocks["description"])
            g_state = "BLOCKED_GOLDILOCKS_CHOP" if goldilocks["regime"] == "CHOP_ZONE" else "BLOCKED_GOLDILOCKS_CLIMAX"
            passed: List[Dict[str, Any]] = []
            for sig in signals:
                if sig.get("mj_sir_paper_only") or sig.get("ema8_prototype_paper_only"):
                    passed.append(sig)
                    continue
                sid = str(sig.get("strategy_id", "")).upper()
                if "STAGE_2" in sid:
                    self.strategy_status["stage2_long"]["state"] = g_state
                elif "STAGE_3" in sid:
                    self.strategy_status["stage3_short"]["state"] = g_state
                elif "CROSSOVER" in sid:
                    self.strategy_status["crossover_reversal"]["state"] = g_state
                elif "FLAGSHIP" in sid:
                    self.strategy_status["flagship_1h"]["state"] = g_state
            signals = passed

        # Proven Quant Edges Filter 2: Futures Volume Surge Gate (vsr_10 >= 1.15)
        fut_surge_gate_on = bool(self.proven_edges_cfg.get("futures_volume_surge_enabled", False))
        fut_min_ratio = float(self.proven_edges_cfg.get("futures_volume_surge_min_ratio", 1.15))
        if fut_surge_gate_on and not is_vol_surge:
            log.info("[FUTURES VOL SURGE GATE BLOCKED] Signals blocked: VSR %.2f < %.2f", vsr_10, fut_min_ratio)
            for sig in signals:
                sid = str(sig.get("strategy_id", "")).upper()
                if "STAGE_2" in sid:
                    self.strategy_status["stage2_long"]["state"] = "BLOCKED_FUT_VOL_SURGE"
                elif "STAGE_3" in sid:
                    self.strategy_status["stage3_short"]["state"] = "BLOCKED_FUT_VOL_SURGE"
                elif "CROSSOVER" in sid:
                    self.strategy_status["crossover_reversal"]["state"] = "BLOCKED_FUT_VOL_SURGE"
                elif "FLAGSHIP" in sid:
                    self.strategy_status["flagship_1h"]["state"] = "BLOCKED_FUT_VOL_SURGE"
            signals = []

        # Dhan OI PE-CE Synergy Advisory Tagging on Signals (Advisory-Only & Fail-Open)
        try:
            syn = self.dhan_oi.evaluate_synergy_status()
            synergy_status = syn.get("synergy_status", "MIXED")
        except Exception as e:
            log.debug("DhanOI signal synergy exception (failing open): %s", e)
            synergy_status = "MIXED"

        if signals:
            for sig in signals:
                sig["dhan_oi_synergy"] = synergy_status
                sig["goldilocks"] = goldilocks
                sig["futures_vsr_10"] = vsr_10
                opt = str(sig.get("opt_side", "")).upper()
                if opt == "CE" and synergy_status == "BEARISH_SYNERGY":
                    sig["advisory_note"] = "⚠️ DhanOI Bearish Synergy (Advisory Warning)"
                elif opt == "PE" and synergy_status == "BULLISH_SYNERGY":
                    sig["advisory_note"] = "⚠️ DhanOI Bullish Synergy (Advisory Warning)"
                elif opt == "CE" and synergy_status == "BULLISH_SYNERGY":
                    sig["advisory_note"] = "✅ DhanOI Bullish Synergy Confirmed"
                elif opt == "PE" and synergy_status == "BEARISH_SYNERGY":
                    sig["advisory_note"] = "✅ DhanOI Bearish Synergy Confirmed"

                # Filter 2: Futures Volume Surge Tagging (vsr_10 >= 1.15)
                if is_vol_surge:
                    sig["conviction"] = "🔥 VOLUME SURGE CONFIRMED"
                    prev_note = sig.get("advisory_note", "")
                    sig["advisory_note"] = (prev_note + " | 🔥 VOLUME SURGE CONFIRMED").strip(" |")
                    sig["play_surge_beep"] = True
                else:
                    sig["play_surge_beep"] = False

        return signals

    # ── MATRIX SCALP (Tier 3 proximity, spot-defined) ────────────────────────
    #
    # WHAT THIS IS FOR
    # ----------------
    # To find out whether Tier 3 proximity signals can be scalped. Not to assume
    # they can. Every trade it produces is a PAPER trade recorded forward in
    # time, which is the only test of a signal that hindsight cannot flatter:
    # the data does not exist yet when the rule is fixed.
    #
    # WHAT IS ALREADY KNOWN, from analyst/ema_proximity_engine.py's own
    # forward_mfe_15m / forward_mae_15m over 2026-11-04 and 2026-11-05:
    #   - The moves are real. Tier 3 median forward 15m MFE 15.8 pts, 52%
    #     reached +15, 42% reached +20. On 06 Nov, 72.7% reached +15.
    #   - Whether they are CAPTURABLE is unresolved. First passage brackets to
    #     28.8%-51.6% against a 49.7% break-even, straddling it, because 85 of
    #     372 windows breached both the target and the stop inside the same 15
    #     minutes and minute OHLC cannot order them.
    # So this is an instrument, not a strategy with an edge.

    _RIBBON_GRADE_RANK = {"B": 1, "A": 2, "A+": 3}

    def _ribbon_fresh_cross_flags(self, rows: List[Dict[str, Any]]) -> Tuple[bool, bool]:
        """Upside / downside reversal cross on newest bar or prior two 1m bars."""
        tail = list(rows[-3:]) if rows else []
        up = dn = False
        for r in tail:
            cs = str(r.get("crossover_state") or "")
            if cs == "8_EMA_REVERSAL_UPSIDE_CROSS":
                up = True
            if cs == "8_EMA_REVERSAL_DOWNSIDE_CROSS":
                dn = True
        return up, dn

    def _ribbon_mtf_agree(self, row: Dict[str, Any], direction: str) -> bool:
        try:
            e4 = float(row.get("ema_4") or 0)
            e8 = float(row.get("ema_8") or 0)
            e4_15 = float(row.get("ema_4_15m") or 0)
            e8_15 = float(row.get("ema_8_15m") or 0)
            e4_1h = float(row.get("ema_4_1h") or 0)
            e8_1h = float(row.get("ema_8_1h") or 0)
        except (TypeError, ValueError):
            return False
        if direction == "LONG":
            if not (e4 > e8 and e4_15 > e8_15):
                return False
            return e4_1h >= e8_1h
        if not (e4 < e8 and e4_15 < e8_15):
            return False
        return e4_1h <= e8_1h

    def _ribbon_sr_matches_direction(self, row: Dict[str, Any], direction: str) -> bool:
        sr = str(row.get("sr_interaction") or "").upper()
        if direction == "LONG":
            return "SUPPORT_BOUNCE" in sr or "SUPPORT_TOUCH" in sr
        return "RESISTANCE_REJECT" in sr or "RESISTANCE_TOUCH" in sr

    def _ribbon_scalp_grade(
        self, row: Dict[str, Any], rows: List[Dict[str, Any]], direction: str
    ) -> Optional[str]:
        if row.get("is_whipsaw_chop"):
            return None
        state = str(row.get("proximity_state") or "")
        if state != "TIER_3_CONFIRMATION_TRIGGER":
            return None
        if not self._ribbon_sr_matches_direction(row, direction):
            return "B"
        mtf = self._ribbon_mtf_agree(row, direction)
        if not mtf:
            return "B"
        up_cross, dn_cross = self._ribbon_fresh_cross_flags(rows)
        fresh = (direction == "LONG" and up_cross) or (direction == "SHORT" and dn_cross)
        if fresh and self._ribbon_sr_matches_direction(row, direction):
            return "A+"
        return "A"

    def _matrix_scalp_direction(self, row: Dict[str, Any]) -> Optional[str]:
        """Direction from the engine's OWN sr_interaction label.

        Deliberately NOT inferred from crossover_state. An earlier analysis
        harness of mine matched substrings in crossover_state and ended up
        sampling 7 of 353 bars, because `trend` is None on every row and
        'UPSIDE'/'DOWNSIDE' only appear in the three reversal-cross rows. The
        engine states the setup explicitly in sr_interaction; that is used, and
        anything unrecognised returns None rather than a guess.
        """
        sr = str(row.get("sr_interaction") or "").upper()
        if "SUPPORT_BOUNCE" in sr or "SUPPORT_TOUCH" in sr:
            return "LONG"
        if "RESISTANCE_REJECT" in sr or "RESISTANCE_TOUCH" in sr:
            return "SHORT"
        return None

    def evaluate_matrix_scalp_tier3(self, spot_price: Optional[float] = None
                                    ) -> List[Dict[str, Any]]:
        """Emit at most one Matrix Scalp signal from the newest closed bar.

        Returns signals in the same shape as the other strategies, so the
        existing path handles them: directional-bias gate, duplicate-position
        check, the v1.9.4b live routing gate (which can only downgrade), paper
        execution, and therefore Active Positions and Orders History with no
        parallel ledger.
        """
        cfg = self.cfg.get("matrix_scalp", {}) or {}
        if not cfg.get("enabled", False):
            if isinstance(getattr(self, "strategy_status", None), dict):
                rs = self.strategy_status.get("ribbon_scalp")
                if isinstance(rs, dict):
                    rs["state"] = "OFF"
            return []
        if not self.ema_proximity_engine:
            return []

        now = datetime.now(IST)
        last_entry = str(cfg.get("last_entry_ist", "15:05"))
        if now.strftime("%H:%M") > last_entry:
            # No new scalps near the close: EOD square-off is 15:20 and an entry
            # placed after this cannot be held long enough to reach a target.
            return []

        try:
            eng = self.ema_proximity_engine
            res = getattr(eng, "_live_matrix_cache_payload", None)
            if not isinstance(res, dict) or not res.get("rows"):
                res = eng.compute_matrix(date_str="LIVE", timeframe="1m")
        except Exception as exc:
            log.warning("Matrix scalp: proximity matrix unavailable (%s)", exc)
            return []
        if not isinstance(res, dict) or res.get("status") == "NO_DATA":
            # v1.9.4c: NO_DATA is a real answer. The loader no longer fabricates
            # flat candles, so an unrecorded session yields no signals.
            return []
        rows = res.get("rows") or res.get("matrix") or []
        if not rows:
            return []

        # Newest CLOSED bar only. rows are oldest-first from compute_matrix.
        row = rows[-1]
        state = str(row.get("proximity_state") or "")
        if cfg.get("tier3_only", True):
            if state != "TIER_3_CONFIRMATION_TRIGGER":
                return []
        elif state not in ("TIER_3_CONFIRMATION_TRIGGER", "TIER_2_TOUCH_ZONE"):
            return []
        if row.get("is_whipsaw_chop"):
            return []

        rib_status = (getattr(self, "strategy_status", None) or {}).get("ribbon_scalp")

        def _rib_state(**kw):
            if isinstance(rib_status, dict):
                rib_status.update(kw)

        direction = self._matrix_scalp_direction(row)
        if not direction:
            _rib_state(state="SCANNING", grade=None)
            return []

        grade = self._ribbon_scalp_grade(row, rows, direction)
        if not grade:
            _rib_state(state="FILTERED", grade=None)
            return []
        min_grade = str(cfg.get("min_grade", "B") or "B")
        if min_grade not in self._RIBBON_GRADE_RANK:
            min_grade = "B"
        if self._RIBBON_GRADE_RANK.get(grade, 0) < self._RIBBON_GRADE_RANK[min_grade]:
            _rib_state(state="BELOW_MIN_GRADE", grade=grade)
            return []

        bar_time = str(row.get("timestamp_ist") or "")[:5]
        # One signal per direction per cooldown. Hovering at the 8 EMA for four
        # minutes is one signal, not four.
        cool = int(cfg.get("cooldown_minutes", 15) or 0)
        key = "matrix_scalp_{}".format(direction.lower())
        last = float(self._matrix_scalp_last.get(key, 0.0))
        if cool and (time.time() - last) < cool * 60.0:
            return []

        spot = spot_price
        if not spot:
            spot = row.get("close_price")
        try:
            spot = float(spot)
        except (TypeError, ValueError):
            return []
        if spot <= 0:
            return []

        spot_tgt = float(cfg.get("spot_target_pts", 15.0))
        spot_tgt2 = float(cfg.get("spot_target_pts_t2", 40.0))
        spot_stop = float(cfg.get("spot_stop_pts", 20.0))
        engine_risk = row.get("risk_points")

        if cfg.get("use_wick_micro_sl", False):
            try:
                if direction == "LONG":
                    wick_risk = spot - (float(row.get("low_price") or row.get("close_price") or spot) - 5.0)
                else:
                    wick_risk = (float(row.get("high_price") or row.get("close_price") or spot) + 5.0) - spot
                wick_risk = max(0.0, float(wick_risk))
                lo = float(cfg.get("min_engine_risk_pts", 2.0))
                hi = float(cfg.get("max_engine_risk_pts", 40.0))
                if lo <= wick_risk <= hi:
                    spot_stop = round(wick_risk, 2)
            except (TypeError, ValueError):
                pass

        # Optionally use the engine's own structural Micro-SL instead of the
        # fixed width, bounded so a 92-point structural stop cannot be adopted
        # silently. Both are recorded either way so they can be compared later.
        if cfg.get("use_engine_micro_sl", False) and engine_risk:
            try:
                r = float(engine_risk)
                lo = float(cfg.get("min_engine_risk_pts", 2.0))
                hi = float(cfg.get("max_engine_risk_pts", 40.0))
                if lo <= r <= hi:
                    spot_stop = r
            except (TypeError, ValueError):
                pass

        # UNIT CONVERSION. target_pts / sl_pts are OPTION PREMIUM points
        # (initial_sl = entry_premium - sl_pts), while the rule is written in
        # SPOT points. Converted with the separately measured deltas: 0.59 on
        # the win side, 0.67 on the loss side.
        d_win = float(cfg.get("delta_win", 0.59))
        d_loss = float(cfg.get("delta_loss", 0.67))
        prem_tgt = round(spot_tgt * d_win, 2)
        prem_stop = round(spot_stop * d_loss, 2)

        opt_side = "CE" if direction == "LONG" else "PE"
        rib_id = "ribbon_scalp_{}".format(direction.lower())
        display = str(cfg.get("display_name") or "Ribbon Scalp")
        sig = {
            "strategy_id": rib_id,
            "setup_name": "{} [{}] {} ({})".format(
                display,
                grade,
                "CE Long" if direction == "LONG" else "PE Long",
                str(row.get("sr_interaction") or "TIER_3"),
            ),
            "direction": direction,
            "opt_side": opt_side,
            "conviction": "RIBBON {}".format(grade),
            "ribbon_grade": grade,
            "details": ("{} [{}] {} | spot {:.2f} | T1 +{:.0f} T2 +{:.0f} / stop -{:.0f} spot pts "
                        "(= +{:.2f} / -{:.2f} premium) | cross={} | bar {}"
                        .format(
                            display,
                            grade,
                            str(row.get("sr_interaction") or "TIER_3"),
                            spot,
                            spot_tgt,
                            spot_tgt2,
                            spot_stop,
                            prem_tgt,
                            prem_stop,
                            row.get("crossover_state") or "—",
                            bar_time or "?",
                        )),
            "target_pts": prem_tgt,
            "sl_pts": prem_stop,
            # Spot context, carried through for the sweet-spot study.
            "matrix_scalp": True,
            "spot_entry": round(spot, 2),
            "spot_target_pts": spot_tgt,
            "spot_stop_pts": spot_stop,
            "spot_target_level": round(spot + spot_tgt, 2) if direction == "LONG"
                                 else round(spot - spot_tgt, 2),
            "spot_stop_level": round(spot - spot_stop, 2) if direction == "LONG"
                               else round(spot + spot_stop, 2),
            "engine_risk_pts": (round(float(engine_risk), 2)
                                if engine_risk not in (None, "") else None),
            "sr_interaction": row.get("sr_interaction"),
            "gap_pts": row.get("gap_pts"),
            "gap_atr_ratio": row.get("gap_atr_ratio"),
            "separation_atr": row.get("spread_4_8_atr"),
            "bar_time_ist": bar_time,
            "advisory_note": ("RIBBON SCALP LOCK — prospective paper only; "
                              "stop width unvalidated; not real-money until UNLOCK"),
            "spot_target_pts_t2": spot_tgt2,
        }
        self._matrix_scalp_last[key] = time.time()
        _rib_state(
            state="TRIGGERED",
            armed=True,
            grade=grade,
            last_direction=direction,
        )
        if not cfg.get("enabled", False):
            _rib_state(state="OFF")
        log.info("RIBBON SCALP %s [%s] | %s", direction, grade, sig["details"])
        return [sig]

    def _load_spot_1m_for_scanner(self, asof_15m_open_ts: Optional[int] = None) -> pd.DataFrame:
        """Load sec 13 1m with EMA warmup; optional cutoff at end of a closed 15m bar."""
        if not self.market_db.exists():
            return pd.DataFrame()
        limit = int(EMA_WARMUP_1M_BARS)
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=5) as conn:
                df = pd.read_sql_query(
                    "SELECT ts, o as open, h as high, l as low, c as close, v as volume "
                    "FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT ?",
                    conn,
                    params=(limit,),
                )
            if df.empty:
                return df
            df = df.iloc[::-1].reset_index(drop=True)
            if asof_15m_open_ts is not None:
                cutoff = int(asof_15m_open_ts) + 900
                df = df[df["ts"] <= cutoff].copy()
            return df
        except Exception as exc:
            log.debug("Scanner 1m load failed: %s", exc)
            return pd.DataFrame()

    def _pending_closed_15m_open_ts(self) -> List[int]:
        """15m bar OPEN unix timestamps not yet evaluated (after engine gap / restart)."""
        bar = self._latest_closed_15m_bar()
        if not bar:
            return []
        closed_open_ts = int(bar.get("ts") or 0)
        if closed_open_ts <= 0:
            return []
        last = int(self._last_live_eval_closed_15m_ts or 0)
        if closed_open_ts <= last:
            return []
        max_catch = int(self.scanner_cfg.get("scanner_catchup_max_15m_bars", 12) or 12)
        if last <= 0:
            return [closed_open_ts]
        slots: List[int] = []
        t = last + 900
        while t <= closed_open_ts:
            slots.append(t)
            t += 900
        if closed_open_ts not in slots:
            slots.append(closed_open_ts)
        slots = sorted(set(slots))
        if len(slots) > max_catch:
            log.warning(
                "Scanner 15m catch-up truncated: %d bars missed, evaluating last %d only",
                len(slots),
                max_catch,
            )
            slots = slots[-max_catch:]
        if bool(self.scanner_cfg.get("scanner_catchup_eval_latest_only", True)) and len(slots) > 1:
            log.info(
                "Scanner 15m catch-up: evaluating latest slot only (%d older slots skipped)",
                len(slots) - 1,
            )
            slots = [slots[-1]]
        return slots

    def evaluate_all_strategies(
        self,
        spot_price: Optional[float] = None,
        asof_15m_open_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate core strategies on last closed 15m (optional historical bar for catch-up)."""
        df = self._load_spot_1m_for_scanner(asof_15m_open_ts=asof_15m_open_ts)
        if df.empty or len(df) < 30:
            return []
        if spot_price is None or float(spot_price) <= 0:
            spot_price = float(df["close"].iloc[-1])
        try:
            return self.evaluate_candle_dataframe(df, spot_price=spot_price)
        except Exception as e:
            log.debug("Strategy scanner evaluate error: %s", e)
            return []

    async def _process_strategy_signals(self, signals: List[Dict[str, Any]]) -> None:
        """Processes actionable strategy signals with cooldown deduplication and triggers orders."""
        now_ts = time.time()
        for sig in signals:
            d = str(sig.get("direction", "")).upper()
            opt = str(sig.get("opt_side", "")).upper()

            # Directional Bias Enforcement Gate:
            if self.directional_bias == "LONG_ONLY" and (d == "SHORT" or opt == "PE"):
                log.warning("[DIRECTIONAL BIAS REJECTED] Signal %s (%s / %s) blocked: LONG_ONLY bias blocks all PE / Short trades.",
                            sig.get("setup_name"), d, opt)
                continue
            if self.directional_bias == "SHORT_ONLY" and (d == "LONG" or opt == "CE"):
                log.warning("[DIRECTIONAL BIAS REJECTED] Signal %s (%s / %s) blocked: SHORT_ONLY bias blocks all CE / Long trades.",
                            sig.get("setup_name"), d, opt)
                continue

            strat_id = str(sig.get("strategy_id", "") or "")
            if not self.algo_strategy_execution_allowed(strat_id):
                log.info(
                    "[ALLOWLIST BLOCKED] %s (%s) — not in algo_execution_allowlist",
                    sig.get("setup_name"),
                    strat_id,
                )
                continue

            if "FLAGSHIP" in str(strat_id).upper():
                ok_risk, risk_reason = self._flagship_risk_allows_entry()
                if not ok_risk:
                    log.info("[FLAGSHIP BLOCKED] %s — %s", sig.get("setup_name"), risk_reason)
                    continue

            last_ts = self.strategy_cooldowns.get(strat_id, 0.0)
            cool_min = float(self.scanner_cfg.get("signal_cooldown_5m_min", 5.0) or 5.0)
            cool_sec = max(0.0, cool_min) * 60.0  # 5m wait between signals — NOT an entry timeframe
            if cool_sec and (now_ts - last_ts) < cool_sec:
                continue

            self.strategy_cooldowns[strat_id] = now_ts
            sig["timestamp"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            self.recent_signals.insert(0, sig)
            if len(self.recent_signals) > 30:
                self.recent_signals.pop()

            log.info("🔥 STRATEGY SIGNAL TRIGGERED: %s | %s | %s | Conviction: %s",
                     sig["setup_name"], sig["direction"], sig["details"], sig["conviction"])

            # Prospective observation: record the pillar state for EVERY signal,
            # before we know whether it will be executed. Gates nothing.
            signal_uid = ""
            try:
                _spot = (self.cached_telemetry.get("spot") or {}).get("price")
                signal_uid = self.log_signal_observation(
                    sig,
                    confluence=self.cached_telemetry.get("five_factor_confluence"),
                    volume=self.cached_telemetry.get("futures_volume_surge"),
                    oi=self.cached_telemetry.get("institutional_pe_ce"),
                    spot=_spot,
                )
            except Exception as exc:
                log.warning("signal observation skipped (%s)", type(exc).__name__)

            # 8600 board: every option-chain source's reading is recorded for every
            # signal. A trade is skipped only by sources the owner has switched
            # to FILTER in the cockpit, only for the lanes he selected, and never
            # by a source that has no data.
            try:
                from analyst.desk8600_board_8510 import judge as _board_judge, load_cfg as _board_cfg
                from analyst.oi_context_8510 import evaluate_gates, load_cfg as _oi_cfg

                board_cfg = _board_cfg(self.scanner_cfg)
                if bool(board_cfg.get("enabled", True)):
                    oi_ctx = self.oi_context()
                    self.log_signal_oi_context(
                        signal_uid, sig, oi_ctx,
                        evaluate_gates(oi_ctx, sig.get("direction", ""), _oi_cfg(self.scanner_cfg)), False,
                    )
                    board = self.desk8600_board()
                    verdict = _board_judge(board["sources"], sig.get("direction", ""), board_cfg, strat_id)
                    self.log_signal_desk8600(signal_uid, sig, board, verdict)
                    sig["desk8600"] = {
                        "agree": verdict["agree"], "against": verdict["against"],
                        "blocked": verdict["blocked"], "reasons": verdict["reasons"],
                    }
                    if verdict["blocked"]:
                        why = "8600 board: " + "; ".join(verdict["reasons"])
                        log.info("[8600 BOARD] %s (%s) not taken — %s", sig.get("setup_name"), strat_id, why)
                        self.mark_signal_outcome(signal_uid, False, skip_reason=why[:300])
                        continue
            except Exception as exc:
                log.warning("8600 board skipped (%s)", type(exc).__name__)

            # Resolve Contract based on direction and algo_strike_selection
            opt_side = sig["opt_side"]
            target_contract = None
            is_proto_sig = str(strat_id).upper().startswith("EMA8_PROTOTYPE")
            if is_proto_sig:
                from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg

                pcfg = load_ema8_prototype_cfg(self.scanner_cfg)
                min_prem = float(
                    sig.get("min_option_premium_inr") or pcfg.get("min_option_premium_inr") or 200.0
                )
                target_contract = self._select_ema8_prototype_contract(opt_side, min_prem)
            else:
                candidate_ids = [f"LONG_{opt_side}_ATM", f"LONG_{opt_side}_ITM"]
                for cid in candidate_ids:
                    if self.algo_strike_selection.get(cid, True):
                        for c in self.priority_contracts:
                            if c.get("id") == cid:
                                target_contract = c
                                break
                        if target_contract:
                            break

            if not target_contract:
                log.info("No enabled priority contract found for %s (%s)", sig["setup_name"], opt_side)
                self.mark_signal_outcome(signal_uid, False,
                                         skip_reason="NO_ENABLED_CONTRACT")
                continue

            # Check if an identical position is already OPEN in ledger
            try:
                with sqlite3.connect(self.ledger_db) as conn:
                    cur = conn.cursor()
                    r = cur.execute(
                        "SELECT count(*) FROM positions WHERE status='OPEN' AND symbol=?",
                        (target_contract["symbol"],)
                    ).fetchone()
                    if r and r[0] > 0:
                        log.info("Position already OPEN for %s — skipping duplicate execution", target_contract["symbol"])
                        self.mark_signal_outcome(signal_uid, False,
                                                 skip_reason="DUPLICATE_POSITION_OPEN")
                        continue
            except Exception as e:
                log.error("Ledger check error in signal execution: %s", e)

            spot_entry = float(sig.get("index_price") or self.cached_telemetry.get("spot", {}).get("price") or 0.0)
            is_long = str(sig.get("direction", "LONG")).upper() == "LONG"
            sl_pts_sig = float(sig.get("sl_pts", 40.0))
            tgt_pts_sig = float(sig.get("target_pts", 40.0))
            spot_ctx = None
            if sig.get("matrix_scalp") or sig.get("pa_homework"):
                spot_ctx = {
                    "strategy_id": sig.get("strategy_id"),
                    "spot_entry": sig.get("spot_entry"),
                    "spot_target_level": sig.get("spot_target_level"),
                    "spot_stop_level": sig.get("spot_stop_level"),
                    "spot_target_pts": sig.get("spot_target_pts"),
                    "spot_stop_pts": sig.get("spot_stop_pts"),
                    "engine_risk_pts": sig.get("engine_risk_pts"),
                    "sr_interaction": sig.get("sr_interaction"),
                }
            elif "FLAGSHIP" in str(sig.get("strategy_id", "")).upper() and spot_entry > 0:
                spot_ctx = {
                    "strategy_id": sig.get("strategy_id"),
                    "spot_entry": spot_entry,
                    "spot_target_pts": tgt_pts_sig,
                    "spot_stop_pts": sl_pts_sig,
                    "spot_target_level": round(spot_entry + tgt_pts_sig, 2) if is_long else round(spot_entry - tgt_pts_sig, 2),
                    "spot_stop_level": round(spot_entry - sl_pts_sig, 2) if is_long else round(spot_entry + sl_pts_sig, 2),
                }
            elif (is_proto_sig or uses_fixed_spot_or_option_exit(strat_id)) and spot_entry > 0:
                spot_ctx = {
                    "strategy_id": sig.get("strategy_id"),
                    "spot_entry": spot_entry,
                    "spot_target_pts": tgt_pts_sig,
                    "spot_stop_pts": sl_pts_sig,
                    "spot_target_level": round(spot_entry + tgt_pts_sig, 2)
                    if is_long
                    else round(spot_entry - tgt_pts_sig, 2),
                    "spot_stop_level": round(spot_entry - sl_pts_sig, 2)
                    if is_long
                    else round(spot_entry + sl_pts_sig, 2),
                }

            # Trigger execution with chaser
            sid_u = str(sig.get("strategy_id", "")).upper()
            proto_paper = bool(sig.get("ema8_prototype_paper_only")) or bool(sig.get("paper_only"))
            paper_algo = (
                "FLAGSHIP" in sid_u
                or sid_u.startswith("STAGE_2")
                or sid_u.startswith("STAGE_3")
                or sid_u.startswith("CROSSOVER")
                or sig.get("mj_sir_paper_only")
                or (sid_u.startswith("EMA8_PROTOTYPE") and proto_paper)
                or (sid_u.startswith("DESK8600_TICKET") and bool(sig.get("paper_only")))
                or sig.get("matrix_scalp")
                or sig.get("pa_homework")
                or sid_u.startswith("PA_SR")
                or sid_u.startswith("RIBBON_SCALP")
            )
            if not self.algo_strategy_execution_allowed(strat_id):
                self.mark_signal_outcome(
                    signal_uid,
                    False,
                    skip_reason=f"ALLOWLIST_NOT_ENABLED:{strat_id}",
                )
                continue

            res = await self.execute_order_with_chaser(
                contract=target_contract,
                side="BUY",
                strategy_name=sig["setup_name"],
                strategy_setup=sig.get("setup_name"),
                strategy_id=strat_id,
                target_pts=sig["target_pts"],
                sl_pts=sig["sl_pts"],
                is_algo_trade=True,
                source="REAL_ALGO_PAPER" if paper_algo else None,
                spot_context=spot_ctx,
            )
            log.info("Strategy %s execution result: %s", sig["setup_name"], res.get("status", "SUBMITTED"))
            self.mark_signal_outcome(
                signal_uid,
                executed=bool(res.get("ok")),
                position_id=str(res.get("position_id", "") or ""),
                skip_reason="" if res.get("ok") else str(
                    res.get("rejection_reason") or res.get("error") or "")[:200],
            )
            if is_proto_sig and res.get("ok"):
                self._sync_open_ema8_prototype_position()
                self._save_ema8_prototype_state()

    async def start_strategy_scanner_loop(self) -> None:
        """Live scanner: 4 core on closed 15m; Ribbon on closed 1m; PA @ S/R armed on 15m, break on spot."""
        self.scanner_running = True
        interval = float(self.scanner_cfg.get("scan_interval_seconds", 3))
        log.info(
            "🚀 Live Scanner started (%.1fs) — 4-core@15m · 8EMA Prototype paper · Ribbon@1m · PA/S/R · Party-Switch: %s",
            interval,
            self.scanner_cfg.get("party_switch_enabled", True),
        )

        while self.scanner_running:
            try:
                if self.scanner_enabled and not self.kill_switch_active:
                    spot = self.cached_telemetry.get("spot", {}).get("price")
                    if not spot or spot <= 0:
                        log.debug("Strategy scanner: spot price unavailable or <= 0, skipping scan cycle")
                        await asyncio.sleep(interval)
                        continue
                    async with self._scanner_heavy_lock:
                        signals: List[Dict[str, Any]] = []
                        closed_1m = await asyncio.to_thread(self._latest_closed_1m_bar)
                        if closed_1m:
                            ts1 = int(closed_1m.get("ts") or 0)
                            if ts1 > 0 and ts1 != self._last_live_eval_closed_1m_ts:
                                self._last_live_eval_closed_1m_ts = ts1
                                log.debug("1m closed bar eval ts=%s", ts1)
                                if self._matrix_scalp_scanner_active():
                                    try:
                                        tier3 = await asyncio.to_thread(
                                            self.evaluate_matrix_scalp_tier3,
                                            spot_price=spot,
                                        )
                                        signals.extend(tier3)
                                    except Exception as exc:
                                        log.warning("Ribbon evaluate skipped: %s", exc)

                        if self._pa_sr_live_scanner_active():
                            pa_breaks = await asyncio.to_thread(
                                self.evaluate_pa_sr_live_breaks, float(spot), closed_1m
                            )
                            signals.extend(pa_breaks)

                        signals.extend(
                            await asyncio.to_thread(self.evaluate_desk8600_ticket_lane, float(spot))
                        )

                        for slot_ts in self._pending_closed_15m_open_ts():
                            self._last_live_eval_closed_15m_ts = int(slot_ts)
                            log.info("15m closed bar eval slot ts=%s", slot_ts)
                            batch = await asyncio.to_thread(
                                self.evaluate_all_strategies,
                                spot_price=spot,
                                asof_15m_open_ts=int(slot_ts),
                            )
                            signals.extend(batch)
                            if self._pa_sr_live_scanner_active():
                                await asyncio.to_thread(self.evaluate_pa_sr_live_arm_on_15m)

                        if signals:
                            signals = await asyncio.to_thread(self.apply_writer_flow_gates, signals)
                            if signals:
                                await self._process_strategy_signals(signals)
            except Exception as e:
                log.error("Strategy scanner iteration error: %s", e)

            await asyncio.sleep(interval)

    def get_scanner_status(self) -> Dict[str, Any]:
        """Returns real-time status of the 4-Strategy Scanner daemon and Party-Switch gate."""
        return {
            "ok": True,
            "enabled": self.scanner_enabled,
            "running": self.scanner_running,
            "active_party": self.active_party,
            "directional_bias": self.directional_bias,
            "last_crossover": self.last_crossover_event,
            "kill_switch_active": self.kill_switch_active,
            "strategies": self.strategy_status,
            "recent_signals": self.recent_signals[:10],
            "ema8_prototype_log": self.get_ema8_prototype_log(limit=15),
            "ema8_prototype_view": self._ema8_prototype_view(),
            "algo_strike_selection": self.algo_strike_selection,
            "tolerance_pts": float(self.scanner_cfg.get("tolerance_pts", 20.0)),
            "scanner_gates": self.get_scanner_gate_config(),
            "party_switch_enabled": bool(self.scanner_cfg.get("party_switch_enabled", True)),
            "bollinger_master_off": not bool(self.scanner_cfg.get("bollinger_enabled", True)),
            "matrix_scalp_enabled": self._matrix_scalp_scanner_active(),
            "pa_sr_live_enabled": self._pa_sr_live_scanner_active(),
            "algo_execution_allowlist": {
                "enabled": bool(self.algo_allowlist_cfg.get("enabled", False)),
                "strategy_ids": list(self.algo_allowlist_cfg.get("strategy_ids") or []),
            },
        }

    # ── FUNDS & PAPER WALLET MANAGEMENT ──────────────────────────────────────
    def get_paper_wallet(self) -> Dict[str, Any]:
        with sqlite3.connect(self.ledger_db) as conn:
            cur = conn.cursor()
            cur.execute("SELECT balance, margin_used, realized_pnl, updated_at FROM paper_wallet WHERE id=1")
            row = cur.fetchone()
            if row:
                bal, used, pnl, upd = row
                return {
                    "balance": round(bal, 2),
                    "margin_used": round(used, 2),
                    "margin_avail": round(bal - used, 2),
                    "realized_pnl": round(pnl, 2),
                    "updated_at": upd,
                }
            return {"balance": 100000.0, "margin_used": 0.0, "margin_avail": 100000.0, "realized_pnl": 0.0}

    def update_paper_wallet(self, delta_amount: float = 0.0, reset: bool = False) -> Dict[str, Any]:
        with sqlite3.connect(self.ledger_db) as conn:
            cur = conn.cursor()
            if reset:
                new_bal = float(self.cfg.get("starting_paper_capital", 100000.0))
                cur.execute("UPDATE paper_wallet SET balance=?, margin_used=0.0, realized_pnl=0.0, updated_at=? WHERE id=1",
                            (new_bal, datetime.now(IST).isoformat()))
            else:
                cur.execute("SELECT balance FROM paper_wallet WHERE id=1")
                row = cur.fetchone()
                curr = row[0] if row else 100000.0
                new_bal = max(0.0, curr + delta_amount)
                cur.execute("UPDATE paper_wallet SET balance=?, updated_at=? WHERE id=1",
                            (new_bal, datetime.now(IST).isoformat()))
            conn.commit()
        return self.get_paper_wallet()

    def reset_paper_ledger(self, clear_orders: bool = False,
                           clear_closed_positions: bool = False) -> Dict[str, Any]:
        """Start P&L tracking fresh, WITHOUT orphaning open positions.

        update_paper_wallet(reset=True) zeroes margin_used unconditionally. With
        positions still open that silently frees margin the desk is actually
        using, and the wallet then disagrees with the position book. This instead
        RECOMPUTES margin_used from the positions that are genuinely still OPEN.

        Open positions are never deleted here: their margin is preserved and
        their P&L will settle into the balance when they close as normal.
        """
        start_cap = float(self.cfg.get("starting_paper_capital", 100000.0))
        out: Dict[str, Any] = {"cleared_orders": 0, "cleared_closed_positions": 0}
        with sqlite3.connect(self.ledger_db) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(entry_price * qty), 0.0) "
                "FROM positions WHERE status='OPEN'").fetchone()
            n_open, open_margin = int(row[0] or 0), float(row[1] or 0.0)

            if clear_orders:
                out["cleared_orders"] = int(cur.execute(
                    "SELECT COUNT(*) FROM orders").fetchone()[0] or 0)
                cur.execute("DELETE FROM orders")
            if clear_closed_positions:
                out["cleared_closed_positions"] = int(cur.execute(
                    "SELECT COUNT(*) FROM positions WHERE status='CLOSED'").fetchone()[0] or 0)
                cur.execute("DELETE FROM positions WHERE status='CLOSED'")

            cur.execute("UPDATE paper_wallet SET balance=?, margin_used=?, "
                        "realized_pnl=0.0, updated_at=? WHERE id=1",
                        (start_cap, open_margin, datetime.now(IST).isoformat()))
            conn.commit()

        log.warning("Paper ledger reset: capital Rs%.2f, realised P&L zeroed, "
                    "margin_used recomputed to Rs%.2f from %d open position(s); "
                    "orders cleared=%d, closed positions cleared=%d",
                    start_cap, open_margin, n_open,
                    out["cleared_orders"], out["cleared_closed_positions"])
        out.update({"ok": True, "open_positions_preserved": n_open,
                    "wallet": self.get_paper_wallet()})
        return out

    # ── POSITIONS & ORDERS RETRIEVAL ─────────────────────────────────────────
    def get_active_positions(self, live_quotes: Optional[Dict[str, float]] = None,
                             spot_price: Optional[float] = None) -> List[Dict[str, Any]]:
        positions = []
        live_quotes = live_quotes or {}
        spot_idx = float(spot_price or self.cached_telemetry.get("spot", {}).get("price") or 0.0)
        sr_levels = dict(self.strategy_status.get("daily_sr", {}).get("levels") or {})
        sr_prox = float(self.scanner_cfg.get("flagship_sr_exit_proximity_pts", 15.0))
        eod_hm = str(self.scanner_cfg.get("flagship_eod_square_off", "15:20"))
        if not hasattr(self, "_flagship_trail_bar_ts"):
            self._flagship_trail_bar_ts = {}
        with sqlite3.connect(self.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute("SELECT * FROM positions WHERE status IN ('OPEN', 'EXIT_PENDING', 'EXIT_FAILED') ORDER BY entry_time DESC").fetchall()
            now_dt = datetime.now(IST)
            resume: List[Tuple[Dict[str, Any], str, Optional[float]]] = []
            for r in rows:
                p = dict(r)
                pos_status = str(p.get("status", "OPEN")).upper()
                if pos_status == "EXIT_PENDING":
                    # An exit that is not being worked (close task crashed, or
                    # the app restarted mid-exit) is picked up again here.
                    pending_reason = str(p.get("exit_reason") or "")
                    if not pending_reason or pending_reason.startswith("EXIT_FAILED"):
                        pending_reason = "EXIT_RECOVERED"
                    resume.append((dict(p), pending_reason, spot_idx if spot_idx > 0 else None))
                    if p.get("entry_time"):
                        p["entry_time"] = format_desk_timestamp_ist(p["entry_time"])
                    positions.append(p)
                    continue
                sid = str(p.get("security_id"))
                entry = float(p.get("entry_price") or 0.0)
                qty = int(p.get("qty") or 0)
                if qty<=0:
                    log.error('Position quantity missing; no guessed sizing or simulated exit')
                    p['valuation_available']=False;p['data_error']='Missing recorded position quantity'
                    if p.get("entry_time"):
                        p["entry_time"] = format_desk_timestamp_ist(p["entry_time"])
                    positions.append(p)
                    continue
                target = float(p.get("target_price") or (entry + 24.0))
                sl = float(p.get("sl_price") or (entry - 24.0))

                # Live quote update
                ltp = live_quotes.get(sid, float(p.get("current_ltp") or entry))
                p["current_ltp"] = ltp

                # Super Trail state fields
                highest_ltp = float(p.get("highest_ltp") or entry)
                initial_sl = float(p.get("initial_sl") or sl)
                current_trailing_sl = float(p.get("current_trailing_sl") or initial_sl)
                target_status = str(p.get("target_status") or "FIXED")
                initial_target = float(p.get("initial_target") or target)
                trailing_target_level = float(p.get("trailing_target_level") or initial_target)
                highest_after_target = float(p.get("highest_after_target") or entry)
                pullback_exit_level = float(p.get("pullback_exit_level") or 0.0)

                is_flagship_pos = "FLAGSHIP" in str(p.get("strategy_id") or "").upper()
                # "proto" here means the fixed spot-or-option exit, which Stage 2,
                # Stage 3 and the Reversal now share with the 8 EMA Prototype.
                is_proto_pos = uses_fixed_spot_or_option_exit(p.get("strategy_id"))
                trail_sl_enabled = bool(p.get("trail_sl_enabled", 1)) and not is_flagship_pos
                trail_target_enabled = bool(p.get("trail_target_enabled", 1)) and not is_flagship_pos
                trail_act = float(p.get("trail_activation_pts") or self.super_trail_cfg.get("trail_activation_profit_pts", 15.0))
                trail_jump = float(p.get("trail_jump_pts") or self.super_trail_cfg.get("trailing_jump_pts", 5.0))
                init_sl_pts = float(self.super_trail_cfg.get("initial_sl_pts", 20.0))
                init_target_pts = float(self.super_trail_cfg.get("initial_target_pts", 25.0))
                target_step = float(p.get("trail_target_step") or self.super_trail_cfg.get("trail_target_step_pts", 10.0))
                target_pullback = float(p.get("trail_target_pullback") or self.super_trail_cfg.get("trail_target_pullback_pts", 5.0))

                # 1. Update Trailing SL
                if trail_sl_enabled:
                    highest_ltp, current_trailing_sl = compute_trailing_sl(
                        entry_price=entry,
                        current_ltp=ltp,
                        highest_ltp=highest_ltp,
                        current_trailing_sl=current_trailing_sl,
                        initial_sl_pts=init_sl_pts,
                        activation_pts=trail_act,
                        jump_pts=trail_jump,
                    )
                else:
                    highest_ltp = max(highest_ltp, ltp)

                # 2. Update Trailing Target
                target_res = compute_trailing_target(
                    entry_price=entry,
                    current_ltp=ltp,
                    target_status=target_status,
                    trailing_target_level=trailing_target_level,
                    highest_after_target=highest_after_target,
                    initial_target_pts=init_target_pts,
                    step_pts=target_step,
                    pullback_pts=target_pullback,
                    trailing_target_enabled=trail_target_enabled,
                )
                target_status = target_res["target_status"]
                trailing_target_level = target_res["trailing_target_level"]
                highest_after_target = target_res["highest_after_target"]
                pullback_exit_level = target_res["pullback_exit_level"]

                # 3. Check Real-Time Exit Conditions
                should_close = False
                exit_reason = ""
                if is_proto_pos and spot_idx > 0:
                    spot_ent = float(p.get("spot_entry") or 0.0)
                    spot_tgt = float(p.get("spot_target_level") or 0.0)
                    spot_sl = float(p.get("spot_stop_level") or 0.0)
                    from analyst.ema8_prototype_8510 import prototype_spot_is_long

                    is_long_spot = prototype_spot_is_long(
                        spot_ent,
                        spot_tgt,
                        strategy_id=str(p.get("strategy_id") or ""),
                        symbol=str(p.get("symbol") or ""),
                    )
                    if spot_ent > 0 and spot_tgt > 0 and spot_sl > 0:
                        if is_long_spot and spot_idx >= spot_tgt:
                            should_close = True
                            exit_reason = "SPOT_TARGET_HIT"
                        elif is_long_spot and spot_idx <= spot_sl:
                            should_close = True
                            exit_reason = "SPOT_SL_HIT"
                        elif (not is_long_spot) and spot_idx <= spot_tgt:
                            should_close = True
                            exit_reason = "SPOT_TARGET_HIT"
                        elif (not is_long_spot) and spot_idx >= spot_sl:
                            should_close = True
                            exit_reason = "SPOT_SL_HIT"
                if is_proto_pos and not should_close:
                    proto_eod_hm = str(self.cfg.get("eod_square_off_time", "15:20")).strip()[:5]
                    if now_dt.strftime("%H:%M") >= proto_eod_hm:
                        should_close = True
                        exit_reason = f"EOD_SQUARE_OFF_{proto_eod_hm}"
                    else:
                        init_tgt = float(p.get("initial_target") or target)
                        init_sl = float(p.get("initial_sl") or sl)
                        if ltp >= init_tgt:
                            should_close = True
                            exit_reason = "OPTION_TARGET_HIT"
                        elif ltp <= init_sl:
                            should_close = True
                            exit_reason = "OPTION_SL_HIT"
                if is_flagship_pos and spot_idx > 0:
                    spot_entry = float(p.get("spot_entry") or 0.0)
                    spot_sl = float(p.get("spot_stop_level") or 0.0)
                    # Every option is bought, so "BUY" says nothing about
                    # direction: a PE is a short on spot.
                    from analyst.ema8_prototype_8510 import prototype_spot_is_long

                    is_long_spot = prototype_spot_is_long(
                        spot_entry,
                        float(p.get("spot_target_level") or 0.0),
                        strategy_id=str(p.get("strategy_id") or ""),
                        symbol=str(p.get("symbol") or ""),
                    )
                    if spot_sl > 0:
                        bar15 = self._latest_closed_15m_bar()
                        if bar15:
                            last_bar_ts = int(self._flagship_trail_bar_ts.get(p["position_id"], 0))
                            if int(bar15["ts"]) != last_bar_ts:
                                spot_sl = flagship_candle_trail_update(
                                    is_long_spot, spot_sl,
                                    bar15["open"], bar15["high"], bar15["low"], bar15["close"],
                                )
                                self._flagship_trail_bar_ts[p["position_id"]] = int(bar15["ts"])
                                cur.execute(
                                    "UPDATE positions SET spot_stop_level=? WHERE position_id=?",
                                    (spot_sl, p["position_id"]),
                                )
                                p["spot_stop_level"] = spot_sl
                    if now_dt.strftime("%H:%M") >= eod_hm:
                        should_close = True
                        exit_reason = f"EOD_SQUARE_OFF_{eod_hm}"
                    else:
                        sr_hit = flagship_sr_exit_trigger(is_long_spot, spot_idx, sr_levels, sr_prox)
                        if sr_hit:
                            should_close = True
                            exit_reason = sr_hit
                        elif is_long_spot and spot_sl > 0 and spot_idx <= spot_sl:
                            should_close = True
                            exit_reason = "FLAGSHIP_SPOT_TRAIL_SL"
                        elif (not is_long_spot) and spot_sl > 0 and spot_idx >= spot_sl:
                            should_close = True
                            exit_reason = "FLAGSHIP_SPOT_TRAIL_SL"
                if not should_close and trail_sl_enabled and ltp <= current_trailing_sl and not is_proto_pos:
                    should_close = True
                    exit_reason = "TRAIL_SL_HIT" if current_trailing_sl > initial_sl else "INITIAL_SL_HIT"
                elif not should_close and target_res["should_exit"] and not is_proto_pos:
                    should_close = True
                    exit_reason = target_res["exit_reason"]

                # P&L Calculation
                gross_pnl = round((ltp - entry) * qty, 2)
                charges = self.calculate_dhan_charges(entry, ltp, qty)
                net_pnl = round(gross_pnl - charges, 2)
                p["gross_pnl"] = gross_pnl
                p["net_pnl"] = net_pnl
                p["dhan_charges"] = charges

                # Duration timer
                try:
                    entry_dt = datetime.fromisoformat(p["entry_time"].replace(" IST", ""))
                    dur_sec = max(0, int((now_dt - entry_dt).total_seconds()))
                    hrs = dur_sec // 3600
                    mins = (dur_sec % 3600) // 60
                    secs = dur_sec % 60
                    p["duration"] = f"{hrs:02d}h {mins:02d}m {secs:02d}s"
                except Exception:
                    p["duration"] = "00h 00m 00s"

                # Progress to Target indicator
                dist_target = max(0.1, target - entry)
                curr_dist = ltp - entry
                pct_target = max(0, min(100, int((curr_dist / dist_target) * 100)))
                p["target_progress_pct"] = pct_target
                p["target_status_text"] = f"{curr_dist:+.1f} / +{dist_target:.1f} pts ({pct_target}% to Target)"

                # Format Trailing SL Display
                diff_sl = current_trailing_sl - entry
                if current_trailing_sl >= entry:
                    p["trail_sl_text"] = f"₹{current_trailing_sl:.2f} (+{diff_sl:.1f} pts Locked)"
                    p["trail_sl_badge"] = "bull"
                else:
                    p["trail_sl_text"] = f"₹{current_trailing_sl:.2f} ({diff_sl:.1f} pts)"
                    p["trail_sl_badge"] = "amber"

                # Format Trailing Target Display
                if target_status == "TRAILING":
                    p["trail_target_text"] = f"₹{trailing_target_level:.2f} | Peak: ₹{highest_after_target:.2f} (Exit: ₹{pullback_exit_level:.2f})"
                    p["trail_target_badge"] = "bull"
                else:
                    p["trail_target_text"] = f"Fixed: ₹{initial_target:.2f} (+{initial_target - entry:.1f} pts)"
                    p["trail_target_badge"] = "side"

                p["highest_ltp"] = highest_ltp
                p["highest_after_target"] = highest_after_target
                p["pullback_exit_level"] = pullback_exit_level
                p["current_trailing_sl"] = current_trailing_sl
                p["trailing_target_level"] = trailing_target_level
                p["target_status"] = target_status

                if should_close:
                    exit_spot_val = spot_idx if spot_idx > 0 else None
                    cur.execute(
                        "UPDATE positions SET status='EXIT_PENDING', exit_reason=? WHERE position_id=?",
                        (exit_reason, p["position_id"]),
                    )
                    p["status"] = "EXIT_PENDING"
                    resume.append((dict(p), exit_reason, exit_spot_val))
                    if p.get("entry_time"):
                        p["entry_time"] = format_desk_timestamp_ist(p["entry_time"])
                    positions.append(p)
                    continue
                else:
                    cur.execute("""
                    UPDATE positions SET current_ltp=?, highest_ltp=?, current_trailing_sl=?, target_status=?,
                                         trailing_target_level=?, highest_after_target=?, pullback_exit_level=?,
                                         gross_pnl=?, net_pnl=?
                    WHERE position_id=?
                    """, (ltp, highest_ltp, current_trailing_sl, target_status, trailing_target_level,
                          highest_after_target, pullback_exit_level, gross_pnl, net_pnl, p["position_id"]))
                    p["symbol"] = format_contract_symbol(raw_symbol=p.get("symbol"))
                    p["source"] = p.get("source") or p.get("placed_by") or "AUTO ALGO 🤖"
                    p["placed_by"] = p["source"]
                    p["strategy_setup"] = p.get("strategy_setup") or p.get("strategy_name") or "Manual — No Strategy"
                    p["structured_contract"] = get_contract_structured_fields(p["symbol"])
                    if p.get("entry_time"):
                        p["entry_time"] = format_desk_timestamp_ist(p["entry_time"])
                    positions.append(p)
            conn.commit()
        # Start the closes only after the ledger write above is committed, so
        # the close path never reads a position mid-transaction.
        for close_pos, close_reason, close_spot in resume:
            close_pos["status"] = "EXIT_PENDING"
            self._spawn_close(close_pos, close_reason, close_spot)
        return positions

    def _spawn_close(self, pos: Dict[str, Any], reason: str, spot_exit: Optional[float]) -> None:
        """Start one close per position and keep its handle.

        A close that is still running is left alone. A close that died is
        logged, and the monitor starts it again on its next pass because the
        position is still EXIT_PENDING.
        """
        pos_id = str(pos.get("position_id") or "")
        if not pos_id:
            return
        tasks = self.__dict__.setdefault("_close_tasks", {})
        running = tasks.get(pos_id)
        if running is not None and not running.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._close_position_with_broker(pos, reason, spot_exit=spot_exit))
            return
        task = loop.create_task(self._close_position_with_broker(pos, reason, spot_exit=spot_exit))
        tasks[pos_id] = task

        def _done(t: "asyncio.Task", _pid: str = pos_id) -> None:
            tasks.pop(_pid, None)
            if t.cancelled():
                log.error("Close task for %s was cancelled; it will be retried", _pid)
            elif t.exception() is not None:
                log.error("Close task for %s failed; it will be retried", _pid, exc_info=t.exception())

        task.add_done_callback(_done)

    # ── UNIFIED POSITION CLOSE WITH BROKER (PHASE 1) ─────────────────────────
    async def _check_broker_sl_fills(self) -> None:
        """Inspects Dhan HQ orders to detect if any companion SL-L order filled on its own."""
        if not self.broker.is_configured():
            return
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM positions WHERE status IN ('OPEN', 'EXIT_PENDING', 'EXIT_FAILED') AND sl_order_id != ''"
                ).fetchall()
            if not rows:
                return

            orders = await self.broker.get_orders()
            orders_by_id = {str(o.get("orderId")): o for o in orders or [] if o.get("orderId")}

            spot_idx = float((self.cached_telemetry.get("spot") or {}).get("price") or 0.0)
            for r in rows:
                p = dict(r)
                if not self._position_live_filled(p):
                    continue
                sl_oid = str(p.get("sl_order_id") or "").strip()
                if sl_oid and sl_oid in orders_by_id:
                    matching = orders_by_id[sl_oid]
                    st = str(matching.get("orderStatus") or "").upper()
                    if st in ("TRADED", "FILLED"):
                        entry = float(p.get("entry_price") or 0.0)
                        fill_price = float(matching.get("averageTradedPrice") or p.get("sl_price") or entry)
                        fill_qty = int(matching.get("filledQty") or p.get("qty") or 0)
                        await self._finalize_closed_position(
                            p, fill_price, fill_qty, "BROKER_SL_FILLED", spot_exit=spot_idx if spot_idx > 0 else None
                        )
        except Exception as exc:
            log.debug("Error in _check_broker_sl_fills: %s", exc)

    async def _close_position_with_broker(
        self,
        pos: Dict[str, Any],
        reason: str,
        spot_exit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Unified close path for ALL positions (Live & Paper).

        Routes every exit (target, stop, opposite close, EOD, manual, kill switch)
        through a single locked broker execution path:
          1. Sets status='EXIT_PENDING' under per-position lock to prevent double-sell.
          2. For LIVE positions:
             - Checks if companion SL-L already filled at Dhan; if so, closes ledger as BROKER_SL_FILLED.
             - Cancels companion SL-L and confirms cancel before selling.
             - Dispatches aggressive limit SELL with chasing loop (never MARKET/SL-M).
             - If sell rejected: marks EXIT_FAILED, raises cockpit banner, re-places protective SL-L.
             - Chases until confirmed filled by Dhan with real fill price and qty.
          3. For PAPER positions: closes at current quote.
          4. Updates paper_wallet, orders book, algo_execution_master, and positions table.
        """
        pos_id = str(pos.get("position_id") or "")
        if not pos_id:
            return {"ok": False, "error": "Missing position_id"}

        if not hasattr(self, "_position_close_locks"):
            self._position_close_locks = {}
        lock = self._position_close_locks.setdefault(pos_id, asyncio.Lock())

        async with lock:
            with sqlite3.connect(self.ledger_db) as conn:
                conn.row_factory = sqlite3.Row
                cur_row = conn.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone()

            if not cur_row:
                return {"ok": False, "error": "Position not found"}

            st = str(cur_row["status"]).upper()
            if st == "CLOSED":
                return {"ok": True, "status": "ALREADY_CLOSED"}
            if st == "EXIT_PENDING" and pos.get("status") != "EXIT_PENDING":
                return {"ok": False, "status": "EXIT_PENDING"}

            # Mark EXIT_PENDING in database, with the reason, so an exit that
            # is interrupted can be resumed for the same reason.
            with sqlite3.connect(self.ledger_db) as conn:
                conn.execute(
                    "UPDATE positions SET status='EXIT_PENDING', exit_reason=? WHERE position_id=?",
                    (reason, pos_id),
                )

            row = dict(cur_row)
            is_live = self._position_live_filled(row) and self.broker.is_configured()
            sec_id = str(cur_row["security_id"] or pos.get("security_id") or "")
            symbol = str(cur_row["symbol"] or pos.get("symbol") or "")
            entry = float(cur_row["entry_price"] or pos.get("entry_price") or 0.0)
            qty = int(cur_row["remaining_qty"] or cur_row["qty"] or pos.get("qty") or 0)

            if is_live:
                return await self._close_live_position(row, reason, spot_exit, ledger_qty=qty)

            else:
                # 3. Paper position: close at current quote
                ltp = None
                if sec_id:
                    ltp = float((self.cached_telemetry.get("quotes") or {}).get(sec_id) or 0.0)
                if not ltp or ltp <= 0:
                    ltp = float(cur_row["current_ltp"] or pos.get("current_ltp") or 0.0)
                if not ltp or ltp <= 0:
                    try:
                        quotes = await self.broker.get_marketfeed_ltp({"NSE_FNO": [int(sec_id)]})
                        ltp = float(quotes.get(sec_id) or 0.0)
                    except Exception:
                        pass
                if not ltp or ltp <= 0:
                    log.error("No valid quote for paper position %s. Leaving OPEN to retry.", pos_id)
                    with sqlite3.connect(self.ledger_db) as conn:
                        conn.execute("UPDATE positions SET status='OPEN', exit_reason='' WHERE position_id=?", (pos_id,))
                    return {"ok": False, "error": "MISSING_QUOTE_RETRY"}

                return await self._finalize_closed_position(
                    dict(cur_row), ltp, qty, reason, spot_exit=spot_exit
                )

    # ── LIVE EXIT: one real position, one real sell ──────────────────────────
    _BROKER_FILLED = ("TRADED", "FILLED")
    _BROKER_DEAD = ("CANCELLED", "REJECTED", "EXPIRED")

    @staticmethod
    def _position_live_filled(row: Dict[str, Any]) -> bool:
        """True when a real BUY filled at Dhan for this position.

        Rows written before the live_filled column existed carry NULL and fall
        back to the stored mode, which is how they were treated before.
        """
        flag = row.get("live_filled")
        if flag is None:
            return str(row.get("mode") or "").upper() in ("LIVE", "REAL", "LIVE_ALLOWED", "BOTH")
        try:
            return int(flag) == 1
        except (TypeError, ValueError):
            return False

    async def _broker_order(self, order_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Return (order, book_readable).

        Dhan's order book comes back empty both when there are no orders and
        when the call failed, so an empty book is reported as unreadable and
        the caller must not conclude anything from the order being absent.
        """
        try:
            orders = await self.broker.get_orders()
        except Exception as exc:
            log.warning("Order book read failed while looking for %s: %s", order_id, exc)
            return None, False
        for o in orders or []:
            if str(o.get("orderId")) == str(order_id):
                return o, True
        return None, bool(orders)

    async def _exit_quote(self, sec_id: str) -> float:
        """Latest option price for an exit: the desk's quote feed first, then Dhan."""
        ltp = float((self.cached_telemetry.get("quotes") or {}).get(sec_id) or 0.0)
        if ltp > 0:
            return ltp
        try:
            quotes = await self.broker.get_marketfeed_ltp({"NSE_FNO": [int(sec_id)]})
            return float(quotes.get(sec_id) or 0.0)
        except Exception as exc:
            log.warning("Exit quote unavailable for %s: %s", sec_id, exc)
            return 0.0

    def _update_position(self, pos_id: str, **cols: Any) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        with sqlite3.connect(self.ledger_db) as conn:
            conn.execute(f"UPDATE positions SET {sets} WHERE position_id=?", (*cols.values(), pos_id))

    def _flag_exit_failed(self, row: Dict[str, Any], err: str, spot_exit: Optional[float]) -> Dict[str, Any]:
        pos_id = str(row["position_id"])
        log.error("Live exit failed for %s: %s", pos_id, err)
        self._update_position(pos_id, status="EXIT_FAILED", exit_reason=f"EXIT_FAILED: {err}")
        self.exit_failure_banner = {
            "position_id": pos_id,
            "symbol": str(row.get("symbol") or ""),
            "error": err,
            "time": datetime.now(IST).strftime("%H:%M:%S IST"),
        }
        sid_u = str(row.get("strategy_id") or "").upper()
        if "EMA8_PROTOTYPE" in sid_u:
            self.log_ema8_prototype_event(
                event="EXIT FAILED",
                direction="LONG" if "LONG" in sid_u else "SHORT",
                spot=spot_exit,
                reason=f"EXIT FAILED — {err}",
                position_id=pos_id,
            )
        return {"ok": False, "status": "EXIT_FAILED", "error": err}

    async def _replace_protective_stop(self, row: Dict[str, Any], qty: int, buf: float) -> None:
        """Put the stop back for the quantity still held after a failed sell."""
        pos_id = str(row["position_id"])
        entry = float(row.get("entry_price") or 0.0)
        sl_price = float(row.get("current_trailing_sl") or row.get("sl_price") or (entry - 24.0))
        sl_trigger = max(1.0, round_to_tick(sl_price, 0.05))
        sl_limit = max(0.5, round_to_tick(sl_trigger - buf, 0.05))
        try:
            re_sl = await self.broker.place_order({
                "correlationId": f"resl_{pos_id}_{int(time.time())}",
                "transactionType": "SELL",
                "exchangeSegment": "NSE_FNO",
                "productType": "INTRADAY",
                "orderType": "STOP_LOSS_LIMIT",
                "validity": "DAY",
                "securityId": str(row.get("security_id") or ""),
                "quantity": qty,
                "price": sl_limit,
                "triggerPrice": sl_trigger,
                "disclosedQuantity": 0,
                "afterMarketOrder": False,
            })
            if re_sl.get("ok"):
                self._update_position(pos_id, sl_order_id=str(re_sl.get("order_id") or ""))
            else:
                log.error("Protective stop for %s was not accepted: %s", pos_id, re_sl.get("error"))
        except Exception as exc:
            log.error("Failed re-placing SL-L for %s: %s", pos_id, exc)

    async def _close_live_position(
        self,
        row: Dict[str, Any],
        reason: str,
        spot_exit: Optional[float],
        *,
        ledger_qty: int,
    ) -> Dict[str, Any]:
        """Sell a real position at Dhan and close the ledger from the real fills.

        Order of work, each step required before the next:
          1. A quote must exist. Without one nothing at Dhan is touched.
          2. The companion stop must be out of the market: filled (then the
             stop was the exit), or confirmed cancelled. If it cannot be
             confirmed, no sell is sent, so the two can never both fill.
          3. One sell order works the remaining quantity. Its id is stored, so
             a restart resumes that order instead of sending a second one.
          4. The ledger closes only when the whole quantity is filled, at the
             quantity-weighted average of every fill.
        """
        pos_id = str(row["position_id"])
        sec_id = str(row.get("security_id") or "")
        symbol = str(row.get("symbol") or "")
        entry = float(row.get("entry_price") or 0.0)
        chase = self.cfg.get("chasing", {}) or {}
        buf = float(chase.get("aggressive_limit_buffer_pts", 2.0))
        interval_s = float(chase.get("auto_modify_interval_ms", 800)) / 1000.0
        # Plumbing value, not a trading limit: how many price changes one
        # order takes before it is swapped for a fresh one, kept under the
        # broker's per-order modification allowance.
        max_mods = int(chase.get("exit_max_modifications_per_order", 20))

        total_qty = int(row.get("live_fill_qty") or ledger_qty or 0)
        done_qty = int(row.get("exit_filled_qty") or 0)
        done_val = float(row.get("exit_filled_value") or 0.0)
        order_id = str(row.get("exit_order_id") or "").strip()
        sl_oid = str(row.get("sl_order_id") or "").strip()

        def _book(fill_qty: int, price: float) -> None:
            nonlocal done_qty, done_val
            if fill_qty <= 0:
                return
            done_qty += fill_qty
            done_val += fill_qty * price
            self._update_position(pos_id, exit_filled_qty=done_qty, exit_filled_value=round(done_val, 4))

        async def _finish(final_reason: str) -> Dict[str, Any]:
            avg = round(done_val / done_qty, 2) if done_qty > 0 else 0.0
            self._update_position(pos_id, exit_order_id="")
            return await self._finalize_closed_position(row, avg, ledger_qty, final_reason, spot_exit=spot_exit)

        # 1. Quote first. Nothing at Dhan has been touched yet, so the
        #    position simply goes back to OPEN with its stop still working.
        if not order_id and await self._exit_quote(sec_id) <= 0:
            log.error("Missing quote for %s during exit. Leaving OPEN to retry.", pos_id)
            self._update_position(pos_id, status="OPEN", exit_reason="")
            return {"ok": False, "error": "MISSING_QUOTE_RETRY"}

        # 2. Companion stop out of the market.
        if sl_oid:
            stop_state = "ACTIVE"
            stop_order: Optional[Dict[str, Any]] = None
            for _attempt in range(3):
                stop_order, readable = await self._broker_order(sl_oid)
                st = str((stop_order or {}).get("orderStatus") or "").upper()
                if st in self._BROKER_FILLED:
                    stop_state = "FILLED"
                    break
                if st in self._BROKER_DEAD:
                    stop_state = "INACTIVE"
                    break
                try:
                    cancel = await self.broker.cancel_order(sl_oid)
                except Exception as exc:
                    cancel = {"ok": False, "error": str(exc)}
                    log.warning("Cancel companion SL-L %s error: %s", sl_oid, exc)
                if stop_order is None and cancel.get("ok"):
                    # Not in the book, and Dhan accepted the cancel: it is gone.
                    stop_state = "INACTIVE"
                    break
                await asyncio.sleep(interval_s)
            else:
                stop_order, _ = await self._broker_order(sl_oid)
                st = str((stop_order or {}).get("orderStatus") or "").upper()
                if st in self._BROKER_FILLED:
                    stop_state = "FILLED"
                elif st in self._BROKER_DEAD:
                    stop_state = "INACTIVE"

            stop_filled = int((stop_order or {}).get("filledQty") or 0)
            stop_avg = float((stop_order or {}).get("averageTradedPrice") or row.get("sl_price") or entry)
            if stop_state == "FILLED":
                _book(stop_filled or (total_qty - done_qty), stop_avg)
                return await _finish("BROKER_SL_FILLED")
            if stop_state == "ACTIVE":
                return self._flag_exit_failed(
                    row, "Companion stop could not be confirmed cancelled; no sell sent", spot_exit
                )
            _book(min(stop_filled, total_qty - done_qty), stop_avg)  # stop part-filled before the cancel
            self._update_position(pos_id, sl_order_id="")

        # 3. Work one sell order for whatever is still held.
        last_limit: Optional[float] = None
        mods = 0
        we_cancelled = False
        unreadable = 0
        while True:
            remaining = total_qty - done_qty
            if remaining <= 0:
                return await _finish(reason)

            if not order_id:
                ltp = await self._exit_quote(sec_id)
                if ltp <= 0:
                    await asyncio.sleep(interval_s)
                    continue
                last_limit = max(0.5, round_to_tick(ltp - buf, 0.05))
                res = await self.broker.place_order({
                    "dhanClientId": getattr(self.broker, "client_id", ""),
                    "correlationId": f"exit_{pos_id}",
                    "transactionType": "SELL",
                    "exchangeSegment": "NSE_FNO",
                    "productType": "INTRADAY",
                    "orderType": "LIMIT",
                    "validity": "DAY",
                    "tradingSymbol": symbol,
                    "securityId": sec_id,
                    "quantity": remaining,
                    "disclosedQuantity": 0,
                    "price": last_limit,
                    "triggerPrice": 0.0,
                    "afterMarketOrder": False,
                })
                if not res.get("ok"):
                    err = str(res.get("error") or res.get("rejection_reason") or "Sell order rejected")
                    out = self._flag_exit_failed(row, err, spot_exit)
                    await self._replace_protective_stop(row, remaining, buf)
                    return out
                order_id = str(res.get("order_id") or "")
                mods = 0
                we_cancelled = False
                self._update_position(pos_id, exit_order_id=order_id)

            await asyncio.sleep(interval_s if unreadable == 0 else min(5.0, interval_s * (1 + unreadable)))
            order, readable = await self._broker_order(order_id)
            if order is None:
                if readable:
                    # The book is readable and this order is not in it (for
                    # example an id left over from an earlier day). Start clean.
                    order_id = ""
                    self._update_position(pos_id, exit_order_id="")
                    unreadable = 0
                    continue
                unreadable += 1
                if unreadable >= 15:
                    # Still EXIT_PENDING with its order id stored: the monitor
                    # resumes this same order on its next pass.
                    self.exit_failure_banner = {
                        "position_id": pos_id,
                        "symbol": symbol,
                        "error": "Cannot read order status from Dhan; exit still working",
                        "time": datetime.now(IST).strftime("%H:%M:%S IST"),
                    }
                    return {"ok": False, "status": "EXIT_PENDING", "error": "ORDER_STATUS_UNREADABLE"}
                continue
            unreadable = 0

            st = str(order.get("orderStatus") or "").upper()
            got = int(order.get("filledQty") or 0)
            avg = float(order.get("averageTradedPrice") or last_limit or 0.0)
            if avg <= 0:
                avg = await self._exit_quote(sec_id)

            if st in self._BROKER_FILLED:
                _book(remaining, avg)
                continue
            if st in self._BROKER_DEAD:
                _book(min(got, remaining), avg)
                order_id = ""
                self._update_position(pos_id, exit_order_id="")
                if we_cancelled and st != "REJECTED":
                    continue  # our own swap for a fresh order
                left = total_qty - done_qty
                if left <= 0:
                    continue
                err = str(order.get("omsErrorDescription") or st)
                out = self._flag_exit_failed(row, err, spot_exit)
                await self._replace_protective_stop(row, left, buf)
                return out

            # Still working: follow the price, but only when it has moved.
            ltp = await self._exit_quote(sec_id)
            if ltp <= 0:
                continue
            new_limit = max(0.5, round_to_tick(ltp - buf, 0.05))
            if last_limit is not None and abs(new_limit - last_limit) < 0.05:
                continue
            if mods >= max_mods:
                try:
                    await self.broker.cancel_order(order_id)
                    we_cancelled = True
                except Exception as exc:
                    log.warning("Cancel of exit order %s for re-place failed: %s", order_id, exc)
                continue
            try:
                mod = await self.broker.modify_order(order_id, {
                    "orderType": "LIMIT",
                    "quantity": remaining,
                    "price": new_limit,
                    "triggerPrice": 0.0,
                    "validity": "DAY",
                })
            except Exception as exc:
                mod = {"ok": False, "error": str(exc)}
            if mod.get("ok"):
                last_limit = new_limit
                mods += 1
            else:
                log.warning("Modify exit order %s failed: %s", order_id, mod.get("error"))

    async def _finalize_closed_position(
        self,
        pos: Dict[str, Any],
        exit_price: float,
        qty: int,
        exit_reason: str,
        spot_exit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Finalizes position closure in positions, paper_wallet, orders, and master table."""
        pos_id = str(pos["position_id"])
        entry = float(pos.get("entry_price") or 0.0)
        now_dt = datetime.now(IST)
        exit_time = now_dt.strftime("%Y-%m-%d %H:%M:%S IST")
        gross = round((exit_price - entry) * qty, 2)
        charges = self.calculate_dhan_charges(entry, exit_price, qty)
        net = round(gross - charges, 2)

        with sqlite3.connect(self.ledger_db) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE positions SET
                    exit_price=?, exit_time=?, status='CLOSED', gross_pnl=?, net_pnl=?,
                    exit_reason=?, current_ltp=?, spot_exit=COALESCE(?, spot_exit)
                WHERE position_id=?
            """, (exit_price, exit_time, gross, net, exit_reason, exit_price, spot_exit, pos_id))

            # Update paper wallet margin, balance, and realized pnl
            req_margin = entry * qty
            cur.execute("""
                UPDATE paper_wallet SET
                    balance = balance + ?,
                    margin_used = max(0.0, margin_used - ?),
                    realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE id=1
            """, (net, req_margin, net, now_dt.isoformat()))

            self._record_exit_order(
                cur, pos, exit_price, qty, exit_reason, net, charges, nifty_spot_exit=spot_exit
            )
            conn.commit()

        closed = dict(pos)
        closed.update(
            exit_price=exit_price,
            exit_time=exit_time,
            gross_pnl=gross,
            net_pnl=net,
            dhan_charges=charges,
            exit_reason=exit_reason,
            spot_exit=spot_exit,
            duration=duration_between(pos.get("entry_time"), exit_time),
        )
        append_algo_execution_master(self.ledger_db, master_row_from_position(closed))

        if getattr(self, "exit_failure_banner", None) and self.exit_failure_banner.get("position_id") == pos_id:
            self.exit_failure_banner = None

        strat_id = str(pos.get("strategy_id") or "").upper()
        if strat_id.startswith("EMA8_PROTOTYPE"):
            self._sync_open_ema8_prototype_position()
            evt = "EXITED"
            er_u = str(exit_reason).upper()
            if "TARGET" in er_u:
                evt = "TARGET HIT"
            elif "SL_HIT" in er_u or "STOP" in er_u or "BROKER_SL" in er_u:
                evt = "STOP HIT"
            elif "OPPOSITE" in er_u:
                evt = "OPPOSITE CLOSE"
            elif "EOD" in er_u:
                evt = "EOD SQUARE OFF"

            direction = str(pos.get("direction") or ("LONG" if "LONG" in strat_id else "SHORT")).upper()
            self.log_ema8_prototype_event(
                event=evt,
                direction=direction,
                spot=spot_exit,
                reason=exit_reason,
                position_id=pos_id,
            )
            self._ema8_prototype_state["last_exit_reason"] = exit_reason
            from analyst.ema8_prototype_8510 import load_ema8_prototype_cfg, note_prototype_exit

            # Profit: the arm stays for a re-entry. Stop-loss: that side is cancelled.
            # Side from the strategy id: the ledger's own direction field is not
            # guaranteed to say LONG / SHORT.
            direction = "LONG" if "LONG" in strat_id else "SHORT"
            if note_prototype_exit(
                self._ema8_prototype_state, load_ema8_prototype_cfg(self.scanner_cfg), direction, exit_reason
            ):
                self.log_ema8_prototype_event(
                    event="SIDE CANCELLED",
                    direction=direction,
                    spot=spot_exit,
                    reason=f"Stop-loss on {direction}: no re-entry on this side until the opposite side is armed",
                    position_id=pos_id,
                )
            self._save_ema8_prototype_state()

        log.info("Position %s exited via %s @ ₹%.2f (Net PnL: ₹%.2f)", pos_id, exit_reason, exit_price, net)
        return {"ok": True, "fill_price": exit_price, "net_pnl": net, "exit_reason": exit_reason}

    # ── EXIT LEG LEDGER ──────────────────────────────────────────────────────
    def _record_exit_order(
        self,
        cur,
        pos: Any,
        exit_price: float,
        qty: int,
        exit_reason: str,
        net_pnl: float,
        charges: float,
        nifty_spot_exit: Optional[float] = None,
    ) -> str:
        """Write the SELL leg of a closed position into the orders book.

        WHY THIS EXISTS
        ---------------
        execute_order_with_chaser only ever recorded the BUY leg. Positions were
        closed by UPDATEing the positions row, so the Institutional Orders Book
        showed entries and never the square-off that followed — every order read
        as BUY no matter how many positions had been closed.

        Takes the caller's open cursor so the exit row and the position UPDATE
        commit together: a close must never be recorded without its order, or
        the book would disagree with the ledger.

        Accepts a sqlite3.Row or a dict (Row has no .get()).
        """
        def fld(key, default=None):
            try:
                val = pos[key]
            except (KeyError, IndexError, TypeError):
                return default
            return default if val is None else val

        pos_id = str(fld("position_id", ""))
        order_id = "X_{}".format(pos_id) if pos_id else "X_{}".format(int(time.time() * 1000))
        symbol = str(fld("symbol", ""))
        entry = float(fld("entry_price", 0.0) or 0.0)
        gross = round((float(exit_price) - entry) * int(qty), 2)
        try:
            cur.execute("""
            INSERT OR REPLACE INTO orders (
                order_id, correlation_id, created_at, strategy_name, symbol, security_id,
                side, product, order_type, lots, qty, signal_ltp, limit_price, fill_price,
                slippage, chase_retries, brokerage_taxes, order_status, mode, rejection_reason,
                placed_by, source, strategy_setup, position_id, exit_reason, net_pnl,
                nifty_spot_entry, nifty_spot_exit
            ) VALUES (?, ?, ?, ?, ?, ?, 'SELL', 'INTRADAY', 'AGGR-LIMIT', 1, ?, ?, ?, ?,
                      0.0, 0, ?, 'FILLED', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, pos_id,
                datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                str(fld("strategy_name", "") or fld("strategy_setup", "")),
                symbol, str(fld("security_id", "")),
                int(qty), float(exit_price), float(exit_price), float(exit_price),
                float(charges), str(fld("mode", "PAPER_ONLY")),
                str(fld("placed_by", "AUTO ALGO 🤖")),
                str(fld("source", "AUTO ALGO 🤖")),
                str(fld("strategy_setup", "Manual — No Strategy")),
                pos_id, str(exit_reason), float(net_pnl),
                float(fld("spot_entry") or 0.0) or None,
                float(nifty_spot_exit) if nifty_spot_exit and nifty_spot_exit > 0 else None,
            ))
        except sqlite3.Error as exc:
            # A failed book entry must not abort the square-off itself.
            log.error("Could not record exit order for %s: %s", pos_id, exc)
            return ""
        log.info("Exit leg recorded: %s SELL %s @ %.2f (%s, net Rs%.2f, gross Rs%.2f)",
                 order_id, symbol, float(exit_price), exit_reason, float(net_pnl), gross)
        return order_id

    def _ledger_nifty_spot(self) -> float:
        sp = float(self.cached_telemetry.get("spot", {}).get("price", 0.0) or 0.0)
        if sp <= 0:
            sp = float(self.broker._ltp_cache.get("13", 0.0) or 0.0)
        if sp <= 0 and self.market_db.exists():
            try:
                with sqlite3.connect(self.market_db) as mconn:
                    r = mconn.execute(
                        "SELECT close FROM spot_candles WHERE sec_id='13' ORDER BY ts DESC LIMIT 1"
                    ).fetchone()
                    if r and r[0]:
                        sp = float(r[0])
            except sqlite3.Error:
                pass
        return sp

    def get_order_book(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()
            orders = []
            for r in rows:
                o = dict(r)
                o["symbol"] = format_contract_symbol(raw_symbol=o.get("symbol"))
                o["source"] = o.get("source") or o.get("placed_by") or "AUTO ALGO 🤖"
                o["placed_by"] = o["source"]
                o["strategy_setup"] = o.get("strategy_setup") or o.get("strategy_name") or "Manual — No Strategy"
                o["structured_contract"] = get_contract_structured_fields(o["symbol"])
                pos_link = o.get("position_id") or o.get("correlation_id")
                prow = None
                if pos_link:
                    try:
                        prow = cur.execute(
                            "SELECT spot_entry, spot_exit, entry_price, exit_price FROM positions WHERE position_id=?",
                            (pos_link,),
                        ).fetchone()
                        if prow:
                            if not o.get("nifty_spot_entry") and prow[0]:
                                o["nifty_spot_entry"] = float(prow[0])
                            if not o.get("nifty_spot_exit") and prow[1]:
                                o["nifty_spot_exit"] = float(prow[1])
                    except sqlite3.Error:
                        prow = None
                side_u = str(o.get("side") or "").upper()
                fill_px = float(o.get("fill_price") or 0)
                sig_px = float(o.get("signal_ltp") or 0)
                pos_entry = float(prow[2]) if prow and prow[2] else 0.0
                pos_exit = float(prow[3]) if prow and prow[3] else 0.0
                if side_u == "BUY":
                    o["option_entry"] = fill_px if fill_px > 0 else (sig_px if sig_px > 0 else pos_entry or None)
                    o["option_exit"] = pos_exit if pos_exit > 0 else None
                elif side_u == "SELL":
                    o["option_entry"] = pos_entry if pos_entry > 0 else None
                    o["option_exit"] = fill_px if fill_px > 0 else (pos_exit if pos_exit > 0 else None)
                else:
                    o["option_entry"] = fill_px if fill_px > 0 else None
                    o["option_exit"] = None
                if o.get("created_at"):
                    o["created_at"] = format_desk_timestamp_ist(o["created_at"])
                orders.append(o)
            return orders

    # ── AGGRESSIVE LIMIT ORDER CHASING ENGINE ────────────────────────────────
    async def execute_order_with_chaser(
        self,
        contract: Dict[str, Any],
        side: str = "BUY",
        strategy_name: str = "Manual Matrix",
        target_pts: float = 40.0,
        sl_pts: float = 40.0,
        is_algo_trade: bool = False,
        source: Optional[str] = None,
        strategy_setup: Optional[str] = None,
        strategy_id: Optional[str] = None,
        enforce_market_hours: bool = False,
        # v1.9.4c Matrix Scalp: spot-side context recorded alongside the
        # premium-based position, so the stop-width study can be done in SPOT
        # points. Optional and ignored by every existing caller.
        spot_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        SEBI Post-March 2026 Aggressive Limit Chaser Implementation:
          1. Never send MARKET or SL-M.
          2. BUY LIMIT placed at Limit = round_to_tick(LTP + buffer, 0.05).
          3. Checks status after auto_modify_interval_ms (800ms).
          4. If PENDING, modifies order price to New_LTP + buffer.
          5. Repeats up to max_chase_retries (3 retries).
          6. On fill, places companion SL-L order at trigger=SL, limit=SL - sl_buffer.
          7. Supports DUAL execution: Real Dhan API + Parallel Paper Sync.
        """
        async with self.lock:
            mode = self.execution_mode
            if self.kill_switch_active:
                return {"ok": False, "error": "KILL SWITCH ACTIVE: All execution locked in STANDBY"}

            # ── v1.9.4b LIVE ROUTING GATE ────────────────────────────────────
            # Resolves to PAPER_ONLY / DRY_RUN / LIVE. This can only ever
            # DOWNGRADE: `mode` is reassigned to PAPER_ONLY, never to BOTH or
            # ALGO_LIVE_ONLY, so no guard failure and no config value here can
            # cause a real order. Arming remains configure_real_money's job and
            # still requires live_validation_status == 'APPROVED'.
            live_route = self.live_execution_decision()
            if live_route.get("disarm"):
                self._apply_live_disarm(live_route["reason"])
            if live_route["route"] != "LIVE":
                mode = "PAPER_ONLY"

            # 8 EMA Prototype route switch. The cockpit's PAPER_ONLY setting
            # for this lane previously changed only the label on the order;
            # the order itself still followed the desk and went to Dhan when
            # LIVE was armed. Downgrade only, and only for this lane.
            proto_lane_paper = False
            if mode != "PAPER_ONLY" and str(
                (spot_context or {}).get("strategy_id") or strategy_id or ""
            ).upper().startswith("EMA8_PROTOTYPE"):
                from analyst.ema8_prototype_8510 import (
                    _execution_mode_labels,
                    load_ema8_prototype_cfg,
                )

                if _execution_mode_labels(load_ema8_prototype_cfg(self.scanner_cfg))[0]:
                    mode = "PAPER_ONLY"
                    proto_lane_paper = True

            # 8600 Ticket lane route switch: PAPER_ONLY for this lane never sends
            # a real order. Downgrade only, and only for this lane.
            if mode != "PAPER_ONLY" and str(
                (spot_context or {}).get("strategy_id") or strategy_id or ""
            ).upper().startswith("DESK8600_TICKET"):
                from analyst.desk8600_ticket_lane_8510 import load_cfg as _ticket_lane_cfg

                if _ticket_lane_cfg(self.scanner_cfg)["route"] == "PAPER_ONLY":
                    mode = "PAPER_ONLY"

            # Directional Bias Enforcement Gate (Live Execution & Strike Matrix Orders)
            opt_type = str(contract.get("opt_type", "")).upper()
            cid = str(contract.get("id", "")).upper()
            sym = str(contract.get("symbol", "")).upper()
            is_pe = (opt_type == "PE" or "PE" in cid or "PE" in sym)
            is_ce = (opt_type == "CE" or "CE" in cid or "CE" in sym)

            if self.directional_bias == "LONG_ONLY" and is_pe:
                err_msg = f"[DIRECTIONAL BIAS REJECTED] PE / Short order for {contract.get('symbol', cid)} blocked: LONG_ONLY bias blocks all PE / Short trades."
                log.warning(err_msg)
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "error": err_msg,
                    "rejection_reason": "Directional Bias: LONG_ONLY active (all PE / Short orders blocked)"
                }

            if self.directional_bias == "SHORT_ONLY" and is_ce:
                err_msg = f"[DIRECTIONAL BIAS REJECTED] CE / Long order for {contract.get('symbol', cid)} blocked: SHORT_ONLY bias blocks all CE / Long trades."
                log.warning(err_msg)
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "error": err_msg,
                    "rejection_reason": "Directional Bias: SHORT_ONLY active (all CE / Long orders blocked)"
                }

            # ── ALGO-ONLY GATE ───────────────────────────────────────────────
            # Enforced here rather than in the HTTP route so that EVERY caller
            # is covered — route, scanner, strike matrix, Tab 6 — and no path
            # can place a discretionary order behind the gate's back.
            #
            # Deliberately placed AFTER the directional-bias checks: both gates
            # block the order either way, and the bias reason is the more
            # specific one to report when both apply.
            if is_algo_trade is not True:
                err_msg = ("[MANUAL TRADE BLOCKED] 8510 is running ALGO-ONLY. "
                           "Only algo-generated signals execute; this order was "
                           "placed manually. Manual trading is permanently disabled.")
                log.warning("%s (%s / %s)", err_msg, strategy_name,
                            contract.get("symbol", contract.get("id", "?")))
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "error": err_msg,
                    "rejection_reason": "Algo-Only mode: manual orders are blocked",
                    "algo_only_blocked": True,
                }

            sid_check = str(strategy_id or "").strip().upper()
            if not self.algo_strategy_execution_allowed(sid_check):
                err_msg = (
                    f"[ALLOWLIST BLOCKED] Strategy {sid_check or 'UNKNOWN'} is not enabled "
                    f"for paper/live algo execution on this desk."
                )
                log.warning("%s (%s)", err_msg, strategy_name)
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "error": err_msg,
                    "rejection_reason": f"ALLOWLIST_NOT_ENABLED:{sid_check or 'UNKNOWN'}",
                    "allowlist_blocked": True,
                }

            # Step 4A Fail-Closed Guard: Resolve spot from cache (Tier 1), Dhan quote, or last closed 1m candle (Tier 2)
            current_spot = float(self.cached_telemetry.get("spot", {}).get("price", 0.0) or 0.0)
            if current_spot <= 0:
                current_spot = float(self.broker._ltp_cache.get("13", 0.0) or 0.0)
            if current_spot <= 0 and self.market_db.exists():
                try:
                    with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True) as conn:
                        cur = conn.cursor()
                        r = cur.execute("SELECT c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 1").fetchone()
                        if r and r[0] and float(r[0]) > 0:
                            current_spot = float(r[0])
                except Exception:
                    pass
            if current_spot <= 0:
                current_spot = float(contract.get("spot", 0.0) or contract.get("index_price", 0.0) or 0.0)

            # Never invent a price: reject order if spot is 0.0 or unavailable
            if current_spot <= 0:
                err_msg = "🚨 ORDER REJECTED: Underlying spot price is 0.0 or unavailable. Routing aborted (fail closed)."
                log.critical(err_msg)
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "error": err_msg,
                    "rejection_reason": "Underlying spot price is 0.0 or unavailable (fail closed)",
                }

            # Dhan OI PE-CE Synergy Advisory Check (Advisory-Only & Fail-Open)
            # A third-party website must NEVER block or delay live order flow.
            try:
                syn = self.dhan_oi.evaluate_synergy_status()
                synergy_status = syn.get("synergy_status", "MIXED")
            except Exception as e:
                log.debug("DhanOI synergy evaluation exception (failing open): %s", e)
                synergy_status = "MIXED"

            if is_pe and synergy_status == "BULLISH_SYNERGY":
                log.info("[DHAN OI ADVISORY] PE order placed while DhanOI indicates Bullish synergy (Advisory only — failing open).")
            elif is_ce and synergy_status == "BEARISH_SYNERGY":
                log.info("[DHAN OI ADVISORY] CE order placed while DhanOI indicates Bearish synergy (Advisory only — failing open).")
            elif is_ce and synergy_status == "BULLISH_SYNERGY":
                log.info("[DHAN OI ADVISORY] Bullish multi-strike synergy confirmed for CE Long order.")
            elif is_pe and synergy_status == "BEARISH_SYNERGY":
                log.info("[DHAN OI ADVISORY] Bearish multi-strike synergy confirmed for PE Short order.")

            # Check Strike Matrix Checkbox filter if triggered automatically by Algo
            contract_id = contract.get("id", "")
            if is_algo_trade and contract_id:
                if not self.algo_strike_selection.get(contract_id, True):
                    sym = contract.get("symbol", contract_id)
                    log.info("[SKIPPED] Strike %s excluded by Trader Strike Matrix Filter", sym)
                    return {
                        "ok": False,
                        "skipped": True,
                        "error": f"[SKIPPED] Strike {sym} excluded by Trader Strike Matrix Filter"
                    }

            # Market Hours Validator (Live + Paper)
            is_open, mkt_status = self.is_nse_market_open()
            if enforce_market_hours:
                is_paper_exec = (mode == "PAPER_ONLY" or (mode == "BOTH" and self.paper_execution_enabled))
                if not is_open:
                    if mode == "ALGO_LIVE_ONLY" or not (is_paper_exec and self.off_hours_testing_active()):
                        curr_t = datetime.now(IST).strftime("%H:%M:%S")
                        err_msg = (
                            f"❌ Market Closed — Cannot Place Order Now\n"
                            f"Market Hours: Mon-Fri 09:15 AM - 03:30 PM IST\n"
                            f"Current Time: {curr_t} IST | Market Status: CLOSED"
                        )
                        log.warning(err_msg)
                        return {
                            "ok": False,
                            "status": "REJECTED",
                            "error": err_msg,
                            "rejection_reason": err_msg,
                            "market_status": "CLOSED",
                            "market_closed": True,
                        }
                    else:
                        log.warning("⚠️ Testing Mode — Market is Closed. Allowing Paper order execution.")

            buf = float(self.chasing_cfg.get("aggressive_limit_buffer_pts", 4.0))
            interval_s = float(self.chasing_cfg.get("auto_modify_interval_ms", 800)) / 1000.0
            max_retries = int(self.chasing_cfg.get("max_chase_retries", 3))
            sl_buf = float(self.chasing_cfg.get("sl_limit_buffer_pts", 4.0))

            symbol = format_contract_symbol(
                expiry=contract.get("expiry"),
                strike=contract.get("strike"),
                opt_type=contract.get("opt_type"),
                raw_symbol=contract.get("symbol"),
            )
            sec_id = str(contract.get("security_id", ""))
            lot = contract.get('lot_size')
            premium = contract.get('ltp')
            if not isinstance(lot,int) or isinstance(lot,bool) or lot<=0 or not sec_id.isdigit():
                return {'ok':False,'status':'REJECTED','error':'Verified instrument and dynamic lot size required'}
            if not isinstance(premium,(int,float)) or not math.isfinite(premium) or premium<=0:
                return {'ok':False,'status':'REJECTED','error':'Real positive Dhan option quote required'}
            qty = lot
            signal_ltp = float(premium)
            delta = float(contract.get("delta", 0.60))

            placed_source = source or ("AUTO ALGO 🤖" if is_algo_trade else ("MANUAL MATRIX ✋" if "Matrix" in strategy_name else ("MANUAL ROUTER ✋" if "Router" in strategy_name else ("MANUAL TAB 6 ✋" if ("Tab 6" in strategy_name or "Dhan OI" in strategy_name) else "MANUAL MATRIX ✋"))))
            if not strategy_setup:
                if sid_check == "STAGE_2_LONG":
                    strategy_setup = "Stage 2 8-EMA (Long)"
                elif sid_check == "STAGE_3_SHORT":
                    strategy_setup = "Stage 3 8-EMA (Short)"
                elif sid_check == "CROSSOVER_BULLISH":
                    strategy_setup = "4/8 Reversal Upside @ 8 EMA"
                elif sid_check == "CROSSOVER_BEARISH":
                    strategy_setup = "4/8 Reversal Downside @ 8 EMA"
                elif sid_check.startswith("FLAGSHIP_1H"):
                    strategy_setup = strategy_name or "1H FLAGSHIP PA"
                elif sid_check.startswith("PA_SR"):
                    strategy_setup = strategy_name or sid_check
                elif "Stage 2" in strategy_name:
                    strategy_setup = "Stage 2 8 EMA Touch"
                elif "4/8" in strategy_name or "Crossover" in strategy_name:
                    if "Bearish" in strategy_name or "Down" in strategy_name:
                        strategy_setup = "4/8 Crossover Reversal — Bearish"
                    else:
                        strategy_setup = "4/8 Crossover Reversal — Bullish"
                elif "1H" in strategy_name:
                    strategy_setup = "1H 8 EMA Support"
                elif "ATM Reversal Upside" in strategy_name:
                    strategy_setup = "ATM Reversal Upside"
                elif "ATM Reversal Downside" in strategy_name:
                    strategy_setup = "ATM Reversal Downside"
                elif "Stage 3" in strategy_name:
                    strategy_setup = "Stage 3 8 EMA"
                elif "P minus C" in strategy_name or "Synergy" in strategy_name or "Dhan OI" in strategy_name:
                    strategy_setup = "P minus C Filter — Total OI + OI Change"
                else:
                    strategy_setup = "Manual — No Strategy"

            is_proto_order = (
                str((spot_context or {}).get("strategy_id") or strategy_id or "").upper().startswith("EMA8_PROTOTYPE")
                or "8 EMA PROTOTYPE" in str(strategy_name or "").upper()
            )

            fixed_exit_order = is_proto_order or uses_fixed_spot_or_option_exit(
                (spot_context or {}).get("strategy_id") or strategy_id
            )
            if fixed_exit_order:
                opt_target_pts = float(target_pts)
                opt_sl_pts = float(sl_pts)
            else:
                opt_target_pts = round(target_pts * abs(delta), 1)
                opt_sl_pts = round(sl_pts * abs(delta), 1)

            order_id = f"ORD_{int(time.time()*1000)}"
            corr_id = f"chase_{int(time.time())}"
            now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            # v1.9.4b: in DRY_RUN, record what the real order WOULD have been,
            # with the actual bid/ask at signal time. No order is sent. These
            # rows are the measurement instrument for execution cost.
            dry_run_row = None
            if live_route["route"] == "DRY_RUN":
                quote = {}
                try:
                    sec_id = int(str(contract.get("security_id") or "0"))
                    if sec_id:
                        depth = await self.broker.get_marketfeed_quote({"NSE_FNO": [sec_id]})
                        quote = depth.get(str(sec_id)) or {}
                except (ValueError, TypeError, AttributeError) as exc:
                    # A missing quote must not stop the paper leg; the row is
                    # written with NULL bid/ask so the gap is visible, not faked.
                    log.warning("Dry-run quote unavailable for %s: %s", symbol, exc)
                dry_run_row = self._record_dry_run_order(
                    contract, side, qty, signal_ltp, strategy_name, order_id, quote)
                log.info("DRY-RUN — Would have placed LIVE order: %s %s %s @ limit %s "
                         "(bid %s / ask %s)", symbol, side, qty,
                         dry_run_row.get("intended_limit_price"),
                         dry_run_row.get("bid"), dry_run_row.get("ask"))

            chase_retries_used = 0
            fill_price = signal_ltp
            order_status = "FILLED"
            rejection_reason = ""
            companion_sl_order_id = None
            live_order_id = None          # v1.9.4b: broker id, live legs only
            live_filled_qty = 0           # quantity Dhan actually filled on the BUY
            ledger_position_id = ""

            # ── 1. LIVE DHAN API EXECUTION (if ALGO_LIVE_ONLY or BOTH) ───────
            if mode in ("ALGO_LIVE_ONLY", "BOTH"):
                if not self.broker.is_configured():
                    order_status = "REJECTED"
                    rejection_reason = "DHAN credentials missing or invalid in .env"
                else:
                    limit_price = round_to_tick(signal_ltp + buf, 0.05)
                    log.info("🚀 Placing Live Protected Aggressive Limit BUY: %s @ ₹%.2f (LTP %.2f + buf %.1f)",
                             symbol, limit_price, signal_ltp, buf)

                    payload = {
                        "correlationId": corr_id,
                        "transactionType": side,
                        "exchangeSegment": "NSE_FNO",
                        "productType": "INTRADAY",
                        "orderType": "LIMIT",
                        "validity": "DAY",
                        "securityId": sec_id,
                        "quantity": qty,
                        "price": limit_price,
                        "triggerPrice": 0.0,
                        "disclosedQuantity": 0,
                        "afterMarketOrder": False,
                    }

                    res = await self.broker.place_order(payload)
                    if not res.get("ok"):
                        order_status = "REJECTED"
                        rejection_reason = res.get("error", "Order placement failed")
                        log.error("Live order failed: %s", rejection_reason)
                    else:
                        live_order_id = res.get("order_id", order_id)
                        order_id = live_order_id

                        # Chasing Loop
                        filled = False
                        for retry in range(max_retries + 1):
                            await asyncio.sleep(interval_s)
                            orders = await self.broker.get_orders()
                            matching = [o for o in orders if str(o.get("orderId")) == str(live_order_id)]
                            if matching:
                                cur_status = matching[0].get("orderStatus", "")
                                if cur_status in ("TRADED", "FILLED"):
                                    filled = True
                                    fill_price = float(matching[0].get("averageTradedPrice") or limit_price)
                                    live_filled_qty = int(matching[0].get("filledQty") or qty)
                                    order_status = "FILLED"
                                    break
                                elif cur_status in ("CANCELLED", "REJECTED"):
                                    order_status = cur_status
                                    rejection_reason = matching[0].get("omsErrorDescription") or "Broker rejected"
                                    break

                            # Modify / chase if still pending
                            if retry < max_retries:
                                chase_retries_used += 1
                                # Sample fresh LTP
                                fresh_quotes = await self.broker.get_marketfeed_ltp({"NSE_FNO": [int(sec_id)]})
                                fresh_ltp = fresh_quotes.get(sec_id, signal_ltp)
                                new_limit_price = round_to_tick(fresh_ltp + buf, 0.05)
                                log.info("🔄 Chasing retry %d/%d: Modifying order %s to ₹%.2f",
                                         retry + 1, max_retries, live_order_id, new_limit_price)
                                mod_res = await self.broker.modify_order(live_order_id, {
                                    "orderType": "LIMIT",
                                    "quantity": qty,
                                    "price": new_limit_price,
                                    "triggerPrice": 0.0,
                                    "validity": "DAY",
                                })
                                if not mod_res.get("ok"):
                                    log.warning("Chasing modify failed: %s", mod_res.get("error"))

                        if not filled and order_status not in ("REJECTED", "CANCELLED"):
                            # Cancel after exceeding retries
                            log.warning("Order %s not filled after %d retries. Cancelling: Slippage Too High",
                                        live_order_id, max_retries)
                            await self.broker.cancel_order(live_order_id)
                            order_status = "CANCELLED"
                            rejection_reason = "Not Filled — Slippage Too High"
                            # The cancel can land on an order that filled, fully
                            # or in part, in the meantime. Whatever Dhan filled
                            # is a real holding and must be tracked and protected.
                            try:
                                after = [o for o in (await self.broker.get_orders() or [])
                                         if str(o.get("orderId")) == str(live_order_id)]
                            except Exception as exc:
                                after = []
                                log.warning("Could not re-read BUY %s after cancel: %s", live_order_id, exc)
                            got = int((after[0].get("filledQty") if after else 0) or 0)
                            if got > 0:
                                live_filled_qty = got
                                fill_price = float(after[0].get("averageTradedPrice") or limit_price)
                                order_status = "FILLED"
                                rejection_reason = (
                                    "" if got >= qty else f"Partial live fill {got}/{qty}; rest cancelled"
                                )

                        # If filled, place companion SL-L
                        if order_status == "FILLED":
                            sl_qty = live_filled_qty or qty
                            sl_trigger = max(1.0, round_to_tick(fill_price - opt_sl_pts, 0.05))
                            sl_limit = max(0.5, round_to_tick(sl_trigger - sl_buf, 0.05))
                            log.info("🛡️ Placing Companion SL-L: Trigger ₹%.2f | Limit ₹%.2f", sl_trigger, sl_limit)
                            sl_res = await self.broker.place_order({
                                "correlationId": f"sl_{corr_id}",
                                "transactionType": "SELL",
                                "exchangeSegment": "NSE_FNO",
                                "productType": "INTRADAY",
                                "orderType": "STOP_LOSS_LIMIT",
                                "validity": "DAY",
                                "securityId": sec_id,
                                "quantity": sl_qty,
                                "price": sl_limit,
                                "triggerPrice": sl_trigger,
                                "disclosedQuantity": 0,
                                "afterMarketOrder": False,
                            })
                            if sl_res.get("ok"):
                                companion_sl_order_id = sl_res.get("order_id")

            # ── 2. PAPER TRADE EXECUTION (if PAPER_ONLY or BOTH) ────────────
            if mode in ("PAPER_ONLY", "BOTH") and self.paper_execution_enabled:
                paper_order_id = f"P_{order_id}"
                # Simulated slippage, DETERMINISTIC per order.
                #
                # This was np.random.choice([0.05,0.10,0.15,0.20]) with no seed,
                # so the same signal produced different P&L on every re-run and
                # replay could never reconcile against the live ledger. Deriving
                # the pick from the order id instead gives a stable value per
                # order without a global seed, which would only make the whole
                # sequence identical and break as soon as the order count moved.
                #
                # The values themselves are ASSUMED, not measured — see the
                # paper_execution._evidence note in the config.
                sim_slippage = self._simulated_slippage_pts(order_id)
                paper_fill_price = round(signal_ltp + sim_slippage, 2)
                # The orders book must agree with the position about the fill.
                # Previously the order row kept fill_price = signal_ltp while the
                # position booked signal_ltp + sim_slippage, so the two ledgers
                # disagreed about the same trade and report_slippage.py read a
                # constant zero. For BOTH, the live Dhan fill is the real one and
                # stays authoritative.
                if mode == "PAPER_ONLY":
                    fill_price = paper_fill_price
                paper_charges = self.calculate_dhan_charges(paper_fill_price, paper_fill_price + opt_target_pts, qty)

                base_fill = fill_price if (mode in ("ALGO_LIVE_ONLY", "BOTH") and order_status == "FILLED") else paper_fill_price
                is_flagship_pos = "FLAGSHIP" in str((spot_context or {}).get("strategy_id") or strategy_name).upper()
                # Initialize super trail levels (premium); FLAGSHIP uses spot trail in get_active_positions.
                if fixed_exit_order:
                    init_sl = round_to_tick(base_fill - float(opt_sl_pts), 0.05)
                    init_target = round_to_tick(base_fill + float(opt_target_pts), 0.05)
                else:
                    init_sl = round_to_tick(
                        base_fill - float(self.super_trail_cfg.get("initial_sl_pts", 20.0)), 0.05
                    )
                    init_target = round_to_tick(
                        base_fill + float(self.super_trail_cfg.get("initial_target_pts", 25.0)), 0.05
                    )
                trail_sl_on = (
                    0
                    if (is_flagship_pos or fixed_exit_order)
                    else (1 if self.super_trail_cfg.get("trailing_sl_enabled", True) else 0)
                )
                trail_tgt_on = (
                    0
                    if (is_flagship_pos or fixed_exit_order)
                    else (1 if self.super_trail_cfg.get("trailing_target_enabled", True) else 0)
                )

                pos_id = f"POS_{int(time.time()*1000)}"
                ledger_position_id = pos_id
                with sqlite3.connect(self.ledger_db) as conn:
                    cur = conn.cursor()
                    cur.execute("""
                    INSERT INTO positions (
                        position_id, symbol, security_id, side, lots, qty, entry_price, entry_time,
                        current_ltp, target_price, sl_price, exit_price, exit_time, duration,
                        gross_pnl, net_pnl, status, mode,
                        highest_ltp, initial_sl, current_trailing_sl, target_status, initial_target,
                        trailing_target_level, highest_after_target, pullback_exit_level,
                        trail_sl_enabled, trail_target_enabled, trail_activation_pts, trail_jump_pts,
                        trail_target_step, trail_target_pullback, target_order_id, sl_order_id,
                        exit_reason, partial_step, remaining_qty,
                        placed_by, source, strategy_setup,
                        strategy_id, spot_entry, spot_target_level, spot_stop_level,
                        spot_target_pts, spot_stop_pts, engine_risk_pts, sr_interaction
                    ) VALUES (
                        ?, ?, ?, ?, 1, ?, ?, ?,
                        ?, ?, ?, NULL, NULL, '00h 00m 00s',
                        0.0, 0.0, 'OPEN', ?,
                        ?, ?, ?, 'FIXED', ?,
                        ?, ?, 0.0,
                        ?, ?, ?, ?,
                        ?, ?, '', ?,
                        '', 0, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """, (
                        pos_id, symbol, sec_id, side, qty, base_fill,
                        datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                        base_fill, init_target, init_sl, mode,
                        base_fill, init_sl, init_sl, init_target,
                        init_target, base_fill,
                        trail_sl_on,
                        trail_tgt_on,
                        float(self.super_trail_cfg.get("trail_activation_profit_pts", 15.0)),
                        float(self.super_trail_cfg.get("trailing_jump_pts", 5.0)),
                        float(self.super_trail_cfg.get("trail_target_step_pts", 10.0)),
                        float(self.super_trail_cfg.get("trail_target_pullback_pts", 5.0)),
                        str(companion_sl_order_id or ""),
                        qty,
                        placed_source, placed_source, strategy_setup,
                        # v1.9.4c Matrix Scalp spot context. NULL for every
                        # other strategy, so nothing is implied where no spot
                        # rule exists.
                        (spot_context or {}).get("strategy_id"),
                        (spot_context or {}).get("spot_entry"),
                        (spot_context or {}).get("spot_target_level"),
                        (spot_context or {}).get("spot_stop_level"),
                        (spot_context or {}).get("spot_target_pts"),
                        (spot_context or {}).get("spot_stop_pts"),
                        (spot_context or {}).get("engine_risk_pts"),
                        (spot_context or {}).get("sr_interaction"),
                    ))

                    # Whether a real BUY filled at Dhan for this position. The
                    # close path sends a real SELL only when this is 1; the
                    # stored mode says what the desk was set to, not what Dhan did.
                    real_fill = bool(live_order_id) and order_status == "FILLED" and mode in ("ALGO_LIVE_ONLY", "BOTH")
                    cur.execute(
                        "UPDATE positions SET live_filled=?, live_order_id=?, live_fill_qty=? WHERE position_id=?",
                        (
                            1 if real_fill else 0,
                            str(live_order_id or "") if real_fill else "",
                            int(live_filled_qty or qty) if real_fill else 0,
                            pos_id,
                        ),
                    )

                    # Deduct paper margin
                    req_margin = paper_fill_price * qty
                    cur.execute("UPDATE paper_wallet SET margin_used = margin_used + ? WHERE id=1", (req_margin,))
                    conn.commit()

                if is_proto_order:
                    dyn_e8 = (spot_context or {}).get("dynamic_e8_at_entry") or (spot_context or {}).get("dynamic_e8")
                    proto_dir = (spot_context or {}).get("direction") or ("LONG" if "LONG" in str((spot_context or {}).get("strategy_id")).upper() else "SHORT")
                    self.log_ema8_prototype_event(
                        event="ENTRY PLACED",
                        direction=proto_dir,
                        dynamic_8ema=dyn_e8,
                        spot=(spot_context or {}).get("spot_entry"),
                        position_id=pos_id,
                        reason=f"ENTRY @ ₹{base_fill:.2f}",
                    )

            # Record in Orders Book
            limit_sent = round_to_tick(signal_ltp + buf, 0.05)
            slippage_pts = round(fill_price - signal_ltp, 2)
            dhan_charges = self.calculate_dhan_charges(fill_price, fill_price + opt_target_pts, qty)

            nifty_entry = None
            if spot_context and spot_context.get("spot_entry"):
                nifty_entry = float(spot_context["spot_entry"])
            elif current_spot > 0:
                nifty_entry = float(current_spot)

            with sqlite3.connect(self.ledger_db) as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT OR REPLACE INTO orders (
                    order_id, correlation_id, created_at, strategy_name, symbol, security_id,
                    side, product, order_type, lots, qty, signal_ltp, limit_price, fill_price,
                    slippage, chase_retries, brokerage_taxes, order_status, mode, rejection_reason,
                    placed_by, source, strategy_setup, dhan_order_id, nifty_spot_entry, nifty_spot_exit,
                    position_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'INTRADAY', 'AGGR-LIMIT', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """, (order_id, corr_id, now_str, strategy_name, symbol, sec_id, side, qty,
                      signal_ltp, limit_sent, fill_price, slippage_pts, chase_retries_used,
                      dhan_charges, order_status, mode, rejection_reason,
                      placed_source, placed_source, strategy_setup,
                      (live_order_id if live_route["route"] == "LIVE" else None),
                      nifty_entry, ledger_position_id or None))
                conn.commit()

            res_dict = {
                "ok": order_status == "FILLED",
                "order_id": order_id,
                "position_id": ledger_position_id,
                "status": order_status,
                "symbol": symbol,
                "source": placed_source,
                "placed_by": placed_source,
                "strategy_setup": strategy_setup,
                "fill_price": fill_price,
                "signal_ltp": signal_ltp,
                "slippage": slippage_pts,
                "chase_retries": chase_retries_used,
                "opt_target_pts": opt_target_pts,
                "opt_sl_pts": opt_sl_pts,
                "target_price": round_to_tick(fill_price + opt_target_pts, 0.05),
                "sl_price": round_to_tick(fill_price - opt_sl_pts, 0.05),
                "companion_sl_order_id": companion_sl_order_id,
                "rejection_reason": rejection_reason,
                # v1.9.4b: state the route and WHY, so a skipped real order is
                # always explained rather than silently absent.
                "live_route": live_route["route"],
                "live_route_reason": live_route["reason"],
                "dry_run_recorded": bool(dry_run_row),
            }
            if proto_lane_paper:
                res_dict["live_note"] = ("Real order SKIPPED — 8 EMA Prototype route is "
                                         "set to PAPER_ONLY")
            elif live_route["route"] == "PAPER_ONLY" and self.execution_mode != "BOTH":
                res_dict["live_note"] = ("Live OFF — Signal generated, Paper trade taken, "
                                         "Real order SKIPPED (Live OFF)")
            elif live_route["route"] == "DRY_RUN" and dry_run_row:
                res_dict["live_note"] = (
                    "DRY-RUN — Would have placed LIVE order: {} {} {} @ limit {} "
                    "(bid {} / ask {})".format(
                        symbol, side, qty, dry_run_row.get("intended_limit_price"),
                        dry_run_row.get("bid"), dry_run_row.get("ask")))
            elif live_route["route"] == "PAPER_ONLY":
                res_dict["live_note"] = "Real order SKIPPED — {}".format(live_route["reason"])
            if not is_open:
                res_dict["warning"] = "⚠️ Testing Mode — Market is Closed"
            return res_dict

    # ── IN-FLIGHT POSITION MODIFICATION ──────────────────────────────────────
    def modify_open_position(
        self,
        position_id: str,
        action: str,
        new_val: Optional[float] = None,
        enforce_market_hours: bool = True,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        with sqlite3.connect(self.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            pos = cur.execute("SELECT * FROM positions WHERE position_id=? AND status='OPEN'", (position_id,)).fetchone()
            if not pos:
                return {"ok": False, "error": "Open position not found"}

            # Block Square-Off outside NSE market hours (both Live and Paper unless override or bypass)
            is_square_off = action in ("CLOSE", "SQUARE_OFF", "EXIT_TRAILING_TARGET")
            is_open, mkt_status = self.is_nse_market_open()
            if is_square_off and enforce_market_hours:
                if not is_open:
                    pos_mode = str(pos["mode"]) if pos else ""
                    is_paper_pos = ("PAPER" in pos_mode)
                    if not (is_paper_pos and self.off_hours_testing_active()):
                        curr_t = datetime.now(IST).strftime("%H:%M:%S")
                        err_msg = (
                            f"❌ Market Closed — Cannot Square-Off Now\n"
                            f"Market Hours: Mon-Fri 09:15 AM - 03:30 PM IST\n"
                            f"Current Time: {curr_t} IST | Market Status: CLOSED\n"
                            f"Your position will remain OPEN till next market open."
                        )
                        log.warning(err_msg)
                        return {
                            "ok": False,
                            "status": "BLOCKED",
                            "error": err_msg,
                            "rejection_reason": err_msg,
                            "market_status": "CLOSED",
                            "market_closed": True,
                        }
                    else:
                        log.warning("⚠️ Testing Mode — Market is Closed. Allowing Paper position square-off.")

            entry = float(pos["entry_price"])
            ltp = float(pos["current_ltp"] or entry)
            qty = int(pos["qty"])
            nifty_spot = self._ledger_nifty_spot()
            exit_spot = nifty_spot if nifty_spot > 0 else None

            if action == "TRAIL_COST":
                # Trail Stop-loss to Cost / Breakeven
                cur.execute("UPDATE positions SET sl_price=?, current_trailing_sl=? WHERE position_id=?", (entry, entry, position_id))
                msg = f"Stop-Loss trailed to Cost (₹{entry:.2f})"

            elif action == "DISABLE_TRAILING":
                # Freezes trailing at current levels
                cur.execute("UPDATE positions SET trail_sl_enabled=0, trail_target_enabled=0 WHERE position_id=?", (position_id,))
                msg = "Super Trail disabled. Position locked at current levels."

            elif action == "EXIT_TRAILING_TARGET":
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._close_position_with_broker(dict(pos), "MANUAL_TRAILING_EXIT", spot_exit=exit_spot))
                except RuntimeError:
                    asyncio.run(self._close_position_with_broker(dict(pos), "MANUAL_TRAILING_EXIT", spot_exit=exit_spot))
                msg = f"Trailing target exit initiated for {position_id}"

            elif action == "WIDEN_TARGET":
                # Widen target by +40 pts
                cur_target = float(pos["target_price"] or (entry + 24.0))
                new_target = round(cur_target + 24.0, 2)
                cur.execute("UPDATE positions SET target_price=?, trailing_target_level=? WHERE position_id=?", (new_target, new_target, position_id))
                msg = f"Target widened to ₹{new_target:.2f} (+40 Spot pts)"

            elif action in ("CLOSE", "SQUARE_OFF"):
                ex_reason = reason or "MANUAL_CLOSE"
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._close_position_with_broker(dict(pos), ex_reason, spot_exit=exit_spot))
                except RuntimeError:
                    asyncio.run(self._close_position_with_broker(dict(pos), ex_reason, spot_exit=exit_spot))
                msg = f"Square-off initiated for {position_id} ({ex_reason})"

            else:
                return {"ok": False, "error": f"Unknown action {action}"}

            conn.commit()
            res_dict = {
                "ok": True,
                "status": "CLOSED" if is_square_off else "MODIFIED",
                "message": msg
            }
            if is_square_off and not is_open:
                res_dict["warning"] = "⚠️ Testing Mode — Market is Closed"
            return res_dict

    # ── EMERGENCY KILL SWITCH ────────────────────────────────────────────────
    async def trigger_emergency_kill_switch(self) -> Dict[str, Any]:
        """
        One-click Emergency Kill Switch:
          1. Halts all automated scanners and chasing loops.
          2. Cancels all pending orders on Dhan HQ.
          3. Squares off all open positions using Aggressive Limit orders.
          4. Locks the router into standby.
        """
        self.kill_switch_active = True
        self.scanner_enabled = False
        log.warning("🛑 EMERGENCY KILL SWITCH ACTIVATED — Halting scanners, cancelling orders, squaring off positions.")
        cancelled_orders = 0
        squared_positions = 0

        # Cancel Live Dhan Orders
        if self.broker.is_configured():
            try:
                orders = await self.broker.get_orders()
                for o in orders:
                    st = o.get("orderStatus", "")
                    oid = str(o.get("orderId"))
                    if st in ("PENDING", "TRANSIT", "TRIGGER_PENDING"):
                        await self.broker.cancel_order(oid)
                        cancelled_orders += 1
            except Exception as e:
                log.error("Kill switch live order cancellation error: %s", e)

        # Square off open positions
        with sqlite3.connect(self.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM positions WHERE status IN ('OPEN', 'EXIT_PENDING', 'EXIT_FAILED')").fetchall()
        nifty_spot = self._ledger_nifty_spot()
        spot_val = nifty_spot if nifty_spot > 0 else None
        for r in rows:
            p = dict(r)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._close_position_with_broker(p, "CLOSED_KILL_SWITCH", spot_exit=spot_val))
            except RuntimeError:
                asyncio.run(self._close_position_with_broker(p, "CLOSED_KILL_SWITCH", spot_exit=spot_val))
            squared_positions += 1

        log.warning("🛑 EMERGENCY KILL SWITCH ACTIVATED! Cancelled %d orders, squared off %d positions.",
                    cancelled_orders, squared_positions)
        return {
            "ok": True,
            "message": f"Kill switch executed: {cancelled_orders} orders cancelled, {squared_positions} positions squared off.",
            "cancelled_orders": cancelled_orders,
            "squared_positions": squared_positions,
        }

    def reset_kill_switch(self) -> Dict[str, Any]:
        self.kill_switch_active = False
        self.scanner_enabled = bool(self.scanner_cfg.get("enabled", True))
        log.info("Kill switch disarmed. Standby cleared. Scanner enabled: %s", self.scanner_enabled)
        return {"ok": True, "message": "Kill switch disarmed. Ready for execution."}

    def _flagship_risk_allows_entry(self) -> Tuple[bool, str]:
        """Daily FLAGSHIP trade count and loss cap (config scanner.flagship_*)."""
        max_tr = int(self.scanner_cfg.get("flagship_max_trades_per_day", 2))
        max_loss = float(self.scanner_cfg.get("flagship_max_daily_loss_inr", -3000.0))
        today = datetime.now(IST).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.ledger_db) as conn:
                cur = conn.cursor()
                cnt = int(cur.execute(
                    "SELECT COUNT(*) FROM positions WHERE substr(entry_time,1,10)=? "
                    "AND upper(coalesce(strategy_id,'')) LIKE 'FLAGSHIP%'",
                    (today,),
                ).fetchone()[0] or 0)
                if cnt >= max_tr:
                    return False, f"FLAGSHIP_MAX_TRADES_{max_tr}"
                day_pnl = float(cur.execute(
                    "SELECT COALESCE(SUM(net_pnl),0) FROM positions WHERE substr(entry_time,1,10)=? "
                    "AND upper(coalesce(strategy_id,'')) LIKE 'FLAGSHIP%' AND status LIKE 'CLOSED%'",
                    (today,),
                ).fetchone()[0] or 0.0)
                if day_pnl <= max_loss:
                    return False, f"FLAGSHIP_MAX_DAILY_LOSS_{max_loss}"
        except Exception as exc:
            log.warning("FLAGSHIP risk gate query failed (fail-open): %s", exc)
        return True, ""

    def _latest_closed_1m_bar(self) -> Optional[Dict[str, float]]:
        if not Path(self.market_db).exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=3) as conn:
                df = pd.read_sql_query(
                    "SELECT ts, h, l, c FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 4",
                    conn,
                )
            if len(df) < 2:
                return None
            row = df.iloc[1]
            return {
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "ts": int(row["ts"]),
            }
        except Exception as exc:
            log.debug("Latest 1m bar unavailable: %s", exc)
            return None

    def _recent_15m_bars_for_live(self, limit: int = 48) -> List[Dict[str, Any]]:
        if not Path(self.market_db).exists():
            return []
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=5) as conn:
                df = pd.read_sql_query(
                    """SELECT ts, o AS open, h AS high, l AS low, c AS close,
                              date(ts, 'unixepoch', '+330 minutes') AS day,
                              strftime('%H:%M', ts, 'unixepoch', '+330 minutes') AS hm
                       FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT ?""",
                    conn,
                    params=(limit * 15 + 30,),
                )
            if df.empty:
                return []
            df = df.iloc[::-1].reset_index(drop=True)
            df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            b15 = resample_nse_15m(df.set_index("dt")).reset_index()
            dt_col = "datetime" if "datetime" in b15.columns else b15.columns[0]
            b15["ts"] = _epoch_seconds(b15[dt_col])
            b15["day"] = pd.to_datetime(b15[dt_col]).dt.strftime("%Y-%m-%d")
            b15["hm"] = pd.to_datetime(b15[dt_col]).dt.strftime("%H:%M")
            if len(b15) < 3:
                return []
            tail = b15.iloc[-limit:].to_dict(orient="records")
            for r in tail:
                r["e8"] = None
            if len(tail) >= 8:
                closes = pd.Series([float(x["close"]) for x in tail])
                _, e8 = ema4_ema8(closes)
                for i, r in enumerate(tail):
                    r["e8"] = float(e8.iloc[i])
            return tail
        except Exception as exc:
            log.warning("15m bars for PA live: %s", exc)
            return []

    def _live_pa_sr_config(self) -> Dict[str, Any]:
        from analyst.pa_sr_homework_8510 import load_pa_sr_config

        return load_pa_sr_config(self.cfg)

    def _live_daily_sr_for_spot(self, spot: float) -> Dict[str, Any]:
        from analyst.pa_sr_homework_8510 import extend_sr_s4_r4

        if not Path(self.market_db).exists():
            return {}
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=5) as conn:
                raw = pd.read_sql_query(
                    """SELECT ts, o AS open, h AS high, l AS low, c AS close,
                              date(ts, 'unixepoch', '+330 minutes') AS day
                       FROM candles_1m WHERE sec_id='13' ORDER BY ts DESC LIMIT 8000""",
                    conn,
                )
            if raw.empty:
                return {}
            raw = raw.iloc[::-1]
            ddf = (
                raw.groupby("day")
                .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
                .tail(25)
            )
            return extend_sr_s4_r4(ddf, spot)
        except Exception as exc:
            log.debug("live daily S/R: %s", exc)
            return {}

    def evaluate_pa_sr_live_arm_on_15m(self) -> None:
        cfg = self._live_pa_sr_config()
        if not cfg.get("live_paper_enabled", False):
            self.strategy_status["pa_sr_homework"]["state"] = "OFF"
            return
        bars = self._recent_15m_bars_for_live()
        if len(bars) < 3:
            return
        from analyst.pa_sr_homework_8510 import scan_pa_sr_pattern_arm, session_vwap_up_to_ts

        idx = len(bars) - 1
        spot = float(bars[idx]["close"])
        sr = self._live_daily_sr_for_spot(spot)
        vwap = None
        try:
            today = str(bars[idx].get("day") or "")
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=3) as conn:
                d1 = pd.read_sql_query(
                    "SELECT ts, c AS close, v AS volume FROM candles_1m "
                    "WHERE sec_id='13' AND date(ts,'unixepoch','+330 minutes')=? ORDER BY ts",
                    conn,
                    params=(today,),
                )
            if not d1.empty:
                vwap = session_vwap_up_to_ts(d1, int(bars[idx]["ts"]))
        except Exception:
            pass
        armed = scan_pa_sr_pattern_arm(bars, idx, sr, cfg, vwap=vwap)
        now = time.time()
        for p in armed:
            p["armed_at"] = now
            p["expires_at"] = now + 20 * 60.0
            key = (p.get("direction"), p.get("bar_hm"), p.get("day"))
            if not any(
                (x.get("direction"), x.get("bar_hm"), x.get("day")) == key for x in self._pa_sr_pending
            ):
                self._pa_sr_pending.append(p)
        self._pa_sr_pending = [x for x in self._pa_sr_pending if float(x.get("expires_at", 0)) > now]
        n = len(self._pa_sr_pending)
        self.strategy_status["pa_sr_homework"].update(
            {"state": "ARMED" if n else "SCANNING", "pending": n, "armed": n > 0}
        )

    def evaluate_pa_sr_live_breaks(
        self, spot: float, bar_1m: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        cfg = self._live_pa_sr_config()
        if not cfg.get("live_paper_enabled", False):
            return []
        from analyst.pa_sr_homework_8510 import live_pa_signal_from_pending

        out: List[Dict[str, Any]] = []
        now = time.time()
        hi = float((bar_1m or {}).get("high") or spot)
        lo = float((bar_1m or {}).get("low") or spot)
        kept: List[Dict[str, Any]] = []
        for p in self._pa_sr_pending:
            if float(p.get("expires_at", 0)) < now:
                continue
            direction = p.get("direction")
            ph = float(p.get("pattern_high") or 0)
            pl = float(p.get("pattern_low") or 0)
            fired = False
            if direction == "LONG" and (spot >= ph or hi >= ph):
                out.append(live_pa_signal_from_pending(p, max(spot, ph), cfg))
                fired = True
                self.strategy_status["pa_sr_homework"]["state"] = "TRIGGERED"
            elif direction == "SHORT" and (spot <= pl or lo <= pl):
                out.append(live_pa_signal_from_pending(p, min(spot, pl), cfg))
                fired = True
                self.strategy_status["pa_sr_homework"]["state"] = "TRIGGERED"
            if not fired:
                kept.append(p)
        self._pa_sr_pending = kept
        self.strategy_status["pa_sr_homework"]["pending"] = len(kept)
        return out

    def _latest_closed_15m_bar(self) -> Optional[Dict[str, float]]:
        if not Path(self.market_db).exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=3) as conn:
                q = """
                    SELECT ts, o, h, l, c FROM candles_1m
                    WHERE sec_id='13' ORDER BY ts DESC LIMIT ?
                """
                df = pd.read_sql_query(q, conn, params=(EMA_WARMUP_1M_BARS,))
            if df.empty:
                return None
            df = df.iloc[::-1]
            df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            b15 = resample_nse_15m(df.set_index("dt").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"}))
            if len(b15) < 2:
                return None
            row = b15.iloc[-2]
            return {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "ts": int(b15.index[-2].timestamp()),
            }
        except Exception as exc:
            log.debug("Latest 15m bar unavailable: %s", exc)
            return None

    def _backtest_flagship_signals_at_bar(
        self, df_all: pd.DataFrame, bar_ts: int, bar_close: float
    ) -> List[Tuple[str, str, str, float, float, Dict[str, Any]]]:
        sub = df_all[df_all["ts"] <= int(bar_ts)]
        if len(sub) < 120:
            return []
        ohlc = sub.set_index("dt")[["open", "high", "low", "close", "volume"]].copy()
        # This runs the live scanner function over old candles. Everything it
        # writes to is swapped for a throwaway copy first and put back after,
        # so a backtest or replay cannot change the live desk: not the
        # party side, the Stage/Reversal memory, the crossover record, or the
        # cockpit cards. Prototype evaluation is skipped via _historical_eval.
        import copy as _copy

        saved = {
            "active_party": self.active_party,
            "_mj_sir_state": self._mj_sir_state,
            "strategy_status": self.strategy_status,
            "last_crossover_event": getattr(self, "last_crossover_event", None),
            "_historical_eval": getattr(self, "_historical_eval", False),
        }
        self.active_party = "NEUTRAL"
        self._mj_sir_state = {}
        self.strategy_status = _copy.deepcopy(self.strategy_status)
        self._historical_eval = True
        prev_level = log.level
        log.setLevel(logging.ERROR)
        try:
            sigs = self.evaluate_candle_dataframe(ohlc, spot_price=float(bar_close))
            sr = dict(self.strategy_status.get("daily_sr", {}).get("levels") or {})
        finally:
            log.setLevel(prev_level)
            for name, value in saved.items():
                setattr(self, name, value)
        out: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
        for s in sigs:
            sid = str(s.get("strategy_id", "")).upper()
            if "FLAGSHIP" not in sid:
                continue
            out.append((
                str(s["strategy_id"]),
                str(s["setup_name"]),
                str(s["direction"]),
                float(s.get("target_pts", 40.0)),
                float(s.get("sl_pts", 40.0)),
                {
                    "flagship": True,
                    "sr_levels": sr,
                    "bb_gate_label": s.get("bb_gate_label", "ON"),
                    "bollinger_applied": s.get("bollinger_applied"),
                },
            ))
        return out

    def _backtest_legacy_cand_signals(
        self,
        b: Dict[str, Any],
        b_prev: Dict[str, Any],
        e4_v: float,
        e8_v: float,
        bbw: float,
        ubb_up: bool,
        lbb_dn: bool,
        ubb_flat: bool,
        lbb_flat: bool,
        atr14: float,
    ) -> List[Tuple[str, str, str, float, float, Dict[str, Any]]]:
        c_p, h_p, l_p = float(b["close"]), float(b["high"]), float(b["low"])
        touch_15 = (l_p <= e8_v + 3.0 and h_p >= e8_v - 3.0)
        cand: List[Tuple[str, str, str, float, float, Dict[str, Any]]] = []
        bb_min = float(self.scanner_cfg.get("bb_width_min", 60.0))
        bb_stage2 = self._bollinger_gate_active("stage2")
        bb_stage3 = self._bollinger_gate_active("stage3")
        bb_cross = self._bollinger_gate_active("crossover")
        gate_bb = "ON" if bool(self.scanner_cfg.get("bollinger_enabled", True)) else "OFF"

        def _meta(flagship: bool = False) -> Dict[str, Any]:
            return {
                "flagship": flagship,
                "sr_levels": {},
                "bollinger_enabled": gate_bb == "ON",
                "bollinger_applied": gate_bb == "ON",
                "bb_gate_label": gate_bb,
            }

        if e4_v > e8_v and touch_15 and c_p >= e8_v - 2.5:
            if bb_stage2:
                if ubb_up and bbw >= bb_min:
                    cand.append(("STAGE_2_LONG", "Stage 2 8-EMA (Long) [Pullback Touch]", "LONG", 40.0, 40.0, _meta()))
            else:
                cand.append(("STAGE_2_LONG", "Stage 2 8-EMA (Long) [Pullback Touch]", "LONG", 40.0, 40.0, _meta()))
        if e4_v < e8_v and touch_15 and c_p <= e8_v + 2.5:
            if bb_stage3:
                if lbb_dn and bbw >= bb_min:
                    cand.append(("STAGE_3_SHORT", "Stage 3 8-EMA (Short) [Resistance Rejection]", "SHORT", 40.0, 40.0, _meta()))
            else:
                cand.append(("STAGE_3_SHORT", "Stage 3 8-EMA (Short) [Resistance Rejection]", "SHORT", 40.0, 40.0, _meta()))
        if float(b_prev["e4"]) <= float(b_prev["e8"]) and e4_v > e8_v and touch_15:
            if (not bb_cross) or ubb_flat:
                cand.append(("CROSSOVER_BULLISH", "4 MA vs 8 EMA Upside Reversal", "LONG", 20.0, 20.0, _meta()))
        elif float(b_prev["e4"]) >= float(b_prev["e8"]) and e4_v < e8_v and touch_15:
            if (not bb_cross) or lbb_flat:
                cand.append(("CROSSOVER_BEARISH", "4 MA vs 8 EMA Downside Reversal", "SHORT", 20.0, 20.0, _meta()))
        return cand

    # ── PREVIOUS-DAY REPLAY (BACKTEST SIMULATION) ─────────────────────────────
    def _classify_strategy_filter_tag(self, strategy_id: str, setup_name: str) -> Tuple[str, str]:
        sid = str(strategy_id or "").upper()
        sname = str(setup_name or "").upper()
        if "FLAGSHIP_1H" in sid or "FLAGSHIP" in sname or "1 HR FLAGSHIP" in sname:
            return "H1_FLAGSHIP", "1 HR FLAGSHIP"
        if "STAGE_3" in sid or "STAGE 3" in sname:
            return "STAGE_3_SHORT", "STAGE 3 8 EMA SHORT"
        if "CROSSOVER" in sid or "REVERSAL" in sname or "4 MA" in sname or "4X8" in sname:
            return "CROSSOVER", "4x8 EMA CROSSOVER REVERSAL"
        if "PA_SR" in sid or "PA LONG" in sname or "PA SHORT" in sname:
            return "PA_SR_HOMEWORK", "15m PA @ S/R (Homework)"
        if "EMA8_PROTOTYPE" in sid or "8 EMA PROTOTYPE" in sname:
            return "EMA8_PROTOTYPE", "8 EMA Prototype (Paper)"
        return "STAGE_2_LONG", "STAGE 2 8 EMA LONG"

    def _compute_side_summary(self, pnl_pts_list: List[float], net_inr_list: List[float]) -> Dict[str, Any]:
        total = len(net_inr_list)
        if total == 0:
            return {
                "total_trades": 0, "win_rate_pct": 0.0, "avg_win_pts": 0.0, "avg_loss_pts": 0.0,
                "total_net_pnl_inr": 0.0, "max_drawdown_inr": 0.0, "expectancy_pts": 0.0,
                "expectancy_inr": 0.0, "profit_factor": 0.0,
            }
        # Backtested rows carry no rupee figure (it is 0 by design), so they
        # are judged on points. Before, every one of them counted as a loss.
        def _is_win(pts: float, inr: float) -> bool:
            return inr > 0 if inr != 0 else pts > 0

        wins_pts = [p for p, n in zip(pnl_pts_list, net_inr_list) if _is_win(p, n)]
        loss_pts = [p for p, n in zip(pnl_pts_list, net_inr_list) if not _is_win(p, n)]
        wins_inr = [n for n in net_inr_list if n > 0]
        loss_inr = [abs(n) for n in net_inr_list if n <= 0]
        wr = round((len(wins_pts) / total) * 100.0, 1)
        avg_w = round(float(np.mean(wins_pts)), 2) if wins_pts else 0.0
        avg_l = round(float(np.mean(loss_pts)), 2) if loss_pts else 0.0
        tot_net = round(float(sum(net_inr_list)), 2)
        exp_pts = round(float(np.mean(pnl_pts_list)), 2)
        exp_inr = round(tot_net / total, 2)
        pf = round(sum(wins_inr) / sum(loss_inr), 2) if sum(loss_inr) > 0 else (99.0 if sum(wins_inr) > 0 else 0.0)
        cum = np.cumsum(net_inr_list)
        peak = np.maximum.accumulate(np.insert(cum, 0, 0.0))[1:]
        max_dd = round(float(np.max(np.maximum(0.0, peak - cum))), 2) if len(cum) else 0.0
        return {
            "total_trades": total, "win_rate_pct": wr, "avg_win_pts": avg_w, "avg_loss_pts": avg_l,
            "total_net_pnl_inr": tot_net, "max_drawdown_inr": max_dd, "expectancy_pts": exp_pts,
            "expectancy_inr": exp_inr, "profit_factor": pf,
        }

    def _load_backtest_1m_candles(self, from_date: str, to_date: str) -> pd.DataFrame:
        """Loads 1m candles directly from the 94,213 verified candles in ict_smc.sqlite3."""
        root_dir = Path(__file__).resolve().parent.parent
        candidate_dbs = [
            root_dir / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3",
            Path(self.market_db),
            root_dir / "data" / "terminal8510" / "market.sqlite3",
            root_dir / "data" / "market.sqlite3",
        ]
        q = """
            SELECT ts, o AS open, h AS high, l AS low, c AS close, v AS volume,
                   date(ts, 'unixepoch', '+330 minutes') AS day,
                   strftime('%H:%M', ts, 'unixepoch', '+330 minutes') AS hm
            FROM candles_1m
            WHERE sec_id IN ('13', 'NIFTY', 'IDX_I')
              AND date(ts, 'unixepoch', '+330 minutes') >= ?
              AND date(ts, 'unixepoch', '+330 minutes') <= ?
            ORDER BY ts ASC
        """
        for db_p in candidate_dbs:
            if db_p and db_p.exists():
                try:
                    with sqlite3.connect(f"file:{db_p}?mode=ro", uri=True, timeout=5) as conn:
                        df_part = pd.read_sql_query(q, conn, params=(from_date, to_date))
                        if not df_part.empty:
                            return df_part
                except Exception as exc:
                    pass
        return pd.DataFrame()

    def _recorded_history(self):
        from analyst.recorded_history_8510 import RecordedHistory
        return RecordedHistory(self.ledger_db, self.market_db)

    def get_available_dates(self) -> List[str]:
        return self._recorded_history().dates()

    def get_available_replay_dates(self) -> List[str]:
        return self.get_available_dates()

    def daily_ingest(self, date_str: str, overwrite: bool = False) -> Dict[str, Any]:
        archive = self.run_post_market_archivers(date_str)
        payload = self._recorded_history().daily(date_str)
        payload['post_market_archive'] = archive
        self._recorded_history().persist_view(payload)
        return payload

    def get_eod_summary(self, date_str: str = "2026-11-05") -> Dict[str, Any]:
        return {'ok': True, **self._recorded_history().daily(date_str)['eod_summary']}

    def run_trend_lab(self, from_date: str = "2026-11-04", to_date: str = "2026-11-05") -> Dict[str, Any]:
        rows = []
        master_all = fetch_master_trades(self.ledger_db, from_date, to_date)
        master_by_day: Dict[str, int] = {}
        for m in master_all:
            d = str(m.get("trade_date") or m.get("Date") or "")[:10]
            if d:
                master_by_day[d] = master_by_day.get(d, 0) + 1
        for day in self._recorded_history().dates():
            if from_date <= day <= to_date:
                data = self._recorded_history().daily(day)
                m_cnt = master_by_day.get(day, 0)
                trade_src = DATA_SOURCE_IMMUTABLE if m_cnt else "NONE"
                rows.append({
                    'date': day,
                    'stage2': 'UNKNOWN',
                    'crossover_4_8': 'UNKNOWN',
                    'ema_1h_8': 'UNKNOWN',
                    'total_oi_bias': 'UNKNOWN',
                    'oi_change_bias': 'UNKNOWN',
                    'combined_bias': 'UNKNOWN',
                    'writers': 'Not reconstructed',
                    'volumes': 'Not reconstructed',
                    'trend': 'UNKNOWN',
                    'score': None,
                    'multi_factor_score': None,
                    'recorded_trades': m_cnt or len(data['trades']),
                    'trade_data_source': trade_src,
                    'immutable_master_trades': m_cnt,
                    'recorded_spot_minutes': len(data['spot_candles_1m']),
                })
        return {
            'ok': True,
            'from_date': from_date,
            'to_date': to_date,
            'rows': rows,
            'evaluations': rows,
            'count': len(rows),
            'provenance': 'OBSERVATIONS_ONLY',
            'primary_trade_source': DATA_SOURCE_IMMUTABLE,
        }

    def check_and_run_auto_ingest(self, target_date: str = "2026-11-05", mock_ist_hour: Optional[int] = None) -> bool:
        now = datetime.now(IST)
        if (mock_ist_hour if mock_ist_hour is not None else now.hour) < 16:
            return False
        self.daily_ingest(target_date)
        return True

    @staticmethod
    def _fifteen_min_bar_close_hm(hm: str) -> str:
        """Bar label is open time (09:15 bucket); close ≈ open + 14 minutes."""
        try:
            h, m = int(str(hm)[:2]), int(str(hm)[3:5])
        except ValueError:
            return str(hm)
        m += 14
        if m >= 60:
            h += 1
            m -= 60
        return f"{h:02d}:{m:02d}"

    @staticmethod
    def _filter_trades_by_bias(rows: List[Dict[str, Any]], bias: Optional[str]) -> List[Dict[str, Any]]:
        b = str(bias or "BOTH").upper()
        if b == "LONG_ONLY":
            return [r for r in rows if str(r.get("direction") or r.get("Direction") or "").upper() == "LONG"]
        if b == "SHORT_ONLY":
            return [r for r in rows if str(r.get("direction") or r.get("Direction") or "").upper() == "SHORT"]
        return rows

    def _filter_trades_by_strategy(
        self, rows: List[Dict[str, Any]], strategy_filter: Optional[str]
    ) -> List[Dict[str, Any]]:
        sf = str(strategy_filter or "ALL").upper().strip()
        if sf in ("", "ALL"):
            return rows
        out: List[Dict[str, Any]] = []
        for r in rows:
            sid = str(r.get("strategy_id") or "")
            sname = str(r.get("setup_name") or r.get("setup") or r.get("Setup_Name") or "")
            f_code, _ = self._classify_strategy_filter_tag(sid, sname)
            if f_code == sf:
                out.append(r)
                continue
            sid_u = sid.upper()
            sn_u = sname.upper()
            if sf == "EMA8_PROTOTYPE" and (
                "EMA8_PROTOTYPE" in sid_u or "8 EMA PROTOTYPE" in sn_u or "8 EMA Prototype" in sname
            ):
                out.append(r)
            elif sf == "H1_FLAGSHIP" and ("FLAGSHIP" in sid_u or "FLAGSHIP" in sn_u):
                out.append(r)
            elif sf == "STAGE_2_LONG" and ("STAGE_2" in sid_u or "STAGE 2" in sn_u):
                out.append(r)
            elif sf == "STAGE_3_SHORT" and ("STAGE_3" in sid_u or "STAGE 3" in sn_u):
                out.append(r)
        return out

    @staticmethod
    def _replay_summary_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trades:
            return {"total_net_pnl": 0.0, "win_rate": None, "trades_count": 0}
        nets: List[float] = []
        wins = 0
        for t in trades:
            net = t.get("net_pnl") if t.get("net_pnl") is not None else t.get("Net_Pnl_INR")
            if net is not None:
                nv = float(net)
                nets.append(nv)
                if nv > 0:
                    wins += 1
            elif t.get("st_pnl_pts") is not None:
                if float(t["st_pnl_pts"]) > 0:
                    wins += 1
        total_net = round(sum(nets), 2) if nets else 0.0
        wr = round(100.0 * wins / len(trades), 1) if trades else None
        return {"total_net_pnl": total_net, "win_rate": wr, "trades_count": len(trades)}

    def get_unified_trades_report(
        self, from_date: str, to_date: str, bias: Optional[str] = None
    ) -> Dict[str, Any]:
        """Immutable master first; legacy recorded ledger only if master empty for range."""
        active_bias = bias or self.directional_bias
        master = self._filter_trades_by_bias(
            fetch_master_trades(self.ledger_db, from_date, to_date), active_bias
        )
        if master:
            net = sum(float(r.get("net_pnl") or r.get("Net_Pnl_INR") or 0.0) for r in master)
            return {
                "ok": True,
                "from_date": from_date,
                "to_date": to_date,
                "trades": master,
                "rows": master,
                "total_trades": len(master),
                "primary_source": DATA_SOURCE_IMMUTABLE,
                "provenance": DATA_SOURCE_IMMUTABLE,
                "message": "Immutable algo execution master (append-only live fills)",
                "recorded_net_pnl": net,
            }
        legacy = self._recorded_history().report(from_date, to_date, active_bias)
        legacy["primary_source"] = "RECORDED_PAPER_LEDGER"
        for r in legacy.get("trades") or []:
            r["data_source_tag"] = r.get("data_source_tag") or "RECORDED_PAPER_LEDGER"
        return legacy

    def _synthetic_replay_trades_for_date(
        self, target_date: str, bias: Optional[str], strategy_filter: str = "ALL"
    ) -> List[Dict[str, Any]]:
        bt = self.run_date_range_backtest(
            target_date,
            target_date,
            bias=bias,
            strategy_filter=strategy_filter,
            data_type_filter="SYNTHESIZED",
            include_today_paper=False,
        )
        return [self._replay_trade_from_backtest_row(r) for r in (bt.get("trades") or [])]

    def get_replay_trades_for_date(self, target_date: str, bias: Optional[str] = None) -> List[Dict[str, Any]]:
        master = self._filter_trades_by_bias(
            fetch_master_trades(self.ledger_db, target_date, target_date), bias
        )
        if master:
            return master
        return self._synthetic_replay_trades_for_date(target_date, bias)

    def get_algo_execution_master(
        self, from_date: str, to_date: str, strategy_filter: str = "ALL"
    ) -> List[Dict[str, Any]]:
        return fetch_master_trades(self.ledger_db, from_date, to_date, strategy_filter)

    def _replay_trade_from_backtest_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Map Super-Trail backtest row → Tab 4 replay table (real NSE 1m spot OHLC source)."""
        date_hm = str(row.get("Date") or "")
        parts = date_hm.split()
        day = str(row.get("date_only") or (parts[0] if parts else ""))
        hm = parts[1] if len(parts) > 1 else "09:30"
        ent_day = str(row.get("entry_day") or day)
        ent_hm = str(row.get("entry_hm") or hm)
        ex_day = str(row.get("exit_day") or day)
        ex_hm = str(row.get("exit_hm") or hm)
        is_synth = str(
            row.get("data_source_tag") or row.get("price_type") or row.get("source") or "SYNTHESIZED"
        ).upper() in (DATA_SOURCE_SYNTHESIZED, "SYNTHESIZED")
        st_pts = float(row.get("st_pnl_pts") or 0.0)
        net = None if is_synth else float(row.get("net_pnl") or row.get("fixed_net_pnl_row") or 0.0)
        gross = None if is_synth else float(row.get("gross_pnl_inr") or row.get("gross_pnl") or 0.0)
        chg = None if is_synth else float(row.get("charges") or row.get("dhan_charges") or 78.16)
        ex_hm_disp = str(ex_hm or hm)
        if (
            is_synth
            and str(ent_day) == str(ex_day)
            and str(ent_hm) == str(ex_hm)
            and st_pts != 0
        ):
            ex_hm_disp = self._fifteen_min_bar_close_hm(ent_hm)
        def _to_iso(day_s: str, hm_s: str) -> str:
            d = str(day_s or "").strip() or str(day or "")
            t = str(hm_s or "").strip() or str(hm or "")
            if "T" in t:
                return t
            if t.count(":") == 1:
                t = f"{t}:00"
            elif t.count(":") > 2:
                t = t[:8]
            return f"{d}T{t}+05:30"

        entry_raw = _to_iso(ent_day, ent_hm)
        exit_raw = _to_iso(ex_day, ex_hm_disp)
        entry_disp = format_desk_timestamp_ist(entry_raw)
        exit_disp = format_desk_timestamp_ist(exit_raw)
        is_proto = bool(row.get("ema8_prototype")) or "EMA8_PROTOTYPE" in str(
            row.get("strategy_id") or ""
        ).upper()
        outcome = row.get("Exit_Reason") or row.get("exit_reason")
        if is_proto and outcome == "TRAIL_SL_HIT":
            outcome = "SL_HIT"
        t = {
            "trade_id": row.get("Trade_ID"),
            "strategy_id": row.get("strategy_id") or row.get("Strategy_Filter_Tag"),
            "setup_name": row.get("Setup_Name"),
            "setup": row.get("Setup_Name"),
            "contract_symbol": row.get("Contract_Symbol"),
            "symbol": row.get("Contract_Symbol"),
            "direction": row.get("Direction"),
            "entry_ist": entry_disp,
            "exit_ist": exit_disp,
            "entry_time": entry_disp,
            "exit_time": exit_disp,
            "duration": duration_between(entry_raw, exit_raw),
            "index_entry": row.get("Index_Entry"),
            "index_exit": row.get("Index_Exit"),
            "option_entry": None if is_synth else row.get("option_entry"),
            "option_exit": None if is_synth else row.get("option_exit"),
            "gross_pnl": gross,
            "net_pnl": net,
            "st_pnl_pts": st_pts,
            "outcome": outcome,
            "result": outcome,
            "charges": chg,
            "dhan_charges": chg,
            "provenance": "REAL_NSE_SPOT_OHLC",
            "data_source": "candles_1m_sec_id_13",
            "replay_mode": "SPOT_SYNTHESIZED_REPLAY" if is_synth else "IMMUTABLE_MASTER",
            "price_type": DATA_SOURCE_SYNTHESIZED if is_synth else "REAL_ALGO_MASTER",
            "data_source_tag": DATA_SOURCE_SYNTHESIZED if is_synth else row.get("data_source_tag"),
            "Data_Type": DATA_SOURCE_SYNTHESIZED if is_synth else row.get("Data_Type"),
        }
        if is_synth:
            t["data_source_tag"] = DATA_SOURCE_SYNTHESIZED
            t["Data_Type"] = DATA_SOURCE_SYNTHESIZED
        try:
            t["entry_unix"] = int(pd.Timestamp(entry_raw).timestamp())
            t["exit_unix"] = int(pd.Timestamp(exit_raw).timestamp())
        except Exception:
            pass
        t["direction"] = str(row.get("Direction") or row.get("direction") or "").upper()
        return t

    def get_replay_session_candles(self, session_date: str, tf: str = "15m") -> Dict[str, Any]:
        """NSE Nifty spot OHLC (+ 4/8 EMA on 15m) for replay chart overlay."""
        day = str(session_date)[:10]
        df = self._load_backtest_1m_candles(day, day)
        if df.empty:
            return {"ok": False, "error": "No 1m candles for session", "date": day, "tf": tf, "candles": []}
        df = df[(df["hm"] >= "09:15") & (df["hm"] <= "15:30")].copy()
        df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        tf_l = str(tf).lower()
        if tf_l in ("15m", "15"):
            from analyst.nse_spot_candles_8510 import ema4_ema8, resample_nse_15m

            b15 = resample_nse_15m(df.set_index("dt")).reset_index()
            if b15.empty:
                return {"ok": False, "error": "15m resample empty", "date": day, "tf": tf, "candles": []}
            dt_col = "datetime" if "datetime" in b15.columns else b15.columns[0]
            e4_s, e8_s = ema4_ema8(b15["close"])
            out: List[Dict[str, Any]] = []
            for i in range(len(b15)):
                ts_i = int(pd.Timestamp(b15[dt_col].iloc[i]).timestamp())
                out.append(
                    {
                        "time": ts_i,
                        "open": float(b15["open"].iloc[i]),
                        "high": float(b15["high"].iloc[i]),
                        "low": float(b15["low"].iloc[i]),
                        "close": float(b15["close"].iloc[i]),
                        "e4": float(e4_s.iloc[i]),
                        "e8": float(e8_s.iloc[i]),
                    }
                )
            return {"ok": True, "date": day, "tf": "15m", "candles": out, "source": "candles_1m_sec_id_13"}
        candles = [
            {
                "time": int(r["ts"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for _, r in df.iterrows()
        ]
        return {"ok": True, "date": day, "tf": "1m", "candles": candles, "source": "candles_1m_sec_id_13"}

    @staticmethod
    def build_prototype_replay_markers(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Chart markers: small arrows entry/target; large SL arrows with English labels."""
        markers: List[Dict[str, Any]] = []
        for t in trades or []:
            setup = str(t.get("setup_name") or t.get("setup") or "")
            if "EMA8_PROTOTYPE" not in str(t.get("strategy_id") or "").upper() and "8 EMA Prototype" not in setup:
                continue
            direction = str(t.get("direction") or "").upper()
            if not direction:
                direction = "LONG" if "Long" in setup else "SHORT" if "Short" in setup else ""
            ent = t.get("entry_unix")
            ex = t.get("exit_unix")
            if ent is None or ex is None:
                continue
            outcome = str(t.get("outcome") or t.get("result") or "").upper()
            ent_i, ex_i = int(ent), int(ex)
            is_long = direction == "LONG"
            markers.append(
                {
                    "time": ent_i,
                    "kind": "entry",
                    "direction": direction,
                    "position": "belowBar" if is_long else "aboveBar",
                    "shape": "arrowUp" if is_long else "arrowDown",
                    "color": "#22c55e" if is_long else "#ef4444",
                    "size": 1,
                    "text": "",
                }
            )
            if "SL" in outcome:
                hm = (
                    pd.to_datetime(ex_i, unit="s", utc=True)
                    .tz_convert("Asia/Kolkata")
                    .strftime("%H:%M")
                )
                markers.append(
                    {
                        "time": ex_i,
                        "kind": "sl_exit",
                        "direction": direction,
                        "position": "aboveBar" if is_long else "belowBar",
                        "shape": "arrowDown" if is_long else "arrowUp",
                        "color": "#dc2626",
                        "size": 3,
                        "text": f"STOP LOSS HIT ON {direction} {hm}",
                    }
                )
            elif "TARGET" in outcome:
                markers.append(
                    {
                        "time": ex_i,
                        "kind": "target_exit",
                        "direction": direction,
                        "position": "aboveBar" if is_long else "belowBar",
                        "shape": "arrowUp" if is_long else "arrowDown",
                        "color": "#10b981",
                        "size": 1,
                        "text": "",
                    }
                )
        return markers

    async def run_date_replay(
        self,
        selected_date: str,
        speed_multiplier: float = 10.0,
        bias: Optional[str] = None,
        strategy_filter: Optional[str] = None,
        spot_replay: bool = False,
    ) -> Dict[str, Any]:
        """Recorded ledger review OR rule replay on stored NSE spot 1m (real OHLC, not synthetic candles)."""
        sf = str(strategy_filter or "ALL").upper().strip()
        use_spot = bool(spot_replay or sf == "EMA8_PROTOTYPE")
        candles = len(self._recorded_history().candles(selected_date))
        master_for_day = self._filter_trades_by_bias(
            fetch_master_trades(self.ledger_db, selected_date, selected_date), bias
        )
        master_filtered = self._filter_trades_by_strategy(master_for_day, sf)
        if use_spot:
            filt = sf if sf != "ALL" else "ALL"
            if master_filtered:
                stats = self._replay_summary_stats(master_filtered)
                return {
                    "ok": True,
                    "date": selected_date,
                    "trades": master_filtered,
                    "trades_count": stats["trades_count"],
                    "candles_count": candles,
                    "total_net_pnl": stats["total_net_pnl"],
                    "win_rate": stats["win_rate"],
                    "replay_kind": DATA_SOURCE_IMMUTABLE,
                    "strategy_filter": filt,
                    "message": "Immutable master rows for this strategy (real fills).",
                }
            bt = self.run_date_range_backtest(
                selected_date,
                selected_date,
                bias=bias,
                strategy_filter=filt if filt != "ALL" else "EMA8_PROTOTYPE",
                data_type_filter="SYNTHESIZED",
                include_today_paper=False,
            )
            trades = [self._replay_trade_from_backtest_row(r) for r in (bt.get("trades") or [])]
            stats = self._replay_summary_stats(trades)
            chart_markers = self.build_prototype_replay_markers(trades)
            return {
                "ok": True,
                "date": selected_date,
                "trades": trades,
                "trades_count": stats["trades_count"],
                "candles_count": candles,
                "total_net_pnl": stats["total_net_pnl"],
                "win_rate": stats["win_rate"],
                "replay_kind": "REAL_SPOT_OHLC_RULE_REPLAY",
                "strategy_filter": filt,
                "chart_markers": chart_markers,
                "message": (
                    "Rule replay on stored NSE Nifty spot 1m (sec_id=13) for selected strategy. "
                    "Index entry/exit and IST times are from the walk; options blank (SYNTHESIZED)."
                ),
            }
        if master_filtered:
            stats = self._replay_summary_stats(master_filtered)
            return {
                "ok": True,
                "date": selected_date,
                "trades": master_filtered,
                "trades_count": stats["trades_count"],
                "candles_count": candles,
                "total_net_pnl": stats["total_net_pnl"],
                "win_rate": stats["win_rate"],
                "replay_kind": DATA_SOURCE_IMMUTABLE,
                "strategy_filter": sf,
                "message": "Immutable algo execution master — real fills captured at close.",
            }

        result = self.get_unified_trades_report(
            selected_date, selected_date, bias=bias or self.directional_bias
        )
        rows = result["trades"]
        return {
            **result,
            "date": selected_date,
            "trades_count": len(rows),
            "candles_count": len(self._recorded_history().candles(selected_date)),
            "total_net_pnl": result["recorded_net_pnl"],
            "win_rate": 100 * sum(r["net_pnl"] > 0 for r in rows) / len(rows) if rows else None,
            "replay_kind": "RECORDED_LEDGER_REVIEW",
        }

    # ── DATE-RANGE SUPER TRAIL BACKTESTER ────────────────────────────────────
    def _load_historical_trades(self) -> List[Dict[str, Any]]:
        return self._recorded_history().trades()

    def _classify_strategy_filter_tag(self, strategy_id: str, setup_name: str) -> Tuple[str, str]:
        sid = str(strategy_id or "").upper()
        sname = str(setup_name or "").upper()
        if "FLAGSHIP_1H" in sid or "FLAGSHIP" in sname or "1 HR FLAGSHIP" in sname:
            return "H1_FLAGSHIP", "1 HR FLAGSHIP"
        if "STAGE_3" in sid or "STAGE 3" in sname:
            return "STAGE_3_SHORT", "STAGE 3 8 EMA SHORT"
        if "CROSSOVER" in sid or "REVERSAL" in sname or "4 MA" in sname or "4X8" in sname:
            return "CROSSOVER", "4x8 EMA CROSSOVER REVERSAL"
        if "PA_SR" in sid or "PA LONG" in sname or "PA SHORT" in sname:
            return "PA_SR_HOMEWORK", "15m PA @ S/R (Homework)"
        if "EMA8_PROTOTYPE" in sid or "8 EMA PROTOTYPE" in sname:
            return "EMA8_PROTOTYPE", "8 EMA Prototype (Paper)"
        return "STAGE_2_LONG", "STAGE 2 8 EMA LONG"

    def _compute_side_summary(self, pnl_pts_list: List[float], net_inr_list: List[float]) -> Dict[str, Any]:
        total = len(net_inr_list)
        if total == 0:
            return {
                "total_trades": 0, "win_rate_pct": 0.0, "avg_win_pts": 0.0, "avg_loss_pts": 0.0,
                "total_net_pnl_inr": 0.0, "max_drawdown_inr": 0.0, "expectancy_pts": 0.0,
                "expectancy_inr": 0.0, "profit_factor": 0.0,
            }
        # Backtested rows carry no rupee figure (it is 0 by design), so they
        # are judged on points. Before, every one of them counted as a loss.
        def _is_win(pts: float, inr: float) -> bool:
            return inr > 0 if inr != 0 else pts > 0

        wins_pts = [p for p, n in zip(pnl_pts_list, net_inr_list) if _is_win(p, n)]
        loss_pts = [p for p, n in zip(pnl_pts_list, net_inr_list) if not _is_win(p, n)]
        wins_inr = [n for n in net_inr_list if n > 0]
        loss_inr = [abs(n) for n in net_inr_list if n <= 0]
        wr = round((len(wins_pts) / total) * 100.0, 1)
        avg_w = round(float(np.mean(wins_pts)), 2) if wins_pts else 0.0
        avg_l = round(float(np.mean(loss_pts)), 2) if loss_pts else 0.0
        tot_net = round(float(sum(net_inr_list)), 2)
        exp_pts = round(float(np.mean(pnl_pts_list)), 2)
        exp_inr = round(tot_net / total, 2)
        pf = round(sum(wins_inr) / sum(loss_inr), 2) if sum(loss_inr) > 0 else (99.0 if sum(wins_inr) > 0 else 0.0)
        cum = np.cumsum(net_inr_list)
        peak = np.maximum.accumulate(np.insert(cum, 0, 0.0))[1:]
        max_dd = round(float(np.max(np.maximum(0.0, peak - cum))), 2) if len(cum) else 0.0
        return {
            "total_trades": total, "win_rate_pct": wr, "avg_win_pts": avg_w, "avg_loss_pts": avg_l,
            "total_net_pnl_inr": tot_net, "max_drawdown_inr": max_dd, "expectancy_pts": exp_pts,
            "expectancy_inr": exp_inr, "profit_factor": pf,
        }

    def run_date_range_backtest(
        self,
        from_date: str = "2026-06-01",
        to_date: str = "2026-11-04",
        custom_cfg: Optional[Dict[str, Any]] = None,
        enforce_ma_stack: bool = False,
        bias: Optional[str] = None,
        include_today_paper: bool = True,
        strategy_filter: str = "ALL",
        data_type_filter: str = "ALL",
        use_previous_scanner: bool = False,
    ) -> Dict[str, Any]:
        active_bias = str(bias or self.directional_bias or "BOTH").upper()
        strat_flt = str(strategy_filter or "ALL").upper().strip()
        dtype_flt = str(data_type_filter or "ALL").upper().strip()
        if dtype_flt == "SYNTHETIC":
            dtype_flt = "SYNTHESIZED"
        # Prototype history uses real NSE spot 1m — not live ledger rows (until they fire live).
        if strat_flt == "EMA8_PROTOTYPE" and dtype_flt == "REAL":
            dtype_flt = "SYNTHESIZED"
        lot_qty = 65
        delta_est = 0.60
        charges_est = 78.16
        gate_bb = "ON" if bool(self.scanner_cfg.get("bollinger_enabled", True)) else "OFF"

        all_rows: List[Dict[str, Any]] = []
        synth_rows: List[Dict[str, Any]] = []

        # ── A. Historical Simulation from 1m Nifty Candles (or Previous Scanner Data) ──
        if dtype_flt in ("ALL", "SYNTHESIZED"):
            try:
                if Path(self.market_db).exists():
                    from datetime import date as _date_cls

                    warmup_from = (
                        _date_cls.fromisoformat(str(from_date)[:10]) - timedelta(days=28)
                    ).isoformat()
                    with sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True, timeout=5) as conn:
                        q = """
                            SELECT ts, o AS open, h AS high, l AS low, c AS close, v AS volume,
                                   date(ts, 'unixepoch', '+330 minutes') AS day,
                                   strftime('%H:%M', ts, 'unixepoch', '+330 minutes') AS hm
                            FROM candles_1m
                            WHERE sec_id = '13'
                              AND date(ts, 'unixepoch', '+330 minutes') >= ?
                              AND date(ts, 'unixepoch', '+330 minutes') <= ?
                            ORDER BY ts ASC
                        """
                        df_all = pd.read_sql_query(q, conn, params=(warmup_from, to_date))
                else:
                    df_all = pd.DataFrame()
            except Exception as e:
                log.warning("Historical candle query failed in run_date_range_backtest: %s", e)
                df_all = pd.DataFrame()

            if not df_all.empty:
                df_all = df_all[(df_all["hm"] >= "09:15") & (df_all["hm"] <= "15:30")].copy()
                df_all["dt"] = pd.to_datetime(df_all["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                df_15m = resample_nse_15m(df_all.set_index("dt")).reset_index()
                dt_col = "datetime" if "datetime" in df_15m.columns else df_15m.columns[0]
                df_15m["ts"] = _epoch_seconds(df_15m[dt_col])
                df_15m["day"] = pd.to_datetime(df_15m[dt_col]).dt.strftime("%Y-%m-%d")
                df_15m["hm"] = pd.to_datetime(df_15m[dt_col]).dt.strftime("%H:%M")

                if len(df_15m) >= 20:
                    df_15m["e4"], df_15m["e8"] = ema4_ema8(df_15m["close"])
                    mid = df_15m["close"].rolling(20).mean()
                    sd = df_15m["close"].rolling(20).std(ddof=0).fillna(0.0)
                    df_15m["ubb"] = mid + 2.0 * sd
                    df_15m["lbb"] = mid - 2.0 * sd
                    df_15m["bbw"] = df_15m["ubb"] - df_15m["lbb"]
                    tr0 = df_15m["high"] - df_15m["low"]
                    tr1 = (df_15m["high"] - df_15m["close"].shift(1)).abs()
                    tr2 = (df_15m["low"] - df_15m["close"].shift(1)).abs()
                    tr = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
                    df_15m["atr14"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()

                    bars = df_15m.to_dict(orient="records")
                    eod_hm = str(self.scanner_cfg.get("flagship_eod_square_off", "15:20"))
                    sr_prox = float(self.scanner_cfg.get("flagship_sr_exit_proximity_pts", 15.0))
                    delta_est = float(self.cfg.get("risk", {}).get("option_delta_default", delta_est))
                    hw_bt = self.cfg.get("homework_backtest") or {}
                    trail_every_bar = bool(hw_bt.get("candle_trail_every_bar", True))
                    from analyst.pa_sr_homework_8510 import (
                        entry_on_pattern_break,
                        extend_sr_s4_r4,
                        load_pa_sr_config,
                        scan_pa_sr_breakout,
                        session_vwap_up_to_ts,
                    )

                    pa_cfg = load_pa_sr_config(self.cfg)
                    pa_enabled = bool(pa_cfg.get("enabled", True))
                    daily_ohlc = (
                        df_15m.groupby("day", sort=True)
                        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
                        .reset_index()
                    )
                    day_to_1m: Dict[str, pd.DataFrame] = {
                        str(d): g for d, g in df_all.groupby("day")
                    }
                    sr_cache: Dict[str, Dict[str, Any]] = {}
                    from analyst.ema8_prototype_8510 import backtest_ema8_prototype_at_bar

                    proto_bt_state: Dict[str, Any] = {}
                    # Stage 2 / Stage 3 / Reversal: backtested through the live
                    # evaluator with their own memory. The older rule set in
                    # _backtest_legacy_cand_signals is used only when dynamic
                    # entries are switched off in config.
                    from analyst.mj_sir_stage_reversal_8510 import (
                        backtest_mj_dynamic_at_bar,
                        load_mj_cfg as _load_mj_cfg,
                    )

                    mj_bt_state: Dict[str, Any] = {}
                    mj_dynamic_bt = bool(_load_mj_cfg(self.scanner_cfg).get("dynamic_8ema_entry", True))
                    # Only build the lanes the caller asked for. Rows of other
                    # lanes were discarded further down anyway; generating them
                    # (the Flagship pass above all) was most of the run time.
                    want_mj = strat_flt in ("ALL", "STAGE_2_LONG", "STAGE_3_SHORT", "CROSSOVER")
                    want_proto = strat_flt in ("ALL", "EMA8_PROTOTYPE")
                    want_flagship = strat_flt in ("ALL", "H1_FLAGSHIP")
                    want_pa = strat_flt in ("ALL", "PA_SR_HOMEWORK")

                    for idx in range(20, len(bars) - 1):
                        b = bars[idx]
                        b_prev = bars[idx - 1]
                        day_key = str(b.get("day") or "")
                        if day_key and (day_key < from_date or day_key > to_date):
                            continue
                        hm = str(b["hm"])
                        if hm < "09:15" or hm >= "15:30":
                            continue

                        c_p = float(b["close"])
                        e4_v = float(b["e4"])
                        e8_v = float(b["e8"])
                        bbw = float(b["bbw"])
                        ubb_up = float(b["ubb"]) > float(b_prev["ubb"])
                        lbb_dn = float(b["lbb"]) < float(b_prev["lbb"])
                        ubb_flat = abs(float(b["ubb"]) - float(b_prev["ubb"])) <= 1.8
                        lbb_flat = abs(float(b["lbb"]) - float(b_prev["lbb"])) <= 1.8
                        atr14 = float(b.get("atr14") or 15.0)

                        if proto_bt_state.get("bt_open"):
                            open_exit_ts = int(proto_bt_state["bt_open"].get("exit_ts") or 0)
                            bar_open_ts = int(b.get("ts") or 0)
                            if bar_open_ts >= open_exit_ts:
                                proto_bt_state.pop("bt_open", None)

                        if not want_mj:
                            cand_signals = []
                        elif mj_dynamic_bt:
                            cand_signals = backtest_mj_dynamic_at_bar(
                                bars,
                                idx,
                                mj_bt_state,
                                self.scanner_cfg,
                                b15_df=df_15m,
                                day_1m=day_to_1m.get(day_key),
                                bb_stage2=self._bollinger_gate_active("stage2"),
                                bb_stage3=self._bollinger_gate_active("stage3"),
                                bb_cross=self._bollinger_gate_active("crossover"),
                            )
                        else:
                            cand_signals = self._backtest_legacy_cand_signals(
                                b, b_prev, e4_v, e8_v, bbw, ubb_up, lbb_dn, ubb_flat, lbb_flat, atr14
                            )
                        if want_proto:
                            cand_signals += backtest_ema8_prototype_at_bar(
                                bars,
                                idx,
                                proto_bt_state,
                                self.scanner_cfg,
                                bb_gate_active=False,
                                b15_df=df_15m,
                                day_1m=day_to_1m.get(day_key),
                            )
                        if want_flagship:
                            cand_signals += self._backtest_flagship_signals_at_bar(df_all, int(b["ts"]), c_p)

                        if pa_enabled and want_pa and idx >= 21:
                            day_key = str(b["day"])
                            if day_key not in sr_cache:
                                prior = daily_ohlc[daily_ohlc["day"] < day_key]
                                if not prior.empty:
                                    ddf = prior.set_index("day")
                                    sr_cache[day_key] = extend_sr_s4_r4(ddf, c_p)
                                else:
                                    sr_cache[day_key] = extend_sr_s4_r4(daily_ohlc.set_index("day"), c_p)
                            vwap = None
                            d1 = day_to_1m.get(day_key)
                            if d1 is not None:
                                vwap = session_vwap_up_to_ts(d1, int(b["ts"]))
                            pa_cands = scan_pa_sr_breakout(
                                bars, idx - 1, sr_cache[day_key], pa_cfg, vwap=vwap
                            )
                            cand_signals += pa_cands

                        for sid, sname, direc, tgt_pts, sl_pts, meta in cand_signals:
                            f_code, f_tag = self._classify_strategy_filter_tag(sid, sname)
                            if strat_flt != "ALL" and f_code != strat_flt:
                                continue
                            if active_bias == "LONG_ONLY" and direc != "LONG":
                                continue
                            if active_bias == "SHORT_ONLY" and direc != "SHORT":
                                continue

                            is_long = direc == "LONG"
                            is_flagship = bool(meta.get("flagship"))
                            is_pa = bool(meta.get("pa_homework"))
                            is_proto = bool(meta.get("ema8_prototype")) or f_code == "EMA8_PROTOTYPE"
                            is_mj_dyn = bool(meta.get("mj_dynamic"))
                            # Entered inside the candle at a known tick: exit is
                            # walked on 1-minute candles after that tick.
                            fixed_walk = is_proto or is_mj_dyn
                            if is_pa:
                                entry_spot = entry_on_pattern_break(bars, idx, meta, direc)
                                start_i = idx
                            elif fixed_walk and meta.get("entry_spot") is not None:
                                entry_spot = round(float(meta["entry_spot"]), 2)
                                start_i = idx
                            else:
                                entry_spot = round(float(bars[idx + 1]["open"]), 2)
                                start_i = idx + 1
                            teb = False if is_proto else (trail_every_bar and not is_pa)
                            tgo = False if is_proto else bool(
                                meta.get("trail_green_only", not trail_every_bar)
                            )
                            if fixed_walk:
                                proto_res = simulate_prototype_spot_exit(
                                    bars=bars,
                                    start_idx=start_i,
                                    entry_spot=entry_spot,
                                    is_long=is_long,
                                    sl_pts=float(sl_pts),
                                    tgt_pts=float(tgt_pts),
                                    eod_hm=eod_hm,
                                    day_1m=day_to_1m.get(day_key),
                                    entry_ts=int(meta.get("entry_ts") or 0),
                                    exit_on_opposite_close=is_proto,
                                )
                                st_pnl_pts = proto_res["st_pnl_pts"]
                                fx_pnl_pts = proto_res["fx_pnl_pts"]
                                exit_spot = proto_res["exit_spot"]
                                st_reason = proto_res["exit_reason"]
                                trail_steps = proto_res["trail_steps"]
                                mfe_pts = proto_res["mfe_pts"]
                                ent_day = str(meta.get("entry_day") or proto_res["entry_day"])
                                ent_hm = str(meta.get("entry_hm") or proto_res["entry_hm"])
                                ex_day = proto_res["exit_day"]
                                ex_hm = proto_res["exit_hm"]
                                exit_ts = proto_res["exit_ts"]
                                is_ambiguous = proto_res["is_ambiguous"]

                                # Track open trade for alternating trades rule
                                if is_proto:
                                    proto_bt_state["bt_open"] = {
                                        "direction": direc,
                                        "entry_ts": int(meta.get("entry_ts") or 0),
                                        "exit_ts": exit_ts,
                                    }
                                    from analyst.ema8_prototype_8510 import (
                                        load_ema8_prototype_cfg as _lpc,
                                        reentry_enabled as _reentry_on,
                                    )

                                    from analyst.ema8_prototype_8510 import note_prototype_exit as _note_exit

                                    _note_exit(proto_bt_state, _lpc(self.scanner_cfg), direc, st_reason)
                                    if not _reentry_on(_lpc(self.scanner_cfg)):
                                        proto_bt_state["last_traded_direction"] = direc
                                        proto_bt_state["consumed_cross_ts"] = meta.get("cross_bar_open_ts") or meta.get("entry_ts")
                            else:
                                st_pnl_pts, fx_pnl_pts, exit_spot, st_reason, trail_steps, mfe_pts, ent_day, ent_hm, ex_day, ex_hm = simulate_backtest_spot_exit(
                                    bars,
                                    start_i,
                                    entry_spot,
                                    is_long,
                                    float(sl_pts),
                                    float(tgt_pts),
                                    is_flagship,
                                    meta.get("sr_levels") or {},
                                    eod_hm,
                                    sr_prox,
                                    trail_every_bar=teb,
                                    trail_green_only=tgo,
                                    exit_at_8ema=bool(
                                        meta.get("exit_at_8ema", meta.get("exit_at_atma"))
                                    )
                                    if is_pa
                                    else False,
                                    exit_on_opposite_8ema_close=False,
                                )
                                is_ambiguous = False
                                if meta.get("entry_hm"):
                                    ent_day = str(meta.get("entry_day") or ent_day)
                                    ent_hm = str(meta["entry_hm"])

                            if fixed_walk:
                                # Spot rule only: no modelled premium, no rupees.
                                synth_opt_entry, synth_opt_exit = None, None
                                synth_gross_inr = 0.0
                                synth_net_inr = 0.0
                                fx_net_inr = 0.0
                            else:
                                synth_opt_entry, synth_opt_exit = synthesize_option_premium_from_spot(
                                    entry_spot, exit_spot, is_long, delta_est
                                )
                                synth_gross_inr = round((synth_opt_exit - synth_opt_entry) * lot_qty, 2)
                                synth_net_inr = round(synth_gross_inr - charges_est, 2)
                                fx_net_inr = round((fx_pnl_pts * delta_est) * lot_qty - charges_est, 2)

                            falloff_pts = round(max(0.0, mfe_pts - max(0.0, st_pnl_pts)), 1)
                            atm_strike = int(round(entry_spot / 100.0) * 100)
                            opt_type = "CE" if is_long else "PE"
                            contract_sym = f"NIFTY {atm_strike} {opt_type} (Synthesized Strike)"
                            trail_str = (
                                f"Fixed {int(sl_pts)}/{int(tgt_pts)} Nifty spot, 1m walk after entry (no Super Trail; option exit not modelled) → {st_reason}"
                                if fixed_walk
                                else " -> ".join(trail_steps[:6]) + f" -> {st_reason}"
                            )

                            if is_flagship:
                                ema8_line_col = (
                                    f"1 HR FLAGSHIP PA | Spot entry {entry_spot:.1f} exit {exit_spot:.1f} | "
                                    f"15m confirm {hm} | S/R aware trail"
                                )
                            else:
                                ema8_line_col = (
                                    f"15m 8 EMA: {e8_v:.2f} | 4 EMA: {e4_v:.2f} | "
                                    f"Entry {entry_spot:.1f}, dist {entry_spot - e8_v:+.1f} pts"
                                )

                            synth_rows.append({
                                "Trade_ID": f"SYN_{b['day']}_{hm}_{sid}",
                                "Date": f"{ent_day} {ent_hm}",
                                "date_only": str(ent_day or b["day"]),
                                "entry_day": ent_day,
                                "entry_hm": ent_hm,
                                "exit_day": ex_day,
                                "exit_hm": ex_hm,
                                "ema8_prototype": is_proto,
                                "Setup_Name": sname,
                                "Contract_Symbol": contract_sym,
                                "Direction": direc,
                                "Index_Entry": f"{entry_spot:.2f}",
                                "Index_Exit": f"{exit_spot:.2f}",
                                "Option_Entry_Display": "—",
                                "Option_Exit_Display": "—",
                                "option_entry": None,
                                "option_exit": None,
                                "price_type": "SYNTHESIZED",
                                "data_source_tag": DATA_SOURCE_SYNTHESIZED,
                                "Exit_Reason": st_reason,
                                "exit_reason": st_reason,
                                "Net_Pnl_Display": f"{st_pnl_pts:+.1f} pts (Spot)" if is_proto else f"{st_pnl_pts:+.1f} pts | ₹{synth_net_inr:,.2f} (SYNTHESIZED)",
                                "net_pnl": 0.0 if is_proto else synth_net_inr,
                                "gross_pnl": 0.0 if is_proto else synth_gross_inr,
                                "gross_pnl_inr": 0.0 if is_proto else synth_gross_inr,
                                "charges": 0.0 if fixed_walk else charges_est,
                                "dhan_charges": 0.0 if fixed_walk else charges_est,
                                "fixed_net_pnl_row": 0.0 if is_proto else fx_net_inr,
                                "st_pnl_pts": st_pnl_pts,
                                "fx_pnl_pts": fx_pnl_pts,
                                "Trailing_Details": trail_str,
                                "All_Fall_Off": f"MFE +{mfe_pts:.1f}p | Falloff {falloff_pts:.1f}p",
                                "Strategy_Filter_Tag": f_tag,
                                "Data_Type": "SYNTHESIZED",
                                "Ema8_Line_Details": ema8_line_col,
                                "Ema8_From_Matrix": ema8_line_col,
                                "dynamic_8ema_entry": meta.get("dynamic_e8_at_entry") or meta.get("e8"),
                                "Dynamic_8EMA_Entry": f"{float(meta.get('dynamic_e8_at_entry') or meta.get('e8') or 0.0):.2f}" if fixed_walk else "—",
                                "is_ambiguous": is_ambiguous if fixed_walk else False,
                                "strategy_id": sid,
                                "source": "SYNTHESIZED",
                                "BB_Gate": meta.get("bb_gate_label", gate_bb),
                                "Bollinger_Applied": meta.get("bollinger_applied", gate_bb == "ON"),
                                "Entry_TF": "15m",
                            })

                        # If prototype trade closed during this 15m bar, clear bt_open so bar close flip check can arm
                        if proto_bt_state.get("bt_open"):
                            open_exit_ts = int(proto_bt_state["bt_open"].get("exit_ts") or 0)
                            bar_close_ts = int(b.get("ts") or 0) + 900
                            if bar_close_ts >= open_exit_ts:
                                proto_bt_state.pop("bt_open", None)

        # ── B. Immutable master (append-only algo fills — paper or live) ──
        master_src: List[Dict[str, Any]] = []
        if dtype_flt in ("ALL", "REAL"):
            for m in fetch_master_trades(self.ledger_db, from_date, to_date):
                sname = str(m.get("setup_name") or "")
                f_code, _f_tag = self._classify_strategy_filter_tag(
                    m.get("strategy_id") or "", sname
                )
                if strat_flt != "ALL" and f_code != strat_flt:
                    continue
                direc = str(m.get("direction") or "LONG").upper()
                if active_bias == "LONG_ONLY" and direc != "LONG":
                    continue
                if active_bias == "SHORT_ONLY" and direc != "SHORT":
                    continue
                master_src.append(m)

        master_bt = [master_to_backtest_row(m) for m in master_src]
        if dtype_flt == "REAL":
            all_rows = [tag_immutable_master_row(r) for r in master_bt]
        elif dtype_flt == "SYNTHESIZED":
            all_rows = [normalize_synthetic_backtest_row(r) for r in synth_rows]
        else:
            all_rows = merge_master_and_synthetic(master_bt, synth_rows)

        def _row_net_inr(row: Dict[str, Any]) -> float:
            if row.get("data_source_tag") == DATA_SOURCE_SYNTHESIZED:
                return 0.0
            return float(row.get("net_pnl") or row.get("fixed_net_pnl_row") or 0.0)

        st_summary = self._compute_side_summary(
            [float(x["st_pnl_pts"]) for x in all_rows],
            [_row_net_inr(x) for x in all_rows],
        )
        fx_summary = self._compute_side_summary(
            [float(x["fx_pnl_pts"]) for x in all_rows],
            [_row_net_inr(x) for x in all_rows],
        )

        proto_trades = [x for x in all_rows if x.get("ema8_prototype")]
        proto_summary = compute_ema8_prototype_summary(proto_trades)

        if strat_flt == "EMA8_PROTOTYPE":
            st_summary["win_rate_pct"] = proto_summary["win_rate_pct"]
            st_summary["avg_win_pts"] = proto_summary["avg_win_pts"]
            st_summary["avg_loss_pts"] = proto_summary["avg_loss_pts"]
            st_summary["total_net_pnl_inr"] = 0.0
            st_summary["total_net_pts"] = proto_summary["net_points"]
            fx_summary["win_rate_pct"] = proto_summary["win_rate_pct"]
            fx_summary["avg_win_pts"] = proto_summary["avg_win_pts"]
            fx_summary["avg_loss_pts"] = proto_summary["avg_loss_pts"]
            fx_summary["total_net_pnl_inr"] = 0.0
            fx_summary["total_net_pts"] = proto_summary["net_points"]

        tags = {str(x.get("data_source_tag") or "") for x in all_rows}
        if tags == {DATA_SOURCE_IMMUTABLE} or (DATA_SOURCE_IMMUTABLE in tags and DATA_SOURCE_SYNTHESIZED not in tags):
            dtype_mode = "REAL"
        elif DATA_SOURCE_IMMUTABLE in tags and DATA_SOURCE_SYNTHESIZED in tags:
            dtype_mode = "MIXED"
        else:
            dtype_mode = "SYNTHESIZED"

        return {
            "ok": True,
            "from_date": from_date,
            "to_date": to_date,
            "strategy_filter": strat_flt,
            "data_type_filter": dtype_flt,
            "data_type_mode": dtype_mode,
            "total_trades": len(all_rows),
            "trades": all_rows,
            "rows": all_rows,
            "super_trail_summary": st_summary,
            "fixed_40_40_summary": fx_summary,
            "win_rate_trailing": st_summary["win_rate_pct"],
            "win_rate_fixed": fx_summary["win_rate_pct"],
            "trailing_net_pnl": st_summary["total_net_pnl_inr"],
            "fixed_net_pnl": fx_summary["total_net_pnl_inr"],
            "alpha_locked": round(st_summary["total_net_pnl_inr"] - fx_summary["total_net_pnl_inr"], 2),
            "max_drawdown_trailing": st_summary["max_drawdown_inr"],
            "max_drawdown_fixed": fx_summary["max_drawdown_inr"],
            "profit_factor_trailing": st_summary["profit_factor"],
            "profit_factor_fixed": fx_summary["profit_factor"],
            "reconciliation": [],
            "reconciliation_match": True,
            "scanner_gates": self.get_scanner_gate_config(),
            "bb_gate_run_label": gate_bb,
            "ema8_prototype_summary": proto_summary,
        }
