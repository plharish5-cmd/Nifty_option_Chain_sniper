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
from ..timeutil import market_open_now, now_epoch, now_ist, session_bounds
from ..engine import pcr_trinity

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
    ce_vol = sum(r["ce"]["vol"] for r in rows)
    pe_vol = sum(r["pe"]["vol"] for r in rows)
    total_vol = ce_vol + pe_vol

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

    strike_map: List[Dict[str, Any]] = []
    for r in rows:
        ce_x, pe_x = r["ce"], r["pe"]
        ce_oi_val = int(round(ce_x.get("oi") or 0))
        ce_chg_val = int(round(ce_x.get("oi_chg") or 0))
        pe_oi_val = int(round(pe_x.get("oi") or 0))
        pe_chg_val = int(round(pe_x.get("oi_chg") or 0))
        ce_chg_f = float(ce_x.get("chg") or 0)
        pe_chg_f = float(pe_x.get("chg") or 0)
        ce_b = "Long Buildup" if ce_chg_val > 0 and ce_chg_f >= 0 else ("Short Buildup" if ce_chg_val > 0 else ("Short Covering" if ce_chg_val < 0 and ce_chg_f >= 0 else ("Long Unwinding" if ce_chg_val < 0 else "Neutral")))
        pe_b = "Long Buildup" if pe_chg_val > 0 and pe_chg_f >= 0 else ("Short Buildup" if pe_chg_val > 0 else ("Short Covering" if pe_chg_val < 0 and pe_chg_f >= 0 else ("Long Unwinding" if pe_chg_val < 0 else "Neutral")))
        dominant_b = pe_b if abs(pe_chg_val) > abs(ce_chg_val) and pe_b != "Neutral" else (ce_b if ce_b != "Neutral" else "Neutral")

        r["call_oi"] = ce_oi_val
        r["call_oi_chg"] = ce_chg_val
        r["put_oi"] = pe_oi_val
        r["put_oi_chg"] = pe_chg_val
        r["call_buildup"] = ce_b
        r["put_buildup"] = pe_b
        r["buildup"] = dominant_b
        r["max_pain"] = mp
        r["spot"] = spot

        strike_map.append({
            "strike": r["strike"],
            "call_oi": ce_oi_val,
            "call_oi_chg": ce_chg_val,
            "put_oi": pe_oi_val,
            "put_oi_chg": pe_chg_val,
            "buildup": dominant_b,
            "call_buildup": ce_b,
            "put_buildup": pe_b,
            "max_pain": mp,
            "spot": spot,
        })

    now_t = time.time()
    return {
        "expiry": expiry, "ts": int(now_epoch()), "fetched_at": now_t, "spot_at_fetch": spot,
        "is_stale": False, "spot": spot, "atm": atm, "step": step, "rows": rows,
        "strike_map": strike_map,
        "totals": {"ce_oi": ce_oi, "pe_oi": pe_oi, "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
                   "ce_oi_chg": ce_chg, "pe_oi_chg": pe_chg,
                   "pcr_chg": round(pe_chg / ce_chg, 2) if ce_chg > 0 else None,
                   "ce_vol": round(ce_vol), "pe_vol": round(pe_vol),
                   "total_vol": round(total_vol),
                   "pcr_vol": round(pe_vol / ce_vol, 2) if ce_vol else None},
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
        self._pcr_cache: Dict[str, Any] = {}
        self._last_pcr_time = 0.0

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
        self._pcr_cache.clear()
        ts = int(ch["ts"] // 60 * 60)
        db.save_chain_snap(exp, ts, [(r["strike"], r["ce"]["oi"], r["pe"]["oi"], r["ce"]["ltp"], r["pe"]["ltp"],
                                      r["ce"].get("vol"), r["pe"].get("vol"),
                                      r["ce"].get("bid"), r["ce"].get("ask"), r["pe"].get("bid"), r["pe"].get("ask"))
                                     for r in ch["rows"]])

    def is_stale(self, exp: Optional[str] = None, live_spot: Optional[float] = None) -> bool:
        """Evaluates whether the option chain snapshot is stale based on age or spot drift from rules.json."""
        exp = exp or (self.expiries[0] if self.expiries else (sorted(self.chains)[0] if self.chains else None))
        ch = self.chains.get(exp) if exp else None
        if not ch:
            return True
        max_age = float(config.rule("data", "oc_max_age_s", 20))
        max_spot_delta = float(config.rule("data", "oc_stale_spot_delta", 40.0))
        fetched_at = float(ch.get("fetched_at", ch.get("ts", 0)))
        age = time.time() - fetched_at
        if age > max_age:
            return True
        if live_spot is not None and live_spot > 0:
            spot_at_fetch = float(ch.get("spot_at_fetch", ch.get("spot", live_spot)))
            if spot_at_fetch > 0 and abs(live_spot - spot_at_fetch) > max_spot_delta:
                return True
        return False

    def summary(self, exp: Optional[str] = None, live_spot: Optional[float] = None) -> Optional[Dict[str, Any]]:
        exp = exp or (self.expiries[0] if self.expiries else (sorted(self.chains)[0] if self.chains else None))
        ch = self.chains.get(exp) if exp else None
        if not ch:
            return None
        stale = self.is_stale(exp, live_spot)
        return {
            **{k: ch[k] for k in ("expiry", "ts", "spot", "atm", "totals", "max_pain", "call_wall", "put_wall", "builds")},
            "strike_map": ch.get("strike_map", []),
            "fetched_at": ch.get("fetched_at", ch.get("ts")),
            "spot_at_fetch": ch.get("spot_at_fetch", ch.get("spot")),
            "is_stale": stale,
        }

    def multi_expiry_volume(self) -> Dict[str, Any]:
        """Returns Call, Put, and Total traded volume for current week, next week, and both combined."""
        today_iso = now_ist().date().isoformat()
        active_exps = [e for e in (self.expiries or sorted(self.chains)) if e >= today_iso]
        cur_exp = active_exps[0] if len(active_exps) > 0 else (self.expiries[0] if self.expiries else None)
        next_exp = active_exps[1] if len(active_exps) > 1 else (self.expiries[1] if len(self.expiries) > 1 else None)

        cur_ch = self.chains.get(cur_exp) if cur_exp else None
        next_ch = self.chains.get(next_exp) if next_exp else None

        def get_vols(ch):
            if not ch:
                return 0.0, 0.0, 0.0
            t = ch.get("totals", {})
            if t.get("total_vol"):
                return float(t.get("total_vol") or 0), float(t.get("ce_vol") or 0), float(t.get("pe_vol") or 0)
            rows = ch.get("rows", [])
            ce_v = sum(float((r.get("ce") or {}).get("vol") or 0) for r in rows)
            pe_v = sum(float((r.get("pe") or {}).get("vol") or 0) for r in rows)
            return ce_v + pe_v, ce_v, pe_v

        cur_vol, cur_ce, cur_pe = get_vols(cur_ch)
        next_vol, next_ce, next_pe = get_vols(next_ch)

        both_vol = cur_vol + next_vol
        both_ce = cur_ce + next_ce
        both_pe = cur_pe + next_pe

        return {
            "cur_expiry": cur_exp,
            "next_expiry": next_exp,
            "cur_vol": round(cur_vol),
            "cur_ce_vol": round(cur_ce),
            "cur_pe_vol": round(cur_pe),
            "cur_pcr_vol": round(cur_pe / cur_ce, 2) if cur_ce else None,
            "next_vol": round(next_vol),
            "next_ce_vol": round(next_ce),
            "next_pe_vol": round(next_pe),
            "next_pcr_vol": round(next_pe / next_ce, 2) if next_ce else None,
            "both_vol": round(both_vol),
            "both_ce_vol": round(both_ce),
            "both_pe_vol": round(both_pe),
            "both_pcr_vol": round(both_pe / both_ce, 2) if both_ce else None,
            "next_share_pct": round(100 * next_vol / both_vol, 1) if both_vol else 0.0,
        }

    def get_pcr_trinity(
        self,
        exp: Optional[str] = None,
        fut_candles: Optional[List[Dict[str, Any]]] = None,
        fut_src: str = "combined",
        hub: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Computes the complete 3-Chart Institutional PCR & Open Interest Positioning Suite data.
        Returns:
          - alignment_state, verdict_badge, verdict_summary
          - pcr_oi, pcr_vol, pcr_band, pcr_band_label
          - pcr_slope_30m, pcr_delta_open, chg_oi_slope, net_oi_slope_30m
          - total_oi_gap, total_oi_gap_delta, net_oi_chg, call_oi_chg, put_oi_chg
          - checkpoints: [09:45, 12:30, 14:30, Current]
          - series: { cur: [...], next: [...], both: [...] }
          - verdicts: { cur: {...}, next: {...}, both: {...} }
          - pcr_oi_series: the series for the selected expiry
          - expiries_meta: metadata for current/next expiry dates, near/next futures & rollover %
        """
        now_t = time.time()
        fut_mode = "near" if fut_src in ("near", "oct") else ("next" if fut_src in ("next", "nov") else "combined")

        today_iso = now_ist().date().isoformat()
        active_exps = [e for e in (self.expiries or sorted(self.chains)) if e >= today_iso]
        cur_exp = active_exps[0] if len(active_exps) > 0 else (self.expiries[0] if self.expiries else None)
        next_exp = active_exps[1] if len(active_exps) > 1 else (self.expiries[1] if len(self.expiries) > 1 else None)
        sel_exp = exp or cur_exp
        sel_key = "both" if sel_exp == "both" else ("next" if (sel_exp == next_exp or sel_exp == "next") else "cur")

        # Fast in-memory cache if queried within 3s without new snapshot and same fut_mode
        if (
            self._pcr_cache
            and (now_t - self._last_pcr_time < 3.0)
            and self._pcr_cache.get("fut_src") == fut_mode
        ):
            out = dict(self._pcr_cache)
            out["pcr_oi_series"] = out.get("series", {}).get(sel_key, [])
            out["selected_expiry"] = sel_exp
            verdict = out.get("verdicts", {}).get(sel_key, {})
            out.update({k: v for k, v in verdict.items() if k not in ("series", "verdicts")})
            return out

        today_start, _ = session_bounds(now_ist().date())

        # Load snapshots from SQLite
        rows = db.query("""
            SELECT expiry, ts, SUM(ce_oi), SUM(pe_oi), SUM(ce_vol), SUM(pe_vol)
            FROM chain_snap
            WHERE ts >= ?
            GROUP BY expiry, ts
            ORDER BY ts
        """, (today_start,))

        # Load today's futures candles for alignment and resolve contract mode
        oct_candles = db.candles_1m("FUT", today_start)
        if not oct_candles:
            oct_candles = db.candles_1m(str(config.rules().get("instruments", {}).get("nifty_security_id", "13")), today_start)
        nov_candles = db.candles_1m("FUT2", today_start)

        # Attach OI to candles
        oct_oi_rows = {int(r["ts"]): float(r["oi"]) for r in db.query("SELECT ts, oi FROM fut_oi WHERE ts >= ? ORDER BY ts", (today_start,))}
        oct_open_oi = next(iter(oct_oi_rows.values()), 0.0) if oct_oi_rows else 0.0
        for c in oct_candles:
            t = int(c["ts"])
            c_oi = oct_oi_rows.get(t, oct_open_oi)
            c["oi"] = int(c_oi)
            c["oi_chg"] = int(c_oi - oct_open_oi)

        nov_oi_rows = {int(r["ts"]): float(r["oi"]) for r in db.query("SELECT ts, oi FROM fut_oi_sym WHERE sym='FUT2' AND ts >= ? ORDER BY ts", (today_start,))}
        nov_open_oi = next(iter(nov_oi_rows.values()), 0.0) if nov_oi_rows else 0.0
        for c in nov_candles:
            t = int(c["ts"])
            c_oi = nov_oi_rows.get(t, nov_open_oi)
            c["oi"] = int(c_oi)
            c["oi_chg"] = int(c_oi - nov_open_oi)

        if fut_candles is None:
            fut_map = pcr_trinity.resolve_futures_series(oct_candles, nov_candles, fut_mode=fut_mode)
            fut_candles = list(fut_map.values())

        # Compute rollover percentage & labels
        tot_oct_v = sum(float(c.get("v") or 0) for c in oct_candles)
        tot_nov_v = sum(float(c.get("v") or 0) for c in nov_candles)
        tot_fut_v = tot_oct_v + tot_nov_v
        rollover_pct = round((tot_nov_v / max(tot_fut_v, 1.0)) * 100, 1)

        near_label = (hub.fut.get("symbol") if hub and getattr(hub, "fut", None) else None) or "NIFTY-Oct2026-FUT"
        next_label = (hub.fut2.get("symbol") if hub and getattr(hub, "fut2", None) else None) or "NIFTY-Nov2026-FUT"

        def _fmt_exp(s: Optional[str]) -> str:
            if not s:
                return "—"
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(s.replace("Z", ""))
                return dt.strftime("%d %b")
            except Exception:
                return str(s)

        expiries_meta = {
            "cur_date": _fmt_exp(cur_exp),
            "next_date": _fmt_exp(next_exp),
            "cur_expiry": cur_exp,
            "next_expiry": next_exp,
            "near_fut": near_label,
            "next_fut": next_label,
            "rollover_pct": rollover_pct,
            "fut_src": fut_mode,
        }

        snaps_by_exp: Dict[str, List[Tuple]] = {}
        for r in rows:
            snaps_by_exp.setdefault(r["expiry"], []).append((r["ts"], r[2], r[3], r[4], r[5]))

        cur_totals = (self.chains.get(cur_exp) or {}).get("totals") if cur_exp else None
        next_totals = (self.chains.get(next_exp) or {}).get("totals") if next_exp else None

        cur_series = pcr_trinity.build_intraday_pcr_series(snaps_by_exp.get(cur_exp, []), fut_candles, cur_totals)
        next_series = pcr_trinity.build_intraday_pcr_series(snaps_by_exp.get(next_exp, []), fut_candles, next_totals)
        both_series = pcr_trinity.combine_multi_expiry_series(cur_series, next_series)

        cfg = config.rules().get("pcr_trinity", {})
        cur_verdict = pcr_trinity.compute_pcr_trinity_verdict(cur_series, cfg)
        next_verdict = pcr_trinity.compute_pcr_trinity_verdict(next_series, cfg)
        both_verdict = pcr_trinity.compute_pcr_trinity_verdict(both_series, cfg)

        main_verdict = both_verdict if sel_key == "both" else (next_verdict if sel_key == "next" else cur_verdict)

        res = {
            **main_verdict,
            "selected_expiry": sel_exp,
            "cur_expiry": cur_exp,
            "next_expiry": next_exp,
            "fut_src": fut_mode,
            "expiries_meta": expiries_meta,
            "series": {
                "cur": cur_series,
                "next": next_series,
                "both": both_series,
            },
            "verdicts": {
                "cur": cur_verdict,
                "next": next_verdict,
                "both": both_verdict,
            },
            "pcr_oi_series": cur_series if sel_key == "cur" else (next_series if sel_key == "next" else both_series),
        }

        self._pcr_cache = res
        self._last_pcr_time = now_t
        return res

    def get_pe_ce_matrix(
        self,
        exp: Optional[str] = None,
        tf: str = "3m",
        scope: str = "atm_10",
    ) -> Dict[str, Any]:
        """Returns the Institutional PE-CE Difference & Skew Matrix 2.0."""
        from ..engine import pe_ce_matrix
        return pe_ce_matrix.compute_pe_ce_matrix(expiry=exp, tf=tf, scope=scope)

    def get_oi_matrix(
        self,
        exp: Optional[str] = None,
        scope: str = "atm_10",
    ) -> Dict[str, Any]:
        """Returns the Institutional Open Interest Matrix & Strike Map 2.0."""
        from ..engine import oi_matrix

        today_iso = now_ist().date().isoformat()
        active_exps = [e for e in (self.expiries or sorted(self.chains)) if e >= today_iso]
        cur_exp = active_exps[0] if len(active_exps) > 0 else (self.expiries[0] if self.expiries else None)
        next_exp = active_exps[1] if len(active_exps) > 1 else (self.expiries[1] if len(self.expiries) > 1 else None)

        expiry_mode = exp or "cur"
        if expiry_mode == "both":
            cur_ch = self.chains.get(cur_exp) if cur_exp else None
            next_ch = self.chains.get(next_exp) if next_exp else None
        elif expiry_mode == "next":
            cur_ch = self.chains.get(next_exp) if next_exp else None
            next_ch = None
        else:
            cur_ch = self.chains.get(cur_exp) if cur_exp else None
            next_ch = None

        return oi_matrix.build_oi_matrix(
            chain_dict=cur_ch,
            next_chain_dict=self.chains.get(next_exp) if expiry_mode == "both" else None,
            expiry_mode=expiry_mode,
            scope=scope,
        )

    def get_smart_oi(
        self,
        exp: Optional[str] = None,
        fut_src: str = "combined",
        tf: str = "3m",
        range_mode: str = "atm",
        span: int = 10,
        candles: Optional[List[Dict[str, Any]]] = None,
        hub: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Returns the Smart OI Noise-Filtered & Price Action Confluence Suite payload."""
        from ..engine import smart_oi

        today_iso = now_ist().date().isoformat()
        active_exps = [e for e in (self.expiries or sorted(self.chains)) if e >= today_iso]
        cur_exp = active_exps[0] if len(active_exps) > 0 else (self.expiries[0] if self.expiries else None)
        next_exp = active_exps[1] if len(active_exps) > 1 else (self.expiries[1] if len(self.expiries) > 1 else None)

        expiry_mode = exp or "cur"
        if expiry_mode == "both":
            cur_ch = self.chains.get(cur_exp) if cur_exp else None
            next_ch = self.chains.get(next_exp) if next_exp else None
        elif expiry_mode == "next":
            cur_ch = self.chains.get(next_exp) if next_exp else None
            next_ch = None
        else:
            cur_ch = self.chains.get(cur_exp) if cur_exp else None
            next_ch = None

        trinity = self.get_pcr_trinity(exp=expiry_mode, fut_src=fut_src, hub=hub) if hasattr(self, "get_pcr_trinity") else {}
        pcr_series = trinity.get("pcr_oi_series") if isinstance(trinity, dict) else []

        payload = smart_oi.build_smart_oi_payload(
            chain_dict=cur_ch,
            next_chain_dict=next_ch,
            candles=candles,
            pcr_oi_series=pcr_series,
            expiry_mode=expiry_mode,
            tf=tf,
            range_mode=range_mode,
            span=span,
        )
        if isinstance(payload, dict):
            payload["expiries_meta"] = trinity.get("expiries_meta") if isinstance(trinity, dict) else None
        return payload

