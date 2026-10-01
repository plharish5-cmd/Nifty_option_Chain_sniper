"""
oi_timeline.py — Per-Strike Intraday OI Change Timeline Engine.

WHAT THIS MODULE DOES:
──────────────────────
- Computes per-strike cumulative intraday OI change (dCE, dPE) from market open to close.
- Uses stored `ce_chg_oi` and `pe_chg_oi` as the PRIMARY series (measured vs previous close).
- Computes baseline subtraction `oi(t) - oi(t0)` as a SECONDARY series and verifies agreement.
- Strictly filters market hours: only 09:15:00 to 15:30:00 IST eligible; asserts first snapshot >= 09:15.
- Evaluates per-session coverage gate ((eligible * cadence) / 375 mins) and max in-session gaps.
- Handles roll hazard (detects discontinuous jumps in total OI across consecutive snapshots).
- Resamples session snapshots to a uniform time grid (default 3 min) so minute-based durations
  (`crossover_confirm_minutes`, `unwind_lookback_minutes`) remain invariant across cadence changes.
- Disambiguates buyers vs sellers using `analytics.quadrant_strict` / `analytics.quadrant`,
  reporting `AGREE` vs `DISAGREE` between combined mode and naive OI-only reading.
- Operates strictly in CONTRACTS by default; only renders lakhs when `lot_size` is explicitly configured.

WHAT THIS MODULE CANNOT DO:
───────────────────────────
- It CANNOT emit BUY or SELL execution signals. Alert-only; positioning read and veto only.
- It CANNOT invent historical data. It relies solely on snapshots captured by `store.py`.
- It CANNOT guess expiry dates for historical sessions where unrecorded: marks `expiry_unknown=True`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import logging

import config
import analytics

logger = logging.getLogger("oi_timeline")

# ═══════════════════════════════════════════════════════════════════════════
# 1. Configuration Loader
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_RULES_PATH = Path(__file__).parent / "strategy1_rules.json"

MKT_OPEN_TIME = dtime(9, 15, 0)
MKT_CLOSE_TIME = dtime(15, 30, 0)
MKT_SESSION_MINUTES = 375.0  # 09:15 to 15:30 IST


def load_rules(rules_path: Optional[Path] = None) -> dict:
    """Loads oi_timeline rules from strategy1_rules.json with strict fallbacks."""
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "colors": {
            "ce": "#EF4444",
            "pe": "#10B981",
        },
        "interpretation_mode": "combined",
        "max_strikes": 5,
        "resample_minutes": 3,
        "warmup_until": "09:50",
        "eod_from": "15:00",
        "crossover_confirm_minutes": 9,
        "unwind_lookback_minutes": 9,
        "stale_minutes": 10,
        "min_coverage": 0.80,
        "dominance_min_contracts": 50000.0,
        "dominance_min_lakhs": 5.0,
        "divergence_tolerance_contracts": 2500.0,
        "roll_jump_threshold_pct": 0.35,
        "quadrant_local_minutes": 15,
        "max_contiguous_missing_bins": 8,
        "max_divergence_contracts": 50000.0,
        "lot_size": None,
    }

    if not path.exists():
        return defaults

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            oi_cfg = data.get("oi_timeline", {})
            return {**defaults, **oi_cfg}
    except Exception as err:
        print(f"[oi_timeline] Warning: failed to parse strategy1_rules.json ({err}). Using defaults.")
        return defaults


# ═══════════════════════════════════════════════════════════════════════════
# 2. Data Structures (@dataclass per result)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SessionCoverage:
    """Coverage audit for a single session date."""
    session_date: str
    total_snapshots: int
    eligible_snapshots: int
    cadence_minutes: float
    occupied_bins: int
    total_bins: int
    coverage_pct: float            # (occupied_bins / 125) * 100
    old_coverage_pct: float        # (eligible * cadence / 375) * 100
    max_gap_minutes: float
    max_contiguous_missing_bins: int
    first_snapshot_time: Optional[str]
    last_snapshot_time: Optional[str]
    passes_gate: bool
    baseline_late: bool
    roll_hazard_detected: bool
    status_note: str
    divergence_flag: bool = False
    max_divergence_contracts: float = 0.0
    missed_opening_oi_contracts: float = 0.0
    missed_opening_strike: Optional[float] = None
    lateness_minutes: float = 0.0


@dataclass
class StrikePoint:
    """Data point for a watched strike at a specific time."""
    timestamp: datetime
    clock_time: str
    spot: float
    ce_oi: float
    pe_oi: float
    ce_chg_oi: float               # Primary dCE
    pe_chg_oi: float               # Primary dPE
    secondary_dce: float           # oi(t) - oi(t0)
    secondary_dpe: float           # oi(t) - oi(t0)
    ce_ltp: float
    pe_ltp: float
    ce_ltp_chg: float              # NSE-provided change vs prev close
    pe_ltp_chg: float              # NSE-provided change vs prev close
    intraday_ce_ltp_chg: float     # ltp(t) - ltp(t0)
    intraday_pe_ltp_chg: float     # ltp(t) - ltp(t0)
    spread: float                  # dPE - dCE
    pcr: float                     # pe_oi / ce_oi
    dominance: str                 # WARMUP, PE_DOMINANT, CE_DOMINANT, BALANCED, CROSSOVER, EOD_UNWIND, EXPIRY_DAY
    flow: str                      # BUILDING, UNWINDING, FLAT
    state: str                     # Alias for dominance (backwards compatibility)
    state_duration_minutes: int
    ce_quadrant_cum: str           # long_buildup, short_buildup, short_covering, long_unwinding
    pe_quadrant_cum: str
    ce_quadrant_local: str         # over last quadrant_local_minutes
    pe_quadrant_local: str
    agreement_cum: str             # AGREE or DISAGREE (naive vs cum)
    agreement_local: str           # AGREE or DISAGREE (naive local vs local)
    cum_vs_local_agreement: str    # AGREE or DISAGREE (cum vs local flow)
    naive_interpretation: str      # e.g. "PE WRITING"
    combined_interpretation: str   # e.g. "PE WRITING" vs "PE BUYING"
    combined_ce_quadrant: str = "" # Backwards compatibility alias to ce_quadrant_cum
    combined_pe_quadrant: str = "" # Backwards compatibility alias to pe_quadrant_cum
    agreement: str = "AGREE"       # Backwards compatibility alias to agreement_cum


@dataclass
class StrikeSummaryStats:
    """Latest status summary for a strike."""
    strike: float
    current_spot: float
    ce_oi: float
    pe_oi: float
    dce: float                     # In configured units (contracts or lakhs)
    dpe: float                     # In configured units (contracts or lakhs)
    spread: float                  # dPE - dCE in configured units
    pcr: float
    dominance: str
    flow: str
    state: str                     # Alias for dominance
    state_duration_minutes: int
    interpretation_mode: str
    ce_quadrant_cum: str
    pe_quadrant_cum: str
    ce_quadrant_local: str
    pe_quadrant_local: str
    agreement_cum: str
    agreement_local: str
    cum_vs_local_agreement: str
    agreement: str                 # Backwards compatibility alias
    veto_note: str
    divergence_warning: Optional[str] = None
    staleness_warning: Optional[str] = None
    combined_interpretation: str = ""
    units_label: str = "contracts"

    @property
    def stale_warning(self) -> Optional[str]:
        """Backwards compatibility alias for staleness_warning."""
        return self.staleness_warning


@dataclass
class StrikeTimelineResult:
    """Complete timeline output for one strike across the session."""
    strike: float
    session_date: str
    expiry: str
    expiry_unknown: bool
    is_expiry_day: bool
    baseline_late: bool
    divergence_detected: bool
    max_divergence_contracts: float
    series: List[StrikePoint]
    latest_stats: Optional[StrikeSummaryStats]
    roll_hazard: bool
    crossover_points: List[Tuple[str, str, float]]  # (time_str, direction, spread)


@dataclass
class SessionTimelineResult:
    """Complete multi-strike timeline package for a session."""
    session_date: str
    expiry: str
    expiry_unknown: bool
    is_expiry_day: bool
    coverage: SessionCoverage
    atm_strike: float
    watched_strikes: List[float]
    strike_timelines: Dict[float, StrikeTimelineResult]
    resample_minutes: int
    interpretation_mode: str
    units_label: str
    lot_size: Optional[int]
    guard_warning: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Market-Hours Filtering & Roll Hazard Detection
# ═══════════════════════════════════════════════════════════════════════════

def filter_market_hours(
    snapshots: List[Tuple[datetime, float, pd.DataFrame]]
) -> List[Tuple[datetime, float, pd.DataFrame]]:
    """
    Strictly filters snapshots to 09:15:00 - 15:30:00 IST on the session date.
    Asserts first eligible snapshot is >= 09:15:00. Discards pre-market and after-hours data.
    """
    eligible: List[Tuple[datetime, float, pd.DataFrame]] = []
    for ts, spot, df in snapshots:
        if MKT_OPEN_TIME <= ts.time() <= MKT_CLOSE_TIME:
            eligible.append((ts, spot, df))

    eligible.sort(key=lambda item: item[0])
    if eligible and eligible[0][0].time() < MKT_OPEN_TIME:
        logger.error(
            "Fatal: First eligible snapshot %s is before market open %s!",
            eligible[0][0], MKT_OPEN_TIME
        )
        eligible = [x for x in eligible if x[0].time() >= MKT_OPEN_TIME]
    return eligible


def detect_roll_hazard(
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    threshold_pct: float = 0.35,
) -> List[Tuple[datetime, datetime, float, float, float]]:
    """
    Detects discontinuous jumps in total standing OI across consecutive snapshots.
    A jump > threshold_pct (default 35%) flags contract roll or multi-expiry concatenation.
    Returns list of (t_prev, t_curr, prev_oi, curr_oi, pct_change).
    """
    jumps: List[Tuple[datetime, datetime, float, float, float]] = []
    if len(snapshots) < 2:
        return jumps

    for i in range(1, len(snapshots)):
        t_prev, _, df_prev = snapshots[i - 1]
        t_curr, _, df_curr = snapshots[i]
        oi_prev = float(df_prev["ce_oi"].sum() + df_prev["pe_oi"].sum()) if "ce_oi" in df_prev else 0.0
        oi_curr = float(df_curr["ce_oi"].sum() + df_curr["pe_oi"].sum()) if "ce_oi" in df_curr else 0.0

        if oi_prev > 0:
            chg_pct = (oi_curr - oi_prev) / oi_prev
            if abs(chg_pct) >= threshold_pct:
                jumps.append((t_prev, t_curr, oi_prev, oi_curr, chg_pct))
    return jumps


def detect_multistrike_discontinuity(
    snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    rules: Optional[dict] = None,
    min_strikes: Optional[int] = None,
    threshold_contracts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Standing guard: Detects simultaneous multi-strike OI discontinuities across consecutive snapshots.
    Flags when >= min_strikes experience a single-step shift > threshold_contracts at the exact same moment.
    Thresholds read from strategy1_rules.json under oi_timeline.
    """
    r = rules or load_rules()
    oi_r = r.get("oi_timeline", r)
    m_strikes = min_strikes if min_strikes is not None else int(oi_r.get("multistrike_discontinuity_min_strikes", 3))
    thresh = threshold_contracts if threshold_contracts is not None else float(oi_r.get("multistrike_discontinuity_threshold_contracts", 50000.0))

    events = []
    if len(snapshots) < 2:
        return events

    for i in range(1, len(snapshots)):
        t_prev, _, df_prev = snapshots[i - 1]
        t_curr, _, df_curr = snapshots[i]

        merged = pd.merge(
            df_prev[["strike", "ce_oi", "pe_oi"]],
            df_curr[["strike", "ce_oi", "pe_oi"]],
            on="strike",
            suffixes=("_prev", "_curr"),
        )
        if merged.empty:
            continue

        merged["d_ce"] = merged["ce_oi_curr"] - merged["ce_oi_prev"]
        merged["d_pe"] = merged["pe_oi_curr"] - merged["pe_oi_prev"]

        ce_jumpers = merged[merged["d_ce"].abs() >= thresh]
        pe_jumpers = merged[merged["d_pe"].abs() >= thresh]

        if len(ce_jumpers) >= m_strikes or len(pe_jumpers) >= m_strikes:
            events.append({
                "timestamp_prev": t_prev,
                "timestamp_curr": t_curr,
                "ce_affected_strikes": ce_jumpers["strike"].tolist(),
                "pe_affected_strikes": pe_jumpers["strike"].tolist(),
                "max_ce_step": float(ce_jumpers["d_ce"].abs().max()) if not ce_jumpers.empty else 0.0,
                "max_pe_step": float(pe_jumpers["d_pe"].abs().max()) if not pe_jumpers.empty else 0.0,
            })
    return events


def compute_session_coverage(
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    sess_date: date,
    cadence_minutes: Optional[float] = None,
    rules: Optional[dict] = None,
) -> SessionCoverage:
    """
    Audits snapshot coverage during market hours (09:15:00 to 15:30:00 IST = 375 minutes).
    Cadence-invariant metric:
      coverage % = (3-min grid bins containing >= 1 snapshot) / 125 bins * 100.0
    Also computes max in-session gap.
    """
    r = rules or load_rules()
    min_cov = float(r.get("min_coverage", 0.80))
    date_str = sess_date.strftime("%Y-%m-%d")

    mkt_snaps = filter_market_hours(all_snapshots)
    n_eligible = len(mkt_snaps)

    # Cadence detection: 3.0 min through 2026-09-03, 1.0 min from 2026-09-04
    if cadence_minutes is not None:
        cadence = cadence_minutes
    elif date_str >= "2026-09-04":
        cadence = 1.0
    else:
        cadence = 3.0

    open_dt = datetime.combine(sess_date, MKT_OPEN_TIME)
    close_dt = datetime.combine(sess_date, MKT_CLOSE_TIME)
    TOTAL_BINS = 125  # 375 minutes / 3 minutes

    if n_eligible == 0:
        return SessionCoverage(
            session_date=date_str,
            total_snapshots=len(all_snapshots),
            eligible_snapshots=0,
            cadence_minutes=cadence,
            occupied_bins=0,
            total_bins=TOTAL_BINS,
            coverage_pct=0.0,
            old_coverage_pct=0.0,
            max_gap_minutes=MKT_SESSION_MINUTES,
            max_contiguous_missing_bins=TOTAL_BINS,
            first_snapshot_time=None,
            last_snapshot_time=None,
            passes_gate=False,
            baseline_late=True,
            roll_hazard_detected=False,
            status_note="NO_MARKET_HOURS_DATA",
            divergence_flag=False,
            max_divergence_contracts=0.0,
        )

    # Count 3-minute bins containing >= 1 snapshot
    bins_occupied = set()
    for ts, spot, df in mkt_snaps:
        mins_from_open = (ts - open_dt).total_seconds() / 60.0
        bin_idx = int(mins_from_open // 3.0)
        if 0 <= bin_idx < TOTAL_BINS:
            bins_occupied.add(bin_idx)
        elif bin_idx == TOTAL_BINS and ts.time() <= MKT_CLOSE_TIME:
            bins_occupied.add(TOTAL_BINS - 1)

    n_bins = len(bins_occupied)
    cov_pct = (n_bins / float(TOTAL_BINS)) * 100.0
    old_cov_pct = min(100.0, (n_eligible * cadence / MKT_SESSION_MINUTES) * 100.0)

    # Longest contiguous run of missing 3-minute bins
    missing_runs = []
    curr_run = 0
    for b in range(TOTAL_BINS):
        if b not in bins_occupied:
            curr_run += 1
        else:
            if curr_run > 0:
                missing_runs.append(curr_run)
                curr_run = 0
    if curr_run > 0:
        missing_runs.append(curr_run)
    max_missing_bins = max(missing_runs) if missing_runs else 0

    max_allowed_gap_bins = int(r.get("max_contiguous_missing_bins", 8))
    gap_exceeded = max_missing_bins > max_allowed_gap_bins

    # Gaps in minutes
    gaps = [(mkt_snaps[0][0] - open_dt).total_seconds() / 60.0]
    for i in range(1, n_eligible):
        gaps.append((mkt_snaps[i][0] - mkt_snaps[i - 1][0]).total_seconds() / 60.0)
    gaps.append((close_dt - mkt_snaps[-1][0]).total_seconds() / 60.0)
    max_gap = max(gaps)

    first_t = mkt_snaps[0][0]
    last_t = mkt_snaps[-1][0]
    baseline_late = first_t.time() > dtime(9, 20, 0)
    roll_jumps = detect_roll_hazard(mkt_snaps, threshold_pct=float(r.get("roll_jump_threshold_pct", 0.35)))
    has_roll = len(roll_jumps) > 0

    passes = (cov_pct >= (min_cov * 100.0)) and not has_roll and not gap_exceeded

    status = "PASS" if passes else "FAIL"
    if baseline_late:
        status += " (LATE_START)"
    if has_roll:
        status += " (ROLL_JUMP)"
    if gap_exceeded:
        status += f" (GAP_EXCEEDED: {max_missing_bins} bins = {max_missing_bins*3}m)"
    if n_eligible < 50:
        status += " (SPARSE)"

    return SessionCoverage(
        session_date=date_str,
        total_snapshots=len(all_snapshots),
        eligible_snapshots=n_eligible,
        cadence_minutes=cadence,
        occupied_bins=n_bins,
        total_bins=TOTAL_BINS,
        coverage_pct=round(cov_pct, 1),
        old_coverage_pct=round(old_cov_pct, 1),
        max_gap_minutes=round(max_gap, 1),
        max_contiguous_missing_bins=max_missing_bins,
        first_snapshot_time=first_t.strftime("%H:%M:%S"),
        last_snapshot_time=last_t.strftime("%H:%M:%S"),
        passes_gate=passes,
        baseline_late=baseline_late,
        roll_hazard_detected=has_roll,
        status_note=status,
        divergence_flag=False,
        max_divergence_contracts=0.0,
        missed_opening_oi_contracts=0.0,
        missed_opening_strike=None,
        lateness_minutes=round(max(0.0, (first_t - open_dt).total_seconds() / 60.0), 1),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Resampling & Per-Strike Timeline Engine
# ═══════════════════════════════════════════════════════════════════════════

def _resample_series_grid(
    mkt_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    strike: float,
    sess_date: date,
    resample_minutes: int = 3,
) -> pd.DataFrame:
    """
    Extracts strike records and resamples them to a uniform time grid
    from 09:15:00 to 15:30:00 at resample_minutes intervals using asof / forward-fill.
    """
    records = []
    for ts, spot, df in mkt_snaps:
        row = df[df["strike"] == strike]
        if not row.empty:
            r = row.iloc[0]
            records.append({
                "timestamp": ts,
                "spot": spot,
                "ce_oi": float(r.get("ce_oi", 0) or 0),
                "pe_oi": float(r.get("pe_oi", 0) or 0),
                "ce_chg_oi": float(r.get("ce_chg_oi", 0) or 0),
                "pe_chg_oi": float(r.get("pe_chg_oi", 0) or 0),
                "ce_ltp": float(r.get("ce_ltp", 0) or 0),
                "pe_ltp": float(r.get("pe_ltp", 0) or 0),
                "ce_chg": float(r.get("ce_chg", 0) or 0),
                "pe_chg": float(r.get("pe_chg", 0) or 0),
            })

    if not records:
        return pd.DataFrame()

    df_raw = pd.DataFrame(records)
    df_raw = df_raw.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    df_raw = df_raw.set_index("timestamp")

    # Generate uniform target grid: 09:15 to 15:30
    grid_start = datetime.combine(sess_date, MKT_OPEN_TIME)
    # Align grid start to first actual timestamp if after 09:15
    start_point = max(grid_start, df_raw.index[0])
    grid_end = datetime.combine(sess_date, MKT_CLOSE_TIME)

    target_grid = []
    curr = start_point
    while curr <= grid_end and curr <= df_raw.index[-1]:
        target_grid.append(curr)
        curr += timedelta(minutes=resample_minutes)

    # Ensure the final snapshot timestamp is included so the timeline reaches session close / latest snapshot
    if target_grid and df_raw.index[-1] > target_grid[-1]:
        target_grid.append(df_raw.index[-1])

    if not target_grid:
        return df_raw.reset_index()

    # Reindex via forward-fill
    df_resampled = df_raw.reindex(df_raw.index.union(target_grid)).ffill().loc[target_grid]
    df_resampled.index.name = "timestamp"
    return df_resampled.reset_index()


def evaluate_strike_timeline(
    mkt_snaps: List[Tuple[datetime, float, pd.DataFrame]],
    strike: float,
    sess_date: date,
    expiry: str = "",
    expiry_unknown: bool = True,
    is_expiry_day: bool = False,
    rules: Optional[dict] = None,
) -> StrikeTimelineResult:
    """
    Builds the complete intraday OI change timeline for a single watched strike:
      - Primary series: ce_chg_oi, pe_chg_oi (NSE change vs prev close)
      - Secondary series: baseline subtraction oi(t) - oi(t0)
      - Asserts agreement within tolerance when first snapshot <= 09:20:00
      - Disambiguates buyer vs seller via quadrant analysis (combined vs naive)
      - Tracks state transitions and crossovers with minute-based durations.
    """
    r = rules or load_rules()
    resample_min = int(r.get("resample_minutes", 3))
    confirm_min = int(r.get("crossover_confirm_minutes", 9))
    unwind_min = int(r.get("unwind_lookback_minutes", 9))
    local_min = int(r.get("quadrant_local_minutes", 15))
    warmup_str = r.get("warmup_until", "09:50")
    eod_str = r.get("eod_from", "15:00")
    dom_min = float(r.get("dominance_min_contracts", 50000.0))
    tol_contracts = float(r.get("divergence_tolerance_contracts", 2500.0))
    stale_min = int(r.get("stale_minutes", 10))
    mode = r.get("interpretation_mode", "combined")

    warmup_time = datetime.strptime(warmup_str, "%H:%M").time()
    eod_time = datetime.strptime(eod_str, "%H:%M").time()

    # Check baseline late
    first_snap_t = mkt_snaps[0][0] if mkt_snaps else datetime.now()
    baseline_late = first_snap_t.time() > dtime(9, 20, 0)

    # Roll hazard
    jumps = detect_roll_hazard(mkt_snaps, threshold_pct=float(r.get("roll_jump_threshold_pct", 0.35)))
    has_roll = len(jumps) > 0

    # Resample strike snapshots
    df_grid = _resample_series_grid(mkt_snaps, strike, sess_date, resample_min)
    if df_grid.empty:
        return StrikeTimelineResult(
            strike=strike,
            session_date=sess_date.strftime("%Y-%m-%d"),
            expiry=expiry,
            expiry_unknown=expiry_unknown,
            is_expiry_day=is_expiry_day,
            baseline_late=baseline_late,
            divergence_detected=False,
            max_divergence_contracts=0.0,
            series=[],
            latest_stats=None,
            roll_hazard=has_roll,
            crossover_points=[],
        )

    # Baseline OI for secondary series
    base_ce_oi = float(df_grid.iloc[0]["ce_oi"])
    base_pe_oi = float(df_grid.iloc[0]["pe_oi"])

    # Baseline LTP for intraday premium change (first non-zero price)
    valid_ce = df_grid[df_grid["ce_ltp"] > 0]
    base_ce_ltp = float(valid_ce.iloc[0]["ce_ltp"]) if not valid_ce.empty else 0.0
    valid_pe = df_grid[df_grid["pe_ltp"] > 0]
    base_pe_ltp = float(valid_pe.iloc[0]["pe_ltp"]) if not valid_pe.empty else 0.0

    # Verification: Primary vs Secondary divergence check
    divergence_detected = False
    max_divergence = 0.0
    for _, row in df_grid.iterrows():
        pri_dce = float(row["ce_chg_oi"])
        sec_dce = float(row["ce_oi"]) - base_ce_oi
        pri_dpe = float(row["pe_chg_oi"])
        sec_dpe = float(row["pe_oi"]) - base_pe_oi

        diff_c = abs(pri_dce - sec_dce)
        diff_p = abs(pri_dpe - sec_dpe)
        diff_max = max(diff_c, diff_p)
        if diff_max > max_divergence:
            max_divergence = diff_max
        if not baseline_late and diff_max > tol_contracts:
            divergence_detected = True

    # State tracking loop
    series_points: List[StrikePoint] = []
    crossover_points: List[Tuple[str, str, float]] = []

    current_dominance = "WARMUP"
    dom_start_idx = 0
    prior_dominant_side = "BALANCED"

    unwind_steps = max(1, unwind_min // resample_min)
    crossover_steps = max(1, confirm_min // resample_min)

    # Track signs of (dPE - dCE)
    sign_history: List[int] = []  # +1 for PE > CE, -1 for CE > PE, 0 for equal

    for i, row in df_grid.iterrows():
        ts: datetime = row["timestamp"]
        spot_val = float(row["spot"])
        ce_oi = float(row["ce_oi"])
        pe_oi = float(row["pe_oi"])
        pri_dce = float(row["ce_chg_oi"])
        pri_dpe = float(row["pe_chg_oi"])
        sec_dce = ce_oi - base_ce_oi
        sec_dpe = pe_oi - base_pe_oi

        ce_ltp = float(row["ce_ltp"])
        pe_ltp = float(row["pe_ltp"])
        ce_chg = float(row.get("ce_chg", 0) or 0)
        pe_chg = float(row.get("pe_chg", 0) or 0)

        # Intraday premium change vs session open baseline
        d_ce_ltp = (ce_ltp - base_ce_ltp) if ce_ltp > 0 and base_ce_ltp > 0 else 0.0
        d_pe_ltp = (pe_ltp - base_pe_ltp) if pe_ltp > 0 and base_pe_ltp > 0 else 0.0

        spread = pri_dpe - pri_dce
        pcr = (pe_oi / ce_oi) if ce_oi > 0 else 0.0

        current_sign = 1 if spread > 0 else (-1 if spread < 0 else 0)
        sign_history.append(current_sign)

        # 1. Cumulative Quadrant: ΔOI(t)-ΔOI(t0) vs ΔLTP(t)-ΔLTP(t0)
        ce_quad_cum = analytics.quadrant(d_ce_ltp, pri_dce)
        pe_quad_cum = analytics.quadrant(d_pe_ltp, pri_dpe)

        # 2. Local Quadrant: over last quadrant_local_minutes (default 15m)
        local_steps = max(1, local_min // resample_min)
        if i >= local_steps:
            prev_row = df_grid.iloc[i - local_steps]
            d_ce_oi_local = pri_dce - float(prev_row["ce_chg_oi"])
            d_pe_oi_local = pri_dpe - float(prev_row["pe_chg_oi"])
            d_ce_ltp_local = (ce_ltp - float(prev_row["ce_ltp"])) if ce_ltp > 0 and float(prev_row["ce_ltp"]) > 0 else 0.0
            d_pe_ltp_local = (pe_ltp - float(prev_row["pe_ltp"])) if pe_ltp > 0 and float(prev_row["pe_ltp"]) > 0 else 0.0
        else:
            d_ce_oi_local = pri_dce
            d_pe_oi_local = pri_dpe
            d_ce_ltp_local = d_ce_ltp
            d_pe_ltp_local = d_pe_ltp

        ce_quad_local = analytics.quadrant(d_ce_ltp_local, d_ce_oi_local)
        pe_quad_local = analytics.quadrant(d_pe_ltp_local, d_pe_oi_local)

        local_spread = d_pe_oi_local - d_ce_oi_local

        # Naive OI-only reading
        naive_read = "PE_BUILDUP" if spread > 0 else ("CE_BUILDUP" if spread < 0 else "BALANCED")
        if spread >= dom_min:
            naive_read = "PUT WRITERS DOMINANT (SUPPORT BUILDING)"
        elif spread <= -dom_min:
            naive_read = "CALL WRITERS DOMINANT (RESISTANCE CAPPING)"

        # Cumulative Agreement:
        is_disagree_cum = False
        if ce_quad_cum == "long_buildup" and spread < -dom_min:
            combined_read = "CALL BUYERS ACTIVE (BULLISH ACCUMULATION)"
            is_disagree_cum = True
        elif pe_quad_cum == "long_buildup" and spread >= dom_min:
            combined_read = "PUT BUYERS ACTIVE (BEARISH DOWNWARD PUSH)"
            is_disagree_cum = True
        else:
            combined_read = naive_read
        agreement_cum = "DISAGREE" if is_disagree_cum else "AGREE"

        # Local Agreement:
        local_dom_thresh = dom_min * (local_min / MKT_SESSION_MINUTES)
        is_disagree_local = False
        if ce_quad_local == "long_buildup" and local_spread < -local_dom_thresh:
            is_disagree_local = True
        elif pe_quad_local == "long_buildup" and local_spread >= local_dom_thresh:
            is_disagree_local = True
        agreement_local = "DISAGREE" if is_disagree_local else "AGREE"

        # Cum vs Local Agreement (does current local flow agree with day's net positioning?):
        if spread >= dom_min:
            cum_vs_local_agree = "AGREE" if pe_quad_cum == pe_quad_local else "DISAGREE"
        elif spread <= -dom_min:
            cum_vs_local_agree = "AGREE" if ce_quad_cum == ce_quad_local else "DISAGREE"
        else:
            cum_vs_local_agree = "AGREE" if (pe_quad_cum == pe_quad_local and ce_quad_cum == ce_quad_local) else "DISAGREE"

        # 1. Flow evaluation (BUILDING | UNWINDING | FLAT) over unwind_steps lookback
        if i >= unwind_steps:
            ce_leg_diff = pri_dce - df_grid.iloc[i - unwind_steps]["ce_chg_oi"]
            pe_leg_diff = pri_dpe - df_grid.iloc[i - unwind_steps]["pe_chg_oi"]
            if ce_leg_diff > 0 and pe_leg_diff > 0:
                flow = "BUILDING"
            elif ce_leg_diff < 0 and pe_leg_diff < 0:
                flow = "UNWINDING"
            else:
                flow = "FLAT"
        else:
            flow = "FLAT"

        # 2. Dominance evaluation (PE_DOMINANT | CE_DOMINANT | BALANCED | WARMUP | EOD_UNWIND | EXPIRY_DAY)
        dominance = "BALANCED"

        # Hard overrides:
        if ts.time() < warmup_time:
            dominance = "WARMUP"
        elif ts.time() >= eod_time:
            dominance = "EOD_UNWIND"
        elif is_expiry_day and not expiry_unknown:
            dominance = "EXPIRY_DAY"
        else:
            if spread >= dom_min:
                target_dom = "PE_DOMINANT"
            elif spread <= -dom_min:
                target_dom = "CE_DOMINANT"
            else:
                target_dom = "BALANCED"

            # Check Crossover: flip between PE_DOMINANT and CE_DOMINANT holding for crossover_steps
            if prior_dominant_side in ("PE_DOMINANT", "CE_DOMINANT") and target_dom in ("PE_DOMINANT", "CE_DOMINANT") and target_dom != prior_dominant_side:
                if i >= crossover_steps:
                    recent_signs = sign_history[-crossover_steps:]
                    if len(set(recent_signs)) == 1 and recent_signs[0] == current_sign:
                        dominance = "CROSSOVER"
                        flip_dir = f"{prior_dominant_side} → {target_dom}"
                        crossover_points.append((ts.strftime("%H:%M"), flip_dir, spread))
                        prior_dominant_side = target_dom
                    else:
                        dominance = prior_dominant_side
                else:
                    dominance = target_dom
            else:
                dominance = target_dom
                if target_dom in ("PE_DOMINANT", "CE_DOMINANT"):
                    prior_dominant_side = target_dom

        if dominance != current_dominance:
            current_dominance = dominance
            dom_start_idx = i

        duration_mins = (i - dom_start_idx + 1) * resample_min

        series_points.append(StrikePoint(
            timestamp=ts,
            clock_time=ts.strftime("%H:%M"),
            spot=round(spot_val, 1),
            ce_oi=round(ce_oi, 0),
            pe_oi=round(pe_oi, 0),
            ce_chg_oi=round(pri_dce, 0),
            pe_chg_oi=round(pri_dpe, 0),
            secondary_dce=round(sec_dce, 0),
            secondary_dpe=round(sec_dpe, 0),
            ce_ltp=round(ce_ltp, 2),
            pe_ltp=round(pe_ltp, 2),
            ce_ltp_chg=round(ce_chg, 2),
            pe_ltp_chg=round(pe_chg, 2),
            intraday_ce_ltp_chg=round(d_ce_ltp, 2),
            intraday_pe_ltp_chg=round(d_pe_ltp, 2),
            spread=round(spread, 0),
            pcr=round(pcr, 2),
            dominance=dominance,
            flow=flow,
            state=dominance,
            state_duration_minutes=duration_mins,
            ce_quadrant_cum=ce_quad_cum,
            pe_quadrant_cum=pe_quad_cum,
            ce_quadrant_local=ce_quad_local,
            pe_quadrant_local=pe_quad_local,
            agreement_cum=agreement_cum,
            agreement_local=agreement_local,
            cum_vs_local_agreement=cum_vs_local_agree,
            naive_interpretation=naive_read,
            combined_interpretation=combined_read,
            combined_ce_quadrant=ce_quad_cum,
            combined_pe_quadrant=pe_quad_cum,
            agreement=agreement_cum,
        ))

    # Build Latest Summary Stats
    latest_stats = None
    lot_size = r.get("lot_size")
    units_label = "contracts" if lot_size is None else "lakhs"

    if series_points:
        latest = series_points[-1]
        raw_dce = latest.ce_chg_oi
        raw_dpe = latest.pe_chg_oi
        raw_spread = latest.spread

        # Unit scaling
        if lot_size is not None and lot_size > 0:
            scale_dce = (raw_dce * lot_size) / 100000.0
            scale_dpe = (raw_dpe * lot_size) / 100000.0
            scale_spread = (raw_spread * lot_size) / 100000.0
        else:
            scale_dce = raw_dce
            scale_dpe = raw_dpe
            scale_spread = raw_spread

        # Veto discipline text based on orthogonal dominance and flow:
        if latest.dominance == "PE_DOMINANT":
            if latest.flow == "UNWINDING":
                veto = "Short-side entries discouraged while PE dominant (put writing support), but position is unwinding / reducing"
            else:
                veto = "Short-side entries discouraged while PE side dominant (put writing support building)"
        elif latest.dominance == "CE_DOMINANT":
            if latest.flow == "UNWINDING":
                veto = "Long-side entries discouraged while CE side dominant (call writing resistance), but position is unwinding / reducing"
            else:
                veto = "Long-side entries discouraged while CE side dominant (call writing resistance capping)"
        elif latest.dominance == "BALANCED":
            veto = "No positioning edge (balanced book — directional trades discouraged)"
        elif latest.dominance == "CROSSOVER":
            veto = f"Crossover in progress: momentum shifting to {latest.naive_interpretation}"
        elif latest.dominance == "WARMUP":
            veto = "Early-session warmup (OI noisy before 09:50 — trades vetoed)"
        elif latest.dominance == "EOD_UNWIND":
            veto = "EOD position squaring in progress (post-15:00 mechanical drop)"
        elif latest.dominance == "EXPIRY_DAY":
            veto = "Expiry day session (pinning and rapid gamma distortion — directional trades vetoed)"
        else:
            veto = "Positioning neutral / active monitoring"

        div_warn = None
        if divergence_detected:
            div_warn = f"Primary/Secondary divergence observed (max {max_divergence:,.0f} contracts). Primary ce_chg_oi trusted."

        # Real system timezone check (ensures host OS resolves to IST, UTC+05:30)
        stale_warn = None
        now_dt = datetime.now()
        is_ist_name = any("IST" in tz.upper() for tz in time.tzname)
        is_ist_offset = (time.timezone == -19800) or (time.altzone == -19800)
        utc_offset_sec = (now_dt - datetime.utcnow()).total_seconds()
        is_530 = abs(utc_offset_sec - 19800) < 120

        if not (is_ist_name or is_ist_offset or is_530):
            raise RuntimeError(
                f"Host timezone is not Asia/Kolkata (IST). time.tzname={time.tzname}, "
                f"offset={utc_offset_sec/3600:.2f}h. Timestamps would misalign by clock offset."
            )

        if sess_date == date.today() and MKT_OPEN_TIME <= now_dt.time() <= MKT_CLOSE_TIME:
            age_min = (now_dt - latest.timestamp).total_seconds() / 60.0
            if age_min > stale_min:
                stale_warn = f"Live feed stale: newest snapshot captured {age_min:.0f}m ago (> {stale_min}m threshold)."

        latest_stats = StrikeSummaryStats(
            strike=strike,
            current_spot=latest.spot,
            ce_oi=latest.ce_oi,
            pe_oi=latest.pe_oi,
            dce=round(scale_dce, 2),
            dpe=round(scale_dpe, 2),
            spread=round(scale_spread, 2),
            pcr=latest.pcr,
            dominance=latest.dominance,
            flow=latest.flow,
            state=latest.dominance,
            state_duration_minutes=latest.state_duration_minutes,
            interpretation_mode=mode,
            ce_quadrant_cum=latest.ce_quadrant_cum,
            pe_quadrant_cum=latest.pe_quadrant_cum,
            ce_quadrant_local=latest.ce_quadrant_local,
            pe_quadrant_local=latest.pe_quadrant_local,
            agreement_cum=latest.agreement_cum,
            agreement_local=latest.agreement_local,
            cum_vs_local_agreement=latest.cum_vs_local_agreement,
            agreement=latest.agreement_cum,
            veto_note=veto,
            divergence_warning=div_warn,
            staleness_warning=stale_warn,
            combined_interpretation=latest.combined_interpretation,
            units_label=units_label,
        )

    return StrikeTimelineResult(
        strike=strike,
        session_date=sess_date.strftime("%Y-%m-%d"),
        expiry=expiry,
        expiry_unknown=expiry_unknown,
        is_expiry_day=is_expiry_day,
        baseline_late=baseline_late,
        divergence_detected=divergence_detected,
        max_divergence_contracts=round(max_divergence, 0),
        series=series_points,
        latest_stats=latest_stats,
        roll_hazard=has_roll,
        crossover_points=crossover_points,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Session Multi-Strike Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def evaluate_session_timelines(
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    sess_date: date,
    strikes: Optional[List[float]] = None,
    expiry: Optional[str] = None,
    rules: Optional[dict] = None,
    strike_step: Optional[float] = None,
    current_spot: Optional[float] = None,
    atm_anchor: Optional[str] = None,
) -> SessionTimelineResult:
    """
    Coordinates session evaluation:
      - Validates coverage gate & market-hours filter
      - Selects watched strikes (anchored to current spot or open spot, 100-pt or 50-pt step)
      - Runs per-strike timelines with fixed ATM reference
      - Reconciles footer numbers against independently computed last - first raw snapshots.
    """
    r = rules or load_rules()
    date_str = sess_date.strftime("%Y-%m-%d")
    lot_size = r.get("lot_size")
    units_label = "contracts" if lot_size is None else "lakhs"
    resample_min = int(r.get("resample_minutes", 3))
    mode = r.get("interpretation_mode", "combined")

    # Coverage audit
    cov = compute_session_coverage(all_snapshots, sess_date, rules=r)

    # Filter market hours
    mkt_snaps = filter_market_hours(all_snapshots)
    if not mkt_snaps:
        return SessionTimelineResult(
            session_date=date_str,
            expiry=expiry or "",
            expiry_unknown=True,
            is_expiry_day=False,
            coverage=cov,
            atm_strike=0.0,
            watched_strikes=[],
            strike_timelines={},
            resample_minutes=resample_min,
            interpretation_mode=mode,
            units_label=units_label,
            lot_size=lot_size,
            guard_warning="No market-hours snapshots found for this session.",
        )

    # Expiry scope: "nearest" (default) or "all" (multi-expiry aggregation)
    expiry_scope = str(r.get("expiry_scope", "nearest")).lower()
    processed_snaps: List[Tuple[datetime, float, pd.DataFrame]] = []
    for ts, spot, df in mkt_snaps:
        if "expiry" in df.columns and df["expiry"].nunique() > 1:
            if expiry_scope == "all":
                agg_dict = {
                    col: "sum" if ("oi" in col or "vol" in col) else "mean"
                    for col in df.columns if col not in ("strike", "expiry")
                }
                df_scoped = df.groupby("strike", as_index=False).agg(agg_dict)
                df_scoped.attrs = dict(df.attrs)
                processed_snaps.append((ts, spot, df_scoped))
            else:  # nearest
                first_exp = df["expiry"].dropna().iloc[0]
                df_scoped = df[df["expiry"] == first_exp].copy()
                df_scoped.attrs = dict(df.attrs)
                processed_snaps.append((ts, spot, df_scoped))
        else:
            processed_snaps.append((ts, spot, df))
    mkt_snaps = processed_snaps

    # Expiry handling
    exp_val = expiry or mkt_snaps[0][2].attrs.get("expiry")
    expiry_unknown = (exp_val is None or str(exp_val).strip() == "")
    expiry_str = str(exp_val) if not expiry_unknown else "UNKNOWN"
    is_exp_day = False
    if not expiry_unknown:
        try:
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            is_exp_day = (exp_date == sess_date)
        except Exception:
            is_exp_day = False

    # Determine strike spacing and ATM from market-hours snapshots
    first_ts, first_spot, first_df = mkt_snaps[0]
    last_ts, last_spot, last_df = mkt_snaps[-1]
    sorted_strikes = sorted(first_df["strike"].unique())

    # Derive strike step from chain or parameters
    diffs = np.diff(sorted_strikes)
    pos_diffs = diffs[diffs > 0]
    chain_step = float(np.median(pos_diffs)) if len(pos_diffs) > 0 else 50.0

    step = float(strike_step) if strike_step is not None else float(chain_step)

    # Reference spot for ATM: explicit current_spot > atm_anchor="current" (last_spot) > first_spot
    atm_anchor_mode = str(atm_anchor or r.get("atm_anchor", "open")).lower()
    if current_spot is not None:
        ref_spot = float(current_spot)
    elif atm_anchor_mode == "current":
        ref_spot = float(last_spot)
    else:
        ref_spot = float(first_spot)

    atm = round(ref_spot / step) * step

    # Select watched strikes
    if strikes is not None and len(strikes) > 0:
        max_allowed = int(r.get("max_strikes", 15))
        watched = [float(k) for k in strikes[:max_allowed] if float(k) in sorted_strikes]
        if not watched:
            watched = [k for k in sorted_strikes if k in [float(s) for s in strikes]]
    else:
        # Default: ATM ± strikes based on step
        max_s = int(r.get("max_strikes", 5))
        half = max_s // 2
        watched = [atm + (i - half) * step for i in range(max_s)]
        # Restrict to strikes existing in chain
        watched = [k for k in watched if k in sorted_strikes]

    timelines: Dict[float, StrikeTimelineResult] = {}
    for k in watched:
        tl = evaluate_strike_timeline(
            mkt_snaps=mkt_snaps,
            strike=k,
            sess_date=sess_date,
            expiry=expiry_str,
            expiry_unknown=expiry_unknown,
            is_expiry_day=is_exp_day,
            rules=r,
        )
        timelines[k] = tl

    # Footer reconciliation assertion (Series-Inversion Guard)
    last_df = mkt_snaps[-1][2]
    for k in watched:
        r_last = last_df[last_df["strike"] == k]
        if not r_last.empty and k in timelines and timelines[k].series:
            raw_last_ce_chg = float(r_last.iloc[0].get("ce_chg_oi", 0) or 0)
            raw_last_pe_chg = float(r_last.iloc[0].get("pe_chg_oi", 0) or 0)
            final_pt = timelines[k].series[-1]
            # Primary dCE / dPE must match the raw snapshot values
            if abs(final_pt.ce_chg_oi - raw_last_ce_chg) >= 1.0:
                logger.warning(
                    "Series Inversion Warning at strike %s: dCE %s != raw %s",
                    k, final_pt.ce_chg_oi, raw_last_ce_chg
                )
            if abs(final_pt.pe_chg_oi - raw_last_pe_chg) >= 1.0:
                logger.warning(
                    "Series Inversion Warning at strike %s: dPE %s != raw %s",
                    k, final_pt.pe_chg_oi, raw_last_pe_chg
                )

    # Propagate missed opening OI / divergence metrics across all watched strikes to session coverage
    max_div_session = 0.0
    max_div_strike = None
    div_detected_session = False
    for k, tl in timelines.items():
        if tl.max_divergence_contracts > max_div_session:
            max_div_session = tl.max_divergence_contracts
            max_div_strike = k
        if tl.divergence_detected:
            div_detected_session = True

    cov.max_divergence_contracts = max_div_session
    cov.missed_opening_oi_contracts = max_div_session
    cov.missed_opening_strike = max_div_strike
    cov.divergence_flag = div_detected_session
    if div_detected_session and max_div_strike is not None:
        cov.status_note += f" (MISSED_OPENING_OI: {max_div_session:,.0f} @ {max_div_strike:.0f})"

    return SessionTimelineResult(
        session_date=date_str,
        expiry=expiry_str,
        expiry_unknown=expiry_unknown,
        is_expiry_day=is_exp_day,
        coverage=cov,
        atm_strike=atm,
        watched_strikes=watched,
        strike_timelines=timelines,
        resample_minutes=resample_min,
        interpretation_mode=mode,
        units_label=units_label,
        lot_size=lot_size,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Global Coverage Audit Helper
# ═══════════════════════════════════════════════════════════════════════════

def get_coverage_summary(rules: Optional[dict] = None) -> List[SessionCoverage]:
    """Audits all retained snapshot session folders against the coverage gate."""
    import store
    snapshot_dir = config.SNAPSHOT_DIR
    if not snapshot_dir.exists():
        return []

    r = rules or load_rules()
    date_folders = sorted([d for d in snapshot_dir.iterdir() if d.is_dir()])

    summaries: List[SessionCoverage] = []
    for d in date_folders:
        try:
            sess_date = datetime.strptime(d.name, "%Y-%m-%d").date()
            snaps = store.load_day(sess_date)
            res = evaluate_session_timelines(snaps, sess_date, rules=r)
            summaries.append(res.coverage)
        except Exception as err:
            print(f"[oi_timeline] Warning loading session {d.name}: {err}")
            continue
    return summaries


def export_timeline_to_csv(
    session_res: SessionTimelineResult,
    output_dir: str | Path = "data/oi_timeline",
) -> Path:
    """
    Exports all strike series points from a SessionTimelineResult to data/oi_timeline/{YYYY-MM-DD}.csv.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    target_file = out_path / f"{session_res.session_date}.csv"

    rows = []
    for strike, tl in session_res.strike_timelines.items():
        for pt in tl.series:
            rows.append({
                "session_date": session_res.session_date,
                "timestamp": pt.timestamp.isoformat(),
                "clock_time": pt.clock_time,
                "strike": strike,
                "spot": pt.spot,
                "ce_oi": pt.ce_oi,
                "pe_oi": pt.pe_oi,
                "ce_chg_oi": pt.ce_chg_oi,
                "pe_chg_oi": pt.pe_chg_oi,
                "secondary_dce": pt.secondary_dce,
                "secondary_dpe": pt.secondary_dpe,
                "ce_ltp": pt.ce_ltp,
                "pe_ltp": pt.pe_ltp,
                "ce_ltp_chg": pt.ce_ltp_chg,
                "pe_ltp_chg": pt.pe_ltp_chg,
                "intraday_ce_ltp_chg": pt.intraday_ce_ltp_chg,
                "intraday_pe_ltp_chg": pt.intraday_pe_ltp_chg,
                "spread": pt.spread,
                "pcr": pt.pcr,
                "dominance": pt.dominance,
                "flow": pt.flow,
                "state_duration_minutes": pt.state_duration_minutes,
                "ce_quadrant_cum": pt.ce_quadrant_cum,
                "pe_quadrant_cum": pt.pe_quadrant_cum,
                "ce_quadrant_local": pt.ce_quadrant_local,
                "pe_quadrant_local": pt.pe_quadrant_local,
                "agreement_cum": pt.agreement_cum,
                "agreement_local": pt.agreement_local,
                "cum_vs_local_agreement": pt.cum_vs_local_agreement,
                "naive_interpretation": pt.naive_interpretation,
                "combined_interpretation": pt.combined_interpretation,
            })

    df = pd.DataFrame(rows)
    df.to_csv(target_file, index=False)
    return target_file


# ═══════════════════════════════════════════════════════════════════════════
# 7. WRITER GAP + WRITER LINES Engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WriterGapStrikeData:
    """Per-strike writer metrics for a poll."""
    strike: float
    ce_d: float          # in Lakhs
    pe_d: float          # in Lakhs
    gap: float           # pe_d - ce_d in Lakhs
    side: str            # CE_ABOVE | PE_ABOVE | INTERTWINED
    ce_x_5m: float = 0.0 # 5m change in Lakhs
    pe_x_5m: float = 0.0
    ce_z_5m: float = 0.0 # 5m change z-score
    pe_z_5m: float = 0.0
    ce_bend: str = "→"   # ⇈⇈, ⇊⇊, ↑, ↓, →
    pe_bend: str = "→"
    ce_who: str = "?"    # WRT, BUY, COV, EXIT, ?
    pe_who: str = "?"
    ce_who_full: str = "UNCLEAR"
    pe_who_full: str = "UNCLEAR"
    ce_iv_chg: float = 0.0
    pe_iv_chg: float = 0.0
    gap_5m: float = 0.0
    state: str = ""      # CROSSED, INTERTWINED, CONVERGING, or side


@dataclass
class WallMigrationInfo:
    """Whole-chain wall migration status vs 30 minutes ago."""
    max_ce_oi_strike: float
    max_pe_oi_strike: float
    max_ce_add_strike: float
    max_pe_add_strike: float
    prev_max_ce_add: Optional[float] = None
    prev_max_pe_add: Optional[float] = None
    ce_migration: str = "SAME"  # UP, DOWN, SAME
    pe_migration: str = "SAME"  # UP, DOWN, SAME
    status: str = "walls steady" # "walls migrating up", "walls migrating down", "walls steady"
    summary_str: str = "walls steady"


@dataclass
class WriterCrossoverEvent:
    """Confirmed writer net gap crossover event."""
    timestamp: datetime
    clock_time: str
    direction: str          # "BULLISH", "BEARISH"
    net_gap: float          # in Lakhs
    spot: float
    how: str                # e.g. "CE COVERING (short covering)", "PE WRITING"
    leg_moved_more: str     # "CE" or "PE"
    classification: str     # "COVERING", "WRITING", etc.
    leading_strike: float
    leading_strike_change: float # in Lakhs
    wall_migration_status: str
    telegram_text: str = ""
    is_expiry: bool = False


@dataclass
class WriterLinesRead:
    """5-minute close bar read."""
    timestamp: datetime
    clock_time: str
    read_text: str          # e.g. "🟢 BULLISH IMPULSE (short covering)"
    raw_read: str           # e.g. "BULLISH IMPULSE"
    breadth_str: str        # e.g. "3/3"
    breadth_count: int      # e.g. 3
    bending_leg: str        # "CE", "PE", "BOTH", "NONE"
    dominant_who_ce: str    # e.g. "COVERING 3/3"
    dominant_who_pe: str    # e.g. "WRITING 2/3"
    gap_5m: float           # in Lakhs
    net_gap: float          # in Lakhs
    spot: float
    price_agree: bool
    price_agree_str: str    # "price confirms", "price diverges"
    wall_migration_str: str
    is_expiry: bool
    is_impulse: bool = False
    is_downgraded: bool = False
    unclear_warning: bool = False
    leading_strike: float = 0.0
    leading_change: float = 0.0
    leading_iv_chg: float = 0.0
    telegram_text: str = ""


@dataclass
class WriterBucketData:
    """Combined ΔOI and delta-weighted greeks metrics for a strike bucket."""
    bucket_type: str         # "OTM" | "ITM"
    ce_strikes: List[float]
    pe_strikes: List[float]
    ce_sum: float            # in Lakhs
    pe_sum: float            # in Lakhs
    ce_dw: float             # delta-weighted in Lakhs
    pe_dw: float             # delta-weighted in Lakhs
    gap: float               # pe_dw - ce_dw in Lakhs
    score: float             # (pe_dw - ce_dw) / (|pe_dw| + |ce_dw|)
    bias: str                # BULLISH (> +0.2) | BEARISH (< -0.2) | BALANCED
    ratio_str: str           # pe_sum / ce_sum or "n/a (unwinding)"
    raw_ratio: Optional[float] = None
    score_5m: Optional[float] = None
    gap_5m: Optional[float] = None
    score_bend: str = "→"    # ⇈⇈, ⇊⇊, ↑, ↓, →
    gap_bend: str = "→"
    recentred: bool = False


@dataclass
class WriterGapPollResult:
    """Complete 3-minute poll evaluation."""
    timestamp: datetime
    clock_time: str
    spot: float
    basket_strikes: List[float]
    basket_changed: bool
    strikes: Dict[float, WriterGapStrikeData]
    net_ce_d: float
    net_pe_d: float
    net_gap: float
    net_side: str
    gap_trend_15m: str      # e.g. "widening (+35.4L vs 15m ago)"
    wall_migration: WallMigrationInfo
    last_crossover: Optional[WriterCrossoverEvent]
    new_crossover: Optional[WriterCrossoverEvent]
    is_expiry: bool
    briefing_block_text: str # Output A text
    bucket_otm: Optional[WriterBucketData] = None
    bucket_itm: Optional[WriterBucketData] = None


@dataclass
class WriterLinesTableResult:
    """Complete 5-minute close evaluation."""
    timestamp: datetime
    clock_time: str
    spot: float
    spot_chg_5m: float
    basket_strikes: List[float]
    strikes: Dict[float, WriterGapStrikeData]
    net_ce_x_5m: float
    net_pe_x_5m: float
    net_gap_5m: float
    net_state: str
    breadth_ce: str
    breadth_pe: str
    read: WriterLinesRead
    table_text: str          # Output B text
    is_expiry: bool
    bucket_otm: Optional[WriterBucketData] = None
    bucket_itm: Optional[WriterBucketData] = None


@dataclass
class WriterGapSessionResult:
    """Full session result container for Writer Gap and Writer Lines."""
    session_date: str
    is_expiry_day: bool
    is_expiry_eve: bool
    min_gap_lakhs: float
    basket_changes: List[Tuple[datetime, List[float]]]
    poll_results: List[WriterGapPollResult]
    table_results: List[WriterLinesTableResult]
    crossovers: List[WriterCrossoverEvent]
    reads: List[WriterLinesRead]
    unclear_evals: int
    total_evals: int


def load_writer_gap_rules(rules_path: Optional[Path] = None) -> dict:
    """Loads writer_gap thresholds from strategy1_rules.json with fallbacks."""
    path = rules_path or DEFAULT_RULES_PATH
    defaults = {
        "delta_min": 0.25,
        "delta_max": 0.75,
        "atm_range": 200.0,
        "top_n_strikes": 3,
        "basket_hold_minutes": 30,
        "basket_spot_exit_strikes": 1,
        "lot_size": 65,
        "intertwined_pct": 0.15,
        "sign_flips_threshold": 2,
        "sign_flips_window_min": 30,
        "iv_noise": 0.3,
        "resid_noise": 0.1,
        "min_gap_lakhs": 20.0,
        "crossover_confirm_polls": 2,
        "bend_z_threshold": 2.5,
        "bend_min_lakhs": 10.0,
        "bend_mild_z_threshold": 1.0,
        "breadth_min": 2,
        "wall_migration_lookback_min": 30,
        "expiry_suppress_telegram_from": "14:00",
        "telegram_combined_window_min": 5,
        "log_csv_path": "data/writer_gap.csv",
        "buckets": {
            "enabled": True,
            "width_strikes": 7,
            "max_width_strikes": 12,
            "step": 50.0,
            "score_bullish_threshold": 0.2,
            "score_bearish_threshold": -0.2,
            "bend_z_threshold": 2.5,
            "bend_min_lakhs": 10.0,
            "bend_mild_z_threshold": 1.0,
            "score_bend_z_threshold": 2.5,
            "score_bend_mild_z_threshold": 1.0,
            "score_bend_min_delta": 0.04,
        },
    }
    if not path.exists():
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            wg = data.get("writer_gap", {})
            merged = {**defaults, **wg}
            if "buckets" in wg and isinstance(wg["buckets"], dict):
                merged["buckets"] = {**defaults["buckets"], **wg["buckets"]}
            return merged
    except Exception as err:
        logger.warning("Failed to load writer_gap rules: %s. Using defaults.", err)
        return defaults


def is_writer_gap_expiry(sess_date: date, df: Optional[pd.DataFrame] = None) -> Tuple[bool, bool]:
    """Checks whether the session is an expiry day or expiry eve."""
    is_exp_day = False
    is_exp_eve = False

    exp_date = None
    if df is not None and "expiry" in df.columns:
        first_exp = df["expiry"].dropna().iloc[0] if not df["expiry"].dropna().empty else None
        if first_exp:
            try:
                exp_date = datetime.strptime(str(first_exp), "%d-%b-%Y").date()
            except Exception:
                try:
                    exp_date = datetime.strptime(str(first_exp), "%Y-%m-%d").date()
                except Exception:
                    pass
    if exp_date is not None:
        if sess_date == exp_date:
            is_exp_day = True
        elif sess_date == (exp_date - timedelta(days=1)):
            is_exp_eve = True

    try:
        import rvol_gate
        if rvol_gate.is_expiry_session(sess_date):
            is_exp_day = True
        next_day = sess_date + timedelta(days=1)
        if rvol_gate.is_expiry_session(next_day):
            is_exp_eve = True
    except Exception:
        pass

    return is_exp_day, is_exp_eve


def select_writer_basket(
    df: pd.DataFrame,
    spot: float,
    rules: Optional[dict] = None,
) -> List[float]:
    """
    Selects top 3 strikes by gamma * (ce_oi + pe_oi) from nearest expiry
    within ATM ± 200 meeting 0.25 <= |delta| <= 0.75 (CE and PE).
    """
    r = rules or load_writer_gap_rules()
    d_min = float(r.get("delta_min", 0.25))
    d_max = float(r.get("delta_max", 0.75))
    atm_rng = float(r.get("atm_range", 200.0))
    n_strikes = int(r.get("top_n_strikes", 3))

    if df.empty:
        return []

    if "expiry" in df.columns and df["expiry"].dropna().nunique() > 0:
        near_exp = df["expiry"].dropna().iloc[0]
        chain = df[df["expiry"] == near_exp].copy()
    else:
        chain = df.copy()

    step = 50.0
    atm = round(spot / step) * step

    cands = chain[chain["strike"].between(atm - atm_rng, atm + atm_rng)].copy()
    if cands.empty:
        cands = chain.copy()

    if "total_gamma" not in cands.columns:
        if "ce_gamma" in cands.columns:
            cands["total_gamma"] = cands["ce_gamma"] * (cands["ce_oi"].fillna(0) + cands["pe_oi"].fillna(0))
        else:
            cands["total_gamma"] = cands["ce_oi"].fillna(0) + cands["pe_oi"].fillna(0)

    has_greeks = ("ce_delta" in cands.columns and "pe_delta" in cands.columns)
    if has_greeks:
        strict = cands[
            (cands["ce_delta"].abs() >= d_min) & (cands["ce_delta"].abs() <= d_max) &
            (cands["pe_delta"].abs() >= d_min) & (cands["pe_delta"].abs() <= d_max)
        ].copy()
    else:
        strict = pd.DataFrame()

    if len(strict) >= n_strikes:
        chosen = strict.sort_values("total_gamma", ascending=False).head(n_strikes)
        return sorted([float(k) for k in chosen["strike"].tolist()])

    chosen_strikes = []
    if not strict.empty:
        chosen_strikes = [float(k) for k in strict.sort_values("total_gamma", ascending=False)["strike"].tolist()]

    rem_cands = cands[~cands["strike"].isin(chosen_strikes)].sort_values("total_gamma", ascending=False)
    for k in rem_cands["strike"].tolist():
        chosen_strikes.append(float(k))
        if len(chosen_strikes) == n_strikes:
            break

    return sorted(chosen_strikes[:n_strikes])


def classify_who_is_acting(
    d_oi: float,
    d_iv: Optional[float],
    resid: Optional[float],
    iv_noise: float = 0.3,
    resid_noise: float = 0.1,
) -> Tuple[str, str]:
    """
    Classifies writer vs buyer per leg per bar:
      OI ↑ + IV/resid ↓ → WRITING (WRT)
      OI ↑ + IV/resid ↑ → BUYING (BUY)
      OI ↓ + IV/resid ↑ → COVERING (COV)
      OI ↓ + IV/resid ↓ → LONG EXIT (EXIT)
      |IV change| < iv_noise (or |resid| < resid_noise) → UNCLEAR (?)
    """
    if abs(d_oi) < 1e-4:
        return ("UNCLEAR", "?")

    up = None
    if d_iv is not None and not np.isnan(d_iv):
        if abs(d_iv) < iv_noise:
            return ("UNCLEAR", "?")
        up = (d_iv > 0)
    elif resid is not None and not np.isnan(resid):
        if abs(resid) < resid_noise:
            return ("UNCLEAR", "?")
        up = (resid > 0)
    else:
        return ("UNCLEAR", "?")

    if d_oi > 0:
        return ("BUYING", "BUY") if up else ("WRITING", "WRT")
    else:
        return ("COVERING", "COV") if up else ("LONG EXIT", "EXIT")


def compute_wall_migration(
    chain_df: pd.DataFrame,
    prev_30m_df: Optional[pd.DataFrame] = None,
) -> WallMigrationInfo:
    """
    Computes wall migration across the whole chain (nearest expiry):
      max_ce_oi_strike, max_pe_oi_strike (total OI walls)
      max_ce_add_strike, max_pe_add_strike (largest ΔOI since 09:15)
    Compare with 30 min ago -> UP / DOWN / SAME for each.
      both add-walls moving UP -> "walls migrating up" (bullish)
      both moving DOWN -> "walls migrating down" (bearish)
      otherwise -> "walls steady"
    """
    if chain_df.empty:
        return WallMigrationInfo(0, 0, 0, 0, status="walls steady", summary_str="walls steady")

    if "expiry" in chain_df.columns and chain_df["expiry"].dropna().nunique() > 0:
        near_exp = chain_df["expiry"].dropna().iloc[0]
        df_near = chain_df[chain_df["expiry"] == near_exp].copy()
    else:
        df_near = chain_df.copy()

    max_ce_oi_s = float(df_near.loc[df_near["ce_oi"].fillna(0).idxmax()]["strike"]) if not df_near.empty else 0.0
    max_pe_oi_s = float(df_near.loc[df_near["pe_oi"].fillna(0).idxmax()]["strike"]) if not df_near.empty else 0.0

    max_ce_add_s = float(df_near.loc[df_near["ce_chg_oi"].fillna(0).idxmax()]["strike"]) if not df_near.empty else 0.0
    max_pe_add_s = float(df_near.loc[df_near["pe_chg_oi"].fillna(0).idxmax()]["strike"]) if not df_near.empty else 0.0

    status = "walls steady"
    summary = status
    prev_ce_add = None
    prev_pe_add = None
    ce_mig = "SAME"
    pe_mig = "SAME"

    if prev_30m_df is not None and not prev_30m_df.empty:
        if "expiry" in prev_30m_df.columns and prev_30m_df["expiry"].dropna().nunique() > 0:
            df_p = prev_30m_df[prev_30m_df["expiry"] == prev_30m_df["expiry"].dropna().iloc[0]].copy()
        else:
            df_p = prev_30m_df.copy()
        if not df_p.empty:
            prev_ce_add = float(df_p.loc[df_p["ce_chg_oi"].fillna(0).idxmax()]["strike"])
            prev_pe_add = float(df_p.loc[df_p["pe_chg_oi"].fillna(0).idxmax()]["strike"])
            ce_mig = "UP" if max_ce_add_s > prev_ce_add else ("DOWN" if max_ce_add_s < prev_ce_add else "SAME")
            pe_mig = "UP" if max_pe_add_s > prev_pe_add else ("DOWN" if max_pe_add_s < prev_pe_add else "SAME")
            if ce_mig == "UP" and pe_mig == "UP":
                status = "walls migrating up"
            elif ce_mig == "DOWN" and pe_mig == "DOWN":
                status = "walls migrating down"
            else:
                status = "walls steady"
            summary = f"{status} (CE {prev_ce_add:.0f}→{max_ce_add_s:.0f}, PE {prev_pe_add:.0f}→{max_pe_add_s:.0f})"

    return WallMigrationInfo(
        max_ce_oi_strike=max_ce_oi_s,
        max_pe_oi_strike=max_pe_oi_s,
        max_ce_add_strike=max_ce_add_s,
        max_pe_add_strike=max_pe_add_s,
        prev_max_ce_add=prev_ce_add,
        prev_max_pe_add=prev_pe_add,
        ce_migration=ce_mig,
        pe_migration=pe_mig,
        status=status,
        summary_str=summary,
    )


def compute_price_agreement(gap_5m: float, spot_chg_5m: float) -> Tuple[bool, str]:
    """
    agree = sign(gap_5m) == sign(spot change over the bar).
    Print "price confirms" or "price diverges".
    """
    if abs(gap_5m) < 0.1 or abs(spot_chg_5m) < 0.1:
        agree = True
    else:
        agree = ((gap_5m > 0 and spot_chg_5m > 0) or (gap_5m < 0 and spot_chg_5m < 0))
    return agree, ("price confirms" if agree else "price diverges")


def format_crossover_telegram(event: WriterCrossoverEvent) -> str:
    """Formats Telegram alert for confirmed writer net gap crossover."""
    sign_gap = "+" if event.net_gap >= 0 else ""
    sign_chg = "+" if event.leading_strike_change >= 0 else ""
    return (
        f"🔀 WRITER CROSS {event.clock_time} → {event.direction} "
        f"({event.how} {sign_chg}{event.leading_strike_change:.0f}L at {event.leading_strike:,.0f})\n"
        f" Net gap {sign_gap}{event.net_gap:.0f}L | Spot {event.spot:,.0f} | {event.wall_migration_status}"
    )


def format_impulse_telegram(read: WriterLinesRead) -> str:
    """Formats Telegram alert for 5-min IMPULSE read."""
    pfx = "🟢" if "BULLISH" in read.read_text else "🔴"
    gap_val = read.gap_5m if abs(read.gap_5m) > 0 else read.net_gap
    sign_gap = "+" if gap_val >= 0 else ""
    sign_chg = "+" if read.leading_change >= 0 else ""
    sign_iv = "+" if read.leading_iv_chg >= 0 else ""
    if read.bending_leg == "CE":
        leg_summary = f"CE {read.dominant_who_ce.lower()}"
    elif read.bending_leg == "PE":
        leg_summary = f"PE {read.dominant_who_pe.lower()}"
    else:
        leg_summary = read.dominant_who_ce if "CE" in read.raw_read else read.dominant_who_pe
    return (
        f"{pfx} {read.raw_read} {read.clock_time} — {leg_summary} "
        f"({read.leading_strike:,.0f} {sign_chg}{read.leading_change:.0f}L, IV {sign_iv}{read.leading_iv_chg:.1f})\n"
        f" | gap {sign_gap}{gap_val:.0f}L | {read.price_agree_str}"
    )


def combine_telegram_messages(cross_msg: str, impulse_msg: str) -> str:
    """Combines crossover and impulse alerts if firing within 5 minutes in same direction."""
    return f"{impulse_msg}\n{cross_msg}"


def compute_writer_bucket(
    df: pd.DataFrame,
    spot: float,
    bucket_type: str = "OTM",
    rules: Optional[dict] = None,
    ref_bucket_5m: Optional[WriterBucketData] = None,
    score_5m_history: Optional[List[float]] = None,
    gap_5m_history: Optional[List[float]] = None,
    bar_had_recentre: bool = False,
) -> WriterBucketData:
    """
    Computes combined ΔOI and delta-weighted greeks metrics for a strike bucket.
    Primary (OTM):
      CE bucket = ATM + next width strikes ABOVE
      PE bucket = ATM + next width strikes BELOW
    Secondary (ITM):
      CE bucket = ATM + width strikes BELOW
      PE bucket = ATM + width strikes ABOVE
    """
    r = rules or load_writer_gap_rules()
    b_cfg = r.get("buckets", {})
    step = float(b_cfg.get("step", 50.0))
    width = int(b_cfg.get("width_strikes", 7))
    max_w = int(b_cfg.get("max_width_strikes", 12))
    width = min(max(1, width), max_w)
    lot_size = float(r.get("lot_size", 65))

    atm = round(spot / step) * step

    if bucket_type.upper() == "OTM":
        ce_strikes = [atm + i * step for i in range(0, width + 1)]
        pe_strikes = [atm - i * step for i in range(width, -1, -1)]
    else:  # ITM
        ce_strikes = [atm - i * step for i in range(width, -1, -1)]
        pe_strikes = [atm + i * step for i in range(0, width + 1)]

    # Filter to nearest expiry if multi-expiry
    if df is not None and not df.empty:
        if "expiry" in df.columns and df["expiry"].dropna().nunique() > 0:
            near_exp = df["expiry"].dropna().iloc[0]
            chain = df[df["expiry"] == near_exp]
        else:
            chain = df
        chain_k = chain.set_index("strike")
    else:
        chain_k = pd.DataFrame()

    ce_d_list = []
    ce_dw_list = []
    for k in ce_strikes:
        if not chain_k.empty and k in chain_k.index:
            row = chain_k.loc[k]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            c_d = float(row.get("ce_chg_oi", 0) or 0) * lot_size / 1e5
            raw_delta = row.get("ce_delta", None)
            if raw_delta is None or (isinstance(raw_delta, float) and np.isnan(raw_delta)):
                c_delta = 0.5
            else:
                c_delta = abs(float(raw_delta))
        else:
            c_d = 0.0
            c_delta = 0.5
        ce_d_list.append(c_d)
        ce_dw_list.append(c_d * c_delta)

    pe_d_list = []
    pe_dw_list = []
    for k in pe_strikes:
        if not chain_k.empty and k in chain_k.index:
            row = chain_k.loc[k]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            p_d = float(row.get("pe_chg_oi", 0) or 0) * lot_size / 1e5
            raw_delta = row.get("pe_delta", None)
            if raw_delta is None or (isinstance(raw_delta, float) and np.isnan(raw_delta)):
                p_delta = 0.5
            else:
                p_delta = abs(float(raw_delta))
        else:
            p_d = 0.0
            p_delta = 0.5
        pe_d_list.append(p_d)
        pe_dw_list.append(p_d * p_delta)

    ce_sum = sum(ce_d_list)
    pe_sum = sum(pe_d_list)
    ce_dw_sum = sum(ce_dw_list)
    pe_dw_sum = sum(pe_dw_list)

    gap = pe_dw_sum - ce_dw_sum
    denom = abs(pe_dw_sum) + abs(ce_dw_sum)
    score = (pe_dw_sum - ce_dw_sum) / denom if denom > 1e-6 else 0.0
    score = max(-1.0, min(1.0, score))

    score_bull = float(b_cfg.get("score_bullish_threshold", 0.2))
    score_bear = float(b_cfg.get("score_bearish_threshold", -0.2))
    if score > score_bull:
        bias = "BULLISH"
    elif score < score_bear:
        bias = "BEARISH"
    else:
        bias = "BALANCED"

    if pe_sum > 0 and ce_sum > 0:
        raw_ratio = pe_sum / ce_sum
        ratio_str = f"{raw_ratio:.2f}"
    else:
        raw_ratio = None
        ratio_str = "n/a (unwinding)"

    score_5m = None
    gap_5m = None
    score_bend = "→"
    gap_bend = "→"

    if ref_bucket_5m is not None and not bar_had_recentre:
        score_5m = score - ref_bucket_5m.score
        gap_5m = gap - ref_bucket_5m.gap

        bend_z_th = float(b_cfg.get("bend_z_threshold", 2.5))
        bend_mild_z_th = float(b_cfg.get("bend_mild_z_threshold", 1.0))
        bend_min_l = float(b_cfg.get("bend_min_lakhs", 10.0))

        std_gap = float(np.std(gap_5m_history)) if gap_5m_history and len(gap_5m_history) > 1 else 1.0
        z_gap = (gap_5m / std_gap) if std_gap > 0 else 0.0
        if z_gap >= bend_z_th and gap_5m >= bend_min_l:
            gap_bend = "⇈⇈"
        elif z_gap <= -bend_z_th and gap_5m <= -bend_min_l:
            gap_bend = "⇊⇊"
        elif z_gap >= bend_mild_z_th:
            gap_bend = "↑"
        elif z_gap <= -bend_mild_z_th:
            gap_bend = "↓"
        else:
            gap_bend = "→"

        score_bend_z_th = float(b_cfg.get("score_bend_z_threshold", 2.5))
        score_bend_mild_z_th = float(b_cfg.get("score_bend_mild_z_threshold", 1.0))
        score_min_delta = float(b_cfg.get("score_bend_min_delta", 0.04))

        std_score = float(np.std(score_5m_history)) if score_5m_history and len(score_5m_history) > 1 else 0.05
        z_score = (score_5m / std_score) if std_score > 0 else 0.0
        if (z_score >= score_bend_z_th or score_5m >= 0.15) and abs(score_5m) >= 0.10:
            score_bend = "⇈⇈"
        elif (z_score <= -score_bend_z_th or score_5m <= -0.15) and abs(score_5m) >= 0.10:
            score_bend = "⇊⇊"
        elif z_score >= score_bend_mild_z_th or score_5m >= score_min_delta:
            score_bend = "↑"
        elif z_score <= -score_bend_mild_z_th or score_5m <= -score_min_delta:
            score_bend = "↓"
        else:
            score_bend = "→"

    return WriterBucketData(
        bucket_type=bucket_type.upper(),
        ce_strikes=ce_strikes,
        pe_strikes=pe_strikes,
        ce_sum=round(ce_sum, 1),
        pe_sum=round(pe_sum, 1),
        ce_dw=round(ce_dw_sum, 1),
        pe_dw=round(pe_dw_sum, 1),
        gap=round(gap, 1),
        score=round(score, 2),
        bias=bias,
        ratio_str=ratio_str,
        raw_ratio=round(raw_ratio, 2) if raw_ratio is not None else None,
        score_5m=round(score_5m, 2) if score_5m is not None else None,
        gap_5m=round(gap_5m, 1) if gap_5m is not None else None,
        score_bend=score_bend,
        gap_bend=gap_bend,
        recentred=bar_had_recentre,
    )


def format_bucket_terminal_lines(b: WriterBucketData) -> List[str]:
    """Formats the 2 terminal lines for WRITER BUCKETS inside the WRITER GAP block."""
    ce_min, ce_max = min(b.ce_strikes), max(b.ce_strikes)
    pe_min, pe_max = min(b.pe_strikes), max(b.pe_strikes)

    line1 = (
        f"  BUCKET {b.bucket_type:<3}  CE {ce_min:,.0f}–{ce_max:,.0f}: {b.ce_sum:>+4.0f}L (dw {b.ce_dw:>+4.0f}L) | "
        f"PE {pe_min:,.0f}–{pe_max:,.0f}: {b.pe_sum:>+4.0f}L (dw {b.pe_dw:>+4.0f}L)"
    )

    if b.score_5m is not None:
        chg_5m_str = f"5m Δ {b.score_5m:>+5.2f} {b.score_bend}"
    elif b.recentred:
        chg_5m_str = "5m Δ re-centred"
    else:
        chg_5m_str = "5m Δ —"

    ratio_part = f" | ratio {b.ratio_str}" if b.ratio_str else ""
    line2 = f"              score {b.score:>+5.2f} {b.bias} {b.gap_bend} | {chg_5m_str}{ratio_part}"
    return [line1, line2]


def log_writer_gap_event(csv_path: Path, row: dict) -> None:
    """Appends validated event to data/writer_gap.csv."""
    import csv
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp", "source", "read", "how", "who_ce", "who_pe", "breadth",
        "net_gap", "price_agree", "wall_migration", "is_expiry", "spot",
        "spot_plus_5m", "spot_plus_15m", "spot_plus_30m", "spot_plus_60m",
        "otm_score", "otm_gap", "otm_score_5m", "otm_gap_5m", "otm_ratio",
        "itm_score", "itm_gap", "itm_score_5m", "itm_gap_5m", "itm_ratio",
        "bucket_otm_score", "bucket_otm_gap", "bucket_otm_score_5m", "bucket_otm_gap_5m", "bucket_otm_ratio",
        "bucket_itm_score", "bucket_itm_gap", "bucket_itm_score_5m", "bucket_itm_gap_5m", "bucket_itm_ratio",
    ]
    file_exists = csv_path.exists()
    if file_exists:
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                existing_cols = [c.strip() for c in first_line.split(",")]
            if not all(col in existing_cols for col in fieldnames):
                import pandas as pd
                old_df = pd.read_csv(csv_path)
                for col in fieldnames:
                    if col not in old_df.columns:
                        old_df[col] = None
                old_df = old_df.reindex(columns=fieldnames)
                old_df.to_csv(csv_path, index=False)
        except Exception as exc:
            logger.warning("Could not migrate headers in %s: %s", csv_path, exc)

    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.warning("Could not append to %s: %s", csv_path, exc)


def format_writer_gap_3m_block(poll_res: WriterGapPollResult) -> str:
    """Formats Output A: 3-min briefing block (max 14 lines)."""
    lines = [
        "── WRITER GAP ──────────────────────────────────────────",
        "  Strike   |  CE ΔOI  |  PE ΔOI  |   Gap   | Side",
    ]
    for k in poll_res.basket_strikes:
        s_data = poll_res.strikes.get(k)
        if s_data:
            lines.append(
                f"  {k:<8,.0f} | {s_data.ce_d:>+7.1f}L | {s_data.pe_d:>+7.1f}L | {s_data.gap:>+7.1f}L | {s_data.side}"
            )
    lines.append(
        f"  NET (3)  | {poll_res.net_ce_d:>+7.1f}L | {poll_res.net_pe_d:>+7.1f}L | {poll_res.net_gap:>+7.1f}L | {poll_res.net_side}"
    )
    if poll_res.bucket_otm:
        lines.extend(format_bucket_terminal_lines(poll_res.bucket_otm))
    lines.append(f"  Gap trend: {poll_res.gap_trend_15m}")
    if poll_res.last_crossover:
        lines.append(
            f"  Last cross: {poll_res.last_crossover.clock_time} {poll_res.last_crossover.direction} via {poll_res.last_crossover.how}"
        )
    else:
        lines.append("  Last cross: None today")
    lines.append(f"  Walls: {poll_res.wall_migration.summary_str}")
    return "\n".join(lines[:14])


def format_writer_lines_5m_table(table_res: WriterLinesTableResult) -> str:
    """Formats Output B: 5-min closes table."""
    lines = [
        f"── WRITER LINES ({table_res.clock_time}) ──────────────────────────────────────",
        "  Strike  |  CE 5m  | Bend | Who |  PE 5m  | Bend | Who |  Gap 5m  | State",
    ]
    for k in table_res.basket_strikes:
        s = table_res.strikes.get(k)
        if s:
            lines.append(
                f"  {k:<7,.0f} | {s.ce_x_5m:>+6.1f}L |  {s.ce_bend:^4} | {s.ce_who:^3} | {s.pe_x_5m:>+6.1f}L |  {s.pe_bend:^4} | {s.pe_who:^3} | {s.gap_5m:>+7.1f}L | {s.state}"
            )
    lines.append(
        f"  NET (3) | {table_res.net_ce_x_5m:>+6.1f}L | {table_res.breadth_ce}  |     | {table_res.net_pe_x_5m:>+6.1f}L | {table_res.breadth_pe}  |     | {table_res.net_gap_5m:>+7.1f}L | {table_res.net_state}"
    )
    lines.append(f"  READ: {table_res.read.read_text}")
    lines.append(f"  Price: {table_res.read.price_agree_str} | Walls: {table_res.read.wall_migration_str}")
    return "\n".join(lines)


def determine_writer_lines_read(
    strikes: Dict[float, WriterGapStrikeData],
    net_gap: float,
    gap_5m: float,
    spot_chg_5m: float,
    wall_info: WallMigrationInfo,
    is_expiry: bool = False,
    rules: Optional[dict] = None,
    bar_state: str = "",
    gap_narrowing_2bars: bool = False,
    gap_widening: bool = False,
    timestamp: Optional[datetime] = None,
) -> WriterLinesRead:
    """
    Evaluates the READ line for 5-min closes:
    Breadth >= 2 required for any IMPULSE; price divergence downgrades to WATCH.
    """
    from collections import Counter

    r = rules or load_writer_gap_rules()
    ts = timestamp or datetime.now()

    ce_pairs = [(s.ce_bend, s.ce_who_full) for s in strikes.values()]
    pe_pairs = [(s.pe_bend, s.pe_who_full) for s in strikes.values()]

    ce_ctr = Counter(ce_pairs).most_common(1)
    pe_ctr = Counter(pe_pairs).most_common(1)

    dom_ce_bend, dom_ce_who = ce_ctr[0][0] if ce_ctr else ("→", "UNCLEAR")
    ce_breadth = ce_ctr[0][1] if ce_ctr else 0

    dom_pe_bend, dom_pe_who = pe_ctr[0][0] if pe_ctr else ("→", "UNCLEAR")
    pe_breadth = pe_ctr[0][1] if pe_ctr else 0

    agree_price, agree_str = compute_price_agreement(gap_5m, spot_chg_5m)

    read_str = "NEUTRAL"
    raw_read = "NEUTRAL"
    is_impulse = False
    is_downgraded = False

    if ce_breadth >= 2 and dom_ce_bend == "⇊⇊" and dom_ce_who == "COVERING":
        raw_read = "BULLISH IMPULSE"
        is_impulse = True
        if not agree_price:
            is_downgraded = True
            read_str = "🟢 BULLISH WATCH (short covering)"
        else:
            read_str = "🟢 BULLISH IMPULSE (short covering)"
    elif pe_breadth >= 2 and dom_pe_bend == "⇈⇈" and dom_pe_who == "WRITING":
        raw_read = "BULLISH SUPPORT"
        read_str = "🟢 BULLISH SUPPORT"
    elif ce_breadth >= 2 and dom_ce_bend == "⇈⇈" and dom_ce_who == "BUYING":
        raw_read = "BULLISH (call buying)"
        read_str = "🟢 BULLISH (call buying, not a lid)"
    elif pe_breadth >= 2 and dom_pe_bend == "⇊⇊" and dom_pe_who == "COVERING":
        raw_read = "BEARISH IMPULSE"
        is_impulse = True
        if not agree_price:
            is_downgraded = True
            read_str = "🔴 BEARISH WATCH (floor giving way)"
        else:
            read_str = "🔴 BEARISH IMPULSE (floor giving way)"
    elif ce_breadth >= 2 and dom_ce_bend == "⇈⇈" and dom_ce_who == "WRITING":
        raw_read = "BEARISH RESISTANCE"
        read_str = "🔴 BEARISH RESISTANCE"
    elif pe_breadth >= 2 and dom_pe_bend == "⇈⇈" and dom_pe_who == "BUYING":
        raw_read = "BEARISH (put buying)"
        read_str = "🔴 BEARISH (put buying, not a floor)"
    elif dom_ce_bend == "⇈⇈" and dom_pe_bend == "⇈⇈" and dom_ce_who == "WRITING" and dom_pe_who == "WRITING":
        raw_read = "RANGE TIGHTENING"
        read_str = "RANGE TIGHTENING"
    elif dom_ce_bend == "⇊⇊" and dom_pe_bend == "⇊⇊":
        raw_read = "BREAKOUT PENDING"
        read_str = "BREAKOUT PENDING"
    elif bar_state == "CROSSED":
        raw_read = "TREND CHANGE"
        read_str = "TREND CHANGE"
    elif bar_state == "INTERTWINED":
        raw_read = "CONSOLIDATION"
        read_str = "CONSOLIDATION"
    elif gap_narrowing_2bars:
        raw_read = "WEAKENING"
        read_str = "WEAKENING"
    elif gap_widening:
        raw_read = "CONTINUATION"
        read_str = "CONTINUATION"
    else:
        raw_read = "NEUTRAL"
        read_str = "NEUTRAL"

    bending_leg = "NONE"
    if dom_ce_bend in ("⇈⇈", "⇊⇊"):
        bending_leg = "CE"
    elif dom_pe_bend in ("⇈⇈", "⇊⇊"):
        bending_leg = "PE"

    unclear_warning = False
    if bending_leg == "CE" and dom_ce_who == "UNCLEAR":
        unclear_warning = True
        read_str += " (writer/buyer unclear)"
    elif bending_leg == "PE" and dom_pe_who == "UNCLEAR":
        unclear_warning = True
        read_str += " (writer/buyer unclear)"

    if is_expiry:
        read_str += " — EXPIRY — walls unstable"

    if strikes:
        leading_strike_row = max(strikes.values(), key=lambda r_s: abs(r_s.ce_x_5m) if bending_leg == "CE" else abs(r_s.pe_x_5m))
        leading_strike = leading_strike_row.strike
        leading_chg = leading_strike_row.ce_x_5m if bending_leg == "CE" else leading_strike_row.pe_x_5m
        leading_iv = leading_strike_row.ce_iv_chg if bending_leg == "CE" else leading_strike_row.pe_iv_chg
    else:
        leading_strike = 0.0
        leading_chg = 0.0
        leading_iv = 0.0

    read_record = WriterLinesRead(
        timestamp=ts,
        clock_time=ts.strftime("%H:%M"),
        read_text=read_str,
        raw_read=raw_read,
        breadth_str=f"{ce_breadth}/3" if bending_leg == "CE" else f"{pe_breadth}/3",
        breadth_count=ce_breadth if bending_leg == "CE" else pe_breadth,
        bending_leg=bending_leg,
        dominant_who_ce=f"{dom_ce_who} {ce_breadth}/3",
        dominant_who_pe=f"{dom_pe_who} {pe_breadth}/3",
        gap_5m=round(gap_5m, 1),
        net_gap=round(net_gap, 1),
        spot=0.0,
        price_agree=agree_price,
        price_agree_str=agree_str,
        wall_migration_str=wall_info.status if wall_info else "walls steady",
        is_expiry=is_expiry,
        is_impulse=is_impulse,
        is_downgraded=is_downgraded,
        unclear_warning=unclear_warning,
        leading_strike=leading_strike,
        leading_change=round(leading_chg, 1),
        leading_iv_chg=round(leading_iv, 1),
    )
    read_record.telegram_text = format_impulse_telegram(read_record)
    return read_record


def evaluate_writer_gap_session(
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    sess_date: date,
    rules: Optional[dict] = None,
) -> WriterGapSessionResult:
    """
    Complete evaluation engine for WRITER GAP (3-min) and WRITER LINES (5-min closes).
    """
    from collections import Counter

    r = rules or load_writer_gap_rules()
    date_str = sess_date.strftime("%Y-%m-%d")

    mkt = filter_market_hours(all_snapshots)
    if not mkt:
        return WriterGapSessionResult(
            session_date=date_str,
            is_expiry_day=False,
            is_expiry_eve=False,
            min_gap_lakhs=float(r.get("min_gap_lakhs", 20.0)),
            basket_changes=[],
            poll_results=[],
            table_results=[],
            crossovers=[],
            reads=[],
            unclear_evals=0,
            total_evals=0,
        )

    is_exp_day, is_exp_eve = is_writer_gap_expiry(sess_date, mkt[0][2])
    is_expiry_session = is_exp_day or is_exp_eve

    min_gap_lakhs = float(r.get("min_gap_lakhs", 20.0))
    if is_expiry_session:
        min_gap_lakhs *= 2.0

    lot_size = float(r.get("lot_size", 65))
    iv_noise = float(r.get("iv_noise", 0.3))
    resid_noise = float(r.get("resid_noise", 0.1))
    bend_z = float(r.get("bend_z_threshold", 2.5))
    bend_min_l = float(r.get("bend_min_lakhs", 10.0))
    bend_mild_z = float(r.get("bend_mild_z_threshold", 1.0))
    step = 50.0

    # 3-min resample grid from 09:15 to 15:30
    grid_3m = []
    cur = datetime.combine(sess_date, dtime(9, 15))
    end = datetime.combine(sess_date, dtime(15, 30))
    while cur <= end and cur <= mkt[-1][0]:
        grid_3m.append(cur)
        cur += timedelta(minutes=3)

    snaps_3m = []
    for gt in grid_3m:
        cand = [s for s in mkt if s[0] <= gt]
        if cand:
            snaps_3m.append((gt, cand[-1][1], cand[-1][2]))

    # 5m close grid
    grid_5m = []
    cur = datetime.combine(sess_date, dtime(9, 20))
    while cur <= end and cur <= mkt[-1][0]:
        grid_5m.append(cur)
        cur += timedelta(minutes=5)
    snaps_5m = []
    for gt in grid_5m:
        cand = [s for s in mkt if s[0] <= gt]
        if cand:
            snaps_5m.append((gt, cand[-1][1], cand[-1][2]))

    def get_forward_spot(ts: datetime, minutes: int) -> Optional[float]:
        target_t = ts + timedelta(minutes=minutes)
        cand = [s for s in mkt if s[0] >= target_t]
        if cand:
            return round(cand[0][1], 1)
        return None

    def get_snap_30m_ago(target_t):
        t30 = target_t - timedelta(minutes=30)
        cand = [s for s in snaps_3m if s[0] <= t30]
        return cand[-1][2] if cand else None

    def get_snap_15m_ago(target_t):
        t15 = target_t - timedelta(minutes=15)
        cand = [s for s in snaps_3m if s[0] <= t15]
        return cand[-1] if cand else None

    basket = None
    basket_selected_time = None
    basket_changes = []
    basket_changes_times = set()

    confirmed_crossovers: List[WriterCrossoverEvent] = []
    last_confirmed_crossover: Optional[WriterCrossoverEvent] = None
    prior_confirmed_side: Optional[str] = None
    candidate_crossover_side: Optional[str] = None
    candidate_crossover_count = 0
    candidate_crossover_polls = []
    candidate_crossover_prior_poll = None
    prev_poll_info = None

    gap_history_30m = []
    poll_results: List[WriterGapPollResult] = []
    poll_results_by_time = {}

    # 1. Evaluate 3-min polls
    for idx_3m, (ts, spot, df) in enumerate(snaps_3m):
        basket_changed = False
        reselect_needed = False
        if basket is None:
            reselect_needed = True
        else:
            time_elapsed = (ts - basket_selected_time).total_seconds() / 60.0
            min_k, max_k = min(basket), max(basket)
            spot_outside = (spot < min_k - step) or (spot > max_k + step)
            if time_elapsed >= 30.0 or spot_outside:
                reselect_needed = True

        if reselect_needed:
            new_basket = select_writer_basket(df, spot, r)
            if basket != new_basket:
                logger.debug("basket changed -> %s", new_basket)   # was print(): 60+ 'basket changed' lines a day in the log
                basket = new_basket
                basket_changed = True
                basket_changes.append((ts, list(new_basket)))
                basket_changes_times.add(ts)
                candidate_crossover_side = None
                candidate_crossover_count = 0
                candidate_crossover_polls = []
            basket_selected_time = ts

        df_k = df.set_index("strike")
        strike_data_dict: Dict[float, WriterGapStrikeData] = {}
        for k in basket:
            if k in df_k.index:
                row = df_k.loc[k]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                c_d = float(row.get("ce_chg_oi", 0) or 0) * lot_size / 1e5
                p_d = float(row.get("pe_chg_oi", 0) or 0) * lot_size / 1e5
            else:
                c_d, p_d = 0.0, 0.0
            g_k = p_d - c_d
            sum_k = abs(c_d + p_d)
            if sum_k > 0 and abs(g_k) < 0.15 * sum_k:
                s_k = "INTERTWINED"
            elif g_k > 0:
                s_k = "PE_ABOVE"
            elif g_k < 0:
                s_k = "CE_ABOVE"
            else:
                s_k = "INTERTWINED"
            strike_data_dict[k] = WriterGapStrikeData(
                strike=k,
                ce_d=round(c_d, 2),
                pe_d=round(p_d, 2),
                gap=round(g_k, 2),
                side=s_k,
            )

        net_ce_d = sum(s.ce_d for s in strike_data_dict.values())
        net_pe_d = sum(s.pe_d for s in strike_data_dict.values())
        net_gap = net_pe_d - net_ce_d

        gap_history_30m.append((ts, net_gap))
        t30 = ts - timedelta(minutes=30)
        gap_history_30m = [item for item in gap_history_30m if item[0] >= t30]

        sign_flips = 0
        prev_sign = None
        for _, g_val in gap_history_30m:
            cur_s = 1 if g_val > 0 else (-1 if g_val < 0 else 0)
            if cur_s != 0:
                if prev_sign is not None and cur_s != prev_sign:
                    sign_flips += 1
                prev_sign = cur_s

        net_sum_d = abs(net_ce_d + net_pe_d)
        is_intertwined = (abs(net_gap) < 0.15 * net_sum_d if net_sum_d > 0 else True) or (sign_flips >= 2)
        if is_intertwined:
            net_side = "INTERTWINED"
        elif net_gap > 0:
            net_side = "PE_ABOVE"
        elif net_gap < 0:
            net_side = "CE_ABOVE"
        else:
            net_side = "INTERTWINED"

        prev_30m_df = get_snap_30m_ago(ts)
        wall_info = compute_wall_migration(df, prev_30m_df)

        snap_15m = get_snap_15m_ago(ts)
        gap_trend = "flat"
        if snap_15m:
            ts_15, spot_15, df_15 = snap_15m
            df15_k = df_15.set_index("strike")
            c15 = sum(float(df15_k.loc[k].get("ce_chg_oi", 0) or 0) * lot_size / 1e5 for k in basket if k in df15_k.index)
            p15 = sum(float(df15_k.loc[k].get("pe_chg_oi", 0) or 0) * lot_size / 1e5 for k in basket if k in df15_k.index)
            g15 = p15 - c15
            diff_g = abs(net_gap) - abs(g15)
            if diff_g > 1.0:
                gap_trend = f"widening ({net_gap - g15:+.1f}L vs 15m ago)"
            elif diff_g < -1.0:
                gap_trend = f"narrowing ({net_gap - g15:+.1f}L vs 15m ago)"
            else:
                gap_trend = f"steady ({net_gap - g15:+.1f}L vs 15m ago)"

        # Crossover confirmation tracking (Section 3)
        new_cross_event: Optional[WriterCrossoverEvent] = None
        current_net_sign = "PE_ABOVE" if net_gap > 0 else ("CE_ABOVE" if net_gap < 0 else None)

        if prior_confirmed_side is None:
            if abs(net_gap) >= min_gap_lakhs and current_net_sign is not None:
                prior_confirmed_side = current_net_sign
        else:
            if current_net_sign is not None and current_net_sign != prior_confirmed_side:
                if candidate_crossover_side == current_net_sign:
                    candidate_crossover_count += 1
                    candidate_crossover_polls.append((ts, spot, df, net_ce_d, net_pe_d, net_gap, strike_data_dict))
                else:
                    candidate_crossover_side = current_net_sign
                    candidate_crossover_count = 1
                    candidate_crossover_prior_poll = prev_poll_info
                    candidate_crossover_polls = [(ts, spot, df, net_ce_d, net_pe_d, net_gap, strike_data_dict)]

                if candidate_crossover_count >= 2 and not basket_changed and abs(net_gap) >= min_gap_lakhs:
                    direction = "BULLISH" if candidate_crossover_side == "PE_ABOVE" else "BEARISH"
                    prior_confirmed_side = candidate_crossover_side

                    p0 = candidate_crossover_polls[0]
                    p1 = candidate_crossover_polls[-1]
                    p_ref = candidate_crossover_prior_poll or p0

                    d_ce_tot = abs(p1[3] - p_ref[3])
                    d_pe_tot = abs(p1[4] - p_ref[4])

                    leg_moved_more = "CE" if d_ce_tot >= d_pe_tot else "PE"

                    leading_k = basket[0]
                    max_k_chg = 0.0
                    for k in basket:
                        # FIX 30-Sep-2026: the reference poll can come from BEFORE a basket change, so a new
                        # strike (e.g. 22750) is missing from its dict -> KeyError 22750.0 ("Error evaluating
                        # writer gap poll: 22750.0" / "writer lines bar: 22750.0", 17x a day). Skip such strikes.
                        a_k, b_k = p1[6].get(k), p_ref[6].get(k)
                        if a_k is None or b_k is None:
                            continue
                        if leg_moved_more == "CE":
                            chg = a_k.ce_d - b_k.ce_d
                        else:
                            chg = a_k.pe_d - b_k.pe_d
                        if abs(chg) >= abs(max_k_chg):
                            max_k_chg = chg
                            leading_k = k

                    r_curr = p1[2].set_index("strike").loc[leading_k] if leading_k in p1[2].set_index("strike").index else None
                    r_prev = p_ref[2].set_index("strike").loc[leading_k] if leading_k in p_ref[2].set_index("strike").index else None

                    if r_curr is not None and r_prev is not None:
                        if leg_moved_more == "CE":
                            d_oi_leg = float(r_curr.get("ce_oi", 0) or 0) - float(r_prev.get("ce_oi", 0) or 0)
                            iv_c_now = float(r_curr.get("ce_iv", 0) or 0)
                            iv_c_old = float(r_prev.get("ce_iv", 0) or 0)
                            d_iv_leg = (iv_c_now - iv_c_old) if (iv_c_now >= 1.0 and iv_c_old >= 1.0) else None
                            delta_val = float(r_prev.get("ce_delta", 0.5) or 0.5)
                            ltp_chg = float(r_curr.get("ce_ltp", 0) or 0) - float(r_prev.get("ce_ltp", 0) or 0)
                        else:
                            d_oi_leg = float(r_curr.get("pe_oi", 0) or 0) - float(r_prev.get("pe_oi", 0) or 0)
                            iv_p_now = float(r_curr.get("pe_iv", 0) or 0)
                            iv_p_old = float(r_prev.get("pe_iv", 0) or 0)
                            d_iv_leg = (iv_p_now - iv_p_old) if (iv_p_now >= 1.0 and iv_p_old >= 1.0) else None
                            delta_val = float(r_prev.get("pe_delta", -0.5) or -0.5)
                            ltp_chg = float(r_curr.get("pe_ltp", 0) or 0) - float(r_prev.get("pe_ltp", 0) or 0)
                        d_sp = p1[1] - p_ref[1]
                        resid_val = ltp_chg - delta_val * d_sp
                        who_full, who_abbr = classify_who_is_acting(d_oi_leg, d_iv_leg, resid_val, iv_noise, resid_noise)
                    else:
                        who_full, who_abbr = ("COVERING" if leg_moved_more == "CE" and direction == "BULLISH" else "WRITING"), "COV"

                    how_str = f"{leg_moved_more} {who_full}"
                    if who_full == "COVERING" and leg_moved_more == "CE":
                        how_str += " (short covering)"

                    event_ts = p0[0]  # timestamp when the crossover flip occurred
                    event_spot = p0[1]
                    event_net_gap = p0[5]
                    new_cross_event = WriterCrossoverEvent(
                        timestamp=event_ts,
                        clock_time=event_ts.strftime("%H:%M"),
                        direction=direction,
                        net_gap=round(event_net_gap, 1),
                        spot=round(event_spot, 1),
                        how=how_str,
                        leg_moved_more=leg_moved_more,
                        classification=who_full,
                        leading_strike=leading_k,
                        leading_strike_change=round(max_k_chg, 1),
                        wall_migration_status=wall_info.status,
                        is_expiry=is_expiry_session,
                    )
                    new_cross_event.telegram_text = format_crossover_telegram(new_cross_event)
                    last_confirmed_crossover = new_cross_event
                    confirmed_crossovers.append(new_cross_event)

                    candidate_crossover_side = None
                    candidate_crossover_count = 0
                    candidate_crossover_polls = []
                    candidate_crossover_prior_poll = None
            else:
                candidate_crossover_side = None
                candidate_crossover_count = 0
                candidate_crossover_polls = []
                candidate_crossover_prior_poll = None

        prev_poll_info = (ts, spot, df, net_ce_d, net_pe_d, net_gap, strike_data_dict)

        poll_res = WriterGapPollResult(
            timestamp=ts,
            clock_time=ts.strftime("%H:%M"),
            spot=round(spot, 1),
            basket_strikes=list(basket),
            basket_changed=basket_changed,
            strikes=strike_data_dict,
            net_ce_d=round(net_ce_d, 1),
            net_pe_d=round(net_pe_d, 1),
            net_gap=round(net_gap, 1),
            net_side=net_side,
            gap_trend_15m=gap_trend,
            wall_migration=wall_info,
            last_crossover=last_confirmed_crossover,
            new_crossover=new_cross_event,
            is_expiry=is_expiry_session,
            briefing_block_text="",
        )
        poll_res.briefing_block_text = format_writer_gap_3m_block(poll_res)
        poll_results.append(poll_res)
        poll_results_by_time[ts] = poll_res

    # 2. Evaluate 5-min closes (Output B)
    table_results: List[WriterLinesTableResult] = []
    reads: List[WriterLinesRead] = []
    changes_5m_history: Dict[Tuple[float, str], List[float]] = {}
    past_2_gaps = []
    unclear_count = 0
    total_who_evals = 0

    for idx_5m in range(1, len(snaps_5m)):
        t_prev, spot_prev, df_prev = snaps_5m[idx_5m - 1]
        t_curr, spot_curr, df_curr = snaps_5m[idx_5m]
        d_spot_5m = spot_curr - spot_prev

        bar_had_basket_change = any(t_prev < bc <= t_curr for bc in basket_changes_times)

        cand_poll = [p for p in snaps_3m if p[0] <= t_curr]
        cur_basket = select_writer_basket(df_curr, spot_curr, r) if not cand_poll else poll_results_by_time[cand_poll[-1][0]].basket_strikes

        df_curr_k = df_curr.set_index("strike")
        df_prev_k = df_prev.set_index("strike")

        strike_5m_data: Dict[float, WriterGapStrikeData] = {}
        bends_ce = []
        bends_pe = []
        who_ce_list = []
        who_pe_list = []

        for k in cur_basket:
            cur_ce_oi = float(df_curr_k.loc[k].get("ce_oi", 0) or 0) if k in df_curr_k.index else 0
            prev_ce_oi = float(df_prev_k.loc[k].get("ce_oi", 0) or 0) if k in df_prev_k.index else 0
            ce_x_5m = (cur_ce_oi - prev_ce_oi) * lot_size / 1e5

            cur_pe_oi = float(df_curr_k.loc[k].get("pe_oi", 0) or 0) if k in df_curr_k.index else 0
            prev_pe_oi = float(df_prev_k.loc[k].get("pe_oi", 0) or 0) if k in df_prev_k.index else 0
            pe_x_5m = (cur_pe_oi - prev_pe_oi) * lot_size / 1e5

            gap_5m_k = pe_x_5m - ce_x_5m

            if (k, "CE") not in changes_5m_history:
                changes_5m_history[(k, "CE")] = []
            if (k, "PE") not in changes_5m_history:
                changes_5m_history[(k, "PE")] = []
            changes_5m_history[(k, "CE")].append(ce_x_5m)
            changes_5m_history[(k, "PE")].append(pe_x_5m)

            std_ce = np.std(changes_5m_history[(k, "CE")]) if len(changes_5m_history[(k, "CE")]) > 1 else 1.0
            std_pe = np.std(changes_5m_history[(k, "PE")]) if len(changes_5m_history[(k, "PE")]) > 1 else 1.0

            z_ce = ce_x_5m / std_ce if std_ce > 0 else 0.0
            z_pe = pe_x_5m / std_pe if std_pe > 0 else 0.0

            if bar_had_basket_change:
                ce_bend = "→"
                pe_bend = "→"
            else:
                if z_ce >= bend_z and ce_x_5m >= bend_min_l:
                    ce_bend = "⇈⇈"
                elif z_ce <= -bend_z and ce_x_5m <= -bend_min_l:
                    ce_bend = "⇊⇊"
                elif z_ce >= bend_mild_z:
                    ce_bend = "↑"
                elif z_ce <= -bend_mild_z:
                    ce_bend = "↓"
                else:
                    ce_bend = "→"

                if z_pe >= bend_z and pe_x_5m >= bend_min_l:
                    pe_bend = "⇈⇈"
                elif z_pe <= -bend_z and pe_x_5m <= -bend_min_l:
                    pe_bend = "⇊⇊"
                elif z_pe >= bend_mild_z:
                    pe_bend = "↑"
                elif z_pe <= -bend_mild_z:
                    pe_bend = "↓"
                else:
                    pe_bend = "→"

            r_now = df_curr_k.loc[k] if k in df_curr_k.index else None
            r_old = df_prev_k.loc[k] if k in df_prev_k.index else None

            iv_ce_chg = 0.0
            iv_pe_chg = 0.0
            if r_now is not None and r_old is not None:
                d_oi_c = cur_ce_oi - prev_ce_oi
                iv_c_now = float(r_now.get("ce_iv", 0) or 0)
                iv_c_old = float(r_old.get("ce_iv", 0) or 0)
                d_iv_c = (iv_c_now - iv_c_old) if (iv_c_now >= 1.0 and iv_c_old >= 1.0) else None
                if d_iv_c is not None:
                    iv_ce_chg = d_iv_c
                ltp_c_chg = float(r_now.get("ce_ltp", 0) or 0) - float(r_old.get("ce_ltp", 0) or 0)
                delta_c = float(r_old.get("ce_delta", 0.5) or 0.5)
                resid_c = ltp_c_chg - delta_c * d_spot_5m
                who_ce_full, who_ce_abbr = classify_who_is_acting(d_oi_c, d_iv_c, resid_c, iv_noise, resid_noise)

                d_oi_p = cur_pe_oi - prev_pe_oi
                iv_p_now = float(r_now.get("pe_iv", 0) or 0)
                iv_p_old = float(r_old.get("pe_iv", 0) or 0)
                d_iv_p = (iv_p_now - iv_p_old) if (iv_p_now >= 1.0 and iv_p_old >= 1.0) else None
                if d_iv_p is not None:
                    iv_pe_chg = d_iv_p
                ltp_p_chg = float(r_now.get("pe_ltp", 0) or 0) - float(r_old.get("pe_ltp", 0) or 0)
                delta_p = float(r_old.get("pe_delta", -0.5) or -0.5)
                resid_p = ltp_p_chg - delta_p * d_spot_5m
                who_pe_full, who_pe_abbr = classify_who_is_acting(d_oi_p, d_iv_p, resid_p, iv_noise, resid_noise)
            else:
                who_ce_full, who_ce_abbr = "UNCLEAR", "?"
                who_pe_full, who_pe_abbr = "UNCLEAR", "?"

            total_who_evals += 2
            if who_ce_abbr == "?":
                unclear_count += 1
            if who_pe_abbr == "?":
                unclear_count += 1

            bends_ce.append(ce_bend)
            bends_pe.append(pe_bend)
            who_ce_list.append((who_ce_full, who_ce_abbr))
            who_pe_list.append((who_pe_full, who_pe_abbr))

            c_d = float(r_now.get("ce_chg_oi", 0) or 0) * lot_size / 1e5 if r_now is not None else 0
            p_d = float(r_now.get("pe_chg_oi", 0) or 0) * lot_size / 1e5 if r_now is not None else 0
            g_k = p_d - c_d
            sum_k = abs(c_d + p_d)
            if sum_k > 0 and abs(g_k) < 0.15 * sum_k:
                s_k = "INTERTWINED"
            elif g_k > 0:
                s_k = "PE_ABOVE"
            elif g_k < 0:
                s_k = "CE_ABOVE"
            else:
                s_k = "INTERTWINED"

            strike_5m_data[k] = WriterGapStrikeData(
                strike=k,
                ce_d=round(c_d, 2),
                pe_d=round(p_d, 2),
                gap=round(g_k, 2),
                side=s_k,
                ce_x_5m=round(ce_x_5m, 1),
                pe_x_5m=round(pe_x_5m, 1),
                ce_z_5m=round(z_ce, 2),
                pe_z_5m=round(z_pe, 2),
                ce_bend=ce_bend,
                pe_bend=pe_bend,
                ce_who=who_ce_abbr,
                pe_who=who_pe_abbr,
                ce_who_full=who_ce_full,
                pe_who_full=who_pe_full,
                ce_iv_chg=round(iv_ce_chg, 2),
                pe_iv_chg=round(iv_pe_chg, 2),
                gap_5m=round(gap_5m_k, 1),
                state=s_k,
            )

        net_ce_5m = sum(s.ce_x_5m for s in strike_5m_data.values())
        net_pe_5m = sum(s.pe_x_5m for s in strike_5m_data.values())
        net_gap_5m = net_pe_5m - net_ce_5m

        poll_curr = cand_poll[-1][0] if cand_poll else None
        curr_net_gap = poll_results_by_time[poll_curr].net_gap if poll_curr else (net_pe_5m - net_ce_5m)
        curr_net_side = poll_results_by_time[poll_curr].net_side if poll_curr else "INTERTWINED"

        past_2_gaps.append(abs(curr_net_gap))
        gap_narrowing_2bars = False
        gap_widening = False
        if len(past_2_gaps) >= 3:
            if past_2_gaps[-1] < past_2_gaps[-2] < past_2_gaps[-3]:
                gap_narrowing_2bars = True
            elif past_2_gaps[-1] > past_2_gaps[-2] > past_2_gaps[-3]:
                gap_widening = True

        crossed_now = any(t_prev < c.timestamp <= t_curr for c in confirmed_crossovers)
        if crossed_now:
            bar_state = "CROSSED"
        elif curr_net_side == "INTERTWINED":
            bar_state = "INTERTWINED"
        elif gap_narrowing_2bars:
            bar_state = "CONVERGING"
        else:
            bar_state = curr_net_side

        ce_pairs = [(b, w[0]) for b, w in zip(bends_ce, who_ce_list)]
        pe_pairs = [(b, w[0]) for b, w in zip(bends_pe, who_pe_list)]

        ce_ctr = Counter(ce_pairs).most_common(1)
        pe_ctr = Counter(pe_pairs).most_common(1)

        dom_ce_bend, dom_ce_who = ce_ctr[0][0] if ce_ctr else ("→", "UNCLEAR")
        ce_breadth = ce_ctr[0][1] if ce_ctr else 0

        dom_pe_bend, dom_pe_who = pe_ctr[0][0] if pe_ctr else ("→", "UNCLEAR")
        pe_breadth = pe_ctr[0][1] if pe_ctr else 0

        agree_price, agree_str = compute_price_agreement(net_gap_5m, d_spot_5m)

        prev_30m_df = get_snap_30m_ago(t_curr)
        wall_info_curr = compute_wall_migration(df_curr, prev_30m_df)

        read_record = determine_writer_lines_read(
            strikes=strike_5m_data,
            net_gap=curr_net_gap,
            gap_5m=net_gap_5m,
            spot_chg_5m=d_spot_5m,
            wall_info=wall_info_curr,
            is_expiry=is_expiry_session,
            rules=r,
            bar_state=bar_state,
            gap_narrowing_2bars=gap_narrowing_2bars,
            gap_widening=gap_widening,
            timestamp=t_curr,
        )
        read_record.spot = round(spot_curr, 1)
        reads.append(read_record)

        table_res = WriterLinesTableResult(
            timestamp=t_curr,
            clock_time=t_curr.strftime("%H:%M"),
            spot=round(spot_curr, 1),
            spot_chg_5m=round(d_spot_5m, 1),
            basket_strikes=list(cur_basket),
            strikes=strike_5m_data,
            net_ce_x_5m=round(net_ce_5m, 1),
            net_pe_x_5m=round(net_pe_5m, 1),
            net_gap_5m=round(net_gap_5m, 1),
            net_state=bar_state,
            breadth_ce=f"{ce_breadth}/3",
            breadth_pe=f"{pe_breadth}/3",
            read=read_record,
            table_text="",
            is_expiry=is_expiry_session,
        )
        table_res.table_text = format_writer_lines_5m_table(table_res)
        table_results.append(table_res)

    return WriterGapSessionResult(
        session_date=date_str,
        is_expiry_day=is_exp_day,
        is_expiry_eve=is_exp_eve,
        min_gap_lakhs=min_gap_lakhs,
        basket_changes=basket_changes,
        poll_results=poll_results,
        table_results=table_results,
        crossovers=confirmed_crossovers,
        reads=reads,
        unclear_evals=unclear_count,
        total_evals=total_who_evals,
    )


_SENT_TELEGRAM_EVENTS: Set[str] = set()


def export_writer_gap_to_csv(
    result: WriterGapSessionResult,
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    csv_path: Optional[Path] = None,
) -> Path:
    """
    Appends every confirmed crossover and every 5-min READ to data/writer_gap.csv:
    timestamp, source (cross|read), read, how, who_ce, who_pe, breadth,
    net_gap, price_agree, wall_migration, is_expiry, spot,
    spot +5 / +15 / +30 / +60 min.
    """
    target_path = csv_path or (config.DATA_DIR / "writer_gap.csv")
    mkt = filter_market_hours(all_snapshots)

    def get_forward_spot(ts: datetime, minutes: int) -> Optional[float]:
        target_t = ts + timedelta(minutes=minutes)
        cand = [s for s in mkt if s[0] >= target_t]
        if cand:
            return round(cand[0][1], 1)
        return None

    # Confirmed crossovers
    for c in result.crossovers:
        row = {
            "timestamp": c.timestamp.isoformat(),
            "source": "cross",
            "read": "",
            "how": c.how,
            "who_ce": "",
            "who_pe": "",
            "breadth": "",
            "net_gap": c.net_gap,
            "price_agree": "",
            "wall_migration": c.wall_migration_status,
            "is_expiry": c.is_expiry,
            "spot": c.spot,
            "spot_plus_5m": get_forward_spot(c.timestamp, 5),
            "spot_plus_15m": get_forward_spot(c.timestamp, 15),
            "spot_plus_30m": get_forward_spot(c.timestamp, 30),
            "spot_plus_60m": get_forward_spot(c.timestamp, 60),
        }
        log_writer_gap_event(target_path, row)

    # 5-min reads
    for r in result.reads:
        row = {
            "timestamp": r.timestamp.isoformat(),
            "source": "read",
            "read": r.read_text,
            "how": "",
            "who_ce": r.dominant_who_ce,
            "who_pe": r.dominant_who_pe,
            "breadth": r.breadth_str,
            "net_gap": r.net_gap,
            "price_agree": r.price_agree_str,
            "wall_migration": r.wall_migration_str,
            "is_expiry": r.is_expiry,
            "spot": r.spot,
            "spot_plus_5m": get_forward_spot(r.timestamp, 5),
            "spot_plus_15m": get_forward_spot(r.timestamp, 15),
            "spot_plus_30m": get_forward_spot(r.timestamp, 30),
            "spot_plus_60m": get_forward_spot(r.timestamp, 60),
        }
        log_writer_gap_event(target_path, row)

    return target_path


def dispatch_writer_alerts(
    crossover: Optional[WriterCrossoverEvent] = None,
    read: Optional[WriterLinesRead] = None,
    rules: Optional[dict] = None,
    send_func: Optional[Any] = None,
) -> Optional[str]:
    """
    Dispatches Telegram alerts for confirmed crossovers and 5-min IMPULSE reads.
    Combines alerts if both fire within 5 minutes for the same direction.
    Suppresses IMPULSE after 14:00 on expiry day.
    De-duplicates: never sends the same event twice.
    """
    global _SENT_TELEGRAM_EVENTS
    r = rules or load_writer_gap_rules()

    if send_func is None:
        try:
            import scheduler
            send_func = scheduler.send_telegram_alert
        except Exception:
            send_func = None

    crossover_to_send = None
    if crossover is not None:
        c_key = f"cross_{crossover.timestamp.isoformat()}_{crossover.direction}"
        if c_key not in _SENT_TELEGRAM_EVENTS:
            crossover_to_send = crossover
            _SENT_TELEGRAM_EVENTS.add(c_key)

    impulse_to_send = None
    if read is not None and read.is_impulse:
        # Expiry day post-14:00 suppression
        suppress = False
        if read.is_expiry and read.timestamp.time() >= dtime(14, 0):
            suppress = True

        if not suppress:
            r_key = f"impulse_{read.timestamp.isoformat()}_{read.raw_read}"
            if r_key not in _SENT_TELEGRAM_EVENTS:
                impulse_to_send = read
                _SENT_TELEGRAM_EVENTS.add(r_key)

    # Check combination if both fired within 5 minutes for the same direction
    combined_msg = None
    if crossover_to_send and impulse_to_send:
        diff_sec = abs((crossover_to_send.timestamp - impulse_to_send.timestamp).total_seconds())
        same_dir = (
            ("BULLISH" in crossover_to_send.direction and "BULLISH" in impulse_to_send.read_text) or
            ("BEARISH" in crossover_to_send.direction and "BEARISH" in impulse_to_send.read_text)
        )
        if diff_sec <= 300 and same_dir:
            combined_msg = combine_telegram_messages(crossover_to_send.telegram_text, impulse_to_send.telegram_text)
            if send_func:
                send_func(combined_msg)
            return combined_msg

    # Send individually if not combined
    dispatched = []
    if impulse_to_send:
        if send_func:
            send_func(impulse_to_send.telegram_text)
        dispatched.append(impulse_to_send.telegram_text)

    if crossover_to_send:
        if send_func:
            send_func(crossover_to_send.telegram_text)
        dispatched.append(crossover_to_send.telegram_text)

    return "\n\n".join(dispatched) if dispatched else None


def get_writer_gap_poll_live(
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    sess_date: date,
    rules: Optional[dict] = None,
) -> Optional[WriterGapPollResult]:
    """Evaluates the latest 3-min poll for the briefing."""
    sess_res = evaluate_writer_gap_session(all_snapshots, sess_date, rules)
    if sess_res.poll_results:
        return sess_res.poll_results[-1]
    return None


def get_writer_lines_bar_live(
    all_snapshots: List[Tuple[datetime, float, pd.DataFrame]],
    sess_date: date,
    rules: Optional[dict] = None,
) -> Optional[WriterLinesTableResult]:
    """Evaluates the latest 5-min close bar for periodic snapshots."""
    sess_res = evaluate_writer_gap_session(all_snapshots, sess_date, rules)
    if sess_res.table_results:
        return sess_res.table_results[-1]
    return None

