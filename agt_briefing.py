"""
agt_briefing.py — NIFTY Live Option Chain Market Briefing for Antigravity Terminal.

Reads directly from local parquet snapshots stored by the scanner.
Runs every 3 minutes (e.g. via 'watch -n 180 .venv/bin/python agt_briefing.py').
Alert-only. Zero web calls. No order placement or modification.

Rules:
  - Imports only: store, oi_timeline, analytics, config, and stdlib.
  - All thresholds read from strategy1_rules.json (zero hardcoding).
  - Never crashes on missing data (prints 'N/A' and exits cleanly).
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# Core repository imports
import analytics
import config
import oi_timeline
import session_levels
import store
from scheduler import send_telegram_alert

logger = logging.getLogger("agt_briefing")

# Canonical code vintage tag for observation logs and briefings
CODE_VINTAGE: str = "v2.1-clean"

# ═══════════════════════════════════════════════════════════════════════════
# 1. Dynamic Rule & Threshold Loader (strategy1_rules.json)
# ═══════════════════════════════════════════════════════════════════════════

RULES_PATH = Path(__file__).parent / "strategy1_rules.json"


def load_briefing_rules() -> dict:
    """Load configuration and thresholds from strategy1_rules.json with fallbacks."""
    defaults = {
        "dominance_min_contracts": 50000.0,
        "multistrike_discontinuity_threshold_contracts": 50000.0,
        "roll_jump_threshold_pct": 0.35,
        "resample_minutes": 3,
        "quadrant_local_minutes": 15,
        "warmup_until": "09:50",
        "eod_from": "15:00",
        "stale_minutes": 10,
        "freeze_start": "11:30",
        "freeze_end": "13:15",
        "display_lot_size": 65,
        "display_units": "lakhs",
        "step": 50.0,
        "major_wall_threshold": 75000.0,
        "writers_ratio_pe_threshold": 1.5,
        "writers_ratio_ce_threshold": 0.67,
        "writers_ratio_early_session_threshold": 1.3,
        "writers_ratio_early_session_until": "10:45",
        "sr_approach_threshold_pts": 15.0,
        "sr_approach_cooldown_minutes": 30,
        "freeze_end_alert_enabled": True,
        "freeze_end_alert_scope": "near",
        "eod_summary_time": "15:25",
        "eod_summary_enabled": True,
        "s1_reclaim_trap_alert_enabled": True,
        "telegram_briefing_enabled": True,
        "floor_weakening_consecutive_bars": 2,
        "gex_lot_size": 65,
        "gex_max_null_pct": 0.30,
        "gex_neutral_pct": 0.10,
        "gex_extreme_distance_pts": 200.0,
        "expiry_pcr_swing_threshold": 1.0,
        "expiry_pcr_swing_cooldown": 15,
        "expiry_unwind_time": "14:00",
        "expiry_max_pain_telegram_time": "13:00",
        "expiry_summary_time": "13:15",
        "iv_window_minutes": 30,
        "iv_cheap_percentile": 35.0,
        "iv_expensive_percentile": 65.0,
        "iv_min_sessions_required": 3,
        "iv_max_pct_jump_per_poll": 25.0,
        "iv_carry_max_polls": 5,
        "iv_reference_mode": "interpolate",
        "chk_box1_min_wall_contracts": 50000.0,
        "chk_box2_surge_limit_contracts": 5000.0,
        "chk_box3_expensive_percentile": 65.0,
        "chk_box4_gex_threshold_cr": 10.0,
        "chk_box5_market_open_time": "09:20",
        "chk_box5_freeze_start": "11:30",
        "chk_box5_freeze_end": "13:15",
        "chk_box5_freeze_grace_end": "13:30",
        "chk_box5_late_cutoff_time": "14:45",
        "chk_box5_fresh_data_minutes": 5.0,
        "observations_enabled": True,
        "observations_dir": "data/observations",
        "observations_market_open_time": "09:15",
        "observations_freeze_end_time": "13:15",
        "observations_eod_summary_time": "15:25",
        "max_gap_seconds": 180.0,
        "data_gap": {
            "max_gap_seconds": 180.0,
        },
        "mj_mode": {
            "enabled": True,
            "shadow_logging": True,
            "lot_size_mj": 1,
            "target_pts": 25,
            "no_trade_after": "14:00",
        },
        "writer_dominance": {
            "enabled": True,
            "atm_steps": 2,
            "dominance_strike_span": 2,
            "bands": {
                "balanced_max": 1.3,
                "mild_max": 2.0,
                "clear_max": 3.0,
            },
            "labels": {
                "balanced": "BALANCED",
                "mild": "MILD",
                "clear": "CLEAR",
                "heavy": "HEAVY",
            },
            "structure": {
                "range_min_width_pts": 100,
                "compression_max_width_pts": 50,
                "trending_threshold": 2.0,
            },
            "asymmetry": {
                "min_gap": 2.0,
                "conviction_bands": {
                    "weak_max": 1.5,
                    "firm_max": 3.0,
                },
                "conviction_labels": {
                    "weak": "WEAK",
                    "firm": "FIRM",
                    "strong": "STRONG",
                },
            },
            "risk_staging": {
                "cascade_delta_threshold": -25000,
                "proximity_pts": 25.0,
            },
            "telegram_line_enabled": True,
        },
    }

    if not RULES_PATH.exists():
        return defaults

    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            oi_tl = data.get("oi_timeline", {})
            oi_al = data.get("oi_alerts", {})
            gex_cfg = data.get("gex", {})
            expiry_cfg = data.get("expiry_mode", {})
            iv_cfg = data.get("iv_context", {})
            chk_cfg = data.get("checklist", {})
            obs_cfg = data.get("observations", {})
            dom_cfg = data.get("writer_dominance", {})
            data_gap = data.get("data_gap", {})
            return {
                "dominance_min_contracts": float(oi_tl.get("dominance_min_contracts", 50000.0)),
                "multistrike_discontinuity_threshold_contracts": float(
                    oi_tl.get("multistrike_discontinuity_threshold_contracts", 50000.0)
                ),
                "roll_jump_threshold_pct": float(oi_tl.get("roll_jump_threshold_pct", 0.35)),
                "resample_minutes": int(oi_tl.get("resample_minutes", 3)),
                "quadrant_local_minutes": int(oi_tl.get("quadrant_local_minutes", 15)),
                "warmup_until": str(oi_al.get("warmup_until", oi_tl.get("warmup_until", "09:50"))),
                "eod_from": str(oi_al.get("eod_from", oi_tl.get("eod_from", "15:00"))),
                "stale_minutes": int(oi_tl.get("stale_minutes", 10)),
                "freeze_start": str(oi_al.get("freeze_start", "11:30")),
                "freeze_end": str(oi_al.get("freeze_end", "13:15")),
                "display_lot_size": int(oi_tl.get("display_lot_size", getattr(config, "LOT_SIZE", 65))),
                "display_units": str(oi_tl.get("display_units", "lakhs")),
                "step": float(getattr(config, "STRIKE_STEP", 50.0)),
                "major_wall_threshold": float(oi_al.get("major_wall_threshold", 75000.0)),
                "writers_ratio_pe_threshold": float(oi_al.get("writers_ratio_pe_threshold", 1.5)),
                "writers_ratio_ce_threshold": float(oi_al.get("writers_ratio_ce_threshold", 0.67)),
                "writers_ratio_early_session_threshold": float(
                    oi_al.get("writers_ratio_early_session_threshold", data.get("writers_ratio_early_session_threshold", 1.3))
                ),
                "writers_ratio_early_session_until": str(
                    oi_al.get("writers_ratio_early_session_until", data.get("writers_ratio_early_session_until", "10:45"))
                ),
                "sr_approach_threshold_pts": float(
                    oi_al.get("sr_approach_threshold_pts", data.get("sr_approach_threshold_pts", 15.0))
                ),
                "sr_approach_cooldown_minutes": int(
                    oi_al.get("sr_approach_cooldown_minutes", 30)
                ),
                "freeze_end_alert_enabled": bool(oi_al.get("freeze_end_alert_enabled", True)),
                "freeze_end_alert_scope": str(oi_al.get("freeze_end_alert_scope", "near")),
                "eod_summary_time": str(oi_al.get("eod_summary_time", "15:25")),
                "eod_summary_enabled": bool(oi_al.get("eod_summary_enabled", True)),
                "s1_reclaim_trap_alert_enabled": bool(oi_al.get("s1_reclaim_trap_alert_enabled", True)),
                "telegram_briefing_enabled": bool(oi_al.get("telegram_briefing_enabled", True)),
                "floor_weakening_consecutive_bars": int(oi_al.get("floor_weakening_consecutive_bars", 2)),
                "gex_lot_size": int(gex_cfg.get("lot_size", getattr(config, "LOT_SIZE", 65))),
                "gex_max_null_pct": float(gex_cfg.get("max_null_gamma_pct", 0.30)),
                "gex_neutral_pct": float(gex_cfg.get("neutral_pct", 0.10)),
                "gex_neutral_band_cr": float(gex_cfg.get("neutral_band_cr", 25.0)),
                "gex_extreme_distance_pts": float(gex_cfg.get("extreme_distance_pts", 200.0)),
                "expiry_pcr_swing_threshold": float(expiry_cfg.get("pcr_swing_threshold", 1.0)),
                "expiry_pcr_swing_cooldown": int(expiry_cfg.get("pcr_swing_cooldown_minutes", 15)),
                "expiry_unwind_time": str(expiry_cfg.get("unwind_cutoff_time", "14:00")),
                "expiry_max_pain_telegram_time": str(expiry_cfg.get("max_pain_telegram_time", "13:00")),
                "expiry_summary_time": str(expiry_cfg.get("expiry_summary_time", "13:15")),
                "iv_window_minutes": int(iv_cfg.get("window_minutes", 30)),
                "iv_cheap_percentile": float(iv_cfg.get("cheap_percentile", 35.0)),
                "iv_expensive_percentile": float(iv_cfg.get("expensive_percentile", 65.0)),
                "iv_min_sessions_required": int(iv_cfg.get("min_sessions_required", 3)),
                "iv_max_pct_jump_per_poll": float(iv_cfg.get("max_pct_jump_per_poll", 25.0)),
                "iv_carry_max_polls": int(iv_cfg.get("carry_max_polls", iv_cfg.get("iv_carry_max_polls", 5))),
                "iv_reference_mode": str(iv_cfg.get("reference_mode", "interpolate")),
                "chk_box1_min_wall_contracts": float(chk_cfg.get("box1_min_wall_contracts", 50000.0)),
                "chk_box2_surge_limit_contracts": float(chk_cfg.get("box2_surge_limit_contracts", 5000.0)),
                "chk_box3_expensive_percentile": float(chk_cfg.get("box3_expensive_percentile", 65.0)),
                "chk_box4_gex_threshold_cr": float(chk_cfg.get("box4_gex_threshold_cr", 10.0)),
                "chk_box5_market_open_time": str(chk_cfg.get("box5_market_open_time", "09:20")),
                "chk_box5_freeze_start": str(chk_cfg.get("box5_freeze_start", "11:30")),
                "chk_box5_freeze_end": str(chk_cfg.get("box5_freeze_end", "13:15")),
                "chk_box5_freeze_grace_end": str(chk_cfg.get("box5_freeze_grace_end", "13:30")),
                "chk_box5_late_cutoff_time": str(chk_cfg.get("box5_late_cutoff_time", "14:45")),
                "chk_box5_fresh_data_minutes": float(chk_cfg.get("box5_fresh_data_minutes", 5.0)),
                "observations_enabled": bool(obs_cfg.get("enabled", True)),
                "observations_dir": str(obs_cfg.get("dir", "data/observations")),
                "observations_market_open_time": str(obs_cfg.get("market_open_time", "09:15")),
                "observations_freeze_end_time": str(obs_cfg.get("freeze_end_time", "13:15")),
                "observations_eod_summary_time": str(obs_cfg.get("eod_summary_time", "15:25")),
                "max_gap_seconds": float(data_gap.get("max_gap_seconds", data.get("max_gap_seconds", 180.0))),
                "data_gap": data_gap,
                "mj_mode": data.get("mj_mode", {
                    "enabled": True,
                    "shadow_logging": True,
                    "lot_size_mj": 1,
                    "target_pts": 25,
                    "no_trade_after": "14:00",
                }),
                "writer_dominance": {
                    "enabled": bool(dom_cfg.get("enabled", True)),
                    "atm_steps": int(dom_cfg.get("dominance_strike_span", dom_cfg.get("atm_steps", 2))),
                    "dominance_strike_span": int(dom_cfg.get("dominance_strike_span", dom_cfg.get("atm_steps", 2))),
                    "bands": dom_cfg.get("bands", {
                        "balanced_max": 1.3,
                        "mild_max": 2.0,
                        "clear_max": 3.0,
                    }),
                    "labels": dom_cfg.get("labels", {
                        "balanced": "BALANCED",
                        "mild": "MILD",
                        "clear": "CLEAR",
                        "heavy": "HEAVY",
                    }),
                    "structure": dom_cfg.get("structure", {
                        "range_min_width_pts": 100,
                        "compression_max_width_pts": 50,
                        "trending_threshold": 2.0,
                    }),
                    "asymmetry": dom_cfg.get("asymmetry", {
                        "min_gap": 2.0,
                        "conviction_bands": {
                            "weak_max": 1.5,
                            "firm_max": 3.0,
                        },
                        "conviction_labels": {
                            "weak": "WEAK",
                            "firm": "FIRM",
                            "strong": "STRONG",
                        },
                    }),
                    "risk_staging": dom_cfg.get("risk_staging", {
                        "cascade_delta_threshold": -25000,
                        "proximity_pts": 25.0,
                    }),
                    "telegram_line_enabled": bool(dom_cfg.get("telegram_line_enabled", True)),
                },
            }
    except Exception as err:
        logger.warning(f"Could not load rules from {RULES_PATH}: {err}")
        return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 2. Briefing Formatter & Engine
# ═══════════════════════════════════════════════════════════════════════════

def parse_expiry_date(expiry_val: Any) -> Optional[date]:
    """Parses various date and string formats into a date object."""
    if not expiry_val:
        return None
    if isinstance(expiry_val, date):
        return expiry_val
    if isinstance(expiry_val, datetime):
        return expiry_val.date()
    val_str = str(expiry_val).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    return None


def compute_max_pain(df: pd.DataFrame) -> Optional[float]:
    """
    Computes Max Pain strike where total option writer payout is minimised.
    Formula:
      call_pain(K) = sum(max(0, K - S) * ce_oi(S) for S <= K)
      put_pain(K)  = sum(max(0, S - K) * pe_oi(S) for S >= K)
      total_pain(K) = call_pain(K) + put_pain(K)
      Max pain = strike K where total_pain(K) is minimum.
    """
    if df is None or getattr(df, "empty", True) or "strike" not in df.columns:
        return None
    try:
        strikes = sorted(df["strike"].dropna().unique())
        if not strikes:
            return None
        ce_map = df.set_index("strike")["ce_oi"].fillna(0.0).to_dict()
        pe_map = df.set_index("strike")["pe_oi"].fillna(0.0).to_dict()

        min_pain = float("inf")
        best_strike = None
        for K in strikes:
            call_pain = sum((K - S) * float(ce_map.get(S, 0.0)) for S in strikes if S <= K)
            put_pain = sum((S - K) * float(pe_map.get(S, 0.0)) for S in strikes if S >= K)
            tot = call_pain + put_pain
            if tot < min_pain:
                min_pain = tot
                best_strike = K
        return float(best_strike) if best_strike is not None else None
    except Exception as exc:
        logger.warning(f"Max pain computation error: {exc}")
        return None


def compute_institutional_walls(
    df: pd.DataFrame,
    spot: float,
    cap_pts: float = 500.0,
) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    """
    Computes Max Institutional Support (PE) and Resistance (CE) within spot ± cap_pts (default 500 pts).
    Returns (max_put_row, max_call_row).
    """
    if df is None or getattr(df, "empty", True) or "strike" not in df.columns:
        return None, None

    df_cap = df[df["strike"].between(spot - cap_pts, spot + cap_pts)]
    if df_cap.empty:
        return None, None

    max_p_row = None
    puts_below = df_cap[df_cap["strike"] <= spot].dropna(subset=["pe_oi"])
    if not puts_below.empty and "pe_oi" in puts_below.columns:
        max_p_row = puts_below.loc[puts_below["pe_oi"].idxmax()]

    max_c_row = None
    calls_above = df_cap[df_cap["strike"] >= spot].dropna(subset=["ce_oi"])
    if not calls_above.empty and "ce_oi" in calls_above.columns:
        max_c_row = calls_above.loc[calls_above["ce_oi"].idxmax()]

    return max_p_row, max_c_row


def compute_writer_dominance(
    latest_df: pd.DataFrame,
    latest_spot: float,
    rules: dict,
    atm_override: Optional[float] = None,
    gex_data: Optional[dict] = None,
    s1_strike: Optional[float] = None,
    r1_strike: Optional[float] = None,
    is_data_gap: bool = False,
    gap_msg: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes Writer Dominance from ΔOI across ATM ± 2 strikes.
    Rule: use ΔOI for who's winning now, total OI for who's already committed.
    - Strikes ABOVE spot: CE ΔOI is the meaningful number (building ceiling)
    - Strikes BELOW spot: PE ΔOI is the meaningful number (building floor)
    - ATM strike: both matter; read the ratio

    Dominance ratio at single strike = larger ΔOI ÷ smaller ΔOI
    Bands:
      1.0-1.3: Balanced
      1.3-2.0: Mild dominance
      2.0-3.0: Clear dominance
      3.0+:    Heavy dominance

    Structure (three-way / four-state classifier):
      - RANGE: both sides ΔOI positive, gap >= range_min_width_pts (default 100)
      - COMPRESSION: both sides ΔOI positive at same strike or gap <= compression_max_width_pts (default 50)
      - ABANDONMENT: both sides ΔOI negative across span
      - TRENDING: one side dominant >= trending_threshold (default 2.0x), opposite edge not defended

    Asymmetry:
      - Floor conviction vs Ceiling conviction
      - Bands: < 1.5 WEAK, 1.5-3.0 FIRM, 3.0+ STRONG
      - If edge ratio differs by >= asymmetry_min_gap (default 2.0x), emit asymmetric narrative.

    Output lines:
      ⚖️ DOMINANCE: CALLS 3.2× HEAVY (CE +399k vs PE +126k)
         Heaviest: 23,250 CE 7.8× | 23,200 CE 2.9×
      🔒 STRUCTURE: RANGE 23,050–23,250 (edges defended both sides)
         Floor conviction 1.3× WEAK | Ceiling 7.8× STRONG
         → Asymmetric: lid firm, floor thin. Downside drift likelier than an upside break.
         → Ceiling being built 29-79 pts above spot
         → Risk to call writers: close above 23,250 = squeeze
    """
    dom_cfg = rules.get("writer_dominance", {})
    if not dom_cfg.get("enabled", True):
        return {
            "enabled": False,
            "terminal_lines": [],
            "telegram_line": None,
            "telegram_struct_line": None,
            "obs_dominance": None,
            "obs_structure": None,
            "agg_side": "NONE",
            "agg_ratio": 1.0,
            "agg_label": "DISABLED",
            "intensity_label": "DISABLED",
            "structure_state": "NONE",
            "structure_line": None,
            "asymmetry": {},
            "strikes": [],
            "analyzed_strikes": [],
            "top_heaviest": [],
        }

    if is_data_gap:
        return {
            "enabled": True,
            "is_gap": True,
            "terminal_lines": [
                "  ⚖️ DOMINANCE: INVALID (data gap — flow unverified)",
                "  🔒 STRUCTURE: INVALID (data gap)",
            ],
            "telegram_line": "⚖️ Dominance: INVALID (data gap)",
            "telegram_struct_line": None,
            "obs_dominance": "INVALID (data gap)",
            "obs_structure": "INVALID (data gap)",
            "agg_side": "NONE",
            "agg_ratio": None,
            "agg_label": "INVALID",
            "intensity_label": "INVALID",
            "structure_state": "INVALID",
            "structure_line": "🔒 STRUCTURE: INVALID (data gap)",
            "asymmetry": {},
            "strikes": [],
            "analyzed_strikes": [],
            "top_heaviest": [],
        }

    if latest_df is None or getattr(latest_df, "empty", True) or "strike" not in latest_df.columns:
        return {
            "enabled": True,
            "terminal_lines": [
                "  ⚖️ DOMINANCE: N/A (no option chain data)",
            ],
            "telegram_line": "⚖️ Dominance: N/A",
            "telegram_struct_line": None,
            "obs_dominance": "N/A",
            "obs_structure": "N/A",
            "agg_side": "NONE",
            "agg_ratio": 1.0,
            "agg_label": "N/A",
            "intensity_label": "N/A",
            "structure_state": "N/A",
            "structure_line": "🔒 STRUCTURE: N/A",
            "asymmetry": {},
            "strikes": [],
            "analyzed_strikes": [],
            "top_heaviest": [],
        }

    step = float(rules.get("step", getattr(config, "STRIKE_STEP", 50.0)))
    if atm_override is not None:
        atm = float(atm_override)
    else:
        atm = float(round(latest_spot / step) * step)
        avail_strikes = sorted(latest_df["strike"].unique())
        if atm not in avail_strikes and avail_strikes:
            atm = float(min(avail_strikes, key=lambda s: abs(s - latest_spot)))

    atm_steps = int(dom_cfg.get("dominance_strike_span", dom_cfg.get("atm_steps", 2)))
    bands = dom_cfg.get("bands", {
        "balanced_max": 1.3,
        "mild_max": 2.0,
        "clear_max": 3.0,
    })
    b_bal = float(bands.get("balanced_max", 1.3))
    b_mild = float(bands.get("mild_max", 2.0))
    b_clear = float(bands.get("clear_max", 3.0))

    labels = dom_cfg.get("labels", {
        "balanced": "BALANCED",
        "mild": "MILD",
        "clear": "CLEAR",
        "heavy": "HEAVY",
    })

    struct_cfg = dom_cfg.get("structure", {})
    range_min_width_pts = float(struct_cfg.get("range_min_width_pts", 100))
    comp_max_width_pts = float(struct_cfg.get("compression_max_width_pts", 50))
    trending_threshold = float(struct_cfg.get("trending_threshold", 2.0))

    asym_cfg = dom_cfg.get("asymmetry", {})
    asym_min_gap = float(asym_cfg.get("min_gap", 2.0))
    conv_bands = asym_cfg.get("conviction_bands", {"weak_max": 1.5, "firm_max": 3.0})
    weak_max = float(conv_bands.get("weak_max", 1.5))
    firm_max = float(conv_bands.get("firm_max", 3.0))
    conv_labels = asym_cfg.get("conviction_labels", {"weak": "WEAK", "firm": "FIRM", "strong": "STRONG"})

    risk_cfg = dom_cfg.get("risk_staging", {})
    cascade_delta_threshold = float(risk_cfg.get("cascade_delta_threshold", -25000))
    proximity_pts = float(risk_cfg.get("proximity_pts", 25.0))

    chain_strikes = sorted([atm + i * step for i in range(-atm_steps, atm_steps + 1)], reverse=True)

    analyzed_strikes = []
    for s in chain_strikes:
        s_row = latest_df[latest_df["strike"] == s]
        if not s_row.empty:
            sr = s_row.iloc[0]
            c_d = float(sr.get("ce_chg_oi", 0) or 0.0)
            p_d = float(sr.get("pe_chg_oi", 0) or 0.0)
            c_oi = float(sr.get("ce_oi", 0) or 0.0)
            p_oi = float(sr.get("pe_oi", 0) or 0.0)
        else:
            c_d = 0.0
            p_d = 0.0
            c_oi = 0.0
            p_oi = 0.0

        larger = max(c_d, p_d)
        smaller = min(c_d, p_d)

        if c_d > p_d:
            dominant_side = "CE"
            dominant_name = "Calls"
        elif p_d > c_d:
            dominant_side = "PE"
            dominant_name = "Puts"
        else:
            dominant_side = "BALANCED"
            dominant_name = "Balanced"

        if larger <= 0 and smaller <= 0:
            ratio = 1.0
        elif smaller <= 0 and larger > 0:
            denom = abs(smaller) if abs(smaller) > 0 else 1.0
            ratio = larger / denom
        else:
            ratio = larger / smaller if smaller > 0 else 99.9

        ratio = min(ratio, 99.9)

        if ratio <= b_bal:
            verdict = labels.get("balanced", "Balanced").title()
        elif ratio <= b_mild:
            verdict = f"{dominant_name} {labels.get('mild', 'mild').lower()}"
        elif ratio <= b_clear:
            verdict = f"{dominant_name} {labels.get('clear', 'clear').lower()}"
        else:
            verdict = f"{dominant_name} {labels.get('heavy', 'heavy').lower()}"

        dist_from_spot = s - latest_spot
        if abs(dist_from_spot) < (step / 2.0) or s == atm:
            pos_label = "ATM"
        elif dist_from_spot > 0:
            pos_label = f"+{int(round(dist_from_spot))} above"
        else:
            pos_label = f"−{int(round(abs(dist_from_spot)))} below"

        if s > latest_spot + (step / 2.0):
            meaningful_side = "CE"
        elif s < latest_spot - (step / 2.0):
            meaningful_side = "PE"
        else:
            meaningful_side = "BOTH"

        analyzed_strikes.append({
            "strike": s,
            "ce_d": c_d,
            "pe_d": p_d,
            "ce_oi": c_oi,
            "pe_oi": p_oi,
            "dominant_side": dominant_side,
            "dominant_name": dominant_name,
            "ratio": ratio,
            "verdict": verdict,
            "pos_label": pos_label,
            "meaningful_side": meaningful_side,
            "dist_from_spot": dist_from_spot,
        })

    ce_sum = sum(max(0.0, float(r["ce_d"])) for r in analyzed_strikes)
    pe_sum = sum(max(0.0, float(r["pe_d"])) for r in analyzed_strikes)

    if ce_sum >= pe_sum:
        agg_side = "CALLS"
        agg_ratio = (ce_sum / pe_sum) if pe_sum > 0 else (99.9 if ce_sum > 0 else 1.0)
    else:
        agg_side = "PUTS"
        agg_ratio = (pe_sum / ce_sum) if ce_sum > 0 else (99.9 if pe_sum > 0 else 1.0)

    agg_ratio = min(agg_ratio, 99.9)

    if agg_ratio <= b_bal:
        intensity_label = labels.get("balanced", "BALANCED").upper()
        agg_label = labels.get("balanced", "BALANCED").upper()
    elif agg_ratio <= b_mild:
        intensity_label = labels.get("mild", "MILD").upper()
        agg_label = agg_side
    elif agg_ratio <= b_clear:
        intensity_label = labels.get("clear", "CLEAR").upper()
        agg_label = agg_side
    else:
        intensity_label = labels.get("heavy", "HEAVY").upper()
        agg_label = agg_side

    def fmt_k(val: float) -> str:
        sgn = "+" if val >= 0 else "-"
        return f"{sgn}{abs(val)/1000:,.0f}k" if abs(val) >= 1000 else f"{sgn}{abs(val):,.0f}"

    if agg_side == "PUTS" and intensity_label != labels.get("balanced", "BALANCED").upper():
        counts_str = f"PE {fmt_k(pe_sum)} vs CE {fmt_k(ce_sum)}"
    else:
        counts_str = f"CE {fmt_k(ce_sum)} vs PE {fmt_k(pe_sum)}"

    if intensity_label == labels.get("balanced", "BALANCED").upper():
        dom_header = f"BALANCED {agg_ratio:.1f}× ({counts_str})"
    else:
        dom_header = f"{agg_side} {agg_ratio:.1f}× {intensity_label} ({counts_str})"

    line1 = f"  ⚖️ DOMINANCE: {dom_header}"

    if agg_side == "CALLS":
        cand = [r for r in analyzed_strikes if r["dominant_side"] == "CE" and r["ce_d"] > 0]
        cand.sort(key=lambda x: (x["strike"] >= latest_spot - (step / 2.0), x["ratio"]), reverse=True)
    elif agg_side == "PUTS":
        cand = [r for r in analyzed_strikes if r["dominant_side"] == "PE" and r["pe_d"] > 0]
        cand.sort(key=lambda x: (x["strike"] <= latest_spot + (step / 2.0), x["ratio"]), reverse=True)
    else:
        cand = sorted(analyzed_strikes, key=lambda x: x["ratio"], reverse=True)

    if not cand:
        cand = sorted(analyzed_strikes, key=lambda x: x["ratio"], reverse=True)

    top_heaviest = cand[:2]
    heaviest_parts = [
        f"{int(round(r['strike'])):,} {r['dominant_side']} {r['ratio']:.1f}×"
        for r in top_heaviest
    ]
    line2 = f"     Heaviest: {' | '.join(heaviest_parts)}" if heaviest_parts else "     Heaviest: N/A"

    # ── PART 2: Structure Classifier ──────────────────────────────────────────
    all_neg = all(r["ce_d"] <= 0 and r["pe_d"] <= 0 for r in analyzed_strikes)

    structure_state = "NONE"
    structure_line = ""
    structure_sublines: List[str] = []
    floor_strike = None
    ceil_strike = None
    asym_narrative_short = ""

    if all_neg or (ce_sum <= 0 and pe_sum <= 0):
        structure_state = "ABANDONMENT"
        structure_line = "  🔒 STRUCTURE: ABANDONMENT (unwinding across span, stand aside)"
        structure_sublines.append("     → No levels to trade against, stand aside")
        structure_summary = "ABANDONMENT (stand aside)"
    else:
        ce_above = [r for r in analyzed_strikes if r["strike"] >= (latest_spot - step / 2.0) and r["ce_d"] > 0]
        pe_below = [r for r in analyzed_strikes if r["strike"] <= (latest_spot + step / 2.0) and r["pe_d"] > 0]

        has_ce_above = len(ce_above) > 0
        has_pe_below = len(pe_below) > 0

        if has_ce_above and has_pe_below:
            c_edge = max(r["strike"] for r in ce_above)
            p_edge = min(r["strike"] for r in pe_below)
            gap = c_edge - p_edge

            if gap <= comp_max_width_pts:
                structure_state = "COMPRESSION"
                pin_strike = int(round(c_edge))
                structure_line = f"  🔒 STRUCTURE: COMPRESSION {pin_strike:,} (writers pinning one level)"
                structure_sublines.append("     → Breakout pending, direction unknown, no position until expansion")
                structure_summary = f"COMPRESSION {pin_strike:,}"
            else:
                structure_state = "RANGE"
                floor_strike = p_edge
                ceil_strike = c_edge
                structure_line = f"  🔒 STRUCTURE: RANGE {int(round(floor_strike)):,}–{int(round(ceil_strike)):,} (edges defended both sides)"
                structure_summary = f"RANGE {int(round(floor_strike)):,}–{int(round(ceil_strike)):,}"

        elif agg_ratio >= trending_threshold:
            structure_state = "TRENDING"
            if agg_side == "CALLS":
                structure_line = f"  🔒 STRUCTURE: TRENDING (calls dominant {agg_ratio:.1f}×, floor not defended)"
                structure_sublines.append("     → Directional flow dominant, not a range; trade with flow")
                structure_summary = f"TRENDING (calls {agg_ratio:.1f}×)"
            else:
                structure_line = f"  🔒 STRUCTURE: TRENDING (puts dominant {agg_ratio:.1f}×, ceiling not defended)"
                structure_sublines.append("     → Directional flow dominant, not a range; trade with flow")
                structure_summary = f"TRENDING (puts {agg_ratio:.1f}×)"
        else:
            active_strikes = [r["strike"] for r in analyzed_strikes if r["ce_d"] > 0 or r["pe_d"] > 0]
            if len(active_strikes) <= 1:
                pin_s = int(round(active_strikes[0])) if active_strikes else int(round(atm))
                structure_state = "COMPRESSION"
                structure_line = f"  🔒 STRUCTURE: COMPRESSION {pin_s:,} (writers pinning one level)"
                structure_sublines.append("     → Breakout pending, direction unknown, no position until expansion")
                structure_summary = f"COMPRESSION {pin_s:,}"
            else:
                min_s = min(active_strikes)
                max_s = max(active_strikes)
                if (max_s - min_s) >= range_min_width_pts:
                    structure_state = "RANGE"
                    floor_strike = min_s
                    ceil_strike = max_s
                    structure_line = f"  🔒 STRUCTURE: RANGE {int(round(floor_strike)):,}–{int(round(ceil_strike)):,} (edges defended both sides)"
                    structure_summary = f"RANGE {int(round(floor_strike)):,}–{int(round(ceil_strike)):,}"
                else:
                    structure_state = "COMPRESSION"
                    structure_line = f"  🔒 STRUCTURE: COMPRESSION {int(round(atm)):,} (writers pinning one level)"
                    structure_sublines.append("     → Breakout pending, direction unknown, no position until expansion")
                    structure_summary = f"COMPRESSION {int(round(atm)):,}"

    # ── PART 3: Asymmetry ─────────────────────────────────────────────────────
    asymmetry_data = {
        "floor_strike": floor_strike,
        "ceil_strike": ceil_strike,
        "floor_conviction": 1.0,
        "ceil_conviction": 1.0,
        "floor_band": "BALANCED",
        "ceil_band": "BALANCED",
        "conv_gap": 1.0,
        "is_asymmetric": False,
        "narrative": None,
    }

    if structure_state == "RANGE" and floor_strike is not None and ceil_strike is not None:
        p_row = next((r for r in analyzed_strikes if r["strike"] == floor_strike), None)
        c_row = next((r for r in analyzed_strikes if r["strike"] == ceil_strike), None)

        p_conv = float(p_row["ratio"]) if p_row else 1.0
        c_conv = float(c_row["ratio"]) if c_row else 1.0

        p_band = conv_labels.get("weak", "WEAK") if p_conv < weak_max else (conv_labels.get("firm", "FIRM") if p_conv <= firm_max else conv_labels.get("strong", "STRONG"))
        c_band = conv_labels.get("weak", "WEAK") if c_conv < weak_max else (conv_labels.get("firm", "FIRM") if c_conv <= firm_max else conv_labels.get("strong", "STRONG"))

        asym_conv_line = f"     Floor conviction {p_conv:.1f}× {p_band} | Ceiling {c_conv:.1f}× {c_band}"
        structure_sublines.append(asym_conv_line)

        larger_c = max(p_conv, c_conv)
        smaller_c = min(p_conv, c_conv)
        conv_gap = (larger_c / smaller_c) if smaller_c > 0 else 99.9

        is_asym = (conv_gap >= asym_min_gap or abs(c_conv - p_conv) >= asym_min_gap)
        if is_asym:
            if c_conv > p_conv:
                asym_narrative = "     → Asymmetric: lid firm, floor thin. Downside drift likelier than an upside break."
                asym_narrative_short = "Asymmetric: lid firm, floor thin"
            else:
                asym_narrative = "     → Asymmetric: floor firm, lid thin. Upside breakout likelier than a downside break."
                asym_narrative_short = "Asymmetric: floor firm, lid thin"
        else:
            asym_narrative = "     → Symmetric range: balanced defense both sides."
            asym_narrative_short = "Symmetric range"

        structure_sublines.append(asym_narrative)

        asymmetry_data.update({
            "floor_conviction": p_conv,
            "ceil_conviction": c_conv,
            "floor_band": p_band,
            "ceil_band": c_band,
            "conv_gap": conv_gap,
            "is_asymmetric": is_asym,
            "narrative": asym_narrative.strip(),
        })

    # ── PART 4: Risk Lines (Ceiling & Floor Risk / Breakout Staging) ───────────
    risk_sublines: List[str] = []
    if agg_side == "CALLS" and intensity_label != labels.get("balanced", "BALANCED").upper():
        call_strikes = [r["strike"] for r in top_heaviest if r["strike"] >= latest_spot]
        if not call_strikes:
            call_strikes = [r["strike"] for r in top_heaviest]

        min_c = min(call_strikes) if call_strikes else (atm + step)
        max_c = max(call_strikes) if call_strikes else (atm + step)
        dist_min = int(round(min_c - latest_spot))
        dist_max = int(round(max_c - latest_spot))

        if dist_min != dist_max:
            dist_str = f"{dist_min}-{dist_max} pts above spot"
        elif dist_min >= 0:
            dist_str = f"{dist_min} pts above spot"
        else:
            dist_str = f"at ATM ({int(round(min_c)):,})"

        risk_sublines.append(f"     → Ceiling being built {dist_str}")
        risk_sublines.append(f"     → Risk to call writers: close above {int(round(max_c)):,} = squeeze")
        range_summary = f"Ceiling {dist_str}"

    elif agg_side == "PUTS" and intensity_label != labels.get("balanced", "BALANCED").upper():
        put_strikes = [r["strike"] for r in top_heaviest if r["strike"] <= latest_spot]
        if not put_strikes:
            put_strikes = [r["strike"] for r in top_heaviest]

        min_p = min(put_strikes) if put_strikes else (atm - step)
        max_p = max(put_strikes) if put_strikes else (atm - step)
        dist_min = int(round(latest_spot - max_p))
        dist_max = int(round(latest_spot - min_p))

        if dist_min != dist_max:
            dist_str = f"{dist_min}-{dist_max} pts below spot"
        elif dist_min >= 0:
            dist_str = f"{dist_min} pts below spot"
        else:
            dist_str = f"at ATM ({int(round(max_p)):,})"

        risk_sublines.append(f"     → Floor being built {dist_str}")
        risk_sublines.append(f"     → Risk to put writers: close below {int(round(min_p)):,} = cascade")
        range_summary = f"Floor {dist_str}"

    elif structure_state == "RANGE" and floor_strike is not None and ceil_strike is not None:
        risk_sublines.append(f"     → Both sides writing a range ({int(round(floor_strike)):,}–{int(round(ceil_strike)):,})")
        risk_sublines.append(f"     → Risk to range writers: breakout beyond {int(round(floor_strike)):,}–{int(round(ceil_strike)):,}")
        range_summary = f"Range {int(round(floor_strike)):,}–{int(round(ceil_strike)):,}"
    else:
        all_s = [r["strike"] for r in analyzed_strikes]
        min_all = int(round(min(all_s))) if all_s else int(round(latest_spot - step))
        max_all = int(round(max(all_s))) if all_s else int(round(latest_spot + step))
        risk_sublines.append(f"     → Both sides writing a range ({min_all:,}–{max_all:,})")
        risk_sublines.append(f"     → Risk to range writers: breakout beyond {min_all:,}–{max_all:,}")
        range_summary = f"Range {min_all:,}–{max_all:,}"

    terminal_lines = [line1, line2, structure_line]
    terminal_lines.extend(structure_sublines)
    terminal_lines.extend(risk_sublines)

    telegram_line = f"⚖️ Dominance: {dom_header}"
    telegram_struct_line = f"🔒 Structure: {structure_summary}"
    if asym_narrative_short:
        telegram_struct_line += f" ({asym_narrative_short})"

    obs_dominance = dom_header
    obs_structure = structure_line.replace("  🔒 STRUCTURE: ", "").strip()

    return {
        "enabled": True,
        "agg_side": agg_side,
        "agg_ratio": agg_ratio,
        "agg_label": agg_label,
        "intensity_label": intensity_label,
        "ce_sum": ce_sum,
        "pe_sum": pe_sum,
        "terminal_lines": terminal_lines,
        "telegram_line": telegram_line,
        "telegram_struct_line": telegram_struct_line,
        "obs_dominance": obs_dominance,
        "obs_structure": obs_structure,
        "structure_state": structure_state,
        "structure_line": structure_line.strip(),
        "floor_strike": floor_strike,
        "ceil_strike": ceil_strike,
        "asymmetry": asymmetry_data,
        "top_heaviest": top_heaviest,
        "analyzed_strikes": analyzed_strikes,
        "range_summary": range_summary,
    }


def detect_wall_migrations(
    today_df: pd.DataFrame,
    yesterday_df: Optional[pd.DataFrame],
    spot: float,
    rules: dict,
) -> List[str]:
    """
    Detects strikes within ±proximity_pts of spot where writer dominance flipped between sessions.
    CE-dominant yesterday -> PE-dominant today: Conquered by bulls.
    PE-dominant yesterday -> CE-dominant today: Conquered by bears.
    """
    if today_df is None or getattr(today_df, "empty", True) or yesterday_df is None or getattr(yesterday_df, "empty", True) or spot <= 0:
        return []

    cfg = rules.get("wall_migration", {})
    if not cfg.get("enabled", True):
        return []

    prox = float(cfg.get("proximity_pts", 200.0))
    ratio_thresh = float(cfg.get("ratio_threshold", 1.2))

    t_strikes = today_df[(today_df["strike"] >= spot - prox) & (today_df["strike"] <= spot + prox)]
    messages = []

    for _, row_t in t_strikes.iterrows():
        stk = float(row_t["strike"])
        row_y = yesterday_df[yesterday_df["strike"] == stk]
        if row_y.empty:
            continue

        c_t = float(row_t.get("ce_oi", 0) or 0)
        p_t = float(row_t.get("pe_oi", 0) or 0)
        c_y = float(row_y.iloc[0].get("ce_oi", 0) or 0)
        p_y = float(row_y.iloc[0].get("pe_oi", 0) or 0)

        if max(c_t, p_t, c_y, p_y) < 10000:
            continue

        was_ce_dom = (c_y >= p_y * ratio_thresh) and (c_y > 0)
        is_pe_dom = (p_t >= c_t * ratio_thresh) and (p_t > 0)

        was_pe_dom = (p_y >= c_y * ratio_thresh) and (p_y > 0)
        is_ce_dom = (c_t >= p_t * ratio_thresh) and (c_t > 0)

        stk_str = f"{int(round(stk)):,}"
        if was_ce_dom and is_pe_dom:
            msg = f"{stk_str} conquered by bulls — Call writers surrendered, Put writers established base"
            messages.append(msg)
            logger.info(f"wall_migration: {msg}")
        elif was_pe_dom and is_ce_dom:
            msg = f"{stk_str} conquered by bears — Put writers surrendered, Call writers established lid"
            messages.append(msg)
            logger.info(f"wall_migration: {msg}")

    return messages


def compute_gex_metrics(
    df: pd.DataFrame,
    spot: float,
    rules: dict,
) -> dict:
    """
    Computes Gamma Exposure (GEX) proxy across all strikes.
    Formula:
      gex(K) = (ce_oi(K) - pe_oi(K)) * gamma(K) * spot * lot_size
      TOTAL GEX = sum(gex(K)) across all strikes

    Empirical Volatility Regime Proxy (No Directional Alpha, p=0.68/0.74):
      GEX is strictly a volatility regime filter, NOT a directional predictor.
      In NSE India, participant positioning differs from US dealer mechanics
      (retail writes options, prop desks run long-vol books). The CE-PE proxy
      is treated strictly as an empirical volatility correlation:
        TOTAL GEX > 0: SHORT gamma proxy -> Volatility expansion / trending regime (moves amplify)
        TOTAL GEX < 0: LONG gamma proxy -> Volatility dampening / rangebound regime (walls hold)
        TOTAL GEX within +-neutral_band_cr (default +-25 Cr) -> NEUTRAL gamma (no regime active)
    """
    default_res = {
        "status": "na",
        "total_gex": 0.0,
        "total_gex_cr": 0.0,
        "regime": "N/A",
        "terminal_str": "N/A",
        "telegram_str": None,
        "zero_gamma": None,
        "zero_gamma_str": None,
        "top5_strikes": [],
    }

    if df is None or getattr(df, "empty", True) or spot <= 0:
        return default_res

    # Check gamma presence
    if "ce_gamma" not in df.columns:
        return {**default_res, "status": "insufficient_data", "terminal_str": "insufficient data"}

    total_strikes = len(df)
    if total_strikes == 0:
        return default_res

    null_count = int(df["ce_gamma"].isna().sum())
    max_null_pct = float(rules.get("gex_max_null_pct", 0.30))
    if (null_count / total_strikes) > max_null_pct:
        return {**default_res, "status": "insufficient_data", "terminal_str": "insufficient data"}

    try:
        lot_size = int(rules.get("gex_lot_size", rules.get("display_lot_size", 65)))
        neutral_pct = float(rules.get("gex_neutral_pct", 0.10))

        df_sorted = df.sort_values("strike").copy()
        ce_oi = pd.to_numeric(df_sorted["ce_oi"], errors="coerce").fillna(0.0)
        pe_oi = pd.to_numeric(df_sorted["pe_oi"], errors="coerce").fillna(0.0)
        gamma = pd.to_numeric(df_sorted["ce_gamma"], errors="coerce").fillna(0.0)

        strike_gex = (ce_oi - pe_oi) * gamma * spot * lot_size
        df_sorted["gex"] = strike_gex

        total_gex = float(strike_gex.sum())
        total_gex_cr = total_gex / 1e7

        max_abs_strike = float(strike_gex.abs().max()) if not strike_gex.empty else 0.0
        neutral_bound = neutral_pct * max_abs_strike

        neutral_band_cr = float(rules.get("gex_neutral_band_cr", 25.0))
        abs_gex_cr = abs(total_gex_cr)
        sign_char = "+" if total_gex_cr >= 0 else "-"

        if abs_gex_cr <= neutral_band_cr or abs(total_gex) <= neutral_bound:
            regime = "NEUTRAL"
            terminal_str = f"{sign_char}₹{abs_gex_cr:.1f}Cr (NEUTRAL gamma — no regime)"
            telegram_str = f"🎯 GEX: NEUTRAL ({sign_char}₹{abs_gex_cr:.0f}Cr) — no regime"
        elif total_gex > 0:
            regime = "SHORT"
            terminal_str = f"+₹{total_gex_cr:.1f}Cr (SHORT gamma — trending day, breakouts follow through)"
            telegram_str = "🎯 GEX: SHORT → big moves likely today"
        else:
            regime = "LONG"
            terminal_str = f"-₹{abs_gex_cr:.1f}Cr (LONG gamma — rangebound day, walls reliable)"
            telegram_str = "🎯 GEX: LONG → walls hold, range trading"

        # Zero Gamma Level:
        # Find the strike where cumulative sum changes sign
        cum = strike_gex.cumsum().values
        strikes = df_sorted["strike"].values
        crossings = []
        for i in range(len(strikes) - 1):
            c1, c2 = cum[i], cum[i + 1]
            if (c1 <= 0 and c2 > 0) or (c1 >= 0 and c2 < 0):
                chosen = strikes[i] if abs(c1) < abs(c2) else strikes[i + 1]
                crossings.append(chosen)

        if crossings:
            zero_gamma = float(min(crossings, key=lambda s: abs(s - spot)))
        else:
            # Fallback to strike where strike-level GEX crosses zero closest to spot
            g_crossings = []
            g_vals = strike_gex.values
            for i in range(len(strikes) - 1):
                g1, g2 = g_vals[i], g_vals[i + 1]
                if (g1 <= 0 and g2 > 0) or (g1 >= 0 and g2 < 0):
                    chosen = strikes[i] if abs(g1) < abs(g2) else strikes[i + 1]
                    g_crossings.append(chosen)
            zero_gamma = float(min(g_crossings, key=lambda s: abs(s - spot))) if g_crossings else None

        if zero_gamma is not None:
            dist = abs(int(round(zero_gamma - spot)))
            extreme_dist = float(rules.get("gex_extreme_distance_pts", 200.0))
            if dist > extreme_dist:
                side = "above" if zero_gamma > spot else "below"
                if regime == "SHORT":
                    reg_note = "entire session in SHORT gamma zone — all moves amplified"
                elif regime == "LONG":
                    reg_note = "entire session in LONG gamma zone — all moves dampened"
                else:
                    reg_note = "entire session in NEUTRAL gamma zone — no regime"
                zg_note = f"{dist} pts {side} — {reg_note}"
            else:
                if spot >= zero_gamma:
                    zg_note = "spot is ABOVE → dealers buy dips"
                else:
                    zg_note = "spot is BELOW → dealers sell rallies"
            zero_gamma_str = f"{int(round(zero_gamma)):,} ({zg_note})"
        else:
            zero_gamma_str = "N/A"

        # Top 5 strikes by absolute GEX contribution
        top5_df = df_sorted.reindex(df_sorted["gex"].abs().sort_values(ascending=False).index).head(5)
        top5_strikes = []
        for _, r in top5_df.iterrows():
            top5_strikes.append({
                "strike": float(r["strike"]),
                "ce_oi": float(r.get("ce_oi", 0) or 0),
                "pe_oi": float(r.get("pe_oi", 0) or 0),
                "gamma": float(r.get("ce_gamma", 0) or 0),
                "gex_cr": float(r["gex"]) / 1e7,
            })

        return {
            "status": "ok",
            "total_gex": total_gex,
            "total_gex_cr": total_gex_cr,
            "regime": regime,
            "terminal_str": terminal_str,
            "telegram_str": telegram_str,
            "zero_gamma": zero_gamma,
            "zero_gamma_str": zero_gamma_str,
            "top5_strikes": top5_strikes,
        }
    except Exception as exc:
        logger.warning(f"GEX computation error: {exc}", exc_info=True)
        return {**default_res, "terminal_str": "N/A"}


# Module-level session state tracker for consecutive-poll IV percentile: {date: (datetime, percentile, consecutive_carries)}
_SESSION_IV_STATE: Dict[date, Tuple[Any, ...]] = {}


def reset_session_iv_state() -> None:
    """Resets the module-level session IV percentile tracker."""
    global _SESSION_IV_STATE
    _SESSION_IV_STATE.clear()


def extract_reference_atm_iv(
    df: pd.DataFrame,
    spot: float,
    mode: str = "interpolate",
    pinned_strike: Optional[float] = None,
) -> Optional[float]:
    """
    Extracts ATM IV ranked against the SAME reference across polls.
    - 'interpolate': Linearly interpolates IV at spot (moneyness 1.0) using straddling strikes.
                    At each strike, uses mean of ce_iv and pe_iv (or whichever is valid/positive)
                    to cancel forward-basis skew.
    - 'pinned': Uses pinned_strike if provided, else nearest strike to spot.
    """
    if df is None or getattr(df, "empty", True) or spot <= 0:
        return None

    if "strike" not in df.columns:
        return None

    has_ce = "ce_iv" in df.columns
    has_pe = "pe_iv" in df.columns
    if not has_ce and not has_pe:
        return None

    valid_rows = []
    for _, r in df.iterrows():
        k = float(r["strike"])
        v = []
        if has_ce:
            civ = float(r.get("ce_iv", 0) or 0)
            if civ > 0 and not np.isnan(civ):
                v.append(civ)
        if has_pe:
            piv = float(r.get("pe_iv", 0) or 0)
            if piv > 0 and not np.isnan(piv):
                v.append(piv)
        if v:
            valid_rows.append((k, float(np.mean(v))))

    if not valid_rows:
        return None

    valid_rows.sort(key=lambda x: x[0])
    strikes = [x[0] for x in valid_rows]
    ivs = [x[1] for x in valid_rows]

    if mode == "pinned" and pinned_strike is not None:
        idx = np.where(np.isclose(strikes, pinned_strike))[0]
        if len(idx) > 0:
            return float(ivs[idx[0]])
        nearest_idx = int(np.argmin([abs(k - pinned_strike) for k in strikes]))
        return float(ivs[nearest_idx])

    if mode != "interpolate":
        nearest_idx = int(np.argmin([abs(k - spot) for k in strikes]))
        return float(ivs[nearest_idx])

    # Interpolate to fixed moneyness (strike == spot)
    if spot <= strikes[0]:
        return float(ivs[0])
    if spot >= strikes[-1]:
        return float(ivs[-1])

    for i in range(len(strikes) - 1):
        k1, k2 = strikes[i], strikes[i + 1]
        if k1 <= spot <= k2:
            if k2 == k1:
                return float(ivs[i])
            w = (spot - k1) / (k2 - k1)
            return float((1.0 - w) * ivs[i] + w * ivs[i + 1])

    return float(ivs[0])


_HIST_IV_CACHE: Dict[Path, Optional[Tuple[str, float]]] = {}


def _extract_snap_atm_iv(item: Tuple[str, Path]) -> Optional[Tuple[str, float]]:
    """Extracts ATM strike IV from a single snapshot parquet file using reference extraction."""
    s_name, f = item
    if f in _HIST_IV_CACHE:
        return _HIST_IV_CACHE[f]
    try:
        schema = pq.read_schema(f)
        req_cols = [c for c in ["strike", "ce_iv", "pe_iv"] if c in schema.names]
        if "strike" not in req_cols:
            _HIST_IV_CACHE[f] = None
            return None
        table = pq.read_table(f, columns=req_cols)
        meta = table.schema.metadata
        spot = None
        if meta and b"PANDAS_ATTRS" in meta:
            attrs = json.loads(meta[b"PANDAS_ATTRS"].decode("utf-8"))
            spot = attrs.get("spot")
        if not spot:
            m_path = f.with_suffix(".meta.json")
            if m_path.exists():
                meta_data = json.loads(m_path.read_text())
                spot = meta_data.get("spot")
        if not spot:
            _HIST_IV_CACHE[f] = None
            return None

        df = table.to_pandas()
        iv = extract_reference_atm_iv(df, float(spot), mode="interpolate")
        if iv is not None and iv > 0:
            res = (s_name, float(iv))
        else:
            res = None
        _HIST_IV_CACHE[f] = res
        return res
    except Exception as exc:
        logger.debug(f"Error reading IV snapshot {f}: {exc}")
        _HIST_IV_CACHE[f] = None
        return None


def compute_iv_context(
    latest_df: pd.DataFrame,
    latest_spot: float,
    latest_ts: datetime,
    target_date: date,
    rules: dict,
    base_dir: Optional[Path] = None,
    prev_iv_percentile: Optional[float] = None,
) -> dict:
    """
    Computes ATM Implied Volatility context relative to historical baseline.
    Thresholds read from strategy1_rules.json under 'iv_context'.
    """
    default_res = {
        "status": "na",
        "curr_iv": None,
        "avg_iv": None,
        "percentile": None,
        "label": None,
        "terminal_str": "N/A",
        "telegram_str": None,
    }

    if latest_df is None or getattr(latest_df, "empty", True) or latest_spot <= 0:
        return default_res

    ref_mode = str(rules.get("iv_reference_mode", "interpolate"))
    curr_iv = extract_reference_atm_iv(latest_df, latest_spot, mode=ref_mode)

    if curr_iv is None or curr_iv <= 0:
        return default_res

    window_min = int(rules.get("iv_window_minutes", 30))
    dt_ref = datetime.combine(date.today(), latest_ts.time())
    t_start = (dt_ref - timedelta(minutes=window_min)).time()
    t_end = (dt_ref + timedelta(minutes=window_min)).time()
    t_min_str = t_start.strftime("%H%M%S")
    t_max_str = t_end.strftime("%H%M%S")
    target_date_str = target_date.strftime("%Y-%m-%d")

    snap_dir = base_dir or config.SNAPSHOT_DIR
    matching_files = []
    if snap_dir.exists():
        for d in sorted(snap_dir.iterdir()):
            if not d.is_dir() or d.name > target_date_str:
                continue
            for f in d.glob("*.parquet"):
                stem = f.stem
                if len(stem) >= 6:
                    if d.name == target_date_str and stem[:6] > latest_ts.strftime("%H%M%S"):
                        continue
                    if t_min_str <= stem[:6] <= t_max_str:
                        matching_files.append((d.name, f))

    min_sessions = int(rules.get("iv_min_sessions_required", 3))

    if not matching_files:
        return {
            "status": "building_history",
            "curr_iv": curr_iv,
            "avg_iv": None,
            "percentile": None,
            "label": None,
            "terminal_str": "building history",
            "telegram_str": "💰 IV: building history",
        }

    try:
        with ThreadPoolExecutor(max_workers=24) as ex:
            extracted = list(ex.map(_extract_snap_atm_iv, matching_files))
    except Exception as exc:
        logger.warning(f"Error extracting historical IV snapshots: {exc}")
        extracted = []

    session_dates = set()
    hist_ivs = []
    for res in extracted:
        if res is not None:
            session_dates.add(res[0])
            hist_ivs.append(res[1])

    if len(session_dates) < min_sessions or not hist_ivs:
        return {
            "status": "building_history",
            "curr_iv": curr_iv,
            "avg_iv": None,
            "percentile": None,
            "label": None,
            "terminal_str": "building history",
            "telegram_str": "💰 IV: building history",
        }

    avg_iv = float(np.mean(hist_ivs))
    less = sum(1 for x in hist_ivs if x < curr_iv)
    equal = sum(1 for x in hist_ivs if x == curr_iv)
    pct = (less + 0.5 * equal) / len(hist_ivs) * 100.0
    pct = float(np.clip(pct, 0.0, 100.0))

    # Sanity guard: if percentile jumps > iv_max_pct_jump_per_poll (default 25) between consecutive polls,
    # log warning and carry the previous value rather than flipping Box 3.
    # After iv_carry_max_polls consecutive carries (default 5), stop carrying and emit
    # "IV: unavailable (feed anomaly N polls)" with Box 3 set to HOLD rather than PASS or FAIL.
    max_jump = float(rules.get("iv_max_pct_jump_per_poll", 25.0))
    max_carries = int(rules.get("iv_carry_max_polls", rules.get("carry_max_polls", 5)))
    raw_pct = pct
    prior_pct = prev_iv_percentile
    last_carries = 0
    if target_date in _SESSION_IV_STATE:
        st = _SESSION_IV_STATE[target_date]
        last_ts = st[0]
        last_pct = st[1]
        last_carries = st[2] if len(st) > 2 else 0
        if prior_pct is None and 0 < (latest_ts - last_ts).total_seconds() <= 900:
            prior_pct = last_pct

    if prior_pct is not None:
        jump = abs(raw_pct - prior_pct)
        if jump > max_jump:
            carry_count = last_carries + 1
            if carry_count > max_carries:
                logger.warning(
                    f"IV feed anomaly: percentile jump {jump:.1f} exceeds max allowed {max_jump:.1f} "
                    f"between consecutive polls ({prior_pct:.1f} -> {raw_pct:.1f}) at {latest_ts.strftime('%H:%M:%S')}. "
                    f"Consecutive carries exceeded limit ({carry_count} > {max_carries}). "
                    f"Stopping carry. Emitting IV unavailable."
                )
                _SESSION_IV_STATE[target_date] = (latest_ts, prior_pct, carry_count)
                return {
                    "status": "feed_anomaly",
                    "curr_iv": curr_iv,
                    "avg_iv": avg_iv,
                    "percentile": None,
                    "label": "UNAVAILABLE",
                    "consecutive_carries": carry_count,
                    "terminal_str": f"IV: unavailable (feed anomaly {carry_count} polls)",
                    "telegram_str": f"💰 IV: unavailable (feed anomaly {carry_count} polls)",
                }
            else:
                logger.warning(
                    f"IV feed anomaly: percentile jump {jump:.1f} exceeds max allowed {max_jump:.1f} "
                    f"between consecutive polls ({prior_pct:.1f} -> {raw_pct:.1f}) at {latest_ts.strftime('%H:%M:%S')}. "
                    f"Carrying previous value {prior_pct:.1f} (carry {carry_count}/{max_carries})."
                )
                pct = prior_pct
                _SESSION_IV_STATE[target_date] = (latest_ts, pct, carry_count)
        else:
            _SESSION_IV_STATE[target_date] = (latest_ts, raw_pct, 0)
    else:
        _SESSION_IV_STATE[target_date] = (latest_ts, raw_pct, 0)

    pct_round = int(round(pct))
    cheap_thresh = float(rules.get("iv_cheap_percentile", 35.0))
    exp_thresh = float(rules.get("iv_expensive_percentile", 65.0))

    if pct < cheap_thresh:
        label = "CHEAP"
        tg_str = f"💰 IV: CHEAP ({pct_round}%) — good time to buy options"
    elif pct > exp_thresh:
        label = "EXPENSIVE"
        tg_str = f"💰 IV: EXPENSIVE ({pct_round}%) — premium costly, size down"
    else:
        label = "FAIR"
        tg_str = f"💰 IV: FAIR ({pct_round}%) — normal premium"

    reliable_base = int(rules.get("reliable_baseline", rules.get("iv_context", {}).get("reliable_baseline", 60)))
    base_caveat = ""
    n_sessions = len(session_dates)
    if n_sessions < reliable_base:
        base_caveat = f" ({n_sessions}-session baseline — tails unreliable below {reliable_base} sessions)"

    term_str = f"{curr_iv:.1f}%  |  Avg: {avg_iv:.1f}%  |  {label} (rank {pct_round}%){base_caveat}"
    return {
        "status": "ok",
        "curr_iv": curr_iv,
        "avg_iv": avg_iv,
        "percentile": pct,
        "label": label,
        "terminal_str": term_str,
        "telegram_str": tg_str,
    }


def _visual_len(s: str) -> int:
    """Calculates visual terminal column width of string including wide emojis."""
    w = 0
    for ch in s:
        if ch == "\ufe0f":
            continue
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("W", "F") or ord(ch) in (0x2705, 0x26A0, 0x274C, 0x1F4CA, 0x1F4B0):
            w += 2
        else:
            w += 1
    return w


def _truncate_visual(s: str, max_w: int) -> str:
    cur_w = 0
    res = []
    for ch in s:
        ch_w = 2 if (unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) in (0x2705, 0x26A0, 0x274C, 0x1F4CA, 0x1F4B0)) else 1
        if cur_w + ch_w > max_w:
            break
        cur_w += ch_w
        res.append(ch)
    return "".join(res)


def _pad_box_line(content: str, target_w: int = 40) -> str:
    """Pads a box row to exact visual width target_w enclosed in double borders."""
    vl = _visual_len(content)
    if vl > target_w:
        content = _truncate_visual(content, target_w - 3) + "..."
        vl = _visual_len(content)
    pad = max(0, target_w - vl)
    return "  ║" + content + (" " * pad) + "║"


def compute_pretrade_checklist(
    s1_row: Optional[pd.Series],
    r1_row: Optional[pd.Series],
    d_pe_15m: float,
    d_ce_15m: float,
    iv_percentile: Optional[float],
    gex_data: dict,
    latest_ts: datetime,
    target_date: date,
    target_time: Optional[dtime],
    rules: dict,
    has_history: bool = True,
    l8_report: Optional[Any] = None,
    l8_verdict: Optional[str] = None,
    include_l8: Optional[bool] = None,
    iv_status: Optional[str] = None,
    iv_data: Optional[dict] = None,
    is_data_gap: bool = False,
    gap_msg: Optional[str] = None,
) -> dict:
    """
    Computes 5-Box Pre-Trade Checklist and scores readiness to trade.
    Thresholds read from strategy1_rules.json under 'checklist'.
    """
    # Box 1: Walls defined
    min_wall = float(rules.get("chk_box1_min_wall_contracts", 50000.0))
    if s1_row is None or r1_row is None:
        b1_pass = False
        b1_status = "N/A"
        b1_issue = "Walls data unavailable"
    else:
        s1_pe = float(s1_row.get("pe_oi", 0) or 0)
        r1_ce = float(r1_row.get("ce_oi", 0) or 0)
        b1_pass = (s1_pe > min_wall and r1_ce > min_wall)
        b1_status = "PASS ✅" if b1_pass else "FAIL ✗"
        b1_issue = "Walls undefined" if not b1_pass else None

    # Box 2: Flow aligned with bias
    surge_lim = float(rules.get("chk_box2_surge_limit_contracts", 5000.0))
    ce_buy_flow = (d_pe_15m > 0 and d_ce_15m < surge_lim)
    pe_buy_flow = (d_ce_15m > 0 and d_pe_15m < surge_lim)
    if is_data_gap:
        b2_pass = True
        b2_status = "HOLD ⏸"
        b2_issue = None
    elif not has_history:
        b2_pass = False
        b2_status = "N/A"
        b2_issue = "Flow history unavailable"
    else:
        b2_pass = (ce_buy_flow or pe_buy_flow)
        b2_status = "PASS ✅" if b2_pass else "FAIL ✗"
        b2_issue = "Flow contradicts setup" if not b2_pass else None

    # Box 3: IV not expensive
    exp_thresh = float(rules.get("chk_box3_expensive_percentile", rules.get("iv_expensive_percentile", 65.0)))
    eff_iv_status = iv_status or (iv_data.get("status") if isinstance(iv_data, dict) else None)
    if eff_iv_status == "feed_anomaly":
        b3_pass = True
        b3_status = "HOLD ⏸"
        b3_issue = None
    elif iv_percentile is None:
        b3_pass = False
        b3_status = "N/A"
        b3_issue = "IV data unavailable"
    else:
        b3_pass = (iv_percentile < exp_thresh)
        b3_status = "PASS ✅" if b3_pass else "FAIL ✗"
        b3_issue = f"IV expensive ({int(round(iv_percentile))}%)" if not b3_pass else None

    # Box 4: GEX supports trade type
    gex_thresh = float(rules.get("chk_box4_gex_threshold_cr", 10.0))
    gex_status = gex_data.get("status")
    gex_cr = float(gex_data.get("total_gex_cr", 0.0))
    if gex_status != "ok":
        b4_pass = False
        b4_status = "N/A"
        b4_issue = "GEX data unavailable"
    else:
        if ce_buy_flow and not pe_buy_flow:
            b4_pass = (gex_cr > 0 or abs(gex_cr) < gex_thresh)
        elif pe_buy_flow and not ce_buy_flow:
            b4_pass = (gex_cr < 0 or abs(gex_cr) < gex_thresh)
        else:
            b4_pass = (gex_cr > 0 or abs(gex_cr) < gex_thresh or gex_cr < 0)
        b4_status = "PASS ✅" if b4_pass else "FAIL ✗"
        b4_issue = "GEX against trade" if not b4_pass else None

    # Box 5: No danger zone
    now_t = latest_ts.time()
    try:
        f_start = datetime.strptime(rules.get("chk_box5_freeze_start", "11:30"), "%H:%M").time()
        f_end = datetime.strptime(rules.get("chk_box5_freeze_end", "13:15"), "%H:%M").time()
        mkt_open = datetime.strptime(rules.get("chk_box5_market_open_time", "09:20"), "%H:%M").time()
        late_cut = datetime.strptime(rules.get("chk_box5_late_cutoff_time", "14:45"), "%H:%M").time()
    except Exception as exc:
        logger.warning(f"Error parsing checklist timing rules: {exc}")
        f_start, f_end = dtime(11, 30), dtime(13, 15)
        mkt_open, late_cut = dtime(9, 20), dtime(14, 45)

    fresh_mins = float(rules.get("chk_box5_fresh_data_minutes", 5.0))

    in_freeze = (f_start <= now_t < f_end)
    before_open = (now_t < mkt_open)
    after_cutoff = (now_t > late_cut)

    now_dt = datetime.now()
    if target_date == now_dt.date():
        age_sec = max(0.0, (now_dt - latest_ts).total_seconds())
        is_stale_data = (age_sec > fresh_mins * 60.0)
    elif target_time is not None:
        target_dt = datetime.combine(target_date, target_time)
        age_sec = abs((target_dt - latest_ts).total_seconds())
        is_stale_data = (age_sec > fresh_mins * 60.0)
    else:
        is_stale_data = False

    b5_pass = not (in_freeze or before_open or after_cutoff or is_stale_data)
    b5_status = "PASS ✅" if b5_pass else "FAIL ✗"
    if not b5_pass:
        if in_freeze:
            b5_issue = "Buyer freeze active"
        elif before_open:
            b5_issue = "Market open warmup (<09:20)"
        elif after_cutoff:
            b5_issue = "Late session (>14:45)"
        elif is_stale_data:
            b5_issue = "Stale data feed (>5m)"
        else:
            b5_issue = "Danger zone timing"
    else:
        b5_issue = None

    # Box 6: RVOL trend zone (>=1.30)
    chk_time = target_time if target_time is not None else now_t
    rvol_val = None
    try:
        import rvol_gate
        rvol_cfg = rvol_gate.load_rvol_config()
        trend_thresh = float(rvol_cfg.get("trend_zone", 1.30))
        dead_thresh = float(rvol_cfg.get("dead_zone", 0.85))
        gate_start_str = str(rvol_cfg.get("gate_start", "09:30"))
        gate_start_t = datetime.strptime(gate_start_str, "%H:%M").time()

        if chk_time < gate_start_t:
            b6_pass = True
            b6_status = "HOLD ⏸"
            b6_issue = None
        else:
            rvol_val = rvol_gate.compute_rvol(target_date, chk_time)
            if rvol_val is None:
                b6_pass = False
                b6_status = "N/A"
                b6_issue = "RVOL data unavailable"
            elif rvol_val < trend_thresh:
                b6_pass = False
                b6_status = "FAIL ✗"
                if rvol_val < dead_thresh:
                    b6_issue = f"DEAD ZONE (RVOL {rvol_val:.2f}× < {dead_thresh:.2f}×)"
                else:
                    b6_issue = f"TRAP ZONE (RVOL {rvol_val:.2f}× < {trend_thresh:.2f}×)"
            else:
                b6_pass = True
                b6_status = "PASS ✅"
                b6_issue = None
    except Exception as exc:
        logger.warning(f"Error computing RVOL for checklist: {exc}")
        b6_pass = False
        b6_status = "N/A"
        b6_issue = "RVOL data unavailable"

    # Box 7: L8 Order Flow confirmation
    # "Pre-trade checklist Box 7: 'L8 confirms direction or is silent' — FAIL if L8 is VETO. 7/7 = TRADE."
    should_include_box7 = (
        include_l8 is True
        or l8_report is not None
        or l8_verdict is not None
        or bool(rules.get("include_box7", False))
        or bool(rules.get("include_l8", False))
        or bool(rules.get("order_flow", {}).get("enabled", False))
    )

    boxes = [
        ("Walls defined:", b1_status, b1_pass, b1_issue, "Walls"),
        ("Flow aligned:", b2_status, b2_pass, b2_issue, "Flow"),
        ("IV not expensive:", b3_status, b3_pass, b3_issue, "IV"),
        ("GEX supports:", b4_status, b4_pass, b4_issue, "GEX"),
        ("No danger zone:", b5_status, b5_pass, b5_issue, "Timing"),
        ("RVOL (futures vol) (>=1.30):", b6_status, b6_pass, b6_issue, "RVOL"),
    ]

    if should_include_box7:
        v_str = ""
        sim_sup = False
        if l8_report is not None:
            v_str = str(getattr(l8_report, "verdict", l8_report)).upper()
            sim_sup = bool(getattr(l8_report, "sim_suppressed", False))
        elif l8_verdict is not None:
            v_str = str(l8_verdict).upper()
        else:
            v_str = "SILENT"

        if "VETO" in v_str and not sim_sup:
            b7_pass = False
            b7_status = "FAIL ✗"
            b7_issue = "L8 Order Flow VETO"
        elif "CONFIRM" in v_str:
            b7_pass = True
            b7_status = "PASS ✅"
            b7_issue = None
        elif "VETO" in v_str and sim_sup:
            b7_pass = True
            b7_status = "PASS (SIM) ✅"
            b7_issue = None
        else:
            b7_pass = True
            b7_status = "PASS (SILENT) ✅"
            b7_issue = None

        boxes.append(("L8 Order Flow:", b7_status, b7_pass, b7_issue, "L8 Flow"))

    score = sum(1 for b in boxes if b[2])
    total_boxes = len(boxes)
    failed_issues = [b[3] for b in boxes if not b[2] and b[3]]
    failed_tags = [b[4] for b in boxes if not b[2]]

    # Terminal rendering
    inner_w = 40
    term_lines = [
        "  ╔" + "═" * inner_w + "╗",
        _pad_box_line("  PRE-TRADE CHECKLIST", inner_w),
        "  ╠" + "═" * inner_w + "╣",
    ]
    for label, status, _, _, _ in boxes:
        term_lines.append(_pad_box_line(f"  □ {label:<27} {status}", inner_w))
    term_lines.append("  ╠" + "═" * inner_w + "╣")

    if score == total_boxes:
        verdict = "TRADE ✅"
        term_lines.append(_pad_box_line(f"  SCORE: {total_boxes}/{total_boxes} — TRADE ✅", inner_w))
        term_lines.append(_pad_box_line("  All conditions met", inner_w))
    elif score == total_boxes - 1:
        verdict = "CAUTION ⚠️"
        term_lines.append(_pad_box_line(f"  SCORE: {score}/{total_boxes} — CAUTION ⚠️", inner_w))
        iss_text = failed_issues[0] if failed_issues else "Condition weak"
        term_lines.append(_pad_box_line(f"  Issue: {iss_text}", inner_w))
    else:
        verdict = "WAIT ❌"
        term_lines.append(_pad_box_line(f"  SCORE: {score}/{total_boxes} — WAIT ❌", inner_w))
        iss_text = ", ".join(failed_tags) if failed_tags else "Conditions failed"
        term_lines.append(_pad_box_line(f"  Missing: {iss_text}", inner_w))
    term_lines.append("  ╚" + "═" * inner_w + "╝")

    # Telegram rendering
    def _box_icon(status: str) -> str:
        if "PASS" in status:
            return "✅"
        if "FAIL" in status:
            return "✗"
        if "HOLD" in status:
            return "⏸"
        return "N/A"

    icons_row = " ".join(f"{_box_icon(b[1])} {b[4]}" for b in boxes)

    tg_lines = []
    if score == total_boxes:
        tg_lines.append(f"📋 CHECKLIST: {total_boxes}/{total_boxes} — ALL CLEAR ✅")
    elif score == total_boxes - 1:
        tg_lines.append(f"📋 CHECKLIST: {score}/{total_boxes} — CAUTION ⚠️")
        tg_lines.append(icons_row)
        if "IV" in failed_tags:
            tg_lines.append("Issue: IV expensive — size down if you trade")
        elif "RVOL" in failed_tags:
            if failed_issues:
                tg_lines.append(f"Issue: {failed_issues[0]} — no trade")
            else:
                tg_lines.append("Issue: RVOL < 1.30 (not in trend zone) — no trade")
        elif "L8 Flow" in failed_tags:
            tg_lines.append("Issue: L8 Order Flow VETO — execution unconfirmed")
        else:
            iss = failed_issues[0] if failed_issues else "Condition weak"
            tg_lines.append(f"Issue: {iss} — size down if you trade")
    else:
        tg_lines.append(f"📋 CHECKLIST: {score}/{total_boxes} — WAIT ❌")
        tg_lines.append(f"Missing: {', '.join(failed_tags)}")

    return {
        "score": score,
        "verdict": verdict,
        "terminal_lines": term_lines,
        "telegram_lines": tg_lines,
        "boxes": boxes,
        "failed_issues": failed_issues,
        "failed_tags": failed_tags,
    }


def _ordinal_suffix(n: int) -> str:
    """Returns integer with its English ordinal suffix, e.g. 1st, 2nd, 3rd, 5th, 6th."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def build_expiry_1315_summary(
    latest_spot: float,
    max_pain: float,
    pcr_standing: float,
    pcr_trend: str,
    d_pe_15m: float = 0.0,
    d_ce_15m: float = 0.0,
    floor_weakening_streak: int = 0,
    eff_dom: Optional[str] = None,
    bias_str: Optional[str] = None,
) -> str:
    """Generates the 13:15 Expiry Day Telegram & Terminal Alert."""
    if latest_spot < max_pain:
        mech_pull = "UP"
        dist = abs(int(round(max_pain - latest_spot)))
        dist_str = f"{dist} pts below"
    elif latest_spot > max_pain:
        mech_pull = "DOWN"
        dist = abs(int(round(latest_spot - max_pain)))
        dist_str = f"{dist} pts above"
    else:
        mech_pull = "PINNED"
        dist = 0
        dist_str = "at max pain"

    bearish_score = 0
    bullish_score = 0

    if bias_str and "BEARISH" in bias_str:
        bearish_score += 2
    elif bias_str and "BULLISH" in bias_str:
        bullish_score += 2

    if eff_dom == "CE_DOMINANT":
        bearish_score += 1
    elif eff_dom == "PE_DOMINANT":
        bullish_score += 1

    if "Falling" in pcr_trend or "↓" in pcr_trend:
        bearish_score += 1
    elif "Rising" in pcr_trend or "↑" in pcr_trend:
        bullish_score += 1

    if d_pe_15m < 0 and d_ce_15m > 0:
        bearish_score += 1
    elif d_pe_15m > 0 and d_ce_15m < 0:
        bullish_score += 1

    if floor_weakening_streak >= 2:
        bearish_score += 1

    if bearish_score > bullish_score:
        flow_dir = "BEARISH"
    elif bullish_score > bearish_score:
        flow_dir = "BULLISH"
    else:
        flow_dir = "NEUTRAL"

    flow_parts = []
    pe_k = d_pe_15m / 1000.0
    ce_k = d_ce_15m / 1000.0
    flow_parts.append(f"PE {pe_k:+.0f}k / CE {ce_k:+.0f}k")

    if floor_weakening_streak >= 2:
        flow_parts.append(f"Floor weakening {_ordinal_suffix(floor_weakening_streak)} consecutive")

    if "Falling" in pcr_trend or "↓" in pcr_trend:
        flow_parts.append(f"PCR falling to {pcr_standing:.2f}")
    elif "Rising" in pcr_trend or "↑" in pcr_trend:
        flow_parts.append(f"PCR rising to {pcr_standing:.2f}")
    else:
        flow_parts.append(f"PCR {pcr_standing:.2f}")

    flow_details_str = ", ".join(flow_parts)

    is_agreement = (
        (mech_pull == "UP" and flow_dir == "BULLISH") or
        (mech_pull == "DOWN" and flow_dir == "BEARISH")
    )
    is_disagreement = (
        (mech_pull == "UP" and flow_dir == "BEARISH") or
        (mech_pull == "DOWN" and flow_dir == "BULLISH")
    )

    return (
        f"🎯 MAX PAIN: {int(round(max_pain)):,} | Spot {int(round(latest_spot)):,} ({dist_str}) (informational only — no edge on Nifty)\n"
        f"Writer flow: {flow_dir} ({flow_details_str})\n"
        f"➡️ Trade writer flow / price action only — Max Pain has no directional edge"
    )


def format_floor_weakening_warning(
    streak: int,
    min_consecutive: int = 2,
    strike: Optional[float] = None,
    pe_delta: Optional[float] = None,
    gex_regime: Optional[str] = None,
    is_cascading: bool = False,
    staged: bool = False,
) -> Optional[str]:
    """Generates the floor weakening warning message if consecutive streak threshold is met."""
    if streak < min_consecutive and not is_cascading:
        return None

    if staged and strike is not None:
        def fmt_k_val(val: float) -> str:
            sgn = "+" if val >= 0 else "-"
            return f"{sgn}{abs(val)/1000:,.0f}k" if abs(val) >= 1000 else f"{sgn}{abs(val):,.0f}"

        pe_txt = fmt_k_val(pe_delta) if pe_delta is not None else "-0k"
        gex_mod = ""
        if gex_regime:
            reg_upper = str(gex_regime).upper()
            if "NEUTRAL" in reg_upper:
                gex_mod = "\n     GEX NEUTRAL gamma — no regime (moves follow flow)"
            elif "LONG" in reg_upper:
                gex_mod = "\n     GEX LONG gamma — break likely absorbed, not extended"
            elif "SHORT" in reg_upper:
                gex_mod = "\n     GEX SHORT gamma — dealer hedging amplifies break, moves run"

        if is_cascading:
            return f"🚨 FLOOR CASCADING {int(round(strike)):,} (PE ΔOI {pe_txt}){gex_mod}"
        return f"⚠️ FLOOR WEAKENING {int(round(strike)):,} (PE ΔOI {pe_txt}, {_ordinal_suffix(streak)} interval){gex_mod}"

    if streak == 2:
        return "⚠️ Floor weakening — 2nd consecutive warning"
    if streak == 3:
        return "⚠️ Floor weakening — 3rd consecutive warning"
    return f"⚠️ Floor weakening — {streak}th consecutive warning"


class DayRange(tuple):
    """2-tuple (day_low, day_high) with day_low_time and day_high_time attributes."""
    day_low: float
    day_high: float
    day_low_time: str
    day_high_time: str

    def __new__(cls, day_low: float, day_high: float, day_low_time: str = "09:15", day_high_time: str = "09:15"):
        item = super().__new__(cls, (day_low, day_high))
        item.day_low = float(day_low)
        item.day_high = float(day_high)
        item.day_low_time = str(day_low_time)
        item.day_high_time = str(day_high_time)
        return item


def compute_day_range(
    target_date: date,
    eval_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    all_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    target_time: Optional[dtime] = None,
) -> DayRange:
    """
    Computes today's low and high spot values from 5m/1m/15m OHLC candles and snapshots.
    Guarantees:
      low = min(candle low, running min of spot)
      high = max(candle high, running max of spot)
      Asserts spot within [low, high] on every entry.
    """
    candle_low = float("inf")
    candle_high = float("-inf")
    candle_low_time = "09:15"
    candle_high_time = "09:15"

    # 1. Check candle sources (1m candles for exact minute, then 5m/15m)
    for p in [
        config.DATA_DIR / "candles" / f"{target_date}.csv",
        config.DATA_DIR / "candles_5m" / f"{target_date}.csv",
        config.DATA_DIR / "candles_15m" / f"{target_date}.csv",
    ]:
        if p.exists():
            try:
                cdf = pd.read_csv(p)
                if not cdf.empty:
                    cdf = cdf.rename(columns={c: c.lower() for c in cdf.columns})
                    if "high" in cdf.columns and "low" in cdf.columns and "timestamp" in cdf.columns:
                        cdf["ts"] = pd.to_datetime(cdf["timestamp"])
                        cdf["t"] = cdf["ts"].dt.time
                        cond = (cdf["t"] >= dtime(9, 15))
                        if target_time is not None:
                            cond = cond & (cdf["t"] <= target_time)
                        cdf_mkt = cdf[cond]
                        if not cdf_mkt.empty:
                            id_low = cdf_mkt["low"].idxmin()
                            id_high = cdf_mkt["high"].idxmax()
                            c_low = float(cdf_mkt.loc[id_low, "low"])
                            c_high = float(cdf_mkt.loc[id_high, "high"])
                            if c_low < candle_low:
                                candle_low = c_low
                                candle_low_time = cdf_mkt.loc[id_low, "ts"].strftime("%H:%M")
                            if c_high > candle_high:
                                candle_high = c_high
                                candle_high_time = cdf_mkt.loc[id_high, "ts"].strftime("%H:%M")
                            # If 5m candles are available and complete to target_time, that is authoritative
                            if "candles_5m" in str(p) and not cdf_mkt.empty:
                                break
            except Exception as e:
                logger.warning(f"Error reading candle file {p}: {e}")

    # 2. Extract running spot extremes from snapshots up to target_time
    combined_snaps = (all_snaps or []) + (eval_snaps or [])
    mkt_spots = [
        (s[0], float(s[1])) for s in combined_snaps
        if s[0].date() == target_date and dtime(9, 15) <= s[0].time() <= (target_time or dtime(15, 30)) and float(s[1]) > 0
    ]

    spot_low = float("inf")
    spot_high = float("-inf")
    spot_low_time = "09:15"
    spot_high_time = "09:15"

    if mkt_spots:
        min_s = min(mkt_spots, key=lambda x: x[1])
        spot_low = min_s[1]
        spot_low_time = min_s[0].strftime("%H:%M")

        max_s = max(mkt_spots, key=lambda x: x[1])
        spot_high = max_s[1]
        spot_high_time = max_s[0].strftime("%H:%M")

    day_low = 0.0
    day_high = 0.0
    day_low_time = "09:15"
    day_high_time = "09:15"

    # 3. Combine: low = min(candle low, running min of spot); high likewise
    if candle_low < spot_low:
        day_low = candle_low
        day_low_time = candle_low_time
    elif spot_low != float("inf"):
        day_low = spot_low
        day_low_time = spot_low_time

    if candle_high > spot_high:
        day_high = candle_high
        day_high_time = candle_high_time
    elif spot_high != float("-inf"):
        day_high = spot_high
        day_high_time = spot_high_time

    # 4. Containment assertion: spot must be within [day_low, day_high]
    if eval_snaps:
        latest_spot = float(eval_snaps[-1][1])
        if latest_spot > 0:
            if latest_spot < day_low:
                day_low = latest_spot
                day_low_time = eval_snaps[-1][0].strftime("%H:%M")
            if latest_spot > day_high:
                day_high = latest_spot
                day_high_time = eval_snaps[-1][0].strftime("%H:%M")
            assert day_low <= latest_spot <= day_high, (
                f"Spot containment violated: spot {latest_spot} not in [{day_low}, {day_high}]"
            )

    return DayRange(day_low, day_high, day_low_time, day_high_time)


def check_approaching_ceiling_floor(
    latest_spot: float,
    latest_df: pd.DataFrame,
    r1_row: Optional[pd.Series],
    r2_row: Optional[pd.Series],
    s1_row: Optional[pd.Series],
    s2_row: Optional[pd.Series],
    rules: dict,
    mult: float,
) -> Optional[str]:
    """
    When spot comes within 15 points (sr_approach_threshold_pts) of R1 or R2:
    ⚠️ APPROACHING CEILING
    Spot [X] → R1 [level] CE [OI]L only [Y] pts away
    If rejection candle forms → PE buy opportunity
    ➡️ WATCH for rejection at [level]

    When spot comes within 15 points of S1 or S2:
    ⚠️ APPROACHING FLOOR
    Spot [X] → S1 [level] PE [OI]L only [Y] pts away
    If bounce candle forms → CE buy opportunity
    ➡️ WATCH for bounce at [level]
    """
    approach_thresh = float(rules.get("sr_approach_threshold_pts", 15.0))
    spot_str = f"{int(round(latest_spot)):,}"

    # Check Ceiling (R1, then R2)
    for label, row in [("R1", r1_row), ("R2", r2_row)]:
        if row is not None:
            r_strike = float(row["strike"])
            dist = r_strike - latest_spot
            if 0.0 <= dist <= approach_thresh:
                oi_l = f"{float(row.get('ce_oi', 0) or 0) * mult:.1f}"
                return (
                    f"⚠️ APPROACHING CEILING\n"
                    f"Spot {spot_str} → {label} {int(round(r_strike)):,} CE {oi_l}L only {int(round(dist))} pts away\n"
                    f"If rejection candle forms → PE buy opportunity\n"
                    f"➡️ WATCH for rejection at {int(round(r_strike)):,}"
                )

    # Check Floor (S1, then S2)
    for label, row in [("S1", s1_row), ("S2", s2_row)]:
        if row is not None:
            s_strike = float(row["strike"])
            dist = latest_spot - s_strike
            if 0.0 <= dist <= approach_thresh:
                oi_l = f"{float(row.get('pe_oi', 0) or 0) * mult:.1f}"
                return (
                    f"⚠️ APPROACHING FLOOR\n"
                    f"Spot {spot_str} → {label} {int(round(s_strike)):,} PE {oi_l}L only {int(round(dist))} pts away\n"
                    f"If bounce candle forms → CE buy opportunity\n"
                    f"➡️ WATCH for bounce at {int(round(s_strike)):,}"
                )

    return None


def check_trend_higher_lows(snaps: list, now: datetime) -> bool:
    """
    Check 2 — Trend Filter:
    Only fire if the last 3 snapshots (9 minutes) show spot making HIGHER lows, not lower lows.
    Implementation:
      Get spot prices at t-9min, t-6min, t-3min.
      If spot[t-3] > spot[t-6] > spot[t-9]:
          trend is turning UP -> ALLOW alert
      Else:
          downtrend intact -> SUPPRESS alert
    """
    if not snaps or len(snaps) < 3:
        return False

    def get_spot_at_offset(target_offset_min: float) -> Optional[float]:
        target_t = now - timedelta(minutes=target_offset_min)
        candidates = [s for s in snaps if s[0] <= target_t]
        if not candidates:
            return None
        if (target_t - candidates[-1][0]).total_seconds() > 180:
            return None
        return candidates[-1][1]

    sp_9m = get_spot_at_offset(9.0)
    sp_6m = get_spot_at_offset(6.0)
    sp_3m = get_spot_at_offset(3.0)

    if sp_9m is None or sp_6m is None or sp_3m is None:
        if len(snaps) >= 4:
            sp_3m = snaps[-2][1]
            sp_6m = snaps[-3][1]
            sp_9m = snaps[-4][1]
        else:
            return False

    return bool(sp_3m > sp_6m > sp_9m)


def check_s1_liquidity_trap(
    eval_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    s1_strike: Optional[float],
    latest_spot: float,
    rules: dict,
) -> Optional[str]:
    """
    Fires when spot breaks below S1 and then reclaims it.
    Enforces BOTH:
      CHECK 1: Bounce strength >= min_false_breakdown_reclaim_pts (15 pts) within 2 snapshots (6 min)
      CHECK 2: Trend filter: Higher lows over last 3 snapshots (9 min)
    This is the most reliable CE buy signal of the day.
    """
    if s1_strike is None or len(eval_snaps) < 2:
        return None

    if not rules.get("s1_reclaim_trap_alert_enabled", True):
        return None

    now = eval_snaps[-1][0]
    eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
    try:
        eod_no_trade_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
        if now.time() >= eod_no_trade_t:
            return None
    except Exception:
        if now.time() >= dtime(15, 0):
            return None
    min_reclaim_pts = float(
        rules.get("alerts", {}).get(
            "min_false_breakdown_reclaim_pts",
            rules.get("min_false_breakdown_reclaim_pts", 15.0)
        )
    )
    sweep_min = float(rules.get("sweep_min_pts", 2.0))
    sweep_max = float(rules.get("sweep_max_pts", 25.0))

    # CHECK 1 — BOUNCE STRENGTH:
    # Spot must have swept below S1 within 2 snapshots (6 minutes)
    sweep_snaps = [s for s in eval_snaps if 0 <= (now - s[0]).total_seconds() <= 360]
    if len(sweep_snaps) < 3 and len(eval_snaps) >= 3:
        sweep_snaps = eval_snaps[-3:]
    sweep_low = min(s[1] for s in sweep_snaps)
    sweep_pts = s1_strike - sweep_low

    if not (sweep_min <= sweep_pts <= sweep_max):
        return None

    # Reclaim must move >= min_false_breakdown_reclaim_pts above S1
    if latest_spot < (s1_strike + min_reclaim_pts):
        return None

    # CHECK 2 — TREND FILTER:
    # Spot must make higher lows over last 3 snapshots (9 minutes)
    if not check_trend_higher_lows(eval_snaps, now):
        return None

    clock_str = now.strftime("%H:%M")
    return (
        f"🪤 LIQUIDITY TRAP | NIFTY {int(round(latest_spot)):,} | "
        f"S1 {int(round(s1_strike)):,} Swept (Low {int(round(sweep_low)):,} → Reclaim {int(round(latest_spot)):,}) | "
        f"Breakout Sellers Trapped | CE BUY SIGNAL | {clock_str}"
    )


def build_freeze_end_alert(
    latest_ts: datetime,
    latest_spot: float,
    latest_df: pd.DataFrame,
    s1_strike: Optional[float],
    r1_strike: Optional[float],
    bias_word: str,
    action_word: str,
    eval_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    rules: dict,
) -> str:
    """
    Constructs special Telegram alert when buyer freeze window ends at 13:15:
    "🔔 FREEZE WINDOW ENDED — Market open for trading
    Spot: [current] | Bias: [current]
    Floor: [S1] | Ceiling: [R1]
    OI since 11:30: Puts [+/-Xk] | Calls [+/-Xk]
    ➡️ [action based on current bias]"
    """
    freeze_start_str = rules.get("freeze_start", "11:30")
    try:
        f_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        snaps_start = [s for s in eval_snaps if s[0].time() <= f_start_t]
        df_start = snaps_start[-1][2] if snaps_start else eval_snaps[0][2]
    except Exception as e:
        logger.warning(f"Error getting snapshot at freeze start {freeze_start_str}: {e}")
        df_start = eval_snaps[0][2] if eval_snaps else latest_df

    atm = float(round(latest_spot / 50.0) * 50.0)
    scope = rules.get("freeze_end_alert_scope", "near")

    if scope == "near":
        near_curr = latest_df[(latest_df["strike"] >= atm - 100.0) & (latest_df["strike"] <= atm + 100.0)]
        near_prev = df_start[(df_start["strike"] >= atm - 100.0) & (df_start["strike"] <= atm + 100.0)]
        d_pe = float(near_curr["pe_oi"].sum() - near_prev["pe_oi"].sum())
        d_ce = float(near_curr["ce_oi"].sum() - near_prev["ce_oi"].sum())
    elif scope == "atm":
        curr_atm = latest_df[latest_df["strike"] == atm]
        prev_atm = df_start[df_start["strike"] == atm]
        d_pe = float(curr_atm["pe_oi"].sum() - prev_atm["pe_oi"].sum()) if not curr_atm.empty and not prev_atm.empty else 0.0
        d_ce = float(curr_atm["ce_oi"].sum() - prev_atm["ce_oi"].sum()) if not curr_atm.empty and not prev_atm.empty else 0.0
    else:
        d_pe = float(latest_df["pe_oi"].sum() - df_start["pe_oi"].sum())
        d_ce = float(latest_df["ce_oi"].sum() - df_start["ce_oi"].sum())

    pe_sign = f"{d_pe/1000:+.0f}k" if abs(d_pe) >= 1000 else f"{d_pe:+.0f}"
    ce_sign = f"{d_ce/1000:+.0f}k" if abs(d_ce) >= 1000 else f"{d_ce:+.0f}"

    floor_str = f"{int(round(s1_strike)):,}" if s1_strike is not None else "N/A"
    ceiling_str = f"{int(round(r1_strike)):,}" if r1_strike is not None else "N/A"

    # Determine bias and action from OI shift during freeze window:
    # Puts ADDING + Calls LEAVING -> market has SUPPORT building -> BULLISH -> CE BUY SIGNAL
    # Calls ADDING + Puts LEAVING -> market has RESISTANCE building -> BEARISH -> PE BUY SIGNAL
    # Both adding / both leaving -> undecided -> WAIT FOR DIRECTION
    if d_pe > 0 and d_ce <= 0:
        bias_word = "BULLISH ▲"
        act_line = "➡️ CE BUY SIGNAL — confirm 8 EMA on chart"
    elif d_ce > 0 and d_pe <= 0:
        bias_word = "BEARISH ▼"
        act_line = "➡️ PE BUY SIGNAL — confirm 8 EMA on chart"
    elif d_pe > 0 and d_ce > 0:
        bias_word = "NEUTRAL →"
        act_line = "➡️ WAIT FOR DIRECTION"
    elif d_pe < 0 and d_ce < 0:
        bias_word = "NEUTRAL →"
        act_line = "➡️ WAIT FOR DIRECTION"
    else:
        bias_word = bias_word or "NEUTRAL →"
        act_word = action_word or "WAIT FOR DIRECTION"
        act_line = act_word if act_word.startswith("➡️") else f"➡️ {act_word}"

    return (
        f"🔔 FREEZE WINDOW ENDED — Market open for trading\n"
        f"Spot: {int(round(latest_spot)):,} | Bias: {bias_word}\n"
        f"Floor: {floor_str} | Ceiling: {ceiling_str}\n"
        f"OI since {freeze_start_str}: Puts {pe_sign} | Calls {ce_sign}\n"
        f"{act_line}"
    )


def build_eod_summary(
    target_date: date,
    eval_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    all_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    rules: dict,
    latest_spot: float,
    latest_df: pd.DataFrame,
    r1_strike: Optional[float],
    r2_strike: Optional[float],
    s1_strike: Optional[float],
    s2_strike: Optional[float],
) -> str:
    """
    Constructs the end-of-day summary alert at 15:25:
    "📋 TODAY'S SUMMARY | 18 Sep
    Day range: 23,270 → 23,389 (119 pts)
    Floor held: 23,300 ✅ 
    Ceiling held: 23,389 (R2 23,400) ✅
    Best CE signal: 09:50 false breakdown ✅
    Missed move: 13:15 breakout (freeze window)
    App accuracy: Floors/ceilings correct ✅
    Tomorrow watch: [current S1] and [current R1]"
    """
    date_str = target_date.strftime("%d %b")
    day_low, day_high = compute_day_range(target_date, eval_snaps, all_snaps)
    low_int = int(day_low)
    high_int = int(round(day_high))
    range_pts = high_int - low_int

    floor_val = s1_strike if s1_strike is not None else 23300.0
    floor_held = (latest_spot >= floor_val)
    floor_icon = "✅" if floor_held else "❌"
    floor_line = f"Floor held: {int(round(floor_val)):,} {floor_icon}"

    ceil_val = r2_strike if (r2_strike is not None and abs(high_int - r2_strike) <= 50) else (r1_strike if r1_strike is not None else 23400.0)
    ceil_label = "R2" if (r2_strike is not None and ceil_val == r2_strike) else "R1"
    ceil_held = (high_int <= ceil_val + 5.0)
    ceil_icon = "✅" if ceil_held else "❌"
    ceiling_line = f"Ceiling held: {high_int:,} ({ceil_label} {int(round(ceil_val)):,}) {ceil_icon}"

    s1_txt = f"{int(round(s1_strike)):,}" if s1_strike is not None else "N/A"
    r1_txt = f"{int(round(r1_strike)):,}" if r1_strike is not None else "N/A"

    # Scan observation log for real logged signals
    obs_file = config.DATA_DIR / "observations" / f"{target_date}.md"
    signals_found: List[str] = []
    if obs_file.exists():
        try:
            for ln in obs_file.read_text(encoding="utf-8").splitlines():
                if "CE BUY" in ln:
                    signals_found.append("CE BUY")
                elif "PE BUY" in ln:
                    signals_found.append("PE BUY")
                elif "LIQUIDITY TRAP" in ln:
                    signals_found.append("LIQUIDITY TRAP")
        except Exception:
            pass

    if signals_found:
        signal_line = f"Signals logged: {', '.join(sorted(set(signals_found)))}"
    else:
        signal_line = "Signals logged: NO TRADE (system disciplined)"

    # Audit for session data gaps (Rule 4: data_gap.max_gap_seconds)
    max_gap_sec = float(rules.get("data_gap", {}).get("max_gap_seconds", rules.get("max_gap_seconds", 180.0)))
    gaps_found: List[str] = []
    snaps_to_check = [s for s in (eval_snaps or all_snaps or []) if dtime(9, 15) <= s[0].time() <= dtime(15, 30)]
    for i in range(1, len(snaps_to_check)):
        p_t = snaps_to_check[i - 1][0]
        c_t = snaps_to_check[i][0]
        gap_s = (c_t - p_t).total_seconds()
        if gap_s > max_gap_sec:
            g_m = int(round(gap_s / 60.0))
            gaps_found.append(f"{p_t.strftime('%H:%M')}–{c_t.strftime('%H:%M')} ({g_m} min)")

    lines = [
        f"📋 TODAY'S SUMMARY | {date_str}",
        f"Day range: {low_int:,} → {high_int:,} ({range_pts} pts)",
        floor_line,
        ceiling_line,
        signal_line,
        *([f"Data gaps: {', '.join(gaps_found)} — flow/streak signals invalidated"] if gaps_found else []),
        f"Tomorrow watch: {s1_txt} and {r1_txt}",
    ]
    return "\n".join(lines)


def was_prev_session_expiry(prev_date: date, rules: Optional[Dict[str, Any]] = None) -> bool:
    """
    True when `prev_date` was itself an expiry session.

    Participant-wise OI for an expiry session is measured AFTER that week's
    contracts have been settled, so every option column (CE/PE long & short,
    Net CE, Net PE) collapses. Their percentile ranks then read as extreme
    lows that describe settlement, not positioning. Futures columns are
    unaffected and stay comparable across the expiry boundary.

    Detection order:
      1. The snapshot meta for prev_date names prev_date as its expiry.
      2. Fallback: prev_date falls on the configured weekly expiry weekday
         (expiry_mode.expiry_weekday, default 1 = Tuesday).
    """
    try:
        r = rules or load_briefing_rules()
        exp_cfg = r.get("expiry_mode", {}) if isinstance(r, dict) else {}

        snap_root = Path(__file__).parent / "data" / "snapshots" / prev_date.strftime("%Y-%m-%d")
        if snap_root.is_dir():
            meta_files = sorted(snap_root.glob("*.meta.json"))
            if meta_files:
                try:
                    m_data = json.loads(meta_files[-1].read_text())
                    exp_d = parse_expiry_date(m_data.get("expiry"))
                    if exp_d is not None:
                        return exp_d == prev_date
                except Exception:  # noqa: BLE001
                    pass

        expiry_weekday = int(exp_cfg.get("expiry_weekday", 1))  # Mon=0, Tue=1
        return prev_date.weekday() == expiry_weekday
    except Exception as err:  # noqa: BLE001
        logger.warning(f"was_prev_session_expiry error: {err}")
        return False


def build_institutional_context(
    target_date: date,
    gex_data: Optional[Dict[str, Any]] = None,
    iv_data: Optional[Dict[str, Any]] = None,
    rules: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Constructs the INSTITUTIONAL CONTEXT block for the morning Telegram briefing (at 09:15).
    Reads previous-session EOD positioning from participant_history.py.
    Returns formatted multi-line text block, or None if data is not available (Rule 8).
    """
    try:
        import participant_history as ph
    except Exception as err:
        logger.warning(f"Could not import participant_history: {err}")
        return None

    try:
        tidy_df = ph.load()
    except Exception as err:
        logger.warning(f"Could not load participant data: {err}")
        return None

    if tidy_df is None or tidy_df.empty:
        return None

    r = rules or load_briefing_rules()
    inst_cfg = r.get("institutional_context", {})
    if not inst_cfg.get("enabled", True):
        return None

    if isinstance(target_date, str):
        try:
            target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except Exception:
            pass

    # Filter previous session completed data strictly before target_date
    tidy_df = tidy_df.copy()
    tidy_df["date"] = pd.to_datetime(tidy_df["date"]).dt.date
    df_prev = tidy_df[tidy_df["date"] < target_date]
    if df_prev.empty:
        df_prev = tidy_df[tidy_df["date"] <= target_date]
    if df_prev.empty:
        return None

    prev_date = df_prev["date"].max()
    window = int(r.get("window_sessions", 65))

    # Option OI resets on expiry, so the session AFTER an expiry (normally
    # Wednesday) has option percentiles that reflect settlement, not
    # positioning. Futures columns remain valid.
    post_expiry = was_prev_session_expiry(prev_date, r)

    try:
        pcts_df = ph.compute_percentiles(tidy_df, window=window, as_of_date=prev_date, rules=r)
        div_dict = ph.compute_divergence(df_prev, window=window, rules=r)
    except Exception as err:
        logger.warning(f"Error computing participant percentiles/divergence: {err}")
        return None

    if pcts_df.empty:
        return None

    # Thresholds from rules
    pro_bullish_thresh = float(inst_cfg.get("pro_bias", {}).get("bullish_pct", 70.0))
    pro_bearish_thresh = float(inst_cfg.get("pro_bias", {}).get("bearish_pct", 30.0))
    ret_ce_extreme_thresh = float(inst_cfg.get("retail_trap", {}).get("net_ce_extreme_pct", 90.0))
    ret_pe_extreme_thresh = float(inst_cfg.get("retail_trap", {}).get("net_pe_extreme_pct", 90.0))
    spread_extreme_thresh = float(inst_cfg.get("spread", {}).get("extreme_pct", 90.0))
    spread_high_thresh = float(inst_cfg.get("spread", {}).get("high_pct", 70.0))
    high_conviction = float(inst_cfg.get("percentile_conviction", {}).get("high", 80.0))
    low_conviction = float(inst_cfg.get("percentile_conviction", {}).get("low", 20.0))

    # 1. PRO bias
    pro_row = pcts_df[(pcts_df["participant"] == "PRO") & (pcts_df["series"] == "fut_long")]
    pro_fut_pct = float(pro_row["percentile_rank"].iloc[0]) if not pro_row.empty else 50.0

    if pro_fut_pct > pro_bullish_thresh:
        pro_bias = "BULLISH"
        pro_bias_line = f"PRO bias: BULLISH ▲ (fut long {int(pro_fut_pct)}th pct)"
    elif pro_fut_pct < pro_bearish_thresh:
        pro_bias = "BEARISH"
        pro_bias_line = f"PRO bias: BEARISH ▼ (fut long {int(pro_fut_pct)}th pct)"
    else:
        pro_bias = "NEUTRAL"
        pro_bias_line = f"PRO bias: NEUTRAL → (fut long {int(pro_fut_pct)}th pct)"

    # 2. FII hedge
    fii_net_pe_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "net_pe")]
    fii_pe_long_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "pe_long")]
    fii_net_pe_pct = float(fii_net_pe_row["percentile_rank"].iloc[0]) if not fii_net_pe_row.empty else 50.0
    fii_pe_long_pct = float(fii_pe_long_row["percentile_rank"].iloc[0]) if not fii_pe_long_row.empty else 50.0
    fii_pe_pct = max(fii_net_pe_pct, fii_pe_long_pct)

    if post_expiry:
        fii_hedge_line = "FII hedge: n/a — post-expiry (option OI reset at settlement)"
    elif fii_pe_pct >= high_conviction:
        fii_hedge_line = f"FII hedge: HEAVY (PE long {int(fii_pe_pct)}th pct — normal posture)"
    elif fii_pe_pct <= low_conviction:
        fii_hedge_line = f"FII hedge: LIGHT (PE long {int(fii_pe_pct)}th pct — unusual)"
    else:
        fii_hedge_line = f"FII hedge: NORMAL (PE long {int(fii_pe_pct)}th pct — normal posture)"

    # 3. Retail positioning & trap detection
    ret_net_ce_row = pcts_df[(pcts_df["participant"] == "Retailers") & (pcts_df["series"] == "net_ce")]
    ret_net_pe_row = pcts_df[(pcts_df["participant"] == "Retailers") & (pcts_df["series"] == "net_pe")]
    ret_net_ce_pct = float(ret_net_ce_row["percentile_rank"].iloc[0]) if not ret_net_ce_row.empty else 50.0
    ret_net_pe_pct = float(ret_net_pe_row["percentile_rank"].iloc[0]) if not ret_net_pe_row.empty else 50.0

    retail_trap = None
    retail_trap_side = None
    if post_expiry:
        # Retail CE/PE percentiles are settlement artefacts today — no trap read.
        retail_line = "Retail: n/a — post-expiry (option OI reset at settlement)"
    elif ret_net_ce_pct > ret_ce_extreme_thresh:
        retail_line = f"Retail: WRONG SIDE (CE long {int(ret_net_ce_pct)}th pct extreme)"
        retail_trap = "Call buyers may get squeezed"
        retail_trap_side = "CE"
    elif ret_net_pe_pct > ret_pe_extreme_thresh:
        retail_line = f"Retail: WRONG SIDE (PE long {int(ret_net_pe_pct)}th pct extreme)"
        retail_trap = "Put buyers may get squeezed"
        retail_trap_side = "PE"
    elif ret_net_ce_pct > high_conviction:
        retail_line = f"Retail: CALL HEAVY ({int(ret_net_ce_pct)}th pct)"
    elif ret_net_pe_pct > high_conviction:
        retail_line = f"Retail: PUT HEAVY ({int(ret_net_pe_pct)}th pct)"
    else:
        retail_line = "Retail: BALANCED"

    # 4. Spread (FII vs Client divergence)
    spreads = div_dict.get("spreads", {})
    pe_spread_pct = float(spreads.get("pe_spread", {}).get("percentile_rank", 50.0) or 50.0)
    ce_spread_pct = float(spreads.get("ce_spread", {}).get("percentile_rank", 50.0) or 50.0)
    fut_spread_pct = float(spreads.get("fut_spread", {}).get("percentile_rank", 50.0) or 50.0)
    # participant_history.compute_divergence defines a spread as extreme when it
    # is ABOVE the 90th OR BELOW the 10th percentile — both tails mean FII and
    # retail are maximally opposed, only the side differs. Rank each spread by
    # its distance from the median so a 0th-percentile reading is not mislabelled
    # as "NORMAL".
    def _two_sided(p: float) -> float:
        return max(float(p), 100.0 - float(p))

    if post_expiry:
        # CE/PE spreads are distorted by settlement — futures spread only.
        raw_spread_pct = fut_spread_pct
        spread_suffix = " [futures only]"
    else:
        raw_spread_pct = max(
            (pe_spread_pct, ce_spread_pct, fut_spread_pct),
            key=_two_sided,
        )
        spread_suffix = ""
    spread_pct = _two_sided(raw_spread_pct)

    if spread_pct >= spread_extreme_thresh:
        spread_line = f"Spread: EXTREME ({int(raw_spread_pct)}th pct){spread_suffix} — big move coming"
    elif spread_pct >= spread_high_thresh:
        spread_line = f"Spread: HIGH ({int(raw_spread_pct)}th pct){spread_suffix} — directional pressure building"
    else:
        spread_line = f"Spread: NORMAL ({int(raw_spread_pct)}th pct){spread_suffix}"

    # 5. Combined Action Line (Rule 7: Institutional + GEX + IV)
    gex_regime = str((gex_data or {}).get("regime", "")).upper()
    iv_regime = str((iv_data or {}).get("label", (iv_data or {}).get("cheap_or_expensive", ""))).upper()

    pro_opposite = (
        (retail_trap_side == "CE" and pro_bias == "BEARISH")
        or (retail_trap_side == "PE" and pro_bias == "BULLISH")
    )

    if retail_trap and pro_opposite:
        action_line = "➡️ TRAP SETUP — retail will get squeezed"
    elif pro_bias == "BULLISH" and gex_regime == "SHORT" and iv_regime == "CHEAP":
        action_line = "➡️ STRONG CE BUY SETUP — all layers aligned"
    elif pro_bias == "BULLISH" and gex_regime == "LONG" and iv_regime == "CHEAP":
        action_line = "➡️ CE bias but rangebound — wait for wall break"
    elif pro_bias == "BEARISH":
        action_line = "➡️ PE bias — smart money positioned short"
    elif pro_bias == "BULLISH":
        action_line = "➡️ CE bias — smart money positioned long"
    else:
        action_line = "➡️ NEUTRAL — wait for morning price action"

    # Watch for line
    if pro_bias == "BULLISH" and retail_trap_side == "CE":
        watch_line = "➡️ Watch for: Upside breakout OR call buyer washout"
    elif pro_bias == "BEARISH" and retail_trap_side == "PE":
        watch_line = "➡️ Watch for: Downside breakdown OR put buyer washout"
    elif pro_bias == "BULLISH":
        watch_line = "➡️ Watch for: Upside breakout above resistance"
    elif pro_bias == "BEARISH":
        watch_line = "➡️ Watch for: Downside breakdown below support"
    else:
        watch_line = "➡️ Watch for: Directional expansion outside range"

    out_lines = [
        "🏛️ INSTITUTIONAL CONTEXT (prev session EOD)",
    ]
    if post_expiry:
        out_lines.append("⚠️ Prev session was EXPIRY — option OI reset at settlement.")
        out_lines.append("   Futures readings only; option percentiles suppressed.")
    out_lines += [
        "",
        pro_bias_line,
        fii_hedge_line,
        retail_line,
        spread_line,
    ]

    # Post-expiry the FII futures stance is the one clean cross-session read.
    if post_expiry:
        fii_fut_row = pcts_df[(pcts_df["participant"] == "FII") & (pcts_df["series"] == "net_fut")]
        if not fii_fut_row.empty:
            fii_fut_pct = float(fii_fut_row["percentile_rank"].iloc[0])
            if fii_fut_pct <= low_conviction:
                fii_fut_txt = f"FII futures: NET SHORT extreme ({int(fii_fut_pct)}th pct)"
            elif fii_fut_pct >= high_conviction:
                fii_fut_txt = f"FII futures: NET LONG extreme ({int(fii_fut_pct)}th pct)"
            else:
                fii_fut_txt = f"FII futures: neutral ({int(fii_fut_pct)}th pct)"
            out_lines.append(fii_fut_txt)

    out_lines += [
        "",
        f"➡️ Smart money: {pro_bias}",
    ]
    if retail_trap:
        out_lines.append(f"➡️ Retail trap: {retail_trap}")
    out_lines.append(action_line)
    out_lines.append(watch_line)

    return "\n".join(out_lines)


# ═══════════════════════════════════════════════════════════════════════════
# Daily Observation Log System (Rule 4: strategy1_rules.json / observations)
# ═══════════════════════════════════════════════════════════════════════════

def format_observation_entry(
    target_date: date,
    latest_ts: datetime,
    latest_spot: float,
    bias_word: str,
    gex_data: Optional[dict],
    iv_data: Optional[dict],
    s1_row: Optional[pd.Series],
    r1_row: Optional[pd.Series],
    mult: float,
    d_pe_15m: float,
    d_ce_15m: float,
    floor_weakening_streak: int,
    chk_data: Optional[dict],
    action_line: str,
    rules: dict,
    alert_line: Optional[str] = None,
    rvol_val: Any = "DEFAULT",
    day_range_line: Optional[str] = None,
    dom_data: Optional[dict] = None,
    is_data_gap: bool = False,
    gap_msg: Optional[str] = None,
    tf: Optional[str] = None,
    writer_lines_str: Optional[str] = None,
) -> str:
    """
    Formats an observation entry adhering to the strict format schema:
    ## 10:16 | Spot 23,373 | CAUTION ⚠️
    - GEX: -₹18Cr LONG gamma (rangebound)
    - RVOL: 0.52× CHOP ZONE
    - IV: 82nd pct EXPENSIVE
    - Floor: 23,300 PE [234L] | Ceiling: 23,400 CE [245L]
    - Flow: Puts leaving -19k | Calls adding +13k
    - Floor weakening: 4th consecutive
    - Alert: [FLOOR_TEST] Spot 23,358 → 23,350 PE floor [162L]
    - Checklist: 4/6 WAIT (IV, RVOL failing)
    - Action: NO TRADE
    > Note: [chart observation / what price did]

    ---
    """
    time_str = latest_ts.strftime("%H:%M")
    spot_str = f"{int(round(latest_spot)):,}"
    mkt_open_str = rules.get(
        "observations_market_open_time",
        rules.get("market_open_time", getattr(config, "MARKET_OPEN", dtime(9, 15)).strftime("%H:%M") if hasattr(config, "MARKET_OPEN") else "09:15")
    )
    try:
        mkt_open_t = datetime.strptime(str(mkt_open_str), "%H:%M").time()
    except Exception:
        mkt_open_t = dtime(9, 15)
    is_pre_market = (latest_ts.time() < mkt_open_t)

    if is_pre_market:
        header = f"## {time_str} | Spot {spot_str} (prev close) | PRE-MARKET"
    else:
        bias_clean = bias_word.strip()
        header = f"## {time_str} | Spot {spot_str} | {bias_clean}"

    # 1. GEX line
    total_gex_cr = gex_data.get("total_gex_cr", 0.0) if gex_data else 0.0
    regime = gex_data.get("regime", "N/A") if gex_data else "N/A"
    if not gex_data or gex_data.get("status") != "ok" or regime == "N/A":
        gex_line = "- GEX: N/A"
    elif regime == "LONG":
        sign = "-" if total_gex_cr < 0 else "+"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr LONG gamma (rangebound)"
    elif regime == "SHORT":
        sign = "+" if total_gex_cr >= 0 else "-"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr SHORT gamma (trending)"
    elif regime == "NEUTRAL":
        sign = "+" if total_gex_cr >= 0 else "-"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr NEUTRAL gamma (no regime)"
    else:
        gex_line = f"- GEX: {regime}"

    # 2. RVOL line
    rvol_line = "- RVOL: N/A"
    regime_code = None
    try:
        import rvol_gate
        if rvol_val == "DEFAULT":
            rvol_val = rvol_gate.compute_rvol(target_date, latest_ts.time())
        if rvol_val is not None:
            regime_code = rvol_gate.get_rvol_regime(rvol_val)
            regime_label = (
                "DEAD ZONE"
                if regime_code == "DEAD_ZONE"
                else (
                    "TRAP ZONE"
                    if regime_code == "TRAP_ZONE"
                    else (
                        "TREND ZONE"
                        if regime_code == "TREND_ZONE"
                        else (
                            "EXTREME ZONE"
                            if regime_code == "EXTREME_ZONE"
                            else regime_code
                        )
                    )
                )
            )
            rvol_line = f"- RVOL: {rvol_val:.2f}× {regime_label}"
    except Exception as exc:
        logger.warning(f"Error computing RVOL for observation log: {exc}")

    # 3. IV line
    pct = iv_data.get("percentile") if iv_data else None
    label = iv_data.get("label") if iv_data else None
    status = iv_data.get("status") if iv_data else None
    if status == "building_history":
        iv_line = "- IV: building history"
    elif status == "feed_anomaly":
        polls = iv_data.get("consecutive_carries", 0)
        iv_line = f"- IV: unavailable (feed anomaly {polls} polls)"
    elif pct is not None and label:
        pct_round = int(round(pct))
        iv_line = f"- IV: {_ordinal_suffix(pct_round)} pct {label}"
    else:
        iv_line = "- IV: N/A"

    # 4. Floor / Ceiling line
    if s1_row is not None:
        pe_oi_l = int(round(float(s1_row.get("pe_oi", 0) or 0) * mult))
        floor_str = f"{int(round(float(s1_row['strike']))):,} PE [{pe_oi_l}L]"
    else:
        floor_str = "N/A"

    if r1_row is not None:
        ce_oi_l = int(round(float(r1_row.get("ce_oi", 0) or 0) * mult))
        ceil_str = f"{int(round(float(r1_row['strike']))):,} CE [{ce_oi_l}L]"
    else:
        ceil_str = "N/A"

    floor_ceil_line = f"- Floor: {floor_str} | Ceiling: {ceil_str}"

    # 5. Flow line
    if is_data_gap:
        flow_line = "- Flow: INVALID (data gap)"
    elif abs(d_pe_15m) >= 1000:
        pe_txt = f"Puts adding {d_pe_15m/1000:+.0f}k" if d_pe_15m > 0 else f"Puts leaving {d_pe_15m/1000:.0f}k"
    else:
        pe_txt = f"Puts adding {d_pe_15m:+.0f}" if d_pe_15m > 0 else (f"Puts leaving {d_pe_15m:+.0f}" if d_pe_15m < 0 else "Puts flat")

    if not is_data_gap:
        if abs(d_ce_15m) >= 1000:
            ce_txt = f"Calls adding {d_ce_15m/1000:+.0f}k" if d_ce_15m > 0 else f"Calls leaving {d_ce_15m/1000:.0f}k"
        else:
            ce_txt = f"Calls adding {d_ce_15m:+.0f}" if d_ce_15m > 0 else (f"Calls leaving {d_ce_15m:+.0f}" if d_ce_15m < 0 else "Calls flat")
        flow_line = f"- Flow: {pe_txt} | {ce_txt}"

    # 6. Floor weakening line (only if active and not a data gap)
    min_consec = int(rules.get("floor_weakening_consecutive_bars", 2))
    floor_weakening_line = None
    if not is_data_gap and floor_weakening_streak >= min_consec:
        floor_weakening_line = f"- Floor weakening: {_ordinal_suffix(floor_weakening_streak)} consecutive"

    # 7. Checklist line
    score = chk_data.get("score", 0) if chk_data else 0
    total_boxes = len(chk_data.get("boxes", [])) if chk_data else 6
    failed_tags = chk_data.get("failed_tags", []) if chk_data else []
    if score == total_boxes:
        verdict = "TRADE"
        fail_txt = ""
    elif score == total_boxes - 1:
        verdict = "CAUTION"
        fail_txt = f" ({', '.join(failed_tags)} failing)" if failed_tags else ""
    else:
        verdict = "WAIT"
        fail_txt = f" ({', '.join(failed_tags)} failing)" if failed_tags else ""

    checklist_line = f"- Checklist: {score}/{total_boxes} {verdict}{fail_txt}"

    # 8. Action line
    if is_pre_market:
        action_line_formatted = "- Action: PRE-MARKET — no signal (stale spot, no RVOL/GEX)"
    elif is_data_gap:
        action_line_formatted = "- Action: NO TRADE (data gap)"
    else:
        act_clean = action_line.replace("➡️ ", "").replace("⚠️ ", "").strip()
        if any(k in act_clean for k in ("NO TRADE", "NO DATA", "CHOP ZONE", "DEAD ZONE", "TRAP ZONE", "HOLD", "WAIT", "FREEZE", "EXPIRY", "EOD", "PRE-MARKET")):
            act_str = "NO TRADE"
        elif rvol_val is None and ("BUY" in act_clean):
            act_str = "NO TRADE"
        elif "CE BUY" in act_clean:
            act_str = "CE BUY"
        elif "PE BUY" in act_clean:
            act_str = "PE BUY"
        else:
            act_str = act_clean.split(" — ")[0].strip()

        action_line_formatted = f"- Action: {act_str}"

    # MJ MODE shadow logging (Item 2)
    # Present on all market-hours entries regardless of RVOL zone; absent on pre-market entries.
    mj_shadow_line = None
    shadow_enabled = rules.get("mj_mode", {}).get("shadow_logging", True)
    if not is_pre_market and shadow_enabled:
        if s1_row is not None:
            pe_oi_l = int(round(float(s1_row.get("pe_oi", 0) or 0) * mult))
            mj_floor_str = f"{int(round(float(s1_row['strike']))):,} PE [{pe_oi_l}L]"
        else:
            mj_floor_str = "N/A"

        if r1_row is not None:
            ce_oi_l = int(round(float(r1_row.get("ce_oi", 0) or 0) * mult))
            mj_ceil_str = f"{int(round(float(r1_row['strike']))):,} CE [{ce_oi_l}L]"
        else:
            mj_ceil_str = "N/A"

        if rvol_val is not None:
            zone_short = regime_code.replace("_ZONE", "").strip() if regime_code else "N/A"
            rvol_str = f"{rvol_val:.2f} {zone_short}"
        else:
            rvol_str = "N/A"

        mj_shadow_line = f"- mj_shadow: CE setup {mj_floor_str} | PE setup {mj_ceil_str} | RVOL {rvol_str}"

    # 9. Manual note and separator
    note_line = "> Note: [chart observation / what price did]"
    separator_line = "---"

    # Dominance & Structure lines (suppressed pre-market)
    dom_line = None
    struct_line = None
    if is_data_gap:
        dom_line = "- Dominance: INVALID (data gap)"
        struct_line = "- Structure: INVALID (data gap)"
    else:
        if dom_data is None:
            dom_data = getattr(build_briefing, "writer_dominance", None)
        if not is_pre_market and dom_data and dom_data.get("enabled", True):
            d_val = dom_data.get("obs_dominance")
            s_val = dom_data.get("obs_structure")
            if d_val:
                dom_line = f"- Dominance: {d_val}"
            if s_val:
                struct_line = f"- Structure: {s_val}"

    lines = [
        header,
    ]
    if tf:
        lines.append(f"<!-- tf: {tf} | src: live -->")
    if day_range_line:
        lines.append(day_range_line)

    if is_data_gap and gap_msg:
        gap_alert = f"- Alert: [DATA_GAP] {gap_msg}"
        alert_line = f"{gap_alert}\n{alert_line}" if alert_line else gap_alert

    lines.extend([
        gex_line,
        rvol_line,
        iv_line,
        floor_ceil_line,
        flow_line,
    ])
    if dom_line:
        lines.append(dom_line)
    if struct_line:
        lines.append(struct_line)
    if floor_weakening_line:
        lines.append(floor_weakening_line)
    if alert_line:
        lines.append(alert_line)
    if writer_lines_str:
        lines.append(f"- Writer lines: {writer_lines_str}")
    lines.extend([
        checklist_line,
        action_line_formatted,
    ])
    if mj_shadow_line:
        lines.append(mj_shadow_line)
    lines.extend([
        note_line,
        "",
        separator_line,
    ])
    return "\n".join(lines)


def append_observation_log(
    target_date: date,
    latest_ts: datetime,
    latest_spot: float,
    bias_word: str,
    gex_data: Optional[dict],
    iv_data: Optional[dict],
    s1_row: Optional[pd.Series],
    r1_row: Optional[pd.Series],
    mult: float,
    d_pe_15m: float,
    d_ce_15m: float,
    floor_weakening_streak: int,
    chk_data: Optional[dict],
    action_line: str,
    rules: dict,
    eval_snaps: Optional[List[Tuple[datetime, float, pd.DataFrame]]] = None,
    all_snaps: Optional[List[Tuple[datetime, float, pd.DataFrame]]] = None,
    latest_df: Optional[pd.DataFrame] = None,
    r1_strike: Optional[float] = None,
    r2_strike: Optional[float] = None,
    s1_strike: Optional[float] = None,
    s2_strike: Optional[float] = None,
    alert_line: Optional[str] = None,
    dom_data: Optional[dict] = None,
    is_data_gap: bool = False,
    gap_msg: Optional[str] = None,
    rvol_val: Any = "DEFAULT",
    tf: Optional[str] = None,
    writer_lines_str: Optional[str] = None,
) -> Optional[str]:
    """
    Appends observation entry to data/observations/YYYY-MM-DD.md.
    Creates the daily file if it does not exist.
    Inserts '== FREEZE END ==' at 13:15 and '== EOD SUMMARY ==' at 15:25.
    Idempotent: skips appending if ## HH:MM is already present.
    """
    if not rules.get("observations_enabled", True):
        return None

    obs_dir = Path(rules.get("observations_dir", "data/observations"))
    if not obs_dir.is_absolute():
        obs_dir = Path(__file__).resolve().parent / obs_dir
    obs_dir.mkdir(parents=True, exist_ok=True)

    date_str = target_date.strftime("%Y-%m-%d")
    file_path = obs_dir / f"{date_str}.md"

    if not file_path.exists():
        file_path.write_text(f"# NIFTY Daily Observation Log — {date_str}\n\n", encoding="utf-8")
        print(f"Created observation log: {file_path.resolve()}")

    content = file_path.read_text(encoding="utf-8")
    time_str = latest_ts.strftime("%H:%M")

    # Milestone 1: Freeze End separator (13:15)
    freeze_end_str = rules.get("observations_freeze_end_time", rules.get("freeze_end", "13:15"))
    try:
        f_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        if latest_ts.time() >= f_end_t and "== FREEZE END ==" not in content:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write("\n== FREEZE END ==\n\n")
            content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Error processing freeze end observation marker: {exc}")

    # Deduplication: If this exact minute timestamp and timeframe is already logged, do not duplicate
    pattern = rf"^##\s+{re.escape(time_str)}\b.*?(?=(?:\n##\s|\Z))"
    blocks_for_time = re.findall(pattern, content, flags=re.MULTILINE | re.DOTALL)
    if tf is not None:
        tag = f"tf: {tf}"
        if any(tag in blk for blk in blocks_for_time):
            return None
    else:
        if any("<!-- tf:" not in blk for blk in blocks_for_time):
            return None

    dr = compute_day_range(
        target_date,
        eval_snaps or [(latest_ts, latest_spot, latest_df if latest_df is not None else pd.DataFrame())],
        all_snaps or eval_snaps or [(latest_ts, latest_spot, latest_df if latest_df is not None else pd.DataFrame())],
        target_time=latest_ts.time(),
    )
    low_str = f"{dr.day_low:,.2f}".rstrip('0').rstrip('.') if dr.day_low % 1 else f"{int(dr.day_low):,}"
    high_str = f"{dr.day_high:,.2f}" if dr.day_high % 1 else f"{int(dr.day_high):,}"
    day_range_line = f"- Day range: Low {low_str} ({dr.day_low_time}) ↔ High {high_str} ({dr.day_high_time})"

    entry_text = format_observation_entry(
        target_date=target_date,
        latest_ts=latest_ts,
        latest_spot=latest_spot,
        bias_word=bias_word,
        gex_data=gex_data,
        iv_data=iv_data,
        s1_row=s1_row,
        r1_row=r1_row,
        mult=mult,
        d_pe_15m=d_pe_15m,
        d_ce_15m=d_ce_15m,
        floor_weakening_streak=floor_weakening_streak,
        chk_data=chk_data,
        action_line=action_line,
        rules=rules,
        alert_line=alert_line,
        rvol_val=rvol_val,
        day_range_line=day_range_line,
        dom_data=dom_data,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
        tf=tf,
        writer_lines_str=writer_lines_str,
    )

    if not _obs_is_repeat(content, entry_text, tf, latest_ts, rules):   # OBSFIX-2 change-only logging
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(entry_text + "\n\n")

    # Milestone 2: EOD Summary separator (15:25)
    eod_sum_str = rules.get("observations_eod_summary_time", rules.get("eod_summary_time", "15:25"))
    try:
        eod_t = datetime.strptime(eod_sum_str, "%H:%M").time()
        if latest_ts.time() >= eod_t and "== EOD SUMMARY ==" not in content and eval_snaps:
            eod_text = build_eod_summary(
                target_date=target_date,
                eval_snaps=eval_snaps,
                all_snaps=all_snaps or eval_snaps,
                rules=rules,
                latest_spot=latest_spot,
                latest_df=latest_df if latest_df is not None else eval_snaps[-1][2],
                r1_strike=r1_strike,
                r2_strike=r2_strike,
                s1_strike=s1_strike,
                s2_strike=s2_strike,
            )
            eod_block = f"== EOD SUMMARY ==\n{eod_text}\n\n"
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(eod_block)
    except Exception as exc:
        logger.warning(f"Error processing EOD summary observation marker: {exc}")

    return entry_text


def build_telegram_briefing(
    latest_ts: datetime,
    latest_spot: float,
    latest_df: pd.DataFrame,
    eff_dom: str,
    pcr_standing: float,
    pcr_trend: str,
    d_pe_15m: float,
    d_ce_15m: float,
    mult: float,
    rules: dict,
    eval_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    is_stale: bool = False,
    floor_weakening_streak: int = 0,
    day_low: Optional[float] = None,
    day_high: Optional[float] = None,
    s1_strike: Optional[float] = None,
    r1_strike: Optional[float] = None,
    is_expiry_day: bool = False,
    gex_tg_line: Optional[str] = None,
    max_pain: Optional[float] = None,
    iv_tg_line: Optional[str] = None,
    chk_tg_lines: Optional[List[str]] = None,
    institutional_context: Optional[str] = None,
    day_low_time: Optional[str] = None,
    day_high_time: Optional[str] = None,
    prior_day_levels: Optional[dict] = None,
    writer_dom_line: Optional[str] = None,
    writer_struct_line: Optional[str] = None,
    is_data_gap: bool = False,
    gap_msg: Optional[str] = None,
) -> str:
    """
    Constructs an ultra-concise, plain-word Telegram briefing (maximum 8-10 lines)
    designed for quick reading on a phone while on the move.
    """
    # ── Line 1: Header (Spot, Time, Bias) ──────────────────────────────────
    mkt_open_str = rules.get(
        "observations_market_open_time",
        rules.get("market_open_time", getattr(config, "MARKET_OPEN", dtime(9, 15)).strftime("%H:%M") if hasattr(config, "MARKET_OPEN") else "09:15")
    )
    try:
        mkt_open_t = datetime.strptime(str(mkt_open_str), "%H:%M").time()
    except Exception:
        mkt_open_t = dtime(9, 15)
    is_pre_market = (latest_ts.time() < mkt_open_t)

    is_rising = "Rising" in pcr_trend
    is_falling = "Falling" in pcr_trend

    eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
    try:
        eod_no_trade_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
        is_eod_no_trade = (latest_ts.time() >= eod_no_trade_t)
    except Exception:
        is_eod_no_trade = (latest_ts.time() >= dtime(15, 0))

    # Bias with CAUTION detection & Expiry Unwind override
    if is_pre_market:
        bias_word = "PRE-MARKET"
    elif is_data_gap:
        bias_word = "INVALID (data gap)"
    elif is_expiry_day and latest_ts.time() >= dtime(14, 0):
        bias_word = "EXPIRY UNWIND ⚠️"
    elif is_eod_no_trade or eff_dom == "EOD_UNWIND":
        bias_word = "EOD UNWIND ⚠️"
    elif eff_dom == "PE_DOMINANT":
        if is_falling and (1.0 <= pcr_standing <= 1.2):
            bias_word = "CAUTION ⚠️"
        else:
            bias_word = "BULLISH ▲"
    elif eff_dom == "CE_DOMINANT":
        if is_rising and (0.8 <= pcr_standing <= 1.0):
            bias_word = "CAUTION ⚠️"
        else:
            bias_word = "BEARISH ▼"
    else:
        if is_falling and (1.0 <= pcr_standing <= 1.2):
            bias_word = "CAUTION ⚠️"
        elif is_falling and (pcr_standing < 1.0):
            bias_word = "BEARISH ▼"
        elif is_rising and (pcr_standing > 1.2):
            bias_word = "BULLISH ▲"
        elif is_rising and (0.8 <= pcr_standing <= 1.0):
            bias_word = "CAUTION ⚠️"
        elif 0.9 <= pcr_standing <= 1.1 and (not is_rising and not is_falling):
            bias_word = "NEUTRAL →"
        else:
            bias_word = "NEUTRAL →"

    time_str = latest_ts.strftime("%H:%M")
    spot_str = f"{int(round(latest_spot)):,}"
    if is_pre_market:
        spot_str = f"{spot_str} (prev close)"
    line1 = f"📊 NIFTY {time_str} | Spot {spot_str} | {bias_word}"

    # Day Range Line (Item 1: Low 23,329 (09:15) ↔ High 23,466 (12:15))
    if day_low is None or day_high is None or day_low_time is None or day_high_time is None:
        dr = compute_day_range(latest_ts.date(), eval_snaps, eval_snaps, target_time=latest_ts.time())
        if day_low is None:
            day_low = dr.day_low
        if day_high is None:
            day_high = dr.day_high
        day_low_time = day_low_time or dr.day_low_time
        day_high_time = day_high_time or dr.day_high_time

    low_str = f"{day_low:,.2f}".rstrip('0').rstrip('.') if day_low % 1 else f"{int(day_low):,}"
    high_str = f"{day_high:,.2f}" if day_high % 1 else f"{int(day_high):,}"
    line1b = f"Day range: Low {low_str} ({day_low_time}) ↔ High {high_str} ({day_high_time})"

    # ── Line 2: Big Money Floor ────────────────────────────────────────────
    line2 = None
    pe_strike: Optional[float] = None
    pe_candidates = latest_df[
        (latest_df["strike"] >= latest_spot - 200.0) & (latest_df["strike"] <= latest_spot + 30.0)
    ].dropna(subset=["pe_oi"])

    if not pe_candidates.empty:
        max_pe_row = pe_candidates.loc[pe_candidates["pe_oi"].idxmax()]
        pe_strike = float(max_pe_row["strike"])
        pe_oi_l = int(round(float(max_pe_row["pe_oi"]) * mult))
        if latest_spot < pe_strike:
            line2 = f"⚠️ Floor at {int(round(pe_strike)):,} BREAKING — danger zone"
        else:
            line2 = f"🏦 Puts protecting {int(round(pe_strike)):,} ({pe_oi_l}L) — floor holding"

    # ── Line 3: Ceiling ────────────────────────────────────────────────────
    line3 = None
    ce_strike: Optional[float] = None
    ce_candidates = latest_df[
        (latest_df["strike"] >= latest_spot) & (latest_df["strike"] <= latest_spot + 200.0)
    ].dropna(subset=["ce_oi"])

    if not ce_candidates.empty:
        max_ce_row = ce_candidates.loc[ce_candidates["ce_oi"].idxmax()]
        ce_strike = float(max_ce_row["strike"])
        ce_oi_l = int(round(float(max_ce_row["ce_oi"]) * mult))
        dist_ce = int(round(ce_strike - latest_spot))
        if dist_ce <= 20:
            line3 = f"⚠️ CEILING at {int(round(ce_strike)):,} RIGHT ABOVE — resistance strong"
        else:
            line3 = f"🚧 {int(round(ce_strike)):,} calls blocking ({ce_oi_l}L) — {dist_ce} pts above"

    # Prior-day levels line (Item 2)
    line3b = None
    if prior_day_levels:
        pdh = prior_day_levels.get("PDH")
        pdl = prior_day_levels.get("PDL")
        pdc = prior_day_levels.get("PDC")
        if pdh and pdl and pdc:
            exp_warn = ""
            if "date" in prior_day_levels:
                try:
                    prev_d = datetime.strptime(str(prior_day_levels["date"]), "%Y-%m-%d").date()
                    if was_prev_session_expiry(prev_d, rules):
                        exp_warn = " ⚠️ (from expiry session — expect wider range / false breaks)"
                except Exception:
                    pass
            line3b = f"📍 Prior-day: PDH {int(round(pdh)):,} | PDL {int(round(pdl)):,} | PDC {int(round(pdc)):,}{exp_warn}"

    # ── Line 4: PCR Summary ────────────────────────────────────────────────
    if pcr_standing > 1.5 and is_rising:
        ratio_phrase = "Ratio strongly bullish ↑↑"
        trend_txt = "rising"
    elif pcr_standing > 1.0 and is_rising:
        ratio_phrase = "Ratio turning bullish ↑"
        trend_txt = "rising"
    elif pcr_standing < 0.7 and is_falling:
        ratio_phrase = "Ratio strongly bearish ↓↓"
        trend_txt = "falling"
    elif pcr_standing < 1.0 and is_falling:
        ratio_phrase = "Ratio turning bearish ↓"
        trend_txt = "falling"
    elif 1.0 <= pcr_standing <= 1.2 and is_falling:
        ratio_phrase = "Ratio weakening ↓"
        trend_txt = "falling"
    elif 0.8 <= pcr_standing <= 1.0 and is_rising:
        ratio_phrase = "Ratio recovering ↑"
        trend_txt = "rising"
    elif 0.9 <= pcr_standing <= 1.1:
        ratio_phrase = "Ratio balanced →"
        trend_txt = "flat" if (not is_rising and not is_falling) else ("rising" if is_rising else "falling")
    elif pcr_standing > 1.2:
        ratio_phrase = "Ratio bullish"
        trend_txt = "rising" if is_rising else ("falling" if is_falling else "flat")
    elif pcr_standing < 0.8:
        ratio_phrase = "Ratio bearish"
        trend_txt = "falling" if is_falling else ("rising" if is_rising else "flat")
    else:
        ratio_phrase = "Ratio balanced →"
        trend_txt = "flat"

    line4 = f"📈 {ratio_phrase} ({pcr_standing:.2f} {trend_txt})"

    # ── Line 5: Last 15 Min Flow ───────────────────────────────────────────
    if abs(d_pe_15m) >= 1000:
        pe_flow_txt = f"Puts adding {d_pe_15m/1000:+.0f}k" if d_pe_15m > 0 else f"Puts leaving {d_pe_15m/1000:.0f}k"
    else:
        pe_flow_txt = "Puts flat"

    if abs(d_ce_15m) >= 1000:
        ce_flow_txt = f"Calls adding {d_ce_15m/1000:+.0f}k" if d_ce_15m > 0 else f"Calls leaving {d_ce_15m/1000:.0f}k"
    else:
        ce_flow_txt = "Calls flat"

    if d_pe_15m > 0 and d_ce_15m < 0:
        flow_desc = "bullish pressure"
    elif d_pe_15m > 0 and d_ce_15m > 0:
        flow_desc = "market undecided"
    elif d_pe_15m < 0 and d_ce_15m < 0:
        flow_desc = "no conviction"
    elif d_pe_15m < 0 and d_ce_15m > 0:
        flow_desc = "bearish pressure"
    elif d_pe_15m > 0 and abs(d_ce_15m) < 1000:
        flow_desc = "bullish pressure"
    elif d_pe_15m < 0 and abs(d_ce_15m) < 1000:
        flow_desc = "bearish pressure"
    elif abs(d_pe_15m) < 1000 and d_ce_15m > 0:
        flow_desc = "bearish pressure"
    elif abs(d_pe_15m) < 1000 and d_ce_15m < 0:
        flow_desc = "bullish pressure"
    else:
        flow_desc = "market balanced"

    line5 = f"🔄 {pe_flow_txt} | {ce_flow_txt} — {flow_desc}"

    # ── Line 6: Active Wall Break / Sweep Warning ──────────────────────────
    line6 = None
    major_thresh = rules.get("major_wall_threshold", 75000.0)
    min_reclaim_pts = float(
        rules.get("alerts", {}).get(
            "min_false_breakdown_reclaim_pts",
            rules.get("min_false_breakdown_reclaim_pts", 15.0)
        )
    )
    eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
    try:
        eod_no_trade_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
        is_eod_no_trade = (latest_ts.time() >= eod_no_trade_t)
    except Exception:
        is_eod_no_trade = (latest_ts.time() >= dtime(15, 0))

    def is_valid_false_breakdown(snaps: list, level: float) -> bool:
        if is_eod_no_trade:
            return False
        if len(snaps) < 2:
            return False
        n_t = snaps[-1][0]
        sw_window = [s for s in snaps if 0 <= (n_t - s[0]).total_seconds() <= 360]
        if len(sw_window) < 3 and len(snaps) >= 3:
            sw_window = snaps[-3:]
        sw_low = min(s[1] for s in sw_window)
        sw_pts = level - sw_low
        sw_min = float(rules.get("sweep_min_pts", 2.0))
        sw_max = float(rules.get("sweep_max_pts", 25.0))
        if not (sw_min <= sw_pts <= sw_max):
            return False
        if latest_spot < (level + min_reclaim_pts):
            return False
        return check_trend_higher_lows(snaps, n_t)

    # Check S1 break and reclaim (Finding 3)
    if not is_eod_no_trade and s1_strike is not None and is_valid_false_breakdown(eval_snaps, s1_strike):
        line6 = f"🪤 FAKE BREAKDOWN at {int(round(s1_strike)):,} — expect bounce UP"

    if line6 is None and not is_eod_no_trade:
        major_walls = latest_df[
            (latest_df["ce_oi"] >= major_thresh) | (latest_df["pe_oi"] >= major_thresh)
        ]
        if len(eval_snaps) >= 2:
            prev_spot = eval_snaps[-2][1]
            for _, w_row in major_walls.iterrows():
                w_strike = float(w_row["strike"])
                w_pe = float(w_row.get("pe_oi", 0) or 0)
                w_ce = float(w_row.get("ce_oi", 0) or 0)
                if w_pe >= major_thresh:
                    if is_valid_false_breakdown(eval_snaps, w_strike):
                        line6 = f"🪤 FAKE BREAKDOWN at {int(round(w_strike)):,} — expect bounce UP"
                        break
                    elif prev_spot >= w_strike and latest_spot < (w_strike - 10.0):
                        line6 = f"🚨 {int(round(w_strike)):,} WALL BROKEN — big move possible"
                        break
                if w_ce >= major_thresh:
                    if prev_spot > w_strike and latest_spot <= w_strike:
                        line6 = f"🪤 FAKE BREAKOUT at {int(round(w_strike)):,} — expect rejection DOWN"
                        break
                    elif prev_spot <= w_strike and latest_spot > (w_strike + 10.0):
                        line6 = f"🚨 {int(round(w_strike)):,} WALL BROKEN — big move possible"
                        break

    if line6 is None and not is_eod_no_trade and ce_strike is not None and latest_spot > ce_strike:
        line6 = f"🚨 {int(round(ce_strike)):,} CEILING BROKEN — breakout in progress"

    # ── Line 7: Freeze Window Warning ──────────────────────────────────────
    line7 = None
    freeze_start_str = rules.get("freeze_start", "11:30")
    freeze_end_str = rules.get("freeze_end", "13:15")
    try:
        f_start = datetime.strptime(freeze_start_str, "%H:%M").time()
        f_end = datetime.strptime(freeze_end_str, "%H:%M").time()
        now_t = latest_ts.time()
        is_in_freeze = (f_start <= now_t <= f_end)
    except Exception:
        is_in_freeze = False

    if is_in_freeze:
        line7 = "⏸ FREEZE WINDOW — watch only, no trades"

    # ── Line 8: Action ─────────────────────────────────────────────────────
    if is_pre_market:
        line8 = "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)"
    elif is_stale:
        line8 = f"➡️ NO DATA since {latest_ts.strftime('%H:%M')} — cannot read market"
    elif is_expiry_day and latest_ts.time() >= dtime(14, 0):
        line8 = "➡️ EXPIRY UNWIND — OI data unreliable"
    elif is_eod_no_trade:
        line8 = f"➡️ EOD — no new trades after {eod_no_trade_str}"
    elif is_in_freeze:
        line8 = f"➡️ FREEZE WINDOW — no entry until {freeze_end_str}"
    elif line6 and "FAKE BREAKDOWN" in line6:
        line8 = "➡️ CE BUY SIGNAL — confirm 8 EMA on chart"
    elif line6 and "FAKE BREAKOUT" in line6:
        line8 = "➡️ PE BUY SIGNAL — confirm 8 EMA on chart"
    elif "CAUTION" in bias_word:
        line8 = "➡️ NO TRADE — caution, ratio falling"
    elif eff_dom == "PE_DOMINANT" and (pe_strike is not None and latest_spot >= pe_strike):
        line8 = "➡️ CE BUY SIGNAL — confirm 8 EMA on chart"
    elif eff_dom == "CE_DOMINANT" and (ce_strike is not None and latest_spot <= ce_strike):
        line8 = "➡️ PE BUY SIGNAL — confirm 8 EMA on chart"
    else:
        line8 = "➡️ NO TRADE — wait for clear direction"

    if is_data_gap:
        line5 = "🔄 Flow: INVALID (data gap)"
        line6 = None
        writer_dom_line = "⚖️ Dominance: INVALID (data gap)"
        writer_struct_line = None
        resample_mins = int(rules.get("resample_minutes", 3))
        next_ts = latest_ts + timedelta(minutes=resample_mins)
        line8 = f"➡️ WAIT — data gap, signals invalid until {next_ts.strftime('%H:%M')}"

    # Strict post-15:00 suppression: never show CE BUY or PE BUY
    if is_eod_no_trade and ("BUY" in line8 or "BUY SIGNAL" in line8):
        line8 = f"➡️ EOD — no new trades after {eod_no_trade_str}"

    # Strict pre-market suppression: never show CE BUY or PE BUY before market open
    if is_pre_market:
        line8 = "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)"

    # RVOL Gate check on line8 & display integration
    rvol_tg_line = None
    rvol_flip_line = None
    rvol_disp = None
    try:
        import rvol_gate
        rvol_disp = rvol_gate.get_rvol_display(latest_ts.date(), latest_ts.time())
        gate_dec = rvol_gate.get_gate_decision(latest_ts.date(), latest_ts.time())
        rvol_tg_line = rvol_disp.get("telegram_line")

        if is_pre_market:
            line8 = "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)"
        elif is_data_gap:
            resample_mins = int(rules.get("resample_minutes", 3))
            next_ts = latest_ts + timedelta(minutes=resample_mins)
            line8 = f"➡️ WAIT — data gap, signals invalid until {next_ts.strftime('%H:%M')}"
        elif gate_dec.get("status") == "SUPPRESSED" and ("BUY" in line8 or "BUY SIGNAL" in line8):
            if gate_dec.get("regime") == "DEAD_ZONE":
                line8 = "⚠️ DEAD ZONE (RVOL < 0.85) — no trade. Wait for RVOL >= 1.30 before any entry."
            else:
                # TRAP ZONE — add MJ MODE guidance
                mj_cfg = rules.get("mj_mode", {})
                mj_enabled = mj_cfg.get("enabled", True)
                mj_lot = int(mj_cfg.get("lot_size_mj", 1))
                mj_target = int(mj_cfg.get("target_pts", 25))
                mj_no_trade = mj_cfg.get("no_trade_after", "14:00")
                if mj_enabled:
                    # Use pe_strike / ce_strike already computed above
                    try:
                        floor_str = f"{int(round(pe_strike)):,} PE [{pe_oi_l}L]" if pe_strike else "see S1"
                        ceil_str = f"{int(round(ce_strike)):,} CE [{ce_oi_l}L]" if ce_strike else "see R1"
                        ce_tgt_str = f"{int(round(ce_strike)):,}" if ce_strike else "R1"
                        pe_tgt_str = f"{int(round(pe_strike)):,}" if pe_strike else "S1"
                    except Exception:
                        floor_str = "see S1"
                        ceil_str = "see R1"
                        ce_tgt_str = "R1"
                        pe_tgt_str = "S1"
                    line8 = (
                        f"🎯 MJ MODE ACTIVE (RVOL trap zone — scanner suppressed)\n"
                        f"Use price action rules ({mj_lot} lot only):\n"
                        f"CE setup: bounce at {floor_str} + 8 EMA up → target {ce_tgt_str} (+{mj_target}pts)\n"
                        f"PE setup: reject at {ceil_str} + 8 EMA down → target {pe_tgt_str} (-{mj_target}pts)\n"
                        f"⚠️ Stop: below/above 8 EMA | No trade after {mj_no_trade}"
                    )
                else:
                    line8 = "⚠️ TRAP ZONE (RVOL 0.85-1.30) — no trade (false breakouts). Wait for RVOL >= 1.30."
        elif rvol_disp.get("value") is None and ("BUY" in line8 or "BUY SIGNAL" in line8):
            line8 = "➡️ NO TRADE — RVOL N/A (cannot confirm volume expansion)"
        elif rvol_disp.get("regime") == "EXTREME_ZONE" and ("BUY" in line8 or "BUY SIGNAL" in line8):
            line8 = f"{line8} | ⚡ EXTREME ZONE — watch reversal."
        elif rvol_disp.get("trend_day") and ("BUY" in line8 or "BUY SIGNAL" in line8):
            line8 = f"{line8} | 🚀 TREND ZONE — hold to full target."

        if is_pre_market:
            line8 = "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)"
        elif is_data_gap:
            resample_mins = int(rules.get("resample_minutes", 3))
            next_ts = latest_ts + timedelta(minutes=resample_mins)
            line8 = f"➡️ WAIT — data gap, signals invalid until {next_ts.strftime('%H:%M')}"

        flip_info = rvol_gate.find_regime_flip(latest_ts.date())
        if flip_info and latest_ts.time() >= dtime(12, 30):
            flip_t = datetime.strptime(flip_info["flip_time"], "%H:%M").time()
            if flip_t <= latest_ts.time():
                rvol_flip_line = f"RVOL regime flip at {flip_info['flip_time']}: {flip_info.get('from', 'CHOP/TRAP')} → {flip_info['to']} — signals re-enabled"
    except Exception as exc:
        logger.warning(f"RVOL gate integration error: {exc}")

    # ── Assemble ───────────────────────────────────────────────────────────
    min_consec = rules.get("floor_weakening_consecutive_bars", 2)
    floor_weakening_line = format_floor_weakening_warning(floor_weakening_streak, min_consec) if not is_data_gap else None

    body_lines: List[str] = []
    if institutional_context:
        body_lines.append(institutional_context)
        body_lines.append("")
    if is_expiry_day:
        body_lines.append("📅 EXPIRY DAY — max pain magnet active")
    body_lines.extend([line1, line1b])
    if is_data_gap and gap_msg:
        body_lines.append(gap_msg)
    body_lines.append("")

    if gex_tg_line:
        body_lines.append(gex_tg_line)
    if rvol_tg_line and rvol_tg_line != "📊 RVOL: N/A":
        body_lines.append(rvol_tg_line)
    if rvol_flip_line:
        body_lines.append(f"ℹ️ {rvol_flip_line}")
    if iv_tg_line:
        body_lines.append(iv_tg_line)
    if is_expiry_day and latest_ts.time() >= dtime(13, 0) and max_pain is not None:
        body_lines.append(f"🎯 MAX PAIN: {int(round(max_pain)):,} (informational only — no edge on Nifty)")
    if gex_tg_line or rvol_tg_line or iv_tg_line or (is_expiry_day and latest_ts.time() >= dtime(13, 0) and max_pain is not None):
        body_lines.append("")

    floor_ceil_ratio = [ln for ln in [line2, line3, line3b, line4] if ln]
    if floor_ceil_ratio:
        body_lines.extend(floor_ceil_ratio)
        body_lines.append("")
    if line5:
        body_lines.append(line5)
        if writer_dom_line:
            body_lines.append(writer_dom_line)
        if writer_struct_line:
            body_lines.append(writer_struct_line)
        if floor_weakening_line:
            body_lines.append(floor_weakening_line)
        body_lines.append("")
    if line6 or line7:
        if line6:
            body_lines.append(line6)
        if line7:
            body_lines.append(line7)
        body_lines.append("")
    body_lines.append(line8)
    if chk_tg_lines:
        body_lines.append("")
        body_lines.extend(chk_tg_lines)

    build_telegram_briefing.last_bias_word = bias_word
    build_telegram_briefing.last_action_line = line8
    return "\n".join(body_lines).strip()


def build_briefing(target_date: date, target_time: Optional[dtime] = None, append_obs: bool = False) -> str:
    """
    Constructs the complete text briefing for the specified date and optional time cutoff.
    If no snapshots exist for target_date, returns 'Market closed or no data yet'.
    append_obs: If True, appends to daily observation log (only called by scheduler / live poller).
    """
    build_briefing.last_snapshot_ts = None
    build_briefing.last_is_stale = True
    build_briefing.telegram_briefing = None
    build_briefing.iv_data = None
    build_briefing.checklist_data = None
    build_briefing.institutional_context = None
    build_briefing.writer_dominance = None
    build_briefing.writer_gap = None
    rules = load_briefing_rules()
    folder = config.SNAPSHOT_DIR / target_date.strftime("%Y-%m-%d")
    if not folder.exists() or not any(folder.glob("*.parquet")):
        return "Market closed or no data yet"

    all_snaps = store.load_day(target_date)
    if not all_snaps:
        return "Market closed or no data yet"

    if target_time is not None:
        all_snaps = [s for s in all_snaps if s[0].time() <= target_time]
        if not all_snaps:
            return "Market closed or no data yet"

    mkt_snaps = oi_timeline.filter_market_hours(all_snaps)
    eval_snaps = mkt_snaps if mkt_snaps else all_snaps

    if not eval_snaps:
        return "Market closed or no data yet"

    latest_ts, latest_spot, latest_df = eval_snaps[-1]
    step = rules.get("step", 50.0)

    # ── Data Gap Detection (Rule 4: strategy1_rules.json / data_gap) ──────
    max_gap_sec = float(rules.get("data_gap", {}).get("max_gap_seconds", rules.get("max_gap_seconds", 180.0)))
    is_data_gap = False
    gap_msg = None
    gap_prev_ts = None
    gap_mins = 0

    if len(eval_snaps) >= 2:
        gap_prev_ts = eval_snaps[-2][0]
        actual_gap_sec = (latest_ts - gap_prev_ts).total_seconds()
        if actual_gap_sec > max_gap_sec:
            is_data_gap = True
            gap_mins = int(round(actual_gap_sec / 60.0))
    elif len(eval_snaps) == 1:
        mkt_open_t = dtime(9, 15)
        if latest_ts.time() > mkt_open_t:
            open_dt = datetime.combine(latest_ts.date(), mkt_open_t)
            open_gap_sec = (latest_ts - open_dt).total_seconds()
            if open_gap_sec > max_gap_sec:
                is_data_gap = True
                gap_prev_ts = open_dt
                gap_mins = int(round(open_gap_sec / 60.0))

    if is_data_gap:
        prev_str = gap_prev_ts.strftime("%H:%M") if gap_prev_ts else "??:??"
        curr_str = latest_ts.strftime("%H:%M")
        resample_mins = int(rules.get("resample_minutes", 3))
        next_ts = latest_ts + timedelta(minutes=resample_mins)
        next_str = next_ts.strftime("%H:%M")
        gap_msg = f"⚠️ DATA GAP {prev_str}-{curr_str} ({gap_mins} min) — flow, dominance and streak signals invalid until {next_str}"

    build_briefing.data_gap = gap_msg
    build_briefing.is_data_gap = is_data_gap

    # ── Expiry Day Resolution ─────────────────────────────────────────────
    expiry_str = latest_df.attrs.get("expiry")
    if not expiry_str and "expiry" in latest_df.columns:
        valid_exp = latest_df["expiry"].dropna()
        if not valid_exp.empty:
            expiry_str = str(valid_exp.iloc[0])
    if not expiry_str:
        meta_files = sorted(folder.glob("*.meta.json"))
        if meta_files:
            try:
                m_data = json.loads(meta_files[-1].read_text())
                expiry_str = m_data.get("expiry")
            except Exception:
                pass

    exp_date = parse_expiry_date(expiry_str)
    is_expiry_day = (exp_date is not None and target_date == exp_date)

    exp_dt = None
    if exp_date is not None:
        exp_dt = datetime.combine(exp_date, dtime(15, 30, 0))
    elif expiry_str:
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                exp_dt = datetime.strptime(str(expiry_str).strip(), fmt).replace(hour=15, minute=30, second=0)
                break
            except ValueError:
                pass

    if ("ce_gamma" not in latest_df.columns or latest_df["ce_gamma"].isna().all()) and exp_dt is not None:
        try:
            import greeks
            latest_df = greeks.add_greeks(latest_df, latest_spot, expiry=exp_dt, now=latest_ts)
        except Exception as e:
            logger.warning(f"Failed to attach greeks: {e}")

    gex_data = compute_gex_metrics(latest_df, latest_spot, rules)
    iv_data = compute_iv_context(latest_df, latest_spot, latest_ts, target_date, rules)
    max_pain = compute_max_pain(latest_df)

    max_pain_str = None
    if max_pain is not None:
        diff_pts = latest_spot - max_pain
        if diff_pts > 0:
            mp_note = f"{int(round(diff_pts))} pts above"
        elif diff_pts < 0:
            mp_note = f"{abs(int(round(diff_pts)))} pts below"
        else:
            mp_note = "spot is at max pain"
        max_pain_str = f"{int(round(max_pain)):,} (spot {int(round(latest_spot)):,} — {mp_note}) (informational only — no edge on Nifty)"

    # ATM Strike Calculation: nearest to current spot
    atm = float(round(latest_spot / 50.0) * 50.0)
    if "strike" in latest_df.columns:
        avail_strikes = sorted(latest_df["strike"].unique())
        if atm not in avail_strikes and avail_strikes:
            atm = float(min(avail_strikes, key=lambda s: abs(s - latest_spot)))

    lot_size = rules.get("display_lot_size", 65)
    mult = (lot_size / 100000.0) if rules.get("display_units") == "lakhs" else (1.0 / 100000.0)

    # ATM Row & Dual PCR Values
    atm_row = latest_df[latest_df["strike"] == atm]
    if not atm_row.empty:
        ar = atm_row.iloc[0]
        atm_pe_oi = float(ar.get("pe_oi", 0) or 0)
        atm_ce_oi = float(ar.get("ce_oi", 0) or 0)
        atm_pe_chg = float(ar.get("pe_chg_oi", 0) or 0)
        atm_ce_chg = float(ar.get("ce_chg_oi", 0) or 0)
        pcr_standing = (atm_pe_oi / atm_ce_oi) if atm_ce_oi > 0 else 0.0
        pcr_flow_str = "INVALID (data gap)" if is_data_gap else (f"{atm_pe_chg / atm_ce_chg:.2f}" if atm_ce_chg > 0 else "N/A")
    else:
        pcr_standing = 0.0
        pcr_flow_str = "INVALID (data gap)" if is_data_gap else "N/A"

    # Nearest Heavy Strike (within 100 pts of spot with highest total OI)
    near_heavy = latest_df[
        (latest_df["strike"] >= latest_spot - 100.0) & (latest_df["strike"] <= latest_spot + 100.0)
    ].copy()
    if near_heavy.empty:
        near_heavy = latest_df[
            (latest_df["strike"] >= atm - 100.0) & (latest_df["strike"] <= atm + 100.0)
        ].copy()

    if not near_heavy.empty and "ce_oi" in near_heavy.columns and "pe_oi" in near_heavy.columns:
        near_heavy["total_oi"] = near_heavy["ce_oi"].fillna(0) + near_heavy["pe_oi"].fillna(0)
        heavy_row = near_heavy.loc[near_heavy["total_oi"].idxmax()]
        heavy_strike = float(heavy_row["strike"])
        heavy_pe = float(heavy_row.get("pe_oi", 0) or 0)
        heavy_ce = float(heavy_row.get("ce_oi", 0) or 0)
        pcr_heavy = (heavy_pe / heavy_ce) if heavy_ce > 0 else 0.0
    else:
        heavy_strike = atm
        pcr_heavy = pcr_standing

    # Writers Ratio across near strikes (ATM ± 100 pts)
    near_df = latest_df[(latest_df["strike"] >= atm - 100.0) & (latest_df["strike"] <= atm + 100.0)]
    pe_chg_near = float(near_df["pe_chg_oi"].sum()) if "pe_chg_oi" in near_df.columns else 0.0
    ce_chg_near = float(near_df["ce_chg_oi"].sum()) if "ce_chg_oi" in near_df.columns else 0.0

    early_until_str = rules.get("writers_ratio_early_session_until", "10:45")
    early_thresh = rules.get("writers_ratio_early_session_threshold", 1.3)
    base_pe_thresh = rules.get("writers_ratio_pe_threshold", 1.5)
    ce_ratio_thresh = rules.get("writers_ratio_ce_threshold", 0.67)

    try:
        early_until_t = datetime.strptime(early_until_str, "%H:%M").time()
        pe_ratio_thresh = early_thresh if latest_ts.time() <= early_until_t else base_pe_thresh
    except Exception as e:
        logger.warning(f"Could not parse early session time {early_until_str}: {e}")
        pe_ratio_thresh = base_pe_thresh

    if is_data_gap:
        writers_ratio_line = f"{'WRITERS RATIO:':<22}INVALID (data gap)"
    elif ce_chg_near > 0:
        w_ratio = pe_chg_near / ce_chg_near
        if w_ratio >= pe_ratio_thresh:
            w_desc = "PE dominant — floor defending"
        elif w_ratio <= ce_ratio_thresh:
            w_desc = "CE dominant — ceiling defending"
        else:
            w_desc = "Balanced flow"
        writers_ratio_line = f"{'WRITERS RATIO:':<22}{w_ratio:.2f}x  ({w_desc})"
    elif ce_chg_near <= 0 and pe_chg_near > 0:
        writers_ratio_line = f"{'WRITERS RATIO:':<22}>10.0x  (PE dominant — floor defending)"
    elif ce_chg_near > 0 and pe_chg_near <= 0:
        writers_ratio_line = f"{'WRITERS RATIO:':<22}0.00x  (CE dominant — ceiling defending)"
    else:
        writers_ratio_line = f"{'WRITERS RATIO:':<22}N/A  (Balanced flow)"

    # ── Max Institutional Walls (Spot ± 500 pts) ───────────────────────────
    max_p_row, max_c_row = compute_institutional_walls(latest_df, latest_spot, cap_pts=500.0)

    if max_c_row is not None:
        max_c_strike = float(max_c_row["strike"])
        max_c_oi = float(max_c_row["ce_oi"])
        dist_res = max_c_strike - latest_spot
        max_res_line = f"  MAX RESISTANCE: {max_c_strike:,.0f} CE [{max_c_oi * mult:.1f}L] \u2014 {int(round(dist_res))} pts above spot"
    else:
        max_res_line = "  MAX RESISTANCE: None in range"

    if max_p_row is not None:
        max_p_strike = float(max_p_row["strike"])
        max_p_oi = float(max_p_row["pe_oi"])
        dist_sup = latest_spot - max_p_strike
        max_sup_line = f"  MAX SUPPORT:    {max_p_strike:,.0f} PE [{max_p_oi * mult:.1f}L] \u2014 {int(round(dist_sup))} pts below spot"
    else:
        max_sup_line = "  MAX SUPPORT:    None in range"


    inst_wall_lines = [
        "MAX INSTITUTIONAL WALLS",
        max_res_line,
        max_sup_line,
    ]

    # ── Support / Resistance (OI Walls) ────────────────────────────────────
    # Strikes at or above spot with highest CE OI = Resistance (R1, R2 by distance from spot)
    # Strikes at or below spot with highest PE OI = Support (S1, S2 by distance from spot)
    w = analytics.window(latest_df, latest_spot, n=getattr(config, "WINDOW_STRIKES", 5) + 3)
    above_spot = w[w["strike"] >= latest_spot].dropna(subset=["ce_oi"])
    if len(above_spot) < 2:
        above_spot = latest_df[latest_df["strike"] >= latest_spot].dropna(subset=["ce_oi"])

    below_spot = w[w["strike"] <= latest_spot].dropna(subset=["pe_oi"])
    if len(below_spot) < 2:
        below_spot = latest_df[latest_df["strike"] <= latest_spot].dropna(subset=["pe_oi"])

    ce_sorted = above_spot.nlargest(2, "ce_oi").sort_values(by="strike", ascending=True)
    r1_row = ce_sorted.iloc[0] if len(ce_sorted) > 0 else None
    r2_row = ce_sorted.iloc[1] if len(ce_sorted) > 1 else r1_row

    pe_sorted = below_spot.nlargest(2, "pe_oi").sort_values(by="strike", ascending=False)
    s1_row = pe_sorted.iloc[0] if len(pe_sorted) > 0 else None
    s2_row = pe_sorted.iloc[1] if len(pe_sorted) > 1 else s1_row

    # Major Wall: strongest institutional anchor outside immediate levels
    # Defined as any strike where OI > major_wall_threshold (default 75,000 contracts)
    major_thresh = rules.get("major_wall_threshold", 75000.0)
    major_wall_txt = None

    excluded_strikes = set()
    if r1_row is not None:
        excluded_strikes.add(float(r1_row["strike"]))
    if r2_row is not None:
        excluded_strikes.add(float(r2_row["strike"]))
    if s1_row is not None:
        excluded_strikes.add(float(s1_row["strike"]))
    if s2_row is not None:
        excluded_strikes.add(float(s2_row["strike"]))
    for i in [-2, -1, 0, 1, 2]:
        excluded_strikes.add(atm + i * step)

    wall_candidates = []
    for _, r in latest_df.iterrows():
        stk = float(r["strike"])
        p_oi = float(r.get("pe_oi", 0) or 0)
        c_oi = float(r.get("ce_oi", 0) or 0)
        if stk not in excluded_strikes:
            if p_oi > major_thresh:
                wall_candidates.append({
                    "strike": stk,
                    "side": "PE",
                    "oi": p_oi,
                    "dist": abs(latest_spot - stk),
                    "dir": "below" if latest_spot >= stk else "above",
                })
            if c_oi > major_thresh:
                wall_candidates.append({
                    "strike": stk,
                    "side": "CE",
                    "oi": c_oi,
                    "dist": abs(latest_spot - stk),
                    "dir": "above" if stk >= latest_spot else "below",
                })

    if not wall_candidates:
        # Fallback: only exclude R1, R2, S1, S2
        fallback_excluded = {
            float(x["strike"]) for x in [r1_row, r2_row, s1_row, s2_row] if x is not None
        }
        for _, r in latest_df.iterrows():
            stk = float(r["strike"])
            p_oi = float(r.get("pe_oi", 0) or 0)
            c_oi = float(r.get("ce_oi", 0) or 0)
            if stk not in fallback_excluded:
                if p_oi > major_thresh:
                    wall_candidates.append({
                        "strike": stk,
                        "side": "PE",
                        "oi": p_oi,
                        "dist": abs(latest_spot - stk),
                        "dir": "below" if latest_spot >= stk else "above",
                    })
                if c_oi > major_thresh:
                    wall_candidates.append({
                        "strike": stk,
                        "side": "CE",
                        "oi": c_oi,
                        "dist": abs(latest_spot - stk),
                        "dir": "above" if stk >= latest_spot else "below",
                    })

    if wall_candidates:
        wall_candidates.sort(key=lambda x: x["dist"])
        best_wall = wall_candidates[0]
        pts_int = int(abs(latest_spot - best_wall["strike"]))
        major_wall_txt = (
            f"  MAJOR WALL: {int(best_wall['strike'])} {best_wall['side']} "
            f"[{best_wall['oi'] / 1000:.1f}L] \u2014 {pts_int} pts {best_wall['dir']} spot"
        )


    # ── Timeline & Dominance Evaluation ───────────────────────────────────
    tl = oi_timeline.evaluate_strike_timeline(
        mkt_snaps=eval_snaps,
        strike=atm,
        sess_date=target_date,
        rules=rules,
    )

    if tl and tl.series:
        latest_pt = tl.series[-1]
        current_state = latest_pt.dominance
        duration_mins = latest_pt.state_duration_minutes

        # Walk backward to find the previous distinct state
        prev_state = None
        for pt in reversed(tl.series[:-1]):
            if pt.dominance != current_state:
                prev_state = pt.dominance
                break

        if prev_state:
            regime_str = f"{prev_state} → {current_state} ({duration_mins} min)"
        else:
            regime_str = f"{current_state} ({duration_mins} min)"

        # PCR trend (current standing vs 15 minutes ago)
        target_15m_ts = latest_ts - timedelta(minutes=15)
        snap_15m = None
        if not is_data_gap:
            for i in range(len(eval_snaps) - 1, 0, -1):
                s_curr = eval_snaps[i]
                s_prev = eval_snaps[i - 1]
                if (s_curr[0] - s_prev[0]).total_seconds() > max_gap_sec:
                    snap_15m = s_curr if s_curr[0] <= target_15m_ts else None
                    break
                if s_prev[0] <= target_15m_ts:
                    snap_15m = s_prev
                    break
            if snap_15m is None and len(eval_snaps) > 1:
                has_any_gap = any(
                    (eval_snaps[j][0] - eval_snaps[j-1][0]).total_seconds() > max_gap_sec
                    for j in range(1, len(eval_snaps))
                )
                if not has_any_gap:
                    snap_15m = eval_snaps[0]

        prev_atm_pcr = None
        prev_heavy_pcr = None
        if snap_15m is not None and not is_data_gap:
            r_atm_15m = snap_15m[2][snap_15m[2]["strike"] == atm]
            if not r_atm_15m.empty:
                pe_15m = float(r_atm_15m.iloc[0].get("pe_oi", 0) or 0)
                ce_15m = float(r_atm_15m.iloc[0].get("ce_oi", 0) or 0)
                if ce_15m > 0:
                    prev_atm_pcr = pe_15m / ce_15m

            r_hvy_15m = snap_15m[2][snap_15m[2]["strike"] == heavy_strike]
            if not r_hvy_15m.empty:
                h_pe_15m = float(r_hvy_15m.iloc[0].get("pe_oi", 0) or 0)
                h_ce_15m = float(r_hvy_15m.iloc[0].get("ce_oi", 0) or 0)
                if h_ce_15m > 0:
                    prev_heavy_pcr = h_pe_15m / h_ce_15m

        if prev_atm_pcr is None and tl and tl.series and not is_data_gap:
            lookback_steps = max(1, 15 // rules.get("resample_minutes", 3))
            idx_15m = max(0, len(tl.series) - 1 - lookback_steps)
            prev_atm_pcr = tl.series[idx_15m].pcr

        if prev_atm_pcr is not None:
            if pcr_standing > prev_atm_pcr + 0.02:
                pcr_atm_arrow = "↑"
                pcr_trend = "Rising ↑"
            elif pcr_standing < prev_atm_pcr - 0.02:
                pcr_atm_arrow = "↓"
                pcr_trend = "Falling ↓"
            else:
                pcr_atm_arrow = "→"
                pcr_trend = "Flat →"
        else:
            pcr_atm_arrow = "→"
            pcr_trend = "Flat →"

        if prev_heavy_pcr is not None:
            if pcr_heavy > prev_heavy_pcr + 0.02:
                pcr_heavy_arrow = "↑"
            elif pcr_heavy < prev_heavy_pcr - 0.02:
                pcr_heavy_arrow = "↓"
            else:
                pcr_heavy_arrow = "→"
        else:
            pcr_heavy_arrow = "→"

        # OI Flow (last 15 min)
        if is_data_gap:
            d_ce_15m = 0.0
            d_pe_15m = 0.0
            spread_15m = 0.0
            ce_act = "INVALID (data gap)"
            pe_act = "INVALID (data gap)"
            flow_str = "INVALID (data gap)"
            floor_weakening_streak = 0
        else:
            idx_15m_step = max(0, len(tl.series) - 1 - max(1, 15 // rules.get("resample_minutes", 3)))
            pt_15m = tl.series[idx_15m_step]
            raw_d_ce = latest_pt.ce_chg_oi - pt_15m.ce_chg_oi
            raw_d_pe = latest_pt.pe_chg_oi - pt_15m.pe_chg_oi

            norm_by_time = bool(rules.get("flow", {}).get("normalise_by_time", True))
            elapsed_sec = (latest_pt.timestamp - pt_15m.timestamp).total_seconds()
            std_sec = float(rules.get("flow", {}).get("standard_window_seconds", 900))
            if norm_by_time and elapsed_sec > 0:
                time_factor = std_sec / elapsed_sec
                d_ce_15m = raw_d_ce * time_factor
                d_pe_15m = raw_d_pe * time_factor
            else:
                d_ce_15m = raw_d_ce
                d_pe_15m = raw_d_pe
            spread_15m = d_pe_15m - d_ce_15m

            # Consecutive briefings with puts leaving (d_pe < 0) and calls adding (d_ce > 0)
            resample_mins = rules.get("resample_minutes", 3)
            step_15m = max(1, 15 // resample_mins)
            floor_weakening_streak = 0
            for idx in range(len(tl.series) - 1, 0, -1):
                t_c = tl.series[idx].timestamp
                t_p = tl.series[idx - 1].timestamp
                if (t_c - t_p).total_seconds() > max_gap_sec:
                    break
                idx_prev = max(0, idx - step_15m)
                has_gap_in_window = False
                for j in range(idx, idx_prev, -1):
                    if (tl.series[j].timestamp - tl.series[j-1].timestamp).total_seconds() > max_gap_sec:
                        has_gap_in_window = True
                        break
                if has_gap_in_window:
                    break
                delta_pe = tl.series[idx].pe_chg_oi - tl.series[idx_prev].pe_chg_oi
                delta_ce = tl.series[idx].ce_chg_oi - tl.series[idx_prev].ce_chg_oi
                if delta_pe < 0 and delta_ce > 0:
                    floor_weakening_streak += 1
                else:
                    break

            if d_ce_15m < 0:
                ce_act = "Call unwinding ↓"
            elif d_ce_15m > 0:
                ce_act = "Call writing ↑" if (latest_pt.ce_ltp <= pt_15m.ce_ltp) else "Call buying ↑"
            else:
                ce_act = "Flat"

            if d_pe_15m > 0:
                pe_act = "Put writing ↑" if (latest_pt.pe_ltp <= pt_15m.pe_ltp) else "Put buying ↑"
            elif d_pe_15m < 0:
                pe_act = "Put unwinding ↓"
            else:
                pe_act = "Flat"

            flow_str = latest_pt.flow

        # Effective dominance for Bias line
        eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
        try:
            eod_no_trade_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
            is_eod_no_trade = (latest_ts.time() >= eod_no_trade_t)
        except Exception:
            is_eod_no_trade = (latest_ts.time() >= dtime(15, 0))

        mkt_open_str = rules.get(
            "observations_market_open_time",
            rules.get("market_open_time", getattr(config, "MARKET_OPEN", dtime(9, 15)).strftime("%H:%M") if hasattr(config, "MARKET_OPEN") else "09:15")
        )
        try:
            mkt_open_t = datetime.strptime(str(mkt_open_str), "%H:%M").time()
        except Exception:
            mkt_open_t = dtime(9, 15)
        is_pre_market = (latest_ts.time() < mkt_open_t)

        eff_dom = current_state
        if is_pre_market:
            eff_dom = "PRE_MARKET"
            regime_str = "PRE-MARKET — no live session data"
            bias_str = "PRE-MARKET"
        elif is_data_gap:
            eff_dom = "INVALID"
            regime_str = "DATA GAP — signals invalid"
            bias_str = "INVALID (data gap)"
        elif eff_dom in ("EOD_UNWIND", "WARMUP"):
            if is_eod_no_trade:
                eff_dom = "EOD_UNWIND"
            elif latest_pt.spread >= rules.get("dominance_min_contracts", 50000.0):
                eff_dom = "PE_DOMINANT"
            elif latest_pt.spread <= -rules.get("dominance_min_contracts", 50000.0):
                eff_dom = "CE_DOMINANT"
            elif prev_state in ("PE_DOMINANT", "CE_DOMINANT"):
                eff_dom = prev_state
            else:
                eff_dom = "BALANCED"

        # Expiry Day Unwind Override after 14:00
        if is_pre_market:
            eff_dom = "PRE_MARKET"
            regime_str = "PRE-MARKET — no live session data"
            bias_str = "PRE-MARKET"
        elif is_data_gap:
            eff_dom = "INVALID"
            regime_str = "DATA GAP — signals invalid"
            bias_str = "INVALID (data gap)"
        elif is_expiry_day and latest_ts.time() >= dtime(14, 0):
            eff_dom = "EXPIRY UNWIND"
            regime_str = "EXPIRY UNWIND — OI data unreliable"
            bias_str = "EXPIRY UNWIND ⚠️"
        elif is_eod_no_trade:
            eff_dom = "EOD_UNWIND"
            regime_str = f"EOD_UNWIND ({duration_mins} min)"
            bias_str = "EOD UNWIND ⚠️"
        elif eff_dom == "PE_DOMINANT" and "Rising" in pcr_trend:
            bias_str = "BULLISH  ▲"
        elif eff_dom == "CE_DOMINANT" and "Falling" in pcr_trend:
            bias_str = "BEARISH  ▼"
        elif eff_dom == "BALANCED":
            bias_str = "NEUTRAL  →"
        else:
            bias_str = "NEUTRAL  →"

    else:
        current_state = "N/A"
        prev_state = None
        regime_str = "N/A"
        pcr_trend = "Flat →"
        pcr_atm_arrow = "→"
        pcr_heavy_arrow = "→"
        bias_str = "NEUTRAL  →"
        d_ce_15m = 0.0
        d_pe_15m = 0.0
        spread_15m = 0.0
        ce_act = "N/A"
        pe_act = "N/A"
        flow_str = "N/A"
        eff_dom = "BALANCED"
        floor_weakening_streak = 0

        mkt_open_str = rules.get(
            "observations_market_open_time",
            rules.get("market_open_time", getattr(config, "MARKET_OPEN", dtime(9, 15)).strftime("%H:%M") if hasattr(config, "MARKET_OPEN") else "09:15")
        )
        try:
            mkt_open_t = datetime.strptime(str(mkt_open_str), "%H:%M").time()
        except Exception:
            mkt_open_t = dtime(9, 15)
        is_pre_market = (latest_ts.time() < mkt_open_t)

        if is_pre_market:
            eff_dom = "PRE_MARKET"
            regime_str = "PRE-MARKET — no live session data"
            bias_str = "PRE-MARKET"
        elif is_data_gap:
            eff_dom = "INVALID"
            regime_str = "DATA GAP — signals invalid"
            bias_str = "INVALID (data gap)"
        elif is_expiry_day and latest_ts.time() >= dtime(14, 0):
            eff_dom = "EXPIRY UNWIND"
            regime_str = "EXPIRY UNWIND — OI data unreliable"
            bias_str = "EXPIRY UNWIND ⚠️"

    floor_weakening_warning_line = (
        format_floor_weakening_warning(
            floor_weakening_streak,
            min_consecutive=rules.get("floor_weakening_consecutive_bars", 2),
        )
        if not is_data_gap
        else None
    )

    # ── Institutional Read ────────────────────────────────────────────────
    if is_data_gap:
        pe_support_str = "INVALID (data gap)"
        ce_resist_str = "INVALID (data gap)"
        spike_str = "INVALID (data gap)"
    else:
        pe_writing = (d_pe_15m > 0)
        ce_writing = (d_ce_15m > 0)

        pe_support_str = "YES  ✅" if pe_writing else "NO  ✗"
        ce_resist_str = "YES  ✅" if ce_writing else "NO  ✗"

        # Sudden OI spike (>50k, 1 bar)
        spike_thresh = rules.get("multistrike_discontinuity_threshold_contracts", 50000.0)
        has_spike = False
        if len(eval_snaps) >= 2:
            prev_snap_df = eval_snaps[-2][2]
            merged = pd.merge(
                prev_snap_df[["strike", "ce_oi", "pe_oi"]],
                latest_df[["strike", "ce_oi", "pe_oi"]],
                on="strike",
                suffixes=("_prev", "_curr"),
            )
            if not merged.empty:
                ce_spikes = (merged["ce_oi_curr"] - merged["ce_oi_prev"]) >= spike_thresh
                pe_spikes = (merged["pe_oi_curr"] - merged["pe_oi_prev"]) >= spike_thresh
                has_spike = bool(ce_spikes.any() or pe_spikes.any())
        spike_str = "YES  🚨" if has_spike else "NO  ✗"

    # Roll Hazard
    roll_threshold_pct = rules.get("roll_jump_threshold_pct", 0.35)
    roll_jumps = [
        tl_item
        for tl_item in getattr(tl, "series", [])
        if getattr(tl_item, "jump_detected", False)
        and getattr(tl_item, "roll_ratio", 0.0) >= roll_threshold_pct
    ] if 'tl' in locals() and tl else []
    roll_str = "YES  ⚠️" if len(roll_jumps) > 0 else "NO  ✗"

    # ── Option Chain Snapshot (ATM ± 2 strikes) ───────────────────────────
    chain_strikes = [atm + i * step for i in [2, 1, 0, -1, -2]]
    chain_rows: List[str] = []
    for s in chain_strikes:
        s_row = latest_df[latest_df["strike"] == s]
        if not s_row.empty:
            sr = s_row.iloc[0]
            c_oi = sr.get("ce_oi", 0) or 0.0
            p_oi = sr.get("pe_oi", 0) or 0.0
            c_d = sr.get("ce_chg_oi", 0) or 0.0
            p_d = sr.get("pe_chg_oi", 0) or 0.0
            pcr_s = (p_oi / c_oi) if c_oi > 0 else 0.0
            c_oi_str = f"{c_oi * mult:.1f}L"
            p_oi_str = f"{p_oi * mult:.1f}L"
            if is_data_gap:
                c_d_str = "INVALID"
                p_d_str = "INVALID"
            else:
                c_d_str = f"{c_d / 1000:+.1f}k" if abs(c_d) >= 1000 else f"{c_d:+.0f}"
                p_d_str = f"{p_d / 1000:+.1f}k" if abs(p_d) >= 1000 else f"{p_d:+.0f}"
            pcr_str = f"{pcr_s:.2f}"
        else:
            c_oi_str = "N/A"
            p_oi_str = "N/A"
            c_d_str = "N/A"
            p_d_str = "N/A"
            pcr_str = "N/A"

        row_text = f"  {s:<7.0f} | {c_oi_str:>6}  | {c_d_str:>7}  | {pcr_str:>5} | {p_oi_str:>6}  | {p_d_str:>7}"
        if s == atm:
            row_text += "  ← ATM"
        chain_rows.append(row_text)

    # ── Veto / Action Guidance ────────────────────────────────────────────
    freeze_start_str = rules.get("freeze_start", "11:30")
    freeze_end_str = rules.get("freeze_end", "13:15")
    try:
        f_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        f_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        is_in_freeze = (f_start_t <= latest_ts.time() <= f_end_t)
    except Exception:
        is_in_freeze = False

    resample_mins = int(rules.get("resample_minutes", 3))
    next_ts = latest_ts + timedelta(minutes=resample_mins)
    next_str = next_ts.strftime("%H:%M")

    if is_data_gap:
        veto1 = "⚠️  DATA GAP — flow, dominance & streak signals invalid"
        veto2 = f"➡️ WAIT — data gap, signals invalid until {next_str}"
    elif is_pre_market:
        veto1 = "⏸  PRE-MARKET — market opens at 09:15"
        veto2 = "➡️ PRE-MARKET — no signal (stale spot, no RVOL/GEX)"
    elif is_expiry_day and latest_ts.time() >= dtime(14, 0):
        veto1 = "⚠️  EXPIRY UNWIND — OI data unreliable"
        veto2 = "⏸  Positions settling — avoid directional trades"
    elif is_eod_no_trade:
        veto1 = "⚠️  EOD UNWIND — close positions, no new entry"
        veto2 = "➡️ EOD — no new trades, close open positions"
    elif eff_dom == "PE_DOMINANT":
        veto1 = "❌ Do NOT short — PE writers dominant"
        veto2 = "✅ CE buy bias — but confirm 8 EMA on 15m chart first"
    elif eff_dom == "CE_DOMINANT":
        veto1 = "❌ Do NOT go long — CE writers dominant"
        veto2 = "✅ PE buy bias — but confirm 8 EMA on 15m chart first"
    elif eff_dom == "BALANCED":
        veto1 = "⚠️  No clear edge — balanced market"
        veto2 = "⏸  Wait for directional expansion"
    elif eff_dom == "WARMUP":
        veto1 = f"⏸  Market warmup until {rules.get('warmup_until', '09:50')} — observe only"
        veto2 = "⚠️  No trades during warmup"
    else:
        veto1 = "⚠️  Intraday positions squaring off"
        if prev_state == "PE_DOMINANT":
            veto2 = "✅ CE buy bias — but confirm 8 EMA on 15m chart first"
        elif prev_state == "CE_DOMINANT":
            veto2 = "✅ PE buy bias — but confirm 8 EMA on 15m chart first"
        else:
            veto2 = "⏸  Observe only — square-offs dominant"

    if is_in_freeze:
        freeze_line = f"⏸  Buyer freeze: {freeze_start_str}–{freeze_end_str} — ACTIVE (observe only)"
    else:
        freeze_line = None

    # ── Data Health ───────────────────────────────────────────────────────
    cov = oi_timeline.compute_session_coverage(all_snaps, target_date, rules=rules)
    cov_icon = "✅" if (cov.passes_gate or cov.occupied_bins >= 100) else "⚠️"
    cov_str = f"{cov.occupied_bins}/{cov.total_bins} bins  {cov_icon}"

    now_dt = datetime.now()
    if target_date == now_dt.date():
        age_sec = max(0, (now_dt - latest_ts).total_seconds())
        if age_sec < 60:
            age_str = f"{int(age_sec)}s ago"
        elif age_sec < 3600:
            age_str = f"{int(age_sec // 60)}m {int(age_sec % 60)}s ago"
        else:
            age_str = f"{int(age_sec // 3600)}h {int((age_sec % 3600) // 60)}m ago"
        is_stale = (age_sec > rules.get("stale_minutes", 10) * 60)
        snap_icon = "✅" if not is_stale else "⚠️"
        stale_feed_str = "YES ⚠️" if is_stale else "NO"
    else:
        age_str = "session complete"
        snap_icon = "✅"
        stale_feed_str = "NO"
        is_stale = False

    r1_txt = (
        f"  R1:  {r1_row['strike']:<7,.0f} [OI wall: {r1_row['ce_oi'] * mult:.1f}L CE writers]"
        if r1_row is not None
        else "  R1:  N/A"
    )
    r2_txt = (
        f"  R2:  {r2_row['strike']:<7,.0f} [OI wall: {r2_row['ce_oi'] * mult:.1f}L CE writers]"
        if r2_row is not None
        else "  R2:  N/A"
    )
    atm_txt = f"  ATM: {atm:<7,.0f} ← You are here"
    s1_txt = (
        f"  S1:  {s1_row['strike']:<7,.0f} [OI wall: {s1_row['pe_oi'] * mult:.1f}L PE writers]"
        if s1_row is not None
        else "  S1:  N/A"
    )
    s2_txt = (
        f"  S2:  {s2_row['strike']:<7,.0f} [OI wall: {s2_row['pe_oi'] * mult:.1f}L PE writers]"
        if s2_row is not None
        else "  S2:  N/A"
    )

    div = "═" * 51
    time_display = latest_ts.strftime("%H:%M")
    spot_display = f"Spot {latest_spot:,.0f} (prev close)" if is_pre_market else f"Spot {latest_spot:,.0f}"

    sr_lines = [
        "SUPPORT / RESISTANCE",
        r1_txt,
        r2_txt,
        atm_txt,
        s1_txt,
        s2_txt,
    ]
    if major_wall_txt:
        sr_lines.append(major_wall_txt)

    pd_levels = session_levels.load_prior_day_levels(target_date)
    if pd_levels:
        pdh = pd_levels.get("PDH", 0)
        pdl = pd_levels.get("PDL", 0)
        pdc = pd_levels.get("PDC", 0)
        pdo = pd_levels.get("PDO", 0)
        sr_lines.append(f"  PRIOR DAY:  PDH {pdh:,.0f} | PDL {pdl:,.0f} | PDC {pdc:,.0f} | PDO {pdo:,.0f}")
        if "date" in pd_levels:
            try:
                prev_d = datetime.strptime(str(pd_levels["date"]), "%Y-%m-%d").date()
                if was_prev_session_expiry(prev_d, rules):
                    sr_lines.append("  ⚠️ PDH/PDL from expiry session — expect wider range / false breaks")
                prev_snaps = store.load_day(prev_d)
                if prev_snaps:
                    y_df = prev_snaps[-1][2]
                    mig_msgs = detect_wall_migrations(latest_df, y_df, latest_spot, rules)
                    for m in mig_msgs:
                        sr_lines.append(f"  🏛️ MIGRATION: {m}")
            except Exception as mig_err:
                logger.warning(f"Failed to check wall migrations: {mig_err}")

    pcr_atm_label = f"PCR (ATM {atm:,.0f}):"
    pcr_atm_line = f"{pcr_atm_label:<22}{pcr_standing:.2f}  {pcr_atm_arrow}"
    pcr_heavy_label = f"PCR (max OI {heavy_strike:,.0f}):"
    pcr_heavy_line = f"{pcr_heavy_label:<22}{pcr_heavy:.2f}  {pcr_heavy_arrow}  ← price magnet"

    effective_target_time = target_time if target_time is not None else latest_ts.time()
    dr = compute_day_range(target_date, eval_snaps, all_snaps, target_time=effective_target_time)
    day_low, day_high = dr.day_low, dr.day_high
    day_low_time, day_high_time = dr.day_low_time, dr.day_high_time

    # ── Institutional Context (Market Open 09:15 only - Rules 1, 8, 9) ───
    is_0915_open = (
        (target_time is not None and target_time.hour == 9 and target_time.minute == 15)
        or (target_time is None and latest_ts.time().hour == 9 and latest_ts.time().minute == 15)
    )
    inst_context = None
    if is_0915_open and rules.get("institutional_context", {}).get("enabled", True):
        inst_context = build_institutional_context(
            target_date=target_date,
            gex_data=gex_data,
            iv_data=iv_data,
            rules=rules,
        )

    header_lines = [
        div,
        *([ "⚠️ EXPIRY DAY — OI reading different today", div ] if is_expiry_day else []),
        *([ gap_msg, div ] if is_data_gap and gap_msg else []),
        f"NIFTY LIVE BRIEFING  |  {time_display}  |  {spot_display}",
        div,
    ]
    if inst_context:
        header_lines.extend([
            "",
            inst_context,
            "",
            div,
        ])

    has_history = len(eval_snaps) >= 2
    chk_data = compute_pretrade_checklist(
        s1_row=s1_row,
        r1_row=r1_row,
        d_pe_15m=d_pe_15m,
        d_ce_15m=d_ce_15m,
        iv_percentile=iv_data.get("percentile"),
        gex_data=gex_data,
        latest_ts=latest_ts,
        target_date=target_date,
        target_time=target_time,
        rules=rules,
        has_history=has_history,
        iv_status=iv_data.get("status"),
        iv_data=iv_data,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
    )

    low_str = f"{day_low:,.2f}".rstrip('0').rstrip('.') if day_low % 1 else f"{int(day_low):,}"
    high_str = f"{day_high:,.2f}" if day_high % 1 else f"{int(day_high):,}"
    day_range_str = f"Low {low_str} ({day_low_time}) ↔ High {high_str} ({day_high_time}) ({int(round(day_high - day_low))} pts)"

    s1_strike = float(s1_row["strike"]) if s1_row is not None else None
    r1_strike = float(r1_row["strike"]) if r1_row is not None else None
    s2_strike = float(s2_row["strike"]) if s2_row is not None else None
    r2_strike = float(r2_row["strike"]) if r2_row is not None else None

    dom_data = compute_writer_dominance(
        latest_df=latest_df,
        latest_spot=latest_spot,
        rules=rules,
        atm_override=atm,
        gex_data=gex_data,
        s1_strike=s1_strike,
        r1_strike=r1_strike,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
    )
    build_briefing.writer_dominance = dom_data

    writer_gap_poll = None
    try:
        writer_gap_poll = oi_timeline.get_writer_gap_poll_live(eval_snaps, target_date, rules)
    except Exception as exc:
        logger.warning(f"Error evaluating writer gap poll: {exc}")
    build_briefing.writer_gap = writer_gap_poll

    lines = [
        *header_lines,
        "",
        f"{'BIAS:':<22}{bias_str}",
        f"{'REGIME:':<22}{regime_str}",
        f"{'GEX:':<22}{gex_data['terminal_str']}",
        f"{'IV (ATM):':<22}{iv_data['terminal_str']}",
        *([f"{'Zero gamma:':<22}{gex_data['zero_gamma_str']}"] if gex_data.get("zero_gamma_str") else []),
        *([f"{'MAX PAIN:':<22}{max_pain_str}"] if max_pain_str else []),
        f"{'DAY RANGE:':<22}{day_range_str}",
        pcr_atm_line,
        pcr_heavy_line,
        f"{'PCR (flow):':<22}{pcr_flow_str}",
        writers_ratio_line,
        "",
        *inst_wall_lines,
        "",
        *sr_lines,
        "",
        "OI FLOW (last 15 min)",
        f"  CE ΔOI:  {d_ce_15m:>+7,.0f}  ({ce_act})",
        f"  PE ΔOI:  {d_pe_15m:>+7,.0f}  ({pe_act})",
        f"  Spread:  {spread_15m:>+7,.0f} contracts",
        f"  Flow:    {flow_str}",
        *([f"  {floor_weakening_warning_line}"] if floor_weakening_warning_line else []),
        "",
        "INSTITUTIONAL READ",
        f"  PE writers building support:   {pe_support_str}",
        f"  CE writers building resistance: {ce_resist_str}",
        *dom_data.get("terminal_lines", []),
        f"  Sudden OI spike (>50k, 1 bar): {spike_str}",
        f"  Roll hazard detected:           {roll_str}",
        *([f"\n{writer_gap_poll.briefing_block_text}"] if writer_gap_poll else []),
        "",
        "OPTION CHAIN SNAPSHOT  (ATM ± 2 strikes)",
        "  Strike  |  CE OI  |  CE ΔOI  |  PCR  |  PE OI  |  PE ΔOI",
        *chain_rows,
        "",
        "VETO / ACTION GUIDANCE",
        f"  {veto1}",
        f"  {veto2}",
        *([f"  {freeze_line}"] if freeze_line else []),
        "",
        *chk_data["terminal_lines"],
        "",
        "DATA HEALTH",
        f"  Last snapshot:  {latest_ts.strftime('%H:%M:%S')}  ({age_str}) {snap_icon}",
        *([f"  Data gap:       {gap_mins} min ({prev_str}–{curr_str}) ⚠️"] if is_data_gap else []),
        f"  Coverage today: {cov_str}",
        f"  Stale feed:     {stale_feed_str}",
        div,
    ]

    s1_strike = float(s1_row["strike"]) if s1_row is not None else None
    r1_strike = float(r1_row["strike"]) if r1_row is not None else None
    s2_strike = float(s2_row["strike"]) if s2_row is not None else None
    r2_strike = float(r2_row["strike"]) if r2_row is not None else None

    tg_briefing = build_telegram_briefing(
        latest_ts=latest_ts,
        latest_spot=latest_spot,
        latest_df=latest_df,
        eff_dom=eff_dom,
        pcr_standing=pcr_standing,
        pcr_trend=pcr_trend,
        d_pe_15m=d_pe_15m,
        d_ce_15m=d_ce_15m,
        mult=mult,
        rules=rules,
        eval_snaps=eval_snaps,
        is_stale=is_stale,
        floor_weakening_streak=floor_weakening_streak,
        day_low=day_low,
        day_high=day_high,
        s1_strike=s1_strike,
        r1_strike=r1_strike,
        is_expiry_day=is_expiry_day,
        gex_tg_line=gex_data.get("telegram_str"),
        max_pain=max_pain,
        iv_tg_line=iv_data.get("telegram_str"),
        chk_tg_lines=chk_data.get("telegram_lines"),
        institutional_context=inst_context,
        day_low_time=day_low_time,
        day_high_time=day_high_time,
        prior_day_levels=pd_levels,
        writer_dom_line=dom_data.get("telegram_line") if rules.get("writer_dominance", {}).get("telegram_line_enabled", True) else None,
        writer_struct_line=dom_data.get("telegram_struct_line") if rules.get("writer_dominance", {}).get("telegram_line_enabled", True) else None,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
    )

    # Finding 1: Freeze End Alert
    freeze_end_alert = None
    freeze_end_str = rules.get("freeze_end", "13:15")
    try:
        f_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        prev_ts = eval_snaps[-2][0] if len(eval_snaps) >= 2 else latest_ts
        is_freeze_end = (
            (prev_ts.time() < f_end_t <= latest_ts.time())
            or (target_time is not None and target_time.hour == f_end_t.hour and target_time.minute == f_end_t.minute)
            or (target_time is None and latest_ts.time().hour == f_end_t.hour and latest_ts.time().minute == f_end_t.minute)
        )
        if is_freeze_end and rules.get("freeze_end_alert_enabled", True):
            if is_expiry_day and max_pain is not None:
                freeze_end_alert = build_expiry_1315_summary(
                    latest_spot=latest_spot,
                    max_pain=max_pain,
                    pcr_standing=pcr_standing,
                    pcr_trend=pcr_trend,
                    d_pe_15m=d_pe_15m,
                    d_ce_15m=d_ce_15m,
                    floor_weakening_streak=floor_weakening_streak,
                    eff_dom=eff_dom,
                    bias_str=bias_str,
                )
            else:
                freeze_end_alert = build_freeze_end_alert(
                    latest_ts=latest_ts,
                    latest_spot=latest_spot,
                    latest_df=latest_df,
                    s1_strike=s1_strike,
                    r1_strike=r1_strike,
                    bias_word=bias_str.strip() if 'bias_str' in locals() else "NEUTRAL →",
                    action_word="confirm 8 EMA on chart",
                    eval_snaps=eval_snaps,
                    rules=rules,
                )
    except Exception as e:
        logger.warning(f"Error evaluating freeze end alert: {e}")

    # Finding 2: Approaching Ceiling or Floor Alert
    approach_alert = check_approaching_ceiling_floor(
        latest_spot=latest_spot,
        latest_df=latest_df,
        r1_row=r1_row,
        r2_row=r2_row,
        s1_row=s1_row,
        s2_row=s2_row,
        rules=rules,
        mult=mult,
    )

    # Finding 3: S1 Liquidity Trap Alert
    trap_alert = check_s1_liquidity_trap(
        eval_snaps=eval_snaps,
        s1_strike=s1_strike,
        latest_spot=latest_spot,
        rules=rules,
    )

    if is_data_gap:
        freeze_end_alert = None
        trap_alert = None

    # Finding 5: EOD Summary Alert (at 15:25)
    eod_summary = None
    eod_summary_str = rules.get("eod_summary_time", "15:25")
    try:
        eod_sum_t = datetime.strptime(eod_summary_str, "%H:%M").time()
        is_eod = (
            (latest_ts.time() >= eod_sum_t)
            or (
                target_time is not None
                and (
                    target_time.hour > eod_sum_t.hour
                    or (target_time.hour == eod_sum_t.hour and target_time.minute >= eod_sum_t.minute)
                )
            )
        )
        if is_eod and rules.get("eod_summary_enabled", True):
            eod_summary = build_eod_summary(
                target_date=target_date,
                eval_snaps=eval_snaps,
                all_snaps=all_snaps,
                rules=rules,
                latest_spot=latest_spot,
                latest_df=latest_df,
                r1_strike=r1_strike,
                r2_strike=r2_strike,
                s1_strike=s1_strike,
                s2_strike=s2_strike,
            )
    except Exception as e:
        logger.warning(f"Error evaluating EOD summary: {e}")

    build_briefing.telegram_briefing = tg_briefing
    build_briefing.iv_data = iv_data
    build_briefing.checklist_data = chk_data
    build_briefing.freeze_end_alert = freeze_end_alert
    build_briefing.approach_alert = approach_alert
    build_briefing.trap_alert = trap_alert
    build_briefing.eod_summary = eod_summary
    build_briefing.institutional_context = inst_context
    build_briefing.last_snapshot_ts = latest_ts
    build_briefing.last_is_stale = is_stale
    build_briefing.writer_dominance = dom_data

    # Daily Observation Log append (only active when append_obs=True)
    obs_text = None
    if rules.get("observations_enabled", True) and append_obs:
        try:
            obs_bias = getattr(build_telegram_briefing, "last_bias_word", "NEUTRAL →")
            obs_action = getattr(build_telegram_briefing, "last_action_line", "NO TRADE")
            obs_text = append_observation_log(
                target_date=target_date,
                latest_ts=latest_ts,
                latest_spot=latest_spot,
                bias_word=obs_bias,
                gex_data=gex_data,
                iv_data=iv_data,
                s1_row=s1_row,
                r1_row=r1_row,
                mult=mult,
                d_pe_15m=d_pe_15m,
                d_ce_15m=d_ce_15m,
                floor_weakening_streak=floor_weakening_streak,
                chk_data=chk_data,
                action_line=obs_action,
                rules=rules,
                eval_snaps=eval_snaps,
                all_snaps=all_snaps,
                latest_df=latest_df,
                r1_strike=r1_strike,
                r2_strike=r2_strike,
                s1_strike=s1_strike,
                s2_strike=s2_strike,
                dom_data=dom_data,
                is_data_gap=is_data_gap,
                gap_msg=gap_msg,
            )
        except Exception as e:
            logger.warning(f"Error appending observation log: {e}")

    build_briefing.observation_text = obs_text
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Telegram Delivery Dispatcher
# ═══════════════════════════════════════════════════════════════════════════

def dispatch_telegram_briefing(
    output: str,
    rules: dict,
    target_date: date,
    latest_ts: Optional[datetime],
    tg_briefing: Optional[str] = None,
    freeze_end_alert: Optional[str] = None,
    approach_alert: Optional[str] = None,
    trap_alert: Optional[str] = None,
    eod_summary: Optional[str] = None,
) -> None:
    """
    Sends the concise phone-optimized briefing text and alerts to Telegram if enabled,
    within market hours, and market data exists without a stale feed (>10m).
    """
    try:
        # Check if Telegram briefing is enabled in rules
        if not rules.get("telegram_briefing_enabled", True):
            return

        # Market data must exist
        if not output or output == "Market closed or no data yet" or latest_ts is None:
            return

        now_dt = datetime.now()

        # Only send during market hours: 09:15 to 15:30 IST
        mkt_open = dtime(9, 15, 0)
        mkt_close = dtime(15, 30, 0)
        if not (mkt_open <= now_dt.time() <= mkt_close):
            return

        # If last snapshot is older than stale_minutes, skip Telegram and print STALE
        stale_threshold_sec = float(rules.get("stale_minutes", 10)) * 60.0
        age_sec = (now_dt - latest_ts).total_seconds()
        if target_date != now_dt.date() or age_sec > stale_threshold_sec:
            print("STALE - Telegram skipped")
            return

        # Send special alerts if fired
        if freeze_end_alert:
            send_telegram_alert(freeze_end_alert)
        if approach_alert:
            send_telegram_alert(approach_alert)
        if trap_alert:
            send_telegram_alert(trap_alert)
        if eod_summary:
            send_telegram_alert(eod_summary)
        if writer_gap_poll and writer_gap_poll.new_crossover:
            oi_timeline.dispatch_writer_alerts(crossover=writer_gap_poll.new_crossover, read=None, rules=rules, send_func=send_telegram_alert)

        # Send concise phone briefing (maximum 8 lines, plain words)
        msg_to_send = tg_briefing or output
        send_telegram_alert(msg_to_send)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Telegram dispatch failed ({exc.__class__.__name__}): {exc}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# 3b. Periodic 5-MIN & 15-MIN Snapshots
# ═══════════════════════════════════════════════════════════════════════════

def get_snapshot_tf_for_time(t: Union[dtime, datetime, str]) -> Optional[str]:
    """
    Returns '15m' if t is on a 15-minute boundary,
    '5m' if t is on a 5-minute boundary (skipping 15m boundary),
    or None if outside market hours (09:20 - 15:25) or not on a 5-minute boundary.
    """
    if isinstance(t, datetime):
        t_val = t.time()
    elif isinstance(t, str):
        t_val = datetime.strptime(t[:5], "%H:%M").time()
    else:
        t_val = t

    # Must be within market trading hours where a candle has closed
    # First 5m candle closes at 09:20; last 5m candle closes at 15:25
    if t_val < dtime(9, 20) or t_val > dtime(15, 25):
        return None

    if t_val.minute % 5 != 0:
        return None

    # On a 15-minute boundary, send ONLY the 15-minute report (skip 5-min)
    if t_val.minute % 15 == 0 and t_val <= dtime(15, 15):
        return "15m"

    # Otherwise 5-min boundary
    return "5m"


# OBSFIX helpers ─────────────────────────────────────────────────────────────
def _obs_is_repeat(content: str, entry_text: str, tf, latest_ts, rules: dict) -> bool:
    """OBSFIX-2: True when this entry has the same label and action as the previous entry of the same kind
    (briefing / 5m / 15m), written less than observations_heartbeat_minutes ago."""
    try:
        hb = int(rules.get("observations_heartbeat_minutes", 15))
        if hb <= 0:
            return False
        want = f"tf: {tf}" if tf is not None else None
        last = None
        for blk in re.findall(r"^##\s+\d\d:\d\d\b.*?(?=(?:\n##\s|\Z))", content, flags=re.MULTILINE | re.DOTALL):
            if "- Checklist:" not in blk and "- GEX:" not in blk:
                continue                                   # alert-only chunks are not entries of this kind
            if (want and want in blk) or (want is None and "<!-- tf:" not in blk):
                last = blk
        if not last:
            return False
        hdr = lambda s: re.match(r"##\s+(\d\d):(\d\d)\s*\|[^|]*\|\s*(.*)", s)
        act = lambda s: (re.search(r"^- Action:\s*(.*)$", s, flags=re.MULTILINE) or [None, ""])[1].strip()
        h_old, h_new = hdr(last), hdr(entry_text.lstrip())
        if not h_old or not h_new:
            return False
        if h_old.group(3).strip() != h_new.group(3).strip() or act(last) != act(entry_text):
            return False
        gap = (latest_ts.hour * 60 + latest_ts.minute) - (int(h_old.group(1)) * 60 + int(h_old.group(2)))
        return 0 <= gap < hb
    except Exception:
        return False


def _expiry_unwind_now(target_date, latest_ts, latest_df, rules: dict) -> bool:
    """OBSFIX-1: expiry day and at/after the unwind time (rules expiry_unwind_time, default 14:00)."""
    try:
        exp = None
        if latest_df is not None and "expiry" in getattr(latest_df, "columns", []):
            vals = latest_df["expiry"].dropna()
            exp = str(vals.iloc[0]) if len(vals) else None
        if not exp:
            folder = config.SNAPSHOT_DIR / target_date.strftime("%Y-%m-%d")
            metas = sorted(folder.glob("*.meta.json"))
            if metas:
                exp = json.loads(metas[-1].read_text()).get("expiry")
        exp_d = parse_expiry_date(exp)
        if exp_d is None or exp_d != target_date:
            return False
        cut = str(rules.get("expiry_unwind_time", "14:00"))
        return latest_ts.time() >= datetime.strptime(cut, "%H:%M").time()
    except Exception:
        return False

def build_periodic_snapshot(
    timeframe: str,
    target_date: date,
    target_time: Optional[dtime] = None,
    append_obs: bool = False,
    all_snaps: Optional[List[Tuple[datetime, float, pd.DataFrame]]] = None,
) -> Dict[str, Any]:
    """
    Builds the 5-MIN or 15-MIN periodic snapshot report.
    Uses ONLY existing fields from the 3-min briefing in the same order.
    Flow is computed over the whole window (5m or 15m).
    """
    tf = timeframe.lower().strip()
    if tf not in ("5m", "15m"):
        raise ValueError(f"Invalid timeframe {timeframe}; expected '5m' or '15m'")

    rules = load_briefing_rules()
    folder = config.SNAPSHOT_DIR / target_date.strftime("%Y-%m-%d")
    if not folder.exists() or not any(folder.glob("*.parquet")):
        return {"status": "error", "message": "Market closed or no data yet"}

    if all_snaps is None:
        all_snaps = store.load_day(target_date)
    if not all_snaps:
        return {"status": "error", "message": "Market closed or no data yet"}

    eval_day_snaps = all_snaps
    if target_time is not None:
        eval_day_snaps = [s for s in all_snaps if s[0].time() <= target_time]
        if not eval_day_snaps:
            return {"status": "error", "message": "Market closed or no data yet"}

    mkt_snaps = oi_timeline.filter_market_hours(eval_day_snaps)
    eval_snaps = mkt_snaps if mkt_snaps else eval_day_snaps
    if not eval_snaps:
        return {"status": "error", "message": "Market closed or no data yet"}

    latest_ts, latest_spot, latest_df = eval_snaps[-1]
    time_str = target_time.strftime("%H:%M") if target_time else latest_ts.strftime("%H:%M")
    spot_str = f"{int(round(latest_spot)):,}"

    # Feed staleness check (> 6 minutes)
    is_stale_feed = False
    ref_dt = datetime.combine(target_date, target_time) if target_time else datetime.now()
    if target_date == ref_dt.date():
        age_sec = (ref_dt - latest_ts).total_seconds()
        if age_sec > 360:
            is_stale_feed = True

    # Market timings & states
    mkt_open_str = rules.get("observations_market_open_time", rules.get("market_open_time", "09:15"))
    try:
        mkt_open_t = datetime.strptime(str(mkt_open_str), "%H:%M").time()
    except Exception:
        mkt_open_t = dtime(9, 15)
    is_pre_market = (latest_ts.time() < mkt_open_t)

    freeze_start_str = rules.get("freeze_start", "12:45")
    freeze_end_str = rules.get("freeze_end", "13:15")
    try:
        f_start_t = datetime.strptime(freeze_start_str, "%H:%M").time()
        f_end_t = datetime.strptime(freeze_end_str, "%H:%M").time()
        is_freeze = (f_start_t <= latest_ts.time() <= f_end_t)
    except Exception:
        is_freeze = False

    eod_no_trade_str = rules.get("eod_no_trade_from", rules.get("eod_from", "15:00"))
    try:
        eod_t = datetime.strptime(eod_no_trade_str, "%H:%M").time()
        is_eod = (latest_ts.time() >= eod_t)
    except Exception:
        is_eod = (latest_ts.time() >= dtime(15, 0))

    # Data Gap Detection
    max_gap_sec = float(rules.get("data_gap", {}).get("max_gap_seconds", rules.get("max_gap_seconds", 180.0)))
    is_data_gap = False
    gap_msg = None
    if len(eval_snaps) >= 2:
        gap_prev_ts = eval_snaps[-2][0]
        actual_gap_sec = (latest_ts - gap_prev_ts).total_seconds()
        if actual_gap_sec > max_gap_sec:
            is_data_gap = True
            gap_mins = int(round(actual_gap_sec / 60.0))
            prev_str = gap_prev_ts.strftime("%H:%M")
            curr_str = latest_ts.strftime("%H:%M")
            gap_msg = f"⚠️ DATA GAP {prev_str}-{curr_str} ({gap_mins} min) — flow, dominance and streak signals invalid"

    # Multiplier
    lot_size = rules.get("display_lot_size", 65)
    mult = (lot_size / 100000.0) if rules.get("display_units") == "lakhs" else (1.0 / 100000.0)

    # 1. Day Range
    dr = compute_day_range(target_date, eval_snaps, all_snaps, target_time=latest_ts.time())
    low_str = f"{dr.day_low:,.2f}".rstrip('0').rstrip('.') if dr.day_low % 1 else f"{int(dr.day_low):,}"
    high_str = f"{dr.day_high:,.2f}" if dr.day_high % 1 else f"{int(dr.day_high):,}"
    day_range_line = f"- Day range: Low {low_str} ({dr.day_low_time}) ↔ High {high_str} ({dr.day_high_time})"

    # 2. GEX
    gex_data = compute_gex_metrics(latest_df, latest_spot, rules)
    total_gex_cr = gex_data.get("total_gex_cr", 0.0) if gex_data else 0.0
    regime = gex_data.get("regime", "N/A") if gex_data else "N/A"
    if not gex_data or gex_data.get("status") != "ok" or regime == "N/A":
        gex_line = "- GEX: N/A"
    elif regime == "LONG":
        sign = "-" if total_gex_cr < 0 else "+"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr LONG gamma (rangebound)"
    elif regime == "SHORT":
        sign = "+" if total_gex_cr >= 0 else "-"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr SHORT gamma (trending)"
    elif regime == "NEUTRAL":
        sign = "+" if total_gex_cr >= 0 else "-"
        gex_line = f"- GEX: {sign}₹{abs(total_gex_cr):.0f}Cr NEUTRAL gamma (no regime)"
    else:
        gex_line = f"- GEX: {regime}"

    # 3. RVOL
    rvol_line = "- RVOL: N/A"
    rvol_val = None
    regime_code = "NA"
    if not is_stale_feed:
        try:
            import rvol_gate
            rvol_val = rvol_gate.compute_rvol(target_date, latest_ts.time())
            if rvol_val is not None:
                regime_code = rvol_gate.get_rvol_regime(rvol_val)
                regime_label = (
                    "DEAD ZONE" if regime_code == "DEAD_ZONE"
                    else ("TRAP ZONE" if regime_code == "TRAP_ZONE"
                    else ("TREND ZONE" if regime_code == "TREND_ZONE"
                    else ("EXTREME ZONE" if regime_code == "EXTREME_ZONE" else regime_code)))
                )
                rvol_line = f"- RVOL: {rvol_val:.2f}× {regime_label}"
        except Exception as exc:
            logger.warning(f"Error computing RVOL for snapshot: {exc}")

    # 4. IV
    iv_data = compute_iv_context(latest_df, latest_spot, latest_ts, target_date, rules)
    pct = iv_data.get("percentile") if iv_data else None
    label = iv_data.get("label") if iv_data else None
    status = iv_data.get("status") if iv_data else None
    if status == "building_history":
        iv_line = "- IV: building history"
    elif status == "feed_anomaly":
        polls = iv_data.get("consecutive_carries", 0)
        iv_line = f"- IV: unavailable (feed anomaly {polls} polls)"
    elif pct is not None and label:
        pct_round = int(round(pct))
        iv_line = f"- IV: {_ordinal_suffix(pct_round)} pct {label}"
    else:
        iv_line = "- IV: N/A"

    # 5. Support / Resistance (Floor & Ceiling)
    w = analytics.window(latest_df, latest_spot, n=getattr(config, "WINDOW_STRIKES", 5) + 3)
    above_spot = w[w["strike"] >= latest_spot].dropna(subset=["ce_oi"])
    if len(above_spot) < 2:
        above_spot = latest_df[latest_df["strike"] >= latest_spot].dropna(subset=["ce_oi"])
    below_spot = w[w["strike"] <= latest_spot].dropna(subset=["pe_oi"])
    if len(below_spot) < 2:
        below_spot = latest_df[latest_df["strike"] <= latest_spot].dropna(subset=["pe_oi"])

    ce_sorted = above_spot.nlargest(2, "ce_oi").sort_values(by="strike", ascending=True)
    r1_row = ce_sorted.iloc[0] if len(ce_sorted) > 0 else None
    r2_row = ce_sorted.iloc[1] if len(ce_sorted) > 1 else r1_row

    pe_sorted = below_spot.nlargest(2, "pe_oi").sort_values(by="strike", ascending=False)
    s1_row = pe_sorted.iloc[0] if len(pe_sorted) > 0 else None
    s2_row = pe_sorted.iloc[1] if len(pe_sorted) > 1 else s1_row

    s1_strike = float(s1_row["strike"]) if s1_row is not None else None
    r1_strike = float(r1_row["strike"]) if r1_row is not None else None
    s2_strike = float(s2_row["strike"]) if s2_row is not None else None
    r2_strike = float(r2_row["strike"]) if r2_row is not None else None

    if s1_row is not None:
        pe_oi_l = int(round(float(s1_row.get("pe_oi", 0) or 0) * mult))
        floor_str = f"{int(round(float(s1_row['strike']))):,} PE [{pe_oi_l}L]"
    else:
        floor_str = "N/A"

    if r1_row is not None:
        ce_oi_l = int(round(float(r1_row.get("ce_oi", 0) or 0) * mult))
        ceil_str = f"{int(round(float(r1_row['strike']))):,} CE [{ce_oi_l}L]"
    else:
        ceil_str = "N/A"

    floor_ceil_line = f"- Floor: {floor_str} | Ceiling: {ceil_str}"

    # 6. Flow (Total puts +- and calls +- over whole window: 5 min or 15 min)
    win_mins = 15 if tf == "15m" else 5
    win_start_dt = latest_ts - timedelta(minutes=win_mins)
    prior_snaps = [s for s in eval_snaps if s[0] <= win_start_dt]
    snap_start = prior_snaps[-1] if prior_snaps else eval_snaps[0]

    df_curr = latest_df.set_index("strike")
    df_start = snap_start[2].set_index("strike")
    common_stks = df_curr.index.intersection(df_start.index)

    if is_data_gap:
        d_pe_win = 0.0
        d_ce_win = 0.0
        flow_line = "- Flow: INVALID (data gap)"
        flow_tg_line = f"Flow ({tf}): INVALID (data gap)"
    else:
        d_pe_win = float((df_curr.loc[common_stks, "pe_oi"] - df_start.loc[common_stks, "pe_oi"]).sum())
        d_ce_win = float((df_curr.loc[common_stks, "ce_oi"] - df_start.loc[common_stks, "ce_oi"]).sum())

        if abs(d_pe_win) >= 1000:
            pe_txt = f"Puts adding {d_pe_win/1000:+.0f}k" if d_pe_win > 0 else f"Puts leaving {d_pe_win/1000:.0f}k"
        else:
            pe_txt = f"Puts adding {d_pe_win:+.0f}" if d_pe_win > 0 else (f"Puts leaving {d_pe_win:+.0f}" if d_pe_win < 0 else "Puts flat")

        if abs(d_ce_win) >= 1000:
            ce_txt = f"Calls adding {d_ce_win/1000:+.0f}k" if d_ce_win > 0 else f"Calls leaving {d_ce_win/1000:.0f}k"
        else:
            ce_txt = f"Calls adding {d_ce_win:+.0f}" if d_ce_win > 0 else (f"Calls leaving {d_ce_win:+.0f}" if d_ce_win < 0 else "Calls flat")

        flow_line = f"- Flow: {pe_txt} | {ce_txt}"
        flow_tg_line = f"Flow ({tf}): {pe_txt} | {ce_txt}"

    # 7. Dominance & Structure
    dom_data = compute_writer_dominance(
        latest_df,
        latest_spot,
        rules,
        s1_strike=s1_strike,
        r1_strike=r1_strike,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
    )
    dom_line = None
    struct_line = None
    if dom_data and dom_data.get("enabled", True):
        d_val = dom_data.get("obs_dominance")
        s_val = dom_data.get("obs_structure")
        if d_val:
            dom_line = f"- Dominance: {d_val}"
        if s_val:
            struct_line = f"- Structure: {s_val}"

    # 8. Floor Weakening Streak
    floor_weakening_streak = 0
    atm = float(round(latest_spot / 50.0) * 50.0)
    tl = oi_timeline.evaluate_strike_timeline(mkt_snaps=eval_snaps, strike=atm, sess_date=target_date, rules=rules)
    if tl and tl.series and not is_data_gap:
        resample_mins = rules.get("resample_minutes", 3)
        step_15m = max(1, 15 // resample_mins)
        for idx in range(len(tl.series) - 1, 0, -1):
            t_c = tl.series[idx].timestamp
            t_p_target = t_c - timedelta(minutes=15)
            pt_p = None
            for j in range(idx - 1, -1, -1):
                if tl.series[j].timestamp <= t_p_target:
                    pt_p = tl.series[j]
                    break
            if pt_p is None:
                p_idx = max(0, idx - step_15m)
                pt_p = tl.series[p_idx]
            d_pe_b = tl.series[idx].pe_chg_oi - pt_p.pe_chg_oi
            d_ce_b = tl.series[idx].ce_chg_oi - pt_p.ce_chg_oi
            if d_pe_b < 0 and d_ce_b > 0:
                floor_weakening_streak += 1
            else:
                break

    min_consec = int(rules.get("floor_weakening_consecutive_bars", 2))
    floor_weakening_line = None
    if not is_data_gap and floor_weakening_streak >= min_consec:
        floor_weakening_line = f"- Floor weakening: {_ordinal_suffix(floor_weakening_streak)} consecutive"

    # 9. Checklist
    chk_data = compute_pretrade_checklist(
        s1_row=s1_row,
        r1_row=r1_row,
        d_pe_15m=d_pe_win,
        d_ce_15m=d_ce_win,
        iv_percentile=iv_data.get("percentile") if iv_data else None,
        gex_data=gex_data,
        latest_ts=latest_ts,
        target_date=target_date,
        target_time=latest_ts.time(),
        rules=rules,
        iv_data=iv_data,
        is_data_gap=is_data_gap,
        gap_msg=gap_msg,
    )
    score = chk_data.get("score", 0) if chk_data else 0
    total_boxes = len(chk_data.get("boxes", [])) if chk_data else 6
    failed_tags = chk_data.get("failed_tags", []) if chk_data else []
    if score == total_boxes:
        verdict = "TRADE"
        fail_txt = ""
    elif score == total_boxes - 1:
        verdict = "CAUTION"
        fail_txt = f" ({', '.join(failed_tags)} failing)" if failed_tags else ""
    else:
        verdict = "WAIT"
        fail_txt = f" ({', '.join(failed_tags)} failing)" if failed_tags else ""
    checklist_line = f"- Checklist: {score}/{total_boxes} {verdict}{fail_txt}"

    # Bias
    if is_pre_market:
        bias_word = "PRE-MARKET"
    elif is_data_gap:
        bias_word = "INVALID (data gap)"
    elif is_freeze:
        bias_word = "FREEZE WINDOW ⚠️"
    elif is_eod:
        bias_word = "EOD UNWIND ⚠️"
    elif dom_data and dom_data.get("agg_side") == "PUTS" and dom_data.get("intensity_label") in ("HEAVY", "CLEAR"):
        bias_word = "BULLISH ▲"
    elif dom_data and dom_data.get("agg_side") == "CALLS" and dom_data.get("intensity_label") in ("HEAVY", "CLEAR"):
        bias_word = "BEARISH ▼"
    else:
        bias_word = "NEUTRAL →"

    # 10. Action Line (STAND ASIDE / WATCH FOR PULLBACK / READY SHORT / READY LONG)
    _exp_unwind = _expiry_unwind_now(target_date, latest_ts, latest_df, rules)   # OBSFIX-1
    if _exp_unwind:
        bias_word = "EXPIRY UNWIND ⚠️"
    if is_pre_market or is_data_gap or is_freeze or is_eod or is_stale_feed or _exp_unwind:
        act_str = "STAND ASIDE"
    elif regime_code == "DEAD_ZONE" or (rvol_val is not None and rvol_val < float(rules.get("rvol", {}).get("dead_zone", 0.85))):
        act_str = "STAND ASIDE"
    elif score < 4:
        act_str = "STAND ASIDE"
    elif "BULLISH" in bias_word:
        act_str = "READY LONG" if score >= 5 else "WATCH FOR PULLBACK"
    elif "BEARISH" in bias_word:
        act_str = "READY SHORT" if score >= 5 else "WATCH FOR PULLBACK"
    else:
        act_str = "STAND ASIDE"
    action_line = f"- Action: {act_str}"

    # 11. mj_shadow
    mj_shadow_line = None
    shadow_enabled = rules.get("mj_mode", {}).get("shadow_logging", True)
    if not is_pre_market and shadow_enabled:
        mj_floor_str = f"{int(round(float(s1_row['strike']))):,} PE [{int(round(float(s1_row.get('pe_oi', 0) or 0) * mult))}L]" if s1_row is not None else "N/A"
        mj_ceil_str = f"{int(round(float(r1_row['strike']))):,} CE [{int(round(float(r1_row.get('ce_oi', 0) or 0) * mult))}L]" if r1_row is not None else "N/A"
        if rvol_val is not None:
            zone_short = regime_code.replace("_ZONE", "").strip() if regime_code else "N/A"
            rvol_str = f"{rvol_val:.2f} {zone_short}"
        else:
            rvol_str = "N/A"
        mj_shadow_line = f"- mj_shadow: CE setup {mj_floor_str} | PE setup {mj_ceil_str} | RVOL {rvol_str}"

    # 12. Alerts fired inside the window
    alerts_fired = []
    app_alert = check_approaching_ceiling_floor(latest_spot, latest_df, r1_row, r2_row, s1_row, s2_row, rules, mult)
    if app_alert:
        for al_sub in app_alert.split("\n"):
            if "APPROACHING" in al_sub or "Spot" in al_sub:
                alerts_fired.append(al_sub.replace("⚠️ ", "").strip())
    trap_alert = check_s1_liquidity_trap(eval_snaps, s1_strike, latest_spot, rules)
    if trap_alert:
        alerts_fired.append("LIQUIDITY_TRAP (S1 breakdown failure)")
    if floor_weakening_streak >= 5:
        alerts_fired.append(f"FLOOR_WEAKENING ({floor_weakening_streak}th consecutive)")

    alert_line = None
    if alerts_fired:
        alert_line = "\n".join([f"- Alert: [{al}]" for al in alerts_fired])

    # Writer Lines evaluation for 5m close
    writer_lines_res = None
    if tf == "5m":
        try:
            writer_lines_res = oi_timeline.get_writer_lines_bar_live(eval_snaps, target_date, rules)
        except Exception as exc:
            logger.warning(f"Error evaluating writer lines bar: {exc}")

    # Observation Entry Formatting (uses single canonical writer with tf tag)
    obs_text = None
    if append_obs and rules.get("observations_enabled", True):
        try:
            obs_text = append_observation_log(
                target_date=target_date,
                latest_ts=latest_ts,
                latest_spot=latest_spot,
                bias_word=bias_word,
                gex_data=gex_data,
                iv_data=iv_data,
                s1_row=s1_row,
                r1_row=r1_row,
                mult=mult,
                d_pe_15m=d_pe_win,
                d_ce_15m=d_ce_win,
                floor_weakening_streak=floor_weakening_streak,
                chk_data=chk_data,
                action_line=act_str,
                rules=rules,
                eval_snaps=eval_snaps,
                all_snaps=all_snaps,
                latest_df=latest_df,
                r1_strike=r1_strike,
                r2_strike=r2_strike,
                s1_strike=s1_strike,
                s2_strike=s2_strike,
                alert_line=alert_line,
                dom_data=dom_data,
                is_data_gap=is_data_gap,
                gap_msg=gap_msg,
                rvol_val=rvol_val if rvol_val is not None else "DEFAULT",
                tf=tf,
                writer_lines_str=writer_lines_res.read.read_text if (writer_lines_res and writer_lines_res.read) else None,
            )
        except Exception as exc:
            logger.warning(f"Error appending periodic snapshot to observation log: {exc}")

    # Telegram Formatting
    tg_marker = "🟦 5-MIN" if tf == "5m" else "🟪 15-MIN"
    tg_header = f"{tg_marker} | {time_str} | Spot {spot_str} | {bias_word}"
    if is_stale_feed:
        tg_header += " | ⚠️ FEED STALE"

    tg_lines = [
        tg_header,
        f"Day range: Low {low_str} ({dr.day_low_time}) ↔ High {high_str} ({dr.day_high_time})",
        gex_line.replace("- GEX: ", "GEX: "),
        rvol_line.replace("- RVOL: ", "RVOL: "),
        iv_line.replace("- IV: ", "IV: "),
        floor_ceil_line.replace("- Floor: ", "Floor: "),
        flow_tg_line,
    ]
    if dom_line:
        tg_lines.append(dom_line.replace("- Dominance: ", "Dominance: "))
    if struct_line:
        tg_lines.append(struct_line.replace("- Structure: ", "Structure: "))
    if floor_weakening_line:
        tg_lines.append(floor_weakening_line.replace("- Floor weakening: ", "Floor weakening: "))
    tg_lines.append(checklist_line.replace("- Checklist: ", "Checklist: "))
    tg_lines.append(f"Action: {act_str}")
    if mj_shadow_line:
        tg_lines.append(mj_shadow_line.replace("- mj_shadow: ", "mj_shadow: "))
    if alert_line:
        for al in alert_line.split("\n"):
            tg_lines.append(al.replace("- Alert: ", "Alert: "))

    telegram_text = "\n".join(tg_lines)

    return {
        "timeframe": tf,
        "timestamp": latest_ts,
        "spot": latest_spot,
        "bias": bias_word,
        "is_stale_feed": is_stale_feed,
        "telegram_text": telegram_text,
        "observation_text": obs_text,
        "writer_lines_table": writer_lines_res.table_text if writer_lines_res else None,
        "writer_lines_res": writer_lines_res,
        "fields": {
            "day_range": day_range_line,
            "gex": gex_line,
            "rvol": rvol_line,
            "iv": iv_line,
            "floor_ceil": floor_ceil_line,
            "flow": flow_line,
            "dominance": dom_line,
            "structure": struct_line,
            "floor_weakening": floor_weakening_line,
            "writer_lines": writer_lines_res.table_text if writer_lines_res else None,
            "writer_lines_read": writer_lines_res.read.read_text if (writer_lines_res and writer_lines_res.read) else None,
            "checklist": checklist_line,
            "action": action_line,
            "mj_shadow": mj_shadow_line,
            "alerts": alert_line,
        }
    }


def dispatch_telegram_snapshot(
    snapshot_res: Dict[str, Any],
    rules: dict,
    target_date: date,
    latest_ts: Optional[datetime],
) -> None:
    """
    Sends the periodic snapshot Telegram message if enabled and market hours.
    """
    try:
        if not rules.get("telegram_briefing_enabled", True):
            return
        if not snapshot_res or not snapshot_res.get("telegram_text") or latest_ts is None:
            return
        now_dt = datetime.now()
        mkt_open = dtime(9, 15, 0)
        mkt_close = dtime(15, 30, 0)
        if not (mkt_open <= now_dt.time() <= mkt_close):
            return
        stale_threshold_sec = float(rules.get("stale_minutes", 10)) * 60.0
        age_sec = (now_dt - latest_ts).total_seconds()
        if target_date != now_dt.date() or age_sec > stale_threshold_sec:
            print("STALE - Telegram snapshot skipped")
            return
        send_telegram_alert(snapshot_res["telegram_text"])

        # Also dispatch 5-min IMPULSE alert if fired
        w_res = snapshot_res.get("writer_lines_res")
        if w_res and w_res.read and w_res.read.is_impulse:
            oi_timeline.dispatch_writer_alerts(crossover=None, read=w_res.read, rules=rules, send_func=send_telegram_alert)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Telegram snapshot dispatch failed: {exc}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4. CLI Entrypoint
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    target_date = date.today()
    target_time: Optional[dtime] = None

    for arg in sys.argv[1:]:
        arg_s = arg.strip()
        if "-" in arg_s and len(arg_s) == 10:
            try:
                target_date = datetime.strptime(arg_s, "%Y-%m-%d").date()
            except ValueError:
                pass
        elif ":" in arg_s:
            try:
                if len(arg_s) == 5:
                    target_time = datetime.strptime(arg_s, "%H:%M").time().replace(second=59, microsecond=999999)
                elif len(arg_s) == 8:
                    target_time = datetime.strptime(arg_s, "%H:%M:%S").time().replace(microsecond=999999)
            except ValueError:
                pass

    is_live = any(arg in sys.argv for arg in ("--live", "--append-obs", "--daemon"))

    if "--all" in sys.argv or "--trio" in sys.argv:
        output = build_briefing(target_date, target_time, append_obs=is_live)
        tg_3m = getattr(build_briefing, "telegram_briefing", None) or output

        snap_5m = build_periodic_snapshot("5m", target_date, target_time, append_obs=is_live)
        tg_5m = snap_5m.get("telegram_text", "No 5m snapshot available")

        snap_15m = build_periodic_snapshot("15m", target_date, target_time, append_obs=is_live)
        tg_15m = snap_15m.get("telegram_text", "No 15m snapshot available")

        sep = "═" * 54
        print(f"\n{sep}\n📊 3-MIN BRIEFING\n{sep}\n{tg_3m}\n")
        print(f"{sep}\n{tg_5m}\n")
        print(f"{sep}\n{tg_15m}\n{sep}\n")
        return

    if "--snapshot" in sys.argv:
        idx = sys.argv.index("--snapshot")
        snap_tf = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "5m"
        snap_res = build_periodic_snapshot(snap_tf, target_date, target_time, append_obs=is_live)
        print(snap_res.get("telegram_text", "No snapshot available"))
        if is_live:
            dispatch_telegram_snapshot(snap_res, load_briefing_rules(), target_date, snap_res.get("timestamp"))
        return

    if "--replay-snapshots" in sys.argv:
        # Replay periodic snapshots over a time range
        # Parse times from sys.argv
        times_parsed = []
        for arg in sys.argv[1:]:
            if ":" in arg and not arg.startswith("--"):
                try:
                    times_parsed.append(datetime.strptime(arg[:5], "%H:%M").time())
                except Exception:
                    pass
        start_t = times_parsed[0] if len(times_parsed) >= 1 else dtime(11, 0)
        end_t = times_parsed[1] if len(times_parsed) >= 2 else dtime(11, 35)

        day_snaps = store.load_day(target_date)
        cur_dt = datetime.combine(target_date, start_t)
        end_dt = datetime.combine(target_date, end_t)
        while cur_dt <= end_dt:
            tf = get_snapshot_tf_for_time(cur_dt.time())
            if tf:
                res = build_periodic_snapshot(tf, target_date, cur_dt.time(), append_obs=is_live, all_snaps=day_snaps)
                if res and res.get("telegram_text"):
                    print(f"[{cur_dt.strftime('%H:%M')} {tf}]")
                    print(res["telegram_text"])
                    print("-" * 50)
            cur_dt += timedelta(minutes=5)
        return

    try:
        output = build_briefing(target_date, target_time, append_obs=is_live)
        print(output)
    except Exception as err:
        logger.error(f"Error generating briefing: {err}", exc_info=True)
        print("Market closed or no data yet")
        return

    rules = load_briefing_rules()
    latest_ts = getattr(build_briefing, "last_snapshot_ts", None)
    tg_briefing = getattr(build_briefing, "telegram_briefing", None)
    freeze_end_alert = getattr(build_briefing, "freeze_end_alert", None)
    approach_alert = getattr(build_briefing, "approach_alert", None)
    trap_alert = getattr(build_briefing, "trap_alert", None)
    eod_summary = getattr(build_briefing, "eod_summary", None)

    # Print any special alerts to terminal
    if freeze_end_alert:
        print("\n" + freeze_end_alert)
    if approach_alert:
        print("\n" + approach_alert)
    if trap_alert:
        print("\n" + trap_alert)
    if eod_summary:
        print("\n" + eod_summary)

    if is_live:
        dispatch_telegram_briefing(
            output,
            rules,
            target_date,
            latest_ts,
            tg_briefing=tg_briefing,
            freeze_end_alert=freeze_end_alert,
            approach_alert=approach_alert,
            trap_alert=trap_alert,
            eod_summary=eod_summary,
        )


if __name__ == "__main__":
    main()
