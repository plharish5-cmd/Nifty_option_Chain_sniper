"""Early read of the FORMING 15-min candle — acting ~3 minutes before it closes.

What is known at minute k of a 15-min candle (k closed 1-min bars):
  * OPEN is final.
  * HIGH-so-far and LOW-so-far can only EXTEND, never retract.
  * Only the CLOSE (and wick extensions) are still unknown, over r = 15 - k minutes.

Two answers are computed for every candlestick pattern:
  1. Flip range   - the band of closes inside which the pattern survives (deterministic,
                    explainable: "engulfing holds while close stays above 23,441").
  2. Lock prob    - Monte Carlo: replay r-minute paths taken from YOUR OWN 1-min history,
                    scaled to today's 1-min volatility, complete the candle, re-run TA-Lib.
                    Fraction of completed candles that keep the pattern = lock probability.
                    Falls back to a random walk when history is too short (flagged in output).
The same simulated closes give P(close above/below any level) for 15-min close triggers.

Every provisional read is logged and reconciled against the final candle so the
early-read accuracy per pattern can be measured (see backtest_early.py for history).
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd

from .common import closed_1m, resample
from .talib_patterns import detect_arrays, side_of

TF = 15


# ---------------------------------------------------------------- provisional candle
def provisional(df_1m_closed: pd.DataFrame, now: pd.Timestamp | None = None) -> dict | None:
    """Current 15-min bucket built from closed 1-min bars. None if no bucket is forming."""
    bars = resample(df_1m_closed, TF, now)
    if bars.empty or not bars["forming"].iloc[-1]:
        return None
    start = bars.index[-1]
    k = int(bars["n1m"].iloc[-1])
    row = bars.iloc[-1]
    return {"start": start, "end": start + pd.Timedelta(minutes=TF), "elapsed": k,
            "remaining": TF - k, "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"], "history": bars.iloc[:-1]}


def vol_unit(df_1m_closed: pd.DataFrame, lookback: int) -> float:
    """Typical 1-min range (points) over the last `lookback` closed bars."""
    rng = (df_1m_closed["high"] - df_1m_closed["low"]).iloc[-lookback:]
    v = float(rng.mean()) if len(rng) else 0.0
    return v if v > 0 else 1.0


# ---------------------------------------------------------------- path library
def build_path_library(hist_1m: pd.DataFrame, r: int, lookback: int) -> np.ndarray:
    """Normalised r-minute paths from history: rows of (d_close, max_up, max_dn) / vol unit.

    Reference price is the close just before the segment; segments never cross a day.
    Vectorised with sliding windows, so 5 years of 1-min data builds in well under a second.
    """
    from numpy.lib.stride_tricks import sliding_window_view as swv
    if hist_1m is None or hist_1m.empty or r <= 0:
        return np.empty((0, 3))
    out = []
    for _, day in hist_1m.groupby(hist_1m.index.date):
        h, l, c = (day[x].to_numpy(dtype=float) for x in ("high", "low", "close"))
        n = len(c)
        if n < lookback + r + 1:
            continue
        unit = swv(h - l, lookback).mean(axis=1)[: n - lookback - r + 1]   # bars [i-lookback, i)
        starts = np.arange(lookback, n - r + 1)
        c0 = c[starts - 1]
        up = swv(h, r).max(axis=1)[starts]
        dn = swv(l, r).min(axis=1)[starts]
        cl = c[starts + r - 1]
        good = unit > 0
        out.append(np.column_stack([(cl - c0)[good] / unit[good], (up - c0)[good] / unit[good],
                                    (dn - c0)[good] / unit[good]]))
    return np.vstack(out) if out else np.empty((0, 3))


def random_walk_paths(df_1m_closed: pd.DataFrame, r: int, n: int, unit: float,
                      rng: np.random.Generator) -> np.ndarray:
    """Fallback: Gaussian 1-min steps with today's close-to-close sigma (normalised)."""
    sig = float(df_1m_closed["close"].diff().iloc[-60:].std() or unit * 0.6)
    steps = rng.normal(0, sig, size=(n, r))
    path = np.cumsum(steps, axis=1)
    half = unit * 0.5  # intraminute excursion beyond the close path
    up = np.maximum(path.max(axis=1), 0) + half
    dn = np.minimum(path.min(axis=1), 0) - half
    return np.column_stack([path[:, -1], up, dn]) / unit


# ---------------------------------------------------------------- simulation
def simulate(prov: dict, paths_norm: np.ndarray, unit: float, n: int,
             rng: np.random.Generator) -> np.ndarray:
    """Completed candles (n, 4) = open/high/low/close."""
    pick = paths_norm[rng.integers(0, len(paths_norm), size=n)] * unit
    close = prov["close"] + pick[:, 0]
    high = np.maximum(prov["high"], prov["close"] + pick[:, 1])
    low = np.minimum(prov["low"], prov["close"] + pick[:, 2])
    high = np.maximum(high, close)
    low = np.minimum(low, close)
    return np.column_stack([np.full(n, prov["open"]), high, low, close])


def _eval_last(hist: pd.DataFrame, candle, names, depth: int) -> dict:
    tail = hist.iloc[-(depth - 1):]
    o = np.append(tail["open"].to_numpy(float), candle[0])
    h = np.append(tail["high"].to_numpy(float), candle[1])
    l = np.append(tail["low"].to_numpy(float), candle[2])
    c = np.append(tail["close"].to_numpy(float), candle[3])
    res = detect_arrays(o, h, l, c, names)
    return {k: int(v[-1]) for k, v in res.items()}


def flip_range(prov: dict, hist: pd.DataFrame, name: str, value_now: int, unit: float,
               step: float, span_units: float, depth: int):
    """Closes (with minimal wick extension) for which the pattern keeps the same value.

    Returns (low_close, high_close) of the contiguous band containing the current close,
    or None if the pattern is not present now.
    """
    if value_now == 0:
        return None
    lo, hi = prov["close"] - span_units * unit, prov["close"] + span_units * unit
    grid = np.arange(lo, hi + step, step)
    ok = []
    for cl in grid:
        cand = (prov["open"], max(prov["high"], cl), min(prov["low"], cl), cl)
        ok.append(_eval_last(hist, cand, [name], depth)[name] == value_now)
    ok = np.array(ok)
    i0 = int(np.argmin(np.abs(grid - prov["close"])))
    if not ok[i0]:
        return None
    a = i0
    while a > 0 and ok[a - 1]:
        a -= 1
    b = i0
    while b < len(grid) - 1 and ok[b + 1]:
        b += 1
    low_close = None if a == 0 else float(grid[a])      # None = holds beyond scanned span
    high_close = None if b == len(grid) - 1 else float(grid[b])
    return low_close, high_close


def early_read(df_1m_closed: pd.DataFrame, rules: dict, now: pd.Timestamp | None = None,
               hist_1m: pd.DataFrame | None = None, levels=None,
               path_lib: np.ndarray | None = None) -> dict:
    """Full early read for the forming 15-min candle.

    path_lib: optional prebuilt library for the current `remaining` minutes (cache it in the UI).
    """
    er = rules["early_read"]
    names = list(rules["talib_patterns"])
    depth = rules["bars_for_talib"]
    df_1m_closed = closed_1m(df_1m_closed, now)   # defensive: never read past `now`
    prov = provisional(df_1m_closed, now)
    if prov is None:
        return {"status": "no forming 15-min candle"}
    if prov["elapsed"] < er["min_elapsed_min"]:
        return {"status": f"too early: {prov['elapsed']}/15 min elapsed "
                          f"(reads start at {er['min_elapsed_min']})", "prov": prov}
    hist = prov["history"]
    if len(hist) < depth - 1:
        return {"status": f"need {depth - 1} closed 15-min bars of history, have {len(hist)}",
                "prov": prov}

    rng = np.random.default_rng(er["random_seed"])
    unit = vol_unit(df_1m_closed, er["vol_lookback_1m"])
    r = prov["remaining"]
    lib = path_lib if path_lib is not None else build_path_library(
        hist_1m if hist_1m is not None else df_1m_closed, r, er["vol_lookback_1m"])
    source = "your 1-min history"
    if len(lib) < er["history_min_segments"]:
        lib = random_walk_paths(df_1m_closed, r, 2000, unit, rng)
        source = "random walk (history too short)"
    sims = simulate(prov, lib, unit, er["n_sims"], rng)

    now_vals = _eval_last(hist, (prov["open"], prov["high"], prov["low"], prov["close"]), names, depth)
    sim_vals = {n: np.empty(len(sims), dtype=int) for n in names}
    for j, cand in enumerate(sims):
        v = _eval_last(hist, cand, names, depth)
        for n in names:
            sim_vals[n][j] = v[n]

    rows = []
    for n in names:
        v_now = now_vals[n]
        if v_now != 0:
            p = float(np.mean(sim_vals[n] == v_now))
            fr = flip_range(prov, hist, n, v_now, unit, er["flip_grid_step_pts"],
                            er["flip_grid_span_units"], depth)
            rows.append({"pattern": rules["talib_patterns"][n], "code": n, "side": side_of(n, v_now),
                         "state": "PRESENT", "prob": p,
                         "holds_above": fr[0] if fr else None, "holds_below": fr[1] if fr else None,
                         "alert": p >= er["lock_prob_alert"] and prov["elapsed"] >= er["alert_at_elapsed_min"]})
        else:
            nz = sim_vals[n][sim_vals[n] != 0]
            p = len(nz) / len(sims)
            if p >= 0.25:  # pattern not there yet but likely to appear
                dom = int(np.sign(np.median(nz))) * 100
                rows.append({"pattern": rules["talib_patterns"][n], "code": n, "side": side_of(n, dom),
                             "state": "EMERGING", "prob": p, "holds_above": None, "holds_below": None,
                             "alert": False})

    lvl_rows = []
    for lv in levels or []:
        lpx, lname = (lv["px"], lv.get("name", "level")) if isinstance(lv, dict) else (float(lv), "level")
        if abs(lpx - prov["close"]) > 6 * unit:
            continue
        lvl_rows.append({"level": lname, "px": lpx,
                         "p_close_above": float(np.mean(sims[:, 3] > lpx)),
                         "p_close_below": float(np.mean(sims[:, 3] < lpx))})

    return {"status": "ok", "prov": prov, "unit": unit, "paths_source": source,
            "n_paths": int(len(lib)), "patterns": pd.DataFrame(rows),
            "levels": pd.DataFrame(lvl_rows),
            "close_p10_p90": (float(np.percentile(sims[:, 3], 10)), float(np.percentile(sims[:, 3], 90)))}


# ---------------------------------------------------------------- logging / reconcile
FIELDS = ["bucket", "elapsed", "code", "side", "state", "prob", "final_value", "held"]


def log_read(result: dict, log_dir: str | Path) -> None:
    """Append provisional reads once per (bucket, elapsed minute, pattern)."""
    if result.get("status") != "ok" or result["patterns"].empty:
        return
    prov = result["prov"]
    path = Path(log_dir) / f"early_{prov['start']:%Y-%m-%d}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                seen.add((r["bucket"], r["elapsed"], r["code"]))
    new = path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not new:
            w.writeheader()
        for _, r in result["patterns"].iterrows():
            key = (str(prov["start"]), str(prov["elapsed"]), r["code"])
            if key in seen:
                continue
            w.writerow({"bucket": prov["start"], "elapsed": prov["elapsed"], "code": r["code"],
                        "side": r["side"], "state": r["state"], "prob": round(r["prob"], 3),
                        "final_value": "", "held": ""})


def reconcile(log_dir: str | Path, day: pd.Timestamp, df_1m_closed: pd.DataFrame,
              rules: dict, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Fill final_value/held for buckets that have closed. Returns the day's log."""
    path = Path(log_dir) / f"early_{day:%Y-%m-%d}.csv"
    if not path.exists():
        return pd.DataFrame(columns=FIELDS)
    logdf = pd.read_csv(path, dtype={"final_value": "object", "held": "object"})
    bars = resample(df_1m_closed, TF, now)
    closed = bars[~bars["forming"]]
    depth = rules["bars_for_talib"]
    changed = False
    for i, r in logdf[logdf["held"].isna()].iterrows():
        b = pd.Timestamp(r["bucket"])
        if b not in closed.index:
            continue
        pos = closed.index.get_loc(b)
        if pos < depth - 1:
            continue
        hist = closed.iloc[pos - depth + 1:pos]
        bar = closed.iloc[pos]
        v = _eval_last(hist, (bar["open"], bar["high"], bar["low"], bar["close"]), [r["code"]], depth)[r["code"]]
        want_present = r["state"] == "PRESENT"
        final_side = side_of(r["code"], v)
        held = (final_side == r["side"]) if want_present else (final_side == r["side"])
        logdf.at[i, "final_value"] = str(v)
        logdf.at[i, "held"] = str(bool(held))
        changed = True
    if changed:
        logdf.to_csv(path, index=False)
    return logdf
