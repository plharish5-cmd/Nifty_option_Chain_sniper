"""
liquidity_engine.py — Production Liquidity & Smart Money Engine for NIFTY 50.

WHAT THIS MODULE CANNOT DO:
───────────────────────────
1. Stop-loss pools are INFERRED from conventional order placement, NOT OBSERVED
   order book data. NSE publishes no resting-order data at these prices. The model
   identifies where stops are conventionally clustered; if traders place stops
   elsewhere, the model is wrong. Never treat a pool as confirmed.
2. A swept pool is an OBSERVATION, NOT A DIRECTIONAL TRIGGER. Price sweeping a pool
   and returning happens constantly in normal market auctioning. It becomes a setup
   only with displacement in the opposite direction AND a confirmed structure break.
3. It CANNOT eliminate the fractal pivot confirmation lag (N bars into the future).
   A pivot at bar t requires High[t] > max(High[t+1..t+N]) and cannot be known until
   bar t+N closes. Trying to bypass this creates backtests that cannot trade live.
4. Two closes are required for claims, reclaims, and breaks. The newest bar can never
   confirm anything — it reports WAITING.
5. OTE (Optimal Trade Entry) is a confirmation filter, NEVER a standalone entry.
6. SMT Divergence requires a real BANKNIFTY candle feed. If unavailable, it reports
   UNAVAILABLE — needs BANKNIFTY feed. Substituting a proxy is strictly forbidden.
7. Judas Swing cannot be predicted while forming — it is reported only after reversal
   and displacement are fully confirmed.
8. Power of Three (AMD) is valid ONLY at a known pool, OI wall, or prior session extreme.
9. No win rates, profit factors, or invented statistics.
10. NO STREAMLIT IMPORTS in this engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# Already built modules — import, do not rewrite
import smc
import order_flow
import breakout_quality
import session_levels
import wick_support
import analytics
import flow
import institutional
import confluence


# ═══════════════════════════════════════════════════════════════════════════
# Helper Functions & Times
# ═══════════════════════════════════════════════════════════════════════════

def _times(df: pd.DataFrame) -> pd.Series:
    """Extract datetime series from timestamp column or index safely."""
    if df is None or df.empty:
        return pd.Series(dtype="datetime64[ns]")
    if "timestamp" in df.columns:
        return pd.to_datetime(df["timestamp"])
    return pd.Series(pd.to_datetime(df.index), index=df.index)


DEFAULT_RULES_PATH = Path(__file__).parent / "liquidity_rules.json"


def load_rules(rules_path: Optional[Path] = None) -> dict:
    """Load configuration rules from liquidity_rules.json with robust fallbacks."""
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "pivot_window_n": 2,
        "touch_tolerance_pts": 8.0,
        "zone_half_width_pts": 6.0,
        "pool_offset_atr_mult": 0.5,
        "min_pool_offset_pts": 10.0,
        "pool_approach_buffer_pts": 15.0,
        "striking_distance_pts": 150.0,
        "eqh_eql_tolerance_pct": 0.001,
        "displacement_min_score": 3,
        "displacement_body_atr_mult": 1.2,
        "displacement_max_wick_pct": 0.30,
        "displacement_body_range_pct": 0.60,
        "fvg_mitigate_at": 0.50,
        "fvg_iofed_pct": 0.25,
        "ote_lower": 0.618,
        "ote_upper": 0.79,
        "equilibrium_band_pct": 0.03,
        "turtle_soup_max_bars": 5,
        "judas_min_move_atr": 0.8,
        "judas_min_displacement_score": 3,
        "amd_min_consolidation_bars": 4,
        "amd_max_range_atr": 1.0,
        "confluence_proximity_pts": 25.0,
        "confluence_min_score": 3,
    }
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                defaults.update({k: v for k, v in loaded.items() if not k.startswith("_")})
        except Exception:
            pass
    return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Liquidity Pool Map Dataclasses & Logic
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PoolItem:
    pool_id: str                          # e.g. "EQH_24150", "PDH_24200"
    pool_type: str                        # EQH | EQL | PDH | PDL | PWH | PWL | PMH | PML | SESSION | BUY_STOPS | SELL_STOPS
    source: str                           # Description of anchor level
    level: float                          # Exact price of structural anchor
    pool_price: float                     # Price where stops cluster (level + offset for buy stops, - offset for sell stops)
    pool_side: str                        # "BUY_STOPS" (above resistance) | "SELL_STOPS" (below support)
    weight: float                         # Composite weight
    state: str                            # "RESTING" | "SWEPT" | "CLAIMED" | "WAITING"
    touch_count: int
    family_agreements: list[str]
    timeframe: str                        # "MONTHLY" | "WEEKLY" | "DAILY" | "SESSION" | "INTRADAY"
    origin_move_atr: float
    swept_time: Optional[datetime] = None
    claimed_time: Optional[datetime] = None
    swept_bar: Optional[int] = None
    distance_pts: float = 0.0
    mechanical_note: str = ""
    provenance: str = "INFERRED — no order book data. Modelled stop placement."


@dataclass
class LiquidityPoolMap:
    pools: list[PoolItem] = field(default_factory=list)
    nearest_above: Optional[PoolItem] = None
    nearest_below: Optional[PoolItem] = None
    resting_count: int = 0
    swept_count: int = 0
    claimed_count: int = 0
    waiting_count: int = 0
    provenance: str = "INFERRED — no order book data. NSE publishes no resting-order depth. Stops modelled by convention."


def compute_pool_map(
    df: pd.DataFrame,
    pivots: list[smc.Pivot],
    spot: float,
    atr: float,
    ohlc_daily: Optional[pd.DataFrame] = None,
    chain_levels: Optional[Any] = None,
    rules: Optional[dict] = None,
) -> LiquidityPoolMap:
    """
    Builds the ranked liquidity pool map from all structural levels.
    Weights each pool by touches, family agreement, timeframe hierarchy, and ATR move.
    Tracks sweep state: RESTING -> SWEPT (traded through and returned) -> CLAIMED (two closes beyond).
    """
    rules = rules or load_rules()
    pool_map = LiquidityPoolMap()
    if df is None or df.empty or len(df) < 2:
        return pool_map

    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    n_bars = len(df)

    atr_val = max(float(atr), 1.0)
    offset_mult = float(rules.get("pool_offset_atr_mult", 0.5))
    min_offset = float(rules.get("min_pool_offset_pts", 10.0))
    offset = max(offset_mult * atr_val, min_offset)
    tol = float(rules.get("touch_tolerance_pts", 8.0))
    eq_tol_pct = float(rules.get("eqh_eql_tolerance_pct", 0.001))

    raw_pools: list[PoolItem] = []

    # Helper to count touches and originating move
    def _touches_and_move(price: float) -> tuple[int, float]:
        t_count = 0
        max_move = 0.0
        for i in range(n_bars):
            if abs(h[i] - price) <= tol or abs(l[i] - price) <= tol:
                t_count += 1
                # Check move originating after touch
                if i + 5 < n_bars:
                    post_move = abs(c[min(i + 5, n_bars - 1)] - price)
                    if post_move > max_move:
                        max_move = post_move
        return min(t_count, 6), max_move / atr_val

    # 1. Prior Daily, Weekly, Monthly Levels
    if ohlc_daily is not None and not ohlc_daily.empty:
        d_df = ohlc_daily.copy()
        c_map = {str(col).lower(): col for col in d_df.columns}
        if "high" in c_map and "low" in c_map:
            h_col, l_col = c_map["high"], c_map["low"]
            # Prior Day High / Low (PDH, PDL)
            if len(d_df) >= 2:
                pdh = float(d_df[h_col].iloc[-2])
                pdl = float(d_df[l_col].iloc[-2])
                t_pdh, m_pdh = _touches_and_move(pdh)
                t_pdl, m_pdl = _touches_and_move(pdl)
                raw_pools.append(PoolItem(
                    pool_id=f"PDH_{pdh:.0f}", pool_type="PDH", source="Prior Day High",
                    level=pdh, pool_price=pdh + offset, pool_side="BUY_STOPS",
                    weight=3.0 + t_pdh + min(m_pdh, 3.0), state="RESTING", touch_count=t_pdh,
                    family_agreements=["Daily Session Extreme"], timeframe="DAILY", origin_move_atr=m_pdh,
                    mechanical_note="Shorts' protective buy-stops resting above yesterday's high."
                ))
                raw_pools.append(PoolItem(
                    pool_id=f"PDL_{pdl:.0f}", pool_type="PDL", source="Prior Day Low",
                    level=pdl, pool_price=pdl - offset, pool_side="SELL_STOPS",
                    weight=3.0 + t_pdl + min(m_pdl, 3.0), state="RESTING", touch_count=t_pdl,
                    family_agreements=["Daily Session Extreme"], timeframe="DAILY", origin_move_atr=m_pdl,
                    mechanical_note="Longs' protective sell-stops resting below yesterday's low."
                ))

            # Prior Week High / Low (PWH, PWL)
            date_col = c_map.get("date")
            if date_col and len(d_df) >= 10:
                try:
                    d_df["_dt"] = pd.to_datetime(d_df[date_col])
                    d_df["_week"] = d_df["_dt"].dt.isocalendar().week
                    d_df["_year"] = d_df["_dt"].dt.isocalendar().year
                    weeks = d_df.groupby(["_year", "_week"]).agg({h_col: "max", l_col: "min"}).reset_index()
                    if len(weeks) >= 2:
                        pwh = float(weeks[h_col].iloc[-2])
                        pwl = float(weeks[l_col].iloc[-2])
                        t_pwh, m_pwh = _touches_and_move(pwh)
                        t_pwl, m_pwl = _touches_and_move(pwl)
                        raw_pools.append(PoolItem(
                            pool_id=f"PWH_{pwh:.0f}", pool_type="PWH", source="Prior Week High",
                            level=pwh, pool_price=pwh + offset, pool_side="BUY_STOPS",
                            weight=4.0 + t_pwh + min(m_pwh, 3.0), state="RESTING", touch_count=t_pwh,
                            family_agreements=["Weekly Extreme"], timeframe="WEEKLY", origin_move_atr=m_pwh,
                            mechanical_note="Institutional swing stops resting above previous week's high."
                        ))
                        raw_pools.append(PoolItem(
                            pool_id=f"PWL_{pwl:.0f}", pool_type="PWL", source="Prior Week Low",
                            level=pwl, pool_price=pwl - offset, pool_side="SELL_STOPS",
                            weight=4.0 + t_pwl + min(m_pwl, 3.0), state="RESTING", touch_count=t_pwl,
                            family_agreements=["Weekly Extreme"], timeframe="WEEKLY", origin_move_atr=m_pwl,
                            mechanical_note="Institutional swing stops resting below previous week's low."
                        ))
                except Exception:
                    pass

    # 2. Today's Session Opening Range (09:15-09:30)
    if len(df) >= 1:
        sess_h = float(h[:min(2, n_bars)].max())
        sess_l = float(l[:min(2, n_bars)].min())
        t_sh, m_sh = _touches_and_move(sess_h)
        t_sl, m_sl = _touches_and_move(sess_l)
        raw_pools.append(PoolItem(
            pool_id=f"SESSION_ORH_{sess_h:.0f}", pool_type="SESSION", source="Today's Opening Range High",
            level=sess_h, pool_price=sess_h + offset, pool_side="BUY_STOPS",
            weight=2.0 + t_sh, state="RESTING", touch_count=t_sh,
            family_agreements=["Session Opening Range"], timeframe="SESSION", origin_move_atr=m_sh,
            mechanical_note="Opening session buy-stops placed by early breakout traders."
        ))
        raw_pools.append(PoolItem(
            pool_id=f"SESSION_ORL_{sess_l:.0f}", pool_type="SESSION", source="Today's Opening Range Low",
            level=sess_l, pool_price=sess_l - offset, pool_side="SELL_STOPS",
            weight=2.0 + t_sl, state="RESTING", touch_count=t_sl,
            family_agreements=["Session Opening Range"], timeframe="SESSION", origin_move_atr=m_sl,
            mechanical_note="Opening session sell-stops placed by early range buyers."
        ))

    # 3. Equal Highs (EQH) and Equal Lows (EQL) from Pivots
    high_pivots = [p for p in pivots if p.kind == "HIGH"]
    low_pivots = [p for p in pivots if p.kind == "LOW"]

    for i in range(len(high_pivots)):
        for j in range(i + 1, len(high_pivots)):
            p1, p2 = high_pivots[i], high_pivots[j]
            if p2.i - p1.i < 2:
                continue
            avg = (p1.price + p2.price) / 2.0
            if abs(p1.price - p2.price) / max(avg, 1.0) <= eq_tol_pct:
                t_eqh, m_eqh = _touches_and_move(avg)
                raw_pools.append(PoolItem(
                    pool_id=f"EQH_{avg:.0f}", pool_type="EQH", source=f"Equal Highs (bars {p1.i}&{p2.i})",
                    level=avg, pool_price=avg + offset, pool_side="BUY_STOPS",
                    weight=3.5 + t_eqh + min(m_eqh, 2.0), state="RESTING", touch_count=t_eqh + 2,
                    family_agreements=["Intraday Double/Triple Top"], timeframe="INTRADAY", origin_move_atr=m_eqh,
                    mechanical_note="Cleanest buy-side liquidity pool on chart; retail stops clustered tightly above."
                ))

    for i in range(len(low_pivots)):
        for j in range(i + 1, len(low_pivots)):
            p1, p2 = low_pivots[i], low_pivots[j]
            if p2.i - p1.i < 2:
                continue
            avg = (p1.price + p2.price) / 2.0
            if abs(p1.price - p2.price) / max(avg, 1.0) <= eq_tol_pct:
                t_eql, m_eql = _touches_and_move(avg)
                raw_pools.append(PoolItem(
                    pool_id=f"EQL_{avg:.0f}", pool_type="EQL", source=f"Equal Lows (bars {p1.i}&{p2.i})",
                    level=avg, pool_price=avg - offset, pool_side="SELL_STOPS",
                    weight=3.5 + t_eql + min(m_eql, 2.0), state="RESTING", touch_count=t_eql + 2,
                    family_agreements=["Intraday Double/Triple Bottom"], timeframe="INTRADAY", origin_move_atr=m_eql,
                    mechanical_note="Cleanest sell-side liquidity pool on chart; retail stops clustered tightly below."
                ))

    # 4. Prominent Swing Pivots as BUY_STOPS / SELL_STOPS
    for p in pivots[-6:]:
        t_p, m_p = _touches_and_move(p.price)
        if p.kind == "HIGH":
            raw_pools.append(PoolItem(
                pool_id=f"SWING_H_{p.price:.0f}", pool_type="BUY_STOPS", source=f"Swing High ({p.subkind or 'Pivot'})",
                level=p.price, pool_price=p.price + offset, pool_side="BUY_STOPS",
                weight=1.5 + t_p + min(m_p, 2.0), state="RESTING", touch_count=t_p,
                family_agreements=["Fractal Swing Pivot"], timeframe="INTRADAY", origin_move_atr=m_p,
                mechanical_note="Protective buy-stops resting above local swing high."
            ))
        else:
            raw_pools.append(PoolItem(
                pool_id=f"SWING_L_{p.price:.0f}", pool_type="SELL_STOPS", source=f"Swing Low ({p.subkind or 'Pivot'})",
                level=p.price, pool_price=p.price - offset, pool_side="SELL_STOPS",
                weight=1.5 + t_p + min(m_p, 2.0), state="RESTING", touch_count=t_p,
                family_agreements=["Fractal Swing Pivot"], timeframe="INTRADAY", origin_move_atr=m_p,
                mechanical_note="Protective sell-stops resting below local swing low."
            ))

    # 5. Option Chain Walls (if available)
    if chain_levels is not None:
        for attr, p_side, p_type, name in [
            ("r1", "BUY_STOPS", "BUY_STOPS", "Call Wall R1"),
            ("r2", "BUY_STOPS", "BUY_STOPS", "Call Wall R2"),
            ("s1", "SELL_STOPS", "SELL_STOPS", "Put Wall S1"),
            ("s2", "SELL_STOPS", "SELL_STOPS", "Put Wall S2"),
        ]:
            val = getattr(chain_levels, attr, None)
            if val is not None and val > 0:
                p_lvl = float(val)
                t_w, m_w = _touches_and_move(p_lvl)
                p_price = p_lvl + offset if p_side == "BUY_STOPS" else p_lvl - offset
                raw_pools.append(PoolItem(
                    pool_id=f"CHAIN_{attr.upper()}_{p_lvl:.0f}", pool_type=p_type, source=f"Option {name}",
                    level=p_lvl, pool_price=p_price, pool_side=p_side,
                    weight=3.0 + t_w, state="RESTING", touch_count=t_w,
                    family_agreements=["Option Chain Heavy OI Wall"], timeframe="SESSION", origin_move_atr=m_w,
                    mechanical_note=f"Stops clustered beyond major {name} where writers defend positions."
                ))

    # De-duplicate raw pools within 5 points
    merged_pools: list[PoolItem] = []
    raw_pools = sorted(raw_pools, key=lambda x: x.pool_price)

    for p in raw_pools:
        found_match = False
        for m in merged_pools:
            if m.pool_side == p.pool_side and abs(m.pool_price - p.pool_price) <= 5.0:
                for fam in p.family_agreements:
                    if fam not in m.family_agreements:
                        m.family_agreements.append(fam)
                        m.weight += 1.5
                m.weight = max(m.weight, p.weight)
                m.touch_count = max(m.touch_count, p.touch_count)
                found_match = True
                break
        if not found_match:
            merged_pools.append(p)

    # Track Sweep & Claim States
    for p in merged_pools:
        p.distance_pts = round(p.pool_price - spot, 1)

        for k in range(n_bars):
            if p.pool_side == "BUY_STOPS":
                if h[k] >= p.pool_price:
                    if c[k] > p.pool_price:
                        if k + 1 < n_bars:
                            if c[k + 1] > p.pool_price:
                                p.state = "CLAIMED"
                                p.claimed_time = ts[k + 1]
                                break
                            else:
                                p.state = "SWEPT"
                                p.swept_time = ts[k]
                                p.swept_bar = k
                        else:
                            p.state = "WAITING"
                    else:
                        p.state = "SWEPT"
                        p.swept_time = ts[k]
                        p.swept_bar = k
            else:  # SELL_STOPS
                if l[k] <= p.pool_price:
                    if c[k] < p.pool_price:
                        if k + 1 < n_bars:
                            if c[k + 1] < p.pool_price:
                                p.state = "CLAIMED"
                                p.claimed_time = ts[k + 1]
                                break
                            else:
                                p.state = "SWEPT"
                                p.swept_time = ts[k]
                                p.swept_bar = k
                        else:
                            p.state = "WAITING"
                    else:
                        p.state = "SWEPT"
                        p.swept_time = ts[k]
                        p.swept_bar = k

    pools_above = [p for p in merged_pools if p.pool_price >= spot]
    pools_below = [p for p in merged_pools if p.pool_price < spot]

    pools_above = sorted(pools_above, key=lambda x: x.pool_price)
    pools_below = sorted(pools_below, key=lambda x: x.pool_price, reverse=True)

    nearest_above = pools_above[0] if pools_above else None
    nearest_below = pools_below[0] if pools_below else None

    r_cnt = sum(1 for p in merged_pools if p.state == "RESTING")
    s_cnt = sum(1 for p in merged_pools if p.state == "SWEPT")
    c_cnt = sum(1 for p in merged_pools if p.state == "CLAIMED")
    w_cnt = sum(1 for p in merged_pools if p.state == "WAITING")

    all_sorted = sorted(merged_pools, key=lambda x: (x.state == "RESTING", x.weight), reverse=True)

    pool_map.pools = all_sorted
    pool_map.nearest_above = nearest_above
    pool_map.nearest_below = nearest_below
    pool_map.resting_count = r_cnt
    pool_map.swept_count = s_cnt
    pool_map.claimed_count = c_cnt
    pool_map.waiting_count = w_cnt
    return pool_map


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Inducement
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Inducement:
    detected: bool
    minor_pivot_price: Optional[float]
    target_pool_price: Optional[float]
    is_taken: bool
    taken_time: Optional[datetime]
    quality_verdict: str
    note: str
    provenance: str = "INFERRED — minor swing liquidity taken prior to structural pool."


def evaluate_inducement(
    df: pd.DataFrame,
    pivots: list[smc.Pivot],
    target_pool: Optional[PoolItem],
    spot: float,
) -> Inducement:
    """
    Detects whether minor inducement liquidity was taken before reaching a structural zone.
    A zone reached WITHOUT inducement taken first is lower quality.
    """
    if df is None or df.empty or target_pool is None:
        return Inducement(
            detected=False, minor_pivot_price=None, target_pool_price=None,
            is_taken=False, taken_time=None,
            quality_verdict="NO TARGET ZONE",
            note="No active target pool currently identified in proximity to spot."
        )

    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()

    if target_pool.pool_price > spot:
        cands = [p for p in pivots if spot < p.price < target_pool.level]
        if not cands:
            return Inducement(
                detected=False, minor_pivot_price=None, target_pool_price=target_pool.pool_price,
                is_taken=False, taken_time=None,
                quality_verdict="LOW QUALITY (No inducement taken before zone)",
                note=f"Target pool {target_pool.pool_id} approached directly without taking minor inducement first."
            )
        minor = cands[-1]
        taken = False
        taken_at = None
        for k in range(minor.confirmed_i, len(df)):
            if h[k] > minor.price:
                taken = True
                taken_at = ts[k]
                break
        verdict = "HIGH QUALITY (Inducement taken)" if taken else "LOW QUALITY (No inducement taken before zone)"
        note = (f"Minor high at {minor.price:,.1f} swept at {taken_at.strftime('%H:%M') if taken_at else 'N/A'}, "
                f"trapping early shorts before testing {target_pool.pool_id}.") if taken else \
               f"Minor swing at {minor.price:,.1f} remains untaken; zone entry is susceptible to stop hunt."
        return Inducement(
            detected=True, minor_pivot_price=minor.price, target_pool_price=target_pool.pool_price,
            is_taken=taken, taken_time=taken_at, quality_verdict=verdict, note=note
        )

    cands = [p for p in pivots if target_pool.level < p.price < spot]
    if not cands:
        return Inducement(
            detected=False, minor_pivot_price=None, target_pool_price=target_pool.pool_price,
            is_taken=False, taken_time=None,
            quality_verdict="LOW QUALITY (No inducement taken before zone)",
            note=f"Target pool {target_pool.pool_id} approached directly without taking minor inducement first."
        )
    minor = cands[-1]
    taken = False
    taken_at = None
    for k in range(minor.confirmed_i, len(df)):
        if l[k] < minor.price:
            taken = True
            taken_at = ts[k]
            break
    verdict = "HIGH QUALITY (Inducement taken)" if taken else "LOW QUALITY (No inducement taken before zone)"
    note = (f"Minor low at {minor.price:,.1f} swept at {taken_at.strftime('%H:%M') if taken_at else 'N/A'}, "
            f"trapping early longs before testing {target_pool.pool_id}.") if taken else \
           f"Minor swing at {minor.price:,.1f} remains untaken; zone entry is susceptible to stop hunt."
    return Inducement(
        detected=True, minor_pivot_price=minor.price, target_pool_price=target_pool.pool_price,
        is_taken=taken, taken_time=taken_at, quality_verdict=verdict, note=note
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Displacement
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Displacement:
    score: int
    large_body: bool
    small_wicks: bool
    fvg_created: bool
    two_closes_in_dir: bool
    direction: str
    candle_idx: int
    candle_time: Optional[datetime]
    verdict: str
    note: str
    provenance: str = "DERIVED — evaluated on OHLCV candle geometry & FVG alignment."


def evaluate_displacement(
    df: pd.DataFrame,
    gaps: list[smc.FVG],
    atr: float,
    rules: Optional[dict] = None,
) -> Displacement:
    """Grades impulsive move conviction (0 to 4)."""
    rules = rules or load_rules()
    if df is None or len(df) < 3:
        return Displacement(
            score=0, large_body=False, small_wicks=False, fvg_created=False,
            two_closes_in_dir=False, direction="NEUTRAL", candle_idx=-1,
            candle_time=None, verdict="NO DISPLACEMENT / DRIFT",
            note="Insufficient candle history to evaluate displacement.",
        )

    atr_val = max(float(atr), 1.0)
    body_atr_mult = float(rules.get("displacement_body_atr_mult", 1.2))
    max_wick_pct = float(rules.get("displacement_max_wick_pct", 0.30))
    body_range_pct = float(rules.get("displacement_body_range_pct", 0.60))

    o = df["open"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    ts = _times(df).tolist()

    idx = len(df) - 1
    best_disp = Displacement(
        score=0, large_body=False, small_wicks=False, fvg_created=False,
        two_closes_in_dir=False, direction="NEUTRAL", candle_idx=idx,
        candle_time=ts[idx], verdict="NO DISPLACEMENT / DRIFT",
        note="No impulsive displacement observed.",
    )

    for i in range(max(2, len(df) - 3), len(df)):
        body = abs(c[i] - o[i])
        rng = max(h[i] - l[i], 0.1)
        wicks = rng - body
        is_bull = c[i] > o[i]

        c1 = (body >= body_atr_mult * atr_val) or (body / rng >= body_range_pct)
        c2 = (wicks / rng) <= max_wick_pct
        c3 = any(g.i == i for g in gaps)

        if is_bull:
            c4 = (c[i] > c[i - 1] and c[i - 1] > c[max(0, i - 2)]) if i >= 2 else (c[i] > c[i - 1])
            dir_str = "BULLISH"
        else:
            c4 = (c[i] < c[i - 1] and c[i - 1] < c[max(0, i - 2)]) if i >= 2 else (c[i] < c[i - 1])
            dir_str = "BEARISH"

        score = int(c1) + int(c2) + int(c3) + int(c4)
        if score >= best_disp.score:
            verdict = "CONVICTION DISPLACEMENT" if score >= 3 else ("MODERATE" if score == 2 else "NO DISPLACEMENT / DRIFT")
            note_parts = []
            if c1: note_parts.append("large body")
            if c2: note_parts.append("small wicks")
            if c3: note_parts.append("FVG imbalance")
            if c4: note_parts.append("2 consecutive closes")
            note = f"{dir_str} move ({score}/4): " + (", ".join(note_parts) if note_parts else "insignificant drive")
            best_disp = Displacement(
                score=score, large_body=c1, small_wicks=c2, fvg_created=c3,
                two_closes_in_dir=c4, direction=dir_str, candle_idx=i,
                candle_time=ts[i], verdict=verdict, note=note
            )

    return best_disp


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Dealing Range, Premium and Discount
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DealingRangeAnalysis:
    high: float
    low: float
    equilibrium: float
    size: float
    spot: float
    position_pct: float
    zone: str
    in_ote: bool
    ote_low: float
    ote_high: float
    bands: dict[str, float]
    verdict: str
    note: str
    provenance: str = "DERIVED — active swing low to high dealing range. OTE is confirmation, never standalone entry."


def evaluate_dealing_range(
    pivots: list[smc.Pivot],
    spot: float,
    rules: Optional[dict] = None,
) -> DealingRangeAnalysis:
    """Computes dealing range retracement bands and classifies spot position."""
    rules = rules or load_rules()
    eq_buf = float(rules.get("equilibrium_band_pct", 0.03))

    high_pivots = [p for p in pivots if p.kind == "HIGH"]
    low_pivots = [p for p in pivots if p.kind == "LOW"]

    if not high_pivots or not low_pivots:
        return DealingRangeAnalysis(
            high=spot + 50.0, low=spot - 50.0, equilibrium=spot, size=100.0,
            spot=spot, position_pct=0.5, zone="EQUILIBRIUM", in_ote=False,
            ote_low=spot - 10.0, ote_high=spot + 10.0,
            bands={"0.0": spot - 50, "0.5": spot, "1.0": spot + 50},
            verdict="INSUFFICIENT PIVOTS",
            note="Dealing range cannot be anchored without both confirmed swing high and low.",
        )

    active_h = high_pivots[-1].price
    active_l = low_pivots[-1].price

    if active_h <= active_l:
        recent_ps = pivots[-6:]
        active_h = max(p.price for p in recent_ps)
        active_l = min(p.price for p in recent_ps)
        if active_h <= active_l:
            active_h = spot + 50.0
            active_l = spot - 50.0

    size = max(active_h - active_l, 1.0)
    eq = (active_h + active_l) / 2.0
    pos_pct = max(0.0, min(1.0, (spot - active_l) / size))

    bands = {
        "0.000": round(active_l, 1),
        "0.250": round(active_l + 0.25 * size, 1),
        "0.500": round(eq, 1),
        "0.618": round(active_l + 0.618 * size, 1),
        "0.705": round(active_l + 0.705 * size, 1),
        "0.790": round(active_l + 0.790 * size, 1),
        "1.000": round(active_h, 1),
    }

    if spot > eq + (eq_buf * size):
        zone = "PREMIUM"
        action = "Look for institutional sell setups"
    elif spot < eq - (eq_buf * size):
        zone = "DISCOUNT"
        action = "Look for institutional buy setups"
    else:
        zone = "EQUILIBRIUM"
        action = "Fair value compression — avoid fresh initiating entries"

    ote_low = bands["0.618"]
    ote_high = bands["0.790"]
    in_ote = ote_low <= spot <= ote_high

    ote_text = "Spot is in OTE (62%–79% Retracement). OTE is a confirmation, NEVER a standalone entry." if in_ote else \
               "Outside OTE zone. OTE is a confirmation, NEVER a standalone entry."

    note = f"Spot at {pos_pct*100:.1f}% of active dealing range [{active_l:,.1f}–{active_h:,.1f}]. {action}. {ote_text}"
    verdict = f"{zone} ({pos_pct*100:.0f}%)" + (" · IN OTE" if in_ote else "")

    return DealingRangeAnalysis(
        high=active_h, low=active_l, equilibrium=eq, size=size, spot=spot,
        position_pct=pos_pct, zone=zone, in_ote=in_ote,
        ote_low=ote_low, ote_high=ote_high, bands=bands, verdict=verdict, note=note
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Breaker and Mitigation Blocks
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BreakerMitigationItem:
    block_type: str
    direction: str
    origin_ob_direction: str
    top: float
    bottom: float
    mid: float
    origin_bar_i: int
    broken_bar_i: int
    confirmed_broken: bool
    origin_took_liquidity: bool
    retested: bool
    state: str
    note: str
    provenance: str = "DERIVED — failed smc.order_blocks promoted upon two-close violation."


@dataclass
class BreakerMitigation:
    items: list[BreakerMitigationItem] = field(default_factory=list)
    active_breaker: Optional[BreakerMitigationItem] = None
    breaker_count: int = 0
    mitigation_count: int = 0
    provenance: str = "DERIVED — failed order blocks promoted to Breakers (took liquidity) or Mitigation (failed swing)."


def evaluate_breaker_mitigation(
    df: pd.DataFrame,
    order_blocks: list[smc.OrderBlock],
    pivots: list[smc.Pivot],
) -> BreakerMitigation:
    """Tracks failed order blocks and promotes them to Breakers or Mitigation blocks."""
    res = BreakerMitigation()
    if df is None or df.empty or not order_blocks:
        return res

    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    n_bars = len(df)

    items: list[BreakerMitigationItem] = []

    for ob in order_blocks:
        is_bull_ob = ob.direction == "BULLISH"
        broken_i = None
        two_close_confirmed = False

        for k in range(ob.i + 1, n_bars):
            if is_bull_ob:
                if c[k] < ob.low:
                    if k + 1 < n_bars:
                        if c[k + 1] < ob.low:
                            broken_i = k + 1
                            two_close_confirmed = True
                            break
                    else:
                        broken_i = k
                        two_close_confirmed = False
                        break
            else:
                if c[k] > ob.high:
                    if k + 1 < n_bars:
                        if c[k + 1] > ob.high:
                            broken_i = k + 1
                            two_close_confirmed = True
                            break
                    else:
                        broken_i = k
                        two_close_confirmed = False
                        break

        if broken_i is None:
            continue

        prior_pivots = [p for p in pivots if p.confirmed_i <= ob.i]
        took_liq = False
        if prior_pivots:
            if is_bull_ob:
                prior_highs = [p.price for p in prior_pivots if p.kind == "HIGH"]
                if prior_highs and max(h[max(0, ob.i - 5):ob.i + 1]) >= max(prior_highs):
                    took_liq = True
            else:
                prior_lows = [p.price for p in prior_pivots if p.kind == "LOW"]
                if prior_lows and min(l[max(0, ob.i - 5):ob.i + 1]) <= min(prior_lows):
                    took_liq = True

        block_type = "BREAKER" if took_liq else "MITIGATION"
        new_dir = "BEARISH" if is_bull_ob else "BULLISH"

        retested = False
        if two_close_confirmed and broken_i < n_bars - 1:
            for m in range(broken_i + 1, n_bars):
                if ob.low <= h[m] and l[m] <= ob.high:
                    retested = True
                    break

        state = "RETESTED" if retested else ("ACTIVE" if two_close_confirmed else "WAITING")
        desc = ("took out prior extreme before failing (Breaker)" if took_liq else
                "did not sweep prior extreme (Mitigation failure swing)")
        note = f"{new_dir} {block_type} [{ob.low:,.1f}–{ob.high:,.1f}] — failed {ob.direction} OB {desc}."

        items.append(BreakerMitigationItem(
            block_type=block_type, direction=new_dir, origin_ob_direction=ob.direction,
            top=ob.high, bottom=ob.low, mid=(ob.high + ob.low) / 2.0,
            origin_bar_i=ob.i, broken_bar_i=broken_i,
            confirmed_broken=two_close_confirmed, origin_took_liquidity=took_liq,
            retested=retested, state=state, note=note
        ))

    b_cnt = sum(1 for it in items if it.block_type == "BREAKER")
    m_cnt = sum(1 for it in items if it.block_type == "MITIGATION")
    active_b = next((it for it in reversed(items) if it.state == "ACTIVE"), None)

    res.items = items
    res.active_breaker = active_b
    res.breaker_count = b_cnt
    res.mitigation_count = m_cnt
    return res


# ═══════════════════════════════════════════════════════════════════════════
# 6 · Balanced Price Range and Consequent Encroachment
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BPRAndCE:
    bprs: list[dict] = field(default_factory=list)
    ces: list[dict] = field(default_factory=list)
    active_bpr: Optional[dict] = None
    bpr_count: int = 0
    ce_count: int = 0
    note: str = ""
    provenance: str = "DERIVED — overlapping opposing FVGs (BPR) and FVG 50% consequent encroachment."


def evaluate_bpr_and_ce(
    df: pd.DataFrame,
    gaps: list[smc.FVG],
) -> BPRAndCE:
    """Balanced Price Range (BPR) and Consequent Encroachment (CE) derivation."""
    res = BPRAndCE()
    if df is None or df.empty or not gaps:
        return res

    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    n_bars = len(df)

    bull_gaps = [g for g in gaps if g.direction == "BULLISH"]
    bear_gaps = [g for g in gaps if g.direction == "BEARISH"]

    bprs: list[dict] = []
    for bg in bull_gaps:
        for sg in bear_gaps:
            b_bottom = max(bg.low, sg.low)
            b_top = min(bg.high, sg.high)
            if b_top > b_bottom:
                latest_i = max(bg.i, sg.i)
                retested = False
                for k in range(latest_i + 1, n_bars):
                    if l[k] <= b_top and h[k] >= b_bottom:
                        retested = True
                        break
                bprs.append({
                    "top": round(b_top, 1),
                    "bottom": round(b_bottom, 1),
                    "mid": round((b_top + b_bottom) / 2.0, 1),
                    "bull_i": bg.i,
                    "bear_i": sg.i,
                    "state": "RETESTED" if retested else "ACTIVE",
                })

    ces: list[dict] = []
    for g in gaps:
        ce_lvl = (g.low + g.high) / 2.0
        retested = False
        for k in range(g.i + 1, n_bars):
            if l[k] <= ce_lvl <= h[k]:
                retested = True
                break
        ces.append({
            "fvg_id": f"FVG_{g.direction}_{g.i}",
            "direction": g.direction,
            "ce_level": round(ce_lvl, 1),
            "retested": retested,
            "open": g.open,
        })

    active_b = next((b for b in reversed(bprs) if b["state"] == "ACTIVE"), None)
    note = f"{len(bprs)} BPR zones, {len(ces)} CE levels tracked. BPR represents high-conviction fair value equilibrium."

    res.bprs = bprs
    res.ces = ces
    res.active_bpr = active_b
    res.bpr_count = len(bprs)
    res.ce_count = len(ces)
    res.note = note
    return res


# ═══════════════════════════════════════════════════════════════════════════
# 7 · Turtle Soup / False Break
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TurtleSoup:
    detected: bool
    level_name: str
    level_price: float
    raid_direction: str
    bars_outside: int
    reclaimed: bool
    reclaim_state: str
    bar_index: int
    time: Optional[datetime]
    sentiment: str
    note: str
    provenance: str = "DERIVED — prior session extreme raid with swift reclaim inside N bars."


def evaluate_turtle_soup(
    df: pd.DataFrame,
    ohlc_daily: Optional[pd.DataFrame],
    rules: Optional[dict] = None,
) -> TurtleSoup:
    """Classic Turtle Soup / false break of prior day or week extremes."""
    rules = rules or load_rules()
    max_bars = int(rules.get("turtle_soup_max_bars", 5))

    default_res = TurtleSoup(
        detected=False, level_name="", level_price=0.0, raid_direction="",
        bars_outside=0, reclaimed=False, reclaim_state="NONE", bar_index=-1,
        time=None, sentiment="NEUTRAL",
        note="No Turtle Soup false break detected on prior session extremes."
    )

    if df is None or df.empty or len(df) < 3 or ohlc_daily is None or ohlc_daily.empty:
        return default_res

    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    ts = _times(df).tolist()
    n_bars = len(df)

    c_map = {str(col).lower(): col for col in ohlc_daily.columns}
    if "high" not in c_map or "low" not in c_map or len(ohlc_daily) < 2:
        return default_res

    pdh = float(ohlc_daily[c_map["high"]].iloc[-2])
    pdl = float(ohlc_daily[c_map["low"]].iloc[-2])

    levels_to_check = [("PDH", pdh, "ABOVE"), ("PDL", pdl, "BELOW")]

    for name, lvl, direction in levels_to_check:
        for i in range(max(1, n_bars - 15), n_bars):
            raided = (h[i] > lvl) if direction == "ABOVE" else (l[i] < lvl)
            if not raided:
                continue

            bars_out = 0
            reclaimed_confirmed = False
            reclaim_state = "OUTSIDE"

            for j in range(i, min(i + max_bars + 1, n_bars)):
                is_outside = (c[j] > lvl) if direction == "ABOVE" else (c[j] < lvl)
                if is_outside:
                    bars_out += 1
                else:
                    if j + 1 < n_bars:
                        second_inside = (c[j + 1] <= lvl) if direction == "ABOVE" else (c[j + 1] >= lvl)
                        if second_inside:
                            reclaimed_confirmed = True
                            reclaim_state = "CONFIRMED"
                            break
                        else:
                            reclaim_state = "WAITING"
                    else:
                        reclaim_state = "WAITING"
                    break

            if reclaim_state in ("CONFIRMED", "WAITING"):
                sent = "BEARISH_REVERSAL" if direction == "ABOVE" else "BULLISH_REVERSAL"
                note = (f"Turtle Soup on {name} ({lvl:,.1f}): price pierced {direction.lower()} "
                        f"for {bars_out} bars and closed back inside ({reclaim_state}). "
                        f"Classic stop raid on prior session extreme.")
                return TurtleSoup(
                    detected=True, level_name=name, level_price=lvl, raid_direction=direction,
                    bars_outside=bars_out, reclaimed=reclaimed_confirmed,
                    reclaim_state=reclaim_state, bar_index=i, time=ts[i],
                    sentiment=sent, note=note
                )

    return default_res


# ═══════════════════════════════════════════════════════════════════════════
# 8 · Session Model (NSE Specific)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SessionWindowInfo:
    name: str
    label: str
    start_time: str
    end_time: str
    is_active: bool
    is_completed: bool
    high: Optional[float]
    low: Optional[float]
    range_pts: Optional[float]
    character: str


@dataclass
class SessionModel:
    current_time: str
    active_window: Optional[SessionWindowInfo]
    windows: list[SessionWindowInfo]
    day_high: float
    day_low: float
    day_range: float
    high_window: str
    low_window: str
    pending_extreme: str
    note: str
    provenance: str = "DERIVED — NSE 09:15–15:30 auction session model. 15:15–15:30 auction excluded."


def evaluate_session_model(
    df: pd.DataFrame,
    rules: Optional[dict] = None,
) -> SessionModel:
    """Session model derived strictly from NSE market structure (09:15–15:30 IST)."""
    rules = rules or load_rules()
    window_defs = rules.get("session_windows", [
        {"start": "09:15", "end": "09:30", "name": "Opening auction settling", "character": "Reference only — first candle not traded"},
        {"start": "09:30", "end": "10:30", "name": "Primary directional window", "character": "Most days' range is set here"},
        {"start": "10:30", "end": "11:30", "name": "Continuation or first reversal", "character": "Continuation or initial session reversal"},
        {"start": "11:30", "end": "13:15", "name": "Midday compression", "character": "Midday compression — no fresh entries"},
        {"start": "13:15", "end": "14:30", "name": "Second directional window", "character": "Afternoon directional expansion"},
        {"start": "14:30", "end": "15:15", "name": "Closing drive", "character": "The day's other extreme often prints here"},
        {"start": "15:15", "end": "15:30", "name": "Closing call auction", "character": "Exclude entirely — auction artefact"},
    ])

    if df is None or df.empty:
        return SessionModel(
            current_time="09:15", active_window=None, windows=[],
            day_high=0.0, day_low=0.0, day_range=0.0, high_window="None",
            low_window="None", pending_extreme="NEITHER",
            note="No session candle data available."
        )

    ts_series = _times(df)
    now_t = ts_series.iloc[-1].time() if not ts_series.empty else time(9, 15)
    now_str = now_t.strftime("%H:%M")

    mask_trade = (ts_series.dt.time >= time(9, 15)) & (ts_series.dt.time < time(15, 15))
    trade_df = df[mask_trade] if mask_trade.any() else df

    d_high = float(trade_df["high"].max()) if not trade_df.empty else 0.0
    d_low = float(trade_df["low"].min()) if not trade_df.empty else 0.0
    d_range = round(d_high - d_low, 1)

    windows_info: list[SessionWindowInfo] = []
    active_win: Optional[SessionWindowInfo] = None
    hi_win_name = "None"
    lo_win_name = "None"

    for w in window_defs:
        st_parts = [int(p) for p in w["start"].split(":")]
        et_parts = [int(p) for p in w["end"].split(":")]
        t_start = time(st_parts[0], st_parts[1])
        t_end = time(et_parts[0], et_parts[1])

        is_act = t_start <= now_t < t_end
        is_comp = now_t >= t_end

        w_mask = (ts_series.dt.time >= t_start) & (ts_series.dt.time < t_end)
        w_df = df[w_mask]

        if not w_df.empty:
            w_h = float(w_df["high"].max())
            w_l = float(w_df["low"].min())
            w_rng = round(w_h - w_l, 1)
            if abs(w_h - d_high) < 1.0:
                hi_win_name = w["name"]
            if abs(w_l - d_low) < 1.0:
                lo_win_name = w["name"]
        else:
            w_h, w_l, w_rng = None, None, None

        win_info = SessionWindowInfo(
            name=f"{w['start']}–{w['end']}", label=w["name"],
            start_time=w["start"], end_time=w["end"],
            is_active=is_act, is_completed=is_comp,
            high=w_h, low=w_l, range_pts=w_rng,
            character=w.get("character", "")
        )
        windows_info.append(win_info)
        if is_act:
            active_win = win_info

    if now_t >= time(13, 15):
        if "Primary" in hi_win_name and "Primary" not in lo_win_name and "Continuation" not in lo_win_name:
            pending = "LOW"
        elif "Primary" in lo_win_name and "Primary" not in hi_win_name and "Continuation" not in hi_win_name:
            pending = "HIGH"
        else:
            pending = "BOTH PRINTED"
    else:
        pending = "BOTH PENDING / DEVELOPING"

    note = (f"Active window: {active_win.label if active_win else 'After-hours / Closed'}. "
            f"Day High printed in '{hi_win_name}', Low in '{lo_win_name}'. "
            f"Pending extreme: {pending}.")

    return SessionModel(
        current_time=now_str, active_window=active_win, windows=windows_info,
        day_high=d_high, day_low=d_low, day_range=d_range,
        high_window=hi_win_name, low_window=lo_win_name,
        pending_extreme=pending, note=note
    )


# ═══════════════════════════════════════════════════════════════════════════
# 9 · Judas Swing
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class JudasSwing:
    detected: bool
    direction: str
    initial_move_pts: float
    initial_move_atr: float
    reversal_confirmed: bool
    confirmation_time: Optional[datetime]
    confirmation_bar: Optional[int]
    status: str
    note: str
    provenance: str = "DERIVED — first-hour false break reversed with confirmed displacement."


def evaluate_judas_swing(
    df: pd.DataFrame,
    disp: Displacement,
    atr: float,
    rules: Optional[dict] = None,
) -> JudasSwing:
    """False directional move in first hour that reverses with confirmed displacement."""
    rules = rules or load_rules()
    min_atr = float(rules.get("judas_min_move_atr", 0.8))
    default_res = JudasSwing(
        detected=False, direction="NONE", initial_move_pts=0.0, initial_move_atr=0.0,
        reversal_confirmed=False, confirmation_time=None, confirmation_bar=None,
        status="NONE", note="No confirmed Judas swing detected in the session."
    )

    if df is None or len(df) < 5:
        return default_res

    atr_val = max(float(atr), 1.0)
    ts = _times(df)
    o = df["open"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()

    first_hour_mask = (ts.dt.time >= time(9, 15)) & (ts.dt.time <= time(10, 15))
    if not first_hour_mask.any():
        return default_res

    fh_indices = np.where(first_hour_mask)[0]
    if len(fh_indices) < 3:
        return default_res

    open_price = float(o[fh_indices[0]])
    fh_max_h = float(h[fh_indices].max())
    fh_min_l = float(l[fh_indices].min())

    up_move = fh_max_h - open_price
    down_move = open_price - fh_min_l

    if up_move >= min_atr * atr_val:
        subseq_bars = np.where(ts.dt.time > time(10, 15))[0]
        if len(subseq_bars) >= 2:
            recent_c = c[subseq_bars]
            if np.any(recent_c < open_price) and disp.score >= 3 and disp.direction == "BEARISH":
                conf_bar = int(subseq_bars[-1])
                return JudasSwing(
                    detected=True, direction="BULL_TRAP", initial_move_pts=round(up_move, 1),
                    initial_move_atr=round(up_move / atr_val, 2), reversal_confirmed=True,
                    confirmation_time=ts.iloc[conf_bar], confirmation_bar=conf_bar,
                    status="CONFIRMED",
                    note=f"Judas Swing Bull Trap confirmed: morning spike of {up_move:.1f} pts ({up_move/atr_val:.1f} ATR) "
                         f"fully retraced with conviction bearish displacement."
                )

    if down_move >= min_atr * atr_val:
        subseq_bars = np.where(ts.dt.time > time(10, 15))[0]
        if len(subseq_bars) >= 2:
            recent_c = c[subseq_bars]
            if np.any(recent_c > open_price) and disp.score >= 3 and disp.direction == "BULLISH":
                conf_bar = int(subseq_bars[-1])
                return JudasSwing(
                    detected=True, direction="BEAR_TRAP", initial_move_pts=round(down_move, 1),
                    initial_move_atr=round(down_move / atr_val, 2), reversal_confirmed=True,
                    confirmation_time=ts.iloc[conf_bar], confirmation_bar=conf_bar,
                    status="CONFIRMED",
                    note=f"Judas Swing Bear Trap confirmed: morning dump of {down_move:.1f} pts ({down_move/atr_val:.1f} ATR) "
                         f"fully retraced with conviction bullish displacement."
                )

    return default_res


# ═══════════════════════════════════════════════════════════════════════════
# 10 · SMT Divergence (NIFTY against BANKNIFTY)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SMTDivergence:
    available: bool
    divergence_type: str
    nifty_swing: str
    banknifty_swing: str
    pool_level: Optional[float]
    sentiment: str
    status_label: str
    note: str
    provenance: str = "OBSERVED — inter-market correlation between NIFTY and BANKNIFTY at liquidity pools."


def _align_banknifty_df(nifty_df: pd.DataFrame, banknifty_df: pd.DataFrame) -> pd.DataFrame:
    """Align BANKNIFTY candles resolution to match NIFTY candles if needed."""
    if banknifty_df is None or banknifty_df.empty or len(banknifty_df) < 2:
        return banknifty_df
    if nifty_df is None or nifty_df.empty or len(nifty_df) < 2:
        return banknifty_df

    if "timestamp" not in nifty_df.columns or "timestamp" not in banknifty_df.columns:
        return banknifty_df

    try:
        n_ts = pd.to_datetime(nifty_df["timestamp"], errors="coerce")
        b_ts = pd.to_datetime(banknifty_df["timestamp"], errors="coerce")
        n_deltas = n_ts.diff().dropna().dt.total_seconds() / 60.0
        b_deltas = b_ts.diff().dropna().dt.total_seconds() / 60.0
        n_med = round(n_deltas.median()) if not n_deltas.empty else 1
        b_med = round(b_deltas.median()) if not b_deltas.empty else 1
        if n_med > 1 and b_med < n_med:
            import price_action as _pa
            return _pa.resample(banknifty_df, int(n_med))
    except Exception:
        pass
    return banknifty_df


def evaluate_smt_divergence(
    nifty_df: pd.DataFrame,
    banknifty_df: Optional[pd.DataFrame],
    rules: Optional[dict] = None,
) -> SMTDivergence:
    """SMT Divergence: NIFTY against BANKNIFTY."""
    if banknifty_df is None or banknifty_df.empty or len(banknifty_df) < 5:
        return SMTDivergence(
            available=False, divergence_type="UNAVAILABLE", nifty_swing="N/A",
            banknifty_swing="N/A", pool_level=None, sentiment="NEUTRAL",
            status_label="UNAVAILABLE — needs BANKNIFTY feed",
            note="Requires live continuous BANKNIFTY candle feed. Proxy substitutions strictly forbidden."
        )

    banknifty_df = _align_banknifty_df(nifty_df, banknifty_df)
    if banknifty_df is None or banknifty_df.empty or len(banknifty_df) < 5:
        return SMTDivergence(
            available=False, divergence_type="UNAVAILABLE", nifty_swing="N/A",
            banknifty_swing="N/A", pool_level=None, sentiment="NEUTRAL",
            status_label="UNAVAILABLE — needs BANKNIFTY feed",
            note="Insufficient BANKNIFTY candles after alignment. Minimum 5 bars required."
        )

    r = rules or {}
    p_n = int(r.get("pivot_window_n", 2))
    n_pivots = smc.pivots(nifty_df, n=p_n)
    b_pivots = smc.pivots(banknifty_df, n=p_n)

    n_highs = [p for p in n_pivots if p.kind == "HIGH"]
    b_highs = [p for p in b_pivots if p.kind == "HIGH"]
    n_lows = [p for p in n_pivots if p.kind == "LOW"]
    b_lows = [p for p in b_pivots if p.kind == "LOW"]

    if len(n_highs) >= 2 and len(b_highs) >= 2:
        n_hh = n_highs[-1].price > n_highs[-2].price
        b_lh = b_highs[-1].price < b_highs[-2].price
        if n_hh and b_lh:
            return SMTDivergence(
                available=True, divergence_type="BEARISH_SMT",
                nifty_swing="HIGHER_HIGH", banknifty_swing="LOWER_HIGH",
                pool_level=n_highs[-1].price, sentiment="BEARISH",
                status_label="BEARISH SMT DIVERGENCE",
                note=f"Bearish SMT: NIFTY printed Higher High ({n_highs[-1].price:,.1f}) while BANKNIFTY failed to confirm (Lower High)."
            )

        n_lh = n_highs[-1].price < n_highs[-2].price
        b_hh = b_highs[-1].price > b_highs[-2].price
        if n_lh and b_hh:
            return SMTDivergence(
                available=True, divergence_type="BEARISH_SMT",
                nifty_swing="LOWER_HIGH", banknifty_swing="HIGHER_HIGH",
                pool_level=n_highs[-1].price, sentiment="BEARISH",
                status_label="BEARISH SMT DIVERGENCE",
                note=f"Bearish SMT: BANKNIFTY printed Higher High while NIFTY failed to confirm (Lower High)."
            )

    if len(n_lows) >= 2 and len(b_lows) >= 2:
        n_ll = n_lows[-1].price < n_lows[-2].price
        b_hl = b_lows[-1].price > b_lows[-2].price
        if n_ll and b_hl:
            return SMTDivergence(
                available=True, divergence_type="BULLISH_SMT",
                nifty_swing="LOWER_LOW", banknifty_swing="HIGHER_LOW",
                pool_level=n_lows[-1].price, sentiment="BULLISH",
                status_label="BULLISH SMT DIVERGENCE",
                note=f"Bullish SMT: NIFTY printed Lower Low ({n_lows[-1].price:,.1f}) while BANKNIFTY held Higher Low."
            )

        n_hl = n_lows[-1].price > n_lows[-2].price
        b_ll = b_lows[-1].price < b_lows[-2].price
        if n_hl and b_ll:
            return SMTDivergence(
                available=True, divergence_type="BULLISH_SMT",
                nifty_swing="HIGHER_LOW", banknifty_swing="LOWER_LOW",
                pool_level=n_lows[-1].price, sentiment="BULLISH",
                status_label="BULLISH SMT DIVERGENCE",
                note=f"Bullish SMT: BANKNIFTY printed Lower Low while NIFTY held Higher Low."
            )

    return SMTDivergence(
        available=True, divergence_type="NO_DIVERGENCE",
        nifty_swing="ALIGNED", banknifty_swing="ALIGNED",
        pool_level=None, sentiment="NEUTRAL",
        status_label="ALIGNED — no SMT divergence",
        note="Both NIFTY and BANKNIFTY swings are aligned in direction."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 11 · Power of Three (AMD)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PowerOfThree:
    detected: bool
    phase: str
    direction: str
    anchor_level_name: str
    anchor_price: float
    consolidation_range: tuple[float, float]
    manipulation_extreme: Optional[float]
    distribution_target: Optional[float]
    confirmed: bool
    note: str
    provenance: str = "DERIVED — Accumulation, Manipulation, Distribution valid ONLY at known pool/wall/extreme."


def evaluate_power_of_three(
    df: pd.DataFrame,
    pool_map: LiquidityPoolMap,
    disp: Displacement,
    atr: float,
    rules: Optional[dict] = None,
) -> PowerOfThree:
    """Power of Three (AMD) at known structural levels."""
    rules = rules or load_rules()
    min_bars = int(rules.get("amd_min_consolidation_bars", 4))
    max_range_mult = float(rules.get("amd_max_range_atr", 1.0))

    default_res = PowerOfThree(
        detected=False, phase="NONE", direction="NONE", anchor_level_name="",
        anchor_price=0.0, consolidation_range=(0.0, 0.0), manipulation_extreme=None,
        distribution_target=None, confirmed=False,
        note="No Power of Three (AMD) sequence active at a known structural level."
    )

    if df is None or len(df) < min_bars + 3:
        return default_res

    atr_val = max(float(atr), 1.0)
    h = df["high"].astype(float).to_numpy()
    l = df["low"].astype(float).to_numpy()
    c = df["close"].astype(float).to_numpy()
    n_bars = len(df)

    key_pools = [p for p in pool_map.pools if p.weight >= 3.0][:6]
    if not key_pools:
        return default_res

    for pool in key_pools:
        lvl = pool.level
        w_start = max(0, n_bars - 8)
        w_end = max(1, n_bars - 2)
        sub_h = h[w_start:w_end]
        sub_l = l[w_start:w_end]
        if len(sub_h) < min_bars:
            continue

        c_high = float(sub_h.max())
        c_low = float(sub_l.min())
        c_rng = c_high - c_low

        if c_rng <= max_range_mult * atr_val and (abs(c_high - lvl) <= 15.0 or abs(c_low - lvl) <= 15.0):
            pen_high = float(h[w_end:].max())
            pen_low = float(l[w_end:].min())

            if pen_high > c_high and c[-1] < c_low and disp.score >= 2 and disp.direction == "BEARISH":
                return PowerOfThree(
                    detected=True, phase="DISTRIBUTION", direction="BEARISH_AMD",
                    anchor_level_name=pool.source, anchor_price=lvl,
                    consolidation_range=(round(c_low, 1), round(c_high, 1)),
                    manipulation_extreme=round(pen_high, 1),
                    distribution_target=round(c_low - c_rng, 1),
                    confirmed=disp.score >= 3,
                    note=f"Bearish AMD at {pool.source} ({lvl:,.1f}): Accumulation [{c_low:,.0f}–{c_high:,.0f}], "
                         f"Manipulation wick to {pen_high:,.1f}, followed by impulsive Distribution lower."
                )

            if pen_low < c_low and c[-1] > c_high and disp.score >= 2 and disp.direction == "BULLISH":
                return PowerOfThree(
                    detected=True, phase="DISTRIBUTION", direction="BULLISH_AMD",
                    anchor_level_name=pool.source, anchor_price=lvl,
                    consolidation_range=(round(c_low, 1), round(c_high, 1)),
                    manipulation_extreme=round(pen_low, 1),
                    distribution_target=round(c_high + c_rng, 1),
                    confirmed=disp.score >= 3,
                    note=f"Bullish AMD at {pool.source} ({lvl:,.1f}): Accumulation [{c_low:,.0f}–{c_high:,.0f}], "
                         f"Manipulation wick to {pen_low:,.1f}, followed by impulsive Distribution higher."
                )

    return default_res


# ═══════════════════════════════════════════════════════════════════════════
# 12 · Confluence Scoring
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ConfluenceFactor:
    name: str
    detail: str
    strength: str
    sentiment: str


@dataclass
class ConfluenceScore:
    zone_price: float
    zone_name: str
    total_factors: int
    strong_factors_count: int
    factors: list[ConfluenceFactor]
    verdict: str
    sentiment: str
    note: str
    provenance: str = "COMPOSITE — independent factor count without collapsing into a single percentage."


def evaluate_confluence(
    spot: float,
    pool_map: LiquidityPoolMap,
    inducement: Inducement,
    disp: Displacement,
    dr: DealingRangeAnalysis,
    breakers: BreakerMitigation,
    bpr_ce: BPRAndCE,
    chain_levels: Optional[Any],
    candles: pd.DataFrame,
    ohlc_daily: Optional[pd.DataFrame],
    rules: Optional[dict] = None,
) -> ConfluenceScore:
    """Counts independent agreeing factors for candidate zone in play."""
    rules = rules or load_rules()
    prox = float(rules.get("confluence_proximity_pts", 25.0))

    cand_pool = pool_map.nearest_above if (dr.zone == "PREMIUM" or spot < (pool_map.nearest_above.pool_price if pool_map.nearest_above else spot)) else pool_map.nearest_below
    if cand_pool is None:
        cand_pool = pool_map.nearest_above or pool_map.nearest_below

    zone_price = cand_pool.level if cand_pool else spot
    zone_name = cand_pool.source if cand_pool else "Spot Zone"

    factors: list[ConfluenceFactor] = []

    # 1. Pool Present & Weight
    if cand_pool:
        st_str = "STRONG" if cand_pool.weight >= 4.0 else "MODERATE"
        factors.append(ConfluenceFactor(
            name="Liquidity Pool Present",
            detail=f"{cand_pool.pool_type} at {cand_pool.pool_price:,.1f} (Weight: {cand_pool.weight:.1f}, {cand_pool.state})",
            strength=st_str,
            sentiment="BEARISH" if cand_pool.pool_side == "BUY_STOPS" else "BULLISH"
        ))

    # 2. Inducement Taken Before Arrival
    if inducement.detected and inducement.is_taken:
        factors.append(ConfluenceFactor(
            name="Inducement Taken First",
            detail=f"Minor swing at {inducement.minor_pivot_price:,.1f} swept prior to zone arrival",
            strength="STRONG",
            sentiment="BEARISH" if "BUY" in (cand_pool.pool_side if cand_pool else "") else "BULLISH"
        ))

    # 3. Displacement on Approach
    if disp.score >= 3:
        factors.append(ConfluenceFactor(
            name="Displacement Impulsive Drive",
            detail=f"{disp.direction} displacement ({disp.score}/4 criteria met)",
            strength="STRONG",
            sentiment=disp.direction
        ))
    elif disp.score == 2:
        factors.append(ConfluenceFactor(
            name="Moderate Displacement",
            detail=f"{disp.direction} moderate impulse (2/4 criteria met)",
            strength="MODERATE",
            sentiment=disp.direction
        ))

    # 4. Premium / Discount Alignment
    if dr.zone == "PREMIUM" and (cand_pool and cand_pool.pool_side == "BUY_STOPS"):
        factors.append(ConfluenceFactor(
            name="Premium Zone Alignment",
            detail=f"Spot at {dr.position_pct*100:.0f}% of dealing range — favorable institutional sell zone",
            strength="STRONG",
            sentiment="BEARISH"
        ))
    elif dr.zone == "DISCOUNT" and (cand_pool and cand_pool.pool_side == "SELL_STOPS"):
        factors.append(ConfluenceFactor(
            name="Discount Zone Alignment",
            detail=f"Spot at {dr.position_pct*100:.0f}% of dealing range — favorable institutional buy zone",
            strength="STRONG",
            sentiment="BULLISH"
        ))

    # 5. Order Block / Breaker / FVG Present
    if breakers.active_breaker and abs(breakers.active_breaker.mid - zone_price) <= prox:
        factors.append(ConfluenceFactor(
            name="Breaker / Mitigation Block",
            detail=f"{breakers.active_breaker.direction} {breakers.active_breaker.block_type} at {breakers.active_breaker.mid:,.1f}",
            strength="STRONG",
            sentiment=breakers.active_breaker.direction
        ))
    elif bpr_ce.active_bpr and abs(bpr_ce.active_bpr["mid"] - zone_price) <= prox:
        factors.append(ConfluenceFactor(
            name="Balanced Price Range (BPR)",
            detail=f"Opposing FVG overlap at {bpr_ce.active_bpr['mid']:,.1f}",
            strength="STRONG",
            sentiment="NEUTRAL"
        ))

    # 6. Option Chain Wall Agreement
    if chain_levels:
        for attr, label, sent in [("r1", "Call Wall R1", "BEARISH"), ("r2", "Call Wall R2", "BEARISH"),
                                  ("s1", "Put Wall S1", "BULLISH"), ("s2", "Put Wall S2", "BULLISH")]:
            val = getattr(chain_levels, attr, None)
            if val and abs(float(val) - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Option Chain Wall",
                    detail=f"{label} at {float(val):,.0f} defending zone",
                    strength="STRONG",
                    sentiment=sent
                ))
                break

    # 7. Volume Profile Confluence (POC / Value Area)
    try:
        prof = order_flow.volume_profile(candles)
        if prof:
            if abs(prof.poc - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Volume Profile POC",
                    detail=f"Point of Control at {prof.poc:,.1f} coincides with zone",
                    strength="STRONG",
                    sentiment="NEUTRAL"
                ))
            elif abs(prof.vah - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Volume Profile VAH",
                    detail=f"Value Area High at {prof.vah:,.1f} provides upper boundary resistance",
                    strength="MODERATE",
                    sentiment="BEARISH"
                ))
            elif abs(prof.val - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Volume Profile VAL",
                    detail=f"Value Area Low at {prof.val:,.1f} provides lower boundary support",
                    strength="MODERATE",
                    sentiment="BULLISH"
                ))
    except Exception:
        pass

    # 8. Higher Timeframe Level Alignment
    if ohlc_daily is not None and not ohlc_daily.empty and len(ohlc_daily) >= 2:
        c_map = {str(col).lower(): col for col in ohlc_daily.columns}
        if "high" in c_map and "low" in c_map:
            pdh = float(ohlc_daily[c_map["high"]].iloc[-2])
            pdl = float(ohlc_daily[c_map["low"]].iloc[-2])
            if abs(pdh - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Prior Day High (PDH)",
                    detail=f"Daily high at {pdh:,.1f} aligns with candidate zone",
                    strength="STRONG",
                    sentiment="BEARISH"
                ))
            elif abs(pdl - zone_price) <= prox:
                factors.append(ConfluenceFactor(
                    name="Prior Day Low (PDL)",
                    detail=f"Daily low at {pdl:,.1f} aligns with candidate zone",
                    strength="STRONG",
                    sentiment="BULLISH"
                ))

    strong_cnt = sum(1 for f in factors if f.strength == "STRONG")
    tot_cnt = len(factors)

    bull_f = sum(1 for f in factors if f.sentiment == "BULLISH")
    bear_f = sum(1 for f in factors if f.sentiment == "BEARISH")
    sent_str = "BULLISH" if bull_f > bear_f else ("BEARISH" if bear_f > bull_f else "NEUTRAL")

    verdict = f"{tot_cnt} Factors ({strong_cnt} Strong, {tot_cnt - strong_cnt} Moderate)"
    note = f"Confluence at {zone_name} ({zone_price:,.1f}): {tot_cnt} independent agreeing factors."

    return ConfluenceScore(
        zone_price=zone_price, zone_name=zone_name, total_factors=tot_cnt,
        strong_factors_count=strong_cnt, factors=factors, verdict=verdict,
        sentiment=sent_str, note=note
    )


# ═══════════════════════════════════════════════════════════════════════════
# Master Terminal Snapshot Dataclass & Unified Evaluator
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiquidityTerminalSnapshot:
    timeframe: str
    spot: float
    atr: float
    pool_map: LiquidityPoolMap
    inducement: Inducement
    displacement: Displacement
    dealing_range: DealingRangeAnalysis
    breaker_mitigation: BreakerMitigation
    bpr_ce: BPRAndCE
    turtle_soup: TurtleSoup
    session_model: SessionModel
    judas_swing: JudasSwing
    smt_divergence: SMTDivergence
    power_of_three: PowerOfThree
    confluence: ConfluenceScore
    structure: smc.Structure
    pivots: list[smc.Pivot]
    gaps: list[smc.FVG]
    order_blocks: list[smc.OrderBlock]
    warnings: list[str] = field(default_factory=list)


def evaluate_liquidity_terminal(
    candles_df: Optional[pd.DataFrame],
    pa_frames: Optional[dict] = None,
    spot: float = 24000.0,
    chain_df: Optional[pd.DataFrame] = None,
    ohlc_daily: Optional[pd.DataFrame] = None,
    levels: Optional[Any] = None,
    banknifty_candles: Optional[pd.DataFrame] = None,
    rules: Optional[dict] = None,
    timeframe: str = "15m",
) -> LiquidityTerminalSnapshot:
    """
    Unified evaluator for the 12 institutional Liquidity & Smart Money concepts.
    Guarded against insufficient bars, missing columns, all-NaN volume, zero ATR,
    empty daily history, missing BANKNIFTY feed, division by zero, and pre-open data.
    """
    rules = rules or load_rules()
    n = int(rules.get("pivot_window_n", 2))
    warnings: list[str] = []

    df = candles_df
    if (df is None or df.empty) and pa_frames:
        df = pa_frames.get(timeframe) or pa_frames.get("15m") or pa_frames.get("5m") or pa_frames.get("1m")

    if df is None or df.empty:
        warnings.append("No candle data available (pre-open or data feed disconnected).")
        return _build_empty_snapshot(spot, timeframe, warnings)

    c_lower = {str(c).lower(): c for c in df.columns}
    req_cols = ["open", "high", "low", "close"]
    missing = [col for col in req_cols if col not in c_lower]
    if missing:
        warnings.append(f"Missing required price columns: {missing}")
        return _build_empty_snapshot(spot, timeframe, warnings)

    clean_df = pd.DataFrame({
        "open": df[c_lower["open"]].astype(float),
        "high": df[c_lower["high"]].astype(float),
        "low": df[c_lower["low"]].astype(float),
        "close": df[c_lower["close"]].astype(float),
    }, index=df.index)

    if "volume" in c_lower:
        clean_df["volume"] = df[c_lower["volume"]].astype(float).fillna(0.0)
    else:
        clean_df["volume"] = 0.0

    if "timestamp" in df.columns:
        clean_df["timestamp"] = pd.to_datetime(df["timestamp"])
    elif "date" in df.columns:
        clean_df["timestamp"] = pd.to_datetime(df["date"])
    else:
        clean_df["timestamp"] = pd.to_datetime(df.index)

    min_required_bars = 2 * n + 3
    if len(clean_df) < min_required_bars:
        warnings.append(f"Insufficient bars ({len(clean_df)} < {min_required_bars} required). Building degraded terminal.")
        return _build_empty_snapshot(spot, timeframe, warnings, df=clean_df)

    atr_val = 0.0
    if "atr" in df.columns and not df["atr"].isna().all():
        atr_val = float(df["atr"].dropna().iloc[-1])
    if atr_val <= 0.0:
        tr = np.maximum(
            clean_df["high"] - clean_df["low"],
            np.maximum(
                abs(clean_df["high"] - clean_df["close"].shift(1)),
                abs(clean_df["low"] - clean_df["close"].shift(1))
            )
        )
        atr_val = float(tr.rolling(min(14, len(clean_df))).mean().iloc[-1])
        if math.isnan(atr_val) or atr_val <= 0.0:
            atr_val = max(float((clean_df["high"] - clean_df["low"]).mean()), 10.0)

    curr_spot = spot
    if curr_spot <= 0.0:
        curr_spot = float(clean_df["close"].iloc[-1])

    try:
        pivots = smc.pivots(clean_df, n=n)
    except Exception as e:
        warnings.append(f"smc.pivots exception: {e}")
        pivots = []

    try:
        st = smc.structure(clean_df, pivots)
    except Exception as e:
        warnings.append(f"smc.structure exception: {e}")
        st = smc.Structure()

    try:
        gaps = smc.fvgs(clean_df, mitigate_at=float(rules.get("fvg_mitigate_at", 0.5)))
    except Exception as e:
        warnings.append(f"smc.fvgs exception: {e}")
        gaps = []

    try:
        obs = smc.order_blocks(clean_df, st, gaps)
    except Exception as e:
        warnings.append(f"smc.order_blocks exception: {e}")
        obs = []

    pool_map = compute_pool_map(
        clean_df, pivots, curr_spot, atr_val,
        ohlc_daily=ohlc_daily, chain_levels=levels, rules=rules
    )

    inducement = evaluate_inducement(clean_df, pivots, pool_map.nearest_above or pool_map.nearest_below, curr_spot)
    disp = evaluate_displacement(clean_df, gaps, atr_val, rules=rules)
    dr = evaluate_dealing_range(pivots, curr_spot, rules=rules)
    breakers = evaluate_breaker_mitigation(clean_df, obs, pivots)
    bpr_ce = evaluate_bpr_and_ce(clean_df, gaps)
    turtle = evaluate_turtle_soup(clean_df, ohlc_daily, rules=rules)
    sess_model = evaluate_session_model(clean_df, rules=rules)
    judas = evaluate_judas_swing(clean_df, disp, atr_val, rules=rules)
    smt = evaluate_smt_divergence(clean_df, banknifty_candles, rules=rules)
    amd = evaluate_power_of_three(clean_df, pool_map, disp, atr_val, rules=rules)
    confl = evaluate_confluence(
        curr_spot, pool_map, inducement, disp, dr,
        breakers, bpr_ce, levels, clean_df, ohlc_daily, rules=rules
    )

    return LiquidityTerminalSnapshot(
        timeframe=timeframe,
        spot=curr_spot,
        atr=round(atr_val, 1),
        pool_map=pool_map,
        inducement=inducement,
        displacement=disp,
        dealing_range=dr,
        breaker_mitigation=breakers,
        bpr_ce=bpr_ce,
        turtle_soup=turtle,
        session_model=sess_model,
        judas_swing=judas,
        smt_divergence=smt,
        power_of_three=amd,
        confluence=confl,
        structure=st,
        pivots=pivots,
        gaps=gaps,
        order_blocks=obs,
        warnings=warnings,
    )


def _build_empty_snapshot(
    spot: float,
    timeframe: str,
    warnings: list[str],
    df: Optional[pd.DataFrame] = None,
) -> LiquidityTerminalSnapshot:
    """Builds a safe, non-raising degraded snapshot when data is missing or incomplete."""
    empty_pool_map = LiquidityPoolMap()
    empty_inducement = Inducement(
        detected=False, minor_pivot_price=None, target_pool_price=None,
        is_taken=False, taken_time=None, quality_verdict="NO DATA",
        note="Data guard triggered: awaiting sufficient continuous candle history."
    )
    empty_disp = Displacement(
        score=0, large_body=False, small_wicks=False, fvg_created=False,
        two_closes_in_dir=False, direction="NEUTRAL", candle_idx=-1,
        candle_time=None, verdict="NO DATA", note="Awaiting candle history."
    )
    empty_dr = DealingRangeAnalysis(
        high=spot + 50.0, low=spot - 50.0, equilibrium=spot, size=100.0,
        spot=spot, position_pct=0.5, zone="EQUILIBRIUM", in_ote=False,
        ote_low=spot - 10.0, ote_high=spot + 10.0,
        bands={"0.0": spot - 50, "0.5": spot, "1.0": spot + 50},
        verdict="AWAITING BARS", note="Insufficient bars to anchor dealing range."
    )
    empty_breakers = BreakerMitigation()
    empty_bpr_ce = BPRAndCE()
    empty_turtle = TurtleSoup(
        detected=False, level_name="", level_price=0.0, raid_direction="",
        bars_outside=0, reclaimed=False, reclaim_state="NONE", bar_index=-1,
        time=None, sentiment="NEUTRAL", note="Awaiting session data."
    )
    empty_sess = SessionModel(
        current_time="09:15", active_window=None, windows=[],
        day_high=spot, day_low=spot, day_range=0.0, high_window="None",
        low_window="None", pending_extreme="NEITHER", note="Awaiting session data."
    )
    empty_judas = JudasSwing(
        detected=False, direction="NONE", initial_move_pts=0.0, initial_move_atr=0.0,
        reversal_confirmed=False, confirmation_time=None, confirmation_bar=None,
        status="NONE", note="Awaiting session data."
    )
    empty_smt = SMTDivergence(
        available=False, divergence_type="UNAVAILABLE", nifty_swing="N/A",
        banknifty_swing="N/A", pool_level=None, sentiment="NEUTRAL",
        status_label="UNAVAILABLE — needs BANKNIFTY feed",
        note="Requires continuous BANKNIFTY candle feed. Proxy substitutions strictly forbidden."
    )
    empty_amd = PowerOfThree(
        detected=False, phase="NONE", direction="NONE", anchor_level_name="",
        anchor_price=0.0, consolidation_range=(0.0, 0.0), manipulation_extreme=None,
        distribution_target=None, confirmed=False, note="Awaiting structural levels."
    )
    empty_confl = ConfluenceScore(
        zone_price=spot, zone_name="Spot", total_factors=0, strong_factors_count=0,
        factors=[], verdict="AWAITING DATA", sentiment="NEUTRAL",
        note="Insufficient data to compute confluence factors."
    )

    return LiquidityTerminalSnapshot(
        timeframe=timeframe,
        spot=spot,
        atr=20.0,
        pool_map=empty_pool_map,
        inducement=empty_inducement,
        displacement=empty_disp,
        dealing_range=empty_dr,
        breaker_mitigation=empty_breakers,
        bpr_ce=empty_bpr_ce,
        turtle_soup=empty_turtle,
        session_model=empty_sess,
        judas_swing=empty_judas,
        smt_divergence=empty_smt,
        power_of_three=empty_amd,
        confluence=empty_confl,
        structure=smc.Structure(),
        pivots=[],
        gaps=[],
        order_blocks=[],
        warnings=warnings,
    )
