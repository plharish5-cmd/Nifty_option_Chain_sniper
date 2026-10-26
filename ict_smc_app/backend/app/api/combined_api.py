"""
api/combined_api.py — Oct / Nov / combined NIFTY-future volume. Read-only & additive.
The Nov leg is REST-only (recorded under FUT2 by hub.setup_second_future), so it costs no live connection
and can never touch the feed. Foundation for Oct+Nov RVOL / big-trades / heatmap aggregation. Display only.
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query

from ..engine import rvol as rvol_mod
from ..market import futures as fut_mod

router = APIRouter()


def _crvol(series):
    """Cumulative RVOL (reusing the real engine) on any 1m volume series. Returns {value, state} or None."""
    try:
        st = rvol_mod.state(rvol_mod.cumulative(series))
        return {"value": st.get("value"), "state": st.get("state"), "trend": st.get("trend")}
    except Exception:                                       # noqa: BLE001
        return None


@router.get("/api/fut_candles")
def fut_candles(
    tf: str = Query("3m"),
    days: int = Query(3),
    src: str = Query("oct"),
    fut_contract: Optional[str] = Query(None),
    fut_src: Optional[str] = Query(None),
):
    """Raw NIFTY-FUTURE OHLC candles (the active Oct contract, or next-month Nov, or combined) resampled to `tf`, on the same
    IST time axis as the writer-flow chart — so the real future candles can overlay the Call/Put OI lines.
    Adapts automatically across expiry rollover (the active future is whatever the hub currently tracks)."""
    from ..market import candles as candles_mod
    from ..timeutil import chart_time
    from .main import _h
    try:
        req_src = fut_contract or fut_src or src
        if req_src in ("near", "oct"):
            raw = fut_mod.rows_1m(days)
            bars = candles_mod.resample(candles_mod.session_filter(raw), tf) if raw else []
        elif req_src in ("next", "nov"):
            raw = fut_mod.sym_rows_1m(fut_mod.SEC2, days)
            bars = candles_mod.resample(candles_mod.session_filter(raw), tf) if raw else []
        else:  # combined
            h = _h()
            spot1m = h.rows_1m(days) if hasattr(h, "rows_1m") else []
            oct_m = fut_mod.mapped(spot1m, fut_mod.rows_1m(days)) if spot1m else fut_mod.rows_1m(days)
            nov_raw = fut_mod.sym_rows_1m(fut_mod.SEC2, days)
            nov_m = fut_mod.mapped(spot1m, nov_raw) if (spot1m and nov_raw) else (nov_raw or [])
            combined_rows = fut_mod.combined_1m(oct_m, nov_m)
            bars = candles_mod.resample(candles_mod.session_filter(combined_rows), tf) if combined_rows else []

        return {
            "available": True,
            "tf": tf,
            "src": req_src,
            "candles": [
                {
                    "time": chart_time(int(r["ts"])),
                    "o": round(float(r["o"]), 1),
                    "h": round(float(r["h"]), 1),
                    "l": round(float(r["l"]), 1),
                    "c": round(float(r["c"]), 1),
                    "v": round(float(r.get("v", 0))),
                }
                for r in bars
            ],
        }
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "candles": []}


@router.get("/api/combined_vol")
def combined_vol(src: str = Query("combined"), heat: int = Query(0)):
    from .main import _h
    h = _h()
    try:
        # cRVOL needs a baseline of EARLIER sessions (≥3 for 5-day, ≥10 for 20-day),
        # so pull ~30 calendar days, not 3. Nov leg is REST-only and short-lived — that's fine:
        # on days before the Nov recorder started it simply contributes 0, so combined == Oct there.
        spot1m = h.rows_1m(30)
        oct_m = fut_mod.mapped(spot1m, fut_mod.rows_1m(30))
        nov_raw = fut_mod.sym_rows_1m(fut_mod.SEC2, 30)
        nov_m = fut_mod.mapped(spot1m, nov_raw) if nov_raw else []
        series = oct_m if src == "oct" else nov_m if src == "nov" else fut_mod.combined_1m(oct_m, nov_m)
        today = max((int(r["ts"]) // 86400 for r in series), default=0)
        oct_today = sum(float(r.get("v") or 0) for r in oct_m if int(r["ts"]) // 86400 == today)
        nov_today = sum(float(r.get("v") or 0) for r in nov_m if int(r["ts"]) // 86400 == today)
        tot = oct_today + nov_today
        combined_m = fut_mod.combined_1m(oct_m, nov_m)
        series_today = None
        if heat:
            series_today = [{"ts": int(r["ts"]), "c": round(r["c"], 1), "v": round(float(r.get("v") or 0))}
                            for r in series if int(r["ts"]) // 86400 == today]
        return {"available": True, "src": src, "fut2_status": getattr(h, "fut2_status", "?"), "series_today": series_today,
                "oct_minutes": len(oct_m), "nov_minutes": len(nov_m),
                "oct_vol_today": round(oct_today), "nov_vol_today": round(nov_today), "combined_vol_today": round(tot),
                "nov_share_pct": round(100 * nov_today / tot, 1) if tot else 0,
                "crvol_oct": _crvol(oct_m), "crvol_nov": _crvol(nov_m), "crvol_combined": _crvol(combined_m),   # cumulative RVOL per source
                "last": [{"ts": int(r["ts"]), "c": round(r["c"], 1), "v": round(float(r.get("v") or 0))} for r in series[-10:]]}
    except Exception as e:                                   # noqa: BLE001
        return {"available": False, "error": str(e)[:200], "fut2_status": getattr(h, "fut2_status", "?")}
