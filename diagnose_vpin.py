"""
diagnose_vpin.py — Diagnostic Script for VPIN Input Frames (Stage 0)
===================================================================

Per Stage 0 specification:
Inspects the exact frame(s) that feed vpin.py / vpin_tab.py without modifying anything:
1. df[['timestamp','close','volume']].head(40) and .tail(40)
2. df['volume'].is_monotonic_increasing — True indicates feed is cumulative
3. df['close'].nunique() vs len(df) — sparsity / staleness check
4. df['timestamp'].duplicated().sum() and count of consecutive duplicate rows
5. df['close'].diff().std() — rolling σ that BVC divides by
6. Total session volume vs bucket_volume, and implied bucket count
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

import config


def load_bucket_volume_config() -> float:
    """Load configured bucket_volume from strategy1_rules.json or default to 50000.0."""
    p = Path(__file__).parent / "strategy1_rules.json"
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            v = d.get("vpin", {}).get("bucket_volume")
            if v is not None and float(v) > 0:
                return float(v)
        except Exception:
            pass
    return 50000.0


def run_diagnostics(df: pd.DataFrame, source_label: str, bucket_volume: float = 50000.0) -> None:
    """Run the 6 Stage 0 diagnostic checks on a given DataFrame."""
    print("=" * 80)
    print(f"DIAGNOSTIC REPORT: {source_label}")
    print("=" * 80)

    if df is None or df.empty:
        print(f"DataFrame is EMPTY or None for {source_label}.\n")
        return

    # Normalize column names for diagnostics
    d = df.copy()
    col_map = {str(c).strip().lower(): c for c in d.columns}

    ts_col = col_map.get("timestamp") or col_map.get("date") or "timestamp"
    cl_col = col_map.get("close") or "close"
    vo_col = col_map.get("volume") or "volume"

    if cl_col not in d.columns or vo_col not in d.columns:
        print(f"Required columns missing. Columns present: {list(d.columns)}\n")
        return

    diag_cols = [ts_col, cl_col, vo_col] if ts_col in d.columns else [cl_col, vo_col]
    sub = d[diag_cols].copy()
    sub.columns = ["timestamp", "close", "volume"] if len(diag_cols) == 3 else ["close", "volume"]
    sub["close"] = pd.to_numeric(sub["close"], errors="coerce")
    sub["volume"] = pd.to_numeric(sub["volume"], errors="coerce").fillna(0.0)

    print(f"Total Rows: {len(sub)}")
    print("\n--- 1. HEAD(40) ---")
    print(sub.head(40).to_string())

    print("\n--- 1. TAIL(40) ---")
    print(sub.tail(40).to_string())

    # 2. Cumulative volume check
    is_monotonic = sub["volume"].is_monotonic_increasing
    print("\n--- 2. VOLUME MONOTONICITY ---")
    print(f"df['volume'].is_monotonic_increasing: {is_monotonic}")
    if is_monotonic:
        print("  -> FEED IS CUMULATIVE: volume is steadily increasing and must be differenced before bucketing!")
    else:
        print("  -> Feed is NOT strictly monotonic increasing.")

    # 3. Price sparsity / staleness
    unique_closes = sub["close"].nunique()
    total_rows = len(sub)
    close_ratio = unique_closes / total_rows if total_rows > 0 else 0.0
    print("\n--- 3. PRICE SPARSITY & STALENESS ---")
    print(f"Unique closes: {unique_closes} vs total rows: {total_rows} ({close_ratio:.2%})")
    if close_ratio < 0.20:
        print("  -> WARNING: Price series is very flat/stale (unique closes < 20% of rows).")
    else:
        print("  -> Price changes appear active.")

    # 4. Duplicate timestamps & consecutive duplicates
    dup_ts_count = sub["timestamp"].duplicated().sum() if "timestamp" in sub.columns else 0
    consecutive_dup_count = 0
    if "timestamp" in sub.columns:
        ts_series = sub["timestamp"]
        consecutive_dup_count = int((ts_series == ts_series.shift(1)).sum())

    print("\n--- 4. DUPLICATE ROWS ---")
    print(f"df['timestamp'].duplicated().sum(): {dup_ts_count}")
    print(f"Consecutive duplicate timestamp rows: {consecutive_dup_count}")

    # 5. Volatility σ
    diff_s = sub["close"].diff()
    sigma = diff_s.std()
    print("\n--- 5. PRICE CHANGE VOLATILITY (σ_Δ) ---")
    print(f"df['close'].diff().std(): {sigma:.6f}")
    if sigma is None or np.isnan(sigma) or sigma < 1e-6:
        print("  -> WARNING: σ is zero, near-zero, or NaN! BVC cannot divide by this without returning None.")

    # 6. Total volume vs bucket_volume & implied bucket count
    tot_vol = sub["volume"].sum()
    implied_buckets = tot_vol / bucket_volume if bucket_volume > 0 else 0.0
    print("\n--- 6. TOTAL VOLUME & IMPLIED BUCKET COUNT ---")
    print(f"Total session volume: {tot_vol:,.2f}")
    print(f"Bucket volume V:      {bucket_volume:,.2f}")
    print(f"Implied bucket count: {implied_buckets:,.1f} buckets")
    if implied_buckets > 500:
        print(f"  -> ABNORMAL BUCKET COUNT: {implied_buckets:,.0f} buckets implies massive volume feed (e.g. index volume or cumulative poll error)!")
    print("\n")


def main():
    print("=" * 80)
    print("STAGE 0: VPIN INPUT FRAME DIAGNOSTICS")
    print("=" * 80)

    bucket_vol = load_bucket_volume_config()
    print(f"Loaded bucket_volume configuration: {bucket_vol:,.0f}\n")

    # Source 1: candle_source.fetch() -> (idx_c, fut_c)
    # This is what dashboard.py fetches in fetch_price_action() and passes downstream
    print("Fetching from candle_source.fetch() (dashboard live/cached feed)...")
    try:
        import candle_source as cs
        idx_c, fut_c = cs.fetch()
        if fut_c is not None and not fut_c.empty:
            run_diagnostics(fut_c, "candle_source.fetch() -> fut_c (Futures Candle Feed)", bucket_vol)
        else:
            print("candle_source.fetch() returned None or empty for fut_c.\n")
        if idx_c is not None and not idx_c.empty:
            run_diagnostics(idx_c, "candle_source.fetch() -> idx_c (Index Candle Feed)", bucket_vol)
    except Exception as exc:
        print(f"Error calling candle_source.fetch(): {exc}\n")

    # Source 2: dhan_frames.fetch(mode="futures")
    print("Fetching from dhan_frames.fetch(interval='1', days_back=1, mode='futures')...")
    try:
        import dhan_frames
        raw_fut, info = dhan_frames.fetch(interval="1", days_back=1, mode="futures")
        if raw_fut is not None and not raw_fut.empty:
            run_diagnostics(raw_fut, "dhan_frames.fetch(mode='futures')", bucket_vol)
        else:
            print(f"dhan_frames returned empty. Info errors: {info.get('errors')}\n")
    except Exception as exc:
        print(f"Error calling dhan_frames.fetch(): {exc}\n")

    # Source 3: Latest offline session CSV from data/candles/
    candle_dir = Path(__file__).parent / "data" / "candles"
    csv_files = sorted(candle_dir.glob("2026-*.csv"), reverse=True)
    regular_files = [f for f in csv_files if not f.name.startswith("vol_")]
    if regular_files:
        latest_csv = regular_files[0]
        print(f"Reading latest stored session CSV: {latest_csv.name}...")
        try:
            stored_df = pd.read_csv(latest_csv)
            run_diagnostics(stored_df, f"Stored Session CSV ({latest_csv.name})", bucket_vol)
        except Exception as exc:
            print(f"Error reading {latest_csv}: {exc}\n")

    # Source 4: Cumulative volume file if present (vol_*.csv)
    vol_files = sorted(candle_dir.glob("vol_*.csv"), reverse=True)
    if vol_files:
        latest_vol = vol_files[0]
        print(f"Reading stored cumulative volume file: {latest_vol.name}...")
        try:
            vol_df = pd.read_csv(latest_vol)
            run_diagnostics(vol_df, f"Cumulative Volume File ({latest_vol.name})", bucket_vol)
        except Exception as exc:
            print(f"Error reading {latest_vol}: {exc}\n")


if __name__ == "__main__":
    main()
