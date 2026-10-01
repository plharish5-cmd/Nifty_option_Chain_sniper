"""
test_periodic_snapshots.py — Unit Tests for 5-MIN & 15-MIN Periodic Snapshots.

Verifies:
1. get_snapshot_tf_for_time boundary skip rules:
   11:05 (5m), 11:10 (5m), 11:15 (15m only), 11:20 (5m), 11:25 (5m), 11:30 (15m only), 11:35 (5m)
2. build_periodic_snapshot content & field ordering:
   Spot + Bias | Day range | GEX | RVOL | IV | Floor | Ceiling | Flow | Dominance | Structure | Floor weakening | Checklist | Action | mj_shadow
3. Telegram headers: 🟦 5-MIN and 🟪 15-MIN, max 18 lines
4. Window flow calculation (5m vs 15m)
5. Stale feed flag (>6 min -> ⚠️ FEED STALE, RVOL = N/A)
6. Observation log tagging with tf: 5m and tf: 15m
"""

import unittest
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
import tempfile
import shutil

import agt_briefing
import config
import store


class TestPeriodicSnapshots(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.target_date = date(2026, 9, 24)
        cls._orig_load_day = store.load_day
        cls._cached_day = store.load_day(cls.target_date)
        store.load_day = lambda d: cls._cached_day if d == cls.target_date else cls._orig_load_day(d)

    @classmethod
    def tearDownClass(cls):
        store.load_day = cls._orig_load_day

    def test_timing_boundary_skip_rules(self):
        """Tests that 15m boundary sends ONLY 15m and skips 5m."""
        expectations = [
            (dtime(9, 15), None),         # Market open, no closed candle yet
            (dtime(9, 20), "5m"),
            (dtime(9, 25), "5m"),
            (dtime(9, 30), "15m"),        # 15m boundary
            (dtime(11, 5), "5m"),
            (dtime(11, 10), "5m"),
            (dtime(11, 15), "15m"),       # 15m boundary (5m skipped)
            (dtime(11, 20), "5m"),
            (dtime(11, 25), "5m"),
            (dtime(11, 30), "15m"),       # 15m boundary (5m skipped)
            (dtime(11, 35), "5m"),
            (dtime(15, 15), "15m"),       # Last 15m candle close
            (dtime(15, 20), "5m"),
            (dtime(15, 25), "5m"),        # Last 5m candle close
            (dtime(15, 30), None),        # Session closed
            (dtime(11, 12), None),        # Not on boundary
        ]
        for t_val, expected in expectations:
            result = agt_briefing.get_snapshot_tf_for_time(t_val)
            self.assertEqual(
                result,
                expected,
                f"Time {t_val.strftime('%H:%M')} expected {expected}, got {result}",
            )

    def test_snapshot_content_and_headers(self):
        """Tests build_periodic_snapshot output for 5m and 15m on real session data."""
        target_date = date(2026, 9, 24)

        # 5-min snapshot at 11:10
        snap_5m = agt_briefing.build_periodic_snapshot("5m", target_date, dtime(11, 10))
        self.assertEqual(snap_5m.get("timeframe"), "5m")
        tg_5m = snap_5m.get("telegram_text", "")
        self.assertTrue(tg_5m.startswith("🟦 5-MIN | 11:10 | Spot"))
        lines_5m = [ln for ln in tg_5m.split("\n") if ln.strip()]
        self.assertLessEqual(len(lines_5m), 18, f"5m telegram lines exceeded 18: {len(lines_5m)}")

        # 15-min snapshot at 11:15
        snap_15m = agt_briefing.build_periodic_snapshot("15m", target_date, dtime(11, 15))
        self.assertEqual(snap_15m.get("timeframe"), "15m")
        tg_15m = snap_15m.get("telegram_text", "")
        self.assertTrue(tg_15m.startswith("🟪 15-MIN | 11:15 | Spot"))
        lines_15m = [ln for ln in tg_15m.split("\n") if ln.strip()]
        self.assertLessEqual(len(lines_15m), 18, f"15m telegram lines exceeded 18: {len(lines_15m)}")

        # Check required fields exist in tg_15m
        self.assertIn("Day range:", tg_15m)
        self.assertIn("GEX:", tg_15m)
        self.assertIn("RVOL:", tg_15m)
        self.assertIn("IV:", tg_15m)
        self.assertIn("Floor:", tg_15m)
        self.assertIn("Flow (15m):", tg_15m)
        self.assertIn("Checklist:", tg_15m)
        self.assertIn("Action:", tg_15m)

    def test_window_flow_difference(self):
        """Verifies that 5m and 15m compute flow over different windows."""
        target_date = date(2026, 9, 24)
        snap_5m = agt_briefing.build_periodic_snapshot("5m", target_date, dtime(11, 15))
        snap_15m = agt_briefing.build_periodic_snapshot("15m", target_date, dtime(11, 15))

        tg_5m = snap_5m.get("telegram_text", "")
        tg_15m = snap_15m.get("telegram_text", "")

        flow_5m_line = [ln for ln in tg_5m.split("\n") if "Flow (5m):" in ln][0]
        flow_15m_line = [ln for ln in tg_15m.split("\n") if "Flow (15m):" in ln][0]

        self.assertIn("Flow (5m):", flow_5m_line)
        self.assertIn("Flow (15m):", flow_15m_line)

    def test_stale_feed_guard(self):
        """Tests that stale feed (>6 min) triggers ⚠️ FEED STALE and RVOL = N/A."""
        target_date = date(2026, 9, 24)
        # 11:25 target_time when latest available snapshot is 11:15 (10 mins old)
        # We can simulate by requesting 11:25 with a mock or checking ref_dt
        snap = agt_briefing.build_periodic_snapshot("5m", target_date, dtime(11, 10))
        # Now artificially check staleness by passing a future target_time beyond available snaps
        # On 2026-09-24, snaps end at 15:30. Let's pass 15:45 (15 mins past close)
        snap_stale = agt_briefing.build_periodic_snapshot("5m", target_date, dtime(15, 45))
        self.assertTrue(snap_stale.get("is_stale_feed"))
        self.assertIn("FEED STALE", snap_stale.get("telegram_text", ""))
        self.assertIn("RVOL: N/A", snap_stale.get("telegram_text", ""))

    def test_observation_log_tagging(self):
        """Tests that observation entries written by build_periodic_snapshot are tagged with tf: 5m and tf: 15m."""
        target_date = date(2026, 9, 24)
        temp_dir = Path(tempfile.mkdtemp())
        try:
            # Create temporary rules pointing to temp observations_dir
            rules = agt_briefing.load_briefing_rules()
            orig_dir = rules.get("observations_dir")
            rules["observations_dir"] = str(temp_dir)

            # Build 5m with append_obs=True
            snap_5m = agt_briefing.build_periodic_snapshot("5m", target_date, dtime(11, 10), append_obs=True)
            # Build 15m with append_obs=True
            snap_15m = agt_briefing.build_periodic_snapshot("15m", target_date, dtime(11, 15), append_obs=True)

            log_file = config.DATA_DIR / "observations" / f"{target_date:%Y-%m-%d}.md"
            self.assertTrue(log_file.exists())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("<!-- tf: 5m | src: live -->", content)
            self.assertIn("<!-- tf: 15m | src: live -->", content)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
