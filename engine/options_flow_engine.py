"""
engine/options_flow_engine.py — Options Flow, Four Pillars & Environmental Confluence Engine.

Implements institutional quantitative logic for:
  1. The Four Pillars Scorecard:
     - Volatility Regime (India VIX & IV Percentile)
     - Institutional Positioning (FII / DII Index Futures & Options flow)
     - Market Breadth (Advances vs Declines across benchmark basket)
     - Global Cues (GIFT Nifty, S&P 500, DXY, Brent Crude)
     - Real-time Composite Bias Score (-4.0 to +4.0)
     - Dynamic Institutional Algo Playbook Directive generator
  2. OI Pillar Migration (Strike-Level Real-Time Classification):
     - LONG BUILD-UP: Price UP, OI UP (Institutional aggressive buying)
     - SHORT BUILD-UP: Price DOWN, OI UP (Institutional aggressive selling/writing)
     - SHORT COVERING: Price UP, OI DOWN (Trapped bears forced to cover)
     - LONG UNWINDING: Price DOWN, OI DOWN (Longs dumping inventory)
  3. Gamma Squeeze & Sticky Pin Zones:
     - Dealer Gamma Exposure (GEX) profiling
     - Zero Gamma (Gamma Flip) boundary
     - Sticky Zones: Long dealer gamma pinning price to strike walls
     - Squeeze Zones: Short dealer gamma accelerating momentum / volatility breakout
  4. Institutional Zone Matrix:
     - Merges Price Action Liquidity Pools (EQH, EQL, PDH, PDL) with Option Chain S/R (S1, S2, R1, R2, Call/Put Walls)
     - Classifies status as "CONFIRMED", "CONTESTED", or "WEAKENING" based on OI build-up vs decay
     - Validates SMC vs. Options Flow Confluence (Agreement vs. Disagreement)
  5. Active Setups Feed Generator:
     - Real-time convergence between SMC setups (OB, FVG, SFP, Dragon Fruit) and Options positioning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
import numpy as np
import pandas as pd

from engine.smc_engine import DealingRange


# ═══════════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PillarScore:
    name: str                           # "VIX_REGIME" | "FII_DII" | "MARKET_BREADTH" | "GLOBAL_CUES"
    title: str                          # Display name
    raw_metric: str                     # e.g., "VIX 12.8 (-3.4%)"
    score: float                        # -1.0 to +1.0
    regime: str                         # e.g., "COMPRESSING (BULLISH)"
    detail: str                         # Quant description
    status_color: str                   # "EMERALD", "ROSE", "SLATE", "AMBER"


@dataclass
class FourPillarsReport:
    pillars: list[PillarScore]
    composite_score: float              # Sum of 4 pillars (-4.0 to +4.0)
    bias_label: str                     # "EXTREME BULLISH", "MODERATE BULLISH", "NEUTRAL", etc.
    badge_variant: str                  # "EMERALD", "ROSE", "SLATE"
    algo_directive: str                 # Dynamic execution playbook string
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class StrikeOIClassification:
    strike: int
    ce_ltp: float
    ce_ltp_chg: float
    ce_oi: float
    ce_oi_chg: float
    ce_oi_pct: float
    ce_state: Literal["LONG BUILD-UP", "SHORT BUILD-UP", "SHORT COVERING", "LONG UNWINDING", "FLAT"]
    pe_ltp: float
    pe_ltp_chg: float
    pe_oi: float
    pe_oi_chg: float
    pe_oi_pct: float
    pe_state: Literal["LONG BUILD-UP", "SHORT BUILD-UP", "SHORT COVERING", "LONG UNWINDING", "FLAT"]
    is_atm: bool = False
    dist_from_atm: float = 0.0


@dataclass
class GammaProfile:
    spot_price: float
    zero_gamma_level: float
    net_gex_crores: float
    current_regime: Literal["STICKY_PIN_ZONE", "GAMMA_SQUEEZE_ZONE", "TRANSITION"]
    dealer_position: Literal["LONG_GAMMA", "SHORT_GAMMA"]
    sticky_strikes: list[int]          # Strikes where dealer gamma pinning dampens volatility
    squeeze_strikes: list[int]         # Strikes where dealer hedging accelerates breakout
    call_wall_strike: int
    put_wall_strike: int
    rationale: str


@dataclass
class ZoneMatrixRow:
    level: float
    label: str                          # e.g. "R1 / Call Wall", "S1 / Bullish OB", "EQL Liquidity Pool"
    zone_type: Literal["SUPPORT", "RESISTANCE", "MAGNET"]
    oi_evidence: str                    # e.g. "24,000 CE (1.42 Cr OI, +18% fresh call writing)"
    smc_evidence: str                   # e.g. "15m Bearish Breaker Block + EQH Pool"
    alignment: Literal["CONFIRMED", "CONTESTED", "WEAKENING"]
    alignment_reason: str
    distance_pts: float
    is_immediate: bool = False


@dataclass
class ActiveSetupCard:
    setup_name: str                     # e.g. "Dragon Fruit Combo Detected"
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    timeframe: str                      # "15m", "5m", "1m"
    conviction: Literal["TIER 1 SNIPER", "HIGH PROBABILITY", "TACTICAL"]
    logic: str                          # e.g. "15m Imbalance overlaps with 23900 Call Unwinding. Premium Zone."
    action: str                         # e.g. "Look for SFP (Swing Failure) on 1m chart for short entry."
    trigger_level: float
    stop_loss: float
    target_level: float
    risk_reward_ratio: str
    smc_pillar: str
    options_pillar: str
    timestamp: datetime = field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════════════════
# Four Pillars Engine
# ═══════════════════════════════════════════════════════════════════════════

class FourPillarsEngine:
    """
    Quantifies the macroeconomic and environmental backdrop across 4 uncorrelated axes:
    1. Volatility Regime (VIX)
    2. Institutional Net Positioning (FII/DII)
    3. Market Breadth (Adv/Dec)
    4. Global Cues (GIFT Nifty / Macro)
    """

    @staticmethod
    def evaluate(
        vix: float = 12.85,
        vix_chg_pct: float = -2.4,
        fii_net_crores: float = 850.0,
        dii_net_crores: float = 620.0,
        advances: int = 34,
        declines: int = 16,
        gift_nifty_pts: float = 45.0,
        spx_chg_pct: float = 0.65,
        dxy: float = 103.8
    ) -> FourPillarsReport:
        # 1. Volatility Regime
        # Low or falling VIX is bullish for equities; spike in VIX is bearish risk-off
        if vix_chg_pct <= -2.0 and vix < 14.0:
            v_score = 1.0
            v_regime = "COMPRESSING (RISK-ON)"
            v_color = "EMERALD"
            v_detail = f"VIX at {vix:.2f} ({vix_chg_pct:+.1f}%), IV crush favoring long gamma calls / option sellers"
        elif vix_chg_pct <= 0.0:
            v_score = 0.5
            v_regime = "BENIGN"
            v_color = "EMERALD"
            v_detail = f"VIX stable at {vix:.2f} ({vix_chg_pct:+.1f}%), orderly liquidity distribution"
        elif vix_chg_pct > 5.0 or vix > 18.0:
            v_score = -1.0
            v_regime = "SPIKING (RISK-OFF)"
            v_color = "ROSE"
            v_detail = f"VIX expansion {vix:.2f} ({vix_chg_pct:+.1f}%), high dealer put hedging pressure"
        else:
            v_score = -0.5
            v_regime = "ELEVATED"
            v_color = "AMBER"
            v_detail = f"VIX rising {vix:.2f} ({vix_chg_pct:+.1f}%), caution on naked directional exposure"

        # 2. Institutional Positioning (FII/DII Net Cash + Derivatives)
        total_inst_cr = fii_net_crores + dii_net_crores
        if total_inst_cr >= 1000.0:
            f_score = 1.0
            f_regime = "AGGRESSIVE INFLOWS"
            f_color = "EMERALD"
            f_detail = f"FII: {fii_net_crores:+.0f} Cr | DII: {dii_net_crores:+.0f} Cr. Heavy net institutional absorption."
        elif total_inst_cr > 0:
            f_score = 0.5
            f_regime = "MILD ACCUMULATION"
            f_color = "EMERALD"
            f_detail = f"FII: {fii_net_crores:+.0f} Cr | DII: {dii_net_crores:+.0f} Cr. Net positive flow floor."
        elif total_inst_cr <= -1000.0:
            f_score = -1.0
            f_regime = "AGGRESSIVE OUTFLOWS"
            f_color = "ROSE"
            f_detail = f"FII: {fii_net_crores:+.0f} Cr | DII: {dii_net_crores:+.0f} Cr. Heavy institutional liquidations."
        else:
            f_score = -0.5
            f_regime = "MILD DISTRIBUTION"
            f_color = "AMBER"
            f_detail = f"FII: {fii_net_crores:+.0f} Cr | DII: {dii_net_crores:+.0f} Cr. Negative institutional drag."

        # 3. Market Breadth (Advances vs Declines)
        ratio = advances / max(declines, 1)
        if ratio >= 2.0:
            b_score = 1.0
            b_regime = "BROAD EXPANSION"
            b_color = "EMERALD"
            b_detail = f"{advances} Advances / {declines} Declines ({ratio:.1f}:1 ratio). Broad-based rally participation."
        elif ratio > 1.1:
            b_score = 0.5
            b_regime = "POSITIVE SPREAD"
            b_color = "EMERALD"
            b_detail = f"{advances} Advances / {declines} Declines. Positive underlying participation."
        elif ratio <= 0.5:
            b_score = -1.0
            b_regime = "BROAD DETERIORATION"
            b_color = "ROSE"
            b_detail = f"{advances} Advances / {declines} Declines ({ratio:.1f}:1 ratio). Severe market internal selling."
        else:
            b_score = -0.4
            b_regime = "NEGATIVE SPREAD"
            b_color = "AMBER"
            b_detail = f"{advances} Advances / {declines} Declines. Defensive or selective rotation."

        # 4. Global Cues (GIFT Nifty, SPX, DXY)
        if gift_nifty_pts >= 40.0 and spx_chg_pct >= 0.3:
            g_score = 1.0
            g_regime = "STRONG TAILWINDS"
            g_color = "EMERALD"
            g_detail = f"GIFT Nifty {gift_nifty_pts:+.0f} pts | S&P {spx_chg_pct:+.2f}% | DXY {dxy:.1f}. Global risk-on tailwinds."
        elif gift_nifty_pts > 0:
            g_score = 0.4
            g_regime = "MILD TAILWINDS"
            g_color = "EMERALD"
            g_detail = f"GIFT Nifty {gift_nifty_pts:+.0f} pts. Stable overnight cues."
        elif gift_nifty_pts <= -40.0 or spx_chg_pct <= -0.5:
            g_score = -1.0
            g_regime = "HEAVY HEADWINDS"
            g_color = "ROSE"
            g_detail = f"GIFT Nifty {gift_nifty_pts:+.0f} pts | S&P {spx_chg_pct:+.2f}%. Overnight selling pressure."
        else:
            g_score = -0.3
            g_regime = "MUTED / CHOPPY"
            g_color = "SLATE"
            g_detail = f"GIFT Nifty {gift_nifty_pts:+.0f} pts. Mixed global handoff."

        pillars = [
            PillarScore(
                name="VIX_REGIME",
                title="VIX Volatility",
                raw_metric=f"{vix:.2f} ({vix_chg_pct:+.1f}%)",
                score=v_score,
                regime=v_regime,
                detail=v_detail,
                status_color=v_color
            ),
            PillarScore(
                name="FII_DII",
                title="Institutional Net",
                raw_metric=f"{total_inst_cr:+.0f} Cr",
                score=f_score,
                regime=f_regime,
                detail=f_detail,
                status_color=f_color
            ),
            PillarScore(
                name="MARKET_BREADTH",
                title="Market Breadth",
                raw_metric=f"{advances}A / {declines}D",
                score=b_score,
                regime=b_regime,
                detail=b_detail,
                status_color=b_color
            ),
            PillarScore(
                name="GLOBAL_CUES",
                title="Global Cues",
                raw_metric=f"{gift_nifty_pts:+.0f} pts",
                score=g_score,
                regime=g_regime,
                detail=g_detail,
                status_color=g_color
            )
        ]

        composite = round(sum(p.score for p in pillars), 2)

        # Composite Bias Classification & Playbook Directive
        if composite >= 2.5:
            bias_label = "EXTREME BULLISH"
            badge_variant = "EMERALD"
            directive = (
                "AGGRESSIVE MOMENTUM PLAYBOOK: Trend-following Call Spreads. Buy 15m Bullish Order Block / FVG pullbacks; "
                "Pyramid into breaks of PDH; Target upper Call Wall liquidity sweeps."
            )
        elif composite >= 1.0:
            bias_label = "MODERATE BULLISH"
            badge_variant = "EMERALD"
            directive = (
                "BUY-THE-DIP PLAYBOOK: Long Bull Put Spreads / Futures with trailing stops. Seek sniper entries at Discount Zone "
                "and Put Wall confluence; Avoid chasing breakouts at extreme Premium."
            )
        elif composite <= -2.5:
            bias_label = "EXTREME BEARISH"
            badge_variant = "ROSE"
            directive = (
                "SHORT EXPANSION PLAYBOOK: Bear Put Spreads / Short Futures. Fade rally pullbacks into Bearish Breakers "
                "and Call Walls; Target sell-side liquidity sweeps below Equal Lows."
            )
        elif composite <= -1.0:
            bias_label = "MODERATE BEARISH"
            badge_variant = "ROSE"
            directive = (
                "SELL-THE-RALLY PLAYBOOK: Bear Call Spreads. Short at 15m Premium Zone & Call writing walls; "
                "Take rapid partial profits at 50% Equilibrium."
            )
        else:
            bias_label = "NEUTRAL / CHOPPY"
            badge_variant = "SLATE"
            directive = (
                "MEAN-REVERSION PLAYBOOK: Non-directional Iron Condors / Short Strangles outside Gamma Sticky strikes. "
                "Fade range extremes; Execute SFP reversals at liquidity sweeps only."
            )

        return FourPillarsReport(
            pillars=pillars,
            composite_score=composite,
            bias_label=bias_label,
            badge_variant=badge_variant,
            algo_directive=directive
        )


# ═══════════════════════════════════════════════════════════════════════════
# OI Pillar Migration Engine
# ═══════════════════════════════════════════════════════════════════════════

class OIPillarMigrationEngine:
    """
    Computes intraday strike-level positioning migration:
    - Long Build-up (Price Up, OI Up)
    - Short Build-up (Price Down, OI Up)
    - Short Covering (Price Up, OI Down)
    - Long Unwinding (Price Down, OI Down)
    """

    @staticmethod
    def classify_side(ltp_chg: float, oi_chg: float, min_oi_threshold: float = 100.0) -> str:
        if abs(oi_chg) < min_oi_threshold:
            return "FLAT"
        if ltp_chg > 0 and oi_chg > 0:
            return "LONG BUILD-UP"
        elif ltp_chg < 0 and oi_chg > 0:
            return "SHORT BUILD-UP"
        elif ltp_chg > 0 and oi_chg < 0:
            return "SHORT COVERING"
        elif ltp_chg < 0 and oi_chg < 0:
            return "LONG UNWINDING"
        return "FLAT"

    @classmethod
    def parse_chain(cls, df: pd.DataFrame, spot: float, atm_strike: Optional[int] = None) -> list[StrikeOIClassification]:
        """
        Parses option chain DataFrame into tabular strike rows with classified migration states.
        Expected flexible column names:
          CE: ['ce_ltp', 'ce_chg', 'ce_oi', 'ce_oi_chg'] or standard NSE export.
        """
        results: list[StrikeOIClassification] = []
        if df is None or df.empty:
            return results

        # Normalize column mappings
        col_map = {c.lower().strip(): c for c in df.columns}
        
        # Determine ATM strike
        if atm_strike is None:
            atm_strike = int(round(spot / 50.0) * 50)

        for _, row in df.iterrows():
            strike = int(row.get("strike", row.get("STRIKE", 0)))
            if strike == 0:
                continue

            # CE fields
            ce_ltp = float(row.get("ce_ltp", row.get("ce_close", row.get("CE_LTP", 0.0))))
            ce_ltp_chg = float(row.get("ce_chg", row.get("ce_ltp_chg", row.get("CE_CHG", 0.0))))
            ce_oi = float(row.get("ce_oi", row.get("CE_OI", 0.0)))
            ce_oi_chg = float(row.get("ce_oi_chg", row.get("ce_chg_in_oi", row.get("CE_CHNG_IN_OI", 0.0))))
            ce_oi_pct = float(row.get("ce_oi_pct", 0.0))
            if ce_oi_pct == 0.0 and ce_oi > 0:
                ce_oi_pct = round((ce_oi_chg / max(ce_oi - ce_oi_chg, 1.0)) * 100.0, 1)

            ce_state = cls.classify_side(ce_ltp_chg, ce_oi_chg)

            # PE fields
            pe_ltp = float(row.get("pe_ltp", row.get("pe_close", row.get("PE_LTP", 0.0))))
            pe_ltp_chg = float(row.get("pe_chg", row.get("pe_ltp_chg", row.get("PE_CHG", 0.0))))
            pe_oi = float(row.get("pe_oi", row.get("PE_OI", 0.0)))
            pe_oi_chg = float(row.get("pe_oi_chg", row.get("pe_chg_in_oi", row.get("PE_CHNG_IN_OI", 0.0))))
            pe_oi_pct = float(row.get("pe_oi_pct", 0.0))
            if pe_oi_pct == 0.0 and pe_oi > 0:
                pe_oi_pct = round((pe_oi_chg / max(pe_oi - pe_oi_chg, 1.0)) * 100.0, 1)

            pe_state = cls.classify_side(pe_ltp_chg, pe_oi_chg)

            is_atm = (strike == atm_strike)
            dist = strike - spot

            results.append(StrikeOIClassification(
                strike=strike,
                ce_ltp=ce_ltp,
                ce_ltp_chg=ce_ltp_chg,
                ce_oi=ce_oi,
                ce_oi_chg=ce_oi_chg,
                ce_oi_pct=ce_oi_pct,
                ce_state=ce_state,
                pe_ltp=pe_ltp,
                pe_ltp_chg=pe_ltp_chg,
                pe_oi=pe_oi,
                pe_oi_chg=pe_oi_chg,
                pe_oi_pct=pe_oi_pct,
                pe_state=pe_state,
                is_atm=is_atm,
                dist_from_atm=dist
            ))

        results.sort(key=lambda x: x.strike)
        return results


# ═══════════════════════════════════════════════════════════════════════════
# Gamma Squeeze & Sticky Pin Engine
# ═══════════════════════════════════════════════════════════════════════════

class GammaSqueezeEngine:
    """
    Computes market-maker dealer gamma profile:
    - Sticky Zones: Positive Gamma strikes that pin the price
    - Squeeze Zones: Negative Gamma strikes that trigger explosive delta-hedging runs
    - Zero Gamma Flip: Price level where dealer exposure transitions from stabilizing to destabilizing
    """

    @staticmethod
    def calculate(strikes_oi: list[StrikeOIClassification], spot: float) -> GammaProfile:
        if not strikes_oi:
            return GammaProfile(
                spot_price=spot,
                zero_gamma_level=spot,
                net_gex_crores=0.0,
                current_regime="TRANSITION",
                dealer_position="LONG_GAMMA",
                sticky_strikes=[int(round(spot/50)*50)],
                squeeze_strikes=[],
                call_wall_strike=int(round(spot/50)*50 + 200),
                put_wall_strike=int(round(spot/50)*50 - 200),
                rationale="Insufficient chain depth for GEX derivation."
            )

        # Approximate Gamma Exposure (GEX) per strike:
        # Standard Black-Scholes approx: Dealer is Long Put OI (Short Gamma) & Short Call OI (Long Gamma)
        # Call GEX = Spot * Gamma * Call_OI * 100
        # Put GEX = -Spot * Gamma * Put_OI * 100
        total_gex = 0.0
        gex_by_strike: dict[int, float] = {}

        max_ce_oi = -1.0
        call_wall = strikes_oi[0].strike
        max_pe_oi = -1.0
        put_wall = strikes_oi[0].strike

        for s in strikes_oi:
            if s.ce_oi > max_ce_oi:
                max_ce_oi = s.ce_oi
                call_wall = s.strike
            if s.pe_oi > max_pe_oi:
                max_pe_oi = s.pe_oi
                put_wall = s.strike

            # Distance-decay gamma proxy
            dist = abs(s.strike - spot)
            gamma_weight = np.exp(-0.5 * (dist / 180.0) ** 2) / (180.0 * np.sqrt(2 * np.pi))

            # Calls add positive dealer gamma (dealers buy dips, sell rips)
            # Puts add negative dealer gamma (dealers sell dips, buy rips)
            strike_gex = (s.ce_oi - s.pe_oi) * gamma_weight * (spot ** 2) * 0.0001
            gex_by_strike[s.strike] = strike_gex
            total_gex += strike_gex

        # Determine Zero Gamma flip level
        sorted_strikes = sorted(gex_by_strike.keys())
        zero_gamma = spot
        for i in range(len(sorted_strikes) - 1):
            s1, s2 = sorted_strikes[i], sorted_strikes[i + 1]
            g1, g2 = gex_by_strike[s1], gex_by_strike[s2]
            if (g1 <= 0 and g2 > 0) or (g1 >= 0 and g2 < 0):
                zero_gamma = float((s1 + s2) / 2.0)
                break

        # Sticky strikes: Heavy OI clusters where spot is near
        sticky_strikes = [s.strike for s in strikes_oi if abs(s.strike - spot) <= 250 and (s.ce_oi + s.pe_oi) >= 0.7 * (max_ce_oi + max_pe_oi)]
        if not sticky_strikes:
            sticky_strikes = [call_wall, put_wall]

        # Squeeze strikes: Strikes outside sticky walls where delta-hedging causes acceleration
        squeeze_strikes = [s.strike for s in strikes_oi if s.strike >= call_wall or s.strike <= put_wall]

        dealer_pos = "LONG_GAMMA" if total_gex >= 0 else "SHORT_GAMMA"
        if dealer_pos == "LONG_GAMMA":
            regime = "STICKY_PIN_ZONE"
            rationale = (
                f"Dealers in positive gamma (+{abs(total_gex):,.0f} Cr GEX). Dynamic hedging acts as a volatility damper, "
                f"pinning price between Put Wall {put_wall} and Call Wall {call_wall}."
            )
        else:
            regime = "GAMMA_SQUEEZE_ZONE"
            rationale = (
                f"Dealers in short gamma (-{abs(total_gex):,.0f} Cr GEX). Spot beyond Zero Gamma {zero_gamma:.0f}. "
                f"Market makers forced to hedge in direction of move; expect explosive breakout acceleration."
            )

        return GammaProfile(
            spot_price=spot,
            zero_gamma_level=round(zero_gamma, 1),
            net_gex_crores=round(total_gex, 1),
            current_regime=regime,
            dealer_position=dealer_pos,
            sticky_strikes=sticky_strikes,
            squeeze_strikes=squeeze_strikes,
            call_wall_strike=call_wall,
            put_wall_strike=put_wall,
            rationale=rationale
        )


# ═══════════════════════════════════════════════════════════════════════════
# Institutional Zone Matrix & Convergence Engine
# ═══════════════════════════════════════════════════════════════════════════

class ZoneMatrixEngine:
    """
    Unifies Options S/R walls with Smart Money Concepts liquidity pools & order blocks.
    Evaluates real-time health: CONFIRMED vs. CONTESTED vs. WEAKENING.
    """

    @staticmethod
    def build_matrix(
        spot: float,
        strikes_oi: list[StrikeOIClassification],
        smc_res: dict,
        gamma: GammaProfile
    ) -> list[ZoneMatrixRow]:
        rows: list[ZoneMatrixRow] = []
        if not strikes_oi:
            return rows

        # Identify key option barriers
        call_wall = gamma.call_wall_strike
        put_wall = gamma.put_wall_strike
        atm = int(round(spot / 50.0) * 50)

        # Collect SMC objects
        obs = smc_res.get("order_blocks", [])
        breakers = smc_res.get("breaker_blocks", [])
        pools = smc_res.get("liquidity_pools", [])
        fvgs = smc_res.get("active_fvgs", [])

        # 1. Call Wall Resistance Zone
        cw_row = next((s for s in strikes_oi if s.strike == call_wall), None)
        if cw_row:
            # Check if any Bearish OB or Breaker or EQH sits near Call Wall
            matching_smc = []
            for ob in obs:
                if ob.direction == "BEARISH" and abs(ob.mean_threshold - call_wall) <= 60:
                    matching_smc.append(f"Bearish Supply OB @ {ob.bottom:,.0f}-{ob.top:,.0f}")
            for bb in breakers:
                if bb.direction == "BULLISH" and abs(bb.mean_threshold - call_wall) <= 60:
                    matching_smc.append(f"Bearish Breaker Block @ {bb.mean_threshold:,.0f}")
            for p in pools:
                if p.pool_type == "EQH" and abs(p.level - call_wall) <= 70:
                    matching_smc.append(f"Resting EQH Liquidity Pool @ {p.level:,.0f}")

            smc_str = " + ".join(matching_smc) if matching_smc else "Unconfirmed by major SMC blocks"

            # Determine alignment status
            if cw_row.ce_state == "SHORT BUILD-UP":
                alignment = "CONFIRMED"
                align_reason = "Heavy call writing defends the ceiling (+OI build-up corroborates SMC supply)."
            elif cw_row.ce_state == "LONG UNWINDING":
                alignment = "WEAKENING"
                align_reason = "Call buyers exiting; ceiling remains untested but losing institutional backing."
            elif cw_row.ce_state == "SHORT COVERING":
                alignment = "WEAKENING"
                align_reason = "Shorts covering rapidly; high risk of upward breakout / liquidity sweep."
            else:
                alignment = "CONTESTED"
                align_reason = "Two-way volume without decisive open interest dominance."

            rows.append(ZoneMatrixRow(
                level=float(call_wall),
                label=f"R1: Heavy Call Wall ({call_wall})",
                zone_type="RESISTANCE",
                oi_evidence=f"{cw_row.ce_oi/1e5:.1f}L CE OI ({cw_row.ce_oi_pct:+.1f}% ΔOI, {cw_row.ce_state})",
                smc_evidence=smc_str,
                alignment=alignment,
                alignment_reason=align_reason,
                distance_pts=call_wall - spot,
                is_immediate=abs(call_wall - spot) <= 100
            ))

        # 2. Put Wall Support Zone
        pw_row = next((s for s in strikes_oi if s.strike == put_wall), None)
        if pw_row:
            matching_smc = []
            for ob in obs:
                if ob.direction == "BULLISH" and abs(ob.mean_threshold - put_wall) <= 60:
                    matching_smc.append(f"Bullish Demand OB @ {ob.bottom:,.0f}-{ob.top:,.0f}")
            for p in pools:
                if p.pool_type == "EQL" and abs(p.level - put_wall) <= 70:
                    matching_smc.append(f"Resting EQL Liquidity Pool @ {p.level:,.0f}")
            for fvg in fvgs:
                if fvg.direction == "BULLISH" and abs(fvg.consequent_encroachment - put_wall) <= 50:
                    matching_smc.append(f"Bullish FVG @ {fvg.consequent_encroachment:,.0f}")

            smc_str = " + ".join(matching_smc) if matching_smc else "Unconfirmed by major SMC blocks"

            if pw_row.pe_state == "SHORT BUILD-UP":
                alignment = "CONFIRMED"
                align_reason = "Heavy put writing establishes structural floor (+PE OI build-up backs SMC demand)."
            elif pw_row.pe_state == "LONG UNWINDING":
                alignment = "WEAKENING"
                align_reason = "Put buyers closing; floor softening."
            elif pw_row.pe_state == "SHORT COVERING":
                alignment = "WEAKENING"
                align_reason = "Put writers fleeing; warning of potential floor collapse / breakdown."
            else:
                alignment = "CONTESTED"
                align_reason = "Equilibrium flow at put wall."

            rows.append(ZoneMatrixRow(
                level=float(put_wall),
                label=f"S1: Heavy Put Wall ({put_wall})",
                zone_type="SUPPORT",
                oi_evidence=f"{pw_row.pe_oi/1e5:.1f}L PE OI ({pw_row.pe_oi_pct:+.1f}% ΔOI, {pw_row.pe_state})",
                smc_evidence=smc_str,
                alignment=alignment,
                alignment_reason=align_reason,
                distance_pts=spot - put_wall,
                is_immediate=abs(spot - put_wall) <= 100
            ))

        # 3. Liquidity Pools from SMC (EQH / EQL)
        for p in pools:
            if p.is_swept:
                continue
            # Near strike comparison
            nearest_s = int(round(p.level / 50.0) * 50)
            s_data = next((s for s in strikes_oi if s.strike == nearest_s), None)
            oi_txt = "Neutral flow at nearest strike"
            status = "CONTESTED"
            reason = "Resting stop-loss cluster waiting for institutional sweep."

            if s_data:
                if p.pool_type == "EQH":
                    oi_txt = f"{nearest_s} CE: {s_data.ce_state} ({s_data.ce_oi_pct:+.1f}%)"
                    if "SHORT COVERING" in s_data.ce_state:
                        status = "WEAKENING"
                        reason = "Call writers bailing; EQH is prime stop-hunt magnet."
                    elif "SHORT BUILD-UP" in s_data.ce_state:
                        status = "CONFIRMED"
                        reason = "Call writers aggressively placing walls in front of EQH."
                else:
                    oi_txt = f"{nearest_s} PE: {s_data.pe_state} ({s_data.pe_oi_pct:+.1f}%)"
                    if "SHORT COVERING" in s_data.pe_state:
                        status = "WEAKENING"
                        reason = "Put writers fleeing; EQL is prime sell-side stop-hunt target."
                    elif "SHORT BUILD-UP" in s_data.pe_state:
                        status = "CONFIRMED"
                        reason = "Put writers holding line above EQL."

            rows.append(ZoneMatrixRow(
                level=float(p.level),
                label=f"{p.pool_type} Resting Liquidity ({p.touch_count}-Touch)",
                zone_type="MAGNET",
                oi_evidence=oi_txt,
                smc_evidence=f"{p.pool_type} span [{p.span_low:,.1f} - {p.span_high:,.1f}]",
                alignment=status,
                alignment_reason=reason,
                distance_pts=abs(spot - p.level),
                is_immediate=abs(spot - p.level) <= 80
            ))

        # Sort matrix by price descending
        rows.sort(key=lambda r: r.level, reverse=True)
        return rows


# ═══════════════════════════════════════════════════════════════════════════
# Active Setups Feed Generator (SMC + OI Convergence)
# ═══════════════════════════════════════════════════════════════════════════

class ConvergenceSetupEngine:
    """
    Generates actionable institutional setup execution cards converging SMC and Options Flow.
    """

    @staticmethod
    def generate_feed(
        smc_res: dict,
        strikes_oi: list[StrikeOIClassification],
        four_pillars: FourPillarsReport,
        gamma: GammaProfile,
        spot: float
    ) -> list[ActiveSetupCard]:
        cards: list[ActiveSetupCard] = []

        combos = smc_res.get("dragon_fruit_combos", [])
        msb_list = smc_res.get("msb_list", [])
        sfps = smc_res.get("sfps", [])
        dealing_range = smc_res.get("dealing_range")

        # 1. Dragon Fruit Combo Setup
        for c in combos:
            entry_strike = int(round(c.mean_sniper_entry / 50.0) * 50)
            strike_row = next((s for s in strikes_oi if s.strike == entry_strike), None)

            if c.direction == "BEARISH":
                ce_info = f"{entry_strike} Call {strike_row.ce_state if strike_row else 'Pressure'}"
                regime_txt = "Premium Zone" if (dealing_range and c.mean_sniper_entry >= dealing_range.equilibrium) else "Equilibrium Zone"
                logic = f"15m Imbalance overlaps with {c.ob.direction} OB and {ce_info}. {regime_txt}."
                action = "Look for SFP (Swing Failure) on 1m/5m chart for short entry. Limit order at Mean Threshold."
                stop = c.overlap_high + 15.0
                target = dealing_range.low if dealing_range else spot - 150.0
                rr = f"1:{max(2.5, abs(c.mean_sniper_entry - target)/max(abs(c.mean_sniper_entry - stop), 1.0)):.1f}"

                cards.append(ActiveSetupCard(
                    setup_name="Dragon Fruit Combo Detected",
                    direction="BEARISH",
                    timeframe="15m",
                    conviction="TIER 1 SNIPER",
                    logic=logic,
                    action=action,
                    trigger_level=c.mean_sniper_entry,
                    stop_loss=stop,
                    target_level=target,
                    risk_reward_ratio=rr,
                    smc_pillar="15m FVG + Bearish OB Overlap",
                    options_pillar=f"Strike {entry_strike} Migration Confluence"
                ))
            else:
                pe_info = f"{entry_strike} Put {strike_row.pe_state if strike_row else 'Support'}"
                regime_txt = "Discount Zone" if (dealing_range and c.mean_sniper_entry <= dealing_range.equilibrium) else "Equilibrium Zone"
                logic = f"15m Imbalance overlaps with {c.ob.direction} Demand OB and {pe_info}. {regime_txt}."
                action = "Look for SFP or micro-MSB on 1m chart for sniper long entry. Limit order at 50% CE."
                stop = c.overlap_low - 15.0
                target = dealing_range.high if dealing_range else spot + 150.0
                rr = f"1:{max(2.5, abs(target - c.mean_sniper_entry)/max(abs(c.mean_sniper_entry - stop), 1.0)):.1f}"

                cards.append(ActiveSetupCard(
                    setup_name="Dragon Fruit Combo Detected",
                    direction="BULLISH",
                    timeframe="15m",
                    conviction="TIER 1 SNIPER",
                    logic=logic,
                    action=action,
                    trigger_level=c.mean_sniper_entry,
                    stop_loss=stop,
                    target_level=target,
                    risk_reward_ratio=rr,
                    smc_pillar="15m FVG + Bullish OB Overlap",
                    options_pillar=f"Strike {entry_strike} Migration Confluence"
                ))

        # 2. Swing Failure Pattern (SFP) Reversals
        for sfp in sfps[-3:]:
            target_strike = int(round(sfp.level_swept / 50.0) * 50)
            if sfp.direction == "BEARISH":
                logic = f"Liquidity Sweep above {sfp.level_swept:,.1f} ({sfp.pool_type}). Long upper shadow failed to close."
                action = "Initiate short fade on candle close below swept level. Target liquidity pool at range low."
                stop = sfp.extreme_wick_price + 8.0
                target = sfp.level_swept - 120.0
                rr = f"1:{abs(sfp.close_price - target)/max(abs(stop - sfp.close_price), 1.0):.1f}"

                cards.append(ActiveSetupCard(
                    setup_name="SFP Liquidity Sweep (Short Fade)",
                    direction="BEARISH",
                    timeframe="15m",
                    conviction="HIGH PROBABILITY",
                    logic=logic,
                    action=action,
                    trigger_level=sfp.close_price,
                    stop_loss=stop,
                    target_level=target,
                    risk_reward_ratio=rr,
                    smc_pillar=f"Swept {sfp.pool_type} @ {sfp.level_swept:,.0f}",
                    options_pillar=f"Gamma Squeeze rejection outside {gamma.call_wall_strike} Wall"
                ))
            else:
                logic = f"Liquidity Sweep below {sfp.level_swept:,.1f} ({sfp.pool_type}). Deep wick hunt with strong close above."
                action = "Initiate long reversal on candle close back above swept level. Target 50% Equilibrium."
                stop = sfp.extreme_wick_price - 8.0
                target = sfp.level_swept + 120.0
                rr = f"1:{abs(target - sfp.close_price)/max(abs(sfp.close_price - stop), 1.0):.1f}"

                cards.append(ActiveSetupCard(
                    setup_name="SFP Liquidity Sweep (Long Reversal)",
                    direction="BULLISH",
                    timeframe="15m",
                    conviction="HIGH PROBABILITY",
                    logic=logic,
                    action=action,
                    trigger_level=sfp.close_price,
                    stop_loss=stop,
                    target_level=target,
                    risk_reward_ratio=rr,
                    smc_pillar=f"Swept {sfp.pool_type} @ {sfp.level_swept:,.0f}",
                    options_pillar=f"Sticky Put Wall {gamma.put_wall_strike} absorption"
                ))

        # 3. Market Structure Break (Strong MSB) Follow-Through
        for msb in msb_list[-2:]:
            if msb.strength == "STRONG":
                target_strike = int(round(msb.level_price / 50.0) * 50)
                if msb.direction == "BULLISH":
                    logic = f"Strong Bullish MSB closed above LH {msb.level_price:,.1f}. Composite Bias: {four_pillars.bias_label} ({four_pillars.composite_score:+0.1f})."
                    action = f"Wait for retracement into Entry Zone [{msb.entry_zone_low:,.0f} - {msb.entry_zone_high:,.0f}]. Enter on 1m micro-break."
                    stop = msb.extreme_price - 10.0
                    target = msb.level_price + 180.0
                    rr = f"1:{abs(target - msb.close_price)/max(abs(msb.close_price - stop), 1.0):.1f}"

                    cards.append(ActiveSetupCard(
                        setup_name="Strong MSB Trend Continuation",
                        direction="BULLISH",
                        timeframe="15m",
                        conviction="HIGH PROBABILITY",
                        logic=logic,
                        action=action,
                        trigger_level=msb.level_price,
                        stop_loss=stop,
                        target_level=target,
                        risk_reward_ratio=rr,
                        smc_pillar="Full-bodied Candle Close above LH",
                        options_pillar=f"Composite Score {four_pillars.composite_score:+0.1f}/4.0"
                    ))
                else:
                    logic = f"Strong Bearish MSB closed below HL {msb.level_price:,.1f}. Composite Bias: {four_pillars.bias_label} ({four_pillars.composite_score:+0.1f})."
                    action = f"Wait for pullback to Entry Zone [{msb.entry_zone_low:,.0f} - {msb.entry_zone_high:,.0f}]. Short on 1m micro-break."
                    stop = msb.extreme_price + 10.0
                    target = msb.level_price - 180.0
                    rr = f"1:{abs(msb.close_price - target)/max(abs(stop - msb.close_price), 1.0):.1f}"

                    cards.append(ActiveSetupCard(
                        setup_name="Strong MSB Trend Continuation",
                        direction="BEARISH",
                        timeframe="15m",
                        conviction="HIGH PROBABILITY",
                        logic=logic,
                        action=action,
                        trigger_level=msb.level_price,
                        stop_loss=stop,
                        target_level=target,
                        risk_reward_ratio=rr,
                        smc_pillar="Full-bodied Candle Close below HL",
                        options_pillar=f"Composite Score {four_pillars.composite_score:+0.1f}/4.0"
                    ))

        # Fallback tactical card if no high-conviction setup active
        if not cards:
            cards.append(ActiveSetupCard(
                setup_name="Gamma Range Mean Reversion",
                direction="NEUTRAL",
                timeframe="15m",
                conviction="TACTICAL",
                logic=f"Spot pinned between Call Wall {gamma.call_wall_strike} and Put Wall {gamma.put_wall_strike}. Dealer Gamma Positive.",
                action="Deploy delta-neutral credit spreads (Iron Condors); fade liquidity sweeps at outer walls.",
                trigger_level=spot,
                stop_loss=gamma.call_wall_strike + 50.0,
                target_level=gamma.zero_gamma_level,
                risk_reward_ratio="1:2.0",
                smc_pillar="Equilibrium Range Bound",
                options_pillar=f"Dealer GEX +{abs(gamma.net_gex_crores):,.0f} Cr Pin"
            ))

        return cards
