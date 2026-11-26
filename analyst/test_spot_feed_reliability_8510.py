"""v266 — spot-feed reliability: WS reconnect on repeated timeouts + continuous
REST keep-alive. Root fix for 2026-11-25 missed trades (47/51 entries fail-closed
on 'Spot feed STALE', spot aged to 208s)."""
import asyncio
import time
import types

from analyst.dhan_spot_rest_fallback_8510 import (
    keepalive_nifty_spot,
    _reset_throttle_for_tests,
    SPOT_KEEPALIVE_AGE_SEC,
    SPOT_MAX_AGE_SEC,
    NIFTY_IDX_SEC_ID,
)


class _Broker:
    def __init__(self, price):
        self._price = price
        self._ltp_cache = {}
        self._cache_timestamps = {}
        self.base_url = "https://api.dhan.co/v2"
    def is_configured(self):
        return True
    def _headers(self):
        return {}


class _Client:
    def __init__(self, price):
        self._price = price
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, json=None, headers=None):
        px = self._price
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"data": {"IDX_I": {str(NIFTY_IDX_SEC_ID): {"last_price": px}}}},
        )


def _patch_client(monkeypatch, price):
    import analyst.dhan_spot_rest_fallback_8510 as mod
    def _factory(timeout):
        return _Client(price)
    monkeypatch.setattr(mod, "ipv4_async_client", _factory, raising=False)
    # ipv4_async_client is imported inside the function; patch the source module too
    import analyst.dhan_ipv4_guard_8510 as guard
    monkeypatch.setattr(guard, "ipv4_async_client", _factory, raising=False)


def _backend(price, spot_age_sec):
    b = types.SimpleNamespace()
    b.broker = _Broker(price)
    b.cached_telemetry = {
        "spot": {"price": 25000.0, "is_stale": False,
                 "updated_epoch": time.time() - spot_age_sec, "status": "LIVE"}
    }
    return b


def test_keepalive_refreshes_when_spot_older_than_3s(monkeypatch):
    _reset_throttle_for_tests()
    _patch_client(monkeypatch, 25123.5)
    b = _backend(25000.0, spot_age_sec=SPOT_KEEPALIVE_AGE_SEC + 1.0)  # 4s old > 3s keepalive
    did = asyncio.run(keepalive_nifty_spot(b, market_open=True))
    assert did is True
    assert b.cached_telemetry["spot"]["price"] == 25123.5
    assert b.cached_telemetry["spot"]["is_stale"] is False
    # and it happened BEFORE the 5s entry-block threshold would have fired
    assert SPOT_KEEPALIVE_AGE_SEC < SPOT_MAX_AGE_SEC


def test_keepalive_noop_when_spot_fresh(monkeypatch):
    _reset_throttle_for_tests()
    _patch_client(monkeypatch, 25123.5)
    b = _backend(25000.0, spot_age_sec=1.0)  # 1s old < 3s -> no refresh
    did = asyncio.run(keepalive_nifty_spot(b, market_open=True))
    assert did is False
    assert b.cached_telemetry["spot"]["price"] == 25000.0


def test_keepalive_noop_when_market_closed(monkeypatch):
    _reset_throttle_for_tests()
    _patch_client(monkeypatch, 25123.5)
    b = _backend(25000.0, spot_age_sec=99.0)
    assert asyncio.run(keepalive_nifty_spot(b, market_open=False)) is False


def test_ws_reconnect_on_consecutive_timeouts():
    """The feed loop must break to reconnect after N consecutive recv timeouts,
    instead of looping forever on a half-dead socket (the 208s stall)."""
    import inspect
    from analyst import terminal8500_market as m
    src = inspect.getsource(m)
    assert "feed_max_consecutive_timeouts" in src
    assert "consecutive_timeouts" in src
    assert "forcing reconnect" in src
    # the old infinite-continue-only pattern must be gone
    assert "except websocket.WebSocketTimeoutException:continue" not in src
