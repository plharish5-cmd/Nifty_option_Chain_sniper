"""
dhan/feed.py — DhanHQ v2 Live Market Feed (WebSocket) with auto-reconnect.

  URL        wss://api-feed.dhan.co?version=2&token=<ACCESS_TOKEN>&clientId=<CLIENT_ID>&authType=2
  Subscribe  {"RequestCode": 15, "InstrumentCount": 2,
              "InstrumentList": [{"ExchangeSegment": "IDX_I", "SecurityId": "13"},   # NIFTY 50
                                 {"ExchangeSegment": "IDX_I", "SecurityId": "21"}]}  # INDIA VIX
             15 = ticker (LTP + LTT) — all an index has. 17 = quote, 21 = full (for F&O/equity).
  Limits     5 connections per user (your Streamlit dashboard and orderflow_recorder count too!),
             5000 instruments per connection, 100 per subscribe message.
  Keep-alive the server pings every 10 s; the websockets library answers automatically. No answer for 40 s
             → server closes the socket.

Binary replies are little-endian. Header = 8 bytes:
  byte 0      response code   (1 index, 2 ticker, 4 quote, 5 OI, 6 prev close, 7 market status, 8 full, 50 disconnect)
  bytes 1-2   int16 message length
  byte 3      exchange segment (0 = IDX_I, 1 = NSE_EQ, 2 = NSE_FNO …)
  bytes 4-7   int32 security id
Ticker body: float32 LTP, int32 LTT.   Prev-close body: float32 prev close, int32 prev OI.
Disconnect body: int16 reason (805 too many connections, 807 token expired, 808 auth failed, 809 token invalid …).

LTT TIME BASE. Different Dhan feeds have been seen stamping LTT either as a true epoch or as IST wall-clock
seconds. Instead of trusting either, the first ticks are compared with the local clock and the base that is
within 2 minutes of "now" is used; if neither is, the receive time is used (mistake #3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Awaitable, Callable, Dict, List, Optional

import websockets

from .. import config
from ..timeutil import IST_OFFSET_S, market_open_now, now_epoch

log = logging.getLogger("dhan.feed")

HDR = struct.Struct("<BHBI")
TICK = struct.Struct("<fi")
PREV = struct.Struct("<fi")
SEG = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}
DISCONNECT_REASON = {805: "too many connections/requests (max 5 sockets per user)", 806: "Data API not subscribed",
                     807: "access token expired", 808: "authentication failed", 809: "access token invalid",
                     810: "client id invalid", 804: "too many instruments"}
TOKEN_REASONS = {807, 808, 809, 810}


def decode(buf: bytes) -> List[dict]:
    """One websocket message may carry several packets back to back."""
    out, i, n = [], 0, len(buf)
    while i + HDR.size <= n:
        code, length, seg, sid = HDR.unpack_from(buf, i)
        if length < HDR.size or i + length > n:
            length = n - i
        body = i + HDR.size
        p: dict = {"code": code, "seg": SEG.get(seg, str(seg)), "sid": str(sid)}
        try:
            if code in (1, 2) and body + TICK.size <= n:
                p["ltp"], p["ltt"] = TICK.unpack_from(buf, body)
            elif code == 6 and body + PREV.size <= n:
                p["prev_close"], p["prev_oi"] = PREV.unpack_from(buf, body)
            elif code == 50 and body + 2 <= n:
                p["reason"] = struct.unpack_from("<h", buf, body)[0]
            elif code in (4, 8) and body + 4 <= n:               # quote/full: LTP is the first float either way
                p["ltp"] = struct.unpack_from("<f", buf, body)[0]
                if body + 14 <= n:
                    p["ltt"] = struct.unpack_from("<i", buf, body + 6)[0]
        except struct.error:
            pass
        out.append(p)
        i += length
    return out


TickCB = Callable[[str, float, float], Awaitable[None]]
PrevCB = Callable[[str, float], Awaitable[None]]


class Feed:
    def __init__(self, ids: List[str], on_tick: TickCB, on_prev: Optional[PrevCB] = None,
                 segment: str = "IDX_I") -> None:
        self.ids, self.segment = [str(x) for x in ids], segment
        self.on_tick, self.on_prev = on_tick, on_prev
        self.status = "idle"
        self.last_tick: Dict[str, float] = {}
        self.last_msg = 0.0
        self.connects = 0
        self.last_error = ""
        self.ltt_mode: Optional[str] = None                 # 'epoch' | 'ist' | 'recv'
        self._ltt_votes: List[str] = []
        self._stop = asyncio.Event()
        self.token_blocked = False

    def url(self) -> str:
        tok, cid = config.env("DHAN_ACCESS_TOKEN"), config.env("DHAN_CLIENT_ID")
        return f"{config.feed_url()}?version=2&token={tok}&clientId={cid}&authType=2"

    def stop(self) -> None:
        self._stop.set()

    # ── LTT → epoch ───────────────────────────────────────────────────────
    def _tick_time(self, ltt: Optional[int]) -> float:
        now = now_epoch()
        if not ltt:
            return now
        if self.ltt_mode is None:
            vote = "epoch" if abs(ltt - now) < 120 else ("ist" if abs(ltt - IST_OFFSET_S - now) < 120 else "recv")
            self._ltt_votes.append(vote)
            if len(self._ltt_votes) >= 5:
                self.ltt_mode = max(set(self._ltt_votes), key=self._ltt_votes.count)
                log.info("feed LTT time base detected: %s", self.ltt_mode)
            mode = vote
        else:
            mode = self.ltt_mode
        t = float(ltt) if mode == "epoch" else (float(ltt - IST_OFFSET_S) if mode == "ist" else now)
        return t if abs(t - now) < 300 else now             # never let a bad stamp move a candle

    # ── main loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        wait = 2.0
        while not self._stop.is_set():
            if not config.env("DHAN_ACCESS_TOKEN"):
                self.status = "no token in .env"
                await asyncio.sleep(15)
                continue
            if self.token_blocked:
                # do not hammer Dhan with a dead token — wait until .env changes
                tok = config.env("DHAN_ACCESS_TOKEN")
                await asyncio.sleep(15)
                if config.env("DHAN_ACCESS_TOKEN") == tok:
                    continue
                self.token_blocked = False
            t0 = time.monotonic()
            try:
                self.status = "connecting"
                async with websockets.connect(self.url(), ping_interval=20, ping_timeout=20,
                                              close_timeout=5, max_size=2 ** 22) as ws:
                    self.connects += 1
                    self.status = "connected"
                    await self._subscribe(ws)
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        self.last_msg = time.time()
                        if isinstance(msg, str):
                            log.info("feed text: %s", msg[:200])
                            continue
                        await self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001 — every failure means reconnect
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                if "401" in self.last_error or "403" in self.last_error:
                    self.token_blocked = True
                    self.status = "token rejected — update DHAN_ACCESS_TOKEN in .env"
            if self._stop.is_set():
                break
            lived = time.monotonic() - t0
            wait = 2.0 if lived > 120 else min(wait * 2, float(config.rule("data", "reconnect_max_wait_s", 60)))
            if not market_open_now():
                wait = max(wait, 30.0)                       # after hours: reconnect lazily
            if not self.token_blocked:
                self.status = f"reconnecting in {wait:.0f}s"
            log.warning("feed closed (%s); reconnecting in %.0fs", self.last_error, wait)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        self.status = "stopped"

    async def _subscribe(self, ws) -> None:
        for k in range(0, len(self.ids), 100):
            chunk = self.ids[k:k + 100]
            await ws.send(json.dumps({"RequestCode": 15, "InstrumentCount": len(chunk),
                                      "InstrumentList": [{"ExchangeSegment": self.segment, "SecurityId": s} for s in chunk]}))

    async def _handle(self, msg: bytes) -> None:
        for p in decode(msg):
            code = p["code"]
            if code in (1, 2, 4, 8) and p.get("ltp"):
                t = self._tick_time(p.get("ltt"))
                self.last_tick[p["sid"]] = time.time()
                await self.on_tick(p["sid"], round(float(p["ltp"]), 2), t)   # float32 → 2 dp
            elif code == 6 and self.on_prev and p.get("prev_close"):
                await self.on_prev(p["sid"], float(p["prev_close"]))
            elif code == 50:
                r = int(p.get("reason", 0))
                self.last_error = f"server disconnect {r}: {DISCONNECT_REASON.get(r, 'unknown')}"
                if r in TOKEN_REASONS:
                    self.token_blocked = True
                    self.status = "token rejected — update DHAN_ACCESS_TOKEN in .env"
                raise ConnectionError(self.last_error)

    def tick_age(self, sid: str) -> float:
        t = self.last_tick.get(str(sid))
        return (time.time() - t) if t else 1e9
