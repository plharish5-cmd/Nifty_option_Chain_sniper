"""Price / INDIA VIX divergence — a CONTEXT read, deliberately not a trade trigger.

WHAT IT KNOWS THAT PRICE DOES NOT
Every momentum oscillator (RSI, MACD, Stochastic, CCI) is derived from price, so stacking them gives
several confirmations of one opinion rather than confluence. VIX is genuinely independent: it is what
people are paying for protection.

The normal relationship is inverse. Price up, VIX down: a rally nobody feels the need to hedge.
The informative case is the broken relationship:

  DISTRIBUTION_WARNING   price makes a higher high AND VIX rises with it.
                         Someone is paying up for protection into strength. One of the few honest
                         tells that a move is being sold into rather than bought.
  CAPITULATION_WATCH     price makes a lower low AND VIX falls.
                         Fear is draining out of a decline; sellers are finishing.
  NORMAL_RISK_ON/OFF     the inverse relationship is intact -- nothing to say.

WHY IT RETURNS A STATE, NOT A SIGNAL
A VIX divergence has no entry, no stop and no invalidation level; it says the character of a move has
changed, over hours. Dressing that as a ticket would be exactly the error that cost money on 29 Oct,
where five correct observations became five wrong trades. It raises or lowers confidence in a trend
the other engines are already reading. It never fires an order.

UNAVAILABLE is a first-class answer. If the VIX series is missing or too thin, this says so and
returns no state -- it never falls back to a stale or assumed value.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import config

NORMAL_RISK_ON = "NORMAL_RISK_ON"
NORMAL_RISK_OFF = "NORMAL_RISK_OFF"
DISTRIBUTION_WARNING = "DISTRIBUTION_WARNING"
CAPITULATION_WATCH = "CAPITULATION_WATCH"
FLAT = "FLAT"
UNAVAILABLE = "UNAVAILABLE"


def get_cfg() -> Dict[str, Any]:
    return dict(config.rules().get("vix_divergence", {}))


def _series_at(vix_bars: List[Dict[str, Any]], ts: int, tol_s: int) -> Optional[float]:
    """VIX close at or just before `ts`. Never interpolates: a value that was not observed is None."""
    best: Optional[float] = None
    best_dt = None
    for b in vix_bars:
        t = int(b.get("ts", 0))
        if t > ts:
            continue
        dt = ts - t
        if dt <= tol_s and (best_dt is None or dt < best_dt):
            best, best_dt = float(b.get("c", b.get("close", 0.0))), dt
    return best


def compute_vix_divergence(
    price_bars: List[Dict[str, Any]],
    vix_bars: List[Dict[str, Any]],
    lookback_bars: int = 20,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compare the move in price with the move in VIX over `lookback_bars` closed bars."""
    c = {**{"min_price_move_pts": 25.0, "min_vix_move_pct": 1.5, "match_tolerance_s": 300,
            "lookback_bars": 20, "enabled": True}, **(cfg if cfg is not None else get_cfg())}
    lookback = int(c.get("lookback_bars", lookback_bars) or lookback_bars)

    pb = [b for b in sorted(price_bars or [], key=lambda x: int(x["ts"]))
          if b.get("closed") is not False]
    # The VIX series needs the SAME closed-bar filter as price. Without it a forming candle's ticks
    # reach vix_to, so the reported VIX move -- and therefore DISTRIBUTION_WARNING -- can flip back
    # and forth within a single bar and then settle somewhere else at the close. Context that
    # changes under the trader mid-bar is worse than no context.
    vb = [b for b in sorted(vix_bars or [], key=lambda x: int(x["ts"]))
          if b.get("closed") is not False]
    out: Dict[str, Any] = {
        "ok": False, "state": UNAVAILABLE, "label": "⚠️ VIX unavailable",
        "available": False, "price_move_pts": None, "vix_move_pct": None,
        "price_from": None, "price_to": None, "vix_from": None, "vix_to": None,
        "detail": "", "is_context_only": True,
    }
    if len(pb) < 2:
        out["detail"] = "not enough closed price bars"
        return out
    if not vb:
        out["detail"] = ("No INDIA VIX series available, so no price/VIX relationship can be read. "
                         "Reporting unavailable rather than assuming one.")
        return out

    window = pb[-lookback:] if len(pb) > lookback else pb
    t0, t1 = int(window[0]["ts"]), int(window[-1]["ts"])
    tol = int(c["match_tolerance_s"])
    v0, v1 = _series_at(vb, t0, tol), _series_at(vb, t1, tol)
    if v0 is None or v1 is None or v0 <= 0:
        out["detail"] = (f"VIX has no observation within {tol}s of the window edges "
                         f"({'start' if v0 is None else 'end'} missing). Reporting unavailable.")
        return out

    p0, p1 = float(window[0]["c"]), float(window[-1]["c"])
    dp = p1 - p0
    dv_pct = (v1 - v0) / v0 * 100.0

    out.update(ok=True, available=True,
               price_from=round(p0, 2), price_to=round(p1, 2),
               vix_from=round(v0, 2), vix_to=round(v1, 2),
               price_move_pts=round(dp, 1), vix_move_pct=round(dv_pct, 2))

    min_p, min_v = float(c["min_price_move_pts"]), float(c["min_vix_move_pct"])
    big_price, big_vix = abs(dp) >= min_p, abs(dv_pct) >= min_v

    if not big_price:
        out.update(state=FLAT, label="⚖️ Flat — no VIX read",
                   detail=f"Price moved only {dp:+.1f} pts over the window (needs {min_p:.0f}).")
        return out

    if dp > 0 and big_vix and dv_pct > 0:
        out.update(state=DISTRIBUTION_WARNING, label="⚠️ VIX RISING INTO STRENGTH",
                   detail=(f"Price +{dp:.0f} pts while VIX also rose {dv_pct:+.1f}% "
                           f"({v0:.2f} → {v1:.2f}). Protection is being bought into the rally — the "
                           f"move is being hedged, not simply chased. Lower confidence in continuation; "
                           f"this is context, not an entry."))
    elif dp < 0 and big_vix and dv_pct < 0:
        out.update(state=CAPITULATION_WATCH, label="🟢 VIX FALLING INTO WEAKNESS",
                   detail=(f"Price {dp:.0f} pts while VIX fell {dv_pct:+.1f}% "
                           f"({v0:.2f} → {v1:.2f}). Fear is draining out of the decline — sellers "
                           f"finishing rather than starting. Context, not an entry."))
    elif dp > 0:
        out.update(state=NORMAL_RISK_ON, label="🟢 Normal risk-on",
                   detail=(f"Price +{dp:.0f} pts with VIX {dv_pct:+.1f}% — the usual inverse "
                           f"relationship. Nothing unusual in the hedging."))
    else:
        out.update(state=NORMAL_RISK_OFF, label="🔴 Normal risk-off",
                   detail=(f"Price {dp:.0f} pts with VIX {dv_pct:+.1f}% — the usual inverse "
                           f"relationship for a decline."))
    return out
