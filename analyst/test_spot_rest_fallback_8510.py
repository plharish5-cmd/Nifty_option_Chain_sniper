"""Dhan REST spot/option LTP fallback at entry when WebSocket ages > 5s."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from analyst.dhan_spot_rest_fallback_8510 import (
    _reset_throttle_for_tests,
    entry_rest_ltp_lookup,
    recover_nifty_spot_for_entry,
    spot_blob_needs_rest_fallback,
)
from analyst.offhours_cleanup_8510 import new_position_entry_allowed


def _broker() -> MagicMock:
    b = MagicMock()
    b.is_configured.return_value = True
    b.base_url = "https://api.dhan.co/v2"
    b._headers.return_value = {"Authorization": "x"}
    b._ltp_cache = {}
    b._cache_timestamps = {}
    return b


def test_fresh_websocket_spot_does_not_call_rest():
    _reset_throttle_for_tests()
    blob = {"price": 25870.0, "is_stale": False, "updated_epoch": time.time()}
    needs, _ = spot_blob_needs_rest_fallback(blob)
    assert needs is False
    ok, why = new_position_entry_allowed(
        market_open=True, market_status="OPEN", spot_blob=blob
    )
    assert ok and not why


def test_stale_spot_recovers_via_rest():
    _reset_throttle_for_tests()
    backend = SimpleNamespace(
        cached_telemetry={
            "spot": {
                "price": 25870.0,
                "is_stale": False,
                "updated_epoch": time.time() - 16.6,
            }
        },
        broker=_broker(),
    )
    with patch(
        "analyst.dhan_spot_rest_fallback_8510.entry_rest_ltp_lookup",
        new_callable=AsyncMock,
        return_value=({"13": 25872.35}, None),
    ) as rest:
        ok, msg = asyncio.run(recover_nifty_spot_for_entry(backend, market_open=True))
    rest.assert_awaited_once()
    assert ok is True
    assert "Spot recovered via Dhan REST fallback" in msg
    assert backend.cached_telemetry["spot"]["is_stale"] is False
    assert float(backend.cached_telemetry["spot"]["price"]) == 25872.35


def test_stale_spot_rest_failure_stays_fail_closed():
    _reset_throttle_for_tests()
    backend = SimpleNamespace(
        cached_telemetry={"spot": {"price": 25870.0, "updated_epoch": time.time() - 12.0}},
        broker=_broker(),
    )
    with patch(
        "analyst.dhan_spot_rest_fallback_8510.entry_rest_ltp_lookup",
        new_callable=AsyncMock,
        return_value=({}, "RL001"),
    ):
        ok, why = asyncio.run(recover_nifty_spot_for_entry(backend, market_open=True))
    assert ok is False
    assert "REST fallback failed" in why


def test_fresh_spot_skips_rest_lookup():
    _reset_throttle_for_tests()
    backend = SimpleNamespace(
        cached_telemetry={
            "spot": {"price": 25870.0, "is_stale": False, "updated_epoch": time.time()}
        },
        broker=_broker(),
    )
    with patch(
        "analyst.dhan_spot_rest_fallback_8510.entry_rest_ltp_lookup",
        new_callable=AsyncMock,
    ) as rest:
        ok, msg = asyncio.run(recover_nifty_spot_for_entry(backend, market_open=True))
    rest.assert_not_awaited()
    assert ok is True and msg == ""


def test_rate_limit_throttle_reuses_cache_without_second_http():
    _reset_throttle_for_tests()
    broker = _broker()
    calls = {"n": 0}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": {"IDX_I": {"13": {"last_price": 25800.0}}}}

    async def fake_post(*_a, **_k):
        calls["n"] += 1
        return FakeResp()

    with patch("analyst.dhan_ipv4_guard_8510.ipv4_async_client") as mcc:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.post = fake_post
        mcc.return_value = client

        p1, e1 = asyncio.run(entry_rest_ltp_lookup(broker, {"IDX_I": [13]}))
        assert e1 is None and p1["13"] == 25800.0
        assert calls["n"] == 1

        p2, e2 = asyncio.run(entry_rest_ltp_lookup(broker, {"IDX_I": [13]}))
        assert e2 is None and p2["13"] == 25800.0
        assert calls["n"] == 1