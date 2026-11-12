#!/usr/bin/env python3
"""
analyst/ema_proximity_engine.py

NIFTY 4-EMA & 8-EMA Multi-Timeframe (1m / 15m / 1H) Dynamic Ribbon, Crossover,
Proximity Radar & Support/Resistance Matrix Engine for Port 8510.

Computes minute-by-minute side-by-side:
- 1m Rolling 4 EMA and 8 EMA vs Actual Close Price
- Concurrent 15m and 1H 4/8 EMA Anchors (Zero Look-Ahead Bias)
- 4-8 Spread (Pts & ATR) and Crossover Directional Regimes
- 3-Tier Audio/Visual Proximity Radar (TIER_1_APPROACHING, TIER_2_TOUCH_ZONE, TIER_3_CONFIRMATION_TRIGGER)
- Whip-Saw Chop Suppressor (12-bar flip counter & spread gate)
- Dynamic Support & Resistance Interaction (Stage 2 Bounce, Stage 3 Rejection, 1H S/R)
- Structural Pocket Micro-SL & Opposite Crossover Kill-Switch Advisory
- Post-Touch Forward 5m / 15m MFE & MAE Potential Tracker
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from analyst.nse_spot_candles_8510 import (
    EMA_WARMUP_1M_BARS,
    IST_TZ,
    apply_forming_bar_close,
    attach_bar_timestamps,
    compute_dynamic_emas_on_forming_bar,
    current_nse_15m_open_ts,
    current_nse_1h_open_ts,
    epoch_seconds,
    ema4_ema8,
    ensure_forming_htf_bar,
    forming_ohlc_from_1m_ts,
    load_tradingview_ema_cross_config,
    resample_nse_15m,
    resample_nse_1h,
    resample_spot_from_1m,
)

log = logging.getLogger("ema_proximity_engine")

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "analyst" / "terminal8510_config.json"
DEFAULT_MARKET_DB = ROOT / "ict_smc_app" / "backend" / "data" / "ict_smc.sqlite3"
DEFAULT_REPLAY_DIR = ROOT / "analyst" / "replay_data"

DEFAULT_RADAR_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "audio_alerts_enabled": False,
    "timeframe": "1m",
    "approaching_pts_threshold": 12.0,
    "touch_pts_threshold": 5.0,
    "whipsaw_max_flips_12m": 3,
    "goldilocks_min_atr": 0.13,
    "goldilocks_max_atr": 0.26,
    "auto_highlight_strike_matrix": True,
}


class EmaProximityEngine:
    """
    Modular 4-EMA & 8-EMA Multi-Timeframe Proximity, Crossover & Radar Alert Engine.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        db_path: Optional[Path] = None,
        replay_dir: Optional[Path] = None,
    ):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.db_path = db_path or DEFAULT_MARKET_DB
        self.replay_dir = replay_dir or DEFAULT_REPLAY_DIR
        self._latest_radar_cache: Optional[Dict[str, Any]] = None
        self._live_matrix_cache_key: Optional[Tuple[Any, ...]] = None
        self._live_matrix_cache_payload: Optional[Dict[str, Any]] = None
        self._live_cross_cache: Optional[Tuple[float, float, Dict[str, Any]]] = None
        self._live_1m_df_cache: Optional[Tuple[float, pd.DataFrame]] = None
        self._tv_ema_cfg: Optional[Dict[str, Any]] = None

    def tv_ema_settings(self) -> Dict[str, Any]:
        if self._tv_ema_cfg is None:
            self._tv_ema_cfg = load_tradingview_ema_cross_config(self.config_path)
        return dict(self._tv_ema_cfg)

    def get_config(self) -> Dict[str, Any]:
        """Loads 'ema_proximity_radar' section from terminal8510_config.json or returns default."""
        cfg = dict(DEFAULT_RADAR_CONFIG)
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if "ema_proximity_radar" in data and isinstance(data["ema_proximity_radar"], dict):
                    cfg.update(data["ema_proximity_radar"])
                # Fallback to proven_edges goldilocks if present
                if "proven_edges" in data and isinstance(data["proven_edges"], dict):
                    pe = data["proven_edges"]
                    if "goldilocks_min_atr" in pe:
                        cfg["goldilocks_min_atr"] = float(pe["goldilocks_min_atr"])
                    if "goldilocks_max_atr" in pe:
                        cfg["goldilocks_max_atr"] = float(pe["goldilocks_max_atr"])
            except Exception as e:
                log.warning("Could not load radar config from %s: %s", self.config_path, e)
        return cfg

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Persists updated radar configuration into terminal8510_config.json."""
        current = self.get_config()
        current.update(updates)
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            data["ema_proximity_radar"] = current
            try:
                self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                log.info("Persisted updated ema_proximity_radar config to %s", self.config_path)
            except Exception as e:
                log.error("Failed to write radar config to %s: %s", self.config_path, e)
        return current

    def load_candles_for_date(
        self,
        date_str: str,
        timeframe: str = "1m",
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Loads 1-minute, 15-minute, and 1-hour candles for given date (e.g. '2026-11-05' or 'LIVE').
        Returns (df_1m, df_15m, df_1h) with columns: ts, open, high, low, close, volume.
        """
        target_date = date_str.strip()
        is_live = (target_date.upper() == "LIVE")
        today_iso = datetime.now(IST).strftime("%Y-%m-%d")
        if is_live:
            target_date = today_iso

        records: List[Dict[str, Any]] = []

        # 1. If historical replay date, try reading pre-ingested replay_data/{target_date}.json
        json_file = self.replay_dir / f"{target_date}.json"
        if not is_live and json_file.exists():
            try:
                payload = json.loads(json_file.read_text(encoding="utf-8"))
                if "spot_candles_1m" in payload and isinstance(payload["spot_candles_1m"], list) and len(payload["spot_candles_1m"]) >= 30:
                    records = payload["spot_candles_1m"]
            except Exception as e:
                log.warning("Error reading %s: %s", json_file, e)

        # 2. Fallback to SQLite candles_1m in market_db
        if not records and self.db_path.exists():
            try:
                with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                    cur = conn.cursor()
                    rows = cur.execute("""
                        SELECT ts, o, h, l, c, v
                        FROM candles_1m
                        WHERE sec_id='13'
                          AND date(datetime(ts, 'unixepoch', '+330 minutes')) = ?
                        ORDER BY ts ASC
                    """, (target_date,)).fetchall()
                    if rows:
                        warmup_records: List[Dict[str, Any]] = []
                        if is_live or target_date == today_iso:
                            first_ts = int(rows[0][0])
                            w_rows = cur.execute("""
                                SELECT ts, o, h, l, c, v
                                FROM candles_1m
                                WHERE sec_id='13'
                                  AND ts < ?
                                ORDER BY ts DESC
                                LIMIT ?
                            """, (first_ts, EMA_WARMUP_1M_BARS)).fetchall()
                            for r in sorted(w_rows, key=lambda x: x[0]):
                                warmup_records.append({
                                    "ts": int(r[0]),
                                    "open": float(r[1]),
                                    "high": float(r[2]),
                                    "low": float(r[3]),
                                    "close": float(r[4]),
                                    "volume": float(r[5] or 0.0),
                                })

                        for r in rows:
                            records.append({
                                "ts": int(r[0]),
                                "open": float(r[1]),
                                "high": float(r[2]),
                                "low": float(r[3]),
                                "close": float(r[4]),
                                "volume": float(r[5] or 0.0),
                            })
                        records = warmup_records + records
            except Exception as e:
                log.warning("Error querying candles_1m from %s for %s: %s", self.db_path, target_date, e)

        # 3. If records still empty, fallback to latest available candles
        if not records and self.db_path.exists():
            try:
                with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                    cur = conn.cursor()
                    rows = cur.execute("""
                        SELECT ts, o, h, l, c, v
                        FROM candles_1m
                        WHERE sec_id='13'
                        ORDER BY ts DESC
                        LIMIT 375
                    """).fetchall()
                    if rows:
                        rows = sorted(rows, key=lambda x: x[0])
                        for r in rows:
                            records.append({
                                "ts": int(r[0]),
                                "open": float(r[1]),
                                "high": float(r[2]),
                                "low": float(r[3]),
                                "close": float(r[4]),
                                "volume": float(r[5] or 0.0),
                            })
            except Exception as e:
                log.debug("Live fallback query error: %s", e)

        self.last_load_had_no_data = False
        if not records:
            # v1.9.4c: THE SYNTHETIC FALLBACK IS REMOVED.
            #
            # This block used to manufacture 375 flat candles at 24350.0 "to
            # avoid crashes". The cost of that convenience was severe and
            # silent: on a flat series the 4 MA and 8 EMA sit on top of price
            # with a gap of ~0, so the proximity classifier marked almost every
            # bar a TOUCH and the desk invented Tier 2 / Tier 3 signals for any
            # date with no recorded data. Nothing downstream could tell those
            # apart from real ones, so a CSV export or a backtest spanning
            # unrecorded dates returned fabricated signals that looked like
            # measurements. An empty export is a fact; a flat 24350.0 session is
            # a lie.
            #
            # No data now means no rows. Callers must handle an empty frame;
            # compute_matrix reports status NO_DATA.
            self.last_load_had_no_data = True
            log.warning(
                "No recorded candles for %s (timeframe=%s). Returning EMPTY, not "
                "synthetic candles. Nothing can be computed for this date.",
                target_date, timeframe)
            cols = ["ts", "open", "high", "low", "close", "volume", "datetime"]
            empty = pd.DataFrame(columns=cols)
            return empty, empty.copy(), empty.copy()

        df_1m = pd.DataFrame(records)
        df_1m["datetime"] = pd.to_datetime(df_1m["ts"], unit="s", utc=True).dt.tz_convert(IST)
        df_1m = df_1m.sort_values("ts").reset_index(drop=True)

        df_idx = df_1m.set_index("datetime")
        _, resample_15m, resample_1h = resample_spot_from_1m(df_idx)
        if not resample_15m.empty and "datetime" not in resample_15m.columns:
            resample_15m = resample_15m.reset_index()
        if not resample_1h.empty and "datetime" not in resample_1h.columns:
            resample_1h = resample_1h.reset_index()

        return df_1m, resample_15m, resample_1h

    def compute_matrix(
        self,
        candles_input: Optional[Union[pd.DataFrame, List[Dict[str, Any]], str]] = None,
        date_str: Optional[str] = None,
        timeframe: str = "1m",
        filter_state: str = "ALL",
        live_spot: Optional[float] = None,
        custom_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Main calculation engine. Computes the complete 4-EMA & 8-EMA Proximity Matrix
        with zero look-ahead bias.
        """
        cfg = self.get_config()
        if custom_cfg:
            cfg.update(custom_cfg)
        tv_ema = self.tv_ema_settings()

        approaching_pts = float(cfg.get("approaching_pts_threshold", 12.0))
        touch_pts = float(cfg.get("touch_pts_threshold", 5.0))
        max_flips = int(cfg.get("whipsaw_max_flips_12m", 3))
        goldilocks_min_atr = float(cfg.get("goldilocks_min_atr", 0.13))

        def no_data(date_label: str, tf: str) -> Dict[str, Any]:
            """v1.9.4c: an explicit empty result, so a date with no recorded
            candles yields no rows instead of rows computed from a fabricated
            flat series. An empty table is the correct answer here."""
            return {
                "ok": False,
                "status": "NO_DATA",
                "date": date_label,
                "timeframe": tf,
                "rows": [],
                "matrix": [],
                "radar": [],
                "n_bars": 0,
                "message": ("No recorded candles for {} ({}). Nothing computed. "
                            "This date was never recorded, so there are no "
                            "signals to report -- real or otherwise."
                            .format(date_label, tf)),
            }

        target_date = date_str or "LIVE"
        is_live = (str(target_date).upper() == "LIVE")
        today_iso = datetime.now(IST).strftime("%Y-%m-%d")
        filter_key = str(filter_state).upper().strip()
        tf_key = timeframe.lower().strip()
        if is_live and candles_input is None:
            cache_key = (tf_key, filter_key, int(time.time()) // 2)
            if self._live_matrix_cache_key == cache_key and self._live_matrix_cache_payload is not None:
                return self._live_matrix_cache_payload

        # Determine inputs
        if isinstance(candles_input, str):
            target_date = candles_input
            is_live = (str(target_date).upper() == "LIVE")
            df_1m, df_15m, df_1h = self.load_candles_for_date(target_date, timeframe=timeframe)
        elif isinstance(candles_input, pd.DataFrame):
            df_1m = candles_input.copy()
            if "datetime" not in df_1m.columns and "ts" in df_1m.columns:
                df_1m["datetime"] = pd.to_datetime(df_1m["ts"], unit="s", utc=True).dt.tz_convert(IST)
            # Build resampled 15m and 1h
            _, df_15m, df_1h = resample_spot_from_1m(df_1m.set_index("datetime"))
            if not df_15m.empty and "datetime" not in df_15m.columns:
                df_15m = df_15m.reset_index()
            if not df_1h.empty and "datetime" not in df_1h.columns:
                df_1h = df_1h.reset_index()
        elif isinstance(candles_input, list) and len(candles_input) > 0:
            df_1m = pd.DataFrame(candles_input)
            if "datetime" not in df_1m.columns and "ts" in df_1m.columns:
                df_1m["datetime"] = pd.to_datetime(df_1m["ts"], unit="s", utc=True).dt.tz_convert(IST)
            _, df_15m, df_1h = resample_spot_from_1m(df_1m.set_index("datetime"))
            if not df_15m.empty and "datetime" not in df_15m.columns:
                df_15m = df_15m.reset_index()
            if not df_1h.empty and "datetime" not in df_1h.columns:
                df_1h = df_1h.reset_index()
        else:
            df_1m, df_15m, df_1h = self.load_candles_for_date(target_date, timeframe=timeframe)

        # Apply real-time forming bar if live_spot provided during live market
        # v1.9.4c: bail out BEFORE computing anything on an empty frame.
        if df_1m is None or df_1m.empty:
            return no_data(str(target_date), timeframe)

        if live_spot and float(live_spot) > 0 and not df_1m.empty and (is_live or target_date == today_iso):
            curr_spot = float(live_spot)
            now_dt = datetime.now(IST)
            now_ts = int(now_dt.timestamp())
            curr_bar_ts = (now_ts // 60) * 60
            last_bar_ts = int(df_1m["ts"].iloc[-1])

            if last_bar_ts < curr_bar_ts:
                last_c = float(df_1m["close"].iloc[-1])
                forming_bar = {
                    "ts": curr_bar_ts,
                    "open": last_c,
                    "high": max(last_c, curr_spot),
                    "low": min(last_c, curr_spot),
                    "close": curr_spot,
                    "volume": 0.0,
                    "datetime": pd.to_datetime(curr_bar_ts, unit="s", utc=True).tz_convert(IST),
                }
                df_1m = pd.concat([df_1m, pd.DataFrame([forming_bar])], ignore_index=True)
            elif last_bar_ts == curr_bar_ts:
                df_1m.loc[df_1m.index[-1], "close"] = curr_spot
                df_1m.loc[df_1m.index[-1], "high"] = max(df_1m.loc[df_1m.index[-1], "high"], curr_spot)
                df_1m.loc[df_1m.index[-1], "low"] = min(df_1m.loc[df_1m.index[-1], "low"], curr_spot)

            df_idx = df_1m.set_index("datetime")
            _, df_15m, df_1h = resample_spot_from_1m(df_idx)
            if not df_15m.empty and "datetime" not in df_15m.columns:
                df_15m = df_15m.reset_index()
            if not df_1h.empty and "datetime" not in df_1h.columns:
                df_1h = df_1h.reset_index()

        # Handle active timeframe selection (1m, 5m, 15m, 1h)
        active_tf = timeframe.lower().strip()
        if active_tf == "5m":
            df_idx = df_1m.set_index("datetime")
            df_active = df_idx.resample("5min", closed="left", label="left").agg({
                "ts": "first", "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
            }).dropna().reset_index()
        elif active_tf == "15m":
            df_active = df_15m.copy()
        elif active_tf in ("1h", "60m"):
            df_active = df_1h.copy()
        else:
            df_active = df_1m.copy()

        def _drop_forming(bars: pd.DataFrame, bar_seconds: int) -> pd.DataFrame:
            if bars.empty or "ts" not in bars.columns:
                return bars
            now_ts = int(datetime.now(IST).timestamp())
            out = bars
            while len(out) > 0 and now_ts < int(out["ts"].iloc[-1]) + bar_seconds:
                out = out.iloc[:-1]
            return out

        # Live: drop all forming higher-TF bars so EMAs match TradingView last closed candle.
        if is_live or target_date == today_iso:
            df_15m = _drop_forming(df_15m, 900)
            df_1h = _drop_forming(df_1h, 3600)
            if active_tf in ("15m", "15min"):
                df_active = _drop_forming(df_active, 900)
            elif active_tf in ("1h", "60m"):
                df_active = _drop_forming(df_active, 3600)

        n_bars = len(df_active)
        if n_bars == 0:
            return {
                "ok": False,
                "error": "No candle data available to compute matrix",
                "date": target_date,
                "date_iso": target_date,
                "rows": [],
                "summary": {},
            }

        # Format dates
        if is_live:
            date_iso = today_iso
            formatted_date = datetime.now(IST).strftime("%d %b %Y")
        elif len(str(target_date)) == 10 and str(target_date)[4] == "-":
            date_iso = str(target_date)
            try:
                dt_obj = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=IST)
                formatted_date = dt_obj.strftime("%d %b %Y")
            except Exception:
                formatted_date = date_iso
        else:
            first_ts = int(df_active["ts"].iloc[0])
            dt_obj = datetime.fromtimestamp(first_ts, tz=timezone.utc).astimezone(IST)
            formatted_date = dt_obj.strftime("%d %b %Y")
            date_iso = dt_obj.strftime("%Y-%m-%d")

        # ── 1. Calculate Active Timeframe EMAs (4 and 8) ───────────────────────
        close_series = df_active["close"].astype(float)
        ema_4_series, ema_8_series = ema4_ema8(close_series, tv_ema)

        # True Range and 14-period ATR
        high_s = df_active["high"].astype(float)
        low_s = df_active["low"].astype(float)
        prev_close_s = close_series.shift(1).fillna(close_series.iloc[0])
        tr1 = high_s - low_s
        tr2 = (high_s - prev_close_s).abs()
        tr3 = (low_s - prev_close_s).abs()
        tr_series = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_14_series = tr_series.rolling(14, min_periods=1).mean()

        # ── 2. Calculate 15m and 1H Anchor Series (Zero Look-Ahead) ────────────
        # Compute 15m rolling EMAs
        if not df_15m.empty:
            c15 = df_15m["close"].astype(float)
            df_15m["ema_4"], df_15m["ema_8"] = ema4_ema8(c15, tv_ema)
            if "close_ts" not in df_15m.columns:
                df_15m["close_ts"] = df_15m["ts"] + 900
        else:
            df_15m["ema_4"] = pd.Series(dtype=float)
            df_15m["ema_8"] = pd.Series(dtype=float)
            df_15m["close_ts"] = pd.Series(dtype=int)

        # Compute 1H rolling EMAs
        if not df_1h.empty:
            c1h = df_1h["close"].astype(float)
            df_1h["ema_4"], df_1h["ema_8"] = ema4_ema8(c1h, tv_ema)
            if "close_ts" not in df_1h.columns:
                df_1h["close_ts"] = df_1h["ts"] + 3600
        else:
            df_1h["ema_4"] = pd.Series(dtype=float)
            df_1h["ema_8"] = pd.Series(dtype=float)
            df_1h["close_ts"] = pd.Series(dtype=int)

        # Lookup function for last closed bar strictly before or at bar_ts
        # For a 1m bar at timestamp ts, the last CLOSED 15m bar must have close_ts <= ts
        # (e.g. at 09:30:00, 15m bar from 09:15-09:30 closed at 09:30:00, so it is closed).
        records_15m = sorted(
            (df_15m.to_dict(orient="records") if not df_15m.empty else []),
            key=lambda r: int(r.get("close_ts") or 0),
        )
        records_1h = sorted(
            (df_1h.to_dict(orient="records") if not df_1h.empty else []),
            key=lambda r: int(r.get("close_ts") or 0),
        )

        initial_close = float(close_series.iloc[0])
        if active_tf in ("15m", "15min"):
            active_bar_seconds = 900
        elif active_tf in ("1h", "60m"):
            active_bar_seconds = 3600
        elif active_tf == "5m":
            active_bar_seconds = 300
        else:
            active_bar_seconds = 60

        def get_anchor_15m(asof_ts: int) -> Tuple[float, float]:
            # Latest 15m bar fully closed at or before asof_ts (bar end, not open).
            valid = [r for r in records_15m if int(r["close_ts"]) <= asof_ts]
            if valid:
                last_r = valid[-1]
                return float(last_r["ema_4"]), float(last_r["ema_8"])
            elif records_15m:
                # First 15m bar not closed yet: use its developing EMA or initial close
                return float(records_15m[0]["ema_4"]), float(records_15m[0]["ema_8"])
            return initial_close, initial_close

        def get_anchor_1h(asof_ts: int) -> Tuple[float, float]:
            valid = [r for r in records_1h if int(r["close_ts"]) <= asof_ts]
            if valid:
                last_r = valid[-1]
                return float(last_r["ema_4"]), float(last_r["ema_8"])
            elif records_1h:
                return float(records_1h[0]["ema_4"]), float(records_1h[0]["ema_8"])
            return initial_close, initial_close

        # ── 3. Build Minute-by-Minute Table Rows (Numpy-Vectorized) ───────────
        ts_arr = df_active["ts"].to_numpy(dtype=np.int64)
        open_arr = df_active["open"].to_numpy(dtype=np.float64)
        high_arr = df_active["high"].to_numpy(dtype=np.float64)
        low_arr = df_active["low"].to_numpy(dtype=np.float64)
        close_arr = df_active["close"].to_numpy(dtype=np.float64)
        vol_arr = df_active["volume"].to_numpy(dtype=np.float64) if "volume" in df_active.columns else np.zeros(n_bars, dtype=np.float64)
        ema_4_arr = ema_4_series.to_numpy(dtype=np.float64)
        ema_8_arr = ema_8_series.to_numpy(dtype=np.float64)
        atr_14_arr = atr_14_series.to_numpy(dtype=np.float64)

        all_computed_rows: List[Dict[str, Any]] = []
        flips_window: List[int] = []  # signs (+1 or -1) of (close - ema_8)

        for t in range(n_bars):
            ts_val = int(ts_arr[t])
            dt_bar = datetime.fromtimestamp(ts_val, tz=timezone.utc).astimezone(IST)
            timestamp_ist = dt_bar.strftime("%H:%M:%S")
            row_date_iso = dt_bar.strftime("%Y-%m-%d")
            row_date_str = dt_bar.strftime("%d %b %Y")

            open_p = round(float(open_arr[t]), 2)
            high_p = round(float(high_arr[t]), 2)
            low_p = round(float(low_arr[t]), 2)
            close_p = round(float(close_arr[t]), 2)
            vol_val = float(vol_arr[t])

            ema_4 = round(float(ema_4_arr[t]), 2)
            ema_8 = round(float(ema_8_arr[t]), 2)
            atr_14 = max(0.01, round(float(atr_14_arr[t]), 2))

            gap_close_to_4ema = round(close_p - ema_4, 2)
            gap_close_to_8ema = round(close_p - ema_8, 2)
            gap_pts = gap_close_to_8ema
            abs_gap_pts = round(abs(gap_pts), 2)
            gap_atr_ratio = round(abs_gap_pts / atr_14, 3)

            spread_4_minus_8 = round(ema_4 - ema_8, 2)
            spread_4_8_atr = round(abs(spread_4_minus_8) / atr_14, 3)

            wick_touched_ema8 = bool(low_p <= ema_8 <= high_p)
            wick_touched_ema4 = bool(low_p <= ema_4 <= high_p)

            # Higher-TF anchors: evaluate at this row's candle *close* so 15m/1H
            # columns match Dhan/TradingView on the same closed bar (not one bar early).
            asof_ts = int(ts_val) + active_bar_seconds
            if active_tf in ("15m", "15min"):
                ema_4_15m, ema_8_15m = ema_4, ema_8
            else:
                ema_4_15m, ema_8_15m = get_anchor_15m(asof_ts)
            ema_4_15m = round(ema_4_15m, 2)
            ema_8_15m = round(ema_8_15m, 2)
            spread_15m = round(ema_4_15m - ema_8_15m, 2)

            if active_tf in ("1h", "60m"):
                ema_4_1h, ema_8_1h = ema_4, ema_8
            else:
                ema_4_1h, ema_8_1h = get_anchor_1h(asof_ts)
            ema_4_1h = round(ema_4_1h, 2)
            ema_8_1h = round(ema_8_1h, 2)
            spread_1h = round(ema_4_1h - ema_8_1h, 2)

            # Trailing 12-bar flip counter
            current_sign = 1 if (close_p - ema_8) >= 0 else -1
            flips_window.append(current_sign)
            if len(flips_window) > 12:
                flips_window.pop(0)

            flips_count = 0
            for i in range(1, len(flips_window)):
                if flips_window[i] != flips_window[i - 1]:
                    flips_count += 1

            is_whipsaw_chop = bool(flips_count >= max_flips or spread_4_8_atr < goldilocks_min_atr)

            # Crossover & Reversal State
            crossover_state = "NEUTRAL"
            if t > 0:
                prev_ema4 = float(ema_4_arr[t - 1])
                prev_ema8 = float(ema_8_arr[t - 1])
                prev_spread = prev_ema4 - prev_ema8

                if prev_ema4 <= prev_ema8 and ema_4 > ema_8 and close_p > ema_8:
                    crossover_state = "8_EMA_REVERSAL_UPSIDE_CROSS"
                elif prev_ema4 >= prev_ema8 and ema_4 < ema_8 and close_p < ema_8:
                    crossover_state = "8_EMA_REVERSAL_DOWNSIDE_CROSS"
                elif ema_4 < ema_8 and abs(spread_4_minus_8) <= 3.0:
                    if t >= 2:
                        prev2_spread = float(ema_4_arr[t - 2]) - float(ema_8_arr[t - 2])
                        if spread_4_minus_8 > prev_spread > prev2_spread:
                            crossover_state = "PRE_CROSS_CONVERGING_UP"
                elif ema_4 > ema_8 and spread_4_minus_8 <= 3.0:
                    if t >= 2:
                        prev2_spread = float(ema_4_arr[t - 2]) - float(ema_8_arr[t - 2])
                        if spread_4_minus_8 < prev_spread < prev2_spread:
                            crossover_state = "PRE_CROSS_CONVERGING_DOWN"

            if crossover_state == "NEUTRAL":
                if is_whipsaw_chop:
                    crossover_state = "ENTANGLED_CONSOLIDATION"
                elif ema_4 > ema_8 and spread_4_8_atr >= goldilocks_min_atr:
                    crossover_state = "BULLISH_SEPARATION_STRONG"
                elif ema_4 < ema_8 and spread_4_8_atr >= goldilocks_min_atr:
                    crossover_state = "BEARISH_SEPARATION_STRONG"

            # Support & Resistance Interaction
            sr_interaction = "NONE"
            # 1. 1H S/R interaction check
            dist_1h = abs(close_p - ema_8_1h)
            if dist_1h <= 8.0:
                if ema_4_1h >= ema_8_1h and close_p >= ema_8_1h and low_p <= ema_8_1h + 8.0:
                    sr_interaction = "1H_8EMA_SUPPORT_TOUCH"
                elif ema_4_1h <= ema_8_1h and close_p <= ema_8_1h and high_p >= ema_8_1h - 8.0:
                    sr_interaction = "1H_8EMA_RESISTANCE_REJECT"

            # 2. Stage 2 Support Bounce (Long) vs Stage 3 Resistance Reject (Short)
            if sr_interaction == "NONE":
                if ema_4 > ema_8:
                    # In bullish ribbon pocket [ema_8, ema_4]
                    if (low_p <= ema_4 or abs(low_p - ema_8) <= touch_pts or low_p <= ema_8) and close_p > ema_8:
                        sr_interaction = "STAGE_2_8EMA_SUPPORT_BOUNCE"
                elif ema_4 < ema_8:
                    # In bearish ribbon pocket [ema_4, ema_8]
                    if (high_p >= ema_4 or abs(high_p - ema_8) <= touch_pts or high_p >= ema_8) and close_p < ema_8:
                        sr_interaction = "STAGE_3_8EMA_RESISTANCE_REJECT"

            # 3-Tier Proximity Radar State Machine
            proximity_state = "ZONE_FAR"
            if is_whipsaw_chop:
                if abs_gap_pts <= approaching_pts:
                    proximity_state = "WHIPSAW_CHOP_BLOCKED"
                else:
                    proximity_state = "ZONE_FAR"
            else:
                # Check for Tier 3: Confirmation Trigger
                is_clean_trigger = (
                    sr_interaction in (
                        "STAGE_2_8EMA_SUPPORT_BOUNCE",
                        "STAGE_3_8EMA_RESISTANCE_REJECT",
                        "1H_8EMA_SUPPORT_TOUCH",
                        "1H_8EMA_RESISTANCE_REJECT",
                    )
                    or crossover_state in (
                        "8_EMA_REVERSAL_UPSIDE_CROSS",
                        "8_EMA_REVERSAL_DOWNSIDE_CROSS",
                    )
                    or (
                        (wick_touched_ema8 or abs_gap_pts <= 3.0 or gap_atr_ratio <= 0.15)
                        and (
                            (ema_4 >= ema_8 and close_p > ema_8)
                            or (ema_4 < ema_8 and close_p < ema_8)
                        )
                    )
                )

                if is_clean_trigger:
                    proximity_state = "TIER_3_CONFIRMATION_TRIGGER"
                elif (
                    wick_touched_ema8
                    or abs_gap_pts <= touch_pts
                    or gap_atr_ratio <= 0.25
                    or (min(ema_4, ema_8) <= close_p <= max(ema_4, ema_8))
                ):
                    proximity_state = "TIER_2_TOUCH_ZONE"
                elif abs_gap_pts <= approaching_pts:
                    # Check if distance is shrinking
                    prev_abs_gap = abs(float(close_arr[t - 1]) - float(ema_8_arr[t - 1])) if t > 0 else abs_gap_pts
                    if abs_gap_pts <= prev_abs_gap:
                        proximity_state = "TIER_1_APPROACHING"
                    else:
                        proximity_state = "TIER_1_APPROACHING"
                else:
                    proximity_state = "ZONE_FAR"

            # Stop-Loss Minimizer & Early Rescue Advisory
            # Additive observation metadata: use the existing structural-stop formula
            # even when the displayed guard is a crossover exit warning. No execution
            # or original text/state calculation consumes these new fields.
            structural_sl_spot = round(min(low_p, ema_8) - 0.20 * atr_14, 2) if ema_4 >= ema_8 else round(max(high_p, ema_8) + 0.20 * atr_14, 2)
            risk_pts = round(abs(close_p - structural_sl_spot), 2)
            sl_guard_advice = "--"
            if crossover_state == "8_EMA_REVERSAL_DOWNSIDE_CROSS" or (t > 0 and float(ema_4_arr[t - 1]) >= float(ema_8_arr[t - 1]) and ema_4 < ema_8):
                sl_guard_advice = "⚠️ LONG KILL-SWITCH WARNING (4 EMA crossed below 8 EMA) | Exit Long Immediately"
            elif crossover_state == "8_EMA_REVERSAL_UPSIDE_CROSS" or (t > 0 and float(ema_4_arr[t - 1]) <= float(ema_8_arr[t - 1]) and ema_4 > ema_8):
                sl_guard_advice = "⚠️ SHORT KILL-SWITCH WARNING (4 EMA crossed above 8 EMA) | Exit Short Immediately"
            elif ema_4 >= ema_8:
                structural_sl_spot = round(min(low_p, ema_8) - 0.20 * atr_14, 2)
                risk_pts = round(close_p - structural_sl_spot, 2)
                sl_guard_advice = f"🟢 CE Micro-SL: {structural_sl_spot} (Risk: {risk_pts} pts)"
            else:
                structural_sl_spot = round(max(high_p, ema_8) + 0.20 * atr_14, 2)
                risk_pts = round(structural_sl_spot - close_p, 2)
                sl_guard_advice = f"🔴 PE Micro-SL: {structural_sl_spot} (Risk: {risk_pts} pts)"

            # Action / Alert Recommendation
            if proximity_state == "WHIPSAW_CHOP_BLOCKED":
                action_recommendation = f"🚫 CHOP SUPPRESSED ({flips_count} flips in 12m) — Do Not Trade"
            elif crossover_state == "8_EMA_REVERSAL_UPSIDE_CROSS":
                action_recommendation = "🔥 8-EMA REVERSAL UPSIDE CROSS (CE BUY) | Strong Momentum Shift"
            elif crossover_state == "8_EMA_REVERSAL_DOWNSIDE_CROSS":
                action_recommendation = "🔥 8-EMA REVERSAL DOWNSIDE CROSS (PE BUY) | Strong Momentum Shift"
            elif sr_interaction == "STAGE_2_8EMA_SUPPORT_BOUNCE":
                action_recommendation = "🚀 HIGH-CONVICTION 8-EMA BOUNCE (CE LONG) | Stage 2 Support Protected"
            elif sr_interaction == "STAGE_3_8EMA_RESISTANCE_REJECT":
                action_recommendation = "📉 HIGH-CONVICTION 8-EMA REJECTION (PE SHORT) | Stage 3 Resistance Defended"
            elif sr_interaction == "1H_8EMA_SUPPORT_TOUCH":
                action_recommendation = "🛡️ 1H 8-EMA INSTITUTIONAL SUPPORT TOUCH — High R:R Rebound Zone"
            elif sr_interaction == "1H_8EMA_RESISTANCE_REJECT":
                action_recommendation = "🧱 1H 8-EMA RESISTANCE CEILING TEST — Guard Overhead Rejection"
            elif proximity_state == "TIER_2_TOUCH_ZONE":
                action_recommendation = f"🟠 IN 4/8-EMA POCKET ZONE (Gap: {gap_pts:+.2f} pts) — Watch for Bounce/Break"
            elif proximity_state == "TIER_1_APPROACHING":
                action_recommendation = f"🟡 APPROACHING 4/8-EMA (Gap: {gap_pts:+.2f} pts) — Prepare Order Router"
            else:
                action_recommendation = f"⚪ TRENDING OUTSIDE ZONE (Gap: {gap_pts:+.2f} pts)"

            # Forward 5m / 15m MFE and MAE (Numpy slice)
            fwd_mfe_5m: Optional[float] = None
            fwd_mfe_15m: Optional[float] = None
            fwd_mae_15m: Optional[float] = None

            is_bullish = (ema_4 >= ema_8)
            if t + 1 < n_bars:
                end_5 = min(t + 1 + 5, n_bars)
                end_15 = min(t + 1 + 15, n_bars)

                h5_max = float(high_arr[t + 1 : end_5].max())
                l5_min = float(low_arr[t + 1 : end_5].min())
                h15_max = float(high_arr[t + 1 : end_15].max())
                l15_min = float(low_arr[t + 1 : end_15].min())

                if is_bullish:
                    fwd_mfe_5m = round(max(0.0, h5_max - close_p), 2)
                    fwd_mfe_15m = round(max(0.0, h15_max - close_p), 2)
                    fwd_mae_15m = round(max(0.0, close_p - l15_min), 2)
                else:
                    fwd_mfe_5m = round(max(0.0, close_p - l5_min), 2)
                    fwd_mfe_15m = round(max(0.0, close_p - l15_min), 2)
                    fwd_mae_15m = round(max(0.0, h15_max - close_p), 2)

            row_obj = {
                "date": row_date_str,
                "date_iso": row_date_iso,
                "timestamp_ist": timestamp_ist,
                "ts": ts_val,
                "open_price": open_p,
                "high_price": high_p,
                "low_price": low_p,
                "close_price": close_p,
                "volume": vol_val,
                "ema_4": ema_4,
                "ema_8": ema_8,
                "gap_close_to_4ema": gap_close_to_4ema,
                "gap_close_to_8ema": gap_close_to_8ema,
                "spread_4_minus_8": spread_4_minus_8,
                "spread_4_8_atr": spread_4_8_atr,
                "gap_pts": gap_pts,
                "abs_gap_pts": abs_gap_pts,
                "atr_14": atr_14,
                "gap_atr_ratio": gap_atr_ratio,
                "wick_touched_ema8": wick_touched_ema8,
                "wick_touched_ema4": wick_touched_ema4,
                "flips_last_12_bars": flips_count,
                "is_whipsaw_chop": is_whipsaw_chop,
                "ema_4_15m": ema_4_15m,
                "ema_8_15m": ema_8_15m,
                "spread_15m": spread_15m,
                "ema_4_1h": ema_4_1h,
                "ema_8_1h": ema_8_1h,
                "spread_1h": spread_1h,
                "crossover_state": crossover_state,
                "sr_interaction": sr_interaction,
                "proximity_state": proximity_state,
                "action_recommendation": action_recommendation,
                "sl_guard_advice": sl_guard_advice,
                "micro_sl_price": structural_sl_spot,
                "risk_points": risk_pts,
                "forward_mfe_5m": fwd_mfe_5m,
                "forward_mfe_15m": fwd_mfe_15m,
                "forward_mae_15m": fwd_mae_15m,
            }
            all_computed_rows.append(row_obj)

        # Check if rows should be filtered to target session date (excluding warmup bars)
        target_rows = [r for r in all_computed_rows if r["date_iso"] == date_iso]
        # If matching rows exist, use them as session rows; otherwise use all rows
        session_rows = target_rows if target_rows else all_computed_rows

        # Summary KPIs (calculated strictly over session rows)
        upside_crossovers = 0
        downside_crossovers = 0
        stage2_support_bounces = 0
        stage3_resistance_rejects = 0
        h1_sr_touches = 0
        touches_8ema = 0
        approaching_alerts = 0
        whipsaw_blocks = 0
        clean_triggers = 0
        clean_bounce_wins = 0  # 15m MFE >= 15 pts
        clean_mfe_sum = 0.0

        for r in session_rows:
            if r["crossover_state"] == "8_EMA_REVERSAL_UPSIDE_CROSS":
                upside_crossovers += 1
            elif r["crossover_state"] == "8_EMA_REVERSAL_DOWNSIDE_CROSS":
                downside_crossovers += 1

            if r["sr_interaction"] == "STAGE_2_8EMA_SUPPORT_BOUNCE":
                stage2_support_bounces += 1
            elif r["sr_interaction"] == "STAGE_3_8EMA_RESISTANCE_REJECT":
                stage3_resistance_rejects += 1
            elif "1H" in r["sr_interaction"]:
                h1_sr_touches += 1

            if r["proximity_state"] == "TIER_3_CONFIRMATION_TRIGGER":
                clean_triggers += 1
                touches_8ema += 1
            elif r["proximity_state"] == "TIER_2_TOUCH_ZONE":
                touches_8ema += 1
            elif r["proximity_state"] == "TIER_1_APPROACHING":
                approaching_alerts += 1
            elif r["proximity_state"] == "WHIPSAW_CHOP_BLOCKED":
                whipsaw_blocks += 1

            if r["proximity_state"] in ("TIER_3_CONFIRMATION_TRIGGER", "TIER_2_TOUCH_ZONE") and r["forward_mfe_15m"] is not None:
                if r["forward_mfe_15m"] >= 15.0:
                    clean_bounce_wins += 1
                clean_mfe_sum += r["forward_mfe_15m"]

        win_rate = round((clean_bounce_wins / max(1, touches_8ema)) * 100, 1)
        avg_mfe = round(clean_mfe_sum / max(1, touches_8ema), 2)
        rows = session_rows

        summary = {
            "total_bars": len(session_rows),
            "upside_crossovers": upside_crossovers,
            "downside_crossovers": downside_crossovers,
            "stage2_support_bounces": stage2_support_bounces,
            "stage3_resistance_rejects": stage3_resistance_rejects,
            "h1_sr_touches": h1_sr_touches,
            "touches_8ema": touches_8ema,
            "approaching_alerts": approaching_alerts,
            "whipsaw_blocks": whipsaw_blocks,
            "clean_triggers": clean_triggers,
            "clean_bounce_wins": clean_bounce_wins,
            "clean_trigger_win_rate_pct": win_rate,
            "avg_mfe_15m_pts": avg_mfe,
            "active_timeframe": active_tf,
            "tradingview_ema_cross": tv_ema,
        }

        # Filtering support
        filtered_rows = rows
        f_upper = filter_state.upper().strip()
        if f_upper == "CROSSOVERS":
            filtered_rows = [r for r in rows if "CROSS" in r["crossover_state"]]
        elif f_upper in ("CLEAN_BOUNCES", "CLEAN_TRIGGERS"):
            filtered_rows = [r for r in rows if r["proximity_state"] == "TIER_3_CONFIRMATION_TRIGGER"]
        elif f_upper in ("NEAR_TOUCHES", "TOUCHES"):
            filtered_rows = [r for r in rows if r["proximity_state"] in ("TIER_2_TOUCH_ZONE", "TIER_3_CONFIRMATION_TRIGGER", "TIER_1_APPROACHING")]
        elif f_upper in ("WHIPSAW_BLOCKED", "CHOP"):
            filtered_rows = [r for r in rows if r["proximity_state"] == "WHIPSAW_CHOP_BLOCKED"]
        elif f_upper in ("H1_TOUCHES", "H1_SR"):
            filtered_rows = [r for r in rows if "1H" in r["sr_interaction"]]
        elif f_upper in ("STAGE2_STAGE3", "STAGE2", "STAGE3"):
            filtered_rows = [r for r in rows if "STAGE" in r["sr_interaction"]]

        latest_row = rows[-1] if rows else {}
        self._latest_radar_cache = latest_row
        if not df_1m.empty:
            last_1m_ts = int(df_1m["ts"].iloc[-1])
            summary["spot_1m_last_ist"] = datetime.fromtimestamp(last_1m_ts, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")
        if latest_row.get("timestamp_ist"):
            summary["last_closed_bar_ist"] = latest_row.get("timestamp_ist")
            summary["last_closed_bar_label"] = (
                f"Last closed {active_tf} bar {latest_row.get('timestamp_ist')} "
                f"(1m feed {summary.get('spot_1m_last_ist', '—')})"
            )

        result = {
            "ok": True,
            "date": formatted_date,
            "date_iso": date_iso,
            "timeframe": active_tf,
            "filter_state": filter_state,
            "summary": summary,
            "rows": filtered_rows,
            "latest": latest_row,
        }
        if is_live and candles_input is None:
            self._live_matrix_cache_key = (tf_key, filter_key, int(time.time()) // 2)
            self._live_matrix_cache_payload = result
        return result

    def _live_1m_dataframe(self) -> pd.DataFrame:
        """Reuse today's 1m+warmup ~45s — forming tick only patches OHLC."""
        now = time.time()
        if self._live_1m_df_cache and (now - self._live_1m_df_cache[0]) < 45:
            return self._live_1m_df_cache[1].copy()
        df_1m, _, _ = self.load_candles_for_date("LIVE", timeframe="15m")
        if df_1m is None or df_1m.empty:
            return pd.DataFrame()
        self._live_1m_df_cache = (now, df_1m.copy())
        return df_1m.copy()

    def get_dhan_live_ema_cross(self, live_spot: Optional[float] = None) -> Dict[str, Any]:
        """
        Dhan-style 4/8 EMA on NIFTY spot (sec 13): 15m and 1H include the *forming*
        bar so values move every tick like the broker chart. Algos still use closed bars.
        """
        spot = float(live_spot or 0.0)
        if spot <= 0:
            return {"ok": False, "error": "no live spot"}

        now = time.time()
        cache = self._live_cross_cache
        if cache and (now - cache[0]) < 0.25 and abs(cache[1] - spot) < 0.01:
            return cache[2]

        df_1m = self._live_1m_dataframe()
        if df_1m is None or df_1m.empty:
            return {"ok": False, "error": "no 1m candles"}

        now_dt = datetime.now(IST_TZ)
        now_ts = int(now_dt.timestamp())
        curr_bar_ts = (now_ts // 60) * 60
        last_bar_ts = int(df_1m["ts"].iloc[-1])

        if last_bar_ts < curr_bar_ts:
            last_c = float(df_1m["close"].iloc[-1])
            forming_bar = {
                "ts": curr_bar_ts,
                "open": last_c,
                "high": max(last_c, spot),
                "low": min(last_c, spot),
                "close": spot,
                "volume": 0.0,
                "datetime": pd.to_datetime(curr_bar_ts, unit="s", utc=True).tz_convert(IST),
            }
            df_1m = pd.concat([df_1m, pd.DataFrame([forming_bar])], ignore_index=True)
        elif last_bar_ts == curr_bar_ts:
            df_1m.loc[df_1m.index[-1], "close"] = spot
            df_1m.loc[df_1m.index[-1], "high"] = max(float(df_1m.loc[df_1m.index[-1], "high"]), spot)
            df_1m.loc[df_1m.index[-1], "low"] = min(float(df_1m.loc[df_1m.index[-1], "low"]), spot)

        df_idx = df_1m.set_index("datetime")
        b15_raw = resample_nse_15m(df_idx)
        b1h_raw = resample_nse_1h(df_idx)
        tv_ema = self.tv_ema_settings()
        open_15_ts = current_nse_15m_open_ts(now_dt)
        open_1h_ts = current_nse_1h_open_ts(now_dt)

        def _drop_forming_bar(bars: pd.DataFrame, bar_seconds: int) -> pd.DataFrame:
            if bars is None or bars.empty or "ts" not in bars.columns:
                return bars
            now_ts = int(now_dt.timestamp())
            out = bars
            while len(out) > 0 and now_ts < int(out["ts"].iloc[-1]) + bar_seconds:
                out = out.iloc[:-1]
            return out

        def _tf_block_closed(bars: pd.DataFrame, bar_seconds: int, label: str) -> Dict[str, Any]:
            if bars is None or bars.empty:
                return {"ok": False, "timeframe": label}
            prep = _prep(bars)
            close = prep["close"].astype(float)
            e4_s, e8_s = ema4_ema8(close, tv_ema)
            i = -1
            e4 = round(float(e4_s.iloc[i]), 2)
            e8 = round(float(e8_s.iloc[i]), 2)
            cl = round(float(close.iloc[i]), 2)
            spread = round(e4 - e8, 2)
            gap8 = round(spot - e8, 2)
            stack = "BULLISH" if e4 > e8 else ("BEARISH" if e4 < e8 else "FLAT")
            cross = "NONE"
            if len(close) >= 2:
                pe4, pe8 = float(e4_s.iloc[-2]), float(e8_s.iloc[-2])
                if pe4 <= pe8 and e4 > e8:
                    cross = "4_CROSS_ABOVE_8"
                elif pe4 >= pe8 and e4 < e8:
                    cross = "4_CROSS_BELOW_8"
            open_ts = int(prep["ts"].iloc[i]) if "ts" in prep.columns else 0
            bar_open = (
                datetime.fromtimestamp(open_ts, tz=timezone.utc).astimezone(IST).strftime("%H:%M")
                if open_ts
                else "—"
            )
            return {
                "ok": True,
                "timeframe": label,
                "bar_open_ist": bar_open,
                "forming": False,
                "close": cl,
                "ema_4": e4,
                "ema_8": e8,
                "spread_4_minus_8": spread,
                "gap_spot_to_8": gap8,
                "stack": stack,
                "cross_event": cross,
            }

        def _tf_block_live(
            bars_indexed: pd.DataFrame,
            bar_seconds: int,
            bar_open_ts: int,
            label: str,
        ) -> Dict[str, Any]:
            if bars_indexed is None or bars_indexed.empty:
                return {"ok": False, "timeframe": label}
            work = ensure_forming_htf_bar(bars_indexed, bar_open_ts=bar_open_ts, live_spot=spot)
            fh, fl, _ = forming_ohlc_from_1m_ts(df_1m, bar_open_ts=bar_open_ts, tick_spot=spot)
            e4_v, e8_v, _, _ = compute_dynamic_emas_on_forming_bar(
                work,
                bar_ci=-1,
                live_spot=spot,
                form_high=fh,
                form_low=fl,
                tv_cfg=tv_ema,
            )
            e4 = round(e4_v, 2)
            e8 = round(e8_v, 2)
            cl = round(spot, 2)
            spread = round(e4 - e8, 2)
            gap8 = round(spot - e8, 2)
            stack = "BULLISH" if e4 > e8 else ("BEARISH" if e4 < e8 else "FLAT")
            cross = "NONE"
            patched = apply_forming_bar_close(
                work, bar_ci=-1, live_spot=spot, form_high=fh, form_low=fl
            )
            e4_s, e8_s = ema4_ema8(patched["close"], tv_ema)
            if len(e4_s) >= 2:
                pe4, pe8 = float(e4_s.iloc[-2]), float(e8_s.iloc[-2])
                if pe4 <= pe8 and e4 > e8:
                    cross = "4_CROSS_ABOVE_8"
                elif pe4 >= pe8 and e4 < e8:
                    cross = "4_CROSS_BELOW_8"
            bar_open = datetime.fromtimestamp(int(bar_open_ts), tz=timezone.utc).astimezone(IST).strftime("%H:%M")
            forming = bar_open_ts <= now_ts < bar_open_ts + bar_seconds
            return {
                "ok": True,
                "timeframe": label,
                "bar_open_ist": bar_open,
                "forming": forming,
                "dynamic_tick": True,
                "close": cl,
                "ema_4": e4,
                "ema_8": e8,
                "spread_4_minus_8": spread,
                "gap_spot_to_8": gap8,
                "stack": stack,
                "cross_event": cross,
            }

        def _prep(bars: pd.DataFrame) -> pd.DataFrame:
            if bars is None or bars.empty:
                return bars
            out = bars.reset_index(drop=False) if "datetime" in bars.columns else bars.copy()
            if "ts" not in out.columns and "datetime" in out.columns:
                out["ts"] = epoch_seconds(out["datetime"])
            return out

        df_15m = attach_bar_timestamps(b15_raw, 900) if not b15_raw.empty else b15_raw
        df_1h = attach_bar_timestamps(b1h_raw, 3600) if not b1h_raw.empty else b1h_raw
        closed_15 = _drop_forming_bar(_prep(df_15m), 900)
        closed_1h = _drop_forming_bar(_prep(df_1h), 3600)

        last_1m_ts = int(df_1m["ts"].iloc[-1])
        payload = {
            "ok": True,
            "instrument": "NIFTY_SPOT",
            "sec_id": "13",
            "spot_live": round(spot, 2),
            "spot_1m_last_ist": datetime.fromtimestamp(last_1m_ts, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S"),
            "session_anchor": "09:15",
            "tradingview_ema_cross": tv_ema,
            "mode": "dynamic_forming_bar",
            "refresh_seconds": 3,
            "note": (
                "TradingView MACD+EMA Cross: Fast 4 / Slow 8 on Close, 09:15 NIFTY spot. "
                "tf_15m / tf_1h = live spot on forming bar (prototype-style); tf_*_closed = last closed bar."
            ),
            "tf_15m": _tf_block_live(b15_raw, 900, open_15_ts, "15m"),
            "tf_1h": _tf_block_live(b1h_raw, 3600, open_1h_ts, "1h"),
            "tf_15m_closed": _tf_block_closed(closed_15, 900, "15m_closed"),
            "tf_1h_closed": _tf_block_closed(closed_1h, 3600, "1h_closed"),
            "updated_at_ist": now_dt.strftime("%H:%M:%S"),
        }
        self._live_cross_cache = (now, spot, payload)
        return payload

    def get_latest_radar_status(self, live_spot: Optional[float] = None) -> Dict[str, Any]:
        """
        Returns fast radar pill from the last matrix compute (telemetry must not
        re-run compute_matrix — that blocks the HTTP event loop).
        """
        r: Optional[Dict[str, Any]] = None
        cached_payload = self._live_matrix_cache_payload
        if isinstance(cached_payload, dict) and cached_payload.get("latest"):
            r = cached_payload["latest"]
        elif self._latest_radar_cache:
            r = self._latest_radar_cache

        if not r:
            return {
                "ok": False,
                "tier": "ZONE_FAR",
                "pill_text": "🎯 4/8 EMA Radar: -- [WAITING]",
                "proximity_state": "ZONE_FAR",
            }

        tier = r.get("proximity_state", "ZONE_FAR")
        close_p = r.get("close_price", 0.0)
        ema_4 = r.get("ema_4", 0.0)
        ema_8 = r.get("ema_8", 0.0)
        spread = r.get("spread_4_minus_8", 0.0)
        ema_8_1h = r.get("ema_8_1h", 0.0)
        spread_tag = "BULLISH" if spread >= 0 else "BEARISH"

        badge_icon = "⚪"
        tier_short = "ZONE FAR"
        if tier == "TIER_3_CONFIRMATION_TRIGGER":
            badge_icon = "🟢🔥"
            tier_short = "TIER 3: CONFIRMATION TRIGGER"
        elif tier == "TIER_2_TOUCH_ZONE":
            badge_icon = "🟠⚡"
            tier_short = "TIER 2: POCKET TOUCH"
        elif tier == "TIER_1_APPROACHING":
            badge_icon = "🟡🔔"
            tier_short = "TIER 1: APPROACHING"
        elif tier == "WHIPSAW_CHOP_BLOCKED":
            badge_icon = "🔴🚫"
            tier_short = "WHIPSAW CHOP BLOCKED"

        pill_text = (
            f"🎯 4/8 EMA Radar: Close {close_p:,.1f} | 4-EMA: {ema_4:,.1f} | 8-EMA: {ema_8:,.1f} "
            f"(Spread: {spread:+.1f} pts {spread_tag}) | 1H 8-EMA: {ema_8_1h:,.1f} | [{badge_icon} {tier_short}]"
        )

        return {
            "ok": True,
            "tier": tier,
            "bar_ts": r.get("ts"),
            "micro_sl_price": r.get("micro_sl_price"),
            "risk_points": r.get("risk_points"),
            "close_price": close_p,
            "ema_4": ema_4,
            "ema_8": ema_8,
            "spread_4_minus_8": spread,
            "ema_8_1h": ema_8_1h,
            "gap_pts": r.get("gap_pts", 0.0),
            "gap_atr_ratio": r.get("gap_atr_ratio", 0.0),
            "abs_gap_pts": r.get("abs_gap_pts"),
            "wick_touched_ema8": r.get("wick_touched_ema8", False),
            "flips_last_12_bars": r.get("flips_last_12_bars"),
            "crossover_state": r.get("crossover_state", "NEUTRAL"),
            "sr_interaction": r.get("sr_interaction", "NONE"),
            "is_whipsaw_chop": r.get("is_whipsaw_chop", False),
            "action_recommendation": r.get("action_recommendation", ""),
            "sl_guard_advice": r.get("sl_guard_advice", ""),
            "pill_text": pill_text,
            "timestamp_ist": r.get("timestamp_ist", ""),
        }


# Global singleton instance
ema_proximity_engine = EmaProximityEngine()
