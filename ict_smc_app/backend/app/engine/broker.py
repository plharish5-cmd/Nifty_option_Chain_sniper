"""
engine/broker.py — Lightweight Async Decoupling & Pub/Sub Broker Layer.

Separates raw incoming market tick ingestion from heavy ICT/SMC mathematical
evaluations (BOS, CHOCH, FVG, OB, Liquidity sweeps, Delta, Auction, MMXM, CCT).

Features:
- Dual-mode broker: Redis Pub/Sub if running; seamless zero-overhead in-memory
  asyncio.Queue event bus fallback if Redis is offline.
- Decouples API gateway / feed loops from heavy quant signal calculation workers.
- All configurations read from rules.json under "broker".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

try:
    import msgpack
except ImportError:
    msgpack = None

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

from .. import config

log = logging.getLogger("engine.broker")

Handler = Callable[[Dict[str, Any]], Awaitable[None]]


class AsyncBroker:
    """Decoupled async pub/sub message broker with Redis and in-memory queue fallback."""

    def __init__(self) -> None:
        bcfg = config.rules().get("broker", {})
        self.enabled = bool(bcfg.get("enabled", True))
        self.redis_url = os.environ.get("REDIS_URL") or bcfg.get("redis_url", "redis://localhost:6379/0")
        self.channel_ticks = bcfg.get("channel_ticks", "market:ticks")
        self.channel_signals = bcfg.get("channel_signals", "engine:signals")
        self.queue_max_size = int(bcfg.get("queue_max_size", 50000))
        self.worker_concurrency = int(bcfg.get("worker_concurrency", 2))

        self.mode: str = "memory"  # 'redis' or 'memory'
        self._redis_client: Optional[Any] = None
        self._redis_pubsub: Optional[Any] = None
        self._memory_queues: Dict[str, List[asyncio.Queue]] = {}
        self._handlers: Dict[str, List[Handler]] = {}
        self._consumer_tasks: List[asyncio.Task] = []
        self._running = False
        self._published_count = 0
        self._processed_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Starts the broker. Attempts Redis connection first, falls back to in-memory bus."""
        self._running = True
        if self.enabled and aioredis is not None:
            try:
                client = aioredis.from_url(
                    self.redis_url,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                    decode_responses=False,
                )
                await client.ping()
                self._redis_client = client
                self.mode = "redis"
                log.info("AsyncBroker connected to Redis at %s", self.redis_url)
            except Exception as e:
                log.info(
                    "Redis not reachable (%s); activating in-memory async pub/sub broker",
                    e,
                )
                self.mode = "memory"
                self._redis_client = None
        else:
            self.mode = "memory"
            log.info("AsyncBroker running in in-memory async mode")

        # Start consumer tasks for any handlers already registered
        for channel, handlers in self._handlers.items():
            for handler in handlers:
                self._spawn_consumer(channel, handler)

    async def stop(self) -> None:
        """Stops the broker and cancels consumer worker tasks cleanly."""
        self._running = False
        for task in self._consumer_tasks:
            task.cancel()
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        self._consumer_tasks.clear()

        if self._redis_pubsub is not None:
            try:
                await self._redis_pubsub.close()
            except Exception as e:
                log.warning("Error closing redis pubsub: %s", e)
            self._redis_pubsub = None

        if self._redis_client is not None:
            try:
                await self._redis_client.close()
            except Exception as e:
                log.warning("Error closing redis client: %s", e)
            self._redis_client = None

    def _serialize(self, payload: Any) -> bytes:
        if msgpack is not None:
            try:
                return msgpack.packb(payload, use_bin_type=True)
            except Exception:
                pass
        return json.dumps(payload, default=str).encode("utf-8")

    def _deserialize(self, raw: bytes) -> Any:
        if msgpack is not None:
            try:
                return msgpack.unpackb(raw, raw=False)
            except Exception:
                pass
        return json.loads(raw.decode("utf-8", errors="replace"))

    async def publish(self, channel: str, payload: Dict[str, Any]) -> None:
        """Publishes a payload to a channel asynchronously without blocking."""
        if not self._running:
            return
        self._published_count += 1

        if self.mode == "redis" and self._redis_client is not None:
            try:
                data = self._serialize(payload)
                await self._redis_client.publish(channel, data)
                return
            except Exception as e:
                log.warning("Redis publish failed (%s), failing over to in-memory bus", e)
                self.mode = "memory"

        # In-memory distribution
        queues = self._memory_queues.get(channel, [])
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()  # Drop oldest tick to prevent memory bloat during lag
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(payload)
            except Exception as e:
                log.error("Failed to enqueue in memory broker: %s", e)

    def subscribe(self, channel: str, handler: Handler) -> None:
        """Registers a handler callback for messages on the specified channel."""
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)

        if self._running:
            self._spawn_consumer(channel, handler)

    def _spawn_consumer(self, channel: str, handler: Handler) -> None:
        if self.mode == "redis" and self._redis_client is not None:
            task = asyncio.create_task(self._redis_subscriber_loop(channel, handler))
        else:
            if channel not in self._memory_queues:
                self._memory_queues[channel] = []
            q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max_size)
            self._memory_queues[channel].append(q)
            task = asyncio.create_task(self._memory_subscriber_loop(q, handler))
        self._consumer_tasks.append(task)

    async def _memory_subscriber_loop(self, queue: asyncio.Queue, handler: Handler) -> None:
        while self._running:
            try:
                payload = await queue.get()
                self._processed_count += 1
                try:
                    await handler(payload)
                except Exception as e:
                    log.exception("Handler error on in-memory broker channel: %s", e)
                finally:
                    queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Subscriber loop exception: %s", e)
                await asyncio.sleep(0.05)

    async def _redis_subscriber_loop(self, channel: str, handler: Handler) -> None:
        try:
            pubsub = self._redis_client.pubsub()
            await pubsub.subscribe(channel)
            while self._running:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is not None and msg.get("data"):
                        payload = self._deserialize(msg["data"])
                        self._processed_count += 1
                        try:
                            await handler(payload)
                        except Exception as e:
                            log.exception("Handler error on redis broker channel: %s", e)
                    else:
                        await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning("Redis subscriber error (%s), reconnecting...", e)
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Fatal redis subscriber loop error: %s", e)

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "running": self._running,
            "published": self._published_count,
            "processed": self._processed_count,
            "channels": list(self._handlers.keys()),
        }
