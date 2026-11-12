"""Plain-language views for the cockpit. Display only: nothing here decides a trade."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

IST = timezone(timedelta(hours=5, minutes=30))

PROTO_STEPS = ["15m close crosses 8 EMA", "Armed", "Price moves away", "Touch of 8 EMA → entry", "In trade"]


def prototype_view(row: Optional[Dict[str, Any]], proto_state: Optional[Dict[str, Any]], spot: Optional[float],
                   live_route: str = "") -> Dict[str, Any]:
    """Where the 8 EMA Prototype is in its sequence, in words.

    step 0..4 indexes PROTO_STEPS (the step being waited for; 4 = in trade).
    """
    row = row or {}
    st = proto_state or {}
    arm = st.get("arm") or {}
    opened = st.get("open") or {}
    side = lambda d: "CE" if str(d).upper() == "LONG" else "PE"
    paper_lane = bool(row.get("paper_only_lane"))
    if paper_lane:
        route = "Paper only — this lane never sends a real order"
    elif str(live_route).upper() == "LIVE":
        route = "REAL ORDERS — desk is armed live"
    else:
        route = "Paper now — real orders when the desk is armed live"
    e8 = row.get("dynamic_e8")
    out: Dict[str, Any] = {
        "steps": PROTO_STEPS, "step": 0, "tone": "wait", "route": route, "route_live": (not paper_lane and str(live_route).upper() == "LIVE"),
        "headline": "Waiting for a 15-minute candle to close across the 8 EMA",
        "sub": "", "ema8": e8, "spot": spot,
        "gap_pts": None if (e8 is None or not spot) else round(float(spot) - float(e8), 1),
    }
    if row.get("enabled") is False or str(row.get("state")) == "PAUSED":
        out.update(step=-1, tone="off", headline="Switched off", sub="Turn Master Control to ACTIVE to run this lane")
        return out
    if opened:
        s = side(opened.get("direction"))
        out.update(step=4, tone="ce" if s == "CE" else "pe", headline=f"In a {s} trade",
                   sub="Exit: +40 / −40 spot or option points, an opposite 15-minute close, or 15:20")
        return out
    blocked = str(st.get("sl_blocked_side") or "")
    if arm:
        s = side(arm.get("direction"))
        try:
            since = datetime.fromtimestamp(int(arm.get("cross_bar_open_ts")) + 900, IST).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            since = ""
        away = bool(arm.get("away_seen"))
        tol = row.get("effective_touch_tol_pts", row.get("touch_tol_pts"))
        out.update(
            step=3 if away else 2, tone="ce" if s == "CE" else "pe",
            headline=(f"Armed {s} — waiting for price to come back and touch the 8 EMA" if away
                      else f"Armed {s} — waiting for price to move away from the 8 EMA"),
            sub=(f"armed at {since}" if since else "") + (f" · touch within ±{float(tol):g} pts" if tol is not None and away else ""),
        )
    elif blocked:
        s = side(blocked)
        other = "PE" if s == "CE" else "CE"
        out.update(headline=f"{s} side cancelled after a stop — waiting for a 15-minute close that arms {other}")
    hold = str(row.get("block_reason") or "")
    if hold:
        out["sub"] = (out["sub"] + " · " if out["sub"] else "") + hold
    return out
