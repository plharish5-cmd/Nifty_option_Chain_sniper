"""
ict_model.py — the ICT "2022 Mentorship" model written as explicit, testable rules for NIFTY.

The model (same steps in both directions; shown for a BUY):
  1. LIQUIDITY   a pool of sell-side stops: previous-day low, opening-range low, or a confirmed 5m swing low
                 made earlier today.
  2. SWEEP       a 5m candle trades BELOW that pool and price closes back above it within `reclaim_candles`.
  3. DISPLACEMENT + MSS
                 within `mss_window_candles` a strong bullish candle (body >= disp_body_atr x recent range)
                 CLOSES above the last short-term high that existed before the sweep (market-structure shift).
  4. FVG         the displacement leg leaves a Fair Value Gap (3-candle imbalance, >= min_fvg_pts).
  5. ENTRY       a LIMIT order on the retrace: FVG top | FVG midpoint (CE) | OTE (62-79 % of the leg).
  6. STOP        just beyond the sweep extreme.      TARGET  2R | 3R | the next liquidity pool above.
  7. MANAGEMENT  optional: stop to break-even once 75 % of the way to the target (Episode 41).
  8. PREMIUM / DISCOUNT (pd_filter) buy only BELOW the midpoint of today's range so far ("eq") or below the day's
                 open ("open"); sell only above it — the ICT dealing-range rule (Episodes 2, 10, 27, 35, 36).

Everything is CAUSAL: a setup only uses candles that had already closed at the moment the order is placed
(`sig_t`). backtest_ict.py then tests it against random controls — this file only finds setups.

  .venv/bin/python ict_model.py --day 2026-10-01     # print every setup the model finds that day
  .venv/bin/python ict_model.py                      # the most recent stored session

Information only — not financial advice.
"""
from __future__ import annotations

import json
import sys
from datetime import time as dtime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
RULES_PATH = ROOT / "ict_rules.json"

_DEF = dict(
    tf_minutes=5, liquidity=["pd", "swing", "or"], swing_n=2, or_minutes=15, reclaim_candles=2,
    mss_window_candles=12, pre_lookback_candles=6, disp_body_atr=0.8, atr_n=12, min_fvg_pts=3.0,
    ote_lo=0.62, ote_hi=0.79, entry="ce", tp="2", mode="be75", pd_filter="eq", fill_wait_min=60, sl_buffer_pts=2.0,
    min_risk_pts=8, max_risk_pts=60, min_signal="09:25", last_signal="14:45", max_trades_day=2,
    windows={"A morning 09:15-11:45": ["09:15", "11:45"], "B lunch 11:45-13:30": ["11:45", "13:30"],
             "C afternoon 13:30-15:00": ["13:30", "15:00"]},
    validation=dict(reps=1000, grid_reps=200, min_trades=30, p_max=0.05),
)


def rules(path: Optional[Path] = None) -> dict:
    r = json.loads(json.dumps(_DEF))
    p = path or RULES_PATH
    try:
        if p.exists():
            r.update({k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")})
    except Exception:
        pass
    return r


def _hm(s: str) -> dtime:
    a, b = s.split(":")
    return dtime(int(a), int(b))


# ═══════════════════════════════════════════════════════════════════════════
# Resampling (09:15-anchored, per session)
# ═══════════════════════════════════════════════════════════════════════════
def resample(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    out = []
    for _, g in m1.groupby(m1["timestamp"].dt.date):
        r = (g.set_index("timestamp").resample(f"{minutes}min", origin="start_day", offset="15min")
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index())
        out.append(r)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])


# ═══════════════════════════════════════════════════════════════════════════
# Detection (long space; shorts are the same code on negated prices)
# ═══════════════════════════════════════════════════════════════════════════
def _pivots(h: np.ndarray, l: np.ndarray, n: int):
    """Swing pivots. A pivot at p only becomes KNOWN at candle p+n (so callers must use p+n < i)."""
    ph, pl = [], []
    for p in range(n, len(h) - n):
        if h[p] > h[p - n:p].max() and h[p] >= h[p + 1:p + n + 1].max():
            ph.append(p)
        if l[p] < l[p - n:p].min() and l[p] <= l[p + 1:p + n + 1].min():
            pl.append(p)
    return ph, pl


def smt_flag(key, i: int, j: int, n: int, n_or: int, ref: Optional[dict]) -> Optional[bool]:
    """SMT divergence at a sweep of a LOW (long space): True when the correlated index (Bank Nifty) did NOT take its own
    matching low while NIFTY did. key = ("pd",) | ("or",) | ("sw", p) | ("eq", p1, p2).  None = no reference data."""
    if not ref:
        return None
    rl = ref["l"]
    try:
        if key[0] == "pd":
            base = ref["pdl"]
        elif key[0] == "or":
            base = np.nanmin(rl[:n_or]) if n_or <= len(rl) else np.nan
        elif key[0] == "eq":
            base = min(np.nanmin(rl[max(0, int(q) - n):int(q) + n + 1]) for q in key[1:3])
        else:
            p = int(key[1])
            base = np.nanmin(rl[max(0, p - n):p + n + 1])
        win = rl[i:j + 1]
        if np.isnan(base) or np.all(np.isnan(win)):
            return None
        return bool(np.nanmin(win) >= base)
    except Exception:
        return None


def _detect_long(t, o, h, l, c, atr, pdl, pdh, P, ref: Optional[dict] = None) -> List[dict]:
    N = len(c)
    n = int(P["swing_n"])
    tf = int(P["tf_minutes"])
    n_or = max(1, int(P["or_minutes"]) // tf)
    liq = set(P["liquidity"])
    ph, pl = _pivots(h, l, n)
    consumed = set()
    out: List[dict] = []
    t_min, t_max = _hm(P["min_signal"]), _hm(P["last_signal"])
    i = 1
    while i < N - 1:
        # liquidity pools that exist at candle i (known before it opened)
        active = []
        if "pd" in liq and not np.isnan(pdl):
            active.append((("pd",), float(pdl), "PDL"))
        if "or" in liq and i >= n_or:
            active.append((("or",), float(l[:n_or].min()), "OR low"))
        if "swing" in liq:
            for p in pl:
                if p + n < i:
                    active.append((("sw", p), float(l[p]), "swing low"))
        breached = [(price, kind, key) for key, price, kind in active if key not in consumed and l[i] < price]
        for _, _, key in breached:
            consumed.add(key)
        if not breached:
            i += 1
            continue
        # the sweep must be reclaimed (close back above the pool) within reclaim_candles
        chosen = None
        for price, kind, key in sorted(breached, key=lambda x: -x[0]):
            rj = next((j for j in range(i, min(i + int(P["reclaim_candles"]), N - 1) + 1) if c[j] > price), None)
            if rj is not None:
                chosen = (price, kind, rj, key)
                break
        if chosen is None:
            i += 1
            continue
        L, kind, rj, lkey = chosen
        # last short-term high that was already KNOWN before the sweep candle
        known = [p for p in ph if p + n < i]
        if known:
            ref_high = float(h[known[-1]])
        else:
            lo_i = max(0, i - int(P["pre_lookback_candles"]))
            if i - lo_i < 2:
                i += 1
                continue
            ref_high = float(h[lo_i:i].max())
        found = None
        for k in range(max(i + 1, rj), min(i + int(P["mss_window_candles"]), N - 1) + 1):
            if c[k] < L:                                           # reclaim failed → it was a breakdown, not a sweep
                break
            a = atr[k]
            if np.isnan(a) or not (c[k] > ref_high and c[k] > o[k] and (c[k] - o[k]) >= float(P["disp_body_atr"]) * a):
                continue
            best = None                                            # largest bullish FVG inside the displacement leg
            for m in range(max(i, 1), k + 1):
                if m + 1 > min(k + 1, N - 1):
                    continue
                gap = l[m + 1] - h[m - 1]
                if gap >= float(P["min_fvg_pts"]) and (best is None or gap > best[0]):
                    best = (gap, m)
            if best is None:
                continue
            found = (k, best[1], best[0])
            break
        if found is None:
            i += 1
            continue
        k, m, gap = found
        sig = max(k, m + 1)
        sig_t = pd.Timestamp(t[sig]) + pd.Timedelta(minutes=tf)
        if sig_t.time() > t_max:
            break
        if sig_t.time() < t_min:
            i += 1
            continue
        leg_lo, leg_hi = float(l[i:sig + 1].min()), float(h[i:sig + 1].max())
        fvg_top, fvg_bot = float(l[m + 1]), float(h[m - 1])
        cands = [x for x in (float(h[:i].max()) if i > 0 else np.nan, pdh) if not np.isnan(x) and x > leg_hi]
        out.append(dict(
            sweep_t=pd.Timestamp(t[i]), mss_t=pd.Timestamp(t[k]), sig_t=sig_t, level_kind=kind, level=L,
            fvg_top=fvg_top, fvg_bot=fvg_bot, leg_lo=leg_lo, leg_hi=leg_hi,
            entry_fvg=fvg_top, entry_ce=(fvg_top + fvg_bot) / 2,
            entry_ote=leg_hi - (float(P["ote_lo"]) + float(P["ote_hi"])) / 2 * (leg_hi - leg_lo),
            sl=leg_lo - float(P["sl_buffer_pts"]), liq=min(cands) if cands else np.nan,
            close_at_signal=float(c[sig]),
            eq=(float(h[:sig + 1].max()) + float(l[:sig + 1].min())) / 2,      # 50 % of today's range so far
            day_open=float(o[0]), smt=smt_flag(lkey, i, rj, n, n_or, ref)))
        i = sig + 1
    return out


def _flip(s: dict) -> dict:
    """Convert a setup found in negated (long) space back to real prices for a SELL."""
    r = dict(s)
    for k in ("level", "fvg_top", "fvg_bot", "leg_lo", "leg_hi", "entry_fvg", "entry_ce", "entry_ote", "sl", "liq",
              "close_at_signal", "eq", "day_open"):
        r[k] = -s[k]
    r["fvg_top"], r["fvg_bot"] = -s["fvg_bot"], -s["fvg_top"]
    r["leg_lo"], r["leg_hi"] = -s["leg_hi"], -s["leg_lo"]
    return r


def ref_arrays(g: pd.DataFrame, r5: Optional[pd.DataFrame], prev: tuple) -> Optional[dict]:
    """Bank Nifty 5m arrays aligned to this NIFTY day's candles (NaN where missing) + its previous-day high/low."""
    if r5 is None or r5.empty or prev is None or any(np.isnan(x) for x in prev):
        return None
    a = r5.set_index("timestamp").reindex(g["timestamp"])
    if a["low"].isna().all():
        return None
    return dict(o=a["open"].to_numpy(float), h=a["high"].to_numpy(float), l=a["low"].to_numpy(float),
                c=a["close"].to_numpy(float), pdh=float(prev[0]), pdl=float(prev[1]))


def _neg_ref(ref: Optional[dict]) -> Optional[dict]:
    if not ref:
        return None
    return dict(o=-ref["o"], h=-ref["l"], l=-ref["h"], c=-ref["c"], pdh=-ref["pdl"], pdl=-ref["pdh"])


def day_hilo(r5: Optional[pd.DataFrame]) -> dict:
    if r5 is None or r5.empty:
        return {}
    g = r5.groupby(r5["timestamp"].dt.date)
    return {d: (float(x["high"].max()), float(x["low"].min())) for d, x in g}


def detect_all(d5: pd.DataFrame, P: Optional[dict] = None, ref5: Optional[pd.DataFrame] = None) -> List[dict]:
    """Find every setup in a multi-day 5-minute frame (columns timestamp, open, high, low, close).
    Returns dicts sorted by order time, each with day, side ('BULLISH'/'BEARISH') and the entry/stop/target levels.
    ref5 = Bank Nifty 5m frame → every setup also gets `smt` (True = Bank Nifty did NOT confirm the sweep)."""
    P = P or rules()
    if d5 is None or d5.empty:
        return []
    d5 = d5.sort_values("timestamp").reset_index(drop=True)
    rng = (d5["high"] - d5["low"]).rolling(int(P["atr_n"]), min_periods=4).mean().shift(1)
    day = d5["timestamp"].dt.date
    days = sorted(day.unique())
    out: List[dict] = []
    prev_hi = prev_lo = np.nan
    rhl = day_hilo(ref5)
    rdays = sorted(rhl)
    for dd in days:
        g = d5[day == dd]
        ix = g.index.to_numpy()
        t = g["timestamp"].to_numpy()
        o, h, l, c = (g[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        atr = rng.iloc[ix].to_numpy(float)
        if len(c) >= 20:
            pr = [x for x in rdays if x < dd]
            ref = ref_arrays(g, ref5, rhl[pr[-1]] if pr else None)
            for s in _detect_long(t, o, h, l, c, atr, prev_lo, prev_hi, P, ref):
                s.update(day=str(dd), side="BULLISH")
                out.append(s)
            for s in _detect_long(t, -o, -l, -h, -c, atr, -prev_hi if not np.isnan(prev_hi) else np.nan,
                                  -prev_lo if not np.isnan(prev_lo) else np.nan, P, _neg_ref(ref)):
                s = _flip(s)
                s.update(day=str(dd), side="BEARISH")
                out.append(s)
        prev_hi, prev_lo = float(h.max()), float(l.min())
    out.sort(key=lambda s: s["sig_t"])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Presentation
# ═══════════════════════════════════════════════════════════════════════════
def describe(s: dict, entry: str = "ce", tp: str = "2") -> str:
    buy = s["side"] == "BULLISH"
    e = s["entry_" + entry]
    risk = abs(e - s["sl"])
    if tp == "liq":
        tgt = s["liq"]
    else:
        tgt = e + (1 if buy else -1) * float(tp) * risk
    return (f"{'BUY ' if buy else 'SELL'} limit {e:,.1f}  SL {s['sl']:,.1f} ({risk:.0f} pts)  "
            f"TP {tgt:,.1f}  · swept {s['level_kind']} {s['level']:,.1f} at {s['sweep_t']:%H:%M}, "
            f"MSS {s['mss_t']:%H:%M}, FVG {s['fvg_bot']:,.1f}-{s['fvg_top']:,.1f}, order from {s['sig_t']:%H:%M}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    import backtest_five_rule as fr
    m1 = fr.load_1m()
    if m1.empty:
        sys.exit("No stored 1-minute candles found (data/candles, data/candles_hist).")
    P = rules()
    d5 = resample(m1, int(P["tf_minutes"]))
    days = sorted(d5["timestamp"].dt.date.unique())
    day = pd.Timestamp(a.day).date() if a.day else days[-1]
    got = [s for s in detect_all(d5[d5["timestamp"].dt.date <= day], P) if s["day"] == str(day)]
    print(f"ICT 2022 model · {day} · {len(got)} setup(s)  (entry={P['entry']}, target={P['tp']}R, mode={P['mode']})")
    for s in got:
        print("  " + describe(s, P["entry"], P["tp"]))
    print("These are rule-matches, not recommendations — see the Evidence Board for what the backtest says. "
          "Information only — not financial advice.")


if __name__ == "__main__":
    main()
