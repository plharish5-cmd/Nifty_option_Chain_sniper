"""
anti_fakeout_pressure_engine.py — Algorithmic Engine for The "Truth" Option Chain,
Real-Time Pressure Gauge, Fakeout Detection (VWAP + OI Divergence), Risk Discipline HUD,
and Macroeconomic Event Surveillance.

Built strictly from the Option Seller's perspective using Open Interest as the ultimate truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# 1. Data Models
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CoreStrikeQuote:
    strike: float
    ce_oi: float
    ce_chg_oi: float
    pe_oi: float
    pe_chg_oi: float
    ce_ltp: float
    pe_ltp: float
    ce_vol: float
    pe_vol: float
    ce_balloon: str             # "INFLATING" (heavy adding) or "DEFLATING" (unwinding)
    pe_balloon: str


@dataclass
class TrueSRResult:
    true_resistance_strike: float
    true_resistance_oi: float
    true_resistance_chg: float
    true_resistance_status: str     # 'DEFENDING' (chg > 0) or 'UNWINDING' (chg < 0)
    true_support_strike: float
    true_support_oi: float
    true_support_chg: float
    true_support_status: str        # 'DEFENDING' (chg > 0) or 'UNWINDING' (chg < 0)
    atm_strike: float
    corridor_width: float           # true_resistance - true_support


@dataclass
class PressureGaugeResult:
    call_weight_oi: float           # Total Call OI in 200-pt core
    put_weight_oi: float            # Total Put OI in 200-pt core
    call_momentum_chg: float        # Total Call ΔOI in 200-pt core
    put_momentum_chg: float         # Total Put ΔOI in 200-pt core
    pcr_oi: float
    pcr_chg: float
    net_pressure_index: float       # -100 (Max Downward Pressure) to +100 (Max Upward Pressure)
    pressure_regime: str            # 'BEARISH_COMPRESSION', 'BULLISH_LIFT', 'EQUAL_PRESSURE'
    balloon_status: str             # Visual balloon description
    dominant_side: str              # 'CALL_SELLERS (BEARS)', 'PUT_SELLERS (BULLS)', 'BALANCED'
    call_weight_pct: float          # e.g. 54.2%
    put_weight_pct: float           # e.g. 45.8%


@dataclass
class FakeoutDetectorResult:
    status: str                     # 'FAKE_BREAKOUT', 'GENUINE_BREAKOUT', 'FAKE_BREAKDOWN', 'GENUINE_BREAKDOWN', 'CORRIDOR_WAIT'
    action_prompt: str              # Crisp actionable directive (e.g. 'DO NOT BUY CALLS', 'HIGH PROBABILITY PUT')
    rationale: str
    is_fakeout: bool
    is_divergence: bool
    tested_level: str               # 'TRUE_RESISTANCE', 'TRUE_SUPPORT', 'MID_CORRIDOR'
    tested_strike: float
    spot_vs_vwap: str               # 'ABOVE_VWAP', 'BELOW_VWAP', 'AT_VWAP'
    vwap_val: Optional[float] = None
    spot_price: float = 0.0


@dataclass
class MacroEvent:
    title: str
    category: str                   # 'CENTRAL_BANK', 'INFLATION', 'EMPLOYMENT', 'BUDGET'
    scheduled_time: datetime
    impact_level: str               # 'HIGH', 'CRITICAL', 'MEDIUM'
    description: str


@dataclass
class MacroEventStatus:
    next_event: Optional[MacroEvent]
    hours_remaining: float
    is_critical: bool               # Event within 2 hours -> Trading Halted
    is_alert: bool                  # Event within 12 hours -> Caution
    warning_banner_text: str
    trading_permitted: bool


@dataclass
class RiskDisciplineStatus:
    rr_ratio: float
    rr_locked: bool                 # True if RR < 2.0 (Execution blocked)
    base_capital: float
    realized_pnl: float
    current_equity: float
    house_money_pool: float         # Riskable profit buffer
    max_risk_inr: float             # Max permissible risk on next trade
    daily_trades_count: int
    max_daily_trades: int
    profit_target: float
    max_loss_limit: float
    killswitch_active: bool
    killswitch_reason: str
    action_recommendation: str      # e.g. 'EXECUTE', 'LOCKED (LOW RR)', 'SIT QUIETLY'


# ─────────────────────────────────────────────────────────────────────────────
# 2. The "Truth" Option Chain Engine (200-pt Constraint)
# ─────────────────────────────────────────────────────────────────────────────
class TruthOptionChainEngine:
    """
    Analyzes Option Chain data strictly from the Option Seller's perspective
    constrained to a 200-point range around ATM (2 strikes above, 2 strikes below).
    """

    @staticmethod
    def extract_core_200pt_range(
        df: pd.DataFrame,
        spot: float,
    ) -> Tuple[pd.DataFrame, float]:
        """
        Restricts the option chain strictly to a 200-point range around ATM.
        ATM = round(spot / 50) * 50.
        Strikes: [ATM - 100, ATM + 100] (exactly 5 strikes: ATM-100, ATM-50, ATM, ATM+50, ATM+100).
        """
        if df.empty or spot <= 0:
            return pd.DataFrame(), 0.0

        d = df.copy()
        # Normalize column names
        col_map = {}
        for c in d.columns:
            cl = str(c).lower().strip().replace(" ", "_").replace("-", "_")
            if cl in ("strike", "strike_price"):
                col_map[c] = "strike"
            elif cl in ("ce_oi", "call_oi", "coi"):
                col_map[c] = "ce_oi"
            elif cl in ("ce_chg_oi", "call_oi_chg", "call_chg_oi"):
                col_map[c] = "ce_chg_oi"
            elif cl in ("pe_oi", "put_oi", "poi"):
                col_map[c] = "pe_oi"
            elif cl in ("pe_chg_oi", "put_oi_chg", "put_chg_oi"):
                col_map[c] = "pe_chg_oi"
            elif cl in ("ce_ltp", "call_ltp", "call_price"):
                col_map[c] = "ce_ltp"
            elif cl in ("pe_ltp", "put_ltp", "put_price"):
                col_map[c] = "pe_ltp"
            elif cl in ("ce_vol", "call_vol", "c_vol"):
                col_map[c] = "ce_vol"
            elif cl in ("pe_vol", "put_vol", "p_vol"):
                col_map[c] = "pe_vol"

        d = d.rename(columns=col_map)
        for col in ["strike", "ce_oi", "ce_chg_oi", "pe_oi", "pe_chg_oi", "ce_ltp", "pe_ltp", "ce_vol", "pe_vol"]:
            if col not in d.columns:
                d[col] = 0.0
            else:
                d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)

        atm = round(spot / 50.0) * 50.0
        core = d[(d["strike"] >= atm - 100.0) & (d["strike"] <= atm + 100.0)].sort_values("strike").copy()

        # Fallback if sparse strikes
        if len(core) < 3 and len(d) >= 5:
            d["diff"] = (d["strike"] - spot).abs()
            core = d.nsmallest(5, "diff").sort_values("strike").drop(columns=["diff"])

        return core.reset_index(drop=True), atm

    @classmethod
    def calculate_true_sr(
        cls,
        core_df: pd.DataFrame,
        atm: float,
    ) -> TrueSRResult:
        """
        Calculates True Resistance (highest Call OI in 200-pt core)
        and True Support (highest Put OI in 200-pt core).
        """
        if core_df.empty:
            return TrueSRResult(
                true_resistance_strike=atm + 50.0,
                true_resistance_oi=0.0,
                true_resistance_chg=0.0,
                true_resistance_status="UNKNOWN",
                true_support_strike=atm - 50.0,
                true_support_oi=0.0,
                true_support_chg=0.0,
                true_support_status="UNKNOWN",
                atm_strike=atm,
                corridor_width=100.0,
            )

        max_ce_idx = core_df["ce_oi"].idxmax()
        max_pe_idx = core_df["pe_oi"].idxmax()

        r_row = core_df.loc[max_ce_idx]
        s_row = core_df.loc[max_pe_idx]

        r_strike = float(r_row["strike"])
        r_oi = float(r_row["ce_oi"])
        r_chg = float(r_row["ce_chg_oi"])
        r_status = "DEFENDING" if r_chg > 0 else "UNWINDING"

        s_strike = float(s_row["strike"])
        s_oi = float(s_row["pe_oi"])
        s_chg = float(s_row["pe_chg_oi"])
        s_status = "DEFENDING" if s_chg > 0 else "UNWINDING"

        return TrueSRResult(
            true_resistance_strike=r_strike,
            true_resistance_oi=r_oi,
            true_resistance_chg=r_chg,
            true_resistance_status=r_status,
            true_support_strike=s_strike,
            true_support_oi=s_oi,
            true_support_chg=s_chg,
            true_support_status=s_status,
            atm_strike=atm,
            corridor_width=abs(r_strike - s_strike),
        )

    @classmethod
    def calculate_pressure_gauge(
        cls,
        core_df: pd.DataFrame,
    ) -> PressureGaugeResult:
        """
        Calculates the Pressure Gauge (Change in OI as a gas balloon pressure metric).
        - Call ΔOI > 0: Downward compression accumulating (balloon compressed).
        - Call ΔOI < 0: Downward pressure released (sellers unwinding).
        - Put ΔOI > 0: Upward floor lifting.
        - Put ΔOI < 0: Upward floor deflating.
        """
        if core_df.empty:
            return PressureGaugeResult(
                call_weight_oi=0.0,
                put_weight_oi=0.0,
                call_momentum_chg=0.0,
                put_momentum_chg=0.0,
                pcr_oi=1.0,
                pcr_chg=1.0,
                net_pressure_index=0.0,
                pressure_regime="EQUAL_PRESSURE",
                balloon_status="Neutral Equilibrium",
                dominant_side="BALANCED",
                call_weight_pct=50.0,
                put_weight_pct=50.0,
            )

        call_weight = float(core_df["ce_oi"].sum())
        put_weight = float(core_df["pe_oi"].sum())
        call_momentum = float(core_df["ce_chg_oi"].sum())
        put_momentum = float(core_df["pe_chg_oi"].sum())

        tot_weight = call_weight + put_weight
        call_pct = (call_weight / tot_weight * 100.0) if tot_weight > 0 else 50.0
        put_pct = (put_weight / tot_weight * 100.0) if tot_weight > 0 else 50.0

        pcr_oi = (put_weight / call_weight) if call_weight > 0 else 1.0
        pcr_chg = (put_momentum / call_momentum) if call_momentum > 0 else (1.0 if call_momentum == 0 else 0.0)

        # Net Pressure Index: Put ΔOI (lift) minus Call ΔOI (compression)
        # Normalized between -100 and +100
        net_diff = put_momentum - call_momentum
        tot_abs_chg = abs(put_momentum) + abs(call_momentum)
        if tot_abs_chg > 0:
            net_pressure_idx = (net_diff / tot_abs_chg) * 100.0
        else:
            net_pressure_idx = 0.0

        net_pressure_idx = max(-100.0, min(100.0, net_pressure_idx))

        if net_pressure_idx >= 22.0:
            regime = "BULLISH_LIFT"
            balloon = "🎈 Pressure Released Above: Put sellers pumping upward floor"
            dominant = "PUT_SELLERS (BULLS)"
        elif net_pressure_idx <= -22.0:
            regime = "BEARISH_COMPRESSION"
            balloon = "💨 Downward Pressure Building: Call sellers actively compressing ceiling"
            dominant = "CALL_SELLERS (BEARS)"
        else:
            regime = "EQUAL_PRESSURE"
            balloon = "⚖️ Balanced Pressure: Gas balloon equilibrium (Tug-of-War Deadlock)"
            dominant = "BALANCED"

        return PressureGaugeResult(
            call_weight_oi=call_weight,
            put_weight_oi=put_weight,
            call_momentum_chg=call_momentum,
            put_momentum_chg=put_momentum,
            pcr_oi=pcr_oi,
            pcr_chg=pcr_chg,
            net_pressure_index=net_pressure_idx,
            pressure_regime=regime,
            balloon_status=balloon,
            dominant_side=dominant,
            call_weight_pct=call_pct,
            put_weight_pct=put_pct,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fakeout Detector (VWAP + OI Divergence)
# ─────────────────────────────────────────────────────────────────────────────
class FakeoutDetectorEngine:
    """
    Exposes institutional traps by cross-referencing Price (OHLCV/VWAP)
    with Open Interest Writer behavior.
    """

    @staticmethod
    def detect_fakeout(
        spot: float,
        vwap: Optional[float],
        true_sr: TrueSRResult,
        pressure: PressureGaugeResult,
        candle_high: Optional[float] = None,
        candle_low: Optional[float] = None,
    ) -> FakeoutDetectorResult:
        """
        Evaluates genuine vs fake breakout/breakdown:
        - FAKE_BREAKOUT: Price breaks above True Resistance / VWAP, but Call ΔOI at resistance
          remains heavily positive (sellers defending) and PCR ΔOI remains balanced or bearish.
        - GENUINE_BREAKOUT: Price breaks above True Resistance + Call ΔOI < 0 (sellers unwinding).
        - FAKE_BREAKDOWN: Price breaks below True Support / VWAP, but Put ΔOI remains positive.
        - GENUINE_BREAKDOWN: Price breaks below True Support + Put ΔOI < 0.
        """
        high_val = candle_high if candle_high is not None else spot
        low_val = candle_low if candle_low is not None else spot
        vwap_val = vwap if (vwap is not None and not np.isnan(vwap) and vwap > 0) else spot

        spot_vs_vwap = "AT_VWAP"
        if spot > vwap_val + 3.0:
            spot_vs_vwap = "ABOVE_VWAP"
        elif spot < vwap_val - 3.0:
            spot_vs_vwap = "BELOW_VWAP"

        r_strike = true_sr.true_resistance_strike
        s_strike = true_sr.true_support_strike
        r_chg = true_sr.true_resistance_chg
        s_chg = true_sr.true_support_chg

        # ── Test 1: Resistance Interaction (Breakout Evaluation) ──
        # In testing range if spot or candle high reached within 15 pts of R or breached R
        if high_val >= r_strike - 15.0 or spot >= r_strike - 15.0:
            is_breaching = spot >= r_strike or high_val >= r_strike

            # Condition for FAKE BREAKOUT:
            # Price breached or testing ceiling, but Call writers are ADDING contracts (+ΔOI)
            # and Put/Call change ratio is not heavily bullish.
            if r_chg > 0 and pressure.pcr_chg <= 1.20:
                return FakeoutDetectorResult(
                    status="FAKE_BREAKOUT",
                    action_prompt="DO NOT BUY CALLS — CALL SELLERS STANDING FIRM",
                    rationale=(
                        f"Price tested/breached True Resistance {r_strike:,.0f} ({spot_vs_vwap}), "
                        f"but Call Writers are HEAVILY DEFENDING ({r_chg:+,.0f} contracts added). "
                        f"Sellers are trapping breakout buyers. Prepare for rejection reversal."
                    ),
                    is_fakeout=True,
                    is_divergence=True,
                    tested_level="TRUE_RESISTANCE",
                    tested_strike=r_strike,
                    spot_vs_vwap=spot_vs_vwap,
                    vwap_val=vwap_val,
                    spot_price=spot,
                )
            elif r_chg < 0:
                return FakeoutDetectorResult(
                    status="GENUINE_BREAKOUT",
                    action_prompt="HIGH PROBABILITY CALL — RESISTANCE BROKEN & SELLERS COVERING",
                    rationale=(
                        f"Price cleared True Resistance {r_strike:,.0f} ({spot_vs_vwap}), and Call Writers "
                        f"are IN PANIC COVERING ({r_chg:+,.0f} unwound). "
                        f"Pressure balloon released upward! Clear institutional follow-through."
                    ),
                    is_fakeout=False,
                    is_divergence=False,
                    tested_level="TRUE_RESISTANCE",
                    tested_strike=r_strike,
                    spot_vs_vwap=spot_vs_vwap,
                    vwap_val=vwap_val,
                    spot_price=spot,
                )

        # ── Test 2: Support Interaction (Breakdown Evaluation) ──
        if low_val <= s_strike + 15.0 or spot <= s_strike + 15.0:
            is_breaking_down = spot <= s_strike or low_val <= s_strike

            # Condition for FAKE BREAKDOWN:
            # Price dipped below floor, but Put writers are actively ADDING contracts (+ΔOI)
            if s_chg > 0 and pressure.pcr_chg >= 0.80:
                return FakeoutDetectorResult(
                    status="FAKE_BREAKDOWN",
                    action_prompt="DO NOT BUY PUTS — PUT SELLERS FORTIFYING FLOOR",
                    rationale=(
                        f"Price tested/pierced True Support {s_strike:,.0f} ({spot_vs_vwap}), "
                        f"but Put Writers are AGGRESSIVELY ABSORBING ({s_chg:+,.0f} contracts added). "
                        f"Bear trap active. High odds of sharp V-shape bounce back above VWAP."
                    ),
                    is_fakeout=True,
                    is_divergence=True,
                    tested_level="TRUE_SUPPORT",
                    tested_strike=s_strike,
                    spot_vs_vwap=spot_vs_vwap,
                    vwap_val=vwap_val,
                    spot_price=spot,
                )
            elif s_chg < 0:
                return FakeoutDetectorResult(
                    status="GENUINE_BREAKDOWN",
                    action_prompt="HIGH PROBABILITY PUT — SUPPORT FLOOR COLLAPSED",
                    rationale=(
                        f"Price cracked True Support {s_strike:,.0f} ({spot_vs_vwap}), and Put Writers "
                        f"are IN PANIC COVERING ({s_chg:+,.0f} unwound). "
                        f"Institutional floor disintegrated. Downside cascade active."
                    ),
                    is_fakeout=False,
                    is_divergence=False,
                    tested_level="TRUE_SUPPORT",
                    tested_strike=s_strike,
                    spot_vs_vwap=spot_vs_vwap,
                    vwap_val=vwap_val,
                    spot_price=spot,
                )

        # ── Test 3: Mid-Corridor Equilibrium ──
        if pressure.pressure_regime == "BULLISH_LIFT":
            return FakeoutDetectorResult(
                status="CORRIDOR_WAIT",
                action_prompt="ACCUMULATION ON DIPS: Put Writers Dominating Tug-of-War",
                rationale=(
                    f"Price ({spot:,.1f}) trading in {s_strike:,.0f}–{r_strike:,.0f} corridor. "
                    f"Net Pressure Index +{pressure.net_pressure_index:.1f}% indicates upward lift. "
                    f"Buy dips near {s_strike:,.0f}, target {r_strike:,.0f}."
                ),
                is_fakeout=False,
                is_divergence=False,
                tested_level="MID_CORRIDOR",
                tested_strike=s_strike,
                spot_vs_vwap=spot_vs_vwap,
                vwap_val=vwap_val,
                spot_price=spot,
            )
        elif pressure.pressure_regime == "BEARISH_COMPRESSION":
            return FakeoutDetectorResult(
                status="CORRIDOR_WAIT",
                action_prompt="FADE RALLIES: Call Writers Compressing Ceiling",
                rationale=(
                    f"Price ({spot:,.1f}) trading in {s_strike:,.0f}–{r_strike:,.0f} corridor. "
                    f"Net Pressure Index {pressure.net_pressure_index:.1f}% indicates downward compression. "
                    f"Sell rallies near {r_strike:,.0f}, target {s_strike:,.0f}."
                ),
                is_fakeout=False,
                is_divergence=False,
                tested_level="MID_CORRIDOR",
                tested_strike=r_strike,
                spot_vs_vwap=spot_vs_vwap,
                vwap_val=vwap_val,
                spot_price=spot,
            )
        else:
            return FakeoutDetectorResult(
                status="CORRIDOR_WAIT",
                action_prompt="WAIT: Pressure is Equal (Balanced Tug-of-War)",
                rationale=(
                    f"Price trapped inside 200-pt core between S1 ({s_strike:,.0f}) and R1 ({r_strike:,.0f}). "
                    f"Tug-of-war is deadlocked. No institutional edge until boundary test or pressure breakout."
                ),
                is_fakeout=False,
                is_divergence=False,
                tested_level="MID_CORRIDOR",
                tested_strike=true_sr.atm_strike,
                spot_vs_vwap=spot_vs_vwap,
                vwap_val=vwap_val,
                spot_price=spot,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Macroeconomic Event & Volatility Spike Overlay
# ─────────────────────────────────────────────────────────────────────────────
class MacroEventEngine:
    """
    Monitors high-impact macroeconomic events (RBI MPC, US Fed FOMC, CPI, NFP)
    to enforce protective trading halts prior to 200–300 point spike risks.
    """

    # Curated institutional schedule of high-impact events
    SCHEDULED_EVENTS: List[Dict[str, Any]] = [
        {
            "title": "RBI Monetary Policy Committee (MPC) Rate Decision",
            "category": "CENTRAL_BANK",
            "time_iso": "2026-09-11T10:00:00",
            "impact": "CRITICAL",
            "desc": "RBI repo rate verdict & Governor policy speech. Historical 200-300 pt Nifty spike zone.",
        },
        {
            "title": "India CPI Inflation & IIP Data",
            "category": "INFLATION",
            "time_iso": "2026-09-12T17:30:00",
            "impact": "HIGH",
            "desc": "National headline inflation and manufacturing output.",
        },
        {
            "title": "US Federal Reserve FOMC Interest Rate Decision",
            "category": "CENTRAL_BANK",
            "time_iso": "2026-09-16T23:30:00",
            "impact": "CRITICAL",
            "desc": "Fed fund rate, dot plot & Jerome Powell press conference.",
        },
        {
            "title": "US Headline CPI Inflation Release",
            "category": "INFLATION",
            "time_iso": "2026-09-15T18:00:00",
            "impact": "HIGH",
            "desc": "Monthly US consumer price index.",
        },
        {
            "title": "RBI MPC Policy Review Meeting",
            "category": "CENTRAL_BANK",
            "time_iso": "2026-10-09T10:00:00",
            "impact": "CRITICAL",
            "desc": "Bimonthly rate policy announcement.",
        },
    ]

    @classmethod
    def evaluate_macro_status(
        cls,
        now: Optional[datetime] = None,
    ) -> MacroEventStatus:
        """
        Evaluates proximity to upcoming high-impact macroeconomic events.
        Enforces halt if event is within 2 hours (< 2.0 hrs).
        """
        current_dt = now or datetime.now()

        upcoming: List[Tuple[float, MacroEvent]] = []
        for ev in cls.SCHEDULED_EVENTS:
            try:
                ev_dt = datetime.fromisoformat(ev["time_iso"])
                diff_hours = (ev_dt - current_dt).total_seconds() / 3600.0
                if diff_hours >= -1.0:  # Include event up to 1 hr after occurrence
                    macro_obj = MacroEvent(
                        title=ev["title"],
                        category=ev["category"],
                        scheduled_time=ev_dt,
                        impact_level=ev["impact"],
                        description=ev["desc"],
                    )
                    upcoming.append((diff_hours, macro_obj))
            except Exception:
                continue

        upcoming.sort(key=lambda x: x[0])

        if not upcoming:
            return MacroEventStatus(
                next_event=None,
                hours_remaining=999.0,
                is_critical=False,
                is_alert=False,
                warning_banner_text="No High-Impact Macroeconomic Events within 48 hours. Normal Execution Permitted.",
                trading_permitted=True,
            )

        nearest_hours, nearest_ev = upcoming[0]

        is_critical = 0.0 <= nearest_hours <= 2.0
        is_alert = 2.0 < nearest_hours <= 12.0
        trading_permitted = not is_critical

        if is_critical:
            banner = (
                f"🚨 CRITICAL MACRO ALERT: {nearest_ev.title} in {nearest_hours*60:.0f} minutes! "
                f"200–300 pt volatility shock imminent. TRADING HALTED — DO NOT HOLD NAKED OPTIONS."
            )
        elif is_alert:
            banner = (
                f"⚠️ HIGH MACRO IMPACT TODAY: {nearest_ev.title} scheduled in {nearest_hours:.1f} hours. "
                f"Cut position sizing by 50% and strictly avoid overnight carry."
            )
        else:
            banner = (
                f"📅 Upcoming Macro: {nearest_ev.title} in {nearest_hours:.0f} hours ({nearest_ev.scheduled_time.strftime('%b %d, %H:%M')}). "
                f"Current regime clear for intraday execution."
            )

        return MacroEventStatus(
            next_event=nearest_ev,
            hours_remaining=nearest_hours,
            is_critical=is_critical,
            is_alert=is_alert,
            warning_banner_text=banner,
            trading_permitted=trading_permitted,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Risk Management & Discipline HUD Engine
# ─────────────────────────────────────────────────────────────────────────────
class RiskDisciplineEngine:
    """
    Enforces mechanical capital protection, 1:2 R:R lock,
    profits re-risking limits, and overtrading killswitch.
    """

    @staticmethod
    def evaluate_trade_risk(
        entry_price: float,
        stop_loss: float,
        target_price: float,
        base_capital: float = 100000.0,
        realized_pnl: float = 0.0,
        daily_trades_count: int = 0,
        max_daily_trades: int = 4,
        daily_profit_target: float = 3000.0,
        daily_max_loss: float = 1500.0,
    ) -> RiskDisciplineStatus:
        """
        Calculates Risk-to-Reward ratio, locks execution if RR < 2.0,
        and triggers the Overtrading Killswitch if limits are reached.
        """
        risk_pts = abs(entry_price - stop_loss)
        reward_pts = abs(target_price - entry_price)

        rr_ratio = (reward_pts / risk_pts) if risk_pts > 0 else 0.0
        rr_locked = rr_ratio < 2.0

        current_equity = base_capital + realized_pnl

        # ── Profits Re-risking Rule (House Money) ──
        # If flat or in loss, max risk is 1% of base capital
        # If in profit, max risk can use up to 35% of banked profits, shielding base capital
        if realized_pnl <= 0:
            house_money = 0.0
            max_risk_inr = base_capital * 0.01  # ₹1,000 max risk
        else:
            house_money = realized_pnl
            # Shield principal completely: risk only a fraction of profit
            max_risk_inr = min(base_capital * 0.015, realized_pnl * 0.35)

        # ── Overtrading Killswitch Checks ──
        killswitch = False
        reason = "Normal Active Execution"
        rec = "EXECUTE APPROVED"

        if daily_trades_count >= max_daily_trades:
            killswitch = True
            reason = f"🛑 DAILY TRADE LIMIT EXHAUSTED ({daily_trades_count}/{max_daily_trades} trades taken). Step away to prevent fatigue."
            rec = "SIT QUIETLY"
        elif realized_pnl >= daily_profit_target:
            killswitch = True
            reason = f"🟢 DAILY PROFIT TARGET HIT (+₹{realized_pnl:,.0f} >= ₹{daily_profit_target:,.0f}). Bank your gains and SIT QUIETLY."
            rec = "SIT QUIETLY"
        elif realized_pnl <= -abs(daily_max_loss):
            killswitch = True
            reason = f"🛑 DAILY MAX LOSS HIT (-₹{abs(realized_pnl):,.0f} <= -₹{abs(daily_max_loss):,.0f}). Trading stopped to preserve capital."
            rec = "SIT QUIETLY"
        elif rr_locked:
            rec = f"LOCKED: R:R is 1:{rr_ratio:.2f} (Required ≥ 1:2.0)"

        return RiskDisciplineStatus(
            rr_ratio=rr_ratio,
            rr_locked=rr_locked,
            base_capital=base_capital,
            realized_pnl=realized_pnl,
            current_equity=current_equity,
            house_money_pool=house_money,
            max_risk_inr=max_risk_inr,
            daily_trades_count=daily_trades_count,
            max_daily_trades=max_daily_trades,
            profit_target=daily_profit_target,
            max_loss_limit=daily_max_loss,
            killswitch_active=killswitch,
            killswitch_reason=reason,
            action_recommendation=rec,
        )
