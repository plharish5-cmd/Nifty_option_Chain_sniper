"""
api/hub.py — the live pipeline. One object owns the feed, candles, chain, analyses, signals and clients.

  tick ─▶ Candles (forming 1m, memory) ─▶ every closed 1m ─▶ SQLite
                                              │
                                              ├─▶ paper trades resolved on the 1m close
                                              └─▶ when a 1m close also closes a 5m/15m/1h candle:
                                                    re-run the engine on CLOSED candles of that tf
                                                    primary tf (5m) → playbook → alerts → paper entries
  every change ─▶ WebSocket broadcast to the browser (ticks throttled to 4/s)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from .. import config, db
from ..dhan.feed import Feed
from ..dhan.rest import DhanError, DhanREST, TokenError
from ..engine.analyze import Analysis, analyze, to_json
from ..market.candles import Candles, backfill, backfill_daily, session_filter
from ..market.chain import ChainPoller
from ..signals import alerts as alerts_mod
from ..signals import paper
from ..signals.playbook import Signal, evaluate, live
from ..timeutil import (TF_SECONDS, bucket_start, chart_time, hm, ist_date, market_open_now, now_epoch, now_ist,
                        session_bounds)

log = logging.getLogger("hub")

DAYS_FOR_TF = {"1m": 2, "5m": 5, "15m": 12, "1h": 30}


class Hub:
    def __init__(self) -> None:
        I = config.rules()["instruments"]
        self.nifty, self.vix = str(I["nifty_security_id"]), str(I["vix_security_id"])
        self.tfs: List[str] = list(config.rules()["engine"]["timeframes"])
        self.primary = config.rule("playbook", "primary_tf", "5m")
        self.bias_tf = config.rule("playbook", "bias_tf", "15m")
        self.rest = DhanREST()
        self.candles = Candles(self.on_close_1m)
        self.feed = Feed([self.nifty, self.vix], self.on_tick, self.on_prev)
        self.chain = ChainPoller(self.rest, self.on_chain)
        self.alerts = alerts_mod.Alerts(self.broadcast)
        self.clients: Set[WebSocket] = set()
        self.analyses: Dict[str, Analysis] = {}
        self.json: Dict[str, Dict[str, Any]] = {}
        self.signals: List[Signal] = []
        self.prev_close: Dict[str, float] = {}
        self.price_source = "feed"
        self.status: Dict[str, Any] = {"backfill": "pending", "token": "unknown", "errors": []}
        self._last_tick_push = 0.0
        self._tasks: List[asyncio.Task] = []
        self._seen_connects = 0

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        db.conn()
        await self.repair(full=True)
        self.recompute_all()
        for coro in (self.feed.run(), self.candles.sweep_timer(), self.chain.run(), self.watchdog(), self.housekeeping()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        self.feed.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rest.close()

    async def repair(self, full: bool = False) -> None:
        """REST backfill: on start (30 days), after every feed reconnect (today only), and at 15:31."""
        D = config.rules()["data"]
        try:
            n = await backfill(self.rest, self.nifty, int(D["backfill_days"]) if full else 1)
            if full:
                await backfill_daily(self.rest, self.nifty, int(D["daily_days"]))
            self.status["backfill"] = f"ok · {n} one-minute candles · {now_ist():%H:%M:%S}"
            self.status["token"] = "valid"
        except TokenError as e:
            self.status["token"] = "expired / invalid — paste a fresh DHAN_ACCESS_TOKEN in .env"
            self.status["backfill"] = f"failed: {str(e)[:120]}"
        except DhanError as e:
            self.status["backfill"] = f"failed: {str(e)[:160]}"
        if not self.prev_close.get(self.nifty):
            d = db.daily(self.nifty, 3)
            today = now_ist().date().isoformat()
            older = [x for x in d if x["d"] < today]
            if older:
                self.prev_close[self.nifty] = float(older[-1]["c"])

    # ── feed callbacks ────────────────────────────────────────────────────
    async def on_tick(self, sid: str, ltp: float, t: float) -> None:
        if sid == self.vix and not (5 <= ltp <= 90):
            self.status["vix_warning"] = f"VIX id {self.vix} returned {ltp} — check vix_security_id in rules.json"
        await self.candles.tick(sid, ltp, t)
        if time.time() - self._last_tick_push >= 0.25:
            self._last_tick_push = time.time()
            await self.broadcast({"type": "quote", "quote": self.quote()})

    async def on_prev(self, sid: str, pc: float) -> None:
        self.prev_close[sid] = pc

    async def on_close_1m(self, sid: str, c: Dict[str, float]) -> None:
        if sid != self.nifty:
            return
        closed_trade = paper.on_1m_close(c)
        if closed_trade:
            await self.broadcast({"type": "paper", "trade": closed_trade})
        end = c["ts"] + 60
        _, sess_close = session_bounds(ist_date(c["ts"]))
        for tf in self.tfs:
            step = TF_SECONDS[tf]
            b = bucket_start(c["ts"], tf)
            if end == b + step or end >= sess_close:          # this minute closed a tf candle
                self.recompute(tf)
                if tf == self.primary:
                    await self.run_playbook()
                await self.broadcast({"type": "analysis", "tf": tf, "analysis": self.json.get(tf)})
        await self.broadcast({"type": "candle", "tf": "1m", "candle": _cj(c)})

    async def on_chain(self, expiry: str, ch: Dict[str, Any]) -> None:
        await self.broadcast({"type": "chain", "expiry": expiry, "summary": self.chain.summary(expiry)})

    # ── analysis ──────────────────────────────────────────────────────────
    def rows_1m(self, days: int = 3) -> List[Dict[str, float]]:
        return self.candles.frame(self.nifty, "1m", days=days, include_forming=False)

    def recompute(self, tf: str) -> None:
        rows = self.candles.frame(self.nifty, tf, days=DAYS_FOR_TF.get(tf, 5), include_forming=False)
        an = analyze(rows, tf, rows_1m=self.rows_1m(2), daily=db.daily(self.nifty, 10))
        if an is None:
            return
        self.analyses[tf] = an
        self.json[tf] = to_json(an)

    def recompute_all(self) -> None:
        for tf in self.tfs:
            try:
                self.recompute(tf)
            except Exception as e:                           # noqa: BLE001
                log.exception("analysis %s failed", tf)
                self.status["errors"] = (self.status["errors"] + [f"analysis {tf}: {e}"])[-5:]
        p = self.analyses.get(self.primary)
        if p:
            self.signals = evaluate(p, self.analyses.get(self.bias_tf))

    async def run_playbook(self) -> None:
        p = self.analyses.get(self.primary)
        if not p:
            return
        self.signals = evaluate(p, self.analyses.get(self.bias_tf))
        fresh = live(self.signals, p)
        if not fresh:
            return
        await self.alerts.handle(fresh)
        if market_open_now():
            for s in fresh:
                t = paper.maybe_open(s)
                if t:
                    await self.broadcast({"type": "paper", "trade": t})

    # ── background loops ──────────────────────────────────────────────────
    async def watchdog(self) -> None:
        """REST fallback when ticks stop, and a gap repair after every reconnect."""
        D = config.rules()["data"]
        while True:
            await asyncio.sleep(float(D["rest_ltp_every_s"]))
            try:
                if self.feed.connects > self._seen_connects:
                    if self._seen_connects > 0 and market_open_now():
                        await self.repair(full=False)          # fill the minutes missed while disconnected
                        self.recompute_all()
                    self._seen_connects = self.feed.connects
                if market_open_now() and self.feed.tick_age(self.nifty) > float(D["feed_stale_s"]):
                    q = await self.rest.ltp({"IDX_I": [int(self.nifty), int(self.vix)]})
                    m = q.get("IDX_I", {})
                    t = now_epoch()
                    for sid in (self.nifty, self.vix):
                        if m.get(sid):
                            await self.candles.tick(sid, m[sid], t)
                    self.price_source = "rest (feed stale)"
                    await self.broadcast({"type": "quote", "quote": self.quote()})
                else:
                    self.price_source = "feed"
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001
                self.status["errors"] = (self.status["errors"] + [f"watchdog: {str(e)[:120]}"])[-5:]

    async def housekeeping(self) -> None:
        done: Dict[str, str] = {}
        while True:
            await asyncio.sleep(20)
            n = now_ist()
            day = n.date().isoformat()
            hhmm = n.strftime("%H:%M")
            try:
                if hhmm >= "15:31" and done.get("eod") != day and n.weekday() < 5:
                    done["eod"] = day
                    await self.repair(full=False)              # official candles replace tick-built ones
                    await backfill_daily(self.rest, self.nifty, 5)
                    self.recompute_all()
                if hhmm >= "08:35" and done.get("renew") != day and config.env("AUTO_RENEW_TOKEN") == "1":
                    done["renew"] = day
                    from ..dhan.auth import renew_into_env
                    self.status["token_renew"] = await renew_into_env(self.rest)
                if done.get("prune") != day:
                    done["prune"] = day
                    self.status["prune"] = db.prune()
            except Exception as e:                           # noqa: BLE001
                self.status["errors"] = (self.status["errors"] + [f"housekeeping: {str(e)[:120]}"])[-5:]

    # ── views ─────────────────────────────────────────────────────────────
    def quote(self) -> Dict[str, Any]:
        px = self.candles.last_price.get(self.nifty)
        if px is None:
            rows = db.candles_1m(self.nifty, int(now_epoch()) - 10 * 86400)
            px = rows[-1]["c"] if rows else None
        pc = self.prev_close.get(self.nifty)
        vix = self.candles.last_price.get(self.vix)
        vpc = self.prev_close.get(self.vix)
        return {"spot": px, "prev_close": pc, "chg": round(px - pc, 2) if px and pc else None,
                "chg_pct": round(100 * (px - pc) / pc, 2) if px and pc else None,
                "vix": vix, "vix_chg": round(vix - vpc, 2) if vix and vpc else None,
                "source": self.price_source if market_open_now() else "after hours",
                "market_open": market_open_now(), "ts": int(now_epoch())}

    def narrative(self) -> List[str]:
        """Plain-language read of each timeframe, e.g. 'swept PDH; MSS bearish; in premium (68 %)'."""
        out = []
        for tf in self.tfs:
            an = self.analyses.get(tf)
            if not an:
                continue
            parts = [f"trend {an.trend}"]
            raid = next((x for x in reversed(an.liq) if x.kind in ("SWEEP", "FALSE_BREAKOUT")), None)
            if raid:
                parts.append(f"{hm(raid.ts)} {'swept' if raid.kind == 'SWEEP' else 'false breakout of'} "
                             f"{raid.src} {raid.level:,.1f}")
            if an.events:
                e = an.events[-1]
                parts.append(f"{hm(e.ts)} {'MSS' if e.is_mss else 'BOS'} {e.dir}{'' if e.displacement else ' (weak)'}")
            if an.rng:
                r = an.rng
                parts.append(f"in {r['zone']} ({r['pos_pct']:.0f} %) of {r['low']:,.0f}–{r['high']:,.0f}")
            out.append(f"{tf}: " + " · ".join(parts))
        return out

    def dashboard(self) -> Dict[str, Any]:
        bias = {}
        for tf, an in self.analyses.items():
            bias[tf] = {"trend": an.trend, "zone": an.rng["zone"] if an.rng else None,
                        "pos_pct": an.rng["pos_pct"] if an.rng else None,
                        "last_event": (f"{'MSS' if an.events[-1].is_mss else 'BOS'} {an.events[-1].dir} "
                                       f"{hm(an.events[-1].ts)}") if an.events else None}
        p = self.analyses.get(self.primary)
        return {"quote": self.quote(), "bias": bias, "narrative": self.narrative(),
                "session": p.sess if p else {}, "chain": self.chain.summary(),
                "expiries": self.chain.expiries, "signals": [s.to_dict() for s in self.today_signals()][-15:],
                "alerts": alerts_mod.recent(20), "paper_open": paper.open_trade(), "paper_stats": paper.stats(),
                "status": self.status_view()}

    def today_signals(self) -> List[Signal]:
        o, _ = session_bounds(now_ist().date())
        return [s for s in self.signals if s.ts >= o]

    def status_view(self) -> Dict[str, Any]:
        return {**self.status, "feed": self.feed.status, "feed_error": self.feed.last_error,
                "feed_connects": self.feed.connects, "ltt_mode": self.feed.ltt_mode,
                "tick_age_s": round(self.feed.tick_age(self.nifty), 1) if self.feed.tick_age(self.nifty) < 1e8 else None,
                "chain": self.chain.status, "chain_error": self.chain.last_error,
                "rest_calls": self.rest.calls, "rest_error": self.rest.last_error,
                "market_open": market_open_now(), "now_ist": now_ist().strftime("%a %d %b %H:%M:%S")}

    # ── websocket clients ────────────────────────────────────────────────
    async def broadcast(self, msg: Dict[str, Any]) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, default=_jdefault)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:                                # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def _cj(c: Dict[str, float]) -> Dict[str, Any]:
    return {"time": chart_time(c["ts"]), "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"],
            "closed": bool(c.get("closed", True))}


def _jdefault(o):
    try:
        import numpy as np
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
    except ImportError:
        pass
    return str(o)
