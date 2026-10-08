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
    """Live candle builder for a set of instruments (NIFTY, VIX)."""

    def __init__(self, on_close_1m: CloseCB) -> None:
        self.forming: Dict[str, Dict[str, float]] = {}
        self.on_close_1m = on_close_1m
        self.last_price: Dict[str, float] = {}
        self.prev_close: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def tick(self, sid: str, ltp: float, t: float) -> None:
        self.last_price[sid] = ltp
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
        db.upsert_candles(sid, [c])
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
    def frame(self, sid: str, tf: str, days: int = 3, include_forming: bool = True) -> List[Dict[str, float]]:
        """Candles for the chart / engine. Engine callers pass include_forming=False."""
        if tf == "1D":
            rows = db.daily(sid, limit=max(days, 5))
            out = [{"ts": int(datetime.fromisoformat(r["d"]).replace(tzinfo=IST).timestamp()),
                    "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": 0.0, "closed": True} for r in rows]
            today = self.today_ohlc(sid)
            if today and (not out or out[-1]["ts"] < today["ts"]):
                out.append(today)
            return out
        since = int((now_ist() - timedelta(days=days + 3)).timestamp())
        rows = session_filter(db.candles_1m(sid, since))
        # keep only the last `days` trading days
        dates = sorted({ist_date(r["ts"]) for r in rows})[-days:]
        keep = set(dates)
        rows = [r for r in rows if ist_date(r["ts"]) in keep]
        f = self.forming.get(sid)
        if include_forming and f and (not rows or f["ts"] > rows[-1]["ts"]):
            rows = rows + [dict(f)]
        out = resample(rows, tf)
        if include_forming and f:
            # the higher-tf candle containing the forming minute is still forming
            if out and out[-1]["ts"] == bucket_start(f["ts"], tf):
                out[-1]["closed"] = False
        if not include_forming:
            out = [c for c in out if c["closed"]]
        return out

    def today_ohlc(self, sid: str) -> Optional[Dict[str, float]]:
        o, _ = session_bounds(now_ist().date())
        rows = db.candles_1m(sid, int(o))
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
async def backfill(rest, sid: str, days: int, segment: str = "IDX_I", instrument: str = "INDEX") -> int:
    """Download the last `days` calendar days of 1m candles (one call, < 90 days) into SQLite."""
    to = now_ist()
    frm = (to - timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = session_filter(await rest.intraday(sid, frm, to, "1", segment, instrument))
    now = now_epoch()
    rows = [r for r in rows if r["ts"] <= now and is_closed(r["ts"], "1m", now)]   # future-date guard + no forming
    db.upsert_candles(sid, rows)
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
