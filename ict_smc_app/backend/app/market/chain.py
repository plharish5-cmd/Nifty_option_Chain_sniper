"""
market/chain.py — NIFTY option chain for the two nearest expiries.

  * Expiry dates ALWAYS come from POST /optionchain/expirylist (mistake #5). NIFTY weeklies moved to Tuesday in
    2025 and holidays shift them — a hard-coded "next Thursday" silently pulls an empty or wrong chain.
  * Dhan allows one unique chain request every 3 s, so the two expiries are fetched alternately: each expiry
    refreshes about every 6 s. That is plenty — Dhan's OI itself only updates every few seconds to minutes.
  * OI change here = oi − previous_oi (change since yesterday's close, the number NSE shows).
  * Builds/unwinds use price change AND OI change together:
        price ↑ OI ↑ long build      price ↓ OI ↑ short build (writing)
        price ↓ OI ↓ long unwind     price ↑ OI ↓ short covering
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from .. import config, db
from ..timeutil import market_open_now, now_epoch

log = logging.getLogger("chain")


def _side(x: Dict[str, Any]) -> Dict[str, Any]:
    g = x.get("greeks") or {}
    ltp = float(x.get("last_price") or 0)
    prev = float(x.get("previous_close_price") or 0)
    oi = float(x.get("oi") or 0)
    poi = float(x.get("previous_oi") or 0)
    return {
        "ltp": ltp, "chg": round(ltp - prev, 2) if prev else 0.0, "oi": oi, "oi_chg": oi - poi,
        "oi_chg_pct": round(100 * (oi - poi) / poi, 1) if poi else None,
        "iv": round(float(x.get("implied_volatility") or 0), 2), "vol": float(x.get("volume") or 0),
        "delta": round(float(g.get("delta") or 0), 3), "gamma": round(float(g.get("gamma") or 0), 5),
        "theta": round(float(g.get("theta") or 0), 2), "vega": round(float(g.get("vega") or 0), 2),
        "bid": float(x.get("top_bid_price") or 0), "ask": float(x.get("top_ask_price") or 0),
        "sid": x.get("security_id"),
    }


def build_class(chg: float, oi_chg: float) -> str:
    if oi_chg > 0:
        return "Long build" if chg > 0 else "Short build"
    if oi_chg < 0:
        return "Short covering" if chg > 0 else "Long unwind"
    return "—"


def normalize(raw: Dict[str, Any], expiry: str) -> Dict[str, Any]:
    spot = float(raw.get("last_price") or 0)
    rows: List[Dict[str, Any]] = []
    for k, v in (raw.get("oc") or {}).items():
        try:
            strike = float(k)
        except ValueError:
            continue
        ce, pe = _side((v or {}).get("ce") or {}), _side((v or {}).get("pe") or {})
        if ce["oi"] == 0 and pe["oi"] == 0 and ce["ltp"] == 0 and pe["ltp"] == 0:
            continue
        rows.append({"strike": strike, "ce": ce, "pe": pe})
    rows.sort(key=lambda r: r["strike"])
    strikes = [r["strike"] for r in rows]
    step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=50.0)
    atm = min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0
    ce_oi = sum(r["ce"]["oi"] for r in rows)
    pe_oi = sum(r["pe"]["oi"] for r in rows)
    ce_chg = sum(r["ce"]["oi_chg"] for r in rows)
    pe_chg = sum(r["pe"]["oi_chg"] for r in rows)

    # max pain: strike where option buyers' total intrinsic value is smallest
    def pain(s: float) -> float:
        return sum(r["ce"]["oi"] * max(s - r["strike"], 0) + r["pe"]["oi"] * max(r["strike"] - s, 0) for r in rows)
    mp = min(strikes, key=pain) if strikes else 0.0

    builds = []
    for r in rows:
        if abs(r["strike"] - atm) > 10 * step:                     # far OTM noise excluded
            continue
        for side in ("ce", "pe"):
            x = r[side]
            if x["oi_chg"]:
                builds.append({"strike": r["strike"], "side": side.upper(), "oi_chg": x["oi_chg"], "chg": x["chg"],
                               "ltp": x["ltp"], "kind": build_class(x["chg"], x["oi_chg"])})
    builds.sort(key=lambda b: -abs(b["oi_chg"]))
    near = [r for r in rows if abs(r["strike"] - atm) <= 10 * step]
    ce_wall = max(near, key=lambda r: r["ce"]["oi"], default=None)
    pe_wall = max(near, key=lambda r: r["pe"]["oi"], default=None)
    return {
        "expiry": expiry, "ts": int(now_epoch()), "spot": spot, "atm": atm, "step": step, "rows": rows,
        "totals": {"ce_oi": ce_oi, "pe_oi": pe_oi, "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
                   "ce_oi_chg": ce_chg, "pe_oi_chg": pe_chg,
                   "pcr_chg": round(pe_chg / ce_chg, 2) if ce_chg > 0 else None},
        "max_pain": mp, "call_wall": ce_wall["strike"] if ce_wall else None,
        "put_wall": pe_wall["strike"] if pe_wall else None,
        "builds": builds[:10],
    }


class ChainPoller:
    def __init__(self, rest, on_update) -> None:
        self.rest, self.on_update = rest, on_update
        self.expiries: List[str] = []
        self.chains: Dict[str, Dict[str, Any]] = db.load_chains()      # survives restart
        self.status = "idle"
        self.last_error = ""
        self._last_snap: Dict[str, float] = {}
        self._exp_at = 0.0

    async def refresh_expiries(self) -> None:
        exps = await self.rest.expiry_list()
        today = time.strftime("%Y-%m-%d", time.gmtime(now_epoch() + 19800))
        self.expiries = [e for e in sorted(exps) if e >= today][: int(config.rule("data", "chain_expiries", 2))]
        self._exp_at = time.time()
        # drop chains of expiries that are gone
        for e in list(self.chains):
            if e not in self.expiries:
                self.chains.pop(e, None)

    async def run(self) -> None:
        i = 0
        while True:
            try:
                if not self.expiries or time.time() - self._exp_at > 3600:
                    await self.refresh_expiries()
                if not self.expiries:
                    self.status = "no expiries returned"
                    await asyncio.sleep(30)
                    continue
                # nearest expiry on most polls, the next one every 4th (OI / IV don't move every 3 s, and the
                # 1-per-3-s chain limit is shared by every app on your Dhan account)
                every = max(1, int(config.rule("data", "chain_far_every", 4)))
                exp = self.expiries[1] if len(self.expiries) > 1 and i % every == every - 1 else self.expiries[0]
                i += 1
                t_poll = time.time()
                raw = await self.rest.option_chain(exp)
                ch = normalize(raw, exp)
                if ch["rows"]:
                    self.chains[exp] = ch
                    db.save_chain(exp, ch["ts"], ch)
                    self._snapshot(exp, ch)
                    await self.on_update(exp, ch)
                self.status = "live" if market_open_now() else "after hours (last snapshot)"
                gap = float(config.rule("data", "chain_poll_every_s", 6))
                if market_open_now() and time.time() - t_poll < gap:
                    await asyncio.sleep(gap - (time.time() - t_poll))
                if not market_open_now():
                    await asyncio.sleep(60)                 # nothing changes after hours; be polite
            except asyncio.CancelledError:
                raise
            except Exception as e:                           # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                self.status = "error"
                log.warning("chain poll failed: %s", self.last_error)
                await asyncio.sleep(10 if "Token" not in type(e).__name__ else 30)

    def _snapshot(self, exp: str, ch: Dict[str, Any]) -> None:
        every = float(config.rule("data", "chain_snapshot_every_s", 60))
        if time.time() - self._last_snap.get(exp, 0) < every or not market_open_now():
            return
        self._last_snap[exp] = time.time()
        ts = int(ch["ts"] // 60 * 60)
        db.save_chain_snap(exp, ts, [(r["strike"], r["ce"]["oi"], r["pe"]["oi"], r["ce"]["ltp"], r["pe"]["ltp"])
                                     for r in ch["rows"]])

    def summary(self, exp: Optional[str] = None) -> Optional[Dict[str, Any]]:
        exp = exp or (self.expiries[0] if self.expiries else (sorted(self.chains)[0] if self.chains else None))
        ch = self.chains.get(exp) if exp else None
        if not ch:
            return None
        return {k: ch[k] for k in ("expiry", "ts", "spot", "atm", "totals", "max_pain", "call_wall", "put_wall", "builds")}
