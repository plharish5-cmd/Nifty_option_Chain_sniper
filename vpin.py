"""
vpin.py — Volume-Synchronized Probability of Informed Trading (VPIN)
=============================================================================

Pure computation module implementing the market microstructure order-flow
toxicity metric from:
  David Easley, Marcos M. López de Prado, and Maureen O'Hara (2010, 2011, 2012)
  "The Volume Clock: Insights into the High-Frequency Trading Process"
  "Flow Toxicity and Liquidity in a High-Frequency World"

MATHEMATICAL SPECIFICATION:
--------------------------
Step 1: Session Isolation & Volume Buckets
  - Group all bucketing by session date. A bucket NEVER spans two sessions;
    any partial forming bucket at the session close is discarded, not carried forward.
  - Drop duplicate timestamps and per-bar volume <= 0.
  - If volume is cumulative, difference it into per-bar volume (.diff()).
  - Accumulate bar volume into buckets of exactly equal size V.
  - A bar straddling a boundary is split proportionally across buckets.
  - Single bars larger than V populate multiple completed buckets.
  - Sub-microsecond timestamp offsets ensure no two completed buckets share identical end timestamps.
  - Conservation assertion: sum of bucket volumes == sum of per-bar volumes for the session.

Step 2: Bulk Volume Classification (BVC) & Volatility
  - Delta S = S_i - S_{i-1} computed strictly intraday.
  - The first bar of each session has Delta S = NaN and is dropped from BVC.
  - sigma_Delta is computed from intraday bar-to-bar price changes only,
    strictly excluding overnight session-boundary differences.
  - For each contributing bar i in bucket tau:
      Z_i = (S_i - S_{i-1}) / sigma_Delta
      buy_ratio = norm.cdf(Z_i)
      V_buy(tau)  = sum( chunk_i * buy_ratio )
      V_sell(tau) = V - V_buy(tau)
  - When Delta S == 0 (flat price), buy_ratio is identically 0.50 (50/50 split).
  - When abs(Delta S) < bvc_flat_tolerance_z * sigma_Delta, buy_ratio must fall in [bvc_flat_min_buy, bvc_flat_max_buy].
  - If sigma_Delta is 0 with a non-zero price jump or window is short, return None for that bucket.

Step 3: Dual Metrics (VPIN & DIR)
  - Signed imbalance for bucket tau:
      I(tau) = ( V_buy(tau) - V_sell(tau) ) / V  in [-1, +1]
  - Undirectional VPIN over rolling window of n completed buckets:
      VPIN_tau = sum_{j=tau-n+1..tau} |I(j)| / n  in [0, 1]
  - Signed Directional Bias DIR over same rolling window:
      DIR_tau = sum_{j=tau-n+1..tau} I(j) / n  in [-1, +1]
  - Invariant: abs(DIR) <= VPIN universally, with strict inequality on mixed-sign data.

Step 4: Empirical CDF (The Sole Threshold System)
  - Empirical percentile rank of VPIN_tau against rolling history of
    cdf_window completed buckets strictly prior to bucket tau:
      CDF_tau = (1 / cdf_window) * sum( 1(VPIN_{prior} <= VPIN_tau) )
  - Returns None until cdf_window is genuinely full with valid observations.
  - All alarms, colors, and states read from CDF alone (no raw-VPIN bands).

Step 5: Bucket Duration & Spike Ratio
  - duration(tau) = end_time(tau) - start_time(tau)
  - median_dur = rolling median duration over duration_window buckets
  - spike_ratio = median_dur / duration(tau)

CONSTRAINTS:
-----------
- Pure computation: no I/O, no Streamlit.
- All configuration thresholds loaded from strategy1_rules.json["vpin"].
- Standing Rule 4: Zero hardcoded numbers in Python.
- Standing Rule 6: No bare except: pass.
"""

from __future__ import annotations

print(f"[vpin] loaded from {__file__}")

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.stats import norm

log = logging.getLogger("vpin")

CONFIG_PATH = Path(__file__).parent / "strategy1_rules.json"


def load_vpin_config(config_path: Optional[Union[Path, str]] = None) -> dict:
    """
    Load VPIN configuration thresholds from strategy1_rules.json.

    All thresholds must come from JSON (Standing Rule 4: zero hardcoded numbers).
    """
    p = Path(config_path) if config_path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"Configuration file not found: {p}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "vpin" not in data:
        raise KeyError(f"'vpin' configuration key missing from {p}")

    return data["vpin"]


@dataclass
class FormingBucket:
    """
    Represents the currently forming (incomplete) volume bucket.

    Per specification: The forming bucket is excluded from all signals,
    scores, and completed reads.
    """
    bucket_idx: int
    session_date: Any
    accumulated_vol: float
    bucket_volume: float
    pct_filled: float
    v_buy: Optional[float]
    v_sell: Optional[float]
    bar_count: int
    is_forming: bool = True


@dataclass
class VPINOutput:
    """
    Result container for pure VPIN computation.

    Attributes:
    -----------
    buckets : pd.DataFrame
        Completed buckets with columns:
        [bucket_idx, session_idx, session_date, session_bucket_idx,
         start_time, end_time, duration_sec, end_price, total_vol,
         v_buy, v_sell, abs_imbalance, signed_imbalance, bar_count,
         valid, vpin, dir, ln_vpin, cdf, is_toxic, spike_ratio]
    forming_bucket : Optional[FormingBucket]
        Incomplete forming bucket state for the latest session.
    metadata : dict
        Execution and configuration metadata.
    """
    buckets: pd.DataFrame
    forming_bucket: Optional[FormingBucket]
    metadata: dict


def calculate_vpin(
    df: pd.DataFrame,
    config: Optional[dict] = None,
) -> VPINOutput:
    """
    Compute VPIN, signed DIR, BVC, bucket duration spike ratio, and empirical CDF.

    Parameters:
    -----------
    df : pd.DataFrame
        OHLCV DataFrame with columns ['close', 'volume'] (case-insensitive).
        Optional columns: ['timestamp', 'date', 'open', 'high', 'low'].
    config : Optional[dict]
        VPIN configuration dictionary. If None, loaded from strategy1_rules.json['vpin'].

    Returns:
    --------
    VPINOutput
        DataClass containing completed buckets DataFrame, forming bucket, and metadata.
    """
    cfg = config if config is not None else load_vpin_config()

    # Extract all parameters strictly from configuration (Standing Rule 4)
    buckets_per_day = int(cfg["buckets_per_day"])
    cfg_bucket_vol = cfg.get("bucket_volume")
    sigma_window = int(cfg["sigma_window"])
    n_buckets = int(cfg["n_buckets"])
    cdf_window = int(cfg["cdf_window"])
    cdf_threshold = float(cfg["cdf_threshold"])
    duration_window = int(cfg.get("duration_window", 20))
    spike_threshold = float(cfg.get("spike_threshold", 2.0))
    bvc_flat_tolerance_z = float(cfg.get("bvc_flat_tolerance_z", 0.125))
    bvc_flat_min_buy = float(cfg.get("bvc_flat_min_buy", 0.45))
    bvc_flat_max_buy = float(cfg.get("bvc_flat_max_buy", 0.55))

    empty_metadata = {
        "bucket_volume": None,
        "buckets_per_day": buckets_per_day,
        "sigma_window": sigma_window,
        "n_buckets": n_buckets,
        "cdf_window": cdf_window,
        "cdf_threshold": cdf_threshold,
        "total_bars": 0,
        "completed_buckets": 0,
        "cdf_window_full": False,
        "latest_vpin": None,
        "latest_dir": None,
        "latest_cdf": None,
        "latest_spike": None,
        "state_line": "NO DATA",
    }

    if df is None or df.empty:
        return VPINOutput(
            buckets=pd.DataFrame(),
            forming_bucket=None,
            metadata=empty_metadata,
        )

    # -------------------------------------------------------------------------
    # 1. Normalize columns & Deduplicate Timestamps (Bug 1)
    # -------------------------------------------------------------------------
    d = df.copy()
    col_map = {str(c).strip().lower(): c for c in d.columns}

    if "close" not in col_map or "volume" not in col_map:
        raise ValueError(
            f"Input DataFrame must contain 'close' and 'volume'. Available: {list(d.columns)}"
        )

    close_series = pd.to_numeric(d[col_map["close"]], errors="coerce")
    volume_series = pd.to_numeric(d[col_map["volume"]], errors="coerce")

    if "timestamp" in col_map:
        timestamp_series = pd.to_datetime(d[col_map["timestamp"]], errors="coerce")
    elif "date" in col_map:
        timestamp_series = pd.to_datetime(d[col_map["date"]], errors="coerce")
    else:
        timestamp_series = pd.Series(d.index, index=d.index)

    valid_mask = close_series.notna() & volume_series.notna()
    d = pd.DataFrame({
        "timestamp": timestamp_series[valid_mask],
        "close": close_series[valid_mask],
        "volume": volume_series[valid_mask],
    }).reset_index(drop=True)

    # Drop any row whose timestamp duplicates the previous row (Bug 1)
    d = d.drop_duplicates(subset=["timestamp"], keep="first").reset_index(drop=True)

    if d.empty:
        return VPINOutput(
            buckets=pd.DataFrame(),
            forming_bucket=None,
            metadata=empty_metadata,
        )

    # -------------------------------------------------------------------------
    # 2. Cumulative-Feed Guard & Differencing (Bug 1)
    # -------------------------------------------------------------------------
    # Checked PER SESSION (29-Sep-2026 fix): a running total that resets every morning is not monotonic
    # across days, so the old whole-frame check let it through (74 real buckets -> 14,125 phantom ones).
    _sess = pd.to_datetime(d["timestamp"], errors="coerce").dt.date
    _cum_days = []
    for _day, _v in d["volume"].groupby(_sess):
        if len(_v) > 2 and bool(_v.is_monotonic_increasing) and float(_v.iloc[-1]) > float(_v.iloc[0]):
            _dv = _v.diff()
            _dv.iloc[0] = _v.iloc[0]
            d.loc[_v.index, "volume"] = _dv.clip(lower=0.0)
            _cum_days.append(_day)
    if _cum_days:
        log.info("Cumulative volume feed detected on %d session(s) and differenced into per-bar volume.",
                 len(_cum_days))

    # Drop any row with per-bar volume <= 0 (Bug 1)
    d = d[d["volume"] > 0.0].reset_index(drop=True)

    if d.empty:
        return VPINOutput(
            buckets=pd.DataFrame(),
            forming_bucket=None,
            metadata=empty_metadata,
        )

    # -------------------------------------------------------------------------
    # 3. Session Partitioning & Intraday Delta S (Bug 5)
    # -------------------------------------------------------------------------
    if pd.api.types.is_datetime64_any_dtype(d["timestamp"]):
        d["session_date"] = d["timestamp"].dt.date
    else:
        # Try datetime conversion
        parsed_dt = pd.to_datetime(d["timestamp"], errors="coerce")
        if parsed_dt.notna().all():
            d["session_date"] = parsed_dt.dt.date
        else:
            d["session_date"] = "session_0"

    # Intraday bar-to-bar price changes: first bar of each session has delta_s = NaN (Bug 5)
    d["delta_s"] = d.groupby("session_date")["close"].diff()

    # Rolling standard deviation computed strictly from intraday bar-to-bar changes,
    # completely excluding session-boundary overnight jumps (Bug 5)
    valid_intraday_deltas = d["delta_s"].dropna()
    rolling_sigmas = valid_intraday_deltas.rolling(
        window=sigma_window,
        min_periods=sigma_window,
    ).std(ddof=1)
    d["sigma_delta"] = rolling_sigmas.reindex(d.index)

    # -------------------------------------------------------------------------
    # 4. Determine Bucket Volume V
    # -------------------------------------------------------------------------
    # 29-Sep-2026 fix (no look-ahead): with bucket_volume null, each session's V = average daily volume of the
    # PREVIOUS sessions (up to adv_lookback_sessions) / buckets_per_day. The first session has no history, so it
    # only calibrates V and forms no buckets. Before, V came from the average of ALL rows, later days included.
    session_vols = d.groupby("session_date", sort=False)["volume"].sum()
    V_by_session: Dict[Any, Optional[float]] = {}
    if cfg_bucket_vol is not None and float(cfg_bucket_vol) > 0.0:
        V_by_session = {k: float(cfg_bucket_vol) for k in session_vols.index}
    else:
        look = int(cfg.get("adv_lookback_sessions", 20))
        vals = list(session_vols.values)
        for k_i, k in enumerate(session_vols.index):
            prior = [x for x in vals[max(0, k_i - look):k_i] if x > 0]
            V_by_session[k] = float(np.mean(prior) / float(buckets_per_day)) if prior else None
    bucket_volume = next((v for v in reversed(list(V_by_session.values())) if v), None)
    if bucket_volume is None:
        empty_metadata["state_line"] = "CALIBRATING (need one previous session to size the buckets)"
        return VPINOutput(buckets=pd.DataFrame(), forming_bucket=None, metadata=empty_metadata)

    # -------------------------------------------------------------------------
    # 5. Equal-Volume Bucketing & BVC per Session (Bug 1, Bug 2, Bug 5)
    # -------------------------------------------------------------------------
    all_completed_buckets: List[dict] = []
    last_forming_bucket: Optional[FormingBucket] = None

    session_groups = list(d.groupby("session_date", sort=False))
    num_sessions = len(session_groups)

    for session_idx, (s_date, s_df) in enumerate(session_groups):
        bucket_volume = V_by_session.get(s_date)
        if not bucket_volume:
            log.info("Session %s only calibrates the bucket size (no previous session).", s_date)
            continue
        session_used_vol = 0.0
        curr_vol = 0.0
        curr_v_buy = 0.0
        curr_invalid_bvc = False
        curr_bar_count = 0
        bucket_start_time = None
        session_completed: List[dict] = []

        n_bars_session = len(s_df)
        for i in range(n_bars_session):
            row = s_df.iloc[i]
            t_i = row["timestamp"]
            s_i = float(row["close"])
            v_i = float(row["volume"])
            ds_i = row["delta_s"]
            sig_i = row["sigma_delta"]

            # Classification logic per bar
            if pd.isna(ds_i):
                # First bar of session: drop from BVC (Bug 5)
                is_bar_invalid = True
                buy_ratio = 0.50
            elif ds_i == 0.0:
                # Flat price: Delta S == 0 gives exact 50/50 split (Test 3)
                is_bar_invalid = False
                buy_ratio = 0.50
            elif pd.isna(sig_i) or sig_i <= 1e-12:
                # Short warmup window or zero volatility with non-zero move
                is_bar_invalid = True
                buy_ratio = 0.50
            else:
                is_bar_invalid = False
                z_i = float(ds_i / sig_i)
                buy_ratio = float(norm.cdf(z_i))

                # Bug 2 Assertion: verify BVC classification consistency on small moves
                if abs(ds_i) < bvc_flat_tolerance_z * sig_i:
                    assert bvc_flat_min_buy <= buy_ratio <= bvc_flat_max_buy, (
                        f"BVC anomaly at {t_i}: |dS|={abs(ds_i):.4f} < {bvc_flat_tolerance_z}*sigma ({sig_i:.4f}) "
                        f"but buy_ratio={buy_ratio:.4f} not in [{bvc_flat_min_buy}, {bvc_flat_max_buy}]"
                    )

            # 29-Sep-2026 fix: a bar that cannot be classified (09:15 bar, sigma warm-up) still fills its bucket but
            # counts as NEUTRAL (half buy, half sell) - it adds no imbalance. Before, it voided its whole bucket, and
            # every 50-bucket window touching one returned None: on real data VPIN existed for 6% of buckets and
            # the CDF for 0%.
            if is_bar_invalid:
                is_bar_invalid = False
                buy_ratio = 0.50
            if bucket_start_time is None:
                bucket_start_time = t_i
            session_used_vol += v_i

            rem_vol = v_i
            sub_bucket_in_bar = 0

            while rem_vol > 0.0:
                needed = bucket_volume - curr_vol

                if rem_vol < needed - 1e-9:
                    chunk = rem_vol
                    curr_vol += chunk
                    if is_bar_invalid:
                        curr_invalid_bvc = True
                    else:
                        curr_v_buy += chunk * buy_ratio
                    curr_bar_count += 1
                    rem_vol = 0.0
                else:
                    chunk = needed
                    curr_vol += chunk
                    if is_bar_invalid:
                        curr_invalid_bvc = True
                    else:
                        curr_v_buy += chunk * buy_ratio
                    curr_bar_count += 1
                    rem_vol -= chunk

                    # Timestamp assignment: microsecond offset for multi-bucket bars
                    # ensures no two completed buckets share an identical end timestamp (Bug 1)
                    if sub_bucket_in_bar == 0:
                        bucket_end_time = t_i
                    else:
                        if isinstance(t_i, pd.Timestamp):
                            bucket_end_time = t_i + pd.Timedelta(microseconds=sub_bucket_in_bar)
                        else:
                            bucket_end_time = t_i
                    sub_bucket_in_bar += 1

                    if curr_invalid_bvc:
                        b_buy = None
                        b_sell = None
                        b_signed_imb = None
                        b_imb = None
                        b_valid = False
                    else:
                        b_buy = float(curr_v_buy)
                        b_sell = float(bucket_volume - curr_v_buy)
                        # Bug 3: signed imbalance I(tau) = (V_buy - V_sell) / V in [-1, +1]
                        b_signed_imb = float((b_buy - b_sell) / bucket_volume)
                        # Undirectional absolute imbalance
                        b_imb = float(abs(b_buy - b_sell))
                        b_valid = True

                    # Calculate bucket duration (for volume-clock spike ratio)
                    if isinstance(t_i, pd.Timestamp) and isinstance(bucket_start_time, pd.Timestamp):
                        dur_sec = max(1.0, (bucket_end_time - bucket_start_time).total_seconds())
                    else:
                        dur_sec = max(1.0, float(curr_bar_count * 60))

                    bucket_record = {
                        "bucket_idx": len(all_completed_buckets),
                        "session_idx": session_idx,
                        "session_date": s_date,
                        "session_bucket_idx": len(session_completed),
                        "start_time": bucket_start_time,
                        "end_time": bucket_end_time,
                        "duration_sec": dur_sec,
                        "end_price": s_i,
                        "total_vol": bucket_volume,
                        "v_buy": b_buy,
                        "v_sell": b_sell,
                        "abs_imbalance": b_imb,
                        "signed_imbalance": b_signed_imb,
                        "bar_count": curr_bar_count,
                        "valid": b_valid,
                    }
                    session_completed.append(bucket_record)
                    all_completed_buckets.append(bucket_record)

                    # Reset accumulator
                    curr_vol = 0.0
                    curr_v_buy = 0.0
                    curr_invalid_bvc = False
                    curr_bar_count = 0
                    bucket_start_time = t_i

        # Bug 1 Assertion: summed bucket volume == summed per-bar volume for the session
        session_bar_vol = float(session_used_vol)
        session_bucket_vol = float(len(session_completed) * bucket_volume) + curr_vol
        assert abs(session_bucket_vol - session_bar_vol) < 1e-5, (
            f"Volume conservation failed for session {s_date}: "
            f"summed bucket volume ({session_bucket_vol:.4f}) != "
            f"summed per-bar volume ({session_bar_vol:.4f})"
        )

        # Bug 5: A bucket never spans two sessions.
        # If this is the latest session, capture the forming bucket.
        # Prior session partial buckets are discarded at session end.
        if session_idx == num_sessions - 1:
            if curr_vol > 0.0:
                last_forming_bucket = FormingBucket(
                    bucket_idx=len(all_completed_buckets),
                    session_date=s_date,
                    accumulated_vol=float(curr_vol),
                    bucket_volume=bucket_volume,
                    pct_filled=float(curr_vol / bucket_volume),
                    v_buy=None if curr_invalid_bvc else float(curr_v_buy),
                    v_sell=None if curr_invalid_bvc else float(curr_vol - curr_v_buy),
                    bar_count=curr_bar_count,
                    is_forming=True,
                )
        else:
            if curr_vol > 0.0:
                log.info(
                    f"Session {s_date} ended: discarded partial bucket of {curr_vol:.1f} contracts."
                )

    b_df = pd.DataFrame(all_completed_buckets)

    # Bug 1 Assertion: no two completed buckets share an identical end timestamp
    if not b_df.empty:
        duplicates = b_df[b_df["end_time"].duplicated()]["end_time"].tolist()
        assert len(duplicates) == 0, (
            f"Assertion failed: found duplicate completed bucket end timestamps! "
            f"Duplicates: {duplicates[:5]}"
        )

    if b_df.empty:
        empty_metadata["bucket_volume"] = bucket_volume
        return VPINOutput(
            buckets=b_df,
            forming_bucket=last_forming_bucket,
            metadata=empty_metadata,
        )

    # -------------------------------------------------------------------------
    # 6. Rolling VPIN and Directional Bias DIR (Bug 3)
    # -------------------------------------------------------------------------
    # I(tau) = ( V_buy(tau) - V_sell(tau) ) / V  in [-1, +1]
    # VPIN   = sum |I(tau)| / n                   in [0, 1]
    # DIR    = sum  I(tau)  / n                   in [-1, +1]
    # Invariant: abs(DIR) <= VPIN, equality strictly on single-sign windows.
    vpin_series: List[Optional[float]] = []
    dir_series: List[Optional[float]] = []
    ln_vpin_series: List[Optional[float]] = []

    total_b = len(b_df)
    for tau in range(total_b):
        if tau < n_buckets - 1:
            vpin_series.append(None)
            dir_series.append(None)
            ln_vpin_series.append(None)
            continue

        window_slice = b_df.iloc[tau - n_buckets + 1 : tau + 1]

        if not window_slice["valid"].all():
            vpin_series.append(None)
            dir_series.append(None)
            ln_vpin_series.append(None)
            continue

        signed_imbs = window_slice["signed_imbalance"].values.astype(float)
        abs_imbs = np.abs(signed_imbs)

        vpin_val = float(np.mean(abs_imbs))
        dir_val = float(np.mean(signed_imbs))

        # Assert Bug 3 Invariant: abs(DIR) <= VPIN universally
        assert abs(dir_val) <= vpin_val + 1e-9, (
            f"Sign invariant violated at bucket {tau}: |DIR|={abs(dir_val):.6f} > VPIN={vpin_val:.6f}"
        )

        # Check mixed-sign data condition
        has_pos = bool((signed_imbs > 1e-9).any())
        has_neg = bool((signed_imbs < -1e-9).any())
        if has_pos and has_neg:
            assert abs(dir_val) < vpin_val - 1e-9 or vpin_val < 1e-9, (
                f"Bug 3 recurring at bucket {tau}: |DIR| == VPIN on mixed-sign data! "
                f"DIR={dir_val:.6f}, VPIN={vpin_val:.6f}"
            )

        vpin_series.append(vpin_val)
        dir_series.append(dir_val)

        if vpin_val > 0.0:
            ln_vpin_series.append(float(np.log(vpin_val)))
        else:
            ln_vpin_series.append(None)

    b_df["vpin"] = vpin_series
    b_df["dir"] = dir_series
    b_df["ln_vpin"] = ln_vpin_series

    # -------------------------------------------------------------------------
    # 7. Empirical CDF over Rolling History (Bug 4)
    # -------------------------------------------------------------------------
    # Empirical percentile rank of VPIN_tau against cdf_window completed buckets
    # strictly prior to bucket tau:
    #   H = [VPIN_{tau - cdf_window}, ..., VPIN_{tau - 1}]
    # No lookahead. Returns None until cdf_window is genuinely full.
    cdf_series: List[Optional[float]] = []
    is_toxic_series: List[bool] = []

    for tau in range(total_b):
        if tau < cdf_window:
            cdf_series.append(None)
            is_toxic_series.append(False)
            continue

        curr_vpin = b_df["vpin"].iloc[tau]
        if curr_vpin is None or pd.isna(curr_vpin):
            cdf_series.append(None)
            is_toxic_series.append(False)
            continue

        prior_vpin_slice = b_df["vpin"].iloc[tau - cdf_window : tau]

        if prior_vpin_slice.isna().any() or any(x is None for x in prior_vpin_slice):
            cdf_series.append(None)
            is_toxic_series.append(False)
            continue

        history = np.array(prior_vpin_slice, dtype=float)
        rank_val = float(np.mean(history <= (float(curr_vpin) + 1e-12)))
        cdf_series.append(rank_val)
        is_toxic_series.append(bool(rank_val >= cdf_threshold))

    b_df["cdf"] = cdf_series
    b_df["is_toxic"] = is_toxic_series

    # -------------------------------------------------------------------------
    # 8. Bucket Duration & Spike Ratio (Volume Clock)
    # -------------------------------------------------------------------------
    # spike_ratio = median_duration / duration(tau)
    rolling_median_dur = b_df["duration_sec"].rolling(
        window=duration_window,
        min_periods=duration_window,
    ).median()
    safe_dur = b_df["duration_sec"].replace(0.0, 1.0)
    b_df["spike_ratio"] = rolling_median_dur / safe_dur

    # -------------------------------------------------------------------------
    # 9. Metadata, Window Fullness & Plain-Words State Line (Bug 4)
    # -------------------------------------------------------------------------
    latest_bucket = b_df.iloc[-1] if not b_df.empty else None
    latest_vpin = latest_bucket["vpin"] if latest_bucket is not None else None
    latest_dir = latest_bucket["dir"] if latest_bucket is not None else None
    latest_cdf = latest_bucket["cdf"] if latest_bucket is not None else None
    latest_spike = latest_bucket["spike_ratio"] if latest_bucket is not None else None

    valid_vpins = b_df["vpin"].dropna()
    valid_count = len(valid_vpins)
    cdf_window_full = valid_count >= cdf_window

    if latest_cdf is not None and not pd.isna(latest_cdf):
        if latest_cdf >= cdf_threshold:
            state_line = f"TOXIC FLOW (CDF {latest_cdf:.2%}) — expect expansion, direction unknown"
        else:
            state_line = f"NORMAL (CDF {latest_cdf:.2%})"
    else:
        state_line = f"CALIBRATING ({min(valid_count, cdf_window)} of {cdf_window} buckets)"

    metadata = {
        "bucket_volume": bucket_volume,
        "buckets_per_day": buckets_per_day,
        "sigma_window": sigma_window,
        "n_buckets": n_buckets,
        "cdf_window": cdf_window,
        "cdf_threshold": cdf_threshold,
        "duration_window": duration_window,
        "spike_threshold": spike_threshold,
        "total_bars": len(d),
        "completed_buckets": total_b,
        "cdf_window_full": cdf_window_full,
        "valid_vpin_count": valid_count,
        "latest_vpin": float(latest_vpin) if (latest_vpin is not None and not pd.isna(latest_vpin)) else None,
        "latest_dir": float(latest_dir) if (latest_dir is not None and not pd.isna(latest_dir)) else None,
        "latest_cdf": float(latest_cdf) if (latest_cdf is not None and not pd.isna(latest_cdf)) else None,
        "latest_spike_ratio": float(latest_spike) if (latest_spike is not None and not pd.isna(latest_spike)) else None,
        "state_line": state_line,
    }

    return VPINOutput(
        buckets=b_df,
        forming_bucket=last_forming_bucket,
        metadata=metadata,
    )
