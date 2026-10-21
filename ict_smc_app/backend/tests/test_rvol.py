"""Cumulative RVOL — time-matched, no look-ahead, states and events."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.engine import rvol  # noqa: E402

D0 = 1791431100 - 25 * 86400          # 25 calendar days before 8 Oct 09:15 IST


def day(start, per_min, minutes=375, boost=None):
    rows = []
    for m in range(minutes):
        v = per_min * (3 if m < 15 else 1)             # busy open, like NIFTY
        if boost and boost[0] <= m < boost[1]:
            v *= boost[2]
        rows.append({"ts": start + 60 * m, "o": 1, "h": 1, "l": 1, "c": 1, "v": v})
    return rows


def history(n=22):
    rows, d = [], D0
    k = 0
    while k < n:
        rows += day(d, 1000)
        d += 86400
        k += 1
    return rows, d


def test_time_matched_cumulative():
    rows, today = history()
    rows += day(today, 1000, minutes=60)               # an ordinary morning
    res = rvol.cumulative(rows)
    p = res["points"][-1]
    assert abs(p["c20"] - 1.0) < 1e-6 and abs(p["c5"] - 1.0) < 1e-6   # busy open does NOT inflate it
    assert rvol.state(res)["state"] == "NORMAL"


def test_activation_and_states():
    rows, today = history()
    rows += day(today, 1000, minutes=90, boost=(30, 90, 4))   # from 09:45 volume runs at 4× normal
    res = rvol.cumulative(rows)
    st = rvol.state(res)
    assert st["state"] == "ACTIVE" and st["trend"] == "BUILDING"
    ev = rvol.events(res, {}, [], "5m")
    act = [e for e in ev if e["kind"] == "RVOL_ACTIVATED"]
    assert len(act) == 1 and act[0]["value"] >= 1.5


def test_no_look_ahead():
    rows, today = history()
    full = rows + day(today, 1000, minutes=200, boost=(100, 200, 5))
    part = rows + day(today, 1000, minutes=120, boost=(100, 200, 5))
    a = {p["ts"]: p for p in rvol.cumulative(full)["points"]}
    b = rvol.cumulative(part)["points"]
    assert all(a[p["ts"]] == p for p in b)


def test_breakout_without_and_with_rvol():
    rows, today = history()
    quiet = rows + day(today, 500, minutes=60)             # half a normal day
    res = rvol.cumulative(quiet)
    bo = [{"ts": today + 1800, "kind": "BOS", "dir": "bull", "text": "BOS bull through 22,500"}]
    ev = rvol.events(res, {}, bo, "5m")
    assert [e["kind"] for e in ev] == ["BREAKOUT_NO_RVOL"]
    hot = rows + day(today, 3000, minutes=60)
    ev2 = rvol.events(rvol.cumulative(hot), {today + 1800: {"rvol": 2.0}}, bo, "5m")
    assert "BREAKOUT_WITH_RVOL" in [e["kind"] for e in ev2]


def test_unknown_without_history():
    rows = day(D0, 1000, minutes=30)
    st = rvol.state(rvol.cumulative(rows))
    assert st["state"] == "UNKNOWN" and "earlier sessions" in st["text"]


def test_alert_is_fresh_only_and_sent_once():
    import asyncio
    from app import db
    from app.signals import alerts as al
    from app.timeutil import now_epoch
    sent = []

    class A:
        async def broadcast(self, m):
            sent.append(m)
    t = int(now_epoch())
    db.execute("DELETE FROM alerts WHERE key LIKE 'RVOL|T%'")
    e = {"kind": "RVOL_ACTIVATED", "ts": t, "key": "RVOL|T|1", "title": "x", "body": "y"}
    old = dict(e, ts=t - 3600, key="RVOL|T|2")
    assert asyncio.run(al.notify_event(A(), e)) is not None
    assert asyncio.run(al.notify_event(A(), e)) is None          # never twice
    assert asyncio.run(al.notify_event(A(), old)) is None        # a restart never replays the morning
    assert len(sent) == 1 and sent[0]["alert"]["grade"] == "rvol"


def test_rollover_keeps_old_history():
    from app import db
    from app.market import futures
    sec = "FUT_T"
    db.execute("DELETE FROM kv WHERE k = ?", (f"fut_sid_{sec}",))
    db.execute("DELETE FROM candles_1m WHERE sec_id = ?", (sec,))
    assert futures.history_days(sec, "35001", 35)[0] == 35          # first ever run: full history
    db.upsert_candles(sec, [{"ts": D0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 10}])
    assert futures.history_days(sec, "35001", 35)[0] == 35          # same contract after a restart: full
    days, note = futures.history_days(sec, "35002", 35)              # expiry rollover
    assert days == 1 and "rollover" in note


def test_semantic_classification_tiers():
    assert rvol.classify_rvol(3.2)["tier"] == "EARTHQUAKE_SPIKE"
    assert rvol.classify_rvol(2.5)["tier"] == "EARTHQUAKE_SPIKE"
    assert rvol.classify_rvol(1.8)["tier"] == "ABOVE_AVERAGE"
    assert rvol.classify_rvol(1.5)["tier"] == "ABOVE_AVERAGE"
    assert rvol.classify_rvol(1.2)["tier"] == "NORMAL_PARTICIPATION"
    assert rvol.classify_rvol(0.8)["tier"] == "NORMAL_PARTICIPATION"
    assert rvol.classify_rvol(0.5)["tier"] == "ASLEEP_LULL"
    assert rvol.classify_rvol(None)["tier"] == "UNKNOWN"


def test_breakout_authenticity_verdicts():
    real = rvol.breakout_authenticity(2.2, 1.6)
    assert real["is_real_breakout"] is True
    assert real["status"] == "REAL_BREAKOUT_FUEL"

    fake = rvol.breakout_authenticity(0.6, 0.7)
    assert fake["is_real_breakout"] is False
    assert fake["status"] == "FAKEOUT_WARNING"

    neutral = rvol.breakout_authenticity(1.2, 1.1)
    assert neutral["is_real_breakout"] is None
    assert neutral["status"] == "NEUTRAL_FLOW"
