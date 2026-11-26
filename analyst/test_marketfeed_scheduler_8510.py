"""v266 #3: per-account marketfeed arbiter — serialisation, spacing, shared
429 backoff and per-symbol cache independence."""
import asyncio
import time

import pytest

from analyst.marketfeed_scheduler_8510 import AccountGate, get_gate, _reset_for_tests


class _Broker:
    def __init__(self, cid="CID1"):
        self.client_id = cid


def setup_function(_):
    _reset_for_tests()


def test_per_account_gate_is_shared_by_client_id():
    a, b = _Broker("CID1"), _Broker("CID1")
    assert get_gate(a) is get_gate(b)
    c = _Broker("CID2")
    assert get_gate(c) is not get_gate(a)


def test_reservation_serialises_concurrent_senders():
    """Two coroutines reserving the same gate must not overlap, and the second
    must be spaced at least min_interval after the first send."""
    gate = AccountGate(min_interval=0.2)
    order = []

    async def sender(tag):
        async with gate.slot():
            order.append((tag, "start", time.monotonic()))
            await asyncio.sleep(0.05)  # simulated HTTP
            order.append((tag, "end", time.monotonic()))

    async def run():
        await asyncio.gather(sender("A"), sender("B"))

    asyncio.run(run())
    # No overlap: first end precedes second start.
    starts = [t for (_, k, t) in order if k == "start"]
    ends = [t for (_, k, t) in order if k == "end"]
    assert ends[0] <= starts[1] + 1e-6
    # Second send spaced >= min_interval after the first.
    assert starts[1] - starts[0] >= 0.2 - 1e-3


def test_shared_429_backoff_blocks_all_callers():
    gate = AccountGate(min_interval=0.0, backoff_start=5.0)
    assert not gate.in_backoff()
    gate.note_rate_limited()
    assert gate.in_backoff()
    # seconds_until_slot reflects the backoff window, not just spacing.
    assert gate.seconds_until_slot() > 4.0
    gate.note_success()
    assert not gate.in_backoff()


def test_per_symbol_cache_age_is_independent():
    """A spot refresh must not renew an option quote's freshness."""
    gate = AccountGate()
    t0 = time.monotonic()
    gate.store_prices({"13": 24000.0}, now_mono=t0 - 10.0)   # stale spot
    gate.store_prices({"61471": 150.0}, now_mono=t0)          # fresh option
    # Option alone is fresh within ttl.
    assert gate.cached_prices(["61471"], ttl=5.0, now_mono=t0) == {"61471": 150.0}
    # Spot alone is stale -> None (not renewed by the option write).
    assert gate.cached_prices(["13"], ttl=5.0, now_mono=t0) is None
    # Mixed request: any stale member -> None.
    assert gate.cached_prices(["13", "61471"], ttl=5.0, now_mono=t0) is None


def test_backoff_short_circuits_entry_lookup(monkeypatch):
    """entry_rest_ltp_lookup must not hit HTTP while the shared gate is in backoff."""
    import analyst.dhan_spot_rest_fallback_8510 as mod

    broker = _Broker("CID9")
    broker.is_configured = lambda: True
    broker.base_url = "https://api.dhan.co/v2"
    broker._headers = lambda: {}

    gate = get_gate(broker, min_interval=mod.MIN_REST_INTERVAL_SEC)
    gate.note_rate_limited()

    called = {"http": False}

    def _boom(*a, **k):
        called["http"] = True
        raise AssertionError("HTTP must not be attempted during backoff")

    monkeypatch.setattr(mod, "ipv4_async_client", _boom, raising=False)

    prices, err = asyncio.run(mod.entry_rest_ltp_lookup(broker, {"IDX_I": [13]}))
    assert prices == {}
    assert err == "rate limited (backoff)"
    assert called["http"] is False
