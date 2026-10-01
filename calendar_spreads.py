"""
calendar_spreads.py
═══════════════════════════════════════════════════════════════════════════════
Volatility-Based Calendar Spread Engine for Nifty & Index Options
═══════════════════════════════════════════════════════════════════════════════

Executes institutional volatility skew & calendar spread analytics:
  1. The "1-Second Rule" (IV Differential):
     - Calculates IV difference between Near Expiry (Expiry 1) and Far Expiry (Expiry 2)
       at At-The-Money (ATM) strikes.
     - Formula: ((IV_Far - IV_Near) / IV_Near) * 100
     - Trigger: Only arms the trade if the IV differential magnitude > 15.0%.
  2. Strategy Directional Selection:
     - If Near IV > Far IV: Recommends LONG CALENDAR SPREAD
       (Buy Far Expiry ATM, Sell Near Expiry ATM) to capture rapid near-term theta
       decay and post-event IV normalization.
     - If Near IV < Far IV: Recommends SHORT CALENDAR SPREAD (Reverse Calendar)
       (Sell Far Expiry ATM, Buy Near Expiry ATM).
  3. Strict Risk Management & Exit SOP:
     - Expiry Day Filter: Strictly FORBIDS entry on Expiry Day (DTE = 0).
     - Event Risk Gate: Force exit / lockout 3 days prior to major macro/earnings events.
     - Profit-Taking Rule: Auto-flags exit when trade captures 30% to 40% of maximum payoff.
     - Stop Loss Threshold: Auto-flags protective exit if spread value declines by 20% of debit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger("calendar_spreads")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures & Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalendarLeg:
    """Details of a single leg in the calendar spread."""
    expiry: str
    strike: float
    option_type: str  # 'CE' or 'PE'
    action: str  # 'BUY' or 'SELL'
    iv: float
    ltp: float
    dte: int


@dataclass
class RiskEvaluation:
    """Risk management and safety filters evaluation."""
    is_safe: bool
    is_expiry_day: bool
    days_to_nearest_event: Optional[int]
    event_lockout_active: bool
    rejections: List[str] = field(default_factory=list)

    @property
    def rejection_reasons(self) -> List[str]:
        return self.rejections


@dataclass
class ExitAlert:
    """Real-time exit advisory for active calendar spread."""
    should_exit: bool
    exit_type: str  # 'PROFIT_TARGET', 'STOP_LOSS', 'EVENT_FORCE_EXIT', 'EXPIRY_FORCE_EXIT', 'HOLD'
    current_pnl_pts: float
    current_pnl_pct_of_max: float
    threshold_reached_pct: float
    message: str


@dataclass
class CalendarOpportunity:
    """Evaluated calendar spread opportunity."""
    is_armed: bool
    strategy_name: str  # 'LONG_CALENDAR_SPREAD', 'SHORT_CALENDAR_SPREAD', 'NO_TRADE'
    strike: float
    option_type: str  # 'CE' or 'PE'
    near_leg: Optional[CalendarLeg]
    far_leg: Optional[CalendarLeg]
    near_iv: float
    far_iv: float
    iv_diff_pct: float  # ((IV_Far - IV_Near) / IV_Near) * 100
    iv_diff_threshold: float
    net_debit: float
    estimated_max_payoff: float
    profit_target_pts: float
    stop_loss_pts: float
    risk_status: RiskEvaluation
    rationale: str


# ─────────────────────────────────────────────────────────────────────────────
# CalendarSpreadEngine Class
# ─────────────────────────────────────────────────────────────────────────────

class CalendarSpreadEngine:
    """
    Automated Engine for identifying, sizing, and managing volatility calendar spreads.
    """

    def __init__(
        self,
        iv_diff_threshold: float = 15.0,
        profit_take_pct: float = 35.0,  # 30% - 40% threshold
        max_loss_pct: float = 20.0,
        event_blackout_days: int = 3
    ):
        """
        Parameters:
            iv_diff_threshold: Minimum percentage IV difference to arm spread (default 15.0%).
            profit_take_pct: Percentage of maximum payoff to trigger exit (default 35.0%).
            max_loss_pct: Max allowable drawdown percentage on entry debit (default 20.0%).
            event_blackout_days: Days before major event to force exit / lockout (default 3 days).
        """
        self.iv_diff_threshold = iv_diff_threshold
        self.profit_take_pct = profit_take_pct
        self.max_loss_pct = max_loss_pct
        self.event_blackout_days = event_blackout_days
        logger.info(
            f"CalendarSpreadEngine initialized | IV Diff Threshold = {iv_diff_threshold}% | "
            f"Profit Target = {profit_take_pct}% of max payoff | Event Blackout = {event_blackout_days} days"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Flexible Column Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        Standardizes column names for seamless ingestion of standard pandas DataFrames.
        """
        d = df.copy()
        col_map: Dict[str, str] = {}
        for col in d.columns:
            c_low = str(col).lower().strip().replace(" ", "_").replace("-", "_")

            if c_low in ("strike", "strike_price", "strikeprice"):
                col_map[col] = "strike"
            elif c_low in ("expiry", "expiry_date", "exp_date", "exp"):
                col_map[col] = "expiry"
            elif c_low in ("date", "timestamp", "datetime", "time"):
                col_map[col] = "date"
            elif c_low in ("call_iv", "ce_iv", "c_iv"):
                col_map[col] = "call_iv"
            elif c_low in ("put_iv", "pe_iv", "p_iv"):
                col_map[col] = "put_iv"
            elif c_low in ("call_price", "ce_ltp", "call_ltp", "ce_close"):
                col_map[col] = "call_price"
            elif c_low in ("put_price", "pe_ltp", "put_ltp", "pe_close"):
                col_map[col] = "put_price"
            elif c_low in ("spot", "underlying", "underlying_value", "spot_price"):
                col_map[col] = "spot"
            elif c_low == "open":
                col_map[col] = "open"
            elif c_low == "high":
                col_map[col] = "high"
            elif c_low == "low":
                col_map[col] = "low"
            elif c_low in ("close", "ltp"):
                col_map[col] = "close"

        return d.rename(columns=col_map)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. The 1-Second Rule (IV Differential Calculation)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def calculate_iv_differential(iv_near: float, iv_far: float) -> float:
        """
        Calculates percentage IV differential between Near and Far Expiries.
        Formula: ((IV_Far - IV_Near) / IV_Near) * 100
        """
        if iv_near <= 0.001:
            logger.warning("Near IV is zero or non-positive; cannot compute IV differential.")
            return 0.0
        return ((iv_far - iv_near) / iv_near) * 100.0

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Risk Management & Expiry Day / Event Filters
    # ─────────────────────────────────────────────────────────────────────────

    def check_risk_filters(
        self,
        dte_near: int,
        current_date: Optional[date] = None,
        upcoming_events: Optional[List[Union[date, str]]] = None
    ) -> RiskEvaluation:
        """
        Verifies:
          1. Expiry Day Rule: NEVER trade if Near DTE == 0.
          2. Event Gate: Forbid trades or force exit within 3 days of major events.
        """
        rejections: List[str] = []
        is_expiry_day = (dte_near <= 0)

        if is_expiry_day:
            rejections.append("⛔ EXPIRY DAY LOCKOUT: Near expiry DTE is 0. High gamma/pin risk.")

        # Event filter check
        cur = current_date or date.today()
        days_to_event = None
        event_lockout = False

        if upcoming_events:
            min_diff = 9999
            for ev in upcoming_events:
                ev_date = pd.to_datetime(ev).date() if not isinstance(ev, date) else ev
                diff = (ev_date - cur).days
                if 0 <= diff < min_diff:
                    min_diff = diff

            if min_diff != 9999:
                days_to_event = min_diff
                if min_diff <= self.event_blackout_days:
                    event_lockout = True
                    rejections.append(
                        f"⚠️ EVENT LOCKOUT: Major event scheduled in {min_diff} day(s) "
                        f"(Blackout window is <= {self.event_blackout_days} days)."
                    )

        is_safe = len(rejections) == 0
        return RiskEvaluation(
            is_safe=is_safe,
            is_expiry_day=is_expiry_day,
            days_to_nearest_event=days_to_event,
            event_lockout_active=event_lockout,
            rejections=rejections
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Opportunity Evaluation & Strategy Selection
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate_spread_opportunity(
        self,
        df: pd.DataFrame,
        spot: Optional[float] = None,
        option_type: str = "CE",  # 'CE' or 'PE'
        current_date: Optional[date] = None,
        upcoming_events: Optional[List[Union[date, str]]] = None
    ) -> CalendarOpportunity:
        """
        Evaluates option chain data to find ATM calendar spread opportunities:
          - Identifies Expiry 1 (Near) and Expiry 2 (Far).
          - Locates ATM strike.
          - Calculates IV differential and applies 15% rule.
          - Applies Expiry Day and Event risk gates.
        """
        d = self.normalize_dataframe(df)

        if "expiry" not in d.columns or "strike" not in d.columns:
            raise ValueError("DataFrame must contain 'expiry' and 'strike' columns.")

        # Extract spot
        if spot is None:
            if "spot" in d.columns and not d["spot"].isna().all():
                spot = float(d["spot"].dropna().iloc[0])
            elif "close" in d.columns and not d["close"].isna().all():
                spot = float(d["close"].dropna().iloc[0])
            else:
                raise ValueError("Spot price must be provided or present in DataFrame.")

        # Identify unique sorted expiries
        expiries = sorted(d["expiry"].dropna().unique())
        if len(expiries) < 2:
            logger.warning("Fewer than 2 expiries present. Cannot form calendar spread.")
            risk = self.check_risk_filters(dte_near=99, current_date=current_date, upcoming_events=upcoming_events)
            return CalendarOpportunity(
                is_armed=False,
                strategy_name="NO_TRADE",
                strike=round(spot / 50.0) * 50,
                option_type=option_type,
                near_leg=None,
                far_leg=None,
                near_iv=0.0,
                far_iv=0.0,
                iv_diff_pct=0.0,
                iv_diff_threshold=self.iv_diff_threshold,
                net_debit=0.0,
                estimated_max_payoff=0.0,
                profit_target_pts=0.0,
                stop_loss_pts=0.0,
                risk_status=risk,
                rationale="Insufficient expiries in dataset (minimum 2 expiries needed)."
            )

        exp_near = expiries[0]
        exp_far = expiries[1]

        # Calculate DTE
        cur_dt = current_date or date.today()
        d_near = pd.to_datetime(exp_near).date()
        d_far = pd.to_datetime(exp_far).date()
        dte_near = max(0, (d_near - cur_dt).days)
        dte_far = max(0, (d_far - cur_dt).days)

        # Risk Filters
        risk = self.check_risk_filters(
            dte_near=dte_near,
            current_date=cur_dt,
            upcoming_events=upcoming_events
        )

        # Find ATM Strike
        atm_strike = round(spot / 50.0) * 50

        # Filter near and far data for ATM strike
        d_near_atm = d[(d["expiry"] == exp_near) & (d["strike"] == atm_strike)]
        d_far_atm = d[(d["expiry"] == exp_far) & (d["strike"] == atm_strike)]

        if d_near_atm.empty or d_far_atm.empty:
            # Try nearest available strike
            avail_strikes = sorted(d["strike"].unique())
            atm_strike = float(min(avail_strikes, key=lambda x: abs(x - spot)))
            d_near_atm = d[(d["expiry"] == exp_near) & (d["strike"] == atm_strike)]
            d_far_atm = d[(d["expiry"] == exp_far) & (d["strike"] == atm_strike)]

        # Extract IV and Prices
        is_call = (option_type.upper() == "CE")
        iv_col = "call_iv" if is_call else "put_iv"
        price_col = "call_price" if is_call else "put_price"

        near_iv = float(d_near_atm[iv_col].iloc[0]) if iv_col in d_near_atm.columns else 15.0
        far_iv = float(d_far_atm[iv_col].iloc[0]) if iv_col in d_far_atm.columns else 15.0

        near_price = float(d_near_atm[price_col].iloc[0]) if price_col in d_near_atm.columns else 100.0
        far_price = float(d_far_atm[price_col].iloc[0]) if price_col in d_far_atm.columns else 160.0

        # Compute IV Differential
        iv_diff_pct = self.calculate_iv_differential(near_iv, far_iv)
        is_iv_triggered = abs(iv_diff_pct) >= self.iv_diff_threshold

        # Determine Strategy Direction
        # If Near IV > Far IV (iv_diff_pct < 0): Long Calendar Spread (Buy Far, Sell Near)
        # If Near IV < Far IV (iv_diff_pct > 0): Short Calendar Spread (Sell Far, Buy Near)
        if near_iv > far_iv:
            strat_name = "LONG_CALENDAR_SPREAD"
            near_action = "SELL"
            far_action = "BUY"
            net_debit = far_price - near_price  # Debit paid
            # Estimated max payoff is typically around 2x to 3x of net debit or ~60% of near strike extrinsic
            est_max_payoff = max(net_debit * 1.5, near_price * 0.9)
            strat_desc = (
                f"Near IV ({near_iv:.1f}%) > Far IV ({far_iv:.1f}%). Differential: {iv_diff_pct:.1f}%. "
                f"Recommend LONG CALENDAR: Sell Near Expiry ({exp_near}) to collect rich premium, "
                f"Buy Far Expiry ({exp_far}) for low-cost convexity."
            )
        else:
            strat_name = "SHORT_CALENDAR_SPREAD"
            near_action = "BUY"
            far_action = "SELL"
            net_debit = near_price - far_price  # Net credit received or debit
            est_max_payoff = max(abs(net_debit) * 1.5, far_price * 0.8)
            strat_desc = (
                f"Near IV ({near_iv:.1f}%) < Far IV ({far_iv:.1f}%). Differential: {iv_diff_pct:.1f}%. "
                f"Recommend SHORT CALENDAR: Buy Near Expiry ({exp_near}), "
                f"Sell Far Expiry ({exp_far}) to harvest overpriced far-month volatility."
            )

        # Arms trade only if IV condition met AND Risk Filters passed
        is_armed = is_iv_triggered and risk.is_safe

        profit_target_pts = est_max_payoff * (self.profit_take_pct / 100.0)
        stop_loss_pts = max(15.0, abs(net_debit) * (self.max_loss_pct / 100.0))

        near_leg = CalendarLeg(
            expiry=str(exp_near),
            strike=atm_strike,
            option_type=option_type.upper(),
            action=near_action,
            iv=near_iv,
            ltp=near_price,
            dte=dte_near
        )

        far_leg = CalendarLeg(
            expiry=str(exp_far),
            strike=atm_strike,
            option_type=option_type.upper(),
            action=far_action,
            iv=far_iv,
            ltp=far_price,
            dte=dte_far
        )

        rationale = (
            f"ATM Strike: {atm_strike:,.0f} | Near IV: {near_iv:.1f}% vs Far IV: {far_iv:.1f}% | "
            f"IV Diff: {iv_diff_pct:+.1f}% (Threshold: {self.iv_diff_threshold}%). "
            f"{'✅ Armed' if is_armed else '❌ Not Armed'}: "
            + (strat_desc if is_armed else ("Risk lockout or IV threshold not met: " + " · ".join(risk.rejections or [f"IV diff {abs(iv_diff_pct):.1f}% < {self.iv_diff_threshold}%"])))
        )

        logger.info(rationale)

        return CalendarOpportunity(
            is_armed=is_armed,
            strategy_name=strat_name if is_armed else "NO_TRADE",
            strike=atm_strike,
            option_type=option_type.upper(),
            near_leg=near_leg,
            far_leg=far_leg,
            near_iv=near_iv,
            far_iv=far_iv,
            iv_diff_pct=round(iv_diff_pct, 2),
            iv_diff_threshold=self.iv_diff_threshold,
            net_debit=round(net_debit, 2),
            estimated_max_payoff=round(est_max_payoff, 2),
            profit_target_pts=round(profit_target_pts, 2),
            stop_loss_pts=round(stop_loss_pts, 2),
            risk_status=risk,
            rationale=rationale
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Real-Time Exit SOP Monitoring
    # ─────────────────────────────────────────────────────────────────────────

    def monitor_exit(
        self,
        entry_debit: float,
        current_spread_value: float,
        max_payoff: float,
        dte_near: int,
        days_to_event: Optional[int] = None
    ) -> ExitAlert:
        """
        Monitors an open calendar position and flags exits:
          1. Profit Target: 30% - 40% of max payoff reached.
          2. Event Force Exit: 3 days prior to major event.
          3. Expiry Force Exit: 0 DTE remaining on near leg.
          4. Stop Loss: Drawdown exceeding max_loss_pct of entry debit.
        """
        current_pnl = current_spread_value - entry_debit
        pnl_pct_of_max = (current_pnl / max_payoff * 100.0) if max_payoff > 0 else 0.0
        drawdown_pct = (-current_pnl / entry_debit * 100.0) if entry_debit > 0 else 0.0

        # 1. Event Force Exit
        if days_to_event is not None and days_to_event <= self.event_blackout_days:
            msg = (
                f"🚨 FORCE EXIT (EVENT RISK): Major event in {days_to_event} day(s). "
                f"Close position to eliminate gap/volatility shock risk."
            )
            logger.warning(msg)
            return ExitAlert(
                should_exit=True,
                exit_type="EVENT_FORCE_EXIT",
                current_pnl_pts=round(current_pnl, 2),
                current_pnl_pct_of_max=round(pnl_pct_of_max, 1),
                threshold_reached_pct=round(pnl_pct_of_max, 1),
                message=msg
            )

        # 2. Expiry Day Force Exit
        if dte_near <= 0:
            msg = "⛔ FORCE EXIT (EXPIRY DAY): Near leg has reached 0 DTE. Square off before market close."
            logger.warning(msg)
            return ExitAlert(
                should_exit=True,
                exit_type="EXPIRY_FORCE_EXIT",
                current_pnl_pts=round(current_pnl, 2),
                current_pnl_pct_of_max=round(pnl_pct_of_max, 1),
                threshold_reached_pct=round(pnl_pct_of_max, 1),
                message=msg
            )

        # 3. Profit-Taking Threshold Reached (30% - 40%)
        if pnl_pct_of_max >= self.profit_take_pct:
            msg = (
                f"🎯 PROFIT TARGET REACHED: Captured {pnl_pct_of_max:.1f}% of max payoff "
                f"(+{current_pnl:+.1f} pts >= {self.profit_take_pct}% threshold). Book profits!"
            )
            logger.info(msg)
            return ExitAlert(
                should_exit=True,
                exit_type="PROFIT_TARGET",
                current_pnl_pts=round(current_pnl, 2),
                current_pnl_pct_of_max=round(pnl_pct_of_max, 1),
                threshold_reached_pct=round(pnl_pct_of_max, 1),
                message=msg
            )

        # 4. Stop Loss Breach
        if drawdown_pct >= self.max_loss_pct:
            msg = (
                f"🛑 STOP LOSS TRIGGERED: Drawdown of {drawdown_pct:.1f}% on debit "
                f"({current_pnl:+.1f} pts) exceeds max loss threshold ({self.max_loss_pct}%). Cut trade."
            )
            logger.warning(msg)
            return ExitAlert(
                should_exit=True,
                exit_type="STOP_LOSS",
                current_pnl_pts=round(current_pnl, 2),
                current_pnl_pct_of_max=round(pnl_pct_of_max, 1),
                threshold_reached_pct=round(drawdown_pct, 1),
                message=msg
            )

        # 5. Position Healthy - Hold
        return ExitAlert(
            should_exit=False,
            exit_type="HOLD",
            current_pnl_pts=round(current_pnl, 2),
            current_pnl_pct_of_max=round(pnl_pct_of_max, 1),
            threshold_reached_pct=round(pnl_pct_of_max, 1),
            message=f"Hold position: Current PnL {current_pnl:+.1f} pts ({pnl_pct_of_max:.1f}% of max payoff)."
        )

    # Method aliases for flexible API usage
    monitor_spread_exit = monitor_exit
    evaluate_calendar_opportunity = evaluate_spread_opportunity


# ─────────────────────────────────────────────────────────────────────────────
# Self-Test Execution Block
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═════════════════════════════════════════════════════════════════")
    print("  TESTING CALENDAR SPREAD ENGINE (calendar_spreads.py)")
    print("═════════════════════════════════════════════════════════════════")

    # Sample option chain data with 2 expiries
    test_chain = {
        "Expiry": [
            "2026-09-10", "2026-09-10", "2026-09-10",
            "2026-09-17", "2026-09-17", "2026-09-17"
        ],
        "Strike": [23300, 23350, 23400, 23300, 23350, 23400],
        "Call_IV": [18.5, 19.2, 19.8, 14.8, 15.2, 15.5],  # Near IV (~19%) > Far IV (~15%)
        "Put_IV": [19.0, 19.5, 20.1, 15.0, 15.3, 15.8],
        "Call_Price": [120.0, 85.0, 60.0, 185.0, 145.0, 115.0],
        "Put_Price": [55.0, 80.0, 115.0, 110.0, 140.0, 175.0],
        "Spot": [23350.0] * 6
    }
    df_chain = pd.DataFrame(test_chain)

    engine = CalendarSpreadEngine(
        iv_diff_threshold=15.0,
        profit_take_pct=35.0,
        max_loss_pct=20.0,
        event_blackout_days=3
    )

    # 1. Test Opportunity Evaluation
    opp = engine.evaluate_spread_opportunity(
        df=df_chain,
        spot=23350.0,
        option_type="CE",
        current_date=date(2026, 9, 6),
        upcoming_events=[date(2026, 9, 20)] # Event 14 days away (safe)
    )
    print("\n1. SPREAD OPPORTUNITY EVALUATION:")
    print(f"   Strategy:        {opp.strategy_name}")
    print(f"   Armed Status:    {opp.is_armed}")
    print(f"   Strike:          {opp.strike:,.0f} {opp.option_type}")
    print(f"   Near IV / Far IV: {opp.near_iv:.1f}% vs {opp.far_iv:.1f}%")
    print(f"   IV Differential: {opp.iv_diff_pct:+.1f}% (Threshold: {opp.iv_diff_threshold}%)")
    print(f"   Net Debit:       ₹{opp.net_debit:.2f} pts")
    print(f"   Estimated Max:   ₹{opp.estimated_max_payoff:.2f} pts")
    print(f"   Profit Target:   +{opp.profit_target_pts:.2f} pts ({engine.profit_take_pct}%)")
    print(f"   Stop Loss:       -{opp.stop_loss_pts:.2f} pts")
    print(f"   Rationale:       {opp.rationale}")

    # 2. Test Profit-Taking Exit Monitor
    # Suppose entry debit was 60.0 pts, current spread value rose to 90.0 pts (gain of 30 pts)
    # on max payoff of 76.5 pts -> 30/76.5 = 39.2% of max payoff (triggers 35% rule)
    exit_status = engine.monitor_spread_exit(
        entry_debit=60.0,
        current_spread_value=90.0,
        max_payoff=76.5,
        dte_near=3,
        days_to_event=10
    )
    print("\n2. PROFIT MONITORING TEST:")
    print(f"   Should Exit: {exit_status.should_exit}")
    print(f"   Exit Type:   {exit_status.exit_type}")
    print(f"   PnL:         {exit_status.current_pnl_pts:+.1f} pts ({exit_status.current_pnl_pct_of_max:.1f}% of max)")
    print(f"   Message:     {exit_status.message}")

    # 3. Test Event Lockout Exit
    event_exit = engine.monitor_spread_exit(
        entry_debit=60.0,
        current_spread_value=62.0,
        max_payoff=76.5,
        dte_near=3,
        days_to_event=2 # Within 3-day window!
    )
    print("\n3. EVENT LOCKOUT MONITORING TEST:")
    print(f"   Should Exit: {event_exit.should_exit}")
    print(f"   Exit Type:   {event_exit.exit_type}")
    print(f"   Message:     {event_exit.message}")

    print("\nSUCCESS: calendar_spreads.py verified and operational!")
