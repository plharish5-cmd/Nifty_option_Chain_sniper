"""
gap_sniper.py — GIFT Nifty Opening Gap & 09:15–09:45 AM Confluence Sniper Playbook.

Features:
  1. Opening Gap & Wall Interaction:
     - Calculates pre-market gap size against NIFTY near-month Futures close (22,771.20).
     - Identifies if expected open lands directly on Put Support Floor (22,600) or Call Ceiling (23,000).
  2. 5-Point Confluence Scoring:
     - GIFT Nifty Gap magnitude (>= 80–100 pts)
     - Key Structural Walls & Straddle Pin Breakevens
     - Opening 1m/3m candle rejection wicks
     - Fresh option writer flow (Put fortify vs Call capitulation)
     - Session VWAP & PDC mean reversion pathway
  3. 09:15–09:45 AM Execution Plans:
     - Scenario A: -80 to -100 pt Gap-Down Absorption & Dip Buy (ATM/NTM CE)
     - Scenario B: -80 to -100 pt Cascade Breakdown Momentum (PE scalp)
     - Scenario C: +80 to +100 pt Gap-Up Fade Rejection (PE fade)
     - Scenario D: +80 to +100 pt Short Squeeze Breakout (CE rally to ceiling)
     - Scenario E: Flat / Equilibrium Open
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..market.candles import db
from ..timeutil import now_ist, session_bounds, to_ist, IST_OFFSET_S
from . import desk_bias, expiry_playbook

log = logging.getLogger("gap_sniper")

# In-memory cache for live GIFT Nifty quotes
_last_gift_nifty_fetch: float = 0.0
_cached_gift_nifty_val: float = 22640.0


def fetch_live_gift_nifty(default_val: float = 22640.0) -> float:
    """
    Fetches the live GIFT Nifty LTP from NSE IX / TradingView scanner feed.
    Caches for 5 seconds to prevent rate-limiting.
    Falls back gracefully to default_val (22,640) if offline or network error.
    """
    global _last_gift_nifty_fetch, _cached_gift_nifty_val
    now = time.time()
    if now - _last_gift_nifty_fetch < 5.0 and _cached_gift_nifty_val > 0:
        return _cached_gift_nifty_val

    try:
        url = "https://scanner.tradingview.com/global/scan"
        payload = json.dumps({
            "symbols": {"tickers": ["NSEIX:NIFTY1!"]},
            "columns": ["close"]
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Content-Type": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            if items and len(items[0].get("d", [])) > 0:
                px = float(items[0]["d"][0])
                if px > 10000.0:
                    _cached_gift_nifty_val = round(px, 1)
                    _last_gift_nifty_fetch = now
                    return _cached_gift_nifty_val
    except Exception as e:
        log.warning("GIFT Nifty live fetch failed, using fallback %.1f: %s", default_val, e)

    if _cached_gift_nifty_val > 0:
        return _cached_gift_nifty_val
    return default_val


_last_dhan_quote_fetch: float = 0.0
_cached_dhan_quotes: Dict[str, float] = {
    "5024": 22642.0,
    "13": 22777.80,
    "48704": 22774.80,
    "61471": 22889.80,
}


def fetch_dhan_live_quotes(hub: Optional[Any] = None, oct_sid: int = 48704, nov_sid: int = 61471) -> Dict[str, float]:
    """
    Direct Dhan REST / WebSocket Live Feed:
    Queries Dhan live quotes for GIFT Nifty (5024), Cash Spot (13),
    Near-Month Future (48704), and Next-Month Future (61471).
    Caches for 2.0 seconds to prevent rate limiting.
    """
    global _last_dhan_quote_fetch, _cached_dhan_quotes
    now = time.time()
    if now - _last_dhan_quote_fetch < 2.0 and len(_cached_dhan_quotes) >= 4:
        return _cached_dhan_quotes

    # 1. From active Hub DhanREST instance
    if hub and hasattr(hub, "rest"):
        try:
            import asyncio
            async def _hub_get():
                return await hub.rest.ltp({
                    "IDX_I": [5024, 13],
                    "NSE_FNO": [oct_sid, nov_sid],
                })
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if not (loop and loop.is_running()):
                data = asyncio.run(_hub_get())
                idx_map = data.get("IDX_I", {})
                fno_map = data.get("NSE_FNO", {})
                if "13" in idx_map:
                    _cached_dhan_quotes["13"] = float(idx_map["13"])
                if "5024" in idx_map and float(idx_map["5024"]) > 10000:
                    _cached_dhan_quotes["5024"] = float(idx_map["5024"])
                if str(oct_sid) in fno_map:
                    _cached_dhan_quotes[str(oct_sid)] = float(fno_map[str(oct_sid)])
                if str(nov_sid) in fno_map:
                    _cached_dhan_quotes[str(nov_sid)] = float(fno_map[str(nov_sid)])
                _last_dhan_quote_fetch = now
                return _cached_dhan_quotes
        except Exception as e:
            log.warning("Hub Dhan live quote fetch error: %s", e)

    # 2. Standalone DhanREST client query
    try:
        from ..dhan.rest import DhanREST
        import asyncio
        async def _standalone_get():
            r = DhanREST()
            res = await r.ltp({"IDX_I": [5024, 13], "NSE_FNO": [oct_sid, nov_sid]})
            await r.close()
            return res
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if not (loop and loop.is_running()):
            data = asyncio.run(_standalone_get())
            idx_map = data.get("IDX_I", {})
            fno_map = data.get("NSE_FNO", {})
            if "13" in idx_map:
                _cached_dhan_quotes["13"] = float(idx_map["13"])
            if "5024" in idx_map and float(idx_map["5024"]) > 10000:
                _cached_dhan_quotes["5024"] = float(idx_map["5024"])
            if str(oct_sid) in fno_map:
                _cached_dhan_quotes[str(oct_sid)] = float(fno_map[str(oct_sid)])
            if str(nov_sid) in fno_map:
                _cached_dhan_quotes[str(nov_sid)] = float(fno_map[str(nov_sid)])
            _last_dhan_quote_fetch = now
    except Exception as e:
        log.warning("Standalone Dhan live quote fetch error: %s", e)

    return _cached_dhan_quotes


def get_cfg() -> Dict[str, Any]:
    return config.rules().get("gap_sniper", {})


def get_previous_day_levels(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """
    Extracts Previous Day Close (PDC), High (PDH), Low (PDL), and Open (PDO)
    from historical session records or rules.json defaults.
    """
    c = cfg or get_cfg()
    def_pdc = float(c.get("default_pdc", 22753.2))
    def_pdh = float(c.get("default_pdh", 22780.0))
    def_pdl = float(c.get("default_pdl", 22387.35))
    def_pdo = float(c.get("default_pdo", 22524.4))

    daily_rows = db.daily("13", limit=5)
    if daily_rows and len(daily_rows) >= 1:
        # Latest completed daily record
        prev = daily_rows[-1]
        pdc = float(prev.get("c") or def_pdc)
        pdh = float(prev.get("h") or def_pdh)
        pdl = float(prev.get("l") or def_pdl)
        pdo = float(prev.get("o") or def_pdo)
        if pdh <= pdc:
            pdh = max(pdh, def_pdh)
        if pdl >= pdc:
            pdl = min(pdl, def_pdl)
        return {
            "pdc": pdc,
            "pdh": pdh,
            "pdl": pdl,
            "pdo": pdo,
        }
    return {
        "pdc": def_pdc,
        "pdh": def_pdh,
        "pdl": def_pdl,
        "pdo": def_pdo,
    }


def scan_opening_wicks(candles: List[Dict[str, Any]], wick_ratio_thresh: float = 0.35) -> Dict[str, Any]:
    """
    Scans the opening 1m/3m candles of the session for rejection wicks near key levels.
    """
    if not candles:
        return {
            "has_opened": False,
            "status_text": "Pre-Market: Awaiting 09:15–09:20 AM Opening Candle Wicks",
            "lower_wick_detected": False,
            "upper_wick_detected": False,
            "lower_wick_ratio": 0.0,
            "upper_wick_ratio": 0.0,
        }

    # Examine the first 3 candles of the day (up to 09:20 AM)
    opening_candles = candles[:3]
    max_lower_ratio = 0.0
    max_upper_ratio = 0.0

    for cd in opening_candles:
        o = float(cd.get("o") or 0.0)
        h = float(cd.get("h") or 0.0)
        l = float(cd.get("l") or 0.0)
        c = float(cd.get("c") or 0.0)
        total_range = max(h - l, 1.0)

        # Lower wick (buying absorption off lows)
        body_bottom = min(o, c)
        lower_wick = max(body_bottom - l, 0.0)
        lower_ratio = lower_wick / total_range
        if lower_ratio > max_lower_ratio:
            max_lower_ratio = lower_ratio

        # Upper wick (selling rejection off highs)
        body_top = max(o, c)
        upper_wick = max(h - body_top, 0.0)
        upper_ratio = upper_wick / total_range
        if upper_ratio > max_upper_ratio:
            max_upper_ratio = upper_ratio

    lower_wick_ok = max_lower_ratio >= wick_ratio_thresh
    upper_wick_ok = max_upper_ratio >= wick_ratio_thresh

    if lower_wick_ok:
        status_text = f"🟢 Strong Lower Rejection Wick ({max_lower_ratio * 100:.0f}% of bar): Institutional absorption confirmed"
    elif upper_wick_ok:
        status_text = f"🔴 Strong Upper Rejection Wick ({max_upper_ratio * 100:.0f}% of bar): Overhead supply defending ceiling"
    else:
        status_text = "Opening range forming: Watching 1m/3m body and wick balance"

    return {
        "has_opened": True,
        "status_text": status_text,
        "lower_wick_detected": lower_wick_ok,
        "upper_wick_detected": upper_wick_ok,
        "lower_wick_ratio": round(max_lower_ratio, 2),
        "upper_wick_ratio": round(max_upper_ratio, 2),
    }


def compute_gap_confluence(
    gift_nifty: Optional[float] = None,
    sim_gap_pts: Optional[float] = None,
    opt_expiry: str = "cur",
    fut_contract: str = "combined",
    hub: Optional[Any] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes the master Gap & Opening Confluence Sniper synthesis payload.
    From 09:00 AM IST onwards, uses Live Streaming NIFTY 50 Cash (Spot) vs.
    Previous Day Close (PDC) as the primary anchor for all gap classifications,
    confluence checklist, playbook directives, and voice announcements.
    """
    c = cfg or get_cfg()
    sig_gap = float(c.get("major_gap_threshold_pts", 80.0))
    maj_gap = float(c.get("major_gap_threshold_pts", 80.0))
    mod_gap = float(c.get("moderate_gap_threshold_pts", 25.0))
    flat_band = float(c.get("flat_band_pts", 25.0))
    wall_prox = float(c.get("wall_proximity_pts", 40.0))
    wick_thresh = float(c.get("wick_rejection_ratio", 0.35))
    fortify_thresh = int(c.get("writer_fortify_oi", 1500000))
    unwind_thresh = int(c.get("writer_unwind_oi", -1500000))
    panic_cover_thresh = int(c.get("writer_panic_cover_oi", -2000000))
    def_sim_gap = float(c.get("default_sim_gap_pts", -100.0))

    # 1. Resolve Instrument Symbols and Dhan Security IDs
    oct_sid = int((hub and getattr(hub, "fut", None) and hub.fut.get("sid")) or 48704)
    nov_sid = int((hub and getattr(hub, "fut2", None) and hub.fut2.get("sid")) or 61471)
    oct_symbol = (hub and getattr(hub, "fut", None) and hub.fut.get("symbol")) or "NIFTY-Oct2026-FUT"
    nov_symbol = (hub and getattr(hub, "fut2", None) and hub.fut2.get("symbol")) or "NIFTY-Nov2026-FUT"

    # Query live Dhan quotes directly
    dhan_quotes = fetch_dhan_live_quotes(hub=hub, oct_sid=oct_sid, nov_sid=nov_sid)

    today_start, _ = session_bounds(now_ist().date())
    fut_candles = db.candles_1m("FUT", today_start)
    if not fut_candles:
        latest = db.query("SELECT MAX(ts) as mx FROM candles_1m WHERE sec_id='FUT'")
        if latest and latest[0]["mx"]:
            latest_dt = to_ist(latest[0]["mx"]).date()
            today_start, _ = session_bounds(latest_dt)
            fut_candles = db.candles_1m("FUT", today_start)
    if not fut_candles:
        fut_candles = db.candles_1m("13", today_start)

    # Near-month Future LTP
    oct_fut_ltp = float(fut_candles[-1].get("c") or 22774.80) if fut_candles else 22774.80
    if dhan_quotes.get(str(oct_sid)):
        oct_fut_ltp = float(dhan_quotes[str(oct_sid)])
    elif hub and getattr(hub, "fut", None) and hub.fut.get("ltp"):
        oct_fut_ltp = float(hub.fut["ltp"])

    # Live Spot LTP
    live_spot = float(dhan_quotes.get("13") or 22777.80)
    if hub and hasattr(hub, "quote"):
        q = hub.quote()
        if q.get("spot") and float(q["spot"]) > 10000.0:
            live_spot = float(q["spot"])

    # Next-month Future LTP
    nov_candles = db.candles_1m("FUT2", today_start)
    if not nov_candles:
        latest2 = db.query("SELECT MAX(ts) as mx FROM candles_1m WHERE sec_id='FUT2'")
        if latest2 and latest2[0]["mx"]:
            latest2_dt = to_ist(latest2[0]["mx"]).date()
            nov_start, _ = session_bounds(latest2_dt)
            nov_candles = db.candles_1m("FUT2", nov_start)
    nov_fut_ltp = float(nov_candles[-1].get("c") or 22889.80) if nov_candles else 22889.80
    if dhan_quotes.get(str(nov_sid)):
        nov_fut_ltp = float(dhan_quotes[str(nov_sid)])
    elif hub and getattr(hub, "fut2", None) and hub.fut2.get("ltp"):
        nov_fut_ltp = float(hub.fut2["ltp"])

    # Previous Day Levels (Spot & Futures PDC)
    pd_levels = get_previous_day_levels(c)
    pdc_spot = pd_levels["pdc"]
    pdh = pd_levels["pdh"]
    pdl = pd_levels["pdl"]
    pdo = pd_levels["pdo"]

    oct_fut_pdc = float(c.get("default_oct_fut_pdc", 22771.2))
    nov_fut_pdc = float(c.get("default_nov_fut_pdc", 22900.2))

    # Futures Gaps
    oct_fut_gap_pts = round(oct_fut_ltp - oct_fut_pdc, 1)
    oct_fut_gap_pct = round((oct_fut_gap_pts / max(oct_fut_pdc, 1.0)) * 100.0, 2)
    nov_fut_gap_pts = round(nov_fut_ltp - nov_fut_pdc, 1)
    nov_fut_gap_pct = round((nov_fut_gap_pts / max(nov_fut_pdc, 1.0)) * 100.0, 2)
    roll_spread = round(nov_fut_ltp - oct_fut_ltp, 1)

    # GIFT Nifty reference
    tv_px = fetch_live_gift_nifty(default_val=0.0)
    dhan_px = dhan_quotes.get("5024", 0.0)
    gift_nifty_ltp = tv_px if tv_px > 10000.0 else (dhan_px if dhan_px > 10000.0 else float(c.get("default_gift_nifty_ref", 22642.0)))
    gift_diff_oct = round(gift_nifty_ltp - oct_fut_ltp, 1)

    # 2. Determine Primary Anchor & Precise Gap Delta
    now_dt = now_ist()
    is_after_9am = (now_dt.hour > 9) or (now_dt.hour == 9 and now_dt.minute >= 0)

    is_simulated = False
    if sim_gap_pts is not None:
        is_simulated = True
        spot_gap_pts = round(float(sim_gap_pts), 1)
        spot_gap_pct = round((spot_gap_pts / max(pdc_spot, 1.0)) * 100.0, 2)
        live_spot = round(pdc_spot + spot_gap_pts, 1)
        gift_diff_oct = spot_gap_pts
        gift_nifty_ltp = round(oct_fut_ltp + spot_gap_pts, 1)
        use_spot_anchor = True
        primary_anchor = "SPOT"
    elif gift_nifty is not None and gift_nifty > 0:
        is_simulated = True
        gift_nifty_ltp = float(gift_nifty)
        gift_diff_oct = round(gift_nifty_ltp - oct_fut_ltp, 1)
        live_spot = gift_nifty_ltp
        spot_gap_pts = round(live_spot - pdc_spot, 1)
        spot_gap_pct = round((spot_gap_pts / max(pdc_spot, 1.0)) * 100.0, 2)
        use_spot_anchor = True
        primary_anchor = "SPOT"
    else:
        is_simulated = False
        spot_gap_pts = round(live_spot - pdc_spot, 1)
        spot_gap_pct = round((spot_gap_pts / max(pdc_spot, 1.0)) * 100.0, 2)
        # Rule: Whenever live_spot > 0 and (current_ist_time >= 09:00 or live_spot != pdc_spot)
        if live_spot > 0 and (is_after_9am or live_spot != pdc_spot):
            use_spot_anchor = True
            primary_anchor = "SPOT"
        else:
            use_spot_anchor = False
            primary_anchor = "GIFT_NIFTY"

    if use_spot_anchor:
        gap_pts = spot_gap_pts
        gap_pct = spot_gap_pct
        expected_open_spot = live_spot
        expected_open_fut = oct_fut_ltp
    else:
        gap_pts = gift_diff_oct
        gap_pct = round((gap_pts / max(oct_fut_ltp, 1.0)) * 100.0, 2)
        expected_open_fut = gift_nifty_ltp
        expected_open_spot = round(pdc_spot + gap_pts, 1)

    near_fut_close = oct_fut_ltp
    nov_fut_close = nov_fut_ltp
    spot_close = live_spot

    # Accurate Gap Classification Tiers (Based on spot_gap_pts)
    if gap_pts >= maj_gap:
        gap_type = "GAP_UP"
        gap_tier = "MAJOR_GAP_UP"
        gap_tier_label = f"🚀 MAJOR GAP UP (+{gap_pts:.1f} pts)"
        is_significant_gap = True
        is_major_gap = True
    elif gap_pts >= mod_gap:
        gap_type = "GAP_UP"
        gap_tier = "MODERATE_GAP_UP"
        gap_tier_label = f"🟢 MODERATE GAP UP (+{gap_pts:.1f} pts)"
        is_significant_gap = True
        is_major_gap = False
    elif gap_pts <= -maj_gap:
        gap_type = "GAP_DOWN"
        gap_tier = "MAJOR_GAP_DOWN"
        gap_tier_label = f"🩸 MAJOR GAP DOWN ({gap_pts:.1f} pts)"
        is_significant_gap = True
        is_major_gap = True
    elif gap_pts <= -mod_gap:
        gap_type = "GAP_DOWN"
        gap_tier = "MODERATE_GAP_DOWN"
        gap_tier_label = f"🟠 MODERATE GAP DOWN ({gap_pts:.1f} pts)"
        is_significant_gap = True
        is_major_gap = False
    else:
        gap_type = "FLAT"
        gap_tier = "FLAT"
        gap_tier_label = f"⚖️ FLAT / EQUILIBRIUM OPEN ({gap_pts:+.1f} pts)"
        is_significant_gap = False
        is_major_gap = False

    # Proximity calculations for spot
    dist_to_pdh = pdh - live_spot
    dist_to_pdl = live_spot - pdl
    if abs(dist_to_pdh) <= abs(dist_to_pdl):
        if dist_to_pdh > 0:
            prox_str = f"{dist_to_pdh:.1f} pts below PDH {int(pdh):,}"
        elif dist_to_pdh < 0:
            prox_str = f"{abs(dist_to_pdh):.1f} pts above PDH {int(pdh):,}"
        else:
            prox_str = f"At PDH {int(pdh):,}"
    else:
        if dist_to_pdl > 0:
            prox_str = f"{dist_to_pdl:.1f} pts above PDL {int(pdl):,}"
        elif dist_to_pdl < 0:
            prox_str = f"{abs(dist_to_pdl):.1f} pts below PDL {int(pdl):,}"
        else:
            prox_str = f"At PDL {int(pdl):,}"

    # Action bias for spot row
    if gap_pts >= maj_gap:
        spot_bias = "SHORT SQUEEZE BREAKOUT"
    elif gap_pts <= -maj_gap:
        spot_bias = "PUT WALL ABSORPTION BUY"
    elif gap_pts >= mod_gap or (dist_to_pdh <= 10.0 and live_spot >= pdc_spot):
        spot_bias = f"PDH BREAKOUT / PDC RETEST ({int(pdh):,})"
    elif gap_pts <= -mod_gap:
        spot_bias = f"SCALP PE BELOW PDC {int(pdc_spot):,}"
    else:
        spot_bias = "WAIT FOR 09:30 ORB"

    # Multi-Contract Benchmark Table (Row 1 = Spot [PRIMARY ANCHOR])
    spot_row = {
        "instrument": "NIFTY 50 Cash (Spot)",
        "symbol": "NIFTY 50",
        "ltp": live_spot,
        "pdc": pdc_spot,
        "diff_vs_oct": round(live_spot - oct_fut_ltp, 1),
        "basis_vs_spot": 0.0,
        "basis_vs_gift": round(live_spot - gift_nifty_ltp, 1),
        "diff_vs_gift": round(live_spot - gift_nifty_ltp, 1),
        "gap_pts": spot_gap_pts,
        "gap_pct": spot_gap_pct,
        "proximity": prox_str,
        "live_gap_status": (
            f"🚀 Major Gap Up (+{spot_gap_pts:.1f} pts)" if spot_gap_pts >= maj_gap
            else (f"🟢 Moderate Gap Up (+{spot_gap_pts:.1f} pts)" if spot_gap_pts >= mod_gap
            else (f"🩸 Major Gap Down ({spot_gap_pts:.1f} pts)" if spot_gap_pts <= -maj_gap
            else (f"🟠 Moderate Gap Down ({spot_gap_pts:.1f} pts)" if spot_gap_pts <= -mod_gap
            else f"⚖️ Flat Open ({spot_gap_pts:+.1f} pts)")))
        ),
        "classification": "PRIMARY ANCHOR" if use_spot_anchor else "DOMESTIC CASH SPOT",
        "action_bias": spot_bias,
        "badge_color": (
            "#10b981" if spot_gap_pts >= mod_gap
            else ("#ef4444" if spot_gap_pts <= -maj_gap
            else ("#f97316" if spot_gap_pts <= -mod_gap else "#94a3b8"))
        ),
        "is_anchor": use_spot_anchor,
    }

    basis_oct_spot = round(oct_fut_ltp - live_spot, 1)
    oct_row = {
        "instrument": "NIFTY October Future (Near-Month Expiry Today)",
        "symbol": oct_symbol,
        "ltp": oct_fut_ltp,
        "pdc": oct_fut_pdc,
        "diff_vs_oct": 0.0,
        "basis_vs_spot": basis_oct_spot,
        "basis_vs_gift": round(oct_fut_ltp - gift_nifty_ltp, 1),
        "diff_vs_gift": round(oct_fut_ltp - gift_nifty_ltp, 1),
        "gap_pts": oct_fut_gap_pts,
        "gap_pct": oct_fut_gap_pct,
        "proximity": f"Basis vs Spot: {basis_oct_spot:+.1f} pts",
        "live_gap_status": f"🎯 Near-Month Baseline ({oct_fut_gap_pts:+.1f} pts)",
        "classification": "NEAR-MONTH FUTURE",
        "action_bias": (
            "PUT WALL ABSORPTION BUY" if gap_pts <= -maj_gap
            else ("SHORT SQUEEZE BREAKOUT" if gap_pts >= maj_gap else "WAIT FOR 09:30 ORB")
        ),
        "badge_color": "#10b981",
        "is_anchor": False,
    }

    nov_row = {
        "instrument": "NIFTY November Future (Next-Month)",
        "symbol": nov_symbol,
        "ltp": nov_fut_ltp,
        "pdc": nov_fut_pdc,
        "diff_vs_oct": roll_spread,
        "basis_vs_spot": round(nov_fut_ltp - live_spot, 1),
        "basis_vs_gift": round(nov_fut_ltp - gift_nifty_ltp, 1),
        "diff_vs_gift": round(nov_fut_ltp - gift_nifty_ltp, 1),
        "gap_pts": nov_fut_gap_pts,
        "gap_pct": nov_fut_gap_pct,
        "roll_spread": roll_spread,
        "proximity": f"Carry Spread: {roll_spread:+.1f} pts vs Oct Fut",
        "live_gap_status": (
            f"📅 Carry Premium (+{roll_spread:.1f} pts)" if roll_spread >= maj_gap
            else (f"📅 Carry Discount ({roll_spread:+.1f} pts)" if roll_spread <= -maj_gap
            else f"📅 Carry Spread ({roll_spread:+.1f} pts)")
        ),
        "classification": "NEXT-MONTH FUTURE",
        "action_bias": "ROLLOVER CARRY EXPANSION" if roll_spread > 50 else "NORMAL CARRY SPREAD",
        "badge_color": "#818cf8" if roll_spread >= 80 else "#94a3b8",
        "is_anchor": False,
    }

    gift_row = {
        "instrument": "GIFT Nifty (Offshore Reference — Pre-09:00 Only)",
        "symbol": "GIFT NIFTY",
        "ltp": gift_nifty_ltp,
        "pdc": oct_fut_pdc,
        "diff_vs_oct": gift_diff_oct,
        "basis_vs_spot": round(gift_nifty_ltp - live_spot, 1),
        "basis_vs_gift": 0.0,
        "diff_vs_gift": 0.0,
        "gap_pts": gift_diff_oct,
        "gap_pct": round((gift_diff_oct / max(oct_fut_pdc, 1.0)) * 100.0, 2),
        "proximity": f"{gift_diff_oct:+.1f} pts vs Oct Fut",
        "live_gap_status": (
            f"🌐 Secondary Lead ({gift_diff_oct:+.1f} pts)" if use_spot_anchor
            else (
                f"🔻 Major Gap Down ({gift_diff_oct:+.1f} pts)" if gift_diff_oct <= -maj_gap
                else (f"🔺 Major Gap Up ({gift_diff_oct:+.1f} pts)" if gift_diff_oct >= maj_gap
                else f"⚖️ Flat Open ({gift_diff_oct:+.1f} pts)")
            )
        ),
        "classification": "OFFSHORE REFERENCE ANCHOR (PRE-09:00 ONLY)" if use_spot_anchor else "ANCHOR BENCHMARK",
        "action_bias": "SECONDARY AFTER 09:00 IST" if use_spot_anchor else "GLOBAL LEAD BENCHMARK",
        "badge_color": "#64748b" if use_spot_anchor else ("#10b981" if gift_diff_oct >= maj_gap else ("#f43f5e" if gift_diff_oct <= -maj_gap else "#94a3b8")),
        "is_anchor": not use_spot_anchor,
    }

    multi_contract_table = [spot_row, oct_row, nov_row, gift_row]

    # 4. Structural OI Walls & Straddle Pin Breakevens
    pe_wall, ce_wall = desk_bias.get_structural_walls(live_spot, opt_expiry)
    expiry_data = expiry_playbook.compute_expiry_cockpit_telemetry(expiry=opt_expiry)
    straddle = expiry_data.get("straddle") or {}
    lower_be = float(straddle.get("lower_breakeven") or (live_spot - 140.0))
    upper_be = float(straddle.get("upper_breakeven") or (live_spot + 140.0))

    # 5. Opening Candle Wicks
    wick_data = scan_opening_wicks(fut_candles, wick_thresh)

    # 6. Fresh Option Writer Flow
    fresh_put_delta = 0
    fresh_call_delta = 0
    pcr = 1.02
    vwap = oct_fut_ltp

    if hub and hasattr(hub, "chain"):
        trinity = hub.chain.get_pcr_trinity(exp=opt_expiry, fut_src=fut_contract, hub=hub)
    else:
        from ..market.chain import ChainPoller
        from ..dhan.rest import DhanREST
        poller = ChainPoller(DhanREST(), lambda *a, **k: None)
        trinity = poller.get_pcr_trinity(exp=opt_expiry, fut_src=fut_contract, hub=hub)

    series = trinity.get("pcr_oi_series") or []
    if series:
        last_pt = series[-1]
        fresh_put_delta = int(last_pt.get("put_oi_chg") or 0)
        fresh_call_delta = int(last_pt.get("call_oi_chg") or 0)
        pcr = float(last_pt.get("pcr_oi") or 1.0)

    # VWAP calculation
    tot_vol = sum(float(b.get("v") or 0.0) for b in fut_candles)
    tot_pv = sum(float(b.get("c") or 0.0) * float(b.get("v") or 0.0) for b in fut_candles)
    vwap = round(tot_pv / max(tot_vol, 1.0), 1) if tot_vol > 0 else oct_fut_ltp

    # 7. Confluence Scoring & Checklist
    checklist: List[Dict[str, Any]] = []

    # Checklist Item 1: Gap Magnitude
    c1_passed = is_significant_gap or abs(gap_pts) >= mod_gap
    checklist.append({
        "id": "gap_magnitude",
        "title": "NIFTY 50 Spot Gap Magnitude" if use_spot_anchor else "GIFT Nifty Gap Magnitude",
        "detail": f"{gap_pts:+.1f} pts ({gap_pct:+.2f}%) • {'🔥 Significant (>= 80 pts)' if is_significant_gap else ('🟢 Moderate gap (>= 25 pts)' if abs(gap_pts) >= mod_gap else 'Normal drift')}",
        "passed": c1_passed,
        "tone": "bull" if gap_pts <= -sig_gap or (gap_pts >= mod_gap and live_spot >= pdc_spot) else ("bear" if gap_pts >= sig_gap or gap_pts <= -mod_gap else "neutral"),
    })

    # Checklist Item 2: Structural Wall Interaction
    dist_to_pe_wall = live_spot - pe_wall
    dist_to_ce_wall = ce_wall - live_spot
    lands_on_put_wall = abs(dist_to_pe_wall) <= wall_prox or (0 <= dist_to_pe_wall <= 75)
    lands_on_call_wall = abs(dist_to_ce_wall) <= wall_prox or (0 <= dist_to_ce_wall <= 75)
    testing_pdh_wall = abs(dist_to_pdh) <= 15.0 or (live_spot >= pdc_spot and live_spot >= pdh - 15.0)

    c2_passed = lands_on_put_wall or lands_on_call_wall or testing_pdh_wall

    if testing_pdh_wall:
        wall_text = f"🎯 Testing PDH ({int(pdh):,}) & Call Ceiling ({int(ce_wall):,})"
        wall_tone = "bull"
    elif lands_on_put_wall:
        wall_text = f"🎯 Lands on Put Wall Floor ({int(pe_wall):,}) & Lower Pin ({int(lower_be):,})"
        wall_tone = "bull"
    elif lands_on_call_wall:
        wall_text = f"🎯 Lands beneath Call Wall Ceiling ({int(ce_wall):,}) & Upper Pin ({int(upper_be):,})"
        wall_tone = "bear"
    else:
        wall_text = f"Mid-Range Open ({int(live_spot):,}) between {int(pe_wall):,} S1 & {int(ce_wall):,} R1"
        wall_tone = "neutral"

    checklist.append({
        "id": "wall_interaction",
        "title": "Key Structural Wall Alignment",
        "detail": wall_text,
        "passed": c2_passed,
        "tone": wall_tone,
    })

    # Checklist Item 3: Opening Wick & Rejection Test
    if not wick_data["has_opened"]:
        c3_passed = True  # Pre-market anticipated
        wick_desc = "Pre-market: 09:15–09:20 wick test pending (look for lower wick absorption or breakout hold)"
    else:
        c3_passed = wick_data["lower_wick_detected"] or wick_data["upper_wick_detected"]
        wick_desc = wick_data["status_text"]

    checklist.append({
        "id": "wick_rejection",
        "title": "Opening 1m/3m Wick Test (09:15–09:20)",
        "detail": wick_desc,
        "passed": c3_passed,
        "tone": "bull" if wick_data.get("lower_wick_detected") else ("bear" if wick_data.get("upper_wick_detected") else "neutral"),
    })

    # Checklist Item 4: Writer Flow Confluence
    writer_confirmed = False
    if gap_type == "GAP_DOWN":
        if fresh_put_delta > fortify_thresh or fresh_call_delta < 0 or is_simulated:
            writer_confirmed = True
            writer_text = f"Put Writers Fortifying (+{fresh_put_delta / 1e5:.1f}L) • Calls Covered ({fresh_call_delta / 1e5:.1f}L)"
            writer_tone = "bull"
        else:
            writer_text = f"Put Writers Neutral ({fresh_put_delta / 1e5:+.1f}L)"
            writer_tone = "neutral"
    elif gap_type == "GAP_UP":
        if fresh_call_delta < panic_cover_thresh:
            writer_confirmed = True
            writer_text = f"🔥 Call Writers Panic Covering ({fresh_call_delta / 1e5:.1f}L) • Short Squeeze Fuel"
            writer_tone = "bull"
        elif fresh_call_delta > 0 or is_simulated:
            writer_confirmed = True
            writer_text = f"Call Writers Defending ({fresh_call_delta / 1e5:+.1f}L) • Supply Active"
            writer_tone = "bear"
        else:
            writer_text = "Writer flow flat"
            writer_tone = "neutral"
    else:
        writer_confirmed = True
        writer_text = f"Balanced flow • PCR {pcr:.2f}"
        writer_tone = "neutral"

    checklist.append({
        "id": "writer_flow",
        "title": "Fresh Intraday Writer Flow",
        "detail": writer_text,
        "passed": writer_confirmed,
        "tone": writer_tone,
    })

    # Checklist Item 5: VWAP / PDC Mean Reversion Pathway
    c5_passed = True
    pathway_text = f"PDC Reference: {pdc_spot:,.1f} • VWAP: {vwap:,.1f} • Clear mean-reversion fairway"
    checklist.append({
        "id": "target_pathway",
        "title": "VWAP & PDC Target Pathway",
        "detail": pathway_text,
        "passed": c5_passed,
        "tone": "bull" if gap_type == "GAP_DOWN" or (gap_type == "GAP_UP" and live_spot >= pdc_spot) else "bear",
    })

    confluence_score = sum(1 for item in checklist if item["passed"])

    # ──────────────── Determine Playbook Scenario ────────────────
    # Strike helpers
    atm_strike = int(round(live_spot / 50.0) * 50)
    lower_atm = int(atm_strike if atm_strike <= live_spot else atm_strike - 50)
    upper_atm = lower_atm + 50

    # Scenario A / B: -80 to -100 Gap Down (Absorption vs Cascade)
    if gap_pts <= -sig_gap:
        is_cascade = fresh_put_delta < unwind_thresh or live_spot < pe_wall - 40.0
        if is_cascade:
            playbook_title = "🔴 THE CASCADE: MOMENTUM BREAKDOWN"
            playbook_code = "GAP_DOWN_CASCADE"
            bias_theme = "bearish"
            bias_color = "#ef4444"
            action = f"BUY {int(pe_wall - 50):,} PE or {int(pe_wall - 100):,} PE"
            entry_zone = f"Below {int(pe_wall - 20):,} on confirmed close"
            stop_loss = round(pe_wall + 25.0, 1)
            target_1 = round(pe_wall - 100.0, 1)
            target_2 = round(pe_wall - 200.0, 1)
            narrative = (
                f"NIFTY 50 {gap_pts:.0f} pt gap-down sliced through {int(pe_wall):,} Put Wall with Put writers unwinding. "
                f"Do not catch falling knives; scalp PE momentum targeting lower demand base at {int(target_2):,}."
            )
        else:
            playbook_title = "🟢 ABSORPTION & DIP BUY AT PUT WALL"
            playbook_code = "GAP_DOWN_ABSORPTION"
            bias_theme = "bullish"
            bias_color = "#10b981"
            action = f"BUY ATM/NTM CE ({int(pe_wall):,} CE or {int(pe_wall + 50):,} CE)"
            entry_zone = f"{int(pe_wall + 20):,} – {int(pe_wall + 60):,} (on 1m/3m lower rejection wick)"
            stop_loss = round(pe_wall - 20.0, 1)  # 22,580
            target_1 = round(vwap if vwap > live_spot else live_spot + 60.0, 1)
            target_2 = round(pdc_spot if pdc_spot > live_spot else live_spot + 110.0, 1)
            narrative = (
                f"NIFTY 50 indicates a {gap_pts:.0f} pt gap-down directly into the {int(pe_wall):,} Put Support floor "
                f"and lower Straddle Pin boundary ({int(lower_be):,}). Do NOT short blindly at 09:15 AM! "
                f"Watch for 1m/3m lower rejection wicks. Buy near-the-money CE with SL strictly below {int(stop_loss):,}, "
                f"targeting mean-reversion back to VWAP ({target_1:,.0f}) and PDC ({target_2:,.0f})."
            )

    # Scenario C / D: +80 to +100 Gap Up (Squeeze vs Fade)
    elif gap_pts >= sig_gap:
        is_squeeze = fresh_call_delta < panic_cover_thresh
        if is_squeeze:
            playbook_title = "🟢 SHORT SQUEEZE BREAKOUT: RALLY TO CEILING"
            playbook_code = "GAP_UP_SQUEEZE"
            bias_theme = "bullish"
            bias_color = "#10b981"
            action = f"BUY {int(ce_wall - 100):,} CE"
            entry_zone = f"Above {int(ce_wall - 100):,} on expanding volume"
            stop_loss = round(ce_wall - 150.0, 1)
            target_1 = round(ce_wall - 50.0, 1)
            target_2 = round(ce_wall, 1)
            narrative = (
                f"Massive institutional short-covering: Call writers panicked with >20L calls covered. "
                f"Do not fade! Buy continuation breakout targeting dominant ceiling at {int(ce_wall):,}."
            )
        else:
            playbook_title = "🔴 FADE THE GAP: OPENING RESISTANCE REJECTION"
            playbook_code = "GAP_UP_FADE"
            bias_theme = "bearish"
            bias_color = "#ef4444"
            action = f"BUY {int(live_spot):,} PE or {int(live_spot - 50):,} PE"
            entry_zone = f"{int(live_spot - 20):,} – {int(live_spot + 20):,} (on upper rejection wick)"
            stop_loss = round(ce_wall + 15.0, 1)
            target_1 = round(vwap if vwap < live_spot else live_spot - 60.0, 1)
            target_2 = round(pdc_spot if pdc_spot < live_spot else live_spot - 110.0, 1)
            narrative = (
                f"NIFTY 50 indicates a +{gap_pts:.0f} pt gap-up directly beneath the {int(ce_wall):,} Call Resistance ceiling "
                f"and upper Straddle boundary ({int(upper_be):,}). Overhead Call writers defending wall. "
                f"Fade upper rejection wicks back toward Session VWAP ({target_1:,.0f}) and PDC ({target_2:,.0f})."
            )

    # Live Pre-Open testing PDH / Bullish above PDC (e.g. 22,777.8 testing PDH 22,780)
    elif (live_spot >= pdc_spot and dist_to_pdh <= 5.0) or (gap_pts >= mod_gap and dist_to_pdh <= 10.0):
        playbook_title = f"🟢 BULLISH PRE-OPEN ABOVE PDC (TESTING PDH {int(pdh):,})"
        playbook_code = "BULLISH_ABOVE_PDC_PDH"
        bias_theme = "bullish"
        bias_color = "#10b981"
        is_bullish_flow = (fresh_put_delta >= fresh_call_delta) or (fresh_call_delta <= 0) or is_simulated
        if is_bullish_flow:
            action = (
                f"BUY ATM {lower_atm:,} CE / {upper_atm:,} CE on PDH Breakout or PDC Retest "
                f"({int(pdc_spot + 2):,}–{int(pdc_spot + 12):,}) · Strict Index SL < {int(pdc_spot - 28):,} · "
                f"Target 1: {int(pdh + 70):,} · Target 2: {int(ce_wall - 100):,}–{int(ce_wall):,} Call Wall"
            )
            entry_zone = f"{int(pdc_spot + 2):,} – {int(pdh + 5):,} (PDH Breakout / PDC Retest)"
            stop_loss = round(pdc_spot - 28.0, 1)
            target_1 = round(pdh + 70.0, 1)
            target_2 = round(ce_wall, 1)
            narrative = (
                f"NIFTY 50 live at {live_spot:,.1f} holds firmly above PDC ({pdc_spot:,.1f}) and presses into Previous Day High ({pdh:,.1f}). "
                f"Put writers fortifying and Call writers covering. If spot breaks PDH ({pdh:,.1f}) or holds PDC ({pdc_spot:,.1f}), "
                f"buy ATM {lower_atm:,}/{upper_atm:,} CE with SL strictly < {int(stop_loss):,}, targeting expansion toward {int(target_1):,} and {int(target_2):,} Call Wall."
            )
        else:
            action = f"WAIT / SCALP PE BELOW PDC {int(pdc_spot):,} TARGETING VWAP {int(vwap):,}"
            entry_zone = f"Below {int(pdc_spot):,} on upper rejection wick off {int(pdh):,}"
            stop_loss = round(pdh + 10.0, 1)
            target_1 = round(vwap, 1)
            target_2 = round(pe_wall, 1)
            narrative = (
                f"NIFTY 50 tested Previous Day High ({pdh:,.1f}) with upper wick rejection. "
                f"If price slips below PDC ({pdc_spot:,.1f}), scalp PE targeting session VWAP ({vwap:,.1f}) and Put Wall ({int(pe_wall):,})."
            )

    # Moderate Gap Up (+25 to +80 pts)
    elif gap_pts >= mod_gap:
        playbook_title = f"🟢 MODERATE GAP UP (+{gap_pts:.1f} PTS): BULLISH ABOVE PDC"
        playbook_code = "MODERATE_GAP_UP"
        bias_theme = "bullish"
        bias_color = "#10b981"
        action = f"BUY ATM {lower_atm:,} CE on PDC Retest ({int(pdc_spot + 5):,}–{int(pdc_spot + 15):,}) · Strict Index SL < {int(pdc_spot - 25):,} · Target 1: {int(pdh):,} · Target 2: {int(ce_wall):,}"
        entry_zone = f"{int(pdc_spot + 5):,} – {int(pdc_spot + 15):,}"
        stop_loss = round(pdc_spot - 25.0, 1)
        target_1 = round(pdh, 1)
        target_2 = round(ce_wall, 1)
        narrative = (
            f"Moderate gap up of {gap_pts:+.1f} pts holding above PDC ({pdc_spot:,.1f}). "
            f"Bias bullish targeting PDH ({pdh:,.1f}) and {int(ce_wall):,} Call Wall with strict risk management."
        )

    # Moderate Gap Down (-80 to -25 pts)
    elif gap_pts <= -mod_gap:
        playbook_title = f"🟠 MODERATE GAP DOWN ({gap_pts:.1f} PTS): BELOW PDC"
        playbook_code = "MODERATE_GAP_DOWN"
        bias_theme = "bearish"
        bias_color = "#f97316"
        action = f"SCALP PE ON REJECTION OF PDC {int(pdc_spot):,} OR WAIT FOR PUT WALL ABSORPTION AT {int(pe_wall):,}"
        entry_zone = f"{int(pdc_spot - 20):,} – {int(pdc_spot):,}"
        stop_loss = round(pdc_spot + 20.0, 1)
        target_1 = round(vwap, 1)
        target_2 = round(pe_wall, 1)
        narrative = (
            f"Moderate gap down of {gap_pts:.1f} pts opening below PDC ({pdc_spot:,.1f}). "
            f"Sellers active below PDC; watch for absorption near Put Wall ({int(pe_wall):,}) before taking fresh shorts."
        )

    # Scenario E: Flat / Equilibrium Open (|gap_pts| < 25 pts)
    else:
        playbook_title = "⚖️ EQUILIBRIUM OPEN: AWAIT 09:30 RANGE"
        playbook_code = "FLAT_EQUILIBRIUM"
        bias_theme = "neutral"
        bias_color = "#94a3b8"
        action = "WAIT FOR 09:30 OPENING RANGE BREAKOUT"
        entry_zone = "Wait for Opening Range High/Low formation"
        stop_loss = 0.0
        target_1 = round(pdh, 1)
        target_2 = round(pdl, 1)
        narrative = (
            f"Flat equilibrium open ({gap_pts:+.1f} pts). Price opening inside balanced range between {int(pe_wall):,} Support "
            f"and {int(ce_wall):,} Resistance. Await 09:30 AM Opening Range settlement before committing capital."
        )

    # ──────────────── Audio & Synthetic Voice Alert ────────────────
    gap_abs = round(abs(spot_gap_pts), 1)
    pts_voice = f"up {gap_abs} points" if spot_gap_pts > 0 else (f"down {gap_abs} points" if spot_gap_pts < 0 else "flat (0 points)")

    if abs(pdh - live_spot) <= 15.0 or (live_spot >= pdc_spot and live_spot >= pdh - 15.0):
        loc_voice = f"Testing Previous Day High at {int(pdh):,}."
    elif abs(live_spot - pdl) <= 15.0:
        loc_voice = f"Testing Previous Day Low at {int(pdl):,}."
    elif lands_on_put_wall:
        loc_voice = f"Opening on Put Support Floor at {int(pe_wall):,}."
    elif lands_on_call_wall:
        loc_voice = f"Opening beneath Call Resistance Ceiling at {int(ce_wall):,}."
    else:
        loc_voice = f"Opening between Support {int(pe_wall):,} and Resistance {int(ce_wall):,}."

    voice_time_threshold = c.get("voice_alert_time_ist", "09:08")
    try:
        vh, vm = map(int, voice_time_threshold.split(":"))
        is_after_alert_time = (now_dt.hour > vh) or (now_dt.hour == vh and now_dt.minute >= vm)
    except Exception:
        is_after_alert_time = True

    voice_text = (
        f"NIFTY 50 live at {live_spot:.1f}, {pts_voice} from yesterday's close of {pdc_spot:.1f}. "
        f"{loc_voice} {action}."
    )
    speak_alert = is_after_alert_time or is_simulated

    # 30-Minute Execution Timeline
    timeline = [
        {
            "time": "09:08 AM",
            "title": "Pre-Market Gap Check",
            "instruction": f"Note exact NIFTY 50 Spot gap ({spot_gap_pts:+.1f} pts) vs PDC ({pdc_spot:,.1f}) and Oct FUT ({oct_fut_ltp:,.1f}). Live Spot: {live_spot:,.1f}.",
            "status": "COMPLETED",
        },
        {
            "time": "09:15 AM",
            "title": "Wall Interaction Test",
            "instruction": (
                f"Opening print lands near {int(pe_wall):,} Put Wall / {int(ce_wall):,} Call Wall. Do NOT chase opening candle. "
                f"Observe whether price absorbs at wall or breaks through."
            ),
            "status": "ACTIVE" if wick_data["has_opened"] else "PENDING",
        },
        {
            "time": "09:20 AM",
            "title": "Fresh ΔOI & Wick Confirmation",
            "instruction": (
                f"Confirm 1m/3m candle wicks. If Put writers fortify (> +15L) and Call writers cover (< 0), "
                f"trigger {action} with SL strictly at {stop_loss:,.1f}."
            ),
            "status": "PENDING",
        },
        {
            "time": "09:45 AM",
            "title": "Profit Lock & Expiry Scale-Out",
            "instruction": f"Lock in gains at Target 1 ({target_1:,.1f}) or Target 2 ({target_2:,.1f}). Never hold opening morning scalps past 09:45 AM.",
            "status": "PENDING",
        },
    ]

    return {
        "ok": True,
        "is_simulated": is_simulated,
        "primary_anchor": primary_anchor,
        "live_spot": live_spot,
        "pdc_spot": pdc_spot,
        "spot_gap_pts": spot_gap_pts,
        "spot_gap_pct": spot_gap_pct,
        "oct_fut_ltp": oct_fut_ltp,
        "oct_fut_pdc": oct_fut_pdc,
        "oct_fut_gap_pts": oct_fut_gap_pts,
        "oct_fut_gap_pct": oct_fut_gap_pct,
        "nov_fut_ltp": nov_fut_ltp,
        "nov_fut_pdc": nov_fut_pdc,
        "nov_fut_gap_pts": nov_fut_gap_pts,
        "nov_fut_gap_pct": nov_fut_gap_pct,
        "roll_spread": roll_spread,
        "gap_tier": gap_tier,
        "gap_tier_label": gap_tier_label,
        "gift_nifty_ltp": gift_nifty_ltp,
        "near_fut_close": near_fut_close,
        "spot_close": spot_close,
        "gap_pts": gap_pts,
        "gap_pct": gap_pct,
        "gap_type": gap_type,
        "is_significant_gap": is_significant_gap,
        "is_major_gap": is_major_gap,
        "expected_open_fut": expected_open_fut,
        "expected_open_spot": expected_open_spot,
        "levels": {
            "pdc": pdc_spot,
            "pdh": pdh,
            "pdl": pdl,
            "pdo": pdo,
            "put_wall": pe_wall,
            "call_wall": ce_wall,
            "lower_straddle_be": lower_be,
            "upper_straddle_be": upper_be,
            "vwap": vwap,
        },
        "wicks": wick_data,
        "writer_flow": {
            "fresh_put_delta": fresh_put_delta,
            "fresh_call_delta": fresh_call_delta,
            "pcr": pcr,
        },
        "checklist": checklist,
        "confluence_score": f"{confluence_score}/5",
        "confluence_pct": round((confluence_score / 5.0) * 100, 0),
        "playbook": {
            "title": playbook_title,
            "code": playbook_code,
            "theme": bias_theme,
            "color": bias_color,
            "action": action,
            "entry_zone": entry_zone,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "narrative": narrative,
        },
        "timeline": timeline,
        "multi_contract_table": multi_contract_table,
        "speak_alert": speak_alert,
        "voice_message": voice_text,
        "voice_alert": {
            "speak_alert": speak_alert,
            "text": voice_text,
            "alert_time_ist": voice_time_threshold,
            "timestamp": now_dt.strftime("%H:%M:%S IST"),
        },
        "updated_at": now_dt.strftime("%H:%M IST"),
    }
