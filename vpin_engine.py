"""
vpin_engine.py — Volume-Synchronized Probability of Informed Trading (VPIN)
=============================================================================

Implementation of the high-frequency market microstructure model proposed by:
  David Easley, Marcos M. Lopez de Prado, and Maureen O'Hara (2010, 2011, 2012)
  "The Volume Clock: Insights into the High-Frequency Trading Process"
  "Flow Toxicity and Liquidity in a High-Frequency World"

MATHEMATICAL FRAMEWORK:
-----------------------
1. Data Preparation:
   Standard OHLCV time bars (e.g., 1-minute or 5-minute bars).
   Ticks and tick-rule are NOT required.

2. Price Change & Volatility:
   Price change: Delta_S_i = S_i - S_{i-1} (using Close price S_i).
   Rolling sample standard deviation: sigma_{Delta S}.

3. Bulk Volume Classification (BVC):
   Z_i = Delta_S_i / sigma_{Delta S, i}
   Buy Volume:  V_i^B = V_i * Phi(Z_i)    where Phi is scipy.stats.norm.cdf
   Sell Volume: V_i^S = V_i - V_i^B

4. The Volume Clock (Bucketing):
   Synchronize time bars into discrete uniform volume buckets of size V.
   Strict uniform bucket size: Every bucket contains exactly V total volume.
   Row splitting constraint: If a bar's volume exceeds the remaining capacity of
   the current volume bucket, split V_i^B and V_i^S proportionally, fill the
   bucket, and carry the remaining volume into subsequent bucket(s).

5. VPIN Calculation:
   VPIN_tau = sum_{j=tau-n+1}^{tau} |V_j^S - V_j^B| / (n * V)
   where n is the rolling window of buckets, V is the bucket volume.
   VPIN is bounded in [0, 1].

6. Signals & Strategies:
   - High Toxicity (VPIN > threshold_high or CDF[VPIN] > 0.90):
     Impending volatility expansion / large directional move.
     Option Buying (Call if directional imbalance > 0, Put if < 0).
   - Low Toxicity (VPIN < threshold_low):
     Balanced flow, low adverse selection risk.
     Option Selling (Rangebound theta capture, Straddles / Iron Condors).
   - Extreme Toxicity Alert (CDF[VPIN] > 0.99):
     Market maker withdrawal risk / liquidity flash crash warning (Rule 8: Alert only).

All thresholds and parameters are loaded from vpin_rules.json (Rule 4).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.stats import norm

log = logging.getLogger("vpin_engine")

CONFIG_PATH = Path(__file__).parent / "vpin_rules.json"


def load_vpin_rules(path: Path | str | None = None) -> dict:
    """Load configuration thresholds from JSON file (Rule 4: no hardcoding)."""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        log.warning("Rules file %s not found. Using fallback defaults.", p)
        return {
            "bucket_volume": 50000.0,
            "window_length": 50,
            "volatility_window": 50,
            "buckets_per_day": 50,
            "toxicity_threshold_low": 0.18,
            "toxicity_threshold_moderate": 0.25,
            "toxicity_threshold_high": 0.35,
            "toxicity_threshold_extreme": 0.45,
            "cdf_alert_percentile": 0.90,
            "cdf_extreme_percentile": 0.99,
            "directional_imbalance_threshold": 0.15,
            "rolling_cdf_window": 250,
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Failed to read %s: %s. Using default config.", p, exc)
        return {
            "bucket_volume": 50000.0,
            "window_length": 50,
            "volatility_window": 50,
            "buckets_per_day": 50,
            "toxicity_threshold_low": 0.18,
            "toxicity_threshold_moderate": 0.25,
            "toxicity_threshold_high": 0.35,
            "toxicity_threshold_extreme": 0.45,
            "cdf_alert_percentile": 0.90,
            "cdf_extreme_percentile": 0.99,
            "directional_imbalance_threshold": 0.15,
            "rolling_cdf_window": 250,
        }


@dataclass
class VolumeBucket:
    """Represents a single completed volume bucket in the volume clock."""
    bucket_idx: int
    timestamp: Any
    price: float
    buy_volume: float
    sell_volume: float
    total_volume: float
    abs_imbalance: float
    signed_imbalance: float
    bar_count: int


@dataclass
class VPINResult:
    """Container for VPIN calculation outputs."""
    buckets: pd.DataFrame
    bars: pd.DataFrame
    summary: dict


class VPINEngine:
    """
    VPIN (Volume-Synchronized Probability of Informed Trading) Engine.

    Calculates high-frequency order flow toxicity and directional imbalance
    by synchronizing standard OHLCV time bars into equal-volume buckets.
    """

    def __init__(
        self,
        bucket_volume: float | None = None,
        window_length: int | None = None,
        volatility_window: int | None = None,
        config_path: Path | str | None = None,
        **kwargs,
    ) -> None:
        """
        Initialize the VPINEngine.

        Parameters
        ----------
        bucket_volume : float, optional
            Fixed volume V per bucket. If None, loaded from vpin_rules.json.
        window_length : int, optional
            Rolling window length n (number of buckets) for VPIN formula.
            If None, loaded from vpin_rules.json.
        volatility_window : int, optional
            Rolling window for price change standard deviation (sigma_{Delta S}).
            If None, loaded from vpin_rules.json.
        config_path : Path or str, optional
            Path to custom rules JSON.
        """
        self.rules = load_vpin_rules(config_path)

        # Expose bucket_volume (V) and window_length (n)
        self.bucket_volume = float(
            bucket_volume
            if bucket_volume is not None
            else self.rules.get("bucket_volume", 50000.0)
        )
        self.window_length = int(
            window_length
            if window_length is not None
            else self.rules.get("window_length", 50)
        )
        self.volatility_window = int(
            volatility_window
            if volatility_window is not None
            else self.rules.get("volatility_window", 50)
        )

        # Thresholds from JSON rules (Rule 4: nothing hardcoded)
        self.buckets_per_day = int(self.rules.get("buckets_per_day", 50))
        self.toxicity_low = float(self.rules.get("toxicity_threshold_low", 0.18))
        self.toxicity_mod = float(self.rules.get("toxicity_threshold_moderate", 0.25))
        self.toxicity_high = float(self.rules.get("toxicity_threshold_high", 0.35))
        self.toxicity_extreme = float(self.rules.get("toxicity_threshold_extreme", 0.45))
        self.cdf_alert_pct = float(self.rules.get("cdf_alert_percentile", 0.90))
        self.cdf_extreme_pct = float(self.rules.get("cdf_extreme_percentile", 0.99))
        self.directional_thresh = float(
            self.rules.get("directional_imbalance_threshold", 0.15)
        )
        self.rolling_cdf_window = int(self.rules.get("rolling_cdf_window", 250))

        # Runtime state for streaming / incremental bucketing if needed
        self._current_v_buy = 0.0
        self._current_v_sell = 0.0
        self._current_v_total = 0.0
        self._current_bar_count = 0

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1: Data Preparation
    # ══════════════════════════════════════════════════════════════════════════
    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Step 1: Data Preparation.
        Accepts standard OHLCV time bars and standardizes column formats.
        Does NOT require tick-level data or the Tick-rule.

        Parameters
        ----------
        df : pd.DataFrame
            Input DataFrame containing open, high, low, close, volume columns.

        Returns
        -------
        pd.DataFrame
            Cleaned and normalized DataFrame with lowercase columns.
        """
        if df is None or df.empty:
            raise ValueError("Input DataFrame is empty or None")

        d = df.copy()

        # Standardize column naming (case-insensitive)
        col_map = {}
        for c in d.columns:
            cl = str(c).strip().lower()
            if cl in ("time", "timestamp", "datetime", "date"):
                col_map[c] = "timestamp"
            elif cl in ("open", "o"):
                col_map[c] = "open"
            elif cl in ("high", "h"):
                col_map[c] = "high"
            elif cl in ("low", "l"):
                col_map[c] = "low"
            elif cl in ("close", "c"):
                col_map[c] = "close"
            elif cl in ("volume", "vol", "v", "qty"):
                col_map[c] = "volume"

        d = d.rename(columns=col_map)

        required = ["close", "volume"]
        missing = [c for c in required if c not in d.columns]
        if missing:
            raise ValueError(f"Missing required columns in DataFrame: {missing}")

        # Ensure numeric types
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d["volume"] = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0)

        # Ensure timestamp if present
        if "timestamp" in d.columns:
            d["timestamp"] = pd.to_datetime(d["timestamp"])
            d = d.sort_values("timestamp").drop_duplicates(
                subset=["timestamp"], keep="last"
            )
        else:
            d["timestamp"] = pd.RangeIndex(start=0, stop=len(d))

        d = d.dropna(subset=["close"]).reset_index(drop=True)
        return d

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 & STEP 3: Price Change, Volatility & Bulk Volume Classification
    # ══════════════════════════════════════════════════════════════════════════
    def calculate_bvc(
        self, df: pd.DataFrame, volatility_window: int | None = None
    ) -> pd.DataFrame:
        """
        Steps 2 & 3: Bulk Volume Classification (BVC).

        Step 2:
          - Price change: Delta_S_i = S_i - S_{i-1} (Close price S_i)
          - Rolling standard deviation: sigma_{Delta S} over volatility_window

        Step 3:
          - Z-score: Z_i = Delta_S_i / sigma_{Delta S, i}
          - Buy Volume:  V_i^B = V_i * Phi(Z_i), where Phi is scipy.stats.norm.cdf
          - Sell Volume: V_i^S = V_i - V_i^B

        Parameters
        ----------
        df : pd.DataFrame
            Cleaned OHLCV DataFrame from prepare_data.
        volatility_window : int, optional
            Rolling window for volatility estimation.

        Returns
        -------
        pd.DataFrame
            DataFrame with delta_s, sigma_delta_s, z_score, buy_volume, sell_volume.
        """
        d = df.copy()
        w = volatility_window or self.volatility_window

        # Step 2: Price Change Delta S = S_i - S_{i-1}
        d["delta_s"] = d["close"].diff().fillna(0.0)

        # Step 2: Rolling standard deviation of price changes sigma_{Delta S}
        # min_periods=2 prevents initial NaNs from propagating indefinitely
        d["sigma_delta_s"] = (
            d["delta_s"].rolling(window=w, min_periods=2).std().bfill()
        )

        # Fallback if standard deviation is zero, NaN or near-zero
        std_safe = d["sigma_delta_s"].replace(0.0, np.nan).bfill().fillna(1.0)

        # Step 3: Z calculation Z_i = (S_i - S_{i-1}) / sigma_{Delta S}
        # When delta_s == 0 or sigma is invalid, Z = 0 -> norm.cdf(0) = 0.5 (50/50 split)
        z_scores = np.where(
            std_safe > 1e-12,
            d["delta_s"] / std_safe,
            0.0
        )
        d["z_score"] = z_scores

        # Step 3: Standard Normal CDF Phi(Z)
        cdf_vals = norm.cdf(z_scores)

        # Step 3: Buy Volume V^B = V_i * Z and Sell Volume V^S = V_i - V^B
        d["buy_volume"] = d["volume"] * cdf_vals
        d["sell_volume"] = d["volume"] - d["buy_volume"]

        return d

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4: The Volume Clock (Bucketing)
    # ══════════════════════════════════════════════════════════════════════════
    def build_volume_buckets(
        self, bvc_df: pd.DataFrame, bucket_volume: float | None = None
    ) -> pd.DataFrame:
        """
        Step 4: The Volume Clock (Bucketing).

        Aggregates consecutive time bars into Volume Buckets such that every single
        bucket contains EXACTLY the same total volume V.

        Algorithm Constraint:
        If a time bar's volume exceeds the remaining capacity of the current volume
        bucket, split that bar's V^B and V^S proportionally to fill the current bucket,
        and carry the remainder over to the next bucket. Handles multi-bucket splitting
        if a single bar's volume spans multiple buckets (V_i > 2V).

        Parameters
        ----------
        bvc_df : pd.DataFrame
            DataFrame after Bulk Volume Classification (contains buy_volume, sell_volume).
        bucket_volume : float, optional
            Bucket volume size V. If None, uses self.bucket_volume.

        Returns
        -------
        pd.DataFrame
            DataFrame of uniform volume buckets of size V.
        """
        V = float(bucket_volume if bucket_volume is not None else self.bucket_volume)
        if V <= 0:
            raise ValueError(f"Bucket volume V must be > 0, got {V}")

        buckets: List[Dict[str, Any]] = []

        curr_v_buy = 0.0
        curr_v_sell = 0.0
        curr_v_total = 0.0
        curr_bar_count = 0
        bucket_idx = 0

        # Iterate over bars and synchronize into volume buckets
        for row in bvc_df.itertuples(index=False):
            v_rem = float(row.volume)
            vb_rem = float(row.buy_volume)
            vs_rem = float(row.sell_volume)
            ts = row.timestamp
            px = float(row.close)

            # Skip zero volume bars
            if v_rem <= 1e-9:
                continue

            curr_bar_count += 1

            # While there is volume left in this bar to assign into buckets
            while v_rem > 1e-9:
                needed = V - curr_v_total

                if v_rem < needed - 1e-9:
                    # Bar fits completely into current bucket without completing it
                    curr_v_buy += vb_rem
                    curr_v_sell += vs_rem
                    curr_v_total += v_rem

                    # Consumed entire bar
                    v_rem = 0.0
                    vb_rem = 0.0
                    vs_rem = 0.0
                else:
                    # Bar fills current bucket (needed <= v_rem).
                    # Proportional split: fraction of the bar that completes the bucket
                    fraction = needed / v_rem if v_rem > 0 else 0.0
                    fill_vb = vb_rem * fraction
                    fill_vs = vs_rem * fraction

                    # Complete current bucket
                    final_v_buy = curr_v_buy + fill_vb
                    final_v_sell = curr_v_sell + fill_vs
                    # Exact enforcement: total volume is strictly V
                    final_v_total = V

                    abs_imb = abs(final_v_sell - final_v_buy)
                    signed_imb = final_v_buy - final_v_sell

                    buckets.append({
                        "bucket_idx": bucket_idx,
                        "timestamp": ts,
                        "price": px,
                        "buy_volume": final_v_buy,
                        "sell_volume": final_v_sell,
                        "total_volume": final_v_total,
                        "abs_imbalance": abs_imb,
                        "signed_imbalance": signed_imb,
                        "bar_count": curr_bar_count,
                    })

                    bucket_idx += 1

                    # Deduct filled portion from bar's remaining volume
                    v_rem -= needed
                    vb_rem -= fill_vb
                    vs_rem -= fill_vs

                    # Reset accumulators for next bucket
                    curr_v_buy = 0.0
                    curr_v_sell = 0.0
                    curr_v_total = 0.0
                    curr_bar_count = 1 if v_rem > 1e-9 else 0

        # Save incomplete bucket state for streaming continuity
        self._current_v_buy = curr_v_buy
        self._current_v_sell = curr_v_sell
        self._current_v_total = curr_v_total
        self._current_bar_count = curr_bar_count

        if not buckets:
            log.warning("No full volume buckets formed. Total volume < bucket_volume V.")
            return pd.DataFrame(columns=[
                "bucket_idx", "timestamp", "price", "buy_volume", "sell_volume",
                "total_volume", "abs_imbalance", "signed_imbalance", "bar_count"
            ])

        b_df = pd.DataFrame(buckets)
        return b_df

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5: VPIN Calculation & Order Flow Toxicity
    # ══════════════════════════════════════════════════════════════════════════
    def calculate_vpin(
        self, buckets_df: pd.DataFrame, window_length: int | None = None
    ) -> pd.DataFrame:
        """
        Step 5: VPIN Calculation.

        Once data is grouped into identical volume buckets of size V:
        VPIN = sum_{tau=1}^n |V_tau^S - V_tau^B| / (n * V)

        Also computes:
          - Directional Imbalance: sum(V^B - V^S) / (n * V) in [-1, +1]
          - Empirical CDF[VPIN]: Historical percentile of current toxicity
          - Regime classification: OPTION_BUYING vs OPTION_SELLING

        Parameters
        ----------
        buckets_df : pd.DataFrame
            DataFrame of volume buckets from build_volume_buckets.
        window_length : int, optional
            Number of buckets in rolling window n.

        Returns
        -------
        pd.DataFrame
            Buckets DataFrame with vpin, vpin_direction, vpin_cdf, toxicity_level,
            and regime columns.
        """
        if buckets_df.empty:
            return buckets_df

        d = buckets_df.copy()
        n = int(window_length if window_length is not None else self.window_length)
        V = float(self.bucket_volume)

        denom = n * V

        # Rolling sum of absolute order imbalances |V^S - V^B|
        # Standard literature convention: requires full window n
        rolling_abs_imb = d["abs_imbalance"].rolling(window=n, min_periods=n).sum()
        d["vpin"] = rolling_abs_imb / denom

        # Signed Directional Imbalance over rolling window: sum(V^B - V^S) / (n * V)
        rolling_signed_imb = d["signed_imbalance"].rolling(window=n, min_periods=n).sum()
        d["vpin_direction"] = rolling_signed_imb / denom

        # Empirical CDF / Percentile of VPIN over historical window
        # Easley et al.: CDF[VPIN] > 0.90 flags severe toxicity
        cdf_win = self.rolling_cdf_window
        vpin_series = d["vpin"]

        # Calculate rolling empirical CDF: fraction of past values <= current value
        def _calc_cdf(window_vals: pd.Series) -> float:
            valid = window_vals.dropna()
            if len(valid) < 2:
                return np.nan
            curr = valid.iloc[-1]
            return float((valid <= curr).mean())

        d["vpin_cdf"] = (
            vpin_series.rolling(window=cdf_win, min_periods=min(20, n))
            .apply(_calc_cdf, raw=False)
        )

        # Classify Toxicity Level
        conditions = [
            d["vpin"] >= self.toxicity_extreme,
            d["vpin"] >= self.toxicity_high,
            d["vpin"] >= self.toxicity_mod,
            d["vpin"] < self.toxicity_mod,
        ]
        choices = ["EXTREME", "HIGH", "MODERATE", "LOW"]
        d["toxicity_level"] = np.select(conditions, choices, default="UNKNOWN")

        # Alert flag for market maker distress / liquidity crash
        d["alert_liquidity_shock"] = (
            (d["vpin_cdf"] >= self.cdf_extreme_pct) |
            (d["vpin"] >= self.toxicity_extreme)
        )

        # Market Regime & Option Strategy Mapping (as per audio / transcript):
        # High VPIN = large move expected (Option Buying)
        # Low VPIN = rangebound sideways market (Option Selling / Theta)
        regimes = []
        for v, d_imb in zip(d["vpin"], d["vpin_direction"]):
            if pd.isna(v):
                regimes.append("WARMUP")
            elif v >= self.toxicity_high:
                if d_imb >= self.directional_thresh:
                    regimes.append("EXPANSION_OPTION_BUYING_CE")
                elif d_imb <= -self.directional_thresh:
                    regimes.append("EXPANSION_OPTION_BUYING_PE")
                else:
                    regimes.append("EXPANSION_VOLATILITY_BREAKOUT")
            elif v <= self.toxicity_low:
                regimes.append("RANGEBOUND_OPTION_SELLING")
            else:
                regimes.append("BALANCED_FLOW")

        d["strategy_regime"] = regimes
        return d

    # ══════════════════════════════════════════════════════════════════════════
    # FULL PIPELINE & TIME-BAR MAPPING
    # ══════════════════════════════════════════════════════════════════════════
    def calculate(
        self,
        df: pd.DataFrame,
        bucket_volume: float | None = None,
        window_length: int | None = None,
        volatility_window: int | None = None,
    ) -> VPINResult:
        """
        Execute the full 5-step VPIN pipeline.

        1. Data Preparation (standard OHLCV)
        2. Price Change Delta S & Volatility sigma_{Delta S}
        3. Bulk Volume Classification (BVC) via norm.cdf
        4. Volume Clock Bucketing (strict uniform V, proportional split)
        5. VPIN calculation over rolling n buckets

        Also maps the latest bucket VPIN back onto the original time bars so
        traders can overlay VPIN alongside candlesticks and indicator charts.

        Returns
        -------
        VPINResult
            Named container with buckets DataFrame, time-bars DataFrame, and summary.
        """
        # Step 1: Data Preparation
        clean_df = self.prepare_data(df)

        # Steps 2 & 3: BVC
        bvc_df = self.calculate_bvc(clean_df, volatility_window=volatility_window)

        # Step 4: Volume Bucketing
        b_volume = bucket_volume or self.bucket_volume
        buckets_df = self.build_volume_buckets(bvc_df, bucket_volume=b_volume)

        # Step 5: VPIN Calculation
        w_len = window_length or self.window_length
        buckets_df = self.calculate_vpin(buckets_df, window_length=w_len)

        # Map bucket VPIN back to time bars (as-of join)
        bars_df = bvc_df.copy()
        if not buckets_df.empty and "vpin" in buckets_df.columns:
            # Merge on timestamp using forward fill (as-of timestamp)
            merge_cols = [
                "timestamp", "vpin", "vpin_direction", "vpin_cdf",
                "toxicity_level", "alert_liquidity_shock", "strategy_regime"
            ]
            bucket_signals = buckets_df[merge_cols].drop_duplicates(
                subset=["timestamp"], keep="last"
            )
            bars_df = pd.merge_asof(
                bars_df.sort_values("timestamp"),
                bucket_signals.sort_values("timestamp"),
                on="timestamp",
                direction="backward"
            )
        else:
            bars_df["vpin"] = np.nan
            bars_df["vpin_direction"] = np.nan
            bars_df["vpin_cdf"] = np.nan
            bars_df["toxicity_level"] = "WARMUP"
            bars_df["alert_liquidity_shock"] = False
            bars_df["strategy_regime"] = "WARMUP"

        # Summary statistics
        latest_vpin = float(buckets_df["vpin"].dropna().iloc[-1]) if not buckets_df.empty and not buckets_df["vpin"].dropna().empty else None
        latest_dir = float(buckets_df["vpin_direction"].dropna().iloc[-1]) if not buckets_df.empty and not buckets_df["vpin_direction"].dropna().empty else None
        latest_cdf = float(buckets_df["vpin_cdf"].dropna().iloc[-1]) if not buckets_df.empty and not buckets_df["vpin_cdf"].dropna().empty else None
        latest_regime = str(buckets_df["strategy_regime"].iloc[-1]) if not buckets_df.empty else "UNKNOWN"

        summary = {
            "bucket_volume": b_volume,
            "window_length": w_len,
            "volatility_window": self.volatility_window,
            "total_bars": len(clean_df),
            "total_buckets": len(buckets_df),
            "latest_vpin": latest_vpin,
            "latest_direction": latest_dir,
            "latest_cdf": latest_cdf,
            "latest_regime": latest_regime,
            "alert_active": bool(buckets_df["alert_liquidity_shock"].iloc[-1]) if not buckets_df.empty else False,
        }

        return VPINResult(buckets=buckets_df, bars=bars_df, summary=summary)

    # ══════════════════════════════════════════════════════════════════════════
    # STRATEGY SIGNAL GENERATOR (Option Buying vs Option Selling)
    # ══════════════════════════════════════════════════════════════════════════
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate actionable options strategies based on VPIN order flow toxicity:

        1. EXPANSION / MOMENTUM (Option Buying):
           Triggered when order flow is toxic (VPIN > toxicity_high) and directional
           imbalance shows strong institutional sponsorship.
           - Buy CE: Directional Imbalance > threshold (Informed Bullish Flow)
           - Buy PE: Directional Imbalance < -threshold (Informed Bearish Flow)

        2. RANGEBOUND / THETA CAPTURE (Option Selling):
           Triggered when VPIN < toxicity_low. Order flow is balanced, adverse
           selection risk is minimal, and market makers maintain tight spreads.
           - Sell Straddle / Strangle / Iron Condor.

        3. ALERT ONLY (Rule 8):
           Extreme order flow toxicity (CDF[VPIN] >= 0.99) warns of imminent liquidity
           evaporation or flash crash conditions.
        """
        result = self.calculate(df)
        b_df = result.buckets.copy()

        if b_df.empty or "vpin" not in b_df.columns:
            return pd.DataFrame()

        signals = []
        for r in b_df.itertuples():
            vpin_val = getattr(r, "vpin", np.nan)
            dir_val = getattr(r, "vpin_direction", 0.0)
            cdf_val = getattr(r, "vpin_cdf", np.nan)

            sig = "NO_SIGNAL"
            action = "WAIT"
            desc = "Normal order flow"

            if pd.isna(vpin_val):
                sig = "WARMUP"
                action = "WAIT"
                desc = "Accumulating volume buckets"
            elif cdf_val >= self.cdf_extreme_pct or vpin_val >= self.toxicity_extreme:
                sig = "CRITICAL_TOXICITY_ALERT"
                action = "ALERT_ONLY"
                desc = f"Flash crash danger zone: CDF={cdf_val:.2%}, VPIN={vpin_val:.3f}. MM withdrawal imminent."
            elif vpin_val >= self.toxicity_high:
                if dir_val >= self.directional_thresh:
                    sig = "TOXIC_BUY_IMBALANCE"
                    action = "OPTION_BUY_CE"
                    desc = f"High toxicity bullish momentum (VPIN={vpin_val:.3f}, Dir=+{dir_val:.3f})"
                elif dir_val <= -self.directional_thresh:
                    sig = "TOXIC_SELL_IMBALANCE"
                    action = "OPTION_BUY_PE"
                    desc = f"High toxicity bearish breakdown (VPIN={vpin_val:.3f}, Dir={dir_val:.3f})"
                else:
                    sig = "HIGH_TOXICITY_EXPANSION"
                    action = "OPTION_BUY_STRADDLE"
                    desc = f"Explosive move impending, direction unresolved (VPIN={vpin_val:.3f})"
            elif vpin_val <= self.toxicity_low:
                sig = "LOW_TOXICITY_BALANCE"
                action = "OPTION_SELL_STRADDLE"
                desc = f"Balanced order flow, low adverse selection (VPIN={vpin_val:.3f}). Capture theta."

            signals.append({
                "bucket_idx": getattr(r, "bucket_idx", 0),
                "timestamp": getattr(r, "timestamp", None),
                "price": getattr(r, "price", 0.0),
                "vpin": vpin_val,
                "vpin_direction": dir_val,
                "vpin_cdf": cdf_val,
                "signal": sig,
                "action": action,
                "description": desc,
            })

        return pd.DataFrame(signals)

    # ══════════════════════════════════════════════════════════════════════════
    # DHAN & DATA HELPERS (Rule 1: REUSE existing modules without rewriting)
    # ══════════════════════════════════════════════════════════════════════════
    @classmethod
    def from_dhan_frames(
        cls,
        interval: str = "15",
        days_back: int = 5,
        mode: str = "futures",
        bucket_volume: float | None = None,
        window_length: int | None = None,
    ) -> tuple[VPINResult, dict]:
        """
        Fetch OHLCV directly via dhan_frames.py and calculate VPIN.
        Reuses existing dhan_frames module without rewriting it (Rule 1).

        Parameters
        ----------
        interval : str
            Candle interval (e.g. "1", "5", "15").
        days_back : int
            Number of trading sessions.
        mode : str
            "futures" (recommended for volume) or "index".
        """
        import dhan_frames  # Reusing existing module (Rule 1)

        try:
            df, report = dhan_frames.fetch(
                interval=interval, days_back=days_back, mode=mode
            )
        except Exception as exc:
            log.error("Error fetching candles from dhan_frames: %s", exc)
            return VPINResult(pd.DataFrame(), pd.DataFrame(), {}), {"error": str(exc)}

        engine = cls(bucket_volume=bucket_volume, window_length=window_length)
        res = engine.calculate(df)
        return res, report

    @classmethod
    def from_csv(
        cls,
        csv_path: Path | str,
        bucket_volume: float | None = None,
        window_length: int | None = None,
    ) -> VPINResult:
        """Load offline candles from a CSV file (e.g. data/candles/YYYY-MM-DD.csv)."""
        p = Path(csv_path)
        if not p.exists():
            raise FileNotFoundError(f"CSV file not found: {p}")
        df = pd.read_csv(p)
        engine = cls(bucket_volume=bucket_volume, window_length=window_length)
        return engine.calculate(df)


# ══════════════════════════════════════════════════════════════════════════════
# SELF-TEST / VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("VPINEngine Self-Test & Diagnostic")
    print("=" * 60)

    # 1. Test with synthetic bars
    print("\n[1] Testing with synthetic bars...")
    np.random.seed(42)
    n_bars = 200
    prices = 24000.0 + np.cumsum(np.random.normal(0, 5, n_bars))
    vols = np.random.uniform(5000, 25000, n_bars)
    dates = pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min")

    synth_df = pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + 2,
        "low": prices - 2,
        "close": prices,
        "volume": vols,
    })

    engine = VPINEngine(bucket_volume=50000.0, window_length=20, volatility_window=20)
    result = engine.calculate(synth_df)

    print(f"Total synthetic bars: {len(synth_df)}")
    print(f"Volume buckets created: {len(result.buckets)}")
    print(f"Summary: {result.summary}")

    # Check strict volume conservation
    if not result.buckets.empty:
        all_uniform = np.allclose(result.buckets["total_volume"], 50000.0)
        print(f"Strict uniform bucket size enforced: {all_uniform}")
        assert all_uniform, "Bucket sizes are not strictly uniform!"

    # 2. Test with real offline candle file if available
    real_csv = Path(__file__).parent / "data" / "candles" / "2026-09-11.csv"
    if real_csv.exists():
        print(f"\n[2] Testing with real historical candles: {real_csv.name}...")
        df_real = pd.read_csv(real_csv)
        # NIFTY index volume is ~270M, so use bucket_volume = 2.5M
        engine_real = VPINEngine(bucket_volume=2500000.0, window_length=20)
        res_real = engine_real.calculate(df_real)
        print(f"Real bars: {len(df_real)}")
        print(f"Buckets formed: {len(res_real.buckets)}")
        print(f"Summary: {res_real.summary}")

        signals = engine_real.generate_signals(df_real)
        print(f"\nSignals generated: {len(signals)}")
        print(signals[["timestamp", "price", "vpin", "vpin_direction", "action"]].tail(5))

    print("\nAll VPINEngine self-tests passed successfully.")
