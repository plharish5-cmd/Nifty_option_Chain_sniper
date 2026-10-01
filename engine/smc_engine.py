"""
engine/smc_engine.py — Advanced Smart Money Concepts (SMC) & Price Action Engine.

Strict institutional implementation of:
  1. Market Structure Breaks (MSB):
     - Strong MSB: Full-bodied candle close breaking the swing level (LH in downtrend or HL in uptrend).
     - Weak MSB: Wick-only pierce or sluggish range breakout without clean candle closes.
     - Dynamic Entry Zone: [Broken Swing Level <-> Deepest Extreme Pivot].
  2. Imbalances (Fair Value Gaps - FVG):
     - 3-candle Fair Value Gap detection (Bullish & Bearish).
     - Multi-state tracking: UNFILLED, PARTIALLY_FILLED (50% Consequent Encroachment / CE), MITIGATED.
     - Gap as primary magnet and liquidity target.
  3. Order Blocks (OB) & Breaker Blocks:
     - Identification of last opposite-color candle preceding impulsive MSB/BOS.
     - 50% Mean Threshold (midpoint) computation for sniper entries.
     - Automatic state flip to "Breaker Block" upon clean violation (Support <-> Resistance flip).
  4. Swing Failure Patterns (SFP) & Liquidity Sweeps:
     - Piercing of significant swing high/low with wick failing to close beyond (rejection / stop hunt).
     - Detection of Equal Highs (EQH) and Equal Lows (EQL) liquidity pools.
  5. Dealing Range & Premium / Discount Analysis:
     - 50% Equilibrium (EQ) calculation of the active swing range.
     - Premium Zone (>50%) for high-probability shorts, Discount Zone (<50%) for longs.
     - Optimal Trade Entry (OTE): Fibonacci 0.705 - 0.790 golden retracement zone.
  6. Dragon Fruit Combo:
     - Confluence where an Imbalance (FVG) and Order Block overlap or sit side-by-side in the Entry Zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════════════════
# Core Data Models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SwingPoint:
    index: int
    timestamp: datetime
    price: float
    kind: Literal["HIGH", "LOW"]
    subkind: Literal["HH", "LH", "HL", "LL", ""] = ""
    confirmed_bar: int = 0
    violated_bar: Optional[int] = None

    def is_active(self, current_bar: int) -> bool:
        if current_bar < self.confirmed_bar:
            return False
        return self.violated_bar is None or current_bar <= self.violated_bar


@dataclass
class MarketStructureBreak:
    break_id: str
    direction: Literal["BULLISH", "BEARISH"]
    strength: Literal["STRONG", "WEAK"]
    level_price: float
    extreme_price: float
    break_bar: int
    break_time: datetime
    close_price: float
    entry_zone_low: float
    entry_zone_high: float
    quality_score: int          # 1 to 7 based on institutional quality checkpoints
    quality_factors: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class FairValueGap:
    fvg_id: str
    direction: Literal["BULLISH", "BEARISH"]
    top: float
    bottom: float
    consequent_encroachment: float   # 50% CE level
    bar_index: int                   # middle candle index
    timestamp: datetime
    status: Literal["UNFILLED", "PARTIALLY_FILLED", "MITIGATED"] = "UNFILLED"
    fill_percentage: float = 0.0
    mitigated_bar: Optional[int] = None
    mitigated_time: Optional[datetime] = None

    @property
    def height(self) -> float:
        return abs(self.top - self.bottom)


@dataclass
class OrderBlock:
    ob_id: str
    direction: Literal["BULLISH", "BEARISH"]   # BULLISH = Demand OB, BEARISH = Supply OB
    top: float
    bottom: float
    mean_threshold: float                      # 50% midpoint
    bar_index: int
    timestamp: datetime
    state: Literal["ACTIVE", "MITIGATED", "BREAKER"] = "ACTIVE"
    breaker_bar: Optional[int] = None
    breaker_time: Optional[datetime] = None
    msb_ref_id: str = ""
    quality_score: int = 5
    volume_ratio: float = 1.0


@dataclass
class SwingFailurePattern:
    sfp_id: str
    direction: Literal["BULLISH", "BEARISH"]   # BULLISH = Swept low & bounced, BEARISH = Swept high & rejected
    level_swept: float
    extreme_wick_price: float
    close_price: float
    bar_index: int
    timestamp: datetime
    pool_type: Literal["EQH", "EQL", "MAJOR_SWING_HIGH", "MAJOR_SWING_LOW"]
    rationale: str = ""


@dataclass
class LiquidityPool:
    pool_id: str
    pool_type: Literal["EQH", "EQL"]
    level: float
    span_low: float
    span_high: float
    touch_count: int
    bar_indices: list[int]
    is_swept: bool = False
    swept_by_bar: Optional[int] = None


@dataclass
class DealingRange:
    high: float
    low: float
    equilibrium: float          # 50% EQ
    ote_705: float              # 70.5% retracement
    ote_790: float              # 79.0% retracement
    current_price: float
    current_regime: Literal["PREMIUM", "DISCOUNT", "EQUILIBRIUM"]
    discount_depth_pct: float   # 0 to 100% position within range


@dataclass
class DragonFruitCombo:
    combo_id: str
    direction: Literal["BULLISH", "BEARISH"]
    ob: OrderBlock
    fvg: FairValueGap
    overlap_low: float
    overlap_high: float
    mean_sniper_entry: float
    entry_zone: tuple[float, float]
    quality_rank: str = "TIER_1_INSTITUTIONAL"
    narrative: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Advanced Price Action Engine Implementation
# ═══════════════════════════════════════════════════════════════════════════

class AdvancedSMCEngine:
    """
    State-of-the-art SMC Engine for high-frequency institutional setups.
    Parses OHLCV timeseries with zero lookahead bias and extracts verified
    institutional price structures.
    """

    def __init__(self, pivot_window: int = 2, eq_tolerance_pct: float = 0.08):
        self.pivot_window = pivot_window
        self.eq_tolerance_pct = eq_tolerance_pct

    def analyze(self, df: pd.DataFrame) -> dict:
        """
        Executes full SMC analysis across the provided DataFrame.
        Expected columns: ['timestamp'/'date', 'open', 'high', 'low', 'close', 'volume']
        """
        if df is None or len(df) < 5:
            return {
                "swings": [],
                "msb_list": [],
                "active_msb": None,
                "fvgs": [],
                "active_fvgs": [],
                "order_blocks": [],
                "breaker_blocks": [],
                "sfps": [],
                "liquidity_pools": [],
                "dealing_range": None,
                "dragon_fruit_combos": []
            }

        df = df.copy()
        if "timestamp" not in df.columns:
            if "date" in df.columns:
                df["timestamp"] = pd.to_datetime(df["date"])
            else:
                df["timestamp"] = pd.to_datetime(df.index)
        else:
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "volume" not in df.columns:
            df["volume"] = 1.0
        else:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(1.0)

        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        n_bars = len(df)

        # 1. Swing Pivots Detection
        swings = self._detect_swings(df)

        # 2. Liquidity Pools (EQH / EQL)
        pools = self._detect_liquidity_pools(df, swings)

        # 3. Fair Value Gaps (FVG) with Real-time Mitigation Tracking
        fvgs = self._detect_fvgs(df)

        # 4. Market Structure Breaks (MSB) — Strong vs Weak
        msbs = self._detect_msbs(df, swings)

        # 5. Order Blocks (OB) & Breaker Blocks (State-Flipping Engine)
        obs, breakers = self._detect_obs_and_breakers(df, msbs)

        # 6. Swing Failure Patterns (SFP) / Liquidity Sweeps
        sfps = self._detect_sfps(df, swings, pools)

        # 7. Dealing Range (50% Equilibrium & OTE Golden Zone)
        dealing_range = self._calculate_dealing_range(df, swings)

        # 8. Dragon Fruit Combo (FVG + OB side-by-side or overlapping)
        combos = self._detect_dragon_fruit_combos(obs, fvgs, msbs, dealing_range)

        # Active elements
        active_fvgs = [f for f in fvgs if f.status != "MITIGATED"]
        active_obs = [ob for ob in obs if ob.state == "ACTIVE"]
        active_msb = msbs[-1] if msbs else None

        return {
            "df": df,
            "swings": swings,
            "msb_list": msbs,
            "active_msb": active_msb,
            "fvgs": fvgs,
            "active_fvgs": active_fvgs,
            "order_blocks": obs,
            "active_obs": active_obs,
            "breaker_blocks": breakers,
            "sfps": sfps,
            "liquidity_pools": pools,
            "dealing_range": dealing_range,
            "dragon_fruit_combos": combos,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Swing Points Detection (Strict N-bar confirmed lag)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_swings(self, df: pd.DataFrame) -> list[SwingPoint]:
        n = self.pivot_window
        swings: list[SwingPoint] = []
        n_bars = len(df)

        highs = df["high"].values
        lows = df["low"].values
        ts = df["timestamp"].tolist()

        for i in range(n, n_bars - n):
            # Check swing high
            is_sh = True
            for k in range(1, n + 1):
                if highs[i] <= highs[i - k] or highs[i] <= highs[i + k]:
                    is_sh = False
                    break
            if is_sh:
                swings.append(SwingPoint(
                    index=i,
                    timestamp=ts[i],
                    price=float(highs[i]),
                    kind="HIGH",
                    confirmed_bar=i + n
                ))

            # Check swing low
            is_sl = True
            for k in range(1, n + 1):
                if lows[i] >= lows[i - k] or lows[i] >= lows[i + k]:
                    is_sl = False
                    break
            if is_sl:
                swings.append(SwingPoint(
                    index=i,
                    timestamp=ts[i],
                    price=float(lows[i]),
                    kind="LOW",
                    confirmed_bar=i + n
                ))

        swings.sort(key=lambda x: x.index)

        # Label subkind (HH, LH, HL, LL)
        last_h = None
        last_l = None
        for s in swings:
            if s.kind == "HIGH":
                if last_h is not None:
                    s.subkind = "HH" if s.price > last_h.price else "LH"
                else:
                    s.subkind = "HH"
                last_h = s
            else:
                if last_l is not None:
                    s.subkind = "HL" if s.price > last_l.price else "LL"
                else:
                    s.subkind = "LL"
                last_l = s

        # Mark violated bars
        for s in swings:
            for b in range(s.confirmed_bar, n_bars):
                if s.kind == "HIGH" and highs[b] > s.price:
                    s.violated_bar = b
                    break
                elif s.kind == "LOW" and lows[b] < s.price:
                    s.violated_bar = b
                    break

        return swings

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Liquidity Pools (Equal Highs / Equal Lows)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_liquidity_pools(self, df: pd.DataFrame, swings: list[SwingPoint]) -> list[LiquidityPool]:
        pools: list[LiquidityPool] = []
        tol_pct = self.eq_tolerance_pct / 100.0

        # Equal Highs
        high_swings = [s for s in swings if s.kind == "HIGH"]
        for i in range(len(high_swings)):
            s1 = high_swings[i]
            matched = [s1]
            for j in range(i + 1, len(high_swings)):
                s2 = high_swings[j]
                if abs(s1.price - s2.price) / s1.price <= tol_pct:
                    matched.append(s2)
            if len(matched) >= 2:
                avg_p = float(np.mean([m.price for m in matched]))
                indices = [m.index for m in matched]
                pool_id = f"EQH_{indices[0]}_{round(avg_p, 1)}"
                if not any(p.pool_id == pool_id for p in pools):
                    pools.append(LiquidityPool(
                        pool_id=pool_id,
                        pool_type="EQH",
                        level=avg_p,
                        span_low=min(m.price for m in matched),
                        span_high=max(m.price for m in matched),
                        touch_count=len(matched),
                        bar_indices=indices
                    ))

        # Equal Lows
        low_swings = [s for s in swings if s.kind == "LOW"]
        for i in range(len(low_swings)):
            s1 = low_swings[i]
            matched = [s1]
            for j in range(i + 1, len(low_swings)):
                s2 = low_swings[j]
                if abs(s1.price - s2.price) / s1.price <= tol_pct:
                    matched.append(s2)
            if len(matched) >= 2:
                avg_p = float(np.mean([m.price for m in matched]))
                indices = [m.index for m in matched]
                pool_id = f"EQL_{indices[0]}_{round(avg_p, 1)}"
                if not any(p.pool_id == pool_id for p in pools):
                    pools.append(LiquidityPool(
                        pool_id=pool_id,
                        pool_type="EQL",
                        level=avg_p,
                        span_low=min(m.price for m in matched),
                        span_high=max(m.price for m in matched),
                        touch_count=len(matched),
                        bar_indices=indices
                    ))

        # Check if swept
        n_bars = len(df)
        highs = df["high"].values
        lows = df["low"].values
        for p in pools:
            last_idx = max(p.bar_indices)
            for b in range(last_idx + 1, n_bars):
                if p.pool_type == "EQH" and highs[b] > p.span_high:
                    p.is_swept = True
                    p.swept_by_bar = b
                    break
                elif p.pool_type == "EQL" and lows[b] < p.span_low:
                    p.is_swept = True
                    p.swept_by_bar = b
                    break

        return pools

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Fair Value Gaps (3-Candle Imbalance with Real-time Mitigation Tracking)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_fvgs(self, df: pd.DataFrame) -> list[FairValueGap]:
        fvgs: list[FairValueGap] = []
        n_bars = len(df)
        if n_bars < 3:
            return fvgs

        highs = df["high"].values
        lows = df["low"].values
        ts = df["timestamp"].tolist()

        for i in range(2, n_bars):
            c0_high, c0_low = highs[i - 2], lows[i - 2]
            c2_high, c2_low = highs[i], lows[i]

            # Bullish FVG: Low of candle 3 is higher than High of candle 1
            if c2_low > c0_high:
                gap_bot = float(c0_high)
                gap_top = float(c2_low)
                ce = float((gap_bot + gap_top) / 2.0)
                fvg = FairValueGap(
                    fvg_id=f"BULL_FVG_{i-1}",
                    direction="BULLISH",
                    top=gap_top,
                    bottom=gap_bot,
                    consequent_encroachment=ce,
                    bar_index=i - 1,
                    timestamp=ts[i - 1]
                )
                # Check mitigation in subsequent bars
                for b in range(i + 1, n_bars):
                    b_low = lows[b]
                    if b_low <= gap_bot:
                        fvg.status = "MITIGATED"
                        fvg.fill_percentage = 100.0
                        fvg.mitigated_bar = b
                        fvg.mitigated_time = ts[b]
                        break
                    elif b_low <= ce:
                        fvg.status = "PARTIALLY_FILLED"
                        fill = (gap_top - b_low) / (gap_top - gap_bot) * 100.0
                        fvg.fill_percentage = max(fvg.fill_percentage, fill)
                    elif b_low < gap_top:
                        fill = (gap_top - b_low) / (gap_top - gap_bot) * 100.0
                        fvg.fill_percentage = max(fvg.fill_percentage, fill)
                fvgs.append(fvg)

            # Bearish FVG: High of candle 3 is lower than Low of candle 1
            elif c2_high < c0_low:
                gap_top = float(c0_low)
                gap_bot = float(c2_high)
                ce = float((gap_bot + gap_top) / 2.0)
                fvg = FairValueGap(
                    fvg_id=f"BEAR_FVG_{i-1}",
                    direction="BEARISH",
                    top=gap_top,
                    bottom=gap_bot,
                    consequent_encroachment=ce,
                    bar_index=i - 1,
                    timestamp=ts[i - 1]
                )
                # Check mitigation in subsequent bars
                for b in range(i + 1, n_bars):
                    b_high = highs[b]
                    if b_high >= gap_top:
                        fvg.status = "MITIGATED"
                        fvg.fill_percentage = 100.0
                        fvg.mitigated_bar = b
                        fvg.mitigated_time = ts[b]
                        break
                    elif b_high >= ce:
                        fvg.status = "PARTIALLY_FILLED"
                        fill = (b_high - gap_bot) / (gap_top - gap_bot) * 100.0
                        fvg.fill_percentage = max(fvg.fill_percentage, fill)
                    elif b_high > gap_bot:
                        fill = (b_high - gap_bot) / (gap_top - gap_bot) * 100.0
                        fvg.fill_percentage = max(fvg.fill_percentage, fill)
                fvgs.append(fvg)

        return fvgs

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Market Structure Breaks (MSB): Strong vs. Weak MSB
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_msbs(self, df: pd.DataFrame, swings: list[SwingPoint]) -> list[MarketStructureBreak]:
        msb_list: list[MarketStructureBreak] = []
        n_bars = len(df)
        if len(swings) < 3:
            return msb_list

        closes = df["close"].values
        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        ts = df["timestamp"].tolist()

        for s_idx in range(1, len(swings)):
            prior_swings = swings[:s_idx]
            curr_swing = swings[s_idx]

            # Bullish MSB
            if curr_swing.kind == "LOW":
                preceding_lhs = [s for s in prior_swings if s.kind == "HIGH" and s.subkind in ("LH", "")]
                if preceding_lhs:
                    target_lh = preceding_lhs[-1]
                    deepest_ll = curr_swing

                    for b in range(curr_swing.confirmed_bar, n_bars):
                        if highs[b] > target_lh.price:
                            candle_body = abs(closes[b] - opens[b])
                            candle_range = max(highs[b] - lows[b], 0.001)
                            body_ratio = candle_body / candle_range

                            is_closed_above = closes[b] > target_lh.price
                            is_strong = is_closed_above and (body_ratio >= 0.45 or closes[b] > (opens[b] + candle_range * 0.4))

                            q_factors = []
                            q_score = 3
                            if is_closed_above:
                                q_score += 2
                                q_factors.append("Full-bodied candle close above LH")
                            else:
                                q_factors.append("Wick pierce without candle close (Weak)")

                            if body_ratio >= 0.55:
                                q_score += 1
                                q_factors.append("Impulsive displacement candle (high body ratio)")

                            if b - curr_swing.index <= 8:
                                q_score += 1
                                q_factors.append("Immediate impulse out of deepest LL")

                            entry_low = min(deepest_ll.price, target_lh.price)
                            entry_high = max(deepest_ll.price, target_lh.price)

                            msb_obj = MarketStructureBreak(
                                break_id=f"BULL_MSB_{b}_{round(target_lh.price, 1)}",
                                direction="BULLISH",
                                strength="STRONG" if is_strong else "WEAK",
                                level_price=target_lh.price,
                                extreme_price=deepest_ll.price,
                                break_bar=b,
                                break_time=ts[b],
                                close_price=float(closes[b]),
                                entry_zone_low=entry_low,
                                entry_zone_high=entry_high,
                                quality_score=min(q_score, 7),
                                quality_factors=q_factors,
                                description=(
                                    f"{'Strong' if is_strong else 'Weak'} Bullish MSB: Broken LH @ {target_lh.price:,.1f} "
                                    f"(Origin LL @ {deepest_ll.price:,.1f}). Entry Zone: [{entry_low:,.1f} - {entry_high:,.1f}]."
                                )
                            )
                            msb_list.append(msb_obj)
                            break

            # Bearish MSB
            elif curr_swing.kind == "HIGH":
                preceding_hls = [s for s in prior_swings if s.kind == "LOW" and s.subkind in ("HL", "")]
                if preceding_hls:
                    target_hl = preceding_hls[-1]
                    highest_hh = curr_swing

                    for b in range(curr_swing.confirmed_bar, n_bars):
                        if lows[b] < target_hl.price:
                            candle_body = abs(closes[b] - opens[b])
                            candle_range = max(highs[b] - lows[b], 0.001)
                            body_ratio = candle_body / candle_range

                            is_closed_below = closes[b] < target_hl.price
                            is_strong = is_closed_below and (body_ratio >= 0.45 or closes[b] < (opens[b] - candle_range * 0.4))

                            q_factors = []
                            q_score = 3
                            if is_closed_below:
                                q_score += 2
                                q_factors.append("Full-bodied candle close below HL")
                            else:
                                q_factors.append("Wick pierce without candle close (Weak)")

                            if body_ratio >= 0.55:
                                q_score += 1
                                q_factors.append("Impulsive displacement candle (high body ratio)")

                            if b - curr_swing.index <= 8:
                                q_score += 1
                                q_factors.append("Immediate impulse out of highest HH")

                            entry_low = min(highest_hh.price, target_hl.price)
                            entry_high = max(highest_hh.price, target_hl.price)

                            msb_obj = MarketStructureBreak(
                                break_id=f"BEAR_MSB_{b}_{round(target_hl.price, 1)}",
                                direction="BEARISH",
                                strength="STRONG" if is_strong else "WEAK",
                                level_price=target_hl.price,
                                extreme_price=highest_hh.price,
                                break_bar=b,
                                break_time=ts[b],
                                close_price=float(closes[b]),
                                entry_zone_low=entry_low,
                                entry_zone_high=entry_high,
                                quality_score=min(q_score, 7),
                                quality_factors=q_factors,
                                description=(
                                    f"{'Strong' if is_strong else 'Weak'} Bearish MSB: Broken HL @ {target_hl.price:,.1f} "
                                    f"(Origin HH @ {highest_hh.price:,.1f}). Entry Zone: [{entry_low:,.1f} - {entry_high:,.1f}]."
                                )
                            )
                            msb_list.append(msb_obj)
                            break

        seen_bars = set()
        deduped = []
        for m in msb_list:
            if m.break_bar not in seen_bars:
                seen_bars.add(m.break_bar)
                deduped.append(m)

        return deduped

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Order Blocks (OB) & Breaker Blocks (State-Flipping Engine)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_obs_and_breakers(
        self, df: pd.DataFrame, msbs: list[MarketStructureBreak]
    ) -> tuple[list[OrderBlock], list[OrderBlock]]:
        obs: list[OrderBlock] = []
        breakers: list[OrderBlock] = []
        n_bars = len(df)

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        volumes = df["volume"].values
        ts = df["timestamp"].tolist()

        for msb in msbs:
            b_bar = msb.break_bar
            if msb.direction == "BULLISH":
                ob_bar = None
                for idx in range(b_bar - 1, max(0, b_bar - 15), -1):
                    if closes[idx] < opens[idx]:
                        ob_bar = idx
                        break
                if ob_bar is None:
                    ob_bar = max(0, b_bar - 1)

                top = float(max(opens[ob_bar], closes[ob_bar]))
                bottom = float(lows[ob_bar])
                mean_th = float((top + bottom) / 2.0)
                vol_ratio = float(volumes[ob_bar] / (np.mean(volumes[max(0, ob_bar-5):ob_bar+1]) + 1e-6))

                ob = OrderBlock(
                    ob_id=f"BULL_OB_{ob_bar}",
                    direction="BULLISH",
                    top=top,
                    bottom=bottom,
                    mean_threshold=mean_th,
                    bar_index=ob_bar,
                    timestamp=ts[ob_bar],
                    state="ACTIVE",
                    msb_ref_id=msb.break_id,
                    quality_score=6 if msb.strength == "STRONG" else 4,
                    volume_ratio=vol_ratio
                )

                # If price cleanly closes below demand OB bottom -> flips to Bearish Breaker
                for fwd in range(b_bar + 1, n_bars):
                    if closes[fwd] < ob.bottom:
                        ob.state = "BREAKER"
                        ob.breaker_bar = fwd
                        ob.breaker_time = ts[fwd]
                        breakers.append(ob)
                        break
                obs.append(ob)

            elif msb.direction == "BEARISH":
                ob_bar = None
                for idx in range(b_bar - 1, max(0, b_bar - 15), -1):
                    if closes[idx] > opens[idx]:
                        ob_bar = idx
                        break
                if ob_bar is None:
                    ob_bar = max(0, b_bar - 1)

                bottom = float(min(opens[ob_bar], closes[ob_bar]))
                top = float(highs[ob_bar])
                mean_th = float((top + bottom) / 2.0)
                vol_ratio = float(volumes[ob_bar] / (np.mean(volumes[max(0, ob_bar-5):ob_bar+1]) + 1e-6))

                ob = OrderBlock(
                    ob_id=f"BEAR_OB_{ob_bar}",
                    direction="BEARISH",
                    top=top,
                    bottom=bottom,
                    mean_threshold=mean_th,
                    bar_index=ob_bar,
                    timestamp=ts[ob_bar],
                    state="ACTIVE",
                    msb_ref_id=msb.break_id,
                    quality_score=6 if msb.strength == "STRONG" else 4,
                    volume_ratio=vol_ratio
                )

                # If price cleanly closes above supply OB top -> flips to Bullish Breaker
                for fwd in range(b_bar + 1, n_bars):
                    if closes[fwd] > ob.top:
                        ob.state = "BREAKER"
                        ob.breaker_bar = fwd
                        ob.breaker_time = ts[fwd]
                        breakers.append(ob)
                        break
                obs.append(ob)

        return obs, breakers

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Swing Failure Patterns (SFP) / Liquidity Sweeps
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_sfps(
        self, df: pd.DataFrame, swings: list[SwingPoint], pools: list[LiquidityPool]
    ) -> list[SwingFailurePattern]:
        sfps: list[SwingFailurePattern] = []
        n_bars = len(df)
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        ts = df["timestamp"].tolist()

        for p in pools:
            if not p.is_swept or p.swept_by_bar is None:
                continue
            b = p.swept_by_bar
            if p.pool_type == "EQH":
                if highs[b] > p.level and closes[b] < p.level:
                    sfps.append(SwingFailurePattern(
                        sfp_id=f"SFP_EQH_{b}",
                        direction="BEARISH",
                        level_swept=p.level,
                        extreme_wick_price=float(highs[b]),
                        close_price=float(closes[b]),
                        bar_index=b,
                        timestamp=ts[b],
                        pool_type="EQH",
                        rationale=f"Swept {p.touch_count}-touch Equal Highs @ {p.level:,.1f}; wick rejection back below level."
                    ))
            elif p.pool_type == "EQL":
                if lows[b] < p.level and closes[b] > p.level:
                    sfps.append(SwingFailurePattern(
                        sfp_id=f"SFP_EQL_{b}",
                        direction="BULLISH",
                        level_swept=p.level,
                        extreme_wick_price=float(lows[b]),
                        close_price=float(closes[b]),
                        bar_index=b,
                        timestamp=ts[b],
                        pool_type="EQL",
                        rationale=f"Swept {p.touch_count}-touch Equal Lows @ {p.level:,.1f}; wick rejection back above level."
                    ))

        for s in swings:
            if not s.is_active(n_bars - 1) and s.violated_bar is not None:
                b = s.violated_bar
                if s.kind == "HIGH":
                    if highs[b] > s.price and closes[b] < s.price:
                        sfps.append(SwingFailurePattern(
                            sfp_id=f"SFP_SWING_H_{b}",
                            direction="BEARISH",
                            level_swept=s.price,
                            extreme_wick_price=float(highs[b]),
                            close_price=float(closes[b]),
                            bar_index=b,
                            timestamp=ts[b],
                            pool_type="MAJOR_SWING_HIGH",
                            rationale=f"Liquidity Sweep of prior Swing High {s.price:,.1f}; closed inside range at {closes[b]:,.1f}."
                        ))
                elif s.kind == "LOW":
                    if lows[b] < s.price and closes[b] > s.price:
                        sfps.append(SwingFailurePattern(
                            sfp_id=f"SFP_SWING_L_{b}",
                            direction="BULLISH",
                            level_swept=s.price,
                            extreme_wick_price=float(lows[b]),
                            close_price=float(closes[b]),
                            bar_index=b,
                            timestamp=ts[b],
                            pool_type="MAJOR_SWING_LOW",
                            rationale=f"Liquidity Sweep of prior Swing Low {s.price:,.1f}; closed inside range at {closes[b]:,.1f}."
                        ))

        return sfps

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Dealing Range (50% Equilibrium & OTE Golden Zone)
    # ──────────────────────────────────────────────────────────────────────────
    def _calculate_dealing_range(self, df: pd.DataFrame, swings: list[SwingPoint]) -> Optional[DealingRange]:
        cur_p = float(df["close"].iloc[-1])
        recent_highs = [s.price for s in swings if s.kind == "HIGH"]
        recent_lows = [s.price for s in swings if s.kind == "LOW"]

        if not recent_highs or not recent_lows:
            hi = float(df["high"].max())
            lo = float(df["low"].min())
        else:
            hi = max(recent_highs[-4:])
            lo = min(recent_lows[-4:])

        rng = max(hi - lo, 1.0)
        eq = lo + 0.50 * rng

        # OTE Fibonacci retracement levels (0.705 and 0.790)
        ote_705 = lo + 0.705 * rng
        ote_790 = lo + 0.790 * rng

        pos_pct = (cur_p - lo) / rng * 100.0
        if cur_p > eq + 0.02 * rng:
            regime = "PREMIUM"
        elif cur_p < eq - 0.02 * rng:
            regime = "DISCOUNT"
        else:
            regime = "EQUILIBRIUM"

        return DealingRange(
            high=round(hi, 1),
            low=round(lo, 1),
            equilibrium=round(eq, 1),
            ote_705=round(ote_705, 1),
            ote_790=round(ote_790, 1),
            current_price=round(cur_p, 1),
            current_regime=regime,
            discount_depth_pct=round(pos_pct, 1)
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Dragon Fruit Combo Detection (OB + FVG Alignment)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_dragon_fruit_combos(
        self,
        obs: list[OrderBlock],
        fvgs: list[FairValueGap],
        msbs: list[MarketStructureBreak],
        dealing_range: Optional[DealingRange]
    ) -> list[DragonFruitCombo]:
        combos: list[DragonFruitCombo] = []

        for ob in obs:
            if ob.state != "ACTIVE":
                continue

            for fvg in fvgs:
                if fvg.status == "MITIGATED":
                    continue
                if fvg.direction != ob.direction:
                    continue

                overlap_low = max(ob.bottom, fvg.bottom)
                overlap_high = min(ob.top, fvg.top)

                is_overlapping = overlap_low <= overlap_high
                is_adjacent = abs(ob.top - fvg.bottom) <= 5.0 or abs(fvg.top - ob.bottom) <= 5.0

                if is_overlapping or is_adjacent:
                    effective_low = min(ob.bottom, fvg.bottom)
                    effective_high = max(ob.top, fvg.top)
                    sniper_entry = (ob.mean_threshold + fvg.consequent_encroachment) / 2.0

                    regime_note = ""
                    if dealing_range:
                        if ob.direction == "BEARISH" and sniper_entry >= dealing_range.equilibrium:
                            regime_note = "Premium Zone confluence (High-conviction short)."
                        elif ob.direction == "BULLISH" and sniper_entry <= dealing_range.equilibrium:
                            regime_note = "Discount Zone confluence (High-conviction long)."

                    combos.append(DragonFruitCombo(
                        combo_id=f"DFC_{ob.ob_id}_{fvg.fvg_id}",
                        direction=ob.direction,
                        ob=ob,
                        fvg=fvg,
                        overlap_low=round(effective_low, 1),
                        overlap_high=round(effective_high, 1),
                        mean_sniper_entry=round(sniper_entry, 1),
                        entry_zone=(round(effective_low, 1), round(effective_high, 1)),
                        narrative=(
                            f"Dragon Fruit Combo: {ob.direction} OB [{ob.bottom:,.1f}-{ob.top:,.1f}] "
                            f"overlaps with {fvg.direction} FVG [{fvg.bottom:,.1f}-{fvg.top:,.1f}]. "
                            f"Sniper Entry: {sniper_entry:,.1f}. {regime_note}"
                        )
                    ))

        return combos
