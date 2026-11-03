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
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .. import config, db
from ..timeutil import market_open_now, now_epoch, now_ist, session_bounds, to_ist
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

        # Permanent Startup Protection: Anchor 09:15 baseline if missing (late boot at 09:30–10:00 AM)
        today_start, _ = session_bounds(now_ist().date())
        if ts > today_start:
            has_anchor = db.query("SELECT 1 FROM chain_snap WHERE expiry = ? AND ts = ? LIMIT 1", (exp, today_start))
            if not has_anchor:
                prev_eod_rows = []
                for r in ch.get("rows", []):
                    c_oi = float(r["ce"].get("oi") or 0)
                    c_chg = float(r["ce"].get("oi_chg") or 0)
                    p_oi = float(r["pe"].get("oi") or 0)
                    p_chg = float(r["pe"].get("oi_chg") or 0)
                    prev_c_oi = max(0.0, c_oi - c_chg)
                    prev_p_oi = max(0.0, p_oi - p_chg)
                    prev_eod_rows.append((
                        r["strike"], prev_c_oi, prev_p_oi,
                        r["ce"].get("ltp"), r["pe"].get("ltp"),
                        0.0, 0.0,
                        r["ce"].get("bid"), r["ce"].get("ask"),
                        r["pe"].get("bid"), r["pe"].get("ask")
                    ))
                if prev_eod_rows:
                    db.save_chain_snap(exp, today_start, prev_eod_rows)
                    log.info("Anchored 09:15:00 settled EOD baseline in chain_snap for %s", exp)

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
        scope: str = "all",
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
        if not active_exps or (len(active_exps) < 2 and self.expiries and self.expiries[0] < today_iso):
            active_exps = list(self.expiries or sorted(self.chains))
        cur_exp = active_exps[0] if len(active_exps) > 0 else (self.expiries[0] if self.expiries else None)
        next_exp = active_exps[1] if len(active_exps) > 1 else (self.expiries[1] if len(self.expiries) > 1 else None)
        sel_exp = exp or cur_exp
        sel_key = "both" if sel_exp == "both" else ("next" if (sel_exp == next_exp or sel_exp == "next") else "cur")

        # Fast in-memory cache if queried within 3s without new snapshot and same fut_mode and scope
        if (
            self._pcr_cache
            and (now_t - self._last_pcr_time < 3.0)
            and self._pcr_cache.get("fut_src") == fut_mode
            and self._pcr_cache.get("scope") == scope
        ):
            out = dict(self._pcr_cache)
            out["pcr_oi_series"] = out.get("series", {}).get(sel_key, [])
            out["selected_expiry"] = sel_exp
            verdict = out.get("verdicts", {}).get(sel_key, {})
            out.update({k: v for k, v in verdict.items() if k not in ("series", "verdicts")})
            return out

        today_start, today_end = session_bounds(now_ist().date())

        # Load snapshots from SQLite (strike level)
        snap_rows = db.query("""
            SELECT expiry, ts, strike, ce_oi, pe_oi, ce_vol, pe_vol
            FROM chain_snap
            WHERE ts >= ? AND ts <= ?
            ORDER BY ts ASC, strike ASC
        """, (today_start, today_end))

        if not snap_rows:
            latest = db.query("SELECT MAX(ts) as mx FROM chain_snap")
            if latest and latest[0]["mx"]:
                latest_dt = to_ist(latest[0]["mx"]).date()
                today_start, today_end = session_bounds(latest_dt)
                snap_rows = db.query("""
                    SELECT expiry, ts, strike, ce_oi, pe_oi, ce_vol, pe_vol
                    FROM chain_snap
                    WHERE ts >= ? AND ts <= ?
                    ORDER BY ts ASC, strike ASC
                """, (today_start, today_end))

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

        # Determine ATM strike and scope bounds
        atm_strike = 22700.0
        if fut_candles:
            atm_strike = round(float(fut_candles[-1].get("close") or fut_candles[-1].get("c", 22700.0)) / 50.0) * 50.0
        elif hub and getattr(hub, "candles", None) and hasattr(hub.candles, "last_price"):
            lp = hub.candles.last_price.get(getattr(hub, "nifty", "13"), 22700.0)
            if lp:
                atm_strike = round(float(lp) / 50.0) * 50.0

        if scope == "atm_10":
            min_strike = atm_strike - 500.0
            max_strike = atm_strike + 500.0
        elif scope == "atm_5":
            min_strike = atm_strike - 250.0
            max_strike = atm_strike + 250.0
        else:
            min_strike = 0.0
            max_strike = 1000000.0

        snaps_by_exp: Dict[str, List[Tuple]] = {}
        grouped_snaps: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        for r in snap_rows:
            grouped_snaps.setdefault(r["expiry"], {}).setdefault(int(r["ts"]), []).append(r)

        for e, t_dict in grouped_snaps.items():
            for t, s_rows in sorted(t_dict.items(), key=lambda x: x[0]):
                in_scope = [r for r in s_rows if min_strike <= float(r["strike"]) <= max_strike]
                ce_oi = sum(float(r["ce_oi"] or 0) for r in in_scope)
                pe_oi = sum(float(r["pe_oi"] or 0) for r in in_scope)
                ce_vol = sum(float(r["ce_vol"] or 0) for r in in_scope)
                pe_vol = sum(float(r["pe_vol"] or 0) for r in in_scope)
                snaps_by_exp.setdefault(e, []).append((t, ce_oi, pe_oi, ce_vol, pe_vol))

        # Startup Protection for Late Boot: Ensure 09:15 baseline anchor exists for all active expiries
        for e in active_exps:
            e_snaps = snaps_by_exp.get(e, [])
            if e_snaps and e_snaps[0][0] > today_start:
                ch_e = self.chains.get(e)
                prev_ce = 0.0
                prev_pe = 0.0
                if ch_e and ch_e.get("rows"):
                    in_scope = [r for r in ch_e["rows"] if min_strike <= float(r["strike"]) <= max_strike]
                    prev_ce = sum(max(0.0, float(r["ce"].get("oi") or 0) - float(r["ce"].get("oi_chg") or 0)) for r in in_scope)
                    prev_pe = sum(max(0.0, float(r["pe"].get("oi") or 0) - float(r["pe"].get("oi_chg") or 0)) for r in in_scope)
                if prev_ce <= 0.0 or prev_pe <= 0.0:
                    prev_s_rows = db.query("""
                        SELECT strike, ce_oi, pe_oi FROM chain_snap
                        WHERE expiry = ? AND ts < ?
                        AND ts = (SELECT MAX(ts) FROM chain_snap WHERE expiry = ? AND ts < ?)
                    """, (e, today_start, e, today_start))
                    if prev_s_rows:
                        in_scope = [r for r in prev_s_rows if min_strike <= float(r["strike"]) <= max_strike]
                        prev_ce = sum(float(r["ce_oi"] or 0) for r in in_scope)
                        prev_pe = sum(float(r["pe_oi"] or 0) for r in in_scope)
                if prev_ce > 0.0 or prev_pe > 0.0:
                    snaps_by_exp[e].insert(0, (today_start, prev_ce, prev_pe, 0.0, 0.0))

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
            "scope": scope,
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

    def get_stockmojo_oi_change(
        self,
        exp: Optional[str] = "cur",
        fut_src: str = "combined",
        baseline_mode: str = "exchange",
        hub: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        StockMojo Parity OI Change (Call vs Put) time series feeder.
        Calculates:
          Exchange ΔOI = Σ(current_strike_oi - yesterday_eod_close_oi)
        Returns:
          series with ts, time, time_label, fut_close, call_oi_chg, put_oi_chg,
          call_oi_chg_cr, put_oi_chg_cr, net_diff_cr, call_oi, put_oi, pcr_oi.
        """
        today_iso = now_ist().date().isoformat()
        active_exps = [e for e in (self.expiries or sorted(self.chains)) if e >= today_iso]
        if not active_exps:
            active_exps = list(self.expiries or sorted(self.chains))
        cur_exp = active_exps[0] if active_exps else None
        next_exp = active_exps[1] if len(active_exps) > 1 else None
        monthly_exp = active_exps[-1] if active_exps else cur_exp

        sel_exp = cur_exp if exp in ("cur", None) else (next_exp if exp == "next" else (monthly_exp if exp == "monthly" else exp))

        trinity = self.get_pcr_trinity(exp=sel_exp, fut_src=fut_src, hub=hub, scope="all")
        raw_series = trinity.get("pcr_oi_series", [])
        if not raw_series:
            raw_series = (trinity.get("series") or {}).get("cur", [])

        # Intraday baseline offset if user explicitly selected intraday mode
        first_intraday_ce = 0.0
        first_intraday_pe = 0.0
        if baseline_mode == "intraday" and raw_series:
            first_intraday_ce = float(raw_series[0].get("call_oi", 0))
            first_intraday_pe = float(raw_series[0].get("put_oi", 0))

        series_out = []
        for pt in raw_series:
            t = int(pt.get("ts", 0))
            dt = to_ist(t)
            fut_c = round(float(pt.get("fut_close", 0.0)), 2)
            c_oi = int(pt.get("call_oi", 0))
            p_oi = int(pt.get("put_oi", 0))

            if baseline_mode == "intraday" and first_intraday_ce > 0:
                c_chg = int(c_oi - first_intraday_ce)
                p_chg = int(p_oi - first_intraday_pe)
            else:
                c_chg = int(pt.get("call_oi_chg", 0))
                p_chg = int(pt.get("put_oi_chg", 0))

            c_chg_cr = round(c_chg / 10000000.0, 2)
            p_chg_cr = round(p_chg / 10000000.0, 2)
            net_cr = round((p_chg - c_chg) / 10000000.0, 2)

            series_out.append({
                "ts": t,
                "time": dt.strftime("%H:%M"),
                "time_label": dt.strftime("%-d %b, %-I:%M %p"),
                "fut_close": fut_c,
                "call_oi_chg": c_chg,
                "put_oi_chg": p_chg,
                "call_oi_chg_cr": c_chg_cr,
                "put_oi_chg_cr": p_chg_cr,
                "net_diff_cr": net_cr,
                "call_oi": c_oi,
                "put_oi": p_oi,
                "pcr_oi": round(float(pt.get("pcr_oi", 1.0)), 2),
            })

        def _fmt(s: Optional[str]) -> str:
            if not s:
                return "—"
            try:
                from datetime import datetime
                d = datetime.fromisoformat(s.replace("Z", ""))
                return d.strftime("%d %b")
            except Exception:
                return str(s)

        exp_meta = dict(trinity.get("expiries_meta", {}))
        exp_meta["monthly_date"] = _fmt(monthly_exp)
        exp_meta["monthly_expiry"] = monthly_exp

        pts_map = {p["time"]: p for p in series_out}
        checkpoints = []
        for ck in ("09:15", "09:58", "10:00", "12:15", "15:27", "15:30"):
            if ck in pts_map:
                checkpoints.append(pts_map[ck])

        return {
            "status": "ok",
            "opt_expiry": sel_exp,
            "fut_contract": fut_src,
            "baseline_mode": baseline_mode,
            "expiries_meta": exp_meta,
            "series": series_out,
            "latest": series_out[-1] if series_out else None,
            "checkpoints": checkpoints,
            "scale": "crores",
        }

    def get_writer_flow(
        self,
        exp: Optional[str] = None,
        fut_src: str = "combined",
        tf: str = "1m",
        hub: Optional[Any] = None,
        scope: str = "all",
    ) -> Dict[str, Any]:
        """
        Builds the complete Classic Option Writer Flow payload from the exact same
        full-session (09:15–15:30 IST) trinity series for cur, next, and both expiries.
        Eliminates duplicate timestamps, truncation after 10:05, and expiry mismatches.
        """
        trinity = self.get_pcr_trinity(exp=exp, fut_src=fut_src, hub=hub, scope=scope)
        series = trinity.get("pcr_oi_series", [])
        if not series:
            series = (trinity.get("series") or {}).get("cur", [])

        meta = trinity.get("expiries_meta") or {}
        sel_exp = trinity.get("selected_expiry") or exp or "cur"
        today_s = str(now_ist().date())

        if sel_exp == "both":
            exp_label = f"Both ({meta.get('cur_date', '27 Oct')} + {meta.get('next_date', '03 Nov')})"
        elif sel_exp in ("next", meta.get("next_expiry")):
            exp_label = meta.get("next_date", "03 Nov")
        else:
            exp_label = meta.get("cur_date", "27 Oct")

        pts = []
        prev_net = 0.0
        prev_ce_day = 0.0
        prev_pe_day = 0.0
        rolling_flows: List[float] = []

        for i, item in enumerate(series):
            t = int(item.get("time", 0))
            if t <= 0:
                continue
            hm_str = datetime.utcfromtimestamp(t).strftime("%H:%M")

            fut = float(item.get("fut_close", 0.0))
            ce_day = float(item.get("call_oi_chg", 0.0))
            pe_day = float(item.get("put_oi_chg", 0.0))
            net_oi = pe_day - ce_day

            step_ce = ce_day - prev_ce_day if i > 0 else ce_day
            step_pe = pe_day - prev_pe_day if i > 0 else pe_day
            flow_val = step_pe - step_ce

            rolling_flows.append(abs(flow_val))
            if len(rolling_flows) > 20:
                rolling_flows.pop(0)
            avg_flow = sum(rolling_flows) / max(len(rolling_flows), 1)
            sig = abs(flow_val) >= (avg_flow * 2.2) if avg_flow > 0 else False

            # 30-min rolling balance
            lookback_bars = 10 if tf in ("3m", "5m") else 30
            start_idx = max(0, i - lookback_bars)
            window_slice = series[start_idx:i+1]
            if len(window_slice) >= 2:
                w_first = window_slice[0]
                d_pe_30 = pe_day - float(w_first.get("put_oi_chg", 0.0))
                d_ce_30 = ce_day - float(w_first.get("call_oi_chg", 0.0))
                denom = max(abs(d_pe_30) + abs(d_ce_30), 100000.0)
                bal = round(max(-1.0, min(1.0, (d_pe_30 - d_ce_30) / denom)), 4)
            else:
                bal = 0.0

            flip = (prev_net <= 0 and net_oi > 0) or (prev_net >= 0 and net_oi < 0)
            conf = "BULL" if net_oi > 0 else "BEAR"
            regime = "BULL" if pe_day > ce_day else "BEAR"

            pts.append({
                "t": t,
                "hm": hm_str,
                "fut": round(fut, 1),
                "ce": round(ce_day, 1),
                "pe": round(pe_day, 1),
                "flow": round(flow_val, 1),
                "bal": bal,
                "regime": regime,
                "sig": sig,
                "flip": flip,
                "conf": conf,
            })

            prev_net = net_oi
            prev_ce_day = ce_day
            prev_pe_day = pe_day

        last_pt = pts[-1] if pts else {}
        last_ce = float(last_pt.get("ce", 0.0))
        last_pe = float(last_pt.get("pe", 0.0))
        net_gap = abs(last_pe - last_ce)

        def _fmt_inr(v: float) -> str:
            sign = "+" if v >= 0 else "-"
            av = abs(v)
            if av >= 1e7:
                return f"{sign}{av/1e7:.2f}Cr"
            if av >= 1e5:
                return f"{sign}{av/1e5:.2f}L"
            return f"{sign}{av:.0f}"

        read_str = (
            f"Since yesterday: Put {_fmt_inr(last_pe)} vs Call {_fmt_inr(last_ce)} "
            f"(gap {_fmt_inr(net_gap)}). Writers: {'Put Writers leading (Bullish Floor)' if last_pe > last_ce else 'Call Writers leading (Bearish Resistance)'}."
        )
        note_str = (
            f"<b>Active Expiry:</b> {exp_label}. Last trend: <b>{last_pt.get('conf', 'BAL')}</b>. "
            f"30-min balance: <b>{last_pt.get('bal', 0.0)}</b>."
        )

        fut_name = meta.get("near_fut", "Oct FUT") if fut_src == "near" else (meta.get("next_fut", "Nov FUT") if fut_src == "next" else "Oct+Nov Combined")

        return {
            "ok": True,
            "day": today_s,
            "tf": tf,
            "label": f"NIFTY future {fut_name} (expiry {exp_label})",
            "expiry": exp_label,
            "unwind": None,
            "read": read_str,
            "note": note_str,
            "points": pts,
            "snapshots": len(pts),
        }

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

        trinity_scope = "atm_10" if range_mode == "atm" and span == 10 else ("atm_5" if range_mode == "atm" and span == 5 else "all")
        trinity = self.get_pcr_trinity(exp=expiry_mode, fut_src=fut_src, hub=hub, scope=trinity_scope) if hasattr(self, "get_pcr_trinity") else {}
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
            payload["trinity_series"] = trinity.get("series") if isinstance(trinity, dict) else None
            payload["pcr_oi_series"] = trinity.get("pcr_oi_series") if isinstance(trinity, dict) else None
        return payload

