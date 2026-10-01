"""
strike_select.py — Strike Selection & Premium Behaviour Engine.

WHAT THIS ENGINE DOES:
──────────────────────
- Evaluates candidate option strikes given market spot, direction (CE/PE), and spot targets/stops.
- Computes expected rupee outcomes, capital strain, return on capital (ROC), and risk-reward.
- Classifies ATM IV into market momentum regimes (<10, 10–20, >20) with buyer/seller verdicts.
- Models calendar-day theta decay (including intraday, overnight, and multi-day weekend decay).
- Translates spot target/stop points into delta-adjusted account rupees and vega sensitivities.
- Restates Open Interest walls into four-participant intent (Call/Put buyers and sellers).

WHAT THIS ENGINE CANNOT DO:
───────────────────────────
- It CANNOT guarantee option outcomes. Delta projections are strictly FIRST-ORDER linear
  approximations. As spot moves, Gamma changes Delta continuously: first-order math
  understates gains on large favourable moves and overstates losses on adverse moves.
- It CANNOT treat IV regime bands (<10, 10–20, >20) or the ₹200–₹300 premium band as proven laws.
  These are one experienced trader's heuristic thresholds; they have NOT been statistically
  backtested or validated on your historical dataset.
- It CANNOT predict volatility shocks or skew shifts. A correct directional call with falling IV
  (IV crush) can lose money even when the index hits target.
- It CANNOT execute or place trades. Alert-only. Never recommends a strike; displays trade-offs
  so the trader can make an informed choice.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import greeks
import analytics

# ═══════════════════════════════════════════════════════════════════════════
# 1. Rules & Configuration Loader
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_RULES_PATH = Path(__file__).parent / "strike_select_rules.json"


def load_rules(rules_path: Optional[Path] = None) -> dict:
    """Loads tunable thresholds from strike_select_rules.json with strict fallbacks."""
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "premium_band_min": 200.0,
        "premium_band_max": 300.0,
        "capital_max_lot_pct": 30.0,
        "default_target_pts": 40.0,
        "default_stop_pts": 40.0,
        "iv_bands": {
            "sub_10": {
                "max_iv": 10.0,
                "momentum": "very slow",
                "option_buyers": "BAD — premium barely moves",
                "option_sellers": "VERY GOOD",
                "badge": "VERY SLOW",
                "color": "#EF4444",
                "description": "Momentum is sluggish. Correct directional calls often fail to gain premium.",
            },
            "normal_10_20": {
                "min_iv": 10.0,
                "max_iv": 20.0,
                "momentum": "normal",
                "option_buyers": "GOOD — tradeable",
                "option_sellers": "risky",
                "badge": "NORMAL",
                "color": "#10B981",
                "description": "Ideal momentum regime. Greeks behave predictably.",
            },
            "excessive_above_20": {
                "min_iv": 20.0,
                "momentum": "excessive",
                "option_buyers": "NOT good — whipsaw",
                "option_sellers": "DANGEROUS",
                "badge": "EXCESSIVE",
                "color": "#F59E0B",
                "description": "Elevated implied volatility. Prone to whipsaws and IV crush.",
            },
        },
        "vega_iv_shift_points": 2.0,
        "default_lot_size": 65,
        "default_capital": 20000.0,
        "min_bid_price": 0.05,
        "focus_strikes_count": 8,
    }

    if not path.exists():
        return defaults

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Merge loaded keys over defaults
            merged = {**defaults, **data}
            if "iv_bands" in data and isinstance(data["iv_bands"], dict):
                merged["iv_bands"] = {**defaults["iv_bands"], **data["iv_bands"]}
            return merged
    except Exception as err:
        # Logging without bare except
        print(f"[strike_select] Warning: failed to parse rules JSON ({err}). Using defaults.")
        return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 2. Data Structures (@dataclass per result)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class IVRegimeRead:
    """Classifies ATM IV into the source's three regimes."""
    atm_iv: float
    regime_key: str          # 'sub_10', 'normal_10_20', 'excessive_above_20'
    momentum: str            # 'very slow', 'normal', 'excessive'
    buyer_verdict: str       # 'BAD — premium barely moves', 'GOOD — tradeable', 'NOT good — whipsaw'
    seller_verdict: str      # 'VERY GOOD', 'risky', 'DANGEROUS'
    badge: str               # 'VERY SLOW', 'NORMAL', 'EXCESSIVE'
    color: str               # Hex color code
    band_position_pct: float # 0 to 100% position within current band
    sub_10_warning: bool     # True if IV < 10 (dead option momentum trap)
    description: str
    caveat: str = (
        "These bands are one trader's thresholds, not a measured statistical result. "
        "They are plausible and worth testing; they have not been validated on your data."
    )


@dataclass
class StrikeCandidate:
    """Evaluated option contract with financial return and risk metrics."""
    strike: int
    option_type: str              # 'CE' or 'PE'
    premium: float                # LTP in ₹
    bid: float                    # Best bid in ₹
    ask: float                    # Best ask in ₹
    delta: float                  # Signed delta
    abs_delta: float              # Magnitude of delta (0.0 to 1.0)
    gamma: float                  # Gamma (curvature)
    theta: float                  # Daily theta (decay per day in points)
    vega: float                   # Vega (points per 1% IV shift)
    iv: float                     # Implied volatility %
    target_points: float          # Index spot target points
    stop_points: float            # Index spot stop points
    expected_premium_gain: float  # target_points × delta
    expected_rupees: float        # expected_premium_gain × lot_size
    lot_cost: float               # premium × lot_size
    cost_capital_pct: float       # (lot_cost / capital) × 100
    roc_pct: float                # (expected_rupees / lot_cost) × 100
    loss_if_stop: float           # min(stop_points × delta × lot_size, lot_cost)
    loss_capital_pct: float       # (loss_if_stop / capital) × 100
    spot_rr: float                # spot target / stop ratio
    delta_adjusted_rr: float      # expected_rupees / loss_if_stop
    in_premium_band: bool         # premium in ₹200–₹300 band
    exceeds_capital_limit: bool   # lot_cost > capital_max_lot_pct (e.g. 30%)
    vega_impact_2pt: float        # Vega rupee impact for a 2-pt IV change
    notes: List[str] = field(default_factory=list)


@dataclass
class ThetaDecayStep:
    """A calendar milestone projecting premium decay assuming spot stays flat."""
    timepoint: str                # e.g. 'Now', 'Today End (15:30)', 'Tomorrow Open (09:15)', 'Day +2'
    calendar_date: str            # 'Fri 11 Sep', etc.
    calendar_days_elapsed: float  # Cumulative calendar days elapsed
    projected_premium: float      # Projected option price in ₹
    projected_loss_pts: float     # Points lost from current price
    projected_loss_rs: float      # Rupee loss per lot
    is_today: bool = False
    is_weekend: bool = False


@dataclass
class ThetaDecayProjection:
    """Multi-session calendar-day theta decay model for a selected strike."""
    strike: int
    option_type: str
    current_premium: float
    daily_theta_pts: float
    daily_theta_rs: float
    dte_calendar_days: float
    steps: List[ThetaDecayStep]
    weekend_warning: str
    model_note: str = (
        "Decay model projects premium assuming flat spot and constant volatility. "
        "Calendar days are counted: weekend holding incurs 2–3 full days of theta with zero market progress."
    )


@dataclass
class WallParticipantIntent:
    """Restatement of open interest and flow at a strike in participant terms."""
    strike: int
    ce_oi: float
    pe_oi: float
    ce_chg_oi: float
    pe_chg_oi: float
    ce_ltp: float
    pe_ltp: float
    ce_pillar: str                 # 'long buildup', 'short buildup', 'short covering', 'long unwinding'
    pe_pillar: str
    call_buyer_intent: str         # Expects index to rise (supports push up)
    call_seller_intent: str        # Expects index NOT to rise (overhead ceiling / resistance)
    put_buyer_intent: str          # Expects index to fall (pushes down)
    put_seller_intent: str         # Expects index NOT to fall (downside floor / support)
    dual_accumulation: bool        # Call buyers + Put sellers both active -> both want price above strike
    dual_distribution: bool        # Call sellers + Put buyers both active -> both want price below strike
    net_posture: str


@dataclass
class WallIntentAnalysis:
    """Overview of market boundaries in participant intent terms."""
    spot: float
    max_call_oi_strike: int        # Strongest overhead resistance (Call sellers' fortress)
    max_put_oi_strike: int         # Strongest downside support (Put sellers' fortress)
    max_call_oi: float
    max_put_oi: float
    strikes: List[WallParticipantIntent]
    summary_text: str
    oi_contract_distinction: str = (
        "Fact Check: Open Interest measures outstanding contracts (lots), NOT individual orders. "
        "Volume is today's gross turnover; OI is the cumulative open book. "
        "A high volume-to-OI ratio indicates contracts changing hands repeatedly without new positions being created."
    )


@dataclass
class StrikeSelectionResult:
    """Complete container returned by the strike selection engine."""
    direction: str
    target_pts: float
    stop_pts: float
    premium_min: float
    premium_max: float
    capital: float
    lot_size: int
    spot: float
    atm_strike: int
    atm_iv_read: IVRegimeRead
    candidates: List[StrikeCandidate]
    top_by_roc: Optional[StrikeCandidate]
    top_in_band: Optional[StrikeCandidate]
    reference_comparison: List[Dict[str, Any]]
    decay_projection: Optional[ThetaDecayProjection]
    wall_analysis: Optional[WallIntentAnalysis]
    guard_warning: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# 3. IV Regime Evaluator
# ═══════════════════════════════════════════════════════════════════════════

def get_iv_regime(atm_iv: float, rules: Optional[dict] = None) -> IVRegimeRead:
    """
    Evaluates ATM IV against the three source bands:
      < 10  : very slow | BAD for buyers | VERY GOOD for sellers
      10–20 : normal    | GOOD for buyers | risky for sellers
      > 20  : excessive | NOT good (whipsaw) | DANGEROUS for sellers
    """
    r = rules or load_rules()
    bands = r.get("iv_bands", {})
    sub_10_cfg = bands.get("sub_10", {})
    normal_cfg = bands.get("normal_10_20", {})
    excess_cfg = bands.get("excessive_above_20", {})

    safe_iv = max(0.0, float(atm_iv)) if atm_iv == atm_iv and atm_iv is not None else 0.0

    if safe_iv < sub_10_cfg.get("max_iv", 10.0):
        regime_key = "sub_10"
        momentum = sub_10_cfg.get("momentum", "very slow")
        buyer = sub_10_cfg.get("option_buyers", "BAD — premium barely moves")
        seller = sub_10_cfg.get("option_sellers", "VERY GOOD")
        badge = sub_10_cfg.get("badge", "VERY SLOW")
        color = sub_10_cfg.get("color", "#EF4444")
        desc = sub_10_cfg.get("description", "Sub-10 IV: option premium barely moves on spot moves.")
        pct = (safe_iv / 10.0) * 100.0
        sub_10_warn = True
    elif safe_iv <= normal_cfg.get("max_iv", 20.0):
        regime_key = "normal_10_20"
        momentum = normal_cfg.get("momentum", "normal")
        buyer = normal_cfg.get("option_buyers", "GOOD — tradeable")
        seller = normal_cfg.get("option_sellers", "risky")
        badge = normal_cfg.get("badge", "NORMAL")
        color = normal_cfg.get("color", "#10B981")
        desc = normal_cfg.get("description", "Normal IV regime: predictable delta and clean tradeability.")
        pct = ((safe_iv - 10.0) / 10.0) * 100.0
        sub_10_warn = False
    else:
        regime_key = "excessive_above_20"
        momentum = excess_cfg.get("momentum", "excessive")
        buyer = excess_cfg.get("option_buyers", "NOT good — whipsaw")
        seller = excess_cfg.get("option_sellers", "DANGEROUS")
        badge = excess_cfg.get("badge", "EXCESSIVE")
        color = excess_cfg.get("color", "#F59E0B")
        desc = excess_cfg.get("description", "Excessive IV: high premium, severe whipsaw and IV crush risk.")
        pct = min(100.0, ((safe_iv - 20.0) / 10.0) * 100.0)
        sub_10_warn = False

    return IVRegimeRead(
        atm_iv=safe_iv,
        regime_key=regime_key,
        momentum=momentum,
        buyer_verdict=buyer,
        seller_verdict=seller,
        badge=badge,
        color=color,
        band_position_pct=max(0.0, min(100.0, pct)),
        sub_10_warning=sub_10_warn,
        description=desc,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Strike Selection Candidate Evaluator
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_strike_candidates(
    df: pd.DataFrame,
    spot: float,
    direction: str = "CE",
    target_pts: float = 40.0,
    stop_pts: float = 40.0,
    premium_min: float = 200.0,
    premium_max: float = 300.0,
    lot_size: int = 65,
    capital: float = 20000.0,
    dte_years: float = 0.02,
    rules: Optional[dict] = None,
) -> List[StrikeCandidate]:
    """
    Evaluates every strike in the chain window for the given direction.
    Computes delta-multiplied gains, 1-lot cost, capital percentage, ROC,
    and flags for the ₹200–₹300 band and the >30% capital limit.
    Returns candidates sorted by Return on Capital (ROC %), descending.
    """
    if df is None or df.empty or spot <= 0:
        return []
    from analytics import deduplicate_strikes_live
    df = deduplicate_strikes_live(df, "evaluate_strike_candidates")

    r = rules or load_rules()
    cap_max_pct = float(r.get("capital_max_lot_pct", 30.0))
    min_bid = float(r.get("min_bid_price", 0.05))
    vega_shift = float(r.get("vega_iv_shift_points", 2.0))
    pfx = "ce" if direction.upper() == "CE" else "pe"
    is_call = (direction.upper() == "CE")

    candidates: List[StrikeCandidate] = []

    for _, row in df.iterrows():
        try:
            strike = int(row.get("strike", 0))
            if strike <= 0:
                continue

            ltp_raw = row.get(f"{pfx}_ltp")
            if ltp_raw is None or pd.isna(ltp_raw):
                continue
            ltp = float(ltp_raw)
            if ltp <= 0.05:  # Guard against zero/sub-tick premium
                continue

            # Bid / Ask checks
            bid_raw = row.get(f"{pfx}_bid")
            ask_raw = row.get(f"{pfx}_ask")
            bid = float(bid_raw) if (bid_raw is not None and not pd.isna(bid_raw)) else ltp
            ask = float(ask_raw) if (ask_raw is not None and not pd.isna(ask_raw)) else ltp

            # Illiquid strike filter
            if bid < min_bid and ask < min_bid:
                continue

            # Greeks extraction / computation
            iv_raw = row.get(f"{pfx}_iv")
            iv = float(iv_raw) if (iv_raw is not None and not pd.isna(iv_raw)) else 0.0

            delta_raw = row.get(f"{pfx}_delta")
            gamma_raw = row.get(f"{pfx}_gamma")
            theta_raw = row.get(f"{pfx}_theta")
            vega_raw = row.get(f"{pfx}_vega")

            # Fallback computation if Greeks column is absent or NaN
            if (delta_raw is None or pd.isna(delta_raw)) and iv > 0 and dte_years > 0:
                bs = greeks.black_scholes(spot, strike, iv, dte_years, is_call)
                delta = bs.delta if bs.delta is not None else (0.5 if is_call else -0.5)
                gamma = bs.gamma or 0.0
                theta = bs.theta or 0.0
                vega = bs.vega or 0.0
            else:
                delta = float(delta_raw) if (delta_raw is not None and not pd.isna(delta_raw)) else (0.5 if is_call else -0.5)
                gamma = float(gamma_raw) if (gamma_raw is not None and not pd.isna(gamma_raw)) else 0.0
                theta = float(theta_raw) if (theta_raw is not None and not pd.isna(theta_raw)) else 0.0
                if vega_raw is not None and not pd.isna(vega_raw):
                    vega = float(vega_raw)
                elif iv > 0 and dte_years > 0:
                    bs = greeks.black_scholes(spot, strike, iv, dte_years, is_call)
                    vega = bs.vega or 0.0
                else:
                    vega = 0.0

            abs_delta = abs(delta)
            if abs_delta < 1e-4:  # Guard against zero or NaN delta
                continue

            # Financial metrics
            gain_pts = target_pts * abs_delta
            gain_rs = gain_pts * lot_size
            lot_cost = ltp * lot_size
            cost_cap_pct = (lot_cost / capital * 100.0) if capital > 0 else 0.0
            roc_pct = (gain_rs / lot_cost * 100.0) if lot_cost > 0 else 0.0

            # Stop loss in rupees: loss cannot exceed 100% of the premium paid (option buyer protection)
            theoretical_stop_pts = stop_pts * abs_delta
            loss_rs = min(theoretical_stop_pts * lot_size, lot_cost)
            loss_cap_pct = (loss_rs / capital * 100.0) if capital > 0 else 0.0

            spot_rr = (target_pts / stop_pts) if stop_pts > 0 else 1.0
            delta_adj_rr = (gain_rs / loss_rs) if loss_rs > 0 else 1.0

            in_band = (premium_min <= ltp <= premium_max)
            exceeds_cap = (cost_cap_pct > cap_max_pct)

            vega_rs_impact = abs(vega) * vega_shift * lot_size

            notes: List[str] = []
            if in_band:
                notes.append("₹200–₹300 Target Premium Band")
            if exceeds_cap:
                notes.append(f"Exceeds {cap_max_pct:.0f}% Capital Limit ({cost_cap_pct:.1f}%)")
            if ltp < 30.0:
                notes.append("Low Premium / Lottery Strike (high delta drag)")
            elif ltp > 400.0:
                notes.append("Heavy Intrinsic Value (capital intensive)")

            candidates.append(StrikeCandidate(
                strike=strike,
                option_type=direction.upper(),
                premium=round(ltp, 2),
                bid=round(bid, 2),
                ask=round(ask, 2),
                delta=round(delta, 4),
                abs_delta=round(abs_delta, 4),
                gamma=round(gamma, 5),
                theta=round(theta, 2),
                vega=round(vega, 2),
                iv=round(iv, 2),
                target_points=target_pts,
                stop_points=stop_pts,
                expected_premium_gain=round(gain_pts, 2),
                expected_rupees=round(gain_rs, 2),
                lot_cost=round(lot_cost, 2),
                cost_capital_pct=round(cost_cap_pct, 2),
                roc_pct=round(roc_pct, 2),
                loss_if_stop=round(loss_rs, 2),
                loss_capital_pct=round(loss_cap_pct, 2),
                spot_rr=round(spot_rr, 2),
                delta_adjusted_rr=round(delta_adj_rr, 2),
                in_premium_band=in_band,
                exceeds_capital_limit=exceeds_cap,
                vega_impact_2pt=round(vega_rs_impact, 2),
                notes=notes,
            ))
        except Exception as err:
            # Continue on isolated parsing glitch without bare except
            continue

    # Sort strictly by Return on Capital (ROC %), descending
    candidates.sort(key=lambda c: c.roc_pct, reverse=True)
    return candidates


# ═══════════════════════════════════════════════════════════════════════════
# 5. Theta Decay Projection Engine (Calendar Days Model)
# ═══════════════════════════════════════════════════════════════════════════

def project_theta_decay(
    candidate: StrikeCandidate,
    spot: float,
    now: Optional[datetime] = None,
    expiry_dt: Optional[datetime] = None,
    dte_calendar_days: float = 4.0,
    lot_size: int = 65,
    rules: Optional[dict] = None,
) -> ThetaDecayProjection:
    """
    Projects option premium assuming spot remains static.
    Generates milestones for:
      - Now
      - End of today (15:30)
      - Tomorrow's open (09:15)
      - Each remaining calendar day up to expiry
    Preserves intrinsic value: an ITM option decays extrinsic time value to zero,
    never decaying below its intrinsic worth max(0, Spot - Strike).
    """
    now = now or datetime.now()
    r = rules or load_rules()

    prem = candidate.premium
    strike = candidate.strike
    is_call = candidate.option_type == "CE"
    iv = candidate.iv if candidate.iv > 0 else 14.0

    # Strict intrinsic floor
    intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
    extrinsic = max(0.0, prem - intrinsic)

    daily_theta_pts = abs(candidate.theta)
    if daily_theta_pts <= 0.01:
        # Fallback approximation: extrinsic / max(1, dte_calendar_days)
        daily_theta_pts = extrinsic / max(1.0, dte_calendar_days)
    daily_theta_rs = daily_theta_pts * lot_size

    # Guard against zero DTE: on expiry day after close
    if dte_calendar_days <= 0.0:
        steps = [
            ThetaDecayStep(
                timepoint="Now (Expiry Bell)",
                calendar_date=now.strftime("%a %d %b"),
                calendar_days_elapsed=0.0,
                projected_premium=round(intrinsic, 2),
                projected_loss_pts=round(prem - intrinsic, 2),
                projected_loss_rs=round((prem - intrinsic) * lot_size, 2),
                is_today=True,
            )
        ]
        return ThetaDecayProjection(
            strike=strike,
            option_type=candidate.option_type,
            current_premium=prem,
            daily_theta_pts=round(daily_theta_pts, 2),
            daily_theta_rs=round(daily_theta_rs, 2),
            dte_calendar_days=0.0,
            steps=steps,
            weekend_warning="Expiry day reached: all extrinsic time value has decayed to zero.",
        )

    steps: List[ThetaDecayStep] = []

    # Step 0: Right Now
    steps.append(ThetaDecayStep(
        timepoint="Now",
        calendar_date=now.strftime("%a %d %b %H:%M"),
        calendar_days_elapsed=0.0,
        projected_premium=round(prem, 2),
        projected_loss_pts=0.0,
        projected_loss_rs=0.0,
        is_today=True,
    ))

    # Step 1: End of Today (15:30)
    market_close = dtime(15, 30)
    market_open = dtime(9, 15)
    cur_time = now.time()

    if cur_time < market_close:
        # Calculate remaining trading day fraction
        total_session_mins = 6.25 * 60  # 375 mins
        cur_mins = max(0.0, (cur_time.hour * 60 + cur_time.minute) - (market_open.hour * 60 + market_open.minute))
        rem_frac = max(0.0, min(1.0, (total_session_mins - cur_mins) / total_session_mins))
        today_decay = daily_theta_pts * rem_frac
    else:
        today_decay = 0.0

    today_end_prem = max(intrinsic, prem - today_decay)
    steps.append(ThetaDecayStep(
        timepoint="Today End (15:30)",
        calendar_date=now.strftime("%a %d %b 15:30"),
        calendar_days_elapsed=0.3,
        projected_premium=round(today_end_prem, 2),
        projected_loss_pts=round(prem - today_end_prem, 2),
        projected_loss_rs=round((prem - today_end_prem) * lot_size, 2),
        is_today=True,
    ))

    # Step 2: Tomorrow Open (09:15)
    tomorrow_dt = now + timedelta(days=1)
    # Overnight decay absorbs remainder of 1 day's theta
    tomorrow_prem = max(intrinsic, prem - daily_theta_pts)
    steps.append(ThetaDecayStep(
        timepoint="Tomorrow Open (09:15)",
        calendar_date=tomorrow_dt.strftime("%a %d %b 09:15"),
        calendar_days_elapsed=1.0,
        projected_premium=round(tomorrow_prem, 2),
        projected_loss_pts=round(prem - tomorrow_prem, 2),
        projected_loss_rs=round((prem - tomorrow_prem) * lot_size, 2),
        is_today=False,
    ))

    # Step 3..N: Subsequent calendar days to expiry
    total_days = max(1, int(math.ceil(dte_calendar_days)))
    max_forecast_days = min(total_days, 10)  # Up to 10 calendar days ahead

    for d in range(2, max_forecast_days + 1):
        target_date = now + timedelta(days=d)
        is_weekend = target_date.weekday() in (5, 6)  # Saturday / Sunday
        day_name = target_date.strftime("%a %d %b")

        # Theoretical decay of time value proportional to sqrt(time remaining)
        time_fraction_left = max(0.0, 1.0 - (d / dte_calendar_days))
        projected_extrinsic = extrinsic * math.sqrt(time_fraction_left)
        step_prem = max(intrinsic, intrinsic + projected_extrinsic)

        step_loss_pts = prem - step_prem
        step_loss_rs = step_loss_pts * lot_size

        timepoint_label = f"Day +{d} ({'Weekend' if is_weekend else 'Session'})"
        if d == total_days:
            timepoint_label = f"Day +{d} (Expiry Day)"

        steps.append(ThetaDecayStep(
            timepoint=timepoint_label,
            calendar_date=day_name,
            calendar_days_elapsed=float(d),
            projected_premium=round(step_prem, 2),
            projected_loss_pts=round(step_loss_pts, 2),
            projected_loss_rs=round(step_loss_rs, 2),
            is_today=False,
            is_weekend=is_weekend,
        ))

    weekend_warning = (
        f"Weekend Decay Warning: Holding {candidate.strike} {candidate.option_type} over a weekend "
        f"(Friday close to Monday open) costs ~{2 * daily_theta_pts:.1f} pts (~₹{2 * daily_theta_rs:,.0f}/lot) "
        f"of pure theta decay while the index is stationary."
    )

    return ThetaDecayProjection(
        strike=strike,
        option_type=candidate.option_type,
        current_premium=prem,
        daily_theta_pts=round(daily_theta_pts, 2),
        daily_theta_rs=round(daily_theta_rs, 2),
        dte_calendar_days=round(dte_calendar_days, 1),
        steps=steps,
        weekend_warning=weekend_warning,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Wall Intent Analysis Engine (Four Participants)
# ═══════════════════════════════════════════════════════════════════════════

def analyze_wall_intent(
    df: pd.DataFrame,
    spot: float,
    rules: Optional[dict] = None,
) -> WallIntentAnalysis:
    """
    Restates Open Interest walls into the four market participants:
      - Call Buyers: expect price to rise (supports push up)
      - Put Buyers: expect price to fall (pushes down)
      - Call Sellers: will not let it rise (resistance ceiling)
      - Put Sellers: will not let it fall (support floor)
    
    Highest Put OI = strongest support (Put sellers' fortress).
    Highest Call OI = strongest resistance (Call sellers' fortress).
    Dual Accumulation: Call buyers + Put sellers at same strike -> both want price above it.
    Dual Distribution: Call sellers + Put buyers at same strike -> both want price below it.
    """
    if df is None or df.empty or spot <= 0:
        return WallIntentAnalysis(
            spot=spot,
            max_call_oi_strike=0,
            max_put_oi_strike=0,
            max_call_oi=0.0,
            max_put_oi=0.0,
            strikes=[],
            summary_text="Option chain data unavailable for wall analysis.",
        )
    from analytics import deduplicate_strikes_live
    df = deduplicate_strikes_live(df, "analyze_wall_intent")

    r = rules or load_rules()
    focus_n = int(r.get("focus_strikes_count", 8))
    step = getattr(config, "STRIKE_STEP", 50)
    atm = int(round(spot / step) * step)

    # Filter window around ATM
    w = df[(df["strike"] >= atm - focus_n * step) & (df["strike"] <= atm + focus_n * step)].copy()
    if w.empty:
        w = df.copy()

    # Identify major walls
    max_call_row = df.loc[df["ce_oi"].fillna(0).idxmax()] if "ce_oi" in df.columns and not df["ce_oi"].isna().all() else None
    max_put_row = df.loc[df["pe_oi"].fillna(0).idxmax()] if "pe_oi" in df.columns and not df["pe_oi"].isna().all() else None

    max_c_strike = int(max_call_row["strike"]) if max_call_row is not None else 0
    max_p_strike = int(max_put_row["strike"]) if max_put_row is not None else 0
    max_c_oi = float(max_call_row["ce_oi"]) if max_call_row is not None else 0.0
    max_p_oi = float(max_put_row["pe_oi"]) if max_put_row is not None else 0.0

    # Retrieve four pillars classification
    fp_res = analytics.four_pillars(df, spot, strikes=focus_n)
    pillar_map: Dict[int, Tuple[str, str]] = {}
    for prow in fp_res.get("rows", []):
        pillar_map[prow.strike] = (prow.ce.pillar, prow.pe.pillar)

    strike_intents: List[WallParticipantIntent] = []

    for _, row in w.iterrows():
        try:
            k = int(row["strike"])
            ce_oi = float(row.get("ce_oi", 0) or 0)
            pe_oi = float(row.get("pe_oi", 0) or 0)
            ce_chg = float(row.get("ce_chg_oi", 0) or 0)
            pe_chg = float(row.get("pe_chg_oi", 0) or 0)
            ce_ltp = float(row.get("ce_ltp", 0) or 0)
            pe_ltp = float(row.get("pe_ltp", 0) or 0)

            ce_pil, pe_pil = pillar_map.get(k, ("flat", "flat"))

            # Call Intent
            if ce_pil == "long buildup":
                c_buyer_intent = "Call Buyers aggressive: expecting breakout above this level."
                c_seller_intent = "Call Sellers inactive / giving ground."
            elif ce_pil == "short buildup":
                c_buyer_intent = "Call Buyers discouraged by overhead supply."
                c_seller_intent = "Call Sellers defending ceiling: will not let price rise above."
            elif ce_pil == "short covering":
                c_buyer_intent = "Call Buyers holding firm."
                c_seller_intent = "Call Sellers retreating / short covering: ceiling giving way."
            else:
                c_buyer_intent = "Call Buyers unwinding."
                c_seller_intent = "Passive call positioning."

            # Put Intent
            if pe_pil == "short buildup":
                p_buyer_intent = "Put Buyers passive / sidelined."
                p_seller_intent = "Put Sellers defending floor: will not let price fall below."
            elif pe_pil == "long buildup":
                p_buyer_intent = "Put Buyers aggressive: expecting drop below this level."
                p_seller_intent = "Put Sellers stepping back."
            elif pe_pil == "short covering":
                p_buyer_intent = "Put Buyers maintaining pressure."
                p_seller_intent = "Put Sellers abandoning floor: support weakening."
            else:
                p_buyer_intent = "Put Buyers unwinding."
                p_seller_intent = "Passive put positioning."

            # Dual conditions
            dual_acc = (ce_pil in ("long buildup", "short covering")) and (pe_pil == "short buildup")
            dual_dist = (ce_pil == "short buildup") and (pe_pil in ("long buildup", "short covering"))

            if dual_acc:
                net_posture = "🟢 STRONG ACCUMULATION (Call Buyers + Put Sellers both want price ABOVE)"
            elif dual_dist:
                net_posture = "🔴 HEAVY RESISTANCE (Call Sellers + Put Buyers both want price BELOW)"
            elif k == max_c_strike:
                net_posture = "🛑 STRONGEST RESISTANCE (Max Call OI Fortress)"
            elif k == max_p_strike:
                net_posture = "🛡️ STRONGEST SUPPORT (Max Put OI Fortress)"
            elif ce_oi > pe_oi * 1.5:
                net_posture = "Call Heavy Ceiling"
            elif pe_oi > ce_oi * 1.5:
                net_posture = "Put Heavy Floor"
            else:
                net_posture = "Neutral Contested Strike"

            strike_intents.append(WallParticipantIntent(
                strike=k,
                ce_oi=ce_oi,
                pe_oi=pe_oi,
                ce_chg_oi=ce_chg,
                pe_chg_oi=pe_chg,
                ce_ltp=ce_ltp,
                pe_ltp=pe_ltp,
                ce_pillar=ce_pil,
                pe_pillar=pe_pil,
                call_buyer_intent=c_buyer_intent,
                call_seller_intent=c_seller_intent,
                put_buyer_intent=p_buyer_intent,
                put_seller_intent=p_seller_intent,
                dual_accumulation=dual_acc,
                dual_distribution=dual_dist,
                net_posture=net_posture,
            ))
        except Exception as err:
            continue

    summary = (
        f"Strongest Support sits at {max_p_strike:,} (Put Sellers' line in the sand with {max_p_oi:,.0f} contracts). "
        f"Strongest Resistance sits at {max_c_strike:,} (Call Sellers' fortress with {max_c_oi:,.0f} contracts). "
        f"Current spot is {spot:,.1f}."
    )

    return WallIntentAnalysis(
        spot=spot,
        max_call_oi_strike=max_c_strike,
        max_put_oi_strike=max_p_strike,
        max_call_oi=max_c_oi,
        max_put_oi=max_p_oi,
        strikes=strike_intents,
        summary_text=summary,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Complete Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run_strike_selection(
    df: Optional[pd.DataFrame],
    spot: float,
    direction: str = "CE",
    target_pts: float = 40.0,
    stop_pts: float = 40.0,
    premium_min: float = 200.0,
    premium_max: float = 300.0,
    lot_size: Optional[int] = None,
    capital: Optional[float] = None,
    dte_calendar_days: Optional[float] = None,
    now: Optional[datetime] = None,
    expiry_dt: Optional[datetime] = None,
    selected_strike: Optional[int] = None,
    read: Optional[Any] = None,
    rules: Optional[dict] = None,
) -> StrikeSelectionResult:
    """
    Main entry point for strike selection analysis.
    Safely coordinates IV regime analysis, strike candidates ranking,
    theta decay projection, and wall intent parsing.
    """
    now = now or datetime.now()
    r = rules or load_rules()
    actual_lot_size = lot_size if lot_size is not None else int(getattr(config, "LOT_SIZE", r.get("default_lot_size", 65)))
    actual_capital = capital if capital is not None else float(getattr(config, "CAPITAL", r.get("default_capital", 20000.0)))
    step = getattr(config, "STRIKE_STEP", 50)
    atm = int(round(spot / step) * step) if spot > 0 else 0

    # Guard 1: Empty chain
    if df is None or df.empty or spot <= 0:
        empty_iv = get_iv_regime(0.0, r)
        return StrikeSelectionResult(
            direction=direction,
            target_pts=target_pts,
            stop_pts=stop_pts,
            premium_min=premium_min,
            premium_max=premium_max,
            capital=actual_capital,
            lot_size=actual_lot_size,
            spot=spot,
            atm_strike=atm,
            atm_iv_read=empty_iv,
            candidates=[],
            top_by_roc=None,
            top_in_band=None,
            reference_comparison=r.get("source_reference_strikes", []),
            decay_projection=None,
            wall_analysis=None,
            guard_warning="Option chain is currently empty or spot price is zero.",
        )

    # Determine ATM IV
    atm_iv = 0.0
    if read is not None and hasattr(read, "atm_iv_ce") and hasattr(read, "atm_iv_pe"):
        atm_iv = (float(read.atm_iv_ce) + float(read.atm_iv_pe)) / 2.0
    else:
        # Look up ATM strike in df
        atm_row = df[df["strike"] == atm]
        if not atm_row.empty:
            c_iv = float(atm_row.iloc[0].get("ce_iv", 0) or 0)
            p_iv = float(atm_row.iloc[0].get("pe_iv", 0) or 0)
            atm_iv = (c_iv + p_iv) / 2.0 if (c_iv > 0 and p_iv > 0) else (c_iv or p_iv)
    if atm_iv <= 0.0:
        atm_iv = 13.5  # Fallback reasonable default if unpopulated

    iv_read = get_iv_regime(atm_iv, r)

    # Determine DTE
    actual_dte_days = float(dte_calendar_days) if dte_calendar_days is not None else 4.0
    dte_years = max(1e-5, actual_dte_days / 365.0)

    # Evaluate candidate strikes
    candidates = evaluate_strike_candidates(
        df=df,
        spot=spot,
        direction=direction,
        target_pts=target_pts,
        stop_pts=stop_pts,
        premium_min=premium_min,
        premium_max=premium_max,
        lot_size=actual_lot_size,
        capital=actual_capital,
        dte_years=dte_years,
        rules=r,
    )

    # Identify top picks
    top_by_roc = candidates[0] if candidates else None
    in_band_candidates = [c for c in candidates if c.in_premium_band]
    top_in_band = in_band_candidates[0] if in_band_candidates else None

    # Pick candidate for theta projection
    proj_candidate = None
    if selected_strike is not None:
        proj_candidate = next((c for c in candidates if c.strike == selected_strike), None)
    if proj_candidate is None:
        proj_candidate = top_in_band or top_by_roc or (candidates[0] if candidates else None)

    decay_proj = None
    if proj_candidate is not None:
        decay_proj = project_theta_decay(
            candidate=proj_candidate,
            spot=spot,
            now=now,
            expiry_dt=expiry_dt,
            dte_calendar_days=actual_dte_days,
            lot_size=actual_lot_size,
            rules=r,
        )

    # Wall intent analysis
    wall_analysis = analyze_wall_intent(df, spot, r)

    ref_comparison = r.get("source_reference_strikes", [])

    return StrikeSelectionResult(
        direction=direction,
        target_pts=target_pts,
        stop_pts=stop_pts,
        premium_min=premium_min,
        premium_max=premium_max,
        capital=actual_capital,
        lot_size=actual_lot_size,
        spot=spot,
        atm_strike=atm,
        atm_iv_read=iv_read,
        candidates=candidates,
        top_by_roc=top_by_roc,
        top_in_band=top_in_band,
        reference_comparison=ref_comparison,
        decay_projection=decay_proj,
        wall_analysis=wall_analysis,
    )
