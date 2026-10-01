"""
engine/terminal_engine.py — Unified Trading Terminal Quant Engine.

Coordinates the Advanced SMC Engine and Options Flow & Confluence Engine.
Provides complete, institutional data feeds for both:
  1. High-performance Streamlit terminal dashboard
  2. Production React / Next.js Tailwind terminal components
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from engine.smc_engine import (
    AdvancedSMCEngine,
    DealingRange,
    DragonFruitCombo,
    FairValueGap,
    LiquidityPool,
    MarketStructureBreak,
    OrderBlock,
    SwingFailurePattern,
    SwingPoint,
)
from engine.options_flow_engine import (
    ActiveSetupCard,
    ConvergenceSetupEngine,
    FourPillarsEngine,
    FourPillarsReport,
    GammaProfile,
    GammaSqueezeEngine,
    OIPillarMigrationEngine,
    StrikeOIClassification,
    ZoneMatrixEngine,
    ZoneMatrixRow,
)


@dataclass
class TerminalSnapshot:
    spot_price: float
    timestamp: datetime
    four_pillars: FourPillarsReport
    gamma_profile: GammaProfile
    divergent_oi_chart: list[dict]
    strike_evidence_table: list[StrikeOIClassification]
    active_setups: list[ActiveSetupCard]
    zone_matrix: list[ZoneMatrixRow]
    smc_data: dict
    dealing_range: Optional[DealingRange]
    dragon_fruit_combos: list[DragonFruitCombo]

    def to_dict(self) -> dict:
        """Converts complete snapshot into clean JSON-serializable dictionary."""
        def _serialize(obj):
            if isinstance(obj, (datetime, pd.Timestamp)):
                return obj.isoformat()
            if hasattr(obj, "__dict__"):
                return {k: _serialize(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
            if isinstance(obj, list):
                return [_serialize(x) for x in obj]
            if isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            if isinstance(obj, (np.int64, np.int32)):
                return int(obj)
            if isinstance(obj, (np.float64, np.float32)):
                return float(obj)
            return obj

        return _serialize(self)


class UnifiedTerminalEngine:
    """
    Unified high-performance engine powering the institutional trading terminal.
    """

    def __init__(self, pivot_window: int = 2):
        self.smc_engine = AdvancedSMCEngine(pivot_window=pivot_window)

    def process(
        self,
        candle_df: pd.DataFrame,
        chain_df: Optional[pd.DataFrame] = None,
        spot: Optional[float] = None,
        vix: float = 12.85,
        vix_chg_pct: float = -2.4,
        fii_net_crores: float = 850.0,
        dii_net_crores: float = 620.0,
        advances: int = 34,
        declines: int = 16,
        gift_nifty_pts: float = 45.0,
        spx_chg_pct: float = 0.65,
        dxy: float = 103.8
    ) -> TerminalSnapshot:
        # 1. Resolve Spot Price
        if spot is None:
            if candle_df is not None and not candle_df.empty:
                spot = float(candle_df["close"].iloc[-1])
            else:
                spot = 24150.0

        # 2. Execute Advanced SMC Engine
        smc_res = self.smc_engine.analyze(candle_df)
        dealing_range = smc_res.get("dealing_range")
        dragon_fruit_combos = smc_res.get("dragon_fruit_combos", [])

        # 3. Parse & Classify Option Chain (OI Pillar Migration)
        if chain_df is None or chain_df.empty:
            chain_df = self._generate_mock_chain(spot)

        atm_strike = int(round(spot / 50.0) * 50)
        strike_classifications = OIPillarMigrationEngine.parse_chain(chain_df, spot, atm_strike=atm_strike)

        # 4. Calculate Gamma Squeeze & Sticky Pin Zones
        gamma_profile = GammaSqueezeEngine.calculate(strike_classifications, spot)

        # 5. Evaluate Four Pillars Scorecard & Composite Bias
        four_pillars = FourPillarsEngine.evaluate(
            vix=vix,
            vix_chg_pct=vix_chg_pct,
            fii_net_crores=fii_net_crores,
            dii_net_crores=dii_net_crores,
            advances=advances,
            declines=declines,
            gift_nifty_pts=gift_nifty_pts,
            spx_chg_pct=spx_chg_pct,
            dxy=dxy
        )

        # 6. Build Zone Matrix (Liquidity Pools + Option S/R Walls)
        zone_matrix = ZoneMatrixEngine.build_matrix(
            spot=spot,
            strikes_oi=strike_classifications,
            smc_res=smc_res,
            gamma=gamma_profile
        )

        # 7. Generate Active Setups Feed (SMC + OI Convergence)
        active_setups = ConvergenceSetupEngine.generate_feed(
            smc_res=smc_res,
            strikes_oi=strike_classifications,
            four_pillars=four_pillars,
            gamma=gamma_profile,
            spot=spot
        )

        # 8. Prepare Divergent OI Bar Chart Data (ATM Centered)
        divergent_chart_data = self._build_divergent_oi_chart_data(strike_classifications, atm_strike)

        # Return comprehensive snapshot
        ts = candle_df["timestamp"].iloc[-1] if (candle_df is not None and not candle_df.empty and "timestamp" in candle_df.columns) else datetime.now()

        return TerminalSnapshot(
            spot_price=round(spot, 1),
            timestamp=pd.to_datetime(ts),
            four_pillars=four_pillars,
            gamma_profile=gamma_profile,
            divergent_oi_chart=divergent_chart_data,
            strike_evidence_table=strike_classifications,
            active_setups=active_setups,
            zone_matrix=zone_matrix,
            smc_data=smc_res,
            dealing_range=dealing_range,
            dragon_fruit_combos=dragon_fruit_combos
        )

    def _build_divergent_oi_chart_data(
        self,
        strikes: list[StrikeOIClassification],
        atm_strike: int,
        window: int = 10
    ) -> list[dict]:
        """
        Creates divergent horizontal bar chart data centered around ATM strike.
        Call OI Change -> Negative/Left red bars
        Put OI Change  -> Positive/Right green bars
        """
        # Filter window around ATM
        subset = [s for s in strikes if abs(s.strike - atm_strike) <= (window * 50)]
        chart_rows = []
        for s in subset:
            chart_rows.append({
                "strike": s.strike,
                "is_atm": s.is_atm,
                "call_oi_chg_raw": s.ce_oi_chg,
                "call_oi_chg_left": -abs(s.ce_oi_chg) if s.ce_oi_chg > 0 else abs(s.ce_oi_chg),
                "put_oi_chg_raw": s.pe_oi_chg,
                "put_oi_chg_right": abs(s.pe_oi_chg) if s.pe_oi_chg > 0 else -abs(s.pe_oi_chg),
                "ce_state": s.ce_state,
                "pe_state": s.pe_state,
                "ce_oi": s.ce_oi,
                "pe_oi": s.pe_oi,
            })
        return chart_rows

    def _generate_mock_chain(self, spot: float) -> pd.DataFrame:
        """Generates realistic institutional option chain around spot."""
        atm = int(round(spot / 50.0) * 50)
        rows = []
        for step in range(-12, 13):
            strike = atm + step * 50
            dist = strike - spot

            # Call LTP & OI decay
            ce_ltp = max(5.0, spot - strike + 120.0 * np.exp(-abs(dist)/250.0))
            ce_oi = int(50000 + 450000 * np.exp(-((strike - (atm + 150)) / 220.0) ** 2))
            ce_chg = int(25000 * np.sin(step / 3.0) + (15000 if strike >= atm else -10000))
            ce_ltp_chg = round(0.8 * (-dist / 100.0), 2)

            # Put LTP & OI decay
            pe_ltp = max(5.0, strike - spot + 120.0 * np.exp(-abs(dist)/250.0))
            pe_oi = int(45000 + 520000 * np.exp(-((strike - (atm - 150)) / 220.0) ** 2))
            pe_chg = int(32000 * np.cos(step / 3.0) + (22000 if strike <= atm else -8000))
            pe_ltp_chg = round(0.8 * (dist / 100.0), 2)

            rows.append({
                "strike": strike,
                "ce_ltp": round(ce_ltp, 2),
                "ce_chg": ce_ltp_chg,
                "ce_oi": ce_oi,
                "ce_oi_chg": ce_chg,
                "pe_ltp": round(pe_ltp, 2),
                "pe_chg": pe_ltp_chg,
                "pe_oi": pe_oi,
                "pe_oi_chg": pe_chg,
            })

        return pd.DataFrame(rows)
