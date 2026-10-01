"""
mentor_sweeps.py — Lecture 7 ("Double bottoms and other important things") as code, with a
history check of every rule against a shuffled-bars control.

  .venv/bin/python mentor_sweeps.py               # today's picture (15-min + day + week + month)
  .venv/bin/python mentor_sweeps.py --backtest    # 1 year of 15-min (Dhan) + daily since 2019

His rules (numbers in mentor_sweeps_rules.json):
  double bottom   bottom candle + next candle CLOSES above it -> whole bottom candle = buy zone;
                  entry on the retest, SL below it, target = latest swing high, only if R:R >= 1:1;
                  SL hit -> out; day closes back above the zone -> re-enter. Mirror = double top.
  sweep           candle trades above the previous high but closes below it -> down to that
                  range's low (mirror at lows). Same with the latest swing high/low. Any timeframe.
  swing range     open inside the latest 15-min swing range = useless market; a 15-min close
                  outside it -> go that way, T1 = nearest imbalance edge.
  void            gap between two candles / 3-candle imbalance: first return usually does NOT
                  fill it; no close beyond it -> reversal, close beyond it -> continuation.
  stops           untouched swing lows/highs = stop clusters = targets.
  volume          highest-volume candle at a bottom is the last one (futures candles only).
  higher TF       day structure decides; day/week setups -> writing, contract month by days left.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import mentor_structure as ms

log = logging.getLogger("mentor_sweeps")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_sweeps_rules.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def _arr(c: pd.DataFrame):
    return (c["open"].to_numpy(float), c["high"].to_numpy(float), c["low"].to_numpy(float),
            c["close"].to_numpy(float))


# ─────────────────────────────── candle patterns ────────────────────────────
def patterns(c: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    """His confirmations, flagged on the bar that completes them."""
    R = R or rules()
    share = R["patterns"]["star_middle_body_share"]
    O, H, L, C = _arr(c)
    n = len(c)
    body = np.abs(C - O)
    red, green = C < O, C > O
    out = pd.DataFrame(False, index=c.index, columns=["bull_engulf", "bear_engulf", "morning_star",
                                                       "evening_star", "bull_mother_baby", "bear_mother_baby"])
    for i in range(1, n):
        if red[i - 1] and green[i] and C[i] >= O[i - 1] and O[i] <= C[i - 1]:
            out.iat[i, 0] = True
        if green[i - 1] and red[i] and C[i] <= O[i - 1] and O[i] >= C[i - 1]:
            out.iat[i, 1] = True
        inside = H[i] <= H[i - 1] and L[i] >= L[i - 1]
        if red[i - 1] and green[i] and inside:
            out.iat[i, 4] = True
        if green[i - 1] and red[i] and inside:
            out.iat[i, 5] = True
        if i >= 2 and body[i - 2] > 0:
            small = body[i - 1] <= share * body[i - 2]
            if red[i - 2] and small and green[i] and C[i] > (O[i - 2] + C[i - 2]) / 2:
                out.iat[i, 2] = True
            if green[i - 2] and small and red[i] and C[i] < (O[i - 2] + C[i - 2]) / 2:
                out.iat[i, 3] = True
    return out


def _pattern_near(pt: pd.DataFrame, i: int, side: str, span: int = 2) -> str:
    cols = ["bull_engulf", "morning_star", "bull_mother_baby"] if side == "bull" else \
        ["bear_engulf", "evening_star", "bear_mother_baby"]
    lo, hi = max(0, i - span), min(len(pt) - 1, i + span)
    hit = [col for col in cols if pt[col].iloc[lo:hi + 1].any()]
    return ", ".join(h.replace("_", " ") for h in hit)


# ─────────────────────────────── trade replay ───────────────────────────────
def _min_sl(K: dict, tf: str, rule: str | None = None) -> float:
    """Smallest SL distance worth trading (points). A per-rule value in min_sl_points_by_rule wins."""
    by = K.get("min_sl_points_by_rule", {}).get(rule or "", {})
    return float(by.get(tf, K.get("min_sl_points", {}).get(tf, 0)))


def _replay(H, L, C, side: str, start: int, entry: float, sl: float, target: float, max_bars: int,
            include_start: bool = False) -> tuple[str, int, float]:
    """Walk forward. SL is counted first when one candle touches both (conservative)."""
    risk = abs(entry - sl)
    first = start if include_start else start + 1
    end = min(len(C) - 1, start + max_bars)
    for k in range(first, end + 1):
        if side == "bull":
            if L[k] <= sl:
                return "SL", k, -1.0
            if H[k] >= target:
                return "TARGET", k, abs(target - entry) / risk
        else:
            if H[k] >= sl:
                return "SL", k, -1.0
            if L[k] <= target:
                return "TARGET", k, abs(target - entry) / risk
    if end < first:
        return "OPEN", start, 0.0
    mtm = (C[end] - entry) if side == "bull" else (entry - C[end])
    return "TIMEOUT", end, mtm / risk


def _session_end(ts: pd.Series | None, k: int) -> int:
    if ts is None:
        return k
    d = ts.iloc[k].date()
    j = k
    while j + 1 < len(ts) and ts.iloc[j + 1].date() == d:
        j += 1
    return j


# ─────────────────────────────── double bottom / top ────────────────────────
def double_zones(c: pd.DataFrame, R: dict | None = None, tf: str = "15m", intraday: bool = True,
                 refined: bool = False) -> pd.DataFrame:
    """Two-candle zones and what happened to each. refined=True (final doubts class 33:22-33:49): zone only from
    the bottom candle's low up to the SECOND candle's low - smaller risk, but some trades are missed."""
    R = R or rules()
    D, K = R["double_bottom"], R["risk"]
    buf, min_rr = K["sl_buffer_points"], K["min_rr"]
    O, H, L, C = _arr(c)
    n = len(c)
    ts = c["timestamp"] if intraday and "timestamp" in c else None
    pt = patterns(c, R)
    lb, sw, wait, hold = D["bottom_lookback_bars"], D["swing_lookback_bars"], D["max_wait_bars_for_retest"], \
        D["max_bars_in_trade"]
    rows = []
    for i in range(lb, n - 1):
        for side in ("bull", "bear"):
            if side == "bull":
                if not (L[i] <= L[i - lb:i].min() and C[i + 1] > H[i]):
                    continue
                zlo, zhi = L[i], H[i]
                if refined and L[i] < L[i + 1] < H[i]:
                    zhi = L[i + 1]
                target = H[max(0, i - sw):i].max()
                sweep = L[i] < L[i - 1]
            else:
                if not (H[i] >= H[i - lb:i].max() and C[i + 1] < L[i]):
                    continue
                zlo, zhi = L[i], H[i]
                if refined and L[i] < H[i + 1] < H[i]:
                    zlo = H[i + 1]
                target = L[max(0, i - sw):i].min()
                sweep = H[i] > H[i - 1]
            imb = (i + 2 < n) and ((H[i] < L[i + 2]) if side == "bull" else (L[i] > H[i + 2]))
            row = {"tf": tf, "side": side, "bottom_idx": i, "formed_idx": i + 1, "zone_lo": zlo, "zone_hi": zhi,
                   "target": target, "sweep": bool(sweep), "imbalance": bool(imb),
                   "pattern": _pattern_near(pt, i + 1, side), "status": "WAITING", "entry_idx": None,
                   "entry": None, "sl": None, "rr": None, "result": "", "r": np.nan, "reentry_r": np.nan}
            sl = zlo - buf if side == "bull" else zhi + buf
            # wait for the retest; if the target comes first, the move happened without us
            for k in range(i + 2, min(n, i + 2 + wait)):
                if side == "bull":
                    if H[k] >= target and L[k] > zhi:
                        row["status"] = "MISSED (target before retest)"
                        break
                    if L[k] <= zhi:
                        if O[k] <= sl:
                            row["status"] = "GAPPED THROUGH"
                            break
                        entry = min(O[k], zhi)
                        row.update(entry_idx=k, entry=entry)
                        break
                else:
                    if L[k] <= target and H[k] < zlo:
                        row["status"] = "MISSED (target before retest)"
                        break
                    if H[k] >= zlo:
                        if O[k] >= sl:
                            row["status"] = "GAPPED THROUGH"
                            break
                        entry = max(O[k], zlo)
                        row.update(entry_idx=k, entry=entry)
                        break
            else:
                if i + 2 + wait <= n:
                    row["status"] = "NO RETEST"
            if row["entry_idx"] is not None:
                entry, k = row["entry"], row["entry_idx"]
                risk = abs(entry - sl)
                reward = (target - entry) if side == "bull" else (entry - target)
                rr = reward / risk if risk > 0 else 0.0
                row.update(sl=sl, rr=round(rr, 2))
                if reward <= 0:
                    row["status"] = "NO ROOM (target behind entry)"
                else:
                    res, m, r = _replay(H, L, C, side, k, entry, sl, target, hold, include_start=True)
                    row.update(status="TAKEN" if rr >= min_rr else "SKIPPED (R:R < 1)", result=res, r=r)
                    # his re-entry: SL hit, but the DAY closes back beyond the zone edge
                    if res == "SL" and R["double_bottom"]["reentry_on_day_close"]:
                        e = _session_end(ts, m)
                        back = C[e] > zlo if side == "bull" else C[e] < zhi
                        if back and e + 1 < n:
                            nsl = (L[m:e + 1].min() - buf) if side == "bull" else (H[m:e + 1].max() + buf)
                            ne = C[e]
                            nrisk, nrew = abs(ne - nsl), (target - ne) if side == "bull" else (ne - target)
                            if nrisk > 0 and nrew > 0 and nrew / nrisk >= min_rr:
                                _, _, rr2 = _replay(H, L, C, side, e, ne, nsl, target, hold)
                                row["reentry_r"] = rr2
            rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────────────── sweeps ─────────────────────────────────────
def sweep_signals(c: pd.DataFrame, R: dict | None = None, tf: str = "15m", major: bool = False) -> pd.DataFrame:
    """Candle sweeps (previous candle) and swing sweeps (latest confirmed swing)."""
    R = R or rules()
    S, K = R["sweep"], R["risk"]
    poke, buf, min_rr = S["min_poke_points"], K["sl_buffer_points"], K["min_rr"]
    hold = S["max_bars"].get(tf, 10)
    O, H, L, C = _arr(c)
    n = len(c)
    rows = []

    def add(kind, side, i, target, level):
        entry = C[i]
        sl = H[i] + buf if side == "bear" else L[i] - buf
        reward = (entry - target) if side == "bear" else (target - entry)
        risk = abs(entry - sl)
        if reward <= 0 or risk <= 0:
            return
        rr = reward / risk
        res, _, r = _replay(H, L, C, side, i, entry, sl, target, hold)
        rows.append({"tf": tf, "kind": kind, "side": side, "idx": i, "level": level, "entry": entry, "sl": sl,
                     "target": target, "rr": round(rr, 2), "passes_rr": rr >= min_rr and risk >= _min_sl(K, tf, f"{kind}_sweep"),
                     "result": res, "r": r})

    for i in range(1, n):
        up = H[i] > H[i - 1] + poke and C[i] < H[i - 1]
        dn = L[i] < L[i - 1] - poke and C[i] > L[i - 1]
        if up and not dn:
            add("candle", "bear", i, L[i - 1], H[i - 1])
        elif dn and not up:
            add("candle", "bull", i, H[i - 1], L[i - 1])

    # major=True: Lecture 6 - swings inside the previous swing's range are ignored (like inside candles)
    piv, _ = ms.pivots(c.reset_index(drop=True), negate_inside=major)
    p = 0
    piv = sorted(piv, key=lambda x: x.confirm)
    used = set()
    hs, ls = [], []
    for i in range(n):
        while p < len(piv) and piv[p].confirm < i:
            (hs if piv[p].kind == "H" else ls).append(piv[p])
            p += 1
        for st_ in (hs, ls):
            while st_ and st_[-1].dropped_at is not None and st_[-1].dropped_at < i:
                st_.pop()
        last_h = hs[-1] if hs else None
        last_l = ls[-1] if ls else None
        if last_h and last_l:
            if H[i] > last_h.price + poke and C[i] < last_h.price and ("H", last_h.idx) not in used:
                used.add(("H", last_h.idx))
                add("swing", "bear", i, last_l.price, last_h.price)
            elif L[i] < last_l.price - poke and C[i] > last_l.price and ("L", last_l.idx) not in used:
                used.add(("L", last_l.idx))
                add("swing", "bull", i, last_h.price, last_l.price)
            if C[i] > last_h.price:
                used.add(("H", last_h.idx))       # a close above = broken, no longer sweepable
            if C[i] < last_l.price:
                used.add(("L", last_l.idx))
    return pd.DataFrame(rows)


# ─────────────────────────────── voids ──────────────────────────────────────
def voids(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """Voids between two candles and 3-candle imbalances; what the FIRST return did."""
    R = R or rules()
    V = R["void"]
    mn = V["min_points"].get(tf, 5)
    O, H, L, C = _arr(c)
    n = len(c)
    raw = []
    for i in range(1, n):
        if L[i] > H[i - 1] + mn:
            raw.append(("gap", "bull", i, H[i - 1], L[i]))
        elif H[i] < L[i - 1] - mn:
            raw.append(("gap", "bear", i, H[i], L[i - 1]))
    for g in ms.imbalances(c):
        if g["top"] - g["bottom"] >= mn:
            raw.append(("imbalance", g["side"], g["idx"] + 2, g["bottom"], g["top"]))
    rows = []
    for kind, side, f, bot, top in raw:
        hgt = top - bot
        k = next((j for j in range(f + 1, n) if (L[j] <= top if side == "bull" else H[j] >= bot)), None)
        row = {"tf": tf, "kind": kind, "side": side, "formed_idx": f, "bottom": bot, "top": top, "touch_idx": k,
               "first_touch": "UNTOUCHED", "close_beyond": None, "close_rule_ok": None}
        if k is not None:
            away = V["away_multiple"] * max(hgt, H[k] - L[k])
            outcome = None
            for j in range(k, min(n, k + V["look_bars"])):
                if side == "bull":
                    if L[j] <= bot:
                        outcome = "FILLED"
                        break
                    if j > k and H[j] >= top + away:
                        outcome = "REJECTED"
                        break
                else:
                    if H[j] >= top:
                        outcome = "FILLED"
                        break
                    if j > k and L[j] <= bot - away:
                        outcome = "REJECTED"
                        break
            row["first_touch"] = outcome or "UNDECIDED"
            # close rule: does the touching candle close beyond the far edge?
            beyond = C[k] < bot if side == "bull" else C[k] > top
            rng = max(H[k] - L[k], 1e-9)
            want_up = (not beyond) if side == "bull" else beyond
            ok = None
            for j in range(k + 1, min(n, k + 1 + 8)):
                up_hit, dn_hit = H[j] >= C[k] + rng, L[j] <= C[k] - rng
                if up_hit and dn_hit:
                    ok = False              # ambiguous candle counts against the rule
                    break
                if up_hit or dn_hit:
                    ok = up_hit == want_up
                    break
            row.update(close_beyond=bool(beyond), close_rule_ok=ok)
        rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────────────── Lecture 6: body voids ──────────────────────
def body_voids(c: pd.DataFrame, R: dict | None = None, tf: str = "15m") -> pd.DataFrame:
    """Lecture 6 (48:18-59:46): a void is the gap between two candles' BODIES (one candle's close/open to the
    next one's). Price comes back into it; if the candle does NOT close beyond the far edge -> trade away
    from it (buy at a void below price, sell at a void above), SL beyond that candle's wick, target = the
    extreme made since the void formed (the previous high/low, where the stops sit). A close beyond it = no
    trade. Size doesn't matter. Useful while the 15-min structure is not complete."""
    R = R or rules()
    B, K = R["body_void"], R["risk"]
    mn, buf, min_rr = B["min_points"].get(tf, 1), K["sl_buffer_points"], K["min_rr"]
    hold = B["max_bars"].get(tf, 26)
    O, H, L, C = _arr(c)
    n = len(c)
    bt, bb = np.maximum(O, C), np.minimum(O, C)
    rows = []
    for i in range(1, n):
        if bb[i] > bt[i - 1] + mn:
            side, bot, top = "bull", bt[i - 1], bb[i]
        elif bt[i] < bb[i - 1] - mn:
            side, bot, top = "bear", bt[i], bb[i - 1]
        else:
            continue
        k = next((j for j in range(i + 1, n) if (L[j] <= top if side == "bull" else H[j] >= bot)), None)
        row = {"tf": tf, "side": side, "formed_idx": i, "bottom": bot, "top": top, "touch_idx": k, "closed_beyond": None,
               "entry": None, "sl": None, "target": None, "rr": None, "passes_rr": False, "result": "", "r": np.nan}
        if k is None:
            row["result"] = "UNTOUCHED"
            rows.append(row)
            continue
        beyond = C[k] < bot if side == "bull" else C[k] > top
        row["closed_beyond"] = bool(beyond)
        if beyond:
            row["result"] = "CLOSED THROUGH (no trade)"
            rows.append(row)
            continue
        entry = C[k]
        sl = L[k] - buf if side == "bull" else H[k] + buf
        target = H[i:k].max() if side == "bull" else L[i:k].min()
        reward = (target - entry) if side == "bull" else (entry - target)
        risk = abs(entry - sl)
        if reward <= 0 or risk <= 0:
            row["result"] = "NO ROOM"
            rows.append(row)
            continue
        rr = reward / risk
        res, _, r = _replay(H, L, C, side, k, entry, sl, target, hold)
        row.update(entry=entry, sl=sl, target=target, rr=round(rr, 2), passes_rr=rr >= min_rr and risk >= _min_sl(K, tf, "body_void"),
                   result=res, r=r)
        rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────────────── Lecture 6: volume-profile divergence ──────
def futures_minutes(days: int = 0) -> pd.DataFrame | None:
    """Stored Nifty futures 1-min candles with real volume (your data/candles store), all days if 0."""
    try:
        import candle_source
        try:
            _, fut = candle_source._load_stored_candles(days)
        except Exception:
            _, fut = candle_source.fetch(days or 60)
        if fut is None or not len(fut):
            return None
        f = fut.copy()
        f["timestamp"] = pd.to_datetime(f["timestamp"])
        f["volume"] = pd.to_numeric(f["volume"], errors="coerce").fillna(0)
        return f if f["volume"].sum() > 0 else None
    except Exception as exc:
        log.info("futures candles with volume unavailable: %s", exc)
        return None


def vp_divergence(m: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    """His volume-profile divergence (1:02:06-1:06:05): day's point of control (price with the most volume)
    vs its close. Day 1 closed above its POC; day 2 closed BELOW day 1's close while its POC moved UP (volume on
    the upper side) -> divergence -> momentum next day (either way): he buys a call AND a put. Mirror too.
    Test: is the next day's range bigger than usual after a divergence day?"""
    R = R or rules()
    V = R["volume_profile"]
    x = m.copy()
    x["day"] = x["timestamp"].dt.date
    x["tp"] = (x["high"] + x["low"] + x["close"]) / 3
    rows = []
    for d, g in x.groupby("day"):
        if len(g) < V["min_bars_per_day"]:
            continue
        b = (g["tp"] / V["bin_points"]).round() * V["bin_points"]
        vol = g.groupby(b)["volume"].sum()
        rows.append({"day": d, "poc": float(vol.idxmax()) if vol.sum() > 0 else np.nan, "open": g["open"].iloc[0],
                     "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].iloc[-1]})
    t = pd.DataFrame(rows)
    if len(t) < 3:
        return t
    t["range"] = t["high"] - t["low"]
    t["avg_range"] = t["range"].rolling(V["avg_days"], min_periods=3).mean().shift(1)
    pc, pp = t["close"].shift(1), t["poc"].shift(1)
    t["divergence"] = np.where((t["close"] < pc) & (t["poc"] > pp) & (t["poc"] > t["close"]), "BEARISH CLOSE, POC UP",
                      np.where((t["close"] > pc) & (t["poc"] < pp) & (t["poc"] < t["close"]), "BULLISH CLOSE, POC DOWN", ""))
    t["next_range_x_avg"] = (t["range"].shift(-1) / t["avg_range"].shift(-1)).round(2)
    t["next_momentum"] = t["next_range_x_avg"] >= V["momentum_x_avg_range"]
    return t


# ─────────────────────────────── swing range at the open ────────────────────
def swing_range_days(c: pd.DataFrame, R: dict | None = None) -> pd.DataFrame:
    R = R or rules()
    K = R["risk"]
    buf, min_rr = K["sl_buffer_points"], K["min_rr"]
    c = c.reset_index(drop=True)
    O, H, L, C = _arr(c)
    n = len(c)
    piv, _ = ms.pivots(c)
    gaps = ms.imbalances(c)
    d = c["timestamp"].dt.date
    starts = list(np.flatnonzero(d.ne(d.shift()).to_numpy()))
    rows = []
    for si, j in enumerate(starts):
        e = (starts[si + 1] - 1) if si + 1 < len(starts) else n - 1
        known = [p for p in piv if p.confirm < j]
        hs = [p for p in known if p.kind == "H"]
        ls = [p for p in known if p.kind == "L"]
        if not hs or not ls:
            continue
        sh, sl_ = hs[-1].price, ls[-1].price
        if sh <= sl_:
            continue
        rng = H[j:e + 1].max() - L[j:e + 1].min()
        where = "INSIDE" if sl_ < O[j] < sh else "ABOVE" if O[j] >= sh else "BELOW"
        row = {"day": d.iloc[j], "swing_high": sh, "swing_low": sl_, "open": O[j], "open_is": where,
               "day_range": rng, "trend_day": abs(C[e] - O[j]) >= R["swing_range"]["trend_day_body_share"] * rng,
               "break": "", "trade": "", "rr": None, "r": np.nan, "break_time": None, "entry": None,
               "stop": None, "target": None}
        for k in range(j, e + 1):
            side = "bull" if C[k] > sh else "bear" if C[k] < sl_ else None
            if side is None:
                continue
            entry = C[k]
            stop = L[k] - buf if side == "bull" else H[k] + buf
            if side == "bull":
                cand = [g["bottom"] for g in gaps if g["idx"] + 2 < k and (g["filled_idx"] is None or g["filled_idx"] > k)
                        and g["bottom"] > entry]
                target = min(cand) if cand else None
            else:
                cand = [g["top"] for g in gaps if g["idx"] + 2 < k and (g["filled_idx"] is None or g["filled_idx"] > k)
                        and g["top"] < entry]
                target = max(cand) if cand else None
            row["break"] = "UP" if side == "bull" else "DOWN"
            row.update(break_time=c["timestamp"].iloc[k], entry=entry, stop=stop, target=target)
            if target is None:
                row["trade"] = "NO IMBALANCE TARGET"
                break
            rr = abs(target - entry) / abs(entry - stop)
            row["rr"] = round(rr, 2)
            if rr < min_rr:
                row["trade"] = "SKIPPED (R:R < 1)"
                break
            res, _, r = _replay(H, L, C, side, k, entry, stop, target, 26)
            row.update(trade=res, r=r)
            break
        rows.append(row)
    return pd.DataFrame(rows)


# ─────────────────────────────── stops / volume / HTF ───────────────────────
def stop_clusters(c: pd.DataFrame, R: dict | None = None) -> dict:
    """Untouched swing lows below and swing highs above the last price (his stop-loss pools)."""
    R = R or rules()
    S = R["stops"]
    c = c.reset_index(drop=True)
    O, H, L, C = _arr(c)
    piv, _ = ms.pivots(c)
    last = C[-1]
    below, above = [], []
    for p in piv:
        after = slice(p.idx + 1, len(c))
        if p.kind == "L" and p.price < last and (L[after].min() if p.idx + 1 < len(c) else np.inf) > p.price:
            below.append(p.price)
        if p.kind == "H" and p.price > last and (H[after].max() if p.idx + 1 < len(c) else -np.inf) < p.price:
            above.append(p.price)

    def cluster(xs, down):
        xs = sorted(xs, reverse=not down)[: 12]
        if len(xs) < S["cluster_min_count"]:
            return None
        xs = sorted(xs, reverse=down)                   # nearest first
        grp = [x for x in xs if abs(x - xs[0]) <= S["cluster_points"]]
        return grp if len(grp) >= S["cluster_min_count"] else None

    return {"below": sorted(below, reverse=True), "above": sorted(above),
            "cluster_below": cluster(below, True), "cluster_above": cluster(above, False)}


def volume_climax(c: pd.DataFrame, R: dict | None = None) -> pd.Series | None:
    R = R or rules()
    if "volume" not in c or float(pd.to_numeric(c["volume"], errors="coerce").fillna(0).sum()) <= 0:
        return None
    V = R["volume"]
    v = pd.to_numeric(c["volume"], errors="coerce").fillna(0)
    avg = v.rolling(V["look_bars"], min_periods=5).mean().shift(1)
    return v >= V["climax_multiple"] * avg


def daily_frame(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x.index = pd.to_datetime(x.index)
    return x.rename_axis("timestamp").reset_index()[["timestamp", "open", "high", "low", "close"]]


def resample(d: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = d.set_index("timestamp")[["open", "high", "low", "close"]]
    r = x.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return r.reset_index()


def day_bias(daily: pd.DataFrame) -> pd.Series:
    """His day structure (BOS/CHoCH on daily closes, Lecture 4 engine) known at each day's close.
    Returns +1/-1/0 indexed by date, shifted so day D gets the bias as of D-1's close."""
    res = ms.analyse(daily.reset_index(drop=True))
    trend = np.zeros(len(daily), dtype=int)
    ev = sorted((e for e in res.events if e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN")),
                key=lambda e: e["idx"])
    cur, p = 0, 0
    for i in range(len(daily)):
        while p < len(ev) and ev[p]["idx"] <= i:
            cur = 1 if ev[p]["kind"].endswith("UP") else -1
            p += 1
        trend[i] = cur
    s = pd.Series(trend, index=pd.to_datetime(daily["timestamp"]).dt.date)
    return s.shift(1).fillna(0).astype(int)


def contract_choice(today: date, R: dict | None = None) -> str:
    """Day-timeframe trade: current month only if the signal is early (enough trading days left)."""
    R = R or rules()
    need = R["higher_timeframe"]["current_month_if_trading_days_left_at_least"]

    def last_tuesday(y, m):
        last = (pd.Timestamp(y, m, 1) + pd.offsets.MonthEnd(0)).date()
        return last - pd.Timedelta(days=(last.weekday() - 1) % 7)

    mexp = last_tuesday(today.year, today.month)        # NSE monthly expiry: last Tuesday (holiday -> earlier day)
    if mexp < today:
        nxt = pd.Timestamp(today) + pd.offsets.MonthBegin(1)
        mexp = last_tuesday(nxt.year, nxt.month)
    left = int(np.busday_count(today, mexp)) if mexp >= today else 0
    if left >= need:
        return f"current-month contract is fine ({left} trading days to the monthly expiry {mexp:%d-%b})"
    return f"take the NEXT month (only {left} trading days left to {mexp:%d-%b}); he prefers next or the one after"


# ─────────────────────────────── live picture ───────────────────────────────
def live(c15: pd.DataFrame, daily: pd.DataFrame | None, R: dict | None = None) -> dict:
    R = R or rules()
    c15 = c15.reset_index(drop=True)
    out = {"tf": "15m"}
    dz = double_zones(c15, R)
    out["zones"] = dz[(dz["status"] == "WAITING") & (dz["formed_idx"] >= len(c15) - 30)] if len(dz) else dz
    sw = sweep_signals(c15, R)
    out["sweeps"] = sw[sw["idx"] >= len(c15) - 26] if len(sw) else sw
    vd = voids(c15, R)
    out["voids"] = vd[vd["first_touch"] == "UNTOUCHED"] if len(vd) else vd
    bv = body_voids(c15, R)
    out["body_voids"] = bv[bv["result"] == "UNTOUCHED"] if len(bv) else bv
    out["body_signals"] = bv[(bv["touch_idx"].notna()) & (bv["touch_idx"] >= len(c15) - 26) & bv["entry"].notna()] \
        if len(bv) else bv
    fm = futures_minutes(15)
    if fm is not None:
        vp = vp_divergence(fm, R)
        out["vp"] = vp.iloc[-1].to_dict() if len(vp) and "divergence" in vp else None
    else:
        out["vp"] = None
    sr = swing_range_days(c15, R)
    out["today_range"] = sr.iloc[-1].to_dict() if len(sr) and sr.iloc[-1]["day"] == c15["timestamp"].iloc[-1].date() else None
    out["stops"] = stop_clusters(c15, R)
    vc = volume_climax(c15, R)
    out["volume_climax"] = None if vc is None else bool(vc.iloc[-3:].any())
    out["patterns"] = {k: bool(v) for k, v in patterns(c15, R).iloc[-1].items() if v}
    if daily is not None and len(daily) > 30:
        d = daily.reset_index(drop=True)
        out["day"] = _last_candle_read(d, "day")
        out["week"] = _last_candle_read(resample(d, "W-FRI"), "week")
        out["month"] = _last_candle_read(resample(d, "MS"), "month")
        out["day_trend"] = int(day_bias(d).iloc[-1]) if len(d) else 0
        res = ms.analyse(d)
        out["day_trend_now"] = res.trend
        out["contract"] = contract_choice(date.today(), R)
    return out


def _last_candle_read(d: pd.DataFrame, name: str) -> dict | None:
    """His candle read on the latest completed/forming candle vs the previous one."""
    if len(d) < 3:
        return None
    O, H, L, C = _arr(d)
    pt = patterns(d)
    i = len(d) - 1
    txt = []
    bias = 0
    if H[i] > H[i - 1] and C[i] < H[i - 1]:
        txt.append(f"swept the previous {name}'s high {H[i - 1]:,.0f} and is below it -> intent DOWN, "
                   f"first target {L[i - 1]:,.0f}")
        bias -= 1
    if L[i] < L[i - 1] and C[i] > L[i - 1]:
        txt.append(f"swept the previous {name}'s low {L[i - 1]:,.0f} and is above it -> intent UP, "
                   f"first target {H[i - 1]:,.0f}")
        bias += 1
    if C[i] > H[i - 1]:
        txt.append(f"above the previous {name}'s high {H[i - 1]:,.0f} -> ready to go up")
        bias += 1
    if C[i] < L[i - 1]:
        txt.append(f"below the previous {name}'s low {L[i - 1]:,.0f} -> ready to go down")
        bias -= 1
    pats = [k.replace("_", " ") for k, v in pt.iloc[i].items() if v]
    if pats:
        txt.append("pattern: " + ", ".join(pats))
    return {"name": name, "start": pd.Timestamp(d["timestamp"].iloc[i]), "open": O[i], "high": H[i], "low": L[i],
            "close": C[i], "prev_high": H[i - 1], "prev_low": L[i - 1], "bias": int(np.sign(bias)),
            "text": "; ".join(txt) or f"inside the previous {name}'s range (no sweep)"}


# ─────────────────────────────── history check ──────────────────────────────
def _shuffle(c: pd.DataFrame, seed: int = 42, k: int | None = None) -> pd.DataFrame:
    """Random-order control. k shuffles (default history.control_shuffles = 5) are chained end to end, each on
    its own later calendar span, so every control is judged on k x the trades - one shuffle alone was too noisy
    (29-Sep-2026: one extra day flipped the daily controls from +0.00 R to +0.27 R)."""
    if k is None:
        k = int(rules().get("history", {}).get("control_shuffles", 5))
    if k <= 1:
        return _shuffle_one(c, seed)
    ts = pd.to_datetime(c["timestamp"])
    span = (ts.max() - ts.min()) + pd.Timedelta(days=7)
    parts, start = [], None
    for j in range(k):
        x = _shuffle_one(c, seed + j, start)
        x["timestamp"] = ts.values + j * span
        start = float(x["close"].iloc[-1])
        parts.append(x)
    return pd.concat(parts, ignore_index=True)


def _shuffle_one(c: pd.DataFrame, seed: int = 42, start: float | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rel = pd.DataFrame({"o": c["open"].values - c["close"].shift(1).bfill().values,
                        "h": c["high"].values - c["open"].values, "l": c["low"].values - c["open"].values,
                        "c": c["close"].values - c["open"].values}).iloc[rng.permutation(len(c))].reset_index(drop=True)
    cl = (float(c["close"].iloc[0]) if start is None else start) + (rel["o"] + rel["c"]).cumsum()
    op = cl - rel["c"]
    x = pd.DataFrame({"timestamp": c["timestamp"].values, "open": op.values, "high": (op + rel["h"]).values,
                      "low": (op + rel["l"]).values, "close": cl.values})
    x["high"] = x[["open", "high", "close"]].max(axis=1)
    x["low"] = x[["open", "low", "close"]].min(axis=1)
    return x


def _stats(df: pd.DataFrame, minimum: int, label: str, tf: str, res_col="result", r_col="r") -> dict:
    n = len(df)
    if n < minimum:
        return {"Setup": label, "TF": tf, "Trades": n, "Target hit %": "INSUFFICIENT SAMPLE", "SL hit %": "",
                "Avg R per trade": ""}
    return {"Setup": label, "TF": tf, "Trades": n,
            "Target hit %": round(100 * (df[res_col] == "TARGET").mean(), 1),
            "SL hit %": round(100 * (df[res_col] == "SL").mean(), 1),
            "Avg R per trade": round(float(df[r_col].mean()), 2)}


def trend_series(c: pd.DataFrame) -> np.ndarray:
    """His 15-min structure trend (+1 / -1 / 0) known at each bar's close, from the Lecture 5-6 engine."""
    res = ms.analyse(c.reset_index(drop=True))
    ev = sorted((e for e in res.events if e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN")), key=lambda e: e["idx"])
    out = np.zeros(len(c), dtype=int)
    cur, p = 0, 0
    for i in range(len(c)):
        while p < len(ev) and ev[p]["idx"] <= i:
            cur = 1 if ev[p]["kind"].endswith("UP") else -1
            p += 1
        out[i] = cur
    return out


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    minimum = R["history"]["min_sample_size"]
    h15 = ms.load_history(ms.rules())
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = daily_frame(dd) if dd is not None else None
    except Exception as exc:
        log.warning("daily history unavailable: %s", exc)
        daily = None
    frames = {}
    if h15 is not None and len(h15) > 500:
        frames["15m"] = h15.reset_index(drop=True)
    if daily is not None and len(daily) > 200:
        frames["1d"] = daily
        frames["1w"] = resample(daily, "W-FRI")
        frames["1M"] = resample(daily, "MS")
    if not frames:
        return {"error": "no history (Dhan token / daily OHLC?)"}

    dz_rows, sw_rows, vd_rows = [], [], []
    bias = day_bias(frames["1d"]) if "1d" in frames else None
    for tf, c in frames.items():
        intr = tf == "15m"
        for tag, cc in (("", c), ("CONTROL ", _shuffle(c))):
            ctl = bool(tag)
            dz = double_zones(cc, R, tf, intraday=intr)
            if len(dz):
                taken = dz[dz["status"] == "TAKEN"]
                skipped = dz[dz["status"] == "SKIPPED (R:R < 1)"]
                if not ctl:
                    dz_rows.append(_stats(taken, minimum, "Double bottom/top, his 1:1 filter (TAKEN)", tf))
                    dz_rows.append(_stats(skipped, minimum, "  ...the ones his filter skipped (R:R < 1)", tf))
                    for nm, m in (("  bottoms (buy)", taken["side"] == "bull"), ("  tops (sell)", taken["side"] == "bear"),
                                  ("  with imbalance after it", taken["imbalance"]),
                                  ("  with a candle pattern", taken["pattern"] != "")):
                        dz_rows.append(_stats(taken[m], minimum, nm, tf))
                    if intr and bias is not None and len(taken):
                        dts = cc["timestamp"].dt.date.iloc[taken["bottom_idx"].astype(int)].values
                        b = pd.Series(dts).map(bias).fillna(0).to_numpy()
                        sgn = np.where(taken["side"] == "bull", 1, -1)
                        dz_rows.append(_stats(taken[b == sgn], minimum, "  WITH the day structure", tf))
                        dz_rows.append(_stats(taken[b == -sgn], minimum, "  AGAINST the day structure", tf))
                    if intr:
                        # final doubts class: double bottoms work in a BULLISH market; the top version is less promising
                        tr_ = trend_series(cc)
                        tb = tr_[taken["bottom_idx"].astype(int).to_numpy()]
                        sg = np.where(taken["side"] == "bull", 1, -1)
                        dz_rows.append(_stats(taken[(taken["side"] == "bull") & (tb == 1)], minimum,
                                              "  bottoms in a BULLISH 15-min structure (his rule)", tf))
                        dz_rows.append(_stats(taken[(taken["side"] == "bull") & (tb != 1)], minimum,
                                              "  bottoms in a non-bullish 15-min structure", tf))
                        dz_rows.append(_stats(taken[(taken["side"] == "bear") & (tb == -1)], minimum,
                                              "  tops in a BEARISH 15-min structure", tf))
                        rf = double_zones(cc, R, tf, intraday=intr, refined=True)
                        rft = rf[rf["status"] == "TAKEN"] if len(rf) else rf
                        dz_rows.append(_stats(rft, minimum, "  REFINED zone (to the 2nd candle's low), final class", tf))
                        dz_rows.append({"Setup": "  refined zone: setups missed (no retest of the smaller zone)", "TF": tf,
                                        "Trades": int((rf["status"] == "NO RETEST").sum() + (rf["status"] ==
                                                      "MISSED (target before retest)").sum()) if len(rf) else 0,
                                        "Target hit %": "", "SL hit %": "", "Avg R per trade": ""})
                    re = taken[taken["reentry_r"].notna()]
                    if len(re) >= minimum:
                        dz_rows.append({"Setup": "  his re-entry after SL (day closed back)", "TF": tf, "Trades": len(re),
                                        "Target hit %": "", "SL hit %": "",
                                        "Avg R per trade": round(float(re["reentry_r"].mean()), 2)})
                    else:
                        dz_rows.append({"Setup": "  his re-entry after SL (day closed back)", "TF": tf, "Trades": len(re),
                                        "Target hit %": "INSUFFICIENT SAMPLE", "SL hit %": "", "Avg R per trade": ""})
                else:
                    dz_rows.append(_stats(taken, minimum, "CONTROL: double bottom/top on shuffled bars", tf))
            sw = sweep_signals(cc, R, tf)
            if tf == "15m":
                swm = sweep_signals(cc, R, tf, major=True)
                if len(swm):
                    y = swm[(swm["kind"] == "swing") & swm["passes_rr"]]
                    sw_rows.append(_stats(y, minimum, ("CONTROL: " if ctl else "") +
                                          "Swing sweep, MAJOR swings only (Lecture 6), R:R >= 1", tf))
            if len(sw):
                for kind in ("candle", "swing"):
                    x = sw[(sw["kind"] == kind)]
                    if not ctl:
                        sw_rows.append(_stats(x[x["passes_rr"]], minimum, f"{kind.title()} sweep, R:R >= 1", tf))
                        sw_rows.append(_stats(x[~x["passes_rr"]], minimum, f"  {kind} sweep, R:R < 1 (he skips)", tf))
                        if intr and bias is not None and kind == "candle":
                            y = x[x["passes_rr"]]
                            dts = cc["timestamp"].dt.date.iloc[y["idx"].astype(int)].values
                            b = pd.Series(dts).map(bias).fillna(0).to_numpy()
                            sgn = np.where(y["side"] == "bull", 1, -1)
                            sw_rows.append(_stats(y[b == sgn], minimum, "  candle sweep WITH the day structure", tf))
                            sw_rows.append(_stats(y[b == -sgn], minimum, "  candle sweep AGAINST the day structure", tf))
                    else:
                        sw_rows.append(_stats(x[x["passes_rr"]], minimum, f"CONTROL: {kind} sweep on shuffled bars", tf))
            if tf in ("15m", "1d"):
                vd = voids(cc, R, tf)
                if len(vd):
                    t = vd[vd["first_touch"].isin(["FILLED", "REJECTED"])]
                    for kind in ("gap", "imbalance"):
                        y = t[t["kind"] == kind]
                        cr = vd[(vd["kind"] == kind) & vd["close_rule_ok"].notna()]
                        lab = ("CONTROL: " if ctl else "") + ("Void between two candles" if kind == "gap" else "3-candle imbalance")
                        vd_rows.append({"Void": lab, "TF": tf, "First returns": len(y),
                                        "NOT filled first time %": round(100 * (y["first_touch"] == "REJECTED").mean(), 1)
                                        if len(y) >= minimum else "INSUFFICIENT SAMPLE",
                                        "Close rule right %": round(100 * cr["close_rule_ok"].astype(bool).mean(), 1)
                                        if len(cr) >= minimum else "INSUFFICIENT SAMPLE"})
    bv_rows = []
    for tf in ("15m", "1d"):
        if tf not in frames:
            continue
        for lab, cc in (("", frames[tf]), ("CONTROL: ", _shuffle(frames[tf]))):
            bv = body_voids(cc, R, tf)
            if not len(bv):
                continue
            tr_ = bv[bv["result"].isin(["TARGET", "SL", "TIMEOUT"])]
            touched = bv[bv["closed_beyond"].notna()]
            bv_rows.append({**_stats(tr_[tr_["passes_rr"]], minimum, lab + "Body void, no close beyond, R:R >= 1", tf),
                            "Closed through on first return %": round(100 * touched["closed_beyond"].astype(bool).mean(), 1)
                            if len(touched) >= minimum else "INSUFFICIENT SAMPLE"})
            if not lab:
                bv_rows.append({**_stats(tr_[~tr_["passes_rr"]], minimum, "  body void, R:R < 1 (he skips)", tf),
                                "Closed through on first return %": ""})
    vol_rows = []
    fm = futures_minutes(0)
    if fm is not None:
        vp = vp_divergence(fm, R)
        if len(vp) and "divergence" in vp:
            ok = vp[vp["next_range_x_avg"].notna()]
            dv, nd = ok[ok["divergence"] != ""], ok[ok["divergence"] == ""]
            for lab, x in (("Day AFTER a volume-profile divergence", dv), ("Day after any other day", nd)):
                vol_rows.append({"Volume rule": lab, "Days": len(x),
                                 "Big-move days %": round(100 * x["next_momentum"].mean(), 1) if len(x) >= minimum
                                 else ("INSUFFICIENT SAMPLE" if len(x) else ""),
                                 "Avg range vs normal": round(float(x["next_range_x_avg"].mean()), 2) if len(x) else ""})
        f15 = (fm.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
               .groupby(pd.Grouper(freq="15min", origin="start_day", offset="15min"))
               .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())
        vc = volume_climax(f15, R)
        if vc is not None:
            O_, H_, L_, C_ = _arr(f15)
            res_ = []
            for i in np.flatnonzero(vc.to_numpy()):
                if i < 5 or i + 8 >= len(f15):
                    continue
                at_low = L_[i] <= L_[i - 5:i].min()
                at_high = H_[i] >= H_[i - 5:i].max()
                if not (at_low or at_high):
                    continue
                side = "bull" if at_low else "bear"
                rng = max(H_[i] - L_[i], 1e-9)
                up, dn = H_[i + 1:i + 9].max() - C_[i], C_[i] - L_[i + 1:i + 9].min()
                res_.append((side, bool(C_[i] < O_[i]), up > dn if side == "bull" else dn > up))
            # final class: he has seen it only for RED high-volume candles at a BOTTOM, not at tops
            for lab, sel in (("High-volume RED candle at a 15-min low -> bounce (his case)",
                              [x[2] for x in res_ if x[0] == "bull" and x[1]]),
                             ("High-volume candle at a 15-min high -> falls (he has NOT seen this work)",
                              [x[2] for x in res_ if x[0] == "bear"])):
                vol_rows.append({"Volume rule": lab, "Days": len(sel),
                                 "Big-move days %": round(100 * np.mean(sel), 1) if len(sel) >= minimum
                                 else "INSUFFICIENT SAMPLE", "Avg range vs normal": "(% = moved more his way, next 2 h)"})
    else:
        vol_rows.append({"Volume rule": "No stored futures candles with volume found (data/candles)", "Days": 0,
                         "Big-move days %": "", "Avg range vs normal": ""})
    sr_tab = pd.DataFrame()
    sr_break = {}
    if "15m" in frames:
        sr = swing_range_days(frames["15m"], R)
        csr = swing_range_days(_shuffle(frames["15m"]), R)
        rows = []
        for lab, x in (("Open INSIDE the swing range ('useless')", sr[sr["open_is"] == "INSIDE"]),
                       ("Open OUTSIDE the swing range", sr[sr["open_is"] != "INSIDE"])):
            rows.append({"Days": lab, "Count": len(x),
                         "Avg day range (pts)": round(float(x["day_range"].mean()), 0) if len(x) else "",
                         "Trend days %": round(100 * x["trend_day"].mean(), 1) if len(x) >= minimum else "INSUFFICIENT SAMPLE"})
        for lab, x in (("Break trade (15m close beyond swing, T1 = imbalance edge)", sr),
                       ("CONTROL: same on shuffled bars", csr)):
            t = x[x["trade"].isin(["TARGET", "SL", "TIMEOUT"])] if len(x) else x
            st_ = _stats(t, minimum, lab, "15m", res_col="trade") if len(t) else {"Target hit %": "", "SL hit %": "",
                                                                                     "Avg R per trade": "", "Trades": 0}
            sr_break["control" if lab.startswith("CONTROL") else "his"] = st_
            rows.append({"Days": lab, "Count": st_["Trades"], "Avg day range (pts)": "",
                         "Trend days %": f"target {st_['Target hit %']} / SL {st_['SL hit %']} / avg R {st_['Avg R per trade']}"})
        sr_tab = pd.DataFrame(rows)
    span = {tf: (c["timestamp"].min(), c["timestamp"].max(), len(c)) for tf, c in frames.items()}
    if fm is not None:
        span["futures 1m (volume)"] = (fm["timestamp"].min(), fm["timestamp"].max(), len(fm))
    b = {"span": span, "double": pd.DataFrame(dz_rows), "sweeps": pd.DataFrame(sw_rows),
         "voids": pd.DataFrame(vd_rows), "swing_range": sr_tab, "sr_break": sr_break,
         "body_voids": pd.DataFrame(bv_rows), "volume": pd.DataFrame(vol_rows)}
    b["verdicts"] = verdicts(b, R)
    save_verdicts(b["verdicts"], R)
    return b


# ─────────────────────────────── verdicts (shown on the cards) ──────────────
VERDICT_ORDER = ("swing_sweep_15m", "swing_sweep_major_15m", "swing_sweep_1d", "candle_sweep_15m", "candle_sweep_1d", "sweep_1w",
                 "double_15m", "double_with_day_15m", "double_1d", "reentry_15m", "gap_first_return",
                 "void_close_rule", "useless_open", "swing_break", "body_void_15m", "body_void_1d", "vp_divergence")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cmp(key: str, name: str, his, ctrl, n: int, unit: str = "R", noise: float | None = None) -> dict:
    """his vs control: 'BEATS RANDOM' / 'NO EDGE' / 'WORSE THAN RANDOM' / 'NOT ENOUGH DATA'."""
    h, c = _num(his), _num(ctrl)
    if h is None or c is None:
        return {"key": key, "rule": name, "verdict": "NOT ENOUGH DATA", "detail": f"{n} cases", "level": 0}
    noise = 0.15 if noise is None else noise
    d = h - c
    min_star = int(rules().get("history", {}).get("min_trades_for_star", 200))
    if d >= noise and (unit != "R" or h > 0) and n < min_star:
        v, lv = f"PROMISING, SMALL SAMPLE (< {min_star})", 0
    elif d >= noise and (unit != "R" or h > 0):
        v, lv = "BEATS RANDOM", 1
    elif d <= -noise:
        v, lv = "WORSE THAN RANDOM", -1
    else:
        v, lv = "NO EDGE", 0
    fmt = (lambda x: f"{x:+.2f} R") if unit == "R" else (lambda x: f"{x:.0f}%")
    return {"key": key, "rule": name, "verdict": v, "level": lv, "n": int(n),
            "detail": f"{fmt(h)} vs {fmt(c)} on shuffled bars, {n} cases"}


def verdicts(b: dict, R: dict | None = None) -> dict:
    """Plain-language verdict per rule from a backtest result."""
    R = R or rules()

    def row(tab, col, label, tf):
        t = b[tab]
        if not len(t):
            return None
        x = t[(t[col].str.strip() == label.strip()) & (t["TF"] == tf)]
        return x.iloc[0] if len(x) else None

    def rr(tab, label, tf, ctrl_label, col="Setup"):
        a, c = row(tab, col, label, tf), row(tab, col, ctrl_label, tf)
        return (None if a is None else a["Avg R per trade"], None if c is None else c["Avg R per trade"],
                0 if a is None else int(a["Trades"]))

    out = {}
    for tf in ("15m", "1d"):
        for kind in ("swing", "candle"):
            h, c, n = rr("sweeps", f"{kind.title()} sweep, R:R >= 1", tf, f"CONTROL: {kind} sweep on shuffled bars")
            out[f"{kind}_sweep_{tf}"] = _cmp(f"{kind}_sweep_{tf}", f"{kind.title()} sweep ({'15-min' if tf == '15m' else 'day'})",
                                             h, c, n)
    h_, c_, n_ = rr("sweeps", "Swing sweep, MAJOR swings only (Lecture 6), R:R >= 1", "15m",
                    "CONTROL: Swing sweep, MAJOR swings only (Lecture 6), R:R >= 1")
    out["swing_sweep_major_15m"] = _cmp("swing_sweep_major_15m", "Swing sweep on MAJOR swings only (15-min, Lecture 6)",
                                        h_, c_, n_)
    hs, cs, ns = rr("sweeps", "Swing sweep, R:R >= 1", "1w", "CONTROL: swing sweep on shuffled bars")
    hc, cc, nc = rr("sweeps", "Candle sweep, R:R >= 1", "1w", "CONTROL: candle sweep on shuffled bars")
    out["sweep_1w"] = _cmp("sweep_1w", "Week-candle sweep", hc, cc, nc)
    for tf in ("15m", "1d"):
        h, c, n = rr("double", "Double bottom/top, his 1:1 filter (TAKEN)", tf, "CONTROL: double bottom/top on shuffled bars")
        out[f"double_{tf}"] = _cmp(f"double_{tf}", f"Double bottom/top ({'15-min' if tf == '15m' else 'day'})", h, c, n)
    w, a = row("double", "Setup", "WITH the day structure", "15m"), row("double", "Setup", "AGAINST the day structure", "15m")
    if w is not None and a is not None:
        v = _cmp("double_with_day_15m", "Double bottom/top WITH vs AGAINST the day trend", w["Avg R per trade"],
                 a["Avg R per trade"], int(w["Trades"]) + int(a["Trades"]))
        v["detail"] = v["detail"].replace("on shuffled bars", "against the day trend")
        v["verdict"] = {"BEATS RANDOM": "WITH THE DAY TREND IS BETTER", "NO EDGE": "SMALL / NO DIFFERENCE",
                        "WORSE THAN RANDOM": "WITH THE DAY TREND IS WORSE"}.get(v["verdict"], v["verdict"])
        out["double_with_day_15m"] = v
    re_ = row("double", "Setup", "his re-entry after SL (day closed back)", "15m")
    if re_ is not None:
        n = int(re_["Trades"])
        val = _num(re_["Avg R per trade"])
        out["reentry_15m"] = {"key": "reentry_15m", "rule": "Re-entry after SL when the day closes back",
                              "verdict": "NOT ENOUGH DATA" if val is None else ("POSITIVE, SMALL SAMPLE" if val > 0.15 and n < 150
                                                                                 else "POSITIVE" if val > 0.15 else "NO EDGE"),
                              "detail": f"{'' if val is None else f'{val:+.2f} R, '}{n} re-entries", "level": 0 if val is None
                              else (1 if val > 0.15 else 0)}
    v = b["voids"]
    if len(v):
        g = v[(v["Void"] == "Void between two candles") & (v["TF"] == "15m")]
        gc = v[(v["Void"] == "CONTROL: Void between two candles") & (v["TF"] == "15m")]
        if len(g) and len(gc):
            x = _cmp("gap_first_return", "Gap NOT filled on the first return (he said 70-80%)", g.iloc[0]["NOT filled first time %"],
                     gc.iloc[0]["NOT filled first time %"], int(g.iloc[0]["First returns"]), unit="%", noise=10)
            hv = _num(g.iloc[0]["NOT filled first time %"])
            if hv is not None and hv < 60:
                x["verdict"], x["level"] = "NOT CONFIRMED", -1
            out["gap_first_return"] = x
        cr = v[(v["Void"] == "3-candle imbalance") & (v["TF"] == "15m")]
        if len(cr):
            out["void_close_rule"] = _cmp("void_close_rule", "Void close rule (no close beyond = reversal)",
                                          cr.iloc[0]["Close rule right %"], 50, int(cr.iloc[0]["First returns"]), unit="%", noise=5)
            out["void_close_rule"]["detail"] = out["void_close_rule"]["detail"].replace("on shuffled bars", "(coin flip)")
    s = b["swing_range"]
    if len(s):
        ins = s[s["Days"].str.startswith("Open INSIDE")]
        outs = s[s["Days"].str.startswith("Open OUTSIDE")]
        if len(ins) and len(outs):
            ti, to = _num(ins.iloc[0]["Trend days %"]), _num(outs.iloc[0]["Trend days %"])
            ri, ro = _num(ins.iloc[0]["Avg day range (pts)"]), _num(outs.iloc[0]["Avg day range (pts)"])
            if None in (ti, to, ri, ro):
                out["useless_open"] = {"key": "useless_open", "rule": "Open inside the swing range = useless day",
                                       "verdict": "NOT ENOUGH DATA", "detail": "", "level": 0}
            else:
                ok = ti < to - 5 and ri < ro * 0.9
                out["useless_open"] = {"key": "useless_open", "rule": "Open inside the swing range = useless day",
                                       "verdict": "CONFIRMED" if ok else "NOT CONFIRMED", "level": 1 if ok else -1,
                                       "detail": f"inside-open days: range {ri:.0f} pts, {ti:.0f}% trend days; "
                                                 f"outside: {ro:.0f} pts, {to:.0f}%"}
    bvt = b.get("body_voids")
    if bvt is not None and len(bvt):
        for tf in ("15m", "1d"):
            a = bvt[(bvt["Setup"] == "Body void, no close beyond, R:R >= 1") & (bvt["TF"] == tf)]
            cc = bvt[(bvt["Setup"] == "CONTROL: Body void, no close beyond, R:R >= 1") & (bvt["TF"] == tf)]
            if len(a) and len(cc):
                out[f"body_void_{tf}"] = _cmp(f"body_void_{tf}", f"Body void rejection ({'15-min' if tf == '15m' else 'day'}, Lecture 6)",
                                              a.iloc[0]["Avg R per trade"], cc.iloc[0]["Avg R per trade"], int(a.iloc[0]["Trades"]))
    vt = b.get("volume")
    if vt is not None and len(vt) >= 2 and vt.iloc[0]["Volume rule"].startswith("Day AFTER"):
        x, y = _num(vt.iloc[0]["Big-move days %"]), _num(vt.iloc[1]["Big-move days %"])
        out["vp_divergence"] = _cmp("vp_divergence", "Volume-profile divergence -> big move next day (Lecture 6)", x, y,
                                    int(vt.iloc[0]["Days"]), unit="%", noise=10)
        out["vp_divergence"]["detail"] = out["vp_divergence"]["detail"].replace("on shuffled bars", "after other days")
    sb = b.get("sr_break") or {}
    if sb.get("his"):
        out["swing_break"] = _cmp("swing_break", "15-min close beyond the swing range, T1 = imbalance edge",
                                  sb["his"].get("Avg R per trade"), (sb.get("control") or {}).get("Avg R per trade"),
                                  int(sb["his"].get("Trades", 0)))
    stamp = min(f for f, _, _ in b["span"].values()), max(t for _, t, _ in b["span"].values())
    return {"run": f"{pd.Timestamp.now():%d-%b-%Y %H:%M}", "data": f"{stamp[0]:%b-%Y}..{stamp[1]:%d-%b-%Y}",
            "rules": {k: out[k] for k in VERDICT_ORDER if k in out}}


def _verdict_path(R: dict) -> Path:
    return HERE / R["history"].get("verdict_file", "data/mentor_sweeps_verdicts.json")


def save_verdicts(v: dict, R: dict | None = None) -> None:
    R = R or rules()
    p = _verdict_path(R)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(v, indent=1))
    except OSError as exc:
        log.warning("could not save verdicts to %s: %s", p, exc)


def load_verdicts(R: dict | None = None) -> dict:
    """Latest saved backtest verdicts; falls back to the 28-Sep-2026 run stored in the rules file."""
    R = R or rules()
    p = _verdict_path(R)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError as exc:
            log.warning("verdict file unreadable (%s); using the stored ones", exc)
    return R.get("verdicts_default", {"rules": {}})


def summary_text(L: dict) -> str:
    lines = []
    for k in ("month", "week", "day"):
        x = L.get(k)
        if x:
            lines.append(f"{k.title()}: {x['text']}")
    if L.get("contract"):
        lines.append("Contract for a day-chart trade: " + L["contract"])
    tr = L.get("today_range")
    if tr:
        lines.append(f"15m swing range {tr['swing_low']:,.0f}-{tr['swing_high']:,.0f}; opened {tr['open_is']}"
                     + (" (useless market until it closes outside)" if tr["open_is"] == "INSIDE" else "")
                     + (f"; broke {tr['break']}" if tr["break"] else ""))
    z = L.get("zones")
    if z is not None and len(z):
        for _, r in z.tail(3).iterrows():
            lines.append(f"{'BUY' if r['side'] == 'bull' else 'SELL'} two-candle zone {r['zone_lo']:,.0f}-{r['zone_hi']:,.0f} "
                         f"-> target {r['target']:,.0f} (wait for the retest; take only if R:R >= 1)")
    s = L.get("stops") or {}
    if s.get("cluster_below"):
        lines.append("Buyers' stops stacked at " + ", ".join(f"{x:,.0f}" for x in s["cluster_below"]))
    if s.get("cluster_above"):
        lines.append("Sellers' stops stacked at " + ", ".join(f"{x:,.0f}" for x in s["cluster_above"]))
    return "\n".join(lines) or "nothing to report"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 200, "display.max_columns", 20, "display.max_colwidth", 60)
    if a.backtest:
        b = backtest()
        if "error" in b:
            print(b["error"])
            return 1
        for tf, (f, t, n) in b["span"].items():
            print(f"{tf}: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y}")
        for k in ("double", "sweeps", "voids", "swing_range", "body_voids", "volume"):
            print(f"\n== {k} ==")
            print(b[k].to_string(index=False))
        print("\n== verdicts (also shown on the tab cards) ==")
        for v in b["verdicts"]["rules"].values():
            print(f"{v['verdict']:<30} {v['rule']}: {v['detail']}")
        return 0
    c, src = ms.recent_candles(10)
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = daily_frame(dd) if dd is not None else None
    except Exception as exc:
        log.warning("daily unavailable: %s", exc)
        daily = None
    if c is None:
        print("no 15-min candles")
        return 1
    print(summary_text(live(c, daily)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
