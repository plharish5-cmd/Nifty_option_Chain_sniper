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
from ..delta import adapters as delta_adapters
from ..delta import proxy as proxy_mod
from ..engine import crt_tbs
from ..market import instruments as instr
from ..market import options as optc
from ..strikes import context as sctx
from ..strikes.selector import StrikeBook, select_strikes
from ..delta import confluence
from ..delta import engine as delta_engine
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
        self.feed = Feed([self.nifty, self.vix], self.on_tick, self.on_prev)
        self.chain = ChainPoller(self.rest, self.on_chain)
        self.alerts = alerts_mod.Alerts(self.broadcast)
        self.clients: Set[WebSocket] = set()
        self.analyses: Dict[str, Analysis] = {}
        self.json: Dict[str, Dict[str, Any]] = {}
        self.signals: List[Signal] = []
        self.zones: Dict[str, List[registry.Zone]] = {}
        self.delta: Dict[str, Dict[str, Any]] = {}
        self.delta_adapter = self._make_adapter(db.kv_get("delta_adapter") or config.rule("delta", "adapter", "none"))
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

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        db.conn()
        await self.repair(full=True)
        self.recompute_all(after_repair=True)
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
        for pair in self.crt_pairs():
            ltf = pair["ltf"]
            if end == bucket_start(c["ts"], ltf) + TF_SECONDS[ltf] or end >= sess_close:
                fresh = self.recompute_crt(pair)
                await self.act_on_crt(fresh)
        await self.broadcast({"type": "candle", "tf": "1m", "candle": _cj(c)})
        if self.option_instruments():
            asyncio.create_task(self._options_after_close())

    async def on_chain(self, expiry: str, ch: Dict[str, Any]) -> None:
        await self.broadcast({"type": "chain", "expiry": expiry, "summary": self.chain.summary(expiry)})
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
        t = paper.open_trade()
        if t and t.get("opt_sid") and t.get("opt_key"):
            try:
                pi = instr.parse_key(t["opt_key"])
                pi = instr.option(pi.expiry, pi.strike, pi.right, t["opt_sid"])
                if pi.key not in {i.key for i in out}:
                    out.append(pi)
            except ValueError:
                pass
        return out

    async def refresh_options(self) -> None:
        if self._opt_busy:
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
        first_ts = int(an.bars.ts[max(0, len(an.bars) - 150)])
        j["markers"] = sorted(j["markers"] + delta_engine.markers(dres["events"], first_ts), key=lambda m: m["time"])
        self._mk[tf] = list(j["markers"])
        j["delta"] = {k: dres.get(k) for k in ("adapter", "available", "mock", "message", "has_levels")}
        j["registry"] = {"zones": len(zs), "active": sum(1 for z in zs if z.active_for(int(an.bars.ts[-1]) + 1))}
        j["crt"] = self.crt_view(tf)
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

    def crt_view(self, tf: str) -> Dict[str, Any]:
        """Ranges + markers for a chart tf: every pair whose LTF is as fine or finer than the chart, bucketed."""
        out = {"ranges": [], "markers": [], "pairs": []}
        if tf not in TF_SECONDS:
            return out
        for pid, r in self.crt.items():
            if TF_SECONDS[r["ltf"]] > TF_SECONDS[tf] and r["htf"] != tf:
                continue
            out["pairs"].append({"pair": pid, "name": r["name"], "message": r.get("message", "")})
            out["ranges"] += [dict(x, pair_name=r["name"]) for x in r["ranges"][-4:]]
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
        return confluence.apply(sigs, self.combined_flow(self.primary), proxy_res=self.proxy.get(self.primary),
                                context={"foci": self.foci()} if any(self.book.current().values()) else None,
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
