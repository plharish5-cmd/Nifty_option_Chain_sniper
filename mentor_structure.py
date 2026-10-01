"""
mentor_structure.py — the trainer's market structure, imbalance and order-block method
(Lecture 5), exactly as he draws it, with a history check on Nifty 15-min candles.

  .venv/bin/python mentor_structure.py                 # today's structure + active zones
  .venv/bin/python mentor_structure.py --backtest      # last 365 days of 15-min candles (Dhan)

His rules (thresholds in mentor_structure_rules.json):
  swings      from VALID candles only (break or equal the previous valid candle's high/low)
  BOS/CHoCH   a candle CLOSE beyond the level; wicks don't count
  inducement  the latest pullback of the current leg; a higher high (lower low) is marked only when
              price takes it (a wick is enough); after a BOS the range's lowest point = higher low
  imbalance   candle-1 high < candle-3 low (bullish) / candle-1 low > candle-3 high (bearish)
  order block lowest candle of a pullback between the low and the inducement, with an imbalance
              right after it (shift candle by candle until one has it); sweep of the previous low
              is a bonus; zone = OB + imbalance; first and last OB of a leg matter most
  trade       entry on the tap, SL beyond the OB candle, T1 previous high, T2 Fibonacci 1.2
"""
from __future__ import annotations

import argparse
import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
from mentor_option_chain import valid_candles

log = logging.getLogger("mentor_structure")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_structure_rules.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


@dataclass
class Pivot:
    kind: str          # 'H' or 'L'
    idx: int
    price: float
    confirm: int       # bar at which it became known
    dropped_at: int | None = None   # Lecture 6: bar at which it turned out to be an inside swing (ignored after that)


def alive(p: "Pivot", now: int) -> bool:
    """Known at bar `now` and not (yet) negated as an inside swing."""
    return p.confirm <= now and (p.dropped_at is None or p.dropped_at > now)


@dataclass
class OrderBlock:
    side: str                  # 'bull' / 'bear'
    leg_id: int
    pivot_idx: int             # the pullback's extreme candle
    ob_idx: int                # the candle used as OB (after shifting)
    shifted: int               # how many candles it moved
    sweep: bool
    zone_lo: float
    zone_hi: float
    ob_lo: float
    ob_hi: float
    rank: str                  # FIRST / LAST / ONLY / MIDDLE
    valid_from: int            # bar where the inducement was taken
    sl: float
    t1: float
    t2: float
    naive_zone: tuple = (0.0, 0.0)     # lowest candle only (what most people mark)
    tap_idx: int | None = None
    result: str = ""


@dataclass
class Result:
    candles: pd.DataFrame
    pivots: list = field(default_factory=list)
    events: list = field(default_factory=list)       # dicts: idx, kind, price
    legs: list = field(default_factory=list)
    obs: list = field(default_factory=list)
    imbalances: list = field(default_factory=list)
    trend: int = 0
    state: dict = field(default_factory=dict)


# ─────────────────────────────── pivots ─────────────────────────────────────
def negate_inside_swings(piv: list[Pivot]) -> list[Pivot]:
    """Lecture 6 (13:25-18:29): a swing that forms entirely inside the previous swing's high-low range is like
    an inside candle - ignore it. With pivots A, B, C, D (alternating), C and D are dropped when C stays inside
    A and D stays inside B. Checked again after each drop (nested). The drop is known only when D is known
    (dropped_at = D.confirm), so nothing looks ahead."""
    stack: list[Pivot] = []
    for p in piv:
        stack.append(p)
        while len(stack) >= 4:
            a, b, cc, d = stack[-4:]
            if a.kind == "H":
                inside = cc.price < a.price and d.price > b.price
            else:
                inside = cc.price > a.price and d.price < b.price
            if not inside:
                break
            cc.dropped_at = d.confirm
            d.dropped_at = d.confirm
            del stack[-2:]
    return piv


def pivots(c: pd.DataFrame, negate_inside: bool = False) -> tuple[list[Pivot], pd.DataFrame]:
    v = valid_candles(c)
    piv: list[Pivot] = []
    leg, ext = 0, None
    for i in v.index[v["valid"]]:
        d = int(v.at[i, "dir"]) or 1
        if leg == 0:
            leg, ext = d, (i, v.at[i, "high"] if d > 0 else v.at[i, "low"])
            continue
        if d == leg:
            if leg > 0 and v.at[i, "high"] >= ext[1]:
                ext = (i, v.at[i, "high"])
            elif leg < 0 and v.at[i, "low"] <= ext[1]:
                ext = (i, v.at[i, "low"])
        else:
            piv.append(Pivot("H" if leg > 0 else "L", int(ext[0]), float(ext[1]), int(i)))
            leg, ext = d, (i, v.at[i, "high"] if d > 0 else v.at[i, "low"])
    if negate_inside:
        negate_inside_swings(piv)
    return piv, v


# ─────────────────────────────── imbalances ─────────────────────────────────
def imbalances(c: pd.DataFrame) -> list[dict]:
    h, lo = c["high"].values, c["low"].values
    out = []
    for i in range(len(c) - 2):
        if h[i] < lo[i + 2]:
            out.append({"side": "bull", "idx": i, "bottom": float(h[i]), "top": float(lo[i + 2])})
        elif lo[i] > h[i + 2]:
            out.append({"side": "bear", "idx": i, "bottom": float(h[i + 2]), "top": float(lo[i])})
    for g in out:                                       # first bar that trades back into the gap
        g["filled_idx"] = None
        for k in range(g["idx"] + 3, len(c)):
            if (g["side"] == "bull" and lo[k] <= g["top"]) or (g["side"] == "bear" and h[k] >= g["bottom"]):
                g["filled_idx"] = k
                break
    return out


# ─────────────────────────────── structure ──────────────────────────────────
def analyse(c: pd.DataFrame, R: dict | None = None) -> Result:
    R = R or rules()
    c = c.reset_index(drop=True)
    S = R["structure"]
    piv, v = pivots(c, S.get("negate_inside_swings", False))
    res = Result(candles=v, pivots=piv, imbalances=imbalances(c))
    n = len(c)
    if n < R["structure"]["seed_bars"] + 3:
        return res
    H, L, O = c["high"].values, c["low"].values, c["close"].values
    s = R["structure"]["seed_bars"]
    hi_i = int(np.argmax(H[:s])); lo_i = int(np.argmin(L[:s]))
    top, bot = (hi_i, H[hi_i]), (lo_i, L[lo_i])      # current H and L reference
    trend, confirmed, run, ind = 0, False, None, None
    leg_id = 0

    def ev(i, kind, price):
        res.events.append({"idx": int(i), "kind": kind, "price": float(price)})

    def latest_pullback(kind, after, before, now):
        cand = [p for p in piv if p.kind == kind and after < p.idx < before and alive(p, now)]
        return cand[-1] if cand else None

    shifting = S.get("shifting", False)
    prev_bot = prev_top = None           # the low / high of the structure before the last BOS (for shifting)

    for i in range(s, n):
        if trend == 0:
            if O[i] > top[1]:
                trend = 1
                j = bot[0] if bot[0] <= i else i
                lo_idx = top[0] + int(np.argmin(L[top[0]:i + 1]))
                bot = (lo_idx, L[lo_idx]); ev(i, "BOS_UP", top[1]); ev(lo_idx, "HL", bot[1])
                run, confirmed, ind = (i, H[i]), False, None
            elif O[i] < bot[1]:
                trend = -1
                hi_idx = bot[0] + int(np.argmax(H[bot[0]:i + 1]))
                top = (hi_idx, H[hi_idx]); ev(i, "BOS_DN", bot[1]); ev(hi_idx, "LH", top[1])
                run, confirmed, ind = (i, L[i]), False, None
            continue

        if trend == 1:
            if H[i] >= run[1]:
                run = (i, H[i])
            if (shifting and O[i] < bot[1] and not confirmed and ind is None and prev_bot is not None
                    and prev_bot[1] < bot[1] and O[i] > prev_bot[1]):
                # Lecture 6 shifting (1:06:09-1:15:17): after the BOS price ran up with NO pullback and came
                # straight back below the marked low. Not a CHoCH: the high shifts to the new top, the marked
                # low becomes the inducement (already taken), and the low goes back to the previous low.
                top = run
                ind = Pivot("L", int(bot[0]), float(bot[1]), int(i))
                ev(i, "SHIFT", bot[1]); ev(top[0], "HH", top[1]); ev(i, "IND_TAKEN", ind.price)
                bot = prev_bot
                confirmed = True
                leg_id += 1
                res.legs.append({"id": leg_id, "side": "bull", "low": bot, "high": top, "ind": (ind.idx, ind.price),
                                 "taken": i, "shifted": True})
                continue
            if O[i] < bot[1]:                                        # CHoCH down
                hi_idx = bot[0] + int(np.argmax(H[bot[0]:i + 1]))
                top = (hi_idx, H[hi_idx]); ev(i, "CHOCH_DN", bot[1]); ev(hi_idx, "LH", top[1])
                trend, run, confirmed, ind = -1, (i, L[i]), False, None
                prev_bot = prev_top = None
                continue
            if not confirmed:
                p = latest_pullback("L", bot[0], run[0], i)
                if p is not None:
                    ind = p
                if ind is not None and L[i] < ind.price:            # inducement taken (wick is enough)
                    confirmed = True
                    top = run
                    ev(top[0], "HH", top[1]); ev(i, "IND_TAKEN", ind.price)
                    leg_id += 1
                    res.legs.append({"id": leg_id, "side": "bull", "low": bot, "high": top, "ind": (ind.idx, ind.price),
                                     "taken": i})
            else:
                if O[i] > top[1]:                                    # BOS up
                    prev_bot = bot
                    lo_idx = top[0] + int(np.argmin(L[top[0]:i + 1]))
                    bot = (lo_idx, L[lo_idx]); ev(i, "BOS_UP", top[1]); ev(lo_idx, "HL", bot[1])
                    run, confirmed, ind = (i, H[i]), False, None
        else:
            if L[i] <= run[1]:
                run = (i, L[i])
            if (shifting and O[i] > top[1] and not confirmed and ind is None and prev_top is not None
                    and prev_top[1] > top[1] and O[i] < prev_top[1]):
                bot = run
                ind = Pivot("H", int(top[0]), float(top[1]), int(i))
                ev(i, "SHIFT", top[1]); ev(bot[0], "LL", bot[1]); ev(i, "IND_TAKEN", ind.price)
                top = prev_top
                confirmed = True
                leg_id += 1
                res.legs.append({"id": leg_id, "side": "bear", "low": bot, "high": top, "ind": (ind.idx, ind.price),
                                 "taken": i, "shifted": True})
                continue
            if O[i] > top[1]:                                        # CHoCH up
                lo_idx = top[0] + int(np.argmin(L[top[0]:i + 1]))
                bot = (lo_idx, L[lo_idx]); ev(i, "CHOCH_UP", top[1]); ev(lo_idx, "HL", bot[1])
                trend, run, confirmed, ind = 1, (i, H[i]), False, None
                prev_bot = prev_top = None
                continue
            if not confirmed:
                p = latest_pullback("H", top[0], run[0], i)
                if p is not None:
                    ind = p
                if ind is not None and H[i] > ind.price:
                    confirmed = True
                    bot = run
                    ev(bot[0], "LL", bot[1]); ev(i, "IND_TAKEN", ind.price)
                    leg_id += 1
                    res.legs.append({"id": leg_id, "side": "bear", "low": bot, "high": top, "ind": (ind.idx, ind.price),
                                     "taken": i})
            else:
                if O[i] < bot[1]:                                    # BOS down
                    prev_top = top
                    hi_idx = bot[0] + int(np.argmax(H[bot[0]:i + 1]))
                    top = (hi_idx, H[hi_idx]); ev(i, "BOS_DN", bot[1]); ev(hi_idx, "LH", top[1])
                    run, confirmed, ind = (i, L[i]), False, None

    res.trend = trend
    res.state = {"trend": trend, "high": top, "low": bot, "confirmed": confirmed,
                 "inducement": (ind.idx, ind.price) if ind is not None else None, "run": run}
    for leg in res.legs:
        res.obs += _order_blocks(c, piv, leg, R)
    return res


def _order_blocks(c: pd.DataFrame, piv: list[Pivot], leg: dict, R: dict) -> list[OrderBlock]:
    B, T = R["order_block"], R["trade"]
    H, L, OP, CL = c["high"].values, c["low"].values, c["open"].values, c["close"].values
    bull = leg["side"] == "bull"
    lo_i, lo_p = leg["low"]
    hi_i, hi_p = leg["high"]
    ind_i, _ = leg["ind"]
    t0 = leg["taken"]
    if bull:
        start = lo_i
        cands = [lo_i] + [p.idx for p in piv if p.kind == "L" and lo_i < p.idx < ind_i and alive(p, t0)]
        end = hi_i
    else:
        start = hi_i
        cands = [hi_i] + [p.idx for p in piv if p.kind == "H" and hi_i < p.idx < ind_i and alive(p, t0)]
        end = lo_i
    cands = sorted(set(cands))
    out: list[OrderBlock] = []
    for pos, k in enumerate(cands):
        found = None
        for sft in range(B["max_shift_candles"] + 1):
            j = k + sft
            if j + 2 > end or j + 2 >= len(c):
                break
            gap = (H[j] < L[j + 2]) if bull else (L[j] > H[j + 2])
            if gap:
                found = (j, sft)
                break
        if not found:
            continue
        j, sft = found
        if any(o.ob_idx == j for o in out):
            continue                                      # two pullbacks shifted onto the same candle
        if bull:
            g_lo, g_hi = H[j], L[j + 2]
            since = L[j + 3:t0 + 1]
            if B["require_unfilled_imbalance"] and len(since) and since.min() <= g_lo:
                continue                                  # imbalance already filled before the setup
            touched = len(since) and since.min() < g_hi       # price already wicked into part of the imbalance
            g_hi = min(g_hi, since.min()) if len(since) else g_hi
            big = (g_hi - g_lo) > B["large_imbalance_x_ob_range"] * max(H[j] - L[j], 1e-9)
            ob_only = big or (touched and B.get("partial_tap_ob_only", False))
            z_lo, z_hi = L[j], (H[j] if ob_only else g_hi)
            sweep = j > 0 and L[j] < L[j - 1] and CL[j] > L[j - 1]
            sl = L[j] - T["sl_buffer_points"]
            t1 = hi_p
            t2 = lo_p + T["fib_extension"] * (hi_p - lo_p)
            naive = (L[k], H[k])
        else:
            g_lo, g_hi = H[j + 2], L[j]
            since = H[j + 3:t0 + 1]
            if B["require_unfilled_imbalance"] and len(since) and since.max() >= g_hi:
                continue
            touched = len(since) and since.max() > g_lo
            g_lo = max(g_lo, since.max()) if len(since) else g_lo
            big = (g_hi - g_lo) > B["large_imbalance_x_ob_range"] * max(H[j] - L[j], 1e-9)
            ob_only = big or (touched and B.get("partial_tap_ob_only", False))
            z_lo, z_hi = (L[j] if ob_only else g_lo), H[j]
            sweep = j > 0 and H[j] > H[j - 1] and CL[j] < H[j - 1]
            sl = H[j] + T["sl_buffer_points"]
            t1 = lo_p
            t2 = hi_p - T["fib_extension"] * (hi_p - lo_p)
            naive = (L[k], H[k])
        rank = "ONLY" if len(cands) == 1 else "FIRST" if pos == 0 else "LAST" if pos == len(cands) - 1 else "MIDDLE"
        out.append(OrderBlock("bull" if bull else "bear", leg["id"], int(k), int(j), int(sft), bool(sweep),
                              float(z_lo), float(z_hi), float(L[j]), float(H[j]), rank, int(t0),
                              float(sl), float(t1), float(t2), (float(naive[0]), float(naive[1]))))
    return out


# ─────────────────────────────── trades ─────────────────────────────────────
def _walk(H, L, C, bull: bool, start: int, entry: float, sl: float, target: float, max_bars: int) -> tuple[str, float]:
    """From the bar after `start`: SL first if one candle touches both; timeout = mark to market."""
    risk = max(abs(entry - sl), 1e-9)
    end = min(len(C) - 1, start + max_bars)
    for k in range(start + 1, end + 1):
        if (bull and L[k] <= sl) or (not bull and H[k] >= sl):
            return "SL", -1.0
        if (bull and H[k] >= target) or (not bull and L[k] <= target):
            return "TARGET", abs(target - entry) / risk
    if end <= start:
        return "OPEN", 0.0
    return "TIMEOUT", ((C[end] - entry) if bull else (entry - C[end])) / risk


def _breaks_after(res: Result) -> list[int]:
    return sorted(e["idx"] for e in res.events if e["kind"] in ("BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN", "SHIFT"))


def inducement_sweeps(c: pd.DataFrame, res: Result, R: dict | None = None) -> pd.DataFrame:
    """Lecture 6 (1:07:35-1:08:30): the candle that takes the inducement but does NOT close beyond it (a wick)
    -> trade back towards the leg's extreme, SL beyond the wick. He rates it 50-50."""
    R = R or rules()
    T = R["trade"]
    H, L, C = c["high"].values, c["low"].values, c["close"].values
    rows = []
    for leg in res.legs:
        i = leg["taken"]
        ind = leg["ind"][1]
        bull = leg["side"] == "bull"
        if bull and not (L[i] < ind and C[i] > ind):
            continue
        if not bull and not (H[i] > ind and C[i] < ind):
            continue
        entry = C[i]
        sl = (L[i] - T["sl_buffer_points"]) if bull else (H[i] + T["sl_buffer_points"])
        target = leg["high"][1] if bull else leg["low"][1]
        if (bull and target <= entry) or (not bull and target >= entry):
            continue
        out, r = _walk(H, L, C, bull, i, entry, sl, target, T["max_bars_after_tap"])
        rows.append({"side": leg["side"], "idx": i, "entry": entry, "sl": sl, "target": target,
                     "rr": abs(target - entry) / max(abs(entry - sl), 1e-9), "result": out, "r": r,
                     "shifted_leg": bool(leg.get("shifted"))})
    return pd.DataFrame(rows)


def simulate(c: pd.DataFrame, res: Result, R: dict | None = None) -> pd.DataFrame:
    """First tap of each OB zone after its inducement was taken; SL first if both on one bar.
    Lecture 6 additions: an OB is dropped once the structure breaks again before it is tapped
    (order_block.cancel_on_next_break); after an SL, if that candle closed back inside the zone (only a
    wick beyond it) he re-enters with the SL beyond the new wick (column r_reentry)."""
    R = R or rules()
    T = R["trade"]
    cancel = R["order_block"].get("cancel_on_next_break", False)
    H, L, CLS = c["high"].values, c["low"].values, c["close"].values
    brk = _breaks_after(res)
    rows = []
    for ob in res.obs:
        bull = ob.side == "bull"
        entry_level = ob.zone_hi if bull else ob.zone_lo
        nxt = next((b for b in brk if b > ob.valid_from), None)
        tap = None
        for i in range(ob.valid_from + 1, min(len(c), ob.valid_from + 1 + 4 * T["max_bars_after_tap"])):
            if cancel and nxt is not None and i > nxt:
                ob.result = "CANCELLED (structure broke again)"
                break
            if bull and H[i] >= ob.t1 and tap is None:
                break                                    # ran to target without us: missed
            if not bull and L[i] <= ob.t1 and tap is None:
                break
            if (bull and L[i] <= entry_level) or (not bull and H[i] >= entry_level):
                tap = i
                break
        naive_tap = None
        n_level = ob.naive_zone[1] if bull else ob.naive_zone[0]
        for i in range(ob.valid_from + 1, min(len(c), ob.valid_from + 1 + 4 * T["max_bars_after_tap"])):
            if (bull and L[i] <= n_level) or (not bull and H[i] >= n_level):
                naive_tap = i
                break
            if (bull and H[i] >= ob.t1) or (not bull and L[i] <= ob.t1):
                break
        out = {"side": ob.side, "rank": ob.rank, "shifted": ob.shifted, "sweep": ob.sweep,
               "zone": f"{ob.zone_lo:,.0f}-{ob.zone_hi:,.0f}", "tapped": tap is not None,
               "naive_tapped": naive_tap is not None, "t1": None, "t2": None, "sl_hit": None, "r": None,
               "r_multiple_t1": abs(ob.t1 - entry_level) / max(abs(entry_level - ob.sl), 1e-9),
               "r_reentry": None, "cancelled": ob.result.startswith("CANCELLED")}
        if tap is not None:
            ob.tap_idx = tap
            hit1 = hit2 = slh = False
            for k in range(tap, min(len(c), tap + T["max_bars_after_tap"])):
                if (bull and L[k] <= ob.sl) or (not bull and H[k] >= ob.sl):
                    slh = True
                    back = CLS[k] > ob.ob_lo if bull else CLS[k] < ob.ob_hi     # only a wick beyond the zone
                    if back:
                        nsl = (L[k] - T["sl_buffer_points"]) if bull else (H[k] + T["sl_buffer_points"])
                        if (bull and ob.t1 > CLS[k]) or (not bull and ob.t1 < CLS[k]):
                            _, out["r_reentry"] = _walk(H, L, CLS, bull, k, CLS[k], nsl, ob.t1, T["max_bars_after_tap"])
                    break
                if (bull and H[k] >= ob.t1) or (not bull and L[k] <= ob.t1):
                    hit1 = True
                if (bull and H[k] >= ob.t2) or (not bull and L[k] <= ob.t2):
                    hit2 = True
                    break
            r = out["r_multiple_t1"]
            out.update({"t1": hit1, "t2": hit2, "sl_hit": slh,
                        "r": (r if hit1 else -1.0 if slh else 0.0)})   # exit all at T1 or SL, else flat
            ob.result = "T2" if hit2 else "T1" if hit1 else "SL" if slh else "OPEN"
        rows.append(out)
    return pd.DataFrame(rows)


def fvg_rejections(c: pd.DataFrame, gaps: list[dict], look: int = 8) -> pd.DataFrame:
    """His imbalance note: wick into an untouched gap and close back outside with a candle of the
    trend's colour -> continuation. Fair test: from that candle's close, does price go +1 candle-range
    in the trend direction before -1 (within `look` bars)? A coin flip scores ~50%."""
    H, L, OP, CL = (c[x].values for x in ("high", "low", "open", "close"))
    rows = []
    for g in gaps:
        k = g["filled_idx"]
        if k is None or k + 1 >= len(c):
            continue
        rng = max(H[k] - L[k], 1e-9)
        if g["side"] == "bull":
            rejected = CL[k] > g["top"] and CL[k] > OP[k]
            up, dn = CL[k] + rng, CL[k] - rng
        else:
            rejected = CL[k] < g["bottom"] and CL[k] < OP[k]
            up, dn = CL[k] - rng, CL[k] + rng            # 'up' = trend direction target
        ok = None
        for m in range(k + 1, min(len(c), k + 1 + look)):
            hit_t = H[m] >= up if g["side"] == "bull" else L[m] <= up
            hit_s = L[m] <= dn if g["side"] == "bull" else H[m] >= dn
            if hit_s:
                ok = False
                break
            if hit_t:
                ok = True
                break
        if ok is None:
            continue
        rows.append({"side": g["side"], "rejected": rejected, "continued": ok})
    return pd.DataFrame(rows)


# ─────────────────────────────── data ───────────────────────────────────────
def load_history(R: dict | None = None, refresh: bool = False) -> pd.DataFrame | None:
    """Nifty 15-min candles from Dhan in 88-day windows, cached; topped up incrementally."""
    R = R or rules()
    Hc = R["history"]
    cache = HERE / Hc["cache"]
    have = pd.read_csv(cache, parse_dates=["timestamp"]) if cache.exists() else None
    fresh = cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)) < timedelta(hours=1)
    if have is not None and fresh and not refresh:
        return have
    want_from = date.today() - timedelta(days=Hc["days"])
    ranges = []                                   # (from, to) windows still missing
    if have is None or have.empty:
        ranges.append((want_from, date.today()))
    else:
        have["timestamp"] = pd.to_datetime(have["timestamp"])
        first = have["timestamp"].min().date()
        if first > want_from + timedelta(days=5):  # history.days was raised: fetch the older part too
            ranges.append((want_from, first))
        # holes left by an earlier run that failed half way (e.g. Dhan 502s): any gap > 6 calendar days
        dts = sorted(have["timestamp"].dt.date.unique())
        for a, b in zip(dts, dts[1:]):
            if (b - a).days > 6:
                ranges.append((a + timedelta(days=1), b - timedelta(days=1)))
        ranges.append(((have["timestamp"].max() - timedelta(days=3)).date(), date.today()))
    frames = [] if have is None else [have]
    try:
        import dhan_client as dc
        sid = str(getattr(config, "DHAN_INDEX_SECURITY_ID", "13"))
        windows = []
        for a, b in ranges:
            cur = a
            while cur <= b:
                windows.append((cur, min(cur + timedelta(days=int(Hc.get("window_days", 88))), b)))
                cur = windows[-1][1] + timedelta(days=1)
        if len(windows) > 3:
            log.warning("fetching %d x %s-day windows of %s-min candles from Dhan (%s..%s) - one-time, ~%ds",
                        len(windows), Hc.get("window_days", 88), Hc["dhan_interval"], windows[0][0], windows[-1][1],
                        int(len(windows) * 0.6))
        for cur, to in windows:
            for attempt in range(3):                 # Dhan answers 502/503 now and then; retry with a pause
                try:
                    payload = dc._post("/charts/intraday", {
                        "securityId": sid, "exchangeSegment": "IDX_I", "instrument": "INDEX",
                        "interval": Hc["dhan_interval"], "oi": False,
                        "fromDate": f"{cur:%Y-%m-%d} 09:00:00", "toDate": f"{to:%Y-%m-%d} 16:00:00"})
                    frames.append(dc._to_frame(payload)[["timestamp", "open", "high", "low", "close"]])
                    break
                except Exception as exc:
                    msg = str(exc)
                    retry = any(code in msg for code in ("HTTP 502", "HTTP 503", "HTTP 504", "HTTP 429"))
                    if retry and attempt < 2:
                        _time.sleep(2.0 * (attempt + 1))
                        continue
                    log.info("Dhan %s-min %s..%s not available: %s", Hc["dhan_interval"], cur, to, msg[:100])
                    break
            _time.sleep(0.35)
    except Exception as exc:
        log.warning("Dhan history unavailable: %s", exc)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    t = df["timestamp"].dt.time
    df = df[(t >= config.MARKET_OPEN) & (t < config.MARKET_CLOSE)]
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    return df


def recent_candles(days: int = 5, R: dict | None = None) -> tuple[pd.DataFrame | None, str]:
    """Last few sessions of 15-min candles: futures from your candle source when available, else the
    Dhan index history."""
    R = R or rules()
    m = R["structure"]["timeframe_minutes"]
    try:
        import candle_source
        idx, fut = candle_source.fetch(days)
        # FIX 30-Sep-2026: live used FUTURES candles while every backtest and every level (PDH, swings, OI strikes)
        # is on the INDEX. After a monthly rollover the next-month future trades ~100+ pts above spot, so alerts
        # showed 22,874 / 22,880 levels with spot at 22,757. Index is now the default; set
        # structure.live_source = "futures" in mentor_structure_rules.json to go back.
        want = R["structure"].get("live_source", "index")
        if want == "futures" and fut is not None and len(fut):
            base, src = fut, "futures"
        elif idx is not None and len(idx):
            base, src = idx, "index"
        else:
            base, src = fut, "futures"
        b = base.copy()
        b["timestamp"] = pd.to_datetime(b["timestamp"])
        r = (b.set_index("timestamp")[["open", "high", "low", "close"]]
             .groupby(pd.Grouper(freq=f"{m}min", origin="start_day", offset="15min"))
             .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna().reset_index())
        return r, src
    except Exception as exc:
        log.info("candle_source unavailable (%s); using Dhan 15-min index history", str(exc)[:80])
    h = load_history(R)
    if h is None:
        return None, "none"
    last_days = sorted(h["timestamp"].dt.date.unique())[-days:]
    return h[h["timestamp"].dt.date.isin(last_days)].reset_index(drop=True), "index"


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    h = load_history(R)
    if h is None or len(h) < 200:
        return {"error": "no 15-min history (Dhan token?)"}
    res = analyse(h, R)
    tr = simulate(h, res, R)
    fv = fvg_rejections(h, res.imbalances)
    minimum = R["history"]["min_sample_size"]

    def rate(df, col, mask=None):
        x = df if mask is None else df[mask]
        x = x[x[col].notna()]
        return ("INSUFFICIENT SAMPLE" if len(x) < minimum else round(100 * x[col].astype(bool).mean(), 1)), len(x)

    # control: the same bars in random order (same volatility, no real structure). Any edge must beat this.
    rng = np.random.default_rng(42)
    rel = pd.DataFrame({"o": h["open"].values - h["close"].shift(1).bfill().values,
                        "h": h["high"].values - h["open"].values, "l": h["low"].values - h["open"].values,
                        "c": h["close"].values - h["open"].values}).iloc[rng.permutation(len(h))].reset_index(drop=True)
    cl = float(h["close"].iloc[0]) + (rel["o"] + rel["c"]).cumsum()
    op = cl - rel["c"]
    ctrl = pd.DataFrame({"timestamp": h["timestamp"].values, "open": op, "high": op + rel["h"],
                         "low": op + rel["l"], "close": cl})
    ctrl["high"] = ctrl[["open", "high", "close"]].max(axis=1)
    ctrl["low"] = ctrl[["open", "low", "close"]].min(axis=1)
    cres = analyse(ctrl, R)
    ctr = simulate(ctrl, cres, R)
    cfv = fvg_rejections(ctrl, cres.imbalances)

    rows = []
    if len(tr):
        tp = tr[tr["tapped"]]
        for name, mask in (("All order blocks", None), ("FIRST / ONLY in the leg", tp["rank"].isin(["FIRST", "ONLY"])),
                           ("LAST in the leg", tp["rank"] == "LAST"), ("MIDDLE in the leg", tp["rank"] == "MIDDLE"),
                           ("With sweep of previous candle", tp["sweep"]), ("Without sweep", ~tp["sweep"]),
                           ("Bullish", tp["side"] == "bull"), ("Bearish", tp["side"] == "bear")):
            r1, n = rate(tp, "t1", mask)
            r2, _ = rate(tp, "t2", mask)
            rs, _ = rate(tp, "sl_hit", mask)
            x = tp if mask is None else tp[mask]
            exp = round(float(x["r"].mean()), 2) if len(x) >= minimum else "INSUFFICIENT SAMPLE"
            rows.append({"Order blocks": name, "Tapped": n, "T1 hit %": r1, "T2 (Fib 1.2) hit %": r2,
                         "SL hit %": rs, "Avg R per trade": exp})
        ctp = ctr[ctr["tapped"]] if len(ctr) else ctr
        if len(ctp) >= minimum:
            rows.append({"Order blocks": "CONTROL: same bars shuffled (no real structure)", "Tapped": len(ctp),
                         "T1 hit %": round(100 * ctp["t1"].astype(bool).mean(), 1),
                         "T2 (Fib 1.2) hit %": round(100 * ctp["t2"].astype(bool).mean(), 1),
                         "SL hit %": round(100 * ctp["sl_hit"].astype(bool).mean(), 1),
                         "Avg R per trade": round(float(ctp["r"].mean()), 2)})
        shifted = tr["shifted"] > 0
        rows.append({"Order blocks": "His shifted OB vs 'lowest candle' OB: zone reached", "Tapped":
                     int(tr[shifted]["tapped"].sum()), "T1 hit %":
                     f"his {100 * tr[shifted]['tapped'].mean():.0f}% vs naive {100 * tr[shifted]['naive_tapped'].mean():.0f}%"
                     if shifted.sum() >= minimum else "INSUFFICIENT SAMPLE", "T2 (Fib 1.2) hit %": "", "SL hit %": ""})
    frows = []
    if len(fv):
        for name, mask in (("Wick into imbalance, closed back out (his signal)", fv["rejected"]),
                           ("Entered imbalance without that rejection", ~fv["rejected"])):
            r, n = rate(fv, "continued", mask)
            frows.append({"Imbalance": name, "Cases": n, "Went +1 range before -1 %": r})
        if len(cfv):
            r, n = rate(cfv, "continued", cfv["rejected"])
            frows.append({"Imbalance": "CONTROL: his signal on shuffled bars", "Cases": n, "Went +1 range before -1 %": r})
    # ── Lecture 6 checks ────────────────────────────────────────────────────
    import copy

    def variant(**flags):
        Rv = copy.deepcopy(R)
        for k, v in flags.items():
            sec = "order_block" if k in ("cancel_on_next_break", "partial_tap_ob_only") else "structure"
            Rv[sec][k] = v
        out = []
        for frame in (h, ctrl):
            r_ = analyse(frame, Rv)
            t_ = simulate(frame, r_, Rv)
            tp_ = t_[t_["tapped"]] if len(t_) else t_
            out.append((len(r_.legs), len(tp_), round(float(tp_["r"].mean()), 2) if len(tp_) >= minimum
                        else "INSUFFICIENT SAMPLE"))
        return out

    l6 = []
    for name, flags in (("His rules now (Lecture 6: inside swings ignored + shifting + old OBs cancelled)", {}),
                        ("Lecture 5 rules (none of the three)", {"negate_inside_swings": False, "shifting": False,
                                                                 "cancel_on_next_break": False}),
                        ("  without ignoring inside swings", {"negate_inside_swings": False}),
                        ("  without shifting", {"shifting": False}),
                        ("  without cancelling old OBs", {"cancel_on_next_break": False}),
                        ("  without 'imbalance partly tapped -> OB candle only' (final class)",
                         {"partial_tap_ob_only": False})):
        (lg, n, a), (_, cn, ca) = variant(**flags)
        l6.append({"Lecture 6 check": name, "Legs": lg, "OBs tapped": n, "Avg R per trade": a,
                   "CONTROL avg R (shuffled)": ca, "Control trades": cn})
    if len(tr):
        tp = tr[tr["tapped"]]
        re_ = tp[tp["r_reentry"].notna()]
        l6.append({"Lecture 6 check": "Re-entry after SL when that candle closed back inside the zone (wick only)",
                   "Legs": "", "OBs tapped": len(re_), "Avg R per trade": round(float(re_["r_reentry"].mean()), 2)
                   if len(re_) >= minimum else "INSUFFICIENT SAMPLE", "CONTROL avg R (shuffled)": "", "Control trades": ""})
        cn_ = int(tr["cancelled"].sum())
        l6.append({"Lecture 6 check": "OBs cancelled because the structure broke again before the tap", "Legs": "",
                   "OBs tapped": cn_, "Avg R per trade": "", "CONTROL avg R (shuffled)": "", "Control trades": ""})
    isw, cisw = inducement_sweeps(h, res, R), inducement_sweeps(ctrl, cres, R)
    l6.append({"Lecture 6 check": "Inducement swept with a wick (no close beyond) -> trade to the previous extreme",
               "Legs": "", "OBs tapped": len(isw),
               "Avg R per trade": round(float(isw["r"].mean()), 2) if len(isw) >= minimum else "INSUFFICIENT SAMPLE",
               "CONTROL avg R (shuffled)": round(float(cisw["r"].mean()), 2) if len(cisw) >= minimum else "INSUFFICIENT SAMPLE",
               "Control trades": len(cisw)})
    if len(isw) >= minimum:
        l6[-1]["Lecture 6 check"] += f" (target hit {100 * (isw['result'] == 'TARGET').mean():.0f}%; he said 50-50)"
    l6.append({"Lecture 6 check": "Legs found by SHIFTING (no pullback, straight back below the low)", "Legs":
               sum(1 for lg in res.legs if lg.get("shifted")), "OBs tapped": "", "Avg R per trade": "",
               "CONTROL avg R (shuffled)": "", "Control trades": ""})
    tp_all = tr[tr["tapped"]] if len(tr) else tr
    if len(tp_all) >= minimum:
        a = float(tp_all["r"].mean())
        cr = float(ctp["r"].mean()) if len(ctr) and len(ctp) else 0.0
        v = {"run": f"{pd.Timestamp.now():%d-%b-%Y}", "avg_r": round(a, 2), "control_r": round(cr, 2),
             "trades": int(len(tp_all)), "verdict": "BEATS RANDOM" if a - cr >= 0.15 and a > 0 else
             "LOSES IN BACKTEST" if a < 0 else "NO EDGE"}
        try:
            vp = HERE / R["history"].get("verdict_file", "data/mentor_structure_verdict.json")
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text(json.dumps(v))
        except OSError as exc:
            log.warning("could not save the order-block verdict: %s", exc)
    return {"bars": len(h), "from": h["timestamp"].min(), "to": h["timestamp"].max(),
            "legs": len(res.legs), "obs": len(res.obs), "order_blocks": pd.DataFrame(rows),
            "imbalances": pd.DataFrame(frows), "lecture6": pd.DataFrame(l6), "trades": tr}


def load_verdict(R: dict | None = None) -> dict:
    R = R or rules()
    p = HERE / R["history"].get("verdict_file", "data/mentor_structure_verdict.json")
    try:
        return json.loads(p.read_text()) if p.exists() else R.get("verdict_default", {})
    except ValueError:
        return R.get("verdict_default", {})


def summary_text(res: Result, c: pd.DataFrame) -> str:
    st = res.state
    if not st:
        return "not enough candles"
    tr = {1: "BULLISH (HH-HL)", -1: "BEARISH (LH-LL)", 0: "UNDECIDED"}[st["trend"]]
    last = c.iloc[-1]
    lines = [f"Structure {tr} | last close {last['close']:,.1f}",
             f"High {st['high'][1]:,.1f} | Low {st['low'][1]:,.1f}"
             + (f" | inducement {st['inducement'][1]:,.1f}" if st["inducement"] else "")
             + (" | leg confirmed (inducement taken)" if st["confirmed"] else "")]
    act = [o for o in res.obs if o.tap_idx is None and o.valid_from >= len(c) - 60]
    for o in act[-4:]:
        lines.append(f"{'BUY' if o.side == 'bull' else 'SELL'} zone {o.zone_lo:,.0f}-{o.zone_hi:,.0f} ({o.rank}"
                     f"{', sweep' if o.sweep else ''}) SL {o.sl:,.0f} T1 {o.t1:,.0f} T2 {o.t2:,.0f}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    R = rules()
    if a.backtest:
        b = backtest(R)
        if "error" in b:
            print(b["error"])
        else:
            print(f"{b['bars']} bars {b['from']} -> {b['to']} | legs {b['legs']} | order blocks {b['obs']}")
            print(b["order_blocks"].to_string(index=False))
            print(b["imbalances"].to_string(index=False))
            print("\n== Lecture 6 ==")
            print(b["lecture6"].to_string(index=False))
    else:
        c, src = recent_candles(5, R)
        if c is None:
            print("no candles")
        else:
            r = analyse(c, R)
            simulate(c, r, R)
            print(f"[{src}] " + summary_text(r, c))
