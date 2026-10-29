"""Causal, alert-only RSI / price divergence with a confirmation gate and a regime veto.

WHY THIS EXISTS, AND WHY IT IS SHAPED LIKE cvd_divergence.py
------------------------------------------------------------
On 29 Oct the desk's OI-based divergence printed "bearish divergence — price new high, writers NOT
following" five times (09:33, 09:39, 10:00, 11:21, 13:15) on a session that ran 22,720 -> 22,953.
All five were wrong, and the 09:33 one was ranked A+ 95% and became a short that stopped out in 21
minutes. That is not a broken indicator -- in an uptrend price makes higher highs on falling momentum
constantly, because each push needs less force once sellers have given up. It is what a trend looks
like from inside an oscillator.

The same day, cvd_divergence.py fired twice, armed a trigger level, waited, and ended INVALIDATED and
EXPIRED. Zero bad trades. The whole difference is that it treats a divergence as a WATCH and demands
a price-action close through a level before it becomes anything. So this engine deliberately copies
that contract rather than inventing a second one:

    ARMED -> STRUCTURE_CONFIRMED            (price closed through the trigger)
    ARMED -> INVALIDATED                    (price ran past the divergence pivot instead)
    ARMED -> EXPIRED                        (nothing happened within expiry_bars)

Nothing is ever emitted as an instruction while merely ARMED.

REGULAR vs HIDDEN
Regular divergence (price HH / RSI LH) suggests a reversal. Hidden divergence (price HL / RSI LL)
suggests continuation, and is the one that matters on a trend day. Both are detected; hidden is
reported as continuation and never as a reversal watch.

Alert-only. Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import config
from ..timeutil import hm

TERMINAL = {"EXPIRED", "INVALIDATED"}
BAR_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}


def get_cfg() -> Dict[str, Any]:
    return dict(config.rules().get("rsi_divergence", {}))


# ── RSI ───────────────────────────────────────────────────────────────────────────────────────────

def wilder_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI. Returns one value per close, None until `period` deltas exist.

    Wilder smoothing (alpha = 1/period), NOT a simple moving average of gains -- the SMA variant is a
    common mistake and gives visibly different values, so a divergence detected on one can be absent
    on the other.
    """
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


# ── pivots ────────────────────────────────────────────────────────────────────────────────────────

def find_pivots(bars: List[Dict[str, Any]], window: int) -> Tuple[List[int], List[int]]:
    """Confirmed swing highs and lows: a bar whose high (low) is the extreme of +/- `window` bars.

    Pivot-based, never bar-to-bar. Comparing consecutive bars produces a divergence on almost every
    push and is the main source of false signals. A pivot also cannot be known until `window` bars
    after it, which is why this is causal: the caller must not treat a pivot as available early.
    """
    highs: List[int] = []
    lows: List[int] = []
    n = len(bars)
    for i in range(window, n - window):
        h, l = float(bars[i]["h"]), float(bars[i]["l"])
        left = bars[i - window:i]
        right = bars[i + 1:i + window + 1]
        # Strict on the left, inclusive on the right: on a plateau of equal highs this marks exactly
        # one pivot (the first), instead of one per bar in the plateau.
        if h > max(float(x["h"]) for x in left) and h >= max(float(x["h"]) for x in right):
            highs.append(i)
        if l < min(float(x["l"]) for x in left) and l <= min(float(x["l"]) for x in right):
            lows.append(i)
    return highs, lows


def classify(kind: str, p_now: float, p_prev: float, r_now: float, r_prev: float,
             min_price: float, min_rsi: float) -> Optional[str]:
    """Which of the four divergences, if any, two pivots describe."""
    dp, dr = p_now - p_prev, r_now - r_prev
    if abs(dp) < min_price or abs(dr) < min_rsi:
        return None
    if kind == "high":
        if dp > 0 and dr < 0:
            return "REGULAR_BEARISH"      # higher high, lower RSI -> momentum fading
        if dp < 0 and dr > 0:
            return "HIDDEN_BEARISH"       # lower high, higher RSI -> downtrend continuing
    else:
        if dp < 0 and dr > 0:
            return "REGULAR_BULLISH"      # lower low, higher RSI -> selling exhausting
        if dp > 0 and dr < 0:
            return "HIDDEN_BULLISH"       # higher low, lower RSI -> uptrend continuing
    return None


IS_REVERSAL = {"REGULAR_BEARISH", "REGULAR_BULLISH"}
IS_SHORT = {"REGULAR_BEARISH", "HIDDEN_BEARISH"}


def aggregate_1m(one_min: List[Dict[str, Any]], step: int, now_e: int) -> List[Dict[str, Any]]:
    """Roll 1-minute candles into `step`-second bars, keeping only FULLY CLOSED buckets.

    A bucket is closed when its end (k + step) has passed, so the bar currently forming is never
    handed to the divergence engine. Lives here rather than inline in the endpoint so the arithmetic
    is testable -- a half-formed final bar silently flowing in would shift every pivot by one.
    """
    buckets: Dict[int, Dict[str, Any]] = {}
    for b in sorted(one_min or [], key=lambda x: int(x["ts"])):
        k = (int(b["ts"]) // step) * step
        q = buckets.get(k)
        if q is None:
            buckets[k] = {"ts": k, "o": float(b["o"]), "h": float(b["h"]),
                          "l": float(b["l"]), "c": float(b["c"])}
        else:
            q["h"] = max(q["h"], float(b["h"]))
            q["l"] = min(q["l"], float(b["l"]))
            q["c"] = float(b["c"])
    return [v for k, v in sorted(buckets.items()) if k + step <= now_e]


# ── engine ────────────────────────────────────────────────────────────────────────────────────────

def compute_rsi_divergence(
    bars: List[Dict[str, Any]],
    tf: str = "3m",
    day_regime: str = "none",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Detect divergences on CLOSED bars and run each through the watch/trigger/expire lifecycle.

    `bars` are oldest-first dicts with ts, o, h, l, c. `day_regime` is "bull" | "bear" | "none" from
    the desk's cumulative Net PE-CE: a reversal watch pointing against a decided day is recorded but
    marked vetoed, and can never confirm. That is the 29 Oct lesson expressed as code.
    """
    c = {**{
        "rsi_period": 14, "pivot_window": 3, "min_pivot_gap_bars": 3, "max_pivot_gap_bars": 60,
        "min_price_diff_pts": 8.0, "min_rsi_diff": 3.0, "expiry_bars": 10,
        "structure_buffer_pts": 1.0, "sl_buffer_pts": 12.0, "enabled": True,
    }, **(cfg if cfg is not None else get_cfg())}

    step = BAR_SECONDS.get(tf, 180)
    bars = [b for b in sorted(bars or [], key=lambda x: int(x["ts"])) if b.get("closed") is not False]
    closes = [float(b["c"]) for b in bars]
    rsi = wilder_rsi(closes, int(c["rsi_period"]))

    out: Dict[str, Any] = {
        "ok": True, "timeframe": tf, "rsi_period": int(c["rsi_period"]),
        "day_regime": day_regime, "total_bars": len(bars),
        "current_rsi": None, "events": [], "active": None, "continuation": None,
        "message": "",
    }
    if len(bars) <= int(c["rsi_period"]) + 2 * int(c["pivot_window"]):
        out["ok"] = False
        out["message"] = f"not enough closed {tf} bars for RSI({c['rsi_period']}) divergence"
        return out
    out["current_rsi"] = None if rsi[-1] is None else round(rsi[-1], 1)

    w = int(c["pivot_window"])
    hi_idx, lo_idx = find_pivots(bars, w)
    events: List[Dict[str, Any]] = []
    # Why a pair produced nothing. Without this, "0 events" is indistinguishable from a broken
    # detector -- which is exactly the ambiguity hit on the first live run, where the real cause was
    # that the 10:30 and 14:54 pivots sit ~88 bars apart on 3m and the gap filter dropped them.
    rejected = {"gap_too_short": 0, "gap_too_long": 0, "rsi_unavailable": 0, "no_divergence": 0}

    def scan(idxs: List[int], kind: str) -> None:
        for a, b in zip(idxs, idxs[1:]):
            gap = b - a
            if gap < int(c["min_pivot_gap_bars"]):
                rejected["gap_too_short"] += 1
                continue
            if gap > int(c["max_pivot_gap_bars"]):
                rejected["gap_too_long"] += 1
                continue
            if rsi[a] is None or rsi[b] is None:
                rejected["rsi_unavailable"] += 1
                continue
            field = "h" if kind == "high" else "l"
            typ = classify(kind, float(bars[b][field]), float(bars[a][field]),
                           float(rsi[b]), float(rsi[a]),
                           float(c["min_price_diff_pts"]), float(c["min_rsi_diff"]))
            if not typ:
                rejected["no_divergence"] += 1
                continue
            # A pivot is only confirmed `w` bars later, so that is the earliest this could be known.
            armed_i = min(b + w, len(bars) - 1)
            short = typ in IS_SHORT
            between = bars[a:b + 1]
            # The trigger is the structure between the two pivots: for a bearish watch, the lowest low
            # between the highs. Closing through it is the price-action confirmation, and it is the
            # whole difference between this engine and the one that lost money on 29 Oct.
            trigger = min(x["l"] for x in between) if short else max(x["h"] for x in between)
            invalidation = (float(bars[b][field]) + float(c["sl_buffer_pts"])) if short else \
                           (float(bars[b][field]) - float(c["sl_buffer_pts"]))
            vetoed = typ in IS_REVERSAL and (
                (short and day_regime == "bull") or (not short and day_regime == "bear"))
            events.append({
                "type": typ, "kind": kind, "side": "short" if short else "long",
                "is_reversal": typ in IS_REVERSAL,
                "pivot_from_ts": int(bars[a]["ts"]), "pivot_to_ts": int(bars[b]["ts"]),
                "pivot_from_time": hm(int(bars[a]["ts"])), "pivot_to_time": hm(int(bars[b]["ts"])),
                "price_from": round(float(bars[a][field]), 2), "price_to": round(float(bars[b][field]), 2),
                "rsi_from": round(float(rsi[a]), 1), "rsi_to": round(float(rsi[b]), 1),
                "armed_ts": int(bars[armed_i]["ts"]), "armed_time": hm(int(bars[armed_i]["ts"])),
                "armed_i": armed_i,
                "structure_trigger": round(trigger, 2),
                "invalidation": round(invalidation, 2),
                "expires_ts": int(bars[armed_i]["ts"]) + int(c["expiry_bars"]) * step,
                "state": "VETOED" if vetoed else "ARMED",
                "vetoed": vetoed,
                "action": (
                    f"Counter-trend vs a {day_regime.upper()} day — watch only, will not confirm"
                    if vetoed else
                    f"Wait for close {'below' if short else 'above'} {trigger:.1f}"),
            })

    scan(hi_idx, "high")
    scan(lo_idx, "low")
    events.sort(key=lambda e: e["armed_ts"])

    # Lifecycle, bar by bar after arming. Causal: only bars at or after armed_i can move an event.
    buf = float(c["structure_buffer_pts"])
    for e in events:
        if e["state"] == "VETOED":
            continue
        short = e["side"] == "short"
        for i in range(e["armed_i"] + 1, len(bars)):
            bar = bars[i]
            adverse = float(bar["h"]) if short else float(bar["l"])
            # Ran past the divergence pivot instead of confirming: the premise is gone.
            if (short and adverse >= e["invalidation"]) or (not short and adverse <= e["invalidation"]):
                e.update(state="INVALIDATED", resolved_ts=int(bar["ts"]), resolved_time=hm(int(bar["ts"])))
                break
            if int(bar["ts"]) > e["expires_ts"]:
                e.update(state="EXPIRED", resolved_ts=int(bar["ts"]), resolved_time=hm(int(bar["ts"])))
                break
            closed_through = (float(bar["c"]) < e["structure_trigger"] - buf) if short else \
                             (float(bar["c"]) > e["structure_trigger"] + buf)
            if closed_through:
                e.update(state="STRUCTURE_CONFIRMED", resolved_ts=int(bar["ts"]),
                         resolved_time=hm(int(bar["ts"])), entry=round(float(bar["c"]), 2),
                         sl=e["invalidation"],
                         risk_pts=round(abs(float(bar["c"]) - e["invalidation"]), 1))
                break

    out["events"] = events
    out["active"] = next((e for e in reversed(events)
                          if e["state"] not in TERMINAL and e["state"] != "VETOED"), None)
    out["continuation"] = next((e for e in reversed(events) if not e["is_reversal"]), None)
    confirmed = [e for e in events if e["state"] == "STRUCTURE_CONFIRMED"]
    out["pivots"] = {
        "highs": len(hi_idx), "lows": len(lo_idx),
        "high_times": [hm(int(bars[i]["ts"])) for i in hi_idx],
        "low_times": [hm(int(bars[i]["ts"])) for i in lo_idx],
    }
    out["rejected"] = rejected
    out["counts"] = {
        "total": len(events),
        "armed": sum(1 for e in events if e["state"] == "ARMED"),
        "confirmed": len(confirmed),
        "invalidated": sum(1 for e in events if e["state"] == "INVALIDATED"),
        "expired": sum(1 for e in events if e["state"] == "EXPIRED"),
        "vetoed": sum(1 for e in events if e["state"] == "VETOED"),
    }
    return out
