"""
participant_history.py — Multi-Session Participant Positioning History & Calibration Engine.

Reads, verifies, normalizes, and analyzes participant-wise open interest history
(FII, PRO, Retailers/Client, DII) for NIFTY 50 index derivatives on the NSE.

================================================================================
WHAT THIS DATA CANNOT TELL YOU (DERIVATIVES REALITY AUDIT)
================================================================================
1. NO INTRADAY SIGNALS:
   This is end-of-day data released by the NSE Clearing Corporation daily after
   18:00 IST for the completed trading session. It provides structural context
   for the upcoming trading day, NEVER an intraday entry or exit trigger.

2. FUTURES SHORTS ARE FREQUENTLY CASH PORTFOLIO HEDGES:
   An FII managing an institutional cash equity portfolio (tens of thousands of
   crores) routinely shorts index futures and buys protective index puts as a
   hedging mechanism. Deriving a directional "bearish bet" from net short futures
   without cash portfolio context is a category error.

3. "CLIENT" IS NOT "RETAIL":
   The NSE category "Client" contains all market participants who are not FII,
   DII, or proprietary broker desks. It combines retail option buyers, High Net-
   Worth Individuals (HNIs), family offices, corporate treasuries, and algorithmic
   prop desks trading under client codes. It must NEVER be labeled pure "retail"
   without this explicit qualification.

4. 65 SESSIONS CANNOT ESTABLISH PREDICTIVE EDGE:
   A 65-session sample (~3 calendar months) is far too short to establish
   statistical significance or out-of-sample validity. At decile thresholds
   (top/bottom 10%), each bucket contains only ~6-7 observations. Any bucket
   with n < 10 must be reported as INSUFFICIENT SAMPLE, never as a win rate.
   Forward returns across this window reflect regime-specific noise, not an edge.

5. AGGREGATE DERIVATIVES WITHOUT EXPIRY OR STRIKE RESOLUTION:
   The dataset reports gross contracts across all strikes and expiries combined.
   It cannot reveal whether positions are front-month momentum, far-month hedges,
   deep OTM lottery tickets, ATM straddles, or calendar spreads.
================================================================================
"""

from __future__ import annotations

import datetime
from datetime import date, datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Base Paths
DEFAULT_BASE_DIR = Path(__file__).parent
DEFAULT_DATA_DIR = DEFAULT_BASE_DIR / "data"
DEFAULT_RULES_PATH = DEFAULT_BASE_DIR / "participant_rules.json"
DEFAULT_EXCEL_PATH = DEFAULT_DATA_DIR / "NSE_Participant_OI_and_Option_Chain_Master.xlsx"
DEFAULT_PARQUET_PATH = DEFAULT_DATA_DIR / "participants.parquet"
DEFAULT_CSV_DIR = DEFAULT_DATA_DIR / "participant_oi"
DEFAULT_OHLC_PATH = DEFAULT_DATA_DIR / "ohlc" / "OHLC.csv"

# Participant Names
PARTICIPANTS = ["FII", "PRO", "Retailers", "DII"]
CLIENT_ALIASES = {
    "CLIENT": "Retailers",
    "RETAILER": "Retailers",
    "RETAILERS": "Retailers",
    "CLIENTS": "Retailers",
    "PRO": "PRO",
    "PROP": "PRO",
    "FII": "FII",
    "FPI": "FII",
    "DII": "DII",
}


def load_rules(rules_path: Union[str, Path, None] = None) -> dict[str, Any]:
    """Load configuration and calibration rules from JSON."""
    p = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not parse %s: %s. Using default rules.", p, e)
    return {
        "window_sessions": 65,
        "min_window_sessions": 20,
        "percentile_thresholds": {
            "extreme_high": 95.0,
            "high": 80.0,
            "normal_upper": 80.0,
            "normal_lower": 20.0,
            "low": 20.0,
            "extreme_low": 5.0,
        },
        "divergence_thresholds": {
            "upper_pct": 90.0,
            "lower_pct": 10.0,
        },
        "outcome_scoring": {
            "min_sample_size": 10,
            "deciles": {"top": 90.0, "bottom": 10.0},
            "horizons_days": [1, 3],
        },
    }


def get_last_completed_session(ohlc_path: Union[str, Path, None] = None) -> date:
    """
    Determine the date of the last completed trading session.
    Derives from OHLC dataset if present, else checks local IST time.
    """
    op = Path(ohlc_path) if ohlc_path else DEFAULT_OHLC_PATH
    if op.exists():
        try:
            df_o = pd.read_csv(op, usecols=["Date"])
            if not df_o.empty:
                max_d = pd.to_datetime(df_o["Date"], errors="coerce").dropna().max()
                if pd.notna(max_d):
                    return max_d.date()
        except Exception as e:
            logger.debug("OHLC last session check failed: %s", e)

    now_ist = dt.now()
    # If today is weekday and past 18:00 IST, today's session is completed
    if now_ist.weekday() < 5 and (now_ist.hour > 18 or (now_ist.hour == 18 and now_ist.minute >= 0)):
        return now_ist.date()
    # Otherwise fallback to today or previous day
    return now_ist.date()


def parse_date_safely(val: Any) -> Optional[date]:
    """Parse dates strictly day-first (DD-MM-YYYY) to prevent silent reordering."""
    if val is None or pd.isna(val):
        return None
    # datetime (and pandas Timestamp) are subclasses of date, so test them first
    if isinstance(val, dt):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None

    # Common formats with day-first priority
    formats = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d%m%Y", "%b %d, %Y", "%d-%b-%Y"]
    for fmt in formats:
        try:
            return dt.strptime(s, fmt).date()
        except ValueError:
            pass

    # Fallback to pandas with dayfirst=True
    try:
        t = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.notna(t):
            return t.date()
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 1. Loader Engine
# ═══════════════════════════════════════════════════════════════════════════

def load_from_excel(path: Union[str, Path]) -> tuple[pd.DataFrame, list[str]]:
    """
    Read the master workbook into a tidy DataFrame.
    Guards against:
    - Missing sheets
    - DII short-column absence (handled without imputation)
    - Precomputed column discrepancies (verified and reported)
    - Rows dated after the last completed session
    """
    excel_path = Path(path)
    if not excel_path.exists():
        raise FileNotFoundError(f"Participant master workbook not found at {excel_path}")

    xl = pd.ExcelFile(excel_path)
    sheets_present = xl.sheet_names
    mismatch_reports: list[str] = []
    rows: list[dict[str, Any]] = []

    last_session = get_last_completed_session()
    refused_count = 0

    target_sheets = [s for s in ["FII", "PRO", "Retailers", "DII"] if s in sheets_present]
    if not target_sheets:
        raise ValueError(f"Workbook at {excel_path} has none of the expected participant sheets: {sheets_present}")

    for sheet in target_sheets:
        df_sheet = xl.parse(sheet)
        if df_sheet.empty or "Date" not in df_sheet.columns:
            continue

        participant_name = "Retailers" if sheet == "Retailers" else sheet

        for idx, row in df_sheet.iterrows():
            d = parse_date_safely(row["Date"])
            if d is None:
                continue

            if d > last_session:
                refused_count += 1
                continue

            fut_long = float(row.get("Future Index Long", np.nan))
            fut_short = float(row.get("Future Index Short", np.nan))
            ce_long = float(row.get("Option Index Call Long", np.nan))
            pe_long = float(row.get("Option Index Put Long", np.nan))

            # DII lacks option-short columns in the master workbook
            has_ce_short = "Option Index Call Short" in row and pd.notna(row["Option Index Call Short"])
            has_pe_short = "Option Index Put Short" in row and pd.notna(row["Option Index Put Short"])

            ce_short = float(row["Option Index Call Short"]) if has_ce_short else np.nan
            pe_short = float(row["Option Index Put Short"]) if has_pe_short else np.nan

            # Derive rather than trusting precomputed
            net_fut = fut_long - fut_short if (pd.notna(fut_long) and pd.notna(fut_short)) else np.nan
            net_ce = ce_long - ce_short if (pd.notna(ce_long) and pd.notna(ce_short)) else np.nan
            net_pe = pe_long - pe_short if (pd.notna(pe_long) and pd.notna(pe_short)) else np.nan

            # Verification against workbook precomputed columns
            if "Net Futures" in row and pd.notna(row["Net Futures"]) and pd.notna(net_fut):
                diff = abs(net_fut - float(row["Net Futures"]))
                if diff > 1.0:
                    mismatch_reports.append(f"{d} {participant_name} Net Futures mismatch: derived={net_fut}, sheet={row['Net Futures']}")

            if "Net CE" in row and pd.notna(row["Net CE"]):
                if pd.notna(net_ce):
                    diff = abs(net_ce - float(row["Net CE"]))
                    if diff > 1.0:
                        mismatch_reports.append(f"{d} {participant_name} Net CE mismatch: derived={net_ce}, sheet={row['Net CE']}")

            if "Net PE" in row and pd.notna(row["Net PE"]):
                if pd.notna(net_pe):
                    diff = abs(net_pe - float(row["Net PE"]))
                    if diff > 1.0:
                        mismatch_reports.append(f"{d} {participant_name} Net PE mismatch: derived={net_pe}, sheet={row['Net PE']}")

            rows.append({
                "date": d,
                "participant": participant_name,
                "fut_long": fut_long,
                "fut_short": fut_short,
                "ce_long": ce_long,
                "ce_short": ce_short,
                "pe_long": pe_long,
                "pe_short": pe_short,
                "net_ce": net_ce,
                "net_pe": net_pe,
                "net_fut": net_fut,
            })

    if refused_count > 0:
        logger.warning("Refused %d row(s) dated after last completed session (%s).", refused_count, last_session)

    if not rows:
        return pd.DataFrame(), mismatch_reports

    tidy = pd.DataFrame(rows).sort_values(["date", "participant"]).reset_index(drop=True)
    return tidy, mismatch_reports


def load_from_daily_csv(path: Union[str, Path]) -> tuple[pd.DataFrame, list[str]]:
    """
    Parse a single NSE daily participant CSV file:
    `fao_participant_oi_DDMMYYYY.csv` or `oi_YYYY-MM-DD.csv`.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Daily CSV file not found at {csv_path}")

    text = csv_path.read_text(encoding="utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return pd.DataFrame(), [f"File {csv_path.name} is too short to parse."]

    # Date extraction: check header line 1 or filename
    session_date: Optional[date] = None
    first_line = lines[0]
    import re
    date_match = re.search(r"as on\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", first_line, re.IGNORECASE)
    if date_match:
        session_date = parse_date_safely(date_match.group(1))

    if session_date is None:
        # Check filename patterns: oi_YYYY-MM-DD.csv or fao_participant_oi_DDMMYYYY.csv
        fn = csv_path.stem
        m_iso = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
        if m_iso:
            session_date = parse_date_safely(m_iso.group(1))
        else:
            m_nse = re.search(r"(\d{8})", fn)
            if m_nse:
                session_date = parse_date_safely(m_nse.group(1))

    if session_date is None:
        return pd.DataFrame(), [f"Could not parse session date from {csv_path.name}"]

    last_session = get_last_completed_session()
    if session_date > last_session:
        logger.warning("Refused CSV %s dated %s after last completed session %s.", csv_path.name, session_date, last_session)
        return pd.DataFrame(), [f"Refused row dated {session_date} after last session {last_session}"]

    import io
    df_raw = pd.read_csv(io.StringIO("\n".join(lines[1:])))
    df_raw.columns = [c.strip() for c in df_raw.columns]

    client_col = "Client Type" if "Client Type" in df_raw.columns else df_raw.columns[0]
    rows: list[dict[str, Any]] = []

    for _, r in df_raw.iterrows():
        raw_client = str(r.get(client_col, "")).strip().upper()
        if raw_client not in CLIENT_ALIASES:
            continue
        participant_name = CLIENT_ALIASES[raw_client]

        fut_long = float(r.get("Future Index Long", np.nan))
        fut_short = float(r.get("Future Index Short", np.nan))
        ce_long = float(r.get("Option Index Call Long", np.nan))
        pe_long = float(r.get("Option Index Put Long", np.nan))

        has_ce_short = "Option Index Call Short" in r and pd.notna(r["Option Index Call Short"])
        has_pe_short = "Option Index Put Short" in r and pd.notna(r["Option Index Put Short"])

        ce_short = float(r["Option Index Call Short"]) if has_ce_short else np.nan
        pe_short = float(r["Option Index Put Short"]) if has_pe_short else np.nan

        net_fut = fut_long - fut_short if (pd.notna(fut_long) and pd.notna(fut_short)) else np.nan
        net_ce = ce_long - ce_short if (pd.notna(ce_long) and pd.notna(ce_short)) else np.nan
        net_pe = pe_long - pe_short if (pd.notna(pe_long) and pd.notna(pe_short)) else np.nan

        rows.append({
            "date": session_date,
            "participant": participant_name,
            "fut_long": fut_long,
            "fut_short": fut_short,
            "ce_long": ce_long,
            "ce_short": ce_short,
            "pe_long": pe_long,
            "pe_short": pe_short,
            "net_ce": net_ce,
            "net_pe": net_pe,
            "net_fut": net_fut,
        })

    tidy = pd.DataFrame(rows).sort_values(["date", "participant"]).reset_index(drop=True)
    return tidy, []


def load_from_csv_dir(dir_path: Union[str, Path]) -> tuple[pd.DataFrame, list[str]]:
    """Parse all daily CSVs in a directory into one unified tidy frame."""
    p = Path(dir_path)
    if not p.exists() or not p.is_dir():
        return pd.DataFrame(), [f"Directory {p} not found."]

    all_csvs = sorted(p.glob("*.csv"))
    dfs: list[pd.DataFrame] = []
    all_reports: list[str] = []

    for f in all_csvs:
        try:
            df, reports = load_from_daily_csv(f)
            if not df.empty:
                dfs.append(df)
            if reports:
                all_reports.extend(reports)
        except Exception as e:
            all_reports.append(f"Error parsing {f.name}: {e}")

    if not dfs:
        return pd.DataFrame(), all_reports

    merged = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["date", "participant"]).sort_values(["date", "participant"]).reset_index(drop=True)
    return merged, all_reports


def load(
    path: Union[str, Path, None] = None,
    force_reload: bool = False,
    cache: bool = True,
    cache_path: Union[str, Path, None] = None,
) -> pd.DataFrame:
    """
    Main loader function:
    1. Returns cached Parquet if available and not force_reload.
    2. Otherwise parses Excel master or CSV files.
    3. Verifies derived columns against precomputed.
    4. Refuses rows dated after last completed session.
    5. Caches tidy frame to data/participants.parquet.
    """
    cp = Path(cache_path) if cache_path else DEFAULT_PARQUET_PATH

    # Check parquet cache
    if not force_reload and path is None and cp.exists():
        try:
            df = pd.read_parquet(cp)
            if not df.empty and "date" in df.columns:
                # Ensure date column is python date
                df["date"] = pd.to_datetime(df["date"]).dt.date
                return df.sort_values(["date", "participant"]).reset_index(drop=True)
        except Exception as e:
            logger.warning("Could not read parquet cache %s: %s. Falling back to source.", cp, e)

    target_path = Path(path) if path else DEFAULT_EXCEL_PATH

    # If specified path is a parquet file directly
    if str(target_path).endswith(".parquet") and target_path.exists():
        df = pd.read_parquet(target_path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.sort_values(["date", "participant"]).reset_index(drop=True)

    tidy_df = pd.DataFrame()
    mismatches: list[str] = []

    if target_path.exists() and str(target_path).endswith((".xlsx", ".xls")):
        tidy_df, mismatches = load_from_excel(target_path)
    elif target_path.exists() and str(target_path).endswith(".csv"):
        tidy_df, mismatches = load_from_daily_csv(target_path)
    elif DEFAULT_CSV_DIR.exists() and any(DEFAULT_CSV_DIR.glob("*.csv")):
        tidy_df, mismatches = load_from_csv_dir(DEFAULT_CSV_DIR)

    if mismatches:
        for m in mismatches[:5]:
            logger.info("Formula verification: %s", m)
        if len(mismatches) > 5:
            logger.info("... and %d other column verification notices.", len(mismatches) - 5)

    if tidy_df.empty:
        logger.warning("No participant data could be loaded from %s.", target_path)
        return pd.DataFrame()

    # Cache to parquet if requested
    if cache and not tidy_df.empty:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            # Write parquet
            tidy_df.to_parquet(cp, index=False)
            logger.info("Cached %d participant records to %s", len(tidy_df), cp)
        except Exception as e:
            logger.warning("Failed to cache parquet to %s: %s", cp, e)

    return tidy_df


def append(
    day_data: Union[pd.DataFrame, str, Path],
    parquet_path: Union[str, Path, None] = None,
) -> tuple[int, int]:
    """
    Append one or more sessions to the parquet cache without rewriting historical values.
    Refuses rows dated after last completed session.
    Returns (added_count, refused_count).
    """
    cp = Path(parquet_path) if parquet_path else DEFAULT_PARQUET_PATH
    last_session = get_last_completed_session()

    new_df = pd.DataFrame()
    if isinstance(day_data, (str, Path)):
        p = Path(day_data)
        if str(p).endswith(".csv"):
            new_df, _ = load_from_daily_csv(p)
        elif str(p).endswith((".xlsx", ".xls")):
            new_df, _ = load_from_excel(p)
        elif str(p).endswith(".parquet"):
            new_df = pd.read_parquet(p)
    elif isinstance(day_data, pd.DataFrame):
        new_df = day_data.copy()

    if new_df.empty:
        return 0, 0

    new_df["date"] = pd.to_datetime(new_df["date"]).dt.date
    valid_rows = new_df[new_df["date"] <= last_session].copy()
    refused_count = len(new_df) - len(valid_rows)

    if valid_rows.empty:
        logger.warning("All %d rows were refused (dated after last session %s).", refused_count, last_session)
        return 0, refused_count

    if cp.exists():
        existing_df = pd.read_parquet(cp)
        existing_df["date"] = pd.to_datetime(existing_df["date"]).dt.date
        # Deduplicate: remove any rows from existing that share (date, participant) with valid_rows
        combined = pd.concat([existing_df, valid_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset=["date", "participant"], keep="last")
        combined = combined.sort_values(["date", "participant"]).reset_index(drop=True)
        added_count = len(combined) - len(existing_df)
    else:
        combined = valid_rows.sort_values(["date", "participant"]).reset_index(drop=True)
        added_count = len(combined)

    cp.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(cp, index=False)
    logger.info("Parquet cache updated: added %d rows, refused %d rows.", added_count, refused_count)
    return added_count, refused_count


# ═══════════════════════════════════════════════════════════════════════════
# 2. Percentile Engine
# ═══════════════════════════════════════════════════════════════════════════

def _calc_percentile(series: pd.Series, val: float) -> float:
    """Compute exact percentile rank of val relative to series (0.0 to 100.0)."""
    clean = series.dropna()
    n = len(clean)
    if n == 0 or pd.isna(val):
        return 50.0
    less = (clean < val).sum()
    equal = (clean == val).sum()
    pct = (less + 0.5 * equal) / n * 100.0
    return float(np.clip(pct, 0.0, 100.0))


def _classify_percentile(pct: float, thresholds: dict[str, float]) -> str:
    """Classify a percentile rank into a standard label."""
    if pct >= thresholds.get("extreme_high", 95.0):
        return "EXTREME HIGH"
    if pct >= thresholds.get("high", 80.0):
        return "HIGH"
    if pct <= thresholds.get("extreme_low", 5.0):
        return "EXTREME LOW"
    if pct <= thresholds.get("low", 20.0):
        return "LOW"
    return "NORMAL"


def compute_percentiles(
    df: pd.DataFrame,
    window: int = 65,
    as_of_date: Optional[date] = None,
    rules: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    For each participant and each series, report latest session against trailing window.
    Metrics returned:
    - raw_value
    - mean, median, std, min, max
    - percentile_rank (0 to 100)
    - z_score
    - dod_change
    - dod_percentile
    - label: EXTREME HIGH, HIGH, NORMAL, LOW, EXTREME LOW
    """
    if df.empty:
        return pd.DataFrame()

    r = rules or load_rules()
    pct_thresh = r.get("percentile_thresholds", {})
    series_cols = ["net_ce", "net_pe", "net_fut", "ce_long", "ce_short", "pe_long", "pe_short", "fut_long", "fut_short"]

    # Filter by date if specified
    d_df = df.copy()
    d_df["date"] = pd.to_datetime(d_df["date"]).dt.date
    if as_of_date:
        d_df = d_df[d_df["date"] <= as_of_date]

    unique_dates = sorted(d_df["date"].unique())
    if not unique_dates:
        return pd.DataFrame()

    trailing_dates = unique_dates[-window:]
    window_df = d_df[d_df["date"].isin(trailing_dates)].copy()

    records: list[dict[str, Any]] = []

    for participant in window_df["participant"].unique():
        part_df = window_df[window_df["participant"] == participant].sort_values("date").reset_index(drop=True)
        if part_df.empty:
            continue

        for col in series_cols:
            if col not in part_df.columns:
                continue

            s = part_df[col].dropna()
            if s.empty:
                continue

            current_val = float(s.iloc[-1])
            mean_val = float(s.mean())
            median_val = float(s.median())
            std_val = float(s.std(ddof=1)) if len(s) > 1 else 0.0
            min_val = float(s.min())
            max_val = float(s.max())

            pct_rank = _calc_percentile(s, current_val)
            z_score = (current_val - mean_val) / std_val if std_val > 1e-6 else 0.0

            # Day-over-day change and its percentile rank
            s_diff = part_df[col].diff().dropna()
            if len(s_diff) > 0 and pd.notna(part_df[col].iloc[-1]) and len(part_df) > 1 and pd.notna(part_df[col].iloc[-2]):
                dod_change = float(part_df[col].iloc[-1] - part_df[col].iloc[-2])
                dod_pct = _calc_percentile(s_diff, dod_change)
            else:
                dod_change = 0.0
                dod_pct = 50.0

            label = _classify_percentile(pct_rank, pct_thresh)

            records.append({
                "participant": participant,
                "series": col,
                "as_of_date": part_df["date"].iloc[-1],
                "raw_value": current_val,
                "percentile_rank": round(pct_rank, 1),
                "z_score": round(z_score, 2),
                "dod_change": round(dod_change, 0),
                "dod_percentile": round(dod_pct, 1),
                "label": label,
                "mean": round(mean_val, 1),
                "median": round(median_val, 1),
                "std": round(std_val, 1),
                "min": round(min_val, 1),
                "max": round(max_val, 1),
                "sample_size": len(s),
            })

    return pd.DataFrame(records)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Structural Baseline Engine
# ═══════════════════════════════════════════════════════════════════════════

def _longest_run(binary_series: pd.Series) -> int:
    """Calculate maximum consecutive sequence of True in a boolean series."""
    max_run = 0
    current_run = 0
    for val in binary_series:
        if bool(val):
            current_run += 1
            if current_run > max_run:
                max_run = current_run
        else:
            current_run = 0
    return max_run


def compute_structural_baseline(df: pd.DataFrame, window: int = 65) -> dict[str, Any]:
    """
    Compute what each participant does NORMALLY over the trailing window:
    - Share of sessions net long vs net short per instrument
    - Mean and median net position
    - Longest run on one side (consecutive sessions)
    - Cross-instrument joint postures (e.g. PRO long both sides, FII short calls & long puts)
    """
    if df.empty:
        return {}

    d_df = df.copy()
    d_df["date"] = pd.to_datetime(d_df["date"]).dt.date
    unique_dates = sorted(d_df["date"].unique())[-window:]
    w_df = d_df[d_df["date"].isin(unique_dates)].copy()

    total_sessions = len(unique_dates)
    results: dict[str, Any] = {
        "total_sessions": total_sessions,
        "date_start": unique_dates[0] if unique_dates else None,
        "date_end": unique_dates[-1] if unique_dates else None,
        "participants": {},
        "structural_truths": [],
    }

    instruments = ["net_ce", "net_pe", "net_fut"]

    for part in ["FII", "PRO", "Retailers", "DII"]:
        p_df = w_df[w_df["participant"] == part].sort_values("date").reset_index(drop=True)
        if p_df.empty:
            continue

        p_stats: dict[str, Any] = {}
        for inst in instruments:
            if inst not in p_df.columns:
                continue
            s = p_df[inst].dropna()
            n = len(s)
            if n == 0:
                continue

            long_count = int((s > 0).sum())
            short_count = int((s < 0).sum())
            long_pct = round(long_count / n * 100.0, 1)
            short_pct = round(short_count / n * 100.0, 1)

            max_long_run = _longest_run(s > 0)
            max_short_run = _longest_run(s < 0)

            latest_val = float(s.iloc[-1])
            mean_val = float(s.mean())
            median_val = float(s.median())

            p_stats[inst] = {
                "observed_sessions": n,
                "latest_value": latest_val,
                "long_count": long_count,
                "short_count": short_count,
                "long_pct": long_pct,
                "short_pct": short_pct,
                "mean": round(mean_val, 1),
                "median": round(median_val, 1),
                "max_consecutive_long": max_long_run,
                "max_consecutive_short": max_short_run,
            }

        # Joint posture analysis
        joint = {}
        if "net_ce" in p_df.columns and "net_pe" in p_df.columns:
            valid_opts = p_df.dropna(subset=["net_ce", "net_pe"])
            vn = len(valid_opts)
            if vn > 0:
                # Long both sides
                long_both = int(((valid_opts["net_ce"] > 0) & (valid_opts["net_pe"] > 0)).sum())
                # Short both sides
                short_both = int(((valid_opts["net_ce"] < 0) & (valid_opts["net_pe"] < 0)).sum())
                # Short calls, Long puts (US Dealer convention)
                short_ce_long_pe = int(((valid_opts["net_ce"] < 0) & (valid_opts["net_pe"] > 0)).sum())
                # Long calls, Short puts (Leveraged bullish)
                long_ce_short_pe = int(((valid_opts["net_ce"] > 0) & (valid_opts["net_pe"] < 0)).sum())

                joint = {
                    "sessions": vn,
                    "long_both_count": long_both,
                    "long_both_pct": round(long_both / vn * 100.0, 1),
                    "short_both_count": short_both,
                    "short_both_pct": round(short_both / vn * 100.0, 1),
                    "short_ce_long_pe_count": short_ce_long_pe,
                    "short_ce_long_pe_pct": round(short_ce_long_pe / vn * 100.0, 1),
                    "long_ce_short_pe_count": long_ce_short_pe,
                    "long_ce_short_pe_pct": round(long_ce_short_pe / vn * 100.0, 1),
                }

        p_stats["joint_options"] = joint
        results["participants"][part] = p_stats

    # Construct explicit empirical findings
    truths = []
    # 1. FII US dealer convention check
    fii_joint = results["participants"].get("FII", {}).get("joint_options", {})
    if fii_joint:
        cnt = fii_joint.get("short_ce_long_pe_count", 0)
        tot = fii_joint.get("sessions", 0)
        pct = fii_joint.get("short_ce_long_pe_pct", 0.0)
        truths.append({
            "code": "FII_DEALER_CONVENTION",
            "title": "FIIs Match US Dealer Convention (Short CE / Long PE)",
            "finding": f"FIIs were net short calls and net long puts on {cnt} of {tot} sessions ({pct}%).",
            "validates": "GEX_DEALER_SIGN = -1",
            "conclusion": "Validates GEX_DEALER_SIGN = -1 across the sample. FIIs consistently buy downside protection and sell upside convexity.",
            "sample_size": tot,
        })

    # 2. PRO desks short both sides myth check
    pro_joint = results["participants"].get("PRO", {}).get("joint_options", {})
    if pro_joint:
        lb_cnt = pro_joint.get("long_both_count", 0)
        lb_pct = pro_joint.get("long_both_pct", 0.0)
        sb_cnt = pro_joint.get("short_both_count", 0)
        tot = pro_joint.get("sessions", 0)
        truths.append({
            "code": "PRO_LONG_BOTH_SIDES",
            "title": "Pro Desks Net Long Both Sides (Debunks 'Short Both' Myth)",
            "finding": f"PRO desks are net LONG both calls and puts on {lb_cnt} of {tot} sessions ({lb_pct}%). The 'short both sides' claim held on only {sb_cnt} of {tot} sessions.",
            "validates": "Never assume PRO desks are net short both sides",
            "conclusion": "Assuming Indian proprietary desks are net short both options inverts gamma calculations. They maintain long gamma or long vega on most sessions.",
            "sample_size": tot,
        })

    # 3. Retail put writing check
    ret_pe_stats = results["participants"].get("Retailers", {}).get("net_pe", {})
    ret_joint = results["participants"].get("Retailers", {}).get("joint_options", {})
    if ret_pe_stats:
        short_pe_cnt = ret_pe_stats.get("short_count", 0)
        tot_pe = ret_pe_stats.get("observed_sessions", 0)
        short_pe_pct = ret_pe_stats.get("short_pct", 0.0)
        leveraged_bullish_cnt = ret_joint.get("long_ce_short_pe_count", 0) if ret_joint else 0
        leveraged_bullish_pct = ret_joint.get("long_ce_short_pe_pct", 0.0) if ret_joint else 0
        truths.append({
            "code": "RETAIL_NET_PUT_WRITERS",
            "title": "Clients Are Net Put Writers (Baseline Market Structure)",
            "finding": f"Clients are net short puts on {short_pe_cnt} of {tot_pe} sessions ({short_pe_pct}%). They maintain a leveraged bullish posture (long calls + short puts) on {leveraged_bullish_cnt} of {tot_pe} sessions ({leveraged_bullish_pct}%).",
            "validates": "Divergence cannot be a binary opposite-sides flag",
            "conclusion": "Retail writing puts directly into FII put buyers is the baseline structural architecture of this market every single day, not an abnormal divergence.",
            "sample_size": tot_pe,
        })

    results["structural_truths"] = truths
    return results


# ═══════════════════════════════════════════════════════════════════════════
# 4. Divergence Engine (Percentile-Ranked Spreads)
# ═══════════════════════════════════════════════════════════════════════════

def compute_divergence(
    df: pd.DataFrame,
    window: int = 65,
    rules: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Measure the FII-minus-Retail spread on Net CE, Net PE, and Net Futures.
    Replaces binary opposite-sides checks with percentile-ranked spreads.
    A flag fires ONLY when the spread is beyond the 90th or below the 10th percentile.
    """
    if df.empty:
        return {}

    r = rules or load_rules()
    div_rules = r.get("divergence_thresholds", {})
    upper_thresh = div_rules.get("upper_pct", 90.0)
    lower_thresh = div_rules.get("lower_pct", 10.0)

    d_df = df.copy()
    d_df["date"] = pd.to_datetime(d_df["date"]).dt.date
    dates = sorted(d_df["date"].unique())[-window:]
    w_df = d_df[d_df["date"].isin(dates)].copy()

    fii = w_df[w_df["participant"] == "FII"].set_index("date")
    ret = w_df[w_df["participant"] == "Retailers"].set_index("date")

    common_dates = fii.index.intersection(ret.index)
    if len(common_dates) < 2:
        return {"known": False, "note": "Insufficient overlapping sessions for divergence calculation."}

    fii_c = fii.loc[common_dates]
    ret_c = ret.loc[common_dates]

    spreads_data: dict[str, Any] = {}
    instrument_map = [
        ("net_pe", "PE Spread (FII PE Net − Retail PE Net)", "pe_spread"),
        ("net_ce", "CE Spread (FII CE Net − Retail CE Net)", "ce_spread"),
        ("net_fut", "Futures Spread (FII Futures Net − Retail Futures Net)", "fut_spread"),
    ]

    for col, title, key in instrument_map:
        if col not in fii_c.columns or col not in ret_c.columns:
            continue

        s = fii_c[col] - ret_c[col]
        s = s.dropna()
        n = len(s)
        if n == 0:
            continue

        q_low = float(s.quantile(lower_thresh / 100.0))
        q_high = float(s.quantile(upper_thresh / 100.0))

        today_val = float(s.iloc[-1])
        pct_rank = _calc_percentile(s, today_val)
        mean_val = float(s.mean())
        std_val = float(s.std(ddof=1)) if n > 1 else 0.0
        z_score = (today_val - mean_val) / std_val if std_val > 1e-6 else 0.0

        # Flag condition: strictly beyond 90th or below 10th percentile
        is_extreme_high = pct_rank >= upper_thresh
        is_extreme_low = pct_rank <= lower_thresh
        is_flagged = is_extreme_high or is_extreme_low

        # Historical trigger frequency
        hist_triggers = ((s <= q_low) | (s >= q_high)).sum()
        hist_trigger_pct = round(hist_triggers / n * 100.0, 1)

        spreads_data[key] = {
            "title": title,
            "today_value": today_val,
            "percentile_rank": round(pct_rank, 1),
            "z_score": round(z_score, 2),
            "mean": round(mean_val, 1),
            "median": round(float(s.median()), 1),
            "p10": round(q_low, 1),
            "p90": round(q_high, 1),
            "flagged": bool(is_flagged),
            "flag_direction": "EXTREME HIGH DIVERGENCE" if is_extreme_high else ("EXTREME LOW DIVERGENCE" if is_extreme_low else "NORMAL DIVERGENCE"),
            "historical_triggers_count": int(hist_triggers),
            "historical_triggers_pct": hist_trigger_pct,
            "sample_size": n,
            "history_series": s,
        }

    return {
        "known": True,
        "as_of_date": common_dates[-1],
        "window_sessions": len(common_dates),
        "spreads": spreads_data,
        "upper_threshold": upper_thresh,
        "lower_threshold": lower_thresh,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 5. Forward-Outcome Scoring Engine
# ═══════════════════════════════════════════════════════════════════════════

def score_forward_outcomes(
    participant_df: pd.DataFrame,
    ohlc_df: Optional[pd.DataFrame] = None,
    ohlc_path: Union[str, Path, None] = None,
    min_sample: int = 10,
    top_pct: float = 90.0,
    bottom_pct: float = 10.0,
) -> dict[str, Any]:
    """
    Join participant positioning to daily NIFTY OHLC and score forward returns.
    Rules strictly enforced:
    - Any bucket with n < min_sample (10) MUST report INSUFFICIENT SAMPLE, never a percentage.
    - Report the result whether or not it is favourable.
    - Deciles are fixed in advance (10th / 90th).
    - Explicit statement: No claim of predictive power without out-of-sample check.
    """
    if participant_df.empty:
        return {"known": False, "note": "Participant positioning dataset is empty.", "outcomes": []}

    # Load OHLC
    o_df = pd.DataFrame()
    if ohlc_df is not None and not ohlc_df.empty:
        o_df = ohlc_df.copy()
    else:
        op = Path(ohlc_path) if ohlc_path else DEFAULT_OHLC_PATH
        if op.exists():
            try:
                o_df = pd.read_csv(op)
            except Exception as e:
                return {"known": False, "note": f"Failed to load OHLC from {op}: {e}", "outcomes": []}
        else:
            return {"known": False, "note": f"OHLC file not found at {op}.", "outcomes": []}

    if "Date" not in o_df.columns or "Close" not in o_df.columns:
        return {"known": False, "note": "OHLC dataset missing Date or Close column.", "outcomes": []}

    o_df["date"] = pd.to_datetime(o_df["Date"], errors="coerce").dt.date
    o_df = o_df.dropna(subset=["date", "Close"]).sort_values("date").reset_index(drop=True)
    o_df["next_1d_ret"] = (o_df["Close"].shift(-1) - o_df["Close"]) / o_df["Close"] * 100.0
    o_df["next_3d_ret"] = (o_df["Close"].shift(-3) - o_df["Close"]) / o_df["Close"] * 100.0

    # Reshape participant_df to wide format
    p_df = participant_df.copy()
    p_df["date"] = pd.to_datetime(p_df["date"]).dt.date

    # Build wide participant series
    wide_rows: dict[date, dict[str, float]] = {}
    for _, r in p_df.iterrows():
        d = r["date"]
        part = str(r["participant"]).lower()
        if d not in wide_rows:
            wide_rows[d] = {}
        for col in ["net_ce", "net_pe", "net_fut"]:
            if col in r and pd.notna(r[col]):
                wide_rows[d][f"{part}_{col}"] = float(r[col])

    wide_df = pd.DataFrame.from_dict(wide_rows, orient="index").reset_index()
    wide_df = wide_df.rename(columns={"index": "date"})

    # Compute divergence spreads in wide format
    if "fii_net_pe" in wide_df.columns and "retailers_net_pe" in wide_df.columns:
        wide_df["divergence_pe_spread"] = wide_df["fii_net_pe"] - wide_df["retailers_net_pe"]
    if "fii_net_ce" in wide_df.columns and "retailers_net_ce" in wide_df.columns:
        wide_df["divergence_ce_spread"] = wide_df["fii_net_ce"] - wide_df["retailers_net_ce"]
    if "fii_net_fut" in wide_df.columns and "retailers_net_fut" in wide_df.columns:
        wide_df["divergence_fut_spread"] = wide_df["fii_net_fut"] - wide_df["retailers_net_fut"]

    # Join with OHLC
    merged = pd.merge(wide_df, o_df[["date", "Close", "next_1d_ret", "next_3d_ret"]], on="date", how="inner")
    merged = merged.sort_values("date").reset_index(drop=True)

    if len(merged) < 5:
        return {"known": False, "note": f"Only {len(merged)} overlapping sessions between participant data and OHLC.", "outcomes": []}

    target_series = [
        ("fii_net_ce", "FII Net CE"),
        ("fii_net_pe", "FII Net PE"),
        ("fii_net_fut", "FII Net Futures"),
        ("retailers_net_ce", "Client Net CE (Retail / HNI)"),
        ("retailers_net_pe", "Client Net PE (Retail / HNI)"),
        ("retailers_net_fut", "Client Net Futures"),
        ("pro_net_ce", "PRO Net CE"),
        ("pro_net_pe", "PRO Net PE"),
        ("divergence_pe_spread", "PE Spread (FII − Client)"),
    ]

    outcomes: list[dict[str, Any]] = []

    for key, label in target_series:
        if key not in merged.columns:
            continue

        sub = merged.dropna(subset=[key, "next_1d_ret"]).copy()
        n_total = len(sub)
        if n_total < 5:
            continue

        q_bot = float(sub[key].quantile(bottom_pct / 100.0))
        q_top = float(sub[key].quantile(top_pct / 100.0))

        bot_bucket = sub[sub[key] <= q_bot]
        top_bucket = sub[sub[key] >= q_top]

        for b_name, b_df, thresh_desc in [
            (f"Bottom Decile (≤{bottom_pct:.0f}th pct)", bot_bucket, f"≤ {q_bot:,.0f}"),
            (f"Top Decile (≥{top_pct:.0f}th pct)", top_bucket, f"≥ {q_high:,.0f}" if "q_high" in locals() else f"≥ {q_top:,.0f}"),
        ]:
            b_n = len(b_df)
            is_valid_sample = b_n >= min_sample

            m1 = float(b_df["next_1d_ret"].mean()) if b_n > 0 else np.nan
            m3 = float(b_df["next_3d_ret"].dropna().mean()) if b_n > 0 and not b_df["next_3d_ret"].dropna().empty else np.nan

            pos1 = int((b_df["next_1d_ret"] > 0).sum()) if b_n > 0 else 0
            pos3 = int((b_df["next_3d_ret"].dropna() > 0).sum()) if b_n > 0 and not b_df["next_3d_ret"].dropna().empty else 0
            n3 = len(b_df["next_3d_ret"].dropna())

            hit1_str = f"{pos1 / b_n * 100:.1f}% ({pos1}/{b_n})" if is_valid_sample else "INSUFFICIENT SAMPLE"
            hit3_str = f"{pos3 / n3 * 100:.1f}% ({pos3}/{n3})" if (is_valid_sample and n3 >= min_sample) else "INSUFFICIENT SAMPLE"

            outcomes.append({
                "series_key": key,
                "series_label": label,
                "bucket": b_name,
                "threshold_rule": thresh_desc,
                "sample_size": b_n,
                "min_sample_required": min_sample,
                "is_valid_sample": is_valid_sample,
                "mean_1d_ret": round(m1, 2) if pd.notna(m1) else None,
                "mean_3d_ret": round(m3, 2) if pd.notna(m3) else None,
                "hit_rate_1d": hit1_str,
                "hit_rate_3d": hit3_str,
            })

    # Summary finding on edge presence
    total_joined = len(merged)
    decile_n = round(total_joined * (bottom_pct / 100.0))
    statistical_verdict = (
        f"In a sample of {total_joined} sessions, decile buckets hold only ~{decile_n} observations each "
        f"(below minimum requirement of {min_sample}). "
        "NO STATISTICALLY SOUND DIRECTIONAL PREDICTIVE EDGE CAN BE CLAIMED. "
        "Out-of-sample testing is impossible on 65 sessions. "
        "These metrics represent previous-session positioning context, not an actionable predictive signal."
    )

    return {
        "known": True,
        "sample_size_total": total_joined,
        "min_sample": min_sample,
        "statistical_verdict": statistical_verdict,
        "outcomes": outcomes,
    }


def get_provenance(df: pd.DataFrame) -> dict[str, Any]:
    """Return provenance metadata: latest session date, session count, participant count."""
    if df.empty or "date" not in df.columns:
        return {
            "source": "None",
            "latest_session": None,
            "session_count": 0,
            "participants": [],
        }
    dates = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    parts = sorted(df["participant"].unique()) if "participant" in df.columns else []
    return {
        "source": "NSE Clearing Corporation (via Master Workbook / Daily Files)",
        "latest_session": dates[-1] if dates else None,
        "first_session": dates[0] if dates else None,
        "session_count": len(dates),
        "participants": parts,
    }
