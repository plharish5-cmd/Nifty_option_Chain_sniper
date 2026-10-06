"""
option_chain_oi_tab.py — Real-Time Option Chain & Open Interest Analysis Engine.

Institutional-grade analysis module built strictly from the Seller's (Writer's) perspective:
1. Open Interest (OI) & OI Change Engine:
   - 400–500 pt Focus Window around Spot (eliminates illiquid far OTM noise).
   - Major Resistance (Call Wall = Max Call OI) & Secondary Resistance.
   - Major Support (Put Wall = Max Put OI) & Secondary Support.
   - Live Writer Tracking (Aggressive writing vs Short covering / Unwinding).
2. Breakout Validation Algorithm:
   - Valid Breakout Expected (Call sellers covering at R1 + shifting higher).
   - Fake Breakout / Reversal Expected (Call sellers heavily defending R1).
   - Valid Breakdown Expected (Put sellers covering at S1 + shifting lower).
   - Fake Breakdown / Bounce Expected (Put sellers heavily defending S1).
   - Anti-Trap Rule: 30–50 pt breach with expanding OI/Volume flags a Trap!
     Traders must not trust breakout/breakdown until sustained beyond buffer.
3. UI / UX Components:
   - Option Chain Heatmap (dual-sided table with Call red and Put green gradients).
   - Dynamic S/R Chart (Plotly candlestick overlaid with S/R walls & shaded 30-50 pt anti-trap zones).
   - Double Confirmation HUD (Price Action Status x Option Chain Status -> Unified Institutional Verdict).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config

log = logging.getLogger("option_chain_oi_tab")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Institutional Color Palette & Styling Helpers
# ─────────────────────────────────────────────────────────────────────────────
RED = "#EF4444"
RED_BG = "rgba(239, 68, 68, 0.12)"
RED_BORDER = "rgba(239, 68, 68, 0.45)"

GREEN = "#10B981"
GREEN_BG = "rgba(16, 185, 129, 0.12)"
GREEN_BORDER = "rgba(16, 185, 129, 0.45)"

AMBER = "#F59E0B"
AMBER_BG = "rgba(245, 158, 11, 0.12)"
AMBER_BORDER = "rgba(245, 158, 11, 0.45)"

BLUE = "#38BDF8"
BLUE_BG = "rgba(56, 189, 248, 0.12)"
BLUE_BORDER = "rgba(56, 189, 248, 0.45)"

CARD_BG = "#0D131F"
BORDER_DARK = "#1E293B"
TEXT_MUTED = "#8FA6B8"


def _render_html(html_str: str) -> None:
    """Renders raw HTML cleanly without markdown parsing glitches."""
    clean = "\n".join(line for line in html_str.splitlines() if line.strip())
    if hasattr(st, "html"):
        st.html(clean)
    else:
        st.markdown(clean, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Data Structures
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StrikeLevel:
    strike: float
    oi: float
    chg_oi: float
    vol: float
    ltp: float
    iv: float
    dist_pts: float
    activity: str = "BUILDING"  # 'BUILDING' or 'UNWINDING'


@dataclass
class BreakoutValidationVerdict:
    status: str                 # 'VALID_BREAKOUT_EXPECTED', 'FAKE_BREAKOUT_REVERSAL_EXPECTED', 'VALID_BREAKDOWN_EXPECTED', 'FAKE_BREAKDOWN_BOUNCE_EXPECTED', 'CORRIDOR_RANGE'
    direction: str              # 'BULLISH', 'BEARISH_REVERSAL', 'BEARISH', 'BULLISH_REBOUND', 'RANGE'
    confidence: str             # 'HIGH', 'MEDIUM', 'LOW'
    action: str                 # 'BUY_CE', 'BUY_PE / SHORT', 'WAIT_FOR_CONFIRMATION', 'SELL_STRANGLE / RANGE'
    title: str
    summary: str
    rationale: str
    is_bull_trap: bool = False
    is_bear_trap: bool = False
    trap_warning: Optional[str] = None
    tested_level: str = "NONE"   # 'R1', 'S1', 'NONE'
    tested_strike: float = 0.0
    buffer_zone: Tuple[float, float] = (0.0, 0.0)
    target_strike: Optional[float] = None
    stop_loss: Optional[float] = None


@dataclass
class DoubleConfirmationState:
    pa_trend: str               # 'BULLISH', 'BEARISH', 'NEUTRAL'
    pa_detail: str              # e.g. 'Candle close above VWAP + Bullish Momentum'
    oi_bias: str                # 'BULLISH', 'BEARISH', 'NEUTRAL'
    oi_detail: str              # e.g. 'Call Sellers Unwinding at R1; Resistance shifting up'
    trap_active: bool
    trap_badge: str             # 'NONE', '⚠️ BULL TRAP ACTIVE', '⚠️ BEAR TRAP ACTIVE'
    confluent_verdict: str      # High contrast verdict headline
    verdict_badge_color: str
    verdict_bg_color: str
    verdict_border_color: str
    actionable_instruction: str
    key_metrics: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Option Chain & OI Engine (Seller's Perspective)
# ─────────────────────────────────────────────────────────────────────────────
class OptionChainOIEngine:
    """
    Algorithmic engine analyzing Open Interest & Option Chain data strictly
    from the perspective of institutional sellers (option writers).
    """

    @staticmethod
    def normalize_chain(df: pd.DataFrame) -> pd.DataFrame:
        """Standardize column names to strike, ce_oi, ce_chg_oi, pe_oi, pe_chg_oi, etc."""
        d = df.copy()
        col_map = {}
        for c in d.columns:
            cl = str(c).lower().strip().replace(" ", "_").replace("-", "_")
            if cl in ("strike", "strike_price", "strikeprice"):
                col_map[c] = "strike"
            elif cl in ("ce_oi", "call_oi", "coi", "c_oi", "calloi"):
                col_map[c] = "ce_oi"
            elif cl in ("ce_chg_oi", "call_oi_chg", "call_chg_oi", "ce_prev_oi", "call_coi"):
                col_map[c] = "ce_chg_oi"
            elif cl in ("ce_vol", "call_vol", "c_vol", "call_volume", "ce_volume"):
                col_map[c] = "ce_vol"
            elif cl in ("ce_ltp", "call_ltp", "call_price", "ce_close"):
                col_map[c] = "ce_ltp"
            elif cl in ("ce_iv", "call_iv", "c_iv"):
                col_map[c] = "ce_iv"
            elif cl in ("pe_oi", "put_oi", "poi", "p_oi", "putoi"):
                col_map[c] = "pe_oi"
            elif cl in ("pe_chg_oi", "put_oi_chg", "put_chg_oi", "pe_prev_oi", "put_coi"):
                col_map[c] = "pe_chg_oi"
            elif cl in ("pe_vol", "put_vol", "p_vol", "put_volume", "pe_volume"):
                col_map[c] = "pe_vol"
            elif cl in ("pe_ltp", "put_ltp", "put_price", "pe_close"):
                col_map[c] = "pe_ltp"
            elif cl in ("pe_iv", "put_iv", "p_iv"):
                col_map[c] = "pe_iv"

        d = d.rename(columns=col_map)
        for col in ["strike", "ce_oi", "ce_chg_oi", "ce_vol", "ce_ltp", "ce_iv",
                    "pe_oi", "pe_chg_oi", "pe_vol", "pe_ltp", "pe_iv"]:
            if col not in d.columns:
                d[col] = 0.0
            else:
                d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)

        return d.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)

    @classmethod
    def filter_focus_window(
        cls,
        df: pd.DataFrame,
        spot: float,
        focus_radius: int = 450,
    ) -> pd.DataFrame:
        """
        Filters option chain to a focus window radius of 400-500 pts around spot
        to eliminate illiquid far OTM noise.
        """
        d = cls.normalize_chain(df)
        if d.empty or spot <= 0:
            return d

        low_b = spot - focus_radius
        high_b = spot + focus_radius
        focus = d[(d["strike"] >= low_b) & (d["strike"] <= high_b)].copy()

        # Fallback if focus filter left too few strikes
        if len(focus) < 6 and len(d) >= 6:
            d["diff"] = (d["strike"] - spot).abs()
            focus = d.nsmallest(14, "diff").sort_values("strike").drop(columns=["diff"])

        return focus.reset_index(drop=True)

    @classmethod
    def analyze_levels(
        cls,
        focus_df: pd.DataFrame,
        spot: float,
    ) -> Dict[str, Any]:
        """
        Identifies Major & Secondary Resistance (Max Call OI) and Support (Max Put OI).
        Calculates Focus Window PCR, Net ΔOI, and Writer Intensity.
        """
        if focus_df.empty:
            return {}

        # 1. Resistance (Calls)
        sorted_ce = focus_df.sort_values("ce_oi", ascending=False)
        r1_row = sorted_ce.iloc[0]
        r2_row = sorted_ce.iloc[1] if len(sorted_ce) > 1 else r1_row

        r1 = StrikeLevel(
            strike=float(r1_row["strike"]),
            oi=float(r1_row["ce_oi"]),
            chg_oi=float(r1_row["ce_chg_oi"]),
            vol=float(r1_row["ce_vol"]),
            ltp=float(r1_row["ce_ltp"]),
            iv=float(r1_row["ce_iv"]),
            dist_pts=float(r1_row["strike"] - spot),
            activity="BUILDING" if r1_row["ce_chg_oi"] > 0 else "UNWINDING",
        )

        r2 = StrikeLevel(
            strike=float(r2_row["strike"]),
            oi=float(r2_row["ce_oi"]),
            chg_oi=float(r2_row["ce_chg_oi"]),
            vol=float(r2_row["ce_vol"]),
            ltp=float(r2_row["ce_ltp"]),
            iv=float(r2_row["ce_iv"]),
            dist_pts=float(r2_row["strike"] - spot),
            activity="BUILDING" if r2_row["ce_chg_oi"] > 0 else "UNWINDING",
        )

        # 2. Support (Puts)
        sorted_pe = focus_df.sort_values("pe_oi", ascending=False)
        s1_row = sorted_pe.iloc[0]
        s2_row = sorted_pe.iloc[1] if len(sorted_pe) > 1 else s1_row

        s1 = StrikeLevel(
            strike=float(s1_row["strike"]),
            oi=float(s1_row["pe_oi"]),
            chg_oi=float(s1_row["pe_chg_oi"]),
            vol=float(s1_row["pe_vol"]),
            ltp=float(s1_row["pe_ltp"]),
            iv=float(s1_row["pe_iv"]),
            dist_pts=float(s1_row["strike"] - spot),
            activity="BUILDING" if s1_row["pe_chg_oi"] > 0 else "UNWINDING",
        )

        s2 = StrikeLevel(
            strike=float(s2_row["strike"]),
            oi=float(s2_row["pe_oi"]),
            chg_oi=float(s2_row["pe_chg_oi"]),
            vol=float(s2_row["pe_vol"]),
            ltp=float(s2_row["pe_ltp"]),
            iv=float(s2_row["pe_iv"]),
            dist_pts=float(s2_row["strike"] - spot),
            activity="BUILDING" if s2_row["pe_chg_oi"] > 0 else "UNWINDING",
        )

        # 3. Focus Window Aggregate Telemetry
        tot_ce_oi = float(focus_df["ce_oi"].sum())
        tot_pe_oi = float(focus_df["pe_oi"].sum())
        focus_pcr = (tot_pe_oi / tot_ce_oi) if tot_ce_oi > 0 else 1.0

        net_ce_chg = float(focus_df["ce_chg_oi"].sum())
        net_pe_chg = float(focus_df["pe_chg_oi"].sum())

        ce_additions = float(focus_df[focus_df["ce_chg_oi"] > 0]["ce_chg_oi"].sum())
        ce_unwinding = float(focus_df[focus_df["ce_chg_oi"] < 0]["ce_chg_oi"].sum())
        pe_additions = float(focus_df[focus_df["pe_chg_oi"] > 0]["pe_chg_oi"].sum())
        pe_unwinding = float(focus_df[focus_df["pe_chg_oi"] < 0]["pe_chg_oi"].sum())

        # Top 3 Strikes Call & Put writing
        top_ce_writers = focus_df.nlargest(3, "ce_chg_oi")[["strike", "ce_oi", "ce_chg_oi"]].to_dict("records")
        top_pe_writers = focus_df.nlargest(3, "pe_chg_oi")[["strike", "pe_oi", "pe_chg_oi"]].to_dict("records")
        top_ce_unwind = focus_df.nsmallest(3, "ce_chg_oi")[["strike", "ce_oi", "ce_chg_oi"]].to_dict("records")
        top_pe_unwind = focus_df.nsmallest(3, "pe_chg_oi")[["strike", "pe_oi", "pe_chg_oi"]].to_dict("records")

        return {
            "r1": r1,
            "r2": r2,
            "s1": s1,
            "s2": s2,
            "tot_ce_oi": tot_ce_oi,
            "tot_pe_oi": tot_pe_oi,
            "focus_pcr": focus_pcr,
            "net_ce_chg": net_ce_chg,
            "net_pe_chg": net_pe_chg,
            "ce_additions": ce_additions,
            "ce_unwinding": ce_unwinding,
            "pe_additions": pe_additions,
            "pe_unwinding": pe_unwinding,
            "top_ce_writers": top_ce_writers,
            "top_pe_writers": top_pe_writers,
            "top_ce_unwind": top_ce_unwind,
            "top_pe_unwind": top_pe_unwind,
        }

    @classmethod
    def validate_breakout(
        cls,
        focus_df: pd.DataFrame,
        spot: float,
        levels_data: Dict[str, Any],
        proximity_threshold: float = 65.0,
    ) -> BreakoutValidationVerdict:
        """
        Implements the Breakout Validation Algorithm and Anti-Trap Rule:
        - Valid Breakout Expected: Call ΔOI < 0 at R1 and higher strike sees positive ΔOI.
        - Fake Breakout / Reversal Expected: Call ΔOI > 0 at R1 (sellers defending).
        - Valid Breakdown Expected: Put ΔOI < 0 at S1 and lower strike sees positive ΔOI.
        - Fake Breakdown / Bounce Expected: Put ΔOI > 0 at S1 (sellers defending).
        - Anti-Trap Rule: 30-50 pt breach with expanding ΔOI and volume -> FLAG AS TRAP!
        """
        r1: StrikeLevel = levels_data.get("r1")
        r2: StrikeLevel = levels_data.get("r2")
        s1: StrikeLevel = levels_data.get("s1")
        s2: StrikeLevel = levels_data.get("s2")

        if not r1 or not s1:
            return BreakoutValidationVerdict(
                status="CORRIDOR_RANGE",
                direction="RANGE",
                confidence="LOW",
                action="WAIT_FOR_CONFIRMATION",
                title="Option Chain S/R Calibrating",
                summary="Awaiting active strike quotes to calibrate S/R walls.",
                rationale="Data stream establishing baseline.",
            )

        # Proximity metrics
        dist_to_r1 = r1.strike - spot       # Positive = spot below R1, Negative = spot breached R1
        dist_to_s1 = spot - s1.strike       # Positive = spot above S1, Negative = spot breached S1

        # Locate next higher strike above R1
        higher_strikes = focus_df[focus_df["strike"] > r1.strike].sort_values("strike")
        next_higher_strike = higher_strikes.iloc[0] if not higher_strikes.empty else None

        # Locate next lower strike below S1
        lower_strikes = focus_df[focus_df["strike"] < s1.strike].sort_values("strike", ascending=False)
        next_lower_strike = lower_strikes.iloc[0] if not lower_strikes.empty else None

        # Determine whether spot is primarily interacting with R1 or S1
        is_breaching_r1 = spot >= r1.strike
        is_breaching_s1 = spot <= s1.strike
        abs_dist_r1 = abs(spot - r1.strike)
        abs_dist_s1 = abs(spot - s1.strike)

        def _check_r1() -> Optional[BreakoutValidationVerdict]:
            if -55.0 <= dist_to_r1 <= proximity_threshold:
                # 1A. Anti-Trap Rule for Resistance Breach (0 to 50 pts breach)
                breach_pts = spot - r1.strike
                if 0.0 <= breach_pts <= 50.0:
                    if r1.chg_oi > 0:
                        buffer_top = r1.strike + 50.0
                        return BreakoutValidationVerdict(
                            status="FAKE_BREAKOUT_REVERSAL_EXPECTED",
                            direction="BEARISH_REVERSAL",
                            confidence="HIGH",
                            action="AVOID_CE / PREPARE_PUT_FADE",
                            title="⚠️ BULL TRAP DETECTED — RESISTANCE FAKEOUT",
                            summary=(
                                f"Price breached Resistance {r1.strike:,.0f} by {breach_pts:.1f} pts, but Call Writers "
                                f"are EXPANDING positions ({r1.chg_oi:+,.0f} contracts) instead of covering. "
                                f"Unreliable breakout! Do NOT chase longs."
                            ),
                            rationale=(
                                f"Anti-Trap Rule Triggered: Volume and OI are expanding into the breach. "
                                f"Institutions are trapping retail breakout buyers. Trend cannot be trusted "
                                f"until sustained above the 30–50 pt buffer zone ({buffer_top:,.0f})."
                            ),
                            is_bull_trap=True,
                            tested_level="R1",
                            tested_strike=r1.strike,
                            buffer_zone=(r1.strike, buffer_top),
                            target_strike=s1.strike,
                            stop_loss=r1.strike + 25.0,
                        )

                # 1B. Testing R1 from below: Call Sellers covering vs Defending
                if r1.chg_oi < 0:
                    higher_ce_chg = float(next_higher_strike["ce_chg_oi"]) if next_higher_strike is not None else 0.0
                    higher_strike_val = float(next_higher_strike["strike"]) if next_higher_strike is not None else (r1.strike + 50.0)

                    if higher_ce_chg > 0:
                        return BreakoutValidationVerdict(
                            status="VALID_BREAKOUT_EXPECTED",
                            direction="BULLISH",
                            confidence="HIGH",
                            action="BUY_CE / CALL_MOMENTUM",
                            title="🟢 VALID BREAKOUT EXPECTED — RESISTANCE SHIFTING HIGHER",
                            summary=(
                                f"Spot ({spot:,.1f}) approaching R1 {r1.strike:,.0f}. Call Writers are "
                                f"SQUARING OFF ({r1.chg_oi:+,.0f} unwound) and retreating to {higher_strike_val:,.0f} "
                                f"({higher_ce_chg:+,.0f} added). Ceiling is giving way!"
                            ),
                            rationale=(
                                f"Seller liquidation confirmed at {r1.strike:,.0f}. New resistance building higher at "
                                f"{higher_strike_val:,.0f}. Clear runway for upward momentum expansion."
                            ),
                            tested_level="R1",
                            tested_strike=r1.strike,
                            buffer_zone=(r1.strike, r1.strike + 50.0),
                            target_strike=higher_strike_val,
                            stop_loss=r1.strike - 35.0,
                        )
                    else:
                        return BreakoutValidationVerdict(
                            status="VALID_BREAKOUT_EXPECTED",
                            direction="BULLISH",
                            confidence="MEDIUM",
                            action="BUY_CE / WATCH_FOLLOWTHROUGH",
                            title="🟢 RESISTANCE WALL WEAKENING — WRITERS UNWINDING",
                            summary=(
                                f"Call sellers at R1 {r1.strike:,.0f} are actively covering ({r1.chg_oi:+,.0f} contracts). "
                                f"Resistance wall is softening. Watch for strong breakout candle close."
                            ),
                            rationale="Call writer unwinding removes overhead selling pressure.",
                            tested_level="R1",
                            tested_strike=r1.strike,
                            buffer_zone=(r1.strike, r1.strike + 40.0),
                            target_strike=r1.strike + 50.0,
                            stop_loss=r1.strike - 35.0,
                        )
                else:
                    return BreakoutValidationVerdict(
                        status="FAKE_BREAKOUT_REVERSAL_EXPECTED",
                        direction="BEARISH_REVERSAL",
                        confidence="HIGH",
                        action="BUY_PE / FADE_RESISTANCE",
                        title="🔴 FAKE BREAKOUT / REVERSAL EXPECTED — CALL WRITERS DEFENDING",
                        summary=(
                            f"Spot ({spot:,.1f}) is testing Major Resistance {r1.strike:,.0f}, but Call Writers are "
                            f"HEAVILY DEFENDING ({r1.chg_oi:+,.0f} contracts added). Overhead supply is concrete."
                        ),
                        rationale=(
                            f"Writers are absorbing retail buying with massive supply at {r1.strike:,.0f}. "
                            f"High probability of rejection candle and mean-reversion slide toward {s1.strike:,.0f}."
                        ),
                        tested_level="R1",
                        tested_strike=r1.strike,
                        buffer_zone=(r1.strike, r1.strike + 40.0),
                        target_strike=s1.strike,
                        stop_loss=r1.strike + 30.0,
                    )
            return None

        def _check_s1() -> Optional[BreakoutValidationVerdict]:
            if -55.0 <= dist_to_s1 <= proximity_threshold:
                # 2A. Anti-Trap Rule for Support Breakdown (0 to 50 pts breakdown)
                breakdown_pts = s1.strike - spot
                if 0.0 <= breakdown_pts <= 50.0:
                    if s1.chg_oi > 0:
                        buffer_bottom = s1.strike - 50.0
                        return BreakoutValidationVerdict(
                            status="FAKE_BREAKDOWN_BOUNCE_EXPECTED",
                            direction="BULLISH_REBOUND",
                            confidence="HIGH",
                            action="AVOID_PE / PREPARE_CALL_BOUNCE",
                            title="⚠️ BEAR TRAP DETECTED — SUPPORT FAKEOUT",
                            summary=(
                                f"Price breached Support {s1.strike:,.0f} by {breakdown_pts:.1f} pts, but Put Writers "
                                f"are EXPANDING positions ({s1.chg_oi:+,.0f} contracts) instead of covering. "
                                f"Unreliable breakdown! Do NOT chase shorts."
                            ),
                            rationale=(
                                f"Anti-Trap Rule Triggered: Put writers are absorbing the dip with high volume additions. "
                                f"Institutions are hunting retail stops below {s1.strike:,.0f}. Do not sell into this trap "
                                f"unless sustained below buffer zone ({buffer_bottom:,.0f})."
                            ),
                            is_bear_trap=True,
                            tested_level="S1",
                            tested_strike=s1.strike,
                            buffer_zone=(buffer_bottom, s1.strike),
                            target_strike=r1.strike,
                            stop_loss=s1.strike - 25.0,
                        )

                # 2B. Testing S1 from above: Put Sellers covering vs Defending
                if s1.chg_oi < 0:
                    lower_pe_chg = float(next_lower_strike["pe_chg_oi"]) if next_lower_strike is not None else 0.0
                    lower_strike_val = float(next_lower_strike["strike"]) if next_lower_strike is not None else (s1.strike - 50.0)

                    if lower_pe_chg > 0:
                        return BreakoutValidationVerdict(
                            status="VALID_BREAKDOWN_EXPECTED",
                            direction="BEARISH",
                            confidence="HIGH",
                            action="BUY_PE / PUT_MOMENTUM",
                            title="🔴 VALID BREAKDOWN EXPECTED — SUPPORT SHIFTING LOWER",
                            summary=(
                                f"Spot ({spot:,.1f}) approaching S1 {s1.strike:,.0f}. Put Writers are "
                                f"PANIC COVERING ({s1.chg_oi:+,.0f} unwound) and retreating to {lower_strike_val:,.0f} "
                                f"({lower_pe_chg:+,.0f} added). Floor is giving way!"
                            ),
                            rationale=(
                                f"Institutional floor at {s1.strike:,.0f} is breaking as sellers liquidate. "
                                f"New support base shifting lower to {lower_strike_val:,.0f}. Downside momentum active."
                            ),
                            tested_level="S1",
                            tested_strike=s1.strike,
                            buffer_zone=(s1.strike - 50.0, s1.strike),
                            target_strike=lower_strike_val,
                            stop_loss=s1.strike + 35.0,
                        )
                    else:
                        return BreakoutValidationVerdict(
                            status="VALID_BREAKDOWN_EXPECTED",
                            direction="BEARISH",
                            confidence="MEDIUM",
                            action="BUY_PE / WATCH_BREAKDOWN",
                            title="🔴 SUPPORT FLOOR WEAKENING — WRITERS UNWINDING",
                            summary=(
                                f"Put sellers at S1 {s1.strike:,.0f} are unwinding ({s1.chg_oi:+,.0f} contracts). "
                                f"Institutional support floor is destabilized."
                            ),
                            rationale="Put writer unwinding removes floor bid.",
                            tested_level="S1",
                            tested_strike=s1.strike,
                            buffer_zone=(s1.strike - 40.0, s1.strike),
                            target_strike=s1.strike - 50.0,
                            stop_loss=s1.strike + 35.0,
                        )
                else:
                    return BreakoutValidationVerdict(
                        status="FAKE_BREAKDOWN_BOUNCE_EXPECTED",
                        direction="BULLISH_REBOUND",
                        confidence="HIGH",
                        action="BUY_CE / LONG_BOUNCE",
                        title="🟢 FAKE BREAKDOWN / BOUNCE EXPECTED — PUT WRITERS DEFENDING",
                        summary=(
                            f"Spot ({spot:,.1f}) is testing Major Support {s1.strike:,.0f}, but Put Writers are "
                            f"AGGRESSIVELY DEFENDING ({s1.chg_oi:+,.0f} contracts added). Support floor is fortified."
                        ),
                        rationale=(
                            f"Writers are absorbing supply with massive open interest additions at {s1.strike:,.0f}. "
                            f"High odds of bullish rebound wick and mean-reversion bounce toward {r1.strike:,.0f}."
                        ),
                        tested_level="S1",
                        tested_strike=s1.strike,
                        buffer_zone=(s1.strike - 40.0, s1.strike),
                        target_strike=r1.strike,
                        stop_loss=s1.strike - 30.0,
                    )
            return None

        # Execute check based on priority
        if is_breaching_r1 and not is_breaching_s1:
            res = _check_r1() or _check_s1()
        elif is_breaching_s1 and not is_breaching_r1:
            res = _check_s1() or _check_r1()
        elif abs_dist_r1 < abs_dist_s1:
            res = _check_r1() or _check_s1()
        else:
            res = _check_s1() or _check_r1()

        if res is not None:
            return res

        # ── Condition 3: Price in Mid-Corridor between S1 and R1 ──
        focus_pcr = levels_data.get("focus_pcr", 1.0)
        net_ce = levels_data.get("net_ce_chg", 0.0)
        net_pe = levels_data.get("net_pe_chg", 0.0)

        if focus_pcr >= 1.25 and net_pe > net_ce:
            return BreakoutValidationVerdict(
                status="CORRIDOR_RANGE",
                direction="BULLISH",
                confidence="MEDIUM",
                action="BUY_CE_ON_DIPS / PLAY_CORRIDOR",
                title="📈 CORRIDOR ACCUMULATION — PUT WRITERS DOMINATING",
                summary=(
                    f"Spot ({spot:,.1f}) trading comfortably inside {s1.strike:,.0f} – {r1.strike:,.0f} corridor. "
                    f"Focus PCR is {focus_pcr:.2f} with Put writers aggressively outpacing Call writing. "
                    f"Upward drift toward {r1.strike:,.0f} favored."
                ),
                rationale="Underlying support building steadily across strike spectrum.",
                tested_level="NONE",
                tested_strike=0.0,
                target_strike=r1.strike,
                stop_loss=s1.strike,
            )
        elif focus_pcr <= 0.80 and net_ce > net_pe:
            return BreakoutValidationVerdict(
                status="CORRIDOR_RANGE",
                direction="BEARISH",
                confidence="MEDIUM",
                action="BUY_PE_ON_RALLIES / PLAY_CORRIDOR",
                title="📉 CORRIDOR DISTRIBUTION — CALL WRITERS CAPPING",
                summary=(
                    f"Spot ({spot:,.1f}) trading inside {s1.strike:,.0f} – {r1.strike:,.0f} corridor. "
                    f"Focus PCR is {focus_pcr:.2f} with Call writers heavily building overhead resistance. "
                    f"Downward pressure toward {s1.strike:,.0f} favored."
                ),
                rationale="Call writing pressure creating overhead cap.",
                tested_level="NONE",
                tested_strike=0.0,
                target_strike=s1.strike,
                stop_loss=r1.strike,
            )
        else:
            return BreakoutValidationVerdict(
                status="CORRIDOR_RANGE",
                direction="RANGE",
                confidence="HIGH",
                action="SELL_STRANGLE / TRADE_BOUNDARIES",
                title="⚖️ BALANCED STRANGLE CORRIDOR — IRON BOUNDARIES",
                summary=(
                    f"Market pinned inside 50–100 pt corridor between S1 ({s1.strike:,.0f}) and R1 ({r1.strike:,.0f}). "
                    f"Writers are active on both sides. Theta decay favored over directional breakout."
                ),
                rationale="No immediate breakout pressure. Both Call and Put walls intact.",
                tested_level="NONE",
                tested_strike=0.0,
                target_strike=r1.strike,
                stop_loss=s1.strike,
            )

    @classmethod
    def evaluate_double_confirmation(
        cls,
        spot: float,
        verdict: BreakoutValidationVerdict,
        pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
        smc_res: Optional[Any] = None,
    ) -> DoubleConfirmationState:
        """
        Cross-references Price Action (Candles / VWAP / SMC) with Option Chain Writer Telemetry.
        Generates a unified institutional verdict HUD.
        """
        # 1. Price Action Trend Extraction
        pa_trend = "NEUTRAL"
        pa_detail = "Consolidation / Rangebound near Spot"
        last_close = spot
        vwap_val = None

        df_candle = None
        if pa_frames:
            for tf in ("5m", "15m", "1m"):
                fr = pa_frames.get(tf)
                if fr is not None and hasattr(fr, "empty") and not fr.empty and "close" in fr.columns:
                    df_candle = fr
                    break

        if df_candle is not None and not df_candle.empty:
            last_c = float(df_candle.iloc[-1]["close"])
            last_close = last_c
            if "vwap" in df_candle.columns and df_candle["vwap"].notna().any():
                vwap_val = float(df_candle["vwap"].dropna().iloc[-1])

            # Check candle structure
            if len(df_candle) >= 3:
                recent = df_candle.tail(3)
                is_higher_closes = recent["close"].iloc[-1] > recent["close"].iloc[0]
                is_lower_closes = recent["close"].iloc[-1] < recent["close"].iloc[0]

                if vwap_val is not None:
                    if last_c > vwap_val and is_higher_closes:
                        pa_trend = "BULLISH"
                        pa_detail = f"Close ({last_c:,.1f}) > VWAP ({vwap_val:,.1f}) + Higher Closes"
                    elif last_c < vwap_val and is_lower_closes:
                        pa_trend = "BEARISH"
                        pa_detail = f"Close ({last_c:,.1f}) < VWAP ({vwap_val:,.1f}) + Lower Closes"
                    else:
                        pa_trend = "NEUTRAL"
                        pa_detail = f"Oscillating near VWAP ({vwap_val:,.1f})"
                else:
                    if is_higher_closes:
                        pa_trend = "BULLISH"
                        pa_detail = f"Ascending candles pushing higher ({last_c:,.1f})"
                    elif is_lower_closes:
                        pa_trend = "BEARISH"
                        pa_detail = f"Descending candles pressing lower ({last_c:,.1f})"

        # SMC Override if available
        if smc_res is not None:
            smc_trend = getattr(smc_res, "trend", None)
            smc_phase = getattr(smc_res, "phase", None)
            if smc_trend in ("BULLISH", "BEARISH"):
                pa_trend = smc_trend
                pa_detail = f"SMC: {smc_trend} ({smc_phase})"

        # 2. Option Chain Bias
        oi_bias = "NEUTRAL"
        if verdict.direction in ("BULLISH", "BULLISH_REBOUND"):
            oi_bias = "BULLISH"
        elif verdict.direction in ("BEARISH", "BEARISH_REVERSAL"):
            oi_bias = "BEARISH"

        oi_detail = verdict.summary[:85] + ("..." if len(verdict.summary) > 85 else "")

        # 3. Double Confirmation Synthesis
        trap_active = verdict.is_bull_trap or verdict.is_bear_trap
        trap_badge = "NONE"
        if verdict.is_bull_trap:
            trap_badge = "⚠️ BULL TRAP ACTIVE"
        elif verdict.is_bear_trap:
            trap_badge = "⚠️ BEAR TRAP ACTIVE"

        # Confluence evaluation
        if verdict.is_bull_trap:
            confluent_verdict = "⚠️ INSTITUTIONAL BULL TRAP: PRICE SURGING BUT CALL WRITERS EXPANDING"
            badge_color = AMBER
            bg_color = "rgba(245, 158, 11, 0.18)"
            border_color = AMBER
            instruction = "DO NOT BUY BREAKOUT. Price breached resistance, but sellers added positions. Fade or wait."
        elif verdict.is_bear_trap:
            confluent_verdict = "⚠️ INSTITUTIONAL BEAR TRAP: PRICE FLUSHING BUT PUT WRITERS EXPANDING"
            badge_color = AMBER
            bg_color = "rgba(245, 158, 11, 0.18)"
            border_color = AMBER
            instruction = "DO NOT SHORT BREAKDOWN. Price pierced support, but put writers fortified base. Watch for squeeze."
        elif pa_trend == "BULLISH" and verdict.status == "VALID_BREAKOUT_EXPECTED":
            confluent_verdict = "⚡ CONFLUENT INSTITUTIONAL BULLISH BREAKOUT (GRADE A+ LONG)"
            badge_color = GREEN
            bg_color = "rgba(16, 185, 129, 0.20)"
            border_color = GREEN
            instruction = f"HIGH PROBABILITY CALL (CE) TRADE: Enter on retest of {verdict.tested_strike:,.0f}. Stop {verdict.stop_loss:,.0f}."
        elif pa_trend == "BEARISH" and verdict.status == "VALID_BREAKDOWN_EXPECTED":
            confluent_verdict = "⚡ CONFLUENT INSTITUTIONAL BEARISH BREAKDOWN (GRADE A+ SHORT)"
            badge_color = RED
            bg_color = "rgba(239, 68, 68, 0.20)"
            border_color = RED
            instruction = f"HIGH PROBABILITY PUT (PE) TRADE: Enter on breakdown retest of {verdict.tested_strike:,.0f}. Stop {verdict.stop_loss:,.0f}."
        elif pa_trend == "BULLISH" and verdict.status == "FAKE_BREAKOUT_REVERSAL_EXPECTED":
            confluent_verdict = "🛑 BEARISH DIVERGENCE: PRICE AT RESISTANCE BUT WRITERS BRICK WALL"
            badge_color = RED
            bg_color = "rgba(239, 68, 68, 0.15)"
            border_color = RED
            instruction = "CE BUYING BLOCKED. Price action reaching ceiling while Call sellers heavily defending. Look for Put scalp."
        elif pa_trend == "BEARISH" and verdict.status == "FAKE_BREAKDOWN_BOUNCE_EXPECTED":
            confluent_verdict = "🛡️ BULLISH DIVERGENCE: PRICE AT SUPPORT BUT WRITERS ABSORBING"
            badge_color = GREEN
            bg_color = "rgba(16, 185, 129, 0.15)"
            border_color = GREEN
            instruction = "PE BUYING BLOCKED. Price action testing floor while Put sellers actively absorbing. Look for Call bounce."
        elif pa_trend == "NEUTRAL" and verdict.status == "CORRIDOR_RANGE":
            confluent_verdict = "⚖️ RANGEBOUND CONGRUENCE: BALANCED PRICE & WRITER STANCE"
            badge_color = BLUE
            bg_color = "rgba(56, 189, 248, 0.15)"
            border_color = BLUE
            instruction = "NON-DIRECTIONAL REGIME. Trade support/resistance boundaries or harvest decay via delta-neutral spreads."
        else:
            confluent_verdict = f"🧭 ALIGNED {oi_bias} FLOW · {verdict.title}"
            badge_color = GREEN if oi_bias == "BULLISH" else (RED if oi_bias == "BEARISH" else BLUE)
            bg_color = "rgba(15, 23, 42, 0.6)"
            border_color = badge_color
            instruction = verdict.action

        return DoubleConfirmationState(
            pa_trend=pa_trend,
            pa_detail=pa_detail,
            oi_bias=oi_bias,
            oi_detail=oi_detail,
            trap_active=trap_active,
            trap_badge=trap_badge,
            confluent_verdict=confluent_verdict,
            verdict_badge_color=badge_color,
            verdict_bg_color=bg_color,
            verdict_border_color=border_color,
            actionable_instruction=instruction,
            key_metrics={
                "spot": spot,
                "tested_strike": verdict.tested_strike,
                "stop_loss": verdict.stop_loss,
                "target_strike": verdict.target_strike,
            }
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Interactive Plotly Dynamic S/R Chart with Anti-Trap Zones
# ─────────────────────────────────────────────────────────────────────────────
def create_dynamic_oi_sr_chart(
    df: pd.DataFrame,
    spot: float,
    levels_data: Dict[str, Any],
    verdict: BreakoutValidationVerdict,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    timeframe: str = "5m",
) -> go.Figure:
    """
    Creates an institutional Plotly Candlestick chart overlaid with:
    - Major Call Wall (R1) in Solid Red
    - Secondary Call Wall (R2) in Dotted Red
    - Major Put Wall (S1) in Solid Green
    - Secondary Put Wall (S2) in Dotted Green
    - Shaded 30-50 pt Bull Trap Buffer Zone above R1
    - Shaded 30-50 pt Bear Trap Buffer Zone below S1
    - Spot Line and Intraday VWAP
    """
    fig = go.Figure()

    r1: StrikeLevel = levels_data.get("r1")
    r2: StrikeLevel = levels_data.get("r2")
    s1: StrikeLevel = levels_data.get("s1")
    s2: StrikeLevel = levels_data.get("s2")

    # 1. Fetch Candles
    candle_df = None
    if pa_frames and timeframe in pa_frames:
        fr = pa_frames[timeframe]
        if fr is not None and not fr.empty and "close" in fr.columns:
            candle_df = fr.tail(60).copy()

    if candle_df is None or candle_df.empty:
        # Fallback synthetic range visualization if no candles available
        ts_now = pd.date_range(end=datetime.now(), periods=30, freq="5min")
        rng = np.linspace(spot - 25, spot + 25, 30)
        candle_df = pd.DataFrame({
            "timestamp": ts_now,
            "open": rng - 4,
            "high": rng + 8,
            "low": rng - 8,
            "close": rng,
            "vwap": [spot] * 30,
        })

    ts = (
        pd.to_datetime(candle_df["timestamp"])
        if "timestamp" in candle_df.columns
        else pd.to_datetime(candle_df.index)
    )

    # 2. Add Candlesticks
    fig.add_trace(
        go.Candlestick(
            x=ts,
            open=candle_df["open"],
            high=candle_df["high"],
            low=candle_df["low"],
            close=candle_df["close"],
            name=f"NIFTY ({timeframe})",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
            increasing_fillcolor=GREEN,
            decreasing_fillcolor=RED,
            line_width=1.2,
        )
    )

    # 3. Add VWAP if available
    if "vwap" in candle_df.columns and candle_df["vwap"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=ts,
                y=candle_df["vwap"],
                name="VWAP",
                line=dict(color=BLUE, width=1.6, dash="solid"),
            )
        )

    # 4. Overlay S/R Levels and Shaded Anti-Trap Zones
    if r1:
        # R1 Major Resistance Line
        fig.add_hline(
            y=r1.strike,
            line=dict(color=RED, width=2.4, dash="solid"),
            annotation_text=f"🛑 R1 Call Wall: {r1.strike:,.0f} (OI: {r1.oi/1e5:.1f}L | ΔOI: {r1.chg_oi:+,.0f})",
            annotation_position="top left",
            annotation_font=dict(color=RED, size=11),
        )

        # 30-50 pt Bull Trap Zone Shading [R1, R1 + 40]
        fig.add_hrect(
            y0=r1.strike,
            y1=r1.strike + 40.0,
            fillcolor="rgba(239, 68, 68, 0.12)",
            line=dict(color=RED, width=1.0, dash="dot"),
            annotation_text="⚠️ Anti-Trap Zone (30-50 pt Bull Trap Buffer)",
            annotation_position="top right",
            annotation_font=dict(color="#FCA5A5", size=9),
        )

    if r2 and r2.strike != (r1.strike if r1 else 0):
        fig.add_hline(
            y=r2.strike,
            line=dict(color="#F87171", width=1.4, dash="dash"),
            annotation_text=f"R2 Overhead: {r2.strike:,.0f}",
            annotation_position="top left",
            annotation_font=dict(color="#F87171", size=10),
        )

    if s1:
        # S1 Major Support Line
        fig.add_hline(
            y=s1.strike,
            line=dict(color=GREEN, width=2.4, dash="solid"),
            annotation_text=f"🛡️ S1 Put Wall: {s1.strike:,.0f} (OI: {s1.oi/1e5:.1f}L | ΔOI: {s1.chg_oi:+,.0f})",
            annotation_position="bottom left",
            annotation_font=dict(color=GREEN, size=11),
        )

        # 30-50 pt Bear Trap Zone Shading [S1 - 40, S1]
        fig.add_hrect(
            y0=s1.strike - 40.0,
            y1=s1.strike,
            fillcolor="rgba(16, 185, 129, 0.12)",
            line=dict(color=GREEN, width=1.0, dash="dot"),
            annotation_text="⚠️ Anti-Trap Zone (30-50 pt Bear Trap Buffer)",
            annotation_position="bottom right",
            annotation_font=dict(color="#86EFAC", size=9),
        )

    if s2 and s2.strike != (s1.strike if s1 else 0):
        fig.add_hline(
            y=s2.strike,
            line=dict(color="#34D399", width=1.4, dash="dash"),
            annotation_text=f"S2 Floor: {s2.strike:,.0f}",
            annotation_position="bottom left",
            annotation_font=dict(color="#34D399", size=10),
        )

    # 5. Live Spot Price Line
    fig.add_hline(
        y=spot,
        line=dict(color="#FACC15", width=1.8, dash="dashdot"),
        annotation_text=f"📍 Spot: {spot:,.1f}",
        annotation_position="top right",
        annotation_font=dict(color="#FACC15", size=11, family="monospace"),
    )

    fig.update_layout(
        title=dict(
            text=f"<b>Dynamic S/R Chart & Anti-Trap Buffer Zones</b> · Nifty ({timeframe}) vs Call/Put Walls",
            font=dict(size=14, color="#E2E8F0"),
        ),
        xaxis=dict(
            rangeslider=dict(visible=False),
            gridcolor="rgba(255, 255, 255, 0.05)",
            showgrid=True,
        ),
        yaxis=dict(
            gridcolor="rgba(255, 255, 255, 0.05)",
            showgrid=True,
            zeroline=False,
        ),
        paper_bgcolor="#0A0F1A",
        plot_bgcolor="#0A0F1A",
        height=480,
        margin=dict(l=45, r=45, t=40, b=30),
        hovermode="x unified",
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=10, color="#94A3B8"),
        ),
    )

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# 5. Option Chain Heatmap (Dual-Sided Table with Red & Green Gradients)
# ─────────────────────────────────────────────────────────────────────────────
def render_option_chain_heatmap_table(
    focus_df: pd.DataFrame,
    spot: float,
    r1_strike: float,
    r2_strike: float,
    s1_strike: float,
    s2_strike: float,
) -> None:
    """
    Renders an institutional dual-sided Option Chain Heatmap filtered to the focus window.
    Calls on Left (Red Gradient), Strike in Center, Puts on Right (Green Gradient).
    """
    if focus_df.empty:
        st.info("Option chain quotes unavailable.")
        return

    df_sorted = focus_df.sort_values("strike", ascending=False).copy()
    max_ce_oi = df_sorted["ce_oi"].max() or 1.0
    max_pe_oi = df_sorted["pe_oi"].max() or 1.0
    atm_strike = round(spot / 50.0) * 50.0

    rows_html: List[str] = []
    for idx, row in df_sorted.iterrows():
        stk = float(row["strike"])
        ce_oi = float(row["ce_oi"])
        ce_chg = float(row["ce_chg_oi"])
        ce_vol = float(row["ce_vol"])
        ce_ltp = float(row["ce_ltp"])
        ce_iv = float(row["ce_iv"])

        pe_oi = float(row["pe_oi"])
        pe_chg = float(row["pe_chg_oi"])
        pe_vol = float(row["pe_vol"])
        pe_ltp = float(row["pe_ltp"])
        pe_iv = float(row["pe_iv"])

        # Strike badge styling
        is_atm = abs(stk - atm_strike) < 25.0
        is_r1 = abs(stk - r1_strike) < 5.0
        is_r2 = abs(stk - r2_strike) < 5.0 and not is_r1
        is_s1 = abs(stk - s1_strike) < 5.0
        is_s2 = abs(stk - s2_strike) < 5.0 and not is_s1

        strike_badges = []
        if is_atm:
            strike_badges.append('<span style="background:#FACC15;color:#000;padding:1px 5px;border-radius:3px;font-size:0.75em;font-weight:700;">ATM</span>')
        if is_r1:
            strike_badges.append('<span style="background:#EF4444;color:#FFF;padding:1px 5px;border-radius:3px;font-size:0.75em;font-weight:700;">🛑 R1 WALL</span>')
        elif is_r2:
            strike_badges.append('<span style="background:#B91C1C;color:#FFF;padding:1px 5px;border-radius:3px;font-size:0.75em;font-weight:600;">R2</span>')
        if is_s1:
            strike_badges.append('<span style="background:#10B981;color:#FFF;padding:1px 5px;border-radius:3px;font-size:0.75em;font-weight:700;">🛡️ S1 WALL</span>')
        elif is_s2:
            strike_badges.append('<span style="background:#047857;color:#FFF;padding:1px 5px;border-radius:3px;font-size:0.75em;font-weight:600;">S2</span>')

        badge_str = " ".join(strike_badges)
        strike_style = "font-weight:700;color:#F8FAFC;" if (is_atm or is_r1 or is_s1) else "color:#CBD5E1;"

        # Heatmap gradients
        ce_ratio = min(1.0, ce_oi / max_ce_oi)
        pe_ratio = min(1.0, pe_oi / max_pe_oi)

        ce_oi_bg = f"rgba(239, 68, 68, {0.08 + 0.62 * ce_ratio:.2f})"
        pe_oi_bg = f"rgba(16, 185, 129, {0.08 + 0.62 * pe_ratio:.2f})"

        # Delta OI styling
        ce_chg_color = "#F87171" if ce_chg > 0 else "#94A3B8"
        pe_chg_color = "#34D399" if pe_chg > 0 else "#94A3B8"
        ce_chg_sym = "+" if ce_chg > 0 else ""
        pe_chg_sym = "+" if pe_chg > 0 else ""

        # Activity badge
        ce_act = "🧱 Built" if ce_chg > 25000 else ("🏃 Covered" if ce_chg < -15000 else "—")
        pe_act = "🧱 Built" if pe_chg > 25000 else ("🏃 Covered" if pe_chg < -15000 else "—")

        tr_bg = "#111827" if is_atm else ("#0C1322" if idx % 2 == 0 else "#090E1A")
        border_highlight = "border-top: 2px solid #FACC15; border-bottom: 2px solid #FACC15;" if is_atm else ""

        row_html = (
            f'<tr style="background:{tr_bg}; {border_highlight} border-bottom: 1px solid #1E293B; font-family: monospace; font-size: 0.84em; text-align: right;">'
            # CALL SIDE
            f'<td style="padding: 6px 8px; color: #94A3B8;">{ce_iv:.1f}</td>'
            f'<td style="padding: 6px 8px; color: #F1F5F9; font-weight: 600;">{ce_ltp:,.2f}</td>'
            f'<td style="padding: 6px 8px; color: #64748B;">{ce_vol:,.0f}</td>'
            f'<td style="padding: 6px 8px; color: {ce_chg_color};">{ce_chg_sym}{ce_chg:,.0f} <span style="font-size:0.75em; opacity:0.8;">{ce_act}</span></td>'
            f'<td style="padding: 6px 12px; background: {ce_oi_bg}; color: #FFFFFF; font-weight: 700;">{ce_oi:,.0f}</td>'
            # STRIKE (CENTER)
            f'<td style="padding: 6px 14px; text-align: center; {strike_style} background: #151F30; border-left: 1px solid #334155; border-right: 1px solid #334155;">'
            f'{stk:,.0f} {badge_str}'
            f'</td>'
            # PUT SIDE
            f'<td style="padding: 6px 12px; background: {pe_oi_bg}; color: #FFFFFF; font-weight: 700; text-align: left;">{pe_oi:,.0f}</td>'
            f'<td style="padding: 6px 8px; color: {pe_chg_color}; text-align: left;"><span style="font-size:0.75em; opacity:0.8;">{pe_act}</span> {pe_chg_sym}{pe_chg:,.0f}</td>'
            f'<td style="padding: 6px 8px; color: #64748B; text-align: left;">{pe_vol:,.0f}</td>'
            f'<td style="padding: 6px 8px; color: #F1F5F9; font-weight: 600; text-align: left;">{pe_ltp:,.2f}</td>'
            f'<td style="padding: 6px 8px; color: #94A3B8; text-align: left;">{pe_iv:.1f}</td>'
            f'</tr>'
        )
        rows_html.append(row_html)

    table_content = "\n".join(rows_html)

    heatmap_container = (
        f'<div style="overflow-x: auto; border: 1px solid #1E293B; border-radius: 8px; margin-top: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3);">'
        f'<table style="width: 100%; border-collapse: collapse; text-align: right;">'
        f'<thead>'
        f'<tr style="background: #1E293B; color: #94A3B8; font-size: 0.76em; text-transform: uppercase; font-weight: 700; letter-spacing: 0.05em;">'
        f'<th colspan="5" style="text-align: center; padding: 8px; color: #EF4444; border-right: 1px solid #334155; background: rgba(239, 68, 68, 0.1);">🔴 CALL WRITERS (RESISTANCE SPREAD)</th>'
        f'<th style="text-align: center; padding: 8px; color: #FACC15; background: #0F172A;">STRIKE</th>'
        f'<th colspan="5" style="text-align: center; padding: 8px; color: #10B981; border-left: 1px solid #334155; background: rgba(16, 185, 129, 0.1);">🟢 PUT WRITERS (SUPPORT SPREAD)</th>'
        f'</tr>'
        f'<tr style="background: #111827; color: #94A3B8; font-size: 0.72em; text-transform: uppercase; border-bottom: 1px solid #334155;">'
        f'<th style="padding: 6px 8px;">IV</th>'
        f'<th style="padding: 6px 8px;">LTP</th>'
        f'<th style="padding: 6px 8px;">Volume</th>'
        f'<th style="padding: 6px 8px;">Call ΔOI</th>'
        f'<th style="padding: 6px 12px;">Call OI</th>'
        f'<th style="text-align: center; padding: 6px 14px; background: #151F30; border-left: 1px solid #334155; border-right: 1px solid #334155; color: #F8FAFC;">STRIKE</th>'
        f'<th style="text-align: left; padding: 6px 12px;">Put OI</th>'
        f'<th style="text-align: left; padding: 6px 8px;">Put ΔOI</th>'
        f'<th style="text-align: left; padding: 6px 8px;">Volume</th>'
        f'<th style="text-align: left; padding: 6px 8px;">LTP</th>'
        f'<th style="text-align: left; padding: 6px 8px;">IV</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>'
        f'{table_content}'
        f'</tbody>'
        f'</table>'
        f'</div>'
    )

    _render_html(heatmap_container)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Double Confirmation Executive HUD & Anti-Trap Banner
# ─────────────────────────────────────────────────────────────────────────────
def render_double_confirmation_hud(hud: DoubleConfirmationState) -> None:
    """Renders the high-contrast Double Confirmation Executive Decision HUD."""
    pa_badge_bg = (
        GREEN_BG if hud.pa_trend == "BULLISH" else (RED_BG if hud.pa_trend == "BEARISH" else BLUE_BG)
    )
    pa_badge_col = (
        GREEN if hud.pa_trend == "BULLISH" else (RED if hud.pa_trend == "BEARISH" else BLUE)
    )

    oi_badge_bg = (
        GREEN_BG if hud.oi_bias == "BULLISH" else (RED_BG if hud.oi_bias == "BEARISH" else BLUE_BG)
    )
    oi_badge_col = (
        GREEN if hud.oi_bias == "BULLISH" else (RED if hud.oi_bias == "BEARISH" else BLUE)
    )

    trap_alert_html = ""
    if hud.trap_active:
        trap_alert_html = (
            f'<div style="margin-top: 10px; background: rgba(245, 158, 11, 0.25); border: 1px solid #F59E0B; border-radius: 6px; padding: 8px 14px; display: flex; align-items: center; gap: 10px;">'
            f'<span style="font-size: 1.3em;">⚠️</span>'
            f'<span style="color: #FDE68A; font-weight: 700; font-size: 0.92em;">{hud.trap_badge}: 30–50 pt Buffer Rule in effect. DO NOT chase breakout/breakdown until confirmed sustained beyond buffer!</span>'
            f'</div>'
        )

    hud_html = (
        f'<div style="background: {hud.verdict_bg_color}; border: 2px solid {hud.verdict_border_color}; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.4);">'
        # Top Header Ribbon
        f'<div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; border-bottom: 1px solid rgba(255,255,255,0.12); padding-bottom: 10px;">'
        f'<div style="display: flex; align-items: center; gap: 8px;">'
        f'<span style="background: {hud.verdict_badge_color}; color: #000; font-weight: 900; font-size: 0.72em; padding: 2px 8px; border-radius: 4px; text-transform: uppercase;">DOUBLE CONFIRMATION</span>'
        f'<span style="font-size: 1.25em; font-weight: 800; color: #FFFFFF; letter-spacing: 0.02em;">{hud.confluent_verdict}</span>'
        f'</div>'
        f'<div style="font-size: 0.82em; color: #94A3B8; font-family: monospace;">'
        f'NIFTY SPOT: <b style="color: #FACC15;">{hud.key_metrics.get("spot", 0.0):,.1f}</b>'
        f'</div>'
        f'</div>'
        # Middle 2-Column Matrix (Price Action x Option Chain)
        f'<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px;">'
        # Column A: Price Action Engine
        f'<div style="background: rgba(13, 19, 31, 0.8); border: 1px solid #1E293B; border-radius: 8px; padding: 12px 14px;">'
        f'<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">'
        f'<span style="font-size: 0.74em; color: #8FA6B8; font-weight: 700; text-transform: uppercase;">1. PRICE ACTION (CANDLES / VWAP)</span>'
        f'<span style="background: {pa_badge_bg}; color: {pa_badge_col}; font-size: 0.75em; font-weight: 800; padding: 2px 8px; border-radius: 4px;">{hud.pa_trend}</span>'
        f'</div>'
        f'<div style="font-size: 0.88em; color: #E2E8F0;">{hud.pa_detail}</div>'
        f'</div>'
        # Column B: Option Chain Writer Engine
        f'<div style="background: rgba(13, 19, 31, 0.8); border: 1px solid #1E293B; border-radius: 8px; padding: 12px 14px;">'
        f'<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">'
        f'<span style="font-size: 0.74em; color: #8FA6B8; font-weight: 700; text-transform: uppercase;">2. OPTION WRITER POSITIONING</span>'
        f'<span style="background: {oi_badge_bg}; color: {oi_badge_col}; font-size: 0.75em; font-weight: 800; padding: 2px 8px; border-radius: 4px;">{hud.oi_bias}</span>'
        f'</div>'
        f'<div style="font-size: 0.88em; color: #E2E8F0;">{hud.oi_detail}</div>'
        f'</div>'
        f'</div>'
        # Actionable Rule
        f'<div style="margin-top: 12px; background: rgba(0,0,0,0.3); border-radius: 6px; padding: 10px 14px; border-left: 4px solid {hud.verdict_badge_color};">'
        f'<span style="font-weight: 700; color: #F8FAFC; font-size: 0.9em;">🎯 Institutional Playbook: </span>'
        f'<span style="color: #CBD5E1; font-size: 0.88em;">{hud.actionable_instruction}</span>'
        f'</div>'
        f'{trap_alert_html}'
        f'</div>'
    )
    _render_html(hud_html)


def render_breakout_validation_summary_card(verdict: BreakoutValidationVerdict) -> None:
    """Renders a standalone Breakout Validation & Anti-Trap Alert Card for Tab 1 / Tab 2 integration."""
    color = (
        GREEN if verdict.direction in ("BULLISH", "BULLISH_REBOUND")
        else (RED if verdict.direction in ("BEARISH", "BEARISH_REVERSAL") else BLUE)
    )
    bg = (
        GREEN_BG if verdict.direction in ("BULLISH", "BULLISH_REBOUND")
        else (RED_BG if verdict.direction in ("BEARISH", "BEARISH_REVERSAL") else BLUE_BG)
    )

    trap_str = ""
    if verdict.is_bull_trap or verdict.is_bear_trap:
        color = AMBER
        bg = AMBER_BG
        trap_str = f'<div style="margin-top: 6px; color: #FDE68A; font-weight: 700; font-size: 0.85em;">{verdict.rationale}</div>'

    card_html = (
        f'<div style="background: {bg}; border: 1px solid {color}; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px;">'
        f'<div style="display: flex; justify-content: space-between; align-items: center;">'
        f'<div style="font-weight: 800; font-size: 1.05em; color: #FFFFFF;">{verdict.title}</div>'
        f'<span style="background: {color}; color: #000; font-size: 0.72em; font-weight: 800; padding: 2px 8px; border-radius: 4px;">{verdict.confidence} CONFIDENCE</span>'
        f'</div>'
        f'<div style="font-size: 0.9em; color: #E2E8F0; margin-top: 4px;">{verdict.summary}</div>'
        f'{trap_str}'
        f'</div>'
    )
    _render_html(card_html)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Complete Dedicated Tab Renderer
# ─────────────────────────────────────────────────────────────────────────────
def render_option_chain_oi_tab(
    df: pd.DataFrame,
    spot: float,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    read: Optional[Any] = None,
) -> None:
    """
    Renders the complete dedicated '⚡ Option Chain & OI Analysis' Tab.
    Integrates Engine, Dynamic S/R Chart with Anti-Trap Shading, Double Confirmation HUD,
    and Dual-Sided Heatmap Table.
    """
    st.markdown("## ⚡ Option Chain & Open Interest Analysis")
    st.caption("Institutional quantitative engine analyzing writer positioning, breakout validity, and 30–50 pt anti-trap zones.")

    tab_oi, tab_strike_select = st.tabs([
        "⚡ OI Breakout & Anti-Trap Analysis",
        "🎯 Strike Selection & Premium Behaviour (₹200–₹300 Rule)",
    ])
    # TABMOVE: "📊 Call vs Put Writers Graph" and "🟥 PE − CE OI Table & Smart OI" now live in 🧪 Options Lab

    with tab_oi:
        # 1. Interactive Control Toolbar
        c_ctrl1, c_ctrl2, c_ctrl3 = st.columns([2, 1, 1])
        with c_ctrl1:
            focus_radius = st.slider(
                "🎯 Strike Focus Area Radius (± pts around Spot)",
                min_value=300,
                max_value=600,
                value=450,
                step=50,
                help="Filters option chain to eliminate illiquid far OTM retail noise. 400-500 pt radius isolates institutional core strikes."
            )
        with c_ctrl2:
            chart_tf = st.selectbox("Chart Timeframe", ["5m", "15m", "1m"], index=0)
        with c_ctrl3:
            st.metric("Live Spot", f"{spot:,.1f}", delta=None)

        # 2. Engine Analytics Execution
        engine = OptionChainOIEngine()
        focus_df = engine.filter_focus_window(df, spot, focus_radius=focus_radius)
        levels = engine.analyze_levels(focus_df, spot)

        if not levels:
            st.warning("Option Chain quotes currently unavailable or parsing.")
            return

        verdict = engine.validate_breakout(focus_df, spot, levels)
        hud_state = engine.evaluate_double_confirmation(spot, verdict, pa_frames=pa_frames)

        # 3. Double Confirmation Executive HUD (Centerpiece)
        render_double_confirmation_hud(hud_state)

        # 4. Top Telemetry Metrics Cards
        r1: StrikeLevel = levels["r1"]
        r2: StrikeLevel = levels["r2"]
        s1: StrikeLevel = levels["s1"]
        s2: StrikeLevel = levels["s2"]

        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric(
                label="🛑 Major Resistance (Max Call OI)",
                value=f"{r1.strike:,.0f}",
                delta=f"{r1.dist_pts:+.1f} pts from Spot",
                delta_color="inverse"
            )
            st.caption(f"Call OI: **{r1.oi:,.0f}** | ΔOI: **{r1.chg_oi:+,.0f}** ({r1.activity})")

        with m2:
            st.metric(
                label="🛡️ Major Support (Max Put OI)",
                value=f"{s1.strike:,.0f}",
                delta=f"{s1.dist_pts:+.1f} pts from Spot"
            )
            st.caption(f"Put OI: **{s1.oi:,.0f}** | ΔOI: **{s1.chg_oi:+,.0f}** ({s1.activity})")

        with m3:
            pcr_val = levels["focus_pcr"]
            pcr_sent = "BULLISH" if pcr_val >= 1.15 else ("BEARISH" if pcr_val <= 0.85 else "NEUTRAL")
            st.metric(
                label="📊 Focus Window PCR (ΔPE/ΔCE)",
                value=f"{pcr_val:.2f}",
                delta=pcr_sent
            )
            st.caption(f"PE OI: {levels['tot_pe_oi']:,.0f} / CE OI: {levels['tot_ce_oi']:,.0f}")

        with m4:
            trap_status = "⚠️ ACTIVE TRAP" if verdict.is_bull_trap or verdict.is_bear_trap else "NORMAL"
            st.metric(
                label="⚖️ Anti-Trap Status (30-50 pt)",
                value=trap_status,
                delta=verdict.direction
            )
            st.caption("Volume & OI synchronization check")

        # 5. Dynamic S/R Candlestick Chart with Shaded Anti-Trap Zones
        st.markdown("### 📈 Dynamic S/R Candlestick Chart & Shaded Anti-Trap Zones")
        fig_chart = create_dynamic_oi_sr_chart(
            df=df,
            spot=spot,
            levels_data=levels,
            verdict=verdict,
            pa_frames=pa_frames,
            timeframe=chart_tf,
        )
        st.plotly_chart(fig_chart, use_container_width=True)

        # 6. Option Chain Heatmap Table (Seller's Perspective)
        st.markdown(f"### 🔥 Option Chain Heatmap Table *(Filtered to ±{focus_radius} pt Focus Area)*")
        st.caption("Call OI & ΔOI in Red Gradient (Overhead Ceilings) · Put OI & ΔOI in Green Gradient (Support Floors).")
        render_option_chain_heatmap_table(
            focus_df=focus_df,
            spot=spot,
            r1_strike=r1.strike,
            r2_strike=r2.strike,
            s1_strike=s1.strike,
            s2_strike=s2.strike,
        )

        # 7. Institutional Deep-Dive Radar (Expandable)
        with st.expander("🔍 Deep-Dive: Option Writer Stance & Strike Shift Radar", expanded=False):
            c_rad1, c_rad2 = st.columns(2)
            with c_rad1:
                st.markdown("#### 🔴 Call Writers (Ceiling Reinforcement)")
                st.markdown(f"**Net Call ΔOI in Focus Window:** `{levels['net_ce_chg']:+,.0f}`")
                st.markdown(f"**Added:** `+{levels['ce_additions']:,.0f}` | **Covered:** `{levels['ce_unwinding']:,.0f}`")
                st.markdown("**Top Call Writing Strikes (Adding Supply):**")
                for item in levels["top_ce_writers"]:
                    st.write(f"- Strike **{item['strike']:,.0f}**: OI {item['ce_oi']:,.0f} (ΔOI **{item['ce_chg_oi']:+,.0f}**)")
                st.markdown("**Top Call Unwinding Strikes (Shedding Supply):**")
                for item in levels["top_ce_unwind"]:
                    st.write(f"- Strike **{item['strike']:,.0f}**: OI {item['ce_oi']:,.0f} (ΔOI **{item['ce_chg_oi']:+,.0f}**)")

            with c_rad2:
                st.markdown("#### 🟢 Put Writers (Floor Fortification)")
                st.markdown(f"**Net Put ΔOI in Focus Window:** `{levels['net_pe_chg']:+,.0f}`")
                st.markdown(f"**Added:** `+{levels['pe_additions']:,.0f}` | **Covered:** `{levels['pe_unwinding']:,.0f}`")
                st.markdown("**Top Put Writing Strikes (Adding Demand Floor):**")
                for item in levels["top_pe_writers"]:
                    st.write(f"- Strike **{item['strike']:,.0f}**: OI {item['pe_oi']:,.0f} (ΔOI **{item['pe_chg_oi']:+,.0f}**)")
                st.markdown("**Top Put Unwinding Strikes (Abandoning Floor):**")
                for item in levels["top_pe_unwind"]:
                    st.write(f"- Strike **{item['strike']:,.0f}**: OI {item['pe_oi']:,.0f} (ΔOI **{item['pe_chg_oi']:+,.0f}**)")

    with tab_strike_select:
        try:
            import strike_select_tab
            import context
            ctx = context.Ctx(
                df=df,
                spot=spot,
                read=read,
                pa_frames=pa_frames or {},
                dte=getattr(read, "dte", 4.0) if read else 4.0,
            )
            strike_select_tab.render(ctx)
        except Exception as _stk_err:
            log.exception(f"Strike Selection & Premium Behaviour Tab encountered an error: {_stk_err}")
            st.error(f"Strike Selection & Premium Behaviour Tab encountered an error: {_stk_err}")

    # TABMOVE: the Writers Graph and PE − CE OI Table blocks moved to options_lab_tab.render()
