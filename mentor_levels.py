"""
mentor_levels.py — price action at support / resistance: rejection wicks (hammer at support, inverted hammer /
shooting star at resistance) confirmed by a follow-through candle. Your 29-Sep-2026 homework:

  Trade 1  CE  hammer at the 22,542 support (a daily low from 06-Apr) + follow-through -> exit near the day's open
  Trade 2  PE  inverted hammer at the day's high (resistance) + follow-through       -> exit at the 8 EMA

  .venv/bin/python mentor_levels.py               # levels near price now + any fresh signal (15-min and 3-min)
  .venv/bin/python mentor_levels.py --backtest    # 15-min (1 year) and 3-min (your data/candles days) vs controls

Levels (numbers in mentor_levels_rules.json):
  swing       daily swing lows / highs (a low with 3 higher-low days each side), known only after they formed
  prior_day   previous day's high, low, close
  day_extreme today's high / low so far
  manual      your own levels in data/manual_levels.json, e.g. {"levels": [22542, 23000]}  (live only)

Two controls, because two things have to be true for this to work:
  shuffled bars   same candles in random order, levels rebuilt from them  -> is the CANDLE rule better than noise?
  fake levels     real candles, every level moved 40-150 pts at random    -> do the LEVELS matter?
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import config
import mentor_structure as ms
import mentor_sweeps as msw

log = logging.getLogger("mentor_levels")
HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "mentor_levels_rules.json"
VERDICT_PATH = HERE / "data" / "mentor_levels_verdict.json"
MANUAL_PATH = HERE / "data" / "manual_levels.json"
TYPES = ("swing", "prior_day", "day_extreme", "manual")


def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


# ─────────────────────────────── data ───────────────────────────────────────
def minute_candles(minutes: int = 3, days: int | None = None) -> pd.DataFrame | None:
    """Index 1-min candles stored in data/candles/ (one CSV per day), resampled to `minutes`."""
    d = Path(getattr(config, "CANDLE_DIR", HERE / "data" / "candles"))
    files = sorted(d.glob("20*.csv")) if d.exists() else []
    if days:
        files = files[-days:]
    frames = []
    for f in files:
        try:
            x = pd.read_csv(f, usecols=["timestamp", "open", "high", "low", "close"])
            frames.append(x)
        except (OSError, ValueError) as exc:
            log.info("skip %s: %s", f.name, exc)
    if not frames:
        return None
    m = pd.concat(frames, ignore_index=True)
    m["timestamp"] = pd.to_datetime(m["timestamp"], errors="coerce")
    m = m.dropna().drop_duplicates("timestamp").sort_values("timestamp").set_index("timestamp")
    m = m.between_time("09:15", "15:29")
    out = []
    for _, g in m.groupby(m.index.date):
        r = g.resample(f"{minutes}min", origin="start_day", offset="9h15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        out.append(r)
    return pd.concat(out).reset_index() if out else None


def daily_of(c: pd.DataFrame) -> pd.DataFrame:
    g = c.groupby(c["timestamp"].dt.date)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last()})
    d.index.name = "date"
    return d.reset_index()


def swing_levels(daily: pd.DataFrame, k: int) -> list[tuple[str, float, date]]:
    """(side, price, known_from) — a swing is only known k days after it formed."""
    H, L = daily["high"].values, daily["low"].values
    dts = list(pd.to_datetime(daily["date"]).dt.date)
    out = []
    for j in range(k, len(daily) - k):
        if L[j] == L[j - k:j + k + 1].min():
            out.append(("support", float(L[j]), dts[j + k]))
        if H[j] == H[j - k:j + k + 1].max():
            out.append(("resistance", float(H[j]), dts[j + k]))
    return out


def manual_levels() -> list[float]:
    try:
        return [float(x) for x in json.loads(MANUAL_PATH.read_text()).get("levels", [])]
    except (OSError, ValueError):
        return []


# ─────────────────────────────── signals ────────────────────────────────────
def signals(c: pd.DataFrame, R: dict | None = None, tf: str = "15m", daily: pd.DataFrame | None = None,
            shift: np.ndarray | None = None, extra: list[float] | None = None) -> pd.DataFrame:
    """Rejection candle at a level + follow-through candle; three exits simulated.
    daily: the daily bars for swing / prior-day levels (default: built from `c`).
    shift: random offsets for the fake-level control. extra: manual levels (live)."""
    R = R or rules()
    P, K = R["pattern"], R["risk"]
    c = c.reset_index(drop=True)
    O, H, L, C = (c[k].values.astype(float) for k in ("open", "high", "low", "close"))
    ts = pd.to_datetime(c["timestamp"])
    days = ts.dt.date.values
    n = len(c)
    ema = c["close"].ewm(span=R["exit"]["ema_span"], adjust=False).mean().values
    daily = daily_of(c) if daily is None else daily
    ddates = list(pd.to_datetime(daily["date"]).dt.date)
    sw = swing_levels(daily, R["levels"]["swing_k"])
    look = R["levels"]["swing_lookback_days"]
    tol = R["levels"]["tolerance_points"][tf]
    minr = P["min_range_points"][tf]
    buf, min_sl = K["sl_buffer_points"], K["min_sl_points"][tf]
    min_bars = P["min_bars_into_day"][tf]
    rows = []
    day_start = 0
    cache: dict = {}

    def levels_for(d):
        if d in cache:
            return cache[d]
        pos = next((p for p, x in enumerate(ddates) if x >= d), len(ddates))
        lv = []
        if pos > 0:
            pd_ = daily.iloc[pos - 1]
            lv += [("prior_day", float(pd_["high"])), ("prior_day", float(pd_["low"])), ("prior_day", float(pd_["close"]))]
        oldest = ddates[max(0, pos - look)] if ddates else d
        lv += [("swing", p) for _, p, known in sw if known < d and known >= oldest]
        lv += [("manual", float(p)) for p in (extra or [])]
        if shift is not None and len(lv):
            lv = [(t, p + shift[int(round(p)) % len(shift)]) for t, p in lv]
        cache[d] = lv
        return lv

    for i in range(n - 1):
        if i == 0 or days[i] != days[i - 1]:
            day_start = i
        if days[i + 1] != days[i] or i - day_start < min_bars:
            continue
        rng = H[i] - L[i]
        if rng < minr:
            continue
        body = abs(C[i] - O[i])
        lw, uw = min(O[i], C[i]) - L[i], H[i] - max(O[i], C[i])
        run_hi, run_lo = H[day_start:i].max(), L[day_start:i].min()
        lv = levels_for(days[i]) + [("day_extreme", run_lo), ("day_extreme", run_hi)]
        for side, wick in (("bull", lw), ("bear", uw)):
            if wick < P["wick_body_ratio"] * body or wick < P["wick_range_share"] * rng:
                continue
            if side == "bull":     # low pierced/touched the level, closed back above it
                hits = [(t, p) for t, p in lv if L[i] <= p + tol and C[i] > p and not (t == "day_extreme" and p == run_hi)]
            else:
                hits = [(t, p) for t, p in lv if H[i] >= p - tol and C[i] < p and not (t == "day_extreme" and p == run_lo)]
            if not hits:
                continue
            t_, lvl = sorted(hits, key=lambda h: (TYPES.index(h[0]), abs(h[1] - (L[i] if side == "bull" else H[i]))))[0]
            j = i + 1
            ok = (C[j] > C[i] and C[j] > O[j]) if side == "bull" else (C[j] < C[i] and C[j] < O[j])
            row = {"tf": tf, "idx": i, "time": ts.iloc[i], "side": side, "level_type": t_, "level": round(lvl, 1),
                   "confirmed": bool(ok), "entry": np.nan, "sl": np.nan, "target": np.nan, "passes": False}
            if ok:
                entry = C[j]
                sl = L[i] - buf if side == "bull" else H[i] + buf
                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                opp = [p for _, p in lv + [("x", O[day_start]), ("x", H[day_start:j + 1].max()), ("x", L[day_start:j + 1].min())]]
                if side == "bull":
                    tg = sorted(p for p in opp if p - entry >= K["min_rr"] * risk)
                else:
                    tg = sorted((p for p in opp if entry - p >= K["min_rr"] * risk), reverse=True)
                target = tg[0] if tg else (entry + R["exit"]["fallback_r"] * risk if side == "bull"
                                           else entry - R["exit"]["fallback_r"] * risk)
                end = j
                while end + 1 < n and days[end + 1] == days[j]:
                    end += 1
                row.update(entry=entry, sl=sl, target=target, risk=risk, passes=risk >= min_sl,
                           target_is_level=bool(tg), **_exits(H, L, C, ema, side, j, end, entry, sl, target, risk))
            rows.append(row)
    return pd.DataFrame(rows)


def _exits(H, L, C, ema, side, j, end, entry, sl, target, risk) -> dict:
    """R for three exits: level target, 8-EMA close against the trade, and whichever comes first."""
    s = 1 if side == "bull" else -1
    out = {}
    for mode in ("level", "ema", "first"):
        r, how = None, "CLOSE"
        for k in range(j + 1, end + 1):
            if (L[k] <= sl) if s > 0 else (H[k] >= sl):
                r, how = -1.0, "SL"
                break
            if mode in ("level", "first") and ((H[k] >= target) if s > 0 else (L[k] <= target)):
                r, how = abs(target - entry) / risk, "TARGET"
                break
            if mode in ("ema", "first") and ((C[k] < ema[k]) if s > 0 else (C[k] > ema[k])):
                r, how = s * (C[k] - entry) / risk, "EMA"
                break
        if r is None:
            r = s * (C[end] - entry) / risk if end > j else 0.0
        out[f"r_{mode}"], out[f"exit_{mode}"] = r, how
    return out


# ─────────────────────────────── backtest ───────────────────────────────────
def _row(df: pd.DataFrame, lab: str, tf: str, minimum: int) -> dict:
    n = len(df)
    base = {"Setup": lab, "TF": tf, "Trades": n}
    if n < minimum:
        return {**base, "Avg R (level exit)": "INSUFFICIENT SAMPLE", "Avg R (8 EMA exit)": "", "Avg R (first of both)": "",
                "Target hit %": "", "SL hit %": ""}
    return {**base, "Avg R (level exit)": round(df["r_level"].mean(), 2), "Avg R (8 EMA exit)": round(df["r_ema"].mean(), 2),
            "Avg R (first of both)": round(df["r_first"].mean(), 2),
            "Target hit %": round(100 * (df["exit_first"] == "TARGET").mean(), 1),
            "SL hit %": round(100 * (df["exit_first"] == "SL").mean(), 1)}


def backtest(R: dict | None = None) -> dict:
    R = R or rules()
    minimum = R["history"]["min_sample_size"]
    frames = {}
    h15 = ms.load_history(ms.rules())
    if h15 is not None and len(h15) > 500:
        frames["15m"] = h15.reset_index(drop=True)
    m3 = minute_candles(3)
    if m3 is not None and len(m3) > 1000:
        frames["3m"] = m3
    if not frames:
        return {"error": "no 15-min history and no data/candles files"}
    rng = np.random.default_rng(7)
    fake = rng.uniform(40, 150, 97) * rng.choice([-1, 1], 97)
    rows, verd = [], {}
    for tf, c in frames.items():
        real = signals(c, R, tf)
        t = real[real["confirmed"] & real["passes"]] if len(real) else real
        tr = msw.trend_series(c) if len(t) else None
        # 5 different shuffles pooled: one shuffle alone is too noisy a yardstick
        ctrl = pd.concat([signals(msw._shuffle(c, seed, k=1), R, tf) for seed in range(42, 42 + R["history"]["control_shuffles"])],
                         ignore_index=True)
        ct = ctrl[ctrl["confirmed"] & ctrl["passes"]] if len(ctrl) else ctrl
        fk = signals(c, R, tf, shift=fake)
        fk = fk[fk["confirmed"] & fk["passes"]] if len(fk) else fk
        if not len(t):
            continue
        rows.append(_row(t, "ALL rejections at a level, confirmed", tf, minimum))
        rows.append(_row(ct, f"CONTROL: shuffled bars ({R['history']['control_shuffles']} shuffles pooled)", tf, minimum))
        for typ in ("swing", "prior_day", "day_extreme"):
            x = t[t["level_type"] == typ]
            rows.append(_row(x, f"  at {typ} levels", tf, minimum))
            if typ != "day_extreme":
                rows.append(_row(fk[fk["level_type"] == typ], f"  CONTROL: {typ} levels moved 40-150 pts", tf, minimum))
            rows.append(_row(ct[ct["level_type"] == typ] if len(ct) else ct, f"  CONTROL: shuffled, {typ}", tf, minimum))
        for side, lab in (("bull", "hammers at support (buy)"), ("bear", "inverted hammers at resistance (sell)")):
            rows.append(_row(t[t["side"] == side], "  " + lab, tf, minimum))
        s = np.where(t["side"] == "bull", 1, -1)
        tt = tr[t["idx"].astype(int).values]
        rows.append(_row(t[tt == s], "  with the 15-min structure trend" if tf == "15m" else "  with the 3-min structure trend", tf, minimum))
        rows.append(_row(t[tt == -s], "  against it (reversal trades)", tf, minimum))
        rows.append(_row(real[~real["confirmed"]].assign(r_level=np.nan, r_ema=np.nan, r_first=np.nan, exit_first=""),
                         "  (no follow-through - not taken)", tf, 10 ** 9))
        # verdicts per level type (first-of-both exit) vs the shuffled control
        v = {}
        for typ in ("all", "swing", "prior_day", "day_extreme"):
            x = t if typ == "all" else t[t["level_type"] == typ]
            y = ct if typ == "all" else (ct[ct["level_type"] == typ] if len(ct) else ct)
            z = None if typ in ("all", "day_extreme") else fk[fk["level_type"] == typ]
            if len(x) < minimum or len(y) < minimum:
                v[typ] = {"verdict": "NOT ENOUGH DATA", "level": 0, "detail": f"{len(x)} trades"}
                continue
            a, b = x["r_first"].mean(), y["r_first"].mean()
            d = a - b
            lvl = 1 if d >= R["history"]["beats_by_r"] else -1 if d <= -R["history"]["beats_by_r"] else 0
            small = lvl > 0 and len(x) < R["history"].get("min_trades_for_star", 200)
            if small:
                lvl = 0
            detail = f"{a:+.2f} R vs {b:+.2f} R on shuffled bars, {len(x)} trades"
            if z is not None and len(z) >= minimum:
                detail += f"; fake levels {z['r_first'].mean():+.2f} R"
                if a - z["r_first"].mean() < R["history"]["beats_by_r"] and lvl > 0:
                    lvl, detail = 0, detail + " (levels add nothing)"
            v[typ] = {"verdict": "PROMISING, SMALL SAMPLE" if small else
                      {1: "BEATS RANDOM", 0: "NO EDGE", -1: "WORSE THAN RANDOM"}[lvl], "level": lvl,
                      "detail": detail}
        verd[tf] = v
    return {"table": pd.DataFrame(rows), "verdicts": verd,
            "span": {tf: (c["timestamp"].min(), c["timestamp"].max(), len(c)) for tf, c in frames.items()}}


def save_verdict(v: dict) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_PATH.write_text(json.dumps({"run": str(date.today()), **v}, indent=1))


def load_verdict() -> dict:
    try:
        return json.loads(VERDICT_PATH.read_text())
    except (OSError, ValueError):
        return {}


# ─────────────────────────────── live ───────────────────────────────────────
def live_frames() -> dict:
    """Closed 15-min and 3-min candles for today (+ earlier days for the levels)."""
    out = {}
    c15, _ = ms.recent_candles(5)
    if c15 is not None and len(c15):
        out["15m"] = c15.reset_index(drop=True)
    try:
        import candle_source
        idx, _ = candle_source.fetch(5)
        if idx is not None and len(idx):
            b = idx.copy()
            b["timestamp"] = pd.to_datetime(b["timestamp"])
            b = b.set_index("timestamp").between_time("09:15", "15:29")
            r = [g.resample("3min", origin="start_day", offset="9h15min").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna() for _, g in b.groupby(b.index.date)]
            out["3m"] = pd.concat(r).reset_index()
    except Exception as exc:
        log.info("3-min candles unavailable: %s", exc)
    return out


def live_signals(now=None) -> list[dict]:
    """Signals whose follow-through candle is the latest CLOSED candle today."""
    R = rules()
    now = pd.Timestamp(now or pd.Timestamp.now())
    daily = None
    try:
        import participant_correlation as pc
        dd = pc.load_ohlc()
        daily = msw.daily_frame(dd).rename(columns={"timestamp": "date"}) if dd is not None else None
        if daily is not None:
            daily["date"] = pd.to_datetime(daily["date"]).dt.date
    except Exception as exc:
        log.info("daily history unavailable (%s); levels from recent candles only", exc)
    V = load_verdict()
    out = []
    for tf, c in live_frames().items():
        mins = 15 if tf == "15m" else 3
        c = c[pd.to_datetime(c["timestamp"]) + pd.Timedelta(minutes=mins) <= now].reset_index(drop=True)
        if len(c) < 10:
            continue
        d = daily if daily is not None else daily_of(c)
        today = now.date()
        if len(d) and d["date"].iloc[-1] >= today:
            d = d[d["date"] < today]
        s = signals(c, R, tf, daily=d.reset_index(drop=True), extra=manual_levels())
        if not len(s):
            continue
        s = s[s["confirmed"] & (s["idx"] + 1 == len(c) - 1) & (pd.to_datetime(s["time"]).dt.date == today)]
        for _, x in s.iterrows():
            v = V.get(tf, {}).get(x["level_type"], V.get(tf, {}).get("all", {}))
            out.append({**x.to_dict(), "verdict": v.get("verdict", "not tested"), "verdict_level": v.get("level", 0),
                        "verdict_detail": v.get("detail", "")})
    return out


def day_report(day: date) -> pd.DataFrame:
    """Every wick candle at a level on one day (15-min and 3-min), taken or not, with the reason."""
    R = rules()
    P = R["pattern"]
    daily = None
    try:
        import participant_correlation as pc
        daily = msw.daily_frame(pc.load_ohlc()).rename(columns={"timestamp": "date"})
        daily["date"] = pd.to_datetime(daily["date"]).dt.date
        daily = daily[daily["date"] < day].reset_index(drop=True)
    except Exception as exc:
        print(f"(daily levels from the candles only: {exc})")
    frames = {}
    h = ms.load_history(ms.rules())
    if h is not None:
        frames["15m"] = h[h["timestamp"].dt.date <= day].reset_index(drop=True)
    m3 = minute_candles(3)
    if m3 is None or day not in set(m3["timestamp"].dt.date):
        lf = live_frames().get("3m")
        m3 = lf if lf is not None and day in set(pd.to_datetime(lf["timestamp"]).dt.date) else m3
    if m3 is not None:
        m3 = m3[pd.to_datetime(m3["timestamp"]).dt.date <= day].reset_index(drop=True)
        frames["3m"] = m3
    rows = []
    for tf, c in frames.items():
        c = c[pd.to_datetime(c["timestamp"]).dt.date.isin(sorted(set(pd.to_datetime(c["timestamp"]).dt.date))[-11:])]
        c = c.reset_index(drop=True)
        if day not in set(pd.to_datetime(c["timestamp"]).dt.date):
            print(f"{tf}: no candles for {day}")
            continue
        s = signals(c, R, tf, daily=daily, extra=manual_levels())
        if len(s):
            s = s[pd.to_datetime(s["time"]).dt.date == day]
            for _, x in s.iterrows():
                why = ("TAKEN" if x["passes"] else "SL below minimum") if x["confirmed"] else "no follow-through"
                rows.append({"TF": tf, "Candle": f"{pd.Timestamp(x['time']):%H:%M}",
                             "Type": "hammer (buy)" if x["side"] == "bull" else "inv. hammer (sell)",
                             "Level": f"{x['level']:,.0f} {x['level_type']}", "Status": why,
                             "Entry": "" if pd.isna(x["entry"]) else round(x["entry"]),
                             "SL": "" if pd.isna(x["sl"]) else round(x["sl"]),
                             "Target": "" if pd.isna(x["target"]) else round(x["target"]),
                             "Exit (first)": "" if not x["confirmed"] else f"{x['exit_first']} {x['r_first']:+.1f} R",
                             "Exit (8 EMA)": "" if not x["confirmed"] else f"{x['exit_ema']} {x['r_ema']:+.1f} R"})
    return pd.DataFrame(rows)


def text(x: dict) -> str:
    buy = x["side"] == "bull"
    what = "Hammer at SUPPORT" if buy else "Inverted hammer at RESISTANCE"
    return (f"{what} {x['level']:,.0f} ({x['level_type'].replace('_', ' ')}, {x['tf']}) + follow-through -> "
            f"{'BUY CE' if buy else 'BUY PE'} at {x['entry']:,.0f}, SL {x['sl']:,.0f} (beyond the wick), "
            f"target {x['target']:,.0f}{'' if x.get('target_is_level') else ' (2R, no level in reach)'} "
            f"or exit on a close {'below' if buy else 'above'} the 8 EMA"
            + ("" if x["passes"] else f" [SL only {x['risk']:.0f} pts - below the minimum, skip]"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--day", help="list every wick-at-level candle on this day, e.g. 2026-09-29")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pd.set_option("display.width", 230, "display.max_columns", 20, "display.max_colwidth", 60)
    if a.backtest:
        b = backtest()
        if "error" in b:
            print(b["error"])
            return 1
        for tf, (f, t, n) in b["span"].items():
            print(f"{tf}: {n:,} bars {f:%d-%b-%Y} .. {t:%d-%b-%Y}")
        print(b["table"].to_string(index=False))
        print("\n== verdicts (exit = target level or 8 EMA, whichever first) ==")
        for tf, v in b["verdicts"].items():
            for typ, x in v.items():
                print(f"{tf:4} {typ:12} {x['verdict']:18} {x['detail']}")
        save_verdict(b["verdicts"])
        print(f"saved {VERDICT_PATH}")
        return 0
    if a.day:
        t = day_report(pd.Timestamp(a.day).date())
        print(t.to_string(index=False) if len(t) else f"No wick candle at a level on {a.day} "
              f"(wick >= {rules()['pattern']['wick_body_ratio']}x body and >= half the candle, within "
              f"{rules()['levels']['tolerance_points']} pts of a level).")
        return 0
    sig = live_signals()
    print("\n".join(text(x) for x in sig) if sig else "No rejection-at-level signal on the latest closed candle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
