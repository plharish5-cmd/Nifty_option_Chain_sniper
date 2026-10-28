"""
dhan/rest.py — DhanHQ v2 REST client (async, httpx).

AUTH (data endpoints)            headers:  access-token: <JWT>   client-id: <your client id>
                                           Content-Type: application/json
TRADING endpoints (/orders, /super/orders, /forever …) use the same access-token, need a whitelisted static IP,
and are NOT implemented anywhere in this app on purpose — v1 is analysis + paper trading only.

RATE LIMITS (Dhan v2 docs, Oct 2026)               how this client enforces them
  Data APIs (charts)      5 / second, 100 000 / day   'data'  bucket  ≥ 0.21 s between calls
  Quote APIs (marketfeed) 1 / second                  'quote' bucket  ≥ 1.05 s
  Option chain            1 unique request / 3 s      'chain' bucket  ≥ chain_min_gap_s (3.1 s)
  Non-trading             20 / second                 'misc'  bucket  ≥ 0.06 s
A 429 / DH-904 / 805 answer backs the bucket off exponentially instead of retrying immediately (mistake #2).

ERRORS are classified, because the fixes differ (an expired token and a missing Data plan both come back 401):
  TokenError        DH-901 / 807 / 808 / 809 / 'Invalid_Authentication'  → new token needed (daily, 24 h)
  NotEntitled       DH-902 / 806                                         → Data API plan not active
  RateLimited       429 / DH-904 / 805                                   → slow down
  BadRequest        DH-905 / 811-814                                     → wrong body / ids / dates
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from .. import config
from ..timeutil import IST

log = logging.getLogger("dhan.rest")


class DhanError(RuntimeError):
    pass


class TokenError(DhanError):
    pass


class NotEntitled(DhanError):
    pass


class RateLimited(DhanError):
    pass


class BadRequest(DhanError):
    pass


class _Bucket:
    """Minimum spacing between calls + exponential back-off after a rate-limit answer."""

    def __init__(self, gap_s: float):
        self.gap = gap_s
        self.next_ok = 0.0
        self.penalty = 0.0
        self._lock: Optional[asyncio.Lock] = None          # created on first use, inside the event loop
                                                            # (Python 3.9 can't make one outside a running loop)

    async def wait(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if now < self.next_ok:
                await asyncio.sleep(self.next_ok - now)
            self.next_ok = time.monotonic() + self.gap + self.penalty

    def punish(self) -> None:
        self.penalty = min(60.0, max(2.0, self.penalty * 2 or 2.0))
        self.next_ok = time.monotonic() + self.penalty

    def forgive(self) -> None:
        self.penalty = max(0.0, self.penalty / 2 - 0.5)


class DhanREST:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0))
        gap = float(config.rule("data", "chain_min_gap_s", 3.1))
        self.buckets = {"data": _Bucket(0.21), "quote": _Bucket(1.05), "chain": _Bucket(gap), "misc": _Bucket(0.06)}
        self.token_ok: Optional[bool] = None
        self.last_error = ""
        self.calls = 0
        self._load_usage()

    # ── daily call counter (Dhan's limits are per ACCOUNT: every app on your token shares them) ─────────
    def _load_usage(self) -> None:
        from .. import db
        from ..timeutil import now_ist
        day = now_ist().date().isoformat()
        try:
            u = db.kv_get("dhan_calls") or {}
        except Exception:                                    # noqa: BLE001
            u = {}
        self.usage_d = u if u.get("day") == day else {"day": day, "data": 0, "chain": 0, "quote": 0, "misc": 0, "rate_limited": 0}

    def _count(self, bucket: str, limited: bool = False) -> None:
        from .. import db
        from ..timeutil import now_ist
        day = now_ist().date().isoformat()
        if self.usage_d.get("day") != day:
            self.usage_d = {"day": day, "data": 0, "chain": 0, "quote": 0, "misc": 0, "rate_limited": 0}
        self.usage_d[bucket] = self.usage_d.get(bucket, 0) + 1
        if limited:
            self.usage_d["rate_limited"] += 1
        if self.usage_d[bucket] % 10 == 0 or limited:
            try:
                db.kv_set("dhan_calls", self.usage_d)
            except Exception:                                # noqa: BLE001
                pass

    def usage(self) -> Dict[str, Any]:
        env_lim = config.env("DHAN_API_LIMIT") or config.env("MAX_CALLS") or config.env("DAILY_DATA_LIMIT")
        lim = int(env_lim) if env_lim else int(config.rule("data", "daily_data_limit", 15000))
        env_bud = config.env("DESK_DATA_BUDGET")
        bud = int(env_bud) if env_bud else int(config.rule("data", "desk_data_budget", lim))
        d = self.usage_d.get("data", 0)
        return {**self.usage_d, "data_limit": lim, "desk_budget": bud, "data_pct": round(100 * d / lim, 1),
                "over_budget": d >= bud}

    def over_budget(self) -> bool:
        """True once THIS desk has used its share of the account's daily Data-API calls — optional refreshes
        (option / futures candles) then pause; the live feed, recorder and repairs keep working."""
        env_bud = config.env("DESK_DATA_BUDGET") or config.env("DHAN_API_LIMIT") or config.env("MAX_CALLS")
        bud = int(env_bud) if env_bud else int(config.rule("data", "desk_data_budget", 15000))
        return self.usage_d.get("data", 0) >= bud

    # ── plumbing ──────────────────────────────────────────────────────────
    @staticmethod
    def headers() -> Dict[str, str]:
        tok, cid = config.env("DHAN_ACCESS_TOKEN"), config.env("DHAN_CLIENT_ID")
        if not tok or not cid:
            raise TokenError("DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID missing from .env")
        return {"access-token": tok, "client-id": cid, "Content-Type": "application/json", "Accept": "application/json"}

    @staticmethod
    def _classify(status: int, text: str) -> DhanError:
        low = text.lower()
        if status == 429 or "dh-904" in low or '"805"' in low or "too many" in low:
            return RateLimited(f"HTTP {status}: {text[:200]}")
        if ("dh-901" in low or "invalid_authentication" in low or '"807"' in low or '"808"' in low
                or '"809"' in low or "expired" in low or (status == 401 and "dh-902" not in low)):
            return TokenError(f"HTTP {status}: {text[:200]} — Dhan tokens last 24 h; paste a fresh one in .env")
        if "dh-902" in low or '"806"' in low or "not subscribed" in low:
            return NotEntitled(f"HTTP {status}: {text[:200]} — Data API plan not active for this account")
        if "dh-905" in low or any(f'"{c}"' in low for c in ("811", "812", "813", "814")) or status == 400:
            return BadRequest(f"HTTP {status}: {text[:200]}")
        return DhanError(f"HTTP {status}: {text[:200]}")

    async def _call(self, method: str, path: str, bucket: str, body: Optional[dict] = None,
                    retries: int = 3, extra_headers: Optional[Dict[str, str]] = None) -> Any:
        url = f"{config.base_url()}{path}"
        b = self.buckets[bucket]
        for attempt in range(retries + 1):
            await b.wait()
            try:
                h = self.headers()
                if extra_headers:
                    h.update(extra_headers)
                self.calls += 1
                self._count(bucket)
                r = await self.client.request(method, url, json=body, headers=h)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self.last_error = f"{path}: network {type(e).__name__}"
                if attempt == retries:
                    raise DhanError(self.last_error) from None
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    raise DhanError(f"{path}: non-JSON reply {r.text[:120]!r}") from None
                if isinstance(data, dict) and data.get("status") == "failure":
                    err = self._classify(400, r.text)
                    self.last_error = str(err)
                    raise err
                b.forgive()
                self.token_ok = True
                return data
            err = self._classify(r.status_code, r.text)
            self.last_error = f"{path}: {err}"
            if isinstance(err, RateLimited):
                self.usage_d["rate_limited"] = self.usage_d.get("rate_limited", 0) + 1
                b.punish()
                # Retry in place ONLY while the bucket is still lightly backed off. b.wait() sleeps
                # gap + penalty BEFORE each attempt, so once punish() has saturated the penalty at 60 s a
                # 4-attempt retry blocks the caller inside ONE call for ~4 minutes and writes no snapshot —
                # that is what tore 24-30 min holes in the chain-snapshot series. Past the ceiling, raise and
                # let the caller's own poll loop set the cadence; the bucket still spaces the next call.
                ceiling = float(config.rule("data", "ratelimit_retry_ceiling_s", 8.0))
                if attempt >= retries or b.penalty >= ceiling:
                    raise err
                continue
            if isinstance(err, TokenError):
                self.token_ok = False
            if isinstance(err, (TokenError, NotEntitled, BadRequest)) or attempt == retries:
                raise err
            await asyncio.sleep(1.5 * (attempt + 1))              # 5xx: short retry
        raise DhanError(f"{path}: gave up")

    # ── data endpoints ────────────────────────────────────────────────────
    async def intraday(self, security_id: str, frm: datetime, to: datetime, interval: str = "1",
                       segment: str = "IDX_I", instrument: str = "INDEX", oi: bool = False) -> List[Dict[str, float]]:
        """POST /charts/intraday. interval ∈ {1,5,15,25,60}; at most 90 days per call; 5 years of history.
        Returns candles as dicts {ts (epoch, candle start), o, h, l, c, v}. NIFTY index volume is always 0."""
        body = {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                "interval": str(interval), "oi": bool(oi),
                "fromDate": frm.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": to.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")}
        d = await self._call("POST", "/charts/intraday", "data", body)
        return _arrays_to_candles(d)

    async def expiry_list(self, scrip: int = 13, seg: str = "IDX_I") -> List[str]:
        d = await self._call("POST", "/optionchain/expirylist", "chain", {"UnderlyingScrip": scrip, "UnderlyingSeg": seg})
        return list(d.get("data") or [])

    async def option_chain(self, expiry: str, scrip: int = 13, seg: str = "IDX_I") -> Dict[str, Any]:
        d = await self._call("POST", "/optionchain", "chain",
                             {"UnderlyingScrip": scrip, "UnderlyingSeg": seg, "Expiry": expiry})
        return d.get("data") or {}

    async def ltp(self, ids: Dict[str, List[int]]) -> Dict[str, Dict[str, float]]:
        """POST /marketfeed/ltp  {"IDX_I": [13, 21]} → {"IDX_I": {"13": 22231.8, "21": 11.2}}. 1 call / second."""
        d = await self._call("POST", "/marketfeed/ltp", "quote", ids, retries=1)
        out: Dict[str, Dict[str, float]] = {}
        for seg, m in (d.get("data") or {}).items():
            out[seg] = {str(k): float((v or {}).get("last_price") or 0) for k, v in (m or {}).items()}
        return out

    async def profile(self) -> Dict[str, Any]:
        """GET /profile — cheapest way to test a token (returns tokenValidity, dataPlan, dataValidity)."""
        return await self._call("GET", "/profile", "misc", retries=0)

    async def renew_token(self) -> Optional[str]:
        """GET /RenewToken — extends a still-valid WEB-generated token by 24 h (does not revive an expired one)."""
        d = await self._call("GET", "/RenewToken", "misc", retries=0,
                             extra_headers={"dhanClientId": config.env("DHAN_CLIENT_ID")})
        return (d or {}).get("accessToken") or (d or {}).get("token")

    async def close(self) -> None:
        await self.client.aclose()


def _arrays_to_candles(d: Dict[str, Any]) -> List[Dict[str, float]]:
    """Dhan returns parallel arrays: open, high, low, close, volume, timestamp (epoch SECONDS, true UTC epoch)."""
    if not isinstance(d, dict):
        return []
    ts = d.get("timestamp") or d.get("start_Time") or []
    o, h, l, c = d.get("open") or [], d.get("high") or [], d.get("low") or [], d.get("close") or []
    v = d.get("volume") or [0] * len(ts)
    oi = d.get("open_interest") or []                        # only when requested with oi=True (F&O)
    n = min(len(ts), len(o), len(h), len(l), len(c))
    out = []
    for k in range(n):
        try:
            out.append({"ts": int(float(ts[k])), "o": float(o[k]), "h": float(h[k]), "l": float(l[k]),
                        "c": float(c[k]), "v": float(v[k] if k < len(v) else 0)})
            if k < len(oi) and oi[k] is not None:
                out[-1]["oi"] = float(oi[k])
        except (TypeError, ValueError):
            continue
    return out
