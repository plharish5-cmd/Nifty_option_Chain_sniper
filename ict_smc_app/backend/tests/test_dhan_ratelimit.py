"""Rate-limit back-off must not stall the caller inside a single call.

_Bucket.wait() sleeps gap + penalty BEFORE every attempt, so retrying a 429 in place while the penalty is
already saturated (60 s) blocked one chain poll for ~4 minutes and wrote no snapshot — that is what tore
24-30 minute holes in the option-chain snapshot series on 28 Oct 2026, and why `rate_limited` reached 435
(each stalled poll burns one increment per attempt).

These tests pin the bounded behaviour: punish, then hand control back to the caller's own poll loop.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ICT_DATA_DIR", str(Path(__file__).resolve().parent / ".data"))
os.environ.setdefault("DHAN_ACCESS_TOKEN", "test-token-not-a-secret")
os.environ.setdefault("DHAN_CLIENT_ID", "test-client")

from app.dhan.rest import DhanREST, RateLimited  # noqa: E402


class _Resp429:
    status_code = 429
    text = '{"errorCode":"DH-904","errorMessage":"Too many requests"}'


def _rest_always_429():
    """A client whose every request is rate-limited, with sleeping stubbed out so the test is instant."""
    rest = DhanREST()
    attempts = {"n": 0}

    async def _request(*_a, **_k):
        attempts["n"] += 1
        return _Resp429()

    rest.client.request = _request
    for b in rest.buckets.values():
        async def _wait(_b=b):
            return None
        b.wait = _wait
    return rest, attempts


def test_saturated_penalty_raises_without_burning_four_attempts():
    """Penalty already at the 60 s cap: the call must give up at once, not sleep 4 x 63 s in place."""
    rest, attempts = _rest_always_429()
    rest.buckets["chain"].penalty = 60.0

    with pytest.raises(RateLimited):
        asyncio.run(rest._call("POST", "/optionchain", "chain", {}))

    assert attempts["n"] == 1, f"saturated bucket should raise after one attempt, took {attempts['n']}"


def test_lightly_backed_off_bucket_still_retries():
    """A cold bucket retries while the penalty is under the ceiling (2.0, 4.0), then stops at 8.0."""
    rest, attempts = _rest_always_429()
    rest.buckets["chain"].penalty = 0.0

    with pytest.raises(RateLimited):
        asyncio.run(rest._call("POST", "/optionchain", "chain", {}))

    # punish() doubles 2 -> 4 -> 8; the ceiling is 8.0, so the third attempt is the last.
    assert attempts["n"] == 3, f"expected 3 bounded attempts, got {attempts['n']}"
    assert rest.buckets["chain"].penalty == 8.0


def test_every_attempt_is_counted_against_the_daily_budget():
    """Each attempt is a real HTTP call to Dhan, so the counters must see all of them — fewer retries is
    precisely how this fix reduces the daily burn."""
    rest, attempts = _rest_always_429()
    rest.buckets["chain"].penalty = 60.0
    before_chain = rest.usage_d.get("chain", 0)
    before_limited = rest.usage_d.get("rate_limited", 0)

    with pytest.raises(RateLimited):
        asyncio.run(rest._call("POST", "/optionchain", "chain", {}))

    assert rest.usage_d["chain"] - before_chain == attempts["n"]
    assert rest.usage_d["rate_limited"] - before_limited == attempts["n"]
