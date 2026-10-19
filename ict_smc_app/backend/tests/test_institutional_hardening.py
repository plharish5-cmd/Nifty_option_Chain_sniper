"""
tests/test_institutional_hardening.py — Phase 3 Institutional Hardening Unit Tests.

Covers:
1. Zero-Copy Read Safety & Copy-on-Write isolation in CandleRingBuffer.
2. In-memory _frame_cache bounds & version-based eviction in Candles.
3. Monotonic WebSocket sequence watermarking, replay buffer, and /api/replay endpoint.
4. Option chain staleness watermarks (age and spot delta) and confluence skipping.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))

import pytest
from starlette.testclient import TestClient

from app import config
from app.market.buffers import CandleRingBuffer
from app.market.candles import Candles
from app.api.hub import Hub
from app.api.main import app
from app.market import chain
from app.delta import confluence
from app.signals.playbook import Signal
from app.timeutil import now_ist, session_bounds


def test_dataframe_cow_isolation():
    """Verify that downstream modification of to_dataframe() output does NOT mutate internal buffer cache."""
    buf = CandleRingBuffer(maxlen=100)
    for i in range(5):
        buf.append({"ts": 1700000000 + i * 60, "o": 22000.0 + i, "h": 22010.0 + i, "l": 21990.0 + i, "c": 22005.0 + i, "v": 100.0})

    df1 = buf.to_dataframe()
    assert df1 is not None
    assert len(df1) == 5
    assert "injected_col" not in df1.columns

    # Mutate df1 by adding a column and altering a cell
    df1["injected_col"] = 999.0
    df1.iloc[0, 1] = 99999.0  # open price

    # Obtain a fresh DataFrame view from the buffer
    df2 = buf.to_dataframe()
    assert "injected_col" not in df2.columns
    assert df2.iloc[0, 1] == 22000.0  # original open price remains untainted


@pytest.mark.anyio
async def test_frame_cache_version_eviction_and_max_bounds():
    """Verify that _frame_cache evicts stale version keys and respects max_frame_cache_entries."""
    async def dummy_close(sid, c):
        pass

    c = Candles(dummy_close)
    sid = "13"
    from datetime import timedelta
    yesterday = now_ist().date() - timedelta(days=1)
    if yesterday.weekday() >= 5:
        yesterday -= timedelta(days=2)
    o, _ = session_bounds(yesterday)
    base_ts = int(o) + 300  # 09:20 IST yesterday (closed in-session candles)

    # Hydrate buffer with initial rows
    rows = [{"ts": base_ts + i * 60, "o": 22000.0, "h": 22010.0, "l": 21990.0, "c": 22005.0, "v": 100.0} for i in range(10)]
    c.hydrate_buffer(sid, rows)
    buf = c.get_candle_buffer(sid)
    v0 = buf.version

    # Request frame
    f1 = c.frame(sid, "1m", days=1, include_forming=False)
    assert len(f1) == 10
    keys_v0 = [k for k in c._frame_cache.keys() if k[0] == sid and k[5] == v0]
    assert len(keys_v0) >= 1

    # Append a new candle: increments buf.version
    buf.append({"ts": base_ts + 10 * 60, "o": 22005.0, "h": 22015.0, "l": 22000.0, "c": 22010.0, "v": 150.0})
    v1 = buf.version
    assert v1 > v0

    # Requesting frame with new version should purge the old version key
    f2 = c.frame(sid, "1m", days=1, include_forming=False)
    assert len(f2) == 11
    # Verify stale v0 key was evicted
    assert all(k[5] >= v1 for k in c._frame_cache.keys() if k[0] == sid)


@pytest.mark.anyio
async def test_ws_monotonic_seq_and_replay_buffer():
    """Verify Hub.broadcast stamps monotonic seq integers and stores them in _replay_buffer."""
    hub = Hub()
    assert hub._ws_seq == 0
    assert len(hub._replay_buffer) == 0

    await hub.broadcast({"type": "quote", "msg": "a"})
    await hub.broadcast({"type": "quote", "msg": "b"})
    await hub.broadcast({"type": "alert", "msg": "c"})

    assert hub._ws_seq == 3
    assert len(hub._replay_buffer) == 3
    assert [m["seq"] for m in hub._replay_buffer] == [1, 2, 3]

    # Test replay query
    replayed = hub.get_replay(from_seq=2)
    assert len(replayed) == 2
    assert [m["seq"] for m in replayed] == [2, 3]


@pytest.mark.anyio
async def test_api_replay_endpoint():
    """Verify GET /api/replay endpoint returns missed messages."""
    import app.api.main as m
    if m.hub is None:
        m.hub = Hub()

    await m.hub.broadcast({"type": "alert", "title": "Test Replay"})
    cur_seq = m.hub._ws_seq

    client = TestClient(app)
    res = client.get(f"/api/replay?from_seq={cur_seq}")
    assert res.status_code == 200
    data = res.json()
    assert data["from_seq"] == cur_seq
    assert len(data["messages"]) >= 1
    assert data["messages"][-1]["seq"] == cur_seq
    assert data["messages"][-1]["type"] == "alert"


def test_option_chain_staleness_watermarks():
    """Verify fetched_at and spot_at_fetch stamping and is_stale calculation."""
    raw = {
        "last_price": 22500.0,
        "oc": {
            "22500": {
                "ce": {"last_price": 100.0, "previous_close_price": 90.0, "oi": 50000, "previous_oi": 45000},
                "pe": {"last_price": 95.0, "previous_close_price": 85.0, "oi": 48000, "previous_oi": 42000},
            }
        }
    }
    expiry = "2026-10-20"
    ch = chain.normalize(raw, expiry)

    assert "fetched_at" in ch
    assert ch["spot_at_fetch"] == 22500.0
    assert ch["is_stale"] is False

    poller = chain.ChainPoller(None, None)
    poller.chains[expiry] = dict(ch)

    # 1. Fresh chain and spot close to fetch spot (within oc_stale_spot_delta: 40 pts)
    assert poller.is_stale(expiry, live_spot=22510.0) is False

    # 2. Spot drifted by > 40 points
    assert poller.is_stale(expiry, live_spot=22550.0) is True  # delta = 50 > 40
    assert poller.is_stale(expiry, live_spot=22440.0) is True  # delta = 60 > 40

    # 3. Time elapsed > oc_max_age_s: 20 seconds
    poller.chains[expiry]["fetched_at"] = time.time() - 25.0
    assert poller.is_stale(expiry, live_spot=22505.0) is True

    # 4. Summary includes watermarks
    summ = poller.summary(expiry, live_spot=22505.0)
    assert summ is not None
    assert summ["is_stale"] is True
    assert "fetched_at" in summ
    assert "spot_at_fetch" in summ


def test_confluence_skips_stale_option_context():
    """Verify confluence scoring ignores option context when marked as stale."""
    sig = Signal(
        key="test_sig", setup="A", name="Turtle Soup Long", tf="5m", side="long", i=10, ts=1700000000, status="TRADE",
        entry=22500.0, sl=22480.0, tp=22550.0, risk=20.0, rr=2.5, target_src="Liquidity Pool",
        reasons=["sweep", "mss"], story=["swept PDL"]
    )

    foci = [{"label": "22500 CE", "available": True, "line": "long build", "lean": "supportive"}]

    # Case A: Fresh context -> scores option context
    confluence.apply([sig], delta_res={"available": False, "events": []},
                     context={"foci": foci, "is_stale": False}, live_i=10)
    assert sig.confidence_parts is not None
    assert sig.confidence_parts["options_context"] is not None

    # Case B: Stale context -> skips option context
    sig2 = Signal(
        key="test_sig2", setup="A", name="Turtle Soup Long", tf="5m", side="long", i=10, ts=1700000000, status="TRADE",
        entry=22500.0, sl=22480.0, tp=22550.0, risk=20.0, rr=2.5, target_src="Liquidity Pool",
        reasons=["sweep", "mss"], story=["swept PDL"]
    )
    confluence.apply([sig2], delta_res={"available": False, "events": []},
                     context={"foci": foci, "is_stale": True}, live_i=10)
    assert sig2.confidence_parts is not None
    assert sig2.confidence_parts["options_context"] is None
    assert any("skipped (chain data is stale)" in line for line in sig2.confluence)
