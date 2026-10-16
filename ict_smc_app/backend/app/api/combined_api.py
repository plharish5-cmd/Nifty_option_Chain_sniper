"""
api/combined_api.py — Oct / Nov / combined NIFTY-future volume. Read-only & additive.
The Nov leg is REST-only (recorded under FUT2 by hub.setup_second_future), so it costs no live connection
and can never touch the feed. Foundation for Oct+Nov RVOL / big-trades / heatmap aggregation. Display only.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..market import futures as fut_mod

router = APIRouter()


@router.get("/api/combined_vol")
def combined_vol(src: str = Query("combined")):
    from .main import _h
    h = _h()
    try:
        spot1m = h.rows_1m(3)
        oct_m = fut_mod.mapped(spot1m, fut_mod.rows_1m(3))
        nov_raw = fut_mod.sym_rows_1m(fut_mod.SEC2, 3)
        nov_m = fut_mod.mapped(spot1m, nov_raw) if nov_raw else []
        series = oct_m if src == "oct" else nov_m if src == "nov" else fut_mod.combined_1m(oct_m, nov_m)
        today = max((int(r["ts"]) // 86400 for r in series), default=0)
        oct_today = sum(float(r.get("v") or 0) for r in oct_m if int(r["ts"]) // 86400 == today)
        nov_today = sum(float(r.get("v") or 0) for r in nov_m if int(r["ts"]) // 86400 == today)
        tot = oct_today + nov_today
        return {"available": True, "src": src, "fut2_status": getattr(h, "fut2_status", "?"),
                "oct_minutes": len(oct_m), "nov_minutes": len(nov_m),
                "oct_vol_today": round(oct_today), "nov_vol_today": round(nov_today), "combined_vol_today": round(tot),
                "nov_share_pct": round(100 * nov_today / tot, 1) if tot else 0,
                "last": [{"ts": int(r["ts"]), "c": round(r["c"], 1), "v": round(float(r.get("v") or 0))} for r in series[-10:]]}
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "fut2_status": getattr(h, "fut2_status", "?")}
