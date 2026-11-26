"""Per-account Dhan /v2/marketfeed/* request arbiter (v266 #3).

Every in-process caller of the marketfeed REST endpoints — the background spot
keep-alive, the entry spot/option recovery, the paper-quote fallback and the
DhanBroker LTP/quote wrappers — shares ONE account's 1-request-per-second
budget. Without a shared arbiter each path throttled independently, so two
coroutines could both pass their own "last call was >1s ago" check and fire in
the same second, tripping Dhan's RL001 (429). They also each kept their own
cache, so a spot refresh could renew an option quote's freshness timestamp.

This module gives each account:
  * an asyncio.Lock so sends are serialised inside the engine's event loop;
  * a single min-interval spacing shared by all callers;
  * a shared 429 backoff window every caller honours;
  * a PER-SYMBOL price cache (so a spot refresh never renews an option's age).

It holds no order state and imports nothing from the protected execution path.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

DEFAULT_MIN_INTERVAL_SEC = 1.0
DEFAULT_BACKOFF_START_SEC = 3.0
DEFAULT_BACKOFF_MAX_SEC = 15.0


class AccountGate:
    """Serialises and rate-limits marketfeed REST for a single Dhan account."""

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL_SEC,
        backoff_start: float = DEFAULT_BACKOFF_START_SEC,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SEC,
    ) -> None:
        self.min_interval = float(min_interval)
        self._backoff_start = float(backoff_start)
        self._backoff_max = float(backoff_max)
        self._lock = asyncio.Lock()
        self._last_send_mono: float = 0.0
        self._backoff_until_mono: float = 0.0
        self._backoff_delay: float = float(backoff_start)
        # Per-symbol cache keyed by security id string.
        self._price: Dict[str, float] = {}
        self._price_at_mono: Dict[str, float] = {}

    # --- shared state queries (safe to call without the lock) ---
    def in_backoff(self, now_mono: Optional[float] = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        return now < self._backoff_until_mono

    def cached_prices(self, sids, ttl: float, now_mono: Optional[float] = None):
        """Return {sid: price} for every requested sid whose cache is within ttl,
        or None if ANY requested sid is missing/stale (per-symbol age)."""
        now = time.monotonic() if now_mono is None else now_mono
        out: Dict[str, float] = {}
        for sid in sids:
            s = str(sid)
            ts = self._price_at_mono.get(s)
            if ts is None or (now - ts) > ttl or s not in self._price:
                return None
            out[s] = self._price[s]
        return out or None

    def store_prices(self, prices: Dict[str, float], now_mono: Optional[float] = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        for sid, px in prices.items():
            self._price[str(sid)] = float(px)
            self._price_at_mono[str(sid)] = now

    def note_rate_limited(self, now_mono: Optional[float] = None) -> None:
        """Record a 429 so every caller backs off the shared window."""
        now = time.monotonic() if now_mono is None else now_mono
        self._backoff_until_mono = now + self._backoff_delay
        self._backoff_delay = min(self._backoff_delay + 2.0, self._backoff_max)

    def note_success(self) -> None:
        self._backoff_delay = self._backoff_start
        self._backoff_until_mono = 0.0

    def note_external_send(self, now_mono: Optional[float] = None) -> None:
        """A caller that does its own HTTP (e.g. DhanBroker) registers its send
        here so other callers space off the same budget."""
        now = time.monotonic() if now_mono is None else now_mono
        self._last_send_mono = now

    def seconds_until_slot(self, now_mono: Optional[float] = None) -> float:
        now = time.monotonic() if now_mono is None else now_mono
        wait_space = (self._last_send_mono + self.min_interval) - now
        wait_backoff = self._backoff_until_mono - now
        return max(0.0, wait_space, wait_backoff)

    async def reserve(self) -> None:
        """Acquire the account lock and reserve a send slot, waiting out the
        min-interval spacing and any shared backoff. Caller MUST call release()
        (via `async with gate.slot():`) after its HTTP completes."""
        await self._lock.acquire()
        try:
            wait = self.seconds_until_slot()
            if wait > 0:
                await asyncio.sleep(wait)
            # Mark the send time INSIDE the lock so a second coroutine that
            # acquires next spaces off this reservation, not the previous one.
            self._last_send_mono = time.monotonic()
        except BaseException:
            self._lock.release()
            raise

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()

    def slot(self) -> "_Slot":
        return _Slot(self)


class _Slot:
    def __init__(self, gate: AccountGate) -> None:
        self._gate = gate

    async def __aenter__(self) -> AccountGate:
        await self._gate.reserve()
        return self._gate

    async def __aexit__(self, *exc) -> None:
        self._gate.release()


_GATES: Dict[str, AccountGate] = {}


def account_key(broker: Any) -> str:
    for attr in ("client_id", "_client_id", "clientId", "dhan_client_id"):
        v = getattr(broker, attr, None)
        if v:
            return str(v)
    return f"id:{id(broker)}"


def get_gate(broker: Any, *, min_interval: float = DEFAULT_MIN_INTERVAL_SEC) -> AccountGate:
    key = account_key(broker)
    gate = _GATES.get(key)
    if gate is None:
        gate = AccountGate(min_interval=min_interval)
        _GATES[key] = gate
    return gate


def _reset_for_tests() -> None:
    _GATES.clear()
