"""
pa_signals.py — YOUR price-action method, from your daily homework (29/30-Sep-2026 charts), on 15-min INDEX candles.

  THE SETUP
    pattern    hammer (long lower wick) / double hammer (two hammers with lows close together) at SUPPORT,
               inverted hammer (long upper wick) at RESISTANCE
    location   day high / day low, previous day high / low (R1 / S1 on your chart), intraday 15-min swing high /
               low, previous days' wick zones, OI walls (live), your manual levels (data/manual_levels.json)
    trigger    FOLLOW-THROUGH: a later candle (within trigger_bars) trades beyond the pattern candle's high (buy)
               or low (sell) -> entry at that break. No break = no trade.
    SL         beyond the pattern candle's wick (both hammers for a double hammer)
    exits      T1 = next opposite level; your exits: trailing SL under each 15-min candle once in profit, a close
               through the 8 EMA, or the opposite pattern forming at a level (IH at the day high)
    tags       8-EMA reclaim (close back above the 8 EMA - your trade 3), and the CONFLUENCE the app already
               computes: OI wall at the level, PCR, writers ratio, dominance, RVOL, GEX, IV, previous-day wick

  .venv/bin/python pa_signals.py                    # today so far: setups, triggers, exit hints, confluence
  .venv/bin/python pa_signals.py --day 2026-09-30   # any day (replay), no look-ahead
  .venv/bin/python pa_signals.py --backtest         # 5 years of 15-min: vs random entry times, same trade shape
  .venv/bin/python homework.py 2026-09-30           # your homework trades vs what the app found

Numbers in pa_signals_rules.json. Confluence is SHOWN, not used as a filter, until your homework and the
backtest say which items matter.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("pa_signals")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "pa_signals_rules.json"
VERDICT_PATH = HERE / "data" / "pa_signals_verdict.json"
MANUAL_PATH = HERE / "data" / "manual_levels.json"


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


# ─────────────────────────────── levels ─────────────────────────────────────
def _daily(c: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(c["timestamp"])
    g = c.groupby(ts.dt.date)
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last()})


def _manual() -> list[float]:
    try:
        return [float(x) for x in json.loads(MANUAL_PATH.read_text()).get("levels", [])]
    except (OSError, ValueError):
        return []


def day_levels(c: pd.DataFrame, day: date, R: dict, oi: dict | None = None,
               d: pd.DataFrame | None = None) -> list[tuple[str, float, str]]:
    """(name, price, 'sup'|'res'|'both') known BEFORE `day` starts (+ OI walls when given, live only)."""
    L = R["levels"]
    d = _daily(c) if d is None else d
    prev = d[d.index < day]
    out = []
    if len(prev):
        p = prev.iloc[-1]
        out += [("PDH", float(p["high"]), "res"), ("PDL", float(p["low"]), "sup"), ("PDC", float(p["close"]), "both")]
        # previous days' wicks (your 'previous day wicks at support and resistance')
        for dd, r in prev.tail(L["wick_days"]).iterrows():
            lw, uw = min(r["open"], r["close"]) - r["low"], r["high"] - max(r["open"], r["close"])
            rng = r["high"] - r["low"]
            if rng > 0 and lw >= L["wick_share_of_day"] * rng:
                out.append((f"lower wick {dd:%d-%b}", float(r["low"]), "sup"))
            if rng > 0 and uw >= L["wick_share_of_day"] * rng:
                out.append((f"upper wick {dd:%d-%b}", float(r["high"]), "res"))
    for k in _manual():
        out.append(("manual", k, "both"))
    if oi:
        if oi.get("floor"):
            out.append((f"{oi['floor']:,.0f} PE wall", float(oi["floor"]), "sup"))
        if oi.get("ceiling"):
            out.append((f"{oi['ceiling']:,.0f} CE wall", float(oi["ceiling"]), "res"))
    return out


def swing_points(H: np.ndarray, L: np.ndarray, k: int) -> tuple[list, list]:
    """15-min fractal swings: (confirmed_at_index, price). A swing at j is known only at j + k."""
    hi, lo = [], []
    for j in range(k, len(H) - k):
        if H[j] == H[j - k:j + k + 1].max():
            hi.append((j + k, float(H[j])))
        if L[j] == L[j - k:j + k + 1].min():
            lo.append((j + k, float(L[j])))
    return hi, lo


# ─────────────────────────────── the scan ───────────────────────────────────
def scan(c: pd.DataFrame, R: dict | None = None, oi: dict | None = None, only_day: date | None = None) -> pd.DataFrame:
    """Every setup (pattern at a level), whether it triggered, and how it would have ended (three exit styles)."""
    R = R or rules()
    P, T, X = R["pattern"], R["trigger"], R["exit"]
    c = c.reset_index(drop=True)
    O, H, L, C = (c[k].values.astype(float) for k in ("open", "high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    days = ts.dt.date.values
    n = len(c)
    ema = c["close"].ewm(span=X["ema_span"], adjust=False).mean().values
    sw_hi, sw_lo = swing_points(H, L, R["levels"]["swing_k"])
    hi_j = np.array([j for j, _ in sw_hi], dtype=int)
    lo_j = np.array([j for j, _ in sw_lo], dtype=int)
    look = R["levels"]["swing_lookback_bars"]
    dly = _daily(c)

    def recent(js, pts, i):
        a, b = np.searchsorted(js, i - look, "left"), np.searchsorted(js, i - 1, "right")
        return pts[a:b]
    tol = R["levels"]["tolerance_points"]
    rows = []
    lv_cache: dict = {}
    day_start = 0
    for i in range(n):
        if i == 0 or days[i] != days[i - 1]:
            day_start = i
        if only_day is not None and days[i] != only_day:
            continue
        if days[i] not in lv_cache:
            lv_cache[days[i]] = day_levels(c, days[i], R, oi if only_day is not None else None, dly)
        rng = H[i] - L[i]
        if rng < P["min_range_points"]:
            continue
        body = abs(C[i] - O[i])
        lw, uw = min(O[i], C[i]) - L[i], H[i] - max(O[i], C[i])
        is_ham = lw >= P["wick_body_ratio"] * max(body, 0.5) and lw >= P["wick_share"] * rng and uw <= P["other_wick_max"] * rng
        is_ih = uw >= P["wick_body_ratio"] * max(body, 0.5) and uw >= P["wick_share"] * rng and lw <= P["other_wick_max"] * rng
        dbl = False
        if i - 1 >= day_start:
            # double hammer: two candles with long lower wicks whose lows are within dh_tol. FIX 30-Sep-2026: also
            # when the 2nd candle is a full hammer itself (your 09:30 + 09:45 double hammer was labelled 'hammer')
            lw0 = min(O[i - 1], C[i - 1]) - L[i - 1]
            rng0 = H[i - 1] - L[i - 1]
            if rng0 > 0 and lw0 >= P["double_wick_share"] * rng0 and lw >= P["double_wick_share"] * rng and \
                    abs(L[i] - L[i - 1]) <= P["double_low_tolerance"]:
                dbl = True
        for side, ok in (("CE", is_ham or dbl), ("PE", is_ih)):
            if not ok:
                continue
            ext = L[i] if side == "CE" else H[i]
            if side == "CE" and dbl:
                ext = min(L[i], L[i - 1])
            # levels this candle tested
            cands = []
            run_lo = L[day_start:i].min() if i > day_start else np.nan
            run_hi = H[day_start:i].max() if i > day_start else np.nan
            if side == "CE":
                if not np.isnan(run_lo) and ext <= run_lo + tol:
                    cands.append(("DAY LOW", run_lo))
                cands += [(f"swing low {ts.iloc[max(0, j - R['levels']['swing_k'])]:%d-%b %H:%M}", p)
                          for j, p in recent(lo_j, sw_lo, i) if abs(ext - p) <= tol]
                cands += [(nm, p) for nm, p, s in lv_cache[days[i]] if s in ("sup", "both") and ext <= p + tol and C[i] > p - tol]
            else:
                if not np.isnan(run_hi) and ext >= run_hi - tol:
                    cands.append(("DAY HIGH", run_hi))
                cands += [(f"swing high {ts.iloc[max(0, j - R['levels']['swing_k'])]:%d-%b %H:%M}", p)
                          for j, p in recent(hi_j, sw_hi, i) if abs(ext - p) <= tol]
                cands += [(nm, p) for nm, p, s in lv_cache[days[i]] if s in ("res", "both") and ext >= p - tol and C[i] < p + tol]
            if not cands:
                continue
            pattern = ("double hammer" if dbl else "hammer") if side == "CE" else "inverted hammer"
            row = {"idx": i, "time": ts.iloc[i], "day": days[i], "side": side, "pattern": pattern,
                   "levels": "; ".join(f"{nm} {p:,.0f}" for nm, p in cands[:3]), "level_types": ",".join(sorted({_ltype(nm) for nm, _ in cands})),
                   "pattern_high": H[i], "pattern_low": min(L[i], L[i - 1]) if dbl else L[i],
                   "ema_reclaim": bool((C[i] > ema[i] and C[i - 1] <= ema[i - 1]) if side == "CE" and i > 0
                                       else (C[i] < ema[i] and C[i - 1] >= ema[i - 1]) if i > 0 else False),
                   "triggered": False}
            # follow-through: break of the pattern candle's high (CE) / low (PE) within trigger_bars, same day
            brk = H[i] + T["buffer_points"] if side == "CE" else L[i] - T["buffer_points"]
            j = next((k for k in range(i + 1, min(n, i + 1 + T["trigger_bars"]))
                      if days[k] == days[i] and ((H[k] >= brk) if side == "CE" else (L[k] <= brk))), None)
            if j is not None:
                entry = max(brk, O[j]) if side == "CE" else min(brk, O[j])
                sl = (row["pattern_low"] - R["risk"]["sl_buffer_points"]) if side == "CE" else (H[i] + R["risk"]["sl_buffer_points"])
                risk = abs(entry - sl)
                if R["risk"]["min_sl_points"] <= risk <= R["risk"]["max_sl_points"]:
                    t1 = _target(side, entry, risk, c, i, days, day_start, recent(hi_j, sw_hi, i + 1),
                                 recent(lo_j, sw_lo, i + 1), lv_cache[days[i]], R)
                    end = j
                    while end + 1 < n and days[end + 1] == days[j] and ts.iloc[end + 1].strftime("%H:%M") <= X["exit_by"]:
                        end += 1
                    row.update(triggered=True, trigger_idx=j, trigger_time=ts.iloc[j], entry=entry, sl=sl, risk=risk,
                               t1=t1, **_exits(side, O, H, L, C, ema, j, end, entry, sl, risk, t1, X))
                else:
                    row["skip"] = f"SL {risk:.0f} pts outside {R['risk']['min_sl_points']}-{R['risk']['max_sl_points']}"
            rows.append(row)
    out = pd.DataFrame(rows)
    return add_opp_exit(c, out, R) if len(out) else out


def _opp_exit_r(side, H, L, C, days, j, end, entry, sl, risk, opp_idx) -> tuple[float, str]:
    """Your exit: hold until the OPPOSITE pattern forms at a level (IH at resistance for a CE), exit at its close.
    SL still applies; anything open is closed at `end`."""
    s = 1 if side == "CE" else -1
    for k in range(j, end + 1):
        if (L[k] <= sl) if s > 0 else (H[k] >= sl):
            return -1.0, "SL"
        if k > j and k in opp_idx:
            return s * (C[k] - entry) / risk, "OPP"
    return s * (C[end] - entry) / risk, "CLOSE"


def add_opp_exit(c: pd.DataFrame, s: pd.DataFrame, R: dict, opp_sets: dict | None = None) -> pd.DataFrame:
    """Adds r_opp / exit_opp to triggered rows. opp_sets = {'CE': set(idx of IH setups), 'PE': set(idx of hammer setups)}."""
    H, L, C = (c[k].values.astype(float) for k in ("high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    days, tod = ts.dt.date.values, ts.dt.strftime("%H:%M").values
    if opp_sets is None:
        opp_sets = {"CE": set(s.loc[s["side"] == "PE", "idx"].astype(int)), "PE": set(s.loc[s["side"] == "CE", "idx"].astype(int))}
    r_opp, e_opp = [], []
    for _, x in s.iterrows():
        if not x.get("triggered"):
            r_opp.append(np.nan)
            e_opp.append("")
            continue
        j = int(x["trigger_idx"])
        end = j
        while end + 1 < len(c) and days[end + 1] == days[j] and tod[end + 1] <= R["exit"]["exit_by"]:
            end += 1
        r, e = _opp_exit_r(x["side"], H, L, C, days, j, end, x["entry"], x["sl"], x["risk"], opp_sets[x["side"]])
        r_opp.append(r)
        e_opp.append(e)
    s = s.copy()
    s["r_opp"], s["exit_opp"] = r_opp, e_opp
    return s


def _ltype(nm: str) -> str:
    n = nm.lower()
    return ("day" if n.startswith("day") else "swing" if n.startswith("swing") else "prev_day" if n in ("pdh", "pdl", "pdc")
            else "wick" if "wick" in n else "oi_wall" if "wall" in n else "manual")


def _target(side, entry, risk, c, i, days, day_start, sw_hi, sw_lo, lv, R):
    H, L = c["high"].values, c["low"].values
    opp = []
    if side == "CE":
        opp += [H[day_start:i + 1].max()] + [p for j, p in sw_hi if j <= i] + [p for _, p, s in lv if s in ("res", "both")]
        opp = sorted(p for p in opp if p - entry >= R["risk"]["min_rr"] * risk)
    else:
        opp += [L[day_start:i + 1].min()] + [p for j, p in sw_lo if j <= i] + [p for _, p, s in lv if s in ("sup", "both")]
        opp = sorted((p for p in opp if entry - p >= R["risk"]["min_rr"] * risk), reverse=True)
    return float(opp[0]) if opp else float(entry + (1 if side == "CE" else -1) * R["exit"]["fallback_r"] * risk)


def _exits(side, O, H, L, C, ema, j, end, entry, sl, risk, t1, X) -> dict:
    """Three ways you exit: T1 level; trailing SL under each closed candle once +trail_after_r; 8-EMA close."""
    s = 1 if side == "CE" else -1
    out = {}
    for mode in ("t1", "trail", "ema"):
        stop, r, how = sl, None, "CLOSE"
        for k in range(j, end + 1):
            first = k == j
            hit_sl = (L[k] <= stop) if s > 0 else (H[k] >= stop)
            if hit_sl:
                r, how = s * (stop - entry) / risk, "SL" if stop == sl else "TSL"
                break
            if mode == "t1" and ((H[k] >= t1) if s > 0 else (L[k] <= t1)):
                r, how = abs(t1 - entry) / risk, "T1"
                break
            if mode == "ema" and not first and ((C[k] < ema[k]) if s > 0 else (C[k] > ema[k])):
                r, how = s * (C[k] - entry) / risk, "EMA"
                break
            if mode == "trail" and s * (C[k] - entry) >= X["trail_after_r"] * risk:
                stop = max(stop, L[k] - 1) if s > 0 else min(stop, H[k] + 1)
        if r is None:
            r = s * (C[end] - entry) / risk
        out[f"r_{mode}"], out[f"exit_{mode}"] = r, how
    return out


# ─────────────────────────────── confluence (live) ──────────────────────────
_NUM = r"([-+]?[\d,]+(?:\.\d+)?)"


def briefing_context(path: Path | None = None) -> dict:
    """The app's own latest numbers, read from the last 5-MIN / 15-MIN block of agt_briefing_watch.log."""
    p = path or (Path(getattr(config, "DATA_DIR", HERE / "data")) / "agt_briefing_watch.log")
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 60000))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return {}
    blocks = re.split(r"\n──── 🟦 5-MIN|\n──── 🟪 15-MIN", tail)
    b = blocks[-1] if len(blocks) > 1 else ""
    ctx = {}

    def grab(pat, key, conv=lambda x: float(x.replace(",", ""))):
        # latest value anywhere in the tail (the full briefing has PCR / writers ratio, the 5-min block has RVOL)
        m = re.findall(pat, b) or re.findall(pat, tail)
        if m:
            try:
                ctx[key] = conv(m[-1])
            except ValueError:
                pass
    grab(r"RVOL:\s*" + _NUM + "×", "rvol")
    grab(r"IV:\s*(\d+)(?:st|nd|rd|th) pct", "iv_pct")
    grab(r"IV:.*pct\s+(\w+)", "iv_word", str)
    grab(r"GEX:\s*([-+]?₹[-\d]+Cr[^\n]*)", "gex", str)
    grab(r"Floor:\s*" + _NUM + r" PE", "floor")
    grab(r"Ceiling:\s*" + _NUM + r" CE", "ceiling")
    grab(r"Dominance:\s*(\w+)", "dominance", str)
    grab(r"WRITERS RATIO:\s*" + _NUM + "x", "writers_ratio")
    grab(r"PCR \(ATM[^)]*\):\s*" + _NUM, "pcr")
    m = re.search(r"\|\s*(\d\d:\d\d)\s*\|", b)
    ctx["time"] = m.group(1) if m else None
    return ctx


def confluence(side: str, price: float, level_types: str, ctx: dict, R: dict) -> list[tuple[bool, str]]:
    """Each item: (supports this trade?, text). price = the pattern candle's low (CE) / high (PE)."""
    bull = side == "CE"
    C = R["confluence"]
    out = []
    w = ctx.get("floor") if bull else ctx.get("ceiling")
    if w:
        near = abs(w - price) <= C["wall_near_points"]
        out.append((near, f"{'PE floor' if bull else 'CE ceiling'} {w:,.0f} {'at' if near else f'{abs(w - price):.0f} pts from'} the level"))
    if "pcr" in ctx:
        out.append(((ctx["pcr"] >= C["pcr_bull"]) if bull else (ctx["pcr"] <= C["pcr_bear"]), f"PCR {ctx['pcr']:.2f}"))
    if "writers_ratio" in ctx:
        wr = ctx["writers_ratio"]
        out.append(((wr >= 1.0) if bull else (wr <= 1.0), f"writers ratio {wr:.2f}x ({'PE' if wr >= 1 else 'CE'} side)"))
    if "dominance" in ctx:
        d = ctx["dominance"].upper()
        out.append(((d == "PUTS") if bull else (d == "CALLS"), f"dominance {d.lower()}"))
    if "rvol" in ctx:
        out.append((ctx["rvol"] >= C["rvol_min"], f"RVOL {ctx['rvol']:.2f}x"))
    if "gex" in ctx:
        g = ctx["gex"].upper()
        out.append(("LONG" in g, "long gamma (range day, reversals at levels hold)" if "LONG" in g
                    else "short gamma (trending - reversals fail more)" if "SHORT" in g else "gamma neutral"))
    if "iv_word" in ctx:
        out.append((ctx["iv_word"].upper() != "EXPENSIVE", f"IV {ctx.get('iv_pct', 0):.0f}th pct {ctx['iv_word'].lower()}"))
    if "wick" in level_types:
        out.append((True, "previous-day wick at this level"))
    return out


# ─────────────────────────────── timeframes ─────────────────────────────────
TF_MIN = {"15m": 15, "5m": 5, "3m": 3}
TF_TAG = {"15m": "[PA 15m SUPER]", "5m": "[PA 5m]", "3m": "[PA 3m]"}
TF_HEAD = {"15m": "🟪🟪 15-MIN SUPER SIGNAL", "5m": "🟦 5-MIN signal", "3m": "🟨 3-MIN signal (noisy)"}


def rules_for(tf: str, R: dict | None = None) -> dict:
    """Base rules with the timeframe's overrides (smaller tolerance / SL on 5-min and 3-min)."""
    import copy
    R = copy.deepcopy(R or rules())
    for sec, vals in R.get("tf_overrides", {}).get(tf, {}).items():
        if isinstance(vals, dict) and isinstance(R.get(sec), dict):
            R[sec].update(vals)
    return R


def _resample(m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    x = m.copy()
    x["timestamp"] = pd.to_datetime(x["timestamp"])
    x = x.set_index("timestamp").between_time("09:15", "15:29")
    out = [g[["open", "high", "low", "close"]].resample(f"{minutes}min", origin="start_day", offset="9h15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna() for _, g in x.groupby(x.index.date)]
    return pd.concat(out).reset_index() if out else pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])


def _minute_data(day: date, days_before: int) -> pd.DataFrame | None:
    """1-min index candles up to `day`: live candle source for today, stored data/candles/*.csv otherwise."""
    frames = []
    if day == date.today():
        try:
            import candle_source
            idx, _ = candle_source.fetch(days_before + 1)
            if idx is not None and len(idx):
                frames.append(idx[["timestamp", "open", "high", "low", "close"]])
        except Exception as exc:
            log.info("live 1-min candles unavailable: %s", exc)
    d = Path(getattr(config, "CANDLE_DIR", HERE / "data" / "candles"))
    files = [f for f in sorted(d.glob("20*.csv")) if f.stem <= f"{day:%Y-%m-%d}"][-(days_before + 1):] if d.exists() else []
    for f in files:
        try:
            frames.append(pd.read_csv(f, usecols=["timestamp", "open", "high", "low", "close"]))
        except (OSError, ValueError) as exc:
            log.info("skip %s: %s", f.name, exc)
    if not frames:
        return None
    m = pd.concat(frames, ignore_index=True)
    m["timestamp"] = pd.to_datetime(m["timestamp"], errors="coerce")
    m = m.dropna().drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    return m[m["timestamp"].dt.date <= day]


def tf_candles(tf: str, day: date, days_before: int = 10) -> pd.DataFrame | None:
    if tf == "15m":
        return day_candles(day, days_before)
    m = _minute_data(day, days_before)
    if m is None or not len(m):
        return None
    return _resample(m, TF_MIN[tf]).reset_index(drop=True)


# ─────────────────────────────── live / day view ────────────────────────────
def day_candles(day: date, days_before: int = 10) -> pd.DataFrame | None:
    c, src = ms.recent_candles(days_before + 1)
    h = None
    if c is None or day not in set(pd.to_datetime(c["timestamp"]).dt.date):
        h = ms.load_history(ms.rules())
        if h is None:
            return None
        h = h[h["timestamp"].dt.date <= day]
        keep = sorted(h["timestamp"].dt.date.unique())[-(days_before + 1):]
        c = h[h["timestamp"].dt.date.isin(keep)]
    c = c[pd.to_datetime(c["timestamp"]).dt.date <= day].reset_index(drop=True)
    return c


def live(now: datetime | None = None, tfs: list[str] | None = None) -> dict:
    """Per timeframe: closed candles today, setups, triggers + confluence from the latest briefing block."""
    base = rules()
    now = now or datetime.now()
    ctx = briefing_context()
    oi = {"floor": ctx.get("floor"), "ceiling": ctx.get("ceiling")}
    out = {"ctx": ctx}
    for tf in tfs or base.get("timeframes", ["15m", "5m", "3m"]):
        R = rules_for(tf, base)
        try:
            c = tf_candles(tf, now.date())
        except Exception as exc:
            log.info("%s candles unavailable: %s", tf, exc)
            c = None
        if c is None or not len(c):
            continue
        c = c[pd.to_datetime(c["timestamp"]) + timedelta(minutes=TF_MIN[tf]) <= now].reset_index(drop=True)
        if not len(c) or pd.to_datetime(c["timestamp"]).dt.date.iloc[-1] != now.date():
            continue
        s = scan(c, R, oi=oi, only_day=now.date())
        out[tf] = {"setups": s, "last_idx": len(c) - 1, "candles": c, "R": R}
    return out


def fmt_setup(x: dict, ctx: dict, R: dict, verdict: str, tf: str = "15m") -> str:
    buy = x["side"] == "CE"
    icon = "🟢" if buy else "🔴"
    head = (f"{TF_TAG[tf]} {TF_HEAD[tf]}\n{icon} PA SETUP · {x['side']} · {pd.Timestamp(x['time']):%H:%M} candle\n"
            f"{x['pattern'].upper()} at {x['levels']}" + (" · closed back through the 8 EMA" if x.get("ema_reclaim") else ""))
    trig = (f"\n{'BUY CE above' if buy else 'BUY PE below'} {x['pattern_high'] + R['trigger']['buffer_points'] if buy else x['pattern_low'] - R['trigger']['buffer_points']:,.0f}"
            f" (follow-through) · SL {x['pattern_low'] - R['risk']['sl_buffer_points'] if buy else x['pattern_high'] + R['risk']['sl_buffer_points']:,.0f}"
            f" · valid for {R['trigger']['trigger_bars']} candles")
    cf = confluence(x["side"], x["pattern_low"] if buy else x["pattern_high"], x.get("level_types", ""), ctx, R)
    ok = sum(1 for a, _ in cf if a)
    conf = ("\nConfluence " + f"{ok}/{len(cf)}: " + " · ".join(("✔ " if a else "✘ ") + t for a, t in cf)) if cf else ""
    return head + trig + conf + f"\nBacktest ({tf}): {verdict}"


def fmt_trigger(x: dict, tf: str = "15m") -> str:
    buy = x["side"] == "CE"
    return (f"{TF_TAG[tf]} {TF_HEAD[tf]}\n✅ PA TRIGGERED · {x['side']} at {x['entry']:,.0f} "
            f"({pd.Timestamp(x['trigger_time']):%H:%M} broke the {x['pattern']} {'high' if buy else 'low'})\n"
            f"SL {x['sl']:,.0f} ({x['risk']:.0f} pts) · T1 {x['t1']:,.0f} · then trail "
            f"{'under' if buy else 'over'} each closed {TF_MIN[tf]}-min candle, or exit on a close "
            f"{'below' if buy else 'above'} the {tf} 8 EMA")


def verdict_text(tf: str = "15m") -> str:
    try:
        v = json.loads(VERDICT_PATH.read_text())
        a = v.get(tf, v if tf == "15m" else {}).get("all", {})
        return f"{a.get('verdict', 'not tested')} ({a.get('detail', '')})" if a else "not tested yet"
    except (OSError, ValueError, AttributeError):
        return f"not tested yet - run pa_signals.py --backtest --tf {tf}"


# ─────────────────────────────── backtest ───────────────────────────────────
def random_entries(c: pd.DataFrame, t: pd.DataFrame, R: dict, k: int = 5, seed: int = 7,
                   opp_sets: dict | None = None) -> pd.DataFrame:
    """Like-for-like control: for every real setup, k RANDOM candles of the same day stand in for the 'pattern';
    the same follow-through break (their high / low + buffer, within trigger_bars), the same SL distance, the same
    T1 distance and the same three exits. So only the pattern + level differ from the real trades."""
    X, T = R["exit"], R["trigger"]
    c = c.reset_index(drop=True)
    O, H, L, C = (c[x].values.astype(float) for x in ("open", "high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    days, tod = ts.dt.date.values, ts.dt.strftime("%H:%M").values
    ema = c["close"].ewm(span=X["ema_span"], adjust=False).mean().values
    by_day = pd.Series(np.arange(len(c))).groupby(days).apply(list).to_dict()
    rng = np.random.default_rng(seed)
    n = len(c)
    rows = []
    opp_sets = opp_sets or {"CE": set(), "PE": set()}
    for _, x in t.iterrows():
        pool = [i for i in by_day[x["day"]] if "09:30" <= tod[i] <= "14:30"]
        for i in rng.choice(pool, size=min(k, len(pool)), replace=False):
            s = 1 if x["side"] == "CE" else -1
            brk = H[i] + T["buffer_points"] if s > 0 else L[i] - T["buffer_points"]
            j = next((q for q in range(i + 1, min(n, i + 1 + T["trigger_bars"]))
                      if days[q] == days[i] and ((H[q] >= brk) if s > 0 else (L[q] <= brk))), None)
            if j is None:
                continue
            entry = max(brk, O[j]) if s > 0 else min(brk, O[j])
            risk = x["risk"]
            sl, t1 = entry - s * risk, entry + s * abs(x["t1"] - x["entry"])
            end = j
            while end + 1 < n and days[end + 1] == days[j] and tod[end + 1] <= X["exit_by"]:
                end += 1
            r_o, e_o = _opp_exit_r(x["side"], H, L, C, days, j, end, entry, sl, risk, opp_sets[x["side"]])
            rows.append({"side": x["side"], **_exits(x["side"], O, H, L, C, ema, j, end, entry, sl, risk, t1, X),
                         "r_opp": r_o, "exit_opp": e_o})
    return pd.DataFrame(rows)


def _v(a: pd.Series, b: pd.Series, R: dict, key: str) -> dict:
    Hh = R["history"]
    if len(a) < Hh["min_sample_size"] or len(b) < Hh["min_sample_size"]:
        return {"verdict": "NOT ENOUGH DATA", "level": 0, "n": int(len(a)), "detail": f"{len(a)} trades"}
    d = a.mean() - b.mean()
    se = float(np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)))
    z = d / se if se > 0 else 0.0
    lvl = (1 if d >= Hh["beats_by_r"] else -1 if d <= -Hh["beats_by_r"] else 0) if abs(z) >= Hh["min_z"] else 0
    v = {1: "BEATS RANDOM", 0: "NO EDGE", -1: "WORSE THAN RANDOM"}[lvl]
    net = a.mean() - Hh.get("cost_r", 0.05)          # brokerage + slippage per trade, in R
    if lvl > 0 and net < Hh.get("min_net_r", 0.05):  # beats random but doesn't pay after costs: not a signal
        v, lvl = ("BETTER THAN RANDOM BUT LOSES" if a.mean() <= 0 else "BETTER THAN RANDOM, ~BREAKEVEN AFTER COSTS"), 0
    if lvl > 0 and len(a) < Hh["min_trades_for_star"]:
        v, lvl = "PROMISING, SMALL SAMPLE", 0
    return {"verdict": v, "level": lvl, "n": int(len(a)),
            "detail": f"{a.mean():+.2f} R vs {b.mean():+.2f} R at random times, {len(a)} trades, gap {z:.1f}x noise"}


def backtest(R: dict | None = None, tf: str = "15m") -> dict:
    R = rules_for(tf, R)
    if tf == "15m":
        h = ms.load_history(ms.rules())
    else:
        # 5-min / 3-min from the Dhan 1-min history (mentor_traps.py fetches and caches it); else data/candles
        h = None
        try:
            import mentor_traps as mt
            m1 = mt.minute_history()
            if m1 is not None and len(m1):
                h = _resample(m1, TF_MIN[tf])
        except Exception as exc:
            log.info("1-min history unavailable (%s); using data/candles", exc)
        if h is None or not len(h):
            m = _minute_data(date.today(), 400)
            h = _resample(m, TF_MIN[tf]) if m is not None else None
    if h is None or len(h) < 1000:
        return {"error": f"no {tf} history"}
    c = h.reset_index(drop=True)
    s = scan(c, R)
    t = s[s["triggered"]] if len(s) else s
    if not len(t):
        return {"error": "no triggered setups"}
    opp = {"CE": set(s.loc[s["side"] == "PE", "idx"].astype(int)), "PE": set(s.loc[s["side"] == "CE", "idx"].astype(int))}
    rnd = random_entries(c, t, R, opp_sets=opp)
    rows, verd = [], {}

    def row(x, lab, mode="trail"):
        n = len(x)
        if n < R["history"]["min_sample_size"]:
            return {"Setup": lab, "Trades": n, "Avg R (trail)": "INSUFFICIENT SAMPLE"}
        return {"Setup": lab, "Trades": n, "Avg R (trail)": round(x["r_trail"].mean(), 2), "Avg R (T1)": round(x["r_t1"].mean(), 2),
                "Avg R (8 EMA)": round(x["r_ema"].mean(), 2), "Win % (trail)": round(100 * (x["r_trail"] > 0).mean(), 1),
                "T1 hit %": round(100 * (x["exit_t1"] == "T1").mean(), 1),
                "Avg R (exit at opposite pattern)": round(x["r_opp"].mean(), 2) if "r_opp" in x else ""}
    rows.append(row(t, "ALL triggered setups"))
    rows.append(row(rnd, "CONTROL: random candles, same break entry / SL / exits (5 each)"))
    for side in ("CE", "PE"):
        rows.append(row(t[t["side"] == side], f"  {side}"))
    for p in ("hammer", "double hammer", "inverted hammer"):
        rows.append(row(t[t["pattern"] == p], f"  {p}"))
    for lt in ("day", "swing", "prev_day", "wick"):
        rows.append(row(t[t["level_types"].str.contains(lt)], f"  at {lt} levels"))
    rows.append(row(t[t["ema_reclaim"]], "  with the 8-EMA reclaim"))
    rows.append(row(t[t["level_types"].str.count(",") >= 1], "  two or more level types together"))
    for mode in ("trail", "t1", "ema", "opp"):
        verd[f"all_{mode}"] = _v(t[f"r_{mode}"], rnd[f"r_{mode}"], R, mode)
    verd["all"] = verd["all_trail"]
    verd["tf"] = tf
    return {"table": pd.DataFrame(rows), "verdicts": verd, "setups": len(s), "triggered": len(t),
            "span": (c["timestamp"].min(), c["timestamp"].max())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--day")
    ap.add_argument("--tf", default="all", help="15m, 5m, 3m or all")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 240, "display.max_columns", 20, "display.max_colwidth", 70)
    R = rules()
    tfs = R.get("timeframes", ["15m", "5m", "3m"]) if a.tf == "all" else [a.tf]
    if a.backtest:
        allv = {}
        try:
            allv = json.loads(VERDICT_PATH.read_text())
        except (OSError, ValueError):
            pass
        allv = {k: v for k, v in allv.items() if k in TF_MIN}
        for tf in tfs:
            b = backtest(R, tf)
            if "error" in b:
                print(f"{tf}: {b['error']}")
                continue
            print(f"\n===== {tf}: {b['span'][0]:%d-%b-%Y} .. {b['span'][1]:%d-%b-%Y}: {b['setups']:,} setups, "
                  f"{b['triggered']:,} triggered =====")
            print(b["table"].to_string(index=False))
            for k, v in b["verdicts"].items():
                if isinstance(v, dict):
                    print(f"{k:10} {v['verdict']:28} {v['detail']}")
            allv[tf] = b["verdicts"]
        VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
        VERDICT_PATH.write_text(json.dumps({"run": str(date.today()), **allv}, indent=1))
        print(f"saved {VERDICT_PATH}")
        return 0
    day = pd.Timestamp(a.day).date() if a.day else date.today()
    for tf in tfs:
        Rt = rules_for(tf, R)
        c = tf_candles(tf, day)
        print(f"\n===== {TF_HEAD[tf]} - {day:%a %d-%b-%Y} =====")
        if c is None or day not in set(pd.to_datetime(c["timestamp"]).dt.date):
            print(f"no {tf} candles for {day}")
            continue
        s = scan(c, Rt, only_day=day)
        if not len(s):
            print("no hammer / inverted hammer at a level")
            continue
        for _, x in s.iterrows():
            line = f"{pd.Timestamp(x['time']):%H:%M} {x['side']} {x['pattern']:15} at {x['levels']}"
            if x["triggered"]:
                line += (f"\n      -> TRIGGERED {pd.Timestamp(x['trigger_time']):%H:%M} entry {x['entry']:,.0f} SL {x['sl']:,.0f} "
                         f"T1 {x['t1']:,.0f} | trail {x['exit_trail']} {x['r_trail'] * x['risk']:+.0f} pts | "
                         f"T1 {x['exit_t1']} {x['r_t1'] * x['risk']:+.0f} | 8 EMA {x['exit_ema']} {x['r_ema'] * x['risk']:+.0f} | "
                         f"opposite pattern {x['exit_opp']} {x['r_opp'] * x['risk']:+.0f}")
            else:
                line += f"\n      -> {x.get('skip') if isinstance(x.get('skip'), str) else 'no follow-through'}"
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
