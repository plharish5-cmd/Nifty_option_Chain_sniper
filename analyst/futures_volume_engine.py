"""8510 futures volume: causal same-slot median, SMA10 and bar acceleration.
The live 8600 database is read-only. Missing/partial bars are never padded.
"""
from __future__ import annotations
import bisect
import json
import logging
import math
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / 'ict_smc_app/backend/data/ict_smc.sqlite3'
DEFAULT_CONFIG_PATH = ROOT / 'analyst/terminal8510_config.json'
IST = ZoneInfo('Asia/Kolkata')
log = logging.getLogger(__name__)
_READ_CACHE = {}


def load_config(config_path=None):
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    return json.loads(path.read_text()).get('futures_volume_engine', {})


class FuturesVolumeEngine:
    def __init__(self, db_path=None, vsr_threshold=None, config_path=None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        cfg = load_config(self.config_path)
        # Defaults are owned by the checked-in JSON, including for temporary test configs.
        defaults = load_config(DEFAULT_CONFIG_PATH)
        self.cfg = {**defaults, **cfg}
        self.vsr_threshold = float(vsr_threshold if vsr_threshold is not None else self.cfg['vsr_threshold'])
        self.min_baseline_sessions = int(self.cfg['min_baseline_sessions'])
        self.lookback_sessions = int(self.cfg['lookback_sessions'])
        self._df_15m = None; self._df_1m = None
        self._records_15m = []; self._ts_15m = []
        self._coverage = {}; self._loaded_at = 0; self._last_loaded_ts = 0
        self.ensure_loaded()

    def ensure_loaded(self, force=False):
        if not force and self._loaded_at and time.monotonic()-self._loaded_at < self.cfg['refresh_seconds']:
            return
        self._loaded_at = time.monotonic()
        if not self.db_path.exists():
            self._records_15m = []; self._ts_15m = []; return
        try:
            stat = self.db_path.stat()
            cache_key = (str(self.db_path.resolve()),stat.st_mtime_ns,stat.st_size,self.min_baseline_sessions,self.lookback_sessions)
            cached = _READ_CACHE.get(cache_key)
            if not force and cached and time.monotonic()-cached['at'] < self.cfg['refresh_seconds']:
                self._records_15m = [dict(r) for r in cached['records']]
                self._ts_15m = list(cached['timestamps']);self._df_15m=cached['bars'];self._df_1m=cached['minutes']
                self._coverage=dict(cached['coverage']);self._last_loaded_ts=self._ts_15m[-1] if self._ts_15m else 0
                return
            with sqlite3.connect('file:'+str(self.db_path)+'?mode=ro', uri=True) as conn:
                frame = pd.read_sql_query("SELECT ts,sec_id,v FROM candles_1m WHERE sec_id IN ('FUT','FUT2') ORDER BY ts", conn)
            if frame.empty:
                self._records_15m = []; self._ts_15m = []; return
            frame['v'] = pd.to_numeric(frame.v, errors='coerce')
            frame = frame[frame.v.notna() & frame.v.ge(0) & frame.v.map(math.isfinite)]
            frame['ts'] = frame.ts.astype(int)
            stamp = pd.to_datetime(frame.ts, unit='s', utc=True).dt.tz_convert(IST)
            clock = stamp.dt.hour*60+stamp.dt.minute
            frame = frame[(clock >= 555) & (clock < 930)]
            if frame.empty:
                self._records_15m = []; self._ts_15m = []; return
            pivot = frame.pivot_table(index='ts', columns='sec_id', values='v', aggfunc='last').reindex(columns=['FUT','FUT2'])
            pivot['COMBINED'] = pivot[['FUT','FUT2']].sum(axis=1, min_count=1)
            pivot['both_legs'] = pivot[['FUT','FUT2']].notna().all(axis=1).astype(int)
            pivot['ts'] = pivot.index
            stamp = pd.to_datetime(pivot.ts, unit='s', utc=True).dt.tz_convert(IST)
            pivot['session_date'] = stamp.dt.strftime('%Y-%m-%d')
            pivot['slot'] = stamp.dt.floor('15min').dt.strftime('%H:%M')
            pivot['bar_ts'] = stamp.dt.floor('15min').astype('int64') // 10**9
            pivot['sma10'] = pivot.COMBINED.rolling(10, min_periods=10).mean()
            pivot['vsr_10'] = pivot.COMBINED / pivot.sma10.where(pivot.sma10.gt(0))
            self._df_1m = pivot.copy()
            # Group observed session slots; resample across nights would manufacture zero bars.
            bars = pivot.groupby(['session_date','slot','bar_ts'], as_index=False).agg(
                COMBINED=('COMBINED','sum'), FUT=('FUT','sum'), FUT2=('FUT2','sum'),
                observed_minutes=('ts','nunique'), both_legs=('both_legs','sum'))
            bars = bars.rename(columns={'bar_ts':'ts'}).sort_values('ts').reset_index(drop=True)
            bars['complete'] = (bars.observed_minutes==15) & (bars.both_legs==15)
            full_vol = bars.COMBINED.where(bars.complete)
            bars['sma10'] = full_vol.rolling(10, min_periods=10).mean()
            bars['vsr_10'] = full_vol / bars.sma10.where(bars.sma10.gt(0))
            prior = full_vol.shift(1)
            bars['bar_accel_ratio'] = full_vol / prior.clip(lower=1.0)
            histories = defaultdict(list); records = []
            for _, b in bars.iterrows():
                record = b.to_dict(); history = histories[b.slot][-self.lookback_sessions:]
                n = len(history); median = statistics.median(history) if history else None
                sufficient = n >= self.min_baseline_sessions and median is not None and median > 0
                status = 'ADEQUATE' if sufficient else 'INSUFFICIENT_HISTORY'
                if not b.complete: status = 'INCOMPLETE_BAR'
                record.update(slot_rvol_median=float(b.COMBINED/median) if sufficient and b.complete else None,
                    median_baseline=median, sessions_covered=n, slot_status=status,
                    coverage_pct=round(n*100/self.lookback_sessions, 1),
                    current_bar_coverage_pct=round(b.both_legs*100/15, 1),
                    datetime_ist=datetime.fromtimestamp(int(b.ts),IST).isoformat())
                records.append(record)
                if b.complete and b.COMBINED > 0:
                    histories[b.slot].append(float(b.COMBINED))
            self._records_15m = records; self._ts_15m = [int(r['ts']) for r in records]
            self._df_15m = bars
            self._last_loaded_ts = self._ts_15m[-1] if self._ts_15m else 0
            self._coverage = {'n_sessions':int(bars.session_date.nunique())}
            _READ_CACHE.clear()
            _READ_CACHE[cache_key]={'at':time.monotonic(),'records':[dict(r) for r in records],'timestamps':list(self._ts_15m),'bars':bars,'minutes':self._df_1m,'coverage':dict(self._coverage)}
        except Exception:
            self._records_15m = []; self._ts_15m = []
            log.exception('Cannot read futures volume; surge unavailable')

    def _format_record(self, rec=None):
        rec = rec or {}
        def finite(key):
            v = rec.get(key)
            return float(v) if isinstance(v,(int,float)) and math.isfinite(v) else None
        vsr, slot, accel = finite('vsr_10'), finite('slot_rvol_median'), finite('bar_accel_ratio')
        status = rec.get('slot_status','INSUFFICIENT_HISTORY')
        primary = slot is not None and slot >= self.cfg['slot_rvol_threshold']
        fallback = status=='INSUFFICIENT_HISTORY' and slot is None and vsr is not None and vsr >= self.cfg['vsr_fallback_threshold']
        surge = bool((primary or fallback) and accel is not None and accel >= self.cfg['bar_accel_threshold'] and rec.get('complete',False))
        legacy = bool(vsr is not None and vsr >= self.vsr_threshold)
        def display(v):return f'{v:.2f}x' if v is not None else 'unavailable'
        n = int(rec.get('sessions_covered',0)); coverage = rec.get('coverage_pct',0.0)
        tooltip = f"Same-slot baseline: {n}/{self.lookback_sessions} prior complete sessions ({coverage}%). Combined FUT+FUT2; observed clock slots only. Slot status: {status}. Acceleration {display(accel)}. SMA10 {display(vsr)}."
        return dict(vsr_10=vsr, volume=finite('COMBINED'), sma10=finite('sma10'),
            slot_rvol_median=slot, median_baseline=finite('median_baseline'), bar_accel_ratio=accel,
            sessions_covered=n, coverage_pct=coverage, current_bar_coverage_pct=rec.get('current_bar_coverage_pct',0),
            slot_status=status, coverage_status=status, slot=rec.get('slot'),
            is_institutional_surge=surge, is_surge=legacy,
            tag='VOLUME SURGE CONFIRMED' if surge else 'NORMAL_VOLUME',
            badge='INSTITUTIONAL VOLUME SURGE' if surge else 'VOLUME UNAVAILABLE' if vsr is None else 'NORMAL VOLUME',
            pill_text=f"Futures: Slot {display(slot)} | SMA10 {display(vsr)} | Accel {display(accel)} | {n}/{self.lookback_sessions} sessions",
            tooltip=tooltip, bar_ts=rec.get('ts'), datetime_ist=rec.get('datetime_ist'),
            baseline_basis='Prior complete same-clock 15m slots; current session excluded',
            fallback_used=bool(fallback), available=bool(rec.get('complete',False)))

    def get_latest_vsr(self):
        return self.get_vsr_at_timestamp(int(time.time()))

    def get_vsr_at_timestamp(self, ts):
        self.ensure_loaded()
        index = bisect.bisect_right(self._ts_15m,int(ts)-900)-1
        return self._format_record(self._records_15m[index] if index>=0 else None)

_GLOBAL_ENGINE = None

def get_futures_volume_engine():
    global _GLOBAL_ENGINE
    if _GLOBAL_ENGINE is None:_GLOBAL_ENGINE=FuturesVolumeEngine()
    return _GLOBAL_ENGINE
