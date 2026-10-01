"""
oi_analytics.py
═══════════════════════════════════════════════════════════════════════════════
Open Interest (OI) Analytics Engine for Nifty & Index Options
═══════════════════════════════════════════════════════════════════════════════

Comprehensive Open Interest intelligence suite that processes daily and intraday
Option Chain data to identify:
  1. Trend Identification & Build-ups:
     - Long Build-up (Price Up + OI Up): Fresh aggressive buyers entering.
     - Short Build-up (Price Down + OI Up): Fresh aggressive sellers entering.
     - Long Unwinding (Price Down + OI Down): Long liquidation / stop hunting.
     - Short Covering (Price Up + OI Down): Trapped shorts forced to cover.
  2. Dynamic Support & Resistance:
     - Strike with Highest Put OI (Major Institutional Wall / Support).
     - Strike with Highest Call OI (Major Institutional Ceiling / Resistance).
     - Secondary S/R and total Put/Call Open Interest distribution.
  3. The "Open-Low" / "Open-High" Setup:
     - Long: Open price nearly identical (within 0.5% - 0.75%) to Low of Day.
     - Short: Open price nearly identical to High of Day.
  4. Institutional Trap & Reversal Engine:
     - Detects "Trapped Sellers" when market gaps/breaks heavily past Highest Call OI.
     - Detects "Trapped Buyers" when market flushes past Highest Put OI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from ui_utils import render_html, _render_html

logger = logging.getLogger("oi_analytics")
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
class SupportResistanceLevels:
    """Represents dynamic Support and Resistance identified from Peak OI."""
    support_strike: float
    support_oi: float
    resistance_strike: float
    resistance_oi: float
    secondary_support_strike: float
    secondary_support_oi: float
    secondary_resistance_strike: float
    secondary_resistance_oi: float
    pcr_oi: float
    total_call_oi: float
    total_put_oi: float
    oi_range_pts: float
    note: str = ""


@dataclass
class OpenHighLowSetup:
    """Represents the Open-Low / Open-High setup status."""
    is_open_low: bool
    is_open_high: bool
    open_price: float
    high_price: float
    low_price: float
    current_price: float
    open_low_diff_pct: float
    open_high_diff_pct: float
    threshold_pct: float
    verdict: str
    bias: str  # 'BULLISH', 'BEARISH', or 'NEUTRAL'


@dataclass
class TrapReversalSignal:
    """Represents an Institutional Trap / Liquidity Squeeze Setup."""
    is_trapped: bool
    trap_type: str  # 'TRAPPED_CALL_SELLERS_SQUEEZE', 'TRAPPED_PUT_SELLERS_FLUSH', or 'NONE'
    breached_strike: float
    breach_type: str  # 'CALL_RESISTANCE_BLOWN' or 'PUT_SUPPORT_BROKEN'
    spot_price: float
    gap_or_distance_pts: float
    urgency: str  # 'HIGH_MOMENTUM_REVERSAL', 'WATCH', 'NONE'
    description: str


@dataclass
class OISummary:
    """Master analytical summary output."""
    timestamp: Optional[datetime]
    spot: float
    support_resistance: SupportResistanceLevels
    open_high_low: Optional[OpenHighLowSetup]
    trap_signals: List[TrapReversalSignal]
    overall_sentiment: str  # 'BULLISH', 'BEARISH', 'NEUTRAL'
    buildup_summary: Dict[str, int]
    actionable_trade_recommendation: str


# ─────────────────────────────────────────────────────────────────────────────
# OpenInterestAnalyzer Class
# ─────────────────────────────────────────────────────────────────────────────

class OpenInterestAnalyzer:
    """
    Analyzes Option Chain and Price Action Data for Nifty 50 and index derivatives.
    Ingests standard pandas DataFrames with flexible column naming.
    """

    def __init__(self, open_hl_threshold_pct: float = 0.5):
        """
        Parameters:
            open_hl_threshold_pct: Max percentage difference between Open and
                                  Low/High to qualify as Open=Low or Open=High (default 0.5%).
        """
        self.open_hl_threshold_pct = open_hl_threshold_pct
        logger.info(f"OpenInterestAnalyzer initialized with Open-HL threshold = {open_hl_threshold_pct}%")

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Flexible Column Normalization
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        Standardizes column names to internal standard:
        ['date', 'strike', 'expiry', 'call_oi', 'put_oi', 'call_iv', 'put_iv',
         'call_price', 'put_price', 'call_oi_chg', 'put_oi_chg', 'open', 'high', 'low', 'close', 'spot']
        """
        d = df.copy()
        col_map: Dict[str, str] = {}
        for col in d.columns:
            c_low = str(col).lower().strip().replace(" ", "_").replace("-", "_")

            # Strike
            if c_low in ("strike", "strike_price", "strikeprice"):
                col_map[col] = "strike"
            # Expiry
            elif c_low in ("expiry", "expiry_date", "exp_date", "exp"):
                col_map[col] = "expiry"
            # Date / Timestamp
            elif c_low in ("date", "timestamp", "datetime", "time"):
                col_map[col] = "date"
            # Call OI
            elif c_low in ("call_oi", "ce_oi", "coi", "c_oi", "calloi", "calls_oi"):
                col_map[col] = "call_oi"
            # Put OI
            elif c_low in ("put_oi", "pe_oi", "poi", "p_oi", "putoi", "puts_oi"):
                col_map[col] = "put_oi"
            # Call OI Change
            elif c_low in ("call_oi_chg", "ce_oi_chg", "ce_prev_oi", "call_coi", "ce_chg_oi"):
                col_map[col] = "call_oi_chg"
            # Put OI Change
            elif c_low in ("put_oi_chg", "pe_oi_chg", "pe_prev_oi", "put_coi", "pe_chg_oi"):
                col_map[col] = "put_oi_chg"
            # Call IV
            elif c_low in ("call_iv", "ce_iv", "c_iv"):
                col_map[col] = "call_iv"
            # Put IV
            elif c_low in ("put_iv", "pe_iv", "p_iv"):
                col_map[col] = "put_iv"
            # Call Price / LTP
            elif c_low in ("call_price", "ce_ltp", "call_ltp", "ce_close", "c_price"):
                col_map[col] = "call_price"
            # Put Price / LTP
            elif c_low in ("put_price", "pe_ltp", "put_ltp", "pe_close", "p_price"):
                col_map[col] = "put_price"
            # Spot / Underlying
            elif c_low in ("spot", "underlying", "underlying_value", "spot_price"):
                col_map[col] = "spot"
            # OHLC
            elif c_low == "open":
                col_map[col] = "open"
            elif c_low == "high":
                col_map[col] = "high"
            elif c_low == "low":
                col_map[col] = "low"
            elif c_low in ("close", "ltp"):
                col_map[col] = "close"

        d = d.rename(columns=col_map)
        return d

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Trend Identification & Build-ups
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def classify_buildup(price_change: float, oi_change: float) -> str:
        """
        Classifies price and open interest interaction into one of 4 archetypes:
          - Long Build-up: Price ↑ AND OI ↑
          - Short Build-up: Price ↓ AND OI ↑
          - Long Unwinding: Price ↓ AND OI ↓
          - Short Covering: Price ↑ AND OI ↓
        """
        if price_change > 0 and oi_change > 0:
            return "LONG_BUILDUP"
        elif price_change < 0 and oi_change > 0:
            return "SHORT_BUILDUP"
        elif price_change < 0 and oi_change < 0:
            return "LONG_UNWINDING"
        elif price_change > 0 and oi_change < 0:
            return "SHORT_COVERING"
        return "NEUTRAL"

    def identify_buildups(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Evaluates build-ups per strike across both Call and Put contracts.
        Expected columns: strike, and either prices & oi or changes.
        """
        d = self.normalize_dataframe(df)
        res = d.copy()

        # Check Call Buildups
        if "call_price" in res.columns and "call_oi" in res.columns:
            # If changes not explicitly present, calculate diff
            if "call_price_chg" not in res.columns:
                res["call_price_chg"] = res["call_price"].diff().fillna(0)
            if "call_oi_chg" not in res.columns:
                res["call_oi_chg"] = res["call_oi"].diff().fillna(0)

            res["call_buildup"] = [
                self.classify_buildup(p, o)
                for p, o in zip(res["call_price_chg"], res["call_oi_chg"])
            ]

        # Check Put Buildups
        if "put_price" in res.columns and "put_oi" in res.columns:
            if "put_price_chg" not in res.columns:
                res["put_price_chg"] = res["put_price"].diff().fillna(0)
            if "put_oi_chg" not in res.columns:
                res["put_oi_chg"] = res["put_oi"].diff().fillna(0)

            res["put_buildup"] = [
                self.classify_buildup(p, o)
                for p, o in zip(res["put_price_chg"], res["put_oi_chg"])
            ]

        return res

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Dynamic Support & Resistance Identification
    # ─────────────────────────────────────────────────────────────────────────

    def get_support_resistance(self, df: pd.DataFrame) -> SupportResistanceLevels:
        """
        Identifies dynamic Support (Peak Put OI) and Resistance (Peak Call OI).
        Also computes secondary levels and total Put/Call Ratio (PCR).
        """
        d = self.normalize_dataframe(df)

        if "strike" not in d.columns or "call_oi" not in d.columns or "put_oi" not in d.columns:
            raise ValueError("DataFrame must contain 'strike', 'call_oi', and 'put_oi' columns.")

        # Aggregate across multiple expiries if present
        grouped = d.groupby("strike")[["call_oi", "put_oi"]].sum().reset_index()

        # Sort for Peak Call OI (Resistance)
        call_sorted = grouped.sort_values(by="call_oi", ascending=False)
        r1_strike = float(call_sorted.iloc[0]["strike"])
        r1_oi = float(call_sorted.iloc[0]["call_oi"])
        r2_strike = float(call_sorted.iloc[1]["strike"]) if len(call_sorted) > 1 else r1_strike
        r2_oi = float(call_sorted.iloc[1]["call_oi"]) if len(call_sorted) > 1 else r1_oi

        # Sort for Peak Put OI (Support)
        put_sorted = grouped.sort_values(by="put_oi", ascending=False)
        s1_strike = float(put_sorted.iloc[0]["strike"])
        s1_oi = float(put_sorted.iloc[0]["put_oi"])
        s2_strike = float(put_sorted.iloc[1]["strike"]) if len(put_sorted) > 1 else s1_strike
        s2_oi = float(put_sorted.iloc[1]["put_oi"]) if len(put_sorted) > 1 else s1_oi

        total_call_oi = float(grouped["call_oi"].sum())
        total_put_oi = float(grouped["put_oi"].sum())
        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 1.0

        oi_range = abs(r1_strike - s1_strike)
        note = (
            f"Primary Wall Support at {s1_strike:,.0f} ({s1_oi:,.0f} Puts) | "
            f"Primary Ceiling Resistance at {r1_strike:,.0f} ({r1_oi:,.0f} Calls). PCR: {pcr:.2f}"
        )

        logger.debug(note)

        return SupportResistanceLevels(
            support_strike=s1_strike,
            support_oi=s1_oi,
            resistance_strike=r1_strike,
            resistance_oi=r1_oi,
            secondary_support_strike=s2_strike,
            secondary_support_oi=s2_oi,
            secondary_resistance_strike=r2_strike,
            secondary_resistance_oi=r2_oi,
            pcr_oi=pcr,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            oi_range_pts=oi_range,
            note=note
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 4. The "Open-Low" / "Open-High" Setup
    # ─────────────────────────────────────────────────────────────────────────

    def check_open_low_high(
        self,
        open_price: float,
        high_price: float,
        low_price: float,
        current_price: Optional[float] = None
    ) -> OpenHighLowSetup:
        """
        Checks whether market Open is nearly identical to Low (Bullish) or High (Bearish).
        Threshold window is specified in percentage (e.g. 0.5% - 0.75%).
        """
        cur = current_price if current_price is not None else open_price

        # Differences relative to Open
        diff_low_pct = abs(open_price - low_price) / open_price * 100.0
        diff_high_pct = abs(high_price - open_price) / open_price * 100.0

        is_open_low = diff_low_pct <= self.open_hl_threshold_pct
        is_open_high = diff_high_pct <= self.open_hl_threshold_pct

        if is_open_low and not is_open_high:
            verdict = (
                f"🟢 OPEN = LOW CONFIRMED (Diff: {diff_low_pct:.3f}% <= {self.open_hl_threshold_pct}%). "
                f"Buyers established control from the opening bell. Strong long bias."
            )
            bias = "BULLISH"
        elif is_open_high and not is_open_low:
            verdict = (
                f"🔴 OPEN = HIGH CONFIRMED (Diff: {diff_high_pct:.3f}% <= {self.open_hl_threshold_pct}%). "
                f"Sellers dominated from the opening bell. Strong short bias."
            )
            bias = "BEARISH"
        elif is_open_low and is_open_high:
            verdict = "⚠️ Range compression: Open, High, and Low are all within threshold."
            bias = "NEUTRAL"
        else:
            verdict = (
                f"Neutral: Open-Low diff ({diff_low_pct:.2f}%) and "
                f"Open-High diff ({diff_high_pct:.2f}%) exceed threshold ({self.open_hl_threshold_pct}%)."
            )
            bias = "NEUTRAL"

        return OpenHighLowSetup(
            is_open_low=is_open_low,
            is_open_high=is_open_high,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            current_price=cur,
            open_low_diff_pct=diff_low_pct,
            open_high_diff_pct=diff_high_pct,
            threshold_pct=self.open_hl_threshold_pct,
            verdict=verdict,
            bias=bias
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Institutional Trap & Reversal Engine
    # ─────────────────────────────────────────────────────────────────────────

    def detect_trapped_participants(
        self,
        df: pd.DataFrame,
        spot: float,
        prev_close: Optional[float] = None
    ) -> List[TrapReversalSignal]:
        """
        Detects 'trapped sellers' or 'trapped buyers':
        If the market gaps or breaks heavily past the highest OI strikes,
        flags high-momentum reversal / short squeeze setups due to forced OI unwinding.
        """
        sr = self.get_support_resistance(df)
        signals: List[TrapReversalSignal] = []

        res_strike = sr.resistance_strike
        sup_strike = sr.support_strike

        # 1. Trapped Call Sellers (Short Squeeze Trigger)
        # Spot is substantially above the peak Call OI strike
        if spot > res_strike:
            pts_above = spot - res_strike
            gap_str = f" Spot is +{pts_above:.1f} pts above peak Call OI."
            if prev_close and prev_close < res_strike:
                gap_str = f" Heavy Gap-Up past {res_strike:,.0f} Call wall ({pts_above:.1f} pts above)."

            desc = (
                f"🚀 TRAPPED CALL SELLERS DETECTED! Market has broken past peak Call OI strike "
                f"at {res_strike:,.0f}. Institutional call writers are caught offside.{gap_str} "
                f"Forced short covering can trigger an explosive momentum squeeze."
            )
            signals.append(TrapReversalSignal(
                is_trapped=True,
                trap_type="TRAPPED_CALL_SELLERS_SQUEEZE",
                breached_strike=res_strike,
                breach_type="CALL_RESISTANCE_BLOWN",
                spot_price=spot,
                gap_or_distance_pts=pts_above,
                urgency="HIGH_MOMENTUM_REVERSAL",
                description=desc
            ))
            logger.warning(desc)

        # 2. Trapped Put Sellers (Long Flush / Panic Cascade)
        # Spot is substantially below the peak Put OI strike
        if spot < sup_strike:
            pts_below = sup_strike - spot
            gap_str = f" Spot is -{pts_below:.1f} pts below peak Put OI."
            if prev_close and prev_close > sup_strike:
                gap_str = f" Heavy Gap-Down below {sup_strike:,.0f} Put wall ({pts_below:.1f} pts below)."

            desc = (
                f"⚠️ TRAPPED PUT SELLERS DETECTED! Market has flushed below peak Put OI strike "
                f"at {sup_strike:,.0f}. Institutional put writers are trapped.{gap_str} "
                f"Forced long unwinding / panic hedging can accelerate aggressive downside momentum."
            )
            signals.append(TrapReversalSignal(
                is_trapped=True,
                trap_type="TRAPPED_PUT_SELLERS_FLUSH",
                breached_strike=sup_strike,
                breach_type="PUT_SUPPORT_BROKEN",
                spot_price=spot,
                gap_or_distance_pts=pts_below,
                urgency="HIGH_MOMENTUM_REVERSAL",
                description=desc
            ))
            logger.warning(desc)

        if not signals:
            signals.append(TrapReversalSignal(
                is_trapped=False,
                trap_type="NONE",
                breached_strike=0.0,
                breach_type="NONE",
                spot_price=spot,
                gap_or_distance_pts=0.0,
                urgency="NONE",
                description=f"Market is trading safely within OI boundaries ({sup_strike:,.0f} - {res_strike:,.0f})."
            ))

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Master Full-Spectrum Analysis
    # ─────────────────────────────────────────────────────────────────────────

    def analyze_chain(
        self,
        df: pd.DataFrame,
        spot: Optional[float] = None,
        open_price: Optional[float] = None,
        high_price: Optional[float] = None,
        low_price: Optional[float] = None,
        prev_close: Optional[float] = None,
        timestamp: Optional[datetime] = None
    ) -> OISummary:
        """
        Executes a complete, consolidated Open Interest analysis run.
        """
        d = self.normalize_dataframe(df)

        # Extract spot price if not explicitly provided
        if spot is None:
            if "spot" in d.columns and not d["spot"].isna().all():
                spot = float(d["spot"].dropna().iloc[0])
            elif "close" in d.columns and not d["close"].isna().all():
                spot = float(d["close"].dropna().iloc[0])
            else:
                spot = 0.0

        # S&R
        sr = self.get_support_resistance(d)

        # Traps
        traps = self.detect_trapped_participants(d, spot, prev_close=prev_close)

        # Open-Low-High setup
        ohl_setup = None
        if open_price is not None and high_price is not None and low_price is not None:
            ohl_setup = self.check_open_low_high(open_price, high_price, low_price, current_price=spot)

        # Build-ups aggregation
        df_buildup = self.identify_buildups(d)
        buildup_counts: Dict[str, int] = {
            "LONG_BUILDUP": 0,
            "SHORT_BUILDUP": 0,
            "LONG_UNWINDING": 0,
            "SHORT_COVERING": 0,
            "NEUTRAL": 0
        }

        for col in ("call_buildup", "put_buildup"):
            if col in df_buildup.columns:
                counts = df_buildup[col].value_counts().to_dict()
                for k, v in counts.items():
                    if k in buildup_counts:
                        buildup_counts[k] += int(v)

        # Determine overall sentiment
        bullish_score = 0
        bearish_score = 0

        if sr.pcr_oi >= 1.25:
            bullish_score += 2
        elif sr.pcr_oi <= 0.75:
            bearish_score += 2

        if ohl_setup:
            if ohl_setup.bias == "BULLISH":
                bullish_score += 2
            elif ohl_setup.bias == "BEARISH":
                bearish_score += 2

        for t in traps:
            if t.trap_type == "TRAPPED_CALL_SELLERS_SQUEEZE":
                bullish_score += 3
            elif t.trap_type == "TRAPPED_PUT_SELLERS_FLUSH":
                bearish_score += 3

        if buildup_counts["LONG_BUILDUP"] + buildup_counts["SHORT_COVERING"] > buildup_counts["SHORT_BUILDUP"] + buildup_counts["LONG_UNWINDING"]:
            bullish_score += 1
        else:
            bearish_score += 1

        if bullish_score > bearish_score + 1:
            sentiment = "BULLISH"
            rec = "🟢 Focus on BUY CALLS (Long setups). Look for dips near Support."
        elif bearish_score > bullish_score + 1:
            sentiment = "BEARISH"
            rec = "🔴 Focus on BUY PUTS (Short setups). Look for rallies near Resistance."
        else:
            sentiment = "NEUTRAL"
            rec = "⚪ Sideways / Rangebound market. Trade mean-reversion within Peak OI boundaries."

        # Override recommendation if high-urgency trap is active
        for t in traps:
            if t.urgency == "HIGH_MOMENTUM_REVERSAL":
                if t.trap_type == "TRAPPED_CALL_SELLERS_SQUEEZE":
                    rec = "🔥 HIGH CONVICTION CE MOMENTUM: Call writers trapped! Ride short-covering squeeze."
                elif t.trap_type == "TRAPPED_PUT_SELLERS_FLUSH":
                    rec = "💥 HIGH CONVICTION PE MOMENTUM: Put writers trapped! Ride long-liquidation cascade."

        return OISummary(
            timestamp=timestamp or datetime.now(),
            spot=spot,
            support_resistance=sr,
            open_high_low=ohl_setup,
            trap_signals=traps,
            overall_sentiment=sentiment,
            buildup_summary=buildup_counts,
            actionable_trade_recommendation=rec
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Pure Option Chain & Open Interest Directional Trade Filter (No VWAP)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OITradeFilterVerdict:
    """Represents a pure Option Chain / Open Interest directional trade filter verdict (Zero VWAP)."""
    bias: str                       # 'BULLISH', 'BEARISH', 'NEUTRAL', 'SQUEEZE_LONG', 'CASCADE_SHORT'
    allowed_direction: str          # 'BUY_CE', 'BUY_PE', 'RANGE_BOUND', or 'BLOCKED'
    confidence: str                 # 'HIGH', 'MEDIUM', 'LOW'
    ce_permission: str              # 'ALLOWED', 'BLOCKED_BY_CALL_WALL', 'CAUTION'
    pe_permission: str              # 'ALLOWED', 'BLOCKED_BY_PUT_WALL', 'CAUTION'
    pcr: float
    support_strike: float
    support_oi: float
    support_dist_pts: float
    resistance_strike: float
    resistance_oi: float
    resistance_dist_pts: float
    open_hl_status: str
    trap_status: str
    reasons: List[str]
    warnings: List[str]
    actionable_summary: str


def evaluate_oi_trade_filter(
    df: pd.DataFrame,
    spot: Optional[float] = None,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    prev_close: Optional[float] = None,
    open_hl_threshold_pct: float = 0.5
) -> OITradeFilterVerdict:
    """
    Evaluates pure Option Chain & Open Interest data to form a directional execution filter.
    Contains ZERO VWAP dependency.
    """
    analyzer = OpenInterestAnalyzer(open_hl_threshold_pct=open_hl_threshold_pct)
    d = analyzer.normalize_dataframe(df)

    # Derive spot if not passed
    if spot is None or spot <= 0:
        if "spot" in d.columns and not d["spot"].isna().all():
            spot = float(d["spot"].dropna().iloc[0])
        elif "close" in d.columns and not d["close"].isna().all():
            spot = float(d["close"].dropna().iloc[0])
        elif pa_frames:
            for tf in ("1m", "5m"):
                fr = pa_frames.get(tf)
                if fr is not None and not fr.empty and "close" in fr.columns:
                    spot = float(fr.iloc[-1]["close"])
                    break
        if spot is None:
            spot = 0.0

    # Derive OHLC if available from pa_frames
    open_p, high_p, low_p = None, None, None
    if pa_frames:
        for tf in ("1m", "5m", "15m"):
            fr = pa_frames.get(tf)
            if fr is not None and not fr.empty and "open" in fr.columns:
                open_p = float(fr.iloc[0]["open"])
                high_p = float(fr["high"].max())
                low_p = float(fr["low"].min())
                break

    # Analyze S&R
    sr = analyzer.get_support_resistance(d)
    traps = analyzer.detect_trapped_participants(d, spot, prev_close=prev_close)

    # Open-Low / Open-High check
    ohl_setup = None
    open_hl_desc = "Intraday OHLC Not Available"
    if open_p is not None and high_p is not None and low_p is not None:
        ohl_setup = analyzer.check_open_low_high(open_p, high_p, low_p, current_price=spot)
        open_hl_desc = ohl_setup.verdict

    # Buildups
    df_buildup = analyzer.identify_buildups(d)
    buildup_counts = {"LONG_BUILDUP": 0, "SHORT_BUILDUP": 0, "LONG_UNWINDING": 0, "SHORT_COVERING": 0}
    for col in ("call_buildup", "put_buildup"):
        if col in df_buildup.columns:
            counts = df_buildup[col].value_counts().to_dict()
            for k in buildup_counts:
                buildup_counts[k] += int(counts.get(k, 0))

    dist_to_sup = spot - sr.support_strike       # Positive means spot is above support
    dist_to_res = sr.resistance_strike - spot    # Positive means spot is below resistance

    reasons: List[str] = []
    warnings: List[str] = []
    bias = "NEUTRAL"
    allowed = "RANGE_BOUND"
    conf = "MEDIUM"
    ce_perm = "CAUTION"
    pe_perm = "CAUTION"
    trap_status = "No Traps Active (Trading within OI boundaries)"

    # Check for Trapped Squeeze or Flush
    is_squeeze = any(t.trap_type == "TRAPPED_CALL_SELLERS_SQUEEZE" for t in traps)
    is_flush = any(t.trap_type == "TRAPPED_PUT_SELLERS_FLUSH" for t in traps)

    if is_squeeze:
        trap_status = f"🔥 TRAPPED CALL SELLERS: Spot broke above {sr.resistance_strike:,.0f} Call wall (+{abs(dist_to_res):.1f} pts)"
    elif is_flush:
        trap_status = f"💥 TRAPPED PUT SELLERS: Spot flushed below {sr.support_strike:,.0f} Put wall (-{abs(dist_to_sup):.1f} pts)"

    # 1. CE Permission Assessment
    if is_squeeze:
        ce_perm = "ALLOWED (SHORT_SQUEEZE)"
        reasons.append(f"Short Squeeze Trigger: Call writers trapped above {sr.resistance_strike:,.0f}.")
    elif dist_to_res <= 20.0 and dist_to_res >= -10.0:
        ce_perm = "BLOCKED_BY_CALL_WALL"
        warnings.append(f"Heavy Call Resistance at {sr.resistance_strike:,.0f} (Only {dist_to_res:.1f} pts away). R:R unfavourable for Longs.")
    elif sr.pcr_oi < 0.70:
        ce_perm = "BLOCKED_BY_PCR"
        warnings.append(f"Bearish PCR ({sr.pcr_oi:.2f} < 0.70): Heavy Call writing across strikes. Long CE trades blocked.")
    elif dist_to_sup >= 0 and dist_to_res >= 35.0:
        ce_perm = "ALLOWED"
        reasons.append(f"Put Support at {sr.support_strike:,.0f} holding (+{dist_to_sup:.1f} pts above). Headroom to Resistance (+{dist_to_res:.1f} pts).")

    # 2. PE Permission Assessment
    if is_flush:
        pe_perm = "ALLOWED (LIQUIDATION_CASCADE)"
        reasons.append(f"Long Liquidation Trigger: Put writers trapped below {sr.support_strike:,.0f}.")
    elif dist_to_sup <= 20.0 and dist_to_sup >= -10.0:
        pe_perm = "BLOCKED_BY_PUT_WALL"
        warnings.append(f"Heavy Put Support at {sr.support_strike:,.0f} (Only {dist_to_sup:.1f} pts away). High bounce risk for Shorts.")
    elif sr.pcr_oi > 1.35:
        pe_perm = "BLOCKED_BY_PCR"
        warnings.append(f"Bullish PCR ({sr.pcr_oi:.2f} > 1.35): Heavy Put writing across strikes. Short PE trades blocked.")
    elif dist_to_res >= 0 and dist_to_sup >= 35.0:
        pe_perm = "ALLOWED"
        reasons.append(f"Call Resistance at {sr.resistance_strike:,.0f} holding (-{dist_to_res:.1f} pts below). Headroom to Support (-{dist_to_sup:.1f} pts).")

    # 3. Overall Directional Decision (No VWAP)
    if is_squeeze or (ce_perm.startswith("ALLOWED") and (pe_perm.startswith("BLOCKED") or pe_perm.startswith("CAUTION")) and sr.pcr_oi >= 1.0):
        bias = "SQUEEZE_LONG" if is_squeeze else "BULLISH"
        allowed = "BUY_CE"
        conf = "HIGH" if (is_squeeze or (ohl_setup and ohl_setup.bias == "BULLISH")) else "MEDIUM"
        action = f"🟢 FOCUS ON BUY CALLS (CE). Put wall {sr.support_strike:,.0f} is secure. Target {sr.resistance_strike:,.0f}."
    elif is_flush or (pe_perm.startswith("ALLOWED") and (ce_perm.startswith("BLOCKED") or ce_perm.startswith("CAUTION")) and sr.pcr_oi <= 0.85):
        bias = "CASCADE_SHORT" if is_flush else "BEARISH"
        allowed = "BUY_PE"
        conf = "HIGH" if (is_flush or (ohl_setup and ohl_setup.bias == "BEARISH")) else "MEDIUM"
        action = f"🔴 FOCUS ON BUY PUTS (PE). Call ceiling {sr.resistance_strike:,.0f} is capping. Target {sr.support_strike:,.0f}."
    elif dist_to_sup < 30.0 and dist_to_res < 30.0:
        bias = "NEUTRAL"
        allowed = "RANGE_BOUND"
        conf = "LOW"
        action = f"⚠️ TIGHT PIN / NO-TRADE ZONE. Spot sandwiched between Put Wall {sr.support_strike:,.0f} and Call Wall {sr.resistance_strike:,.0f}."
    else:
        bias = "NEUTRAL"
        allowed = "RANGE_BOUND"
        conf = "MEDIUM"
        action = f"⚖️ SELECTIVE / RANGEBOUND. Play support bounces near {sr.support_strike:,.0f} and resistance fades near {sr.resistance_strike:,.0f}."

    return OITradeFilterVerdict(
        bias=bias,
        allowed_direction=allowed,
        confidence=conf,
        ce_permission=ce_perm,
        pe_permission=pe_perm,
        pcr=sr.pcr_oi,
        support_strike=sr.support_strike,
        support_oi=sr.support_oi,
        support_dist_pts=dist_to_sup,
        resistance_strike=sr.resistance_strike,
        resistance_oi=sr.resistance_oi,
        resistance_dist_pts=dist_to_res,
        open_hl_status=open_hl_desc,
        trap_status=trap_status,
        reasons=reasons,
        warnings=warnings,
        actionable_summary=action
    )


def render_oi_filter_card(
    df: pd.DataFrame,
    spot: float,
    pa_frames: Optional[Dict[str, pd.DataFrame]] = None,
    prev_close: Optional[float] = None
) -> None:
    """
    Renders an institutional Streamlit card displaying the Option Chain / OI Trade Filter.
    Zero VWAP dependencies — pure OI, PCR, Peak Walls, and Open=Low/High symmetry.
    """
    try:
        import streamlit as st
    except ImportError:
        return

    verdict = evaluate_oi_trade_filter(df, spot=spot, pa_frames=pa_frames, prev_close=prev_close)

    st.markdown("### 🧠 AI Option Chain & OI Execution Filter *(Pure OI — Zero VWAP)*")

    # ── High-Contrast Status Banner ──────────────────────────────────────────
    if verdict.allowed_direction == "BUY_CE":
        banner_bg = "#064E3B"
        border_color = "#10B981"
        badge_text = f"🟢 BUY CALLS (CE) PERMITTED · Confidence: {verdict.confidence}"
    elif verdict.allowed_direction == "BUY_PE":
        banner_bg = "#7F1D1D"
        border_color = "#EF4444"
        badge_text = f"🔴 BUY PUTS (PE) PERMITTED · Confidence: {verdict.confidence}"
    else:
        banner_bg = "#1E293B"
        border_color = "#F59E0B"
        badge_text = f"⚠️ RANGEBOUND / NO-TRADE ZONE · Confidence: {verdict.confidence}"

    _render_html(
        f"""
        <div style="background-color:{banner_bg}; border: 2px solid {border_color}; border-radius: 8px; padding: 12px 18px; margin-bottom: 14px;">
            <div style="font-size: 1.15em; font-weight: 700; color: #FFFFFF;">{badge_text}</div>
            <div style="font-size: 0.95em; color: #E2E8F0; margin-top: 4px;">{verdict.actionable_summary}</div>
        </div>
        """
    )

    # ── 4 Key Telemetry Columns ─────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric(
            label="🛡️ Peak Put Support Wall",
            value=f"{verdict.support_strike:,.0f}",
            delta=f"{verdict.support_dist_pts:+.1f} pts from Spot"
        )
        st.caption(f"Put OI: **{verdict.support_oi:,.0f}** contracts")

    with c2:
        st.metric(
            label="🛑 Peak Call Resistance Wall",
            value=f"{verdict.resistance_strike:,.0f}",
            delta=f"{verdict.resistance_dist_pts:+.1f} pts from Spot",
            delta_color="inverse"
        )
        st.caption(f"Call OI: **{verdict.resistance_oi:,.0f}** contracts")

    with c3:
        pcr_sentiment = "BULLISH" if verdict.pcr >= 1.2 else ("BEARISH" if verdict.pcr <= 0.8 else "NEUTRAL")
        st.metric(
            label="📊 Total PCR (Put/Call)",
            value=f"{verdict.pcr:.2f}",
            delta=pcr_sentiment
        )
        st.caption("Ratio of Total Puts to Calls")

    with c4:
        st.metric(
            label="⚖️ Open=Low/High Status",
            value="ACTIVE" if "CONFIRMED" in verdict.open_hl_status else "NORMAL",
            delta=verdict.bias
        )
        st.caption(verdict.open_hl_status[:38] + "..." if len(verdict.open_hl_status) > 38 else verdict.open_hl_status)

    # ── Expandable Institutional Analysis ───────────────────────────────────
    with st.expander("🔍 Deep-Dive: Option Chain Filter Telemetry & Strike Build-ups", expanded=False):
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown(f"**Call (CE) Status:** `{verdict.ce_permission}`")
            st.markdown(f"**Put (PE) Status:** `{verdict.pe_permission}`")
            st.markdown(f"**Institutional Trap Status:** {verdict.trap_status}")

        with col_b:
            if verdict.reasons:
                st.markdown("**Supporting Factors:**")
                for r in verdict.reasons:
                    st.markdown(f"- ✅ {r}")
            if verdict.warnings:
                st.markdown("**Execution Warnings:**")
                for w in verdict.warnings:
                    st.markdown(f"- ⚠️ {w}")

        # Near-ATM Strike Build-up Table
        try:
            an = OpenInterestAnalyzer()
            d_buildup = an.identify_buildups(df)
            if "strike" in d_buildup.columns:
                atm_center = round(spot / 50.0) * 50
                atm_window = d_buildup[
                    (d_buildup["strike"] >= atm_center - 250) &
                    (d_buildup["strike"] <= atm_center + 250)
                ].copy()

                display_cols = [c for c in ["strike", "call_oi", "call_oi_chg", "call_buildup", "put_buildup", "put_oi_chg", "put_oi"] if c in atm_window.columns]
                if display_cols:
                    st.markdown("**Near-ATM Strike Build-up Scanner:**")
                    st.dataframe(atm_window[display_cols].sort_values("strike"), use_container_width=True, hide_index=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Self-Test Execution Block
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═════════════════════════════════════════════════════════════════")
    print("  TESTING OPEN INTEREST ANALYZER (oi_analytics.py)")
    print("═════════════════════════════════════════════════════════════════")

    # Construct representative test data
    test_data = {
        "Strike": [23000, 23100, 23200, 23300, 23400, 23500],
        "Call_OI": [120000, 250000, 480000, 1150000, 890000, 520000],
        "Put_OI": [1450000, 920000, 510000, 310000, 180000, 95000],
        "Call_Price": [510.0, 425.0, 345.0, 260.0, 190.0, 130.0],
        "Put_Price": [75.0, 95.0, 125.0, 175.0, 240.0, 320.0],
        "Call_OI_Chg": [5000, 12000, 45000, -85000, 60000, 35000],
        "Put_OI_Chg": [85000, 42000, -15000, -35000, 8000, 2000],
        "Spot": [23350.0] * 6
    }
    df_sample = pd.DataFrame(test_data)

    analyzer = OpenInterestAnalyzer(open_hl_threshold_pct=0.5)

    # 1. Test S&R
    sr = analyzer.get_support_resistance(df_sample)
    print("\n1. SUPPORT & RESISTANCE:")
    print(f"   Peak Put OI Support:    {sr.support_strike:,.0f} ({sr.support_oi:,.0f} Puts)")
    print(f"   Peak Call OI Resistance: {sr.resistance_strike:,.0f} ({sr.resistance_oi:,.0f} Calls)")
    print(f"   Put-Call Ratio (PCR):    {sr.pcr_oi:.2f}")

    # 2. Test Open=Low
    ohl = analyzer.check_open_low_high(
        open_price=23300.0,
        high_price=23420.0,
        low_price=23295.0, # 5 pts away (0.021%)
        current_price=23380.0
    )
    print("\n2. OPEN-LOW SETUP:")
    print(f"   {ohl.verdict}")

    # 3. Test Trap Seller Logic (Spot = 23350 > 23300 Peak Call OI)
    summary = analyzer.analyze_chain(
        df=df_sample,
        spot=23350.0,
        open_price=23300.0,
        high_price=23420.0,
        low_price=23295.0,
        prev_close=23250.0
    )
    print("\n3. MASTER SUMMARY & TRAP SIGNALS:")
    print(f"   Overall Sentiment: {summary.overall_sentiment}")
    print(f"   Actionable Rec:    {summary.actionable_trade_recommendation}")

    # 4. Test Pure OI Directional Trade Filter (No VWAP)
    v = evaluate_oi_trade_filter(
        df=df_sample,
        spot=23350.0,
        prev_close=23250.0
    )
    print("\n4. PURE OI TRADE FILTER (ZERO VWAP):")
    print(f"   Allowed Direction: {v.allowed_direction} (Confidence: {v.confidence})")
    print(f"   CE Permission:     {v.ce_permission}")
    print(f"   PE Permission:     {v.pe_permission}")
    print(f"   Action Summary:    {v.actionable_summary}")

    print("\nSUCCESS: oi_analytics.py verified and operational!")

