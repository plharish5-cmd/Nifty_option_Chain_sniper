"""
tests/test_setup_log.py — /api/setups records what OcLab fired, so the conviction score can eventually
be measured instead of argued about.

The browser recomputes and re-posts the same setups on every poll, so the critical property is that a
re-post is a no-op: the UNIQUE key must absorb duplicates server-side rather than relying on the client
to remember what it already sent.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

from app.api.setup_log_api import router  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(router)
client = TestClient(app)

# 2026-10-28 13:09 IST — the DIP BUY that was forced to 95% by putAngleDeg +29 and bought 10 minutes
# after the session high.
TS = 1793173140          # verified: to_ist(1793173140) == 2026-10-28 13:09:00+05:30


def _setup(ts=TS, entry=22726.4, key=None):
    return {
        "ts": ts,
        "key": key or f"{ts}|DIP_BUY|bull|{entry}",
        "setup": "DIP BUY",
        "side": "bull",
        "entry": entry,
        "sl": 22691.0,
        "tp": 22789.0,
        "conviction_pct": 95,
        "conviction_band": "A+",
        "synergy": True,
        "scissor": True,
        "spot": 22726.4,
        "payload": {"note": "putAngleDeg +29"},
    }


def test_setup_is_stored_and_readable():
    r = client.post("/api/setups/log", json={"setups": [_setup()]})
    assert r.status_code == 200, r.text
    assert r.json()["received"] == 1

    day = client.get("/api/setups", params={"d": "2026-10-28"}).json()
    assert day["ok"] is True
    mine = [s for s in day["setups"] if s["key"] == _setup()["key"]]
    assert len(mine) == 1, "the setup should come back exactly once"
    got = mine[0]
    assert got["setup"] == "DIP BUY"
    assert got["conviction_pct"] == 95
    assert got["sl"] == 22691.0 and got["tp"] == 22789.0
    assert got["synergy"] == 1 and got["scissor"] == 1


def test_reposting_the_same_setup_is_a_no_op():
    """OcLab re-renders constantly; a duplicate post must not create a second row."""
    s = _setup(entry=22735.8, key="dup-guard-13-12")
    client.post("/api/setups/log", json={"setups": [s]})
    before = client.get("/api/setups", params={"d": "2026-10-28"}).json()["count"]

    second = client.post("/api/setups/log", json={"setups": [s, s, s]})
    assert second.json()["stored"] == 0, "re-posts must be absorbed by the UNIQUE key"

    after = client.get("/api/setups", params={"d": "2026-10-28"}).json()["count"]
    assert after == before


def test_empty_batch_is_accepted():
    r = client.post("/api/setups/log", json={"setups": []})
    assert r.status_code == 200
    assert r.json()["stored"] == 0


def test_batch_is_capped():
    """A single poll cannot legitimately produce hundreds of setups — a runaway client must not be able
    to flood the table."""
    many = [_setup(ts=TS + i, key=f"flood-{i}") for i in range(120)]
    r = client.post("/api/setups/log", json={"setups": many})
    assert r.json()["received"] <= 50


def test_days_endpoint_counts_sessions():
    client.post("/api/setups/log", json={"setups": [_setup(key="days-probe")]})
    days = client.get("/api/setups/days").json()
    assert days["ok"] is True
    assert days["sessions"] >= 1
    assert any(d["d"] == "2026-10-28" for d in days["days"])


def test_hhmm_is_resolved_server_side():
    """The browser's rows carry only "HH:MM". The server must turn that into a real timestamp rather than
    the client doing timezone arithmetic that breaks around midnight."""
    from app.timeutil import ist_date, now_ist

    key = f"hhmm-resolve-{int(time.time() * 1000)}"
    r = client.post("/api/setups/log", json={"setups": [{
        "key": key, "hhmm": "13:09", "setup": "DIP BUY", "side": "bull",
        "entry": 22726.4, "sl": 22691.0, "conviction_pct": 95,
    }]})
    assert r.status_code == 200, r.text

    today = ist_date(int(now_ist().timestamp())).isoformat()
    rows = client.get("/api/setups", params={"d": today}).json()["setups"]
    got = [s for s in rows if s["key"] == key]
    assert len(got) == 1, "setup should be filed under today's IST session"
    assert got[0]["d"] == today


def test_unparseable_hhmm_does_not_store_a_nonsense_timestamp():
    # Unique key per run: a fixed one made this test depend on starting from an empty table, which is
    # precisely the fragility conftest.py exists to remove — no test should need it to pass.
    key = f"hhmm-garbage-{int(time.time() * 1000)}"
    r = client.post("/api/setups/log", json={"setups": [{
        "key": key, "hhmm": "xx:yy", "setup": "DIP BUY", "side": "bull", "entry": 1.0,
    }]})
    assert r.status_code == 200
    assert r.json()["stored"] == 1          # stored, stamped now, and warned — never silently dropped


def test_truncated_sl_reconstructed_to_5_digits():
    """Validates that a 4-digit truncated SL (e.g. 2796.0) is reconstructed to 5-digit price (22796.0)."""
    ts_val = 1793260080  # 13:18 IST
    key = f"sl-fix-{ts_val}"
    r = client.post("/api/setups/log", json={"setups": [{
        "ts": ts_val,
        "key": key,
        "setup": "DIP BUY CE",
        "side": "bull",
        "entry": 22828.5,
        "sl": 2796.0,  # truncated: missing leading 2!
        "tp": 22888.0,
        "payload": {"risk_pts": 33},
    }]})
    assert r.status_code == 200

    from app.timeutil import ist_date
    day = ist_date(ts_val).isoformat()
    rows = client.get("/api/setups", params={"d": day}).json()["setups"]
    match = [s for s in rows if s["key"] == key]
    assert len(match) == 1
    assert match[0]["sl"] == 22796.0, f"Expected 22796.0 but got {match[0]['sl']}"


def test_one_row_per_bar_even_when_the_browser_reposts():
    """The client re-posts the same closed bar on every poll; the bar is one signal, so one row.

    The ts is derived from the bar's "HH:MM", so every re-post of a bar carries the SAME ts. Dedup is
    therefore on the bar itself -- (ts, setup, side) -- which is exactly the client's own key.
    """
    base_ts = 1793248020  # 09:57 IST
    reposts = [
        {"ts": base_ts, "key": f"{base_ts}|SNIPER BREAKOUT|bull", "setup": "SNIPER BREAKOUT",
         "side": "bull", "entry": 22745.05, "sl": 22710.0, "tp": 22815.1},
    ] * 3
    r = client.post("/api/setups/log", json={"setups": reposts})
    assert r.status_code == 200
    assert r.json()["stored"] == 1, "a re-post of the same bar must be a no-op"

    from app.timeutil import ist_date
    day = ist_date(base_ts).isoformat()
    rows = client.get("/api/setups", params={"d": day}).json()["setups"]
    assert len([s for s in rows if s["ts"] == base_ts and s["setup"] == "SNIPER BREAKOUT"]) == 1


def test_consecutive_one_minute_bars_are_not_merged():
    """Regression: bucketing on (ts // 180) collapsed three 1m bars into one 3m bucket, so two real
    signals out of every three were silently dropped on the 1m timeframe."""
    base_ts = 1793250000 - (1793250000 % 60)
    bars = [{"ts": base_ts + i * 60, "key": f"m1-{i}-{base_ts}", "setup": "DIP BUY CE", "side": "bull",
             "entry": 22800.0 + i, "sl": 22780.0 + i, "tp": 22840.0 + i} for i in range(3)]
    r = client.post("/api/setups/log", json={"setups": bars})
    assert r.json()["stored"] == 3, "three distinct 1-minute bars are three distinct signals"


def test_a_conflict_does_not_delete_the_ticket_it_suppressed():
    """Arbitration has to stay falsifiable: if the suppressed ticket is deleted, 'did standing aside
    cost a good trade?' can never be answered, and the veto becomes unmeasurable."""
    base_ts = 1793251200 - (1793251200 % 180)
    client.post("/api/setups/log", json={"setups": [{
        "ts": base_ts, "key": f"keep-brk-{base_ts}", "setup": "SNIPER BREAKOUT", "side": "bull",
        "entry": 22760.0, "sl": 22730.0, "tp": 22820.0}]})
    client.post("/api/setups/log", json={"setups": [{
        "ts": base_ts, "key": f"keep-cfl-{base_ts}", "setup": "CONFLICT", "side": "warn",
        "entry": 22760.0, "sl": 22730.0, "payload": {"suppressed": True}}]})

    from app.timeutil import ist_date
    rows = client.get("/api/setups", params={"d": ist_date(base_ts).isoformat()}).json()["setups"]
    kinds = {s["setup"] for s in rows if s["ts"] == base_ts}
    assert "SNIPER BREAKOUT" in kinds, "the suppressed ticket must survive, or suppression is unmeasurable"
    assert "CONFLICT" in kinds, "the demotion itself must be recorded too"


def test_tp_is_recorded_as_shown_never_invented():
    """The table records what the desk displayed. A ticket shown with no target keeps none, and a
    clamped target is stored as the clamped figure -- not re-derived as a raw 2R extension."""
    ts_val = 1793248560
    r = client.post("/api/setups/log", json={"setups": [{
        "ts": ts_val, "key": f"tp-none-{ts_val}", "setup": "SNIPER BREAKOUT", "side": "bull",
        "entry": 22750.0, "sl": 22720.0, "tp": None,
    }]})
    assert r.status_code == 200

    from app.timeutil import ist_date
    rows = client.get("/api/setups", params={"d": ist_date(ts_val).isoformat()}).json()["setups"]
    got = [s for s in rows if s["key"] == f"tp-none-{ts_val}"][0]
    assert got["tp"] is None, f"no target was shown, so none may be stored (got {got['tp']})"

    # A capped target is kept verbatim: 22867 was displayed, 22888 was the raw extension.
    ts2 = ts_val + 180
    client.post("/api/setups/log", json={"setups": [{
        "ts": ts2, "key": f"tp-capped-{ts2}", "setup": "DIP BUY CE", "side": "bull",
        "entry": 22828.5, "sl": 22796.0, "tp": 22867.0,
    }]})
    rows = client.get("/api/setups", params={"d": ist_date(ts2).isoformat()}).json()["setups"]
    got = [s for s in rows if s["key"] == f"tp-capped-{ts2}"][0]
    assert got["tp"] == 22867.0, "the clamped target the trader saw, not a recomputed 2R"
def test_alert_ist_timestamp_alignment():
    """Validates that alert timestamps with double +5h30m shift are normalized to true UTC epoch."""
    from app.signals.alerts import normalize_alert_ts
    from app.timeutil import to_ist

    # Candle 14:30 IST: True UTC epoch is 1793264400. Shifted was 1793284200 (+19800s).
    shifted_ts = 1793284200
    title = "5m 14:30 · CRT entry SELL"
    normalized = normalize_alert_ts(shifted_ts, title)
    assert normalized == 1793264400
    assert to_ist(normalized).strftime("%H:%M") == "14:30"

