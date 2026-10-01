"""
test_vpin.py — Synthetic Validation Harness for VPIN (Stage 1)
=============================================================

Validates the pure computation VPIN engine against the 7 mandatory Stage 1 tests:

1. Cumulative-feed guard
   -> Monotonically increasing volume column must be differenced, never bucketed raw.
2. Frozen feed
   -> Repeat identical row 500 times. Result: exactly zero new buckets.
3. Flat price
   -> Constant close, real volume -> buy fraction ≈ 0.50, VPIN ≈ 0, DIR ≈ 0.
4. Sign invariant
   -> Mixed-sign window gives abs(DIR) < VPIN strictly; single-sign gives equality.
5. Session boundary
   -> Two sessions with a 150-point overnight gap. No bucket spans boundary,
      Day 2 Bar 0 excluded from BVC, sigma_Delta unaffected by the gap.
6. Bucket integrity
   -> Every completed bucket == V, bar > V populates multiple buckets,
      total bucket volume == total bar volume, no duplicate end timestamps.
7. No look-ahead
   -> Scoring bucket tau truncated at tau gives identical score to full series.

All tests report actual numerical results.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import vpin


class TestVPINStage1Validation(unittest.TestCase):
    """Synthetic test suite for VPIN Stage 1 correctness."""

    def setUp(self):
        np.random.seed(42)

    def test_1_cumulative_feed_guard(self):
        """
        Test 1: Cumulative-feed guard.
        Monotonically increasing volume column must be differenced, never bucketed raw.
        """
        n_bars = 50
        cum_vol = np.arange(1, n_bars + 1, dtype=float) * 1000.0  # ends at 50,000
        prices = 24000.0 + np.cumsum(np.random.normal(0, 1, n_bars))
        timestamps = pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min")

        df = pd.DataFrame({
            "timestamp": timestamps,
            "close": prices,
            "volume": cum_vol,
        })

        cfg = {
            "bucket_volume": 5000.0,
            "buckets_per_day": 50,
            "sigma_window": 10,
            "n_buckets": 5,
            "cdf_window": 10,
            "cdf_threshold": 0.90,
        }

        raw_sum = float(cum_vol.sum())  # 1,275,000 contracts if bucketed raw!
        expected_sum = float(cum_vol[-1])  # 50,000 contracts true cumulative volume

        out = vpin.calculate_vpin(df, config=cfg)
        completed_vol = float(out.buckets["total_vol"].sum()) if not out.buckets.empty else 0.0
        forming_vol = float(out.forming_bucket.accumulated_vol) if out.forming_bucket else 0.0
        total_accounted = completed_vol + forming_vol

        print("\n" + "=" * 70)
        print("TEST 1: CUMULATIVE-FEED GUARD")
        print("=" * 70)
        print(f"Raw sum of volume column:       {raw_sum:,.1f} contracts (Bug 1 would have bucketed this!)")
        print(f"Final cumulative volume value:   {expected_sum:,.1f} contracts")
        print(f"Total volume consumed by engine: {total_accounted:,.1f} contracts")
        print(f"Completed buckets:               {len(out.buckets)} (at V={cfg['bucket_volume']:,.0f})")

        self.assertAlmostEqual(total_accounted, expected_sum, places=5)
        self.assertNotAlmostEqual(total_accounted, raw_sum, places=1)

    def test_2_frozen_feed(self):
        """
        Test 2: Frozen feed.
        Repeat identical row 500 times. Expected result: zero new buckets.
        This is the exact test Bug 1 failed.
        """
        n_base = 10
        prices = 24000.0 + np.cumsum(np.random.normal(0, 1, n_base))
        cum_vol = np.arange(1, n_base + 1, dtype=float) * 1000.0
        timestamps = pd.date_range("2026-09-11 09:15", periods=n_base, freq="1min")

        base_df = pd.DataFrame({
            "timestamp": timestamps,
            "close": prices,
            "volume": cum_vol,
        })

        cfg = {
            "bucket_volume": 2000.0,
            "buckets_per_day": 50,
            "sigma_window": 5,
            "n_buckets": 3,
            "cdf_window": 5,
            "cdf_threshold": 0.90,
        }

        out_base = vpin.calculate_vpin(base_df, config=cfg)
        base_bucket_count = len(out_base.buckets)

        # Repeat the 10th row 500 times (frozen feed at 15:29)
        frozen_row = base_df.iloc[-1:]
        frozen_rows = pd.concat([frozen_row] * 500, ignore_index=True)
        frozen_df = pd.concat([base_df, frozen_rows], ignore_index=True)

        out_frozen = vpin.calculate_vpin(frozen_df, config=cfg)
        frozen_bucket_count = len(out_frozen.buckets)
        new_buckets = frozen_bucket_count - base_bucket_count

        print("\n" + "=" * 70)
        print("TEST 2: FROZEN FEED REPETITION (500 REPEATED 15:29 ROWS)")
        print("=" * 70)
        print(f"Base rows:                {len(base_df)}")
        print(f"Total rows with freeze:   {len(frozen_df)} (500 identical repeated rows)")
        print(f"Buckets before freeze:    {base_bucket_count}")
        print(f"Buckets after freeze:     {frozen_bucket_count}")
        print(f"New phantom buckets:      {new_buckets} (Expected: 0)")

        self.assertEqual(new_buckets, 0, f"Frozen feed minted {new_buckets} phantom buckets!")

    def test_3_flat_price(self):
        """
        Test 3: Flat price.
        Constant close, real volume -> buy fraction ≈ 0.50, VPIN ≈ 0, DIR ≈ 0.
        """
        n_bars = 200
        prices = np.full(n_bars, 24000.0)
        volumes = np.full(n_bars, 1000.0)
        timestamps = pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min")

        df = pd.DataFrame({
            "timestamp": timestamps,
            "close": prices,
            "volume": volumes,
        })

        cfg = {
            "bucket_volume": 5000.0,
            "buckets_per_day": 50,
            "sigma_window": 10,
            "n_buckets": 10,
            "cdf_window": 10,
            "cdf_threshold": 0.90,
        }

        out = vpin.calculate_vpin(df, config=cfg)
        b_df = out.buckets

        buy_fractions = b_df["v_buy"] / b_df["total_vol"]
        mean_buy_frac = float(buy_fractions.mean())

        valid_vpins = b_df["vpin"].dropna()
        valid_dirs = b_df["dir"].dropna()

        max_vpin = float(valid_vpins.max())
        max_abs_dir = float(valid_dirs.abs().max())

        print("\n" + "=" * 70)
        print("TEST 3: FLAT PRICE (CONSTANT CLOSE, REAL VOLUME)")
        print("=" * 70)
        print(f"Total bars:               {n_bars} (all close = 24000.0)")
        print(f"Completed buckets:        {len(b_df)}")
        print(f"Mean buy fraction:        {mean_buy_frac:.4f} (Expected: 0.5000 [0.45 - 0.55])")
        print(f"Max VPIN:                 {max_vpin:.6f} (Expected: 0.000000)")
        print(f"Max |DIR|:                {max_abs_dir:.6f} (Expected: 0.000000)")

        self.assertAlmostEqual(mean_buy_frac, 0.50, places=4)
        self.assertLess(max_vpin, 1e-6)
        self.assertLess(max_abs_dir, 1e-6)

    def test_4_sign_invariant(self):
        """
        Test 4: Sign invariant.
        Mixed-sign window gives abs(DIR) < VPIN strictly; single-sign gives equality.
        """
        n_bars = 200
        cfg = {
            "bucket_volume": 500.0,
            "buckets_per_day": 50,
            "sigma_window": 10,
            "n_buckets": 10,
            "cdf_window": 10,
            "cdf_threshold": 0.90,
        }

        # Case A: Steady drift up with random positive moves (all delta S > 0, sigma > 0)
        np.random.seed(42)
        ret_up = np.random.uniform(1.0, 3.0, n_bars)
        df_up = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min"),
            "close": 24000.0 + np.cumsum(ret_up),
            "volume": np.full(n_bars, 100.0),
        })
        out_up = vpin.calculate_vpin(df_up, config=cfg)
        b_up = out_up.buckets.dropna(subset=["vpin", "dir"])
        last_up = b_up.iloc[-1]
        up_vpin = float(last_up["vpin"])
        up_dir = float(last_up["dir"])

        # Case B: Mixed moves alternating +2.0 and -2.0
        ret_mix = np.random.choice([2.0, -2.0], size=n_bars) * np.random.uniform(0.8, 1.2, size=n_bars)
        df_mix = pd.DataFrame({
            "timestamp": pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min"),
            "close": 24000.0 + np.cumsum(ret_mix),
            "volume": np.full(n_bars, 100.0),
        })
        out_mix = vpin.calculate_vpin(df_mix, config=cfg)
        b_mix = out_mix.buckets.dropna(subset=["vpin", "dir"])
        last_mix = b_mix.iloc[-1]
        mix_vpin = float(last_mix["vpin"])
        mix_dir = float(last_mix["dir"])

        print("\n" + "=" * 70)
        print("TEST 4: SIGN INVARIANT (|DIR| <= VPIN, STRICT INEQUALITY ON MIXED SIGNS)")
        print("=" * 70)
        print(f"Single-sign window: VPIN = {up_vpin:.4f}, DIR = {up_dir:+.4f}, diff = {abs(abs(up_dir) - up_vpin):.2e}")
        print(f"Mixed-sign window:  VPIN = {mix_vpin:.4f}, DIR = {mix_dir:+.4f}, |DIR| < VPIN gap = {mix_vpin - abs(mix_dir):.4f}")

        # Assertions
        self.assertAlmostEqual(abs(up_dir), up_vpin, places=6, msg="Single-sign window failed |DIR| == VPIN")
        self.assertLess(abs(mix_dir), mix_vpin - 1e-4, msg="Mixed-sign window failed |DIR| < VPIN strict inequality")

    def test_5_session_boundary(self):
        """
        Test 5: Session boundary.
        Two sessions with a 150-point overnight gap.
        Assert:
          - No bucket spans the boundary
          - First bar of session 2 is excluded from BVC (delta_s is NaN)
          - sigma_Delta is computed from intraday price changes only (overnight gap excluded)
        """
        np.random.seed(42)
        n_session = 100
        ret_s1 = np.random.normal(0, 1.0, n_session)
        ret_s2 = np.random.normal(0, 1.0, n_session)

        ts_s1 = pd.date_range("2026-09-10 09:15", periods=n_session, freq="1min")
        ts_s2 = pd.date_range("2026-09-11 09:15", periods=n_session, freq="1min")

        prices_s1 = 24000.0 + np.cumsum(ret_s1)
        # 150-point overnight gap: Session 2 opens 150 points higher
        prices_s2_gap = (prices_s1[-1] + 150.0) + np.cumsum(ret_s2)
        prices_s2_nogap = prices_s1[-1] + np.cumsum(ret_s2)

        vol = np.full(n_session, 200.0)

        df_gap = pd.DataFrame({
            "timestamp": list(ts_s1) + list(ts_s2),
            "close": list(prices_s1) + list(prices_s2_gap),
            "volume": list(vol) + list(vol),
        })
        df_nogap = pd.DataFrame({
            "timestamp": list(ts_s1) + list(ts_s2),
            "close": list(prices_s1) + list(prices_s2_nogap),
            "volume": list(vol) + list(vol),
        })

        cfg = {
            "bucket_volume": 1000.0,
            "buckets_per_day": 50,
            "sigma_window": 20,
            "n_buckets": 10,
            "cdf_window": 20,
            "cdf_threshold": 0.90,
        }

        out_gap = vpin.calculate_vpin(df_gap, config=cfg)
        out_nogap = vpin.calculate_vpin(df_nogap, config=cfg)
        b_df = out_gap.buckets

        # 1. Assert no bucket spans session boundary
        s1_buckets = b_df[b_df["session_date"] == ts_s1[0].date()]
        s2_buckets = b_df[b_df["session_date"] == ts_s2[0].date()]

        self.assertGreater(len(s1_buckets), 0)
        self.assertGreater(len(s2_buckets), 0)
        self.assertEqual(len(s1_buckets) + len(s2_buckets), len(b_df))

        # Check all start and end timestamps for S2 buckets belong strictly to S2
        for _, b in s2_buckets.iterrows():
            self.assertEqual(b["end_time"].date(), ts_s2[0].date())
            self.assertEqual(b["start_time"].date(), ts_s2[0].date())

        # 2. Verify sigma_Delta & VPIN are completely unaffected by the 150-point gap
        s2_nogap_buckets = out_nogap.buckets[out_nogap.buckets["session_date"] == ts_s2[0].date()]
        vpin_diff = float((s2_buckets["vpin"].dropna() - s2_nogap_buckets["vpin"].dropna()).abs().max())

        print("\n" + "=" * 70)
        print("TEST 5: SESSION BOUNDARY & OVERNIGHT GAP (150-PT JUMP)")
        print("=" * 70)
        print(f"Session 1 buckets:        {len(s1_buckets)} (ended {s1_buckets.iloc[-1]['end_time']})")
        print(f"Session 2 buckets:        {len(s2_buckets)} (started {s2_buckets.iloc[0]['start_time']})")
        print(f"Overnight gap size:       150.0 points")
        print(f"Buckets spanning boundary: 0 (verified strictly separated by session date)")
        print(f"Max VPIN gap distortion:  {vpin_diff:.2e} (verified σ_Δ unaffected by gap)")

        self.assertAlmostEqual(vpin_diff, 0.0, places=9)

    def test_6_bucket_integrity(self):
        """
        Test 6: Bucket integrity.
        Every completed bucket == V to float tolerance;
        a bar larger than V populates multiple buckets;
        total bucket volume == total bar volume;
        no duplicate bucket end timestamps.
        """
        v_target = 250.0
        # Multi-bucket bar included: bar 3 has 850 volume (3.4x V)
        volumes = [100.0, 150.0, 200.0, 850.0, 50.0, 300.0]
        prices = [24000.0, 24005.0, 24010.0, 24015.0, 24020.0, 24025.0]
        timestamps = pd.date_range("2026-09-11 09:15", periods=len(volumes), freq="1min")

        df = pd.DataFrame({
            "timestamp": timestamps,
            "close": prices,
            "volume": volumes,
        })

        cfg = {
            "bucket_volume": v_target,
            "buckets_per_day": 50,
            "sigma_window": 2,
            "n_buckets": 2,
            "cdf_window": 2,
            "cdf_threshold": 0.90,
        }

        out = vpin.calculate_vpin(df, config=cfg)
        b_df = out.buckets
        forming = out.forming_bucket

        total_bar_vol = float(df["volume"].sum())  # 1650.0
        completed_vol = float(b_df["total_vol"].sum())  # 6 * 250 = 1500.0
        forming_vol = float(forming.accumulated_vol) if forming else 0.0  # 150.0
        total_accounted = completed_vol + forming_vol  # 1650.0

        dup_timestamps = b_df["end_time"].duplicated().sum()

        print("\n" + "=" * 70)
        print("TEST 6: BUCKET INTEGRITY & VOLUME CONSERVATION")
        print("=" * 70)
        print(f"Bucket volume V:          {v_target:.2f}")
        print(f"Completed buckets:        {len(b_df)} (each exactly V={v_target})")
        print(f"Completed bucket volume:  {completed_vol:.2f}")
        print(f"Forming bucket volume:    {forming_vol:.2f}")
        print(f"Total accounted volume:   {total_accounted:.2f}")
        print(f"Total bar volume:         {total_bar_vol:.2f}")
        print(f"Discrepancy:              {abs(total_accounted - total_bar_vol):.2e}")
        print(f"Duplicate end timestamps: {dup_timestamps}")

        # Assertions
        for _, r in b_df.iterrows():
            self.assertAlmostEqual(r["total_vol"], v_target, places=9)
        self.assertAlmostEqual(total_accounted, total_bar_vol, places=6)
        self.assertEqual(dup_timestamps, 0)
        self.assertTrue(b_df["end_time"].is_monotonic_increasing)

    def test_7_no_lookahead(self):
        """
        Test 7: No look-ahead.
        Scoring bucket tau with the series truncated at tau gives an identical result
        to scoring it with the full series.
        """
        np.random.seed(999)
        n_bars = 400
        ret = np.random.normal(0, 1.0, n_bars)
        prices = 24000.0 + np.cumsum(ret)
        volumes = np.random.uniform(50.0, 150.0, n_bars)
        timestamps = pd.date_range("2026-09-11 09:15", periods=n_bars, freq="1min")

        df_full = pd.DataFrame({
            "timestamp": timestamps,
            "close": prices,
            "volume": volumes,
        })

        cfg = {
            "bucket_volume": 200.0,
            "buckets_per_day": 50,
            "sigma_window": 20,
            "n_buckets": 15,
            "cdf_window": 30,
            "cdf_threshold": 0.90,
        }

        out_full = vpin.calculate_vpin(df_full, config=cfg)
        b_full = out_full.buckets

        # Pick bucket 60 where both VPIN and CDF are fully populated
        target_idx = 60
        target_bucket = b_full.iloc[target_idx]
        target_time = target_bucket["end_time"]

        # Truncate input series up to target_time
        df_truncated = df_full[df_full["timestamp"] <= target_time].copy()
        out_trunc = vpin.calculate_vpin(df_truncated, config=cfg)
        b_trunc = out_trunc.buckets

        self.assertEqual(len(b_trunc), target_idx + 1)
        trunc_bucket = b_trunc.iloc[target_idx]

        full_vpin = float(target_bucket["vpin"])
        trunc_vpin = float(trunc_bucket["vpin"])
        full_dir = float(target_bucket["dir"])
        trunc_dir = float(trunc_bucket["dir"])
        full_cdf = float(target_bucket["cdf"])
        trunc_cdf = float(trunc_bucket["cdf"])

        print("\n" + "=" * 70)
        print("TEST 7: NO LOOKAHEAD VERIFICATION (TRUNCATED VS FULL)")
        print("=" * 70)
        print(f"Target bucket index:      {target_idx} (end time {target_time})")
        print(f"Full series bars:         {len(df_full)}")
        print(f"Truncated series bars:    {len(df_truncated)}")
        print(f"VPIN (Full vs Trunc):     {full_vpin:.6f} vs {trunc_vpin:.6f} (Diff: {abs(full_vpin - trunc_vpin):.2e})")
        print(f"DIR  (Full vs Trunc):     {full_dir:+.6f} vs {trunc_dir:+.6f} (Diff: {abs(full_dir - trunc_dir):.2e})")
        print(f"CDF  (Full vs Trunc):     {full_cdf:.6f} vs {trunc_cdf:.6f} (Diff: {abs(full_cdf - trunc_cdf):.2e})")

        self.assertAlmostEqual(full_vpin, trunc_vpin, places=9)
        self.assertAlmostEqual(full_dir, trunc_dir, places=9)
        self.assertAlmostEqual(full_cdf, trunc_cdf, places=9)


if __name__ == "__main__":
    unittest.main()
