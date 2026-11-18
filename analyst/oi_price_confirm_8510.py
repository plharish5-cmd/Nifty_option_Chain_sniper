"""Price confirmation for OI momentum S gate (writers + price agree). Pure functions only."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

Bar = Dict[str, float]


def slice_spot_window(
    bars: Sequence[Bar],
    *,
    now_ts: int,
    window_min: int,
) -> List[Bar]:
    """1m bars with ts in (now_ts - window_min*60, now_ts], sorted ascending."""
    if not bars or window_min <= 0:
        return []
    start = int(now_ts) - int(window_min) * 60
    out = [
        b
        for b in bars
        if int(b.get("ts") or 0) > start and int(b.get("ts") or 0) <= int(now_ts)
    ]
    out.sort(key=lambda x: int(x.get("ts") or 0))
    return out


def load_spot_1m_bars_for_day(
    market_db: Any,
    *,
    day: str,
    now_ts: int,
    window_min: int,
    sec_id: str = "13",
) -> List[Bar]:
    """Best-effort 1m NIFTY spot from the recorded market DB — never raises."""
    import sqlite3
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    try:
        path = Path(market_db) if market_db else None
        if not path or not path.exists():
            return []
        start = int(now_ts) - max(int(window_min), 1) * 60 - 120
        end = int(now_ts)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3) as conn:
            rows = conn.execute(
                "SELECT ts, o, h, l, c FROM candles_1m WHERE sec_id=? AND ts>=? AND ts<=? ORDER BY ts",
                (sec_id, start, end),
            ).fetchall()
        out: List[Bar] = []
        for ts, o, h, l, c in rows:
            dt = datetime.fromtimestamp(int(ts), tz=IST)
            if dt.strftime("%Y-%m-%d") != day:
                continue
            out.append({"ts": float(ts), "open": float(o), "high": float(h), "low": float(l), "close": float(c)})
        return slice_spot_window(out, now_ts=now_ts, window_min=window_min)
    except Exception:
        return []


def price_agrees_with_momentum(
    direction: str,
    window_bars: Sequence[Bar],
) -> Tuple[Optional[bool], str]:
    """
    CE: higher close vs window start + recent swing high rising.
    PE: lower close + recent swing low falling.
    Returns (None, reason) when data is insufficient — fail OPEN (do not downgrade).
    """
    d = str(direction or "").upper()
    if d not in ("CE", "PE"):
        return None, "n/a"
    if len(window_bars) < 2:
        return None, "insufficient spot"
    closes = [float(b["close"]) for b in window_bars]
    highs = [float(b["high"]) for b in window_bars]
    lows = [float(b["low"]) for b in window_bars]
    c0, cn = closes[0], closes[-1]
    if d == "CE":
        swing_high_rising = highs[-1] > max(highs[:-1])
        agrees = cn > c0 and swing_high_rising
    else:
        swing_low_falling = lows[-1] < min(lows[:-1])
        agrees = cn < c0 and swing_low_falling
    return agrees, ("agrees" if agrees else "diverges")


def apply_price_confirm_to_momentum_read(
    item: Dict[str, Any],
    *,
    require: bool,
    window_bars: Sequence[Bar],
) -> Dict[str, Any]:
    """Downgrade CE/PE → NEUTRAL when require=True and price diverges; never raises."""
    out = dict(item)
    if not require:
        out["price_confirm"] = None
        return out
    direction = str(out.get("direction") or "").upper()
    agrees, label = price_agrees_with_momentum(direction, window_bars)
    out["price_confirm"] = label if agrees is not None else None
    if direction in ("CE", "PE") and agrees is False:
        out["direction"] = "NEUTRAL"
        out["strength"] = "NEUTRAL"
        prev = str(out.get("detail") or "")
        out["detail"] = (prev + f" · price: {label} → NEUTRAL").strip()
    elif direction in ("CE", "PE") and agrees is True:
        prev = str(out.get("detail") or "")
        if "price:" not in prev:
            out["detail"] = (prev + f" · price: {label}").strip()
    return out