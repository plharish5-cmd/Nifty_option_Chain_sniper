"""Display + audit helpers for 8 EMA live vs 15m close (no EMA maths changes)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

EMA_CONVERGENCE_NOTE = (
    "Live vs close differ mid-candle by design; they converge at candle close."
)


def spot_quote_stale(spot_blob: Optional[Dict[str, Any]], *, max_age_sec: float = 5.0) -> Tuple[bool, Optional[float]]:
    """True when spot must not drive touch logic (fail-open)."""
    if not isinstance(spot_blob, dict):
        return True, None
    if spot_blob.get("is_stale"):
        try:
            ue = float(spot_blob.get("updated_epoch") or 0.0)
            age = time.time() - ue if ue > 0 else None
        except (TypeError, ValueError):
            age = None
        return True, age
    try:
        ue = float(spot_blob.get("updated_epoch") or 0.0)
    except (TypeError, ValueError):
        return True, None
    if ue <= 0:
        return True, None
    age = time.time() - ue
    return age > float(max_age_sec), age


def format_ema_touch_audit(
    *,
    e8_live: float,
    spot: float,
    e8_closed: float,
    at_hm: Optional[str] = None,
) -> str:
    at = at_hm or datetime.now(IST).strftime("%H:%M:%S")
    return (
        f"8EMA {float(e8_live):.1f} @ {at} from NIFTY spot {float(spot):.1f}, "
        f"15m close EMA {float(e8_closed):.1f}"
    )


def enrich_prototype_view(
    base: Dict[str, Any],
    *,
    e8_live: Optional[float],
    e8_closed: Optional[float],
    ema_stale: bool,
) -> Dict[str, Any]:
    out = dict(base)
    out["ema8_live"] = round(float(e8_live), 1) if e8_live is not None else None
    out["ema8_closed"] = round(float(e8_closed), 1) if e8_closed is not None else None
    out["ema8"] = out["ema8_live"] if out["ema8_live"] is not None else base.get("ema8")
    out["ema_stale"] = bool(ema_stale)
    out["ema_note"] = EMA_CONVERGENCE_NOTE if not ema_stale else "Spot feed stale — touch logic paused (fail-open)."
    return out