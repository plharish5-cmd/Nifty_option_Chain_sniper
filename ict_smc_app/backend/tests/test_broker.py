"""
tests/test_broker.py — Unit tests for the async pub/sub decoupling broker.
"""
import asyncio
import pytest
from app.engine.broker import AsyncBroker


@pytest.mark.anyio
async def test_async_broker_in_memory_pubsub():
    broker = AsyncBroker()
    # Force memory mode for deterministic testing without external redis dependency
    broker.mode = "memory"
    broker._redis_client = None

    received_ticks = []
    received_signals = []

    async def handle_tick(data):
        received_ticks.append(data)

    async def handle_signal(data):
        received_signals.append(data)

    broker.subscribe("test:ticks", handle_tick)
    broker.subscribe("test:signals", handle_signal)

    await broker.start()
    assert broker.is_running
    assert broker.status()["mode"] == "memory"

    # Publish messages
    await broker.publish("test:ticks", {"sid": "13", "ltp": 24150.0, "t": 1000.0})
    await broker.publish("test:ticks", {"sid": "13", "ltp": 24152.0, "t": 1001.0})
    await broker.publish("test:signals", {"type": "candle_close", "c": 24152.0})

    # Allow worker consumer loops to process queue
    await asyncio.sleep(0.1)

    assert len(received_ticks) == 2
    assert received_ticks[0]["ltp"] == 24150.0
    assert received_ticks[1]["ltp"] == 24152.0

    assert len(received_signals) == 1
    assert received_signals[0]["c"] == 24152.0

    status = broker.status()
    assert status["published"] == 3
    assert status["processed"] == 3

    await broker.stop()
    assert not broker.is_running


@pytest.mark.anyio
async def test_async_broker_serialization_roundtrip():
    broker = AsyncBroker()
    sample = {"sid": "13", "nested": {"val": 123.45, "flags": [True, False]}, "str": "hello"}
    serialized = broker._serialize(sample)
    deserialized = broker._deserialize(serialized)
    assert deserialized["sid"] == "13"
    assert deserialized["nested"]["val"] == 123.45
    assert deserialized["nested"]["flags"] == [True, False]
