"""api/oi_walls_replay_api.py — replay the OI walls minute by minute for any stored session.

WHY
chain_snap has recorded per-strike CE/PE OI every 60 seconds all along, and
engine.itm_atm_oi_walls.compute_itm_atm_oi_walls() is a pure function of (spot, chain_rows, pcr).
Together that means the walls at any past minute can be rebuilt exactly as they were — which turns
every stored minute into a test case for an engine whose signals are otherwise ungradeable.

SPEED, WHICH IS THE POINT
A previous attempt at this was unusably slow. The design here is built around that:

  * ONE request per day, not one per slider tick. The whole session is computed server-side and
    returned as an array of frames, so dragging the slider is pure client-side indexing with no
    network at all.
  * ONE SQL query for the day's ~28,000 rows, grouped in memory — not 375 queries.
  * An LRU cache keyed (day, expiry). Re-opening a day, or switching tabs and back, is instant.
  * Nothing is computed until the replay tab actually asks. The live desk never pays for this.

NO LOOK-AHEAD
Each frame sees only candles at or before its own timestamp. A replay that leaks future bars would
flatter every signal it draws, which is the exact error that produced a false +3.3 sd in this repo
on 2026-11-01.

DERIVED LEVELS, NOT CONFIGURED ONES
analyst_config.json pins put_support_bottom=23200, iron ceiling 23550-23623 and so on — one day's
numbers. Replaying October with a fixed 23,220 panic trigger would be nonsense, so each frame
derives its support zone from the snapshot it is replaying and passes it in as an override.

READ-ONLY. Queries SQLite, computes, returns. Writes nothing, calls no external API.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..engine.itm_atm_oi_walls import compute_itm_atm_oi_walls
from ..timeutil import IST

log = logging.getLogger(__name__)
router = APIRouter()

_CACHE: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
_CACHE_MAX = 8
_RSI_LOOKBACK = 120          # 1-minute bars handed to the divergence check, plenty for RSI(14)


def _day_bounds(day: str) -> Tuple[int, int]:
    d = datetime.fromisoformat(day).replace(tzinfo=IST)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def _derive_levels(strikes: Dict[float, Dict[str, float]], spot: float) -> Dict[str, float]:
    """Support zone from THIS snapshot's heaviest put cluster, not from a config constant.

    The heaviest PE strike at or below spot is the floor the market is actually leaning on. The
    zone is that strike up to the next PE strike above it that also carries real OI.
    """
    below = {k: v for k, v in strikes.items() if k <= spot and (v.get("pe_oi") or 0) > 0}
    if not below:
        return {}
    bottom = max(below, key=lambda k: below[k]["pe_oi"])
    heavy = below[bottom]["pe_oi"] * 0.5
    above = sorted(k for k, v in strikes.items() if k > bottom and (v.get("pe_oi") or 0) >= heavy)
    top = above[0] if above else bottom + 50.0
    # Next meaningful shelf below the floor, used as the downside projection once it breaks.
    lower = sorted((k for k, v in strikes.items() if k < bottom and (v.get("pe_oi") or 0) >= heavy),
                   reverse=True)
    return {"support_bottom": bottom, "support_top": top,
            "structural_downside": lower[0] if lower else bottom - 100.0}


def _build_day(day: str, expiry: str) -> Dict[str, Any]:
    lo, hi = _day_bounds(day)

    # One query for the whole session. 375 separate queries is what made this slow before.
    rows = db.query(
        "SELECT ts, strike, ce_oi, pe_oi FROM chain_snap "
        "WHERE expiry=? AND ts>=? AND ts<? ORDER BY ts, strike", (expiry, lo, hi))
    if not rows:
        return {"ok": False, "day": day, "expiry": expiry, "frames": [],
                "message": f"no chain snapshots stored for {expiry} on {day}"}

    by_ts: "defaultdict[int, Dict[float, Dict[str, float]]]" = defaultdict(dict)
    for r in rows:
        by_ts[int(r["ts"])][float(r["strike"])] = {
            "ce_oi": float(r["ce_oi"] or 0.0), "pe_oi": float(r["pe_oi"] or 0.0)}
    stamps = sorted(by_ts)

    candles = db.candles_1m("13", lo, hi)
    spot_at = {int(c["ts"]): float(c["c"]) for c in candles}

    # Baseline for oi_chg: the day's FIRST snapshot. Live, Dhan reports change against the
    # previous close, which is not stored; day-open is the closest available proxy. Frames are
    # therefore directionally faithful but will not match a remembered live reading exactly.
    base = by_ts[stamps[0]]

    frames: List[Dict[str, Any]] = []
    for ts in stamps:
        cur = by_ts[ts]
        spot = spot_at.get(ts)
        if spot is None:                                  # nearest candle at or before this snap
            earlier = [t for t in spot_at if t <= ts]
            spot = spot_at[max(earlier)] if earlier else None
        if spot is None:
            continue

        chain_rows = []
        for stk, v in sorted(cur.items()):
            b = base.get(stk, v)
            chain_rows.append({
                "strike": stk,
                "ce": {"oi": v["ce_oi"], "oi_chg": v["ce_oi"] - b["ce_oi"]},
                "pe": {"oi": v["pe_oi"], "oi_chg": v["pe_oi"] - b["pe_oi"]},
            })

        ce_tot = sum(v["ce_oi"] for v in cur.values())
        pe_tot = sum(v["pe_oi"] for v in cur.values())
        pcr = round(pe_tot / ce_tot, 2) if ce_tot else None
        lv = _derive_levels(cur, spot)
        past = [c for c in candles if c["ts"] <= ts][-_RSI_LOOKBACK:]   # never the future

        try:
            w = compute_itm_atm_oi_walls(
                spot=spot, chain_rows=chain_rows, pcr=pcr,
                candles_spot=past, candles_fut=None,
                support_top_override=lv.get("support_top"),
                support_bottom_override=lv.get("support_bottom"),
                structural_downside_override=lv.get("structural_downside"),
            )
        except Exception as e:                            # one bad minute must not lose the day
            log.warning("oi-walls replay: frame %s failed: %s", ts, e)
            continue

        frames.append({
            "ts": ts,
            "time": datetime.fromtimestamp(ts, IST).strftime("%H:%M"),
            "spot": round(spot, 2),
            "pcr": pcr,
            "derived_levels": lv,
            "walls": w,
        })

    return {"ok": True, "day": day, "expiry": expiry, "count": len(frames), "frames": frames,
            "oi_chg_basis": "change since the first snapshot of the day (Dhan's previous-close "
                            "baseline is not stored, so live and replay deltas differ slightly)"}


@router.get("/api/desk/oi-walls-replay/days")
def replay_days(limit: int = Query(60, ge=1, le=400)):
    """Sessions that can be replayed, newest first, with the expiries stored for each."""
    rows = db.query(
        "SELECT date(ts,'unixepoch','+330 minutes') AS d, expiry, COUNT(DISTINCT ts) AS snaps "
        "FROM chain_snap GROUP BY d, expiry ORDER BY d DESC, expiry")
    out: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for r in rows:
        day = out.setdefault(r["d"], {"day": r["d"], "expiries": []})
        day["expiries"].append({"expiry": r["expiry"], "snapshots": int(r["snaps"])})
    return {"ok": True, "days": list(out.values())[:limit]}


@router.get("/api/desk/oi-walls-replay")
def replay(day: str = Query(..., description="session date, YYYY-MM-DD"),
           expiry: Optional[str] = Query(None, description="defaults to the day's busiest expiry")):
    """Every minute of one session's OI walls, in one response. The slider is then local."""
    try:
        datetime.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")

    if not expiry:
        lo, hi = _day_bounds(day)
        r = db.query("SELECT expiry, COUNT(*) n FROM chain_snap WHERE ts>=? AND ts<? "
                     "GROUP BY expiry ORDER BY n DESC LIMIT 1", (lo, hi))
        if not r:
            return {"ok": False, "day": day, "frames": [],
                    "message": f"no chain snapshots stored for {day}"}
        expiry = r[0]["expiry"]

    key = (day, expiry)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return {**hit, "cached": True}

    t0 = time.time()
    res = _build_day(day, expiry)
    res["build_ms"] = int((time.time() - t0) * 1000)
    _CACHE[key] = res
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return {**res, "cached": False}
