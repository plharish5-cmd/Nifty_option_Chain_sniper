"""Candles.historical_frame — the review-mode reader behind /api/candles?asof=.

Guards the three things that would silently corrupt a reviewed session:
  * the window must END on the requested date, never run past it (that would be look-ahead)
  * it must count SESSIONS, not calendar days, so weekends never shorten the window
  * everything it returns must be closed, because it already happened
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market import candles as cmod
from app.market.candles import Candles

IST = timezone(timedelta(hours=5, minutes=30))


def _session(day: datetime, px: float) -> list[dict]:
    """One full 09:15–15:29 session of 1-minute bars."""
    out = []
    t = day.replace(hour=9, minute=15, second=0, microsecond=0)
    for i in range(375):
        p = px + i * 0.1
        out.append({"ts": int(t.timestamp()), "o": p, "h": p + 2, "l": p - 2, "c": p, "v": 100.0})
        t += timedelta(minutes=1)
    return out


@pytest.fixture()
def seeded(monkeypatch):
    """20 weekday sessions from 2026-09-01, served straight out of a fake db.candles_1m."""
    rows: list[dict] = []
    day = datetime(2026, 9, 1, tzinfo=IST)
    made = 0
    while made < 20:
        if day.weekday() < 5:
            rows.extend(_session(day, 23000 + made * 10))
            made += 1
        day += timedelta(days=1)

    def fake_candles_1m(sec_id, since, until=None):
        return [r for r in rows if r["ts"] >= since and (until is None or r["ts"] < until)]

    monkeypatch.setattr(cmod.db, "candles_1m", fake_candles_1m)
    return Candles.__new__(Candles)       # no __init__: historical_frame touches no live state


def _days_in(out):
    return sorted({datetime.fromtimestamp(c["ts"], IST).date() for c in out})


def test_window_ends_on_the_requested_date(seeded):
    out = Candles.historical_frame(seeded, "13", "5m", 3, "2026-09-16")
    assert out, "expected candles for a date inside the seeded range"
    assert _days_in(out)[-1].isoformat() == "2026-09-16"


def test_nothing_after_asof_leaks_in(seeded):
    """The whole point of review mode: a bar from the day after would be look-ahead."""
    cutoff = datetime(2026, 9, 17, tzinfo=IST).timestamp()
    out = Candles.historical_frame(seeded, "13", "5m", 3, "2026-09-16")
    assert max(c["ts"] for c in out) < cutoff


def test_counts_sessions_not_calendar_days(seeded):
    """2026-09-14 is a Monday. Three SESSIONS back reaches the previous Thursday, not Saturday."""
    out = Candles.historical_frame(seeded, "13", "5m", 3, "2026-09-14")
    got = [d.isoformat() for d in _days_in(out)]
    assert got == ["2026-09-10", "2026-09-11", "2026-09-14"], got


def test_everything_returned_is_closed(seeded):
    out = Candles.historical_frame(seeded, "13", "5m", 5, "2026-09-18")
    assert out and all(c["closed"] for c in out)


def test_bar_count_matches_the_timeframe(seeded):
    """375 one-minute bars per session must resample to 75 five-minute bars."""
    out = Candles.historical_frame(seeded, "13", "5m", 2, "2026-09-16")
    assert len(out) == 150, len(out)
    assert len(Candles.historical_frame(seeded, "13", "1m", 1, "2026-09-16")) == 375


def test_date_before_any_data_is_empty_not_an_error(seeded):
    assert Candles.historical_frame(seeded, "13", "5m", 3, "2020-01-01") == []


def test_malformed_date_is_empty_not_an_exception(seeded):
    assert Candles.historical_frame(seeded, "13", "5m", 3, "not-a-date") == []


def test_asof_routes_frame_to_the_historical_reader(monkeypatch):
    """frame(asof=...) must take the historical branch and never touch the live path."""
    sentinel = [{"ts": 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 0.0, "closed": True}]
    called = {}

    def fake_hist(self, sid, tf, days, asof):
        called.update(sid=sid, tf=tf, days=days, asof=asof)
        return sentinel

    monkeypatch.setattr(Candles, "historical_frame", fake_hist)
    cs = Candles.__new__(Candles)
    assert Candles.frame(cs, "13", "15m", days=4, asof="2026-10-01") is sentinel
    assert called == {"sid": "13", "tf": "15m", "days": 4, "asof": "2026-10-01"}
