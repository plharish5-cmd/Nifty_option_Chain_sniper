"""
timeutil.py — one place for Indian market time.

Rules this file enforces (mistake #3 in the guide):
  * Every timestamp stored or sent is an EPOCH in seconds (true UTC epoch). No naive datetimes cross a boundary.
  * Every human-facing time is IST (UTC+05:30, no daylight saving — a fixed offset is exact, no tzdata needed).
  * A candle's timestamp is its START. A 5m candle stamped 14:50 covers 14:50:00–14:54:59 and is CLOSED from 14:55:00.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Tuple

from . import config

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_S = 19800

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1D": 86400}


# TEST ONLY — simulated clock for replaying a past session after hours with tools/fake_dhan.py.
#   ICT_SIM_START = epoch the replay starts at, ICT_SIM_REAL0 = wall-clock epoch it started, ICT_SIM_SPEED = ×speed
# Never set these in your real .env.
import os as _os
import time as _time
_SIM_START = float(_os.environ.get("ICT_SIM_START", "0") or 0)
_SIM_REAL0 = float(_os.environ.get("ICT_SIM_REAL0", "0") or 0)
_SIM_SPEED = float(_os.environ.get("ICT_SIM_SPEED", "1") or 1)


def now_epoch() -> float:
    if _SIM_START:
        return _SIM_START + (_time.time() - _SIM_REAL0) * _SIM_SPEED
    return datetime.now(timezone.utc).timestamp()


def now_ist() -> datetime:
    return datetime.fromtimestamp(now_epoch(), IST)


def to_ist(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, IST)


def hm(epoch: float) -> str:
    return to_ist(epoch).strftime("%H:%M")


def parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_bounds(d: date) -> Tuple[float, float]:
    """(open epoch, close epoch) of the cash session on IST date d."""
    o = datetime.combine(d, parse_hm(config.rule("session", "open", "09:15")), IST)
    c = datetime.combine(d, parse_hm(config.rule("session", "close", "15:30")), IST)
    return o.timestamp(), c.timestamp()


def in_session(epoch: float) -> bool:
    t = to_ist(epoch)
    if t.weekday() >= 5:
        return False
    o, c = session_bounds(t.date())
    return o <= epoch < c


def market_open_now() -> bool:
    return in_session(now_epoch())


def bucket_start(epoch: float, tf: str) -> int:
    """Start of the tf-candle containing epoch, aligned to the 09:15 IST session open (NSE convention:
    5m candles are 09:15, 09:20 …; 1h candles are 09:15, 10:15 … 15:15 — NOT clock hours)."""
    if tf == "1D":
        d = to_ist(epoch).date()
        return int(datetime.combine(d, time(0, 0), IST).timestamp())
    step = TF_SECONDS[tf]
    t = to_ist(epoch)
    o, _ = session_bounds(t.date())
    if epoch < o:                              # pre-open ticks belong to nothing
        return int(o)
    return int(o + ((epoch - o) // step) * step)


def is_closed(start_epoch: float, tf: str, now: float = None) -> bool:
    """A candle is closed when its window has fully elapsed (or the session has ended)."""
    now = now_epoch() if now is None else now
    end = start_epoch + TF_SECONDS[tf]
    _, c = session_bounds(to_ist(start_epoch).date())
    return now >= min(end, c)


def in_windows(epoch: float, windows: List[List[str]]) -> bool:
    t = to_ist(epoch).time()
    return any(parse_hm(a) <= t < parse_hm(b) for a, b in windows)


def ist_date(epoch: float) -> date:
    return to_ist(epoch).date()


def chart_time(epoch: float) -> int:
    """Lightweight Charts draws its axis in UTC. Shifting by +5:30 makes the axis read IST.
    Only ever applied at the very edge (the JSON sent to the chart) — never stored."""
    return int(epoch) + IST_OFFSET_S
