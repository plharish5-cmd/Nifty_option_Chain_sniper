"""
next_candle.py — 15-Minute Next Candle Prediction Engine.

WHAT THIS MODULE DOES
─────────────────────
Evaluates the most recently closed 15-minute candle against technical, chain,
and order flow pillars to produce an honest directional or NO_CALL forecast
for the NEXT single 15-minute candle.

Produces one structured Prediction dataclass containing:
  • direction: UP | DOWN | NO_CALL
  • confidence: LOW | MEDIUM | HIGH (a count-based label, NEVER an invented percentage)
  • factors: list of Factor items detailing what agreed, disagreed, or remained neutral
  • stretch check: flags when price is overextended (> 1.5 ATR from 8 EMA) and downgrades confidence
  • baseline comparison: compares against trend-following baseline

WHAT THIS MODULE CANNOT DO
──────────────────────────
  1. It cannot forecast intra-candle wicks, targets, or stop-loss points.
  2. It does not place, modify, or cancel orders (Alert-only).
  3. It never evaluates the unclosed, forming candle.
  4. It never generates fabricated win-rate percentages (e.g. 86.5%).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import analytics as an
import config
import order_flow as of
import session_levels as sl
import setups as su
import strategy1 as s1

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "next_candle_rules.json"


def load_rules() -> dict:
    """Load configurable thresholds from next_candle_rules.json."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to read next_candle_rules.json: %s", e)
    return {
        "stretch_atr_mult": 1.5,
        "confidence_high_min_agree": 6,
        "confidence_high_max_disagree": 0,
        "confidence_medium_min_agree": 4,
        "confidence_high_min_score": 7.0,
        "confidence_medium_min_score": 4.5,
        "min_sample_size": 30,
        "min_candles_required": 10,
        "trend_one_way_threshold": 0.70,
        "doji_body_pct_max": 20.0,
        "doji_pts_max": 5.0,
        "atr_period": 14,
        "no_entry_after": "15:15",
        "factor_weights": {
            "Writers Striking Power": 3.0,
            "Option Chain Walls": 2.0,
            "PCR Dynamics": 1.5,
            "Participant Futures Bias": 1.5,
            "Close vs 8 EMA": 1.0,
            "4/8 EMA Momentum": 1.0,
            "Bollinger Band Stage": 1.0,
            "Candle Pattern": 1.0,
            "8 EMA Stretch": 1.0,
            "Position vs Extremes": 1.0,
            "Wall Churn": 1.0,
            "Volume & Tape Speed": 1.0
        },
        "writer_dominance_boost_multiplier": 1.5,
        "writer_trap_boost_multiplier": 2.0,
        "writer_ratio_dominant_threshold": 1.35,
        "writer_ratio_overwhelming_threshold": 1.80
    }


def _get_factor_weight(factor_name: str, rules: Optional[dict] = None) -> float:
    """Return the configured weight for a given factor from rules."""
    r = rules or load_rules()
    weights = r.get("factor_weights", {})
    return float(weights.get(factor_name, 1.0))


@dataclass
class Factor:
    name: str
    source: str
    stance: str       # "AGREE" | "DISAGREE" | "NEUTRAL"
    direction: str    # "UP" | "DOWN" | "NEUTRAL"
    reason: str
    weight: float = 1.0


@dataclass
class Prediction:
    at: datetime | pd.Timestamp
    candle_index: int
    direction: str             # "UP" | "DOWN" | "NO_CALL"
    confidence: str            # "LOW" | "MEDIUM" | "HIGH" (never a percentage)
    confidence_detail: str     # e.g. "6 of 8 agree, 0 disagree"
    factors: list[Factor] = field(default_factory=list)
    agree_count: int = 0
    disagree_count: int = 0
    neutral_count: int = 0
    baseline: str = "NO_CALL"  # Trend baseline ("UP" | "DOWN")
    agrees_baseline: bool = False
    is_stretched: bool = False
    stretch_atr: float = 0.0
    close_price: float = 0.0
    ema8: float = 0.0
    atr: float = 0.0
    notes: list[str] = field(default_factory=list)
    is_forming: bool = False
    target_candle_label: str = ""
    up_score: float = 0.0
    down_score: float = 0.0
    writer_ratio_label: str = ""


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range if not already present."""
    if "atr" in df.columns and df["atr"].notna().any():
        return df["atr"].ffill()
    h = df["high"]
    l = df["low"]
    c = df["close"]
    prev_c = c.shift(1)
    tr1 = h - l
    tr2 = (h - prev_c).abs()
    tr3 = (l - prev_c).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def get_participant_futures_factor(
    target_date: Optional[date] = None,
    rules: Optional[dict] = None
) -> Factor:
    """
    Evaluates participant-wise Future Index Long and Short positioning
    from the Master Excel / Parquet dataset (FII, PRO, Retailers, DII).
    Thresholds and settings are configured via next_candle_rules.json.
    """
    rules = rules or load_rules()
    threshold = float(rules.get("participant_fii_net_fut_threshold", 3000))
    p_path = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "participants.parquet"

    if not p_path.exists():
        return Factor(
            name="Participant Futures Bias",
            source="Participant-Wise OI & Option Chain Master Excel",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Participant parquet dataset not found"
        )

    try:
        df = pd.read_parquet(p_path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        dates = sorted(df["date"].unique())
        if len(dates) < 2:
            return Factor(
                name="Participant Futures Bias",
                source="Participant-Wise OI & Option Chain Master Excel",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason="Insufficient participant history (< 2 sessions)"
            )

        # Select active session pair
        if target_date is not None:
            avail = [d for d in dates if d <= target_date]
            if len(avail) >= 2:
                d_curr, d_prev = avail[-1], avail[-2]
            elif len(avail) == 1:
                d_curr, d_prev = dates[-1], dates[-2]
            else:
                d_curr, d_prev = dates[-1], dates[-2]
        else:
            d_curr, d_prev = dates[-1], dates[-2]

        fii_curr = df[(df["date"] == d_curr) & (df["participant"] == "FII")]
        fii_prev = df[(df["date"] == d_prev) & (df["participant"] == "FII")]
        cl_curr = df[(df["date"] == d_curr) & (df["participant"] == "Retailers")]
        cl_prev = df[(df["date"] == d_prev) & (df["participant"] == "Retailers")]

        if fii_curr.empty or fii_prev.empty:
            return Factor(
                name="Participant Futures Bias",
                source="Participant-Wise OI & Option Chain Master Excel",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason=f"FII data missing for session {d_curr}"
            )

        f_row_c = fii_curr.iloc[0]
        f_row_p = fii_prev.iloc[0]
        f_long_chg = float(f_row_c["fut_long"]) - float(f_row_p["fut_long"])
        f_short_chg = float(f_row_c["fut_short"]) - float(f_row_p["fut_short"])
        f_net_chg = f_long_chg - f_short_chg

        cl_long_chg = 0.0
        if not cl_curr.empty and not cl_prev.empty:
            cl_long_chg = float(cl_curr.iloc[0]["fut_long"]) - float(cl_prev.iloc[0]["fut_long"])

        base_w_part = _get_factor_weight("Participant Futures Bias", rules)
        # Institutional Short Dominance:
        if (f_short_chg >= f_long_chg + threshold) or (f_net_chg <= -threshold):
            contrarian_note = f", Retailers added {cl_long_chg:+,.0f} Longs (trapped)" if cl_long_chg > 1000 else ""
            return Factor(
                name="Participant Futures Bias",
                source="Participant-Wise OI & Option Chain Master Excel",
                stance="AGREE",
                direction="DOWN",
                reason=(
                    f"FII added +{f_short_chg:,.0f} Index Shorts vs +{f_long_chg:,.0f} Longs "
                    f"(Net {f_net_chg:+,.0f} contracts){contrarian_note} — Institutional derivative selling bias"
                ),
                weight=base_w_part
            )
        # Institutional Long Dominance:
        elif (f_long_chg >= f_short_chg + threshold) or (f_net_chg >= threshold):
            return Factor(
                name="Participant Futures Bias",
                source="Participant-Wise OI & Option Chain Master Excel",
                stance="AGREE",
                direction="UP",
                reason=(
                    f"FII added +{f_long_chg:,.0f} Index Longs vs +{f_short_chg:,.0f} Shorts "
                    f"(Net {f_net_chg:+,.0f} contracts) — Institutional derivative buying demand"
                ),
                weight=base_w_part
            )
        else:
            return Factor(
                name="Participant Futures Bias",
                source="Participant-Wise OI & Option Chain Master Excel",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason=(
                    f"FII futures changes balanced (+{f_long_chg:,.0f} Longs vs +{f_short_chg:,.0f} Shorts, "
                    f"Net {f_net_chg:+,.0f} contracts) within ±{threshold:,.0f} threshold"
                ),
                weight=base_w_part
            )
    except Exception as e:
        logger.error("Error evaluating participant futures factor: %s", e)
        return Factor(
            name="Participant Futures Bias",
            source="Participant-Wise OI & Option Chain Master Excel",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason=f"Participant evaluation error: {e}",
            weight=1.0
        )


def predict_next_candle(
    df_15m: pd.DataFrame,
    chain_df: Optional[pd.DataFrame] = None,
    prev_close: Optional[float] = None,
    rules: Optional[dict] = None,
    target_idx: Optional[int] = None,
    allow_forming: bool = True
) -> Prediction:
    """
    Produce a structured prediction for the NEXT candle following target_idx.
    
    If target_idx is None, evaluates the last CLOSED candle in df_15m.
    Guards defensively against insufficient data and missing indicators.
    """
    rules = rules or load_rules()
    
    # ── 1. Defensive Data Guards ─────────────────────────────────────────────
    if df_15m is None or df_15m.empty:
        return Prediction(
            at=datetime.now(),
            candle_index=-1,
            direction="NO_CALL",
            confidence="LOW",
            confidence_detail="0 of 0 agree",
            notes=["No candle data available."]
        )
    
    d = df_15m.copy()
    if "timestamp" not in d.columns:
        if isinstance(d.index, pd.DatetimeIndex):
            d["timestamp"] = d.index
        else:
            d["timestamp"] = pd.to_datetime(d.index, errors="coerce")
    else:
        d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
    
    d = d.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)

    # Filter closed candles unless forming candle evaluation is requested
    if not allow_forming:
        if "closed" in d.columns:
            d = d[d["closed"].fillna(True).astype(bool)].reset_index(drop=True)
        else:
            d = d.reset_index(drop=True)

    orig_len = len(d)
    ts_series = pd.to_datetime(d["timestamp"])
    unique_dates = ts_series.dt.date.unique()
    target_date = unique_dates[-1] if len(unique_dates) else None

    # Warmup history loading if fewer than min_candles_required (e.g. morning candles 1 to 9)
    if orig_len < rules.get("min_candles_required", 10) and target_date is not None:
        try:
            cand_dir = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "candles_15m"
            hist_files = sorted(cand_dir.glob("*.csv"))
            prev_file = None
            target_str = str(target_date)
            for f in hist_files:
                if f.stem < target_str:
                    prev_file = f
            if prev_file is not None:
                prev_df = pd.read_csv(prev_file)
                if "timestamp" in prev_df.columns:
                    prev_df["timestamp"] = pd.to_datetime(prev_df["timestamp"])
                    prev_df = prev_df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
                    d = pd.concat([prev_df.tail(25), d], ignore_index=True)
                    d = d.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
        except Exception as e:
            logger.error("Warmup loading error in predict_next_candle: %s", e)

    if len(d) < rules.get("min_candles_required", 10):
        ts = d["timestamp"].iloc[-1] if len(d) else datetime.now()
        return Prediction(
            at=ts,
            candle_index=len(d) - 1,
            direction="NO_CALL",
            confidence="LOW",
            confidence_detail="Insufficient candles",
            notes=[f"Fewer than {rules.get('min_candles_required', 10)} closed candles ({len(d)} present). Need baseline history."]
        )

    # Determine which candle to evaluate
    if target_idx is None:
        idx = len(d) - 1
        disp_idx = orig_len - 1
    else:
        warmup_count = len(d) - orig_len
        idx = warmup_count + target_idx
        if idx < 0 or idx >= len(d):
            idx = len(d) - 1
        disp_idx = target_idx

    sub_d = d.iloc[:idx + 1].copy()
    sub_d = su.prepare(sub_d)  # Ensure 4 EMA, 8 EMA, Bollinger Bands
    
    curr = sub_d.iloc[-1]
    prev = sub_d.iloc[-2] if len(sub_d) > 1 else None

    is_curr_forming = not bool(curr.get("closed", True))

    ts = curr["timestamp"]

    # Auto-load chain snapshot if not passed
    if chain_df is None and ts is not None:
        try:
            cand_dt = pd.to_datetime(ts)
            snap_dir = Path(getattr(config, "DATA_DIR", Path(__file__).parent / "data")) / "snapshots" / cand_dt.strftime("%Y-%m-%d")
            if snap_dir.exists():
                end_dt = cand_dt + pd.Timedelta(minutes=15)
                start_str = cand_dt.strftime("%H%M")
                end_str = end_dt.strftime("%H%M")
                all_snaps = sorted(snap_dir.glob("*.parquet"))
                cand_snaps = [f for f in all_snaps if start_str <= f.stem[:4] <= end_str]
                if cand_snaps:
                    chain_df = pd.read_parquet(cand_snaps[-1])
        except Exception as e:
            logger.debug("Failed auto-loading snapshot for %s: %s", ts, e)

    c = float(curr["close"])
    o = float(curr["open"])
    h = float(curr["high"])
    l = float(curr["low"])
    rng = h - l
    body = abs(c - o)
    body_pct = (body / rng * 100.0) if rng > 0 else 0.0
    
    e4 = float(curr.get("ema4", curr.get("ema8", c)))
    e8 = float(curr.get("ema8", c))
    
    atr_series = _compute_atr(sub_d, rules.get("atr_period", 14))
    atr_val = float(atr_series.iloc[-1]) if len(atr_series) and not np.isnan(atr_series.iloc[-1]) else max(15.0, rng)
    if atr_val <= 0:
        atr_val = 20.0
        
    # Baseline: Prevailing 8 EMA Side
    baseline_dir = "DOWN" if c < e8 else ("UP" if c > e8 else "NO_CALL")
    
    # ── 2. Stretch Check (8 EMA distance in ATR units) ───────────────────────
    dist_8ema = c - e8
    stretch_atr = abs(dist_8ema) / atr_val
    is_stretched = stretch_atr >= rules.get("stretch_atr_mult", 1.5)
    
    # ── 3. Detect Candlestick Patterns ───────────────────────────────────────
    pats_all = s1.detect(sub_d, s1.load_params())
    pat_tags = pats_all[-1] if len(pats_all) else []
    
    # Check for Doji / Indecision Compression
    is_doji = (body_pct <= rules.get("doji_body_pct_max", 20.0)) or (body <= rules.get("doji_pts_max", 5.0))
    is_inside_bar = False
    if prev is not None:
        is_inside_bar = (h <= float(prev["high"])) and (l >= float(prev["low"]))
        
    # ── 4. Stage & Bollinger Bands ───────────────────────────────────────────
    stage_str, ubb_slope, lbb_slope = s1._stage(sub_d, s1.load_params())
    
    # ── 5. Evaluate the 10 Factors ───────────────────────────────────────────
    factors: list[Factor] = []
    
    # Factor 1: Candle close vs 8 EMA (strategy1)
    if c > e8:
        factors.append(Factor(
            name="Close vs 8 EMA",
            source="strategy1",
            stance="AGREE",
            direction="UP",
            reason=f"Close ({c:,.1f}) holds above 8 EMA ({e8:,.1f}) by {dist_8ema:+.1f} pts"
        ))
    elif c < e8:
        factors.append(Factor(
            name="Close vs 8 EMA",
            source="strategy1",
            stance="AGREE",
            direction="DOWN",
            reason=f"Close ({c:,.1f}) closed below 8 EMA ({e8:,.1f}) by {dist_8ema:+.1f} pts"
        ))
    else:
        factors.append(Factor(
            name="Close vs 8 EMA",
            source="strategy1",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Close is exactly pinned at 8 EMA"
        ))
        
    # Factor 2: 4/8 EMA separation and direction (strategy1)
    if e4 > e8 + 1.0:
        factors.append(Factor(
            name="4/8 EMA Momentum",
            source="strategy1",
            stance="AGREE",
            direction="UP",
            reason=f"4 EMA ({e4:,.1f}) leads above 8 EMA ({e8:,.1f}) — bullish alignment"
        ))
    elif e4 < e8 - 1.0:
        factors.append(Factor(
            name="4/8 EMA Momentum",
            source="strategy1",
            stance="AGREE",
            direction="DOWN",
            reason=f"4 EMA ({e4:,.1f}) leads below 8 EMA ({e8:,.1f}) — bearish alignment"
        ))
    else:
        factors.append(Factor(
            name="4/8 EMA Momentum",
            source="strategy1",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason=f"4 EMA and 8 EMA compressed within {abs(e4 - e8):.1f} pts"
        ))
        
    # Factor 3: UBB / LBB slope — Stage 1/2/3 (strategy1)
    if "STAGE 2" in stage_str:
        factors.append(Factor(
            name="Bollinger Band Stage",
            source="strategy1",
            stance="AGREE",
            direction="UP",
            reason="Stage 2 expansion: Upper Bollinger Band sloping upward"
        ))
    elif "STAGE 3" in stage_str:
        factors.append(Factor(
            name="Bollinger Band Stage",
            source="strategy1",
            stance="AGREE",
            direction="DOWN",
            reason="Stage 3 expansion: Lower Bollinger Band sloping downward"
        ))
    else:
        factors.append(Factor(
            name="Bollinger Band Stage",
            source="strategy1",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Stage 1 rangebound: Bands are horizontally compressed"
        ))
        
    # Factor 4: Candle pattern on the closed/forming bar (strategy1 / price action)
    bull_pats = {"HAMMER", "DOUBLE HAMMER", "BULLISH ENGULF", "INVERTED HAMMER"}
    bear_pats = {"SHOOTING STAR", "DOUBLE IH", "BEARISH ENGULF"}
    active_bull = [p for p in pat_tags if p in bull_pats]
    active_bear = [p for p in pat_tags if p in bear_pats]

    is_opening_rejection = False
    if disp_idx == 0 and prev_close is not None:
        gap_pts = o - prev_close
        drop_from_open = o - c
        bounce_from_open = c - o
        rejection_thresh = rules.get("opening_rejection_atr_ratio", 0.4) * atr_val
        if gap_pts >= 40.0 and drop_from_open >= rejection_thresh:
            is_opening_rejection = True
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="DOWN",
                reason=f"Opening Gap-Up ({gap_pts:+.1f} pts) rejected with heavy supply drop ({drop_from_open:.1f} pts from open)"
            ))
        elif gap_pts <= -40.0 and bounce_from_open >= rejection_thresh:
            is_opening_rejection = True
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="UP",
                reason=f"Opening Gap-Down ({gap_pts:+.1f} pts) absorbed with strong demand bounce ({bounce_from_open:.1f} pts from open)"
            ))

    if not is_opening_rejection:
        if is_doji:
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason=f"Doji / tight body ({body:.1f} pts, {body_pct:.0f}% body) — market hesitation"
            ))
        elif active_bear:
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="DOWN",
                reason=f"Bearish pattern detected: {', '.join(active_bear)}"
            ))
        elif active_bull:
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="UP",
                reason=f"Bullish pattern detected: {', '.join(active_bull)}"
            ))
        elif c > o:
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="UP",
                reason=f"Solid green candle ({body:.1f} pts body)"
            ))
        else:
            factors.append(Factor(
                name="Candle Pattern",
                source="strategy1",
                stance="AGREE",
                direction="DOWN",
                reason=f"Solid red candle ({body:.1f} pts body)"
            ))
        
    # Factor 5: Distance from 8 EMA in ATR units (computed stretch check)
    if is_stretched:
        stretched_dir = "DOWN" if c < e8 else "UP"
        counter_dir = "UP" if stretched_dir == "DOWN" else "DOWN"
        factors.append(Factor(
            name="8 EMA Stretch",
            source="computed",
            stance="DISAGREE",
            direction=counter_dir,
            reason=f"Stretched {stretch_atr:.1f} ATR from 8 EMA ({abs(dist_8ema):.1f} pts) — counter-trend relief bar overdue"
        ))
    else:
        factors.append(Factor(
            name="8 EMA Stretch",
            source="computed",
            stance="AGREE",
            direction=baseline_dir,
            reason=f"Normal proximity ({stretch_atr:.1f} ATR from 8 EMA, threshold 1.5 ATR)"
        ))
        
    # Factor 6: Position vs Day High / Day Low (session_levels)
    try:
        hi_ext, lo_ext = sl.extremes(sub_d)
        dh = hi_ext.price if hi_ext else float(sub_d["high"].max())
        dl = lo_ext.price if lo_ext else float(sub_d["low"].min())
        
        dist_dh = abs(c - dh)
        dist_dl = abs(c - dl)
        
        if dist_dl <= 15.0:
            factors.append(Factor(
                name="Position vs Extremes",
                source="session_levels",
                stance="AGREE",
                direction="DOWN",
                reason=f"Pressing Day Low ({dl:,.1f}) within {dist_dl:.1f} pts — breakdown zone"
            ))
        elif dist_dh <= 15.0:
            factors.append(Factor(
                name="Position vs Extremes",
                source="session_levels",
                stance="AGREE",
                direction="UP",
                reason=f"Pressing Day High ({dh:,.1f}) within {dist_dh:.1f} pts — breakout zone"
            ))
        else:
            factors.append(Factor(
                name="Position vs Extremes",
                source="session_levels",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason=f"Trading inside daily range (DH: {dh:,.1f}, DL: {dl:,.1f})"
            ))
    except Exception as e:
        logger.error("Error evaluating session extremes: %s", e)
        factors.append(Factor(
            name="Position vs Extremes",
            source="session_levels",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Daily extremes calculation unavailable"
        ))
        
    # Factor 7: Chain: CE vs PE OI change at nearest walls (analytics.four_pillars)
    base_w_walls = _get_factor_weight("Option Chain Walls", rules)
    w_dom_thresh = float(rules.get("writer_ratio_dominant_threshold", 1.35))
    w_boost = float(rules.get("writer_dominance_boost_multiplier", 1.5))
    if chain_df is not None and not chain_df.empty:
        try:
            fp = an.four_pillars(chain_df, spot=c, strikes=6)
            ce_dom = fp.get("ce_dominant", "flat")
            pe_dom = fp.get("pe_dominant", "flat")
            tot_ce_chg = chain_df["ce_chg_oi"].sum()
            tot_pe_chg = chain_df["pe_chg_oi"].sum()
            
            if tot_ce_chg > tot_pe_chg + 150000:
                cpr_ratio = tot_ce_chg / max(tot_pe_chg, 1.0)
                w = base_w_walls * (w_boost if cpr_ratio >= w_dom_thresh else 1.0)
                factors.append(Factor(
                    name="Option Chain Walls",
                    source="analytics.four_pillars",
                    stance="AGREE",
                    direction="DOWN",
                    reason=f"Aggressive Call Writing (+{tot_ce_chg:,.0f} CE vs +{tot_pe_chg:,.0f} PE | Call/Put Writers: {cpr_ratio:.2f}x) establishes ceiling",
                    weight=w
                ))
            elif tot_pe_chg > tot_ce_chg + 150000:
                pcr_ratio = tot_pe_chg / max(tot_ce_chg, 1.0)
                w = base_w_walls * (w_boost if pcr_ratio >= w_dom_thresh else 1.0)
                factors.append(Factor(
                    name="Option Chain Walls",
                    source="analytics.four_pillars",
                    stance="AGREE",
                    direction="UP",
                    reason=f"Aggressive Put Writing (+{tot_pe_chg:,.0f} PE vs +{tot_ce_chg:,.0f} CE | Put/Call Writers: {pcr_ratio:.2f}x) establishes floor",
                    weight=w
                ))
            else:
                factors.append(Factor(
                    name="Option Chain Walls",
                    source="analytics.four_pillars",
                    stance="NEUTRAL",
                    direction="NEUTRAL",
                    reason=f"Balanced OI change (+{tot_ce_chg:,.0f} CE vs +{tot_pe_chg:,.0f} PE)",
                    weight=base_w_walls
                ))
        except Exception as e:
            logger.error("Error evaluating four_pillars: %s", e)
            factors.append(Factor(
                name="Option Chain Walls",
                source="analytics.four_pillars",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason="Option chain pillars computation failed",
                weight=base_w_walls
            ))
    else:
        factors.append(Factor(
            name="Option Chain Walls",
            source="analytics.four_pillars",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="No option chain snapshot provided for this candle",
            weight=base_w_walls
        ))
        
    # Factor 8: PCR state and direction (analytics.pcr_change_state)
    base_w_pcr = _get_factor_weight("PCR Dynamics", rules)
    if chain_df is not None and not chain_df.empty:
        try:
            d_ce = float(chain_df["ce_chg_oi"].sum())
            d_pe = float(chain_df["pe_chg_oi"].sum())
            pcr_val, pcr_state, pcr_note = an.pcr_change_state(d_ce, d_pe)
            
            if "Calls building, puts unwinding" in pcr_state or (pcr_val is not None and pcr_val < 0.70):
                factors.append(Factor(
                    name="PCR Dynamics",
                    source="analytics.pcr_change_state",
                    stance="AGREE",
                    direction="DOWN",
                    reason=f"Bearish PCR state: {pcr_state} (Call writers aggressively replacing Put writers)",
                    weight=base_w_pcr
                ))
            elif "Puts building, calls unwinding" in pcr_state or (pcr_val is not None and pcr_val > 1.25):
                factors.append(Factor(
                    name="PCR Dynamics",
                    source="analytics.pcr_change_state",
                    stance="AGREE",
                    direction="UP",
                    reason=f"Bullish PCR state: {pcr_state} (Put writers aggressively replacing Call writers)",
                    weight=base_w_pcr
                ))
            else:
                factors.append(Factor(
                    name="PCR Dynamics",
                    source="analytics.pcr_change_state",
                    stance="NEUTRAL",
                    direction="NEUTRAL",
                    reason=f"Neutral PCR condition: {pcr_state}",
                    weight=base_w_pcr
                ))
        except Exception as e:
            logger.error("Error evaluating pcr_change_state: %s", e)
            factors.append(Factor(
                name="PCR Dynamics",
                source="analytics.pcr_change_state",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason="PCR dynamics computation failed",
                weight=base_w_pcr
            ))
    else:
        factors.append(Factor(
            name="PCR Dynamics",
            source="analytics.pcr_change_state",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="PCR data unavailable",
            weight=base_w_pcr
        ))
        
    # Factor 9: Churn at the wall in play (flow.ChurnFlag)
    if chain_df is not None and not chain_df.empty and "ce_vol" in chain_df.columns:
        try:
            # Volume-to-OI absorption heuristic
            atm_strike = round(c / 50.0) * 50.0
            atm_row = chain_df[chain_df["strike"] == atm_strike]
            if not atm_row.empty:
                c_vol = float(atm_row["ce_vol"].iloc[0])
                p_vol = float(atm_row["pe_vol"].iloc[0])
                c_oi = float(atm_row["ce_oi"].iloc[0])
                p_oi = float(atm_row["pe_oi"].iloc[0])
                
                c_ratio = c_vol / c_oi if c_oi > 0 else 0
                p_ratio = p_vol / p_oi if p_oi > 0 else 0
                
                if c_ratio > 3.0 and c_ratio > p_ratio * 1.5:
                    factors.append(Factor(
                        name="Wall Churn",
                        source="flow.ChurnFlag",
                        stance="AGREE",
                        direction="DOWN",
                        reason=f"High Call churn ({c_ratio:.1f}×) at ATM strike {int(atm_strike)} — heavy resistance defense"
                    ))
                elif p_ratio > 3.0 and p_ratio > c_ratio * 1.5:
                    factors.append(Factor(
                        name="Wall Churn",
                        source="flow.ChurnFlag",
                        stance="AGREE",
                        direction="UP",
                        reason=f"High Put churn ({p_ratio:.1f}×) at ATM strike {int(atm_strike)} — heavy support defense"
                    ))
                else:
                    factors.append(Factor(
                        name="Wall Churn",
                        source="flow.ChurnFlag",
                        stance="NEUTRAL",
                        direction="NEUTRAL",
                        reason="Normal strike churn rates"
                    ))
            else:
                factors.append(Factor(
                    name="Wall Churn",
                    source="flow.ChurnFlag",
                    stance="NEUTRAL",
                    direction="NEUTRAL",
                    reason="ATM strike row missing"
                ))
        except Exception as e:
            logger.error("Error evaluating churn: %s", e)
            factors.append(Factor(
                name="Wall Churn",
                source="flow.ChurnFlag",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason="Churn calculation error"
            ))
    else:
        factors.append(Factor(
            name="Wall Churn",
            source="flow.ChurnFlag",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Volume data unavailable for churn analysis"
        ))
        
    # Factor 10: Tape speed & Volume Profile (order_flow)
    vol_curr = float(curr.get("volume", 0.0))
    vol_rolling = sub_d["volume"].rolling(8, min_periods=1).mean().iloc[-1] if "volume" in sub_d.columns else 0.0
    
    if vol_rolling > 0 and vol_curr > vol_rolling * 1.25:
        bar_flow_dir = "DOWN" if c < o else ("UP" if c > o else baseline_dir)
        factors.append(Factor(
            name="Volume & Tape Speed",
            source="order_flow",
            stance="AGREE",
            direction=bar_flow_dir,
            reason=f"Volume accelerating ({vol_curr/1e6:.2f}M vs {vol_rolling/1e6:.2f}M avg) confirming {'selling pressure' if bar_flow_dir=='DOWN' else 'buying demand'}"
        ))
    elif vol_rolling > 0 and vol_curr < vol_rolling * 0.70:
        factors.append(Factor(
            name="Volume & Tape Speed",
            source="order_flow",
            stance="DISAGREE",
            direction="NEUTRAL",
            reason=f"Volume stalling ({vol_curr/1e6:.2f}M vs {vol_rolling/1e6:.2f}M avg) — low conviction"
        ))
    else:
        factors.append(Factor(
            name="Volume & Tape Speed",
            source="order_flow",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Volume tracking within normal rolling bounds"
        ))

    # Factor 11: Participant Futures Bias (from Participant-Wise OI & Option Chain Master Excel)
    cand_dt_obj = pd.to_datetime(ts).date() if ts is not None else None
    factors.append(get_participant_futures_factor(target_date=cand_dt_obj, rules=rules))

    # Factor 12: Call vs Put Writers Striking Price Power (CE vs PE Aggression at Striking Strike)
    base_w_wge = _get_factor_weight("Writers Striking Power", rules)
    trap_boost = float(rules.get("writer_trap_boost_multiplier", 2.0))
    dom_boost = float(rules.get("writer_dominance_boost_multiplier", 1.5))
    writer_ratio_label = "⚪ Writers Balanced"

    if chain_df is not None and not chain_df.empty:
        try:
            import writers_graph_engine as wge
            atm_k = round(c / 50.0) * 50.0
            cw_pwr = wge.evaluate_striking_price_prediction_power(chain_df, spot=c, striking_strike=atm_k)
            cpr_ratio = (cw_pwr.ce_chg_oi / max(cw_pwr.pe_chg_oi, 1.0)) if cw_pwr.pe_chg_oi > 0 else (2.5 if cw_pwr.ce_chg_oi > 0 else 1.0)
            pcr_ratio = cw_pwr.strike_writer_pcr

            if cw_pwr.strike_dominance == "PUT_WRITERS_DOMINATING":
                writer_ratio_label = f"🟢 Put Writers / Call Writers: {pcr_ratio:.2f}x @ {atm_k:,.0f}"
            elif cw_pwr.strike_dominance == "CALL_WRITERS_DOMINATING":
                writer_ratio_label = f"🔴 Call Writers / Put Writers: {cpr_ratio:.2f}x @ {atm_k:,.0f}"
            elif cw_pwr.focus_net_diff > 0:
                writer_ratio_label = f"🟢 Focus Put Writers: {cw_pwr.focus_writer_pcr:.2f}x"
            elif cw_pwr.focus_net_diff < 0:
                writer_ratio_label = f"🔴 Focus Call Writers: {1.0 / max(cw_pwr.focus_writer_pcr, 0.01):.2f}x"

            if cw_pwr.trap_risk == "BEAR_TRAP_RISK":
                w = base_w_wge * trap_boost
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="AGREE",
                    direction="UP",
                    reason=f"⚠️ Bear Trap Active at {atm_k:,.0f}: Spot breached low but Put Writers aggressively built (+{cw_pwr.pe_chg_oi:,.0f} PE vs +{cw_pwr.ce_chg_oi:,.0f} CE | Put/Call Writers: {pcr_ratio:.2f}x) — upward rebound expected",
                    weight=w
                ))
            elif cw_pwr.trap_risk == "BULL_TRAP_RISK":
                w = base_w_wge * trap_boost
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="AGREE",
                    direction="DOWN",
                    reason=f"⚠️ Bull Trap Active at {atm_k:,.0f}: Spot breached high but Call Writers fortified (+{cw_pwr.ce_chg_oi:,.0f} CE vs +{cw_pwr.pe_chg_oi:,.0f} PE | Call/Put Writers: {cpr_ratio:.2f}x) — downward rejection expected",
                    weight=w
                ))
            elif cw_pwr.expected_direction == "UP" and cw_pwr.confidence == "HIGH":
                w = base_w_wge * dom_boost
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="AGREE",
                    direction="UP",
                    reason=f"Put Writers heavily defending striking price {atm_k:,.0f} (+{cw_pwr.pe_chg_oi:,.0f} PE vs +{cw_pwr.ce_chg_oi:,.0f} CE | Put/Call Writers: {pcr_ratio:.2f}x) — {cw_pwr.win_rate_edge_pct:.0f}% win rate edge",
                    weight=w
                ))
            elif cw_pwr.expected_direction == "DOWN" and cw_pwr.confidence == "HIGH":
                w = base_w_wge * dom_boost
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="AGREE",
                    direction="DOWN",
                    reason=f"Call Writers heavily capping striking price {atm_k:,.0f} (+{cw_pwr.ce_chg_oi:,.0f} CE vs +{cw_pwr.pe_chg_oi:,.0f} PE | Call/Put Writers: {cpr_ratio:.2f}x) — {cw_pwr.win_rate_edge_pct:.0f}% win rate edge",
                    weight=w
                ))
            elif cw_pwr.expected_direction in ("UP", "DOWN"):
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="AGREE",
                    direction=cw_pwr.expected_direction,
                    reason=f"Writers leaning {cw_pwr.expected_direction} at {atm_k:,.0f} (PE ΔOI: {cw_pwr.pe_chg_oi:+,.0f}, CE ΔOI: {cw_pwr.ce_chg_oi:+,.0f})",
                    weight=base_w_wge
                ))
            else:
                factors.append(Factor(
                    name="Writers Striking Power",
                    source="writers_graph_engine",
                    stance="NEUTRAL",
                    direction="NEUTRAL",
                    reason=f"Writers balanced at striking price {atm_k:,.0f} (PE ΔOI: {cw_pwr.pe_chg_oi:+,.0f}, CE ΔOI: {cw_pwr.ce_chg_oi:+,.0f})",
                    weight=1.0
                ))
        except Exception as _e:
            logger.warning("Error evaluating writers striking power factor: %s", _e)
            factors.append(Factor(
                name="Writers Striking Power",
                source="writers_graph_engine",
                stance="NEUTRAL",
                direction="NEUTRAL",
                reason="Writers calculation error",
                weight=1.0
            ))
    else:
        factors.append(Factor(
            name="Writers Striking Power",
            source="writers_graph_engine",
            stance="NEUTRAL",
            direction="NEUTRAL",
            reason="Option chain snapshot unavailable for striking price evaluation",
            weight=1.0
        ))

    # ── 6. Aggregate Factor Votes & Determine Direction (Weighted Synthesis) ──
    up_votes = [f for f in factors if f.direction == "UP" and f.stance == "AGREE"]
    down_votes = [f for f in factors if f.direction == "DOWN" and f.stance == "AGREE"]
    disagree_votes = [f for f in factors if f.stance == "DISAGREE"]
    neutral_votes = [f for f in factors if f.stance == "NEUTRAL"]
    
    up_count = len(up_votes)
    down_count = len(down_votes)
    disagree_count = len(disagree_votes)
    neutral_count = len(neutral_votes)

    up_score = sum(getattr(f, "weight", 1.0) for f in up_votes)
    down_score = sum(getattr(f, "weight", 1.0) for f in down_votes)
    disagree_score = sum(getattr(f, "weight", 1.0) for f in disagree_votes)

    min_high_score = float(rules.get("confidence_high_min_score", 7.0))
    min_med_score = float(rules.get("confidence_medium_min_score", 4.5))

    # Check for first-class NO_CALL conditions:
    # 1. Inside bar + Doji
    # 2. Split scores / balanced market
    if is_inside_bar and is_doji:
        direction = "NO_CALL"
        confidence = "LOW"
        agree_count = 0
        conf_detail = "Inside Bar + Doji compression — no directional edge"
        notes = ["Price is compressed within the previous candle's range. No call is warranted."]
    elif abs(up_score - down_score) < 0.75 and max(up_score, down_score) < 4.0:
        direction = "NO_CALL"
        confidence = "LOW"
        agree_count = max(up_count, down_count)
        conf_detail = f"Split factors ({up_score:.1f} UP vs {down_score:.1f} DOWN pts) — market balanced"
        notes = ["Technical and chain factors are evenly divided. Awaiting directional expansion."]
    elif down_score > up_score:
        direction = "DOWN"
        agree_count = down_count
        conf_detail = f"{agree_count} agree ({down_score:.1f} weighted pts), {disagree_count} disagree"
        notes = []
        if (agree_count >= rules.get("confidence_high_min_agree", 6) or down_score >= min_high_score) and disagree_count <= rules.get("confidence_high_max_disagree", 0):
            confidence = "HIGH"
        elif (agree_count >= rules.get("confidence_medium_min_agree", 4) or down_score >= min_med_score) and down_score > disagree_score:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
    else:
        direction = "UP"
        agree_count = up_count
        conf_detail = f"{agree_count} agree ({up_score:.1f} weighted pts), {disagree_count} disagree"
        notes = []
        if (agree_count >= rules.get("confidence_high_min_agree", 6) or up_score >= min_high_score) and disagree_count <= rules.get("confidence_high_max_disagree", 0):
            confidence = "HIGH"
        elif (agree_count >= rules.get("confidence_medium_min_agree", 4) or up_score >= min_med_score) and up_score > disagree_score:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
            
    # ── 7. Stretch Downgrade Rule ────────────────────────────────────────────
    # Beyond 1.5 ATR from 8 EMA: downgrade confidence by one level.
    # Do NOT flip direction; a stretched trend is still a trend. Just stop calling it HIGH.
    if is_stretched and direction in ("UP", "DOWN"):
        if confidence == "HIGH":
            confidence = "MEDIUM"
            conf_detail += " [Downgraded: 8 EMA Stretched]"
            notes.append(f"Price is {stretch_atr:.1f} ATR from 8 EMA. Confidence downgraded to MEDIUM due to overdue relief bar risk.")
        elif confidence == "MEDIUM":
            confidence = "LOW"
            conf_detail += " [Downgraded: 8 EMA Stretched]"
            notes.append(f"Price is {stretch_atr:.1f} ATR from 8 EMA. Confidence downgraded to LOW due to overdue relief bar risk.")
            
    # Mark alignment against baseline
    agrees_baseline = (direction == baseline_dir)
    
    target_label = f"Next 15m Candle ({disp_idx + 2:02d})"
    if disp_idx == 0 and is_curr_forming:
        target_label = "Next 15m Candle (09:30–09:45)"
        notes.append("[FORMING CANDLE ANALYSIS: 09:15–09:30 bar in progress; projected for 09:30 opening]")
    elif is_curr_forming:
        notes.append(f"[FORMING CANDLE ANALYSIS: Candle {disp_idx + 1:02d} in progress; projected for next bar]")

    return Prediction(
        at=ts,
        candle_index=disp_idx,
        direction=direction,
        confidence=confidence,
        confidence_detail=conf_detail,
        factors=factors,
        agree_count=agree_count,
        disagree_count=disagree_count,
        neutral_count=neutral_count,
        baseline=baseline_dir,
        agrees_baseline=agrees_baseline,
        is_stretched=is_stretched,
        stretch_atr=stretch_atr,
        close_price=c,
        ema8=e8,
        atr=atr_val,
        notes=notes,
        is_forming=is_curr_forming,
        target_candle_label=target_label,
        up_score=up_score,
        down_score=down_score,
        writer_ratio_label=writer_ratio_label
    )


def predict_session_candles(
    df_15m: pd.DataFrame,
    chain_snapshots: Optional[dict] = None,
    prev_close: Optional[float] = None,
    rules: Optional[dict] = None
) -> list[Prediction]:
    """
    Run prediction engine sequentially across every closed candle of a session.
    """
    rules = rules or load_rules()
    if df_15m is None or df_15m.empty:
        return []
        
    d = df_15m.copy()
    if "closed" in d.columns:
        d = d[d["closed"].fillna(True).astype(bool)].reset_index(drop=True)
        
    preds: list[Prediction] = []
    for i in range(len(d)):
        curr_row = d.iloc[i]
        curr_ts = curr_row.get("timestamp")
        
        # Match chain snapshot if provided
        snap_df = None
        if chain_snapshots and curr_ts is not None:
            ts_str = pd.to_datetime(curr_ts).strftime("%H%M")
            snap_df = chain_snapshots.get(ts_str)
            
        p = predict_next_candle(
            df_15m=d,
            chain_df=snap_df,
            prev_close=prev_close,
            rules=rules,
            target_idx=i
        )
        preds.append(p)
        
    return preds
