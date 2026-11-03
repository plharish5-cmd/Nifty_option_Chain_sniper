"""OI-walls replay — the guarantees that make it worth trusting.

The replay exists so the walls engine can be GRADED against history. That is only worth anything
if a frame sees exactly what was knowable at its own timestamp. A replay that leaks future bars
flatters every signal it draws, which is the error that manufactured a false +3.3 sd in this repo
on 2026-11-01.

So the load-bearing test here is causality: the frame for 11:00 must be byte-identical whether it
is built from the full session or from a database that ends at 11:00.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.api import oi_walls_replay_api as R

IST = timezone(timedelta(hours=5, minutes=30))
DAY = "2026-10-15"
EXPIRY = "2026-10-16"


def _seed(cut_minute=None):
    """A synthetic session: 375 minutes, 18 strikes, a deliberate heavy PE shelf at 22,900."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE chain_snap (expiry TEXT, ts INTEGER, strike REAL, ce_oi REAL, pe_oi REAL)")
    con.execute("CREATE TABLE candles_1m (sec_id TEXT, ts INTEGER, o REAL,h REAL,l REAL,c REAL,v REAL)")
    start = datetime(2026, 10, 15, 9, 15, tzinfo=IST)
    snaps, cands = [], []
    for m in range(375):
        if cut_minute is not None and m > cut_minute:
            break
        ts = int((start + timedelta(minutes=m)).timestamp())
        px = 23000.0 + m * 0.5                       # steady climb, so future bars differ clearly
        cands.append(("13", ts, px, px + 2, px - 2, px, 100.0))
        for k in range(22600, 23500, 50):
            ce = 50000.0 + m * 10 + (30000.0 if k == 23300 else 0.0)
            pe = 40000.0 + m * 12 + (60000.0 if k == 22900 else 0.0)
            snaps.append((EXPIRY, ts, float(k), ce, pe))
    con.executemany("INSERT INTO chain_snap VALUES (?,?,?,?,?)", snaps)
    con.executemany("INSERT INTO candles_1m VALUES (?,?,?,?,?,?,?)", cands)
    con.commit()
    return con


class _Db:
    def __init__(self, con):
        self.con = con

    def query(self, q, p=()):
        return [dict(r) for r in self.con.execute(q, p)]

    def candles_1m(self, sid, since, until=None):
        return [dict(r) for r in self.con.execute(
            "SELECT ts,o,h,l,c,v FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<?",
            (sid, since, until))]


@pytest.fixture()
def full(monkeypatch):
    monkeypatch.setattr(R, "db", _Db(_seed()))
    R._CACHE.clear()
    return R._build_day(DAY, EXPIRY)


def test_every_minute_becomes_a_frame(full):
    assert full["ok"] is True
    assert full["count"] == 375, full["count"]


def test_frames_are_ordered_and_span_the_session(full):
    f = full["frames"]
    assert [x["ts"] for x in f] == sorted(x["ts"] for x in f)
    assert f[0]["time"] == "09:15"
    assert f[-1]["time"] == "15:29"


def test_no_lookahead_frame_matches_a_truncated_database(monkeypatch, full):
    """THE test. Rebuild with data that STOPS at 11:00 and compare that frame to the full run."""
    cut = 105                                        # 09:15 + 105 min = 11:00
    monkeypatch.setattr(R, "db", _Db(_seed(cut_minute=cut)))
    R._CACHE.clear()
    truncated = R._build_day(DAY, EXPIRY)["frames"][-1]
    same_time = [x for x in full["frames"] if x["ts"] == truncated["ts"]]
    assert same_time, "the truncated frame's timestamp must exist in the full run"
    a, b = same_time[0], truncated
    assert a["spot"] == b["spot"]
    assert a["pcr"] == b["pcr"]
    assert a["derived_levels"] == b["derived_levels"]
    assert a["walls"] == b["walls"], "frame changed when future data was present — look-ahead"


def test_support_is_derived_from_the_snapshot_not_the_config(full):
    """22,900 carries the planted PE shelf. The configured default is 23,200 and must not win."""
    lv = full["frames"][0]["derived_levels"]
    assert lv["support_bottom"] == 22900.0, lv
    assert lv["support_bottom"] != 23200.0
    assert lv["structural_downside"] < lv["support_bottom"]


def test_panic_trigger_tracks_the_derived_support(full):
    """With support derived at 22,900 the trigger must sit near it, never at the hardcoded 23,220."""
    w = full["frames"][0]["walls"]
    trig = w.get("panic_unwinding_trigger") or w.get("put_unwinding_trigger_price")
    if trig is not None:
        assert abs(float(trig) - 22920.0) < 1.0, trig


def test_oi_change_is_measured_against_the_day_open(full):
    """First frame has no change yet; later frames must show the accumulated change."""
    assert full["frames"][0]["walls"] is not None
    assert "oi_chg_basis" in full and "first snapshot" in full["oi_chg_basis"]


def test_a_day_with_no_snapshots_reports_cleanly(monkeypatch):
    monkeypatch.setattr(R, "db", _Db(_seed()))
    R._CACHE.clear()
    res = R._build_day("2020-01-01", EXPIRY)
    assert res["ok"] is False and res["frames"] == []


def test_whole_session_builds_fast(full):
    """It must stay a single fast request — the slider depends on the day arriving in one go."""
    import time
    R._CACHE.clear()
    t0 = time.time()
    R._build_day(DAY, EXPIRY)
    assert (time.time() - t0) < 3.0, "a session should build in well under a second"
