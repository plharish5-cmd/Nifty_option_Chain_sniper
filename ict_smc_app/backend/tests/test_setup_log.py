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


def test_three_minute_bucket_deduplication():
    """Validates that at most 1 canonical row is logged per 3-minute candle for a given (setup, side)."""
    base_ts = 1793248020  # 09:57 IST
    # Simulate 3 ticks arriving inside the same 3m bar
    ticks = [
        {"ts": base_ts + 0, "key": f"{base_ts}|tick1", "setup": "SNIPER BREAKOUT", "side": "bull", "entry": 22745.05, "sl": 22710.0},
        {"ts": base_ts + 30, "key": f"{base_ts}|tick2", "setup": "SNIPER BREAKOUT", "side": "bull", "entry": 22741.15, "sl": 22706.0},
        {"ts": base_ts + 60, "key": f"{base_ts}|tick3", "setup": "SNIPER BREAKOUT", "side": "bull", "entry": 22743.35, "sl": 22708.0},
    ]
    r = client.post("/api/setups/log", json={"setups": ticks})
    assert r.status_code == 200
    assert r.json()["stored"] == 1  # exactly 1 stored, others deduplicated

    from app.timeutil import ist_date
    day = ist_date(base_ts).isoformat()
    rows = client.get("/api/setups", params={"d": day}).json()["setups"]
    breakout_rows = [s for s in rows if (s["ts"] // 180) * 180 == (base_ts // 180) * 180 and s["setup"] == "SNIPER BREAKOUT"]
    assert len(breakout_rows) == 1, f"Expected 1 deduplicated row, found {len(breakout_rows)}"


def test_tp_always_populated_on_sniper_breakout():
    """Validates that directional setups with missing tp automatically compute 2.0R target."""
    ts_val = 1793248560
    key = f"tp-auto-{ts_val}"
    r = client.post("/api/setups/log", json={"setups": [{
        "ts": ts_val,
        "key": key,
        "setup": "SNIPER BREAKOUT",
        "side": "bull",
        "entry": 22750.0,
        "sl": 22720.0,  # 30pt risk
        "tp": None,      # missing tp!
    }]})
    assert r.status_code == 200

    from app.timeutil import ist_date
    day = ist_date(ts_val).isoformat()
    rows = client.get("/api/setups", params={"d": day}).json()["setups"]
    match = [s for s in rows if s["key"] == key]
    assert len(match) == 1
    # 2.0R target: 22750 + 2 * 30 = 22810.0
    assert match[0]["tp"] == 22810.0, f"Expected 22810.0 but got {match[0]['tp']}"


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

