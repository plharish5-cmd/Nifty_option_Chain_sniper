"""
fakeout_engine.py — the Fakeout Avoidance (Liquidity Sweep → MSS → FVG) short setup, with honest rules.
FAKEOUTFIX

The old tab painted "SELL HERE" on the candle that was still forming, accepted a 0.3-point "FVG", kept a sweep
alive after price closed back above it, sold before the structure shift, put the stop below a high that had
already printed, and drew the target at a low that only formed later. Rules here:

  1. Closed candles only. Nothing paints until a candle has closed.
  2. FVG must be at least max(MIN_FVG_PTS, MIN_FVG_ATR × ATR) points.
  3. A sweep is CANCELLED if any later candle closes above the swept high before the entry.
  4. Entry only on a candle AFTER the bearish MSS / BOS. Stop = highest high since the sweep + SL_BUFFER.
     Targets: T1 = 1R, T2 = 2R. "SSL" shown is the swing low that was KNOWN at the entry bar.
  5. Every entry is logged to data/signals/fakeout_log.csv with the time the app first showed it, and its
     result (SL / T1 / T2 reached / end-of-day) is filled in from the candles that followed.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import smc
from smc import FVG_Tracker, pivots, structure

MIN_FVG_PTS = 3.0          # never smaller than this
MIN_FVG_ATR = 0.10         # … or this fraction of the candle ATR, whichever is bigger
SL_BUFFER = 2.0            # points above the true high
RECENT_BARS = 30           # only sweeps from the last 30 candles count
ATR_LEN = 14

LOG_DIR = Path(__file__).parent / "data" / "signals"
LOG_FILE = LOG_DIR / "fakeout_log.csv"
LAST_ERROR = ""
LOG_COLS = ["signal_id", "date", "tf", "sweep_time", "swept_high", "sweep_wick", "mss_time", "mss_level",
            "fvg_bottom", "fvg_top", "entry_time", "entry", "sl", "risk", "t1", "t2", "ssl_target",
            "first_seen", "lag_min", "live", "result", "result_time", "r", "t2_hit", "mfe_r"]


# ─── helpers ─────────────────────────────────────────────────────────────────
def _naive_ist(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s)
    try:
        if s.dt.tz is not None:
            s = s.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except Exception:                                   # noqa: BLE001
        pass
    return s


def _times(df: pd.DataFrame) -> pd.Series:
    if "timestamp" in df.columns:
        return _naive_ist(df["timestamp"]).reset_index(drop=True)
    return _naive_ist(pd.Series(df.index)).reset_index(drop=True)


def tf_minutes(tf: str) -> int:
    t = str(tf).lower().strip()
    try:
        if t.endswith("m"):
            return int(t[:-1])
        if t.endswith("h"):
            return 60 * int(t[:-1])
    except ValueError:
        pass
    return 5


def closed_only(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the candle(s) still forming at the end. Without a 'closed' column every candle counts as closed."""
    if df is None or len(df) == 0 or "closed" not in df.columns:
        return df
    m = df["closed"].astype(object).where(df["closed"].notna(), True).astype(bool).to_numpy()
    k = len(df)
    while k > 0 and not m[k - 1]:
        k -= 1
    return df.iloc[:k]


def _atr(d: pd.DataFrame) -> np.ndarray:
    h = d["high"].astype(float).to_numpy()
    l = d["low"].astype(float).to_numpy()
    c = d["close"].astype(float).to_numpy()
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).rolling(ATR_LEN, min_periods=3).mean().bfill().to_numpy()


def min_fvg_size(atr_at_bar: float) -> float:
    a = float(atr_at_bar) if np.isfinite(atr_at_bar) else 0.0
    return max(MIN_FVG_PTS, MIN_FVG_ATR * a)


# ─── the setup ───────────────────────────────────────────────────────────────
def find_setup(d: pd.DataFrame) -> Dict[str, Any]:
    """
    d = CLOSED candles only (use closed_only()). Returns a dict the chart understands
    (sweep_bar_idx, mss_level, fvg_top, entry_bar_idx, …) plus status, sl/t1/t2, notes and outcome.
    Bar indexes are positions in d — identical to positions in the full frame, since d is its prefix.
    """
    out: Dict[str, Any] = {"status": "NO_SWEEP", "notes": [], "cancelled": []}
    if d is None or len(d) < 8:
        out["notes"].append("Not enough closed candles yet.")
        return out
    h = d["high"].astype(float).to_numpy()
    l = d["low"].astype(float).to_numpy()
    c = d["close"].astype(float).to_numpy()
    ts = _times(d)
    hm = lambda k: ts.iloc[k].strftime("%H:%M")            # noqa: E731
    n = len(d)
    atr = _atr(d)

    ps = pivots(d, n=2)
    start = max(0, n - RECENT_BARS)
    sweeps = [x for x in smc.liquidity(d, ps) if x.kind == "SWEEP" and x.direction == "ABOVE" and x.i >= start]
    bear_ev = [e for e in structure(d, ps).events if e.direction == "BEARISH"]
    fvgs = [g for g in FVG_Tracker(d).fvgs if g.direction == "BEARISH"]
    out["fvg_too_small"] = 0

    if not sweeps:
        out["notes"].append(f"No sweep above a swing high in the last {RECENT_BARS} closed candles.")
        return out

    for sw in reversed(sweeps):                           # newest first; take the newest that still stands
        lvl = float(sw.level)
        s: Dict[str, Any] = {"sweep_bar_idx": sw.i, "sweep_price": float(sw.wick), "sweep_level": lvl,
                             "sweep_time": hm(sw.i)}
        mss = None
        entry = None
        cancelled_at = None
        waiting_fvg = None
        for k in range(sw.i + 1, n):
            if c[k] > lvl:                                # rule 3
                cancelled_at = k
                break
            if mss is None:
                hit = [e for e in bear_ev if e.i == k]
                if hit:
                    mss = hit[0]
            if mss is None or k <= mss.i:                 # rule 4: only AFTER the MSS candle has closed
                continue
            for g in fvgs:
                if not (sw.i < g.i < k):
                    continue
                size = float(g.top) - float(g.bottom)
                if size < min_fvg_size(atr[g.i]):         # rule 2
                    continue
                if np.any(c[g.i + 1:k] > g.top):          # gap already closed through → dead
                    continue
                waiting_fvg = g
                if h[k] >= g.bottom and c[k] <= g.top:
                    entry = (k, g)
                    break
            if entry:
                break

        if cancelled_at is not None:
            out["cancelled"].append(
                f"{s['sweep_time']} sweep of {lvl:,.1f} (wick {sw.wick:,.1f}) — cancelled: "
                f"{hm(cancelled_at)} closed {c[cancelled_at]:,.1f} above it.")
            continue

        # small-FVG count (for the explanation)
        out["fvg_too_small"] = sum(1 for g in fvgs if g.i > sw.i and (g.top - g.bottom) < min_fvg_size(atr[g.i]))
        out.update(s)
        if mss is not None:
            out["mss_level"] = float(mss.level)
            out["mss_bar_idx"] = int(mss.i)
            out["mss_time"] = hm(mss.i)
        g = entry[1] if entry else waiting_fvg
        if g is None and mss is not None:                 # show the newest qualifying gap after the sweep, if any
            ok = [x for x in fvgs if x.i > sw.i and (x.top - x.bottom) >= min_fvg_size(atr[x.i])
                  and not np.any(c[x.i + 1:] > x.top)]
            g = ok[-1] if ok else None
        if g is not None:
            out["fvg_top"], out["fvg_bottom"], out["fvg_start_idx"] = float(g.top), float(g.bottom), int(g.i)

        if entry:
            k, g = entry
            ent = float(c[k])
            sl = float(h[sw.i:k + 1].max()) + SL_BUFFER
            risk = sl - ent
            if risk <= 0:
                out["status"] = "WAIT_FVG_TAP"
                break
            ssl = smc.active(ps, k, "LOW")
            out.update({
                "status": "ENTRY", "entry_bar_idx": int(k), "entry_price": ent, "entry_time": hm(k),
                "entry_ts": ts.iloc[k], "sl": sl, "risk": risk, "t1": ent - risk, "t2": ent - 2 * risk,
                "ssl_target": float(ssl.price) if (ssl is not None and ssl.price < ent) else 0.0,
            })
            out.update(_outcome(h, l, c, ts, k, ent, sl, risk))
        elif mss is None:
            out["status"] = "WAIT_MSS"
        elif g is None:
            out["status"] = "WAIT_FVG"
        else:
            out["status"] = "WAIT_FVG_TAP"
        break
    else:
        out["status"] = "CANCELLED"

    out["notes"] = _explain(out)
    return out


def _outcome(h, l, c, ts, k, ent, sl, risk) -> Dict[str, Any]:
    """First of SL or T1 decides (same candle hitting both = SL, the worst case). Also: did T2 come before SL."""
    t1, t2 = ent - risk, ent - 2 * risk
    res, rt, r, t2_hit, best = "OPEN", "", None, False, 0.0
    for j in range(k + 1, len(c)):
        if h[j] >= sl:
            if res == "OPEN":
                res, rt, r = "SL", ts.iloc[j].strftime("%H:%M"), -1.0
            break
        best = max(best, (ent - l[j]) / risk)
        if l[j] <= t2:
            t2_hit = True
        if res == "OPEN" and l[j] <= t1:
            res, rt, r = "T1", ts.iloc[j].strftime("%H:%M"), 1.0
        if t2_hit:
            break
    last_t = ts.iloc[len(c) - 1]
    if res == "OPEN" and (last_t.hour, last_t.minute) >= (15, 25):
        res, rt, r = "EOD", last_t.strftime("%H:%M"), round((ent - c[-1]) / risk, 2)
    return {"result": res, "result_time": rt, "r": r, "t2_hit": bool(t2_hit), "mfe_r": round(best, 2)}


def _explain(s: Dict[str, Any]) -> List[str]:
    notes = list(s.get("cancelled", []))[::-1]
    st_ = s.get("status")
    if "sweep_time" in s and st_ != "CANCELLED":
        notes.append(f"{s['sweep_time']} sweep of {s['sweep_level']:,.1f} (wick {s['sweep_price']:,.1f}) — still standing.")
    if st_ == "WAIT_MSS":
        notes.append("Waiting for a candle to CLOSE below a swing low (MSS / BOS). No entry before that.")
    if s.get("mss_time"):
        notes.append(f"{s['mss_time']} MSS: closed below {s['mss_level']:,.1f}.")
    if st_ == "WAIT_FVG":
        extra = f" ({s['fvg_too_small']} smaller gap(s) ignored)" if s.get("fvg_too_small") else ""
        notes.append(f"No bearish FVG of ≥ {MIN_FVG_PTS:g} pts / {MIN_FVG_ATR:g}×ATR after the sweep{extra}.")
    if s.get("fvg_top") and st_ in ("WAIT_FVG_TAP", "ENTRY"):
        notes.append(f"FVG {s['fvg_bottom']:,.1f} – {s['fvg_top']:,.1f} ({s['fvg_top'] - s['fvg_bottom']:.1f} pts).")
    if st_ == "WAIT_FVG_TAP":
        notes.append("Waiting for a candle to rally into the FVG and CLOSE inside or below it.")
    if st_ == "ENTRY":
        notes.append(f"{s['entry_time']} entry {s['entry_price']:,.1f} · SL {s['sl']:,.1f} (true high + {SL_BUFFER:g}) · "
                     f"T1 {s['t1']:,.1f} · T2 {s['t2']:,.1f}.")
        res = s.get("result")
        if res and res != "OPEN":
            notes.append(f"Result: {res} at {s['result_time']}" + (f" ({s['r']:+.2f}R)" if s.get("r") is not None else "")
                         + (" · T2 reached" if s.get("t2_hit") else ""))
    if st_ == "CANCELLED":
        notes.append("Every recent sweep was cancelled by a close back above it — no short setup.")
    return notes


# ─── signal log ──────────────────────────────────────────────────────────────
def _logging_off() -> bool:
    return (os.environ.get("PAPER_PROFILE", "").lower().startswith("replay")
            or os.environ.get("FAKEOUT_NOLOG", "") == "1")


def _s(v: Any) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and np.isnan(v):
            return ""
    except Exception:                                   # noqa: BLE001
        pass
    return str(v)


def load_log() -> pd.DataFrame:
    """Everything as text (the file is small); numbers are converted where they are used."""
    try:
        if LOG_FILE.exists():
            d = pd.read_csv(LOG_FILE, dtype=str, keep_default_na=False)
            for col in LOG_COLS:
                if col not in d.columns:
                    d[col] = ""
            return d
    except Exception:                                   # noqa: BLE001
        pass
    return pd.DataFrame(columns=LOG_COLS)


def log_signal(s: Dict[str, Any], tf: str, now: Optional[datetime] = None) -> Optional[str]:
    """Add a new entry (first time it is seen) or refresh its result. Returns the signal_id."""
    if s.get("status") != "ENTRY" or _logging_off():
        return None
    now = (pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="Asia/Kolkata"))
    if now.tzinfo is not None:
        now = now.tz_convert("Asia/Kolkata").tz_localize(None)
    ets: pd.Timestamp = pd.Timestamp(s["entry_ts"])
    sid = f"{ets:%Y-%m-%d}|{tf}|{ets:%H:%M}"
    bar_close = ets + timedelta(minutes=tf_minutes(tf))
    lag = round((now - bar_close).total_seconds() / 60.0, 1)
    row = {
        "signal_id": sid, "date": f"{ets:%Y-%m-%d}", "tf": tf, "sweep_time": s.get("sweep_time", ""),
        "swept_high": round(s.get("sweep_level", 0.0), 1), "sweep_wick": round(s.get("sweep_price", 0.0), 1),
        "mss_time": s.get("mss_time", ""), "mss_level": round(s.get("mss_level", 0.0), 1),
        "fvg_bottom": round(s.get("fvg_bottom", 0.0), 1), "fvg_top": round(s.get("fvg_top", 0.0), 1),
        "entry_time": s["entry_time"], "entry": round(s["entry_price"], 1), "sl": round(s["sl"], 1),
        "risk": round(s["risk"], 1), "t1": round(s["t1"], 1), "t2": round(s["t2"], 1),
        "ssl_target": round(s.get("ssl_target", 0.0), 1),
        "first_seen": f"{now:%Y-%m-%d %H:%M:%S}", "lag_min": lag,
        # live = the app showed it within two candles of the entry candle closing (not a backfill after hours)
        "live": bool(0 <= lag <= 2 * tf_minutes(tf)),
        "result": s.get("result", "OPEN"), "result_time": s.get("result_time", ""), "r": s.get("r"),
        "t2_hit": bool(s.get("t2_hit", False)), "mfe_r": s.get("mfe_r", 0.0),
    }
    row = {k: _s(v) for k, v in row.items()}
    log = load_log()
    try:
        if len(log) and (log["signal_id"] == sid).any():
            i = log.index[log["signal_id"] == sid][0]
            old = log.loc[i]
            if old.get("result", "") not in ("OPEN", "") and row["result"] == "OPEN":
                return sid                                  # never downgrade a finished result
            changed = False
            for col in ("result", "result_time", "r", "t2_hit", "mfe_r"):
                if old.get(col, "") != row[col]:
                    log.loc[i, col] = row[col]
                    changed = True
            if not changed:
                return sid
        else:
            log = pd.concat([log, pd.DataFrame([row], columns=LOG_COLS)], ignore_index=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log[LOG_COLS].to_csv(LOG_FILE, index=False)
    except Exception as e:                              # noqa: BLE001
        global LAST_ERROR
        LAST_ERROR = f"signal log not saved: {e}"
    return sid


def track_record(live_only: bool = True) -> Dict[str, Any]:
    log = load_log()
    if len(log) == 0:
        return {"n": 0, "rows": log}
    lv = log["live"].astype(str).str.lower().isin(["true", "1"])
    use = log[lv] if live_only else log
    done = use[use["result"].isin(["SL", "T1", "EOD"])]
    r = pd.to_numeric(done["r"], errors="coerce")
    return {
        "n": int(len(use)), "done": int(len(done)), "backfill": int((~lv).sum()),
        "t1": int((done["result"] == "T1").sum()), "sl": int((done["result"] == "SL").sum()),
        "eod": int((done["result"] == "EOD").sum()),
        "t2": int(done["t2_hit"].astype(str).str.lower().isin(["true", "1"]).sum()),
        "avg_r": float(r.mean()) if len(r.dropna()) else float("nan"),
        "sum_r": float(r.sum()) if len(r.dropna()) else 0.0,
        "rows": use,
    }
