"""
engine/gtf.py — GTF Demand & Supply Indicator 2.0 Equivalent.
Automated institutional zone detection using core price-action principles:
  1. Boring Candle Detection: consolidation where CandleBody < AverageBodyRange(14) * 0.6
  2. Explosive (Impulsive) Candle Detection: CandleBody > AverageBodyRange(14) * 1.5 with Volume expansion
  3. Zone Construction:
     - Demand: 1-3 boring candles preceding an explosive green candle (RBR / DBR)
     - Supply: 1-3 boring candles preceding an explosive red candle (DBD / RBD)
  4. 3 Marking Modes:
     - Conservative: Body-to-Wick
     - Aggressive: Wick-to-Wick
     - Dynamic: ATR-adjusted boundaries
  5. Mitigation & Retest Tracking: FRESH, TESTED (1x/2x), or MITIGATED/BROKEN

All thresholds loaded from rules.json ("gtf" section).
Observation and alert-only (never places orders).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .. import config
from ..timeutil import chart_time, hm
from .primitives import Bars

log = logging.getLogger(__name__)


@dataclass
class GtfZone:
    id: str
    type: str                  # "DEMAND" | "SUPPLY"
    dir: str                   # "bull" | "bear"
    from_time: int
    to_time: int
    top: float
    bottom: float
    proximal: float
    distal: float
    status: str                # "FRESH" | "TESTED" | "MITIGATED" | "BROKEN"
    test_count: int
    mode: str                  # "conservative" | "aggressive" | "dynamic"
    pattern: str               # "RBR" | "DBR" | "DBD" | "RBD"
    score: float               # 0 - 10
    grade: str                 # "A+" | "A" | "B"
    base_bars: int
    explosive_time: int
    explosive_body_ratio: float
    label: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "dir": self.dir,
            "from": chart_time(self.from_time),
            "to": chart_time(self.to_time),
            "from_raw": self.from_time,
            "to_raw": self.to_time,
            "top": round(self.top, 2),
            "bottom": round(self.bottom, 2),
            "proximal": round(self.proximal, 2),
            "distal": round(self.distal, 2),
            "status": self.status,
            "test_count": self.test_count,
            "mode": self.mode,
            "pattern": self.pattern,
            "score": round(self.score, 1),
            "grade": self.grade,
            "base_bars": self.base_bars,
            "explosive_time": self.explosive_time,
            "explosive_body_ratio": round(self.explosive_body_ratio, 2),
            "label": self.label,
        }


def compute_abr(closes: np.ndarray, opens: np.ndarray, period: int = 14) -> np.ndarray:
    """Average Body Range: SMA of abs(close - open)."""
    n = len(closes)
    bodies = np.abs(closes - opens)
    abr = np.empty_like(bodies, dtype=float)
    if n == 0:
        return abr
    for i in range(n):
        start = max(0, i - period + 1)
        abr[i] = float(np.mean(bodies[start : i + 1]))
    return abr


def compute_vol_sma(volumes: Optional[np.ndarray], n_bars: int, period: int = 20) -> np.ndarray:
    """SMA of volume."""
    vol_sma = np.zeros(n_bars, dtype=float)
    if volumes is None or len(volumes) < n_bars:
        return vol_sma
    for i in range(n_bars):
        start = max(0, i - period + 1)
        vol_sma[i] = float(np.mean(volumes[start : i + 1]))
    return vol_sma


class GtfSupplyDemandEngine:
    """
    GTF Institutional Supply & Demand Zone Engine (v2.0 Equivalent).
    Automates institutional order tracking across timeframes.
    """

    def __init__(self, rules_cfg: Optional[Dict[str, Any]] = None):
        cfg = rules_cfg or config.rules().get("gtf", {})
        self.boring_body_ratio = float(cfg.get("boring_body_ratio", 0.60))
        self.explosive_body_ratio = float(cfg.get("explosive_body_ratio", 1.50))
        self.abr_period = int(cfg.get("abr_period", 14))
        self.vol_sma_period = int(cfg.get("vol_sma_period", 20))
        self.vol_mult = float(cfg.get("vol_mult", 1.0))
        self.base_min_bars = int(cfg.get("base_min_bars", 1))
        self.base_max_bars = int(cfg.get("base_max_bars", 3))
        self.max_active_zones = int(cfg.get("max_active_zones", 8))
        self.max_active_supplies = int(cfg.get("max_active_supplies", 6))
        self.max_active_demands = int(cfg.get("max_active_demands", 6))
        self.max_broken_zones = int(cfg.get("max_broken_zones", 4))
        self.massive_leg_out_atr_mult = float(cfg.get("massive_leg_out_atr_mult", 2.0))
        self.massive_leg_out_base_mult = float(cfg.get("massive_leg_out_base_mult", 2.5))
        self.relaxed_base_body_ratio = float(cfg.get("relaxed_base_body_ratio", 0.65))
        self.relaxed_base_max_bars = int(cfg.get("relaxed_base_max_bars", 4))
        self.marking_mode = str(cfg.get("marking_mode", "conservative")).lower()
        self.dynamic_atr_mult = float(cfg.get("dynamic_atr_mult", 0.20))
        self.near_zone_tolerance_pts = float(cfg.get("near_zone_tolerance_pts", 25.0))

    def detect_zones(
        self,
        bars_or_candles: Union[Bars, List[Dict[str, Any]]],
        atr_arr: Optional[np.ndarray] = None,
        volumes: Optional[np.ndarray] = None,
        tf: str = "5m",
        mode_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process OHLCV candles and extract institutional GTF Demand & Supply zones."""
        if isinstance(bars_or_candles, Bars):
            b = bars_or_candles
        elif isinstance(bars_or_candles, list):
            valid = [c for c in bars_or_candles if c.get("closed", True) is not False]
            if not valid:
                return {"available": False, "message": "No valid closed candles"}
            b = Bars.from_rows(valid)
        else:
            return {"available": False, "message": "Invalid bars format"}

        n = len(b)
        if n < 5:
            return {"available": False, "message": "Insufficient bars (<5) for GTF evaluation"}

        marking_mode = (mode_override or self.marking_mode).lower()
        if marking_mode not in ("conservative", "aggressive", "dynamic"):
            marking_mode = "conservative"

        close = b.c
        open_p = b.o
        high = b.h
        low = b.l
        ts = b.ts

        abr = compute_abr(close, open_p, self.abr_period)
        vol_sma = compute_vol_sma(volumes, n, self.vol_sma_period)

        # ATR calculation for dynamic mode
        if atr_arr is None or len(atr_arr) != n:
            pc = np.r_[close[0], close[:-1]]
            tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
            eff_atr = np.zeros(n, dtype=float)
            for k in range(n):
                eff_atr[k] = float(np.mean(tr[max(0, k - 13) : k + 1]))
        else:
            eff_atr = atr_arr

        # Identify Boring and Explosive candles
        is_boring = np.zeros(n, dtype=bool)
        is_explosive_up = np.zeros(n, dtype=bool)
        is_explosive_down = np.zeros(n, dtype=bool)

        for i in range(n):
            body = abs(close[i] - open_p[i])
            c_range = max(high[i] - low[i], 1e-4)
            cur_abr = max(abr[i], 5.0)
            cur_atr = float(eff_atr[i])

            # Boring candle: narrow body relative to ABR or small body ratio
            if body <= cur_abr * self.boring_body_ratio or (body / c_range) <= 0.45:
                is_boring[i] = True

            # Explosive candle: large body relative to ABR or directional ATR expansion
            has_vol = True
            if volumes is not None and len(volumes) == n and volumes[i] > 0 and vol_sma[i] > 0:
                has_vol = volumes[i] >= vol_sma[i] * self.vol_mult

            is_exp = (
                body >= cur_abr * self.explosive_body_ratio
                or body >= cur_atr * 1.3
                or (c_range >= cur_atr * self.massive_leg_out_atr_mult and body / c_range >= 0.40)
            )

            if is_exp and has_vol:
                if close[i] > open_p[i]:
                    is_explosive_up[i] = True
                elif close[i] < open_p[i]:
                    is_explosive_down[i] = True

        raw_zones: List[GtfZone] = []

        # Scan for base + explosive leg-out clusters
        for j in range(self.base_min_bars, n):
            if not (is_explosive_up[j] or is_explosive_down[j]):
                continue

            exp_body = abs(close[j] - open_p[j])
            exp_range = max(high[j] - low[j], 1e-4)
            cur_atr = float(eff_atr[j])
            cur_abr = max(abr[j], 5.0)

            # Check if leg-out departure is massive (>= 2.0x ATR or >= 2.0x ABR or multi-bar departure)
            is_massive_leg_out = (
                exp_body >= cur_atr * self.massive_leg_out_atr_mult
                or exp_range >= cur_atr * self.massive_leg_out_atr_mult
                or exp_body >= cur_abr * 2.0
            )
            if not is_massive_leg_out and j + 1 < n:
                if is_explosive_down[j] and close[j + 1] < close[j]:
                    multi_drop = abs(close[j + 1] - open_p[j])
                    if multi_drop >= cur_atr * self.massive_leg_out_atr_mult:
                        is_massive_leg_out = True
                elif is_explosive_up[j] and close[j + 1] > close[j]:
                    multi_rally = abs(close[j + 1] - open_p[j])
                    if multi_rally >= cur_atr * self.massive_leg_out_atr_mult:
                        is_massive_leg_out = True

            # Determine max lookback bars for base candles
            max_bars_to_check = self.relaxed_base_max_bars if is_massive_leg_out else self.base_max_bars
            base_indices = []

            for k in range(1, max_bars_to_check + 1):
                idx = j - k
                if idx < 0:
                    break

                b_body = abs(close[idx] - open_p[idx])
                b_range = max(high[idx] - low[idx], 1e-4)
                b_ratio = b_body / b_range
                idx_abr = max(abr[idx], 5.0)

                # Standard boring check
                is_std_boring = (b_body <= idx_abr * self.boring_body_ratio) or (b_ratio <= 0.45)

                # Dynamic relaxation on massive leg-out or leg-out >= 2.5x base range
                is_massive_vs_base = (
                    exp_body >= b_range * self.massive_leg_out_base_mult
                    or exp_range >= b_range * self.massive_leg_out_base_mult
                )
                allow_relaxation = is_massive_leg_out or is_massive_vs_base

                is_relaxed_boring = (
                    b_body <= idx_abr * self.boring_body_ratio * 1.35
                    or b_ratio <= self.relaxed_base_body_ratio
                )

                if is_std_boring or (allow_relaxation and is_relaxed_boring):
                    base_indices.insert(0, idx)
                else:
                    break

            base_count = len(base_indices)
            if base_count < self.base_min_bars:
                continue

            base_from_ts = int(ts[base_indices[0]])
            exp_ts = int(ts[j])
            exp_ratio = exp_body / max(abr[j], 1.0)

            # Determine pattern: RBR/DBR for Demand, DBD/RBD for Supply with gap & prior trend handling
            prior_idx = base_indices[0] - 1
            if prior_idx >= 0:
                gap = open_p[base_indices[0]] - close[prior_idx]
                prior_start = max(0, prior_idx - 2)
                prior_net = close[prior_idx] - open_p[prior_start]
                if abs(gap) >= 15.0:
                    is_prior_rally = gap > 0
                elif abs(prior_net) >= 5.0:
                    is_prior_rally = prior_net > 0
                else:
                    is_prior_rally = close[prior_idx] >= open_p[prior_idx]
            else:
                is_prior_rally = close[0] >= open_p[0]

            if is_explosive_up[j]:
                # DEMAND ZONE
                pattern = "RBR" if is_prior_rally else "DBR"
                highest_body = float(max(max(open_p[idx], close[idx]) for idx in base_indices))
                lowest_wick = float(min(low[idx] for idx in base_indices))
                highest_wick = float(max(high[idx] for idx in base_indices))

                if marking_mode == "conservative":
                    proximal = highest_body
                    distal = lowest_wick
                elif marking_mode == "aggressive":
                    proximal = highest_wick
                    distal = lowest_wick
                else:  # dynamic
                    pad = cur_atr * self.dynamic_atr_mult
                    proximal = highest_body + pad
                    distal = lowest_wick - pad

                top = max(proximal, distal)
                bottom = min(proximal, distal)
                z_type = "DEMAND"
                z_dir = "bull"
            else:
                # SUPPLY ZONE
                pattern = "RBD" if is_prior_rally else "DBD"
                lowest_body = float(min(min(open_p[idx], close[idx]) for idx in base_indices))
                highest_wick = float(max(high[idx] for idx in base_indices))
                lowest_wick = float(min(low[idx] for idx in base_indices))

                if marking_mode == "conservative":
                    proximal = lowest_body
                    distal = highest_wick
                elif marking_mode == "aggressive":
                    proximal = lowest_wick
                    distal = highest_wick
                else:  # dynamic
                    pad = cur_atr * self.dynamic_atr_mult
                    proximal = lowest_body - pad
                    distal = highest_wick + pad

                top = max(proximal, distal)
                bottom = min(proximal, distal)
                z_type = "SUPPLY"
                z_dir = "bear"

            # Check subsequent candles for tests / mitigations
            status = "FRESH"
            test_count = 0
            end_ts = int(ts[-1])

            for m in range(j + 1, n):
                c_close = close[m]
                c_high = high[m]
                c_low = low[m]

                if z_type == "DEMAND":
                    # Broken if closed below distal line
                    if c_close < distal:
                        status = "BROKEN"
                        end_ts = int(ts[m])
                        break
                    # Tested if low pierced proximal line
                    elif c_low <= proximal:
                        test_count += 1
                        status = "TESTED"
                else:
                    # Broken if closed above distal line
                    if c_close > distal:
                        status = "BROKEN"
                        end_ts = int(ts[m])
                        break
                    # Tested if high pierced proximal line
                    elif c_high >= proximal:
                        test_count += 1
                        status = "TESTED"

            # Score Zone
            score = 0.0
            if status == "FRESH":
                score += 4.0
            elif status == "TESTED":
                score += 2.5 if test_count == 1 else 1.5
            else:
                score += 0.5

            if exp_ratio >= 2.0 or is_massive_leg_out:
                score += 3.0
            elif exp_ratio >= 1.5:
                score += 2.0
            else:
                score += 1.0

            if base_count == 1:
                score += 2.5
            elif base_count == 2:
                score += 1.5
            else:
                score += 1.0

            if volumes is not None and len(volumes) == n and volumes[j] >= vol_sma[j]:
                score += 0.5

            grade = "A+" if score >= 8.5 else "A" if score >= 6.5 else "B"
            if status == "BROKEN":
                flip_desc = "Flipped to Resistance" if z_type == "DEMAND" else "Flipped to Support"
                label = f"GTF {z_type} {pattern} · BROKEN [{grade}] — {flip_desc}"
            elif status == "TESTED":
                label = f"GTF {z_type} {pattern} · TESTED ({test_count}x) [{grade}]"
            else:
                label = f"GTF {z_type} {pattern} · FRESH [{grade}]"

            raw_zones.append(GtfZone(
                id=f"gtf_{z_type.lower()}_{base_from_ts}",
                type=z_type,
                dir=z_dir,
                from_time=base_from_ts,
                to_time=end_ts,
                top=top,
                bottom=bottom,
                proximal=proximal,
                distal=distal,
                status=status,
                test_count=test_count,
                mode=marking_mode,
                pattern=pattern,
                score=min(10.0, score),
                grade=grade,
                base_bars=base_count,
                explosive_time=exp_ts,
                explosive_body_ratio=exp_ratio,
                label=label,
            ))

        # Filter and rank zones: prioritize proximity to current Spot price combined with grade/freshness
        cur_price = float(close[-1])
        active_zones = [z for z in raw_zones if z.status != "BROKEN"]
        broken_zones = [z for z in raw_zones if z.status == "BROKEN"]

        # Separate into active Supply and Demand
        active_supplies = [z for z in active_zones if z.type == "SUPPLY"]
        active_demands = [z for z in active_zones if z.type == "DEMAND"]

        # Rank active zones by proximity to current Spot price combined with grade and freshness
        def supply_rank_key(z: GtfZone) -> float:
            dist = abs(z.proximal - cur_price)
            grade_bonus = 100.0 if z.grade == "A+" else 50.0 if z.grade == "A" else 0.0
            fresh_bonus = 50.0 if z.status == "FRESH" else 0.0
            return dist - grade_bonus - fresh_bonus

        def demand_rank_key(z: GtfZone) -> float:
            dist = abs(cur_price - z.proximal)
            grade_bonus = 100.0 if z.grade == "A+" else 50.0 if z.grade == "A" else 0.0
            fresh_bonus = 50.0 if z.status == "FRESH" else 0.0
            return dist - grade_bonus - fresh_bonus

        active_supplies.sort(key=supply_rank_key)
        active_demands.sort(key=demand_rank_key)

        selected_supplies = active_supplies[: self.max_active_supplies]
        selected_demands = active_demands[: self.max_active_demands]
        selected_active = selected_supplies + selected_demands

        # Include recently broken zones for visual flip context (most recent up to max_broken_zones)
        broken_zones.sort(key=lambda z: z.to_time)
        selected_broken = broken_zones[-self.max_broken_zones :] if self.max_broken_zones > 0 else []

        display_zones = selected_active + selected_broken
        display_zones.sort(key=lambda z: z.from_time)

        fresh_demands = [z.to_dict() for z in active_demands if z.status == "FRESH"]
        fresh_supplies = [z.to_dict() for z in active_supplies if z.status == "FRESH"]

        # Nearest fresh supply and demand to spot for HUD pill
        # Prefer fresh zones positioned correctly relative to spot (within near_zone_tolerance_pts)
        supplies_above = [z for z in active_supplies if z.proximal >= cur_price - self.near_zone_tolerance_pts]
        fresh_supplies_above = [z for z in supplies_above if z.status == "FRESH"]
        candidate_supplies = fresh_supplies_above if fresh_supplies_above else (supplies_above if supplies_above else active_supplies)
        nearest_supply = min(candidate_supplies, key=lambda z: abs(z.proximal - cur_price)).to_dict() if candidate_supplies else None

        demands_below = [z for z in active_demands if z.proximal <= cur_price + self.near_zone_tolerance_pts]
        fresh_demands_below = [z for z in demands_below if z.status == "FRESH"]
        candidate_demands = fresh_demands_below if fresh_demands_below else (demands_below if demands_below else active_demands)
        nearest_demand = min(candidate_demands, key=lambda z: abs(cur_price - z.proximal)).to_dict() if candidate_demands else None

        stats = {
            "total_detected": len(raw_zones),
            "active_count": len(active_zones),
            "fresh_count": sum(1 for z in active_zones if z.status == "FRESH"),
            "tested_count": sum(1 for z in active_zones if z.status == "TESTED"),
            "broken_count": len(broken_zones),
        }

        return {
            "available": True,
            "tf": tf,
            "marking_mode": marking_mode,
            "current_price": round(cur_price, 2),
            "zones": [z.to_dict() for z in display_zones],
            "fresh_demands": fresh_demands,
            "fresh_supplies": fresh_supplies,
            "nearest_demand": nearest_demand,
            "nearest_supply": nearest_supply,
            "stats": stats,
        }


def compute_gtf(
    bars: Bars,
    atr_arr: Optional[np.ndarray] = None,
    volumes: Optional[np.ndarray] = None,
    tf: str = "5m",
    rules_cfg: Optional[Dict[str, Any]] = None,
    marking_mode: str = "conservative",
) -> Dict[str, Any]:
    """Module-level helper to detect GTF Supply & Demand zones."""
    engine = GtfSupplyDemandEngine(rules_cfg=rules_cfg)
    return engine.detect_zones(bars, atr_arr=atr_arr, volumes=volumes, tf=tf, mode_override=marking_mode)
