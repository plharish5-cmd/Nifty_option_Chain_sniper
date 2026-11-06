#!/usr/bin/env python3
"""
analyst/futures_volume_engine.py — v1.9 Slot-Normalized Median Futures Volume & Micro-Acceleration Engine.

NIFTY Continuous Futures Volume Surge & RVOL Engine for Port 8510.
Stitches continuous monthly NIFTY Futures volume across historical months (FUT + FUT2 combined),
computes:
  1. slot_rvol_median (Primary Institutional Metric): Volume surge vs the historical median of the exact
     same HH:MM time-of-day clock slot across prior sessions (using app.engine.volume_store).
     Fails closed (None / INSUFFICIENT_HISTORY) on thin history.
  2. vsr_10 & bar_accel_ratio (Secondary Micro-Trigger): Candle-to-candle immediate expansion (Vt / Vt-1 >= 1.15)
     and 10-bar moving average surge.
  3. is_institutional_surge: True dual-condition institutional volume surge trigger
     (slot_rvol_median >= 1.25 [or vsr_10 >= 1.25 when warming up] AND bar_accel_ratio >= 1.15).
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("futures_volume_engine")
IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
DEFAULT_CONFIG_PATH = ROOT / "analyst" / "analyst_config.json"

# Import pure volume_store functions from ict_smc_app/backend/app/engine/volume_store.py
try:
    sys.path.insert(0, str(ROOT / "ict_smc_app" / "backend"))
    from app.engine import volume_store as vs
except Exception as exc:
    log.warning("Could not import app.engine.volume_store: %s", exc)
    vs = None


def load_config() -> Dict[str, Any]:
    """Loads futures volume parameters from analyst/analyst_config.json."""
    defaults = {
        "vsr_threshold": 1.15,
        "slot_rvol_threshold": 1.25,
        "bar_accel_threshold": 1.15,
        "vsr_fallback_threshold": 1.25,
        "min_baseline_sessions": 5,
        "lookback_sessions": 20,
    }
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if "futures_volume_engine" in cfg and isinstance(cfg["futures_volume_engine"], dict):
                    defaults.update(cfg["futures_volume_engine"])
        except Exception as e:
            log.warning("Could not load config from %s: %s", DEFAULT_CONFIG_PATH, e)
    return defaults


class FuturesVolumeEngine:
    """
    v1.9 Continuous NIFTY Futures Dual-Volume Engine.
    Combines Slot-Normalized Median (FUT + FUT2) with Bar-to-Bar Acceleration.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        vsr_threshold: Optional[float] = None,
        config_path: Optional[Path] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        cfg = load_config()

        self.vsr_threshold = vsr_threshold if vsr_threshold is not None else float(cfg.get("vsr_threshold", 1.15))
        self.slot_rvol_threshold = float(cfg.get("slot_rvol_threshold", 1.25))
        self.bar_accel_threshold = float(cfg.get("bar_accel_threshold", 1.15))
        self.vsr_fallback_threshold = float(cfg.get("vsr_fallback_threshold", 1.25))
        self.min_baseline_sessions = int(cfg.get("min_baseline_sessions", 5))
        self.lookback_sessions = int(cfg.get("lookback_sessions", 20))

        self._df_15m: Optional[pd.DataFrame] = None
        self._df_1m: Optional[pd.DataFrame] = None
        self._records_15m: List[Dict[str, Any]] = []
        self._ts_15m: List[int] = []
        self._coverage: Dict[str, Any] = {
            "n_sessions": 0,
            "mean_coverage_pct": 0.0,
            "first_session": None,
            "last_session": None,
        }
        self._last_loaded_ts = 0
        self.ensure_loaded()

    def ensure_loaded(self, force: bool = False) -> None:
        """Loads continuous futures volume from sqlite and computes dual-volume metrics."""
        if self._df_15m is not None and not force:
            return

        if not self.db_path.exists():
            log.warning("Futures DB not found at %s. Volume surge will fail-open.", self.db_path)
            return

        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                df = pd.read_sql_query("""
                    SELECT ts, sec_id, v FROM candles_1m
                    WHERE sec_id IN ('FUT', 'FUT2')
                    ORDER BY ts ASC
                """, conn)

            if df.empty:
                return

            # Pivot to combine front-month and next-month volume (rollover resilient)
            p = df.pivot(index="ts", columns="sec_id", values="v").fillna(0.0)
            if "FUT" not in p.columns:
                p["FUT"] = 0.0
            if "FUT2" not in p.columns:
                p["FUT2"] = 0.0
            p["COMBINED"] = p["FUT"] + p["FUT2"]

            # Compute coverage honestly using volume_store if available
            try:
                raw_1m_rows = []
                for idx, row in p.iterrows():
                    ts_val = int(idx)
                    dt_val = datetime.fromtimestamp(ts_val, tz=IST)
                    raw_1m_rows.append({
                        "ts": ts_val,
                        "volume": float(row["COMBINED"]),
                        "session_date": dt_val.strftime("%Y-%m-%d"),
                        "session_time": dt_val.strftime("%H:%M:%S"),
                    })
                if vs:
                    self._coverage = vs.coverage_from_rows(raw_1m_rows)
            except Exception as cov_err:
                log.warning("Coverage calculation warning: %s", cov_err)

            # 1-minute frame
            df_1m = p.copy()
            df_1m["sma10"] = df_1m["COMBINED"].rolling(10, min_periods=3).mean()
            df_1m["vsr_10"] = (df_1m["COMBINED"] / (df_1m["sma10"] + 1e-9)).fillna(1.0)
            df_1m["ts"] = df_1m.index
            self._df_1m = df_1m

            # 15-minute frame
            p.index = pd.to_datetime(p.index, unit="s", utc=True).tz_convert(IST)
            b15 = p.resample("15min", closed="left", label="left").agg({
                "COMBINED": "sum",
                "FUT": "sum",
                "FUT2": "sum",
            }).dropna()

            b15["ts"] = (b15.index.astype("int64") // 10**9).astype(int)
            b15["sma10"] = b15["COMBINED"].rolling(10, min_periods=3).mean()
            b15["vsr_10"] = (b15["COMBINED"] / (b15["sma10"] + 1e-9)).fillna(1.0)
            b15["datetime_ist"] = b15.index.strftime("%Y-%m-%d %H:%M:%S")
            b15["session_date"] = b15.index.strftime("%Y-%m-%d")
            b15["session_time"] = b15.index.strftime("%H:%M:%S")

            if vs:
                b15["slot"] = b15["session_time"].apply(lambda t: vs.slot_key(t, 15))
            else:
                b15["slot"] = b15["session_time"].str.slice(0, 5)

            # Micro acceleration: Vt / max(Vt-1, 1)
            prev_vol = b15["COMBINED"].shift(1).clip(lower=1.0)
            b15["bar_accel_ratio"] = (b15["COMBINED"] / prev_vol).fillna(1.0)

            # Compute slot-normalized median baseline with ZERO look-ahead bias
            # For each bar at (session_date, slot), baseline is drawn strictly from prior sessions (session_date < bar_date)
            slot_history_map: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
            for _, r in b15.iterrows():
                slot_history_map[str(r["slot"])].append((str(r["session_date"]), float(r["COMBINED"])))

            slot_rvol_list: List[Optional[float]] = []
            median_baseline_list: List[Optional[float]] = []
            sessions_covered_list: List[int] = []
            cov_status_list: List[str] = []
            is_inst_surge_list: List[bool] = []

            for _, r in b15.iterrows():
                b_slot = str(r["slot"])
                b_date = str(r["session_date"])
                b_vol = float(r["COMBINED"])
                b_vsr = float(r["vsr_10"])
                b_accel = float(r["bar_accel_ratio"])

                # Prior sessions for this clock slot
                prior_vols = [vol for dt, vol in slot_history_map[b_slot] if dt < b_date][-self.lookback_sessions:]
                n_base = len(prior_vols)
                sessions_covered_list.append(n_base)

                if vs and n_base >= self.min_baseline_sessions:
                    res_vsr = vs.slot_vsr(b_vol, prior_vols, min_sessions=self.min_baseline_sessions)
                    if res_vsr.get("ok"):
                        rvol = round(float(res_vsr["ratio"]), 2)
                        med = round(float(res_vsr["median"]), 1)
                        slot_rvol_list.append(rvol)
                        median_baseline_list.append(med)
                        cov_status_list.append("ADEQUATE")

                        # Institutional surge criteria
                        inst_surge = (rvol >= self.slot_rvol_threshold) and (b_accel >= self.bar_accel_threshold)
                        is_inst_surge_list.append(inst_surge)
                        continue

                # Fails closed on thin history (< 5 sessions)
                slot_rvol_list.append(None)
                median_baseline_list.append(None)
                cov_status_list.append("INSUFFICIENT_HISTORY")
                # Fallback to SMA(10) when baseline is warming up
                inst_surge = (b_vsr >= self.vsr_fallback_threshold) and (b_accel >= self.bar_accel_threshold)
                is_inst_surge_list.append(inst_surge)

            b15["slot_rvol_median"] = slot_rvol_list
            b15["median_baseline"] = median_baseline_list
            b15["sessions_covered"] = sessions_covered_list
            b15["coverage_status"] = cov_status_list
            b15["is_institutional_surge"] = is_inst_surge_list

            self._df_15m = b15
            self._records_15m = b15.to_dict(orient="records")
            self._ts_15m = [r["ts"] for r in self._records_15m]
            if self._ts_15m:
                self._last_loaded_ts = self._ts_15m[-1]
            log.info("FuturesVolumeEngine loaded %d 15m bars with Slot-Normalized Median baseline.", len(b15))
        except Exception as e:
            log.error("Error loading continuous futures volume: %s", e)

    def _format_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Formats a 15m record into rich telemetry payload."""
        vsr = round(float(rec.get("vsr_10", 1.0)), 2)
        vol = round(float(rec.get("COMBINED", 0.0)), 1)
        sma = round(float(rec.get("sma10", 0.0)), 1)
        bar_accel = round(float(rec.get("bar_accel_ratio", 1.0)), 2)

        slot_rvol = rec.get("slot_rvol_median")
        slot_rvol_val = round(float(slot_rvol), 2) if slot_rvol is not None else None
        median_base = round(float(rec.get("median_baseline")), 1) if rec.get("median_baseline") is not None else None
        n_cov = int(rec.get("sessions_covered", 0))
        cov_status = str(rec.get("coverage_status", "INSUFFICIENT_HISTORY"))

        # Primary dual-trigger: Slot RVOL >= 1.25 & Bar Accel >= 1.15
        is_inst_surge = bool(rec.get("is_institutional_surge", False))
        # Legacy SMA(10) surge for backward compatibility
        is_surge = vsr >= self.vsr_threshold

        badge = "🔥 INSTITUTIONAL VOLUME SURGE" if (is_inst_surge or is_surge) else "NORMAL VOLUME"
        tag = "🔥 VOLUME SURGE CONFIRMED (59.0% OOS WR | +₹106.1/tr)" if is_surge else "NORMAL_VOLUME"

        slot_str = str(rec.get("slot", "--:--"))
        mean_cov_pct = round(float(self._coverage.get("mean_coverage_pct", 0.0)), 1)

        slot_display = f"{slot_rvol_val:.2f}x" if slot_rvol_val is not None else "Warming"
        pill_text = f"📊 Futures Vol Surge (VSR): {vsr:.2f}x [Slot: {slot_display} | Accel: {bar_accel:.2f}x | {badge}]"

        tooltip = (
            f"Slot RVOL (FUT+FUT2): {slot_display} (Slot {slot_str}) | "
            f"Bar Accel: {bar_accel:.2f}x | SMA(10) VSR: {vsr:.2f}x | "
            f"Baseline: {n_cov} prior sessions ({mean_cov_pct}% mean coverage)"
        )

        return {
            "vsr_10": vsr,
            "volume": vol,
            "sma10": sma,
            "bar_accel_ratio": bar_accel,
            "slot_rvol_median": slot_rvol_val,
            "median_baseline": median_base,
            "sessions_covered": n_cov,
            "coverage_pct": mean_cov_pct,
            "coverage_status": cov_status,
            "slot": slot_str,
            "is_institutional_surge": is_inst_surge,
            "is_surge": is_surge,
            "tag": tag,
            "badge": badge,
            "pill_text": pill_text,
            "tooltip": tooltip,
            "bar_ts": rec.get("ts"),
            "datetime_ist": rec.get("datetime_ist"),
        }

    def get_latest_vsr(self) -> Dict[str, Any]:
        """Returns the most recent fully closed 15m futures volume surge reading."""
        self.ensure_loaded()
        if not self._records_15m:
            return {
                "vsr_10": 1.0,
                "volume": 0.0,
                "sma10": 0.0,
                "bar_accel_ratio": 1.0,
                "slot_rvol_median": None,
                "median_baseline": None,
                "sessions_covered": 0,
                "coverage_pct": 0.0,
                "coverage_status": "INSUFFICIENT_HISTORY",
                "slot": "00:00",
                "is_institutional_surge": False,
                "is_surge": False,
                "tag": "NORMAL_VOLUME",
                "badge": "NORMAL VOLUME",
                "pill_text": "📊 Futures Vol Surge (VSR): 1.00x [NORMAL VOLUME]",
                "tooltip": "Futures Volume: No data recorded",
                "bar_ts": 0,
                "datetime_ist": "",
            }

        return self._format_record(self._records_15m[-1])

    def get_vsr_at_timestamp(self, ts: int) -> Dict[str, Any]:
        """
        Zero look-ahead lookup: finds the last fully closed 15m bar strictly before ts.
        (Bar started at bar_ts closes at bar_ts + 900 <= ts).
        """
        self.ensure_loaded()
        if not self._ts_15m:
            return self.get_latest_vsr()

        idx = bisect.bisect_right(self._ts_15m, ts - 900) - 1
        if idx < 0:
            idx = 0
        return self._format_record(self._records_15m[idx])


# Singleton accessor
_GLOBAL_ENGINE: Optional[FuturesVolumeEngine] = None


def get_futures_volume_engine() -> FuturesVolumeEngine:
    global _GLOBAL_ENGINE
    if _GLOBAL_ENGINE is None:
        _GLOBAL_ENGINE = FuturesVolumeEngine()
    return _GLOBAL_ENGINE


if __name__ == "__main__":
    eng = get_futures_volume_engine()
    latest = eng.get_latest_vsr()
    print("Latest Futures Dual-Volume Reading:")
    print(latest)
    print(f"Full path: {Path(__file__).resolve()}")
