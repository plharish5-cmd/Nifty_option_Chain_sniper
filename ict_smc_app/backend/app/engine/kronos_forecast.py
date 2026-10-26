"""
engine/kronos_forecast.py — NIFTY 50 Kronos forecast: session-aware candles in, honest summary out.

This module holds every piece of the Kronos tab that is NOT the neural network:
  * 1-hour NIFTY session slots and the next-session timestamps to forecast into
  * the summary built from the sampled paths (mean path, full band, direction COUNT,
    volatility amplification, expected volume)
  * the step-back / reveal split used to check a past forecast against what really happened

Deliberately torch-free and I/O-free so it unit-tests in milliseconds. The model lives in the
sidecar (ict_smc_app/kronos/), reached over localhost by api/kronos_api.py.

Honesty rules baked in, not bolted on:
  * "18 of 30 runs ended higher" is a COUNT of the model's own runs. It is never relabelled as a
    probability of the index rising. Fields are named *_count / *_of for that reason.
  * The published backtests on this model (Tradehull, Oct 2026) found direction ~= a coin flip
    (47.6% vs 50.5% for resampled-random paths) and the real close outside the band ~2 times in 3.
    `verdict()` therefore never emits a buy or sell instruction, only a range read.
  * NIFTY index candles from Dhan carry volume 0. Volume forecasting is meaningless on the index,
    so volume is reported only when a real volume series was supplied (futures), and flagged.

Thresholds live in rules.json under "kronos". Nothing here is hardcoded that a trader would tune.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..timeutil import IST_OFFSET_S, hm

log = logging.getLogger("kronos_forecast")

# NIFTY trades 09:15-15:30. Dhan's 60-minute candles start on these slots; the 15:15 candle is a
# 15-minute stub, not a full hour, so it is excluded from the model's context by default.
FULL_HOUR_SLOTS = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]
STUB_SLOT = "15:15"
SLOTS_PER_DAY = len(FULL_HOUR_SLOTS)


def get_cfg(rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = dict(rules or {})
    c.setdefault("hist_candles", 360)
    c.setdefault("pred_candles", 24)
    c.setdefault("paths", 30)
    c.setdefault("temperature", 1.0)
    c.setdefault("top_p", 0.9)
    c.setdefault("include_stub_1515", False)
    c.setdefault("undecided_band_pct", 60.0)   # <= this share of runs on one side = undecided
    c.setdefault("show_candles", 140)          # how much history the chart draws behind "now"
    c.setdefault("sidecar_url", "http://127.0.0.1:8701")
    c.setdefault("sidecar_timeout_s", 180.0)
    return c


# ── candles ───────────────────────────────────────────────────────────────────────────────────
def _ist_hm(ts: int) -> str:
    return hm(ts)


def session_slots(ts_list: Sequence[int], include_stub: bool = False) -> List[int]:
    """Keeps only true NIFTY hourly slots, dropping Dhan's 15:15 quarter-hour stub by default."""
    allowed = set(FULL_HOUR_SLOTS) | ({STUB_SLOT} if include_stub else set())
    return [int(t) for t in ts_list if _ist_hm(int(t)) in allowed]


def filter_hourly(candles: Sequence[Dict[str, Any]], include_stub: bool = False) -> List[Dict[str, Any]]:
    """Takes Dhan 60-minute candles and returns only the session slots, oldest first."""
    allowed = set(FULL_HOUR_SLOTS) | ({STUB_SLOT} if include_stub else set())
    out = [dict(c) for c in candles if _ist_hm(int(c.get("ts", 0))) in allowed]
    out.sort(key=lambda c: int(c["ts"]))
    return out


def next_session_ts(last_ts: int, count: int, include_stub: bool = False) -> List[int]:
    """
    The candle-start epochs the forecast should land on — market hours only.

    Kronos is given real future timestamps so its time embedding sees a trading calendar rather
    than a naive +1h walk through nights and weekends. Public holidays are not modelled; the
    forecast horizon is ~4 sessions, so a holiday shifts labels, never the shape of the path.
    """
    slots = FULL_HOUR_SLOTS + ([STUB_SLOT] if include_stub else [])
    slot_minutes = []
    for s in slots:
        hh, mm = s.split(":")
        slot_minutes.append(int(hh) * 60 + int(mm))

    def ist_parts(ts: int) -> Tuple[int, int]:
        ist = int(ts) + IST_OFFSET_S
        day = ist // 86400
        minute_of_day = (ist % 86400) // 60
        return day, minute_of_day

    day, minute = ist_parts(int(last_ts))
    try:
        idx = slot_minutes.index(minute)
    except ValueError:
        idx = max((i for i, m in enumerate(slot_minutes) if m <= minute), default=len(slot_minutes) - 1)

    out: List[int] = []
    while len(out) < count:
        idx += 1
        if idx >= len(slot_minutes):
            idx = 0
            day += 1
            # skip Sat/Sun. Epoch day 0 (1970-01-01) was a Thursday, so (day + 3) % 7 is the
            # Python weekday (Mon=0) — verified against 2026 dates, not assumed.
            while (day + 3) % 7 in (5, 6):
                day += 1
        out.append(day * 86400 + slot_minutes[idx] * 60 - IST_OFFSET_S)
    return out


# ── summary over the sampled paths ────────────────────────────────────────────────────────────
def _closes(path: Sequence[Sequence[float]]) -> List[float]:
    """close is column 3 of [open, high, low, close, volume]."""
    return [float(step[3]) for step in path]


def _realised_vol(closes: Sequence[float]) -> float:
    """Standard deviation of simple returns — the same measure for history and for each path."""
    if len(closes) < 3:
        return 0.0
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return var ** 0.5


def summarize_paths(
    paths: Sequence[Sequence[Sequence[float]]],
    last_close: float,
    hist_closes: Sequence[float],
    has_real_volume: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Turns N sampled paths into the panel the trader reads.

    Every directional field is a COUNT of runs, never a probability of the index moving.
    """
    c = get_cfg(cfg)
    n = len(paths)
    if n == 0 or not last_close:
        return {"ok": False, "message": "no sampled paths"}

    horizon = len(paths[0])
    per_path_closes = [_closes(p) for p in paths]
    end_closes = [pc[-1] for pc in per_path_closes]

    up_count = sum(1 for e in end_closes if e > last_close)
    down_count = n - up_count

    mean_path = [sum(pc[i] for pc in per_path_closes) / n for i in range(horizon)]
    band_low = [min(pc[i] for pc in per_path_closes) for i in range(horizon)]
    band_high = [max(pc[i] for pc in per_path_closes) for i in range(horizon)]

    mean_end = mean_path[-1]
    lowest_end, highest_end = min(end_closes), max(end_closes)

    # Volatility amplification: how many runs swing harder over the horizon than the last
    # `horizon` real candles did. Same estimator on both sides so the comparison is fair.
    hist_window = list(hist_closes)[-horizon:] if len(hist_closes) >= horizon else list(hist_closes)
    hist_vol = _realised_vol(hist_window)
    path_vols = [_realised_vol(pc) for pc in per_path_closes]
    vol_amp_count = sum(1 for v in path_vols if v > hist_vol) if hist_vol > 0 else 0

    expected_volume: Optional[float] = None
    if has_real_volume:
        expected_volume = sum(sum(float(step[4]) for step in p) for p in paths) / n

    one_sided = max(up_count, down_count) / n * 100.0
    undecided = one_sided <= float(c["undecided_band_pct"])

    return {
        "ok": True,
        "runs": n,
        "horizon": horizon,
        "last_close": round(float(last_close), 2),
        "up_count": up_count,
        "down_count": down_count,
        "up_of": f"{up_count} of {n}",
        "down_of": f"{down_count} of {n}",
        "undecided": undecided,
        "mean_path": [round(v, 2) for v in mean_path],
        "band_low": [round(v, 2) for v in band_low],
        "band_high": [round(v, 2) for v in band_high],
        "mean_end": round(mean_end, 2),
        "mean_end_pct": round((mean_end - last_close) / last_close * 100.0, 2),
        "range_low": round(lowest_end, 2),
        "range_high": round(highest_end, 2),
        "hist_vol": round(hist_vol, 6),
        "vol_amp_count": vol_amp_count,
        "vol_amp_of": f"{vol_amp_count} of {n}",
        "vol_amp_pct": round(vol_amp_count / n * 100.0, 1),
        "expected_volume": round(expected_volume, 0) if expected_volume is not None else None,
        "has_real_volume": bool(has_real_volume),
    }


def verdict(summary: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    A plain-language read of the run counts. Never a trade instruction.

    The model's own published backtest put direction at roughly a coin flip, so this returns a
    description of the spread the model drew, plus the standing caveat.
    """
    if not summary.get("ok"):
        return {"label": "NO FORECAST", "text": summary.get("message", "no data"), "tone": "flat"}

    n, up, down = summary["runs"], summary["up_count"], summary["down_count"]
    pct = max(up, down) / n * 100.0

    if summary.get("undecided"):
        label, tone = "⚖️ UNDECIDED", "flat"
        text = (f"The runs split {up} up / {down} down — the model has no directional lean. "
                f"Treat the band as a range, not a signal.")
    elif up > down:
        label, tone = "🟢 RUNS LEAN UP", "up"
        text = (f"{up} of {n} runs ended above the last close ({pct:.0f}% of runs). "
                f"This is a count of the model's own paths, not a {pct:.0f}% chance of a rise.")
    else:
        label, tone = "🔴 RUNS LEAN DOWN", "down"
        text = (f"{down} of {n} runs ended below the last close ({pct:.0f}% of runs). "
                f"This is a count of the model's own paths, not a {pct:.0f}% chance of a fall.")

    vol_note = ""
    if summary.get("vol_amp_count") is not None:
        amp = summary["vol_amp_count"]
        if amp > n * 0.6:
            vol_note = (f" {amp} of {n} runs swing harder than the last {summary['horizon']} real candles — "
                        f"expansion favours option buyers and hurts sellers.")
        elif amp < n * 0.25:
            vol_note = (f" Only {amp} of {n} runs expand volatility — a quiet, theta-friendly read.")

    return {
        "label": label,
        "tone": tone,
        "text": text + vol_note,
        "caveat": ("Published backtests of this model on Indian equities found direction close to a coin flip "
                   "(47.6% vs 50.5% for resampled-random paths) and the real close outside the band about "
                   "2 times in 3. Use it as a first look at the possible range, never as an entry trigger."),
    }


# ── step back / reveal ────────────────────────────────────────────────────────────────────────
def split_for_stepback(
    candles: Sequence[Dict[str, Any]],
    hist: int,
    pred: int,
    step_back: int = 0,
) -> Dict[str, Any]:
    """
    Splits a candle series into (context, actual-future) for a forecast made `step_back` candles ago.

    step_back=0 is a live forecast with no future to compare against. Any larger value rewinds
    "now", so Reveal can draw what really happened after that forecast.
    """
    rows = [dict(c) for c in candles]
    rows.sort(key=lambda c: int(c["ts"]))
    n = len(rows)
    cut = n - int(step_back)
    if cut < hist:
        return {"ok": False, "message": f"need {hist} candles before the forecast point, have {max(cut, 0)}"}

    context = rows[max(0, cut - hist):cut]
    actual = rows[cut:cut + pred]
    return {
        "ok": True,
        "context": context,
        "actual": actual,
        "origin_ts": int(context[-1]["ts"]),
        "step_back": int(step_back),
        "is_live": int(step_back) == 0,
        "revealed": len(actual),
    }


def score_reveal(summary: Dict[str, Any], actual: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Marks a past forecast against what really happened — the part that keeps this honest.

    Reports direction hit, whether each actual close stayed inside the band, and the miss of the
    mean path, so a unanimous-but-wrong forecast is visible rather than quietly forgotten.
    """
    if not summary.get("ok") or not actual:
        return {"ok": False, "message": "nothing revealed yet"}

    last_close = float(summary["last_close"])
    closes = [float(c["c"]) for c in actual]
    band_low, band_high = summary["band_low"], summary["band_high"]

    inside = 0
    for i, px in enumerate(closes):
        if i < len(band_low) and band_low[i] <= px <= band_high[i]:
            inside += 1

    actual_end = closes[-1]
    actual_up = actual_end > last_close
    model_said_up = summary["up_count"] > summary["down_count"]
    direction_hit = (actual_up == model_said_up) and not summary.get("undecided")

    n_cmp = min(len(closes), len(summary["mean_path"]))
    mean_at_end = summary["mean_path"][n_cmp - 1]

    return {
        "ok": True,
        "revealed": len(closes),
        "actual_end": round(actual_end, 2),
        "actual_pct": round((actual_end - last_close) / last_close * 100.0, 2),
        "actual_up": actual_up,
        "direction_hit": bool(direction_hit),
        "inside_band": inside,
        "outside_band": len(closes) - inside,
        "inside_band_pct": round(inside / len(closes) * 100.0, 1),
        "mean_miss_pts": round(actual_end - mean_at_end, 2),
        "note": ("A unanimous forecast can still be wrong — that is the point of Reveal."
                 if not direction_hit and max(summary["up_count"], summary["down_count"]) == summary["runs"]
                 else ""),
    }
