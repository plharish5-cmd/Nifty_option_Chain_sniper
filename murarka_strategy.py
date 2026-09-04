"""
murarka_strategy.py — Nitin Murarka's "Lighter Side" Dynamic-DTE Option Chain Strategy.

CORE METHODOLOGY:
─────────────────
1. Strike Selection & Dynamic Window (Trick 1 & 2):
   - Center Strike is ANCHORED to the 9:08 AM Pre-Opening Spot price (static all day).
   - Window size (N) scales dynamically with Days to Expiry (DTE):
       * 5 DTE (Wednesday): +/- 5 strikes (11 strikes total)
       * 4 DTE (Thursday):  +/- 4 strikes (9 strikes total)
       * 3 DTE (Friday):    +/- 3 strikes (7 strikes total)
       * 2 DTE (Monday):    +/- 2 strikes (5 strikes total)
       * 1 DTE (Expiry):    +/- 1 strike  (3 strikes total: ATM, 1 ITM, 1 OTM)
   - Excludes far OTM noise strikes where premium has decayed to insignificant levels.

2. Cumulative Change in Open Interest (Trick 3 — "The Lighter Side"):
   - Sum Call Chg OI and Put Chg OI across the active +/- N strike window.
   - If Call Chg OI > 1.30x Put Chg OI -> Heavy resistance above -> Trade the LIGHTER PUT side (BEARISH).
   - If Put Chg OI > 1.30x Call Chg OI -> Heavy support below -> Trade the LIGHTER CALL side (BULLISH).
   - Put unwinding (negative Put Chg OI) while Call OI builds -> High Conviction Bearish.

3. Price & OI Correlation (Trick 4):
   - Cross-reference Price Change (Chg) with Chg in OI.
   - Bearish: Call Price Chg < 0 (decay/writing) & Put Price Chg > 0.
   - Bullish: Put Price Chg < 0 & Call Price Chg > 0.
   - EXPIRY DAY TRAP: If BOTH Call Price Chg < 0 AND Put Price Chg < 0 -> AVOID ALL TRADES (Straddle writing chop).

4. Three-Tier Execution Hierarchy:
   - Primary Direction: Window PCR & Cumulative Chg in OI.
   - Entry Timing: Contract LTP must cross and hold ABOVE its Strike VWAP.
   - Secondary Demand: Multi-Expiry IV comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

import config

STRIKE_STEP = getattr(config, "STRIKE_STEP", 50)


@dataclass
class MurarkaBias:
    timestamp: datetime
    center_strike: float
    dte: float
    window_n: int
    strike_min: float
    strike_max: float
    tot_call_chg_oi: float
    tot_put_chg_oi: float
    oi_ratio: float               # Put Chg OI / Call Chg OI
    bias: str                     # BULLISH (Trade Call) | BEARISH (Trade Put) | NEUTRAL
    action: str                   # BUY_CE | BUY_PE | NO_TRADE
    price_confirmed: bool
    is_expiry_trap: bool
    summary: str


def get_dynamic_strike_window(dte: float) -> int:
    """
    Returns the number of strikes (+/- N) around ATM based on Days to Expiry (DTE).
    """
    if dte >= 4.0:
        return 5
    elif dte >= 3.0:
        return 4
    elif dte >= 2.0:
        return 3
    elif dte >= 1.2:
        return 2
    else:
        return 1


def calculate_center_strike(spot_or_preopen: float) -> float:
    """
    Anchors the center strike to the nearest 50 strike from the 9:08 pre-open price.
    """
    return round(spot_or_preopen / STRIKE_STEP) * STRIKE_STEP


def evaluate_murarka_bias(
    chain_df: pd.DataFrame,
    center_strike: float,
    dte: float,
    timestamp: Optional[datetime] = None,
) -> Optional[MurarkaBias]:
    """
    Evaluates Nitin Murarka's "Lighter Side" OI strategy across a snapshot option chain DataFrame.
    """
    if chain_df is None or chain_df.empty:
        return None

    now = timestamp or datetime.now()
    window_n = get_dynamic_strike_window(dte)
    min_strike = center_strike - (window_n * STRIKE_STEP)
    max_strike = center_strike + (window_n * STRIKE_STEP)

    sub = chain_df[(chain_df["strike"] >= min_strike) & (chain_df["strike"] <= max_strike)].copy()
    if sub.empty:
        return None

    tot_call_chg_oi = float(sub["ce_chg_oi"].sum()) if "ce_chg_oi" in sub.columns else 0.0
    tot_put_chg_oi = float(sub["pe_chg_oi"].sum()) if "pe_chg_oi" in sub.columns else 0.0

    ce_chg_sum = float(sub["ce_chg"].sum()) if "ce_chg" in sub.columns else 0.0
    pe_chg_sum = float(sub["pe_chg"].sum()) if "pe_chg" in sub.columns else 0.0

    # Rule 3: The Lighter Side Principle
    bias = "NEUTRAL"
    action = "NO_TRADE"

    if tot_call_chg_oi > 0 and tot_put_chg_oi > 0:
        ratio = tot_put_chg_oi / tot_call_chg_oi
        if ratio >= 1.30:
            bias = "BULLISH"
            action = "BUY_CE"
        elif ratio <= 0.77:  # 1 / 1.30 = 0.769
            bias = "BEARISH"
            action = "BUY_PE"
    elif tot_call_chg_oi > 0 and tot_put_chg_oi <= 0:
        # Put unwinding while Call writing builds
        ratio = 0.0
        bias = "BEARISH"
        action = "BUY_PE"
    elif tot_put_chg_oi > 0 and tot_call_chg_oi <= 0:
        # Call unwinding while Put writing builds
        ratio = 99.0
        bias = "BULLISH"
        action = "BUY_CE"
    else:
        ratio = 1.0

    # Rule 4: Price & OI Correlation
    price_confirmed = True
    is_expiry_trap = False

    if ce_chg_sum < 0 and pe_chg_sum < 0:
        # Both Call and Put prices are decaying -> Expiry Day trap
        is_expiry_trap = True
        price_confirmed = False
        bias = "NEUTRAL (EXPIRY_TRAP)"
        action = "NO_TRADE"
    elif bias == "BEARISH" and pe_chg_sum < 0:
        price_confirmed = False
    elif bias == "BULLISH" and ce_chg_sum < 0:
        price_confirmed = False

    summary_msg = (
        f"[{now:%H:%M}] {bias} | Center: {center_strike:,.0f} (+/-{window_n} strikes: {min_strike:,.0f}-{max_strike:,.0f})\n"
        f"  Call Chg OI: {tot_call_chg_oi:,.0f} | Put Chg OI: {tot_put_chg_oi:,.0f} (Ratio: {ratio:.2f})\n"
        f"  Price Confirmed: {price_confirmed} | Expiry Trap: {is_expiry_trap}"
    )

    return MurarkaBias(
        timestamp=now,
        center_strike=center_strike,
        dte=dte,
        window_n=window_n,
        strike_min=min_strike,
        strike_max=max_strike,
        tot_call_chg_oi=tot_call_chg_oi,
        tot_put_chg_oi=tot_put_chg_oi,
        oi_ratio=round(ratio, 2),
        bias=bias,
        action=action,
        price_confirmed=price_confirmed,
        is_expiry_trap=is_expiry_trap,
        summary=summary_msg,
    )


def check_vwap_entry_confirmation(contract_ltp: float, contract_vwap: float) -> bool:
    """
    Nitin Murarka Entry Timing Rule:
    Only enter when the option contract LTP is holding cleanly ABOVE its Strike VWAP.
    """
    return contract_ltp > contract_vwap if contract_vwap > 0 else True
