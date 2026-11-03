"""
market/candles.py — ticks → 1-minute candles → every higher timeframe.

Design decisions (each one is a bug we have already paid for in the Streamlit app):
  * ONE source timeframe: 1 minute. 5m / 15m / 1h are resampled from it, aligned to the 09:15 session open,
    so all timeframes always agree (polling 5m and 15m separately lets them disagree mid-candle).
  * The forming candle lives in memory only and is sent to the UI flagged closed=False. The analysis engine
    NEVER sees it — every signal is computed from closed candles.
  * The REST backfill is the source of truth: after a reconnect (and again at 15:31) today's 1m candles are
    re-downloaded and overwrite whatever was built from ticks during the gap.
  * Candles outside 09:15–15:30 are dropped (Dhan has returned stray rows stamped 19:28).
  * Daily OHLC is assembled from 60-minute intraday candles — /charts/historical answers DH-905 for the index.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional

from .. import config, db
from ..timeutil import (IST, TF_SECONDS, bucket_start, in_session, is_closed, ist_date, now_epoch, now_ist,
                        session_bounds, to_ist)
from .buffers import CandleRingBuffer, TickRingBuffer

try:
    import pandas as pd
    pd.options.mode.copy_on_write = True
except Exception:
    pass

log = logging.getLogger("candles")

CloseCB = Callable[[str, Dict[str, float]], Awaitable[None]]


def session_filter(rows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    return [r for r in rows if in_session(r["ts"])]


def resample(rows_1m: List[Dict[str, float]], tf: str, now: Optional[float] = None) -> List[Dict[str, float]]:
    """1m rows (sorted) → tf candles aligned to 09:15. Adds 'closed' (bool)."""
    if tf == "1m":
        return [dict(r, closed=is_closed(r["ts"], "1m", now)) for r in rows_1m]
    out: List[Dict[str, float]] = []
    cur: Optional[Dict[str, float]] = None
    for r in rows_1m:
        b = bucket_start(r["ts"], tf)
        if cur is None or cur["ts"] != b:
            if cur is not None:
                out.append(cur)
            cur = {"ts": b, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r.get("v", 0.0)}
        else:
            cur["h"] = max(cur["h"], r["h"])
            cur["l"] = min(cur["l"], r["l"])
            cur["c"] = r["c"]
            cur["v"] = cur["v"] + r.get("v", 0.0)
    if cur is not None:
        out.append(cur)
    for c in out:
        # closed only when its whole window has elapsed (Candles.frame() also forces the candle that holds the
        # forming minute to closed=False)
        c["closed"] = is_closed(c["ts"], tf, now)
    return out


def daily_from(rows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Aggregate intraday rows (any tf) to IST-date daily OHLC, session hours only."""
    days: Dict[str, Dict[str, float]] = {}
    for r in session_filter(rows):
        d = ist_date(r["ts"]).isoformat()
        x = days.get(d)
        if x is None:
            days[d] = {"d": d, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
        else:
            x["h"], x["l"], x["c"] = max(x["h"], r["h"]), min(x["l"], r["l"]), r["c"]
    return [days[k] for k in sorted(days)]


class Candles:
    """Live candle builder for a set of instruments (NIFTY, VIX) with zero-copy RAM ring buffers."""

    def __init__(self, on_close_1m: CloseCB) -> None:
        self.forming: Dict[str, Dict[str, float]] = {}
        self.on_close_1m = on_close_1m
        self.last_price: Dict[str, float] = {}
        self.prev_close: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        bcfg = config.rules().get("buffers", {})
        self.max_candles = int(bcfg.get("max_candles_1m", 25000))
        self.max_ticks = int(bcfg.get("max_ticks", 10000))
        self.candle_buffers: Dict[str, CandleRingBuffer] = {}
        self.tick_buffer = TickRingBuffer(maxlen=self.max_ticks)
        self._frame_cache: Dict[tuple, List[Dict[str, float]]] = {}

    def get_candle_buffer(self, sid: str) -> CandleRingBuffer:
        buf = self.candle_buffers.get(sid)
        if buf is None:
            buf = CandleRingBuffer(maxlen=self.max_candles)
            self.candle_buffers[sid] = buf
        return buf

    def hydrate_buffer(self, sid: str, rows: List[Dict[str, float]]) -> None:
        """Hydrates the in-memory ring buffer with historical candles."""
        if rows:
            self.get_candle_buffer(sid).extend(rows)
            self._frame_cache.clear()

    def flush_write_behind(self) -> int:
        """Flushes all staged newly closed candles from ring buffers to SQLite in one batch."""
        total = 0
        for sid, buf in list(self.candle_buffers.items()):
            dirty = buf.get_and_clear_dirty()
            if dirty:
                db.upsert_candles(sid, dirty)
                total += len(dirty)
        return total

    async def write_behind_timer(self) -> None:
        """Non-blocking background task running every 60s to persist newly closed candles to WAL SQLite."""
        bcfg = config.rules().get("buffers", {})
        interval = float(bcfg.get("write_behind_interval_s", 60))
        while True:
            await asyncio.sleep(interval)
            try:
                n = self.flush_write_behind()
                if n > 0:
                    log.info("Write-behind persisted %d candles to SQLite", n)
            except Exception:
                log.exception("write-behind timer")

    async def tick(self, sid: str, ltp: float, t: float) -> None:
        self.last_price[sid] = ltp
        self.tick_buffer.append(sid, ltp, t)
        if not in_session(t):
            return
        b = bucket_start(t, "1m")
        async with self._lock:
            cur = self.forming.get(sid)
            if cur is not None and b < cur["ts"]:
                return                                          # late tick for an already-closed minute
            if cur is None or b > cur["ts"]:
                if cur is not None:
                    await self._close(sid, cur)
                self.forming[sid] = {"ts": b, "o": ltp, "h": ltp, "l": ltp, "c": ltp, "v": 0.0}
            else:
                cur["h"], cur["l"], cur["c"] = max(cur["h"], ltp), min(cur["l"], ltp), ltp

    async def _close(self, sid: str, c: Dict[str, float]) -> None:
        # Stages in RAM ring buffer with write-behind dirty flag (flushed every 60s or on shutdown)
        self.get_candle_buffer(sid).append(c, stage_dirty=True)
        self._frame_cache.clear()
        await self.on_close_1m(sid, dict(c, closed=True))

    async def sweep_timer(self) -> None:
        """Closes a forming minute when the minute ends even if no further tick arrives (quiet markets, VIX)."""
        while True:
            await asyncio.sleep(1.0)
            now = now_epoch()
            async with self._lock:
                for sid, cur in list(self.forming.items()):
                    if is_closed(cur["ts"], "1m", now):
                        del self.forming[sid]
                        await self._close(sid, cur)

    # ── reading ───────────────────────────────────────────────────────────
    def historical_frame(self, sid: str, tf: str, days: int, asof: str) -> List[Dict[str, float]]:
        """Candles for a PAST window ending on `asof`, read straight from SQLite.

        Deliberately separate from the live path below: no ring buffer (it only holds recent
        bars), no forming candle, no frame cache (one-off review queries would evict the live
        entries the desk depends on). Everything returned is closed, because it already happened.
        """
        try:
            end_day = datetime.fromisoformat(asof).replace(tzinfo=IST)
        except ValueError:
            return []
        if tf == "1D":
            rows = [r for r in db.daily(sid, limit=days + 400) if r["d"] <= asof][-days:]
            return [{"ts": int(datetime.fromisoformat(r["d"]).replace(tzinfo=IST).timestamp()),
                     "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": 0.0, "closed": True}
                    for r in rows]
        # Pull a generous CALENDAR window, then keep the last `days` SESSIONS inside it, so
        # weekends and holidays never silently shorten the window.
        start = end_day - timedelta(days=days * 2 + 10)
        raw = db.candles_1m(sid, int(start.timestamp()),
                            int((end_day + timedelta(days=1)).timestamp()))
        rows = session_filter(raw)
        keep = set(sorted({ist_date(r["ts"]) for r in rows})[-days:])
        out = resample([r for r in rows if ist_date(r["ts"]) in keep], tf)
        for c in out:
            c["closed"] = True
        return out

    def frame(self, sid: str, tf: str, days: int = 3, include_forming: bool = True,
              limit: Optional[int] = None, asof: Optional[str] = None) -> List[Dict[str, float]]:
        """Candles for the chart / engine. Fulfills queries from RAM ring buffers with zero disk I/O.
        Caches resampled frames and supports limit parameter to slice only the tail window needed.

        `asof` (ISO date) switches to the historical reader above and leaves this path untouched.
        """
        if asof:
            return self.historical_frame(sid, tf, days, asof)
        if tf == "1D":
            rows = db.daily(sid, limit=max(days, 5))
            out = [{"ts": int(datetime.fromisoformat(r["d"]).replace(tzinfo=IST).timestamp()),
                    "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": 0.0, "closed": True} for r in rows]
            today = self.today_ohlc(sid)
            if today and (not out or out[-1]["ts"] < today["ts"]):
                out.append(today)
            if limit is not None and limit > 0 and len(out) > limit:
                return out[-limit:]
            return out

        buf = self.get_candle_buffer(sid)
        f = self.forming.get(sid)
        cache_key = (sid, tf, days, include_forming, limit, buf.version,
                     int(f["ts"]) if f else None, round(f["c"], 1) if f else None)
        cached = self._frame_cache.get(cache_key)
        if cached is not None:
            # Refresh LRU access order
            self._frame_cache[cache_key] = self._frame_cache.pop(cache_key)
            return [dict(x) for x in cached]

        since = int((now_ist() - timedelta(days=days + 3)).timestamp())
        # Zero-copy RAM retrieval: check if in-memory buffer has data covering `since`
        if len(buf) > 0 and (buf.earliest_ts() is None or buf.earliest_ts() <= since):
            raw_rows = buf.slice(since)
        else:
            # Cold cache fallback: seed from SQLite once and populate ring buffer
            raw_rows = db.candles_1m(sid, since)
            if raw_rows:
                buf.extend(raw_rows)
            elif len(buf) > 0:
                raw_rows = buf.slice(since)
        rows = session_filter(raw_rows)
        # keep only the last `days` trading days
        dates = sorted({ist_date(r["ts"]) for r in rows})[-days:]
        keep = set(dates)
        rows = [r for r in rows if ist_date(r["ts"]) in keep]
        if include_forming and f and (not rows or f["ts"] > rows[-1]["ts"]):
            rows = rows + [dict(f)]
        out = resample(rows, tf)
        if include_forming and f:
            # the higher-tf candle containing the forming minute is still forming
            if out and out[-1]["ts"] == bucket_start(f["ts"], tf):
                out[-1]["closed"] = False
        if not include_forming:
            out = [c for c in out if c["closed"]]
        if limit is not None and limit > 0 and len(out) > limit:
            out = out[-limit:]

        bcfg = config.rules().get("buffers", {})
        max_cache = int(bcfg.get("max_frame_cache_entries", bcfg.get("resample_cache_size", 64)))
        # Evict stale versions from _frame_cache
        current_versions = {s: b.version for s, b in self.candle_buffers.items()}
        for k in list(self._frame_cache.keys()):
            if k[0] in current_versions and k[5] < current_versions[k[0]]:
                self._frame_cache.pop(k, None)
        # Evict oldest entries if capacity is reached
        while len(self._frame_cache) >= max_cache:
            self._frame_cache.pop(next(iter(self._frame_cache)))
        self._frame_cache[cache_key] = [dict(x) for x in out]
        return [dict(x) for x in out]

    def today_ohlc(self, sid: str) -> Optional[Dict[str, float]]:
        o, _ = session_bounds(now_ist().date())
        buf = self.get_candle_buffer(sid)
        if len(buf) > 0 and (buf.earliest_ts() is None or buf.earliest_ts() <= int(o)):
            rows = buf.today(int(o))
        else:
            rows = db.candles_1m(sid, int(o))
            if rows:
                buf.extend(rows)
        f = self.forming.get(sid)
        if f:
            rows = rows + [f]
        if not rows:
            return None
        day0 = int(datetime.combine(now_ist().date(), datetime.min.time(), IST).timestamp())
        return {"ts": day0,
                "o": rows[0]["o"], "h": max(r["h"] for r in rows), "l": min(r["l"] for r in rows),
                "c": rows[-1]["c"], "v": 0.0, "closed": False}


# ── REST backfill ────────────────────────────────────────────────────────────
async def backfill(rest, sid: str, days: int, segment: str = "IDX_I", instrument: str = "INDEX",
                   candles_builder: Optional[Candles] = None) -> int:
    """Download the last `days` calendar days of 1m candles (one call, < 90 days) into SQLite and memory buffer."""
    to = now_ist()
    frm = (to - timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = session_filter(await rest.intraday(sid, frm, to, "1", segment, instrument))
    now = now_epoch()
    rows = [r for r in rows if r["ts"] <= now and is_closed(r["ts"], "1m", now)]   # future-date guard + no forming
    db.upsert_candles(sid, rows)
    if candles_builder is not None:
        candles_builder.hydrate_buffer(sid, rows)
    return len(rows)


async def backfill_daily(rest, sid: str, days: int) -> int:
    to = now_ist()
    frm = (to - timedelta(days=min(days, 89))).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = await rest.intraday(sid, frm, to, "60")
    daily = daily_from(rows)
    today = now_ist().date().isoformat()
    _, close = session_bounds(now_ist().date())
    if daily and daily[-1]["d"] == today and now_epoch() < close:
        daily = daily[:-1]                                   # a half-built day is worse than none
    db.upsert_daily(sid, daily)
    return len(daily)
