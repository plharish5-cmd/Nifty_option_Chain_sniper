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
from collections import deque
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

try:
    import msgpack
except ImportError:
    msgpack = None

from fastapi import WebSocket

from .. import config, db
from ..engine.broker import AsyncBroker
from ..dhan.feed import Feed
from ..dhan.rest import DhanError, DhanREST, TokenError
from ..delta import adapters as delta_adapters
from ..delta import proxy as proxy_mod
from ..engine import crt_tbs
from ..market import instruments as instr
from ..market import options as optc
from ..strikes import context as sctx
from ..strikes.selector import StrikeBook, select_strikes
from ..delta import confluence
from ..delta import engine as delta_engine
from ..engine import auction as auction_mod
from ..engine import cct as cct_mod
from ..engine import mmxm as mmxm_mod
from ..engine import qml_sbr as qml_mod
from ..engine import pa_advanced as pa_adv_mod
from ..engine import volume as vol_mod
from ..market import activity as act_mod
from ..market import futures as fut_mod
from ..delta.recorder import FutRecorder
from ..engine import oi as oi_mod
from ..engine import rvol as rvol_mod
from ..market import ivtrack as iv_mod
from ..engine import registry
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
        self.feed = Feed([self.nifty, self.vix], self.on_tick, self.on_prev, on_full=self.on_full)
        # real volume: current-month NIFTY future (REST candles + live Full packets) and options activity
        self.fut: Optional[Dict[str, Any]] = None
        self.fut_status = "not resolved yet"
        self.bn: Optional[Dict[str, Any]] = None
        self.bn_status = "not resolved yet"
        self.fut2: Optional[Dict[str, Any]] = None          # next-month future (REST-only, for Oct+Nov aggregation)
        self.fut2_status = "not resolved yet"
        self.recorder = FutRecorder(lambda: self.candles.last_price.get(self.nifty))
        self.activity = act_mod.Activity()
        self.ivtrack = iv_mod.IVTrack()
        self.volume: Dict[str, Dict[str, Any]] = {}
        self.chain = ChainPoller(self.rest, self.on_chain)
        self.alerts = alerts_mod.Alerts(self.broadcast)
        self.clients: Set[WebSocket] = set()
        self.client_formats: Dict[WebSocket, str] = {}
        self.broker = AsyncBroker()
        self.analyses: Dict[str, Analysis] = {}
        self.json: Dict[str, Dict[str, Any]] = {}
        self.signals: List[Signal] = []
        self.zones: Dict[str, List[registry.Zone]] = {}
        self.delta: Dict[str, Dict[str, Any]] = {}
        self.auction: Dict[str, Dict[str, Any]] = {}
        self.pa_advanced: Dict[str, Dict[str, Any]] = {}
        cur_ad = db.kv_get("delta_adapter")
        def_ad = config.rule("delta", "adapter", "fut_quote_rule")
        self.delta_adapter = self._make_adapter(cur_ad if (cur_ad and cur_ad != "none") else def_ad)
        # strike mode
        self.book = StrikeBook()
        self.flow: Dict[str, Dict[str, Dict[str, Any]]] = {}       # tf → {"CE": delta res, "PE": delta res}
        self.proxy: Dict[str, Dict[str, Any]] = {}                 # tf → proxy res
        self._mk: Dict[str, List[Dict[str, Any]]] = {}             # tf → markers without strike flow
        self.mode = db.kv_get("inst_mode") or "INDEX"               # INDEX | OPTION_CHART (experimental view)
        self.strike_expiry: Optional[str] = db.kv_get("strike_expiry")
        self.auto_strikes = db.kv_get("auto_strikes")
        if self.auto_strikes is None:
            self.auto_strikes = bool(config.rule("strikes", "auto_refresh", True))
        self.proxy_on = db.kv_get("proxy_on")
        if self.proxy_on is None:
            self.proxy_on = bool(config.rule("options_flow_proxy", "enabled", False))
        self._opt_busy = False
        # setup D — CRT + TBS (separate engine, never touches A/B/C)
        self.crt: Dict[str, Dict[str, Any]] = {}                  # pair id "1h>1m" → evaluate_pair() result
        self._crt_an: Dict[str, Any] = {}                          # htf → (last closed htf ts, Analysis)
        self.prev_close: Dict[str, float] = {}
        self.price_source = "feed"
        self.status: Dict[str, Any] = {"backfill": "pending", "token": "unknown", "errors": []}
        self._last_tick_push = 0.0
        self._tasks: List[asyncio.Task] = []
        self._seen_connects = 0
        self._last_intra_sweep_check = 0.0
        self._intra_sweeps_fired: Dict[str, float] = {}
        self._ws_seq: int = 0
        replay_size = int(config.rule("broker", "ws_replay_buffer_size", 256))
        self._replay_buffer: deque[Dict[str, Any]] = deque(maxlen=replay_size)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        db.conn()
        await self.broker.start()
        self.broker.subscribe(self.broker.channel_ticks, self._handle_broker_tick)
        self.broker.subscribe(self.broker.channel_signals, self._handle_broker_signal)
        await self.repair(full=True)
        await self.setup_futures()
        self.recompute_all(after_repair=True)
        for coro in (self.feed.run(), self.candles.sweep_timer(), self.candles.write_behind_timer(),
                     self.chain.run(), self.watchdog(), self.housekeeping(), self.futures_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        self.feed.stop()
        await self.broker.stop()
        self.candles.flush_write_behind()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rest.close()

    async def repair(self, full: bool = False) -> None:
        """REST backfill: on start (30 days), after every feed reconnect (today only), and at 15:31."""
        D = config.rules()["data"]
        try:
            n = await backfill(self.rest, self.nifty, int(D["backfill_days"]) if full else 1, candles_builder=self.candles)
            if full:
                await backfill_daily(self.rest, self.nifty, int(D["daily_days"]))
            self.status["backfill"] = f"ok · {n} one-minute candles · {now_ist():%H:%M:%S}"
            if self.fut and not full:
                await fut_mod.refresh(self.rest, self.fut["sid"], 1)
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
        """Ultra-low latency raw tick ingestion: dispatches to broker asynchronously."""
        await self.broker.publish(self.broker.channel_ticks, {"sid": sid, "ltp": ltp, "t": t})

    async def _handle_broker_tick(self, data: Dict[str, Any]) -> None:
        """Worker consumption for raw ticks: updates RAM buffers, triggers 1 Hz intra-sweep check, and throttled quote broadcast."""
        sid, ltp, t = str(data["sid"]), float(data["ltp"]), float(data["t"])
        if sid == self.vix and not (5 <= ltp <= 90):
            self.status["vix_warning"] = f"VIX id {self.vix} returned {ltp} — check vix_security_id in rules.json"
        await self.candles.tick(sid, ltp, t)
        if sid == self.nifty:
            await self._check_intra_sweep(ltp, t)
        if time.time() - self._last_tick_push >= 0.25:
            self._last_tick_push = time.time()
            await self.broadcast({"type": "quote", "quote": self.quote()})

    async def _check_intra_sweep(self, ltp: float, t: float) -> None:
        """Lightweight intra-candle check (throttled to 1 Hz) for live wick penetrations of armed liquidity pools and CRT levels."""
        now = time.time()
        throttle = float(config.rules().get("alerts", {}).get("intra_sweep_throttle_s", 1.0))
        if now - self._last_intra_sweep_check < throttle:
            return
        self._last_intra_sweep_check = now

        an = self.analyses.get(self.primary)
        if not an:
            return

        cooldown = float(config.rules().get("alerts", {}).get("intra_sweep_cooldown_s", 120.0))

        # 1. Armed liquidity pools check (PDH, PDL, EQH, EQL, ORH, ORL, IBH, IBL, SWING)
        for p in an.pools:
            if p.taken_i is not None:
                continue
            breached = False
            if p.side == "BSL" and ltp >= p.price:
                breached = True
            elif p.side == "SSL" and ltp <= p.price:
                breached = True

            if breached:
                key = f"intra_pool_{p.side}_{p.src}_{int(p.price)}"
                if now - self._intra_sweeps_fired.get(key, 0.0) >= cooldown:
                    self._intra_sweeps_fired[key] = now
                    title = f"⚡ LIVE SWEEP (IN-WICK): {p.src} {p.side} breached at {ltp:.1f}"
                    body = f"Live LTP {ltp:.1f} breached {p.src} level ({p.price:.1f}). Wick active, monitoring for close back inside."
                    alert_row = {
                        "ts": int(t),
                        "key": f"{key}_{int(t)}",
                        "tf": "live",
                        "kind": "IN_WICK_SWEEP",
                        "side": "bear" if p.side == "BSL" else "bull",
                        "grade": "info",
                        "title": title,
                        "body": body,
                        "price": ltp,
                        "payload": {"pool": p.src, "side": p.side, "level": p.price, "ltp": ltp},
                    }
                    await self.broadcast({"type": "alert", "alert": alert_row})

        # 2. Active CRT ranges check
        for pair_name, r in self.crt.items():
            crth = r.get("crth")
            crtl = r.get("crtl")
            if crth and ltp >= crth:
                key = f"intra_crt_high_{pair_name}_{int(crth)}"
                if now - self._intra_sweeps_fired.get(key, 0.0) >= cooldown:
                    self._intra_sweeps_fired[key] = now
                    title = f"⚡ LIVE CRT HIGH BREACH: {pair_name} at {ltp:.1f}"
                    body = f"Live LTP {ltp:.1f} breached CRT High ({crth:.1f}). Wick in progress."
                    alert_row = {
                        "ts": int(t),
                        "key": f"{key}_{int(t)}",
                        "tf": "live",
                        "kind": "CRT_HIGH_BREACH",
                        "side": "bear",
                        "grade": "info",
                        "title": title,
                        "body": body,
                        "price": ltp,
                        "payload": {"pair": pair_name, "level": crth, "ltp": ltp},
                    }
                    await self.broadcast({"type": "alert", "alert": alert_row})

            if crtl and ltp <= crtl:
                key = f"intra_crt_low_{pair_name}_{int(crtl)}"
                if now - self._intra_sweeps_fired.get(key, 0.0) >= cooldown:
                    self._intra_sweeps_fired[key] = now
                    title = f"⚡ LIVE CRT LOW BREACH: {pair_name} at {ltp:.1f}"
                    body = f"Live LTP {ltp:.1f} breached CRT Low ({crtl:.1f}). Wick in progress."
                    alert_row = {
                        "ts": int(t),
                        "key": f"{key}_{int(t)}",
                        "tf": "live",
                        "kind": "CRT_LOW_BREACH",
                        "side": "bull",
                        "grade": "info",
                        "title": title,
                        "body": body,
                        "price": ltp,
                        "payload": {"pair": pair_name, "level": crtl, "ltp": ltp},
                    }
                    await self.broadcast({"type": "alert", "alert": alert_row})

    async def on_full(self, p: Dict[str, Any], t: float) -> None:
        """NIFTY future Full packet → estimated delta + provisional FUT 1m volume (recorder)."""
        try:
            self.recorder.on_packet(p, t)
        except Exception:                                    # noqa: BLE001 — never lose the feed over the recorder
            log.exception("recorder")

    # ── futures (real volume) ─────────────────────────────────────────────
    async def setup_futures(self) -> None:
        if not config.rule("futures", "enabled", True):
            self.fut_status = "disabled in rules.json (futures.enabled)"
            return
        q = self.quote()
        try:
            pick, why = await fut_mod.resolve(self.rest, float(q.get("spot") or 0))
        except Exception as e:                               # noqa: BLE001
            pick, why = None, f"resolve failed: {str(e)[:160]}"
        if not pick:
            self.fut_status = f"unavailable — {why}"
            log.warning("NIFTY future: %s", self.fut_status)
            return
        changed = not self.fut or self.fut["sid"] != pick["sid"]
        self.fut = pick
        self.fut_status = f"{pick['symbol']} (id {pick['sid']}" + (f", expiry {pick['expiry']})" if pick.get("expiry") else ")")
        try:
            days, note = (fut_mod.history_days(fut_mod.SEC, pick["sid"], int(config.rule("futures", "backfill_days", 35)))
                          if changed else (1, ""))
            n = await fut_mod.refresh(self.rest, pick["sid"], days)
            self.fut_status += f" · {n} one-minute candles" + (f" · {note}" if note else "")
            if note:
                log.warning("NIFTY future %s", note)
        except DhanError as e:
            self.fut_status += f" · candles failed: {str(e)[:100]}"
        if changed:
            await self.feed.subscribe_full("NSE_FNO", pick["sid"])
            log.info("NIFTY future: %s — %s", self.fut_status, why)
        await self.setup_banknifty()
        await self.setup_second_future()

    async def setup_second_future(self) -> None:
        """Next-month NIFTY future — REST only (no live packet → zero connection slots). ADDITIVE: any failure here
        is swallowed and never touches the active contract or the rest of the desk. Stored under FUT2."""
        if not config.rule("futures", "second_future", True):
            self.fut2_status = "disabled (futures.second_future)"
            return
        try:
            q = self.quote()
            pick, why = await fut_mod.resolve_second(self.rest, float(q.get("spot") or 0))
            if not pick:
                self.fut2_status = f"unavailable — {why}"
                return
            changed = not self.fut2 or self.fut2["sid"] != pick["sid"]
            self.fut2 = pick
            days, _ = (fut_mod.history_days(fut_mod.SEC2, pick["sid"], int(config.rule("futures", "backfill_days", 35)))
                       if changed else (1, ""))
            n = await fut_mod.refresh(self.rest, pick["sid"], days, fut_mod.SEC2)
            self.fut2_status = f"{pick['symbol']} (id {pick['sid']}) · {n} 1m candles · basis {pick.get('basis')}"
        except Exception as e:                               # noqa: BLE001 — never break the desk over the aggregation leg
            self.fut2_status = f"error: {str(e)[:120]}"

    async def setup_banknifty(self) -> None:
        """BANKNIFTY current-month future — OI build-up as confirmation (REST only, 1 call a minute)."""
        if not config.rule("futures", "banknifty", True):
            self.bn_status = "disabled in rules.json (futures.banknifty)"
            return
        try:
            idx = await fut_mod.index_close(self.rest, str(config.rule("futures", "banknifty_index_id", "25")))
            pick, why = await fut_mod.resolve(self.rest, float(idx or 0), "BANKNIFTY-")
        except Exception as e:                               # noqa: BLE001
            pick, why = None, f"resolve failed: {str(e)[:160]}"
        if not pick:
            self.bn_status = f"unavailable — {why}"
            return
        changed = not self.bn or self.bn["sid"] != pick["sid"]
        self.bn = pick
        self.bn_status = f"{pick['symbol']} (id {pick['sid']})"
        try:
            days, _ = (fut_mod.history_days(fut_mod.BN_SEC, pick["sid"], int(config.rule("futures", "backfill_days", 35)))
                       if changed else (1, ""))
            n = await fut_mod.refresh(self.rest, pick["sid"], days, fut_mod.BN_SEC)
            self.bn_status += f" · {n} one-minute candles"
        except DhanError as e:
            self.bn_status += f" · candles failed: {str(e)[:100]}"

    async def futures_loop(self) -> None:
        """Re-resolve hourly (rollover) and fetch the official FUT candle a few seconds after each minute."""
        last_resolve = time.time()
        while True:
            await asyncio.sleep(5)
            try:
                if time.time() - last_resolve > float(config.rule("futures", "resolve_every_s", 3600)) or not self.fut:
                    last_resolve = time.time()
                    if market_open_now() or not self.fut:
                        await self.setup_futures()
            except Exception:                                # noqa: BLE001
                log.exception("futures loop")

    def _budget_skip(self) -> bool:
        """Over this desk's daily Data-API budget: optional refreshes run only every 5th minute."""
        if not self.rest.over_budget():
            return False
        self._bud_n = getattr(self, "_bud_n", 0) + 1
        return self._bud_n % 5 != 0

    async def _fut_after_close(self) -> None:
        await asyncio.sleep(float(config.rule("futures", "candle_fetch_delay_s", 4)))
        if self.fut and not self._budget_skip():
            try:
                await fut_mod.refresh(self.rest, self.fut["sid"], 1)
            except DhanError as e:
                self.fut_status = self.fut_status.split(" · ")[0] + f" · refresh failed: {str(e)[:80]}"
            if self.bn:
                try:
                    await fut_mod.refresh(self.rest, self.bn["sid"], 1, fut_mod.BN_SEC)
                except DhanError as e:
                    self.bn_status = self.bn_status.split(" · ")[0] + f" · refresh failed: {str(e)[:80]}"

    async def on_prev(self, sid: str, pc: float) -> None:
        self.prev_close[sid] = pc

    async def on_close_1m(self, sid: str, c: Dict[str, float]) -> None:
        """Asynchronously dispatches candle close events to the dedicated signal worker."""
        if sid != self.nifty:
            return
        await self.broker.publish(self.broker.channel_signals, {"sid": sid, "candle": c})

    async def _handle_broker_signal(self, data: Dict[str, Any]) -> None:
        """Worker consumption for closed candles: executes heavy ICT/SMC mathematical calculations."""
        sid = str(data["sid"])
        c = data["candle"]
        try:
            self.recorder.close_minute(int(c["ts"]))           # estimated delta + provisional FUT volume first
        except Exception:                                    # noqa: BLE001
            log.exception("recorder close")
        if self.fut:
            asyncio.create_task(self._fut_after_close())
        for closed_trade in paper.on_1m_close_all(c):
            await self.broadcast({"type": "paper", "trade": closed_trade})
        end = c["ts"] + 60
        _, sess_close = session_bounds(ist_date(c["ts"]))
        for tf in self.tfs:
            step = TF_SECONDS[tf]
            b = bucket_start(c["ts"], tf)
            if end == b + step or end >= sess_close:          # this minute closed a tf candle
                await asyncio.to_thread(self.recompute, tf)
                if tf == self.primary:
                    await self.run_playbook()
                await self.broadcast({"type": "analysis", "tf": tf, "analysis": self.json.get(tf)})
        for pair in self.crt_pairs():
            ltf = pair["ltf"]
            if end == bucket_start(c["ts"], ltf) + TF_SECONDS[ltf] or end >= sess_close:
                fresh = await asyncio.to_thread(self.recompute_crt, pair)
                await self.act_on_crt(fresh)
        await self.broadcast({"type": "candle", "tf": "1m", "candle": _cj(c)})
        if self.option_instruments():
            asyncio.create_task(self._options_after_close())
        try:
            await self.rvol_alerts()
        except Exception:                                    # noqa: BLE001 — alerts must never break the loop
            log.exception("rvol alerts")

    async def on_chain(self, expiry: str, ch: Dict[str, Any]) -> None:
        if self.chain.expiries and expiry == self.chain.expiries[0]:
            try:
                self.activity.ingest(ch)
                picks = {}
                for side, rec in self.book.current().items():
                    ins = (rec or {}).get("instrument") or {}
                    if ins.get("expiry") == expiry and ins.get("strike") is not None:
                        picks[side] = {"strike": float(ins["strike"]), "key": ins.get("key") or f"{side}{ins['strike']}"}
                self.ivtrack.ingest(ch, picks)
            except Exception:                                # noqa: BLE001
                log.exception("activity / iv")
        live_spot = self.candles.last_price.get(self.nifty)
        await self.broadcast({"type": "chain", "expiry": expiry, "summary": self.chain.summary(expiry, live_spot=live_spot)})
        if expiry == self.sel_expiry() and config.rule("strikes", "enabled", True):
            try:
                await self.reselect(force=False)
            except Exception as e:                           # noqa: BLE001
                log.exception("strike selection failed")
                self.status["errors"] = (self.status["errors"] + [f"strikes: {e}"])[-5:]

    # ── strike mode ───────────────────────────────────────────────────────
    def sel_expiry(self) -> Optional[str]:
        ex = self.chain.expiries or sorted(self.chain.chains)
        if self.strike_expiry and self.strike_expiry in self.chain.chains:
            return self.strike_expiry
        k = int(config.rule("strikes", "expiry_index", 0))
        return ex[min(k, len(ex) - 1)] if ex else None

    def snapshot(self) -> Optional[Dict[str, Any]]:
        exp = self.sel_expiry()
        ch = self.chain.chains.get(exp) if exp else None
        if not ch:
            return None
        q = self.quote()
        today = self.candles.today_ohlc(self.nifty) or {}
        return {"expiry": exp, "spot": q.get("spot") or ch.get("spot"), "chain": ch, "open": today.get("o"),
                "prev_close": self.prev_close.get(self.nifty)}

    async def reselect(self, force: bool = False) -> Dict[str, Any]:
        snap = self.snapshot()
        if not snap:
            return {"reasons": ["No option chain loaded yet."], "calls": [], "puts": []}
        before = {k: (v or {}).get("instrument", {}).get("key") for k, v in self.book.current().items()}
        res = select_strikes(snap)
        if self.auto_strikes or force:
            notes = self.book.update(res, now_epoch(), force=force)
        else:
            self.book.last, notes = res, []
        after = {k: (v or {}).get("instrument", {}).get("key") for k, v in self.book.current().items()}
        if after != before:
            db.execute("INSERT OR REPLACE INTO strike_picks (ts, expiry, ce_key, pe_key, payload) VALUES (?,?,?,?,?)",
                       (int(now_epoch()), snap["expiry"], after.get("CE"), after.get("PE"),
                        json.dumps({"current": self.book.current(), "notes": notes, "reasons": res["reasons"]},
                                   default=_jdefault)))
            asyncio.create_task(self.refresh_options())
            await self.broadcast({"type": "strikes", "strikes": self.strikes_view()})
        return res

    def option_instruments(self) -> List[instr.Instrument]:
        out = [i for i in self.book.instruments() if i.security_id]
        for t in paper.open_trades():
            if not (t.get("opt_sid") and t.get("opt_key")):
                continue
            try:
                pi = instr.parse_key(t["opt_key"])
                pi = instr.option(pi.expiry, pi.strike, pi.right, t["opt_sid"])
                if pi.key not in {i.key for i in out}:
                    out.append(pi)
            except ValueError:
                pass
        return out

    async def refresh_options(self) -> None:
        if self._opt_busy or self._budget_skip():
            return
        self._opt_busy = True
        try:
            for inst in self.option_instruments():
                try:
                    await optc.refresh(self.rest, inst, int(config.rule("strikes", "candles_days", 2)))
                except (DhanError, ValueError) as e:
                    self.status["option_candles"] = f"{inst.label}: {str(e)[:120]}"
            paper.fill_option_prices()
            for tf in self.tfs:
                self.recompute_flow(tf)
            if self.analyses.get(self.primary):
                self.signals = self.evaluate_primary()           # confidence picks up the strike flow
            await self.broadcast({"type": "analysis", "tf": self.primary, "analysis": self.json.get(self.primary)})
        finally:
            self._opt_busy = False

    async def _options_after_close(self) -> None:
        await asyncio.sleep(float(config.rule("strikes", "candle_fetch_delay_s", 4)))
        await self.refresh_options()

    def recompute_flow(self, tf: str) -> None:
        an, zs = self.analyses.get(tf), self.zones.get(tf)
        if an is None or zs is None:
            return
        cur = self.book.current()
        sides: Dict[str, Dict[str, Any]] = {}
        res: Dict[str, Dict[str, Any]] = {}
        for right in ("CE", "PE"):
            rec = cur.get(right)
            if not rec or not rec["instrument"].get("security_id"):
                continue
            i = rec["instrument"]
            inst = instr.option(i["expiry"], i["strike"], i["right"], i["security_id"])
            candles = optc.by_ts(inst, tf, int(config.rule("strikes", "candles_days", 2)))
            sides[right] = {"instrument": inst, "candles": candles, "abs_delta": abs(rec.get("delta") or 1.0)}
            try:
                r = delta_engine.compute(an, zs, self.delta_adapter, flow={"instrument": inst, "candles": candles})
                if r["available"] and not r["mock"]:
                    delta_engine.persist(r["events"])
            except Exception as e:                           # noqa: BLE001
                r = {"available": False, "mock": False, "events": [], "bars": [], "message": f"strike delta error: {e}"}
            res[right] = r
        self.flow[tf] = res
        try:
            self.proxy[tf] = proxy_mod.compute(an, zs, sides.get("CE"), sides.get("PE"), enabled=self.proxy_on)
        except Exception as e:                               # noqa: BLE001
            self.proxy[tf] = {"enabled": self.proxy_on, "available": False, "events": [], "bars": [],
                              "label": proxy_mod.LABEL, "message": f"proxy error: {e}"}
        j = self.json.get(tf)
        if j is not None:
            first_ts = int(an.bars.ts[max(0, len(an.bars) - 150)])
            extra = []
            for r in res.values():
                extra += delta_engine.markers(r.get("events", []), first_ts)
            extra += proxy_mod.markers(self.proxy[tf], first_ts)
            j["markers"] = sorted(self._mk.get(tf, []) + extra, key=lambda m: m["time"])
            j["proxy"] = {k: self.proxy[tf].get(k) for k in ("enabled", "available", "label", "message")}
            j["strike_flow"] = {k: {"instrument": v.get("instrument_label"), "available": v.get("available"),
                                    "message": v.get("message")} for k, v in res.items()}

    def combined_flow(self, tf: str) -> Dict[str, Any]:
        """Index delta + strike delta (already in NIFTY direction) as one source for confluence."""
        parts = [self.delta.get(tf) or {}] + list((self.flow.get(tf) or {}).values())
        av = [p for p in parts if p.get("available")]
        return {"available": bool(av), "mock": any(p.get("mock") for p in av),
                "events": sorted([e for p in av for e in p.get("events", [])], key=lambda e: e["ts"])}

    def foci(self) -> List[Dict[str, Any]]:
        out = []
        for right, rec in self.book.current().items():
            if not rec:
                continue
            i = rec["instrument"]
            inst = instr.option(i["expiry"], i["strike"], i["right"], i.get("security_id"))
            out.append(sctx.focus(right, instr.chain_row(inst, self.chain.chains), inst.label))
        return out

    def direction(self) -> Optional[str]:
        live_sig = [s for s in self.today_signals() if s.setup in ("A", "C")]
        if live_sig:
            return live_sig[-1].side
        p = self.analyses.get(self.primary)
        return {"bull": "long", "bear": "short"}.get(p.trend) if p else None

    def strikes_view(self) -> Dict[str, Any]:
        foci = self.foci()
        last = self.book.last or {}
        return {"expiry": self.sel_expiry(), "expiries": self.chain.expiries, "auto": self.auto_strikes,
                "mode": self.mode, "current": self.book.current(),
                "manual": {k: bool(v) for k, v in self.book.manual.items()},
                "candidates": {"calls": last.get("calls", []), "puts": last.get("puts", [])},
                "reasons": last.get("reasons", []), "picks": last.get("picks", {}), "log": self.book.log[-12:],
                "focus": foci, "verdict": sctx.verdict(self.direction(), foci),
                "switch_rule": f"switch only on a {config.rule('strikes', 'switch_tf', '3m')} close, "
                               f"≥ {int(100 * float(config.rule('strikes', 'switch_min_improve', 0.15)))} % better",
                "proxy_on": self.proxy_on, "proxy_label": proxy_mod.LABEL}

    # ── analysis ──────────────────────────────────────────────────────────
    def rows_1m(self, days: int = 3) -> List[Dict[str, float]]:
        return self.candles.frame(self.nifty, "1m", days=days, include_forming=False)

    def _make_adapter(self, name: str):
        try:
            return delta_adapters.make(name)
        except ValueError:
            log.warning("unknown delta adapter %s — using footprint_feed", name)
            return delta_adapters.make("footprint_feed")

    def set_adapter(self, name: str) -> Dict[str, Any]:
        self.delta_adapter = delta_adapters.make(name)              # raises ValueError on a bad name
        db.kv_set("delta_adapter", name)
        self.recompute_all()
        return self.delta_adapter.status()

    def recompute(self, tf: str, after_repair: bool = False) -> None:
        rows = self.candles.frame(self.nifty, tf, days=DAYS_FOR_TF.get(tf, 5), include_forming=False)
        an = analyze(rows, tf, rows_1m=self.rows_1m(2), daily=db.daily(self.nifty, 10))
        if an is None:
            return
        self.analyses[tf] = an
        j = to_json(an)
        # zone registry: normalize → forward-only merge with SQLite (void only after a REST repair)
        E = config.rules()["engine"]
        guard = (int(E["atr_len"]) * 3 + int(E["pivot_n"].get(tf, 2))) * TF_SECONDS[tf]   # warm-up bars can differ
        try:
            zs = registry.reconcile(tf, registry.build(an), (int(an.bars.ts[0]), int(an.bars.ts[-1])),
                                    after_repair=after_repair, guard_s=guard)
        except Exception as e:                               # noqa: BLE001 — never lose the chart over the registry
            log.exception("registry %s failed", tf)
            zs = registry.build(an)
            self.status["errors"] = (self.status["errors"] + [f"registry {tf}: {e}"])[-5:]
        self.zones[tf] = zs
        # delta / footprint (closed candles only — compute() re-checks against the clock)
        try:
            dres = delta_engine.compute(an, zs, self.delta_adapter)
            if dres["available"] and not dres["mock"]:
                delta_engine.persist(dres["events"])
        except Exception as e:                               # noqa: BLE001
            log.exception("delta %s failed", tf)
            dres = {"tf": tf, "available": False, "mock": False, "message": f"delta error: {e}", "events": [], "bars": []}
        self.delta[tf] = dres
        vctx = self.volume_for(tf, an, j)
        first_ts = int(an.bars.ts[max(0, len(an.bars) - 150)])
        j["markers"] = sorted(j["markers"] + delta_engine.markers(dres["events"], first_ts), key=lambda m: m["time"])
        self._mk[tf] = list(j["markers"])
        j["delta"] = {k: dres.get(k) for k in ("adapter", "available", "mock", "message", "has_levels")}
        j["registry"] = {"zones": len(zs), "active": sum(1 for z in zs if z.active_for(int(an.bars.ts[-1]) + 1))}
        j["crt"] = self.crt_view(tf)
        # auction read — observation only (never trades, never touches A/B/C/D)
        try:
            real = dres.get("available") and not dres.get("mock")
            dz = {int(x["ts"]): x["z"] for x in dres.get("bars", []) if x.get("z") is not None} if dres.get("available") else None
            lbl = f"{dres.get('adapter')}{' (MOCK)' if dres.get('mock') else ''}" if dres.get("available") else "unavailable"
            ares = auction_mod.compute(an, self.rows_1m(4), dz if (real or dres.get("mock")) else None, lbl, vctx)
            self.auction[tf] = ares
            j["auction"] = {k: ares[k] for k in ("today", "prev", "boxes", "read", "refs")} | {
                "markers": auction_mod.markers(ares, int(an.bars.ts[max(0, len(an.bars) - 150)]))}
        except Exception as e:                               # noqa: BLE001 — never lose the chart over the auction read
            log.exception("auction %s failed", tf)
            j["auction"] = None
        # QML · RBS / SBR on the line chart — observation only (never trades, never touches A/B/C/D)
        try:
            Nn = len(an.bars)
            Tq = lambda i: chart_time(int(an.bars.ts[min(max(i, 0), Nn - 1)]))   # noqa: E731
            q = qml_mod.compute(an.bars, an.atr, int(E["pivot_n"].get(tf, 2)), Tq, hm, config.rules().get("qml", {}))
            cur = self.book.current() or {}
            for s in q["setups"]:
                rec = cur.get("PE" if s["dir"] == "bear" else "CE") or {}
                ins = rec.get("instrument") or {}
                s["strike"] = (f"{ins.get('expiry', '')} {int(ins['strike'])} {'PE' if s['dir'] == 'bear' else 'CE'}"
                               if ins.get("strike") is not None else None)
            j["qml"] = q
        except Exception as e:                               # noqa: BLE001 — never lose the chart over this layer
            log.exception("qml %s failed", tf)
            j["qml"] = {"setups": [], "markers": [], "summary": f"QML error: {e}"}

        # MMXM — Market Maker Models (MMBM / MMSM) State Engine
        try:
            mmxm_res = mmxm_mod.compute_mmxm(an.bars, an.atr, an.swings, an.events, an.fvgs, an.obs, tf, config.rules().get("mmxm", {}), j.get("smt", {}).get("markers") if isinstance(j.get("smt"), dict) else None)
            j["mmxm"] = mmxm_res
        except Exception as e:                               # noqa: BLE001
            log.exception("mmxm %s failed", tf)
            j["mmxm"] = {"available": False, "message": f"MMXM error: {e}", "models": [], "markers": [], "lines": [], "stats": {}}
            mmxm_res = None

        # CCT — Candle Continuity Theory & 4-Candle Execution Framework (MMXM-Aware)
        try:
            cct_res = cct_mod.compute_cct(an.bars, an.atr, an.swings, an.fvgs, an.obs, an.sess, tf, config.rules().get("cct", {}), mmxm_data=mmxm_res)
            j["cct"] = cct_res
        except Exception as e:                               # noqa: BLE001
            log.exception("cct %s failed", tf)
            j["cct"] = {"available": False, "message": f"CCT error: {e}", "triggers": [], "markers": [], "lines": [], "stats": {}}

        # 10 Advanced Price Action & Institutional Concepts (Observation only — alert/inspection)
        try:
            htf_target = "15m" if tf in ("1m", "5m") else "1h" if tf == "15m" else "1D"
            htf_an = self.analyses.get(htf_target)
            last_vw = j.get("volume", {}).get("last_vwap", {}).get("v") if isinstance(j.get("volume"), dict) else None
            chain_sum = self.chain.summary() if hasattr(self, "chain") else None
            pa_res = pa_adv_mod.compute_pa_advanced(
                an.bars, an.atr, an.swings, an.obs, an.fvgs, an.pools, an.rng,
                vwap_val=last_vw, htf_an=htf_an, tf=tf, chain_summary=chain_sum,
                rules_cfg=config.rules().get("pa_advanced", {})
            )
            # Option strike selection & Greeks filter (BUY CE / BUY PE Scanner)
            try:
                cur = self.book.current() or {}
                foci_list = self.foci()
                foci_map = {f.get("label", "").split()[-1]: f for f in foci_list}
                ce_rec = cur.get("CE") or {}
                pe_rec = cur.get("PE") or {}
                pa_res["strikes"] = {
                    "CE": {
                        "strike": ce_rec.get("instrument", {}).get("strike"),
                        "label": ce_rec.get("instrument", {}).get("label"),
                        "ltp": ce_rec.get("ltp"),
                        "delta": ce_rec.get("delta"),
                        "theta": ce_rec.get("theta"),
                        "iv": ce_rec.get("iv"),
                        "oi": ce_rec.get("oi"),
                        "focus": foci_map.get("CE"),
                    } if ce_rec else None,
                    "PE": {
                        "strike": pe_rec.get("instrument", {}).get("strike"),
                        "label": pe_rec.get("instrument", {}).get("label"),
                        "ltp": pe_rec.get("ltp"),
                        "delta": pe_rec.get("delta"),
                        "theta": pe_rec.get("theta"),
                        "iv": pe_rec.get("iv"),
                        "oi": pe_rec.get("oi"),
                        "focus": foci_map.get("PE"),
                    } if pe_rec else None,
                }
            except Exception as se:
                log.warning("enrich pa_advanced strikes failed: %s", se)

            self.pa_advanced[tf] = pa_res
            j["pa_advanced"] = pa_res
        except Exception as e:                               # noqa: BLE001
            log.exception("pa_advanced %s failed", tf)
            j["pa_advanced"] = {"available": False, "message": f"PA Advanced error: {e}", "summary": "Error", "zones": [], "markers": []}

        self.json[tf] = j
        self.recompute_flow(tf)

    # ── setup D: CRT + TBS ────────────────────────────────────────────────
    def crt_pairs(self) -> List[Dict[str, Any]]:
        C = config.rules().get("crt_tbs", {})
        if not C.get("enabled", True):
            return []
        return [p for p in C.get("pairs", []) if p.get("enabled", True)]

    def recompute_crt(self, pair: Dict[str, Any]) -> List[Signal]:
        """Re-evaluate one pair on CLOSED candles; returns signals whose entry candle just closed."""
        D = config.rules().get("crt_tbs", {}).get("days", {})
        htf, ltf = pair["htf"], pair["ltf"]
        try:
            hr = [r for r in self.candles.frame(self.nifty, htf, days=int(D.get("htf", {}).get(htf, 30)),
                                                include_forming=False) if r.get("closed", True)]
            lr = self.candles.frame(self.nifty, ltf, days=int(D.get("ltf", {}).get(ltf, 3)), include_forming=False)
            key = hr[-1]["ts"] if hr else None
            cached = self._crt_an.get(htf)
            an = cached[1] if cached and cached[0] == key else analyze(hr, htf)
            self._crt_an[htf] = (key, an)
            res = crt_tbs.evaluate_pair(pair, hr, lr, now_epoch(), an=an, bias_an=self.analyses.get(self.bias_tf))
        except Exception as e:                               # noqa: BLE001 — never lose the desk over setup D
            log.exception("crt %s>%s failed", htf, ltf)
            self.status["errors"] = (self.status["errors"] + [f"crt {htf}>{ltf}: {e}"])[-5:]
            return []
        self.crt[res["pair"]] = res
        for tf, j in self.json.items():
            j["crt"] = self.crt_view(tf)
        return crt_tbs.live(res, int(lr[-1]["ts"]) if lr else None)

    async def act_on_crt(self, fresh: List[Signal]) -> None:
        if not fresh:
            return
        C = config.rules().get("crt_tbs", {})
        if C.get("alerts", True):
            await self.alerts.handle(fresh)
        if market_open_now():
            for s in fresh:
                t = paper.maybe_open(s, strikes=self.book.current(), mode=self.mode)
                if t:
                    await self.broadcast({"type": "paper", "trade": t})

    # ── volume (NIFTY FUT, basis-adjusted) ────────────────────────────────
    def mapped_1m(self) -> List[Dict[str, float]]:
        key = (db.last_candle_ts(fut_mod.SEC), db.last_candle_ts(self.nifty))
        if getattr(self, "_mp_key", None) != key:
            self._mp_key = key
            self._mp = fut_mod.mapped(self.rows_1m(8), fut_mod.rows_1m(8)) if key[0] else []
            self._rv_cache: Dict[str, Any] = {}
        return self._mp

    def rvol_tf(self, tf: str) -> Dict[int, Dict[str, Any]]:
        mp = self.mapped_1m()
        if tf not in self._rv_cache:
            self._rv_cache[tf] = vol_mod.rvol(mp, tf) if mp else {}
        return self._rv_cache[tf]

    def volume_for(self, tf: str, an, j: Dict[str, Any]) -> Dict[str, Any]:
        """Volume layer for one chart tf + RVOL annotations on OB / FVG / BOS / MSS / sweeps. Display only.
        Returns the context the auction read uses (volume profiles, RVOL, options activity)."""
        try:
            mp = self.mapped_1m()
            b, N = an.bars, len(an.bars)
            t0 = int(b.ts[max(0, N - 300)])
            act = act_mod.per_bar(act_mod.rows_since(t0 - 6 * 3600), tf, int(config.rule("activity", "z_window", 30)))
            iv = self.iv_view(tf, an)
            if not mp:
                j["volume"] = {"available": False, "message": f"NIFTY future: {self.fut_status}",
                               "act_last": act.get(int(b.ts[-1])), **iv}
                return {"act": act, "iv_trend": iv.get("iv_trend")}
            oib = oi_mod.per_bar(an, fut_mod.oi_rows(8))
            ois = oi_mod.summary(an, oib, int(config.rule("oi", "summary_bars", 12)))
            oil = oi_mod.oi_line(an, oib)
            bnv = self.bn_view(tf, an)
            cmp = oi_mod.compare(ois, bnv.get("summary"), (oib.get(int(b.ts[-1])) or {}).get("build"), bnv.get("last_build"))
            rv = self.rvol_tf(tf)
            summ = vol_mod.annotate(j, an, rv)
            profiles = vol_mod.day_profiles(mp)
            today = ist_date(int(b.ts[-1])).isoformat()
            step = TF_SECONDS[tf]
            bars, idx = [], {}
            for k in range(max(0, N - 300), N):
                ts = int(b.ts[k])
                x = rv.get(ts) or {}
                o = oib.get(ts) or {}
                bars.append({"time": chart_time(ts), "v": x.get("v", 0), "rvol": x.get("rvol"), "up": bool(b.c[k] >= b.o[k]),
                             "oi": o.get("oi"), "doi": o.get("doi"), "build": o.get("build")})
                idx[ts] = k

            def sample(series: List[Dict[str, Any]], from_ts: int = 0) -> List[Dict[str, Any]]:
                by = {p["ts"]: p for p in series}
                out = []
                for ts in idx:
                    if ts < from_ts:
                        continue
                    p = None
                    for mm in range(ts + step - 60, ts - 60, -60):   # value at the bar's last minute (its close)
                        p = by.get(mm)
                        if p:
                            break
                    if p:
                        out.append({"time": chart_time(ts), "v": p["vwap"], "sd": p["sd"]})
                return out

            days = sorted({ist_date(ts).isoformat() for ts in idx})
            vw: List[Dict[str, Any]] = []
            for d in days:
                vw += sample(vol_mod.session_vwap(mp, d))
            avwaps = []
            mss = next((e for e in reversed(an.events) if e.is_mss), None)
            if mss is not None:
                a_ts = int(b.ts[mss.origin_i])
                avwaps.append({"label": f"AVWAP · MSS leg {hm(a_ts)}", "anchor": chart_time(a_ts), "dir": mss.dir,
                               "points": sample(vol_mod.vwap_series(mp, a_ts), a_ts)})
            crs = [r for pr in self.crt.values() for r in pr.get("ranges", []) if r.get("status") in ("armed", "tbs", "entered")]
            if crs:
                cr = max(crs, key=lambda r: r["k_ts"])
                avwaps.append({"label": f"AVWAP · CRT {cr['htf']} {hm(cr['k_ts'])}", "anchor": chart_time(cr["k_ts"]),
                               "dir": "flat", "points": sample(vol_mod.vwap_series(mp, int(cr["k_ts"])), int(cr["k_ts"]))})
            strip = lambda p: {k: v for k, v in p.items() if k != "hist"} if p else None   # noqa: E731
            prev_day = max([d for d in profiles if d < today], default=None)
            naked = vol_mod.naked_pocs(profiles, mp, today)
            last_vw = vw[-1] if vw else None
            j["volume"] = {"available": True, "contract": self.fut_status, "basis": mp[-1]["basis"], "bars": bars,
                           "vwap": vw, "bands": config.rule("volume", "vwap_bands", [1, 2]), "avwaps": avwaps,
                           "today": strip(profiles.get(today)), "prev": strip(profiles.get(prev_day)) if prev_day else None,
                           "naked": naked, "last": summ, "act_last": act.get(int(b.ts[-1])),
                           "last_vwap": last_vw, "rvol_method": summ.get("method"),
                           "oi_last": oib.get(int(b.ts[-1])), "oi_summary": ois,
                           "oi_line": [dict(x, time=chart_time(x["ts"])) for x in oil[-300:]],
                           "oi_surge_last": next((x for x in reversed(oil) if x["surge"]), None),
                           "banknifty": bnv, "oi_compare": cmp, **iv, **self.rvol_view(tf, an, rv)}
            self.volume[tf] = dict(j["volume"], profiles={d: profiles[d] for d in sorted(profiles)[-3:]})
            return {"profiles": profiles, "rvol": rv, "act": act, "oi": oib, "iv_trend": iv.get("iv_trend"),
                    "crvol": j["volume"].get("rvol_state"),
                    "oi_line": {x["ts"]: x for x in oil}, "oi_compare": cmp}
        except Exception as e:                               # noqa: BLE001 — never lose the chart over volume
            log.exception("volume %s failed", tf)
            j["volume"] = {"available": False, "message": f"volume error: {e}"}
            return {}

    def rvol_view(self, tf: str, an, rv: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        crv = self.rvol_now()
        if not crv.get("available"):
            return {"rvol_state": {"state": "UNKNOWN", "text": crv.get("message", "no data")}, "crvol": [], "rvol_events": []}
        ts = [int(t) for t in an.bars.ts[-300:]]
        pb = rvol_mod.per_bar(crv, tf, ts)
        evs = rvol_mod.events(crv, rv, self.breakouts(an), tf)
        return {"rvol_state": rvol_mod.state(crv), "rvol_days": crv.get("days"), "rvol_baseline": crv.get("baseline"),
                "crvol": [{"time": chart_time(t), "c20": p.get("c20"), "c5": p.get("c5")} for t, p in sorted(pb.items())],
                "rvol_events": [{k: e[k] for k in ("kind", "time", "at", "title", "value")} for e in evs]}

    def bn_view(self, tf: str, an) -> Dict[str, Any]:
        """BANKNIFTY future on the same timeframe: build-up per candle + summary over the same window."""
        from types import SimpleNamespace
        from ..engine.primitives import Bars, atr as atr_fn
        from ..market.candles import resample
        rows = fut_mod.sym_rows_1m(fut_mod.BN_SEC, 8)
        if not rows:
            return {"available": False, "message": f"BANKNIFTY future: {self.bn_status}"}
        bars = [r for r in resample(rows, tf) if r["closed"] and int(r["ts"]) <= int(an.bars.ts[-1])]
        if len(bars) < 3:
            return {"available": False, "message": "BANKNIFTY future: not enough candles yet"}
        bb = Bars.from_rows(bars)
        abn = SimpleNamespace(tf=tf, bars=bb, atr=atr_fn(bb, 14))
        pb = oi_mod.per_bar(abn, fut_mod.sym_oi_rows(fut_mod.BN_SEC, 8))
        summ = oi_mod.summary(abn, pb, int(config.rule("oi", "summary_bars", 12)))
        last = pb.get(int(bb.ts[-1])) or {}
        recent = [{"time": chart_time(int(t)), "build": pb[int(t)].get("build"), "doi": pb[int(t)].get("doi")}
                  for t in bb.ts[-12:] if int(t) in pb and "build" in pb[int(t)]]
        return {"available": True, "contract": self.bn_status, "summary": summ, "last_build": last.get("build"),
                "oi": last.get("oi"), "recent": recent, "close": float(bb.c[-1])}

    # ── cumulative RVOL (NIFTY future) ────────────────────────────────────
    def rvol_now(self) -> Dict[str, Any]:
        key = db.last_candle_ts(fut_mod.SEC)
        if getattr(self, "_crv_key", None) != key:
            self._crv_key = key
            self._crv = rvol_mod.cumulative(fut_mod.rows_1m(int(config.rule("rvol", "history_days", 40)))) if key else \
                {"available": False, "message": f"NIFTY future: {self.fut_status}"}
        return self._crv

    def breakouts(self, an) -> List[Dict[str, Any]]:
        """BOS / MSS of this timeframe + CRT TBS candles — the 'breakouts' RVOL is asked to confirm."""
        out = [{"ts": int(e.ts), "kind": "MSS" if e.is_mss else "BOS", "dir": e.dir,
                "text": f"{'MSS' if e.is_mss else 'BOS'} {e.dir} through {e.level:,.1f}"} for e in an.events]
        step = TF_SECONDS[an.tf]
        for r in self.crt.values():
            for ev in r.get("events", []):
                if ev.get("kind") == "CRT_TBS" and ev.get("ts"):
                    out.append({"ts": bucket_start(int(ev["ts"]), an.tf), "kind": "CRT TBS", "dir": ev.get("dir", ""),
                                "text": f"CRT TBS ({r.get('name', '')})"})
        return [b for b in out if b["ts"] + step <= int(an.bars.ts[-1]) + step]

    async def rvol_alerts(self) -> None:
        an = self.analyses.get(self.primary)
        crv = self.rvol_now()
        if an is None or not crv.get("available"):
            return
        evs = rvol_mod.events(crv, self.rvol_tf(self.primary) if self.mapped_1m() else {}, self.breakouts(an), self.primary)
        for e in evs:
            e["tf"] = self.primary
            await alerts_mod.notify_event(self.alerts, e, int(config.rule("rvol", "alert_max_age_s", 600)))

    def iv_view(self, tf: str, an) -> Dict[str, Any]:
        """ATM IV line (one point per candle, its close) + trend, and the selected strikes' delta drift. Live only."""
        b = an.bars
        t0 = int(b.ts[max(0, len(b) - 300)])
        atm = iv_mod.series("ATM", t0)
        pb = iv_mod.per_bar(atm, tf)
        pts = [{"time": chart_time(int(t)), "iv": pb[int(t)]["iv"]} for t in b.ts[-300:] if int(t) in pb]
        tr = iv_mod.trend(atm, int(config.rule("iv", "trend_minutes", 60)))
        strikes = []
        for side, rec in self.book.current().items():
            ins = (rec or {}).get("instrument") or {}
            key = ins.get("key")
            if not key:
                continue
            rows = iv_mod.series(key, int(now_epoch()) - 86400)
            d = iv_mod.trend(rows, 375, "delta")
            last = rows[-1] if rows else None
            strikes.append({"side": side, "label": ins.get("label") or key, "delta": last.get("delta") if last else None,
                            "iv": last.get("iv") if last else None, "delta_trend": d})
        return {"iv": pts, "iv_trend": tr, "iv_last": atm[-1] if atm else None, "strike_greeks": strikes}

    def crt_view(self, tf: str) -> Dict[str, Any]:
        """Ranges + markers for a chart tf: every pair whose LTF is as fine or finer than the chart, bucketed."""
        out = {"ranges": [], "markers": [], "pairs": []}
        if tf not in TF_SECONDS:
            return out
        for pid, r in self.crt.items():
            if TF_SECONDS[r["ltf"]] > TF_SECONDS[tf] and r["htf"] != tf:
                continue
            out["pairs"].append({"pair": pid, "name": r["name"], "message": r.get("message", "")})
            rv = self.rvol_tf(r["htf"]) if self.mapped_1m() else {}
            out["ranges"] += [dict(x, pair_name=r["name"], rvol=(rv.get(int(x["k_ts"])) or {}).get("rvol"))
                              for x in r["ranges"][-4:]]
            if TF_SECONDS[r["ltf"]] <= TF_SECONDS[tf]:
                out["markers"] += crt_tbs.markers(r, tf, 0)
        out["markers"].sort(key=lambda m: m["time"])
        return out

    def crt_signals_today(self) -> List[Signal]:
        o, _ = session_bounds(now_ist().date())
        return [s for r in self.crt.values() for s in r.get("signals", []) if s.ts >= o]

    def evaluate_primary(self) -> List[Signal]:
        p = self.analyses.get(self.primary)
        if not p:
            return []
        sigs = evaluate(p, self.analyses.get(self.bias_tf))
        live_spot = float(self.candles.last_price.get(self.nifty) or (p.bars.c[-1] if p and len(p.bars) else 0))
        is_stale = self.chain.is_stale(self.sel_expiry(), live_spot)
        return confluence.apply(sigs, self.combined_flow(self.primary), proxy_res=self.proxy.get(self.primary),
                                context={"foci": self.foci(), "is_stale": is_stale} if any(self.book.current().values()) else None,
                                live_i=p.last_i)

    def recompute_all(self, after_repair: bool = False) -> None:
        for tf in self.tfs:
            try:
                self.recompute(tf, after_repair=after_repair)
            except Exception as e:                           # noqa: BLE001
                log.exception("analysis %s failed", tf)
                self.status["errors"] = (self.status["errors"] + [f"analysis {tf}: {e}"])[-5:]
        if self.analyses.get(self.primary):
            self.signals = self.evaluate_primary()
        for pair in self.crt_pairs():
            self.recompute_crt(pair)                          # startup / repair: no alerting from history

    async def run_playbook(self) -> None:
        p = self.analyses.get(self.primary)
        if not p:
            return
        self.signals = self.evaluate_primary()
        fresh = live(self.signals, p)
        if not fresh:
            return
        await self.alerts.handle(fresh)
        if market_open_now():
            for s in fresh:
                t = paper.maybe_open(s, strikes=self.book.current(), mode=self.mode)
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
                        self.recompute_all(after_repair=True)
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
                    self.recompute_all(after_repair=True)
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
        v = self.volume.get(self.primary)
        p = self.analyses.get(self.primary)
        if v and v.get("available") and p is not None:
            px = float(p.bars.c[-1])
            bits = []
            lv = v.get("last_vwap")
            if lv:
                sdv = (px - lv["v"]) / lv["sd"] if lv["sd"] else 0.0
                bits.append(f"{abs(px - lv['v']):,.0f} pts {'above' if px >= lv['v'] else 'below'} VWAP {lv['v']:,.1f} ({sdv:+.1f}σ)")
            lr = (v.get("last") or {}).get("last_rvol")
            if lr is not None:
                bits.append(f"last candle volume {lr:.1f}× normal")
            if v.get("naked"):
                bits.append("naked POC " + ", ".join(f"{n['price']:,.1f} ({n['day'][5:]})" for n in v["naked"][-2:]))
            os_ = v.get("oi_summary") or {}
            if os_.get("dominant") and os_["dominant"] != "flat":
                bits.append(f"futures: {os_['dominant']} over the last {os_['bars']} candles (net OI {os_['net_doi']:+,})")
            rs = v.get("rvol_state") or {}
            if rs.get("value") is not None:
                bits.insert(0, rs["text"])
            sl = v.get("oi_surge_last")
            if sl and p is not None and sl["ts"] >= int(p.bars.ts[-1]) - 3 * TF_SECONDS[self.primary]:
                bits.append(f"OI SURGE at {hm(sl['ts'])} ({sl['surge_x']}× normal ΔOI, OI above its 8-EMA): big move building")
            cp = v.get("oi_compare") or {}
            if cp.get("text"):
                bits.append(f"NIFTY vs BANKNIFTY futures: {cp['text']}")
            it = v.get("iv_trend") or {}
            if it.get("word"):
                bits.append(f"ATM IV {it['word']} {it['from']:.1f}→{it['to']:.1f} in {it['minutes']} min"
                            + (" (orderly move)" if it["word"] == "falling" else " (wider swings)" if it["word"] == "rising" else ""))
            for g in v.get("strike_greeks") or []:
                dt = g.get("delta_trend") or {}
                if dt.get("word") == "falling" and g["side"] == "CE" or dt.get("word") == "rising" and g["side"] == "PE":
                    bits.append(f"{g['label']} |Δ| {abs(dt['from']):.2f}→{abs(dt['to']):.2f}: drifting out of the money")
            al = v.get("act_last") or {}
            if al.get("z") is not None:
                bits.append(f"options activity z {al['z']:+.1f}")
            if bits:
                out.append(f"Volume ({self.primary}, NIFTY FUT): " + " · ".join(bits))
        a = self.auction.get(self.primary)
        if a and a.get("read"):
            r = a["read"]
            out.append(f"Auction ({self.primary}): {r['location']['text']} · {r['response']['text']} → {r['decision']['text']}")
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
                "session": p.sess if p else {}, "chain": self.chain.summary(live_spot=self.candles.last_price.get(self.nifty)),
                "expiries": self.chain.expiries, "signals": [s.to_dict() for s in self.today_signals()][-15:],
                "alerts": alerts_mod.recent(20), "paper_open": paper.open_trade(), "paper_open_all": paper.open_trades(), "paper_stats": paper.stats(),
                "delta": self.delta_status(), "strikes": self.strikes_view(), "mode": self.mode,
                "proxy": {"enabled": self.proxy_on, "label": proxy_mod.LABEL},
                "status": self.status_view()}

    def delta_status(self) -> Dict[str, Any]:
        st = self.delta_adapter.status()
        d = self.delta.get(self.primary) or {}
        return {**st, "adapters": delta_adapters.names(), "labels": delta_adapters.LABELS, "tf": self.primary,
                "available_on_tf": bool(d.get("available")), "has_levels": bool(d.get("has_levels")),
                "tf_message": d.get("message", "")}

    def today_signals(self) -> List[Signal]:
        o, _ = session_bounds(now_ist().date())
        return sorted([s for s in self.signals if s.ts >= o] + self.crt_signals_today(), key=lambda s: s.ts)

    def status_view(self) -> Dict[str, Any]:
        return {**self.status, "dhan_calls": self.rest.usage(), "futures": self.fut_status, "banknifty": self.bn_status, "recorder": self.recorder.status(), "feed": self.feed.status, "feed_error": self.feed.last_error,
                "feed_connects": self.feed.connects, "ltt_mode": self.feed.ltt_mode,
                "tick_age_s": round(self.feed.tick_age(self.nifty), 1) if self.feed.tick_age(self.nifty) < 1e8 else None,
                "chain": self.chain.status, "chain_error": self.chain.last_error,
                "rest_calls": self.rest.calls, "rest_error": self.rest.last_error,
                "broker": self.broker.status(),
                "market_open": market_open_now(), "now_ist": now_ist().strftime("%a %d %b %H:%M:%S")}

    # ── websocket clients ────────────────────────────────────────────────
    async def broadcast(self, msg: Dict[str, Any]) -> None:
        self._ws_seq += 1
        msg["seq"] = self._ws_seq
        self._replay_buffer.append(dict(msg))
        if not self.clients:
            return
        json_data: Optional[str] = None
        bin_data: Optional[bytes] = None
        dead = []
        for ws in list(self.clients):
            fmt = self.client_formats.get(ws, "json")
            try:
                if fmt == "msgpack" and msgpack is not None:
                    if bin_data is None:
                        bin_data = msgpack.packb(msg, default=_jdefault, use_bin_type=True)
                    await ws.send_bytes(bin_data)
                else:
                    if json_data is None:
                        json_data = json.dumps(msg, default=_jdefault)
                    await ws.send_text(json_data)
            except Exception:                                # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
            self.client_formats.pop(ws, None)

    def get_replay(self, from_seq: int) -> List[Dict[str, Any]]:
        """Returns buffered broadcast messages with seq >= from_seq for sequence gap recovery."""
        return [dict(m) for m in self._replay_buffer if m.get("seq", 0) >= from_seq]


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
