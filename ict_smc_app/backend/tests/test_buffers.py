"""
tests/test_buffers.py — Unit tests for zero-copy in-memory ring buffers.
"""
import pytest
from app.market.buffers import CandleRingBuffer, TickRingBuffer
from app.market.candles import Candles


def test_candle_ring_buffer_basic_append_and_update():
    buf = CandleRingBuffer(maxlen=5)
    assert len(buf) == 0

    c1 = {"ts": 1000, "o": 100.0, "h": 105.0, "l": 99.0, "c": 104.0, "v": 10.0}
    buf.append(c1)
    assert len(buf) == 1
    assert buf.latest_ts() == 1000

    # Same timestamp updates the candle in-place
    c1_updated = {"ts": 1000, "o": 100.0, "h": 110.0, "l": 99.0, "c": 109.0, "v": 20.0}
    buf.append(c1_updated)
    assert len(buf) == 1
    assert buf.last_n(1)[0]["h"] == 110.0

    # New timestamp appends
    c2 = {"ts": 1060, "o": 109.0, "h": 112.0, "l": 108.0, "c": 111.0, "v": 15.0}
    buf.append(c2)
    assert len(buf) == 2
    assert buf.latest_ts() == 1060


def test_candle_ring_buffer_capacity_truncation():
    buf = CandleRingBuffer(maxlen=3)
    for i in range(5):
        buf.append({"ts": 1000 + i * 60, "o": 100.0 + i, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1.0})

    assert len(buf) == 3
    assert buf.earliest_ts() == 1000 + 2 * 60
    assert buf.latest_ts() == 1000 + 4 * 60


def test_candle_ring_buffer_slice_and_last_n():
    buf = CandleRingBuffer(maxlen=10)
    for i in range(5):
        buf.append({"ts": 1000 + i * 60, "o": 100.0 + i, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1.0})

    # Slice with since
    res = buf.slice(1000 + 2 * 60)
    assert len(res) == 3
    assert res[0]["ts"] == 1000 + 2 * 60

    # Slice with since and until
    res2 = buf.slice(1000 + 1 * 60, 1000 + 3 * 60)
    assert len(res2) == 3

    # Last n
    last2 = buf.last_n(2)
    assert len(last2) == 2
    assert last2[-1]["ts"] == 1000 + 4 * 60


def test_tick_ring_buffer():
    tbuf = TickRingBuffer(maxlen=4)
    assert len(tbuf) == 0

    tbuf.append("13", 24000.0, 1000.1)
    tbuf.append("13", 24001.0, 1000.2)
    tbuf.append("21", 13.5, 1000.3)
    tbuf.append("13", 24002.5, 1000.4)
    tbuf.append("13", 24003.0, 1000.5)  # Truncates oldest

    assert len(tbuf) == 4
    assert tbuf.last_price("13") == 24003.0
    assert tbuf.last_price("21") == 13.5

    ticks_13 = tbuf.last_ticks("13", n=10)
    assert len(ticks_13) == 3
    assert ticks_13[-1][1] == 24003.0


@pytest.mark.anyio
async def test_candles_integration_with_ring_buffer():
    closed_candles = []

    async def on_close(sid, c):
        closed_candles.append((sid, c))

    candles = Candles(on_close)
    c_buf = candles.get_candle_buffer("13")
    assert len(c_buf) == 0

    # Hydrate with sample candles
    historical = [
        {"ts": 1700000000 + i * 60, "o": 22000.0 + i, "h": 22010.0, "l": 21990.0, "c": 22005.0, "v": 100.0}
        for i in range(10)
    ]
    candles.hydrate_buffer("13", historical)
    assert len(c_buf) == 10

    # Query frame using memory buffer
    rows = c_buf.slice(1700000000 + 5 * 60)
    assert len(rows) == 5


def test_candle_ring_buffer_dataframe_caching_and_limit():
    buf = CandleRingBuffer(maxlen=10)
    for i in range(5):
        buf.append({"ts": 1000 + i * 60, "o": 100.0 + i, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1.0})

    df1 = buf.to_dataframe()
    assert df1 is not None
    assert len(df1) == 5

    # Repeated call returns the exact same cached DataFrame object without re-parsing
    df2 = buf.to_dataframe()
    assert df1 is df2

    # Limit parameter returns tail window
    df_limit = buf.to_dataframe(limit=2)
    assert len(df_limit) == 2
    assert int(df_limit.iloc[-1]["ts"]) == 1000 + 4 * 60

    # Appending invalidates cache
    buf.append({"ts": 1000 + 5 * 60, "o": 106.0, "h": 108.0, "l": 104.0, "c": 107.0, "v": 1.0})
    df3 = buf.to_dataframe()
    assert df3 is not df1
    assert len(df3) == 6


def test_candle_ring_buffer_write_behind_dirty_staging():
    buf = CandleRingBuffer(maxlen=10)
    assert not buf.has_dirty()

    c = {"ts": 1000, "o": 100.0, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1.0}
    buf.append(c, stage_dirty=True)
    assert buf.has_dirty()

    dirty = buf.get_and_clear_dirty()
    assert len(dirty) == 1
    assert dirty[0]["ts"] == 1000
    assert not buf.has_dirty()


@pytest.mark.anyio
async def test_candles_write_behind_flush():
    async def on_close(sid, c):
        pass

    candles = Candles(on_close)
    c1 = {"ts": 1700000000, "o": 22000.0, "h": 22010.0, "l": 21990.0, "c": 22005.0, "v": 10.0}
    candles.get_candle_buffer("13").append(c1, stage_dirty=True)

    n = candles.flush_write_behind()
    assert n == 1
    # Second flush should have 0 dirty
    assert candles.flush_write_behind() == 0

