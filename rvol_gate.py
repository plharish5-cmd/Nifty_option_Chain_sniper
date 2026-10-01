"""
rvol_gate.py — Standalone NIFTY Relative Volume (RVOL) Gate Engine.

Computes time-of-day normalized cumulative Relative Volume for NIFTY using
intraday 5-minute candles (with Futures volume) stored in data/candles_futures/.

Formula:
  RVOL(T) = today's cumulative volume to T ÷ avg cumulative volume to T
            over last N completed sessions (same clock-time bucket).

All thresholds are read dynamically from strategy1_rules.json under "rvol".
No hardcoded values.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
RULES_PATH = REPO_ROOT / "strategy1_rules.json"
CANDLES_FUTURES_DIR = REPO_ROOT / "data" / "candles_futures"
CANDLES_5M_DIR = REPO_ROOT / "data" / "candles_5m"
GATE_LOG_PATH = REPO_ROOT / "data" / "rvol_gate_log.jsonl"


def load_rvol_config() -> Dict[str, Any]:
    """Load RVOL configuration from strategy1_rules.json under 'rvol'."""
    defaults = {
        "dead_zone": 0.85,
        "trend_zone": 1.30,
        "extreme_zone": 2.50,
        "baseline_sessions": 10,
        "min_baseline_sessions": 5,
        "gate_start": "09:30",
        "recheck_times": ["12:30", "14:00"],
        "labels": {
            "< 0.85": "DEAD ZONE — no trade",
            "0.85-1.30": "TRAP ZONE — no trade (false breakouts)",
            "1.30-2.50": "TREND ZONE — trade with system",
            "> 2.50": "EXTREME ZONE — caution, watch reversal",
        },
    }
    if RULES_PATH.exists():
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                rvol_cfg = data.get("rvol", {})
                defaults.update(rvol_cfg)
        except Exception as exc:
            logger.warning(f"Failed to read {RULES_PATH}: {exc}")
    return defaults


def _parse_date(d: Union[date, datetime, str]) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    return str(d).strip()


def _parse_time(t: Union[dtime, datetime, str]) -> dtime:
    if isinstance(t, datetime):
        return t.time()
    if isinstance(t, dtime):
        return t
    s = str(t).strip()
    if len(s) == 5:
        return datetime.strptime(s, "%H:%M").time()
    if len(s) == 8:
        return datetime.strptime(s, "%H:%M:%S").time()
    return datetime.strptime(s[:5], "%H:%M").time()


def _load_session_df(date_str: str) -> Optional[pd.DataFrame]:
    """Load 5m candle DataFrame for date_str, reading candles_5m first with candles_futures fallback."""
    for d in (CANDLES_5M_DIR, CANDLES_FUTURES_DIR):
        csv_path = d / f"{date_str}.csv"
        pq_path = d / f"{date_str}.parquet"

        path_to_read = None
        is_pq = False
        if csv_path.exists():
            path_to_read = csv_path
            is_pq = False
        elif pq_path.exists():
            path_to_read = pq_path
            is_pq = True

        if not path_to_read:
            continue

        try:
            df = pd.read_parquet(path_to_read) if is_pq else pd.read_csv(path_to_read)
            df = df.rename(columns={c: c.lower() for c in df.columns})
            if df.empty or "volume" not in df.columns:
                continue
            if "timestamp" not in df.columns:
                continue

            # If past session is incomplete (< 60 candles), try next fallback
            today_str = str(date.today())
            if date_str != today_str and len(df) < 60:
                continue

            # Standardize timestamp and time column
            df["dt"] = pd.to_datetime(df["timestamp"])
            df["time_str"] = df["dt"].dt.strftime("%H:%M")
            df["time_val"] = df["dt"].dt.time
            df = df.sort_values("dt").reset_index(drop=True)
            return df
        except Exception as exc:
            logger.warning(f"Error reading {path_to_read}: {exc}")
    return None


def _get_available_sessions() -> List[str]:
    """List of all available dates in candles_5m (first) and candles_futures (fallback), sorted ascending."""
    dates = set()
    for d in (CANDLES_5M_DIR, CANDLES_FUTURES_DIR):
        if d.exists():
            for p in d.glob("*.csv"):
                s = p.stem[:10]
                if len(s) == 10 and s[4] == "-" and s[7] == "-":
                    dates.add(s)
            for p in d.glob("*.parquet"):
                s = p.stem[:10]
                if len(s) == 10 and s[4] == "-" and s[7] == "-":
                    dates.add(s)
    return sorted(dates)


def is_expiry_session(date_val: Union[date, datetime, str], cfg: Optional[Dict[str, Any]] = None) -> bool:
    """
    Check if a session date is an expiry session to be filtered from baseline.
    Checks:
      1. Explicitly excluded dates (e.g. '2026-09-22')
      2. Configured expiry weekdays (e.g. [1, 3] for Tue/Thu)
      3. Snapshot metadata if present naming target_date as expiry.
    """
    c = cfg or load_rvol_config()
    d_str = _parse_date(date_val)
    if d_str in c.get("excluded_dates", []):
        return True

    try:
        dt = datetime.strptime(d_str, "%Y-%m-%d").date()
    except Exception:
        return False

    exp_weekdays = c.get("expiry_weekdays", [1, 3])
    if dt.weekday() in exp_weekdays:
        return True

    # Check snapshot meta
    snap_root = REPO_ROOT / "data" / "snapshots" / d_str
    if snap_root.is_dir():
        meta_files = sorted(snap_root.glob("*.meta.json"))
        if meta_files:
            try:
                m_data = json.loads(meta_files[-1].read_text(encoding="utf-8"))
                exp_s = str(m_data.get("expiry", "")).strip()
                for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
                    try:
                        if datetime.strptime(exp_s, fmt).date() == dt:
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
    return False


def get_baseline_dates(
    target_date: Union[date, datetime, str],
    baseline_sessions: Optional[int] = None,
    filter_expiries: Optional[bool] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Retrieve list of prior session date strings for baseline calculation.
    If filter_expiries is True, skips expiry sessions.
    """
    c = cfg or load_rvol_config()
    n = baseline_sessions if baseline_sessions is not None else int(c.get("baseline_sessions", 10))
    do_filter = filter_expiries if filter_expiries is not None else bool(c.get("filter_expiry_sessions", False))

    target_date_str = _parse_date(target_date)
    all_sessions = _get_available_sessions()
    prior = [d for d in all_sessions if d < target_date_str]

    if not do_filter:
        return prior[-n:]

    selected: List[str] = []
    for d in reversed(prior):
        if is_expiry_session(d, c):
            continue
        selected.append(d)
        if len(selected) == n:
            break
    return sorted(selected)


def compute_rvol(
    target_date: Union[date, datetime, str],
    target_time: Union[dtime, datetime, str],
    filter_expiries: Optional[bool] = None,
) -> Optional[float]:
    """
    Compute time-of-day normalized cumulative RVOL for target_date up to target_time.

    Returns:
      float ratio (e.g. 0.81), or None if fewer than min_baseline_sessions exist.
    """
    cfg = load_rvol_config()
    min_sessions = int(cfg.get("min_baseline_sessions", 5))

    target_date_str = _parse_date(target_date)
    target_time_val = _parse_time(target_time)

    baseline_dates = get_baseline_dates(target_date_str, filter_expiries=filter_expiries, cfg=cfg)
    if len(baseline_dates) < min_sessions:
        return None

    today_df = _load_session_df(target_date_str)
    if today_df is None or today_df.empty:
        return None

    today_bars = today_df[today_df["time_val"] <= target_time_val]
    if today_bars.empty:
        return None

    today_cum_vol = float(today_bars["volume"].sum())

    baseline_vols: List[float] = []
    for d in baseline_dates:
        b_df = _load_session_df(d)
        if b_df is None or b_df.empty:
            continue
        b_bars = b_df[b_df["time_val"] <= target_time_val]
        if not b_bars.empty:
            vol = float(b_bars["volume"].sum())
            if vol > 0:
                baseline_vols.append(vol)

    if len(baseline_vols) < min_sessions:
        return None

    avg_base_vol = float(np.mean(baseline_vols))
    if avg_base_vol <= 0:
        return None

    return round(today_cum_vol / avg_base_vol, 2)


def compute_bar_rvol(
    target_date: Union[date, datetime, str],
    target_time: Union[dtime, datetime, str],
    filter_expiries: Optional[bool] = None,
) -> Optional[float]:
    """
    Compute per-bar Relative Volume (single 5m bar volume vs same time-of-day mean across baseline sessions).

    Returns:
      float ratio (e.g. 1.25), or None if fewer than min_baseline_sessions exist.
    """
    cfg = load_rvol_config()
    min_sessions = int(cfg.get("min_baseline_sessions", 5))

    target_date_str = _parse_date(target_date)
    target_time_val = _parse_time(target_time)

    baseline_dates = get_baseline_dates(target_date_str, filter_expiries=filter_expiries, cfg=cfg)
    if len(baseline_dates) < min_sessions:
        return None

    today_df = _load_session_df(target_date_str)
    if today_df is None or today_df.empty:
        return None

    today_bar = today_df[today_df["time_val"] == target_time_val]
    if today_bar.empty:
        return None

    today_bar_vol = float(today_bar["volume"].iloc[0])

    baseline_vols: List[float] = []
    for d in baseline_dates:
        b_df = _load_session_df(d)
        if b_df is None or b_df.empty:
            continue
        b_bar = b_df[b_df["time_val"] == target_time_val]
        if not b_bar.empty:
            vol = float(b_bar["volume"].iloc[0])
            if vol > 0:
                baseline_vols.append(vol)

    if len(baseline_vols) < min_sessions:
        return None

    avg_bar_vol = float(np.mean(baseline_vols))
    if avg_bar_vol <= 0:
        return None

    return round(today_bar_vol / avg_bar_vol, 2)


def get_rvol_regime(rvol_value: Optional[float]) -> str:
    """
    Determine regime from RVOL value using strategy1_rules.json thresholds.
    Returns: "DEAD_ZONE" | "TRAP_ZONE" | "TREND_ZONE" | "EXTREME_ZONE" | "NA"
    """
    if rvol_value is None:
        return "NA"

    cfg = load_rvol_config()
    dead_zone = float(cfg.get("dead_zone", 0.85))
    trend_zone = float(cfg.get("trend_zone", 1.30))
    extreme_zone = float(cfg.get("extreme_zone", 2.50))

    if rvol_value < dead_zone:
        return "DEAD_ZONE"
    if rvol_value < trend_zone:
        return "TRAP_ZONE"
    if rvol_value <= extreme_zone:
        return "TREND_ZONE"
    return "EXTREME_ZONE"


def get_rvol_display(
    target_date: Union[date, datetime, str],
    target_time: Union[dtime, datetime, str],
) -> Dict[str, Any]:
    """
    Format RVOL for display and alerts, computing both per-bar and cumulative RVOL.

    Returns:
    {
      "value": float | None,
      "rvol_cum": float | None,
      "rvol_bar": float | None,
      "regime": "DEAD_ZONE" | "TRAP_ZONE" | "TREND_ZONE" | "EXTREME_ZONE" | "NA",
      "label": str,
      "telegram_line": str,
      "suppress_signals": bool,
      "trend_day": bool,
      "caution": bool,
    }
    """
    cfg = load_rvol_config()
    primary_metric = cfg.get("primary_metric", "cumulative")
    rvol_cum = compute_rvol(target_date, target_time)
    rvol_bar = compute_bar_rvol(target_date, target_time)

    val = rvol_bar if primary_metric == "bar" else rvol_cum
    regime = get_rvol_regime(val)

    cum_s = f"{rvol_cum:.2f}×" if rvol_cum is not None else "N/A"
    bar_s = f"{rvol_bar:.2f}×" if rvol_bar is not None else "N/A"

    if regime == "DEAD_ZONE":
        label = f"{val:.2f}× (futures vol) → ⚠️ DEAD ZONE (bar {bar_s} | cum {cum_s})"
        telegram_line = f"📊 RVOL: {bar_s} (bar) | {cum_s} (cum) (futures vol) — DEAD ZONE (no trade)"
        suppress_signals = True
        trend_day = False
        caution = False
    elif regime == "TRAP_ZONE":
        label = f"{val:.2f}× (futures vol) → ⚠️ TRAP ZONE (bar {bar_s} | cum {cum_s})"
        telegram_line = f"📊 RVOL: {bar_s} (bar) | {cum_s} (cum) (futures vol) — TRAP ZONE (looks active, still no trade)"
        suppress_signals = True
        trend_day = False
        caution = False
    elif regime == "TREND_ZONE":
        label = f"{val:.2f}× (futures vol) → 🚀 TREND ZONE (bar {bar_s} | cum {cum_s})"
        telegram_line = f"📊 RVOL: {bar_s} (bar) | {cum_s} (cum) (futures vol) — TREND ZONE ✅ (trade with system)"
        suppress_signals = False
        trend_day = True
        caution = False
    elif regime == "EXTREME_ZONE":
        label = f"{val:.2f}× (futures vol) → ⚡ EXTREME ZONE (bar {bar_s} | cum {cum_s})"
        telegram_line = f"📊 RVOL: {bar_s} (bar) | {cum_s} (cum) (futures vol) — EXTREME ZONE ⚠️ (trend but watch reversal)"
        suppress_signals = False
        trend_day = True
        caution = True
    else:  # NA
        label = "RVOL: N/A (futures vol)"
        telegram_line = "📊 RVOL: N/A (futures vol)"
        suppress_signals = False
        trend_day = False
        caution = False

    return {
        "value": val,
        "rvol_cum": rvol_cum,
        "rvol_bar": rvol_bar,
        "regime": regime,
        "label": label,
        "telegram_line": telegram_line,
        "suppress_signals": suppress_signals,
        "trend_day": trend_day,
        "caution": caution,
    }


def get_gate_decision(
    target_date: Union[date, datetime, str],
    signal_time: Union[dtime, datetime, str],
) -> Dict[str, Any]:
    """
    Evaluate RVOL Gate decision for a signal at signal_time.

    Rules:
      - If signal_time < 09:30 → status: "HOLD"
      - If RVOL < 1.30 (DEAD ZONE < 0.85 or TRAP ZONE 0.85-1.30) → status: "SUPPRESSED"
      - If RVOL 1.30-2.50 (TREND ZONE) → status: "ALLOWED"
      - If RVOL > 2.50 (EXTREME ZONE) → status: "ALLOWED" (with caution note)

    Returns:
      {"status": "HOLD"|"SUPPRESSED"|"ALLOWED", "rvol_at_gate": float|None, "regime": str, "reason": str}
    """
    cfg = load_rvol_config()
    gate_start_str = str(cfg.get("gate_start", "09:30"))
    try:
        gate_start_t = datetime.strptime(gate_start_str, "%H:%M").time()
    except Exception:
        gate_start_t = dtime(9, 30)

    sig_time_val = _parse_time(signal_time)

    # 1. Before gate start (09:30)
    if sig_time_val < gate_start_t:
        return {
            "status": "HOLD",
            "rvol_at_gate": None,
            "regime": "HOLD",
            "reason": f"Gate starts at {gate_start_str} — first RVOL reading not yet available",
        }

    # 2. Compute RVOL at signal time
    primary_metric = cfg.get("primary_metric", "cumulative")
    rvol = compute_bar_rvol(target_date, sig_time_val) if primary_metric == "bar" else compute_rvol(target_date, sig_time_val)
    regime = get_rvol_regime(rvol)
    dead_thresh = float(cfg.get("dead_zone", 0.85))
    trend_thresh = float(cfg.get("trend_zone", 1.30))
    extreme_thresh = float(cfg.get("extreme_zone", 2.50))

    if regime == "DEAD_ZONE" or (rvol is not None and rvol < dead_thresh):
        return {
            "status": "SUPPRESSED",
            "rvol_at_gate": rvol,
            "regime": "DEAD_ZONE",
            "reason": f"⚠️ DEAD ZONE (RVOL {rvol:.2f}× < {dead_thresh}) — no trade. Volume dead, wait for expansion.",
        }

    if regime == "TRAP_ZONE" or (rvol is not None and rvol < trend_thresh):
        return {
            "status": "SUPPRESSED",
            "rvol_at_gate": rvol,
            "regime": "TRAP_ZONE",
            "reason": f"⚠️ TRAP ZONE (RVOL {rvol:.2f}× in {dead_thresh}-{trend_thresh}) — no trade. False breakouts common.",
        }

    if regime == "EXTREME_ZONE" or (rvol is not None and rvol > extreme_thresh):
        return {
            "status": "ALLOWED",
            "rvol_at_gate": rvol,
            "regime": "EXTREME_ZONE",
            "reason": f"⚡ EXTREME ZONE (RVOL {rvol:.2f}× > {extreme_thresh}) — trend active but caution: watch for reversal.",
        }

    # Allowed if >= 1.30 or if baseline data is NA (< 5 sessions)
    return {
        "status": "ALLOWED",
        "rvol_at_gate": rvol,
        "regime": regime,
        "reason": "✅ TREND ZONE (RVOL >= 1.30) — trade with system" if rvol is not None else "Baseline sessions < 5 (N/A)",
    }


def find_regime_flip(target_date: Union[date, datetime, str]) -> Optional[Dict[str, Any]]:
    """
    Scan intraday 5m buckets on target_date to find if and when regime flipped
    from DEAD/TRAP zone to TREND_ZONE/EXTREME_ZONE.
    """
    target_date_str = _parse_date(target_date)
    today_df = _load_session_df(target_date_str)
    if today_df is None or today_df.empty:
        return None

    times = [t for t in today_df["time_val"] if t >= dtime(9, 30)]
    if not times:
        return None

    in_suppressed = False
    flip_info = None

    for t_val in times:
        val = compute_rvol(target_date_str, t_val)
        reg = get_rvol_regime(val)
        if reg in ("DEAD_ZONE", "TRAP_ZONE", "CHOP_ZONE"):
            in_suppressed = True
        elif in_suppressed and reg in ("TREND_ZONE", "EXTREME_ZONE", "NORMAL", "TREND_DAY"):
            flip_info = {
                "date": target_date_str,
                "flip_time": t_val.strftime("%H:%M"),
                "from": "CHOP/TRAP",
                "to": reg,
                "rvol_at_flip": val,
            }
            break

    return flip_info


DEFAULT_GATE_LOG_COMMENT = (
    "Per-poll counts (60s), i.e. signal-minutes suppressed, not distinct trade opportunities "
    "— e.g. 257 suppressed means ~257 polls where a BUY was blocked, not 257 setups."
)


def log_session_gate_decision(
    target_date: Union[date, datetime, str],
    signals_suppressed: Optional[int] = 0,
    signals_allowed: Optional[int] = 0,
    counters_backfilled: Optional[bool] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Write or append EOD session summary to data/rvol_gate_log.jsonl.
    """
    target_date_str = _parse_date(target_date)
    r_0930 = compute_rvol(target_date_str, dtime(9, 30))
    r_1000 = compute_rvol(target_date_str, dtime(10, 0))
    reg_open = get_rvol_regime(r_1000 if r_1000 is not None else r_0930)

    flip = find_regime_flip(target_date_str)

    entry = {
        "date": target_date_str,
        "rvol_at_0930": r_0930,
        "rvol_at_1000": r_1000,
        "regime_open": reg_open,
        "flip_time": flip["flip_time"] if flip else None,
        "flip_to": flip["to"] if flip else None,
        "signals_suppressed": int(signals_suppressed) if signals_suppressed is not None else None,
        "signals_allowed": int(signals_allowed) if signals_allowed is not None else None,
        "counters_backfilled": bool(counters_backfilled) if counters_backfilled is not None else False,
        "comment": comment if comment is not None else DEFAULT_GATE_LOG_COMMENT,
    }

    GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    found = False
    if GATE_LOG_PATH.exists():
        for line in GATE_LOG_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if obj.get("date") == target_date_str:
                    entries.append(entry)
                    found = True
                else:
                    entries.append(obj)
            except Exception:
                continue
    if not found:
        entries.append(entry)

    temp_path = GATE_LOG_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    temp_path.replace(GATE_LOG_PATH)

    return entry
