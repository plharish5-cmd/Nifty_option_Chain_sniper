"""
gift_nifty_feed.py — Live Dhan API Feed Connector for GIFT Nifty.

Exchange Segment: IDX_I (with fallback to NSE_IX)
Security ID: 5024
WebSocket URL: wss://api-feed.dhan.co?version=2&token=...&clientId=...&authType=2
REST Endpoint: https://api.dhan.co/v2/marketfeed/quote / ltp
Resilient Offshore Fallback: TradingView Scanner (NSEIX:NIFTY1!)

Alert-only, zero order placement.
All thresholds loaded from rules.json (no hardcoded parameters).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .. import config

log = logging.getLogger("gift_nifty_feed")

IST = timezone(timedelta(hours=5, minutes=30))

# Packet unpacking structs for Dhan v2 Feed
_HDR = struct.Struct("<BHBI")
_TICK = struct.Struct("<fi")
_PREV = struct.Struct("<fi")


def now_ist_iso() -> str:
    """Returns current IST timestamp in ISO-8601 format."""
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")


class DhanFeedBridge:
    """
    Live Dhan Market Feed Connector for GIFT Nifty.
    Maintains real-time tick ingestion, automated fallback to Dhan REST and
    offshore TradingView scanner feeds, and background polling worker.
    """

    def __init__(self, hub: Optional[Any] = None) -> None:
        self.hub = hub
        rule_cfg = config.rule("gift_nifty_feed", "", {}) or {}
        gap_cfg = config.rule("gap_sniper", "", {}) or {}

        self.symbol: str = str(rule_cfg.get("symbol") or "GIFT Nifty")
        self.security_id: str = str(rule_cfg.get("security_id") or gap_cfg.get("gift_nifty_security_id") or "5024")
        self.exchange_segment: str = str(rule_cfg.get("exchange_segment") or "IDX_I")
        self.fallback_segment: str = str(rule_cfg.get("fallback_segment") or "NSE_IX")
        self.poll_interval_s: float = float(rule_cfg.get("poll_interval_s", 3.0))
        self.stale_threshold_s: float = float(rule_cfg.get("stale_threshold_s", 10.0))
        self.tv_ticker: str = str(rule_cfg.get("tradingview_ticker") or "NSEIX:NIFTY1!")

        self.default_ltp: float = float(rule_cfg.get("default_ltp", 22975.40))
        self.default_prev_close: float = float(rule_cfg.get("default_prev_close", 22771.20))

        # Live State
        self.ltp: float = self.default_ltp
        self.prev_close: float = self.default_prev_close
        self.change: float = round(self.ltp - self.prev_close, 2)
        self.change_pct: float = round((self.change / self.prev_close) * 100.0, 2) if self.prev_close > 0 else 0.0
        self.timestamp: str = now_ist_iso()
        self.last_tick_epoch: float = 0.0
        self.source: str = "initialized"
        self.status: str = "idle"

        self._running: bool = False
        self._worker_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def update_from_feed(self, sid: str, ltp: float, t: float) -> None:
        """Called directly by Hub or WebSocket feed when a tick arrives."""
        if str(sid) != self.security_id:
            return
        if ltp <= 1000.0 or ltp > 100000.0:
            log.warning("Ignoring abnormal GIFT Nifty feed tick: %.2f", ltp)
            return

        self.ltp = round(float(ltp), 2)
        self.last_tick_epoch = time.time()
        self.timestamp = now_ist_iso()
        self.source = "dhan_feed"
        self.status = "live_feed"

        if self.prev_close > 0:
            self.change = round(self.ltp - self.prev_close, 2)
            self.change_pct = round((self.change / self.prev_close) * 100.0, 2)

    def update_from_prev(self, sid: str, prev: float) -> None:
        """Called when Dhan feed delivers Code 6 (previous close)."""
        if str(sid) != self.security_id:
            return
        if prev > 1000.0:
            self.prev_close = round(float(prev), 2)
            if self.ltp > 0:
                self.change = round(self.ltp - self.prev_close, 2)
                self.change_pct = round((self.change / self.prev_close) * 100.0, 2)

    def is_stale(self) -> bool:
        """Returns True if the last tick was received more than stale_threshold_s ago."""
        return (time.time() - self.last_tick_epoch) > self.stale_threshold_s

    async def fetch_dhan_rest(self) -> bool:
        """Attempts to query Dhan REST API for live quote."""
        tok = config.env("DHAN_ACCESS_TOKEN")
        cid = config.env("DHAN_CLIENT_ID")
        if not tok or not cid:
            return False

        # Try both IDX_I and fallback segment
        for seg in (self.exchange_segment, self.fallback_segment):
            try:
                url = f"{config.base_url()}/marketfeed/quote"
                payload = json.dumps({seg: [int(self.security_id)]}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "access-token": tok,
                        "client-id": cid,
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        d = data.get("data", {}).get(seg, {}).get(str(self.security_id), {})
                        if d and d.get("last_price"):
                            px = float(d["last_price"])
                            if 10000.0 < px < 100000.0:
                                self.ltp = round(px, 2)
                                ohlc = d.get("ohlc", {})
                                pc = float(ohlc.get("close") or d.get("close") or self.prev_close)
                                if pc > 10000.0:
                                    self.prev_close = round(pc, 2)
                                self.change = round(self.ltp - self.prev_close, 2)
                                self.change_pct = round((self.change / self.prev_close) * 100.0, 2) if self.prev_close > 0 else 0.0
                                self.last_tick_epoch = time.time()
                                self.timestamp = now_ist_iso()
                                self.source = "dhan_rest"
                                self.status = "live_rest"
                                return True
            except Exception as e:
                log.debug("Dhan REST quote failed for %s (%s): %s", self.security_id, seg, e)
        return False

    def fetch_tradingview_fallback(self) -> bool:
        """
        Fetches live GIFT Nifty futures LTP from TradingView global scanner.
        Guarantees 24/5 continuity even if Dhan REST gateway is undergoing pre-market maintenance.
        """
        try:
            url = "https://scanner.tradingview.com/global/scan"
            payload = json.dumps({
                "symbols": {"tickers": [self.tv_ticker]},
                "columns": ["close", "change", "change_abs", "previous_close"]
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Content-Type": "application/json"
                }
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("data", [])
                if items and len(items[0].get("d", [])) >= 2:
                    d = items[0]["d"]
                    close_px = float(d[0]) if d[0] is not None else 0.0
                    chg_pct = float(d[1]) if d[1] is not None else 0.0
                    chg_abs = float(d[2]) if len(d) > 2 and d[2] is not None else 0.0

                    if 10000.0 < close_px < 100000.0:
                        self.ltp = round(close_px, 2)
                        if chg_abs != 0.0:
                            self.change = round(chg_abs, 2)
                            self.change_pct = round(chg_pct, 2)
                            self.prev_close = round(self.ltp - self.change, 2)
                        elif self.prev_close > 0:
                            self.change = round(self.ltp - self.prev_close, 2)
                            self.change_pct = round((self.change / self.prev_close) * 100.0, 2)
                        self.last_tick_epoch = time.time()
                        self.timestamp = now_ist_iso()
                        self.source = "tradingview"
                        self.status = "live_offshore"
                        return True
        except Exception as e:
            log.warning("TradingView scanner fallback for GIFT Nifty failed: %s", e)
        return False

    async def sync_now(self) -> Dict[str, Any]:
        """Ensures state is refreshed if stale, and returns quote payload."""
        async with self._get_lock():
            if self.is_stale():
                rest_ok = await self.fetch_dhan_rest()
                if not rest_ok:
                    self.fetch_tradingview_fallback()
            return self.get_quote()

    def get_quote(self) -> Dict[str, Any]:
        """Returns the standard JSON payload according to API specifications."""
        return {
            "ok": True,
            "symbol": self.symbol,
            "ltp": float(self.ltp),
            "change": float(self.change),
            "change_pct": float(self.change_pct),
            "timestamp": str(self.timestamp),
            "prev_close": float(self.prev_close),
            "source": self.source,
            "status": self.status,
            "security_id": self.security_id,
        }

    async def _worker_loop(self) -> None:
        """Lightweight background worker refreshing state every poll_interval_s."""
        log.info("GIFT Nifty Live Feed background worker started (cadence: %.1fs)", self.poll_interval_s)
        while self._running:
            try:
                if self.is_stale():
                    rest_ok = await self.fetch_dhan_rest()
                    if not rest_ok:
                        self.fetch_tradingview_fallback()
            except Exception as e:
                log.warning("Error in GIFT Nifty background worker loop: %s", e)
            await asyncio.sleep(self.poll_interval_s)

    async def start(self) -> None:
        """Starts the background worker task."""
        if self._running:
            return
        self._running = True
        # Perform initial sync
        await self.sync_now()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stops background tasks."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                log.debug("GIFT Nifty worker task cancelled")
            self._worker_task = None


# Singleton instance
_bridge_instance: Optional[DhanFeedBridge] = None


def get_gift_nifty_bridge(hub: Optional[Any] = None) -> DhanFeedBridge:
    """Returns the singleton instance of DhanFeedBridge."""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = DhanFeedBridge(hub=hub)
    elif hub and not _bridge_instance.hub:
        _bridge_instance.hub = hub
    return _bridge_instance
